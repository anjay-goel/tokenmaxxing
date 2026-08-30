from decimal import Decimal, ROUND_HALF_UP

from tokenmaxxing.pricing import ApiValueEstimate


def compact_tokens(tokens: int) -> str:
    scales = ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K"))
    for index, (threshold, suffix) in enumerate(scales):
        if tokens >= threshold:
            scaled = f"{tokens / threshold:.1f}"
            if scaled == "1000.0" and index:
                threshold, suffix = scales[index - 1]
                scaled = f"{tokens / threshold:.1f}"
            return f"{scaled.rstrip('0').rstrip('.')}{suffix}"
    return str(tokens)


def compact_usd(cost_nanos: int) -> str:
    if cost_nanos == 0:
        return "$0"
    dollars = Decimal(cost_nanos) / Decimal(1_000_000_000)
    if dollars < Decimal("0.01"):
        return "<$0.01"
    if dollars < 10:
        value = dollars.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"${format(value, 'f').rstrip('0').rstrip('.')}"
    if dollars < 100:
        return f"${dollars.quantize(Decimal('1'), rounding=ROUND_HALF_UP)}"
    scales = (
        (Decimal(1_000_000_000), "B"),
        (Decimal(1_000_000), "M"),
        (Decimal(1_000), "K"),
        (Decimal(1), ""),
    )
    scale_index, threshold, suffix = next(
        (index, threshold, suffix)
        for index, (threshold, suffix) in enumerate(scales)
        if dollars >= threshold
    )
    value = (dollars / threshold).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if value == 1000 and scale_index:
        threshold, suffix = scales[scale_index - 1]
        value = (dollars / threshold).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"${format(value, 'f').rstrip('0').rstrip('.')}{suffix}"


def api_value_text(estimate: ApiValueEstimate) -> str | None:
    if estimate.total_tokens and estimate.priced_tokens * 100 < estimate.total_tokens * 95:
        return None
    return compact_usd(estimate.cost_nanos)


_QUIPS = (
    (100_000, "Just a light snack."),
    (1_000_000, "A tidy little token trail."),
    (10_000_000, "The agents are stretching their legs."),
    (100_000_000, "Autocomplete has been promoted to middle management."),
    (1_000_000_000, "The agents have started holding stand-ups."),
    (10_000_000_000, "A small civilization has entered the context window."),
)


def usage_quip(tokens: int) -> str:
    if tokens == 0:
        return "Quiet. Suspiciously human."
    for upper_bound, copy in _QUIPS:
        if tokens < upper_bound:
            return copy
    return "The context window now has a GDP."
