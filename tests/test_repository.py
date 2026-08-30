import pytest

from tokenmaxxing.db import Database
from tokenmaxxing.models import (
    Channel,
    LinkDraft,
    ObservationDraft,
    Projection,
    RunDraft,
    SessionDraft,
    TokenUsage,
    UsageEventDraft,
)
from tokenmaxxing.repository import Repository


@pytest.fixture
def repo(database: Database) -> Repository:
    return Repository(database)


def observation(stable_key: str, channel: Channel = "disk") -> ObservationDraft:
    return ObservationDraft(
        source="claude",
        channel=channel,
        stable_key=stable_key,
        event_type="usage",
        observed_at_ns=1,
        parser_version="v1",
        projection={"usage": {"input": 5}},
        source_session_id="session-1",
    )


def event(
    event_key: str,
    *,
    tokens: TokenUsage = TokenUsage(input=5),
    model: str | None = "claude-sonnet",
    duration_ns: int | None = None,
) -> UsageEventDraft:
    return UsageEventDraft(
        source="claude",
        event_key=event_key,
        granularity="model_call",
        status="canonical",
        tokens=tokens,
        model=model,
        duration_ns=duration_ns,
    )


def linked_projection(stable_key: str, event_key: str) -> Projection:
    return Projection(
        observations=(observation(stable_key),),
        events=(event(event_key),),
        links=(
            LinkDraft(
                source="claude",
                channel="disk",
                observation_key=stable_key,
                event_key=event_key,
                method="source_identity",
                role="primary",
                confidence="exact",
            ),
        ),
    )


def test_projection_keys_and_links_are_idempotent(repo: Repository) -> None:
    projection = linked_projection("disk:1", "claude:1")

    first = repo.apply_projection(projection)
    second = repo.apply_projection(projection)

    assert first.observations_inserted == 1
    assert first.events_inserted == 1
    assert first.links_inserted == 1
    assert second.observations_inserted == 0
    assert second.events_inserted == 0
    assert second.links_inserted == 0
    assert repo.observation_count("claude") == 1
    assert repo.event_count("claude") == 1


def test_connection_exposes_the_sql_adapter_boundary(repo: Repository) -> None:
    assert repo.connection.execute("SELECT 1").fetchone() == (1,)


def test_event_upsert_enriches_sparse_fields(repo: Repository) -> None:
    repo.apply_projection(Projection(events=(event("claude:1", tokens=TokenUsage(input=5)),)))

    stats = repo.apply_projection(
        Projection(
            events=(
                event(
                    "claude:1",
                    tokens=TokenUsage(output=7),
                    model=None,
                    duration_ns=25,
                ),
            )
        )
    )

    stored = repo.get_event("claude:1")
    assert stats.events_updated == 1
    assert stored is not None
    assert stored.tokens == TokenUsage(input=5, output=7)
    assert stored.model == "claude-sonnet"
    assert stored.duration_ns == 25


def test_progressive_observations_remain_separate_evidence(repo: Repository) -> None:
    repo.apply_projection(linked_projection("disk:progress:1", "claude:message-1"))
    repo.apply_projection(linked_projection("disk:progress:2", "claude:message-1"))

    assert repo.observation_count("claude") == 2
    assert repo.event_count("claude") == 1
    assert repo.totals()[0].tokens.input == 5


def test_links_are_many_to_many(repo: Repository) -> None:
    observations = (observation("disk:outer"), observation("disk:iteration"))
    events = (event("claude:outer"), event("claude:iteration"))
    links = tuple(
        LinkDraft(
            source="claude",
            channel="disk",
            observation_key=observation_key,
            event_key=event_key,
            method="decomposition",
            role="supporting",
            confidence="deterministic",
        )
        for observation_key in ("disk:outer", "disk:iteration")
        for event_key in ("claude:outer", "claude:iteration")
    )

    stats = repo.apply_projection(Projection(observations=observations, events=events, links=links))

    assert stats.links_inserted == 4
    assert repo.channels_for_event("claude:outer") == {"disk"}
    assert repo.channels_for_event("claude:iteration") == {"disk"}


def test_projection_write_rolls_back_atomically_on_a_dangling_link(repo: Repository) -> None:
    projection = Projection(
        observations=(observation("disk:rollback"),),
        events=(event("claude:rollback"),),
        links=(
            LinkDraft(
                source="claude",
                channel="disk",
                observation_key="missing",
                event_key="claude:rollback",
                method="source_identity",
                role="primary",
                confidence="exact",
            ),
        ),
    )

    with pytest.raises(ValueError, match="link target"):
        repo.apply_projection(projection)

    assert repo.observation_count("claude") == 0
    assert repo.event_count("claude") == 0


def test_link_resolves_the_full_observation_identity(
    repo: Repository,
    database: Database,
) -> None:
    stable_key = "shared"
    event_key = "claude:shared"
    projection = Projection(
        observations=(
            observation(stable_key, "disk"),
            observation(stable_key, "otel"),
        ),
        events=(event(event_key),),
        links=(
            LinkDraft(
                source="claude",
                channel="disk",
                observation_key=stable_key,
                event_key=event_key,
                method="source_identity",
                role="primary",
                confidence="exact",
            ),
        ),
    )

    stats = repo.apply_projection(projection)

    assert stats.links_inserted == 1
    rows = database.connection.execute(
        "SELECT o.source, o.channel, o.stable_key FROM observations o "
        "JOIN observation_links l ON l.observation_id = o.id"
    ).fetchall()
    assert rows == [("claude", "disk", stable_key)]


def test_grouped_totals_sum_countable_events_only(repo: Repository) -> None:
    repo.apply_projection(
        Projection(
            events=(
                event("claude:sonnet-1", tokens=TokenUsage(input=5), model="sonnet"),
                event("claude:sonnet-2", tokens=TokenUsage(input=7), model="sonnet"),
                event("claude:opus", tokens=TokenUsage(input=11), model="opus"),
                UsageEventDraft(
                    source="claude",
                    event_key="claude:excluded",
                    granularity="model_call",
                    status="excluded",
                    tokens=TokenUsage(input=100),
                    model="sonnet",
                ),
            )
        )
    )

    totals = {total.group: total.tokens.input for total in repo.totals(group_by="model")}
    assert totals == {"opus": 11, "sonnet": 12}
    assert repo.source_total("claude").tokens.input == 23
    assert repo.list_event_keys("claude") == {
        "claude:excluded",
        "claude:opus",
        "claude:sonnet-1",
        "claude:sonnet-2",
    }

    with pytest.raises(ValueError, match="group_by"):
        repo.totals(group_by="event_key; DROP TABLE usage_events")


def test_profile_reporting_rows_group_root_and_child_executions_privately(
    repo: Repository,
) -> None:
    repo.apply_projection(
        Projection(
            sessions=tuple(
                SessionDraft(source="codex", source_session_id=f"session-{index}")
                for index in range(1, 5)
            ),
            runs=(
                RunDraft(
                    source="codex",
                    source_session_id="session-1",
                    source_run_id="root-sentinel",
                ),
                RunDraft(
                    source="codex",
                    source_session_id="session-2",
                    source_run_id="child",
                    parent_run_id="root-sentinel",
                ),
                RunDraft(
                    source="codex",
                    source_session_id="session-3",
                    source_run_id="grandchild",
                    parent_run_id="child",
                ),
            ),
        )
    )
    session_ids = {
        source_id: database_id
        for database_id, source_id in repo.connection.execute(
            "SELECT id, source_session_id FROM sessions ORDER BY id"
        )
    }
    run_ids = {
        source_id: database_id
        for database_id, source_id in repo.connection.execute(
            "SELECT id, source_run_id FROM runs ORDER BY id"
        )
    }
    events = (
        UsageEventDraft(
            source="codex",
            event_key="root-direct",
            granularity="counter_delta",
            status="canonical",
            tokens=TokenUsage(input=10),
            model="a-root-direct",
            session_id=session_ids["session-1"],
        ),
        UsageEventDraft(
            source="codex",
            event_key="root-sentinel",
            granularity="counter_delta",
            status="canonical",
            tokens=TokenUsage(input=20),
            model="b-root-sentinel",
            run_id=run_ids["root-sentinel"],
        ),
        UsageEventDraft(
            source="codex",
            event_key="child-1",
            granularity="counter_delta",
            status="canonical",
            tokens=TokenUsage(input=30),
            model="c-child-1",
            run_id=run_ids["child"],
        ),
        UsageEventDraft(
            source="codex",
            event_key="child-2",
            granularity="counter_delta",
            status="provisional",
            tokens=TokenUsage(input=40),
            model="d-child-2",
            run_id=run_ids["child"],
        ),
        UsageEventDraft(
            source="codex",
            event_key="grandchild",
            granularity="counter_delta",
            status="canonical",
            tokens=TokenUsage(input=50),
            model="e-grandchild",
            run_id=run_ids["grandchild"],
        ),
        UsageEventDraft(
            source="codex",
            event_key="unidentified",
            granularity="counter_delta",
            status="canonical",
            tokens=TokenUsage(input=60),
            model="f-unidentified",
        ),
        UsageEventDraft(
            source="codex",
            event_key="zero",
            granularity="counter_delta",
            status="canonical",
            tokens=TokenUsage(input=0),
            model="g-zero",
            session_id=session_ids["session-4"],
        ),
        UsageEventDraft(
            source="codex",
            event_key="excluded",
            granularity="counter_delta",
            status="excluded",
            tokens=TokenUsage(input=70),
            model="h-excluded",
            session_id=session_ids["session-4"],
        ),
        UsageEventDraft(
            source="codex",
            event_key="conflicted",
            granularity="counter_delta",
            status="conflicted",
            tokens=TokenUsage(input=80),
            model="i-conflicted",
            session_id=session_ids["session-4"],
        ),
    )
    repo.apply_projection(Projection(events=events))

    rows = repo.profile_reporting_rows()

    assert [row.agent_key for row in rows] == [
        "codex:session:1",
        "codex:session:1",
        "codex:run:2",
        "codex:run:2",
        "codex:run:3",
        None,
        "codex:session:4",
    ]
    assert repo.reporting_rows() == [row.usage for row in rows]
    assert all(not hasattr(row.usage, "agent_key") for row in rows)
