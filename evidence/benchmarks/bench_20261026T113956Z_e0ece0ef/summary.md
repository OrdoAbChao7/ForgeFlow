# ForgeFlow benchmark bench_20261026T113956Z_e0ece0ef

- Agent: `context_oracle_sim` (simulated, not LLM quality)
- Git SHA: `eefddc013e4e171bc4395e93c76dabca1f5b50cb`
- Repeats: 2
- Token budget: 900

| Mode | Context | Recovery | Reviewer | Runs | Verified | False completion | Avg tokens | Latency (s) |
|---|---|---|---|---:|---:|---:|---:|---:|
| C_core | structure | True | False | 4 | 100.00% | 50.00% | 449.0 | 12.051 |
| D_full | structure | True | True | 4 | 100.00% | 50.00% | 449.0 | 12.305 |
| ablation_no_context | none | True | False | 4 | 0.00% | 100.00% | 619.5 | 18.371 |
| ablation_no_recovery | structure | False | False | 4 | 50.00% | 50.00% | 283.0 | 9.707 |
| ablation_full_file | full_file | True | False | 4 | 100.00% | 50.00% | 446.5 | 11.797 |
| ablation_keyword | keyword | True | False | 4 | 100.00% | 50.00% | 486.5 | 12.020 |
| ablation_diff_only | diff_only | True | False | 4 | 0.00% | 100.00% | 712.5 | 17.933 |

## Baselines

- `A_native_agent`: **NOT_RUN** — no approved paid model baseline; CLI presence is not a benchmark result
- `B_openspec_gsd`: **NOT_RUN** — OpenSpec/GSD baseline tools were not configured or executed

## Limitations

- Agent is a deterministic context-sensitive simulator, not a real LLM.
- No native-agent or OpenSpec/GSD baseline was run without approved credentials/tools.
- Costs are zero for the simulated agent; token figures are prompt approximations.
- Static reviewer had no findings on these benign fixtures; reviewer ablation is therefore neutral.
- Parallel ablation measures harness/worktree overhead with a simulated worker, not LLM-agent concurrency.
