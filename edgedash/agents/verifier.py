"""
verifier.py — judges output plausibility. No writes, no repairs (rule 34).

Reads scores, extractions, gaps, and fetch time from storage.
Calls run_all_checks and returns a Verdict in the AgentResult notes.
The Orchestrator decides what to do with a failure.

The Verifier stores its last Verdict on self.last_verdict so the
Orchestrator can inspect it without re-parsing the notes string.
"""

from __future__ import annotations

from datetime import datetime, timezone

import edgedash.storage as storage
from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.planning import StopConditions
from edgedash.verification import Verdict, run_all_checks


class Verifier:
    name: str = "Verifier"

    def __init__(self) -> None:
        self.last_verdict: Verdict | None = None

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: StopConditions | None = None,
    ) -> AgentResult:
        started_at = datetime.now(timezone.utc)

        # Collect inputs — cheap queries only, no full table loads
        scored_rows = storage.get_listings(db_path, limit=500, min_score=0)
        scores      = [r["fit_score"] for r in scored_rows if r.get("fit_score") is not None]

        facts_list  = [
            e["extracted"]
            for e in storage.get_scored_listings_with_extractions(db_path)
        ]

        gaps            = storage.get_latest_gap_snapshot(db_path)
        latest_fetch_at = storage.last_fetch_time(db_path)

        verdict: Verdict = run_all_checks(
            scores          = scores,
            facts_list      = facts_list,
            gaps            = gaps,
            latest_fetch_at = latest_fetch_at,
            config          = config,
            now             = started_at,
        )

        # Build notes — rule 37: name the check and the observed value
        if verdict.passed:
            notes = f"VERDICT: pass — {verdict.summary}"
            status = "ok"
        else:
            failed_detail = "; ".join(
                f"{c.name} observed {c.observed} (threshold {c.threshold})"
                for c in verdict.failed_checks
            )
            notes  = f"VERDICT: fail — {failed_detail}"
            status = "failed"

        storage.log_cycle(
            path            = db_path,
            agent           = self.name,
            started_at      = started_at.isoformat(),
            finished_at     = datetime.now(timezone.utc).isoformat(),
            records_touched = len(scores),
            status          = status,
            notes           = notes,
        )

        self.last_verdict = verdict

        return AgentResult(
            agent           = self.name,
            status          = status,
            records_touched = len(scores),
            notes           = notes,
        )
