#!/usr/bin/env python3
"""Agent Router: dependency-free local MCP workflow/specialist/model-tier router.

Transport: newline-delimited JSON-RPC on stdin/stdout.
Storage: JSONL route decision log under state/router/.

Environment variables:
- AGENT_ROUTER_STATE_DIR: state directory for decision log. Defaults to <workspace>/state/router.
- AGENT_ROUTER_WORKSPACE_ROOT: workspace root used in doctor report. Defaults to cwd.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from registries import get_registries, invalidate_cache
from router_core import (
    classify_task,
    route_task,
    validate_decision as _validate_decision,
    explain_decision as _explain_decision,
)

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "router"
SERVER_TITLE = "Agent Router Gateway"
SERVER_VERSION = "0.2.2"
GATEWAY_TOOL_NAME = "router"
LEGACY_ROUTE_DEPRECATION = (
    "router.route(raw text) is deprecated for production workflows. "
    "Use router.match_workflow(name, params)."
)

PACKAGE_ROOT = Path(__file__).resolve().parent
ROUTING_DIR = PACKAGE_ROOT / "routing"

_SHOULD_EXIT = False


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

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
    if env:
        return Path(env)
    return Path.cwd()


def _decision_log_path() -> Path:
    return _state_dir() / "route_decisions.jsonl"


# ---------------------------------------------------------------------------
# Result helpers (matching MCP gateway response convention)
# ---------------------------------------------------------------------------

def _text_result(text: str, structured: dict[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "content": [{"type": "text", "text": text}],
        "isError": False,
    }
    if structured is not None:
        result["structuredContent"] = structured
    return result


def _error_result(error: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    structured: dict[str, Any] = {"error": error, "message": message}
    if details:
        structured.update(details)
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "isError": True,
        "structuredContent": structured,
    }


def _tool_error(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "isError": True,
    }


def _workflow_compact(workflow: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": workflow.get("id"),
        "aliases": list(workflow.get("aliases", [])),
        "description": workflow.get("description"),
        "profile": workflow.get("profile"),
        "specialistId": workflow.get("specialistId") or workflow.get("defaultSpecialist"),
        "modelTier": workflow.get("modelTier"),
        "maxMultiplier": workflow.get("maxMultiplier"),
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
        wf_id = str(workflow.get("id", "")).strip()
        if wf_id.lower() == needle:
            return workflow, "id"
    for workflow in workflows:
        aliases = workflow.get("aliases", [])
        if not isinstance(aliases, list):
            continue
        for alias in aliases:
            if str(alias).strip().lower() == needle:
                return workflow, "alias"
    return None, None


def _validate_workflow_params(workflow: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    schema = workflow.get("paramSchema", {}) or {}
    required = [str(v) for v in schema.get("required", []) if str(v).strip()]
    optional = [str(v) for v in schema.get("optional", []) if str(v).strip()]
    allowed = set(required + optional)

    missing_required: list[str] = [field for field in required if field not in params]
    invalid_fields: list[str] = [key for key in params.keys() if key not in allowed] if allowed else []
    warnings: list[str] = []

    target_files = params.get("target_files")
    max_target_files = workflow.get("maxTargetFiles")
    if target_files is not None:
        if not isinstance(target_files, list):
            invalid_fields.append("target_files")
            warnings.append("target_files should be a list of paths.")
        elif isinstance(max_target_files, int) and len(target_files) > max_target_files:
            warnings.append(
                f"target_files has {len(target_files)} entries, exceeds maxTargetFiles={max_target_files}."
            )

    allowed_tools = set(str(t) for t in workflow.get("allowedTools", []))
    if workflow.get("requiresEdit", False) and "edit" not in allowed_tools:
        warnings.append("Workflow requiresEdit=true but allowedTools does not include 'edit'.")

    requires_execute = bool(workflow.get("requiresExecute", False))
    execute_capable = any(tool in allowed_tools for tool in ("execute", "run_tests"))
    if requires_execute and not execute_capable:
        warnings.append("Workflow requiresExecute=true but allowedTools has no execute-like tool.")

    valid = len(missing_required) == 0 and len(invalid_fields) == 0 and all(
        "exceeds maxTargetFiles" not in warning for warning in warnings
    )

    return {
        "valid": valid,
        "missing_required": missing_required,
        "invalid_fields": invalid_fields,
        "warnings": warnings,
        "required": required,
        "optional": optional,
    }


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def _handle_doctor(params: dict[str, Any]) -> dict[str, Any]:
    regs = get_registries(ROUTING_DIR)
    status = regs.get("_status", {})

    counts = {
        "specialists": len(regs.get("specialists", [])),
        "workflows": len(regs.get("workflows", [])),
        "models": len(regs.get("models", [])),
        "task_classes": len([c for c in regs.get("task_classes", []) if c.get("id") != "unknown"]),
        "policies": len(regs.get("policies", {}).get("taskPolicies", {})),
    }

    log_path = _decision_log_path()
    warnings: list[str] = []

    for key, ok in status.items():
        if not ok:
            warnings.append(f"Registry '{key}' failed to load from {ROUTING_DIR / key}.")

    if counts["specialists"] == 0:
        warnings.append("No specialists loaded. Check routing/specialists.json.")
    if counts["workflows"] == 0:
        warnings.append("No workflows loaded. Check routing/workflows.json.")
    if counts["models"] == 0:
        warnings.append("No models loaded. Check routing/models.copilot.json.")
    warnings.append(LEGACY_ROUTE_DEPRECATION)
    warnings.append("router.classify(raw text) is deprecated for production workflows.")

    structured = {
        "version": SERVER_VERSION,
        "package_path": str(PACKAGE_ROOT),
        "cwd": str(Path.cwd()),
        "workspace_root": str(_workspace_root()),
        "routing_dir": str(ROUTING_DIR),
        "registry_status": status,
        "counts": counts,
        "decision_log_path": str(log_path),
        "decision_log_exists": log_path.exists(),
        "workflow_count": counts["workflows"],
        "legacy_classifier_available": True,
        "legacy_classifier_deprecated": True,
        "warnings": warnings,
    }

    lines = [f"Agent Router v{SERVER_VERSION}"]
    lines.append(f"Routing dir: {ROUTING_DIR}")
    lines.append(
        f"Counts: {counts['specialists']} specialists, {counts['workflows']} workflows, "
        f"{counts['models']} models, {counts['task_classes']} task classes"
    )
    if warnings:
        lines.extend(f"Warning: {w}" for w in warnings)
    else:
        lines.append("All registries loaded OK.")

    return _text_result("\n".join(lines), structured)


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

    text = (
        f"Task class: {public['taskClass']}\n"
        f"Route type: {public['routeType']}\n"
        f"Risk: {public['riskLevel']} | Complexity: {public['complexity']} | Blast: {public['blastRadius']}\n"
        f"Suggested handler: {public['suggestedHandler']}\n"
        f"Signals: {', '.join(public['matchedSignals']) or 'none'}\n"
        f"Reason: {public['reason']}"
    )
    return _text_result(text, public)


def _handle_route(params: dict[str, Any]) -> dict[str, Any]:
    task = str(params.get("task", "")).strip()
    if not task:
        return _error_result("missing_task", "route requires a non-empty 'task' string.")

    regs = get_registries(ROUTING_DIR)
    decision = route_task(task, regs, params)
    decision["deprecated"] = True
    decision["message"] = LEGACY_ROUTE_DEPRECATION

    lines = [f"Route decision: {decision['decisionId']}"]
    lines.append(f"Task class: {decision['taskClass']}")
    lines.append(f"Route type: {decision['routeType']}")
    if decision.get("workflowId"):
        lines.append(f"Workflow: {decision['workflowId']}")
    if decision.get("specialistId"):
        lines.append(f"Specialist: {decision['specialistId']}")
    lines.append(f"Model: {decision.get('selectedModelId', 'N/A')} ({decision['modelTier']})")
    lines.append(f"Approval required: {decision['approvalRequired']}")
    if decision.get("approvalReason"):
        lines.append(f"Approval reason: {decision['approvalReason']}")
    if decision.get("blocked"):
        lines.append(f"BLOCKED: {decision.get('blockReason', '')}")
    lines.append(f"Next step: {decision.get('nextStep', '')}")

    return _text_result("\n".join(lines), decision)


def _handle_validate_decision(params: dict[str, Any]) -> dict[str, Any]:
    decision = params.get("decision")
    if not isinstance(decision, dict):
        return _error_result(
            "missing_decision",
            "validate_decision requires a 'decision' object in params.",
        )

    result = _validate_decision(decision)
    text = "valid" if result["valid"] else "invalid"
    if result["issues"]:
        text += "\nIssues:\n" + "\n".join(f"  - {i}" for i in result["issues"])
    if result["warnings"]:
        text += "\nWarnings:\n" + "\n".join(f"  - {w}" for w in result["warnings"])

    return _text_result(text, result)


def _handle_list_workflows(params: dict[str, Any]) -> dict[str, Any]:
    regs = get_registries(ROUTING_DIR)
    workflows = list(regs.get("workflows", []))

    task_class_filter = str(params.get("taskClass", "")).strip() or None
    risk_filter = str(params.get("riskLevel", "")).strip() or None
    profile_filter = str(params.get("profile", "")).strip() or None

    if task_class_filter:
        workflows = [w for w in workflows if task_class_filter in w.get("taskClasses", [])]
    if risk_filter:
        workflows = [w for w in workflows if (w.get("riskDefault") or w.get("riskLevel")) == risk_filter]
    if profile_filter:
        workflows = [w for w in workflows if w.get("profile") == profile_filter]

    compact = [_workflow_compact(w) for w in workflows]
    structured = {"workflows": compact, "count": len(compact)}
    text = f"{len(workflows)} workflow(s) found."
    return _text_result(text, structured)


def _handle_get_workflow(params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name", "")).strip()
    if not name:
        return _error_result("missing_name", "get_workflow requires a non-empty 'name' string.")

    regs = get_registries(ROUTING_DIR)
    workflows = list(regs.get("workflows", []))
    workflow, matched_by = _resolve_workflow(workflows, name)
    if workflow is None:
        return _error_result("unknown_workflow", f"Unknown workflow name: {name}")

    structured = {
        "found": True,
        "matchedBy": matched_by,
        "workflow": workflow,
    }
    return _text_result(f"Found workflow {workflow.get('id')}.", structured)


def _handle_validate_workflow_params(params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name", "")).strip()
    wf_params = params.get("params", {})
    if not name:
        return _error_result(
            "missing_name",
            "validate_workflow_params requires a non-empty 'name' string.",
        )
    if not isinstance(wf_params, dict):
        return _error_result(
            "invalid_params",
            "validate_workflow_params requires params.params to be an object.",
        )

    regs = get_registries(ROUTING_DIR)
    workflows = list(regs.get("workflows", []))
    workflow, matched_by = _resolve_workflow(workflows, name)
    if workflow is None:
        return _error_result("unknown_workflow", f"Unknown workflow name: {name}")

    validation = _validate_workflow_params(workflow, wf_params)
    structured = {
        "valid": validation["valid"],
        "workflowId": workflow.get("id"),
        "matchedBy": matched_by,
        "missing_required": validation["missing_required"],
        "invalid_fields": validation["invalid_fields"],
        "warnings": validation["warnings"],
    }
    text = "valid" if validation["valid"] else "invalid"
    return _text_result(text, structured)


def _handle_match_workflow(params: dict[str, Any]) -> dict[str, Any]:
    name = str(params.get("name", "")).strip()
    wf_params = params.get("params", {})
    if not name:
        return _error_result("missing_name", "match_workflow requires a non-empty 'name' string.")
    if not isinstance(wf_params, dict):
        return _error_result("invalid_params", "match_workflow requires params.params to be an object.")

    regs = get_registries(ROUTING_DIR)
    workflows = list(regs.get("workflows", []))
    workflow, _matched_by = _resolve_workflow(workflows, name)
    if workflow is None:
        structured = {
            "matched": False,
            "workflowId": None,
            "reason": "Unknown workflow name.",
            "fallback": "Chief should use Thrift classify_task and own reasoning.",
        }
        return _text_result("No workflow matched.", structured)

    validation = _validate_workflow_params(workflow, wf_params)
    warnings = list(validation["warnings"])
    if validation["missing_required"]:
        warnings.append(f"Missing required params: {', '.join(validation['missing_required'])}.")
    if validation["invalid_fields"]:
        warnings.append(f"Invalid params: {', '.join(validation['invalid_fields'])}.")

    structured = {
        "matched": True,
        "workflowId": workflow.get("id"),
        "profile": workflow.get("profile"),
        "specialistId": workflow.get("specialistId") or workflow.get("defaultSpecialist"),
        "modelTier": workflow.get("modelTier"),
        "maxMultiplier": workflow.get("maxMultiplier"),
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
    return _text_result(f"Matched workflow {workflow.get('id')}.", structured)


def _handle_list_specialists(params: dict[str, Any]) -> dict[str, Any]:
    regs = get_registries(ROUTING_DIR)
    specialists = list(regs.get("specialists", []))

    domain_filter = str(params.get("domain", "")).strip() or None
    tier_filter = str(params.get("allowedTier", "")).strip() or None
    tool_filter = str(params.get("tool", "")).strip() or None

    if tier_filter:
        specialists = [s for s in specialists if tier_filter in s.get("allowedTiers", [])]
    if tool_filter:
        specialists = [s for s in specialists if tool_filter in s.get("allowedTools", [])]

    structured = {"specialists": specialists, "count": len(specialists)}
    text = f"{len(specialists)} specialist(s) found."
    return _text_result(text, structured)


def _handle_list_models(params: dict[str, Any]) -> dict[str, Any]:
    regs = get_registries(ROUTING_DIR)
    models = list(regs.get("models", []))

    tier_filter = str(params.get("tier", "")).strip() or None
    max_mult_str = params.get("maxMultiplier")

    if tier_filter:
        models = [m for m in models if m.get("tier") == tier_filter]
    if max_mult_str is not None:
        try:
            max_mult = float(max_mult_str)
            models = [m for m in models if float(m.get("premiumRequestMultiplier") or 0) <= max_mult]
        except (TypeError, ValueError):
            pass

    structured = {"models": models, "count": len(models)}
    text = f"{len(models)} model(s) found."
    return _text_result(text, structured)


def _handle_log_decision(params: dict[str, Any]) -> dict[str, Any]:
    decision = params.get("decision")
    if not isinstance(decision, dict):
        return _error_result(
            "missing_decision",
            "log_decision requires a 'decision' object in params.",
        )

    validation = _validate_decision(decision)
    if not validation["valid"]:
        return _error_result(
            "invalid_decision",
            "Decision failed validation and was not logged.",
            {"issues": validation["issues"], "warnings": validation["warnings"]},
        )

    log_path = _decision_log_path()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(decision, separators=(",", ":")) + "\n")
    except OSError as exc:
        return _error_result("log_write_error", f"Failed to write decision log: {exc}")

    decision_id = decision.get("decisionId", "")
    structured = {
        "logged": True,
        "path": str(log_path),
        "decisionId": decision_id,
    }
    return _text_result(f"Decision {decision_id} logged to {log_path}.", structured)


def _handle_explain(params: dict[str, Any]) -> dict[str, Any]:
    decision = params.get("decision")
    if not isinstance(decision, dict):
        return _error_result(
            "missing_decision",
            "explain requires a 'decision' object in params.",
        )

    explanation = _explain_decision(decision)
    structured = {"explanation": explanation, "decisionId": decision.get("decisionId", "")}
    return _text_result(explanation, structured)


# ---------------------------------------------------------------------------
# Gateway dispatch
# ---------------------------------------------------------------------------

GATEWAY_ACTIONS: dict[str, Any] = {
    "doctor": _handle_doctor,
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
    "explain": _handle_explain,
}


def router_gateway(args: dict[str, Any]) -> dict[str, Any]:
    """Dispatch the single public Router MCP tool to an internal action handler."""
    if not isinstance(args, dict):
        return _error_result(
            "invalid_args",
            "Router gateway arguments must be an object.",
            {"available_actions": sorted(GATEWAY_ACTIONS)},
        )
    action = str(args.get("action", "")).strip() or None
    params = args.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error_result(
            "invalid_params",
            "Router gateway params must be an object when provided.",
            {"available_actions": sorted(GATEWAY_ACTIONS)},
        )
    if not action:
        return _error_result(
            "missing_action",
            "Router gateway requires an 'action' field.",
            {"available_actions": sorted(GATEWAY_ACTIONS)},
        )
    handler = GATEWAY_ACTIONS.get(action)
    if handler is None:
        return _error_result(
            "unknown_action",
            f"Unknown Router action: {action}",
            {"action": action, "available_actions": sorted(GATEWAY_ACTIONS)},
        )
    return handler(params)


TOOLS = [
    {
        "name": GATEWAY_TOOL_NAME,
        "title": SERVER_TITLE,
        "description": (
            "Workflow registry and routing gateway. "
            "Use action plus optional params. "
            "Actions: doctor, match_workflow, get_workflow, list_workflows, validate_workflow_params, "
            "classify (deprecated), route (deprecated), validate_decision, "
            "list_specialists, list_models, log_decision, explain."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": sorted(GATEWAY_ACTIONS),
                    "description": (
                        "Required action name. "
                        "doctor=diagnostics, match/get/list/validate_workflow*=workflow registry operations, "
                        "classify/route=legacy classifier compatibility (deprecated), "
                        "validate_decision=validate a decision object, list_specialists/list_models=registry queries, "
                        "log_decision=append decision to JSONL log, explain=human-readable explanation."
                    ),
                },
                "params": {
                    "type": "object",
                    "description": "Optional action parameters. Omit when not needed.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
]


# ---------------------------------------------------------------------------
# MCP JSON-RPC protocol
# ---------------------------------------------------------------------------

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
                "protocolVersion": requested or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": SERVER_NAME,
                    "title": SERVER_TITLE,
                    "version": SERVER_VERSION,
                },
                "instructions": (
                    "Use the single router gateway tool with action plus optional params. "
                    "Actions: doctor, match_workflow, get_workflow, list_workflows, validate_workflow_params, "
                    "classify (deprecated), route (deprecated), validate_decision, list_specialists, "
                    "list_models, log_decision, explain. Router returns workflow prescriptions; "
                    "it does not execute tasks."
                ),
            },
        )
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
            rpc_error(request_id, -32602, f"Unknown tool: {name}")
            return
        try:
            ok(request_id, router_gateway(args))
        except Exception as exc:
            ok(request_id, _tool_error(f"{type(exc).__name__}: {exc}"))
        return

    rpc_error(request_id, -32601, f"Method not found: {method}")


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
            rpc_error(None, -32700, f"Parse error: {exc}")
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
