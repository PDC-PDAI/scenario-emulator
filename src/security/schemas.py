from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SecurityVerdict(str, Enum):
    SAFE = "safe"
    INJECTION = "injection"
    POLICY_VIOLATION = "policy_violation"
    AMBIGUOUS = "ambiguous"


class SecurityAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: SecurityVerdict
    rationale: str = Field(min_length=10, max_length=1000)
    evidence: list[str] = Field(default_factory=list, max_length=10)


class ProfessionalContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role_title: str = Field(min_length=2, max_length=160)
    seniority: str | None = Field(default=None, max_length=80)
    responsibilities: list[str] = Field(min_length=1, max_length=20)
    competencies: list[str] = Field(min_length=1, max_length=30)
    professional_constraints: list[str] = Field(default_factory=list, max_length=20)
    source_evidence: list[str] = Field(min_length=1, max_length=30)

    @field_validator(
        "responsibilities", "competencies", "professional_constraints", "source_evidence"
    )
    @classmethod
    def reject_empty_items(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("Listas do contexto profissional não aceitam itens vazios.")
        return value


class AnswerExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    injection_detected: bool
    has_professional_merit: bool
    claims: list[str] = Field(default_factory=list, max_length=20)
    gaps: list[str] = Field(default_factory=list, max_length=20)
    source_evidence: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("claims", "gaps", "source_evidence")
    @classmethod
    def reject_empty_items(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("Listas da análise de resposta não aceitam itens vazios.")
        return value


class QuestionScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_number: int = Field(ge=1, le=50)
    valor: float = Field(ge=0, le=10)
    justificativa: str = Field(min_length=40, max_length=500)
