# Install and Runbook

## Requirements

- Python >= 3.11
- Git
- uv (recommended)
- Optional: Docker daemon (clean-room containers; blocked in current build), Pi, Claude Code
- Optional: Icarus Verilog/Verilator and FPGA hardware for FPGA adapter

## Windows PowerShell

```powershell
cd E:\Projects\ForgeFlow
uv sync --extra dev
uv run forge --help
uv run forge doctor --json
uv run pytest -q
```

If PowerShell blocks local scripts, use the `.exe` entry under `.venv\Scripts\forge.exe`
or invoke `uv run forge`.

## WSL2 / Linux

```bash
cd /mnt/e/Projects/ForgeFlow   # WSL path may differ
uv sync --extra dev
uv run forge --help
uv run forge doctor --json
uv run pytest -q
```

WSL performance is best when the repo is cloned inside the Linux filesystem. Git line
endings: evidence fingerprints LF-normalize text so CRLF checkouts do not create false
drift.

## Clean install verification

```bash
uv run python scripts/verify_install.py
```

This builds the wheel, creates a fresh venv, installs the wheel + pytest, runs CLI/help/
doctor and a real mock-agent → pytest → verify → deliver E2E. It requires network/cache
access for dependencies. A failed install exits non-zero and prints command records.

## First project flow

```bash
uv run forge init
uv run forge goal --file examples/basic_goal/goal.yaml   # edit paths for your project
uv run forge run <goal-id> --agent mock
uv run forge verify <goal-id>
uv run forge deliver <goal-id>
```

`--agent mock` is deterministic. For Pi/Claude:

```bash
uv run forge run <goal-id> --agent pi_rpc --allow-paid
uv run forge run <goal-id> --agent claude_cli --allow-paid
```

`--allow-paid` is an explicit cost authorization. Without it the CLI refuses. Credentials
must come from the normal CLI configuration; never put keys in goal files or logs.

## Interrupted runs

```bash
uv run forge recover <goal-id>
uv run forge resume <goal-id>
uv run forge run <goal-id>
```

Expired non-side-effecting attempts become `READY`; side-effecting attempts become
`BLOCKED` because the external outcome is unknown. Reconcile using the recorded
idempotency key.

## Troubleshooting

| Symptom | Check |
|---|---|
| `git add` fails in a benchmark | Windows path length; benchmark uses short temp paths and `core.longpaths=true` |
| clean-room fails | Git repo and immutable goal baseline commit required; protected tests must match that baseline |
| verification STALE | source/test/env/policy changed after evidence; rerun `forge verify` |
| Docker blocked | start Docker Desktop; current build still reports container path unavailable |
| Pi/Claude refused | pass `--allow-paid`; check credentials and provider readiness |
| Harbor blocked | install/configure Harbor; internal fixture benchmark still works |
| FPGA BLOCKED | install Icarus; hardware PASS requires an actual board and trust root |
