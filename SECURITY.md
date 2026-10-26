# Security

ForgeFlow treats coding agents as untrusted producers of code and claims. Please report
security issues privately to the repository owner rather than opening a public issue with
exploit details.

## Security model

- Agent self-report is never evidence.
- Commands run through an argv-only runner with workspace-confined cwd, environment
  allowlist and timeout/process-tree cleanup.
- Network, irreversible, shell and non-allowlisted commands require an approval bound to
  the exact request.
- Protected verifier files are anchored to the immutable goal baseline commit, restored
  from that commit in clean room, and checked even if an agent commits test weakening;
  test deletion and high-risk additions are rejected.
- Evidence binds source/protected/environment fingerprints; edits make gates STALE.
- Trace logs scrub credentials, prompts and code.
- ForgeFlow does not push, deploy or open public ports.
- Worktree cleanup removes only recorded ForgeFlow-owned worktrees.

## Explicit non-goals / limitations

- The local clean room is not an OS sandbox; use an external container/VM for hostile
  code once a real Docker execution path is implemented and verified.
- SHA-256 manifests are integrity checks, not signatures. There is no built-in signing
  trust root.
- External side effects are not exactly-once; side-effecting tasks require idempotency
  keys and manual reconciliation after unknown outcomes.
- Secrets present in the user's shell environment may still be inherited by a coding
  agent process; ForgeFlow allows only explicit verifier environment overrides, but does
  not isolate the parent environment for third-party agents.
