"""Shared constants for Agent Router."""

from __future__ import annotations

TIER_ORDER = {
    "free": 0,
    "cheap": 1,
    "balanced": 2,
    "expensive": 3,
    "blocked": 4,
}

ROUTE_TYPES = frozenset({"WORKFLOW", "SPECIALIST_AGENT", "MANUAL_PLAN_FIRST"})
RISK_LEVELS = frozenset({"low", "medium", "high"})
COMPLEXITY_LEVELS = frozenset({"low", "medium", "high"})
BLAST_RADIUS_LEVELS = frozenset({"low", "medium", "high"})
MODEL_TIERS = frozenset({"free", "cheap", "balanced", "expensive", "blocked"})
PRICING_APPLIED = frozenset({"default", "long_context", "blocked"})
KNOWN_APPROVAL_CONDITIONS = frozenset(
    {
        "tier_expensive",
        "model_requires_approval",
        "specialist_approval_always",
        "long_context_pricing",
    }
)

REQUIRED_DECISION_FIELDS = (
    "decisionId",
    "createdAt",
    "task",
    "taskClass",
    "routeType",
    "modelTier",
    "approvalRequired",
    "reason",
    "matchedSignals",
    "rankedModels",
)
