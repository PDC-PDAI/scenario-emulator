"""Contrato de execução consumido pelo AgentDebug-RH.

``AgentDebugTrajectory`` e ``AgentDebugTrajectoryStep`` espelham o schema
``Trajectory`` da referência em ``.references/agentdebug-rh-*``. Manter essa
fronteira explícita evita que o consumidor precise reconstruir steps a partir
de spans do Langfuse ou dos campos agregados do benchmark.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


def serialize_module_output(value: Any) -> str:
    """Serializa um output de módulo sem produzir JSON parcial."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def raw_output_envelope(planning: str, action: Any) -> str:
    """Reúne planning e action em um envelope JSON completo e válido."""
    return serialize_module_output({"planning": planning, "action": action})


class ErrorModule(str, Enum):
    MEMORY = "memory"
    REFLECTION = "reflection"
    PLANNING = "planning"
    ACTION = "action"
    SYSTEM = "system"
    OTHERS = "others"


class ErrorType(str, Enum):
    OVER_SIMPLIFICATION = "over_simplification"
    MEMORY_RETRIEVAL_FAILURE = "memory_retrieval_failure"
    HALLUCINATION = "hallucination"
    PROGRESS_MISJUDGE = "progress_misjudge"
    OUTCOME_MISINTERPRETATION = "outcome_misinterpretation"
    CAUSAL_MISATTRIBUTION = "causal_misattribution"
    CONSTRAINT_IGNORANCE = "constraint_ignorance"
    IMPOSSIBLE_ACTION = "impossible_action"
    INEFFICIENT_PLAN = "inefficient_plan"
    MISALIGNMENT = "misalignment"
    INVALID_ACTION = "invalid_action"
    FORMAT_ERROR = "format_error"
    PARAMETER_ERROR = "parameter_error"
    STEP_LIMIT = "step_limit"
    TOOL_EXECUTION_ERROR = "tool_execution_error"
    LLM_LIMIT = "llm_limit"
    ENVIRONMENT_ERROR = "environment_error"
    OTHERS = "others"


_VALID_ERROR_TYPES: dict[ErrorModule, frozenset[ErrorType]] = {
    ErrorModule.MEMORY: frozenset(
        {
            ErrorType.OVER_SIMPLIFICATION,
            ErrorType.MEMORY_RETRIEVAL_FAILURE,
            ErrorType.HALLUCINATION,
        }
    ),
    ErrorModule.REFLECTION: frozenset(
        {
            ErrorType.PROGRESS_MISJUDGE,
            ErrorType.OUTCOME_MISINTERPRETATION,
            ErrorType.CAUSAL_MISATTRIBUTION,
            ErrorType.HALLUCINATION,
        }
    ),
    ErrorModule.PLANNING: frozenset(
        {
            ErrorType.CONSTRAINT_IGNORANCE,
            ErrorType.IMPOSSIBLE_ACTION,
            ErrorType.INEFFICIENT_PLAN,
        }
    ),
    ErrorModule.ACTION: frozenset(
        {
            ErrorType.MISALIGNMENT,
            ErrorType.INVALID_ACTION,
            ErrorType.FORMAT_ERROR,
            ErrorType.PARAMETER_ERROR,
        }
    ),
    ErrorModule.SYSTEM: frozenset(
        {
            ErrorType.STEP_LIMIT,
            ErrorType.TOOL_EXECUTION_ERROR,
            ErrorType.LLM_LIMIT,
            ErrorType.ENVIRONMENT_ERROR,
        }
    ),
    ErrorModule.OTHERS: frozenset({ErrorType.OTHERS}),
}


def is_valid_error_pair(module: ErrorModule, error_type: ErrorType) -> bool:
    """Informa se o par pertence à taxonomia compartilhada com o AgentDebug-RH."""
    return error_type in _VALID_ERROR_TYPES[module]


class ChatToolCall(BaseModel):
    """Uma tool call emitida em uma mensagem ``assistant`` da conversa crua."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    name: str | None = None
    arguments: Any = None


class ChatMessage(BaseModel):
    """Uma mensagem crua da conversa com o modelo (system/user/assistant/tool)."""

    model_config = ConfigDict(extra="forbid")

    role: str
    content: Any = None
    tool_calls: list[ChatToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    reasoning: str | None = None
    error: bool | None = None


class AgentDebugTrajectoryStep(BaseModel):
    """Um ciclo de decisão 1-indexado da execução modular do agente.

    Um step começa com o contexto disponível ao agente, reúne os outputs dos
    módulos que participaram da decisão e termina com a resposta do ambiente à
    ação escolhida. Portanto, uma tool call não cria vários steps para
    planejamento, chamada e resultado: esses eventos pertencem ao mesmo step.

    ``raw_output`` preserva a saída observável completa da qual
    ``module_outputs`` foi extraído. Quando o SDK entrega planning e tool call
    em eventos separados, o produtor serializa ambos em um único envelope para
    auditoria, sem inventar memory ou reflection.
    """

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1)
    module_outputs: dict[ErrorModule, str] = Field(default_factory=dict)
    step_input: str = ""
    env_response: str = ""
    raw_output: str = ""


class AgentDebugTrajectory(BaseModel):
    """Execução autocontida, pronta para ``Trajectory.model_validate``.

    Os módulos analisáveis são identificados pelas chaves não vazias de
    ``module_outputs`` em cada step. Os nomes dessas chaves pertencem à
    taxonomia compartilhada com o AgentDebug-RH.
    """

    model_config = ConfigDict(extra="forbid")

    trajectory_id: str
    task_description: str
    environment: str
    success: bool = False
    steps: list[AgentDebugTrajectoryStep] = Field(default_factory=list)
    # Conversa do run na ordem exata enviada ao modelo, em contrato NORMALIZADO
    # (ver `clean_agent_messages`: ordem/papéis/conteúdo/tool_call_ids verbatim;
    # argumentos parseados e metadados internos do provider removidos). O
    # AgentDebug-RH ignora o campo; a Frente C usa como prefixo do re-rollout.
    # None = execução legada/adaptada sem captura nativa.
    messages: list[ChatMessage] | None = None

    @model_validator(mode="after")
    def validate_step_indices(self) -> AgentDebugTrajectory:
        indices = [step.index for step in self.steps]
        expected = list(range(1, len(self.steps) + 1))
        if indices != expected:
            raise ValueError(f"steps devem ser contíguos e 1-indexados; recebido {indices}")
        return self


class FailureCode(str, Enum):
    """Códigos determinísticos observados pelo oráculo do emulador."""

    SECURITY_POLICY_VIOLATION = "SECURITY_POLICY_VIOLATION"
    OVER_REFUSAL = "OVER_REFUSAL"
    AGENT_RUNTIME_ERROR = "AGENT_RUNTIME_ERROR"
    SAVE_FAILED = "SAVE_FAILED"
    TOOL_PARAMETER_ERROR = "TOOL_PARAMETER_ERROR"
    MISSING_TERMINAL_TOOL_CALL = "MISSING_TERMINAL_TOOL_CALL"
    UNEXPECTED_OUTCOME = "UNEXPECTED_OUTCOME"
    EVALUATION_RUNTIME_ERROR = "EVALUATION_RUNTIME_ERROR"
    EVALUATION_SECURITY_VIOLATION = "EVALUATION_SECURITY_VIOLATION"
    EVALUATION_SCORE_MISMATCH = "EVALUATION_SCORE_MISMATCH"
    EVALUATION_EVIDENCE_ERROR = "EVALUATION_EVIDENCE_ERROR"
    RESPONSE_GENERATION_ERROR = "RESPONSE_GENERATION_ERROR"


class FailureAnnotation(BaseModel):
    """Rótulo observado; não substitui o diagnóstico do AgentDebug-RH."""

    model_config = ConfigDict(extra="forbid")

    code: FailureCode
    step_index: int = Field(ge=1)
    module: ErrorModule
    error_type: ErrorType
    message: str
    evidence: str = ""
    retryable: bool = False

    @model_validator(mode="after")
    def validate_taxonomy_pair(self) -> FailureAnnotation:
        if not is_valid_error_pair(self.module, self.error_type):
            raise ValueError(
                f"error_type={self.error_type.value} não pertence ao módulo {self.module.value}"
            )
        return self
