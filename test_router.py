#!/usr/bin/env python3
"""Tests for Agent Router v0.2.2. Stdlib only."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "server.py"

sys.path.insert(0, str(ROOT))

from router_core import (
    classify_task,
    route_task,
    validate_decision,
    explain_decision,
)
from registries import get_registries, invalidate_cache
from schemas import TIER_ORDER, ROUTE_TYPES, REQUIRED_DECISION_FIELDS
from server import router_gateway

KNOWN_GOVERNOR_PROFILES = {
    "research_only",
    "analysis",
    "documentation_read",
    "documentation_write",
    "data_intake",
    "tiny_fix",
    "small_patch",
    "medium_refactor",
    "feature_work",
    "architecture_review",
    "debug_failure",
    "test_work",
    "exploratory_work",
    "general_work",
    "maintenance_work",
    "supervised_work",
}

MAINTENANCE_WORKFLOW_IDS = {
    "workflow.maintenance-prompt-audit",
    "workflow.maintenance-schema-audit",
    "workflow.maintenance-state-cleanup",
    "workflow.maintenance-mnemo",
    "workflow.maintenance-thrift-economy",
    "workflow.maintenance-governor-ledger",
    "workflow.maintenance-nexus-health",
    "workflow.alias-curation",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rpc(proc: subprocess.Popen, request: dict) -> dict:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("server closed stdout")
    return json.loads(line)


def _call_router(proc: subprocess.Popen, rid: int, action: str, params: dict | None = None) -> dict:
    return _rpc(proc, {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "tools/call",
        "params": {"name": "router", "arguments": {"action": action, "params": params or {}}},
    })


def _start_server(state_dir: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["AGENT_ROUTER_STATE_DIR"] = state_dir
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.Popen(
        [sys.executable, str(SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _stop_server(proc: subprocess.Popen) -> None:
    """Send JSON-RPC shutdown, close pipes, terminate/kill only if needed."""
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.write(
                json.dumps({"jsonrpc": "2.0", "id": 9999, "method": "shutdown"}, separators=(",", ":")) + "\n"
            )
            proc.stdin.flush()
    except (OSError, BrokenPipeError):
        pass
    for pipe in (proc.stdin, proc.stdout, proc.stderr):
        if pipe and not pipe.closed:
            try:
                pipe.close()
            except OSError:
                pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def _live_registries() -> dict:
    invalidate_cache()
    return get_registries()


# ---------------------------------------------------------------------------
# Unit tests: classification
# ---------------------------------------------------------------------------

class ClassifyTests(unittest.TestCase):

    def setUp(self):
        self.task_classes = _live_registries()["task_classes"]

    def test_classify_docs_task(self):
        result = classify_task("Update README wording to match existing GitHub style.", self.task_classes)
        self.assertEqual(result["taskClass"], "documentation_update")
        self.assertEqual(result["routeType"], "WORKFLOW")
        self.assertIn("doc-writer", result["suggestedHandler"])

    def test_classify_small_code_edit(self):
        result = classify_task("Rename this config key and update references in one file.", self.task_classes)
        self.assertEqual(result["taskClass"], "small_code_edit")
        self.assertEqual(result["routeType"], "WORKFLOW")

    def test_classify_simple_test_import_error(self):
        result = classify_task(
            "Tests are failing with an import error. Diagnose and apply the smallest fix.",
            self.task_classes,
        )
        self.assertEqual(result["taskClass"], "test_failure_simple")
        self.assertEqual(result["routeType"], "WORKFLOW")
        self.assertEqual(result["_preferredWorkflow"], "workflow.test-failure-triage")

    def test_classify_docs_update_with_entrypoint_stays_docs(self):
        result = classify_task(
            "Update documentation for the Thrift MCP server entrypoint in README or docs without changing source code.",
            self.task_classes,
        )
        self.assertEqual(result["taskClass"], "documentation_update")
        self.assertEqual(result["routeType"], "WORKFLOW")
        self.assertEqual(result["_preferredWorkflow"], "workflow.docs-sync")

    def test_classify_generic_small_refactor(self):
        result = classify_task(
            "Perform a small deterministic code refactor in the repository.",
            self.task_classes,
        )
        self.assertEqual(result["taskClass"], "small_code_edit")
        self.assertEqual(result["routeType"], "WORKFLOW")
        self.assertEqual(result["_preferredWorkflow"], "workflow.small-refactor")

    def test_classify_repeated_test_failure(self):
        result = classify_task(
            "The subprocess tests still hang after two fixes. Diagnose deeply.",
            self.task_classes,
        )
        self.assertEqual(result["taskClass"], "test_failure_repeated")
        self.assertIn(result["routeType"], {"SPECIALIST_AGENT", "MANUAL_PLAN_FIRST"})
        self.assertEqual(result["suggestedHandler"], "test-fixer")

    def test_classify_major_architecture(self):
        result = classify_task(
            "Design how Mnemo, Thrift, Governor, and Router should coordinate memory, budgeting, and routing across agent teams.",
            self.task_classes,
        )
        self.assertEqual(result["taskClass"], "major_agentic_architecture")
        self.assertIn(result["routeType"], {"MANUAL_PLAN_FIRST", "SPECIALIST_AGENT"})

    def test_classify_context_inspection(self):
        result = classify_task(
            "Inspect the Thrift MCP server entrypoint and summarize the relevant files without editing source.",
            self.task_classes,
        )
        self.assertEqual(result["taskClass"], "context_inspection")
        self.assertEqual(result["routeType"], "WORKFLOW")
        self.assertEqual(result["suggestedHandler"], "tooling-integrator")

    def test_classify_memory_feedback(self):
        result = classify_task(
            "Write memory feedback for this completed Mnemo/Thrift/Governor run.",
            self.task_classes,
        )
        self.assertEqual(result["taskClass"], "memory_feedback")
        self.assertEqual(result["routeType"], "WORKFLOW")
        self.assertEqual(result["suggestedHandler"], "memory-feedback-writer")

    def test_classify_alias_curation(self):
        result = classify_task(
            "Review Mnemo alias proposals and curate aliases for retrieval.",
            self.task_classes,
        )
        self.assertEqual(result["taskClass"], "alias_curation")
        self.assertEqual(result["routeType"], "WORKFLOW")
        self.assertEqual(result["_preferredWorkflow"], "workflow.alias-curation")
        self.assertEqual(result["suggestedHandler"], "memory-feedback-writer")

    def test_classify_unknown_returns_fallback(self):
        result = classify_task("Do something nobody has ever heard of.", self.task_classes)
        self.assertEqual(result["taskClass"], "unknown")
        self.assertIn(result["routeType"], ROUTE_TYPES)

    def test_classify_empty_returns_fallback(self):
        result = classify_task("", self.task_classes)
        self.assertEqual(result["taskClass"], "unknown")

    def test_classify_matched_signals_present(self):
        result = classify_task("Update the README for the new release.", self.task_classes)
        self.assertIsInstance(result["matchedSignals"], list)
        self.assertTrue(len(result["matchedSignals"]) > 0)

    def test_classify_has_required_fields(self):
        result = classify_task("Write docs.", self.task_classes)
        for field in ("taskClass", "routeType", "riskLevel", "complexity", "blastRadius",
                      "reason", "matchedSignals", "suggestedHandler"):
            self.assertIn(field, result, f"Missing field: {field}")


# ---------------------------------------------------------------------------
# Unit tests: routing
# ---------------------------------------------------------------------------

class RouteTests(unittest.TestCase):

    def _route(self, task: str) -> dict:
        return route_task(task, _live_registries())

    def test_route_docs_task(self):
        d = self._route("Update README wording to match existing GitHub style.")
        self.assertEqual(d["taskClass"], "documentation_update")
        self.assertEqual(d["routeType"], "WORKFLOW")
        self.assertEqual(d["workflowId"], "workflow.docs-sync")
        self.assertEqual(d["specialistId"], "doc-writer")
        self.assertIn(d["modelTier"], ("free", "cheap"))
        self.assertFalse(d["approvalRequired"])
        self.assertFalse(d["blocked"])

    def test_route_alias_curation_task(self):
        d = self._route("Run alias curation for Mnemo retrieval proposals.")
        self.assertEqual(d["taskClass"], "alias_curation")
        self.assertEqual(d["routeType"], "WORKFLOW")
        self.assertEqual(d["workflowId"], "workflow.alias-curation")
        self.assertEqual(d["specialistId"], "memory-feedback-writer")
        self.assertIn(d["modelTier"], ("free", "cheap"))
        self.assertFalse(d["approvalRequired"])
        self.assertFalse(d["blocked"])

    def test_route_small_edit(self):
        d = self._route("Rename this config key and update references in one file.")
        self.assertEqual(d["taskClass"], "small_code_edit")
        self.assertEqual(d["routeType"], "WORKFLOW")
        self.assertEqual(d["workflowId"], "workflow.small-refactor")
        self.assertEqual(d["specialistId"], "code-editor")
        self.assertIn(d["modelTier"], ("free", "cheap"))
        self.assertFalse(d["approvalRequired"])

    def test_route_docs_update_with_entrypoint_stays_docs(self):
        d = self._route(
            "Update documentation for the Thrift MCP server entrypoint in README or docs without changing source code."
        )
        self.assertEqual(d["taskClass"], "documentation_update")
        self.assertEqual(d["routeType"], "WORKFLOW")
        self.assertEqual(d["workflowId"], "workflow.docs-sync")
        self.assertEqual(d["specialistId"], "doc-writer")

    def test_route_generic_small_refactor(self):
        d = self._route("Perform a small deterministic code refactor in the repository.")
        self.assertEqual(d["taskClass"], "small_code_edit")
        self.assertEqual(d["routeType"], "WORKFLOW")
        self.assertEqual(d["workflowId"], "workflow.small-refactor")
        self.assertEqual(d["specialistId"], "code-editor")

    def test_route_simple_test_import_error(self):
        d = self._route("Tests are failing with an import error. Diagnose and apply the smallest fix.")
        self.assertEqual(d["taskClass"], "test_failure_simple")
        self.assertEqual(d["routeType"], "WORKFLOW")
        self.assertEqual(d["workflowId"], "workflow.test-failure-triage")
        self.assertEqual(d["specialistId"], "test-fixer")

    def test_route_repeated_test_failure(self):
        d = self._route("The subprocess tests still hang after two fixes. Diagnose deeply.")
        self.assertEqual(d["taskClass"], "test_failure_repeated")
        self.assertEqual(d["specialistId"], "test-fixer")
        self.assertEqual(d["modelTier"], "balanced")

    def test_route_major_architecture_approval_required(self):
        d = self._route(
            "Design how Mnemo, Thrift, Governor, and Router should coordinate memory, budgeting, and routing across agent teams."
        )
        self.assertEqual(d["taskClass"], "major_agentic_architecture")
        self.assertTrue(d["approvalRequired"], "Architecture task must require approval")
        self.assertIn(d["modelTier"], ("expensive", "blocked"))

    def test_route_context_inspection_workflow(self):
        d = self._route("Inspect the Thrift MCP server entrypoint and summarize the relevant files without editing source.")
        self.assertEqual(d["taskClass"], "context_inspection")
        self.assertEqual(d["routeType"], "WORKFLOW")
        self.assertEqual(d["workflowId"], "workflow.context-inspection")
        self.assertEqual(d["specialistId"], "tooling-integrator")
        self.assertIn(d["modelTier"], ("free", "cheap"))
        self.assertFalse(d["approvalRequired"])

    def test_route_memory_feedback_cheap(self):
        d = self._route("Write memory feedback for this completed run.")
        self.assertEqual(d["taskClass"], "memory_feedback")
        self.assertEqual(d["routeType"], "WORKFLOW")
        self.assertEqual(d["specialistId"], "memory-feedback-writer")
        self.assertIn(d["modelTier"], ("free", "cheap"))
        self.assertFalse(d["approvalRequired"])

    def test_route_memory_feedback_uses_gateway_tool_names(self):
        d = self._route("Write memory feedback for this completed run.")
        self.assertIn("mnemo.record", d["allowedTools"])
        self.assertNotIn("mnemo_record", d["allowedTools"])

    def test_route_decision_has_all_fields(self):
        d = self._route("Update README.")
        required = [
            "decisionId", "createdAt", "task", "taskClass", "routeType",
            "modelTier", "approvalRequired", "reason", "fallbackUsed",
            "blocked", "allowedTools", "requiredMemory", "requiredChecks",
        ]
        for field in required:
            self.assertIn(field, d, f"Missing field: {field}")

    def test_route_decision_id_prefix(self):
        d = self._route("Fix lint error.")
        self.assertTrue(d["decisionId"].startswith("rd_"), "decisionId must start with rd_")

    def test_route_workflow_first_over_specialist(self):
        """Docs task should prefer workflow over bare specialist routing."""
        d = self._route("Update the CHANGELOG with new entries.")
        self.assertEqual(d["routeType"], "WORKFLOW")
        self.assertIsNotNone(d["workflowId"])

    def test_route_unknown_model_cost_blocked_or_approval(self):
        """A registry with unknown multiplier should block or require approval."""
        custom_regs = dict(_live_registries())
        custom_regs["models"] = [
            {"id": "mystery-model", "vendor": "unknown", "tier": "cheap"}
        ]
        d = route_task("Rename a function.", custom_regs)
        self.assertTrue(
            d["blocked"] is True or d["approvalRequired"] is True,
            "Unknown model multiplier must block or require approval",
        )


# ---------------------------------------------------------------------------
# Unit tests: workflow registry APIs
# ---------------------------------------------------------------------------

class WorkflowRegistryTests(unittest.TestCase):

    def test_list_workflows_includes_required(self):
        resp = router_gateway({"action": "list_workflows", "params": {}})
        self.assertFalse(resp["isError"])
        workflows = resp["structuredContent"]["workflows"]
        ids = {w.get("id") for w in workflows}
        required = {
            "workflow.context-inspection",
            "workflow.memory-feedback",
            "workflow.alias-curation",
            "workflow.docs-sync",
            "workflow.small-refactor",
            "workflow.test-failure-triage",
            "workflow.maintenance-prompt-audit",
            "workflow.maintenance-schema-audit",
            "workflow.maintenance-state-cleanup",
            "workflow.maintenance-mnemo",
            "workflow.maintenance-thrift-economy",
            "workflow.maintenance-governor-ledger",
            "workflow.maintenance-nexus-health",
        }
        self.assertTrue(required.issubset(ids))

    def test_get_workflow_known(self):
        resp = router_gateway(
            {"action": "get_workflow", "params": {"name": "workflow.small-refactor"}}
        )
        self.assertFalse(resp["isError"])
        self.assertEqual(resp["structuredContent"]["workflow"]["id"], "workflow.small-refactor")

    def test_get_workflow_alias(self):
        resp = router_gateway({"action": "get_workflow", "params": {"name": "small-refactor"}})
        self.assertFalse(resp["isError"])
        self.assertEqual(resp["structuredContent"]["workflow"]["id"], "workflow.small-refactor")
        self.assertEqual(resp["structuredContent"]["matchedBy"], "alias")

    def test_alias_curation_workflow_registry_entry(self):
        resp = router_gateway({"action": "get_workflow", "params": {"name": "workflow.alias-curation"}})
        self.assertFalse(resp["isError"])
        workflow = resp["structuredContent"]["workflow"]
        self.assertEqual(workflow["id"], "workflow.alias-curation")
        self.assertEqual(workflow.get("profile"), "maintenance_work")
        self.assertEqual(workflow.get("specialistId"), "memory-feedback-writer")
        self.assertFalse(workflow.get("requiresEdit"))
        self.assertFalse(workflow.get("requiresExecute"))

        match = router_gateway({
            "action": "match_workflow",
            "params": {
                "name": "alias-curation",
                "params": {"task_summary": "Curate Mnemo alias proposals.", "domain": "agentic"},
            },
        })
        self.assertFalse(match["isError"])
        sc = match["structuredContent"]
        self.assertTrue(sc["matched"])
        self.assertEqual(sc["workflowId"], "workflow.alias-curation")
        self.assertTrue(sc["paramsValid"])

    def test_match_workflow_success(self):
        resp = router_gateway(
            {
                "action": "match_workflow",
                "params": {
                    "name": "workflow.small-refactor",
                    "params": {
                        "task_summary": "Reject BA and GB prefixes in inbox/iban.php",
                        "target_files": ["inbox/iban.php"],
                        "runtime_available": False,
                    },
                },
            }
        )
        self.assertFalse(resp["isError"])
        sc = resp["structuredContent"]
        self.assertTrue(sc["matched"])
        self.assertEqual(sc["workflowId"], "workflow.small-refactor")
        self.assertTrue(sc["paramsValid"])

    def test_match_workflow_missing_required_param(self):
        resp = router_gateway(
            {
                "action": "match_workflow",
                "params": {
                    "name": "workflow.small-refactor",
                    "params": {"target_files": ["inbox/iban.php"]},
                },
            }
        )
        self.assertFalse(resp["isError"])
        sc = resp["structuredContent"]
        self.assertTrue(sc["matched"])
        self.assertFalse(sc["paramsValid"])
        self.assertIn("task_summary", sc["missing_required"])

    def test_match_workflow_target_files_over_limit(self):
        resp = router_gateway(
            {
                "action": "match_workflow",
                "params": {
                    "name": "workflow.small-refactor",
                    "params": {
                        "task_summary": "Too many files.",
                        "target_files": ["a.py", "b.py", "c.py", "d.py"],
                    },
                },
            }
        )
        self.assertFalse(resp["isError"])
        sc = resp["structuredContent"]
        self.assertTrue(sc["matched"])
        self.assertFalse(sc["paramsValid"])
        self.assertTrue(any("maxTargetFiles" in w for w in sc["warnings"]))

    def test_match_workflow_unknown(self):
        resp = router_gateway(
            {"action": "match_workflow", "params": {"name": "workflow.unknown", "params": {}}}
        )
        self.assertFalse(resp["isError"])
        sc = resp["structuredContent"]
        self.assertFalse(sc["matched"])
        self.assertIsNone(sc["workflowId"])

    def test_validate_workflow_params_success(self):
        resp = router_gateway(
            {
                "action": "validate_workflow_params",
                "params": {
                    "name": "workflow.docs-sync",
                    "params": {"task_summary": "Update README and docs."},
                },
            }
        )
        self.assertFalse(resp["isError"])
        self.assertTrue(resp["structuredContent"]["valid"])

    def test_validate_workflow_params_failure(self):
        resp = router_gateway(
            {
                "action": "validate_workflow_params",
                "params": {
                    "name": "workflow.docs-sync",
                    "params": {"unknown_field": True},
                },
            }
        )
        self.assertFalse(resp["isError"])
        sc = resp["structuredContent"]
        self.assertFalse(sc["valid"])
        self.assertIn("task_summary", sc["missing_required"])

    def test_workflow_profiles_match_known_governor_profiles(self):
        resp = router_gateway({"action": "list_workflows", "params": {}})
        self.assertFalse(resp["isError"])
        for workflow in resp["structuredContent"]["workflows"]:
            profile = workflow.get("profile")
            self.assertIn(profile, KNOWN_GOVERNOR_PROFILES, f"Unknown profile in workflow {workflow.get('id')}: {profile}")

    def test_maintenance_workflows_use_maintenance_profile(self):
        for workflow_id in MAINTENANCE_WORKFLOW_IDS:
            resp = router_gateway({"action": "get_workflow", "params": {"name": workflow_id}})
            self.assertFalse(resp["isError"], workflow_id)
            workflow = resp["structuredContent"]["workflow"]
            self.assertEqual(workflow.get("profile"), "maintenance_work", workflow_id)

    def test_match_workflow_for_each_maintenance_workflow(self):
        for workflow_id in MAINTENANCE_WORKFLOW_IDS:
            resp = router_gateway(
                {
                    "action": "match_workflow",
                    "params": {
                        "name": workflow_id,
                        "params": {"task_summary": f"Run maintenance workflow {workflow_id}"},
                    },
                }
            )
            self.assertFalse(resp["isError"], workflow_id)
            sc = resp["structuredContent"]
            self.assertTrue(sc["matched"], workflow_id)
            self.assertEqual(sc["profile"], "maintenance_work", workflow_id)

    def test_validate_workflow_params_for_maintenance_workflow(self):
        resp = router_gateway(
            {
                "action": "validate_workflow_params",
                "params": {
                    "name": "workflow.maintenance-schema-audit",
                    "params": {"task_summary": "Run schema audit and summarize findings."},
                },
            }
        )
        self.assertFalse(resp["isError"])
        self.assertTrue(resp["structuredContent"]["valid"])

    def test_no_workflow_registry_references_legacy_route(self):
        resp = router_gateway({"action": "list_workflows", "params": {}})
        self.assertFalse(resp["isError"])
        serialized = json.dumps(resp["structuredContent"]["workflows"])
        self.assertNotIn("router.route", serialized)


# ---------------------------------------------------------------------------
# Unit tests: validation
# ---------------------------------------------------------------------------

class ValidateDecisionTests(unittest.TestCase):

    def _valid_decision(self) -> dict:
        return {
            "decisionId": "rd_abc123",
            "createdAt": "2026-05-15T10:00:00Z",
            "task": "Update README.",
            "taskClass": "documentation_update",
            "routeType": "WORKFLOW",
            "modelTier": "cheap",
            "approvalRequired": False,
            "reason": "Matched documentation_update keywords.",
            "blocked": False,
            "workflowId": "workflow.docs-sync",
            "specialistId": "doc-writer",
            "selectedModelId": "gpt-mini",
        }

    def test_valid_decision_passes(self):
        result = validate_decision(self._valid_decision())
        self.assertTrue(result["valid"])
        self.assertEqual(result["issues"], [])

    def test_missing_required_field(self):
        d = self._valid_decision()
        del d["taskClass"]
        result = validate_decision(d)
        self.assertFalse(result["valid"])
        self.assertTrue(any("taskClass" in i for i in result["issues"]))

    def test_invalid_route_type(self):
        d = self._valid_decision()
        d["routeType"] = "INVALID_TYPE"
        result = validate_decision(d)
        self.assertFalse(result["valid"])
        self.assertTrue(any("routeType" in i for i in result["issues"]))

    def test_invalid_risk_level(self):
        d = self._valid_decision()
        d["riskLevel"] = "critical"
        result = validate_decision(d)
        self.assertFalse(result["valid"])

    def test_invalid_model_tier(self):
        d = self._valid_decision()
        d["modelTier"] = "ultra"
        result = validate_decision(d)
        self.assertFalse(result["valid"])

    def test_approval_required_without_reason_warns(self):
        d = self._valid_decision()
        d["approvalRequired"] = True
        d["approvalReason"] = None
        result = validate_decision(d)
        self.assertTrue(result["valid"])
        self.assertTrue(len(result["warnings"]) > 0)

    def test_non_dict_input(self):
        result = validate_decision("not a dict")
        self.assertFalse(result["valid"])

    def test_null_required_field(self):
        d = self._valid_decision()
        d["reason"] = None
        result = validate_decision(d)
        self.assertFalse(result["valid"])


# ---------------------------------------------------------------------------
# Unit tests: explain
# ---------------------------------------------------------------------------

class ExplainDecisionTests(unittest.TestCase):

    def _sample_decision(self) -> dict:
        return route_task("Update README wording.", _live_registries())

    def test_explain_returns_string(self):
        explanation = explain_decision(self._sample_decision())
        self.assertIsInstance(explanation, str)

    def test_explain_contains_decision_id(self):
        d = self._sample_decision()
        explanation = explain_decision(d)
        self.assertIn(d["decisionId"], explanation)

    def test_explain_contains_route_type(self):
        explanation = explain_decision(self._sample_decision())
        self.assertIn("WORKFLOW", explanation)

    def test_explain_contains_specialist(self):
        d = self._sample_decision()
        explanation = explain_decision(d)
        self.assertIn(d["specialistId"], explanation)

    def test_explain_approval_required_section(self):
        d = route_task(
            "Design how Mnemo, Thrift, Governor, and Router should coordinate memory, budgeting, and routing.",
            _live_registries(),
        )
        explanation = explain_decision(d)
        self.assertIn("Approval required", explanation)


# ---------------------------------------------------------------------------
# Unit tests: log_decision (core)
# ---------------------------------------------------------------------------

class LogDecisionCoreTests(unittest.TestCase):

    def test_log_writes_jsonl(self):
        from server import _handle_log_decision

        d = route_task("Update README.", _live_registries())

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AGENT_ROUTER_STATE_DIR"] = tmp
            try:
                result = _handle_log_decision({"decision": d})
                self.assertFalse(result.get("isError", False))
                log_path = Path(tmp) / "route_decisions.jsonl"
                self.assertTrue(log_path.exists())
                lines = log_path.read_text(encoding="utf-8").strip().splitlines()
                self.assertEqual(len(lines), 1)
                parsed = json.loads(lines[0])
                self.assertEqual(parsed["decisionId"], d["decisionId"])
            finally:
                del os.environ["AGENT_ROUTER_STATE_DIR"]

    def test_log_rejects_invalid_decision(self):
        from server import _handle_log_decision
        result = _handle_log_decision({"decision": {"taskClass": "x"}})
        self.assertTrue(result.get("isError", False))


# ---------------------------------------------------------------------------
# MCP server integration tests — shared subprocess via setUpClass
# ---------------------------------------------------------------------------



class MCPStandaloneSmokeTests(unittest.TestCase):
    """Standalone MCP process tests that should not run with a shared server active."""

    def test_mcp_initialize_smoke(self):
        """Standalone subprocess test for the full initialize/tools/shutdown cycle."""
        tmp = tempfile.mkdtemp()
        proc = _start_server(tmp)
        try:
            init = _rpc(proc, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "t", "version": "1"}},
            })
            self.assertEqual(init["result"]["serverInfo"]["name"], "router")
            self.assertEqual(init["result"]["serverInfo"]["version"], "0.2.2")
            tools = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual(len(tools["result"]["tools"]), 1)
        finally:
            _stop_server(proc)
            shutil.rmtree(tmp, ignore_errors=True)


class MCPServerTests(unittest.TestCase):
    """All MCP integration tests share one subprocess for speed."""

    _proc: subprocess.Popen
    _tmp: str

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.mkdtemp()
        cls._proc = _start_server(cls._tmp)
        _rpc(cls._proc, {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "clientInfo": {"name": "test", "version": "0"},
            },
        })

    @classmethod
    def tearDownClass(cls) -> None:
        _stop_server(cls._proc)
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_tools_list_has_exactly_one_tool(self):
        resp = _rpc(self._proc, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        tools = resp["result"]["tools"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "router")

    def test_schema_has_no_forbidden_keys(self):
        resp = _rpc(self._proc, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        schema = resp["result"]["tools"][0]["inputSchema"]
        forbidden = {
            "minimum", "maximum", "default", "minItems", "maxItems",
            "minLength", "maxLength", "pattern", "anyOf", "oneOf", "allOf",
        }
        text = json.dumps(schema)
        for key in forbidden:
            self.assertNotIn(f'"{key}"', text, f"Forbidden key found in schema: {key}")

    def test_schema_action_is_enum(self):
        resp = _rpc(self._proc, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        schema = resp["result"]["tools"][0]["inputSchema"]
        action_schema = schema["properties"]["action"]
        self.assertIn("enum", action_schema)
        self.assertIsInstance(action_schema["enum"], list)
        self.assertIn("doctor", action_schema["enum"])
        self.assertIn("route", action_schema["enum"])
        self.assertIn("match_workflow", action_schema["enum"])
        self.assertIn("get_workflow", action_schema["enum"])
        self.assertIn("validate_workflow_params", action_schema["enum"])

    def test_doctor_action(self):
        resp = _call_router(self._proc, 2, "doctor")
        sc = resp["result"]["structuredContent"]
        self.assertEqual(sc["version"], "0.2.2")
        self.assertIsInstance(sc["counts"]["specialists"], int)
        self.assertIsInstance(sc["counts"]["workflows"], int)
        self.assertTrue(sc["legacy_classifier_available"])
        self.assertTrue(sc["legacy_classifier_deprecated"])
        self.assertIsInstance(sc["warnings"], list)
        self.assertTrue(any("deprecated" in w for w in sc["warnings"]))
        self.assertFalse(resp["result"]["isError"])

    def test_classify_docs(self):
        resp = _call_router(self._proc, 3, "classify", {"task": "Update README wording."})
        sc = resp["result"]["structuredContent"]
        self.assertEqual(sc["taskClass"], "documentation_update")
        self.assertEqual(sc["routeType"], "WORKFLOW")
        self.assertTrue(sc["deprecated"])

    def test_classify_small_edit(self):
        resp = _call_router(self._proc, 3, "classify", {"task": "Rename the config key in one file."})
        sc = resp["result"]["structuredContent"]
        self.assertEqual(sc["taskClass"], "small_code_edit")

    def test_classify_repeated_test_failure(self):
        resp = _call_router(self._proc, 3, "classify", {"task": "Tests still failing after two attempts."})
        sc = resp["result"]["structuredContent"]
        self.assertEqual(sc["taskClass"], "test_failure_repeated")

    def test_classify_architecture(self):
        resp = _call_router(self._proc, 3, "classify", {
            "task": "Design the architecture for cross-agent memory coordination."
        })
        sc = resp["result"]["structuredContent"]
        self.assertEqual(sc["taskClass"], "major_agentic_architecture")

    def test_route_docs_task(self):
        resp = _call_router(self._proc, 4, "route", {"task": "Update README wording."})
        sc = resp["result"]["structuredContent"]
        self.assertEqual(sc["taskClass"], "documentation_update")
        self.assertEqual(sc["routeType"], "WORKFLOW")
        self.assertEqual(sc["workflowId"], "workflow.docs-sync")
        self.assertFalse(sc["approvalRequired"])
        self.assertTrue(sc["deprecated"])

    def test_match_workflow_action(self):
        resp = _call_router(
            self._proc,
            4,
            "match_workflow",
            {
                "name": "workflow.small-refactor",
                "params": {
                    "task_summary": "Reject BA and GB prefixes in inbox/iban.php",
                    "target_files": ["inbox/iban.php"],
                    "runtime_available": False,
                },
            },
        )
        sc = resp["result"]["structuredContent"]
        self.assertTrue(sc["matched"])
        self.assertEqual(sc["workflowId"], "workflow.small-refactor")
        self.assertTrue(sc["paramsValid"])

    def test_get_workflow_action_alias(self):
        resp = _call_router(self._proc, 4, "get_workflow", {"name": "small-refactor"})
        sc = resp["result"]["structuredContent"]
        self.assertEqual(sc["workflow"]["id"], "workflow.small-refactor")
        self.assertEqual(sc["matchedBy"], "alias")

    def test_validate_workflow_params_action_failure(self):
        resp = _call_router(
            self._proc,
            4,
            "validate_workflow_params",
            {"name": "workflow.small-refactor", "params": {"target_files": ["a.py"]}},
        )
        sc = resp["result"]["structuredContent"]
        self.assertFalse(sc["valid"])
        self.assertIn("task_summary", sc["missing_required"])

    def test_route_small_edit(self):
        resp = _call_router(self._proc, 4, "route", {"task": "Rename the function in one file."})
        sc = resp["result"]["structuredContent"]
        self.assertEqual(sc["taskClass"], "small_code_edit")
        self.assertFalse(sc["approvalRequired"])

    def test_route_simple_test_import_error(self):
        resp = _call_router(self._proc, 4, "route", {
            "task": "Tests are failing with an import error. Diagnose and apply the smallest fix."
        })
        sc = resp["result"]["structuredContent"]
        self.assertEqual(sc["taskClass"], "test_failure_simple")
        self.assertEqual(sc["routeType"], "WORKFLOW")
        self.assertEqual(sc["workflowId"], "workflow.test-failure-triage")
        self.assertEqual(sc["specialistId"], "test-fixer")

    def test_route_repeated_test_failure(self):
        resp = _call_router(self._proc, 4, "route", {
            "task": "Tests still failing after two attempts. Deeply diagnose."
        })
        sc = resp["result"]["structuredContent"]
        self.assertEqual(sc["taskClass"], "test_failure_repeated")
        self.assertEqual(sc["specialistId"], "test-fixer")
        self.assertEqual(sc["modelTier"], "balanced")

    def test_route_major_architecture_approval_required(self):
        resp = _call_router(self._proc, 4, "route", {
            "task": "Design how Mnemo, Thrift, Governor, and Router should coordinate memory, budgeting, and routing across agent teams."
        })
        sc = resp["result"]["structuredContent"]
        self.assertEqual(sc["taskClass"], "major_agentic_architecture")
        self.assertTrue(sc["approvalRequired"])

    def test_mcp_route_context_inspection_workflow(self):
        resp = _call_router(self._proc, 4, "route", {"task": "Inspect the Thrift MCP server entrypoint and summarize the relevant files without editing source."})
        sc = resp["result"]["structuredContent"]
        self.assertEqual(sc["taskClass"], "context_inspection")
        self.assertEqual(sc["routeType"], "WORKFLOW")
        self.assertEqual(sc["workflowId"], "workflow.context-inspection")
        self.assertEqual(sc["specialistId"], "tooling-integrator")
        self.assertFalse(sc["approvalRequired"])

    def test_route_memory_feedback_cheap(self):
        resp = _call_router(self._proc, 4, "route", {"task": "Write memory feedback for this run."})
        sc = resp["result"]["structuredContent"]
        self.assertEqual(sc["taskClass"], "memory_feedback")
        self.assertIn(sc["modelTier"], ("free", "cheap"))
        self.assertFalse(sc["approvalRequired"])

    def test_validate_valid_decision(self):
        route_resp = _call_router(self._proc, 5, "route", {"task": "Update README."})
        decision = route_resp["result"]["structuredContent"]
        val_resp = _call_router(self._proc, 6, "validate_decision", {"decision": decision})
        sc = val_resp["result"]["structuredContent"]
        self.assertTrue(sc["valid"])
        self.assertEqual(sc["issues"], [])

    def test_validate_broken_decision(self):
        broken = {"taskClass": "x", "routeType": "BAD"}
        val_resp = _call_router(self._proc, 6, "validate_decision", {"decision": broken})
        sc = val_resp["result"]["structuredContent"]
        self.assertFalse(sc["valid"])
        self.assertTrue(len(sc["issues"]) > 0)

    def test_log_decision_writes_jsonl(self):
        route_resp = _call_router(self._proc, 7, "route", {"task": "Update README."})
        decision = route_resp["result"]["structuredContent"]
        log_resp = _call_router(self._proc, 8, "log_decision", {"decision": decision})
        sc = log_resp["result"]["structuredContent"]
        self.assertTrue(sc["logged"])
        self.assertTrue(Path(sc["path"]).exists())

    def test_explain_returns_readable_text(self):
        route_resp = _call_router(self._proc, 9, "route", {"task": "Update README."})
        decision = route_resp["result"]["structuredContent"]
        exp_resp = _call_router(self._proc, 10, "explain", {"decision": decision})
        sc = exp_resp["result"]["structuredContent"]
        self.assertIn("Route Decision", sc["explanation"])
        self.assertIn("Specialist", sc["explanation"])

    def test_list_workflows(self):
        resp = _call_router(self._proc, 11, "list_workflows")
        sc = resp["result"]["structuredContent"]
        self.assertGreater(sc["count"], 0)
        self.assertIsInstance(sc["workflows"], list)

    def test_list_specialists(self):
        resp = _call_router(self._proc, 11, "list_specialists")
        sc = resp["result"]["structuredContent"]
        self.assertGreater(sc["count"], 0)

    def test_list_models(self):
        resp = _call_router(self._proc, 11, "list_models")
        sc = resp["result"]["structuredContent"]
        self.assertGreater(sc["count"], 0)

    def test_unknown_action_returns_structured_error(self):
        resp = _call_router(self._proc, 12, "nonexistent_action")
        self.assertTrue(resp["result"]["isError"])
        sc = resp["result"]["structuredContent"]
        self.assertEqual(sc["error"], "unknown_action")
        self.assertIn("available_actions", sc)



if __name__ == "__main__":
    unittest.main()
