# Independent Review and Resolutions

Date: 2026-10-26. An independent read-only review examined the trust kernel, runtime,
runner, worktree/parallel, agent boundaries, benchmark and delivery code. No repository
files were changed by the reviewer. Findings below were reproduced or inspected; all
CONFIRMED defects were fixed and covered by tests before final evidence capture.

## Confirmed findings and fixes

| Severity | Finding | Resolution | Regression test |
|---|---|---|---|
| HIGH | Older PASS masked a newer failing trusted run | Gate chooses newest evidence per command signature; any newest failure wins; `verify_goal` requires no command failures | `test_newer_failing_command_overrides_older_pass`, `test_newer_failure_cannot_be_masked_by_previous_pass` |
| HIGH | Clean-room failure still returned PASS from old normal evidence | Clean-room gate evaluation accepts only `clean_room` trust; success/evidence required; failures force FAIL | `test_clean_room_setup_failure_blocks_evidence`, clean-room E2E |
| HIGH | Agent could commit weakened protected tests to move HEAD and pass HEAD checks | Goal stores immutable baseline commit at creation; protected files compare/restore against that commit; ignored protected files are walked directly | `test_committed_protected_test_tampering_is_blocked`, `test_gitignored_protected_file_is_still_guarded` |
| MEDIUM | Worker self-commits appeared as clean success with no merge | Worktree change detection includes `base..HEAD`; self-commit is ownership-checked and merged; no-change task is not a merge | `test_worker_self_commit_is_not_lost` |
| MEDIUM | Conflict rebase targeted `HEAD` (no-op); failed post-merge regression stayed merged | Rebase targets current main commit; regression failure resets to the pre-merge commit and reports rollback | `test_post_merge_regression_rolls_back`, conflict tests |
| MEDIUM | `refresh_staleness` ignored env/protected-only changes | Re-evaluates PASS gates with the current binding and persists invalidation; `status` refreshes first | `test_refresh_staleness_detects_env_and_protected_changes` |
| MEDIUM | CLI verify/deliver returned exit 0 on FAIL/BLOCKED | Non-PASS verification and non-READY delivery now exit 1 after emitting output | CLI E2E failure checks |
| MEDIUM | Task verification accepted old-attempt evidence and caller-supplied gate objects | Evidence must bind the current attempt; gate objects must be persisted, current-revision PASS with covering evidence | `test_dependency_not_released_until_verified`, runtime E2E |
| MEDIUM | Verifier environment could override PATH; inherited output could outlive process | PATH/PYTHONPATH/NODE_PATH overrides denied; executable resolves via trusted PATH; incomplete capture is evidence failure | `test_environment_allowlist_and_forbidden_override` |
| MEDIUM | Gitignored files escaped ownership/source coverage | Ownership sees ignored paths; source fingerprint includes ignored non-runtime files and clean room copies them | ignored protected test; context/evidence suites |

## Residual risks (documented, not hidden)

- The local clean room is not an OS sandbox. A hostile process can still access the host
  under the current user; Docker execution is `EXTERNAL_BLOCKED`.
- SHA-256 binds integrity, not signer identity. Manual/hardware gates require an external
  trust root.
- External side effects are not exactly-once; blocked reconciliation is required.
- Real paid Pi/Claude model quality, Harbor comparisons, remote GitHub Actions, Icarus
  simulation and FPGA hardware were not run.
- The benchmark agent is a labeled deterministic simulator, not an LLM-quality result.

## Verification after fixes

- Full suite re-run during final evidence capture; see `evidence/final/`.
- Focused review regressions: evidence/security, E2E, worktree, parallel, benchmark and
  CLI suites all passed locally.
- `ruff check src tests` passed.
