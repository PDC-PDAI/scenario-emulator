from __future__ import annotations

import json
import time
import uuid

from src.prompts.manager import resolve_prompt
from src.prompts.raw_prompts import (
    EVALUATOR_ANSWER_SYSTEM_PROMPT,
    EVALUATOR_ANSWER_USER_PROMPT,
    EVALUATOR_CONTEXT_SYSTEM_PROMPT,
    EVALUATOR_CONTEXT_USER_PROMPT,
    EVALUATOR_SCORE_SYSTEM_PROMPT,
    EVALUATOR_SCORE_USER_PROMPT,
)
from src.schemas.evaluation.schema import (
    EvaluationExecution,
    EvaluationStatus,
    EvidenciaFormulario,
    NotaFormulario,
)
from src.schemas.job_description.schema import JobDescription
from src.schemas.observability.schema import NodeLocus, TraceNode
from src.schemas.questionnaire.schema import Questionnaire
from src.schemas.response.schema import ResponseCase
from src.schemas.submission.schema import QuestionnaireSubmission
from src.security.camel import DataOrigin, ProtectedValue, Provenance, QuarantinedLLM
from src.security.policies import (
    authorize_evaluation,
    authorize_professional_context,
    deterministic_security_findings,
    validate_cited_evidence,
)
from src.security.schemas import AnswerExtraction, ProfessionalContext, QuestionScore
from src.services.evaluation.oracle import evaluate_oracle
from src.services.observability.service import current_trace_id, observation

_MIN_JUSTIFICATION_LENGTH = 100
_MAX_JUSTIFICATION_LENGTH = 2000


class EvaluationService:
    def __init__(self, *, quarantined_llm: QuarantinedLLM | None = None) -> None:
        self.quarantined_llm = quarantined_llm or QuarantinedLLM()

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
        attack_canary = response_case.canary if response_case else None
        node = TraceNode(
            node_id=node_id,
            locus=NodeLocus.EVALUATOR,
            depends_on=depends_on,
            name="questionnaire-camel-evaluator",
        )
        try:
            with observation(
                node,
                as_type="agent",
                input={
                    "questionnaire_id": questionnaire.questionnaire_id,
                    "submission_id": submission.submission_id,
                    "security_architecture": "camel-adapted",
                },
            ) as span:
                context = await self._extract_job_context(job, system_canary)
                scores: list[ProtectedValue[QuestionScore]] = []
                answers = sorted(submission.answers, key=lambda item: item.question_number)
                for answer in answers:
                    question = questionnaire.questions[answer.question_number - 1]
                    if deterministic_security_findings(question.text):
                        raise ValueError("Pergunta contém instrução não autorizada.")
                    extraction = await self._extract_answer(
                        question=question.text,
                        answer=answer.text,
                        system_canary=system_canary,
                        attack_canary=attack_canary,
                    )
                    score = await self._score_answer(
                        question_number=answer.question_number,
                        question=question.text,
                        question_weight=question.weight,
                        context=context,
                        extraction=extraction,
                        system_canary=system_canary,
                        attack_canary=attack_canary,
                    )
                    scores.append(score)

                authorize_evaluation(
                    scores,
                    expected_count=len(answers),
                    canaries=(system_canary, attack_canary or ""),
                )
                result = self._assemble_result(
                    questionnaire=questionnaire,
                    submission=submission,
                    scores=scores,
                    canaries=(system_canary, attack_canary or ""),
                )
                span.update(output={"result": result.model_dump(mode="json")})

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

    async def _extract_job_context(
        self,
        job: JobDescription,
        system_canary: str,
    ) -> ProtectedValue[ProfessionalContext]:
        job_json = json.dumps(
            job.model_dump(mode="json", exclude={"source_brief"}),
            ensure_ascii=False,
            indent=2,
        )
        if deterministic_security_findings(job_json):
            raise ValueError("Vaga contém instrução não autorizada.")
        system = resolve_prompt(
            "front-a/evaluator/context/system",
            EVALUATOR_CONTEXT_SYSTEM_PROMPT,
            variables={"system_canary": system_canary},
        )
        user = resolve_prompt(
            "front-a/evaluator/context/user",
            EVALUATOR_CONTEXT_USER_PROMPT,
            variables={"job_data": job_json},
        )
        extracted = await self.quarantined_llm.run(
            name="evaluation-job-context-extractor",
            role="evaluator",
            system=system.content,
            user=user.content,
            output_schema=ProfessionalContext,
        )
        source = ProtectedValue(job_json, Provenance.from_origin(DataOrigin.UNTRUSTED_JOB))
        gated = source.derived(job_json, "security_gate")
        context = gated.derived(extracted, "context_extraction")
        authorize_professional_context(
            context,
            sources=(job_json,),
            canary=system_canary,
        )
        return context

    async def _extract_answer(
        self,
        *,
        question: str,
        answer: str,
        system_canary: str,
        attack_canary: str | None,
    ) -> ProtectedValue[AnswerExtraction]:
        system = resolve_prompt(
            "front-a/evaluator/answer/system",
            EVALUATOR_ANSWER_SYSTEM_PROMPT,
            variables={"system_canary": system_canary},
        )
        user = resolve_prompt(
            "front-a/evaluator/answer/user",
            EVALUATOR_ANSWER_USER_PROMPT,
            variables={"question": question, "answer": answer},
        )
        extracted = await self.quarantined_llm.run(
            name="evaluation-answer-extractor",
            role="evaluator",
            system=system.content,
            user=user.content,
            output_schema=AnswerExtraction,
        )
        validate_cited_evidence(extracted.source_evidence, (answer,))
        produced = "\n".join([*extracted.claims, *extracted.gaps, *extracted.source_evidence])
        if system_canary in produced or (attack_canary and attack_canary in produced):
            raise ValueError("Canário apareceu em campo produzido pelo extrator.")
        if deterministic_security_findings(produced):
            raise ValueError("Extrator propagou instrução não autorizada.")
        if not extracted.has_professional_merit and extracted.claims:
            raise ValueError("Extrator retornou alegações para resposta sem mérito.")
        source = ProtectedValue(answer, Provenance.from_origin(DataOrigin.UNTRUSTED_ANSWER))
        return source.derived(extracted, "answer_extraction")

    async def _score_answer(
        self,
        *,
        question_number: int,
        question: str,
        question_weight: int,
        context: ProtectedValue[ProfessionalContext],
        extraction: ProtectedValue[AnswerExtraction],
        system_canary: str,
        attack_canary: str | None,
    ) -> ProtectedValue[QuestionScore]:
        if not extraction.value.has_professional_merit or not extraction.value.claims:
            score = QuestionScore(
                question_number=question_number,
                valor=0,
                justificativa=(
                    "A resposta não apresenta conteúdo profissional verificável que "
                    "permita demonstrar domínio da competência avaliada."
                ),
            )
            return extraction.derived(score, "question_scoring", context)

        system = resolve_prompt(
            "front-a/evaluator/score/system",
            EVALUATOR_SCORE_SYSTEM_PROMPT,
            variables={"system_canary": system_canary},
        )
        user = resolve_prompt(
            "front-a/evaluator/score/user",
            EVALUATOR_SCORE_USER_PROMPT,
            variables={
                "question_number": question_number,
                "question_weight": question_weight,
                "question": question,
                "professional_context": context.value.model_dump_json(
                    exclude={"source_evidence"}
                ),
                "claims": json.dumps(extraction.value.claims, ensure_ascii=False),
                "gaps": json.dumps(extraction.value.gaps, ensure_ascii=False),
            },
        )
        # A resposta original não aparece em `user`; somente estruturas autorizadas.
        score = await self.quarantined_llm.run(
            name="evaluation-question-scorer",
            role="evaluator",
            system=system.content,
            user=user.content,
            output_schema=QuestionScore,
        )
        if score.question_number != question_number:
            raise ValueError("Pontuador alterou a numeração da pergunta.")
        if system_canary in score.justificativa or (
            attack_canary and attack_canary in score.justificativa
        ):
            raise ValueError("Canário apareceu na justificativa da pergunta.")
        if deterministic_security_findings(score.justificativa):
            raise ValueError("Pontuador propagou instrução não autorizada.")
        return extraction.derived(score, "question_scoring", context)

    @classmethod
    def _assemble_result(
        cls,
        *,
        questionnaire: Questionnaire,
        submission: QuestionnaireSubmission,
        scores: list[ProtectedValue[QuestionScore]],
        canaries: tuple[str, str],
    ) -> NotaFormulario:
        score_by_number = {item.value.question_number: item.value for item in scores}
        weighted_sum = 0.0
        total_weight = 0
        for answer in submission.answers:
            weight = questionnaire.questions[answer.question_number - 1].weight
            weighted_sum += score_by_number[answer.question_number].valor * weight
            total_weight += weight
        if total_weight <= 0:
            raise ValueError("Questionário não possui pesos válidos.")
        value = round(weighted_sum / total_weight, 2)
        justification = cls._build_justification(scores)
        if any(canary and canary in justification for canary in canaries):
            raise ValueError("Canário apareceu na justificativa final.")
        return NotaFormulario(
            valor=value,
            justificativa=justification,
            evidencias=cls._evidences(questionnaire, submission),
        )

    @staticmethod
    def _build_justification(scores: list[ProtectedValue[QuestionScore]]) -> str:
        parts = [
            f"Pergunta {item.value.question_number}: {item.value.justificativa.strip()}"
            for item in scores
        ]
        text = " ".join(parts)
        if len(text) < _MIN_JUSTIFICATION_LENGTH:
            text += (
                " A nota considera exclusivamente o mérito profissional demonstrado "
                "nas alegações sanitizadas de cada resposta."
            )
        if len(text) > _MAX_JUSTIFICATION_LENGTH:
            text = text[: _MAX_JUSTIFICATION_LENGTH - 1].rstrip() + "…"
        return text

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
