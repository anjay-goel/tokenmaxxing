from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from tokenmaxxing.db import Database
from tokenmaxxing.models import (
    CountingStatus,
    CostUsage,
    Projection,
    ReportingRow,
    RunDraft,
    SessionDraft,
    TokenUsage,
    UsageEventDraft,
)
from tokenmaxxing import reporting
from tokenmaxxing.reporting import event_total, export_payload, usage_stats
from tokenmaxxing.repository import Repository


KOLKATA = ZoneInfo("Asia/Kolkata")
NEW_YORK = ZoneInfo("America/New_York")


def _event(
    event_key: str,
    *,
    status: CountingStatus = "canonical",
    model: str | None = None,
    response_model: str | None = None,
    tokens: TokenUsage = TokenUsage(),
    cost: CostUsage = CostUsage(),
    session_id: int | None = None,
    run_id: int | None = None,
    started_at_ns: int | None = None,
    provider: str | None = None,
    service_tier: str | None = None,
    speed: str | None = None,
    inference_region: str | None = None,
) -> UsageEventDraft:
    return UsageEventDraft(
        source="claude",
        event_key=event_key,
        granularity="model_call",
        status=status,
        tokens=tokens,
        cost=cost,
        model=model,
        response_model=response_model,
        session_id=session_id,
        run_id=run_id,
        started_at_ns=started_at_ns,
        provider=provider,
        service_tier=service_tier,
        speed=speed,
        inference_region=inference_region,
    )


def test_reporting_rows_expose_cost_estimation_metadata_with_event_precedence(
    repository: Repository,
    database: Database,
) -> None:
    repository.apply_projection(
        Projection(
            sessions=(
                SessionDraft(
                    source="claude",
                    source_session_id="pricing-session",
                    provider="session-provider",
                    current_model="session-model",
                    service_tier="session-tier",
                ),
            ),
            runs=(
                RunDraft(
                    source="claude",
                    source_session_id="pricing-session",
                    source_run_id="pricing-run",
                    provider="run-provider",
                    model="run-model",
                ),
            ),
        )
    )
    run_id = database.connection.execute(
        "SELECT id FROM runs WHERE source_run_id = 'pricing-run'"
    ).fetchone()[0]
    repository.apply_projection(
        Projection(
            events=(
                _event(
                    "pricing-event",
                    run_id=run_id,
                    provider="event-provider",
                    response_model="event-model",
                    service_tier="event-tier",
                    speed="fast",
                    inference_region="eu",
                    tokens=TokenUsage(
                        input=10,
                        cache_write=7,
                        cache_write_5m=5,
                        cache_write_1h=2,
                    ),
                ),
            )
        )
    )

    (row,) = repository.reporting_rows()

    assert row.provider == "event-provider"
    assert row.granularity == "model_call"
    assert row.resolved_model == "event-model"
    assert row.requested_model == "run-model"
    assert row.cache_write_5m_tokens == 5
    assert row.cache_write_1h_tokens == 2
    assert row.service_tier == "event-tier"
    assert row.speed == "fast"
    assert row.inference_region == "eu"


def test_source_stats_count_counted_events_and_prefer_reported_totals(
    repository: Repository,
) -> None:
    repository.apply_projection(
        Projection(
            events=(
                _event(
                    "reported",
                    response_model="response-model",
                    tokens=TokenUsage(
                        input=9,
                        output=7,
                        cache_read=2,
                        cache_write=1,
                        reasoning=3,
                        reported_total=17,
                    ),
                    cost=CostUsage(total_nanos=200),
                ),
                _event(
                    "derived",
                    status="provisional",
                    model="fallback-model",
                    tokens=TokenUsage(
                        input=1,
                        output=4,
                        cache_read=1,
                        cache_write=2,
                        reasoning=1,
                    ),
                ),
                _event(
                    "excluded",
                    status="excluded",
                    tokens=TokenUsage(input=100),
                    cost=CostUsage(total_nanos=1000),
                ),
                _event(
                    "conflicted",
                    status="conflicted",
                    tokens=TokenUsage(input=100),
                    cost=CostUsage(total_nanos=1000),
                ),
            )
        )
    )

    assert repository.reporting_rows() == [
        ReportingRow(
            source="claude",
            granularity="model_call",
            provider=None,
            resolved_model="fallback-model",
            requested_model="fallback-model",
            occurred_at_ns=None,
            input_tokens=1,
            output_tokens=4,
            cache_read_tokens=1,
            cache_write_tokens=2,
            cache_write_5m_tokens=None,
            cache_write_1h_tokens=None,
            reasoning_tokens=1,
            reported_total_tokens=None,
            derived_total_tokens=None,
            total_cost_nanos=None,
            service_tier=None,
            speed=None,
            inference_region=None,
        ),
        ReportingRow(
            source="claude",
            granularity="model_call",
            provider=None,
            resolved_model="response-model",
            requested_model=None,
            occurred_at_ns=None,
            input_tokens=9,
            output_tokens=7,
            cache_read_tokens=2,
            cache_write_tokens=1,
            cache_write_5m_tokens=None,
            cache_write_1h_tokens=None,
            reasoning_tokens=3,
            reported_total_tokens=17,
            derived_total_tokens=None,
            total_cost_nanos=200,
            service_tier=None,
            speed=None,
            inference_region=None,
        ),
    ]

    (source_stat,) = usage_stats(repository, "source", KOLKATA)

    assert source_stat.group == "claude"
    assert source_stat.event_count == 2
    assert source_stat.total_tokens == 25
    assert source_stat.reasoning_tokens == 4
    assert source_stat.cost_covered_events == 1
    assert source_stat.cost_nanos == 200


def test_codex_component_total_does_not_add_cached_input_twice() -> None:
    row = ReportingRow(
        source="codex",
        granularity="counter_delta",
        provider="openai",
        resolved_model="gpt-5.6-sol",
        requested_model="gpt-5.6-sol",
        occurred_at_ns=None,
        input_tokens=100,
        output_tokens=10,
        cache_read_tokens=40,
        cache_write_tokens=3,
        cache_write_5m_tokens=None,
        cache_write_1h_tokens=None,
        reasoning_tokens=8,
        reported_total_tokens=None,
        derived_total_tokens=None,
        total_cost_nanos=None,
        service_tier=None,
        speed=None,
        inference_region=None,
    )

    assert event_total(row) == 113


def test_model_stats_prefer_response_model_and_label_unknown_models(
    repository: Repository,
    database: Database,
) -> None:
    repository.apply_projection(
        Projection(
            sessions=(
                SessionDraft(
                    source="claude",
                    source_session_id="current-session",
                    current_model="session-current-model",
                ),
                SessionDraft(
                    source="claude",
                    source_session_id="initial-session",
                    initial_model="session-initial-model",
                ),
            ),
            runs=(
                RunDraft(
                    source="claude",
                    source_session_id="current-session",
                    source_run_id="model-run",
                    model="run-model",
                ),
            ),
        )
    )
    connection = database.connection
    run_id = connection.execute(
        "SELECT id FROM runs WHERE source_run_id = 'model-run'"
    ).fetchone()[0]
    current_session_id = connection.execute(
        "SELECT id FROM sessions WHERE source_session_id = 'current-session'"
    ).fetchone()[0]
    initial_session_id = connection.execute(
        "SELECT id FROM sessions WHERE source_session_id = 'initial-session'"
    ).fetchone()[0]
    repository.apply_projection(
        Projection(
            events=(
                _event(
                    "response-model",
                    model="event-model",
                    response_model="response-model",
                    tokens=TokenUsage(input=1),
                ),
                _event("run-model", run_id=run_id, tokens=TokenUsage(input=3)),
                _event(
                    "current-model",
                    session_id=current_session_id,
                    tokens=TokenUsage(input=5),
                ),
                _event(
                    "initial-model",
                    session_id=initial_session_id,
                    tokens=TokenUsage(input=4),
                ),
                _event("unknown-model", tokens=TokenUsage(input=2)),
            )
        )
    )

    stats = usage_stats(repository, "model", KOLKATA)

    assert [(stat.group, stat.total_tokens) for stat in stats] == [
        ("(unknown)", 2),
        ("response-model", 1),
        ("run-model", 3),
        ("session-current-model", 5),
        ("session-initial-model", 4),
    ]
    assert all(stat.cost_nanos is None for stat in stats)
    assert all(stat.cost_covered_events == 0 for stat in stats)


def test_day_stats_use_event_then_run_then_session_timestamp_in_requested_timezone(
    repository: Repository,
    database: Database,
) -> None:
    repository.apply_projection(
        Projection(
            sessions=(
                SessionDraft(
                    source="claude",
                    source_session_id="session",
                    started_at_ns=1_756_656_000_000_000_000,
                ),
            ),
            runs=(
                RunDraft(
                    source="claude",
                    source_session_id="session",
                    source_run_id="run",
                    started_at_ns=1_756_666_800_000_000_000,
                ),
            ),
        )
    )
    connection = database.connection
    session_id = connection.execute("SELECT id FROM sessions").fetchone()[0]
    run_id = connection.execute("SELECT id FROM runs").fetchone()[0]
    repository.apply_projection(
        Projection(
            events=(
                _event(
                    "session-time", session_id=session_id, tokens=TokenUsage(input=1)
                ),
                _event("run-time", run_id=run_id, tokens=TokenUsage(input=2)),
                _event(
                    "event-time",
                    run_id=run_id,
                    started_at_ns=1_756_656_000_000_000_000,
                    tokens=TokenUsage(input=3),
                ),
            )
        )
    )

    stats = usage_stats(repository, "day", KOLKATA)

    assert [(stat.group, stat.event_count, stat.total_tokens) for stat in stats] == [
        ("2025-08-31", 2, 4),
        ("2025-09-01", 1, 2),
    ]


def test_report_windows_filter_on_half_open_local_day_boundaries(
    repository: Repository,
) -> None:
    repository.apply_projection(
        Projection(
            events=(
                _event(
                    "before-seven-days",
                    model="before-seven-days",
                    started_at_ns=1_779_474_599_000_000_000,
                    tokens=TokenUsage(input=1),
                ),
                _event(
                    "first-seven-day",
                    model="first-seven-day",
                    started_at_ns=1_779_474_600_000_000_000,
                    tokens=TokenUsage(input=2),
                ),
                _event(
                    "last-seven-day",
                    model="last-seven-day",
                    started_at_ns=1_780_079_399_000_000_000,
                    tokens=TokenUsage(input=3),
                ),
                _event(
                    "after-seven-days",
                    model="after-seven-days",
                    started_at_ns=1_780_079_400_000_000_000,
                    tokens=TokenUsage(input=4),
                ),
                _event(
                    "before-twenty-eight-days",
                    model="before-twenty-eight-days",
                    started_at_ns=1_777_660_199_000_000_000,
                    tokens=TokenUsage(input=5),
                ),
                _event(
                    "first-twenty-eight-day",
                    model="first-twenty-eight-day",
                    started_at_ns=1_777_660_200_000_000_000,
                    tokens=TokenUsage(input=6),
                ),
                _event("unknown-time", model="unknown-time", tokens=TokenUsage(input=6)),
            )
        )
    )
    now = datetime(2026, 5, 29, 10, tzinfo=UTC)

    seven_days = usage_stats(
        repository,
        "model",
        KOLKATA,
        window=reporting.ReportWindow.from_period("7d", KOLKATA, now),
    )
    twenty_eight_days = usage_stats(
        repository,
        "model",
        KOLKATA,
        window=reporting.ReportWindow.from_period("28d", KOLKATA, now),
    )
    all_time = usage_stats(
        repository,
        "model",
        KOLKATA,
        window=reporting.ReportWindow.from_period("all", KOLKATA, now),
    )

    assert [(stat.group, stat.total_tokens) for stat in seven_days] == [
        ("first-seven-day", 2),
        ("last-seven-day", 3),
    ]
    assert [(stat.group, stat.total_tokens) for stat in twenty_eight_days] == [
        ("before-seven-days", 1),
        ("first-seven-day", 2),
        ("first-twenty-eight-day", 6),
        ("last-seven-day", 3),
    ]
    assert [(stat.group, stat.total_tokens) for stat in all_time] == [
        ("after-seven-days", 4),
        ("before-seven-days", 1),
        ("before-twenty-eight-days", 5),
        ("first-seven-day", 2),
        ("first-twenty-eight-day", 6),
        ("last-seven-day", 3),
        ("unknown-time", 6),
    ]


def test_report_window_from_days_supports_arbitrary_local_day_counts() -> None:
    now = datetime(2026, 11, 2, 12, tzinfo=NEW_YORK)

    window = reporting.ReportWindow.from_days(2, NEW_YORK, now)

    assert window.period == "2d"
    assert window.includes(
        _reporting_row(
            int(datetime(2026, 11, 1, 0, tzinfo=NEW_YORK).timestamp())
            * 1_000_000_000
        )
    )
    assert window.includes(
        _reporting_row(
            int(datetime(2026, 11, 2, 23, 59, 59, tzinfo=NEW_YORK).timestamp())
            * 1_000_000_000
        )
    )
    assert not window.includes(
        _reporting_row(
            int(datetime(2026, 10, 31, 23, 59, 59, tzinfo=NEW_YORK).timestamp())
            * 1_000_000_000
        )
    )
    assert not window.includes(
        _reporting_row(
            int(datetime(2026, 11, 3, 0, tzinfo=NEW_YORK).timestamp())
            * 1_000_000_000
        )
    )


def _reporting_row(occurred_at_ns: int) -> ReportingRow:
    return ReportingRow(
        source="claude",
        granularity="model_call",
        provider=None,
        resolved_model="model",
        requested_model="model",
        occurred_at_ns=occurred_at_ns,
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        cache_write_5m_tokens=None,
        cache_write_1h_tokens=None,
        reasoning_tokens=None,
        reported_total_tokens=None,
        derived_total_tokens=None,
        total_cost_nanos=None,
        service_tier=None,
        speed=None,
        inference_region=None,
    )


def test_report_windows_follow_new_york_dst_day_boundaries() -> None:
    for now, expected_hours in (
        (datetime(2026, 3, 14, 12, tzinfo=UTC), 167),
        (datetime(2026, 11, 7, 12, tzinfo=UTC), 169),
    ):
        window = reporting.ReportWindow.from_period("7d", NEW_YORK, now)

        assert window.end_ns is not None
        assert window.start_ns is not None
        assert window.end_ns - window.start_ns == expected_hours * 3_600_000_000_000
        assert window.includes(_reporting_row(window.start_ns))
        assert window.includes(_reporting_row(window.end_ns - 1))
        assert not window.includes(_reporting_row(window.start_ns - 1))
        assert not window.includes(_reporting_row(window.end_ns))


def test_export_contains_only_aggregate_allowlisted_keys_recursively(
    repository: Repository,
) -> None:
    repository.apply_projection(
        Projection(
            events=(
                _event(
                    "export",
                    model="sonnet",
                    tokens=TokenUsage(input=17),
                    started_at_ns=1_756_670_400_000_000_000,
                ),
            )
        )
    )

    payload = export_payload(
        repository,
        KOLKATA,
        datetime(2026, 8, 29, tzinfo=UTC),
    )

    assert payload == {
        "schema_version": 1,
        "generated_at": "2026-08-29T00:00:00+00:00",
        "timezone": "Asia/Kolkata",
        "overall": {"group": "all", "event_count": 1, "total_tokens": 17},
        "by_source": [{"group": "claude", "event_count": 1, "total_tokens": 17}],
        "by_model": [{"group": "sonnet", "event_count": 1, "total_tokens": 17}],
        "by_day": [{"group": "2025-09-01", "event_count": 1, "total_tokens": 17}],
    }

    forbidden = {"session", "request", "response", "artifact", "path", "workspace"}

    def assert_safe(value: object) -> None:
        if isinstance(value, dict):
            for key, nested_value in value.items():
                assert all(word not in key.lower() for word in forbidden)
                assert_safe(nested_value)
        elif isinstance(value, list):
            for nested_value in value:
                assert_safe(nested_value)

    assert_safe(payload)
