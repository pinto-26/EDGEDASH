"""
arbeitnow.py — Arbeitnow public job board source.

API docs: https://www.arbeitnow.com/api/job-board-api
No API key or signup required.

Paging strategy:
  - Fetch page 1 unconditionally.
  - Continue fetching while results keep matching config.keywords,
    up to MAX_PAGES pages total.

Filtering strategy (steering rule 12 — a dead source never kills the cycle):
  - Primary filter: keyword match AND city match.
  - If that would leave fewer than MIN_RESULTS, relax the city filter,
    log that we did, and keep keyword-matched results regardless of location.
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

from edgedash.config import Config
from edgedash.sources.base import Source, register
from edgedash.sources.http import SourceError, get_json

logger = logging.getLogger(__name__)

_API_URL    = "https://www.arbeitnow.com/api/job-board-api"
_MAX_PAGES  = 5
_MIN_RESULTS = 5
_RATE_LIMIT  = 1.0  # seconds between page requests (steering rule 14)


def _to_iso(ts: int | str | None) -> str | None:
    """Convert a Unix timestamp or ISO string to a normalised ISO-8601 string."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
    except (ValueError, TypeError):
        return str(ts) or None


def _strip_html(raw: str | None) -> str | None:
    """Remove HTML tags and decode entities from a description string."""
    if raw is None:
        return None
    no_tags = re.sub(r"<[^>]+>", " ", raw)
    decoded = html.unescape(no_tags)
    collapsed = re.sub(r"\s+", " ", decoded).strip()
    return collapsed or None


def _keyword_match(job: dict[str, Any], keywords: list[str]) -> bool:
    """Return True if any keyword appears in title, description, or tags."""
    if not keywords:
        return True
    haystack = " ".join([
        (job.get("title") or ""),
        (job.get("description") or ""),
        " ".join(job.get("tags") or []),
    ]).lower()
    return any(kw.lower() in haystack for kw in keywords)


def _city_match(job: dict[str, Any], city: str) -> bool:
    """Return True if the city appears in the location field, or job is remote."""
    if job.get("remote"):
        return True
    location = (job.get("location") or "").lower()
    return city.lower() in location


def _normalise(job: dict[str, Any], source_name: str) -> dict[str, Any]:
    """Map one Arbeitnow API job dict onto our canonical schema."""
    return {
        "source":      source_name,
        "external_id": job.get("slug") or None,
        "title":       job.get("title") or None,
        "company":     job.get("company_name") or None,
        "location":    job.get("location") or None,
        "url":         job.get("url") or None,
        "description": _strip_html(job.get("description")),
        "posted_at":   _to_iso(job.get("created_at")),
        "raw":         job,
    }


@register
class ArbeitnowSource(Source):
    name: str = "arbeitnow"

    def fetch(self, config: Config) -> list[dict[str, Any]]:
        all_raw: list[dict[str, Any]] = []

        for page in range(1, _MAX_PAGES + 1):
            if page > 1:
                time.sleep(_RATE_LIMIT)

            data = get_json(_API_URL, params={"page": page})
            jobs: list[dict[str, Any]] = data.get("data", [])

            if not jobs:
                logger.info("arbeitnow: page %d returned no jobs — stopping.", page)
                break

            all_raw.extend(jobs)

            # Stop paging early if this page had no keyword matches at all
            page_matches = [j for j in jobs if _keyword_match(j, config.keywords)]
            if not page_matches:
                logger.info(
                    "arbeitnow: page %d had no keyword matches — stopping early.", page
                )
                break

        print(f"  [arbeitnow] {len(all_raw)} raw results fetched across pages.")

        # --- primary filter: keywords AND city ---
        strict = [
            j for j in all_raw
            if _keyword_match(j, config.keywords) and _city_match(j, config.target_city)
        ]

        if len(strict) >= _MIN_RESULTS:
            filtered = strict
            print(
                f"  [arbeitnow] {len(filtered)} results after keyword + city filter "
                f"(city: {config.target_city})."
            )
        else:
            # Relax city filter — keep keyword matches regardless of location
            filtered = [j for j in all_raw if _keyword_match(j, config.keywords)]
            print(
                f"  [arbeitnow] City filter ('{config.target_city}') left only "
                f"{len(strict)} result(s) — relaxed to keyword-only. "
                f"{len(filtered)} results kept (includes remote / other locations)."
            )

        return [_normalise(j, self.name) for j in filtered]
