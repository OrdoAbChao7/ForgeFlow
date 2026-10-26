# Pi RPC example

Handshake only (no model charge):

```bash
uv run python examples/pi_rpc/probe.py
```

A real bounded run requires explicit cost authorization:

```bash
uv run forge run <goal-id> --agent pi_rpc --allow-paid
```

The adapter uses `pi --mode rpc` JSONL stdin/stdout, correlates responses by request
ID, treats prompt acceptance as distinct from `agent_settled`, cancels through `abort`,
and reports a crashed process as `CRASHED` rather than success. If Pi is absent or
unauthenticated, the adapter is `EXTERNAL_BLOCKED`; fixture contract tests still run.
