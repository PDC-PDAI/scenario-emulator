from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import src.services.scenario.service as scenario_module
from src.schemas.coordinator_prompt.schema import (
    CoordinatorPrompt,
    CoordinatorPromptBatch,
    ExpectedAction,
    PromptCategory,
    PromptIntent,
)
from src.schemas.job_description.schema import JobDescription
from src.schemas.questionnaire.schema import ExecutionStatus, QuestionnaireExecution
from src.services.scenario.service import ScenarioService

_QUESTIONNAIRE_COUNT = 2


def _job() -> JobDescription:
    return JobDescription(
        id="job-1",
        title="Pessoa Desenvolvedora Backend",
        summary="Desenvolvimento de APIs Python para uma plataforma de recrutamento.",
        responsibilities=["Construir e manter APIs."],
        requirements=["Experiência com Python e FastAPI."],
        source_brief="Vaga backend Python e FastAPI.",
    )


def _prompt(*, sequence: int, intent: PromptIntent) -> CoordinatorPrompt:
    benign = intent is PromptIntent.BENIGN
    return CoordinatorPrompt(
        id=f"command-{sequence}",
        job_description_id="job-1",
        sequence=sequence,
        intent=intent,
        category=(
            PromptCategory.PROFESSIONAL_CUSTOMIZATION if benign else PromptCategory.PROMPT_INJECTION
        ),
        command="Gere perguntas técnicas para a vaga.",
        expected_action=ExpectedAction.COMPLY if benign else ExpectedAction.REFUSE,
        rationale="Caso de teste do progresso do pipeline.",
    )


class FakeJobService:
    async def generate(self, brief: str, *, scenario_id: str) -> JobDescription:
        assert brief == "Vaga backend Python"
        assert scenario_id.startswith("scenario-")
        return _job()


class FakeCoordinatorService:
    async def generate(
        self,
        job: JobDescription,
        *,
        benign_count: int,
        malicious_count: int,
        scenario_id: str,
    ) -> CoordinatorPromptBatch:
        assert job.id == "job-1"
        assert (benign_count, malicious_count) == (_QUESTIONNAIRE_COUNT, 0)
        assert scenario_id.startswith("scenario-")
        return CoordinatorPromptBatch(
            job_description_id=job.id,
            prompts=[
                _prompt(sequence=1, intent=PromptIntent.BENIGN),
                _prompt(sequence=2, intent=PromptIntent.BENIGN),
            ],
        )


class FakeQuestionnaireService:
    async def execute(
        self,
        job: JobDescription,
        coordinator_prompt: CoordinatorPrompt,
        *,
        scenario_id: str,
        experiment_tags: list[str] | None = None,
        experiment_metadata: dict[str, str | None] | None = None,
    ) -> QuestionnaireExecution:
        assert job.id == "job-1"
        assert scenario_id.startswith("scenario-")
        refused = coordinator_prompt.expected_action is ExpectedAction.REFUSE
        return QuestionnaireExecution(
            trajectory_id=f"trajectory-{coordinator_prompt.sequence}",
            coordinator_prompt=coordinator_prompt,
            status=ExecutionStatus.REFUSED if refused else ExecutionStatus.SUCCEEDED,
            benchmark_passed=True,
            duration_ms=10,
        )


class FailingJobService:
    async def generate(self, brief: str, *, scenario_id: str) -> JobDescription:
        raise RuntimeError("falha simulada")


@pytest.mark.asyncio
async def test_pipeline_logs_each_stage_with_canonical_tags(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(scenario_module, "logger", logger)
    service = ScenarioService(
        job_service=FakeJobService(),  # type: ignore[arg-type]
        coordinator_service=FakeCoordinatorService(),  # type: ignore[arg-type]
        questionnaire_service=FakeQuestionnaireService(),  # type: ignore[arg-type]
    )

    result = await service.run("Vaga backend Python", benign_count=2, malicious_count=0)

    started = [
        call.kwargs
        for call in logger.info.call_args_list
        if call.kwargs["status"] == "started" and call.kwargs["stage"] != "scenario"
    ]
    assert [entry["stage"] for entry in started] == [
        "job-description",
        "coordinator-prompts",
        "questionnaire",
        "questionnaire",
    ]
    assert [entry["locus"] for entry in started] == [
        "JOB",
        "COORDINATOR",
        "SUB-GER",
        "SUB-GER",
    ]
    assert [entry["tags"] for entry in started] == [
        ["front-a", "JOB_DESCRIPTION", "JOB"],
        ["front-a", "COORDINATOR_PROMPTS", "COORDINATOR"],
        ["front-a", "QUESTIONNAIRE", "SUB-GER"],
        ["front-a", "QUESTIONNAIRE", "SUB-GER"],
    ]
    assert [entry["stage_tag"] for entry in started] == [
        "JOB_DESCRIPTION",
        "COORDINATOR_PROMPTS",
        "QUESTIONNAIRE",
        "QUESTIONNAIRE",
    ]
    assert started[2]["questionnaire_index"] == 1
    assert started[2]["questionnaire_total"] == _QUESTIONNAIRE_COUNT
    assert started[3]["prompt_intent"] == "benign"
    assert len(result.executions) == _QUESTIONNAIRE_COUNT

    completed = [
        call.kwargs
        for call in logger.info.call_args_list
        if call.kwargs["status"] == "completed" and call.kwargs["stage"] == "scenario"
    ]
    assert completed[0]["status"] == "completed"
    assert completed[0]["passed"] == _QUESTIONNAIRE_COUNT
    assert completed[0]["failed"] == 0


@pytest.mark.asyncio
async def test_pipeline_logs_the_stage_that_failed(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(scenario_module, "logger", logger)
    service = ScenarioService(job_service=FailingJobService())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="falha simulada"):
        await service.run("Vaga backend Python", benign_count=1, malicious_count=0)

    failure = logger.exception.call_args
    assert failure.args[0].startswith("[JOB_DESCRIPTION]")
    assert failure.kwargs["stage"] == "job-description"
    assert failure.kwargs["stage_tag"] == "JOB_DESCRIPTION"
    assert failure.kwargs["locus"] == "JOB"
    assert failure.kwargs["status"] == "failed"
    assert failure.kwargs["tags"] == ["front-a", "JOB_DESCRIPTION", "JOB"]
