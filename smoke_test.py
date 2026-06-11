#!/usr/bin/env python3
"""Smoke test for the Agent Router MCP server."""

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
    return rpc(proc, {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}})


def call_router(proc: subprocess.Popen[str], request_id: int, action: str, params: dict | None = None) -> dict:
    return call_tool(proc, request_id, "router", {"action": action, "params": params or {}})


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        state_dir = Path(tmp) / "state" / "router"
        env = dict(os.environ)
        env["AGENT_ROUTER_STATE_DIR"] = str(state_dir)
        env["AGENT_ROUTER_WORKSPACE_ROOT"] = tmp
        env["AGENT_SUITE_SESSION_ID"] = "smoke-suite"
        proc = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        try:
            init = rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            tools = rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            doctor = call_router(proc, 3, "doctor")
            suggest = call_router(proc, 4, "suggest_workflow", {"task": "Update README wording to match current GitHub style."})
            route = call_router(proc, 5, "route", {"task": "Design how Mnemo, Thrift, Governor, and Router should coordinate memory and routing.", "estimated_input_tokens": 300000})
            validate = call_router(proc, 6, "validate_registries")
            decision = suggest["result"]["structuredContent"]["decision"]
            log = call_router(proc, 7, "log_decision", {"decision": decision})
            outcome = call_router(proc, 8, "log_outcome", {"decisionId": decision["decisionId"], "outcome": "followed", "selectedModelId": decision["selectedModelId"], "selectionRank": 1})
            recent = call_router(proc, 9, "recent_decisions", {"limit": 10})
            shutdown = rpc(proc, {"jsonrpc": "2.0", "id": 10, "method": "shutdown"})
        finally:
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

    assert init["result"]["serverInfo"]["name"] == "router"
    assert init["result"]["serverInfo"]["version"] == "0.5.2"
    assert len(tools["result"]["tools"]) == 1
    assert doctor["result"]["structuredContent"]["validation"]["valid"] is True
    assert suggest["result"]["structuredContent"]["topWorkflowId"] == "workflow.docs-sync"
    route_sc = route["result"]["structuredContent"]
    assert "matchedSignals" in route_sc
    assert "rankedModels" in route_sc
    assert validate["result"]["structuredContent"]["valid"] is True
    assert log["result"]["structuredContent"]["logged"] is True
    assert outcome["result"]["structuredContent"]["logged"] is True
    assert recent["result"]["structuredContent"]["count"] >= 1
    assert shutdown["result"] == {}
    print("OK: agent-router smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
