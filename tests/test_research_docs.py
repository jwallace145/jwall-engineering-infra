"""docs/research-projects.md shows the sample the tests load, not a drifted copy."""

from __future__ import annotations

import re
from typing import Any

import yaml

from jwall_engineering_sites.settings import ProjectGuardrails
from tests.conftest import FIXTURES, MATH_PROVER_GUARDRAILS, REPO_ROOT


def _yaml_blocks() -> list[Any]:
    text = (REPO_ROOT / "docs" / "research-projects.md").read_text()
    return [yaml.safe_load(b) for b in re.findall(r"^```yaml\n(.*?)^```$", text, re.M | re.S)]


def test_the_docs_sample_is_the_fixture() -> None:
    fixture = yaml.safe_load((FIXTURES / "bots" / "math-prover.yaml").read_text())
    assert [b for b in _yaml_blocks() if b.get("project") == "math-prover"] == [fixture]


def test_the_docs_central_entry_is_the_one_the_tests_approve() -> None:
    entries = [
        b["projects"]["math-prover"]
        for b in _yaml_blocks()
        if "math-prover" in b.get("projects", {})
    ]
    assert [ProjectGuardrails.model_validate(e) for e in entries] == [MATH_PROVER_GUARDRAILS]
