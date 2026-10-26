# ForgeFlow 2.0 Adapter Protocol Notes

**Verification date:** 2026-10-26
**Status:** Research notes, not a specification. Facts are separated into VERIFIED (observed in the cited source on the date above) and UNVERIFIED / UNCERTAIN (inference, version-limited, or not directly sourced).

**Method.** Official documentation was fetched directly (some fetches via `curl`/`gh` because the browser-based fetch/search tooling was unavailable or blocked for those domains). The locally installed Pi 1.3.0 package was inspected as a cross-check. No source code was executed and no instructions from fetched or local content were followed. Local Pi documentation is treated as data.

**Legend**

- **VERIFIED** — stated explicitly in the cited official/local documentation on 2026-10-26.
- **UNVERIFIED** — not found, only implied, or dependent on an unstated version.
- **Adapter implication** — engineering consequence for ForgeFlow, not a protocol fact.

---

## 1. Pi `--mode rpc`

**Versions and sources**

- Public docs (verified reachable 2026-10-26):
  - <https://pi.dev/docs/latest/rpc>
  - <https://pi.dev/docs/latest/rpc-commands>
  - <https://pi.dev/docs/latest/json>
- Local install: `C:/Users/32583/.pi/agent/install/current-version` contains `1.3.0`.
- Local docs cross-checked:
  - `C:/Users/32583/.pi/agent/install/releases/1.3.0/node_modules/@earendil-works/pi-coding-agent/docs/rpc.md`
  - `.../docs/rpc-commands.md`
  - `.../docs/json.md`

### VERIFIED

**Process and framing**

- RPC mode starts a long-lived subprocess: `pi --mode rpc --no-session`.
- Normal CLI options still select working folder, model, tools, resources, and session behavior. `pi --help` is authoritative for the installed version.
- RPC mode rejects `@file` prompt arguments; prompts must be sent through the `prompt` command.
- Four record families:
  - stdin: **Command** (prompt, inspect state, change configuration, session management)
  - stdout: **`response`** (success/data for one command)
  - stdout: **session event** (run/message/tool/queue/compaction/retry activity)
  - both directions: **extension UI record**
- Strict JSONL framing:
  - One complete JSON object per record, terminated with LF (`\n`).
  - Split records only on LF; strip an optional preceding CR to accept CRLF.
  - Do **not** use a generic line reader that treats Unicode separators as record boundaries. Node.js `readline` also splits on `U+2028`/`U+2029`, which are valid inside JSON strings.
  - Read stdout continuously. Pi honors stdout backpressure, but a client that stops reading can stall the process. Honor stdin backpressure when writing.
  - stdout is reserved for protocol records; diagnostics/logging go to stderr.

**Request IDs**

- Every command accepts an optional string `id`.
- A matching response repeats it:

```json
{"id":"req-1","type":"get_state"}
{"id":"req-1","type":"response","command":"get_state","success":true,"data":{"...":"..."}}
```

- Command handling is asynchronous; correlate by ID, not by response order.
- Session events generally carry no command ID.
- `bash_execution_update` is the exception: when the originating `bash` command has an ID, its output events repeat that ID.
- An `extension_ui_response` uses the ID supplied by its `extension_ui_request` and does not produce a normal command response.

**Command surface** (from the documented command list; each has a typed response)

- Prompting/queueing: `prompt`, `steer`, `follow_up`, `abort`, `clear_queue`.
- Session: `new_session`, `switch_session`, `fork`, `clone`, `get_fork_messages`, `get_entries`, `get_tree`, `get_last_assistant_text`, `set_session_name`, `get_session_stats`, `export_html`.
- State/config: `get_state`, `get_messages`, `set_model`, `cycle_model`, `get_available_models`, `set_thinking_level`, `cycle_thinking_level`, `get_available_thinking_levels`, `set_steering_mode`, `set_follow_up_mode`, `set_auto_compaction`, `set_auto_retry`, `abort_retry`, `compact`.
- Tools/shell: `bash`, `abort_bash`.
- Introspection: `get_commands`.

**Prompt acceptance vs completion**

- A successful `prompt` response means the prompt was accepted, queued, or handled. It does **not** mean model work completed.
- `data.disposition` values:
  - `"started"` — accepted to start a run.
  - `"queued"` — queued during an active run.
  - `"handled"` — an extension command or input handler consumed it; no run started, so do not wait for `agent_settled`.
- `success: false` means rejected before acceptance. Failures after acceptance are reported through the event/message stream, not as a second response for the same request ID.

**Run lifecycle and event names**

Representative stream:

```json
{"type":"agent_start"}
{"type":"turn_start"}
{"type":"message_start","message":{"role":"user","content":"Review this repository","timestamp":1733234401000}}
{"type":"message_end","message":{"role":"user","content":"Review this repository","timestamp":1733234401000}}
{"type":"message_start","message":{"role":"assistant","content":[],"stopReason":"pending","...":"..."}}
{"type":"message_update","usage":{"...":"..."},"assistantMessageEvent":{"type":"text_delta","contentIndex":0,"delta":"Hello"}}
{"type":"message_end","message":{"role":"assistant","...":"..."}}
{"type":"turn_end","message":{"role":"assistant","...":"..."},"toolResults":[]}
{"type":"agent_end","messages":[{"...":"..."}],"willRetry":false}
{"type":"agent_settled","aborted":false}
```

- `agent_end` (`messages`, `willRetry`) closes **one low-level agent run**. Retries, overflow recovery, compaction retry, steering, or follow-up work can still continue.
- `agent_settled` (`aborted`) means Pi has **no remaining automatic work** for that session-level run: no retries, compaction recovery, or queued messages. `aborted` is true when the run ended because it was aborted.
- `turn_start` / `turn_end` (`message`, `toolResults`) delimit one assistant response plus its tool calls and results.
- `message_start` / `message_end` (`message`) delimit a message; `message_end.message` is authoritative.
- `message_update` (`usage`, `assistantMessageEvent`) is delta-only on the wire; it omits cumulative message/partial snapshots. Nested types include:
  - `start`, `text_start`, `text_delta`, `text_end`, `thinking_start`, `thinking_delta`, `thinking_end`
  - `toolcall_start` (`id`, `toolName`), `toolcall_delta`, `toolcall_end` (`toolCall`)
  - `done`, `error`
- Deltas carry `contentIndex`. Buffer deltas for live display, but replace reconstructed data with the completed content in `text_end`, `thinking_end`, or `toolcall_end`, and replace the whole partial message with `message_end.message`.
- Tool events:
  - `tool_execution_start` (`toolCallId`, `toolName`, `args`)
  - `tool_execution_update` (`toolCallId`, `toolName`, `args`, `partialResult`)
  - `tool_execution_end` (`toolCallId`, `toolName`, `result`, `isError`, `durationMs`)
- Other session events:
  - `queue_update`, `entry_appended`, `session_info_changed`, `thinking_level_changed`
  - `compaction_start` / `compaction_end` with reason `manual | threshold | overflow`
  - `auto_retry_start` (`attempt`, `maxAttempts`, `delayMs`, `errorMessage`) / `auto_retry_end` (`success`, `attempt`, optional `finalError`)
  - `summarization_retry_scheduled`, `summarization_retry_attempt_start`, `summarization_retry_finished`
- RPC-only records: `bash_execution_update` (repeats command ID) and `extension_error`.

**Errors**

- A failed command returns exactly one response with `success: false` and an `error` string:

```json
{"id":"req-3","type":"response","command":"set_model","success":false,"error":"Model not found: invalid/model"}
```

- Malformed JSON produces a parse response with **no request ID**:

```json
{"type":"response","command":"parse","success":false,"error":"Failed to parse command: Unexpected token..."}
```

- Provider failures and aborts after prompt acceptance appear in the message/event stream.
- Clients must also handle child-process startup failures, unexpected exits, stderr diagnostics, cancellation, and their own deadlines. Do not parse stderr as protocol data.

**Shutdown**

- Close the child's stdin to request an orderly shutdown.
- Pi disposes the active runtime before exiting.
- An extension can also request shutdown through its extension context.
- Pi completes shutdown after the current command or after the active run emits `agent_settled`.
- Clients must still handle process signals and unexpected exits.

**Public/local agreement**

- The public `pi.dev/docs/latest/*` pages and the installed 1.3.0 docs agree on framing, request-ID correlation, prompt dispositions, `agent_end` vs `agent_settled`, parse-error behavior, and shutdown. Specific event field lists above were cross-checked against the public JSON event page.

### UNVERIFIED / UNCERTAIN

- **Cross-version stability.** Public URLs are `/docs/latest`, not version-pinned. The docs explicitly say the installed version's `pi --help` is authoritative. Whether event names, fields, or dispositions change between 1.x releases is not documented here.
- **Exhaustive event list.** The document records the names found in the current public/local `json.md`; extensions may add records. `extension_error` and extension UI records were not exercised.
- **Backpressure limits / maximum record size.** Not documented in the pages read.
- **Behavior when a client writes multiple commands before reading any output.** Only "read stdout continuously" and stdin backpressure are documented.
- **Exact semantics of `disposition` under concurrent steer/follow-up and cancellation.** Only the three values and their broad meanings are documented.

### Adapter implications

- Spawn one process per session; never use a shell pipeline that could inject text into stdout.
- Implement a byte-level LF splitter with CR stripping; do not use platform line readers that treat U+2028/U+2029 as newlines.
- Use a monotonically increasing command ID; keep a pending map. Treat the ID-less `command:"parse"` response as a transport-level fatal/degraded condition, not as a response to any request.
- Model completion at **`agent_settled`**, not `agent_end` and not the `prompt` response. If `disposition == "handled"`, do not wait.
- Normalize `message_update` deltas into ForgeFlow streaming events, but reconcile from `text_end`/`toolcall_end`/`message_end` to avoid drift.
- Map `tool_execution_*` to tool spans/call records using `toolCallId` as the correlation key.
- Reserve stderr for logs/diagnostics and surface unexpected exit as an adapter error distinct from a protocol error.
- Shutdown by closing stdin, then wait; escalate to signals/termination only after a deadline.
- Keep the Pi version in adapter metadata and gate any field-specific parsing on it.

---

## 2. Agent Client Protocol (ACP) v1

**Sources**

- <https://agentclientprotocol.com/get-started/introduction.md>
- <https://agentclientprotocol.com/get-started/architecture.md>
- <https://agentclientprotocol.com/protocol/v1/overview.md>
- <https://agentclientprotocol.com/protocol/v1/initialization.md>
- <https://agentclientprotocol.com/protocol/v1/session-setup.md>
- <https://agentclientprotocol.com/protocol/v1/prompt-turn.md>
- <https://agentclientprotocol.com/protocol/v1/transports.md>
- <https://agentclientprotocol.com/protocol/v1/schema.md>
- <https://agentclientprotocol.com/llms.txt> (index; shows v1 stable and a v2 draft)

### VERIFIED

**What it is**

- ACP standardizes communication between **code editors/IDEs and coding agents**.
- Local agents run as subprocesses of the client (the editor/IDE). Remote agents over HTTP/WebSocket are described as work in progress.
- JSON-RPC 2.0: methods use request/response; notifications are one-way.
- `llms.txt` lists v1 as the stable protocol and a v2 draft. These notes cover **v1 only**.

**Transport and framing**

- Baseline stdio transport: the client launches the agent as a subprocess.
- JSON-RPC messages travel on stdin/stdout, **newline-delimited**, and each message MUST NOT contain embedded newlines.
- stderr is for UTF-8 logs; the agent MUST NOT write non-ACP data to stdout; the client MUST NOT write non-ACP data to stdin.
- A Streamable HTTP transport is a draft; custom transports are allowed.

**Version negotiation**

- `initialize` request includes `protocolVersion: 1`, `clientCapabilities`, and `clientInfo`.
- Response includes `protocolVersion: 1`, `agentCapabilities`, `agentInfo`, and `authMethods`.
- Client MUST send the latest version it supports. If the agent supports it, the agent MUST respond with the same version; otherwise it responds with the latest version it supports. The client SHOULD close the connection if it cannot support the returned version.

**Capabilities**

- Omitted capability = unsupported. New capabilities are designed to be non-breaking.
- Baseline prompt content an agent MUST support: `ContentBlock::Text` and `ContentBlock::ResourceLink`.
- Optional prompt content capabilities include `image`, `audio`, and `embeddedContext`.
- MCP transport capabilities include `http` and `sse` (SSE is deprecated by MCP).
- Session loading is gated by the `loadSession` capability; resuming is gated by `sessionCapabilities.resume`.

**Flow**

1. `initialize`
2. `authenticate` if the agent requires it
3. `session/new` (or `session/load` / `session/resume`)
4. `session/prompt`
5. Agent streams `session/update` notifications
6. Optional `session/request_permission` from agent to client
7. `session/prompt` response with a `stopReason`
8. `session/cancel` (notification) to interrupt

- `session/new` params include `cwd` and `mcpServers` (`{name, command, args, env}`); response contains `sessionId`.
- `session/load` replays conversation history through `session/update` notifications before responding.
- `session/resume` restores context **without** replaying history and reconnects requested MCP servers.
- `session/prompt` params are `sessionId` and `prompt: ContentBlock[]`.
- `session/update` variants (complete set in v1):
  `user_message_chunk`, `agent_message_chunk`, `agent_thought_chunk`, `tool_call`, `tool_call_update`, `plan`, `available_commands_update`, `current_mode_update`, `config_option_update`, `session_info_update`, `usage_update`, `notice`, `compaction_update`, `compaction_summary_chunk`.
- `stopReason` values: `end_turn`, `max_tokens`, `max_turn_requests`, `refusal`, `cancelled`.
- On `session/cancel`, the agent MUST respond to all pending `session/request_permission` requests with the `cancelled` outcome and MUST eventually respond to the original `session/prompt` with `stopReason: "cancelled"`. Agents MUST catch library abort exceptions and translate them to the `cancelled` stop reason.

**Schema conventions**

- All file paths are absolute; line numbers are 1-based.
- JSON keys are camelCase; discriminator values are snake_case.
- `_meta` carries extension fields; `_`-prefixed custom methods are reserved for extensions.

**Why it is not an agent-to-agent protocol**

- The introduction defines ACP as standardizing **editor/IDE ↔ coding agent** communication.
- The architecture has the editor boot the agent subprocess; the **client** provides filesystem, terminal, and permission services.
- `session/request_permission`, `fs/*`, and `terminal/*` are client-side methods: the agent asks the host, not another agent.
- There is no discovery, delegation, task handoff, peer identity, or agent-to-agent message routing in v1. The protocol's topology is a host with one connected agent session (multiple sessions are host-managed, not peer-to-peer).

### UNVERIFIED / UNCERTAIN

- ACP **v2 draft** was not reviewed; do not assume v1 compatibility.
- Remote/HTTP transport is explicitly work in progress; production behavior is not established.
- Exact `agentCapabilities`/`clientCapabilities` schemas beyond the documented examples were not exhaustively enumerated.
- Whether two agents can be connected to one client session simultaneously is not addressed in the pages read; the architecture is host↔agent.

### Adapter implications

- ForgeFlow should treat ACP as an **agent-hosting adapter** (ForgeFlow acts as ACP client/editor), not as its internal agent-to-agent bus.
- Implement `initialize` with strict version negotiation and close on unsupported versions.
- Use `session/new` with an absolute `cwd`; pass MCP servers through the ACP `mcpServers` field rather than inventing a side channel.
- Correlate `tool_call`/`tool_call_update` by tool-call ID; permission requests must be answered or cancelled explicitly.
- Normalize `session/update` variants to the same internal event model used for Pi, and map `stopReason` to ForgeFlow termination reasons.
- Use `session/load` only when history replay is desired; use `session/resume` to continue without replay.
- Keep the ACP version (`1`) and capability set in adapter metadata.

---

## 3. Model Context Protocol revision `2026-07-28`

**Sources**

- <https://modelcontextprotocol.io/specification/2026-07-28>
- <https://modelcontextprotocol.io/specification/2026-07-28/basic>
- <https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning>
- <https://modelcontextprotocol.io/specification/2026-07-28/basic/transports>
- <https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio>
- <https://modelcontextprotocol.io/specification/2026-07-28/server/discover>
- <https://modelcontextprotocol.io/specification/2026-07-28/server/tools>
- <https://modelcontextprotocol.io/specification/2026-07-28/server/resources>
- <https://modelcontextprotocol.io/specification/2026-07-28/server/prompts>
- <https://modelcontextprotocol.io/specification/2026-07-28/changelog>
- <https://modelcontextprotocol.io/extensions/tasks/overview>

### VERIFIED

**Status and core model**

- `2026-07-28` is the current protocol revision documented by the official versioning page.
- The core is **stateless**:
  - No protocol-level sessions and no `Mcp-Session-Id`.
  - No `initialize` / `notifications/initialized` handshake for modern clients.
  - Every request carries per-request protocol fields in `_meta`:
    - `io.modelcontextprotocol/protocolVersion` (required, e.g. `"2026-07-28"`)
    - `io.modelcontextprotocol/clientCapabilities` (required)
    - `io.modelcontextprotocol/clientInfo` (optional; SHOULD be included)
  - Servers SHOULD include `io.modelcontextprotocol/serverInfo` in every result's `_meta`.
  - A connection/process is not a conversation or session; all state needed to process a request must be in the request or addressable by the server.
- A request missing a required `_meta` field is malformed: server MUST reject with JSON-RPC `-32602` (`Invalid params`), HTTP `400`.
- If processing requires a client capability not declared, server MUST return `MissingRequiredClientCapabilityError` (`-32021`) with `data.requiredCapabilities`.

**Version negotiation**

- No handshake. Version is negotiated per request.
- If the requested version is unsupported, the server returns `UnsupportedProtocolVersionError`, code `-32022`, with `data.supported` and `data.requested`.
- The client retries with a mutually supported version.
- `server/discover`:
  - Mandatory for servers, optional for clients.
  - Returns `supportedVersions`, `capabilities`, `serverInfo`, `instructions`, `ttlMs`, and `cacheScope`.
- On stdio, clients SHOULD send `server/discover` first when they need deterministic behavior against an unknown/legacy server. If the server instead responds as a legacy server, the client MAY fall back to the `initialize` handshake; it must **not** blindly fall back when the error is `UnsupportedProtocolVersionError` with a usable version list.

**JSON-RPC rules**

- JSON-RPC 2.0. Request IDs are strings or integers, not `null`, and unique among outstanding requests.
- Results MUST include `resultType`:
  - `"complete"` — request completed.
  - `"input_required"` — incomplete; result is an `InputRequiredResult` (MRTR flow).
  - Unknown `resultType` MUST be treated as invalid.
  - Absent `resultType` MUST be treated as `"complete"` for backward compatibility with earlier servers.
- Notifications MUST NOT have an ID.

**Methods verified for this revision**

- Discovery: `server/discover`
- Tools: `tools/list`, `tools/call`, `notifications/tools/list_changed`
- Resources: `resources/list`, `resources/read`, `resources/templates/list`, `notifications/resources/list_changed`, `notifications/resources/updated`
- Prompts: `prompts/list`, `prompts/get`, `notifications/prompts/list_changed`
- Subscriptions: `subscriptions/listen` (replaces HTTP GET and the old `resources/subscribe`/`unsubscribe`)
- Common notifications: `notifications/cancelled`, `notifications/progress`, `notifications/message`
- Tasks extension: `tasks/get`, `tasks/update`, `tasks/cancel`, optional `notifications/tasks`

**Changelog facts relevant to adapters**

- Removed protocol sessions and `Mcp-Session-Id`.
- Added `server/discover` and per-request version/capability fields.
- Replaced HTTP GET and `resources/subscribe`/`resources/unsubscribe` with `subscriptions/listen`.
- Removed `ping`, `logging/setLevel`, and `notifications/roots/list_changed`.
- Moved tasks to an extension; removed `tasks/result` and `tasks/list`.
- Introduced MRTR / `InputRequiredResult` and required `resultType`.
- Removed SSE resumability.
- Added `extensions` to capabilities.
- Added W3C trace context (`traceparent`/`tracestate`/`baggage`) in `_meta`.
- Tool order is deterministic.
- Added `ttlMs`/`cacheScope` on tool/prompt/resource list/read results.
- Resource-not-found changed from `-32002` to `-32602`.
- Roots, Sampling, Logging, HTTP+SSE, and OAuth DCR are deprecated.

**Capabilities**

- Server capabilities:
  - `tools: {listChanged}`
  - `resources: {listChanged, subscribe}` (`subscribe` covers resource-specific updates through `subscriptions/listen` filters)
  - `prompts: {listChanged}`
  - `extensions`
- Client capabilities are sent per request in `_meta`, not negotiated once.
- `extensions` is a map keyed by extension identifier.

**Tasks extension (optional)**

- Extension identifier: `io.modelcontextprotocol/tasks`.
- Advertised in client per-request capabilities and in server discover capabilities.
- A long-running request may return `resultType: "task"` with a `CreateTaskResult` containing `taskId`, `ttlMs`, and `pollIntervalMs`.
- The task must be durably created before the response is sent.
- Default consumption is polling via `tasks/get`, respecting `pollIntervalMs`.
- If a task moves to `input_required`, the client fulfills requests with `tasks/update`.
- `tasks/cancel` requests cancellation; cancellation is cooperative.
- Servers may optionally push state with `notifications/tasks`; clients opt in.
- Task statuses: `working`, `input_required`, `completed`, `failed`, `cancelled`.

**stdio framing**

- Newline-delimited JSON-RPC; messages MUST NOT contain embedded newlines.
- stdout carries only MCP messages; logs go to stderr.
- Client sends requests and notifications; server sends responses and notifications. In this revision servers do not initiate JSON-RPC requests to clients over stdio.
- Cancellation uses `notifications/cancelled`.
- Shutdown: close stdin, wait for exit, then force terminate if needed.
- Restart on unexpected exit is a client responsibility.
- For legacy compatibility, a client SHOULD probe with `server/discover` before other requests; a legacy server may reject it, time out, or respond with an implementation-defined error, after which the client may fall back to `initialize`.

### UNVERIFIED / UNCERTAIN

- `completion/complete` was not verified against the `2026-07-28` pages read; do not assume it is present or shaped as in earlier revisions.
- The full MRTR state machine (`InputRequiredResult` → client input → retry semantics) was only partially read.
- Exact server/client capability schemas beyond tools/resources/prompts/extensions were not exhaustively enumerated.
- Legacy/dual-era server behavior is documented as a compatibility matrix, but real-world servers will vary; probe empirically.
- Whether all SDKs have adopted the stateless core is out of scope.

### Adapter implications

- Do not implement an MCP `initialize` handshake for modern servers. Send `_meta` on every request and validate `resultType`.
- Implement `server/discover` first on stdio, with a timeout, and support the legacy fallback path.
- Handle `-32022` by retrying with a mutually supported version; handle `-32021` as a capability/config error, not a transport failure.
- Treat every MCP result as potentially `input_required`; the adapter must be able to pause a tool call and resume it after client input.
- Do not rely on sessions, `Mcp-Session-Id`, HTTP GET streams, `ping`, or `logging/setLevel`.
- Use `subscriptions/listen` for resource/tool/prompt changes rather than the removed subscribe methods.
- Propagate W3C trace context via `_meta` so MCP calls join ForgeFlow's OTel traces.
- Cache list/read results using `ttlMs`/`cacheScope`; do not assume infinite freshness.
- Keep the negotiated revision and task-extension use in adapter metadata.

---

## 4. Claude Code CLI non-interactive / stream-json

**Sources**

- <https://code.claude.com/docs/en/cli-reference.md>
- <https://code.claude.com/docs/en/headless.md>
- <https://code.claude.com/docs/en/agent-sdk/streaming-output.md>
- <https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode.md>
- <https://code.claude.com/docs/en/agent-sdk/permissions.md>
- <https://code.claude.com/docs/en/permission-modes.md>
- Local `claude --version` on 2026-10-26: `2.1.310`.

### VERIFIED

**Invocation and output formats**

- `-p, --print` runs non-interactively.
- `--output-format text|json|stream-json` (print mode only).
  - `text` (default): plain text.
  - `json`: structured JSON with result, session ID, and metadata. `total_cost_usd` and a per-model cost breakdown are included. With `--json-schema`, the structured value is in `structured_output`; if the run ends without valid structured output, the field is absent.
  - `stream-json`: newline-delimited JSON, one event object per line.
- `--input-format text|stream-json` (print mode only).
- `--include-partial-messages` requires `--print` and `--output-format stream-json`; it adds `stream_event` records carrying raw Claude API streaming events.
- `--verbose` shows full turn-by-turn output. Every official `stream-json` example pairs it with `stream-json`, but the docs do not explicitly state that omission is rejected.

**Sessions**

- `--no-session-persistence` disables session persistence; sessions are not saved and cannot be resumed. Print mode only. `CLAUDE_CODE_SKIP_PROMPT_HISTORY` does the same in any mode.
- `--session-id <uuid>` uses a specific session ID (must be a valid UUID).
- `--resume`, `--continue`, and `--fork-session` resume/continue/re-key sessions.
- `system/init` and other `system` events carry `session_id`; `json` output includes `session_id`.

**Permissions**

- `-p` does not imply a permission mode. Without a flag, a new session starts in the documented default mode.
- `--permission-mode` accepts `default`, `acceptEdits`, `plan`, `auto`, `dontAsk`, `bypassPermissions`, and `manual` (alias for `default`). `claude --help` lists `manual` in place of `default`; both work.
- `--dangerously-skip-permissions` is equivalent to `--permission-mode bypassPermissions`.
- `--allow-dangerously-skip-permissions` adds bypass mode to the cycle without starting in it.
- `--allowedTools` / `--allowed-tools` and `--disallowedTools` / `--disallowed-tools` add allow/deny rules. A bare tool name removes the tool from context; a scoped rule such as `Bash(rm *)` keeps the tool and denies matching calls.
- `--tools ""` disables all built-in tools; `--tools "Bash,Edit,Read"` restricts to named built-ins. This does not affect MCP tools; use `--disallowedTools "mcp__*"` for those.
- `--permission-prompt-tool mcp__<server>__<tool>` routes permission prompts to an MCP tool in non-interactive mode.
- `--permission-prompts host|none` (print mode, v2.1.259+): `host` sends prompts to the Agent SDK host or permission-prompt tool; `none` denies them instead of waiting. With `--output-format stream-json`, denials appear as `permission_denied` system messages, and the final result lists them in `permission_denials`.

**Other print-mode limits**

- `--max-turns` limits agentic turns; exits with an error when reached. With `--input-format stream-json`, a message still queued when a turn limit ends stays queued and starts a new turn with its own limit.
- `--max-budget-usd` stops the run at an estimated spend cap.
- Exit code 0 on success, non-zero on failure. Invalid flags are reported to stderr before the run starts; in-run failures (e.g. missing authentication) are printed as the result on stdout.
- SIGTERM exits with code 143 and leaves the in-progress turn unfinished with no result; SIGINT ends the turn instead.
- Piped stdin is capped at 10 MB.

**Stream-json shape**

- Each line is a JSON object. The last line is a `result` message with final response text, cost, and session metadata.
- Event/message types verified:
  - `system` with subtypes including `init`, `api_retry`, `plugin_install`, `compact_boundary`, and `permission_denied`.
  - `assistant` — complete content blocks; multiple messages per response, one per non-empty block.
  - `user` — user/tool-result messages.
  - `result` — final result; includes `permission_denials` when applicable.
  - `stream_event` — only with `--include-partial-messages`; wraps raw Claude API events.
  - `prompt_suggestion` — only with `--prompt-suggestions`.
- `system/init` is the first event unless startup events precede it (`plugin_install`, or `hook_started`/`hook_progress`/`hook_response` for SessionStart/Setup hooks). It reports model, tools, MCP servers, and loaded plugins, and may carry a `capabilities` array for feature detection (`interrupt_receipt_v1`, `interrupt_cancel_queued_v1`, …). Unknown capability values should be ignored.
- Subagent messages appear as `assistant`/`user` with `parent_tool_use_id`; main-conversation messages carry `null`. A forked skill started from the prompt uses a `forked-command-` prefix. `--forward-subagent-text` is required to forward subagent text/thinking blocks; without it, only tool_use/tool_result blocks are forwarded.
- `system/api_retry` fields: `type: "system"`, `subtype: "api_retry"`, `attempt`, `max_retries`, `retry_delay_ms`, `error_status` (int or null), optional `no_response`, `error` category, `uuid`, `session_id`.
- `stream_event.event` is a raw Claude API event: `message_start`, `content_block_start`, `content_block_delta`, `content_block_stop`, `message_delta`, `message_stop`. Delta types include `text_delta` (`text`) and `input_json_delta` (`partial_json`).
- Stream events are emitted for the main session only; subagent token deltas are not forwarded. Use complete messages with `parent_tool_use_id` for subagent attribution.
- On a cut stream, `message_stop` still arrives; a cut text/thinking block gets `content_block_stop`, but a cut tool call does not, so its input must be treated as incomplete.

**Streaming input**

- With `--input-format stream-json`, stdin accepts user message envelopes of the form:

```json
{"type":"user","message":{"role":"user","content":"Analyze this codebase for security issues"}}
```

- `content` can also be an array of content blocks (text, image).
- `--replay-user-messages` re-emits stdin user messages on stdout for acknowledgment; requires stream-json input and output.

### UNVERIFIED / UNCERTAIN

- Whether `--verbose` is a hard requirement for `--output-format stream-json` (docs only pair them).
- The complete `result` object schema (exact field names beyond `result`, `session_id`, `total_cost_usd`, `permission_denials`, `structured_output`) was not enumerated.
- The full set of `system` subtypes (e.g. compact boundary variants, hook events) was not exhaustively verified.
- Exact CLI stdin envelope validation rules (e.g. required `parent_tool_use_id`) are documented in SDK terms, not as a CLI schema.
- Behavior differences across Claude Code versions before 2.1.310 were not tested; several features are version-gated in the docs.

### Adapter implications

- Use `claude -p --output-format stream-json --verbose --input-format stream-json --no-session-persistence` for a stateless, reconfigurable adapter; add `--session-id` only when ForgeFlow wants deterministic correlation.
- Read stdout line-by-line as JSON; treat `result` as the terminal record, and `system/init` as the readiness/metadata record.
- Do not assume `assistant` messages correspond one-to-one with turns; they correspond to content blocks.
- Correlate tool calls via `tool_use` block IDs and tool results via `tool_result` blocks; correlate subagents via `parent_tool_use_id`.
- Bridge permissions explicitly: choose a permission mode, an allow/deny list, and either `--permission-prompts none` (auto-deny) or an MCP permission-prompt tool. Do not rely on interactive prompts in `-p`.
- Handle non-zero exit separate from protocol errors; on SIGTERM the last turn has no result.
- Treat `stream_event` as optional token-level telemetry; build canonical state from complete messages.
- Record the CLI version and feature-gated flags in adapter metadata.

---

## 5. OpenTelemetry GenAI conventions and Harbor

### 5a. OpenTelemetry GenAI semantic conventions

**Sources**

- <https://github.com/open-telemetry/semantic-conventions-genai>
- `docs/gen-ai/client-inference.md`
- `docs/gen-ai/gen-ai-spans.md`
- `docs/gen-ai/gen-ai-agent-spans.md`
- `docs/gen-ai/gen-ai-metrics.md`
- `docs/gen-ai/gen-ai-events.md`
- `docs/gen-ai/gen-ai-exceptions.md`
- `docs/gen-ai/mcp.md`
- The former `opentelemetry.io/docs/specs/semconv/gen-ai/*` pages now point to this repository.

### VERIFIED

- The GenAI semantic conventions live in `open-telemetry/semantic-conventions-genai`; their document status is **Development**, not stable.
- GenAI spans:
  - Inference: name `{gen_ai.operation.name} {gen_ai.request.model}`; kind `CLIENT` (MAY be `INTERNAL` in some cases).
    - Required: `gen_ai.operation.name`, `gen_ai.provider.name`.
    - Conditionally required: `gen_ai.conversation.id`, `gen_ai.request.model`, `gen_ai.request.stream`, `gen_ai.response.finish_reasons`.
    - Recommended request: `temperature`, `max_tokens`, `top_p`, `top_k`, `frequency_penalty`, `presence_penalty`, `stop_sequences`, `seed`, `choice.count`.
    - Recommended response: `response.id`, `response.model`, `time_to_first_chunk`.
    - Recommended usage: `input_tokens`, `output_tokens`, `cache_read.input_tokens`, `cache_write.input_tokens`, `reasoning.output_tokens`, modality-specific variants.
    - Opt-in content: `gen_ai.input.messages`, `gen_ai.output.messages`, `gen_ai.system_instructions`, `gen_ai.tool.definitions`.
    - `error.type` is conditional.
  - Agent spans:
    - `create_agent {gen_ai.agent.name}` — CLIENT.
    - `invoke_agent {gen_ai.agent.name}` — CLIENT, with an INTERNAL variant.
    - `plan {gen_ai.agent.name}` — INTERNAL.
    - `invoke_workflow {gen_ai.workflow.name}` — INTERNAL.
    - Attributes: `gen_ai.agent.id`, `gen_ai.agent.name`, `gen_ai.agent.description`, `gen_ai.agent.version`.
  - Tool spans:
    - `execute_tool {gen_ai.tool.name} ...` — INTERNAL.
    - Required: `gen_ai.tool.name`.
    - Conditional: `gen_ai.agent.name`, `gen_ai.tool.call.id`.
    - Recommended: `gen_ai.tool.description`, `gen_ai.tool.type`.
    - Opt-in: `gen_ai.tool.call.arguments`, `gen_ai.tool.call.result`; `error.type`.
- `gen_ai.operation.name` well-known values include `chat`, `generate_content`, `text_completion`, `embeddings`, `execute_tool`, `invoke_agent`, `create_agent`, `invoke_workflow`, `plan`, `retrieval`, `fetch_response`, and memory operations.
- Metrics include:
  - `gen_ai.client.operation.duration`
  - `gen_ai.server.request.duration`
  - `gen_ai.server.time_per_output_token`
  - `gen_ai.server.time_to_first_token`
  - `gen_ai.invoke_workflow.duration`
  - `gen_ai.invoke_agent.duration`
  - `gen_ai.invoke_agent.inference_calls`
  - `gen_ai.invoke_agent.tool_calls`
  - `gen_ai.execute_tool.duration`
  - Inference token metrics are separate (`gen_ai.client.inference.usage.*`, `gen_ai.client.inference.operation.*`).
- Event `gen_ai.evaluation.result`:
  - Required: `gen_ai.evaluation.name`.
  - Conditional: `gen_ai.evaluation.score.label`, `gen_ai.evaluation.score.value`.
  - Recommended: `explanation`, `gen_ai.response.id`.
- An MCP semantic-convention document exists. MCP `2026-07-28` supports W3C `traceparent`, `tracestate`, and `baggage` in `_meta` for OTel trace context.

### UNVERIFIED / UNCERTAIN

- The conventions are in Development status; attribute names and required/optional levels may change.
- The exact span kind for every server-side or framework-specific variant was not enumerated.
- Whether ForgeFlow should emit `invoke_agent` CLIENT vs INTERNAL depends on deployment topology and was not decided by the source.
- Content attributes (`gen_ai.input.messages`, etc.) are opt-in and may be privacy-sensitive; no default is specified here.

### Adapter implications

- Instrument adapter runs as `invoke_agent` (CLIENT when ForgeFlow calls an external agent process; INTERNAL when ForgeFlow owns the loop).
- Create one `execute_tool` span per normalized tool call, keyed by the protocol's tool-call ID (`toolCallId` in Pi, tool-call ID in ACP, `tool_use` ID in Claude Code).
- Attach `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.agent.name`, and `gen_ai.conversation.id` consistently across adapters.
- Emit `gen_ai.evaluation.result` events from Harbor/verifier outcomes.
- Propagate W3C trace context into MCP `_meta` and, where possible, into subprocess environment or protocol metadata.
- Gate content capture behind an explicit privacy setting; do not emit prompts/tool results by default.

### 5b. Harbor

**Sources**

- <https://github.com/harbor-framework/harbor>
- <https://docs.harborframework.com/llms.txt>
- `getting-started/quick-start.md`
- `core-concepts/index.md`
- `jobs/run-a-job.md`
- `jobs/configs.md`
- `agents/pre-integrated-agents.md`
- `agents/custom-agents.md`
- `agents/acp.md`
- `hosted-harbor/api.md`
- `hosted-harbor/submitting-jobs.md`
- `hosted-harbor/cli.md`

### VERIFIED

**Purpose**

- Harbor is a Python framework "from the creators of Terminal-Bench for evaluating and optimizing agents and language models."
- It is the official harness for Terminal-Bench-2.0.
- It is **an evaluation/optimization framework**, not the CNCF container image registry that shares the name.
- Uses:
  - Evaluate arbitrary agents such as Claude Code, OpenHands, and Codex CLI.
  - Build and share benchmarks/environments.
  - Run thousands of environments in parallel via Daytona, Modal, LangSmith, Blaxel, Novita, Tensorlake, Runta, and Sail.
  - Generate RL rollouts.
- Topics on the repo include `evals`, `rl-environments`, and `terminal-bench`.

**Core concepts**

- **Task**: instructions + sandbox environment + verifier.
- **Dataset**: collection of tasks, usually a benchmark (e.g. Terminal-Bench, SWE-Bench Verified).
- **Agent**: the system under evaluation.
- **Sandbox**: execution environment.
- **Verifier**: evaluates the agent's work and produces the task reward.
- **Trial**: one agent run on one task.
- **Job**: collection of trials combining datasets, agents, tasks, and models.
- **Trajectory**: recorded run, with **ATIF** as the trajectory format.

**CLI (verified forms)**

```bash
harbor run --dataset terminal-bench@2.0 --agent claude-code --model anthropic/claude-opus-5-5 --n-concurrent 4
harbor run -p "<path>" -a "<agent>" -m "<model>"
harbor run -d "<org/dataset>@<ref>" -a "<agent>" -m "<model>"
harbor run -t "<org/task>@<ref>" -a "<agent>" -m "<model>"
harbor datasets list
harbor view ./jobs
harbor upload "./jobs/<job-id>"
harbor auth login
```

- Installation: `uv tool install harbor` or `pip install harbor`.

**Python API (verified example shape)**

```python
from harbor.job import Job
from harbor.models.job.config import JobConfig
from harbor.models.trial.config import AgentConfig, TaskConfig

job = await Job.create(JobConfig(
    agents=[AgentConfig(name="codex", model_name="openai/gpt-5.6-sol")],
    tasks=[TaskConfig(name="hello-world/hello-world")],
))
await job.run()
```

**Custom agents**

- `BaseInstalledAgent`: Harbor installs and runs the agent CLI inside the task environment.
- `BaseAgent`: the agent loop runs outside the environment and controls a `BaseEnvironment`.
- Documented methods/hooks include `name()`, `version()`, `setup()`, `run(instruction, environment, context)`, `exec_as_root`, `exec_as_agent`, `with_prompt_template`, and `populate_context_post_run`.
- Custom agent selection: `--agent module.path:ClassName`; kwargs via `--ak`/`--agent-kwarg`; env via `--ae`/`--agent-env`.

**Pre-integrated agents**

- The pre-integrated list includes both `claude-code` and `pi`.
- Capability matrix includes ATIF trajectories, resume, MCP servers, and skills. `pi` supports ATIF, resume, MCP servers, and skills; `claude-code` supports ATIF, resume, loading native/ATIF trajectories, handoff, native configuration, ACP bridge, MCP servers, skills, HTTP MCP headers, and native web tools. Pi converts its active session branch to ATIF.

**ACP integration**

- Generic `acp` installs and runs agents from the ACP Registry inside the task environment: `--agent acp:<agent_id>[@version]`.
- Options include `auth_policy`, `authenticate_method_id`, `permission_mode` (default `allow`), `distribution_preference` (`binary,npx,uvx`), `registry_ref`, `registry_cache_dir`, and `registry_entry_path`.
- Outputs include `acp.txt`, `acp-events.jsonl`, `acp-summary.json`, and `trajectory.json` (ATIF).

**Hosted Harbor Hub API**

- Base URL: `https://ofhuhcpkvzjlejydnvyd.supabase.co/functions/v1`
- Auth: `Authorization: Bearer sk-harbor-...`
- `POST /job-submit`
- Requires an `Idempotency-Key` header (max 200 chars).
- Body: `config`, optional `organization`, `job_secrets`, `registry_credentials`, `dry_run`.
- Response: `job_id`, `job_name`, `viewer_url`, `status_url`, `n_trials`, `owner_org`, `agent_sources`.
- Errors: `{"error":{"code","message"}}`; codes include `bad_request` (400), `validation_failed` (400), `unauthorized` (401), `forbidden` (403).

### UNVERIFIED / UNCERTAIN

- Exact `jobs/configs.md` schema (all fields and defaults) was not fully read.
- Hosted API endpoint coverage beyond `job-submit` was not enumerated.
- Whether every pre-integrated agent supports every capability in the matrix was only read from the table, not exercised.
- Performance/scaling claims (thousands of parallel environments) are vendor claims, not independently measured.

### Adapter implications

- ForgeFlow should integrate Harbor as an **evaluation harness**, not as an execution runtime for production agent calls.
- Prefer Harbor's native `pi` and `claude-code` integrations or the generic `acp` integration over writing a new harness.
- Use Harbor tasks/datasets/verifiers to score adapters; emit `gen_ai.evaluation.result` from verifier outcomes.
- Consume ATIF trajectories as the evaluation record; map ForgeFlow's normalized event stream to ATIF where possible.
- Use the hosted API only with an idempotency key and explicit secrets handling; keep API keys out of logs.
- Treat Sandbox/Verifier as untrusted execution: isolate credentials and network policy.

---

## 6. Tree-sitter

**Sources**

- <https://tree-sitter.github.io/tree-sitter/>
- <https://tree-sitter.github.io/tree-sitter/using-parsers/>
- <https://tree-sitter.github.io/tree-sitter/creating-parsers/>
- <https://github.com/tree-sitter/tree-sitter/blob/master/docs/src/index.md>
- <https://github.com/tree-sitter/tree-sitter/blob/master/docs/src/using-parsers/1-getting-started.md>
- <https://github.com/tree-sitter/tree-sitter/blob/master/docs/src/4-code-navigation.md>

### VERIFIED

- Definition: "Tree-sitter is a parser generator tool and an incremental parsing library. It can build a concrete syntax tree for a source file and efficiently update the syntax tree as the source file is edited."
- Aims: general enough for any language, fast enough per keystroke, robust with syntax errors, dependency-free pure C11 runtime embeddable in any application.
- Official bindings:
  - C#
  - Go
  - Haskell
  - Java (JDK 22+)
  - JavaScript (Node.js)
  - JavaScript (Wasm)
  - Kotlin
  - Python
  - Rust
  - Swift
  - Zig
- Third-party bindings:
  - C# (.NET), C++, Crystal, D, Delphi, ELisp, Go, Guile, Janet, Java (JDK 8+), Java (JDK 11+), Julia, Lua (two), OCaml, Odin, Perl, Pharo, PHP, R, Ruby.
  - Upstream notes some may be incomplete or out of date.
- Upstream parsers include Agda, Bash, C, C++, C#, CSS, ERB/EJS, Go, Haskell, HTML, Java, JavaScript, and others.
- The API is syntax-tree based: languages, parsers, `TSTree` (whole file), `TSNode` (start/end positions, children, type). The documented code-navigation system identifies "interesting syntactic nodes" and tags definitions/references syntactically.

### UNVERIFIED / UNCERTAIN

- Tree-sitter performs **no name binding, type inference, or semantic resolution**. This is not stated as a single sentence in the pages read; it follows from the documented API and from "concrete syntax tree"/"syntactic nodes." Treat it as an architectural fact, not a quoted guarantee.
- Parser coverage and binding completeness vary by language and by third-party maintenance.
- Incremental parsing behavior under arbitrary edits was not benchmarked.
- Grammar-level error recovery is robust but produces error nodes; the exact recovery behavior is grammar-dependent.

### Adapter implications

- Use Tree-sitter for fast, syntax-level code intelligence: symbol outlines, definition/reference tagging by query, structural diffs, and edit-aware re-parsing.
- Do not use it as a substitute for a compiler/LSP when ForgeFlow needs types, resolved references, or semantic navigation. Pair it with an LSP or language server for semantic queries.
- Pin grammar versions per language; parser ABI and grammar changes can alter node shapes.
- Treat query captures (`@definition.*`, `@reference.*`, `@name`) as syntax-derived labels, not semantic truth.
- Budget for error nodes and partial trees; never assume a parse is complete or error-free.

---

## Summary matrix

| Area | Verified level | Protocol shape | Primary adapter role | Key uncertainty |
|---|---|---|---|---|
| Pi `--mode rpc` | Strong: public docs + local 1.3.0 agree | Long-lived subprocess, strict JSONL, command/response + events, `agent_settled` | Spawn, frame, correlate IDs, normalize events | Cross-version stability; extension records |
| ACP v1 | Strong for v1 docs | JSON-RPC 2.0 over NDJSON stdio, editor↔agent sessions | Act as ACP client/host | v2 draft; remote transport WIP |
| MCP `2026-07-28` | Strong for core + stdio + tasks | Stateless JSON-RPC, per-request `_meta`, `resultType`, optional tasks | Tool/resource client; discover + version retry | `completion/complete`; MRTR details |
| Claude Code CLI | Strong for flags/stream-json shape; version 2.1.310 | `-p` + NDJSON stream-json, `system`/`assistant`/`user`/`result` | Spawn, parse lines, bridge permissions | `--verbose` requirement; full result schema |
| OTel GenAI | Strong for names/shapes; Development status | Spans/metrics/events, `gen_ai.*` | Instrumentation boundary | Stability of Development conventions |
| Harbor | Strong for purpose/CLI/API | Python framework + CLI + hosted REST | Evaluation harness | Full config schema; vendor claims |
| Tree-sitter | Strong for bindings/API; syntax-only is derived | C11 runtime, CST, incremental parsing | Syntax-level code intelligence | No semantic layer; grammar variance |

---

## Cross-cutting adapter implications

1. **Completion is protocol-specific.** Pi: `agent_settled`; ACP: `session/prompt` response with `stopReason`; Claude Code: final `result` record; MCP: `resultType` plus task status. Do not unify on process exit.
2. **Correlation IDs differ.** Pi command `id` + `toolCallId`; ACP JSON-RPC `id` + tool-call ID; Claude Code `session_id` + `tool_use` ID + `parent_tool_use_id`; MCP JSON-RPC `id` + `progressToken`/task `taskId`. Normalize into a ForgeFlow run/turn/tool-call hierarchy.
3. **Permissions are host-side.** ACP has `session/request_permission`; Claude Code has permission modes and prompt routing; Pi has extension UI records. ForgeFlow needs one permission broker mapped to all three.
4. **Streaming models differ.** Pi sends delta-only `message_update` with authoritative `*_end` records; ACP sends typed `session/update` variants; Claude Code sends complete messages plus optional raw `stream_event` deltas. Reconcile from authoritative records, not deltas.
5. **Version negotiation differs.** ACP negotiates once in `initialize`; MCP negotiates per request; Pi and Claude Code rely on installed CLI version + documented flags. Store all four in adapter metadata.
6. **Evaluation is separate.** Harbor consumes agents/tasks and produces trials/ATIF; it is not the runtime protocol. Keep evaluation orchestration out of the adapter transport layer.
7. **Observability is cross-cutting.** Instrument at adapter boundaries with OTel GenAI conventions, propagate trace context into MCP `_meta`, and avoid content capture by default.

---

## Blocked or not fully verified

- **WebSearch** returned `API Error: 400 Model does not support this protocol` for all queries; **WebFetch** failed for `agentclientprotocol.com` and `github.com`. Direct `curl`/`gh` access was used instead, so the cited text is still official source content.
- **Pi cross-version behavior** beyond 1.3.0 is not verified; public docs are `/docs/latest`.
- **ACP v2 draft** and remote transports were not reviewed.
- **MCP `completion/complete`** and full MRTR semantics were not verified for `2026-07-28`.
- **Claude Code** `--verbose` hard requirement and complete `result` schema were not verified.
- **Tree-sitter "syntax-only"** is a derived conclusion from the documented concrete-syntax-tree API, not a direct quotation.
- **Harbor** full job-config schema and hosted endpoint coverage were not exhaustively read.
