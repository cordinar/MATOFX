# Surrogate output scale (log) — build spec

**Status:** **SHIPPED 2026-09-16** (all 6 slices). Decision record: **ADR-0021**.
Extends `designs/surrogate-family.md` (ADR-0008) and `designs/combined-surrogate.md`
(ADR-0018). Scalar results only.

## Problem

A scalar surrogate learns each result exactly as measured. Results that behave
like a product of powers of the inputs (temperature rise, thermal resistance,
stress, deflection, pressure drop, power) curve steeply into one corner of the
design space. A Gaussian process has one set of length scales for the whole
space, so it compromises: when the Finned Cold Plate cohort grew from 20 to 70
runs (40–131 °C), the peak-temperature fit got *looser* in the cool region the
user tested (cross-validated RMSE 2.98 °C; misses of 2.2 and 3.8 °C on jobs
5523 and 3241).

Fitting `log(y)` and predicting `exp(·)` turns the power law into a near-flat
sum, weights errors as percentages, and cannot predict a negative value.

## Evidence (benchmark on every existing fit, 2026-09-16)

5-fold CV over the demo DB's existing surrogate training sets. "Auto" = the rule
in §3, decided on the training folds only.

| Method | Auto: better / same / worse (>5%) | Auto worst | Auto mean change | Log always: worse | Log always: worst |
|---|---|---|---|---|---|
| GP (smooth + Matérn), 82 fits | 34 / 42 / 6 | 1.55× | −13% | 32 | 163× |
| Polynomial, 69 fits | 25 / 42 / 2 | 1.07× | −19% | 26 | ~4×10¹⁴× |
| Gradient boosting, 69 fits | 9 / 59 / 1 | 1.05× | −3% | 10 | 1.96× |

Conclusions that shape the design:
- **Never a global switch.** Always-on log is catastrophic for additive results
  (mass: up to 10,843× worse with a polynomial).
- **Per result, measured.** The choice is made per result from cross-validated
  error, not from the user's intuition.
- **Visible and overridable,** because the measured choice is still wrong
  occasionally (6 GP fits).
- Best Finned Cold Plate peak-temperature fit was *polynomial + log* (1.40 °C):
  scale and method interact, so the comparison must cover both.

## 1. User flow

1. **Build form** (models page, scalar column, under *Methods to try*):
   **Output scale** — `As measured` / `Log` / `Both` (default `Both`).
2. **Fit & compare.** The existing Q² matrix ranks methods using each result's
   *chosen* scale. Below it, a new **Output scale** table for the kept scalar
   method (re-renders when *Keep scalar method* changes):

   | Result | Error as measured | Error on log scale | MATOFX picks | Your choice | |
   |---|---|---|---|---|---|
   | Peak component temperature (°C) | 2.98 | 1.85 | Log | [Log ▾] | |
   | Mass (g) | 0.43 | 1.71 | As measured | [Log ▾] | ⚠ 4.0× worse on log scale |

   - Errors are cross-validated RMSE in the result's own unit, labels via the
     descriptor (never internal names).
   - *MATOFX picks* carries a one-line reason on hover (e.g. "Log was lower on
     average but not in every repeat — kept as measured").
   - *Your choice* defaults to the pick. Overriding marks the row "chosen by you".
   - `As measured` mode: table not shown. `Log` mode: table shown with every
     positive result forced to Log (pick column reads "Log (your setting)").
3. **Build model** (no compare): the mode is applied directly — `Both` uses the
   automatic picks; warnings appear on the model page instead.
4. **Model page → Scalar fits card:** new *Scale* column (`As measured` / `Log`),
   source (`auto` / `you`), both errors, and the ⚠ chip where it applies.
5. **Refit** keeps the version's mode; *automatic* picks are re-decided on the
   new data, *user overrides* are carried forward.

## 2. Safeguard (warning)

A row warns when the **scale in use** has a cross-validated RMSE more than
**1.5×** the other scale's (`_SCALE_WARN_RATIO = 1.5`). This covers:
- `Log` mode forcing a result that fits worse on a log scale;
- a user override in the table;
- (never for an automatic pick, which by construction only selects log when it
  is ≥10% better).

The warning is advisory, not blocking, and is recorded with the version so it
also shows on the model page and in the audit payload. Computing it needs both
scales, so **`Log` mode fits both** (same cost as `Both`).

Results that can't be logged (any training value ≤ 0) stay as measured with the
note "Log scale isn't possible: some runs are zero or negative." — in every mode.

## 3. Automatic rule (`Both`)

Per result, per method, on the result's valid training rows:
1. If any `y <= 0` → as measured.
2. If the displayed CV RMSE on the log scale is not lower → as measured (no repeats
   run). *Added during the build:* on real data the repeats alone picked log for
   fan power while the table showed log's error as the larger one.
3. Repeated shuffled 5-fold CV (seeds 10/11/12), one seed at a time, both scales,
   RMSE in original units; stop at the first repeat log loses.
4. Pick log iff it won every repeat **and** `mean(rmse_log) < 0.9 × mean(rmse_measured)`.

The *displayed* errors are the existing single CV (`_cross_val_quality`: LOO for
n ≤ 10, else 5-fold seed 0) on each scale, so they match today's `rmse_cv`. The
rule uses the repeats; the hover reason explains any disagreement.

**Cost (measured):** about 3× as measured — 373-run Matérn, 3 results: 65 s vs
21 s; 70-run cohort, 3 methods × 6 results: 28 s. Off the event loop
(`asyncio.to_thread`); in-app request ceiling 600 s.

## 4. Build slices

### Slice 1 — fitting core (`studies/surrogate/__init__.py`)
- `Surrogate.output_scale: Literal["as_measured", "log"]`; `__setstate__`
  defaults `"as_measured"` → every existing bundle loads and predicts unchanged
  (bundle sha is of the file, which isn't rewritten).
- `fit_surrogate(..., output_scale="as_measured")`: log fits `np.log(y)`;
  `_cross_val_quality` computes Q²/RMSE and `predicted` **in original units**
  (wrap in `TransformedTargetRegressor(func=np.log, inverse_func=np.exp,
  check_inverse=False)` for CV). Refuse log with a clear message if any `y <= 0`.
- `predict`: log → mean `exp(μ)` (the median; consistent with CV predictions);
  GP std by delta method `exp(μ)·σ`.
- Coverage (`y_scale` / `coverage/gp.py:_relative_sigma_one`): for log fits the
  latent σ is already relative; add `Surrogate.relative_sigma(points)` returning
  `σ_latent / std(log y)` for log and today's `σ / std(y)` otherwise, and call
  it from `gp.py` so support thresholds keep their calibration.
- `conditioned_on`: build the frozen GP on `log(actual)` for log fits.
- Unaffected by design: `sobol_indices`, `optimize_surrogate`, BO acquisition
  (they consume `predict`).

Tests: power-law data → log beats as measured; linear data → as measured wins;
`y <= 0` refuses log; old pickle (no attribute) predicts identically; relative
sigma unchanged for as-measured fits; conditioned_on on a log fit; CV
predictions in original units.

### Slice 2 — decision function (pure)
Built as `studies/surrogate/scale.py:fit_with_output_scale(factor_columns, response,
*, kind, mode, override) -> (Surrogate, ScaleDecision)`. Encodes §2 and §3. Unit tests replay the
benchmark cases (model_21 t_source → log; mass → as measured; a forced-log mass
→ warning).

### Slice 3 — service + API
- `ModelCreateRequest` / `ModelCompareRequest`: `output_scale_mode:
  Literal["as_measured","log","both"]`; create also takes `output_scales:
  dict[str, Literal["as_measured","log"]]` (overrides from the table).
  `ModelRefitRequest` gains an optional `output_scale_mode` (None = keep) and its
  `surrogate_kind` now defaults to None (keep) — see *Found during the build*.
- `compare_fits`: per method, per response, both scales' quality + the
  decision; `mean_q2` / `best_overall` / `best_per_response` use the chosen
  scale. Schema: `ModelMethodFit.quality[resp]` gains `output_scale`,
  `scale_source`, `rmse_as_measured`, `rmse_log`, `q2_as_measured`, `q2_log`,
  `scale_reason`, `scale_warning`.
- `_fit_responses` / `_fit_all` / `refit`: thread mode + overrides; record on
  each scalar response row in `version.responses`: `output_scale`,
  `output_scale_source`, `output_scale_mode`; and in `version.quality[resp]` the
  same fields as compare. **No migration** (both are JSON columns).
- Audit `models.model.create` / refit payloads include per-result scales and
  warnings.

Tests: router round-trips; refit keeps overrides and re-decides auto picks;
compare ranking uses the chosen scale; unknown override name rejected.

### Slice 4 — UI (`ui/pages/models.py`)
- Build form radio *Output scale*, with a one-line hint.
- `_render_compare_panel`: Output scale table bound to *Keep scalar method*,
  per-row override select, ⚠ chip + tooltip; `_keep` sends `output_scales`.
- Overlaid parity plot shows each method on its chosen scale.
- Scalar fits card / `_render_quality`: Scale, source, both errors, ⚠.
- Labels only, never internal names (existing `resp_display`).

Verify in the running app (Finned Cold Plate 70-run cohort): table values match
the benchmark; override to log on mass shows the warning; model page shows it.

### Slice 5 — exported tool + runtime
Nothing to change in `models/runtime.py` (the bundle pickles the `Surrogate`);
add tests that an exported log fit returns the same value as the in-app
prediction, `<name>_std` is the delta-method σ, and `support_sigma` uses the
latent relative σ.

### Slice 6 — docs (ships with the code)
- `docs/user/90-cohorts-and-surrogates.md`: new *Output scale* section (what it
  is, when log helps, how MATOFX picks, the warning, refit behaviour), and the
  build-form + Scalar fits wording.
- `decisions/0021-surrogate-output-scale.md`, `ARCHITECTURE.md` (v2 surrogate
  sub-section), `ROADMAP.md`.

## Out of scope
- **Fields** (POD coefficients change sign; log doesn't apply).
- MOP auto-metamodel (`studies/optimisation/mop.py`) and the parametric-study
  profiler (`ui/pages/studies.py`): stay as measured for now.
- Log with a user-supplied baseline, e.g. `log(T − T_ambient)` (1.60 vs 1.85 °C
  in the benchmark) — a possible follow-up.
- Different *methods* per result (today one scalar method per version).
- `ModelRefitRequest.surrogate_kind` only accepts `gp`/`poly` — pre-existing,
  noted, not fixed here.

## Decisions (2026-09-16)
1. Default mode **Both**. 2. Warning ratio **1.5×**. 3. ADR-0021 written.

## Found during the build
- **Displayed-check contradiction** (see §3 step 2).
- **Cost:** the first cut of `Both` took 169 s on the 373-run model (8× as measured,
  risking the 600 s request ceiling with four methods). Displayed check first plus
  early-stopping repeats: 65 s (3×).
- **Refit silently changed the method** (`ModelRefitRequest.surrogate_kind`
  defaulted to `gp`); fixed, and Refit gained a dialog with the Output scale choice,
  since otherwise a pre-ADR model could never get a log-scale refit.
- **Exported `.vtu` files carried the reference training run's arrays** (a stale
  `temperature` beside `temperature_field`); `_write_field_file` now clears them.
