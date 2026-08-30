import json
import sqlite3
from pathlib import Path

import pytest

import tokenmaxxing.ingest.codex as codex_import
from tokenmaxxing.db import Database
from tokenmaxxing.ingest.codex import CodexRoots, sync_codex
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
def codex_fixtures() -> Path:
    return Path(__file__).parent / "fixtures" / "codex"


def _roots(root: Path) -> CodexRoots:
    return CodexRoots(
        sessions=root / "sessions",
        archived_sessions=root / "archived_sessions",
        state_db=root / "state_5.sqlite",
        thread_history_db=root / "thread_history_1.sqlite",
    )


def _copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def _session_meta(session_id: str) -> dict[str, object]:
    return {
        "timestamp": "2026-08-28T00:00:00Z",
        "type": "session_meta",
        "payload": {"id": session_id, "model_provider": "openai"},
    }


def _token_count(total: int, last: int) -> dict[str, object]:
    return {
        "timestamp": "2026-08-28T00:00:01Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {"input_tokens": total},
                "last_token_usage": {"input_tokens": last},
            },
        },
    }


def _create_state_db(path: Path, *, tokens_used: int = 0) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            model_provider TEXT NOT NULL,
            tokens_used INTEGER NOT NULL,
            cli_version TEXT,
            model TEXT,
            reasoning_effort TEXT,
            agent_role TEXT,
            archived_at INTEGER
        );
        CREATE TABLE thread_spawn_edges (
            parent_thread_id TEXT NOT NULL,
            child_thread_id TEXT NOT NULL PRIMARY KEY,
            status TEXT NOT NULL
        );
        """
    )
    connection.executemany(
        "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            (
                "parent-session",
                1_700_000_000,
                1_700_000_010,
                "openai",
                tokens_used,
                "1.0.0",
                "gpt-parent",
                "high",
                "root",
                None,
            ),
            (
                "child-session",
                1_700_000_020,
                1_700_000_030,
                "openai",
                tokens_used,
                "1.0.0",
                "gpt-child",
                "medium",
                "worker",
                None,
            ),
        ),
    )
    connection.execute(
        "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
        ("parent-session", "child-session", "completed"),
    )
    connection.commit()
    connection.close()


def _create_thread_history_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE thread_turns (
            thread_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            rollout_ordinal INTEGER NOT NULL,
            status TEXT NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            duration_ms INTEGER,
            PRIMARY KEY (thread_id, turn_id)
        )
        """
    )
    connection.execute(
        "INSERT INTO thread_turns VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "parent-session",
            "turn-1",
            0,
            "completed",
            1_700_000_001,
            1_700_000_003,
            2_000,
        ),
    )
    connection.commit()
    connection.close()


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


def test_incremental_append_restores_the_current_owner(
    repository: Repository, tmp_path: Path, codex_fixtures: Path
) -> None:
    roots = _roots(tmp_path)
    rollout = roots.sessions / "parent.jsonl"
    _copy(codex_fixtures / "basic.jsonl", rollout)
    sync_codex(repository, roots)

    with rollout.open("ab") as destination:
        destination.write(
            b'{"timestamp":"2026-08-28T00:00:02Z","type":"event_msg",'
            b'"payload":{"type":"token_count","info":{"total_token_usage":'
            b'{"input_tokens":115,"output_tokens":12},"last_token_usage":'
            b'{"input_tokens":15,"output_tokens":2}}}}\n'
        )
    sync_codex(repository, roots)

    assert repository.source_total("codex").tokens == TokenUsage(input=115, output=12)


def test_replacement_excludes_stale_owner_and_restores_reappearance(
    repository: Repository, tmp_path: Path
) -> None:
    roots = _roots(tmp_path)
    rollout = roots.sessions / "rollout.jsonl"
    old_records = [_session_meta("old-owner"), _token_count(10, 10)]
    new_records = [_session_meta("new-owner"), _token_count(20, 20)]
    _write_jsonl(rollout, old_records)
    sync_codex(repository, roots)

    _write_jsonl(rollout, new_records)
    sync_codex(repository, roots)

    old_event = repository.get_event("codex:old-owner:0")
    new_event = repository.get_event("codex:new-owner:0")
    assert old_event is not None and old_event.status == "excluded"
    assert new_event is not None and new_event.status == "canonical"
    assert repository.source_total("codex").tokens.input == 20

    _write_jsonl(rollout, old_records)
    sync_codex(repository, roots)

    old_event = repository.get_event("codex:old-owner:0")
    new_event = repository.get_event("codex:new-owner:0")
    assert old_event is not None and old_event.status == "canonical"
    assert new_event is not None and new_event.status == "excluded"
    assert repository.source_total("codex").tokens.input == 10


def test_truncated_or_missing_rollout_excludes_and_restores_owner(
    repository: Repository, tmp_path: Path
) -> None:
    roots = _roots(tmp_path)
    rollout = roots.sessions / "rollout.jsonl"
    records = [_session_meta("old-owner"), _token_count(10, 10)]
    _write_jsonl(rollout, records)
    sync_codex(repository, roots)

    _write_jsonl(rollout, records[:1])
    sync_codex(repository, roots)

    event = repository.get_event("codex:old-owner:0")
    assert event is not None and event.status == "excluded"
    assert repository.source_total("codex").tokens.input is None

    _write_jsonl(rollout, records)
    sync_codex(repository, roots)
    event = repository.get_event("codex:old-owner:0")
    assert event is not None and event.status == "canonical"

    rollout.unlink()
    sync_codex(repository, roots)

    event = repository.get_event("codex:old-owner:0")
    assert event is not None and event.status == "excluded"
    assert repository.source_total("codex").tokens.input is None

    _write_jsonl(rollout, records)
    sync_codex(repository, roots)

    event = repository.get_event("codex:old-owner:0")
    assert event is not None and event.status == "canonical"
    assert repository.source_total("codex").tokens.input == 10


def test_missing_divergent_copy_recovers_survivor_and_reappearance_conflict(
    repository: Repository, tmp_path: Path
) -> None:
    roots = _roots(tmp_path)
    owner = "duplicate-owner"
    survivor = roots.sessions / "a-survivor.jsonl"
    missing = roots.sessions / "b-missing.jsonl"
    survivor_records = [_session_meta(owner), _token_count(10, 10)]
    missing_records = [_session_meta(owner), _token_count(20, 20)]
    _write_jsonl(survivor, survivor_records)
    _write_jsonl(missing, missing_records)
    sync_codex(repository, roots)

    conflicted = repository.get_event(f"codex:{owner}:0")
    assert conflicted is not None and conflicted.status == "conflicted"

    missing.unlink()
    sync_codex(repository, roots)

    survivor_event = repository.get_event(f"codex:{owner}:0")
    assert survivor_event is not None and survivor_event.status == "canonical"
    assert repository.source_total("codex").tokens.input == 10

    repeated = sync_codex(repository, roots)
    assert repeated.events_updated == 0

    _write_jsonl(missing, missing_records)
    sync_codex(repository, roots)
    reappeared = repository.get_event(f"codex:{owner}:0")
    assert reappeared is not None and reappeared.status == "conflicted"

    missing.unlink()
    sync_codex(repository, roots)
    recovered = repository.get_event(f"codex:{owner}:0")
    assert recovered is not None and recovered.status == "canonical"
    assert repository.source_total("codex").tokens.input == 10


def test_rebuild_owners_scopes_cleanup_keys_under_sqlite_variable_limit(
    repository: Repository, tmp_path: Path
) -> None:
    roots = _roots(tmp_path)
    owners = {"owner-a", "owner-b"}
    for owner in owners:
        _write_jsonl(
            roots.sessions / f"{owner}.jsonl",
            [_session_meta(owner), *(_token_count(index, 1) for index in range(1, 27))],
        )
    sync_codex(repository, roots)

    connection = repository._database.connection
    stale_observation = (
        "source = 'codex' AND source_session_id = 'owner-a' "
        "AND source_sequence = 25 AND event_type = 'token_count'"
    )
    connection.execute(
        f"UPDATE observations SET event_type = 'stale_token_count' WHERE {stale_observation}"
    )
    if not hasattr(connection, "getlimit") or not hasattr(connection, "setlimit"):
        pytest.skip("sqlite variable limits are unavailable")
    variable_limit = sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER
    original_limit = connection.getlimit(variable_limit)
    if original_limit < 51:
        pytest.skip("sqlite variable limit is already too low for this regression")
    connection.setlimit(variable_limit, 50)
    try:
        codex_import._rebuild_owners(repository, owners)
    finally:
        connection.setlimit(variable_limit, original_limit)

    removed = repository.get_event("codex:owner-a:25")
    assert removed is not None and removed.status == "excluded"
    assert repository.source_total("codex").tokens.input == 51


def test_rebuild_owners_chunks_owner_query_under_sqlite_variable_limit(
    repository: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _roots(tmp_path)
    owners = {f"owner-{index:03d}" for index in range(51)}
    for owner in owners:
        _write_jsonl(
            roots.sessions / f"{owner}.jsonl",
            [_session_meta(owner), _token_count(1, 1)],
        )
    rebuild = codex_import._rebuild_owners
    monkeypatch.setattr(codex_import, "_rebuild_owners", lambda *_: WriteStats())
    sync_codex(repository, roots)
    monkeypatch.setattr(codex_import, "_rebuild_owners", rebuild)

    connection = repository._database.connection
    if not hasattr(connection, "getlimit") or not hasattr(connection, "setlimit"):
        pytest.skip("sqlite variable limits are unavailable")
    variable_limit = sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER
    original_limit = connection.getlimit(variable_limit)
    if original_limit < 51:
        pytest.skip("sqlite variable limit is already too low for this regression")
    connection.setlimit(variable_limit, 50)
    try:
        codex_import._rebuild_owners(repository, owners)
    finally:
        connection.setlimit(variable_limit, original_limit)

    assert repository.source_total("codex").tokens.input == 51


def test_rebuild_owners_uses_observation_owners_for_wildcard_and_colon_ids(
    repository: Repository, tmp_path: Path
) -> None:
    roots = _roots(tmp_path)
    owners = {"wild_%", "wild_AX", "prefix", "prefix:child"}
    for owner in owners:
        _write_jsonl(
            roots.sessions / f"{owner.replace(':', '-')}.jsonl",
            [_session_meta(owner), _token_count(1, 1)],
        )
    sync_codex(repository, roots)

    connection = repository._database.connection
    stale_owners = ("wild_%", "prefix")
    connection.execute(
        "UPDATE observations SET event_type = 'stale_token_count' "
        "WHERE source = 'codex' AND source_session_id IN (?, ?) "
        "AND event_type = 'token_count'",
        stale_owners,
    )

    codex_import._rebuild_owners(repository, set(stale_owners))

    for owner in stale_owners:
        event = repository.get_event(f"codex:{owner}:0")
        assert event is not None and event.status == "excluded"
    for owner in ("wild_AX", "prefix:child"):
        event = repository.get_event(f"codex:{owner}:0")
        assert event is not None and event.status == "canonical"

    connection.execute(
        "UPDATE observations SET event_type = 'token_count' "
        "WHERE source = 'codex' AND source_session_id IN (?, ?) "
        "AND event_type = 'stale_token_count'",
        stale_owners,
    )
    codex_import._rebuild_owners(repository, set(stale_owners))

    for owner in owners:
        event = repository.get_event(f"codex:{owner}:0")
        assert event is not None and event.status == "canonical"


def test_owner_cleanup_starts_from_exact_owner_observations(
    repository: Repository, tmp_path: Path
) -> None:
    roots = _roots(tmp_path)
    owner = "exact-owner"
    child_owner = "exact-owner:child"
    for source_owner in (owner, child_owner):
        _write_jsonl(
            roots.sessions / f"{source_owner.replace(':', '-')}.jsonl",
            [_session_meta(source_owner), _token_count(1, 1)],
        )
    sync_codex(repository, roots)

    connection = repository._database.connection
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        codex_import._rebuild_owners(repository, {owner})
    finally:
        connection.set_trace_callback(None)

    owner_event = repository.get_event(f"codex:{owner}:0")
    child_event = repository.get_event(f"codex:{child_owner}:0")
    assert owner_event is not None and owner_event.status == "canonical"
    assert child_event is not None and child_event.status == "canonical"
    query = next(
        statement
        for statement in statements
        if statement.startswith("SELECT DISTINCT e.id, e.event_key FROM observations o ")
    )
    details = [str(row[3]) for row in connection.execute(f"EXPLAIN QUERY PLAN {query}")]

    assert "SEARCH o USING COVERING INDEX idx_observations_codex_owner_link" in details[0]
    assert "SEARCH l USING COVERING INDEX sqlite_autoindex_observation_links_1" in details[1]
    assert "SEARCH e USING INTEGER PRIMARY KEY" in details[2]


def test_rebuild_owners_excludes_stale_event_under_variable_limit_one(
    repository: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _roots(tmp_path)
    owner = "limit-one"
    _write_jsonl(
        roots.sessions / "limit-one.jsonl",
        [_session_meta(owner), _token_count(1, 1)],
    )
    rebuild = codex_import._rebuild_owners
    monkeypatch.setattr(codex_import, "_rebuild_owners", lambda *_: WriteStats())
    sync_codex(repository, roots)
    monkeypatch.setattr(codex_import, "_rebuild_owners", rebuild)

    connection = repository._database.connection
    connection.execute(
        "UPDATE observations SET event_type = 'stale_token_count' "
        "WHERE source = 'codex' AND source_session_id = ? "
        "AND event_type = 'token_count'",
        (owner,),
    )
    if not hasattr(connection, "getlimit") or not hasattr(connection, "setlimit"):
        pytest.skip("sqlite variable limits are unavailable")
    variable_limit = sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER
    original_limit = connection.getlimit(variable_limit)
    if original_limit < 1:
        pytest.skip("sqlite variable limit is already too low for this regression")
    connection.setlimit(variable_limit, 1)
    try:
        codex_import._rebuild_owners(repository, {owner})
    finally:
        connection.setlimit(variable_limit, original_limit)

    event = repository.get_event(f"codex:{owner}:0")
    assert event is not None and event.status == "excluded"


def test_rebuild_owners_zero_variable_limit_raises_sqlite_error(
    repository: Repository,
) -> None:
    connection = repository._database.connection
    if not hasattr(connection, "getlimit") or not hasattr(connection, "setlimit"):
        pytest.skip("sqlite variable limits are unavailable")
    variable_limit = sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER
    original_limit = connection.getlimit(variable_limit)
    connection.setlimit(variable_limit, 0)
    try:
        with pytest.raises(sqlite3.OperationalError, match="too many SQL variables"):
            codex_import._rebuild_owners(repository, {"limit-zero"})
    finally:
        connection.setlimit(variable_limit, original_limit)


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


def test_sync_recovers_persisted_provisional_owner_after_interrupted_rebuild(
    repository: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _roots(tmp_path)
    _write_jsonl(
        roots.sessions / "rollout.jsonl",
        [
            _session_meta("recovery-session"),
            _token_count(100, 10),
            _token_count(150, 5),
        ],
    )
    rebuild = codex_import._rebuild_owners

    def interrupt_rebuild(_: Repository, __: set[str]) -> None:
        raise RuntimeError("interrupted rebuild")

    monkeypatch.setattr(codex_import, "_rebuild_owners", interrupt_rebuild)
    with pytest.raises(RuntimeError, match="interrupted rebuild"):
        sync_codex(repository, roots)

    statuses = repository._database.connection.execute(
        "SELECT status FROM usage_events WHERE source = 'codex' ORDER BY event_key"
    ).fetchall()
    assert statuses == [("provisional",), ("provisional",)]

    monkeypatch.setattr(codex_import, "_rebuild_owners", rebuild)
    recovery = sync_codex(repository, roots)

    assert recovery.lines_read == 0
    assert repository.source_total("codex").tokens == TokenUsage(input=60)
    recovered_statuses = repository._database.connection.execute(
        "SELECT status FROM usage_events WHERE source = 'codex' ORDER BY event_key"
    ).fetchall()
    assert recovered_statuses == [("canonical",), ("canonical",)]


def test_provisional_owner_recovery_starts_from_the_event_status_index(
    repository: Repository,
) -> None:
    repository.apply_projection(
        Projection(
            observations=(
                ObservationDraft(
                    source="codex",
                    channel="disk",
                    stable_key="provisional-observation",
                    event_type="token_count",
                    observed_at_ns=1,
                    parser_version="test",
                    projection={},
                    source_session_id="recovery-session",
                ),
            ),
            events=(
                UsageEventDraft(
                    source="codex",
                    event_key="codex:recovery-session:0",
                    granularity="counter_delta",
                    status="provisional",
                    tokens=TokenUsage(),
                ),
            ),
            links=(
                LinkDraft(
                    source="codex",
                    channel="disk",
                    observation_key="provisional-observation",
                    event_key="codex:recovery-session:0",
                    method="owner_ordinal",
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
        assert codex_import._provisional_owners(repository) == {"recovery-session"}
    finally:
        connection.set_trace_callback(None)

    query = next(
        statement
        for statement in statements
        if statement.startswith("SELECT DISTINCT o.source_session_id FROM ")
    )
    plan = connection.execute(f"EXPLAIN QUERY PLAN {query}").fetchall()

    assert any(
        "SEARCH e USING" in str(row[3]) and "idx_usage_events_status" in str(row[3])
        for row in plan
    )
    assert any(
        "SEARCH l USING" in str(row[3])
        and "idx_observation_links_usage_event" in str(row[3])
        for row in plan
    )


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


def test_archive_move_does_not_create_a_second_event(
    repository: Repository, tmp_path: Path, codex_fixtures: Path
) -> None:
    roots = _roots(tmp_path)
    active = roots.sessions / "parent.jsonl"
    archived = roots.archived_sessions / "parent.jsonl"
    _copy(codex_fixtures / "basic.jsonl", active)
    sync_codex(repository, roots)

    archived.parent.mkdir(parents=True)
    active.replace(archived)
    sync_codex(repository, roots)

    assert repository.source_total("codex").tokens.input == 100
    assert repository.event_count("codex") == 1


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

    session_rows = repository._database.connection.execute(
        "SELECT source_session_id, root_session_id, parent_session_id "
        "FROM sessions WHERE source = 'codex' ORDER BY source_session_id"
    ).fetchall()
    assert session_rows == [
        ("child-session", "parent-session", "parent-session"),
        ("parent-session", "parent-session", None),
    ]
    run_rows = repository._database.connection.execute(
        "SELECT source_run_id, parent_run_id, depth FROM runs ORDER BY source_run_id"
    ).fetchall()
    assert run_rows == [
        ("child-session", "parent-session", 1),
        ("parent-session", None, 0),
    ]
    child_event = repository.get_event("codex:child-session:0")
    assert child_event is not None
    assert child_event.session_id == repository._database.connection.execute(
        "SELECT id FROM sessions WHERE source = 'codex' AND source_session_id = 'parent-session'"
    ).fetchone()[0]


def test_unchanged_state_does_not_rebuild_canonical_history(
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

    repeated = sync_codex(repository, roots)

    assert repeated.lines_read == 0
    assert repeated.events_updated == 0


def test_thread_history_supplies_turn_timing_only(
    repository: Repository, tmp_path: Path, codex_fixtures: Path
) -> None:
    roots = _roots(tmp_path)
    _copy(codex_fixtures / "basic.jsonl", roots.sessions / "parent.jsonl")
    _create_thread_history_db(roots.thread_history_db)

    sync_codex(repository, roots)

    row = repository._database.connection.execute(
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

    issue = repository._database.connection.execute(
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
