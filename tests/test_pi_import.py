import json
from pathlib import Path

import pytest

import tokenmaxxing.ingest.pi as pi_import
from tokenmaxxing.ingest.pi import sync_pi
from tokenmaxxing.models import Projection, TokenUsage, UsageEventDraft
from tokenmaxxing.repository import Repository


@pytest.fixture
def pi_fixtures() -> Path:
    return Path(__file__).parent / "fixtures" / "pi"


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
        encoding="utf-8",
    )


def _header(session_id: str, *, parent: str | None = None) -> dict[str, object]:
    value: dict[str, object] = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-08-28T00:00:00Z",
        "cwd": "PRIVATE_DYNAMIC_CWD_SENTINEL",
    }
    if parent is not None:
        value["parentSession"] = parent
    return value


def _usage(input_tokens: int, output_tokens: int) -> dict[str, object]:
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cacheRead": 0,
        "cacheWrite": 0,
        "reasoning": 0,
        "totalTokens": input_tokens + output_tokens,
        "cost": {
            "input": input_tokens / 1000,
            "output": output_tokens / 1000,
            "cacheRead": 0,
            "cacheWrite": 0,
            "total": (input_tokens + output_tokens) / 1000,
        },
    }


def _assistant(entry_id: str, input_tokens: int, output_tokens: int) -> dict[str, object]:
    return {
        "type": "message",
        "id": entry_id,
        "parentId": None,
        "timestamp": "2026-08-28T00:00:01Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "PRIVATE_DYNAMIC_ASSISTANT"}],
            "api": "responses",
            "provider": "test-provider",
            "model": "test-model",
            "responseId": f"response-{entry_id}",
            "usage": _usage(input_tokens, output_tokens),
            "stopReason": "stop",
            "timestamp": 1787875201000,
        },
    }


def _subagent_snapshot(
    entry_id: str,
    *,
    status: str,
    input_tokens: int,
    output_tokens: int,
    cost: float,
    timestamp: str,
    model: str,
    finished_at: int | None = None,
    thinking_level: str | None = None,
) -> dict[str, object]:
    data: dict[str, object] = {
        "id": "A001",
        "batchId": "batch-replaced",
        "status": status,
        "startedAt": 1787875200000,
        "model": model,
        "usage": {
            "input": input_tokens,
            "output": output_tokens,
            "cacheRead": 1,
            "cacheWrite": 0,
            "cost": cost,
        },
    }
    if finished_at is not None:
        data["finishedAt"] = finished_at
    if thinking_level is not None:
        data["thinkingLevel"] = thinking_level
    return {
        "type": "custom",
        "id": entry_id,
        "timestamp": timestamp,
        "customType": "orchestrator-subagent",
        "data": data,
    }


def _database_bytes(db_path: Path) -> bytes:
    return b"".join(
        path.read_bytes()
        for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"))
        if path.exists()
    )


def test_direct_usage_keeps_all_safe_metadata_without_retained_content(
    repository: Repository, db_path: Path, pi_fixtures: Path
) -> None:
    sync_pi(repository, pi_fixtures / "tree.jsonl")

    assert repository.list_event_keys("pi") == {
        "pi:root:assistant-1:assistant",
        "pi:root:tool-1:tool_result",
        "pi:root:compact-1:compaction",
        "pi:root:summary-1:branch_summary",
    }
    assert repository.source_total("pi").tokens == TokenUsage(
        input=25,
        output=15,
        cache_read=7,
        cache_write=4,
        cache_write_5m=2,
        cache_write_1h=1,
        reasoning=5,
        reported_total=51,
        derived_total=51,
    )
    assistant = repository.get_event("pi:root:assistant-1:assistant")
    assert assistant is not None
    assert assistant.provider == "test-provider"
    assert assistant.api == "responses"
    assert assistant.model == "test-model"
    assert assistant.response_model == "resolved-model"
    assert assistant.stop_reason == "stop"
    assert assistant.cost.total_nanos == 29_000_000
    assert repository._database.connection.execute(
        "SELECT response_id FROM usage_events WHERE event_key = ?",
        (assistant.event_key,),
    ).fetchone() == ("response-1",)
    assert repository._database.connection.execute(
        "SELECT value_integer FROM samples WHERE source = 'pi' AND name = 'tokens_before'"
    ).fetchone() == (1234,)
    assert repository._database.connection.execute(
        "SELECT current_model, reasoning_effort FROM sessions "
        "WHERE source_session_id = 'root'"
    ).fetchone() == ("test-model", "high")
    projection = repository._database.connection.execute(
        "SELECT projection_json FROM observations WHERE source = 'pi' "
        "AND event_type = 'assistant'"
    ).fetchone()
    assert projection is not None
    assert '"reasoning":2' in projection[0]
    assert '"futureCounter":7' in projection[0]
    assert '"futureFlag":true' in projection[0]
    database_bytes = _database_bytes(db_path)
    for sentinel in (
        b"PRIVATE_ASSISTANT_SENTINEL",
        b"PRIVATE_TOOL_RESULT_SENTINEL",
        b"PRIVATE_COMPACTION_SENTINEL",
        b"PRIVATE_BRANCH_SENTINEL",
        b"PRIVATE_USAGE_STRING_SENTINEL",
        b"RETAINED_SENTINEL",
        b"PRIVATE_CWD_SENTINEL",
    ):
        assert sentinel not in database_bytes
    assert str(pi_fixtures).encode() not in database_bytes


def test_pi_float_costs_are_rounded_to_the_nearest_nanodollar(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "precise-cost.jsonl"
    assistant = _assistant("precise-cost", 1, 1)
    message = assistant["message"]
    assert isinstance(message, dict)
    usage = message["usage"]
    assert isinstance(usage, dict)
    usage["cost"] = {
        "input": 0.0000000006,
        "output": 0.0000000004,
        "cacheRead": 0,
        "cacheWrite": 0,
        "total": 0.0000000006,
    }
    _write_jsonl(path, [_header("precise-cost"), assistant])

    sync_pi(repository, path)

    event = repository.get_event("pi:precise-cost:precise-cost:assistant")
    assert event is not None
    assert event.cost.input_nanos == 1
    assert event.cost.output_nanos == 0
    assert event.cost.total_nanos == 1
    assert event.cost.original_decimal == "6e-10"


def test_subagents_use_batch_identity_and_exclude_copied_aggregates(
    repository: Repository, db_path: Path, pi_fixtures: Path
) -> None:
    sync_pi(repository, pi_fixtures / "subagents.jsonl")

    assert repository.list_event_keys("pi") == {
        "pi:subagent:run-1:A001",
        "pi:subagent:run-2:A001",
        "pi:subagent:run-3:A002",
        "pi:subagent:legacy:sub-root:1787878806000:A003",
    }
    completed = repository.get_event("pi:subagent:run-1:A001")
    failed = repository.get_event("pi:subagent:run-2:A001")
    aborted = repository.get_event("pi:subagent:run-3:A002")
    assert completed is not None
    assert completed.granularity == "run_aggregate"
    assert completed.tokens == TokenUsage(
        input=40,
        output=10,
        cache_read=4,
        cache_write=2,
        reported_total=56,
        derived_total=56,
    )
    assert completed.cost.total_nanos == 56_000_000
    assert failed is not None and failed.tokens.input == 7
    assert aborted is not None and aborted.tokens.input == 3
    assert repository.source_total("pi").tokens.input == 55
    assert repository._database.connection.execute(
        "SELECT batch_id, role, status, model, provider, effort, isolation, duration_ns "
        "FROM runs WHERE source_run_id = 'run-2:A001'"
    ).fetchone() == (
        "run-2",
        "reviewer",
        "failed",
        "review-model",
        "test-provider",
        "medium",
        "worktree",
        1_000_000_000,
    )
    assert repository._database.connection.execute(
        "SELECT event_type FROM observations WHERE source = 'pi' ORDER BY id"
    ).fetchall().count(("subagent_batch",)) == 1
    database_bytes = _database_bytes(db_path)
    for sentinel in (
        b"PRIVATE_TASK_SENTINEL",
        b"PRIVATE_OUTPUT_SENTINEL",
        b"PRIVATE_ERROR_SENTINEL",
        b"PRIVATE_BACKGROUND_CONTENT",
        b"PRIVATE_BATCH_TOOL_CONTENT",
        b"PRIVATE_VIEW_OUTPUT",
    ):
        assert sentinel not in database_bytes


def test_late_nonterminal_subagent_snapshot_cannot_downgrade_terminal_metadata(
    repository: Repository, tmp_path: Path, pi_fixtures: Path
) -> None:
    path = tmp_path / "late-snapshot.jsonl"
    path.write_bytes((pi_fixtures / "subagents.jsonl").read_bytes())
    sync_pi(repository, path)
    terminal = repository.get_event("pi:subagent:run-1:A001")
    assert terminal is not None
    late_snapshot = {
        "type": "custom",
        "id": "late-queued-copy",
        "timestamp": "2026-08-28T01:00:10.000Z",
        "customType": "orchestrator-subagent",
        "data": {
            "id": "A001",
            "batchId": "run-1",
            "status": "queued",
            "startedAt": 1787878801000,
            "usage": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "cost": 0,
            },
        },
    }
    with path.open("a", encoding="utf-8") as session_file:
        session_file.write(json.dumps(late_snapshot, separators=(",", ":")) + "\n")

    sync_pi(repository, path)

    current = repository.get_event("pi:subagent:run-1:A001")
    assert current is not None
    assert current.tokens == terminal.tokens
    assert current.success is True
    assert current.completed_at_ns == terminal.completed_at_ns
    assert repository._database.connection.execute(
        "SELECT status FROM runs WHERE source_run_id = 'run-1:A001'"
    ).fetchone() == ("completed",)


def test_replacement_nonterminal_subagent_snapshots_clear_removed_terminal_metadata(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "replaced-subagent.jsonl"
    _write_jsonl(
        path,
        [
            _header("replaced-subagent"),
            _subagent_snapshot(
                "completed",
                status="completed",
                input_tokens=40,
                output_tokens=10,
                cost=0.2,
                timestamp="2026-08-28T00:00:01Z",
                model="old-provider/old-model",
                finished_at=1787875201000,
            ),
        ],
    )
    sync_pi(repository, path)
    replacement = tmp_path / "replacement.jsonl"
    _write_jsonl(
        replacement,
        [
            _header("replaced-subagent"),
            _subagent_snapshot(
                "queued",
                status="queued",
                input_tokens=0,
                output_tokens=0,
                cost=0,
                timestamp="2026-08-28T00:00:02Z",
                model="new-provider/new-model",
            ),
            _subagent_snapshot(
                "running",
                status="running",
                input_tokens=12,
                output_tokens=3,
                cost=0.1,
                timestamp="2026-08-28T00:00:03Z",
                model="new-provider/new-model",
            ),
        ],
    )
    replacement.replace(path)

    sync_pi(repository, path)

    event = repository.get_event("pi:subagent:batch-replaced:A001")
    assert event is not None
    assert event.tokens.input == 12
    assert event.tokens.output == 3
    assert event.success is None
    assert event.completed_at_ns is None
    assert event.error_category is None
    assert event.provider == "new-provider"
    assert event.model == "new-model"
    assert repository._database.connection.execute(
        "SELECT status, model, provider, completed_at_ns, duration_ns FROM runs "
        "WHERE source_run_id = 'batch-replaced:A001'"
    ).fetchone() == ("running", "new-model", "new-provider", None, None)


def test_subagent_replacement_preserves_cross_channel_fields_disk_does_not_own(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "enriched-subagent.jsonl"
    _write_jsonl(
        path,
        [
            _header("enriched-subagent"),
            _subagent_snapshot(
                "completed",
                status="completed",
                input_tokens=40,
                output_tokens=10,
                cost=0.2,
                timestamp="2026-08-28T00:00:01Z",
                model="old-provider/old-model",
                finished_at=1787875201000,
            ),
        ],
    )
    sync_pi(repository, path)
    event_key = "pi:subagent:batch-replaced:A001"
    current = repository.get_event(event_key)
    assert current is not None
    repository.apply_projection(
        Projection(
            events=(
                UsageEventDraft(
                    source="pi",
                    event_key=event_key,
                    granularity="run_aggregate",
                    status="canonical",
                    tokens=current.tokens,
                    cost=current.cost,
                    api="otel-api",
                    response_model="otel-response-model",
                ),
            )
        )
    )
    replacement = tmp_path / "enriched-replacement.jsonl"
    _write_jsonl(
        replacement,
        [
            _header("enriched-subagent"),
            _subagent_snapshot(
                "running",
                status="running",
                input_tokens=12,
                output_tokens=3,
                cost=0.1,
                timestamp="2026-08-28T00:00:02Z",
                model="new-provider/new-model",
            ),
        ],
    )
    replacement.replace(path)

    sync_pi(repository, path)

    event = repository.get_event(event_key)
    assert event is not None
    assert event.api == "otel-api"
    assert event.response_model == "otel-response-model"


def test_subagent_cost_decimal_tracks_the_snapshot_with_maximum_total(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "cost-provenance.jsonl"
    _write_jsonl(
        path,
        [
            _header("cost-provenance"),
            _subagent_snapshot(
                "higher-cost",
                status="running",
                input_tokens=10,
                output_tokens=2,
                cost=0.2,
                timestamp="2026-08-28T00:00:01Z",
                model="test-provider/test-model",
            ),
            _subagent_snapshot(
                "lower-cost-terminal",
                status="completed",
                input_tokens=20,
                output_tokens=4,
                cost=0.1,
                timestamp="2026-08-28T00:00:02Z",
                model="test-provider/test-model",
                finished_at=1787875202000,
            ),
        ],
    )

    sync_pi(repository, path)

    event = repository.get_event("pi:subagent:batch-replaced:A001")
    assert event is not None
    assert event.cost.total_nanos == 200_000_000
    assert event.cost.original_decimal == "0.2"
    assert event.cost.source == "pi_subagent_reported_estimate"
    assert event.cost.estimated is True


def test_repeat_sync_is_a_true_noop(
    repository: Repository, pi_fixtures: Path
) -> None:
    sync_pi(repository, pi_fixtures / "tree.jsonl")

    repeated = sync_pi(repository, pi_fixtures / "tree.jsonl")

    assert repeated.lines_read == 0
    assert repeated.observations_inserted == 0
    assert repeated.events_inserted == 0
    assert repeated.events_updated == 0


def test_repeated_subagent_sync_is_a_true_noop(
    repository: Repository, pi_fixtures: Path
) -> None:
    sync_pi(repository, pi_fixtures / "subagents.jsonl")

    repeated = sync_pi(repository, pi_fixtures / "subagents.jsonl")

    assert repeated.lines_read == 0
    assert repeated.observations_inserted == 0
    assert repeated.events_inserted == 0
    assert repeated.events_updated == 0


def test_incremental_append_adds_only_the_new_usage(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "append.jsonl"
    _write_jsonl(path, [_header("append"), _assistant("first", 3, 2)])
    first = sync_pi(repository, path)
    with path.open("a", encoding="utf-8") as session_file:
        session_file.write(json.dumps(_assistant("second", 4, 1), separators=(",", ":")) + "\n")

    appended = sync_pi(repository, path)

    assert first.lines_read == 2
    assert appended.lines_read == 1
    assert repository.source_total("pi").tokens.input == 7
    assert repository.event_count("pi") == 2


def test_session_preserves_initial_model_across_later_model_changes(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "models.jsonl"
    _write_jsonl(
        path,
        [
            _header("models"),
            {
                "type": "model_change",
                "id": "model-first",
                "parentId": None,
                "timestamp": "2026-08-28T00:00:01Z",
                "provider": "provider-one",
                "modelId": "model-one",
            },
            {
                "type": "model_change",
                "id": "model-second",
                "parentId": "model-first",
                "timestamp": "2026-08-28T00:00:02Z",
                "provider": "provider-two",
                "modelId": "model-two",
            },
        ],
    )

    sync_pi(repository, path)

    assert repository._database.connection.execute(
        "SELECT initial_model, current_model, provider FROM sessions "
        "WHERE source_session_id = 'models'"
    ).fetchone() == ("model-one", "model-two", "provider-two")


def test_incremental_model_change_keeps_previously_imported_initial_model(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "incremental-model.jsonl"
    first_model = {
        "type": "model_change",
        "id": "model-first",
        "parentId": None,
        "timestamp": "2026-08-28T00:00:01Z",
        "provider": "provider-one",
        "modelId": "model-one",
    }
    second_model = {
        "type": "model_change",
        "id": "model-second",
        "parentId": "model-first",
        "timestamp": "2026-08-28T00:00:02Z",
        "provider": "provider-two",
        "modelId": "model-two",
    }
    _write_jsonl(path, [_header("incremental-model"), first_model])
    sync_pi(repository, path)
    with path.open("a", encoding="utf-8") as session_file:
        session_file.write(json.dumps(second_model, separators=(",", ":")) + "\n")

    sync_pi(repository, path)

    assert repository._database.connection.execute(
        "SELECT initial_model, current_model, provider FROM sessions "
        "WHERE source_session_id = 'incremental-model'"
    ).fetchone() == ("model-one", "model-two", "provider-two")


def test_clone_copies_are_observed_but_counted_once(
    repository: Repository, tmp_path: Path, pi_fixtures: Path
) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "tree.jsonl").write_bytes((pi_fixtures / "tree.jsonl").read_bytes())
    (root / "clone.jsonl").write_bytes((pi_fixtures / "clone.jsonl").read_bytes())

    sync_pi(repository, root)

    assert repository.event_count("pi") == 5
    assert repository.source_total("pi").tokens.input == 27
    assert repository._database.connection.execute(
        "SELECT COUNT(*) FROM observations WHERE source = 'pi' "
        "AND source_turn_id = 'pi:root:assistant-1:assistant'"
    ).fetchone() == (2,)
    assert repository._database.connection.execute(
        "SELECT root_session_id, parent_session_id FROM sessions "
        "WHERE source_session_id = 'clone'"
    ).fetchone() == ("root", "root")


def test_missing_parent_reuses_persisted_clone_lineage_without_colliding_sessions(
    repository: Repository, tmp_path: Path
) -> None:
    root = tmp_path / "sessions"
    parent = root / "parent.jsonl"
    clone_a = root / "clone-a.jsonl"
    clone_b = root / "clone-b.jsonl"
    independent_a = root / "independent-a.jsonl"
    independent_b = root / "independent-b.jsonl"
    copied_call = _assistant("copied-call", 5, 1)
    _write_jsonl(parent, [_header("lineage-root"), copied_call])
    _write_jsonl(
        clone_a,
        [_header("clone-a", parent=str(parent)), copied_call],
    )
    _write_jsonl(
        clone_b,
        [_header("clone-b", parent=str(parent)), copied_call],
    )
    _write_jsonl(
        independent_a,
        [_header("independent-a"), _assistant("copied-call", 2, 1)],
    )
    _write_jsonl(
        independent_b,
        [_header("independent-b"), _assistant("copied-call", 3, 1)],
    )
    sync_pi(repository, root)
    parked = tmp_path / "parent-parked.jsonl"
    parent.replace(parked)
    for clone, session_id in ((clone_a, "clone-a"), (clone_b, "clone-b")):
        replacement = tmp_path / f"{session_id}-replacement.jsonl"
        _write_jsonl(
            replacement,
            [_header(session_id, parent=str(parent)), copied_call],
        )
        replacement.replace(clone)

    sync_pi(repository, root)

    assert repository.source_total("pi").tokens.input == 10
    assert repository._database.connection.execute(
        "SELECT event_key FROM usage_events "
        "WHERE source = 'pi' AND status = 'canonical' ORDER BY event_key"
    ).fetchall() == [
        ("pi:independent-a:copied-call:assistant",),
        ("pi:independent-b:copied-call:assistant",),
        ("pi:lineage-root:copied-call:assistant",),
    ]
    assert repository._database.connection.execute(
        "SELECT root_session_id FROM sessions "
        "WHERE source_session_id IN ('clone-a', 'clone-b') "
        "ORDER BY source_session_id"
    ).fetchall() == [("lineage-root",), ("lineage-root",)]


def test_clone_only_subagent_usage_uses_the_available_physical_session(
    repository: Repository, tmp_path: Path
) -> None:
    parent = tmp_path / "parent.jsonl"
    clone = tmp_path / "clone.jsonl"
    _write_jsonl(parent, [_header("external-root")])
    _write_jsonl(
        clone,
        [
            _header("clone-only", parent=str(parent)),
            {
                "type": "custom",
                "id": "snapshot",
                "timestamp": "2026-08-28T00:00:01Z",
                "customType": "orchestrator-subagent",
                "data": {
                    "id": "A001",
                    "batchId": "batch-only",
                    "status": "completed",
                    "startedAt": 1787875200000,
                    "finishedAt": 1787875201000,
                    "usage": {
                        "input": 4,
                        "output": 1,
                        "cacheRead": 0,
                        "cacheWrite": 0,
                        "cost": 0.005,
                    },
                },
            },
        ],
    )

    sync_pi(repository, clone)

    assert repository.get_event("pi:subagent:batch-only:A001") is not None
    assert repository._database.connection.execute(
        "SELECT s.source_session_id FROM runs r "
        "JOIN sessions s ON s.id = r.session_id "
        "WHERE r.source_run_id = 'batch-only:A001'"
    ).fetchone() == ("clone-only",)


def test_replacement_excludes_removed_usage_and_rebuilds_current_generation(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "replacement.jsonl"
    _write_jsonl(path, [_header("replace"), _assistant("old", 9, 2)])
    sync_pi(repository, path)
    replacement = tmp_path / "new.jsonl"
    _write_jsonl(replacement, [_header("replace"), _assistant("new", 4, 1)])
    replacement.replace(path)

    sync_pi(repository, path)

    old = repository.get_event("pi:replace:old:assistant")
    new = repository.get_event("pi:replace:new:assistant")
    assert old is not None and old.status == "excluded"
    assert new is not None and new.status == "canonical"
    assert repository.source_total("pi").tokens.input == 4


def test_same_file_truncation_excludes_removed_usage(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "truncation.jsonl"
    _write_jsonl(
        path,
        [
            _header("truncate"),
            _assistant("first", 9, 2),
            _assistant("second", 8, 1),
        ],
    )
    sync_pi(repository, path)
    _write_jsonl(path, [_header("truncate"), _assistant("current", 3, 1)])

    sync_pi(repository, path)

    first = repository.get_event("pi:truncate:first:assistant")
    second = repository.get_event("pi:truncate:second:assistant")
    assert first is not None and first.status == "excluded"
    assert second is not None and second.status == "excluded"
    assert repository.source_total("pi").tokens.input == 3


def test_sync_recovers_after_interrupted_rebuild(
    repository: Repository,
    pi_fixtures: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rebuild = pi_import._rebuild_events

    def interrupt_rebuild(_: Repository, __: set[str]) -> object:
        raise RuntimeError("interrupted rebuild")

    monkeypatch.setattr(pi_import, "_rebuild_events", interrupt_rebuild)
    with pytest.raises(RuntimeError, match="interrupted rebuild"):
        sync_pi(repository, pi_fixtures / "tree.jsonl")
    monkeypatch.setattr(pi_import, "_rebuild_events", rebuild)

    recovered = sync_pi(repository, pi_fixtures / "tree.jsonl")

    assert recovered.lines_read == 0
    assert repository.source_total("pi").tokens.input == 25
    assert repository._database.connection.execute(
        "SELECT DISTINCT status FROM usage_events WHERE source = 'pi'"
    ).fetchall() == [("canonical",)]


def test_replacement_scan_failure_repairs_stale_run_from_eof(
    repository: Repository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "repair-crash.jsonl"
    _write_jsonl(
        path,
        [
            _header("repair-crash"),
            _subagent_snapshot(
                "completed",
                status="completed",
                input_tokens=40,
                output_tokens=10,
                cost=0.2,
                timestamp="2026-08-28T00:00:01Z",
                model="old-provider/old-model",
                finished_at=1787875201000,
            ),
        ],
    )
    sync_pi(repository, path)
    replacement = tmp_path / "repair-crash-replacement.jsonl"
    _write_jsonl(
        replacement,
        [
            _header("repair-crash"),
            _subagent_snapshot(
                "running",
                status="running",
                input_tokens=12,
                output_tokens=3,
                cost=0.1,
                timestamp="2026-08-28T00:00:02Z",
                model="new-provider/new-model",
            ),
        ],
    )
    replacement.replace(path)
    repair = pi_import._repair_subagent_runs

    def interrupt_repair(_: Repository, __: object) -> None:
        raise RuntimeError("interrupted subagent repair")

    monkeypatch.setattr(pi_import, "_repair_subagent_runs", interrupt_repair)
    with pytest.raises(RuntimeError, match="interrupted subagent repair"):
        sync_pi(repository, path)
    monkeypatch.setattr(pi_import, "_repair_subagent_runs", repair)

    recovered = sync_pi(repository, path)
    repeated = sync_pi(repository, path)

    assert recovered.lines_read == 0
    assert repository._database.connection.execute(
        "SELECT status, completed_at_ns, duration_ns FROM runs "
        "WHERE source_run_id = 'batch-replaced:A001'"
    ).fetchone() == ("running", None, None)
    event = repository.get_event("pi:subagent:batch-replaced:A001")
    assert event is not None
    assert event.status == "canonical"
    assert event.success is None
    assert event.completed_at_ns is None
    assert repeated.events_updated == 0


def test_global_subagent_status_reduces_all_live_artifact_generations(
    repository: Repository, tmp_path: Path
) -> None:
    root = tmp_path / "sessions"
    terminal_a = root / "a.jsonl"
    replaceable_b = root / "b.jsonl"
    _write_jsonl(
        terminal_a,
        [
            _header("copy-a"),
            _subagent_snapshot(
                "completed-a",
                status="completed",
                input_tokens=40,
                output_tokens=10,
                cost=0.2,
                timestamp="2026-08-28T00:00:01Z",
                model="provider-a/model-a",
                finished_at=1787875201000,
                thinking_level="high",
            ),
        ],
    )
    _write_jsonl(
        replaceable_b,
        [
            _header("copy-b"),
            _subagent_snapshot(
                "completed-b",
                status="completed",
                input_tokens=30,
                output_tokens=8,
                cost=0.15,
                timestamp="2026-08-28T00:00:02Z",
                model="provider-b/model-b",
                finished_at=1787875202000,
            ),
        ],
    )
    sync_pi(repository, root)
    replacement_b = tmp_path / "replacement-b.jsonl"
    _write_jsonl(
        replacement_b,
        [
            _header("copy-b"),
            _subagent_snapshot(
                "running-b",
                status="running",
                input_tokens=12,
                output_tokens=3,
                cost=0.1,
                timestamp="2026-08-28T00:00:03Z",
                model="provider-b/model-b-new",
                thinking_level="low",
            ),
        ],
    )
    replacement_b.replace(replaceable_b)

    sync_pi(repository, root)

    event = repository.get_event("pi:subagent:batch-replaced:A001")
    assert event is not None
    assert event.tokens.input == 40
    assert event.success is True
    assert event.completed_at_ns == 1787875201000000000
    assert (event.provider, event.model, event.effort) == (
        "provider-a",
        "model-a",
        "high",
    )
    assert repository._database.connection.execute(
        "SELECT status, completed_at_ns FROM runs "
        "WHERE source_run_id = 'batch-replaced:A001'"
    ).fetchone() == ("completed", 1787875201000000000)
    replacement_a = tmp_path / "replacement-a.jsonl"
    _write_jsonl(
        replacement_a,
        [
            _header("copy-a"),
            _subagent_snapshot(
                "running-a",
                status="running",
                input_tokens=15,
                output_tokens=4,
                cost=0.12,
                timestamp="2026-08-28T00:00:04Z",
                model="provider-a/model-a-new",
            ),
        ],
    )
    replacement_a.replace(terminal_a)

    sync_pi(repository, root)

    event = repository.get_event("pi:subagent:batch-replaced:A001")
    assert event is not None
    assert event.tokens.input == 15
    assert event.success is None
    assert event.completed_at_ns is None
    assert repository._database.connection.execute(
        "SELECT status, completed_at_ns, duration_ns FROM runs "
        "WHERE source_run_id = 'batch-replaced:A001'"
    ).fetchall() == [("running", None, None)]


def test_subagent_reducer_preserves_epoch_zero_timings(
    repository: Repository, tmp_path: Path
) -> None:
    path = tmp_path / "epoch-zero.jsonl"
    snapshot = _subagent_snapshot(
        "epoch-zero",
        status="completed",
        input_tokens=1,
        output_tokens=1,
        cost=0.001,
        timestamp="2026-08-28T00:00:01Z",
        model="provider/model",
        finished_at=0,
    )
    data = snapshot["data"]
    assert isinstance(data, dict)
    data["startedAt"] = 0
    _write_jsonl(path, [_header("epoch-zero"), snapshot])

    sync_pi(repository, path)

    event = repository.get_event("pi:subagent:batch-replaced:A001")
    assert event is not None
    assert (event.started_at_ns, event.completed_at_ns) == (0, 0)
    assert repository._database.connection.execute(
        "SELECT started_at_ns, completed_at_ns, duration_ns FROM runs "
        "WHERE source_run_id = 'batch-replaced:A001'"
    ).fetchone() == (0, 0, 0)


def test_missing_artifact_is_excluded_and_rediscovery_restores_it(
    repository: Repository, tmp_path: Path
) -> None:
    root = tmp_path / "sessions"
    path = root / "missing.jsonl"
    _write_jsonl(path, [_header("missing"), _assistant("missing-call", 9, 1)])
    sync_pi(repository, root)
    parked = tmp_path / "parked.jsonl"
    path.replace(parked)

    sync_pi(repository, root)

    event = repository.get_event("pi:missing:missing-call:assistant")
    assert event is not None and event.status == "excluded"
    parked.replace(path)

    restored = sync_pi(repository, root)
    repeated = sync_pi(repository, root)

    event = repository.get_event("pi:missing:missing-call:assistant")
    assert restored.lines_read == 0
    assert event is not None and event.status == "canonical"
    assert repeated.events_updated == 0


def test_rediscovery_rebuild_failure_recovers_from_eof_on_next_sync(
    repository: Repository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "sessions"
    path = root / "rediscovered.jsonl"
    _write_jsonl(path, [_header("rediscovered"), _assistant("call", 9, 1)])
    sync_pi(repository, root)
    parked = tmp_path / "parked.jsonl"
    path.replace(parked)
    sync_pi(repository, root)
    parked.replace(path)
    rebuild = pi_import._rebuild_events

    def interrupt_rebuild(_: Repository, __: set[str]) -> object:
        raise RuntimeError("interrupted rediscovery rebuild")

    monkeypatch.setattr(pi_import, "_rebuild_events", interrupt_rebuild)
    with pytest.raises(RuntimeError, match="interrupted rediscovery rebuild"):
        sync_pi(repository, root)
    monkeypatch.setattr(pi_import, "_rebuild_events", rebuild)

    recovered = sync_pi(repository, root)

    event = repository.get_event("pi:rediscovered:call:assistant")
    assert recovered.lines_read == 0
    assert event is not None and event.status == "canonical"
