from __future__ import annotations

import json
import time
import uuid
from typing import Any

from agno.agent import Agent

from src.agents.model import build_model, get_model_identifier
from src.agents.utils import extract_usage, parse_model_output
from src.prompts.manager import resolve_prompt
from src.prompts.raw_prompts import (
    RESPONSE_GENERATOR_SYSTEM_PROMPT,
    RESPONSE_GENERATOR_USER_PROMPT,
)
from src.schemas.agent_debug.schema import (
    AgentDebugTrajectory,
    AgentDebugTrajectoryStep,
    ErrorModule,
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
from src.services.agent_debug.service import response_failure_annotation
from src.services.observability.service import (
    create_langfuse_trace_id,
    current_trace_id,
    observation,
    trace_attributes,
)
from src.services.submission.service import SubmissionService
from src.settings import settings
from src.utils.privacy import redact_for_trace

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
        experiment_tags: list[str] | None = None,
        experiment_metadata: dict[str, str | None] | None = None,
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

        batch_id = f"response-batch-{uuid.uuid4()}"
        langfuse_trace_id = create_langfuse_trace_id(seed=batch_id)
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
        task_description = (
            f"Gerar {benign_count} casos benignos e {malicious_count} casos adversariais "
            f"para o questionário {questionnaire.questionnaire_id} da vaga '{job.title}' "
            f"({job.id})."
        )
        raw_output = ""
        with (
            observation(
                agent_node,
                as_type="agent",
                trace_id=langfuse_trace_id,
                input={
                    "questionnaire_id": questionnaire.questionnaire_id,
                    "benign_count": benign_count,
                    "malicious_count": malicious_count,
                },
                metadata={"trajectory_id": batch_id},
            ) as span,
            trace_attributes(
                session_id=scenario_id,
                trace_name="response-generation-trajectory",
                environment=settings.ENVIRONMENT,
                tags=[
                    "front-a",
                    "agentdebug-rh",
                    "trajectory",
                    "response-generator",
                    *(experiment_tags or []),
                ],
                metadata={
                    "scenario_id": scenario_id,
                    "trajectory_id": batch_id,
                    "questionnaire_id": questionnaire.questionnaire_id,
                    **(experiment_metadata or {}),
                },
            ),
        ):
            try:
                model = build_model("response_generator")
                agent = Agent(
                    name="response_case_generator",
                    model=model,
                    description=system.content,
                    output_schema=GeneratedResponseCaseBatch,
                )
                generation_node = TraceNode(
                    node_id=f"{node_id}.generation",
                    locus=NodeLocus.RESPONSE_GENERATOR,
                    depends_on=[node_id],
                    name="response-case-generation",
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
                trajectory = self._trajectory(
                    batch_id=batch_id,
                    task_description=task_description,
                    step_input=user.content,
                    raw_output=raw_output,
                    cases=cases,
                )
                batch = ResponseGenerationBatch(
                    batch_id=batch_id,
                    questionnaire_id=questionnaire.questionnaire_id,
                    status=ResponseBatchStatus.SUCCEEDED,
                    cases=cases,
                    trace_id=current_trace_id() or langfuse_trace_id,
                    duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
                    agent_debug_trajectory=trajectory,
                )
            except Exception as exc:  # noqa: BLE001 - falha é parte do experimento
                failure_reason = f"RESPONSE_GENERATION_FAILED: {exc}"
                trajectory = self._trajectory(
                    batch_id=batch_id,
                    task_description=task_description,
                    step_input=user.content,
                    raw_output=raw_output,
                    failure_reason=failure_reason,
                )
                batch = ResponseGenerationBatch(
                    batch_id=batch_id,
                    questionnaire_id=questionnaire.questionnaire_id,
                    status=ResponseBatchStatus.FAILED,
                    failure_reason=failure_reason,
                    trace_id=current_trace_id() or langfuse_trace_id,
                    duration_ms=max(0, round((time.perf_counter() - started) * 1000)),
                    agent_debug_trajectory=trajectory,
                )

            annotation = response_failure_annotation(batch)
            if annotation is not None:
                batch = batch.model_copy(update={"failure_annotation": annotation})
            passed = batch.status is ResponseBatchStatus.SUCCEEDED
            span.update(
                output=redact_for_trace(
                    {
                        "trajectory_id": batch_id,
                        "status": batch.status.value,
                        "case_count": len(batch.cases),
                        "cases": batch.cases,
                        "failure_reason": batch.failure_reason,
                        "agent_debug_trajectory": batch.agent_debug_trajectory,
                        "failure_annotation": batch.failure_annotation,
                    }
                ),
                metadata={"model": get_model_identifier(model)} if "model" in locals() else None,
                level="DEFAULT" if passed else "ERROR",
                status_message=annotation.message if annotation else None,
            )
            return batch

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
        batch_id: str,
        task_description: str,
        step_input: str,
        raw_output: str,
        cases: list[ResponseCase] | None = None,
        failure_reason: str | None = None,
    ) -> AgentDebugTrajectory:
        cases = cases or []
        action = raw_output or json.dumps(
            [case.model_dump(mode="json") for case in cases],
            ensure_ascii=False,
            sort_keys=True,
        )
        module_outputs: dict[ErrorModule, str] = {}
        if action:
            module_outputs[ErrorModule.ACTION] = action
        if failure_reason:
            module_outputs[ErrorModule.SYSTEM] = failure_reason
        return AgentDebugTrajectory(
            trajectory_id=batch_id,
            task_description=task_description,
            environment="scenario-emulator/front-a/response-case-generator",
            success=not failure_reason,
            steps=[
                AgentDebugTrajectoryStep(
                    index=1,
                    module_outputs=module_outputs,
                    step_input=step_input,
                    env_response=json.dumps(
                        {
                            "validation": "failed" if failure_reason else "passed",
                            "case_count": len(cases),
                            "failure_reason": failure_reason,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    raw_output=action,
                )
            ],
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
