"""Render the tfvars the stack plan tests run against, through the CLI's own code.

Usage: python -m tests.stack_plans.render OUT_DIR
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


def _file(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load((FIXTURES / "bots" / f"{name}.yaml").read_text())
    return raw


def _dataset_only() -> dict[str, Any]:
    raw = _file("math-prover")
    raw["components"] = [c for c in raw["components"] if c["kind"] == "dataset"]
    return raw


def cases(settings: Settings) -> dict[str, dict[str, Any]]:
    """Each test file's inputs, by its name. `Project.from_config` runs the guardrails."""
    research = with_guardrails(settings, "math-prover", MATH_PROVER_GUARDRAILS)
    projects = {
        "rusty_chess": (_file("rusty-chess"), settings),
        "math_prover": (_file("math-prover"), research),
        "dataset_only": (_dataset_only(), research),
    }
    return {
        name: render_tfvars(Project.from_config(SiteConfig.model_validate(raw), s), s)
        for name, (raw, s) in projects.items()
    }


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    settings = load_settings(REPO_ROOT / "config.yaml", environ={})
    for name, tfvars in cases(settings).items():
        (out / f"{name}.tfvars.json").write_text(json.dumps(tfvars, indent=2) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
