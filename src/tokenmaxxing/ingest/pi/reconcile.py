import json
from pathlib import Path

from tokenmaxxing.ingest.jsonl import (
    _latest_artifact,
    _needs_new_generation,
    _path_hash,
    _prefix_fingerprint,
)
from tokenmaxxing.ingest.pi.parse import (
    _COST_COLUMNS,
    _TOKEN_COLUMNS,
    _mapping,
    _subagent_usage,
    _usage_cost,
    _usage_tokens,
)
from tokenmaxxing.models import (
    CostUsage,
    LinkDraft,
    Projection,
    TokenUsage,
    UsageEventDraft,
    WriteStats,
)
from tokenmaxxing.repository import Repository

def _artifact_event_keys(repository: Repository, artifact_id: int) -> set[str]:
    rows = repository.connection.execute(
        "SELECT DISTINCT source_turn_id FROM observations "
        "WHERE source = 'pi' AND channel = 'disk' AND artifact_id = ? "
        "AND source_turn_id IS NOT NULL",
        (artifact_id,),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _superseded_events(repository: Repository, path: Path) -> set[str]:
    connection = repository.connection
    artifact = _latest_artifact(connection, "pi", _path_hash(connection, path))
    if artifact is None:
        return set()
    stat = path.stat()
    prefix = _prefix_fingerprint(path, artifact.size_bytes)
    if not _needs_new_generation(artifact, stat, prefix, None):
        return set()
    return _artifact_event_keys(repository, artifact.id)


def _rediscovered_events(repository: Repository, path: Path) -> set[str]:
    connection = repository.connection
    artifact = _latest_artifact(connection, "pi", _path_hash(connection, path))
    if artifact is None:
        return set()
    row = connection.execute(
        "SELECT is_missing FROM artifacts WHERE id = ?", (artifact.id,)
    ).fetchone()
    if row is None or row[0] != 1:
        return set()
    event_keys = _artifact_event_keys(repository, artifact.id)
    with repository.transaction() as transaction:
        transaction.execute("UPDATE artifacts SET is_missing = 0 WHERE id = ?", (artifact.id,))
        transaction.executemany(
            "UPDATE usage_events SET status = 'provisional' "
            "WHERE source = 'pi' AND event_key = ?",
            ((event_key,) for event_key in event_keys),
        )
    return event_keys


def _mark_missing(
    repository: Repository, root: Path, paths: list[Path]
) -> set[str]:
    if not root.is_dir():
        return set()
    connection = repository.connection
    present_hashes = {_path_hash(connection, path) for path in paths}
    rows = connection.execute(
        "SELECT a.id, a.path_hash FROM artifacts a "
        "WHERE a.source = 'pi' AND a.generation = ("
        "SELECT MAX(current.generation) FROM artifacts current "
        "WHERE current.source = a.source AND current.path_hash = a.path_hash) "
        "AND COALESCE(a.is_missing, 0) = 0"
    ).fetchall()
    missing_ids = [int(row[0]) for row in rows if str(row[1]) not in present_hashes]
    changed: set[str] = set()
    for artifact_id in missing_ids:
        changed.update(_artifact_event_keys(repository, artifact_id))
    if missing_ids:
        with repository.transaction() as transaction:
            transaction.executemany(
                "UPDATE artifacts SET is_missing = 1 WHERE id = ?",
                ((artifact_id,) for artifact_id in missing_ids),
            )
    return changed


def _provisional_events(repository: Repository) -> set[str]:
    rows = repository.connection.execute(
        "SELECT event_key FROM usage_events WHERE source = 'pi' AND status = 'provisional'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _stale_events(repository: Repository) -> set[str]:
    rows = repository.connection.execute(
        "SELECT e.event_key FROM usage_events e WHERE e.source = 'pi' "
        "AND e.status IN ('canonical', 'provisional') AND NOT EXISTS ("
        "SELECT 1 FROM observation_links l "
        "JOIN observations o ON o.id = l.observation_id "
        "JOIN artifacts a ON a.id = o.artifact_id "
        "WHERE l.usage_event_id = e.id AND o.source = 'pi' "
        "AND COALESCE(a.is_missing, 0) = 0 "
        "AND a.generation = (SELECT MAX(current.generation) FROM artifacts current "
        "WHERE current.source = a.source AND current.path_hash = a.path_hash))"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _max_usage(rows: list[tuple[object, ...]]) -> tuple[TokenUsage, CostUsage]:
    token_values: dict[str, int | None] = {name: None for name in _TOKEN_COLUMNS}
    cost_values: dict[str, int | None] = {
        name: None for name in _COST_COLUMNS if name.endswith("_nanos")
    }
    original_decimal: str | None = None
    cost_source: str | None = None
    cost_estimated: bool | None = None
    for row in rows:
        projection = json.loads(str(row[5]))
        usage = _mapping(projection.get("usage") if isinstance(projection, dict) else None)
        if usage is None:
            continue
        event_type = str(row[4])
        if event_type == "subagent_snapshot":
            parsed = _subagent_usage(usage)
        else:
            tokens = _usage_tokens(usage)
            parsed = (tokens, _usage_cost(usage)) if tokens is not None else None
        if parsed is None:
            continue
        tokens, cost = parsed
        for name in token_values:
            candidate = getattr(tokens, name)
            if candidate is not None:
                token_values[name] = max(token_values[name] or 0, candidate)
        candidate_total = cost.total_nanos
        current_total = cost_values["total_nanos"]
        if candidate_total is not None and (
            current_total is None
            or candidate_total > current_total
            or (
                candidate_total == current_total
                and original_decimal is None
                and cost.original_decimal is not None
            )
        ):
            original_decimal = cost.original_decimal
            cost_source = cost.source
            cost_estimated = cost.estimated
        for name in cost_values:
            candidate = getattr(cost, name)
            if candidate is not None:
                cost_values[name] = max(cost_values[name] or 0, candidate)
    return (
        TokenUsage(**token_values),
        CostUsage(
            **cost_values,
            original_decimal=original_decimal,
            source=cost_source,
            estimated=cost_estimated,
        ),
    )


def _canonical_ids(
    repository: Repository, session_source_id: str | None, run_source_id: str | None
) -> tuple[int | None, int | None]:
    connection = repository.connection
    session_row = None
    if session_source_id is not None:
        session_row = connection.execute(
            "SELECT id FROM sessions WHERE source = 'pi' AND source_session_id = ?",
            (session_source_id,),
        ).fetchone()
    session_id = int(session_row[0]) if session_row is not None else None
    if run_source_id is None:
        return session_id, None
    run_row = connection.execute(
        "SELECT r.id, r.session_id FROM runs r JOIN sessions s ON s.id = r.session_id "
        "WHERE s.source = 'pi' AND r.source_run_id = ? ORDER BY r.id LIMIT 1",
        (run_source_id,),
    ).fetchone()
    if run_row is None:
        return session_id, None
    return int(run_row[1]), int(run_row[0])


def _set_exact_event(
    repository: Repository,
    event_key: str,
    tokens: TokenUsage,
    cost: CostUsage,
    session_id: int | None,
    run_id: int | None,
) -> None:
    assignments = ["status = 'canonical'", "session_id = ?", "run_id = ?"]
    values: list[object] = [session_id, run_id]
    for attribute, column in _TOKEN_COLUMNS.items():
        assignments.append(f"{column} = ?")
        values.append(getattr(tokens, attribute))
    for attribute, column in _COST_COLUMNS.items():
        assignments.append(f"{column} = ?")
        value = getattr(cost, attribute)
        values.append(int(value) if isinstance(value, bool) else value)
    values.append(event_key)
    repository.connection.execute(
        f"UPDATE usage_events SET {', '.join(assignments)} "
        "WHERE source = 'pi' AND event_key = ?",
        tuple(values),
    )


def _rebuild_events(repository: Repository, event_keys: set[str]) -> WriteStats:
    if not event_keys:
        return WriteStats()
    writes = WriteStats()
    with repository.transaction() as connection:
        for event_key in sorted(event_keys):
            rows = connection.execute(
                "SELECT o.stable_key, o.source_session_id, o.source_run_id, "
                "o.observed_at_ns, o.event_type, o.projection_json FROM observations o "
                "JOIN artifacts a ON a.id = o.artifact_id "
                "WHERE o.source = 'pi' AND o.channel = 'disk' AND o.source_turn_id = ? "
                "AND COALESCE(a.is_missing, 0) = 0 "
                "AND a.generation = (SELECT MAX(current.generation) FROM artifacts current "
                "WHERE current.source = a.source AND current.path_hash = a.path_hash) "
                "ORDER BY o.observed_at_ns, o.artifact_id, o.ordinal",
                (event_key,),
            ).fetchall()
            if not rows:
                connection.execute(
                    "UPDATE usage_events SET status = 'excluded' "
                    "WHERE source = 'pi' AND event_key = ?",
                    (event_key,),
                )
                continue
            tokens, cost = _max_usage([tuple(row) for row in rows])
            current = repository.get_event(event_key)
            if current is None:
                continue
            session_source_id = next(
                (str(row[1]) for row in rows if isinstance(row[1], str)), None
            )
            run_source_id = next(
                (str(row[2]) for row in rows if isinstance(row[2], str)), None
            )
            session_id, run_id = _canonical_ids(
                repository, session_source_id, run_source_id
            )
            event = UsageEventDraft(
                source="pi",
                event_key=event_key,
                granularity=current.granularity,
                status="canonical",
                tokens=tokens,
                cost=cost,
                session_id=session_id,
                run_id=run_id,
                provider=current.provider,
                api=current.api,
                model=current.model,
                response_model=current.response_model,
                effort=current.effort,
                stop_reason=current.stop_reason,
                error_category=current.error_category,
                started_at_ns=current.started_at_ns,
                completed_at_ns=(
                    current.completed_at_ns
                    if str(rows[0][4]) == "subagent_snapshot"
                    else max(
                        (int(row[3]) for row in rows if isinstance(row[3], int)),
                        default=current.completed_at_ns,
                    )
                ),
                success=current.success,
            )
            added = repository.apply_projection_in_transaction(
                connection,
                Projection(
                    events=(event,),
                    links=tuple(
                        LinkDraft(
                            source="pi",
                            channel="disk",
                            observation_key=str(row[0]),
                            event_key=event_key,
                            method="lineage_entry_slot"
                            if str(row[4]) != "subagent_snapshot"
                            else "subagent_run_key",
                            role="primary",
                            confidence="exact",
                        )
                        for row in rows
                    ),
                ),
            )
            writes = WriteStats(
                observations_inserted=writes.observations_inserted,
                events_inserted=writes.events_inserted + added.events_inserted,
                events_updated=writes.events_updated + added.events_updated,
                links_inserted=writes.links_inserted + added.links_inserted,
                samples_inserted=writes.samples_inserted,
                issues_recorded=writes.issues_recorded,
            )
            _set_exact_event(repository, event_key, tokens, cost, session_id, run_id)
    return writes
