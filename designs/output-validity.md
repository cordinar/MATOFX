# Output validity — build spec

Build-level spec for **ADR-0017**. Source of truth for the work; read the ADR
first for the decision and rationale. Extends `designs/parametric-studies.md`,
`designs/studies-engine.md`, and `designs/surrogate-family.md`.

## Problem

A run can be **Complete** with non-physical outputs (`NaN`, `Inf`, or
finite-but-absurd magnitudes). There is no notion of output validity, so every
consumer — the study results matrix, sensitivity, the scalar surrogate, the
field surrogate, and optimisation objectives — inherits the garbage. Discovered
on Bracket Stress study 12 (≈74/250 runs non-finite or ~`1e304`), which made the
field POD SVD diverge with only a generic UI error.

## Principle

**One validity verdict, decided once at the record-time chokepoint, stored on the
shared result row, honoured by every consumer with visible counts.** Everything
reads `StudyResult`; if validity is stamped there it is inherited for free across
parametric / optimisation / cohort sources.

## 1. Define — descriptor (`tools/descriptor.py`, `OutputField`)

New optional fields on an output:

| Field | Applies to | Meaning |
|---|---|---|
| `allow_non_finite: bool = False` | numeric, field | Opt **out** of the finiteness floor. Default off → `NaN`/`Inf` is invalid. |
| `min: float \| None` | numeric scalar | Lower physical bound (inclusive). |
| `max: float \| None` | numeric scalar | Upper physical bound (inclusive). |
| `value_min: float \| None` | field | Per-node lower bound, enforced at ingest. |
| `value_max: float \| None` | field | Per-node upper bound, enforced at ingest. |

Rules (descriptor before-validator, with tests — these are the known
descriptor-field touchpoints):
- `min`/`max` only on `type == "number"`; `value_min`/`value_max` only on a
  `file_type == "field"` output. Cross-type use is a validation error.
- `min <= max` and `value_min <= value_max` when both present.
- Finiteness floor is implicit for numeric + field outputs; `allow_non_finite`
  is the only escape hatch.
- Symmetric with input bounds — domain knowledge lives in the descriptor.

## 2. Detect + store — record time (`studies/results_service.py:record_study_result`)

The single chokepoint already holds the `tool` (→ descriptor) and the extracted
scalars. After `_extract_scalars`:
- evaluate each numeric scalar against finiteness + `min`/`max`;
- for each declared **field** output, evaluate its **companion scalar** if one is
  declared/derivable (e.g. `fi_max` beside `failure_field`) — this flags a bad
  field run **without loading the mesh file**;
- build `validity_reasons` (list of `{output, code, detail}`, codes e.g.
  `non_finite`, `below_min`, `above_max`) and set `validity` =
  `valid` | `invalid`.

Two outcomes (ADR decision "both"):
- **Tool-fatal** (no usable output, e.g. a diverged solve) → the tool raises and
  the run is **Failed** via the existing failure path. *(bracket_stress solve
  guard already does this.)*
- **Emitted-but-invalid** → run stays **Complete**, `validity='invalid'`, data
  retained.

## 3. Store — schema (`studies/orm.py`, migration 0085)

Add to `study_result`:
- `validity: str` — `valid` | `invalid` (default `valid`; NOT NULL).
- `validity_reasons: JSON` — list of reason objects (empty when valid).

Lives on the unified store → covers parametric / optimisation / cohort. The lazy
cohort populate (`populate_study_results`) stamps the same fields.

## 4. Display — parametric studies (`studies/results_service.py:ResultsService`, matrix UI)

- `StudyChildResult` gains `validity` + `validity_reasons`.
- Matrix shows a per-run **health badge** (valid / non-finite / out-of-bounds)
  and a study-level summary ("176 of 250 valid; 74 invalid"). Extends the
  existing D3 partial-data banner.

## 5. Consume — surrogates, sensitivity, optimisation

Shared policy, identical UX for scalar and field:
- **Scalar** (`studies/surrogate/ingest.py:collect_training_data`): filter to
  `validity == 'valid'`; return excluded count + reason breakdown.
- **Field** (`collect_field_training_data`): two layers —
  1. pre-filter on stored `validity` (cheap, no file load);
  2. at snapshot load, enforce field `value_min`/`value_max` **per node** and
     finiteness, dropping any snapshot with an out-of-range/non-finite node.
  The already-shipped backstops (drop non-finite snapshots, SVD-non-convergence
  + overflow guards in `field.py`) stay as the last line.
- Services (`compare_fits`, `create_model`, `_fit_responses`,
  `_fit_field_response`) surface "fitted on N of M; K excluded (J non-finite, L
  out-of-bounds)" through the API to the UI; clear error when too few remain.
- **Optimisation**: an invalid result → **infeasible** evaluation (consistent
  with the runaway-job/infeasible direction).

## 6. Backfill (one-off)

A CLI/maintenance pass recomputes `validity` for existing `StudyResult` rows from
their **stored scalars** + current descriptor bounds. Field validity backfills
from the companion scalar (study 12's `fi_max` is already `NaN`/`1e304`), so
existing studies light up without re-running. Per-node field backfill is not
attempted offline (files may be gone); ingest handles it live.

## Slicing

1. Descriptor schema (`min`/`max`/`value_min`/`value_max`/`allow_non_finite`) +
   validator + descriptor tests.
2. Migration 0085 + `record_study_result` evaluation (covers all source kinds).
3. Consumers: scalar + field ingest filtering + excluded-count reporting
   (service → API → UI). *(field.py backstops already shipped.)*
4. Study results matrix health badge + summary.
5. Backfill command + optimisation-infeasible wiring.
6. Help docs — admin: descriptor output bounds; user: "invalid runs excluded"
   note in cohorts-and-surrogates (the field note already added 2026-06-30).

## Already shipped (2026-06-30, ahead of the full build)

- `studies/surrogate/field.py`: drop non-finite snapshots, guard SVD
  non-convergence + squared-singular-value overflow → clear `FieldSurrogateError`
  (→ `ModelConfigError` → HTTP 400 detail).
- `example_tools/bracket_stress/bracket_stress.py`: fail the run on a non-finite
  / blown-up FE solve (`_MAX_PHYSICAL_DISP_M`).
- `ui/api_client.py`: in-process client timeout 5s → 600s (slow fits were being
  cancelled). `models/service.py`: scalar + field fits offloaded via
  `asyncio.to_thread`.

## Open items

- Companion-scalar discovery for field outputs: explicit descriptor link vs
  convention (a `*_max`/peak naming). Lean explicit (a descriptor field naming
  the field output's summary scalar) to avoid magic.
- Whether `validity` should be a 3-state (`valid`/`invalid`/`unknown`) for
  pre-0085 rows before backfill runs. Lean default `valid` + backfill.
