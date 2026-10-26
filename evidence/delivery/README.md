# Delivery evidence

`demo/` is produced by:

```bash
uv run python scripts/make_demo_delivery.py
```

The command creates a disposable Git project with a real pytest verifier, runs the
deterministic mock agent, verifies independently, and emits `manifest.json`,
`manifest.md`, `manifest.sha256`, `log_archive.json` and archived runner logs.

`manifest.json` is the authoritative `forge deliver` output. Its evidence log paths may
refer to the disposable workspace; `log_archive.json` maps those evidence IDs to retained
copies and SHA-256 hashes. A READY manifest is not a cryptographic signature and does not
cover the external blockers listed in `docs/forgeflow-v2/STATUS.md`.
