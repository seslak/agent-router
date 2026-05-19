# Agent Router: Registries

Registries are JSON files under `routing/`. They are loaded at startup and cached in memory. To reload, restart the server.

## task-classes.json

Defines how tasks are classified.

Each class entry:

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique class identifier |
| `keywords` | array of strings | Phrase list for keyword matching |
| `routeType` | string | `WORKFLOW`, `SPECIALIST_AGENT`, or `MANUAL_PLAN_FIRST` |
| `defaultRisk` | string | `low`, `medium`, `high` |
| `defaultComplexity` | string | `low`, `medium`, `high` |
| `defaultBlastRadius` | string | `low`, `medium`, `high` |
| `preferredWorkflow` | string or null | Default workflow id for this class |
| `preferredSpecialist` | string or null | Default specialist id for this class |

Built-in classes: `documentation_update`, `small_code_edit`, `test_failure_simple`, `test_failure_repeated`, `cross_package_design`, `major_agentic_architecture`, `memory_feedback`, `document_ingestion`, `unknown`.

The `unknown` class is used as a fallback when no other class matches.

## specialists.json

Defines available specialist agents.

Each specialist entry:

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique specialist identifier |
| `description` | string | What this specialist handles |
| `responsibilities` | array | Tasks this specialist handles |
| `forbiddenResponsibilities` | array | Tasks outside this specialist's scope |
| `defaultTier` | string | Default model tier |
| `allowedTiers` | array | All tiers this specialist may use |
| `maxMultiplier` | number | Max premium request multiplier |
| `allowedTools` | array | Tools available to this specialist |
| `contextPolicy` | string | `minimal`, `bounded`, or `full` |
| `approvalPolicy` | string | `none`, `if_expensive`, or `always` |
| `memoryPolicy` | string | `none`, `recall_startup`, or `recall_full` |

Built-in specialists: `code-editor`, `test-fixer`, `doc-writer`, `tooling-integrator`, `architect`, `deep-architect`, `memory-feedback-writer`, `workflow-router`.

## models.copilot.json

Defines available models.

Each model entry:

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique model identifier |
| `displayName` | string | Human-readable name |
| `vendor` | string | Model vendor |
| `tier` | string | `free`, `cheap`, `balanced`, or `expensive` |
| `premiumRequestMultiplier` | number | Cost multiplier relative to baseline |
| `strengths` | array | What this model is good at |
| `weaknesses` | array | Where this model falls short |
| `requiresApproval` | boolean | Whether this model always requires approval |

Unknown multipliers are treated as `expensive` per default policy.

## workflows.json

Defines repeatable workflows.

Each workflow entry:

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Unique workflow identifier (e.g. `workflow.docs-sync`) |
| `description` | string | What this workflow does |
| `taskClasses` | array | Task class ids this workflow handles |
| `riskLevel` | string | `low`, `medium`, or `high` |
| `defaultSpecialist` | string | Default specialist for this workflow |
| `allowedTools` | array | Tools allowed within this workflow |
| `requiredChecks` | array | Checks required after completion |
| `stopConditions` | array | Conditions that should stop the workflow |
| `promptFile` | string or null | Optional associated prompt file |

Built-in workflows: `workflow.small-refactor`, `workflow.docs-sync`, `workflow.test-failure-triage`, `workflow.memory-feedback`, `workflow.ingest-document`, `workflow.changed-files-review`, `workflow.release-note`.

## policies.json

Defines routing policy.

Key sections:

- `defaultPolicy`: Global defaults (unknown model behavior, approval rules, multiplier cap)
- `taskPolicies`: Per-task-class overrides (max multiplier, preferred specialist, max tier)
- `escalationRules`: Conditions that trigger tier escalation
- `approvalRules`: Conditions that require approval
- `fallbackPolicy`: Behavior when no model or specialist is found

## Extending registries

To add a new specialist, workflow, or model, edit the JSON files in `routing/`. Restart the server to reload.

To add a new task class, add an entry to `task-classes.json` with a unique `id` and relevant keywords. Add a matching entry to `policies.json` under `taskPolicies` if you need non-default multiplier or tier constraints.
