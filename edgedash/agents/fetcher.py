"""
fetcher.py — real Fetcher agent.

Iterates over enabled sources from config, calls each one's fetch(),
combines the results, stamps each row with a stable id via storage.make_listing_id,
writes to storage, and returns an AgentResult.

Per steering rule 12: one source failing never kills the cycle.
Each source is wrapped in its own try/except; failures are logged and skipped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import edgedash.storage as storage
from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.planning import StopConditions
from edgedash.sources.base import SOURCES
from edgedash.sources.http import SourceError

# Import all source modules so their @register decorators fire before
# SOURCES is read. Add new source imports here as they are built.
import edgedash.sources.arbeitnow  # noqa: F401


class Fetcher:
    name: str = "Fetcher"

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: StopConditions | None = None,
    ) -> AgentResult:
        started_at = datetime.now(timezone.utc).isoformat()
        max_listings = (
            stop_conditions.max_items
            if stop_conditions and stop_conditions.max_items is not None
            else config.max_fetch_listings
        )

        source_summaries: list[str] = []
        all_rows: list[dict[str, Any]] = []

        for source_name in config.sources:
            source_cls = SOURCES.get(source_name)
            if source_cls is None:
                msg = f"Source '{source_name}' not found in registry — skipping."
                print(f"  ⚠  {msg}")
                source_summaries.append(f"{source_name}: SKIPPED (not registered)")
                storage.log_cycle(
                    path=db_path,
                    agent=f"Fetcher:{source_name}",
                    started_at=started_at,
                    finished_at=datetime.now(timezone.utc).isoformat(),
                    records_touched=0,
                    status="failed",
                    notes=msg,
                )
                continue

            src_start = datetime.now(timezone.utc).isoformat()
            try:
                rows = source_cls().fetch(config)
                src_count = len(rows)
                all_rows.extend(rows)
                src_finish = datetime.now(timezone.utc).isoformat()
                storage.log_cycle(
                    path=db_path,
                    agent=f"Fetcher:{source_name}",
                    started_at=src_start,
                    finished_at=src_finish,
                    records_touched=src_count,
                    status="ok",
                    notes=f"{src_count} rows fetched.",
                )
                source_summaries.append(f"{source_name}: {src_count} rows fetched")

            except (SourceError, Exception) as exc:
                src_finish = datetime.now(timezone.utc).isoformat()
                err_msg = f"{type(exc).__name__}: {exc}"
                print(f"  ⚠  Source '{source_name}' failed — {err_msg}")
                storage.log_cycle(
                    path=db_path,
                    agent=f"Fetcher:{source_name}",
                    started_at=src_start,
                    finished_at=src_finish,
                    records_touched=0,
                    status="failed",
                    notes=err_msg,
                )
                source_summaries.append(f"{source_name}: FAILED ({type(exc).__name__})")

        # Stamp every row with the canonical stable id before writing
        # Respect max_listings stop condition
        if len(all_rows) > max_listings:
            all_rows = all_rows[:max_listings]
        for row in all_rows:
            row["id"] = storage.make_listing_id(row["source"], row["url"])

        new_count = storage.upsert_listings(db_path, all_rows)
        deduped = len(all_rows) - new_count

        # Build per-source new-row breakdown for the notes string
        notes = " | ".join(source_summaries)
        if all_rows:
            notes += f" | total: {len(all_rows)} offered, {new_count} new, {deduped} deduped"

        finished_at = datetime.now(timezone.utc).isoformat()
        storage.log_cycle(
            path=db_path,
            agent=self.name,
            started_at=started_at,
            finished_at=finished_at,
            records_touched=new_count,
            status="ok",
            notes=notes,
        )

        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=new_count,
            notes=notes,
        )
