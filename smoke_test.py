#!/usr/bin/env python3
"""End-to-end smoke test for the Agent Router MCP server."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "server.py"


def rpc(proc: subprocess.Popen[str], request: dict) -> dict:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError("server closed stdout")
    return json.loads(line)


def call_tool(proc: subprocess.Popen[str], request_id: int, name: str, arguments: dict) -> dict:
    return rpc(
        proc,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )


def call_router(proc: subprocess.Popen[str], request_id: int, action: str, params: dict | None = None) -> dict:
    return call_tool(proc, request_id, "router", {"action": action, "params": params or {}})


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp) / "state" / "router"
        env = dict(os.environ)
        env["AGENT_ROUTER_STATE_DIR"] = str(state_dir)
        env["AGENT_ROUTER_WORKSPACE_ROOT"] = tmp

        proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            # 1. Initialize
            init = rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "clientInfo": {"name": "smoke-test", "version": "1"},
                    },
                },
            )

            # 2. Tools list
            tools = rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

            # 3. Doctor
            doctor = call_router(proc, 3, "doctor")

            # 4. Match workflow (registry-first)
            match_small = call_router(
                proc,
                4,
                "match_workflow",
                {
                    "name": "workflow.small-refactor",
                    "params": {
                        "task_summary": "Rename one config key in inbox/iban.php.",
                        "target_files": ["inbox/iban.php"],
                        "runtime_available": False,
                    },
                },
            )

            # 5. Get workflow by alias
            get_small = call_router(proc, 5, "get_workflow", {"name": "small-refactor"})

            # 6. Validate workflow params
            validate_params = call_router(
                proc,
                6,
                "validate_workflow_params",
                {
                    "name": "workflow.small-refactor",
                    "params": {"task_summary": "Refactor one file."},
                },
            )

            # 7. Route a docs task (legacy compatibility)
            route_docs = call_router(
                proc, 7, "route",
                {"task": "Update README wording to match current GitHub style."},
            )
            docs_decision = route_docs["result"]["structuredContent"]

            # 8. Validate that decision
            validate = call_router(proc, 8, "validate_decision", {"decision": docs_decision})

            # 9. Log that decision
            log = call_router(proc, 9, "log_decision", {"decision": docs_decision})

            # 10. Route a major architecture task
            route_arch = call_router(
                proc, 10, "route",
                {"task": "Design how Mnemo, Thrift, Governor, and Router should coordinate memory, budgeting, and routing across agent teams."},
            )
            arch_decision = route_arch["result"]["structuredContent"]

            # 11. Classify a small edit (legacy compatibility)
            classify = call_router(
                proc, 11, "classify",
                {"task": "Rename this config key and update references in one file."},
            )

            # 12. Explain the arch decision
            explain = call_router(proc, 12, "explain", {"decision": arch_decision})

            # 13. List workflows
            list_wf = call_router(proc, 13, "list_workflows")

            # 14. List specialists
            list_sp = call_router(proc, 14, "list_specialists")

            # 15. List models
            list_mo = call_router(proc, 15, "list_models")

            # 16. Unknown action
            unknown = call_router(proc, 16, "nonexistent_action")

            # 17. Shutdown
            shutdown = rpc(proc, {"jsonrpc": "2.0", "id": 17, "method": "shutdown"})

            # Check log file existence while tempdir is still live
            log_file_exists = (state_dir / "route_decisions.jsonl").exists()

        finally:
            # Some stdio MCP servers acknowledge shutdown but keep the process
            # alive until stdin reaches EOF. Close pipes before waiting so the
            # smoke test cannot hang when run from a larger test wrapper.
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
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

    # Assertions
    assert init["result"]["serverInfo"]["name"] == "router", "serverInfo.name must be 'router'"
    assert init["result"]["serverInfo"]["version"] == "0.2.2", "version must be 0.2.2"

    tool_names = {t["name"] for t in tools["result"]["tools"]}
    assert tool_names == {"router"}, f"Expected exactly {{router}}, got {tool_names}"

    doc_sc = doctor["result"]["structuredContent"]
    assert doc_sc["version"] == "0.2.2"
    assert doc_sc["counts"]["specialists"] > 0
    assert doc_sc["counts"]["workflows"] > 0
    assert doc_sc["counts"]["models"] > 0
    assert doc_sc["legacy_classifier_available"] is True
    assert doc_sc["legacy_classifier_deprecated"] is True
    assert isinstance(doc_sc["warnings"], list)

    # Registry-first workflow APIs
    match_sc = match_small["result"]["structuredContent"]
    assert match_sc["matched"] is True
    assert match_sc["workflowId"] == "workflow.small-refactor"
    assert match_sc["paramsValid"] is True

    get_sc = get_small["result"]["structuredContent"]
    assert get_sc["workflow"]["id"] == "workflow.small-refactor"
    assert get_sc["matchedBy"] == "alias"

    validate_params_sc = validate_params["result"]["structuredContent"]
    assert validate_params_sc["valid"] is True

    # Docs route
    assert docs_decision["taskClass"] == "documentation_update", \
        f"Expected documentation_update, got {docs_decision['taskClass']}"
    assert docs_decision["routeType"] == "WORKFLOW", \
        f"Expected WORKFLOW, got {docs_decision['routeType']}"
    assert docs_decision["workflowId"] == "workflow.docs-sync"
    assert docs_decision["specialistId"] == "doc-writer"
    assert docs_decision["modelTier"] in ("free", "cheap"), \
        f"Expected cheap/free, got {docs_decision['modelTier']}"
    assert docs_decision["approvalRequired"] is False, "docs task must not require approval"
    assert docs_decision["blocked"] is False
    assert docs_decision["deprecated"] is True

    # Validation
    val_sc = validate["result"]["structuredContent"]
    assert val_sc["valid"] is True, f"Decision should be valid, issues: {val_sc.get('issues')}"

    # Log
    log_sc = log["result"]["structuredContent"]
    assert log_sc["logged"] is True
    assert log_sc["path"]
    assert log_file_exists, "JSONL log must be created while server was running"

    # Architecture route
    assert arch_decision["taskClass"] == "major_agentic_architecture", \
        f"Expected major_agentic_architecture, got {arch_decision['taskClass']}"
    assert arch_decision["approvalRequired"] is True, "Architecture task must require approval"
    assert arch_decision["modelTier"] in ("expensive", "blocked"), \
        f"Architecture task must be expensive or blocked"

    # Classify
    cls_sc = classify["result"]["structuredContent"]
    assert cls_sc["taskClass"] == "small_code_edit", \
        f"Expected small_code_edit, got {cls_sc['taskClass']}"
    assert cls_sc["routeType"] == "WORKFLOW"
    assert cls_sc["deprecated"] is True

    # Explain
    assert explain["result"]["isError"] is False
    assert "Route Decision" in explain["result"]["structuredContent"]["explanation"]

    # List responses
    assert list_wf["result"]["structuredContent"]["count"] > 0
    assert list_sp["result"]["structuredContent"]["count"] > 0
    assert list_mo["result"]["structuredContent"]["count"] > 0

    # Unknown action
    assert unknown["result"]["isError"] is True
    assert "unknown_action" in str(unknown["result"]["structuredContent"].get("error", ""))

    assert shutdown["result"] == {}

    print("OK: agent-router smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
