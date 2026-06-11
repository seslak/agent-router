# Agent Router: Routing Model

Router ranks models and surfaces workflow policy. It does not execute tasks and it does not choose subagents.

## Decision order

1. Classify the task deterministically from `task-classes.json`
2. Resolve workflow first when the class is workflow-backed
3. Resolve specialist policy second
4. Estimate AI credits from the named cost profile
5. Filter to policy-compliant models and return the full ranked list

## Pricing

- Pricing is AI-credit based, not multiplier based
- Long-context pricing is applied only when `estimated_input_tokens` exceeds the model threshold
- Subagents cannot request extended context directly; the router only models the automatic billing row switch
- Reasoning effort is intentionally not modeled here

Reserved extension point: an optional future `effortMultiplier` on cost profiles if Copilot later exposes per-dispatch reasoning control.

## Ranked list contract

`rankedModels` is the primary output:

- sorted by effective tier, then estimated credits, then model id
- capped at 20 entries
- every entry carries its own approval evaluation

The orchestrating LLM chooses the subagent contextually, then dispatches it with the first ranked model present in that subagent's frontmatter.

## Thrift and Governor

- Thrift may provide `estimated_input_tokens`
- Router may provide `governorStartHint`
- `AGENT_SUITE_SESSION_ID` is stamped into decision logs and outcomes for cross-suite correlation
