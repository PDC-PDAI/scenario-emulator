"""Contrato de integração com a pipeline AgentDebug-RH."""

from src.schemas.agent_debug.schema import (
    AgentDebugTrajectory,
    AgentDebugTrajectoryStep,
    ErrorModule,
    ErrorType,
    FailureAnnotation,
    FailureCode,
)

__all__ = [
    "AgentDebugTrajectory",
    "AgentDebugTrajectoryStep",
    "ErrorModule",
    "ErrorType",
    "FailureAnnotation",
    "FailureCode",
]
