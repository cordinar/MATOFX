# Design note — Physics-informed surrogates (PINNs and weaker alternatives)

Status: **Exploratory note, deferred — no decision, no code.** Written 2026-06-11 in answer to "would it make sense to include PINNs in the surrogate model builder?".
Companion docs: `designs/surrogate-family.md` (the shipped surrogate engine this would extend), `decisions/0008-surrogate-family.md`, `designs/optimisation-framework.md` (consumes surrogate uncertainty).
Strategic context: the lightweight-install, governance-first positioning (strategy "A then C") — anything in this note that adds heavy dependencies or per-problem ML babysitting is gated on a paying customer who needs it.

---

## 1. The question

PINNs (physics-informed neural networks) embed the governing PDE's residual into the training loss, so the model is penalised for violating the physics, not just for missing the data. Would offering them as a model kind in the surrogate builder make sense?

**Conclusion: not as a generic model kind. Possibly later as a tool-author extension point. The cheap, high-value move if "more physics" is ever wanted is shape constraints (§4), not PINNs.**

## 2. Why PINNs fight the current architecture

The platform is deliberately physics-blind, and several shipped subsystems depend on properties PINNs don't have.

1. **No physics in the tool contract.** The scalar engine (`matofx/studies/surrogate/__init__.py`) offers `gp`, `gp_matern`, `poly`, `gbm` — pure regressors over `(inputs_json → response)` pairs from archived jobs. The field path is POD/PCA over snapshot ensembles. Nothing in `*_descriptors.json` carries governing equations, domain geometry, boundary conditions, or material laws — by design: the surrogate engine works for *any* tool precisely because it doesn't know whether the tool solves heat conduction or beam bending. A "PINN" entry in the model-kind dropdown is meaningless without a PDE residual, and the platform has none.

2. **Wrong problem class.** A classic PINN solves *one* boundary-value problem (often data-free) by minimising the PDE residual at collocation points. MATOFX surrogates do parametric regression across a design space from tens-to-hundreds of archived runs — the multi-query setting. The physics-informed analogue of that is operator learning (DeepONet / FNO) or parametric PINNs — heavier, more fragile, and still PDE-dependent. For scalar QoIs over a handful of design parameters with 20–200 points, GPR is genuinely the right tool, not a compromise.

3. **No uncertainty.** The optimisation framework's acquisition functions, the `<response>_std` outputs, the in-form coverage gauge, and `block_extrapolation` all lean on GP-style analytic uncertainty. Vanilla PINNs give a point prediction only; Bayesian/ensemble variants multiply the already-high training cost.

4. **Governance loop dies.** The shipped UX is "fit from cohort in seconds, inspect cross-validated Q², publish through the checker gate". Cross-validating a PINN means retraining a neural net k times — minutes-to-hours on GPU per fit, with notoriously finicky loss-weighting between data and physics terms.

5. **Dependency footprint.** Autodiff for the residual term mandates torch/jax in the platform environment — directly against the lightweight-install niche.

## 3. The implementation ladder (if/when physics is wanted)

### Rung 1 — Shape constraints via descriptor metadata  *(recommended first step; cheap, tool-agnostic)*

Most of the practical benefit of physics-informing in the data-scarce regime comes from weak priors, not full PDEs:

- monotonicity — "pressure drop increases with flow rate";
- bounds — "outlet temperature ≥ inlet temperature (an input)";
- known invariances / conservation-style sum constraints.

Implementation sketch:

- Optional per-output block in the tool descriptor, e.g.
  ```json
  "physics_constraints": {
    "pressure_drop": {"monotonic": {"flow_rate": "+"}},
    "outlet_temp":   {"lower_bound_input": "inlet_temp"}
  }
  ```
- Enforcement: `HistGradientBoostingRegressor` has native `monotonic_cst` (the current `gbm` uses `GradientBoostingRegressor`, which doesn't — swap or add a kind); GP/poly fits get a post-fit *audit* (sample the fitted surface, warn on violations) rather than hard enforcement.
- No new dependencies. The tool author — who knows the physics — declares the constraints; the platform stays generic.
- Governance upside: "this surrogate version is certified not to violate its declared physical constraints" becomes a registry-visible quality fact next to Q².

### Rung 2 — Physics as a tool-author plugin, via the v1.x SDK hook

`designs/surrogate-family.md` §1 already lists "Tool-author SDK access to the surrogate engine" as v1.x. That is the natural home for real PINNs, keeping the platform physics-blind — the physics lives with the tool, exactly like `main_script` does.

Contract sketch:

- A tool may ship an optional `static/physics_residual.py` (pointed at by a descriptor `physics` block) exposing `residual(model, collocation_points) -> loss_term`.
- It executes in the **tool's own venv** (which can carry torch), never the platform's.
- The builder shows a "physics-informed" kind **only** for tools that declare the block.
- Training runs as a background job on the GPU queue (far too slow for the inline fit path).
- The result lands in the registry as a normal `surrogate_model_version`; Q² is still computed on held-out archived runs, so the checker gate, su/admin release, surrogate-as-tool synthesis, and lineage tagging are all untouched.

### Rung 3 — Parametric PINN / operator-learning field surrogates

Only if a real customer brings a case where POD/PCA demonstrably fails (strongly nonlinear, moving discontinuities) *and* can articulate their PDE. Per the A-then-C strategy this sits in the "gated on a paying anchor customer" bucket, same as heavy-CAE work. Candidate tech at that point: DeepONet / FNO for fields, POD-Galerkin where intrusive access to the solver exists.

## 4. Bottom line

The honest pitch for MATOFX surrogates is *governed, uncertainty-quantified regression over your archived runs* — GPR delivers that; a PINN kind would weaken it (no uncertainty band, fragile training, heavy deps) while demanding physics knowledge the tool contract deliberately excludes. If a physics differentiator is wanted, Rung 1 gives ~80 % of the practical value for ~5 % of the cost, and Rung 2 gives tool authors a clean escape hatch later without the platform ever needing to know a PDE.
