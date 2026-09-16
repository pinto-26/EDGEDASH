"""
test_query_tools.py — tests for the query tool registry.

Uses a real in-memory SQLite database via storage.init_db + upsert_listings
so each tool is tested against actual data shapes, not mocks.

No network, no LLM, no config file needed — Config is built inline.
"""

from __future__ import annotations

import json
import tempfile
import os
from datetime import datetime, timedelta, timezone

import pytest

from edgedash.config import Config
from edgedash.query.tools import (
    TOOLS,
    _clamp_int,
    best_matches,
    companies_hiring,
    gap_detail,
    listing_count,
    skill_demand,
    top_gaps,
    trend,
)
import edgedash.storage as storage


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _cfg(db_path: str, **overrides) -> Config:
    defaults = dict(
        target_role="Data Analyst",
        target_city="Bengaluru",
        my_skills=["python", "sql"],
        keywords=[],
        experience_years=2,
        skill_aliases={"pyspark": "apache spark", "k8s": "kubernetes"},
        db_path=db_path,
    )
    defaults.update(overrides)
    return Config(**defaults)


@pytest.fixture()
def db(tmp_path):
    """Initialised database with a handful of scored listings and one gap snapshot."""
    path = str(tmp_path / "test.db")
    storage.init_db(path)

    now = datetime.now(timezone.utc)

    # Insert 5 scored listings
    listings = [
        {
            "id": f"listing-{i}",
            "title": f"Data Analyst {i}",
            "company": f"Company{i % 3}",        # 3 companies
            "location": "Bengaluru",
            "url": f"https://example.com/{i}",
            "description": f"Requires python sql pandas skill{i}",
            "source": "test",
            "posted_at": (now - timedelta(days=i)).isoformat(),
            "fetched_at": (now - timedelta(hours=i)).isoformat(),
            "fit_score": 50 + i * 10,             # 50, 60, 70, 80, 90
            "fit_reason": f"reason{i}",
        }
        for i in range(5)
    ]
    storage.upsert_listings(path, listings)

    # Add extraction cache entries (needed for skill_demand and gap_detail)
    import hashlib
    aliases = {"pyspark": "apache spark", "k8s": "kubernetes"}
    for i, listing in enumerate(listings):
        desc_hash = hashlib.sha256(listing["description"].encode()).hexdigest()
        extracted = {
            "required_skills": ["python", "sql", f"skill{i}"],
            "nice_to_have":    ["tableau"],
            "seniority":       "mid",
            "years_required":  2,
            "remote_ok":       None,
        }
        storage.set_extraction(path, desc_hash, extracted)

    # Add two gap snapshots (needed for trend)
    import uuid
    for day_offset in [2, 0]:  # oldest first, newest second
        snap_time = (now - timedelta(days=day_offset)).isoformat()
        gaps = [
            {
                "skill": "kubernetes",
                "listings_blocked": 3 + day_offset,
                "opportunity_cost": 1.5 + day_offset * 0.1,
                "mean_score": 65.0,
                "top_score": 80,
                "also_nice_to_have": 0,
                "low_confidence": False,
                "example_ids": ["listing-0", "listing-1"],
            },
            {
                "skill": "apache spark",
                "listings_blocked": 2,
                "opportunity_cost": 0.9,
                "mean_score": 58.0,
                "top_score": 70,
                "also_nice_to_have": 1,
                "low_confidence": True,
                "example_ids": ["listing-2"],
            },
        ]
        storage.write_gap_snapshot(path, uuid.uuid4().hex, snap_time, gaps)

    return path


# ---------------------------------------------------------------------------
# @tool decorator / registry tests
# ---------------------------------------------------------------------------

class TestToolRegistry:

    def test_all_seven_tools_registered(self):
        expected = {
            "companies_hiring", "best_matches", "top_gaps",
            "gap_detail", "trend", "listing_count", "skill_demand",
        }
        assert expected.issubset(set(TOOLS.keys()))

    def test_each_tool_has_description_and_params(self):
        for name, entry in TOOLS.items():
            assert "description" in entry, f"{name} missing description"
            assert "params"      in entry, f"{name} missing params"
            assert "fn"          in entry, f"{name} missing fn"
            assert len(entry["description"]) > 20, f"{name} description too short"

    def test_descriptions_are_distinct(self):
        descs = [e["description"][:40] for e in TOOLS.values()]
        assert len(descs) == len(set(descs)), "Two tools share a description prefix"


# ---------------------------------------------------------------------------
# _clamp_int
# ---------------------------------------------------------------------------

class TestClampInt:

    def test_clamps_below_lower_bound(self):
        assert _clamp_int(0, 1, 90, 7) == 1

    def test_clamps_above_upper_bound(self):
        assert _clamp_int(999, 1, 90, 7) == 90

    def test_passes_through_valid_value(self):
        assert _clamp_int(14, 1, 90, 7) == 14

    def test_uses_default_on_bad_input(self):
        assert _clamp_int("banana", 1, 90, 7) == 7

    def test_lower_bound_inclusive(self):
        assert _clamp_int(1, 1, 90, 7) == 1

    def test_upper_bound_inclusive(self):
        assert _clamp_int(90, 1, 90, 7) == 90


# ---------------------------------------------------------------------------
# companies_hiring
# ---------------------------------------------------------------------------

class TestCompaniesHiring:

    def test_returns_correct_shape(self, db):
        cfg = _cfg(db)
        result = companies_hiring(cfg, days=7)
        assert "rows" in result and "summary" in result
        for r in result["rows"]:
            assert "company" in r
            assert "listing_count" in r
            assert "best_score" in r

    def test_clamps_days_to_max(self, db):
        cfg = _cfg(db)
        r90  = companies_hiring(cfg, days=90)
        r999 = companies_hiring(cfg, days=999)
        assert r90["rows"] == r999["rows"]

    def test_clamps_days_to_min(self, db):
        cfg = _cfg(db)
        r1 = companies_hiring(cfg, days=1)
        r0 = companies_hiring(cfg, days=0)
        assert r1["rows"] == r0["rows"]

    def test_summary_mentions_days(self, db):
        cfg = _cfg(db)
        result = companies_hiring(cfg, days=30)
        assert "30" in result["summary"]

    def test_recent_window_returns_subset(self, db):
        cfg    = _cfg(db)
        all_co = companies_hiring(cfg, days=90)
        few_co = companies_hiring(cfg, days=1)
        # 1-day window should have ≤ listings than 90-day window
        total_all = sum(r["listing_count"] for r in all_co["rows"])
        total_few = sum(r["listing_count"] for r in few_co["rows"])
        assert total_few <= total_all


# ---------------------------------------------------------------------------
# best_matches
# ---------------------------------------------------------------------------

class TestBestMatches:

    def test_returns_correct_shape(self, db):
        cfg    = _cfg(db)
        result = best_matches(cfg, n=3)
        assert len(result["rows"]) == 3
        for r in result["rows"]:
            assert "score" in r and "title" in r and "company" in r

    def test_sorted_by_score_descending(self, db):
        cfg    = _cfg(db)
        result = best_matches(cfg, n=5)
        scores = [r["score"] for r in result["rows"]]
        assert scores == sorted(scores, reverse=True)

    def test_clamps_n_to_max(self, db):
        cfg    = _cfg(db)
        r25    = best_matches(cfg, n=25)
        r999   = best_matches(cfg, n=999)
        assert len(r25["rows"]) == len(r999["rows"])

    def test_clamps_n_to_min(self, db):
        cfg  = _cfg(db)
        r1   = best_matches(cfg, n=1)
        r0   = best_matches(cfg, n=0)
        assert len(r1["rows"]) == len(r0["rows"]) == 1


# ---------------------------------------------------------------------------
# top_gaps
# ---------------------------------------------------------------------------

class TestTopGaps:

    def test_returns_correct_shape(self, db):
        cfg    = _cfg(db)
        result = top_gaps(cfg, n=2)
        assert len(result["rows"]) <= 2
        for r in result["rows"]:
            assert "skill" in r
            assert "listings_blocked" in r
            assert "opportunity_cost" in r
            assert "rank" in r

    def test_clamps_n_to_max(self, db):
        cfg  = _cfg(db)
        r25  = top_gaps(cfg, n=25)
        r999 = top_gaps(cfg, n=999)
        assert len(r25["rows"]) == len(r999["rows"])

    def test_empty_snapshot_returns_empty(self, tmp_path):
        path = str(tmp_path / "empty.db")
        storage.init_db(path)
        cfg    = _cfg(path)
        result = top_gaps(cfg, n=5)
        assert result["rows"] == []


# ---------------------------------------------------------------------------
# gap_detail
# ---------------------------------------------------------------------------

class TestGapDetail:

    def test_returns_listings_for_known_skill(self, db):
        cfg    = _cfg(db)
        result = gap_detail(cfg, skill="kubernetes")
        assert result["rows"] is not None
        assert "summary" in result
        assert "kubernetes" in result["summary"]

    def test_unknown_skill_returns_empty_not_raises(self, db):
        cfg    = _cfg(db)
        result = gap_detail(cfg, skill="cobol_fortran_abacus")
        assert result["rows"] == []
        assert "not found" in result["summary"].lower()

    def test_empty_skill_returns_empty(self, db):
        cfg    = _cfg(db)
        result = gap_detail(cfg, skill="")
        assert result["rows"] == []

    def test_skill_canonicalised_before_lookup(self, db):
        cfg = _cfg(db)
        # "k8s" should canonicalise to "kubernetes" via aliases
        r_raw    = gap_detail(cfg, skill="kubernetes")
        r_alias  = gap_detail(cfg, skill="k8s")
        assert r_raw["summary"] == r_alias["summary"]

    def test_returned_rows_have_required_fields(self, db):
        cfg    = _cfg(db)
        result = gap_detail(cfg, skill="kubernetes")
        for r in result["rows"]:
            assert "title"   in r
            assert "company" in r
            assert "score"   in r


# ---------------------------------------------------------------------------
# trend
# ---------------------------------------------------------------------------

class TestTrend:

    def test_returns_correct_shape_with_two_snapshots(self, db):
        cfg    = _cfg(db)
        result = trend(cfg, weeks=3)
        assert "rows" in result and "summary" in result
        for r in result["rows"]:
            assert "skill"   in r
            assert "latest"  in r
            assert "is_new"  in r

    def test_clamps_weeks_to_max(self, db):
        cfg   = _cfg(db)
        r12   = trend(cfg, weeks=12)
        r999  = trend(cfg, weeks=999)
        assert r12["rows"] == r999["rows"]

    def test_clamps_weeks_to_min(self, db):
        cfg  = _cfg(db)
        r1   = trend(cfg, weeks=1)
        r0   = trend(cfg, weeks=0)
        assert r1["rows"] == r0["rows"]

    def test_single_snapshot_returns_empty_with_message(self, tmp_path):
        path = str(tmp_path / "single.db")
        storage.init_db(path)
        storage.write_gap_snapshot(path, "abc", "2026-01-01T00:00:00+00:00", [
            {"skill": "python", "listings_blocked": 1, "opportunity_cost": 0.5,
             "mean_score": 50.0, "top_score": 60, "also_nice_to_have": 0,
             "low_confidence": True, "example_ids": []}
        ])
        cfg    = _cfg(path)
        result = trend(cfg, weeks=3)
        assert result["rows"] == []
        assert "1" in result["summary"]


# ---------------------------------------------------------------------------
# listing_count
# ---------------------------------------------------------------------------

class TestListingCount:

    def test_returns_correct_shape(self, db):
        cfg    = _cfg(db)
        result = listing_count(cfg)
        assert len(result["rows"]) == 1
        r = result["rows"][0]
        assert "total_listings" in r
        assert "scored" in r
        assert "unscored" in r
        assert "newest_fetch_at" in r

    def test_counts_match_data(self, db):
        cfg    = _cfg(db)
        result = listing_count(cfg)
        r = result["rows"][0]
        assert r["total_listings"] == 5
        assert r["scored"] == 5
        assert r["unscored"] == 0

    def test_empty_db_returns_zeros(self, tmp_path):
        path = str(tmp_path / "empty.db")
        storage.init_db(path)
        cfg    = _cfg(path)
        result = listing_count(cfg)
        r = result["rows"][0]
        assert r["total_listings"] == 0
        assert r["scored"] == 0


# ---------------------------------------------------------------------------
# skill_demand
# ---------------------------------------------------------------------------

class TestSkillDemand:

    def test_known_skill_returns_counts(self, db):
        cfg    = _cfg(db)
        result = skill_demand(cfg, skill="python")
        assert len(result["rows"]) == 1
        r = result["rows"][0]
        assert r["skill"] == "python"
        assert r["required_in"] > 0
        assert "required_pct" in r

    def test_unknown_skill_returns_empty_not_raises(self, db):
        cfg    = _cfg(db)
        result = skill_demand(cfg, skill="cobol_abacus_9999")
        assert result["rows"][0]["total_appearances"] == 0

    def test_alias_resolved_before_lookup(self, db):
        cfg      = _cfg(db)
        r_canon  = skill_demand(cfg, skill="apache spark")
        r_alias  = skill_demand(cfg, skill="pyspark")
        assert r_canon["rows"][0]["skill"] == r_alias["rows"][0]["skill"]

    def test_empty_skill_returns_empty(self, db):
        cfg    = _cfg(db)
        result = skill_demand(cfg, skill="")
        assert result["rows"] == []

    def test_nice_to_have_tracked_separately(self, db):
        # "tableau" is in nice_to_have only
        cfg    = _cfg(db)
        result = skill_demand(cfg, skill="tableau")
        r = result["rows"][0]
        assert r["nice_to_have_in"] > 0
        # Should not appear in required count
        assert r["required_in"] == 0
