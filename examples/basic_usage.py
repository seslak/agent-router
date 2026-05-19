"""Basic Agent Router usage examples (direct Python API, no MCP required)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from registries import get_registries, invalidate_cache
from router_core import classify_task, route_task, validate_decision, explain_decision


def main() -> None:
    regs = get_registries()

    # --- Classify a task ---
    task = "Update README wording to match existing GitHub style."
    classification = classify_task(task, regs["task_classes"])
    print("=== classify ===")
    print(f"Task class:    {classification['taskClass']}")
    print(f"Route type:    {classification['routeType']}")
    print(f"Risk:          {classification['riskLevel']}")
    print(f"Signals:       {', '.join(classification['matchedSignals'])}")
    print()

    # --- Route a task ---
    decision = route_task(task, regs)
    print("=== route ===")
    print(f"Decision ID:   {decision['decisionId']}")
    print(f"Task class:    {decision['taskClass']}")
    print(f"Route type:    {decision['routeType']}")
    print(f"Workflow:      {decision['workflowId']}")
    print(f"Specialist:    {decision['specialistId']}")
    print(f"Model tier:    {decision['modelTier']}")
    print(f"Model:         {decision['selectedModelId']}")
    print(f"Approval:      {decision['approvalRequired']}")
    print(f"Blocked:       {decision['blocked']}")
    print()

    # --- Validate a decision ---
    validation = validate_decision(decision)
    print("=== validate_decision ===")
    print(f"Valid:   {validation['valid']}")
    print(f"Issues:  {validation['issues']}")
    print(f"Warns:   {validation['warnings']}")
    print()

    # --- Explain a decision ---
    explanation = explain_decision(decision)
    print("=== explain ===")
    print(explanation)
    print()

    # --- Architecture task (expensive + approval) ---
    arch_task = (
        "Design how Mnemo, Thrift, Governor, and Router should coordinate "
        "memory, budgeting, and routing across agent teams."
    )
    arch_decision = route_task(arch_task, regs)
    print("=== architecture task ===")
    print(f"Task class:      {arch_decision['taskClass']}")
    print(f"Specialist:      {arch_decision['specialistId']}")
    print(f"Model tier:      {arch_decision['modelTier']}")
    print(f"Approval req'd:  {arch_decision['approvalRequired']}")
    if arch_decision.get("approvalReason"):
        print(f"Approval reason: {arch_decision['approvalReason']}")


if __name__ == "__main__":
    main()
