import json
from collections.abc import Mapping
from dataclasses import replace
from typing import cast

from tokenmaxxing.ingest.jsonl import SourceLine
from tokenmaxxing.ingest.pi.parse import (
    _COST_COLUMNS,
    _PARSER_VERSION,
    _PI_SUBAGENT_DISK_METADATA_FIELDS,
    _SUBAGENT_STATUSES,
    _SUBAGENT_STATUS_CODES,
    _TERMINAL_STATUSES,
    _TOKEN_COLUMNS,
    PiState,
    _SubagentRecord,
    _mapping,
    _milliseconds_ns,
    _nonnegative_int,
    _physical_key,
    _issue,
    _route,
    _safe_usage_metadata,
    _string,
    _subagent_usage,
    _timestamp_ns,
)
from tokenmaxxing.models import (
    CostUsage,
    LinkDraft,
    ObservationDraft,
    Projection,
    RunDraft,
    TokenUsage,
    UsageEventDraft,
)
from tokenmaxxing.repository import Repository

def _prefer_subagent(
    current: _SubagentRecord | None, candidate: _SubagentRecord
) -> _SubagentRecord:
    if current is None:
        return candidate
    current_terminal = current.status in _TERMINAL_STATUSES
    candidate_terminal = candidate.status in _TERMINAL_STATUSES
    chosen = candidate if candidate_terminal and not current_terminal else current
    if (
        candidate_terminal == current_terminal
        and candidate.observed_at_ns >= current.observed_at_ns
    ):
        chosen = candidate
    tokens = TokenUsage(
        **{
            name: max(
                getattr(current.tokens, name) or 0,
                getattr(candidate.tokens, name) or 0,
            )
            for name in _TOKEN_COLUMNS
        }
    )
    cost_values: dict[str, object] = {}
    provenance = current.cost
    if (candidate.cost.total_nanos or 0) > (current.cost.total_nanos or 0):
        provenance = candidate.cost
    for name in _COST_COLUMNS:
        left = getattr(current.cost, name)
        right = getattr(candidate.cost, name)
        if name.endswith("_nanos"):
            cost_values[name] = max(cast(int | None, left) or 0, cast(int | None, right) or 0)
        else:
            cost_values[name] = getattr(provenance, name)
    return replace(chosen, tokens=tokens, cost=CostUsage(**cost_values))


def _subagent_event(record: _SubagentRecord) -> UsageEventDraft:
    return UsageEventDraft(
        source="pi",
        event_key=record.event_key,
        granularity="run_aggregate",
        status="provisional",
        tokens=record.tokens,
        cost=record.cost,
        provider=record.provider,
        model=record.model,
        effort=record.effort,
        error_category=(
            record.status if record.status in {"failed", "aborted"} else None
        ),
        started_at_ns=record.started_at_ns,
        completed_at_ns=record.completed_at_ns,
        success=(
            record.status == "completed"
            if record.status in _TERMINAL_STATUSES
            else None
        ),
        replace_metadata_fields=_PI_SUBAGENT_DISK_METADATA_FIELDS,
    )


def _subagent_observation_usage(
    usage_value: Mapping[str, object], record: _SubagentRecord
) -> dict[str, object]:
    usage = _safe_usage_metadata(usage_value)
    metadata: dict[str, object] = {
        "status": _SUBAGENT_STATUS_CODES[record.status],
        "startedAt": record.started_at_ns // 1_000_000
        if record.started_at_ns is not None
        else 0,
    }
    if record.completed_at_ns is not None:
        metadata["finishedAt"] = record.completed_at_ns // 1_000_000
    if record.provider is not None:
        metadata["provider"] = record.provider
    if record.model is not None:
        metadata["model"] = record.model
    if record.effort is not None:
        metadata["effort"] = record.effort
    usage["_pi"] = metadata
    return usage


def project_subagent(
    line: SourceLine, state: PiState, data: Mapping[str, object]
) -> Projection:
    agent_id = _string(data.get("id"))
    status = _string(data.get("status"))
    started_ms = _nonnegative_int(data.get("startedAt"))
    usage_value = _mapping(data.get("usage"))
    lineage_root = state.lineage_root_id
    if (
        agent_id is None
        or status is None
        or started_ms is None
        or usage_value is None
        or lineage_root is None
    ):
        return _issue(line, "invalid_subagent", type(data.get("usage")).__name__)
    parsed = _subagent_usage(usage_value)
    if parsed is None:
        return _issue(line, "invalid_subagent_usage", "usage")
    tokens, cost = parsed
    batch_id = _string(data.get("batchId"))
    if batch_id is not None:
        run_key = f"{batch_id}:{agent_id}"
        event_key = f"pi:subagent:{run_key}"
    else:
        run_key = f"legacy:{lineage_root}:{started_ms}:{agent_id}"
        event_key = f"pi:subagent:{run_key}"
    provider, model = _route(_string(data.get("model")))
    observed_at_ns = _timestamp_ns(line.value.get("timestamp"))
    record = _SubagentRecord(
        event_key=event_key,
        run_key=run_key,
        lineage_root_id=lineage_root,
        physical_session_id=state.session_id or lineage_root,
        batch_id=batch_id,
        agent_id=agent_id,
        role=_string(data.get("role")),
        status=status,
        model=model,
        provider=provider,
        effort=_string(data.get("thinkingLevel")),
        isolation=_string(data.get("isolation")),
        started_at_ns=started_ms * 1_000_000,
        completed_at_ns=_milliseconds_ns(data.get("finishedAt")),
        tokens=tokens,
        cost=cost,
        observed_at_ns=observed_at_ns,
    )
    state.subagent_records[event_key] = _prefer_subagent(
        state.subagent_records.get(event_key), record
    )
    observation_key = _physical_key(line)
    observation = ObservationDraft(
        source="pi",
        channel="disk",
        stable_key=observation_key,
        event_type="subagent_snapshot",
        observed_at_ns=observed_at_ns,
        parser_version=_PARSER_VERSION,
        projection=cast(
            Mapping[str, object],
            {"usage": _subagent_observation_usage(usage_value, record)},
        ),
        source_session_id=state.session_id,
        source_run_id=run_key,
        source_turn_id=event_key,
        client_id=batch_id,
        source_sequence=line.ordinal,
        artifact_id=line.artifact_id,
        ordinal=line.ordinal,
    )
    event = _subagent_event(record)
    state.changed_events.add(event_key)
    return Projection(
        observations=(observation,),
        events=(event,),
        links=(
            LinkDraft(
                source="pi",
                channel="disk",
                observation_key=observation_key,
                event_key=event_key,
                method="subagent_run_key",
                role="primary",
                confidence="exact",
            ),
        ),
    )


def _live_subagent_records(
    repository: Repository, scanned_records: Mapping[str, _SubagentRecord]
) -> dict[str, _SubagentRecord]:
    rows = repository.connection.execute(
        "SELECT o.stable_key, o.source_session_id, o.source_run_id, o.source_turn_id, "
        "o.observed_at_ns, o.projection_json, a.id, o.ordinal, s.root_session_id "
        "FROM observations o "
        "JOIN artifacts a ON a.id = o.artifact_id "
        "LEFT JOIN sessions s ON s.source = 'pi' "
        "AND s.source_session_id = o.source_session_id "
        "WHERE o.source = 'pi' AND o.channel = 'disk' "
        "AND o.event_type = 'subagent_snapshot' "
        "AND o.source_turn_id IS NOT NULL AND o.source_run_id IS NOT NULL "
        "AND COALESCE(a.is_missing, 0) = 0 "
        "AND a.generation = (SELECT MAX(current.generation) FROM artifacts current "
        "WHERE current.source = a.source AND current.path_hash = a.path_hash) "
        "ORDER BY o.source_turn_id, o.observed_at_ns, a.id, o.ordinal"
    ).fetchall()
    grouped: dict[str, list[tuple[_SubagentRecord, int, int]]] = {}
    for row in rows:
        projection = json.loads(str(row[5]))
        usage = _mapping(projection.get("usage") if isinstance(projection, dict) else None)
        metadata = _mapping(usage.get("_pi")) if usage is not None else None
        if usage is None or metadata is None:
            continue
        status_code = _nonnegative_int(metadata.get("status"))
        status = _SUBAGENT_STATUSES.get(status_code) if status_code is not None else None
        parsed = _subagent_usage(usage)
        event_key = _string(row[3])
        run_key = _string(row[2])
        physical_session_id = _string(row[1])
        if (
            status is None
            or parsed is None
            or event_key is None
            or run_key is None
            or physical_session_id is None
        ):
            continue
        started_ms = _nonnegative_int(metadata.get("startedAt"))
        finished_ms = _nonnegative_int(metadata.get("finishedAt"))
        batch_id = None if run_key.startswith("legacy:") else run_key.rpartition(":")[0]
        record = _SubagentRecord(
            event_key=event_key,
            run_key=run_key,
            lineage_root_id=_string(row[8]) or physical_session_id,
            physical_session_id=physical_session_id,
            batch_id=batch_id,
            agent_id=run_key.rpartition(":")[2],
            role=None,
            status=status,
            model=_string(metadata.get("model")),
            provider=_string(metadata.get("provider")),
            effort=_string(metadata.get("effort")),
            isolation=None,
            started_at_ns=(
                started_ms * 1_000_000 if started_ms is not None else None
            ),
            completed_at_ns=(
                finished_ms * 1_000_000 if finished_ms is not None else None
            ),
            tokens=parsed[0],
            cost=parsed[1],
            observed_at_ns=int(row[4]),
        )
        grouped.setdefault(event_key, []).append((record, int(row[6]), int(row[7])))

    reduced: dict[str, _SubagentRecord] = {}
    for event_key, candidates in grouped.items():
        terminal = [candidate for candidate in candidates if candidate[0].status in _TERMINAL_STATUSES]
        selected, _, _ = max(
            terminal or candidates,
            key=lambda candidate: (
                candidate[0].observed_at_ns,
                candidate[1],
                candidate[2],
            ),
        )
        tokens = TokenUsage(
            **{
                name: max(getattr(record.tokens, name) or 0 for record, _, _ in candidates)
                for name in _TOKEN_COLUMNS
            }
        )
        cost_provenance = max(
            candidates,
            key=lambda candidate: candidate[0].cost.total_nanos or 0,
        )[0].cost
        cost = CostUsage(
            **{
                name: max(
                    cast(int | None, getattr(record.cost, name)) or 0
                    for record, _, _ in candidates
                )
                for name in _COST_COLUMNS
                if name.endswith("_nanos")
            },
            original_decimal=cost_provenance.original_decimal,
            source=cost_provenance.source,
            estimated=cost_provenance.estimated,
        )
        starts = [record.started_at_ns for record, _, _ in candidates if record.started_at_ns is not None]
        record = replace(
            selected,
            tokens=tokens,
            cost=cost,
            started_at_ns=min(starts) if starts else None,
        )
        scanned = scanned_records.get(event_key)
        if (
            scanned is not None
            and scanned.status == selected.status
            and scanned.observed_at_ns == selected.observed_at_ns
        ):
            record = replace(
                record,
                role=scanned.role,
                isolation=scanned.isolation,
            )
        reduced[event_key] = record
    return reduced


def _repair_subagent_runs(
    repository: Repository,
    records: Mapping[str, _SubagentRecord],
) -> None:
    records = _live_subagent_records(repository, records)
    if not records:
        return
    with repository.transaction() as connection:
        for record in records.values():
            existing = connection.execute(
                "SELECT s.source_session_id, r.role, r.model, r.provider, r.effort, "
                "r.isolation FROM runs r "
                "JOIN sessions s ON s.id = r.session_id "
                "WHERE s.source = 'pi' AND r.source_run_id = ? "
                "ORDER BY r.id LIMIT 1",
                (record.run_key,),
            ).fetchone()
            root_session = connection.execute(
                "SELECT 1 FROM sessions WHERE source = 'pi' AND source_session_id = ?",
                (record.lineage_root_id,),
            ).fetchone()
            source_session_id = (
                str(existing[0])
                if existing is not None
                else (
                    record.lineage_root_id
                    if root_session is not None
                    else record.physical_session_id
                )
            )
            if existing is not None:
                record = replace(
                    record,
                    role=record.role if record.role is not None else cast(str | None, existing[1]),
                    model=record.model if record.model is not None else cast(str | None, existing[2]),
                    provider=(
                        record.provider
                        if record.provider is not None
                        else cast(str | None, existing[3])
                    ),
                    effort=record.effort if record.effort is not None else cast(str | None, existing[4]),
                    isolation=(
                        record.isolation
                        if record.isolation is not None
                        else cast(str | None, existing[5])
                    ),
                )
            duration = None
            if record.started_at_ns is not None and record.completed_at_ns is not None:
                duration = max(0, record.completed_at_ns - record.started_at_ns)
            runs = (
                RunDraft(
                    source="pi",
                    source_session_id=source_session_id,
                    source_run_id=record.run_key,
                    parent_run_id=record.lineage_root_id,
                    batch_id=record.batch_id,
                    role=record.role,
                    status=record.status,
                    model=record.model,
                    provider=record.provider,
                    effort=record.effort,
                    isolation=record.isolation,
                    started_at_ns=record.started_at_ns,
                    completed_at_ns=record.completed_at_ns,
                    duration_ns=duration,
                ),
            )
            event = _subagent_event(record)
            current = repository.get_event(record.event_key)
            event_is_current = (
                current is not None
                and current.status == "canonical"
                and current.granularity == event.granularity
                and all(
                    (getattr(current.tokens, field) or 0)
                    == (getattr(event.tokens, field) or 0)
                    for field in _TOKEN_COLUMNS
                )
                and all(
                    (getattr(current.cost, field) or 0)
                    == (getattr(event.cost, field) or 0)
                    for field in _COST_COLUMNS
                    if field.endswith("_nanos")
                )
                and all(
                    getattr(current.cost, field) == getattr(event.cost, field)
                    for field in _COST_COLUMNS
                    if not field.endswith("_nanos")
                )
                and all(
                    getattr(current, field) == getattr(event, field)
                    for field in event.replace_metadata_fields
                )
            )
            repository.apply_projection_in_transaction(
                connection,
                Projection(
                    runs=runs,
                    events=() if event_is_current else (event,),
                ),
            )
            connection.execute(
                "UPDATE runs SET parent_run_id = ?, batch_id = ?, role = ?, "
                "status = ?, model = ?, provider = ?, effort = ?, isolation = ?, "
                "started_at_ns = ?, completed_at_ns = ?, duration_ns = ? "
                "WHERE session_id = (SELECT id FROM sessions "
                "WHERE source = 'pi' AND source_session_id = ?) "
                "AND source_run_id = ?",
                (
                    record.lineage_root_id,
                    record.batch_id,
                    record.role,
                    record.status,
                    record.model,
                    record.provider,
                    record.effort,
                    record.isolation,
                    record.started_at_ns,
                    record.completed_at_ns,
                    duration,
                    source_session_id,
                    record.run_key,
                ),
            )
