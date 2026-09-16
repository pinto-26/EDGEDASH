"""
verification.py — plausibility checks on agent output. Deterministic. No LLM.

A model cannot be the judge of a model's output (rule 34).
Checks assert properties of distributions and shapes — never single-value
correctness (rule 35). All thresholds come from config (rule 39).

Every function is pure: no clock, no network, no database reads.
`now` is always a parameter, never datetime.now() inside.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from edgedash.config import Config


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    name:      str
    passed:    bool
    observed:  str    # the actual value seen, e.g. "spread=4"
    threshold: str    # the limit it was tested against, e.g. "min_spread=10"
    message:   str    # human-readable verdict


@dataclass
class Verdict:
    passed:        bool
    failed_checks: list[CheckResult] = field(default_factory=list)
    all_checks:    list[CheckResult] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if self.passed:
            return f"OK — all {len(self.all_checks)} checks passed"
        names = ", ".join(c.name for c in self.failed_checks)
        return f"FAILED — {len(self.failed_checks)}/{len(self.all_checks)} checks failed: {names}"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _stdev(values: list[float]) -> float:
    """Population standard deviation. Returns 0.0 for fewer than 2 values."""
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((x - mean) ** 2 for x in values) / n)


# ---------------------------------------------------------------------------
# Check 1: score spread
# ---------------------------------------------------------------------------

def check_score_spread(scores: list[int], config: Config) -> CheckResult:
    """
    Catches score inflation: all scores clustering in a narrow band.
    Passes trivially if fewer than 5 scores — not enough data to judge.
    Fails if spread < min_score_spread OR stdev < min_score_stdev.
    """
    name = "score_spread"

    if len(scores) < 5:
        return CheckResult(
            name=name,
            passed=True,
            observed=f"n={len(scores)}",
            threshold=f"min_n=5",
            message=f"Skipped — only {len(scores)} score(s); need at least 5 to check spread.",
        )

    spread  = max(scores) - min(scores)
    stdev   = _stdev([float(s) for s in scores])
    lo, hi  = min(scores), max(scores)

    if spread < config.min_score_spread:
        return CheckResult(
            name=name,
            passed=False,
            observed=f"spread={spread} (scores {lo}–{hi})",
            threshold=f"min_score_spread={config.min_score_spread}",
            message=(
                f"Score spread {spread} is below minimum {config.min_score_spread}. "
                f"All {len(scores)} scores fall between {lo} and {hi}. "
                f"Possible inflation or extractor collapse."
            ),
        )

    if stdev < config.min_score_stdev:
        return CheckResult(
            name=name,
            passed=False,
            observed=f"stdev={stdev:.2f} (spread={spread})",
            threshold=f"min_score_stdev={config.min_score_stdev}",
            message=(
                f"Score stdev {stdev:.2f} is below minimum {config.min_score_stdev}. "
                f"Spread is {spread} but scores are tightly clustered. "
                f"Possible inflation."
            ),
        )

    return CheckResult(
        name=name,
        passed=True,
        observed=f"spread={spread}, stdev={stdev:.2f}",
        threshold=f"min_spread={config.min_score_spread}, min_stdev={config.min_score_stdev}",
        message=f"Score distribution OK — spread={spread}, stdev={stdev:.2f} across {len(scores)} scores.",
    )


# ---------------------------------------------------------------------------
# Check 2: extraction sanity
# ---------------------------------------------------------------------------

def check_extraction_sanity(
    facts_list: list[dict[str, Any]],
    config: Config,
) -> CheckResult:
    """
    Two sub-checks:
    1. Empty required_skills rate > max_empty_extraction_pct → extractor silently failing.
    2. Any listing with > max_skills_per_listing → model dumped a sentence as skill list.
    """
    name = "extraction_sanity"

    if not facts_list:
        return CheckResult(
            name=name,
            passed=True,
            observed="n=0",
            threshold="n/a",
            message="No extractions to check.",
        )

    n = len(facts_list)

    # Sub-check 1: empty required_skills rate
    empty_count = sum(
        1 for f in facts_list
        if not (f.get("required_skills") or [])
    )
    empty_pct = (empty_count / n) * 100.0

    if empty_pct > config.max_empty_extraction_pct:
        return CheckResult(
            name=name,
            passed=False,
            observed=f"empty_pct={empty_pct:.1f}% ({empty_count}/{n})",
            threshold=f"max_empty_extraction_pct={config.max_empty_extraction_pct}%",
            message=(
                f"{empty_count} of {n} extractions ({empty_pct:.1f}%) have empty "
                f"required_skills. Exceeds max {config.max_empty_extraction_pct}%. "
                f"Possible extractor failure."
            ),
        )

    # Sub-check 2: bloated skill lists
    bloated = [
        (i, len(f.get("required_skills") or []))
        for i, f in enumerate(facts_list)
        if len(f.get("required_skills") or []) > config.max_skills_per_listing
    ]

    if bloated:
        worst_idx, worst_count = max(bloated, key=lambda x: x[1])
        return CheckResult(
            name=name,
            passed=False,
            observed=f"max_skills={worst_count} (listing index {worst_idx})",
            threshold=f"max_skills_per_listing={config.max_skills_per_listing}",
            message=(
                f"{len(bloated)} listing(s) have more than {config.max_skills_per_listing} "
                f"required skills. Worst: {worst_count} skills. "
                f"Model likely returned a sentence or paragraph as a skill list."
            ),
        )

    return CheckResult(
        name=name,
        passed=True,
        observed=f"empty_pct={empty_pct:.1f}%, max_skills={max((len(f.get('required_skills') or []) for f in facts_list), default=0)}",
        threshold=f"max_empty={config.max_empty_extraction_pct}%, max_skills={config.max_skills_per_listing}",
        message=f"Extraction sanity OK — {n} extractions checked.",
    )


# ---------------------------------------------------------------------------
# Check 3: gap sample size
# ---------------------------------------------------------------------------

def check_gap_sample_size(
    gaps: list[dict[str, Any]],
    config: Config,
) -> CheckResult:
    """
    Catches the top-ranked gap being computed from too few listings.
    gaps must be sorted by opportunity_cost descending (as storage returns them).
    """
    name = "gap_sample_size"

    if not gaps:
        return CheckResult(
            name=name,
            passed=True,
            observed="n=0",
            threshold="n/a",
            message="No gaps to check.",
        )

    top_gap   = gaps[0]
    top_skill = top_gap.get("skill", "unknown")
    top_n     = top_gap.get("listings_blocked", 0)

    if top_n < config.min_gap_sample:
        return CheckResult(
            name=name,
            passed=False,
            observed=f"top_gap='{top_skill}', listings_blocked={top_n}",
            threshold=f"min_gap_sample={config.min_gap_sample}",
            message=(
                f"Top-ranked gap '{top_skill}' is based on only {top_n} listing(s). "
                f"Minimum required: {config.min_gap_sample}. "
                f"Gap ranking from too little data."
            ),
        )

    return CheckResult(
        name=name,
        passed=True,
        observed=f"top_gap='{top_skill}', listings_blocked={top_n}",
        threshold=f"min_gap_sample={config.min_gap_sample}",
        message=f"Gap sample size OK — top gap '{top_skill}' from {top_n} listings.",
    )


# ---------------------------------------------------------------------------
# Check 4: data freshness
# ---------------------------------------------------------------------------

def check_freshness(
    latest_fetch_at: str | None,
    config: Config,
    now: datetime,
) -> CheckResult:
    """
    Catches stale data reaching the dashboard.
    `now` is a parameter — never datetime.now() inside this function.
    """
    name = "freshness"

    if latest_fetch_at is None:
        return CheckResult(
            name=name,
            passed=False,
            observed="latest_fetch_at=None",
            threshold=f"max_data_age_days={config.max_data_age_days}",
            message="No listings in the database. Nothing has been fetched yet.",
        )

    try:
        then = datetime.fromisoformat(latest_fetch_at)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
    except ValueError:
        return CheckResult(
            name=name,
            passed=False,
            observed=f"latest_fetch_at='{latest_fetch_at}' (unparseable)",
            threshold=f"max_data_age_days={config.max_data_age_days}",
            message=f"Could not parse latest_fetch_at timestamp: '{latest_fetch_at}'.",
        )

    age_days = (now - then).total_seconds() / 86400.0

    if age_days > config.max_data_age_days:
        return CheckResult(
            name=name,
            passed=False,
            observed=f"age={age_days:.1f}d (fetched {latest_fetch_at[:19]})",
            threshold=f"max_data_age_days={config.max_data_age_days}",
            message=(
                f"Newest listing is {age_days:.1f} days old. "
                f"Exceeds max {config.max_data_age_days} days. "
                f"Dashboard would show stale data."
            ),
        )

    return CheckResult(
        name=name,
        passed=True,
        observed=f"age={age_days:.1f}d",
        threshold=f"max_data_age_days={config.max_data_age_days}",
        message=f"Freshness OK — newest listing is {age_days:.1f} days old.",
    )


# ---------------------------------------------------------------------------
# Run all checks
# ---------------------------------------------------------------------------

def run_all_checks(
    scores: list[int],
    facts_list: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    latest_fetch_at: str | None,
    config: Config,
    now: datetime,
) -> Verdict:
    """
    Run every verification check. Returns a Verdict.
    Passed only if ALL checks pass (rule 35).
    """
    results = [
        check_score_spread(scores, config),
        check_extraction_sanity(facts_list, config),
        check_gap_sample_size(gaps, config),
        check_freshness(latest_fetch_at, config, now),
    ]

    failed = [r for r in results if not r.passed]
    return Verdict(
        passed=len(failed) == 0,
        failed_checks=failed,
        all_checks=results,
    )
