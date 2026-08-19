from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field


class JobDescriptionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=2, max_length=160)
    summary: str = Field(min_length=20, max_length=3000)
    responsibilities: list[str] = Field(min_length=1, max_length=20)
    requirements: list[str] = Field(min_length=1, max_length=20)
    preferred_qualifications: list[str] = Field(default_factory=list, max_length=20)
    seniority: str | None = Field(default=None, max_length=80)
    location: str | None = Field(default=None, max_length=160)
    employment_type: str | None = Field(default=None, max_length=80)


class JobDescription(JobDescriptionDraft):
    id: str = Field(default_factory=lambda: f"job-{uuid.uuid4()}")
    source_brief: str = Field(min_length=3, max_length=10_000)
