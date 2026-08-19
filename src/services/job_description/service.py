from __future__ import annotations

from agno.agent import Agent

from src.agents.model import build_model, get_model_identifier
from src.agents.utils import extract_usage, parse_model_output
from src.prompts.manager import resolve_prompt
from src.prompts.raw_prompts import JOB_DESCRIPTION_SYSTEM_PROMPT, JOB_DESCRIPTION_USER_PROMPT
from src.schemas.job_description.schema import JobDescription, JobDescriptionDraft
from src.schemas.observability.schema import NodeLocus, TraceNode
from src.services.observability.service import observation


class JobDescriptionService:
    async def generate(self, brief: str, *, scenario_id: str) -> JobDescription:
        brief = brief.strip()
        if not brief:
            raise ValueError("O briefing da vaga não pode ser vazio.")

        system = resolve_prompt("front-a/job-description/system", JOB_DESCRIPTION_SYSTEM_PROMPT)
        user = resolve_prompt(
            "front-a/job-description/user",
            JOB_DESCRIPTION_USER_PROMPT,
            variables={"brief": brief},
        )
        model = build_model()
        node_id = f"{scenario_id}.job-description"
        agent_node = TraceNode(
            node_id=node_id,
            locus=NodeLocus.JOB,
            depends_on=[scenario_id],
            name="job-description-agent",
        )
        generation_node = TraceNode(
            node_id=f"{node_id}.generation",
            locus=NodeLocus.JOB,
            depends_on=[node_id],
            name="job-description-generation",
        )

        with observation(agent_node, as_type="agent", input={"brief": brief}) as agent_span:
            agent = Agent(
                name="job_description_agent",
                model=model,
                description=system.content,
                output_schema=JobDescriptionDraft,
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
                draft = parse_model_output(response.content, JobDescriptionDraft)
                update: dict = {"output": draft.model_dump(mode="json")}
                usage = extract_usage(response)
                if usage:
                    update["usage_details"] = usage
                generation.update(**update)

            result = JobDescription(**draft.model_dump(), source_brief=brief)
            agent_span.update(output=result.model_dump(mode="json"))
            return result
