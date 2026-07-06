#!/usr/bin/env python3

import argparse
import logging
import os
import sys
import csv
import shutil
import zipfile
import subprocess
import time

from llm import consume_token_usage

from pathlib import Path
# Ensure local packages are importable when run directly.
sys.path.insert(0, os.path.dirname(__file__))

from models import TestInput
from pipeline import run_pipeline
from runner import reproduce_failure

logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="FlakyGuard Simple – automated flaky-test repair."
    )

    # Required repair inputs.

    parser.add_argument("--repo", default=".", help="Path to repo; ignored in ReproFlake mode")

    parser.add_argument(
        "--test-file",
        default="",
        help="Optional test-file override. Normally inferred from test_config.csv.",
    )
    parser.add_argument(
        "--test-func",
        default="",
        help="Optional test-method override. Normally inferred from test_config.csv.",
    )
    parser.add_argument(
        "--test-case",
        default="",
        help="Optional test-case override; defaults to the inferred test method.",
    )
    parser.add_argument(
        "--language",
        default="java",
        choices=["java", "python"],
        help="Source language (default: java)",
    )

    # Script-based reproduction/validation.
    parser.add_argument(
        "--repro-script",
        required=True,
        help="Path to single_runner.sh. Reproduction and validation use this script's artifact workflow.",
    )
    parser.add_argument(
        "--repro-issue-id",
        required=True,
        help="Issue id passed to single_runner.sh. Must match the issue_id column in test_config.csv.",
    )
    parser.add_argument(
        "--repro-config-csv",
        required=True,
        help="Path to test_config.csv.",
    )
    parser.add_argument(
        "--repro-zip",
        help="Optional artifact zip to copy into <workdir>/data/. If omitted, single_runner.sh can download using the CSV url column.",
    )
    parser.add_argument(
        "--repro-workdir",
        help="Directory where single_runner.sh should run. Default: directory containing --repro-script.",
    )
    parser.add_argument(
        "--repro-timeout",
        type=int,
        default=1800,
        help="Timeout in seconds for script-based reproduction/validation (default: 1800)",
    )

    # Coverage-based context narrowing.
    parser.add_argument(
        "--use-jacoco-coverage",
        action="store_true",
        help="Run JaCoCo and use covered Java files to limit call-graph context.",
    )
    parser.add_argument(
        "--coverage-runner",
        default="",
        help=(
            "Optional command template for generating JaCoCo coverage. "
            "Placeholders: {test_class}, {test_func}, {test_case}, {test_file}, {test_dir}. "
            "Default uses jacoco-maven-plugin prepare-agent/test/report."
        ),
    )
    parser.add_argument(
        "--coverage-report",
        default="target/site/jacoco/jacoco.xml",
        help="Path to JaCoCo XML report relative to --repo (default: target/site/jacoco/jacoco.xml).",
    )
    parser.add_argument(
        "--coverage-timeout",
        type=int,
        default=900,
        help="Timeout in seconds for the JaCoCo coverage command (default: 900).",
    )
    parser.add_argument(
        "--coverage-max-files",
        type=int,
        default=80,
        help="Maximum covered files to use for context (default: 80).",
    )

    # Paper-style repair-loop parameters: M × P × N.
    parser.add_argument(
        "--attempts",
        type=int,
        default=None,
        help="Legacy alias for --context-attempts/M. If set, overrides --context-attempts.",
    )
    parser.add_argument(
        "--context-attempts",
        type=int,
        default=3,
        help="M: number of context collection attempts (default: 3).",
    )
    parser.add_argument(
        "--thoughts-per-context",
        type=int,
        default=2,
        help="P: high-level thoughts/root-cause plans per context (default: 2).",
    )
    parser.add_argument(
        "--fixes-per-thought",
        type=int,
        default=3,
        help="N: concrete patch attempts per thought (default: 3).",
    )
    parser.add_argument(
        "--children",
        type=int,
        default=3,
        help="Smart BFS k: children selected per node (default: 3)",
    )
    parser.add_argument(
        "--max-funcs",
        type=int,
        default=5,
        help="Global filter F: max functions in context (default: 5)",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=-1,
        help="BFS depth limit, -1 = unlimited (default: -1)",
    )

    # Output.
    parser.add_argument(
        "--output-dir",
        default="patches",
        help="Directory to save patch files (default: ./patches)",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")

    return parser.parse_args()

def _read_reproflake_row(csv_path: Path, issue_id: str) -> dict[str, str]:
    columns = [
        "test_type", "issue_id", "zip", "module", "preceding_test",
        "flaky_test", "iterations", "config", "javav", "nondexSeed", "url",
    ]

    with csv_path.open(newline="", encoding="utf-8", errors="replace") as f:
        sample = f.readline()
        f.seek(0)

        if "issue_id" in sample:
            reader = csv.DictReader(f)
            for row in reader:
                clean = {
                    str(k).strip().lstrip("\ufeff"): (v or "").strip()
                    for k, v in row.items()
                    if k
                }
                if clean.get("issue_id") == issue_id:
                    return clean
        else:
            reader = csv.reader(f)
            for raw in reader:
                if not raw:
                    continue
                row = dict(zip(columns, [cell.strip() for cell in raw + [""] * len(columns)]))
                if row.get("issue_id") == issue_id:
                    return row

    raise RuntimeError(f"Could not find issue_id={issue_id} in {csv_path}")


def _safe_remove_dir(path: Path) -> None:
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except PermissionError:
        # Best effort: make files writable, then retry.
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                try:
                    os.chmod(os.path.join(root, name), 0o700)
                except OSError:
                    pass
        shutil.rmtree(path)

#def _prepare_reproflake_work_repo(args) -> str:
def _prepare_reproflake_work_repo(
    args,
) -> tuple[str, dict[str, str]]:
    """
    Extract ReproFlake artifact to a stable work repo and return Flaky/ path.

    This avoids generating patches from a separate local checkout that may not
    match the ReproFlake artifact.
    """
    # if not getattr(args, "repro_script", None) or not getattr(args, "repro_issue_id", None):
    #     return os.path.abspath(args.repo)
    if not getattr(args, "repro_script", None) or not getattr(args, "repro_issue_id", None):
        return os.path.abspath(getattr(args, "repo", ".") or "."), {}
    workdir = (
        Path(args.repro_workdir).resolve()
        if getattr(args, "repro_workdir", None)
        else Path(args.repro_script).resolve().parent
    )

    csv_path = (
        Path(args.repro_config_csv).resolve()
        if getattr(args, "repro_config_csv", None)
        else workdir / "test_config.csv"
    )

    if not csv_path.is_file():
        raise FileNotFoundError(f"test_config.csv not found: {csv_path}")

    row = _read_reproflake_row(csv_path, args.repro_issue_id)

    zip_name = row.get("zip", "").strip()
    if not zip_name:
        raise RuntimeError(f"No zip column found for issue_id={args.repro_issue_id}")

    data_dir = workdir / "data"
    zip_path = data_dir / f"{zip_name}.zip"

    if not zip_path.is_file():
        url = row.get("url", "").strip()
        if not url:
            raise FileNotFoundError(
                f"ReproFlake zip not found and CSV has no url: {zip_path}"
            )

        data_dir.mkdir(parents=True, exist_ok=True)
        part_path = zip_path.with_name(zip_path.name + ".part")

        logger.info("Downloading ReproFlake zip directly: %s", url)

        result = subprocess.run(
            [
                "curl",
                "-L",
                "--fail",
                "--retry", "3",
                "--retry-delay", "2",
                "-o", str(part_path),
                url,
            ],
            cwd=str(workdir),
            capture_output=True,
            text=True,
            timeout=600,
        )

        if result.returncode != 0:
            raise RuntimeError(
                f"Failed to download ReproFlake zip from {url}\n"
                f"stdout:\n{result.stdout[-2000:]}\n"
                f"stderr:\n{result.stderr[-2000:]}"
            )

        part_path.replace(zip_path)
    
    work_repo = data_dir / f"{args.repro_issue_id}_work_repo"
    _safe_remove_dir(work_repo)
    work_repo.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(work_repo)

    nested = work_repo / zip_name
    if nested.is_dir():
        for child in list(nested.iterdir()):
            shutil.move(str(child), str(work_repo / child.name))
        nested.rmdir()

    flaky_repo = work_repo / "Flaky"
    if not flaky_repo.is_dir():
        raise FileNotFoundError(f"Flaky source not found after extraction: {flaky_repo}")

    logger.info("Using ReproFlake artifact source as repo_root: %s", flaky_repo)
    #return str(flaky_repo.resolve())
    return str(flaky_repo.resolve()), row

def _infer_java_test_from_config(
    repo_root: str,
    row: dict[str, str],
) -> tuple[str, str]:
    """
    Infer the test source file and method from test_config.csv.

    Example:
      module:
        dubbo-rpc/dubbo-rpc-api

      flaky_test:
        org.apache.dubbo.rpc.proxy.jdk.JdkProxyFactoryTest#testGetInvoker

      result:
        dubbo-rpc/dubbo-rpc-api/src/test/java/
        org/apache/dubbo/rpc/proxy/jdk/JdkProxyFactoryTest.java
    """
    full_test_name = row.get("flaky_test", "").strip()

    if not full_test_name or "#" not in full_test_name:
        raise ValueError(
            "CSV flaky_test must use the format "
            "'fully.qualified.TestClass#testMethod'; "
            f"got: {full_test_name!r}"
        )

    class_name, test_func = full_test_name.rsplit("#", 1)

    # Handle parameterized/display-name suffixes if present.
    test_func = test_func.split("[", 1)[0]
    test_func = test_func.split("(", 1)[0].strip()

    # A nested class such as ExampleTest$NestedTest is normally stored in
    # ExampleTest.java.
    top_level_class = class_name.split("$", 1)[0]

    package_file = Path(
        *top_level_class.split(".")
    ).with_suffix(".java")

    module = row.get("module", "").strip().strip("/")
    repo_path = Path(repo_root).resolve()

    if module and module != ".":
        module_root = repo_path / module
    else:
        module_root = repo_path

    if not module_root.is_dir():
        raise FileNotFoundError(
            f"Module directory from CSV does not exist: {module_root}"
        )

    # First try the standard Maven location.
    direct_candidate = (
        module_root
        / "src"
        / "test"
        / "java"
        / package_file
    )

    if direct_candidate.is_file():
        return (
            direct_candidate.relative_to(repo_path).as_posix(),
            test_func,
        )

    # Fallback: equivalent to manually running:
    # find <module> -name TestClass.java
    filename = package_file.name

    matches = [
        path
        for path in module_root.rglob(filename)
        if path.is_file()
        and "target" not in path.parts
        and "build" not in path.parts
        and ".git" not in path.parts
    ]

    if not matches:
        raise FileNotFoundError(
            f"Could not find {filename} under {module_root}. "
            f"Derived from CSV flaky_test={full_test_name!r}"
        )

    package_suffix = package_file.as_posix()

    # Prefer the file whose ending matches the fully qualified class path.
    package_matches = [
        path
        for path in matches
        if path.as_posix().endswith(package_suffix)
    ]

    # Prefer Maven test source when several files have the same name.
    maven_test_matches = [
        path
        for path in package_matches
        if "/src/test/java/" in f"/{path.as_posix()}"
    ]

    if len(maven_test_matches) == 1:
        selected = maven_test_matches[0]
    elif len(package_matches) == 1:
        selected = package_matches[0]
    elif len(matches) == 1:
        selected = matches[0]
    else:
        formatted = "\n".join(f"  - {path}" for path in matches)
        raise RuntimeError(
            f"Multiple possible source files found for {full_test_name}:\n"
            f"{formatted}\n"
            "Use --test-file to override this ambiguous case."
        )

    return selected.relative_to(repo_path).as_posix(), test_func

def main():
    args = parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    context_attempts = args.attempts if args.attempts is not None else args.context_attempts


    #repo_root = _prepare_reproflake_work_repo(args)
    repo_root, repro_row = _prepare_reproflake_work_repo(args)

    test_file = args.test_file.lstrip("/")

    if repro_row:
        inferred_test_file, inferred_test_func = _infer_java_test_from_config(
            repo_root,
            repro_row,
        )
    else:
        inferred_test_file = ""
        inferred_test_func = ""
    test_file = args.test_file or inferred_test_file
    test_func = args.test_func or inferred_test_func
    test_case = args.test_case or test_func

    if not test_file:
        raise ValueError(
            "Could not determine test file. Supply --test-file or use "
            "a valid ReproFlake CSV row."
        )

    if not test_func:
        raise ValueError(
            "Could not determine test method. Supply --test-func or ensure "
            "the CSV flaky_test column contains Class#method."
        )

    logger.info("Resolved repo_root: %s", repo_root)
    logger.info("Resolved test_file: %s", test_file)
    logger.info("Resolved test_func: %s", test_func)

    
    if args.repro_script and args.repro_issue_id:
        workdir = (
            Path(args.repro_workdir).resolve()
            if args.repro_workdir
            else Path(args.repro_script).resolve().parent
        )
        csv_path = (
            Path(args.repro_config_csv).resolve()
            if args.repro_config_csv
            else workdir / "test_config.csv"
        )

        row = _read_reproflake_row(csv_path, args.repro_issue_id)
        module = row.get("module", "").strip().strip("/")

        logger.info("CSV module for this issue: %r", module)

        if module and module != ".":
            prefix = module + "/"
            if not test_file.startswith(prefix):
                test_file = prefix + test_file

    logger.info("Final repo_root used by FlakyGuard: %s", repo_root)
    logger.info("Final test_file used by FlakyGuard: %s", test_file)

    test_input = TestInput(
    repo_root=repo_root,
        test_file=test_file,
        test_func=test_func,
        test_case=test_case,
        language=args.language,
        repro_script=args.repro_script,
        repro_issue_id=args.repro_issue_id,
        repro_workdir=args.repro_workdir or "",
        repro_config_csv=args.repro_config_csv,
        repro_zip=args.repro_zip or "",
        repro_timeout=args.repro_timeout,
        script_validation_iterations=5,
        use_jacoco_coverage=args.use_jacoco_coverage,
        coverage_cmd=args.coverage_runner or "",
        coverage_report=args.coverage_report,
        coverage_timeout=args.coverage_timeout,
        coverage_max_files=args.coverage_max_files,
    )
    print(f"\n[1/3] Reproducing flaky failure (script issue_id={test_input.repro_issue_id})…")
    flaky_info = reproduce_failure(test_input)

    if flaky_info is None:
        print("Could not reproduce a failure. Test may not be flaky in this environment.")
        sys.exit(1)

    print("Failure reproduced.")

    total_candidates = context_attempts * args.thoughts_per_context * args.fixes_per_thought
    print(
        f"\n[2/3] Running fixing pipeline "
        f"(M={context_attempts}, P={args.thoughts_per_context}, "
        f"N={args.fixes_per_thought}, candidates={total_candidates})…"
    )

    output_dir = os.path.join(test_input.repo_root, args.output_dir)

    run_start_time = time.time()

    success, message = run_pipeline(
        test_input=test_input,
        flaky_info=flaky_info,
        context_attempts=context_attempts,
        thoughts_per_context=args.thoughts_per_context,
        fixes_per_thought=args.fixes_per_thought,
        k=args.children,
        depth_limit=args.depth,
        max_funcs=args.max_funcs,
        output_dir=output_dir,
    )

    run_elapsed_seconds = time.time() - run_start_time

    token_rows = consume_token_usage()


    total_llm_time = sum(r.get("llm_elapsed_seconds", 0.0) for r in token_rows)

    total_input = sum(r["llm_input_tokens"] for r in token_rows)
    total_output = sum(r["llm_output_tokens"] for r in token_rows)

    out_row = dict(repro_row) if repro_row else {}
    
    out_row["whole_run_time_seconds"] = round(run_elapsed_seconds, 3)
    out_row["llm_time_seconds_total"] = round(total_llm_time, 3)

    out_row["llm_num_calls"] = len(token_rows)
    out_row["llm_input_tokens_total"] = total_input
    out_row["llm_output_tokens_total"] = total_output
    out_row["llm_total_tokens_total"] = total_input + total_output

    out_row["llm_input_tokens_by_call"] = "|".join(
        str(r["llm_input_tokens"]) for r in token_rows
    )
    out_row["llm_output_tokens_by_call"] = "|".join(
        str(r["llm_output_tokens"]) for r in token_rows
    )
    out_row["llm_total_tokens_by_call"] = "|".join(
        str(r["llm_total_tokens"]) for r in token_rows
    )

    token_csv = Path("llm_token_usage.csv")
    write_header = not token_csv.exists()

    fieldnames = list(out_row.keys())

    with token_csv.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()
        writer.writerow(out_row)

    logger.info("Wrote LLM token usage to %s", token_csv)

    print("\n[3/3] Result:")
    if success:
        print("Fix found and validated.")
        print(f"Root cause: {message}")
        print(f"Patch saved to: {output_dir}/")
    else:
        print(f"No fix found: {message}")
        sys.exit(2)


if __name__ == "__main__":
    main()
