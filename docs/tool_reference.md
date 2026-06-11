# Agent Router Tool Reference

Router exposes one public MCP gateway tool:

```text
router
```

## Recommended action

Use `suggest_workflow` for new integrations:

```json
{"action":"suggest_workflow","params":{"task":"Update README wording.","estimated_input_tokens":30000}}
```

It returns:

- `classified`
- `candidates`
- `topWorkflowId`
- `fallback`
- `decision`

The embedded `decision` includes the full `rankedModels` list.

## Registry actions

- `doctor`
- `reload_registries`
- `validate_registries`
- `list_workflows`
- `get_workflow`
- `validate_workflow_params`
- `list_specialists`
- `list_models`

`list_models` supports `maxCredits` and estimates credits using the default `medium` cost profile.

## Routing actions

- `suggest_workflow`
- `match_workflow`
- `route` (legacy)
- `classify` (legacy)
- `validate_decision`
- `explain`

## Logging actions

- `log_decision`
- `log_outcome`
- `recent_decisions`

Decision logs are written to `AGENT_ROUTER_STATE_DIR/route_decisions.jsonl` and rotate at 10 MB to `route_decisions.1.jsonl`.

## Schema note

The `router` tool schema remains Copilot-safe and uses only supported JSON Schema keywords.
