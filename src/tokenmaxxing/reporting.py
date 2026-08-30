from collections.abc import Callable, Iterable
from datetime import datetime, tzinfo
from typing import Literal, cast

from tokenmaxxing.models import JsonValue, UsageStat
from tokenmaxxing.repository import Repository


_GroupBy = Literal["source", "model", "day"]


def _integer(row: dict[str, object], name: str) -> int:
    value = row[name]
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _event_total(row: dict[str, object]) -> int:
    reported_total = row["reported_total_tokens"]
    if reported_total is not None:
        return cast(int, reported_total)
    derived_total = row["derived_total_tokens"]
    if derived_total is not None:
        return cast(int, derived_total)
    return sum(
        _integer(row, name)
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
    )


def _day(row: dict[str, object], timezone: tzinfo) -> str:
    timestamp_ns = row["occurred_at_ns"]
    if timestamp_ns is None:
        return "(unknown)"
    seconds = cast(int, timestamp_ns) // 1_000_000_000
    return datetime.fromtimestamp(seconds, timezone).date().isoformat()


def _aggregate(
    rows: Iterable[dict[str, object]], group_for: Callable[[dict[str, object]], str]
) -> tuple[UsageStat, ...]:
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
        aggregate["input_tokens"] += _integer(row, "input_tokens")
        aggregate["output_tokens"] += _integer(row, "output_tokens")
        aggregate["cache_read_tokens"] += _integer(row, "cache_read_tokens")
        aggregate["cache_write_tokens"] += _integer(row, "cache_write_tokens")
        aggregate["reasoning_tokens"] += _integer(row, "reasoning_tokens")
        aggregate["total_tokens"] += _event_total(row)
        cost_nanos = row["total_cost_nanos"]
        if cost_nanos is not None:
            aggregate["cost_nanos"] += cast(int, cost_nanos)
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
    repository: Repository, group_by: _GroupBy, timezone: tzinfo
) -> tuple[UsageStat, ...]:
    return _usage_stats_rows(repository._reporting_rows(), group_by, timezone)


def _usage_stats_rows(
    rows: Iterable[dict[str, object]], group_by: _GroupBy, timezone: tzinfo
) -> tuple[UsageStat, ...]:
    if group_by == "source":
        return _aggregate(rows, lambda row: cast(str, row["source"]))
    if group_by == "model":
        return _aggregate(rows, lambda row: cast(str, row["resolved_model"]))
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
    rows = repository._reporting_rows()
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
            _export_stat(stat) for stat in _usage_stats_rows(rows, "source", timezone)
        ],
        "by_model": [
            _export_stat(stat) for stat in _usage_stats_rows(rows, "model", timezone)
        ],
        "by_day": [
            _export_stat(stat) for stat in _usage_stats_rows(rows, "day", timezone)
        ],
    }
