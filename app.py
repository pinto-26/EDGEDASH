"""
app.py — EdgeDash read-only Streamlit dashboard.

Per rule 38: every data panel reads from the last PASSING cycle only.
Per rule 48: DATABASE_URL is never printed, logged, or shown to a user.
Per rule 49: this file contains NO writes. No cycle can be triggered here.
Per rule 50: every panel is wrapped so one failure cannot take down the page.
             A stranger must never see a traceback.

Run locally:
    streamlit run app.py

Deploy:
    Push to GitHub, connect to Streamlit Community Cloud.
    Set DATABASE_URL and GEMINI_API_KEY in the Secrets UI.
"""

from __future__ import annotations

import json
import logging
import os
import time
import traceback
from datetime import datetime, timezone
from typing import Any

import streamlit as st

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Page config  (must be first Streamlit call)
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="EdgeDash",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Streamlit Cloud secrets bridge (rule 48)
# Streamlit Cloud puts secrets in st.secrets but does NOT automatically inject
# them into os.environ. storage.py reads os.environ at import time, so we
# bridge here — BEFORE the edgedash imports below.
# Names are logged; values are never printed or logged.
# ---------------------------------------------------------------------------
try:
    _SECRETS_TO_BRIDGE = ["DATABASE_URL", "GEMINI_API_KEY", "GITHUB_REPO_URL"]
    for _secret_name in _SECRETS_TO_BRIDGE:
        if _secret_name not in os.environ:
            _val = st.secrets.get(_secret_name)
            if _val:
                os.environ[_secret_name] = str(_val)
                logger.info("Bridged %s from st.secrets to os.environ", _secret_name)
except Exception:
    pass  # st.secrets unavailable locally — fine, os.environ already has what's needed

# ---------------------------------------------------------------------------
# Safe imports — import failures are caught and shown gracefully (rule 50)
# ---------------------------------------------------------------------------

_import_error: str | None = None
try:
    from edgedash.config import load_config
    from edgedash.query.ask import Answer, ask as ask_question, daily_ask_count
    import edgedash.storage as storage
except Exception as _exc:
    _import_error = str(_exc)
    logger.error("Import failed: %s", traceback.format_exc())

# ---------------------------------------------------------------------------
# Database connectivity check  (rule 50 — show status, not traceback)
# ---------------------------------------------------------------------------

def _check_db(cfg) -> tuple[bool, str]:
    """Return (ok, message). Never raises. Never prints DATABASE_URL."""
    try:
        storage.init_db(cfg.db_path)
        return True, "connected"
    except Exception as exc:
        logger.error("DB connection failed: %s", exc)
        # Sanitise: never include the connection string in the message
        msg = str(exc)
        # Strip anything that looks like a URL with credentials
        import re
        msg = re.sub(r"postgresql://[^\s]+", "postgresql://***", msg)
        msg = re.sub(r"postgres://[^\s]+",   "postgres://***",   msg)
        return False, msg


def _safe_panel(title: str):
    """Context manager — catches exceptions in a panel and shows a friendly message."""
    import contextlib

    @contextlib.contextmanager
    def _inner():
        try:
            yield
        except Exception as exc:
            logger.error("Panel '%s' failed: %s", title, traceback.format_exc())
            st.warning(f"⚠️  {title} could not be loaded. Try refreshing.")

    return _inner()


# ---------------------------------------------------------------------------
# Cached data loaders
# ---------------------------------------------------------------------------

@st.cache_data(ttl=30)
def _load_config():
    return load_config()


@st.cache_data(ttl=30)
def _load_last_verified_cycle(db_path: str):
    return storage.get_last_verified_cycle(db_path)


@st.cache_data(ttl=30)
def _load_recent_cycles(db_path: str, limit: int = 30):
    """All cycle_log rows — goes through storage, not raw sqlite3."""
    try:
        conn_ctx = storage._conn(db_path)
        with conn_ctx as conn:
            if storage._BACKEND == "postgres":
                import psycopg2.extras
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            else:
                cur = conn.cursor()
            cur.execute(
                "SELECT * FROM cycle_log ORDER BY id DESC LIMIT %s"
                if storage._BACKEND == "postgres"
                else "SELECT * FROM cycle_log ORDER BY id DESC LIMIT ?",
                (limit,)
            )
            return storage._fetchall(cur)
    except Exception as exc:
        logger.error("load_recent_cycles failed: %s", exc)
        return []


@st.cache_data(ttl=30)
def _load_scored_listings(db_path: str, min_score: int):
    return storage.get_listings(db_path, limit=10, min_score=min_score)


@st.cache_data(ttl=30)
def _load_gap_snapshot(db_path: str):
    return storage.get_latest_gap_snapshot(db_path)


@st.cache_data(ttl=30)
def _load_counts(db_path: str) -> dict[str, int]:
    try:
        all_scored = storage.get_listings(db_path, limit=100000, min_score=0)
        unscored   = storage.count_unscored(db_path)
        return {"total": len(all_scored) + unscored, "scored": len(all_scored), "unscored": unscored}
    except Exception as exc:
        logger.error("load_counts failed: %s", exc)
        return {"total": 0, "scored": 0, "unscored": 0}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt_ts(ts: str | None) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return str(ts)[:19]


def _parse_notes(notes: str | None) -> dict[str, Any]:
    if not notes:
        return {}
    try:
        return json.loads(notes)
    except (ValueError, TypeError):
        return {"raw": notes}


def _outcome_color(outcome: str) -> str:
    return {
        "complete":      "#22c55e",
        "partial":       "#f59e0b",
        "nothing_to_do": "#60a5fa",
        "degraded":      "#ef4444",
        "failed":        "#ef4444",
    }.get(outcome.lower(), "#9ca3af")


def _verdict_badge(verdict: str) -> str:
    if verdict == "pass":
        return "✅ pass"
    if verdict in ("fail", "failed"):
        return "❌ fail"
    return f"— {verdict}"


# ---------------------------------------------------------------------------
# EARLY EXITS — import failure or no config
# ---------------------------------------------------------------------------

st.title("📊 EdgeDash — Career Intelligence Dashboard")

if _import_error:
    st.error(
        "The app could not start because a required module failed to load. "
        "Check the server logs for details."
    )
    st.stop()

try:
    cfg = _load_config()
except Exception as exc:
    logger.error("load_config failed: %s", exc)
    st.error("Could not load configuration. Check that config.yaml is present.")
    st.stop()

st.caption(
    f"Target: **{cfg.target_role}** in **{cfg.target_city}** · "
    "Read-only · Auto-refreshes every 30s"
)

# Database connectivity
db_ok, db_msg = _check_db(cfg)
if not db_ok:
    # Diagnose: print which env var names are present (never values)
    _env_names = [k for k in os.environ if k in (
        "DATABASE_URL", "GEMINI_API_KEY", "GITHUB_REPO_URL",
        "STREAMLIT_SECRETS_DATABASE_URL",
    )]
    _has_db_url = "DATABASE_URL" in os.environ
    _has_st_secrets = hasattr(st, "secrets") and "DATABASE_URL" in (st.secrets or {})

    st.error(
        "**Database not configured or unreachable.**\n\n"
        "If you are running this on Streamlit Cloud, add `DATABASE_URL` "
        "to your app's Secrets (Settings → Secrets). "
        "If you are running locally, check that `edgedash.db` exists or "
        "run `python run_cycle.py` first."
    )

    # Diagnostic block — variable names only, zero values shown
    with st.expander("Startup diagnostic (no secret values shown)"):
        st.code(
            f"DATABASE_URL in os.environ : {_has_db_url}\n"
            f"DATABASE_URL in st.secrets : {_has_st_secrets}\n"
            f"Env vars present           : {sorted(_env_names)}\n"
            f"storage backend detected   : {storage._BACKEND}\n"
            f"Connection error           : {db_msg}"
        )
        st.caption(
            "If DATABASE_URL shows False but you set it in Streamlit Secrets, "
            "the secret name must be exactly DATABASE_URL (case-sensitive, no quotes). "
            "Also confirm the value includes ?sslmode=require for Supabase."
        )
    st.caption("Details are in the server logs. No traceback is shown here.")
    st.stop()

# ---------------------------------------------------------------------------
# Section 1: Header strip
# ---------------------------------------------------------------------------

with _safe_panel("Header"):
    latest_cycle   = _load_last_verified_cycle(cfg.db_path)
    # Most recent cycle of any kind for the warning check
    all_recent = _load_recent_cycles(cfg.db_path, limit=1)
    newest_any = all_recent[0] if all_recent else None

    counts       = _load_counts(cfg.db_path)
    verified_ts  = _fmt_ts(latest_cycle["started_at"] if latest_cycle else None)
    latest_notes = _parse_notes(newest_any.get("notes") if newest_any else None)
    latest_outcome = latest_notes.get("outcome", "")
    latest_verdict = latest_notes.get("verdict", "n/a")
    latest_ts      = _fmt_ts(newest_any["started_at"] if newest_any else None)

    if newest_any and latest_outcome in ("degraded", "partial") and latest_verdict != "pass":
        st.warning(
            f"⚠️  The most recent cycle ended as **{latest_outcome}** (verdict: {latest_verdict}) "
            f"at {latest_ts}. "
            f"Data below is from the last **verified** cycle: **{verified_ts}**. "
            "Stale verified data is shown in preference to unverified fresh data (rule 38).",
        )

    if counts["total"] == 0:
        st.info(
            "No listings yet. The first cycle has not run. "
            "Run `python run_cycle.py` from the scheduler process to populate the database."
        )

    col1, col2, col3, col4, col5 = st.columns(5)
    with col1:
        st.metric("Last Verified Cycle", verified_ts)
    with col2:
        st.metric("Total Listings", counts["total"])
    with col3:
        st.metric("Scored", counts["scored"])
    with col4:
        delta = f"-{counts['unscored']}" if counts["unscored"] else None
        st.metric("Unscored", counts["unscored"], delta=delta, delta_color="inverse")
    with col5:
        verdict_display = latest_verdict.upper() if latest_verdict != "n/a" else "NO DATA"
        st.metric("Latest Verdict", verdict_display)

st.divider()

# ---------------------------------------------------------------------------
# Section 2: Agent Activity Log
# ---------------------------------------------------------------------------

with _safe_panel("Agent Activity Log"):
    st.subheader("🔄 Agent Activity Log")
    st.caption("All cycles including failed and degraded — exception to rule 38.")

    cycles = _load_recent_cycles(cfg.db_path, limit=30)

    if not cycles:
        st.info("No cycle data yet.")
    else:
        rows: list[dict] = []
        for c in cycles:
            n        = _parse_notes(c.get("notes"))
            outcome  = n.get("outcome") or c.get("status") or "—"
            verdict  = n.get("verdict", "—")
            failed_c = n.get("failed_checks", "")
            retries  = n.get("retry_count", "—")
            elapsed  = n.get("elapsed_s", "—")
            ran      = n.get("ran", "")
            skipped  = n.get("skipped", "")
            agent    = c.get("agent", "")
            ts       = _fmt_ts(c.get("started_at"))

            icon = {
                "complete": "✅", "partial": "⚠️",
                "nothing_to_do": "💤", "degraded": "🔴",
                "ok": "✅", "failed": "🔴",
            }.get(outcome, "•")

            rows.append({
                "": icon, "Timestamp": ts, "Agent": agent,
                "Outcome": outcome,
                "Verdict": _verdict_badge(verdict) if agent == "cycle" else "—",
                "Failed Check": failed_c or "—",
                "Retries": str(retries), "Dur (s)": str(elapsed),
                "Ran": ran[:80] if ran else "—",
                "Skipped": skipped[:60] if skipped else "—",
            })

        import pandas as pd

        df = pd.DataFrame(rows)
        display_cols = ["", "Timestamp", "Agent", "Outcome", "Verdict",
                        "Failed Check", "Retries", "Dur (s)", "Ran", "Skipped"]

        def _highlight(row):
            o = row.get("Outcome", "")
            if o in ("degraded", "failed"):
                return ["background-color: #3b0f0f"] * len(row)
            if o == "partial":
                return ["background-color: #3b2a0f"] * len(row)
            if o == "nothing_to_do":
                return ["background-color: #0f1f3b"] * len(row)
            return [""] * len(row)

        styled = (
            df[display_cols].style
            .apply(_highlight, axis=1)
            .set_properties(**{"font-size": "12px"})
        )
        st.dataframe(styled, use_container_width=True, height=480)

st.divider()

# ---------------------------------------------------------------------------
# Section 3: Scored listings + Skill gaps
# ---------------------------------------------------------------------------

col_left, col_right = st.columns(2)

with col_left:
    with _safe_panel("Top Scored Listings"):
        st.subheader("🏆 Top Scored Listings")
        if not latest_cycle:
            st.info("No verified cycle yet.")
        else:
            listings = _load_scored_listings(cfg.db_path, min_score=cfg.min_fit_score)
            if not listings:
                st.info(f"No listings scored above {cfg.min_fit_score} yet.")
            else:
                for r in listings:
                    score  = r.get("fit_score", 0)
                    title  = r.get("title", "Unknown")
                    co     = r.get("company", "")
                    reason = r.get("fit_reason", "")
                    url    = r.get("url", "")
                    bar_w  = int(score / 5)
                    bar    = "█" * bar_w + "░" * (20 - bar_w)
                    color  = "#22c55e" if score >= 70 else "#f59e0b" if score >= 50 else "#ef4444"
                    with st.container():
                        cs, ci = st.columns([1, 5])
                        with cs:
                            st.markdown(
                                f"<span style='font-size:1.8rem;font-weight:bold;color:{color}'>"
                                f"{score}</span>",
                                unsafe_allow_html=True,
                            )
                        with ci:
                            link = f"[{title}]({url})" if url else title
                            st.markdown(f"**{link}**  \n_{co}_")
                            st.caption(reason[:120] if reason else "—")
                    st.markdown(f"`{bar}`")

with col_right:
    with _safe_panel("Skill Gaps"):
        st.subheader("🎯 Skill Gaps")
        if not latest_cycle:
            st.info("No verified cycle yet.")
        else:
            gaps = _load_gap_snapshot(cfg.db_path)
            if not gaps:
                st.info("No gap snapshot yet.")
            else:
                top_cost = gaps[0]["opportunity_cost"] if gaps else 1.0
                snap_ts  = _fmt_ts(gaps[0].get("computed_at") if gaps else None)
                st.caption(f"Snapshot: {snap_ts}  ·  {len(gaps)} gaps identified")
                for rank, gap in enumerate(gaps[:10], 1):
                    skill   = gap["skill"]
                    blocked = gap["listings_blocked"]
                    cost    = gap["opportunity_cost"]
                    mean_s  = gap["mean_score"]
                    low_c   = gap.get("low_confidence", False)
                    nth     = gap.get("also_nice_to_have", 0)
                    bar_w   = int((cost / max(top_cost, 0.01)) * 16)
                    bar     = "█" * bar_w + "░" * (16 - bar_w)
                    conf    = " ⚠ low-conf" if low_c else ""
                    nth_str = f"  +{nth} nice-to-have" if nth else ""
                    st.markdown(
                        f"**{rank}. {skill}**{conf}  \n"
                        f"`{bar}` cost **{cost:.1f}** · {blocked} blocked "
                        f"· avg {mean_s:.0f}{nth_str}"
                    )

# ---------------------------------------------------------------------------
# Section 4: Ask Your Data  (rules 42-45)
# ---------------------------------------------------------------------------

st.divider()

with _safe_panel("Ask Your Data"):
    st.subheader("💬 Ask Your Data")
    st.caption(
        "Two model calls per question: one to route, one to phrase. "
        "No SQL generated. Rows always shown alongside the answer (rule 44)."
    )

    _EXAMPLES = [
        "Which companies are hiring Data Analysts right now?",
        "What are my top 5 skill gaps?",
        "What are the best jobs I should apply to?",
    ]

    _RATE_WINDOW_SECS = 600
    _RATE_MAX         = 10

    if "ask_timestamps" not in st.session_state:
        st.session_state["ask_timestamps"] = []
    if "ask_question" not in st.session_state:
        st.session_state["ask_question"] = ""

    _now_ts = time.time()
    st.session_state["ask_timestamps"] = [
        t for t in st.session_state["ask_timestamps"]
        if _now_ts - t < _RATE_WINDOW_SECS
    ]
    _session_count = len(st.session_state["ask_timestamps"])

    try:
        _daily_count = daily_ask_count(cfg.db_path)
    except Exception:
        _daily_count = 0

    _daily_capped        = _daily_count >= cfg.daily_ask_limit
    _session_rate_limited = _session_count >= _RATE_MAX

    if _daily_capped:
        st.info(
            f"The ask feature has reached its daily limit ({cfg.daily_ask_limit} questions). "
            "Data panels above are unaffected. Resets at midnight UTC."
        )
    else:
        ex_cols = st.columns(len(_EXAMPLES))
        for col, example in zip(ex_cols, _EXAMPLES):
            with col:
                if st.button(example, use_container_width=True,
                             key=f"ex_{example[:20]}",
                             disabled=_session_rate_limited):
                    st.session_state["ask_question"] = example

        question_input = st.text_input(
            "Ask a question about your job search data:",
            value=st.session_state.get("ask_question", ""),
            placeholder="e.g. What are my biggest skill gaps?",
            key="ask_input",
            disabled=_session_rate_limited,
            max_chars=300,
        )

        if _session_rate_limited:
            oldest = min(st.session_state["ask_timestamps"])
            wait_s = int(_RATE_WINDOW_SECS - (_now_ts - oldest)) + 1
            st.warning(
                f"You've sent {_RATE_MAX} questions in the last 10 minutes. "
                f"Please wait **{wait_s // 60}m {wait_s % 60}s** before asking again."
            )

        elif question_input and question_input.strip():
            with st.spinner("Routing → executing → phrasing…"):
                try:
                    ans: Answer = ask_question(question_input.strip(), cfg)
                    st.session_state["ask_timestamps"].append(time.time())
                except Exception as exc:
                    logger.error("ask pipeline error: %s", traceback.format_exc())
                    st.error("Something went wrong processing your question. Try again.")
                    ans = None

            if ans is not None:
                if ans.answerable:
                    st.success(ans.text)
                    if ans.summary:
                        st.caption(f"Scope: {ans.summary}  ·  Tool: `{ans.tool_used}`")
                else:
                    st.warning(ans.text)

                if ans.rows:
                    st.markdown("**Underlying data:**")
                    import pandas as pd
                    st.dataframe(
                        pd.DataFrame(ans.rows),
                        use_container_width=True,
                        height=min(400, 40 + 35 * len(ans.rows)),
                    )
                elif ans.answerable:
                    st.caption("No rows returned by this query.")

# ---------------------------------------------------------------------------
# Footer  (rule 48 — never show DATABASE_URL or any secret)
# ---------------------------------------------------------------------------

st.divider()

_footer_verified_ts = _fmt_ts(
    latest_cycle["started_at"] if latest_cycle else None
)

_github_url = os.environ.get("GITHUB_REPO_URL", "")

st.caption(
    f"EdgeDash · {cfg.target_role} · {cfg.target_city} · "
    f"Last verified cycle: {_footer_verified_ts} · "
    f"Data from last verified cycle only (rule 38)"
    + (f" · [GitHub]({_github_url})" if _github_url else "")
)
