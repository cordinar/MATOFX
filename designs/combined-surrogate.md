# Combined scalar + field surrogate — build spec

Build-level spec for **ADR-0018**. Read the ADR first. Extends
`designs/surrogate-family.md`. **Gated on ADR-0017 (output validity).**

## Goal

One surrogate model, fitted from one parametric study (or cohort), that predicts
**N scalar outputs + M field outputs**, and can be **exported as a tool** that
emits all of them (numbers + `.vtu` field files) — a fast drop-in stand-in for
the real tool inside workflows.

## Current either/or (what we relax)

- `SurrogateModelVersion.kind` — CHECK `IN ('scalar','field')` (`models/orm.py`).
- `responses` JSON already carries per-response `kind` → can list mixed.
- `_add_version` already takes both `surrogates` and `field_surrogate`; the
  `kind=` line + bundle-write branch force one kind (`models/service.py`).
- Bundle is one `scalar/` **or** one `field/` subtree (`models/bundle.py`).
- Export emits only `type="number"`; runtime loads only the scalar bundle;
  **field export does not exist** (`models/exporter.py`, `tool_synthesis.py`,
  `runtime.py`).

## Data model (`models/orm.py`, migration)

- `kind`: allow `'combined'` (relax CHECK to `IN ('scalar','field','combined')`).
- A combined version's `responses` is a mixed list:
  `[{name, kind:"scalar"}, …, {name, kind:"field", file_format, field_node_array_name, time_resolved, …}, …]`.
- `quality`: a combined dict — per-scalar `{q2, rmse_cv, n}` and per-field
  `{modes_retained, mode_energy, recon_rmse, n}` keyed by response name.
- `input_constants`, `surrogate_method`, `tool_version_ids` unchanged.

## Bundle (`models/bundle.py`)

One bundle dir, both subtrees, one manifest, one sha256 over **all** artefacts:

```
<bundle_dir>/
  manifest.json                 # kind:"combined"; lists scalar + every field
  scalar/
    surrogate_<response>.joblib # one per scalar response
  field/
    <field_response>/           # namespaced per field output
      mu.npy
      modal_basis.npy
      mode_energy.npy
      mode_<m>.joblib
      reference_mesh.<ext>      # per-field geometry
    <other_field_response>/ …
```

- `write_combined_bundle()` / `load_combined_bundle()` (or extend the existing
  writers to compose). sha256 covers scalar + all field subtrees.
- Back-compat: existing scalar-only / field-only bundles still load via their
  current readers; `kind` selects the loader.

## Fit + persist (`models/service.py`)

- `create_model` accepts scalar `response_names` **and** a list of field specs
  (`field_specs`), instead of the single `field_spec`.
- **Per-output valid subset (ADR-0017):** each scalar response fits via
  `collect_training_data` filtered to runs valid for *that* response; each field
  fits via `collect_field_training_data` filtered to runs valid for *that* field
  (stored validity + per-node `value_min`/`value_max` at ingest). Report
  excluded counts per output.
- Fits run offloaded (`asyncio.to_thread`, already in place for both paths).
- `_add_version`: drop the either/or; set `kind` = `combined` when both present
  (else `scalar`/`field` as today); write the combined bundle.
- `refit`: re-fit every scalar + field response from the latest version's
  response set.

## Prediction (`models/service.py`, `api/routers/models.py`)

- A combined predict returns `{scalars: {name: {value, std?}}, fields: {name:
  nodal_array (+ std, mode coeffs)}}`. Existing `field_predict` /
  `field_geometry` endpoints keep working (now valid for `combined` versions, not
  just `field`); add scalar predict access for combined.
- Model detail UI: the scalar quality/predict table **and** one 3-D viewer per
  field response.

## Export-as-tool (the crux — `models/exporter.py`, `tool_synthesis.py`, `runtime.py`)

This is net-new for fields.

- **Descriptor** (`synthesise_descriptor`): scalar responses → `type="number"`
  outputs (+ optional `_std`); **field responses → `type="file"`,
  `file_type="field"`, `filename="outputs/<response>.vtu"`, `field_format`,
  `field_node_array_name`** (and validity bounds from ADR-0017 carried through).
- **Runtime** (`runtime.py`): extend `ModelRuntime` to hold `field_surrogates`
  and a `predict_field(name, point)`; add a `load_combined_model_version()` that
  loads both subtrees (sha256-verified).
- **Orchestrator** (`synthesise_orchestrator`): load combined bundle; predict
  scalars → number outputs; for each field, reconstruct the nodal array and
  **write `outputs/<response>.vtu`** on the baked reference mesh → file output.
  Runs in subprocess, no DB, `bundle_path`+`sha256`+`version_id` baked in; joblib
  only.
- Result: the exported tool is a true stand-in — drop it into a workflow and it
  produces the same numbers + field files as the real tool, far faster.

## UI (`ui/pages/models.py`)

- One fit form: pick scalar responses (+ methods to **Fit & compare**, scalar
  only — POD has no method choice) **and** field outputs (tagged "— 3D field").
- Flow: optional scalar method bake-off, then one **Build model** action that
  persists the chosen scalar method + all selected fields as one combined model.
  Reconciles today's split "Fit & compare" / "Fit field surrogate" buttons.
- Show per-output exclusion counts (ADR-0017) before/after building.

## Slicing

1. ORM `kind='combined'` + migration (relax CHECK).
2. Combined bundle read/write (both subtrees, per-field namespacing, one sha256).
3. Service: `create_model`/`_add_version`/`refit` persist mixed sets; per-output
   valid subset + exclusion reporting.
4. Predict + model-detail UI (scalar table + per-field viewer).
5. **Export-as-tool**: descriptor (number + field-file outputs), orchestrator
   (write `.vtu`), runtime (combined load + `predict_field`).
6. Fit-form UI: unified selection + build flow.
7. Help docs (user: "predict numbers and fields from one model, export as a
   tool"; admin: combined export notes).

## Dependencies & constraints

- **ADR-0017 first** — combined fits rely on per-output validity to avoid
  poisoning both halves.
- Non-negotiables intact: sha256-verified bundle load; joblib not raw pickle;
  subprocess export with everything baked in.

## Open items

- Multi-field reference meshes: fields may share a mesh or differ — bundle stores
  one per field; dedupe only if identical (optional optimisation).
- Exported-tool field output naming when M>1 (one `.vtu` per field response —
  default `outputs/<response>.vtu`).
- Whether to keep `field_spec` (singular) as a thin shim over `field_specs` for
  back-compat with the current single-field create path.
