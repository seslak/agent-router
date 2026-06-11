"""Registry loading and validation for Agent Router."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pricing import PricingError, estimate_credits

_CACHE: dict[str, Any] | None = None
_CACHE_DIR: Path | None = None
_CACHE_MTIMES: tuple[tuple[float, int] | None, ...] | None = None

_FILE_MAP = {
    "specialists": ("specialists.json", "specialists"),
    "workflows": ("workflows.json", "workflows"),
    "models": ("models.copilot.json", "models"),
    "task_classes": ("task-classes.json", "classes"),
    "policies": ("policies.json", None),
}


def _default_routing_dir() -> Path:
    return Path(__file__).resolve().parent / "routing"


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _mtimes(target: Path) -> tuple[tuple[float, int] | None, ...]:
    values = []
    for filename, _key in _FILE_MAP.values():
        path = target / filename
        try:
            stat = path.stat()
            values.append((stat.st_mtime, int(stat.st_size)))
        except OSError:
            values.append(None)
    return tuple(values)


def get_registries(routing_dir: Path | None = None, force_reload: bool = False) -> dict[str, Any]:
    global _CACHE, _CACHE_DIR, _CACHE_MTIMES
    target = (routing_dir or _default_routing_dir()).resolve()
    mtimes = _mtimes(target)
    if not force_reload and _CACHE is not None and _CACHE_DIR == target and _CACHE_MTIMES == mtimes:
        return _CACHE

    data: dict[str, Any] = {
        "specialists": [],
        "workflows": [],
        "models": [],
        "task_classes": [],
        "policies": {},
        "_status": {},
        "_meta": {"routing_dir": str(target), "mtimes": mtimes},
    }
    status: dict[str, bool] = {}
    errors: list[str] = []

    for key, (filename, list_key) in _FILE_MAP.items():
        path = target / filename
        try:
            raw = _load_json(path)
            if list_key is None:
                data[key] = raw
            else:
                data[key] = raw.get(list_key, []) if isinstance(raw, dict) else []
                if key == "models" and isinstance(raw, dict):
                    data["_models_payload"] = raw
                    data["_models_as_of"] = str(raw.get("asOf", ""))
            status[key] = True
        except Exception as exc:
            status[key] = False
            errors.append("{0}: {1}".format(filename, exc))

    data["_status"] = status
    data["_load_errors"] = errors
    _CACHE = data
    _CACHE_DIR = target
    _CACHE_MTIMES = mtimes
    return data


def invalidate_cache() -> None:
    global _CACHE, _CACHE_DIR, _CACHE_MTIMES
    _CACHE = None
    _CACHE_DIR = None
    _CACHE_MTIMES = None


def _has_schema_v2_models(models_payload: dict[str, Any]) -> bool:
    return int(models_payload.get("schemaVersion", 0) or 0) == 2


def _has_schema_v2_policies(policies_payload: dict[str, Any]) -> bool:
    return int(policies_payload.get("schemaVersion", 0) or 0) == 2


def _registry_root_payload(target: Path, filename: str) -> dict[str, Any]:
    path = target / filename
    raw = _load_json(path)
    return raw if isinstance(raw, dict) else {}


def _staleness_warning(models_payload: dict[str, Any]) -> str | None:
    as_of = str(models_payload.get("asOf", "")).strip()
    if not as_of:
        return "models.copilot.json is missing asOf."
    try:
        dt = datetime.strptime(as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return "models.copilot.json asOf is not parseable."
    age_days = (datetime.now(timezone.utc) - dt).days
    if age_days > 180:
        return "models.copilot.json asOf is older than 180 days."
    return None


def validate_registries(routing_dir: Path | None = None) -> dict[str, Any]:
    target = (routing_dir or _default_routing_dir()).resolve()
    regs = get_registries(target)
    errors = list(regs.get("_load_errors", []))
    warnings: list[str] = []

    try:
        models_payload = _registry_root_payload(target, "models.copilot.json")
    except Exception as exc:
        models_payload = {}
        errors.append("models.copilot.json: {0}".format(exc))
    try:
        policies_payload = _registry_root_payload(target, "policies.json")
    except Exception as exc:
        policies_payload = {}
        errors.append("policies.json: {0}".format(exc))

    if models_payload and not _has_schema_v2_models(models_payload):
        errors.append("models.copilot.json registry uses retired multiplier schema; see CHANGELOG 0.3.0")
    if policies_payload and not _has_schema_v2_policies(policies_payload):
        errors.append("policies.json registry uses retired multiplier schema; see CHANGELOG 0.3.0")

    specialists = list(regs.get("specialists", []))
    workflows = list(regs.get("workflows", []))
    task_classes = list(regs.get("task_classes", []))
    models = list(regs.get("models", []))
    policies = dict(regs.get("policies", {}))

    specialist_ids = {str(item.get("id", "")) for item in specialists if str(item.get("id", "")).strip()}
    task_class_ids = {str(item.get("id", "")) for item in task_classes if str(item.get("id", "")).strip()}
    valid_tiers = {"free", "cheap", "balanced", "expensive"}
    cost_profiles = policies.get("costProfiles", {}) if isinstance(policies, dict) else {}

    for workflow in workflows:
        workflow_id = str(workflow.get("id", "")).strip() or "(unknown-workflow)"
        specialist_id = str(workflow.get("specialistId") or workflow.get("defaultSpecialist") or "").strip()
        if specialist_id and specialist_id not in specialist_ids:
            errors.append("workflow {0} references unknown specialistId {1}".format(workflow_id, specialist_id))
        for task_class in workflow.get("taskClasses", []) or []:
            if str(task_class) not in task_class_ids:
                errors.append("workflow {0} references unknown taskClass {1}".format(workflow_id, task_class))

    task_policies = policies.get("taskPolicies", {}) if isinstance(policies, dict) else {}
    for task_class_id, policy in task_policies.items():
        preferred_specialist = str((policy or {}).get("preferredSpecialist") or "").strip()
        if preferred_specialist and preferred_specialist not in specialist_ids:
            errors.append("task policy {0} references unknown preferredSpecialist {1}".format(task_class_id, preferred_specialist))
        cost_profile = str((policy or {}).get("costProfile") or "").strip()
        if cost_profile and cost_profile not in cost_profiles:
            errors.append("task policy {0} references unknown costProfile {1}".format(task_class_id, cost_profile))

    fallback_policy = policies.get("fallbackPolicy", {}) if isinstance(policies, dict) else {}
    missing_specialist = str(fallback_policy.get("onMissingSpecialist") or "").strip()
    if missing_specialist and missing_specialist not in specialist_ids:
        errors.append("fallbackPolicy.onMissingSpecialist references unknown specialistId {0}".format(missing_specialist))

    for specialist in specialists:
        specialist_id = str(specialist.get("id", "")).strip() or "(unknown-specialist)"
        for tier in specialist.get("allowedTiers", []) or []:
            if str(tier) not in valid_tiers:
                errors.append("specialist {0} has invalid allowedTier {1}".format(specialist_id, tier))

    medium_profile = cost_profiles.get("medium", {"inputTokens": 30000, "cachedInputTokens": 15000, "outputTokens": 5000})
    for model in models:
        model_id = str(model.get("id", "")).strip() or "(unknown-model)"
        try:
            estimate_credits(model, medium_profile, estimated_input_tokens=None)
        except PricingError as exc:
            errors.append("model {0} has invalid pricing: {1}".format(model_id, exc))

    stale = _staleness_warning(models_payload)
    if stale:
        warnings.append(stale)

    return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}
