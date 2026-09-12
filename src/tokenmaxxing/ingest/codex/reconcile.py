import json
import sqlite3
import time
from dataclasses import replace
from collections.abc import Mapping

from tokenmaxxing.ingest.codex.parse import _TOKEN_FIELDS, _event_key, _usage
from tokenmaxxing.models import (
    IssueDraft,
    LinkDraft,
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
                "SELECT o.source_session_id, o.artifact_id, o.ordinal, o.observed_at_ns, "
                "o.projection_json, o.stable_key, o.id FROM observations o "
                "WHERE o.source = 'codex' AND o.channel = 'disk' AND o.event_type = 'token_count' "
                f"AND o.source_session_id IN ({placeholders}) AND o.artifact_id IS NOT NULL "
                "ORDER BY o.artifact_id, o.ordinal",
                tuple(owner_chunk),
            ).fetchall()
        )

    previous: dict[tuple[str, int], dict[str, int]] = {}
    candidates: dict[str, list[tuple[TokenUsage, int, int, str, int, str]]] = {}
    occurrences: dict[tuple[str, int, str], int] = {}
    active: dict[str, set[str]] = {owner: set() for owner in owners}
    for (
        owner_value,
        artifact,
        ordinal,
        observed,
        projection_json,
        stable_key,
        observation_id,
    ) in rows:
        owner = str(owner_value)
        projection = json.loads(str(projection_json))
        usage = projection.get("usage") if isinstance(projection, dict) else None
        if not isinstance(usage, dict):
            continue
        total, last = usage.get("total"), usage.get("last")
        if not isinstance(total, dict) or not isinstance(last, dict):
            continue
        stream = (owner, int(artifact))
        tokens, _ = _delta_usage(previous.get(stream), total, last)
        previous[stream] = total
        event_key = _event_key(owner, int(observed), usage)
        occurrence_key = (owner, int(artifact), event_key)
        previous_occurrence = occurrences.get(occurrence_key)
        occurrence = previous_occurrence if previous_occurrence is not None else 0
        if previous_occurrence is not None and any(
            (getattr(tokens, field) or 0) > 0 for field in _TOKEN_FIELDS
        ):
            occurrence += 1
        occurrences[occurrence_key] = occurrence
        if occurrence:
            event_key = f"{event_key}:repeat:{occurrence}"
        active[owner].add(event_key)
        candidates.setdefault(event_key, []).append(
            (tokens, int(artifact), int(ordinal), str(stable_key), int(observed), owner)
        )

    writes = WriteStats()
    with repository.transaction() as target:
        for event_key, versions in candidates.items():
            # Original physical context wins over later compacted or copied counter streams.
            selected = min(versions, key=lambda row: (row[1], row[2]))
            tokens, _, _, _, observed_at_ns, owner = selected
            current = repository.get_event(event_key, source="codex")
            first_by_artifact = {}
            for row in versions:
                first_by_artifact.setdefault(row[1], row)
            conflicting_context = any(
                row[0] != tokens for row in first_by_artifact.values()
            )
            if (
                conflicting_context
                and current is not None
                and current.status == "canonical"
            ):
                tokens = current.tokens
            countable = any(
                (getattr(tokens, field) or 0) > 0 for field in _TOKEN_FIELDS
            )
            session_row = target.execute(
                "SELECT id, root_session_id FROM sessions WHERE source = 'codex' AND source_session_id = ?",
                (owner,),
            ).fetchone()
            session_id = run_id = None
            if session_row is not None:
                root = target.execute(
                    "SELECT id FROM sessions WHERE source = 'codex' AND source_session_id = ?",
                    (str(session_row[1] or owner),),
                ).fetchone()
                session_id = int(root[0]) if root is not None else int(session_row[0])
                run = target.execute(
                    "SELECT id FROM runs WHERE session_id = ? AND source_run_id = ?",
                    (int(session_row[0]), owner),
                ).fetchone()
                run_id = int(run[0]) if run is not None else None
            if current is None:
                legacy = target.execute(
                    "SELECT e.event_key FROM observations o JOIN observation_links l ON l.observation_id = o.id "
                    "JOIN usage_events e ON e.id = l.usage_event_id "
                    "WHERE o.source = 'codex' AND o.channel = 'disk' AND o.stable_key = ? ORDER BY e.id LIMIT 1",
                    (selected[3],),
                ).fetchone()
                current = (
                    repository.get_event(str(legacy[0]), source="codex")
                    if legacy is not None
                    else None
                )
            template = current or UsageEventDraft(
                source="codex",
                event_key=event_key,
                granularity="counter_delta",
                status="provisional",
                tokens=TokenUsage(),
            )
            event = replace(
                template,
                event_key=event_key,
                granularity="counter_delta",
                status="canonical" if countable else "excluded",
                tokens=tokens,
                session_id=session_id
                if session_id is not None
                else template.session_id,
                run_id=run_id if run_id is not None else template.run_id,
                completed_at_ns=observed_at_ns or template.completed_at_ns,
                replace_usage=True,
            )
            unchanged = (
                current is not None
                and current.event_key == event_key
                and all(
                    getattr(current, field) == getattr(event, field)
                    for field in (
                        "status",
                        "tokens",
                        "session_id",
                        "run_id",
                        "completed_at_ns",
                    )
                )
            )
            added = repository.apply_projection_in_transaction(
                target,
                Projection(
                    events=() if unchanged else (event,),
                    links=tuple(
                        LinkDraft(
                            source="codex",
                            channel="disk",
                            observation_key=row[3],
                            event_key=event_key,
                            method="usage_fingerprint",
                            role="primary",
                            confidence="exact",
                        )
                        for row in versions
                    ),
                    issues=(
                        IssueDraft(
                            source="codex",
                            category="conflicting_counter_context",
                            severity="warning",
                            identifier=event_key,
                            observed_type="usage",
                        ),
                    )
                    if conflicting_context
                    else (),
                ),
            )
            writes = WriteStats(
                events_inserted=writes.events_inserted + added.events_inserted,
                events_updated=writes.events_updated + added.events_updated,
                links_inserted=writes.links_inserted + added.links_inserted,
                issues_recorded=writes.issues_recorded + added.issues_recorded,
            )
        for owner in sorted(owners):
            existing = target.execute(
                "SELECT DISTINCT e.id, e.event_key FROM observations o "
                "INDEXED BY idx_observations_codex_owner_link CROSS JOIN observation_links l "
                "INDEXED BY sqlite_autoindex_observation_links_1 CROSS JOIN usage_events e "
                "WHERE o.source = 'codex' AND o.channel = 'disk' AND o.source_session_id = ? "
                "AND l.observation_id = o.id AND e.id = l.usage_event_id AND e.source = 'codex'",
                (owner,),
            ).fetchall()
            target.executemany(
                "UPDATE usage_events SET status = 'excluded' WHERE id = ?",
                (
                    (int(row[0]),)
                    for row in existing
                    if str(row[1]) not in active[owner]
                ),
            )
    return writes


def rebuild_codex_history(repository: Repository) -> WriteStats:
    connection = repository.connection
    with repository.transaction():
        owners = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT source_session_id FROM observations WHERE source = 'codex' "
                "AND channel = 'disk' AND event_type = 'token_count' AND source_session_id IS NOT NULL"
            )
        }
        writes = _rebuild_owners(repository, owners)
        connection.execute(
            "UPDATE issues SET resolved_at_ns = ? WHERE source = 'codex' "
            "AND category = 'conflicting_owner_event' AND resolved_at_ns IS NULL",
            (time.time_ns(),),
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


def _legacy_owners(repository: Repository) -> set[str]:
    rows = repository.connection.execute(
        "SELECT DISTINCT o.source_session_id FROM usage_events e "
        "JOIN observation_links l ON l.usage_event_id = e.id "
        "JOIN observations o ON o.id = l.observation_id "
        "WHERE e.source = 'codex' AND l.method = 'owner_ordinal' "
        "AND o.source = 'codex' AND o.channel = 'disk' "
        "AND o.event_type = 'token_count' AND o.source_session_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM observation_links migrated "
        "WHERE migrated.observation_id = o.id AND migrated.method = 'usage_fingerprint')"
    ).fetchall()
    return {str(row[0]) for row in rows}
