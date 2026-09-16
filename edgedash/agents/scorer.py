"""
scorer.py — Scorer agent. Calls extractor, then scoring. No direct LLM logic.

Per-listing try/except means one failure never stops the batch (rule 17).
Score distribution is logged after every batch (rule 20).
Batch size is capped by config.score_batch_size (rule 21).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import edgedash.storage as storage
from edgedash.agents.base import AgentResult
from edgedash.agents.extractor import extract
from edgedash.config import Config
from edgedash.llm import LLMError
from edgedash.planning import StopConditions
from edgedash.scoring import score_listing


class Scorer:
    name: str = "Scorer"

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: StopConditions | None = None,
    ) -> AgentResult:
        started_at = datetime.now(timezone.utc).isoformat()

        limit = (
            stop_conditions.max_items
            if stop_conditions and stop_conditions.max_items is not None
            else config.score_batch_size
        )
        strict_required = bool(
            stop_conditions and stop_conditions.widen_spread
        )

        listings = storage.get_unscored_listings(db_path, limit=limit)

        if not listings:
            notes = "No unscored listings — nothing to do."
            storage.log_cycle(
                path=db_path, agent=self.name,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                records_touched=0, status="ok", notes=notes,
            )
            return AgentResult(agent=self.name, status="ok",
                               records_touched=0, notes=notes)

        scores: list[int] = []
        failed = 0

        for listing in listings:
            try:
                facts  = extract(listing, config, db_path, strict_required=strict_required)
                result = score_listing(listing, facts, config)
                storage.save_score(
                    db_path,
                    listing["id"],
                    result["score"],
                    result["reason"],
                )
                scores.append(result["score"])

            except (LLMError, Exception) as exc:
                failed += 1
                storage.log_cycle(
                    path=db_path,
                    agent=f"Scorer:{listing.get('id', '?')}",
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    records_touched=0,
                    status="failed",
                    notes=f"{type(exc).__name__}: {exc}",
                )

        # --- Distribution + suspect check (rule 20) ---
        finished_at = datetime.now(timezone.utc).isoformat()
        dist_notes, dist_status = _distribution_notes(scores, failed)

        storage.log_cycle(
            path=db_path, agent=self.name,
            started_at=started_at, finished_at=finished_at,
            records_touched=len(scores), status=dist_status,
            notes=dist_notes,
        )
        return AgentResult(
            agent=self.name,
            status="ok" if failed == 0 else "ok",   # agent-level ok; failures logged per-listing
            records_touched=len(scores),
            notes=dist_notes,
        )


def _distribution_notes(scores: list[int], failed: int) -> tuple[str, str]:
    """Return (notes_string, status) for the cycle_log row."""
    if not scores:
        return f"scored 0 · {failed} failed", "ok"

    lo    = min(scores)
    hi    = max(scores)
    mean  = round(sum(scores) / len(scores))
    spread = hi - lo

    spread_label = "SUSPECT (spread < 10)" if spread < 10 else "spread OK"
    status       = "suspect" if spread < 10 else "ok"

    notes = (
        f"scored {len(scores)} · "
        f"range {lo}-{hi} · "
        f"mean {mean} · "
        f"{failed} failed · "
        f"{spread_label}"
    )
    return notes, status

# ---------------------------------------------------------------------------
# CLI entry point:  python -m edgedash.agents.scorer [--limit N]
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    from edgedash.config import load_config
    from edgedash.storage import init_db

    parser = argparse.ArgumentParser(description="Run the Scorer agent standalone.")
    parser.add_argument("--limit", type=int, default=None,
                        help="Override score_batch_size from config.")
    args = parser.parse_args()

    cfg = load_config()
    if args.limit is not None:
        cfg = cfg.__class__(
            **{**cfg.__dict__, "score_batch_size": args.limit}
        )

    init_db(cfg.db_path)
    result = Scorer().run(cfg, cfg.db_path)
    print(f"\n{result.notes}")
