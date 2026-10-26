# Basic goal demo

Runs a disposable Git project with a broken `add()` function, a mock coding agent, a
real `pytest` verifier, evidence/gate evaluation, optional clean-room reverification,
and a delivery manifest.

```bash
uv run python examples/basic_goal/run_demo.py
uv run python examples/basic_goal/run_demo.py --clean-room
```

The agent is explicitly a deterministic mock. A successful result is only issued after
the independent pytest command exits 0 and evidence is bound to the current source and
environment fingerprints.
