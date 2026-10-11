"""Adapts Front A executions to AgentDebug-RH."""

from src.services.agent_debug.service import (
    agent_debug_trajectory,
    failure_annotation,
    save_trajectory_files,
    scenario_trajectories,
)

__all__ = [
    "agent_debug_trajectory",
    "failure_annotation",
    "save_trajectory_files",
    "scenario_trajectories",
]
