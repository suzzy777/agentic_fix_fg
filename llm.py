"""
utils/llm.py – thin LLM wrapper with exponential-backoff retries.

Supports OpenAI (default) and Anthropic.  Set environment variables:
  OPENAI_API_KEY   + FLAKYGUARD_LLM_PROVIDER=openai   (default)
  ANTHROPIC_API_KEY + FLAKYGUARD_LLM_PROVIDER=anthropic
  FLAKYGUARD_MODEL  – model name override (e.g. "gpt-4o", "claude-opus-4-5")
"""

from __future__ import annotations
import os
import time
import logging
import time

logger = logging.getLogger(__name__)

_PROVIDER = os.getenv("FLAKYGUARD_LLM_PROVIDER", "openai").lower()
_MODEL_DEFAULT = {
    "openai": "gpt-4o-mini",
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

def _call_openai(messages: list[dict], temperature: float, model: str) -> str:
    import openai
    client = openai.OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    kwargs: dict = dict(model=model, messages=messages)
    if not model.startswith("o1"):          # o1 models reject temperature
        kwargs["temperature"] = temperature
    resp = client.chat.completions.create(**kwargs)

    # TOKEN_USAGE_ROWS.append({
    # "llm_provider": "anthropic",
    #     "llm_model": model,
    #     "llm_input_tokens": resp.usage.input_tokens,
    #     "llm_output_tokens": resp.usage.output_tokens,
    #     "llm_total_tokens": resp.usage.input_tokens + resp.usage.output_tokens,
    # })
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


def complete(
    prompt: str,
    system: str = "You are an expert software engineer.",
    temperature: float = 0.0,
    model: str = MODEL,
) -> str:
    """
    Send a single user prompt to the configured LLM and return the text reply.
    Retries with exponential back-off on transient errors.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user",   "content": prompt},
    ]
    call = _call_anthropic if _PROVIDER == "anthropic" else _call_openai

    for attempt in range(MAX_RETRIES):
        try:
            return call(messages, temperature, model)
        except Exception as exc:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = 2 ** (attempt + 1)
            logger.warning("LLM call failed (%s). Retrying in %ds…", exc, wait)
            time.sleep(wait)

    return ""  # unreachable
