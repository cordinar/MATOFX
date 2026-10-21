# 0022. Per-result surrogate tuning: method × output scale × input scale

Date: 2026-09-17
Status: Accepted + **SHIPPED 2026-09-17**. Build-level spec: `designs/surrogate-per-result-tuning.md`. Extends ADR-0021 (per-result output scale), whose build-form *Output scale* control it replaces. Scalar results only.

## Context

ADR-0021 let each scalar result choose its output scale, but a model still fitted
every result with **one method**, and inputs were always used as measured. Three
benchmarks over every training set in the demo database (outer 5-fold CV; every
choice made on the training folds only) measured what that costs:

- **Log inputs** (Gaussian process only): −22% typical error beyond ADR-0021 over
  138 GP fits (71 better / 52 same / 15 worse). Mixed for polynomials and worse on
  the Finned Cold Plate, so not offered for them.
- **A method per result:** "lowest cross-validated error wins" helped 25 of 69
  results but hurt 16 (worst 3.4×) — single CV scores are noisy. With the ADR-0021
  strict rule ("clearly and consistently better") it was 20 better / 6 worse.
- **All three combined under the strict rule:** 45 better / 17 same / 7 worse of 69,
  typical −12%, worst 1.68×. On the 70-run Finned Cold Plate: mass 0.43 → 0.088 g,
  fan power halved, peak temperature 1.85 → 1.43 °C; thermal resistance 11% worse.

## Decision

1. **Each scalar result has its own configuration:** method × output scale
   {as measured, log} × input scale {as measured, log}. Log inputs are offered for
   the Gaussian process methods only.
2. **Build form:** *Tuning per result* — **Automatic** (default) or **Off** (one
   method, as measured: the pre-ADR-0021 behaviour). It replaces ADR-0021's
   *As measured / Log / Both* control; *Methods to try* sets the candidate methods.
3. **Baseline ("before tuning")** = the model's starting method **exactly as
   measured** — suggested as the method with the best before-tuning mean Q², else
   the first ticked method. A result leaves the baseline only for a candidate
   (log output included) whose displayed CV error is lower, whose mean over 3
   repeated shuffled 5-fold checks is at least 10% lower, and which wins every
   repeat — or, if the baseline's own repeats vary more than 3-fold, which is
   better on average and in the worst case. *(Revised 2026-09-17: the baseline
   first included ADR-0021's log-output choice, so a table labelled "before
   tuning" showed log scales; and methods were first ranked after tuning, which
   made every method whose results all moved elsewhere tie for best.)*
4. **Overrides:** the user may choose any evaluated configuration for any result.
5. **Warning:** ⚠ whenever a **user-selected** configuration has a higher
   displayed error than the best configuration evaluated for that result — any
   amount, not a ratio (replaces ADR-0021's 1.5× rule for overrides).
6. **Log inputs are guarded.** A factor is logged only if every training value is
   > 0 **and** the tool declares a minimum > 0 for it (an undeclared minimum is not
   eligible, so the exported form needs nothing extra); a non-positive value at
   prediction still raises a clear error as a backstop. Models are always
   called in real units.
7. **Fitting runs in the background** with progress, because the candidate search
   can take minutes on large models; the page polls it. The synchronous endpoints
   remain for scripts and tests.
8. **Recorded per result** in the version's `responses`/`quality` JSON: method,
   output scale, logged factors, who chose it, every candidate's error, reason,
   warning. `surrogate_method` stays the primary method. **No migration.**
9. **Existing models do not change.** Versions fitted before ADR-0022 refit as they
   were fitted (ADR-0021 versions: *Automatic*; older ones: *Off*) unless the Refit
   dialog says otherwise; user choices are kept.

## Consequences

- Better per-result accuracy on most results, with every choice visible, explained
  and overridable; a few results can still get worse, which is why overrides and
  the warning exist.
- One version can mix methods, so consumers that assumed one method per model
  change: exported `<name>_std` outputs are per result (only where a result has
  variance); the predict-time support guard takes its sigma from a result with
  variance; displays show *Mixed methods*.
- Builds take longer (up to 12 candidate fits per result plus repeats), hence
  background fitting.
- Out of scope: fields, the optimisation MOP auto-metamodel, the study profiler, a
  log with a baseline such as `log(T − T_ambient)`, ensembles.
