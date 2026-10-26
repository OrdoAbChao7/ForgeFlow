from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from forgeflow.context.parser import LANGUAGE_BY_EXTENSION, ParsedFile, parse_bytes
from forgeflow.domain.models import GoalSpec, TaskSpec
from forgeflow.verification.fingerprint import workspace_files

_SOURCE_EXTENSIONS = set(LANGUAGE_BY_EXTENSION)
_TEXT_EXTENSIONS = {".md", ".txt", ".rst", ".toml", ".yaml", ".yml", ".json"}


@dataclass
class ContextWeights:
    changed_file: float = 5.0
    changed_symbol: float = 5.0
    task_reference: float = 3.0
    diff_reference: float = 3.0
    test_relevance: float = 4.0
    import_relation: float = 2.0
    owned_path: float = 2.0
    keyword: float = 1.0
    contract: float = 20.0


@dataclass
class ContextChunk:
    path: str
    start_line: int
    end_line: int
    text: str
    score: float
    reasons: list[str]
    symbol: str | None = None
    kind: str = "symbol"
    estimated_tokens: int = 0
    forced: bool = False
    truncated: bool = False


@dataclass
class OmittedChunk:
    path: str
    symbol: str | None
    reasons: list[str]
    estimated_tokens: int


@dataclass
class ContextBundle:
    mode: str
    budget: int
    used_tokens: int
    chunks: list[ContextChunk] = field(default_factory=list)
    omitted: list[OmittedChunk] = field(default_factory=list)
    files_considered: int = 0
    fingerprint: str = ""
    parser_notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "budget": self.budget,
            "used_tokens": self.used_tokens,
            "chunks": [asdict(chunk) for chunk in self.chunks],
            "omitted": [asdict(item) for item in self.omitted],
            "files_considered": self.files_considered,
            "fingerprint": self.fingerprint,
            "parser_notes": self.parser_notes,
        }

    def to_prompt(self) -> str:
        parts = [
            "## ForgeFlow context bundle",
            f"mode={self.mode} budget={self.budget} used~{self.used_tokens} tokens",
        ]
        for chunk in self.chunks:
            reasons = "; ".join(chunk.reasons[:4])
            parts.append(
                f"\n### {chunk.path}:{chunk.start_line}-{chunk.end_line}"
                f" ({chunk.symbol or chunk.kind}; score={chunk.score:.1f}; why: {reasons})\n"
                f"```\n{chunk.text}\n```"
            )
        if self.omitted:
            parts.append("\nOmitted: " + ", ".join(
                f"{item.path}:{item.symbol or '*'} ({item.reasons[0]})" for item in self.omitted[:10]
            ))
        return "\n".join(parts)


def estimate_tokens(text: str) -> int:
    """Conservative, model-independent approximation; never reported as exact usage."""
    return max(1, (len(text) + 3) // 4)


class ContextCache:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(self.path), timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS file_index(
                 path TEXT PRIMARY KEY,
                 mtime_ns INTEGER NOT NULL,
                 size INTEGER NOT NULL,
                 sha256 TEXT NOT NULL,
                 language TEXT NOT NULL,
                 parser TEXT NOT NULL,
                 data_json TEXT NOT NULL
               )"""
        )
        self.connection.commit()

    def get_or_parse(self, root: Path, relative: str, parser_name: str) -> ParsedFile:
        path = root / relative
        stat = path.stat()
        row = self.connection.execute(
            "SELECT * FROM file_index WHERE path=?", (relative,)
        ).fetchone()
        if row and row["mtime_ns"] == stat.st_mtime_ns and row["size"] == stat.st_size:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest == row["sha256"]:
                return ParsedFile.from_dict(json.loads(row["data_json"]))
        data = path.read_bytes()
        parsed = parse_bytes(relative, data, parser_name)
        self.connection.execute(
            """INSERT INTO file_index(path,mtime_ns,size,sha256,language,parser,data_json)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET mtime_ns=excluded.mtime_ns,size=excluded.size,
               sha256=excluded.sha256,language=excluded.language,parser=excluded.parser,
               data_json=excluded.data_json""",
            (
                relative,
                stat.st_mtime_ns,
                stat.st_size,
                parsed.sha256,
                parsed.language,
                parsed.parser,
                json.dumps(parsed.as_dict(), sort_keys=True),
            ),
        )
        self.connection.commit()
        return parsed

    def invalidate(self, relative: str | None = None) -> None:
        if relative:
            self.connection.execute("DELETE FROM file_index WHERE path=?", (relative,))
        else:
            self.connection.execute("DELETE FROM file_index")
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


class ContextEngine:
    def __init__(self, root: str | Path, *, cache_path: str | Path | None = None) -> None:
        self.root = Path(root).resolve()
        cache = Path(cache_path) if cache_path else self.root / ".forge" / "context_index.sqlite"
        self.cache = ContextCache(cache)

    def close(self) -> None:
        self.cache.close()

    def build(
        self,
        spec: GoalSpec,
        task: TaskSpec,
        *,
        token_budget: int = 4000,
        mode: str = "structure",
        weights: ContextWeights | None = None,
        changed_files: dict[str, set[int]] | None = None,
    ) -> ContextBundle:
        if token_budget < 64:
            raise ValueError("token budget must be at least 64")
        if mode not in {"structure", "full_file", "keyword", "diff_only", "none"}:
            raise ValueError(f"unsupported context mode: {mode}")
        weights = weights or ContextWeights()
        if mode == "none":
            return ContextBundle(mode=mode, budget=token_budget, used_tokens=0)
        changes = changed_files if changed_files is not None else self.git_changed_lines()
        task_text = " ".join(
            [task.title, task.description, spec.objective, *(item.statement for item in spec.acceptance)]
        )
        keywords = sorted(
            {word.lower() for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", task_text)}
        )
        contract_files = {
            str(item).replace("\\", "/")
            for item in spec.metadata.get("contract_files", [])
        } | {
            str(item).replace("\\", "/")
            for item in task.metadata.get("contract_files", [])
        }
        candidates: list[dict[str, Any]] = []
        parser_notes: list[str] = []
        changed_stems = {Path(path).stem for path in changes}
        changed_symbol_names: set[str] = set()
        changed_text = []
        parsed_by_path: dict[str, ParsedFile] = {}
        files = [
            (relative, path)
            for relative, path in workspace_files(self.root)
            if Path(relative).suffix.lower() in _SOURCE_EXTENSIONS
            or Path(relative).suffix.lower() in _TEXT_EXTENSIONS
            or relative.replace("\\", "/") in contract_files
        ]
        for relative, path in files:
            suffix = path.suffix.lower()
            parser_name = LANGUAGE_BY_EXTENSION.get(suffix, "text")
            try:
                parsed = self.cache.get_or_parse(self.root, relative, parser_name)
            except (OSError, UnicodeError) as exc:
                parser_notes.append(f"{relative}: unreadable ({exc})")
                continue
            parsed_by_path[relative] = parsed
            if relative in changes:
                for symbol in parsed.symbols:
                    if _range_intersects(
                        symbol.start_line, symbol.end_line, changes[relative]
                    ):
                        changed_symbol_names.add(symbol.name)
                if suffix in _SOURCE_EXTENSIONS:
                    try:
                        changed_text.append(path.read_text(encoding="utf-8", errors="replace"))
                    except OSError:
                        pass
        changed_text_joined = "\n".join(changed_text)
        imported_modules: set[str] = set()
        for relative in changes:
            parsed = parsed_by_path.get(relative)
            if not parsed:
                continue
            for line in parsed.imports:
                imported_modules.update(_import_tokens(line))
        for relative, parsed in parsed_by_path.items():
            path = self.root / relative
            suffix = Path(relative).suffix.lower()
            force = relative in contract_files
            is_changed = relative in changes
            is_test = bool(re.search(r"(^|/)(test_|tests?/|spec_)", relative))
            owned = any(
                relative == prefix.rstrip("/") or relative.startswith(prefix.rstrip("/") + "/")
                for prefix in task.owned_paths
            )
            reason_base: list[str] = []
            base_score = 0.0
            if is_changed:
                base_score += weights.changed_file
                reason_base.append("changed in git diff")
            if owned:
                base_score += weights.owned_path
                reason_base.append("within task-owned path")
            if force:
                base_score += weights.contract
                reason_base.append("forced contract file")
            if suffix in _SOURCE_EXTENSIONS:
                for symbol in parsed.symbols:
                    score = base_score
                    reasons = list(reason_base)
                    if is_changed and _range_intersects(
                        symbol.start_line, symbol.end_line, changes[relative]
                    ):
                        score += weights.changed_symbol
                        reasons.append("changed symbol/line range")
                    lname = symbol.name.lower()
                    if any(keyword == lname for keyword in keywords):
                        score += weights.task_reference
                        reasons.append("symbol named in task/contract")
                    if lname in changed_text_joined.lower():
                        score += weights.diff_reference
                        reasons.append("referenced by changed code")
                    if is_test and (lname in changed_symbol_names or Path(relative).stem in changed_stems):
                        score += weights.test_relevance
                        reasons.append("test related to changed symbol/file")
                    if any(
                        token in lname or lname in token
                        for token in imported_modules
                        if token and len(token) > 2
                    ):
                        score += weights.import_relation
                        reasons.append("import relationship")
                    if any(keyword in symbol.signature.lower() for keyword in keywords if len(keyword) > 4):
                        score += weights.keyword
                        reasons.append("signature keyword match")
                    if score <= 0 and not force:
                        continue
                    text = _slice_lines(path, symbol.start_line, symbol.end_line)
                    candidates.append(
                        {
                            "path": relative,
                            "start": symbol.start_line,
                            "end": symbol.end_line,
                            "text": text,
                            "score": score,
                            "reasons": reasons,
                            "symbol": symbol.name,
                            "kind": symbol.kind,
                            "forced": force,
                        }
                    )
            else:
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                lowered = text.lower()
                keyword_hits = sum(lowered.count(keyword) for keyword in keywords)
                if keyword_hits:
                    base_score += weights.keyword * min(keyword_hits, 10)
                    reason_base.append(f"{keyword_hits} keyword hits")
                if force or is_changed or owned:
                    candidates.append(
                        {
                            "path": relative,
                            "start": 1,
                            "end": max(1, parsed.line_count),
                            "text": text,
                            "score": base_score,
                            "reasons": reason_base,
                            "symbol": None,
                            "kind": "file",
                            "forced": force,
                        }
                    )
        if mode == "full_file":
            candidates = self._file_level(candidates, parsed_by_path, changes, keywords, weights, task)
        elif mode == "keyword":
            candidates = self._keyword_level(parsed_by_path, changes, keywords, weights, contract_files)
        elif mode == "diff_only":
            candidates = [
                candidate
                for candidate in candidates
                if candidate["path"] in changes
            ]
            for candidate in candidates:
                candidate["score"] += 100.0
                candidate["reasons"] = ["diff-only mode"] + candidate["reasons"]
        candidates.sort(
            key=lambda item: (
                -item["score"],
                item["end"] - item["start"],
                item["path"],
                item["start"],
            )
        )
        selected: list[ContextChunk] = []
        omitted: list[OmittedChunk] = []
        used = 0
        selected_ranges: dict[str, list[tuple[int, int]]] = {}
        for candidate in candidates:
            tokens = estimate_tokens(candidate["text"])
            overlapping = any(
                not (candidate["end"] < start or candidate["start"] > end)
                for start, end in selected_ranges.get(candidate["path"], [])
            )
            if overlapping and not candidate["forced"]:
                omitted.append(
                    OmittedChunk(
                        path=candidate["path"],
                        symbol=candidate["symbol"],
                        reasons=["overlaps a higher-ranked selected chunk"],
                        estimated_tokens=tokens,
                    )
                )
                continue
            remaining = token_budget - used
            if tokens > remaining and not candidate["forced"]:
                if remaining >= 80:
                    text = candidate["text"][: remaining * 4]
                    truncated = True
                    tokens = estimate_tokens(text)
                    reasons = candidate["reasons"] + ["truncated to remaining token budget"]
                else:
                    omitted.append(
                        OmittedChunk(
                            path=candidate["path"],
                            symbol=candidate["symbol"],
                            reasons=["token budget exhausted"],
                            estimated_tokens=tokens,
                        )
                    )
                    continue
            else:
                text = candidate["text"]
                truncated = False
                reasons = candidate["reasons"]
            chunk = ContextChunk(
                path=candidate["path"],
                start_line=candidate["start"],
                end_line=candidate["end"],
                text=text,
                score=round(candidate["score"], 3),
                reasons=reasons,
                symbol=candidate["symbol"],
                kind=candidate["kind"],
                estimated_tokens=estimate_tokens(text),
                forced=bool(candidate["forced"]),
                truncated=truncated,
            )
            selected.append(chunk)
            used += chunk.estimated_tokens
            selected_ranges.setdefault(candidate["path"], []).append(
                (candidate["start"], candidate["end"])
            )
        if used > token_budget:
            parser_notes.append(
                f"forced contract files exceeded budget by {used - token_budget} estimated tokens"
            )
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "mode": mode,
                    "budget": token_budget,
                    "changes": {path: sorted(lines) for path, lines in changes.items()},
                    "selected": [(chunk.path, chunk.start_line, chunk.end_line) for chunk in selected],
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return ContextBundle(
            mode=mode,
            budget=token_budget,
            used_tokens=used,
            chunks=selected,
            omitted=omitted,
            files_considered=len(files),
            fingerprint=fingerprint,
            parser_notes=parser_notes,
        )

    def _file_level(
        self,
        candidates: list[dict[str, Any]],
        parsed_by_path: dict[str, ParsedFile],
        changes: dict[str, set[int]],
        keywords: list[str],
        weights: ContextWeights,
        task: TaskSpec,
    ) -> list[dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            current = grouped.get(candidate["path"])
            if current is None:
                current = {
                    **candidate,
                    "score": candidate["score"],
                    "reasons": list(candidate["reasons"]),
                    "symbol": None,
                    "kind": "file",
                    "forced": candidate["forced"],
                }
                parsed = parsed_by_path.get(candidate["path"])
                try:
                    text = (self.root / candidate["path"]).read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    text = candidate["text"]
                current["text"] = text
                current["start"] = 1
                current["end"] = max(1, parsed.line_count if parsed else 1)
                grouped[candidate["path"]] = current
        return list(grouped.values())

    def _keyword_level(
        self,
        parsed_by_path: dict[str, ParsedFile],
        changes: dict[str, set[int]],
        keywords: list[str],
        weights: ContextWeights,
        contract_files: set[str],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for relative, parsed in parsed_by_path.items():
            try:
                text = (self.root / relative).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            lowered = text.lower()
            hits = sum(lowered.count(keyword) for keyword in keywords)
            if not hits and relative not in contract_files:
                continue
            score = hits * weights.keyword
            reasons = [f"{hits} keyword hits"]
            if relative in changes:
                score += weights.changed_file
                reasons.append("changed file")
            if relative in contract_files:
                score += weights.contract
                reasons.append("forced contract file")
            candidates.append(
                {
                    "path": relative,
                    "start": 1,
                    "end": max(1, parsed.line_count),
                    "text": text,
                    "score": score,
                    "reasons": reasons,
                    "symbol": None,
                    "kind": "file",
                    "forced": relative in contract_files,
                }
            )
        return candidates

    def git_changed_lines(self) -> dict[str, set[int]]:
        import subprocess

        if not (self.root / ".git").exists():
            return {}
        diff = subprocess.run(
            ["git", "diff", "--no-color", "--unified=0", "HEAD"],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        if diff.returncode != 0:
            return {}
        changes: dict[str, set[int]] = {}
        current: str | None = None
        for line in diff.stdout.splitlines():
            if line.startswith("+++ b/"):
                current = line[6:]
                changes.setdefault(current, set())
            elif line.startswith("@@") and current:
                match = re.search(r"\+(\d+)(?:,(\d+))?", line)
                if match:
                    start = int(match.group(1))
                    count = int(match.group(2) or "1")
                    changes[current].update(range(start, start + max(count, 1)))
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        for relative in untracked.stdout.splitlines():
            path = self.root / relative
            if path.is_file():
                try:
                    count = path.read_bytes().count(b"\n") + 1
                except OSError:
                    count = 1
                changes.setdefault(relative, set()).update(range(1, count + 1))
        return changes


def _range_intersects(start: int, end: int, changed: set[int]) -> bool:
    return any(start <= line <= end for line in changed)


def _slice_lines(path: Path, start: int, end: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[max(0, start - 1) : end])


def _import_tokens(line: str) -> set[str]:
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", line))
    stop = {"import", "from", "as", "const", "require", "type"}
    return {token for token in tokens if token not in stop}
