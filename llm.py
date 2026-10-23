"""
utils/llm.py – thin LLM wrapper with exponential-backoff retries.

Supports OpenAI (default) and Anthropic.  Set environment variables:
  OPENAI_API_KEY   + FLAKYGUARD_LLM_PROVIDER=openai   (default)
  ANTHROPIC_API_KEY + FLAKYGUARD_LLM_PROVIDER=anthropic
  FLAKYGUARD_MODEL  – model name override (e.g. "gpt-4o", "claude-opus-4-5")
"""

from __future__ import annotations
import json
import os
import re
import time
import logging
import time

logger = logging.getLogger(__name__)

_PROVIDER = os.getenv("FLAKYGUARD_LLM_PROVIDER", "openai").lower()
_MODEL_DEFAULT = {
    "openai": "gpt-5.4",
    #"openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-6",
    #"anthropic": "claude-sonnet-4-6"
}
MODEL = os.getenv("FLAKYGUARD_MODEL", _MODEL_DEFAULT.get(_PROVIDER, "gpt-4o"))
MAX_RETRIES = 5

TOKEN_USAGE_ROWS = []

def consume_token_usage():
    global TOKEN_USAGE_ROWS
    rows = TOKEN_USAGE_ROWS
    TOKEN_USAGE_ROWS = []
    return rows


def token_usage_mark() -> int:
    """
    Watermark into TOKEN_USAGE_ROWS for later attribution via
    token_usage_since(), without draining it the way consume_token_usage()
    does. Lets a caller (e.g. one M×P×N candidate attempt) find out how much
    token usage its own LLM calls accounted for, while leaving the full
    history intact for the end-of-run consume_token_usage() call.
    """
    return len(TOKEN_USAGE_ROWS)


def token_usage_since(mark: int) -> list[dict]:
    """Token-usage rows recorded since `mark` (see token_usage_mark())."""
    return TOKEN_USAGE_ROWS[mark:]

# def _call_openai(messages: list[dict], temperature: float, model: str) -> str:
#     import openai
#     client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
#     kwargs: dict = dict(model=model, messages=messages)
#     if not model.startswith("o1"):          # o1 models reject temperature
#         kwargs["temperature"] = temperature
#     resp = client.chat.completions.create(**kwargs)

#     # TOKEN_USAGE_ROWS.append({
#     # "llm_provider": "anthropic",
#     #     "llm_model": model,
#     #     "llm_input_tokens": resp.usage.input_tokens,
#     #     "llm_output_tokens": resp.usage.output_tokens,
#     #     "llm_total_tokens": resp.usage.input_tokens + resp.usage.output_tokens,
#     # })
#     return resp.choices[0].message.content or ""


def _call_openai(messages: list[dict], temperature: float, model: str) -> str:
    import openai

    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    kwargs: dict = dict(
        model=model,
        messages=messages,
    )

    if not model.startswith("o1"):
        kwargs["temperature"] = temperature

    start_time = time.time()

    resp = client.chat.completions.create(**kwargs)

    elapsed_seconds = time.time() - start_time

    TOKEN_USAGE_ROWS.append({
        "llm_provider": "openai",
        "llm_model": model,
        "llm_input_tokens": resp.usage.prompt_tokens,
        "llm_output_tokens": resp.usage.completion_tokens,
        "llm_total_tokens": resp.usage.total_tokens,
        "llm_elapsed_seconds": elapsed_seconds,
    })

    return resp.choices[0].message.content or ""


def _call_anthropic(messages: list[dict], temperature: float, model: str) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    # Anthropic expects system separate from messages
    system = ""
    chat: list[dict] = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            chat.append(m)

    start_time = time.time()

    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system,
        messages=chat,
        temperature=temperature,
    )

    elapsed_seconds = time.time() - start_time

    TOKEN_USAGE_ROWS.append({
        "llm_provider": "anthropic",
        "llm_model": model,
        "llm_input_tokens": resp.usage.input_tokens,
        "llm_output_tokens": resp.usage.output_tokens,
        "llm_total_tokens": resp.usage.input_tokens + resp.usage.output_tokens,
        "llm_elapsed_seconds": elapsed_seconds,

    })
    return resp.content[0].text


_SAFE_TAG_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def _save_llm_call_log(
    log_dir: str,
    call_index: int,
    tag: str,
    provider: str,
    model: str,
    temperature: float,
    system: str,
    prompt: str,
    response: str,
    usage_row: dict,
) -> None:
    """
    Persist one LLM call's full prompt + response, paired into one file per
    call, since this codebase makes several distinct kinds of LLM calls
    (context selection, thought, fix, build-repair) rather than one
    tool-using agent loop.

    Every call to complete() lands here when a caller opts in via log_dir,
    so this is the one place this needs to be implemented.
    """
    try:
        os.makedirs(log_dir, exist_ok=True)
        safe_tag = _SAFE_TAG_RE.sub("_", tag)[:80] or "call"
        path = os.path.join(log_dir, f"{call_index:04d}_{safe_tag}.json")
        record = {
            "call_index": call_index,
            "tag": tag,
            "provider": provider,
            "model": model,
            "temperature": temperature,
            "system": system,
            "prompt": prompt,
            "response": response,
            "input_tokens": usage_row.get("llm_input_tokens"),
            "output_tokens": usage_row.get("llm_output_tokens"),
            "elapsed_seconds": usage_row.get("llm_elapsed_seconds"),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
    except OSError as exc:
        # Never let logging failures break the actual LLM call.
        logger.warning("Could not write LLM call log for tag=%s: %s", tag, exc)


def complete(
    prompt: str,
    system: str = "You are an expert software engineer.",
    temperature: float = 0.0,
    model: str = MODEL,
    log_dir: str | None = None,
    tag: str = "call",
) -> str:
    """
    Send a single user prompt to the configured LLM and return the text reply.
    Retries with exponential back-off on transient errors.

    When log_dir is given, the full prompt + response (plus token/timing
    metadata) for the call that actually succeeds is written to
    <log_dir>/<NNNN>_<tag>.json, numbered by its position in
    TOKEN_USAGE_ROWS so it lines up with the corresponding token-usage row.
    `tag` should identify the calling step (e.g. "M1_P2_N1_fix") so the
    files are self-describing; it need not be unique since the numeric
    prefix already guarantees that.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": prompt},
    ]
    call = _call_anthropic if _PROVIDER == "anthropic" else _call_openai

    for attempt in range(MAX_RETRIES):
        try:
            response = call(messages, temperature, model)
            if log_dir:
                _save_llm_call_log(
                    log_dir=log_dir,
                    call_index=len(TOKEN_USAGE_ROWS),
                    tag=tag,
                    provider=_PROVIDER,
                    model=model,
                    temperature=temperature,
                    system=system,
                    prompt=prompt,
                    response=response,
                    usage_row=TOKEN_USAGE_ROWS[-1] if TOKEN_USAGE_ROWS else {},
                )
            return response
        except Exception as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = 2 ** (attempt + 1)
            logger.warning("LLM call failed (%s). Retrying in %ds…", exc, wait)
            time.sleep(wait)

    return ""  # unreachable
