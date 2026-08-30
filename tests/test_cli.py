import json
from pathlib import Path

import pytest


def test_sync_json_reports_each_requested_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tokenmaxxing.cli import main

    exit_code = main(
        [
            "--db",
            str(tmp_path / "usage.sqlite3"),
            "sync",
            "--codex-root",
            str(tmp_path / "codex"),
            "--claude-root",
            str(tmp_path / "claude"),
            "--pi-root",
            str(tmp_path / "pi"),
            "--opencode-db",
            str(tmp_path / "opencode.db"),
            "--json",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "results": [
            {
                "error_category": None,
                "source": source,
                "stats": {
                    "artifacts_seen": 0,
                    "events_inserted": 0,
                    "events_updated": 0,
                    "issues_recorded": 0,
                    "lines_read": 0,
                    "observations_inserted": 0,
                },
                "status": "skipped",
            }
            for source in ("codex", "claude", "pi", "opencode")
        ]
    }


def test_stats_json_is_stable_for_an_empty_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tokenmaxxing.cli import main

    arguments = ["--db", str(tmp_path / "usage.sqlite3"), "stats", "--json"]

    assert main(arguments) == 0
    first = capsys.readouterr().out
    assert main(arguments) == 0
    second = capsys.readouterr().out

    assert first == second == '{"group_by": "source", "stats": []}\n'


def test_stats_human_output_is_a_compact_table(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tokenmaxxing.cli import main

    assert main(["--db", str(tmp_path / "usage.sqlite3"), "stats"]) == 0

    assert (
        capsys.readouterr().out
        == "Group Events Input Output Cache R Cache W Reason Total Cost\n"
    )


def test_local_timezone_uses_an_iana_zoneinfo_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing.cli import _local_timezone

    zoneinfo_file = tmp_path / "zoneinfo" / "Asia" / "Kolkata"
    zoneinfo_file.parent.mkdir(parents=True)
    zoneinfo_file.touch()
    localtime = tmp_path / "localtime"
    localtime.symlink_to(zoneinfo_file)
    monkeypatch.delenv("TZ", raising=False)

    timezone = _local_timezone(localtime)

    assert timezone is not None
    assert timezone.key == "Asia/Kolkata"


def test_malformed_tz_does_not_break_help(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing.cli import main

    monkeypatch.setenv("TZ", "/etc/localtime")

    with pytest.raises(SystemExit) as exited:
        main(["--help"])

    captured = capsys.readouterr()
    assert exited.value.code == 0
    assert "Traceback" not in captured.err
    assert "/etc/localtime" not in captured.err


def test_export_writes_only_aggregate_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tokenmaxxing.cli import main

    database = tmp_path / "usage.sqlite3"
    destination = tmp_path / "report.json"

    assert main(["--db", str(database), "export", str(destination)]) == 0
    assert capsys.readouterr().out == ""
    payload = json.loads(destination.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["overall"] == {
        "event_count": 0,
        "group": "all",
        "total_tokens": 0,
    }
    assert set(payload) == {
        "by_day",
        "by_model",
        "by_source",
        "generated_at",
        "overall",
        "schema_version",
        "timezone",
    }


def test_sync_source_errors_exit_nonzero_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokenmaxxing import cli
    from tokenmaxxing.sync import SourceSyncResult

    def source_error(*args: object, **kwargs: object) -> tuple[SourceSyncResult, ...]:
        assert kwargs == {"raise_errors": False}
        return (
            SourceSyncResult(
                source="claude",
                status="error",
                error_category="ValueError",
            ),
        )

    monkeypatch.setattr(cli, "sync_sources", source_error)

    assert cli.main(["--db", str(tmp_path / "usage.sqlite3"), "sync", "--json"]) == 1
    captured = capsys.readouterr()

    assert "ValueError" in captured.err
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_sync_debug_propagates_the_original_source_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing import cli

    expected = RuntimeError("PRIVATE_SOURCE_CONTENT")

    def source_error(*args: object, **kwargs: object) -> tuple[object, ...]:
        assert kwargs == {"raise_errors": True}
        raise expected

    monkeypatch.setattr(cli, "sync_sources", source_error)

    with pytest.raises(RuntimeError) as raised:
        cli.main(["--debug", "--db", str(tmp_path / "usage.sqlite3"), "sync"])

    assert raised.value is expected
