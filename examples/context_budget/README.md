# Context budget example

```bash
uv run python examples/context_budget/run_demo.py --budget 600
```

Runs Tree-sitter symbol extraction and Git-diff ranking in five modes (`structure`,
`full_file`, `keyword`, `diff_only`, `none`) and prints selected line ranges, scores,
reasons, estimated tokens and omission reasons. This is a syntax-aware retrieval demo,
not a claim of a complete semantic call graph or an LSP implementation.
