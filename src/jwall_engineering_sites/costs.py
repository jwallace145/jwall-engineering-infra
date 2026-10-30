"""Monthly spend from Cost Explorer, by the tags every managed resource carries.

`cost report` asks Cost Explorer for the account's spend grouped by the
cost allocation tags (`Project`, `Component`, `Workflow`), so the bill comes
back already aggregated: no Cost and Usage Report to parse. Spend with no
`Project` tag (resources this tool does not manage, public IPv4, tax) is
reported as untagged rather than dropped. Each Cost Explorer request costs
$0.01; a report makes one or two.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

from .errors import PreflightError, RunError

if TYPE_CHECKING:
    from mypy_boto3_ce import CostExplorerClient

#: Cost Explorer has one endpoint, in us-east-1.
CE_REGION = "us-east-1"
#: What the bill says, before credits are spread or reservations amortized.
METRIC = "UnblendedCost"
#: How many months a report may cover: the current one and twelve before it,
#: inside Cost Explorer's default history.
MAX_MONTHS = 13
PROJECT_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
UNTAGGED = "(untagged)"
#: The untagged table's rows before the rest are summed into one.
UNTAGGED_ROWS = 15
NO_COMPONENT = "(no component)"
#: Rows under a cent in every month are left out of the table (still in the total).
_CENT = Decimal("0.01")


@dataclass
class Table:
    """Spend by row and month; `estimated` months are not final yet."""

    title: str
    months: tuple[str, ...]
    rows: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    estimated: set[str] = field(default_factory=set)

    def add(self, label: str, month: str, amount: Decimal) -> None:
        row = self.rows.setdefault(label, {})
        row[month] = row.get(month, Decimal(0)) + amount


def period(today: date, months: int) -> tuple[date, date]:
    """[start, end) covering `months` calendar months up to and including today."""
    if not 1 <= months <= MAX_MONTHS:
        raise ValueError(f"months must be from 1 to {MAX_MONTHS}")
    year, month = today.year, today.month - (months - 1)
    while month < 1:
        year, month = year - 1, month + 12
    return date(year, month, 1), today + timedelta(days=1)


def _month_keys(start: date, end: date) -> tuple[str, ...]:
    keys: list[str] = []
    year, month = start.year, start.month
    while date(year, month, 1) < end:
        keys.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return tuple(keys)


def _results(ce: CostExplorerClient, request: dict[str, Any]) -> Iterator[Any]:
    """Every ResultsByTime entry of a GetCostAndUsage request, across pages."""
    token: str | None = None
    while True:
        page_request = dict(request, NextPageToken=token) if token else request
        try:
            response = ce.get_cost_and_usage(**page_request)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("AccessDeniedException", "AccessDenied"):
                raise PreflightError(
                    "these credentials may not read Cost Explorer (ce:GetCostAndUsage); "
                    "run cost report with the owner's credentials"
                ) from exc
            raise RunError(f"Cost Explorer request failed: {exc}") from exc
        yield from response["ResultsByTime"]
        token = response.get("NextPageToken")
        if not token:
            return


def _tag_value(key: str) -> str:
    """A tag group key is `Tag$value`; the value is empty when the tag is absent."""
    return key.split("$", 1)[1] if "$" in key else key


def _request(
    start: date, end: date, group_by: list[dict[str, str]], **extra: Any
) -> dict[str, Any]:
    return {
        "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
        "Granularity": "MONTHLY",
        "Metrics": [METRIC],
        "GroupBy": group_by,
        **extra,
    }


def _fill(table: Table, results: Iterator[Any], label: Any) -> Table:
    for result in results:
        month = result["TimePeriod"]["Start"][:7]
        if result.get("Estimated"):
            table.estimated.add(month)
        for group in result.get("Groups", []):
            amount = Decimal(group["Metrics"][METRIC]["Amount"])
            table.add(label(group["Keys"]), month, amount)
    return table


def project_table(ce: CostExplorerClient, project: str, start: date, end: date) -> Table:
    """One project's spend by component; task-runner spend split by its `Workflow` tag
    (Train, Datagen, SPRT, ...), so what the tasks were for shows, not only where they ran."""

    def label(keys: list[str]) -> str:
        component = _tag_value(keys[0]) or NO_COMPONENT
        workflow = _tag_value(keys[1])
        return f"{component}: {workflow}" if workflow else component

    request = _request(
        start,
        end,
        [{"Type": "TAG", "Key": "Component"}, {"Type": "TAG", "Key": "Workflow"}],
        Filter={"Tags": {"Key": "Project", "Values": [project], "MatchOptions": ["EQUALS"]}},
    )
    table = Table(title=f"{project} by component", months=_month_keys(start, end))
    return _fill(table, _results(ce, request), label)


def account_table(ce: CostExplorerClient, start: date, end: date) -> Table:
    """The account's spend by project; spend with no `Project` tag is one untagged row."""
    request = _request(start, end, [{"Type": "TAG", "Key": "Project"}])
    table = Table(title="account by project", months=_month_keys(start, end))
    return _fill(table, _results(ce, request), lambda keys: _tag_value(keys[0]) or UNTAGGED)


def untagged_table(ce: CostExplorerClient, start: date, end: date) -> Table:
    """The untagged spend by service and usage type: what it is, though not whose."""
    request = _request(
        start,
        end,
        [{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        Filter={"Tags": {"Key": "Project", "MatchOptions": ["ABSENT"]}},
    )
    table = Table(title=f"{UNTAGGED} by service and usage type", months=_month_keys(start, end))
    return _fill(table, _results(ce, request), lambda keys: f"{keys[0]}: {keys[1]}")


def _money(amount: Decimal) -> str:
    rounded = amount.quantize(_CENT)
    return "—" if rounded == 0 else f"${rounded:,}"


def render(table: Table, limit: int | None = None) -> str:
    """The table as aligned text: rows by total spend (past `limit` rows, the
    rest summed in one), a total row, and a note on rows under a cent and
    months still estimated."""
    months = table.months

    def total(row: dict[str, Decimal]) -> Decimal:
        return sum((row.get(m, Decimal(0)) for m in months), Decimal(0))

    shown = {
        label: row
        for label, row in table.rows.items()
        if any(row.get(m, Decimal(0)).quantize(_CENT) != 0 for m in months)
    }
    hidden = len(table.rows) - len(shown)
    order = sorted(shown, key=lambda label: (-total(shown[label]), label))
    totals = {
        m: sum((r.get(m, Decimal(0)) for r in table.rows.values()), Decimal(0)) for m in months
    }
    headers = [m + ("*" if m in table.estimated else "") for m in months]

    if limit is not None and len(order) > limit:
        rest = order[limit:]
        order = order[:limit]
        merged = f"({len(rest)} more)"
        shown[merged] = {
            m: sum((shown[label].get(m, Decimal(0)) for label in rest), Decimal(0)) for m in months
        }
        order.append(merged)
    cells = [[label, *(_money(shown[label].get(m, Decimal(0))) for m in months)] for label in order]
    cells.append(["total", *(f"${totals[m].quantize(_CENT):,}" for m in months)])
    label_width = max(len(table.title), *(len(row[0]) for row in cells))
    widths = [max(len(h), *(len(row[i + 1]) for row in cells)) for i, h in enumerate(headers)]

    def line(first: str, rest: list[str]) -> str:
        return "  ".join(
            [first.ljust(label_width), *(v.rjust(w) for v, w in zip(rest, widths, strict=True))]
        )

    out = [line(table.title, headers)]
    out += [line(row[0], row[1:]) for row in cells[:-1]]
    out.append(line("-" * label_width, ["-" * w for w in widths]))
    out.append(line(cells[-1][0], cells[-1][1:]))
    notes = []
    if hidden:
        notes.append(f"{hidden} row(s) under $0.01 a month not shown (in the total)")
    if table.estimated:
        notes.append("* estimated: the month is not closed yet")
    return "\n".join(out + [f"  {n}" for n in notes])
