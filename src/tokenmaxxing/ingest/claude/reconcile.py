import json
import sqlite3
import time
from collections.abc import Mapping
from typing import cast

from tokenmaxxing.ingest.claude.parse import (
    _TOKEN_COLUMNS,
    _mapping,
    _residual,
    _server_counts,
    _string,
    _token_values,
    _usage,
)
from tokenmaxxing.models import (
    IssueDraft,
    LinkDraft,
    Projection,
    TokenUsage,
    UsageEventDraft,
    WriteStats,
)
from tokenmaxxing.repository import Repository

def _component_max(target: dict[str, int], candidate: Mapping[str, int]) -> None:
    for name, value in candidate.items():
        target[name] = max(target.get(name, 0), value)


def _linked_events(
    repository: Repository, semantic_id: str
) -> dict[str, tuple[int, str | None]]:
    rows = repository.connection.execute(
        "SELECT DISTINCT e.id, e.event_key, e.model FROM observations o "
        "INDEXED BY idx_observations_source_turn "
        "CROSS JOIN observation_links l "
        "CROSS JOIN usage_events e "
        "WHERE o.source = 'claude' AND o.source_turn_id = ? "
        "AND l.observation_id = o.id AND e.id = l.usage_event_id "
        "AND e.source = 'claude'",
        (semantic_id,),
    ).fetchall()
    return {str(row[1]): (int(row[0]), _string(row[2])) for row in rows}


def _session_and_run_ids(
    repository: Repository,
    session_id: str | None,
    run_source_id: str | None,
) -> tuple[int | None, int | None]:
    if session_id is None:
        return None, None
    session_row = repository.connection.execute(
        "SELECT id FROM sessions WHERE source = 'claude' AND source_session_id = ?",
        (session_id,),
    ).fetchone()
    if session_row is None:
        return None, None
    canonical_session_id = int(session_row[0])
    if run_source_id is None:
        return canonical_session_id, None
    run_row = repository.connection.execute(
        "SELECT id FROM runs WHERE session_id = ? AND source_run_id = ?",
        (canonical_session_id, run_source_id),
    ).fetchone()
    return canonical_session_id, int(run_row[0]) if run_row is not None else None


def _event_metadata(
    repository: Repository, event_ids: list[int]
) -> dict[str, object]:
    if not event_ids:
        return {}
    placeholders = ", ".join("?" for _ in event_ids)
    cursor = repository.connection.execute(
        "SELECT provider, model, service_tier, speed, inference_region, effort, "
        "stop_reason, completed_at_ns FROM usage_events "
        f"WHERE id IN ({placeholders}) ORDER BY id DESC LIMIT 1",
        tuple(event_ids),
    )
    row = cursor.fetchone()
    if row is None:
        return {}
    return {
        description[0]: row[index]
        for index, description in enumerate(cursor.description or ())
    }


def _update_event_tokens(
    repository: Repository,
    event_key: str,
    status: str,
    tokens: TokenUsage,
    *,
    web_search_count: int | None,
    web_fetch_count: int | None,
) -> None:
    assignments = ["status = ?"]
    values: list[object] = [status]
    for attribute, column in _TOKEN_COLUMNS.items():
        assignments.append(f"{column} = ?")
        values.append(getattr(tokens, attribute))
    assignments.extend(("web_search_count = ?", "web_fetch_count = ?"))
    values.extend((web_search_count, web_fetch_count, event_key))
    repository.connection.execute(
        f"UPDATE usage_events SET {', '.join(assignments)} "
        "WHERE source = 'claude' AND event_key = ?",
        tuple(values),
    )


def _event_is_current(
    repository: Repository,
    event_key: str,
    status: str,
    tokens: TokenUsage,
    *,
    session_id: int | None,
    run_id: int | None,
    web_search_count: int | None,
    web_fetch_count: int | None,
) -> bool:
    token_attributes = tuple(_TOKEN_COLUMNS)
    row = repository.connection.execute(
        "SELECT status, session_id, run_id, web_search_count, web_fetch_count, "
        f"{', '.join(_TOKEN_COLUMNS.values())} FROM usage_events "
        "WHERE source = 'claude' AND event_key = ?",
        (event_key,),
    ).fetchone()
    if row is None:
        return False
    expected = (
        status,
        session_id,
        run_id,
        web_search_count,
        web_fetch_count,
        *(getattr(tokens, attribute) for attribute in token_attributes),
    )
    return tuple(row) == expected


def _rebuild_messages(repository: Repository, messages: set[str]) -> WriteStats:
    if not messages:
        return WriteStats()
    source_messages = sorted(messages)
    connection = repository.connection
    chunk_size = min(
        500, connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER)
    )
    rows: list[tuple[object, ...]] = []
    for offset in range(0, len(source_messages), chunk_size):
        message_chunk = source_messages[offset : offset + chunk_size]
        placeholders = ", ".join("?" for _ in message_chunk)
        rows.extend(
            connection.execute(
                "SELECT o.source_turn_id, o.source_session_id, o.source_run_id, o.stable_key, "
                "o.observed_at_ns, o.projection_json FROM observations o "
                "JOIN artifacts a ON a.id = o.artifact_id "
                "WHERE o.source = 'claude' AND o.channel = 'disk' "
                f"AND o.source_turn_id IN ({placeholders}) "
                "AND COALESCE(a.is_missing, 0) = 0 "
                "AND a.generation = (SELECT MAX(current.generation) FROM artifacts current "
                "WHERE current.source = a.source AND current.path_hash = a.path_hash) "
                "ORDER BY o.source_turn_id, o.observed_at_ns, o.artifact_id, o.ordinal",
                tuple(message_chunk),
            ).fetchall()
        )
    grouped: dict[str, list[tuple[object, ...]]] = {message: [] for message in messages}
    for row in rows:
        semantic_id = row[0]
        if isinstance(semantic_id, str):
            grouped.setdefault(semantic_id, []).append(tuple(row))

    writes = WriteStats()
    with repository.transaction() as connection:
        for semantic_id in sorted(messages):
            message_rows = grouped.get(semantic_id, [])
            linked = _linked_events(repository, semantic_id)
            if not message_rows:
                if linked:
                    connection.executemany(
                        "UPDATE usage_events SET status = 'excluded' WHERE id = ?",
                        ((event_id,) for event_id, _ in linked.values()),
                    )
                continue

            outer: dict[str, int] = {}
            iteration_usage: dict[int, dict[str, int]] = {}
            advisors: dict[int, bool] = {}
            observations: list[str] = []
            session_id: str | None = None
            run_source_id: str | None = None
            completed_at_ns = 0
            search_count: int | None = None
            fetch_count: int | None = None
            for (
                _,
                session_value,
                run_value,
                stable_key,
                observed_at,
                projection_text,
            ) in message_rows:
                if isinstance(session_value, str):
                    session_id = session_value
                if isinstance(run_value, str):
                    run_source_id = run_value
                if isinstance(stable_key, str):
                    observations.append(stable_key)
                if isinstance(observed_at, int):
                    completed_at_ns = max(completed_at_ns, observed_at)
                projection = json.loads(str(projection_text))
                usage_projection = (
                    projection.get("usage") if isinstance(projection, dict) else None
                )
                if not isinstance(usage_projection, dict):
                    continue
                outer_projection = _mapping(usage_projection.get("outer"))
                if outer_projection is not None:
                    _component_max(outer, _token_values(outer_projection))
                    candidate_search, candidate_fetch = _server_counts(outer_projection)
                    if candidate_search is not None:
                        search_count = max(search_count or 0, candidate_search)
                    if candidate_fetch is not None:
                        fetch_count = max(fetch_count or 0, candidate_fetch)
                for key, value in usage_projection.items():
                    if not isinstance(key, str) or not key.startswith("iteration_"):
                        continue
                    try:
                        index = int(key.removeprefix("iteration_"))
                    except ValueError:
                        continue
                    iteration = _mapping(value)
                    if iteration is None:
                        continue
                    current = iteration_usage.setdefault(index, {})
                    _component_max(current, _token_values(iteration))
                    advisors[index] = advisors.get(index, False) or iteration.get(
                        "is_advisor"
                    ) is True

            canonical_session_id, run_id = _session_and_run_ids(
                repository, session_id, run_source_id
            )
            base_key = f"claude:{semantic_id}"
            desired: list[tuple[str, TokenUsage, str, str | None]] = []
            conflict = False
            if iteration_usage:
                normalized_iterations = [
                    (iteration_usage[index], advisors.get(index, False))
                    for index in sorted(iteration_usage)
                ]
                residual, conflict = _residual(outer, normalized_iterations)
                for index in sorted(iteration_usage):
                    key = f"{base_key}:iteration:{index}"
                    existing_model = linked.get(key, (0, None))[1]
                    desired.append(
                        (
                            key,
                            _usage(iteration_usage[index]),
                            "canonical"
                            if advisors.get(index, False) or not conflict
                            else "conflicted",
                            existing_model,
                        )
                    )
                if residual is not None:
                    residual_key = f"{base_key}:residual"
                    residual_model = linked.get(residual_key, (0, None))[1]
                    if residual_model is None:
                        residual_model = next(
                            (
                                linked.get(f"{base_key}:iteration:{index}", (0, None))[1]
                                for index in sorted(iteration_usage)
                                if not advisors.get(index, False)
                            ),
                            None,
                        )
                    desired.append(
                        (
                            residual_key,
                            _usage(residual, complete=True),
                            "canonical",
                            residual_model,
                        )
                    )
            else:
                desired.append(
                    (
                        base_key,
                        _usage(outer),
                        "canonical",
                        linked.get(base_key, (0, None))[1],
                    )
                )

            active_keys = {event_key for event_key, _, _, _ in desired}
            metadata = _event_metadata(
                repository,
                [
                    event_id
                    for event_key, (event_id, _) in linked.items()
                    if event_key in active_keys
                ],
            )
            for linked_key, (event_id, _) in linked.items():
                if linked_key not in active_keys:
                    connection.execute(
                        "UPDATE usage_events SET status = 'excluded' WHERE id = ?",
                        (event_id,),
                    )

            for position, (event_key, tokens, status, event_model) in enumerate(desired):
                event_search_count = search_count if position == 0 else None
                event_fetch_count = fetch_count if position == 0 else None
                event = UsageEventDraft(
                    source="claude",
                    event_key=event_key,
                    granularity="model_call",
                    status=cast(str, status),
                    tokens=tokens,
                    session_id=canonical_session_id,
                    run_id=run_id,
                    provider=_string(metadata.get("provider")) or "anthropic",
                    model=event_model or _string(metadata.get("model")),
                    service_tier=_string(metadata.get("service_tier")),
                    speed=_string(metadata.get("speed")),
                    inference_region=_string(metadata.get("inference_region")),
                    effort=_string(metadata.get("effort")),
                    stop_reason=_string(metadata.get("stop_reason")),
                    completed_at_ns=completed_at_ns or None,
                    web_search_count=event_search_count,
                    web_fetch_count=event_fetch_count,
                )
                event_is_current = _event_is_current(
                    repository,
                    event_key,
                    status,
                    tokens,
                    session_id=canonical_session_id,
                    run_id=run_id,
                    web_search_count=event_search_count,
                    web_fetch_count=event_fetch_count,
                )
                added = repository.apply_projection_in_transaction(
                    connection,
                    Projection(
                        events=() if event_is_current else (event,),
                        links=tuple(
                            LinkDraft(
                                source="claude",
                                channel="disk",
                                observation_key=observation_key,
                                event_key=event_key,
                                method="message_id",
                                role="primary",
                                confidence="exact",
                            )
                            for observation_key in observations
                        ),
                    ),
                )
                writes = WriteStats(
                    observations_inserted=writes.observations_inserted
                    + added.observations_inserted,
                    events_inserted=writes.events_inserted + added.events_inserted,
                    events_updated=writes.events_updated + added.events_updated,
                    links_inserted=writes.links_inserted + added.links_inserted,
                    samples_inserted=writes.samples_inserted + added.samples_inserted,
                    issues_recorded=writes.issues_recorded + added.issues_recorded,
                )
                if not event_is_current:
                    _update_event_tokens(
                        repository,
                        event_key,
                        status,
                        tokens,
                        web_search_count=event_search_count,
                        web_fetch_count=event_fetch_count,
                    )
            if conflict:
                issue_write = repository.apply_projection_in_transaction(
                    connection,
                    Projection(
                        issues=(
                            IssueDraft(
                                source="claude",
                                category="iteration_usage_conflict",
                                severity="error",
                                identifier=base_key,
                                observed_type="usage",
                            ),
                        )
                    ),
                )
                writes = WriteStats(
                    observations_inserted=writes.observations_inserted,
                    events_inserted=writes.events_inserted,
                    events_updated=writes.events_updated,
                    links_inserted=writes.links_inserted,
                    samples_inserted=writes.samples_inserted,
                    issues_recorded=writes.issues_recorded
                    + issue_write.issues_recorded,
                )
            else:
                connection.execute(
                    "UPDATE issues SET resolved_at_ns = ? "
                    "WHERE source = 'claude' AND category = 'iteration_usage_conflict' "
                    "AND identifier = ? AND resolved_at_ns IS NULL",
                    (time.time_ns(), base_key),
                )
    return writes


def _provisional_messages(repository: Repository) -> set[str]:
    rows = repository.connection.execute(
        "SELECT DISTINCT o.source_turn_id FROM usage_events e "
        "INDEXED BY idx_usage_events_status "
        "CROSS JOIN observation_links l INDEXED BY idx_observation_links_usage_event "
        "CROSS JOIN observations o "
        "WHERE o.source = 'claude' AND o.channel = 'disk' "
        "AND e.status = 'provisional' AND o.source_turn_id IS NOT NULL "
        "AND l.usage_event_id = e.id AND o.id = l.observation_id"
    ).fetchall()
    return {str(row[0]) for row in rows}
def _stale_counted_messages(repository: Repository) -> set[str]:
    rows = repository.connection.execute(
        "SELECT DISTINCT old.source_turn_id FROM observations old "
        "JOIN observation_links l ON l.observation_id = old.id "
        "JOIN usage_events e ON e.id = l.usage_event_id "
        "WHERE old.source = 'claude' AND old.channel = 'disk' "
        "AND old.source_turn_id IS NOT NULL "
        "AND e.status IN ('canonical', 'provisional') "
        "AND (NOT EXISTS ("
        "SELECT 1 FROM observations live "
        "INDEXED BY idx_observations_claude_turn_artifact "
        "JOIN artifacts a ON a.id = live.artifact_id "
        "WHERE live.source = 'claude' AND live.channel = 'disk' "
        "AND live.source_turn_id = old.source_turn_id "
        "AND COALESCE(a.is_missing, 0) = 0 "
        "AND a.generation = (SELECT MAX(current.generation) FROM artifacts current "
        "WHERE current.source = a.source AND current.path_hash = a.path_hash)) "
        "OR EXISTS ("
        "SELECT 1 FROM observations missing "
        "INDEXED BY idx_observations_claude_turn_artifact "
        "JOIN artifacts a ON a.id = missing.artifact_id "
        "WHERE missing.source = 'claude' AND missing.channel = 'disk' "
        "AND missing.source_turn_id = old.source_turn_id "
        "AND a.is_missing = 1 "
        "AND a.generation = (SELECT MAX(current.generation) FROM artifacts current "
        "WHERE current.source = a.source AND current.path_hash = a.path_hash)))"
    ).fetchall()
    return {str(row[0]) for row in rows}

