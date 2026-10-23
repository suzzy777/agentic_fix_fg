"""
editing/search_replace.py – parse and apply SEARCH/REPLACE patches.

This version follows the original FlakyGuard/Aider-style parsing approach
more closely than the earlier simplified regex parser.

Original FlakyGuard used:
- find_original_update_blocks(...)
- find_filename(...)
- Aider-style SEARCH/REPLACE block rules

The simplified API is preserved:
- parse_fix(...)
- apply_fix(...)
- revert_all(...)
- write_patch_file(...)
"""

from __future__ import annotations

import datetime
import difflib
import logging
import os
import re
from pathlib import Path

from models import SearchReplaceEdit, Fix

logger = logging.getLogger(__name__)

# ── Aider / original-FlakyGuard style parser constants ───────────────────────

fence = ("```", "```")
DEFAULT_FENCE = fence

HEAD = r"^<{5,9} SEARCH\s*$"
DIVIDER = r"^={5,9}\s*$"
UPDATED = r"^>{5,9} REPLACE\s*$"

HEAD_ERR = "<<<<<<< SEARCH"
DIVIDER_ERR = "======="
UPDATED_ERR = ">>>>>>> REPLACE"

missing_filename_err = (
    "Bad/missing filename. The filename must be alone on the line before "
    "the opening fence {fence[0]}"
)


def strip_filename(filename: str, fence: tuple[str, str]):
    """
    Original FlakyGuard/Aider helper.

    Cleans a candidate filename line found near a SEARCH/REPLACE block.
    """
    filename = filename.strip()

    if filename == "...":
        return None

    start_fence = fence[0]
    if filename.startswith(start_fence):
        return None

    filename = filename.rstrip(":")
    filename = filename.lstrip("#")
    filename = filename.strip()
    filename = filename.strip("`")
    filename = filename.strip("*")

    return filename or None

def _resolve_fname(fname: str, valid_fnames: list[str]) -> str:
    fname = fname.strip().replace("\\", "/").lstrip("/")

    if fname in valid_fnames:
        return fname

    matches = [
        valid for valid in valid_fnames
        if valid.replace("\\", "/").endswith(fname)
    ]

    if len(matches) == 1:
        return matches[0]

    return fname

def _looks_like_prose(value: str) -> bool:
    """
    Reject obvious explanation text.

    This is the only small safety guard added around the original methodology.
    It prevents sentences like "Looking at this test..." from being treated as
    filenames. The original parser is more flexible than the simplified regex,
    but the simplified setup has a known default file, so falling back is safer.
    """
    text = value.strip()
    if not text:
        return True

    if len(text) > 260:
        return True

    lowered = text.lower()
    prose_starts = (
        "looking ",
        "given ",
        "based ",
        "the ",
        "this ",
        "i ",
        "i'll ",
        "let me ",
        "to fix ",
        "we need ",
        "here ",
    )
    if lowered.startswith(prose_starts):
        return True

    # A natural-language sentence with many spaces is not a path.
    if text.count(" ") >= 4 and not text.endswith((".java", ".py", ".go")):
        return True

    return False


def _normalize_valid_fnames(valid_fnames: list[str] | None) -> list[str]:
    if not valid_fnames:
        return []
    result = []
    for fname in valid_fnames:
        if fname and fname not in result:
            result.append(fname)
    return result


def find_filename(
    lines: list[str],
    fence: tuple[str, str],
    valid_fnames: list[str] | None,
):
    """
    Original FlakyGuard/Aider-style filename lookup.

    It searches backward through the few lines before a SEARCH block, rather
    than blindly assuming the immediately previous line is a filename.
    """
    valid_fnames = _normalize_valid_fnames(valid_fnames)

    # Go back through the 3 preceding lines.
    lines = list(lines)
    lines.reverse()
    lines = lines[:3]

    filenames: list[str] = []
    for line in lines:
        filename = strip_filename(line, fence)
        if filename:
            filenames.append(filename)

        # Only continue as long as we keep seeing fences.
        if not line.startswith(fence[0]):
            break

    if not filenames:
        return None

    # Exact match first.
    for fname in filenames:
        if fname in valid_fnames:
            return fname

    # Basename match.
    for fname in filenames:
        for valid_fname in valid_fnames:
            if fname == Path(valid_fname).name:
                return valid_fname

    # Fuzzy match.
    for fname in filenames:
        close_matches = difflib.get_close_matches(
            fname,
            valid_fnames,
            n=1,
            cutoff=0.8,
        )
        if len(close_matches) == 1:
            return close_matches[0]

    # If we know valid filenames, do not fall back to random prose.
    if valid_fnames:
        for fname in filenames:
            if _looks_like_prose(fname):
                continue
            if fname.endswith((".java", ".py", ".go")) or "/" in fname or "\\" in fname:
                return fname
        return None

    # Original fallback behavior when no valid filename list is available.
    for fname in filenames:
        if "." in fname and not _looks_like_prose(fname):
            return fname

    for fname in filenames:
        if not _looks_like_prose(fname):
            return fname

    return None


def find_original_update_blocks(
    content: str,
    fence: tuple[str, str] = DEFAULT_FENCE,
    valid_fnames: list[str] | None = None,
):
    """
    Original FlakyGuard/Aider-style SEARCH/REPLACE block parser.

    Returns:
        list of (filename, original_text, updated_text)
    """
    lines = content.splitlines(keepends=True)
    i = 0
    current_filename = None

    head_pattern = re.compile(HEAD)
    divider_pattern = re.compile(DIVIDER)
    updated_pattern = re.compile(UPDATED)

    result: list[tuple[str, str, str]] = []

    while i < len(lines):
        line = lines[i]

        if head_pattern.match(line.strip()):
            try:
                if i + 1 < len(lines) and divider_pattern.match(lines[i + 1].strip()):
                    filename = find_filename(lines[max(0, i - 3):i], fence, None)
                else:
                    filename = find_filename(lines[max(0, i - 3):i], fence, valid_fnames)

                if not filename:
                    if current_filename:
                        filename = current_filename
                    else:
                        raise ValueError(missing_filename_err.format(fence=fence))

                current_filename = filename

                original_text: list[str] = []
                i += 1
                while i < len(lines) and not divider_pattern.match(lines[i].strip()):
                    original_text.append(lines[i])
                    i += 1

                if i >= len(lines) or not divider_pattern.match(lines[i].strip()):
                    raise ValueError(f"Expected `{DIVIDER_ERR}`")

                updated_text: list[str] = []
                i += 1
                while (
                    i < len(lines)
                    and not updated_pattern.match(lines[i].strip())
                    and not divider_pattern.match(lines[i].strip())
                ):
                    updated_text.append(lines[i])
                    i += 1

                if i >= len(lines) or not (
                    updated_pattern.match(lines[i].strip())
                    or divider_pattern.match(lines[i].strip())
                ):
                    raise ValueError(f"Expected `{UPDATED_ERR}` or `{DIVIDER_ERR}`")

                result.append((filename, "".join(original_text), "".join(updated_text)))

            except ValueError as exc:
                processed = "".join(lines[: i + 1])
                err = exc.args[0]
                raise ValueError(f"{processed}\n^^^ {err}") from exc

        i += 1

    return result


# ── Public parsing API used by the simplified pipeline ───────────────────────

def parse_edits(
    llm_response: str,
    default_filepath: str = "",
    valid_fnames: list[str] | None = None,
) -> list[SearchReplaceEdit]:
    """
    Extract search/replace edits from the LLM response.

    This uses the original FlakyGuard/Aider-style parser, with the simplified
    test file as a valid/default filename when available.
    """
    valid = _normalize_valid_fnames(valid_fnames)
    if default_filepath and default_filepath not in valid:
        valid.append(default_filepath)

    edits: list[SearchReplaceEdit] = []

    try:
        blocks = find_original_update_blocks(
            llm_response,
            fence=DEFAULT_FENCE,
            valid_fnames=valid or None,
        )

        

        for filename, search_text, replace_text in blocks:
            #filepath = _resolve_fname(filepath or default_filepath, valid_fnames)

            filepath = filename or default_filepath

            filepath = _resolve_fname(filepath or default_filepath, valid_fnames)
            
            edits.append(SearchReplaceEdit(
                filepath=filepath,
                search_text=search_text.rstrip("\n"),
                replace_text=replace_text.rstrip("\n"),
            ))

    except Exception as exc:
        logger.warning("Failed to parse SEARCH/REPLACE blocks: %s", exc)

    return edits


def parse_explanation(llm_response: str) -> str:
    """Extract text between <EXPLANATION> tags."""
    match = re.search(r"<EXPLANATION>(.*?)</EXPLANATION>", llm_response, re.DOTALL)
    return match.group(1).strip() if match else ""


def parse_fix(
    llm_response: str,
    default_filepath: str = "",
    valid_fnames: list[str] | None = None,
) -> Fix:
    """Parse a complete Fix from an LLM response."""
    return Fix(
        edits=parse_edits(
            llm_response,
            default_filepath=default_filepath,
            valid_fnames=valid_fnames,
        ),
        explanation=parse_explanation(llm_response),
    )


# ── Application API used by the simplified pipeline ──────────────────────────

class FileBackup:
    """Saves and restores a single file."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self._backup: str | None = None

    def save(self) -> bool:
        try:
            with open(self.filepath, "r", encoding="utf-8", errors="replace") as file:
                self._backup = file.read()
            return True
        except OSError as exc:
            logger.error("Backup failed for %s: %s", self.filepath, exc)
            return False

    def revert(self) -> bool:
        if self._backup is None:
            return False

        try:
            with open(self.filepath, "w", encoding="utf-8") as file:
                file.write(self._backup)
            return True
        except OSError as exc:
            logger.error("Revert failed for %s: %s", self.filepath, exc)
            return False


def _apply_one_edit(content: str, edit: SearchReplaceEdit) -> tuple[bool, str]:
    """Apply a single search/replace edit to content."""
    if edit.search_text in content:
        return True, content.replace(edit.search_text, edit.replace_text, 1)

    return False, content


def apply_fix(fix: Fix, repo_root: str) -> tuple[bool, dict[str, FileBackup] | str]:
    """
    Apply all edits atomically.

    This matches the original behavior conceptually:
    pre-check all edits, back up all files, apply edits, and revert everything
    on failure.
    """
    if not fix.edits:
        return False, "No search-replace edits to apply"

    resolved: list[tuple[str, SearchReplaceEdit]] = []

    for edit in fix.edits:
        filepath = edit.filepath
        if not os.path.isabs(filepath):
            filepath = os.path.join(repo_root, filepath)

        # if not os.path.isfile(filepath):
        #     return False, f"File does not exist: {filepath}"
        if not os.path.isfile(filepath):
            original = edit.filepath.replace("\\", "/").lstrip("/")

            matches = []
            for root, _, files in os.walk(repo_root):
                for name in files:
                    candidate = os.path.join(root, name)
                    rel = os.path.relpath(candidate, repo_root).replace("\\", "/")

                    if rel.endswith(original):
                        matches.append(candidate)

            if len(matches) == 1:
                filepath = matches[0]
                edit.filepath = os.path.relpath(filepath, repo_root)
                logger.info(
                    "Resolved shortened patch path %r to %s",
                    original,
                    edit.filepath,
                )
            else:
                return False, (
                    f"File does not exist: {filepath}\n"
                    f"Could not uniquely resolve short path {original!r}; "
                    f"matches={len(matches)}"
                )
    
        resolved.append((filepath, edit))

    # Pre-check all edits before writing anything.
    for filepath, edit in resolved:
        with open(filepath, "r", encoding="utf-8", errors="replace") as file:
            content = file.read()

        # if edit.search_text not in content:
        #     return False, (
        #         f"Pre-validation failed in {filepath}: "
        #         f"Search text not found:\n{edit.search_text[:200]}..."
        #     )

        if edit.search_text not in content:
            return False, (
                f"Pre-validation failed in {filepath}: "
                f"Search text not found:\n"
                f"----- SEARCH START -----\n"
                f"{edit.search_text}\n"
                f"----- SEARCH END -----"
            )

    backups: dict[str, FileBackup] = {}
    try:
        for filepath, _ in resolved:
            if filepath not in backups:
                backup = FileBackup(filepath)
                if not backup.save():
                    return False, f"Could not back up {filepath}"
                backups[filepath] = backup

        file_contents = {
            filepath: backups[filepath]._backup or ""
            for filepath in backups
        }

        for filepath, edit in resolved:
            ok, new_content = _apply_one_edit(file_contents[filepath], edit)
            if not ok:
                raise ValueError(
                    f"Search text not found during application in {filepath}:\n"
                    f"{edit.search_text[:200]}..."
                )
            file_contents[filepath] = new_content

        for filepath, content in file_contents.items():
            with open(filepath, "w", encoding="utf-8") as file:
                file.write(content)
                file.flush()
                os.fsync(file.fileno())

        return True, backups

    except Exception as exc:
        for backup in backups.values():
            backup.revert()
        return False, f"Atomic operation failed, all changes reverted: {exc}"


def revert_all(backups: dict[str, FileBackup]) -> None:
    """Revert all backed-up files."""
    for backup in backups.values():
        backup.revert()


def write_patch_file(
    backups: dict[str, FileBackup],
    output_dir: str,
    prefix: str = "fix",
    repo_root: str | None = None,
) -> str:
    """Write a unified diff patch file to output_dir.

    If repo_root is provided, patch headers use repo-relative paths so the
    patch can be applied to the ReproFlake artifact copy with patch -p1.
    """
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    patch_path = os.path.join(output_dir, f"{prefix}_{timestamp}.patch")

    with open(patch_path, "w", encoding="utf-8") as patch_file:
        for filepath, backup in backups.items():
            original = (backup._backup or "").splitlines(keepends=True)

            try:
                with open(filepath, "r", encoding="utf-8") as file:
                    modified = file.readlines()
            except OSError:
                continue

            if repo_root:
                try:
                    relpath = os.path.relpath(filepath, repo_root)
                except ValueError:
                    relpath = os.path.basename(filepath)
            else:
                relpath = os.path.basename(filepath)
            relpath = relpath.replace(os.sep, "/")

            diff = difflib.unified_diff(
                original,
                modified,
                fromfile=f"a/{relpath}",
                tofile=f"b/{relpath}",
            )
            patch_file.writelines(diff)

    return patch_path
