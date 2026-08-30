import json
from pathlib import Path

import pytest

from tokenmaxxing.ingest.pi import sync_pi
from tokenmaxxing.models import Projection, TokenUsage, UsageEventDraft
from tokenmaxxing.repository import Repository
from pi_support import (
    _header,
    _subagent_snapshot,
    _write_jsonl,
)


@pytest.fixture
def pi_fixtures() -> Path:
    return Path(__file__).parent / "fixtures" / "pi"


def _database_bytes(db_path: Path) -> bytes:
    return b"".join(
        path.read_bytes()
        for path in (db_path, Path(f"{db_path}-wal"), Path(f"{db_path}-shm"))
        if path.exists()
    )


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
    assert repository.connection.execute(
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
    assert repository.connection.execute(
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
    assert repository.connection.execute(
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
    assert repository.connection.execute(
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


def test_repeated_subagent_sync_is_a_true_noop(
    repository: Repository, pi_fixtures: Path
) -> None:
    sync_pi(repository, pi_fixtures / "subagents.jsonl")

    repeated = sync_pi(repository, pi_fixtures / "subagents.jsonl")

    assert repeated.lines_read == 0
    assert repeated.observations_inserted == 0
    assert repeated.events_inserted == 0
    assert repeated.events_updated == 0


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
    assert repository.connection.execute(
        "SELECT s.source_session_id FROM runs r "
        "JOIN sessions s ON s.id = r.session_id "
        "WHERE r.source_run_id = 'batch-only:A001'"
    ).fetchone() == ("clone-only",)


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
    assert repository.connection.execute(
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
    assert repository.connection.execute(
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
    assert repository.connection.execute(
        "SELECT started_at_ns, completed_at_ns, duration_ns FROM runs "
        "WHERE source_run_id = 'batch-replaced:A001'"
    ).fetchone() == (0, 0, 0)
