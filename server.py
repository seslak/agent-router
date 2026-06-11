#!/usr/bin/env python3
"""Agent Router MCP gateway."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pricing import PricingError, estimate_credits
from registries import get_registries, invalidate_cache, validate_registries
from router_core import classify_task, explain_decision as _explain_decision, route_task, validate_decision as _validate_decision

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {"2025-06-18", "2025-03-26", "2024-11-05"}
SERVER_NAME = "router"
SERVER_TITLE = "Agent Router Gateway"
SERVER_VERSION = "0.5.2"
GATEWAY_TOOL_NAME = "router"
LEGACY_ROUTE_DEPRECATION = (
    "router.route(raw text) is legacy and planned for removal before 1.0. "
    "Use router.suggest_workflow(task, estimated_input_tokens?)."
)
LOG_ROTATE_BYTES = 10 * 1024 * 1024

PACKAGE_ROOT = Path(__file__).resolve().parent
ROUTING_DIR = PACKAGE_ROOT / "routing"
_SHOULD_EXIT = False


def _state_dir() -> Path:
    env = os.environ.get("AGENT_ROUTER_STATE_DIR", "").strip()
    if env:
        return Path(env)
    workspace = os.environ.get("AGENT_ROUTER_WORKSPACE_ROOT", "").strip()
    if workspace:
        return Path(workspace) / "state" / "router"
    return Path.cwd() / "state" / "router"


def _workspace_root() -> Path:
    env = os.environ.get("AGENT_ROUTER_WORKSPACE_ROOT", "").strip()
    return Path(env) if env else Path.cwd()


def _decision_log_path() -> Path:
    return _state_dir() / "route_decisions.jsonl"


def _rotated_log_path() -> Path:
    return _state_dir() / "route_decisions.1.jsonl"


def _session_id() -> str | None:
    value = os.environ.get("AGENT_SUITE_SESSION_ID", "").strip()
    return value or None


def _text_result(text: str, structured: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": False}
    if structured is not None:
        result["structuredContent"] = structured
    return result


def _error_result(error: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    structured: dict[str, Any] = {"error": error, "message": message}
    if details:
        structured.update(details)
    return {"content": [{"type": "text", "text": "Error: {0}".format(message)}], "isError": True, "structuredContent": structured}


def _tool_error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "Error: {0}".format(message)}], "isError": True}


def _workflow_compact(workflow: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": workflow.get("id"),
        "aliases": list(workflow.get("aliases", [])),
        "description": workflow.get("description"),
        "profile": workflow.get("profile"),
        "specialistId": workflow.get("specialistId") or workflow.get("defaultSpecialist"),
        "modelTier": workflow.get("modelTier"),
        "maxCredits": workflow.get("maxCredits"),
        "allowedTools": list(workflow.get("allowedTools", [])),
        "requiresEdit": bool(workflow.get("requiresEdit", False)),
        "requiresExecute": bool(workflow.get("requiresExecute", False)),
        "maxTargetFiles": workflow.get("maxTargetFiles"),
        "riskDefault": workflow.get("riskDefault") or workflow.get("riskLevel"),
        "requiredChecks": list(workflow.get("requiredChecks", [])),
        "paramSchema": workflow.get("paramSchema", {}),
    }


def _resolve_workflow(workflows: list[dict[str, Any]], name: str) -> tuple[dict[str, Any] | None, str | None]:
    needle = name.strip().lower()
    if not needle:
        return None, None
    for workflow in workflows:
        if str(workflow.get("id", "")).strip().lower() == needle:
            return workflow, "id"
    for workflow in workflows:
        for alias in workflow.get("aliases", []) or []:
            if str(alias).strip().lower() == needle:
                return workflow, "alias"
    return None, None


def _validate_workflow_params(workflow: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    schema = workflow.get("paramSchema", {}) or {}
    required = [str(item) for item in schema.get("required", []) if str(item).strip()]
    optional = [str(item) for item in schema.get("optional", []) if str(item).strip()]
    allowed = set(required + optional)
    missing_required = [field for field in required if field not in params]
    invalid_fields = [key for key in params.keys() if allowed and key not in allowed]
    warnings: list[str] = []
    over_limit = False

    target_files = params.get("target_files")
    max_target_files = workflow.get("maxTargetFiles")
    if target_files is not None:
        if not isinstance(target_files, list):
            invalid_fields.append("target_files")
            warnings.append("target_files should be a list of paths.")
        elif isinstance(max_target_files, int) and len(target_files) > max_target_files:
            warnings.append("target_files has {0} entries, exceeds maxTargetFiles={1}.".format(len(target_files), max_target_files))
            over_limit = True

    valid = len(missing_required) == 0 and len(invalid_fields) == 0 and not over_limit
    return {
        "valid": valid,
        "missing_required": missing_required,
        "invalid_fields": invalid_fields,
        "warnings": warnings,
        "required": required,
        "optional": optional,
        "over_limit": over_limit,
    }


def _append_log_row(payload: dict[str, Any]) -> None:
    path = _decision_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= LOG_ROTATE_BYTES:
        rotated = _rotated_log_path()
        if rotated.exists():
            rotated.unlink()
        path.replace(rotated)
    record = dict(payload)
    if record.get("type") == "decision":
        task = str(record.get("task", ""))
        if len(task) > 2000:
            record["task"] = task[:2000]
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, separators=(",", ":")) + "\n")


def _read_recent_log_rows(limit: int) -> list[dict[str, Any]]:
    rows = []
    for path in (_rotated_log_path(), _decision_log_path()):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows[-limit:]


def _doctor_payload() -> dict[str, Any]:
    regs = get_registries(ROUTING_DIR)
    validation = validate_registries(ROUTING_DIR)
    counts = {
        "specialists": len(regs.get("specialists", [])),
        "workflows": len(regs.get("workflows", [])),
        "models": len(regs.get("models", [])),
        "task_classes": len([item for item in regs.get("task_classes", []) if item.get("id") != "unknown"]),
        "policies": len(regs.get("policies", {}).get("taskPolicies", {})),
    }
    warnings = list(validation.get("warnings", []))
    warnings.append(LEGACY_ROUTE_DEPRECATION)
    warnings.append("router.classify(raw text) is legacy and planned for removal before 1.0.")
    regs["_validation"] = validation
    return {
        "version": SERVER_VERSION,
        "package_path": str(PACKAGE_ROOT),
        "cwd": str(Path.cwd()),
        "workspace_root": str(_workspace_root()),
        "routing_dir": str(ROUTING_DIR),
        "registry_status": regs.get("_status", {}),
        "counts": counts,
        "decision_log_path": str(_decision_log_path()),
        "decision_log_exists": _decision_log_path().exists(),
        "workflow_count": counts["workflows"],
        "legacy_classifier_available": True,
        "legacy_classifier_deprecated": True,
        "validation": validation,
        "warnings": warnings,
    }


def _handle_doctor(params: dict[str, Any]) -> dict[str, Any]:
    del params
    structured = _doctor_payload()
    lines = [
        "Agent Router v{0}".format(SERVER_VERSION),
        "Routing dir: {0}".format(ROUTING_DIR),
        "Counts: {0} specialists, {1} workflows, {2} models, {3} task classes".format(
            structured["counts"]["specialists"],
            structured["counts"]["workflows"],
            structured["counts"]["models"],
            structured["counts"]["task_classes"],
        ),
    ]
    for warning in structured["warnings"]:
        lines.append("Warning: {0}".format(warning))
    return _text_result("\n".join(lines), structured)


def _handle_reload_registries(params: dict[str, Any]) -> dict[str, Any]:
    del params
    invalidate_cache()
    get_registries(ROUTING_DIR, force_reload=True)
    payload = _doctor_payload()
    payload["reloaded"] = True
    return _text_result("Registries reloaded.", payload)


def _handle_validate_registries(params: dict[str, Any]) -> dict[str, Any]:
    del params
    payload = validate_registries(ROUTING_DIR)
    text = "valid" if payload["valid"] else "invalid"
    return _text_result(text, payload)


def _handle_classify(params: dict[str, Any]) -> dict[str, Any]:
    task = str(params.get("task", "")).strip()
    if not task:
        return _error_result("missing_task", "classify requires a non-empty 'task' string.")
    regs = get_registries(ROUTING_DIR)
    result = classify_task(task, regs.get("task_classes", []))
    public = {
        "taskClass": result["taskClass"],
        "routeType": result["routeType"],
        "riskLevel": result["riskLevel"],
        "complexity": result["complexity"],
        "blastRadius": result["blastRadius"],
        "reason": result["reason"],
        "matchedSignals": result["matchedSignals"],
        "suggestedHandler": result["suggestedHandler"],
        "deprecated": True,
        "message": LEGACY_ROUTE_DEPRECATION,
    }
    return _text_result("Task class: {0}".format(public["taskClass"]), public)


def _handle_route(params: dict[str, Any]) -> dict[str, Any]:
    task = str(params.get("task", "")).strip()
    if not task:
        return _error_result("missing_task", "route requires a non-empty 'task' string.")
    regs = get_registries(ROUTING_DIR)
    try:
        decision = route_task(task, regs, params)
    except ValueError as exc:
        return _error_result("invalid_params", str(exc))
    decision["deprecated"] = True
    decision["message"] = LEGACY_ROUTE_DEPRECATION
    lines = [
        "Route decision: {0}".format(decision["decisionId"]),
        "Task class: {0}".format(decision["taskClass"]),
        "Route type: {0}".format(decision["routeType"]),
        "Model: {0} ({1})".format(decision.get("selectedModelId", "N/A"), decision["modelTier"]),
        "Approval required: {0}".format(decision["approvalRequired"]),
        "Next step: {0}".format(decision.get("nextStep", "")),
    ]
    return _text_result("\n".join(lines), decision)


def _handle_suggest_workflow(params: dict[str, Any]) -> dict[str, Any]:
    task = str(params.get("task", "")).strip()
    if not task:
        return _error_result("missing_task", "suggest_workflow requires a non-empty 'task' string.")
    regs = get_registries(ROUTING_DIR)
    classified = classify_task(task, regs.get("task_classes", []))
    task_class = str(classified.get("taskClass", "unknown"))
    candidates = []
    for workflow in regs.get("workflows", []):
        if task_class in (workflow.get("taskClasses", []) or []):
            candidates.append(_workflow_compact(workflow))
    top_workflow_id = candidates[0]["id"] if candidates else None
    task_policies = regs.get("policies", {}).get("taskPolicies", {})
    fallback_specialist = (task_policies.get(task_class, {}) or {}).get("preferredSpecialist", "architect")
    try:
        decision = route_task(task, regs, params)
    except ValueError as exc:
        return _error_result("invalid_params", str(exc))
    payload = {
        "classified": {
            "taskClass": classified["taskClass"],
            "routeType": classified["routeType"],
            "riskLevel": classified["riskLevel"],
            "complexity": classified["complexity"],
            "blastRadius": classified["blastRadius"],
            "matchedSignals": classified["matchedSignals"],
        },
        "candidates": candidates,
        "topWorkflowId": top_workflow_id,
        "fallback": {
            "routeType": "SPECIALIST_AGENT",
            "specialistId": fallback_specialist,
        },
        "decision": decision,
    }
    return _text_result("Suggested workflow computed.", payload)


def _handle_validate_decision(params: dict[str, Any]) -> dict[str, Any]:
    decision = params.get("decision")
    if not isinstance(decision, dict):
        return _error_result("missing_decision", "validate_decision requires a 'decision' object in params.")
    result = _validate_decision(decision)
    return _text_result("valid" if result["valid"] else "invalid", result)


def _handle_list_workflows(params: dict[str, Any]) -> dict[str, Any]:
    regs = get_registries(ROUTING_DIR)
    workflows = list(regs.get("workflows", []))
    task_class_filter = str(params.get("taskClass", "")).strip() or None
    risk_filter = str(params.get("riskLevel", "")).strip() or None
    profile_filter = str(params.get("profile", "")).strip() or None
    if task_class_filter:
        workflows = [w for w in workflows if task_class_filter in (w.get("taskClasses", []) or [])]
    if risk_filter:
        workflows = [w for w in workflows if (w.get("riskDefault") or w.get("riskLevel")) == risk_filter]
    if profile_filter:
        workflows = [w for w in workflows if str(w.get("profile", "")) == profile_filter]
    compact = [_workflow_compact(w) for w in workflows]
    return _text_result("{0} workflow(s) found.".format(len(compact)), {"workflows": compact, "count": len(compact)})


def _handle_get_workflow(params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name", "")).strip()
    if not name:
        return _error_result("missing_name", "get_workflow requires a non-empty 'name' string.")
    regs = get_registries(ROUTING_DIR)
    workflow, matched_by = _resolve_workflow(list(regs.get("workflows", [])), name)
    if workflow is None:
        return _error_result("unknown_workflow", "Unknown workflow name: {0}".format(name))
    return _text_result("Found workflow {0}.".format(workflow.get("id")), {"found": True, "matchedBy": matched_by, "workflow": workflow})


def _handle_validate_workflow_params(params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name", "")).strip()
    wf_params = params.get("params", {})
    if not name:
        return _error_result("missing_name", "validate_workflow_params requires a non-empty 'name' string.")
    if not isinstance(wf_params, dict):
        return _error_result("invalid_params", "validate_workflow_params requires params.params to be an object.")
    regs = get_registries(ROUTING_DIR)
    workflow, matched_by = _resolve_workflow(list(regs.get("workflows", [])), name)
    if workflow is None:
        return _error_result("unknown_workflow", "Unknown workflow name: {0}".format(name))
    validation = _validate_workflow_params(workflow, wf_params)
    structured = {
        "valid": validation["valid"],
        "workflowId": workflow.get("id"),
        "matchedBy": matched_by,
        "missing_required": validation["missing_required"],
        "invalid_fields": validation["invalid_fields"],
        "warnings": validation["warnings"],
    }
    return _text_result("valid" if validation["valid"] else "invalid", structured)


def _handle_match_workflow(params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name", "")).strip()
    wf_params = params.get("params", {})
    if not name:
        return _error_result("missing_name", "match_workflow requires a non-empty 'name' string.")
    if not isinstance(wf_params, dict):
        return _error_result("invalid_params", "match_workflow requires params.params to be an object.")
    regs = get_registries(ROUTING_DIR)
    workflow, _matched_by = _resolve_workflow(list(regs.get("workflows", [])), name)
    if workflow is None:
        return _text_result(
            "No workflow matched.",
            {
                "matched": False,
                "workflowId": None,
                "reason": "Unknown workflow name.",
                "fallback": "No workflow matched. Classify the task and proceed with specialist routing or the caller's own judgment.",
            },
        )
    validation = _validate_workflow_params(workflow, wf_params)
    warnings = list(validation["warnings"])
    if validation["missing_required"]:
        warnings.append("Missing required params: {0}.".format(", ".join(validation["missing_required"])))
    if validation["invalid_fields"]:
        warnings.append("Invalid params: {0}.".format(", ".join(validation["invalid_fields"])))
    structured = {
        "matched": True,
        "workflowId": workflow.get("id"),
        "profile": workflow.get("profile"),
        "specialistId": workflow.get("specialistId") or workflow.get("defaultSpecialist"),
        "modelTier": workflow.get("modelTier"),
        "maxCredits": workflow.get("maxCredits"),
        "allowedTools": list(workflow.get("allowedTools", [])),
        "requiresEdit": bool(workflow.get("requiresEdit", False)),
        "requiresExecute": bool(workflow.get("requiresExecute", False)),
        "requiredChecks": list(workflow.get("requiredChecks", [])),
        "params": wf_params,
        "paramsValid": validation["valid"],
        "missing_required": validation["missing_required"],
        "invalid_fields": validation["invalid_fields"],
        "warnings": warnings,
    }
    return _text_result("Matched workflow {0}.".format(workflow.get("id")), structured)


def _handle_list_specialists(params: dict[str, Any]) -> dict[str, Any]:
    regs = get_registries(ROUTING_DIR)
    specialists = list(regs.get("specialists", []))
    domain_filter = str(params.get("domain", "")).strip() or None
    tier_filter = str(params.get("allowedTier", "")).strip() or None
    tool_filter = str(params.get("tool", "")).strip() or None
    if domain_filter:
        specialists = [s for s in specialists if domain_filter in (s.get("domains", []) or [])]
    if tier_filter:
        specialists = [s for s in specialists if tier_filter in (s.get("allowedTiers", []) or [])]
    if tool_filter:
        specialists = [s for s in specialists if tool_filter in (s.get("allowedTools", []) or [])]
    return _text_result("{0} specialist(s) found.".format(len(specialists)), {"specialists": specialists, "count": len(specialists)})


def _handle_list_models(params: dict[str, Any]) -> dict[str, Any]:
    regs = get_registries(ROUTING_DIR)
    models = list(regs.get("models", []))
    policies = dict(regs.get("policies", {}))
    tier_filter = str(params.get("tier", "")).strip() or None
    max_credits_value = params.get("maxCredits")
    warnings: list[str] = []
    if tier_filter:
        models = [m for m in models if str(m.get("category", "")).strip().lower() == tier_filter.lower()]
    if max_credits_value is not None:
        try:
            max_credits = float(max_credits_value)
            medium_profile = dict((policies.get("costProfiles", {}) or {}).get("medium", {"inputTokens": 30000, "cachedInputTokens": 15000, "outputTokens": 5000}))
            filtered = []
            for model in models:
                try:
                    estimate = estimate_credits(model, medium_profile, estimated_input_tokens=None)
                except PricingError:
                    warnings.append("Excluded model with unknown pricing from maxCredits filter: {0}".format(model.get("id", "")))
                    continue
                if float(estimate["credits"]) <= max_credits:
                    filtered.append(dict(model, estimatedCredits=float(estimate["credits"])))
            models = filtered
        except (TypeError, ValueError):
            warnings.append("Ignored invalid maxCredits filter.")
    return _text_result("{0} model(s) found.".format(len(models)), {"models": models, "count": len(models), "warnings": warnings})


def _handle_log_decision(params: dict[str, Any]) -> dict[str, Any]:
    decision = params.get("decision")
    if not isinstance(decision, dict):
        return _error_result("missing_decision", "log_decision requires a 'decision' object in params.")
    validation = _validate_decision(decision)
    if not validation["valid"]:
        return _error_result("invalid_decision", "Decision failed validation and was not logged.", {"issues": validation["issues"], "warnings": validation["warnings"]})
    payload = dict(decision)
    payload["type"] = "decision"
    if _session_id() and not payload.get("sessionId"):
        payload["sessionId"] = _session_id()
    try:
        _append_log_row(payload)
    except OSError as exc:
        return _error_result("log_write_error", "Failed to write decision log: {0}".format(exc))
    return _text_result("Decision {0} logged.".format(payload.get("decisionId", "")), {"logged": True, "path": str(_decision_log_path()), "decisionId": payload.get("decisionId", "")})


def _handle_log_outcome(params: dict[str, Any]) -> dict[str, Any]:
    decision_id = str(params.get("decisionId", "")).strip()
    outcome = str(params.get("outcome", "")).strip()
    if not decision_id:
        return _error_result("missing_decision_id", "log_outcome requires decisionId.")
    if outcome not in {"followed", "overridden", "blocked_respected", "blocked_ignored"}:
        return _error_result("invalid_outcome", "Invalid outcome value.")
    payload = {
        "type": "outcome",
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "decisionId": decision_id,
        "outcome": outcome,
    }
    for key in ("selectedModelId", "selectionRank", "agentUsed", "reason"):
        if key in params and params.get(key) not in (None, ""):
            payload[key] = params.get(key)
    if _session_id():
        payload["sessionId"] = _session_id()
    try:
        _append_log_row(payload)
    except OSError as exc:
        return _error_result("log_write_error", "Failed to write outcome log: {0}".format(exc))
    return _text_result("Outcome logged.", {"logged": True, "decisionId": decision_id, "outcome": outcome, "path": str(_decision_log_path())})


def _handle_recent_decisions(params: dict[str, Any]) -> dict[str, Any]:
    limit_raw = params.get("limit", 20)
    try:
        limit = max(1, min(int(limit_raw), 200))
    except (TypeError, ValueError):
        limit = 20
    rows = _read_recent_log_rows(limit * 4)
    decisions: list[dict[str, Any]] = []
    outcomes: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_type = str(row.get("type", "decision"))
        if row_type == "outcome":
            outcomes[str(row.get("decisionId", ""))] = row
        else:
            decisions.append(row)
    joined = []
    for item in decisions[-limit:]:
        decision_id = str(item.get("decisionId", ""))
        joined.append({"decision": item, "outcome": outcomes.get(decision_id)})
    return _text_result("{0} decision(s) found.".format(len(joined)), {"decisions": joined, "count": len(joined)})


def _handle_explain(params: dict[str, Any]) -> dict[str, Any]:
    decision = params.get("decision")
    if not isinstance(decision, dict):
        return _error_result("missing_decision", "explain requires a 'decision' object in params.")
    explanation = _explain_decision(decision)
    return _text_result(explanation, {"explanation": explanation, "decisionId": decision.get("decisionId", "")})


GATEWAY_ACTIONS: dict[str, Any] = {
    "doctor": _handle_doctor,
    "reload_registries": _handle_reload_registries,
    "validate_registries": _handle_validate_registries,
    "suggest_workflow": _handle_suggest_workflow,
    "match_workflow": _handle_match_workflow,
    "get_workflow": _handle_get_workflow,
    "validate_workflow_params": _handle_validate_workflow_params,
    "list_workflows": _handle_list_workflows,
    "classify": _handle_classify,
    "route": _handle_route,
    "validate_decision": _handle_validate_decision,
    "list_specialists": _handle_list_specialists,
    "list_models": _handle_list_models,
    "log_decision": _handle_log_decision,
    "log_outcome": _handle_log_outcome,
    "recent_decisions": _handle_recent_decisions,
    "explain": _handle_explain,
}


def router_gateway(args: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(args, dict):
        return _error_result("invalid_args", "Router gateway arguments must be an object.", {"available_actions": sorted(GATEWAY_ACTIONS)})
    action = str(args.get("action", "")).strip()
    params = args.get("params", {})
    if params is None:
        params = {}
    if not action:
        return _error_result("missing_action", "Router gateway requires an 'action' field.", {"available_actions": sorted(GATEWAY_ACTIONS)})
    if not isinstance(params, dict):
        return _error_result("invalid_params", "Router gateway params must be an object when provided.", {"available_actions": sorted(GATEWAY_ACTIONS)})
    handler = GATEWAY_ACTIONS.get(action)
    if handler is None:
        return _error_result("unknown_action", "Unknown Router action: {0}".format(action), {"action": action, "available_actions": sorted(GATEWAY_ACTIONS)})
    return handler(params)


TOOLS = [
    {
        "name": GATEWAY_TOOL_NAME,
        "title": SERVER_TITLE,
        "description": (
            "Workflow registry and credit-aware routing gateway. Use action plus optional params. "
            "Actions: doctor, reload_registries, validate_registries, suggest_workflow, match_workflow, get_workflow, "
            "list_workflows, validate_workflow_params, classify (legacy), route (legacy), validate_decision, "
            "list_specialists, list_models, log_decision, log_outcome, recent_decisions, explain."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": sorted(GATEWAY_ACTIONS),
                    "description": "Required action name.",
                },
                "params": {"type": "object", "description": "Optional action parameters. Omit when not needed."},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    }
]


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def ok(request_id: Any, result: dict[str, Any]) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def rpc_error(request_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def handle_request(message: dict[str, Any]) -> None:
    global _SHOULD_EXIT
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    if request_id is None:
        return
    if method == "initialize":
        requested = params.get("protocolVersion") if isinstance(params, dict) else None
        ok(
            request_id,
            {
                "protocolVersion": requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "title": SERVER_TITLE, "version": SERVER_VERSION},
                "instructions": (
                    "Use the single router gateway tool with action plus optional params. "
                    "Actions: doctor, reload_registries, validate_registries, suggest_workflow, match_workflow, get_workflow, "
                    "list_workflows, validate_workflow_params, classify (legacy), route (legacy), validate_decision, "
                    "list_specialists, list_models, log_decision, log_outcome, recent_decisions, explain."
                ),
            },
        )
        return
    if method == "ping":
        ok(request_id, {})
        return
    if method == "shutdown":
        ok(request_id, {})
        _SHOULD_EXIT = True
        return
    if method == "tools/list":
        ok(request_id, {"tools": TOOLS})
        return
    if method == "tools/call":
        if not isinstance(params, dict):
            rpc_error(request_id, -32602, "Invalid tools/call params")
            return
        name = str(params.get("name", ""))
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            rpc_error(request_id, -32602, "Tool arguments must be an object")
            return
        if name != GATEWAY_TOOL_NAME:
            rpc_error(request_id, -32602, "Unknown tool: {0}".format(name))
            return
        try:
            ok(request_id, router_gateway(args))
        except Exception as exc:
            ok(request_id, _tool_error("{0}: {1}".format(type(exc).__name__, exc)))
        return
    rpc_error(request_id, -32601, "Method not found: {0}".format(method))


def main() -> int:
    global _SHOULD_EXIT
    _SHOULD_EXIT = False
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            rpc_error(None, -32700, "Parse error: {0}".format(exc))
            continue
        if isinstance(message, list):
            for item in message:
                if isinstance(item, dict):
                    handle_request(item)
                if _SHOULD_EXIT:
                    break
        elif isinstance(message, dict):
            handle_request(message)
        else:
            rpc_error(None, -32600, "Invalid request")
        if _SHOULD_EXIT:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
