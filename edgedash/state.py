"""
state.py — cheap read of system state from the database.

`now` is a parameter, never datetime.now() inside — so this function is
fully testable with a fixed clock. All queries are counts and MAX(timestamp):
no full table scans, no row fetches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import edgedash.storage as storage
from edgedash.config import Config


@dataclass
class SystemState:
    # Fetch state
    last_fetch_at: str | None           # ISO-8601 or None
    hours_since_fetch: float            # 0.0 if never fetched → triggers fetch

    # Scoring state
    unscored_count: int

    # Gap analysis state
    gaps_computed_at: str | None        # ISO-8601 of most recent snapshot, or None
    gaps_stale: bool                    # True if any scored listing is newer than snapshot

    # Last cycle
    last_cycle_verdict: str | None      # "ok" / "partial" / "failed" / None
    last_cycle_at: str | None           # ISO-8601 or None


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 string to a timezone-aware datetime, or return None."""
    if ts is None:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _hours_between(earlier: datetime | None, later: datetime) -> float:
    """Return elapsed hours from earlier to later, or a large number if earlier is None."""
    if earlier is None:
        return 9999.0
    delta = later - earlier
    return delta.total_seconds() / 3600.0


def read_state(config: Config, now: datetime) -> SystemState:
    """
    Read current system state from the database.

    `now` is passed in — never call datetime.now() here. That makes
    this function deterministic and testable with a fixed clock.
    All storage calls are cheap: MAX(timestamp) and COUNT(*) only.
    """
    path = config.db_path

    last_fetch_at     = storage.last_fetch_time(path)
    unscored_count    = storage.count_unscored(path)
    gaps_computed_at  = storage.latest_gap_time(path)
    latest_scored_at  = storage.latest_score_time(path)
    cycle_summary     = storage.last_cycle_summary(path)

    hours_since_fetch = _hours_between(_parse_iso(last_fetch_at), now)

    # Gaps are stale if any listing was scored after the last gap run
    gaps_stale = False
    if gaps_computed_at is None:
        gaps_stale = True   # never run — treat as stale
    elif latest_scored_at is not None:
        gaps_stale = latest_scored_at > gaps_computed_at

    last_cycle_verdict = cycle_summary["status"] if cycle_summary else None
    last_cycle_at      = cycle_summary["started_at"] if cycle_summary else None

    return SystemState(
        last_fetch_at      = last_fetch_at,
        hours_since_fetch  = round(hours_since_fetch, 2),
        unscored_count     = unscored_count,
        gaps_computed_at   = gaps_computed_at,
        gaps_stale         = gaps_stale,
        last_cycle_verdict = last_cycle_verdict,
        last_cycle_at      = last_cycle_at,
    )
