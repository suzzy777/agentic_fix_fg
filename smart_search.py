"""
analysis/smart_search.py – LLM-guided BFS over the call graph.

This is Algorithm 1 from the paper:
  "Smart Graph Traversal with GenAI"

At each BFS step the LLM is asked which of the callee nodes are most
relevant to the flaky-test error, instead of blindly expanding all
neighbors.  After traversal, a second LLM call selects the globally
most relevant F functions (post-processing filter).

Key parameters (all configurable):
  k  – max children the LLM may select per node  (paper default: 3)
  d  – depth limit (-1 = infinite)               (paper default: ∞)
  F  – final global filter size                  (paper default: 5)
"""

from __future__ import annotations
import logging
import re
from collections import deque

from graph import CallGraph, FuncDef
from llm import complete

logger = logging.getLogger(__name__)


# ── LLM selection helpers ─────────────────────────────────────────────────────

def _llm_select(
    candidates: list[FuncDef],
    k: int,
    problem_statement: str,
    current_func: FuncDef | None = None,
    log_dir: str | None = None,
    tag: str = "bfs_select",
) -> list[FuncDef]:
    """
    Ask the LLM to pick the k most relevant functions from `candidates`.
    Returns the selected subset (may be empty on parse failure).

    Mirrors default_llm_based_select() from llm_selection.py.
    """
    if not candidates:
        return []
    if len(candidates) <= k:
        return candidates  # nothing to filter

    # Build a numbered menu of candidate functions
    menu_lines = []
    for i, fd in enumerate(candidates):
        # Show name + first few lines (to keep context manageable)
        preview = fd.source[:300].replace("\n", " ")
        menu_lines.append(f"[{i}] {fd.name}  ({fd.filepath}, line {fd.start_line})\n    {preview}")

    current_ctx = ""
    if current_func:
        current_ctx = (
            f"\nCurrently expanding function:\n```\n{current_func.source[:500]}\n```\n"
        )

    prompt = f"""You are helping identify the root cause of a flaky test.

Problem: {problem_statement}
{current_ctx}
Select the {k} most relevant functions from the list below that are likely
involved in the flaky behavior.  Return ONLY their indices as a
comma-separated list inside <INDICES> tags.

Candidates:
{chr(10).join(menu_lines)}

Example response: <INDICES>0,2</INDICES>
"""

    response = complete(prompt, temperature=0.0, log_dir=log_dir, tag=tag)
    m = re.search(r"<INDICES>(.*?)</INDICES>", response, re.DOTALL)
    if not m:
        # Fallback: return first k
        return candidates[:k]

    indices_str = m.group(1)
    indices: list[int] = []
    for part in indices_str.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part)
            if 0 <= idx < len(candidates):
                indices.append(idx)

    return [candidates[i] for i in indices[:k]]


def _llm_global_filter(
    collected: list[FuncDef],
    F: int,
    problem_statement: str,
    log_dir: str | None = None,
    tag: str = "bfs_global_filter",
) -> list[FuncDef]:
    """
    Post-processing step: from all collected nodes choose the globally
    most relevant F functions.  (Paper §III-B final step.)
    """
    if len(collected) <= F:
        return collected

    menu_lines = []
    for i, fd in enumerate(collected):
        preview = fd.source[:200].replace("\n", " ")
        menu_lines.append(f"[{i}] {fd.name}  ({fd.filepath}, line {fd.start_line})\n    {preview}")

    prompt = f"""You are helping fix a flaky test.

Problem: {problem_statement}

From the functions below, select the {F} that are MOST CRITICAL for
understanding and fixing the flaky behavior.  Return ONLY their indices
inside <INDICES> tags (comma-separated).

Functions:
{chr(10).join(menu_lines)}
"""

    response = complete(prompt, temperature=0.0, log_dir=log_dir, tag=tag)
    m = re.search(r"<INDICES>(.*?)</INDICES>", response, re.DOTALL)
    if not m:
        return collected[:F]

    indices: list[int] = []
    for part in m.group(1).split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part)
            if 0 <= idx < len(collected):
                indices.append(idx)

    selected = [collected[i] for i in indices[:F]]
    if not selected:
        selected = collected[:F]
    return selected


# ── Smart BFS ─────────────────────────────────────────────────────────────────

def smart_bfs(
    graph: CallGraph,
    start_funcs: list[str],
    problem_statement: str,
    k: int = 3,
    depth_limit: int = -1,
    F: int = 10,
    callee_only: bool = True,
    log_dir: str | None = None,
    tag_prefix: str = "bfs",
    trace: dict | None = None,
) -> list[FuncDef]:
    """
    Algorithm 1 from the paper: LLM-guided BFS.

    Args:
        graph:             The static (or dynamic) call graph.
        start_funcs:       Function names to start BFS from (usually the test function).
        problem_statement: Short description of the flaky error (used in LLM prompts).
        k:                 Max children selected per node.
        depth_limit:       Max BFS depth (-1 = unlimited).
        F:                 Global post-filter: keep only the top-F functions.
        callee_only:       If True, only follow callee edges (paper default for flaky repair).
        log_dir:           When set, persist every LLM call's prompt+response here
                            (see llm.py::complete()'s log_dir/tag).
        tag_prefix:        Prefix for each call's tag, e.g. "M1_context".
        trace:             When given, filled with "steps" (each expansion's
                            node, depth, candidates and picks) and "dropped"
                            (removed by the F filter), for run_log.jsonl.

    Returns:
        Ordered list of FuncDef objects: root nodes first, then selected callees.
    """
    # Collect starting nodes (always included, paper §III-B)
    root_nodes: list[FuncDef] = []
    for fname in start_funcs:
        root_nodes.extend(graph.get_def(fname))

    if not root_nodes:
        logger.warning("smart_bfs: no definitions found for start_funcs %s", start_funcs)
        return []

    # Identify nodes by (file, start line), not bare name: Java overloads share a
    # name, and name-based dedupe silently dropped every overload after the
    # first one the LLM picked. The original FlakyGuard dedupes whole Tags
    # (file + line range + name) the same way.
    def _key(fd: FuncDef) -> tuple[str, int]:
        return (fd.filepath, fd.start_line)

    def _label(fd: FuncDef) -> str:
        return f"{fd.name}:{fd.start_line}"

    root_keys = {_key(fd) for fd in root_nodes}
    visited: set[tuple[str, int]] = set(root_keys)
    collected: list[FuncDef] = list(root_nodes)

    # BFS queue: (FuncDef, depth)
    queue: deque[tuple[FuncDef, int]] = deque((fd, 0) for fd in root_nodes)

    while queue:
        current, depth = queue.popleft()

        if depth_limit != -1 and depth >= depth_limit:
            continue

        if callee_only:
            neighbors = graph.get_callees(current.name)
        else:
            neighbors = graph.get_callees(current.name) + graph.get_callers(current.name)

        # Only consider nodes not yet visited; the graph can list the same
        # definition twice, so dedupe (a duplicate would waste one of the k picks).
        unvisited = []
        offered: set[tuple[str, int]] = set()
        for fd in neighbors:
            if _key(fd) not in visited and _key(fd) not in offered:
                offered.add(_key(fd))
                unvisited.append(fd)
        if not unvisited:
            continue

        # ── LLM selection step (Algorithm 1, line 9) ──
        problem_with_current = (
            f"{problem_statement}\n\nCurrently expanding: {current.name}"
        )
        selected = _llm_select(
            unvisited, k, problem_with_current, current_func=current,
            log_dir=log_dir, tag=f"{tag_prefix}_select_{current.name}",
        )
        logger.info(
            "smart_bfs: expanding %s (depth %d): %d candidate(s) -> selected %s",
            _label(current), depth, len(unvisited), [_label(fd) for fd in selected],
        )
        if trace is not None:
            trace.setdefault("steps", []).append({
                "node": _label(current),
                "depth": depth,
                "candidates": [_label(fd) for fd in unvisited],
                "selected": [_label(fd) for fd in selected],
            })

        for fd in selected:
            if _key(fd) not in visited:
                visited.add(_key(fd))
                collected.append(fd)
                queue.append((fd, depth + 1))

    # ── Global post-processing filter (Algorithm 1, post-step) ──
    # Root nodes are always kept; filter is applied to the rest.
    non_root = [fd for fd in collected if _key(fd) not in root_keys]
    filtered_non_root = _llm_global_filter(
        non_root, F, problem_statement,
        log_dir=log_dir, tag=f"{tag_prefix}_global_filter",
    )

    result = list(root_nodes) + filtered_non_root

    dropped = [_label(fd) for fd in non_root if fd not in filtered_non_root]
    if dropped:
        logger.info("smart_bfs: global filter (F=%d) dropped %s", F, dropped)
    if trace is not None:
        trace["dropped"] = dropped

    logger.info(
        "smart_bfs: %d nodes collected → %d after global filter (started from: %s)",
        len(collected), len(result), start_funcs,
    )
    return result
