# Clean-room demo

```bash
uv run python examples/basic_goal/run_demo.py --clean-room
```

This builds a private-index Git snapshot of the worktree, extracts it into a temporary
directory, restores protected test files from HEAD, reruns the trusted verifier, and
records `clean_room` evidence. Docker is not required; if Docker is requested and its
daemon is unavailable the adapter raises `EXTERNAL_BLOCKED` rather than emulating a
container.
