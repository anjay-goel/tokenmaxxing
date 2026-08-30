import json
from pathlib import Path

import pytest

from tokenmaxxing.db import Database
from tokenmaxxing.ingest.codex import sync_codex
from tokenmaxxing.models import (
    TokenUsage,
)
from tokenmaxxing.repository import Repository
from codex_support import (
    _copy,
    _create_state_db,
    _create_thread_history_db,
    _roots,
    _session_meta,
    _token_count,
    _write_jsonl,
)


@pytest.fixture
def codex_fixtures() -> Path:
    return Path(__file__).parent / "fixtures" / "codex"


def test_counter_reset_uses_last_usage(
    repository: Repository, tmp_path: Path, codex_fixtures: Path
) -> None:
    roots = _roots(tmp_path)
    _copy(codex_fixtures / "reset.jsonl", roots.sessions / "reset.jsonl")

    sync_codex(repository, roots)

    assert repository.source_total("codex").tokens == TokenUsage(input=130, output=17)
    final_event = repository.get_event("codex:reset-session:3")
    assert final_event is not None
    assert final_event.status == "excluded"


def test_copied_parent_prefix_does_not_change_totals(
    repository: Repository, tmp_path: Path, codex_fixtures: Path
) -> None:
    roots = _roots(tmp_path)
    _copy(codex_fixtures / "basic.jsonl", roots.sessions / "parent.jsonl")
    sync_codex(repository, roots)
    assert repository.source_total("codex").tokens.input == 100

    child_rollout = roots.sessions / "child.jsonl"
    child_rollout.write_bytes(
        (codex_fixtures / "copied_parent.jsonl").read_bytes()
        + (codex_fixtures / "child.jsonl").read_bytes()
    )
    sync_codex(repository, roots)

    assert repository.source_total("codex").tokens.input == 125
    assert repository.event_count("codex") == 2


def test_invalid_usage_does_not_change_later_semantic_ordinals_across_syncs(
    repository: Repository, tmp_path: Path
) -> None:
    session = _session_meta("ordinal-session")
    invalid = {
        "timestamp": "2026-08-28T00:00:01Z",
        "type": "event_msg",
        "payload": {"type": "token_count", "info": None},
    }
    valid = _token_count(5, 5)
    incremental_roots = _roots(tmp_path / "incremental")
    incremental_rollout = incremental_roots.sessions / "rollout.jsonl"
    _write_jsonl(incremental_rollout, [session, invalid])
    sync_codex(repository, incremental_roots)
    with incremental_rollout.open("a", encoding="utf-8") as rollout_file:
        rollout_file.write(json.dumps(valid) + "\n")
    sync_codex(repository, incremental_roots)

    one_shot_database = Database.open(tmp_path / "one-shot.sqlite3")
    try:
        one_shot_repository = Repository(one_shot_database)
        one_shot_roots = _roots(tmp_path / "one-shot")
        _write_jsonl(one_shot_roots.sessions / "rollout.jsonl", [session, invalid, valid])
        sync_codex(one_shot_repository, one_shot_roots)

        assert repository.list_event_keys("codex") == {"codex:ordinal-session:0"}
        assert one_shot_repository.list_event_keys("codex") == {
            "codex:ordinal-session:0"
        }
        assert repository.source_total("codex").tokens == TokenUsage(input=5)
        assert one_shot_repository.source_total("codex").tokens == TokenUsage(input=5)
    finally:
        one_shot_database.close()


def test_conflict_invalidates_baseline_until_the_next_unambiguous_event(
    repository: Repository, tmp_path: Path
) -> None:
    roots = _roots(tmp_path)
    _write_jsonl(
        roots.sessions / "copy-a.jsonl",
        [
            _session_meta("conflict-session"),
            _token_count(100, 10),
            _token_count(250, 25),
            _token_count(260, 10),
        ],
    )
    _write_jsonl(
        roots.sessions / "copy-b.jsonl",
        [
            _session_meta("conflict-session"),
            _token_count(200, 20),
            _token_count(250, 25),
            _token_count(260, 10),
        ],
    )

    sync_codex(repository, roots)

    conflicted = repository.get_event("codex:conflict-session:0")
    recovered = repository.get_event("codex:conflict-session:1")
    continued = repository.get_event("codex:conflict-session:2")
    assert conflicted is not None and conflicted.status == "conflicted"
    assert recovered is not None and recovered.tokens == TokenUsage(input=25)
    assert continued is not None and continued.tokens == TokenUsage(input=10)
    assert repository.source_total("codex").tokens == TokenUsage(input=35)


def test_spawn_edges_set_root_parent_and_run_depth(
    repository: Repository, tmp_path: Path, codex_fixtures: Path
) -> None:
    roots = _roots(tmp_path)
    child_rollout = roots.sessions / "child.jsonl"
    child_rollout.parent.mkdir(parents=True)
    child_rollout.write_bytes(
        (codex_fixtures / "copied_parent.jsonl").read_bytes()
        + (codex_fixtures / "child.jsonl").read_bytes()
    )
    _create_state_db(roots.state_db)

    sync_codex(repository, roots)

    session_rows = repository.connection.execute(
        "SELECT source_session_id, root_session_id, parent_session_id "
        "FROM sessions WHERE source = 'codex' ORDER BY source_session_id"
    ).fetchall()
    assert session_rows == [
        ("child-session", "parent-session", "parent-session"),
        ("parent-session", "parent-session", None),
    ]
    run_rows = repository.connection.execute(
        "SELECT source_run_id, parent_run_id, depth FROM runs ORDER BY source_run_id"
    ).fetchall()
    assert run_rows == [
        ("child-session", "parent-session", 1),
        ("parent-session", None, 0),
    ]
    child_event = repository.get_event("codex:child-session:0")
    assert child_event is not None
    assert child_event.session_id == repository.connection.execute(
        "SELECT id FROM sessions WHERE source = 'codex' AND source_session_id = 'parent-session'"
    ).fetchone()[0]


def test_thread_history_supplies_turn_timing_only(
    repository: Repository, tmp_path: Path, codex_fixtures: Path
) -> None:
    roots = _roots(tmp_path)
    _copy(codex_fixtures / "basic.jsonl", roots.sessions / "parent.jsonl")
    _create_thread_history_db(roots.thread_history_db)

    sync_codex(repository, roots)

    row = repository.connection.execute(
        "SELECT source_turn_id, started_at_ns, completed_at_ns, duration_ns, status FROM turns"
    ).fetchone()
    assert row == (
        "turn-1",
        1_700_000_001_000_000_000,
        1_700_000_003_000_000_000,
        2_000_000_000,
        "completed",
    )


def test_missing_owner_records_content_free_issue(
    repository: Repository, tmp_path: Path
) -> None:
    roots = _roots(tmp_path)
    rollout = roots.sessions / "orphan.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-28T00:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {"input_tokens": 9},
                        "last_token_usage": {"input_tokens": 9},
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    sync_codex(repository, roots)

    issue = repository.connection.execute(
        "SELECT category, severity, field_path, observed_type FROM issues"
    ).fetchone()
    assert issue == ("missing_owner", "error", "0", "token_count")
    assert repository.event_count("codex") == 0


def test_state_thread_totals_are_never_imported(
    repository: Repository, tmp_path: Path
) -> None:
    roots = _roots(tmp_path)
    roots.sessions.mkdir(parents=True)
    _create_state_db(roots.state_db, tokens_used=999_999)

    sync_codex(repository, roots)

    assert repository.source_total("codex").tokens == TokenUsage()
