# ForgeFlow 2.0 Status

Last updated: 2026-10-26 (evidence capture and final review are recorded below and in
`evidence/manifest.json`).

## Current phase

Phase 0–5 code, tests, docs and examples are implemented. The local core is verified with
real command output. Real paid model completion, remote GitHub Actions, Harbor, Docker
daemon execution, Icarus simulation and FPGA hardware remain unproven/blocked.

Baseline specification commit: `6f8f99b`.
Implementation/test commit used for final evidence: `eefddc0`.
Evidence manifest status: **PASS** (all phase commands exit 0) at that SHA.
Exact commands, exits, Git SHAs and log SHA-256: `evidence/manifest.json`.

## Gate table

| Gate | Status | Basis | Evidence |
|---|---|---|---|
| G0 | PASS | audit, architecture, ADRs, risk/task DAG, protocol notes, acceptance matrix | `docs/forgeflow-v2/`, `evidence/phase0/` |
| G1 | PASS | wheel build, fresh venv install, CLI help/doctor, real pytest E2E | `evidence/phase5/`, `scripts/verify_install.py` |
| G2 | PASS | DAG/claim/lease/event/replay/hard-kill/rollback/side-effect tests | `evidence/phase1/` |
| G3 | PASS | self-report blocked, trusted PASS, STALE, tamper, clean-room with automatic frozen dependency sync, provenance | `evidence/phase2/`, `evidence/phase0/` |
| G4 | PASS | Tree-sitter Python/TS, diff symbols, cache, explained budgeted context | `evidence/phase3/` |
| G5 | BLOCKED | Mock + Pi RPC + Claude contracts complete; Pi real handshake PASS; real model completion not run (paid approval absent) | `evidence/phase3/`; fixture tests; `NOT_RUN` real model |
| G6 | PASS | worktree ownership, bounded parallel, conflict/abort/retry, serial fallback, post-merge regression | `evidence/phase4/` |
| G7 | PASS | OTel scrubbed spans/local fallback, benchmark raw data, context/recovery/reviewer/parallel ablations; A/B NOT_RUN, Harbor EXTERNAL_BLOCKED | `evidence/phase4/`, `evidence/benchmarks/` |
| G8 | PASS | Python adapter real tools, FPGA fixture parsers/artifact/hardware status, agent-app demo | `evidence/phase5/`, `examples/` |
| G9 | PASS | full tests, ruff, docs, clean install, READY delivery manifest and blocker list consistent. Remote GitHub Actions run `40325860472` at `521ec28` passed all 4 OS/Python jobs. | `evidence/final/`, `evidence/delivery/demo/`, `evidence/ci/github-actions.json` |

Local core is verified; external integrations are blocked. Overall is **not** a claim of
100% production completion.

## Independent review

A read-only independent review found and we fixed three high-severity false-PASS paths:
older PASS masking a newer failure, clean-room failure falling back to old evidence, and
committed protected-test tampering moving HEAD. Medium findings on worktree self-commits,
rebase/regression rollback, stale status refresh, CLI exit codes, attempt-bound evidence,
environment/PATH overrides and ignored files were also fixed. See `REVIEW.md` and the
corresponding regression tests.

## Actual local test snapshot

- Final evidence run: `uv run pytest -q --cov=forgeflow ...` → **113 passed in 351.90s**,
  exit 0. Log: `evidence/final/02_uv_--cov-report_xml_evidence_fina.stdout.log`.
- Coverage: **79.27%** statements (4923/5927), `evidence/final/coverage.json`.
- JUnit: `evidence/final/junit.xml`; docs/CLI: `evidence/phase0/`.
- Delivery: `evidence/delivery/demo/manifest.json`, state **READY**, SHA-256
  `b698816a5761890d86f597d6f53aeecf3e1ba7420ab6e46a03938853da040fd8`.
- Clean wheel install: SHA-256
  `6cff9b750581c4483ba2207b416feca00f160ac476afbbefb2c262cfd78991b5`,
  version 2.0.0, doctor `PASS_WITH_BLOCKERS`, real pytest verify PASS, delivery READY.
- `ruff check src tests`: PASS (`evidence/final/01_uv_tests.stdout.log`).
- Benchmark: `bench_20261026T113956Z_e0ece0ef`; C/D and structure/full-file/keyword
  modes 100% verified on the simulator, no-context and diff-only 0%, no-recovery 50%.
  A/B baselines NOT_RUN; Harbor EXTERNAL_BLOCKED; parallel ablation PASS (1.122× on a
  ~2.3 s simulated workload). This is not an LLM-quality comparison.
- Real external checks: Pi RPC handshake (no model prompt), Claude version/auth probe
  (no paid completion), Docker daemon unavailable, Harbor/Icarus/Verilator missing.

## GitHub repository

- URL: <https://github.com/OrdoAbChao7/ForgeFlow>
- Visibility: private
- Default branch: `main`
- Remote CI: run `40325860472` at `521ec28`, conclusion `success` across
  Ubuntu/Windows × Python 3.11/3.13.

## Environment

- Windows 11, Python 3.13 for development; project supports >=3.11.
- Git, uv, Node, Pi and Claude CLI present.
- Docker executable present but daemon unavailable.
- Harbor, Icarus Verilog, Verilator, FPGA hardware unavailable.

## Known limitations and blockers

| Item | State | Minimum human action |
|---|---|---|
| Real Pi/Claude model completion | BLOCKED / NOT_RUN | approve paid usage, then run `forge run --agent pi_rpc|claude_cli --allow-paid` |
| GitHub CI remote execution | PASS | run `40325860472`, 4/4 matrix jobs success; see `evidence/ci/github-actions.json` |
| Harbor comparison | EXTERNAL_BLOCKED | install/configure Harbor and run a real suite |
| Docker clean-room | EXTERNAL_BLOCKED | start Docker daemon and implement/validate container execution path |
| FPGA simulation | BLOCKED | install Icarus and run RTL simulation |
| FPGA hardware PASS | BLOCKED | real board run + independent signature trust root |
| Manual/hardware gate | BLOCKED by design | configure an external signature verifier |
| Real LLM benchmark quality | NOT_RUN | approve model budget; current agent is a labeled simulator |

## Evidence and recovery next actions

1. Inspect `evidence/manifest.json` (status PASS) for command, exit code, Git SHA, log
   SHA-256. The evidence commit follows implementation commit `eefddc0`; docs-only edits
   do not change the measured code.
2. Run `uv run pytest -q` and `uv run forge doctor --json`.
3. For a local E2E: `uv run python examples/basic_goal/run_demo.py --clean-room`.
4. For benchmark reproduction: `uv run forge benchmark --repeats 2`.
5. To resume interrupted runtime state: `uv run forge recover` then `forge resume`.
6. Do not mark external integrations PASS without replacing the corresponding BLOCKED/
   NOT_RUN evidence with real observations.
