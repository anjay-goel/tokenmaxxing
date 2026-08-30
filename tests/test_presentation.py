import re

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
    assert usage_quip(13_343_876_259) == "The context window now has a GDP."


def test_usage_quips_do_not_address_the_profile_owner_or_reader() -> None:
    messages = (
        usage_quip(tokens)
        for tokens in (0, 99_999, 999_999, 9_999_999, 99_999_999,
                       999_999_999, 9_999_999_999, 10_000_000_000)
    )
    personal_pronoun = re.compile(
        r"\b(?:i|me|my|mine|we|us|our|ours|you|your|yours)\b", re.IGNORECASE
    )

    assert all(personal_pronoun.search(message) is None for message in messages)


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
