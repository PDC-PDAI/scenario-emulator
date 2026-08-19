from __future__ import annotations

import pytest

from src.clients.langfuse.client import get_langfuse_client
from src.settings import settings


@pytest.fixture(autouse=True)
def disable_external_tracing(monkeypatch):
    monkeypatch.setattr(settings, "LANGFUSE_TRACING_ENABLED", False)
    get_langfuse_client.cache_clear()
    yield
    get_langfuse_client.cache_clear()
