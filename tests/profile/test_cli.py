import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tokenmaxxing.db import Database
from tokenmaxxing.profile import cli as profile_cli
from tokenmaxxing.profile.build import BuildResult
from tokenmaxxing.profile.deploy import DeployError, DeployResult
from tokenmaxxing.profile.deploy import DeployPlan
from tokenmaxxing.profile.project import profile_paths


def initialized_profile(tmp_path: Path, minimal_config: str) -> Path:
    config = tmp_path / "profile" / "tokenmaxxing.yaml"
    config.parent.mkdir()
    config.write_text(minimal_config, encoding="utf-8")
    (config.parent / "avatar.webp").write_bytes(b"avatar")
    database = Database.open(tmp_path / "usage.sqlite3")
    database.close()
    return config


def build_result(tmp_path: Path) -> BuildResult:
    site = tmp_path / ".tokenmaxxing" / "site"
    site.mkdir(parents=True, exist_ok=True)
    return BuildResult(site, datetime(2026, 8, 30, tzinfo=UTC), 4, 1024)


def deploy_result() -> DeployResult:
    return DeployResult(returncode=0, stdout="published\n", stderr="provider note\n")


def arguments(
    tmp_path: Path,
    config: Path,
    command: str,
    **overrides: object,
) -> argparse.Namespace:
    values: dict[str, object] = {
        "command": "profile",
        "profile_command": command,
        "config": config,
        "db": tmp_path / "usage.sqlite3",
        "debug": False,
        "json": False,
        "sync": False,
        "non_interactive": False,
        "output": None,
        "publish": False,
        "host": "127.0.0.1",
        "port": None,
        "no_open": False,
        "action": None,
        "directory": tmp_path / "new-profile",
        "no_setup": True,
        "editable_template": False,
        "force": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


@pytest.mark.parametrize(
    "command", ["init", "edit", "preview", "build", "publish", "status", "schedule"]
)
def test_profile_command_is_registered(command: str) -> None:
    from tokenmaxxing.cli import build_parser

    parsed = build_parser().parse_args(["profile", command])

    assert parsed.command == "profile"
    assert parsed.profile_command == command


def test_profile_config_is_an_explicit_parent_option() -> None:
    from tokenmaxxing.cli import build_parser

    parser = build_parser()
    parsed = parser.parse_args(["profile", "--config", "custom.yaml", "build"])
    assert parsed.config == Path("custom.yaml")
    with pytest.raises(SystemExit):
        parser.parse_args(["profile", "build", "--config", "custom.yaml"])


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (
            ["profile", "--help"],
            ("configuration file", "Create a profile project", "Example:"),
        ),
        (
            ["profile", "init", "--help"],
            ("default: ./tokenmaxxing-profile", "never overwrites", "Example:"),
        ),
        (
            ["profile", "edit", "--help"],
            ("configured editor", "Publish after", "Example:"),
        ),
        (
            ["profile", "preview", "--help"],
            ("noindex", "default: 127.0.0.1", "available local port", "Example:"),
        ),
        (
            ["profile", "build", "--help"],
            ("Build and validate", "default: .tokenmaxxing/site", "machine-readable", "Example:"),
        ),
        (
            ["profile", "publish", "--help"],
            ("Sync local histories", "current approval", "machine-readable", "Example:"),
        ),
        (
            ["profile", "status", "--help"],
            ("paths", "approval", "schedule", "machine-readable", "Example:"),
        ),
        (
            ["profile", "schedule", "--help"],
            ("daily publishing", "default: status", "Example:"),
        ),
    ],
)
def test_profile_help_explains_defaults_options_and_examples(
    argv: list[str], expected: tuple[str, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    from tokenmaxxing.cli import build_parser

    with pytest.raises(SystemExit) as exit_info:
        build_parser().parse_args(argv)

    assert exit_info.value.code == 0
    output = capsys.readouterr().out
    assert all(fragment in output for fragment in expected)


def test_init_rejects_config_and_bypasses_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        profile_cli,
        "discover_config",
        lambda start: pytest.fail("init must not discover a config"),
    )
    args = arguments(
        tmp_path,
        tmp_path / "other.yaml",
        "init",
        directory=tmp_path / "profile",
    )

    with pytest.raises(ValueError, match="--config.*init"):
        profile_cli.run_profile(args)


def test_non_init_commands_discover_from_cwd(
    tmp_path: Path,
    minimal_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = initialized_profile(tmp_path, minimal_config)
    seen: list[Path] = []
    monkeypatch.setattr(
        profile_cli,
        "discover_config",
        lambda start: seen.append(start) or config,
    )
    monkeypatch.setattr(
        profile_cli,
        "_status_payload",
        lambda config_path, db_path: {
            "approval": "unconfigured",
            "canonical_url": "https://example.com/tokens/",
            "config": str(config_path),
            "schedule": {"backend": "none", "enabled": False},
            "site": None,
        },
    )

    assert profile_cli.run_profile(arguments(tmp_path, None, "status", json=True)) == 0
    assert seen == [Path.cwd()]


def test_build_json_is_one_stable_document(
    tmp_path: Path,
    minimal_config: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = initialized_profile(tmp_path, minimal_config)
    monkeypatch.setattr(
        profile_cli,
        "build_profile",
        lambda *args, **kwargs: build_result(tmp_path),
    )

    assert profile_cli.run_profile(arguments(tmp_path, config, "build", json=True)) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out) == {
        "file_count": 4,
        "generated_at": "2026-08-30T00:00:00+00:00",
        "site_dir": str(tmp_path / ".tokenmaxxing" / "site"),
        "total_bytes": 1024,
    }


@pytest.mark.parametrize("sync", [False, True])
def test_publish_orders_optional_sync_build_and_deploy(
    tmp_path: Path,
    minimal_config: str,
    monkeypatch: pytest.MonkeyPatch,
    sync: bool,
) -> None:
    config = initialized_profile(tmp_path, minimal_config)
    order: list[str] = []
    monkeypatch.setattr(
        profile_cli,
        "_sync_profile_sources",
        lambda *args, **kwargs: order.append("sync"),
    )
    monkeypatch.setattr(
        profile_cli,
        "build_profile",
        lambda *args, **kwargs: order.append("build") or build_result(tmp_path),
    )
    monkeypatch.setattr(
        profile_cli,
        "make_deploy_plan",
        lambda *args, **kwargs: order.append("plan") or SimpleNamespace(
            argv=("deploy",), cwd=tmp_path, canonical_url="https://example.com/tokens/"
        ),
    )
    monkeypatch.setattr(
        profile_cli,
        "run_deploy",
        lambda *args, **kwargs: order.append("deploy") or deploy_result(),
    )

    assert profile_cli.run_profile(
        arguments(tmp_path, config, "publish", sync=sync, non_interactive=True)
    ) == 0
    assert order == (["sync"] if sync else []) + ["build", "plan", "deploy"]


def test_publish_json_keeps_progress_and_subprocess_details_off_stdout(
    tmp_path: Path,
    minimal_config: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = initialized_profile(tmp_path, minimal_config)
    monkeypatch.setattr(
        profile_cli, "_sync_profile_sources", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        profile_cli, "build_profile", lambda *args, **kwargs: build_result(tmp_path)
    )
    monkeypatch.setattr(
        profile_cli,
        "make_deploy_plan",
        lambda *args: SimpleNamespace(
            argv=("deploy",), cwd=tmp_path, canonical_url="https://example.com/tokens/"
        ),
    )
    monkeypatch.setattr(profile_cli, "run_deploy", lambda *args, **kwargs: deploy_result())

    assert profile_cli.run_profile(
        arguments(
            tmp_path,
            config,
            "publish",
            sync=True,
            json=True,
            non_interactive=True,
        )
    ) == 0

    captured = capsys.readouterr()
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out)["deploy"] == {"returncode": 0}
    assert "published" in captured.err
    assert "provider note" in captured.err


def test_publish_failure_preserves_subprocess_details_on_stderr(
    tmp_path: Path,
    minimal_config: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = initialized_profile(tmp_path, minimal_config)
    monkeypatch.setattr(
        profile_cli, "build_profile", lambda *args, **kwargs: build_result(tmp_path)
    )
    monkeypatch.setattr(
        profile_cli,
        "make_deploy_plan",
        lambda *args: SimpleNamespace(
            argv=("deploy",), cwd=tmp_path, canonical_url="https://example.com/tokens/"
        ),
    )
    error = DeployError(
        "deployment failed",
        result=DeployResult(2, "provider output\n", "provider error\n"),
    )
    monkeypatch.setattr(
        profile_cli,
        "run_deploy",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(DeployError):
        profile_cli.run_profile(
            arguments(tmp_path, config, "publish", non_interactive=True, json=True)
        )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "provider output\nprovider error\n"


@pytest.mark.parametrize("non_interactive", [False, True])
def test_publish_passes_approval_mode_to_deploy_runner(
    tmp_path: Path,
    minimal_config: str,
    monkeypatch: pytest.MonkeyPatch,
    non_interactive: bool,
) -> None:
    config = initialized_profile(tmp_path, minimal_config)
    seen: list[dict[str, object]] = []
    monkeypatch.setattr(
        profile_cli, "build_profile", lambda *args, **kwargs: build_result(tmp_path)
    )
    monkeypatch.setattr(
        profile_cli,
        "make_deploy_plan",
        lambda *args: SimpleNamespace(
            argv=("deploy",), cwd=tmp_path, canonical_url="https://example.com/tokens/"
        ),
    )
    monkeypatch.setattr(
        profile_cli,
        "run_deploy",
        lambda *args, **kwargs: seen.append(kwargs) or DeployResult(0, "", ""),
    )

    assert profile_cli.run_profile(
        arguments(
            tmp_path,
            config,
            "publish",
            non_interactive=non_interactive,
        )
    ) == 0

    assert seen[0]["non_interactive"] is non_interactive
    assert callable(seen[0]["confirm"])


def test_preview_forces_noindex_validates_and_uses_server_seam(
    tmp_path: Path,
    minimal_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = initialized_profile(tmp_path, minimal_config)
    seen: list[tuple[str, object]] = []

    def fake_build(*args, **kwargs):
        seen.append(("build", kwargs))
        return build_result(tmp_path)

    monkeypatch.setattr(profile_cli, "build_profile", fake_build)
    monkeypatch.setattr(
        profile_cli,
        "validate_site",
        lambda site, *, noindex: seen.append(("validate", (site, noindex))),
    )
    monkeypatch.setattr(
        profile_cli,
        "_serve_site",
        lambda site, *, host, port, open_browser: seen.append(
            ("serve", (site, host, port, open_browser))
        ),
    )

    assert profile_cli.run_profile(
        arguments(tmp_path, config, "preview", no_open=True)
    ) == 0

    assert seen[0][0] == "build"
    assert seen[0][1]["noindex"] is True
    assert seen[1][0] == "validate"
    assert seen[1][1][1] is True
    assert seen[2][0] == "serve"
    assert seen[2][1][2:] == (None, False)


def test_preview_server_binds_port_zero_and_browser_false_is_nonfatal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[object] = []

    class FakeServer:
        server_port = 43123

        def __enter__(self):
            calls.append("enter")
            return self

        def __exit__(self, *args: object) -> None:
            calls.append("close")

        def serve_forever(self, *, poll_interval: float) -> None:
            calls.append(("serve", poll_interval))
            raise KeyboardInterrupt

    def server_factory(address, handler):
        calls.append(address)
        assert handler.keywords["directory"] == str(tmp_path)
        return FakeServer()

    profile_cli._serve_site(
        tmp_path,
        host="127.0.0.1",
        port=None,
        open_browser=True,
        server_factory=server_factory,
        browser_open=lambda url: calls.append(("browser", url)) or False,
    )

    assert calls[0] == ("127.0.0.1", 0)
    assert ("browser", "http://127.0.0.1:43123/") in calls
    assert calls[-1] == "close"
    assert capsys.readouterr().out == "Preview → http://127.0.0.1:43123/\n"


def test_edit_can_publish_after_successful_validation(
    tmp_path: Path,
    minimal_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = initialized_profile(tmp_path, minimal_config)
    order: list[str] = []
    monkeypatch.setattr(
        profile_cli,
        "open_editor",
        lambda *args: order.append("edit") or 0,
    )
    monkeypatch.setattr(
        profile_cli,
        "load_config",
        lambda *args: order.append("validate") or SimpleNamespace(),
    )
    monkeypatch.setattr(
        profile_cli,
        "_publish",
        lambda *args, **kwargs: order.append("publish") or 0,
    )

    assert profile_cli.run_profile(
        arguments(tmp_path, config, "edit", publish=True)
    ) == 0
    assert order == ["edit", "validate", "publish"]


@pytest.mark.parametrize(
    ("action", "expected"),
    [(None, "status"), ("status", "status"), ("enable", "enable"), ("disable", "disable")],
)
def test_schedule_actions_use_owned_scheduler_seams(
    tmp_path: Path,
    minimal_config: str,
    monkeypatch: pytest.MonkeyPatch,
    action: str | None,
    expected: str,
) -> None:
    config = initialized_profile(tmp_path, minimal_config)
    calls: list[str] = []
    status = SimpleNamespace(
        enabled=expected == "enable",
        backend="test",
        job_path=None,
        command=("tokenmaxxing",),
        next_step=None,
    )
    monkeypatch.setattr(
        profile_cli, "_schedule_status", lambda *args, **kwargs: calls.append("status") or status
    )
    monkeypatch.setattr(
        profile_cli, "_enable_schedule", lambda *args, **kwargs: calls.append("enable") or status
    )
    monkeypatch.setattr(
        profile_cli, "_disable_schedule", lambda *args, **kwargs: calls.append("disable") or status
    )

    assert profile_cli.run_profile(
        arguments(tmp_path, config, "schedule", action=action)
    ) == 0
    assert calls == [expected]


def test_status_reports_stale_deploy_approval_without_mutating(
    tmp_path: Path,
    minimal_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = initialized_profile(tmp_path, minimal_config)
    paths = profile_paths(config)
    paths.site.mkdir(parents=True)
    paths.deploy_approval.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        profile_cli,
        "make_deploy_plan",
        lambda *args: SimpleNamespace(fingerprint="new"),
    )
    monkeypatch.setattr(profile_cli, "is_approved", lambda *args: False)
    monkeypatch.setattr(
        profile_cli,
        "_schedule_status",
        lambda *args, **kwargs: SimpleNamespace(
            enabled=False,
            backend="test",
            job_path=None,
            command=(),
            next_step=None,
        ),
    )

    payload = profile_cli._status_payload(config, tmp_path / "usage.sqlite3")

    assert payload["approval"] == "stale"
    assert payload["site"] == str(paths.site)


def test_human_status_shows_project_paths_without_the_private_database(
    tmp_path: Path,
    minimal_config: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = initialized_profile(tmp_path, minimal_config)
    paths = profile_paths(config)
    private_database = tmp_path / "private-history" / "usage.sqlite3"
    monkeypatch.setattr(
        profile_cli,
        "_status_payload",
        lambda config_path, db_path: {
            "approval": "current",
            "canonical_url": "https://example.com/tokens/",
            "config": str(config_path),
            "schedule": {
                "backend": "launchd",
                "command": ["tokenmaxxing", "--db", str(private_database)],
                "enabled": True,
                "job_path": str(tmp_path / "Library" / "LaunchAgents" / "profile.plist"),
                "next_step": None,
            },
            "site": str(paths.site),
        },
    )

    assert profile_cli.run_profile(
        arguments(tmp_path, config, "status", db=private_database)
    ) == 0

    output = capsys.readouterr().out
    assert f"Config: {config}" in output
    assert f"Site: {paths.site}" in output
    assert "Deploy approval: current" in output
    assert "Schedule: enabled (launchd)" in output
    assert str(private_database) not in output


def test_init_no_setup_creates_a_valid_starter_without_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        profile_cli.Prompt,
        "ask",
        lambda *args, **kwargs: pytest.fail("--no-setup must not prompt"),
    )
    args = arguments(
        tmp_path,
        None,
        "init",
        directory=tmp_path / "new-profile",
        no_setup=True,
    )

    assert profile_cli.run_profile(args) == 0

    config = profile_cli.load_config(tmp_path / "new-profile" / "tokenmaxxing.yaml")
    assert config.site.indexable is False
    assert config.deploy.command == ()


def test_interactive_setup_retries_invalid_public_fields_before_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = tmp_path / "profile"
    answers = iter(
        (
            "Ada Lovelace",
            "Programmer",
            "Builds analytical engines.",
            "",
            "Website",
            "ada.example",
            "http://ada.example",
            "https://ada.example/",
            "",
            "http://example.com/tokens",
            "https://example.com/tokens",
            "Mars/Olympus_Mons",
            "Europe/London",
            "9am",
            "08:05",
            "none",
        )
    )

    def answer(*args, **kwargs):
        assert not directory.exists()
        return next(answers)

    monkeypatch.setattr(profile_cli.Prompt, "ask", answer)
    args = arguments(
        tmp_path,
        None,
        "init",
        directory=directory,
        no_setup=False,
    )

    assert profile_cli.run_profile(args) == 0

    config = profile_cli.load_config(directory / "tokenmaxxing.yaml")
    assert config.profile.links[0].url == "https://ada.example/"
    assert config.site.canonical_url == "https://example.com/tokens/"
    assert config.site.timezone.key == "Europe/London"
    assert config.schedule.time.isoformat(timespec="minutes") == "08:05"
    errors = capsys.readouterr().err
    assert "Link URL must use https or mailto" in errors
    assert "Public profile URL must use https" in errors
    assert "Timezone must be an IANA name" in errors
    assert "Daily publish time must use HH:MM" in errors


def test_cancelled_interactive_setup_can_be_rerun_without_force(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "profile"
    args = arguments(
        tmp_path,
        None,
        "init",
        directory=directory,
        no_setup=False,
    )
    monkeypatch.setattr(
        profile_cli.Prompt,
        "ask",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        profile_cli.run_profile(args)

    assert not directory.exists()
    answers = iter(("Ada", "", "", "", "", "https://example.com/", "UTC", "09:00", "none"))
    monkeypatch.setattr(profile_cli.Prompt, "ask", lambda *args, **kwargs: next(answers))
    assert profile_cli.run_profile(args) == 0
    assert (directory / "tokenmaxxing.yaml").is_file()


def test_force_preserves_existing_profile_files_byte_for_byte(
    tmp_path: Path,
    minimal_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "profile"
    directory.mkdir()
    config = directory / "tokenmaxxing.yaml"
    config_bytes = ("# keep this comment\n" + minimal_config).encode()
    config.write_bytes(config_bytes)
    custom_css = directory / "custom.css"
    custom_bytes = b"/* mine */\n"
    custom_css.write_bytes(custom_bytes)
    wrangler = directory / "wrangler.jsonc"
    wrangler_bytes = b'{"name":"mine"}\n'
    wrangler.write_bytes(wrangler_bytes)
    monkeypatch.setattr(
        profile_cli.Prompt,
        "ask",
        lambda *args, **kwargs: pytest.fail("existing config must not restart onboarding"),
    )
    args = arguments(
        tmp_path,
        None,
        "init",
        directory=directory,
        no_setup=False,
        force=True,
    )

    assert profile_cli.run_profile(args) == 0

    assert config.read_bytes() == config_bytes
    assert custom_css.read_bytes() == custom_bytes
    assert wrangler.read_bytes() == wrangler_bytes


def test_cloudflare_onboarding_does_not_overwrite_existing_wrangler(
    tmp_path: Path,
    minimal_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "tokenmaxxing.yaml"
    config_path.write_text(minimal_config, encoding="utf-8")
    (tmp_path / "avatar.webp").write_bytes(b"avatar")
    wrangler = tmp_path / "wrangler.jsonc"
    original = b'{"name":"hand-written"}\n'
    wrangler.write_bytes(original)
    answers = iter(
        (
            "Ada Lovelace",
            "Programmer",
            "Builds analytical engines.",
            "",
            "",
            "https://example.com/tokens/",
            "UTC",
            "09:00",
            "cloudflare",
        )
    )
    monkeypatch.setattr(
        profile_cli.Prompt, "ask", lambda *args, **kwargs: next(answers)
    )
    monkeypatch.setattr(
        profile_cli,
        "_cloudflare_command",
        lambda: ("npx", "wrangler", "deploy", "--assets", "{site_dir}"),
    )

    configured = profile_cli._interactive_config(
        config_path, profile_cli.load_config(config_path)
    )
    profile_cli._write_cloudflare_config(wrangler)

    assert configured.config.deploy.command[1] == "wrangler"
    assert configured.cloudflare is True
    assert wrangler.read_bytes() == original


def test_deploy_confirmation_preserves_argv_boundaries_and_escapes_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = DeployPlan(
        command_template=("deploy tool", "{site_dir}"),
        argv=(
            "/Applications/Deploy Tool/deploy",
            "argument with spaces",
            'quote"inside',
            "line\nbreak",
            "tab\tinside",
        ),
        cwd=tmp_path / "working directory",
        canonical_url="https://example.com/tokens/",
        fingerprint="canonical-fingerprint",
    )
    monkeypatch.setattr(
        profile_cli.Confirm, "ask", lambda *args, **kwargs: False
    )

    assert profile_cli._confirm_deploy(plan) is False

    output = capsys.readouterr().err
    assert '[0] "/Applications/Deploy Tool/deploy"' in output
    assert '[1] "argument with spaces"' in output
    assert '[2] "quote\\"inside"' in output
    assert '[3] "line\\nbreak"' in output
    assert '[4] "tab\\tinside"' in output
    assert "line\nbreak" not in output
    assert "canonical-fingerprint" not in output
