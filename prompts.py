from __future__ import annotations


DEFAULT_BEST_PRACTICES = """\
## Guidelines for Repairing Flaky Tests

### Keeping the code healthy
- Match the conventions and idioms already used in the surrounding code.
- Reach for helpers and libraries the project already depends on.
- Cover edge cases and handle errors rather than ignoring them.
- Give variables and functions names that explain themselves.
- Comment only where the intent would otherwise be unclear.

### Keeping the tests reliable
- Favour deterministic constructs over anything time- or order-sensitive.
- Keep each test self-contained; never lean on shared or leftover state.
- Release whatever the test acquires, in setup/teardown as appropriate.
- Inject or mock anything external so its behaviour is under your control.
"""


SEARCH_REPLACE_FORMAT = """\
## Output Contract

Emit your edits as one or more SEARCH/REPLACE blocks. Each block is laid out
exactly like this:

- Line 1: the complete file path on its own, raw text only (no quoting, no
  bold, no escaping).
- Line 2: an opening fence with the language tag, e.g. ```java
- Line 3: the search marker `<<<<<<< SEARCH`
- Then: the exact existing lines you want to locate.
- Then: the divider `=======`
- Then: the replacement lines.
- Then: the replace marker `>>>>>>> REPLACE`
- Last line: the closing fence ```

Worked example:

src/test/java/MyTest.java
```java
<<<<<<< SEARCH
    assertEquals(expected, result);
=======
    assertEquals(expected, actualResult);
>>>>>>> REPLACE
```

Rules to respect:
- The SEARCH region must reproduce the current file byte for byte — same
  comments, whitespace, indentation and line breaks.
- Only the first match of each SEARCH region is rewritten.
- Split unrelated changes across separate blocks; keep each block small and
  give it enough surrounding lines to match uniquely.
- Never use ellipses (..., [...]) or partial code inside a SEARCH region.
- Only patch files that appear in the provided context.
- Edit the test rather than production code unless the production code is
  clearly at fault.
- Put no prose before the blocks.

## Explanation Contract

Once the blocks are done, add a short note inside EXPLANATION tags:

```
<EXPLANATION>
One or two sentences on the cause of the flakiness and how the patch removes it.
</EXPLANATION>
```
"""


# Shared evidence block so the two stages stay in sync.
_EVIDENCE_TEMPLATE = """\
## Evidence

**Reduced test (reference only, to show the shape of the test):**
```{language}
{simplified_test_code}
```

**Test to repair (edit this one):**
```{language}
{original_test_code}
```

**Failure messages:**
```
{assertion_failures}
```

**Stack trace:**
```
{error_trace}
```

**Surrounding context:**
```
{code_context}
```"""


_FLAKY_CATEGORIES = """\
## Flaky Categories to Consider

- Timing, races, sleeps/timeouts, or async work completing in a different order.
- Leaked or shared mutable state: globals, statics, system properties, env vars,
  files, DBs, caches, or mocks carried between tests.
- Order-sensitive assertions or iteration over unordered collections.
- Randomness: RNGs, UUIDs, generated identifiers, or unstable hash ordering.
- Other factors: filesystem, network, wall-clock time, locale, or environment.
- Missing cleanup or isolation between tests."""


def get_flaky_test_thought_prompt(
    *,
    simplified_test_code: str, original_test_code: str,
    assertion_failures: str, error_trace: str, code_context: str,
    language: str,
    context_attempt: int = 1, thought_attempt: int = 1, total_thoughts: int = 1,
    best_practices: str | None = None, failed_thoughts: list[str] | None = None,
) -> str:
    """
    Stage 1 prompt: ask for a diagnosis and a repair plan, but no code edits yet.

    The reply should name the root-cause category, justify why the failure is
    non-deterministic, and sketch a minimal, safe fixing plan.
    """
    practices = best_practices or DEFAULT_BEST_PRACTICES

    prior_section = ""
    if failed_thoughts:
        prior = "\n\n".join(
            f"Rejected plan {n + 1}:\n{body}"
            for n, body in enumerate(failed_thoughts)
        )
        prior_section = f"""
## Plans Already Tried Without Success

These strategies did not yield a working fix:

{prior}

Do not restate the same root cause or plan — offer a materially different one.
"""

    evidence = _EVIDENCE_TEMPLATE.format(
        language=language,
        simplified_test_code=simplified_test_code,
        original_test_code=original_test_code,
        assertion_failures=assertion_failures,
        error_trace=error_trace,
        code_context=code_context,
    )

    return f"""You are a specialist in diagnosing flaky {language} tests.
You are on thought {thought_attempt} of {total_thoughts} for context round {context_attempt}.

Produce only a high-level diagnosis and repair plan. Do NOT write SEARCH/REPLACE edits at this stage.

{practices}

{evidence}

{_FLAKY_CATEGORIES}

{prior_section}

## Deliverable

Return one plan, formatted exactly as:

<THOUGHT>
Root-cause category: ...
Root-cause explanation: ...
Fixing plan: ...
Why this plan is minimal and safe: ...
</THOUGHT>
"""


def render_flaky_repair_prompt(
    *,
    simplified_test_code: str, original_test_code: str,
    assertion_failures: str, error_trace: str, code_context: str,
    language: str, output_format: str,
    thought: str | None = None, best_practices: str | None = None,
) -> str:
    """
    Stage 2 prompt: turn the diagnosis into a concrete SEARCH/REPLACE patch.

    Supplies the same evidence block as stage 1, plus a checklist of common
    failure modes, the plan to follow, and the required output contract.
    """
    practices = best_practices or DEFAULT_BEST_PRACTICES
    plan = thought or (
        "No explicit plan was supplied. Work out the most likely root cause "
        "and a minimal fixing plan before you edit."
    )

    evidence = _EVIDENCE_TEMPLATE.format(
        language=language,
        simplified_test_code=simplified_test_code,
        original_test_code=original_test_code,
        assertion_failures=assertion_failures,
        error_trace=error_trace,
        code_context=code_context,
    )

    return f"""You are a specialist in repairing flaky {language} tests. Find the root cause of the non-deterministic behaviour and eliminate it.

{practices}

{evidence}

{_FLAKY_CATEGORIES}

## How to Fix

1. Read the failure and spot the non-deterministic signal.
2. Pin down the category (timing, state, ordering, randomness, external, ...).
3. Apply a targeted fix, e.g.:
   - sort/normalise before comparing,
   - use fixed, deterministic data,
   - reset or isolate state,
   - add the synchronisation that is missing,
   - mock or pin the non-deterministic dependency.
4. Change the specific failing case rather than shared setup where you can.
5. Prefer fixing the test over the code under test, unless the code under test
   is genuinely buggy.

## Plan to Follow

Treat the following as the plan for this patch:

```
{plan}
```

{output_format}

## Deliverable

Work through the failure and return:

1. The edits, in the SEARCH/REPLACE format above - search against the original
   test function.
2. A short note in EXPLANATION tags when using the combined format.
"""
