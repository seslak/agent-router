# Agent Router: Routing Model

## Core principle

Router v0.1.0 decides. It does not execute.

Every route decision is a recommendation that the caller may accept, modify, or reject. Router never starts work, changes files, or spawns agents.

## Decision order

### 1. Workflow first

If the task matches a known, repeatable workflow, use it. Workflows encode safe, bounded steps that have been validated for their task class.

Workflow selection takes priority over specialist selection because workflows contain explicit stop conditions and required checks. This prevents open-ended specialist improvisation on routine tasks.

### 2. Specialist second

If no workflow matches, or the route type is `SPECIALIST_AGENT`, select the appropriate specialist. Specialists bring judgment to tasks that cannot be reduced to a fixed workflow.

Specialist selection is based on:
- The task class from classification
- The task policy from `policies.json`
- Workflow default specialist (for workflow routes)

### 3. Model/cost tier third

After specialist is resolved, determine the allowed model tier. The specialist's `allowedTiers` and `maxMultiplier` combined with the task policy's `maxTier` define the allowed range.

Router always selects the cheapest capable model within the allowed range. Expensive models require explicit approval.

### 4. Approval before expensive

Routes that reach the `expensive` tier are blocked behind an approval gate. Router sets `approvalRequired: true` and provides an `approvalReason`. The caller must obtain approval before proceeding.

Unknown model multipliers are treated as expensive (or blocked) per policy. This prevents silent cost escalation from misconfigured registries.

### 5. Explain every decision

Every route decision includes:
- `reason`: machine-readable rationale
- `nextStep`: plain-language next action
- `matchedSignals`: which keywords triggered classification

Use the `explain` action for a full human-readable breakdown.

## Route types

| Type | When used |
|------|-----------|
| `WORKFLOW` | Task matches a known repeatable workflow |
| `SPECIALIST_AGENT` | Task needs specialist judgment, no fixed workflow |
| `MANUAL_PLAN_FIRST` | High-risk, high-complexity, or high-blast-radius tasks |

## Classification

Classification is deterministic keyword/rule-based. No LLM calls. Keywords from `routing/task-classes.json` are matched against the normalized task text. The class with the highest phrase-weighted score wins.

Unknown tasks fall back to the `unknown` class, which routes to the `architect` specialist.

## What Router does not do

- Router does not execute tasks
- Router does not call other MCPs
- Router does not switch models automatically
- Router does not spawn subagents
- Router does not modify memory or budgets
- Router does not make binding decisions — only recommendations
