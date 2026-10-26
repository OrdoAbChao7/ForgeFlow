from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import tree_sitter_python
    import tree_sitter_typescript
    from tree_sitter import Language, Node, Parser

    _TREE_SITTER_IMPORT_ERROR: str | None = None
except Exception as exc:  # pragma: no cover - exercised only when optional deps are absent
    tree_sitter_python = None
    tree_sitter_typescript = None
    Node = object  # type: ignore[assignment,misc]
    _TREE_SITTER_IMPORT_ERROR = str(exc)

LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".v": "verilog",
    ".sv": "systemverilog",
}


@dataclass
class Symbol:
    name: str
    kind: str
    start_line: int
    end_line: int
    start_byte: int
    end_byte: int
    signature: str
    parent: str | None = None
    doc: str = ""


@dataclass
class ParsedFile:
    path: str
    language: str
    parser: str
    sha256: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    line_count: int = 0

    def as_dict(self) -> dict:
        data = asdict(self)
        data["symbols"] = [asdict(symbol) for symbol in self.symbols]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> ParsedFile:
        return cls(
            path=data["path"],
            language=data["language"],
            parser=data["parser"],
            sha256=data["sha256"],
            symbols=[Symbol(**item) for item in data.get("symbols", [])],
            imports=list(data.get("imports", [])),
            errors=list(data.get("errors", [])),
            line_count=int(data.get("line_count", 0)),
        )


def _language(parser_name: str) -> tuple[Language, str] | None:
    if _TREE_SITTER_IMPORT_ERROR:
        return None
    try:
        if parser_name == "python":
            return Language(tree_sitter_python.language()), "python"
        if parser_name in {"typescript", "javascript"}:
            return Language(tree_sitter_typescript.language_typescript()), "typescript"
        if parser_name == "tsx":
            return Language(tree_sitter_typescript.language_tsx()), "tsx"
    except Exception:
        return None
    return None


def parse_bytes(path: str, data: bytes, parser_name: str) -> ParsedFile:
    digest = hashlib.sha256(data).hexdigest()
    language = LANGUAGE_BY_EXTENSION.get(Path(path).suffix.lower(), "unknown")
    selected = _language(parser_name)
    if selected is None:
        return _text_fallback(path, language, data, digest)
    lang, actual = selected
    try:
        parser = Parser(lang)
        tree = parser.parse(data)
    except Exception as exc:
        return _text_fallback(
            path, language, data, digest, error=f"tree-sitter parse failed: {exc}"
        )
    symbols: list[Symbol] = []
    imports: list[str] = []
    errors: list[str] = []

    def node_text(node: Node) -> str:
        return data[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    def first_line_signature(node: Node) -> str:
        body = node.child_by_field_name("body")
        end = body.start_byte if body is not None else node.end_byte
        text = data[node.start_byte : end].decode("utf-8", errors="replace")
        return re.sub(r"\s+", " ", text).strip()[:240]

    def find_parent_definition(node: Node) -> str | None:
        parent = node.parent
        while parent is not None:
            if parent.type in {
                "class_definition",
                "class_declaration",
                "interface_declaration",
            }:
                name_node = parent.child_by_field_name("name")
                if name_node is not None:
                    return data[name_node.start_byte : name_node.end_byte].decode(
                        "utf-8", errors="replace"
                    )
            parent = parent.parent
        return None

    if actual == "python":
        interesting = {
            "function_definition": "function",
            "class_definition": "class",
            "import_statement": "import",
            "import_from_statement": "import",
        }
    else:
        interesting = {
            "function_declaration": "function",
            "method_definition": "method",
            "class_declaration": "class",
            "interface_declaration": "interface",
            "type_alias_declaration": "type",
            "import_statement": "import",
            "lexical_declaration": "variable",
            "variable_declaration": "variable",
        }

    def visit(node: Node) -> None:
        if (
            actual == "python"
            and node.type == "assignment"
            and node.parent is not None
            and node.parent.type == "expression_statement"
            and node.parent.parent is not None
            and node.parent.parent.type == "module"
        ):
            left = node.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                name = data[left.start_byte : left.end_byte].decode("utf-8", errors="replace")
                statement = node.parent
                text = node_text(statement)
                symbols.append(
                    Symbol(
                        name=name,
                        kind="constant",
                        start_line=statement.start_point.row + 1,
                        end_line=statement.end_point.row + 1,
                        start_byte=statement.start_byte,
                        end_byte=statement.end_byte,
                        signature=text.strip().splitlines()[0][:240],
                        parent=None,
                        doc="module-level constant",
                    )
                )
            visit_children(node)
            return
        if node.type in interesting:
            text = node_text(node)
            if node.type in {"import_statement", "import_from_statement"}:
                imports.append(text.strip())
            else:
                name_node = node.child_by_field_name("name")
                if name_node is None and node.type in {
                    "lexical_declaration",
                    "variable_declaration",
                }:
                    # Only arrow functions / function expressions are symbols.
                    body = node_text(node)
                    if "=>" not in body and "function" not in body:
                        visit_children(node)
                        return
                if name_node is not None:
                    name = data[name_node.start_byte : name_node.end_byte].decode(
                        "utf-8", errors="replace"
                    )
                    body = node.child_by_field_name("body")
                    start_byte = node.start_byte
                    end_byte = node.end_byte
                    # Decorated Python definitions include decorators in the snippet.
                    if node.parent is not None and node.parent.type == "decorated_definition":
                        start_byte = node.parent.start_byte
                    symbols.append(
                        Symbol(
                            name=name,
                            kind=interesting[node.type],
                            start_line=node.start_point.row + 1,
                            end_line=node.end_point.row + 1,
                            start_byte=start_byte,
                            end_byte=end_byte,
                            signature=first_line_signature(node),
                            parent=find_parent_definition(node),
                            doc=(
                                "has body"
                                if body is not None
                                else text.strip()[:160]
                            ),
                        )
                    )
        visit_children(node)

    def visit_children(node: Node) -> None:
        for child in node.children:
            visit(child)

    visit(tree.root_node)
    if tree.root_node.has_error:
        errors.append("tree-sitter reported syntax errors; symbols may be partial")
    seen: set[tuple[int, int, str]] = set()
    unique_symbols = []
    for symbol in sorted(symbols, key=lambda item: (item.start_line, item.end_line, item.name)):
        key = (symbol.start_line, symbol.end_line, symbol.name)
        if key not in seen:
            seen.add(key)
            unique_symbols.append(symbol)
    return ParsedFile(
        path=path,
        language=language,
        parser=actual,
        sha256=digest,
        symbols=unique_symbols,
        imports=imports,
        errors=errors,
        line_count=data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0),
    )


def _text_fallback(
    path: str, language: str, data: bytes, digest: str, *, error: str | None = None
) -> ParsedFile:
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    symbols: list[Symbol] = []
    pattern = re.compile(
        r"^\s*(?:async\s+)?(class|def|function|interface|type|module|const|let|var)\s+([A-Za-z_$][\w$]*)"
    )
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match:
            symbols.append(
                Symbol(
                    name=match.group(2),
                    kind=match.group(1),
                    start_line=index + 1,
                    end_line=index + 1,
                    start_byte=sum(len(item.encode()) + 1 for item in lines[:index]),
                    end_byte=sum(len(item.encode()) + 1 for item in lines[: index + 1]),
                    signature=line.strip()[:240],
                )
            )
    if not symbols and text:
        symbols.append(
            Symbol(
                name=Path(path).stem,
                kind="file",
                start_line=1,
                end_line=max(1, len(lines)),
                start_byte=0,
                end_byte=len(data),
                signature=lines[0].strip()[:240] if lines else path,
            )
        )
    return ParsedFile(
        path=path,
        language=language,
        parser="text_fallback",
        sha256=digest,
        symbols=symbols,
        errors=[error] if error else ["no tree-sitter grammar; text fallback used"],
        line_count=len(lines),
    )
