"""
ask.py — two-call natural language query pipeline (rules 42-45).

Call 1: ROUTE  — model picks a tool and its params. No data access.
Call 2: PHRASE — model turns the returned rows into 2-3 sentences.
                 May only use numbers present in the rows it was given.

The model never touches the database. The model never generates SQL.
Every parameter is validated and clamped by the tool before execution.

Input guards (checked BEFORE any model call):
  - max 300 characters
  - strip control characters
  - reject obvious prompt-injection patterns
  - daily cap enforced via query_log count
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import edgedash.storage as storage
from edgedash.config import Config
from edgedash.llm import LLMError, complete_json
from edgedash.query.tools import TOOLS

# ---------------------------------------------------------------------------
# Answer type
# ---------------------------------------------------------------------------

@dataclass
class Answer:
    text:       str                          # phrased response (2-3 sentences)
    rows:       list[dict[str, Any]]         # raw data rows (always shown, rule 44)
    tool_used:  str | None                   # None if no tool matched
    params:     dict[str, Any]               # params actually passed to the tool
    summary:    str                          # what the tool looked at
    answerable: bool                         # False → question out of scope


# ---------------------------------------------------------------------------
# Input guards  (checked BEFORE any model call)
# ---------------------------------------------------------------------------

_MAX_CHARS = 300

# Patterns that suggest prompt injection or system manipulation attempts.
# Keep these simple — the goal is catching obvious abuse, not NLP.
_INJECTION_PATTERNS: list[re.Pattern] = [
    re.compile(r"ignore\s+(previous|prior|above|all)", re.I),
    re.compile(r"system\s+prompt", re.I),
    re.compile(r"you\s+are\s+now", re.I),
    re.compile(r"new\s+instruction", re.I),
    re.compile(r"disregard\s+(your|all|previous)", re.I),
    re.compile(r"(act|pretend|behave)\s+as\s+(if\s+)?(you\s+are|a\s+)", re.I),
    re.compile(r"jailbreak", re.I),
    re.compile(r"DAN\b", re.I),
]

# Control-character regex — keeps printable + standard whitespace
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass
class GuardResult:
    ok:     bool
    reason: str    # empty string when ok=True; rejection label when ok=False
    clean:  str    # sanitised input (only meaningful when ok=True)


def guard_input(raw: str) -> GuardResult:
    """
    Validate and sanitise a question string before it reaches the model.
    Returns GuardResult.ok=False with a rejection reason on any failure.
    The reason is logged but never shown to the user verbatim.
    """
    # 1. Strip control characters first
    clean = _CTRL_RE.sub("", raw).strip()

    # 2. Empty / whitespace-only
    if not clean:
        return GuardResult(ok=False, reason="rejected: empty input", clean="")

    # 3. Length cap
    if len(clean) > _MAX_CHARS:
        return GuardResult(
            ok=False,
            reason=f"rejected: input too long ({len(clean)} chars, max {_MAX_CHARS})",
            clean="",
        )

    # 4. Injection patterns — check AFTER cleaning
    for pattern in _INJECTION_PATTERNS:
        if pattern.search(clean):
            return GuardResult(
                ok=False,
                reason="rejected: suspicious input",
                clean="",
            )

    return GuardResult(ok=True, reason="", clean=clean)


# ---------------------------------------------------------------------------
# Daily question count  (global cap per config.daily_ask_limit)
# ---------------------------------------------------------------------------

def daily_ask_count(db_path: str) -> int:
    """Count questions asked today (UTC) from query_log."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT COUNT(*) FROM query_log WHERE asked_at >= ?",
            (today,),
        ).fetchone()
        conn.close()
        return row[0] if row else 0
    except Exception:
        return 0   # table may not exist yet — don't crash


# ---------------------------------------------------------------------------
# Routing prompt
# ---------------------------------------------------------------------------

def _build_tool_list() -> str:
    """Render the tool registry as a readable list for the router."""
    lines: list[str] = []
    for name, entry in TOOLS.items():
        param_parts = []
        for pname, spec in entry["params"].items():
            default = spec.get("default", "")
            ptype   = spec.get("type", "any")
            param_parts.append(f"{pname}: {ptype} = {default!r}")
        sig = f"{name}({', '.join(param_parts)})"
        lines.append(f"{sig}\n  {entry['description']}\n")
    return "\n".join(lines)


_ROUTE_SCHEMA = {
    "type": "object",
    "required": ["tool", "params", "confidence"],
    "properties": {
        "tool":       {"type": "string"},
        "params":     {"type": "object"},
        "confidence": {"type": "string"},
    },
}

_ROUTE_PROMPT_TEMPLATE = """\
You are a query router for a career intelligence database.

Your ONLY job is to select which tool answers the user's question and \
supply its parameters. You may NOT answer the question yourself.

AVAILABLE TOOLS:
{tool_list}

RULES:
- Select the tool whose description best matches the question.
- If no tool clearly matches, return null for "tool". Do NOT pick the \
closest-sounding tool and do NOT answer from general knowledge.
- Return null if the question asks for something not covered by any tool \
above (e.g. career advice, resume tips, general knowledge).
- For string parameters, extract the value from the question text exactly.
- For integer parameters, extract a number if stated, or use the default.

USER QUESTION: {question}

Return a JSON object with exactly these keys:
  tool       — the tool name (string) or null if no tool matches
  params     — object with the tool's parameters, or {{}} if tool is null
  confidence — "high" if you are certain this tool matches, "low" if unsure
"""


# ---------------------------------------------------------------------------
# Phrasing prompt
# ---------------------------------------------------------------------------

_PHRASE_SCHEMA = {
    "type": "object",
    "required": ["answer"],
    "properties": {
        "answer": {"type": "string"},
    },
}

_PHRASE_PROMPT_TEMPLATE = """\
You are summarising database query results for a user.

QUESTION: {question}

WHAT THE QUERY LOOKED AT: {summary}

DATA ROWS (these are the only facts you may use):
{rows_json}

RULES — you must follow these exactly:
- Write 2-3 sentences summarising what the data shows.
- Use ONLY numbers, names, and values that appear in the data rows above.
- Do NOT estimate, extrapolate, or add any information not present in the rows.
- Do NOT add career advice, general knowledge, or external context.
- If the data rows are empty, say plainly that the data does not contain \
an answer to this question.
- Reference the "what the query looked at" context so the user knows the scope \
(e.g. "across 47 listings from the last 7 days").

Return a JSON object with one key:
  answer — your 2-3 sentence summary (string)
"""


# ---------------------------------------------------------------------------
# No-match response  (no model call — fixed message, rule 45)
# ---------------------------------------------------------------------------

def _no_match_answer(question: str) -> Answer:
    tool_descriptions = "\n".join(
        f"  • {name}: {entry['description'].split('.')[0]}."
        for name, entry in TOOLS.items()
    )
    text = (
        f"I can't answer \"{question}\" — no tool covers that question.\n\n"
        f"Questions I can answer:\n{tool_descriptions}"
    )
    return Answer(
        text=text, rows=[], tool_used=None,
        params={}, summary="", answerable=False,
    )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def ask(question: str, config: Config) -> Answer:
    """
    Route the question to a tool, execute it, phrase the result.
    Logs every question to query_log regardless of outcome (including rejections).
    Guards are checked BEFORE any model call.
    """
    started = time.monotonic()
    tool_name: str | None = None
    params:    dict[str, Any] = {}
    answer:    Answer

    try:
        # ---- INPUT GUARD (no model call if this fails) --------------------
        guard = guard_input(question)
        if not guard.ok:
            answer = Answer(
                text=(
                    "I can't process that question. "
                    "Questions must be under 300 characters and ask about "
                    "your job search data."
                ),
                rows=[], tool_used=None, params={},
                summary="", answerable=False,
            )
            # Log the rejection with its reason
            storage.log_query(
                path=config.db_path,
                question=question[:500],   # cap stored length
                tool_chosen=None,
                params={},
                answerable=False,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            # Overwrite the notes with the rejection reason via a second log call
            # (simpler than adding an extra column — the reason is in question field)
            try:
                storage.log_query(
                    path=config.db_path,
                    question=guard.reason,
                    tool_chosen=None,
                    params={},
                    answerable=False,
                    duration_ms=0,
                )
            except Exception:
                pass
            return answer

        question = guard.clean  # use sanitised version from here on

        # ---- CALL 1: ROUTE ------------------------------------------------
        route_prompt = _ROUTE_PROMPT_TEMPLATE.format(
            tool_list=_build_tool_list(),
            question=question,
        )
        try:
            route_result = complete_json(route_prompt, _ROUTE_SCHEMA, config=config)
        except LLMError as exc:
            answer = Answer(
                text=f"Routing failed: {exc}", rows=[],
                tool_used=None, params={}, summary="", answerable=False,
            )
            return answer

        raw_tool = route_result.get("tool")
        raw_params = route_result.get("params") or {}

        # Null tool → out of scope
        if not raw_tool:
            answer = _no_match_answer(question)
            return answer

        # Validate tool name against registry (never getattr on model input)
        if raw_tool not in TOOLS:
            answer = Answer(
                text=(
                    f"The router returned an unknown tool name '{raw_tool}'. "
                    f"Valid tools: {', '.join(TOOLS)}."
                ),
                rows=[], tool_used=None, params={}, summary="", answerable=False,
            )
            return answer

        tool_name  = raw_tool
        params     = raw_params if isinstance(raw_params, dict) else {}

        # ---- CALL 2: EXECUTE ----------------------------------------------
        tool_fn = TOOLS[tool_name]["fn"]
        # Pass only params the tool actually accepts; extras are silently dropped.
        # Clamping and validation happen INSIDE the tool (rule 41).
        accepted = set(TOOLS[tool_name]["params"].keys())
        safe_params = {k: v for k, v in params.items() if k in accepted}

        exec_result = tool_fn(config, **safe_params)
        rows    = exec_result.get("rows", [])
        summary = exec_result.get("summary", "")

        # ---- CALL 3: PHRASE -----------------------------------------------
        phrase_prompt = _PHRASE_PROMPT_TEMPLATE.format(
            question=question,
            summary=summary,
            rows_json=json.dumps(rows[:20], indent=2),  # cap to avoid huge prompts
        )
        try:
            phrase_result = complete_json(phrase_prompt, _PHRASE_SCHEMA, config=config)
            text = phrase_result.get("answer", "").strip()
            if not text:
                text = summary  # fallback: at least say what was looked at
        except LLMError as exc:
            text = f"Data retrieved but phrasing failed: {exc}\n\n{summary}"

        answer = Answer(
            text=text, rows=rows, tool_used=tool_name,
            params=safe_params, summary=summary, answerable=True,
        )
        return answer

    finally:
        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            storage.log_query(
                path=config.db_path,
                question=question,
                tool_chosen=tool_name,
                params=params,
                answerable=answer.answerable if "answer" in dir() else False,
                duration_ms=duration_ms,
            )
        except Exception:
            pass  # never let logging kill the answer
