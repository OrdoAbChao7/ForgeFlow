# Parallel worktree example

```bash
uv run python examples/parallel_tasks/run_demo.py
```

Runs two independent file-owned tasks in isolated Git worktrees with at most two
workers, serial integration, and a post-merge regression callback. Overlapping file
ownership is serialized by default; conflicts can be retried by rebase and fall back to
a serial worker. Cleanup removes only worktrees recorded in `.forge/worktrees.json`.
