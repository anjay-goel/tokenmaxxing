from tokenmaxxing.presentation import (
    api_value_text,
    compact_tokens,
    compact_usd,
    usage_quip,
)
from tokenmaxxing.pricing import ApiValueEstimate


def test_compact_values_keep_one_useful_decimal() -> None:
    assert compact_tokens(13_343_876_259) == "13.3B"
    assert compact_tokens(999_999) == "1M"
    assert compact_usd(8_059_030_000_000) == "$8.1K"


def test_usage_quip_is_shared_for_the_same_window_total() -> None:
    assert usage_quip(13_343_876_259) == "The tokens have unionized."


def test_api_value_text_requires_ninety_five_percent_coverage() -> None:
    estimate = ApiValueEstimate(
        cost_nanos=8_059_030_000_000,
        priced_tokens=95,
        total_tokens=100,
        priced_events=1,
        total_events=1,
        by_provider=(),
    )
    assert api_value_text(estimate) == "$8.1K"
