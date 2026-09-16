"""
planning.py — pure function: (SystemState, Config) -> Plan.

No I/O. No imports from storage or llm. Takes state, returns an ordered
list of Tasks. Every agent appears in the plan — either RUN or SKIP —
with the state value that drove the decision (rule 31).

Decision rules (all thresholds come from Config):
  fetch   → if hours_since_fetch >= fetch_interval_hours
  score   → if unscored_count > 0
  analyse → if gaps_stale or gaps_computed_at is None
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from edgedash.config import Config
from edgedash.state import SystemState


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class StopConditions:
    max_items: int | None     = None   # listings, pages, etc.
    max_seconds: int | None   = None
    widen_spread: bool        = False  # hint to Scorer on retry: use stricter extraction prompt


@dataclass
class Task:
    agent_name: str
    action: Literal["run", "skip"]
    goal: str
    stop_conditions: StopConditions
    reason: str                        # the state value that caused the decision


@dataclass
class Plan:
    tasks: list[Task] = field(default_factory=list)

    def render(self) -> str:
        """One line per agent, showing action, goal, stop conditions, reason."""
        lines: list[str] = []
        for t in self.tasks:
            icon = "▶" if t.action == "run" else "–"
            stops: list[str] = []
            if t.stop_conditions.max_items is not None:
                stops.append(f"max={t.stop_conditions.max_items}")
            if t.stop_conditions.max_seconds is not None:
                stops.append(f"timeout={t.stop_conditions.max_seconds}s")
            stop_str = f"  [{', '.join(stops)}]" if stops else ""
            lines.append(
                f"  {icon} {t.agent_name:<16} {t.goal:<38}{stop_str}\n"
                f"    {'reason:':<10} {t.reason}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Planning logic
# ---------------------------------------------------------------------------

def build_plan(state: SystemState, config: Config) -> Plan:
    """
    Pure function. Returns a Plan with every agent listed — RUN or SKIP.
    Thresholds come from config. No I/O.
    """
    tasks: list[Task] = []

    # ---- Fetcher -------------------------------------------------------
    if state.hours_since_fetch >= config.fetch_interval_hours:
        if state.last_fetch_at is None:
            fetch_reason = "hours_since_fetch=∞ (never fetched)"
        else:
            fetch_reason = (
                f"hours_since_fetch={state.hours_since_fetch:.1f} "
                f">= fetch_interval_hours={config.fetch_interval_hours}"
            )
        tasks.append(Task(
            agent_name      = "Fetcher",
            action          = "run",
            goal            = "fetch fresh job listings",
            stop_conditions = StopConditions(
                max_items   = config.max_fetch_listings,
                max_seconds = None,
            ),
            reason          = fetch_reason,
        ))
    else:
        tasks.append(Task(
            agent_name      = "Fetcher",
            action          = "skip",
            goal            = "fetch fresh job listings",
            stop_conditions = StopConditions(),
            reason          = (
                f"skipped: hours_since_fetch={state.hours_since_fetch:.1f} "
                f"< fetch_interval_hours={config.fetch_interval_hours}"
            ),
        ))

    # ---- Scorer --------------------------------------------------------
    if state.unscored_count > 0:
        tasks.append(Task(
            agent_name      = "Scorer",
            action          = "run",
            goal            = f"score {min(state.unscored_count, config.score_batch_size)} listings",
            stop_conditions = StopConditions(
                max_items   = config.score_batch_size,
                max_seconds = config.max_score_seconds,
            ),
            reason          = f"unscored_count={state.unscored_count}",
        ))
    else:
        tasks.append(Task(
            agent_name      = "Scorer",
            action          = "skip",
            goal            = "score unscored listings",
            stop_conditions = StopConditions(),
            reason          = "skipped: unscored_count=0",
        ))

    # ---- GapAnalyzer ---------------------------------------------------
    if state.gaps_stale or state.gaps_computed_at is None:
        if state.gaps_computed_at is None:
            gap_reason = "gaps_computed_at=None (never run)"
        else:
            gap_reason = "gaps_stale=True (scores newer than last gap snapshot)"
        tasks.append(Task(
            agent_name      = "GapAnalyzer",
            action          = "run",
            goal            = "compute skill gap snapshot",
            stop_conditions = StopConditions(
                max_items   = None,
                max_seconds = config.max_analyse_seconds,
            ),
            reason          = gap_reason,
        ))
    else:
        tasks.append(Task(
            agent_name      = "GapAnalyzer",
            action          = "skip",
            goal            = "compute skill gap snapshot",
            stop_conditions = StopConditions(),
            reason          = (
                f"skipped: gaps_stale=False "
                f"(snapshot at {(state.gaps_computed_at or '')[:19]})"
            ),
        ))

    return Plan(tasks=tasks)
