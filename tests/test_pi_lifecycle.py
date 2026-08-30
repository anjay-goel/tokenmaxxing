import json
from pathlib import Path

import pytest

import tokenmaxxing.ingest.pi.reconcile as pi_reconcile
import tokenmaxxing.ingest.pi.subagents as pi_subagents
from tokenmaxxing.ingest.pi import sync_pi
from tokenmaxxing.repository import Repository
from pi_support import (
    _assistant,
    _header,
    _subagent_snapshot,
    _write_jsonl,
)


@pytest.fixture
def pi_fixtures() -> Path:
    return Path(__file__).parent / "fixtures" / "pi"


def test_repeat_sync_is_a_true_noop(
    repository: Repository, pi_fixtures: Path
) -> None:
    sync_pi(repository, pi_fixtures / "tree.jsonl")

    repeated = sync_pi(repository, pi_fixtures / "tree.jsonl")

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
    assert repository.connection.execute(
        "SELECT COUNT(*) FROM observations WHERE source = 'pi' "
        "AND source_turn_id = 'pi:root:assistant-1:assistant'"
    ).fetchone() == (2,)
    assert repository.connection.execute(
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
    assert repository.connection.execute(
        "SELECT event_key FROM usage_events "
        "WHERE source = 'pi' AND status = 'canonical' ORDER BY event_key"
    ).fetchall() == [
        ("pi:independent-a:copied-call:assistant",),
        ("pi:independent-b:copied-call:assistant",),
        ("pi:lineage-root:copied-call:assistant",),
    ]
    assert repository.connection.execute(
        "SELECT root_session_id FROM sessions "
        "WHERE source_session_id IN ('clone-a', 'clone-b') "
        "ORDER BY source_session_id"
    ).fetchall() == [("lineage-root",), ("lineage-root",)]


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
    rebuild = pi_reconcile._rebuild_events

    def interrupt_rebuild(_: Repository, __: set[str]) -> object:
        raise RuntimeError("interrupted rebuild")

    monkeypatch.setattr(pi_reconcile, "_rebuild_events", interrupt_rebuild)
    with pytest.raises(RuntimeError, match="interrupted rebuild"):
        sync_pi(repository, pi_fixtures / "tree.jsonl")
    monkeypatch.setattr(pi_reconcile, "_rebuild_events", rebuild)

    recovered = sync_pi(repository, pi_fixtures / "tree.jsonl")

    assert recovered.lines_read == 0
    assert repository.source_total("pi").tokens.input == 25
    assert repository.connection.execute(
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
    repair = pi_subagents._repair_subagent_runs

    def interrupt_repair(_: Repository, __: object) -> None:
        raise RuntimeError("interrupted subagent repair")

    monkeypatch.setattr(pi_subagents, "_repair_subagent_runs", interrupt_repair)
    with pytest.raises(RuntimeError, match="interrupted subagent repair"):
        sync_pi(repository, path)
    monkeypatch.setattr(pi_subagents, "_repair_subagent_runs", repair)

    recovered = sync_pi(repository, path)
    repeated = sync_pi(repository, path)

    assert recovered.lines_read == 0
    assert repository.connection.execute(
        "SELECT status, completed_at_ns, duration_ns FROM runs "
        "WHERE source_run_id = 'batch-replaced:A001'"
    ).fetchone() == ("running", None, None)
    event = repository.get_event("pi:subagent:batch-replaced:A001")
    assert event is not None
    assert event.status == "canonical"
    assert event.success is None
    assert event.completed_at_ns is None
    assert repeated.events_updated == 0


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
    rebuild = pi_reconcile._rebuild_events

    def interrupt_rebuild(_: Repository, __: set[str]) -> object:
        raise RuntimeError("interrupted rediscovery rebuild")

    monkeypatch.setattr(pi_reconcile, "_rebuild_events", interrupt_rebuild)
    with pytest.raises(RuntimeError, match="interrupted rediscovery rebuild"):
        sync_pi(repository, root)
    monkeypatch.setattr(pi_reconcile, "_rebuild_events", rebuild)

    recovered = sync_pi(repository, root)

    event = repository.get_event("pi:rediscovered:call:assistant")
    assert recovered.lines_read == 0
    assert event is not None and event.status == "canonical"
