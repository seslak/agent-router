# Agent Router

Local-first workflow/specialist/model-tier router for MCP-capable coding agents.

Agent Router is a small stdio MCP server that answers "what should I do next and how?" before implementing any task. It classifies tasks, selects a workflow or specialist, recommends a model tier, determines whether approval is required, and logs the decision.

> Router recommends. It does not execute.

## Status

Current version: **0.2.2**

Runtime requirements:

- Python **3.9+**
- Standard library only
- No external dependencies
- No cloud service, no database server, no package install

## Why workflow-first routing matters

Without routing, agents tend to improvise: they pick a model tier based on context length, choose specialists arbitrarily, and skip stop conditions. This leads to silent cost escalation, unnecessary use of expensive models for trivial tasks, and architecture decisions slipping into code-edit sessions.

Router enforces a decision layer before any work starts:

1. **Workflow first** — if the task matches a known, repeatable workflow, use it
2. **Specialist second** — if no workflow matches, select the appropriate specialist
3. **Model/cost tier third** — choose the cheapest capable model within policy bounds
4. **Approval before expensive** — gate expensive routes behind explicit approval

## Specialist and model are separate

Specialist identity and model selection are decoupled by design. A specialist defines what judgment is needed. A model defines how much cost is justified. Routing one does not constrain the other independently — both are resolved separately against policy.

This separation prevents "picking an expensive model means picking a capable specialist" conflation, and prevents "choosing a cheap model means I'm locked to a small specialist scope."

## Quick start

Point your MCP client at `server.py`:

```json
{
  "servers": {
    "router": {
      "type": "stdio",
      "command": "python",
      "args": ["path/to/agent-router/server.py"],
      "env": {
        "AGENT_ROUTER_STATE_DIR": "path/to/state/router",
        "AGENT_ROUTER_WORKSPACE_ROOT": "path/to/workspace"
      }
    }
  }
}
```

See `examples/mcp.vscode.json` for a VS Code example.

## Gateway MCP model

Router exposes exactly one public MCP tool:

```text
router
```

Call it with an `action` and optional `params` object:

```json
{"action":"match_workflow","params":{"name":"workflow.small-refactor","params":{"task_summary":"Update README wording.","target_files":["README.md"],"runtime_available":false}}}
```

This keeps the MCP surface minimal for clients with tool-inventory limits.

## Example calls

### Classify a task

```json
{"action":"classify","params":{"task":"Update README wording to match existing GitHub style."}}
```

Returns: task class, route type, risk, complexity, blast radius, matched signals.

### Match a workflow (production)

```json
{"action":"match_workflow","params":{"name":"workflow.docs-sync","params":{"task_summary":"Update README wording to match existing GitHub style.","target_files":["README.md"]}}}
```

Returns a workflow prescription: profile, specialist, model tier, allowed tools, checks, and parameter validation.

### Legacy route/classify (deprecated)

```json
{"action":"route","params":{"task":"Update README wording to match existing GitHub style."}}
```

This remains available for backward compatibility but is deprecated for production workflows.

### Validate a decision

```json
{"action":"validate_decision","params":{"decision":{...}}}
```

### Log a decision

```json
{"action":"log_decision","params":{"decision":{...}}}
```

Appends to `state/router/route_decisions.jsonl`.

### Explain a decision

```json
{"action":"explain","params":{"decision":{...}}}
```

Returns human-readable explanation of routing rationale.

## Route decision shape

```json
{
  "decisionId": "rd_abc123def456",
  "createdAt": "2026-05-15T10:00:00Z",
  "task": "Update README wording.",
  "taskClass": "documentation_update",
  "routeType": "WORKFLOW",
  "workflowId": "workflow.docs-sync",
  "specialistId": "doc-writer",
  "modelTier": "cheap",
  "selectedModelId": "gpt-mini",
  "maxAllowedMultiplier": 1.0,
  "estimatedCostClass": "cheap",
  "approvalRequired": false,
  "approvalReason": null,
  "riskLevel": "low",
  "complexity": "low",
  "blastRadius": "low",
  "allowedTools": ["edit", "create", "read"],
  "requiredMemory": [],
  "requiredChecks": [],
  "reason": "Task classified as 'documentation_update' (signals: readme). Route: WORKFLOW. Specialist: doc-writer. Model: gpt-mini (cheap).",
  "fallbackUsed": false,
  "blocked": false,
  "blockReason": null,
  "nextStep": "Use workflow 'workflow.docs-sync'. Assign specialist 'doc-writer'."
}
```

## Registries

Routing behavior is configured through JSON files in `routing/`:

| File | Purpose |
|------|---------|
| `task-classes.json` | Task classification rules and keywords |
| `specialists.json` | Specialist definitions and tier constraints |
| `models.copilot.json` | Model registry with tiers and multipliers |
| `workflows.json` | Workflow definitions and stop conditions |
| `policies.json` | Routing policy (approval rules, fallbacks, tier caps) |
| `route-decision.schema.json` | Route decision schema reference |

## Cost-aware model tiering

Model tiers: `free` → `cheap` → `balanced` → `expensive` → `blocked`

Router always selects the cheapest capable model within the allowed range. Tier bounds come from:
1. Specialist's `allowedTiers` and `maxMultiplier`
2. Task policy's `maxTier` from `policies.json`

The cheapest model satisfying both constraints is selected.

## Approval rules

`approvalRequired: true` is set when:
- The selected model's tier is `expensive`
- The selected model has `requiresApproval: true`
- The specialist's `approvalPolicy` is `always` or `if_expensive`
- The task policy's escalation rules trigger

Unknown model multipliers are treated as expensive or blocked per `defaultPolicy.unknownModelBehavior`.

## Validation commands

```bash
python -m compileall -q .
python smoke_test.py
python -m unittest discover -s . -p "test*.py"
```

Expected test count: 55 tests passed.

## Relationship to other local MCP servers

| Server | Role |
|--------|------|
| Mnemo | Project memory (store, search, recall) |
| Thrift | Context economy (file windows, token counts, cost telemetry) |
| Governor | Run discipline (lifecycle, patch checks, budget) |
| **Router** | Workflow/specialist/model-tier decision policy |

Router is a decision layer. It recommends which workflow, specialist, and model tier to use. The host environment and user remain responsible for model selection and execution.

## Non-goals

Router v0.2.2 does not:

- Execute tasks
- Switch models automatically
- Spawn subagents
- Call Mnemo, Thrift, or Governor directly
- Perform IDF/embedding/LSH learning
- Provide cost billing integration
- Expose a UI or dashboard

## License

MIT.


## Repeated-failure tier floor

Task policies may define `minTier` when the cheapest model is not appropriate even though it is technically allowed by the specialist. The built-in `test_failure_repeated` policy uses `minTier: balanced` so repeated or deeply diagnosed test failures do not stay on the cheap tier.
