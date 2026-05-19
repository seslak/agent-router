# Changelog

## 0.2.2 - 2026-05-19

Workflow registry alignment patch.

### Added

- Added `workflow.alias-curation` to `routing/workflows.json` so Router surfaces the alias-curation prompt through `list_workflows`, `get_workflow`, and `match_workflow`.
- Added `alias_curation` task class and cost policy for legacy classifier compatibility.
- Expanded the memory-feedback specialist scope to include Mnemo alias proposal curation actions.

### Changed

- Updated workflow library smoke coverage to include `workflow.alias-curation`.

## 0.2.1 - 2026-05-17

Maintenance-profile consistency patch.

### Changed

- Updated maintenance workflow registry alignment to use `profile: maintenance_work` for:
  - `workflow.maintenance-prompt-audit`
  - `workflow.maintenance-schema-audit`
  - `workflow.maintenance-state-cleanup`
  - `workflow.maintenance-mnemo`
  - `workflow.maintenance-thrift-economy`
  - `workflow.maintenance-governor-ledger`
  - `workflow.maintenance-nexus-health`
- Added registry consistency tests for maintenance workflows and known Governor profile names.

## 0.2.0 - 2026-05-17

Workflow registry release. Router is now a workflow prescription provider, with legacy classifier compatibility retained.

### Added

- New workflow-registry actions: `match_workflow`, `get_workflow`, `list_workflows`, `validate_workflow_params`
- Expanded `routing/workflows.json` registry with context, docs, refactor, test triage, and maintenance workflows
- Alias-aware lookup for workflow IDs
- Workflow parameter validation with required/missing/invalid reporting

### Changed

- `doctor` now reports `workflow_count`, `legacy_classifier_available`, and `legacy_classifier_deprecated`
- `route_task` now resolves workflow specialist from `specialistId` or legacy `defaultSpecialist`
- Legacy `route` and `classify` remain available but return explicit deprecation warnings

### Compatibility

- Public MCP surface remains exactly one tool: `router`
- Legacy actions (`route`, `classify`, `validate_decision`, `list_specialists`, `list_models`, `log_decision`, `explain`) are preserved

## 0.1.0 — 2026-05-15

Initial release.

### Added

- Single `router` MCP gateway tool (Copilot-safe schema)
- `doctor` action: version, registry status, counts, warnings
- `classify` action: deterministic keyword-based task classification
- `route` action: full route decision (workflow → specialist → model tier → approval)
- `validate_decision` action: validates required fields and allowed enum values
- `list_workflows` action: registry query with optional task class / risk filters
- `list_specialists` action: registry query with optional tier / tool filters
- `list_models` action: registry query with optional tier / multiplier filters
- `log_decision` action: appends validated decision to JSONL log
- `explain` action: human-readable decision explanation
- `router_core.py`: classification, routing, validation, explanation logic
- `registries.py`: JSON registry loading with in-process cache
- `schemas.py`: shared constants (tier order, route types, risk levels)
- `routing/task-classes.json`: 8 task classes + unknown fallback
- `routing/specialists.json`: 8 built-in specialists
- `routing/models.copilot.json`: 4 example models (gpt-mini, gpt-balanced, claude-sonnet, claude-opus)
- `routing/workflows.json`: 7 workflows
- `routing/policies.json`: default policy, per-task policies, escalation rules, approval rules
- `routing/route-decision.schema.json`: route decision schema reference
- `smoke_test.py`: end-to-end MCP subprocess smoke test
- `test_router.py`: 55 unit tests (stdlib only)
- `docs/routing_model.md`: routing design documentation
- `docs/registries.md`: registry format reference
- `docs/tool_reference.md`: complete action reference

### Design constraints

- Stdlib-only, no external dependencies
- Python 3.9+
- Deterministic routing: no LLM calls, no network requests
- Copilot-safe MCP schema (no `default`, `minimum`, `maximum`, `minItems`, etc.)
- Gateway pattern: one public tool, all operations via `action` + `params`
- Router recommends only; it does not execute work
