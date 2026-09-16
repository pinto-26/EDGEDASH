"""
config.py — loads project configuration from config.yaml at the repo root.

All user-specific values (role, city, skills, etc.) live here.
No other module should define or hardcode these values.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# PyYAML is the only dependency added here. The standard library has no YAML
# parser; writing one is not a real option. `pip install pyyaml` is one line.
import yaml

# Repo root is two levels up from this file: edgedash/edgedash/config.py
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _REPO_ROOT / "config.yaml"


@dataclass
class Config:
    target_role: str = "Data Analyst"
    target_city: str = "Bengaluru"
    keywords: list[str] = field(default_factory=list)
    my_skills: list[str] = field(default_factory=list)
    experience_years: int = 2
    db_path: str = "edgedash.db"
    min_fit_score: int = 60
    sources: list[str] = field(default_factory=lambda: ["arbeitnow"])
    use_mock_fetcher: bool = False
    llm_provider: str = "gemini"
    llm_model: str = "gemini-3.6-flash"
    llm_batch_size: int = 25
    # Scoring
    target_seniority: str = "mid"
    score_batch_size: int = 25
    weight_skill_match: float = 0.45
    weight_seniority_fit: float = 0.25
    weight_location_fit: float = 0.15
    weight_recency: float = 0.15
    # Skill canonicalisation
    skill_aliases: dict[str, str] = field(default_factory=dict)
    # Orchestration thresholds
    fetch_interval_hours: float = 6.0
    max_fetch_pages: int = 5
    max_fetch_listings: int = 200
    max_score_seconds: int = 300
    max_analyse_seconds: int = 60
    # Verification thresholds (rule 39) — every threshold names the failure it catches
    # Score distribution checks — catch score inflation / model collapse
    min_score_spread: int = 10          # catches: all scores within 10 points (inflated)
    min_score_stdev: float = 5.0        # catches: tight clustering that spread misses
    # Extraction sanity checks — catch a broken or hallucinating extractor
    max_empty_extraction_pct: float = 20.0   # catches: extractor silently returning nothing
    max_skills_per_listing: int = 20         # catches: model dumping a sentence as skill list
    # Gap analysis checks — catch ranking from too little data
    min_gap_sample: int = 3             # catches: top gap ranked from fewer than 3 listings
    # Freshness check — catch stale data reaching the dashboard
    max_data_age_days: int = 3          # catches: dashboard showing listings older than 3 days
    # Natural language query abuse guards
    daily_ask_limit: int = 200          # global daily cap — protects free-tier API quota


def _parse_config(raw: dict[str, Any]) -> Config:
    """Build a Config from a raw YAML dict, applying per-field defaults."""
    return Config(
        target_role=raw.get("target_role", Config.target_role),
        target_city=raw.get("target_city", Config.target_city),
        keywords=raw.get("keywords", []),
        my_skills=raw.get("my_skills", []),
        experience_years=int(raw.get("experience_years", Config.experience_years)),
        db_path=raw.get("db_path", Config.db_path),
        min_fit_score=int(raw.get("min_fit_score", Config.min_fit_score)),
        sources=raw.get("sources", ["arbeitnow"]),
        use_mock_fetcher=bool(raw.get("use_mock_fetcher", False)),
        llm_provider=raw.get("llm_provider", "gemini"),
        llm_model=raw.get("llm_model", "gemini-3.6-flash"),
        llm_batch_size=int(raw.get("llm_batch_size", 25)),
        target_seniority=raw.get("target_seniority", "mid"),
        score_batch_size=int(raw.get("score_batch_size", 25)),
        weight_skill_match=float(raw.get("weight_skill_match", 0.45)),
        weight_seniority_fit=float(raw.get("weight_seniority_fit", 0.25)),
        weight_location_fit=float(raw.get("weight_location_fit", 0.15)),
        weight_recency=float(raw.get("weight_recency", 0.15)),
        skill_aliases={
            str(k).lower().strip(): str(v).lower().strip()
            for k, v in (raw.get("skill_aliases") or {}).items()
        },
        fetch_interval_hours=float(raw.get("fetch_interval_hours", 6.0)),
        max_fetch_pages=int(raw.get("max_fetch_pages", 5)),
        max_fetch_listings=int(raw.get("max_fetch_listings", 200)),
        max_score_seconds=int(raw.get("max_score_seconds", 300)),
        max_analyse_seconds=int(raw.get("max_analyse_seconds", 60)),
        min_score_spread=int(raw.get("min_score_spread", 10)),
        min_score_stdev=float(raw.get("min_score_stdev", 5.0)),
        max_empty_extraction_pct=float(raw.get("max_empty_extraction_pct", 20.0)),
        max_skills_per_listing=int(raw.get("max_skills_per_listing", 20)),
        min_gap_sample=int(raw.get("min_gap_sample", 3)),
        max_data_age_days=int(raw.get("max_data_age_days", 3)),
        daily_ask_limit=int(raw.get("daily_ask_limit", 200)),
    )


def load_config(path: Path | None = None) -> Config:
    """
    Load Config from a YAML file.

    Defaults to config.yaml at the repo root.
    Raises FileNotFoundError with a clear message if the file is missing.
    """
    config_path = path or _CONFIG_PATH

    if not config_path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at '{config_path}'.\n"
            "Copy config.yaml.example to config.yaml and fill in your details."
        )

    with config_path.open("r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    return _parse_config(raw)
