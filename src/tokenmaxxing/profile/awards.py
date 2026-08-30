from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from tokenmaxxing.models import ProfileUsageRow, Source
from tokenmaxxing.reporting import event_total


TOKENMAXXER_TOKENS = 10_000_000_000
BILLION_DAY_TOKENS = 1_000_000_000
FLEET_COMMANDER_AGENTS = 250
HOT_STREAK_DAYS = 14
MODEL_COLLECTOR_MODELS = 10
SUPPORTED_HARNESSES: frozenset[Source] = frozenset(
    {"claude", "codex", "opencode", "pi"}
)


@dataclass(frozen=True, slots=True)
class Award:
    key: str
    name: str
    description: str
    metric_value: int
    earned_on: date | None = None


def _day(row: ProfileUsageRow, timezone: tzinfo) -> date | None:
    timestamp_ns = row.usage.occurred_at_ns
    if timestamp_ns is None:
        return None
    return datetime.fromtimestamp(timestamp_ns // 1_000_000_000, timezone).date()


def _first_streak_award_day(active_days: set[date]) -> date | None:
    current = 0
    previous: date | None = None
    for day in sorted(active_days):
        current = current + 1 if previous is not None and day == previous + timedelta(days=1) else 1
        if current >= HOT_STREAK_DAYS:
            return day
        previous = day
    return None


def derive_awards(
    rows: Sequence[ProfileUsageRow], *, timezone: tzinfo
) -> tuple[Award, ...]:
    all_time_tokens = 0
    daily_tokens: dict[date, int] = {}
    daily_agents: dict[date, set[str]] = {}
    models: set[str] = set()
    harnesses: set[Source] = set()

    for row in rows:
        tokens = event_total(row.usage)
        all_time_tokens += tokens
        if tokens <= 0:
            continue
        models.add(row.usage.resolved_model)
        harnesses.add(row.usage.source)
        day = _day(row, timezone)
        if day is None:
            continue
        daily_tokens[day] = daily_tokens.get(day, 0) + tokens
        if row.agent_key is not None:
            daily_agents.setdefault(day, set()).add(row.agent_key)

    billion_day = next(
        (day for day in sorted(daily_tokens) if daily_tokens[day] >= BILLION_DAY_TOKENS),
        None,
    )
    fleet_day = next(
        (
            day
            for day in sorted(daily_agents)
            if len(daily_agents[day]) >= FLEET_COMMANDER_AGENTS
        ),
        None,
    )
    streak_day = _first_streak_award_day(set(daily_agents))

    awards: list[Award] = []
    if all_time_tokens >= TOKENMAXXER_TOKENS:
        awards.append(
            Award(
                key="tokenmaxxer",
                name="Tokenmaxxer",
                description="Tracked at least ten billion tokens.",
                metric_value=all_time_tokens,
            )
        )
    if billion_day is not None:
        awards.append(
            Award(
                key="billion-day",
                name="Billion Day",
                description="Tracked at least one billion tokens in a single local day.",
                metric_value=daily_tokens[billion_day],
                earned_on=billion_day,
            )
        )
    if fleet_day is not None:
        awards.append(
            Award(
                key="fleet-commander",
                name="Fleet Commander",
                description="Directed at least 250 distinct agents in a single local day.",
                metric_value=len(daily_agents[fleet_day]),
                earned_on=fleet_day,
            )
        )
    if streak_day is not None:
        awards.append(
            Award(
                key="hot-streak",
                name="Hot Streak",
                description="Stayed active for at least fourteen consecutive local days.",
                metric_value=HOT_STREAK_DAYS,
                earned_on=streak_day,
            )
        )
    if len(models) >= MODEL_COLLECTOR_MODELS:
        awards.append(
            Award(
                key="model-collector",
                name="Model Collector",
                description="Used at least ten distinct models.",
                metric_value=len(models),
            )
        )
    if SUPPORTED_HARNESSES <= harnesses:
        awards.append(
            Award(
                key="all-systems-go",
                name="All Systems Go",
                description="Used every supported agent harness.",
                metric_value=len(SUPPORTED_HARNESSES),
            )
        )
    return tuple(awards)
