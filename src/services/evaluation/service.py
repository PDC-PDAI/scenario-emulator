from __future__ import annotations

import json
import time
import uuid
from typing import Any

from src.prompts.raw_prompts import (
    FIDES_EVALUATOR_QUARANTINE_SYSTEM_PROMPT,
    FIDES_EVALUATOR_QUARANTINE_USER_PROMPT,
)
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
from src.security.fides import (
    ConfidentialityLabel,
    ContentLabel,
    ContentVariableStore,
    FidesPolicyDenied,
    FidesQuarantinedLLM,
    FidesReferenceError,
    FidesReferenceMonitor,
    IntegrityLabel,
    ToolPolicy,
    VariableType,
)
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

_TRUSTED_PUBLIC = ContentLabel(IntegrityLabel.TRUSTED, ConfidentialityLabel.PUBLIC)
_UNTRUSTED_PUBLIC = ContentLabel(IntegrityLabel.UNTRUSTED, ConfidentialityLabel.PUBLIC)


class EvaluationService:
    def __init__(self, *, quarantined_llm: FidesQuarantinedLLM | None = None) -> None:
        self._quarantined_llm = quarantined_llm or FidesQuarantinedLLM()

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
        store = ContentVariableStore()
        monitor = FidesReferenceMonitor(store)
        job_variable = store.put(
            job.model_dump(mode="json", exclude={"source_brief"}),
            variable_type=VariableType.JOB_DESCRIPTION,
            label=_UNTRUSTED_PUBLIC,
        )
        questionnaire_variable = store.put(
            questionnaire.model_dump(mode="json"),
            variable_type=VariableType.QUESTION,
            label=_UNTRUSTED_PUBLIC,
        )
        answer_variable = store.put(
            [answer.model_dump(mode="json") for answer in submission.answers],
            variable_type=VariableType.ANSWER,
            label=_UNTRUSTED_PUBLIC,
        )
        safe_variables = [
            job_variable.model_safe_dict(),
            questionnaire_variable.model_safe_dict(),
            answer_variable.model_safe_dict(),
        ]
        safe_step_input = json.dumps(safe_variables, ensure_ascii=False, sort_keys=True)
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
        run = None

        with (
            observation(
                agent_node,
                as_type="agent",
                trace_id=langfuse_trace_id,
                input={
                    "questionnaire_id": questionnaire.questionnaire_id,
                    "submission_id": submission.submission_id,
                    "variables": safe_variables,
                },
                metadata={"trajectory_id": trajectory_id, "security_runtime": "FIDES"},
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
                generation_node = TraceNode(
                    node_id=f"{node_id}.generation",
                    locus=NodeLocus.EVALUATOR,
                    depends_on=[node_id],
                    name="questionnaire-evaluation-generation",
                )
                with observation(
                    generation_node,
                    as_type="generation",
                    input={"variables": safe_variables},
                    metadata={"security_runtime": "FIDES"},
                ) as generation:
                    run = await self._quarantined_llm.run(
                        monitor=monitor,
                        control=_TRUSTED_PUBLIC,
                        tool_name="avaliar_respostas_quarentena",
                        references={
                            "job_description": job_variable.reference,
                            "questionnaire": questionnaire_variable.reference,
                            "answers": answer_variable.reference,
                        },
                        expected_types={
                            "job_description": VariableType.JOB_DESCRIPTION,
                            "questionnaire": VariableType.QUESTION,
                            "answers": VariableType.ANSWER,
                        },
                        system_prompt_key="front-a/evaluator/fides/quarantine/system",
                        system_prompt_fallback=FIDES_EVALUATOR_QUARANTINE_SYSTEM_PROMPT,
                        user_prompt_key="front-a/evaluator/fides/quarantine/user",
                        user_prompt_fallback=FIDES_EVALUATOR_QUARANTINE_USER_PROMPT,
                        output_schema=NotaLLM,
                        output_type=VariableType.SCORE,
                        model_role="evaluator",
                        system_variables={"system_canary": system_canary},
                    )
                    generation.update(
                        output={"result_reference": run.reference.model_safe_dict()},
                        metadata={"model": run.model},
                        usage_details=run.usage or None,
                    )
                values = monitor.authorize_and_resolve(
                    tool="materializar_nota_formulario",
                    control=_TRUSTED_PUBLIC,
                    references={"score": run.reference.reference},
                    policy=ToolPolicy(
                        argument_types={"score": VariableType.SCORE},
                        accepts_untrusted_arguments=True,
                        max_confidentiality=ConfidentialityLabel.PUBLIC,
                    ),
                )
                raw = NotaLLM.model_validate(values["score"])
                raw_output = raw.model_dump_json()
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
                    step_input=safe_step_input,
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
            except FidesPolicyDenied as exc:
                execution = self._failed_with_trajectory(
                    trajectory_id=trajectory_id,
                    task_description=task_description,
                    step_input=safe_step_input,
                    raw_output=raw_output,
                    scenario_id=scenario_id,
                    questionnaire=questionnaire,
                    submission=submission,
                    response_case=response_case,
                    started=started,
                    trace_id=langfuse_trace_id,
                    reason=f"LLM_SECURITY_ERROR: {exc}",
                )
            except FidesReferenceError as exc:
                execution = self._failed_with_trajectory(
                    trajectory_id=trajectory_id,
                    task_description=task_description,
                    step_input=safe_step_input,
                    raw_output=raw_output,
                    scenario_id=scenario_id,
                    questionnaire=questionnaire,
                    submission=submission,
                    response_case=response_case,
                    started=started,
                    trace_id=langfuse_trace_id,
                    reason=f"FIDES_REFERENCE_ERROR: {exc}",
                )
            except Exception as exc:  # noqa: BLE001 - falha é dado experimental
                execution = self._failed_with_trajectory(
                    trajectory_id=trajectory_id,
                    task_description=task_description,
                    step_input=safe_step_input,
                    raw_output=raw_output,
                    scenario_id=scenario_id,
                    questionnaire=questionnaire,
                    submission=submission,
                    response_case=response_case,
                    started=started,
                    trace_id=langfuse_trace_id,
                    reason=f"EVALUATION_FAILED: {exc}",
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
                        "fides_audit": monitor.sanitized_audit_log(),
                    }
                ),
                metadata={"model": run.model} if run is not None else None,
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
        if result is not None:
            module_outputs[ErrorModule.REFLECTION] = result.justificativa
            module_outputs[ErrorModule.ACTION] = raw_output or result.model_dump_json()
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
                    raw_output=raw_output or (result.model_dump_json() if result else ""),
                )
            ],
        )

    @classmethod
    def _failed_with_trajectory(
        cls,
        *,
        trajectory_id: str,
        task_description: str,
        step_input: str,
        raw_output: str,
        scenario_id: str,
        questionnaire: Questionnaire,
        submission: QuestionnaireSubmission,
        response_case: ResponseCase | None,
        started: float,
        trace_id: str | None,
        reason: str,
    ) -> EvaluationExecution:
        trajectory = cls._trajectory(
            trajectory_id=trajectory_id,
            task_description=task_description,
            step_input=step_input,
            raw_output=raw_output,
            failure_reason=reason,
        )
        return EvaluationExecution(
            trajectory_id=trajectory_id,
            scenario_id=scenario_id,
            questionnaire_id=questionnaire.questionnaire_id,
            submission=submission,
            response_case=response_case,
            status=EvaluationStatus.FAILED,
            failure_reason=reason,
            trace_id=current_trace_id() or trace_id,
            duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
            agent_debug_trajectory=trajectory,
        )

    @staticmethod
    def _evidences(
        questionnaire: Questionnaire,
        submission: QuestionnaireSubmission,
    ) -> list[EvidenciaFormulario]:
        return [
            EvidenciaFormulario(
                questionId=f"{questionnaire.questionnaire_id}:{answer.question_number}",
                questionText=questionnaire.questions[answer.question_number - 1].text,
                answerSnippet=answer.text[:1000],
            )
            for answer in submission.answers
        ]
