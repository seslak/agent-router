"""Basic Agent Router usage examples."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from registries import get_registries
from router_core import explain_decision, route_task


def main() -> None:
    regs = get_registries()
    decision = route_task("Update README wording to match current GitHub style.", regs, {"estimated_input_tokens": 30000})
    print("Selected model:", decision["selectedModelId"])
    print("Estimated credits:", decision["estimatedCredits"])
    print("Approval required:", decision["approvalRequired"])
    print("Top ranked models:")
    for item in decision["rankedModels"][:3]:
        print(" ", item["rank"], item["modelId"], item["tier"], item["estimatedCredits"])
    print()
    print(explain_decision(decision))


if __name__ == "__main__":
    main()
