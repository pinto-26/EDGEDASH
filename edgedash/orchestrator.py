"""
orchestrator.py — state-driven cycle execution (rules 28-33, 36-38).

Flow: read state → build plan → print plan → execute → verify → (one retry) → summary.

Registry: name → agent instance. Adding an agent = one line here + one rule
in planning.build_plan. Nothing else in this file changes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import edgedash.storage as storage
from edgedash.agents.base import Agent, AgentResult
from edgedash.agents.fetcher import Fetcher
from edgedash.agents.gap_analyzer import GapAnalyzer
from edgedash.agents.mock_fetcher import MockFetcher
from edgedash.agents.scorer import Scorer
from edgedash.agents.verifier import Verifier
from edgedash.config import Config
from edgedash.planning import Plan, StopConditions, Task, build_plan
from edgedash.state import read_state
from edgedash.verification import Verdict

# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------

def _build_registry(config: Config) -> dict[str, Agent]:
    fetcher: Agent = MockFetcher() if config.use_mock_fetcher else Fetcher()
    return {
        "Fetcher":     fetcher,
        "Scorer":      Scorer(),
        "GapAnalyzer": GapAnalyzer(),
        "Verifier":    Verifier(),    # ← one line added
    }


# ---------------------------------------------------------------------------
# Console helpers
# ---------------------------------------------------------------------------

_W = 72


def _banner(text: str) -> None:
    print(f"\n{'─' * _W}")
    print(f"  {text}")
    print(f"{'─' * _W}")


def _row(label: str, value: str) -> None:
    print(f"  {label:<28}{value}")


# ---------------------------------------------------------------------------
# Agent execution helper
# ---------------------------------------------------------------------------

def _run_agent(
    agent: Agent,
    config: Config,
    stop_conditions: StopConditions,
    task_goal: str,
) -> tuple[AgentResult, float]:
    """Run one agent, print result, return (result, elapsed_seconds)."""
    print(f"\n  ▶ {agent.name}  [{task_goal}]", flush=True)
    start = datetime.now(timezone.utc)
    try:
        result = agent.run(config, config.db_path, stop_conditions)
    except Exception as exc:
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        err = f"{type(exc).__name__}: {exc}"
        print(f"  ✗ {agent.name} raised: {err}")
        storage.log_cycle(
            path=config.db_path, agent=agent.name,
            started_at=start.isoformat(),
            finished_at=datetime.now(timezone.utc).isoformat(),
            records_touched=0, status="failed", notes=err,
        )
        return AgentResult(agent=agent.name, status="failed",
                           records_touched=0, notes=err), elapsed
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    icon = "✓" if result.status == "ok" else "✗"
    print(f"  {icon} {result.notes}")
    return result, elapsed


# ---------------------------------------------------------------------------
# Verification + one retry  (rule 36)
# ---------------------------------------------------------------------------

def _verify_and_retry(
    config: Config,
    registry: dict[str, Agent],
    results: list[AgentResult],
    durations: dict[str, float],
    failed_agents: list[str],
) -> tuple[Verdict | None, int, str]:
    """
    Run the Verifier. On fail, retry the failing agent once with adjusted
    context, then re-verify. Maximum one retry for the whole cycle (rule 36).

    Returns (verdict, retry_count, outcome).
    outcome: "complete" | "partial" | "degraded"
    """
    verifier: Verifier = registry["Verifier"]  # type: ignore[assignment]

    # --- First verification pass ---
    v_result, v_dur = _run_agent(
        verifier, config,
        StopConditions(), "verify cycle output"
    )
    results.append(v_result)
    durations["Verifier"] = v_dur

    verdict = verifier.last_verdict
    if verdict is None or verdict.passed:
        outcome = "partial" if failed_agents else "complete"
        return verdict, 0, outcome

    # --- Verification failed — attempt one retry (rule 36) ---
    failed_check_names = {c.name for c in verdict.failed_checks}
    print(f"\n  ⚠  Verification failed: {verdict.summary}")
    print(f"  ↺  Retrying failing agents (max 1 retry per cycle) …")

    retry_count = 1

    if "score_spread" in failed_check_names:
        scorer: Scorer = registry["Scorer"]  # type: ignore[assignment]
        retry_stop = StopConditions(
            max_items   = config.score_batch_size,
            max_seconds = config.max_score_seconds,
            widen_spread= True,   # ← instructs extractor to use strict_required=True
        )
        # Clear existing scores so the Scorer re-scores from scratch
        cleared = storage.clear_all_scores(config.db_path)
        print(f"  ↺  Cleared {cleared} score(s) for re-scoring with stricter extraction.")
        r_retry, d_retry = _run_agent(
            scorer, config, retry_stop,
            f"re-score {config.score_batch_size} listings (strict extraction)"
        )
        results.append(r_retry)
        durations["Scorer.retry"] = d_retry
        if r_retry.status != "ok":
            failed_agents.append("Scorer.retry")

    # Re-run GapAnalyzer if gaps were affected
    if "gap_sample_size" in failed_check_names:
        gap_agent: GapAnalyzer = registry["GapAnalyzer"]  # type: ignore[assignment]
        r_gap, d_gap = _run_agent(
            gap_agent, config,
            StopConditions(max_seconds=config.max_analyse_seconds),
            "recompute gap snapshot after retry"
        )
        results.append(r_gap)
        durations["GapAnalyzer.retry"] = d_gap

    # --- Second verification pass (final — no further retry) ---
    v2_result, v2_dur = _run_agent(
        verifier, config,
        StopConditions(), "re-verify after retry"
    )
    results.append(v2_result)
    durations["Verifier.retry"] = v2_dur

    verdict2 = verifier.last_verdict
    if verdict2 and verdict2.passed:
        outcome = "partial" if failed_agents else "complete"
        return verdict2, retry_count, outcome

    # --- Still failing after one retry → degraded (rule 36) ---
    print(f"\n  ✗  Verification still failing after retry — cycle marked degraded.")
    storage.log_cycle(
        path=config.db_path, agent="Verifier.degraded",
        started_at=datetime.now(timezone.utc).isoformat(),
        finished_at=datetime.now(timezone.utc).isoformat(),
        records_touched=0, status="degraded",
        notes=f"Degraded after retry. {verdict2.summary if verdict2 else 'no verdict'}",
    )
    return verdict2, retry_count, "degraded"


# ---------------------------------------------------------------------------
# Core cycle
# ---------------------------------------------------------------------------

def run_cycle(config: Config) -> list[AgentResult]:
    """
    One full state-driven cycle with verification and bounded retry.

    1. Init DB.
    2. Read state.
    3. Build plan (pure — no side effects).
    4. Print rendered plan (rule 31).
    5. Execute RUN tasks. One failure does not stop the rest (rule 32).
    6. Run Verifier. One retry on failure. Stop if still failing (rule 36).
    7. Write exactly one cycle summary row (rule 33).
    """
    cycle_start = datetime.now(timezone.utc)

    storage.init_db(config.db_path)
    state = read_state(config, cycle_start)
    plan  = build_plan(state, config)

    # Print state + plan (rule 31)
    _banner("EdgeDash — cycle starting")
    _row("Target role:",        config.target_role)
    _row("Target city:",        config.target_city)
    _row("Database:",           config.db_path)
    _row("Last fetch:",         state.last_fetch_at or "never")
    _row("Hours since fetch:",  f"{state.hours_since_fetch:.1f}")
    _row("Unscored listings:",  str(state.unscored_count))
    _row("Gaps stale:",         str(state.gaps_stale))
    _row("Last cycle verdict:", state.last_cycle_verdict or "—")

    print(f"\n{'─' * _W}")
    print("  PLAN")
    print("  ────")
    print(plan.render())

    registry = _build_registry(config)
    results:        list[AgentResult] = []
    agent_durations: dict[str, float] = {}
    failed_agents:   list[str]        = []

    run_tasks  = [t for t in plan.tasks if t.action == "run"]
    skip_tasks = [t for t in plan.tasks if t.action == "skip"]

    verdict:     Verdict | None = None
    retry_count: int            = 0

    if not run_tasks:
        _banner("Nothing to do — all agents skipped")
        outcome = "nothing_to_do"
    else:
        _banner("Running agents")

        for task in run_tasks:
            agent = registry.get(task.agent_name)
            if agent is None:
                print(f"\n  ⚠  {task.agent_name} not found in registry — skipping.")
                failed_agents.append(task.agent_name)
                continue

            result, elapsed = _run_agent(
                agent, config, task.stop_conditions, task.goal
            )
            results.append(result)
            agent_durations[task.agent_name] = elapsed
            if result.status != "ok":
                failed_agents.append(task.agent_name)

        # Verification + bounded retry (rule 36)
        _banner("Verifying cycle output")
        verdict, retry_count, outcome = _verify_and_retry(
            config, registry, results, agent_durations, failed_agents
        )

    # Summary (rule 33)
    cycle_end = datetime.now(timezone.utc)
    elapsed   = (cycle_end - cycle_start).total_seconds()

    verdict_str = (
        "pass" if (verdict and verdict.passed) else
        ("fail" if verdict else "n/a")
    )

    _banner(f"Cycle summary  [{outcome}]")
    print(f"  {'Agent':<22} {'Action':<8} {'Status':<10} {'Rec':>5}  {'Dur':>6}  Notes")
    print(f"  {'─'*22} {'─'*8} {'─'*10} {'─'*5}  {'─'*6}  {'─'*20}")

    for task in plan.tasks:
        if task.action == "skip":
            print(
                f"  {task.agent_name:<22} {'skip':<8} {'—':<10} {'—':>5}  {'—':>6}  "
                f"{task.reason[:38]}"
            )
        else:
            r   = next((x for x in results if x.agent == task.agent_name), None)
            dur = agent_durations.get(task.agent_name, 0.0)
            if r:
                icon  = "✓" if r.status == "ok" else "✗"
                short = r.notes[:38] + "…" if len(r.notes) > 41 else r.notes
                print(
                    f"  {task.agent_name:<22} {'run':<8} "
                    f"{icon} {r.status:<8} {r.records_touched:>5}  "
                    f"{dur:>5.1f}s  {short}"
                )

    # Verifier row
    v_result = next((r for r in results if r.agent == "Verifier"), None)
    if v_result:
        dur  = agent_durations.get("Verifier", 0.0)
        icon = "✓" if v_result.status == "ok" else "✗"
        print(
            f"  {'Verifier':<22} {'run':<8} "
            f"{icon} {v_result.status:<8} {v_result.records_touched:>5}  "
            f"{dur:>5.1f}s  {v_result.notes[:38]}"
        )

    print(f"  {'·' * (_W - 2)}")
    total_touched = sum(r.records_touched for r in results
                        if r.agent not in ("Verifier", "Verifier.retry"))
    print(f"  {'Outcome:':<28}{outcome}")
    print(f"  {'Verdict:':<28}{verdict_str}")
    print(f"  {'Retry count:':<28}{retry_count}")
    print(f"  {'Total records touched:':<28}{total_touched}")
    print(f"  {'Failed agents:':<28}{len(failed_agents)}")
    print(f"  {'Elapsed:':<28}{elapsed:.2f}s")
    print(f"{'─' * _W}\n")

    # One cycle summary row (rule 33)
    run_summary = ", ".join(
        f"{t.agent_name}={agent_durations.get(t.agent_name, 0):.1f}s"
        for t in run_tasks
    )
    skip_summary = ", ".join(
        f"{t.agent_name}(skipped: {t.reason.split('skipped: ')[-1][:40]})"
        for t in skip_tasks
    )
    failed_checks_str = (
        ", ".join(c.name for c in verdict.failed_checks)
        if verdict and not verdict.passed else ""
    )
    summary_notes = json.dumps({
        "outcome":       outcome,
        "verdict":       verdict_str,
        "failed_checks": failed_checks_str,
        "retry_count":   retry_count,
        "ran":           run_summary,
        "skipped":       skip_summary,
        "failed_agents": failed_agents,
        "elapsed_s":     round(elapsed, 2),
    })
    storage.log_cycle(
        path=config.db_path,
        agent="cycle",
        started_at=cycle_start.isoformat(),
        finished_at=cycle_end.isoformat(),
        records_touched=total_touched,
        status=outcome,
        notes=summary_notes,
    )

    return results
