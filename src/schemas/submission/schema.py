from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.schemas.job_description.schema import JobDescriptionDraft
from src.schemas.questionnaire.schema import Questionnaire


class AnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question_number: int = Field(ge=1, le=50)
    text: str = Field(min_length=1, max_length=10_000)


class QuestionnaireSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    respondent_reference: str | None = Field(default=None, min_length=1, max_length=128)
    answers: list[AnswerInput] = Field(min_length=1, max_length=50)


class SubmissionStatus(str, Enum):
    READY_FOR_EVALUATION = "ready_for_evaluation"


class QuestionnaireSubmission(BaseModel):
    """Pacote de respostas que será avaliado por um serviço posterior."""

    model_config = ConfigDict(extra="forbid")

    submission_id: str = Field(default_factory=lambda: f"submission-{uuid.uuid4()}")
    scenario_id: str
    questionnaire_id: str
    respondent_reference: str | None = None
    answers: list[AnswerInput] = Field(min_length=1, max_length=50)
    status: SubmissionStatus = SubmissionStatus.READY_FOR_EVALUATION
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class JobDescriptionContext(JobDescriptionDraft):
    id: str


class EvaluationInputPayload(BaseModel):
    """Envelope autocontido para a etapa externa que fará a avaliação."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    scenario_id: str
    trajectory_id: str
    job_description: JobDescriptionContext
    questionnaire: Questionnaire
    submission: QuestionnaireSubmission
