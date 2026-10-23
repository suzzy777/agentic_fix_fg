"""
core/prompts.py – prompt utilities for FlakyGuard Simple.

The paper-style flow uses two prompt stages:
  1. a high-level thought/root-cause/fixing-plan prompt
  2. a concrete SEARCH/REPLACE patch prompt conditioned on that thought
"""

from __future__ import annotations


# Flaky-test root-cause category, sourced from test_config.csv's first
# column (test_type) via TestInput.category. The category description the
# LLM sees here is kept consistent with the one used elsewhere in this
# project's tooling.
#
# Keys are verified against this project's own real test_config.csv files
# and its dispatch code (jacoco_coverage.py/runner.py/single_runner.sh)
# -- notably "britle" (misspelled, no second
# "t") is the actual value used in this project's data and code, not
# "brittle". "raft" is a category this project's dispatch code supports
# but that doesn't appear in any test_config.csv on disk yet; its
# description here is our own inference (no AgentFlake exemplar exists for
# it), since it isn't Java-iteration/order/timing in the usual sense --
# it shows up on Raft-consensus code (e.g. leader election/log
# replication), so treat it as a distinct, protocol-specific case.
CATEGORY_DESCRIPTIONS = {
    "od": "OD (Order-Dependent — a polluter test corrupts shared state)",
    "td": "TD (Timing-Dependent — race, async, or non-deterministic source)",
    "id": "ID (Implementation-Dependent — relies on JVM iteration order)",
    "nio": "NIO (Non-Idempotent-Outcome — self-pollutes across same-JVM invocations)",
    # Keyed as "britle" to match the misspelling actually used in this
    # project's test_config.csv data and dispatch code, but displayed with
    # the correct spelling since this text is what the LLM/humans read.
    "britle": (
        "Brittle (Order-Dependent variant — polluter corrupts shared state; "
        "structurally identical to OD)"
    ),
    "raft": (
        "Raft (Consensus/distributed-coordination flakiness — timing or state "
        "issues specific to Raft leader-election or log-replication code; "
        "diagnose as a timing/coordination issue scoped to the consensus "
        "protocol, not generic JVM ordering)"
    ),
    "unclassified": (
        "Unclassified (root cause unknown — no category-specific guidance "
        "is available; diagnose from code and error logs alone)"
    ),
}


def describe_category(category: str | None) -> str:
    """Return a human-readable description for a test_config.csv category code."""
    if not category:
        return "Unknown (no category provided — diagnose from code and error logs alone)"
    key = category.strip().lower()
    return CATEGORY_DESCRIPTIONS.get(key, category)


DEFAULT_BEST_PRACTICES = """\
## Best Practices for Fixing Flaky Tests

### Code Quality Standards
- Follow existing code conventions and patterns in the codebase.
- Use existing libraries and utilities when available.
- Ensure proper error handling and edge case coverage.
- Write clear, descriptive variable and function names.
- Add meaningful comments only when necessary to explain complex logic.

### Testing Best Practices
- Prefer deterministic test patterns over time-dependent or order-dependent logic.
- Ensure test isolation; tests should not depend on external state or other tests.
- Clean up resources properly in test setup or teardown.
- Use dependency injection and mocking to control external dependencies.
"""


SEARCH_REPLACE_FORMAT = """\
## Required Output Format

# SEARCH/REPLACE block Rules:

Every SEARCH/REPLACE block must use this format:

1. The FULL file path alone on a line, verbatim. No bold asterisks, no quotes around it, no escaping of characters.
2. The opening fence and code language, e.g. ```java
3. The start of search block: <<<<<<< SEARCH
4. A contiguous chunk of lines to search for in the existing source code.
5. The dividing line: =======
6. The lines to replace into the source code.
7. The end of the replace block: >>>>>>> REPLACE
8. The closing fence: ```

Example:

src/test/java/MyTest.java
```java
<<<<<<< SEARCH
    assertEquals(expected, result);
=======
    assertEquals(expected, actualResult);
>>>>>>> REPLACE
```

Every SEARCH section must EXACTLY MATCH the existing file content, character for character, including comments, spaces, indentation, and line breaks.

SEARCH/REPLACE blocks replace only the first matching occurrence.
Use multiple SEARCH/REPLACE blocks if needed.

Keep SEARCH/REPLACE blocks concise.
Break large edits into smaller blocks.
Include enough lines in each SEARCH section to uniquely match the code.

Only create SEARCH/REPLACE blocks for files that are provided in the prompt/context.
Prefer editing tests, not production code, unless production code is clearly necessary.

Do not include ellipses (...), [...], or incomplete code in SEARCH blocks.
Do not put explanations before the SEARCH/REPLACE blocks.

## Explanation Format

After all SEARCH/REPLACE blocks, include a brief explanation wrapped in EXPLANATION tags:

```
<EXPLANATION>
Brief explanation of what was causing the flakiness and how the fix addresses it.
</EXPLANATION>
```
"""


def get_flaky_test_thought_prompt(
    simplified_test_code: str,
    original_test_code: str,
    assertion_failures: str,
    error_trace: str,
    code_context: str,
    language: str,
    context_attempt: int = 1,
    thought_attempt: int = 1,
    total_thoughts: int = 1,
    best_practices: str | None = None,
    failed_thoughts: list[str] | None = None,
    category: str | None = None,
    polluter: str | None = None,
) -> str:
    """
    Generate the paper-style high-level thought prompt.

    The response should identify the root-cause category, explain why the
    failure is flaky, and propose a fixing plan. It should not produce code
    edits yet.
    """
    best_practices_text = best_practices or DEFAULT_BEST_PRACTICES

    failed_thoughts_section = ""

    if failed_thoughts:
        failed_thoughts_text = "\n\n".join(
            f"Failed thought {index + 1}:\n{thought}"
            for index, thought in enumerate(failed_thoughts)
        )

        failed_thoughts_section = f"""
## Previously Attempted Thoughts That Failed

The following strategies did not produce a valid fix:

{failed_thoughts_text}

Do not repeat the same root cause or fixing strategy.
Propose a substantially different explanation or plan.
"""
        
    category_line = f"**Category:** {describe_category(category)}\n\n" if category else ""
    polluter_line = f"**Polluter (runs immediately before the victim):** {polluter}\n\n" if polluter else ""

    if category:
        category_guidance = (
            f"This test's category is already known: {describe_category(category)}. "
            "Do not re-derive the category from scratch -- state it directly and spend "
            "your analysis on the SPECIFIC mechanism (which field, collection, singleton, "
            "or timing source is involved) and the fixing plan."
        )
        category_output_hint = f"Root-cause category: {describe_category(category)}"
    else:
        category_guidance = ""
        category_output_hint = "Root-cause category: ..."

    return f"""You are an expert at diagnosing flaky tests in {language}.
This is thought attempt {thought_attempt}/{total_thoughts} for context attempt {context_attempt}.

Your task is to produce a high-level fixing thought/plan only. Do NOT produce SEARCH/REPLACE edits yet.

{best_practices_text}

## Test Analysis

{category_line}{polluter_line}**Simplified test for reference:**
```{language}
{simplified_test_code}
```

**Original test to be fixed:**
```{language}
{original_test_code}
```

**Error messages:**
```
{assertion_failures}
```

**Stack trace:**
```
{error_trace}
```

**Additional context:**
```
{code_context}
```

## Common Flaky Test Root-Cause Categories

- Timing/race/asynchronous behavior
- Shared state or polluted global/static state
- Non-deterministic ordering or order-sensitive assertions
- Randomness/generated IDs/hash iteration ordering
- External dependency, filesystem, network, time, locale, or environment dependency
- Resource cleanup/isolation issue

{category_guidance}

{failed_thoughts_section}

## Required Output

Return exactly one high-level thought using this structure:

<THOUGHT>
{category_output_hint}
Root-cause explanation: ...
Fixing plan: ...
Why this plan is minimal and safe: ...
</THOUGHT>
"""


def get_flaky_test_fixing_prompt(
    simplified_test_code: str,
    original_test_code: str,
    assertion_failures: str,
    error_trace: str,
    code_context: str,
    language: str,
    output_format: str,
    best_practices: str | None = None,
    thought: str | None = None,
    category: str | None = None,
    polluter: str | None = None,
) -> str:
    """
    Generate a comprehensive prompt for fixing flaky tests.

    This follows the original FlakyGuard prompt structure:
    simplified test, original test, error messages, stack trace, additional
    context, common flaky-test patterns, fixing strategy, output format, and
    required output.
    """
    best_practices_text = best_practices or DEFAULT_BEST_PRACTICES
    thought_text = thought or "No separate thought was provided. Infer the most likely root cause and fixing plan before editing."
    category_line = f"**Category:** {describe_category(category)}\n\n" if category else ""
    polluter_line = f"**Polluter (runs immediately before the victim):** {polluter}\n\n" if polluter else ""

    return f"""You are an expert at fixing flaky tests in {language}. Your goal is to identify and fix the root cause of non-deterministic test behavior.

{best_practices_text}

## Test Analysis

{category_line}{polluter_line}**Simplified test for reference (to help you understand the structure):**
```{language}
{simplified_test_code}
```

**Original test to be fixed:**
```{language}
{original_test_code}
```

**Error messages:**
```
{assertion_failures}
```

**Stack trace:**
```
{error_trace}
```

**Additional context:**
```
{code_context}
```

## Common Flaky Test Patterns to Look For

1. **Timing Issues**: Race conditions, asynchronous work finishing in different orders, or tests relying on sleeps/timeouts
2. **Shared State**: Global/static variables, system properties, environment variables, files, databases, caches, or mocks shared between tests
3. **Non-deterministic Ordering**: Iteration over unordered collections, order-sensitive assertions, or assumptions about execution order
4. **Non-deterministic Algorithms**: Random number generation, UUID generation, generated IDs, or data structures with unstable ordering
5. **External Dependencies**: Network calls, file system behavior, time-dependent code, or resource cleanup issues

## Fixing Strategy

1. **Analyze the error**: Look for patterns indicating non-deterministic behavior
2. **Identify the root cause**: Timing, shared state, ordering, randomness, external dependency, etc.
3. **Apply appropriate fix**:
   - Make ordering deterministic before comparison
   - Use deterministic test data
   - Isolate or reset test state
   - Add proper synchronization
   - Mock or control non-deterministic dependencies

4. **Focus on the specific failing test case** rather than modifying shared setup
5. **Fix the test logic instead of the code under test, unless you strongly believe that the code under test is buggy.**

## High-Level Thought / Fixing Plan to Follow

Use the following thought as the plan for this concrete patch attempt:

```
{thought_text}
```

{output_format}

## Required Output

Please analyze the test failure and provide:

1. **The necessary fixes** using the SEARCH/REPLACE format above, note the search should be with respect to the original test function.
2. **A brief explanation** wrapped in EXPLANATION tags if using the combined format.
"""


def get_build_error_fix_prompt(
    defects_text: str,
    language: str,
    current_content: str,
    original_content_hint: str,
) -> str:
    """
    Generate a prompt for the compilation-retry step.

    Mirrors original FlakyGuard's build-error-fixing prompt
    (flakyguard/utils/fix_build_errors.py::get_prompt_for_fixing_build_errors):
    a previous flaky-test fix broke the build, so give the LLM the compiler
    diagnostics plus both the current (broken) file content and the original
    (pre-fix, previously-compiling) file content as reference, and ask for
    minimal SEARCH/REPLACE edits that resolve only the compilation errors.
    """
    return f"""You are an expert at fixing compilation errors in {language}. A previous edit made to fix a flaky test broke the build. Your goal is to make the minimum change necessary to resolve the compiler errors below, without undoing the intentional behavior change that fixed the flakiness.

## Compiler Errors To Fix

{defects_text}

{current_content}

{original_content_hint}

## Instructions

1. Analyze each compiler error and locate its cause in the current (broken) code shown above.
2. Fix missing/incorrect imports, syntax errors, type errors, or references to symbols that were renamed or removed by the previous edit.
3. If the original (pre-fix) code is shown above, use it only as a reference for what previously compiled — do not revert the behavior change that was made to address the flaky test, just repair the compile error.
4. Make minimal changes: only touch what is necessary to fix the compiler errors listed above.
5. The SEARCH text must match the CURRENT (broken) file content shown above, not the original.

{SEARCH_REPLACE_FORMAT}

## Required Output

Provide SEARCH/REPLACE edits that fix all compilation errors listed above, followed by a brief EXPLANATION.
"""
