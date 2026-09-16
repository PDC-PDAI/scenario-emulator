# Dataset v2: 1.100 falhas + 100 controles bem-sucedidos

🇧🇷 **Português** · [🇺🇸 English](dataset-v2-success-controls.en.md)

Esta versão acrescenta 100 trajetórias reais, sem injeção de falha, aos 1.100
casos publicados na pasta da macro-entrega-3. O conteúdo dos casos e seus
rótulos são preservados, e todos os IDs públicos são substituídos por UUIDs
aleatórios. Os controles combinam baselines originais aprovadas na revisão
e novas execuções reais geradas para repor os casos reprovados.

## Composição e seleção

- 1.100 casos com falha: 100 por tipo de erro, nas 11 classes existentes.
- 100 controles: dez por combinação de tema e senioridade (dez cenários).
- Total: 1.200 trajetórias e 1.200 entradas no arquivo de labels.
- Origem dos controles finais: 87 baselines antigas revisadas e 13 novas execuções.

As 220 baselines originais foram revisadas: 107 passaram pelas verificações
operacionais e de instruções. A aprovação exige ler o pedido do coordenador,
não apenas o campo de contagem que ele gerou. Por exemplo, um pedido de
“uma pergunta” atendido com oito perguntas é reprovado, mesmo com `success=true`.
Também são excluídos erros de ferramentas, retries, foco descumprido, campos
obrigatórios ausentes, tipos inadequados e pesos que violam as instruções.

Novas execuções completam os cenários com menos de dez aprovações. Elas usam o
agente real (`QuestionnaireService`), o modelo `gpt-5-mini` da OpenAI e os prompts
locais versionados, com a quantidade solicitada explicitamente no comando e no
metadado. Não há edição posterior das perguntas para fazer uma execução passar.
O campo privado `generation_source` distingue `original_baseline` e `fresh_run`.

Cada controle tem exatamente dois steps: `get_info_vaga` seguido de
`salvar_formulario`. O filtro verifica o schema, o vínculo entre chamada e
resposta, os argumentos, o ID da vaga, o ID do formulário, a quantidade de
perguntas persistida e a confirmação de sucesso. Usa os `messages` completos
privados para verificar as respostas das ferramentas, pois os registros
históricos de `env_response` do lookup podem estar truncados.

Além das verificações determinísticas, uma chamada separada ao `gpt-5-mini`
revisa o comando, as instruções do sistema, a vaga, as chamadas/respostas de
ferramentas e as perguntas completas. O revisor precisa aprovar quantidade,
cobertura do foco, tipos, pesos e demais restrições. A revisão não recebe o
label esperado como entrada. Dos 100 controles exportados, 99 receberam essa
aprovação pela API e um foi aprovado por revisão direta do conteúdo completo
na sessão de correção. O método de cada caso fica registrado na auditoria.
Há ainda inspeção direta de amostras.

Os controles não têm falha injetada e passaram nessas revisões. Isso não é uma
prova formal de ausência de todo erro: o revisor usa a mesma família de modelo
do gerador e pode compartilhar limitações. Informe essa origem do ground truth
nas métricas. Resultados de auditoria e hashes das fontes são guardados em
`private/control-audit.jsonl`; o exportador recusa controles sem aprovação ou
cujo conteúdo tenha mudado depois da revisão.

## Arquivos para o Drive

Publicar juntos, com nomes de versão novos, mantendo a versão de 1.100 casos:

1. `error-recovery-front-b-v2-1200-inputs.zip`: JSONL embaralhado e os 1.200 JSONs individuais.
2. `error-recovery-front-b-v2-1200-labels.json`: mapa completo de IDs para os labels.
3. `dataset-v2-success-controls.md`: este guia de composição e avaliação.

O ZIP contém somente `front-b-input.jsonl` e `front-b-inputs/`. Os labels ficam
fora do ZIP e devem ser usados apenas pelo avaliador. A proveniência completa
fica em `private/provenance.jsonl` no pacote local; não é entrada do detector.
Baixe o ZIP e os labels atualizados juntos: os IDs antigos foram substituídos
nos dois arquivos. Labels de uma exportação anterior não correspondem à atual.

## Labels v2

O mapa mantém o formato dos rótulos antigos. Para controles, o valor é `null`:

```json
{
  "trajectory-c7d8ed29-7382-45ad-86d0-9c643371005c": null,
  "trajectory-3414d18d-b535-4118-8dc1-2b6b8cb5e28c": {"step": 2, "module": "system", "error_type": "environment_error"}
}
```

`null` significa **nenhuma falha crítica esperada**. Não significa label ausente,
e não deve ser convertido para `others`, step zero ou uma nova classe da
taxonomia. O leitor de labels precisa aceitar `DatasetLabel | None`. Um ID que
não existe no mapa é erro de integridade; um ID presente com valor `null` é controle.
Os IDs acima são ilustrativos. A ordem de gravação das chaves em `labels.json`
é exatamente a ordem das trajetórias no JSONL e das entradas individuais do ZIP.
Mesmo assim, correlacione por ID: leitores JSON podem não preservar a ordem de
um objeto, e ferramentas de extração podem ordenar arquivos pelo nome.

## Como avaliar falsos positivos

O AgentDebug-RH atual encerra a análise quando `success=true`, tanto na pipeline
principal quanto na detecção de causa raiz. Apenas acrescentar controles e
executar a pipeline atual não mede falsos positivos: os controles seriam pulados.

É necessário um modo de avaliação cega que:

1. Execute o detector em todas as trajetórias, incluindo os controles.
2. Não exponha `success`, os labels, a proveniência ou o mapa de IDs ao detector
   nem use essas informações em condições que pulem a análise. O carregador deve
   separar esses metadados da entrada dos modelos; não basta remover `success`
   se seu valor padrão continuar acionando uma suposição de falha.
3. Permita explicitamente a resposta “nenhum erro”, inclusive no prompt e no
   fluxo da causa raiz, sem pressupor que toda trajetória falhou.
4. Use os IDs somente para correlacionar predições e labels após a análise.
5. Conte FP quando um controle receber uma predição de erro e TN quando receber
   “nenhum erro”. Calcule `FPR = FP / (FP + TN)`; se os 100 controles tiverem
   predições válidas, o denominador será 100. Informe à parte falhas de execução
   do detector e cobertura; não conte timeout ou saída inválida como TN.

Defina antes da avaliação se a predição considerada é o alerta do detector de
módulos ou o diagnóstico final de falha crítica; se avaliar ambos, reporte duas
taxas separadas. S, S+M e ALL continuam restritas aos casos com falha anotada.
Nenhuma métrica de falsos positivos foi executada na preparação deste pacote.

Parte dos controles reutiliza baselines que deram origem a casos com falha. Ao criar
splits, mantenha cada controle e todos os casos com o mesmo
`parent_trajectory_id` juntos. As 1.200 trajetórias, os labels e as entradas
individuais do ZIP seguem a mesma permutação, usando a seed `20260907`.
Todos os IDs públicos são UUIDs v4 gerados independentemente da classe, sem
faixas numéricas para casos antigos ou controles. O mapa entre IDs anteriores
e novos fica somente em `private/id-map.json`. A proveniência acompanha os novos
IDs, preservando `parent_trajectory_id` para formar splits por baseline.

## Reprodução local

Os dois arquivos originais foram baixados do Drive e comparados byte a byte
com a exportação local de 1.100 casos. Checksums SHA-256:

- ZIP: `b496cd31d6d0932722ac806db04db9d774b2005b061190ba21821bc19c5d0051`
- Labels: `53c8c95dc7193474ef4f66e3ba8422942846a815fc9ad92c782b3c4707af62d1`

Execute a partir da raiz do repositório, escolhendo um diretório novo:

```bash
.venv/bin/python -m scripts.build_success_controls \
  --source-zip outputs/drive-original-20260907/inputs.zip \
  --labels outputs/drive-original-20260907/labels.json \
  --baseline-dataset outputs/error-recovery-front-b-1100 \
  --audit-dir outputs/control-audit-v2-20260907 \
  --supplemental-dir outputs/control-replacements-20260907 \
  --output-dir outputs/error-recovery-front-b-v2-1200-reviewed \
  --per-group 10 \
  --shuffle-seed 20260907
```

O comando gera `inputs.zip`, `labels.json`, `private/manifest.json`,
`private/provenance.jsonl`, `private/control-audit.jsonl` e `private/id-map.json`. Cada nova execução gera
novos UUIDs aleatórios; arquive o mapa privado e o pacote para reproduzir a
identidade exata de uma exportação. O manifesto registra composição, exclusões,
checksums das fontes e limitações. O comando recusa uma saída já existente e
fontes incompatíveis com as baselines locais.

Os scripts `scripts/audit_success_controls.py` e
`scripts/generate_success_replacements.py` registram a revisão e as novas
execuções em diretórios separados. A auditoria deve ser executada tanto sobre
as baselines originais quanto sobre as substitutas antes de exportar o pacote.
Os JSONs individuais em `front-b-inputs/` são as gerações completas do contrato
público: contêm todos os steps e o payload salvo, sem resumo em Markdown.
