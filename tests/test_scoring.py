"""
test_scoring.py — tests for the deterministic scoring functions.

No DB, no network, no LLM. score_listing is a pure function.
Run with: pytest tests/test_scoring.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from edgedash.config import Config
from edgedash.scoring import score_listing, _seniority_score, _skill_score


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> Config:
    """Config with sensible test defaults, easy to override per test."""
    defaults = dict(
        target_role="Data Analyst",
        target_city="Bengaluru",
        my_skills=["python", "sql", "pandas", "excel"],
        experience_years=2,
        target_seniority="mid",
        weight_skill_match=0.45,
        weight_seniority_fit=0.25,
        weight_location_fit=0.15,
        weight_recency=0.15,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _listing(**overrides) -> dict:
    """Minimal listing dict, easy to override per test."""
    defaults = dict(
        id="test-id",
        title="Data Analyst",
        company="Acme",
        location="Bengaluru",
        url="https://example.com/job/1",
        description="A test listing.",
        source="test",
        posted_at=datetime.now(timezone.utc).isoformat(),
        fetched_at=datetime.now(timezone.utc).isoformat(),
        fit_score=None,
        fit_reason=None,
    )
    defaults.update(overrides)
    return defaults


def _facts(**overrides) -> dict:
    """Minimal extracted facts, easy to override per test."""
    defaults = dict(
        required_skills=["python", "sql"],
        nice_to_have=["tableau"],
        seniority="mid",
        years_required=2,
        remote_ok=None,
    )
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# score_listing integration tests
# ---------------------------------------------------------------------------

class TestScoreListing:

    def test_perfect_match(self):
        """All required skills present, exact seniority, in target city, posted today."""
        cfg = _cfg()
        listing = _listing(
            location="Bengaluru",
            posted_at=datetime.now(timezone.utc).isoformat(),
        )
        facts = _facts(
            required_skills=["python", "sql"],
            nice_to_have=[],
            seniority="mid",
            remote_ok=None,
        )
        result = score_listing(listing, facts, cfg)
        # skill_match=1.0, seniority=1.0, location=1.0(city), recency=1.0
        assert result["score"] == 100
        assert result["components"]["skill_match"] == 1.0
        assert result["components"]["seniority_fit"] == 1.0
        assert result["components"]["location_fit"] == 1.0
        assert result["components"]["recency"] == pytest.approx(1.0, abs=0.05)
        assert result["missing_skills"] == []

    def test_zero_match(self):
        """No required skills in my profile, wrong seniority, wrong city, old post."""
        cfg = _cfg(my_skills=["powerpoint"])
        old_date = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        listing = _listing(location="London", posted_at=old_date)
        facts = _facts(
            required_skills=["kubernetes", "spark", "scala"],
            seniority="lead",
            remote_ok=False,
        )
        result = score_listing(listing, facts, cfg)
        assert result["score"] < 20
        assert result["components"]["skill_match"] == 0.0
        assert result["components"]["seniority_fit"] == 0.25   # lead vs mid = 2 bands
        assert result["components"]["location_fit"] == 0.1    # not remote, wrong city
        assert result["components"]["recency"] == 0.0
        assert "kubernetes" in result["missing_skills"]

    def test_empty_required_skills_no_divide_by_zero(self):
        """When required_skills is empty, score must not crash and skill_match is 1.0."""
        cfg = _cfg()
        facts = _facts(required_skills=[], nice_to_have=[])
        result = score_listing(_listing(), facts, cfg)
        assert result["score"] >= 0
        assert result["components"]["skill_match"] == 1.0

    def test_null_posted_at(self):
        """posted_at=None must not crash; recency defaults to 0.5."""
        cfg = _cfg()
        listing = _listing(posted_at=None)
        result = score_listing(listing, _facts(), cfg)
        assert result["score"] >= 0
        assert result["components"]["recency"] == 0.5
        assert result["recency_label"] == "date unknown"

    def test_null_remote_ok(self):
        """remote_ok=None and listing in target city -> location_fit 1.0."""
        cfg = _cfg()
        listing = _listing(location="Bengaluru")
        facts = _facts(remote_ok=None)
        result = score_listing(listing, facts, cfg)
        assert result["components"]["location_fit"] == 1.0

    def test_remote_ok_true_overrides_location(self):
        """remote_ok=True must give location_fit=1.0 regardless of listed location."""
        cfg = _cfg()
        listing = _listing(location="Berlin")
        facts = _facts(remote_ok=True)
        result = score_listing(listing, facts, cfg)
        assert result["components"]["location_fit"] == 1.0

    def test_seniority_three_bands_off(self):
        """junior vs lead = 3 bands = score 0.0."""
        s = _seniority_score("junior", "lead")
        assert s == 0.0

    def test_seniority_exact(self):
        assert _seniority_score("senior", "senior") == 1.0

    def test_seniority_one_band(self):
        assert _seniority_score("mid", "senior") == 0.6

    def test_seniority_two_bands(self):
        assert _seniority_score("junior", "senior") == 0.25

    def test_seniority_unknown_gives_neutral(self):
        assert _seniority_score("unknown", "mid") == 0.5

    def test_score_clamped_to_100(self):
        """Score must never exceed 100."""
        cfg = _cfg()
        listing = _listing(
            location="Bengaluru",
            posted_at=datetime.now(timezone.utc).isoformat(),
        )
        facts = _facts(required_skills=["python"], seniority="mid", remote_ok=True)
        result = score_listing(listing, facts, cfg)
        assert 0 <= result["score"] <= 100

    def test_score_clamped_to_zero(self):
        """Score must never go below 0."""
        cfg = _cfg(my_skills=[])
        old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        listing = _listing(location="Tokyo", posted_at=old)
        facts = _facts(
            required_skills=["cobol", "fortran"],
            seniority="lead",
            remote_ok=False,
        )
        result = score_listing(listing, facts, cfg)
        assert result["score"] >= 0

    def test_reason_contains_gap(self):
        """build_reason must name missing required skills."""
        cfg = _cfg(my_skills=["python"])
        facts = _facts(required_skills=["python", "spark", "kubernetes"])
        result = score_listing(_listing(), facts, cfg)
        assert "spark" in result["reason"]
        assert "kubernetes" in result["reason"]

    def test_nice_to_have_counted_at_one_third(self):
        """nice_to_have skills should raise score but less than required."""
        cfg = _cfg(my_skills=["tableau"])
        # Only nice_to_have matched, no required
        score_raw, matched, missing = _skill_score(
            required=["python", "sql"],
            nice_to_have=["tableau"],
            my_skills=["tableau"],
        )
        # numerator = 0 + 1/3; denominator = 2 + 1/3
        expected = (1 / 3) / (2 + 1 / 3)
        assert score_raw == pytest.approx(expected, abs=1e-6)
        assert "tableau" in matched
        assert "python" in missing
