from datetime import datetime
from zoneinfo import ZoneInfo

from tokenmaxxing.models import ProfileUsageRow, ReportingRow, Source
from tokenmaxxing.profile.awards import derive_awards
from tokenmaxxing.profile.data import (
    AgentModelTotal,
    DailyAgentTotal,
    ModelTotal,
    build_profile_data,
)
from tokenmaxxing.reporting import event_total


KOLKATA = ZoneInfo("Asia/Kolkata")
NEW_YORK = ZoneInfo("America/New_York")


def _row(
    total_tokens: int,
    *,
    model: str,
    occurred_at: datetime | None,
    agent_key: str | None,
    source: Source = "codex",
    provider: str | None = None,
) -> ProfileUsageRow:
    usage = ReportingRow(
        source=source,
        granularity="counter_delta" if source == "codex" else "model_call",
        provider=provider,
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
    )
    return ProfileUsageRow(usage=usage, agent_key=agent_key)


def test_build_profile_data_derives_every_window_metric_from_one_snapshot() -> None:
    now = datetime(2026, 8, 30, 12, tzinfo=KOLKATA)
    rows = (
        _row(
            100,
            model="gpt-5.6-sol",
            occurred_at=datetime(2026, 8, 29, 9, tzinfo=KOLKATA),
            agent_key="codex:session:1",
        ),
        _row(
            20,
            model="gpt-5.6-sol",
            occurred_at=datetime(2026, 8, 30, 9, tzinfo=KOLKATA),
            agent_key="codex:session:1",
        ),
        _row(
            50,
            model="gpt-5.6-sol",
            occurred_at=datetime(2026, 8, 30, 10, tzinfo=KOLKATA),
            agent_key="codex:run:2",
        ),
        _row(
            30,
            model="claude-sonnet-4-5",
            occurred_at=datetime(2026, 8, 29, 10, tzinfo=KOLKATA),
            agent_key="claude:session:3",
            source="claude",
        ),
        _row(
            40,
            model="claude-sonnet-4-5",
            occurred_at=datetime(2026, 8, 30, 11, tzinfo=KOLKATA),
            agent_key="claude:session:3",
            source="claude",
        ),
        _row(
            7,
            model="unidentified-model",
            occurred_at=datetime(2026, 8, 28, 12, tzinfo=KOLKATA),
            agent_key=None,
            source="opencode",
        ),
        _row(
            0,
            model="zero-model",
            occurred_at=datetime(2026, 8, 28, 13, tzinfo=KOLKATA),
            agent_key="pi:session:4",
            source="pi",
        ),
        _row(
            1,
            model="boundary-model",
            occurred_at=datetime(2026, 8, 3, 0, tzinfo=KOLKATA),
            agent_key=None,
            source="pi",
        ),
        _row(
            1_000,
            model="before-model",
            occurred_at=datetime(2026, 8, 2, 23, 59, 59, tzinfo=KOLKATA),
            agent_key="codex:session:outside",
        ),
        _row(
            2_000,
            model="future-model",
            occurred_at=datetime(2026, 8, 31, 0, tzinfo=KOLKATA),
            agent_key="codex:session:future",
        ),
        _row(2, model="missing-time", occurred_at=None, agent_key="codex:session:missing"),
    )

    data = build_profile_data(rows, timezone=KOLKATA, now=now, window_days=28)
    recent_rows = rows[:8]

    assert data.generated_at == now
    assert data.window_start.isoformat() == "2026-08-03"
    assert data.window_end.isoformat() == "2026-08-30"
    assert data.total_tokens == sum(event_total(row.usage) for row in recent_rows) == 248
    assert data.all_time_tokens == 3_250
    assert data.api_equivalent.total_tokens == 248
    assert data.api_equivalent.total_events == 8
    assert data.agent_count == 3
    assert data.agent_models == (
        AgentModelTotal(model="gpt-5.6-sol", agents=2),
        AgentModelTotal(model="claude-sonnet-4-5", agents=1),
    )
    assert data.peak_usage == 130
    assert data.longest_streak == 2
    assert data.model_count == 5
    assert data.models == (
        ModelTotal(model="gpt-5.6-sol", total_tokens=170, provider=None),
        ModelTotal(model="claude-sonnet-4-5", total_tokens=70, provider=None),
        ModelTotal(model="unidentified-model", total_tokens=7, provider="opencode"),
        ModelTotal(model="boundary-model", total_tokens=1, provider=None),
        ModelTotal(model="zero-model", total_tokens=0, provider=None),
    )
    assert len(data.recent_days) == 28
    assert all(isinstance(day, DailyAgentTotal) for day in data.recent_days)
    assert data.recent_days[0].day.isoformat() == "2026-08-03"
    assert data.recent_days[-1].day.isoformat() == "2026-08-30"
    assert data.recent_days[-2].agents == 2
    assert data.recent_days[-2].models == (
        AgentModelTotal(model="claude-sonnet-4-5", agents=1),
        AgentModelTotal(model="gpt-5.6-sol", agents=1),
    )
    assert data.recent_days[-1].agents == 3
    assert data.recent_days[-1].models == (
        AgentModelTotal(model="gpt-5.6-sol", agents=2),
        AgentModelTotal(model="claude-sonnet-4-5", agents=1),
    )
    assert all(
        day.agents == sum(model.agents for model in day.models)
        for day in data.recent_days
    )
    assert all(not hasattr(day, "total_tokens") for day in data.recent_days)
    assert len(data.activity_days) == 364
    assert data.activity_days[0].day.isoformat() == "2025-09-01"
    assert data.activity_days[-1].day.isoformat() == "2026-08-30"
    assert data.activity_days[-2].total_tokens == 130
    assert data.activity_days[-2].models == (
        ModelTotal(model="gpt-5.6-sol", total_tokens=100, provider=None),
        ModelTotal(model="claude-sonnet-4-5", total_tokens=30, provider=None),
    )
    assert data.activity_days[-1].total_tokens == 110
    assert all(
        day.total_tokens == sum(model.total_tokens for model in day.models)
        for day in data.activity_days
    )
    assert data.peak_usage == max(
        day.total_tokens for day in data.activity_days[-28:]
    )
    assert data.first_tracked_day is not None
    assert data.first_tracked_day.isoformat() == "2026-08-02"
    assert data.quip == "Just a light snack."
    assert data.awards == derive_awards(rows, timezone=KOLKATA)


def test_model_provider_uses_positive_token_majority_and_alphabetical_ties() -> None:
    now = datetime(2026, 8, 30, 12, tzinfo=KOLKATA)
    rows = (
        _row(
            80,
            model="majority",
            occurred_at=now,
            agent_key=None,
            provider="openai",
        ),
        _row(
            20,
            model="majority",
            occurred_at=now,
            agent_key=None,
            provider="anthropic",
        ),
        _row(
            10,
            model="tie",
            occurred_at=now,
            agent_key=None,
            provider="openai",
        ),
        _row(
            10,
            model="tie",
            occurred_at=now,
            agent_key=None,
            provider="anthropic",
        ),
        _row(
            7,
            model="opencode-fallback",
            occurred_at=now,
            agent_key=None,
            source="opencode",
        ),
        _row(
            5,
            model="unknown-provider",
            occurred_at=now,
            agent_key=None,
        ),
        _row(
            0,
            model="zero-provider",
            occurred_at=now,
            agent_key=None,
            provider="openai",
        ),
    )

    data = build_profile_data(rows, timezone=KOLKATA, now=now, window_days=28)
    providers = {model.model: model.provider for model in data.models}

    assert data.total_tokens == 132
    assert providers == {
        "majority": "openai",
        "tie": "anthropic",
        "opencode-fallback": "opencode",
        "unknown-provider": None,
        "zero-provider": None,
    }
    assert data.activity_days[-1].total_tokens == 132
    assert {
        model.model: model.provider for model in data.activity_days[-1].models
    } == providers


def test_agent_count_uses_source_execution_keys_not_calls_or_batch_markers() -> None:
    timestamp = datetime(2026, 8, 30, 12, tzinfo=KOLKATA)
    rows = (
        _row(10, model="gpt", occurred_at=timestamp, agent_key="codex:session:1"),
        _row(20, model="gpt", occurred_at=timestamp, agent_key="codex:session:1"),
        _row(30, model="gpt", occurred_at=timestamp, agent_key="codex:run:2"),
        _row(40, model="gpt", occurred_at=timestamp, agent_key="codex:run:2"),
        _row(
            10,
            model="claude",
            occurred_at=timestamp,
            agent_key="claude:session:3",
            source="claude",
        ),
        _row(
            20,
            model="claude",
            occurred_at=timestamp,
            agent_key="claude:session:3",
            source="claude",
        ),
        _row(
            5,
            model="pi",
            occurred_at=timestamp,
            agent_key="pi:session:4",
            source="pi",
        ),
        _row(
            6,
            model="pi",
            occurred_at=timestamp,
            agent_key="pi:run:5",
            source="pi",
        ),
        _row(
            7,
            model="pi",
            occurred_at=timestamp,
            agent_key="pi:run:5",
            source="pi",
        ),
        _row(
            0,
            model="pi",
            occurred_at=timestamp,
            agent_key="pi:run:batch-marker",
            source="pi",
        ),
        _row(
            8,
            model="opencode",
            occurred_at=timestamp,
            agent_key="opencode:session:6",
            source="opencode",
        ),
        _row(
            9,
            model="opencode",
            occurred_at=timestamp,
            agent_key="opencode:run:7",
            source="opencode",
        ),
        _row(
            10,
            model="opencode",
            occurred_at=timestamp,
            agent_key=None,
            source="opencode",
        ),
    )

    data = build_profile_data(rows, timezone=KOLKATA, now=timestamp, window_days=28)

    assert data.agent_count == 7
    assert data.agent_models == (
        AgentModelTotal(model="gpt", agents=2),
        AgentModelTotal(model="opencode", agents=2),
        AgentModelTotal(model="pi", agents=2),
        AgentModelTotal(model="claude", agents=1),
    )
    assert data.recent_days[-1].agents == 7
    assert data.recent_days[-1].models == (
        AgentModelTotal(model="gpt", agents=2),
        AgentModelTotal(model="opencode", agents=2),
        AgentModelTotal(model="pi", agents=2),
        AgentModelTotal(model="claude", agents=1),
    )
    assert data.recent_days[-1].agents == sum(
        model.agents for model in data.recent_days[-1].models
    )
    assert data.longest_streak == 1


def test_primary_model_ties_are_alphabetical_and_agents_count_once_across_days() -> None:
    rows = (
        _row(
            10,
            model="z-model",
            occurred_at=datetime(2026, 8, 29, 12, tzinfo=KOLKATA),
            agent_key="codex:session:1",
        ),
        _row(
            10,
            model="a-model",
            occurred_at=datetime(2026, 8, 30, 12, tzinfo=KOLKATA),
            agent_key="codex:session:1",
        ),
    )

    data = build_profile_data(
        rows,
        timezone=KOLKATA,
        now=datetime(2026, 8, 30, 12, tzinfo=KOLKATA),
        window_days=28,
    )

    assert data.agent_count == 1
    assert data.agent_models == (AgentModelTotal(model="a-model", agents=1),)
    assert data.longest_streak == 2
    assert data.recent_days[-2].agents == 1
    assert data.recent_days[-2].models == (
        AgentModelTotal(model="a-model", agents=1),
    )
    assert data.recent_days[-1].agents == 1
    assert data.recent_days[-1].models == (
        AgentModelTotal(model="a-model", agents=1),
    )


def test_local_window_handles_dst_folds_and_half_open_boundaries() -> None:
    rows = (
        _row(
            1,
            model="before",
            occurred_at=datetime(2026, 10, 31, 23, 59, 59, tzinfo=NEW_YORK),
            agent_key=None,
        ),
        _row(
            2,
            model="start",
            occurred_at=datetime(2026, 11, 1, 0, tzinfo=NEW_YORK),
            agent_key="codex:session:1",
        ),
        _row(
            3,
            model="fold-zero",
            occurred_at=datetime(2026, 11, 1, 1, 30, fold=0, tzinfo=NEW_YORK),
            agent_key="codex:session:1",
        ),
        _row(
            4,
            model="fold-one",
            occurred_at=datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=NEW_YORK),
            agent_key="codex:session:1",
        ),
        _row(
            5,
            model="end-day",
            occurred_at=datetime(2026, 11, 2, 23, 59, 59, tzinfo=NEW_YORK),
            agent_key="codex:session:1",
        ),
        _row(
            6,
            model="after",
            occurred_at=datetime(2026, 11, 3, 0, tzinfo=NEW_YORK),
            agent_key=None,
        ),
    )

    data = build_profile_data(
        rows,
        timezone=NEW_YORK,
        now=datetime(2026, 11, 2, 12, tzinfo=NEW_YORK),
        window_days=2,
    )

    assert data.window_start.isoformat() == "2026-11-01"
    assert data.window_end.isoformat() == "2026-11-02"
    assert data.total_tokens == 14
    assert tuple(day.agents for day in data.recent_days) == (1, 1)
    assert tuple(day.total_tokens for day in data.activity_days[-2:]) == (9, 5)
    assert data.agent_count == 1
    assert data.longest_streak == 2


def test_empty_and_trillion_scale_snapshots_preserve_exact_integers() -> None:
    now = datetime(2026, 8, 30, 12, tzinfo=KOLKATA)

    empty = build_profile_data((), timezone=KOLKATA, now=now, window_days=28)
    large = build_profile_data(
        (
            _row(
                1_000_000_000_001,
                model="huge",
                occurred_at=now,
                agent_key="codex:session:1",
            ),
        ),
        timezone=KOLKATA,
        now=now,
        window_days=28,
    )

    assert empty.total_tokens == 0
    assert empty.all_time_tokens == 0
    assert empty.agent_count == 0
    assert empty.peak_usage == 0
    assert empty.longest_streak == 0
    assert empty.model_count == 0
    assert empty.awards == ()
    assert empty.first_tracked_day is None
    assert len(empty.recent_days) == 28
    assert all(day.agents == 0 and day.models == () for day in empty.recent_days)
    assert len(empty.activity_days) == 364
    assert large.total_tokens == 1_000_000_000_001
    assert large.all_time_tokens == 1_000_000_000_001
    assert large.peak_usage == 1_000_000_000_001
    assert large.recent_days[-1].agents == 1
