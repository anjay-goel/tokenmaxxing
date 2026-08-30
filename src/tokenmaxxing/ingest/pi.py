import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import cast

from tokenmaxxing.config import hash_workspace, load_or_create_salt
from tokenmaxxing.ingest.jsonl import (
    SourceLine,
    _database_path,
    _latest_artifact,
    _needs_new_generation,
    _path_hash,
    _prefix_fingerprint,
    scan_jsonl,
)
from tokenmaxxing.models import (
    CostUsage,
    IssueDraft,
    LinkDraft,
    ObservationDraft,
    Projection,
    RunDraft,
    SampleDraft,
    SessionDraft,
    SyncStats,
    TokenUsage,
    UsageEventDraft,
    WriteStats,
    decimal_to_nanodollars,
)
from tokenmaxxing.privacy import PrivacyError, validate_projection
from tokenmaxxing.repository import Repository


_PARSER_VERSION = "pi-disk-v1"
_NANODOLLARS_PER_DOLLAR = Decimal("1000000000")
_TERMINAL_STATUSES = frozenset({"completed", "failed", "aborted"})
_SUBAGENT_STATUS_CODES = {
    "queued": 0,
    "running": 1,
    "completed": 2,
    "failed": 3,
    "aborted": 4,
}
_SUBAGENT_STATUSES = {code: status for status, code in _SUBAGENT_STATUS_CODES.items()}
_TOKEN_COLUMNS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_tokens",
    "cache_write": "cache_write_tokens",
    "cache_write_5m": "cache_write_5m_tokens",
    "cache_write_1h": "cache_write_1h_tokens",
    "reasoning": "reasoning_tokens",
    "reported_total": "reported_total_tokens",
    "derived_total": "derived_total_tokens",
}
_COST_COLUMNS = {
    "input_nanos": "input_cost_nanos",
    "output_nanos": "output_cost_nanos",
    "cache_read_nanos": "cache_read_cost_nanos",
    "cache_write_nanos": "cache_write_cost_nanos",
    "total_nanos": "total_cost_nanos",
    "original_decimal": "original_cost_decimal",
    "source": "cost_source",
    "estimated": "cost_estimated",
}
_PI_DIRECT_DISK_METADATA_FIELDS = frozenset(
    {
        "provider",
        "api",
        "model",
        "response_model",
        "effort",
        "stop_reason",
        "error_category",
        "completed_at_ns",
        "success",
    }
)
_PI_SUBAGENT_DISK_METADATA_FIELDS = frozenset(
    {
        "provider",
        "model",
        "effort",
        "error_category",
        "started_at_ns",
        "completed_at_ns",
        "success",
    }
)


@dataclass(frozen=True, slots=True)
class _Header:
    path: Path
    session_id: str
    version: int | None
    timestamp: str | None
    cwd: str | None
    parent_path: Path | None


@dataclass(frozen=True, slots=True)
class _SubagentRecord:
    event_key: str
    run_key: str
    lineage_root_id: str
    physical_session_id: str
    batch_id: str | None
    agent_id: str
    role: str | None
    status: str
    model: str | None
    provider: str | None
    effort: str | None
    isolation: str | None
    started_at_ns: int | None
    completed_at_ns: int | None
    tokens: TokenUsage
    cost: CostUsage
    observed_at_ns: int


@dataclass(slots=True)
class PiState:
    session_id: str | None = None
    lineage_root_id: str | None = None
    parent_session_id: str | None = None
    schema_version: int | None = None
    started_at_ns: int | None = None
    workspace_hash: str | None = None
    current_provider: str | None = None
    initial_model: str | None = None
    current_model: str | None = None
    thinking_level: str | None = None
    changed_events: set[str] = field(default_factory=set)
    subagent_records: dict[str, _SubagentRecord] = field(default_factory=dict)


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


def _milliseconds_ns(value: object) -> int | None:
    integer = _nonnegative_int(value)
    return integer * 1_000_000 if integer is not None else None


def _nanodollars(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    try:
        return decimal_to_nanodollars(str(value))
    except ValueError:
        try:
            decimal = Decimal(str(value))
        except InvalidOperation:
            return None
        if not decimal.is_finite() or decimal < 0:
            return None
        return int(
            (decimal * _NANODOLLARS_PER_DOLLAR).to_integral_value(
                rounding=ROUND_HALF_UP
            )
        )


def _safe_usage_metadata(value: Mapping[str, object]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, nested in value.items():
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


def _usage_tokens(value: Mapping[str, object]) -> TokenUsage | None:
    required: dict[str, int] = {}
    for source_name, target_name in (
        ("input", "input"),
        ("output", "output"),
        ("cacheRead", "cache_read"),
        ("cacheWrite", "cache_write"),
    ):
        token = _nonnegative_int(value.get(source_name))
        if token is None:
            return None
        required[target_name] = token
    reasoning = _nonnegative_int(value.get("reasoning")) if "reasoning" in value else None
    if reasoning is not None and reasoning > required["output"]:
        return None
    one_hour = (
        _nonnegative_int(value.get("cacheWrite1h"))
        if "cacheWrite1h" in value
        else None
    )
    if one_hour is not None and one_hour > required["cache_write"]:
        return None
    reported = (
        _nonnegative_int(value.get("totalTokens"))
        if "totalTokens" in value
        else None
    )
    derived = sum(required.values())
    return TokenUsage(
        **required,
        cache_write_5m=required["cache_write"] - one_hour if one_hour is not None else None,
        cache_write_1h=one_hour,
        reasoning=reasoning,
        reported_total=reported,
        derived_total=derived,
    )


def _usage_cost(value: Mapping[str, object]) -> CostUsage:
    cost = _mapping(value.get("cost"))
    if cost is None:
        return CostUsage()
    total = cost.get("total")
    return CostUsage(
        input_nanos=_nanodollars(cost.get("input")),
        output_nanos=_nanodollars(cost.get("output")),
        cache_read_nanos=_nanodollars(cost.get("cacheRead")),
        cache_write_nanos=_nanodollars(cost.get("cacheWrite")),
        total_nanos=_nanodollars(total),
        original_decimal=(
            str(total)
            if isinstance(total, (int, float)) and not isinstance(total, bool)
            else None
        ),
        source="pi_reported_estimate",
        estimated=True,
    )


def _subagent_usage(value: Mapping[str, object]) -> tuple[TokenUsage, CostUsage] | None:
    components: dict[str, int] = {}
    for source_name, target_name in (
        ("input", "input"),
        ("output", "output"),
        ("cacheRead", "cache_read"),
        ("cacheWrite", "cache_write"),
    ):
        token = _nonnegative_int(value.get(source_name))
        if token is None:
            return None
        components[target_name] = token
    total = sum(components.values())
    cost_total = value.get("cost")
    return (
        TokenUsage(**components, reported_total=total, derived_total=total),
        CostUsage(
            input_nanos=_nanodollars(value.get("costInput")),
            output_nanos=_nanodollars(value.get("costOutput")),
            cache_read_nanos=_nanodollars(value.get("costCacheRead")),
            cache_write_nanos=_nanodollars(value.get("costCacheWrite")),
            total_nanos=_nanodollars(cost_total),
            original_decimal=str(cost_total)
            if isinstance(cost_total, (int, float)) and not isinstance(cost_total, bool)
            else None,
            source="pi_subagent_reported_estimate",
            estimated=True,
        ),
    )


def _physical_key(line: SourceLine) -> str:
    return f"pi:disk:{line.artifact_id}:{line.generation}:{line.ordinal}"


def _issue(line: SourceLine, category: str, observed_type: str) -> Projection:
    return Projection(
        issues=(
            IssueDraft(
                source="pi",
                category=category,
                severity="error",
                identifier=(
                    f"artifact:{line.artifact_id}:generation:{line.generation}:"
                    f"ordinal:{line.ordinal}"
                ),
                field_path=str(line.ordinal),
                observed_type=observed_type,
            ),
        )
    )


def _route(model: str | None) -> tuple[str | None, str | None]:
    if model is None:
        return None, None
    if "/" not in model:
        return None, model
    return cast(tuple[str, str], tuple(model.split("/", 1)))


def _session_projection(state: PiState, observed_at_ns: int) -> Projection:
    if state.session_id is None:
        return Projection()
    return Projection(
        sessions=(
            SessionDraft(
                source="pi",
                source_session_id=state.session_id,
                root_session_id=state.lineage_root_id,
                parent_session_id=state.parent_session_id,
                schema_version=str(state.schema_version)
                if state.schema_version is not None
                else None,
                provider=state.current_provider,
                initial_model=state.initial_model,
                current_model=state.current_model,
                reasoning_effort=state.thinking_level,
                started_at_ns=state.started_at_ns,
                updated_at_ns=observed_at_ns or state.started_at_ns,
                workspace_hash=state.workspace_hash,
            ),
        ),
        runs=(
            RunDraft(
                source="pi",
                source_session_id=state.session_id,
                source_run_id=state.session_id,
                role="session",
                model=state.current_model,
                provider=state.current_provider,
                effort=state.thinking_level,
                started_at_ns=state.started_at_ns,
            ),
        ),
    )


def _direct_projection(
    line: SourceLine,
    state: PiState,
    *,
    slot: str,
    usage_value: Mapping[str, object],
    provider: str | None = None,
    api: str | None = None,
    model: str | None = None,
    response_model: str | None = None,
    response_id: str | None = None,
    client_id: str | None = None,
    stop_reason: str | None = None,
    success: bool | None = None,
    error_category: str | None = None,
) -> Projection:
    entry_id = _string(line.value.get("id"))
    lineage_root = state.lineage_root_id
    if entry_id is None or lineage_root is None:
        return _issue(line, "missing_usage_identity", slot)
    tokens = _usage_tokens(usage_value)
    if tokens is None:
        return _issue(line, "invalid_usage", slot)
    event_key = f"pi:{lineage_root}:{entry_id}:{slot}"
    observation_key = _physical_key(line)
    observed_at_ns = _timestamp_ns(line.value.get("timestamp"))
    observation = ObservationDraft(
        source="pi",
        channel="disk",
        stable_key=observation_key,
        event_type=slot,
        observed_at_ns=observed_at_ns,
        parser_version=_PARSER_VERSION,
        projection=cast(
            Mapping[str, object], {"usage": _safe_usage_metadata(usage_value)}
        ),
        source_session_id=state.session_id,
        source_run_id=state.session_id,
        source_turn_id=event_key,
        response_id=response_id,
        client_id=client_id,
        source_sequence=line.ordinal,
        artifact_id=line.artifact_id,
        ordinal=line.ordinal,
    )
    event = UsageEventDraft(
        source="pi",
        event_key=event_key,
        granularity="model_call",
        status="provisional",
        tokens=tokens,
        cost=_usage_cost(usage_value),
        provider=provider,
        api=api,
        model=model,
        response_model=response_model,
        effort=state.thinking_level,
        stop_reason=stop_reason,
        error_category=error_category,
        completed_at_ns=observed_at_ns or None,
        success=success,
        replace_metadata_fields=_PI_DIRECT_DISK_METADATA_FIELDS,
    )
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
                method="lineage_entry_slot",
                role="primary",
                confidence="exact",
            ),
        ),
    )


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


def _subagent_projection(
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


def project_pi_line(line: SourceLine, state: PiState) -> Projection:
    record_type = line.value.get("type")
    observed_at_ns = _timestamp_ns(line.value.get("timestamp"))
    if record_type == "session":
        return _session_projection(state, observed_at_ns)
    if record_type == "model_change":
        state.current_provider = _string(line.value.get("provider"))
        state.current_model = _string(line.value.get("modelId"))
        if state.initial_model is None:
            state.initial_model = state.current_model
        return _session_projection(state, observed_at_ns)
    if record_type == "thinking_level_change":
        state.thinking_level = _string(line.value.get("thinkingLevel"))
        return _session_projection(state, observed_at_ns)
    if record_type == "message":
        message = _mapping(line.value.get("message"))
        if message is None:
            return Projection()
        role = message.get("role")
        usage_value = _mapping(message.get("usage"))
        if usage_value is None:
            return Projection()
        if role == "assistant":
            stop_reason = _string(message.get("stopReason"))
            return _direct_projection(
                line,
                state,
                slot="assistant",
                usage_value=usage_value,
                provider=_string(message.get("provider")),
                api=_string(message.get("api")),
                model=_string(message.get("model")),
                response_model=_string(message.get("responseModel")),
                response_id=_string(message.get("responseId")),
                stop_reason=stop_reason,
                success=stop_reason not in {"error", "aborted"}
                if stop_reason is not None
                else None,
                error_category=stop_reason
                if stop_reason in {"error", "aborted"}
                else None,
            )
        if role == "toolResult":
            details = _mapping(message.get("details"))
            if (
                message.get("toolName") == "subagent"
                and details is not None
                and isinstance(details.get("records"), list)
            ):
                return Projection(
                    observations=(
                        ObservationDraft(
                            source="pi",
                            channel="disk",
                            stable_key=_physical_key(line),
                            event_type="subagent_batch",
                            observed_at_ns=observed_at_ns,
                            parser_version=_PARSER_VERSION,
                            projection=cast(
                                Mapping[str, object],
                                {"usage": _safe_usage_metadata(usage_value)},
                            ),
                            source_session_id=state.session_id,
                            client_id=_string(message.get("toolCallId")),
                            source_sequence=line.ordinal,
                            artifact_id=line.artifact_id,
                            ordinal=line.ordinal,
                        ),
                    )
                )
            return _direct_projection(
                line,
                state,
                slot="tool_result",
                usage_value=usage_value,
                client_id=_string(message.get("toolCallId")),
                success=message.get("isError") is False,
                error_category="tool_error" if message.get("isError") is True else None,
            )
        return Projection()
    if record_type in {"compaction", "branch_summary"}:
        usage_value = _mapping(line.value.get("usage"))
        if usage_value is None:
            return Projection()
        direct = _direct_projection(
            line,
            state,
            slot=cast(str, record_type),
            usage_value=usage_value,
            success=True,
        )
        tokens_before = _nonnegative_int(line.value.get("tokensBefore"))
        if record_type != "compaction" or tokens_before is None:
            return direct
        return Projection(
            observations=direct.observations,
            events=direct.events,
            links=direct.links,
            samples=(
                SampleDraft(
                    source="pi",
                    channel="disk",
                    stable_key=f"{_physical_key(line)}:tokens_before",
                    sample_type="context_size",
                    observed_at_ns=observed_at_ns,
                    name="tokens_before",
                    unit="tokens",
                    value_integer=tokens_before,
                    attributes={"from_hook": line.value.get("fromHook") is True},
                ),
            ),
        )
    if record_type == "custom" and line.value.get("customType") == "orchestrator-subagent":
        data = _mapping(line.value.get("data"))
        return _subagent_projection(line, state, data) if data is not None else Projection()
    return Projection()


def _read_header(path: Path) -> _Header | None:
    try:
        with path.open("r", encoding="utf-8") as session_file:
            value = json.loads(session_file.readline())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("type") != "session":
        return None
    session_id = _string(value.get("id"))
    if session_id is None:
        return None
    parent = _string(value.get("parentSession"))
    parent_path = None
    if parent is not None:
        candidate = Path(parent).expanduser()
        parent_path = (
            (path.parent / candidate).resolve()
            if not candidate.is_absolute()
            else candidate.resolve()
        )
    return _Header(
        path=path.resolve(),
        session_id=session_id,
        version=_nonnegative_int(value.get("version")),
        timestamp=_string(value.get("timestamp")),
        cwd=_string(value.get("cwd")),
        parent_path=parent_path,
    )


def _discover_jsonl(root: Path) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix == ".jsonl" else []
    if not root.exists():
        return []
    return sorted(root.rglob("*.jsonl"))


def _resolve_header(
    header: _Header,
    headers: dict[Path, _Header],
    seen: set[Path] | None = None,
) -> tuple[str | None, str]:
    if header.parent_path is None:
        return None, header.session_id
    seen = set() if seen is None else seen
    if header.path in seen:
        return None, header.session_id
    seen.add(header.path)
    parent = headers.get(header.parent_path)
    if parent is None and header.parent_path.exists():
        parent = _read_header(header.parent_path)
        if parent is not None:
            headers[parent.path] = parent
    if parent is None:
        return None, header.session_id
    _, root = _resolve_header(parent, headers, seen)
    return parent.session_id, root


def _workspace_hash(repository: Repository, cwd: str | None) -> str | None:
    if cwd is None:
        return None
    connection = repository._database.connection
    salt = load_or_create_salt(_database_path(connection).parent / "salt")
    return hash_workspace(cwd, salt)


def _session_state(
    repository: Repository, session_id: str
) -> tuple[
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
]:
    row = repository._database.connection.execute(
        "SELECT provider, initial_model, current_model, reasoning_effort, "
        "parent_session_id, root_session_id "
        "FROM sessions WHERE source = 'pi' AND source_session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        return None, None, None, None, None, None
    return cast(
        tuple[
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
        ],
        tuple(row),
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


def _artifact_event_keys(repository: Repository, artifact_id: int) -> set[str]:
    rows = repository._database.connection.execute(
        "SELECT DISTINCT source_turn_id FROM observations "
        "WHERE source = 'pi' AND channel = 'disk' AND artifact_id = ? "
        "AND source_turn_id IS NOT NULL",
        (artifact_id,),
    ).fetchall()
    return {str(row[0]) for row in rows}


def _superseded_events(repository: Repository, path: Path) -> set[str]:
    connection = repository._database.connection
    artifact = _latest_artifact(connection, "pi", _path_hash(connection, path))
    if artifact is None:
        return set()
    stat = path.stat()
    prefix = _prefix_fingerprint(path, artifact.size_bytes)
    if not _needs_new_generation(artifact, stat, prefix, None):
        return set()
    return _artifact_event_keys(repository, artifact.id)


def _rediscovered_events(repository: Repository, path: Path) -> set[str]:
    connection = repository._database.connection
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
    connection = repository._database.connection
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
    rows = repository._database.connection.execute(
        "SELECT event_key FROM usage_events WHERE source = 'pi' AND status = 'provisional'"
    ).fetchall()
    return {str(row[0]) for row in rows}


def _stale_events(repository: Repository) -> set[str]:
    rows = repository._database.connection.execute(
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


def _live_subagent_records(
    repository: Repository, scanned_records: Mapping[str, _SubagentRecord]
) -> dict[str, _SubagentRecord]:
    rows = repository._database.connection.execute(
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
    connection = repository._database.connection
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
    repository._database.connection.execute(
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


def sync_pi(repository: Repository, root: Path) -> SyncStats:
    paths = _discover_jsonl(root)
    headers = {
        header.path: header
        for path in paths
        if (header := _read_header(path)) is not None
    }
    changed_events = _mark_missing(repository, root, paths)
    subagent_records: dict[str, _SubagentRecord] = {}
    stats = SyncStats()
    for path in paths:
        header = headers.get(path.resolve())
        if header is None:
            continue
        parent_id, lineage_root = _resolve_header(header, headers)
        (
            provider,
            initial_model,
            current_model,
            thinking_level,
            persisted_parent_id,
            persisted_lineage_root,
        ) = _session_state(repository, header.session_id)
        if (
            header.parent_path is not None
            and parent_id is None
            and persisted_lineage_root is not None
        ):
            parent_id = persisted_parent_id
            lineage_root = persisted_lineage_root
        state = PiState(
            session_id=header.session_id,
            lineage_root_id=lineage_root,
            parent_session_id=parent_id,
            schema_version=header.version,
            started_at_ns=_timestamp_ns(header.timestamp),
            workspace_hash=_workspace_hash(repository, header.cwd),
            current_provider=provider,
            initial_model=initial_model,
            current_model=current_model,
            thinking_level=thinking_level,
            changed_events=changed_events,
            subagent_records=subagent_records,
        )
        changed_events.update(_rediscovered_events(repository, path))
        superseded_events = _superseded_events(repository, path)
        changed_events.update(superseded_events)
        stats = _add_stats(
            stats, scan_jsonl(repository, "pi", path, project_pi_line, state)
        )
    _repair_subagent_runs(repository, subagent_records)
    changed_events.update(_provisional_events(repository))
    changed_events.update(_stale_events(repository))
    return _add_stats(stats, _rebuild_events(repository, changed_events))
