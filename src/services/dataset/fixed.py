from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, model_validator

from src.agents.generation import generation_scope
from src.schemas.coordinator_prompt.schema import CoordinatorPrompt, PromptIntent
from src.schemas.dataset.schema import DatasetCampaignProfile, DatasetRunSummary
from src.schemas.job_description.schema import JobDescription
from src.schemas.questionnaire.schema import QuestionnaireExecution
from src.services.agent_debug.service import agent_debug_trajectory
from src.services.dataset.controls import validate_control
from src.services.dataset.service import (
    _MAX_PARALLEL,
    _atomic_write,
    _Baseline,
    _generation_id,
    _terminal_step,
)

if TYPE_CHECKING:
    from src.services.dataset.service import ErrorRecoveryDatasetService


class FixedScenarioInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario_id: str
    job: JobDescription
    prompts: list[CoordinatorPrompt]

    @model_validator(mode="after")
    def validate_prompts(self) -> FixedScenarioInputs:
        ids = [prompt.id for prompt in self.prompts]
        if len(ids) != len(set(ids)):
            raise ValueError("IDs dos comandos devem ser únicos.")
        for sequence, prompt in enumerate(self.prompts, start=1):
            if prompt.job_description_id != self.job.id or prompt.sequence != sequence:
                raise ValueError("Comando não corresponde à vaga ou à sequência fixa.")
            if prompt.intent is not PromptIntent.BENIGN:
                raise ValueError("O corpus de baselines deve conter somente comandos benignos.")
        return self


def load_fixed_inputs(campaign: DatasetCampaignProfile) -> list[FixedScenarioInputs]:
    if campaign.fixed_inputs is None:
        raise ValueError("A campanha não tem fixed_inputs.")
    payload = json.loads(campaign.fixed_inputs.read_text(encoding="utf-8"))
    inputs = [FixedScenarioInputs.model_validate(item) for item in payload]
    if [item.scenario_id for item in inputs] != [spec.id for spec in campaign.scenarios]:
        raise ValueError("Cenários do corpus divergem da campanha.")
    for item, spec in zip(inputs, campaign.scenarios, strict=True):
        if len(item.prompts) != spec.executions:
            raise ValueError(f"Quantidade de comandos divergente: {spec.id}.")
    return inputs


def _protocol(campaign: DatasetCampaignProfile) -> dict[str, object]:
    # Protect prompts, tools, schemas, injection code and dependencies on resume.
    root = Path(__file__).resolve().parents[3]
    digest = hashlib.sha256()
    for source in sorted((root / "src").rglob("*.py")) + [root / "uv.lock"]:
        digest.update(str(source.relative_to(root)).encode())
        digest.update(source.read_bytes())
    return {
        "campaign": campaign.model_dump(mode="json"),
        "inputs_sha256": hashlib.sha256(campaign.fixed_inputs.read_bytes()).hexdigest(),
        "runtime_sha256": digest.hexdigest(),
        "experiment_sha256": hashlib.sha256(campaign.experiment_profile.read_bytes()).hexdigest(),
        "prompts": "versioned-local",
        "reasoning": {"enabled": False},
        "provider": {"require_parameters": True, "allow_fallbacks": False},
        "step_policy": "round_robin_compatible_or_semantic",
    }


def validate_clean_baseline(execution: QuestionnaireExecution, *, control: bool = False) -> None:
    """Não deixe uma falha natural recuperada preceder o rótulo sintético."""
    if not execution.benchmark_passed or execution.agent_debug_trajectory is None:
        raise ValueError(execution.failure_reason or "baseline não passou no oráculo")
    for step in execution.agent_debug_trajectory.steps:
        response = step.env_response
        while response is not None:
            if isinstance(response, str):
                try:
                    response = json.loads(response)
                except (ValueError, TypeError):
                    try:
                        response = ast.literal_eval(response)
                    except (ValueError, SyntaxError):
                        break
            if isinstance(response, dict):
                if response.get("error") or response.get("ok") is False:
                    raise ValueError(f"Baseline contém erro natural no step {step.index}.")
                response = response.get("result")
            else:
                break
    _terminal_step(execution.agent_debug_trajectory)
    if control:
        validate_control(execution.agent_debug_trajectory.model_dump(mode="json"))


async def run_fixed_dataset(
    service: ErrorRecoveryDatasetService,
    campaign: DatasetCampaignProfile,
    *,
    limit: int | None,
    max_parallel: int | None,
    output_dir: Path | None,
) -> DatasetRunSummary:
    if limit is not None and limit < 1:
        raise ValueError("limit deve ser maior que zero.")
    parallelism = max_parallel if max_parallel is not None else campaign.max_parallel
    if not 1 <= parallelism <= _MAX_PARALLEL:
        raise ValueError("max_parallel deve estar entre 1 e 10.")
    inputs = load_fixed_inputs(campaign)
    experiment, faults = service._experiment(campaign)
    planned = min(limit, campaign.planned_executions) if limit else campaign.planned_executions
    destination = output_dir or campaign.output_dir
    assert campaign.generation is not None
    semaphore = asyncio.Semaphore(parallelism)
    errors: dict[str, list[str]] = {}

    async def collect(position, spec, job, prompt):
        case_id = _generation_id(position, planned)
        cache = destination / "private" / "fixed-executions" / f"{case_id}.json"
        error_path = cache.with_suffix(".error.json")
        try:
            execution = (
                QuestionnaireExecution.model_validate_json(cache.read_text(encoding="utf-8"))
                if cache.exists()
                else None
            )
            if execution is not None:
                try:
                    validate_clean_baseline(execution, control=assignment[case_id] is None)
                except ValueError:
                    rejected_path = (
                        destination
                        / "private"
                        / "rejected-executions"
                        / f"{case_id}-{time.time_ns()}.json"
                    )
                    _atomic_write(rejected_path, execution.model_dump_json(indent=2) + "\n")
                    execution = None
            if execution is None:
                async with semaphore:
                    execution = (
                        await service._scenario_service_factory().questionnaire_service.execute(
                            job,
                            prompt,
                            scenario_id=f"fixed-{spec.id}",
                            fixed_input_id=case_id,
                            experiment_tags=["fixed-inputs", experiment.name],
                        )
                    )
                _atomic_write(cache, execution.model_dump_json(indent=2) + "\n")
            if execution.coordinator_prompt != prompt:
                raise ValueError("Checkpoint contém outro comando.")
            trajectory = agent_debug_trajectory(job, execution)
            validate_clean_baseline(execution, control=assignment[case_id] is None)
            _terminal_step(trajectory)
            error_path.unlink(missing_ok=True)
            return position, _Baseline(trajectory, f"fixed-{spec.id}", spec)
        except Exception as exc:  # noqa: BLE001 - manter o slot pendente, sem trocar o comando
            errors[case_id] = [f"{type(exc).__name__}: {exc}"]
            _atomic_write(error_path, json.dumps(errors[case_id], ensure_ascii=False) + "\n")
            return position, None

    with generation_scope(campaign.generation):
        service._write_manifest(
            campaign,
            experiment,
            output_dir=destination,
            planned=planned,
            protocol=_protocol(campaign),
        )
        assignment = service._fault_assignment(campaign, output_dir=destination, planned=planned)
        cases = []
        for item, spec in zip(inputs, campaign.scenarios, strict=True):
            for prompt in item.prompts:
                cases.append((len(cases) + 1, spec, item.job, prompt))
        results = await asyncio.gather(*(collect(*case) for case in cases[:planned]))
        accepted = [(position, baseline) for position, baseline in results if baseline is not None]
        summary = service._export_cases(
            campaign=campaign,
            baselines=[baseline for _, baseline in accepted],
            positions=[position for position, _ in accepted],
            faults=faults,
            output_dir=destination,
            planned=planned,
            batch_count=planned,
            rejected=len(errors),
            errors=errors,
            assignment=assignment,
        )
        truth = [
            json.loads(line)
            for line in (destination / "private" / "provenance.jsonl").read_text().splitlines()
        ]
        distribution = {
            "by_fault": summary.counts_by_fault,
            "successful_controls": summary.successful_controls,
            "controls_by_scenario": dict(
                Counter(item["scenario_id"] for item in truth if item["fault_id"] is None)
            ),
            "by_step": dict(
                Counter(
                    str(item["critical_failure_step"])
                    for item in truth
                    if item["fault_id"] is not None
                )
            ),
            "by_fault_and_step": dict(
                Counter(
                    f"{item['fault_id']}@{item['critical_failure_step']}"
                    for item in truth
                    if item["fault_id"] is not None
                )
            ),
            "by_baseline_length": dict(
                Counter(str(item["injection"]["baseline_step_count"]) for item in truth)
            ),
        }
        _atomic_write(
            destination / "private" / "distribution.json", json.dumps(distribution, indent=2) + "\n"
        )
        return summary
