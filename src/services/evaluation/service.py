from __future__ import annotations

import json
import time
import uuid

from agno.agent import Agent

from src.agents.model import build_model, get_model_identifier
from src.agents.utils import extract_usage, parse_model_output
from src.prompts.manager import resolve_prompt
from src.prompts.raw_prompts import EVALUATOR_SYSTEM_PROMPT, EVALUATOR_USER_PROMPT
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
from src.services.evaluation.oracle import evaluate_oracle
from src.services.observability.service import current_trace_id, observation


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
    ) -> EvaluationExecution:
        started = time.perf_counter()
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
        try:
            with observation(
                agent_node,
                as_type="agent",
                input={
                    "questionnaire_id": questionnaire.questionnaire_id,
                    "submission_id": submission.submission_id,
                    "response_intent": response_case.intent.value if response_case else "manual",
                },
            ) as span:
                model = build_model("evaluator")
                agent = Agent(
                    name="questionnaire_response_evaluator",
                    model=model,
                    description=system.content,
                    output_schema=NotaLLM,
                )
                response = await agent.arun(user.content)
                raw = parse_model_output(response.content, NotaLLM)
                result = NotaFormulario(
                    valor=raw.valor,
                    justificativa=raw.justificativa,
                    evidencias=self._evidences(questionnaire, submission),
                )
                output: dict = {"result": result.model_dump(mode="json")}
                usage = extract_usage(response)
                if usage:
                    output["usage"] = usage
                span.update(output=output, metadata={"model": get_model_identifier(model)})
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
        except Exception as exc:  # noqa: BLE001 - falha é dado experimental
            return EvaluationExecution(
                scenario_id=scenario_id,
                questionnaire_id=questionnaire.questionnaire_id,
                submission=submission,
                response_case=response_case,
                status=EvaluationStatus.FAILED,
                failure_reason=f"EVALUATION_FAILED: {exc}",
                trace_id=current_trace_id(),
                duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
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
