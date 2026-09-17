from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Literal

from pydantic import BaseModel, ConfigDict, Field


class DatasetGenerationConfig(BaseModel):
    """Parâmetros comuns do protocolo comparativo no OpenRouter."""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1)
    base_url: Literal["https://openrouter.ai/api/v1"] = "https://openrouter.ai/api/v1"
    temperature: Literal[0] = 0
    top_p: Literal[1] = 1
    max_tokens: int = Field(default=8192, ge=1)
    max_retries: int = Field(default=2, ge=0)
    timeout: float = Field(default=120, gt=0)


dataset_generation: ContextVar[DatasetGenerationConfig | None] = ContextVar(
    "dataset_generation", default=None
)


@contextmanager
def generation_scope(config: DatasetGenerationConfig) -> Iterator[None]:
    token = dataset_generation.set(config)
    try:
        yield
    finally:
        dataset_generation.reset(token)
