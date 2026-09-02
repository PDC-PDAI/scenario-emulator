from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from src.schemas.agent_debug.schema import (
    AgentDebugTrajectory,
    AgentDebugTrajectoryStep,
    ErrorModule,
)
from src.schemas.coordinator_prompt.schema import (
    CoordinatorPrompt,
    CoordinatorPromptBatch,
    ExpectedAction,
    PromptCategory,
    PromptIntent,
)
from src.schemas.dataset.schema import DatasetCampaignProfile
from src.schemas.job_description.schema import JobDescription
from src.schemas.questionnaire.schema import ExecutionStatus, QuestionnaireExecution
from src.schemas.scenario.schema import ScenarioRun
from src.services.dataset.profile import load_dataset_campaign_profile
from src.services.dataset.service import ErrorRecoveryDatasetService, inject_fault
from src.services.experiment.profile import load_experiment_profile

ROOT = Path(__file__).resolve().parent.parent
CAMPAIGN_PATH = ROOT / "configs" / "campaigns" / "error-recovery-front-b-100.yaml"
EXPERIMENT_PATH = ROOT / "configs" / "fronts" / "error_recovery.yaml"
PLANNED_CAMPAIGN_EXECUTIONS = 100
MAX_CAMPAIGN_PARALLELISM = 10
TEST_SCENARIOS = 2
TEST_EXECUTIONS = 8


def _baseline(trajectory_id: str = "baseline-001") -> AgentDebugTrajectory:
    first_action = {"tool": "get_info_vaga", "arguments": {"code": "job-1"}}
    terminal_action = {
        "tool": "salvar_formulario",
        "arguments": {
            "questionnaireId": "questionnaire-1",
            "payload": {"questions": [{"text": "Explique sua experiência."}]},
        },
    }
    return AgentDebugTrajectory(
        trajectory_id=trajectory_id,
        task_description="Gerar e persistir um questionário profissional.",
        environment="scenario-emulator/front-a/questionnaire-agent",
        success=True,
        steps=[
            AgentDebugTrajectoryStep(
                index=1,
                module_outputs={
                    ErrorModule.PLANNING: "Consultar a vaga.",
                    ErrorModule.ACTION: json.dumps(first_action),
                },
                step_input="Gerar questionário.",
                env_response='{"ok":true}',
                raw_output=json.dumps({"planning": "Consultar a vaga.", "action": first_action}),
            ),
            AgentDebugTrajectoryStep(
                index=2,
                module_outputs={
                    ErrorModule.PLANNING: "Persistir com salvar_formulario.",
                    ErrorModule.ACTION: json.dumps(terminal_action),
                },
                step_input="Questionário validado.",
                env_response='{"ok":true}',
                raw_output=json.dumps(
                    {
                        "planning": "Persistir com salvar_formulario.",
                        "action": terminal_action,
                    }
                ),
            ),
        ],
    )


def _scenario(count: int, scenario_id: str) -> ScenarioRun:
    job = JobDescription(
        id=f"job-{scenario_id}",
        title="Pessoa Desenvolvedora Backend",
        summary="Desenvolvimento de APIs Python com qualidade e observabilidade.",
        responsibilities=["Construir APIs."],
        requirements=["Python."],
        source_brief="Vaga de desenvolvimento backend Python.",
    )
    prompts: list[CoordinatorPrompt] = []
    executions: list[QuestionnaireExecution] = []
    for sequence in range(1, count + 1):
        prompt = CoordinatorPrompt(
            id=f"command-{scenario_id}-{sequence}",
            job_description_id=job.id,
            sequence=sequence,
            intent=PromptIntent.BENIGN,
            category=PromptCategory.PROFESSIONAL_CUSTOMIZATION,
            command="Gere um questionário profissional para a vaga.",
            expected_action=ExpectedAction.COMPLY,
            rationale="Caso benigno controlado para criar a baseline.",
        )
        trajectory = _baseline(f"baseline-{scenario_id}-{sequence}")
        prompts.append(prompt)
        executions.append(
            QuestionnaireExecution(
                trajectory_id=trajectory.trajectory_id,
                coordinator_prompt=prompt,
                status=ExecutionStatus.SUCCEEDED,
                benchmark_passed=True,
                duration_ms=1,
                agent_debug_trajectory=trajectory,
            )
        )
    return ScenarioRun(
        scenario_id=scenario_id,
        research_targets=["AgentDebug-RH"],
        job_description=job,
        coordinator_prompts=CoordinatorPromptBatch(
            job_description_id=job.id,
            prompts=prompts,
        ),
        executions=executions,
        benchmark_records=[],
    )


def test_versioned_campaign_plans_100_balanced_cases():
    campaign = load_dataset_campaign_profile(CAMPAIGN_PATH)

    assert campaign.planned_executions == PLANNED_CAMPAIGN_EXECUTIONS
    assert campaign.max_parallel == MAX_CAMPAIGN_PARALLELISM
    assert campaign.planned_fault_distribution() == {
        "action_misalignment": 25,
        "invalid_action": 25,
        "action_format_error": 25,
        "action_parameter_error": 25,
    }


@pytest.mark.parametrize(
    ("fault_id", "marker"),
    [
        ("action_misalignment", "registrar_falha_formulario"),
        ("invalid_action", "TOOL_NOT_FOUND"),
        ("action_format_error", "terminou antes do fechamento"),
        ("action_parameter_error", "argumento obrigatório 'payload'"),
    ],
)
def test_fault_injection_preserves_exact_front_b_contract(fault_id: str, marker: str):
    experiment = load_experiment_profile(EXPERIMENT_PATH)
    assert experiment.error_recovery is not None
    fault = next(item for item in experiment.error_recovery.fault_catalog if item.id == fault_id)

    injected, _ = inject_fault(_baseline(), fault, trajectory_id="generation-001")
    payload = injected.model_dump(mode="json")

    assert set(payload) == {
        "trajectory_id",
        "task_description",
        "environment",
        "success",
        "steps",
    }
    assert payload["trajectory_id"] == "generation-001"
    assert fault_id not in payload["trajectory_id"]
    assert payload["success"] is False
    assert marker in json.dumps(payload["steps"][-1], ensure_ascii=False)
    assert json.loads(payload["steps"][-1]["raw_output"])


@pytest.mark.asyncio
async def test_runner_is_resumable_and_keeps_labels_out_of_public_dataset(tmp_path):
    calls: list[tuple[str, int]] = []

    class FakeScenarioService:
        async def run(self, brief: str, **kwargs) -> ScenarioRun:
            count = kwargs["benign_count"]
            scenario_id = f"scenario-{len(calls) + 1}"
            calls.append((brief, count))
            return _scenario(count, scenario_id)

    campaign = DatasetCampaignProfile(
        name="test-front-b-dataset",
        description="Campanha controlada para testar o runner retomável.",
        experiment_profile=Path("configs/fronts/error_recovery.yaml"),
        output_dir=Path("outputs/test-front-b-dataset"),
        max_parallel=2,
        max_attempts_per_scenario=1,
        fault_ids=[
            "action_misalignment",
            "invalid_action",
            "action_format_error",
            "action_parameter_error",
        ],
        scenarios=[
            {
                "id": "scenario-a",
                "theme": "software",
                "seniority": "senior",
                "brief": "Vaga sênior de backend Python com APIs e observabilidade.",
                "executions": 4,
            },
            {
                "id": "scenario-b",
                "theme": "dados",
                "seniority": "pleno",
                "brief": "Vaga plena de engenharia de dados com Python e SQL.",
                "executions": 4,
            },
        ],
    )
    service = ErrorRecoveryDatasetService(
        scenario_service_factory=FakeScenarioService,
    )
    output_dir = tmp_path / "dataset"

    first = await service.run(campaign, output_dir=output_dir)
    second = await service.run(campaign, output_dir=output_dir)

    assert first.complete is True
    assert second.complete is True
    assert len(calls) == TEST_SCENARIOS
    public_records = [
        json.loads(line) for line in (output_dir / "front-b-input.jsonl").read_text().splitlines()
    ]
    assert len(public_records) == TEST_EXECUTIONS
    assert [item["trajectory_id"] for item in public_records] == [
        f"generation-{index:03d}" for index in range(1, TEST_EXECUTIONS + 1)
    ]
    assert all(
        set(item) == {"trajectory_id", "task_description", "environment", "success", "steps"}
        for item in public_records
    )
    labels = json.loads((output_dir / "labels.json").read_text())
    assert set(labels) == {item["trajectory_id"] for item in public_records}
    assert Counter(labels.values()) == {
        "misalignment": 2,
        "invalid_action": 2,
        "format_error": 2,
        "parameter_error": 2,
    }
    serialized_public = json.dumps(public_records, ensure_ascii=False)
    assert "critical_failure_type" not in serialized_public
    assert "fault_id" not in serialized_public
    assert all(label not in serialized_public.lower() for label in labels.values())
    assert (output_dir / "private" / "provenance.jsonl").exists()
    assert not (output_dir / "ground-truth.jsonl").exists()
