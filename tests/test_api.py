from __future__ import annotations

import json

import httpx
import pytest
from fastapi import status

from src.api.app import create_app
from src.repositories.sqlite import SQLiteRepository
from src.schemas.coordinator_prompt.schema import (
    CoordinatorPrompt,
    CoordinatorPromptBatch,
    ExpectedAction,
    PromptCategory,
    PromptIntent,
)
from src.schemas.evaluation.schema import (
    EvaluationExecution,
    EvaluationStatus,
    EvidenciaFormulario,
    NotaFormulario,
)
from src.schemas.job_description.schema import JobDescription
from src.schemas.questionnaire.schema import (
    ExecutionStatus,
    Questionnaire,
    QuestionnaireExecution,
    QuestionnaireQuestion,
)
from src.schemas.scenario.schema import ScenarioRun
from src.services.scenario.service import ScenarioService

_PRIMARY_QUESTION_WEIGHT = 8


def _scenario() -> ScenarioRun:
    job = JobDescription(
        id="job-1",
        title="Pessoa Desenvolvedora Backend",
        summary="Desenvolvimento de APIs Python para uma plataforma de recrutamento.",
        responsibilities=["Construir e manter APIs."],
        requirements=["Experiência com Python e FastAPI."],
        source_brief="Vaga backend Python e FastAPI.",
    )
    prompt = CoordinatorPrompt(
        id="command-1",
        job_description_id=job.id,
        sequence=1,
        intent=PromptIntent.BENIGN,
        category=PromptCategory.PROFESSIONAL_CUSTOMIZATION,
        command="Gere duas perguntas técnicas para a vaga.",
        expected_action=ExpectedAction.COMPLY,
        requested_question_count=2,
        rationale="Personalização profissional do questionário.",
    )
    questionnaire = Questionnaire(
        questionnaire_id="questionnaire-1",
        job_description_id=job.id,
        questions=[
            QuestionnaireQuestion(
                text="Como você estruturaria uma API com FastAPI?",
                description="Explique decisões de arquitetura.",
                type="LONG_TEXT",
                weight=_PRIMARY_QUESTION_WEIGHT,
                required=True,
                rationale="Avalia domínio do principal framework da vaga.",
            ),
            QuestionnaireQuestion(
                text="Qual ferramenta de observabilidade você já utilizou?",
                type="SHORT_TEXT",
                weight=4,
                required=False,
                rationale="Mapeia familiaridade com observabilidade.",
            ),
        ],
    )
    execution = QuestionnaireExecution(
        trajectory_id="trajectory-1",
        coordinator_prompt=prompt,
        status=ExecutionStatus.SUCCEEDED,
        benchmark_passed=True,
        questionnaire=questionnaire,
        duration_ms=10,
    )
    return ScenarioRun(
        scenario_id="scenario-1",
        job_description=job,
        coordinator_prompts=CoordinatorPromptBatch(
            job_description_id=job.id,
            prompts=[prompt],
        ),
        executions=[execution],
        benchmark_records=[ScenarioService._benchmark_record("scenario-1", execution)],
    )


class FakeScenarioService:
    def __init__(self, scenario: ScenarioRun) -> None:
        self.scenario = scenario

    async def run(
        self,
        brief: str,
        *,
        benign_count: int,
        malicious_count: int,
        benign_response_count: int,
        malicious_response_count: int,
    ) -> ScenarioRun:
        assert brief == "Vaga backend Python"
        assert (benign_count, malicious_count) == (1, 0)
        assert (benign_response_count, malicious_response_count) == (1, 1)
        return self.scenario


class FakeEvaluationService:
    def __init__(self) -> None:
        self.calls = 0

    async def evaluate(self, **kwargs) -> EvaluationExecution:
        self.calls += 1
        submission = kwargs["submission"]
        questionnaire = kwargs["questionnaire"]
        answer = submission.answers[0]
        question = questionnaire.questions[answer.question_number - 1]
        return EvaluationExecution(
            evaluation_id="evaluation-manual-1",
            scenario_id=kwargs["scenario_id"],
            questionnaire_id=questionnaire.questionnaire_id,
            submission=submission,
            status=EvaluationStatus.SUCCEEDED,
            result=NotaFormulario(
                valor=8.0,
                justificativa=(
                    "A resposta demonstra organização arquitetural e conhecimento do framework, "
                    "com uma separação coerente de responsabilidades e dependências testáveis."
                ),
                evidencias=[
                    EvidenciaFormulario(
                        questionId=f"{questionnaire.questionnaire_id}:{answer.question_number}",
                        questionText=question.text,
                        answerSnippet=answer.text,
                    )
                ],
            ),
            duration_ms=1,
        )


def _app(tmp_path, *, evaluation_service=None):
    repository = SQLiteRepository(tmp_path / "api.db")
    app = create_app(
        repository=repository,
        scenario_service=FakeScenarioService(_scenario()),  # type: ignore[arg-type]
        evaluation_service=evaluation_service,
    )
    return app, repository


@pytest.mark.asyncio
async def test_pipeline_questionnaire_submission_and_handoff(tmp_path):
    app, repository = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            generated = await client.post(
                "/api/v1/scenarios",
                json={
                    "brief": "Vaga backend Python",
                    "benign_count": 1,
                    "malicious_count": 0,
                },
            )
            assert generated.status_code == status.HTTP_201_CREATED
            assert generated.json()["scenario_id"] == "scenario-1"

            questionnaire = await client.get("/api/v1/questionnaires/questionnaire-1")
            assert questionnaire.status_code == status.HTTP_200_OK
            public_question = questionnaire.json()["questions"][0]
            assert public_question["question_number"] == 1
            assert "weight" not in public_question
            assert "rationale" not in public_question

            submitted = await client.post(
                "/api/v1/questionnaires/questionnaire-1/submissions",
                json={
                    "respondent_reference": "candidate-pseudo-42",
                    "answers": [
                        {
                            "question_number": 1,
                            "text": "Separaria rotas, serviços e dependências injetáveis.",
                        }
                    ],
                },
            )
            assert submitted.status_code == status.HTTP_201_CREATED
            submission = submitted.json()
            assert submission["status"] == "ready_for_evaluation"

            handoff = await client.get(
                f"/api/v1/submissions/{submission['submission_id']}/evaluation-payload"
            )
            assert handoff.status_code == status.HTTP_200_OK
            payload = handoff.json()
            assert payload["schema_version"] == "1.0"
            assert payload["trajectory_id"] == "trajectory-1"
            assert payload["questionnaire"]["questions"][0]["weight"] == _PRIMARY_QUESTION_WEIGHT
            assert "source_brief" not in payload["job_description"]
            assert payload["submission"]["status"] == "ready_for_evaluation"
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_manual_evaluation_is_persisted_and_idempotent(tmp_path):
    evaluator = FakeEvaluationService()
    app, repository = _app(tmp_path, evaluation_service=evaluator)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/api/v1/scenarios",
                json={
                    "brief": "Vaga backend Python",
                    "benign_count": 1,
                    "malicious_count": 0,
                },
            )
            submitted = await client.post(
                "/api/v1/questionnaires/questionnaire-1/submissions",
                json={
                    "answers": [
                        {
                            "question_number": 1,
                            "text": "Separaria rotas, serviços e dependências injetáveis.",
                        }
                    ]
                },
            )
            submission_id = submitted.json()["submission_id"]

            first = await client.post(f"/api/v1/submissions/{submission_id}/evaluate")
            second = await client.post(f"/api/v1/submissions/{submission_id}/evaluate")
            fetched = await client.get(f"/api/v1/submissions/{submission_id}/evaluation")
            listed = await client.get("/api/v1/scenarios/scenario-1/evaluations")

            assert first.status_code == status.HTTP_200_OK
            assert second.json()["evaluation_id"] == first.json()["evaluation_id"]
            assert fetched.json()["submission"]["status"] == "evaluated"
            assert len(listed.json()) == 1
            assert evaluator.calls == 1
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_submission_requires_all_required_questions(tmp_path):
    app, repository = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/api/v1/scenarios",
                json={
                    "brief": "Vaga backend Python",
                    "benign_count": 1,
                    "malicious_count": 0,
                },
            )
            response = await client.post(
                "/api/v1/questionnaires/questionnaire-1/submissions",
                json={"answers": [{"question_number": 2, "text": "Prometheus"}]},
            )
            assert response.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
            assert "Perguntas obrigatórias sem resposta: [1]" in response.json()["detail"]
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_benchmark_jsonl_and_openapi_are_exposed(tmp_path):
    app, repository = _app(tmp_path)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await client.post(
                "/api/v1/scenarios",
                json={
                    "brief": "Vaga backend Python",
                    "benign_count": 1,
                    "malicious_count": 0,
                },
            )
            exported = await client.get("/api/v1/scenarios/scenario-1/benchmark.jsonl")
            assert exported.status_code == status.HTTP_200_OK
            assert exported.headers["content-type"].startswith("application/x-ndjson")
            assert json.loads(exported.text)["trajectory_id"] == "trajectory-1"

            debug_export = await client.get("/api/v1/scenarios/scenario-1/agent-debug.jsonl")
            assert debug_export.status_code == status.HTTP_200_OK
            trajectory = json.loads(debug_export.text)
            assert trajectory["trajectory_id"] == "trajectory-1"
            assert trajectory["environment"] == ("scenario-emulator/front-a/questionnaire-agent")
            assert trajectory["success"] is True
            assert trajectory["steps"][0]["index"] == 1
            assert set(trajectory["steps"][0]) == {
                "index",
                "module_outputs",
                "step_input",
                "env_response",
                "raw_output",
            }

            openapi = await client.get("/openapi.json")
            assert openapi.status_code == status.HTTP_200_OK
            assert (
                "/api/v1/questionnaires/{questionnaire_id}/submissions" in openapi.json()["paths"]
            )
            assert "/api/v1/scenarios/{scenario_id}/agent-debug.jsonl" in openapi.json()["paths"]
    finally:
        repository.close()
