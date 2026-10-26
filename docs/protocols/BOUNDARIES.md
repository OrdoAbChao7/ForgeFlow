# Protocol boundaries

Verified 2026-10-26; details and sources in `docs/forgeflow-v2/PROTOCOL_NOTES.md`.

| Boundary | ForgeFlow use | Not used for | External status |
|---|---|---|---|
| Pi RPC | coding agent over `pi --mode rpc` JSONL | verification authority | handshake PASS; paid completion NOT_RUN |
| Claude Code CLI | alternative coding agent via print/stream-json | verification authority | contract PASS; paid completion NOT_RUN |
| MCP 2026-07-28 | stateless tool adapter, discovery, optional tasks | scheduler/session store | modern + legacy fixture tests PASS |
| ACP v1 | optional IDE/agent host boundary | agent-to-agent delegation | mock contract PASS |
| OTel GenAI | local spans/usage attributes | raw prompt/code retention | local exporter PASS |
| Harbor | external evaluation bridge | local scheduler | EXTERNAL_BLOCKED (CLI absent) |
| Tree-sitter | syntax-aware context | semantic call graph/LSP | Python + TypeScript fixtures PASS |

MCP and ACP messages are untrusted data. Tool calls require an allowlist or explicit
approval callback; ACP permission requests are denied by default with an explicit
`cancelled` outcome.
