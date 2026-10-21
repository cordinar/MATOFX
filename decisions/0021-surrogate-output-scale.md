# 0021. Per-result output scale (log) for scalar surrogates

Date: 2026-09-16
Status: Accepted + **SHIPPED 2026-09-16**. **Build-form control superseded by ADR-0022 (2026-09-17)**: the output-scale rule lives on as each result's baseline inside per-result tuning; the *As measured / Log / Both* control became *Tuning per result: Automatic / Off*, and the override warning became "any error above the best combination tried". Build-level spec: `designs/surrogate-output-scale.md`. Extends ADR-0008 (surrogate family) and ADR-0018 (combined surrogate). Scalar results only.

## Context

A scalar surrogate learns each result exactly as measured. The maintainer built
two Finned Cold Plate surrogates, one on 20 runs and one on 70, and found that
the 70-run model predicted peak component temperature *worse* at the points
tested (misses of 2.2 and 3.8 °C against the solver).

Investigation showed the data were clean. The wider cohort spans 40–131 °C and
the response curves steeply into the few-fins, low-fan corner: temperature rise
behaves like a product of powers of the inputs. A Gaussian process has one set
of length scales for the whole design space, so it compromises and loses
accuracy in the cool region. Refitting `log(y)` halved the cross-validated
error (2.98 → 1.85 °C).

The maintainer's constraint: **a fix must not help one model and make others
worse.** A benchmark over every existing fit in the demo database (82 GP fits,
69 polynomial, 69 gradient boosting) showed:

- **Log on every result is unacceptable.** It made 32 of 82 GP fits worse (up
  to 163×) and wrecked additive results such as mass under a polynomial (up to
  10,843×).
- **A measured, per-result choice is safe.** Choosing log only when it is at
  least 10% better in cross-validation and wins every repeat: GP 34 better /
  42 same / 6 worse (worst 1.55×, mean −13%); polynomial 25 / 42 / 2 (worst
  1.07×, mean −19%); gradient boosting 9 / 59 / 1 (worst 1.05×, mean −3%).
- Scale and method interact: the best peak-temperature fit was polynomial on a
  log scale (1.40 °C), which neither choice finds alone.

## Decision

1. **Output scale is a per-result property of a scalar fit:** `as_measured` or
   `log` (fit `log(y)`, predict `exp(·)`). Never a model-wide switch.
2. **The build form offers a mode:** `As measured` / `Log` / `Both`.
   **Default `Both`.**
3. **`Both`:** MATOFX fits both scales for every result and picks log only if all
   training values are positive, **the displayed cross-validated error on the log
   scale is lower**, the mean repeated-CV RMSE (3 × shuffled 5-fold, original
   units) is at least 10% lower, **and** log wins every repeat. The displayed check
   comes first: without it the repeats picked log for Finned Cold Plate fan power
   while the table showed log's error as the larger one. Repeats stop at the first
   loss.
4. **The choice is visible and overridable.** Fit & compare shows, per result,
   the error on each scale, MATOFX's pick with a reason, and a per-result
   override. The model page shows the scale used and whether MATOFX or the user
   chose it.
5. **Safeguard:** a result warns when the scale in use has a cross-validated
   RMSE more than **1.5×** the other scale's (a forced `Log` or a user override).
   The warning is advisory, stored with the version and in the audit payload.
   `Log` mode therefore fits both scales, like `Both`.
6. **Results with any value ≤ 0 stay as measured** in every mode, with a note.
7. **Reported quality stays in the result's own units** (Q², RMSE and parity
   plots are computed after converting back), so scales are directly comparable.
8. **Uncertainty:** the predicted value is `exp(μ)` (the median); the GP 1-sigma
   is the delta-method `exp(μ)·σ`. The coverage guard (ADR-0020) uses the latent
   `σ / std(log y)` for log fits, so its thresholds keep their calibration.
9. **Existing models and exported tools never change.** Surrogates predating this
   load as `as_measured`. A better fit needs a refit, which creates a new version.
   Refit keeps the mode and user overrides and re-decides MATOFX's own picks on the
   new data; its dialog can change the mode (models fitted before this ADR are as
   measured until refitted with `Both` or `Log`).
10. **Refit no longer changes the fitting method.** `ModelRefitRequest.surrogate_kind`
    defaulted to `gp`, and the service treats any value as explicit, so the Refit
    button (empty body) silently turned Matérn, polynomial and gradient-boosting
    models into smooth GPs — visible in the demo database (model 18 v1 `gp_matern`,
    v2 `gp`). It now defaults to `None` (keep).

## Consequences

- Better fits for multiplicative results (temperature rise, resistance, stress,
  deflection, power) without degrading additive ones, and a record of every
  choice.
- `Both` costs about 3× as-measured fitting on real data (373-run Matérn, 3
  results: 65 s vs 21 s; 70-run cohort, 3 methods × 6 results: 28 s), because the
  displayed check settles most results before any repeat runs. Fitting is off the
  event loop; the in-app request ceiling is 600 s.
- Benchmark of the **shipped** code (outer 5-fold CV over every training set in the
  demo database, the rule deciding on each training fold): Gaussian process
  (both kernels) 60 better / 65 same / 13 worse over 138 fits, mean −14%, worst
  1.75×; polynomial 25 / 36 / 2, mean −20%, worst 1.07×; gradient boosting
  9 / 59 / 1, mean −3%, worst 1.05×. Worse cases remain possible, which is why the
  choice is shown and overridable.
- No database migration: choices live in the version's `responses` and
  `quality` JSON.
- Out of scope: fields (POD coefficients change sign), the optimisation MOP
  auto-metamodel and the parametric-study profiler (stay as measured), a log
  with a user-supplied baseline such as `log(T − T_ambient)`, and different
  fitting methods per result.
