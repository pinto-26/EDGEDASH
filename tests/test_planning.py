"""
test_planning.py — tests for build_plan().

Pure function — no DB, no network, no config file.
Run with: pytest tests/test_planning.py -v
"""

from __future__ import annotations

from edgedash.config import Config
from edgedash.state import SystemState
from edgedash.planning import build_plan, Plan


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _cfg(**overrides) -> Config:
    defaults = dict(
        target_role="Data Analyst",
        target_city="Bengaluru",
        keywords=[],
        my_skills=[],
        experience_years=2,
        fetch_interval_hours=6.0,
        max_fetch_pages=5,
        max_fetch_listings=200,
        score_batch_size=25,
        max_score_seconds=300,
        max_analyse_seconds=60,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _state(**overrides) -> SystemState:
    defaults = dict(
        last_fetch_at="2026-08-22T10:00:00+00:00",
        hours_since_fetch=1.0,
        unscored_count=0,
        gaps_computed_at="2026-08-22T10:05:00+00:00",
        gaps_stale=False,
        last_cycle_verdict="ok",
        last_cycle_at="2026-08-22T10:00:00+00:00",
    )
    defaults.update(overrides)
    return SystemState(**defaults)


def _tasks_by_agent(plan: Plan) -> dict:
    return {t.agent_name: t for t in plan.tasks}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBuildPlan:

    def test_all_stale_all_three_run(self):
        """Everything is out of date — all three agents should run."""
        state = _state(
            hours_since_fetch=10.0,   # > fetch_interval_hours=6
            unscored_count=15,
            gaps_stale=True,
        )
        plan  = build_plan(state, _cfg())
        tasks = _tasks_by_agent(plan)

        assert tasks["Fetcher"].action    == "run"
        assert tasks["Scorer"].action     == "run"
        assert tasks["GapAnalyzer"].action == "run"

    def test_nothing_to_do_all_three_skipped(self):
        """Fresh data, nothing unscored, gaps current — all three skip."""
        state = _state(
            hours_since_fetch=1.0,    # < 6
            unscored_count=0,
            gaps_stale=False,
            gaps_computed_at="2026-08-22T10:05:00+00:00",
        )
        plan  = build_plan(state, _cfg())
        tasks = _tasks_by_agent(plan)

        assert tasks["Fetcher"].action     == "skip"
        assert tasks["Scorer"].action      == "skip"
        assert tasks["GapAnalyzer"].action == "skip"

    def test_only_unscored_listings(self):
        """Fetch is fresh, but there are unscored listings."""
        state = _state(
            hours_since_fetch=0.5,    # fresh
            unscored_count=8,
            gaps_stale=False,
        )
        plan  = build_plan(state, _cfg())
        tasks = _tasks_by_agent(plan)

        assert tasks["Fetcher"].action == "skip"
        assert tasks["Scorer"].action  == "run"
        assert "unscored_count=8"      in tasks["Scorer"].reason

    def test_gaps_stale_nothing_unscored(self):
        """Scorer already done, but gap snapshot is stale."""
        state = _state(
            hours_since_fetch=0.5,
            unscored_count=0,
            gaps_stale=True,
        )
        plan  = build_plan(state, _cfg())
        tasks = _tasks_by_agent(plan)

        assert tasks["Fetcher"].action     == "skip"
        assert tasks["Scorer"].action      == "skip"
        assert tasks["GapAnalyzer"].action == "run"
        assert "gaps_stale=True"           in tasks["GapAnalyzer"].reason

    def test_never_fetched_triggers_fetch(self):
        """No fetch ever — last_fetch_at=None should schedule a fetch."""
        state = _state(
            last_fetch_at=None,
            hours_since_fetch=9999.0,
        )
        plan  = build_plan(state, _cfg())
        tasks = _tasks_by_agent(plan)
        assert tasks["Fetcher"].action == "run"

    def test_gaps_never_computed_triggers_analyse(self):
        """gaps_computed_at=None should always schedule GapAnalyzer."""
        state = _state(
            gaps_computed_at=None,
            gaps_stale=True,
            unscored_count=0,
            hours_since_fetch=1.0,
        )
        plan  = build_plan(state, _cfg())
        tasks = _tasks_by_agent(plan)
        assert tasks["GapAnalyzer"].action == "run"
        assert "None" in tasks["GapAnalyzer"].reason

    def test_plan_contains_all_three_agents(self):
        """Plan must always list all three agents — none silently absent."""
        state = _state()
        plan  = build_plan(state, _cfg())
        names = {t.agent_name for t in plan.tasks}
        assert names == {"Fetcher", "Scorer", "GapAnalyzer"}

    def test_stop_conditions_come_from_config(self):
        """Stop conditions must be read from config, not hardcoded."""
        cfg   = _cfg(score_batch_size=7, max_score_seconds=99)
        state = _state(unscored_count=50)
        plan  = build_plan(state, cfg)
        tasks = _tasks_by_agent(plan)

        sc = tasks["Scorer"].stop_conditions
        assert sc.max_items   == 7
        assert sc.max_seconds == 99

    def test_render_contains_all_agents(self):
        """render() must include a line for every agent."""
        plan = build_plan(_state(), _cfg())
        rendered = plan.render()
        assert "Fetcher"     in rendered
        assert "Scorer"      in rendered
        assert "GapAnalyzer" in rendered

    def test_render_skip_shows_reason(self):
        """Skipped agents must show their reason in the render output."""
        state = _state(hours_since_fetch=1.0, unscored_count=0, gaps_stale=False,
                       gaps_computed_at="2026-08-22T10:05:00+00:00")
        rendered = build_plan(state, _cfg()).render()
        assert "skipped" in rendered.lower()
