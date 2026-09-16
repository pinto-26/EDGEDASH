"""
base.py — shared protocol and result type for all EdgeDash agents.

Every agent must implement the Agent protocol: a name and a run() method.
AgentResult is the standard return value that the Orchestrator inspects.

Stop conditions are passed in by the Orchestrator (rule 29). Agents must
respect them — they never decide their own limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from edgedash.config import Config
from edgedash.planning import StopConditions


@dataclass
class AgentResult:
    agent: str
    status: Literal["ok", "failed"]
    records_touched: int
    notes: str


@runtime_checkable
class Agent(Protocol):
    """
    Protocol that every agent must satisfy.

    Agents receive config, a db path, and stop_conditions set by the
    Orchestrator. They do their one job and return an AgentResult.
    """

    name: str

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: StopConditions | None = None,
    ) -> AgentResult:
        ...
