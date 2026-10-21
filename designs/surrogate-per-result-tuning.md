# Per-result surrogate tuning (method × output scale × input scale) — build plan

**Status:** **SHIPPED 2026-09-17**. Decision record: **ADR-0022**
(extends ADR-0021, which shipped the per-result output scale). Scalar results only.

## Problem

ADR-0021 lets each scalar result choose its **output** scale, but a model still
uses **one fitting method** for every result, and inputs are always used as
measured. Benchmarks on every training set in the demo database (outer 5-fold CV,
all choices made on training folds only) showed each restriction costs accuracy:

| Change, each result choosing under the strict rule | Better / same / worse (of 69) | Typical | Worst |
|---|---|---|---|
| Log inputs (Gaussian process only), vs ADR-0021 | 71 / 52 / 15 of 138 GP fits | −22% | 2.28× |
| Method per result, simple "lowest error wins" | 25 / 28 / 16 | — | 3.38× |
| Method per result, strict rule | 20 / 43 / 6 | — | 1.88× |
| **Combined: method × output scale × input scale, strict rule** | **45 / 17 / 7** | **−12%** | **1.68×** |

Finned Cold Plate, 70 runs (combined vs today): mass 0.43 → 0.088 g, fan power
0.087 → 0.045, peak temperature 1.85 → 1.43 °C, mean temperature 2.25 → 2.12 °C,
air velocity unchanged, thermal resistance 0.026 → 0.029 (worse).

Findings that shape the design:
- **The strict rule is what makes it safe.** Picking by a single CV score is noisy;
  requiring "clearly and consistently better" cut losses from 16 to 6–7.
- **Log inputs only help Gaussian processes.** For polynomials they are mixed and
  worse on the cold plate, so they are not offered there.
- **About 40% of choices stay as today.** Switches are mostly to a GP with logged
  inputs and output, or to a polynomial for additive results such as mass.

## 1. The rule (per result)

1. **Baseline ("before tuning")** = the starting method **exactly as measured**.
   Starting method = the one chosen above the Fit & compare table (suggested:
   best before-tuning mean Q²), or the first ticked method for **Build model**.
   Log output is a tuning option like any other. *(Revised: first included the
   ADR-0021 log-output choice, which made "before tuning" show log scales.)*
2. **Candidates** = every ticked method × output scale {as measured, log} ×
   input scale {as measured, log — Gaussian process only}. Up to 12 per result.
   Log output needs all training values > 0; log inputs need the factor to be
   eligible (§3).
3. Fit each candidate once and read its **displayed** CV error (the existing
   `_cross_val_quality`). Only candidates with a lower displayed error than the
   baseline go further, in order of displayed error.
4. The first candidate that is **≥10% better on average over 3 repeated shuffled
   5-fold checks and better in every repeat** (early-stopping) is chosen.
   Otherwise the baseline stays.
5. **Overrides:** the user can pick any evaluated candidate for any result.
6. **Warning (⚠):** a user-selected configuration has a higher displayed error than
   the best evaluated configuration for that result.

## 2. What the user sees

**Build form.** *Methods to try* stays; it now also sets the candidate methods.
The *Output scale* select becomes **Tuning per result** (see Open question 1):

| Option | Behaviour |
|---|---|
| **Automatic** (default) | The rule above. |
| **Off** | One method for every result, as measured — the pre-ADR-0021 behaviour. |

**Fit & compare.** The Q² matrix per method stays (it picks the primary method).
The ADR-0021 *Output scale* table becomes a **per-result tuning table** for the
kept primary method:

| Result | Primary method (today) | MATOFX picks | Your choice | |
|---|---|---|---|---|
| Plate + fin mass (g) | GP Matérn · 0.43 | Polynomial · as measured · 0.088 | [▾ all candidates with their errors] | |
| Peak component temperature (°C) | GP Matérn · log output · 1.85 | GP Matérn · log inputs + output · 1.43 | [▾] | |

The pick carries its reason on hover; the dropdown lists every evaluated candidate
with its error, best first. The parity plot shows each result's chosen
configuration.

**Model page → Scalar fits.** Columns: Result, **Method**, **Scale** (e.g. *log
inputs + output*), who chose it, error in use, error of the primary-method
baseline, ⚠. The version row shows *Mixed methods* when results differ.

**Refit** keeps the primary method, the mode and every user override; MATOFX's own
picks are re-decided on the current runs. The Refit dialog's *Output scale* choice
becomes *Tuning per result* (Keep / Automatic / Off).

## 3. Log inputs — mechanics and guards

- `Surrogate.log_factors: tuple[str, ...]` — numeric factors the model sees as
  `log(x)`. `_encode` applies the log before scaling, so **every caller keeps
  passing values in real units**. `training_inputs`, `ranges`, `means` stay in
  real units (coverage gauge, profiler, exported form unchanged).
  `__setstate__` defaults to `()`.
- **Eligibility:** a factor may be logged only if all training values are > 0
  **and** the tool **declares** a minimum > 0 (no declaration → not eligible, so the
  exported form needs no extra bound). A non-positive value at
  predict time raises a clear `ValueError` ("*Fin count* must be above 0 for this
  model"), never a silent NaN.
- **Coverage (ADR-0020):** `ard_length_scales` for a logged axis are in log units.
  They are used to rank axes for steering suggestions; the plan verifies that
  ranking still points at the right axes on the cold plate model, and converts to
  a local real-unit scale if not.
- `conditioned_on` works unchanged (it goes through `_encode`).

## 4. Consumers that assume one method per model

| Place | Today | Change |
|---|---|---|
| `SurrogateModelVersion.surrogate_method` | the one method | stays = primary method; per-result `method`, `input_scale`, `log_factors` live in `responses`/`quality` JSON. **No migration.** |
| `models/exporter.py` `emit_std` | GP gate on the version's method | per result: `<name>_std` only for results with variance |
| `models/tool_synthesis.py` std outputs | all-or-nothing | per result, matching the exporter |
| `models/runtime.py` `_support_outputs` | sigma from the first scalar surrogate | first surrogate **with variance**; none → status only |
| `coverage/responses.py` `surrogate_method` | model-wide | per response |
| `api/routers/approvals.py`, `cohorts.py`, UI version row | show one method | show primary, or *Mixed methods* |
| `ModelService.compare_fits` | loops methods, returns per-method quality | also returns per-result candidates (errors, reason, chosen); CV arrays only for the chosen config and each method's baseline, to keep the response small |

## 5. Cost — measure first

Up to 12 displayed CVs per result, plus repeats for the few candidates that pass.
Estimates from ADR-0021 timings: 70-run cohort, 6 results, 4 methods ≈ 1–1.5 min;
**373-run model ≈ 4–6 min**, against the in-app 600 s request ceiling.

**Slice 0 measures this on the real 373-run and 70-run models before any UI work.**
If a realistic build exceeds ~2 min, fitting moves to a background task with a
progress indicator (Open question 3).

## 6. Build slices

0. **Timing prototype** on a copy of the demo DB (373-run and 70-run), using the
   benchmark code. Decides whether slice 5 is needed.
1. **Log inputs in the core** (`studies/surrogate/__init__.py`): `log_factors`,
   `_encode`, eligibility, clear error, `__setstate__`. Tests: power-law inputs fit
   better; old bundles unchanged; predict in real units; non-positive input refused.
2. **Selection engine** (`studies/surrogate/tuning.py`, replacing
   `scale.py:fit_with_output_scale`): candidates, strict rule vs baseline,
   `TuningDecision` {method, output_scale, log_factors, source, candidates[],
   reason, warning}. Tests replay benchmark shapes (mass → polynomial, power law →
   GP log in+out, noisy near-ties → stay).
3. **Service + API**: compare/create/refit carry per-result tuning and overrides;
   §4 consumers. Tests: round trip, refit keeps overrides and primary method,
   exported tool mixes `_std` outputs correctly, support sigma from a GP result.
4. **UI**: form control, per-result tuning table (override dropdown, live ⚠),
   model page columns, *Mixed methods* label, Refit dialog. Browser check on a DB
   copy.
5. **Background fit with progress** — only if slice 0 says so.
6. **Docs**: help chapter (*Tuning per result*, replacing *Output scale*),
   ADR-0022, ARCHITECTURE, ROADMAP.
7. **Final benchmark of the shipped code** (single process, resumable), reported
   like the combined test.

## Out of scope

Fields; the optimisation MOP auto-metamodel and study profiler; log with a baseline
(`log(T − T_ambient)`); physics-informed models; ensembles.

## Decisions (2026-09-17)

1. Build form: **Automatic / Off**.
2. Warning: ⚠ **whenever a user-selected configuration has a higher displayed error
   than the best evaluated configuration** — any amount (replaces the 1.5× rule of
   §1.6 for overrides; MATOFX's own picks never warn).
3. Slow builds: **background fitting with progress** (slice 5 is in scope).
4. Decision record: **ADR-0022**.
