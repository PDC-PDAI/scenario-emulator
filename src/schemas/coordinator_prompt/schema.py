from __future__ import annotations

import uuid
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PromptIntent(str, Enum):
    BENIGN = "benign"
    MALICIOUS = "malicious"


class ExpectedAction(str, Enum):
    COMPLY = "comply"
    REFUSE = "refuse"


class PromptCategory(str, Enum):
    PROFESSIONAL_CUSTOMIZATION = "professional_customization"
    PROMPT_INJECTION = "prompt_injection"
    POLICY_BYPASS = "policy_bypass"
    SYSTEM_PROMPT_EXFILTRATION = "system_prompt_exfiltration"
    OUTPUT_SCHEMA_MANIPULATION = "output_schema_manipulation"
    DISCRIMINATORY_CONTENT = "discriminatory_content"
    SENSITIVE_DATA_COLLECTION = "sensitive_data_collection"
    HARMFUL_CONTENT = "harmful_content"


class GeneratedCoordinatorPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: PromptIntent
    category: PromptCategory
    command: str = Field(min_length=5, max_length=4000)
    expected_action: ExpectedAction
    requested_question_count: int | None = Field(default=None, ge=1, le=50)
    rationale: str = Field(min_length=5, max_length=1000)

    @model_validator(mode="after")
    def validate_expected_action(self) -> GeneratedCoordinatorPrompt:
        expected = (
            ExpectedAction.COMPLY if self.intent is PromptIntent.BENIGN else ExpectedAction.REFUSE
        )
        if self.expected_action is not expected:
            raise ValueError(f"{self.intent.value} requer expected_action={expected.value}")
        if (
            self.intent is PromptIntent.BENIGN
            and self.category is not PromptCategory.PROFESSIONAL_CUSTOMIZATION
        ):
            raise ValueError("Comando benigno deve usar professional_customization")
        return self


class GeneratedCoordinatorPromptBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompts: list[GeneratedCoordinatorPrompt] = Field(min_length=1, max_length=100)


class CoordinatorPrompt(GeneratedCoordinatorPrompt):
    id: str = Field(default_factory=lambda: f"command-{uuid.uuid4()}")
    job_description_id: str
    sequence: int = Field(ge=1)


class CoordinatorPromptBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_description_id: str
    prompts: list[CoordinatorPrompt] = Field(min_length=1, max_length=100)
