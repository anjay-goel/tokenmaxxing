from collections.abc import Mapping
from datetime import datetime
from typing import cast

from tokenmaxxing.ingest.jsonl import SourceLine
from tokenmaxxing.models import (
    IssueDraft,
    LinkDraft,
    ObservationDraft,
    Projection,
    RunDraft,
    SessionDraft,
    TokenUsage,
    UsageEventDraft,
)
from tokenmaxxing.privacy import PrivacyError, validate_projection

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
