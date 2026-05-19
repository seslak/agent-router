# Contributing

## Development setup

No dependencies to install. Python 3.9+ required.

```bash
python -m compileall -q .
python smoke_test.py
python -m unittest discover -s . -p "test*.py"
```

## Adding a task class

1. Add an entry to `routing/task-classes.json` with unique `id` and keywords.
2. Add a matching entry to `routing/policies.json` under `taskPolicies`.
3. Optionally add a new workflow to `routing/workflows.json`.
4. Add tests in `test_router.py` covering the new class.

## Adding a specialist

Edit `routing/specialists.json`. Add at least: `id`, `description`, `defaultTier`, `allowedTiers`, `maxMultiplier`, `allowedTools`, `contextPolicy`, `approvalPolicy`, `memoryPolicy`.

## Adding a model

Edit `routing/models.copilot.json`. Every model must have a `premiumRequestMultiplier`. Models with unknown multipliers will be blocked or treated as expensive per default policy.

## Schema constraints

The `router` tool's `inputSchema` must remain Copilot-safe. Do not add `default`, `minimum`, `maximum`, `minItems`, `maxItems`, `minLength`, `maxLength`, `pattern`, `oneOf`, `anyOf`, `allOf`, or type arrays to the schema.

## Test requirements

All tests must pass before merging. Use stdlib only (`unittest`). No pytest required.

## Design principles

- Router decides; it does not execute.
- Workflow first, specialist second, model third.
- Cheapest capable model within policy bounds.
- Approval required for expensive routes.
- All routing must be deterministic and explainable.
