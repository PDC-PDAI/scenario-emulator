from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from src.schemas.agent_debug.schema import (
    AgentDebugTrajectory,
    AgentDebugTrajectoryStep,
    ErrorModule,
    ErrorType,
)


class DatasetCampaignScenario(BaseModel):
    """Um briefing e a quantidade de trajetórias que ele deve produzir."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    theme: str = Field(min_length=1)
    seniority: str = Field(min_length=1)
    brief: str = Field(min_length=20, max_length=10_000)
    executions: int = Field(ge=1, le=100)


class DatasetCampaignProfile(BaseModel):
    """Configuração versionada de uma coleta Frente A → Frente B."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")
    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    description: str = Field(min_length=1)
    experiment_profile: Path
    output_dir: Path
    max_parallel: int = Field(default=2, ge=1, le=10)
    max_attempts_per_scenario: int = Field(default=3, ge=1, le=10)
    fault_ids: list[str] = Field(min_length=1)
    scenarios: list[DatasetCampaignScenario] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_campaign(self) -> DatasetCampaignProfile:
        scenario_ids = [scenario.id for scenario in self.scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("Os ids dos cenários da campanha devem ser únicos.")
        if len(self.fault_ids) != len(set(self.fault_ids)):
            raise ValueError("fault_ids não pode conter valores repetidos.")
        for field_name in ("experiment_profile", "output_dir"):
            path = getattr(self, field_name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{field_name} deve ser relativo ao diretório do repositório.")
        return self

    @computed_field
    @property
    def planned_executions(self) -> int:
        return sum(scenario.executions for scenario in self.scenarios)

    def planned_fault_distribution(self, *, limit: int | None = None) -> dict[str, int]:
        total = (
            min(limit, self.planned_executions) if limit is not None else self.planned_executions
        )
        return dict(Counter(self.fault_ids[index % len(self.fault_ids)] for index in range(total)))


class FrontBTrajectory(BaseModel):
    """Contrato exato observado em ``front-b-input.json``."""

    model_config = ConfigDict(extra="forbid")

    trajectory_id: str
    task_description: str
    environment: str
    success: bool = False
    steps: list[AgentDebugTrajectoryStep] = Field(default_factory=list)

    @classmethod
    def from_agent_debug(cls, trajectory: AgentDebugTrajectory) -> FrontBTrajectory:
        return cls.model_validate(trajectory.model_dump(exclude={"messages"}))


class DatasetGroundTruth(BaseModel):
    """Rótulo separado da entrada para não vazar o diagnóstico à Frente B."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    trajectory_id: str
    parent_trajectory_id: str
    scenario_id: str
    theme: str
    seniority: str
    fault_id: str
    critical_failure_step: int = Field(ge=1)
    critical_failure_module: ErrorModule
    critical_failure_type: ErrorType
    injection: dict[str, object]


class DatasetRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    campaign: str
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    planned_executions: int = Field(ge=1)
    recorded_executions: int = Field(ge=0)
    pending_executions: int = Field(ge=0)
    complete: bool
    counts_by_fault: dict[str, int]
    baseline_batches: int = Field(ge=0)
    rejected_baselines: int = Field(ge=0)
    scenario_errors: dict[str, list[str]] = Field(default_factory=dict)
