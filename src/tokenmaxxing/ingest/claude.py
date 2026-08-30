import json
import sqlite3
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

from tokenmaxxing.ingest.jsonl import (
    SourceLine,
    _header_session_id,
    _latest_artifact,
    _needs_new_generation,
    _path_hash,
    _prefix_fingerprint,
    scan_jsonl,
)
from tokenmaxxing.models import (
    IssueDraft,
    LinkDraft,
    ObservationDraft,
    Projection,
    RunDraft,
    SessionDraft,
    SyncStats,
    TokenUsage,
    UsageEventDraft,
    WriteStats,
)
from tokenmaxxing.privacy import PrivacyError, validate_projection
from tokenmaxxing.repository import Repository


_PARSER_VERSION = "claude-disk-v1"
_TOKEN_COLUMNS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_tokens",
    "cache_write": "cache_write_tokens",
    "cache_write_5m": "cache_write_5m_tokens",
    "cache_write_1h": "cache_write_1h_tokens",
}
_SOURCE_TOKEN_FIELDS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_read_input_tokens": "cache_read",
    "cache_creation_input_tokens": "cache_write",
}
_DIFFERENCE_COMPONENTS = tuple(_TOKEN_COLUMNS)
_CLAUDE_DISK_METADATA_FIELDS = frozenset(
    {
        "provider",
        "model",
        "service_tier",
        "speed",
        "inference_region",
        "effort",
        "stop_reason",
        "completed_at_ns",
        "web_search_count",
        "web_fetch_count",
    }
)


def _mapping(value: object) -> Mapping[str, object] | None:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _timestamp_ns(value: object) -> int:
    if not isinstance(value, str):
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int(parsed.timestamp() * 1_000_000_000)


def _token_values(value: Mapping[str, object]) -> dict[str, int]:
    result: dict[str, int] = {}
    for source_name, target_name in _SOURCE_TOKEN_FIELDS.items():
        source_value = _nonnegative_int(value.get(source_name))
        if source_value is not None:
            result[target_name] = source_value
    cache_creation = _mapping(value.get("cache_creation"))
    if cache_creation is not None:
        five_minutes = _nonnegative_int(cache_creation.get("ephemeral_5m_input_tokens"))
        one_hour = _nonnegative_int(cache_creation.get("ephemeral_1h_input_tokens"))
        if five_minutes is not None:
            result["cache_write_5m"] = five_minutes
        if one_hour is not None:
            result["cache_write_1h"] = one_hour
    return result


def _valid_token_fields(value: Mapping[str, object]) -> bool:
    for source_name in _SOURCE_TOKEN_FIELDS:
        if source_name in value and _nonnegative_int(value[source_name]) is None:
            return False
    cache_creation = value.get("cache_creation")
    if cache_creation is not None:
        cache_mapping = _mapping(cache_creation)
        if cache_mapping is None:
            return False
        for name in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
            if name in cache_mapping and _nonnegative_int(cache_mapping[name]) is None:
                return False
    return bool(_token_values(value))


def _usage(value: Mapping[str, int], *, complete: bool = False) -> TokenUsage:
    values = {
        name: value.get(name, 0) if complete else value.get(name)
        for name in _DIFFERENCE_COMPONENTS
    }
    return TokenUsage(**{name: token for name, token in values.items() if token is not None})


def _safe_usage_metadata(value: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, nested in value.items():
        if key in {"iterations", "model", "type"}:
            continue
        try:
            projected = validate_projection(
                cast(Mapping[str, object], {"usage": {key: nested}})
            )
        except PrivacyError:
            continue
        usage = projected.get("usage")
        if isinstance(usage, dict) and key in usage:
            result[key] = usage[key]
    return result


def _server_counts(value: Mapping[str, object]) -> tuple[int | None, int | None]:
    server_tools = _mapping(value.get("server_tool_use"))
    if server_tools is None:
        return None, None
    return (
        _nonnegative_int(server_tools.get("web_search_requests")),
        _nonnegative_int(server_tools.get("web_fetch_requests")),
    )


def _physical_key(line: SourceLine) -> str:
    return f"claude:disk:{line.artifact_id}:{line.generation}:{line.ordinal}"


def _issue(
    line: SourceLine,
    category: str,
    observed_type: str,
    *,
    identifier: str | None = None,
) -> Projection:
    return Projection(
        issues=(
            IssueDraft(
                source="claude",
                category=category,
                severity="error",
                identifier=identifier
                or (
                    f"artifact:{line.artifact_id}:generation:{line.generation}:"
                    f"ordinal:{line.ordinal}"
                ),
                field_path=str(line.ordinal),
                observed_type=observed_type,
            ),
        )
    )


def _residual(
    outer: Mapping[str, int], iterations: list[tuple[dict[str, int], bool]]
) -> tuple[dict[str, int] | None, bool]:
    normal_sum = {name: 0 for name in _DIFFERENCE_COMPONENTS}
    for tokens, advisor in iterations:
        if advisor:
            continue
        for name in _DIFFERENCE_COMPONENTS:
            normal_sum[name] += tokens.get(name, 0)
    difference = {
        name: outer.get(name, 0) - normal_sum[name]
        for name in _DIFFERENCE_COMPONENTS
    }
    if any(value < 0 for value in difference.values()):
        return None, True
    if any(value > 0 for value in difference.values()):
        return difference, False
    return None, False


def project_claude_line(line: SourceLine) -> Projection:
    if line.value.get("type") != "assistant":
        return Projection()
    message = _mapping(line.value.get("message"))
    if message is None:
        return _issue(line, "invalid_message", type(line.value.get("message")).__name__)
    usage_value = message.get("usage")
    usage_mapping = _mapping(usage_value)
    if usage_mapping is None or not _valid_token_fields(usage_mapping):
        return _issue(line, "invalid_usage", type(usage_value).__name__)

    message_id = _string(message.get("id"))
    session_id = _string(line.value.get("sessionId")) or _string(
        line.value.get("session_id")
    )
    transcript_uuid = _string(line.value.get("uuid"))
    if message_id is not None:
        semantic_id = message_id
    elif session_id is not None and transcript_uuid is not None:
        semantic_id = f"{session_id}:{transcript_uuid}"
    else:
        return _issue(line, "missing_message_identity", "assistant")

    iterations_value = usage_mapping.get("iterations")
    iteration_records: list[tuple[Mapping[str, object], bool, str | None]] = []
    if iterations_value is not None:
        if not isinstance(iterations_value, list):
            return _issue(line, "invalid_iterations", type(iterations_value).__name__)
        for iteration in iterations_value:
            iteration_mapping = _mapping(iteration)
            if iteration_mapping is None or not _valid_token_fields(iteration_mapping):
                return _issue(line, "invalid_iteration_usage", type(iteration).__name__)
            iteration_type = iteration_mapping.get("type")
            if iteration_type not in {"message", "advisor_message"}:
                return _issue(line, "invalid_iteration_type", type(iteration_type).__name__)
            advisor = iteration_type == "advisor_message"
            advisor_model = (
                _string(iteration_mapping.get("model"))
                or _string(line.value.get("advisorModel"))
                if advisor
                else None
            )
            iteration_records.append((iteration_mapping, advisor, advisor_model))

    observed_at_ns = _timestamp_ns(line.value.get("timestamp"))
    outer_tokens = _token_values(usage_mapping)
    model = _string(message.get("model"))
    request_id = _string(line.value.get("requestId"))
    version = _string(line.value.get("version"))
    entrypoint = _string(line.value.get("entrypoint"))
    effort = _string(line.value.get("effort"))
    is_sidechain = line.value.get("isSidechain") is True
    run_source_id = _string(line.value.get("agentId")) or session_id
    search_count, fetch_count = _server_counts(usage_mapping)
    event_common = {
        "provider": "anthropic",
        "service_tier": _string(usage_mapping.get("service_tier")),
        "speed": _string(usage_mapping.get("speed")),
        "inference_region": _string(usage_mapping.get("inference_geo")),
        "effort": effort,
        "stop_reason": _string(message.get("stop_reason")),
        "completed_at_ns": observed_at_ns or None,
    }

    projected_usage: dict[str, object] = {
        "outer": _safe_usage_metadata(usage_mapping)
    }
    iteration_tokens: list[tuple[dict[str, int], bool]] = []
    for index, (iteration, advisor, _) in enumerate(iteration_records):
        projected_usage[f"iteration_{index}"] = {
            **_safe_usage_metadata(iteration),
            "is_advisor": advisor,
        }
        iteration_tokens.append((_token_values(iteration), advisor))

    observation_key = _physical_key(line)
    observation = ObservationDraft(
        source="claude",
        channel="disk",
        stable_key=observation_key,
        event_type="assistant_sidechain" if is_sidechain else "assistant",
        observed_at_ns=observed_at_ns,
        parser_version=_PARSER_VERSION,
        projection=cast(Mapping[str, object], {"usage": projected_usage}),
        source_session_id=session_id,
        source_run_id=run_source_id,
        source_turn_id=semantic_id,
        response_id=message_id or transcript_uuid,
        request_id=request_id,
        client_id=entrypoint,
        source_sequence=line.ordinal,
        artifact_id=line.artifact_id,
        ordinal=line.ordinal,
    )

    base_key = f"claude:{semantic_id}"
    events: list[UsageEventDraft] = []
    if iteration_records:
        for index, ((tokens, _), (_, advisor, advisor_model)) in enumerate(
            zip(iteration_tokens, iteration_records, strict=True)
        ):
            events.append(
                UsageEventDraft(
                    source="claude",
                    event_key=f"{base_key}:iteration:{index}",
                    granularity="model_call",
                    status="provisional",
                    tokens=_usage(tokens),
                    model=advisor_model if advisor else model,
                    web_search_count=search_count if index == 0 else None,
                    web_fetch_count=fetch_count if index == 0 else None,
                    replace_metadata_fields=_CLAUDE_DISK_METADATA_FIELDS,
                    **event_common,
                )
            )
        residual, _ = _residual(outer_tokens, iteration_tokens)
        if residual is not None:
            events.append(
                UsageEventDraft(
                    source="claude",
                    event_key=f"{base_key}:residual",
                    granularity="model_call",
                    status="provisional",
                    tokens=_usage(residual, complete=True),
                    model=model,
                    replace_metadata_fields=_CLAUDE_DISK_METADATA_FIELDS,
                    **event_common,
                )
            )
    else:
        events.append(
            UsageEventDraft(
                source="claude",
                event_key=base_key,
                granularity="model_call",
                status="provisional",
                tokens=_usage(outer_tokens),
                model=model,
                web_search_count=search_count,
                web_fetch_count=fetch_count,
                replace_metadata_fields=_CLAUDE_DISK_METADATA_FIELDS,
                **event_common,
            )
        )

    method = "message_id" if message_id is not None else "session_transcript_uuid"
    links = tuple(
        LinkDraft(
            source="claude",
            channel="disk",
            observation_key=observation_key,
            event_key=event.event_key,
            method=method,
            role="primary",
            confidence="exact",
        )
        for event in events
    )
    sessions = (
        (
            SessionDraft(
                source="claude",
                source_session_id=session_id,
                root_session_id=session_id,
                harness_version=version,
                provider="anthropic",
                initial_model=model,
                current_model=model,
                started_at_ns=observed_at_ns or None,
                updated_at_ns=observed_at_ns or None,
            ),
        )
        if session_id is not None
        else ()
    )
    role = entrypoint
    if is_sidechain:
        role = f"{entrypoint}:sidechain" if entrypoint is not None else "sidechain"
    runs = (
        (
            RunDraft(
                source="claude",
                source_session_id=session_id,
                source_run_id=run_source_id,
                parent_run_id=session_id if run_source_id != session_id else None,
                role=role,
                model=model,
                started_at_ns=observed_at_ns or None,
            ),
        )
        if session_id is not None and run_source_id is not None
        else ()
    )
    return Projection(
        observations=(observation,),
        events=tuple(events),
        links=links,
        sessions=sessions,
        runs=runs,
    )


def _add_stats(total: SyncStats, added: SyncStats | WriteStats) -> SyncStats:
    return SyncStats(
        artifacts_seen=total.artifacts_seen + getattr(added, "artifacts_seen", 0),
        lines_read=total.lines_read + getattr(added, "lines_read", 0),
        observations_inserted=total.observations_inserted + added.observations_inserted,
        events_inserted=total.events_inserted + added.events_inserted,
        events_updated=total.events_updated + added.events_updated,
        issues_recorded=total.issues_recorded + added.issues_recorded,
    )


def _discover_jsonl(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix == ".jsonl" else []
    if not root.exists():
        return []
    return sorted(root.rglob("*.jsonl"))


def _superseded_messages(repository: Repository, path: Path) -> set[str]:
    connection = repository._database.connection
    artifact = _latest_artifact(connection, "claude", _path_hash(connection, path))
    if artifact is None:
        return set()
    stat = path.stat()
    prefix = _prefix_fingerprint(path, artifact.size_bytes)
    if not _needs_new_generation(
        artifact, stat, prefix, _header_session_id(path)
    ):
        return set()
    rows = connection.execute(
        "SELECT DISTINCT source_turn_id FROM observations "
        "WHERE source = 'claude' AND channel = 'disk' AND artifact_id = ? "
        "AND source_turn_id IS NOT NULL",
        (artifact.id,),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _rediscovered_messages(repository: Repository, path: Path) -> set[str]:
    connection = repository._database.connection
    artifact = _latest_artifact(connection, "claude", _path_hash(connection, path))
    if artifact is None:
        return set()
    row = connection.execute(
        "SELECT is_missing FROM artifacts WHERE id = ?",
        (artifact.id,),
    ).fetchone()
    if row is None or row[0] != 1:
        return set()
    with repository.transaction() as transaction:
        messages = {
            str(message[0])
            for message in transaction.execute(
                "SELECT DISTINCT source_turn_id FROM observations "
                "WHERE source = 'claude' AND channel = 'disk' AND artifact_id = ? "
                "AND source_turn_id IS NOT NULL",
                (artifact.id,),
            ).fetchall()
        }
        transaction.execute(
            "UPDATE artifacts SET is_missing = 0 WHERE id = ?",
            (artifact.id,),
        )
        transaction.execute(
            "UPDATE usage_events SET status = 'provisional' WHERE id IN ("
            "SELECT l.usage_event_id FROM observation_links l "
            "JOIN observations o ON o.id = l.observation_id "
            "WHERE o.source = 'claude' AND o.channel = 'disk' "
            "AND o.artifact_id = ?)",
            (artifact.id,),
        )
    return messages


def _component_max(target: dict[str, int], candidate: Mapping[str, int]) -> None:
    for name, value in candidate.items():
        target[name] = max(target.get(name, 0), value)


def _linked_events(
    repository: Repository, semantic_id: str
) -> dict[str, tuple[int, str | None]]:
    rows = repository._database.connection.execute(
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
    session_row = repository._database.connection.execute(
        "SELECT id FROM sessions WHERE source = 'claude' AND source_session_id = ?",
        (session_id,),
    ).fetchone()
    if session_row is None:
        return None, None
    canonical_session_id = int(session_row[0])
    if run_source_id is None:
        return canonical_session_id, None
    run_row = repository._database.connection.execute(
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
    cursor = repository._database.connection.execute(
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
    repository._database.connection.execute(
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
    row = repository._database.connection.execute(
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
    connection = repository._database.connection
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
    rows = repository._database.connection.execute(
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
    rows = repository._database.connection.execute(
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


def sync_claude(repository: Repository, root: Path) -> SyncStats:
    stats = SyncStats()
    changed_messages: set[str] = set()
    for path in _discover_jsonl(root):
        changed_messages.update(_rediscovered_messages(repository, path))
        changed_messages.update(_superseded_messages(repository, path))

        def project(line: SourceLine, _: object) -> Projection:
            projection = project_claude_line(line)
            for observation in projection.observations:
                if observation.source_turn_id is not None:
                    changed_messages.add(observation.source_turn_id)
            return projection

        stats = _add_stats(
            stats,
            scan_jsonl(repository, "claude", path, project, object()),
        )
    changed_messages.update(_provisional_messages(repository))
    changed_messages.update(_stale_counted_messages(repository))
    return _add_stats(stats, _rebuild_messages(repository, changed_messages))
