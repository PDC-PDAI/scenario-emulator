# Why are some dataset faults at `step 3`?

🇺🇸 **English** · [🇧🇷 Português](step-3-labels.pt-BR.md) · [Home](../README.md)

This is **intentional** and is not an indexing error.

Some original executions had to fix a recoverable problem before finishing the
task. The successful baseline had this flow:

```text
Step 1 → looks up the job successfully
Step 2 → tries to save but gets a recoverable error
Step 3 → fixes the data and saves successfully
```

When building the dataset, the history is preserved and the critical fault is
injected into the terminal step that originally succeeded:

```text
Step 1 → valid lookup
Step 2 → recoverable failure; the agent spots the problem and tries to fix it
Step 3 → injected critical fault; the task can no longer be completed
```

`step 2` does not get the label because it is not the cause of the final
failure: in the original baseline, the agent recovered from that error. The label
points to `step 3`, where the recovery is interrupted by the injected fault.

## Case audit

The 1,100-trajectory dataset has **28 labels at step 3**, derived from
9 baselines:

- 8 baselines tried to save the wrong number of questions and then corrected the
  count;
- 1 baseline used weights above the allowed limit and then corrected them;
- in all 9 baselines, the original `step 3` ran `salvar_formulario` successfully;
- none of these baselines had already completed the task at `step 2`.

Distribution of the 28 critical faults:

```text
action/misalignment              9
action/invalid_action            6
planning/constraint_ignorance    6
action/format_error              3
system/environment_error         3
action/parameter_error           1
```

## Example: `generation-0005`

```text
Step 1: get_info_vaga works.

Step 2: salvar_formulario receives 8 questions, but it should receive exactly 1.
        The environment answers SAVE_FAILED. This is a recoverable failure.

Step 3: the agent fixes the payload to 1 question, but the injection swaps the
        salvar_formulario tool for salvar_formulario_v2.
        The environment answers TOOL_NOT_FOUND. This is the critical fault.
```

Label:

```json
"generation-0005": {
  "step": 3,
  "module": "action",
  "error_type": "invalid_action"
}
```

## Evaluation rule

The evaluator must not automatically pick the first visible error in the
trajectory. It must locate the **unrecovered critical error that explains the
final outcome**.

This makes the dataset more realistic: it measures causal attribution and the
ability to tell a recoverable intermediate failure from a terminal one.

The fixed 300-sample campaign does not have this pattern: it only accepts
baselines without natural errors, so a recovered error never precedes the label.
