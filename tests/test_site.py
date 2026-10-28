from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from jwall_engineering_sites.errors import ConfigError
from jwall_engineering_sites.settings import Settings
from jwall_engineering_sites.site import REQUIRED_TAGS, Site, SiteConfig, load_site_config
from tests.conftest import FIXTURES

GUITAR = FIXTURES / "sites" / "guitar-knowledge-base.yaml"


def _raw(mutate: dict[str, Any] | None = None, drop: tuple[str, ...] = ()) -> dict[str, Any]:
    raw: dict[str, Any] = yaml.safe_load(GUITAR.read_text())
    for dotted, value in (mutate or {}).items():
        target = raw
        *parents, key = dotted.split(".")
        for part in parents:
            target = target.setdefault(part, {})
        target[key] = value
    for dotted in drop:
        target = raw
        *parents, key = dotted.split(".")
        for part in parents:
            target = target[part]
        del target[key]
    return raw


def _write(tmp_path: Path, raw: dict[str, Any]) -> Path:
    path = tmp_path / "jwall-engineering-sites.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


# -- the three real sites ---------------------------------------------------------


def test_every_fixture_loads(site: Site) -> None:
    assert site.bucket == f"jwall-engineering-{site.project}"
    assert site.state_key == f"project={site.project}/env=prod/{site.project}-prod.tfstate"
    assert site.role_name("plan") == f"jwall-engineering-{site.project}-ci-plan"
    assert site.role_name("preview") == f"jwall-engineering-{site.project}-ci-preview"
    assert site.role_name("apply") == f"jwall-engineering-{site.project}-ci-apply"
    assert site.prod_url == f"https://{site.config.domain}/"
    assert site.preview_url(7) == f"https://pr-7.preview.{site.config.domain}/"
    assert site.preview_prefix(7) == "previews/pr-7/"
    assert site.dev_url is None


def test_every_site_fixture_still_loads_as_a_site(site: Site) -> None:
    assert site.config.kind == "site"
    assert site.config.components == ()
    assert site.config.site is not None


def test_main_site_keeps_extra_tags_and_immutable_paths(settings: Settings) -> None:
    config = load_site_config(FIXTURES / "sites" / "main-site.yaml")
    assert config.tags["CostCenter"] == "personal"
    assert config.site is not None
    assert config.site.immutable_paths == ("_astro/**",)
    assert config.site.output_dir == "dist"


# -- validation rules -----------------------------------------------------------


@pytest.mark.parametrize("tag", REQUIRED_TAGS)
def test_each_required_tag_is_required(tmp_path: Path, tag: str) -> None:
    with pytest.raises(ConfigError, match=f"missing the required keys {tag}"):
        load_site_config(_write(tmp_path, _raw(drop=(f"tags.{tag}",))))


@pytest.mark.parametrize(
    ("tag", "value"),
    [
        ("Project", "other"),
        ("Repository", "jwallace145/other"),
        ("Environment", "dev"),
        ("ManagedBy", "hand"),
    ],
)
def test_cross_checked_tags(tmp_path: Path, tag: str, value: str) -> None:
    with pytest.raises(ConfigError, match=f"{tag} must be"):
        load_site_config(_write(tmp_path, _raw({f"tags.{tag}": value})))


def test_empty_tag_value(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="empty values for Owner"):
        load_site_config(_write(tmp_path, _raw({"tags.Owner": "  "})))


def test_organization_tag_must_match_config(tmp_path: Path, settings: Settings) -> None:
    config = load_site_config(_write(tmp_path, _raw({"tags.Organization": "someone-else"})))
    with pytest.raises(ConfigError, match=r"tags\.Organization must be 'jwall-engineering'"):
        Site.from_config(config, settings)


def test_domain_must_be_under_the_zone(tmp_path: Path, settings: Settings) -> None:
    config = load_site_config(_write(tmp_path, _raw({"domain": "guitar.example.com"})))
    with pytest.raises(ConfigError, match="not under the zone"):
        Site.from_config(config, settings)


def test_apex_domain_is_under_the_zone(settings: Settings) -> None:
    config = load_site_config(FIXTURES / "sites" / "main-site.yaml")
    assert Site.from_config(config, settings).config.domain == "jwall.engineering"


def test_dev_domain(tmp_path: Path, settings: Settings) -> None:
    config = load_site_config(
        _write(tmp_path, _raw({"dev": {"domain": "dev.guitar.jwall.engineering"}}))
    )
    site = Site.from_config(config, settings)
    assert site.dev_url == "https://dev.guitar.jwall.engineering/"


def test_dev_domain_must_differ(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"dev\.domain must differ"):
        load_site_config(_write(tmp_path, _raw({"dev": {"domain": "guitar.jwall.engineering"}})))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("version", 2, "version"),
        ("project", "Guitar", "project"),
        ("project", "a" * 31, "project"),
        ("repository", "jwallace145", "repository"),
        ("domain", "Guitar.jwall.engineering", "domain"),
        ("site.output_dir", "../site", "output_dir"),
        ("site.output_dir", "/site", "output_dir"),
        ("site.immutable_paths", ["/abs/**"], "immutable_paths"),
        ("extra", "nope", "extra"),
    ],
)
def test_field_rules(tmp_path: Path, field: str, value: Any, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        load_site_config(_write(tmp_path, _raw({field: value})))


def test_errors_name_the_file(tmp_path: Path) -> None:
    path = _write(tmp_path, _raw({"version": 2}))
    with pytest.raises(ConfigError, match=str(path)):
        load_site_config(path)


def test_missing_site_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="site file not found"):
        load_site_config(tmp_path / "missing.yaml")


def test_model_is_frozen(guitar: Site) -> None:
    with pytest.raises(Exception, match="frozen"):
        guitar.config.project = "x"


def test_site_config_defaults() -> None:
    config = SiteConfig.model_validate(_raw(drop=("preview",)))
    assert config.preview.enabled is True
    assert config.site is not None
    assert config.site.immutable_paths == ()
