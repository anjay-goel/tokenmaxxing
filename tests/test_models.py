from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from tokenmaxxing.models import (
    CostUsage,
    ObservationDraft,
    Projection,
    TokenUsage,
    decimal_to_nanodollars,
)


def test_token_usage_rejects_negative_and_impossible_reasoning() -> None:
    with pytest.raises(ValueError):
        TokenUsage(input=-1)
    with pytest.raises(ValueError):
        TokenUsage(output=3, reasoning=4)


def test_token_usage_is_an_immutable_record() -> None:
    usage = TokenUsage(input=2, output=3, reasoning=1)

    with pytest.raises(FrozenInstanceError):
        usage.input = 4  # type: ignore[misc]


def test_cost_decimal_conversion_uses_exact_nanodollars() -> None:
    assert decimal_to_nanodollars(Decimal("0.123456789")) == 123_456_789
    assert CostUsage.from_decimal("1.000000001").total_nanos == 1_000_000_001
    with pytest.raises(ValueError):
        decimal_to_nanodollars(True)


def test_observation_projection_is_immutable_and_projection_is_empty_by_default() -> None:
    projection = {"usage": {"input": 1}}
    observation = ObservationDraft(
        source="codex",
        channel="disk",
        stable_key="session:1",
        event_type="usage",
        observed_at_ns=1,
        parser_version="v1",
        projection=projection,
    )

    projection["usage"]["input"] = 99
    assert observation.projection == {"usage": {"input": 1}}
    with pytest.raises(TypeError):
        observation.projection["usage"] = {"input": 2}  # type: ignore[index]
    with pytest.raises(TypeError):
        observation.projection["usage"]["input"] = 2  # type: ignore[index]
    assert Projection() == Projection()


def test_observation_rejects_a_projection_that_crosses_the_privacy_boundary() -> None:
    with pytest.raises(ValueError):
        ObservationDraft(
            source="codex",
            channel="disk",
            stable_key="session:1",
            event_type="usage",
            observed_at_ns=1,
            parser_version="v1",
            projection={"metadata": {"note": "PRIVATE_SENTINEL"}},
        )
