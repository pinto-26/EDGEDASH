"""
gap_analyzer.py — deterministic skill gap analysis. No LLM anywhere.

For each required skill missing from my profile, computes:
  opportunity_cost = sum(fit_score / 100 for each blocked listing)

This is the ranking key (rule 24). Raw frequency is tracked but never
used for ranking. A gap in a high-scoring listing costs more than the
same gap in a listing I'd lose on five other dimensions.

Every run writes a timestamped snapshot (rule 25). Previous runs are
never overwritten.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

import edgedash.storage as storage
from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.planning import StopConditions
from edgedash.skills import canonical


# ---------------------------------------------------------------------------
# Core computation
# ---------------------------------------------------------------------------

def _compute_gaps(
    listings: list[dict[str, Any]],
    my_skills: set[str],
    aliases: dict[str, str],
) -> list[dict[str, Any]]:
    """
    For each canonical missing skill, compute all gap metrics.
    Returns a list of gap dicts, ranked by opportunity_cost descending.
    """
    # skill -> list of (listing_id, fit_score)
    required_blocks: dict[str, list[tuple[str, int]]] = defaultdict(list)
    nice_counts: dict[str, int] = defaultdict(int)

    for listing in listings:
        score     = listing["fit_score"]
        lid       = listing["id"]
        extracted = listing["extracted"]

        for raw in (extracted.get("required_skills") or []):
            canon = canonical(raw, aliases)
            if not canon:
                continue
            if canon not in my_skills:
                required_blocks[canon].append((lid, score))

        for raw in (extracted.get("nice_to_have") or []):
            canon = canonical(raw, aliases)
            if canon and canon not in my_skills:
                nice_counts[canon] += 1

    gaps: list[dict[str, Any]] = []

    for skill, blocked in required_blocks.items():
        scores     = [s for _, s in blocked]
        example_ids = [lid for lid, _ in
                       sorted(blocked, key=lambda x: -x[1])[:5]]

        opportunity_cost = sum(s / 100.0 for s in scores)
        mean_score       = sum(scores) / len(scores)
        top_score        = max(scores)
        low_confidence   = len(blocked) < 3

        gaps.append({
            "skill":            skill,
            "listings_blocked": len(blocked),
            "opportunity_cost": opportunity_cost,
            "mean_score":       mean_score,
            "top_score":        top_score,
            "also_nice_to_have": nice_counts.get(skill, 0),
            "low_confidence":   low_confidence,
            "example_ids":      example_ids,
        })

    # Rule 24: rank by opportunity_cost, not raw count
    gaps.sort(key=lambda g: -g["opportunity_cost"])
    return gaps


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class GapAnalyzer:
    name: str = "GapAnalyzer"

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: StopConditions | None = None,
    ) -> AgentResult:
        started_at = datetime.now(timezone.utc).isoformat()

        listings = storage.get_scored_listings_with_extractions(db_path)

        if not listings:
            notes = "No scored listings with extractions — nothing to analyse."
            storage.log_cycle(
                path=db_path, agent=self.name,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                records_touched=0, status="ok", notes=notes,
            )
            return AgentResult(
                agent=self.name, status="ok",
                records_touched=0, notes=notes,
            )

        my_skills = {
            canonical(s, config.skill_aliases)
            for s in config.my_skills
            if s.strip()
        }

        all_gaps  = _compute_gaps(listings, my_skills, config.skill_aliases)
        top_gaps  = all_gaps[:10]

        # Write timestamped snapshot — never overwrites previous (rule 25)
        run_id      = uuid.uuid4().hex
        computed_at = datetime.now(timezone.utc).isoformat()
        storage.write_gap_snapshot(db_path, run_id, computed_at, all_gaps)

        finished_at = datetime.now(timezone.utc).isoformat()

        # Build AgentResult notes
        if top_gaps:
            top  = top_gaps[0]
            conf = " (low-conf)" if top["low_confidence"] else ""
            notes = (
                f"{len(all_gaps)} gaps · "
                f"top: {top['skill']}{conf} "
                f"({top['listings_blocked']} listings, "
                f"cost {top['opportunity_cost']:.1f}) · "
                f"{len(listings)} listings analysed"
            )
        else:
            notes = f"0 gaps found · {len(listings)} listings analysed"

        storage.log_cycle(
            path=db_path, agent=self.name,
            started_at=started_at, finished_at=finished_at,
            records_touched=len(all_gaps), status="ok", notes=notes,
        )
        return AgentResult(
            agent=self.name, status="ok",
            records_touched=len(all_gaps), notes=notes,
        )
