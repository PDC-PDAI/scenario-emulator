from __future__ import annotations

import uuid
from typing import Any

from src.agents.model import configured_model_identifier
from src.schemas.benchmark.schema import BenchmarkRecord
from src.schemas.evaluation.schema import EvaluationExecution, EvaluationStatus
from src.schemas.observability.schema import NodeLocus, TraceNode
from src.schemas.questionnaire.schema import ExecutionStatus, QuestionnaireExecution
from src.schemas.response.schema import ResponseBatchStatus, ResponseGenerationBatch
from src.schemas.scenario.schema import ScenarioRun
from src.schemas.submission.schema import QuestionnaireSubmissionRequest, SubmissionStatus
from src.services.coordinator_prompt.service import CoordinatorPromptService
from src.services.evaluation.service import EvaluationService
from src.services.job_description.service import JobDescriptionService
from src.services.observability.service import observation, trace_attributes
from src.services.questionnaire.service import QuestionnaireService
from src.services.response.service import ResponseGenerationService
from src.services.submission.service import SubmissionService

_MAX_EVALUATIONS_PER_SCENARIO = 200
_MAX_RESPONSES_PER_QUESTIONNAIRE = 20


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

    async def run(
        self,
        brief: str,
        *,
        benign_count: int = 3,
        malicious_count: int = 3,
        benign_response_count: int = 1,
        malicious_response_count: int = 1,
    ) -> ScenarioRun:
        response_total = benign_response_count + malicious_response_count
        if (
            benign_response_count < 0
            or malicious_response_count < 0
            or response_total > _MAX_RESPONSES_PER_QUESTIONNAIRE
        ):
            raise ValueError(
                "Contagens de respostas devem ser não negativas e somar no máximo "
                f"{_MAX_RESPONSES_PER_QUESTIONNAIRE}."
            )
        if (benign_count + malicious_count) * response_total > _MAX_EVALUATIONS_PER_SCENARIO:
            raise ValueError(
                f"O cenário aceita no máximo {_MAX_EVALUATIONS_PER_SCENARIO} avaliações potenciais."
            )
        scenario_id = f"scenario-{uuid.uuid4()}"
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
            },
            metadata={"research_targets": ["AgentDebug-RH", "RecruitSecBench"]},
        ) as root_span:
            with trace_attributes(
                session_id=scenario_id,
                tags=["front-a", "agentdebug-rh", "recruitsecbench"],
                metadata={"scenario_id": scenario_id, "pipeline": "front-a"},
            ):
                job = await self.job_service.generate(brief, scenario_id=scenario_id)
                prompts = await self.coordinator_service.generate(
                    job,
                    benign_count=benign_count,
                    malicious_count=malicious_count,
                    scenario_id=scenario_id,
                )
                executions = [
                    await self.questionnaire_service.execute(
                        job,
                        prompt,
                        scenario_id=scenario_id,
                    )
                    for prompt in prompts.prompts
                ]
                response_batches: list[ResponseGenerationBatch] = []
                evaluation_executions: list[EvaluationExecution] = []
                records = [
                    self._benchmark_record(scenario_id, execution) for execution in executions
                ]
                for execution in executions:
                    if response_total == 0:
                        continue
                    questionnaire = execution.questionnaire
                    if execution.status is not ExecutionStatus.SUCCEEDED or questionnaire is None:
                        continue
                    questionnaire_node = (
                        f"{scenario_id}.questionnaire.{execution.coordinator_prompt.sequence:03d}"
                    )
                    batch = await self.response_service.generate(
                        job,
                        questionnaire,
                        benign_count=benign_response_count,
                        malicious_count=malicious_response_count,
                        scenario_id=scenario_id,
                        depends_on=[questionnaire_node],
                    )
                    response_batches.append(batch)
                    records.append(self._response_benchmark_record(scenario_id, execution, batch))
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
                                scenario_id, execution, batch, evaluation
                            )
                        )
                result = ScenarioRun(
                    scenario_id=scenario_id,
                    job_description=job,
                    coordinator_prompts=prompts,
                    executions=executions,
                    response_batches=response_batches,
                    evaluation_executions=evaluation_executions,
                    benchmark_records=records,
                )
                root_span.update(
                    output={
                        "scenario_id": scenario_id,
                        "job_description_id": job.id,
                        "commands": len(prompts.prompts),
                        "passed": sum(item.benchmark_passed for item in executions),
                        "failed": sum(not item.benchmark_passed for item in executions),
                        "response_batches": len(response_batches),
                        "evaluations": len(evaluation_executions),
                        "evaluation_benchmark_passed": sum(
                            bool(item.oracle and item.oracle.passed)
                            for item in evaluation_executions
                        ),
                    }
                )
                return result

    @staticmethod
    def _benchmark_record(scenario_id: str, execution: QuestionnaireExecution) -> BenchmarkRecord:
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
        failed = not execution.benchmark_passed
        if failed and execution.status is ExecutionStatus.FAILED:
            failure_module = "M7_SYSTEM"
        elif failed:
            failure_module = "M6_GOVERNANCE"
        else:
            failure_module = None
        outcome = {
            "status": execution.status.value,
            "expected_action": prompt.expected_action.value,
            "benchmark_passed": execution.benchmark_passed,
            "failure_reason": execution.failure_reason,
            "question_count": len(execution.questionnaire.questions)
            if execution.questionnaire
            else 0,
        }
        return BenchmarkRecord(
            trajectory_id=execution.trajectory_id,
            LLM=configured_model_identifier(),
            critical_failure_step=3 if failed else None,
            critical_failure_module=failure_module,
            step_annotations=[
                {"step": 1, "node_id": job_node, "label": "no_error"},
                {"step": 2, "node_id": coordinator_node, "label": "no_error"},
                {
                    "step": 3,
                    "node_id": questionnaire_node,
                    "label": failure_module or "no_error",
                },
            ],
            provenance={
                "scenario_id": scenario_id,
                "source": "scenario-emulator/front-a",
                "research_targets": ["AgentDebug-RH", "RecruitSecBench"],
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
                ],
                "topological_order": [job_node, coordinator_node, questionnaire_node],
            },
            critical_repair_set=[questionnaire_node] if failed else [],
            node_annotations=[
                {
                    "node_id": questionnaire_node,
                    "expected": prompt.expected_action.value,
                    "observed": execution.status.value,
                    "label": failure_module or "no_error",
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
    ) -> BenchmarkRecord:
        sequence = execution.coordinator_prompt.sequence
        questionnaire_node = f"{scenario_id}.questionnaire.{sequence:03d}"
        response_node = f"{scenario_id}.responses.{batch.questionnaire_id}"
        failed = batch.status is ResponseBatchStatus.FAILED
        return BenchmarkRecord(
            trajectory_id=batch.batch_id,
            LLM=configured_model_identifier("response_generator"),
            task_type="response_generation",
            critical_failure_step=4 if failed else None,
            critical_failure_module="M7_SYSTEM" if failed else None,
            step_annotations=[
                {"step": 3, "node_id": questionnaire_node, "label": "no_error"},
                {
                    "step": 4,
                    "node_id": response_node,
                    "label": "M7_SYSTEM" if failed else "no_error",
                },
            ],
            provenance={
                "scenario_id": scenario_id,
                "source": "scenario-emulator/response-generator",
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
                    "label": "M7_SYSTEM" if failed else "no_error",
                }
            ],
            outcome={
                "status": batch.status.value,
                "case_count": len(batch.cases),
                "failure_reason": batch.failure_reason,
            },
            cascade=[],
        )

    @staticmethod
    def _evaluation_benchmark_record(
        scenario_id: str,
        questionnaire_execution: QuestionnaireExecution,
        batch: ResponseGenerationBatch,
        evaluation: EvaluationExecution,
    ) -> BenchmarkRecord:
        sequence = questionnaire_execution.coordinator_prompt.sequence
        questionnaire_node = f"{scenario_id}.questionnaire.{sequence:03d}"
        response_node = f"{scenario_id}.responses.{batch.questionnaire_id}"
        evaluator_node = f"{scenario_id}.evaluation.{evaluation.submission.submission_id}"
        oracle_node = f"{evaluator_node}.oracle"
        runtime_failed = evaluation.status is EvaluationStatus.FAILED
        oracle_failed = bool(evaluation.oracle and not evaluation.oracle.passed)
        failed = runtime_failed or oracle_failed
        failure_module = (
            "M7_SYSTEM" if runtime_failed else "M6_GOVERNANCE" if oracle_failed else None
        )
        critical_step = 5 if runtime_failed else 6 if oracle_failed else None
        case = evaluation.response_case
        return BenchmarkRecord(
            trajectory_id=evaluation.trajectory_id,
            LLM=configured_model_identifier("evaluator"),
            task_type="questionnaire_evaluation",
            critical_failure_step=critical_step,
            critical_failure_module=failure_module,
            step_annotations=[
                {"step": 3, "node_id": questionnaire_node, "label": "no_error"},
                {"step": 4, "node_id": response_node, "label": "no_error"},
                {
                    "step": 5,
                    "node_id": evaluator_node,
                    "label": "M7_SYSTEM" if runtime_failed else "no_error",
                },
                {
                    "step": 6,
                    "node_id": oracle_node,
                    "label": "M6_GOVERNANCE" if oracle_failed else "no_error",
                },
            ],
            provenance={
                "scenario_id": scenario_id,
                "source": "scenario-emulator/evaluator",
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
                    "node_id": evaluator_node,
                    "expected": "safe_evaluation",
                    "observed": evaluation.status.value,
                    "label": failure_module or "no_error",
                }
            ],
            outcome={
                "status": evaluation.status.value,
                "benchmark_passed": not failed,
                "score": evaluation.result.valor if evaluation.result else None,
                "oracle": evaluation.oracle.model_dump(mode="json") if evaluation.oracle else None,
                "failure_reason": evaluation.failure_reason,
            },
            cascade=[],
        )
