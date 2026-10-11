# Dataset v2: 1,100 failures + 100 successful controls

🇺🇸 **English** · [🇧🇷 Português](dataset-v2-success-controls.pt-BR.md) · [Home](../README.md)

This release adds 100 real trajectories, without fault injection, to the 1,100
cases published in the macro-delivery-3 folder. The content and labels of those
cases are preserved, and every public ID is replaced by a random UUID. The
controls combine original baselines approved in the review with fresh real
executions generated to replace the rejected ones.

The counts below describe the recorded campaign. Reproducing its analysis needs
the saved artifacts, which a clean clone does not include.

## Composition and selection

- 1,100 failure cases: 100 per error type, across the 11 existing classes.
- 100 controls: ten per theme and seniority combination (ten scenarios).
- Total: 1,200 trajectories and 1,200 entries in the labels file.
- Origin of the final controls: 87 reviewed original baselines and 13 fresh
  executions.

The 220 original baselines were reviewed: 107 passed the operational and
instruction checks. Approval requires reading the coordinator's request, not
just the count field it generated. For example, a request for “one question”
answered with eight questions is rejected, even with `success=true`. Tool errors,
retries, missed focus, missing required fields, inappropriate types and weights
that violate the instructions are also excluded.

Fresh executions complete the scenarios with fewer than ten approvals. They use
the real agent (`QuestionnaireService`), OpenAI's `gpt-5-mini` and the versioned
local prompts, with the requested count stated explicitly in the command and in
the metadata. Questions are never edited afterwards to make an execution pass.
The private `generation_source` field distinguishes `original_baseline` from
`fresh_run`.

Each control has exactly two steps: `get_info_vaga` followed by
`salvar_formulario`. The filter checks the schema, the link between call and
response, the arguments, the job ID, the questionnaire ID, the persisted question
count and the success confirmation. It uses the full private `messages` to check
the tool responses, because historical lookup `env_response` records may be
truncated.

Beyond the deterministic checks, a separate `gpt-5-mini` call reviews the
command, the system instructions, the job, the tool calls and responses, and the
complete questions. The reviewer must approve the count, focus coverage, types,
weights and remaining constraints. The review does not receive the expected label
as input. Of the 100 exported controls, 99 got this approval through the API and
one was approved by a direct review of the complete content during the correction
session. Each case's method is recorded in the audit. Samples were also inspected
directly.

The controls have no injected fault and passed these reviews. That is not a
formal proof that they contain no error at all: the reviewer uses the same model
family as the generator and may share its limitations. Report this origin of the
ground truth with the metrics. Audit results and source hashes are kept in
`private/control-audit.jsonl`; the exporter refuses controls without approval or
whose content changed after the review.

## Files for the Drive

Publish together, with new versioned names, keeping the 1,100-case release:

1. `error-recovery-front-b-v2-1200-inputs.zip`: the shuffled JSONL and the 1,200
   individual JSONs.
2. `error-recovery-front-b-v2-1200-labels.json`: the complete map from IDs to labels.
3. `dataset-v2-success-controls.md`: this composition and evaluation guide.

The ZIP contains only `front-b-input.jsonl` and `front-b-inputs/`. The labels stay
outside the ZIP and must be used only by the evaluator. Full provenance stays in
`private/provenance.jsonl` in the local package; it is not detector input.
Download the ZIP and the updated labels together: the old IDs were replaced in
both files, so labels from a previous export do not match the current one.

## v2 labels

The map keeps the format of the old labels. For controls, the value is `null`:

```json
{
  "trajectory-c7d8ed29-7382-45ad-86d0-9c643371005c": null,
  "trajectory-3414d18d-b535-4118-8dc1-2b6b8cb5e28c": {"step": 2, "module": "system", "error_type": "environment_error"}
}
```

`null` means **no expected critical failure**. It does not mean a missing label,
and it must not be converted to `others`, step zero or a new taxonomy class. The
label reader must accept `DatasetLabel | None`. An ID absent from the map is an
integrity error; an ID present with a `null` value is a control. The IDs above are
illustrative. The key order in `labels.json` is exactly the order of the
trajectories in the JSONL and of the individual ZIP entries. Even so, join by ID:
JSON readers may not preserve object order, and extraction tools may sort files
by name.

## Blind evaluation

The current AgentDebug-RH stops the analysis when `success=true`, both in the
main pipeline and in root-cause detection. Just adding controls and running the
current pipeline does not measure false positives: the controls would be skipped.

A blind evaluation mode must:

1. Run the detector on every trajectory, including the controls.
2. Not expose `success`, the labels, the provenance or the ID map to the detector,
   nor use that information in conditions that skip the analysis. The loader must
   keep that metadata out of the model input; removing `success` is not enough if
   its default value still triggers a failure assumption.
3. Explicitly allow the answer “no error”, in the prompt and in the root-cause
   flow, without assuming that every trajectory failed.
4. Use the IDs only to join predictions and labels after the analysis.
5. Count an FP when a control gets an error prediction and a TN when it gets
   “no error”. Compute `FPR = FP / (FP + TN)`; if all 100 controls get valid
   predictions, the denominator is 100. Report detector execution failures and
   coverage separately; never count a timeout or invalid output as a TN.

Decide before the evaluation whether the counted prediction is the module
detector's alert or the final critical-failure diagnosis; if you evaluate both,
report two separate rates. S, S+M and ALL remain restricted to the cases with an
annotated failure. No false-positive measurement was run while preparing this
package.

Some controls reuse baselines that gave rise to failure cases. When creating
splits, keep each control together with every case that shares its
`parent_trajectory_id`. The 1,200 trajectories, the labels and the individual ZIP
entries follow the same permutation, with seed `20260907`. Every public ID is a
UUID v4 generated independently of the class, with no numeric ranges for old
cases or controls. The map between old and new IDs lives only in
`private/id-map.json`. The provenance follows the new IDs and preserves
`parent_trajectory_id` for baseline-level splits.

## Local reproduction

The two original files were downloaded from the Drive and compared byte for byte
with the local 1,100-case export. SHA-256 checksums:

- ZIP: `b496cd31d6d0932722ac806db04db9d774b2005b061190ba21821bc19c5d0051`
- Labels: `53c8c95dc7193474ef4f66e3ba8422942846a815fc9ad92c782b3c4707af62d1`

Run from the repository root, with the historical artifacts available, choosing
a new directory:

```bash
uv run python -m scripts.build_success_controls \
  --source-zip outputs/drive-original-20260907/inputs.zip \
  --labels outputs/drive-original-20260907/labels.json \
  --baseline-dataset outputs/error-recovery-front-b-1100 \
  --audit-dir outputs/control-audit-v2-20260907 \
  --supplemental-dir outputs/control-replacements-20260907 \
  --output-dir outputs/error-recovery-front-b-v2-1200-reviewed \
  --per-group 10 \
  --shuffle-seed 20260907
```

The command writes `inputs.zip`, `labels.json`, `private/manifest.json`,
`private/provenance.jsonl`, `private/control-audit.jsonl` and
`private/id-map.json`. Every run generates new random UUIDs; archive the private
map and the package to reproduce the exact identity of an export. The manifest
records composition, exclusions, source checksums and limitations. The command
refuses an existing output directory and sources that do not match the local
baselines.

`scripts/audit_success_controls.py` and `scripts/generate_success_replacements.py`
record the review and the fresh executions in separate directories. Run the audit
on both the original baselines and the replacements before exporting the package.
The individual JSONs in `front-b-inputs/` are the complete generations of the
public contract: they hold every step and the saved payload, with no Markdown
summary.
