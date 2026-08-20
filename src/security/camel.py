from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

from agno.agent import Agent
from pydantic import BaseModel

from src.agents.model import ModelRole, build_model
from src.agents.utils import parse_model_output

T = TypeVar("T")
SchemaT = TypeVar("SchemaT", bound=BaseModel)


class DataOrigin(str, Enum):
    """Origens que participam das decisões de integridade dos dois fluxos."""

    TRUSTED_CONTROL = "trusted_control"
    UNTRUSTED_JOB = "untrusted_job"
    UNTRUSTED_COORDINATOR = "untrusted_coordinator"
    UNTRUSTED_ANSWER = "untrusted_answer"
    MODEL_DERIVED = "model_derived"


@dataclass(frozen=True)
class Provenance:
    origins: frozenset[DataOrigin]
    stages: tuple[str, ...] = ()

    @classmethod
    def from_origin(cls, origin: DataOrigin) -> Provenance:
        return cls(origins=frozenset({origin}))

    def derived(self, stage: str, *others: Provenance) -> Provenance:
        origins = set(self.origins)
        stages = list(self.stages)
        for other in others:
            origins.update(other.origins)
            stages.extend(other.stages)
        stages.append(stage)
        return Provenance(origins=frozenset(origins | {DataOrigin.MODEL_DERIVED}), stages=tuple(stages))

    def passed_through(self, required_stages: tuple[str, ...]) -> bool:
        position = 0
        for stage in self.stages:
            if position < len(required_stages) and stage == required_stages[position]:
                position += 1
        return position == len(required_stages)


@dataclass(frozen=True)
class ProtectedValue(Generic[T]):
    value: T
    provenance: Provenance

    def derived(self, value: SchemaT, stage: str, *others: ProtectedValue[Any]) -> ProtectedValue[SchemaT]:
        return ProtectedValue(
            value=value,
            provenance=self.provenance.derived(stage, *(item.provenance for item in others)),
        )


class PolicyDeniedError(ValueError):
    """Uma saída não pode atravessar o sink privilegiado solicitado."""


class UnsafeContentError(PolicyDeniedError):
    """A entrada ou saída contém conteúdo adversarial ou proibido."""


class QuarantinedLLM:
    """Executa uma única transformação tipada, sem tools, memória ou fluxo agentic."""

    async def run(
        self,
        *,
        name: str,
        role: ModelRole,
        system: str,
        user: str,
        output_schema: type[SchemaT],
    ) -> SchemaT:
        agent = Agent(
            name=name,
            model=build_model(role),
            description=system,
            output_schema=output_schema,
            tools=[],
        )
        response = await agent.arun(user)
        return parse_model_output(response.content, output_schema)
