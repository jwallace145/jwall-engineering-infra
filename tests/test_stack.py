from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from jwall_engineering_sites.errors import RunError
from jwall_engineering_sites.settings import Settings
from jwall_engineering_sites.site import Site
from jwall_engineering_sites.stack import (
    PLAN_CHANGES,
    PLAN_NO_CHANGES,
    Terraform,
    render_backend,
    render_tfvars,
)
from tests.conftest import REPO_ROOT

#: What each site's infra/ directory produced before the migration, by project.
#: Step 6's "no changes" plan depends on these staying identical.
EXPECTED_TFVARS = {
    "guitar-knowledge-base": {
        "domain_name": "guitar.jwall.engineering",
        "dev_domain_name": None,
        "enable_preview": True,
        "zone_id": "Z029546536VUFGDCPXZ21",
    },
    "cs-survival-guide": {
        "domain_name": "survival-guide.jwall.engineering",
        "dev_domain_name": None,
        "enable_preview": True,
        "zone_id": "Z029546536VUFGDCPXZ21",
    },
    "main-site": {
        "domain_name": "jwall.engineering",
        "dev_domain_name": None,
        "enable_preview": True,
        "zone_id": "Z029546536VUFGDCPXZ21",
    },
}


def test_rendered_tfvars_match_the_old_infra_directories(site: Site) -> None:
    tfvars = render_tfvars(site)
    expected = EXPECTED_TFVARS[site.project]
    assert {key: tfvars[key] for key in expected} == expected
    assert tfvars["project"] == site.project
    assert tfvars["repository"] == site.config.repository
    assert tfvars["tags"]["Project"] == site.project
    assert tfvars["tags"]["Organization"] == "jwall-engineering"


def test_rendered_backend_matches_the_old_backend_files(site: Site) -> None:
    assert render_backend(site) == (
        'bucket       = "jwall-engineering-tfstate"\n'
        f'key          = "project={site.project}/env=prod/{site.project}-prod.tfstate"\n'
        'region       = "us-east-1"\n'
        "use_lockfile = true\n"
        "encrypt      = true\n"
    )


def test_tfvars_are_valid_for_the_stack_variables(site: Site) -> None:
    """Every variable the stack declares is rendered, and nothing else."""
    declared = {
        line.split('"')[1]
        for line in (REPO_ROOT / "stacks" / "site" / "variables.tf").read_text().splitlines()
        if line.startswith('variable "')
    }
    assert set(render_tfvars(site)) == declared


def _terraform(site: Site, settings: Settings, logger: logging.Logger) -> Terraform:
    return Terraform(
        site, settings, logger, stack_dir=REPO_ROOT / "stacks" / "site", plugin_dir=None
    )


def test_prepare_writes_inputs_into_a_copy(
    guitar: Site, settings: Settings, logger: logging.Logger
) -> None:
    terraform = _terraform(guitar, settings, logger)
    workdir = terraform.prepare()
    try:
        assert (workdir / "main.tf").is_file()
        assert (
            json.loads((workdir / "terraform.tfvars.json").read_text())["project"]
            == "guitar-knowledge-base"
        )
        assert (workdir / "backend.hcl").read_text() == render_backend(guitar)
        assert not (REPO_ROOT / "stacks" / "site" / "terraform.tfvars.json").exists()
    finally:
        terraform.cleanup(failed=False)
    assert not workdir.exists()


def test_version_mismatch_fails_fast(
    guitar: Site,
    settings: Settings,
    logger: logging.Logger,
    fake_terraform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_TF_VERSION", "1.9.0")
    terraform = _terraform(guitar, settings, logger)
    with pytest.raises(RunError, match=r"config\.yaml pins 1\.14\.7"):
        terraform.prepare()


def test_init_plan_and_outputs_invoke_terraform(
    guitar: Site,
    settings: Settings,
    logger: logging.Logger,
    fake_terraform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_TF_PLAN_EXIT", str(PLAN_CHANGES))
    terraform = _terraform(guitar, settings, logger)
    terraform.prepare()
    try:
        terraform.init()
        result = terraform.plan(detailed_exitcode=True)
        assert result.returncode == PLAN_CHANGES
        assert "Plan:" in result.output
        outputs = terraform.outputs()
        assert outputs["prod_distribution_id"] == "E1PROD"
    finally:
        terraform.cleanup(failed=False)
    calls = [c for c in fake_terraform.read_text().splitlines() if not c.startswith("version")]
    assert calls[0] == "init -input=false -no-color -backend-config=backend.hcl -reconfigure"
    assert (
        calls[1] == "plan -input=false -no-color "
        f"-lock-timeout={settings.terraform.lock_timeout} -detailed-exitcode"
    )
    assert calls[2] == "output -no-color -json"


def test_offline_init_uses_plugin_dir_and_no_backend(
    guitar: Site, settings: Settings, logger: logging.Logger, fake_terraform: Path, tmp_path: Path
) -> None:
    terraform = Terraform(
        guitar, settings, logger, stack_dir=REPO_ROOT / "stacks" / "site", plugin_dir=tmp_path
    )
    terraform.prepare()
    try:
        terraform.init(backend=False)
        terraform.validate()
    finally:
        terraform.cleanup(failed=False)
    calls = [c for c in fake_terraform.read_text().splitlines() if not c.startswith("version")]
    assert calls[0] == f"init -input=false -no-color -plugin-dir={tmp_path} -backend=false"
    assert calls[1] == "validate -no-color"


def test_plan_without_detailed_exitcode_treats_changes_as_success(
    guitar: Site,
    settings: Settings,
    logger: logging.Logger,
    fake_terraform: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_TF_PLAN_EXIT", str(PLAN_NO_CHANGES))
    terraform = _terraform(guitar, settings, logger)
    terraform.prepare()
    try:
        result = terraform.plan(out=Path("tfplan"))
    finally:
        terraform.cleanup(failed=False)
    assert result.returncode == 0
    assert fake_terraform.read_text().splitlines()[-1].endswith("-out=tfplan")


def test_lock_info_is_reported(
    guitar: Site,
    settings: Settings,
    logger: logging.Logger,
    fake_terraform: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("FAKE_TF_LOCKED", "1")
    terraform = _terraform(guitar, settings, logger)
    terraform.prepare()
    try:
        with (
            caplog.at_level(logging.ERROR, logger=logger.name),
            pytest.raises(RunError, match="plan failed"),
        ):
            terraform.plan(detailed_exitcode=True)
    finally:
        terraform.cleanup(failed=False)
    assert "the state is locked" in caplog.text
    assert "someone@host" in caplog.text


def test_missing_binary_is_a_run_error(
    guitar: Site, settings: Settings, logger: logging.Logger
) -> None:
    terraform = Terraform(
        guitar,
        settings,
        logger,
        stack_dir=REPO_ROOT / "stacks" / "site",
        plugin_dir=None,
        binary="no-such-terraform",
    )
    with pytest.raises(RunError, match="not found on PATH"):
        terraform.prepare()


def test_missing_stack_is_a_run_error(
    guitar: Site, settings: Settings, logger: logging.Logger, tmp_path: Path
) -> None:
    terraform = Terraform(guitar, settings, logger, stack_dir=tmp_path / "nope", plugin_dir=None)
    with pytest.raises(RunError, match="site stack not found"):
        terraform.prepare()


def test_failed_run_keeps_workdir_when_asked(
    guitar: Site, settings: Settings, logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JES_KEEP_WORKDIR", "1")
    terraform = _terraform(guitar, settings, logger)
    workdir = terraform.prepare()
    terraform.cleanup(failed=True)
    assert workdir.exists()
    terraform.cleanup(failed=False)
