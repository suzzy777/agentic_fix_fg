from __future__ import annotations

import csv
import logging
import os
import re
import shlex
import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from models import TestInput

logger = logging.getLogger(__name__)


@dataclass
class CoverageData:
    """Covered source files and their executed source-line numbers."""

    files: list[str]
    lines_by_file: dict[str, set[int]]

    @classmethod
    def empty(cls) -> "CoverageData":
        return cls(files=[], lines_by_file={})


_COVERAGE_CACHE: dict[tuple[str, str, str, str], CoverageData] = {}



def _as_text(value) -> str:
    """Return subprocess output as text, even when TimeoutExpired stores bytes."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _run_cmd(cmd: str | list[str], cwd: str, timeout: int, shell: bool = True) -> tuple[bool, str]:
    """Run a command and return (success, combined output)."""
    try:
        result = subprocess.run(
            cmd,
            shell=shell,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired as exc:
        output = _as_text(exc.stdout) + _as_text(exc.stderr) + "\nTIMEOUT"
        return False, output


def _java_test_class_from_file(test_input: TestInput) -> str:
    """
    Infer a Java fully-qualified class name from the test file path.

    Example:
      src/test/java/com/example/MyTest.java -> com.example.MyTest
    """
    test_file = test_input.test_file.replace("\\", "/")

    for marker in ("src/test/java/", "src/main/java/"):
        if marker in test_file:
            rel = test_file.split(marker, 1)[1]
            return rel[:-5].replace("/", ".") if rel.endswith(".java") else rel.replace("/", ".")

    # Fallback: read package declaration from the file.
    test_file_abs = test_input.test_file
    if not os.path.isabs(test_file_abs):
        test_file_abs = os.path.join(test_input.repo_root, test_input.test_file)

    try:
        source = Path(test_file_abs).read_text(encoding="utf-8", errors="replace")
    except OSError:
        class_name = Path(test_file).stem
        return class_name

    package_match = re.search(r"^\s*package\s+([\w.]+)\s*;", source, re.MULTILINE)
    class_name = Path(test_file).stem
    if package_match:
        return f"{package_match.group(1)}.{class_name}"
    return class_name


def _build_coverage_cmd(test_input: TestInput) -> str:
    """
    Build the JaCoCo command.

    A user-provided coverage command can use placeholders:
      {test_class}, {test_func}, {test_case}, {test_file}, {test_dir}
    """
    test_dir = os.path.dirname(test_input.test_file) or "."
    test_class = _java_test_class_from_file(test_input)

    if test_input.coverage_cmd:
        return test_input.coverage_cmd.format(
            test_class=test_class,
            test_func=test_input.test_func,
            test_case=test_input.test_case,
            test_file=test_input.test_file,
            test_dir=test_dir,
        )

    # Default Maven/JaCoCo command. Users can override this if their project
    # needs extra flags or a different module path.
    return (
        "mvn -q "
        "-DargLine=\"\" "
        "org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent "
        "test "
        "org.jacoco:jacoco-maven-plugin:0.8.12:report "
        f"-Dtest={test_class}#{test_input.test_func} "
        "-Drat.skip=true"
    )


def _resolve_report_path(test_input: TestInput) -> str:
    """Return absolute JaCoCo XML report path."""
    report = test_input.coverage_report or "target/site/jacoco/jacoco.xml"
    if os.path.isabs(report):
        return report
    return os.path.join(test_input.repo_root, report)


# ── ReproFlake Docker coverage helpers ───────────────────────────────────────

def _script_workdir(test_input: TestInput) -> Path:
    """Return the directory containing the ReproFlake helper scripts."""
    if test_input.repro_workdir:
        return Path(test_input.repro_workdir).resolve()
    if test_input.repro_script:
        return Path(test_input.repro_script).resolve().parent
    return Path(test_input.repo_root).resolve()


def _read_reproflake_row(test_input: TestInput) -> dict[str, str] | None:
    """Read test_config.csv row for the current issue id."""
    if not test_input.repro_issue_id:
        return None

    csv_path = Path(test_input.repro_config_csv or "")
    if not csv_path.is_file():
        csv_path = _script_workdir(test_input) / "test_config.csv"
    if not csv_path.is_file():
        return None

    columns = [
        "test_type", "issue_id", "zip", "module", "preceding_test",
        "flaky_test", "iterations", "config", "javav", "nondexSeed", "url",
    ]

    with csv_path.open(newline="", encoding="utf-8", errors="replace") as file:
        sample = file.readline()
        file.seek(0)
        if "issue_id" in sample:
            reader = csv.DictReader(file)
            for row in reader:
                clean = {str(k).strip().lstrip("\ufeff"): (v or "").strip() for k, v in row.items() if k}
                if clean.get("issue_id") == test_input.repro_issue_id:
                    return clean
        else:
            reader = csv.reader(file)
            for raw in reader:
                if not raw:
                    continue
                row = dict(zip(columns, [cell.strip() for cell in raw + [""] * (len(columns) - len(raw))]))
                if row.get("issue_id") == test_input.repro_issue_id:
                    return row
    return None


def _docker_chown(path: Path) -> None:
    """Best-effort fix for Docker-created root-owned files on bind mounts."""
    if not path.exists():
        return

    parent = path.resolve().parent
    name = path.resolve().name
    uid = os.getuid()
    gid = os.getgid()
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{parent}:/host",
        "alpine", "sh", "-lc",
        f"chown -R {uid}:{gid} /host/{shlex.quote(name)} 2>/dev/null || true; "
        f"chmod -R u+rwX /host/{shlex.quote(name)} 2>/dev/null || true",
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=False)


def _safe_rmtree(path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except PermissionError:
        # Docker-created files can be root-owned. Fix ownership through Docker, then retry.
        _docker_chown(path)
        shutil.rmtree(path)


def _prepare_reproflake_coverage_source(test_input: TestInput, row: dict[str, str]) -> tuple[Path, Path]:
    """
    Extract the ReproFlake artifact to a stable coverage-only directory.

    Returns (source_root, m2_dir), where source_root is the Flaky source tree
    and m2_dir is the prepared Maven cache from the artifact.
    """
    workdir = _script_workdir(test_input)
    data_dir = workdir / "data"
    zip_name = row.get("zip", "").strip()
    issue_id = row.get("issue_id", test_input.repro_issue_id).strip()
    zip_path = data_dir / f"{zip_name}.zip"

    if not zip_path.is_file() and test_input.repro_zip:
        src_zip = Path(test_input.repro_zip).resolve()
        if src_zip.is_file():
            data_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_zip, zip_path)

    if not zip_path.is_file():
        raise FileNotFoundError(f"ReproFlake artifact zip not found for Docker coverage: {zip_path}")

    coverage_dir = data_dir / f"{issue_id}_coverage_ctx"
    _safe_rmtree(coverage_dir)
    coverage_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zip_file:
        zip_file.extractall(coverage_dir)

    nested = coverage_dir / zip_name
    if nested.is_dir():
        for child in list(nested.iterdir()):
            shutil.move(str(child), str(coverage_dir / child.name))
        nested.rmdir()

    source_root = coverage_dir / "Flaky"
    m2_dir = coverage_dir / "Flakym2" / ".m2"
    if not source_root.is_dir():
        raise FileNotFoundError(f"Flaky source directory not found in artifact: {source_root}")
    if not m2_dir.is_dir():
        raise FileNotFoundError(f"Prepared Maven cache not found in artifact: {m2_dir}")
    return source_root, m2_dir


def _module_report_path(source_root: Path, module: str) -> Path:
    module = (module or "").strip().strip("/")
    if module and module != ".":
        return source_root / module / "target" / "site" / "jacoco" / "jacoco.xml"
    return source_root / "target" / "site" / "jacoco" / "jacoco.xml"

def _find_jacoco_reports(source_root: Path, module: str) -> list[Path]:
    reports: list[Path] = []

    for pattern in [
        "flaky-result/coverage/*jacoco*.xml",
        "**/target/site/jacoco/jacoco.xml",
        "**/*jacoco*.xml",
    ]:
        for path in source_root.glob(pattern):
            if path.is_file() and path not in reports:
                reports.append(path)

    return reports

def _map_artifact_files_to_repo(files: list[str], artifact_root: Path, repo_root: Path, module: str) -> list[str]:
    """Map covered files from the extracted artifact back to --repo paths."""
    mapped: list[str] = []
    seen: set[str] = set()
    module = (module or "").strip().strip("/")

    for file in files:
        try:
            rel = Path(file).resolve().relative_to(artifact_root.resolve())
        except ValueError:
            continue

        candidates = [repo_root / rel]
        if module and module != ".":
            rel_s = rel.as_posix()
            prefix = module + "/"
            if rel_s.startswith(prefix):
                candidates.append(repo_root / rel_s[len(prefix):])
            else:
                candidates.append(repo_root / module / rel)

        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate.is_file():
                s = str(candidate)
                if s not in seen:
                    seen.add(s)
                    mapped.append(s)
                break

    return mapped



def _map_artifact_coverage_to_repo(
    coverage: CoverageData,
    artifact_root: Path,
    repo_root: Path,
    module: str,
) -> CoverageData:
    mapped_files = _map_artifact_files_to_repo(
        coverage.files,
        artifact_root=artifact_root,
        repo_root=repo_root,
        module=module,
    )

    mapped_lines: dict[str, set[int]] = {}
    artifact_root_resolved = artifact_root.resolve()
    module = (module or "").strip().strip("/")

    for artifact_file in coverage.files:
        source_path = Path(artifact_file).resolve()
        try:
            rel = source_path.relative_to(artifact_root_resolved)
        except ValueError:
            continue

        candidates = [repo_root / rel]
        if module and module != ".":
            rel_s = rel.as_posix()
            prefix = module + "/"
            if rel_s.startswith(prefix):
                candidates.append(repo_root / rel_s[len(prefix):])
            else:
                candidates.append(repo_root / module / rel)

        for candidate in candidates:
            candidate = candidate.resolve()
            if candidate.is_file():
                mapped_lines.setdefault(str(candidate), set()).update(
                    coverage.lines_by_file.get(str(source_path), set())
                )
                break

    return CoverageData(files=mapped_files, lines_by_file=mapped_lines)

def _reproflake_issue_dir(test_input: TestInput, row: dict[str, str]) -> Path:
    """Return the data/<issue_id> directory used by the ReproFlake helper."""
    issue_id = row.get("issue_id", test_input.repro_issue_id or "").strip()
    return _script_workdir(test_input) / "data" / issue_id


def _find_reproflake_coverage_reports(issue_dir: Path) -> list[Path]:
    """Find JaCoCo XML reports already produced by the ReproFlake helper."""
    if not issue_dir.is_dir():
        return []

    reports: list[Path] = []
    patterns = [
        "result/Flaky/**/*jacoco*.xml",
        "result/Flaky/**/coverage/*.xml",
        "result/FlakyCodeChange/**/*jacoco*.xml",
        "result/FlakyCodeChange/**/coverage/*.xml",
        "Flaky/flaky-result/coverage/*jacoco*.xml",
        "FlakyCodeChange/flaky-result/coverage/*jacoco*.xml",
        "**/flaky-result/coverage/*jacoco*.xml",
        "**/*jacoco*.xml",
    ]
    for pattern in patterns:
        for path in issue_dir.glob(pattern):
            if path.is_file() and path not in reports:
                reports.append(path)
    return reports


def _helper_script_and_args(row: dict[str, str], iterations: int = 1, code_version: str = "Flaky") -> tuple[str, list[str]]:
    test_type = row.get("test_type", "").strip()
    issue_id = row.get("issue_id", "").strip()
    zip_name = row.get("zip", "").strip()
    module = row.get("module", "").strip()
    preceding_test = row.get("preceding_test", "").strip()
    flaky_test = row.get("flaky_test", "").strip()
    javav = row.get("javav", "").strip()
    nondex_seed = row.get("nondexSeed", "").strip()
    iter_s = str(iterations)

    if test_type == "britle":
        return "flaky_analysis_tool_od_brittle.sh", [
            issue_id, zip_name, module, preceding_test, flaky_test, iter_s, code_version
        ]

    if test_type == "od":
        script = "flaky_analysis_tool_od_proto.sh" if module.startswith("hadoop") else "flaky_analysis_tool_od.sh"
        return script, [issue_id, zip_name, module, preceding_test, flaky_test, iter_s, code_version]

    if test_type == "td":
        script = "flaky_analysis_tool_td_proto.sh" if module.startswith("hadoop") else "flaky_analysis_tool_td.sh"
        return script, [issue_id, zip_name, module, flaky_test, iter_s, code_version]

    if test_type == "id":
        if javav == "8":
            script = "flaky_analysis_tool_id_8.sh"
        elif javav == "17":
            script = "flaky_analysis_tool_id_17.sh"
        else:
            script = "flaky_analysis_tool_id_11.sh"
        return script, [issue_id, zip_name, module, flaky_test, iter_s, code_version, nondex_seed]

    if test_type == "raft":
        return "flaky_analysis_tool_raft.sh", [issue_id, zip_name, module, flaky_test, iter_s, code_version]

    if test_type == "nio":
        return "flaky_analysis_tool_nio.sh", [issue_id, zip_name, module, flaky_test, iter_s, code_version]

    script = "flaky_analysis_tool_proto.sh" if module.startswith("hadoop") else "flaky_analysis_tool.sh"
    return script, [issue_id, zip_name, module, flaky_test, iter_s, code_version]



def _helper_image_for_coverage_fallback(test_input: TestInput, row: dict[str, str]) -> str:
    """
    Read BASE_IMAGE_NAME from the same ReproFlake helper script.
    """
    workdir = _script_workdir(test_input)
    test_type = row.get("test_type", "").strip()
    javav = row.get("javav", "").strip()

    if test_type == "od":
        helper = workdir / "flaky_analysis_tool_od.sh"
    else:
        helper = workdir / f"flaky_analysis_tool_id_{javav}.sh"
        if not helper.is_file():
            helper = workdir / "flaky_analysis_tool_id_11.sh"

    if helper.is_file():
        text = helper.read_text(encoding="utf-8", errors="replace")
        match = re.search(r'^\s*BASE_IMAGE_NAME=["\']?([^"\'\s]+)', text, re.MULTILINE)
        if match:
            return match.group(1)

    if test_type == "od" or javav == "8":
        return "flaky_base_jdk8_od_cov"

    return "flaky_base_jdk_11_id_cover_new"

def _run_reproflake_coverage_helper(test_input: TestInput, row: dict[str, str]) -> bool:
    """Run the same ReproFlake helper used for reproduction, forcing 1 iteration."""
    workdir = _script_workdir(test_input)
    # TD flakes only manifest in the sleep-injected FlakyCodeChange copy.
    test_type = (row.get("test_type") or "").strip().lower()
    code_version = "FlakyCodeChange" if test_type == "td" else "Flaky"
    script_name, args = _helper_script_and_args(row, iterations=1, code_version=code_version)
    script_path = workdir / script_name
    if not script_path.is_file():
        logger.warning("ReproFlake coverage helper not found: %s", script_path)
        return False

    try:
        script_path.chmod(script_path.stat().st_mode | 0o111)
    except OSError:
        pass

    cmd = ["bash", str(script_path)] + args
    logger.info("Collecting JaCoCo coverage with ReproFlake helper: %s", " ".join(shlex.quote(str(x)) for x in cmd))
    ok, output = _run_cmd(cmd, cwd=str(workdir), timeout=test_input.coverage_timeout, shell=False)

    logger.info("Coverage helper exit ok=%s", ok)
    logger.info("Coverage helper output tail:\n%s", output)
    
    if not ok:
        logger.warning("ReproFlake coverage helper failed; output tail:\n%s", output[-3000:])
        
    logger.warning("ReproFlake coverage output:\n%s", output)
    
    return ok


def _parse_reports_to_repo_coverage(
    test_input: TestInput,
    row: dict[str, str],
    report_paths: list[Path],
) -> CoverageData:
    """Parse JaCoCo reports and map covered lines back to the editable repo."""
    module = row.get("module", "").strip().strip("/")
    issue_dir = _reproflake_issue_dir(test_input, row)
    repo_root = Path(test_input.repo_root).resolve()

    parse_roots: list[Path] = []
    for source_name in ("Flaky", "FlakyCodeChange"):
        artifact_source_root = issue_dir / source_name
        if artifact_source_root.is_dir():
            if module and module != ".":
                parse_roots.append(artifact_source_root / module)
            parse_roots.append(artifact_source_root)

    if module and module != ".":
        parse_roots.append(repo_root / module)
    parse_roots.append(repo_root)

    repo_lines: dict[str, set[int]] = {}
    artifact_lines: dict[str, set[int]] = {}

    for report_path in report_paths:
        for parse_root in parse_roots:
            if not parse_root.is_dir():
                continue

            coverage = parse_jacoco_xml(str(report_path), str(parse_root))
            for file in coverage.files:
                path = Path(file).resolve()
                lines = coverage.lines_by_file.get(str(path), set())

                if artifact_source_root.is_dir():
                    try:
                        path.relative_to(artifact_source_root.resolve())
                        artifact_lines.setdefault(str(path), set()).update(lines)
                        continue
                    except ValueError:
                        pass

                if path.is_file():
                    repo_lines.setdefault(str(path), set()).update(lines)

    if artifact_lines and artifact_source_root.is_dir():
        mapped = _map_artifact_coverage_to_repo(
            CoverageData(
                files=list(artifact_lines),
                lines_by_file=artifact_lines,
            ),
            artifact_root=artifact_source_root,
            repo_root=repo_root,
            module=module,
        )
        for file, lines in mapped.lines_by_file.items():
            repo_lines.setdefault(file, set()).update(lines)

    files = sorted(
        (file for file in repo_lines if Path(file).is_file()),
        key=lambda file: len(repo_lines[file]),
        reverse=True,
    )
    return CoverageData(
        files=files,
        lines_by_file={file: repo_lines[file] for file in files},
    )

def _copy_helper_coverage_to_work_repo(test_input: TestInput, row: dict[str, str]) -> None:
    issue_id = row.get("issue_id", test_input.repro_issue_id or "").strip()
    workdir = _script_workdir(test_input)

    helper_root = workdir / "data" / issue_id
    work_repo_root = workdir / "data" / f"{issue_id}_work_repo" / "Flaky"

    if not helper_root.is_dir() or not work_repo_root.is_dir():
        return

    dst = work_repo_root / "flaky-result" / "coverage"
    dst.mkdir(parents=True, exist_ok=True)

    for xml in helper_root.glob("**/*jacoco*.xml"):
        if xml.is_file():
            shutil.copy2(xml, dst / xml.name)
            logger.info("Copied helper JaCoCo XML to work repo: %s", dst / xml.name)
            
def _collect_reproflake_docker_coverage(test_input: TestInput) -> CoverageData:
    row = _read_reproflake_row(test_input)
    if not row:
        return CoverageData.empty()

    issue_dir = _reproflake_issue_dir(test_input, row)
    report_paths = _find_reproflake_coverage_reports(issue_dir)
    
    # if not report_paths:
    #     _run_reproflake_coverage_helper(test_input, row)
    #     _copy_helper_coverage_to_work_repo(test_input, row)
    #     report_paths = _find_reproflake_coverage_reports(issue_dir)

    # if not report_paths:
    #     logger.warning("No ReproFlake JaCoCo XML reports found under: %s", issue_dir)
    #     return CoverageData.empty()

    if not report_paths:
        ok = _run_reproflake_coverage_helper(test_input, row)

        report_paths = _find_reproflake_coverage_reports(issue_dir)
        logger.info(
            "After coverage helper, found JaCoCo reports: %s",
            [str(p) for p in report_paths],
        )

        if not ok:
            logger.warning("Coverage helper failed before producing usable coverage.")
            return CoverageData.empty()
        
        _copy_helper_coverage_to_work_repo(test_input, row)
        report_paths = _find_reproflake_coverage_reports(issue_dir)

    if not report_paths:
        logger.warning("No ReproFlake JaCoCo XML reports found under: %s", issue_dir)
        return CoverageData.empty()

    coverage = _parse_reports_to_repo_coverage(test_input, row, report_paths)

    # The helper's Docker containers can leave root-owned files in data/<issue_id>.
    _docker_chown(issue_dir)

    if coverage.files:
        logger.info(
            "ReproFlake helper coverage selected %d file(s) mapped back to --repo.",
            len(coverage.files),
        )
    else:
        logger.warning(
            "ReproFlake helper coverage produced no files that map back to --repo."
        )
    return coverage


def _covered_line_numbers(sourcefile_elem: ET.Element) -> set[int]:
    covered: set[int] = set()
    for line in sourcefile_elem.findall("line"):
        try:
            line_number = int(line.attrib["nr"])
            covered_instructions = int(line.attrib.get("ci", "0"))
            covered_branches = int(line.attrib.get("cb", "0"))
        except (KeyError, ValueError):
            continue

        if covered_instructions > 0 or covered_branches > 0:
            covered.add(line_number)

    return covered


def _candidate_paths(repo_root: str, package_name: str, filename: str) -> list[str]:
    package_path = package_name.replace(".", os.sep).replace("/", os.sep)
    rel = os.path.join(package_path, filename) if package_path else filename

    return [
        os.path.join(repo_root, "src", "main", "java", rel),
        os.path.join(repo_root, "src", "test", "java", rel),
        os.path.join(repo_root, rel),
    ]


def parse_jacoco_xml(report_path: str, repo_root: str) -> CoverageData:
    if not os.path.isfile(report_path):
        logger.warning("JaCoCo report not found: %s", report_path)
        return CoverageData.empty()

    try:
        tree = ET.parse(report_path)
    except (ET.ParseError, OSError) as exc:
        logger.warning("Could not parse JaCoCo XML report %s: %s", report_path, exc)
        return CoverageData.empty()

    root = tree.getroot()
    lines_by_file: dict[str, set[int]] = {}

    for package in root.findall("package"):
        package_name = package.attrib.get("name", "").replace("/", ".")
        for sourcefile in package.findall("sourcefile"):
            filename = sourcefile.attrib.get("name", "")
            if not filename.endswith(".java"):
                continue

            covered_lines = _covered_line_numbers(sourcefile)
            if not covered_lines:
                continue

            for candidate in _candidate_paths(repo_root, package_name, filename):
                if os.path.isfile(candidate):
                    path = os.path.abspath(candidate)
                    lines_by_file.setdefault(path, set()).update(covered_lines)
                    break

    files = sorted(
        lines_by_file,
        key=lambda path: len(lines_by_file[path]),
        reverse=True,
    )
    return CoverageData(files=files, lines_by_file=lines_by_file)


def _prioritize_coverage_files(test_input: TestInput, files: list[str]) -> list[str]:
    if not files:
        return []

    test_file_abs = test_input.test_file
    if not os.path.isabs(test_file_abs):
        test_file_abs = os.path.join(test_input.repo_root, test_input.test_file)
    test_file_abs = os.path.abspath(test_file_abs)

    max_files = max(1, test_input.coverage_max_files)

    test_parts = Path(test_file_abs).parts
    test_pkg_hint = ""
    if "java" in test_parts:
        idx = list(test_parts).index("java")
        pkg_parts = test_parts[idx + 1:-1]
        if pkg_parts:
            # Use first few package dirs as a weak locality signal.
            test_pkg_hint = os.sep.join(pkg_parts[:3])

    def score(path: str) -> tuple[int, str]:
        norm = os.path.abspath(path)
        if norm == test_file_abs:
            return (0, norm)
        if test_pkg_hint and test_pkg_hint in norm:
            return (1, norm)
        if f"{os.sep}src{os.sep}main{os.sep}java{os.sep}" in norm:
            return (2, norm)
        if f"{os.sep}src{os.sep}test{os.sep}java{os.sep}" in norm:
            return (3, norm)
        return (4, norm)

    ordered = sorted(files, key=score)

    result: list[str] = []
    seen: set[str] = set()
    if os.path.isfile(test_file_abs):
        result.append(test_file_abs)
        seen.add(test_file_abs)

    for path in ordered:
        abs_path = os.path.abspath(path)
        if abs_path in seen:
            continue
        result.append(abs_path)
        seen.add(abs_path)
        if len(result) >= max_files:
            break

    return result



def _prioritize_coverage_data(
    test_input: TestInput,
    coverage: CoverageData,
) -> CoverageData:
    """Apply the existing file limit without losing covered line numbers."""
    files = _prioritize_coverage_files(test_input, coverage.files)
    return CoverageData(
        files=files,
        lines_by_file={
            file: set(coverage.lines_by_file.get(file, set()))
            for file in files
        },
    )

def collect_jacoco_coverage(test_input: TestInput) -> CoverageData:
    """Run JaCoCo and return covered files plus exact covered source lines."""
    if not test_input.use_jacoco_coverage:
        return CoverageData.empty()

    if test_input.language.lower() != "java":
        logger.info(
            "JaCoCo coverage requested, but language is %s; ignoring.",
            test_input.language,
        )
        return CoverageData.empty()

    cache_key = (
        os.path.abspath(test_input.repo_root),
        test_input.test_file,
        test_input.test_func,
        test_input.coverage_cmd or "",
    )
    if cache_key in _COVERAGE_CACHE:
        return _COVERAGE_CACHE[cache_key]

    report_path = _resolve_report_path(test_input)

    # Remove stale report so old execution data is not mistaken for this run.
    try:
        if os.path.isfile(report_path):
            os.remove(report_path)
    except OSError:
        pass

    if test_input.repro_script and test_input.repro_issue_id:
        try:
            coverage = _collect_reproflake_docker_coverage(test_input)
        except Exception as exc:
            logger.warning("ReproFlake jar-based JaCoCo coverage failed: %s", exc)
            coverage = CoverageData.empty()

        if coverage.files:
            coverage = _prioritize_coverage_data(test_input, coverage)
            _COVERAGE_CACHE[cache_key] = coverage
            return coverage

        logger.warning(
            "ReproFlake helper coverage produced no usable XML; using nearby-file scope."
        )

        coverage = CoverageData.empty()
        _COVERAGE_CACHE[cache_key] = coverage
        return coverage
   
    cmd = _build_coverage_cmd(test_input)
    logger.info("Collecting JaCoCo coverage locally: %s", cmd)
    ok, output = _run_cmd(
        cmd,
        cwd=test_input.repo_root,
        timeout=test_input.coverage_timeout,
    )
    if not ok:
        logger.warning(
            "JaCoCo coverage command failed; will try to parse any report "
            "that exists. Output tail:\n%s",
            output[-2000:],
        )

    coverage = parse_jacoco_xml(report_path, test_input.repo_root)
    coverage = _prioritize_coverage_data(test_input, coverage)

    if coverage.files:
        logger.info(
            "JaCoCo coverage selected %d file(s) for context.",
            len(coverage.files),
        )
    else:
        logger.warning(
            "JaCoCo coverage produced no usable files; "
            "falling back to nearby-file scope."
        )

    _COVERAGE_CACHE[cache_key] = coverage
    return coverage


def collect_jacoco_coverage_files(test_input: TestInput) -> list[str]:
    return collect_jacoco_coverage(test_input).files
