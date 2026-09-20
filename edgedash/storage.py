"""
storage.py — the ONLY module permitted to touch the database.

Backend selection (rule 47, 48):
  - If DATABASE_URL is set in the environment, uses Postgres via psycopg2.
  - Otherwise falls back to local SQLite for offline development.
  - Which backend is active is logged to stderr at startup, every time.
  - DATABASE_URL is read once here and nowhere else (rule 48).

Every public function signature is identical regardless of backend.
All dialect differences (placeholders, autoincrement, upsert, booleans,
timestamps) are handled inside this module only (rule 2).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Generator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend detection  (read DATABASE_URL exactly once)
# ---------------------------------------------------------------------------

_DATABASE_URL: str | None = os.environ.get("DATABASE_URL")

# Supabase and most hosted Postgres require SSL. If the URL doesn't already
# include sslmode, append ?sslmode=require automatically.
if _DATABASE_URL and "sslmode" not in _DATABASE_URL:
    _DATABASE_URL = _DATABASE_URL.rstrip("?&") + (
        "?sslmode=require" if "?" not in _DATABASE_URL else "&sslmode=require"
    )

if _DATABASE_URL:
    import psycopg2
    import psycopg2.extras
    _BACKEND = "postgres"
    logger.info("storage: using Postgres backend (DATABASE_URL is set)")
    print("storage: backend = postgres", flush=True)
else:
    import sqlite3 as _sqlite3
    _BACKEND = "sqlite"
    logger.info("storage: using SQLite backend (DATABASE_URL not set)")
    print("storage: backend = sqlite", flush=True)


# ---------------------------------------------------------------------------
# Connection context managers
# ---------------------------------------------------------------------------

@contextmanager
def _pg_conn() -> Generator:
    """Yield a psycopg2 connection with RealDictCursor, auto-commit on exit."""
    conn = psycopg2.connect(_DATABASE_URL)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def _sqlite_conn(path: str) -> Generator:
    """Yield a sqlite3 connection with Row factory, auto-commit on exit."""
    conn = _sqlite3.connect(path)
    conn.row_factory = _sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _conn(path: str):
    """Return the appropriate connection context manager."""
    if _BACKEND == "postgres":
        return _pg_conn()
    return _sqlite_conn(path)


# ---------------------------------------------------------------------------
# Dialect helpers
# ---------------------------------------------------------------------------

def _ph(n: int = 1) -> str:
    """Return the correct positional placeholder for the active backend."""
    return "%s" if _BACKEND == "postgres" else "?"


def _placeholders(n: int) -> str:
    """Return n placeholders as a comma-separated string."""
    p = "%s" if _BACKEND == "postgres" else "?"
    return ", ".join([p] * n)


def _fetchall(cursor) -> list[dict[str, Any]]:
    """Normalise rows to list[dict] regardless of backend cursor type."""
    rows = cursor.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], dict):          # psycopg2 RealDictRow
        return [dict(r) for r in rows]
    return [dict(r) for r in rows]         # sqlite3.Row


def _fetchone(cursor) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    return dict(row)


def _scalar(row: Any) -> Any:
    """Extract the first value from a cursor row, regardless of backend."""
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


def _bool_val(v: Any) -> bool:
    """Postgres returns real bools; SQLite returns 0/1 integers."""
    if isinstance(v, bool):
        return v
    return bool(v)
    """Postgres returns real bools; SQLite returns 0/1 integers."""
    if isinstance(v, bool):
        return v
    return bool(v)


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serial() -> str:
    """AUTOINCREMENT keyword for the active backend."""
    return "SERIAL" if _BACKEND == "postgres" else "INTEGER"


def _autoincrement() -> str:
    return "" if _BACKEND == "postgres" else "AUTOINCREMENT"


def _conflict_ignore() -> str:
    """INSERT that silently skips on PK conflict."""
    if _BACKEND == "postgres":
        return "ON CONFLICT DO NOTHING"
    return ""   # handled via INSERT OR IGNORE prefix in SQLite


def _insert_ignore_prefix() -> str:
    return "INSERT" if _BACKEND == "postgres" else "INSERT OR IGNORE"


def _upsert_extraction() -> str:
    """Upsert for extraction_cache keyed on description_hash."""
    if _BACKEND == "postgres":
        return """
            INSERT INTO extraction_cache (description_hash, extracted_json, created_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (description_hash) DO UPDATE
              SET extracted_json = EXCLUDED.extracted_json,
                  created_at     = EXCLUDED.created_at
        """
    return """
        INSERT OR REPLACE INTO extraction_cache
            (description_hash, extracted_json, created_at)
        VALUES (?, ?, ?)
    """


# ---------------------------------------------------------------------------
# Schema — dialect-neutral DDL
# ---------------------------------------------------------------------------

def _ddl_listings() -> str:
    if _BACKEND == "postgres":
        return """
            CREATE TABLE IF NOT EXISTS listings (
                id          TEXT PRIMARY KEY,
                title       TEXT NOT NULL,
                company     TEXT NOT NULL,
                location    TEXT,
                url         TEXT NOT NULL,
                description TEXT,
                source      TEXT NOT NULL,
                posted_at   TEXT,
                fetched_at  TEXT NOT NULL,
                fit_score   INTEGER,
                fit_reason  TEXT
            )
        """
    return """
        CREATE TABLE IF NOT EXISTS listings (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            company     TEXT NOT NULL,
            location    TEXT,
            url         TEXT NOT NULL,
            description TEXT,
            source      TEXT NOT NULL,
            posted_at   TEXT,
            fetched_at  TEXT NOT NULL,
            fit_score   INTEGER,
            fit_reason  TEXT
        )
    """


def _ddl_cycle_log() -> str:
    if _BACKEND == "postgres":
        return """
            CREATE TABLE IF NOT EXISTS cycle_log (
                id              SERIAL PRIMARY KEY,
                agent           TEXT NOT NULL,
                started_at      TEXT NOT NULL,
                finished_at     TEXT,
                records_touched INTEGER NOT NULL DEFAULT 0,
                status          TEXT NOT NULL,
                notes           TEXT
            )
        """
    return """
        CREATE TABLE IF NOT EXISTS cycle_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            agent           TEXT NOT NULL,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            records_touched INTEGER NOT NULL DEFAULT 0,
            status          TEXT NOT NULL,
            notes           TEXT
        )
    """


def _ddl_skill_gaps() -> str:
    return """
        CREATE TABLE IF NOT EXISTS skill_gaps (
            skill     TEXT PRIMARY KEY,
            frequency INTEGER NOT NULL DEFAULT 1,
            last_seen TEXT NOT NULL
        )
    """


def _ddl_extraction_cache() -> str:
    return """
        CREATE TABLE IF NOT EXISTS extraction_cache (
            description_hash TEXT PRIMARY KEY,
            extracted_json   TEXT NOT NULL,
            created_at       TEXT NOT NULL
        )
    """


def _ddl_gap_snapshots() -> str:
    if _BACKEND == "postgres":
        return """
            CREATE TABLE IF NOT EXISTS gap_snapshots (
                id                SERIAL PRIMARY KEY,
                run_id            TEXT NOT NULL,
                computed_at       TEXT NOT NULL,
                skill             TEXT NOT NULL,
                listings_blocked  INTEGER NOT NULL,
                opportunity_cost  REAL NOT NULL,
                mean_score        REAL NOT NULL,
                top_score         INTEGER NOT NULL,
                also_nice_to_have INTEGER NOT NULL DEFAULT 0,
                low_confidence    INTEGER NOT NULL DEFAULT 0,
                example_ids       TEXT NOT NULL
            )
        """
    return """
        CREATE TABLE IF NOT EXISTS gap_snapshots (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id            TEXT NOT NULL,
            computed_at       TEXT NOT NULL,
            skill             TEXT NOT NULL,
            listings_blocked  INTEGER NOT NULL,
            opportunity_cost  REAL NOT NULL,
            mean_score        REAL NOT NULL,
            top_score         INTEGER NOT NULL,
            also_nice_to_have INTEGER NOT NULL DEFAULT 0,
            low_confidence    INTEGER NOT NULL DEFAULT 0,
            example_ids       TEXT NOT NULL
        )
    """


def _ddl_query_log() -> str:
    if _BACKEND == "postgres":
        return """
            CREATE TABLE IF NOT EXISTS query_log (
                id          SERIAL PRIMARY KEY,
                asked_at    TEXT NOT NULL,
                question    TEXT NOT NULL,
                tool_chosen TEXT,
                params_json TEXT,
                answerable  INTEGER NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0
            )
        """
    return """
        CREATE TABLE IF NOT EXISTS query_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            asked_at    TEXT NOT NULL,
            question    TEXT NOT NULL,
            tool_chosen TEXT,
            params_json TEXT,
            answerable  INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0
        )
    """


# ---------------------------------------------------------------------------
# Init / migrate
# ---------------------------------------------------------------------------

def init_db(path: str) -> None:
    """Create all tables if they do not already exist. Safe to call repeatedly."""
    with _conn(path) as conn:
        cur = conn.cursor()
        for ddl in [
            _ddl_listings(),
            _ddl_skill_gaps(),
            _ddl_cycle_log(),
            _ddl_extraction_cache(),
            _ddl_gap_snapshots(),
            _ddl_query_log(),
        ]:
            cur.execute(ddl)


# ---------------------------------------------------------------------------
# Stable ID
# ---------------------------------------------------------------------------

def make_listing_id(source: str, url: str) -> str:
    raw = f"{source.strip().lower()}::{url.strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Listings
# ---------------------------------------------------------------------------

def upsert_listings(path: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0

    fetched_at = _now_utc()
    prepped: list[dict[str, Any]] = []
    for row in rows:
        entry = dict(row)
        entry.setdefault("id", make_listing_id(entry["source"], entry["url"]))
        entry.setdefault("fetched_at", fetched_at)
        entry.setdefault("fit_score", None)
        entry.setdefault("fit_reason", None)
        entry.setdefault("posted_at", None)
        prepped.append(entry)

    p = "%s" if _BACKEND == "postgres" else "?"
    prefix = "INSERT" if _BACKEND == "postgres" else "INSERT OR IGNORE"
    conflict = "ON CONFLICT (id) DO NOTHING" if _BACKEND == "postgres" else ""
    sql = f"""
        {prefix} INTO listings
            (id, title, company, location, url, description,
             source, posted_at, fetched_at, fit_score, fit_reason)
        VALUES
            ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p},{p})
        {conflict}
    """

    new_count = 0
    with _conn(path) as conn:
        cur = conn.cursor()
        for entry in prepped:
            cur.execute(sql, (
                entry["id"], entry.get("title"), entry.get("company"),
                entry.get("location"), entry.get("url"), entry.get("description"),
                entry.get("source"), entry.get("posted_at"), entry["fetched_at"],
                entry.get("fit_score"), entry.get("fit_reason"),
            ))
            new_count += cur.rowcount
    return new_count


def count_unscored(path: str) -> int:
    with _conn(path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM listings WHERE fit_score IS NULL")
        return _scalar(cur.fetchone()) or 0


def last_fetch_time(path: str) -> str | None:
    with _conn(path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT MAX(fetched_at) FROM listings")
        v = _scalar(cur.fetchone())
        return str(v) if v is not None else None


def get_listings(path: str, limit: int = 50, min_score: int = 0) -> list[dict[str, Any]]:
    p = "%s" if _BACKEND == "postgres" else "?"
    sql = f"""
        SELECT * FROM listings
        WHERE fit_score >= {p}
        ORDER BY fetched_at DESC
        LIMIT {p}
    """
    with _conn(path) as conn:
        if _BACKEND == "postgres":
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = conn.cursor()
        cur.execute(sql, (min_score, limit))
        return _fetchall(cur)


def get_unscored_listings(path: str, limit: int = 25) -> list[dict[str, Any]]:
    p = "%s" if _BACKEND == "postgres" else "?"
    sql = f"""
        SELECT * FROM listings
        WHERE fit_score IS NULL
        ORDER BY fetched_at ASC
        LIMIT {p}
    """
    with _conn(path) as conn:
        if _BACKEND == "postgres":
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = conn.cursor()
        cur.execute(sql, (limit,))
        return _fetchall(cur)


def save_score(path: str, listing_id: str, fit_score: int, fit_reason: str) -> None:
    p = "%s" if _BACKEND == "postgres" else "?"
    sql = f"UPDATE listings SET fit_score = {p}, fit_reason = {p} WHERE id = {p}"
    with _conn(path) as conn:
        cur = conn.cursor()
        cur.execute(sql, (fit_score, fit_reason, listing_id))


def clear_score(path: str, listing_id: str) -> int:
    p = "%s" if _BACKEND == "postgres" else "?"
    sql = f"UPDATE listings SET fit_score = NULL, fit_reason = NULL WHERE id = {p}"
    with _conn(path) as conn:
        cur = conn.cursor()
        cur.execute(sql, (listing_id,))
        return cur.rowcount


def clear_all_scores(path: str) -> int:
    with _conn(path) as conn:
        cur = conn.cursor()
        cur.execute("UPDATE listings SET fit_score = NULL, fit_reason = NULL")
        return cur.rowcount


# ---------------------------------------------------------------------------
# Cycle log
# ---------------------------------------------------------------------------

def log_cycle(
    path: str, agent: str, started_at: str, finished_at: str,
    records_touched: int, status: str, notes: str | None = None,
) -> None:
    p = "%s" if _BACKEND == "postgres" else "?"
    sql = f"""
        INSERT INTO cycle_log
            (agent, started_at, finished_at, records_touched, status, notes)
        VALUES ({p},{p},{p},{p},{p},{p})
    """
    with _conn(path) as conn:
        cur = conn.cursor()
        cur.execute(sql, (agent, started_at, finished_at, records_touched, status, notes))


# ---------------------------------------------------------------------------
# Extraction cache
# ---------------------------------------------------------------------------

def get_extraction(path: str, description_hash: str) -> dict[str, Any] | None:
    p = "%s" if _BACKEND == "postgres" else "?"
    sql = f"SELECT extracted_json FROM extraction_cache WHERE description_hash = {p}"
    with _conn(path) as conn:
        cur = conn.cursor()
        cur.execute(sql, (description_hash,))
        row = cur.fetchone()
        if row is None:
            return None
        v = row["extracted_json"] if isinstance(row, dict) else row[0]
        return json.loads(v)


def set_extraction(path: str, description_hash: str, extracted: dict[str, Any]) -> None:
    with _conn(path) as conn:
        cur = conn.cursor()
        cur.execute(_upsert_extraction(), (description_hash, json.dumps(extracted), _now_utc()))


# ---------------------------------------------------------------------------
# Scored listings with extractions
# ---------------------------------------------------------------------------

def get_scored_listings_with_extractions(path: str) -> list[dict[str, Any]]:
    with _conn(path) as conn:
        if _BACKEND == "postgres":
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = conn.cursor()
        cur.execute(
            "SELECT id, fit_score, location, description FROM listings "
            "WHERE fit_score IS NOT NULL"
        )
        listings = _fetchall(cur)

    results = []
    with _conn(path) as conn:
        if _BACKEND == "postgres":
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = conn.cursor()
        p = "%s" if _BACKEND == "postgres" else "?"
        for listing in listings:
            desc = (listing.get("description") or "").strip()
            if not desc:
                continue
            desc_hash = hashlib.sha256(desc.encode("utf-8")).hexdigest()
            cur.execute(
                f"SELECT extracted_json FROM extraction_cache WHERE description_hash = {p}",
                (desc_hash,)
            )
            row = cur.fetchone()
            if row is None:
                continue
            try:
                v = row["extracted_json"] if isinstance(row, dict) else row[0]
                extracted = json.loads(v)
            except (ValueError, KeyError):
                continue
            results.append({
                "id":        listing["id"],
                "fit_score": listing["fit_score"],
                "location":  listing.get("location"),
                "extracted": extracted,
            })
    return results


# ---------------------------------------------------------------------------
# Gap snapshots
# ---------------------------------------------------------------------------

def write_gap_snapshot(
    path: str, run_id: str, computed_at: str, gaps: list[dict[str, Any]],
) -> None:
    p = "%s" if _BACKEND == "postgres" else "?"
    sql = f"""
        INSERT INTO gap_snapshots
            (run_id, computed_at, skill, listings_blocked,
             opportunity_cost, mean_score, top_score,
             also_nice_to_have, low_confidence, example_ids)
        VALUES ({p},{p},{p},{p},{p},{p},{p},{p},{p},{p})
    """
    with _conn(path) as conn:
        cur = conn.cursor()
        for gap in gaps:
            cur.execute(sql, (
                run_id, computed_at, gap["skill"],
                gap["listings_blocked"],
                round(gap["opportunity_cost"], 4),
                round(gap["mean_score"], 2),
                gap["top_score"],
                gap.get("also_nice_to_have", 0),
                1 if gap.get("low_confidence") else 0,
                json.dumps(gap["example_ids"]),
            ))


def get_latest_gap_snapshot(path: str) -> list[dict[str, Any]]:
    with _conn(path) as conn:
        if _BACKEND == "postgres":
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = conn.cursor()
        cur.execute(
            "SELECT run_id FROM gap_snapshots ORDER BY computed_at DESC LIMIT 1"
        )
        row = cur.fetchone()
        if row is None:
            return []
        run_id = row["run_id"] if isinstance(row, dict) else row[0]

        p = "%s" if _BACKEND == "postgres" else "?"
        cur.execute(
            f"""
            SELECT skill, listings_blocked, opportunity_cost,
                   mean_score, top_score, also_nice_to_have,
                   low_confidence, example_ids, computed_at
            FROM gap_snapshots
            WHERE run_id = {p}
            ORDER BY opportunity_cost DESC
            """,
            (run_id,)
        )
        result = []
        for r in _fetchall(cur):
            r["example_ids"]  = json.loads(r["example_ids"])
            r["low_confidence"] = _bool_val(r["low_confidence"])
            result.append(r)
        return result


def get_snapshot_count(path: str) -> int:
    with _conn(path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT run_id) FROM gap_snapshots")
        return _scalar(cur.fetchone()) or 0


def get_gap_snapshot_by_position(
    path: str, position: str,
) -> tuple[str, str, list[dict]]:
    order = "ASC" if position == "earliest" else "DESC"
    with _conn(path) as conn:
        if _BACKEND == "postgres":
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = conn.cursor()
        cur.execute(
            f"SELECT run_id, computed_at FROM gap_snapshots "
            f"ORDER BY computed_at {order} LIMIT 1"
        )
        run = cur.fetchone()
        if run is None:
            return ("", "", [])
        run_id      = run["run_id"]      if isinstance(run, dict) else run[0]
        computed_at = run["computed_at"] if isinstance(run, dict) else run[1]

        p = "%s" if _BACKEND == "postgres" else "?"
        cur.execute(
            f"""
            SELECT skill, listings_blocked, opportunity_cost,
                   mean_score, top_score, also_nice_to_have,
                   low_confidence, example_ids
            FROM gap_snapshots
            WHERE run_id = {p}
            ORDER BY opportunity_cost DESC
            """,
            (run_id,)
        )
        result = []
        for r in _fetchall(cur):
            r["example_ids"]   = json.loads(r["example_ids"])
            r["low_confidence"] = _bool_val(r["low_confidence"])
            result.append(r)
        return (run_id, computed_at, result)


# ---------------------------------------------------------------------------
# State inspection
# ---------------------------------------------------------------------------

def last_cycle_summary(path: str) -> dict[str, Any] | None:
    with _conn(path) as conn:
        if _BACKEND == "postgres":
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = conn.cursor()
        cur.execute(
            "SELECT status, started_at FROM cycle_log "
            "WHERE agent = 'cycle' ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        return dict(row) if row else None


def latest_score_time(path: str) -> str | None:
    with _conn(path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT MAX(fetched_at) FROM listings WHERE fit_score IS NOT NULL")
        v = _scalar(cur.fetchone())
        return str(v) if v is not None else None


def latest_gap_time(path: str) -> str | None:
    with _conn(path) as conn:
        cur = conn.cursor()
        cur.execute("SELECT MAX(computed_at) FROM gap_snapshots")
        v = _scalar(cur.fetchone())
        return str(v) if v is not None else None


# ---------------------------------------------------------------------------
# Dashboard gate  (rule 38)
# ---------------------------------------------------------------------------

def get_last_verified_cycle(path: str) -> dict[str, Any] | None:
    with _conn(path) as conn:
        if _BACKEND == "postgres":
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = conn.cursor()
        cur.execute(
            "SELECT * FROM cycle_log "
            "WHERE agent = 'cycle' AND status = 'complete' "
            "ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Query log  (rule 45)
# ---------------------------------------------------------------------------

def log_query(
    path: str, question: str, tool_chosen: str | None,
    params: dict[str, Any], answerable: bool, duration_ms: int,
) -> None:
    p = "%s" if _BACKEND == "postgres" else "?"
    sql = f"""
        INSERT INTO query_log
            (asked_at, question, tool_chosen, params_json, answerable, duration_ms)
        VALUES ({p},{p},{p},{p},{p},{p})
    """
    with _conn(path) as conn:
        cur = conn.cursor()
        cur.execute(sql, (
            _now_utc(), question, tool_chosen,
            json.dumps(params), 1 if answerable else 0, duration_ms,
        ))


def get_recent_queries(path: str, limit: int = 20) -> list[dict[str, Any]]:
    p = "%s" if _BACKEND == "postgres" else "?"
    with _conn(path) as conn:
        if _BACKEND == "postgres":
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        else:
            cur = conn.cursor()
        cur.execute(
            f"SELECT * FROM query_log ORDER BY id DESC LIMIT {p}", (limit,)
        )
        result = []
        for r in _fetchall(cur):
            try:
                r["params"] = json.loads(r.get("params_json") or "{}")
            except (ValueError, TypeError):
                r["params"] = {}
            result.append(r)
        return result

# ---------------------------------------------------------------------------
# CLI: --migrate and --check
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    # Path is irrelevant for Postgres but needed for SQLite fallback
    _db_path = os.environ.get("EDGEDASH_DB", "edgedash.db")

    parser = argparse.ArgumentParser(description="EdgeDash storage management.")
    parser.add_argument("--migrate", action="store_true",
                        help="Create all tables (safe to run repeatedly).")
    parser.add_argument("--check",   action="store_true",
                        help="Print backend, connection status, and row counts.")
    args = parser.parse_args()

    if args.migrate:
        print(f"Running migrations against {_BACKEND} backend …")
        try:
            init_db(_db_path)
            print("Migrations complete.")
        except Exception as exc:
            print(f"Migration failed: {exc}", file=sys.stderr)
            sys.exit(1)

    elif args.check:
        print(f"Backend : {_BACKEND}")
        print(f"URL     : {'set (DATABASE_URL)' if _DATABASE_URL else f'sqlite:{_db_path}'}")
        tables = [
            "listings", "skill_gaps", "cycle_log",
            "extraction_cache", "gap_snapshots", "query_log",
        ]
        try:
            init_db(_db_path)   # ensure tables exist
            with _conn(_db_path) as conn:
                if _BACKEND == "postgres":
                    cur = conn.cursor()
                else:
                    cur = conn.cursor()
                print("\nTable row counts:")
                for t in tables:
                    cur.execute(f"SELECT COUNT(*) FROM {t}")
                    count = _scalar(cur.fetchone()) or 0
                    print(f"  {t:<24} {count:>8}")
            print("\nConnection: OK")
        except Exception as exc:
            print(f"\nConnection: FAILED — {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()
