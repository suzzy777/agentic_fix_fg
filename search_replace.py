from __future__ import annotations

import datetime
import difflib
import logging
import os
import re
from pathlib import Path

from models import SearchReplaceEdit, Fix

log = logging.getLogger(__name__)

_CODE_FENCE = ("```", "```")

_OPEN_RE = re.compile(r"^[<]{5,9} SEARCH\s*$")
_MID_RE = re.compile(r"^[=]{5,9}\s*$")
_CLOSE_RE = re.compile(r"^[>]{5,9} REPLACE\s*$")

_OPEN_TOKEN = "<<<<<<< SEARCH"
_MID_TOKEN = "======="
_CLOSE_TOKEN = ">>>>>>> REPLACE"

_NO_PATH_MSG = (
    "Could not determine the target file. Put the path on its own line "
    "directly above the opening {fence} fence."
)

_SOURCE_SUFFIXES = (".java", ".py", ".go")


# ── Filename recovery ────────────────────────────────────────────────────────

def _clean_path_line(raw: str, fence: tuple[str, str]) -> str | None:
    """Turn a candidate path line into a bare path, or None if it isn't one."""
    candidate = raw.strip()

    if candidate == "...":
        return None
    if candidate.startswith(fence[0]):
        return None

    for stripper in (
        lambda s: s.rstrip(":"),
        lambda s: s.lstrip("#"),
        lambda s: s.strip(),
        lambda s: s.strip("`"),
        lambda s: s.strip("*"),
    ):
        candidate = stripper(candidate)

    return candidate or None


def _canonical_path(path: str, known_paths: list[str]) -> str:
    """Map a possibly-shortened path onto a known path when we can."""
    path = path.strip().replace("\\", "/").lstrip("/")

    if path in known_paths:
        return path

    tail_hits = [
        known for known in known_paths
        if known.replace("\\", "/").endswith(path)
    ]
    if len(tail_hits) == 1:
        return tail_hits[0]

    return path


def _is_probably_sentence(value: str) -> bool:
    """
    Heuristic guard so explanatory prose ("Looking at this test...") is never
    mistaken for a file path. The upstream parser is looser, but here we always
    have a known default test file to fall back on, so being strict is safer.
    """
    text = value.strip()
    if not text:
        return True
    if len(text) > 260:
        return True

    opener = (
        "looking ", "given ", "based ", "the ", "this ",
        "i ", "i'll ", "let me ", "to fix ", "we need ", "here ",
    )
    if text.lower().startswith(opener):
        return True

    if text.count(" ") >= 4 and not text.endswith(_SOURCE_SUFFIXES):
        return True

    return False


def _dedupe(known_paths: list[str] | None) -> list[str]:
    if not known_paths:
        return []
    seen: list[str] = []
    for path in known_paths:
        if path and path not in seen:
            seen.append(path)
    return seen


def _recover_target_path(
    preceding: list[str],
    fence: tuple[str, str],
    known_paths: list[str] | None,
):
    """
    Look at the handful of lines just above a SEARCH marker and decide which
    file the block refers to. Tries exact, then basename, then fuzzy matching
    against the known paths before giving up.
    """
    known_paths = _dedupe(known_paths)

    # Walk upward, newest line first, only as long as we keep hitting fences /
    # a path-looking line.
    window = list(reversed(preceding))[:3]

    candidates: list[str] = []
    for entry in window:
        parsed = _clean_path_line(entry, fence)
        if parsed:
            candidates.append(parsed)
        if not entry.startswith(fence[0]):
            break

    if not candidates:
        return None

    # 1) exact
    for name in candidates:
        if name in known_paths:
            return name

    # 2) basename
    for name in candidates:
        for known in known_paths:
            if name == Path(known).name:
                return known

    # 3) fuzzy
    for name in candidates:
        near = difflib.get_close_matches(name, known_paths, n=1, cutoff=0.8)
        if len(near) == 1:
            return near[0]

    # With a known-path list, refuse to accept random prose as a path.
    if known_paths:
        for name in candidates:
            if _is_probably_sentence(name):
                continue
            if name.endswith(_SOURCE_SUFFIXES) or "/" in name or "\\" in name:
                return name
        return None

    # No known-path list: looser fallbacks.
    for name in candidates:
        if "." in name and not _is_probably_sentence(name):
            return name
    for name in candidates:
        if not _is_probably_sentence(name):
            return name

    return None


# ── Block extraction ─────────────────────────────────────────────────────────

def _extract_patch_sections(
    text: str,
    fence: tuple[str, str] = _CODE_FENCE,
    known_paths: list[str] | None = None,
):
    """
    Scan an LLM reply and pull out every SEARCH/REPLACE section.

    Returns a list of ``(path, search_body, replace_body)`` tuples. Consecutive
    blocks may share a path (a REPLACE body may be terminated by the next
    divider rather than a REPLACE marker).
    """
    rows = text.splitlines(keepends=True)
    total = len(rows)
    cursor = 0
    active_path: str | None = None
    sections: list[tuple[str, str, str]] = []

    def _stripped(idx: int) -> str:
        return rows[idx].strip()

    while cursor < total:
        if not _OPEN_RE.match(rows[cursor].strip()):
            cursor += 1
            continue

        try:
            # When a divider follows immediately, the path lives purely in the
            # preceding lines (no known-path disambiguation).
            look_back = rows[max(0, cursor - 3):cursor]
            if cursor + 1 < total and _MID_RE.match(_stripped(cursor + 1)):
                path = _recover_target_path(look_back, fence, None)
            else:
                path = _recover_target_path(look_back, fence, known_paths)

            if not path:
                if active_path:
                    path = active_path
                else:
                    raise ValueError(_NO_PATH_MSG.format(fence=fence))
            active_path = path

            # Gather the SEARCH body up to the divider.
            cursor += 1
            search_lines: list[str] = []
            while cursor < total and not _MID_RE.match(_stripped(cursor)):
                search_lines.append(rows[cursor])
                cursor += 1

            if cursor >= total or not _MID_RE.match(_stripped(cursor)):
                raise ValueError(f"missing `{_MID_TOKEN}` divider")

            # Gather the REPLACE body up to the closing marker or the next
            # divider (chained block).
            cursor += 1
            replace_lines: list[str] = []
            while cursor < total and not (
                _CLOSE_RE.match(_stripped(cursor))
                or _MID_RE.match(_stripped(cursor))
            ):
                replace_lines.append(rows[cursor])
                cursor += 1

            if cursor >= total or not (
                _CLOSE_RE.match(_stripped(cursor))
                or _MID_RE.match(_stripped(cursor))
            ):
                raise ValueError(
                    f"block never closed with `{_CLOSE_TOKEN}` or `{_MID_TOKEN}`"
                )

            sections.append(
                (path, "".join(search_lines), "".join(replace_lines))
            )

        except ValueError as err:
            consumed = "".join(rows[: cursor + 1])
            raise ValueError(f"{consumed}\n^^^ {err.args[0]}") from err

        cursor += 1

    return sections


# ── High-level parsing API ───────────────────────────────────────────────────

def parse_edits(
    llm_response: str,
    default_filepath: str = "",
    valid_fnames: list[str] | None = None,
) -> list[SearchReplaceEdit]:
    """Pull SearchReplaceEdit objects out of an LLM reply."""
    known = _dedupe(valid_fnames)
    if default_filepath and default_filepath not in known:
        known.append(default_filepath)

    edits: list[SearchReplaceEdit] = []
    try:
        sections = _extract_patch_sections(
            llm_response,
            fence=_CODE_FENCE,
            known_paths=known or None,
        )
        for path, search_body, replace_body in sections:
            target = path or default_filepath
            target = _canonical_path(target or default_filepath, valid_fnames)
            edits.append(
                SearchReplaceEdit(
                    filepath=target,
                    search_text=search_body.rstrip("\n"),
                    replace_text=replace_body.rstrip("\n"),
                )
            )
    except Exception as err:  # noqa: BLE001 - best-effort parse
        log.warning("Could not parse SEARCH/REPLACE blocks: %s", err)

    return edits


def parse_explanation(llm_response: str) -> str:
    """Return the text inside <EXPLANATION>...</EXPLANATION>, if present."""
    hit = re.search(r"<EXPLANATION>(.*?)</EXPLANATION>", llm_response, re.DOTALL)
    return hit.group(1).strip() if hit else ""


def parse_fix(
    llm_response: str,
    default_filepath: str = "",
    valid_fnames: list[str] | None = None,
) -> Fix:
    """Build a Fix (edits + explanation) from an LLM reply."""
    return Fix(
        edits=parse_edits(
            llm_response,
            default_filepath=default_filepath,
            valid_fnames=valid_fnames,
        ),
        explanation=parse_explanation(llm_response),
    )


# ── File snapshots for atomic application ────────────────────────────────────

class FileBackup:
    """Holds one file's contents so it can be restored on failure."""

    def __init__(self, filepath: str):
        self.filepath = filepath
        self._original: str | None = None

    # Kept for callers/tests that read the saved text directly.
    @property
    def _backup(self) -> str | None:  # noqa: D401 - compat shim
        return self._original

    def save(self) -> bool:
        try:
            with open(self.filepath, "r", encoding="utf-8", errors="replace") as fh:
                self._original = fh.read()
            return True
        except OSError as err:
            log.error("Could not snapshot %s: %s", self.filepath, err)
            return False

    def revert(self) -> bool:
        if self._original is None:
            return False
        try:
            with open(self.filepath, "w", encoding="utf-8") as fh:
                fh.write(self._original)
            return True
        except OSError as err:
            log.error("Could not restore %s: %s", self.filepath, err)
            return False


def _substitute(content: str, edit: SearchReplaceEdit) -> tuple[bool, str]:
    """Replace the first occurrence of edit.search_text, if it exists."""
    if edit.search_text in content:
        return True, content.replace(edit.search_text, edit.replace_text, 1)
    return False, content


def _locate_on_disk(edit_path: str, repo_root: str) -> str:
    """Absolute path for an edit, resolving shortened paths under repo_root."""
    filepath = edit_path
    if not os.path.isabs(filepath):
        filepath = os.path.join(repo_root, filepath)
    return filepath


def apply_fix(fix: Fix, repo_root: str) -> tuple[bool, dict[str, FileBackup] | str]:
    """
    Apply every edit atomically: resolve paths, pre-validate all search texts,
    snapshot the files, write, and roll everything back if any step fails.
    """
    if not fix.edits:
        return False, "No search-replace edits to apply"

    resolved: list[tuple[str, SearchReplaceEdit]] = []

    for edit in fix.edits:
        filepath = _locate_on_disk(edit.filepath, repo_root)

        if not os.path.isfile(filepath):
            # Try to resolve a shortened path by unique suffix match.
            wanted = edit.filepath.replace("\\", "/").lstrip("/")
            hits = []
            for root, _dirs, names in os.walk(repo_root):
                for name in names:
                    full = os.path.join(root, name)
                    rel = os.path.relpath(full, repo_root).replace("\\", "/")
                    if rel.endswith(wanted):
                        hits.append(full)

            if len(hits) == 1:
                filepath = hits[0]
                edit.filepath = os.path.relpath(filepath, repo_root)
                log.info("Resolved short path %r -> %s", wanted, edit.filepath)
            else:
                return False, (
                    f"File does not exist: {filepath}\n"
                    f"Could not uniquely resolve short path {wanted!r}; "
                    f"matches={len(hits)}"
                )

        resolved.append((filepath, edit))

    # Pre-flight: every search text must be present before we touch anything.
    for filepath, edit in resolved:
        with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
            disk_text = fh.read()
        if disk_text.find(edit.search_text) == -1:
            return False, (
                f"Pre-validation failed in {filepath}: "
                f"Search text not found:\n"
                f"----- SEARCH START -----\n"
                f"{edit.search_text}\n"
                f"----- SEARCH END -----"
            )

    backups: dict[str, FileBackup] = {}
    try:
        for filepath, _edit in resolved:
            if filepath not in backups:
                snapshot = FileBackup(filepath)
                if not snapshot.save():
                    return False, f"Could not back up {filepath}"
                backups[filepath] = snapshot

        working = {
            filepath: (backups[filepath]._original or "")
            for filepath in backups
        }

        for filepath, edit in resolved:
            ok, updated = _substitute(working[filepath], edit)
            if not ok:
                raise ValueError(
                    f"Search text not found during application in {filepath}:\n"
                    f"{edit.search_text[:200]}..."
                )
            working[filepath] = updated

        for filepath, content in working.items():
            with open(filepath, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())

        return True, backups

    except Exception as err:  # noqa: BLE001 - roll back on any failure
        for snapshot in backups.values():
            snapshot.revert()
        return False, f"Atomic operation failed, all changes reverted: {err}"


def revert_all(backups: dict[str, FileBackup]) -> None:
    """Restore every snapshotted file."""
    for snapshot in backups.values():
        snapshot.revert()


def write_patch_file(
    backups: dict[str, FileBackup],
    output_dir: str,
    prefix: str = "fix",
    repo_root: str | None = None,
) -> str:
    """
    Emit a unified diff for the applied edits.

    With repo_root set, headers use repo-relative paths so the diff applies to
    the artifact copy with ``patch -p1``.
    """
    os.makedirs(output_dir, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    patch_path = os.path.join(output_dir, f"{prefix}_{stamp}.patch")

    with open(patch_path, "w", encoding="utf-8") as out:
        for filepath, snapshot in backups.items():
            before = (snapshot._original or "").splitlines(keepends=True)

            try:
                with open(filepath, "r", encoding="utf-8") as fh:
                    after = fh.readlines()
            except OSError:
                continue

            if repo_root:
                try:
                    rel = os.path.relpath(filepath, repo_root)
                except ValueError:
                    rel = os.path.basename(filepath)
            else:
                rel = os.path.basename(filepath)
            rel = rel.replace(os.sep, "/")

            out.writelines(
                difflib.unified_diff(
                    before, after, fromfile=f"a/{rel}", tofile=f"b/{rel}"
                )
            )

    return patch_path
