"""Core routing logic for Agent Router."""

from __future__ import annotations

import math
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from pricing import PricingError, derive_tier, estimate_credits
from schemas import (
    KNOWN_APPROVAL_CONDITIONS,
    MODEL_TIERS,
    PRICING_APPLIED,
    REQUIRED_DECISION_FIELDS,
    RISK_LEVELS,
    ROUTE_TYPES,
    TIER_ORDER,
)

_WORD_RE = re.compile(r"[a-z0-9]+")
_EXT_TOKEN_CAP = 10000000
_OUTPUT_TOKEN_CAP = 1000000


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _new_decision_id() -> str:
    return "rd_" + uuid.uuid4().hex[:12]


def _normalize_text(text: str) -> str:
    return str(text or "").lower()


def _find_by_id(items: list[dict[str, Any]], item_id: str) -> dict[str, Any] | None:
    needle = str(item_id or "").strip()
    if not needle:
        return None
    for item in items:
        if str(item.get("id", "")).strip() == needle:
            return item
    return None


def _tier_rank(tier: str) -> int:
    return TIER_ORDER.get(str(tier or ""), 99)


def _match_phrase(task_lower: str, keyword: str) -> bool:
    pattern = r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"(?![a-z0-9])"
    return re.search(pattern, task_lower) is not None


def _score_task_class(task_lower: str, task_class: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    signals: list[str] = []
    for kw in task_class.get("keywords", []) or []:
        kw_lower = str(kw or "").strip().lower()
        if not kw_lower:
            continue
        if _match_phrase(task_lower, kw_lower):
            kw_words = _WORD_RE.findall(kw_lower)
            score += max(1, len(kw_words))
            signals.append(str(kw))
    return score, signals


def _find_priority_class(task_lower: str, task_classes: list[dict[str, Any]]) -> dict[str, Any] | None:
    for task_class in task_classes:
        phrases = task_class.get("priorityPhrases", []) or []
        for phrase in phrases:
            phrase_text = str(phrase or "").strip().lower()
            if phrase_text and _match_phrase(task_lower, phrase_text):
                return task_class
    return None


def _classification_from_class(
    best_class: dict[str, Any],
    best_score: int,
    best_signals: list[str],
    forced: bool = False,
) -> dict[str, Any]:
    prefix = "Forced high-priority match" if forced else "Matched task class"
    return {
        "taskClass": str(best_class.get("id", "unknown")),
        "routeType": str(best_class.get("routeType", "SPECIALIST_AGENT")),
        "riskLevel": str(best_class.get("defaultRisk", "medium")),
        "complexity": str(best_class.get("defaultComplexity", "medium")),
        "blastRadius": str(best_class.get("defaultBlastRadius", "medium")),
        "reason": (
            "{0} '{1}' (score {2}, signals: {3}).".format(
                prefix,
                str(best_class.get("id", "unknown")),
                int(best_score),
                ", ".join(best_signals) or "none",
            )
        ),
        "matchedSignals": list(best_signals),
        "suggestedHandler": str(best_class.get("preferredSpecialist", "architect")),
        "_preferredWorkflow": best_class.get("preferredWorkflow"),
        "_preferredSpecialist": best_class.get("preferredSpecialist"),
    }


def _fallback_classification(reason: str = "") -> dict[str, Any]:
    return {
        "taskClass": "unknown",
        "routeType": "SPECIALIST_AGENT",
        "riskLevel": "medium",
        "complexity": "medium",
        "blastRadius": "medium",
        "reason": "Fallback classification used ({0}).".format(reason),
        "matchedSignals": [],
        "suggestedHandler": "architect",
        "_preferredWorkflow": None,
        "_preferredSpecialist": "architect",
    }


def classify_task(task_text: str, task_classes: list[dict[str, Any]]) -> dict[str, Any]:
    if not task_text:
        return _fallback_classification("empty task text")
    task_lower = _normalize_text(task_text)
    forced = _find_priority_class(task_lower, task_classes)
    if forced is not None:
        score, signals = _score_task_class(task_lower, forced)
        return _classification_from_class(forced, score, signals, forced=True)

    best_class: dict[str, Any] | None = None
    best_score = 0
    best_signals: list[str] = []
    for task_class in task_classes:
        if str(task_class.get("id", "")) == "unknown":
            continue
        score, signals = _score_task_class(task_lower, task_class)
        if score > best_score:
            best_class = task_class
            best_score = score
            best_signals = signals
    if best_class is None or best_score == 0:
        unknown = _find_by_id(task_classes, "unknown")
        if unknown is None:
            return _fallback_classification("no matching task class")
        return _classification_from_class(unknown, 0, [])
    return _classification_from_class(best_class, best_score, best_signals)


def _optional_nonnegative_int(value: Any, field_name: str, cap: int) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError("{0} must be an integer.".format(field_name))
    if parsed < 0:
        raise ValueError("{0} must be >= 0.".format(field_name))
    return min(parsed, cap)


def _resolve_specialist(
    classification: dict[str, Any],
    task_policy: dict[str, Any],
    workflow: dict[str, Any] | None,
    specialists: list[dict[str, Any]],
    fallback_policy: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    specialist_id = ""
    if workflow is not None:
        specialist_id = str(workflow.get("specialistId") or workflow.get("defaultSpecialist") or "").strip()
    if not specialist_id:
        specialist_id = str(
            classification.get("_preferredSpecialist")
            or task_policy.get("preferredSpecialist")
            or fallback_policy.get("onMissingSpecialist", "architect")
            or "architect"
        ).strip()
    specialist = _find_by_id(specialists, specialist_id)
    if specialist is None and specialist_id:
        fallback_id = str(fallback_policy.get("onMissingSpecialist", "architect") or "architect").strip()
        specialist = _find_by_id(specialists, fallback_id)
        specialist_id = fallback_id if specialist is not None else specialist_id
    return specialist_id or None, specialist


def _allowed_tiers(task_policy: dict[str, Any], specialist: dict[str, Any] | None, workflow: dict[str, Any] | None) -> list[str]:
    policy_max_tier = str(task_policy.get("maxTier", "expensive") or "expensive")
    policy_min_tier = str(task_policy.get("minTier", "") or "").strip() or None
    specialist_tiers = list((specialist or {}).get("allowedTiers", ["cheap"]))
    tiers = [
        str(tier)
        for tier in specialist_tiers
        if _tier_rank(str(tier)) <= _tier_rank(policy_max_tier)
        and (policy_min_tier is None or _tier_rank(str(tier)) >= _tier_rank(policy_min_tier))
    ]
    workflow_model_tier = str((workflow or {}).get("modelTier", "") or "").strip()
    if workflow_model_tier:
        tiers = [tier for tier in tiers if _tier_rank(tier) <= _tier_rank(workflow_model_tier)]
    return tiers


def _compact_ranked_model(model: dict[str, Any], approval_rules: list[dict[str, Any]], default_policy: dict[str, Any], specialist: dict[str, Any] | None, threshold: int | None = None) -> dict[str, Any]:
    approval = _entry_approval(model, approval_rules, default_policy, specialist, threshold)
    return {
        "rank": int(model["_rank"]),
        "modelId": str(model.get("id", "")),
        "displayName": str(model.get("displayName", "")),
        "tier": str(model["_tier"]),
        "estimatedCredits": float(model["_credits"]),
        "pricingApplied": str(model["_pricing_applied"]),
        "category": str(model.get("category", "")),
        "releaseStatus": str(model.get("releaseStatus", "")),
        "approvalRequired": bool(approval["approvalRequired"]),
        "approvalConditions": list(approval["approvalConditions"]),
    }


def _entry_approval(
    model: dict[str, Any],
    approval_rules: list[dict[str, Any]],
    default_policy: dict[str, Any],
    specialist: dict[str, Any] | None,
    threshold_input_tokens: int | None,
) -> dict[str, Any]:
    conditions: list[str] = []
    first_reason = None
    specialist_policy = str((specialist or {}).get("approvalPolicy", "none"))
    effective_tier = str(model.get("_tier", model.get("tier", "")))
    for rule in approval_rules:
        condition = str(rule.get("condition", "")).strip()
        if condition == "tier_expensive":
            matched = effective_tier == "expensive" and bool(default_policy.get("requireApprovalForExpensive", True))
        elif condition == "model_requires_approval":
            matched = bool(model.get("requiresApproval", False))
        elif condition == "specialist_approval_always":
            if specialist_policy == "always":
                matched = True
            elif specialist_policy == "if_expensive":
                matched = effective_tier == "expensive"
            else:
                matched = False
        elif condition == "long_context_pricing":
            matched = str(model.get("_pricing_applied", "default")) == "long_context"
        else:
            matched = False
        if matched:
            conditions.append(condition)
            if first_reason is None:
                first_reason = str(rule.get("reason", "")).strip() or None
    return {
        "approvalRequired": len(conditions) > 0,
        "approvalConditions": conditions,
        "approvalReason": first_reason,
        "thresholdInputTokens": threshold_input_tokens,
    }


def _cost_profile_name(policies: dict[str, Any], task_policy: dict[str, Any]) -> str:
    name = str(task_policy.get("costProfile", "medium") or "medium")
    if name in (policies.get("costProfiles", {}) or {}):
        return name
    return "medium"


def _cost_profile(policies: dict[str, Any], name: str, estimated_output_tokens: int | None) -> dict[str, Any]:
    profiles = policies.get("costProfiles", {}) or {}
    selected = dict(profiles.get(name, profiles.get("medium", {"inputTokens": 30000, "cachedInputTokens": 15000, "outputTokens": 5000})))
    if estimated_output_tokens is not None:
        selected["outputTokens"] = estimated_output_tokens
    return selected


def _resolve_workflow(classification: dict[str, Any], workflows: list[dict[str, Any]]) -> tuple[str | None, dict[str, Any] | None]:
    if str(classification.get("routeType", "")) != "WORKFLOW":
        return None, None
    preferred = str(classification.get("_preferredWorkflow") or "").strip()
    if not preferred:
        return None, None
    workflow = _find_by_id(workflows, preferred)
    return (preferred, workflow) if workflow is not None else (None, None)


def _apply_candidate_filter(
    models: list[dict[str, Any]],
    candidate_models: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    if not candidate_models:
        return list(models), []
    ids = {item.get("id"): item for item in models}
    filtered = []
    unknown = []
    for model_id in candidate_models:
        if model_id in ids:
            filtered.append(ids[model_id])
        else:
            unknown.append(model_id)
    return filtered, unknown


def _governor_start_hint(task_text: str, workflow: dict[str, Any] | None) -> dict[str, Any] | None:
    if workflow is None:
        return None
    profile = str(workflow.get("profile", "")).strip()
    if not profile:
        return None
    task = str(task_text or "").strip()
    if len(task) > 240:
        task = task[:240]
    return {"action": "start_run", "params": {"task": task, "profile": profile}}


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
        parts.append("Use workflow '{0}'.".format(workflow_id))
        if specialist_id:
            parts.append("Assign specialist '{0}'.".format(specialist_id))
    elif route_type == "SPECIALIST_AGENT" and specialist_id:
        parts.append("Assign specialist '{0}'.".format(specialist_id))
    elif route_type == "MANUAL_PLAN_FIRST":
        parts.append("Create a written plan before beginning implementation.")
        if specialist_id:
            parts.append("Proposed specialist: '{0}'.".format(specialist_id))
    if workflow and workflow.get("requiredChecks"):
        parts.append("Run checks: {0}.".format(", ".join(str(item) for item in workflow.get("requiredChecks", []))))
    return " ".join(parts) if parts else "Proceed with recommended route."


def _blocked_decision(
    *,
    task: str,
    classification: dict[str, Any],
    specialist_id: str | None,
    workflow_id: str | None,
    workflow: dict[str, Any] | None,
    max_credits: float,
    allowed_tiers: list[str],
    cost_profile_name: str,
    estimated_input_tokens: int | None,
    registry_as_of: str,
    skipped: dict[str, int],
    warnings: list[str],
    block_reason: str,
    ideal_model_id: str | None = None,
    candidate_constraint: dict[str, Any] | None = None,
    unknown_candidate_models: list[str] | None = None,
) -> dict[str, Any]:
    route_type = str(classification.get("routeType", "SPECIALIST_AGENT"))
    matched_signals = list(classification.get("matchedSignals", []))
    return {
        "decisionId": _new_decision_id(),
        "createdAt": _now_iso(),
        "sessionId": os.environ.get("AGENT_SUITE_SESSION_ID", "").strip() or None,
        "task": task,
        "taskClass": str(classification.get("taskClass", "unknown")),
        "routeType": route_type,
        "workflowId": workflow_id,
        "specialistId": specialist_id,
        "modelTier": "blocked",
        "selectedModelId": None,
        "modelCategory": "",
        "maxAllowedCredits": float(max_credits),
        "maxAllowedMultiplier": None,
        "estimatedCredits": None,
        "estimatedCostClass": "blocked",
        "costProfile": cost_profile_name,
        "pricingApplied": "blocked",
        "estimatedInputTokens": estimated_input_tokens,
        "registryAsOf": registry_as_of,
        "approvalRequired": True,
        "approvalReason": "Route is blocked. Manual review required.",
        "approvalConditions": [],
        "riskLevel": str(classification.get("riskLevel", "medium")),
        "complexity": str(classification.get("complexity", "medium")),
        "blastRadius": str(classification.get("blastRadius", "medium")),
        "allowedTools": list((workflow or {}).get("allowedTools", [])),
        "requiredMemory": [],
        "requiredChecks": list((workflow or {}).get("requiredChecks", [])),
        "reason": block_reason,
        "matchedSignals": matched_signals,
        "fallbackUsed": str(classification.get("taskClass", "unknown")) == "unknown",
        "blocked": True,
        "blockReason": block_reason,
        "nextStep": "Route is blocked. Review policy and model registry configuration before proceeding.",
        "rankedModels": [],
        "skipped": skipped,
        "warnings": warnings,
        "unknownCandidateModels": list(unknown_candidate_models or []),
        "candidateConstraint": candidate_constraint or {"applied": False},
        "governorStartHint": _governor_start_hint(task, workflow),
        "idealModelId": ideal_model_id,
    }


def route_task(task_text: str, registries: dict[str, Any], params: dict[str, Any] | None = None) -> dict[str, Any]:
    params = params or {}
    task_classes = list(registries.get("task_classes", []))
    workflows = list(registries.get("workflows", []))
    specialists = list(registries.get("specialists", []))
    all_models = list(registries.get("models", []))
    policies = dict(registries.get("policies", {}))
    default_policy = dict(policies.get("defaultPolicy", {}))
    task_policies = dict(policies.get("taskPolicies", {}))
    approval_rules = list(policies.get("approvalRules", []))
    tier_bands = dict(policies.get("tierBands", {"cheapMaxCredits": 5, "balancedMaxCredits": 25}))
    fallback_policy = dict(policies.get("fallbackPolicy", {}))
    registry_as_of = str(registries.get("_models_as_of") or policies.get("asOf") or registries.get("models_as_of") or "")
    if not registry_as_of and isinstance(registries.get("_models_payload"), dict):
        registry_as_of = str(registries["_models_payload"].get("asOf", ""))

    estimated_input_tokens = _optional_nonnegative_int(params.get("estimated_input_tokens"), "estimated_input_tokens", _EXT_TOKEN_CAP)
    estimated_output_tokens = _optional_nonnegative_int(params.get("estimated_output_tokens"), "estimated_output_tokens", _OUTPUT_TOKEN_CAP)

    candidate_models_raw = params.get("candidate_models")
    candidate_models: list[str] = []
    if isinstance(candidate_models_raw, list):
        for item in candidate_models_raw[:50]:
            if isinstance(item, str):
                value = item.strip().lower()
                if value:
                    candidate_models.append(value)

    classification = classify_task(task_text, task_classes)
    task_class = str(classification.get("taskClass", "unknown"))
    if task_class == "unknown" and str(fallback_policy.get("onUnknownTaskClass", "use_unknown_class")) != "use_unknown_class":
        classification = _fallback_classification("unknown-task-class disabled by policy")
        task_class = str(classification.get("taskClass", "unknown"))
    workflow_id, workflow = _resolve_workflow(classification, workflows)
    task_policy = dict(task_policies.get(task_class, {}))
    specialist_id, specialist = _resolve_specialist(classification, task_policy, workflow, specialists, fallback_policy)

    cost_profile_name = _cost_profile_name(policies, task_policy)
    cost_profile = _cost_profile(policies, cost_profile_name, estimated_output_tokens)

    policy_max = float(task_policy.get("maxCredits", default_policy.get("maxDefaultCredits", 10)) or default_policy.get("maxDefaultCredits", 10) or 10)
    specialist_max = float((specialist or {}).get("maxCredits", math.inf))
    workflow_max = float((workflow or {}).get("maxCredits", math.inf))
    max_credits = min(policy_max, specialist_max, workflow_max)
    allowed_tiers = _allowed_tiers(task_policy, specialist, workflow)

    preview_allowed = bool(default_policy.get("allowPreviewModels", False))
    unknown_pricing_behavior = str(default_policy.get("unknownPricingBehavior", "block") or "block")
    skipped = {"preview_skipped": 0, "unknown_pricing_skipped": 0, "over_budget_skipped": 0, "tier_filtered": 0}
    warnings: list[str] = []

    def build_candidates(model_pool: list[dict[str, Any]], count_skips: bool) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for model in model_pool:
            release_status = str(model.get("releaseStatus", "ga"))
            if release_status == "public_preview" and not preview_allowed:
                if count_skips:
                    skipped["preview_skipped"] += 1
                continue
            try:
                estimate = estimate_credits(model, cost_profile, estimated_input_tokens=estimated_input_tokens)
            except PricingError:
                if unknown_pricing_behavior == "block":
                    if count_skips:
                        skipped["unknown_pricing_skipped"] += 1
                    continue
                estimate = {
                    "credits": float(tier_bands.get("balancedMaxCredits", 25)) + 1.0,
                    "pricingApplied": "default",
                    "thresholdInputTokens": None,
                }
            tier = derive_tier(float(estimate["credits"]), tier_bands)
            if tier not in allowed_tiers:
                if count_skips:
                    skipped["tier_filtered"] += 1
                continue
            if float(estimate["credits"]) > max_credits:
                if count_skips:
                    skipped["over_budget_skipped"] += 1
                continue
            candidate = dict(model)
            candidate["_credits"] = float(estimate["credits"])
            candidate["_tier"] = tier
            candidate["_pricing_applied"] = str(estimate["pricingApplied"])
            candidate["_threshold_input_tokens"] = estimate.get("thresholdInputTokens")
            candidates.append(candidate)
        candidates.sort(key=lambda item: (_tier_rank(str(item["_tier"])), float(item["_credits"]), str(item.get("id", ""))))
        for index, item in enumerate(candidates, start=1):
            item["_rank"] = index
        return candidates

    unconstrained_candidates = build_candidates(all_models, count_skips=False)
    filtered_models, unknown_candidate_models = _apply_candidate_filter(all_models, candidate_models)
    constrained_candidates = build_candidates(filtered_models, count_skips=True) if candidate_models else build_candidates(all_models, count_skips=True)

    candidate_constraint = {"applied": bool(candidate_models)}
    if candidate_models:
        ideal_model_id = str(unconstrained_candidates[0].get("id", "")) if unconstrained_candidates else None
        ideal_in_candidates = any(str(item.get("id", "")) == ideal_model_id for item in constrained_candidates) if ideal_model_id else False
        candidate_constraint = {
            "applied": True,
            "idealModelId": ideal_model_id,
            "idealModelInCandidates": ideal_in_candidates,
        }
        if unknown_candidate_models:
            warnings.append("Unknown candidate model ids were ignored.")
        if not constrained_candidates and unconstrained_candidates:
            block_reason = "Candidate model constraint blocked all viable routes. Ideal available model is {0}.".format(ideal_model_id)
            return _blocked_decision(
                task=task_text,
                classification=classification,
                specialist_id=specialist_id,
                workflow_id=workflow_id,
                workflow=workflow,
                max_credits=max_credits,
                allowed_tiers=allowed_tiers,
                cost_profile_name=cost_profile_name,
                estimated_input_tokens=estimated_input_tokens,
                registry_as_of=registry_as_of,
                skipped=skipped,
                warnings=warnings,
                block_reason=block_reason,
                ideal_model_id=ideal_model_id,
                candidate_constraint=candidate_constraint,
                unknown_candidate_models=unknown_candidate_models,
            )

    candidates = constrained_candidates
    if not candidates:
        block_reason = (
            "No model found for task class '{0}' within allowed tiers {1} and max credits {2}."
            .format(task_class, allowed_tiers or ["(none)"], max_credits)
        )
        return _blocked_decision(
            task=task_text,
            classification=classification,
            specialist_id=specialist_id,
            workflow_id=workflow_id,
            workflow=workflow,
            max_credits=max_credits,
            allowed_tiers=allowed_tiers,
            cost_profile_name=cost_profile_name,
            estimated_input_tokens=estimated_input_tokens,
            registry_as_of=registry_as_of,
            skipped=skipped,
            warnings=warnings,
            block_reason=block_reason,
            ideal_model_id=str(unconstrained_candidates[0].get("id", "")) if unconstrained_candidates else None,
            candidate_constraint=candidate_constraint,
            unknown_candidate_models=unknown_candidate_models,
        )

    ranked_models_raw = candidates[:20]
    ranked_models = []
    for item in ranked_models_raw:
        ranked_models.append(_compact_ranked_model(item, approval_rules, default_policy, specialist, item.get("_threshold_input_tokens")))

    selected = ranked_models_raw[0]
    selected_approval = _entry_approval(selected, approval_rules, default_policy, specialist, selected.get("_threshold_input_tokens"))

    route_type = str(classification.get("routeType", "SPECIALIST_AGENT"))
    matched_signals = list(classification.get("matchedSignals", []))
    selected_model_id = str(selected.get("id", ""))
    model_tier = str(selected["_tier"])
    reason = (
        "Task classified as '{0}' (signals: {1}). Route: {2}. Specialist: {3}. Model: {4} ({5}, {6} credits)."
        .format(
            task_class,
            ", ".join(matched_signals) if matched_signals else "none",
            route_type,
            specialist_id,
            selected_model_id,
            model_tier,
            selected["_credits"],
        )
    )

    decision = {
        "decisionId": _new_decision_id(),
        "createdAt": _now_iso(),
        "sessionId": os.environ.get("AGENT_SUITE_SESSION_ID", "").strip() or None,
        "task": task_text,
        "taskClass": task_class,
        "routeType": route_type,
        "workflowId": workflow_id,
        "specialistId": specialist_id,
        "modelTier": model_tier,
        "selectedModelId": selected_model_id,
        "modelCategory": str(selected.get("category", "")),
        "maxAllowedCredits": float(max_credits),
        "maxAllowedMultiplier": None,
        "estimatedCredits": float(selected["_credits"]),
        "estimatedCostClass": model_tier,
        "costProfile": cost_profile_name,
        "pricingApplied": str(selected["_pricing_applied"]),
        "estimatedInputTokens": estimated_input_tokens,
        "registryAsOf": registry_as_of,
        "approvalRequired": bool(selected_approval["approvalRequired"]),
        "approvalReason": selected_approval["approvalReason"],
        "approvalConditions": list(selected_approval["approvalConditions"]),
        "riskLevel": str(classification.get("riskLevel", "medium")),
        "complexity": str(classification.get("complexity", "medium")),
        "blastRadius": str(classification.get("blastRadius", "medium")),
        "allowedTools": list((workflow or specialist or {}).get("allowedTools", [])),
        "requiredMemory": ["recall_startup"] if str((specialist or {}).get("contextPolicy", "minimal")) in {"bounded", "full"} else [],
        "requiredChecks": list((workflow or {}).get("requiredChecks", [])),
        "reason": reason,
        "matchedSignals": matched_signals,
        "fallbackUsed": task_class == "unknown",
        "blocked": False,
        "blockReason": None,
        "nextStep": _build_next_step(route_type, workflow_id, specialist_id, workflow, bool(selected_approval["approvalRequired"])),
        "rankedModels": ranked_models,
        "skipped": skipped,
        "warnings": warnings,
        "unknownCandidateModels": unknown_candidate_models,
        "candidateConstraint": candidate_constraint,
        "governorStartHint": _governor_start_hint(task_text, workflow),
        "idealModelId": candidate_constraint.get("idealModelId") if candidate_constraint.get("applied") else None,
    }
    return decision


def validate_decision(decision: dict[str, Any]) -> dict[str, Any]:
    issues: list[str] = []
    warnings: list[str] = []
    if not isinstance(decision, dict):
        return {"valid": False, "issues": ["decision must be an object"], "warnings": []}
    for field in REQUIRED_DECISION_FIELDS:
        if field not in decision:
            issues.append("Missing required field: {0}".format(field))
        elif decision[field] in (None, ""):
            issues.append("Required field '{0}' must not be null or empty".format(field))

    route_type = str(decision.get("routeType", ""))
    if route_type and route_type not in ROUTE_TYPES:
        issues.append("routeType '{0}' is not valid".format(route_type))
    risk = str(decision.get("riskLevel", ""))
    if risk and risk not in RISK_LEVELS:
        issues.append("riskLevel '{0}' is not valid".format(risk))
    model_tier = str(decision.get("modelTier", ""))
    if model_tier and model_tier not in MODEL_TIERS:
        issues.append("modelTier '{0}' is not valid".format(model_tier))
    pricing_applied = str(decision.get("pricingApplied", ""))
    if pricing_applied and pricing_applied not in PRICING_APPLIED:
        issues.append("pricingApplied '{0}' is not valid".format(pricing_applied))
    for condition in decision.get("approvalConditions", []) or []:
        if str(condition) not in KNOWN_APPROVAL_CONDITIONS:
            warnings.append("Unknown approval condition: {0}".format(condition))
    ranked_models = decision.get("rankedModels")
    if not isinstance(ranked_models, list):
        issues.append("rankedModels must be a list")
    if decision.get("approvalRequired") is True and not decision.get("approvalReason"):
        warnings.append("approvalRequired is true but approvalReason is not set")
    if decision.get("blocked") is True and not decision.get("blockReason"):
        warnings.append("blocked is true but blockReason is not set")
    if decision.get("blocked") is False and decision.get("blockReason"):
        warnings.append("blockReason is set but blocked is false")
    return {"valid": len(issues) == 0, "issues": issues, "warnings": warnings}


def explain_decision(decision: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("Route Decision: {0}".format(decision.get("decisionId", "N/A")))
    task = str(decision.get("task", ""))
    lines.append("Task: {0}".format((task[:120] + "...") if len(task) > 120 else task))
    lines.append("")
    lines.append("## Classification")
    lines.append("Task class: {0}".format(decision.get("taskClass", "unknown")))
    matched = decision.get("matchedSignals", []) or []
    if matched:
        lines.append("Matched signals: {0}".format(", ".join(str(item) for item in matched)))
    lines.append(
        "Risk: {0} | Complexity: {1} | Blast radius: {2}".format(
            decision.get("riskLevel", ""),
            decision.get("complexity", ""),
            decision.get("blastRadius", ""),
        )
    )
    lines.append("")
    lines.append("## Route")
    lines.append("Route type: {0}".format(decision.get("routeType", "")))
    if decision.get("workflowId"):
        lines.append("Workflow: {0}".format(decision.get("workflowId")))
    lines.append("Specialist: {0}".format(decision.get("specialistId") or "(none)"))
    lines.append("")
    lines.append("## Model")
    lines.append("Selected model: {0}".format(decision.get("selectedModelId") or "(blocked)"))
    lines.append("Tier: {0}".format(decision.get("modelTier", "")))
    if decision.get("estimatedCredits") is not None:
        lines.append("Estimated credits: {0}".format(decision.get("estimatedCredits")))
    if decision.get("pricingApplied"):
        lines.append("Pricing applied: {0}".format(decision.get("pricingApplied")))
    lines.append("")
    lines.append("## Approval")
    lines.append("Approval required: {0}".format("YES" if decision.get("approvalRequired") else "no"))
    if decision.get("approvalReason"):
        lines.append("Reason: {0}".format(decision.get("approvalReason")))
    if decision.get("approvalConditions"):
        lines.append("Conditions: {0}".format(", ".join(str(item) for item in decision.get("approvalConditions", []))))
    if decision.get("blocked"):
        lines.append("BLOCKED: {0}".format(decision.get("blockReason", "")))
    if decision.get("rankedModels"):
        lines.append("")
        lines.append("## Ranked Models")
        for item in decision.get("rankedModels", [])[:5]:
            lines.append(
                "- #{0} {1} ({2}, {3} credits)".format(
                    item.get("rank"),
                    item.get("modelId"),
                    item.get("tier"),
                    item.get("estimatedCredits"),
                )
            )
    if decision.get("nextStep"):
        lines.append("")
        lines.append("## Next Action")
        lines.append(str(decision.get("nextStep")))
    if decision.get("reason"):
        lines.append("")
        lines.append("## Routing Rationale")
        lines.append(str(decision.get("reason")))
    return "\n".join(lines)
