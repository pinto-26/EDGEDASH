"""
test_verification.py — tests for each verification check.

Pure functions — no DB, no network, no config file.
One passing case + one failing case per check, plus edge cases.
Run with: pytest tests/test_verification.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from edgedash.config import Config
from edgedash.verification import (
    CheckResult,
    Verdict,
    check_extraction_sanity,
    check_freshness,
    check_gap_sample_size,
    check_score_spread,
    run_all_checks,
)


# ---------------------------------------------------------------------------
# Config fixture — sensible defaults, easy to override
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> Config:
    defaults = dict(
        target_role="Data Analyst",
        target_city="Bengaluru",
        keywords=[],
        my_skills=[],
        experience_years=2,
        min_score_spread=10,
        min_score_stdev=5.0,
        max_empty_extraction_pct=20.0,
        max_skills_per_listing=20,
        min_gap_sample=3,
        max_data_age_days=3,
    )
    defaults.update(overrides)
    return Config(**defaults)


_NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# check_score_spread
# ---------------------------------------------------------------------------

class TestCheckScoreSpread:

    def test_passes_good_spread(self):
        scores = [30, 45, 60, 75, 88]   # spread=58, stdev≈20
        result = check_score_spread(scores, _cfg())
        assert result.passed is True
        assert result.name == "score_spread"

    def test_fails_insufficient_spread(self):
        scores = [50, 51, 52, 53, 54]   # spread=4 < 10
        result = check_score_spread(scores, _cfg())
        assert result.passed is False
        assert "spread=4" in result.observed
        assert "min_score_spread=10" in result.threshold

    def test_fails_low_stdev_despite_adequate_spread(self):
        # spread=10 exactly meets threshold, but all scores cluster tightly
        # stdev of [45,46,47,48,55] ≈ 3.6 < 5.0
        scores = [45, 46, 47, 48, 55]
        result = check_score_spread(scores, _cfg(min_score_spread=10))
        assert result.passed is False
        assert "stdev" in result.observed

    def test_passes_trivially_fewer_than_5_scores(self):
        scores = [50, 51, 52]           # only 3 — not enough to judge
        result = check_score_spread(scores, _cfg())
        assert result.passed is True
        assert "only 3" in result.message

    def test_passes_exactly_5_scores_with_good_spread(self):
        scores = [20, 40, 55, 70, 85]
        result = check_score_spread(scores, _cfg())
        assert result.passed is True

    def test_fails_returns_observed_values(self):
        scores = [44, 44, 45, 44, 45]
        result = check_score_spread(scores, _cfg())
        assert result.passed is False
        assert result.observed != ""
        assert result.threshold != ""
        assert result.message != ""

    def test_empty_scores_passes_trivially(self):
        result = check_score_spread([], _cfg())
        assert result.passed is True


# ---------------------------------------------------------------------------
# check_extraction_sanity
# ---------------------------------------------------------------------------

def _facts(required: list[str], nice: list[str] | None = None) -> dict:
    return {
        "required_skills": required,
        "nice_to_have":    nice or [],
    }


class TestCheckExtractionSanity:

    def test_passes_healthy_extractions(self):
        facts = [
            _facts(["python", "sql", "pandas"]),
            _facts(["java", "spring", "sql"]),
            _facts(["tableau", "excel", "sql"]),
        ]
        result = check_extraction_sanity(facts, _cfg())
        assert result.passed is True

    def test_fails_too_many_empty_required_skills(self):
        # 3 of 4 are empty = 75% > 20% threshold
        facts = [
            _facts([]),
            _facts([]),
            _facts([]),
            _facts(["python"]),
        ]
        result = check_extraction_sanity(facts, _cfg())
        assert result.passed is False
        assert "empty_pct" in result.observed
        assert "max_empty_extraction_pct=20.0" in result.threshold

    def test_fails_bloated_skill_list(self):
        # one listing has 21 skills — exceeds max_skills_per_listing=20
        many_skills = [f"skill_{i}" for i in range(21)]
        facts = [
            _facts(["python", "sql"]),
            _facts(many_skills),
        ]
        result = check_extraction_sanity(facts, _cfg())
        assert result.passed is False
        assert "21" in result.observed
        assert "max_skills_per_listing=20" in result.threshold

    def test_passes_exactly_at_empty_threshold(self):
        # exactly 20% empty (1 of 5) — should pass at boundary
        facts = [
            _facts([]),
            _facts(["python"]),
            _facts(["sql"]),
            _facts(["tableau"]),
            _facts(["excel"]),
        ]
        result = check_extraction_sanity(facts, _cfg(max_empty_extraction_pct=20.0))
        assert result.passed is True

    def test_passes_exactly_at_max_skills_boundary(self):
        # exactly 20 skills — should pass (not strictly greater)
        facts = [_facts([f"skill_{i}" for i in range(20)])]
        result = check_extraction_sanity(facts, _cfg())
        assert result.passed is True

    def test_passes_empty_list(self):
        result = check_extraction_sanity([], _cfg())
        assert result.passed is True
        assert "No extractions" in result.message


# ---------------------------------------------------------------------------
# check_gap_sample_size
# ---------------------------------------------------------------------------

def _gap(skill: str, blocked: int) -> dict:
    return {
        "skill":            skill,
        "listings_blocked": blocked,
        "opportunity_cost": blocked * 0.5,
    }


class TestCheckGapSampleSize:

    def test_passes_adequate_sample(self):
        gaps = [_gap("kubernetes", 5), _gap("spark", 3)]
        result = check_gap_sample_size(gaps, _cfg())
        assert result.passed is True
        assert "kubernetes" in result.observed

    def test_fails_top_gap_too_few_listings(self):
        # top gap from only 1 listing — below min_gap_sample=3
        gaps = [_gap("terraform", 1), _gap("spark", 8)]
        result = check_gap_sample_size(gaps, _cfg())
        assert result.passed is False
        assert "terraform" in result.observed
        assert "listings_blocked=1" in result.observed
        assert "min_gap_sample=3" in result.threshold

    def test_passes_exactly_at_minimum(self):
        gaps = [_gap("docker", 3)]
        result = check_gap_sample_size(gaps, _cfg(min_gap_sample=3))
        assert result.passed is True

    def test_passes_empty_gaps(self):
        result = check_gap_sample_size([], _cfg())
        assert result.passed is True
        assert "No gaps" in result.message

    def test_checks_top_gap_not_second(self):
        # top gap has 2 listings (fails), second has 10 (passes)
        gaps = [_gap("terraform", 2), _gap("kubernetes", 10)]
        result = check_gap_sample_size(gaps, _cfg())
        assert result.passed is False
        assert "terraform" in result.observed


# ---------------------------------------------------------------------------
# check_freshness
# ---------------------------------------------------------------------------

class TestCheckFreshness:

    def test_passes_fresh_data(self):
        # fetched 1 day ago — within 3-day window
        fetch_at = "2026-08-24T12:00:00+00:00"
        result   = check_freshness(fetch_at, _cfg(), _NOW)
        assert result.passed is True
        assert "1.0d" in result.observed

    def test_fails_stale_data(self):
        # fetched 5 days ago — exceeds max_data_age_days=3
        fetch_at = "2026-08-20T12:00:00+00:00"
        result   = check_freshness(fetch_at, _cfg(), _NOW)
        assert result.passed is False
        assert "5.0d" in result.observed
        assert "max_data_age_days=3" in result.threshold

    def test_fails_no_data_at_all(self):
        result = check_freshness(None, _cfg(), _NOW)
        assert result.passed is False
        assert "None" in result.observed

    def test_passes_fetched_today(self):
        fetch_at = "2026-08-25T10:00:00+00:00"   # 2h ago
        result   = check_freshness(fetch_at, _cfg(), _NOW)
        assert result.passed is True

    def test_passes_exactly_at_boundary(self):
        # exactly 3 days ago — should pass (not strictly greater)
        fetch_at = "2026-08-22T12:00:00+00:00"
        result   = check_freshness(fetch_at, _cfg(max_data_age_days=3), _NOW)
        assert result.passed is True

    def test_fails_unparseable_timestamp(self):
        result = check_freshness("not-a-date", _cfg(), _NOW)
        assert result.passed is False
        assert "unparseable" in result.observed

    def test_now_is_parameter_not_internal_clock(self):
        # Verify the same fetch_at gives different results with different `now`
        fetch_at = "2026-08-22T12:00:00+00:00"
        now_close = datetime(2026, 8, 24, 12, 0, 0, tzinfo=timezone.utc)  # 2d later
        now_far   = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)  # 6d later
        assert check_freshness(fetch_at, _cfg(), now_close).passed is True
        assert check_freshness(fetch_at, _cfg(), now_far).passed is False


# ---------------------------------------------------------------------------
# run_all_checks
# ---------------------------------------------------------------------------

class TestRunAllChecks:

    def test_all_pass_returns_passed_verdict(self):
        verdict = run_all_checks(
            scores=[30, 50, 65, 75, 88],
            facts_list=[_facts(["python", "sql"])],
            gaps=[_gap("kubernetes", 5)],
            latest_fetch_at="2026-08-24T12:00:00+00:00",
            config=_cfg(),
            now=_NOW,
        )
        assert verdict.passed is True
        assert len(verdict.failed_checks) == 0
        assert len(verdict.all_checks) == 4

    def test_one_failure_fails_verdict(self):
        verdict = run_all_checks(
            scores=[50, 51, 50, 51, 50],  # inflated — will fail
            facts_list=[_facts(["python"])],
            gaps=[_gap("kubernetes", 5)],
            latest_fetch_at="2026-08-24T12:00:00+00:00",
            config=_cfg(),
            now=_NOW,
        )
        assert verdict.passed is False
        assert len(verdict.failed_checks) == 1
        assert verdict.failed_checks[0].name == "score_spread"

    def test_summary_names_failed_checks(self):
        verdict = run_all_checks(
            scores=[50, 51, 50, 51, 50],
            facts_list=[_facts([]) for _ in range(5)],  # 100% empty
            gaps=[_gap("k8s", 5)],
            latest_fetch_at="2026-08-24T12:00:00+00:00",
            config=_cfg(),
            now=_NOW,
        )
        assert "score_spread" in verdict.summary
        assert "extraction_sanity" in verdict.summary
