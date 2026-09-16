from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from src.schemas.agent_debug.schema import (
    AgentDebugTrajectory,
    AgentDebugTrajectoryStep,
    ErrorModule,
    ErrorType,
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
EXPANDED_CAMPAIGN_PATH = ROOT / "configs" / "campaigns" / "error-recovery-front-b-220.yaml"
LARGE_CAMPAIGN_PATH = ROOT / "configs" / "campaigns" / "error-recovery-front-b-1100.yaml"
EXPERIMENT_PATH = ROOT / "configs" / "fronts" / "error_recovery.yaml"
PLANNED_CAMPAIGN_EXECUTIONS = 100
PLANNED_EXPANDED_EXECUTIONS = 220
PLANNED_LARGE_EXECUTIONS = 1_100
PLANNED_LARGE_BASELINES = 220
MAX_CAMPAIGN_PARALLELISM = 10
BASELINE_BATCH_SIZE = 10
LARGE_AUGMENTATIONS = 5
TEST_EXPANDED_BASELINES = 2
TEST_EXPANDED_EXECUTIONS = 4
TEST_SCENARIOS = 2
TEST_EXECUTIONS = 8
ERROR_STEP = 2
FAULT_VARIANTS = 3


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


def test_expanded_campaign_balances_eleven_supported_fault_pairs():
    campaign = load_dataset_campaign_profile(EXPANDED_CAMPAIGN_PATH)

    assert campaign.planned_executions == PLANNED_EXPANDED_EXECUTIONS
    assert campaign.max_parallel == MAX_CAMPAIGN_PARALLELISM
    assert campaign.baseline_batch_size == BASELINE_BATCH_SIZE
    assert set(campaign.planned_fault_distribution().values()) == {20}
    assert set(campaign.planned_fault_distribution()) == {
        "planning_constraint_ignorance",
        "planning_impossible_action",
        "planning_inefficient_plan",
        "action_misalignment",
        "invalid_action",
        "action_format_error",
        "action_parameter_error",
        "system_step_limit",
        "system_tool_execution_error",
        "system_llm_limit",
        "system_environment_error",
    }


def test_large_campaign_reuses_baselines_and_keeps_faults_balanced():
    campaign = load_dataset_campaign_profile(LARGE_CAMPAIGN_PATH)

    assert campaign.planned_baselines == PLANNED_LARGE_BASELINES
    assert campaign.planned_executions == PLANNED_LARGE_EXECUTIONS
    assert campaign.augmentations_per_baseline == LARGE_AUGMENTATIONS
    assert campaign.baseline_source_dir == Path("outputs/error-recovery-front-b-220")
    assert set(campaign.planned_fault_distribution().values()) == {100}


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


@pytest.mark.parametrize(
    ("fault_id", "module", "error_type", "target_step", "marker"),
    [
        (
            "planning_constraint_ignorance",
            ErrorModule.PLANNING,
            ErrorType.CONSTRAINT_IGNORANCE,
            2,
            "não cobre os critérios explícitos",
        ),
        (
            "planning_impossible_action",
            ErrorModule.PLANNING,
            ErrorType.IMPOSSIBLE_ACTION,
            1,
            "Pré-condição ausente",
        ),
        (
            "planning_inefficient_plan",
            ErrorModule.PLANNING,
            ErrorType.INEFFICIENT_PLAN,
            2,
            "mesmos dados",
        ),
        (
            "system_step_limit",
            ErrorModule.SYSTEM,
            ErrorType.STEP_LIMIT,
            1,
            "maximum_steps_reached",
        ),
        (
            "system_tool_execution_error",
            ErrorModule.SYSTEM,
            ErrorType.TOOL_EXECUTION_ERROR,
            2,
            "external_failure",
        ),
        (
            "system_llm_limit",
            ErrorModule.SYSTEM,
            ErrorType.LLM_LIMIT,
            2,
            "provider excedeu o tempo",
        ),
        (
            "system_environment_error",
            ErrorModule.SYSTEM,
            ErrorType.ENVIRONMENT_ERROR,
            2,
            "state_transition_failed",
        ),
    ],
)
def test_new_fault_injectors_expose_unambiguous_evidence_and_private_metadata(
    fault_id: str,
    module: ErrorModule,
    error_type: ErrorType,
    target_step: int,
    marker: str,
):
    experiment = load_experiment_profile(EXPERIMENT_PATH)
    assert experiment.error_recovery is not None
    fault = next(item for item in experiment.error_recovery.fault_catalog if item.id == fault_id)

    injected, metadata = inject_fault(_baseline(), fault, trajectory_id="generation-001")
    serialized = injected.model_dump_json()

    assert injected.success is False
    assert metadata["target_step"] == target_step
    assert metadata["target_module"] == module.value
    assert metadata["error_type"] == error_type.value
    assert metadata["variant_id"]
    assert marker.casefold() in serialized.casefold()
    assert [step.index for step in injected.steps] == list(range(1, len(injected.steps) + 1))


@pytest.mark.parametrize(
    "fault_id",
    [
        "planning_constraint_ignorance",
        "planning_impossible_action",
        "planning_inefficient_plan",
        "action_misalignment",
        "invalid_action",
        "action_format_error",
        "action_parameter_error",
        "system_step_limit",
        "system_tool_execution_error",
        "system_llm_limit",
        "system_environment_error",
    ],
)
def test_each_supported_fault_rotates_between_three_variants(fault_id: str):
    experiment = load_experiment_profile(EXPERIMENT_PATH)
    assert experiment.error_recovery is not None
    fault = next(item for item in experiment.error_recovery.fault_catalog if item.id == fault_id)

    variants = {
        inject_fault(
            _baseline(),
            fault,
            trajectory_id=f"generation-{index + 1:03d}",
            variant_index=index,
        )[1]["variant_id"]
        for index in range(3)
    }

    assert len(variants) == FAULT_VARIANTS


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
    assert all(set(label) == {"step", "module", "error_type"} for label in labels.values())
    assert all(label["step"] == ERROR_STEP for label in labels.values())
    assert all(label["module"] == "action" for label in labels.values())
    assert Counter(label["error_type"] for label in labels.values()) == {
        "misalignment": 2,
        "invalid_action": 2,
        "format_error": 2,
        "parameter_error": 2,
    }
    serialized_public = json.dumps(public_records, ensure_ascii=False)
    assert "critical_failure_type" not in serialized_public
    assert "fault_id" not in serialized_public
    assert all(
        label["error_type"] not in serialized_public.lower() for label in labels.values()
    )
    assert (output_dir / "private" / "provenance.jsonl").exists()
    assert not (output_dir / "ground-truth.jsonl").exists()


@pytest.mark.asyncio
async def test_runner_collects_large_scenario_in_bounded_baseline_batches(tmp_path):
    requested_batches: list[int] = []

    class FakeScenarioService:
        async def run(self, brief: str, **kwargs) -> ScenarioRun:
            count = kwargs["benign_count"]
            requested_batches.append(count)
            return _scenario(count, f"scenario-{len(requested_batches)}")

    campaign = DatasetCampaignProfile(
        name="test-bounded-batches",
        description="Campanha que exige mais de um lote do coordenador.",
        experiment_profile=Path("configs/fronts/error_recovery.yaml"),
        output_dir=Path("outputs/test-bounded-batches"),
        max_parallel=1,
        max_attempts_per_scenario=3,
        baseline_batch_size=10,
        fault_ids=["invalid_action"],
        scenarios=[
            {
                "id": "scenario-a",
                "theme": "software",
                "seniority": "senior",
                "brief": "Vaga sênior de backend Python com APIs e observabilidade.",
                "executions": 22,
            }
        ],
    )

    summary = await ErrorRecoveryDatasetService(
        scenario_service_factory=FakeScenarioService,
    ).run(campaign, output_dir=tmp_path / "dataset")

    assert summary.complete is True
    assert requested_batches == [10, 10, 2]


@pytest.mark.asyncio
async def test_runner_expands_cached_baselines_without_calling_scenario_service(
    tmp_path,
    monkeypatch,
):
    (tmp_path / "experiment.yaml").write_text(EXPERIMENT_PATH.read_text(encoding="utf-8"))
    monkeypatch.chdir(tmp_path)

    class FakeScenarioService:
        async def run(self, brief: str, **kwargs) -> ScenarioRun:
            return _scenario(kwargs["benign_count"], "scenario-source")

    base_campaign = DatasetCampaignProfile(
        name="test-source-dataset",
        description="Campanha pequena que produz o cache usado pela expansão.",
        experiment_profile=Path("experiment.yaml"),
        output_dir=Path("source"),
        max_attempts_per_scenario=1,
        fault_ids=["invalid_action", "action_parameter_error"],
        scenarios=[
            {
                "id": "scenario-a",
                "theme": "software",
                "seniority": "senior",
                "brief": "Vaga sênior de backend Python com APIs e observabilidade.",
                "executions": 2,
            }
        ],
    )
    source_summary = await ErrorRecoveryDatasetService(
        scenario_service_factory=FakeScenarioService,
    ).run(base_campaign, output_dir=tmp_path / "source")
    assert source_summary.complete is True

    class ForbiddenScenarioService:
        async def run(self, brief: str, **kwargs) -> ScenarioRun:
            raise AssertionError("A expansão offline não pode chamar o gerador de cenários.")

    expanded_campaign = base_campaign.model_copy(
        update={
            "name": "test-expanded-dataset",
            "output_dir": Path("expanded"),
            "baseline_source_dir": Path("source"),
            "augmentations_per_baseline": 2,
        }
    )
    summary = await ErrorRecoveryDatasetService(
        scenario_service_factory=ForbiddenScenarioService,
    ).run(expanded_campaign, output_dir=tmp_path / "expanded")

    assert summary.complete is True
    assert summary.recorded_executions == TEST_EXPANDED_EXECUTIONS
    assert summary.unique_baselines == TEST_EXPANDED_BASELINES
    assert summary.augmentations_per_baseline == TEST_EXPANDED_BASELINES
    provenance = [
        json.loads(line)
        for line in (tmp_path / "expanded/private/provenance.jsonl").read_text().splitlines()
    ]
    by_parent: dict[str, list[dict[str, object]]] = {}
    for item in provenance:
        by_parent.setdefault(item["parent_trajectory_id"], []).append(item)
    assert {parent: len(items) for parent, items in by_parent.items()} == {
        "baseline-scenario-source-1": TEST_EXPANDED_BASELINES,
        "baseline-scenario-source-2": TEST_EXPANDED_BASELINES,
    }
    assert all(
        len({item["fault_id"] for item in items}) == TEST_EXPANDED_BASELINES
        for items in by_parent.values()
    )


def test_legacy_front_a_checkpoints_drop_only_empty_security_fields():
    original = _scenario(1, "legacy-front-a")
    payload = original.model_dump(mode="json")
    payload.update(response_batches=[], evaluation_executions=[])
    loaded = ScenarioRun.model_validate(payload)
    assert loaded == original
    assert "response_batches" not in loaded.model_dump()
    assert "evaluation_executions" not in loaded.model_dump()
    payload["response_batches"] = [{"case_id": "security-case"}]
    with pytest.raises(ValueError, match="RecruitSecBench"):
        ScenarioRun.model_validate(payload)
