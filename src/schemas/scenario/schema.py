from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.schemas.benchmark.schema import BenchmarkRecord
from src.schemas.coordinator_prompt.schema import CoordinatorPromptBatch
from src.schemas.experiment.schema import ResearchFront
from src.schemas.job_description.schema import JobDescription
from src.schemas.questionnaire.schema import QuestionnaireExecution


class ScenarioRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(default_factory=lambda: f"scenario-{uuid.uuid4()}")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    research_targets: list[str] = Field(
        default_factory=lambda: ["AgentDebug-RH"]
    )
    research_front: ResearchFront | None = None
    experiment_profile: str | None = None
    job_description: JobDescription
    coordinator_prompts: CoordinatorPromptBatch
    executions: list[QuestionnaireExecution]
    benchmark_records: list[BenchmarkRecord]

    @model_validator(mode="before")
    @classmethod
    def accept_empty_legacy_security_fields(cls, value: object) -> object:
        """Read historical Front A checkpoints without retaining security payloads."""
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        for field in ("response_batches", "evaluation_executions"):
            if field in payload:
                if payload[field] != []:
                    raise ValueError("Security scenarios belong to RecruitSecBench.")
                payload.pop(field)
        return payload
