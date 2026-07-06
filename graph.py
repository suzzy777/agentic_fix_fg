from __future__ import annotations
import os
import re
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

@dataclass
class FuncDef:
    """A function / method definition."""
    name: str
    filepath: str          # absolute path
    start_line: int        # 1-based
    end_line: int
    source: str            # full source text of the function


@dataclass
class CallGraph:
    """
    Stores directed call relationships between functions.
    All names are plain function names (not qualified).
    """
    # name -> list of FuncDef (there may be multiple defs with same name)
    definitions: dict[str, list[FuncDef]] = field(default_factory=dict)
    # caller_name -> set of callee names
    calls: dict[str, set[str]] = field(default_factory=dict)

    def get_callees(self, func_name: str) -> list[FuncDef]:
        """Return all FuncDef objects called by func_name."""
        callee_names = self.calls.get(func_name, set())
        result: list[FuncDef] = []
        for name in callee_names:
            result.extend(self.definitions.get(name, []))
        return result

    def get_callers(self, func_name: str) -> list[FuncDef]:
        """Return all FuncDef objects that call func_name."""
        result: list[FuncDef] = []
        for caller, callees in self.calls.items():
            if func_name in callees:
                result.extend(self.definitions.get(caller, []))
        return result

    def get_def(self, func_name: str) -> list[FuncDef]:
        return self.definitions.get(func_name, [])


# Node types that represent function definitions per language
_FUNC_DEF_TYPES = {
    "go":     {"function_declaration", "method_declaration"},
    "python": {"function_definition"},
    "java":   {"method_declaration", "constructor_declaration"},
}

# Node types that represent function *calls*
_CALL_TYPES = {
    "go":     {"call_expression"},
    "python": {"call"},
    "java":   {"method_invocation", "object_creation_expression"},
}

# File extensions per language
_EXTENSIONS = {
    "go": ".go",
    "python": ".py",
    "java": ".java",
}


def _get_ts_language(language: str):
    """Load tree-sitter Language object for the given language string."""
    try:
        if language == "go":
            from tree_sitter_go import language as go_lang
            import tree_sitter as ts
            return ts.Language(go_lang())
        elif language == "python":
            from tree_sitter_python import language as py_lang
            import tree_sitter as ts
            return ts.Language(py_lang())
        elif language == "java":
            from tree_sitter_java import language as java_lang
            import tree_sitter as ts
            return ts.Language(java_lang())
        else:
            logger.warning("Unsupported language for tree-sitter: %s", language)
            return None
    except ImportError:
        logger.warning("tree-sitter bindings for '%s' not installed. Falling back to regex.", language)
        return None


def _parse_file(filepath: str, language: str):
    """Return (tree, source_bytes) or (None, None) on failure."""
    ts_lang = _get_ts_language(language)
    if ts_lang is None:
        return None, None
    try:
        import tree_sitter as ts
        parser = ts.Parser(ts_lang)
        with open(filepath, "rb") as f:
            source = f.read()
        return parser.parse(source), source
    except Exception as exc:
        logger.debug("Failed to parse %s: %s", filepath, exc)
        return None, None


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _get_func_name(node, language: str, source: bytes) -> str:
    """Extract function name from a function-definition node."""
    name_node = node.child_by_field_name("name")
    if name_node:
        return _node_text(name_node, source)
    # Java constructor: class name is the function name
    for child in node.children:
        if child.type == "identifier":
            return _node_text(child, source)
    return ""


def _walk(node, visitor, *args):
    """Depth-first walk calling visitor(node, *args)."""
    visitor(node, *args)
    for child in node.children:
        _walk(child, visitor, *args)


def _collect_calls(
    func_node,
    language: str,
    source: bytes,
) -> set[tuple[str, int, int]]:
    """Collect called function names together with their source-line ranges."""
    call_types = _CALL_TYPES.get(language, set())
    called: set[tuple[str, int, int]] = set()

    def visit(node):
        if node.type not in call_types:
            return

        # Go / Python: function field is 'function'; Java usually uses 'name'.
        fn = node.child_by_field_name("function") or node.child_by_field_name("name")
        if fn is None:
            return

        # For receiver.method(...), retain only the called method name.
        if fn.type in ("selector_expression", "member_expression", "field_access"):
            field_child = fn.child_by_field_name("field") or fn.child_by_field_name("member")
            if field_child:
                fn = field_child

        if fn.type == "identifier":
            called.add((
                _node_text(fn, source),
                node.start_point[0] + 1,
                node.end_point[0] + 1,
            ))

    _walk(func_node, visit)
    return called


# ── Regex fallback ────────────────────────────────────────────────────────────

def _regex_extract_go(filepath: str) -> tuple[list[FuncDef], dict[str, set[str]]]:
    """Minimal regex-based Go function extractor (used if tree-sitter unavailable)."""
    try:
        with open(filepath, "r", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return [], {}

    source = "".join(lines)
    defs: list[FuncDef] = []
    calls: dict[str, set[str]] = {}

    # Find function declarations
    func_re = re.compile(r'^func\s+(?:\([^)]+\)\s+)?(\w+)\s*\(', re.MULTILINE)
    call_re = re.compile(r'\b(\w+)\s*\(')

    boundaries = [(m.start(), m.group(1)) for m in func_re.finditer(source)]
    for i, (start_pos, name) in enumerate(boundaries):
        end_pos = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(source)
        body = source[start_pos:end_pos]
        start_line = source[:start_pos].count('\n') + 1
        end_line   = source[:end_pos].count('\n') + 1
        defs.append(FuncDef(name=name, filepath=filepath,
                            start_line=start_line, end_line=end_line, source=body))
        calls[name] = {m.group(1) for m in call_re.finditer(body)} - {name}

    return defs, calls


# ── Builder ───────────────────────────────────────────────────────────────────

class CallGraphBuilder:
    """
    Builds a static call graph for a set of source files.

    Args:
        repo_root: Absolute path to the repository root.
        files:     Iterable of file paths (absolute or relative to repo_root)
                   to include in the graph.
        language:  "go" | "python" | "java"
    """

    def __init__(
        self,
        repo_root: str,
        files: list[str],
        language: str = "go",
        covered_lines: dict[str, set[int]] | None = None,
        always_keep: set[tuple[str, str]] | None = None,
    ):
        self.repo_root = repo_root
        self.language = language.lower()
        self.covered_lines = {
            os.path.abspath(path): set(lines)
            for path, lines in (covered_lines or {}).items()
        }
        self.always_keep = {
            (os.path.abspath(path), name)
            for path, name in (always_keep or set())
        }

        ext = _EXTENSIONS.get(self.language, "")
        self.files: list[str] = []
        for f in files:
            path = f if os.path.isabs(f) else os.path.join(repo_root, f)
            path = os.path.abspath(path)
            if os.path.isfile(path) and (not ext or path.endswith(ext)):
                self.files.append(path)

    def _range_is_covered(
        self,
        filepath: str,
        start_line: int,
        end_line: int,
    ) -> bool:
        """Return whether runtime coverage touched this source range."""
        if not self.covered_lines:
            return True

        lines = self.covered_lines.get(os.path.abspath(filepath), set())
        return any(start_line <= line <= end_line for line in lines)

    def build(self) -> CallGraph:
        """Parse files and return a graph pruned by runtime-covered source lines."""
        graph = CallGraph()
        func_def_types = _FUNC_DEF_TYPES.get(self.language, set())
        definitions_seen = 0

        for filepath in self.files:
            tree, source = _parse_file(filepath, self.language)

            if tree is None:
                # Regex fallback remains available when no line-level filtering is used.
                if self.language == "go" and not self.covered_lines:
                    defs, file_calls = _regex_extract_go(filepath)
                    definitions_seen += len(defs)
                    for d in defs:
                        graph.definitions.setdefault(d.name, []).append(d)
                    for caller, callees in file_calls.items():
                        graph.calls.setdefault(caller, set()).update(callees)
                continue

            # Tree-sitter path
            def visit(node):
                nonlocal definitions_seen

                if node.type not in func_def_types:
                    return

                name = _get_func_name(node, self.language, source)
                if not name:
                    return

                definitions_seen += 1
                start_line = node.start_point[0] + 1
                end_line = node.end_point[0] + 1
                is_root = (os.path.abspath(filepath), name) in self.always_keep

                # Keep only methods whose source range was executed. The flaky test
                # root is retained explicitly because standard JaCoCo reports often
                # omit test classes from the analyzed class directories.
                if not is_root and not self._range_is_covered(
                    filepath,
                    start_line,
                    end_line,
                ):
                    return

                src_text = _node_text(node, source)
                fd = FuncDef(
                    name=name,
                    filepath=filepath,
                    start_line=start_line,
                    end_line=end_line,
                    source=src_text,
                )
                graph.definitions.setdefault(name, []).append(fd)

                for callee, call_start, call_end in _collect_calls(
                    node,
                    self.language,
                    source,
                ):
                    # Runtime-prune call-reference tags too. Calls in the explicitly
                    # retained test root are kept so BFS can enter the covered graph.
                    if is_root or self._range_is_covered(
                        filepath,
                        call_start,
                        call_end,
                    ):
                        graph.calls.setdefault(name, set()).add(callee)

            _walk(tree.root_node, visit)

        retained = sum(len(v) for v in graph.definitions.values())
        retained_files = len({
            definition.filepath
            for definitions in graph.definitions.values()
            for definition in definitions
        })

        if self.covered_lines:
            logger.info(
                "Coverage-pruned graph: %d/%d method definitions retained across %d/%d files.",
                retained,
                definitions_seen,
                retained_files,
                len(self.files),
            )
        else:
            logger.info(
                "Call graph: %d function definitions across %d files.",
                retained,
                len(self.files),
            )
        return graph


# ── Scope helpers ─────────────────────────────────────────────────────────────

def files_in_directory(root: str, language: str = "go", max_depth: int = 4) -> list[str]:
    """
    Collect all source files under `root` up to `max_depth` levels.
    Used when no coverage information is available.
    """
    ext = _EXTENSIONS.get(language.lower(), "")
    result: list[str] = []
    for dirpath, _, filenames in os.walk(root):
        depth = dirpath[len(root):].count(os.sep)
        if depth > max_depth:
            continue
        for fname in filenames:
            if fname.endswith(ext) and not fname.endswith("_test" + ext):
                result.append(os.path.join(dirpath, fname))
    return result


def files_near_test(test_file: str, up_levels: int = 2, language: str = "go") -> list[str]:
    """
    Collect source files within `up_levels` parent directories of `test_file`.
    Mirrors the paper's file_scope_relative heuristic.
    """
    base = os.path.dirname(os.path.abspath(test_file))
    for _ in range(up_levels):
        parent = os.path.dirname(base)
        if parent == base:
            break
        base = parent
    return files_in_directory(base, language=language, max_depth=up_levels + 1)
