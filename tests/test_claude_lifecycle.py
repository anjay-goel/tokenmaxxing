import json
import sqlite3
from pathlib import Path

import pytest

import tokenmaxxing.ingest.claude.reconcile as claude_import
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
from claude_support import (
    _assistant,
    _usage,
    _write_jsonl,
)


@pytest.fixture
def claude_fixtures() -> Path:
    return Path(__file__).parent / "fixtures" / "claude"


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

    connection = repository.connection
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
    assert repository.connection.execute(
        "SELECT status FROM usage_events WHERE source = 'claude'"
    ).fetchall() == [("provisional",)]

    monkeypatch.setattr(claude_import, "_rebuild_messages", rebuild)
    recovery = sync_claude(repository, claude_fixtures / "progressive.jsonl")

    assert recovery.lines_read == 0
    assert repository.source_total("claude").tokens.output == 21
    assert repository.connection.execute(
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
    connection = repository.connection
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
    connection = repository.connection
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
    repository.connection.execute(
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
    repository.connection.execute(
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
    high_artifact = repository.connection.execute(
        "SELECT artifact_id FROM observations "
        "WHERE source = 'claude' AND projection_json LIKE '%\"output_tokens\":9%'"
    ).fetchone()
    assert high_artifact is not None
    repository.connection.execute(
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
    high_artifact = repository.connection.execute(
        "SELECT artifact_id FROM observations "
        "WHERE source = 'claude' AND projection_json LIKE '%\"output_tokens\":9%'"
    ).fetchone()
    assert high_artifact is not None
    repository.connection.execute(
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
    repository.connection.execute(
        "UPDATE artifacts SET is_missing = 1 WHERE source = 'claude'"
    )
    connection = repository.connection
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
