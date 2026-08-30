from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from tokenmaxxing.db import Database
from tokenmaxxing.models import (
    CountingStatus,
    CostUsage,
    Projection,
    RunDraft,
    SessionDraft,
    TokenUsage,
    UsageEventDraft,
)
from tokenmaxxing.reporting import export_payload, usage_stats
from tokenmaxxing.repository import Repository


KOLKATA = ZoneInfo("Asia/Kolkata")


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
    )


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

    (source_stat,) = usage_stats(repository, "source", KOLKATA)

    assert source_stat.group == "claude"
    assert source_stat.event_count == 2
    assert source_stat.total_tokens == 25
    assert source_stat.reasoning_tokens == 4
    assert source_stat.cost_covered_events == 1
    assert source_stat.cost_nanos == 200


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
