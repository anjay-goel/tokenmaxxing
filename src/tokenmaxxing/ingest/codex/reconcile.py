import json
import sqlite3
from collections.abc import Mapping
from typing import cast

from tokenmaxxing.ingest.codex.parse import _TOKEN_FIELDS, _usage
from tokenmaxxing.models import (
    IssueDraft,
    Projection,
    TokenUsage,
    UsageEventDraft,
    WriteStats,
)
from tokenmaxxing.repository import Repository

def _delta_usage(
    previous: Mapping[str, int] | None,
    total: Mapping[str, int],
    last: Mapping[str, int],
) -> tuple[TokenUsage, bool]:
    reset = previous is None or any(
        name in previous and name in total and total[name] < previous[name]
        for name in _TOKEN_FIELDS
    )
    if reset:
        values = dict(last)
    else:
        values = {}
        for name in _TOKEN_FIELDS:
            if name not in total:
                continue
            if name in previous:
                values[name] = max(total[name] - previous[name], 0)
            elif name in last:
                values[name] = last[name]
    token_usage = _usage(values)
    return token_usage, any(value > 0 for value in values.values())


def _rebuild_owners(repository: Repository, owners: set[str]) -> WriteStats:
    if not owners:
        return WriteStats()
    connection = repository.connection
    source_owners = sorted(owners)
    chunk_size = max(
        1, min(500, connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
    )
    rows: list[tuple[object, ...]] = []
    for offset in range(0, len(source_owners), chunk_size):
        owner_chunk = source_owners[offset : offset + chunk_size]
        placeholders = ", ".join("?" for _ in owner_chunk)
        rows.extend(
            connection.execute(
                "SELECT o.source_session_id, o.source_sequence, o.observed_at_ns, o.projection_json "
                "FROM observations o JOIN artifacts a ON a.id = o.artifact_id "
                "WHERE o.source = 'codex' AND o.channel = 'disk' AND o.event_type = 'token_count' "
                f"AND o.source_session_id IN ({placeholders}) "
                "AND COALESCE(a.is_missing, 0) = 0 "
                "AND a.generation = (SELECT MAX(current.generation) FROM artifacts current "
                "WHERE current.source = a.source AND current.path_hash = a.path_hash) "
                "ORDER BY o.source_session_id, o.source_sequence, o.artifact_id, o.ordinal",
                tuple(owner_chunk),
            ).fetchall()
        )
    semantic: dict[tuple[str, int], tuple[int, dict[str, object]]] = {}
    conflicts: set[tuple[str, int]] = set()
    for owner_value, ordinal_value, observed_at_ns, projection_json in rows:
        if not isinstance(owner_value, str) or ordinal_value is None:
            continue
        key = (owner_value, int(ordinal_value))
        projection = json.loads(str(projection_json))
        usage = projection.get("usage") if isinstance(projection, dict) else None
        if not isinstance(usage, dict):
            continue
        candidate = (int(observed_at_ns), cast(dict[str, object], usage))
        if key in semantic and semantic[key][1] != candidate[1]:
            conflicts.add(key)
            continue
        semantic.setdefault(key, candidate)

    previous_by_owner: dict[str, dict[str, int]] = {}
    active_keys_by_owner: dict[str, set[str]] = {owner: set() for owner in owners}
    writes = WriteStats()
    with repository.transaction() as target:
        for (owner, ordinal), (observed_at_ns, usage) in sorted(semantic.items()):
            event_key = f"codex:{owner}:{ordinal}"
            active_keys_by_owner.setdefault(owner, set()).add(event_key)
            total = cast(dict[str, int], usage.get("total"))
            last = cast(dict[str, int], usage.get("last"))
            if (owner, ordinal) in conflicts:
                previous_by_owner.pop(owner, None)
                projection = Projection(
                    events=(
                        UsageEventDraft(
                            source="codex",
                            event_key=event_key,
                            granularity="counter_delta",
                            status="conflicted",
                            tokens=TokenUsage(),
                            replace_usage=True,
                        ),
                    ),
                    issues=(
                        IssueDraft(
                            source="codex",
                            category="conflicting_owner_event",
                            severity="error",
                            identifier=event_key,
                            field_path=str(ordinal),
                            observed_type="usage",
                        ),
                    ),
                )
            else:
                tokens, countable = _delta_usage(previous_by_owner.get(owner), total, last)
                session_row = target.execute(
                    "SELECT id, root_session_id FROM sessions "
                    "WHERE source = 'codex' AND source_session_id = ?",
                    (owner,),
                ).fetchone()
                session_id: int | None = None
                run_id: int | None = None
                if session_row is not None:
                    root_id = str(session_row[1] or owner)
                    root_row = target.execute(
                        "SELECT id FROM sessions WHERE source = 'codex' AND source_session_id = ?",
                        (root_id,),
                    ).fetchone()
                    session_id = int(root_row[0]) if root_row is not None else int(session_row[0])
                    run_row = target.execute(
                        "SELECT r.id FROM runs r JOIN sessions s ON s.id = r.session_id "
                        "WHERE s.source = 'codex' AND s.source_session_id = ? "
                        "AND r.source_run_id = ?",
                        (owner, owner),
                    ).fetchone()
                    run_id = int(run_row[0]) if run_row is not None else None
                projection = Projection(
                    events=(
                        UsageEventDraft(
                            source="codex",
                            event_key=event_key,
                            granularity="counter_delta",
                            status="canonical" if countable else "excluded",
                            tokens=tokens,
                            session_id=session_id,
                            run_id=run_id,
                            completed_at_ns=observed_at_ns or None,
                            replace_usage=True,
                        ),
                    )
                )
            added = repository.apply_projection_in_transaction(target, projection)
            writes = WriteStats(
                observations_inserted=writes.observations_inserted + added.observations_inserted,
                events_inserted=writes.events_inserted + added.events_inserted,
                events_updated=writes.events_updated + added.events_updated,
                links_inserted=writes.links_inserted + added.links_inserted,
                samples_inserted=writes.samples_inserted + added.samples_inserted,
                issues_recorded=writes.issues_recorded + added.issues_recorded,
            )
            if (owner, ordinal) not in conflicts:
                previous_by_owner[owner] = total
        for owner in sorted(owners):
            existing_events = {
                (int(row[0]), str(row[1]))
                for row in target.execute(
                    "SELECT DISTINCT e.id, e.event_key FROM observations o "
                    "INDEXED BY idx_observations_codex_owner_link "
                    "CROSS JOIN observation_links l "
                    "INDEXED BY sqlite_autoindex_observation_links_1 "
                    "CROSS JOIN usage_events e "
                    "WHERE o.source = 'codex' AND o.channel = 'disk' "
                    "AND o.source_session_id = ? "
                    "AND l.observation_id = o.id AND e.id = l.usage_event_id "
                    "AND e.source = 'codex'",
                    (owner,),
                )
            }
            target.executemany(
                "UPDATE usage_events SET status = 'excluded' "
                "WHERE id = ?",
                (
                    (event_id,)
                    for event_id, event_key in sorted(
                        existing_events
                    )
                    if event_key not in active_keys_by_owner.get(owner, set())
                ),
            )
    return writes


def _provisional_owners(repository: Repository) -> set[str]:
    rows = repository.connection.execute(
        "SELECT DISTINCT o.source_session_id FROM usage_events e "
        "INDEXED BY idx_usage_events_status "
        "CROSS JOIN observation_links l INDEXED BY idx_observation_links_usage_event "
        "CROSS JOIN observations o "
        "WHERE o.source = 'codex' AND o.channel = 'disk' "
        "AND o.event_type = 'token_count' AND e.status = 'provisional' "
        "AND l.usage_event_id = e.id AND o.id = l.observation_id "
        "AND o.source_session_id IS NOT NULL"
    ).fetchall()
    return {str(row[0]) for row in rows}
def _stale_counted_owners(repository: Repository) -> set[str]:
    rows = repository.connection.execute(
        "SELECT DISTINCT old.source_session_id FROM usage_events e "
        "INDEXED BY idx_usage_events_status "
        "CROSS JOIN observation_links l "
        "INDEXED BY idx_observation_links_usage_event "
        "CROSS JOIN observations old "
        "WHERE e.source = 'codex' AND e.status IN ('canonical', 'provisional', 'conflicted') "
        "AND l.usage_event_id = e.id AND old.id = l.observation_id "
        "AND old.source = 'codex' AND old.channel = 'disk' "
        "AND old.event_type = 'token_count' AND old.source_session_id IS NOT NULL "
        "AND (NOT EXISTS ("
        "SELECT 1 FROM observations live "
        "INDEXED BY idx_observations_codex_owner_artifact "
        "JOIN artifacts a ON a.id = live.artifact_id "
        "WHERE live.source = 'codex' AND live.channel = 'disk' "
        "AND live.event_type = 'token_count' "
        "AND live.source_session_id = old.source_session_id "
        "AND COALESCE(a.is_missing, 0) = 0 "
        "AND a.generation = (SELECT MAX(current.generation) FROM artifacts current "
        "WHERE current.source = a.source AND current.path_hash = a.path_hash)) "
        "OR (e.status = 'conflicted' AND EXISTS ("
        "SELECT 1 FROM observations missing "
        "INDEXED BY idx_observations_codex_owner_artifact "
        "JOIN artifacts a ON a.id = missing.artifact_id "
        "WHERE missing.source = 'codex' AND missing.channel = 'disk' "
        "AND missing.event_type = 'token_count' "
        "AND missing.source_session_id = old.source_session_id "
        "AND a.is_missing = 1 "
        "AND a.generation = (SELECT MAX(current.generation) FROM artifacts current "
        "WHERE current.source = a.source AND current.path_hash = a.path_hash))))"
    ).fetchall()
    return {str(row[0]) for row in rows}
