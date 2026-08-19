from __future__ import annotations

import json

from agno.agent import Agent

from src.agents.model import build_model, get_model_identifier
from src.agents.utils import extract_usage, parse_model_output
from src.prompts.manager import resolve_prompt
from src.prompts.raw_prompts import (
    COORDINATOR_PROMPT_SYSTEM_PROMPT,
    COORDINATOR_PROMPT_USER_PROMPT,
)
from src.schemas.coordinator_prompt.schema import (
    CoordinatorPrompt,
    CoordinatorPromptBatch,
    GeneratedCoordinatorPromptBatch,
    PromptIntent,
)
from src.schemas.job_description.schema import JobDescription
from src.schemas.observability.schema import NodeLocus, TraceNode
from src.services.observability.service import observation

_MAX_PROMPTS_PER_BATCH = 100


class CoordinatorPromptService:
    async def generate(
        self,
        job: JobDescription,
        *,
        benign_count: int,
        malicious_count: int,
        scenario_id: str,
    ) -> CoordinatorPromptBatch:
        if benign_count < 0 or malicious_count < 0 or benign_count + malicious_count < 1:
            raise ValueError("Informe ao menos um comando; contagens não podem ser negativas.")
        if benign_count + malicious_count > _MAX_PROMPTS_PER_BATCH:
            raise ValueError(f"O lote aceita no máximo {_MAX_PROMPTS_PER_BATCH} comandos.")

        system = resolve_prompt("front-a/coordinator/system", COORDINATOR_PROMPT_SYSTEM_PROMPT)
        user = resolve_prompt(
            "front-a/coordinator/user",
            COORDINATOR_PROMPT_USER_PROMPT,
            variables={
                "job_description": json.dumps(
                    job.model_dump(mode="json", exclude={"source_brief"}),
                    ensure_ascii=False,
                    indent=2,
                ),
                "benign_count": benign_count,
                "malicious_count": malicious_count,
            },
        )
        model = build_model()
        node_id = f"{scenario_id}.coordinator-prompts"
        agent_node = TraceNode(
            node_id=node_id,
            locus=NodeLocus.COORDINATOR,
            depends_on=[f"{scenario_id}.job-description"],
            name="coordinator-prompt-agent",
        )
        generation_node = TraceNode(
            node_id=f"{node_id}.generation",
            locus=NodeLocus.COORDINATOR,
            depends_on=[node_id],
            name="coordinator-prompt-generation",
        )

        with observation(
            agent_node,
            as_type="agent",
            input={
                "job_description_id": job.id,
                "benign": benign_count,
                "malicious": malicious_count,
            },
        ) as agent_span:
            agent = Agent(
                name="coordinator_prompt_agent",
                model=model,
                description=system.content,
                output_schema=GeneratedCoordinatorPromptBatch,
            )
            with observation(
                generation_node,
                as_type="generation",
                input={"system": system.content, "user": user.content},
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
                generated = parse_model_output(response.content, GeneratedCoordinatorPromptBatch)
                update: dict = {"output": generated.model_dump(mode="json")}
                usage = extract_usage(response)
                if usage:
                    update["usage_details"] = usage
                generation.update(**update)

            benign = sum(p.intent is PromptIntent.BENIGN for p in generated.prompts)
            malicious = sum(p.intent is PromptIntent.MALICIOUS for p in generated.prompts)
            if (benign, malicious) != (benign_count, malicious_count):
                raise ValueError(
                    "O CoordinatorPromptAgent não respeitou as contagens: "
                    f"esperado=({benign_count}, {malicious_count}), recebido=({benign}, {malicious})."
                )

            prompts = [
                CoordinatorPrompt(
                    **item.model_dump(),
                    job_description_id=job.id,
                    sequence=index,
                )
                for index, item in enumerate(generated.prompts, start=1)
            ]
            result = CoordinatorPromptBatch(job_description_id=job.id, prompts=prompts)
            agent_span.update(output=result.model_dump(mode="json"))
            return result
