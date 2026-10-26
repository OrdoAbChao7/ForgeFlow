# ForgeFlow 2.0 Decisions (ADR log)

Each entry records context, alternatives, rationale and rollback. Status reflects the
implementation, not aspirational architecture.

## ADR-001: SQLite WAL + append-only events instead of an external workflow engine

- **Context**: local Windows/WSL use, need crash recovery, atomic claim and audit without
  a service dependency.
- **Alternatives**: Temporal (durable workflows, server/worker complexity), Beads (issue
  graph, different semantics), in-memory queues (unsafe), Postgres (deployment burden).
- **Decision**: SQLite in WAL with explicit `BEGIN IMMEDIATE`, typed event log, per-goal
  sequence, task projections and replay.
- **Consequence / limits**: single-machine concurrency is modest; cross-process exactly
  once side effects are explicitly not promised. Expired side-effecting attempts are
  BLOCKED with idempotency keys.
- **Rollback**: event/revision rows are portable; a future engine can consume the log.
  Revert by using the previous package version and state database migration.

## ADR-002: Pydantic v2 domain models with deterministic IDs

- **Context**: need JSON schemas, YAML compilation, strict validation and DB serialization.
- **Alternatives**: dataclasses (less validation), attrs, raw dicts.
- **Decision**: strict Pydantic models; task IDs derived from `goal_id:task_key`; UUIDs
  for aggregates; canonical JSON for digests.
- **Rollback**: schema versioning in SQLite; models can be narrowed or migrated.

## ADR-003: Agent self-report never creates evidence

- **Context**: the central failure mode is an agent saying “done” while tests fail.
- **Alternatives**: agent transcript scoring, LLM-as-judge as primary evidence.
- **Decision**: only trusted command/clean-room/manual-verified records become evidence.
  `SELF_REPORTED` records are audit data and cannot satisfy a gate. Gate PASS requires
  current source/protected/environment fingerprints and a policy version.
- **Rollback**: none for trust semantics; a separate advisory judge can be added but not
  as PASS authority.

## ADR-004: Clean-room via private-index Git snapshot, not mutable worktree

- **Context**: clean-room must be based on a trusted commit, apply agent changes, restore
  verifier files, and avoid mutating the user's index.
- **Alternatives**: copy directory (ignores Git semantics), `git stash` (mutates),
  Docker-only (unavailable here).
- **Decision**: build a temporary index with `GIT_INDEX_FILE`, `git add -A`,
  `write-tree`, `commit-tree`, extract with `git archive`; restore protected paths from
  the goal's immutable baseline commit; reject protected-file drift (including committed
  drift); automatically run `uv sync --frozen` for `pyproject.toml` + `uv.lock` unless an
  explicit setup command is provided; compare prepared and live source fingerprints before
  recording evidence.
- **Consequence**: text line endings are LF-normalized for fingerprints because Windows
  Git may check out CRLF. Binaries are hashed exactly.
- **Rollback**: delete clean-room evidence and rerun verifier; local Docker path remains
  explicitly `EXTERNAL_BLOCKED` in this build.

## ADR-005: Tree-sitter symbol index + Git diff, not a claimed semantic graph

- **Context**: need bounded, explainable context retrieval in Python and one other
  language without external services.
- **Alternatives**: full-file prompts, keyword search, embedding/vector service, LSP.
- **Decision**: Tree-sitter Python and TypeScript/TSX syntax, text fallback, SQLite cache,
  configurable scoring and forced contract files. Benchmark all modes at the same budget.
- **Limits**: tree-sitter is syntax-only; no LSP plugin or complete call graph is claimed.
- **Rollback**: mode can be set to `full_file`, `keyword`, `diff_only` or `none`.

## ADR-006: MCP 2026-07-28 stateless client, ACP v1 host boundary

- **Context**: official docs verified on 2026-10-26; MCP removed sessions/initialize in
  the current revision; ACP is editor↔agent, not agent-to-agent.
- **Alternatives**: legacy MCP initialize everywhere, treating ACP as an agent bus.
- **Decision**: send per-request `_meta`, probe `server/discover`, validate `resultType`,
  support optional tasks extension, and keep a clearly marked legacy fallback. ACP client
  implements initialize/session/prompt as a boundary contract with deny-by-default
  permissions. Neither grants task scheduling/verification authority.
- **Rollback**: adapter-specific; MCP legacy is already isolated; ACP is optional.

## ADR-007: Pi RPC and Claude CLI are adapters; paid runs are opt-in

- **Context**: both CLIs are installed locally, but model completion can cost money and
  depends on credentials.
- **Alternatives**: auto-run models for evidence (unbounded cost), never implement.
- **Decision**: contract fixtures plus credential-independent tests; a real Pi handshake
  is exercised locally without a model prompt. `forge run` requires `--allow-paid` for
  any non-mock agent. Missing credentials are `EXTERNAL_BLOCKED`.
- **Rollback**: choose Mock or remove adapter; verification semantics unchanged.

## ADR-008: Worktree scheduler with ownership serialization

- **Context**: parallel agents can conflict and orphan worktrees are dangerous.
- **Alternatives**: shared worktree concurrency (unsafe), five default LLM workers
  (expensive), no parallelism.
- **Decision**: max four workers, default two; overlapping ownership serialized; dirty
  and worker-self-committed changes ownership-checked; serial merge; real rebase onto
  current main; post-merge regression with rollback on failure; cleanup only recorded
  paths. `serialize_overlapping_ownership=False` exists for conflict tests, not
  recommended operation.
- **Rollback**: disable parallel scheduler; core runtime remains single-workspace.

## ADR-009: Deterministic benchmark simulator, explicitly labeled

- **Context**: no approved paid model baseline; fabricating LLM comparisons is forbidden.
- **Alternatives**: ship no benchmark, pretend Mock is an LLM, run paid models unapproved.
- **Decision**: internal context-sensitive simulator gated by contract visibility, real
  pytest execution, raw results, multiple modes and ablations. Baselines A/B NOT_RUN;
  Harbor blocked. Parallel ablation uses a simulated worker and measures overhead only.
- **Rollback**: delete benchmark records; benchmark is isolated from production state.

## ADR-010: Manual/hardware evidence requires an external trust root

- **Context**: a boolean “signed” flag is trivially forged.
- **Alternatives**: accept operator text, build a custom PKI, mark manual gates blocked.
- **Decision**: `record_manual` requires a caller-supplied signature verifier; no default
  trust root ships. Hardware attestations stay `UNVERIFIED`/`SIGNATURE_UNTRUSTED`, never
  hardware PASS. The CLI does not fabricate signatures.
- **Rollback**: integrate an external signer/key policy later; evidence schema extends.

## ADR-011: Local OTel JSONL exporter, no collector requirement

- **Context**: OTel SDK is available but no collector is assumed.
- **Decision**: use the OTel SDK with a local JSONL exporter; fallback writer if SDK is
  absent. Scrub secrets/prompts/code before persistence; 14-day default retention.
- **Rollback**: disable telemetry or point OTel at an external collector after explicit
  configuration; never log raw prompts by default.
