# Contrato de dados Scenario Emulator → AgentDebug-RH

Este documento registra a fronteira entre a frente A (`scenario-emulator`) e a
frente B (`agentdebug-rh`). O schema executável da frente A está em
`src/schemas/agent_debug/schema.py`; o consumidor mantém o schema equivalente
em `src/schemas/observability/execution.py`.

## Unidade de execução: `step`

Um `step` é **um ciclo lógico de decisão do agente**, não uma operação atômica
de infraestrutura:

```text
contexto do step
  → memory/reflection, quando emitidos
  → planning
  → action
  → env_response
```

Assim, planejamento, chamada de tool e resposta da tool pertencem ao mesmo
step. O próximo step só começa quando o agente volta a decidir com a resposta
do ambiente anterior disponível. Em agentes de uma única inferência, como os
geradores estruturados e o avaliador, a inferência completa é um único step.

Os índices são contíguos e 1-indexados. O step 1 não deve inventar `memory` ou
`reflection`, pois ainda não existe histórico de execução.

## Entrada da frente B: `AgentDebugTrajectory`

```json
{
  "trajectory_id": "exec-001",
  "task_description": "Selecionar candidato",
  "environment": "sistema de RH",
  "success": false,
  "steps": [
    {
      "index": 1,
      "module_outputs": {
        "planning": "Plano produzido no primeiro step",
        "action": "{\"tool\":\"buscar_candidatos\",\"arguments\":{}}"
      },
      "step_input": "Contexto disponível antes da decisão",
      "env_response": "Resposta recebida depois da ação",
      "raw_output": "{\"planning\":\"Plano produzido no primeiro step\",\"action\":{\"tool\":\"buscar_candidatos\",\"arguments\":{}}}"
    }
  ]
}
```

Campos da trajetória:

| Campo | Semântica |
|---|---|
| `trajectory_id` | Identificador estável para correlacionar execução, diagnóstico e trace. |
| `task_description` | Objetivo que permite julgar o comportamento do agente. |
| `environment` | Agente/ambiente no qual a trajetória foi produzida. |
| `success` | Resultado da tarefa segundo o oráculo do cenário, não segundo o diagnóstico. |
| `steps` | Ciclos de decisão na ordem em que aconteceram. |

Campos de cada step:

| Campo | Semântica |
|---|---|
| `index` | Posição contígua e 1-indexada. |
| `module_outputs` | Outputs observáveis separados por módulo. Módulos ausentes não são fabricados. |
| `step_input` | Contexto disponível antes da decisão do step. |
| `env_response` | Resultado do ambiente depois da ação do mesmo step. |
| `raw_output` | Saída observável completa da qual os módulos foram extraídos. |

Quando o SDK fornece a saída inteira do modelo, `raw_output` preserva essa
saída. Quando o streaming entrega planning e tool call em eventos separados, a
frente A guarda um envelope JSON com todos os fragmentos observáveis. Ele não
pode repetir apenas `action` se `planning` também foi extraído. O campo pode ser
vazio somente quando a execução falha antes de produzir qualquer saída do
agente.

## Como os módulos analisáveis são identificados

A frente A não envia uma lista paralela. A frente B identifica os módulos pelas
chaves não vazias de `module_outputs`, usando exatamente os nomes da taxonomia:

- `memory`
- `reflection`
- `planning`
- `action`
- `system`
- `others`

No estágio de análise fina atual, `ANALYZED_MODULES` contém `memory`,
`reflection`, `planning` e `action`. `system` é atribuído a partir de evidência
do ambiente no estágio de causa raiz; `others` é a categoria de escape. A
taxonomia é definida pela frente B, e qualquer mudança de nomes ou pares
`(module, error_type)` deve ser versionada e refletida no enum/validação da
frente A antes da publicação de novas trajetórias.

## Ground truth e saída do diagnóstico

São artefatos diferentes:

- `failure_annotation` é o ground truth determinístico do cenário, produzido
  pela frente A para avaliação;
- `TrajectoryDiagnosis` é a predição da frente B, com `outcome`,
  `critical_error` e `feedback`.

O arquivo `expected-diagnosis.json` de cada case é um **oráculo de comparação**,
não uma cópia integral de `TrajectoryDiagnosis`. O comparador deve aplicar o
seguinte mapeamento:

| Oráculo do case | `TrajectoryDiagnosis` |
|---|---|
| `expected_outcome` | `outcome` |
| `critical_error.{step,module,error_type,evidence}` | `critical_error.{step,module,error_type,evidence}` |
| `expected_feedback.target_step` | `feedback.target_step` |
| `expected_feedback.expected_effect` | `feedback.expected_effect` |

O nome `expected_effect` é deliberadamente igual ao contrato da frente B. O
texto operacional real continua em `feedback.step_instruction`.

## Disponibilidade para as métricas do artigo

| Métrica | Dados necessários | Situação atual |
|---|---|---|
| Step Accuracy (`S`) | step esperado × `critical_error.step` | Disponível. |
| Step + Module (`S+M`) | step e módulo esperados × diagnosticados | Disponível. |
| All Correct (`ALL`) | step, módulo e tipo esperados × diagnosticados | Disponível. |
| Task Success Rate | `success` de cada execução/re-rollout | Disponível por execução. |
| Accumulative Success Rate | ordem das tentativas e sucesso acumulado de 1 a 5 | Pendente de re-rollout e vínculo entre tentativas. |
| Relative improvement | baseline e re-rollouts pareados para a mesma tarefa | Pendente do mesmo vínculo. |

O contrato mínimo de trajetória basta para as métricas de localização, mas não
basta sozinho para as métricas de recuperação. Para essas, o futuro contrato de
rollout deverá carregar no mínimo o identificador da execução raiz, o número da
tentativa e o ponto de retomada.
