from __future__ import annotations

import json
import time
import uuid
from typing import Any

import structlog
from agno.agent import Agent
from agno.run.agent import RunOutput
from agno.tools.function import Function
from pydantic import ValidationError

from src.agents.model import build_model, get_model_identifier
from src.agents.utils import extract_usage, parse_model_output
from src.prompts.manager import resolve_prompt
from src.prompts.raw_prompts import QUESTIONNAIRE_SYSTEM_PROMPT, QUESTIONNAIRE_USER_PROMPT
from src.schemas.coordinator_prompt.schema import CoordinatorPrompt, ExpectedAction
from src.schemas.job_description.schema import JobDescription
from src.schemas.observability.schema import NodeLocus, TraceNode
from src.schemas.questionnaire.schema import (
    ExecutionStatus,
    LLMQuestionnaireResult,
    Questionnaire,
    QuestionnaireExecution,
    QuestionnairePayload,
)
from src.services.observability.react import ReactSpanStreamer
from src.services.observability.service import (
    current_trace_id,
    emit_reasoning_summary,
    observation,
)
from src.services.questionnaire.utils import (
    clean_questionnaire_payload,
    compose_coordinator_command,
    validate_question_count,
)

logger = structlog.get_logger(__name__)


class QuestionnaireService:
    async def execute(  # noqa: PLR0915 - o fluxo linear espelha o ciclo ReAct auditável
        self,
        job: JobDescription,
        coordinator_prompt: CoordinatorPrompt,
        *,
        scenario_id: str,
    ) -> QuestionnaireExecution:
        started = time.perf_counter()
        questionnaire_id = f"questionnaire-{uuid.uuid4()}"
        trajectory_id = f"trajectory-{uuid.uuid4()}"
        command, guidelines = compose_coordinator_command(coordinator_prompt.command)

        system = resolve_prompt("front-a/questionnaire/system", QUESTIONNAIRE_SYSTEM_PROMPT)
        user = resolve_prompt(
            "front-a/questionnaire/user",
            QUESTIONNAIRE_USER_PROMPT,
            variables={
                "job_opening_id": job.id,
                "questionnaire_id": questionnaire_id,
                "coordinator_command": command,
                "platform_guidelines": guidelines,
            },
        )
        model = build_model()
        node_id = f"{scenario_id}.questionnaire.{coordinator_prompt.sequence:03d}"
        agent_node = TraceNode(
            node_id=node_id,
            locus=NodeLocus.SUB_GER,
            depends_on=[f"{scenario_id}.coordinator-prompts"],
            name="questionnaire-agent",
        )
        state: dict[str, Any] = {"tool_sequence": []}

        def get_info_vaga(code: str) -> dict[str, Any]:
            """Busca os dados da vaga atual pelo código."""
            state["tool_sequence"].append("get_info_vaga")
            if code != job.id:
                return {"error": "VAGA_NOT_FOUND", "code": code}
            return job.model_dump(mode="json", exclude={"source_brief"})

        def salvar_formulario(
            questionnaireId: str, payload: QuestionnairePayload
        ) -> dict[str, Any]:
            """Valida e salva o questionário no armazenamento efêmero do cenário."""
            state["tool_sequence"].append("salvar_formulario")
            if questionnaireId != questionnaire_id:
                return {"error": "SAVE_FAILED", "message": "questionnaireId inválido"}
            try:
                if hasattr(payload, "model_dump"):
                    raw = payload.model_dump(mode="json")
                elif isinstance(payload, dict):
                    raw = payload
                else:
                    raw = json.loads(str(payload))
                validated = parse_model_output(raw, QuestionnairePayload)
                cleaned = clean_questionnaire_payload(
                    validated.model_dump(mode="json", exclude_none=True)
                )
                validate_question_count(cleaned, coordinator_prompt.requested_question_count)
                questionnaire = Questionnaire(
                    questionnaire_id=questionnaire_id,
                    job_description_id=job.id,
                    questions=cleaned["questions"],
                )
            except (ValidationError, ValueError, TypeError, json.JSONDecodeError) as exc:
                state["save_error"] = str(exc)
                return {"error": "SAVE_FAILED", "message": str(exc)[:500]}
            state["questionnaire"] = questionnaire
            return {
                "ok": True,
                "questionnaireId": questionnaire_id,
                "totalQuestions": len(questionnaire.questions),
            }

        def registrar_falha_formulario(questionnaireId: str, errorReason: str) -> dict[str, Any]:
            """Registra a falha observada pelo agente para este cenário."""
            state["tool_sequence"].append("registrar_falha_formulario")
            if questionnaireId != questionnaire_id:
                return {"error": "INVALID_QUESTIONNAIRE_ID"}
            state["failure_reason"] = errorReason
            return {"ok": True, "questionnaireId": questionnaire_id, "errorReason": errorReason}

        with observation(
            agent_node,
            as_type="agent",
            input={
                "job_description_id": job.id,
                "coordinator_prompt_id": coordinator_prompt.id,
                "intent": coordinator_prompt.intent.value,
                "category": coordinator_prompt.category.value,
                "command": command,
            },
            metadata={
                "trajectory_id": trajectory_id,
                "expected_action": coordinator_prompt.expected_action.value,
            },
        ) as agent_span:
            agent = Agent(
                name="generator_agent",
                model=model,
                description=system.content,
                # Como no modo native-toolcall do rh-agent-agno, o payload canônico
                # vem dos argumentos de salvar_formulario. Sem output_schema aqui, o
                # Agno não força strict=True em todas as tools (incompatível com o
                # campo opcional `description` do contrato real).
                output_schema=None,
                # OpenAI ativa strict automaticamente quando há output_schema. O contrato
                # possui `description` legitimamente opcional; como no rh-agent-agno,
                # desabilitamos strict nas tools sem relaxar a validação Pydantic local.
                tools=[
                    Function.from_callable(get_info_vaga, strict=False),
                    Function.from_callable(salvar_formulario, strict=False),
                    Function.from_callable(registrar_falha_formulario, strict=False),
                ],
                tool_call_limit=6,
            )
            streamer = ReactSpanStreamer(node_prefix=node_id, depends_on=[node_id])
            final_output: Any = None
            run_error: Exception | None = None
            try:
                async for event in agent.arun(
                    user.content,
                    stream=True,
                    stream_events=True,
                    yield_run_output=True,
                ):
                    if isinstance(event, RunOutput):
                        final_output = event
                    else:
                        await streamer.handle(event)
            except Exception as exc:  # noqa: BLE001 - falha vira dado experimental
                run_error = exc
                logger.exception(
                    "questionnaire.execution_failed",
                    trajectory_id=trajectory_id,
                    coordinator_prompt_id=coordinator_prompt.id,
                )

            streamer.flush_final_reasoning()
            result_content = getattr(final_output, "content", None)
            reasoning = self._extract_reasoning(result_content)
            terminal_dependencies = streamer.terminal_dependencies
            if not streamer.emitted_model_reasoning:
                emit_reasoning_summary(
                    node_prefix=node_id,
                    text=reasoning,
                    depends_on=terminal_dependencies,
                )
                if reasoning:
                    terminal_dependencies = [f"{node_id}.reasoning.final"]

            generation_node = TraceNode(
                node_id=f"{node_id}.generation",
                locus=NodeLocus.SUB_GER,
                depends_on=terminal_dependencies,
                name="questionnaire-generator",
            )
            with observation(
                generation_node,
                as_type="generation",
                input={"system": system.content, "user": user.content},
                output=result_content,
                model=get_model_identifier(model),
                prompt=system.langfuse_prompt,
                metadata={
                    "system_prompt_source": system.source,
                    "system_prompt_version": system.version,
                    "user_prompt_source": user.source,
                    "user_prompt_version": user.version,
                },
            ) as generation:
                usage = extract_usage(final_output) if final_output is not None else None
                if usage:
                    generation.update(usage_details=usage)

            status, failure_reason = self._status_from_state(state, run_error)
            expected = coordinator_prompt.expected_action
            benchmark_passed = (
                expected is ExpectedAction.COMPLY and status is ExecutionStatus.SUCCEEDED
            ) or (expected is ExpectedAction.REFUSE and status is ExecutionStatus.REFUSED)
            duration_ms = max(0, round((time.perf_counter() - started) * 1000))
            result = QuestionnaireExecution(
                trajectory_id=trajectory_id,
                coordinator_prompt=coordinator_prompt,
                status=status,
                benchmark_passed=benchmark_passed,
                failure_reason=failure_reason,
                questionnaire=state.get("questionnaire"),
                reasoning_summary=reasoning,
                trace_id=current_trace_id(),
                duration_ms=duration_ms,
            )
            agent_span.update(
                output={
                    "trajectory_id": trajectory_id,
                    "status": status.value,
                    "benchmark_passed": benchmark_passed,
                    "failure_reason": failure_reason,
                    "question_count": len(result.questionnaire.questions)
                    if result.questionnaire
                    else 0,
                }
            )
            return result

    @staticmethod
    def _extract_reasoning(content: Any) -> str | None:
        if content is None:
            return None
        direct = getattr(content, "reasoning", None)
        if direct:
            return str(direct)
        if isinstance(content, dict):
            value = content.get("reasoning")
            return str(value) if value else None
        if isinstance(content, str):
            try:
                return parse_model_output(content, LLMQuestionnaireResult).reasoning
            except Exception:  # noqa: BLE001 - conteúdo de modelo não confiável
                return None
        return None

    @staticmethod
    def _status_from_state(
        state: dict[str, Any], run_error: Exception | None
    ) -> tuple[ExecutionStatus, str | None]:
        failure_reason = state.get("failure_reason")
        if failure_reason:
            status = (
                ExecutionStatus.REFUSED
                if failure_reason == "LLM_SECURITY_ERROR"
                else ExecutionStatus.FAILED
            )
            return status, str(failure_reason)
        if state.get("questionnaire") is not None:
            return ExecutionStatus.SUCCEEDED, None
        if run_error is not None:
            return ExecutionStatus.FAILED, f"AGENT_RUNTIME_ERROR: {run_error}"
        if state.get("save_error"):
            return ExecutionStatus.FAILED, f"SAVE_FAILED: {state['save_error']}"
        return ExecutionStatus.FAILED, "MISSING_TERMINAL_TOOL_CALL"
