"""Core routing logic for Agent Router v0.2.0.

All routing is deterministic keyword/rule-based. No LLM calls, no external requests.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

from schemas import (
    TIER_ORDER,
    ROUTE_TYPES,
    RISK_LEVELS,
    MODEL_TIERS,
    REQUIRED_DECISION_FIELDS,
)

_WORD_RE = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_decision_id() -> str:
    return "rd_" + uuid.uuid4().hex[:12]


def _normalize_text(text: str) -> str:
    return text.lower()


def _text_words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _score_task_class(task_lower: str, task_class: dict[str, Any]) -> tuple[int, list[str]]:
    """Return (score, matched_signals) for one task class against normalized text."""
    score = 0
    signals: list[str] = []
    for kw in task_class.get("keywords", []):
        kw_lower = kw.lower()
        if kw_lower in task_lower:
            kw_words = _WORD_RE.findall(kw_lower)
            score += len(kw_words)
            signals.append(kw)
    return score, signals


def _find_by_id(items: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
    for item in items:
        if item.get("id") == item_id:
            return item
    return None


def _tier_rank(tier: str) -> int:
    return TIER_ORDER.get(tier, 99)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def _contains_any(text: str, phrases: list[str]) -> bool:
    return any(phrase in text for phrase in phrases)


def _forced_task_class_id(task_lower: str) -> str | None:
    """Return a high-priority class for ambiguous phrases.

    The registry scorer is intentionally simple, but some workflow terms overlap:
    "import" can mean a small refactor or a test import error, and "server
    entrypoint" can appear in both documentation and context-inspection tasks.
    These guardrails keep workflow smoke prompts deterministic without making
    registry ordering fragile.
    """
    repeated_test_signals = [
        "still failing",
        "repeated failure",
        "after two fixes",
        "after two attempts",
        "deeply diagnose",
        "keeps failing",
        "hangs",
        "flaky",
    ]
    if _contains_any(task_lower, repeated_test_signals):
        return "test_failure_repeated"

    simple_test_signals = [
        "test fail",
        "test failing",
        "tests are failing",
        "tests failing",
        "failing test",
        "test error",
        "import error",
        "test import error",
        "broken test",
        "diagnose and apply",
    ]
    if _contains_any(task_lower, simple_test_signals):
        return "test_failure_simple"

    docs_signals = [
        "readme",
        "docs",
        "documentation",
        "changelog",
        "release note",
        "release notes",
    ]
    docs_update_signals = [
        "update",
        "sync",
        "write",
        "rewrite",
        "refresh",
        "polish",
        "improve",
        "review and update",
    ]
    if _contains_any(task_lower, docs_signals) and _contains_any(task_lower, docs_update_signals):
        return "documentation_update"

    small_refactor_signals = [
        "small refactor",
        "deterministic code refactor",
        "small deterministic code refactor",
        "small deterministic refactor",
    ]
    if _contains_any(task_lower, small_refactor_signals):
        return "small_code_edit"

    return None


def _classification_from_class(
    best_class: dict[str, Any],
    best_score: int,
    best_signals: list[str],
    forced: bool = False,
) -> dict[str, Any]:
    prefix = "Forced high-priority match" if forced else "Matched task class"
    return {
        "taskClass": best_class["id"],
        "routeType": best_class.get("routeType", "SPECIALIST_AGENT"),
        "riskLevel": best_class.get("defaultRisk", "medium"),
        "complexity": best_class.get("defaultComplexity", "medium"),
        "blastRadius": best_class.get("defaultBlastRadius", "medium"),
        "reason": (
            f"{prefix} '{best_class['id']}' "
            f"(score {best_score}, signals: {', '.join(best_signals) or 'none'})."
        ),
        "matchedSignals": best_signals,
        "suggestedHandler": best_class.get("preferredSpecialist", "architect"),
        "_preferredWorkflow": best_class.get("preferredWorkflow"),
        "_preferredSpecialist": best_class.get("preferredSpecialist"),
    }


def classify_task(
    task_text: str,
    task_classes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Classify a task using deterministic keyword rules.

    Returns a classification dict with taskClass, routeType, risk/complexity/blast,
    reason, matchedSignals, suggestedHandler, and internal _preferred* fields.
    """
    if not task_text:
        return _fallback_classification("empty task text")

    task_lower = _normalize_text(task_text)

    forced_id = _forced_task_class_id(task_lower)
    if forced_id:
        forced_class = _find_by_id(task_classes, forced_id)
        if forced_class:
            score, signals = _score_task_class(task_lower, forced_class)
            return _classification_from_class(forced_class, score, signals, forced=True)

    best_class: dict[str, Any] | None = None
    best_score = 0
    best_signals: list[str] = []

    for tc in task_classes:
        if tc.get("id") == "unknown":
            continue
        score, signals = _score_task_class(task_lower, tc)
        if score > best_score:
            best_score = score
            best_class = tc
            best_signals = signals

    if best_class is None or best_score == 0:
        unknown = _find_by_id(task_classes, "unknown")
        if unknown:
            best_class = unknown
            best_signals = []
        else:
            return _fallback_classification("no matching task class")

    return _classification_from_class(best_class, best_score, best_signals)


def _fallback_classification(reason: str = "") -> dict[str, Any]:
    return {
        "taskClass": "unknown",
        "routeType": "SPECIALIST_AGENT",
        "riskLevel": "medium",
        "complexity": "medium",
        "blastRadius": "medium",
        "reason": f"Fallback classification used ({reason}).",
        "matchedSignals": [],
        "suggestedHandler": "architect",
        "_preferredWorkflow": None,
        "_preferredSpecialist": "architect",
    }


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def route_task(
    task_text: str,
    registries: dict[str, Any],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce a full route decision for the given task.

    Steps:
    1. Classify task
    2. Resolve workflow (if WORKFLOW route type)
    3. Resolve specialist
    4. Get policy constraints
    5. Find candidate models within tier/multiplier bounds
    6. Select cheapest capable model
    7. Determine approval requirement
    8. Build and return RouteDecision
    """
    params = params or {}
    task_classes = registries.get("task_classes", [])
    workflows = registries.get("workflows", [])
    specialists = registries.get("specialists", [])
    all_models = registries.get("models", [])
    policies = registries.get("policies", {})
    default_policy = policies.get("defaultPolicy", {})
    task_policies = policies.get("taskPolicies", {})

    # Step 1: Classify
    classification = classify_task(task_text, task_classes)
    task_class = classification["taskClass"]
    route_type = classification["routeType"]

    # Step 2: Resolve workflow
    workflow_id: str | None = None
    workflow: dict[str, Any] | None = None
    if route_type == "WORKFLOW":
        preferred_wf = classification.get("_preferredWorkflow")
        if preferred_wf:
            workflow = _find_by_id(workflows, preferred_wf)
            if workflow:
                workflow_id = preferred_wf

    # Step 3: Resolve specialist
    specialist_id: str | None = None
    if workflow:
        specialist_id = workflow.get("specialistId") or workflow.get("defaultSpecialist")
    if not specialist_id:
        specialist_id = (
            classification.get("_preferredSpecialist")
            or task_policies.get(task_class, {}).get("preferredSpecialist")
            or "architect"
        )
    specialist = _find_by_id(specialists, specialist_id) if specialist_id else None

    # Step 4: Policy constraints
    task_policy = task_policies.get(task_class, {})
    policy_max_mult = float(task_policy.get("maxMultiplier", default_policy.get("maxDefaultMultiplier", 1)))
    specialist_max_mult = float(specialist.get("maxMultiplier", 99)) if specialist else 99.0
    max_multiplier = min(policy_max_mult, specialist_max_mult)

    policy_max_tier = task_policy.get("maxTier", "expensive")
    policy_min_tier = task_policy.get("minTier")
    specialist_tiers = specialist.get("allowedTiers", ["cheap"]) if specialist else ["cheap"]
    allowed_tiers = [
        t for t in specialist_tiers
        if _tier_rank(t) <= _tier_rank(policy_max_tier)
        and (policy_min_tier is None or _tier_rank(t) >= _tier_rank(policy_min_tier))
    ]

    # Step 5: Find candidate models
    candidates: list[dict[str, Any]] = []
    for m in all_models:
        m_tier = m.get("tier", "")
        m_mult = m.get("premiumRequestMultiplier")
        if m_mult is None:
            behavior = default_policy.get("unknownMultiplierBehavior", "treat_as_expensive")
            if behavior == "treat_as_expensive":
                m_tier = "expensive"
                m_mult = 5.0
            else:
                continue
        if m_tier not in allowed_tiers:
            continue
        if float(m_mult) > max_multiplier:
            continue
        candidates.append(dict(m, _effective_tier=m_tier, _effective_mult=float(m_mult)))

    # Sort cheapest first
    candidates.sort(key=lambda m: (_tier_rank(m.get("tier", "expensive")), m.get("premiumRequestMultiplier", 0)))

    # Step 6: Handle blocked
    if not candidates:
        return _blocked_decision(
            task=task_text,
            classification=classification,
            specialist_id=specialist_id,
            specialist=specialist,
            workflow_id=workflow_id,
            workflow=workflow,
            max_multiplier=max_multiplier,
            allowed_tiers=allowed_tiers,
            task_class=task_class,
        )

    selected = candidates[0]
    model_tier = selected.get("tier", "cheap")
    model_id = selected.get("id", "")

    # Step 7: Approval
    approval_required = False
    approval_reason: str | None = None

    if selected.get("requiresApproval", False):
        approval_required = True
        approval_reason = f"Model '{model_id}' requires explicit approval."

    spec_approval = specialist.get("approvalPolicy", "none") if specialist else "none"
    if spec_approval == "always":
        approval_required = True
        approval_reason = approval_reason or "Specialist policy requires approval for all routes."
    elif spec_approval == "if_expensive" and model_tier == "expensive":
        approval_required = True
        approval_reason = approval_reason or "Expensive model tier requires specialist approval."

    if default_policy.get("requireApprovalForExpensive", True) and model_tier == "expensive":
        approval_required = True
        approval_reason = approval_reason or "Policy requires approval for expensive model tier."

    # Build outputs
    allowed_tools: list[str] = []
    if workflow:
        allowed_tools = list(workflow.get("allowedTools", []))
    elif specialist:
        allowed_tools = list(specialist.get("allowedTools", []))

    required_memory: list[str] = []
    spec_ctx = specialist.get("contextPolicy", "minimal") if specialist else "minimal"
    if spec_ctx in ("full", "bounded"):
        required_memory = ["recall_startup"]

    required_checks: list[str] = []
    if workflow:
        required_checks = list(workflow.get("requiredChecks", []))

    next_step = _build_next_step(route_type, workflow_id, specialist_id, workflow, approval_required)

    signals = classification.get("matchedSignals", [])
    reason = (
        f"Task classified as '{task_class}' "
        f"(signals: {', '.join(signals) if signals else 'none'}). "
        f"Route: {route_type}. "
        f"Specialist: {specialist_id}. "
        f"Model: {model_id} ({model_tier})."
    )
    if task_policy.get("minTier"):
        reason += f" Minimum tier policy: {task_policy['minTier']}."

    return {
        "decisionId": _new_decision_id(),
        "createdAt": _now_iso(),
        "task": task_text,
        "taskClass": task_class,
        "routeType": route_type,
        "workflowId": workflow_id,
        "specialistId": specialist_id,
        "modelTier": model_tier,
        "selectedModelId": model_id,
        "maxAllowedMultiplier": max_multiplier,
        "estimatedCostClass": model_tier,
        "approvalRequired": approval_required,
        "approvalReason": approval_reason,
        "riskLevel": classification.get("riskLevel", "medium"),
        "complexity": classification.get("complexity", "medium"),
        "blastRadius": classification.get("blastRadius", "medium"),
        "allowedTools": allowed_tools,
        "requiredMemory": required_memory,
        "requiredChecks": required_checks,
        "reason": reason,
        "fallbackUsed": task_class == "unknown",
        "blocked": False,
        "blockReason": None,
        "nextStep": next_step,
    }


def _blocked_decision(
    *,
    task: str,
    classification: dict[str, Any],
    specialist_id: str | None,
    specialist: dict[str, Any] | None,
    workflow_id: str | None,
    workflow: dict[str, Any] | None,
    max_multiplier: float,
    allowed_tiers: list[str],
    task_class: str,
) -> dict[str, Any]:
    block_reason = (
        f"No model found for task class '{task_class}' within allowed tiers "
        f"{allowed_tiers or ['(none)']} and max multiplier {max_multiplier}. "
        "Route is blocked. Check registry or policy configuration."
    )
    return {
        "decisionId": _new_decision_id(),
        "createdAt": _now_iso(),
        "task": task,
        "taskClass": task_class,
        "routeType": classification.get("routeType", "SPECIALIST_AGENT"),
        "workflowId": workflow_id,
        "specialistId": specialist_id,
        "modelTier": "blocked",
        "selectedModelId": None,
        "maxAllowedMultiplier": max_multiplier,
        "estimatedCostClass": "blocked",
        "approvalRequired": True,
        "approvalReason": "Route is blocked. Manual review required.",
        "riskLevel": classification.get("riskLevel", "medium"),
        "complexity": classification.get("complexity", "medium"),
        "blastRadius": classification.get("blastRadius", "medium"),
        "allowedTools": [],
        "requiredMemory": [],
        "requiredChecks": [],
        "reason": block_reason,
        "fallbackUsed": task_class == "unknown",
        "blocked": True,
        "blockReason": block_reason,
        "nextStep": "Route is blocked. Review policy and model registry configuration before proceeding.",
    }


def _build_next_step(
    route_type: str,
    workflow_id: str | None,
    specialist_id: str | None,
    workflow: dict[str, Any] | None,
    approval_required: bool,
) -> str:
    parts: list[str] = []
    if approval_required:
        parts.append("Obtain approval before proceeding.")
    if route_type == "WORKFLOW" and workflow_id:
        parts.append(f"Use workflow '{workflow_id}'.")
        if specialist_id:
            parts.append(f"Assign specialist '{specialist_id}'.")
    elif route_type == "SPECIALIST_AGENT" and specialist_id:
        parts.append(f"Assign specialist '{specialist_id}'.")
    elif route_type == "MANUAL_PLAN_FIRST":
        parts.append("Create a written plan before beginning implementation.")
        if specialist_id:
            parts.append(f"Proposed specialist: '{specialist_id}'.")
    if workflow and workflow.get("requiredChecks"):
        parts.append(f"Run checks: {', '.join(workflow['requiredChecks'])}.")
    return " ".join(parts) if parts else "Proceed with recommended route."


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_decision(decision: dict[str, Any]) -> dict[str, Any]:
    """Validate a route decision dict. Returns {valid, issues, warnings}."""
    issues: list[str] = []
    warnings: list[str] = []

    if not isinstance(decision, dict):
        return {"valid": False, "issues": ["decision must be an object"], "warnings": []}

    for field in REQUIRED_DECISION_FIELDS:
        if field not in decision:
            issues.append(f"Missing required field: {field}")
        elif decision[field] is None or decision[field] == "":
            issues.append(f"Required field '{field}' must not be null or empty")

    route_type = decision.get("routeType", "")
    if route_type and route_type not in ROUTE_TYPES:
        issues.append(f"routeType '{route_type}' is not valid (expected: {sorted(ROUTE_TYPES)})")

    risk = decision.get("riskLevel", "")
    if risk and risk not in RISK_LEVELS:
        issues.append(f"riskLevel '{risk}' is not valid (expected: low, medium, high)")

    model_tier = decision.get("modelTier", "")
    if model_tier and model_tier not in MODEL_TIERS:
        issues.append(f"modelTier '{model_tier}' is not valid (expected: {sorted(MODEL_TIERS)})")

    if decision.get("approvalRequired") is True and not decision.get("approvalReason"):
        warnings.append("approvalRequired is true but approvalReason is not set")

    if decision.get("blocked") is True and not decision.get("blockReason"):
        warnings.append("blocked is true but blockReason is not set")

    if decision.get("blocked") is False and decision.get("blockReason"):
        warnings.append("blockReason is set but blocked is false")

    return {"valid": len(issues) == 0, "issues": issues, "warnings": warnings}


# ---------------------------------------------------------------------------
# Explanation
# ---------------------------------------------------------------------------

def explain_decision(decision: dict[str, Any]) -> str:
    """Return a human-readable explanation of a route decision."""
    lines: list[str] = []

    decision_id = decision.get("decisionId", "N/A")
    task = decision.get("task", "")
    task_class = decision.get("taskClass", "unknown")
    route_type = decision.get("routeType", "")
    workflow_id = decision.get("workflowId")
    specialist_id = decision.get("specialistId", "")
    model_tier = decision.get("modelTier", "")
    model_id = decision.get("selectedModelId", "")
    approval_required = decision.get("approvalRequired", False)
    approval_reason = decision.get("approvalReason") or ""
    blocked = decision.get("blocked", False)
    block_reason = decision.get("blockReason") or ""
    reason = decision.get("reason") or ""
    next_step = decision.get("nextStep") or ""
    matched_signals = decision.get("matchedSignals") or []
    risk = decision.get("riskLevel", "")
    complexity = decision.get("complexity", "")
    blast_radius = decision.get("blastRadius", "")

    lines.append(f"Route Decision: {decision_id}")
    task_display = (task[:120] + "...") if len(task) > 120 else task
    lines.append(f"Task: {task_display}")
    lines.append("")

    lines.append("## Classification")
    lines.append(f"Task class: {task_class}")
    if matched_signals:
        lines.append(f"Matched signals: {', '.join(matched_signals)}")
    if risk:
        lines.append(f"Risk: {risk} | Complexity: {complexity} | Blast radius: {blast_radius}")

    lines.append("")
    lines.append("## Route Type")
    if route_type == "WORKFLOW":
        lines.append("Route type: WORKFLOW — matched to a known, repeatable workflow.")
        if workflow_id:
            lines.append(f"Workflow: {workflow_id}")
    elif route_type == "SPECIALIST_AGENT":
        lines.append("Route type: SPECIALIST_AGENT — requires specialist judgment.")
    elif route_type == "MANUAL_PLAN_FIRST":
        lines.append("Route type: MANUAL_PLAN_FIRST — high-complexity or high-risk; plan before implementing.")
    else:
        lines.append(f"Route type: {route_type}")

    lines.append("")
    lines.append("## Specialist")
    lines.append(f"Specialist: {specialist_id or '(none)'}")

    lines.append("")
    lines.append("## Model / Cost Tier")
    if blocked:
        lines.append("Model tier: BLOCKED")
        if block_reason:
            lines.append(f"Block reason: {block_reason}")
    else:
        lines.append(f"Model tier: {model_tier}")
        if model_id:
            lines.append(f"Selected model: {model_id}")

    lines.append("")
    lines.append("## Approval")
    if approval_required:
        lines.append("Approval required: YES")
        if approval_reason:
            lines.append(f"Reason: {approval_reason}")
    else:
        lines.append("Approval required: no")

    if next_step:
        lines.append("")
        lines.append("## Next Action")
        lines.append(next_step)

    if reason:
        lines.append("")
        lines.append("## Routing Rationale")
        lines.append(reason)

    return "\n".join(lines)
