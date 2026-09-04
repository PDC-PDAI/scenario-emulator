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
    DatasetLabel,
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


@dataclass(frozen=True)
class _InjectedFault:
    """Resultado interno de uma mutação e seu ponto causal esperado."""

    steps: list[AgentDebugTrajectoryStep]
    target_step: int
    metadata: dict[str, object]


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


def _baseline_batch_directory(
    campaign: DatasetCampaignProfile,
    output_dir: Path,
    scenario_id: str,
) -> Path:
    cache_root = campaign.baseline_source_dir or output_dir
    batch_dir = cache_root / "private" / "baseline-scenarios" / scenario_id
    if campaign.baseline_source_dir is None:
        batch_dir.mkdir(parents=True, exist_ok=True)
    elif not batch_dir.is_dir():
        raise ValueError(f"Cache de baselines não encontrado para o cenário {scenario_id}.")
    return batch_dir


def _read_cached_scenario(
    batch_path: Path,
    error_path: Path,
    errors: list[str],
) -> tuple[ScenarioRun | None, bool]:
    if batch_path.exists():
        scenario = ScenarioRun.model_validate_json(batch_path.read_text(encoding="utf-8"))
        return scenario, True
    if error_path.exists():
        payload = json.loads(error_path.read_text(encoding="utf-8"))
        errors.append(str(payload.get("error", "erro desconhecido")))
        return None, True
    return None, False


def _generation_id(position: int, planned: int) -> str:
    width = max(3, len(str(planned)))
    return f"generation-{position:0{width}d}"


def _expand_baselines(
    baselines: list[_Baseline],
    *,
    planned: int,
    augmentations_per_baseline: int,
    fault_ids: list[str],
    assignment: dict[str, str],
) -> list[_Baseline]:
    """Reutiliza baselines sem repetir um fault_id para o mesmo pai quando possível."""
    if not baselines:
        return []

    capacity = len(baselines) * augmentations_per_baseline
    output_size = min(planned, capacity)
    assigned_counts = Counter(assignment.values())
    supports_stratification = (
        augmentations_per_baseline > 1
        and output_size == capacity
        and len(baselines) % len(fault_ids) == 0
        and augmentations_per_baseline <= len(fault_ids)
        and len(set(assigned_counts.values())) == 1
    )
    if not supports_stratification:
        expanded = [
            baseline
            for _ in range(augmentations_per_baseline)
            for baseline in baselines
        ]
        return expanded[:output_size]

    slots: dict[str, list[_Baseline]] = {fault_id: [] for fault_id in fault_ids}
    for baseline_index, baseline in enumerate(baselines):
        for offset in range(augmentations_per_baseline):
            fault_id = fault_ids[(baseline_index + offset) % len(fault_ids)]
            slots[fault_id].append(baseline)

    if Counter({fault_id: len(items) for fault_id, items in slots.items()}) != Counter(
        assigned_counts
    ):
        expanded = [
            baseline
            for _ in range(augmentations_per_baseline)
            for baseline in baselines
        ]
        return expanded[:output_size]

    positions: Counter[str] = Counter()
    expanded: list[_Baseline] = []
    for position in range(1, output_size + 1):
        generation_id = _generation_id(position, planned)
        fault_id = assignment[generation_id]
        expanded.append(slots[fault_id][positions[fault_id]])
        positions[fault_id] += 1
    return expanded


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


def _replace_decision_step(
    trajectory: AgentDebugTrajectory,
    *,
    target_index: int,
    planning: str | None = None,
    action_output: str | None = None,
    raw_action: object | None = None,
    env_response: object | None = None,
) -> list[AgentDebugTrajectoryStep]:
    """Substitui uma decisão preservando a coerência entre módulos e raw_output."""
    steps: list[AgentDebugTrajectoryStep] = []
    for original in trajectory.steps:
        if original.index != target_index:
            steps.append(original.model_copy(deep=True))
            continue
        modules = dict(original.module_outputs)
        if planning is not None:
            modules[ErrorModule.PLANNING] = planning
        if action_output is not None:
            modules[ErrorModule.ACTION] = action_output
        rendered_plan = modules.get(ErrorModule.PLANNING, "")
        rendered_action = raw_action
        if rendered_action is None:
            rendered_action = _action_payload(original)
            if action_output is not None and rendered_action is None:
                rendered_action = action_output
        updates: dict[str, object] = {
            "module_outputs": modules,
            "raw_output": raw_output_envelope(rendered_plan, rendered_action),
        }
        if env_response is not None:
            updates["env_response"] = (
                env_response if isinstance(env_response, str) else _json_text(env_response)
            )
        steps.append(
            original.model_copy(
                deep=True,
                update=updates,
            )
        )
    return steps


def _variant(variants: tuple[str, ...], variant_index: int) -> tuple[int, str]:
    position = variant_index % len(variants)
    return position, variants[position]


def _planning_constraint_ignorance(
    trajectory: AgentDebugTrajectory,
    variant_index: int,
) -> _InjectedFault:
    target = _terminal_step(trajectory)
    action = _action_payload(target)
    if action is None:
        raise ValueError("A action terminal não contém um objeto JSON.")

    position, variant_id = _variant(
        ("discard_job_requirements", "discard_seniority", "generic_questionnaire"),
        variant_index,
    )
    ignored = (
        "os requisitos obrigatórios retornados pela consulta da vaga",
        "o nível de senioridade e a profundidade exigida",
        "as restrições específicas do comando do coordenador",
    )[position]
    planning = (
        f"Vou desconsiderar {ignored} e persistir um questionário genérico, "
        "mesmo que ele não cubra os critérios solicitados."
    )
    arguments = action.get("arguments")
    mutated_arguments = dict(arguments) if isinstance(arguments, dict) else {}
    payload = mutated_arguments.get("payload")
    mutated_payload = dict(payload) if isinstance(payload, dict) else {}
    questions = mutated_payload.get("questions")
    question_count = len(questions) if isinstance(questions, list) and questions else 1
    mutated_payload["questions"] = [
        {
            "text": f"Conte algo genérico sobre sua trajetória profissional ({index}).",
            "description": "Pergunta deliberadamente desvinculada dos critérios da vaga.",
            "type": "LONG_TEXT",
            "weight": 5,
            "required": True,
            "rationale": "Cobertura genérica, sem considerar os requisitos recuperados.",
        }
        for index in range(1, question_count + 1)
    ]
    mutated_arguments["payload"] = mutated_payload
    mutated_action = {"tool": action.get("tool"), "arguments": mutated_arguments}
    steps = _replace_decision_step(
        trajectory,
        target_index=target.index,
        planning=planning,
        action_output=_json_text(mutated_action),
        raw_action=mutated_action,
        env_response={
            "result": {"ok": True, "saved": True},
            "oracle": {
                "passed": False,
                "reason": "O formulário salvo não cobre os critérios explícitos da tarefa.",
            },
        },
    )
    return _InjectedFault(
        steps=steps,
        target_step=target.index,
        metadata={
            "operation": "replace_plan_and_payload_with_constraint-blind_version",
            "variant_id": variant_id,
            "ignored_constraint": ignored,
        },
    )


def _planning_impossible_action(
    trajectory: AgentDebugTrajectory,
    variant_index: int,
) -> _InjectedFault:
    first = trajectory.steps[0]
    terminal = _terminal_step(trajectory)
    terminal_action = _action_payload(terminal)
    if terminal_action is None:
        raise ValueError("A action terminal não contém um objeto JSON.")
    _, variant_id = _variant(
        ("save_before_lookup", "save_before_draft", "save_before_validation"),
        variant_index,
    )
    missing_precondition = {
        "save_before_lookup": "os dados da vaga ainda não foram consultados",
        "save_before_draft": "o rascunho do questionário ainda não foi produzido",
        "save_before_validation": "o formulário ainda não foi validado",
    }[variant_id]
    planning = (
        "Vou persistir o formulário imediatamente, embora "
        f"{missing_precondition}."
    )
    action_output = _json_text(terminal_action)
    injected_first = first.model_copy(
        deep=True,
        update={
            "module_outputs": {
                ErrorModule.PLANNING: planning,
                ErrorModule.ACTION: action_output,
            },
            "env_response": _json_text(
                {
                    "ok": False,
                    "tool": terminal_action.get("tool"),
                    "message": f"Pré-condição ausente: {missing_precondition}.",
                }
            ),
            "raw_output": raw_output_envelope(planning, terminal_action),
        },
    )
    return _InjectedFault(
        steps=[injected_first],
        target_step=1,
        metadata={
            "operation": "move_terminal_decision_before_required_precondition",
            "variant_id": variant_id,
            "missing_precondition": missing_precondition,
        },
    )


def _planning_inefficient_plan(
    trajectory: AgentDebugTrajectory,
    variant_index: int,
) -> _InjectedFault:
    first = trajectory.steps[0].model_copy(deep=True)
    lookup_action = _action_payload(first)
    if lookup_action is None:
        raise ValueError("O primeiro step não contém uma action de consulta válida.")
    position, variant_id = _variant(
        ("repeat_lookup_twice", "repeat_lookup_three_times", "repeat_lookup_five_times"),
        variant_index,
    )
    repetitions = (2, 3, 5)[position]
    steps = [first]
    for offset in range(repetitions):
        index = offset + 2
        planning = (
            "Vou consultar novamente os mesmos dados da vaga, embora a resposta completa "
            "já esteja disponível no histórico, adiando a persistência do formulário."
        )
        env_response: object = first.env_response
        if offset == repetitions - 1:
            env_response = {
                "tool": lookup_action.get("tool"),
                "result": "Os mesmos dados foram retornados novamente.",
                "task_complete": False,
            }
        steps.append(
            AgentDebugTrajectoryStep(
                index=index,
                module_outputs={
                    ErrorModule.PLANNING: planning,
                    ErrorModule.ACTION: _json_text(lookup_action),
                },
                step_input=(
                    "Os dados completos da vaga já foram recuperados; falta construir e "
                    "persistir o formulário."
                ),
                env_response=(
                    env_response if isinstance(env_response, str) else _json_text(env_response)
                ),
                raw_output=raw_output_envelope(planning, lookup_action),
            )
        )
    return _InjectedFault(
        steps=steps,
        target_step=2,
        metadata={
            "operation": "repeat_successful_lookup_without_new_information",
            "variant_id": variant_id,
            "repetitions": repetitions,
        },
    )


def _action_misalignment(
    trajectory: AgentDebugTrajectory,
    variant_index: int,
) -> _InjectedFault:
    target = _terminal_step(trajectory)
    original = _action_payload(target)
    if original is None:
        raise ValueError("A action terminal não contém um objeto JSON.")
    arguments = original.get("arguments")
    arguments = dict(arguments) if isinstance(arguments, dict) else {}
    position, variant_id = _variant(
        ("register_failure", "repeat_lookup", "fetch_unknown_job"), variant_index
    )
    questionnaire_id = arguments.get("questionnaireId", "questionnaire-unknown")
    actions = (
        {
            "tool": "registrar_falha_formulario",
            "arguments": {
                "questionnaireId": questionnaire_id,
                "errorReason": "LLM_FAILURE",
            },
        },
        {"tool": "get_info_vaga", "arguments": {"code": questionnaire_id}},
        {"tool": "get_info_vaga", "arguments": {"code": "job-unrelated"}},
    )
    action = actions[position]
    steps = _replace_decision_step(
        trajectory,
        target_index=target.index,
        action_output=_json_text(action),
        raw_action=action,
        env_response={"result": {"ok": True}, "tool": action["tool"]},
    )
    return _InjectedFault(
        steps=steps,
        target_step=target.index,
        metadata={
            "operation": "replace_action_with_decision_conflicting_with_plan",
            "variant_id": variant_id,
            "planned_tool": original.get("tool"),
            "injected_tool": action["tool"],
        },
    )


def _action_invalid_action(
    trajectory: AgentDebugTrajectory,
    variant_index: int,
) -> _InjectedFault:
    target = _terminal_step(trajectory)
    original = _action_payload(target)
    if original is None:
        raise ValueError("A action terminal não contém um objeto JSON.")
    arguments = original.get("arguments")
    arguments = dict(arguments) if isinstance(arguments, dict) else {}
    position, variant_id = _variant(
        ("versioned_tool", "unknown_namespace", "invented_archive_tool"), variant_index
    )
    tool = (
        "salvar_formulario_v2",
        "questionarios.salvar_formulario",
        "arquivar_formulario_definitivo",
    )[position]
    action = {"tool": tool, "arguments": arguments}
    steps = _replace_decision_step(
        trajectory,
        target_index=target.index,
        action_output=_json_text(action),
        raw_action=action,
        env_response={"error": "TOOL_NOT_FOUND", "tool": tool},
    )
    return _InjectedFault(
        steps=steps,
        target_step=target.index,
        metadata={
            "operation": "replace_tool_name",
            "variant_id": variant_id,
            "expected_tool": original.get("tool"),
            "injected_tool": tool,
        },
    )


def _action_format_error(
    trajectory: AgentDebugTrajectory,
    variant_index: int,
) -> _InjectedFault:
    target = _terminal_step(trajectory)
    original = _action_payload(target)
    if original is None:
        raise ValueError("A action terminal não contém um objeto JSON.")
    position, variant_id = _variant(
        ("truncated_json", "plain_text_call", "array_instead_of_object"), variant_index
    )
    action_outputs = (
        '{"tool":"salvar_formulario","arguments":',
        "salvar_formulario(questionnaireId=..., payload=...)",
        '["salvar_formulario", {"questionnaireId":"questionnaire"}]',
    )
    messages = (
        "A saída da ação terminou antes do fechamento do objeto JSON.",
        "A chamada foi emitida como texto livre e não pôde ser interpretada.",
        "Era esperado um objeto de ação, mas foi recebida uma lista.",
    )
    action_output = action_outputs[position]
    steps = _replace_decision_step(
        trajectory,
        target_index=target.index,
        action_output=action_output,
        raw_action=action_output,
        env_response={"ok": False, "message": messages[position], "position": len(action_output)},
    )
    return _InjectedFault(
        steps=steps,
        target_step=target.index,
        metadata={
            "operation": "replace_action_with_unparseable_representation",
            "variant_id": variant_id,
            "expected_tool": original.get("tool"),
        },
    )


def _action_parameter_error(
    trajectory: AgentDebugTrajectory,
    variant_index: int,
) -> _InjectedFault:
    target = _terminal_step(trajectory)
    original = _action_payload(target)
    if original is None:
        raise ValueError("A action terminal não contém um objeto JSON.")
    arguments = original.get("arguments")
    arguments = dict(arguments) if isinstance(arguments, dict) else {}
    position, variant_id = _variant(
        ("missing_payload", "wrong_questionnaire_id", "payload_wrong_type"), variant_index
    )
    invalid_arguments = dict(arguments)
    if position == 0:
        invalid_arguments.pop("payload", None)
        message = "O argumento obrigatório 'payload' não foi informado."
    elif position == 1:
        invalid_arguments["questionnaireId"] = "questionnaire-unrelated"
        message = "O identificador informado não corresponde ao formulário da execução."
    else:
        invalid_arguments["payload"] = "questions=texto-livre"
        message = "O argumento 'payload' possui tipo incompatível com o contrato."
    action = {"tool": original.get("tool"), "arguments": invalid_arguments}
    steps = _replace_decision_step(
        trajectory,
        target_index=target.index,
        action_output=_json_text(action),
        raw_action=action,
        env_response={"ok": False, "tool": action["tool"], "message": message},
    )
    return _InjectedFault(
        steps=steps,
        target_step=target.index,
        metadata={
            "operation": "replace_action_arguments_with_invalid_variant",
            "variant_id": variant_id,
            "tool": action["tool"],
        },
    )


def _system_step_limit(
    trajectory: AgentDebugTrajectory,
    variant_index: int,
) -> _InjectedFault:
    first = trajectory.steps[0].model_copy(deep=True)
    position, variant_id = _variant(
        ("max_steps_one", "budget_exhausted", "orchestrator_iteration_cap"), variant_index
    )
    messages = (
        "A execução foi encerrada ao alcançar o máximo configurado de 1 step.",
        "O orçamento de passos acabou antes da ação terminal.",
        "O orquestrador encerrou o loop no teto de iterações antes da persistência.",
    )
    first = first.model_copy(
        update={
            "env_response": _json_text(
                {
                    "previous_response": first.env_response,
                    "runtime": {
                        "stop_reason": "maximum_steps_reached",
                        "max_steps": 1,
                        "message": messages[position],
                    },
                }
            )
        }
    )
    return _InjectedFault(
        steps=[first],
        target_step=1,
        metadata={
            "operation": "stop_execution_at_configured_step_cap",
            "variant_id": variant_id,
            "configured_max_steps": 1,
        },
    )


def _system_tool_execution_error(
    trajectory: AgentDebugTrajectory,
    variant_index: int,
) -> _InjectedFault:
    target = _terminal_step(trajectory)
    action = _action_payload(target)
    if action is None:
        raise ValueError("A action terminal não contém um objeto JSON.")
    position, variant_id = _variant(
        ("external_503", "connection_reset", "empty_external_response"), variant_index
    )
    failures = (
        {"status": 503, "message": "O serviço externo de formulários está indisponível."},
        {"code": "CONNECTION_RESET", "message": "A conexão caiu durante a chamada válida."},
        {"code": "EMPTY_RESPONSE", "message": "A ferramenta não devolveu resultado."},
    )
    steps = _replace_decision_step(
        trajectory,
        target_index=target.index,
        env_response={
            "tool": action.get("tool"),
            "arguments_accepted": True,
            "external_failure": failures[position],
        },
    )
    return _InjectedFault(
        steps=steps,
        target_step=target.index,
        metadata={
            "operation": "replace_valid_tool_result_with_external_failure",
            "variant_id": variant_id,
            "tool": action.get("tool"),
        },
    )


def _system_llm_limit(
    trajectory: AgentDebugTrajectory,
    variant_index: int,
) -> _InjectedFault:
    target = _terminal_step(trajectory)
    position, variant_id = _variant(
        ("provider_timeout", "token_budget_exhausted", "context_window_exceeded"),
        variant_index,
    )
    messages = (
        "O provider excedeu o tempo máximo antes de produzir a próxima decisão.",
        "A resposta do modelo foi interrompida ao esgotar o orçamento de tokens.",
        "A chamada foi recusada porque o contexto excedeu a janela do modelo.",
    )
    partial_outputs = (
        "",
        '{"reasoning":"Vou preparar o formulário", "questions":[',
        "",
    )
    system_output = messages[position]
    failed_step = target.model_copy(
        deep=True,
        update={
            "module_outputs": {ErrorModule.SYSTEM: system_output},
            "env_response": _json_text(
                {
                    "provider": "model-runtime",
                    "status": "request_failed",
                    "message": system_output,
                }
            ),
            "raw_output": partial_outputs[position],
        },
    )
    steps = [
        step.model_copy(deep=True)
        for step in trajectory.steps
        if step.index < target.index
    ] + [failed_step]
    return _InjectedFault(
        steps=steps,
        target_step=target.index,
        metadata={
            "operation": "replace_terminal_decision_with_provider_limit",
            "variant_id": variant_id,
        },
    )


def _system_environment_error(
    trajectory: AgentDebugTrajectory,
    variant_index: int,
) -> _InjectedFault:
    target = _terminal_step(trajectory)
    action = _action_payload(target)
    if action is None:
        raise ValueError("A action terminal não contém um objeto JSON.")
    position, variant_id = _variant(
        ("state_commit_lost", "simulator_invariant_broken", "storage_partition"),
        variant_index,
    )
    failures = (
        "O simulador confirmou a operação, mas perdeu a alteração de estado.",
        "O estado observado após a ação viola a transição definida pelo ambiente.",
        "O armazenamento do ambiente ficou inacessível antes de confirmar o commit.",
    )
    steps = _replace_decision_step(
        trajectory,
        target_index=target.index,
        env_response={
            "tool": action.get("tool"),
            "arguments_accepted": True,
            "simulator": {
                "status": "state_transition_failed",
                "message": failures[position],
            },
        },
    )
    return _InjectedFault(
        steps=steps,
        target_step=target.index,
        metadata={
            "operation": "replace_environment_transition_with_simulator_failure",
            "variant_id": variant_id,
            "tool": action.get("tool"),
        },
    )


_FAULT_INJECTORS = {
    (ErrorModule.PLANNING, ErrorType.CONSTRAINT_IGNORANCE): _planning_constraint_ignorance,
    (ErrorModule.PLANNING, ErrorType.IMPOSSIBLE_ACTION): _planning_impossible_action,
    (ErrorModule.PLANNING, ErrorType.INEFFICIENT_PLAN): _planning_inefficient_plan,
    (ErrorModule.ACTION, ErrorType.MISALIGNMENT): _action_misalignment,
    (ErrorModule.ACTION, ErrorType.INVALID_ACTION): _action_invalid_action,
    (ErrorModule.ACTION, ErrorType.FORMAT_ERROR): _action_format_error,
    (ErrorModule.ACTION, ErrorType.PARAMETER_ERROR): _action_parameter_error,
    (ErrorModule.SYSTEM, ErrorType.STEP_LIMIT): _system_step_limit,
    (ErrorModule.SYSTEM, ErrorType.TOOL_EXECUTION_ERROR): _system_tool_execution_error,
    (ErrorModule.SYSTEM, ErrorType.LLM_LIMIT): _system_llm_limit,
    (ErrorModule.SYSTEM, ErrorType.ENVIRONMENT_ERROR): _system_environment_error,
}


def inject_fault(
    trajectory: AgentDebugTrajectory,
    fault: FaultMode,
    *,
    trajectory_id: str,
    variant_index: int = 0,
) -> tuple[FrontBTrajectory, dict[str, object]]:
    """Aplica uma mutação controlada em um checkpoint real da Frente A."""
    if not trajectory.success:
        raise ValueError("A injeção exige uma trajetória baseline bem-sucedida.")
    pair = (fault.target_module, fault.error_type)
    injector = _FAULT_INJECTORS.get(pair)
    if injector is None:
        raise ValueError(
            "Par de falha não suportado pelo injetor: "
            f"{fault.target_module.value}/{fault.error_type.value}."
        )
    result = injector(trajectory, variant_index)
    injection = {
        **result.metadata,
        "target_step": result.target_step,
        "target_module": fault.target_module.value,
        "error_type": fault.error_type.value,
    }
    injected = AgentDebugTrajectory(
        trajectory_id=trajectory_id,
        task_description=trajectory.task_description,
        environment=trajectory.environment,
        success=False,
        steps=result.steps,
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
            if (fault.target_module, fault.error_type) not in _FAULT_INJECTORS:
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
        batch_dir = _baseline_batch_directory(campaign, output_dir, spec.id)
        source_only = campaign.baseline_source_dir is not None
        seen_trajectories: set[str] = set()

        for attempt in range(1, campaign.max_attempts_per_scenario + 1):
            batch_path = batch_dir / f"batch-{attempt:02d}.json"
            error_path = batch_dir / f"batch-{attempt:02d}.error.json"
            scenario, cached = _read_cached_scenario(
                batch_path,
                error_path,
                collection.errors,
            )

            if cached and scenario is None:
                continue
            if not cached and source_only:
                continue
            if not cached:
                missing = target - len(collection.baselines)
                if missing <= 0:
                    break
                requested = min(missing, campaign.baseline_batch_size)
                logger.info(
                    "dataset.scenario.started",
                    campaign=campaign.name,
                    scenario=spec.id,
                    attempt=attempt,
                    requested=requested,
                )
                try:
                    async with semaphore:
                        scenario = await self._scenario_service_factory().run(
                            spec.brief,
                            benign_count=requested,
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

        if campaign.baseline_source_dir is not None and len(collection.baselines) < target:
            collection.errors.append(
                f"Cache contém {len(collection.baselines)} de {target} baselines necessárias."
            )

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
            "baseline_source_dir": (
                str(campaign.baseline_source_dir) if campaign.baseline_source_dir else None
            ),
            "augmentations_per_baseline": campaign.augmentations_per_baseline,
            "planned_baselines": min(
                campaign.planned_baselines,
                (planned + campaign.augmentations_per_baseline - 1)
                // campaign.augmentations_per_baseline,
            ),
            "planned_executions": planned,
            "planned_fault_distribution": campaign.planned_fault_distribution(limit=planned),
            "baseline_batch_size": campaign.baseline_batch_size,
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
        fault_occurrences: Counter[str] = Counter()
        parent_occurrences: Counter[str] = Counter()

        for position, baseline in enumerate(baselines[:planned], start=1):
            generation_id = _generation_id(position, planned)
            fault_id = assignment[generation_id]
            fault = faults[fault_id]
            variant_index = fault_occurrences[fault_id]
            fault_occurrences[fault_id] += 1
            injected, injection = inject_fault(
                baseline.trajectory,
                fault,
                trajectory_id=generation_id,
                variant_index=variant_index,
            )
            parent_trajectory_id = baseline.trajectory.trajectory_id
            parent_occurrences[parent_trajectory_id] += 1
            injection["baseline_reuse_index"] = parent_occurrences[parent_trajectory_id]
            injection["baseline_reuse_total"] = campaign.augmentations_per_baseline
            target_step = int(injection["target_step"])
            truth = DatasetGroundTruth(
                case_id=generation_id,
                trajectory_id=injected.trajectory_id,
                parent_trajectory_id=parent_trajectory_id,
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
        labels = {
            item.trajectory_id: DatasetLabel.from_ground_truth(item).model_dump(mode="json")
            for item in ground_truth
        }
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
            unique_baselines=len({item.parent_trajectory_id for item in ground_truth}),
            augmentations_per_baseline=campaign.augmentations_per_baseline,
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
        required_baselines = min(
            campaign.planned_baselines,
            (planned + campaign.augmentations_per_baseline - 1)
            // campaign.augmentations_per_baseline,
        )
        targets = self._target_scenarios(campaign, required_baselines)
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
        expanded_baselines = _expand_baselines(
            baselines,
            planned=planned,
            augmentations_per_baseline=campaign.augmentations_per_baseline,
            fault_ids=campaign.fault_ids,
            assignment=assignment,
        )
        errors = {
            spec.id: collection.errors
            for (spec, _), collection in zip(targets, collections, strict=True)
            if collection.errors
        }
        summary = self._export_cases(
            campaign=campaign,
            baselines=expanded_baselines,
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
