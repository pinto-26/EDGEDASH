"""
rescore.py — manual escape hatch for clearing scores before a re-run.

Rule 18 says never re-score automatically. This command is intentional and
manual only. It clears fit_score and fit_reason but NEVER touches the
extraction cache — so re-scoring costs zero API calls.

Usage:
    python -m edgedash.rescore --id <listing_id>   clear one listing
    python -m edgedash.rescore --all               clear every listing (confirms first)
"""

from __future__ import annotations

import argparse
import sys

import edgedash.storage as storage
from edgedash.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Manually clear scores so the next cycle re-scores them. "
                    "The extraction cache is never touched — re-scoring is free."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--all",
        action="store_true",
        help="Clear every listing's score (requires confirmation).",
    )
    group.add_argument(
        "--id",
        metavar="LISTING_ID",
        help="Clear the score for one specific listing.",
    )
    args = parser.parse_args()

    cfg = load_config()

    if args.all:
        # Refuse to run without explicit confirmation
        print("This will clear scores for ALL listings in the database.")
        print("The extraction cache is preserved — re-scoring costs zero API calls.")
        answer = input("Type 'yes' to confirm: ").strip().lower()
        if answer != "yes":
            print("Aborted.")
            sys.exit(0)

        count = storage.clear_all_scores(cfg.db_path)
        print(f"\nCleared {count} score(s).")

    else:
        listing_id = args.id
        count = storage.clear_score(cfg.db_path, listing_id)
        if count == 0:
            print(f"No listing found with id '{listing_id}'.")
            sys.exit(1)
        print(f"\nCleared score for listing '{listing_id}'.")

    print("Run  python run_cycle.py  (or  python -m edgedash.agents.scorer)")
    print("to re-score. Extraction cache is intact — no API calls needed.")


if __name__ == "__main__":
    main()
