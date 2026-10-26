# ForgeFlow 2.0

**Verifiable & self-improving AI coding harness.** ForgeFlow runs coding agents under
durable state, controlled execution and independent verification. The central rule:
**an agent's self-report is never proof**. Every task transition is event-sourced; only a
trusted command run bound to the current code and environment can satisfy a contract gate.

This repository implements `docs/forgeflow-v2/IMPLEMENTATION_SPEC.md` with real code,
tests, evidence and honest external blockers.

## Status

- **Core verified locally**: 113 tests collected/passed after the locked clean-room
  addition; final evidence is regenerated with the suite. See `docs/forgeflow-v2/STATUS.md`.
- **External integrations blocked/not run**: paid Pi/Claude model completion, remote
  GitHub Actions, Harbor, Docker daemon clean-room execution, Icarus/FPGA hardware.
- The fixture benchmark agent is an explicitly labeled deterministic simulator. Baselines
  A/B are `NOT_RUN`; Harbor is `EXTERNAL_BLOCKED`. There is no fabricated comparison.

## Install

```bash
uv sync --extra dev
uv run forge --help
uv run forge doctor --json
uv run pytest -q
```

Python 3.11+. Windows PowerShell, WSL2 and Linux instructions:
`docs/runbooks/install.md`.

Clean wheel install E2E:

```bash
uv run python scripts/verify_install.py
```

## Quick start

```bash
uv run forge init
uv run python examples/basic_goal/run_demo.py --clean-room
uv run python examples/parallel_tasks/run_demo.py
uv run forge benchmark --repeats 2
```

CLI flow for a project:

```bash
uv run forge goal --file my-goal.yaml
uv run forge run <goal-id> --agent mock
# after offline policy validation + activation:
uv run forge run <goal-id> --policy runtime
uv run forge status <goal-id> --json
uv run forge verify <goal-id> --clean-room
uv run forge explain <criterion-id> --goal <goal-id> --json
uv run forge deliver <goal-id>
```

Real agent runs require explicit cost authorization:

```bash
uv run forge run <goal-id> --agent pi_rpc --allow-paid
uv run forge run <goal-id> --agent claude_cli --allow-paid
```

## What makes it different

- **Durable kernel**: SQLite WAL, atomic leases/fencing, append-only events, replay,
  hard-kill resume and non-replayed side effects.
- **Trusted evidence graph**: commit/tree/diff/source/protected/env/command/exit/report
  digests; PASS/FAIL/BLOCKED/STALE gates; provenance queries.
- **Clean room**: private-index Git snapshot, protected verifier restore from an
  immutable goal baseline, isolated rerun, clean-room-only gate evidence.
- **Context engineering**: Tree-sitter Python/TypeScript + Git diff ranking, cache,
  token budget, explanations and five comparison modes.
- **Interoperability**: Mock, Pi RPC, Claude CLI, MCP 2026-07-28, ACP v1 boundary.
- **Parallel isolation**: bounded worktree scheduling with ownership checks, merge
  conflict handling and post-merge regression.
- **Bounded recovery**: classification, same-error circuit breaker, test-deletion guard,
  external blockers that do not stop independent tasks.
- **Evaluation**: local OTel spans with scrubbing, reproducible fixture benchmark,
  ablations, offline-validated policy rollout.

## Architecture

Control Plane → Execution Plane → Verification Plane → Evaluation Plane.
See `docs/forgeflow-v2/ARCHITECTURE.md`, `DECISIONS.md`, `ACCEPTANCE_MATRIX.md`, and
verified protocol notes in `PROTOCOL_NOTES.md`.

## Limitations

- No built-in OS sandbox; local clean-room is not container isolation. Docker path is
  explicitly blocked in this build.
- SHA-256 is integrity, not signer identity. Manual/hardware gates need an external trust
  root; hardware PASS is never inferred.
- External side effects are not exactly-once. Unknown side-effecting attempts become
  BLOCKED for manual reconciliation.
- Real model quality is not benchmarked in this environment.
- LSP/semantic call graph is not claimed; Tree-sitter is syntax-only.

## License

MIT. See `LICENSE`.
