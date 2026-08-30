import json
import shutil
import time
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from zoneinfo import TZPATH, ZoneInfo

import pytest

from tokenmaxxing.db import Database
from tokenmaxxing.models import Projection, Source, TokenUsage, UsageEventDraft
from tokenmaxxing.repository import Repository
from tokenmaxxing.sync import SourceSyncResult


def _record_usage(
    path: Path,
    *,
    event_key: str,
    model: str,
    tokens: int,
    started_at: datetime | None,
    source: Source = "codex",
) -> None:
    database = Database.open(path)
    try:
        Repository(database).apply_projection(
            Projection(
                events=(
                    UsageEventDraft(
                        source=source,
                        event_key=event_key,
                        granularity="model_call",
                        status="canonical",
                        model=model,
                        started_at_ns=(
                            int(started_at.timestamp()) * 1_000_000_000
                            if started_at is not None
                            else None
                        ),
                        tokens=TokenUsage(input=tokens),
                    ),
                )
            )
        )
    finally:
        database.close()


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

    assert first == second
    assert json.loads(first) == {
        "api_equivalent": {
            "by_provider": [],
            "cost_nanos": 0,
            "priced_events": 0,
            "priced_tokens": 0,
            "total_events": 0,
            "total_tokens": 0,
        },
        "group_by": "model",
        "period": "28d",
        "stats": [],
    }


def test_stats_defaults_to_a_twenty_eight_day_model_leaderboard(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokenmaxxing import cli

    database = tmp_path / "usage.sqlite3"
    now = datetime(2026, 5, 29, 10, tzinfo=UTC)
    _record_usage(
        database,
        event_key="recent",
        model="gpt-5.4",
        tokens=1_250,
        started_at=datetime(2026, 5, 28, tzinfo=UTC),
    )
    _record_usage(
        database,
        event_key="old",
        model="old-model",
        tokens=8_000,
        started_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    _record_usage(
        database,
        event_key="smaller",
        model="a-smaller-model",
        tokens=800,
        started_at=datetime(2026, 5, 28, tzinfo=UTC),
    )
    monkeypatch.setattr(cli, "_now", lambda timezone: now, raising=False)

    assert cli.main(["--db", str(database), "stats"]) == 0

    output = capsys.readouterr().out
    assert "Last 28 days" in output
    assert "2K tokens" in output
    assert "API equivalent" not in output
    assert "Models" in output
    assert "gpt-5.4" in output
    assert "Just a light snack." in output
    assert output.index("gpt-5.4") < output.index("a-smaller-model")
    assert "old-model" not in output
    assert "%" not in output


def test_stats_api_equivalent_uses_the_same_window_in_text_and_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokenmaxxing import cli

    database = tmp_path / "usage.sqlite3"
    now = datetime(2026, 5, 29, 10, tzinfo=UTC)
    _record_usage(
        database,
        event_key="recent",
        model="claude-opus-5",
        tokens=1_000_000,
        started_at=datetime(2026, 5, 28, tzinfo=UTC),
        source="claude",
    )
    _record_usage(
        database,
        event_key="old",
        model="claude-opus-5",
        tokens=2_000_000,
        started_at=datetime(2026, 4, 1, tzinfo=UTC),
        source="claude",
    )
    monkeypatch.setattr(cli, "_now", lambda timezone: now)

    assert cli.main(["--db", str(database), "stats"]) == 0
    assert "API equivalent: ≈$5" in capsys.readouterr().out

    assert cli.main(["--db", str(database), "stats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["api_equivalent"] == {
        "by_provider": [
            {
                "cost_nanos": 5_000_000_000,
                "priced_events": 1,
                "priced_tokens": 1_000_000,
                "provider": "anthropic",
            }
        ],
        "cost_nanos": 5_000_000_000,
        "priced_events": 1,
        "priced_tokens": 1_000_000,
        "total_events": 1,
        "total_tokens": 1_000_000,
    }


def test_api_equivalent_is_hidden_below_meaningful_token_coverage() -> None:
    from tokenmaxxing.cli import _api_value_copy
    from tokenmaxxing.pricing import ApiValueEstimate

    estimate = ApiValueEstimate(
        cost_nanos=1,
        priced_tokens=94,
        total_tokens=100,
        priced_events=1,
        total_events=2,
        by_provider=(),
    )

    assert _api_value_copy(estimate) is None


def test_api_equivalent_is_shown_at_meaningful_token_coverage() -> None:
    from tokenmaxxing.cli import _api_value_copy
    from tokenmaxxing.pricing import ApiValueEstimate

    estimate = ApiValueEstimate(
        cost_nanos=1,
        priced_tokens=95,
        total_tokens=100,
        priced_events=1,
        total_events=2,
        by_provider=(),
    )

    assert _api_value_copy(estimate) == "API equivalent: <$0.01"


def test_stats_uses_one_reporting_snapshot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokenmaxxing.cli import main

    original = Repository.reporting_rows
    calls = 0

    def counted(repository: Repository):
        nonlocal calls
        calls += 1
        return original(repository)

    monkeypatch.setattr(Repository, "reporting_rows", counted)

    assert main(["--db", str(tmp_path / "usage.sqlite3"), "stats"]) == 0
    capsys.readouterr()
    assert calls == 1


@pytest.mark.parametrize(
    ("tokens", "message"),
    [
        (0, "Quiet. Suspiciously human."),
        (99_999, "Just a light snack."),
        (999_999, "A tidy little token trail."),
        (9_999_999, "The agents are stretching their legs."),
        (99_999_999, "Your autocomplete has a work ethic."),
        (999_999_999, "You may have accidentally hired a small team."),
        (9_999_999_999, "You didn't use AI. You employed a small civilization."),
        (10_000_000_000, "The tokens have unionized."),
    ],
)
def test_usage_hint_tracks_the_size_of_the_token_trail(
    tokens: int, message: str
) -> None:
    from tokenmaxxing.cli import _usage_hint

    assert _usage_hint(tokens) == message


def test_stats_periods_are_reported_in_plain_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing import cli

    database = tmp_path / "usage.sqlite3"
    now = datetime(2026, 5, 29, 10, tzinfo=UTC)
    _record_usage(
        database,
        event_key="old",
        model="old-model",
        tokens=2_000,
        started_at=datetime(2026, 4, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(cli, "_now", lambda timezone: now, raising=False)

    assert cli.main(["--db", str(database), "stats", "--period", "28d", "--json"]) == 0
    twenty_eight_days = json.loads(capsys.readouterr().out)
    assert twenty_eight_days["period"] == "28d"
    assert twenty_eight_days["group_by"] == "model"
    assert twenty_eight_days["stats"] == []

    assert cli.main(["--db", str(database), "stats", "--period", "all", "--json"]) == 0
    all_time = json.loads(capsys.readouterr().out)
    assert all_time["period"] == "all"
    assert [(stat["group"], stat["total_tokens"]) for stat in all_time["stats"]] == [
        ("old-model", 2_000)
    ]


def test_stats_harness_view_and_human_model_labels_are_presentation_only(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokenmaxxing import cli

    database = tmp_path / "usage.sqlite3"
    now = datetime(2026, 5, 29, 10, tzinfo=UTC)
    _record_usage(
        database,
        event_key="unknown",
        model="(unknown)",
        tokens=600,
        started_at=now,
    )
    _record_usage(
        database,
        event_key="synthetic",
        model="<synthetic>",
        tokens=0,
        started_at=now,
    )
    monkeypatch.setattr(cli, "_now", lambda timezone: now, raising=False)

    assert cli.main(["--db", str(database), "stats"]) == 0
    output = capsys.readouterr().out
    assert "unknown models" in output
    assert "(unknown)" not in output
    assert "<synthetic>" not in output

    assert (
        cli.main(["--db", str(database), "stats", "--group-by", "harness", "--json"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["group_by"] == "harness"
    assert payload["stats"][0]["group"] == "codex"


def test_stats_empty_state_invites_a_sync(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tokenmaxxing.cli import main

    assert main(["--db", str(tmp_path / "usage.sqlite3"), "stats"]) == 0

    assert "No tokens in the last 28 days yet." in capsys.readouterr().out


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


def test_local_timezone_preserves_dst_rules_from_a_copied_tzfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing.cli import _local_timezone

    source = next(
        path / "America" / "New_York"
        for path in map(Path, TZPATH)
        if (path / "America" / "New_York").is_file()
    )
    localtime = tmp_path / "localtime"
    shutil.copyfile(source, localtime)
    monkeypatch.delenv("TZ", raising=False)

    timezone = _local_timezone(localtime)

    assert datetime(2026, 1, 15, 12, tzinfo=UTC).astimezone(timezone).utcoffset() == timedelta(
        hours=-5
    )
    assert datetime(2026, 7, 15, 12, tzinfo=UTC).astimezone(timezone).utcoffset() == timedelta(
        hours=-4
    )


def test_local_timezone_uses_tzlocal_before_a_fixed_offset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing import cli

    expected = ZoneInfo("America/New_York")
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(cli.tzlocal, "get_localzone", lambda: expected)

    timezone = cli._local_timezone(tmp_path / "missing-localtime")

    assert timezone is expected
    assert datetime(2026, 1, 15, 12, tzinfo=UTC).astimezone(timezone).utcoffset() == timedelta(
        hours=-5
    )
    assert datetime(2026, 7, 15, 12, tzinfo=UTC).astimezone(timezone).utcoffset() == timedelta(
        hours=-4
    )


def test_redirected_stats_are_plain_and_invariant_to_terminal_width(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokenmaxxing import cli

    database = tmp_path / "usage.sqlite3"
    now = datetime(2026, 5, 29, 10, tzinfo=UTC)
    _record_usage(
        database,
        event_key="literal-model",
        model="[red]literal-model[/red]",
        tokens=1_250,
        started_at=now,
    )
    monkeypatch.setattr(cli, "_now", lambda timezone: now)

    monkeypatch.setenv("COLUMNS", "20")
    assert cli.main(["--db", str(database), "stats"]) == 0
    narrow = capsys.readouterr().out
    monkeypatch.setenv("COLUMNS", "200")
    assert cli.main(["--db", str(database), "stats"]) == 0
    wide = capsys.readouterr().out

    assert narrow == wide
    assert "[red]literal-model[/red]" in narrow


def test_tty_stats_render_model_names_as_literal_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing import cli

    class Terminal(StringIO):
        def isatty(self) -> bool:
            return True

    database = tmp_path / "usage.sqlite3"
    now = datetime(2026, 5, 29, 10, tzinfo=UTC)
    _record_usage(
        database,
        event_key="literal-model",
        model="[red]literal-model[/red]",
        tokens=1_250,
        started_at=now,
    )
    output = Terminal()
    monkeypatch.setattr(cli, "_now", lambda timezone: now)
    monkeypatch.setattr(cli.sys, "stdout", output)
    monkeypatch.setenv("TERM", "xterm-256color")

    assert cli.main(["--db", str(database), "stats"]) == 0

    assert "[red]literal-model[/red]" in output.getvalue()


def test_redirected_stats_escape_model_control_characters_but_json_keeps_them(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokenmaxxing import cli

    database = tmp_path / "usage.sqlite3"
    now = datetime(2026, 5, 29, 10, tzinfo=UTC)
    model = "line\nreturn\rescape\x1bdelete\x7fnext\x85"
    _record_usage(
        database,
        event_key="control-model",
        model=model,
        tokens=1_250,
        started_at=now,
    )
    monkeypatch.setattr(cli, "_now", lambda timezone: now)

    assert cli.main(["--db", str(database), "stats"]) == 0
    output = capsys.readouterr().out
    assert "line\\x0areturn\\x0descape\\x1bdelete\\x7fnext\\x85" in output
    assert "API equivalent" not in output
    assert output.count("\n") == 4
    assert "\r" not in output
    assert "\x1b" not in output
    assert "\x7f" not in output
    assert "\x85" not in output

    assert cli.main(["--db", str(database), "stats", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stats"][0]["group"] == model


def test_tty_stats_escape_model_control_characters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing import cli

    class Terminal(StringIO):
        def isatty(self) -> bool:
            return True

    database = tmp_path / "usage.sqlite3"
    now = datetime(2026, 5, 29, 10, tzinfo=UTC)
    model = "line\nreturn\rescape\x1bdelete\x7fnext\x85"
    _record_usage(
        database,
        event_key="control-model",
        model=model,
        tokens=1_250,
        started_at=now,
    )
    output = Terminal()
    monkeypatch.setattr(cli, "_now", lambda timezone: now)
    monkeypatch.setattr(cli.sys, "stdout", output)
    monkeypatch.setenv("TERM", "xterm-256color")

    assert cli.main(["--db", str(database), "stats"]) == 0

    rendered = output.getvalue()
    assert "line\\x0areturn\\x0descape\\x1bdelete\\x7fnext\\x85" in rendered
    assert "API equivalent" not in rendered
    assert rendered.count("\n") == 4
    assert "\r" not in rendered
    assert "\x1b" not in rendered
    assert "\x7f" not in rendered
    assert "\x85" not in rendered


def test_compact_tokens_roll_over_suffix_boundaries() -> None:
    from tokenmaxxing.cli import _compact_tokens

    assert _compact_tokens(999_949) == "999.9K"
    assert _compact_tokens(999_950) == "1M"
    assert _compact_tokens(999_949_999) == "999.9M"
    assert _compact_tokens(999_950_000) == "1B"


@pytest.mark.parametrize(
    ("cost_nanos", "formatted"),
    [
        (0, "$0"),
        (1, "<$0.01"),
        (9_000_000, "<$0.01"),
        (10_000_000, "$0.01"),
        (1_250_000_000, "$1.25"),
        (12_490_000_000, "$12"),
        (999_949_000_000, "$999.9"),
        (999_950_000_000, "$1K"),
        (8_026_670_000_000, "$8K"),
        (1_250_000_000_000_000, "$1.3M"),
    ],
)
def test_compact_usd_handles_quiet_and_extreme_usage(
    cost_nanos: int, formatted: str
) -> None:
    from tokenmaxxing.cli import _compact_usd

    assert _compact_usd(cost_nanos) == formatted


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
    assert capsys.readouterr().out == f"Exported aggregate JSON → {destination}\n"
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


def test_export_defaults_to_a_clearly_named_json_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokenmaxxing.cli import main

    monkeypatch.chdir(tmp_path)

    assert main(["--db", str(tmp_path / "usage.sqlite3"), "export"]) == 0

    destination = Path("tokenmaxxing-export.json")
    assert json.loads(destination.read_text(encoding="utf-8"))["schema_version"] == 1
    assert capsys.readouterr().out == (
        "Exported aggregate JSON → tokenmaxxing-export.json\n"
    )


def test_export_accepts_a_directory_and_rejects_a_misleading_extension(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tokenmaxxing.cli import main

    database = tmp_path / "usage.sqlite3"

    directory = tmp_path / "exports.v1"
    directory.mkdir()
    assert main(["--db", str(database), "export", str(directory)]) == 0
    destination = directory / "tokenmaxxing-export.json"
    assert destination.is_file()
    assert capsys.readouterr().out == f"Exported aggregate JSON → {destination}\n"

    uppercase_destination = tmp_path / "report.JSON"
    assert main(["--db", str(database), "export", str(uppercase_destination)]) == 0
    assert uppercase_destination.is_file()
    assert capsys.readouterr().out == (
        f"Exported aggregate JSON → {uppercase_destination}\n"
    )

    csv_destination = tmp_path / "out.csv"
    with pytest.raises(SystemExit) as exited:
        main(["--db", str(database), "export", str(csv_destination)])
    captured = capsys.readouterr()
    assert exited.value.code == 2
    assert captured.out == ""
    assert "export writes JSON; choose a .json file or an existing directory" in (
        captured.err
    )
    assert not csv_destination.exists()


def test_export_help_names_the_format_and_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tokenmaxxing.cli import main

    with pytest.raises(SystemExit) as exited:
        main(["export", "--help"])

    assert exited.value.code == 0
    output = capsys.readouterr().out
    assert "aggregate JSON" in output
    assert "tokenmaxxing-export.json" in output


def test_sync_source_errors_exit_nonzero_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokenmaxxing import cli
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

    assert captured.err == ""
    assert captured.out == (
        '{"results": [{"error_category": "ValueError", "source": "claude", '
        '"stats": {"artifacts_seen": 0, "events_inserted": 0, '
        '"events_updated": 0, "issues_recorded": 0, "lines_read": 0, '
        '"observations_inserted": 0}, "status": "error"}]}\n'
    )


def test_sync_redirected_output_has_plain_statuses_and_a_hint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from tokenmaxxing.cli import main

    assert (
        main(
            [
                "--db",
                str(tmp_path / "usage.sqlite3"),
                "sync",
                "--source",
                "codex",
                "--codex-root",
                str(tmp_path / "missing-codex"),
            ]
        )
        == 0
    )

    assert capsys.readouterr().out == (
        "codex: skipped\n"
        "Try `tokenmaxxing stats` to see your model leaderboard.\n"
    )


def test_sync_success_and_error_statuses_are_concise(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokenmaxxing import cli

    monkeypatch.setattr(
        cli,
        "sync_sources",
        lambda *args, **kwargs: (
            SourceSyncResult(source="codex", status="ok"),
            SourceSyncResult(
                source="claude", status="error", error_category="ValueError"
            ),
        ),
    )

    assert cli.main(["--db", str(tmp_path / "usage.sqlite3"), "sync"]) == 1
    captured = capsys.readouterr()
    assert captured.out == (
        "codex: synced\nTry `tokenmaxxing stats` to see your model leaderboard.\n"
    )
    assert captured.err == "claude: error (ValueError)\n"


def test_sync_tty_status_uses_conversational_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tokenmaxxing import cli

    class Terminal(StringIO):
        def isatty(self) -> bool:
            return True

    output = Terminal()
    monkeypatch.setattr(cli.sys, "stdout", output)
    monkeypatch.setenv("TERM", "xterm-256color")

    assert (
        cli.main(
            [
                "--db",
                str(tmp_path / "usage.sqlite3"),
                "sync",
                "--source",
                "codex",
                "--codex-root",
                str(tmp_path / "missing-codex"),
            ]
        )
        == 0
    )

    assert "Reading your local token trail" in output.getvalue()


def test_sync_status_rotates_its_copy() -> None:
    from tokenmaxxing.cli import _SYNC_MESSAGE_INTERVAL, _rotating_status

    assert _SYNC_MESSAGE_INTERVAL == 3.0

    updates: list[str] = []

    class FakeStatus:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *args: object) -> None:
            return None

        def update(self, message: str) -> None:
            updates.append(message)

    class FakeConsole:
        def status(self, message: str, *, spinner: str) -> FakeStatus:
            assert spinner == "dots"
            updates.append(message)
            return FakeStatus()

    with _rotating_status(
        FakeConsole(),
        ("Reading your local token trail", "Untangling the agent family tree"),
        interval=0.001,
    ):
        deadline = time.monotonic() + 0.2
        while len(updates) < 2 and time.monotonic() < deadline:
            time.sleep(0.001)

    assert updates[:2] == [
        "Reading your local token trail",
        "Untangling the agent family tree",
    ]

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


def test_unknown_root_command_never_falls_through_to_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokenmaxxing import cli

    monkeypatch.setattr(
        cli, "_export", lambda arguments: pytest.fail("unknown command reached export")
    )

    with pytest.raises(SystemExit):
        cli.main(["definitely-not-a-command"])


def test_version_is_available_without_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    from tokenmaxxing.cli import main

    with pytest.raises(SystemExit) as raised:
        main(["--version"])

    assert raised.value.code == 0
    assert capsys.readouterr().out.strip()


def test_profile_errors_are_useful_and_debug_can_reraise(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tokenmaxxing import cli

    expected = ValueError("profile configuration needs a canonical URL")
    monkeypatch.setattr(
        cli.profile_cli,
        "run_profile",
        lambda arguments: (_ for _ in ()).throw(expected),
    )
    command = ["profile", "--config", str(tmp_path / "tokenmaxxing.yaml"), "status"]

    assert cli.main(command) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error: profile configuration needs a canonical URL\n"

    with pytest.raises(ValueError) as raised:
        cli.main(["--debug", *command])
    assert raised.value is expected
