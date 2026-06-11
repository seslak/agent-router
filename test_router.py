#!/usr/bin/env python3
"""Tests for Agent Router."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "server.py"
sys.path.insert(0, str(ROOT))

from registries import get_registries, invalidate_cache, validate_registries
from router_core import classify_task, explain_decision, route_task, validate_decision
from server import router_gateway


def _live_registries() -> dict:
    invalidate_cache()
    return get_registries(ROOT / "routing", force_reload=True)


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
    return _rpc(
        proc,
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "tools/call",
            "params": {"name": "router", "arguments": {"action": action, "params": params or {}}},
        },
    )


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
    try:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 9999, "method": "shutdown"}, separators=(",", ":")) + "\n")
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
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=5)


class PricingRouteTests(unittest.TestCase):
    def test_cheapest_capable_model_ordering(self) -> None:
        decision = route_task("Rename one config key in one file.", _live_registries())
        ranked = decision["rankedModels"]
        self.assertGreaterEqual(len(ranked), 3)
        self.assertEqual(ranked[0]["modelId"], "gpt-5.4-nano")
        self.assertEqual(ranked[1]["modelId"], "gpt-5-mini")
        self.assertEqual(ranked[2]["modelId"], "gpt-5.4-mini")

    def test_long_context_pricing_condition_present(self) -> None:
        decision = route_task(
            "Design the architecture for cross-agent routing.",
            _live_registries(),
            {"estimated_input_tokens": 300000},
        )
        top = decision["rankedModels"][0]
        self.assertIn(top["pricingApplied"], ("default", "long_context"))
        if top["modelId"] in {"gpt-5.4", "gpt-5.5", "gemini-3.1-pro"}:
            self.assertIn("long_context_pricing", top["approvalConditions"])

    def test_unknown_pricing_block_behavior_excludes_without_raise(self) -> None:
        regs = _live_registries()
        regs["models"] = [{"id": "broken", "displayName": "Broken", "vendor": "x", "category": "lightweight", "releaseStatus": "ga", "pricing": None}]
        decision = route_task("Rename one config key in one file.", regs)
        self.assertTrue(decision["blocked"])
        self.assertEqual(decision["skipped"]["unknown_pricing_skipped"], 1)

    def test_unknown_pricing_treat_as_expensive_requires_approval(self) -> None:
        regs = _live_registries()
        regs["policies"]["defaultPolicy"]["unknownPricingBehavior"] = "treat_as_expensive"
        regs["policies"]["taskPolicies"]["major_agentic_architecture"]["maxCredits"] = 1000
        regs["models"] = [{"id": "broken", "displayName": "Broken", "vendor": "x", "category": "powerful", "releaseStatus": "ga", "pricing": None}]
        decision = route_task("Design the architecture for cross-agent routing.", regs)
        self.assertFalse(decision["blocked"])
        self.assertEqual(decision["modelTier"], "expensive")
        self.assertTrue(decision["approvalRequired"])

    def test_deterministic_tiebreaker_uses_model_id(self) -> None:
        regs = _live_registries()
        regs["models"] = [
            {"id": "b-model", "displayName": "B", "vendor": "x", "category": "lightweight", "releaseStatus": "ga", "pricing": {"default": {"input": 0.2, "cachedInput": 0.02, "output": 1.25}}},
            {"id": "a-model", "displayName": "A", "vendor": "x", "category": "lightweight", "releaseStatus": "ga", "pricing": {"default": {"input": 0.2, "cachedInput": 0.02, "output": 1.25}}},
        ]
        first = route_task("Rename one config key in one file.", regs)
        second = route_task("Rename one config key in one file.", regs)
        self.assertEqual(first["selectedModelId"], "a-model")
        self.assertEqual(second["selectedModelId"], "a-model")

    def test_candidate_filter_and_ideal_outside_candidates(self) -> None:
        decision = route_task(
            "Rename one config key in one file.",
            _live_registries(),
            {"candidate_models": ["gpt-5-mini"]},
        )
        self.assertTrue(decision["candidateConstraint"]["applied"])
        self.assertEqual(decision["selectedModelId"], "gpt-5-mini")
        self.assertFalse(decision["candidateConstraint"]["idealModelInCandidates"])

    def test_constraint_only_block_names_ideal_model(self) -> None:
        decision = route_task(
            "Rename one config key in one file.",
            _live_registries(),
            {"candidate_models": ["claude-fable-5"]},
        )
        self.assertTrue(decision["blocked"])
        self.assertTrue(decision["idealModelId"])

    def test_empty_candidate_list_treated_as_absent(self) -> None:
        unconstrained = route_task("Rename one config key in one file.", _live_registries())
        constrained = route_task("Rename one config key in one file.", _live_registries(), {"candidate_models": []})
        self.assertEqual(unconstrained["selectedModelId"], constrained["selectedModelId"])

    def test_scalar_fields_match_rank_one(self) -> None:
        decision = route_task("Rename one config key in one file.", _live_registries())
        top = decision["rankedModels"][0]
        self.assertEqual(decision["selectedModelId"], top["modelId"])
        self.assertEqual(decision["modelTier"], top["tier"])
        self.assertEqual(decision["estimatedCredits"], top["estimatedCredits"])


class ClassifierAndRegistryTests(unittest.TestCase):
    def test_domain_filter_works(self) -> None:
        resp = router_gateway({"action": "list_specialists", "params": {"domain": "docs"}})
        self.assertFalse(resp["isError"])
        ids = {item["id"] for item in resp["structuredContent"]["specialists"]}
        self.assertIn("doc-writer", ids)
        self.assertNotIn("code-editor", ids)

    def test_word_boundary_keyword_matching(self) -> None:
        result = classify_task("Write the latest release notes.", _live_registries()["task_classes"])
        self.assertEqual(result["taskClass"], "documentation_update")

    def test_validate_workflow_params_over_limit_flag(self) -> None:
        resp = router_gateway(
            {
                "action": "validate_workflow_params",
                "params": {"name": "workflow.small-refactor", "params": {"task_summary": "x", "target_files": ["a", "b", "c", "d"]}},
            }
        )
        self.assertFalse(resp["structuredContent"]["valid"])

    def test_list_models_max_credits_filter(self) -> None:
        resp = router_gateway({"action": "list_models", "params": {"maxCredits": 2}})
        self.assertFalse(resp["isError"])
        ids = {item["id"] for item in resp["structuredContent"]["models"]}
        self.assertIn("gpt-5.4-nano", ids)
        self.assertNotIn("claude-fable-5", ids)

    def test_registry_validation_live(self) -> None:
        payload = validate_registries(ROOT / "routing")
        self.assertTrue(payload["valid"], payload["errors"])

    def test_registry_validation_rejects_missing_specialist_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            shutil.copytree(ROOT / "routing", target / "routing")
            path = target / "routing" / "policies.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["fallbackPolicy"]["onMissingSpecialist"] = "does-not-exist"
            path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
            payload = validate_registries(target / "routing")
            self.assertFalse(payload["valid"])
            self.assertTrue(any("fallbackPolicy.onMissingSpecialist" in error for error in payload["errors"]))

    def test_registry_staleness_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            shutil.copytree(ROOT / "routing", target / "routing")
            path = target / "routing" / "models.copilot.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["asOf"] = "2025-01-01"
            path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
            payload = validate_registries(target / "routing")
            self.assertTrue(any("older than 180 days" in warning for warning in payload["warnings"]))

    def test_priority_phrase_registry_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            shutil.copytree(ROOT / "routing", target / "routing")
            path = target / "routing" / "task-classes.json"
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["classes"].insert(
                0,
                {
                    "id": "custom_priority",
                    "keywords": ["priority marker"],
                    "priorityPhrases": ["priority marker"],
                    "routeType": "SPECIALIST_AGENT",
                    "defaultRisk": "low",
                    "defaultComplexity": "low",
                    "defaultBlastRadius": "low",
                    "preferredWorkflow": None,
                    "preferredSpecialist": "architect"
                }
            )
            path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
            regs = get_registries(target / "routing", force_reload=True)
            result = classify_task("Please handle the priority marker case.", regs["task_classes"])
            self.assertEqual(result["taskClass"], "custom_priority")


class LoggingAndGatewayTests(unittest.TestCase):
    def test_ping_and_protocol_fallback(self) -> None:
        with mock.patch("server.ok") as ok_mock:
            from server import handle_request, PROTOCOL_VERSION
            handle_request({"id": 1, "method": "ping", "params": {}})
            ok_mock.assert_called_once_with(1, {})
            ok_mock.reset_mock()
            handle_request({"id": 2, "method": "initialize", "params": {"protocolVersion": "1900-01-01"}})
            payload = ok_mock.call_args[0][1]
            self.assertEqual(payload["protocolVersion"], PROTOCOL_VERSION)

    def test_log_rotation_and_task_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            decision = route_task("x" * 3000, _live_registries())
            with mock.patch("server.LOG_ROTATE_BYTES", 10):
                os.environ["AGENT_ROUTER_STATE_DIR"] = tmp
                try:
                    router_gateway({"action": "log_decision", "params": {"decision": decision}})
                    router_gateway({"action": "log_decision", "params": {"decision": decision}})
                finally:
                    del os.environ["AGENT_ROUTER_STATE_DIR"]
            rotated = Path(tmp) / "route_decisions.1.jsonl"
            current = Path(tmp) / "route_decisions.jsonl"
            self.assertTrue(rotated.exists())
            self.assertTrue(current.exists())
            first = json.loads(current.read_text(encoding="utf-8").splitlines()[-1])
            self.assertLessEqual(len(first["task"]), 2000)

    def test_session_id_stamped_on_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AGENT_ROUTER_STATE_DIR"] = tmp
            os.environ["AGENT_SUITE_SESSION_ID"] = "suite-1"
            try:
                decision = route_task("Update README wording.", _live_registries())
                router_gateway({"action": "log_decision", "params": {"decision": decision}})
                router_gateway({"action": "log_outcome", "params": {"decisionId": decision["decisionId"], "outcome": "followed", "selectionRank": 1}})
                rows = Path(tmp, "route_decisions.jsonl").read_text(encoding="utf-8").splitlines()
            finally:
                del os.environ["AGENT_ROUTER_STATE_DIR"]
                del os.environ["AGENT_SUITE_SESSION_ID"]
            self.assertEqual(json.loads(rows[0])["sessionId"], "suite-1")
            self.assertEqual(json.loads(rows[1])["sessionId"], "suite-1")

    def test_recent_decisions_joins_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AGENT_ROUTER_STATE_DIR"] = tmp
            try:
                decision = route_task("Update README wording.", _live_registries())
                router_gateway({"action": "log_decision", "params": {"decision": decision}})
                router_gateway({"action": "log_outcome", "params": {"decisionId": decision["decisionId"], "outcome": "overridden", "selectedModelId": "gpt-5-mini", "selectionRank": 2, "agentUsed": "docs-agent"}})
                resp = router_gateway({"action": "recent_decisions", "params": {"limit": 10}})
            finally:
                del os.environ["AGENT_ROUTER_STATE_DIR"]
            joined = resp["structuredContent"]["decisions"][0]
            self.assertEqual(joined["outcome"]["outcome"], "overridden")
            self.assertEqual(joined["outcome"]["selectionRank"], 2)
            self.assertEqual(joined["outcome"]["agentUsed"], "docs-agent")

    def test_recent_decisions_reads_rotated_log_before_live_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AGENT_ROUTER_STATE_DIR"] = tmp
            try:
                rotated = Path(tmp) / "route_decisions.1.jsonl"
                current = Path(tmp) / "route_decisions.jsonl"
                rotated.write_text(
                    json.dumps({"type": "decision", "decisionId": "dec-old", "task": "old"}) + "\n",
                    encoding="utf-8",
                )
                current.write_text(
                    json.dumps({"type": "decision", "decisionId": "dec-new", "task": "new"}) + "\n",
                    encoding="utf-8",
                )
                resp = router_gateway({"action": "recent_decisions", "params": {"limit": 10}})
            finally:
                del os.environ["AGENT_ROUTER_STATE_DIR"]
            ids = [item["decision"]["decisionId"] for item in resp["structuredContent"]["decisions"]]
            self.assertEqual(ids, ["dec-old", "dec-new"])


class SuggestWorkflowAndMCPTests(unittest.TestCase):
    def test_suggest_workflow_includes_decision(self) -> None:
        resp = router_gateway({"action": "suggest_workflow", "params": {"task": "Update README wording."}})
        self.assertFalse(resp["isError"])
        sc = resp["structuredContent"]
        self.assertEqual(sc["classified"]["taskClass"], "documentation_update")
        self.assertEqual(sc["topWorkflowId"], "workflow.docs-sync")
        self.assertEqual(sc["decision"]["workflowId"], "workflow.docs-sync")

    def test_governor_start_hint_present_for_workflow(self) -> None:
        decision = route_task("Update README wording.", _live_registries())
        self.assertEqual(decision["governorStartHint"]["params"]["profile"], "documentation_write")

    def test_mcp_tools_list_and_route_smoke(self) -> None:
        tmp = tempfile.mkdtemp()
        proc = _start_server(tmp)
        try:
            init = _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            self.assertEqual(init["result"]["serverInfo"]["version"], "0.5.2")
            tools = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual(len(tools["result"]["tools"]), 1)
            resp = _call_router(proc, 3, "suggest_workflow", {"task": "Update README wording.", "estimated_input_tokens": 300000})
            self.assertFalse(resp["result"]["isError"])
        finally:
            _stop_server(proc)
            shutil.rmtree(tmp, ignore_errors=True)


class ClassificationCoverageTests(unittest.TestCase):
    def test_docs_update_classification(self) -> None:
        result = classify_task("Please update the README and release notes.", _live_registries()["task_classes"])
        self.assertEqual(result["taskClass"], "documentation_update")

    def test_simple_test_failure_classification(self) -> None:
        result = classify_task("Tests are failing with an import error.", _live_registries()["task_classes"])
        self.assertEqual(result["taskClass"], "test_failure_simple")

    def test_repeated_test_failure_classification(self) -> None:
        result = classify_task("The suite is still failing after two attempts and looks flaky.", _live_registries()["task_classes"])
        self.assertEqual(result["taskClass"], "test_failure_repeated")

    def test_small_refactor_classification(self) -> None:
        result = classify_task("Do a small deterministic refactor in one file.", _live_registries()["task_classes"])
        self.assertEqual(result["taskClass"], "small_code_edit")

    def test_major_architecture_classification(self) -> None:
        result = classify_task("Redesign the routing layer architecture across the MCP ecosystem.", _live_registries()["task_classes"])
        self.assertEqual(result["taskClass"], "major_agentic_architecture")

    def test_context_inspection_classification(self) -> None:
        result = classify_task("Inspect the MCP server entrypoint without editing source.", _live_registries()["task_classes"])
        self.assertEqual(result["taskClass"], "context_inspection")

    def test_memory_feedback_classification(self) -> None:
        result = classify_task("Write memory feedback for the completed interaction log.", _live_registries()["task_classes"])
        self.assertEqual(result["taskClass"], "memory_feedback")

    def test_alias_curation_classification(self) -> None:
        result = classify_task("Review mnemo alias proposals and curate aliases.", _live_registries()["task_classes"])
        self.assertEqual(result["taskClass"], "alias_curation")

    def test_docs_with_entrypoint_stays_docs(self) -> None:
        result = classify_task("Update docs for the MCP server entrypoint.", _live_registries()["task_classes"])
        self.assertEqual(result["taskClass"], "documentation_update")

    def test_unknown_fallback_classification(self) -> None:
        result = classify_task("Completely novel prompt with no registry keywords.", _live_registries()["task_classes"])
        self.assertEqual(result["taskClass"], "unknown")

    def test_empty_fallback_classification(self) -> None:
        result = classify_task("", _live_registries()["task_classes"])
        self.assertEqual(result["taskClass"], "unknown")

    def test_matched_signals_present_for_docs(self) -> None:
        result = classify_task("Update documentation and changelog.", _live_registries()["task_classes"])
        self.assertTrue(result["matchedSignals"])

    def test_classification_required_fields_present(self) -> None:
        result = classify_task("Update documentation and changelog.", _live_registries()["task_classes"])
        for field in ("taskClass", "routeType", "riskLevel", "complexity", "blastRadius", "reason", "matchedSignals", "suggestedHandler"):
            self.assertIn(field, result)

    def test_priority_phrase_for_still_failing_wins(self) -> None:
        result = classify_task("This test is still failing after two fixes.", _live_registries()["task_classes"])
        self.assertEqual(result["taskClass"], "test_failure_repeated")


class DecisionValidationCoverageTests(unittest.TestCase):
    def _decision(self) -> dict:
        return route_task("Update README wording.", _live_registries())

    def test_validate_decision_valid_passes(self) -> None:
        payload = validate_decision(self._decision())
        self.assertTrue(payload["valid"], payload)

    def test_validate_decision_missing_required_field(self) -> None:
        decision = self._decision()
        decision.pop("decisionId", None)
        payload = validate_decision(decision)
        self.assertFalse(payload["valid"])
        self.assertTrue(any("decisionId" in issue for issue in payload["issues"]))

    def test_validate_decision_null_required_field(self) -> None:
        decision = self._decision()
        decision["routeType"] = None
        payload = validate_decision(decision)
        self.assertFalse(payload["valid"])
        self.assertTrue(any("routeType" in issue for issue in payload["issues"]))

    def test_validate_decision_invalid_route_type(self) -> None:
        decision = self._decision()
        decision["routeType"] = "NOT_REAL"
        payload = validate_decision(decision)
        self.assertFalse(payload["valid"])
        self.assertTrue(any("routeType" in issue for issue in payload["issues"]))

    def test_validate_decision_invalid_risk_level(self) -> None:
        decision = self._decision()
        decision["riskLevel"] = "wild"
        payload = validate_decision(decision)
        self.assertFalse(payload["valid"])
        self.assertTrue(any("riskLevel" in issue for issue in payload["issues"]))

    def test_validate_decision_invalid_model_tier(self) -> None:
        decision = self._decision()
        decision["modelTier"] = "ultra"
        payload = validate_decision(decision)
        self.assertFalse(payload["valid"])
        self.assertTrue(any("modelTier" in issue for issue in payload["issues"]))

    def test_validate_decision_approval_required_without_reason_warns(self) -> None:
        decision = self._decision()
        decision["approvalRequired"] = True
        decision["approvalReason"] = ""
        payload = validate_decision(decision)
        self.assertTrue(any("approvalReason" in warning for warning in payload["warnings"]))

    def test_validate_decision_blocked_without_reason_warns(self) -> None:
        decision = self._decision()
        decision["blocked"] = True
        decision["blockReason"] = ""
        payload = validate_decision(decision)
        self.assertTrue(any("blockReason" in warning for warning in payload["warnings"]))

    def test_validate_decision_non_dict_input(self) -> None:
        payload = validate_decision("bad")  # type: ignore[arg-type]
        self.assertFalse(payload["valid"])
        self.assertTrue(any("object" in issue for issue in payload["issues"]))


class ExplainCoverageTests(unittest.TestCase):
    def _decision(self) -> dict:
        return route_task("Update README wording.", _live_registries())

    def test_explain_returns_string(self) -> None:
        self.assertIsInstance(explain_decision(self._decision()), str)

    def test_explain_contains_decision_id(self) -> None:
        decision = self._decision()
        self.assertIn(decision["decisionId"], explain_decision(decision))

    def test_explain_contains_route_type(self) -> None:
        decision = self._decision()
        self.assertIn(decision["routeType"], explain_decision(decision))

    def test_explain_contains_specialist_and_approval_section(self) -> None:
        text = explain_decision(self._decision())
        self.assertIn("Specialist:", text)
        self.assertIn("## Approval", text)

    def test_explain_blocked_decision_shows_block_reason(self) -> None:
        decision = route_task("Rename one config key in one file.", _live_registries(), {"candidate_models": ["claude-fable-5"]})
        text = explain_decision(decision)
        self.assertIn("BLOCKED:", text)
        self.assertIn(str(decision["blockReason"]), text)


class WorkflowCoverageTests(unittest.TestCase):
    def test_list_workflows_task_class_filter(self) -> None:
        resp = router_gateway({"action": "list_workflows", "params": {"taskClass": "documentation_update"}})
        self.assertFalse(resp["isError"])
        self.assertEqual(resp["structuredContent"]["workflows"][0]["id"], "workflow.docs-sync")

    def test_list_workflows_risk_filter(self) -> None:
        resp = router_gateway({"action": "list_workflows", "params": {"riskLevel": "low"}})
        self.assertFalse(resp["isError"])
        self.assertTrue(all(item["riskDefault"] == "low" for item in resp["structuredContent"]["workflows"]))

    def test_list_workflows_profile_filter(self) -> None:
        resp = router_gateway({"action": "list_workflows", "params": {"profile": "maintenance_work"}})
        self.assertFalse(resp["isError"])
        self.assertTrue(resp["structuredContent"]["count"] >= 1)
        self.assertTrue(all(item["profile"] == "maintenance_work" for item in resp["structuredContent"]["workflows"]))

    def test_get_workflow_by_id(self) -> None:
        resp = router_gateway({"action": "get_workflow", "params": {"name": "workflow.docs-sync"}})
        self.assertFalse(resp["isError"])
        self.assertEqual(resp["structuredContent"]["workflow"]["id"], "workflow.docs-sync")

    def test_get_workflow_by_alias(self) -> None:
        resp = router_gateway({"action": "get_workflow", "params": {"name": "docs-sync"}})
        self.assertFalse(resp["isError"])
        self.assertEqual(resp["structuredContent"]["matchedBy"], "alias")

    def test_get_workflow_unknown(self) -> None:
        resp = router_gateway({"action": "get_workflow", "params": {"name": "missing-workflow"}})
        self.assertTrue(resp["isError"])
        self.assertEqual(resp["structuredContent"]["error"], "unknown_workflow")

    def test_match_workflow_success(self) -> None:
        resp = router_gateway({"action": "match_workflow", "params": {"name": "workflow.docs-sync", "params": {"task_summary": "Update docs"}}})
        self.assertFalse(resp["isError"])
        self.assertTrue(resp["structuredContent"]["matched"])
        self.assertTrue(resp["structuredContent"]["paramsValid"])

    def test_match_workflow_missing_required(self) -> None:
        resp = router_gateway({"action": "match_workflow", "params": {"name": "workflow.docs-sync", "params": {}}})
        self.assertFalse(resp["isError"])
        self.assertTrue(resp["structuredContent"]["matched"])
        self.assertFalse(resp["structuredContent"]["paramsValid"])
        self.assertIn("task_summary", resp["structuredContent"]["missing_required"])

    def test_match_workflow_over_limit(self) -> None:
        resp = router_gateway(
            {"action": "match_workflow", "params": {"name": "workflow.small-refactor", "params": {"task_summary": "x", "target_files": ["a", "b", "c", "d"]}}}
        )
        self.assertFalse(resp["isError"])
        self.assertFalse(resp["structuredContent"]["paramsValid"])

    def test_match_workflow_unknown(self) -> None:
        resp = router_gateway({"action": "match_workflow", "params": {"name": "missing", "params": {"task_summary": "x"}}})
        self.assertFalse(resp["isError"])
        self.assertFalse(resp["structuredContent"]["matched"])

    def test_validate_workflow_params_success(self) -> None:
        resp = router_gateway({"action": "validate_workflow_params", "params": {"name": "workflow.docs-sync", "params": {"task_summary": "Update docs"}}})
        self.assertFalse(resp["isError"])
        self.assertTrue(resp["structuredContent"]["valid"])

    def test_validate_workflow_params_failure(self) -> None:
        resp = router_gateway({"action": "validate_workflow_params", "params": {"name": "workflow.docs-sync", "params": {"target_files": ["README.md"]}}})
        self.assertFalse(resp["isError"])
        self.assertFalse(resp["structuredContent"]["valid"])

    def test_maintenance_workflows_use_maintenance_profile(self) -> None:
        regs = _live_registries()
        items = [workflow for workflow in regs["workflows"] if str(workflow["id"]).startswith("workflow.maintenance-")]
        self.assertTrue(items)
        self.assertTrue(all(item["profile"] == "maintenance_work" for item in items))

    def test_every_registry_workflow_matches_via_match_workflow(self) -> None:
        regs = _live_registries()
        for workflow in regs["workflows"]:
            with self.subTest(workflow=workflow["id"]):
                resp = router_gateway({"action": "match_workflow", "params": {"name": workflow["id"], "params": {"task_summary": workflow["description"]}}})
                self.assertFalse(resp["isError"])
                self.assertTrue(resp["structuredContent"]["matched"])

    def test_log_decision_invalid_decision_rejected(self) -> None:
        bad = self._bad_decision()
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["AGENT_ROUTER_STATE_DIR"] = tmp
            try:
                resp = router_gateway({"action": "log_decision", "params": {"decision": bad}})
                path = Path(tmp) / "route_decisions.jsonl"
            finally:
                del os.environ["AGENT_ROUTER_STATE_DIR"]
        self.assertTrue(resp["isError"])
        self.assertFalse(path.exists())

    def _bad_decision(self) -> dict:
        decision = route_task("Update README wording.", _live_registries())
        decision.pop("decisionId", None)
        return decision


class SchemaAndContractCoverageTests(unittest.TestCase):
    def test_single_tool_named_router(self) -> None:
        import server
        self.assertEqual(len(server.TOOLS), 1)
        self.assertEqual(server.TOOLS[0]["name"], "router")

    def test_schema_no_forbidden_keys_recursive(self) -> None:
        import server
        forbidden = {"minimum", "maximum", "default", "minItems", "maxItems", "minLength", "maxLength", "pattern", "anyOf", "oneOf", "allOf", "not", "const", "format", "examples", "nullable", "$ref"}
        schema = server.TOOLS[0]["inputSchema"]
        schema_text = json.dumps(schema)
        for key in forbidden:
            self.assertNotIn(f'"{key}":', schema_text)

    def test_action_enum_matches_sorted_gateway_actions(self) -> None:
        import server
        enum_values = server.TOOLS[0]["inputSchema"]["properties"]["action"]["enum"]
        self.assertEqual(enum_values, sorted(server.GATEWAY_ACTIONS))

    def test_initialize_reports_server_info(self) -> None:
        import server
        with mock.patch("server.ok") as ok_mock:
            server.handle_request({"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
        payload = ok_mock.call_args[0][1]
        self.assertEqual(payload["serverInfo"]["name"], "router")
        self.assertEqual(payload["serverInfo"]["version"], "0.5.2")

    def test_identical_route_calls_are_deterministic_except_ids(self) -> None:
        first = route_task("Update README wording.", _live_registries())
        second = route_task("Update README wording.", _live_registries())
        for key in ("decisionId", "createdAt"):
            first.pop(key, None)
            second.pop(key, None)
        self.assertEqual(first, second)

    def test_governor_profile_contract_static_list(self) -> None:
        static_profiles = {
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
        workflows = _live_registries()["workflows"]
        self.assertTrue(all(str(workflow["profile"]) in static_profiles for workflow in workflows))

    def test_governor_profile_contract_dynamic_import(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = root / "src" / "agent_governor"
            package.mkdir(parents=True, exist_ok=True)
            (package / "__init__.py").write_text("__all__=[]\n", encoding="utf-8")
            (package / "budget.py").write_text(
                "def list_budget_profiles(overrides=None):\n"
                "    return ['analysis','documentation_write','general_work','small_patch','debug_failure','maintenance_work','data_intake']\n",
                encoding="utf-8",
            )
            old = os.environ.get("AGENT_GOVERNOR_HOME")
            os.environ["AGENT_GOVERNOR_HOME"] = str(root)
            try:
                sys.modules.pop("agent_governor", None)
                sys.modules.pop("agent_governor.budget", None)
                sys.path.insert(0, str(root / "src"))
                from agent_governor.budget import list_budget_profiles  # type: ignore

                live_profiles = set(list_budget_profiles())
            finally:
                sys.path = [item for item in sys.path if item != str(root / "src")]
                if old is None:
                    del os.environ["AGENT_GOVERNOR_HOME"]
                else:
                    os.environ["AGENT_GOVERNOR_HOME"] = old
                sys.modules.pop("agent_governor", None)
                sys.modules.pop("agent_governor.budget", None)
            self.assertIn("maintenance_work", live_profiles)


if __name__ == "__main__":
    unittest.main()
