# Contributing

## Setup

```bash
uv sync --extra dev
uv run ruff check src tests
uv run pytest -q
uv run forge doctor --json
```

Python 3.11+ is required. Windows PowerShell and Linux/WSL are first-class; use
`uv run` so the same locked environment is used.

## Verification-first rules

- Never make a failing test pass by deleting it, weakening assertions or replacing it
  with an unrelated stub.
- Never report an agent's self-report or `agent_settled` event as verification.
- New gate types need trusted evidence, scope, environment binding and tests for STALE.
- External services/hardware that are unavailable must be reported `BLOCKED`/`NOT_RUN`;
  add a credential-independent contract test or fixture instead of a false PASS.
- Do not add automatic remote push/deploy, public bind-defaults or secret logging.
- Preserve user modifications; new runtime state belongs under `.forge/`.

## Pull requests

Include: problem, design, tests run with actual exit codes, evidence paths, risk and
rollback. If a benchmark is cited, include raw JSONL and the manifest; do not compare a
Mock agent against a real LLM as if it were a model-quality result.

## Code layout

- `src/forgeflow/domain` — schemas/enums
- `src/forgeflow/state` — SQLite/event store
- `src/forgeflow/runner` — controlled process runner
- `src/forgeflow/verification` — evidence, gates, clean-room, reviewer
- `src/forgeflow/agents` — mock/Pi/Claude/MCP/ACP boundaries
- `src/forgeflow/context` — Tree-sitter/diff context
- `src/forgeflow/evaluation` — benchmark/policy
- `tests/{unit,integration,e2e,chaos,security}`

Commit messages should be focused. Do not include credentials, generated databases or
private prompts in commits.
