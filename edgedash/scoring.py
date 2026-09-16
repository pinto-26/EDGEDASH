"""
scoring.py — deterministic fit scoring. Pure functions only.

No model calls. No network. No imports from llm.py.
The model already ran in extractor.py and produced `facts`.
This module takes those facts and the user's config and does arithmetic.

Entry point:  score_listing(listing, facts, config) -> dict
Reason text:  build_reason(components, facts, config)  -> str  (rule 19)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from edgedash.config import Config

# ---------------------------------------------------------------------------
# Seniority band scale
# ---------------------------------------------------------------------------
# Ordered lowest-to-highest. Distance is abs(index_a - index_b).
# Exact match -> 1.0 | 1 band -> 0.6 | 2 bands -> 0.25 | 3+ bands -> 0.0

_SENIORITY_BANDS: list[str] = ["junior", "mid", "senior", "lead"]
_BAND_SCORES: dict[int, float] = {0: 1.0, 1: 0.6, 2: 0.25}


def _seniority_score(extracted: str, target: str) -> float:
    """Return 0.0–1.0 based on band distance between extracted and target."""
    e = extracted.lower().strip()
    t = target.lower().strip()
    if e == "unknown":
        return 0.5          # can't tell — give neutral benefit of the doubt
    try:
        dist = abs(_SENIORITY_BANDS.index(e) - _SENIORITY_BANDS.index(t))
    except ValueError:
        return 0.5          # unrecognised value — neutral
    return _BAND_SCORES.get(dist, 0.0)


# ---------------------------------------------------------------------------
# Skill match
# ---------------------------------------------------------------------------

def _skill_score(
    required: list[str],
    nice_to_have: list[str],
    my_skills: list[str],
) -> tuple[float, list[str], list[str]]:
    """
    Returns (score 0.0–1.0, matched_skills, missing_skills).

    required_skills each count as 1 point.
    nice_to_have each count as 1/3 point.
    Denominator = len(required) + len(nice_to_have) / 3.
    If both lists are empty, score is 1.0 (no bar to clear).
    """
    my_set = {s.lower().strip() for s in my_skills}
    req_set = {s.lower().strip() for s in required}
    nth_set = {s.lower().strip() for s in nice_to_have}

    if not req_set and not nth_set:
        return 1.0, [], []

    matched_req = req_set & my_set
    matched_nth = nth_set & my_set
    missing     = sorted(req_set - my_set)      # required skills I lack

    numerator   = len(matched_req) + len(matched_nth) / 3.0
    denominator = len(req_set)     + len(nth_set)     / 3.0

    score = numerator / denominator if denominator > 0 else 1.0
    return min(score, 1.0), sorted(matched_req | matched_nth), missing


# ---------------------------------------------------------------------------
# Location fit
# ---------------------------------------------------------------------------

def _location_score(remote_ok: bool | None, location: str | None, target_city: str) -> float:
    """
    remote_ok True              -> 1.0
    location matches target_city -> 1.0
    remote_ok None (unknown)    -> 0.5
    remote_ok False, elsewhere  -> 0.1
    """
    if remote_ok is True:
        return 1.0
    loc = (location or "").lower()
    if target_city.lower() in loc:
        return 1.0
    if remote_ok is None:
        return 0.5
    return 0.1      # remote_ok explicitly False and location doesn't match


# ---------------------------------------------------------------------------
# Recency
# ---------------------------------------------------------------------------

def _recency_score(posted_at: str | None) -> tuple[float, str]:
    """
    Linear decay: posted today -> 1.0, posted 30+ days ago -> 0.0.
    Returns (score, human_label) where label is e.g. "2d ago".
    posted_at None -> 0.5, labelled "date unknown".
    """
    if posted_at is None:
        return 0.5, "date unknown"

    try:
        # Handle both ISO strings and Unix timestamps
        if str(posted_at).lstrip("-").isdigit():
            then = datetime.fromtimestamp(int(posted_at), tz=timezone.utc)
        else:
            then = datetime.fromisoformat(str(posted_at))
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return 0.5, "date unknown"

    days = (datetime.now(timezone.utc) - then).days
    days = max(0, days)         # clamp — future dates treated as today

    if days == 0:
        label = "today"
    elif days == 1:
        label = "1d ago"
    else:
        label = f"{days}d ago"

    score = max(0.0, 1.0 - days / 30.0)
    return score, label


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def score_listing(
    listing: dict[str, Any],
    facts: dict[str, Any],
    config: Config,
) -> dict[str, Any]:
    """
    Compute a fit score for one listing given its extracted facts and config.

    Returns:
        {
            "score":      int 0–100,
            "reason":     str,
            "components": {
                "skill_match":   float,
                "seniority_fit": float,
                "location_fit":  float,
                "recency":       float,
            },
            "matched_skills": list[str],
            "missing_skills": list[str],
            "recency_label":  str,
        }
    """
    # --- Component calculations ---
    skill_raw, matched, missing = _skill_score(
        facts.get("required_skills") or [],
        facts.get("nice_to_have") or [],
        config.my_skills,
    )

    seniority_raw = _seniority_score(
        facts.get("seniority") or "unknown",
        config.target_seniority,
    )

    location_raw = _location_score(
        facts.get("remote_ok"),
        listing.get("location"),
        config.target_city,
    )

    recency_raw, recency_label = _recency_score(listing.get("posted_at"))

    components = {
        "skill_match":   round(skill_raw,   4),
        "seniority_fit": round(seniority_raw, 4),
        "location_fit":  round(location_raw, 4),
        "recency":       round(recency_raw,  4),
    }

    # --- Weighted sum ---
    weighted = (
        skill_raw     * config.weight_skill_match
        + seniority_raw * config.weight_seniority_fit
        + location_raw  * config.weight_location_fit
        + recency_raw   * config.weight_recency
    )
    score = max(0, min(100, round(weighted * 100)))

    reason = build_reason(components, facts, config, matched, missing, recency_label)

    return {
        "score":          score,
        "reason":         reason,
        "components":     components,
        "matched_skills": matched,
        "missing_skills": missing,
        "recency_label":  recency_label,
    }


# ---------------------------------------------------------------------------
# Reason builder  (rule 19 — generated from numbers, not written by model)
# ---------------------------------------------------------------------------

def build_reason(
    components: dict[str, float],
    facts: dict[str, Any],
    config: Config,
    matched_skills: list[str],
    missing_skills: list[str],
    recency_label: str,
) -> str:
    """
    Build a compact, human-readable reason string from score components.
    Every word is derived from the numbers — no model text.
    Example: "4/6 required skills · seniority fits · remote · posted 2d ago · gap: spark, kubernetes"
    """
    parts: list[str] = []

    # Skill match
    req = facts.get("required_skills") or []
    n_matched = len([s for s in matched_skills if s in {r.lower() for r in req}])
    n_req     = len(req)
    if n_req == 0:
        parts.append("no required skills listed")
    else:
        parts.append(f"{n_matched}/{n_req} required skills")

    # Seniority
    seniority = (facts.get("seniority") or "unknown").lower()
    target    = config.target_seniority.lower()
    fit       = components["seniority_fit"]
    if seniority == "unknown":
        parts.append("seniority unknown")
    elif fit == 1.0:
        parts.append("seniority fits")
    elif fit >= 0.6:
        parts.append(f"seniority close ({seniority})")
    else:
        parts.append(f"seniority mismatch ({seniority} vs {target})")

    # Location
    loc_fit = components["location_fit"]
    remote  = facts.get("remote_ok")
    if remote is True:
        parts.append("remote")
    elif loc_fit == 1.0:
        parts.append(f"in {config.target_city}")
    elif loc_fit == 0.5:
        parts.append("location unknown")
    else:
        parts.append("not remote / wrong city")

    # Recency
    parts.append(f"posted {recency_label}")

    # Gaps — most actionable part
    if missing_skills:
        gap_str = ", ".join(missing_skills[:5])   # cap at 5 for readability
        if len(missing_skills) > 5:
            gap_str += f" +{len(missing_skills) - 5} more"
        parts.append(f"gap: {gap_str}")

    return " · ".join(parts)
