import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from importlib.resources import files
from pathlib import Path
from typing import cast

from tokenmaxxing.models import ReportingRow
from tokenmaxxing.reporting import ReportWindow, event_total
from tokenmaxxing.repository import Repository


@dataclass(frozen=True, slots=True)
class TokenRates:
    input: Decimal | None = None
    cached_input: Decimal | None = None
    cache_write: Decimal | None = None
    cache_write_5m: Decimal | None = None
    cache_write_1h: Decimal | None = None
    output: Decimal | None = None


@dataclass(frozen=True, slots=True)
class LongContextRates:
    above_input_tokens: int
    inclusive: bool
    rates: TokenRates


@dataclass(frozen=True, slots=True)
class TimeMultiplier:
    weekdays: frozenset[int]
    start_hour_utc: int
    end_hour_utc: int
    multiplier: Decimal

    def matches(self, value: datetime) -> bool:
        return (
            value.weekday() in self.weekdays
            and self.start_hour_utc <= value.hour < self.end_hour_utc
        )


@dataclass(frozen=True, slots=True)
class PricePeriod:
    effective_from: datetime | None
    effective_until: datetime | None
    rates: TokenRates
    long_context: LongContextRates | None
    tier_multipliers: tuple[tuple[str, Decimal], ...]
    region_multipliers: tuple[tuple[str, Decimal], ...]
    time_multipliers: tuple[TimeMultiplier, ...]

    def includes(self, value: datetime) -> bool:
        return (
            (self.effective_from is None or self.effective_from <= value)
            and (self.effective_until is None or value < self.effective_until)
        )


@dataclass(frozen=True, slots=True)
class ModelPrice:
    provider: str
    model: str
    aliases: frozenset[str]
    pricing_url: str
    retrieved_at: str
    prices: tuple[PricePeriod, ...]


@dataclass(frozen=True, slots=True)
class RateCard:
    unit_tokens: int
    provider_aliases: tuple[tuple[str, frozenset[str]], ...]
    models: tuple[ModelPrice, ...]

    def provider(self, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _key(value)
        for provider, aliases in self.provider_aliases:
            if normalized == provider or normalized in aliases:
                return provider
        return normalized

    def resolve(
        self,
        provider: str | None,
        models: Iterable[str | None],
    ) -> ModelPrice | None:
        normalized_provider = self.provider(provider)
        for value in models:
            if value is None:
                continue
            candidate_provider, candidate = _model_key(value)
            scoped_provider = normalized_provider or self.provider(candidate_provider)
            if scoped_provider is not None:
                for model in self.models:
                    if model.provider == scoped_provider and candidate in model.aliases:
                        return model
            else:
                matches = [model for model in self.models if candidate in model.aliases]
                if len(matches) == 1:
                    return matches[0]
        return None


@dataclass(frozen=True, slots=True)
class ApiValueBreakdown:
    provider: str
    cost_nanos: int
    priced_tokens: int
    priced_events: int


@dataclass(frozen=True, slots=True)
class ApiValueEstimate:
    cost_nanos: int
    priced_tokens: int
    total_tokens: int
    priced_events: int
    total_events: int
    by_provider: tuple[ApiValueBreakdown, ...]


def _key(value: str) -> str:
    return value.strip().lower()


def _model_key(value: str) -> tuple[str | None, str]:
    normalized = _key(value)
    if "/" not in normalized:
        return None, normalized
    provider, model = normalized.split("/", 1)
    return provider, model


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return cast(Mapping[str, object], value)


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be an array")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError(f"{name} must be a non-negative decimal")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} must be a non-negative decimal")
    return result


def _timestamp(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    text = _string(value, name).replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO 8601 timestamp") from error
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return result.astimezone(UTC)


_RATE_FIELDS = (
    "input",
    "cached_input",
    "cache_write",
    "cache_write_5m",
    "cache_write_1h",
    "output",
)


def _rates(value: Mapping[str, object], name: str) -> TokenRates:
    parsed = {
        field: _decimal(value[field], f"{name}.{field}") if field in value else None
        for field in _RATE_FIELDS
    }
    return TokenRates(**parsed)


def _has_rate(rates: TokenRates) -> bool:
    return any(getattr(rates, field) is not None for field in _RATE_FIELDS)


def _multipliers(value: object, name: str) -> tuple[tuple[str, Decimal], ...]:
    if value is None:
        return ()
    mapping = _mapping(value, name)
    return tuple(
        sorted(
            (_key(key), _decimal(multiplier, f"{name}.{key}"))
            for key, multiplier in mapping.items()
        )
    )


def _price_period(value: object, name: str) -> PricePeriod:
    mapping = _mapping(value, name)
    rates = _rates(mapping, name)
    if not _has_rate(rates):
        raise ValueError(f"{name} must contain at least one token rate")
    long_value = mapping.get("long_context")
    long_context = None
    if long_value is not None:
        long_mapping = _mapping(long_value, f"{name}.long_context")
        threshold = long_mapping.get("above_input_tokens")
        if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
            raise ValueError(f"{name}.long_context.above_input_tokens must be non-negative")
        inclusive = long_mapping.get("inclusive", False)
        if not isinstance(inclusive, bool):
            raise ValueError(f"{name}.long_context.inclusive must be a boolean")
        long_context = LongContextRates(
            above_input_tokens=threshold,
            inclusive=inclusive,
            rates=_rates(long_mapping, f"{name}.long_context"),
        )
    time_rules = []
    for index, raw_rule in enumerate(
        _list(mapping.get("time_multipliers", []), f"{name}.time_multipliers")
    ):
        rule_name = f"{name}.time_multipliers[{index}]"
        rule = _mapping(raw_rule, rule_name)
        weekdays = _list(rule.get("weekdays"), f"{rule_name}.weekdays")
        if any(isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6 for day in weekdays):
            raise ValueError(f"{rule_name}.weekdays must contain integers from 0 to 6")
        start = rule.get("start_hour_utc")
        end = rule.get("end_hour_utc")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or not 0 <= start < end <= 24
        ):
            raise ValueError(f"{rule_name} must contain a valid UTC hour range")
        time_rules.append(
            TimeMultiplier(
                weekdays=frozenset(cast(list[int], weekdays)),
                start_hour_utc=start,
                end_hour_utc=end,
                multiplier=_decimal(rule.get("multiplier"), f"{rule_name}.multiplier"),
            )
        )
    for index, first in enumerate(time_rules):
        for second in time_rules[index + 1 :]:
            if (
                first.weekdays & second.weekdays
                and first.start_hour_utc < second.end_hour_utc
                and second.start_hour_utc < first.end_hour_utc
            ):
                raise ValueError(f"{name}.time_multipliers must not overlap")
    return PricePeriod(
        effective_from=_timestamp(mapping.get("effective_from"), f"{name}.effective_from"),
        effective_until=_timestamp(mapping.get("effective_until"), f"{name}.effective_until"),
        rates=rates,
        long_context=long_context,
        tier_multipliers=_multipliers(mapping.get("tier_multipliers"), f"{name}.tier_multipliers"),
        region_multipliers=_multipliers(
            mapping.get("region_multipliers"), f"{name}.region_multipliers"
        ),
        time_multipliers=tuple(time_rules),
    )


def _load_rate_card_data(data: object) -> RateCard:
    root = _mapping(data, "rate card")
    if root.get("schema_version") != 1:
        raise ValueError("unsupported rate card schema_version")
    if root.get("currency") != "USD":
        raise ValueError("rate card currency must be USD")
    unit_tokens = root.get("unit_tokens")
    if isinstance(unit_tokens, bool) or not isinstance(unit_tokens, int) or unit_tokens <= 0:
        raise ValueError("rate card unit_tokens must be a positive integer")

    aliases_data = _mapping(root.get("provider_aliases", {}), "provider_aliases")
    provider_alias_list = []
    provider_owners: dict[str, str] = {}
    for provider, values in aliases_data.items():
        normalized_provider = _key(_string(provider, "provider_aliases key"))
        aliases = frozenset(
            _key(_string(alias, f"provider_aliases.{provider}"))
            for alias in _list(values, f"provider_aliases.{provider}")
        )
        for alias in aliases | {normalized_provider}:
            owner = provider_owners.get(alias)
            if owner is not None and owner != normalized_provider:
                raise ValueError(f"duplicate provider alias: {alias}")
            provider_owners[alias] = normalized_provider
        provider_alias_list.append((normalized_provider, aliases))
    provider_aliases = tuple(sorted(provider_alias_list))

    models = []
    seen_aliases: set[tuple[str, str]] = set()
    for index, raw_model in enumerate(_list(root.get("models"), "models")):
        name = f"models[{index}]"
        model_data = _mapping(raw_model, name)
        provider = _key(_string(model_data.get("provider"), f"{name}.provider"))
        model_name = _key(_string(model_data.get("model"), f"{name}.model"))
        aliases = frozenset(
            {model_name}
            | {
                _key(_string(alias, f"{name}.aliases"))
                for alias in _list(model_data.get("aliases", []), f"{name}.aliases")
            }
        )
        for alias in aliases:
            identity = (provider, alias)
            if identity in seen_aliases:
                raise ValueError(f"duplicate model alias: {provider}/{alias}")
            seen_aliases.add(identity)
        prices = tuple(
            _price_period(value, f"{name}.prices[{price_index}]")
            for price_index, value in enumerate(
                _list(model_data.get("prices"), f"{name}.prices")
            )
        )
        if not prices:
            raise ValueError(f"{name}.prices must not be empty")
        for price_index, price in enumerate(prices):
            if (
                price.effective_from is not None
                and price.effective_until is not None
                and price.effective_from >= price.effective_until
            ):
                raise ValueError(f"{name}.prices[{price_index}] has an empty price period")
        for first_index, first in enumerate(prices):
            for second in prices[first_index + 1 :]:
                if (
                    (first.effective_until is None or second.effective_from is None or second.effective_from < first.effective_until)
                    and (second.effective_until is None or first.effective_from is None or first.effective_from < second.effective_until)
                ):
                    raise ValueError(f"{name} has overlapping price periods")
        retrieved_at = _string(
            model_data.get("retrieved_at", "2026-08-30"), f"{name}.retrieved_at"
        )
        try:
            date.fromisoformat(retrieved_at)
        except ValueError as error:
            raise ValueError(f"{name}.retrieved_at must be an ISO 8601 date") from error
        models.append(
            ModelPrice(
                provider=provider,
                model=model_name,
                aliases=aliases,
                pricing_url=_string(model_data.get("pricing_url"), f"{name}.pricing_url"),
                retrieved_at=retrieved_at,
                prices=prices,
            )
        )
    return RateCard(
        unit_tokens=unit_tokens,
        provider_aliases=provider_aliases,
        models=tuple(models),
    )


def load_rate_card(path: Path | None = None) -> RateCard:
    if path is None:
        with files("tokenmaxxing").joinpath("data/rate-card.json").open(
            "r", encoding="utf-8"
        ) as rate_file:
            return _load_rate_card_data(json.load(rate_file))
    with path.open("r", encoding="utf-8") as rate_file:
        return _load_rate_card_data(json.load(rate_file))


def _integer(value: int | None) -> int:
    return value if value is not None else 0


def _components(row: ReportingRow) -> tuple[dict[str, int], int] | None:
    input_tokens = _integer(row.input_tokens)
    cache_read = _integer(row.cache_read_tokens)
    cache_write = _integer(row.cache_write_tokens)
    cache_write_5m = _integer(row.cache_write_5m_tokens)
    cache_write_1h = _integer(row.cache_write_1h_tokens)
    split_writes = cache_write_5m + cache_write_1h
    if split_writes > cache_write:
        return None
    if row.source == "codex":
        if cache_read > input_tokens:
            return None
        fresh_input = input_tokens - cache_read
        context_input = input_tokens + cache_write
    else:
        fresh_input = input_tokens
        context_input = input_tokens + cache_read + cache_write
    return (
        {
            "input": fresh_input,
            "cached_input": cache_read,
            "cache_write": cache_write - split_writes,
            "cache_write_5m": cache_write_5m,
            "cache_write_1h": cache_write_1h,
            "output": _integer(row.output_tokens),
        },
        context_input,
    )


def _period(model: ModelPrice, occurred_at_ns: int | None) -> tuple[PricePeriod, datetime] | None:
    if occurred_at_ns is None:
        return None
    seconds, nanoseconds = divmod(occurred_at_ns, 1_000_000_000)
    occurred_at = datetime.fromtimestamp(seconds, UTC) + timedelta(
        microseconds=nanoseconds // 1_000
    )
    matches = [period for period in model.prices if period.includes(occurred_at)]
    if len(matches) != 1:
        return None
    return matches[0], occurred_at


_DEFAULT_TIERS = frozenset({"auto", "default", "not_available", "standard"})
_DEFAULT_REGIONS = frozenset({"default", "global", "not_available"})


def _selected_multiplier(
    configured: tuple[tuple[str, Decimal], ...],
    values: Iterable[str | None],
    defaults: frozenset[str],
) -> Decimal | None:
    selected = {_key(value) for value in values if value is not None} - defaults
    if not selected:
        return Decimal(1)
    if len(selected) != 1:
        return None
    return dict(configured).get(selected.pop())


def _multiplier(
    period: PricePeriod, row: ReportingRow, occurred_at: datetime
) -> Decimal | None:
    result = Decimal(1)
    tier = _selected_multiplier(
        period.tier_multipliers,
        (row.speed, row.service_tier),
        _DEFAULT_TIERS,
    )
    region = _selected_multiplier(
        period.region_multipliers,
        (row.inference_region,),
        _DEFAULT_REGIONS,
    )
    if tier is None or region is None:
        return None
    result *= tier * region
    for rule in period.time_multipliers:
        if rule.matches(occurred_at):
            result *= rule.multiplier
            break
    return result


def _estimated_cost(row: ReportingRow, card: RateCard) -> tuple[str, int] | None:
    provider = row.provider
    if provider is None:
        provider = {"codex": "openai", "claude": "anthropic"}.get(row.source)
    model = card.resolve(provider, (row.resolved_model, row.requested_model))
    if model is None:
        return None
    selection = _period(model, row.occurred_at_ns)
    components = _components(row)
    if selection is None or components is None:
        return None
    period, occurred_at = selection
    tokens, context_input = components
    if sum(tokens.values()) != event_total(row):
        return None
    rates = period.rates
    if period.long_context is not None and row.granularity == "model_call":
        threshold = period.long_context.above_input_tokens
        if context_input > threshold or (
            period.long_context.inclusive and context_input == threshold
        ):
            rates = period.long_context.rates
    cost = Decimal(0)
    nanodollars_per_token_unit = Decimal(1_000_000_000) / card.unit_tokens
    for component, token_count in tokens.items():
        rate = getattr(rates, component)
        if token_count and rate is None:
            return None
        if rate is not None:
            cost += token_count * rate * nanodollars_per_token_unit
    multiplier = _multiplier(period, row, occurred_at)
    if multiplier is None:
        return None
    cost *= multiplier
    return model.provider, int(cost.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def estimate_api_value_rows(
    rows: Iterable[ReportingRow], rate_card: RateCard | None = None
) -> ApiValueEstimate:
    card = rate_card or load_rate_card()
    total_cost = 0
    total_tokens = 0
    priced_tokens = 0
    total_events = 0
    priced_events = 0
    breakdowns: dict[str, list[int]] = {}
    for row in rows:
        total_events += 1
        tokens = event_total(row)
        total_tokens += tokens
        if row.total_cost_nanos is not None:
            provider = card.provider(row.provider) or row.source
            estimated = (provider, row.total_cost_nanos)
        else:
            estimated = _estimated_cost(row, card)
        if estimated is None:
            continue
        provider, cost_nanos = estimated
        total_cost += cost_nanos
        priced_tokens += tokens
        priced_events += 1
        provider_breakdown = breakdowns.setdefault(provider, [0, 0, 0])
        provider_breakdown[0] += cost_nanos
        provider_breakdown[1] += tokens
        provider_breakdown[2] += 1
    return ApiValueEstimate(
        cost_nanos=total_cost,
        priced_tokens=priced_tokens,
        total_tokens=total_tokens,
        priced_events=priced_events,
        total_events=total_events,
        by_provider=tuple(
            ApiValueBreakdown(provider, values[0], values[1], values[2])
            for provider, values in sorted(breakdowns.items())
        ),
    )


def estimate_api_value(
    repository: Repository,
    window: ReportWindow | None = None,
    rate_card: RateCard | None = None,
) -> ApiValueEstimate:
    rows = repository.reporting_rows()
    if window is not None:
        rows = [row for row in rows if window.includes(row)]
    return estimate_api_value_rows(rows, rate_card)
