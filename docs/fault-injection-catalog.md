# Catálogo de injeção de falhas

🇧🇷 **Português** · [🇺🇸 English](fault-injection-catalog.en.md)

Este documento descreve como o Scenario Emulator transforma uma trajetória
baseline bem-sucedida em uma trajetória controlada com uma única causa crítica.
O objetivo é produzir entradas avaliáveis pela Frente B sem expor o rótulo no
arquivo público.

## Escopo implementado

A taxonomia completa possui 18 pares `(módulo, tipo de erro)` distribuídos entre
`memory`, `reflection`, `planning`, `action` e `system`. A injeção atual cobre os
11 pares que podem ser representados de maneira causal com os checkpoints
produzidos hoje:

| Módulo | Tipos implementados |
|---|---|
| `planning` | `constraint_ignorance`, `impossible_action`, `inefficient_plan` |
| `action` | `misalignment`, `invalid_action`, `format_error`, `parameter_error` |
| `system` | `step_limit`, `tool_execution_error`, `llm_limit`, `environment_error` |

Os sete pares de `memory` e `reflection` não são injetados. O agente de
questionário atual emite somente `planning` e `action`; acrescentar textos
pós-processados de memória ou reflexão criaria módulos que não participaram da
decisão e produziria um ground truth causalmente falso.

`others/others` também não é injetado. Ele permanece uma categoria de escape
para falhas naturais que não cabem na taxonomia, e não uma classe sintética a
ser balanceada.

## Fluxo comum

1. A campanha executa o agente real até obter uma trajetória que passou no
   oráculo e terminou com `salvar_formulario`.
2. O runner atribui exatamente um `fault_id` a essa baseline.
3. O injetor seleciona uma variante da falha e modifica apenas os steps
   necessários para tornar a falha observável.
4. A trajetória resultante recebe `success=false`.
5. A entrada pública contém somente `trajectory_id`, `task_description`,
   `environment`, `success` e `steps`.
6. O rótulo mínimo `{step, module, error_type}` é gravado separadamente em
   `labels.json`.
7. Operação, variante, baseline de origem e demais detalhes ficam em
   `private/provenance.jsonl`.

As variantes são alternadas pelo número de ocorrência de cada `fault_id` na
exportação. Com vinte exemplos por classe e três variantes, a distribuição fica
em `7/7/6`, sem depender do conteúdo do ID público.

As baselines são solicitadas ao coordenador em lotes de no máximo dez. A meta de
22 execuções por cenário é preenchida por múltiplos batches (`10 + 10 + 2`) para
evitar que uma resposta longa do modelo perca itens e invalide a tentativa
inteira.

## Falhas de planning

### `planning/constraint_ignorance`

**Ponto crítico:** step terminal da baseline.

O plano terminal é substituído por uma decisão explícita de desconsiderar uma
restrição disponível no contexto. A ação continua alinhada ao plano e chama a
tool correta com JSON válido, mas o payload é trocado por perguntas genéricas.
A resposta registra que o formulário foi persistido e que o oráculo rejeitou a
cobertura semântica.

Variantes:

- `discard_job_requirements`: ignora requisitos obrigatórios da vaga;
- `discard_seniority`: ignora senioridade e profundidade esperada;
- `generic_questionnaire`: ignora restrições do comando do coordenador.

O plano é a causa crítica porque a ação executa fielmente a estratégia
defeituosa; não há tool inexistente, JSON inválido ou parâmetro ausente.

### `planning/impossible_action`

**Ponto crítico:** step 1.

A decisão terminal de salvar é movida para antes de uma pré-condição necessária.
A tool existe e os parâmetros têm o formato esperado, mas o ambiente recusa a
transição porque o estado necessário ainda não existe. A trajetória termina
nesse step.

Variantes:

- `save_before_lookup`: tenta salvar antes de consultar a vaga;
- `save_before_draft`: tenta salvar antes de produzir o rascunho;
- `save_before_validation`: tenta salvar antes de validar o formulário.

Isso é diferente de `action/invalid_action`: a ação existe. O defeito está na
estratégia que a escolheu em um estado no qual ela não pode cumprir o objetivo.

### `planning/inefficient_plan`

**Ponto crítico:** step 2.

O primeiro lookup correto é preservado. A partir do step 2, o plano afirma que
vai consultar novamente os mesmos dados, embora eles já estejam no histórico,
e a ação repete `get_info_vaga`. Nenhum step terminal é executado, portanto a
tarefa não é concluída.

Variantes:

- `repeat_lookup_twice`: duas repetições, totalizando três steps;
- `repeat_lookup_three_times`: três repetições, totalizando quatro steps;
- `repeat_lookup_five_times`: cinco repetições, totalizando seis steps.

A tool funciona e cada ação corresponde ao plano. O erro é a repetição inútil
que impede o avanço, e não uma falha da ação ou do ambiente.

## Falhas de action

Todas as falhas de `action` são aplicadas ao step terminal da baseline. O plano
original de persistir o formulário é preservado.

### `action/misalignment`

A ação válida é substituída por outra ação que contradiz o plano do mesmo step.

Variantes:

- `register_failure`: registra falha quando o plano manda salvar;
- `repeat_lookup`: repete uma consulta quando o plano manda persistir;
- `fetch_unknown_job`: consulta outro identificador em vez de persistir.

As ações usadas existem e são serializadas corretamente. O erro é a desconexão
entre intenção declarada e operação executada.

### `action/invalid_action`

O nome de `salvar_formulario` é substituído por uma tool inexistente. Os
argumentos originais são mantidos e o ambiente responde `TOOL_NOT_FOUND`.

Variantes:

- `versioned_tool`: `salvar_formulario_v2`;
- `unknown_namespace`: `questionarios.salvar_formulario`;
- `invented_archive_tool`: `arquivar_formulario_definitivo`.

### `action/format_error`

A representação da action deixa de satisfazer o envelope esperado, sem alterar
o plano.

Variantes:

- `truncated_json`: objeto JSON interrompido;
- `plain_text_call`: chamada escrita como texto livre;
- `array_instead_of_object`: lista JSON no lugar do objeto de ação.

`raw_output` continua sendo um envelope JSON auditável. Dentro dele, o valor de
`action` preserva exatamente a representação inválida que o ambiente recebeu.

### `action/parameter_error`

A tool correta é mantida, mas seus argumentos deixam de satisfazer o contrato.

Variantes:

- `missing_payload`: remove o argumento obrigatório `payload`;
- `wrong_questionnaire_id`: troca o ID pelo de outro formulário;
- `payload_wrong_type`: envia texto livre onde deveria existir um objeto.

## Falhas de system

Erros de `system` não são decisões do agente. Por isso o planning e a action
permanecem corretos sempre que uma decisão chegou a ser produzida. A evidência
principal fica na resposta do ambiente ou no evento do provider.

### `system/step_limit`

**Ponto crítico:** step 1.

O primeiro step correto é preservado, mas o runtime é configurado com teto de
um step e encerra a execução antes da persistência.

Variantes textuais:

- `max_steps_one`;
- `budget_exhausted`;
- `orchestrator_iteration_cap`.

Todas usam o evento estruturado `maximum_steps_reached`. Não há repetição
ineficiente anterior; assim a causa crítica é o limite do sistema.

### `system/tool_execution_error`

**Ponto crítico:** step terminal.

A chamada de `salvar_formulario` e seus argumentos são preservados. O ambiente
registra que os argumentos foram aceitos, mas a dependência externa falhou.

Variantes:

- `external_503`: serviço externo indisponível;
- `connection_reset`: conexão encerrada durante a chamada;
- `empty_external_response`: tool não devolve resultado.

### `system/llm_limit`

**Ponto crítico:** step terminal esperado.

A decisão terminal é substituída por um evento do provider. Esse step contém
somente o módulo `system`, pois não houve planning/action completos a serem
atribuídos ao agente.

Variantes:

- `provider_timeout`;
- `token_budget_exhausted`, com saída parcial preservada;
- `context_window_exceeded`.

### `system/environment_error`

**Ponto crítico:** step terminal.

O agente emite uma chamada válida e o ambiente aceita seus argumentos, mas a
transição de estado do próprio simulador falha.

Variantes:

- `state_commit_lost`: alteração confirmada e depois perdida;
- `simulator_invariant_broken`: estado posterior viola a transição prevista;
- `storage_partition`: armazenamento interno indisponível durante o commit.

A distinção para `tool_execution_error` é a origem: naquele caso a dependência
chamada pela tool falha; aqui o defeito está no simulador ou em seu estado.

## Falhas ainda não implementadas

Um rollout modular futuro deverá emitir `memory` e `reflection` de forma
explícita a partir do step 2:

| Módulo | Tipos pendentes |
|---|---|
| `memory` | `over_simplification`, `memory_retrieval_failure`, `hallucination` |
| `reflection` | `progress_misjudge`, `outcome_misinterpretation`, `causal_misattribution`, `hallucination` |

O step 1 continuará contendo apenas `planning` e `action`, conforme o contrato
da Frente B. Depois que o novo produtor existir, as mutações deverão contaminar
as decisões posteriores: alterar somente o texto de memory/reflection sem mudar
o caminho executado não caracteriza uma causa crítica.

## Campanha expandida

A campanha versionada `configs/campaigns/error-recovery-front-b-220.yaml` gera
vinte instâncias para cada um dos onze pares implementados:

```bash
uv run scenario-emulator validate-dataset-campaign \
  configs/campaigns/error-recovery-front-b-220.yaml

uv run scenario-emulator run-dataset \
  --campaign configs/campaigns/error-recovery-front-b-220.yaml \
  --max-parallel 10
```

Para um smoke test de baixo custo, use um diretório novo e limite a coleta:

```bash
uv run scenario-emulator run-dataset \
  --campaign configs/campaigns/error-recovery-front-b-220.yaml \
  --limit 11 \
  --output-dir outputs/error-recovery-front-b-220-smoke
```

Não reutilize esse diretório para executar depois a campanha completa: a
atribuição privada é persistida e vinculada ao tamanho planejado.

## Expansão offline para 1.100 casos

A campanha `configs/campaigns/error-recovery-front-b-1100.yaml` reutiliza as
220 baselines válidas do dataset menor sem chamar novamente os agentes ou o
provider:

```bash
uv run scenario-emulator run-dataset \
  --campaign configs/campaigns/error-recovery-front-b-1100.yaml
```

Dois campos controlam esse modo:

- `baseline_source_dir` aponta para um dataset que possui os batches originais
  em `private/baseline-scenarios`;
- `augmentations_per_baseline` define quantas trajetórias injetadas cada
  baseline produzirá.

Na configuração de 1.100 casos, cada uma das 220 baselines é usada cinco vezes
e recebe cinco `fault_id` distintos. A alocação estratificada produz:

- 100 casos por tipo de erro;
- variantes balanceadas em `34/33/33` por tipo;
- 110 casos por domínio, sendo 10 de cada tipo de erro;
- 300 casos de `planning`, 400 de `action` e 400 de `system`.

O índice da reutilização fica somente em
`private/provenance.jsonl`, nos campos privados
`injection.baseline_reuse_index` e `injection.baseline_reuse_total`.

Ao criar splits, agrupe obrigatoriamente por `parent_trajectory_id`. Todas as
trajetórias derivadas da mesma baseline devem permanecer no mesmo split; caso
contrário, o texto e os passos compartilhados criam vazamento entre treino e
teste.

## Invariantes de qualidade

- uma baseline deve ter passado no oráculo antes da injeção;
- cada trajetória recebe somente uma causa crítica planejada;
- o par `(module, error_type)` é validado pela taxonomia;
- steps permanecem contíguos e 1-indexados;
- o ID público não contém o nome da falha;
- `fault_id`, operação e variante aparecem somente na proveniência privada;
- `labels.json` permanece fora do diretório entregue ao detector;
- expansões devem manter o mesmo `parent_trajectory_id` no mesmo split;
- variantes de uma classe devem ser separadas por template ao formar splits de
  treino e teste, evitando que pequenas duplicatas inflem a métrica.
