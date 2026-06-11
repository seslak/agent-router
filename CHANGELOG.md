# Changelog

## 0.5.2 - 2026-06-10

Patch-hardening release.

### Changed

- C5/D5/D6: corrected the missing-specialist fallback sentinel to `architect`, validated that fallback against registered specialists, taught recent-decision reads to include rotated logs, and hardened registry cache invalidation with `(mtime, size)` signatures.

## 0.5.1 - 2026-06-10

Tests restoration release.

### Added

- Restored workflow/classification/decision/explain/schema/profile-contract coverage after the 0.3.0-0.5.0 redesign.
- Expanded the router suite from 29 to 79 tests, including the Governor profile contract checks.

## 0.5.0 - 2026-06-10

Suite integration release.

### Added

- Added `governorStartHint` to route and suggest-workflow decisions when a workflow profile is known.
- Added `estimated_output_tokens` support for output-side credit overrides.
- Hardened the Governor profile contract flow for future live-profile integration.

## 0.4.0 - 2026-06-10

Routing-loop closure release.

### Added

- Added `suggest_workflow` as the supported text-to-workflow API.
- Added registry-driven priority phrases in `task-classes.json`.
- Added `log_outcome` and `recent_decisions` so routing choices can be measured after dispatch.

### Changed

- `classify` and `route` are now explicitly legacy compatibility actions with removal planned before 1.0.

## 0.3.0 - 2026-06-10

Credit-based pricing redesign.

### Changed

- Replaced premium-request-multiplier routing with AI-credit estimation and tier bands.
- Migrated `models.copilot.json` and `policies.json` to schema version 2.
- Replaced workflow and specialist `maxMultiplier` limits with `maxCredits`.
- Added full ranked-model output with per-entry approval evaluation.

### Fixed

- Fixed the selection-collapse path around missing pricing data.
- Fixed approval decisions to use effective routed tier and applied pricing, not raw registry fields.
- Fixed domain filtering for `list_specialists`.
- Fixed classifier keyword matching to use word boundaries.
- Fixed registry cache invalidation to reload on mtime changes.
- Fixed route decisions to always include `matchedSignals`, including blocked routes.

### Removed

- Removed multiplier-era config keys and `escalationRules`.
- Multiplier migration note: old caps map approximately as `1 -> 5 credits`, `2 -> 15 credits`, `3 -> 25 credits`, `15 -> 100 credits`.
- `escalationRules` is reserved for a future Governor-driven failure-signal integration.
