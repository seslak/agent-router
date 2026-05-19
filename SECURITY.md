# Security

Agent Router is a local-first tool. It does not make network requests, store secrets, or connect to external services.

## Scope

- Router reads JSON files from the local `routing/` directory.
- Router writes JSONL decision logs to the configured state directory.
- Router does not read or write user code, credentials, or secrets.
- Router communicates via stdio only.

## Reporting issues

If you discover a security issue, please report it via the project's issue tracker with a `security` label. Do not include credentials or sensitive data in issue reports.

## Trust model

Router is a local process controlled by the user. Registry files and decision logs are stored locally. Do not commit decision logs unless you intend to share routing history.
