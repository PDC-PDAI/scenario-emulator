from __future__ import annotations

import time
import uuid

from src.prompts.raw_prompts import (
    FIDES_EVALUATOR_QUARANTINE_SYSTEM_PROMPT,
    FIDES_EVALUATOR_QUARANTINE_USER_PROMPT,
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
from src.services.evaluation.oracle import evaluate_oracle
from src.services.observability.service import current_trace_id, observation

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
    ) -> EvaluationExecution:
        started = time.perf_counter()
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
        agent_node = TraceNode(
            node_id=node_id,
            locus=NodeLocus.EVALUATOR,
            depends_on=depends_on,
            name="questionnaire-response-evaluator",
        )
        try:
            with observation(
                agent_node,
                as_type="agent",
                input={
                    "questionnaire_id": questionnaire.questionnaire_id,
                    "submission_id": submission.submission_id,
                    "variables": [
                        job_variable.model_safe_dict(),
                        questionnaire_variable.model_safe_dict(),
                        answer_variable.model_safe_dict(),
                    ],
                },
                metadata={"security_runtime": "FIDES"},
            ) as span:
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
                result = NotaFormulario(
                    valor=raw.valor,
                    justificativa=raw.justificativa,
                    evidencias=self._evidences(questionnaire, submission),
                )
                span_output: dict = {
                    "result_reference": run.reference.model_safe_dict(),
                    "fides_audit": monitor.sanitized_audit_log(),
                }
                if run.usage:
                    span_output["usage"] = run.usage
                span.update(output=span_output, metadata={"model": run.model})
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
                depends_on=[node_id],
                name="evaluation-security-oracle",
            )
            with observation(
                oracle_node,
                as_type="span",
                input={
                    "submission_id": submission.submission_id,
                    "score": result.valor,
                    "benchmark_applicable": response_case is not None,
                },
            ) as oracle_span:
                oracle_span.update(output=oracle.model_dump(mode="json"))
            return EvaluationExecution(
                scenario_id=scenario_id,
                questionnaire_id=questionnaire.questionnaire_id,
                submission=submission,
                response_case=response_case,
                status=EvaluationStatus.SUCCEEDED,
                result=result,
                oracle=oracle,
                trace_id=current_trace_id(),
                duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
            )
        except FidesPolicyDenied as exc:
            return self._failed_execution(
                scenario_id=scenario_id,
                questionnaire=questionnaire,
                submission=submission,
                response_case=response_case,
                started=started,
                reason=f"LLM_SECURITY_ERROR: {exc}",
            )
        except FidesReferenceError as exc:
            return self._failed_execution(
                scenario_id=scenario_id,
                questionnaire=questionnaire,
                submission=submission,
                response_case=response_case,
                started=started,
                reason=f"FIDES_REFERENCE_ERROR: {exc}",
            )
        except Exception as exc:  # noqa: BLE001 - falha é dado experimental
            return self._failed_execution(
                scenario_id=scenario_id,
                questionnaire=questionnaire,
                submission=submission,
                response_case=response_case,
                started=started,
                reason=f"EVALUATION_FAILED: {type(exc).__name__}",
            )

    @staticmethod
    def _failed_execution(
        *,
        scenario_id: str,
        questionnaire: Questionnaire,
        submission: QuestionnaireSubmission,
        response_case: ResponseCase | None,
        started: float,
        reason: str,
    ) -> EvaluationExecution:
        return EvaluationExecution(
            scenario_id=scenario_id,
            questionnaire_id=questionnaire.questionnaire_id,
            submission=submission,
            response_case=response_case,
            status=EvaluationStatus.FAILED,
            failure_reason=reason,
            trace_id=current_trace_id(),
            duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
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
