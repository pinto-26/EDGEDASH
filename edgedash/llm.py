"""
llm.py — the ONLY module in EdgeDash that calls a language model.

Public API:
    complete_json(prompt, schema, *, max_retries=1) -> dict

Providers are registered with @_register_provider. Adding a third provider
means adding one function and one decorator — complete_json never changes.

Rate limits (steering rule 15):
    - Minimum 1 second between any two calls.
    - Rolling cap of 15 calls per 60 seconds. Sleeps until the window clears.

Validation (steering rule 17):
    - Every response is validated against the caller-supplied JSON schema.
    - One retry on failure, with the exact error appended to the prompt.
    - LLMError is raised on final failure — callers handle per-listing.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Load .env on import so GEMINI_API_KEY is available before any provider init
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE   = _REPO_ROOT / ".env"

if _ENV_FILE.exists():
    with _ENV_FILE.open() as _fh:
        for _line in _fh:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LLMError(Exception):
    """Raised when the LLM fails unrecoverably for a single call."""


# ---------------------------------------------------------------------------
# Rate limiter  (steering rule 15)
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Enforces: ≥4 s between calls and ≤12 calls per rolling 60 s window.
    Gemini free tier is 15 RPM but bursting to that triggers 429s in practice.
    4s gap gives ~15 RPM theoretical but ~12 RPM effective with retry headroom."""

    def __init__(self, min_gap: float = 4.0, max_per_minute: int = 12) -> None:
        self._min_gap       = min_gap
        self._max_per_minute = max_per_minute
        self._last_call: float = 0.0
        self._window: deque[float] = deque()

    def wait(self) -> None:
        now = time.monotonic()

        # Enforce minimum gap between calls
        gap = now - self._last_call
        if gap < self._min_gap:
            time.sleep(self._min_gap - gap)
            now = time.monotonic()

        # Enforce rolling 60-second cap
        cutoff = now - 60.0
        while self._window and self._window[0] < cutoff:
            self._window.popleft()

        if len(self._window) >= self._max_per_minute:
            oldest = self._window[0]
            sleep_for = (oldest + 60.0) - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            # Purge again after sleeping
            cutoff = now - 60.0
            while self._window and self._window[0] < cutoff:
                self._window.popleft()

        self._window.append(now)
        self._last_call = now


_rate_limiter = _RateLimiter()


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

# Signature: (prompt: str, model: str) -> str
_ProviderFn = Callable[[str, str], str]
_PROVIDERS: dict[str, _ProviderFn] = {}


def _register_provider(name: str) -> Callable[[_ProviderFn], _ProviderFn]:
    def decorator(fn: _ProviderFn) -> _ProviderFn:
        _PROVIDERS[name] = fn
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Gemini provider
# ---------------------------------------------------------------------------

@_register_provider("gemini")
def _call_gemini(prompt: str, model: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise LLMError(
            "GEMINI_API_KEY is not set.\n"
            "Add it to your .env file (see .env.example) or export it in your shell.\n"
            "Get a free key at: https://aistudio.google.com/app/apikey"
        )

    from google import genai                    # noqa: PLC0415
    from google.genai import types              # noqa: PLC0415
    import warnings                             # noqa: PLC0415
    # Silence the AFC advisory — we never use function calling
    warnings.filterwarnings("ignore", message=".*AFC.*")
    warnings.filterwarnings("ignore", message=".*automatic function calling.*")

    client = genai.Client(api_key=api_key)

    backoff = 2.0
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            )
            return response.text
        except Exception as exc:
            msg = str(exc).lower()
            is_quota = "429" in msg or "quota" in msg or "rate" in msg
            if is_quota and attempt < 2:
                time.sleep(backoff)
                backoff *= 2
                continue
            raise LLMError(f"Gemini call failed: {exc}") from exc

    raise LLMError("Gemini: exhausted retries on quota/rate error.")


# ---------------------------------------------------------------------------
# Ollama provider  (local, no key required)
# ---------------------------------------------------------------------------

@_register_provider("ollama")
def _call_ollama(prompt: str, model: str) -> str:
    import urllib.request  # standard library — no extra dependency

    payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode())
            return body["response"]
    except Exception as exc:
        raise LLMError(
            f"Ollama call failed. Is Ollama running? (http://localhost:11434)\n"
            f"Error: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# JSON cleaning
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json(text: str) -> str:
    """Strip markdown fences and surrounding prose, return the raw JSON string."""
    # Try to find a fenced block first
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    # No fence — try to find the outermost { } or [ ]
    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = text.find(start_char)
        end   = text.rfind(end_char)
        if start != -1 and end != -1 and end > start:
            return text[start : end + 1]
    return text.strip()


# ---------------------------------------------------------------------------
# Schema validation  (lightweight — no jsonschema dependency)
# ---------------------------------------------------------------------------

def _validate(data: Any, schema: dict) -> None:
    """
    Validate data against a simplified JSON-schema subset.
    Supports: type, properties, required, items.
    None values (JSON null) are always allowed — callers declare nullability
    via the schema structure, not by excluding None here.
    Raises ValueError with a descriptive message on failure.
    """
    # JSON null is valid for any field — never reject None
    if data is None:
        return

    expected_type = schema.get("type")
    if expected_type:
        type_map = {
            "object": dict, "array": list, "string": str,
            "integer": int, "number": (int, float), "boolean": bool,
        }
        py_type = type_map.get(expected_type)
        if py_type and not isinstance(data, py_type):
            raise ValueError(
                f"Expected type '{expected_type}', got '{type(data).__name__}'"
            )

    if expected_type == "object":
        required = schema.get("required", [])
        for key in required:
            if key not in data:
                raise ValueError(f"Missing required field: '{key}'")
        props = schema.get("properties", {})
        for key, sub_schema in props.items():
            if key in data:
                _validate(data[key], sub_schema)

    if expected_type == "array":
        items_schema = schema.get("items")
        if items_schema:
            for i, item in enumerate(data):
                try:
                    _validate(item, items_schema)
                except ValueError as exc:
                    raise ValueError(f"Item {i}: {exc}") from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def complete_json(
    prompt: str,
    schema: dict,
    *,
    config: Any = None,          # accepts a Config; resolved lazily if None
    max_retries: int = 1,
) -> dict:
    """
    Send prompt to the configured LLM, parse the response as JSON,
    validate it against schema, and return the dict.

    Retries once on parse/validation failure with the error appended.
    Raises LLMError on final failure — never swallows silently.
    """
    if config is None:
        from edgedash.config import load_config  # lazy to avoid circular import
        config = load_config()

    provider_name = config.llm_provider
    model         = config.llm_model
    provider_fn   = _PROVIDERS.get(provider_name)

    if provider_fn is None:
        raise LLMError(
            f"Unknown llm_provider '{provider_name}'. "
            f"Available: {list(_PROVIDERS)}"
        )

    json_instruction = (
        "\n\nRespond with valid JSON only. "
        "No markdown, no code fences, no prose before or after the JSON."
    )
    current_prompt = prompt + json_instruction
    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        _rate_limiter.wait()
        try:
            raw_text  = provider_fn(current_prompt, model)
            json_str  = _extract_json(raw_text)
            parsed    = json.loads(json_str)
            _validate(parsed, schema)
            return parsed

        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt < max_retries:
                current_prompt = (
                    f"{prompt}\n\n"
                    f"Your previous response was invalid: {exc}\n"
                    "You MUST reply with valid JSON only — no prose, no markdown fences."
                )
                continue
            # Final failure
            raise LLMError(
                f"LLM response failed validation after {attempt + 1} attempt(s).\n"
                f"Last error: {exc}"
            ) from exc

        except LLMError:
            raise  # propagate immediately, no retry

    raise LLMError(f"complete_json exhausted attempts. Last error: {last_error}")


# ---------------------------------------------------------------------------
# CLI check: python -m edgedash.llm --check
# ---------------------------------------------------------------------------

def _run_check() -> None:
    from edgedash.config import load_config

    cfg = load_config()
    print(f"Provider : {cfg.llm_provider}")
    print(f"Model    : {cfg.llm_model}")
    print("Sending test prompt ...", flush=True)

    test_schema = {
        "type": "object",
        "required": ["status"],
        "properties": {"status": {"type": "string"}},
    }
    try:
        result = complete_json(
            'Reply with exactly: {"status": "ok"}',
            test_schema,
            config=cfg,
        )
        print(f"Response : {result}")
        print("✓  LLM connection working.")
    except LLMError as exc:
        print(f"✗  LLM check failed: {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Send a test prompt.")
    args = parser.parse_args()
    if args.check:
        _run_check()
    else:
        parser.print_help()
