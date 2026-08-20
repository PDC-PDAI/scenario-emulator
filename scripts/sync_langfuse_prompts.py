from __future__ import annotations

from src.clients.langfuse.client import flush_langfuse, get_langfuse_client
from src.prompts.raw_prompts import PROMPTS
from src.settings import settings

QUESTIONNAIRE_FLOW_DESCRIPTION = """## Frente A — Scenario Emulator

```mermaid
flowchart LR
  J[JobDescriptionAgent] -->|1:N| C[CoordinatorPromptAgent]
  C --> Q[QuestionnaireAgent]
  Q --> R[reasoning]
  R --> A[action:tool]
  A --> O[observation]
```

Todos os spans carregam `node_id`, `depends_on` e `locus`. Entradas e saídas são
redigidas antes do envio para evitar PII e segredos no dataset de pesquisa.
"""

EVALUATION_FLOW_DESCRIPTION = """## Avaliação adversarial de questionários

```mermaid
flowchart LR
  Q[QuestionnaireAgent] --> R[ResponseCaseAgent]
  R --> E[QuestionnaireEvaluator]
  E --> O[DeterministicOracle]
```

O avaliador porta a dimensão FORMULARIO do rh-agent-agno. Respostas são dados externos
não confiáveis e os casos maliciosos usam canários para detectar prompt injection.
"""


def sync_prompts() -> int:
    client = get_langfuse_client()
    if client is None:
        raise RuntimeError(
            "Langfuse não configurado. Defina LANGFUSE_PUBLIC_KEY, "
            "LANGFUSE_SECRET_KEY e LANGFUSE_BASE_URL no .env."
        )
    label = settings.LANGFUSE_SYNC_LABEL
    for name, content in PROMPTS.items():
        if name.startswith("front-a/questionnaire/"):
            config = {"description": QUESTIONNAIRE_FLOW_DESCRIPTION, "source": "rh-agent-agno#91"}
        elif name.startswith(("front-a/response/", "front-a/evaluator/")):
            config = {
                "description": EVALUATION_FLOW_DESCRIPTION,
                "source": "rh-agent-agno/evaluator",
            }
        else:
            config = {"description": "Frente A do scenario-emulator"}
        try:
            remote = client.get_prompt(name, label=label, type="text")
            if remote.prompt == content and getattr(remote, "config", None) == config:
                print(f"UNCHANGED {name} (version={remote.version})")
                continue
        except Exception:
            pass
        created = client.create_prompt(
            name=name,
            type="text",
            prompt=content,
            labels=[label],
            config=config,
            commit_message="Sync Front A prompts (questionnaire aligned with rh-agent-agno#91)",
        )
        print(f"SYNCED {name} -> version={created.version}")
    flush_langfuse()
    return 0


if __name__ == "__main__":
    raise SystemExit(sync_prompts())
