"""Render the tfvars the stack plan tests run against, through the CLI's own code.

Usage: python -m tests.stack_plans.render OUT_DIR

Writes OUT_DIR/<stack>/<case>.tfvars.json, one directory per stack under stacks/,
each case matching tests/stack_plans/<stack>/<case>.tftest.hcl.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

from jwall_engineering_sites.project import Project
from jwall_engineering_sites.settings import Settings, load_settings
from jwall_engineering_sites.site import SiteConfig
from jwall_engineering_sites.stack import render_tfvars
from tests.conftest import FIXTURES, MATH_PROVER_GUARDRAILS, REPO_ROOT, with_guardrails

#: What the account stack's outputs look like after an apply (`terraform output -json`),
#: for the workspace stack's plan. The tftest file overrides the metastore lookup
#: with the same metastore ID.
ACCOUNT_OUTPUTS: dict[str, Any] = {
    "workspace_id": 1234567890123456,
    "workspace_url": "https://dbc-01234567-89ab.cloud.databricks.com",
    "admin_group_name": "databricks-poc-workspace-admins",
    "users_group_name": "databricks-poc-workspace-users",
    "metastore_id": "abcdef01-2345-6789-abcd-ef0123456789",
    "root_bucket_name": "jwall-engineering-databricks-poc-databricks-root",
}


def _file(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((FIXTURES / "bots" / f"{name}.yaml").read_text())
    return raw


def _dataset_only() -> dict[str, Any]:
    raw = _file("math-prover")
    raw["components"] = [c for c in raw["components"] if c["kind"] == "dataset"]
    return raw


def _project(raw: dict[str, Any], settings: Settings) -> Project:
    """`Project.from_config` runs the guardrails."""
    return Project.from_config(SiteConfig.model_validate(raw), settings)


def cases(settings: Settings) -> dict[str, dict[str, dict[str, Any]]]:
    """Each test file's inputs: by stack (its directory under stacks/), then by case name."""
    research = with_guardrails(settings, "math-prover", MATH_PROVER_GUARDRAILS)
    bots = {
        "rusty_chess": (_file("rusty-chess"), settings),
        "math_prover": (_file("math-prover"), research),
        "dataset_only": (_dataset_only(), research),
    }
    databricks = _project(_file("databricks-poc"), settings)
    return {
        "bot": {name: render_tfvars(_project(raw, s), s) for name, (raw, s) in bots.items()},
        "databricks-account": {
            "databricks_poc": render_tfvars(databricks, settings, stack="account"),
        },
        "databricks-workspace": {
            "databricks_poc": render_tfvars(
                databricks, settings, stack="workspace", account_outputs=ACCOUNT_OUTPUTS
            ),
        },
    }


def main(out: Path) -> None:
    settings = load_settings(REPO_ROOT / "config.yaml", environ={})
    for stack, stack_cases in cases(settings).items():
        (out / stack).mkdir(parents=True, exist_ok=True)
        for name, tfvars in stack_cases.items():
            (out / stack / f"{name}.tfvars.json").write_text(json.dumps(tfvars, indent=2) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
