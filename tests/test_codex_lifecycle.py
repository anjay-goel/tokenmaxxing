import sqlite3
from pathlib import Path

import pytest

import tokenmaxxing.ingest.codex.reconcile as codex_import
from tokenmaxxing.ingest.codex import sync_codex
from tokenmaxxing.models import (
    LinkDraft,
    ObservationDraft,
    Projection,
    TokenUsage,
    UsageEventDraft,
    WriteStats,
)
from tokenmaxxing.repository import Repository
from codex_support import (
    _event,
    _copy,
    _create_state_db,
    _roots,
    _session_meta,
    _token_count,
    _write_jsonl,
)


@pytest.fixture
def codex_fixtures() -> Path:
    return Path(__file__).parent / "fixtures" / "codex"


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


def test_replacement_retains_each_owner_and_deduplicates_reappearance(
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

    old_event = _event(repository, "old-owner", 0)
    new_event = _event(repository, "new-owner", 0)
    assert old_event is not None and old_event.status == "canonical"
    assert new_event is not None and new_event.status == "canonical"
    assert repository.source_total("codex").tokens.input == 30

    _write_jsonl(rollout, old_records)
    sync_codex(repository, roots)

    old_event = _event(repository, "old-owner", 0)
    new_event = _event(repository, "new-owner", 0)
    assert old_event is not None and old_event.status == "canonical"
    assert new_event is not None and new_event.status == "canonical"
    assert repository.source_total("codex").tokens.input == 30


def test_truncated_or_missing_rollout_preserves_owner(
    repository: Repository, tmp_path: Path
) -> None:
    roots = _roots(tmp_path)
    rollout = roots.sessions / "rollout.jsonl"
    records = [_session_meta("old-owner"), _token_count(10, 10)]
    _write_jsonl(rollout, records)
    sync_codex(repository, roots)

    _write_jsonl(rollout, records[:1])
    sync_codex(repository, roots)

    event = _event(repository, "old-owner", 0)
    assert event is not None and event.status == "canonical"
    assert repository.source_total("codex").tokens.input == 10

    _write_jsonl(rollout, records)
    sync_codex(repository, roots)
    event = _event(repository, "old-owner", 0)
    assert event is not None and event.status == "canonical"

    rollout.unlink()
    sync_codex(repository, roots)

    event = _event(repository, "old-owner", 0)
    assert event is not None and event.status == "canonical"
    assert repository.source_total("codex").tokens.input == 10

    _write_jsonl(rollout, records)
    sync_codex(repository, roots)

    event = _event(repository, "old-owner", 0)
    assert event is not None and event.status == "canonical"
    assert repository.source_total("codex").tokens.input == 10


def test_missing_distinct_stream_preserves_collected_usage(
    repository: Repository, tmp_path: Path
) -> None:
    roots = _roots(tmp_path)
    owner = "duplicate-owner"
    missing = roots.sessions / "a-missing.jsonl"
    survivor = roots.sessions / "b-survivor.jsonl"
    survivor_records = [_session_meta(owner), _token_count(10, 10)]
    missing_records = [_session_meta(owner), _token_count(20, 20, output=7)]
    _write_jsonl(survivor, survivor_records)
    _write_jsonl(missing, missing_records)
    sync_codex(repository, roots)

    conflicted = _event(repository, owner, 0)
    assert conflicted is not None and conflicted.status == "canonical"

    missing.unlink()
    sync_codex(repository, roots)

    survivor_event = _event(repository, owner, 0)
    assert survivor_event is not None and survivor_event.status == "canonical"
    assert survivor_event.tokens.input == 20
    assert survivor_event.tokens.output == 7
    assert repository.source_total("codex").tokens.input == 30

    repeated = sync_codex(repository, roots)
    assert repeated.events_updated == 0
    _write_jsonl(missing, missing_records)
    sync_codex(repository, roots)
    reappeared = _event(repository, owner, 0)
    assert reappeared is not None and reappeared.status == "canonical"

    missing.unlink()
    sync_codex(repository, roots)
    recovered = _event(repository, owner, 0)
    assert recovered is not None and recovered.status == "canonical"
    assert repository.source_total("codex").tokens.input == 30


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

    connection = repository.connection
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

    removed = _event(repository, "owner-a", 25)
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

    connection = repository.connection
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

    connection = repository.connection
    stale_owners = ("wild_%", "prefix")
    connection.execute(
        "UPDATE observations SET event_type = 'stale_token_count' "
        "WHERE source = 'codex' AND source_session_id IN (?, ?) "
        "AND event_type = 'token_count'",
        stale_owners,
    )

    codex_import._rebuild_owners(repository, set(stale_owners))

    for owner in stale_owners:
        event = _event(repository, owner, 0)
        assert event is not None and event.status == "excluded"
    for owner in ("wild_AX", "prefix:child"):
        event = _event(repository, owner, 0)
        assert event is not None and event.status == "canonical"

    connection.execute(
        "UPDATE observations SET event_type = 'token_count' "
        "WHERE source = 'codex' AND source_session_id IN (?, ?) "
        "AND event_type = 'stale_token_count'",
        stale_owners,
    )
    codex_import._rebuild_owners(repository, set(stale_owners))

    for owner in owners:
        event = _event(repository, owner, 0)
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

    connection = repository.connection
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        codex_import._rebuild_owners(repository, {owner})
    finally:
        connection.set_trace_callback(None)

    owner_event = _event(repository, owner, 0)
    child_event = _event(repository, child_owner, 0)
    assert owner_event is not None and owner_event.status == "canonical"
    assert child_event is not None and child_event.status == "canonical"
    query = next(
        statement
        for statement in statements
        if statement.startswith(
            "SELECT DISTINCT e.id, e.event_key FROM observations o "
        )
    )
    details = [str(row[3]) for row in connection.execute(f"EXPLAIN QUERY PLAN {query}")]

    assert (
        "SEARCH o USING COVERING INDEX idx_observations_codex_owner_link" in details[0]
    )
    assert (
        "SEARCH l USING COVERING INDEX sqlite_autoindex_observation_links_1"
        in details[1]
    )
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

    connection = repository.connection
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

    event = _event(repository, owner, 0)
    assert event is not None and event.status == "excluded"


def test_rebuild_owners_zero_variable_limit_raises_sqlite_error(
    repository: Repository,
) -> None:
    connection = repository.connection
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

    statuses = repository.connection.execute(
        "SELECT status FROM usage_events WHERE source = 'codex' ORDER BY event_key"
    ).fetchall()
    assert statuses == [("provisional",), ("provisional",)]

    monkeypatch.setattr(codex_import, "_rebuild_owners", rebuild)
    recovery = sync_codex(repository, roots)

    assert recovery.lines_read == 0
    assert repository.source_total("codex").tokens == TokenUsage(input=60)
    recovered_statuses = repository.connection.execute(
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
    connection = repository.connection
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


def test_rebuild_history_repairs_legacy_collisions_and_restores_observation_dates(
    repository: Repository, tmp_path: Path
) -> None:
    roots = _roots(tmp_path)
    first = _token_count(10, 10)
    second = _token_count(30, 20)
    second["timestamp"] = "2026-08-29T00:00:01Z"
    _write_jsonl(
        roots.sessions / "legacy.jsonl",
        [
            _session_meta("legacy"),
            first,
            _session_meta("legacy"),
            second,
        ],
    )
    sync_codex(repository, roots)
    before = repository.profile_reporting_rows()
    connection = repository.connection
    connection.execute(
        "UPDATE observations SET source_sequence = 0 WHERE source = 'codex'"
    )
    repository.apply_projection(
        Projection(
            events=(
                UsageEventDraft(
                    source="codex",
                    event_key="codex:legacy:0",
                    granularity="counter_delta",
                    status="conflicted",
                    tokens=TokenUsage(),
                    model="known-model",
                ),
            )
        )
    )
    legacy_id = connection.execute(
        "SELECT id FROM usage_events WHERE event_key = 'codex:legacy:0'"
    ).fetchone()[0]
    connection.execute(
        "UPDATE observation_links SET usage_event_id = ? WHERE observation_id IN (SELECT id FROM observations WHERE event_type = 'token_count')",
        (legacy_id,),
    )
    connection.execute(
        "DELETE FROM usage_events WHERE event_key LIKE 'codex:legacy:usage:%'"
    )
    retained_observations = connection.execute(
        "SELECT * FROM observations ORDER BY id"
    ).fetchall()
    codex_import.rebuild_codex_history(repository)
    assert (
        connection.execute("SELECT * FROM observations ORDER BY id").fetchall()
        == retained_observations
    )
    assert repository.source_total("codex").tokens.input == 30
    after = repository.profile_reporting_rows()
    assert [row.usage.occurred_at_ns for row in after] == [
        row.usage.occurred_at_ns for row in before
    ]
    assert all(row.usage.resolved_model == "known-model" for row in after)
    assert repository.get_event("codex:legacy:0").status == "excluded"
    assert codex_import.rebuild_codex_history(repository).events_updated == 0


def test_normal_sync_upgrades_legacy_history_after_source_cleanup(
    repository: Repository, tmp_path: Path
) -> None:
    roots = _roots(tmp_path)
    path = roots.sessions / "removed.jsonl"
    _write_jsonl(path, [_session_meta("upgrade"), _token_count(10, 10)])
    sync_codex(repository, roots)
    connection = repository.connection
    connection.execute(
        "UPDATE usage_events SET event_key = 'codex:upgrade:0', status = 'excluded' "
        "WHERE source = 'codex'"
    )
    connection.execute("UPDATE observation_links SET method = 'owner_ordinal'")
    path.unlink()
    upgraded = sync_codex(repository, roots)
    assert upgraded.lines_read == 0
    assert repository.source_total("codex").tokens.input == 10
    assert repository.get_event("codex:upgrade:0").status == "excluded"
    assert codex_import._legacy_owners(repository) == set()
    assert sync_codex(repository, roots).events_updated == 0
