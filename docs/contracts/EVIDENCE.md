# Evidence contract

An `Evidence` record is created only by a trusted runner/clean-room or by an independently
verified manual path. Its fields bind:

- goal ID and revision;
- task and attempt IDs (when task-scoped);
- Git commit, tree hash and diff hash;
- source fingerprint (tracked, untracked and gitignored workspace source, excluding known
  runtime/cache directories and protected verifier paths) and protected-file fingerprint;
  protected paths are walked directly so ignored test files cannot hide;
- environment digest (Python/platform/locks/packages/tool versions);
- exact argv, cwd, exit code, timeout, runner ID;
- SHA-256 of stdout, stderr, result report and retained logs;
- trust level, gate, contract scope and verifier policy version.

Source text hashes LF-normalize CRLF; binaries are exact. SHA-256 is integrity, not
identity or a signature.

## Gate rules

PASS needs trusted evidence, matching revision/source/protected/environment/policy and
scope coverage, with exit 0 and no timeout. Self-report is audit data only. Source or test
changes produce STALE. No evidence is BLOCKED, never PASS. Manual/hardware evidence needs
an external verifier and never becomes hardware PASS by a boolean flag.

## Provenance

```bash
uv run forge explain adds --goal <goal-id> --json
```

returns task/attempt/code snapshot/test run/evidence/gate/artifact edge data. Logs and
report hashes are included. Clean-room evidence additionally records the trusted snapshot
commit, isolated workspace path, protected files restored, setup mode and whether locked
dependencies were reinstalled (`uv sync --frozen` when lock files are present).
