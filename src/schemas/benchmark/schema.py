from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.schemas.agent_debug.schema import FailureAnnotation


class BenchmarkRecord(BaseModel):
    """Superconjunto dos campos centrais do AgentErrorBench descritos no relatório."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    trajectory_id: str
    llm: str = Field(alias="LLM")
    task_type: str = "questionnaire_generation"
    critical_failure_step: int | None = None
    critical_failure_module: str | None = None
    critical_failure_type: str | None = None
    step_annotations: list[dict[str, Any]] = Field(default_factory=list)
    failure_annotation: FailureAnnotation | None = None

    provenance: dict[str, Any]
    graph: dict[str, Any]
    critical_repair_set: list[str] = Field(default_factory=list)
    node_annotations: list[dict[str, Any]] = Field(default_factory=list)
    outcome: dict[str, Any]
    cascade: list[dict[str, Any]] = Field(default_factory=list)
