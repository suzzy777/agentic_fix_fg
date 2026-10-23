"""
compile_repair.py – the compilation-retry step, adapted to this project's
Maven/Java + Docker setup.

A candidate fix from the LLM can break compilation (a bad import, a syntax
slip, a reference to a symbol it renamed/removed). Rather than just discard
such a candidate, this runs a fast build-only check, and
on failure parses the compiler diagnostics and asks the LLM -- in a separate,
narrowly-scoped prompt -- to patch just those errors using the pre-fix file
content as reference, then rebuilds. This repeats up to
MAX_FIX_BUILD_RETRIES times before the candidate is given up on.

IMPORTANT DIFFERENCE FROM THE ORIGINAL: the original ran `bazel build`
directly in the monorepo checkout it was editing, so its "rebuild" step was
just another local subprocess call. This project's actual compile/test step
does NOT run on the host at all -- it runs inside a per-JDK-version Docker
container that the ReproFlake helper scripts spin up
(flaky_analysis_tool_id_*.sh: docker run with the source bind-mounted to
/app/source, then `docker exec ... mvn clean install -pl $MODULE -am ...`
inside it; see runner.py::validate_fix_detailed). There is no separate,
correctly-configured Maven/JDK toolchain on the host to build against.

So this module does NOT run any build itself. It only:
  1. Parses javac/Maven compiler diagnostics out of the output that a
     *real* ReproFlake/Docker validation run already produced
     (runner.py::validate_fix_detailed's second return value).
  2. Maps each diagnostic's path (as seen inside the container, e.g.
     /app/source/<module>/src/main/java/...) back to the matching file
     under test_input.repo_root, the host checkout that candidate fixes are
     edited in.
  3. Asks the LLM to patch just those errors and applies the edits to
     repo_root, merging any newly-touched files into the caller's backups
     dict so a later revert_all() still fully undoes the candidate.

The caller (pipeline.py) is responsible for the "rebuild": after a
successful repair here, it regenerates the patch file from repo_root and
calls validate_fix_detailed() again, which re-drives the same Docker
container flow. That loop is what plays the role of MAX_FIX_BUILD_RETRIES.
"""

from __future__ import annotations

import logging
import os
import re

from models import CompilationDefect, TestInput
from prompts import get_build_error_fix_prompt
from search_replace import FileBackup, apply_fix as apply_edits, parse_fix

from llm import complete

logger = logging.getLogger(__name__)

# Maven's compiler plugin reports javac diagnostics as, e.g.:
#   [ERROR] COMPILATION ERROR :
#   [ERROR] /app/source/module/src/main/java/com/foo/Bar.java:[23,45] cannot find symbol
# Plain javac invocations (no Maven wrapper) instead look like:
#   src/main/java/com/foo/Bar.java:23: error: cannot find symbol
_MAVEN_JAVAC_RE = re.compile(
    r"\[(?P<severity>ERROR|WARNING)\]\s+(?P<path>[^\s:]+\.java):\[(?P<line>\d+),(?P<col>\d+)\]\s+(?P<msg>.+)"
)
_PLAIN_JAVAC_RE = re.compile(
    r"^(?P<path>[^\s:]+\.java):(?P<line>\d+):\s*(?P<severity>error|warning):\s*(?P<msg>.+)$",
    re.MULTILINE,
)

# The bind-mount target used by flaky_analysis_tool_id_*.sh
# (docker run --mount type=bind,source=$HOST_SRC_ABS,target=/app/source).
# Compiler-reported paths under this prefix need it stripped before they can
# be resolved against test_input.repo_root on the host.
_CONTAINER_SOURCE_PREFIXES = ("/app/source/",)


def parse_compile_defects(output: str) -> list[CompilationDefect]:
    """Extract compiler defects from captured Maven/javac build output."""
    defects: list[CompilationDefect] = []
    seen: set[tuple[str, int, str]] = set()

    for match in _MAVEN_JAVAC_RE.finditer(output):
        key = (match.group("path"), int(match.group("line")), match.group("msg").strip())
        if key in seen:
            continue
        seen.add(key)
        defects.append(
            CompilationDefect(
                path=match.group("path"),
                line=int(match.group("line")),
                severity=match.group("severity").upper(),
                message=match.group("msg").strip(),
            )
        )

    if not defects:
        for match in _PLAIN_JAVAC_RE.finditer(output):
            key = (match.group("path"), int(match.group("line")), match.group("msg").strip())
            if key in seen:
                continue
            seen.add(key)
            defects.append(
                CompilationDefect(
                    path=match.group("path"),
                    line=int(match.group("line")),
                    severity=match.group("severity").upper(),
                    message=match.group("msg").strip(),
                )
            )

    return defects


def _resolve_defect_source_path(defect_path: str, repo_root: str) -> str | None:
    """
    Map a compiler-reported path (as seen inside the ReproFlake Docker
    container) back to the matching file under test_input.repo_root, the
    host checkout candidate fixes are edited/diffed in.

    Tries the direct mapping first (strip the known container mount prefix,
    join onto repo_root); falls back to a suffix search under repo_root for
    cases where the module layout doesn't line up exactly (e.g. a differing
    reactor-relative vs. mount-relative path).
    """
    candidate = defect_path
    for prefix in _CONTAINER_SOURCE_PREFIXES:
        if candidate.startswith(prefix):
            candidate = candidate[len(prefix):]
            break
    else:
        candidate = candidate.lstrip("/")

    direct = os.path.join(repo_root, candidate)
    if os.path.isfile(direct):
        return direct

    parts = candidate.replace("\\", "/").split("/")
    for depth in range(len(parts) - 1, 0, -1):
        suffix = "/".join(parts[-depth:])
        matches = []
        for root, _dirs, files in os.walk(repo_root):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, repo_root).replace("\\", "/")
                if rel.endswith(suffix):
                    matches.append(full)
        if len(matches) == 1:
            return matches[0]

    return None


def _repair_defects_with_llm(
    test_input: TestInput,
    defects: list[CompilationDefect],
    backups: dict[str, FileBackup],
    log_dir: str | None = None,
    tag: str = "buildfix",
) -> bool:
    """
    Resolve each defect's file, build a prompt with current (broken) +
    original (pre-fix) content as reference, ask the LLM for SEARCH/REPLACE
    edits, and apply them to test_input.repo_root.

    Any newly-touched file's backup is merged into `backups` so the
    caller's eventual revert_all() still fully undoes this candidate if it
    is later rejected.
    """
    by_resolved_path: dict[str, tuple[str, list[CompilationDefect]]] = {}
    for defect in defects:
        full_path = _resolve_defect_source_path(defect.path, test_input.repo_root)
        if not full_path:
            logger.warning(
                "Compile-repair: could not resolve compiler-reported path %r to a "
                "file under repo_root=%s; skipping this defect.",
                defect.path, test_input.repo_root,
            )
            continue
        rel_path = os.path.relpath(full_path, test_input.repo_root)
        by_resolved_path.setdefault(full_path, (rel_path, []))[1].append(defect)

    if not by_resolved_path:
        return False

    defect_lines: list[str] = []
    current_blocks: list[str] = []
    original_blocks: list[str] = []
    valid_fnames: list[str] = []

    for full_path, (rel_path, file_defects) in by_resolved_path.items():
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                current_content = f.read()
        except OSError:
            continue

        valid_fnames.append(rel_path)

        for defect in sorted(file_defects, key=lambda d: d.line):
            defect_lines.append(
                f"File: {rel_path}, Line: {defect.line}\n"
                f"Severity: {defect.severity}\nMessage: {defect.message}\n"
            )

        current_blocks.append(
            f"**Current (broken) code for {rel_path}:**\n```{test_input.language}\n{current_content}\n```\n"
        )

        backup = backups.get(full_path)
        if backup is not None and backup._backup is not None:
            original_blocks.append(
                f"**Original (pre-fix, previously compiling) code for {rel_path}:**\n"
                f"```{test_input.language}\n{backup._backup}\n```\n"
            )

    if not current_blocks:
        return False

    prompt = get_build_error_fix_prompt(
        defects_text="\n".join(defect_lines),
        language=test_input.language,
        current_content="\n".join(current_blocks),
        original_content_hint="\n".join(original_blocks),
    )

    response = complete(prompt, temperature=0.0, log_dir=log_dir, tag=tag)
    if not response:
        return False

    fix = parse_fix(
        response,
        default_filepath=valid_fnames[0] if valid_fnames else "",
        valid_fnames=valid_fnames,
    )
    if not fix.edits:
        logger.warning("Compile-repair LLM call produced no SEARCH/REPLACE edits.")
        return False

    ok, result = apply_edits(fix, test_input.repo_root)
    if not ok:
        logger.warning("Compile-repair edits failed to apply: %s", result)
        return False

    new_backups = result  # type: ignore[assignment]
    for filepath, backup in new_backups.items():
        backups.setdefault(filepath, backup)

    return True


def attempt_compile_repair(
    test_input: TestInput,
    backups: dict[str, FileBackup],
    validation_output: str,
    log_dir: str | None = None,
    tag: str = "buildfix",
) -> tuple[bool, list[CompilationDefect]]:
    """
    One repair pass of the compilation-retry step.

    Given the raw output from a *failed* validate_fix_detailed() run, try to
    parse concrete javac/Maven compiler defects out of it. If found, ask the
    LLM to patch them in test_input.repo_root (edits are applied directly,
    same as the outer candidate fix).

    This does NOT run any build -- see the module docstring for why. The
    caller is expected to regenerate the patch file and call
    validate_fix_detailed() again to actually recompile/retest, in a loop
    bounded by test_input.max_compile_fix_retries.

    Returns (repaired, defects):
      - defects: parsed compiler defects, or [] if the failure doesn't look
        like a compile error (e.g. it's a genuine test/assertion failure,
        or output from a language this module doesn't parse).
      - repaired: True if edits were applied and it's worth re-validating.
        False (with defects possibly non-empty) means give up on this
        candidate -- either there was nothing parseable to act on, or the
        LLM repair attempt produced no usable edits.
    """
    if test_input.language != "java" or not test_input.verify_compile_before_validate:
        return False, []

    # Only real compiler errors mean the patch broke the build; warnings are
    # normal in many projects and would send the LLM editing unrelated code.
    defects = [d for d in parse_compile_defects(validation_output) if d.severity == "ERROR"]
    if not defects:
        return False, []

    repaired = _repair_defects_with_llm(test_input, defects, backups, log_dir=log_dir, tag=tag)
    return repaired, defects
