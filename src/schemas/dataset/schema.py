from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from random import Random

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from src.agents.generation import DatasetGenerationConfig
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
    baseline_source_dir: Path | None = None
    fixed_inputs: Path | None = None
    generation: DatasetGenerationConfig | None = None
    fault_seed: int | None = None
    success_controls: int = Field(default=0, ge=0)
    max_parallel: int = Field(default=2, ge=1, le=10)
    max_attempts_per_scenario: int = Field(default=3, ge=1, le=25)
    baseline_batch_size: int = Field(default=10, ge=1, le=10)
    augmentations_per_baseline: int = Field(default=1, ge=1, le=18)
    fault_ids: list[str] = Field(min_length=1)
    scenarios: list[DatasetCampaignScenario] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_campaign(self) -> DatasetCampaignProfile:
        scenario_ids = [scenario.id for scenario in self.scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("Os ids dos cenários da campanha devem ser únicos.")
        if len(self.fault_ids) != len(set(self.fault_ids)):
            raise ValueError("fault_ids não pode conter valores repetidos.")
        for field_name in (
            "experiment_profile",
            "output_dir",
            "baseline_source_dir",
            "fixed_inputs",
        ):
            path = getattr(self, field_name)
            if path is None:
                continue
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{field_name} deve ser relativo ao diretório do repositório.")
        if self.baseline_source_dir == self.output_dir:
            raise ValueError("baseline_source_dir deve ser diferente de output_dir.")
        if self.fixed_inputs is not None:
            if self.generation is None or self.fault_seed is None:
                raise ValueError("fixed_inputs exige generation e fault_seed explícitos.")
            if self.baseline_source_dir is not None or self.augmentations_per_baseline != 1:
                raise ValueError("Entradas fixas exigem uma baseline própria por caso e modelo.")
        elif self.generation is not None:
            raise ValueError("generation exige fixed_inputs para preservar os comandos.")
        if self.success_controls and (
            self.fixed_inputs is None or self.success_controls >= self.planned_executions
        ):
            raise ValueError("Controles exigem entradas fixas e quantidade menor que o total.")
        if self.augmentations_per_baseline > len(self.fault_ids):
            raise ValueError(
                "augmentations_per_baseline não pode exceder a quantidade de fault_ids."
            )
        return self

    @computed_field
    @property
    def planned_baselines(self) -> int:
        return sum(scenario.executions for scenario in self.scenarios)

    @computed_field
    @property
    def planned_executions(self) -> int:
        return self.planned_baselines * self.augmentations_per_baseline

    def planned_fault_distribution(self, *, limit: int | None = None) -> dict[str, int]:
        total = (
            min(limit, self.planned_executions) if limit is not None else self.planned_executions
        )
        return dict(Counter(fault for fault in self.fault_schedule()[:total] if fault is not None))

    def fault_schedule(self) -> list[str | None]:
        if self.success_controls:
            rng = Random(self.fault_seed)
            allocations = [
                divmod(self.success_controls * spec.executions, self.planned_baselines)
                for spec in self.scenarios
            ]
            counts = [count for count, _ in allocations]
            remaining = self.success_controls - sum(counts)
            for index in sorted(range(len(counts)), key=lambda i: -allocations[i][1])[:remaining]:
                counts[index] += 1
            controls: set[int] = set()
            offset = 0
            for spec, count in zip(self.scenarios, counts, strict=True):
                controls.update(rng.sample(range(offset, offset + spec.executions), count))
                offset += spec.executions
            faults = [
                self.fault_ids[index % len(self.fault_ids)]
                for index in range(self.planned_executions - self.success_controls)
            ]
            rng.shuffle(faults)
            iterator = iter(faults)
            return [
                None if i in controls else next(iterator) for i in range(self.planned_executions)
            ]
        schedule = [
            self.fault_ids[index % len(self.fault_ids)] for index in range(self.planned_executions)
        ]
        if self.fault_seed is not None:
            Random(self.fault_seed).shuffle(schedule)
        return schedule


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
    fault_id: str | None
    critical_failure_step: int | None = Field(ge=1)
    critical_failure_module: ErrorModule | None
    critical_failure_type: ErrorType | None
    injection: dict[str, object]

    @model_validator(mode="after")
    def validate_label(self) -> DatasetGroundTruth:
        fields = (
            self.critical_failure_step,
            self.critical_failure_module,
            self.critical_failure_type,
        )
        if self.fault_id is None and any(value is not None for value in fields):
            raise ValueError("Controle correto não pode conter rótulo de falha.")
        if self.fault_id is not None and any(value is None for value in fields):
            raise ValueError("Falha injetada exige rótulo completo.")
        return self


class DatasetLabel(BaseModel):
    """Oráculo mínimo comparável ao ``critical_error`` produzido pela Frente B."""

    model_config = ConfigDict(extra="forbid")

    step: int = Field(ge=1)
    module: ErrorModule
    error_type: ErrorType

    @classmethod
    def from_ground_truth(cls, truth: DatasetGroundTruth) -> DatasetLabel:
        return cls(
            step=truth.critical_failure_step,
            module=truth.critical_failure_module,
            error_type=truth.critical_failure_type,
        )


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
    successful_controls: int = Field(default=0, ge=0)
    unique_baselines: int = Field(ge=0)
    augmentations_per_baseline: int = Field(ge=1)
    baseline_batches: int = Field(ge=0)
    rejected_baselines: int = Field(ge=0)
    scenario_errors: dict[str, list[str]] = Field(default_factory=dict)
