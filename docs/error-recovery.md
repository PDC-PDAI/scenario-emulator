# Error Recovery

🇧🇷 **Português** · [🇺🇸 English](error-recovery.en.md)

O trabalho de Error Recovery produz trajetórias reproduzíveis para localizar e
diagnosticar falhas de agentes com o AgentDebug-RH. O Scenario Emulator funciona
como a Frente A, que gera e anota os dados; o AgentDebug-RH funciona como a Frente
B, que detecta a causa crítica e propõe remediação.

O trabalho usa o perfil
[`configs/fronts/error_recovery.yaml`](../configs/fronts/error_recovery.yaml).

## Escopo atual

O perfil captura checkpoints ReAct por etapa e exporta o contrato `Trajectory`.
O catálogo implementado contém onze falhas distribuídas entre `planning`,
`action` e `system`:

| Módulo | Tipos de erro |
|---|---|
| `planning` | `constraint_ignorance`, `impossible_action`, `inefficient_plan` |
| `action` | `misalignment`, `invalid_action`, `format_error`, `parameter_error` |
| `system` | `step_limit`, `tool_execution_error`, `llm_limit`, `environment_error` |

O comando `run` gera baselines reais. A injeção balanceada acontece no runner
descrito em [Campanhas da Frente B](#campanhas-da-frente-b). O mecanismo, as
variantes e os limites causais de cada classe estão documentados no
[catálogo de injeção de falhas](fault-injection-catalog.md).

O re-rollout não é executado neste repositório. O `usecases-service` oferece
`POST /executions/{id}/re-rollout`, reutiliza o prefixo salvo em `messages` e cria
uma trajetória filha com `lineage`. Por isso, `replay_enabled` permanece `false` no
perfil: habilitá-lo aqui sugeriria uma capacidade que ainda pertence à Frente C.

## Gerando trajetórias

```bash
uv run scenario-emulator run \
  --profile configs/fronts/error_recovery.yaml \
  --brief "Vaga sênior de backend Python, FastAPI e PostgreSQL"
```

O perfil padrão gera três comandos benignos, não cria respostas de candidatos e
mantém o avaliador desabilitado. Os artefatos ficam em:

```text
outputs/error_recovery/
├── scenario.json
├── benchmark.jsonl
├── agent-debug.jsonl
└── trajectories/
```

Para isolar uma execução, sobrescreva os caminhos:

```bash
uv run scenario-emulator run \
  --profile configs/fronts/error_recovery.yaml \
  --brief "Vaga sênior de backend Python, FastAPI e PostgreSQL" \
  --benign 1 \
  --malicious 0 \
  --output outputs/agentdebug-e2e/scenario.json \
  --jsonl outputs/agentdebug-e2e/benchmark.jsonl \
  --agent-debug-jsonl outputs/agentdebug-e2e/agent-debug.jsonl \
  --trajectories-dir outputs/agentdebug-e2e/trajectories
```

## Contrato com o AgentDebug-RH

A definição normativa de `step`, os schemas de entrada e saída e a matriz de
métricas estão em [`docs/data-contracts/agentdebug.md`](data-contracts/agentdebug.md).

Cada execução persiste dois níveis de informação:

- `failure_annotation`: ground truth determinístico do emulador, com código,
  etapa, módulo, tipo de erro, evidência e indicação de retry;
- `agent_debug_trajectory`: entrada autocontida da pipeline de diagnóstico no
  formato `Trajectory` usado pelo AgentDebug-RH.

Cada tool call vira um `TrajectoryStep` 1-indexado. O step reúne o contexto, as
saídas observáveis de módulo (`planning` e `action`) e a resposta da tool em
`env_response`. Memory e reflection não são inventados quando o agente não os
emite. Se não houver tool terminal, um último step explícito preserva a falha.

As trajetórias do questionário também carregam `messages`: a conversa ordenada
com papéis, conteúdo e `tool_call_id` reais. O contrato é normalizado, não
byte-a-byte; argumentos JSON viram objetos e metadados internos do provider são
removidos. Esse histórico é o prefixo usado pela Frente C em um re-rollout.

O campo `success` representa o resultado do oráculo do cenário. Uma recusa correta
é sucesso; over-refusal e obediência indevida são falhas. A anotação é o ground
truth experimental, não um diagnóstico de causa raiz — essa responsabilidade
continua sendo do AgentDebug-RH.

## Smoke test entre as frentes A e B

Um caso autocontido de `action/invalid_action` está em
[`examples/error_recovery/invalid_action/`](../examples/error_recovery/invalid_action/README.md).
Ele funciona a partir de um clone limpo e não depende de `outputs/`.

Mantenha os dois checkouts como irmãos:

```text
PDC-PDAI/
├── scenario-emulator/
└── agentdebug-rh/
```

Depois de instalar e configurar o provider da Frente B, execute:

```bash
cd ../agentdebug-rh
uv run python src/main.py \
  ../scenario-emulator/examples/error_recovery/invalid_action/agent-debug.jsonl \
  --output-dir ../scenario-emulator/outputs/agentdebug-smoke \
  --max-parallel 1 \
  --max-attempts 1 \
  --print-diagnosis
```

O resultado esperado aponta `step 2 / action / invalid_action`, com uma causa raiz
e uma remediação. Compare a saída com
[`expected-diagnosis.json`](../examples/error_recovery/invalid_action/expected-diagnosis.json).

Para analisar uma trajetória recém-gerada:

```bash
cd ../agentdebug-rh
uv run python src/main.py \
  ../scenario-emulator/outputs/agentdebug-e2e/agent-debug.jsonl \
  --output-dir ../scenario-emulator/outputs/agentdebug-e2e/diagnoses \
  --max-parallel 1 \
  --max-attempts 1 \
  --print-diagnosis
```

A Frente B ignora trajetórias cujo `success` seja `true`, pois não há falha a
diagnosticar. Use o caso controlado para validar causa raiz e remediação; use o
fluxo recém-gerado para validar a exportação e a leitura do contrato.

## Campanhas da Frente B

A campanha histórica
[`error-recovery-front-b-100.yaml`](../configs/campaigns/error-recovery-front-b-100.yaml)
gera 100 casos das quatro classes de `action`. A campanha principal
[`error-recovery-front-b-220.yaml`](../configs/campaigns/error-recovery-front-b-220.yaml)
executa dez temas, coleta 220 baselines válidas e distribui vinte casos para
cada uma das onze classes implementadas. O runner repõe baselines naturais que
falharem e reaproveita os lotes salvos quando é executado novamente.

```bash
uv run scenario-emulator validate-dataset-campaign \
  configs/campaigns/error-recovery-front-b-220.yaml

uv run scenario-emulator run-dataset \
  --campaign configs/campaigns/error-recovery-front-b-220.yaml \
  --max-parallel 10
```

A campanha
[`error-recovery-front-b-1100.yaml`](../configs/campaigns/error-recovery-front-b-1100.yaml)
expande as 220 baselines de forma offline. Cada baseline recebe cinco faults
distintos, produzindo 1.100 trajetórias e cem casos por classe sem novas chamadas
ao modelo:

```bash
uv run scenario-emulator run-dataset \
  --campaign configs/campaigns/error-recovery-front-b-1100.yaml
```

Saída:

```text
outputs/error-recovery-front-b-1100/
├── front-b-input.jsonl
├── front-b-inputs/
├── labels.json
└── private/
```

`front-b-input.jsonl` e `front-b-inputs/` são as entradas públicas, com IDs neutros
e sem o rótulo esperado. `labels.json` deve ser enviado separadamente apenas para
a avaliação; não o coloque no diretório consumido pelo detector. `private/` guarda
baselines, proveniência, manifesto e checkpoints.

## Observabilidade e diagnóstico

Com Langfuse configurado, filtre traces pelas tags `error_recovery`, `trajectory`
ou pelo nome do perfil. As saídas incluem a trajetória completa e a anotação de
falha, enquanto os JSONs em `trajectories/` continuam sendo a fonte canônica para
intercâmbio e replay.

O debug detalhado do Agno é opcional:

```bash
AGNO_DEBUG=true AGNO_DEBUG_LEVEL=2 \
  uv run scenario-emulator run \
  --profile configs/fronts/error_recovery.yaml \
  --brief "Vaga sênior de backend Python, FastAPI e PostgreSQL"
```

Use-o apenas localmente, pois pode imprimir prompts e argumentos de tools. A
captura do contrato AgentDebug-RH funciona mesmo com `AGNO_DEBUG=false`.

Se a Frente B responder `Unsupported parameter: 'temperature'`, escolha um modelo
compatível ou uma versão da Frente B que omita esse parâmetro. Além do exit code,
confirme que o relatório mostra zero chamadas com falha e que
`module_analyses[*].status` é `ok`.

[Voltar ao README principal](../README.md)

## Arquitetura e desenvolvimento

## Responsabilidades

A CLI em `src/cli.py` carrega perfis YAML estritos e aplica overrides antes de
chamar os serviços. Caminhos de perfis e campanhas são relativos ao diretório
de trabalho: execute os exemplos a partir da raiz do repo.

| Camada | Arquivos principais | Contrato |
|---|---|---|
| Configuração | `src/settings.py`, `src/services/experiment/profile.py` | `.env`, provider, perfis e capacidades |
| Preparação | `src/services/job_description/service.py`, `src/services/coordinator_prompt/service.py` | Vaga estruturada e comandos |
| Agente observado | `src/services/questionnaire/service.py` | Consulta da vaga e tool terminal de formulário/falha |
| Captura | `src/services/observability/react.py`, `src/agents/utils.py` | Eventos, tool calls e mensagens normalizadas |
| Exportação | `src/services/agent_debug/service.py` | `AgentDebugTrajectory` e anotação determinística |
| Campanhas | `src/services/dataset/service.py` | Baselines, injeções, retomada, labels e proveniência |
| Contratos | `src/schemas/agent_debug/schema.py`, `src/schemas/dataset/schema.py` | Taxonomia, steps e configurações válidas |

`ScenarioService.run` encadeia a criação da vaga, os comandos benignos e a
execução do agente de questionários para gerar baselines da Frente A.

### Execução real e construção do dataset

A execução real usa Agno e tools locais, produzindo checkpoints e a conversa
normalizada em `messages`. Cada ciclo de decisão com tool call e resposta ocupa
um step; eventos de infraestrutura não viram steps independentes.

`ErrorRecoveryDatasetService` coleta baselines aprovadas pelo oráculo e encerradas
com `salvar_formulario`. A coleta usa lotes limitados, persiste progresso e repõe
baselines que falham. Depois, `inject_fault` transforma cópias dos checkpoints:
não provoca uma nova interação do modelo com o ambiente alterado.

A expansão offline reutiliza os batches em `private/baseline-scenarios` do dataset
anterior. Sem esses arquivos, o YAML de 1.100 casos não pode reconstruir a campanha
histórica a partir apenas dos JSONLs públicos. O re-rollout não é executado aqui.

### Alterar uma falha

1. Verifique o par módulo/tipo na taxonomia do schema e no contrato da Frente B.
2. Declare o modo no perfil `configs/fronts/error_recovery.yaml`.
3. Implemente a mutação em `src/services/dataset/service.py`, incluindo variantes
   e proveniência privada. Preserve os steps anteriores à causa crítica quando aplicável.
4. Atualize a campanha que deverá usar a classe e o catálogo nas duas línguas.
5. Teste o efeito observável, step crítico, alinhamento causal e separação de labels.

Não crie `memory` ou `reflection` pós-processados: o agente atual não emite esses
módulos. Um futuro produtor precisa emiti-los e demonstrar seu efeito nas decisões.

### Alterar contratos ou prompts

Mantenha índices contíguos a partir de 1, envelopes `raw_output` completos e
`messages` normalizadas. Uma modificação do schema exige conferir o consumidor
AgentDebug-RH e atualizar o exemplo autocontido. `failure_annotation` é ground
truth experimental; diagnóstico de causa raiz é saída da Frente B.

Prompts locais ficam em `src/prompts/raw_prompts.py`; Langfuse pode substituí-los
por nome e label. Registre as versões realmente usadas quando comparar campanhas.
Mudanças de prompt/provider alteram a distribuição das baselines, mesmo com o
mesmo YAML. Nenhuma seed garante repetição exata do conteúdo de um LLM remoto.

### Verificar

```bash
uv run ruff check .
uv run pytest -q
uv run scenario-emulator validate-profile configs/fronts/error_recovery.yaml
uv run scenario-emulator validate-dataset-campaign configs/campaigns/error-recovery-front-b-220.yaml
```

`test_dataset_campaign.py` cobre injeção, variantes, exportação e retomada;
`test_agent_debug_contract.py` e `test_agent_messages.py` cobrem os contratos;
`test_error_recovery_examples.py` valida o fixture; os testes de controles v2
cobrem auditoria e integridade da seleção. Os modelos são substituídos nos testes.

Os scripts de controles v2 auditam e selecionam trajetórias de sucesso para
medir falsos positivos. Consulte o [protocolo v2](dataset-v2-success-controls.md)
para fontes, critérios de seleção e limitações.

### Diagnóstico local

`AGNO_DEBUG=true AGNO_DEBUG_LEVEL=2` habilita logs detalhados; checkpoints não
precisam dessa opção. Sem Langfuse, JSONs locais continuam sendo os artefatos de
intercâmbio. Se a campanha termina com código 2, confira o resumo e as tentativas
salvas: a meta planejada ainda não foi completada. Use um novo diretório para
smokes com `--limit`; não reutilize sua atribuição privada na campanha completa.
