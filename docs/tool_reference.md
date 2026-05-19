# Agent Router Tool Reference

Router exposes one public MCP gateway tool:

```text
router
```

Production workflow calls should use this shape:

```json
{"action":"match_workflow","params":{"name":"workflow.small-refactor","params":{"task_summary":"Update README wording.","target_files":["README.md"]}}}
```

`action` is required. `params` is optional and defaults to an empty object.

## Actions

### `doctor`

Returns server, registry, and state diagnostics.

```json
{"action":"doctor"}
```

Returns: version, package path, routing dir, registry counts, decision log path, `workflow_count`, `legacy_classifier_available`, `legacy_classifier_deprecated`, warnings.

---

### `match_workflow`

Matches a workflow by ID or alias and returns a workflow prescription with parameter validation.

```json
{"action":"match_workflow","params":{"name":"workflow.small-refactor","params":{"task_summary":"Update README wording.","target_files":["README.md"]}}}
```

Returns: matched, workflowId, profile, specialistId, modelTier, allowedTools, requiredChecks, params, paramsValid, missing_required, invalid_fields, warnings.

---

### `get_workflow`

Returns a full workflow definition by ID or alias.

```json
{"action":"get_workflow","params":{"name":"small-refactor"}}
```

Returns: found, matchedBy, workflow.

---

### `validate_workflow_params`

Validates parameters for a workflow against `paramSchema` and workflow constraints.

```json
{"action":"validate_workflow_params","params":{"name":"workflow.small-refactor","params":{"task_summary":"Update README wording."}}}
```

Returns: valid, workflowId, matchedBy, missing_required, invalid_fields, warnings.

---

### `classify`

Classifies a task using deterministic keyword rules. Deprecated for production workflows.

```json
{"action":"classify","params":{"task":"Update README wording."}}
```

Params:
- `task` (required): task description string

Returns: taskClass, routeType, riskLevel, complexity, blastRadius, reason, matchedSignals, suggestedHandler, plus deprecation metadata.

---

### `route`

Produces a full route decision for a task. Deprecated for production workflows.

```json
{"action":"route","params":{"task":"Update README wording."}}
```

Params:
- `task` (required): task description string
- `mode` (optional): `recommend_only` (default behavior — Router never executes)

Returns full route decision with: decisionId, createdAt, task, taskClass, routeType, workflowId, specialistId, modelTier, selectedModelId, maxAllowedMultiplier, estimatedCostClass, approvalRequired, approvalReason, riskLevel, complexity, blastRadius, allowedTools, requiredMemory, requiredChecks, reason, fallbackUsed, blocked, blockReason, nextStep.

---

### `validate_decision`

Validates a route decision object. Checks required fields and allowed enum values.

```json
{"action":"validate_decision","params":{"decision":{...}}}
```

Params:
- `decision` (required): route decision object

Returns: valid (boolean), issues (array of error strings), warnings (array of warning strings).

---

### `list_workflows`

Returns the workflows registry, with optional filtering.

```json
{"action":"list_workflows","params":{"taskClass":"documentation_update"}}
```

Optional filter params:
- `taskClass`: filter by task class id
- `riskLevel`: filter by risk level (`low`, `medium`, `high`)

Returns: workflows (array), count.

---

### `list_specialists`

Returns the specialists registry, with optional filtering.

```json
{"action":"list_specialists","params":{"allowedTier":"cheap"}}
```

Optional filter params:
- `allowedTier`: filter to specialists that allow this tier
- `tool`: filter to specialists that allow this tool

Returns: specialists (array), count.

---

### `list_models`

Returns the models registry, with optional filtering.

```json
{"action":"list_models","params":{"tier":"cheap"}}
```

Optional filter params:
- `tier`: filter by tier (`free`, `cheap`, `balanced`, `expensive`)
- `maxMultiplier`: filter to models with multiplier at or below this value

Returns: models (array), count.

---

### `log_decision`

Appends a route decision to the local JSONL log.

```json
{"action":"log_decision","params":{"decision":{...}}}
```

Params:
- `decision` (required): route decision object

Decision is validated before logging. Returns error if validation fails.

Log path: `AGENT_ROUTER_STATE_DIR/route_decisions.jsonl` (default: `state/router/route_decisions.jsonl`).

Returns: logged (boolean), path, decisionId.

---

### `explain`

Returns a human-readable explanation of a route decision.

```json
{"action":"explain","params":{"decision":{...}}}
```

Params:
- `decision` (required): route decision object

Returns: explanation (string), decisionId.

---

## Error responses

Unknown actions return a structured error:

```json
{
  "error": "unknown_action",
  "message": "Unknown Router action: foo",
  "action": "foo",
  "available_actions": [...]
}
```

All errors have `"isError": true` in the MCP response.

## Schema compatibility

The `router` tool schema avoids unsupported JSON Schema features. Supported keywords only: `type`, `properties`, `required`, `additionalProperties`, `description`, `enum`, `items`. No `default`, `minimum`, `maximum`, `minItems`, `maxItems`, `pattern`, `oneOf`, `anyOf`, or type arrays.
