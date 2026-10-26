# Acceptance Matrix (G0–G9)

This matrix maps the implementation spec to executable checks. Gate status is recorded in
`STATUS.md`; local command logs and hashes are in `evidence/`. `BLOCKED` and `NOT_RUN`
never count as PASS.

| Gate | Spec requirement | Machine check / evidence | External dependency |
|---|---|---|---|
| G0 | Audit, ADR, risks, task DAG, compatibility, code alignment | `docs/forgeflow-v2/ARCHITECTURE.md`, `DECISIONS.md`, `TASKS.md`, `PROTOCOL_NOTES.md`; `phase0` command verifies files | none |
| G1 | Installable package, CLI help, Windows/WSL/Linux reproducible | `scripts/verify_install.py`: wheel build, fresh venv, `forge --help`, doctor, real pytest E2E | network for wheel deps (recorded) |
| G2 | Goal/Task/State/Event/Resume tests + fault injection | planner/store/runner tests, hard-kill test, transaction rollback, idempotent event, side-effect BLOCKED | none |
| G3 | Independent evidence/version binding/clean room/pollution/STALE | `tests/security/test_evidence.py`, E2E tamper/stale/clean-room, `forge explain`, `forge verify --clean-room` | Docker optional only; local room used |
| G4 | AST/incremental retrieval, explained ranking, budget | `tests/unit/test_context.py`; Python+TS fixtures; cache invalidation; five modes | none |
| G5 | Mock + Pi RPC + second agent contracts; real connectivity honesty | agent tests, MCP/ACP contract tests, Pi real handshake (no model), Claude probe; model completion requires paid approval | Pi/Claude credentials + budget → `BLOCKED` |
| G6 | Parallel isolation, conflict, merge regression, bounded self-heal | `test_worktree.py`, `test_parallel.py`, recovery tests; benchmark parallel ablation | Git only |
| G7 | OTel scrubbing, evaluation/ablation reproducible, baselines NOT_RUN | telemetry tests, benchmark raw JSONL/CSV/manifest, policy tests, benchmark run | Harbor absent → `EXTERNAL_BLOCKED`; A/B baseline → `NOT_RUN` |
| G8 | Python/FPGA/Agent adapters, no fake hardware | adapter tests, FPGA parser/artifact fixtures, hardware status, agent-app demo | Icarus/FPGA hardware → `BLOCKED` |
| G9 | Tests/CI/docs/delivery manifest/blockers consistent | full pytest, ruff, clean install, `forge deliver`, final manifest, CI workflow | remote GitHub Actions execution → `NOT_RUN` locally |

## Phase acceptance details

| Phase | Minimum vertical slice | Evidence |
|---|---|---|
| 0 | repository audit and architecture artifacts | `evidence/phase0`, docs |
| 1 | goal → event → claim → agent → verify → checkpoint | Phase 1 tests + mock E2E |
| 2 | self-report BLOCKED → trusted PASS → source STALE → clean-room PASS | Phase 2 tests, CLI clean-room |
| 3 | context modes, mock/Pi/Claude/MCP/ACP boundaries, worktrees | Phase 3 tests |
| 4 | recovery, spans, benchmark raw data + ablation, policy rollback | Phase 4 tests + benchmark manifest |
| 5 | clean install, adapters, delivery manifest, docs/CI | Phase 5 tests, `manifest.json` |

## Commands that must not be weakened

- Do not delete/modify failing tests or assertions to obtain PASS.
- Do not mark C/D benchmark rows as real-agent quality; agent is `context_oracle_sim`.
- Do not mark A/B baselines PASS when not run.
- Do not mark FPGA/hardware PASS without an independent trust root.
- Do not treat `agent_settled`/self-report as verification.
