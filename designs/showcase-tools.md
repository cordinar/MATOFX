# Design — MATOFX showcase tools (NAFEMS SPDM 2026 coupled chain)

> **SUPERSEDED as the NAFEMS 2026 demo vehicle (2026-08-27).** The talk was re-scoped to
> a **single** purpose-built tool — the Finned Cold Plate (`designs/finned-plate-tool.md`)
> — because the subject is governance, coverage and surrogates, and how many tools were
> chained is not evidence for any of it. The current arc is `NAFEMS_Demo_Runbook.md`,
> mapped to the talk by `NAFEMS_Talk_Plan.md` §5.
>
> This document is kept for history. Nothing here is being built or rehearsed for the
> conference.

Status: **Design locked 2026-06-20. CHAIN BUILT + VERIFIED 2026-06-22 — all four pieces (Composite Laminate, shared bracket mesh, Thermal Soak, Bracket Stress) implemented, tested, and wired. Remaining: studies/optimisation runs (step 6) + rendering polish & capture (step 7).**
Supersedes: the single heat-sink framing in `project_nafems_spdm_2026_ml_demo` (memory) and the ROADMAP "NAFEMS SPDM 2026 demonstrator" note — **that older plan is retired.**
Builds on / exercises: `designs/workflow-file-wiring.md` (ADR-0015 — file/field hand-off), `decisions/0008` + `designs/surrogate-family.md` §4 (field + time-resolved surrogates), `decisions/0009` + `designs/optimisation-framework.md` (workflow-target + multi-objective), `designs/parametric-studies.md`, `designs/tool-environment-provisioning.md` (per-tool venv).

> **Why this exists:** the refinement was discussed once before (~2026-06-18) but the session was lost to a laptop restart and never persisted. This doc is the durable record so it stops evaporating.

---

## 1. Goal & requirements (from the maintainer)

Build a small set of **engineering tools** that, run through MATOFX, **showcase MATOFX's features and rendering** for the **NAFEMS SPDM 2026 conference talk** (a few months out). The tools must:

- work **standalone** *and* wired together in a **workflow**;
- be valid targets for **parametric studies, optimisations, and surrogate-model building**;
- use **open-source / freeware solvers only** — **no commercial solver licences**;
- be **render-rich** (the audience must *see* fields, not read tables);
- correctness & visual quality over speed (slow-ish runs are fine).

**The tool list (locked):** `Composite Laminate` → `Thermal Soak` → `Bracket Stress` — a coupled **thermo-structural digital thread** on a composite L-bracket.

---

## 2. Open-source stack (no licences)

| Concern | Library | Licence | Notes |
|---|---|---|---|
| FEM (conduction + elasticity) | **scikit-fem** | BSD-3 | pure-Python (numpy/scipy), pip, Windows-friendly. The only new dependency; lives in the **FE tools' execution env**. |
| Bracket meshing | **scikit-fem + numpy** | BSD-3 | mesh assembled **procedurally** (structured hex/tet) — **no gmsh**. Full node-ordering control for the spine. |
| Mesh I/O | meshio | MIT | already installed |
| Render / `.vtp` export | pyvista (+ VTK) | BSD-3 | already installed; feeds the existing field viewer |
| Math / CLT | numpy / scipy | BSD-3 | already installed |

**Licensing (locked 2026-06-20) — all-permissive only**, matching MATOFX's *proprietary + permissive-deps* rule (`pyproject.toml` annotates each dep's licence; all MIT/BSD/Apache-2). **`gmsh` (GPL-2.0+) is dropped** — the bracket mesh is built procedurally in scikit-fem + numpy instead, which also gives total node-ordering control for the fixed-mesh spine. **`tetgen` / `pyvista.tetrahedralize` (AGPL / commercial-restricted) is likewise avoided.** scikit-fem (BSD-3) is the single new dependency and belongs in the FE tools' execution environment (`designs/tool-environment-provisioning.md`), not necessarily MATOFX core; the laminate tool needs nothing beyond numpy.

---

## 3. The chain & data flow

```
Composite Laminate ──(SCALAR wiring: per-ply card E₁,E₂,G₁₂,ν₁₂, α₁,α₂, k₁,k₂,
                       Xt,Xc,Yt,Yc,S, ρ,cₚ + stacking sequence + t → mass)──┐
                                                                            ▼
                                  Thermal Soak ──(FILE wiring: nodal T field series .vtp)──► Bracket Stress
```

The chain deliberately exercises **both** wiring kinds:
- **Scalar wiring** (material card → both FE solvers) — the existing name/`upstream_output` wiring.
- **File/field wiring** (temperature field → structural thermal load) — the ADR-0015 path just shipped. Thermal Soak's `field_timesteps` series stages whole; Bracket Stress consumes the **final** snapshot as the thermal load.

### 3.1 Fixed-mesh spine (decision)
All three tools resolve against **one static 3-D bracket mesh** shipped identically in both FE bundles, so the temperature field maps **node-for-node** onto the structural model. Geometry (incl. wall thickness) is **fixed**; no remesh, no remap. General geometry variation + cross-mesh remap (`field_remap_reference_mesh`) stays a **v1.x** extension.

### 3.2 Where mass variation comes from (the defensible resolution)
The real composite-lightweighting levers are **ply count (thickness)**, **stacking angles**, and **material choice** — *not* fibre volume fraction (`Vf` is set by the manufacturing process, so an engineer wouldn't treat it as a free design variable). So:

- **Thickness** — the laminate is `[(±θ₁/±θ₂)ₙ]s`; the number of sub-laminate repeats `n` (integer) sets total thickness and therefore **mass**. The mesh keeps **fixed topology**; total thickness scales the through-thickness node coordinates (a uniform z-morph), so node count/ordering stay identical and the temperature field still maps node-for-node. *This is the real "drop plies to save weight" lever.*
- **Stacking angles** `θ₁,θ₂` redistribute directional stiffness / CTE / conductivity at fixed mass — steering *where* stress and heat peak so a thinner laminate can still meet limits.
- **Material** {CFRP, GFRP} — the big stiffness / conductivity / density step.

`Vf` is held at each material's standard value (the value its measured lamina data was characterised at). Narrative: *"thin the laminate and tune the layup until a failure or thermal limit binds."*

---

## 4. Tool specs

Illustrative ranges below are tunable during build; they exist to size the design space.

### 4.1 Composite Laminate — laminate definition + design insight (analytic, instant, pure numpy)

Defines the layup and emits **(a)** the per-ply material card the FE solvers apply layer-by-layer, and **(b)** laminate-level diagnostics (effective constants, ABD) for insight + rendering. No solver.

- **Ply data (measured, not predicted):** each material is a **named, measured UD lamina** at its standard `Vf` (CFRP = T300/5208 @ Vf 0.70; GFRP = E-glass/epoxy @ Vf 0.55) — stiffness `E₁,E₂,G₁₂,ν₁₂`, CTE `α₁,α₂`, **strengths `Xt,Xc,Yt,Yc,S`**, conductivity `k₁,k₂`, `ρ`, `cₚ`. Using *measured* lamina data (Daniel & Ishai) is what a practising analyst does — micromechanics (ROM/Halpin-Tsai) is only a preliminary estimator and is **not** used for the reported results.
- **Layup → diagnostics (CLT):** assemble **A/B/D** from the stacking sequence; report effective in-plane `Ex,Ey,Gxy,νxy`, effective CTE `αx,αy`, and laminate conductivity `kx,ky`. These are **diagnostic/rendering only** — the structural solve uses the *per-ply* card (see §4.3), because smearing would discard the stacking-sequence/bending physics that is the whole point.
- **Layup parameterisation (locked):** symmetric balanced `[(±θ₁/±θ₂)ₙ]s` — **2 ply-angle factors** `θ₁,θ₂` plus integer **repeats `n`** (the thickness/mass lever). Symmetric ⇒ `B=0` (no bend–stretch coupling); balanced ⇒ no shear–extension coupling. Both keep the laminate well-behaved and the design legible.
- **Inputs / factors:** material `{CFRP, GFRP}`; `θ₁,θ₂` (0–90°); `n` repeats (→ thickness `t = n·t_sub`); `Vf` fixed at the dataset value.
- **Outputs:** per-ply card (`E₁,E₂,G₁₂,ν₁₂,α₁,α₂,k₁,k₂,Xt,Xc,Yt,Yc,S,ρ,cₚ`) + stacking sequence; laminate diagnostics (`Ex,Ey,Gxy,νxy,αx,αy,kx,ky`, ABD); `t`; **mass** (`ρ·area·t`); effective conductivity tensor for Thermal Soak.
- **Rendering:** exploded ply-stack coloured by orientation; the iconic composites **polar plot** of `Ex(θ)` and `αx(θ)`; carpet/quasi-isotropic markers for orientation literacy.
- **Standalone:** sweep angles → watch stiffness/CTE rotate; optimise layup for max stiffness / min CTE / zero-CTE tailoring.

### 4.2 Thermal Soak — transient conduction (scikit-fem)
- **Physics:** transient heat conduction on the 3-D bracket, **anisotropic** `k` (from laminate), heat-input patch (mounted component), convective (Robin) cooling; backward-Euler time stepping. "Soak" = heat-up toward steady.
- **Inputs / factors:** `k1,k2` (**wired**); heat load `Q` 5–50 W; convection `h` 5–50 W/m²K; ambient 20–40 °C; soak duration (e.g. 600 s, ~20 snapshots).
- **Outputs:** **time-resolved nodal temperature field** (`.vtp` per step via `field_timesteps`); `T_max` (°C, KPI).
- **Rendering:** 3-D coloured temperature field + **time slider / play** (existing viewer); `time` becomes an extra surrogate factor.
- **Standalone:** sweep load/convection → POD **time-resolved field surrogate** animating a predicted soak in ms.

### 4.3 Bracket Stress — layered-solid thermo-mechanical (scikit-fem)
- **Model (locked):** **layered solid** — the structured mesh carries one element layer per ply-group, each with its own **rotated orthotropic** ply constitutive and ply CTE (a proper material coordinate system that **follows the flanges around the fold**, not a single global axis). Quadratic / solid-shell elements through the thickness to avoid shear locking in the thin section.
- **Physics:** linear orthotropic thermo-elasticity; load = per-ply thermal expansion (`α`·(T−T_ref) from the wired field) **plus** an external mechanical load; clamp at the fixed-flange face patch.
- **Failure (locked — NOT von Mises):** recover **ply stresses in each ply's material axes** and evaluate **first-ply-failure** by **both max-stress** (gives the mode — fibre vs matrix vs shear) **and Tsai-Wu** (interactive; standard `F₁₂ = −½√(F₁₁F₂₂)` assumption stated). von Mises is for isotropic metals and is not used.
- **Verification (locked):** ship a documented benchmark check (e.g. layered plate under known load/ΔT vs CLT closed-form) so the solid-laminate model is *verified*, not asserted — surfaced through MATOFX V&V tracking. (Layered shell is the textbook thin-laminate default; the verified solid is chosen for the 3-D field visuals, clean 90° fold, and through-thickness/interlaminar stress.)
- **Inputs / factors:** per-ply card + stacking sequence + `t` (**wired** from Laminate); **temperature field** (**wired FILE**, final soak snapshot); mechanical load `F` (e.g. 50–500 N) at the free flange.
- **Outputs:** **failure-index field** (max-stress + Tsai-Wu) + displacement field (`.vtp`); `FI_max` (KPI), `max_deflection` (mm, KPI), critical ply/mode, `mass` (carried).
- **Rendering:** 3-D failure-index field + **deformed shape** (the money shot); per-ply through-thickness stress on demand.
- **Workflow:** terminal node; drives the lightweighting objectives/constraints.

### 4.4 Shared mesh asset
A fixed-topology **L-bracket** (two flanges @ 90° with a **corner fillet** for a real stress concentration), 3-D **layered hex** solid, **assembled procedurally in scikit-fem + numpy** (mapped structured grid — **no gmsh**) and shipped identically in both FE bundles so node ordering is bit-identical.
- **Smooth, not stair-stepped:** the fillet is a **mapped (transfinite) structured mesh** that follows a real fillet radius — *not* a voxel stair-step (which would inject a fake stress concentration an analyst would flag). The bolt hole is replaced by a **clamped face patch** (a structured grid can't cleanly drill a round hole; a face-patch clamp is a sound BC).
- **Through-thickness layers:** `N` element layers = the ply-groups, so Bracket Stress can assign per-ply orientation/constitutive. Quadratic / solid-shell elements to keep the thin section locking-free.
- **Thickness as a z-morph:** total thickness `t` scales the through-thickness coordinates only; node **count and ordering are invariant**, so the temperature field maps node-for-node at any `t`.
- **Material coordinate field:** a per-element rosette whose 0°-axis **follows each flange and the fold**, so ply angles mean the right thing around the corner.
- **Named sets:** clamp patch, mechanical-load face, heat-input patch, convection faces.

---

## 5. Optimisation framing (locked: BOTH)

Over the **whole workflow** (workflow-target optimisation, ADR-0009). **Design variables:** `θ₁,θ₂` (ply angles), `n` (ply repeats → thickness), material `{CFRP,GFRP}`; operating point `Q`/`h`/`F` may be fixed or co-varied.

- **Lead — single-objective constrained lightweighting:** minimise `mass` s.t. **Tsai-Wu (and max-stress) first-ply-failure index `FI < 1`**, `T_max < T_limit` (resin service limit ≈ 120 °C), and `max_deflection < δ_limit`.
- **Then — multi-objective Pareto (3-objective):** **mass vs deflection vs `T_max`** via **NSGA-II / NSGA-III**, with `FI < 1` as the feasibility filter. Mass-vs-deflection is the classic stiffness-to-weight tradeoff; adding `T_max` shows the **thermo-structural coupling** (a thinner, lighter laminate spreads heat differently). The 3-D Pareto / its 2-D projections are the visual headline.

---

## 6. What MATOFX features this showcases

| Feature | Where |
|---|---|
| Scalar output→input wiring | Laminate → both FE tools |
| **File/field wiring (ADR-0015)** | Thermal Soak T-field → Bracket Stress |
| Parametric studies | each tool standalone + workflow |
| Optimisation — single-objective constrained | min mass s.t. Tsai-Wu `FI<1`, `T_max`, deflection |
| Optimisation — multi-objective Pareto | mass vs deflection vs `T_max` (NSGA-II/III) |
| Scalar surrogates | `T_max`, `FI_max`, `max_deflection`, `mass` |
| **Field surrogates (POD+GP)** | temperature & failure-index fields |
| **Time-resolved field surrogate + animation** | transient soak |
| Rendering | 3-D fields, deformed shape, ply-stack, polar plot, time animation |
| **V&V** — *verified* solid-laminate model (benchmark vs CLT) | Bracket Stress verification surfaced via V&V tracking |
| Governance riding along | lineage, KPI on every run/surrogate |

---

## 7. Build sequencing (proposed)

1. **Bracket mesh asset** — ✅ **BUILT 2026-06-20** (`example_tools/_shared/bracket_mesh.py`): procedural scikit-fem + numpy layered-hex L-bracket (mapped quarter-circle fillet, through-thickness ply layers, **thickness z-morph keeping bit-identical topology**, per-element material rosette following the flanges) + named facet sets (clamp/load/heat/convection). Geometry reconciled with the laminate mass (shared mid-surface area). Tests `tests/tools/test_bracket_mesh.py`: structured counts, no inverted elements, topology-invariance under morph, disjoint+covering named sets, orthonormal right-handed rosette, volume vs geometry, **real scikit-fem Laplace solve**. *(The elasticity layered-plate-vs-CLT verification benchmark lands with Bracket Stress, which has the constitutive.)*
2. **Composite Laminate** tool — ✅ **BUILT 2026-06-20** (`example_tools/composite_laminate/`): verified CLT engine (`laminate_clt.py`) + tool script + descriptor (33 outputs: per-ply card incl. strengths, laminate diagnostics, mass, ply-stack + directional-modulus polar plots). Measured Daniel & Ishai lamina data. Tests: `tests/tools/test_composite_laminate_clt.py` (8 textbook-correctness checks — UD→lamina constants, balanced/symmetric→no coupling, cross-ply Ex=Ey, directional modulus, mass∝n) + end-to-end in `test_example_tools.py`. *(Built ahead of the mesh — it's analytic, mesh-independent.)*
3. **Thermal Soak** tool — ✅ **BUILT 2026-06-20** (`example_tools/thermal_soak/`): real scikit-fem transient anisotropic conduction (backward-Euler, pre-factorised) on the shared mesh; orthotropic `k` (wired from laminate) rotated per element by the rosette; heat-flux patch + Robin convection. Outputs the **time-resolved** `.vtu` series (`field_timesteps`) + the **final** soaked field (wired to Bracket Stress) + `T_max`/`T_avg`. Baseline CFRP soaks to ~110 °C (feasible, near the 120 °C limit); GFRP runs hotter (the coupling). Tests `tests/tools/test_thermal_soak.py`: uniform-initial + monotonic heat-up, **steady-state energy conservation** (heat in = convective heat out), low-k-runs-hotter coupling; + e2e (full-res ≈ 6 s).
4. **Bracket Stress** tool — ✅ **BUILT 2026-06-22** (`example_tools/bracket_stress/`): scikit-fem **layered-solid** orthotropic thermo-elasticity (one element layer per ply, ply constitutive rotated by the rosette × ply angle, built on the full 4th-order tensor to dodge Voigt pitfalls), **Hex2** displacement (reduced-integration, anti-locking) consuming the wired temperature field + mechanical load. **First-ply max-stress + Tsai-Wu** (not von Mises). Outputs FI field (`.vtu`) + `FI_max`/`max_deflection`/`mass`. Constitutive in `laminate_solid.py`. Tests `tests/tools/test_laminate_solid.py` (rotation-invariance, 90° swap, failure indices exact) + `tests/tools/test_bracket_stress.py`: **UD coupon → E1** and **layered coupon → CLT Ex** (the verification benchmark), stacking-sequence discrimination (fibres-outboard stiffer — the layered-vs-smeared payoff), safe-state failure. Production solve ≈ 20–24 s; **both FE tools share the 30×6×8 mesh** so the field handoff is node-for-node.
5. **Wire the workflow** — **REVERSED 2026-10-06: the three descriptors now ship PRE-WIRED** (`downstream_tools` on Laminate + Soak; 6 wired inputs on Soak, 16 on Stress, links copied from the demo install's hand-wiring), so a fresh install — e.g. one seeded by `install_showcase_tools.py` — gets the chain without hand-wiring. Each wired input keeps its default, so every tool still runs stand-alone (the single-tool run form ignores wiring); `test_showcase_tools_ship_wired_and_still_run_alone` guards both properties. *History:* originally **delivered as STAND-ALONE tools (maintainer's call, 2026-06-22).** The descriptors carry **no** workflow wiring (`downstream_input` / `downstream_tools` stripped); every input is a plain, defaulted entry so each tool runs alone (the wired scalars default to the CFRP baseline; Bracket Stress takes the temperature field as an uploaded/typed `.vtu`). **The operator wires the chain themselves** in the workflow editor. The hand-offs are nonetheless proven compatible: a full chain smoke (Thermal Soak writes the `.vtu` → Bracket Stress reads it) is node-for-node, and `test_showcase_tools_have_no_workflow_wiring` (since replaced) guarded that the tools stayed wiring-free. *(Output names line up for wiring: Composite Laminate emits `kx/ky/kz/rho/cp/thickness` + the `e1…s_shear` ply card + `theta1/theta2/mass`; Thermal Soak emits `temperature_final`.)*
6. **Studies** — DOE sweeps → parametric study; scalar + field + time-resolved surrogates; constrained + Pareto optimisation.
7. **Rendering polish + demo capture** (talk is **pre-recorded** per the prior decision).

Help docs (`docs/{user,admin}`) and ARCHITECTURE/ROADMAP updates ship **with each tool** per the CLAUDE.md rule.

---

## 8. Deferred to v1.x
- In-plane geometry variation (vary flange size / fillet radius) + **cross-mesh field remap** (the thickness z-morph is *in* v1; in-plane shape changes are not).
- Richer material library; mode-discriminating failure (Hashin/Puck); progressive/ultimate failure beyond first-ply.
- Cost as an objective (dropped for now — re-introducible if a defensible price source is wanted).
- Active-learning loop (surrogate flags low-confidence → auto-submit a governed run) — strong closing beat, but build the deterministic chain + surrogates first.

---

## 8a. Composite Laminate — concrete numbers (locked 2026-06-20)

**Measured UD lamina data** — Daniel & Ishai, *Engineering Mechanics of Composite Materials* (the canonical reference; these exact values appear across the composites literature). Each material is taken at its standard manufacturing `Vf` (not a design variable).

| Lamina property | **CFRP — T300/5208** (Vf ≈ 0.70) | **GFRP — E-glass/epoxy** (Vf ≈ 0.55) |
|---|---|---|
| `E₁` (GPa) | 181 | 39 |
| `E₂` (GPa) | 10.3 | 8.6 |
| `G₁₂` (GPa) | 7.17 | 3.8 |
| `ν₁₂` | 0.28 | 0.28 |
| `α₁` (×10⁻⁶/°C) | 0.02 | 7.0 |
| `α₂` (×10⁻⁶/°C) | 22.5 | 21 |
| `Xt` (MPa) | 1500 | 1080 |
| `Xc` (MPa) | 1500 | 620 |
| `Yt` (MPa) | 40 | 39 |
| `Yc` (MPa) | 246 | 128 |
| `S` (MPa) | 68 | 89 |
| `ρ` (kg/m³) | 1600 | 2100 |

**Thermal** (from thermal datasheets — less standardised than the mechanical canon, flagged as indicative): CFRP `k₁/k₂ ≈ 5 / 0.6` W/m·K, `cₚ ≈ 800` J/kg·K; GFRP `k₁/k₂ ≈ 1.0 / 0.5` W/m·K, `cₚ ≈ 1000` J/kg·K.

**Geometry / mass:** L-bracket flanges 80 mm × depth 50 mm, baseline `t = 4 mm`; `V ≈ 31.2 cm³` (`b·t·(L₁+L₂−t)`); mass `ρ·V` ≈ **CFRP 50 g / GFRP 66 g** at baseline thickness, **scaling linearly with `t`** (the lightweighting lever).

**Factors (locked):** material {CFRP, GFRP}; **2 ply angles** `θ₁,θ₂` 0–90° in `[(±θ₁/±θ₂)ₙ]s`; **`n` ply-repeats** → thickness (integer; the mass lever); `Vf` fixed at the dataset value. Failure: **first-ply max-stress + Tsai-Wu**, `FI < 1`. `T_max` limit ≈ 120 °C (resin service limit).

**Emergent insight:** CFRP beats GFRP on mass *and* stiffness *and* conduction — so the **lightweighting trade is thin-the-laminate + tune-the-angles until first-ply-failure, deflection, or `T_max` binds**, with material choice and thickness as the mass levers. Cost is **not** modelled (dropped). The headline Pareto is **mass vs deflection vs `T_max`**.

> **Defensibility note:** measured lamina data + layered solid (verified vs CLT) + ply-level first-ply Tsai-Wu/max-stress + thickness/angle/material design variables — this is what a practising analyst would actually run, not a simplification. Micromechanics, smeared-solid stiffness, and von Mises (all in earlier drafts) were **rejected** as not audience-defensible.

## 9. Decisions — LOCKED 2026-06-20
1. **Tool list:** Composite Laminate → Thermal Soak → Bracket Stress.
2. **Lightweighting driver:** fixed-**topology** mesh; mass varies by **thickness** (`n` ply-repeats, z-morph) + **material**; angles tune the distribution. (`Vf` fixed — not a design variable.)
3. **Dimensionality / structural model:** **3-D layered solid** — per-ply element layers, rotated orthotropic ply constitutive, material rosette following the flanges; quadratic/solid-shell elements (no locking); **verified vs CLT** (V&V). *(Layered shell considered; solid chosen for visuals + clean fold + interlaminar + lower build risk, backed by verification.)*
4. **Thermal regime:** **transient** soak (time-resolved field).
5. **Structural load:** **thermo-mechanical** (wired temperature field + external mechanical load).
6. **Failure:** **first-ply max-stress + Tsai-Wu** (`FI<1`). **NOT von Mises.**
7. **Optimisation:** **both** — single-objective constrained lightweighting (min mass s.t. `FI<1`, `T_max`, deflection) *then* **3-objective mass-vs-deflection-vs-`T_max`** Pareto (NSGA-II/III). **Cost dropped.**
8. **Solvers / licensing:** **scikit-fem (BSD-3)** only new dep; **gmsh dropped** (GPL) — mesh procedural in scikit-fem + numpy; `tetgen`/`pyvista.tetrahedralize` avoided (AGPL). All-permissive.
9. **Composite Laminate data:** **measured UD lamina** (Daniel & Ishai — CFRP T300/5208, GFRP E-glass/epoxy) incl. **strengths**; CLT for diagnostics/rendering only; per-ply card drives the solve. **Micromechanics/Vf-scaling rejected for reported results.**
10. **Layup:** symmetric balanced `[(±θ₁/±θ₂)ₙ]s` — **2 ply-angle factors** + integer thickness repeats `n`.
