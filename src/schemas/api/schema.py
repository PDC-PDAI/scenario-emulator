from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.schemas.questionnaire.schema import QuestionType

_MAX_PROMPTS_PER_SCENARIO = 100
_MAX_RESPONSES_PER_QUESTIONNAIRE = 20
_MAX_EVALUATIONS_PER_SCENARIO = 200


class ScenarioCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    brief: str = Field(min_length=3, max_length=10_000)
    benign_count: int = Field(default=3, ge=0, le=100)
    malicious_count: int = Field(default=3, ge=0, le=100)
    benign_response_count: int = Field(default=1, ge=0, le=20)
    malicious_response_count: int = Field(default=1, ge=0, le=20)

    @model_validator(mode="after")
    def validate_prompt_count(self) -> ScenarioCreateRequest:
        total = self.benign_count + self.malicious_count
        if total < 1:
            raise ValueError("Informe ao menos um comando de coordenador.")
        if total > _MAX_PROMPTS_PER_SCENARIO:
            raise ValueError(f"O cenário aceita no máximo {_MAX_PROMPTS_PER_SCENARIO} comandos.")
        response_total = self.benign_response_count + self.malicious_response_count
        if response_total > _MAX_RESPONSES_PER_QUESTIONNAIRE:
            raise ValueError(
                f"O cenário aceita no máximo {_MAX_RESPONSES_PER_QUESTIONNAIRE} "
                "respostas por questionário."
            )
        if total * response_total > _MAX_EVALUATIONS_PER_SCENARIO:
            raise ValueError(
                f"O cenário aceita no máximo {_MAX_EVALUATIONS_PER_SCENARIO} avaliações potenciais."
            )
        return self


class ScenarioSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    created_at: datetime
    job_description_id: str
    job_title: str
    execution_count: int = Field(ge=0)
    available_questionnaires: int = Field(ge=0)
    benchmark_passed: int = Field(ge=0)
    evaluation_count: int = Field(default=0, ge=0)
    evaluation_benchmark_passed: int = Field(default=0, ge=0)


class JobDescriptionView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    summary: str


class PublicQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question_number: int = Field(ge=1, le=50)
    text: str
    description: str | None = None
    type: QuestionType
    required: bool


class QuestionnaireView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questionnaire_id: str
    scenario_id: str
    trajectory_id: str
    job: JobDescriptionView
    questions: list[PublicQuestion] = Field(min_length=1, max_length=50)


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    service: str = "scenario-emulator"
    version: str
