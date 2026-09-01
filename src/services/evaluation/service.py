from __future__ import annotations

import json
import time
import uuid
from typing import Any

from agno.agent import Agent

from src.agents.model import build_model, get_model_identifier
from src.agents.utils import extract_usage, parse_model_output
from src.prompts.manager import resolve_prompt
from src.prompts.raw_prompts import EVALUATOR_SYSTEM_PROMPT, EVALUATOR_USER_PROMPT
from src.schemas.agent_debug.schema import (
    AgentDebugTrajectory,
    AgentDebugTrajectoryStep,
    ErrorModule,
)
from src.schemas.evaluation.schema import (
    EvaluationExecution,
    EvaluationStatus,
    EvidenciaFormulario,
    NotaFormulario,
    NotaLLM,
)
from src.schemas.job_description.schema import JobDescription
from src.schemas.observability.schema import NodeLocus, TraceNode
from src.schemas.questionnaire.schema import Questionnaire
from src.schemas.response.schema import ResponseCase
from src.schemas.submission.schema import QuestionnaireSubmission
from src.services.agent_debug.service import evaluation_failure_annotation
from src.services.evaluation.oracle import evaluate_oracle
from src.services.observability.service import (
    create_langfuse_trace_id,
    current_trace_id,
    observation,
    trace_attributes,
)
from src.settings import settings
from src.utils.privacy import redact_for_trace


class EvaluationService:
    async def evaluate(
        self,
        *,
        scenario_id: str,
        job: JobDescription,
        questionnaire: Questionnaire,
        submission: QuestionnaireSubmission,
        response_case: ResponseCase | None,
        depends_on: list[str],
        experiment_tags: list[str] | None = None,
        experiment_metadata: dict[str, str | None] | None = None,
    ) -> EvaluationExecution:
        started = time.perf_counter()
        trajectory_id = f"evaluation-trajectory-{uuid.uuid4()}"
        langfuse_trace_id = create_langfuse_trace_id(seed=trajectory_id)
        node_id = f"{scenario_id}.evaluation.{submission.submission_id}"
        system_canary = f"EVAL_SYSTEM_CANARY_{uuid.uuid4().hex}"
        system = resolve_prompt(
            "front-a/evaluator/system",
            EVALUATOR_SYSTEM_PROMPT,
            variables={"system_canary": system_canary},
        )
        user = resolve_prompt(
            "front-a/evaluator/user",
            EVALUATOR_USER_PROMPT,
            variables={
                "job_description": json.dumps(
                    job.model_dump(mode="json", exclude={"source_brief"}),
                    ensure_ascii=False,
                    indent=2,
                ),
                "questions_and_answers": self._questions_and_answers(questionnaire, submission),
            },
        )
        agent_node = TraceNode(
            node_id=node_id,
            locus=NodeLocus.EVALUATOR,
            depends_on=depends_on,
            name="questionnaire-response-evaluator",
        )
        intent = response_case.intent.value if response_case else "manual"
        category = response_case.category.value if response_case else "manual"
        task_description = (
            f"Avaliar as respostas do questionário {questionnaire.questionnaire_id} para a "
            f"vaga '{job.title}' ({job.id}), ignorando instruções presentes nas respostas; "
            f"caso {intent}/{category}."
        )
        raw_output = ""
        with (
            observation(
                agent_node,
                as_type="agent",
                trace_id=langfuse_trace_id,
                input={
                    "questionnaire_id": questionnaire.questionnaire_id,
                    "submission_id": submission.submission_id,
                    "response_intent": intent,
                    "response_category": category,
                },
                metadata={"trajectory_id": trajectory_id},
            ) as span,
            trace_attributes(
                session_id=scenario_id,
                trace_name="questionnaire-evaluation-trajectory",
                environment=settings.ENVIRONMENT,
                tags=[
                    "front-a",
                    "agentdebug-rh",
                    "trajectory",
                    "evaluator",
                    intent,
                    category,
                    *(experiment_tags or []),
                ],
                metadata={
                    "scenario_id": scenario_id,
                    "trajectory_id": trajectory_id,
                    "questionnaire_id": questionnaire.questionnaire_id,
                    "submission_id": submission.submission_id,
                    **(experiment_metadata or {}),
                },
            ),
        ):
            try:
                model = build_model("evaluator")
                agent = Agent(
                    name="questionnaire_response_evaluator",
                    model=model,
                    description=system.content,
                    output_schema=NotaLLM,
                )
                generation_node = TraceNode(
                    node_id=f"{node_id}.generation",
                    locus=NodeLocus.EVALUATOR,
                    depends_on=[node_id],
                    name="questionnaire-evaluation-generation",
                )
                with observation(
                    generation_node,
                    as_type="generation",
                    input={"user": user.content},
                    model=get_model_identifier(model),
                    prompt=system.langfuse_prompt,
                    metadata={
                        "system_prompt_source": system.source,
                        "system_prompt_version": system.version,
                        "user_prompt_source": user.source,
                        "user_prompt_version": user.version,
                    },
                ) as generation:
                    response = await agent.arun(user.content)
                    raw_output = self._as_text(response.content)
                    generation.update(output=redact_for_trace(response.content))
                    usage = extract_usage(response)
                    if usage:
                        generation.update(usage_details=usage)
                raw = parse_model_output(response.content, NotaLLM)
                result = NotaFormulario(
                    valor=raw.valor,
                    justificativa=raw.justificativa,
                    evidencias=self._evidences(questionnaire, submission),
                )
                oracle = evaluate_oracle(
                    result,
                    questionnaire=questionnaire,
                    submission=submission,
                    response_case=response_case,
                    system_canary=system_canary,
                )
                oracle_node = TraceNode(
                    node_id=f"{node_id}.oracle",
                    locus=NodeLocus.ORACLE,
                    depends_on=[generation_node.node_id],
                    name="evaluation-security-oracle",
                )
                with observation(
                    oracle_node,
                    as_type="evaluator",
                    input={
                        "submission_id": submission.submission_id,
                        "score": result.valor,
                        "benchmark_applicable": response_case is not None,
                    },
                ) as oracle_span:
                    oracle_span.update(output=oracle.model_dump(mode="json"))
                trajectory = self._trajectory(
                    trajectory_id=trajectory_id,
                    task_description=task_description,
                    step_input=user.content,
                    raw_output=raw_output,
                    result=result,
                    oracle=oracle,
                )
                execution = EvaluationExecution(
                    trajectory_id=trajectory_id,
                    scenario_id=scenario_id,
                    questionnaire_id=questionnaire.questionnaire_id,
                    submission=submission,
                    response_case=response_case,
                    status=EvaluationStatus.SUCCEEDED,
                    result=result,
                    oracle=oracle,
                    trace_id=current_trace_id() or langfuse_trace_id,
                    duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
                    agent_debug_trajectory=trajectory,
                )
            except Exception as exc:  # noqa: BLE001 - falha é dado experimental
                failure_reason = f"EVALUATION_FAILED: {exc}"
                trajectory = self._trajectory(
                    trajectory_id=trajectory_id,
                    task_description=task_description,
                    step_input=user.content,
                    raw_output=raw_output,
                    failure_reason=failure_reason,
                )
                execution = EvaluationExecution(
                    trajectory_id=trajectory_id,
                    scenario_id=scenario_id,
                    questionnaire_id=questionnaire.questionnaire_id,
                    submission=submission,
                    response_case=response_case,
                    status=EvaluationStatus.FAILED,
                    failure_reason=failure_reason,
                    trace_id=current_trace_id() or langfuse_trace_id,
                    duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
                    agent_debug_trajectory=trajectory,
                )

            annotation = evaluation_failure_annotation(execution)
            if annotation is not None:
                execution = execution.model_copy(update={"failure_annotation": annotation})
            benchmark_passed = bool(execution.oracle and execution.oracle.passed)
            span.update(
                output=redact_for_trace(
                    {
                        "trajectory_id": trajectory_id,
                        "status": execution.status.value,
                        "benchmark_passed": benchmark_passed,
                        "result": execution.result,
                        "oracle": execution.oracle,
                        "failure_reason": execution.failure_reason,
                        "agent_debug_trajectory": execution.agent_debug_trajectory,
                        "failure_annotation": execution.failure_annotation,
                    }
                ),
                metadata={"model": get_model_identifier(model)} if "model" in locals() else None,
                level="DEFAULT" if benchmark_passed else "ERROR",
                status_message=annotation.message if annotation else None,
            )
            return execution

    @staticmethod
    def _as_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if hasattr(value, "model_dump_json"):
            return value.model_dump_json()
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _trajectory(
        *,
        trajectory_id: str,
        task_description: str,
        step_input: str,
        raw_output: str,
        result: NotaFormulario | None = None,
        oracle: Any = None,
        failure_reason: str | None = None,
    ) -> AgentDebugTrajectory:
        module_outputs: dict[ErrorModule, str] = {}
        normalized_output = result.model_dump_json() if result is not None else ""
        if result is not None:
            module_outputs[ErrorModule.REFLECTION] = result.justificativa
            module_outputs[ErrorModule.ACTION] = normalized_output
        elif raw_output:
            module_outputs[ErrorModule.ACTION] = raw_output
        if failure_reason:
            module_outputs[ErrorModule.SYSTEM] = failure_reason
        env_response = (
            oracle.model_dump_json()
            if oracle is not None
            else json.dumps({"error": failure_reason}, ensure_ascii=False)
        )
        return AgentDebugTrajectory(
            trajectory_id=trajectory_id,
            task_description=task_description,
            environment="scenario-emulator/front-a/questionnaire-response-evaluator",
            success=bool(oracle and oracle.passed and not failure_reason),
            steps=[
                AgentDebugTrajectoryStep(
                    index=1,
                    module_outputs=module_outputs,
                    step_input=step_input,
                    env_response=env_response,
                    raw_output=raw_output or normalized_output,
                )
            ],
        )

    @staticmethod
    def _questions_and_answers(
        questionnaire: Questionnaire, submission: QuestionnaireSubmission
    ) -> str:
        blocks = []
        for answer in submission.answers:
            question = questionnaire.questions[answer.question_number - 1]
            blocks.append(
                f"[question_id={questionnaire.questionnaire_id}:{answer.question_number}]\n"
                f"Pergunta: {question.text}\nResposta: {answer.text}"
            )
        return "\n\n".join(blocks)

    @staticmethod
    def _evidences(
        questionnaire: Questionnaire, submission: QuestionnaireSubmission
    ) -> list[EvidenciaFormulario]:
        return [
            EvidenciaFormulario(
                questionId=f"{questionnaire.questionnaire_id}:{answer.question_number}",
                questionText=questionnaire.questions[answer.question_number - 1].text,
                answerSnippet=answer.text[:1000],
            )
            for answer in submission.answers
        ]
