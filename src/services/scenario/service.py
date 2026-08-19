from __future__ import annotations

import uuid
from typing import Any

from src.agents.model import configured_model_identifier
from src.schemas.benchmark.schema import BenchmarkRecord
from src.schemas.observability.schema import NodeLocus, TraceNode
from src.schemas.questionnaire.schema import ExecutionStatus, QuestionnaireExecution
from src.schemas.scenario.schema import ScenarioRun
from src.services.coordinator_prompt.service import CoordinatorPromptService
from src.services.job_description.service import JobDescriptionService
from src.services.observability.service import observation, trace_attributes
from src.services.questionnaire.service import QuestionnaireService


class ScenarioService:
    def __init__(
        self,
        *,
        job_service: JobDescriptionService | None = None,
        coordinator_service: CoordinatorPromptService | None = None,
        questionnaire_service: QuestionnaireService | None = None,
    ) -> None:
        self.job_service = job_service or JobDescriptionService()
        self.coordinator_service = coordinator_service or CoordinatorPromptService()
        self.questionnaire_service = questionnaire_service or QuestionnaireService()

    async def run(
        self,
        brief: str,
        *,
        benign_count: int = 3,
        malicious_count: int = 3,
    ) -> ScenarioRun:
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
                records = [
                    self._benchmark_record(scenario_id, execution) for execution in executions
                ]
                result = ScenarioRun(
                    scenario_id=scenario_id,
                    job_description=job,
                    coordinator_prompts=prompts,
                    executions=executions,
                    benchmark_records=records,
                )
                root_span.update(
                    output={
                        "scenario_id": scenario_id,
                        "job_description_id": job.id,
                        "commands": len(prompts.prompts),
                        "passed": sum(item.benchmark_passed for item in executions),
                        "failed": sum(not item.benchmark_passed for item in executions),
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
