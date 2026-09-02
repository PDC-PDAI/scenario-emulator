from __future__ import annotations

import asyncio
import json
import random
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

import structlog

from src.agents.model import configured_model_identifier
from src.schemas.agent_debug.schema import (
    AgentDebugTrajectory,
    AgentDebugTrajectoryStep,
    ErrorModule,
    ErrorType,
    raw_output_envelope,
)
from src.schemas.dataset.schema import (
    DatasetCampaignProfile,
    DatasetCampaignScenario,
    DatasetGroundTruth,
    DatasetRunSummary,
    FrontBTrajectory,
)
from src.schemas.experiment.schema import ExperimentProfile, FaultMode, ResearchFront
from src.schemas.scenario.schema import ScenarioRun
from src.services.agent_debug.service import agent_debug_trajectory
from src.services.experiment.profile import load_experiment_profile
from src.services.scenario.service import ScenarioService

logger = structlog.get_logger(__name__)

_SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_PARALLEL = 10
_SUPPORTED_FAULT_TYPES = frozenset(
    {
        ErrorType.MISALIGNMENT,
        ErrorType.INVALID_ACTION,
        ErrorType.FORMAT_ERROR,
        ErrorType.PARAMETER_ERROR,
    }
)


@dataclass(frozen=True)
class _Baseline:
    trajectory: AgentDebugTrajectory
    scenario_id: str
    spec: DatasetCampaignScenario


@dataclass
class _ScenarioCollection:
    baselines: list[_Baseline] = field(default_factory=list)
    batch_count: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def _json_text(payload: object, *, indent: int | None = None) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=indent is None)


def _safe_filename(value: str) -> str:
    rendered = _SAFE_FILENAME.sub("_", value).strip("._")
    return rendered or "trajectory"


def _generation_id(position: int, planned: int) -> str:
    width = max(3, len(str(planned)))
    return f"generation-{position:0{width}d}"


def _action_payload(step: AgentDebugTrajectoryStep) -> dict[str, object] | None:
    raw = step.module_outputs.get(ErrorModule.ACTION, "")
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _terminal_step(trajectory: AgentDebugTrajectory) -> AgentDebugTrajectoryStep:
    for step in reversed(trajectory.steps):
        payload = _action_payload(step)
        if payload and payload.get("tool") == "salvar_formulario":
            return step
    raise ValueError(
        f"A trajetória {trajectory.trajectory_id} não possui action:salvar_formulario."
    )


def _replace_step(
    trajectory: AgentDebugTrajectory,
    *,
    target_index: int,
    action_output: str,
    raw_action: object,
    env_response: dict[str, object],
) -> list[AgentDebugTrajectoryStep]:
    steps: list[AgentDebugTrajectoryStep] = []
    for original in trajectory.steps:
        if original.index != target_index:
            steps.append(original.model_copy(deep=True))
            continue
        modules = dict(original.module_outputs)
        modules[ErrorModule.ACTION] = action_output
        planning = modules.get(ErrorModule.PLANNING, "")
        steps.append(
            original.model_copy(
                deep=True,
                update={
                    "module_outputs": modules,
                    "env_response": _json_text(env_response),
                    "raw_output": raw_output_envelope(planning, raw_action),
                },
            )
        )
    return steps


def inject_fault(
    trajectory: AgentDebugTrajectory,
    fault: FaultMode,
    *,
    trajectory_id: str,
) -> tuple[FrontBTrajectory, dict[str, object]]:
    """Aplica uma mutação controlada em um checkpoint real da Frente A."""
    if not trajectory.success:
        raise ValueError("A injeção exige uma trajetória baseline bem-sucedida.")
    if fault.target_module is not ErrorModule.ACTION:
        raise ValueError(f"A campanha suporta somente action; recebido {fault.target_module}.")
    if fault.error_type not in _SUPPORTED_FAULT_TYPES:
        raise ValueError(f"Tipo de falha não suportado: {fault.error_type.value}.")

    target = _terminal_step(trajectory)
    original_action = _action_payload(target)
    if original_action is None:
        raise ValueError("A action terminal não contém um objeto JSON.")
    original_arguments = original_action.get("arguments")
    arguments = dict(original_arguments) if isinstance(original_arguments, dict) else {}
    expected_tool = str(original_action.get("tool") or "salvar_formulario")

    if fault.error_type is ErrorType.INVALID_ACTION:
        injected_tool = "salvar_formulario_v2"
        action = {"tool": injected_tool, "arguments": arguments}
        env_response = {"error": "TOOL_NOT_FOUND", "tool": injected_tool}
        injection = {
            "operation": "replace_tool_name",
            "expected_tool": expected_tool,
            "injected_tool": injected_tool,
        }
        action_output = _json_text(action)
        raw_action: object = action
    elif fault.error_type is ErrorType.PARAMETER_ERROR:
        invalid_arguments = dict(arguments)
        invalid_arguments.pop("payload", None)
        action = {"tool": expected_tool, "arguments": invalid_arguments}
        env_response = {
            "ok": False,
            "tool": expected_tool,
            "message": "O argumento obrigatório 'payload' não foi informado.",
        }
        injection = {
            "operation": "remove_required_parameter",
            "tool": expected_tool,
            "removed_parameter": "payload",
        }
        action_output = _json_text(action)
        raw_action = action
    elif fault.error_type is ErrorType.FORMAT_ERROR:
        action_output = '{"tool":"salvar_formulario","arguments":'
        raw_action = action_output
        env_response = {
            "ok": False,
            "message": "A saída da ação terminou antes do fechamento do objeto JSON.",
            "position": len(action_output),
        }
        injection = {
            "operation": "malform_action_json",
            "expected_tool": expected_tool,
        }
    else:
        injected_tool = "registrar_falha_formulario"
        questionnaire_id = arguments.get("questionnaireId", "questionnaire-unknown")
        action = {
            "tool": injected_tool,
            "arguments": {
                "questionnaireId": questionnaire_id,
                "errorReason": "LLM_FAILURE",
            },
        }
        env_response = {
            "result": {"ok": True, "failure_registered": True},
            "tool": injected_tool,
        }
        injection = {
            "operation": "replace_action_with_conflicting_terminal_tool",
            "planned_tool": expected_tool,
            "injected_tool": injected_tool,
        }
        action_output = _json_text(action)
        raw_action = action

    steps = _replace_step(
        trajectory,
        target_index=target.index,
        action_output=action_output,
        raw_action=raw_action,
        env_response=env_response,
    )
    injected = AgentDebugTrajectory(
        trajectory_id=trajectory_id,
        task_description=trajectory.task_description,
        environment=trajectory.environment,
        success=False,
        steps=steps,
    )
    return FrontBTrajectory.from_agent_debug(injected), injection


class ErrorRecoveryDatasetService:
    """Executa baselines reais e os projeta em casos controlados da Frente B."""

    def __init__(
        self,
        *,
        scenario_service_factory: Callable[[], ScenarioService] = ScenarioService,
    ) -> None:
        self._scenario_service_factory = scenario_service_factory

    @staticmethod
    def _experiment(
        campaign: DatasetCampaignProfile,
    ) -> tuple[ExperimentProfile, dict[str, FaultMode]]:
        experiment = load_experiment_profile(campaign.experiment_profile)
        if experiment.front is not ResearchFront.ERROR_RECOVERY:
            raise ValueError("A campanha de dataset exige um perfil da frente error_recovery.")
        if experiment.error_recovery is None:
            raise ValueError("O perfil não contém a seção error_recovery.")
        catalog = {fault.id: fault for fault in experiment.error_recovery.fault_catalog}
        missing = sorted(set(campaign.fault_ids) - set(catalog))
        if missing:
            raise ValueError(f"Falhas ausentes no fault_catalog: {', '.join(missing)}")
        selected = {fault_id: catalog[fault_id] for fault_id in campaign.fault_ids}
        for fault in selected.values():
            if (
                fault.target_module is not ErrorModule.ACTION
                or fault.error_type not in _SUPPORTED_FAULT_TYPES
            ):
                raise ValueError(f"Falha não suportada pelo injetor: {fault.id}")
        return experiment, selected

    @staticmethod
    def _target_scenarios(
        campaign: DatasetCampaignProfile,
        limit: int | None,
    ) -> list[tuple[DatasetCampaignScenario, int]]:
        remaining = (
            campaign.planned_executions
            if limit is None
            else min(limit, campaign.planned_executions)
        )
        targets: list[tuple[DatasetCampaignScenario, int]] = []
        for scenario in campaign.scenarios:
            if remaining <= 0:
                break
            target = min(scenario.executions, remaining)
            targets.append((scenario, target))
            remaining -= target
        return targets

    @staticmethod
    def _extract_baselines(
        scenario: ScenarioRun,
        spec: DatasetCampaignScenario,
    ) -> tuple[list[_Baseline], int]:
        accepted: list[_Baseline] = []
        rejected = 0
        for execution in scenario.executions:
            trajectory = agent_debug_trajectory(scenario.job_description, execution)
            try:
                if not trajectory.success:
                    raise ValueError("baseline não passou no oráculo")
                _terminal_step(trajectory)
            except ValueError:
                rejected += 1
                continue
            accepted.append(
                _Baseline(
                    trajectory=trajectory,
                    scenario_id=scenario.scenario_id,
                    spec=spec,
                )
            )
        return accepted, rejected

    async def _collect_scenario(
        self,
        *,
        campaign: DatasetCampaignProfile,
        experiment: ExperimentProfile,
        spec: DatasetCampaignScenario,
        target: int,
        output_dir: Path,
        semaphore: asyncio.Semaphore,
    ) -> _ScenarioCollection:
        collection = _ScenarioCollection()
        batch_dir = output_dir / "private" / "baseline-scenarios" / spec.id
        batch_dir.mkdir(parents=True, exist_ok=True)
        seen_trajectories: set[str] = set()

        for attempt in range(1, campaign.max_attempts_per_scenario + 1):
            batch_path = batch_dir / f"batch-{attempt:02d}.json"
            error_path = batch_dir / f"batch-{attempt:02d}.error.json"
            scenario: ScenarioRun | None = None

            if batch_path.exists():
                scenario = ScenarioRun.model_validate_json(batch_path.read_text(encoding="utf-8"))
            elif error_path.exists():
                payload = json.loads(error_path.read_text(encoding="utf-8"))
                collection.errors.append(str(payload.get("error", "erro desconhecido")))
                continue
            else:
                missing = target - len(collection.baselines)
                if missing <= 0:
                    break
                logger.info(
                    "dataset.scenario.started",
                    campaign=campaign.name,
                    scenario=spec.id,
                    attempt=attempt,
                    requested=missing,
                )
                try:
                    async with semaphore:
                        scenario = await self._scenario_service_factory().run(
                            spec.brief,
                            benign_count=missing,
                            malicious_count=0,
                            benign_response_count=0,
                            malicious_response_count=0,
                            questionnaire_evaluator=False,
                            research_front=ResearchFront.ERROR_RECOVERY,
                            experiment_profile=experiment.name,
                        )
                    _atomic_write(batch_path, scenario.model_dump_json(indent=2) + "\n")
                except Exception as exc:  # noqa: BLE001 - erro fica persistido para retomada
                    message = f"{type(exc).__name__}: {exc}"
                    collection.errors.append(message)
                    _atomic_write(
                        error_path,
                        _json_text(
                            {
                                "scenario": spec.id,
                                "attempt": attempt,
                                "recorded_at": datetime.now(UTC).isoformat(),
                                "error": message,
                            },
                            indent=2,
                        )
                        + "\n",
                    )
                    logger.error(
                        "dataset.scenario.failed",
                        campaign=campaign.name,
                        scenario=spec.id,
                        attempt=attempt,
                        error=message,
                    )
                    continue

            if scenario is None:
                continue
            collection.batch_count += 1
            baselines, rejected = self._extract_baselines(scenario, spec)
            collection.rejected += rejected
            for baseline in baselines:
                trajectory_id = baseline.trajectory.trajectory_id
                if trajectory_id in seen_trajectories:
                    continue
                seen_trajectories.add(trajectory_id)
                collection.baselines.append(baseline)
                if len(collection.baselines) >= target:
                    break
            if len(collection.baselines) >= target:
                break

        return collection

    @staticmethod
    def _write_manifest(
        campaign: DatasetCampaignProfile,
        experiment: ExperimentProfile,
        *,
        output_dir: Path,
        planned: int,
    ) -> None:
        path = output_dir / "private" / "manifest.json"
        created_at = datetime.now(UTC).isoformat()
        if path.exists():
            try:
                created_at = json.loads(path.read_text(encoding="utf-8"))["created_at"]
            except (KeyError, TypeError, ValueError):
                pass
        payload = {
            "schema_version": "1.0",
            "campaign": campaign.name,
            "description": campaign.description,
            "created_at": created_at,
            "experiment_profile": experiment.name,
            "model": configured_model_identifier(),
            "planned_executions": planned,
            "planned_fault_distribution": campaign.planned_fault_distribution(limit=planned),
            "front_b_contract_fields": [
                "trajectory_id",
                "task_description",
                "environment",
                "success",
                "steps",
            ],
            "fault_ids": campaign.fault_ids,
            "scenarios": [scenario.model_dump(mode="json") for scenario in campaign.scenarios],
        }
        _atomic_write(path, _json_text(payload, indent=2) + "\n")

    @staticmethod
    def _fault_assignment(
        campaign: DatasetCampaignProfile,
        *,
        output_dir: Path,
        planned: int,
    ) -> dict[str, str]:
        """Cria uma atribuição balanceada sem codificar o rótulo no ID público."""
        path = output_dir / "private" / "fault-assignment.json"
        generation_ids = [_generation_id(position, planned) for position in range(1, planned + 1)]
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("private/fault-assignment.json deve conter um objeto.")
            assignment = {str(key): str(value) for key, value in payload.items()}
            if set(assignment) != set(generation_ids):
                raise ValueError(
                    "A atribuição privada existente não corresponde ao tamanho da campanha. "
                    "Use outro output_dir para executar com um limit diferente."
                )
            if not set(assignment.values()) <= set(campaign.fault_ids):
                raise ValueError("A atribuição privada contém fault_id desconhecido.")
            return {generation_id: assignment[generation_id] for generation_id in generation_ids}

        schedule = [campaign.fault_ids[index % len(campaign.fault_ids)] for index in range(planned)]
        random.SystemRandom().shuffle(schedule)
        assignment = dict(zip(generation_ids, schedule, strict=True))
        _atomic_write(path, _json_text(assignment, indent=2) + "\n")
        return assignment

    @staticmethod
    def _export_cases(
        *,
        campaign: DatasetCampaignProfile,
        baselines: list[_Baseline],
        faults: dict[str, FaultMode],
        output_dir: Path,
        planned: int,
        batch_count: int,
        rejected: int,
        errors: dict[str, list[str]],
        assignment: dict[str, str],
    ) -> DatasetRunSummary:
        inputs: list[FrontBTrajectory] = []
        ground_truth: list[DatasetGroundTruth] = []
        inputs_dir = output_dir / "front-b-inputs"
        baselines_dir = output_dir / "private" / "baseline-trajectories"

        for position, baseline in enumerate(baselines[:planned], start=1):
            generation_id = _generation_id(position, planned)
            fault_id = assignment[generation_id]
            fault = faults[fault_id]
            injected, injection = inject_fault(
                baseline.trajectory,
                fault,
                trajectory_id=generation_id,
            )
            target_step = _terminal_step(baseline.trajectory).index
            truth = DatasetGroundTruth(
                case_id=generation_id,
                trajectory_id=injected.trajectory_id,
                parent_trajectory_id=baseline.trajectory.trajectory_id,
                scenario_id=baseline.scenario_id,
                theme=baseline.spec.theme,
                seniority=baseline.spec.seniority,
                fault_id=fault_id,
                critical_failure_step=target_step,
                critical_failure_module=fault.target_module,
                critical_failure_type=fault.error_type,
                injection=injection,
            )
            inputs.append(injected)
            ground_truth.append(truth)
            _atomic_write(
                inputs_dir / f"{_safe_filename(injected.trajectory_id)}.json",
                injected.model_dump_json(indent=2) + "\n",
            )
            _atomic_write(
                baselines_dir / f"{_safe_filename(baseline.trajectory.trajectory_id)}.json",
                baseline.trajectory.model_dump_json(indent=2, exclude_none=True) + "\n",
            )

        _atomic_write(
            output_dir / "front-b-input.jsonl",
            "".join(item.model_dump_json() + "\n" for item in inputs),
        )
        _atomic_write(
            output_dir / "private" / "provenance.jsonl",
            "".join(item.model_dump_json() + "\n" for item in ground_truth),
        )
        labels = {item.trajectory_id: item.critical_failure_type.value for item in ground_truth}
        _atomic_write(output_dir / "labels.json", _json_text(labels, indent=2) + "\n")
        counts = Counter(item.fault_id for item in ground_truth)
        recorded = len(inputs)
        summary = DatasetRunSummary(
            campaign=campaign.name,
            planned_executions=planned,
            recorded_executions=recorded,
            pending_executions=max(0, planned - recorded),
            complete=recorded == planned,
            counts_by_fault=dict(counts),
            baseline_batches=batch_count,
            rejected_baselines=rejected,
            scenario_errors=errors,
        )
        _atomic_write(
            output_dir / "private" / "summary.json",
            summary.model_dump_json(indent=2) + "\n",
        )
        return summary

    async def run(
        self,
        campaign: DatasetCampaignProfile,
        *,
        limit: int | None = None,
        max_parallel: int | None = None,
        output_dir: Path | None = None,
    ) -> DatasetRunSummary:
        if limit is not None and limit < 1:
            raise ValueError("limit deve ser maior que zero.")
        experiment, faults = self._experiment(campaign)
        destination = output_dir or campaign.output_dir
        planned = min(limit, campaign.planned_executions) if limit else campaign.planned_executions
        parallelism = max_parallel or campaign.max_parallel
        if not 1 <= parallelism <= _MAX_PARALLEL:
            raise ValueError("max_parallel deve estar entre 1 e 10.")
        destination.mkdir(parents=True, exist_ok=True)
        self._write_manifest(
            campaign,
            experiment,
            output_dir=destination,
            planned=planned,
        )
        assignment = self._fault_assignment(
            campaign,
            output_dir=destination,
            planned=planned,
        )

        semaphore = asyncio.Semaphore(parallelism)
        targets = self._target_scenarios(campaign, planned)
        collections = await asyncio.gather(
            *(
                self._collect_scenario(
                    campaign=campaign,
                    experiment=experiment,
                    spec=spec,
                    target=target,
                    output_dir=destination,
                    semaphore=semaphore,
                )
                for spec, target in targets
            )
        )
        baselines = [baseline for collection in collections for baseline in collection.baselines]
        errors = {
            spec.id: collection.errors
            for (spec, _), collection in zip(targets, collections, strict=True)
            if collection.errors
        }
        summary = self._export_cases(
            campaign=campaign,
            baselines=baselines,
            faults=faults,
            output_dir=destination,
            planned=planned,
            batch_count=sum(collection.batch_count for collection in collections),
            rejected=sum(collection.rejected for collection in collections),
            errors=errors,
            assignment=assignment,
        )
        logger.info(
            "dataset.campaign.completed",
            campaign=campaign.name,
            planned=planned,
            recorded=summary.recorded_executions,
            pending=summary.pending_executions,
            complete=summary.complete,
        )
        return summary
