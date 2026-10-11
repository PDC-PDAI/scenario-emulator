"""Execution contract consumed by AgentDebug-RH.

``AgentDebugTrajectory`` and ``AgentDebugTrajectoryStep`` mirror the reference
``Trajectory`` schema in ``.references/agentdebug-rh-*``. Keeping this boundary
explicit spares the consumer from rebuilding steps out of Langfuse spans or the
aggregated benchmark fields.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


def serialize_module_output(value: Any) -> str:
    """Serialize a module output without producing partial JSON."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def raw_output_envelope(planning: str, action: Any) -> str:
    """Combine planning and action into one complete, valid JSON envelope."""
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
    """Return whether the pair belongs to the taxonomy shared with AgentDebug-RH."""
    return error_type in _VALID_ERROR_TYPES[module]


class ChatToolCall(BaseModel):
    """One tool call emitted in an ``assistant`` message of the raw conversation."""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    name: str | None = None
    arguments: Any = None


class ChatMessage(BaseModel):
    """One raw message of the model conversation (system/user/assistant/tool)."""

    model_config = ConfigDict(extra="forbid")

    role: str
    content: Any = None
    tool_calls: list[ChatToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    reasoning: str | None = None
    error: bool | None = None


class AgentDebugTrajectoryStep(BaseModel):
    """One 1-indexed decision cycle of the agent's modular execution.

    A step starts with the context available to the agent, gathers the outputs of
    the modules that took part in the decision, and ends with the environment's
    response to the chosen action. A tool call therefore does not create separate
    steps for planning, call and result: those events belong to the same step.

    ``raw_output`` preserves the complete observable output from which
    ``module_outputs`` was extracted. When the SDK delivers planning and the tool
    call as separate events, the producer serializes both into a single envelope
    for auditing, without inventing memory or reflection.
    """

    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=1)
    module_outputs: dict[ErrorModule, str] = Field(default_factory=dict)
    step_input: str = ""
    env_response: str = ""
    raw_output: str = ""


class AgentDebugTrajectory(BaseModel):
    """Self-contained execution, ready for ``Trajectory.model_validate``.

    Analyzable modules are identified by the non-empty ``module_outputs`` keys of
    each step. Those key names belong to the taxonomy shared with AgentDebug-RH.
    """

    model_config = ConfigDict(extra="forbid")

    trajectory_id: str
    task_description: str
    environment: str
    success: bool = False
    steps: list[AgentDebugTrajectoryStep] = Field(default_factory=list)
    # The run's conversation in the exact order sent to the model, in the NORMALIZED
    # contract (see `clean_agent_messages`: order/roles/content/tool_call_ids verbatim;
    # arguments parsed and internal provider metadata removed). AgentDebug-RH
    # ignores the field; Front C uses it as the re-rollout prefix.
    # None = legacy/adapted execution without native capture.
    messages: list[ChatMessage] | None = None

    @model_validator(mode="after")
    def validate_step_indices(self) -> AgentDebugTrajectory:
        indices = [step.index for step in self.steps]
        expected = list(range(1, len(self.steps) + 1))
        if indices != expected:
            raise ValueError(f"steps must be contiguous and 1-indexed; got {indices}")
        return self


class FailureCode(str, Enum):
    """Deterministic codes observed by the emulator oracle."""

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
    """Observed label; it does not replace the AgentDebug-RH diagnosis."""

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
                f"error_type={self.error_type.value} does not belong to module {self.module.value}"
            )
        return self
