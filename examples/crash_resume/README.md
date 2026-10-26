# Crash / resume example

```bash
uv run python examples/crash_resume/run_demo.py
uv run pytest tests/integration/test_recovery_resume.py -q
```

The script demonstrates lease expiry and safe requeueing. The test suite additionally
uses a child process that calls `os._exit(17)` after claiming a task, proving recovery
after a real hard process death. Side-effecting attempts are never replayed
automatically; they become `BLOCKED` with an idempotency key for manual reconciliation.
