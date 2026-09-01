from __future__ import annotations

from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.schemas.agent_debug.schema import ErrorModule, ErrorType, is_valid_error_pair

_MAX_RESPONSES_PER_QUESTIONNAIRE = 20
_MAX_EVALUATIONS_PER_SCENARIO = 200


class ResearchFront(str, Enum):
    SECURITY = "security"
    ERROR_RECOVERY = "error_recovery"


class ResearchCapability(str, Enum):
    QUESTIONNAIRE_EVALUATOR = "questionnaire_evaluator"
    ERROR_RECOVERY = "error_recovery"


_FRONT_CAPABILITIES: dict[ResearchFront, frozenset[ResearchCapability]] = {
    ResearchFront.SECURITY: frozenset({ResearchCapability.QUESTIONNAIRE_EVALUATOR}),
    ResearchFront.ERROR_RECOVERY: frozenset({ResearchCapability.ERROR_RECOVERY}),
}


def research_front_supports(front: ResearchFront, capability: ResearchCapability) -> bool:
    """Informa as capacidades habilitadas por uma frente de pesquisa."""
    return capability in _FRONT_CAPABILITIES[front]


class PipelineProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    benign_commands: int = Field(default=3, ge=0, le=100)
    malicious_commands: int = Field(default=3, ge=0, le=100)
    benign_responses: int = Field(default=1, ge=0, le=20)
    malicious_responses: int = Field(default=1, ge=0, le=20)
    questionnaire_evaluator: bool = True

    @model_validator(mode="after")
    def validate_counts(self) -> PipelineProfile:
        command_total = self.benign_commands + self.malicious_commands
        response_total = self.benign_responses + self.malicious_responses
        if command_total < 1:
            raise ValueError("O perfil precisa gerar ao menos um comando.")
        if response_total > _MAX_RESPONSES_PER_QUESTIONNAIRE:
            raise ValueError("O perfil aceita no máximo 20 respostas por questionário.")
        if command_total * response_total > _MAX_EVALUATIONS_PER_SCENARIO:
            raise ValueError("O perfil aceita no máximo 200 avaliações potenciais.")
        if not self.questionnaire_evaluator and response_total:
            raise ValueError(
                "Sem questionnaire_evaluator, benign_responses e malicious_responses "
                "devem ser zero."
            )
        return self


def validate_front_pipeline(front: ResearchFront | None, pipeline: PipelineProfile) -> None:
    """Valida invariantes da pipeline que também se aplicam a chamadores diretos."""
    if (
        front is not None
        and pipeline.questionnaire_evaluator
        and not research_front_supports(front, ResearchCapability.QUESTIONNAIRE_EVALUATOR)
    ):
        raise ValueError("questionnaire_evaluator só pode ser habilitado na frente security.")


class ArtifactProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_dir: Path
    scenario_json: str = "scenario.json"
    benchmark_jsonl: str = "benchmark.jsonl"
    agent_debug_jsonl: str = "agent-debug.jsonl"
    trajectories_dir: str = "trajectories"

    @model_validator(mode="after")
    def validate_relative_names(self) -> ArtifactProfile:
        for field_name in (
            "scenario_json",
            "benchmark_jsonl",
            "agent_debug_jsonl",
            "trajectories_dir",
        ):
            value = Path(getattr(self, field_name))
            if value.is_absolute() or ".." in value.parts:
                raise ValueError(f"{field_name} deve ser relativo a output_dir.")
        return self

    @property
    def scenario_path(self) -> Path:
        return self.output_dir / self.scenario_json

    @property
    def benchmark_path(self) -> Path:
        return self.output_dir / self.benchmark_jsonl

    @property
    def agent_debug_path(self) -> Path:
        return self.output_dir / self.agent_debug_jsonl

    @property
    def trajectory_path(self) -> Path:
        return self.output_dir / self.trajectories_dir


class FaultMode(BaseModel):
    """Modo de falha catalogado para uma futura injeção + re-rollout."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    target_module: ErrorModule
    error_type: ErrorType
    description: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_taxonomy_pair(self) -> FaultMode:
        if not is_valid_error_pair(self.target_module, self.error_type):
            raise ValueError(
                f"error_type={self.error_type.value} não pertence ao módulo "
                f"{self.target_module.value}."
            )
        return self


class ErrorRecoveryProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capture_checkpoints: bool = True
    replay_enabled: bool = False
    fault_catalog: list[FaultMode] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_fault_ids(self) -> ErrorRecoveryProfile:
        ids = [mode.id for mode in self.fault_catalog]
        if len(ids) != len(set(ids)):
            raise ValueError("Os ids de fault_catalog devem ser únicos.")
        if self.replay_enabled:
            raise ValueError(
                "replay_enabled ainda não pode ser true: falta o contrato HTTP de re-rollout."
            )
        return self


class ExperimentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = Field(default="1.0", pattern=r"^1\.0$")
    name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    front: ResearchFront
    description: str = Field(min_length=1)
    pipeline: PipelineProfile
    artifacts: ArtifactProfile
    error_recovery: ErrorRecoveryProfile | None = None

    @model_validator(mode="after")
    def validate_front(self) -> ExperimentProfile:
        supports_error_recovery = research_front_supports(
            self.front, ResearchCapability.ERROR_RECOVERY
        )
        if supports_error_recovery and self.error_recovery is None:
            raise ValueError("A frente error_recovery exige a seção error_recovery.")
        if not supports_error_recovery and self.error_recovery is not None:
            raise ValueError("A frente security não deve declarar error_recovery.")
        validate_front_pipeline(self.front, self.pipeline)
        if (
            self.error_recovery is not None
            and self.error_recovery.capture_checkpoints
            and not self.artifacts.trajectories_dir
        ):
            raise ValueError("capture_checkpoints exige artifacts.trajectories_dir.")
        return self
