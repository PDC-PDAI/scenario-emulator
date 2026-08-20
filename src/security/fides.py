from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Generic, TypeVar

from agno.agent import Agent
from pydantic import BaseModel

from src.agents.model import ModelRole, build_model, get_model_identifier
from src.agents.utils import extract_usage, parse_model_output
from src.prompts.manager import resolve_prompt


class IntegrityLabel(str, Enum):
    TRUSTED = "TRUSTED"
    UNTRUSTED = "UNTRUSTED"


class ConfidentialityLabel(str, Enum):
    PUBLIC = "PUBLIC"
    USER_IDENTITY = "USER_IDENTITY"
    PRIVATE = "PRIVATE"


class VariableType(str, Enum):
    JOB_DESCRIPTION = "job_description"
    COORDINATOR_COMMAND = "coordinator_command"
    QUESTIONNAIRE = "questionnaire"
    QUESTION = "question"
    ANSWER = "answer"
    SCORE = "score"


_CONFIDENTIALITY_RANK = {
    ConfidentialityLabel.PUBLIC: 0,
    ConfidentialityLabel.USER_IDENTITY: 1,
    ConfidentialityLabel.PRIVATE: 2,
}


@dataclass(frozen=True, slots=True)
class ContentLabel:
    integrity: IntegrityLabel
    confidentiality: ConfidentialityLabel


def combine_labels(*labels: ContentLabel) -> ContentLabel:
    """Propaga taint usando o elemento mais restritivo de cada dimensão."""
    if not labels:
        return ContentLabel(IntegrityLabel.TRUSTED, ConfidentialityLabel.PUBLIC)
    integrity = (
        IntegrityLabel.UNTRUSTED
        if any(label.integrity is IntegrityLabel.UNTRUSTED for label in labels)
        else IntegrityLabel.TRUSTED
    )
    confidentiality = max(
        (label.confidentiality for label in labels),
        key=_CONFIDENTIALITY_RANK.__getitem__,
    )
    return ContentLabel(integrity, confidentiality)


@dataclass(frozen=True, slots=True)
class VariableDescriptor:
    reference: str
    variable_type: VariableType
    label: ContentLabel

    def model_safe_dict(self) -> dict[str, str]:
        return {
            "reference": self.reference,
            "type": self.variable_type.value,
            "integrity": self.label.integrity.value,
            "confidentiality": self.label.confidentiality.value,
        }


@dataclass(frozen=True, slots=True)
class _StoredVariable:
    descriptor: VariableDescriptor
    value: Any = field(repr=False)


class FidesReferenceError(RuntimeError):
    """A referência não existe nesta execução ou possui tipo incompatível."""


class FidesPolicyDenied(RuntimeError):
    """O reference monitor negou explicitamente uma ação."""


class ContentVariableStore:
    """Store efêmero por execução; o conteúdo nunca aparece em descritores ou repr."""

    def __init__(self) -> None:
        self._variables: dict[str, _StoredVariable] = {}

    def put(
        self,
        value: Any,
        *,
        variable_type: VariableType,
        label: ContentLabel,
    ) -> VariableDescriptor:
        reference = f"var_{secrets.token_hex(16)}"
        descriptor = VariableDescriptor(reference, variable_type, label)
        self._variables[reference] = _StoredVariable(descriptor, value)
        return descriptor

    def describe(self, reference: str) -> VariableDescriptor:
        stored = self._variables.get(reference)
        if stored is None:
            raise FidesReferenceError("Referência FIDES inexistente ou pertencente a outra execução")
        return stored.descriptor

    def resolve(self, reference: str, *, expected_type: VariableType) -> Any:
        stored = self._variables.get(reference)
        if stored is None:
            raise FidesReferenceError("Referência FIDES inexistente ou pertencente a outra execução")
        if stored.descriptor.variable_type is not expected_type:
            raise FidesReferenceError(
                "Tipo de referência FIDES incompatível: "
                f"esperado={expected_type.value}, recebido={stored.descriptor.variable_type.value}"
            )
        return stored.value


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    argument_types: Mapping[str, VariableType]
    requires_trusted_control: bool = True
    accepts_untrusted_arguments: bool = True
    max_confidentiality: ConfidentialityLabel = ConfidentialityLabel.PUBLIC


@dataclass(frozen=True, slots=True)
class FidesAuditEvent:
    tool: str
    allowed: bool
    decision: str
    references: tuple[dict[str, str], ...]
    control_integrity: IntegrityLabel

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "allowed": self.allowed,
            "decision": self.decision,
            "references": list(self.references),
            "control_integrity": self.control_integrity.value,
        }


class FidesReferenceMonitor:
    """Aplica P-T/P-F antes de expandir qualquer referência para uma ação."""

    def __init__(self, store: ContentVariableStore) -> None:
        self.store = store
        self._audit: list[FidesAuditEvent] = []

    @property
    def audit_log(self) -> tuple[FidesAuditEvent, ...]:
        return tuple(self._audit)

    def sanitized_audit_log(self) -> list[dict[str, Any]]:
        return [event.as_dict() for event in self._audit]

    def authorize_and_resolve(
        self,
        *,
        tool: str,
        control: ContentLabel,
        references: Mapping[str, str],
        policy: ToolPolicy,
    ) -> dict[str, Any]:
        descriptors: list[tuple[str, VariableDescriptor]] = []
        try:
            if set(references) != set(policy.argument_types):
                raise FidesReferenceError("Argumentos de referência não correspondem à política")
            for argument, expected_type in policy.argument_types.items():
                reference = references[argument]
                if not isinstance(reference, str) or not reference.startswith("var_"):
                    raise FidesReferenceError("Ações FIDES aceitam somente referências opacas")
                descriptor = self.store.describe(reference)
                if descriptor.variable_type is not expected_type:
                    raise FidesReferenceError(
                        f"Tipo incompatível para {argument}: esperado={expected_type.value}, "
                        f"recebido={descriptor.variable_type.value}"
                    )
                descriptors.append((argument, descriptor))
        except FidesReferenceError as exc:
            self._record(tool, False, "REFERENCE_ERROR", descriptors, control)
            raise exc

        if policy.requires_trusted_control and control.integrity is not IntegrityLabel.TRUSTED:
            self._record(tool, False, "P-T_DENY_UNTRUSTED_CONTROL", descriptors, control)
            raise FidesPolicyDenied("P-T negou ação originada de controle não confiável")

        if not policy.accepts_untrusted_arguments and any(
            descriptor.label.integrity is IntegrityLabel.UNTRUSTED
            for _, descriptor in descriptors
        ):
            self._record(tool, False, "P-T_DENY_UNTRUSTED_ARGUMENT", descriptors, control)
            raise FidesPolicyDenied("P-T negou argumento não confiável")

        if any(
            _CONFIDENTIALITY_RANK[descriptor.label.confidentiality]
            > _CONFIDENTIALITY_RANK[policy.max_confidentiality]
            for _, descriptor in descriptors
        ):
            self._record(tool, False, "P-F_DENY_CONFIDENTIALITY", descriptors, control)
            raise FidesPolicyDenied("P-F negou fluxo de confidencialidade para o sink")

        self._record(tool, True, "ALLOW", descriptors, control)
        return {
            argument: self.store.resolve(
                descriptor.reference,
                expected_type=policy.argument_types[argument],
            )
            for argument, descriptor in descriptors
        }

    def _record(
        self,
        tool: str,
        allowed: bool,
        decision: str,
        descriptors: list[tuple[str, VariableDescriptor]],
        control: ContentLabel,
    ) -> None:
        references = tuple(
            {
                "argument": argument,
                **descriptor.model_safe_dict(),
            }
            for argument, descriptor in descriptors
        )
        self._audit.append(
            FidesAuditEvent(
                tool=tool,
                allowed=allowed,
                decision=decision,
                references=references,
                control_integrity=control.integrity,
            )
        )


T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class QuarantineRun(Generic[T]):
    reference: VariableDescriptor
    model: str
    usage: dict[str, int] | None = None


def _prompt_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, indent=2)


class FidesQuarantinedLLM:
    """Único componente autorizado a expandir dados não confiáveis para um LLM."""

    def __init__(
        self,
        *,
        model_factory: Callable[[ModelRole], Any] = build_model,
        agent_factory: Callable[..., Any] = Agent,
    ) -> None:
        self._model_factory = model_factory
        self._agent_factory = agent_factory

    async def run(
        self,
        *,
        monitor: FidesReferenceMonitor,
        control: ContentLabel,
        tool_name: str,
        references: Mapping[str, str],
        expected_types: Mapping[str, VariableType],
        system_prompt_key: str,
        system_prompt_fallback: str,
        user_prompt_key: str,
        user_prompt_fallback: str,
        output_schema: type[T],
        output_type: VariableType,
        model_role: ModelRole,
        trusted_variables: Mapping[str, Any] | None = None,
        system_variables: Mapping[str, Any] | None = None,
    ) -> QuarantineRun[T]:
        if output_schema is None:
            raise ValueError("Quarentena FIDES exige output_schema")
        values = monitor.authorize_and_resolve(
            tool=tool_name,
            control=control,
            references=references,
            policy=ToolPolicy(
                argument_types=expected_types,
                accepts_untrusted_arguments=True,
                max_confidentiality=ConfidentialityLabel.PRIVATE,
            ),
        )
        variables = {key: _prompt_value(value) for key, value in values.items()}
        variables.update(
            {key: _prompt_value(value) for key, value in (trusted_variables or {}).items()}
        )
        system = resolve_prompt(
            system_prompt_key,
            system_prompt_fallback,
            variables=dict(system_variables or {}),
        )
        user = resolve_prompt(
            user_prompt_key,
            user_prompt_fallback,
            variables=variables,
        )
        model = self._model_factory(model_role)
        agent = self._agent_factory(
            name=f"fides_quarantine_{output_type.value}",
            model=model,
            description=system.content,
            output_schema=output_schema,
            tools=[],
        )
        response = await agent.arun(user.content)
        parsed = parse_model_output(response.content, output_schema)
        input_labels = tuple(
            monitor.store.describe(reference).label for reference in references.values()
        )
        propagated = combine_labels(*input_labels)
        output_label = ContentLabel(
            IntegrityLabel.UNTRUSTED,
            propagated.confidentiality,
        )
        descriptor = monitor.store.put(
            parsed,
            variable_type=output_type,
            label=output_label,
        )
        return QuarantineRun(
            reference=descriptor,
            model=get_model_identifier(model),
            usage=extract_usage(response),
        )
