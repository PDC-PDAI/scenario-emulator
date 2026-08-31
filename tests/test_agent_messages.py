"""Captura da conversa crua (``messages``) para o re-rollout da Frente C."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agno.models.message import Message

from src.agents.utils import clean_agent_messages
from src.schemas.agent_debug.schema import AgentDebugTrajectory
from src.services.agent_debug.service import save_trajectory_files

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
