from __future__ import annotations

import time
import uuid
from typing import Any

import structlog

from src.agents.model import configured_model_identifier
from src.schemas.benchmark.schema import BenchmarkRecord
from src.schemas.evaluation.schema import EvaluationExecution, EvaluationStatus
from src.schemas.experiment.schema import PipelineProfile, ResearchFront, validate_front_pipeline
from src.schemas.observability.schema import NodeLocus, TraceNode
from src.schemas.questionnaire.schema import ExecutionStatus, QuestionnaireExecution
from src.schemas.response.schema import ResponseBatchStatus, ResponseGenerationBatch
from src.schemas.scenario.schema import ScenarioRun
from src.schemas.submission.schema import QuestionnaireSubmissionRequest, SubmissionStatus
from src.services.agent_debug.service import (
    evaluation_failure_annotation,
    failure_annotation,
    response_failure_annotation,
)
from src.services.coordinator_prompt.service import CoordinatorPromptService
from src.services.evaluation.service import EvaluationService
from src.services.job_description.service import JobDescriptionService
from src.services.observability.service import observation, trace_attributes
from src.services.questionnaire.service import QuestionnaireService
from src.services.response.service import ResponseGenerationService
from src.services.submission.service import SubmissionService

logger = structlog.get_logger(__name__)

_PIPELINE = "front-a"
_STAGE_TAGS = {
    "scenario": "PIPELINE",
    "job-description": "JOB_DESCRIPTION",
    "coordinator-prompts": "COORDINATOR_PROMPTS",
    "questionnaire": "QUESTIONNAIRE",
}


def _research_targets(front: ResearchFront | None) -> list[str]:
    if front is ResearchFront.SECURITY:
        return ["RecruitSecBench"]
    if front is ResearchFront.ERROR_RECOVERY:
        return ["AgentDebug-RH"]
    return ["AgentDebug-RH", "RecruitSecBench"]


def _experiment_provenance(
    front: ResearchFront | None,
    profile: str | None,
) -> dict[str, str | None]:
    return {
        "research_front": front.value if front else None,
        "experiment_profile": profile,
    }


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _progress_fields(
    *,
    scenario_id: str,
    stage: str,
    locus: NodeLocus,
    status: str,
    **extra: Any,
) -> dict[str, Any]:
    stage_tag = _STAGE_TAGS[stage]
    return {
        "scenario_id": scenario_id,
        "pipeline": _PIPELINE,
        "stage": stage,
        "stage_tag": stage_tag,
        "locus": locus.value,
        "status": status,
        "tags": [_PIPELINE, stage_tag, locus.value],
        **extra,
    }


class ScenarioService:
    def __init__(
        self,
        *,
        job_service: JobDescriptionService | None = None,
        coordinator_service: CoordinatorPromptService | None = None,
        questionnaire_service: QuestionnaireService | None = None,
        response_service: ResponseGenerationService | None = None,
        evaluation_service: EvaluationService | None = None,
        submission_service: SubmissionService | None = None,
    ) -> None:
        self.job_service = job_service or JobDescriptionService()
        self.coordinator_service = coordinator_service or CoordinatorPromptService()
        self.questionnaire_service = questionnaire_service or QuestionnaireService()
        self.response_service = response_service or ResponseGenerationService()
        self.evaluation_service = evaluation_service or EvaluationService()
        self.submission_service = submission_service or SubmissionService()

    async def run(  # noqa: PLR0915 - fluxo linear preserva as etapas auditáveis
        self,
        brief: str,
        *,
        benign_count: int = 3,
        malicious_count: int = 3,
        benign_response_count: int = 1,
        malicious_response_count: int = 1,
        questionnaire_evaluator: bool = True,
        research_front: ResearchFront | None = None,
        experiment_profile: str | None = None,
    ) -> ScenarioRun:
        pipeline_started = time.perf_counter()
        pipeline = PipelineProfile(
            benign_commands=benign_count,
            malicious_commands=malicious_count,
            benign_responses=benign_response_count,
            malicious_responses=malicious_response_count,
            questionnaire_evaluator=questionnaire_evaluator,
        )
        validate_front_pipeline(research_front, pipeline)
        response_total = benign_response_count + malicious_response_count
        scenario_id = f"scenario-{uuid.uuid4()}"
        research_targets = _research_targets(research_front)
        experiment_context = _experiment_provenance(research_front, experiment_profile)
        experiment_tags = [
            value
            for value in (
                research_front.value if research_front else None,
                experiment_profile,
            )
            if value
        ]
        current_stage = "scenario"
        current_locus = NodeLocus.SYSTEM
        logger.info(
            "[PIPELINE] Iniciando geração do cenário",
            **_progress_fields(
                scenario_id=scenario_id,
                stage=current_stage,
                locus=current_locus,
                status="started",
                benign_count=benign_count,
                malicious_count=malicious_count,
                **experiment_context,
            ),
        )
        root_node = TraceNode(
            node_id=scenario_id,
            locus=NodeLocus.SYSTEM,
            name="scenario-front-a",
        )
        with observation(
            root_node,
            as_type="agent",
            input={
                "brief": brief,
                "benign_count": benign_count,
                "malicious_count": malicious_count,
                "benign_response_count": benign_response_count,
                "malicious_response_count": malicious_response_count,
                "questionnaire_evaluator": questionnaire_evaluator,
            },
            metadata={
                "research_targets": research_targets,
                **experiment_context,
            },
        ) as root_span:
            with trace_attributes(
                session_id=scenario_id,
                tags=[
                    "front-a",
                    *experiment_tags,
                ],
                metadata={
                    "scenario_id": scenario_id,
                    "pipeline": "front-a",
                    **experiment_context,
                },
            ):
                try:
                    current_stage = "job-description"
                    current_locus = NodeLocus.JOB
                    stage_started = time.perf_counter()
                    logger.info(
                        "[JOB_DESCRIPTION] Gerando descrição da vaga",
                        **_progress_fields(
                            scenario_id=scenario_id,
                            stage=current_stage,
                            locus=current_locus,
                            status="started",
                        ),
                    )
                    job = await self.job_service.generate(brief, scenario_id=scenario_id)
                    logger.info(
                        "[JOB_DESCRIPTION] Descrição da vaga gerada",
                        **_progress_fields(
                            scenario_id=scenario_id,
                            stage=current_stage,
                            locus=current_locus,
                            status="completed",
                            duration_ms=_elapsed_ms(stage_started),
                            job_description_id=job.id,
                        ),
                    )

                    current_stage = "coordinator-prompts"
                    current_locus = NodeLocus.COORDINATOR
                    stage_started = time.perf_counter()
                    logger.info(
                        "[COORDINATOR_PROMPTS] Gerando prompts do coordenador",
                        **_progress_fields(
                            scenario_id=scenario_id,
                            stage=current_stage,
                            locus=current_locus,
                            status="started",
                            benign_count=benign_count,
                            malicious_count=malicious_count,
                        ),
                    )
                    prompts = await self.coordinator_service.generate(
                        job,
                        benign_count=benign_count,
                        malicious_count=malicious_count,
                        scenario_id=scenario_id,
                    )
                    logger.info(
                        "[COORDINATOR_PROMPTS] Prompts do coordenador gerados",
                        **_progress_fields(
                            scenario_id=scenario_id,
                            stage=current_stage,
                            locus=current_locus,
                            status="completed",
                            duration_ms=_elapsed_ms(stage_started),
                            prompt_count=len(prompts.prompts),
                        ),
                    )

                    executions: list[QuestionnaireExecution] = []
                    total_questionnaires = len(prompts.prompts)
                    for index, prompt in enumerate(prompts.prompts, start=1):
                        current_stage = "questionnaire"
                        current_locus = NodeLocus.SUB_GER
                        stage_started = time.perf_counter()
                        progress = {
                            "questionnaire_index": index,
                            "questionnaire_total": total_questionnaires,
                            "coordinator_prompt_id": prompt.id,
                            "prompt_intent": prompt.intent.value,
                            "prompt_category": prompt.category.value,
                            "expected_action": prompt.expected_action.value,
                        }
                        logger.info(
                            f"[QUESTIONNAIRE] Gerando questionário {index}/{total_questionnaires}",
                            **_progress_fields(
                                scenario_id=scenario_id,
                                stage=current_stage,
                                locus=current_locus,
                                status="started",
                                **progress,
                            ),
                        )
                        questionnaire_trace_kwargs: dict[str, Any] = {}
                        if research_front is not None:
                            questionnaire_trace_kwargs = {
                                "experiment_tags": experiment_tags,
                                "experiment_metadata": experiment_context,
                            }
                        execution = await self.questionnaire_service.execute(
                            job,
                            prompt,
                            scenario_id=scenario_id,
                            **questionnaire_trace_kwargs,
                        )
                        executions.append(execution)
                        annotation = failure_annotation(execution)
                        logger.info(
                            (
                                "[QUESTIONNAIRE] "
                                f"Questionário {index}/{total_questionnaires} concluído"
                            ),
                            **_progress_fields(
                                scenario_id=scenario_id,
                                stage=current_stage,
                                locus=current_locus,
                                status="completed",
                                duration_ms=_elapsed_ms(stage_started),
                                execution_status=execution.status.value,
                                benchmark_passed=execution.benchmark_passed,
                                failure_code=annotation.code.value if annotation else None,
                                failure_step=annotation.step_index if annotation else None,
                                error_module=annotation.module.value if annotation else None,
                                error_type=annotation.error_type.value if annotation else None,
                                retryable=annotation.retryable if annotation else None,
                                **progress,
                            ),
                        )

                    records = [
                        self._benchmark_record(
                            scenario_id,
                            execution,
                            research_front=research_front,
                            experiment_profile=experiment_profile,
                        )
                        for execution in executions
                    ]
                    response_batches: list[ResponseGenerationBatch] = []
                    evaluation_executions: list[EvaluationExecution] = []
                    for execution in executions:
                        if not questionnaire_evaluator or response_total == 0:
                            continue
                        questionnaire = execution.questionnaire
                        if (
                            execution.status is not ExecutionStatus.SUCCEEDED
                            or questionnaire is None
                        ):
                            continue
                        questionnaire_node = (
                            f"{scenario_id}.questionnaire."
                            f"{execution.coordinator_prompt.sequence:03d}"
                        )
                        batch = await self.response_service.generate(
                            job,
                            questionnaire,
                            benign_count=benign_response_count,
                            malicious_count=malicious_response_count,
                            scenario_id=scenario_id,
                            depends_on=[questionnaire_node],
                            experiment_tags=experiment_tags,
                            experiment_metadata=experiment_context,
                        )
                        response_batches.append(batch)
                        records.append(
                            self._response_benchmark_record(
                                scenario_id,
                                execution,
                                batch,
                                research_front=research_front,
                                experiment_profile=experiment_profile,
                            )
                        )
                        if batch.status is not ResponseBatchStatus.SUCCEEDED:
                            continue
                        response_node = f"{scenario_id}.responses.{questionnaire.questionnaire_id}"
                        for case in batch.cases:
                            submission = self.submission_service.create(
                                scenario_id=scenario_id,
                                questionnaire=questionnaire,
                                request=QuestionnaireSubmissionRequest(
                                    respondent_reference=f"synthetic:{case.case_id}",
                                    answers=case.answers,
                                ),
                            )
                            evaluation = await self.evaluation_service.evaluate(
                                scenario_id=scenario_id,
                                job=job,
                                questionnaire=questionnaire,
                                submission=submission,
                                response_case=case,
                                depends_on=[response_node],
                                experiment_tags=experiment_tags,
                                experiment_metadata=experiment_context,
                            )
                            submission.status = (
                                SubmissionStatus.EVALUATED
                                if evaluation.status is EvaluationStatus.SUCCEEDED
                                else SubmissionStatus.EVALUATION_FAILED
                            )
                            evaluation.submission = submission
                            evaluation_executions.append(evaluation)
                            records.append(
                                self._evaluation_benchmark_record(
                                    scenario_id,
                                    execution,
                                    batch,
                                    evaluation,
                                    research_front=research_front,
                                    experiment_profile=experiment_profile,
                                )
                            )
                    result = ScenarioRun(
                        scenario_id=scenario_id,
                        research_targets=research_targets,
                        research_front=research_front,
                        experiment_profile=experiment_profile,
                        job_description=job,
                        coordinator_prompts=prompts,
                        executions=executions,
                        response_batches=response_batches,
                        evaluation_executions=evaluation_executions,
                        benchmark_records=records,
                    )
                    passed = sum(item.benchmark_passed for item in executions)
                    failed = sum(not item.benchmark_passed for item in executions)
                    root_span.update(
                        output={
                            "scenario_id": scenario_id,
                            "job_description_id": job.id,
                            "commands": len(prompts.prompts),
                            "passed": passed,
                            "failed": failed,
                            "response_batches": len(response_batches),
                            "evaluations": len(evaluation_executions),
                            "questionnaire_evaluator": questionnaire_evaluator,
                            "evaluation_benchmark_passed": sum(
                                bool(item.oracle and item.oracle.passed)
                                for item in evaluation_executions
                            ),
                            **experiment_context,
                        }
                    )
                    logger.info(
                        "[PIPELINE] Geração do cenário concluída",
                        **_progress_fields(
                            scenario_id=scenario_id,
                            stage="scenario",
                            locus=NodeLocus.SYSTEM,
                            status="completed",
                            duration_ms=_elapsed_ms(pipeline_started),
                            questionnaire_count=len(executions),
                            passed=passed,
                            failed=failed,
                            response_batch_count=len(response_batches),
                            evaluation_count=len(evaluation_executions),
                        ),
                    )
                    return result
                except Exception:
                    stage_tag = _STAGE_TAGS[current_stage]
                    logger.exception(
                        f"[{stage_tag}] Falha durante a etapa {current_stage}",
                        **_progress_fields(
                            scenario_id=scenario_id,
                            stage=current_stage,
                            locus=current_locus,
                            status="failed",
                            duration_ms=_elapsed_ms(pipeline_started),
                        ),
                    )
                    raise

    @staticmethod
    def _benchmark_record(
        scenario_id: str,
        execution: QuestionnaireExecution,
        *,
        research_front: ResearchFront | None = None,
        experiment_profile: str | None = None,
    ) -> BenchmarkRecord:
        prompt = execution.coordinator_prompt
        job_node = f"{scenario_id}.job-description"
        coordinator_node = f"{scenario_id}.coordinator-prompts"
        questionnaire_node = f"{scenario_id}.questionnaire.{prompt.sequence:03d}"
        nodes: list[dict[str, Any]] = [
            {
                "node_id": job_node,
                "locus": "JOB",
                "depends_on": [],
                "output_ref": prompt.job_description_id,
            },
            {
                "node_id": coordinator_node,
                "locus": "COORDINATOR",
                "depends_on": [job_node],
                "output_ref": prompt.id,
            },
            {
                "node_id": questionnaire_node,
                "locus": "SUB-GER",
                "depends_on": [coordinator_node],
                "status": execution.status.value,
            },
        ]
        annotation = failure_annotation(execution)
        failure_module = annotation.module.value if annotation else None
        failure_type = annotation.error_type.value if annotation else None
        failure_step = annotation.step_index if annotation else None
        trajectory_steps = (
            execution.agent_debug_trajectory.steps if execution.agent_debug_trajectory else []
        )
        step_node_ids = {
            step.index: f"{questionnaire_node}.step.{step.index:02d}" for step in trajectory_steps
        }
        previous_node = questionnaire_node
        step_edges: list[dict[str, str]] = []
        for step in trajectory_steps:
            step_node_id = step_node_ids[step.index]
            nodes.append(
                {
                    "node_id": step_node_id,
                    "locus": "SUB-GER",
                    "depends_on": [previous_node],
                    "step": step.index,
                    "modules": [module.value for module in step.module_outputs],
                }
            )
            step_edges.append({"source": previous_node, "target": step_node_id})
            previous_node = step_node_id
        step_annotations = [
            {
                "step": step.index,
                "node_id": step_node_ids[step.index],
                "label": (
                    failure_type
                    if annotation and step.index == annotation.step_index
                    else "no_error"
                ),
                "module": (
                    failure_module if annotation and step.index == annotation.step_index else None
                ),
            }
            for step in trajectory_steps
        ]
        if not step_annotations:
            step_annotations = [
                {
                    "step": failure_step or 1,
                    "node_id": questionnaire_node,
                    "label": failure_type or "no_error",
                    "module": failure_module,
                }
            ]
        outcome = {
            "status": execution.status.value,
            "expected_action": prompt.expected_action.value,
            "benchmark_passed": execution.benchmark_passed,
            "failure_reason": execution.failure_reason,
            "failure_code": annotation.code.value if annotation else None,
            "failure_step": failure_step,
            "failure_module": failure_module,
            "failure_type": failure_type,
            "question_count": len(execution.questionnaire.questions)
            if execution.questionnaire
            else 0,
        }
        return BenchmarkRecord(
            trajectory_id=execution.trajectory_id,
            LLM=configured_model_identifier(),
            critical_failure_step=failure_step,
            critical_failure_module=failure_module,
            critical_failure_type=failure_type,
            step_annotations=step_annotations,
            failure_annotation=annotation,
            provenance={
                "scenario_id": scenario_id,
                "source": "scenario-emulator/front-a",
                "research_targets": _research_targets(research_front),
                **_experiment_provenance(research_front, experiment_profile),
                "coordinator_prompt_id": prompt.id,
                "prompt_intent": prompt.intent.value,
                "prompt_category": prompt.category.value,
                "langfuse_trace_id": execution.trace_id,
            },
            graph={
                "nodes": nodes,
                "edges": [
                    {"source": job_node, "target": coordinator_node},
                    {"source": coordinator_node, "target": questionnaire_node},
                    *step_edges,
                ],
                "topological_order": [
                    job_node,
                    coordinator_node,
                    questionnaire_node,
                    *[step_node_ids[step.index] for step in trajectory_steps],
                ],
            },
            critical_repair_set=(
                [step_node_ids.get(failure_step, questionnaire_node)] if failure_step else []
            ),
            node_annotations=[
                {
                    "node_id": step_node_ids.get(failure_step, questionnaire_node),
                    "expected": prompt.expected_action.value,
                    "observed": execution.status.value,
                    "label": failure_type or "no_error",
                    "module": failure_module,
                }
            ],
            outcome=outcome,
            cascade=[],
        )

    @staticmethod
    def _response_benchmark_record(
        scenario_id: str,
        execution: QuestionnaireExecution,
        batch: ResponseGenerationBatch,
        *,
        research_front: ResearchFront | None = None,
        experiment_profile: str | None = None,
    ) -> BenchmarkRecord:
        sequence = execution.coordinator_prompt.sequence
        questionnaire_node = f"{scenario_id}.questionnaire.{sequence:03d}"
        response_node = f"{scenario_id}.responses.{batch.questionnaire_id}"
        annotation = response_failure_annotation(batch)
        failed = annotation is not None
        failure_module = annotation.module.value if annotation else None
        failure_type = annotation.error_type.value if annotation else None
        return BenchmarkRecord(
            trajectory_id=batch.batch_id,
            LLM=configured_model_identifier("response_generator"),
            task_type="response_generation",
            critical_failure_step=annotation.step_index if annotation else None,
            critical_failure_module=failure_module,
            critical_failure_type=failure_type,
            step_annotations=[
                {
                    "step": 1,
                    "node_id": response_node,
                    "label": failure_type or "no_error",
                    "module": failure_module,
                },
            ],
            failure_annotation=annotation,
            provenance={
                "scenario_id": scenario_id,
                "source": "scenario-emulator/response-generator",
                **_experiment_provenance(research_front, experiment_profile),
                "questionnaire_id": batch.questionnaire_id,
                "langfuse_trace_id": batch.trace_id,
            },
            graph={
                "nodes": [
                    {"node_id": questionnaire_node, "locus": "SUB-GER"},
                    {
                        "node_id": response_node,
                        "locus": "ATTACK-GEN",
                        "status": batch.status.value,
                    },
                ],
                "edges": [{"source": questionnaire_node, "target": response_node}],
                "topological_order": [questionnaire_node, response_node],
            },
            critical_repair_set=[response_node] if failed else [],
            node_annotations=[
                {
                    "node_id": response_node,
                    "expected": "succeeded",
                    "observed": batch.status.value,
                    "label": failure_type or "no_error",
                    "module": failure_module,
                }
            ],
            outcome={
                "status": batch.status.value,
                "case_count": len(batch.cases),
                "failure_reason": batch.failure_reason,
                "failure_code": annotation.code.value if annotation else None,
                "failure_module": failure_module,
                "failure_type": failure_type,
            },
            cascade=[],
        )

    @staticmethod
    def _evaluation_benchmark_record(
        scenario_id: str,
        questionnaire_execution: QuestionnaireExecution,
        batch: ResponseGenerationBatch,
        evaluation: EvaluationExecution,
        *,
        research_front: ResearchFront | None = None,
        experiment_profile: str | None = None,
    ) -> BenchmarkRecord:
        sequence = questionnaire_execution.coordinator_prompt.sequence
        questionnaire_node = f"{scenario_id}.questionnaire.{sequence:03d}"
        response_node = f"{scenario_id}.responses.{batch.questionnaire_id}"
        evaluator_node = f"{scenario_id}.evaluation.{evaluation.submission.submission_id}"
        oracle_node = f"{evaluator_node}.oracle"
        annotation = evaluation_failure_annotation(evaluation)
        failed = annotation is not None
        failure_module = annotation.module.value if annotation else None
        failure_type = annotation.error_type.value if annotation else None
        critical_step = annotation.step_index if annotation else None
        case = evaluation.response_case
        return BenchmarkRecord(
            trajectory_id=evaluation.trajectory_id,
            LLM=configured_model_identifier("evaluator"),
            task_type="questionnaire_evaluation",
            critical_failure_step=critical_step,
            critical_failure_module=failure_module,
            critical_failure_type=failure_type,
            step_annotations=[
                {
                    "step": 1,
                    "node_id": evaluator_node,
                    "label": failure_type or "no_error",
                    "module": failure_module,
                },
            ],
            failure_annotation=annotation,
            provenance={
                "scenario_id": scenario_id,
                "source": "scenario-emulator/evaluator",
                **_experiment_provenance(research_front, experiment_profile),
                "questionnaire_id": evaluation.questionnaire_id,
                "submission_id": evaluation.submission.submission_id,
                "response_case_id": case.case_id if case else None,
                "response_intent": case.intent.value if case else None,
                "attack_category": case.category.value if case else None,
                "langfuse_trace_id": evaluation.trace_id,
            },
            graph={
                "nodes": [
                    {"node_id": questionnaire_node, "locus": "SUB-GER"},
                    {"node_id": response_node, "locus": "ATTACK-GEN"},
                    {
                        "node_id": evaluator_node,
                        "locus": "SUB-EVAL",
                        "status": evaluation.status.value,
                    },
                    {"node_id": oracle_node, "locus": "ORACLE"},
                ],
                "edges": [
                    {"source": questionnaire_node, "target": response_node},
                    {"source": response_node, "target": evaluator_node},
                    {"source": evaluator_node, "target": oracle_node},
                ],
                "topological_order": [
                    questionnaire_node,
                    response_node,
                    evaluator_node,
                    oracle_node,
                ],
            },
            critical_repair_set=[evaluator_node] if failed else [],
            node_annotations=[
                {
                    "node_id": oracle_node if evaluation.oracle is not None else evaluator_node,
                    "expected": "safe_evaluation",
                    "observed": evaluation.status.value,
                    "label": failure_type or "no_error",
                    "module": failure_module,
                }
            ],
            outcome={
                "status": evaluation.status.value,
                "benchmark_passed": not failed,
                "score": evaluation.result.valor if evaluation.result else None,
                "oracle": evaluation.oracle.model_dump(mode="json") if evaluation.oracle else None,
                "failure_reason": evaluation.failure_reason,
                "failure_code": annotation.code.value if annotation else None,
                "failure_module": failure_module,
                "failure_type": failure_type,
            },
            cascade=[],
        )
