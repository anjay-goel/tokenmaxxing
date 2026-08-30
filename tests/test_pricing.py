import json
from datetime import datetime
from pathlib import Path

import pytest

from tokenmaxxing.models import ReportingRow, Source
from tokenmaxxing.pricing import estimate_api_value_rows, load_rate_card


def _ns(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp()) * 1_000_000_000


def _row(
    *,
    source: Source = "pi",
    granularity: str = "model_call",
    provider: str | None = "test-provider",
    model: str = "test-model",
    requested_model: str | None = None,
    occurred_at: str | None = "2026-08-30T12:00:00+00:00",
    input_tokens: int | None = 0,
    output_tokens: int | None = 0,
    cache_read_tokens: int | None = 0,
    cache_write_tokens: int | None = 0,
    cache_write_5m_tokens: int | None = None,
    cache_write_1h_tokens: int | None = None,
    reasoning_tokens: int | None = 0,
    reported_total_tokens: int | None = None,
    derived_total_tokens: int | None = None,
    total_cost_nanos: int | None = None,
    service_tier: str | None = None,
    speed: str | None = None,
    inference_region: str | None = None,
) -> ReportingRow:
    return ReportingRow(
        source=source,
        granularity=granularity,
        provider=provider,
        resolved_model=model,
        requested_model=requested_model,
        occurred_at_ns=_ns(occurred_at) if occurred_at else None,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        cache_write_5m_tokens=cache_write_5m_tokens,
        cache_write_1h_tokens=cache_write_1h_tokens,
        reasoning_tokens=reasoning_tokens,
        reported_total_tokens=reported_total_tokens,
        derived_total_tokens=derived_total_tokens,
        total_cost_nanos=total_cost_nanos,
        service_tier=service_tier,
        speed=speed,
        inference_region=inference_region,
    )


def _card(tmp_path: Path, models: list[dict[str, object]]) -> Path:
    path = tmp_path / "rate-card.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "currency": "USD",
                "unit_tokens": 1_000_000,
                "provider_aliases": {"test-provider": ["test-alias"]},
                "models": models,
            }
        ),
        encoding="utf-8",
    )
    return path


def _model(
    *,
    model: str = "test-model",
    aliases: list[str] | None = None,
    prices: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "provider": "test-provider",
        "model": model,
        "aliases": aliases or [],
        "pricing_url": "https://example.com/pricing",
        "prices": prices
        or [
            {
                "effective_from": "2026-01-01T00:00:00Z",
                "input": "2",
                "cached_input": "0.2",
                "cache_write": "3",
                "cache_write_5m": "4",
                "cache_write_1h": "5",
                "output": "10",
            }
        ],
    }


def test_estimator_uses_exact_aliases_and_requested_model_fallback(tmp_path: Path) -> None:
    card = load_rate_card(_card(tmp_path, [_model(aliases=["test-model-202608"])]))

    estimate = estimate_api_value_rows(
        [
            _row(provider="test-alias", model="test-model-202608", input_tokens=1_000_000),
            _row(model="unpriced-response", requested_model="test-model", output_tokens=100_000),
        ],
        card,
    )

    assert estimate.cost_nanos == 3_000_000_000
    assert estimate.priced_events == 2
    assert estimate.total_events == 2


def test_provider_reported_cost_wins_even_for_unknown_and_zero_cost_models(
    tmp_path: Path,
) -> None:
    card = load_rate_card(_card(tmp_path, []))

    estimate = estimate_api_value_rows(
        [
            _row(model="unknown-paid", input_tokens=20, total_cost_nanos=123),
            _row(model="unknown-free", input_tokens=30, total_cost_nanos=0),
        ],
        card,
    )

    assert estimate.cost_nanos == 123
    assert estimate.priced_events == 2
    assert estimate.priced_tokens == 50


def test_estimator_uses_non_overlapping_source_token_components(tmp_path: Path) -> None:
    card = load_rate_card(_card(tmp_path, [_model()]))

    codex = estimate_api_value_rows(
        [
            _row(
                source="codex",
                input_tokens=100,
                cache_read_tokens=40,
                output_tokens=10,
                reasoning_tokens=8,
                reported_total_tokens=110,
            )
        ],
        card,
    )
    separate = estimate_api_value_rows(
        [
            _row(
                source="claude",
                input_tokens=60,
                cache_read_tokens=40,
                output_tokens=10,
                reasoning_tokens=8,
                reported_total_tokens=110,
            )
        ],
        card,
    )

    assert codex.cost_nanos == separate.cost_nanos == 228_000
    assert codex.priced_tokens == separate.priced_tokens == 110


def test_cache_write_subtypes_replace_their_share_of_the_total(tmp_path: Path) -> None:
    card = load_rate_card(_card(tmp_path, [_model()]))

    estimate = estimate_api_value_rows(
        [
            _row(
                input_tokens=0,
                cache_write_tokens=10,
                cache_write_5m_tokens=4,
                cache_write_1h_tokens=3,
                reported_total_tokens=10,
            )
        ],
        card,
    )

    assert estimate.cost_nanos == 40_000


def test_unknown_or_partially_priceable_events_are_unpriced(tmp_path: Path) -> None:
    card = load_rate_card(
        _card(
            tmp_path,
            [
                _model(
                    prices=[
                        {
                            "effective_from": "2026-01-01T00:00:00Z",
                            "input": "2",
                            "output": "10",
                        }
                    ]
                )
            ],
        )
    )

    estimate = estimate_api_value_rows(
        [
            _row(model="unknown", input_tokens=20, reported_total_tokens=20),
            _row(cache_read_tokens=30, reported_total_tokens=30),
        ],
        card,
    )

    assert estimate.cost_nanos == 0
    assert estimate.priced_events == 0
    assert estimate.priced_tokens == 0
    assert estimate.total_events == 2
    assert estimate.total_tokens == 50


@pytest.mark.parametrize(
    "row",
    [
        _row(
            input_tokens=None,
            output_tokens=None,
            cache_read_tokens=None,
            cache_write_tokens=None,
            reported_total_tokens=1_000_000,
        ),
        _row(input_tokens=10, output_tokens=5, reported_total_tokens=20),
    ],
)
def test_catalog_pricing_requires_complete_reconciled_token_anatomy(
    tmp_path: Path, row: ReportingRow
) -> None:
    card = load_rate_card(_card(tmp_path, [_model()]))

    estimate = estimate_api_value_rows([row], card)

    assert estimate.cost_nanos == 0
    assert estimate.priced_events == 0
    assert estimate.priced_tokens == 0
    assert estimate.total_tokens == row.reported_total_tokens


def test_explicit_provider_cannot_fall_through_to_another_provider(tmp_path: Path) -> None:
    card = load_rate_card(_card(tmp_path, [_model()]))

    estimate = estimate_api_value_rows(
        [_row(provider="different-provider", input_tokens=1_000_000)], card
    )

    assert estimate.priced_events == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [("service_tier", "mystery"), ("speed", "turbo"), ("inference_region", "moon")],
)
def test_unknown_billing_modifiers_leave_events_unpriced(
    tmp_path: Path, field: str, value: str
) -> None:
    card = load_rate_card(_card(tmp_path, [_model()]))

    estimate = estimate_api_value_rows(
        [_row(input_tokens=1_000_000, **{field: value})], card  # type: ignore[arg-type]
    )

    assert estimate.priced_events == 0


def test_rate_selection_supports_dates_long_context_tiers_regions_and_schedules(
    tmp_path: Path,
) -> None:
    card = load_rate_card(
        _card(
            tmp_path,
            [
                _model(
                    prices=[
                        {
                            "effective_from": "2026-01-01T00:00:00Z",
                            "effective_until": "2026-08-01T00:00:00Z",
                            "input": "1",
                            "cached_input": "1",
                            "cache_write": "1",
                            "output": "1",
                        },
                        {
                            "effective_from": "2026-08-01T00:00:00Z",
                            "input": "2",
                            "cached_input": "0.2",
                            "cache_write": "3",
                            "output": "10",
                            "long_context": {
                                "above_input_tokens": 100,
                                "input": "4",
                                "cached_input": "0.4",
                                "cache_write": "6",
                                "output": "15",
                            },
                            "tier_multipliers": {"fast": "2"},
                            "region_multipliers": {"eu": "1.1"},
                            "time_multipliers": [
                                {
                                    "weekdays": [0],
                                    "start_hour_utc": 1,
                                    "end_hour_utc": 4,
                                    "multiplier": "0.5",
                                }
                            ],
                        },
                    ]
                )
            ],
        )
    )

    estimate = estimate_api_value_rows(
        [
            _row(occurred_at="2026-07-01T12:00:00+00:00", input_tokens=1_000_000),
            _row(
                occurred_at="2026-08-03T02:00:00+00:00",
                input_tokens=101,
                output_tokens=10,
                service_tier="fast",
                inference_region="eu",
            ),
        ],
        card,
    )

    assert estimate.cost_nanos == 1_000_000_000 + 609_400


def test_aggregate_above_a_long_context_threshold_uses_the_base_rate(tmp_path: Path) -> None:
    card = load_rate_card(
        _card(
            tmp_path,
            [
                _model(
                    prices=[
                        {
                            "effective_from": "2026-01-01T00:00:00Z",
                            "input": "2",
                            "cached_input": "0.2",
                            "cache_write": "3",
                            "output": "10",
                            "long_context": {
                                "above_input_tokens": 100,
                                "input": "4",
                                "cached_input": "0.4",
                                "cache_write": "6",
                                "output": "15",
                            },
                        }
                    ]
                )
            ],
        )
    )

    estimate = estimate_api_value_rows(
        [_row(granularity="counter_delta", input_tokens=101)],
        card,
    )

    assert estimate.priced_events == 1
    assert estimate.cost_nanos == 202_000


def test_long_context_threshold_can_be_inclusive(tmp_path: Path) -> None:
    card = load_rate_card(
        _card(
            tmp_path,
            [
                _model(
                    prices=[
                        {
                            "effective_from": "2026-01-01T00:00:00Z",
                            "input": "2",
                            "output": "10",
                            "long_context": {
                                "above_input_tokens": 100,
                                "inclusive": True,
                                "input": "4",
                                "output": "15",
                            },
                        }
                    ]
                )
            ],
        )
    )

    estimate = estimate_api_value_rows([_row(input_tokens=100)], card)

    assert estimate.cost_nanos == 400_000


def test_codex_cached_input_cannot_exceed_its_inclusive_input_total(tmp_path: Path) -> None:
    card = load_rate_card(_card(tmp_path, [_model()]))

    estimate = estimate_api_value_rows(
        [_row(source="codex", input_tokens=10, cache_read_tokens=11)],
        card,
    )

    assert estimate.priced_events == 0


def test_inconsistent_cache_write_subtypes_leave_the_event_unpriced(tmp_path: Path) -> None:
    card = load_rate_card(_card(tmp_path, [_model()]))

    estimate = estimate_api_value_rows(
        [
            _row(
                cache_write_tokens=5,
                cache_write_5m_tokens=4,
                cache_write_1h_tokens=3,
            )
        ],
        card,
    )

    assert estimate.priced_events == 0


def test_rate_card_rejects_ambiguous_aliases(tmp_path: Path) -> None:
    path = _card(
        tmp_path,
        [
            _model(model="first", aliases=["shared"]),
            _model(model="second", aliases=["shared"]),
        ],
    )

    with pytest.raises(ValueError, match="duplicate model alias"):
        load_rate_card(path)


def test_rate_card_rejects_provider_alias_collisions(tmp_path: Path) -> None:
    path = tmp_path / "rate-card.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "currency": "USD",
                "unit_tokens": 1_000_000,
                "provider_aliases": {
                    "first": ["shared"],
                    "second": ["shared"],
                },
                "models": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate provider alias"):
        load_rate_card(path)


def test_rate_card_rejects_overlapping_price_periods(tmp_path: Path) -> None:
    path = _card(
        tmp_path,
        [
            _model(
                prices=[
                    {
                        "effective_from": "2026-01-01T00:00:00Z",
                        "effective_until": "2026-09-01T00:00:00Z",
                        "input": "1",
                    },
                    {
                        "effective_from": "2026-08-01T00:00:00Z",
                        "input": "2",
                    },
                ]
            )
        ],
    )

    with pytest.raises(ValueError, match="overlapping price periods"):
        load_rate_card(path)


def test_rate_card_rejects_periods_without_rates(tmp_path: Path) -> None:
    path = _card(
        tmp_path,
        [_model(prices=[{"effective_from": "2026-01-01T00:00:00Z"}])],
    )

    with pytest.raises(ValueError, match="at least one token rate"):
        load_rate_card(path)


def test_rate_card_rejects_overlapping_time_multipliers(tmp_path: Path) -> None:
    path = _card(
        tmp_path,
        [
            _model(
                prices=[
                    {
                        "effective_from": "2026-01-01T00:00:00Z",
                        "input": "1",
                        "time_multipliers": [
                            {
                                "weekdays": [0],
                                "start_hour_utc": 1,
                                "end_hour_utc": 4,
                                "multiplier": "2",
                            },
                            {
                                "weekdays": [0],
                                "start_hour_utc": 3,
                                "end_hour_utc": 5,
                                "multiplier": "3",
                            },
                        ],
                    }
                ]
            )
        ],
    )

    with pytest.raises(ValueError, match="must not overlap"):
        load_rate_card(path)


def test_rate_card_rejects_invalid_retrieval_dates(tmp_path: Path) -> None:
    model = _model()
    model["retrieved_at"] = "yesterday"
    path = _card(tmp_path, [model])

    with pytest.raises(ValueError, match="ISO 8601 date"):
        load_rate_card(path)


@pytest.mark.parametrize(
    ("provider", "model", "input_rate"),
    [
        ("deepseek", "deepseek-v4-pro", 0.66),
        ("zai", "glm-5.3", 1.4),
        ("moonshot", "kimi-k3", 3),
        ("google", "gemini-3.7-flash", 0.75),
        ("xai", "grok-4.6", 2),
        ("mistral", "mistral-medium-3.5", 1.5),
        ("qwen", "qwen3.8-max", 2),
    ],
)
def test_bundled_card_prices_popular_model_families(
    provider: str, model: str, input_rate: float
) -> None:
    estimate = estimate_api_value_rows(
        [
            _row(
                provider=provider,
                model=model,
                granularity="counter_delta",
                input_tokens=1_000_000,
            )
        ],
        load_rate_card(),
    )

    assert estimate.priced_events == 1
    assert estimate.cost_nanos == int(input_rate * 1_000_000_000)


@pytest.mark.parametrize(
    ("provider", "model", "occurred_at", "input_rate", "cached_rate", "output_rate"),
    [
        ("openai", "gpt-5.6-sol", "2026-08-30T12:00:00+00:00", 4, 0.4, 20),
        ("anthropic", "claude-sonnet-5", "2026-09-02T12:00:00+00:00", 2, 0.2, 10),
        ("deepseek", "deepseek-v4-pro", "2026-08-17T12:00:00+00:00", 0.66, 0.022, 1.98),
    ],
)
def test_bundled_card_uses_exact_current_component_rates(
    provider: str,
    model: str,
    occurred_at: str,
    input_rate: float,
    cached_rate: float,
    output_rate: float,
) -> None:
    card = load_rate_card()
    rows = [
        _row(
            provider=provider,
            model=model,
            occurred_at=occurred_at,
            granularity="counter_delta",
            input_tokens=1_000_000,
        ),
        _row(
            provider=provider,
            model=model,
            occurred_at=occurred_at,
            granularity="counter_delta",
            input_tokens=0,
            cache_read_tokens=1_000_000,
        ),
        _row(
            provider=provider,
            model=model,
            occurred_at=occurred_at,
            granularity="counter_delta",
            output_tokens=1_000_000,
        ),
    ]

    estimates = [estimate_api_value_rows([row], card) for row in rows]

    assert [estimate.cost_nanos for estimate in estimates] == [
        int(input_rate * 1_000_000_000),
        int(cached_rate * 1_000_000_000),
        int(output_rate * 1_000_000_000),
    ]


def test_bundled_card_uses_openai_fast_multiplier_and_deepseek_boundary() -> None:
    card = load_rate_card()

    fast = estimate_api_value_rows(
        [
            _row(
                source="codex",
                provider="openai",
                model="gpt-5.6-terra",
                granularity="counter_delta",
                input_tokens=1_000_000,
                service_tier="fast",
            )
        ],
        card,
    )
    before_deepseek = estimate_api_value_rows(
        [
            _row(
                provider="deepseek",
                model="deepseek-v4-pro",
                occurred_at="2026-08-16T15:59:59+00:00",
                input_tokens=1_000_000,
            )
        ],
        card,
    )

    assert fast.cost_nanos == 4_000_000_000
    assert before_deepseek.priced_events == 0
