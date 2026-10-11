"""Coercion of ``Settings`` environment variables."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.settings import Settings

_VERBOSE_LEVEL = 2


def test_agno_debug_level_accepts_env_strings():
    # .env yields strings; Literal[1, 2] alone used to reject "1".
    assert Settings(AGNO_DEBUG_LEVEL="1").AGNO_DEBUG_LEVEL == 1
    assert Settings(AGNO_DEBUG_LEVEL="2").AGNO_DEBUG_LEVEL == _VERBOSE_LEVEL
    assert Settings(AGNO_DEBUG_LEVEL=2).AGNO_DEBUG_LEVEL == _VERBOSE_LEVEL


def test_agno_debug_level_still_rejects_invalid_values():
    with pytest.raises(ValidationError):
        Settings(AGNO_DEBUG_LEVEL="3")
    with pytest.raises(ValidationError):
        Settings(AGNO_DEBUG_LEVEL="verbose")
