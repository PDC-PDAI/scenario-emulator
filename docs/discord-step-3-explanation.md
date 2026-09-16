# Por que algumas falhas do dataset estão no `step 3`?

Isso é **intencional** e não representa erro de indexação.

Algumas execuções originais precisaram corrigir um problema recuperável antes
de concluir a tarefa. A baseline bem-sucedida possuía este fluxo:

```text
Step 1 → consulta a vaga com sucesso
Step 2 → tenta salvar, mas recebe um erro recuperável
Step 3 → corrige os dados e salva com sucesso
```

Na construção do dataset, preservamos o histórico e injetamos a falha crítica
no passo terminal que originalmente teria sucesso:

```text
Step 1 → consulta válida
Step 2 → falha recuperável; o agente identifica o problema e tenta corrigir
Step 3 → falha crítica injetada; a tarefa não consegue mais ser concluída
```

O `step 2` não recebe a label porque ele não é a causa do fracasso final. Na
baseline original, o agente se recuperou desse erro. A label aponta para o
`step 3`, onde a recuperação é interrompida pela falha injetada.

## Auditoria dos casos

No dataset de 1.100 trajetórias existem **28 labels no step 3**, derivadas de
9 baselines:

- 8 baselines tentaram salvar uma quantidade incorreta de perguntas e depois
  corrigiram a quantidade;
- 1 baseline usou pesos acima do limite permitido e depois corrigiu os pesos;
- nas 9 baselines, o `step 3` original executava `salvar_formulario` com sucesso;
- nenhuma dessas baselines já havia concluído a tarefa no `step 2`.

Distribuição das 28 falhas críticas:

```text
action/misalignment              9
action/invalid_action            6
planning/constraint_ignorance    6
action/format_error              3
system/environment_error         3
action/parameter_error           1
```

## Exemplo: `generation-0005`

```text
Step 1: get_info_vaga funciona.

Step 2: salvar_formulario recebe 8 perguntas, mas deveria receber exatamente 1.
        O ambiente responde SAVE_FAILED. É uma falha recuperável.

Step 3: o agente corrige o payload para 1 pergunta, mas a injeção troca a tool
        salvar_formulario por salvar_formulario_v2.
        O ambiente responde TOOL_NOT_FOUND. Esta é a falha crítica.
```

Label:

```json
"generation-0005": {
  "step": 3,
  "module": "action",
  "error_type": "invalid_action"
}
```

## Regra para avaliação

O avaliador não deve selecionar automaticamente o primeiro erro visível da
trajetória. Ele deve localizar o **erro crítico não recuperado que explica o
resultado final**.

Essa característica torna o dataset mais realista: ele mede atribuição causal
e capacidade de distinguir uma falha intermediária recuperável de uma falha
terminal.
