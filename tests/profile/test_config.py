import re
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tokenmaxxing.profile.config import (
    DeployConfig,
    MetricsConfig,
    ProfileConfig,
    ProfileInfo,
    ProfileLink,
    ScheduleConfig,
    SiteConfig,
    discover_config,
    load_config,
    write_initial_config,
)
from tokenmaxxing.profile.data import build_profile_data
from tokenmaxxing.profile.project import profile_paths
from tokenmaxxing.profile.render import render_site


def _write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config_returns_immutable_typed_values(profile_config_path: Path) -> None:
    config = load_config(profile_config_path)

    assert config == ProfileConfig(
        version=1,
        profile=ProfileInfo(
            name="Ada Lovelace",
            bio="Programmer",
            avatar=(profile_config_path.parent / "avatar.webp").resolve(),
        ),
        site=SiteConfig(
            title="Ada's token trail",
            description="Aggregate local AI agent usage.",
            canonical_url="https://example.com/tokens/",
            indexable=True,
            timezone=ZoneInfo("UTC"),
        ),
        metrics=MetricsConfig(),
        deploy=DeployConfig(command=("fake-deploy", "{site_dir}")),
        schedule=ScheduleConfig(time=time(hour=9)),
    )
    with pytest.raises(FrozenInstanceError):
        config.profile.name = "Grace Hopper"  # type: ignore[misc]


def test_profile_schema_rejects_removed_role_field(
    tmp_path: Path, minimal_config: str
) -> None:
    path = _write_config(
        tmp_path,
        minimal_config.replace("  name: Ada Lovelace", "  name: Ada Lovelace\n  role: Programmer"),
    )

    with pytest.raises(ValueError, match=r"unknown configuration key: profile\.role"):
        load_config(path)


def test_canonical_url_normalizes_a_subpath_for_assets_and_sitemap(
    tmp_path: Path, minimal_config: str
) -> None:
    config_path = _write_config(
        tmp_path,
        minimal_config.replace(
            "https://example.com/tokens/", "https://example.com/tokens"
        ),
    )
    (tmp_path / "avatar.webp").write_bytes(b"avatar")
    config = load_config(config_path)
    destination = tmp_path / "site"
    data = build_profile_data(
        (),
        timezone=config.site.timezone,
        now=datetime(2026, 8, 30, 12, tzinfo=config.site.timezone),
        window_days=config.metrics.window_days,
    )

    render_site(
        replace(config, site=replace(config.site, indexable=True)),
        data,
        profile_paths(config_path),
        destination,
        noindex=False,
    )

    html = (destination / "index.html").read_text(encoding="utf-8")
    assert config.site.canonical_url == "https://example.com/tokens/"
    assert re.search(r'href="assets/profile\.css\?v=\d+"', html)
    assert (
        destination / "robots.txt"
    ).read_text(encoding="utf-8") == (
        "User-agent: *\nAllow: /\n"
        "Sitemap: https://example.com/tokens/sitemap.xml\n"
    )


@pytest.mark.parametrize(
    "canonical_url",
    [
        "http://example.com/tokens/",
        "https://user@example.com/tokens/",
        "https://example.com/tokens/?preview=true",
        "https://example.com/tokens/#usage",
        "/tokens/",
    ],
)
def test_load_config_rejects_unsafe_canonical_urls(
    tmp_path: Path, minimal_config: str, canonical_url: str
) -> None:
    path = _write_config(
        tmp_path,
        minimal_config.replace("https://example.com/tokens/", canonical_url),
    )

    with pytest.raises(ValueError, match=r"site\.canonical_url"):
        load_config(path)


def test_load_config_rejects_unknown_keys(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        "version: 1\nprofile:\n  name: Ada\n  typo: true\n",
    )

    with pytest.raises(ValueError, match=r"profile\.typo"):
        load_config(path)


@pytest.mark.parametrize("version", ["2", "1.0", '"1"', "true"])
def test_load_config_requires_version_one(
    tmp_path: Path, minimal_config: str, version: str
) -> None:
    path = _write_config(
        tmp_path,
        minimal_config.replace("version: 1", f"version: {version}"),
    )

    with pytest.raises(ValueError, match="version"):
        load_config(path)


@pytest.mark.parametrize("scheme", ["https://example.com", "mailto:ada@example.com"])
def test_load_config_accepts_public_link_schemes(
    tmp_path: Path, minimal_config: str, scheme: str
) -> None:
    links = f"links:\n    - label: Contact\n      value: Ada\n      url: {scheme}"
    path = _write_config(tmp_path, minimal_config.replace("links: []", links))

    config = load_config(path)

    assert config.profile.links == (ProfileLink("Contact", "Ada", scheme),)


@pytest.mark.parametrize(
    "url",
    ["http://example.com", "javascript:alert(1)", "/relative/path"],
)
def test_load_config_rejects_non_public_link_schemes(
    tmp_path: Path, minimal_config: str, url: str
) -> None:
    links = f"links:\n    - label: Contact\n      value: Ada\n      url: {url}"
    path = _write_config(tmp_path, minimal_config.replace("links: []", links))

    with pytest.raises(ValueError, match=r"profile\.links\[0\]\.url"):
        load_config(path)


def test_load_config_rejects_unknown_link_keys(
    tmp_path: Path, minimal_config: str
) -> None:
    links = "links:\n    - label: Contact\n      value: Ada\n      url: https://example.com\n      typo: true"
    path = _write_config(tmp_path, minimal_config.replace("links: []", links))

    with pytest.raises(ValueError, match=r"profile\.links\[0\]\.typo"):
        load_config(path)


def test_load_config_rejects_an_unknown_timezone(
    tmp_path: Path, minimal_config: str
) -> None:
    path = _write_config(
        tmp_path,
        minimal_config.replace("timezone: UTC", "timezone: Mars/Olympus_Mons"),
    )

    with pytest.raises(ValueError, match=r"site\.timezone"):
        load_config(path)


@pytest.mark.parametrize("theme", ["sepia", "AUTO", ""])
def test_load_config_rejects_unknown_themes(
    tmp_path: Path, minimal_config: str, theme: str
) -> None:
    path = _write_config(
        tmp_path,
        minimal_config.replace("theme: auto", f'theme: "{theme}"'),
    )

    with pytest.raises(ValueError, match=r"site\.theme"):
        load_config(path)


@pytest.mark.parametrize("window_days", ["0", "-1", "true", '"28"'])
def test_load_config_requires_positive_integer_window_days(
    tmp_path: Path, minimal_config: str, window_days: str
) -> None:
    path = _write_config(
        tmp_path,
        minimal_config.replace("window_days: 28", f"window_days: {window_days}"),
    )

    with pytest.raises(ValueError, match=r"metrics\.window_days"):
        load_config(path)


@pytest.mark.parametrize(
    ("existing", "replacement", "error_path"),
    [
        ("window_days: 28", "window_days: null", r"metrics\.window_days"),
        (
            "window_days: 28",
            "window_days: 28\n  show_models: null",
            r"metrics\.show_models",
        ),
        ("theme: auto", "theme: null", r"site\.theme"),
        ('time: "09:00"', "time: null", r"schedule\.time"),
        (
            'command: [fake-deploy, "{site_dir}"]',
            "command: null",
            r"deploy\.command",
        ),
        ("metrics:\n  window_days: 28", "metrics: null", "metrics"),
        (
            'deploy:\n  command: [fake-deploy, "{site_dir}"]',
            "deploy: null",
            "deploy",
        ),
        ('schedule:\n  time: "09:00"', "schedule: null", "schedule"),
    ],
)
def test_load_config_rejects_explicit_null_for_typed_values(
    tmp_path: Path,
    minimal_config: str,
    existing: str,
    replacement: str,
    error_path: str,
) -> None:
    path = _write_config(tmp_path, minimal_config.replace(existing, replacement))

    with pytest.raises(ValueError, match=error_path):
        load_config(path)


def test_load_config_accepts_explicit_null_for_optional_avatar(
    tmp_path: Path, minimal_config: str
) -> None:
    path = _write_config(
        tmp_path,
        minimal_config.replace("avatar: avatar.webp", "avatar: null"),
    )

    assert load_config(path).profile.avatar is None


@pytest.mark.parametrize("schedule_time", ["9:00", "09:0", "24:00", "09:60"])
def test_load_config_requires_zero_padded_twenty_four_hour_schedule_time(
    tmp_path: Path, minimal_config: str, schedule_time: str
) -> None:
    path = _write_config(
        tmp_path,
        minimal_config.replace('time: "09:00"', f'time: "{schedule_time}"'),
    )

    with pytest.raises(ValueError, match=r"schedule\.time"):
        load_config(path)


def test_load_config_rejects_empty_deploy_arguments(
    tmp_path: Path, minimal_config: str
) -> None:
    path = _write_config(
        tmp_path,
        minimal_config.replace(
            'command: [fake-deploy, "{site_dir}"]',
            'command: [fake-deploy, ""]',
        ),
    )

    with pytest.raises(ValueError, match=r"deploy\.command\[1\]"):
        load_config(path)


def test_load_config_accepts_an_empty_deploy_command(
    tmp_path: Path, minimal_config: str
) -> None:
    path = _write_config(
        tmp_path,
        minimal_config.replace(
            'command: [fake-deploy, "{site_dir}"]',
            "command: []",
        ),
    )

    assert load_config(path).deploy.command == ()


def test_load_config_rejects_unknown_deploy_placeholders(
    tmp_path: Path, minimal_config: str
) -> None:
    path = _write_config(
        tmp_path,
        minimal_config.replace("{site_dir}", "{output_dir}"),
    )

    with pytest.raises(ValueError, match=r"deploy\.command\[1\]"):
        load_config(path)


def test_load_config_rejects_a_scalar_deploy_command(
    tmp_path: Path, minimal_config: str
) -> None:
    path = _write_config(
        tmp_path,
        minimal_config.replace(
            'command: [fake-deploy, "{site_dir}"]',
            "command: fake-deploy",
        ),
    )

    with pytest.raises(ValueError, match=r"deploy\.command"):
        load_config(path)


def test_load_config_rejects_assets_outside_project(
    tmp_path: Path, minimal_config: str
) -> None:
    path = _write_config(
        tmp_path,
        minimal_config.replace("avatar: avatar.webp", "avatar: ../avatar.webp"),
    )

    with pytest.raises(ValueError, match="inside the profile project"):
        load_config(path)


def test_load_config_rejects_symlinks_escaping_project(
    tmp_path: Path, minimal_config: str
) -> None:
    outside = tmp_path / "outside.webp"
    outside.write_bytes(b"avatar")
    project = tmp_path / "project"
    project.mkdir()
    (project / "avatar.webp").symlink_to(outside)
    path = project / "config.yaml"
    path.write_text(minimal_config, encoding="utf-8")

    with pytest.raises(ValueError, match="inside the profile project"):
        load_config(path)


def test_discover_config_walks_parents(tmp_path: Path, minimal_config: str) -> None:
    config = _write_config(tmp_path, minimal_config)
    child = tmp_path / "one" / "two"
    child.mkdir(parents=True)

    assert discover_config(child) == config


def test_discover_config_reports_when_no_project_exists(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="config.yaml"):
        discover_config(tmp_path)


def test_discover_config_does_not_support_the_old_default_name(
    tmp_path: Path, minimal_config: str
) -> None:
    (tmp_path / "tokenmaxxing.yaml").write_text(minimal_config, encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="config.yaml"):
        discover_config(tmp_path)


def test_write_initial_config_round_trips_explicit_values(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    config = ProfileConfig(
        version=1,
        profile=ProfileInfo(
            name="Ada Lovelace",
            bio="First programmer.",
            links=(
                ProfileLink("Website", "example.com", "https://example.com"),
            ),
        ),
        site=SiteConfig(
            title="Ada's token trail",
            description="Aggregate local AI agent usage.",
            canonical_url="https://example.com/tokens/",
            indexable=False,
            timezone=ZoneInfo("Europe/London"),
            theme="dark",
            accent="indigo",
        ),
        metrics=MetricsConfig(window_days=7, show_models=False),
        deploy=DeployConfig(command=()),
        schedule=ScheduleConfig(time=time(hour=8, minute=5)),
    )

    write_initial_config(path, config)

    assert load_config(path) == config
    text = path.read_text(encoding="utf-8")
    assert text.startswith("version: 1\nprofile:\n")
    assert 'time: 08:05\n' in text
