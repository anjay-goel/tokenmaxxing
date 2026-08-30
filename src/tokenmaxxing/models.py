from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Literal, Mapping, cast

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | dict[str, JsonValue] | list[JsonValue]
type Source = Literal["codex", "claude", "pi", "opencode"]
type Channel = Literal["disk", "otel"]
type Granularity = Literal[
    "model_call", "turn_aggregate", "run_aggregate", "counter_delta"
]
type CountingStatus = Literal["canonical", "provisional", "excluded", "conflicted"]

_NANODOLLARS_PER_DOLLAR = Decimal("1000000000")


def _freeze_json(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return cast(
            JsonValue,
            MappingProxyType({key: _freeze_json(nested_value) for key, nested_value in value.items()}),
        )
    return value


def _validate_non_negative_ints(values: Mapping[str, int | None]) -> None:
    for name, value in values.items():
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise ValueError(f"{name} must be a non-negative integer or None")


def decimal_to_nanodollars(value: Decimal | str | int) -> int:
    if isinstance(value, bool):
        raise ValueError("cost must be a decimal value")
    try:
        nanodollars = Decimal(value) * _NANODOLLARS_PER_DOLLAR
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("cost must be a finite decimal") from error
    if not nanodollars.is_finite() or nanodollars != nanodollars.to_integral_value():
        raise ValueError("cost cannot be represented in whole nanodollars")
    result = int(nanodollars)
    if result < 0:
        raise ValueError("cost must not be negative")
    return result


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    cache_write_5m: int | None = None
    cache_write_1h: int | None = None
    reasoning: int | None = None
    reported_total: int | None = None
    derived_total: int | None = None

    def __post_init__(self) -> None:
        _validate_non_negative_ints(
            {
                "input": self.input,
                "output": self.output,
                "cache_read": self.cache_read,
                "cache_write": self.cache_write,
                "cache_write_5m": self.cache_write_5m,
                "cache_write_1h": self.cache_write_1h,
                "reasoning": self.reasoning,
                "reported_total": self.reported_total,
                "derived_total": self.derived_total,
            }
        )
        if self.reasoning is not None and self.output is not None and self.reasoning > self.output:
            raise ValueError("reasoning cannot exceed output")


@dataclass(frozen=True, slots=True)
class CostUsage:
    input_nanos: int | None = None
    output_nanos: int | None = None
    cache_read_nanos: int | None = None
    cache_write_nanos: int | None = None
    total_nanos: int | None = None
    original_decimal: str | None = None
    source: str | None = None
    estimated: bool | None = None

    def __post_init__(self) -> None:
        _validate_non_negative_ints(
            {
                "input_nanos": self.input_nanos,
                "output_nanos": self.output_nanos,
                "cache_read_nanos": self.cache_read_nanos,
                "cache_write_nanos": self.cache_write_nanos,
                "total_nanos": self.total_nanos,
            }
        )
        if self.estimated is not None and not isinstance(self.estimated, bool):
            raise ValueError("estimated must be a boolean or None")

    @classmethod
    def from_decimal(
        cls, value: Decimal | str | int, *, source: str | None = None, estimated: bool | None = None
    ) -> "CostUsage":
        return cls(
            total_nanos=decimal_to_nanodollars(value),
            original_decimal=str(value),
            source=source,
            estimated=estimated,
        )


@dataclass(frozen=True, slots=True)
class ObservationDraft:
    source: Source
    channel: Channel
    stable_key: str
    event_type: str
    observed_at_ns: int
    parser_version: str
    projection: Mapping[str, JsonValue]
    source_session_id: str | None = None
    source_run_id: str | None = None
    source_turn_id: str | None = None
    response_id: str | None = None
    request_id: str | None = None
    client_id: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    source_sequence: int | None = None
    artifact_id: int | None = None
    ordinal: int | None = None

    def __post_init__(self) -> None:
        from tokenmaxxing.privacy import validate_projection

        object.__setattr__(self, "projection", _freeze_json(validate_projection(self.projection)))


@dataclass(frozen=True, slots=True)
class UsageEventDraft:
    source: Source
    event_key: str
    granularity: Granularity
    status: CountingStatus
    tokens: TokenUsage
    cost: CostUsage = field(default_factory=CostUsage)
    session_id: int | None = None
    run_id: int | None = None
    turn_id: int | None = None
    provider: str | None = None
    api: str | None = None
    model: str | None = None
    response_model: str | None = None
    service_tier: str | None = None
    speed: str | None = None
    inference_region: str | None = None
    effort: str | None = None
    stop_reason: str | None = None
    error_category: str | None = None
    started_at_ns: int | None = None
    completed_at_ns: int | None = None
    duration_ns: int | None = None
    ttft_ns: int | None = None
    retries: int | None = None
    success: bool | None = None
    status_code: int | None = None
    web_search_count: int | None = None
    web_fetch_count: int | None = None
    tool_use_count: int | None = None
    replace_usage: bool = False
    replace_metadata_fields: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class SessionDraft:
    source: Source
    source_session_id: str
    root_session_id: str | None = None
    parent_session_id: str | None = None
    harness_version: str | None = None
    schema_version: str | None = None
    provider: str | None = None
    initial_model: str | None = None
    current_model: str | None = None
    reasoning_effort: str | None = None
    service_tier: str | None = None
    started_at_ns: int | None = None
    updated_at_ns: int | None = None
    completed_at_ns: int | None = None
    workspace_hash: str | None = None


@dataclass(frozen=True, slots=True)
class RunDraft:
    source: Source
    source_session_id: str
    source_run_id: str
    parent_run_id: str | None = None
    batch_id: str | None = None
    role: str | None = None
    status: str | None = None
    model: str | None = None
    provider: str | None = None
    effort: str | None = None
    isolation: str | None = None
    started_at_ns: int | None = None
    completed_at_ns: int | None = None
    duration_ns: int | None = None


@dataclass(frozen=True, slots=True)
class TurnDraft:
    source: Source
    source_session_id: str
    source_turn_id: str
    source_run_id: str | None = None
    started_at_ns: int | None = None
    completed_at_ns: int | None = None
    duration_ns: int | None = None
    ttft_ns: int | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class LinkDraft:
    source: Source
    channel: Channel
    observation_key: str
    event_key: str
    method: str
    role: Literal["primary", "supporting", "excluded"]
    confidence: Literal["exact", "deterministic"]


@dataclass(frozen=True, slots=True)
class SampleDraft:
    source: Source
    channel: Channel
    stable_key: str
    sample_type: str
    observed_at_ns: int
    name: str
    unit: str | None = None
    value_integer: int | None = None
    value_real: float | None = None
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IssueDraft:
    source: Source
    category: str
    severity: Literal["warning", "error"]
    identifier: str
    field_path: str | None = None
    observed_type: str | None = None


@dataclass(frozen=True, slots=True)
class Projection:
    observations: tuple[ObservationDraft, ...] = ()
    events: tuple[UsageEventDraft, ...] = ()
    links: tuple[LinkDraft, ...] = ()
    sessions: tuple[SessionDraft, ...] = ()
    runs: tuple[RunDraft, ...] = ()
    turns: tuple[TurnDraft, ...] = ()
    samples: tuple[SampleDraft, ...] = ()
    issues: tuple[IssueDraft, ...] = ()


@dataclass(frozen=True, slots=True)
class SyncStats:
    artifacts_seen: int = 0
    lines_read: int = 0
    observations_inserted: int = 0
    events_inserted: int = 0
    events_updated: int = 0
    issues_recorded: int = 0


@dataclass(frozen=True, slots=True)
class WriteStats:
    observations_inserted: int = 0
    events_inserted: int = 0
    events_updated: int = 0
    links_inserted: int = 0
    samples_inserted: int = 0
    issues_recorded: int = 0


@dataclass(frozen=True, slots=True)
class UsageTotal:
    group: str | None
    tokens: TokenUsage
    cost_nanos: int | None


@dataclass(frozen=True, slots=True)
class UsageStat:
    group: str
    event_count: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    total_tokens: int
    cost_nanos: int | None
    cost_covered_events: int
