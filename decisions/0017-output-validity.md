# 0017. Output validity — physicality checks across studies and surrogates

Date: 2026-06-30
Status: Accepted (design discussion with the maintainer, 2026-06-30). **Design only — partial code landed.** Build-level spec: `designs/output-validity.md`. Supersedes nothing; extends ADR-0001 (parametric studies) and ADR-0008 (surrogate family).

## Context

A tool run can be marked **Complete** while its outputs are non-physical — `NaN`,
`Inf`, or finite-but-absurd values (e.g. a dimensionless failure index of
`1e304`). Nothing currently notices, so the garbage propagates to every consumer
of the result: the parametric-study results matrix, sensitivity, the **scalar**
surrogate fit, the **field** surrogate fit, and optimisation objectives.

This surfaced building a failure-index **field** surrogate from a Bracket Stress
parametric study (study 12, 2026-06-30): ~74 of 250 runs produced non-finite or
~`1e304` field values, so the POD `np.linalg.svd` diverged and the UI showed only
a generic "Something went wrong" — no server traceback, nothing persisted. The
runs were marked Complete; the bracket_stress finite-element solve was returning
garbage for ill-conditioned design points instead of failing.

Symptom fixes already landed (the field-fit now drops non-finite snapshots,
guards SVD non-convergence and squared-singular-value overflow, and raises a
clear error; the bracket_stress tool now fails a non-finite / blown-up solve).
But the systemic gap is the absence of any **notion of output validity**.

## Decision

Introduce **one shared validity concept**, decided once at the single point where
results are recorded and respected by every consumer. Maintainer-agreed shape:

1. **Define (declarative, in the descriptor).** Every numeric and field output
   carries an implicit **finiteness floor** (rejects `NaN`/`Inf`; opt-out via
   `allow_non_finite`). Outputs may additionally declare a physical range:
   scalar `min`/`max`; field `value_min`/`value_max` applied **per node**.

2. **Detect + store (the chokepoint).** At result-record time
   (`record_study_result`), evaluate the run's scalars against the descriptor,
   compute a **validity verdict + reasons**, and **store them on `StudyResult`**
   (migration). This covers parametric / optimisation / cohort at once.
   - A field output's validity rides cheaply on its **companion scalar** (e.g.
     `fi_max` beside `failure_field`) — no mesh file is loaded at record time.
   - Per-node field `value_min`/`value_max` (and finiteness) are enforced at
     **ingest**, when each snapshot is loaded anyway, as the second layer.

3. **Two outcomes.** A tool that gives up (no usable output) → run **Failed**
   (the bracket_stress solve guard). A tool that emits invalid numbers → run
   stays **Complete** but is **flagged invalid**, with reasons, data retained.

4. **Display (parametric studies).** The results matrix shows a per-run **health
   badge** and a study-level summary (extending the D3 partial-data banner).

5. **Consume (scalar + field + sensitivity + optimisation).** Invalid runs are
   **excluded by default** with **visible counts** ("fitted on 176 of 250; 74
   excluded — 67 out-of-bounds, 7 non-finite"); a clear error when too few
   remain. Optimisation treats an invalid result as **infeasible**.

6. **Backfill.** A one-off pass recomputes validity from each existing
   `StudyResult`'s stored scalars + current descriptor bounds, so existing
   studies (e.g. study 12, whose `fi_max` is already `NaN`/`1e304`) light up
   without re-running.

## Consequences

- Garbage is caught the instant a run finishes, made visible at the study level,
  and silently-but-transparently excluded from both surrogate kinds — instead of
  mis-fitting, crashing cryptically, or producing a meaningless "successful"
  model.
- New schema: `study_result.validity` + `validity_reasons` (migration 0085);
  new descriptor output fields (`min`/`max`/`value_min`/`value_max`/
  `allow_non_finite`) with the descriptor-field touchpoints (validator + tests).
- The finiteness floor is free for tool authors; physical bounds are opt-in.
- Domain knowledge lives in the descriptor (symmetric with input bounds), not in
  consumer code.
- Build is sliced in `designs/output-validity.md`; partial hardening (field fit,
  bracket_stress guard) already shipped 2026-06-30.
