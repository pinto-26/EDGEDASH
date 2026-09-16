"""
extractor.py — extracts structured facts from a job description.

This is the ONLY part of the scoring pipeline that calls the LLM.
It extracts facts; it never scores. The model does not know a candidate
exists and never sees scoring weights (steering rule 16).

Cache: results are keyed on a SHA-256 hash of the description text.
The same description is never sent to the model twice (steering rule 18).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import edgedash.storage as storage
from edgedash.config import Config
from edgedash.llm import LLMError, complete_json

# ---------------------------------------------------------------------------
# Extraction schema
# ---------------------------------------------------------------------------
# Rule 16: there is no score field here and there must never be one.

EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "required_skills",
        "nice_to_have",
        "seniority",
        "years_required",
        "remote_ok",
    ],
    "properties": {
        "required_skills": {
            "type": "array",
            "items": {"type": "string"},
        },
        "nice_to_have": {
            "type": "array",
            "items": {"type": "string"},
        },
        "seniority": {
            "type": "string",
        },
        "years_required": {
            "type": "integer",
        },
        "remote_ok": {
            "type": "boolean",
        },
    },
}

_VALID_SENIORITY = {"junior", "mid", "senior", "lead", "unknown"}

# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
You are a precise document parser. Read the job listing below and extract \
structured facts. Follow these rules exactly:

- Only extract what the listing explicitly states. Do not infer, guess, \
or assume anything.
- Do not evaluate any candidate. You do not know who is applying.
- If a field is not stated in the listing, use null (for years_required \
and remote_ok) or an empty list (for skill lists).
- For seniority: choose the closest match from \
["junior", "mid", "senior", "lead"] based on explicit words in the \
listing. If none match, use "unknown".
- For years_required: extract the minimum years stated. If a range is \
given (e.g. "3-5 years"), use the lower bound. If not stated, use null.
- For remote_ok: true if the listing explicitly mentions remote work is \
allowed or it is a remote role. false if the listing explicitly says \
on-site only or no remote. null if neither is stated.

Return a JSON object with exactly these keys:
  required_skills  (array of strings)
  nice_to_have     (array of strings)
  seniority        ("junior" | "mid" | "senior" | "lead" | "unknown")
  years_required   (integer or null)
  remote_ok        (boolean or null)

JOB LISTING:
{description}"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _description_hash(text: str) -> str:
    """Stable SHA-256 hash of the description text — used as cache key."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalise(extracted: dict[str, Any]) -> dict[str, Any]:
    """
    Lowercase all skill names and clamp seniority to the allowed set.
    Ensures downstream scoring comparisons work regardless of model casing.
    """
    result = dict(extracted)
    result["required_skills"] = [
        s.lower().strip() for s in (result.get("required_skills") or [])
    ]
    result["nice_to_have"] = [
        s.lower().strip() for s in (result.get("nice_to_have") or [])
    ]
    seniority = (result.get("seniority") or "unknown").lower().strip()
    result["seniority"] = seniority if seniority in _VALID_SENIORITY else "unknown"
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract(
    listing: dict[str, Any],
    config: Config,
    db_path: str,
    strict_required: bool = False,
) -> dict[str, Any]:
    """
    Extract structured facts from a job listing's description.

    strict_required=True: appends a clarification to the prompt asking the
    model to only include skills explicitly stated as mandatory. Used on
    retry when verification detects score inflation — narrowing required_skills
    forces more differentiation between listings in the scoring step.

    Returns a normalised dict matching EXTRACTION_SCHEMA.
    On a cache hit, no model call is made (strict_required bypasses cache
    on retry so a fresh extraction is attempted with the new instruction).
    Raises LLMError if the model fails after retries — callers handle
    per-listing per steering rule 17.
    """
    description = (listing.get("description") or "").strip()
    if not description:
        return {
            "required_skills": [],
            "nice_to_have": [],
            "seniority": "unknown",
            "years_required": None,
            "remote_ok": None,
        }

    desc_hash = _description_hash(description)

    # Cache check — skip on strict_required retry so fresh extraction runs
    if not strict_required:
        cached = storage.get_extraction(db_path, desc_hash)
        if cached is not None:
            return cached

    # Model call
    prompt = _PROMPT_TEMPLATE.format(description=description)
    if strict_required:
        prompt += (
            "\n\nIMPORTANT: For required_skills, only include skills the listing "
            "explicitly states are mandatory or required. Do NOT include skills that "
            "are merely implied, common in the field, or listed as optional. "
            "Err on the side of a shorter list."
        )
    raw = complete_json(prompt, EXTRACTION_SCHEMA, config=config)
    normalised = _normalise(raw)

    # Only cache the normal (non-strict) extraction
    if not strict_required:
        storage.set_extraction(db_path, desc_hash, normalised)

    return normalised
