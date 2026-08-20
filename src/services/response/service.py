from __future__ import annotations

import json
import time
import uuid

from agno.agent import Agent

from src.agents.model import build_model, get_model_identifier
from src.agents.utils import extract_usage, parse_model_output
from src.prompts.manager import resolve_prompt
from src.prompts.raw_prompts import (
    RESPONSE_GENERATOR_SYSTEM_PROMPT,
    RESPONSE_GENERATOR_USER_PROMPT,
)
from src.schemas.job_description.schema import JobDescription
from src.schemas.observability.schema import NodeLocus, TraceNode
from src.schemas.questionnaire.schema import Questionnaire
from src.schemas.response.schema import (
    GeneratedResponseCaseBatch,
    ResponseBatchStatus,
    ResponseCase,
    ResponseGenerationBatch,
    ResponseIntent,
)
from src.schemas.submission.schema import QuestionnaireSubmissionRequest
from src.services.observability.service import current_trace_id, observation
from src.services.submission.service import SubmissionService

_MAX_CASES_PER_QUESTIONNAIRE = 20


class ResponseGenerationService:
    async def generate(
        self,
        job: JobDescription,
        questionnaire: Questionnaire,
        *,
        benign_count: int,
        malicious_count: int,
        scenario_id: str,
        depends_on: list[str],
    ) -> ResponseGenerationBatch:
        started = time.perf_counter()
        total = benign_count + malicious_count
        if benign_count < 0 or malicious_count < 0 or total > _MAX_CASES_PER_QUESTIONNAIRE:
            raise ValueError(
                f"Contagens de respostas devem ser não negativas e somar no máximo "
                f"{_MAX_CASES_PER_QUESTIONNAIRE}."
            )
        node_id = f"{scenario_id}.responses.{questionnaire.questionnaire_id}"
        if total == 0:
            return ResponseGenerationBatch(
                questionnaire_id=questionnaire.questionnaire_id,
                status=ResponseBatchStatus.SUCCEEDED,
                duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
            )

        canaries = [f"ATTACK_CANARY_{uuid.uuid4().hex}" for _ in range(malicious_count)]
        system = resolve_prompt("front-a/response/system", RESPONSE_GENERATOR_SYSTEM_PROMPT)
        user = resolve_prompt(
            "front-a/response/user",
            RESPONSE_GENERATOR_USER_PROMPT,
            variables={
                "job_description": json.dumps(
                    job.model_dump(mode="json", exclude={"source_brief"}),
                    ensure_ascii=False,
                    indent=2,
                ),
                "questionnaire": questionnaire.model_dump_json(indent=2),
                "benign_count": benign_count,
                "malicious_count": malicious_count,
                "malicious_canaries": json.dumps(canaries, ensure_ascii=False),
            },
        )
        agent_node = TraceNode(
            node_id=node_id,
            locus=NodeLocus.RESPONSE_GENERATOR,
            depends_on=depends_on,
            name="response-case-generator",
        )
        try:
            with observation(
                agent_node,
                as_type="agent",
                input={
                    "questionnaire_id": questionnaire.questionnaire_id,
                    "benign_count": benign_count,
                    "malicious_count": malicious_count,
                },
            ) as span:
                model = build_model("response_generator")
                agent = Agent(
                    name="response_case_generator",
                    model=model,
                    description=system.content,
                    output_schema=GeneratedResponseCaseBatch,
                )
                response = await agent.arun(user.content)
                generated = parse_model_output(response.content, GeneratedResponseCaseBatch)
                self._validate_batch(
                    generated,
                    questionnaire=questionnaire,
                    scenario_id=scenario_id,
                    benign_count=benign_count,
                    malicious_count=malicious_count,
                    expected_canaries=canaries,
                )
                cases = [
                    ResponseCase(
                        **case.model_dump(),
                        questionnaire_id=questionnaire.questionnaire_id,
                        sequence=index,
                    )
                    for index, case in enumerate(generated.cases, start=1)
                ]
                output = {"cases": [case.model_dump(mode="json") for case in cases]}
                usage = extract_usage(response)
                if usage:
                    output["usage"] = usage
                span.update(output=output, metadata={"model": get_model_identifier(model)})
            return ResponseGenerationBatch(
                questionnaire_id=questionnaire.questionnaire_id,
                status=ResponseBatchStatus.SUCCEEDED,
                cases=cases,
                trace_id=current_trace_id(),
                duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
            )
        except Exception as exc:  # noqa: BLE001 - falha é parte do experimento
            return ResponseGenerationBatch(
                questionnaire_id=questionnaire.questionnaire_id,
                status=ResponseBatchStatus.FAILED,
                failure_reason=f"RESPONSE_GENERATION_FAILED: {exc}",
                trace_id=current_trace_id(),
                duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
            )

    @staticmethod
    def _validate_batch(
        generated: GeneratedResponseCaseBatch,
        *,
        questionnaire: Questionnaire,
        scenario_id: str,
        benign_count: int,
        malicious_count: int,
        expected_canaries: list[str],
    ) -> None:
        benign = sum(case.intent is ResponseIntent.BENIGN for case in generated.cases)
        malicious = sum(case.intent is ResponseIntent.MALICIOUS for case in generated.cases)
        if (benign, malicious) != (benign_count, malicious_count):
            raise ValueError(
                "Gerador não respeitou as contagens de respostas: "
                f"esperado=({benign_count}, {malicious_count}), recebido=({benign}, {malicious})."
            )
        actual_canaries = sorted(case.canary for case in generated.cases if case.canary)
        if actual_canaries != sorted(expected_canaries):
            raise ValueError("O gerador não utilizou exatamente os canários fornecidos.")
        validator = SubmissionService()
        for case in generated.cases:
            validator.create(
                scenario_id=scenario_id,
                questionnaire=questionnaire,
                request=QuestionnaireSubmissionRequest(answers=case.answers),
            )
