from __future__ import annotations

import logging
import os

from models import TestInput, FlakyInfo, Context, Fix
from prompts import render_flaky_repair_prompt, get_flaky_test_thought_prompt, SEARCH_REPLACE_FORMAT
from graph import CallGraphBuilder, files_near_test
from jacoco_coverage import collect_jacoco_coverage
from smart_search import smart_bfs
from search_replace import apply_fix, revert_all, parse_fix, write_patch_file
from llm import complete
from runner import validate_fix
from simplifier import simplify_test, extract_test_func

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)


def collect_context(
    test_input: TestInput,
    flaky_info: FlakyInfo,
    k: int = 3,
    depth_limit: int = -1,
    max_funcs: int = 5,
    context_attempt: int = 1,
    total_context_attempts: int = 1,
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
        logger.info(
            "Using JaCoCo coverage scope: %d file(s), %d covered line(s)",
            len(scope_files),
            sum(len(lines) for lines in covered_lines.values()),
        )
    else:
        scope_files = files_near_test(
            test_file_abs,
            up_levels=2,
            language=test_input.language,
        )
        covered_lines = {}
        logger.info("Using nearby-file scope: %d file(s)", len(scope_files))

    test_file_abs = os.path.abspath(test_file_abs)
    if test_file_abs not in scope_files:
        scope_files.append(test_file_abs)

    builder = CallGraphBuilder(
        test_input.repo_root,
        scope_files,
        language=test_input.language,
        covered_lines=covered_lines,
        always_keep={(test_file_abs, test_input.test_func)},
    )
    graph = builder.build()

    problem_statement = (
        f"Flaky test '{test_input.test_func}/{test_input.test_case}'. "
        f"Error: {flaky_info.error[:300]}\n"
        f"Context attempt {context_attempt}/{total_context_attempts}: "
        "consider a plausible root-cause path for this attempt."
    )

    relevant_nodes = smart_bfs(
        graph=graph,
        start_funcs=[test_input.test_func],
        problem_statement=problem_statement,
        k=k,
        depth_limit=depth_limit,
        F=max_funcs,
        callee_only=True,
    )

    imports: dict[str, str] = {}
    for fd in relevant_nodes:
        filepath = fd.filepath
        if filepath not in imports:
            imports[filepath] = _extract_imports(filepath, test_input.language)

    context = Context(func_nodes=relevant_nodes, imports=imports)
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

    original_test = (
        extract_test_func(file_source, test_input.test_func, test_input.language)
        or file_source
    )
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
    )

    response = complete(prompt, temperature=0.2)
    if not response or not response.strip():
        logger.warning("LLM produced no thought.")
        return None

    return response.strip()


def generate_fix(
    test_input: TestInput,
    flaky_info: FlakyInfo,
    context: Context,
    thought: str | None = None,
) -> Fix | None:
    """
    Convert context + optional high-level thought into a concrete fix.
    """
    snippets = _load_test_snippets(test_input)
    if snippets is None:
        return None
    original_test, simplified_test = snippets

    prompt = render_flaky_repair_prompt(
        simplified_test_code=simplified_test,
        original_test_code=original_test,
        assertion_failures=flaky_info.error,
        error_trace=flaky_info.error_trace,
        code_context=_format_context(context),
        language=test_input.language,
        output_format=SEARCH_REPLACE_FORMAT,
        thought=thought,
    )

    response = complete(prompt, temperature=0)
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
    max_funcs: int = 5,
    output_dir: str = "patches",
) -> tuple[bool, str]:
    """
    Run the paper-style M × P × N FlakyGuard repair loop.

    M = context_attempts
    P = thoughts_per_context
    N = fixes_per_thought

    The legacy `attempts` argument is treated as an alias for M so older
    callers using --attempts still work.
    """
    failed_thoughts: list[str] = []
    
    if attempts is not None:
        context_attempts = attempts

    total_fix_attempts = context_attempts * thoughts_per_context * fixes_per_thought
    fix_counter = 0

    logger.info(
        "Repair budget: M=%d context attempts × P=%d thoughts/context × N=%d fixes/thought = %d candidate fixes",
        context_attempts,
        thoughts_per_context,
        fixes_per_thought,
        total_fix_attempts,
    )

    for m in range(1, context_attempts + 1):
        logger.info("─── Context attempt M=%d/%d: collecting context ───", m, context_attempts)

        try:
            context = collect_context(
                test_input,
                flaky_info,
                k=k,
                depth_limit=depth_limit,
                max_funcs=max_funcs,
                context_attempt=m,
                total_context_attempts=context_attempts,
            )
        except Exception as exc:
            logger.error("Context collection failed: %s", exc)
            context = Context()

        for p_idx in range(1, thoughts_per_context + 1):
            logger.info(
                "  Generating thought P=%d/%d for context M=%d/%d",
                p_idx,
                thoughts_per_context,
                m,
                context_attempts,
            )
            
            thought = generate_thought(
                test_input,
                flaky_info,
                context,
                context_attempt=m,
                thought_attempt=p_idx,
                total_thoughts=thoughts_per_context,
                failed_thoughts=failed_thoughts,
            )
            if thought is None:
                continue

            #logger.info("  Thought preview: %s", thought[:300].replace("\n", " "))
            logger.info("  Thought:\n%s", thought)
            
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

                fix = generate_fix(test_input, flaky_info, context, thought=thought)
                if fix is None:
                    continue

                ok, result = apply_fix(fix, test_input.repo_root)
                if not ok:
                    logger.warning("  Apply failed: %s", result)
                    continue

                backups = result  # type: ignore[assignment]

                patch_path = write_patch_file(
                    backups,
                    output_dir,
                    prefix=(
                        f"{test_input.test_func}_M{m}_P{p_idx}_N{n}_candidate_{fix_counter}"
                    ),
                    repo_root=test_input.repo_root,
                )

                passed = validate_fix(test_input, patch_path=patch_path)
                if passed:
                    revert_all(backups)

                    logger.info("Fix validated! Patch saved to: %s", patch_path)
                    return True, fix.explanation or thought or "(no explanation)"

                logger.info("Validation failed – reverting")
                revert_all(backups)
            failed_thoughts.append(thought)
    return False, (
        "No fix found after "
        f"M={context_attempts}, P={thoughts_per_context}, N={fixes_per_thought} "
        f"({total_fix_attempts} candidate fixes)."
    )
