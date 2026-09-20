"""
diagnose.py -- read-only diagnostic tool for inspecting DB state.

Usage:
    python -m edgedash.diagnose --scores     print scored listings table + distribution
    python -m edgedash.diagnose --cycles     print recent cycle_log entries
    python -m edgedash.diagnose --gaps       print extraction cache skill summary
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Any

from edgedash.config import load_config

_W = 72  # console width


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _banner(text: str) -> None:
    print(f"\n{'-' * _W}")
    print(f"  {text}")
    print(f"{'-' * _W}")


# ---------------------------------------------------------------------------
# --scores
# ---------------------------------------------------------------------------

def cmd_scores(db_path: str, min_score: int = 0) -> None:
    conn = _connect(db_path)

    rows = conn.execute("""
        SELECT title, company, location, fit_score, fit_reason, posted_at
        FROM   listings
        WHERE  fit_score IS NOT NULL
        ORDER  BY fit_score DESC
    """).fetchall()

    unscored = conn.execute(
        "SELECT COUNT(*) FROM listings WHERE fit_score IS NULL"
    ).fetchone()[0]

    conn.close()

    _banner(f"Scored listings  ({len(rows)} scored, {unscored} unscored)")

    if not rows:
        print("  No scored listings yet.")
        return

    # Table
    fmt = "  {score:>5}  {title:<28}  {company:<18}  {loc:<14}  {reason}"
    print(fmt.format(
        score="SCORE", title="TITLE", company="COMPANY",
        loc="LOCATION", reason="REASON"
    ))
    print(f"  {'-'*5}  {'-'*28}  {'-'*18}  {'-'*14}  {'-'*24}")

    for r in rows:
        title   = (r["title"]    or "")[:28]
        company = (r["company"]  or "")[:18]
        loc     = (r["location"] or "")[:14]
        reason  = (r["fit_reason"] or "")[:55]
        print(fmt.format(
            score=r["fit_score"], title=title,
            company=company, loc=loc, reason=reason
        ))

    # Distribution
    scores = [r["fit_score"] for r in rows]
    lo     = min(scores)
    hi     = max(scores)
    mean   = round(sum(scores) / len(scores))
    spread = hi - lo

    print(f"\n  {'-' * (_W - 2)}")
    print(f"  count={len(scores)}  min={lo}  max={hi}  mean={mean}  spread={spread}", end="")
    if spread < 10:
        print("  SUSPECT -- spread < 10", end="")
    print(f"\n{'-' * _W}\n")


# ---------------------------------------------------------------------------
# --cycles
# ---------------------------------------------------------------------------

def cmd_cycles(db_path: str, limit: int = 20) -> None:
    conn = _connect(db_path)
    rows = conn.execute("""
        SELECT id, agent, started_at, records_touched, status, notes
        FROM   cycle_log
        ORDER  BY id DESC
        LIMIT  ?
    """, (limit,)).fetchall()
    conn.close()

    _banner(f"Recent cycle_log entries  (last {limit})")

    if not rows:
        print("  No entries yet.")
        return

    for r in rows:
        icon = "OK" if r["status"] in ("ok",) else "!!"
        ts   = (r["started_at"] or "")[:19].replace("T", " ")
        note = (r["notes"] or "")[:60]
        print(f"  {icon} [{r['id']:>4}] {ts}  {r['agent']:<28}  "
              f"touched={r['records_touched']:>3}  {note}")

    print(f"{'-' * _W}\n")


# ---------------------------------------------------------------------------
# --gaps  (extraction cache skill frequency)
# ---------------------------------------------------------------------------

def cmd_gaps(db_path: str) -> None:
    import json

    conn = _connect(db_path)
    rows = conn.execute(
        "SELECT extracted_json FROM extraction_cache"
    ).fetchall()
    conn.close()

    freq: dict[str, int] = {}
    for r in rows:
        try:
            data = json.loads(r["extracted_json"])
        except (ValueError, KeyError):
            continue
        for skill in (data.get("required_skills") or []):
            freq[skill] = freq.get(skill, 0) + 1

    _banner(f"Skill frequency across {len(rows)} cached extractions")

    if not freq:
        print("  No cached extractions yet.")
        return

    for skill, count in sorted(freq.items(), key=lambda x: -x[1])[:30]:
        bar = "#" * count
        print(f"  {skill:<30} {bar} {count}")

    print(f"{'-' * _W}\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config()

    parser = argparse.ArgumentParser(description="EdgeDash diagnostic tool.")
    parser.add_argument("--scores", action="store_true", help="Show scored listings.")
    parser.add_argument("--cycles", action="store_true", help="Show recent cycle_log.")
    parser.add_argument("--gaps",   action="store_true", help="Show skill frequency from cache.")
    parser.add_argument("--all",    action="store_true", help="Run all diagnostics.")
    args = parser.parse_args()

    if not any([args.scores, args.cycles, args.gaps, args.all]):
        parser.print_help()
        return

    if args.scores or args.all:
        cmd_scores(cfg.db_path)
    if args.cycles or args.all:
        cmd_cycles(cfg.db_path)
    if args.gaps or args.all:
        cmd_gaps(cfg.db_path)


if __name__ == "__main__":
    main()
