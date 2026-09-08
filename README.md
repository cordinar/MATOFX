# MATOFX SPDM

Funis Consulting Ltd's Simulation Process and Data Management platform.

Single-tenant per install; on-prem Linux / Windows / customer-cloud VM; browser-only
end-user surface; all-Python (NiceGUI on FastAPI); descriptor-driven tool plugin model
with containerised execution. An installed site needs no internet access at runtime —
every asset the browser loads is served by the MATOFX host.

## Quick start (development)

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

alembic upgrade head          # create / migrate the database
ruff check . && mypy && pytest
```

Copy `etc/matofx.example.toml` to `matofx.toml`, point `MATOFX_CONFIG` at it, then:

```bash
matofx-api                    # the API + UI (http://localhost:8000/ui)
matofx-runner                 # the job runner, in a second terminal
```

The first account to register becomes the super user.

## Deploying

`INSTALL.md` is the end-to-end procedure and has two halves: **Part A** builds a
self-contained bundle on a dev machine (`scripts/build_bundle.sh` / `.ps1` — wheel,
hashed lockfile, SBOM, example config, example tools, migration helper, seed scripts),
and **Part B** installs that bundle on a target machine from a fresh image to a running
login screen. `HARDWARE_SIZING.md` covers sizing; `MIGRATE_GUIDE.md` covers moving tools
and workflows between installs.

## Where to look

| Path | What it is |
|---|---|
| `src/matofx/` | The application package. Each sub-package has its own `README.md`. |
| `src/matofx/docs/` | Single-source help (`public` / `user` / `admin`) — feeds the in-app `/ui/help` and the standalone HTML/PDF build. |
| `tests/` | Test suite. |
| `example_tools/` | Reference tool bundles a fresh install can seed (`scripts/seed_example_tools.py`). |
| `example_materials/` | Starter material catalogue. |
| `etc/` | `matofx.example.toml` and example branding assets. |
| `scripts/` | Install, start/stop, bundle build, and first-run seed helpers. |
| `INSTALL.md` | Build the bundle; install it on a target machine. |
| `PROJECT_BRIEF.md` | Source of truth for **design decisions** (47 questions, locked). |
| `ROADMAP.md` | Source of truth for **delivery progress**. |
| `ARCHITECTURE.md` | §1–15 legacy v1 reference (useful when porting concepts); §16+ the as-built record of this codebase. |
| `decisions/` | ADRs for architectural decisions beyond the brief. |
| `designs/` | Per-feature design documents. |
| `CONTRIBUTING.md` | Branch rules, dev setup, coding conventions, ADR process. |

## Status

Phase 1 is complete and Phase 2 is well into delivery — workflows, approvals, compare,
live jobs, parametric studies, surrogate models, optimisation, data lineage, V&V
tracking, KPI extraction, job cohorts, design-space coverage, and the licence /
compliance subsystem have all landed. SSO/MFA, backups, and the broader
install / i18n / observability tail remain. `ROADMAP.md` has the detail.

## Licence

Proprietary — © Funis Consulting Ltd. See `LICENSE`. Third-party dependency licences are
recorded in `THIRD_PARTY_NOTICES.txt` and summarised in `THIRD_PARTY_LICENSES_SUMMARY.md`.
