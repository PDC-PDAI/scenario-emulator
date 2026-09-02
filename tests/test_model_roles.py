from agno.models.message import Message
from openai.types.chat import ChatCompletionChunk

from src.agents.model import _role_config, build_model
from src.agents.openrouter import OpenRouterChat
from src.settings import settings


def test_role_model_inherits_global_configuration(monkeypatch):
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(settings, "OPENAI_MODEL", "global-model")
    monkeypatch.setattr(settings, "EVALUATOR_LLM_PROVIDER", None)
    monkeypatch.setattr(settings, "EVALUATOR_MODEL", None)

    assert _role_config("evaluator") == ("openai", "global-model")


def test_role_model_can_override_provider_and_model(monkeypatch):
    monkeypatch.setattr(settings, "RESPONSE_GENERATOR_LLM_PROVIDER", "ollama")
    monkeypatch.setattr(settings, "RESPONSE_GENERATOR_MODEL", "attack-generator")

    assert _role_config("response_generator") == ("ollama", "attack-generator")


def test_openrouter_uses_reasoning_parameter(monkeypatch):
    max_retries = 12
    monkeypatch.setattr(settings, "LLM_PROVIDER", "openai_like")
    monkeypatch.setattr(settings, "OPENAI_MODEL", "z-ai/glm-5.2:free")
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "OPENAI_REASONING_EFFORT", "high")
    monkeypatch.setattr(settings, "OPENAI_MAX_RETRIES", max_retries)

    model = build_model()

    assert isinstance(model, OpenRouterChat)
    assert model.reasoning_effort is None
    assert model.max_retries == max_retries
    assert model.extra_body == {"reasoning": {"effort": "high", "exclude": False}}


def test_openrouter_preserves_streamed_reasoning_details_for_tool_continuation():
    model = OpenRouterChat(id="z-ai/glm-5.2:free")
    chunk = ChatCompletionChunk.model_validate(
        {
            "id": "response-1",
            "created": 1,
            "model": "z-ai/glm-5.2:free",
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "reasoning_details": [
                            {
                                "type": "reasoning.text",
                                "text": "bloco intacto",
                                "format": "unknown",
                                "index": 0,
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        }
    )

    response = model._parse_provider_response_delta(chunk)
    details = response.provider_data["reasoning_details"]
    formatted = model._format_message(
        Message(role="assistant", content="", provider_data={"reasoning_details": details})
    )

    assert formatted["reasoning_details"] == details
    assert formatted["reasoning_details"][0]["text"] == "bloco intacto"
