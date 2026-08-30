import json
import sqlite3
from pathlib import Path

import pytest

import tokenmaxxing.ingest.claude as claude_import
from tokenmaxxing.ingest.claude import sync_claude
from tokenmaxxing.models import (
    LinkDraft,
    ObservationDraft,
    Projection,
    TokenUsage,
    UsageEventDraft,
    WriteStats,
)
from tokenmaxxing.repository import Repository


@pytest.fixture
def claude_fixtures() -> Path:
    return Path(__file__).parent / "fixtures" / "claude"


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def _usage(*, input_tokens: int = 1, output_tokens: int = 1) -> dict[str, object]:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def _assistant(
    message_id: str | None,
    uuid: str,
    *,
    session_id: str = "test-session",
    usage: object | None = None,
    iterations: list[dict[str, object]] | None = None,
    sidechain: bool = False,
) -> dict[str, object]:
    usage_value = _usage() if usage is None else usage
    if iterations is not None and isinstance(usage_value, dict):
        usage_value = {**usage_value, "iterations": iterations}
    message: dict[str, object] = {
        "model": "base-model",
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "PRIVATE_SENTINEL"}],
        "usage": usage_value,
    }
    if message_id is not None:
        message["id"] = message_id
    return {
        "timestamp": "2026-08-28T00:00:00Z",
        "type": "assistant",
        "sessionId": session_id,
        "uuid": uuid,
        "requestId": f"req-{uuid}",
        "version": "2.1.232",
        "entrypoint": "cli",
        "isSidechain": sidechain,
        "effort": "high",
        "message": message,
    }


def test_progressive_snapshots_count_component_max_once(
    repository: Repository, claude_fixtures: Path
) -> None:
    sync_claude(repository, claude_fixtures / "progressive.jsonl")

    assert repository.observation_count("claude") == 3
    assert repository.event_count("claude") == 1
    assert repository.source_total("claude").tokens == TokenUsage(
        input=2,
        output=21,
        cache_read=20,
        cache_write=3,
        cache_write_5m=1,
        cache_write_1h=2,
    )


def test_rebuild_messages_chunks_under_sqlite_variable_limit(
    repository: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "many-messages.jsonl"
    message_ids = {f"message-{index:03d}" for index in range(51)}
    _write_jsonl(
        path,
        [
            _assistant(message_id, f"entry-{index}", usage=_usage(output_tokens=1))
            for index, message_id in enumerate(sorted(message_ids))
        ],
    )
    rebuild = claude_import._rebuild_messages
    monkeypatch.setattr(claude_import, "_rebuild_messages", lambda *_: WriteStats())
    sync_claude(repository, path)
    monkeypatch.setattr(claude_import, "_rebuild_messages", rebuild)

    connection = repository._database.connection
    if not hasattr(connection, "getlimit") or not hasattr(connection, "setlimit"):
        pytest.skip("sqlite variable limits are unavailable")
    variable_limit = sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER
    original_limit = connection.getlimit(variable_limit)
    if original_limit < 51:
        pytest.skip("sqlite variable limit is already too low for this regression")
    connection.setlimit(variable_limit, 50)
    try:
        claude_import._rebuild_messages(repository, message_ids)
    finally:
        connection.setlimit(variable_limit, original_limit)

    assert repository.source_total("claude").tokens.output == 51


def test_iterations_replace_outer_and_keep_advisor_model(
    repository: Repository, claude_fixtures: Path
) -> None:
    sync_claude(repository, claude_fixtures / "iterations.jsonl")

    assert repository.list_event_keys("claude") == {
        "claude:msg_1:iteration:0",
        "claude:msg_1:iteration:1",
    }
    advisor = repository.get_event("claude:msg_1:iteration:1")
    assert advisor is not None
    assert advisor.model == "advisor-model"
    assert repository.source_total("claude").tokens.output == 19


def test_residual_counts_only_unexplained_positive_outer_usage(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "residual.jsonl"
    _write_jsonl(
        path,
        [
            _assistant(
                "msg_residual",
                "entry-residual",
                usage=_usage(input_tokens=2, output_tokens=15),
                iterations=[
                    {"type": "message", **_usage(input_tokens=2, output_tokens=10)},
                    {
                        "type": "advisor_message",
                        "model": "advisor-model",
                        **_usage(input_tokens=20, output_tokens=7),
                    },
                ],
            )
        ],
    )

    sync_claude(repository, path)

    residual = repository.get_event("claude:msg_residual:residual")
    assert residual is not None
    assert residual.tokens == TokenUsage(
        input=0,
        output=5,
        cache_read=0,
        cache_write=0,
        cache_write_5m=0,
        cache_write_1h=0,
    )
    assert repository.source_total("claude").tokens.output == 22


def test_negative_outer_minus_normal_iteration_is_conflicted(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "conflict.jsonl"
    _write_jsonl(
        path,
        [
            _assistant(
                "msg_conflict",
                "entry-conflict",
                usage=_usage(input_tokens=1, output_tokens=5),
                iterations=[
                    {"type": "message", **_usage(input_tokens=1, output_tokens=10)},
                    {
                        "type": "advisor_message",
                        "model": "advisor-model",
                        **_usage(input_tokens=20, output_tokens=3),
                    },
                ],
            )
        ],
    )

    sync_claude(repository, path)

    normal = repository.get_event("claude:msg_conflict:iteration:0")
    advisor = repository.get_event("claude:msg_conflict:iteration:1")
    assert normal is not None and normal.status == "conflicted"
    assert advisor is not None and advisor.status == "canonical"
    assert repository.source_total("claude").tokens.output == 3
    assert repository._database.connection.execute(
        "SELECT category FROM issues WHERE source = 'claude'"
    ).fetchone() == ("iteration_usage_conflict",)


def test_usage_metadata_is_preserved_without_private_source_fields(
    repository: Repository, db_path: Path, claude_fixtures: Path
) -> None:
    sync_claude(repository, claude_fixtures / "progressive.jsonl")

    event = repository.get_event("claude:msg_progressive")
    assert event is not None
    assert event.service_tier == "standard"
    assert event.speed == "fast"
    assert event.inference_region == "us"
    assert event.effort == "high"
    assert event.web_search_count == 1
    assert event.web_fetch_count == 2
    assert repository._database.connection.execute(
        "SELECT request_id FROM usage_events WHERE source = 'claude'"
    ).fetchone() == ("req-progressive",)
    assert repository._database.connection.execute(
        "SELECT harness_version FROM sessions WHERE source = 'claude'"
    ).fetchone() == ("2.1.232",)
    assert repository._database.connection.execute(
        "SELECT event_type, client_id FROM observations WHERE source = 'claude' "
        "ORDER BY ordinal DESC LIMIT 1"
    ).fetchone() == ("assistant", "cli")
    database_files = (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
    )
    database_bytes = b"".join(
        path.read_bytes() for path in database_files if path.exists()
    )
    assert b"PRIVATE_PROGRESSIVE_SENTINEL" not in database_bytes
    assert str(claude_fixtures).encode() not in database_bytes


def test_unknown_numeric_usage_metadata_is_kept_but_strings_are_dropped(
    repository: Repository, db_path: Path, tmp_path: Path
) -> None:
    path = tmp_path / "extended-usage.jsonl"
    record = _assistant("msg_extended", "entry-extended")
    assert isinstance(record["message"], dict)
    assert isinstance(record["message"]["usage"], dict)
    record["message"]["usage"].update(
        {
            "new_counter": 7,
            "new_flags": {"accelerated": True},
            "new_label": "PRIVATE_ARBITRARY_STRING",
        }
    )
    _write_jsonl(path, [record])

    sync_claude(repository, path)

    projection = repository._database.connection.execute(
        "SELECT projection_json FROM observations WHERE source = 'claude'"
    ).fetchone()
    assert projection is not None
    assert '"new_counter":7' in projection[0]
    assert '"accelerated":true' in projection[0]
    database_files = (
        db_path,
        Path(f"{db_path}-wal"),
        Path(f"{db_path}-shm"),
    )
    database_bytes = b"".join(
        database_file.read_bytes()
        for database_file in database_files
        if database_file.exists()
    )
    assert b"PRIVATE_ARBITRARY_STRING" not in database_bytes


def test_repeat_sync_is_a_true_noop(
    repository: Repository, claude_fixtures: Path
) -> None:
    sync_claude(repository, claude_fixtures / "progressive.jsonl")

    repeated = sync_claude(repository, claude_fixtures / "progressive.jsonl")

    assert repeated.lines_read == 0
    assert repeated.observations_inserted == 0
    assert repeated.events_inserted == 0
    assert repeated.events_updated == 0


def test_incremental_append_updates_the_existing_message(
    repository: Repository, tmp_path: Path, claude_fixtures: Path
) -> None:
    source_lines = (claude_fixtures / "progressive.jsonl").read_bytes().splitlines(keepends=True)
    path = tmp_path / "progressive.jsonl"
    path.write_bytes(source_lines[0])
    first = sync_claude(repository, path)
    path.write_bytes(path.read_bytes() + b"".join(source_lines[1:]))

    appended = sync_claude(repository, path)

    assert first.lines_read == 1
    assert appended.lines_read == 2
    assert repository.event_count("claude") == 1
    assert repository.source_total("claude").tokens.output == 21


def test_root_and_subagent_copies_canonicalize_globally(
    repository: Repository, tmp_path: Path, claude_fixtures: Path
) -> None:
    root = tmp_path / "project"
    _copy(claude_fixtures / "root.jsonl", root / "root.jsonl")
    _copy(
        claude_fixtures / "subagents" / "agent-a.jsonl",
        root / "subagents" / "agent-a.jsonl",
    )

    sync_claude(repository, root)

    assert repository.observation_count("claude") == 3
    assert repository.list_event_keys("claude") == {
        "claude:msg_shared",
        "claude:msg_agent",
    }
    assert repository.source_total("claude").tokens.output == 15
    assert repository._database.connection.execute(
        "SELECT event_type, client_id FROM observations "
        "WHERE source = 'claude' AND response_id = 'msg_agent'"
    ).fetchone() == ("assistant_sidechain", "cli")


def test_missing_message_id_falls_back_to_session_and_transcript_uuid(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "fallback.jsonl"
    _write_jsonl(
        path,
        [_assistant(None, "entry-fallback", session_id="fallback-session")],
    )

    sync_claude(repository, path)

    assert repository.list_event_keys("claude") == {
        "claude:fallback-session:entry-fallback"
    }


def test_malformed_usage_records_issue_and_does_not_block_later_lines(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "malformed.jsonl"
    malformed = _assistant("msg_bad", "entry-bad")
    assert isinstance(malformed["message"], dict)
    malformed["message"]["usage"] = "PRIVATE_MALFORMED_SENTINEL"
    _write_jsonl(
        path,
        [
            _assistant("msg_before", "entry-before"),
            malformed,
            _assistant("msg_after", "entry-after"),
        ],
    )

    sync_claude(repository, path)

    assert repository.list_event_keys("claude") == {
        "claude:msg_before",
        "claude:msg_after",
    }
    assert repository._database.connection.execute(
        "SELECT category, observed_type FROM issues WHERE source = 'claude'"
    ).fetchone() == ("invalid_usage", "str")


def test_replacement_excludes_events_removed_from_the_current_generation(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "replacement.jsonl"
    _write_jsonl(path, [_assistant("msg_old", "entry-old", usage=_usage(output_tokens=9))])
    sync_claude(repository, path)
    replacement = tmp_path / "new.jsonl"
    _write_jsonl(
        replacement,
        [_assistant("msg_new", "entry-new", usage=_usage(output_tokens=4))],
    )
    replacement.replace(path)

    sync_claude(repository, path)

    old = repository.get_event("claude:msg_old")
    new = repository.get_event("claude:msg_new")
    assert old is not None and old.status == "excluded"
    assert new is not None and new.status == "canonical"
    assert repository.source_total("claude").tokens.output == 4


def test_truncation_excludes_removed_events_and_rebuilds_from_zero(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "truncated.jsonl"
    _write_jsonl(
        path,
        [
            _assistant("msg_first", "entry-first", usage=_usage(output_tokens=9)),
            _assistant("msg_second", "entry-second", usage=_usage(output_tokens=8)),
        ],
    )
    sync_claude(repository, path)
    _write_jsonl(
        path,
        [_assistant("msg_short", "entry-short", usage=_usage(output_tokens=3))],
    )

    sync_claude(repository, path)

    first = repository.get_event("claude:msg_first")
    second = repository.get_event("claude:msg_second")
    assert first is not None and first.status == "excluded"
    assert second is not None and second.status == "excluded"
    assert repository.source_total("claude").tokens.output == 3


def test_sync_recovers_provisional_messages_after_interrupted_rebuild(
    repository: Repository,
    claude_fixtures: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuild = claude_import._rebuild_messages

    def interrupt_rebuild(_: Repository, __: set[str]) -> object:
        raise RuntimeError("interrupted rebuild")

    monkeypatch.setattr(claude_import, "_rebuild_messages", interrupt_rebuild)
    with pytest.raises(RuntimeError, match="interrupted rebuild"):
        sync_claude(repository, claude_fixtures / "progressive.jsonl")
    assert repository._database.connection.execute(
        "SELECT status FROM usage_events WHERE source = 'claude'"
    ).fetchall() == [("provisional",)]

    monkeypatch.setattr(claude_import, "_rebuild_messages", rebuild)
    recovery = sync_claude(repository, claude_fixtures / "progressive.jsonl")

    assert recovery.lines_read == 0
    assert repository.source_total("claude").tokens.output == 21
    assert repository._database.connection.execute(
        "SELECT status FROM usage_events WHERE source = 'claude'"
    ).fetchall() == [("canonical",)]


def test_provisional_message_recovery_starts_from_the_event_status_index(
    repository: Repository,
) -> None:
    repository.apply_projection(
        Projection(
            observations=(
                ObservationDraft(
                    source="claude",
                    channel="disk",
                    stable_key="provisional-observation",
                    event_type="assistant",
                    observed_at_ns=1,
                    parser_version="test",
                    projection={},
                    source_session_id="recovery-session",
                    source_turn_id="recovery-message",
                ),
            ),
            events=(
                UsageEventDraft(
                    source="claude",
                    event_key="claude:recovery-message",
                    granularity="model_call",
                    status="provisional",
                    tokens=TokenUsage(),
                ),
            ),
            links=(
                LinkDraft(
                    source="claude",
                    channel="disk",
                    observation_key="provisional-observation",
                    event_key="claude:recovery-message",
                    method="message_id",
                    role="primary",
                    confidence="exact",
                ),
            ),
        )
    )
    connection = repository._database.connection
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        assert claude_import._provisional_messages(repository) == {"recovery-message"}
    finally:
        connection.set_trace_callback(None)

    query = next(
        statement
        for statement in statements
        if statement.startswith("SELECT DISTINCT o.source_turn_id FROM ")
    )
    plan = connection.execute(f"EXPLAIN QUERY PLAN {query}").fetchall()

    details = [str(row[3]) for row in plan]
    assert "SEARCH e USING" in details[0] and "idx_usage_events_status" in details[0]
    assert (
        "SEARCH l USING" in details[1]
        and "idx_observation_links_usage_event" in details[1]
    )
    assert "SEARCH o USING INTEGER PRIMARY KEY" in details[2]


def test_linked_event_recovery_starts_from_target_claude_message(
    repository: Repository,
) -> None:
    repository.apply_projection(
        Projection(
            observations=(
                ObservationDraft(
                    source="claude",
                    channel="disk",
                    stable_key="target-observation",
                    event_type="assistant",
                    observed_at_ns=1,
                    parser_version="test",
                    projection={},
                    source_turn_id="target-message",
                ),
            ),
            events=(
                UsageEventDraft(
                    source="claude",
                    event_key="claude:target-message",
                    granularity="model_call",
                    status="canonical",
                    tokens=TokenUsage(),
                    model="base-model",
                ),
            ),
            links=(
                LinkDraft(
                    source="claude",
                    channel="disk",
                    observation_key="target-observation",
                    event_key="claude:target-message",
                    method="message_id",
                    role="primary",
                    confidence="exact",
                ),
            ),
        )
    )
    connection = repository._database.connection
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        linked = claude_import._linked_events(repository, "target-message")
    finally:
        connection.set_trace_callback(None)

    assert linked == {"claude:target-message": (1, "base-model")}
    query = next(
        statement
        for statement in statements
        if statement.startswith("SELECT DISTINCT e.id, e.event_key, e.model FROM ")
    )
    details = [str(row[3]) for row in connection.execute(f"EXPLAIN QUERY PLAN {query}")]

    assert "SEARCH o USING COVERING INDEX idx_observations_source_turn" in details[0]
    assert "SEARCH l USING COVERING INDEX sqlite_autoindex_observation_links_1" in details[1]
    assert "SEARCH e USING INTEGER PRIMARY KEY" in details[2]


def test_sync_recovers_removed_message_after_interrupted_replacement(
    repository: Repository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "replacement-recovery.jsonl"
    _write_jsonl(
        path,
        [_assistant("msg_removed", "entry-removed", usage=_usage(output_tokens=9))],
    )
    sync_claude(repository, path)
    replacement = tmp_path / "replacement-new.jsonl"
    _write_jsonl(
        replacement,
        [_assistant("msg_current", "entry-current", usage=_usage(output_tokens=4))],
    )
    replacement.replace(path)
    rebuild = claude_import._rebuild_messages

    def interrupt_rebuild(_: Repository, __: set[str]) -> object:
        raise RuntimeError("interrupted rebuild")

    monkeypatch.setattr(claude_import, "_rebuild_messages", interrupt_rebuild)
    with pytest.raises(RuntimeError, match="interrupted rebuild"):
        sync_claude(repository, path)
    monkeypatch.setattr(claude_import, "_rebuild_messages", rebuild)

    recovery = sync_claude(repository, path)

    removed = repository.get_event("claude:msg_removed")
    assert recovery.lines_read == 0
    assert removed is not None and removed.status == "excluded"
    assert repository.source_total("claude").tokens.output == 4


def test_missing_artifact_is_excluded_and_rediscovery_restores_it(
    repository: Repository, tmp_path: Path
) -> None:
    root = tmp_path / "project"
    path = root / "session.jsonl"
    _write_jsonl(
        path,
        [_assistant("msg_missing", "entry-missing", usage=_usage(output_tokens=9))],
    )
    sync_claude(repository, root)
    path.unlink()
    repository._database.connection.execute(
        "UPDATE artifacts SET is_missing = 1 WHERE source = 'claude'"
    )

    sync_claude(repository, root)

    missing = repository.get_event("claude:msg_missing")
    assert missing is not None and missing.status == "excluded"
    _write_jsonl(
        path,
        [_assistant("msg_missing", "entry-missing", usage=_usage(output_tokens=9))],
    )

    restored = sync_claude(repository, root)

    event = repository.get_event("claude:msg_missing")
    assert restored.lines_read == 1
    assert event is not None and event.status == "canonical"
    assert repository.source_total("claude").tokens.output == 9


def test_same_inode_rediscovery_recovers_after_interrupted_rebuild(
    repository: Repository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "project"
    path = root / "session.jsonl"
    parked = tmp_path / "parked.jsonl"
    _write_jsonl(
        path,
        [_assistant("msg_rediscovered", "entry-rediscovered", usage=_usage(output_tokens=9))],
    )
    sync_claude(repository, root)
    inode = path.stat().st_ino
    path.replace(parked)
    assert parked.stat().st_ino == inode
    repository._database.connection.execute(
        "UPDATE artifacts SET is_missing = 1 WHERE source = 'claude'"
    )
    sync_claude(repository, root)
    parked.replace(path)
    assert path.stat().st_ino == inode
    rebuild = claude_import._rebuild_messages

    def interrupt_rebuild(_: Repository, __: set[str]) -> object:
        raise RuntimeError("interrupted rebuild")

    monkeypatch.setattr(claude_import, "_rebuild_messages", interrupt_rebuild)
    with pytest.raises(RuntimeError, match="interrupted rebuild"):
        sync_claude(repository, root)
    monkeypatch.setattr(claude_import, "_rebuild_messages", rebuild)

    recovered = sync_claude(repository, root)
    repeated = sync_claude(repository, root)

    event = repository.get_event("claude:msg_rediscovered")
    assert recovered.lines_read == 0
    assert event is not None and event.status == "canonical"
    assert repository.source_total("claude").tokens.output == 9
    assert repeated.events_updated == 0


def test_missing_higher_copy_recomputes_from_the_surviving_global_message(
    repository: Repository, tmp_path: Path
) -> None:
    root = tmp_path / "project"
    high = root / "a-high.jsonl"
    low = root / "b-low.jsonl"
    _write_jsonl(
        high,
        [_assistant("msg_copied", "entry-high", usage=_usage(output_tokens=9))],
    )
    _write_jsonl(
        low,
        [_assistant("msg_copied", "entry-low", usage=_usage(output_tokens=4))],
    )
    sync_claude(repository, root)
    high.unlink()
    high_artifact = repository._database.connection.execute(
        "SELECT artifact_id FROM observations "
        "WHERE source = 'claude' AND projection_json LIKE '%\"output_tokens\":9%'"
    ).fetchone()
    assert high_artifact is not None
    repository._database.connection.execute(
        "UPDATE artifacts SET is_missing = 1 WHERE id = ?",
        (high_artifact[0],),
    )

    sync_claude(repository, root)
    repeated = sync_claude(repository, root)

    assert repository.source_total("claude").tokens.output == 4
    assert repeated.lines_read == 0
    assert repeated.events_updated == 0


def test_stale_duplicate_message_falls_back_after_deletion_and_recovers_on_reappearance(
    repository: Repository, tmp_path: Path
) -> None:
    root = tmp_path / "project"
    high = root / "a-high.jsonl"
    low = root / "b-low.jsonl"
    _write_jsonl(
        high,
        [_assistant("msg_copied", "entry-high", usage=_usage(output_tokens=9))],
    )
    _write_jsonl(
        low,
        [_assistant("msg_copied", "entry-low", usage=_usage(output_tokens=4))],
    )
    sync_claude(repository, root)

    high.unlink()
    high_artifact = repository._database.connection.execute(
        "SELECT artifact_id FROM observations "
        "WHERE source = 'claude' AND projection_json LIKE '%\"output_tokens\":9%'"
    ).fetchone()
    assert high_artifact is not None
    repository._database.connection.execute(
        "UPDATE artifacts SET is_missing = 1 WHERE id = ?",
        (high_artifact[0],),
    )

    sync_claude(repository, root)
    assert repository.source_total("claude").tokens.output == 4

    _write_jsonl(
        high,
        [_assistant("msg_copied", "entry-high", usage=_usage(output_tokens=9))],
    )
    sync_claude(repository, root)

    assert repository.source_total("claude").tokens.output == 9


def test_stale_message_recovery_uses_the_claude_turn_artifact_index(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "session.jsonl"
    _write_jsonl(
        path,
        [_assistant("msg_stale", "entry-stale", usage=_usage(output_tokens=9))],
    )
    sync_claude(repository, path)
    repository._database.connection.execute(
        "UPDATE artifacts SET is_missing = 1 WHERE source = 'claude'"
    )
    connection = repository._database.connection
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        assert claude_import._stale_counted_messages(repository) == {"msg_stale"}
    finally:
        connection.set_trace_callback(None)

    query = next(
        statement
        for statement in statements
        if statement.startswith("SELECT DISTINCT old.source_turn_id FROM ")
    )
    details = [str(row[3]) for row in connection.execute(f"EXPLAIN QUERY PLAN {query}")]

    expected = (
        "USING COVERING INDEX idx_observations_claude_turn_artifact "
        "(source=? AND channel=? AND source_turn_id=?)"
    )
    assert any("SEARCH live " in detail and expected in detail for detail in details)
    assert any("SEARCH missing " in detail and expected in detail for detail in details)


def test_replacement_refreshes_canonical_string_metadata(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "metadata.jsonl"
    first = _assistant("msg_metadata", "entry-metadata")
    assert isinstance(first["message"], dict)
    assert isinstance(first["message"]["usage"], dict)
    first["message"]["model"] = "old-model"
    first["message"]["usage"].update(
        {"service_tier": "old-tier", "speed": "old-speed", "inference_geo": "old-geo"}
    )
    _write_jsonl(path, [first])
    sync_claude(repository, path)
    replacement = tmp_path / "metadata-new.jsonl"
    current = _assistant("msg_metadata", "entry-metadata")
    assert isinstance(current["message"], dict)
    assert isinstance(current["message"]["usage"], dict)
    current["message"]["model"] = "current-model"
    current["message"]["usage"].update(
        {
            "service_tier": "current-tier",
            "speed": "current-speed",
            "inference_geo": "current-geo",
        }
    )
    _write_jsonl(replacement, [current])
    replacement.replace(path)

    sync_claude(repository, path)

    event = repository.get_event("claude:msg_metadata")
    assert event is not None
    assert event.model == "current-model"
    assert event.service_tier == "current-tier"
    assert event.speed == "current-speed"
    assert event.inference_region == "current-geo"


def test_replacement_clears_metadata_omitted_by_the_current_generation(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "metadata-clear.jsonl"
    first = _assistant("msg_metadata_clear", "entry-metadata-clear")
    assert isinstance(first["message"], dict)
    assert isinstance(first["message"]["usage"], dict)
    first["message"]["model"] = "old-model"
    first["message"]["usage"]["service_tier"] = "old-tier"
    _write_jsonl(path, [first])
    sync_claude(repository, path)
    replacement = tmp_path / "metadata-clear-current.jsonl"
    current = _assistant("msg_metadata_clear", "entry-metadata-clear")
    assert isinstance(current["message"], dict)
    current["message"].pop("model")
    _write_jsonl(replacement, [current])
    replacement.replace(path)

    sync_claude(repository, path)

    event = repository.get_event("claude:msg_metadata_clear")
    assert event is not None
    assert event.model is None
    assert event.service_tier is None


def test_later_progressive_snapshot_preserves_cross_channel_enrichment(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "metadata-scope.jsonl"
    first = _assistant("msg_metadata_scope", "entry-metadata-scope-1")
    assert isinstance(first["message"], dict)
    assert isinstance(first["message"]["usage"], dict)
    first["message"]["model"] = "old-model"
    first["message"]["usage"]["service_tier"] = "old-tier"
    _write_jsonl(path, [first])
    sync_claude(repository, path)
    repository.apply_projection(
        Projection(
            events=(
                UsageEventDraft(
                    source="claude",
                    event_key="claude:msg_metadata_scope",
                    granularity="model_call",
                    status="canonical",
                    tokens=TokenUsage(),
                    api="messages",
                    response_model="resolved-model",
                    duration_ns=700,
                    ttft_ns=300,
                    status_code=200,
                ),
            )
        )
    )
    current = _assistant("msg_metadata_scope", "entry-metadata-scope-2")
    assert isinstance(current["message"], dict)
    current["message"].pop("model")
    with path.open("a", encoding="utf-8") as session_file:
        session_file.write(json.dumps(current, separators=(",", ":")) + "\n")

    sync_claude(repository, path)

    event = repository.get_event("claude:msg_metadata_scope")
    assert event is not None
    assert event.model is None
    assert event.service_tier is None
    assert event.api == "messages"
    assert event.response_model == "resolved-model"
    assert event.duration_ns == 700
    assert event.ttft_ns == 300
    assert event.status_code == 200
