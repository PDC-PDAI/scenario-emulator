from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

import langfuse

from src.schemas.observability.schema import NodeLocus, TraceNode
from src.services.observability import service


def _node() -> TraceNode:
    return TraceNode(
        node_id="scenario-1.questionnaire.001",
        locus=NodeLocus.SUB_GER,
        name="questionnaire-agent",
    )


def test_observation_can_start_an_isolated_trace(monkeypatch):
    span = MagicMock()
    client = MagicMock()
    client.start_as_current_observation.return_value = nullcontext(span)
    monkeypatch.setattr(service, "get_langfuse_client", lambda: client)

    with service.observation(_node(), as_type="agent", trace_id="a" * 32) as observed:
        assert observed is span

    assert client.start_as_current_observation.call_args.kwargs["trace_context"] == {
        "trace_id": "a" * 32
    }


def test_create_langfuse_trace_id_is_correlated_with_trajectory(monkeypatch):
    client = SimpleNamespace(create_trace_id=MagicMock(return_value="b" * 32))
    monkeypatch.setattr(service, "get_langfuse_client", lambda: client)

    trace_id = service.create_langfuse_trace_id(seed="trajectory-123")

    assert trace_id == "b" * 32
    client.create_trace_id.assert_called_once_with(seed="trajectory-123")


def test_create_langfuse_trace_id_is_optional(monkeypatch):
    monkeypatch.setattr(service, "get_langfuse_client", lambda: None)

    assert service.create_langfuse_trace_id(seed="trajectory-123") is None


def test_trace_attributes_group_trajectory_in_scenario_session(monkeypatch):
    propagate = MagicMock(return_value=nullcontext())
    client = MagicMock()
    monkeypatch.setattr(service, "get_langfuse_client", lambda: client)
    monkeypatch.setattr(langfuse, "propagate_attributes", propagate)

    with service.trace_attributes(
        session_id="scenario-1",
        trace_name="questionnaire-trajectory",
        environment="development",
        tags=["front-a", "trajectory"],
        metadata={"trajectory_id": "trajectory-123"},
    ):
        pass

    propagate.assert_called_once_with(
        session_id="scenario-1",
        trace_name="questionnaire-trajectory",
        environment="development",
        tags=["front-a", "trajectory"],
        metadata={"trajectory_id": "trajectory-123"},
    )
