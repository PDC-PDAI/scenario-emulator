from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from src.schemas.benchmark.schema import BenchmarkRecord
from src.schemas.coordinator_prompt.schema import CoordinatorPromptBatch
from src.schemas.job_description.schema import JobDescription
from src.schemas.questionnaire.schema import QuestionnaireExecution


class ScenarioRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str = Field(default_factory=lambda: f"scenario-{uuid.uuid4()}")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    research_targets: list[str] = Field(
        default_factory=lambda: ["AgentDebug-RH", "RecruitSecBench"]
    )
    job_description: JobDescription
    coordinator_prompts: CoordinatorPromptBatch
    executions: list[QuestionnaireExecution]
    benchmark_records: list[BenchmarkRecord]
