from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


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



def simplify_test(func_source: str, test_case: str, language: str) -> str:
    """
    Return a simplified version of the test function that focuses on
    the target test case. The original source is unchanged on disk.

    Args:
        func_source: Full source text of the test function.
        test_case:   The specific test case to keep.
        language:    "python" | "java"

    Returns:
        Simplified source string (or original if simplification is not supported).
    """
    if not test_case:
        return func_source

    lang = language.lower()
    if lang == "python":
        return simplify_python_test_func(func_source, test_case)

    # Java: return as-is for now.
    return func_source


def extract_test_func(file_source: str, test_func: str, language: str) -> str | None:
    """
    Extract the full source of `test_func` from `file_source`.
    Returns None if not found or if extraction is not supported for the language.
    """
    lang = language.lower()
    if lang == "python":
        pattern = re.compile(
            r'(def\s+' + re.escape(test_func) + r'\s*\([^)]*\):)',
            re.MULTILINE,
        )
        match = pattern.search(file_source)
        if not match:
            return None

        # Collect indented lines after def.
        lines = file_source[match.start():].splitlines()
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

    return None
