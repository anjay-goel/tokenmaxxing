from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from tokenmaxxing.models import ProfileUsageRow, ReportingRow, Source
from tokenmaxxing.profile.awards import Award, derive_awards


KOLKATA = ZoneInfo("Asia/Kolkata")


def _row(
    total_tokens: int,
    *,
    occurred_at: datetime | None,
    agent_key: str | None = "codex:session:1",
    model: str = "model-a",
    source: Source = "codex",
) -> ProfileUsageRow:
    return ProfileUsageRow(
        usage=ReportingRow(
            source=source,
            granularity="counter_delta" if source == "codex" else "model_call",
            provider=None,
            resolved_model=model,
            requested_model=model,
            occurred_at_ns=(
                int(occurred_at.timestamp() * 1_000_000_000)
                if occurred_at is not None
                else None
            ),
            input_tokens=total_tokens,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cache_write_5m_tokens=None,
            cache_write_1h_tokens=None,
            reasoning_tokens=0,
            reported_total_tokens=total_tokens,
            derived_total_tokens=total_tokens,
            total_cost_nanos=None,
            service_tier=None,
            speed=None,
            inference_region=None,
        ),
        agent_key=agent_key,
    )


def _keys(rows: tuple[ProfileUsageRow, ...]) -> tuple[str, ...]:
    return tuple(award.key for award in derive_awards(rows, timezone=KOLKATA))


def test_token_awards_require_the_exact_generic_thresholds() -> None:
    day = datetime(2026, 8, 30, 12, tzinfo=KOLKATA)

    assert "tokenmaxxer" not in _keys((_row(9_999_999_999, occurred_at=day),))
    assert "tokenmaxxer" in _keys((_row(10_000_000_000, occurred_at=day),))
    assert "billion-day" not in _keys((_row(999_999_999, occurred_at=day),))
    assert "billion-day" in _keys((_row(1_000_000_000, occurred_at=day),))


def test_billion_day_combines_positive_rows_on_the_same_local_day() -> None:
    first = datetime(2026, 8, 30, 0, 15, tzinfo=KOLKATA)
    rows = (
        _row(600_000_000, occurred_at=first, agent_key=None),
        _row(400_000_000, occurred_at=first + timedelta(hours=20), agent_key=None),
    )

    award = next(
        award
        for award in derive_awards(rows, timezone=KOLKATA)
        if award.key == "billion-day"
    )

    assert award.metric_value == 1_000_000_000
    assert award.earned_on is not None
    assert award.earned_on.isoformat() == "2026-08-30"


def test_utc_timestamps_are_partitioned_at_local_midnight() -> None:
    before_midnight = datetime(2026, 8, 29, 18, 29, tzinfo=UTC)
    local_midnight = datetime(2026, 8, 29, 18, 30, tzinfo=UTC)
    rows = (
        _row(500_000_000, occurred_at=before_midnight, agent_key=None),
        _row(500_000_000, occurred_at=local_midnight, agent_key=None),
    )

    assert "billion-day" not in _keys(rows)

    award = next(
        award
        for award in derive_awards(
            rows
            + (
                _row(
                    500_000_000,
                    occurred_at=local_midnight + timedelta(hours=1),
                    agent_key=None,
                ),
            ),
            timezone=KOLKATA,
        )
        if award.key == "billion-day"
    )
    assert award.metric_value == 1_000_000_000
    assert award.earned_on is not None
    assert award.earned_on.isoformat() == "2026-08-30"


def test_billion_day_freezes_the_first_qualifying_day_value() -> None:
    first = datetime(2026, 8, 20, 9, tzinfo=KOLKATA)
    rows = (
        _row(1_000_000_000, occurred_at=first, agent_key=None),
        _row(2_000_000_000, occurred_at=first + timedelta(days=1), agent_key=None),
    )

    award = next(
        award
        for award in derive_awards(rows, timezone=KOLKATA)
        if award.key == "billion-day"
    )

    assert award.metric_value == 1_000_000_000
    assert award.earned_on is not None
    assert award.earned_on.isoformat() == "2026-08-20"


def test_fleet_commander_counts_each_agent_once_per_local_day() -> None:
    day = datetime(2026, 8, 30, 9, tzinfo=KOLKATA)
    below = tuple(
        _row(1, occurred_at=day, agent_key=f"codex:session:{index}")
        for index in range(249)
    )
    repeated = below + (
        _row(1, occurred_at=day + timedelta(hours=1), agent_key="codex:session:0"),
    )
    threshold = repeated + (
        _row(1, occurred_at=day, agent_key="codex:session:249"),
    )

    assert "fleet-commander" not in _keys(repeated)
    awards = derive_awards(threshold, timezone=KOLKATA)
    fleet = next(award for award in awards if award.key == "fleet-commander")
    assert fleet.metric_value == 250
    assert fleet.earned_on is not None
    assert fleet.earned_on.isoformat() == "2026-08-30"


def test_fleet_commander_freezes_the_first_qualifying_day_value() -> None:
    first = datetime(2026, 8, 20, 9, tzinfo=KOLKATA)
    rows = tuple(
        _row(1, occurred_at=first, agent_key=f"first:{index}")
        for index in range(250)
    ) + tuple(
        _row(1, occurred_at=first + timedelta(days=1), agent_key=f"later:{index}")
        for index in range(300)
    )

    award = next(
        award
        for award in derive_awards(rows, timezone=KOLKATA)
        if award.key == "fleet-commander"
    )

    assert award.metric_value == 250
    assert award.earned_on is not None
    assert award.earned_on.isoformat() == "2026-08-20"


def test_hot_streak_requires_fourteen_positive_agent_days() -> None:
    start = datetime(2026, 8, 1, 9, tzinfo=KOLKATA)
    thirteen = tuple(
        _row(1, occurred_at=start + timedelta(days=offset))
        for offset in range(13)
    )
    fourteen = thirteen + (
        _row(1, occurred_at=start + timedelta(days=13)),
    )

    assert "hot-streak" not in _keys(thirteen)
    award = next(
        award
        for award in derive_awards(fourteen, timezone=KOLKATA)
        if award.key == "hot-streak"
    )
    assert award.metric_value == 14
    assert award.earned_on is not None
    assert award.earned_on.isoformat() == "2026-08-14"


def test_hot_streak_freezes_at_the_first_earned_threshold() -> None:
    start = datetime(2026, 8, 1, 9, tzinfo=KOLKATA)
    rows = tuple(
        _row(1, occurred_at=start + timedelta(days=offset))
        for offset in range(20)
    )

    award = next(
        award
        for award in derive_awards(rows, timezone=KOLKATA)
        if award.key == "hot-streak"
    )

    assert award.metric_value == 14
    assert award.earned_on is not None
    assert award.earned_on.isoformat() == "2026-08-14"


def test_zero_usage_and_missing_timestamps_cannot_earn_day_awards() -> None:
    day = datetime(2026, 8, 30, 9, tzinfo=KOLKATA)
    rows = tuple(
        _row(0, occurred_at=day + timedelta(days=offset), agent_key=f"agent:{offset}")
        for offset in range(20)
    ) + tuple(
        _row(1_000_000_000, occurred_at=None, agent_key=f"missing:{index}")
        for index in range(250)
    )

    keys = _keys(rows)

    assert "billion-day" not in keys
    assert "fleet-commander" not in keys
    assert "hot-streak" not in keys


def test_model_collector_and_all_systems_go_use_positive_counted_usage() -> None:
    day = datetime(2026, 8, 30, 9, tzinfo=KOLKATA)
    nine_models = tuple(
        _row(1, occurred_at=day, model=f"model-{index}")
        for index in range(9)
    )
    all_sources = tuple(
        _row(1, occurred_at=day, source=source, model=f"{source}-model")
        for source in ("claude", "codex", "opencode", "pi")
    )

    assert "model-collector" not in _keys(nine_models)
    assert "model-collector" in _keys(
        nine_models + (_row(1, occurred_at=day, model="model-9"),)
    )
    assert "all-systems-go" not in _keys(all_sources[:-1])
    assert "all-systems-go" in _keys(all_sources)
    assert "all-systems-go" not in _keys(
        all_sources[:-1] + (_row(0, occurred_at=day, source="pi"),)
    )


def test_earned_awards_follow_catalog_order_and_use_generic_copy() -> None:
    day = datetime(2026, 8, 30, 9, tzinfo=KOLKATA)
    rows = tuple(
        _row(
            1_000_000_000,
            occurred_at=day + timedelta(days=index),
            agent_key=f"agent:{index}",
            model=f"model-{index % 10}",
            source=("claude", "codex", "opencode", "pi")[index % 4],
        )
        for index in range(14)
    ) + tuple(
        _row(1, occurred_at=day, agent_key=f"fleet:{index}")
        for index in range(249)
    )

    awards = derive_awards(rows, timezone=KOLKATA)

    assert tuple(award.key for award in awards) == (
        "tokenmaxxer",
        "billion-day",
        "fleet-commander",
        "hot-streak",
        "model-collector",
        "all-systems-go",
    )
    assert tuple(award.name for award in awards) == (
        "Tokenmaxxer",
        "Billion Day",
        "Fleet Commander",
        "Hot Streak",
        "Model Collector",
        "All Systems Go",
    )
    assert all(isinstance(award, Award) and award.description for award in awards)
