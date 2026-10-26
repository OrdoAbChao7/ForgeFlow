# ForgeFlow 2.0 Architecture

Status: implementation architecture, aligned with `IMPLEMENTATION_SPEC.md` and the
current code. This document does not claim completion of external integrations.

## 1. System boundary

ForgeFlow is a local-first Python 3.11+ harness that wraps coding agents with durable
state, controlled execution and independent verification. It is not a model, a
scheduler service, or a cryptographic signing authority. The four planes are:

| Plane | Responsibility | Primary modules |
|---|---|---|
| Control | goal compilation, DAG, claims/leases, checkpoints | `planner/`, `state/`, `runtime.py` |
| Execution | bounded subprocesses, agents, worktrees | `runner/`, `agents/`, `worktree.py`, `parallel.py` |
| Verification | provenance, gates, clean room, review | `verification/`, `recovery/` |
| Evaluation | reproducible fixtures, ablations, policy rollout | `evaluation/` |

`cli.py` is a thin transport. External protocols and domain adapters are optional
boundaries; they never become verification authorities.

## 2. Domain model and identifiers

| Object | Purpose | ID / key |
|---|---|---|
| `Goal` | durable user objective and status | `goal_<uuid>` |
| `GoalSpec` / `GoalRevision` | immutable contract, acceptance, verifier config | `rev_<uuid>`, numeric revision |
| `Task` / `TaskSpec` | executable unit and its ownership/gates | `task_<sha256(goal:key)>`, stable key |
| `TaskAttempt` | one bounded agent execution | `attempt_<uuid>` |
| `Dependency` | hard/soft DAG edge | `(task, depends_on)` |
| `Event` | append-only audit transition | `evt_<uuid>`, per-goal sequence |
| `Evidence` | trusted observation bound to code/env | `ev_<uuid>` |
| `GateEvaluation` | policy decision over evidence | `gate_<uuid>` |
| `Checkpoint` | resumable state snapshot | `cp_<uuid>` |
| `Artifact` | content-addressed output | `art_<uuid>` |
| `BenchmarkRecord` | reproducible evaluation row | `bench_<uuid>` |
| `PolicyCandidate` | versioned offline-validated policy | `policy_<uuid>` |

All timestamps are UTC ISO-8601. JSON is canonicalized for digests. A goal receives a
monotonic `event_seq`; the event log is the audit source of truth and includes
`task_id`, `goal_id`, timestamp, sequence, correlation ID, revision and actor.

## 3. State machine

Goal: `PLANNED -> RUNNING -> VERIFYING -> DELIVERED`, with `PAUSED`, `BLOCKED`,
`FAILED`, `CANCELLED` for off-path transitions. A cancelled or delivered goal cannot
run again.

Task:
`PENDING -> READY -> CLAIMED -> RUNNING -> VERIFYING -> VERIFIED`; failure can move to
`READY` (bounded retry), `FAILED`, or `BLOCKED`. `INTERRUPTED` means a lease expired and
the outcome is unknown; `UNKNOWN` is reserved for manual confirmation. Side-effecting
attempts whose lease expires become `BLOCKED`, never automatically replayed.

Gate: `PENDING`, `PASS`, `FAIL`, `BLOCKED`, `STALE`, `NOT_RUN`. PASS requires trusted,
current evidence; absence of evidence is never a pass. The newest trusted run for a
command signature wins, so an older PASS cannot mask a newer failure. Source,
protected-file, environment or policy changes invalidate prior PASS conservatively as
`STALE`, including in `forge status`.

Illegal transitions raise typed `ConflictError`/`ValidationFailure` and leave the
transaction unchanged.

## 4. Durability and transactions

SQLite runs in WAL mode with explicit `BEGIN IMMEDIATE` for claims and state changes.
Task projections and events commit in the same transaction. A unique per-goal event
sequence prevents gaps. Event `event_id` is idempotent. Each claim uses a random lease
fencing token; completion verifies owner, token and unexpired lease. A heartbeat extends
the lease while an agent runs. On startup, expired leases are marked `INTERRUPTED` and
the event records the attempt status for replay.

Event replay (`Store.project_state`) reconstructs goal/task/attempt status and is tested
against the live projection. This is an audit/consistency mechanism, not a promise of
exactly-once external side effects.

## 5. Execution boundary

`CommandRunner`:

- requires an explicit argv vector (no implicit shell);
- confines cwd under the workspace and resolves symlinks;
- uses an environment allowlist and rejects `FORGE_*` overrides;
- applies timeouts and kills the process group/tree;
- stores full stdout/stderr logs (bounded to 100 MB) and hashes them;
- retains PIDs, timing, exit code and a result JSON;
- requires exact-request approval for network, irreversible, shell, unknown or
  write-disabled operations;
- has no automatic remote push/deploy path.

Agent adapters expose `start/submit/cancel/events/wait_until_settled/status/cost/trace`.
Prompt acceptance is deliberately separate from `agent_settled`. A successful
self-report moves the task only to `VERIFYING`.

`WorktreeManager` creates only ForgeFlow-owned worktrees under `.forge/worktrees`,
checks path ownership including worker self-commits, merges serially, aborts conflicts,
rolls back a merge whose post-merge regression fails, and removes only paths recorded in
`.forge/worktrees.json`. `ParallelWorktreeScheduler` caps workers at four, serializes
overlapping ownership by default, retries actual rebases and can use an explicit serial
fallback. A worktree with no committed changes is not reported as a successful merge.

## 6. Verification and evidence

Evidence is produced only by a trusted recorder from a real `RunResult`, or by a
clean-room/trusted manual path. Fields include goal revision, task/attempt, Git commit,
tree hash, diff hash, source fingerprint, protected-file fingerprint, environment
digest, command, cwd, exit code, timeout, stdout/stderr/report SHA-256, runner identity,
trust level, gate, contract scope, policy version and timestamp.

Provenance chain:

```
Goal -> Contract criterion -> Task -> Attempt -> CodeSnapshot -> TestRun
     -> Evidence -> Gate -> Artifact
```

`evidence_provenance()` and `forge explain <criterion>` answer which command and source
version support a criterion. SHA-256 confirms integrity, not signer identity.

Gate evaluation requires:

1. trusted evidence (`trusted_runner`, `clean_room`, or independently verified manual);
2. matching goal revision, source fingerprint, protected fingerprint and environment;
3. exact verifier policy version;
4. evidence scope covering the required criterion or union of criteria;
5. observed exit code 0 without timeout.

Failures become `FAIL`; only self-report becomes `BLOCKED`; fingerprint drift becomes
`STALE`. Manual/hardware gates cannot pass without an independently configured
signature verifier.

### Clean room

`CleanRoomVerifier` creates a private-index Git commit of the worktree without changing
the real index, extracts it into a temporary directory, restores protected verifier files
from the goal's immutable baseline commit, automatically runs `uv sync --frozen` when
`pyproject.toml` + `uv.lock` are present (unless an explicit setup command is supplied),
runs the trusted verifier config, and records
`clean_room` evidence only if the clean-room operation succeeds and the prepared source
fingerprint matches the live workspace. Protected-file changes (including committed
changes), missing tools or dependency setup failure yield a failed report, and clean-room
gates accept only `clean_room` trust. Docker is detected but not silently emulated; the
current Docker execution path is explicitly unavailable and reports `EXTERNAL_BLOCKED`.

## 7. Context engineering

Tree-sitter extracts Python and TypeScript/TSX symbols (modules, classes, functions,
methods, module constants, line ranges, signatures, imports). Unsupported languages use
an explicit text fallback. Git diff hunks identify changed files/lines/symbols. Ranking
combines changed symbols, task/contract references, changed-code references, related
tests, import relationships, owned paths, keyword hits and forced contract files.
Weights are configurable. The cache is SQLite keyed by path/mtime/size/content hash.

Selection returns line ranges, reasons, approximate tokens, truncation and omitted-chunk
reasons. Contract files are preserved even if this exceeds the budget. This is a syntax
graph, not a semantic LSP call graph; no LSP plugin is claimed in this build.

## 8. Recovery

`FailureClassifier` categorizes compile/type/lint/test/dependency/environment/network/
timeout/resource/permission/contract/external blockers. `RecoveryEngine` applies bounded
attempts, same-error circuit breaking, repair-diff size limits and rejects deletion of
tests. External blockers become `BLOCKED`; independent ready tasks continue. A failed
agent result is retried only under policy and always re-enters independent verification.

## 9. Observability

OpenTelemetry spans are emitted through the OTel SDK to a local JSONL exporter when no
collector exists: `goal.run -> task.run -> agent.run -> tool.run -> verifier.run`.
Attributes include goal/task/attempt IDs, agent, gate, command exit, evidence counts and
GenAI token usage when available. Prompt text, code, credentials and API keys are
scrubbed; raw prompt/code retention is not implemented. Retention defaults to 14 days.

## 10. Evaluation

`forge benchmark` runs a self-contained fixture suite in temporary Git repositories with
real pytest verifiers. It compares context modes and recovery/reviewer ablations, records
raw JSONL/CSV, and computes verified success, false completion, cost, tokens, latency and
merge/parallel overhead. The agent is a labeled deterministic context-sensitive
simulator, not an LLM-quality comparison. A/B native and OpenSpec/GSD baselines are
`NOT_RUN` without approved tools/credentials. Harbor is an evaluation framework boundary;
absent Harbor is `EXTERNAL_BLOCKED`. Policy candidates require thresholded offline
validation before explicit activation and can be rolled back. An active policy can be
applied to `forge run --policy <name>`; unsupported config keys are rejected.

## 11. Adapters

- Python: pytest, ruff, typecheck detection (pyright/mypy), separate coverage; GitHub
  connection requires credentials and is otherwise local-CI simulation only.
- FPGA: Icarus output/synthesis/timing parsers, bitstream SHA-256, hardware attestation
  always unverified without a trust root. Missing Icarus is `BLOCKED`.
- Agent app: trajectory, retrieval explanation, tool results and risk gate; advisory only.

## 12. Security and threat model

Protected against: agent self-report as proof, forged success text, test modification,
cross-source evidence reuse, environment mismatch, stale evidence, path escape, shell
injection, unapproved network/irreversible commands, expired-worker writes, zombie
lease commit, duplicate events, unsafe worktree deletion and secret/prompt trace leakage.

Not solved: OS-level adversary isolation when not using a container; cryptographic
identity/signing; fully trustworthy package installation; exactly-once external side
effects; real FPGA timing closure; LLM output quality. Clean-room temp directories run
with the current user's privileges. No external code is fetched or executed by the
benchmark.

## 13. Compatibility and external boundaries

Verified against official sources on 2026-10-26 in `PROTOCOL_NOTES.md`:

- Pi RPC JSONL (`agent_settled`, request IDs, abort/crash).
- MCP `2026-07-28` stateless core, per-request `_meta`, `server/discover`, `resultType`,
  optional tasks extension, legacy fallback.
- ACP v1 NDJSON JSON-RPC as IDE↔agent only.
- Claude Code CLI 2.1.310 stream-json/print flags.
- OTel GenAI conventions and Harbor evaluation framework.
- Tree-sitter is syntax-only.

## 14. Deliberate non-choices

Beads/Temporal were not embedded: SQLite + append-only events + explicit claims is
sufficient for a local single-machine harness and keeps auditability visible. ACP is not
used as an agent-to-agent bus. MCP is not used as a scheduler. Docker/FPGA/Harbor are not
faked when unavailable. See `DECISIONS.md`.
