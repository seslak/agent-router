#!/usr/bin/env python3
"""Pricing tests for Agent Router."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pricing import PricingError, derive_tier, estimate_credits


class PricingTests(unittest.TestCase):
    def test_gpt_54_nano_medium_profile(self) -> None:
        model = {
            "id": "gpt-5.4-nano",
            "pricing": {"default": {"input": 0.20, "cachedInput": 0.02, "output": 1.25}},
        }
        profile = {"inputTokens": 30000, "cachedInputTokens": 15000, "outputTokens": 5000}
        payload = estimate_credits(model, profile)
        self.assertEqual(payload["credits"], 1.255)
        self.assertEqual(payload["pricingApplied"], "default")

    def test_claude_sonnet_46_includes_cache_write(self) -> None:
        model = {
            "id": "claude-sonnet-4.6",
            "pricing": {"default": {"input": 3.00, "cachedInput": 0.30, "cacheWrite": 3.75, "output": 15.00}},
        }
        profile = {"inputTokens": 30000, "cachedInputTokens": 15000, "outputTokens": 5000}
        payload = estimate_credits(model, profile)
        self.assertEqual(payload["credits"], 22.575)

    def test_long_context_applies_only_above_threshold(self) -> None:
        model = {
            "id": "gpt-5.4",
            "pricing": {
                "default": {"input": 2.50, "cachedInput": 0.25, "output": 15.00},
                "longContext": {"thresholdInputTokens": 272000, "input": 5.00, "cachedInput": 0.50, "output": 22.50},
            },
        }
        profile = {"inputTokens": 30000, "cachedInputTokens": 15000, "outputTokens": 5000}
        self.assertEqual(estimate_credits(model, profile, estimated_input_tokens=300000)["pricingApplied"], "long_context")
        self.assertEqual(estimate_credits(model, profile, estimated_input_tokens=50000)["pricingApplied"], "default")

    def test_anthropic_never_long_context(self) -> None:
        model = {
            "id": "claude-sonnet-4.6",
            "pricing": {"default": {"input": 3.00, "cachedInput": 0.30, "cacheWrite": 3.75, "output": 15.00}},
        }
        payload = estimate_credits(model, {"inputTokens": 1, "cachedInputTokens": 1, "outputTokens": 1}, estimated_input_tokens=999999)
        self.assertEqual(payload["pricingApplied"], "default")

    def test_missing_pricing_raises(self) -> None:
        with self.assertRaises(PricingError):
            estimate_credits({"id": "broken", "pricing": None}, {"inputTokens": 1, "cachedInputTokens": 1, "outputTokens": 1})

    def test_tier_boundaries_are_inclusive(self) -> None:
        bands = {"cheapMaxCredits": 5, "balancedMaxCredits": 25}
        self.assertEqual(derive_tier(0, bands), "free")
        self.assertEqual(derive_tier(5, bands), "cheap")
        self.assertEqual(derive_tier(25, bands), "balanced")
        self.assertEqual(derive_tier(25.001, bands), "expensive")


if __name__ == "__main__":
    unittest.main()
