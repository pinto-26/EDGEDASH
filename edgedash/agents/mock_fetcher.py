"""
mock_fetcher.py — stub Fetcher that returns realistic fake listings.

No network calls. Used during development and for deduplication testing.

Dedup guarantee: the first 4 listings in STABLE_LISTINGS have fixed URLs and
source values, so their IDs are identical across every run. On the second run,
upsert_listings will INSERT OR IGNORE them and the new-row count will drop by 4.
"""

from __future__ import annotations

from datetime import datetime, timezone

import edgedash.storage as storage
from edgedash.agents.base import Agent, AgentResult
from edgedash.config import Config

# ---------------------------------------------------------------------------
# Stable listings — same id on every run, proving dedup works
# ---------------------------------------------------------------------------

_STABLE_LISTINGS: list[dict] = [
    {
        "title": "Data Analyst",
        "company": "Flipkart",
        "location": "Bengaluru",
        "url": "https://careers.flipkart.com/jobs/data-analyst-001",
        "description": (
            "Work with large-scale transactional data using SQL and Python. "
            "Build dashboards in Power BI. Partner with product teams to define KPIs."
        ),
        "source": "mock",
        "posted_at": "2026-08-10",
    },
    {
        "title": "Junior Data Analyst",
        "company": "Infosys",
        "location": "Bengaluru",
        "url": "https://infosys.com/careers/jobs/junior-da-bengaluru-42",
        "description": (
            "Support business intelligence reporting using Excel, SQL, and Tableau. "
            "0-2 years experience. Exposure to ETL pipelines is a plus."
        ),
        "source": "mock",
        "posted_at": "2026-08-09",
    },
    {
        "title": "Data Analyst – Growth",
        "company": "Swiggy",
        "location": "Bengaluru",
        "url": "https://careers.swiggy.com/openings/data-analyst-growth",
        "description": (
            "Analyse user funnel and retention metrics. Write complex SQL queries, "
            "build Python scripts for ad-hoc analysis, and present findings to leadership."
        ),
        "source": "mock",
        "posted_at": "2026-08-11",
    },
    {
        "title": "Business Intelligence Analyst",
        "company": "Razorpay",
        "location": "Bengaluru",
        "url": "https://razorpay.com/jobs/bi-analyst-blr",
        "description": (
            "Own the BI layer for the payments platform. Strong SQL required. "
            "Experience with Looker or Metabase preferred. Python scripting a bonus."
        ),
        "source": "mock",
        "posted_at": "2026-08-08",
    },
]

# ---------------------------------------------------------------------------
# Variable listings — unique URLs ensure fresh ids each run
# (in production the Fetcher would pull genuinely new listings here)
# ---------------------------------------------------------------------------

_VARIABLE_LISTINGS: list[dict] = [
    {
        "title": "Senior Data Analyst",
        "company": "PhonePe",
        "location": "Bengaluru",
        "url": "https://phonepe.com/careers/senior-da-2026-08",
        "description": (
            "Lead analytics for the merchant platform. Mentor junior analysts. "
            "Expert-level SQL, Python pandas, and data visualisation required. "
            "3+ years experience."
        ),
        "source": "mock",
        "posted_at": "2026-08-12",
    },
    {
        "title": "Data Analyst – Risk",
        "company": "CRED",
        "location": "Bengaluru",
        "url": "https://cred.club/jobs/data-analyst-risk-aug26",
        "description": (
            "Use statistical modelling to detect anomalies in credit card spend. "
            "Python (scikit-learn, pandas), SQL, and strong communication skills needed."
        ),
        "source": "mock",
        "posted_at": "2026-08-13",
    },
    {
        "title": "Product Analyst",
        "company": "Meesho",
        "location": "Bengaluru",
        "url": "https://meesho.io/careers/product-analyst-blr-2026",
        "description": (
            "Define and track product metrics for the seller platform. "
            "SQL proficiency is non-negotiable. A/B testing experience preferred."
        ),
        "source": "mock",
        "posted_at": "2026-08-11",
    },
    {
        "title": "Data Analyst – Supply Chain",
        "company": "Amazon",
        "location": "Bengaluru",
        "url": "https://amazon.jobs/en/jobs/supply-chain-da-blr-aug2026",
        "description": (
            "Analyse inventory and fulfilment data. Build automated reports with Python. "
            "Work with Redshift and QuickSight. 1-3 years experience."
        ),
        "source": "mock",
        "posted_at": "2026-08-10",
    },
    {
        "title": "Analytics Engineer",
        "company": "Zepto",
        "location": "Bengaluru",
        "url": "https://zepto.com/careers/analytics-engineer-2026-q3",
        "description": (
            "Bridge data engineering and analytics. Build dbt models, write Python "
            "pipelines, and maintain the Airflow DAGs that power our dashboards."
        ),
        "source": "mock",
        "posted_at": "2026-08-14",
    },
    {
        "title": "Data Analyst – Marketing",
        "company": "Myntra",
        "location": "Bengaluru",
        "url": "https://myntra.com/jobs/marketing-analyst-blr-aug26",
        "description": (
            "Measure campaign ROI and attribution across channels. "
            "Google Analytics, SQL, and Python required. Power BI experience a plus."
        ),
        "source": "mock",
        "posted_at": "2026-08-09",
    },
    {
        "title": "Junior BI Developer",
        "company": "Wipro",
        "location": "Bengaluru",
        "url": "https://wipro.com/careers/junior-bi-developer-blr-2026",
        "description": (
            "Build Power BI and Tableau dashboards for enterprise clients. "
            "SQL and Excel proficiency required. DAX knowledge is a strong plus."
        ),
        "source": "mock",
        "posted_at": "2026-08-07",
    },
    {
        "title": "Data Analyst – Fintech",
        "company": "BharatPe",
        "location": "Bengaluru",
        "url": "https://bharatpe.com/careers/data-analyst-fintech-2026",
        "description": (
            "Analyse merchant transaction patterns. Python, SQL, and basic ML "
            "familiarity expected. Fast-paced startup environment."
        ),
        "source": "mock",
        "posted_at": "2026-08-13",
    },
]

# ---------------------------------------------------------------------------
# Agent implementation
# ---------------------------------------------------------------------------

class MockFetcher:
    name: str = "MockFetcher"

    def run(self, config: Config, db_path: str) -> AgentResult:
        started_at = datetime.now(timezone.utc).isoformat()

        all_listings = _STABLE_LISTINGS + _VARIABLE_LISTINGS  # 4 + 8 = 12

        try:
            new_count = storage.upsert_listings(db_path, all_listings)
            status = "ok"
            notes = (
                f"Offered {len(all_listings)} listings to storage; "
                f"{new_count} were new (dedup dropped {len(all_listings) - new_count})."
            )
        except Exception as exc:
            new_count = 0
            status = "failed"
            notes = f"upsert_listings raised: {exc}"
            raise

        finished_at = datetime.now(timezone.utc).isoformat()
        storage.log_cycle(
            path=db_path,
            agent=self.name,
            started_at=started_at,
            finished_at=finished_at,
            records_touched=new_count,
            status=status,
            notes=notes,
        )

        return AgentResult(
            agent=self.name,
            status=status,
            records_touched=new_count,
            notes=notes,
        )
