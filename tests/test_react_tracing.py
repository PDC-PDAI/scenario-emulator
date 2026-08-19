from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agno.run.agent import RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from src.services.observability import react
from src.services.observability.react import ReactSpanStreamer


def _tool(name: str, *, args=None, result=None, call_id="c1"):
    return SimpleNamespace(
        tool_name=name,
        tool_args=args,
        result=result,
        tool_call_id=call_id,
    )


@pytest.mark.asyncio
async def test_react_spans_are_reasoning_action_observation(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(react, "get_langfuse_client", lambda: client)
    sleep = AsyncMock()
    monkeypatch.setattr(react.asyncio, "sleep", sleep)
    streamer = ReactSpanStreamer(node_prefix="scenario.questionnaire.001")
    tool = _tool("get_info_vaga", args={"code": "job-1"}, result={"title": "Dev"})

    await streamer.handle(RunContentEvent(reasoning_content="Vou consultar a vaga."))
    await streamer.handle(ToolCallStartedEvent(tool=tool))
    await streamer.handle(ToolCallCompletedEvent(tool=tool))

    names = [call.kwargs["name"] for call in client.start_observation.call_args_list]
    assert names == ["reasoning", "action:get_info_vaga", "observation"]
    assert (
        client.start_observation.call_args_list[0]
        .kwargs["metadata"]["node_id"]
        .endswith(".reasoning.01")
    )
    assert client.start_observation.call_args_list[0].kwargs["input"] == {
        "step": 1,
        "context_node_ids": [],
        "trigger": "model_reasoning_summary",
    }
    assert client.start_observation.call_args_list[1].kwargs["metadata"]["depends_on"] == [
        "scenario.questionnaire.001.reasoning.01"
    ]
    sleep.assert_awaited_once_with(react._LANGFUSE_TIMESTAMP_TICK_SECONDS)


@pytest.mark.asyncio
async def test_second_reasoning_depends_on_previous_observation(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(react, "get_langfuse_client", lambda: client)
    monkeypatch.setattr(react.asyncio, "sleep", AsyncMock())
    streamer = ReactSpanStreamer(node_prefix="scenario.questionnaire.001")
    first = _tool("get_info_vaga", result={"title": "Dev"}, call_id="a")
    second = _tool("salvar_formulario", result={"ok": True}, call_id="b")

    for event in (
        ToolCallStartedEvent(tool=first),
        ToolCallCompletedEvent(tool=first),
        ToolCallStartedEvent(tool=second),
        ToolCallCompletedEvent(tool=second),
    ):
        await streamer.handle(event)

    second_reasoning = client.start_observation.call_args_list[3].kwargs
    assert second_reasoning["name"] == "reasoning"
    assert second_reasoning["metadata"]["depends_on"] == [
        "scenario.questionnaire.001.observation.01"
    ]
    assert second_reasoning["input"] == {
        "step": 2,
        "context_node_ids": ["scenario.questionnaire.001.observation.01"],
        "trigger": "tool_selection",
    }
    assert streamer.terminal_dependencies == ["scenario.questionnaire.001.observation.02"]
