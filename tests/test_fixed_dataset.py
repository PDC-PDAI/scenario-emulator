from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agents.generation import dataset_generation, generation_scope
from src.agents.model import build_model, configured_model_identifier
from src.prompts.manager import resolve_prompt
from src.schemas.agent_debug.schema import AgentDebugTrajectory
from src.services.dataset.fixed import load_fixed_inputs, validate_clean_baseline
from src.services.dataset.profile import load_dataset_campaign_profile
from src.services.dataset.service import ErrorRecoveryDatasetService, inject_fault
from src.services.experiment.profile import load_experiment_profile
from src.settings import settings
from tests.test_dataset_campaign import _baseline, _scenario
from tests.test_success_controls import control as control  # noqa: PLC0414 - fixture compartilhada

CAMPAIGN = Path("configs/campaigns/front-a-fixed-300.yaml")
SAMPLE_COUNT = 300
SMOKE_COUNT = 5


@pytest.mark.parametrize(
    "response",
    [
        '{"error": "True", "result": "Missing tool arguments"}',
        "{\"result\": \"{'ok': False, 'error': 'SAVE_FAILED'}\"}",
        '{"result": {"ok": false}}',
    ],
)
def test_successful_baseline_with_recovered_tool_error_is_rejected(response):
    execution = _scenario(1, "recovered-error").executions[0]
    execution.agent_debug_trajectory.steps[0].env_response = response
    with pytest.raises(ValueError, match="erro natural"):
        validate_clean_baseline(execution)


def test_fixed_corpus_and_fault_schedule_are_shared_and_balanced(tmp_path):
    campaign = load_dataset_campaign_profile(CAMPAIGN)
    corpus = load_fixed_inputs(campaign)
    commands = [prompt.command for item in corpus for prompt in item.prompts]
    assert len(commands) == len(set(commands)) == campaign.planned_executions == SAMPLE_COUNT
    assert campaign.generation.model == "google/gemma-4-31b-it"
    service = ErrorRecoveryDatasetService()
    full = service._fault_assignment(campaign, output_dir=tmp_path / "gemma", planned=SAMPLE_COUNT)
    other = service._fault_assignment(campaign, output_dir=tmp_path / "qwen", planned=SAMPLE_COUNT)
    smoke = service._fault_assignment(campaign, output_dir=tmp_path / "smoke", planned=SMOKE_COUNT)
    assert full == other
    assert smoke == dict(list(full.items())[:SMOKE_COUNT])
    assert list(full.values()).count(None) == campaign.success_controls == 60  # noqa: PLR2004
    assert set(Counter(v for v in full.values() if v is not None).values()) == {21, 22}
    for offset in range(0, SAMPLE_COUNT, 30):
        assert list(full.values())[offset : offset + 30].count(None) == 6  # noqa: PLR2004


def test_fixed_generation_uses_exact_parameters_and_local_prompts(monkeypatch):
    campaign = load_dataset_campaign_profile(CAMPAIGN)
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setattr(settings, "OPENAI_REASONING_EFFORT", "high")

    def unexpected_remote_prompt():
        pytest.fail("A campanha fixa não deve consultar prompts remotos.")

    monkeypatch.setattr("src.prompts.manager.get_langfuse_client", unexpected_remote_prompt)
    with generation_scope(campaign.generation):
        model = build_model()
        assert configured_model_identifier() == campaign.generation.model
        assert model.temperature == 0
        assert model.top_p == 1
        assert model.max_tokens == campaign.generation.max_tokens
        request = model.get_request_params()
        assert request["temperature"] == 0
        assert request["top_p"] == 1
        assert request["max_tokens"] == campaign.generation.max_tokens
        assert model.reasoning_effort is None
        assert model.extra_body == {
            "reasoning": {"enabled": False},
            "provider": {"require_parameters": True, "allow_fallbacks": False},
        }
        prompt = resolve_prompt("test", "Olá {{name}}", variables={"name": "vaga"})
        assert prompt.content == "Olá vaga"
        assert prompt.source == "local"
    assert dataset_generation.get() is None


@pytest.mark.parametrize(
    "fault_id",
    [
        "invalid_action",
        "action_format_error",
        "system_tool_execution_error",
        "system_llm_limit",
    ],
)
def test_step_rotation_balances_compatible_positions_without_inventing_steps(fault_id):
    experiment = load_experiment_profile(Path("configs/fronts/error_recovery.yaml"))
    fault = next(fault for fault in experiment.error_recovery.fault_catalog if fault.id == fault_id)
    baseline = _baseline()
    baseline.steps.insert(1, baseline.steps[0].model_copy(update={"index": 2}, deep=True))
    baseline.steps[-1].index = 3
    selected = []
    for variant in range(27):
        injected, metadata = inject_fault(
            baseline,
            fault,
            trajectory_id=f"case-{variant}",
            variant_index=variant,
            distribute_steps=True,
        )
        target = metadata["target_step"]
        selected.append(target)
        assert [step.index for step in injected.steps] == list(range(1, target + 1))
        assert metadata["compatible_steps"] == [1, 2, 3]
        assert metadata["baseline_step_count"] == len(baseline.steps)
    assert Counter(selected) == {1: 9, 2: 9, 3: 9}


@pytest.mark.asyncio
async def test_fixed_runner_keeps_failed_slots_and_resumes_without_shifting_labels(tmp_path):
    campaign = load_dataset_campaign_profile(CAMPAIGN).model_copy(update={"success_controls": 0})
    calls = []
    fail = True

    class FakeQuestionnaire:
        async def execute(self, job, prompt, **kwargs):
            case_id = kwargs["fixed_input_id"]
            calls.append(
                (case_id, job.model_dump(), prompt.model_dump(), configured_model_identifier())
            )
            if fail and case_id == "generation-002":
                raise RuntimeError("Temporary provider failure")
            execution = _scenario(1, case_id).executions[0]
            execution.coordinator_prompt = prompt
            return execution

    service = ErrorRecoveryDatasetService(
        scenario_service_factory=lambda: SimpleNamespace(questionnaire_service=FakeQuestionnaire())
    )
    destination = tmp_path / "dataset"
    first = await service.run(campaign, limit=SMOKE_COUNT, output_dir=destination)
    assert first.pending_executions == 1
    labels_before = json.loads((destination / "labels.json").read_text())
    assert "generation-002" not in labels_before
    assert "generation-005" in labels_before
    fail = False
    second = await service.run(campaign, limit=SMOKE_COUNT, output_dir=destination)
    assert second.complete
    assert len(calls) == SMOKE_COUNT + 1
    labels_after = json.loads((destination / "labels.json").read_text())
    assert all(labels_after[key] == value for key, value in labels_before.items())
    public_before = (destination / "front-b-input.jsonl").read_bytes()
    await service.run(campaign, limit=SMOKE_COUNT, output_dir=destination)
    assert public_before == (destination / "front-b-input.jsonl").read_bytes()
    assert len(calls) == SMOKE_COUNT + 1
    assert not (destination / "private/fixed-executions/generation-002.error.json").exists()
    distribution = json.loads((destination / "private/distribution.json").read_text())
    assert sum(distribution["by_step"].values()) == SMOKE_COUNT

    other_model = campaign.model_copy(
        update={
            "generation": campaign.generation.model_copy(update={"model": "qwen/qwen3.5-9b"}),
        }
    )
    with pytest.raises(ValueError, match="Protocolo/modelo/limite"):
        await service.run(other_model, limit=SMOKE_COUNT, output_dir=destination)
    await service.run(other_model, limit=SMOKE_COUNT, output_dir=tmp_path / "qwen")
    first_inputs = {case_id: (job, prompt) for case_id, job, prompt, _ in calls[:SMOKE_COUNT]}
    last_inputs = {case_id: (job, prompt) for case_id, job, prompt, _ in calls[-SMOKE_COUNT:]}
    assert first_inputs == last_inputs
    assert dataset_generation.get() is None


@pytest.mark.asyncio
async def test_full_fixed_campaign_exports_mixed_cases_without_leaking_labels(tmp_path, control):
    campaign = load_dataset_campaign_profile(CAMPAIGN)

    class FakeQuestionnaire:
        async def execute(self, job, prompt, **kwargs):
            execution = _scenario(1, kwargs["fixed_input_id"]).executions[0]
            execution.coordinator_prompt = prompt
            execution.agent_debug_trajectory = AgentDebugTrajectory.model_validate(
                control
            ).model_copy(update={"trajectory_id": execution.trajectory_id}, deep=True)
            return execution

    service = ErrorRecoveryDatasetService(
        scenario_service_factory=lambda: SimpleNamespace(questionnaire_service=FakeQuestionnaire())
    )
    summary = await service.run(campaign, output_dir=tmp_path)
    assert summary.complete
    assert summary.recorded_executions == summary.unique_baselines == SAMPLE_COUNT
    assert set(summary.counts_by_fault.values()) == {21, 22}
    assert summary.successful_controls == campaign.success_controls
    records = [
        json.loads(line) for line in (tmp_path / "front-b-input.jsonl").read_text().splitlines()
    ]
    assert sum(record["success"] for record in records) == campaign.success_controls
    labels = json.loads((tmp_path / "labels.json").read_text())
    assert sum(value is None for value in labels.values()) == campaign.success_controls
    blind = [
        json.loads(line) for line in (tmp_path / "detector-input.jsonl").read_text().splitlines()
    ]
    assert len(blind) == SAMPLE_COUNT
    assert all(
        set(row) == {"trajectory_id", "task_description", "environment", "steps"} for row in blind
    )
    assert {row["trajectory_id"] for row in blind} == set(labels)
    assert all(
        set(record) == {"trajectory_id", "task_description", "environment", "success", "steps"}
        for record in records
    )


@pytest.mark.asyncio
async def test_recovered_natural_error_is_retried_and_original_is_archived(tmp_path):
    campaign = load_dataset_campaign_profile(CAMPAIGN).model_copy(update={"success_controls": 0})
    calls = 0

    class FakeQuestionnaire:
        async def execute(self, job, prompt, **kwargs):
            nonlocal calls
            calls += 1
            execution = _scenario(1, kwargs["fixed_input_id"]).executions[0]
            execution.coordinator_prompt = prompt
            if calls == 1:
                execution.agent_debug_trajectory.steps[0].env_response = '{"error": "True"}'
            return execution

    service = ErrorRecoveryDatasetService(
        scenario_service_factory=lambda: SimpleNamespace(questionnaire_service=FakeQuestionnaire())
    )
    first = await service.run(campaign, output_dir=tmp_path, limit=1)
    assert not first.complete
    assert first.recorded_executions == 0
    second = await service.run(campaign, output_dir=tmp_path, limit=1)
    assert second.complete
    assert calls == 2  # noqa: PLR2004
    rejected = list((tmp_path / "private/rejected-executions").glob("*.json"))
    assert len(rejected) == 1
    archived = json.loads(rejected[0].read_text())
    assert archived["agent_debug_trajectory"]["steps"][0]["env_response"] == '{"error": "True"}'
