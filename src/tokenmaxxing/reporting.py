from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from typing import Literal

from tokenmaxxing.models import JsonValue, ReportingRow, UsageStat
from tokenmaxxing.repository import Repository


_GroupBy = Literal["source", "model", "day"]
_Period = Literal["7d", "28d", "all"]


@dataclass(frozen=True, slots=True)
class ReportWindow:
    period: str
    start_ns: int | None
    end_ns: int | None

    @classmethod
    def from_period(
        cls, period: _Period, timezone: tzinfo, now: datetime
    ) -> "ReportWindow":
        if period == "all":
            if now.tzinfo is None or now.utcoffset() is None:
                raise ValueError("now must have an explicit timezone")
            return cls(period=period, start_ns=None, end_ns=None)
        day_count = {"7d": 7, "28d": 28}[period]
        return cls.from_days(day_count, timezone, now)

    @classmethod
    def from_days(
        cls, day_count: int, timezone: tzinfo, now: datetime
    ) -> "ReportWindow":
        if isinstance(day_count, bool) or not isinstance(day_count, int) or day_count <= 0:
            raise ValueError("day_count must be a positive integer")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must have an explicit timezone")
        today = now.astimezone(timezone).date()
        start = datetime.combine(
            today - timedelta(days=day_count - 1), time.min, tzinfo=timezone
        )
        end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=timezone)
        return cls(
            period=f"{day_count}d",
            start_ns=int(start.timestamp()) * 1_000_000_000,
            end_ns=int(end.timestamp()) * 1_000_000_000,
        )

    def includes(self, row: ReportingRow) -> bool:
        if self.start_ns is None or self.end_ns is None:
            return True
        return (
            row.occurred_at_ns is not None
            and self.start_ns <= row.occurred_at_ns < self.end_ns
        )


def _integer(value: int | None) -> int:
    return value if value is not None else 0


def event_total(row: ReportingRow) -> int:
    reported_total = row.reported_total_tokens
    if reported_total is not None:
        return reported_total
    derived_total = row.derived_total_tokens
    if derived_total is not None:
        return derived_total
    total = (
        _integer(row.input_tokens)
        + _integer(row.output_tokens)
        + _integer(row.cache_write_tokens)
    )
    if row.source != "codex":
        total += _integer(row.cache_read_tokens)
    return total


def _day(row: ReportingRow, timezone: tzinfo) -> str:
    timestamp_ns = row.occurred_at_ns
    if timestamp_ns is None:
        return "(unknown)"
    seconds = timestamp_ns // 1_000_000_000
    return datetime.fromtimestamp(seconds, timezone).date().isoformat()


def _aggregate(rows: Iterable[ReportingRow], group_for: Callable[[ReportingRow], str]) -> tuple[UsageStat, ...]:
    aggregates: dict[str, dict[str, int]] = {}
    for row in rows:
        group = group_for(row)
        aggregate = aggregates.setdefault(
            group,
            {
                "event_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
                "cost_nanos": 0,
                "cost_covered_events": 0,
            },
        )
        aggregate["event_count"] += 1
        aggregate["input_tokens"] += _integer(row.input_tokens)
        aggregate["output_tokens"] += _integer(row.output_tokens)
        aggregate["cache_read_tokens"] += _integer(row.cache_read_tokens)
        aggregate["cache_write_tokens"] += _integer(row.cache_write_tokens)
        aggregate["reasoning_tokens"] += _integer(row.reasoning_tokens)
        aggregate["total_tokens"] += event_total(row)
        cost_nanos = row.total_cost_nanos
        if cost_nanos is not None:
            aggregate["cost_nanos"] += cost_nanos
            aggregate["cost_covered_events"] += 1
    return tuple(
        UsageStat(
            group=group,
            event_count=aggregate["event_count"],
            input_tokens=aggregate["input_tokens"],
            output_tokens=aggregate["output_tokens"],
            cache_read_tokens=aggregate["cache_read_tokens"],
            cache_write_tokens=aggregate["cache_write_tokens"],
            reasoning_tokens=aggregate["reasoning_tokens"],
            total_tokens=aggregate["total_tokens"],
            cost_nanos=(
                aggregate["cost_nanos"] if aggregate["cost_covered_events"] else None
            ),
            cost_covered_events=aggregate["cost_covered_events"],
        )
        for group, aggregate in sorted(aggregates.items())
    )


def usage_stats(
    repository: Repository,
    group_by: _GroupBy,
    timezone: tzinfo,
    *,
    window: ReportWindow | None = None,
) -> tuple[UsageStat, ...]:
    rows = repository.reporting_rows()
    if window is not None:
        rows = [row for row in rows if window.includes(row)]
    return usage_stats_rows(rows, group_by, timezone)


def usage_stats_rows(
    rows: Iterable[ReportingRow], group_by: _GroupBy, timezone: tzinfo
) -> tuple[UsageStat, ...]:
    if group_by == "source":
        return _aggregate(rows, lambda row: row.source)
    if group_by == "model":
        return _aggregate(rows, lambda row: row.resolved_model)
    if group_by == "day":
        return _aggregate(rows, lambda row: _day(row, timezone))
    raise ValueError(f"unsupported group_by: {group_by}")


def _export_stat(stat: UsageStat) -> dict[str, JsonValue]:
    return {
        "group": stat.group,
        "event_count": stat.event_count,
        "total_tokens": stat.total_tokens,
    }


def _timezone_name(timezone: tzinfo) -> str:
    key = getattr(timezone, "key", None)
    return key if isinstance(key, str) else timezone.tzname(None) or str(timezone)


def export_payload(
    repository: Repository, timezone: tzinfo, generated_at: datetime
) -> dict[str, JsonValue]:
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must have an explicit timezone")
    rows = repository.reporting_rows()
    overall = _aggregate(rows, lambda row: "all")
    overall_stat = (
        overall[0]
        if overall
        else UsageStat(
            group="all",
            event_count=0,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            reasoning_tokens=0,
            total_tokens=0,
            cost_nanos=None,
            cost_covered_events=0,
        )
    )
    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "timezone": _timezone_name(timezone),
        "overall": _export_stat(overall_stat),
        "by_source": [
            _export_stat(stat) for stat in usage_stats_rows(rows, "source", timezone)
        ],
        "by_model": [
            _export_stat(stat) for stat in usage_stats_rows(rows, "model", timezone)
        ],
        "by_day": [
            _export_stat(stat) for stat in usage_stats_rows(rows, "day", timezone)
        ],
    }
