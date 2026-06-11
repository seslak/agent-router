# Agent Router Registries

Registries live under `routing/` and are reloaded when their mtimes change or when `reload_registries` is called.

## `models.copilot.json`

Schema version 2.

- `schemaVersion`
- `asOf`
- `creditUsd`
- `models[]`

Each model carries:

- `id`
- `displayName`
- `vendor`
- `category`
- `releaseStatus`
- `pricing.default`
- optional `pricing.longContext`
- optional Anthropic `cacheWrite`
- `requiresApproval`

## `policies.json`

Schema version 2.

- `defaultPolicy`
- `costProfiles`
- `tierBands`
- `taskPolicies`
- `approvalRules`
- `fallbackPolicy`

## `specialists.json`

Specialists now use `maxCredits`, not multiplier caps. `domains` is used by `list_specialists(domain=...)`.

## `workflows.json`

Workflows may add:

- `profile`
- `specialistId`
- `modelTier`
- `maxCredits`
- `requiredChecks`

## `task-classes.json`

Task classes support:

- `keywords`
- optional `priorityPhrases`
- `preferredWorkflow`
- `preferredSpecialist`
