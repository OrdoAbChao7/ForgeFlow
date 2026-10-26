# Benchmarks

## Reproduce

```bash
uv run forge benchmark --repeats 2
```

Outputs under `evidence/benchmarks/<run-id>/`:

- `runs.jsonl` — every run incl. evidence digests and archived log hashes
- `summary.csv` / `summary.md` / `summary.json`
- `manifest.json` — Git SHA, environment digest, case digests, artifact hashes
- `logs/<run-id>/<runner>/` — retained stdout/stderr/result logs

## Modes

- `C_core`: structure context + bounded recovery.
- `D_full`: C + deterministic static reviewer gate.
- `ablation_no_context`: no context bundle.
- `ablation_no_recovery`: first verification failure is terminal.
- `ablation_full_file`, `ablation_keyword`, `ablation_diff_only`: retrieval comparison at
  the same token budget.
- `parallel_ablation`: serial vs two-worker worktree scheduling with a simulated worker.

## Honest interpretation

The agent is `context_oracle_sim`, a deterministic simulator that writes the known
fixture solution only when the contract marker is visible in the assembled prompt. It
measures whether ForgeFlow delivers contract context and recovers, **not** LLM coding
quality. Costs are zero; token counts are prompt approximations. Real test execution is
pytest and evidence is retained.

Native-agent and OpenSpec/GSD baselines are `NOT_RUN` without approved tools/credentials.
Harbor is `EXTERNAL_BLOCKED` because its CLI is absent. Do not compare simulator rows to
reported LLM benchmarks.
