# 0018. Combined scalar + field surrogate models (exportable as a tool)

Date: 2026-06-30
Status: Accepted + **SHIPPED 2026-06-30** (all 7 slices). Build-level spec: `designs/combined-surrogate.md`. Extends ADR-0008 (surrogate family). Built on ADR-0017 (output validity), which landed first.

## Context

A surrogate model **version** is today strictly one `kind` — `'scalar'` **or**
`'field'` — enforced by a DB CHECK, by `create_model` branching, and by a
single-kind on-disk bundle. So a parametric study can yield either a model of its
scalar outputs *or* a model of one field output, never both.

The maintainer wants to fit **one** surrogate over a study that predicts **both**
the scalar outputs (peak failure index, mass, deflection…) **and** the field
output(s) (the 3-D failure-index map), save them as **one** model, and **export
it as a tool** that — run in a workflow — emits all of them, just like the real
tool. This is the surrogate-as-simulation-stand-in use case.

Two structural facts make this tractable: the version's `responses` list already
records a per-response `kind`, and `_add_version` already accepts both a
`surrogates` dict and a `field_surrogate`. The either/or is enforced by branching,
not by the schema's shape.

The hard gap is **export-as-tool for fields**: the exporter today emits only
`type="number"` outputs and the generated tool loads only the scalar bundle —
field export does not exist at all.

## Decision

A surrogate version may be **`kind='combined'`**: it holds **N scalar surrogates
+ M field surrogates** fitted from one source. Maintainer-agreed shape:

1. **Generality:** many scalars **and** many fields (e.g. a temperature field and
   a failure-index field) in one model. Each field carries its own POD bundle and
   reference mesh.
2. **Validity scope:** each output fits on the runs **valid for that output**
   (per-output subset, per ADR-0017), with exclusions reported per output.
3. **`kind`:** add `'combined'` (relax the CHECK); existing `'scalar'`/`'field'`
   models are unchanged.
4. **Sequencing:** ADR-0017 output-validity **first**, so a combined fit never
   silently poisons both halves.
5. **One bundle, both kinds:** a single bundle dir holds a `scalar/` subtree and a
   per-field-namespaced `field/<response>/` subtree, one manifest, one sha256 over
   all artefacts (non-negotiables intact: sha256-verified load, joblib not pickle,
   subprocess export with bundle_path+sha256+version_id baked in).
6. **Export-as-tool emits both:** the synthesised descriptor declares number
   outputs for scalars **and** `file_type="field"` (`.vtu`) outputs for fields;
   the orchestrator loads both, predicts, and **writes the field file(s)** to
   `outputs/` on the baked reference mesh — a true drop-in simulation stand-in.

## Consequences

- One model predicts numbers + fields; the model detail page shows the scalar
  table **and** the 3-D viewer; refit re-fits everything.
- New work: ORM `kind='combined'` + migration; combined bundle read/write;
  `create_model`/`_add_version`/`refit` persist mixed sets; a unified fit form;
  and — the largest piece — **net-new field export** (descriptor field outputs +
  orchestrator that reconstructs and writes `.vtu` + runtime that loads both
  bundles and predicts fields).
- Per-output validity means scalar and field halves may train on different run
  subsets; quality + exclusion counts are reported per output.
- Build sliced in `designs/combined-surrogate.md`; gated on ADR-0017.
