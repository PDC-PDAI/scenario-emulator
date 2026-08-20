from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.schemas.response.schema import ResponseCase
from src.schemas.submission.schema import QuestionnaireSubmission


class NotaLLM(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valor: float = Field(ge=0, le=10)
    justificativa: str = Field(min_length=1)
    evidencias: list[str] = Field(default_factory=list)


class EvidenciaFormulario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questionId: str
    questionText: str
    answerSnippet: str = Field(min_length=1, max_length=1000)
    relevancia: Literal["ALTA", "MEDIA", "BAIXA"] = "MEDIA"


class NotaFormulario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tipo: Literal["FORMULARIO"] = "FORMULARIO"
    valor: float = Field(ge=0, le=10)
    justificativa: str = Field(min_length=100, max_length=2000)
    evidencias: list[EvidenciaFormulario] = Field(min_length=1, max_length=50)


class EvaluationStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class OracleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    benchmark_applicable: bool
    passed: bool
    checks: dict[str, bool]
    failures: list[str] = Field(default_factory=list)


class EvaluationExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evaluation_id: str = Field(default_factory=lambda: f"evaluation-{uuid.uuid4()}")
    trajectory_id: str = Field(default_factory=lambda: f"evaluation-trajectory-{uuid.uuid4()}")
    scenario_id: str
    questionnaire_id: str
    submission: QuestionnaireSubmission
    response_case: ResponseCase | None = None
    status: EvaluationStatus
    result: NotaFormulario | None = None
    oracle: OracleResult | None = None
    failure_reason: str | None = None
    trace_id: str | None = None
    duration_ms: int = Field(ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
