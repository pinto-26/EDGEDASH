"""
gaps.py — morning read: latest gap snapshot as a terminal table.

Usage:
    python -m edgedash.gaps              latest snapshot
    python -m edgedash.gaps --trend      compare earliest vs latest snapshot
    python -m edgedash.gaps --history N  show last N snapshots (timestamps only)
"""

from __future__ import annotations

import argparse
import sqlite3

import edgedash.storage as storage
from edgedash.config import load_config

_W = 78


def _bar(value: float, max_value: float, width: int = 18) -> str:
    if max_value == 0:
        return " " * width
    filled = round((value / max_value) * width)
    return "█" * filled + "░" * (width - filled)


def _print_snapshot(rows: list[dict]) -> None:
    if not rows:
        print("\n  No gap snapshot found. Run a full cycle first.\n")
        return

    computed_at = rows[0]["computed_at"][:19].replace("T", " ")
    max_cost    = rows[0]["opportunity_cost"]  # already sorted desc
    total_shown = len(rows)

    print(f"\n{'─' * _W}")
    print(f"  Skill Gap Report  ·  snapshot {computed_at} UTC  ·  top {total_shown}")
    print(f"{'─' * _W}")
    print(
        f"  {'#':>2}  {'SKILL':<24}  {'BLK':>4}  {'COST':>5}  "
        f"{'AVG':>4}  {'TOP':>4}  {'BAR + CONFIDENCE'}"
    )
    print(
        f"  {'─'*2}  {'─'*24}  {'─'*4}  {'─'*5}  "
        f"{'─'*4}  {'─'*4}  {'─'*22}"
    )

    for rank, row in enumerate(rows, 1):
        skill    = row["skill"][:24]
        blocked  = row["listings_blocked"]
        cost     = row["opportunity_cost"]
        mean_s   = row["mean_score"]
        top_s    = row["top_score"]
        bar      = _bar(cost, max_cost)
        nth      = row["also_nice_to_have"]
        conf_tag = " ⚠ low-conf" if row["low_confidence"] else ""
        nth_tag  = f" +{nth}▵" if nth else ""

        print(
            f"  {rank:>2}  {skill:<24}  {blocked:>4}  {cost:>5.1f}  "
            f"{mean_s:>4.0f}  {top_s:>4}  {bar}{conf_tag}{nth_tag}"
        )

    print(f"{'─' * _W}")
    print(
        f"  BLK=listings blocked  COST=opportunity cost  "
        f"AVG=mean score  TOP=highest score"
    )
    print(f"  ▵ = also appears as nice-to-have  ⚠ = fewer than 3 listings (low confidence)")
    print(f"{'─' * _W}\n")


def _print_trend(
    earliest_at: str,
    earliest_rows: list[dict],
    latest_at: str,
    latest_rows: list[dict],
) -> None:
    """
    Compare earliest vs latest snapshot for the current top-10 skills.
    Marks NEW skills (not in earliest) and skills that have DROPPED OUT.
    Only uses data that actually exists — no interpolation or extrapolation.
    """
    top10_latest          = latest_rows[:10]
    top10_skills          = {r["skill"] for r in top10_latest}
    earliest_top10_skills = {r["skill"] for r in earliest_rows[:10]}

    earliest_by_skill = {r["skill"]: r for r in earliest_rows}
    latest_by_skill   = {r["skill"]: r for r in latest_rows}

    dropped_out = earliest_top10_skills - top10_skills

    e_date = earliest_at[:19].replace("T", " ")
    l_date = latest_at[:19].replace("T", " ")

    print(f"\n{'─' * _W}")
    print(f"  Skill Gap Trend")
    print(f"  Earliest snapshot : {e_date} UTC")
    print(f"  Latest snapshot   : {l_date} UTC")
    print(f"{'─' * _W}")
    print(
        f"  {'#':>2}  {'SKILL':<24}  {'EARLIEST':>8}  {'LATEST':>8}  "
        f"{'CHANGE':>8}  {'PCT':>6}  NOTE"
    )
    print(
        f"  {'─'*2}  {'─'*24}  {'─'*8}  {'─'*8}  "
        f"{'─'*8}  {'─'*6}  {'─'*8}"
    )

    for rank, row in enumerate(top10_latest, 1):
        skill       = row["skill"][:24]
        latest_cost = row["opportunity_cost"]
        early_row   = earliest_by_skill.get(row["skill"])

        if early_row is None:
            print(
                f"  {rank:>2}  {skill:<24}  {'—':>8}  {latest_cost:>8.2f}  "
                f"{'—':>8}  {'—':>6}  NEW"
            )
        else:
            early_cost = early_row["opportunity_cost"]
            change     = latest_cost - early_cost
            pct        = (change / early_cost * 100) if early_cost > 0 else 0.0
            arrow      = "↑" if change > 0.05 else ("↓" if change < -0.05 else "→")
            print(
                f"  {rank:>2}  {skill:<24}  {early_cost:>8.2f}  {latest_cost:>8.2f}  "
                f"  {change:>+7.2f}  {pct:>+5.1f}%  {arrow}"
            )

    if dropped_out:
        print(f"\n  DROPPED OUT of top 10 since {e_date}:")
        for skill in sorted(dropped_out):
            latest_r = latest_by_skill.get(skill)
            if latest_r:
                idx  = next(
                    (i + 1 for i, r in enumerate(latest_rows) if r["skill"] == skill),
                    None
                )
                note = f"now rank {idx} · cost {latest_r['opportunity_cost']:.2f}"
            else:
                note = "no longer present"
            print(f"    • {skill:<28} {note}")

    print(f"\n{'─' * _W}")
    print(f"  COST = opportunity cost (sum of fit_score/100 per blocked listing)")
    print(f"  ↑ growing  ↓ shrinking  → stable  NEW = first appeared this window")
    print(f"{'─' * _W}\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print the latest skill gap snapshot."
    )
    parser.add_argument(
        "--trend", action="store_true",
        help="Compare earliest vs latest snapshot to show gap movement.",
    )
    parser.add_argument(
        "--history", type=int, metavar="N",
        help="List the last N gap snapshot timestamps instead of printing the latest.",
    )
    args = parser.parse_args()

    cfg = load_config()

    if args.trend:
        n = storage.get_snapshot_count(cfg.db_path)

        if n == 0:
            print("\n  No gap snapshots yet. Run a full cycle first.\n")
            return

        if n == 1:
            _, computed_at, _ = storage.get_gap_snapshot_by_position(
                cfg.db_path, "latest"
            )
            ts = computed_at[:19].replace("T", " ")
            print(f"\n  Only 1 snapshot so far (taken {ts} UTC).")
            print(f"  Trend requires at least 2 snapshots.")
            print(f"  Run the cycle again tomorrow — 1 more day needed.")
            print()
            return

        _, e_at, e_rows = storage.get_gap_snapshot_by_position(cfg.db_path, "earliest")
        _, l_at, l_rows = storage.get_gap_snapshot_by_position(cfg.db_path, "latest")
        _print_trend(e_at, e_rows, l_at, l_rows)
        return

    if args.history:
        conn = sqlite3.connect(cfg.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT run_id, computed_at, COUNT(*) as gap_count
            FROM gap_snapshots
            GROUP BY run_id
            ORDER BY computed_at DESC
            LIMIT ?
        """, (args.history,)).fetchall()
        conn.close()

        if not rows:
            print("\n  No gap snapshots found.\n")
            return

        print(f"\n{'─' * _W}")
        print(f"  Gap snapshot history (last {args.history})")
        print(f"{'─' * _W}")
        for r in rows:
            ts = r["computed_at"][:19].replace("T", " ")
            print(f"  {ts} UTC  ·  {r['gap_count']} skills  ·  run {r['run_id'][:12]}…")
        print(f"{'─' * _W}\n")
        return

    rows = storage.get_latest_gap_snapshot(cfg.db_path)
    _print_snapshot(rows)


if __name__ == "__main__":
    main()
