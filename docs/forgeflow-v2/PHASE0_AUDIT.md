# Phase 0 Baseline Audit

Audit date: 2026-10-26. No user source files existed beyond the specification.

## Repository state before implementation

- `E:\Projects\ForgeFlow` was **not** a Git repository.
- The only file was `docs/forgeflow-v2/IMPLEMENTATION_SPEC.md` (17,004 bytes).
- No source, tests, package metadata, CI or examples existed.
- Action: initialized `main`, committed the untouched spec as baseline
  `6f8f99b8107476df7fd0315664fe0199313a1e59`. No user change was deleted or overwritten.
- Reuse inventory: none; the implementation is new. No third-party repository source was
  copied. Protocol implementations were written from official specifications.

## Environment at audit time

| Item | Observation |
|---|---|
| OS | Windows 11 Home China 10.0.26200, win32 |
| Python | 3.13.7 system; uv-managed 3.12.12 for the project environment |
| uv | 0.12.5 |
| Node | v24.19.0 |
| Git | 2.51.0.windows.1 |
| Docker | 29.7.2 client; daemon **unavailable** at audit |
| Pi | 1.3.0; RPC handshake succeeds; model configured; no paid prompt sent |
| Claude Code | 2.1.310; CLI/auth probe available; no paid prompt sent |
| Harbor | missing |
| Icarus Verilog / Verilator | missing |
| FPGA hardware | unavailable |
| Network | available for dependency installation and official-doc research |

## Dependencies and licenses (installed metadata)

| Package | Version | License (metadata) |
|---|---|---|
| pydantic | 2.14.1 | MIT |
| typer | 0.27.3 | MIT |
| rich | 14.3.4 | MIT |
| PyYAML | 6.0.3 | MIT |
| tree-sitter | 0.25.2 | UNKNOWN in metadata; project is MIT upstream (verified source project, no code copied) |
| tree-sitter-python | 0.25.0 | MIT |
| tree-sitter-typescript | 0.23.2 | MIT |
| opentelemetry-api/sdk | 1.45.1 | Apache-2.0 |
| pytest | 8.4.2 | MIT |
| hypothesis | 6.170.11 | MPL-2.0 |
| ruff | 0.17.2 | MIT |
| shellingham | 1.5.4 | ISC |
| hatchling | build backend in lock | MIT upstream |

ForgeFlow itself is MIT. Dependency licenses are compatible with MIT distribution;
MPL-2.0 applies to Hypothesis only as a development/test dependency. No license text was
stripped or third-party code copied. `uv.lock` is the version record.

## External protocol verification

See `PROTOCOL_NOTES.md` for source URLs and VERIFIED/UNVERIFIED distinctions. Key
boundaries: MCP 2026-07-28 is stateless and uses per-request `_meta`; ACP v1 is
IDE↔agent, not an agent bus; Pi RPC has `agent_end` vs `agent_settled`; Tree-sitter is
syntax-only; Harbor is an evaluation framework.

## Risk register

See `TASKS.md` risks. The highest residual risks are non-container OS isolation, external
side-effect semantics, paid model quality not measured, and hardware/Harbor absence.

## Development commands

```bash
uv sync --extra dev
uv run forge --help
uv run forge doctor --json
uv run pytest -q
```

## G0 conclusion

The repository audit, ADRs, task DAG, acceptance matrix, protocol notes and license/
version inventory exist and match the implementation. Baseline was preserved.
