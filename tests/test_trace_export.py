from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.services.observability import export


def test_fetch_trace_export_includes_nested_observations_and_scores(monkeypatch):
    trace = MagicMock()
    trace.model_dump.return_value = {
        "id": "trace-1",
        "input": {"brief": "Vaga backend"},
        "observations": [{"id": "obs-1", "input": {"step": 1}}],
        "scores": [{"id": "score-1", "name": "benchmark_passed", "value": 1}],
    }
    client = SimpleNamespace(api=SimpleNamespace(trace=SimpleNamespace(get=MagicMock(return_value=trace))))
    monkeypatch.setattr(export, "get_langfuse_client", lambda: client)

    result = export.fetch_trace_export(" trace-1 ")

    client.api.trace.get.assert_called_once_with("trace-1")
    trace.model_dump.assert_called_once_with(mode="json", by_alias=True)
    assert result["observations"][0]["id"] == "obs-1"
    assert result["scores"][0]["id"] == "score-1"


def test_fetch_trace_export_requires_langfuse_configuration(monkeypatch):
    monkeypatch.setattr(export, "get_langfuse_client", lambda: None)

    with pytest.raises(RuntimeError, match="Langfuse não configurado"):
        export.fetch_trace_export("trace-1")
