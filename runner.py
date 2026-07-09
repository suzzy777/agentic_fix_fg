from __future__ import annotations

import csv
import difflib
import hashlib
import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from models import TestInput, FlakyInfo

logger = logging.getLogger(__name__)


# ── Generic command helpers ──────────────────────────────────────────────────

def _build_cmd(test_input: TestInput) -> str:
    """Expand the fallback run_cmd template for the given test input."""
    test_dir = os.path.dirname(test_input.test_file)
    return test_input.run_cmd.format(
        test_func=test_input.test_func,
        test_case=test_input.test_case,
        test_file=test_input.test_file,
        test_dir=test_dir or ".",
    )


def _run_once(cmd: str, cwd: str, timeout: int = 120) -> tuple[bool, str]:
    """Run cmd, return (passed, combined_output)."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT"

def _timeout_text(value) -> str:
    """Convert TimeoutExpired stdout/stderr to text even when Python returns bytes."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _parse_script_summary(output: str) -> tuple[int, int, int] | None:
    blocks = re.findall(
        r"Passes:\s*(\d+)\s*\n\s*Failures:\s*(\d+)\s*\n\s*Errors:\s*(\d+)", output
    )
    if not blocks:
        return None
    best = max(blocks, key=lambda b: int(b[1]) + int(b[2]))
    return int(best[0]), int(best[1]), int(best[2])

_PROGRESS_LINE_RE = re.compile(
    r"INSIDE COVERAGE GENERATOR"
    r"|^Summary:|^Passes:|^Failures:|^Errors:"
    r"|Jacoco exec not found"
    r"|Failed to apply patch"
    r"|skipping coverage generation"
    r"|Failed to patch .*pom"
)


def _run_script_streaming(cmd: str, cwd: str, timeout, env) -> tuple[int, str]:
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )
    chunks: list[str] = []

    def _pump() -> None:
        for line in proc.stdout:
            chunks.append(line)
            if _PROGRESS_LINE_RE.search(line):
                logger.info("[repro] %s", line.rstrip())

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()

    start = time.monotonic()
    while True:
        remaining = None if timeout is None else timeout - (time.monotonic() - start)
        if remaining is not None and remaining <= 0:
            proc.kill()
            proc.wait()
            reader.join(timeout=5)
            raise subprocess.TimeoutExpired(cmd, timeout, output="".join(chunks))
        try:
            proc.wait(timeout=60.0 if remaining is None else min(60.0, remaining))
            break
        except subprocess.TimeoutExpired:
            logger.info(
                "[repro] ... still running (%.0f min elapsed, %d output lines so far)",
                (time.monotonic() - start) / 60.0,
                len(chunks),
            )

    reader.join(timeout=5)
    return proc.returncode, "".join(chunks)


def _safe_rmtree(path: Path) -> None:
    """Remove a directory, fixing Docker-created permissions when possible."""
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
        return
    except PermissionError:
        logger.warning("Permission denied deleting %s; trying chmod/chown cleanup and retrying.", path)

    try:
        subprocess.run(["chmod", "-R", "u+rwX", str(path)], check=False, capture_output=True, text=True)
        shutil.rmtree(path)
        return
    except PermissionError:
        pass

    try:
        subprocess.run(
            [
                "docker", "run", "--rm",
                "-v", f"{path.parent.resolve()}:/host",
                "alpine", "sh", "-c",
                f"chown -R {os.getuid()}:{os.getgid()} /host/{path.name} && chmod -R u+rwX /host/{path.name}",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception as exc:
        logger.warning("Docker chown cleanup failed for %s: %s", path, exc)

    shutil.rmtree(path)


def _script_repro_iterations() -> int:
    """Iterations for script-based reproduction only. Keep this small/fast."""
    raw = os.environ.get("FLAKYGUARD_REPRO_ITERATIONS", "10")
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning("Invalid FLAKYGUARD_REPRO_ITERATIONS=%r; using 10.", raw)
        return 10


# ── Failure parsing ──────────────────────────────────────────────────────────

def _extract_flaky_info(output: str, test_input: TestInput) -> FlakyInfo | None:
    """Parse a test failure output into a FlakyInfo."""
    lang = test_input.language.lower()

    if lang == "go":
        return _parse_go_failure(output)
    if lang == "python":
        return _parse_python_failure(output)
    if lang == "java":
        return _parse_java_failure(output)

    if output.strip():
        return FlakyInfo(error=output[:500], error_trace=output)
    return None


def _parse_go_failure(output: str) -> FlakyInfo | None:
    if "FAIL" not in output and "panic" not in output:
        return None

    error_lines: list[str] = []
    trace_lines: list[str] = []
    in_trace = False

    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("--- FAIL") or "Error:" in stripped or "FAIL\t" in stripped:
            error_lines.append(stripped)
        if re.match(r"\s+\S+\.go:\d+", line) or stripped.startswith("goroutine"):
            in_trace = True
        if in_trace:
            trace_lines.append(line)

    error = "\n".join(error_lines[:10]) or output[:300]
    trace = "\n".join(trace_lines[:40]) or output[:1000]

    error_file, error_line = "", 0
    match = re.search(r"(\S+\.go):(\d+)", trace)
    if match:
        error_file, error_line = match.group(1), int(match.group(2))

    return FlakyInfo(error=error, error_trace=trace, error_file=error_file, error_line=error_line)


def _parse_python_failure(output: str) -> FlakyInfo | None:
    if "FAILED" not in output and "ERROR" not in output and "assert" not in output.lower():
        return None

    failure_section = ""
    match = re.search(r"={3,} FAILURES ={3,}(.*?)(?:={3,}|\Z)", output, re.DOTALL)
    if match:
        failure_section = match.group(1)

    error_match = re.search(r"(AssertionError.*|E\s+assert.*|Exception.*)", failure_section)
    error = error_match.group(0)[:300] if error_match else failure_section[:300] or output[:300]

    error_file, error_line = "", 0
    file_match = re.search(r"(\S+\.py):(\d+)", output)
    if file_match:
        error_file, error_line = file_match.group(1), int(file_match.group(2))

    return FlakyInfo(error=error, error_trace=failure_section or output[:1000], error_file=error_file, error_line=error_line)


def _parse_java_failure(output: str) -> FlakyInfo | None:
    failure_markers = (
        "<<< FAILURE!",
        "AssertionFailedError",
        "AssertionError",
        "Tests run:",
        "There are test failures",
        "FAILURE",
        "ERROR",
    )
    if not any(marker in output for marker in failure_markers):
        return None

    lines = output.splitlines()

    error_lines = [
        line.strip()
        for line in lines
        if (
            "AssertionFailedError" in line
            or "AssertionError" in line
            or "<<< FAILURE!" in line
            or re.search(r"expected:.*but was:", line)
        )
    ]

    if not error_lines:
        error_lines = [
            line.strip()
            for line in lines
            if "Exception" in line or "Failed" in line or "FAILURE" in line
        ]

    error = "\n".join(error_lines[:8]) or output[:500]

    error_file, error_line = "", 0
    match = re.search(r"\(([^()\s]+\.java):(\d+)\)", output)
    if match:
        error_file, error_line = match.group(1), int(match.group(2))
    else:
        match = re.search(r"([^\s()]+\.java):(\d+)", output)
        if match:
            error_file, error_line = match.group(1), int(match.group(2))

    return FlakyInfo(error=error, error_trace=output[:4000], error_file=error_file, error_line=error_line)


# ── ReproFlake script helpers ────────────────────────────────────────────────

def _zip_stem(zip_path: str) -> str:
    """Return zip filename without the .zip suffix."""
    name = os.path.basename(zip_path)
    return name[:-4] if name.endswith(".zip") else name


def _prepare_script_workdir(test_input: TestInput) -> str:
    if not test_input.repro_issue_id:
        raise ValueError("--repro-issue-id is required")

    script_src = os.path.abspath(test_input.repro_script)
    if not os.path.isfile(script_src):
        raise FileNotFoundError(f"Reproduction script not found: {script_src}")

    if test_input.repro_workdir:
        workdir = os.path.abspath(test_input.repro_workdir)
        os.makedirs(workdir, exist_ok=True)
        script_dst = os.path.join(workdir, os.path.basename(script_src))
        if os.path.abspath(script_dst) != script_src:
            shutil.copy2(script_src, script_dst)
        os.chmod(script_dst, 0o755)
    else:
        workdir = os.path.dirname(script_src)

    csv_dst = os.path.join(workdir, "test_config.csv")
    if test_input.repro_config_csv:
        csv_src = os.path.abspath(test_input.repro_config_csv)
        if not os.path.isfile(csv_src):
            raise FileNotFoundError(f"test_config.csv not found: {csv_src}")
        if os.path.abspath(csv_dst) != csv_src:
            shutil.copy2(csv_src, csv_dst)

    if not os.path.isfile(csv_dst):
        raise FileNotFoundError(
            "test_config.csv is required. Put it next to single_runner.sh or pass --repro-config-csv."
        )

    if test_input.repro_zip:
        zip_src = os.path.abspath(test_input.repro_zip)
        if not os.path.isfile(zip_src):
            raise FileNotFoundError(f"Reproduction zip not found: {zip_src}")

        data_dir = os.path.join(workdir, "data")
        os.makedirs(data_dir, exist_ok=True)
        zip_dst = os.path.join(data_dir, f"{_zip_stem(zip_src)}.zip")
        if os.path.abspath(zip_dst) != zip_src:
            shutil.copy2(zip_src, zip_dst)

    return workdir


def _normalize_row(row: dict[str, str]) -> dict[str, str]:
    """Strip whitespace/BOM from CSV keys and values."""
    clean: dict[str, str] = {}
    for key, value in row.items():
        if key is None:
            continue
        clean_key = key.strip().lstrip("\ufeff")
        clean[clean_key] = (value or "").strip()
    return clean


def _read_repro_row(test_input: TestInput, workdir: str) -> dict[str, str]:
    """Read the CSV row matching test_input.repro_issue_id.

    Supports both headered CSVs and the ReproFlake fixed-column format.
    """
    csv_path = os.path.join(workdir, "test_config.csv")
    columns = [
        "test_type",
        "issue_id",
        "zip",
        "module",
        "preceding_test",
        "flaky_test",
        "iterations",
        "config",
        "javav",
        "nondexSeed",
        "url",
    ]

    with open(csv_path, newline="", encoding="utf-8", errors="replace") as file:
        first_line = file.readline()
        file.seek(0)
        has_header = "issue_id" in first_line

        if has_header:
            reader = csv.DictReader(file)
            for row in reader:
                data = _normalize_row(row)
                if data.get("issue_id") == test_input.repro_issue_id:
                    return data
        else:
            reader = csv.reader(file)
            for row in reader:
                if not row:
                    continue
                padded = row + [""] * (len(columns) - len(row))
                data = dict(zip(columns, [cell.strip() for cell in padded[: len(columns)]]))
                if data.get("issue_id") == test_input.repro_issue_id:
                    return data

    raise ValueError(f"Issue id not found in test_config.csv: {test_input.repro_issue_id}")


def _helper_script_for_row(row: dict[str, str]) -> str:
    """Match single_runner.sh's helper-script selection logic."""
    test_type = row.get("test_type", "").strip()
    module = row.get("module", "").strip()
    javav = row.get("javav", "").strip()

    if test_type == "britle":
        return "flaky_analysis_tool_od_brittle.sh"
    if test_type == "od":
        return "flaky_analysis_tool_od_proto.sh" if module.startswith("hadoop") else "flaky_analysis_tool_od.sh"
    if test_type == "td":
        return "flaky_analysis_tool_td_proto.sh" if module.startswith("hadoop") else "flaky_analysis_tool_td.sh"
    if test_type == "id":
        if javav == "8":
            return "flaky_analysis_tool_id_8.sh"
        if javav == "17":
            return "flaky_analysis_tool_id_17.sh"
        return "flaky_analysis_tool_id_11.sh"
    if test_type == "raft":
        return "flaky_analysis_tool_raft.sh"
    if test_type == "nio":
        return "flaky_analysis_tool_nio.sh"

    return "flaky_analysis_tool_proto.sh" if module.startswith("hadoop") else "flaky_analysis_tool.sh"


def _helper_args_for_row(row: dict[str, str], iterations: int, code_version: str) -> list[str]:
    """Build helper-script args using the same convention as single_runner.sh."""
    row = _normalize_row(row)
    test_type = row.get("test_type", "")
    issue_id = row.get("issue_id", "")
    zip_name = row.get("zip", "")
    module = row.get("module", "")
    preceding_test = row.get("preceding_test", "")
    flaky_test = row.get("flaky_test", "")
    nondex_seed = row.get("nondexSeed", "")
    iter_s = str(iterations)

    if test_type in ("britle", "od"):
        # OD helpers require both the order/preceding-test input and the victim/flaky test.
        return [issue_id, zip_name, module, preceding_test, flaky_test, iter_s, code_version]
    if test_type == "td":
        return [issue_id, zip_name, module, flaky_test, iter_s, code_version]
    if test_type == "id":
        return [issue_id, zip_name, module, flaky_test, iter_s, code_version, nondex_seed]
    if test_type in ("raft", "nio"):
        return [issue_id, zip_name, module, flaky_test, iter_s, code_version]

    return [issue_id, zip_name, module, flaky_test, iter_s, code_version]


def _extract_artifact_for_validation(workdir: str, row: dict[str, str], patch_path: str) -> tuple[Path, Path | None]:
    """
    Extract the artifact, install the generated patch as Fixed.patch, and
    temporarily hide data/<zip>.zip so the helper does not overwrite Fixed.patch.
    """
    row = _normalize_row(row)
    issue_id = row["issue_id"]
    zip_name = row["zip"]
    base_dir = Path(workdir) / "data" / issue_id
    zip_path = Path(workdir) / "data" / f"{zip_name}.zip"

    if not zip_path.is_file():
        raise FileNotFoundError(
            f"Artifact zip not found for validation: {zip_path}. "
            "Pass --repro-zip or run reproduction once so the script can download it."
        )

    if base_dir.exists():
        _safe_rmtree(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zip_file:
        zip_file.extractall(base_dir)

    nested = base_dir / zip_name
    if nested.is_dir():
        for child in list(nested.iterdir()):
            shutil.move(str(child), str(base_dir / child.name))
        nested.rmdir()

    fixed_dir = base_dir / "Fixed"
    if fixed_dir.exists():
        _safe_rmtree(fixed_dir)

    #shutil.copy2(patch_path, base_dir / "Fixed.patch")

    fixed_patch_path = base_dir / "Fixed.patch"
    shutil.copy2(patch_path, fixed_patch_path)

    module = row.get("module", "").strip().strip("/")
    if module and module != ".":
        text = fixed_patch_path.read_text(encoding="utf-8", errors="replace")
        text = text.replace("--- a/src/", f"--- a/{module}/src/")
        text = text.replace("+++ b/src/", f"+++ b/{module}/src/")
        fixed_patch_path.write_text(text, encoding="utf-8")
        logger.info("Added module prefix to Fixed.patch: %s", module)
    
    hidden_zip = zip_path.with_suffix(zip_path.suffix + ".flakyguard_hold")
    if hidden_zip.exists():
        hidden_zip.unlink()
    shutil.move(str(zip_path), str(hidden_zip))
    return base_dir, hidden_zip


def _restore_hidden_zip(hidden_zip: Path | None) -> None:
    if hidden_zip and hidden_zip.exists():
        original = hidden_zip.with_suffix("")
        if original.exists():
            original.unlink()
        shutil.move(str(hidden_zip), str(original))


def _collect_repro_logs(workdir: str, base_output: str, issue_id: str | None = None) -> str:
    """Add relevant logs/summaries produced by the reproduction/validation scripts.

    If issue_id is provided, only collect logs from data/<issue_id>/.
    This avoids parsing stale logs from a previous issue.
    """
    parts = [base_output]
    workdir_path = Path(workdir)

    if issue_id:
        search_root = workdir_path / "data" / issue_id
        if not search_root.exists():
            parts.append(f"\n\n===== No issue log directory found: {search_root} =====\n")
            return "".join(parts)
    else:
        search_root = workdir_path

    patterns = [
        "**/summary.txt",
        "**/rounds-test-results.csv",
        "**/testlog/**/*.log",
        "**/surefire-reports/*.txt",
        "**/surefire-reports/*.xml",
        "**/flaky-result/**/*",
        "**/result/**/*",
    ]

    seen: set[Path] = set()
    for pattern in patterns:
        for path in search_root.glob(pattern):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            parts.append(f"\n\n===== {path} =====\n{text[:6000]}")

    return "".join(parts)

# def _has_failure_markers(output: str, test_type: str = "") -> bool:
#     """Return True when validation output contains an actual failure."""
#     test_type = (test_type or "").strip().lower()

#     common_patterns = [
#         r"<<< FAILURE!",
#         r"AssertionFailedError",
#         r"AssertionError",
#         r"There are test failures",
#         r"BUILD FAILURE",
#         r"Failed to apply patch",
#         r"Failures:\s*[1-9]",
#         r"Errors:\s*[1-9]",
#     ]

#     if any(re.search(pattern, output, re.IGNORECASE) for pattern in common_patterns):
#         return True

#     if test_type == "nio":
#         # NIO CSV rows can contain `,failure,` as a configuration label.
#         # Only trust the actual result stored inside the JSON field.
#         nio_failure_pattern = (
#             r'(?:""|")result(?:""|")\s*:\s*'
#             r'(?:""|")(?:FAIL|FAILURE|ERROR)(?:""|")'
#         )
#         return re.search(nio_failure_pattern, output, re.IGNORECASE) is not None

#     # Existing behavior for non-NIO result formats.
#     return re.search(r",\s*failure\s*,", output, re.IGNORECASE) is not None

def _has_failure_markers(output: str, test_type: str = "") -> bool:
    test_type = (test_type or "").strip().lower()

    if test_type == "nio":
        # For NIO, `failure` and `Failures: N` describe the selected
        # configuration/rows, not necessarily the nested test results.
        actual_failure_patterns = [
            r'""result"":""(?:FAIL|FAILURE|ERROR)""',
            r'"result"\s*:\s*"(?:FAIL|FAILURE|ERROR)"',
            r"Failed to apply patch",
            r"BUILD FAILURE",
            r"COMPILATION ERROR",
            r"<<< FAILURE!",
            r"AssertionError",
            r"AssertionFailedError",
        ]

        return any(
            re.search(pattern, output, re.IGNORECASE)
            for pattern in actual_failure_patterns
        )

    patterns = [
        r"<<< FAILURE!",
        r"AssertionFailedError",
        r"AssertionError",
        r"There are test failures",
        r"BUILD FAILURE",
        r"Failed to apply patch",
        r"Failures:\s*[1-9]",
        r"Errors:\s*[1-9]",
        r",\s*failure\s*,",
    ]

    return any(
        re.search(pattern, output, re.IGNORECASE)
        for pattern in patterns
    )


def _failure_marker_report(output: str, test_type: str = "") -> str:
    test_type = (test_type or "").strip().lower()

    patterns = [
        r"<<< FAILURE!",
        r"AssertionFailedError",
        r"AssertionError",
        r"BUILD FAILURE",
        r"Failures:\s*[1-9]",
        r"Errors:\s*[1-9]",
        r'""result"":""(?:FAIL|FAILURE|ERROR)""',
        r'"result"\s*:\s*"(?:FAIL|FAILURE|ERROR)"',
    ]

    if test_type != "nio":
        patterns.append(r",\s*failure\s*,")

    # matches = []
    # for line_number, line in enumerate(output.splitlines(), start=1):
    #     if any(re.search(pattern, line, re.IGNORECASE) for pattern in patterns):
    #         matches.append(f"{line_number:5}: {line}")

    # return "\n".join(matches) if matches else "<no marker lines found>"

    lines = output.splitlines()
    matches = []
    seen = set()
    context = 3  # lines before and after

    for line_number, line in enumerate(lines, start=1):
        if any(re.search(pattern, line, re.IGNORECASE) for pattern in patterns):
            start = max(0, line_number - 1 - context)
            end = min(len(lines), line_number + context)
            for i in range(start, end):
                if i not in seen:
                    seen.add(i)
                    prefix = ">>>" if i == line_number - 1 else "   "
                    matches.append(f"{prefix} {i+1:5}: {lines[i]}")
            matches.append("---")

    return "\n".join(matches) if matches else "<no marker lines found>"


# def _has_failure_markers(output: str) -> bool:
#     """Return True if script/test output contains clear failure markers."""
#     patterns = [
#         r"<<< FAILURE!",
#         r"AssertionFailedError",
#         r"AssertionError",
#         r"There are test failures",
#         r"BUILD FAILURE",
#         r"Failed to apply patch",
#         r"Failures:\s*[1-9]",
#         r"Errors:\s*[1-9]",
#         r",\s*failure\s*,",
#     ]
#     return any(re.search(pattern, output, re.IGNORECASE) for pattern in patterns)


# ── Validation debug helpers ────────────────────────────────────────────────

def _sha12(text: str | None) -> str:
    if text is None:
        return "missing"
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:12]


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _patch_changed_files(patch_path: str) -> list[str]:
    """Return file paths touched by a unified/git patch."""
    changed: list[str] = []
    text = _read_text(Path(patch_path))
    if text is None:
        return changed

    for line in text.splitlines():
        candidate = ""
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4:
                candidate = parts[3]
        elif line.startswith("+++ "):
            candidate = line[4:].split("\t", 1)[0].strip()
        else:
            continue

        if not candidate or candidate == "/dev/null":
            continue
        if candidate.startswith("a/") or candidate.startswith("b/"):
            candidate = candidate[2:]
        if candidate not in changed:
            changed.append(candidate)
    return changed


def _numbered_snippet(text: str, center_line: int, radius: int = 18) -> str:
    lines = text.splitlines()
    start = max(1, center_line - radius)
    end = min(len(lines), center_line + radius)
    return "\n".join(f"{i:4d}: {lines[i - 1]}" for i in range(start, end + 1))


def _method_snippet(text: str, method_name: str) -> str:
    if method_name and method_name in text:
        line_no = text[: text.index(method_name)].count("\n") + 1
        return _numbered_snippet(text, line_no, radius=25)
    return text[:2500]


# def _preview_apply_fixed_patch(base_dir: Path, row: dict[str, str], patch_path: str) -> None:
#     """
#     Keep a visible debug copy showing what Fixed would look like after patching.

#     The ReproFlake helper creates data/<issue>/Fixed, validates it, then deletes it.
#     This preview copy uses the same patch command on a separate directory so we can
#     inspect the patched source after validation.
#     """
#     row = _normalize_row(row)
#     flaky_dir = base_dir / "Flaky"
#     preview_dir = base_dir / "Fixed_debug_preview"
#     patch_file = Path(patch_path)
#     changed_files = _patch_changed_files(patch_path)
#     flaky_test = row.get("flaky_test", "")
#     method_name = flaky_test.split("#")[-1].split(".")[-1] if flaky_test else ""

#     logger.info("[fixed-preview] Fixed.patch: %s", patch_file)
#     logger.info("[fixed-preview] Fixed.patch exists=%s size=%s bytes", patch_file.exists(), patch_file.stat().st_size if patch_file.exists() else "missing")
#     logger.info("[fixed-preview] patch target files: %s", changed_files if changed_files else "<none parsed>")

#     if not flaky_dir.is_dir():
#         logger.info("[fixed-preview] cannot preview patch: missing Flaky dir: %s", flaky_dir)
#         return
#     if preview_dir.exists():
#         _safe_rmtree(preview_dir)
#     shutil.copytree(flaky_dir, preview_dir)

#     cmd = f"patch -p1 -d {shlex.quote(str(preview_dir))} < {shlex.quote(str(patch_file))}"
#     result = subprocess.run(cmd, shell=True, cwd=str(base_dir.parent.parent), capture_output=True, text=True)
#     patch_output = (result.stdout or "") + (result.stderr or "")

#     logger.info("[fixed-preview] preview patch command: %s", cmd)
#     logger.info("[fixed-preview] preview patch exit code: %s", result.returncode)
#     logger.info("[fixed-preview] preview patch output:\n%s", patch_output[:3000] if patch_output else "<no output>")

#     for rel_path in changed_files[:5]:
#         before_path = flaky_dir / rel_path
#         after_path = preview_dir / rel_path
#         before_text = _read_text(before_path)
#         after_text = _read_text(after_path)
#         logger.info("[fixed-preview] checking target: %s", rel_path)
#         logger.info("[fixed-preview] original path exists=%s sha=%s", before_path.is_file(), _sha12(before_text))
#         logger.info("[fixed-preview] patched  path exists=%s sha=%s", after_path.is_file(), _sha12(after_text))
#         logger.info("[fixed-preview] patch changed this file: %s", "YES" if before_text != after_text else "NO")

#         if before_text is not None and after_text is not None:
#             diff_lines = list(difflib.unified_diff(
#                 before_text.splitlines(),
#                 after_text.splitlines(),
#                 fromfile=f"Flaky/{rel_path}",
#                 tofile=f"Fixed_debug_preview/{rel_path}",
#                 lineterm="",
#                 n=4,
#             ))
#             logger.info("[fixed-preview] actual source diff after applying patch:\n%s", "\n".join(diff_lines[:180]) if diff_lines else "<no diff>")
#             if method_name:
#                 logger.info(
#                     "[fixed-preview] patched source around %s:\n%s",
#                     method_name,
#                     _method_snippet(after_text, method_name),
#                 )

#     logger.info("[fixed-preview] kept patched preview directory: %s", preview_dir)


def _collect_logs_from_root(root: Path, base_output: str) -> str:
    """Collect logs only from a specific result root, e.g. data/<issue>/result/Fixed."""
    parts = [base_output]
    if not root.exists():
        parts.append(f"\n\n===== result root does not exist: {root} =====\n")
        return "".join(parts)

    patterns = [
        "**/summary.txt",
        "**/rounds-test-results.csv",
        "**/testlog/**/*.log",
        "**/surefire-reports/*.txt",
        "**/surefire-reports/*.xml",
        "**/*.log",
        "**/*.txt",
        "**/*.xml",
        "**/*.csv",
    ]
    seen: set[Path] = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            text = _read_text(path)
            if text is None:
                continue
            parts.append(f"\n\n===== {path} =====\n{text[:8000]}")
    return "".join(parts)


# def _failure_marker_report(output: str, max_hits: int = 30) -> str:
#     """Return exact lines that caused the validation to be considered failing."""
#     marker_re = re.compile(
#         r"<<< FAILURE!|AssertionFailedError|AssertionError|There are test failures|BUILD FAILURE|"
#         r"Failed to apply patch|Failures:\s*[1-9]|Errors:\s*[1-9]|,\s*failure\s*,",
#         re.IGNORECASE,
#     )
#     lines = output.splitlines()
#     hits: list[str] = []
#     for i, line in enumerate(lines, start=1):
#         if marker_re.search(line):
#             start = max(1, i - 2)
#             end = min(len(lines), i + 2)
#             context = "\n".join(f"{j:5d}: {lines[j - 1]}" for j in range(start, end + 1))
#             hits.append(context)
#             if len(hits) >= max_hits:
#                 break
#     return "\n\n---\n".join(hits) if hits else "<no marker lines found>"

def _failure_marker_report(output: str, test_type: str = "") -> str:
    test_type = (test_type or "").strip().lower()

    patterns = [
        r"<<< FAILURE!",
        r"AssertionFailedError",
        r"AssertionError",
        r"BUILD FAILURE",
        r"Failures:\s*[1-9]",
        r"Errors:\s*[1-9]",
        r'""result"":""(?:FAIL|FAILURE|ERROR)""',
        r'"result"\s*:\s*"(?:FAIL|FAILURE|ERROR)"',
    ]

    if test_type != "nio":
        patterns.append(r",\s*failure\s*,")

    matches = []
    for line_number, line in enumerate(output.splitlines(), start=1):
        if any(re.search(pattern, line, re.IGNORECASE) for pattern in patterns):
            matches.append(f"{line_number:5}: {line}")

    return "\n".join(matches) if matches else "<no marker lines found>"

def _last_count(output: str, label: str) -> int | None:
    matches = re.findall(
        rf"^\s*{re.escape(label)}:\s*(\d+)\s*$",
        output,
        re.MULTILINE,
    )
    return int(matches[-1]) if matches else None


def _validation_success_summary(output: str) -> str:
    summary = []

    if "BUILD SUCCESS" in output:
        summary.append("Build: PASS")

    # Automatically recognize ID/NonDex validation.
    if "All tests pass without NonDex shuffling" in output:
        summary.append("Baseline runs without NonDex shuffling: PASS")

        matches = re.findall(
            r"^\s*Passes:\s*(\d+)\s*$",
            output,
            re.MULTILINE,
        )
        if matches:
            summary.append(f"NonDex shuffled runs passed: {matches[-1]}")

        return "\n".join(summary)

    return "\n".join(summary) or "No success summary found."
# def _validation_success_summary(
#     output: str,
#     test_type: str = "",
# ) -> str:
#     """Return a compact validation summary without duplicate Maven lines."""
#     test_type = (test_type or "").strip().lower()
#     summary: list[str] = []

#     if "BUILD SUCCESS" in output:
#         summary.append("Build: PASS")
#     else:
#         summary.append("Build: no success marker found")

#     if test_type == "id":
#         if "All tests pass without NonDex shuffling" in output:
#             summary.append("Baseline run without NonDex shuffling: PASS")

#         passes = _last_count(output, "Passes")
#         failures = _last_count(output, "Failures")
#         errors = _last_count(output, "Errors")

#         if passes is not None:
#             summary.append(
#                 "NonDex shuffled runs: "
#                 f"{passes} passed, "
#                 f"{failures or 0} failed, "
#                 f"{errors or 0} errors"
#             )

#         return "\n".join(summary)

    # For non-ID tests, collapse repeated Surefire summaries.
    matches = re.findall(
        r"Tests run:\s*(\d+),\s*"
        r"Failures:\s*(\d+),\s*"
        r"Errors:\s*(\d+),\s*"
        r"Skipped:\s*(\d+)",
        output,
    )

    unique_matches = list(dict.fromkeys(matches))

    for tests, failures, errors, skipped in unique_matches:
        summary.append(
            f"Surefire: {tests} tests, "
            f"{failures} failures, "
            f"{errors} errors, "
            f"{skipped} skipped"
        )

    passes = _last_count(output, "Passes")
    if passes is not None:
        summary.append(f"Passing validation rounds: {passes}")

    return "\n".join(summary) if summary else "No success summary found."

def _write_validation_debug_log(workdir: str, issue_id: str, text: str, label: str = "Fixed") -> Path:
    debug_dir = Path(workdir) / "flakyguard-validation-debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    path = debug_dir / f"{issue_id}_{label}_validation_output.log"
    path.write_text(text, encoding="utf-8", errors="replace")
    return path


def _log_fixed_result_summary(base_dir: Path, output: str, returncode: int) -> None:
    """Print only the validation results for CODE_VERSION=Fixed."""
    #fixed_result_dir = base_dir / "result" / "Fixed"

    fixed_result_dir = base_dir / "result" / "Fixed"
    logger.info("[fixed-result] Fixed result dir: %s", fixed_result_dir)
    logger.info("[fixed-result] Fixed result dir exists: %s", fixed_result_dir.exists())
    # fixed_result_dir = base_dir / "Fixed"
    # logger.info("[fixed-result] helper exit code: %s", returncode)
    # logger.info("[fixed-result] Fixed result dir: %s", fixed_result_dir)
    # logger.info("[fixed-result] Fixed result dir exists: %s", fixed_result_dir.exists())

    if fixed_result_dir.exists():
        files = [p for p in fixed_result_dir.rglob("*") if p.is_file()]
        #logger.info("[fixed-result] result files under result/Fixed (%d):\n%s", len(files), "\n".join(str(p.relative_to(base_dir)) for p in files[:80]) if files else "<none>")
        fixed_logs = _collect_logs_from_root(fixed_result_dir, output)
    else:
        # If the helper failed before copying result/Fixed, stdout/stderr is still useful.
        fixed_logs = output

    logger.info("[fixed-result] important PASS/FAIL lines for Fixed only:\n%s", _failure_marker_report(fixed_logs))
    success_lines = []
    for line in fixed_logs.splitlines():
        if re.search(r"BUILD SUCCESS|Tests run:|Passed|PASS|success", line, re.IGNORECASE):
            success_lines.append(line)
        if len(success_lines) >= 30:
            break
    #logger.info("[fixed-result] success/test summary lines for Fixed only:\n%s", "\n".join(success_lines) if success_lines else "<none found>")
    #test_type = row.get("test_type", "").strip().lower()

    # logger.info(
    #     "[fixed-result] validation summary:\n%s",
    #     _validation_success_summary(full_output, test_type),
    # )
    logger.info(
        "[fixed-result] validation summary:\n%s",
        _validation_success_summary(fixed_logs),
    )
def _fixed_validation_output(base_dir: Path, helper_output: str) -> str:
    """Return stdout/stderr plus logs copied for the Fixed validation run only."""
    return _collect_logs_from_root(base_dir / "result" / "Fixed", helper_output)


# ── Public API ───────────────────────────────────────────────────────────────

def reproduce_failure(test_input: TestInput) -> FlakyInfo | None:
    """
    Reproduce a flaky failure.

    Script mode runs single_runner.sh <issue_id>. Command mode is retained as
    a fallback for non-ReproFlake use.
    """
    if test_input.repro_script:
        try:
            workdir = _prepare_script_workdir(test_input)
        except Exception as exc:
            logger.error("Script reproduction setup failed: %s", exc)
            return None

        # Fail early if the issue id is not in this workdir's test_config.csv.
        try:
            row = _read_repro_row(test_input, workdir)
        except Exception as exc:
            logger.error("Script reproduction setup failed: %s", exc)
            return None

        # TD artifacts only manifest the flake in the sleep-injected
        # FlakyCodeChange copy; every other type reproduces on plain Flaky.
        test_type = (row.get("test_type") or "").strip().lower()
        repro_code_version = "FlakyCodeChange" if test_type == "td" else "Flaky"

        script_name = os.path.basename(test_input.repro_script)
        cmd = f"bash ./{script_name} {test_input.repro_issue_id}"

        # Remove stale logs/results for this issue before rerunning.
        issue_dir = Path(workdir) / "data" / test_input.repro_issue_id
        if issue_dir.exists():
            _safe_rmtree(issue_dir)

        repro_iterations = _script_repro_iterations()
        env = os.environ.copy()
        env["FLAKYGUARD_CODE_VERSION"] = repro_code_version
        env["FLAKYGUARD_REPRO_ITERATIONS"] = str(repro_iterations)

        logger.info("Reproducing flaky test with script: %s (cwd=%s)", cmd, workdir)
        logger.info(
            "For FlakyGuard reproduction only, forcing ReproFlake CODE_VERSION=%s and iterations=%d.",
            repro_code_version,
            repro_iterations,
        )

        try:
            returncode, output = _run_script_streaming(
                cmd,
                cwd=workdir,
                timeout=test_input.repro_timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            output = _timeout_text(exc.stdout) + "\nTIMEOUT"
            full_output = _collect_repro_logs(workdir, output, test_input.repro_issue_id)
            return FlakyInfo(error="TIMEOUT", error_trace=full_output)

        full_output = _collect_repro_logs(workdir, output, test_input.repro_issue_id)

        # The statistics generator's Summary block is authoritative. Without
        # this gate, marker-based detection reports success even on all-pass
        # runs: every Maven log contains "Tests run:" and passing Hadoop test
        # logs are full of benign ERROR/exception lines.
        summary = _parse_script_summary(full_output)
        if summary is not None:
            passes, failures, errors = summary
            logger.info(
                "Reproduction summary: Passes=%d Failures=%d Errors=%d",
                passes, failures, errors,
            )
            if failures + errors == 0:
                logger.warning(
                    "All reproduction iterations passed - flaky failure NOT reproduced."
                )
                return None

        info = _extract_flaky_info(full_output, test_input)
        if info:
            logger.info("Failure reproduced via script.")
            return info

        if re.search(r"Failures:\s*[1-9]", full_output) or re.search(r",failure,", full_output):
            logger.info("Failure reproduced via script summary/results.")
            return FlakyInfo(error="Failure reproduced by script", error_trace=full_output[:4000])

        if returncode != 0:
            logger.warning("Reproduction script exited with %d but no test failure was parsed.", returncode)
            return FlakyInfo(
                error=f"Reproduction script failed with exit code {returncode}",
                error_trace=full_output[:4000],
            )

        logger.info("DEBUG script returncode: %s", returncode)
        logger.info("DEBUG output tail:\n%s", output[-4000:])

        logger.warning("Script completed but no flaky failure was found in output/logs.")
        return None

    cmd = _build_cmd(test_input)
    logger.info("Reproducing flaky test: %s (up to %d runs)", cmd, test_input.repro_runs)

    for i in range(test_input.repro_runs):
        logger.info("Reproduction run %d/%d", i + 1, test_input.repro_runs)
        passed, output = _run_once(cmd, cwd=test_input.repo_root)
        if not passed:
            info = _extract_flaky_info(output, test_input)
            if info:
                logger.info("Failure reproduced on run %d/%d", i + 1, test_input.repro_runs)
                return info

    logger.warning("No failure reproduced after %d runs.", test_input.repro_runs)
    return None


def validate_fix(test_input: TestInput, runs: int = 10, patch_path: str | None = None) -> bool:
    """
    Validate a generated fix.

    In script mode, apply the generated patch to the artifact's Flaky copy and
    run the matching helper script with CODE_VERSION=Fixed. The iteration count
    defaults to 10 through TestInput.script_validation_iterations.

    In command fallback mode, run TestInput.run_cmd `runs` times.
    """
    if test_input.repro_script and patch_path:
        hidden_zip: Path | None = None
        try:
            workdir = _prepare_script_workdir(test_input)
            row = _read_repro_row(test_input, workdir)
            helper = _helper_script_for_row(row)
            helper_path = Path(workdir) / helper
            if not helper_path.is_file():
                logger.error("Validation helper script not found: %s", helper_path)
                return False

            base_dir, hidden_zip = _extract_artifact_for_validation(workdir, row, patch_path)

            old_result_dir = base_dir / "result"
            if old_result_dir.exists():
                _safe_rmtree(old_result_dir)
                
            iterations = test_input.script_validation_iterations
            args = _helper_args_for_row(row, iterations=iterations, code_version="Fixed")

            # Force bash + helper path. This prevents Python from accidentally trying
            # to execute an OD order .txt file or another argument as the program.
            cmd = ["bash", str(helper_path)] + args

            logger.info(
                "Validating fix with ReproFlake helper: %s (cwd=%s)",
                shlex.join(cmd),
                workdir,
            )

            # Debug preview: apply the same Fixed.patch to a kept copy so we can
            # inspect the exact patched source even though the helper deletes Fixed/.
           # _preview_apply_fixed_patch(base_dir, row, str(base_dir / "Fixed.patch"))

            env = os.environ.copy()
            if (row.get("test_type") or "").strip().lower() == "td":
                # TD flakes only manifest on the sleep-injected copy; validate
                # the candidate patch on FlakyCodeChange, not plain Flaky.
                env["FLAKYGUARD_FIXED_BASE"] = "FlakyCodeChange"
                logger.info(
                    "TD validation: building Fixed from FlakyCodeChange + candidate patch."
                )

            result = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=test_input.repro_timeout,
                env=env,
            )
            output = result.stdout + result.stderr

            if "Failed to apply patch" in output:
                logger.info(
                    "Candidate patch did not apply cleanly to the validation base - rejecting."
                )
                return False

            # Print only the CODE_VERSION=Fixed result summary. The helper deletes
            # data/<issue>/Fixed after running, but keeps logs under result/Fixed/.
            _log_fixed_result_summary(base_dir, output, result.returncode)
            fixed_output = _fixed_validation_output(base_dir, output)
            debug_log = _write_validation_debug_log(
                workdir,
                test_input.repro_issue_id or row.get("issue_id", "unknown_issue"),
                fixed_output,
                label="Fixed",
            )
            logger.info("[fixed-result] full Fixed validation output saved to: %s", debug_log)

            if result.returncode != 0:
                logger.info("Script validation failed with exit code %d", result.returncode)
                logger.info("Validation stdout/stderr preview:\n%s", output[:3000])
                return False

            if _has_failure_markers(fixed_output):
                logger.info("Script validation found failure markers in result/Fixed only.")
                logger.info("Failure marker details for Fixed only:\n%s", _failure_marker_report(fixed_output))
                return False

            logger.info(
                "Fix validated by ReproFlake helper on patched Fixed artifact (%d iterations).",
                iterations,
            )
            return True

        except Exception as exc:
            logger.error("Script-based validation failed: %s", exc)
            return False
        finally:
            _restore_hidden_zip(hidden_zip)

    cmd = _build_cmd(test_input)
    for i in range(runs):
        passed, output = _run_once(cmd, cwd=test_input.repo_root)
        if not passed:
            logger.info("Validation failed on run %d/%d", i + 1, runs)
            return False
    logger.info("Fix validated: test passed %d/%d runs.", runs, runs)
    return True
