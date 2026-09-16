"""
base.py — Source ABC and the global source registry.

To add a new source:
  1. Create a new module in edgedash/sources/.
  2. Decorate the class with @register.
  3. Import the module anywhere before the Fetcher runs (e.g. at the top of
     fetcher.py). Nothing else needs to change.

Normalised row schema (steering rule 10):
  source, external_id, title, company, location, url,
  description, posted_at, raw
  Missing values must be None — never "" or "N/A".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from edgedash.config import Config

# ---------------------------------------------------------------------------
# Normalised key schema — every Source must return dicts with exactly these keys
# ---------------------------------------------------------------------------

NORMALISED_KEYS: tuple[str, ...] = (
    "source",
    "external_id",
    "title",
    "company",
    "location",
    "url",
    "description",
    "posted_at",
    "raw",
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

SOURCES: dict[str, type[Source]] = {}


def register(cls: type[Source]) -> type[Source]:
    """
    Class decorator that registers a Source under its name.

    Usage:
        @register
        class MySource(Source):
            name = "mysource"
            ...
    """
    SOURCES[cls.name] = cls
    return cls


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class Source(ABC):
    """Base class for all job-board sources."""

    name: str  # must be overridden as a class attribute

    @abstractmethod
    def fetch(self, config: Config) -> list[dict[str, Any]]:
        """
        Fetch listings and return them as normalised dicts.

        Each dict must contain exactly the keys in NORMALISED_KEYS.
        Missing values are None, never "" or "N/A".
        """
        ...
