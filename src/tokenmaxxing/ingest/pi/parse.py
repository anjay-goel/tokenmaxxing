from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from tokenmaxxing.ingest.jsonl import SourceLine
from tokenmaxxing.models import (
    CostUsage,
    IssueDraft,
    LinkDraft,
    ObservationDraft,
    Projection,
    RunDraft,
    SampleDraft,
    SessionDraft,
    TokenUsage,
    UsageEventDraft,
    decimal_to_nanodollars,
)
from tokenmaxxing.privacy import PrivacyError, validate_projection

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
        if data is None:
            return Projection()
        from tokenmaxxing.ingest.pi.subagents import project_subagent

        return project_subagent(line, state, data)
    return Projection()
