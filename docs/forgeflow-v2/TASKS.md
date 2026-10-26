# ForgeFlow 2.0 Tasks and Acceptance DAG

Status legend: `[x]` implemented and locally tested, `[~]` implemented with an external
blocker, `[ ]` not done. Final evidence paths are filled by
`scripts/capture_evidence.py`; see `STATUS.md` and `evidence/manifest.json`.

Baseline audit: repository was not initialized as Git and contained only
`docs/forgeflow-v2/IMPLEMENTATION_SPEC.md`. Baseline commit: `6f8f99b`. No user files
were overwritten.

## Dependency DAG

```
P0 baseline/docs/schema
 ├─> P1 durable kernel ──> P2 evidence/clean-room ──> P3 context/agents/parallel
 │                                                       └─> P4 recovery/OTel/eval
 └──────────────────────────────────────────────────────────> P5 adapters/delivery
```

Critical path: `P0 -> P1 -> P2 -> P3 -> P4 -> P5`. P5 adapter fixtures can proceed in
parallel after P0. External integrations (paid model, Docker, Harbor, FPGA hardware) are
not on the critical path; they are represented by contract tests and BLOCKED states.

## Phase 0 — baseline and architecture

| ID | Task | Depends | Status | Acceptance / evidence |
|---|---|---|---|---|
| P0.1 | Audit repo, Git, tools, ports, credentials | — | [x] | baseline `6f8f99b`; `evidence/phase0`; `STATUS.md` |
| P0.2 | Architecture, ADRs, risk and compatibility matrix | P0.1 | [x] | `ARCHITECTURE.md`, `DECISIONS.md`, `PROTOCOL_NOTES.md` |
| P0.3 | Pydantic schema, state transitions, SQLite migrations, IDs | P0.1 | [x] | `tests/unit/test_planner.py`, `tests/unit/test_store.py` |
| P0.4 | Machine-checkable acceptance matrix | P0.2 | [x] | `ACCEPTANCE_MATRIX.md`, G0–G9 table |

## Phase 1 — durable goal runtime

| ID | Task | Depends | Status | Acceptance / evidence |
|---|---|---|---|---|
| P1.1 | YAML/deterministic goal compiler, revisions, contract digest | P0.3 | [x] | planner tests, CLI `goal` |
| P1.2 | DAG cycle rejection, hard/soft deps, ready frontier | P1.1 | [x] | `test_cycle_is_rejected`, `test_dependency_not_released_until_verified` |
| P1.3 | Atomic claim, owner, lease/fencing, heartbeat, retries | P1.2 | [x] | `test_claim_is_atomic_across_two_stores`, `test_expired_lease_*` |
| P1.4 | SQLite WAL events/projections, replay, idempotent events | P1.3 | [x] | `test_goal_creation_events_and_projection`, `test_event_idempotency` |
| P1.5 | Controlled runner: allowlist, timeout, tree kill, logs, path safety | P1.1 | [x] | `tests/unit/test_runner.py` |
| P1.6 | Checkpoint/resume, hard process kill, side-effect non-replay | P1.4/P1.5 | [x] | `test_process_hard_kill_then_resume`, `test_transaction_rolls_back_on_event_failure` |
| P1.7 | Risk quick mode without dropping gates | P1.1 | [x] | planner quick tests, `forge quick` |
| P1.8 | Mock goal→delivery closed loop with real events | P1.6 | [x] | `tests/integration/test_e2e_mock.py` |

## Phase 2 — evidence provenance and clean room

| ID | Task | Depends | Status | Acceptance / evidence |
|---|---|---|---|---|
| P2.1 | Trusted evidence recorder bound to revision/task/attempt/code/env | P1.8 | [x] | `tests/security/test_evidence.py` |
| P2.2 | Gate engine PENDING/PASS/FAIL/BLOCKED/STALE, scope and policy version | P2.1 | [x] | gate tests, `forge verify` |
| P2.3 | Incremental invalidation after source/test/env/tool change | P2.2 | [x] | `test_evidence_goes_stale_after_source_change` |
| P2.4 | Provenance query Goal→…→Gate→Artifact | P2.2 | [x] | `evidence_provenance`, `forge explain` |
| P2.5 | Clean-room snapshot, protected-file restore, locked deps, isolated rerun | P2.1 | [x] | `test_clean_room_reverification`, `test_clean_room_auto_reinstalls_locked_dependencies` |
| P2.6 | Forged stdout/test-tamper/cross-version/self-report security cases | P2.2 | [x] | security tests |
| P2.7 | “self-report blocked → trusted PASS → stale → clean-room” demonstration | P2.5 | [x] | E2E tests and final evidence |

## Phase 3 — context and agent interoperability

| ID | Task | Depends | Status | Acceptance / evidence |
|---|---|---|---|---|
| P3.1 | Tree-sitter Python + TypeScript symbol extraction, text fallback | P0.3 | [x] | `tests/unit/test_context.py` |
| P3.2 | Git diff symbols, references, tests, contracts, weights, budget, explanations | P3.1 | [x] | context tests |
| P3.3 | Incremental index cache and invalidation | P3.2 | [x] | `test_incremental_cache_invalidates` |
| P3.4 | Mock adapter and accepted-vs-settled contract | P1.8 | [x] | `test_mock_adapter_*`, `test_pi_rpc_contract_*` |
| P3.5 | Pi RPC adapter, handshake, cancel, crash handling | P3.4 | [~] | fixture tests + real handshake PASS; real model completion NOT_RUN (paid) |
| P3.6 | Claude CLI second agent stream-json adapter | P3.4 | [~] | fixture contract tests; real paid completion NOT_RUN |
| P3.7 | MCP 2026-07-28 stateless/tasks/legacy boundary | P3.4 | [x] | `tests/integration/test_mcp_acp.py` |
| P3.8 | ACP v1 IDE boundary + mock contract | P3.4 | [x] | ACP tests and `PROTOCOL_NOTES.md` |
| P3.9 | Worktree isolation, bounded parallel scheduling, conflicts, regression | P3.4 | [x] | `test_worktree.py`, `test_parallel.py` |

## Phase 4 — recovery, observability, evaluation

| ID | Task | Depends | Status | Acceptance / evidence |
|---|---|---|---|---|
| P4.1 | Failure classifier and category coverage | P2.2 | [x] | recovery tests |
| P4.2 | Bounded repair, same-error circuit, test-deletion guard, external BLOCKED | P4.1 | [x] | `test_same_error_circuit_breaker_bounds_attempts`, external-independent test |
| P4.3 | OTel spans and local fallback | P1.8 | [x] | `tests/unit/test_telemetry.py` |
| P4.4 | Secret/prompt/code scrubbing and retention | P4.3 | [x] | scrubber/retention tests |
| P4.5 | Reproducible fixture benchmark with four context modes and ablations | P3.2/P4.2 | [x] | `test_benchmark_is_reproducible...`, final benchmark manifest |
| P4.6 | A/B/C/D groups, metrics, raw JSONL/CSV, NOT_RUN baselines | P4.5 | [~] | C/D run; A/B NOT_RUN; Harbor EXTERNAL_BLOCKED |
| P4.7 | Policy candidate offline validation/activation/rollback | P4.5 | [x] | policy tests and CLI |
| P4.8 | Parallel serial-vs-two-worker ablation | P3.9 | [x] | benchmark `parallel_ablation` PASS |

## Phase 5 — adapters, delivery, release

| ID | Task | Depends | Status | Acceptance / evidence |
|---|---|---|---|---|
| P5.1 | Python pytest/ruff/typecheck/coverage adapter; local CI contract | P0.3 | [x] | `test_domain_adapters.py` |
| P5.2 | FPGA Icarus/report/bitstream/hardware-status adapter | P0.3 | [~] | parser/artifact tests; Icarus/hardware BLOCKED |
| P5.3 | Generic agent-app trajectory/retrieval/tool/risk demo | P3.4 | [x] | `examples/agent_app`, adapter tests |
| P5.4 | `forge deliver` manifest with gates, evidence, unverified list, SHA-256 | P2.5 | [x] | delivery E2E and CLI tests |
| P5.5 | CLI commands and JSON outputs | all | [x] | `forge --help`, CLI E2E |
| P5.6 | README, CONTRIBUTING, CHANGELOG, CI, runbooks, examples | P5.4 | [x] | repo docs and `.github/workflows/ci.yml` |
| P5.7 | Clean wheel install and clean-environment E2E | P5.4 | [x] | `scripts/verify_install.py`, `evidence/phase5` |
| P5.8 | Full test/fault-injection/security/independent review and manifest | P5.7 | [x] | `REVIEW.md`; `evidence/manifest.json` PASS; 113 tests, coverage 79.27%, delivery READY |

## Risks

| Risk | Impact | Mitigation | Residual |
|---|---|---|---|
| Agent edits/commits verifier/tests | False PASS | protected files anchored to immutable goal baseline, clean-room restore, test-deletion and ownership guards | OS adversary can bypass harness |
| Evidence reused after edit | False PASS | source/env/protected/policy fingerprints + STALE | Hash collision assumed infeasible |
| Crash during write | Lost/duplicate work | transactions, events, leases, replay | External effects not exactly-once |
| Side-effect replay | Real-world duplicate action | side-effect tasks blocked on unknown; idempotency key | Manual reconciliation |
| Path/command escape | Host compromise | cwd confinement, argv-only, allowlist/approval | No OS sandbox without Docker |
| Secret leakage in traces | Credential exposure | allowlist env, scrubber, no raw prompts | Process environment still inherited by agents |
| Mock mistaken for real agent | False claims | labeled adapter/benchmark, real-model NOT_RUN | User may misread reports |
| Missing hardware/Harbor | Unverified integrations | BLOCKED/NOT_RUN + fixture contracts | Not production-proven |
