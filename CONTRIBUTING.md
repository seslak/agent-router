# Contributing

## Development setup

```bash
python -m compileall -q .
python smoke_test.py
python -m unittest discover -s . -p "test*.py"
```

## Registry rules

- `routing/models.copilot.json` must use schema version 2 and parseable pricing rows
- `routing/policies.json` must use schema version 2
- specialists and workflows use `maxCredits`
- task classes may define `priorityPhrases`

## Design rules

- Stdlib only
- Deterministic routing only
- One public MCP tool: `router`
- The tool schema must remain Copilot-safe
- Router ranks models; it does not choose subagents or execute work
