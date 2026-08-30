from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from tokenmaxxing.models import ProfileUsageRow
from tokenmaxxing.presentation import usage_quip
from tokenmaxxing.pricing import ApiValueEstimate, estimate_api_value_rows
from tokenmaxxing.profile.awards import Award, derive_awards
from tokenmaxxing.profile.model_icons import canonical_creator
from tokenmaxxing.reporting import ReportWindow, event_total


@dataclass(frozen=True, slots=True)
class ModelTotal:
    model: str
    total_tokens: int
    provider: str | None = None


@dataclass(frozen=True, slots=True)
class HarnessTotal:
    harness: str
    total_tokens: int


@dataclass(frozen=True, slots=True)
class DailyTotal:
    day: date
    total_tokens: int
    models: tuple[ModelTotal, ...]


@dataclass(frozen=True, slots=True)
class AgentModelTotal:
    model: str
    agents: int


@dataclass(frozen=True, slots=True)
class DailyAgentTotal:
    day: date
    agents: int
    models: tuple[AgentModelTotal, ...]


@dataclass(frozen=True, slots=True)
class ProfileData:
    generated_at: datetime
    window_start: date
    window_end: date
    total_tokens: int
    all_time_tokens: int
    api_equivalent: ApiValueEstimate
    agent_count: int
    peak_usage: int
    longest_streak: int
    model_count: int
    models: tuple[ModelTotal, ...]
    harnesses: tuple[HarnessTotal, ...]
    agent_models: tuple[AgentModelTotal, ...]
    recent_token_days: tuple[DailyTotal, ...]
    recent_days: tuple[DailyAgentTotal, ...]
    activity_days: tuple[DailyTotal, ...]
    first_tracked_day: date | None
    quip: str
    awards: tuple[Award, ...]


def _day(row: ProfileUsageRow, timezone: tzinfo) -> date | None:
    timestamp_ns = row.usage.occurred_at_ns
    if timestamp_ns is None:
        return None
    return datetime.fromtimestamp(timestamp_ns // 1_000_000_000, timezone).date()


def _model_totals(
    totals: dict[str, int], provider_totals: dict[str, dict[str, int]] | None = None
) -> tuple[ModelTotal, ...]:
    providers = provider_totals or {}
    return tuple(
        ModelTotal(
            model=model,
            total_tokens=total_tokens,
            provider=_primary_provider(providers.get(model, {})),
        )
        for model, total_tokens in sorted(
            (
                (model, total_tokens)
                for model, total_tokens in totals.items()
                if total_tokens > 0
            ),
            key=lambda item: (-item[1], item[0]),
        )
    )


def _primary_provider(totals: dict[str, int]) -> str | None:
    if not totals:
        return None
    return min(totals, key=lambda provider: (-totals[provider], provider))


def _row_provider(row: ProfileUsageRow) -> str | None:
    return canonical_creator(row.usage.provider)


def _add_provider_tokens(
    totals: dict[str, dict[str, int]], row: ProfileUsageRow, tokens: int
) -> None:
    provider = _row_provider(row)
    if tokens <= 0 or provider is None:
        return
    model = row.usage.resolved_model
    providers = totals.setdefault(model, {})
    providers[provider] = providers.get(provider, 0) + tokens


def _daily_totals(
    rows: Sequence[ProfileUsageRow],
    *,
    start: date,
    day_count: int,
    timezone: tzinfo,
) -> tuple[DailyTotal, ...]:
    totals: dict[date, int] = {}
    models: dict[date, dict[str, int]] = {}
    providers: dict[date, dict[str, dict[str, int]]] = {}
    end = start + timedelta(days=day_count)
    for row in rows:
        day = _day(row, timezone)
        if day is None or not start <= day < end:
            continue
        tokens = event_total(row.usage)
        totals[day] = totals.get(day, 0) + tokens
        model_totals = models.setdefault(day, {})
        model = row.usage.resolved_model
        model_totals[model] = model_totals.get(model, 0) + tokens
        _add_provider_tokens(providers.setdefault(day, {}), row, tokens)
    return tuple(
        DailyTotal(
            day=day,
            total_tokens=totals.get(day, 0),
            models=_model_totals(models.get(day, {}), providers.get(day)),
        )
        for day in (start + timedelta(days=offset) for offset in range(day_count))
    )


def _longest_streak(
    active_days: set[date], recent_days: Sequence[DailyAgentTotal]
) -> int:
    longest = 0
    current = 0
    for daily in recent_days:
        if daily.day in active_days:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _agent_model_totals(totals: dict[str, int]) -> tuple[AgentModelTotal, ...]:
    return tuple(
        AgentModelTotal(model=model, agents=agents)
        for model, agents in sorted(
            totals.items(), key=lambda item: (-item[1], item[0])
        )
    )


def _daily_agent_totals(
    *,
    start: date,
    day_count: int,
    agent_days: set[tuple[date, str]],
    primary_models: dict[str, str],
) -> tuple[DailyAgentTotal, ...]:
    counts: dict[date, dict[str, int]] = {}
    for day, agent_key in agent_days:
        model = primary_models[agent_key]
        daily = counts.setdefault(day, {})
        daily[model] = daily.get(model, 0) + 1
    return tuple(
        DailyAgentTotal(
            day=day,
            agents=sum(counts.get(day, {}).values()),
            models=_agent_model_totals(counts.get(day, {})),
        )
        for day in (start + timedelta(days=offset) for offset in range(day_count))
    )


def build_profile_data(
    rows: Sequence[ProfileUsageRow],
    *,
    timezone: tzinfo,
    now: datetime,
    window_days: int,
) -> ProfileData:
    snapshot = tuple(rows)
    window = ReportWindow.from_days(window_days, timezone, now)
    generated_at = now.astimezone(timezone)
    window_end = generated_at.date()
    window_start = window_end - timedelta(days=window_days - 1)
    recent_rows = tuple(row for row in snapshot if window.includes(row.usage))

    model_totals: dict[str, int] = {}
    model_provider_totals: dict[str, dict[str, int]] = {}
    harness_totals: dict[str, int] = {}
    agent_model_tokens: dict[str, dict[str, int]] = {}
    agent_days: set[tuple[date, str]] = set()
    for row in recent_rows:
        tokens = event_total(row.usage)
        model = row.usage.resolved_model
        model_totals[model] = model_totals.get(model, 0) + tokens
        _add_provider_tokens(model_provider_totals, row, tokens)
        source = row.usage.source
        harness_totals[source] = harness_totals.get(source, 0) + tokens
        agent_key = row.agent_key
        if agent_key is None or tokens <= 0:
            continue
        per_model = agent_model_tokens.setdefault(agent_key, {})
        per_model[model] = per_model.get(model, 0) + tokens
        day = _day(row, timezone)
        if day is not None:
            agent_days.add((day, agent_key))

    primary_models = {
        agent_key: min(totals, key=lambda model: (-totals[model], model))
        for agent_key, totals in agent_model_tokens.items()
    }
    agent_model_counts: dict[str, int] = {}
    for model in primary_models.values():
        agent_model_counts[model] = agent_model_counts.get(model, 0) + 1
    agent_models = _agent_model_totals(agent_model_counts)

    recent_token_days = _daily_totals(
        recent_rows,
        start=window_start,
        day_count=window_days,
        timezone=timezone,
    )
    recent_days = _daily_agent_totals(
        start=window_start,
        day_count=window_days,
        agent_days=agent_days,
        primary_models=primary_models,
    )
    activity_start = window_end - timedelta(days=363)
    activity_days = _daily_totals(
        snapshot,
        start=activity_start,
        day_count=364,
        timezone=timezone,
    )
    tracked_days = tuple(
        day for row in snapshot if (day := _day(row, timezone)) is not None
    )
    total_tokens = sum(event_total(row.usage) for row in recent_rows)

    return ProfileData(
        generated_at=generated_at,
        window_start=window_start,
        window_end=window_end,
        total_tokens=total_tokens,
        all_time_tokens=sum(event_total(row.usage) for row in snapshot),
        api_equivalent=estimate_api_value_rows(row.usage for row in recent_rows),
        agent_count=len(primary_models),
        peak_usage=max(
            (daily.total_tokens for daily in recent_token_days), default=0
        ),
        longest_streak=_longest_streak(
            {day for day, _ in agent_days}, recent_days
        ),
        model_count=sum(total > 0 for total in model_totals.values()),
        models=_model_totals(model_totals, model_provider_totals),
        harnesses=tuple(
            HarnessTotal(harness=harness, total_tokens=tokens)
            for harness, tokens in sorted(
                (
                    (harness, tokens)
                    for harness, tokens in harness_totals.items()
                    if tokens > 0
                ),
                key=lambda item: (-item[1], item[0]),
            )
        ),
        agent_models=agent_models,
        recent_token_days=recent_token_days,
        recent_days=recent_days,
        activity_days=activity_days,
        first_tracked_day=min(tracked_days, default=None),
        quip=usage_quip(total_tokens),
        awards=derive_awards(snapshot, timezone=timezone),
    )
