# Stirred-Tank Batch Mixing (`batch_mixing`)

A render-rich CFD demo tool for MATOFX. FreeCAD builds a stirred tank (baffles +
pitched-blade impeller) from the inputs; OpenFOAM blends two miscible fluids (a
heavier **additive** charged as a layer on top of the **bulk**) with a frozen-rotor
MRF solve; the tool reports **t₉₅** (time to 95 % homogeneity), **impeller power**,
and the full **blend trajectory** as an animated field.

Design: `designs/cstr-mixing-demo.md`. Built + validated end-to-end 2026-07-10.

---

## What it produces (MATOFX-native outputs)

| Output | Type | Notes |
|---|---|---|
| `t95` | number (KPI) | time to 95 % homogeneity; `null` if not blended within the horizon |
| `impeller_power` | number (KPI) | torque·ω, from the resolved impeller |
| `blend_energy`, `richardson`, `cov_final`, `blended_within_horizon` | number | derived / regime / validity |
| `mixing_history.csv` | 2× `csv_plot` | one tidy CSV → interactive Plotly: **Homogeneity %** (col 1) and **Power** (col 3) vs time |
| `alpha_step_*.vtu` | `field` series | additive fraction on a **fixed grid** — animated field viewer **and** the POD field-surrogate feed |
| `blend.gif` | image | animates inline; additive layer → homogeneous |
| `geometry_preview.png` | image | the built tank/impeller/baffles |

Every geometric + operational input is a free variable → valid target for
parametric studies, multi-objective optimisation (minimise t₉₅ **and** power) and
scalar + field surrogates.

---

## One-time host setup

Needs **Windows + WSL2** (OpenFOAM is Linux-native) and **FreeCAD** on Windows.

1. **FreeCAD 1.0** on Windows (headless `freecadcmd.exe`). Default path
   `C:\Program Files\FreeCAD 1.0\bin`; override with `MATOFX_FREECADCMD`.
2. **WSL2 + Ubuntu** (`wsl --install` if absent). A `.wslconfig` with generous
   memory + swap helps on low-RAM hosts.
3. **OpenFOAM ESI v2606** in WSL:
   ```bash
   curl https://dl.openfoam.com/add-debian-repo.sh | sudo bash
   sudo apt-get update && sudo apt-get install -y openfoam2606-default
   echo "source /usr/lib/openfoam/openfoam2606/etc/bashrc" >> ~/.bashrc && source ~/.bashrc
   ```
4. **Build the custom solver** `matofxMixingFoam` (stock `twoLiquidMixingFoam` +
   MRF; no sudo, installs to `$FOAM_USER_APPBIN`):
   ```bash
   bash openfoam/solver/build_solver.sh
   ```

Config overrides (env vars, see `paths.py`): `MATOFX_FREECADCMD`,
`MATOFX_WSL_DISTRO` (default `Ubuntu`), `MATOFX_OF_BASHRC`, `MATOFX_SOLVER`,
`MATOFX_N_PROCS` (default 8).

---

## Pipeline (what `batch_mixing.py` orchestrates)

```
inputs/inputs.json
  → geometry/build_geometry.py   (FreeCADCmd)  → work/geom/*.stl + geometry_meta.json
  → geometry/render_preview.py                 → outputs/geometry_preview.png
  → openfoam/render_case.py                    → work/case  (fills @TOKENS@ from meta+inputs)
  → openfoam/run_openfoam.sh   (WSL: stage→blockMesh→snappy→topoSet→setFields
                                →decomposePar→mpirun matofxMixingFoam→reconstruct→foamToVTK)
  → post/parse_results.py                      → outputs/mixing_history.csv + scalars
                                               + outputs/alpha_step_*.vtu (fixed grid)
  → post/make_animation.py                     → outputs/blend.gif
  → outputs/outputs.json
```

Each stage maps failure to a clean error (ADR-0017): a bad run is **Failed**, never
a silent non-physical result. A run that doesn't reach the threshold within
`end_time` is **infeasible** (`t95=null`, `blended_within_horizon=0`) — valid output,
correctly censored for the optimiser.

## File layout

```
batch_mixing/
  descriptor.toml            # MATOFX schema (20 inputs, 11 outputs)
  batch_mixing.py            # central orchestrator = main_script
  paths.py                   # host config + Windows↔WSL translation
  geometry/build_geometry.py # FreeCAD (headless): parametric geometry → STLs + meta
  geometry/render_preview.py # geometry preview PNG
  openfoam/
    case_template/           # tokenised OpenFOAM case (system/ constant/ 0.orig/)
    render_case.py           # fill the template from meta + inputs
    run_openfoam.sh          # WSL driver (stage + mesh + solve + export)
    solver/build_solver.sh   # build matofxMixingFoam (= twoLiquidMixingFoam + MRF)
    examples/                # baseline mesh/field/curve/animation artifacts
  post/parse_results.py      # mixing_history.csv + scalars + fixed-grid field series
  post/make_animation.py     # blend.gif
```

---

## Running it

**In MATOFX:** register `descriptor.toml` as a tool and submit like any other —
the browser renders the two Plotly mixing charts, the animated field, and the GIF.

**Standalone** (a job dir holding `inputs/inputs.json`):
```bash
python -c "import os,sys; os.chdir(r'<job_dir>'); \
  sys.path.insert(0, r'<...>/example_tools/batch_mixing'); \
  import batch_mixing; raise SystemExit(batch_mixing.main())"
```
Reduce `end_time` (e.g. 1.5 s) for a fast smoke test; the baseline blend needs
~25–30 s of simulated time (≈40 min wall on a coarse mesh). Baseline result at
120 rpm, 10 % heavier additive: **t₉₅ ≈ 12.5 s, power ≈ 0.095 W** (coarse mesh —
quantitatively mesh-dependent; refine `mesh_base_cell` for production).
