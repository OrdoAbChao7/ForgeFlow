# Changelog

## [2.0.0] - 2026-10-26

### Added

- Durable SQLite WAL goal runtime: Goal/Revision/Task/TaskAttempt/Event/Checkpoint,
  atomic claims, leases/fencing, replay and crash recovery.
- Controlled command runner: cwd confinement, env allowlist, timeout process-group kill,
  full logs/hashes, approval policy for network/irreversible operations.
- Evidence provenance bound to revision, task attempt, commit/tree/diff, source,
  protected verifier files, environment, command, exit code and policy version.
- Gate engine with PASS/FAIL/BLOCKED/STALE, incremental invalidation and
  `forge explain` criterion provenance.
- Clean-room verifier using a private Git index snapshot, protected-file restore and
  isolated trusted rerun; optional Docker path reports EXTERNAL_BLOCKED when unavailable.
- Tree-sitter Python/TypeScript context, Git-diff symbol ranking, configurable weights,
  SQLite incremental cache, budgets, explanations and five comparison modes.
- Agent boundaries: deterministic Mock, Pi RPC JSONL, Claude Code CLI stream-json,
  MCP 2026-07-28 stateless client (discovery/tasks/legacy fallback), ACP v1 host boundary.
- Worktree isolation and bounded parallel scheduler with ownership checks, conflict
  abort/retry and serial fallback.
- Recovery classifier, bounded retries, same-error circuit breaker, test-deletion guard,
  external blocker handling, OTel local tracing with scrubbing.
- Reproducible internal benchmark with context/recovery/reviewer/parallel ablations,
  raw JSONL/CSV, manifests, NOT_RUN baselines, Harbor boundary and policy rollout.
- Python, FPGA and agent-app adapters; delivery manifests; CLI, CI, examples and
  clean-install verification.

### Known limitations

- Real paid model completion, remote CI, Harbor, Docker clean-room, Icarus and FPGA
  hardware are not proven in this environment. See `docs/forgeflow-v2/STATUS.md`.
- Benchmark agent is a deterministic context-sensitive simulator, not an LLM-quality
  comparison.
