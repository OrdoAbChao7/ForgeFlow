from __future__ import annotations

import os
import time
from pathlib import Path

from forgeflow.context.engine import ContextEngine, ContextWeights
from forgeflow.context.parser import parse_bytes
from forgeflow.domain.enums import GateKind
from forgeflow.domain.models import AcceptanceCriterion, GoalSpec, TaskSpec, VerifierConfig
from tests.conftest import init_repo


def _context_project(path: Path) -> Path:
    repo = init_repo(
        path,
        {
            "src/__init__.py": "",
            "src/alpha.py": (
                "class Alpha:\n"
                "    def compute(self, value):\n"
                "        return value + 1\n\n"
                "def unrelated():\n"
                "    return 42\n"
            ),
            "src/beta.py": (
                "from src.alpha import Alpha\n\n"
                "def use_compute():\n"
                "    return Alpha().compute(2)\n"
            ),
            "tests/test_alpha.py": (
                "from src.alpha import Alpha\n\n"
                "def test_compute():\n"
                "    assert Alpha().compute(2) == 3\n"
            ),
            "docs/CONTRACT.md": (
                "# Contract\n\nThe compute function must use CONTRACT_RULE_ALPHA.\n"
            ),
            "tests/__init__.py": "",
        },
    )
    return repo


def _spec() -> GoalSpec:
    return GoalSpec(
        title="Fix compute",
        objective="Repair compute in alpha according to CONTRACT_RULE_ALPHA.",
        acceptance=[
            AcceptanceCriterion(
                id="compute", statement="compute returns the correct value", gate=GateKind.UNIT
            )
        ],
        tasks=[
            TaskSpec(
                key="fix",
                title="Fix compute",
                description="Change Alpha.compute in src/alpha.py.",
                owned_paths=["src/alpha.py"],
                gates=[GateKind.UNIT],
                acceptance_ids=["compute"],
                metadata={"contract_files": ["docs/CONTRACT.md"]},
            )
        ],
        verifier=VerifierConfig(),
    )


def test_python_and_typescript_symbol_extraction() -> None:
    python = parse_bytes(
        "m.py",
        b"import os\n\nclass A:\n    def f(self, x):\n        return x\n\ndef g():\n    return 1\n",
        "python",
    )
    assert python.parser == "python"
    names = {symbol.name for symbol in python.symbols}
    assert {"A", "f", "g"}.issubset(names)
    assert any(symbol.parent == "A" for symbol in python.symbols)
    assert python.imports
    typescript = parse_bytes(
        "m.ts",
        b"export function f(x: number): number { return x; }\nclass C { m() {} }\n",
        "typescript",
    )
    assert typescript.parser == "typescript"
    assert {"f", "C", "m"}.issubset({symbol.name for symbol in typescript.symbols})


def test_text_fallback_is_explicit() -> None:
    parsed = parse_bytes("notes.rb", b"class Widget\nend\n", "text")
    assert parsed.parser == "text_fallback"
    assert any("text fallback" in error for error in parsed.errors)


def test_changed_symbols_contracts_and_explanations(tmp_path: Path) -> None:
    repo = _context_project(tmp_path / "repo")
    alpha = repo / "src" / "alpha.py"
    alpha.write_text(alpha.read_text().replace("return value + 1", "return value + 2"))
    engine = ContextEngine(repo)
    try:
        bundle = engine.build(_spec(), _spec().tasks[0], token_budget=800, mode="structure")
    finally:
        engine.close()
    paths = {chunk.path for chunk in bundle.chunks}
    assert "src/alpha.py" in paths
    assert "docs/CONTRACT.md" in paths
    compute = next(chunk for chunk in bundle.chunks if chunk.symbol == "compute")
    assert any("changed symbol" in reason for reason in compute.reasons)
    contract = next(chunk for chunk in bundle.chunks if chunk.path == "docs/CONTRACT.md")
    assert contract.forced
    assert "forced contract file" in contract.reasons
    assert bundle.used_tokens > 0
    assert bundle.fingerprint
    assert "ForgeFlow context bundle" in bundle.to_prompt()


def test_modes_and_budget(tmp_path: Path) -> None:
    repo = _context_project(tmp_path / "repo")
    alpha = repo / "src" / "alpha.py"
    alpha.write_text(alpha.read_text().replace("value + 1", "value + 2"))
    engine = ContextEngine(repo)
    spec = _spec()
    try:
        none = engine.build(spec, spec.tasks[0], mode="none")
        assert none.chunks == []
        diff_only = engine.build(spec, spec.tasks[0], mode="diff_only", token_budget=1000)
        assert diff_only.chunks
        assert all(chunk.path == "src/alpha.py" for chunk in diff_only.chunks)
        keyword = engine.build(spec, spec.tasks[0], mode="keyword", token_budget=1000)
        assert any("CONTRACT" in chunk.text for chunk in keyword.chunks)
        full = engine.build(spec, spec.tasks[0], mode="full_file", token_budget=1000)
        assert {chunk.path for chunk in full.chunks} >= {"src/alpha.py", "docs/CONTRACT.md"}
        tiny = engine.build(spec, spec.tasks[0], mode="structure", token_budget=64)
        assert tiny.used_tokens >= 1
        assert any("budget" in note for note in tiny.parser_notes) or tiny.omitted
    finally:
        engine.close()


def test_incremental_cache_invalidates(tmp_path: Path) -> None:
    repo = _context_project(tmp_path / "repo")
    engine = ContextEngine(repo)
    cache = engine.cache
    first = cache.get_or_parse(repo, "src/alpha.py", "python")
    cached = cache.get_or_parse(repo, "src/alpha.py", "python")
    assert first.sha256 == cached.sha256
    path = repo / "src" / "alpha.py"
    path.write_text(path.read_text() + "\n\ndef new_symbol():\n    return 3\n", encoding="utf-8")
    future = time.time() + 2
    os.utime(path, (future, future))
    changed = cache.get_or_parse(repo, "src/alpha.py", "python")
    assert any(symbol.name == "new_symbol" for symbol in changed.symbols)
    cache.invalidate("src/alpha.py")
    refreshed = cache.get_or_parse(repo, "src/alpha.py", "python")
    assert refreshed.sha256 == changed.sha256
    engine.close()


def test_weights_are_configurable(tmp_path: Path) -> None:
    repo = _context_project(tmp_path / "repo")
    engine = ContextEngine(repo)
    spec = _spec()
    try:
        heavy_contract = engine.build(
            spec,
            spec.tasks[0],
            mode="structure",
            token_budget=4000,
            weights=ContextWeights(contract=100.0, changed_symbol=0.5),
        )
    finally:
        engine.close()
    assert heavy_contract.chunks[0].forced
    assert heavy_contract.chunks[0].score >= 100
