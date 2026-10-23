"""
utils/simplifier.py – table-driven test simplification.

The paper (§III-D) notes that Go tests heavily use "table-driven testing"
where one test function contains many similar test cases in a slice/table.
Without simplification, LLMs often focus on the wrong case.

This module:
  1. Extracts only the target test case from the table (simplification).
  2. Provides the original full function separately so the LLM's edits
     can be transplanted back (patch transplantation is handled in editing/).

For Python, a similar approach applies to parametrize-decorated tests.
For Java, parameterized test methods are simplified similarly.

The simplification is done with regex rather than full AST manipulation,
making it language-portable and easy to understand.  It may not handle
every edge case that the original Bazel/AST-based approach does, but it
is sufficient for a research baseline.
"""

from __future__ import annotations
import re
import logging

logger = logging.getLogger(__name__)


# ── Go table-driven test simplification ──────────────────────────────────────

# Pattern: t.Run("case name", func(t *testing.T) { ... })
# We keep only the t.Run block whose name matches `test_case`.
_GO_TRUN_RE = re.compile(
    r't\.Run\s*\(\s*"([^"]+)"\s*,\s*func\s*\([^)]*\)\s*\{',
    re.MULTILINE,
)

# Pattern: struct literal slice that defines a test table
# e.g.: tests := []struct{ ... }{ {name: "case1", ...}, ... }
_GO_TABLE_SLICE_RE = re.compile(
    r'(\w+)\s*:=\s*\[\]struct\s*\{[^{]*\}\s*\{',
    re.DOTALL,
)


def _find_matching_brace_end(source: str, open_pos: int) -> int:
    """Return index of the closing '}' that matches the '{' at open_pos."""
    depth = 0
    i = open_pos
    while i < len(source):
        if source[i] == '{':
            depth += 1
        elif source[i] == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(source) - 1


def simplify_go_test_func(func_source: str, test_case: str) -> str:
    """
    Given the source of a Go test function, return a version that contains
    only the target test case.

    Strategy:
      1. If the function uses t.Run("case name", …) blocks, keep only the
         matching block.
      2. If it uses a struct slice table + range loop, keep only the matching
         struct entry.
      3. Otherwise return the source unchanged (already simple).
    """
    # --- Strategy 1: t.Run blocks ---
    matches = list(_GO_TRUN_RE.finditer(func_source))
    if matches:
        target_match = None
        for m in matches:
            # Case-insensitive substring match (mirrors paper's test_case matching)
            if test_case.lower() in m.group(1).lower() or m.group(1) in test_case:
                target_match = m
                break

        if target_match and len(matches) > 1:
            # Find the full t.Run(...) block
            brace_start = target_match.end() - 1  # points at '{'
            brace_end = _find_matching_brace_end(func_source, brace_start)
            # The t.Run call ends with ')' after the closing brace
            call_end = func_source.find(')', brace_end) + 1

            kept_block = func_source[target_match.start():call_end]

            # Reconstruct: keep function signature + kept block + closing brace
            sig_end = func_source.find('{') + 1
            sig = func_source[:sig_end]
            return sig + "\n\t" + kept_block + "\n}"

    # --- Strategy 2: struct table ---
    table_match = _GO_TABLE_SLICE_RE.search(func_source)
    if table_match:
        table_var = table_match.group(1)
        slice_open = func_source.index('{', table_match.start())
        slice_close = _find_matching_brace_end(func_source, slice_open)
        table_body = func_source[slice_open + 1:slice_close]

        # Split table entries by top-level '{...}' blocks
        entries: list[str] = []
        i = 0
        while i < len(table_body):
            if table_body[i] == '{':
                end = _find_matching_brace_end(table_body, i)
                entries.append(table_body[i:end + 1])
                i = end + 1
            else:
                i += 1

        # Keep only entries that mention the test_case name
        kept = [e for e in entries if test_case in e]
        if kept and len(kept) < len(entries):
            new_table = func_source[:slice_open + 1] + "\n" + "\n".join(kept) + "\n" + func_source[slice_close:]
            return new_table

    # --- No simplification possible ---
    return func_source


# ── Python parametrize simplification ────────────────────────────────────────

_PY_PARAMETRIZE_RE = re.compile(
    r'@pytest\.mark\.parametrize\s*\([^)]+\)',
    re.DOTALL,
)


def simplify_python_test_func(func_source: str, test_case: str) -> str:
    """
    Keep only the parametrize decorator entry matching test_case.
    For now, returns unchanged (parametrize is harder to strip portably).
    """
    return func_source  # TODO: implement if needed for your baseline


# ── Public API ────────────────────────────────────────────────────────────────

def simplify_test(func_source: str, test_case: str, language: str) -> str:
    """
    Return a simplified version of the test function that focuses on
    the target test case.  The original source is unchanged on disk.

    Args:
        func_source: Full source text of the test function.
        test_case:   The specific test case to keep.
        language:    "go" | "python" | "java"

    Returns:
        Simplified source string (or original if simplification not possible).
    """
    if not test_case:
        return func_source

    lang = language.lower()
    if lang == "go":
        return simplify_go_test_func(func_source, test_case)
    elif lang == "python":
        return simplify_python_test_func(func_source, test_case)
    # Java: return as-is for now
    return func_source


def extract_test_func(file_source: str, test_func: str, language: str) -> str | None:
    """
    Extract the full source of `test_func` from `file_source`.
    Returns None if not found.
    """
    lang = language.lower()
    if lang == "go":
        pattern = re.compile(
            r'(func\s+' + re.escape(test_func) + r'\s*\([^)]*\)\s*\{)',
            re.MULTILINE,
        )
        m = pattern.search(file_source)
        if not m:
            return None
        brace_start = file_source.index('{', m.start())
        brace_end = _find_matching_brace_end(file_source, brace_start)
        return file_source[m.start():brace_end + 1]

    elif lang == "python":
        pattern = re.compile(
            r'(def\s+' + re.escape(test_func) + r'\s*\([^)]*\):)',
            re.MULTILINE,
        )
        m = pattern.search(file_source)
        if not m:
            return None
        # Collect indented lines after def
        lines = file_source[m.start():].splitlines()
        if len(lines) < 2:
            return lines[0] if lines else None
        base_indent = len(lines[0]) - len(lines[0].lstrip())
        result = [lines[0]]
        for line in lines[1:]:
            if line.strip() == "":
                result.append(line)
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= base_indent and line.strip():
                break
            result.append(line)
        return "\n".join(result)

    elif lang == "java":
        return _extract_java_method(file_source, test_func)

    return None


def _extract_java_method(file_source: str, test_func: str) -> str | None:
    """
    Return the exact source of the Java method named `test_func` (annotations
    included), matching the original FlakyGuard's tree-sitter
    method_declaration lookup. The result is a verbatim slice of
    `file_source`, so SEARCH blocks the LLM copies from it still match the file.
    """
    from graph import _get_ts_language  # local import: graph.py is heavier

    ts_lang = _get_ts_language("java")
    if ts_lang is None:
        return None
    import tree_sitter as ts

    source = file_source.encode("utf-8")
    tree = ts.Parser(ts_lang).parse(source)

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type == "method_declaration":
            name = node.child_by_field_name("name")
            if name is not None and name.text.decode("utf-8") == test_func:
                # Start at the beginning of the line so the first line keeps
                # its indentation, like every other line of the slice.
                start = node.start_byte
                line_start = source.rfind(b"\n", 0, start) + 1
                if not source[line_start:start].strip():
                    start = line_start
                return source[start:node.end_byte].decode("utf-8")
        stack.extend(reversed(node.children))
    return None
