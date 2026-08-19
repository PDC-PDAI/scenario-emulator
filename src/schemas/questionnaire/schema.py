from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from src.schemas.coordinator_prompt.schema import CoordinatorPrompt


class QuestionType(str, Enum):
    SHORT_TEXT = "SHORT_TEXT"
    LONG_TEXT = "LONG_TEXT"


class QuestionnaireQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=1000)
    type: QuestionType
    weight: int = Field(ge=1, le=10)
    required: bool = True
    rationale: str = Field(min_length=1, max_length=500)


class LLMQuestionnaireResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    reasoning: str | None = Field(default=None, max_length=5000)
    questions: list[QuestionnaireQuestion] = Field(min_length=1, max_length=50)


class QuestionnairePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[QuestionnaireQuestion] = Field(min_length=1, max_length=50)


class Questionnaire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questionnaire_id: str
    job_description_id: str
    questions: list[QuestionnaireQuestion] = Field(min_length=1, max_length=50)


class ExecutionStatus(str, Enum):
    SUCCEEDED = "succeeded"
    REFUSED = "refused"
    FAILED = "failed"


class QuestionnaireExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trajectory_id: str
    coordinator_prompt: CoordinatorPrompt
    status: ExecutionStatus
    benchmark_passed: bool
    failure_reason: str | None = None
    questionnaire: Questionnaire | None = None
    reasoning_summary: str | None = None
    trace_id: str | None = None
    duration_ms: int = Field(ge=0)
