from collections.abc import Mapping
from dataclasses import dataclass
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

_PARSER_VERSION = "codex-disk-v1"
_TOKEN_FIELDS = (
    "input",
    "output",
    "cache_read",
    "cache_write",
    "cache_write_5m",
    "cache_write_1h",
    "reasoning",
    "reported_total",
    "derived_total",
)
_SOURCE_TOKEN_FIELDS = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cached_input_tokens": "cache_read",
    "cache_read_input_tokens": "cache_read",
    "cache_creation_input_tokens": "cache_write",
    "cache_creation_5m_input_tokens": "cache_write_5m",
    "cache_creation_1h_input_tokens": "cache_write_1h",
    "reasoning_output_tokens": "reasoning",
    "total_tokens": "reported_total",
}


@dataclass(slots=True)
class CodexState:
    owner_session_id: str | None = None
    owner_ordinal: int = 0


def _mapping(value: object) -> Mapping[str, object] | None:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else None


def _timestamp_ns(value: object) -> int:
    if not isinstance(value, str):
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return int(parsed.timestamp() * 1_000_000_000)


def _database_timestamp_ns(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    magnitude = abs(value)
    if magnitude >= 1_000_000_000_000_000:
        return value
    if magnitude >= 1_000_000_000_000:
        return value * 1_000_000
    return value * 1_000_000_000


def _usage_values(value: object) -> dict[str, int]:
    source = _mapping(value)
    if source is None:
        return {}
    usage: dict[str, int] = {}
    for source_name, target_name in _SOURCE_TOKEN_FIELDS.items():
        token_value = source.get(source_name)
        if isinstance(token_value, int) and not isinstance(token_value, bool) and token_value >= 0:
            usage[target_name] = token_value
    return usage


def _usage(usage: Mapping[str, int]) -> TokenUsage:
    return TokenUsage(**{name: usage[name] for name in _TOKEN_FIELDS if name in usage})


def _physical_key(line: SourceLine) -> str:
    return f"codex:disk:{line.artifact_id}:{line.generation}:{line.ordinal}"


def _issue(line: SourceLine, category: str, observed_type: str) -> Projection:
    return Projection(
        issues=(
            IssueDraft(
                source="codex",
                category=category,
                severity="error",
                identifier=(
                    f"artifact:{line.artifact_id}:generation:{line.generation}:ordinal:{line.ordinal}"
                ),
                field_path=str(line.ordinal),
                observed_type=observed_type,
            ),
        )
    )


def project_codex_line(line: SourceLine, state: CodexState) -> Projection:
    record_type = line.value.get("type")
    payload = _mapping(line.value.get("payload"))
    observed_at_ns = _timestamp_ns(line.value.get("timestamp"))

    if record_type == "session_meta":
        session_id = payload.get("id") if payload is not None else None
        if not isinstance(session_id, str) or not session_id:
            state.owner_session_id = None
            state.owner_ordinal = 0
            return _issue(line, "missing_session_id", type(session_id).__name__)
        state.owner_session_id = session_id
        state.owner_ordinal = 0
        cli_version = payload.get("cli_version")
        provider = payload.get("model_provider")
        model = payload.get("model")
        observation = ObservationDraft(
            source="codex",
            channel="disk",
            stable_key=_physical_key(line),
            event_type="session_meta",
            observed_at_ns=observed_at_ns,
            parser_version=_PARSER_VERSION,
            projection={},
            source_session_id=session_id,
            source_sequence=0,
            artifact_id=line.artifact_id,
            ordinal=line.ordinal,
        )
        session = SessionDraft(
            source="codex",
            source_session_id=session_id,
            root_session_id=session_id,
            harness_version=cli_version if isinstance(cli_version, str) else None,
            provider=provider if isinstance(provider, str) else None,
            initial_model=model if isinstance(model, str) else None,
            current_model=model if isinstance(model, str) else None,
            started_at_ns=observed_at_ns or None,
            updated_at_ns=observed_at_ns or None,
        )
        run = RunDraft(
            source="codex",
            source_session_id=session_id,
            source_run_id=session_id,
            role="session",
            model=model if isinstance(model, str) else None,
            started_at_ns=observed_at_ns or None,
        )
        return Projection(observations=(observation,), sessions=(session,), runs=(run,))

    if record_type != "event_msg" or payload is None or payload.get("type") != "token_count":
        return Projection()

    owner = state.owner_session_id
    if owner is None:
        return _issue(line, "missing_owner", "token_count")

    owner_ordinal = state.owner_ordinal
    info = _mapping(payload.get("info"))
    total = _usage_values(info.get("total_token_usage") if info is not None else None)
    last = _usage_values(info.get("last_token_usage") if info is not None else None)
    if not total or not last:
        return _issue(line, "invalid_usage", type(payload.get("info")).__name__)
    state.owner_ordinal += 1

    observation_key = _physical_key(line)
    event_key = f"codex:{owner}:{owner_ordinal}"
    observation = ObservationDraft(
        source="codex",
        channel="disk",
        stable_key=observation_key,
        event_type="token_count",
        observed_at_ns=observed_at_ns,
        parser_version=_PARSER_VERSION,
        projection={"usage": {"last": last, "total": total}},
        source_session_id=owner,
        source_sequence=owner_ordinal,
        artifact_id=line.artifact_id,
        ordinal=line.ordinal,
    )
    event = UsageEventDraft(
        source="codex",
        event_key=event_key,
        granularity="counter_delta",
        status="provisional",
        tokens=_usage(last),
        completed_at_ns=observed_at_ns or None,
    )
    link = LinkDraft(
        source="codex",
        channel="disk",
        observation_key=observation_key,
        event_key=event_key,
        method="owner_ordinal",
        role="primary",
        confidence="exact",
    )
    return Projection(observations=(observation,), events=(event,), links=(link,))
