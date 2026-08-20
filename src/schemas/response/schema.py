from __future__ import annotations

import uuid
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.schemas.submission.schema import AnswerInput


class ResponseIntent(str, Enum):
    BENIGN = "benign"
    MALICIOUS = "malicious"


class ResponseAttackCategory(str, Enum):
    PROFESSIONAL_RESPONSE = "professional_response"
    SCORE_MANIPULATION = "score_manipulation"
    ROLE_OVERRIDE = "role_override"
    OUTPUT_SCHEMA_OVERRIDE = "output_schema_override"
    SYSTEM_PROMPT_EXFILTRATION = "system_prompt_exfiltration"


class GeneratedResponseCase(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    intent: ResponseIntent
    category: ResponseAttackCategory
    rationale: str = Field(min_length=5, max_length=1000)
    canary: str | None = Field(default=None, min_length=8, max_length=128)
    answers: list[AnswerInput] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_intent_contract(self) -> GeneratedResponseCase:
        if self.intent is ResponseIntent.BENIGN:
            if self.category is not ResponseAttackCategory.PROFESSIONAL_RESPONSE:
                raise ValueError("Resposta benigna deve usar professional_response.")
            if self.canary is not None:
                raise ValueError("Resposta benigna não pode conter canário de ataque.")
        else:
            if self.category is ResponseAttackCategory.PROFESSIONAL_RESPONSE:
                raise ValueError("Resposta maliciosa deve usar uma categoria de ataque.")
            if not self.canary:
                raise ValueError("Resposta maliciosa deve conter um canário.")
            if not any(self.canary in answer.text for answer in self.answers):
                raise ValueError("O canário deve aparecer em ao menos uma resposta maliciosa.")
        return self


class GeneratedResponseCaseBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: list[GeneratedResponseCase] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_unique_canaries(self) -> GeneratedResponseCaseBatch:
        canaries = [case.canary for case in self.cases if case.canary]
        if len(canaries) != len(set(canaries)):
            raise ValueError("Canários maliciosos devem ser únicos no lote.")
        return self


class ResponseCase(GeneratedResponseCase):
    case_id: str = Field(default_factory=lambda: f"response-case-{uuid.uuid4()}")
    questionnaire_id: str
    sequence: int = Field(ge=1)


class ResponseBatchStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ResponseGenerationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(default_factory=lambda: f"response-batch-{uuid.uuid4()}")
    questionnaire_id: str
    status: ResponseBatchStatus
    cases: list[ResponseCase] = Field(default_factory=list, max_length=20)
    failure_reason: str | None = None
    trace_id: str | None = None
    duration_ms: int = Field(ge=0)
