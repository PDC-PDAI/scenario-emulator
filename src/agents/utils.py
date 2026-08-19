from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def parse_model_output(content: Any, schema: type[T]) -> T:
    if isinstance(content, schema):
        return content
    if isinstance(content, BaseModel):
        return schema.model_validate(content.model_dump())
    if isinstance(content, dict):
        return schema.model_validate(content)
    if isinstance(content, str):
        cleaned = content.strip().removeprefix("```json").removesuffix("```").strip()
        return schema.model_validate(json.loads(cleaned))
    return schema.model_validate(content)


def extract_usage(response: Any) -> dict[str, int] | None:
    for raw_candidate in (getattr(response, "usage", None), getattr(response, "metrics", None)):
        if raw_candidate is None:
            continue
        candidate = raw_candidate
        if hasattr(raw_candidate, "model_dump"):
            candidate = raw_candidate.model_dump(exclude_none=True)
        elif hasattr(raw_candidate, "__dict__") and not isinstance(raw_candidate, dict):
            candidate = vars(raw_candidate)
        if not isinstance(candidate, dict):
            continue
        input_tokens = candidate.get("input_tokens") or candidate.get("prompt_tokens")
        output_tokens = candidate.get("output_tokens") or candidate.get("completion_tokens")
        usage: dict[str, int] = {}
        if input_tokens is not None:
            usage["input"] = int(input_tokens)
        if output_tokens is not None:
            usage["output"] = int(output_tokens)
        if usage:
            return usage
    return None
