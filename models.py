"""
core/types.py – shared dataclasses for FlakyGuard Simple.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass
class TestInput:
    """Everything needed to locate, run, and repair the flaky test."""
    repo_root: str
    test_file: str
    test_func: str
    test_case: str
    language: str = "java"

    # Flaky-test root-cause category, from test_config.csv's first column
    # (test_type: "id"/"od"/"nio"/"td"/"brittle"/...). Passed into the
    # thought/fix prompts as explicit context -- see
    # prompts.py::describe_category() for the category -> description
    # mapping (matches AgentFlake's own _PRETTY_TYPE wording).
    category: str = ""

    # The polluter/state-setter test that runs immediately before the victim
    # (test_config.csv's "preceding_test" / "polluter/state setter" column).
    # Only OD and its britle variant have one -- id/nio/td leave this empty.
    # Mirrors AgentFlake's own optional "Polluter:" prompt line
    # (orchestrator_common.py's polluter_line), shown alongside Category
    # only when set.
    polluter: str = ""

    # Command-based fallback mode only. In the ReproFlake/script workflow,
    # reproduction and validation are driven by single_runner.sh + test_config.csv.
    run_cmd: str = "mvn -Dtest={test_func} test"
    repro_runs: int = 100

    # Script-based reproduction/validation mode.
    # Reproduction runs:
    #   bash ./single_runner.sh <repro_issue_id>
    # Validation applies the generated patch to the artifact's Flaky copy and
    # reruns the matching helper script with CODE_VERSION=Fixed.
    repro_script: str = ""
    repro_issue_id: str = ""
    repro_workdir: str = ""
    repro_config_csv: str = ""
    repro_zip: str = ""
    repro_timeout: int = 1800
    script_validation_iterations: int = 10

    # Paper-style repair-loop parameters.
    # M × P × N = context_attempts × thoughts_per_context × fixes_per_thought.
    context_attempts: int = 3
    thoughts_per_context: int = 2
    fixes_per_thought: int = 3

    # Optional Java coverage-based context narrowing.
    # When enabled, pipeline.py runs JaCoCo and builds the call graph only
    # from covered files plus the test file, closer to original FlakyGuard.
    use_jacoco_coverage: bool = False
    coverage_cmd: str = ""
    coverage_report: str = "target/site/jacoco/jacoco.xml"
    coverage_timeout: int = 900
    coverage_max_files: int = 80

    # Set by reproduce_failure() when a multi-round script reproduction (e.g.
    # FLAKYGUARD_REPRO_ITERATIONS>1) finds a NonDex seed that actually failed
    # in *this* environment. When set, coverage collection reuses this seed
    # instead of test_config.csv's recorded nondexSeed, so coverage is
    # measured on a run that actually exercises the failing code path.
    repro_failing_seed: str = ""

    # Compilation-retry step (mirrors original FlakyGuard's build-error
    # repair in flakyguard/utils/fix_build_errors.py). This project's actual
    # compile/test step runs inside a per-JDK Docker container driven by the
    # ReproFlake helper scripts (see runner.py::validate_fix_detailed and
    # compile_repair.py) -- there is no separate host-side build to run --
    # so this only gates whether a candidate that breaks that Docker build
    # gets a few LLM-repair-and-re-validate attempts before being discarded.
    verify_compile_before_validate: bool = True
    max_compile_fix_retries: int = 3

    # Persist every LLM call's full prompt + response under
    # <output_dir>/llm_calls/ (see llm.py::complete()'s log_dir/tag and
    # pipeline.py::run_pipeline()). Useful for debugging a specific run, but
    # adds up over a large batch (dozens of calls per run, several KB to
    # 50+ KB each when compile-repair embeds full file contents) -- turn
    # off for bulk/CI runs where only run_log.jsonl and the CSVs matter.
    log_llm_calls: bool = True


@dataclasses.dataclass
class FlakyInfo:
    """Captured details of a reproduced flaky failure."""
    error: str
    error_trace: str
    error_file: str = ""
    error_line: int = 0


@dataclasses.dataclass
class FuncNode:
    """A function definition extracted from source code."""
    name: str
    filepath: str
    start_line: int
    end_line: int
    source: str


@dataclasses.dataclass
class Context:
    """Code context collected by smart graph search."""
    func_nodes: list[FuncNode] = dataclasses.field(default_factory=list)
    imports: dict[str, str] = dataclasses.field(default_factory=dict)

    # Coverage-scope diagnostics from collect_context() (pipeline.py): which
    # source narrowed the call-graph scope for this context attempt, and how
    # much it covered. Carried on Context (rather than only logged) so
    # run_pipeline() can record it per attempt in run_log.jsonl -- see the
    # "context" step there.
    scope_source: str = "unknown"  # "jacoco" | "nearby_files"
    scope_files_count: int = 0
    coverage_covered_lines_count: int = 0
    scope_files: list[str] = dataclasses.field(default_factory=list)
    covered_files_count: int = 0  # files JaCoCo actually hit (scope_files may add the test/polluter files)
    # Everything smart BFS could choose from (after coverage pruning), each
    # BFS expansion step, and what the F filter removed -- for run_log.jsonl.
    graph_methods: list[str] = dataclasses.field(default_factory=list)
    bfs_steps: list[dict] = dataclasses.field(default_factory=list)
    bfs_dropped: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class SearchReplaceEdit:
    """One SEARCH/REPLACE block produced by the LLM."""
    filepath: str
    search_text: str
    replace_text: str


@dataclasses.dataclass
class Fix:
    """A proposed fix: search/replace edits plus explanation."""
    edits: list[SearchReplaceEdit]
    explanation: str = ""


@dataclasses.dataclass(frozen=True)
class CompilationDefect:
    """A single compiler error/warning parsed from a Maven/javac build log."""
    path: str
    line: int
    severity: str  # "ERROR" or "WARNING"
    message: str
