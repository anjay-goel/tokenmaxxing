import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tokenmaxxing.pricing import ApiValueEstimate
from tokenmaxxing.profile.config import (
    DeployConfig,
    MetricsConfig,
    ProfileConfig,
    ProfileInfo,
    ProfileLink,
    ScheduleConfig,
    SiteConfig,
)
from tokenmaxxing.profile.data import build_profile_data
from tokenmaxxing.profile.render import public_payload


def _config(avatar: Path | None = None) -> ProfileConfig:
    return ProfileConfig(
        version=1,
        profile=ProfileInfo(
            name="Ada Lovelace",
            bio="Makes machines think.",
            avatar=avatar,
            links=(
                ProfileLink(
                    label="Website",
                    value="ada.example",
                    url="https://ada.example/",
                ),
            ),
        ),
        site=SiteConfig(
            title="Ada's token trail",
            description="Aggregate local AI agent usage.",
            canonical_url="https://example.com/tokens/",
            indexable=True,
            timezone=ZoneInfo("UTC"),
        ),
        metrics=MetricsConfig(),
        deploy=DeployConfig(),
        schedule=ScheduleConfig(),
    )


def _data():
    timezone = ZoneInfo("UTC")
    return build_profile_data(
        (),
        timezone=timezone,
        now=datetime(2026, 8, 30, 12, tzinfo=timezone),
        window_days=28,
    )


def test_public_payload_contains_only_allowlisted_aggregate_fields() -> None:
    payload = public_payload(_config(Path("/private/avatar.webp")), _data())
    encoded = json.dumps(payload, sort_keys=True)

    assert set(payload) == {
        "schema_version",
        "generated_at",
        "profile",
        "site",
        "stats",
    }
    assert payload["profile"]["avatar"] == "assets/avatar.webp"
    for forbidden in (
        "session_id",
        "run_id",
        "event_id",
        "/private/",
        "prompt",
        "reasoning",
    ):
        assert forbidden not in encoded


def test_public_payload_uses_api_card_only_at_ninety_five_percent_coverage() -> None:
    priced = replace(
        _data(),
        api_equivalent=ApiValueEstimate(
            cost_nanos=8_059_030_000_000,
            priced_tokens=95,
            total_tokens=100,
            priced_events=1,
            total_events=1,
            by_provider=(),
        ),
        agent_count=7,
        peak_usage=2_000_000,
        longest_streak=4,
        model_count=3,
    )
    unpriced = replace(
        priced,
        api_equivalent=replace(priced.api_equivalent, priced_tokens=94),
    )

    assert [card["label"] for card in public_payload(_config(), priced)["stats"]["cards"]] == [
        "API equivalent",
        "Agents",
        "Peak usage",
        "Longest streak",
    ]
    assert [card["label"] for card in public_payload(_config(), unpriced)["stats"]["cards"]] == [
        "Agents",
        "Peak usage",
        "Longest streak",
        "Models",
    ]


def test_disabling_api_equivalent_restores_models_as_the_fourth_card() -> None:
    data = replace(
        _data(),
        api_equivalent=ApiValueEstimate(
            cost_nanos=8_059_030_000_000,
            priced_tokens=100,
            total_tokens=100,
            priced_events=1,
            total_events=1,
            by_provider=(),
        ),
        model_count=3,
    )
    config = replace(
        _config(),
        metrics=replace(_config().metrics, show_api_equivalent=False),
    )

    assert [card["label"] for card in public_payload(config, data)["stats"]["cards"]] == [
        "Agents",
        "Peak usage",
        "Longest streak",
        "Models",
    ]


def test_empty_usage_uses_the_models_fallback_card() -> None:
    assert [card["label"] for card in public_payload(_config(), _data())["stats"]["cards"]] == [
        "Agents",
        "Peak usage",
        "Longest streak",
        "Models",
    ]


def test_public_payload_keeps_exact_daily_conservation() -> None:
    data = _data()
    payload = public_payload(_config(), data)
    agents = payload["stats"]["agents"]

    assert len(agents) == 28
    assert all(day["agents"] == sum(model["agents"] for model in day["models"]) for day in agents)
    assert sum(day["agents"] for day in agents) == 0
