"""
core/pipeline.py – FlakyGuard-style fixing loop.

This version restores the paper-style M × P × N repair loop:

  M context attempts
    × P high-level thoughts/plans per context
      × N concrete fixes per thought

Each thought records a root-cause category, explanation, and fixing plan.
Concrete fixes are then generated from that thought, applied, and validated.
"""

from __future__ import annotations

import json
import logging
import os
import time

from models import TestInput, FlakyInfo, Context, Fix
from prompts import get_flaky_test_fixing_prompt, get_flaky_test_thought_prompt, SEARCH_REPLACE_FORMAT, describe_category
from graph import CallGraphBuilder, files_near_test
from jacoco_coverage import collect_jacoco_coverage
from smart_search import smart_bfs
from search_replace import apply_fix, revert_all, parse_fix, write_patch_file
from llm import complete, token_usage_mark, token_usage_since
from runner import validate_fix_detailed
from simplifier import simplify_test, extract_test_func
from compile_repair import attempt_compile_repair, parse_compile_defects

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)


# ── Structured per-step run log ───────────────────────────────────────────────
#
# A per-run structured log (agentic_iterations.jsonl + run_summary.csv),
# adapted to this project's M x P x N repair loop instead of a single
# agentic iteration counter. One JSON line is appended per context-collection pass,
# per thought, and per concrete fix attempt, plus one "summary" line at the
# end recording which attempt (if any) actually won -- that's what answers
# "which iteration found the right patch" for a given run.

def _run_log_path(output_dir: str) -> str:
    return os.path.join(output_dir, "run_log.jsonl")


def _append_run_log(output_dir: str, record: dict) -> None:
    os.makedirs(output_dir, exist_ok=True)
    record = {"ts": time.time(), **record}
    with open(_run_log_path(output_dir), "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _tokens_from_rows(rows: list[dict]) -> tuple[int, int]:
    tokens_in = sum(r.get("llm_input_tokens", 0) for r in rows)
    tokens_out = sum(r.get("llm_output_tokens", 0) for r in rows)
    return tokens_in, tokens_out


def _rel(path: str, repo_root: str) -> str:
    try:
        return os.path.relpath(path, repo_root)
    except ValueError:
        return path


def _fmt_func(fd, repo_root: str) -> str:
    return f"{fd.name} ({_rel(fd.filepath, repo_root)}:{fd.start_line}-{fd.end_line})"


def _resolve_polluter(test_input: TestInput) -> tuple[str, str | None]:
    """
    Split test_input.polluter ("pkg.Class#method") into (method_name, abs source
    file). The file is looked up next to the victim's test source root first
    (same module), then anywhere under repo_root by package path; None if it
    can't be found uniquely.
    """
    if not test_input.polluter or "#" not in test_input.polluter:
        return "", None
    class_fqn, method = test_input.polluter.rsplit("#", 1)
    method = method.split("[", 1)[0].split("(", 1)[0].strip()
    rel_class_path = class_fqn.split("$", 1)[0].replace(".", "/") + ".java"

    victim_file = test_input.test_file.replace("\\", "/")
    marker = "src/test/java/"
    if marker in victim_file:
        candidate = os.path.join(
            test_input.repo_root, victim_file.split(marker, 1)[0] + marker + rel_class_path
        )
        if os.path.isfile(candidate):
            return method, os.path.abspath(candidate)

    matches = []
    for root, _dirs, files in os.walk(test_input.repo_root):
        for name in files:
            full = os.path.join(root, name)
            if full.replace("\\", "/").endswith("/" + rel_class_path):
                matches.append(full)
    if len(matches) == 1:
        return method, os.path.abspath(matches[0])
    return method, None


def _changed_methods(backups: dict, repo_root: str, language: str) -> list[str]:
    """
    Names of the methods a candidate patch changed ("name (file:start-end)"),
    found by diffing each edited file against its pre-fix backup and mapping
    the changed lines onto the method/constructor declarations that contain
    them. Edits outside any method (imports, fields) are reported per file.
    Java only; other languages return [].
    """
    if language.lower() != "java":
        return []
    from graph import _get_ts_language
    ts_lang = _get_ts_language("java")
    if ts_lang is None:
        return []
    import difflib
    import tree_sitter as ts

    touched: list[str] = []
    for filepath, backup in backups.items():
        old_lines = (backup._backup or "").splitlines()
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as f:
                new_source = f.read()
        except OSError:
            continue
        new_lines = new_source.splitlines()

        changed: set[int] = set()  # 1-based lines in the new file
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            # A pure deletion has no new lines; attribute it to where it was.
            changed.update(range(j1 + 1, j2 + 1) if j2 > j1 else {max(j1, 1)})
        if not changed:
            continue

        tree = ts.Parser(ts_lang).parse(new_source.encode("utf-8"))
        stack, hit = [tree.root_node], set()
        while stack:
            node = stack.pop()
            if node.type in ("method_declaration", "constructor_declaration"):
                start, end = node.start_point[0] + 1, node.end_point[0] + 1
                inside = {line for line in changed if start <= line <= end}
                if inside:
                    name = node.child_by_field_name("name")
                    label = name.text.decode("utf-8") if name is not None else node.type
                    touched.append(f"{label} ({_rel(filepath, repo_root)}:{start}-{end})")
                    hit |= inside
            stack.extend(node.children)
        if changed - hit:
            touched.append(f"(outside any method) {_rel(filepath, repo_root)}")
    return touched


def _classify_validation_failure(validation_output: str) -> str:
    """
    Best-effort category for a failed validation, for the run log only (this
    does not gate any control flow -- attempt_compile_repair() already makes
    its own, independent judgment call from the same output).
    """
    if validation_output and parse_compile_defects(validation_output):
        return "compile_failed"
    return "test_failed"


def collect_context(
    test_input: TestInput,
    flaky_info: FlakyInfo,
    k: int = 3,
    depth_limit: int = -1,
    max_funcs: int = 10,
    context_attempt: int = 1,
    total_context_attempts: int = 1,
    log_dir: str | None = None,
) -> Context:
    """
    Build the call graph for files near the test and run smart BFS
    to collect the most relevant function nodes.
    """
    test_file_abs = os.path.join(test_input.repo_root, test_input.test_file)

    coverage = collect_jacoco_coverage(test_input)
    if coverage.files:
        scope_files = list(coverage.files)
        covered_lines = coverage.lines_by_file
        scope_source = "jacoco"
        covered_lines_count = sum(len(lines) for lines in covered_lines.values())
        logger.info(
            "Using JaCoCo coverage scope: %d file(s), %d covered line(s)",
            len(scope_files),
            covered_lines_count,
        )
        for path in scope_files:
            logger.info(
                "    covered file: %s (%d line(s))",
                _rel(path, test_input.repo_root),
                len(covered_lines.get(path, ())),
            )
    else:
        scope_files = files_near_test(
            test_file_abs,
            up_levels=2,
            language=test_input.language,
        )
        covered_lines = {}
        scope_source = "nearby_files"
        covered_lines_count = 0
        logger.info("Using nearby-file scope: %d file(s)", len(scope_files))
        for path in scope_files:
            logger.info("    scope file: %s", _rel(path, test_input.repo_root))

    test_file_abs = os.path.abspath(test_file_abs)
    if test_file_abs not in scope_files:
        scope_files.append(test_file_abs)
    always_keep = {(test_file_abs, test_input.test_func)}

    # OD/britle: the polluter writes the shared state the victim reads, and
    # that write path is not on the victim's call chain -- so BFS must also
    # start from the polluter, and its file/method must be in the graph.
    polluter_method, polluter_file = _resolve_polluter(test_input)
    start_funcs = [test_input.test_func]
    if polluter_method:
        if polluter_file:
            if polluter_file not in scope_files:
                scope_files.append(polluter_file)
            always_keep.add((polluter_file, polluter_method))
            logger.info(
                "Polluter %s found in %s; BFS will also start from it.",
                polluter_method, _rel(polluter_file, test_input.repo_root),
            )
        else:
            logger.warning(
                "Could not locate the source file for polluter %s; BFS will "
                "only start from it if it is already in the graph.",
                test_input.polluter,
            )
        if polluter_method != test_input.test_func:
            start_funcs.append(polluter_method)

    builder = CallGraphBuilder(
        test_input.repo_root,
        scope_files,
        language=test_input.language,
        covered_lines=covered_lines,
        always_keep=always_keep,
    )
    graph = builder.build()

    methods_by_file: dict[str, list[str]] = {}
    graph_methods: list[str] = []
    for defs in graph.definitions.values():
        for fd in defs:
            methods_by_file.setdefault(fd.filepath, []).append(fd.name)
            graph_methods.append(_fmt_func(fd, test_input.repo_root))
    logger.info("Methods available to smart BFS (after coverage pruning):")
    for path in sorted(methods_by_file):
        logger.info(
            "    %s: %s",
            _rel(path, test_input.repo_root),
            ", ".join(sorted(methods_by_file[path])),
        )

    category_line = f"Category: {describe_category(test_input.category)}\n" if test_input.category else ""
    polluter_line = (
        f"Polluter (runs immediately before the victim): {test_input.polluter}\n"
        if test_input.polluter else ""
    )

    problem_statement = (
        f"Flaky test '{test_input.test_func}/{test_input.test_case}'. "
        f"{category_line}"
        f"{polluter_line}"
        f"Error: {flaky_info.error[:300]}\n"
        f"Context attempt {context_attempt}/{total_context_attempts}: "
        "consider a plausible root-cause path for this attempt."
    )

    bfs_trace: dict = {}
    relevant_nodes = smart_bfs(
        graph=graph,
        start_funcs=start_funcs,
        problem_statement=problem_statement,
        k=k,
        depth_limit=depth_limit,
        F=max_funcs,
        callee_only=True,
        log_dir=log_dir,
        tag_prefix=f"M{context_attempt}_context",
        trace=bfs_trace,
    )

    imports: dict[str, str] = {}
    for fd in relevant_nodes:
        filepath = fd.filepath
        if filepath not in imports:
            imports[filepath] = _extract_imports(filepath, test_input.language)

    context = Context(
        func_nodes=relevant_nodes,
        imports=imports,
        scope_source=scope_source,
        scope_files_count=len(scope_files),
        coverage_covered_lines_count=covered_lines_count,
        scope_files=[_rel(path, test_input.repo_root) for path in scope_files],
        covered_files_count=len(coverage.files),
        graph_methods=sorted(graph_methods),
        bfs_steps=bfs_trace.get("steps", []),
        bfs_dropped=bfs_trace.get("dropped", []),
    )
    for fd in context.func_nodes:
        logger.info("    context function: %s", _fmt_func(fd, test_input.repo_root))
    logger.info(
        "Context: %d function nodes from %d files",
        len(context.func_nodes),
        len({func.filepath for func in context.func_nodes}),
    )
    return context


def _extract_imports(filepath: str, language: str) -> str:
    """Extract the import block from a source file."""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as file:
            source = file.read()
    except OSError:
        return ""

    lang = language.lower()

    if lang == "go":
        import re

        match = re.search(r'import\s*\(([^)]*)\)', source, re.DOTALL)
        if match:
            return "import (\n" + match.group(1) + ")\n"

        single = re.search(r'import\s+"[^"]+"', source)
        return single.group(0) if single else ""

    if lang == "python":
        return "\n".join(
            line for line in source.splitlines()
            if line.startswith("import ") or line.startswith("from ")
        )

    if lang == "java":
        return "\n".join(
            line for line in source.splitlines()
            if line.strip().startswith("import ")
        )

    return ""


def _format_context(context: Context) -> str:
    """Format context nodes into a string for the LLM prompt."""
    sections: list[str] = []
    seen_files: set[str] = set()

    for fd in context.func_nodes:
        filepath = fd.filepath

        if filepath not in seen_files:
            seen_files.add(filepath)
            imports = context.imports.get(filepath, "")
            if imports:
                sections.append(f"=== {filepath} (imports) ===\n{imports}\n")

        sections.append(f"=== {filepath} ===\n{fd.source}\n")

    return "\n".join(sections)


def _valid_filenames_for_context(test_input: TestInput, context: Context) -> list[str]:
    """
    Return paths that the LLM is allowed to edit.

    This is used only to help the original/Aider-style parser choose the
    correct filename near a SEARCH/REPLACE block.
    """
    valid = [test_input.test_file]

    test_abs = os.path.join(test_input.repo_root, test_input.test_file)
    valid.append(test_abs)

    for node in context.func_nodes:
        abs_path = node.filepath
        valid.append(abs_path)

        try:
            rel_path = os.path.relpath(abs_path, test_input.repo_root)
            valid.append(rel_path)
        except ValueError:
            pass

    # Preserve order while removing duplicates.
    seen = set()
    result = []
    for item in valid:
        if item and item not in seen:
            seen.add(item)
            result.append(item)

    return result


def _load_test_snippets(test_input: TestInput) -> tuple[str, str] | None:
    """Return (original_test, simplified_test) for LLM prompts."""
    test_file_abs = os.path.join(test_input.repo_root, test_input.test_file)

    try:
        with open(test_file_abs, "r", encoding="utf-8", errors="replace") as file:
            file_source = file.read()
    except OSError:
        logger.error("Cannot read test file: %s", test_file_abs)
        return None

    original_test = extract_test_func(file_source, test_input.test_func, test_input.language)
    if not original_test:
        logger.warning(
            "Could not extract test method %s from %s; sending the whole test file instead.",
            test_input.test_func, test_input.test_file,
        )
        original_test = file_source
    simplified_test = simplify_test(original_test, test_input.test_case, test_input.language)
    return original_test, simplified_test


def generate_thought(
    test_input: TestInput,
    flaky_info: FlakyInfo,
    context: Context,
    context_attempt: int,
    thought_attempt: int,
    total_thoughts: int,
    failed_thoughts: list[str],
    log_dir: str | None = None,
    tag: str = "thought",
) -> str | None:
    """
    Generate the paper-style high-level thought before generating a patch.

    The thought should contain: root-cause category, root-cause explanation,
    and fixing plan. It does not contain SEARCH/REPLACE edits.
    """
    snippets = _load_test_snippets(test_input)
    if snippets is None:
        return None
    original_test, simplified_test = snippets

    # prompt = get_flaky_test_thought_prompt(
    #     simplified_test_code=simplified_test,
    #     original_test_code=original_test,
    #     assertion_failures=flaky_info.error,
    #     error_trace=flaky_info.error_trace,
    #     code_context=_format_context(context),
    #     language=test_input.language,
    #     context_attempt=context_attempt,
    #     thought_attempt=thought_attempt,
    #     total_thoughts=total_thoughts,
    # )

    prompt = get_flaky_test_thought_prompt(
        simplified_test_code=simplified_test,
        original_test_code=original_test,
        assertion_failures=flaky_info.error,
        error_trace=flaky_info.error_trace,
        code_context=_format_context(context),
        language=test_input.language,
        context_attempt=context_attempt,
        thought_attempt=thought_attempt,
        total_thoughts=total_thoughts,
        failed_thoughts=failed_thoughts,
        category=test_input.category,
        polluter=test_input.polluter,
    )

    response = complete(prompt, temperature=0.0, log_dir=log_dir, tag=tag)
    if not response or not response.strip():
        logger.warning("LLM produced no thought.")
        return None

    return response.strip()


def generate_fix(
    test_input: TestInput,
    flaky_info: FlakyInfo,
    context: Context,
    thought: str | None = None,
    log_dir: str | None = None,
    tag: str = "fix",
) -> Fix | None:
    """
    Convert context + optional high-level thought into a concrete fix.
    """
    snippets = _load_test_snippets(test_input)
    if snippets is None:
        return None
    original_test, simplified_test = snippets

    prompt = get_flaky_test_fixing_prompt(
        simplified_test_code=simplified_test,
        original_test_code=original_test,
        assertion_failures=flaky_info.error,
        error_trace=flaky_info.error_trace,
        code_context=_format_context(context),
        language=test_input.language,
        output_format=SEARCH_REPLACE_FORMAT,
        thought=thought,
        category=test_input.category,
        polluter=test_input.polluter,
    )

    response = complete(prompt, temperature=0.0, log_dir=log_dir, tag=tag)
    if not response:
        return None

    fix = parse_fix(
        response,
        default_filepath=test_input.test_file,
        valid_fnames=_valid_filenames_for_context(test_input, context),
    )

    if not fix.edits:
        logger.warning("LLM produced no SEARCH/REPLACE edits.")
        return None

    return fix

def run_pipeline(
    test_input: TestInput,
    flaky_info: FlakyInfo,
    attempts: int | None = None,
    context_attempts: int = 3,
    thoughts_per_context: int = 2,
    fixes_per_thought: int = 3,
    k: int = 3,
    depth_limit: int = -1,
    max_funcs: int = 10,
    output_dir: str = "patches",
) -> tuple[bool, str, dict]:
    """
    Run the paper-style M × P × N FlakyGuard repair loop.

    M = context_attempts
    P = thoughts_per_context
    N = fixes_per_thought

    The legacy `attempts` argument is treated as an alias for M so older
    callers using --attempts still work.

    Every context-collection pass, thought, and concrete fix attempt is
    appended as a JSON line to <output_dir>/run_log.jsonl (see
    _append_run_log() docstring above) so the full step-by-step record of a
    run -- including exactly which M/P/N attempt finally passed -- survives
    the run itself, not just the scrollback of `logger.info` calls.

    Returns (success, message, run_info). run_info always has
    "total_candidates_tried" and "step_log_path"; on success it also has
    "winning_candidate_index"/"winning_m"/"winning_p"/"winning_n".
    """
    failed_thoughts: list[str] = []

    if attempts is not None:
        context_attempts = attempts

    total_fix_attempts = context_attempts * thoughts_per_context * fixes_per_thought
    fix_counter = 0
    run_start_mark = token_usage_mark()

    # Every LLM call's full prompt + response gets persisted here (see
    # llm.py::complete()'s log_dir/tag), mirroring the original FlakyGuard/
    # AgentFlake's llm_context.txt + llm_response.json. Toggle off via
    # TestInput.log_llm_calls for large batch runs where the per-call
    # storage isn't worth it.
    llm_log_dir = os.path.join(output_dir, "llm_calls") if test_input.log_llm_calls else None

    logger.info(
        "Repair budget: M=%d context attempts × P=%d thoughts/context × N=%d fixes/thought = %d candidate fixes",
        context_attempts,
        thoughts_per_context,
        fixes_per_thought,
        total_fix_attempts,
    )

    context_stats = {"covered_files": 0, "covered_lines": 0, "graph_methods": 0, "context_methods": 0}

    def _finish(success: bool, message: str, winning: dict | None) -> tuple[bool, str, dict]:
        run_tokens_in, run_tokens_out = _tokens_from_rows(token_usage_since(run_start_mark))
        run_info = {
            "total_candidates_tried": fix_counter,
            "total_tokens_in": run_tokens_in,
            "total_tokens_out": run_tokens_out,
            "step_log_path": _run_log_path(output_dir),
            **context_stats,
        }
        if winning:
            run_info.update(winning)
        _append_run_log(output_dir, {
            "step": "summary",
            "verdict": "PASSED" if success else "FAILED",
            **run_info,
        })
        return success, message, run_info

    for m in range(1, context_attempts + 1):
        logger.info("─── Context attempt M=%d/%d: collecting context ───", m, context_attempts)

        context_mark = token_usage_mark()
        context_start = time.time()
        context_error = None
        try:
            context = collect_context(
                test_input,
                flaky_info,
                k=k,
                depth_limit=depth_limit,
                max_funcs=max_funcs,
                context_attempt=m,
                total_context_attempts=context_attempts,
                log_dir=llm_log_dir,
            )
        except Exception as exc:
            logger.error("Context collection failed: %s", exc)
            context = Context()
            context_error = str(exc)

        context_tokens_in, context_tokens_out = _tokens_from_rows(token_usage_since(context_mark))
        _append_run_log(output_dir, {
            "step": "context",
            "m": m,
            "elapsed_seconds": round(time.time() - context_start, 3),
            "tokens_in": context_tokens_in,
            "tokens_out": context_tokens_out,
            "num_func_nodes": len(context.func_nodes),
            "scope_source": context.scope_source,
            "scope_files_count": context.scope_files_count,
            "coverage_covered_lines_count": context.coverage_covered_lines_count,
            "scope_files": context.scope_files,
            "covered_files_count": context.covered_files_count,
            "graph_methods_count": len(context.graph_methods),
            "graph_methods": context.graph_methods,
            "bfs_steps": context.bfs_steps,
            "bfs_dropped": context.bfs_dropped,
            "func_nodes": [_fmt_func(fd, test_input.repo_root) for fd in context.func_nodes],
            "error": context_error,
        })
        # Numbers for llm_token_usage.csv: the context a winning fix came
        # from, or the last one tried if nothing won.
        context_stats = {
            "covered_files": context.covered_files_count,
            "covered_lines": context.coverage_covered_lines_count,
            "graph_methods": len(context.graph_methods),
            "context_methods": len(context.func_nodes),
        }

        for p_idx in range(1, thoughts_per_context + 1):
            logger.info(
                "  Generating thought P=%d/%d for context M=%d/%d",
                p_idx,
                thoughts_per_context,
                m,
                context_attempts,
            )

            thought_mark = token_usage_mark()
            thought_start = time.time()
            thought = generate_thought(
                test_input,
                flaky_info,
                context,
                context_attempt=m,
                thought_attempt=p_idx,
                total_thoughts=thoughts_per_context,
                failed_thoughts=failed_thoughts,
                log_dir=llm_log_dir,
                tag=f"M{m}_P{p_idx}_thought",
            )
            thought_tokens_in, thought_tokens_out = _tokens_from_rows(token_usage_since(thought_mark))
            _append_run_log(output_dir, {
                "step": "thought",
                "m": m,
                "p": p_idx,
                "elapsed_seconds": round(time.time() - thought_start, 3),
                "tokens_in": thought_tokens_in,
                "tokens_out": thought_tokens_out,
                "generated": thought is not None,
                "preview": (thought[:300].replace("\n", " ") if thought else None),
            })

            if thought is None:
                continue

            logger.info("  Thought preview: %s", thought[:300].replace("\n", " "))

            for n in range(1, fixes_per_thought + 1):
                fix_counter += 1
                logger.info(
                    "  Generating fix N=%d/%d from M=%d/%d, P=%d/%d (candidate %d/%d)",
                    n,
                    fixes_per_thought,
                    m,
                    context_attempts,
                    p_idx,
                    thoughts_per_context,
                    fix_counter,
                    total_fix_attempts,
                )

                candidate_mark = token_usage_mark()
                candidate_start = time.time()
                verdict = "failed"
                category = "unknown"
                patch_path: str | None = None
                compile_repair_attempts = 0
                changed_methods: list[str] = []

                fix = generate_fix(
                    test_input, flaky_info, context, thought=thought,
                    log_dir=llm_log_dir, tag=f"M{m}_P{p_idx}_N{n}_fix",
                )
                if fix is None:
                    category = "no_fix_generated"
                else:
                    ok, result = apply_fix(fix, test_input.repo_root)
                    if not ok:
                        logger.warning("  Apply failed: %s", result)
                        category = "apply_failed"
                    else:
                        backups = result  # type: ignore[assignment]

                        patch_path = write_patch_file(
                            backups,
                            output_dir,
                            prefix=(
                                f"{test_input.test_func}_M{m}_P{p_idx}_N{n}_candidate_{fix_counter}"
                            ),
                            repo_root=test_input.repo_root,
                        )

                        passed, validation_output = validate_fix_detailed(test_input, patch_path=patch_path)

                        # Compilation-retry step (mirrors original FlakyGuard's
                        # fix_build_errors() retry loop): a candidate that fails
                        # validation because it broke the build -- not because it
                        # failed to fix the flakiness -- gets a few narrowly-scoped
                        # chances for the LLM to repair the compiler errors before
                        # it's discarded. Each repair re-runs the *same* ReproFlake/
                        # Docker validation (see compile_repair.py for why there's
                        # no separate host-side build step).
                        while (
                            not passed
                            and test_input.verify_compile_before_validate
                            and compile_repair_attempts < test_input.max_compile_fix_retries
                        ):
                            repaired, defects = attempt_compile_repair(
                                test_input, backups, validation_output,
                                log_dir=llm_log_dir,
                                tag=f"M{m}_P{p_idx}_N{n}_buildfix{compile_repair_attempts + 1}",
                            )
                            if not repaired:
                                # Nothing to repair (e.g. it's a genuine test
                                # failure, not a build failure) -- don't count
                                # this as a used retry.
                                break

                            compile_repair_attempts += 1
                            logger.info(
                                "  Compilation retry %d/%d: LLM patched %d compiler defect(s); re-validating.",
                                compile_repair_attempts,
                                test_input.max_compile_fix_retries,
                                len(defects),
                            )

                            patch_path = write_patch_file(
                                backups,
                                output_dir,
                                prefix=(
                                    f"{test_input.test_func}_M{m}_P{p_idx}_N{n}_candidate_{fix_counter}"
                                    f"_buildfix{compile_repair_attempts}"
                                ),
                                repo_root=test_input.repo_root,
                            )
                            passed, validation_output = validate_fix_detailed(test_input, patch_path=patch_path)

                        if passed:
                            verdict = "passed"
                            category = "passed"
                        else:
                            category = _classify_validation_failure(validation_output)

                        # Must run before revert_all: it diffs the patched
                        # files on disk against their backups.
                        changed_methods = _changed_methods(
                            backups, test_input.repo_root, test_input.language,
                        )
                        if changed_methods:
                            logger.info("  Patch changed: %s", ", ".join(changed_methods))

                        if passed:
                            revert_all(backups)
                            logger.info("✓ Fix validated! Patch saved to: %s", patch_path)
                        else:
                            logger.info("  Validation failed – reverting")
                            revert_all(backups)

                candidate_tokens_in, candidate_tokens_out = _tokens_from_rows(token_usage_since(candidate_mark))
                _append_run_log(output_dir, {
                    "step": "fix_attempt",
                    "m": m,
                    "p": p_idx,
                    "n": n,
                    "candidate_index": fix_counter,
                    "verdict": verdict,
                    "category": category,
                    "compile_repair_attempts": compile_repair_attempts,
                    "patch_path": patch_path,
                    "changed_methods": changed_methods,
                    "elapsed_seconds": round(time.time() - candidate_start, 3),
                    "tokens_in": candidate_tokens_in,
                    "tokens_out": candidate_tokens_out,
                })

                if verdict == "passed":
                    return _finish(
                        True,
                        fix.explanation or thought or "(no explanation)",
                        {
                            "winning_candidate_index": fix_counter,
                            "winning_m": m,
                            "winning_p": p_idx,
                            "winning_n": n,
                            "winning_changed_methods": changed_methods,
                        },
                    )

            failed_thoughts.append(thought)

    return _finish(
        False,
        (
            "No fix found after "
            f"M={context_attempts}, P={thoughts_per_context}, N={fixes_per_thought} "
            f"({total_fix_attempts} candidate fixes)."
        ),
        None,
    )
