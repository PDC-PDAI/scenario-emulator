from src.agents.model import _role_config
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
