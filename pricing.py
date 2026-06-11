"""Deterministic pricing helpers for Agent Router."""

from __future__ import annotations

from typing import Any


CREDIT_USD = 0.01


class PricingError(ValueError):
    """Raised when model pricing data is missing or malformed."""


def _require_number(value: Any, field_name: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise PricingError("Invalid pricing field: {0}".format(field_name)) from exc


def _pricing_row(model: dict[str, Any], estimated_input_tokens: int | None = None) -> tuple[dict[str, Any], str, int | None]:
    pricing = model.get("pricing")
    if not isinstance(pricing, dict):
        raise PricingError("Missing pricing data.")
    default_row = pricing.get("default")
    if not isinstance(default_row, dict):
        raise PricingError("Missing pricing.default data.")
    long_row = pricing.get("longContext")
    if isinstance(long_row, dict):
        threshold = long_row.get("thresholdInputTokens")
        try:
            threshold_value = int(threshold)
        except (TypeError, ValueError) as exc:
            raise PricingError("Invalid longContext thresholdInputTokens.") from exc
        if estimated_input_tokens is not None and estimated_input_tokens > threshold_value:
            return long_row, "long_context", threshold_value
    return default_row, "default", None


def estimate_credits(
    model: dict[str, Any],
    cost_profile: dict[str, Any],
    estimated_input_tokens: int | None = None,
) -> dict[str, Any]:
    """Estimate AI credits for a model and cost profile."""

    row, applied, default_threshold = _pricing_row(model, estimated_input_tokens=estimated_input_tokens)
    input_tokens = int(cost_profile.get("inputTokens", 0) or 0)
    cached_input_tokens = int(cost_profile.get("cachedInputTokens", 0) or 0)
    output_tokens = int(cost_profile.get("outputTokens", 0) or 0)
    cache_write_tokens = int(cost_profile.get("cacheWriteTokens", cached_input_tokens) or 0)

    price_input = _require_number(row.get("input"), "input")
    price_cached = _require_number(row.get("cachedInput"), "cachedInput")
    price_output = _require_number(row.get("output"), "output")
    price_cache_write = 0.0
    if "cacheWrite" in row:
        price_cache_write = _require_number(row.get("cacheWrite"), "cacheWrite")

    usd = (
        (float(input_tokens) * price_input)
        + (float(cached_input_tokens) * price_cached)
        + (float(output_tokens) * price_output)
        + (float(cache_write_tokens) * price_cache_write)
    ) / 1000000.0
    credits = round(usd / CREDIT_USD, 3)
    return {
        "credits": credits,
        "pricingApplied": applied,
        "thresholdInputTokens": default_threshold,
    }


def derive_tier(credits: float, tier_bands: dict[str, Any]) -> str:
    if credits <= 0:
        return "free"
    cheap_max = float(tier_bands.get("cheapMaxCredits", 5))
    balanced_max = float(tier_bands.get("balancedMaxCredits", 25))
    if credits <= cheap_max:
        return "cheap"
    if credits <= balanced_max:
        return "balanced"
    return "expensive"
