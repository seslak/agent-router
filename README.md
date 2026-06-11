# Agent Router

Local-first workflow and model-ranking router for MCP-capable coding agents.

Agent Router is a small stdio MCP server that answers "what should I do next and how much model budget is justified?" before implementation starts. It classifies tasks, suggests repeatable workflows, returns a ranked list of policy-compliant models, reports approval requirements, and logs routing outcomes.

> Router recommends. It does not execute.

## Status

Current version: **0.5.2**

Runtime requirements:

- Python **3.9+**
- Standard library only
- No external dependencies
- No cloud service
- No database server
- No package install required

## Why workflow-first routing matters

Without routing, agents tend to improvise: they pick a model tier based on context length, choose specialists arbitrarily, and skip stop conditions. This leads to silent cost escalation, unnecessary use of expensive models for trivial tasks, and architecture decisions slipping into code-edit sessions.

Router enforces a decision layer before any work starts:

1. **Workflow first** — if the task matches a known, repeatable workflow, use it
2. **Specialist context second** — if no workflow matches, select the appropriate specialist route
3. **Ranked model list third** — estimate AI-credit cost and rank viable models by policy
4. **Approval before expensive** — gate expensive routes behind explicit approval conditions
5. **Outcome logging after dispatch** — record what was actually selected so routing quality can be reviewed

## Core contract

The recommended production call is `suggest_workflow`:

1. Call `suggest_workflow` once per task.
2. Router returns workflow candidates plus `decision.rankedModels`.
3. The orchestrating LLM chooses the subagent contextually.
4. Dispatch that subagent with the first `rankedModels` entry present in the subagent frontmatter model list.
5. If that entry has `approvalRequired: true`, obtain approval first.
6. Report the actual choice with `log_outcome` using `selectedModelId` and `selectionRank`.

`route` and `classify` remain available as legacy compatibility actions and are planned for removal before 1.0.

## Specialist and model are separate

Specialist identity and model selection are decoupled by design. A specialist defines what judgment is needed. A model defines how much cost is justified. Routing one does not constrain the other independently — both are resolved separately against policy.

This separation prevents "picking an expensive model means picking a capable specialist" conflation, and prevents "choosing a cheap model means I am locked to a small specialist scope."

## Pricing model

Router uses GitHub Copilot's AI-credit pricing model, not premium-request multipliers.

- 1 credit = $0.01 USD
- Costs are estimated from per-1M-token input, cached-input, output, and optional Anthropic cache-write prices
- Long-context pricing applies only when the caller provides `estimated_input_tokens` above a model threshold
- Routing defaults to the model's `default` pricing row otherwise

Task policies choose a cost profile (`small`, `medium`, `large`) and a credit ceiling. Router ranks viable models by:

1. effective credit tier
2. estimated credits
3. model id

Multiplier-era fields are deprecated. `maxAllowedMultiplier` is retained as `null` for one release as a compatibility field.

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
{"action":"suggest_workflow","params":{"task":"Update README wording to match existing GitHub style.","estimated_input_tokens":12000}}
```

This keeps the MCP surface minimal for clients with tool-inventory limits.

## Main actions

- `doctor`
- `reload_registries`
- `validate_registries`
- `suggest_workflow`
- `match_workflow`
- `get_workflow`
- `list_workflows`
- `validate_workflow_params`
- `classify` legacy
- `route` legacy
- `validate_decision`
- `list_specialists`
- `list_models`
- `log_decision`
- `log_outcome`
- `recent_decisions`
- `explain`

## Important params

- `estimated_input_tokens` — long-context-aware pricing input, usually supplied by Thrift.
- `estimated_output_tokens` — optional override for the policy cost profile's output-token assumption.
- `candidate_models` — optional list of dispatchable model ids. Router constrains ranking to that set and reports when the ideal model exists outside it.

## Example calls

### Suggest a workflow

```json
{"action":"suggest_workflow","params":{"task":"Refactor the Router README and run tests.","estimated_input_tokens":16000,"candidate_models":["gpt-5-mini","gpt-5-thinking","claude-sonnet-4"]}}
```

Returns workflow candidates and a decision containing `rankedModels`, approval flags, candidate-model constraint information, and an optional Governor start hint.

### Match a workflow directly

```json
{"action":"match_workflow","params":{"name":"workflow.docs-sync","params":{"task_summary":"Update README wording to match existing GitHub style.","target_files":["README.md"]}}}
```

Returns a workflow prescription: profile, specialist, model tier, allowed tools, checks, and parameter validation.

### Log the actual model outcome

```json
{"action":"log_outcome","params":{"decision_id":"rd_abc123def456","selectedModelId":"gpt-5-mini","selectionRank":1,"notes":"Used first dispatchable ranked model."}}
```

Appends an outcome row to the route decision log.

### Read recent decisions

```json
{"action":"recent_decisions","params":{"limit":10}}
```

Returns recent decision rows, including matching outcome rows when available. Rotated decision logs are included.

### Legacy route/classify

```json
{"action":"route","params":{"task":"Update README wording to match existing GitHub style."}}
```

Legacy actions remain available for backward compatibility but are deprecated for production workflows.

## Route decision highlights

The selected model remains available in scalar fields for compatibility, but the primary output is `rankedModels`.

Key fields:

- `decisionId`
- `createdAt`
- `taskClass`
- `routeType`
- `workflowId`
- `specialistId`
- `selectedModelId`
- `modelTier`
- `estimatedCredits`
- `pricingApplied`
- `approvalRequired`
- `approvalConditions`
- `rankedModels`
- `candidateConstraint`
- `governorStartHint`
- `matchedSignals`

## Relationship to the suite

- Thrift can supply `estimated_input_tokens` from `plan_context.total_planned_tokens` or `count_tokens.tokens`.
- Router returns `rankedModels` and optional `governorStartHint`.
- Governor can consume `governorStartHint.params.profile`.
- Router logs decisions and outcomes using the shared `AGENT_SUITE_SESSION_ID` convention.

## State and environment

Router writes decision logs under `AGENT_ROUTER_STATE_DIR` when set, otherwise under `state/router` relative to the workspace.

Useful environment variables:

- `AGENT_ROUTER_STATE_DIR` — location for Router state and route decision logs
- `AGENT_ROUTER_WORKSPACE_ROOT` — workspace root used for default state placement
- `AGENT_SUITE_SESSION_ID` — shared suite session id used for correlation across components

## Repository layout

```text
server.py                         MCP gateway server
router_core.py                    deterministic classification/routing logic
pricing.py                        AI-credit estimation helpers
registries.py                     registry loading/cache helpers
schemas.py                        shared constants and validation helpers
routing/*.json                    task, workflow, specialist, model, policy registries
docs/routing_model.md             routing design documentation
docs/registries.md                registry format reference
docs/tool_reference.md            complete action reference
smoke_test.py                     subprocess smoke test
test_router.py                    router unit tests
test_pricing.py                   pricing unit tests
```

## Validation

```bash
python -m compileall -q .
python smoke_test.py
python -m unittest discover -s . -p "test*.py"
```

Current root suite: **81 tests**.

## Design constraints

- Stdlib-only, no external dependencies
- Deterministic routing: no LLM calls and no network requests
- Copilot-safe MCP schema
- Gateway pattern: one MCP tool with action dispatch
- Registry-driven task, workflow, specialist, model, and policy behavior
- No automatic execution or model switching
