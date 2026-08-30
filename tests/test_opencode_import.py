import json
import sqlite3
from pathlib import Path

import pytest

from tokenmaxxing.ingest.opencode import OpenCodeRoots, sync_opencode
from tokenmaxxing.repository import Repository


def _database_bytes(path: Path) -> bytes:
    return b"".join(
        candidate.read_bytes()
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"))
        if candidate.exists()
    )


def _source_database(tmp_path: Path) -> Path:
    path = tmp_path / "opencode.db"
    schema = (Path(__file__).parent / "fixtures" / "opencode" / "schema.sql").read_text(
        encoding="utf-8"
    )
    with sqlite3.connect(path) as connection:
        connection.executescript(schema)
    return path


def _session(
    connection: sqlite3.Connection,
    session_id: str,
    *,
    parent_id: str | None = None,
    version: str = "1.18.20",
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    connection.execute(
        "INSERT INTO session (id, parent_id, version, agent, time_created, time_updated, "
        "tokens_input, tokens_output) VALUES (?, ?, ?, 'build', 10, 20, ?, ?)",
        (session_id, parent_id, version, input_tokens, output_tokens),
    )


def _assistant_message(
    connection: sqlite3.Connection,
    message_id: str,
    session_id: str,
    *,
    provider: str = "openai",
    model: str = "gpt-5",
    agent: str = "build",
    forbidden: str | None = None,
) -> None:
    data: dict[str, object] = {
        "role": "assistant",
        "providerID": provider,
        "modelID": model,
        "agent": agent,
        "time": {"completed": 30},
    }
    if forbidden is not None:
        data["content"] = forbidden
    connection.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, 11, 30, ?)",
        (message_id, session_id, json.dumps(data)),
    )


def _step_finish(
    connection: sqlite3.Connection,
    part_id: str,
    message_id: str,
    session_id: str,
    *,
    input_tokens: int = 10,
    output_tokens: int = 4,
    reasoning_tokens: int = 2,
    cache_read: int = 3,
    cache_write: int = 1,
    reported_total: int | None = None,
    cost: float = 0.000000123,
    forbidden: str | None = None,
) -> None:
    data: dict[str, object] = {
        "type": "step-finish",
        "cost": cost,
        "tokens": {
            "input": input_tokens,
            "output": output_tokens,
            "reasoning": reasoning_tokens,
            "total": reported_total
            if reported_total is not None
            else input_tokens + output_tokens + reasoning_tokens + cache_read + cache_write,
            "cache": {"read": cache_read, "write": cache_write},
        },
    }
    if forbidden is not None:
        data["snapshot"] = forbidden
    connection.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, 12, 30, ?)",
        (part_id, message_id, session_id, json.dumps(data)),
    )


def _non_finish_part(
    connection: sqlite3.Connection, part_id: str, message_id: str, session_id: str
) -> None:
    connection.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
        "VALUES (?, ?, ?, 12, 30, ?)",
        (part_id, message_id, session_id, json.dumps({"type": "text", "text": "PRIVATE_TEXT"})),
    )


def _event_identity(repository: Repository, event_key: str) -> tuple[int | None, int | None]:
    row = repository.connection.execute(
        "SELECT session_id, run_id FROM usage_events WHERE source = 'opencode' "
        "AND event_key = ?",
        (event_key,),
    ).fetchone()
    assert row is not None
    return row


def _run_for_source_session(repository: Repository, source_session_id: str) -> tuple[int, str | None]:
    row = repository.connection.execute(
        "SELECT r.id, r.parent_run_id FROM runs r "
        "JOIN sessions s ON s.id = r.session_id "
        "WHERE s.source = 'opencode' AND s.source_session_id = ?",
        (source_session_id,),
    ).fetchone()
    assert row is not None
    return row


def test_completed_steps_are_canonical_usage_not_assistant_or_session_aggregates(
    repository: Repository, tmp_path: Path
) -> None:
    path = _source_database(tmp_path)
    with sqlite3.connect(path) as source:
        _session(source, "session-1", input_tokens=999, output_tokens=999)
        _assistant_message(source, "message-1", "session-1")
        _step_finish(source, "part-1", "message-1", "session-1", reported_total=99)
        _step_finish(
            source,
            "part-2",
            "message-1",
            "session-1",
            input_tokens=20,
            output_tokens=8,
            reasoning_tokens=5,
            cache_read=7,
            cache_write=2,
            reported_total=42,
            cost=0.000000456,
        )

    sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))

    assert repository.list_event_keys("opencode") == {"opencode:part:part-1", "opencode:part:part-2"}
    event = repository.get_event("opencode:part:part-1")
    assert event is not None
    assert event.tokens.output == 6
    assert event.tokens.reasoning == 2
    assert event.tokens.reported_total == 99
    assert event.tokens.derived_total == 20
    assert event.cost.source == "opencode_reported_estimate"
    assert event.cost.estimated is True
    assert event.provider == "openai"
    assert event.model == "gpt-5"
    assert repository.source_total("opencode").tokens.input == 30


def test_no_finish_assistant_has_one_fallback_and_completed_step_suppresses_it(
    repository: Repository, tmp_path: Path
) -> None:
    path = _source_database(tmp_path)
    with sqlite3.connect(path) as source:
        _session(source, "session-1")
        _assistant_message(source, "message-no-finish", "session-1")
        _non_finish_part(source, "part-text", "message-no-finish", "session-1")
        _assistant_message(source, "message-with-finish", "session-1")
        _step_finish(source, "part-finish", "message-with-finish", "session-1")

    sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))

    assert repository.list_event_keys("opencode") == {
        "opencode:message:message-no-finish",
        "opencode:part:part-finish",
    }
    fallback = repository.get_event("opencode:message:message-no-finish")
    assert fallback is not None
    assert fallback.tokens.input == 0


def test_finite_fractional_nanodollar_cost_is_rounded_and_retained(
    repository: Repository, tmp_path: Path
) -> None:
    path = _source_database(tmp_path)
    with sqlite3.connect(path) as source:
        _session(source, "session-1")
        _assistant_message(source, "message-1", "session-1")
        _step_finish(source, "part-1", "message-1", "session-1", cost=0.0000000006)

    sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))

    event = repository.get_event("opencode:part:part-1")
    assert event is not None
    assert event.cost.total_nanos == 1
    assert event.cost.source == "opencode_reported_estimate"


def test_exact_below_half_nanodollar_cost_is_not_rounded_up(
    repository: Repository, tmp_path: Path
) -> None:
    path = _source_database(tmp_path)
    with sqlite3.connect(path) as source:
        _session(source, "session-1")
        _assistant_message(source, "message-1", "session-1")
        source.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
            "VALUES ('part-1', 'message-1', 'session-1', 12, 30, ?)",
            (
                "{\"type\":\"step-finish\",\"cost\":0.000000000499999999999999999,"
                "\"tokens\":{\"input\":10,\"output\":4,\"reasoning\":2,\"total\":20,"
                "\"cache\":{\"read\":3,\"write\":1}}}",
            ),
        )

    sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))

    event = repository.get_event("opencode:part:part-1")
    assert event is not None
    assert event.cost.total_nanos == 0


def test_missing_reported_total_remains_derived(
    repository: Repository, tmp_path: Path
) -> None:
    path = _source_database(tmp_path)
    with sqlite3.connect(path) as source:
        _session(source, "session-1")
        _assistant_message(source, "message-1", "session-1")
        source.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) "
            "VALUES ('part-1', 'message-1', 'session-1', 12, 30, ?)",
            (
                json.dumps(
                    {
                        "type": "step-finish",
                        "tokens": {
                            "input": 10,
                            "output": 4,
                            "reasoning": 2,
                            "cache": {"read": 3, "write": 1},
                        },
                    }
                ),
            ),
        )

    sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))

    event = repository.get_event("opencode:part:part-1")
    assert event is not None
    assert event.tokens.reported_total is None
    assert event.tokens.derived_total == 20
    assert repository.source_total("opencode").tokens.derived_total == 20


def test_importer_never_retains_forbidden_source_content(
    repository: Repository, db_path: Path, tmp_path: Path
) -> None:
    path = _source_database(tmp_path)
    with sqlite3.connect(path) as source:
        _session(source, "session-1")
        _assistant_message(
            source,
            "message-1",
            "session-1",
            forbidden="PRIVATE_OPENCODE_MESSAGE_SENTINEL",
        )
        _step_finish(
            source,
            "part-1",
            "message-1",
            "session-1",
            forbidden="PRIVATE_OPENCODE_PART_SENTINEL",
        )

    sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))

    database_bytes = _database_bytes(db_path)
    assert b"PRIVATE_OPENCODE_MESSAGE_SENTINEL" not in database_bytes
    assert b"PRIVATE_OPENCODE_PART_SENTINEL" not in database_bytes


def test_unsupported_schema_fails_without_reading_content(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "opencode.db"
    with sqlite3.connect(path) as source:
        source.execute("CREATE TABLE session (id TEXT PRIMARY KEY)")

    with pytest.raises(ValueError, match="unsupported OpenCode V1 schema"):
        sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))


def test_v2_session_version_is_rejected_before_source_data_is_read(
    repository: Repository, tmp_path: Path
) -> None:
    path = _source_database(tmp_path)
    with sqlite3.connect(path) as source:
        _session(source, "session-1", version="2")
        _assistant_message(source, "message-1", "session-1", forbidden="PRIVATE_V2_SENTINEL")

    with pytest.raises(ValueError, match="unsupported OpenCode V1 schema"):
        sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))

def test_same_source_is_a_noop_but_correction_replaces_usage_exactly(
    repository: Repository, tmp_path: Path
) -> None:
    path = _source_database(tmp_path)
    with sqlite3.connect(path) as source:
        _session(source, "session-1")
        _assistant_message(source, "message-1", "session-1")
        _step_finish(source, "part-1", "message-1", "session-1")

    first = sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))
    repeated = sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))
    with sqlite3.connect(path) as source:
        source.execute(
            "UPDATE part SET data = ? WHERE id = 'part-1'",
            (
                json.dumps(
                    {
                        "type": "step-finish",
                        "tokens": {
                            "input": 1,
                            "output": 1,
                            "reasoning": 0,
                            "total": 2,
                            "cache": {"read": 0, "write": 0},
                        },
                    }
                ),
            ),
        )

    corrected = sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))
    event = repository.get_event("opencode:part:part-1")
    assert first.events_inserted == 1
    assert repeated.events_inserted == 0
    assert repeated.events_updated == 0
    assert corrected.events_updated == 1
    assert event is not None
    assert event.tokens.input == 1
    assert event.tokens.output == 1
    assert event.tokens.reported_total == 2
    assert event.cost.total_nanos is None


def test_deleted_and_reappearing_parts_are_excluded_then_restored(
    repository: Repository, tmp_path: Path
) -> None:
    path = _source_database(tmp_path)
    with sqlite3.connect(path) as source:
        _session(source, "session-1")
        _assistant_message(source, "message-1", "session-1")
        _step_finish(source, "part-1", "message-1", "session-1")

    sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))
    with sqlite3.connect(path) as source:
        source.execute("DELETE FROM part WHERE id = 'part-1'")
    sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))

    missing = repository.get_event("opencode:part:part-1")
    assert missing is not None and missing.status == "excluded"
    with sqlite3.connect(path) as source:
        _step_finish(source, "part-1", "message-1", "session-1")
    restored = sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))

    event = repository.get_event("opencode:part:part-1")
    fallback = repository.get_event("opencode:message:message-1")
    assert restored.events_updated == 2
    assert event is not None and event.status == "canonical"
    assert fallback is not None and fallback.status == "excluded"


def test_child_and_nested_session_calls_retain_physical_runs_without_double_counting(
    repository: Repository, tmp_path: Path
) -> None:
    path = _source_database(tmp_path)
    with sqlite3.connect(path) as source:
        _session(source, "root")
        _session(source, "child", parent_id="root", input_tokens=999, output_tokens=999)
        _session(source, "grandchild", parent_id="child")
        _assistant_message(source, "root-message", "root")
        _step_finish(source, "root-part", "root-message", "root", reported_total=11)
        _assistant_message(source, "child-message", "child")
        _step_finish(source, "child-part", "child-message", "child", reported_total=22)
        _assistant_message(source, "child-fallback", "child")
        _assistant_message(source, "grandchild-message", "grandchild")
        _step_finish(
            source,
            "grandchild-part",
            "grandchild-message",
            "grandchild",
            reported_total=33,
        )

    first = sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))
    repeated = sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))

    root_database_id = repository.connection.execute(
        "SELECT id FROM sessions WHERE source = 'opencode' AND source_session_id = 'root'"
    ).fetchone()[0]
    child_run = _run_for_source_session(repository, "child")
    grandchild_run = _run_for_source_session(repository, "grandchild")

    assert _event_identity(repository, "opencode:part:root-part") == (
        root_database_id,
        None,
    )
    assert {
        _event_identity(repository, event_key)
        for event_key in (
            "opencode:part:child-part",
            "opencode:message:child-fallback",
        )
    } == {(root_database_id, child_run[0])}
    assert _event_identity(repository, "opencode:part:grandchild-part") == (
        root_database_id,
        grandchild_run[0],
    )
    assert child_run[1] == "root"
    assert grandchild_run[1] == "child"
    assert repository.event_count("opencode") == 4
    assert repository.source_total("opencode").tokens.reported_total == 66
    assert first.events_inserted == 4
    assert repeated.events_inserted == 0
    assert repeated.events_updated == 0


def test_legacy_child_event_without_run_is_repaired_on_normal_sync(
    repository: Repository, tmp_path: Path
) -> None:
    path = _source_database(tmp_path)
    with sqlite3.connect(path) as source:
        _session(source, "root")
        _session(source, "child", parent_id="root")
        _assistant_message(source, "child-message", "child")
        _step_finish(source, "child-part", "child-message", "child", reported_total=77)

    sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))
    event_key = "opencode:part:child-part"
    before = repository.get_event(event_key)
    assert before is not None
    child_run_id = _run_for_source_session(repository, "child")[0]
    repository.connection.execute(
        "UPDATE usage_events SET run_id = NULL WHERE source = 'opencode' AND event_key = ?",
        (event_key,),
    )

    repaired = sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))

    after = repository.get_event(event_key)
    assert after is not None
    assert after.event_key == before.event_key
    assert after.tokens == before.tokens
    assert after.session_id == before.session_id
    assert after.run_id == child_run_id
    assert repository.event_count("opencode") == 1
    assert repository.source_total("opencode").tokens.reported_total == 77
    assert repaired.events_inserted == 0
    assert repaired.events_updated == 1


def test_active_wal_rows_are_visible_to_the_read_only_importer(
    repository: Repository, tmp_path: Path
) -> None:
    data_dir = tmp_path / "OpenCode Data"
    data_dir.mkdir()
    path = _source_database(data_dir)
    source = sqlite3.connect(path, isolation_level=None)
    source.execute("PRAGMA journal_mode = WAL")
    _session(source, "session-1")
    _assistant_message(source, "message-1", "session-1")
    _step_finish(source, "part-1", "message-1", "session-1")

    sync_opencode(repository, OpenCodeRoots.from_data_dir(path.parent))

    assert repository.get_event("opencode:part:part-1") is not None
    assert Path(f"{path}-wal").exists()
    source.close()
