# Design — Stirred-tank batch-blend mixing tool (CSTR mixing demo)

Status: **BUILT + VALIDATED END-TO-END 2026-07-10.** Full tool (`matofx/example_tools/batch_mixing/`): FreeCAD geometry → OpenFOAM (ESI v2606) `matofxMixingFoam` MRF solve → post → MATOFX-native outputs, driven by `batch_mixing.py`. Orchestrator runs clean (RC=0); descriptor validates (20 inputs, 11 outputs). Baseline 30 s run: **t₉₅ = 12.5 s, power ≈ 0.095 W**. Remaining: refine mesh for production runs; register in a live MATOFX instance; run the offline study/optimisation/surrogate campaign. Design decisions 1–5 locked 2026-07-09.
Builds on / exercises: `designs/showcase-tools.md` (the open-source render-rich demo-tool pattern), `designs/parametric-studies.md`, `decisions/0009` + `designs/optimisation-framework.md` (multi-objective / NSGA), `decisions/0008` + `designs/surrogate-family.md` §4 (field + time-resolved surrogates), `designs/output-validity.md` (ADR-0017 validity floors/ceilings + infeasible handling), `designs/tool-environment-provisioning.md` (per-tool execution env).

> **Why this exists:** a second flagship demo tool alongside the NAFEMS thermo-structural chain — a **CFD** primitive (turbulent stirred-tank blending in OpenFOAM, geometry/mesh auto-built in FreeCAD) that is deliberately *expensive per run*, so the value story is MATOFX's **parametric study → multi-objective optimisation → scalar + field surrogate** stack sitting on top of it. Not a live demo: all simulations, studies and surrogates are pre-run. Runtime budget: **≤ 1 hour per simulation**.

---

## 1. Goal & requirements (from the maintainer)

A **batch-blend stirred-tank** tool: a closed tank (no through-flow) with **baffles** and a **pitched-blade impeller**, initially charged with two **perfectly miscible** fluids — a **bulk** fluid and an **additive layer on top**. The impeller blends them; the tool reports **when homogeneity is reached** (time to 95 % uniform) and the **power** to get there.

Locked requirements:

- **Batch blend, no through-flow.** Closed tank; mixing is impeller-driven.
- **Full 3-D, turbulent** (RANS).
- **Geometry + mesh auto-built in FreeCAD** from user inputs (tank, baffles, impeller).
- **Two miscible fluids with different (ρ, μ) and volume fractions**; **density difference in scope** (buoyancy).
- **Multi-objective:** minimise **(t₉₅, impeller power)** — a Pareto trade-off.
- **Every geometric/operational parameter exposed as a Vary-able input** (a study picks whatever subset it varies; not all vary at once).
- **Render-rich:** an **animation** of the blending concentration field, plus the **field captured throughout the trajectory** so a surrogate can predict t₉₅ *and* the blend evolution.
- Open-source stack only (no commercial CFD licence). Host: **WSL2 + Ubuntu** (OpenFOAM is Linux-native; FreeCAD runs native Windows or in WSL). **GPU is not used** — stock OpenFOAM solvers are CPU-only; throughput comes from multi-core `decomposePar` and pre-running offline.

---

## 2. Open-source stack (no licences)

| Concern | Tool | Licence | Notes |
|---|---|---|---|
| Parametric geometry | **FreeCAD** (Python API, headless) | LGPL | tank + baffles + pitched-blade impeller built from inputs → STL export |
| Background mesh | **blockMesh** (OpenFOAM) | GPL | Cartesian background block over the (constant) tank envelope |
| Body-fitted mesh | **snappyHexMesh** (OpenFOAM) | GPL | carves baffles + resolves impeller **blades as wall patches** (needed for torque) |
| Flow + mixing solve | **`twoLiquidMixingFoam`** (OpenFOAM, **lightly modified** — see §4) | GPL | transient, incompressible, two miscible fluids, mixture ρ/μ from `alpha`, `p_rgh` buoyancy, MRF impeller zone, RANS turbulence |
| Field resample | **`sample` / `postProcess -func interpolate`** (OpenFOAM) | GPL | `alpha` → fixed Cartesian grid at fixed snapshot times (surrogate feed) |
| Animation | **ParaView `pvbatch`** (headless) | BSD | scripted → mp4 of the evolving concentration field |

**Licensing note.** OpenFOAM/ParaView are **GPL/BSD external executables invoked as subprocesses**, not linked into MATOFX — same posture as any external solver a tool shells out to. This does **not** contaminate MATOFX's proprietary core (contrast the `gmsh`-in-process concern in `showcase-tools.md`, which was avoided by *linking*; here we only spawn processes). Confirm this reading is acceptable before build — it's the one licensing question for this tool.

---

## 3. Physics & modelling decisions

### 3.1 Solver & impeller representation

- **`twoLiquidMixingFoam`** — the natural OpenFOAM solver for two *miscible* incompressible fluids: it transports a mixture fraction `alpha` (0 = bulk, 1 = additive), derives **mixture viscosity and density** from it, and includes gravity via `p_rgh`. This gives the viscosity feedback (additive locally stiffens/thins the flow) and the buoyancy (density difference) the maintainer wants — VOF/two-phase would be physically wrong for miscible fluids.
- **MRF ("frozen rotor") impeller zone**, RANS, **fully transient.** The mean flow reaches quasi-steady within a few revolutions while the scalar keeps blending on a slower timescale, so freezing the impeller is defensible and keeps timesteps large enough to hit ≤ 1 hr. **Accepted trade-off:** the impeller does **not** visibly rotate in the animation — the money shot is the concentration field homogenising; the impeller is rendered static. (A visibly-spinning hero shot would need AMI sliding mesh, which breaks the hour budget — out of scope, could be a one-off outside the tool.)

### 3.2 Turbulent diffusivity (already stock) + the real reason for a custom solver (MRF)

In a RANS + frozen-MRF blend the scalar is mixed by only two mechanisms: **convection by the mean flow** and **turbulent diffusion** (RANS models the unresolved fluctuations as `ν_t/Sc_t`). Molecular diffusivity alone (~1e-9 m²/s) would never smear striations out and t₉₅ would be meaningless — so the turbulent term is essential.

**RESOLVED — it is already in stock v2606 (verified 2026-07-09 against the installed source).** `twoLiquidMixingFoam/alphaDiffusionEqn.H` reads:
```cpp
fvm::laplacian( volScalarField("Dab", Dab + alphatab*turbulence->nut()), alpha1 )
```
where `createFields.H` documents `alphatab` as *"the reciprocal of the turbulent Schmidt number."* So `D_eff = Dab + (1/Sc_t)·ν_t` out of the box; we just run RAS and set **`alphatab ≈ 1.43` (Sc_t = 0.7)** in `transportProperties`. **No diffusivity modification is needed** — the original risk is void.

**But the custom solver is still required — for MRF.** Stock `twoLiquidMixingFoam` has **no MRF support** (`grep MRF` in the solver → nothing; `UEqn.H` lacks `MRF.DDt`), so it cannot spin the impeller zone. So `matofxMixingFoam` = stock `twoLiquidMixingFoam` + the standard MRF wiring copied from `interFoam`:
- `createFields.H`: `#include "createMRF.H"`
- `UEqn.H`: `MRF.correctBoundaryVelocity(U);` and `+ MRF.DDt(rho, U)`
- `pEqn.H`: `MRF.makeRelative(phiHbyA);` and `constrainPressure(p_rgh, U, phiHbyA, rAUf, MRF)`

`createMRF.H` is on the include path and MRF lives in `libfiniteVolume` (no new library). **BUILT + verified 2026-07-09** (compiles clean, runs) via `openfoam/solver/build_solver.sh` — idempotent, no sudo, installs to `$FOAM_USER_APPBIN`. Net effect vs the old plan: same one custom-solver build step, but the change is standard MRF plumbing (lower risk) rather than touching the scalar equation.

### 3.3 Turbulence, mesh, numerics

- **Realizable k-ε with wall functions** (robust stirred-tank workhorse), y⁺ ≈ 30–100. `k-ω SST` as an optional switch. LES is out (won't fit the hour).
- **Coarse-ish 3-D mesh (~0.3–1 M cells).** Mesh resolution + rpm range are **calibrated against the 1-hr budget during build** — a tuning step, not a guess.
- **Big viscosity ratios (>~100:1) stiffen the solve and can diverge** → bound the viscosity-ratio input range for the demo.

### 3.4 Initial condition & regimes

- `setFields` charges `alpha = 1` in a **top layer** whose thickness comes from the **additive volume %**, `alpha = 0` below.
- Density difference gives **two regimes** (both physical, unequal difficulty):
  - *Heavy additive on top* → unstable stratification, buoyancy **helps** mix.
  - *Light additive on top* → stably stratified, the impeller must **overcome** buoyancy — the hard case where baffle/impeller design matters most.
- The **Richardson number** (buoyancy vs impeller inertia) is the difficulty knob — logged per run as a regime indicator and a useful **surrogate feature**.

### 3.5 Homogeneity metric & power

- **Global volume-weighted coefficient of variation** `CoV(t) = std(alpha)/mean(alpha)`, computed live by a `functionObject` writing `CoV(t)` to a log.
- **t₉₅ = first time CoV ≤ threshold** (default 0.05 → 95 % homogeneous), and stays below.
- **Censoring:** a run that never reaches the threshold within the **max simulated blend time** → **infeasible** (ties into ADR-0017 output-validity + the optimiser's infeasible handling). This is a real outcome for over-baffled / too-slow configs and must be a clean flag, not silent garbage.
- **Power** the physical way: the impeller **blades are wall patches**, the `forces` functionObject integrates **torque about the shaft axis**, and `P = torque · ω`. Second objective straight from the CFD — no Np correlation needed.

---

## 4. Field surrogate — why the fixed tank envelope matters

A POD/SVD field surrogate needs every training run on a **common basis**, but each parametric run has a **different mesh** (baffles/impeller vary). You cannot POD across meshes directly. The fix — and the reason **tank dimensions are held constant** across the surrogate:

> Resample every run's `alpha` field onto a **fixed Cartesian grid spanning the invariant tank envelope**, at a **fixed set of snapshot times** (≈ 20–40 on a common time axis). The outer tank is invariant, so the grid is shared by all runs → the field surrogate gets an aligned `alpha(x, t)` tensor per run and can predict the **whole blending trajectory** (hence t₉₅) for unseen designs.

The tool's field output is therefore **not the raw mesh** — it's `alpha` resampled to the fixed grid at fixed times. This also **caps data volume** (which would otherwise explode across hundreds of runs). Consistent with the prior field-surrogate data-quality lesson: **enforce finite-only, drop diverged snapshots, set per-run validity before ingest** (§6).

---

## 5. MATOFX tool contract (descriptor)

Descriptor idiom matches the example tools (`schema_version = 1`, `[[inputs]]`, `[[outputs]]`, output-validity bounds, `field_timesteps` series). **Every geometric/operational input is Vary-able**; a study varies a chosen subset.

### 5.1 Inputs (all Vary-able)

| Group | Input | Unit | Notes |
|---|---|---|---|
| **Tank** | diameter `T` | m | held constant across the surrogate (fixed envelope) |
| | liquid height `H` | m | default `H = T` |
| **Baffles** | count | – | default 4 |
| | width (·T) | – | fraction of T (e.g. T/10) |
| | radial offset from wall | m | |
| | height span | – | fraction of H |
| **Impeller (pitched-blade)** | diameter ratio `D/T` | – | default ~0.33 |
| | off-bottom clearance `C/T` | – | default ~0.25–0.33 |
| | blade count | – | integer (integer-param handling) |
| | blade angle | deg | default 45 |
| | shaft diameter | m | |
| | hub diameter | m | explicit; else derived from shaft/D (validated: shaft < hub < 0.9·D) |
| | blade width / thickness | m | explicit; else derived from D |
| | **rotational speed** | rpm | primary operational lever |
| **Bulk fluid** | density ρ | kg/m³ | |
| | viscosity μ | Pa·s | |
| | volume % | – | |
| **Additive fluid** | density ρ | kg/m³ | on top; sign of Δρ sets the regime |
| | viscosity μ | Pa·s | ratio bounded for stability |
| | volume % | – | sets top-layer thickness via `setFields` |
| **Numerics** | homogeneity threshold | – | default CoV = 0.05 |
| | max simulated blend time | s | censoring horizon |
| | turbulence model | choice | realizable k-ε (default) / k-ω SST |

### 5.2 Outputs

**Scalars** (with ADR-0017 validity bounds; `kpi = true` on the headline pair):
- **`t95`** — time to 95 % homogeneity (s). Infeasible if not reached within the horizon.
- **`power`** — impeller power draw (W).
- **`blend_energy`** — `power · t95` (J), a derived convenience objective.
- **`richardson`** — regime indicator / surrogate feature.
- **`converged`**, **`blended_within_horizon`** — validity flags.

**Files (retained):**
- **`geometry_preview.png`** — clean 3-D perspective + 2-D plan of the built tank/baffles/impeller (from `geometry/render_preview.py`), surfaced as an output/input-guide picture so the user sees the geometry each run resolved.
- **`mixing_history.csv`** — one tidy table (`post/parse_results.py`) rendered natively by MATOFX's `csv_plot` (interactive Plotly, no static PNGs). Columns (0-indexed): `0 Time (s)`, `1 Homogeneity (%)` = 100·(1−min(CoV,1)) crossing 95% at t₉₅, `2 CoV` (rigorous metric, kept for the surrogate), `3 Impeller power (W)`. **Two `csv_plot` outputs** point at this one file — `yaxis_column=1` (homogeneity) and `yaxis_column=3` (power) — the "extra column, not a second file" pattern. Headers become the Plotly axis labels.
- **`alpha_series`** — `alpha` resampled to the fixed grid at fixed snapshot times (`field_timesteps = "outputs/alpha_step_*.vtu"`, `value_min/value_max` per-node validity) — the **field-surrogate feed**.
- **`blend.mp4`** — ParaView-rendered animation of the evolving concentration field (static MRF impeller).

---

## 6. Per-run pipeline (tool main script)

```
inputs.json
  → FreeCAD (headless): parametric tank + baffles + pitched-blade impeller → STL
  → blockMesh (background over the fixed tank envelope)
  → snappyHexMesh (carve baffles; blades as wall patches)
  → setFields (additive layer on top; thickness from vol%)
  → decomposePar
  → matofxMixingFoam  (twoLiquidMixingFoam + turbulent D_eff; MRF, RANS, transient)
        · functionObject: CoV(t) → log
        · functionObject: forces on impeller patch → torque → power
  → reconstructPar
  → sample alpha → fixed Cartesian grid @ fixed snapshot times   (surrogate feed)
  → pvbatch → blend.mp4                                           (animation)
  → parse t95, power, blend_energy, richardson, validity → outputs.json
```

**Every stage has a failure → validity path** (FreeCAD build fail, snappy mesh fail, solver divergence, no-blend-within-horizon) so bad runs are cleanly **infeasible**, never silent garbage feeding the surrogate/optimiser. Mirrors the ADR-0017 posture and the earlier field-surrogate NaN-poisoning lesson.

---

## 7. Study / optimisation / surrogate wiring

- **Parametric study** over any chosen subset of §5.1 (e.g. rpm × D/T × baffle count × Δρ), producing StudyResults with the scalar + field outputs.
- **Multi-objective optimisation:** minimise **(t₉₅, power)** → Pareto front via the NSGA path (`decisions/0009`). `blend_energy` available as an alternative single objective; `blended_within_horizon` as an infeasibility constraint.
- **Surrogates:** scalar surrogates for **t₉₅** and **power**; **time-resolved field surrogate** for `alpha(x,t)` on the fixed grid (predicts the whole blend trajectory, and therefore t₉₅, for unseen designs). Surrogate-assisted optimisation then explores the design space cheaply — the governed-AI story.

---

## 8. Open items before build

1. **Licensing sign-off** — confirm the "OpenFOAM/ParaView as spawned GPL/BSD subprocesses, not linked" reading is acceptable (§2). Only real licensing question.
2. **Runtime calibration** — mesh resolution + rpm ranges tuned to keep representative runs ≤ 1 hr on the target core count (§3.3). Sets the practical DOE ceiling.
3. **Custom solver** — build + validate `matofxMixingFoam` (turbulent `D_eff`) in the tool execution env; confirm MRF + turbulence support in the installed ESI version, else fall back to the two-step "steady MRF flow → freeze → transient scalar transport" (drops live viscosity feedback).
4. **Compute plan for the offline campaign** — hundreds of ≤ 1 hr runs = hundreds of core-hours; sequenced across the workstation cores (GPU does not help). Pre-run, so acceptable, but sizes the calendar.

---

## 10. Build scope & sequencing (2026-07-09)

The tool follows **the usual MATOFX shape**: a single **central Python orchestrator** = the descriptor's `main_script`, run in the job dir under the tool's venv, reading `inputs/inputs.json` and writing `outputs/` + `outputs/outputs.json` (exactly like `thermal_soak.py`). The difference is that the "compute" is **orchestration of two external engines** — FreeCAD (geometry) and OpenFOAM (solve) — invoked as **separate scripts called from the central file**, never inline.

### 10.1 Execution model — the Windows ↔ WSL boundary (the key architectural fact)

Three interpreters, three worlds — the central file is the only thing that spans them:

| Stage | Runs under | Invoked by central via |
|---|---|---|
| **Central orchestrator** | MATOFX tool **venv**, native **Windows** | (is the `main_script`) |
| **Geometry** | **FreeCAD's bundled Python** (`FreeCADCmd.exe`), Windows | `subprocess.run([FREECADCMD, build_geometry.py, params.json])` |
| **Mesh + solve + sample** | **OpenFOAM in WSL2** (Linux) | `subprocess.run(["wsl.exe","-d",DISTRO,"bash",run_openfoam.sh])` |
| **Animation** | **ParaView `pvbatch`** (in WSL, alongside OpenFOAM) | via the same WSL bash driver, or a second `wsl.exe` call |

Consequences that shape the code:
- **FreeCAD script is self-contained** — it runs under FreeCAD's interpreter (Python 3.11, `Part`/`Mesh`/`MeshPart` bundled), **cannot import the MATOFX venv**. Parameters cross via **`geometry_params.json`**; results cross back via **STL files + a `geometry_meta.json`**.
- **FreeCADCmd invocation gotchas (verified 2026-07-09, FreeCAD 1.0.2):** (a) `freecadcmd.exe` **opens every file argument as a document** — passing `geometry_params.json` positionally makes its JSON-mesh importer choke, so the **params path must cross via the `MATOFX_GEOM_PARAMS` env var**, not argv; (b) the script is exec'd with **`__name__` != "__main__"**, so the entry point must run **unconditionally** (no `if __name__` guard) and print its own success/failure breadcrumb, since a bare FreeCADCmd traceback is easy to miss in the job log. Central invocation: `subprocess.run([FREECADCMD, build_geometry.py], env={**os.environ, "MATOFX_GEOM_PARAMS": params_path})`.
- **Path translation.** The job dir is a Windows path; WSL sees it at `/mnt/c/...`. A `to_wsl_path()` helper is a cross-cutting utility. **Decision (recommended): stage the OpenFOAM case into the WSL-native filesystem** (`~/matofx_runs/job_<id>` on ext4), run there, copy results back — `/mnt/c` 9p I/O is slow and snappyHexMesh/reconstructPar are I/O-heavy, which matters against the 1-hr budget. Simpler alternative (run in-place on `/mnt/c`) is the fallback if staging plumbing is a problem.
- **GPU absent** from all of this (confirmed); throughput = `decomposePar` core count.

### 10.2 File layout (`matofx/example_tools/batch_mixing/`)

Proposed alias `batch_mixing`, name **"Stirred-Tank Batch Mixing"** (adjustable).

```
batch_mixing/
  descriptor.toml            # §5 schema
  batch_mixing.py             # CENTRAL orchestrator (main_script) — spans Win↔WSL
  paths.py                   # FreeCAD bin, WSL distro, staging root, to_wsl_path()
  geometry/
    build_geometry.py        # FreeCAD script (FreeCADCmd) — self-contained
    render_preview.py        # dev aid (venv/matplotlib): STLs → geometry_preview.png
  openfoam/
    case_template/           # templated OpenFOAM case (system/ constant/ 0.orig/)
    render_case.py           # fill template from inputs + geometry_meta → concrete case
    run_openfoam.sh          # WSL bash driver: mesh→setFields→decompose→solve→reconstruct→sample
    solver/                  # matofxMixingFoam sources (turbulent D_eff) + Allwmake
  post/
    make_animation.py        # pvbatch → blend.mp4
    parse_results.py         # CoV log→t95, forces log→power, validity  (imported by central)
```

### 10.3 Stage interface contracts (what crosses each boundary)

1. **Central → FreeCAD** — `geometry_params.json`: tank `T,H`; baffle count/width/offset/height; impeller `D/T, C/T`, blade count/angle, shaft dia; additive-layer thickness (from vol%).
   **FreeCAD → Central** — `geometry/*.stl` (named regions: `tankWall`, `topLid`, `baffles`, `impellerBlades`, `hub`, `shaft`) **+ `geometry_meta.json`**: MRF cylinder (centre/axis/radius/z-range → `topoSet cylinderToCell`), `locationInMesh` seed point, liquid height for `setFields`, per-region patch names, refinement hints.
2. **Central → OpenFOAM** — the **rendered case dir** (STLs into `constant/triSurface/`, dicts filled from inputs: rpm→ω, ρ/μ per fluid, gravity, `Sc_t`, threshold, snapshot times, end time, turbulence model). Staged into WSL fs.
   **OpenFOAM → Central** — `postProcessing/CoV/**/CoV.dat`, `postProcessing/forces/**/*.dat`, sampled `alpha` VTKs at fixed snapshot times, solver log.
3. **Central → `outputs/`** — parse → `t95`, `power`, `blend_energy`, `richardson`, validity flags; copy the resampled `alpha_step_*.vtu` series + `blend.mp4`; write `outputs.json`.

Cross-cutting: **every stage maps failure → a clean validity flag** (FreeCAD build fail, snappy fail, solver divergence, no-blend-within-horizon), so a bad run is `Failed`/infeasible, never a silent non-physical field into the surrogate (ADR-0017 posture, and the prior field-surrogate NaN-poisoning lesson).

### 10.4 A modelling simplification to lock for the mesh

**No air / no headspace.** Model the **liquid domain only**, filled to the top, with the **top boundary a free-slip wall** approximating a flat, non-deforming free surface. Keeps it a pure two-liquid `twoLiquidMixingFoam` problem (no VOF, no gas phase). The additive layer sits just under that slip lid. Confirm acceptable.

### 10.5 Build phases (order is partly forced by the install state)

- **Phase 0 — WSL environment (prereq, user-run).** Install OpenFOAM (ESI, apt) + ParaView in WSL2; verify `wsl.exe` reachable from the Windows venv. **Then build `matofxMixingFoam`** (stock `twoLiquidMixingFoam` + turbulent `D_eff`) with `Allwmake`, and smoke-test it on a stock tutorial. *OpenFOAM is not installed yet, so this gates Phases 2+.*
- **Phase 1 — FreeCAD geometry (buildable NOW, no OpenFOAM needed).** `build_geometry.py` + `geometry_params.json` contract; parametric tank/baffles/pitched-blade impeller → named STLs + `geometry_meta.json`. Test standalone via `FreeCADCmd.exe` on Windows; eyeball STLs in FreeCAD GUI. **This is the natural first build.**
- **Phase 2 — OpenFOAM case template + `render_case.py`.** One hand-built golden case (baseline inputs) that meshes and runs to completion in WSL under the hour; then parameterise the dicts. Wire `run_openfoam.sh`. **CASE BUILT + PHYSICS VALIDATED 2026-07-09** — `case_template/` meshes (snappy, 83k coarse, checkMesh OK) and runs `matofxMixingFoam` (MRF, RANS) end-to-end. A 30 s parallel baseline (8 cores, ~40 min, 120 rpm, heavier additive 10% on top) gives a clean monotonic mixing curve **CoV 3.14→6e-4, t₉₅ = 12.5 s**, impeller **power ≈ 0.095 W**, mass conserved; renders in `openfoam/examples/`. Confirmed: turbulent diffusivity stock (§3.2), MRF drives fluid to tip speed, CoV(t)/t₉₅/power computed in post (no coded FO). Remaining: `render_case.py` templating + `run_openfoam.sh`.
- **Phase 3 — Central orchestrator end-to-end.** `batch_mixing.py` sequences Phase 1→2, staging + path translation, `parse_results.py` (t95/power/validity), writes `outputs.json`. One full `inputs.json → outputs.json` run.
- **Phase 4 — Field-surrogate feed + animation.** `sample` → fixed grid at fixed snapshot times; `make_animation.py` (pvbatch → mp4); descriptor `field_timesteps` series wired; validity/finite enforcement at ingest.
- **Phase 5 — MATOFX integration.** Register the tool + execution env (`designs/tool-environment-provisioning.md`), then the offline **study / multi-objective optimisation / scalar+field surrogate** campaign (§7). Help docs updated (§9).

### 10.6 Decisions to confirm before coding Phase 1

1. **Alias/name** — `batch_mixing` / "Stirred-Tank Batch Mixing" OK?
2. **Free-slip lid** simplification (§10.4) acceptable?
3. **Deferred to Phase 2** (not needed for Phase 1): WSL distro name, OpenFOAM flavour (ESI vs Foundation — affects `twoLiquidMixingFoam` availability/API for the mod), staging-vs-in-place I/O choice.

---

## 9. Docs to update when this is built (per CLAUDE.md)

- `docs/user/` — new chapter: running the stirred-tank mixing tool (inputs, reading t₉₅ / power / the animation).
- `docs/admin/30-adding-tools.md` / tool-env docs — the OpenFOAM + FreeCAD + custom-solver execution env and WSL2 host note.
- `ARCHITECTURE.md` §16 — a sub-section for the CFD demo tool + the field-resample-to-fixed-grid surrogate feed.
- `ROADMAP.md` — status entry.
- `NAFEMS_Showcase_Tools_Guide.md` — if this joins the conference narrative alongside the thermo-structural chain.
