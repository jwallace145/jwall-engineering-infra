"""Cost reports: the Cost Explorer requests, the tables built from them, and `cost report`."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from typer.testing import CliRunner

from jwall_engineering_sites import cli, costs
from jwall_engineering_sites.errors import PreflightError, RunError
from tests.test_cli import _as_role

OWNER = "arn:aws:iam::650503560686:user/owner"
AUG, SEP, OCT = "2026-08-01", "2026-09-01", "2026-10-01"
START, END = date(2026, 8, 1), date(2026, 10, 31)


def _group(keys: list[str], amount: str) -> dict[str, Any]:
    return {"Keys": keys, "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}}}


def _month(start: str, groups: list[dict[str, Any]], estimated: bool = False) -> dict[str, Any]:
    return {"TimePeriod": {"Start": start}, "Groups": groups, "Estimated": estimated}


class FakeCE:
    """Answers GetCostAndUsage with canned responses, in order, and records every request.
    A response is the ResultsByTime list, or (list, token) for a page with more to come."""

    def __init__(self, *responses: Any, error: str | None = None) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.regions: list[str] = []
        self.error = error

    def get_cost_and_usage(self, **request: Any) -> dict[str, Any]:
        self.requests.append(request)
        if self.error:
            raise ClientError({"Error": {"Code": self.error, "Message": "no"}}, "GetCostAndUsage")
        page = self.responses.pop(0) if self.responses else []
        results, token = page if isinstance(page, tuple) else (page, None)
        response: dict[str, Any] = {"ResultsByTime": results}
        if token:
            response["NextPageToken"] = token
        return response


def _any(fake: object) -> Any:
    return fake


# --- the period ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("today", "months", "expected"),
    [
        (date(2026, 10, 30), 3, (date(2026, 8, 1), date(2026, 10, 31))),
        (date(2026, 10, 30), 1, (date(2026, 10, 1), date(2026, 10, 31))),
        (date(2026, 2, 1), 3, (date(2025, 12, 1), date(2026, 2, 2))),
        (date(2026, 12, 31), 13, (date(2025, 12, 1), date(2027, 1, 1))),
    ],
)
def test_the_period_covers_whole_months_through_today(
    today: date, months: int, expected: tuple[date, date]
) -> None:
    assert costs.period(today, months) == expected


@pytest.mark.parametrize("months", [0, 14])
def test_the_period_stays_inside_cost_explorers_history(months: int) -> None:
    with pytest.raises(ValueError, match="from 1 to 13"):
        costs.period(date(2026, 10, 30), months)


# --- the requests ----------------------------------------------------------------


def test_a_project_report_filters_on_the_project_and_groups_by_component_and_workflow() -> None:
    ce = FakeCE([])
    costs.project_table(_any(ce), "rusty-chess", START, END)
    assert ce.requests == [
        {
            "TimePeriod": {"Start": "2026-08-01", "End": "2026-10-31"},
            "Granularity": "MONTHLY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "TAG", "Key": "Component"}, {"Type": "TAG", "Key": "Workflow"}],
            "Filter": {
                "Tags": {"Key": "Project", "Values": ["rusty-chess"], "MatchOptions": ["EQUALS"]}
            },
        }
    ]


def test_the_account_report_groups_by_project_without_a_filter() -> None:
    ce = FakeCE([])
    costs.account_table(_any(ce), START, END)
    (request,) = ce.requests
    assert request["GroupBy"] == [{"Type": "TAG", "Key": "Project"}]
    assert "Filter" not in request


def test_the_untagged_report_takes_spend_with_no_project_tag() -> None:
    ce = FakeCE([])
    costs.untagged_table(_any(ce), START, END)
    (request,) = ce.requests
    assert request["Filter"] == {"Tags": {"Key": "Project", "MatchOptions": ["ABSENT"]}}
    assert request["GroupBy"] == [
        {"Type": "DIMENSION", "Key": "SERVICE"},
        {"Type": "DIMENSION", "Key": "USAGE_TYPE"},
    ]


def test_every_page_is_read() -> None:
    ce = FakeCE(
        ([_month(AUG, [_group(["Component$dataset", "Workflow$"], "1.00")])], "token-1"),
        [_month(SEP, [_group(["Component$dataset", "Workflow$"], "2.00")])],
    )
    table = costs.project_table(_any(ce), "rusty-chess", START, END)
    assert "NextPageToken" not in ce.requests[0]
    assert ce.requests[1]["NextPageToken"] == "token-1"
    assert len(ce.requests) == 2
    assert table.rows == {"dataset": {"2026-08": Decimal("1.00"), "2026-09": Decimal("2.00")}}


def test_access_denied_is_a_preflight_error_naming_the_permission() -> None:
    with pytest.raises(PreflightError, match="ce:GetCostAndUsage"):
        costs.account_table(_any(FakeCE(error="AccessDeniedException")), START, END)


def test_other_errors_are_run_errors() -> None:
    with pytest.raises(RunError, match="Cost Explorer request failed"):
        costs.account_table(_any(FakeCE(error="LimitExceededException")), START, END)


# --- the tables ----------------------------------------------------------------


def test_task_spend_is_split_by_workflow_and_untagged_components_are_named() -> None:
    ce = FakeCE(
        [
            _month(
                OCT,
                [
                    _group(["Component$task-runner", "Workflow$Train"], "14.60"),
                    _group(["Component$task-runner", "Workflow$Datagen"], "41.30"),
                    _group(["Component$task-runner", "Workflow$"], "0.40"),
                    _group(["Component$container-instance", "Workflow$"], "9.20"),
                    _group(["Component$", "Workflow$"], "0.50"),
                ],
                estimated=True,
            )
        ]
    )
    table = costs.project_table(_any(ce), "rusty-chess", START, END)
    assert table.months == ("2026-08", "2026-09", "2026-10")
    assert table.estimated == {"2026-10"}
    assert {label: row["2026-10"] for label, row in table.rows.items()} == {
        "task-runner: Train": Decimal("14.60"),
        "task-runner: Datagen": Decimal("41.30"),
        "task-runner": Decimal("0.40"),
        "container-instance": Decimal("9.20"),
        "(no component)": Decimal("0.50"),
    }


def test_spend_with_no_project_is_one_untagged_row() -> None:
    ce = FakeCE(
        [
            _month(AUG, [_group(["Project$"], "47.90")]),
            _month(OCT, [_group(["Project$"], "34.02"), _group(["Project$rusty-chess"], "4.07")]),
        ]
    )
    table = costs.account_table(_any(ce), START, END)
    assert table.rows == {
        "(untagged)": {"2026-08": Decimal("47.90"), "2026-10": Decimal("34.02")},
        "rusty-chess": {"2026-10": Decimal("4.07")},
    }


def test_untagged_rows_name_the_service_and_usage_type() -> None:
    ce = FakeCE(
        [_month(OCT, [_group(["Amazon Virtual Private Cloud", "USE2-PublicIPv4"], "3.64")])]
    )
    table = costs.untagged_table(_any(ce), START, END)
    assert table.rows == {
        "Amazon Virtual Private Cloud: USE2-PublicIPv4": {"2026-10": Decimal("3.64")}
    }


# --- rendering ----------------------------------------------------------------


def _table(rows: dict[str, dict[str, str]], estimated: set[str] | None = None) -> costs.Table:
    table = costs.Table(title="t", months=("2026-09", "2026-10"), estimated=estimated or set())
    for label, months in rows.items():
        for month, amount in months.items():
            table.add(label, month, Decimal(amount))
    return table


def _lines(text: str) -> list[list[str]]:
    return [line.split() for line in text.splitlines()]


def test_rows_sort_by_total_and_the_total_counts_every_row() -> None:
    text = costs.render(
        _table(
            {
                "small": {"2026-10": "1.00"},
                "big": {"2026-09": "10.00", "2026-10": "5.00"},
                "dust": {"2026-10": "0.004"},
            },
            estimated={"2026-10"},
        )
    )
    lines = _lines(text)
    assert lines[0] == ["t", "2026-09", "2026-10*"]
    assert lines[1] == ["big", "$10.00", "$5.00"]
    assert lines[2] == ["small", "—", "$1.00"]
    assert lines[4] == ["total", "$10.00", "$6.00"]  # the 0.004 is in it, rounded
    assert "1 row(s) under $0.01 a month not shown" in text
    assert "* estimated" in text


def test_columns_line_up() -> None:
    text = costs.render(_table({"a": {"2026-10": "1234.5"}, "bb": {"2026-09": "1"}}))
    lengths = {len(line) for line in text.splitlines()}
    assert len(lengths) == 1
    assert "$1,234.50" in text


def test_a_limit_sums_the_rest_into_one_row() -> None:
    rows = {f"r{i}": {"2026-10": str(10 - i)} for i in range(5)}
    lines = _lines(costs.render(_table(rows), limit=2))
    assert [line[0] for line in lines[1:4]] == ["r0", "r1", "(3"]
    assert lines[3] == ["(3", "more)", "—", "$21.00"]  # 8 + 7 + 6
    assert lines[5] == ["total", "$0.00", "$40.00"]


def test_no_notes_when_nothing_is_hidden_or_estimated() -> None:
    text = costs.render(_table({"a": {"2026-10": "1"}}))
    assert len(text.splitlines()) == 4


# --- the command ----------------------------------------------------------------


@pytest.fixture
def ce(monkeypatch: pytest.MonkeyPatch, bot_workspace: Path) -> FakeCE:
    fake = FakeCE(
        [_month(OCT, [_group(["Project$"], "34.02"), _group(["Project$rusty-chess"], "4.07")])],
        [_month(OCT, [_group(["Tax", "USE2-BoxUsage"], "0.94")])],
    )

    def client(self: boto3.session.Session, name: str, *args: Any, **kwargs: Any) -> Any:
        # sts is only handed to the patched caller_identity; every AWS call is ce.
        if name == "ce":
            fake.regions.append(self.region_name)
        return fake

    monkeypatch.setattr(boto3.session.Session, "client", client)
    return fake


def test_cost_report_prints_the_account_and_its_untagged_spend(ce: FakeCE) -> None:
    with _as_role(OWNER):
        result = CliRunner().invoke(cli.app, ["cost", "report", "--months", "2"])
    assert result.exit_code == 0, result.output
    assert "account by project" in result.output and "rusty-chess" in result.output
    assert "(untagged) by service and usage type" in result.output
    assert "Tax: USE2-BoxUsage" in result.output
    assert len(ce.requests) == 2
    assert ce.regions == ["us-east-1"]


def test_cost_report_for_one_project(ce: FakeCE) -> None:
    ce.responses = [[_month(OCT, [_group(["Component$dataset", "Workflow$"], "0.16")])]]
    with _as_role(OWNER):
        result = CliRunner().invoke(cli.app, ["cost", "report", "--project", "rusty-chess"])
    assert result.exit_code == 0, result.output
    assert "rusty-chess by component" in result.output and "$0.16" in result.output
    (request,) = ce.requests
    assert request["Filter"]["Tags"]["Values"] == ["rusty-chess"]


@pytest.mark.parametrize("args", [["--project", "Rusty Chess"], ["--months", "14"]])
def test_cost_report_rejects_bad_options(ce: FakeCE, args: list[str]) -> None:
    with _as_role(OWNER):
        result = CliRunner().invoke(cli.app, ["cost", "report", *args])
    assert result.exit_code == 2
    assert ce.requests == []


def test_cost_report_needs_no_ci_role(ce: FakeCE) -> None:
    """Any credentials that can read Cost Explorer will do: the owner's own."""
    ce.responses = []
    with patch("jwall_engineering_sites.aws.caller_identity", return_value=("1", OWNER)):
        result = CliRunner().invoke(cli.app, ["cost", "report", "--project", "rusty-chess"])
    assert result.exit_code == 0, result.output
