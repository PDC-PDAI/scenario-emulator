# Error Recovery Case ER-01 — `action/invalid_action`

## Finalidade

Este case verifica se a pipeline AgentDebug-RH localiza uma falha de seleção de
tool no ponto em que ela surge, sem atribuí-la ao planejamento correto nem aos
steps anteriores.

O artefato é autocontido, sintético e não contém credenciais, PII ou dependência
de arquivos em `outputs/`. Ele pode ser executado imediatamente após clonar os
dois repositórios.

## Hipótese experimental

Se o planejamento seleciona a operação correta, mas a ação chama uma tool que
não existe, o diagnóstico deve apontar:

| Campo | Valor esperado |
|---|---|
| Step crítico | `2` |
| Módulo | `action` |
| Tipo | `invalid_action` |
| Resultado da tarefa | `false` |

## Construção do erro

A trajetória contém dois steps:

1. `get_info_vaga` consulta a vaga com sucesso;
2. o planning determina corretamente o uso de `salvar_formulario`, mas a ação é
   alterada para `salvar_formulario_v2`. O ambiente responde `TOOL_NOT_FOUND`.

A única variável controlada é o nome da tool no módulo `action` do step 2. O
contexto anterior, o planejamento e o índice do step permanecem válidos. Isso
reduz ambiguidade entre `planning/constraint_ignorance` e
`action/invalid_action`.

Aqui, step significa o ciclo completo `contexto → planning → action → resposta
do ambiente`. Por isso cada chamada de tool ocupa um step, e não três. O
`raw_output` preserva planning e action no mesmo envelope, antes da separação
em `module_outputs`.

## Pré-requisitos

Organize os checkouts como diretórios irmãos:

```text
workspace/
├── scenario-emulator/
└── agentdebug-rh/
```

No `agentdebug-rh`, instale as dependências e configure o provider conforme o
`.env.example` do próprio projeto:

```bash
uv sync
cp .env.example .env
```

Antes de executar, revise o JSONL: a pipeline enviará seu conteúdo ao provider
configurado no AgentDebug-RH.

## Execução

A partir do diretório `agentdebug-rh`:

```bash
uv run python src/main.py \
  ../scenario-emulator/examples/error_recovery/invalid_action/agent-debug.jsonl \
  --output-dir output/cases/error-recovery-invalid-action \
  --max-parallel 1 \
  --max-attempts 1 \
  --print-diagnosis
```

`--max-attempts 1` executa detecção por módulo, seleção da causa raiz e geração
de feedback. Não tenta re-rollout.

## Critérios de aceite

O case passa quando:

1. o arquivo é aceito pelo schema `Trajectory` sem conversão;
2. a causa raiz é atribuída ao step 2;
3. o módulo crítico é `action`;
4. o tipo é `invalid_action`;
5. o feedback recomenda usar `salvar_formulario`, sem alterar o step 1.

O diagnóstico produzido deve ser comparado com
[`expected-diagnosis.json`](expected-diagnosis.json). Como a classificação é
feita por LLM, divergências devem ser registradas como resultado experimental,
não corrigidas silenciosamente no fixture. O arquivo é um oráculo parcial, não
um dump de `TrajectoryDiagnosis`; o mapeamento dos campos está documentado em
[`docs/data-contracts/agentdebug.md`](../../../docs/data-contracts/agentdebug.md).

## Artefatos

| Arquivo | Uso |
|---|---|
| [`agent-debug.jsonl`](agent-debug.jsonl) | entrada direta da CLI AgentDebug-RH |
| [`trajectory.json`](trajectory.json) | mesma trajetória em JSON legível |
| [`manifest.json`](manifest.json) | operação de injeção e ground truth |
| [`expected-diagnosis.json`](expected-diagnosis.json) | oráculo para comparação |

## Limitação

Este case testa detecção, causa raiz e remediação. Ele não demonstra recuperação
completa: `replay.performed=false` enquanto o contrato HTTP do adaptador de
re-rollout não estiver implementado.

Uma execução equivalente foi publicada no Langfuse para inspeção da hierarquia:
[error-recovery-injected-trajectory](https://fuse-agents.sissa.ufg.br/project/cmshwjvwp0019of07n2hc6rs0/traces/5cd1b7be742a2348b156f033e32d9b67).
