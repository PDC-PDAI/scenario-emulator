"""Captura da conversa (``messages``) para o re-rollout da Frente C."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from agno.models.base import Model
from agno.models.message import Message
from agno.models.response import ModelResponse

import src.services.questionnaire.service as questionnaire_service_module
from src.agents.utils import clean_agent_messages
from src.schemas.agent_debug.schema import AgentDebugTrajectory
from src.schemas.coordinator_prompt.schema import (
    CoordinatorPrompt,
    ExpectedAction,
    PromptCategory,
    PromptIntent,
)
from src.schemas.job_description.schema import JobDescription
from src.schemas.questionnaire.schema import ExecutionStatus
from src.services.agent_debug.service import save_trajectory_files
from src.services.questionnaire.service import QuestionnaireService

_CONVERSATION_LENGTH = 5


def _run_output() -> SimpleNamespace:
    return SimpleNamespace(
        messages=[
            Message(role="system", content="Você é o agente gerador de questionários."),
            Message(role="user", content="Gere o questionário da vaga job-1."),
            Message(
                role="assistant",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_info_vaga", "arguments": '{"code": "job-1"}'},
                    }
                ],
            ),
            Message(
                role="tool",
                tool_call_id="call_1",
                tool_name="get_info_vaga",
                content='{"id": "job-1", "title": "Dev"}',
            ),
            Message(role="assistant", content='{"reasoning": "ok", "questions": []}'),
        ]
    )


def test_clean_agent_messages_preserves_conversation_order_and_ids():
    messages = clean_agent_messages(_run_output())

    assert messages is not None
    assert [m.role for m in messages] == ["system", "user", "assistant", "tool", "assistant"]

    call = messages[2].tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "get_info_vaga"
    assert call.arguments == {"code": "job-1"}  # argumentos JSON viram dict

    tool = messages[3]
    assert tool.tool_call_id == "call_1"
    assert tool.name == "get_info_vaga"
    assert tool.error is None

    assert messages[4].content == '{"reasoning": "ok", "questions": []}'
    assert messages[4].tool_calls is None


def test_clean_agent_messages_marks_tool_errors():
    run_output = SimpleNamespace(
        messages=[
            Message(
                role="tool",
                tool_call_id="call_9",
                tool_name="salvar_formulario",
                content="TOOL_NOT_FOUND",
                tool_call_error=True,
            )
        ]
    )

    (message,) = clean_agent_messages(run_output)

    assert message.error is True


def test_clean_agent_messages_returns_none_without_capture():
    assert clean_agent_messages(None) is None
    assert clean_agent_messages(SimpleNamespace(messages=None)) is None
    assert clean_agent_messages(SimpleNamespace(messages=[])) is None


def test_saved_trajectory_round_trips_messages(tmp_path):
    trajectory = AgentDebugTrajectory(
        trajectory_id="trajectory-msg",
        task_description="Executar com captura crua.",
        environment="scenario-emulator/front-a/questionnaire-agent",
        success=True,
        steps=[],
        messages=clean_agent_messages(_run_output()),
    )
    legacy = AgentDebugTrajectory(
        trajectory_id="trajectory-legacy",
        task_description="Execução legada sem captura.",
        environment="scenario-emulator/front-a/questionnaire-agent",
        success=False,
        steps=[],
    )

    paths = save_trajectory_files([trajectory, legacy], tmp_path)

    with_messages = AgentDebugTrajectory.model_validate_json(
        paths[0].read_text(encoding="utf-8")
    )
    assert with_messages.messages is not None
    assert len(with_messages.messages) == _CONVERSATION_LENGTH
    assert with_messages.messages[2].tool_calls[0].id == "call_1"

    # Trajetória sem captura mantém o arquivo no formato de antes (sem a chave).
    assert "messages" not in json.loads(paths[1].read_text(encoding="utf-8"))


class ExplodingStreamModel(Model):
    """Um delta e depois TimeoutError: o Agno emite RunErrorEvent SEM RunOutput."""

    def __init__(self) -> None:
        super().__init__(id="exploding-model", name="Exploding", provider="Test")

    async def ainvoke_stream(self, messages, assistant_message, **kwargs):  # type: ignore[override]
        yield ModelResponse(role="assistant", content="Parcial antes do timeout...")
        raise TimeoutError("provider timeout simulado")

    async def ainvoke(self, *a: Any, **k: Any) -> ModelResponse:  # pragma: no cover
        raise NotImplementedError

    def invoke(self, *a: Any, **k: Any) -> ModelResponse:  # pragma: no cover
        raise NotImplementedError

    def invoke_stream(self, *a: Any, **k: Any):  # pragma: no cover
        raise NotImplementedError

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response  # pragma: no cover

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return response


@pytest.mark.asyncio
async def test_provider_failure_still_exports_messages(monkeypatch):
    # Caminho apontado na revisão: erro de provider/timeout → RunErrorEvent sem
    # RunOutput. A conversa até o turno da falha deve ser recuperada da sessão
    # em memória e sair na trajetória.
    monkeypatch.setattr(questionnaire_service_module, "build_model", ExplodingStreamModel)
    job = JobDescription(
        id="job-err",
        title="Pessoa Desenvolvedora Backend",
        summary="Desenvolvimento de APIs Python para uma plataforma de recrutamento.",
        responsibilities=["Construir e manter APIs."],
        requirements=["Experiência com Python e FastAPI."],
        source_brief="Vaga backend Python e FastAPI.",
    )
    prompt = CoordinatorPrompt(
        id="command-err",
        job_description_id=job.id,
        sequence=1,
        intent=PromptIntent.BENIGN,
        category=PromptCategory.PROFESSIONAL_CUSTOMIZATION,
        command="Gere duas perguntas técnicas para a vaga.",
        expected_action=ExpectedAction.COMPLY,
        requested_question_count=2,
        rationale="Cenário controlado de falha de provider.",
    )

    result = await QuestionnaireService().execute(job, prompt, scenario_id="scenario-err")

    assert result.status is ExecutionStatus.FAILED
    assert (result.failure_reason or "").startswith("AGENT_RUNTIME_ERROR")
    trajectory = result.agent_debug_trajectory
    assert trajectory is not None
    assert trajectory.messages, "a conversa deve ser recuperada mesmo sem RunOutput no stream"
    roles = [m.role for m in trajectory.messages]
    assert roles[0] == "system"
    assert "user" in roles
