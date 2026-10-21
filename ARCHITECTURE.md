# MATOFX SPDM — Architecture (legacy_code/)

> Reference document describing the existing application in `legacy_code/`.
> The legacy tree is read-only; this file lives under `OUTPUTS/` so it can be edited and extended without touching the reference code.

---

## 1. What MATOFX SPDM is

MATOFX SPDM ("Simulation Process & Data Management") is a desktop-style web application that lets a small organisation submit, track, approve and review engineering simulation/calculation jobs. Users log in through a browser, fill in a form for a tool (e.g. material-properties calculator, heat-transfer model, CAD upload), and the system runs the underlying executable in the background, captures logs, and stores results against a hierarchy of **Project → Scenario → Job/Workflow**.

The application is implemented as **two cooperating Python processes** sharing a single SQLite database:

| Process               | Entry point             | Port         | Role                                               |
| --------------------- | ----------------------- | ------------ | -------------------------------------------------- |
| Streamlit UI          | `matofx.py`             | 8501–8600    | Login, navigation, forms, dashboards, admin space  |
| FastAPI job server    | `fastapi_job_server.py` | 8000         | Job queue, subprocess execution, status, logs      |
| Licence usage logger  | `licence_usage_logging.py` | n/a       | Background hash-chained licence audit log          |

`launch_all_services.py` is the orchestrator that boots all three.

---

## 2. Top-level directory map

```
legacy_code/
├── matofx.py                   # Streamlit entry point (UI + page routing)
├── fastapi_job_server.py       # FastAPI backend (job queue + runner)
├── launch_all_services.py      # Process orchestrator (uvicorn + streamlit + licence)
├── licence_usage_logging.py    # Tamper-evident licence usage logger
├── inactivity_check.py         # (currently disabled) idle-session reaper
├── forms.py                    # Shared form helpers
├── global_settings.json        # Runtime config (env, URLs, email domain, FreeCAD path)
├── requirements.txt            # Pinned Python dependencies
│
├── dashboard_pages/            # Streamlit pages (one .py per nav entry)
│   ├── regular/                #   Logged-in user pages: home, my_jobs, compare_jobs, account
│   └── admin/                  #   Admin pages: users, DB, teams, tools, workflows, paths, usage, logs
│
├── functions/                  # Application logic (imported as `from functions import ...`)
│   ├── database_create.py      # Schema definitions + first-run seeding
│   ├── database_actions.py     # All DB read/write helpers (alias `dba`)
│   ├── general_func.py         # Misc helpers (email, idle, licence usage, pyvista)
│   ├── login_logout.py         # Authentication + session bootstrap
│   ├── session_manager.py      # Cookie-based session restore
│   ├── licence_check.py        # Hardware fingerprinting + licence validation
│   ├── licence_tracker.py      # Per-feature licence accounting
│   ├── new_project.py / new_scenario.py / new_job.py
│   ├── existing_project.py / job_search.py / job_approvals.py
│   ├── new_custom_workflow.py / edit_custom_workflow.py / custom_workflow_render.py
│   ├── render_forms.py / render_forms_auto.py     # Generic tool-form renderer
│   ├── update_input_json.py    # Persist user inputs into job inputs.json
│   ├── submit.py               # POST job to FastAPI (and HPC stub)
│   ├── copy_to_fsx.py          # Stage tool static files into job working dir
│   ├── code_editor.py          # In-app script editing
│   ├── cad.py / gltf.py / pdf_page.py / web_page.py    # Demo pages (3D, PDF, web)
│   ├── stp_to_stl.py           # FreeCAD-driven STEP→STL conversion
│   ├── encode_image.py / get_json_values.py / queries.py / report.py / web.py / go_to.py / scenario_close.py
│
├── tools/                      # Tool plugins (data + scripts)
│   ├── cad_upload/
│   ├── colour_check/
│   ├── heat_transfer/
│   ├── material_properties/
│   ├── material_properties_repeat/
│   └── icons/
│
├── workflows/                  # Multi-tool workflow "master" pages
│   ├── icons/
│   ├── material_properties_to_heat_transfer/
│   └── wf3/
│
├── DDP/                        # Vendored Ansys "Data Driven Process" wrapper
│   ├── main.py                 #   PythonWrapper context manager
│   ├── CLI.py / IO.py          #   InputParser / OutputParser, JSON-in / JSON-out contract
│   ├── execute.py / unit.py / log.py / constants.py / Exceptions.py
│   ├── osl/                    #   optiSLang messaging layer
│   └── report/                 #   HTML report template + generator
│
├── database/                   # SQLite files (per ENVIRONMENT, e.g. staging_database.db)
├── job_logs/                   # Per-job stdout/stderr (job_logs/{job_id}.log)
├── usage_logs/                 # Software-usage and licence-usage logs
├── logos/  static/  certs/     # Branding, demo assets, TLS material
├── tutorials/  licence/        # In-app tutorials and licence files
├── test/                       # Pytest tree (e.g. basecake_checking/oreo_cookie_region_analysis)
│
├── tools - Copy/               # ⚠ Snapshots — do NOT treat as live code
├── tools_archive/              # ⚠ Older revisions of tools
└── workflows - Copy/           # ⚠ Snapshot
```

---

## 3. Process model

```
            ┌──────────────────────────────┐
 browser ──▶│  Streamlit (matofx.py)       │
            │   - login / nav / forms      │
            │   - reads + writes SQLite    │
            └──────┬───────────────────────┘
                   │ HTTP POST /submit
                   ▼
            ┌──────────────────────────────┐
            │  FastAPI (fastapi_job_server)│
            │   - Queued / Running / Done  │      ┌─────────────────────────┐
            │   - subprocess.Popen ────────┼─────▶│ Tool venv python.exe    │
            │   - writes job_logs/*.log    │      │  cwd = job working dir  │
            │   - GPU scheduler loop ──────┼──┐   │  inputs.json → outputs  │
            └──────┬───────────────────────┘  │   └─────────────────────────┘
                   │ writes                   │ HTTP /resources
                   ▼                          ▼
            ┌──────────────────────────────┐  ┌─────────────────────────┐
            │ SQLite: {ENV}_database.db    │  │ GPU agent (remote host) │
            │  jobs, scenarios, projects,  │  │  GPU_AGENT_URL          │
            │  users, tools, workflows,    │  └─────────────────────────┘
            │  permissions, paths …        │
            └──────────────────────────────┘
                   ▲
                   │ reads
            ┌──────┴───────────────────────┐
            │ licence_usage_logging.py     │
            │  hash-chained audit log      │
            └──────────────────────────────┘
```

There is **no message queue** — coordination happens entirely through SQLite rows and HTTP calls between the Streamlit and FastAPI processes.

---

## 4. Configuration & environment

### `global_settings.json`

Loaded by both the Streamlit and FastAPI processes at startup and spread into `streamlit.session_state`.

| Key            | Purpose                                                            |
| -------------- | ------------------------------------------------------------------ |
| `ENVIRONMENT`  | Selects DB filename `database/{ENVIRONMENT}_database.db`           |
| `url`          | Public URL embedded in notification emails                         |
| `FASTAPI_URL`  | Streamlit → FastAPI base URL (`http://localhost:8000`)             |
| `GPU_AGENT_URL`| Remote host running the GPU resource agent                         |
| `FREECAD_PYTHON`| Path to FreeCAD's bundled Python (used by `stp_to_stl.py`)        |
| `EMAIL_DOMAIN` | Domain used for outbound email                                     |

### Environment variables

- `venv_MATOFX` — path to the main app virtualenv. `launch_all_services.py` resolves Python and Streamlit executables from it.
- Per-tool venv env-vars — declared by name in each tool's `*_descriptors.json` (`venv` field). The submit code reads `os.environ.get(<name>)` to find the interpreter to launch the tool with.
- `VENV_DDP_MAIN` — used by the HPC submission stub in `functions/submit.py:submit_hpc_job`.

---

## 5. Database — the integration bus

All state — users, projects, scenarios, jobs, tools, workflows, permissions, file paths, approvals, licence accounting — lives in a single SQLite file at `database/{ENVIRONMENT}_database.db`. Schema is defined in `functions/database_create.py:create_db()` and seeded once by `fill_new_db()` on first run (triggered from `matofx.py` if the DB file is missing).

### Core tables and how they relate

```
 users ─────┐
            │  (username FK on creator/approver fields)
            ▼
 projects ──▶ scenarios ──▶ jobs ──▶ approvals
                                  │
                                  └──▶ workflow_state / workflow_datetime_*
                                       (workflow_yes_no = 'Yes' jobs cluster
                                        by scenario_id into a workflow)

 tools                  ── declares resource needs, queue, email/approval policy
 workflows              ── canonical workflow templates (tool_1..tool_10)
 custom_workflows       ── user-defined variations
 permissions            ── feature × access-right matrix (drives nav)
 access_rights / teams  ── lookup tables
 hpc_queues / aos_servers / paths   ── infra config rows
 user_sessions          ── persistent login tokens
```

### Conventions

- **Always go through `functions/database_actions.py` (`dba`).** It enables FK constraints, sets `PRAGMA busy_timeout`, JSON-encodes list values, and validates identifiers via `SAFE_IDENT`.
- **Lists in DB columns are JSON-encoded** by `dba.insert_row` / `dba.update_row_multi`. Read them back through the same helpers, never hand-rolled SQL.
- **Schema changes are not migrated automatically.** Adding a column requires either editing `database_create.py` and recreating the DB, or running a one-off `dba.insert_column` against the existing file.
- **ENUM-style fields** (`logon_enabled`, `workflow_yes_no`, …) are enforced by `CHECK` constraints, e.g. `IN ('Yes','No')`.

### `jobs` lifecycle column states

`Created → Queued → Running [→ Running - repeat] → Complete | Failed`

`workflow_state` follows the same trajectory but is rolled up across all jobs that share a `scenario_id` and `workflow_yes_no='Yes'`.

---

## 6. Streamlit UI (`matofx.py`)

Responsibilities of `main()`:

1. **Bootstrap session state** — load `global_settings.json` into `sst`, set `sst.DB_PATH`, `sst.wdir`, `sst.VERSION`.
2. **First-run setup** — if no DB file, create schema + seed and rerun.
3. **Page config + CSS** — title, favicon, hide default header, custom banner colour `#40A1A4`.
4. **Header banner** — base64-embedded logo + username + access-right.
5. **Licence init** — once per session: `sst.licence_details = licence_check.licence_check()`.
6. **Auth gate** — if `users` table is empty, show first-user/super-user registration; else show login. Idle timeout is checked each rerun via `general_func.check_idle_time`.
7. **Dynamic navigation** — read `permissions` table, filter by `sst.user_access_rights`, build `Dashboard / Admin Space / My Account / Demo` groups via `st.navigation`. **Adding a page requires both** registering it in `page_mapping` and adding a row in `permissions`.

Pages are registered as `st.Page` objects pointing either at module functions (`add_edit_user.tabs`) or at file paths (`dashboard_pages/regular/home.py`).

---

## 7. Job execution (`fastapi_job_server.py`)

### Endpoints

| Method | Path               | Purpose                                |
| ------ | ------------------ | -------------------------------------- |
| POST   | `/submit`          | Queue a job (CPU or GPU-pending)       |
| GET    | `/status/{job_id}` | Return job_id, current state, log_path |
| GET    | `/job_logs/{job_id}` | Return full log file content         |

### `JobRequest` schema

```python
job_id: str
exe_path: str
args: List[str] = []
working_dir: Optional[str] = None
job_type: str
requires_gpu: bool = False
```

### `run_job` flow

1. Set DB row to `Running`, write per-job log to `job_logs/{job_id}.log`, send "started" email if the tool has `email_notifications='Yes'`.
2. If the job is part of a workflow and the workflow is not yet `In Progress`/`Complete`/`Failed`, mark all sibling workflow jobs `In Progress`.
3. `subprocess.Popen([exe_path] + args, cwd=working_dir, stdout=log, stderr=log)` and `wait()`.
4. On `returncode == 0` and `'repeat' not in job_type`:
   - Read tool descriptors; call `clean_directory(working_dir, retain_files, delete_io_files)` to delete intermediate files (preserves `inputs/` and `outputs/` except for explicitly listed deletions).
   - Mark job `Complete`, send "complete" email.
   - Roll up the workflow: if all sibling workflow jobs are `Complete` → workflow `Complete`; if any is `Failed` → workflow `Failed`; once all are terminal, stamp `workflow_datetime_completed`.
5. On `returncode == 0` and `'repeat' in job_type`: mark `Running - repeat` (tool is expected to keep iterating; cleanup/rollup are deferred until the non-repeat finish).
6. On any non-zero exit or exception: mark `Failed`, append exception to log, send "failed" email.

### GPU scheduling

- `pending_gpu_jobs` is an in-memory list of jobs with `requires_gpu=True`.
- `gpu_scheduler_loop` (started once at FastAPI startup) polls `GPU_AGENT_URL/resources` every 5s; if free GPU memory ≥ the tool's `gpu_memory_requirement`, it dequeues and runs the job locally.
- This is single-host scheduling against a remote GPU agent — there is no multi-node scheduler.

### Crash recovery

On import, if the DB exists, `fastapi_job_server.py` runs `UPDATE users SET logged_on = 0` so that crashed sessions don't permanently lock users out.

### Email

`functions/general_func.py:send_email` sends HTML mail via SMTP. `send_notification` composes a per-status message (Running / Complete / Failed) and includes a deep-link back to the job using `global_settings.url + urlencoded(project, scenario, workflow, tool)`.

---

## 8. Tools — the plugin contract

Each tool lives in `tools/<tool_alias>/`:

```
tools/<tool_alias>/
├── input_output/
│   └── <tool_alias>_descriptors.json   # the contract
└── static/
    └── main_model.py                   # (or whatever main_script names)
```

### `<tool_alias>_descriptors.json`

The single file that drives both the UI form and post-job cleanup:

| Key                     | Used by                                                             |
| ----------------------- | ------------------------------------------------------------------- |
| `venv`                  | `submit.get_python_arguments` — env-var name for tool's interpreter |
| `main_script`           | `submit.get_python_arguments` — script launched in `working_dir`    |
| `input_groups`          | `render_forms` — form layout (column/row/sub_columns)               |
| `input_columns_layout`  | `render_forms` — column width ratios                                |
| `inputs`                | `render_forms` — per-field type/label/options/required/units        |
| `retain_files`          | `fastapi_job_server.clean_directory` — files kept after success     |
| `delete_io_files`       | `fastapi_job_server.clean_directory` — files explicitly removed from `inputs/`/`outputs/` |

**Adding or changing a tool input means editing this JSON, not writing UI code.** `functions/render_forms.py` and `render_forms_auto.py` build the entire form generically.

### Runtime metadata

The `tools` DB row holds operational settings: `cpu_cores_requirement`, `ram_memory_requirement`, `gpu_memory_requirement`, `licences_required`, `email_notifications`, `approvals`, `approvers`, `queue`, `server`, `url`, `port`, `released`, `categories`, `downstream_tools`. These are surfaced/edited in the *New/Edit tool* admin page.

### Job working directory

For each submission the UI:

1. Computes `job_dir = {sst.file_share_dir}/job_{job_id:07}` (`file_share_dir` comes from the `paths` table key `job_execution`).
2. Calls `copy_to_fsx.copy_files(tool_alias, job_dir)` — copies everything in `tools/<tool_alias>/static/` into `job_dir`.
3. Writes user inputs into `inputs/<tool_alias>_input.json` via `update_input_json`.
4. POSTs `/submit` with `exe_path = <tool venv>/python.exe`, `args = [main_script]`, `working_dir = job_dir`.

The duplicates `tools - Copy/` and `tools_archive/` are **snapshots, not active code** — runtime never reads them.

---

## 9. Workflows

`workflows/<workflow_alias>/<workflow_alias>_workflow_master.py` is a Streamlit "master" page that composes several tool tabs. Pattern (from `material_properties_to_heat_transfer`):

```python
def main(workflow_name, selected_scenario_id):
    tab_names = ['Material Properties Calculator', 'Heat Transfer Model']
    tab1, tab2 = st.tabs(tab_names, default=sst.go_to_tab if 'go_to_tab' in sst else None)

    workflows = dba.get_scenario_jobs(selected_scenario_id, workflow_yes_no="Yes")
    workflow_options = {tool: jid for jid, tool, wf in workflows}

    with tab1:
        render_forms.tool_render_routine(selected_job_id=workflow_options[tab_names[0]],
                                         selected_job_tool=tab_names[0])
    with tab2:
        render_forms.tool_render_routine(selected_job_id=workflow_options[tab_names[1]],
                                         selected_job_tool=tab_names[1],
                                         in_workflow=True,
                                         previous_tool=tab_names[0])
```

`in_workflow=True` plus `previous_tool=...` lets a downstream tab pull outputs from the upstream tool's job dir to pre-fill its inputs. The `workflows` table is the canonical template (`tool_1 .. tool_10`); `custom_workflows` holds user-defined variants with permissions.

---

## 10. DDP — the JSON-in / JSON-out tool wrapper

`legacy_code/DDP/` is a vendored helper from Ansys (preserve copyright header). Tool main scripts are expected to use it as a context manager:

```python
from DDP.main import PythonWrapper

with PythonWrapper(tool_name="material_properties",
                   tool_version="1.0",
                   input_json_filepath="inputs/material_properties_input.json",
                   output_json_filepath="outputs/material_properties_output.json") as pw:
    ip = pw.input_parser
    op = pw.output_parser
    # ... do work, write op.<field> = value ...
```

`__exit__` calls `op.check_all_fields_assigned()` then `op.write_json()`. On exception, the JSON is still written (without integrity check) and the exception is re-raised so the FastAPI runner sees a non-zero exit code. `DDP/osl/` integrates with Ansys optiSLang messaging; `DDP/report/` produces an HTML report from `report_template.html`.

---

## 11. Authentication, sessions, permissions

- **First user** — when `users` is empty, `matofx.main` shows the first-user registration form and forces `access='super_user'` (`add_edit_user.add_user(..., first_user=True)`).
- **Login** — `functions/login_logout.login` validates credentials (`bcrypt`-hashed passwords in the `users` table) and sets `sst.logged_in = True`, populating `sst.user` and `sst.user_access_rights`.
- **Persistent sessions** — `user_sessions` table holds tokens with `created_at`, `last_seen`, `expires_at`. `functions/session_manager.py` can restore a session from a cookie (currently commented out in `matofx.py`).
- **Idle timeout** — `general_func.check_idle_time` runs each rerun; logs the user out if `sst.now - last_activity` exceeds the configured limit.
- **Crash safety** — `fastapi_job_server.py` resets `users.logged_on = 0` on import.
- **Per-feature permissions** — `permissions` is a feature×access_right matrix; the UI iterates rows and checks `permissions.loc[i, sst.user_access_rights]` to decide whether to add the page to the sidebar.

---

## 12. Licensing

- `functions/licence_check.py` derives a hardware fingerprint (MAC, disk serial, CPU id, OS UUID), and validates licence files in `licence/`.
- `licence_usage_logging.py` runs as its own subprocess, periodically writing a tamper-evident log entry to `usage_logs/`. Each entry is hash-chained and signed using a key derived deterministically from machine fingerprint + sorted licence ids (PyNaCl `SigningKey`). Don't reimplement licence accounting in the UI.
- `licence_tracker.py` exposes per-feature live usage (concurrent users, expiry, etc.) consumed by the *Usage Statistics* admin page.

---

## 13. Logging & observability

- **Per-job logs** — `job_logs/{job_id}.log`, populated by FastAPI's redirected stdout/stderr.
- **Software usage log** — `usage_logs/software_usage.log`, plain-text append-only, created once on first launch.
- **Licence audit log** — `usage_logs/` (hash-chained, see §12).
- **Log inspection page** — `dashboard_pages/admin/log_inspection.py` renders these for admins.
- There is **no centralised logger** (no `logging.basicConfig`); modules `print()` for diagnostics and `st.error/st.success` for user-visible messages.

---

## 14. Where to start when…

| Task                                         | Start here                                                                 |
| -------------------------------------------- | -------------------------------------------------------------------------- |
| Add a new dashboard / admin page             | `matofx.py` (register `st.Page`, add to `page_mapping`) + insert row in `permissions` |
| Add a new tool                               | New `tools/<alias>/` with `static/` + `input_output/<alias>_descriptors.json`; row in `tools` table |
| Change a tool's form fields                  | Edit `tools/<alias>/input_output/<alias>_descriptors.json` only            |
| Change job-execution behaviour               | `fastapi_job_server.py:run_job` and `clean_directory`                      |
| Change job submission payload                | `functions/submit.py` + `JobRequest` model                                 |
| Add a DB column                              | `functions/database_create.py` (for fresh DBs) + `dba.insert_column` for migration |
| New workflow (multi-tool)                    | `workflows/<alias>/<alias>_workflow_master.py` + row in `workflows` table  |
| Notification logic                           | `fastapi_job_server.py:send_notification` + `general_func.send_email`      |
| Idle/auth                                    | `functions/login_logout.py`, `functions/session_manager.py`, `general_func.check_idle_time` |
| GPU scheduling / remote agent                | `fastapi_job_server.py:gpu_scheduler_loop` + `GPU_AGENT_URL`               |

---

## 15. Known smells / things to be careful with

- **Duplicate trees** — `tools - Copy/`, `workflows - Copy/`, `tools_archive/`, and `database/staging_database - Copy.db` are snapshots. Never wire them up; assume drift from the live versions.
- **Module-level `sst` reads** — many `functions/*.py` files import `streamlit.session_state as sst` and read it at import time (e.g. `submit.py` reads `sst.wdir`). They will fail outside the Streamlit process. FastAPI handlers re-read `global_settings.json` directly to avoid this.
- **No DB migrations** — schema evolution is manual.
- **In-memory GPU queue** — `pending_gpu_jobs` is lost on FastAPI restart; jobs already marked `Queued (GPU pending)` need manual re-submission.
- **Email + URLs read settings on each call** — `send_notification` re-opens `global_settings.json` every time; not a hot path so fine, but be aware.
- **`shell=False` Popen with raw paths** — `run_job` runs `[exe_path] + args` directly; tool descriptors must keep `main_script` as a relative path inside `working_dir`.
- **Inactivity reaper disabled** — `inactivity_check.py` exists but is commented out in `launch_all_services.py`. The crash-safety `logged_on=0` reset on FastAPI startup is the only thing covering crashed sessions.

---

## 16. v2 architecture (`OUTPUTS/matofx/`) — what's being built

The v2 rewrite lives entirely under `OUTPUTS/matofx/`. The legacy tree is reference-only; nothing in v2 depends on it at runtime. The shape below tracks the **as-built state** — `PROJECT_BRIEF.md` is the source of truth for *decisions*, `ROADMAP.md` for *delivery progress*; this section captures the live module map for cross-cutting orientation.

### Top-level package layout

```
OUTPUTS/matofx/src/matofx/
├── api/            # FastAPI routes (HTTP surface; consumes services)
│   ├── app.py / daemon.py / deps.py / errors.py / schemas.py
│   └── routers/        # admin, admin_db_explorer, auth, jobs, projects,
│                       # tools, tutorials, workflows
├── audit/          # Hash-chained Ed25519-signed audit log (§B11 / §E26)
├── cli/            # `matofx` admin CLI (`audit verify`, etc.)
├── config/         # Pydantic Settings; TOML + env-var overrides
├── db/             # SQLAlchemy 2.x async; SQLite + Postgres; Alembic
│                   # (migrations 0001..0023 — see §16.7)
├── features.py     # `feature_enabled("name")` per-customer flag service
├── hierarchy/      # Project → Scenario → Job ORM + service + ACL
│                   # (Job + JobService.list_visible_to_user / search_by_inputs / get_output_document)
├── notify/         # Email notification scaffold (Phase 1 single channel)
├── obs/            # Structured JSON-Lines logging; metrics/OTel deferred
├── runner/         # Job runner daemon (subprocess execution backend)
├── secrets/        # SecretsStore Protocol (file / env / customer)
├── tools/          # Tool registry + descriptor + execution backends
│   ├── descriptor.py    # `ToolDescriptor` Pydantic model (port of legacy *_descriptors.json)
│   ├── orm.py           # `Tool` row + state machine (new / test / released / archived)
│   ├── service.py       # `ToolService` (install / list / promote / archive, audited)
│   ├── states.py / versioning.py / loader.py / syntax_check.py / paths_service.py
│   ├── exec/            # Execution targets: local / SSH-AOS / WinRM-AOS / Slurm / MS-HPC
│   ├── exec_orm.py      # `AosHost` / `HpcCluster` / `PythonEnv` / `RuntimeSetting` / `StorageLocation`
│   ├── staging.py       # Per-job working-dir staging
│   ├── approvers.py     # Approver-cascade invariants (sole-approver guard)
│   └── forms/           # descriptor → Pydantic v2 form pipeline (2026-05-08)
│       ├── model.py     #   Cached per (alias, version) per §F30 Q5
│       ├── errors.py    #   `FieldError` + `FormValidationError` (plain-English per-field)
│       ├── render.py    #   `render_form(descriptor) → FormHandle`; one ui.expansion per group
│       └── seeders.py   #   `load_presets` / `from_previous_job_inputs` / `from_descriptor_defaults`
├── workflows/      # Admin-released + custom workflows (2026-05-11)
│   ├── orm.py           # `Workflow` (kind ∈ {admin, custom}), `WorkflowMember`,
│   │                    #   `WorkflowVersion`, `WorkflowShare`, `WorkflowRun`
│   ├── service.py       # `WorkflowService` (admin + custom CRUD, archive cascade, copy_to_owner)
│   ├── ordering.py      # `compute_order` — union of forward downstream_tools +
│   │                    #   backward `InputField.upstream_tool` / `downstream_input`;
│   │                    #   BFS-of-chains + topo tail; `CycleError`
│   ├── run_service.py   # Runtime: WorkflowRun + child Job rows, tab gating
│   ├── errors.py / _custom_helpers.py
├── tutorials/      # In-app tutorial pages (orm / service / storage / errors)
├── ui/             # NiceGUI on FastAPI; the dashboard
│   ├── app.py / api_client.py / state.py / _holder.py / units.py
│   ├── format.py        # Pretty-printing helpers (datetime / job_id / state / tool name)
│   ├── theme.py         # Light/dark Palette + CSS vars + Plotly helpers (2026-05-10)
│   └── pages/           # One file per route (see §16.6)
└── users/          # Identity, RBAC, teams, sessions, password reset, role catalog
```

### Cross-cutting design rules (encoded in memory + lints)

- **Single auth path** — every route consumes `current_user` from `matofx.users.service`; no module reaches into `os.environ` for credentials (CI lint enforces this outside `matofx.config.secrets`).
- **Audit is mandatory for state changes** — every service write goes through `matofx.audit` (§E26 18-field schema; chain-head guard).
- **Tool authors never touch UI / Docker / DB** — they ship a `descriptor.toml` + a Python orchestrator + assets. The framework derives forms via `matofx.tools.forms`, stages via `matofx.tools.staging`, runs via `matofx.tools.exec`, persists via `matofx.tools.service`.
- **All datetimes UTC on the wire and at rest** — UI rendering goes through `matofx.ui.format.format_user_datetime` to produce the `dd mmm yyyy HH:MM:SS TZ (UTC)` shape locked on 2026-05-08.

### Forms pipeline (NEW, 2026-05-08)

`matofx.tools.forms` is the bridge between a tool's `descriptor.toml` (authoring surface) and the runtime that validates user-submitted inputs and renders the input form. Replaces the legacy `functions/render_forms.py` + `render_forms_auto.py` + ad-hoc validators.

```
ToolDescriptor                       (Pydantic model, parsed from descriptor.toml)
    │
    ├─► build_input_model(d) ──► type[BaseModel]   (cached per (alias, version) per §F30 Q5)
    │       └─ Annotated[T, AfterValidator] for select/multiselect/file
    │       └─ Field(ge=, le=) for numeric bounds
    │       └─ extra="forbid" so payload typos surface as FieldError
    │
    ├─► validate_inputs(d, payload) ──► dict (success) | FormValidationError (failure)
    │       └─ Pydantic ValidationError → FieldError list with plain-English messages
    │       └─ SumCheck post-validation (deduplicated, missing-participant tolerant)
    │
    ├─► render_form(d) ──► FormHandle (state dict + widgets + error_labels)
    │       └─ One ui.expansion per [[input_groups]] entry; sub_columns honoured
    │       └─ FormHandle.values() / .seed(values, fill_missing_with_defaults=) / .show_errors(exc) / .clear_errors()
    │
    └─► seeders.load_presets(bundle, descriptor) ──► list[Preset(name, description, values)]
        seeders.from_previous_job_inputs(inputs_doc) ──► {name: value} dict
        seeders.from_descriptor_defaults(descriptor) ──► {name: default} dict
        (each result is dict-shaped; FormHandle.seed(...) consumes any of them)
```

Errors land as `FormValidationError` carrying a list of `FieldError(name, label, message, sum_parameters)` for the renderer to surface under each offending widget. The translation layer in `model._friendly_message` maps Pydantic's technical codes (`int_parsing`, `greater_than_equal`, `extra_forbidden`, etc.) through fixed-phrase + bound-formatter dispatch tables; custom `AfterValidator` closures raise `ValueError` with the friendly message directly.

### My Jobs workspace (landed, 2026-05-08)

Replaces the 100-line flat list at `/my-jobs`. The single interactive surface where users do work on jobs:

```
/my-jobs                                     ← unified workspace (the only "doing work" page)
    │
    ├─ Sidebar (cascading):
    │     Project ─► Scenario ─► Tool / Workflow ─► Job-ID (search-as-you-type)
    │     ↕  bidirectional: Job-ID selection back-fills the four pickers above
    │
    ├─ Left meta column:   Project + Scenario info cards
    ├─ Mid column:         preset / previous-job / defaults popovers (state == created)
    └─ Right pane (state-shaped):
          created          → matofx.tools.forms.render
          queued/running   → repainting status row + cancel + streaming logs
          complete         → outputs panel + 3 download buttons
          failed           → error + logs
          pending_approval / denied / running_repeat → visible-but-not-actionable notice (Phase 2 actions)
          (workflow row)   → "Workflow rendering ships in Phase 2" placeholder

/jobs/{id}                  ← deep-link alias  → ui.navigate.to(/my-jobs?job=...)
/scenarios/{id}/submit      ← deep-link alias  → ui.navigate.to(/my-jobs?scenario=...&pick_tool=1)
```

URL state lives in query params (`?proj=&scen=&tool=&job=`). `history.push` on `job` change (back-button steps through viewed jobs); `history.replace` on filter chatter (no history noise).

Empty-state right pane (no params, no remembered selection) shows "Recent activity" — last 10 jobs the user touched.

Every label flows through `matofx.ui.format`: tool **name** not alias; `dd mmm yyyy HH:MM:SS TZ (HH:MM:SS UTC)` timestamps; zero-padded job IDs; friendly state labels.

### Job lifecycle and the "one tool per scenario" invariant (locked 2026-05-08)

Each scenario can hold **at most one job per tool**. Enforced by a DB-level `UNIQUE (scenario_id, tool_alias)` on `job` (Alembic migration 0017). To run the same tool again, the user creates a new scenario — that's the unit of "another attempt" in the SPDM data model.

The lifecycle uses **draft jobs**:

```
[ user picks scenario + tool ]
    │
    ▼
POST /api/scenarios/{sid}/draft-jobs   ← idempotent: if (scen, tool) already
    │                                    has a row, returns it (any state).
    ▼                                    Otherwise inserts a fresh `created`-state row.
[ Job ID surfaces in workspace picker; form widgets render against the draft ]
    │
    ▼
[ user fills form, clicks Submit ]
    │
    ▼
POST /api/jobs/{id}/queue              ← transitions `created` → `queued`,
    │                                    stages working dir, hands off to runner.
    ▼
[ runner picks up, state advances to running → complete | failed ]
```

`JobService.create` translates the unique-constraint `IntegrityError` into a friendly `DuplicateToolInScenarioError` carrying the existing job's id; the API layer surfaces this as `409 Conflict` with body `{"detail": ..., "existing_job_id": N}` so the workspace can navigate to the existing run rather than dead-end.

The legacy "create + queue in one shot" endpoint `POST /api/scenarios/{sid}/jobs` still exists (the runner-test flows use it) but the workspace's interactive flow goes through the draft path so the Job ID is visible from the moment the form opens, not after submit.

### 16.2 Theme system (shipped 2026-05-10)

Per-user dark/light theme, default `dark`, toggled live without a page reload.

- **Persistence** — `user.theme_preference` (`TEXT NOT NULL DEFAULT 'dark'`, CHECK `IN ('light','dark')`), migration `0018`. Round-trips through `UserService.set_theme_preference` (idempotent, audited as `auth.user.set_theme_preference`) and `POST /api/auth/me/theme`.
- **Toggle location** — Account page only (`/account` → "Display" card). No header widget (explicit user pref).
- **Live flip** — `matofx.ui.theme` defines `LIGHT_PALETTE` and `DARK_PALETTE` (`Palette` dataclass). The chrome injects both as CSS variables in one `<style>` block: `:root, body { --matofx-page-bg: <light>; ... }` and `body.body--dark { --matofx-page-bg: <dark>; ... }`. NiceGUI's `ui.dark_mode()` toggles the `body--dark` class on the client → variables re-point → no reload, no re-render.
- **Plot integration** — `apply_theme_to_plotly(fig, theme=None)` sets template + paper/plot bg + font colour from the active palette. **Never** pin `template="plotly_white"`; route every figure through this helper. Centralised Plotly config lives in `matofx.ui.theme`: `MATOFX_PLOTLY_CONFIG = {"displaylogo": False, "responsive": True}`; `themed_plotly(fig) → {data, layout, config}` for `ui.plotly`; `export_plotly_html(fig)` for CDN-embed downloads.

### 16.3 Compare — Jobs / Workflows / Scenarios (jobs 2026-05-10; workflows + scenarios 2026-05-24)

`/compare` is a full v2 surface — pick 2–6 items, see overlay charts + side-by-side input/output diff, download HTML or PDF report. **Three modes from one page** (G36), chosen by a plain-English radio on the Select tab and carried in `?mode=`: **Jobs of one tool**, **Runs of one workflow**, **Scenarios**. Replaces the legacy `dashboard_pages/regular/compare_jobs.py`.

```
OUTPUTS/matofx/src/matofx/ui/pages/compare/
├── page.py             # @ui.page("/compare") — Select + Compare tabs; per-mode pickers + renderers
├── selection.py        # URL parser (?mode=tool|workflow|scenario&...) + cross-tool detection
├── diff.py             # compute_differing_keys
├── render.py           # cell renderer (header / inputs / outputs) with optional download_callback
├── overlay.py          # merged + per-column individual charts (sized to column width)
├── field_overlay.py    # JobFieldOutput value object (field frames; rendered inline by render.py + report.py)
├── report.py           # build_compare_report (HTML + auto-print PDF)
├── workflow_groups.py  # PURE: align_workflow_runs → per-step alignment + version-mix flag
└── scenario_groups.py  # PURE: match_scenarios → per-tool + per-workflow groups across scenarios
```

**The job-group engine.** Every mode resolves to one or more *job groups* — a same-tool side-by-side block rendered by `page._render_job_group(client, columns: list[GroupColumn])`. A `GroupColumn` is `(job_id | None, participant_label, missing_message)`:
- **Jobs** — one group; columns are the ticked jobs (no participant label).
- **Workflows** — `align_workflow_runs(runs)` (same-definition guard — cross-definition refused per G36 Q4; version-mix flag from `workflow_version_snapshot`; union of steps ordered by `workflow_position`) → one collapsible group per step, columns = the runs.
- **Scenarios** — `match_scenarios(...)` groups standalone jobs by `tool_alias` and workflow runs by definition; a tool/workflow in ≥2 scenarios → a group (columns = scenarios), one in a single scenario → a "Not comparable" note (G36 auto-match-tools-and-workflows). Workflow groups reuse `_render_workflow_comparison`, which accepts `Sequence[dict | None]` participants so a scenario lacking the workflow is a placeholder column.

**Missing-participant alignment (locked 2026-05-24).** Columns align by participant (scenario / run). When a participant lacks the tool/step, its column renders a **top-aligned placeholder card with a "not present here" message** rather than collapsing — each logical grid row (header / inputs / outputs) emits exactly one child per column, `align-items: start`.

**Column-header context (2026-05-24).** Each column header (`render.render_job_header`) shows the job label, the **tool name + the version this job pinned** (`JobResponse.tool_version`, resolved from `Job.tool_version_id` in `GET /api/jobs/{id}` — can differ from the live released version), the **project / scenario names** (resolved in `_load_jobs` via `get_scenario` + `list_projects`), the friendly state, and the **completion datetime** (via `matofx.ui.format.format_user_datetime`).

- **Service** — `JobService.get_output_document(job_id)` returns the `OutputsDocument` wire shape (404 when not terminal; 200 with empty doc when terminal-but-malformed). Permission gate same as `GET /api/jobs/{id}`.
- **API** — `GET /api/jobs/{id}/output-document`; `GET /api/users/me/visible-jobs?mine=...`; `GET /api/users/me/visible-workflow-runs` (workflow picker); `GET /api/scenarios/{sid}/jobs` + `GET /api/projects/{pid}/scenarios` + `GET /api/scenarios/{sid}/workflow-runs` (scenario mode). `JobResponse` gained `tool_version` (optional; only the single-job `GET /api/jobs/{id}` resolves it — list endpoints skip the per-row version lookup).
- **Pickers** — tool: ownership scope → tool → tick jobs (complete only). Workflow: definition dropdown → tick runs (complete only). Scenario: project dropdown → tick scenarios; **ticks persist across project switches** so a cross-project set can be assembled (G36 cl.3).
- **Layout — full width (2026-05-24)** — the page container is full-width (no `max-w-screen-2xl` cap) and the inputs/outputs grid uses `grid-template-columns: repeat(n_cols, minmax(0, 1fr))` with `align-items: start`, so columns fill the whole panel and shrink-to-fit instead of forcing a horizontal scroll. `_COLUMN_WIDTH_PX` (440) now sizes only the overlay charts (which need a concrete first-paint width); the grid no longer uses it.
- **Reports (rebuilt 2026-05-24)** — `compare/report.py` builds a self-contained HTML doc with Plotly via CDN (`include_plotlyjs=False`), theme-aware; `auto_print=True` injects `window.print()`; "Download PDF" uses `ui.run_javascript` → `URL.createObjectURL` + `window.open`. **Never re-add kaleido** — see `feedback_no_kaleido_browser_print_pdf.md`. Each group lays out as a CSS grid (header / inputs / outputs / images rows) with `align-items: start` so cards are **top-aligned** like the screen; **image** outputs/inputs embed inline as base64 data URLs (built by the page from the already-fetched preview bytes, image types only); **HTML outputs** (e.g. a plotly heatmap a tool wrote to disk) render in a per-column "HTML outputs" section — a plotly `write_html` file has its figure extracted (`overlay._try_extract_plotly_figure`) and re-rendered against the report's CDN plotly.js, other HTML embeds via `<iframe srcdoc>`; the column header carries the same project / scenario / tool-version / completion context as the screen. `build_compare_report` = one group (Jobs mode); `build_multi_compare_report` = **one overall report** with a section per tool/step (Scenario / Workflow modes render this instead of per-group buttons — `_render_job_group(..., show_report_buttons=False)` returns its `ReportGroup` for the mode renderer to fold in). **HTML-only interactive content (2026-06-15):** `ReportGroup.field_outputs` (the `JobFieldOutput` list) renders a "Field animations" section — the *same* self-contained `_field_animation_figure`, themed via `apply_theme_to_plotly` + emitted with `_figure_fragment` (`pio.to_html(include_plotlyjs=False)`) so it plays off the CDN plotly.js; and `ReportJobEntry.videos` (`ReportVideo`, base64 `data:` URLs the page collects from the already-fetched preview blobs via `_collect_report_videos`) render a per-column "Videos" row of `<video controls>`. Both are gated by `interactive = not auto_print` threaded down `_build_document → _render_group_section → _render_group_grid`: present in the HTML download, **dropped from the print/PDF flavour** (neither plays on paper).
- **Per-element downloads** — every chart has a "Download as HTML" button via `export_plotly_html(fig)`; every file output row has a download icon via `render_job_outputs_section(..., download_callback=...)`.
- **Field animations inline in the Outputs card (2026-06-15).** A `file_type = "field"` output (time-resolved nodal field — e.g. the `transient_plate` tool's `.vtp` series) can't ride the blob pre-loader (`_is_previewable_output` excludes it) — the frames come from the parsed `field-frames` endpoint, not a raw download. `page._load_field_outputs` resolves which outputs are fields **from the descriptor** (the output document carries only `name`/`filename`), fetches each run's frames in parallel (`get_job_field_frames`, `return_exceptions=True` so one bad series doesn't blank the grid), and the page hands each job its frames via `JobCellModel.field_frames`. The cell renderer then draws the animation **inline in the Outputs card** (`render._render_field_row` → `_render_field_animation_inline`, full column width like every other output — *not* a separate squashed strip) with a **"Download animation (HTML)"** button (the same `export_plotly_html` artefact the Job page offers) **instead of the bare `.vtp` link**. The animation figure is the *same* builder the Job page uses — `_field_animation_figure` / `_field_frame_figure` / `_field_animation_filename`, moved into the shared `ui/_field_surrogate_viewer.py` (NiceGUI-free, plotly-only) so both surfaces import them — so a transient field looks identical whether you open one run or compare several. The figure carries its own play/pause + step slider, so no per-frame NiceGUI wiring. `field_overlay.py` now holds only the `JobFieldOutput` value object (shared by the page loader and the report). **Field animations + videos are embedded in the HTML report but not the PDF** (see the Reports bullet) — both play in a browser, neither on paper.
- **Select-tab selection persistence (2026-06-15).** The Select tab now re-ticks whatever the URL selection carries (`?mode=&jobs=/workflows=/scenarios=`), so landing from My Jobs **or** flipping back from the Compare tab keeps the picks. `_render_select_tab` passes `preselect_ids` (only for the mode matching `?mode=`) into each picker; the tool/workflow pickers default the dropdown to the **tool/definition that owns** the preselected ids and keep only the ticks that exist under it (`preselect_pending` consumes it once — changing scope or tool clears as before); the scenario picker seeds `ticked` directly (cross-project count stays correct). The picker list renderers paint checkboxes from `ticked` (`value=id in ticked`) and `_sync()` the Compare-N button on first paint. Picks clear only by navigating away (no query params) or choosing a different tool.
- **Deferred (G36 extras, cross-cutting for all three modes)** — 3D overlay (CAD/mesh via `ui.scene`); reference-run absolute/% output deltas; CSV table export; saved/shareable comparison sets; ECharts migration. `MAX_SELECTION` is still 6 (G36 cl.1 specifies 10 — revisit separately).

### 16.4 Workflows (shipped 2026-05-11)

Two workflow kinds share one table and one runtime path, discriminated by `Workflow.kind ∈ {admin, custom}` (`workflows/orm.py`). Schema lives in migrations `0021` (admin), `0022` (runs), `0023` (custom).

- **Admin-released workflows** (`workflows.manage` permission) — floating tool-version pinning (always invokes whichever version is currently in `released` state); same `new/test/released/archived` lifecycle as tools; per-edit snapshots into `workflow_version`; archiving a tool auto-archives every workflow containing it.
- **Custom workflows** (`custom_workflows.create` permission) — user-owned (`owner_id NOT NULL`), 2–10 tools, always `released` until archived; `visibility ∈ {private, shared}`; sharing via `workflow_share (workflow_id, user_id, shared_at, shared_by)` junction table; recipients get view + run only, no edit. Aliases auto-synthesised `custom_<owner_id>_<slug>` (collision-bumped). Owner-only edit / archive / unarchive.
- **Inspector** (`custom_workflows.inspect` permission, admin + super_user) — `/admin/custom-workflows` page: read-only listing of every custom row, archive-with-reason, make-a-copy into the inspector's own list. Never promoted into the admin's personal picker.
- **Ordering** — `workflows.ordering.compute_order` is a port of legacy `general_func.order_tools`. Builds the order from the **union** of forward `tool.downstream_tools` and backward `InputField.upstream_tool` + `downstream_input`. BFS-of-chains + topo tail; raises `CycleError` at save time.
- **Output→input wiring** — auto-discovered from each tool's descriptor (`InputField.downstream_input=True` + `upstream_tool="<alias>"`). Convention: input name = upstream output name (or an explicit `upstream_output`). **No manual wiring in the admin form.** Missing upstream → input field falls back to free user entry at run time.
- **File wiring (ADR-0015, `designs/workflow-file-wiring.md`, shipped 2026-06-20)** — the wiring flags are **type-agnostic**: a `type="file"` input may be wired to an upstream `type="file"` output (a mesh, a result field, a CSV), so a downstream solver consumes an upstream solver's *result file*, not just its scalars. The headless orchestrator `WorkflowRunService.advance` resolves each completed member's file outputs to their on-disk path under the retained working dir (`<mount>/<working_dir_relative>/outputs/<filename>`) via `_read_member_outputs` — the *same shape a previous-job pick produces* — so the pure wiring planner (`resolve_wired_values`) is unchanged. At queue time `queue_for_runner` copies the resolved upstream file(s) into the downstream working dir (`inputs/uploaded/<field>/`) via `tools.exec.uploads.stage_wired_file_inputs` (reuse of the upload-promotion copy; we **copy** not reference so the hand-off survives upstream cleanup and works across targets). A time-resolved field (`field_timesteps` glob) stages the whole series; the value is the primary snapshot. A genuinely-absent upstream file is omitted → the downstream job's own validation surfaces "required input missing". Standalone/manual submit is untouched (`wired_file_inputs=None`).
- **Runtime** — `WorkflowRun` + child `Job` rows (`Job.workflow_run_id` FK). Tabs gate on upstream `complete`; failure → downstream stays locked; cancel cascade applies SIGTERM to running children. `/my-jobs` nests child jobs under the parent run row.
- **UI** — `/my-workflows` (user create/edit/list with badges: My workflow / Shared by X / Shared / Archived); `/admin/workflows` (admin-released CRUD); `/admin/custom-workflows` (inspector). Custom workflows surface alongside admin in the `/my-jobs` workflow picker.

### 16.5 Job Search (shipped 2026-05-12)

`/job-search` is a port of legacy `functions/job_search.py` — *discovery by inputs*, distinct from `/my-jobs` which is *navigation*. The home-page "Search for Jobs" button now lands here.

- **Service** — `JobService.search_by_inputs(tool_alias, filters, numeric_tol, mine)` filters by equality + numeric ± tolerance on `Job.inputs`; ACL via `list_visible_to_user`; pinned to `state=complete`. `JobService.list_distinct_input_values(tool_alias, input_name)` powers file-input dropdowns.
- **API** — `POST /api/tools/{alias}/job-search` (the type-aware boundary splits filters into equality vs numeric using the descriptor); `GET /api/tools/{alias}/distinct-input-values`. Both return `VisibleJobResponse` via the shared `hydrate_visible_job_responses` helper.
- **"Use as starting point"** — `POST /api/scenarios/{sid}/draft-jobs` accepts an optional `inputs` field; idempotency preserved (existing draft wins, seeded inputs dropped). Per-tab in the UI so a user can View 10 jobs and pick any one.
- **Locked rules** — same-tool only (legacy parity, no cross-tool); strict equality for list/select/multiselect/file; numeric ± tolerance default 25%, user-editable; read-only "View inputs" tabs use a k/v list grouped by descriptor `[[input_groups]]` (not a re-rendered form).

#### 16.5b Find similar runs (shipped 2026-06-10)

A third **Similar** mode on the same `/job-search` page (alongside All / Filtered). Where the boolean search *filters* (a run is in-box or out), Similar *ranks* every visible completed run by closeness to a single **reference point** and returns the closest first — so the user always gets the N nearest even when nothing falls inside a tight tolerance band.

- **Ranking core** — `rank_jobs_by_similarity(...)` in `hierarchy/service.py` is pure and DB-free (reads only `Job.id` / `Job.inputs`, unit-tested in `tests/unit/test_similarity_ranking.py`). Numeric (`integer`/`float`) inputs are the distance axes; categorical / string / select / multiselect / file inputs present in the reference are **exact-match gates**. Each *varied* numeric factor is min-max-normalised into `[0,1]` (the reference value folded into the min/max), and distance is the square root of the **weighted** sum of squared normalised differences. A candidate missing a numeric factor is penalised the maximum normalised distance (`1.0`) on that axis — never a silent perfect match. `closeness_pct = 100·(1 − distance/√Σw)`, clamped `[0,100]`; pure-categorical matches (no numeric axis) read as 100%.
- **Weighting** — `ResultsService.latest_sensitivity_weights(tool_alias)` walks the tool's studies newest-first and returns `{factor: |SRC|}` from the first accessible, sufficiently-sampled study (standardised regression coefficients via `compute_sensitivity` over its first usable scalar response). Found → `weighting="sensitivity"` + a `weighting_source` label; none → `weighting="uniform"` (every active factor equal). Active factors a study didn't score fall back to the mean of the scored weights, so a swept-but-unstudied input still counts.
- **Service / API** — `JobService.find_similar_by_inputs(...)` reuses the same ACL-correct `state=complete` candidate set as `search_by_inputs`, then calls the pure ranker. `POST /api/tools/{alias}/job-search/similar` is the type-aware boundary: it resolves the reference (`reference_job_id` loads that run's inputs ACL-checked — 400 if it belongs to another tool; `reference_inputs` overlays on top), resolves weights, and returns `JobSearchSimilarResponse {weighting, weights, weighting_source, results:[SimilarJobResponse]}` where each row extends `VisibleJobResponse` with `distance` + `closeness_pct` (rows pre-sorted closest-first).
- **UI** — Similar mode offers a *Reference from* toggle: **Typed values** (the same per-input widget grid as Filtered) or **Existing run** (a completed-run picker). A Max-results spinner caps `top_n`. Results render through the shared row path with an extra **Closeness** column and a note saying how "similar" was weighted (`_weighting_note`). View Jobs / Compare these / Use as starting point are reused unchanged. The seed run is excluded from its own results.
- **Locked rules** — same-tool only (no cross-tool similarity); reference job must belong to the tool (400 otherwise); seed-from-typed requires ≥1 reference value; missing numeric ⇒ full penalty, never a free match; weighting decision + source are always surfaced (no silent "magic" ranking).

### 16.6 UI page surfaces (current state)

```
OUTPUTS/matofx/src/matofx/ui/pages/
├── login.py / home.py / dashboard.py / account.py / demo.py
├── my_jobs.py / job.py / submit.py            # unified workspace + deep-link aliases
├── job_search.py                              # discovery by inputs
├── compare/                                   # Compare Jobs (page + selection/diff/render/overlay/report)
├── my_workflows.py / workflow_run.py          # custom + admin run UI
├── project.py / scenario.py
├── admin.py / admin_tools.py / admin_tool_new.py / admin_tool_edit.py
├── admin_workflows.py / admin_custom_workflows.py
├── admin_users.py / admin_user_new.py / admin_user_edit.py
├── admin_teams.py / admin_paths.py / admin_tutorials.py / admin_explore_db.py
├── admin_general_settings.py / admin_send_notifications.py
├── admin_migrate.py                              # cross-install Tools/Workflows bundle export + import
├── _chrome.py / _descriptor_wizard.py / _stub.py
```

### 16.7 Alembic migration history (current state)

```
0001 baseline                       0014 seed_world_team
0002 audit_log                      0015 tool_versions
0003 users                          0016 user_email_notifications_opt_in
0004 hierarchy                      0017 one_tool_per_scenario        ← UNIQUE(scenario, tool)
0005 tools                          0018 user_theme_preference         ← dark/light
0006 custom_roles                   0019 user_locale_and_timezone
0007 audit_prior_hash_unique        0020 tutorials
0008 teams_and_user_extras          0021 workflows                     ← admin workflows
0009 user_logon_events              0022 workflow_runs                 ← runtime + Job.workflow_run_id
0010 hierarchy_hardening            0023 custom_workflows              ← kind/owner/visibility + share
0011 tool_archive_and_locking       0024 job_priority                  ← skip-queue flag + pickup index
0012 execution_backend              0025 tutorials_workflow_alias      ← workflow-scoped tutorials
0013 database_explore_permission    0026 runtime_setting_domain        ← (domain, key) composite PK
                                    0027 user_favourites               ← Quick Access per-user stars
                                    0028 migrate_trusted_peers         ← cross-install bundle import trust list
                                    0029 audit_read_permission         ← seeds audit.read onto administrator
                                    0030 notifications_send_permission ← seeds notifications.send onto administrator
```

The list above is a historical snapshot; migrations have since continued through `0049`. Recent heads of note: `0044` multi_role_capabilities (ADR-0005 — seeds the `library_curator` / `materials_curator` capability roles + `LIBRARY_CURATE` / `MATERIALS_CURATE`), `0045` file_library (four tables, §16.19), `0046` approval_directed_approver (directed approver + `library_file_version_id` as a third approval target, §16.19), `0047` live_jobs_permissions (§16.18), `0048` materials_database (six tables, §16.20), `0049` approval_material_target (`material_version_id` as a fourth approval target, §16.20).

### 16.8 Submission flow extensions (shipped 2026-05-13)

Eleven user-requested fixes batched together. Re-shapes the workspace submit form, the Compare overlays, the runner's pickup ordering, and a handful of admin / diagnostic surfaces.

**Submit form (workspace `/my-jobs`, draft branch)** — the right-pane form now exposes three actions plus the always-on Cancel:

```
[Cancel]   [Save Inputs]   [Skip Queue *]   [Submit]
                              * admins/su only
```

- **Save Inputs** — `POST /api/jobs/{id}/draft-update` → `JobService.update_draft`. DB-only: writes `inputs` (and optionally `name`) back to the draft row without staging the working directory. Re-opening the draft repopulates from `Job.inputs` via the existing `FormHandle.seed` path. Refuses non-`created` state with `JobNotQueueableError`.
- **Skip Queue** — `POST /api/jobs/{id}/queue` with `priority: true`. Gated by `Permission.JOBS_SKIP_QUEUE` server-side; UI button hides behind `is_admin()`. Sets `Job.priority=True`; runner's `_pickup_once` orders by `priority DESC, created_at ASC` (composite index `ix_job_pickup_order`). Resource / licence checks unchanged — priority only affects pickup *order*, not eligibility. Migration `0024_job_priority`.
- **Auto-named drafts** — `JobService.create` patches the placeholder ("Untitled run") into `Proj <project> Scen <scenario> <Tool Name> Job 0000123` after the flush (job id known). User-editable; only fires when the caller passed the placeholder, so explicit names survive.
- **Pre-release tools accept jobs from privileged users** — the submit/stage check delegates to the canonical `tools.service.can_user_see_tool` (state **and** team gate) rather than demanding `state == "released"`. Admins / super-users can submit tools in `new`; beta-testers can submit `test`; team-restricted tools refuse non-members. Same visibility ladder as the home-page tool browser. (Originally only the state gate via `user_can_see_state`; the team gate was added when the predicate was unified — see §16.36.)

**Job folder layout** — `JobService` now takes `environment` (threaded from `Settings.environment` via `get_job_service`). Staged working dirs become `<mount>/jobs/<env>/job_0000123/` (sanitised to `[a-z0-9_-]` for safety). Stops dev / staging / prod folders piling up next to each other on a shared mount; `environment=None` (tests) keeps the legacy flat layout.

**Compare overlays** — `JobCsvOutput` now carries `plot_title` / `xaxis_title` / `yaxis_title` / `xaxis_column` / `yaxis_column` from the descriptor; `_load_csv_outputs` threads them through. Merged + individual charts honour them with fallback to CSV column names so an axis is never unlabelled. New `JobHtmlOutput` + `render_html_overlay_section` adds an "Show HTML outputs" expander under the CSV section — extracts embedded plotly figure JSON via `_try_extract_plotly_figure` when present (so the theme applies) and falls back to `<iframe srcdoc>` for non-plotly HTML (legacy reports, hand-written summaries).

**Descriptor: csv_plot 0-indexed** — `OutputField.xaxis_column` / `yaxis_column` now `Field(ge=0)`. Wizard labels updated to "X-axis column (0-indexed)" with `min_value=0` and a helper line under the row. The renderer already treated indices as 0-based (`df.iloc[:, x_col]`); this fix aligns the docs and the wizard with what the runtime always did. Existing descriptors with `column=1` meaning "first column" need manual re-save to `column=0`.

**Diagnostic — launcher log capture** — `LocalTarget.submit` now redirects the launcher's own stdout/stderr to `<wd>/logs/launcher.log` instead of `DEVNULL`. Without this, a launcher startup failure (interpreter path wrong, import error in the auto-generated source) is silent: the per-tool `stdout.log` / `stderr.log` files never get opened because the launcher dies before line ~208 of the launcher source, and the operator sees an empty `logs/` folder. The launcher log surfaces in `JobLogsResponse.launcher` and renders as a default-collapsed "launcher (diagnostic)" expander on the job page when non-empty.

**Live job view — repainting status row + streaming logs (2026-08-31)** — `_render_inflight` used to draw its status row once, from the job dict captured at page build, and its 2 s tick only acted on a terminal state; the whole `queued → running` transition (badge, `started_at`, Cancel button) was therefore invisible until a manual refresh. The row now lives in its own container and is repainted when the state — or `cancel_requested_at` — actually changes, and only then. The same tick used to `clear()` the log container and re-render it, destroying the `ui.expansion` a user had opened and rebuilding it closed every 2 s. Logs now go through `_LiveLogs`, which builds each section at most once and only `push()`es into a `ui.log` (NiceGUI's log element appends without re-transmitting history, and follows the tail only while the viewer is at the bottom). `_TERMINAL_STATES` is now one frozenset shared with `my_jobs._wire_outer_refresh`, which had drifted — the tick treated `cancelled` as still in flight.

**`GET /jobs/{id}/logs` — tail mode (2026-08-31)** — the endpoint's "streaming and tail-mode deferred to Phase 2" is now done. `since_stdout` / `since_stderr` / `since_launcher` byte offsets return only what is new; the response always carries `*_offset` plus `truncated` (a log that shrank below the requested offset was rotated — the caller must clear rather than append). `tail=true` additionally ends each chunk on a newline, so a multi-byte character is never split across two polls and no line arrives in halves; it is **off by default** because the finished-job view must show a final line written without a trailing newline, which is exactly what a crash leaves. A pathologically long unterminated chunk (a progress bar redrawing with carriage returns) is released at `_MAX_UNTERMINATED_BYTES`. Before this, every poll re-read and re-sent the whole file, per viewer.

**Compliance-report reminder (2026-08-31)** — `compliance.schedule.reporting_status` is the single answer to "is a report due", read by both the chrome banner and Admin → Licence & compliance so the two cannot disagree. Cadence comes from the signed licence manifest (`LicenceManifest.compliance_report_months`, default 3, `0` = off) rather than site settings — a reporting interval is a term of the agreement, and an admin-editable one could be extended to silence the prompt it exists to raise. The clock runs from the last report *generated* (the only observable fact; whether it was sent is unknowable here), falling back to `install_identity.adopted_at`. Carried on `GET /api/site/mode` — already fetched once per page render by the chrome, already per-user — so the banner costs no extra round-trip; gated on `audit.read`, the same right the page is gated on. It is a banner only: no read-only mode, no refused writes. Clearing is derived from a DB fact per render, so there is no dismissal state and two admins can never see different answers.

**Admin → Tools — change log modal** — `_render_change_log_entry` lays each field-level diff out as a header + `old:` / `new:` indented rows instead of an inline `old → new` arrow. Structured values (lists / dicts) format as multi-line pretty-printed JSON in a `<pre>` block so a 12-team list or a nested descriptor patch stays scannable.

**UI — numeric input spinner suppression** — global CSS in `_install_palette_css` hides the browser-native number-input spin buttons (`-webkit-appearance: none`, `-moz-appearance: textfield`). Without this, a stray click on the up/down arrow silently decrements/increments by the descriptor's `precision` step (so 25 → 24.99 with `precision=0.01`). Users edit numeric fields by keyboard; the spin affordance was unwanted.

**API surface added 2026-05-13:**

| Method | Path | Purpose |
| ------ | ---- | ------- |
| POST | `/api/jobs/{id}/draft-update` | Save Inputs button — persist WIP form to draft row |
| POST | `/api/jobs/{id}/queue` (extended) | New `priority: bool` field; server enforces `jobs.skip_queue` permission |

**Permissions added 2026-05-13:**

- `jobs.skip_queue` — granted by default to `administrator` and to `super_user` via the catalog sentinel.

### 16.9 Rich output renderers — PDF / 3-D animation / video (shipped 2026-05-14)

Three new `file_type` values join the existing dispatch table (`html_plot`, `csv_plot`, `csv_table`, `image`, `cad_file`, `report`):

| `file_type` | Extensions | Inline viewer | Size cap |
| ----------- | ---------- | ------------- | -------- |
| `pdf` | `.pdf` | `<iframe>` over a `data:application/pdf` URL — browser's native PDF viewer. | 25 MB |
| `animation_3d` | `.gltf`, `.glb` | `ui.scene.gltf(...)` (three.js `GLTFLoader`, plays embedded `AnimationMixer` keyframes). Other animated formats (`.fbx`, `.dae`, `.usdz`) fall to `render_cad_placeholder` + download link. | 50 MB |
| `video` | `.mp4`, `.webm`, `.ogg`, `.ogv`, `.mov`, `.m4v` | HTML5 `<video controls>` over a `data:` URL keyed by extension MIME. Codec compatibility is the browser's call; unsupported codecs surface the native "no playable source" hint and the always-present download link is the escape hatch. | 50 MB |

**Where the dispatch lives:**

- `matofx.ui.pages.job._render_file_output` — adds three new arms before the unknown-`file_type` fallback. Per-type renderers live alongside `_render_image_file`: `_render_pdf_output`, `_render_animation_3d_output`, `_render_video_output`. All three follow the same shape — fetch via `client.download_job_output` (server-side bearer auth), check size cap, embed as base64 data URL, append a download link beneath.
- `matofx.ui.cad_preview` — gains `render_pdf_preview`, `render_gltf_preview`, `render_video_preview` as the shared sync helpers. The job page, the compare cell renderer, and the upload widget's preview pane all route through these so the iframe / scene / video markup stays in one place.
- `matofx.ui.pages.compare.render._dispatch_inline_preview` — recognises the three new `file_type`s (output-side) and the new extension sets (input-side). Inline previews appear under each row in the compare grid for any file the page pre-fetched.
- `matofx.ui.pages.compare.page._is_previewable_output` + `_RICH_PREVIEW_EXTENSIONS_FOR_PRELOAD` — extends both the output pre-fetcher and the input-side pre-fetcher to pull PDF / GLTF / video bytes through `/api/jobs/{id}/outputs/{filename}` and `/api/jobs/{id}/file-preview` respectively. No new endpoints needed: PDF / GLB / MP4 are served byte-for-byte, no server-side transform (unlike CAD STP→STL).
- `matofx.tools.forms.render._render_preview` — extends the upload widget's preview pane to dispatch the three new extension groups through a new `_render_bytes_preview` helper. Same in-memory-cache → fall-back-to-fetch path as the image preview.
- `matofx.ui.pages._descriptor_wizard._FILE_TYPE_HINTS` — three new entries so tool authors can pick them when declaring an output in the wizard.

**Auth-via-data-URL pattern (carried over from the HTML / image embedders):** browser-initiated iframe `src` and `<img src>` requests can't carry the bearer header, so we always fetch the bytes server-side (the page handler has the bearer) and embed them as base64 `data:` URLs. ~33% size penalty over raw bytes; the per-type cap above accounts for it.

**What's deliberately out of scope (v1):** server-side transcoding (`.mov`→`.mp4`, `.fbx`→`.glb`), in-scene playback controls for `animation_3d` (`AnimationMixer` plays on load, no scrubber), and PDF page navigation outside what the browser viewer offers. Each is a follow-up if the user surfaces a real need — for now the download link is always present so an unsupported sub-format never blocks the workflow.

**Demo group wiring (sidebar):** the four `/demo/*` pages (`demo.py`) showcase the same viewers without a job-submission round-trip. They scan a filesystem folder resolved by `matofx.ui.pages._demo_assets.resolve_demo_root` (default `OUTPUTS/demo_assets/`, override via `MATOFX_DEMO_ASSETS_DIR`) and present one picker + inline viewer per category. Subfolders: `cad/`, `images/`, `pdf/`, `animation_3d/`, `video/`. CAD non-STL formats (STP/STEP/IGES/OBJ) tessellate via `matofx.tools.cad_convert.convert_to_stl` directly (no API round-trip — the demo runs in-process). `/demo/web` is a sandboxed iframe with a URL field. Tests for the resolver live in `tests/ui/test_demo_assets.py`.

### 16.10 Tutorials — scoped upload manager (shipped 2026-05-14)

End-to-end wiring of the in-app tutorials surface. Admins upload PDFs and videos scoped to a single tool, a single workflow, or the general pool; the Home page surfaces what's currently active to every user.

**Module** — `matofx.tutorials/` (`orm.py`, `service.py`, `storage.py`, `errors.py`).

- **Scope** — `Tutorial.tool_alias` and `Tutorial.workflow_alias` are mutually exclusive FKs (RESTRICT). Both `NULL` ⇒ general. Enforced by DB CHECK `ck_tutorial_scope_exclusive` (migration 0025). Tool / workflow archives don't cascade-delete tutorials.
- **Media** — `Tutorial.media_type ∈ {document, video}` with a CHECK pin. Documents are PDF only; videos accept the same browser-playable container set as §16.9 (`.mp4`, `.webm`, `.mov`, …, see `tutorials.storage.VIDEO_EXTENSIONS`).
- **Activation rule** — exactly one active per `(scope, media_type)` for tool- and workflow-scoped rows; the **general pool is uncapped** so an install can ship "Getting started" + "Power-user tips" + a safety brief side-by-side. Cascade lives in `TutorialService.activate` (not a partial unique index — portable across SQLite + Postgres). Promoting a new doc to active demotes the previous one in the same scope; videos and documents cascade independently.
- **Storage** — bytes land on disk under `settings.tutorials.tutorials_dir`; the row stores original filename + on-disk path. Append-only: admins upload new versions rather than overwriting; deleted rows leave the file in place for now (a GC sweep is a follow-up).
- **Audit** — every mutation (`create_upload`, `activate`, `deactivate`, `delete`) writes to `matofx.audit` with the scope + media_type + version in the payload.

**API surface** — `GET /api/admin/tutorials`, `POST /api/admin/tutorials` (multipart upload), `POST /api/admin/tutorials/{id}/activate`, `POST /api/admin/tutorials/{id}/deactivate`, `DELETE /api/admin/tutorials/{id}`. Plus the read-side: `GET /api/tutorials/active` for the Home-page widgets and per-tool/per-workflow lookups.

**Admin page** — `/admin/tutorials` (`admin_tutorials.py`): grouped listing (general → per-tool → per-workflow) with an Upload dialog that asks for scope + media_type + version + title + description + file picker, plus an inline `is_active` toggle on each row and a delete button.

**Home-page integration** — the left rail lists every active general tutorial as a clickable link; each tool / workflow card in Quick Access exposes its active document + video alongside the existing Submit / Search buttons. Permission gate: regular users see what's active in their visible scope; admins see the same view (the inspector listing is the admin page).

**Permission** — new `Permission.TUTORIALS_MANAGE` granted by default to `administrator` and `super_user`. Migration 0025 extends the existing `tutorial` table from migration 0020 with `workflow_alias`, the matching index, and the scope-exclusivity CHECK — no data loss on upgrade.

### 16.11 General Settings — admin-tunable runtime knobs (shipped 2026-05-15)

Closed catalog of operator-editable settings spanning session policy, password rules, SMTP, job housekeeping, and branding. Admins edit each row individually under `/admin/general-settings`; defaults mirror the corresponding TOML baseline (`AuthSettings`, `EmailSettings`) so a fresh install with no admin edits behaves identically to one with no DB overrides.

**Catalog** — `matofx.config.general_settings.GENERAL_SETTINGS_CATALOG` is a dict of `SettingSpec` (frozen dataclass shared with the runner catalog from §16.7-era work). Currently 33 entries split into four sections (the 33rd is `favourites_enabled`, added 2026-05-15 — see §16.12):

| Section | Role gate | Keys |
| ------- | --------- | ---- |
| Session & Security | `super_user` | session idle / hard timeouts, password length + char-class flags + allowed-symbol set, lockout threshold + duration, registration mode + default role (last two reserved — not wired) |
| Notifications | `administrator` | `job_email_notifications_enabled` master switch, `smtp_host/port/username/password/use_starttls/from_address`, `public_url` for deep-link composition |
| Job housekeeping | `administrator` | retention windows for drafts, logs, completed work dirs, failed work dirs, audit log — every retention uses `zero_means_never=True` (sentinel: `0` = never delete; GC consumer short-circuits) |
| Branding & UX | `administrator` | `app_title`, uploaded `header_icon` + `favicon`, `support_email`, `default_timezone`, maintenance-mode banner toggle + message |

**Service** — `matofx.config.general_service`:

- `load_general_settings()` — permissive reader for code that just needs the effective value (catalog default merged with any DB override). Used by `matofx.users.settings_overlay` to expose `effective_session_policy()` / `effective_lockout_policy()` etc. Settings precedence: **DB row → TOML baseline → catalog default**; the overlay layer lets an admin's edits win without forcing the operator's `matofx.toml` to win when no admin has saved a row yet.
- `GeneralSettingsService` — strict writer. `set(key, value, *, actor)` enforces the per-row `requires_role` gate, validates against the spec (min/max/choices/MIME), writes to `runtime_setting` (`domain='general'`), and audits with secrets redacted in the payload.

**Runtime-setting domain split (migration 0026)** — `runtime_setting`'s PK widens from `(key)` to `(domain, key)` with a CHECK pinning `domain ∈ {'runner', 'general'}`. Existing runner rows are backfilled with `domain='runner'` by the migration's column default. Both loaders consult `domain` to filter rows. New catalogs can be added in future releases by extending the CHECK.

**API surface** — `/api/admin/general-settings`:

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/api/admin/general-settings` | List all rows with effective values; secrets render as `***REDACTED***` |
| POST | `/api/admin/general-settings/{key}` | Set a single value (validated against the spec; role-gated) |
| POST | `/api/admin/general-settings/{key}/reset` | Drop the DB override; value falls back to catalog default |
| PUT | `/api/admin/general-settings/{key}/image` | Multipart upload for `header_icon` / `favicon` (MIME-checked) |

No "save all" verb — one audit record per mutation, one error path per row.

**Admin page** — `/admin/general-settings` (`admin_general_settings.py`): four collapsible cards, one per section; per-row Save / Reset buttons; kind-specific renderers (number input with optional "never delete" pill for zero-means-never rows, bool switch, masked secret with Replace dialog, select dropdown, image upload). Numeric inputs honour the §16.8 spinner-suppression CSS.

**Secrets handling** — `SettingSpec.redacted=True` (currently only `smtp_password`) replaces the value with a `***REDACTED***` sentinel in every read path (router responses, audit payloads, listing UI). Clearing requires explicit empty-and-save; replacing requires entering the new value in full — the old value is never displayed.

**Branding cache** — `matofx.ui.branding_cache` holds a module-level `BrandingSnapshot` (app title, header icon path, favicon path, support contact, maintenance banner). Refreshed at startup and from a `_maybe_refresh_branding()` hook the router fires after every mutation to a branding key. The synchronous chrome (`_chrome.render_shell`) reads the snapshot directly so a page render doesn't have to hit the DB.

**Send Notifications shell** — `/admin/send-notifications` (`admin_send_notifications.py`) ships as a two-tab UI shell (Compose + Header/Footer template) wired into the sidebar but with the Send / Save Template actions deliberately stubbed: clicking either surfaces a clear "not wired yet" notice rather than failing silently. The compose form's SMTP relay is the same one configured in General Settings → Notifications; the `job_email_notifications_enabled` master switch does **not** gate broadcasts. Server-side delivery + persistence lands in a follow-up turn.

### 16.12 Quick Access favourites (shipped 2026-05-15)

Per-user "star" affordance on the Home page's Workflow Quick Access and Single Tool Quick Access sections. Starred items float to the top of their respective section (alphabetical within the favourited group, then alphabetical within the rest). The whole feature is gated by a single admin toggle so customers can roll it out or pull it back without code changes.

**Admin toggle** — `favourites_enabled` (`bool`, default `False`) in the General Settings catalog (§16.11), under the Branding & UX section. When off: stars don't render, ordering is the underlying API order, and any stored rows are preserved (re-enabling the feature restores every user's preferences).

**Storage** — `user_favourite` table (migration `0027_user_favourites.py`), one row per starred item:

| col | type | notes |
| --- | --- | --- |
| `user_id` | FK → `user.id` ON DELETE CASCADE | wipes preferences when an account is deleted |
| `kind` | `VARCHAR(16)` | CHECK `kind IN ('tool', 'workflow')`; widening means a migration |
| `alias` | `VARCHAR(128)` | no FK — archived items keep the row, just don't render |
| `created_at` | timestamp | bookkeeping only; sort is alphabetical |

PK is `(user_id, kind, alias)`, which makes `PUT /api/me/favourites/{kind}/{alias}` naturally idempotent (a re-favourite is a no-op upsert) without a separate UNIQUE index.

**Service** — `matofx.favourites.FavouritesService`: stateless dataclass with `snapshot` / `add` / `remove`. Mutations consult the live `favourites_enabled` flag and raise `FavouritesDisabledError` when off; reads always return a `FavouritesSnapshot(enabled, tools, workflows)` so the UI never has to error-path the admin toggle. Lists are alphabetical by alias.

**API surface** — three routes on `/api/me/favourites`:

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/api/me/favourites` | Caller's snapshot + feature flag (always succeeds) |
| PUT | `/api/me/favourites/{kind}/{alias}` | Idempotent add; 409 when disabled, 400 for unknown kind |
| DELETE | `/api/me/favourites/{kind}/{alias}` | Idempotent remove; same gating |

No `user_id` in the path — identity is the bearer token. No "save all" verb — every star click is one HTTP call that returns the new snapshot. No new permissions-table row — favouriting is a per-user UX action, not a feature gate.

**UI** — `home.py` fetches the snapshot once at page load and threads it into both Quick Access sections. Each card renders **two** star icons (`star` filled orange `#FB8C00`, `star_outline` grey) and toggles which one is visible — a one-line `set_visibility()` swap rather than a tear-down/rebuild on every click. Click handlers are optimistic: flip the icon + re-sort immediately, reconcile with the API in a background task, revert + `ui.notify` toast on failure. The card itself is never rebuilt, so search-box state and scroll position survive every toggle.

**Lost-permission handling** — if a user has starred a tool/workflow they later lose access to (state demotion, team revoke, archive), the API's tool/workflow list endpoints simply omit it; the favourite row stays in the DB but no card renders. When access returns the star reappears. There is no "stale favourite" cleanup job.

**Key files** — `matofx/favourites/` (subpackage: `orm.py`, `service.py`, `errors.py`), `api/routers/favourites.py`, `api/schemas.py` (`FavouritesSnapshot`), `ui/api_client.py` (3 methods), `ui/pages/home.py` (sort + star UX), migration `0027_user_favourites.py`.

### 16.13 Cross-install Migrate Tools/Workflows (shipped 2026-05-15)

Operator-facing surface to copy admin-released tools and admin-released workflows from one MATOFX install to another — typically Staging → Production. End-to-end decisions live in PROJECT_BRIEF.md §Q5 (2026-05-15 amendment); the operator walkthrough is `OUTPUTS/MIGRATE_GUIDE.md`. Three durable rules govern the implementation, captured as memory entries: lifecycle resets to `new` on import (`feedback_migrate_reset_to_new.md`), every FK reference travels by stable name not row id (`feedback_migrate_references_by_name.md`), and atomicity is per-item with cascading skips (`feedback_migrate_per_item_atomicity.md`).

**Transport** — signed gzipped tarball only. No peer-to-peer HTTP between installs, even on the same subnet, because the brief's "no outbound at runtime" property is a security feature. Operators move the bundle by `scp` / share / USB. The bundle is signed with the install's existing Ed25519 audit key (per §Q4 — "signed by the per-install Ed25519 key").

**Bundle layout:**

```
matofx-migrate-<utc-timestamp>-<short-fp>.tar.gz
  manifest.json          # canonical JSON, sorted-keys, no whitespace
  manifest.sig           # detached 64-byte Ed25519 signature over manifest.json
  tools/<alias>-<version>/descriptor.toml
  tools/<alias>-<version>/static/...
```

Workflow definitions are **inlined** in the manifest — removes the "tarball member without a manifest hash" attack surface and keeps the bundle small. Tool blob files are hashed individually with hashes carried *inside* the signed manifest, so tampering with any tool file fails the per-file SHA-256 check even though the file isn't signed directly.

**Module layout** — `matofx/migrate/`:

```
src/matofx/migrate/
├── __init__.py        # re-exports public surface
├── orm.py             # MigrateTrustedPeer (one trust row)
├── signing.py         # Ed25519 sign/verify, fingerprint canonicalise/display
├── bundle.py          # Pydantic manifest schema + pack/unpack pure functions
├── errors.py          # plain-English error catalog (UI quotes verbatim)
├── progress.py        # ProgressEvent + emit() callback contract
├── trust.py           # TrustedPeerService (audited CRUD)
└── service.py         # ExportService + ImportService (DB-aware glue)
```

Pure layers (`signing`, `bundle`, `progress`, `errors`) have zero side effects — no DB, no filesystem, no install-key lookup. The service layer wires those into the install's actual ORM rows.

**Storage** — `migrate_trusted_peer` (migration `0028`):

| col | type | notes |
| --- | --- | --- |
| `id` | PK BigInt | autoincrement |
| `fingerprint` | `VARCHAR(128)` UNIQUE | canonical lowercase hex SHA-256 of the source's public key (64 chars); CHECK length > 0 |
| `label` | `VARCHAR(128)` | human label, e.g. `"Staging"`; CHECK length > 0 |
| `added_by_user_id` | FK → `user.id` ON DELETE SET NULL | nullable; trust row outlives the admin who added it |
| `added_at` | timestamp UTC | server-default `current_timestamp` |
| `last_used_at` | timestamp UTC, nullable | stamped on every successful import; diagnostic only |
| `notes` | TEXT default `''` | optional admin note |

**Permission** — `Permission.MIGRATE_TOOLS_WORKFLOWS` (slug `"migrate.tools_workflows"`). Default-on for `super_user` via the catalog sentinel; **not** in `ADMINISTRATOR_DEFAULT_PERMISSIONS`. Super-users grant it to administrators case-by-case via the role-edit UI — deliberate gate, since cross-install operations cross a security boundary.

**Per-item atomicity** — `ImportService` opens a SAVEPOINT per item via `await session.begin_nested()`; commits on success, rolls back the savepoint on `ItemSkippedError` so the outer transaction (and subsequent items) survive. Dry-run mode runs the same code path but rolls back the savepoint regardless and skips the closing `migrate.bundle.imported` audit + `mark_used` writes. Disk writes for tool bundles happen *after* the savepoint commits — a rolled-back DB transaction never leaves orphan files.

**Reference resolution** — every FK from the source travels as a stable string identifier (`aos_host_name`, `hpc_cluster_name`, `python_env_name`, `python_env_storage_location_name`, tool alias, approver username); on import, the resolver looks up the destination's matching row by name. Unmatched tool dependency → tool skipped with plain-English reason naming the missing object and pointing to the admin page that creates it. Unmatched approver username → dropped from the descriptor's `approver_usernames` list with a warning event; if the resulting list is empty AND `approvals_policy != "none"` the tool is skipped (descriptor validator refuses).

**Idempotency** — `(alias, version)` already present with the **same descriptor hash** is a no-op (`outcome="no-op"`); a different descriptor hash is a hard skip (`VersionHashConflictError`) — operator bumps the version on source and re-exports. Workflow alias collisions are always a hard skip.

**API surface** (`api/routers/migrate.py`, mounted at `/api/admin/migrate`):

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/this-install-fingerprint` | This install's fingerprint (canonical + display) |
| GET | `/trusted-peers` | List trust entries |
| POST | `/trusted-peers` | Add (canonicalises fingerprint; 409 on dup) |
| DELETE | `/trusted-peers/{fingerprint}` | Remove (mandatory `reason` in body, audited) |
| POST | `/export` | Build a signed bundle; returns `application/gzip` download with `Content-Disposition: attachment; filename="..."` + `X-Bundle-Fingerprint` header |
| POST | `/import?dry_run=...` | Multipart bundle upload; returns `ImportEnvelope = {result, events}` |

Domain errors fall through to the global handler in `api/errors.py`: `BundleUntrustedError → 403`, `TrustPeerNotFoundError → 404`, `TrustPeerAlreadyTrustedError → 409`, `ToolNotFoundForExportError / WorkflowNotFoundForExportError → 404`, `BundleError / BundleSigningError → 400`. The install-key dep (`get_install_key_pair`) returns 503 with an actionable message when audit signing is unconfigured.

**Audit events** — exactly two per round-trip: `migrate.bundle.exported` on source (bundle hash + tool list + workflow list + notes), `migrate.bundle.imported` on dest (bundle hash + source fingerprint + source label + per-item outcomes). Per-item events are NOT separately audited — the bundle-import event carries the structured outcomes list. Trust mutations audit as `migrate.peer.trusted` / `migrate.peer.untrusted` with the canonical fingerprint and (for removal) the mandatory reason. `mark_used` is *not* audited — the bundle-import event already records the source fingerprint.

**CLI parity** — `matofx migrate {fingerprint|trust list/add/remove|export|import}` mirrors the UI surface. Every mutating verb takes `--user USERNAME` so the audit row has a real actor (CLI has no "logged-in user" concept). Progress events render with `OK / SKIP / WARN / ERR / ---` prefixes for grep-ability.

**UI** — `/admin/migrate` (`admin_migrate.py`). Single linear page (no tabs):

1. *Trusted peers* — collapsed expansion. This install's fingerprint at the top with a copy-to-clipboard button; below it, the destination's trust list with "Add trusted peer" and per-row "Remove" (mandatory reason in modal).
2. *Export* — checkboxes per admin-released tool and per admin-released workflow, optional notes, "Generate bundle" button. Browser downloads the tarball via a base64 data-URL (NiceGUI `ui.download` doesn't take server-memory bytes).
3. *Import* — `ui.upload` drop-zone (accepts `.tar.gz` / `application/gzip`), "Preview (dry-run)" and "Confirm import" buttons, live status log panel rendered with the same plain-English event strings the CLI emits. Each event level (`info / imported / skipped / warning / error / summary`) gets a colour cue from the theme palette. After the run, a "Next steps" footer surfaces concrete admin actions when skipped items need attention.

**Out of scope (v1 of this feature)**:
- Custom workflows. User-owned scratch; different user base on the dest install. Not exportable.
- Workflow-level approver lists. The v2 `Workflow` ORM doesn't have approvers — they're tool-level (in the descriptor). `WorkflowManifestEntry.approvers` is reserved in the schema for forward-compat; export emits empty, import is a no-op.
- Multi-version-per-alias bundles. The bundle module supports them but the import path collapses extras into one Tool row (latest by import order). Refine if a real customer needs it.
- SSE / live event streaming. The import path returns the complete event list synchronously in the response JSON; the page renders all events at once when the request finishes. Typical bundles import in well under a second.
- Feature-flag gate. `features.cross_install_migration` is documented in the brief but the feature-flag runtime isn't wired into the router yet. Permission + flag will compose when both land.

**Key files** — `matofx/migrate/` (whole subpackage), `api/routers/migrate.py`, `api/deps.py` (four new factories), `api/errors.py` (domain-error map entries), `cli/main.py` (migrate subparser tree), `ui/pages/admin_migrate.py`, `ui/api_client.py` (six new methods), `ui/pages/_chrome.py` (nav entry), `users/permissions.py` (`MIGRATE_TOOLS_WORKFLOWS`), migration `0028_migrate_trusted_peers.py`. Operator guide: `OUTPUTS/MIGRATE_GUIDE.md`.

### 16.14 Email & broadcast notifications (shipped 2026-05-15)

Outbound email moved from "job-state notifier only" to a shared transport plus an admin broadcast surface. Three related parts.

**(a) Config & shared SMTP transport.** `[email]` is now a documented TOML block (`etc/matofx.example.toml`): `enabled` (master kill-switch; `false` → `NoopNotifier`, the safe default), `smtp_host/port/username/password`, `use_tls` (implicit SMTPS:465) vs `use_starttls` (STARTTLS:587), `from_address`, `public_url`, `timeout_seconds`, and **`tls_verify`**. `tls_verify=false` is an opt-in, TOML-only escape hatch (deliberately *no* General-Settings catalog row, mirroring `use_tls`) for hosts whose outbound TLS is intercepted by an antivirus / proxy presenting a CA cert OpenSSL 3.x rejects ("Basic Constraints of CA cert not marked critical"). Secure by default; PROJECT_BRIEF §1173 mandates verification, this honours it (the insecure path is never the default and needs config-file access).

The SMTP mechanics live in **`matofx/notify/smtp.py`**, shared by the job notifier and the broadcaster so there's exactly one place that does transport:

- `resolve_effective_email_settings(settings, overrides)` — TOML baseline overlaid with admin DB overrides (the `smtp_*` / `public_url` / `use_starttls` General-Settings rows). Unset keys fall through to the TOML baseline, **not** the catalog default. `use_tls` / `tls_verify` are TOML-only.
- `assemble_message(...)` — builds the MIME tree: `text/plain`; `+html` → `multipart/alternative`; `+inline_images` → `multipart/related` (cid: refs); `+attachments` → `multipart/mixed`.
- `send_one(effective, msg)` — one-shot connect→send (job notifier path).
- `SmtpSession(effective)` — async CM that opens **one** authenticated connection and reuses it for many `send()` calls (broadcast fan-out — relays rate-limit logins). `tls_verify=false` injects a permissive `ssl` context for the STARTTLS/implicit-TLS layer only.

`EmailNotifier` was refactored onto these helpers — **no behaviour change** to job-state emails (verified by the existing `tests/notify/test_email.py` suite, monkeypatch target moved to `matofx.notify.smtp.aiosmtplib.send`).

**(b) Broadcast — Admin → Send Notifications.** Previously a UI stub; now wired end-to-end. New `Permission.NOTIFICATIONS_SEND` (`notifications.send`) gates the whole surface — seeded onto `administrator` by migration `0030`, `super_user` via the catalog sentinel. The reusable header/footer template persists through four `broadcast_*` rows added to `GENERAL_SETTINGS_CATALOG` (`broadcast_header_text` / `broadcast_footer_text` / `broadcast_header_image` / `broadcast_footer_image`) — these reuse `GeneralSettingsService` validation/persistence/audit + the `branding.icons_dir` image store, but are **deliberately excluded from `admin_general_settings.py`'s `SECTIONS`** so they never render on the General Settings *page*; they're managed only from this page. (`test_general_settings_router.py` asserts catalog length = 37 = 33 page-surfaced + 4 broadcast.)

`BroadcastService` (`matofx/notify/broadcast.py`): resolves the audience (`all` = every user, `active` = `user.is_active`; distinct, non-empty emails), wraps the admin's subject/body in the persisted template (HTML with inline `cid:` header/footer images + plain-text fallback + uploaded file attachments), fans out best-effort over one `SmtpSession`, and returns a `{disabled, attempted, sent, failed[]}` summary. Broadcasts **ignore** the per-user `email_notifications_enabled` opt-out (that flag is job-state-only, migration 0016) and the job-notification master switch — broadcasts are announcements every user is expected to receive. Audit: one `notify.broadcast.sent` (outcome `failure` if any recipient failed) or `notify.broadcast.skipped` when the subsystem is off.

API (`api/routers/notifications.py`, mounted `/api`):

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/admin/notifications/template` | header/footer text + image-set flags |
| POST | `/admin/notifications/template` | save header/footer text |
| PUT | `/admin/notifications/template/{slot}/image` | upload header/footer image (`slot` ∈ header/footer) |
| DELETE | `/admin/notifications/template/{slot}/image` | clear image |
| GET | `/admin/notifications/template/{slot}/image` | serve image (admin-only; compose-page preview) |
| POST | `/admin/notifications/broadcast` | multipart: audience (scope) + optional `group_kind` + subject + body + files (+ repeated `selection`) → fan-out summary |
| GET | `/admin/notifications/audience-options` | teams + (non-curator) roles + curator categories for the compose dropdowns |

UI (`ui/pages/admin_send_notifications.py`): Compose tab validates, shows a confirm dialog (broadcasts hit real inboxes), then a sent/failed summary; reports the email-disabled case explicitly instead of a misleading "sent 0". Template tab persists text via *Save text*; the single image per slot uploads/clears immediately with a live preview (image route is admin-only so the bytes are fetched through the authenticated client and rendered as a data URI, not an `<img src>`).

**Amendment 2026-06-25 — group audiences + template-tab UX + restyle.** The page was restyled to the home-shell idiom (lowercase left-aligned tabs over a transparent panel, `section_card` icon-chip cards) and three changes landed:

- **Group audiences (two selectors).** The audience is now a **base scope** (`audience` ∈ `active`/`all`) plus an **optional group restriction** (`group_kind` ∈ `""`/`teams`/`roles`/`curators` with a `selection`). The two are **intersected**: `active` + a team yields that team's currently-enabled members only (`BroadcastService._resolve_recipients` computes the scope email set, then keeps the group emails within it). Access-rights groups cover any role (base or capability) **except** the capability roles that grant a curate/approve permission (`PERMISSION_GROUPS` = Library/Materials/V&V/Models/Cohort curators + Optimisation approvers, in `notify/broadcast.py`); those are filtered out of the dropdown server-side (the endpoint queries `role_permission` for the group slugs) because they have their own **by-category** selector ("Specific curators and approvers"). This removes both the duplicate and the ugly `Vv Curator` snake-case rendering. The remaining roles are ordered by descending rights — the four built-in base roles (`super_user`/`administrator`/`beta_tester`/`regular_user`) first, then custom roles A→Z. The seeded `World` universal-visibility team is hidden from the teams list (it equals "Everyone"). Recipients are always resolved **server-side** in `_resolve_group_emails` (team join / role base+capability union / `user_has_permission` per curator perm) and deduped; the browser never supplies an address list. The chosen entities ride a repeated `selection` form field. A new `GET /admin/notifications/audience-options` (gated by `notifications.send`, so the page needs neither `teams.manage` nor `users.manage`) feeds the dropdowns.
- **Template tab.** Each of header/footer is now an image card **+** a boxed text card **+** one live preview that renders image **and** text together, updating as you type and clearing when the image is removed (the upload widget's own thumbnail is `reset()` after each upload so it can't linger as a confusing second copy). Two **Save** buttons (renamed from *Save text*), one under each block. The stale "the compose form doesn't repeat anything you put here" tail was dropped from the subheader.

**(c) Notification deep links + post-login redirect.** Job-state emails now link the canonical workspace URL `…/ui/my-jobs?job=<id>` directly (`/ui/jobs/{id}` is only a thin redirect alias that dropped the job seed on the pre-login hop). To make a deep link clicked **while logged out** survive sign-in, `require_logged_in()` (`ui/pages/_chrome.py`) now captures the requested page via `_post_login_target()` (reads `context.client.request`, strips the `/ui` mount prefix since `ui.navigate.to` is mount-relative, refuses to round-trip `/login`) and redirects to `/login?next=<url-encoded>`. `login.py` validates it through `_safe_next()` — same-site rooted paths only; protocol-relative (`//evil`), absolute URLs, and `/login` loops fall back to `/home`, so the param can't be an open redirect — and navigates there on success and for an already-logged-in visitor.

**Permissions added 2026-05-15:** `notifications.send` — default on `administrator`, `super_user` via sentinel (migration `0030`).

**Key files** — `matofx/notify/smtp.py` (new), `matofx/notify/broadcast.py` (new), `matofx/notify/email.py` (refactored onto smtp.py), `matofx/notify/__init__.py` (exports), `config/models.py` (`EmailSettings.tls_verify`), `config/general_settings.py` (4 `broadcast_*` rows + `IMAGE_MIME_TYPES_EMAIL`), `api/routers/notifications.py` (new), `api/deps.py` (`get_broadcast_service`), `api/app.py` (router mount), `api/schemas.py` (notification/broadcast models), `ui/pages/admin_send_notifications.py` (rewritten), `ui/api_client.py` (broadcast/template methods), `ui/pages/login.py` + `ui/pages/_chrome.py` (next-redirect), `users/permissions.py` (`NOTIFICATIONS_SEND`), migration `0030_notifications_send_permission.py`, `etc/matofx.example.toml` + `INSTALL.md` (`[email]` docs). Tests: `tests/notify/test_broadcast.py`, `tests/api/test_notifications_router.py`, `tests/ui/test_login_redirect.py`.

### 16.15 Job Approvals (shipped 2026-05-15)

Ports legacy `functions/job_approvals.py` + the `submit.py` approval gate, and extends it: the legacy approver saw only `Job ID / Tool / Requestor / Scenario / Project` and decided blind; v2 surfaces the full input list and resource/licence footprint so the decision is informed. Legacy had a single pre-run gate (`tools.approvals ∈ {Yes,No}`) and deleted the approval row on decision (no history). v2 keeps the existing richer descriptor model (`approvals_policy ∈ {none,pre,post,both}`, `approver_usernames`, `User.is_approver` — all already shipped) and adds the missing runtime: the gates, the persistent record, the batch approver page, the notifications.

**Policy semantics.** `pre` = approve before the run. `post` = approve before *results* are released. `both` = `pre` then `post`. `none` = unchanged. Policy is read from the **captured `tool_version` descriptor snapshot** pinned at draft create (`Job.tool_version_id`), never the live `tool` row — a policy edit can't change a job already in flight (same snapshot discipline `queue_for_runner` already uses for the bundle).

**Job states (4 new).** `JobState` / `JOB_STATES` (`hierarchy/orm.py`) extends to add `pending_approval`, `denied`, `pending_results`, `results_withheld`; the `ck_job_state` CHECK is widened in migration `0031`. Transition graph:

```
created ─submit─▶  pre|both  : pending_approval ─approve─▶ queued ─▶ running ─▶ …
                              └─deny──────────▶ denied  (terminal — never ran)
        ─submit─▶  none|post : queued ─▶ running ─▶ …

running ─ok──▶  post|both : pending_results ─release──▶ complete
                            └─withhold────────────────▶ results_withheld (terminal)
running ─ok──▶  none|pre  : complete
running ─fail─▶  failed   (failed runs skip post — nothing to release)
```

`both` job: `pending_approval → queued → running → pending_results → complete`. `denied` and `results_withheld` are terminal. My Jobs' right pane is state-shaped, so these four states each render a read-only notice (see "UI" below) — they were already reserved as Phase-2 placeholders in §16.1.

**Pre gate** lives *above* `queue_for_runner`, not inside it (keeps that method a pure stage+queue). New `JobService` branch on the submit/queue path: if snapshot policy ∈ {`pre`,`both`} → persist the inputs snapshot + name onto the row, set `state=pending_approval`, **no working-dir staging** (DB-only hold — consistent with the Save-Inputs / "stage at queue time" principle, §16.8), create the `approval_request` row, emit audit + notify. **Both** create+queue endpoints honour the gate — `POST /api/scenarios/{sid}/jobs` (one-shot) and `POST /api/jobs/{id}/queue` (workspace draft) — so the one-shot path is not a bypass. On approval the existing `queue_for_runner` runs unchanged; the approval-triggered call **skips the project-EDIT recheck** (the approver's authority is being a listed approver, not project access) and is audited `actor=user:<approver>, action=hierarchy.job.queue, payload.via=approval`.

**Post gate** intercepts the runner's success finalisation (`running → complete`): policy ∈ {`post`,`both`} → `pending_results` instead of `complete`, create the `approval_request` (kind=`post`), notify. Failed runs never enter post (nothing to release) → `failed` as normal. `release` → `complete`; `withhold` → `results_withheld` (files **retained on disk**, never purged).

**Output lockdown (the post-gate blast radius).** While a job is `pending_results` or `results_withheld`, outputs are visible only to **tool-eligible approvers + admins**; the requestor (and every other viewer) gets a "results pending approval" / "results withheld" notice. One `JobService` access helper centralises the rule; it gates **every** output surface: the five output endpoints (`/outputs`, `/output-document`, `/outputs/{f}`, `/outputs.zip`, `/all-files.zip`), the output CAD-preview endpoint, Compare's `get_output_document` path (withheld job renders as a "results withheld" cell, not an abort), and the My Jobs complete-pane. After `release` the job is `complete` and normal project ACL resumes; after `withhold` the approver/admin-only rule is permanent.

**`approval_request` table** (`matofx/approvals/orm.py`, migration `0031`). Columns: `id` PK · `job_id` FK→job · `kind ∈ {pre,post}` · `status ∈ {pending,approved,denied}` · `requested_at` · `requested_by_user_id` · `eligible_approvers` (JSON username list, **snapshotted at request time** so later approver-list edits don't rewrite history) · `decided_by_user_id?` · `decided_at?` · `decision_reason?` (NOT NULL-at-app-level when status=denied) · `decided_via ∈ {approver,self,admin_override}` · `tool_alias` · `tool_version_id` (denormalised so the inbox lists without joins and survives tool changes). **Never deleted** (legacy deleted on decision — we keep the row). A `both` job accrues two rows over its life. Partial unique index: ≤1 `pending` per `(job_id, kind)`. Audit events: `approval.request.created` / `approval.request.approved` / `approval.request.denied` via the existing `AuditWriter`.

**Authorisation.** A decision is allowed iff the caller is in the request's `eligible_approvers` snapshot **and** still `is_approver`, **or** is an admin (audited `via=admin_override`, rendered visibly distinct from a normal approver decision so oversight is auditable). **Self-approval is allowed but explicit**: a submitter who is an eligible approver still sees their own job in the inbox and must tick + Submit it (audited `via=self`); it is never auto-granted.

**`/approvals` page — the only place to act** (`ui/pages/approvals.py`; legacy parity with `dashboard_pages/.../job_approvals.py` but batch + enriched). A table of everything pending on the current user (pre + post mixed, kind-tagged — "Approve" vs "Release results"). Each row expands to the **rich review panel**: job identity (Job ID / Tool name+version / Requestor full name / submitted-at / Project › Scenario), resource + licence footprint, the **grouped input list** rendered from the captured descriptor (Label / Value / Unit, sectioned as the form showed it, changed-from-default marked ▲, file inputs with the existing preview / CAD-preview endpoints), and the optional requestor justification. Per row the approver picks **Approve / Deny / (undecided)**; one **Submit decisions** button processes the whole batch in a single pass. Undecided rows stay pending. A denied row reveals a **mandatory** per-job reason field; Submit is blocked until every denied row has one. Filter row above the table (per UI Conventions). Admins get a superset view of all pending requests.

**Home card + nav badge** (`ui/pages/home.py`, mirrors legacy `home.py:123-148`). A "Simulation Approvals" card renders **only when the user is an approver** (`is_approver` ∧ listed on ≥1 non-archived tool); shows the pending count + a button to `/approvals`, or "No pending approvals" when empty. The same eligibility+count helper feeds the chrome nav badge. The legacy `?approvals=True` session-state hack is dropped — v2 has real routing, so the email links straight to `/ui/approvals` (deep-link survives logged-out sign-in via the existing `_post_login_target` / `_safe_next` machinery, §16.14c).

**Notifications** (reuse `matofx/notify/`, no new transport). On pre-submit (or a workflow spawning several approval-needing jobs) → one email per submission event to every eligible approver, subject "Jobs awaiting your approval", body listing each as `Job 0001234 — Heat-transfer model — Jane Smith` + a direct `/ui/approvals` link. On post-hold → same shape to approvers ("Results awaiting release"). On a Submit-decisions batch → **one consolidated email per requestor** summarising that batch: approved jobs ("now queued" / "results released"), denied jobs with the reason verbatim. Honours the existing per-user `email_notifications_enabled` opt-out (job-state-class, migration 0016) and the email master switch — these are job-lifecycle notifications, not broadcasts.

**Optional justification.** `POST /api/jobs/{id}/queue` (and the one-shot create+queue) gain an optional `justification` free-text field, persisted on the `approval_request` and shown in the review panel. Additive; only meaningful when the tool needs approval.

**API surface (planned).**

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/api/users/me/approvals` | requests pending on the caller (pre+post), enriched for the inbox |
| GET | `/api/approvals/{id}` | one request + rich review payload (identity, footprint, grouped inputs, justification) |
| POST | `/api/approvals/decisions` | batch: `[{request_id, decision: approve\|deny, reason?}]` → per-item outcome |
| GET | `/api/users/me/approvals/count` | pending count for the home card + nav badge |

**Build phases** (pause for review after the design section and after each phase): **1 ✅** data layer — `JobState` extension + CHECK, migration `0031` (states + `approval_request`), `matofx/approvals/` (`orm.py`/`service.py`/`errors.py`), audit events; **2 ✅** pre gate — `JobService.queue_or_hold_for_approval` above `queue_for_runner`, `decide_approval` approval-triggered queue path (project-EDIT recheck bypassed, `via=approval`); **3 ✅** post gate + output lockdown — `supervisor._open_post_approval_if_required` runner intercept, `decide_approval` post release/withhold, `ResultsWithheldError` + `JobService.ensure_outputs_accessible` across all 6 output surfaces + Compare; **4 ✅** `/approvals` batch page + approver-only home card + cached-`is_approver` nav link + My Jobs read-only notices + the approvals API (`/api/users/me/approvals[/summary]`, `/api/approvals/{id}`, `/api/approvals/decisions` with per-item savepoint isolation); **5 ✅** notifications — `Notifier.notify_approval_request_opened` (eligible approvers, fired from the pre-hold API path + the supervisor's post-hold) and `notify_approval_decisions` (one consolidated email per requestor per batch), riding the existing `job_email_notifications_enabled` master switch + per-user opt-out; `EmailNotifier`/`NoopNotifier` both implement them so call sites need no None-check; the optional submit-time `justification` field (`JobQueueRequest`/`JobCreate` → `queue_or_hold_for_approval` → `ApprovalRequest`, shown in the review panel, surfaced in the submit form only for approval-policy tools); **6 ✅** docs/tests landed per phase.

Implementation notes (as built): the pre gate reads the policy via `ToolDescriptor.model_validate` (real installs always have a full descriptor); the **post** gate reads it as a plain mapping (run finalisation must be fail-safe on a minimal/legacy snapshot). `decide_approval` is one kind-aware method (not separate pre/post entry points). The chrome nav gates on a login-cached `is_approver` (state.py) since `render_shell` is sync; the live pending **count** is on the home card / summary endpoint, not the nav. Admin = `Permission.PROJECTS_SEE_ALL` (the see-all capability) for both the output-lockdown bypass and the approvals admin superset/override.

**Submit feedback + in-place refresh (fixed 2026-09-21).** `_submit` ended with `ui.navigate.reload()`. The queue *did* refresh, but the document reloaded on the same tick as the `ui.notify` calls immediately above it, so every message — the "N decision(s) applied" confirmation **and** each per-item failure from the batch endpoint's `ok: false` results — was destroyed before it rendered. `POST /api/approvals/decisions` returns **200 with per-item outcomes** (each item has its own savepoint), so a rejected decision is a successful HTTP call; the only signal the approver ever got was that toast. A batch where items failed therefore came back looking untouched, radios reset, with no explanation — reproduced in a browser: two rows in, one approved, list identical afterwards, zero messages.

The page now owns a `list_container` + `empty_slot` and a `_rebuild(fresh)` / `_refresh()` pair, so a decision re-fetches `GET /api/users/me/approvals` and redraws the rows **without tearing down the client**. Messages survive. Failures are `close_button=True, timeout=None` (sticky) because the row is about to be redrawn still pending, and are named by job — `0002977 · <job name>`, never a bare request id (`feedback_use_labels_not_names`). The intro line, filter row, bulk bar and Submit button hide when the refreshed queue is empty, so the "all caught up" card stands alone. Outcome wording is factored into the module-level pure helpers `request_display_name` + `summarise_decision_results` and covered by `tests/ui/test_approvals_outcome.py` (10 tests) — the page had no UI tests at all before.

**Workflow members were blind to the approval states (fixed 2026-09-21).** `workflow_run._tab_status_for` branched on only the five original lifecycle states, so the four §16.15 states fell through the `created` case and — upstream being ready — came back as `"ready"`. A member already submitted and parked with an approver re-rendered its **input form** and chipped itself *Ready*; a `denied` member, terminally refused, offered a form too. Standalone jobs were unaffected because the My Jobs right pane routes those states to the approval notice; only the workflow surface was blind. Four linked defects, all fixed and verified in a browser against a `pre`-policy member of a real 3-step run:

1. **Status classification.** `_tab_status_for` now returns each approval state as its own status, and the tab body routes them to the shared notice instead of the form. `_TAB_STATE_ICON` / `_TAB_STATE_COLOR` gain entries; the chip goes through the new `_tab_status_label` because `tab_status.title()` would have read "Pending_Approval". The tab-strip button gains a tooltip naming the status (the strip carries only an icon).
2. **No warning before the click.** The workflow form offered neither the approval heads-up nor the **Justification** box the standalone form has had since §16.15 — and `queue_draft_job` was called without `justification`, so a workflow gate reached its approver with no context at all. Both added; the post-submit toast now distinguishes "sent for approval" from "submitted" (on both surfaces). Those toasts are deliberately **not** sticky: the navigation that follows tears the client down and takes them with it, so the durable signals are the pre-submit heads-up and the destination notice.
3. **Polling parked on the gate.** `pending_approval` / `pending_results` were absent from `_ACTIVE_CHILD_STATES`, which lists states that move *without this user acting*. They belong there for the same reason `queued` does — an approver drives them rather than the runner — so the poll stopped the moment a member hit a gate and the tab sat on "Awaiting approval" until a manual refresh.
4. **Body signature collapsed the release.** `_active_body_phase` mapped `pending_approval` and `queued` both to `"inflight"`, so when an approver released the run the signature never moved: the strip's badge flipped to *queued* while the body below it went on saying "Awaiting approval". Each approval state is now its own phase (they render different copy anyway). Caught only by driving the real app — the first three fixes all passed while this one still failed.

The notice itself moved out of `my_jobs.py` into `ui/pages/_approval_notice.py` (`APPROVAL_NOTICES`, `APPROVAL_STATES`, `is_approval_state`, `render_approval_notice`) — its living in one page is how the other surface came to have no approval handling at all. `_render_waiting` additionally separates terminally-blocked upstreams (`denied` / `results_withheld` / `failed`, via `_UPSTREAM_DEAD_ENDS`) from genuinely-pending ones: "waiting for X to finish" is wrong about a job that will never finish. Covered by `tests/ui/test_workflow_run_approval_gating.py` (40 tests), including whole-vocabulary guards that every `JOB_STATES` member maps to exactly one tab status and one body phase, so a future state can't silently fall into the `created` branch again.

### 16.16 Conditional input dependencies (designed 2026-05-16)

Generalises the descriptor's two existing cross-field mechanisms (the fixed-shape `SumCheck`; `downstream_input`/`upstream_tool` value-sourcing) into a declarative **rule engine**: an input's visibility, options, required-ness, bounds, default, value, and enabled-state can all depend on the runtime values of *other inputs in the same descriptor*. Ports — and supersedes — the legacy "hide field X when Y is set" behaviour the forms README flagged as not-yet-ported. Design locked with the maintainer over 2026-05-16; this section is the durable record.

**Authoring surface.** A new top-level descriptor array, shaped like the §F30 `cross_field_constraints` it sits next to:

```jsonc
"dependencies": [
  { "when": "cooling_type == 'forced'",
    "effects": [ { "kind": "show", "fields": ["fan_speed", "duct_diameter"] } ] },
  { "when": "a == 1 and (b == 1 or c == 1 or d == 1)",
    "effects": [ { "kind": "set_value", "field": "e", "value": 2 },
                 { "kind": "disable",   "field": "e" } ] }
]
```

`when` is the **same fixed DSL** locked for §F30 cross-field constraints — *not a second language*. Operators: `+ - * / == != < <= > >= and or not in len`, parentheses, names resolving to the submitted value of a sibling input, literals. No imports, attribute access, builtins, calls (except `len`), comprehensions, or f-strings. The evaluator is the in-house `matofx.tools.forms.safe_expr` (`ast`-walker) — see the 2026-05-16 supersede note on PROJECT_BRIEF §F30 clarifier 1 for why it is owned, not `simpleeval`.

**Effect kinds (orthogonal, composable):** `show` / `hide`, `set_options`, `set_required`, `set_default`, `set_min_max`, `set_value`, `disable`. `set_value` and `disable` are deliberately separate (lock-at-current vs force-a-value vs suggest-but-editable are all expressible). Option-sets for `set_options` are **enumerated in the descriptor** — the DSL never does I/O, so the schema stays frozen and the evaluator stays pure.

**Condition reference scope (locked): sibling inputs only.** No upstream-tool output, preset, or user/material context in v1 — keeps the evaluator pure and deterministic. Widen later only if a tool needs it.

**Chaining, ordering, precedence.** Rules chain (A controls B; B controls C). The resolver evaluates **topologically against resolved values** — a condition reads the *post-rule* value of any field another rule controls (so `if a==1 then b=2` followed by `if a==1 and b==2 then c=3` fires as a chain). The ordering/cycle graph has an edge **only between a `set_value` write and a condition read** of that field: a condition reads input *values*, never a field's visibility / options / bounds / required-ness, so only `set_value` (the one effect that changes a value) can create a read-after-write dependency. This means `when a==1 → show a` (a rule keyed on the field it also reveals) is legitimate and *not* a cycle, while `when x==1 → set_value x` is a genuine value loop and is rejected. The **graph is cycle-checked at descriptor-load/install time** and a cyclic descriptor is *refused at install* (fits §F30 "bad descriptors never reach a running install"). Multiple rules writing one field resolve by **declaration order, last-wins** — no priority knob; rules with no `set_value` dependency between them carry no edge, so they apply in declaration order and the later one wins.

**Value-state matrix (the validation contract):**

| Field state | Written to `inputs.json`? | Required-checked? |
| --- | --- | --- |
| Hidden | Per author `on_hide`: `drop` (default) → omitted; `keep_default`; `keep_last` | No (when dropped) |
| `disable` + `set_value` | Forced value written; user input ignored | No (machine-set) |
| `disable` only | Current value written as-is | Yes |

`set_options` whose new set excludes the field's current value → the value is **cleared to unselected**; normal required-checking then applies.

**The frozen-schema reconciliation (§F30 Q5).** Conditional required-ness/options cannot live in the single static per-`(alias, version)` Pydantic model. So validation splits along the seam `_check_sum_checks` already occupies:

```
validate_inputs(descriptor, payload)
  1. static Pydantic model   → types / numeric-parse / table-shape / file-ext  (UNCONDITIONAL)
  2. resolver(descriptor, parsed_values) → EffectiveForm
        per field: visible · required · disabled · options · (min,max) · forced_value · on_hide
        topological, last-wins, evaluated against resolved values
  3. post-pass over EffectiveForm:
        apply on_hide policy · force set_value · clear stale set_options selections
        enforce effective required-ness + effective option membership + effective bounds
        sum-check drops hidden participants (skip the check if it empties)
  → resolved dict (the inputs.json shape)  |  FormValidationError (per-field, plain English)
```

The *rules* are frozen per version; the *resolved shape* is deterministically computed from rules + submitted values — §F30 Q5 preserved. `build_input_model`'s static model is narrowed to type/shape/file-ext only (required-ness and option-membership move into the post-pass; they were the only conditional-sensitive parts).

**One resolver, three surfaces.** A single pure `resolver.resolve(descriptor, values) -> EffectiveForm` is called from: (a) `render.py` — **live** NiceGUI reactivity (re-resolve on every widget change; show/hide, enable/disable, repopulate selects, surface a lightweight "set automatically because …" note rather than silently mutating, per the "users aren't modellers" rule); (b) submit — **authoritative** server-side recompute, the client is never trusted about what was visible; (c) the SDK launch safety-net (§F30's five validation points). Same function, no drift.

**Feature interactions (adopted defaults):** a fully-hidden input group is auto-satisfied for `require_all_groups_opened` (don't force opening an empty section); presets/seeders are ignored for `set_value`+`disable` fields (the rule wins) and follow `on_hide` for hidden fields; v1 rules target whole fields only — no per-table-column conditionals.

**Install-time lint (descriptor `@model_validator`s, mirroring `_sum_check_targets_exist`):** every field named in a `when`/effect exists and is a sibling input; the `when` string parses under `safe_expr` using only the allowed grammar; the rule graph is acyclic; `set_value`/`set_options` literals are type-valid for their target field. A descriptor failing any of these is refused at install, never at submit.

**Module layout (new):** `matofx/tools/forms/safe_expr.py` (sandboxed evaluator), `matofx/tools/forms/resolver.py` (rule engine → `EffectiveForm`); descriptor models extend `matofx/tools/descriptor.py`; integration lands in `matofx/tools/forms/model.py` (post-pass) and `matofx/tools/forms/render.py` (reactivity).

**Build phases (pause for review after the design section and after each phase):** **1** descriptor schema (`Dependency` + effect models, the five install-time validators) + `safe_expr` evaluator + `resolver` core, all unit-tested — *no behaviour change to existing forms yet*; **2** wire the resolver post-pass into `model.validate_inputs` (static model narrowed; value-state matrix; sum-check skips hidden participants); **3** live reactivity + the "set automatically" affordance in `render.py` — **UX revisit of the input form expected here** (maintainer flagged 2026-05-16); **4** docs/tests per phase, forms README + this section updated as-built. Phase 1 is the smallest reviewable slice and ships dark (engine present, nothing calls it) so review can happen before any user-visible change.

**Implementation notes (as built, Phase 1 + 2 landed 2026-05-16→17):**

- **`set_value`-only edges.** A condition reads input *values*, never a field's visibility / options / bounds. So the ordering/cycle graph (descriptor `_dependencies_acyclic`, resolver `_ordered_rules`) draws an edge **only** from a `set_value` write to a condition read of that field. `when a==1 → show a` is legitimate; `when x==1 → set_value x` is the only kind of loop, and it's rejected at install.
- **Dual-path validation (not a single narrowed model).** The §F30 strict model is kept **unchanged** for rule-free tools — narrowing it universally would have relocated required/option/bound enforcement out of Pydantic and broken the 87 `test_model.py` plain-English-message assertions. `build_input_model` builds the relaxed shape **only** when `descriptor.dependencies` is non-empty (still deterministic per `(alias, version)`, cache key unchanged). Rule-free tools are byte-identical to pre-§16.16; the full pre-existing suite stays green.
- **Forced beats hidden.** If a field is both `set_value`-forced and hidden, the forced value is authoritative and written to `inputs.json` (not dropped) — `set_value` is machine-intent, `on_hide` only governs the user-driven case.
- **Membership at submit is a hard error, not a silent clear.** The "stale option → cleared to unselected" behaviour is the *live renderer's* job (`_apply_select_options`); the submit post-pass instead emits a precise "must be one of …" `FieldError` so a tampered/raced payload fails loudly.
- **`FormHandle.refresh()`.** The live re-resolve is exposed as a method and is also invoked by `FormHandle.seed(...)`, so loading a preset / previous-job snapshot lands in its resolved state (rule-hidden/locked fields don't flash the raw seed). Rule-free tools: `refresh()` is a no-op; the only structural change is an inert per-field wrapper element + an empty hidden note.
- **Auto-note copy.** Plain-English only, never the DSL: *"Set automatically based on your other answers."* / *"Locked based on your other answers."* / *"Options updated based on your other answers."* (§F30 "users aren't modellers"). Layout/visual polish of the note + hidden-field transitions is the deferred UX-revisit, intentionally minimal here.
- **Test coverage added:** `safe_expr`, `resolver`, descriptor validators (Phase 1); `test_model_dependencies.py` (relaxed-model + post-pass: value-state matrix, effective required/options/bounds, hidden sum-check participant) and `test_render_reactivity.py` (container show/hide, disable, option repopulation + stale clear, forced value, seed-triggers-resolve) (Phase 2). Full forms+descriptor suite green; rule-free regression unaffected.
- **§F30 cross-field constraints wired (2026-05-17, follow-up).** The same `safe_expr` evaluator now also backs author-declared `ToolDescriptor.cross_field_constraints` (`{expression, error_message}`). Install-time lint (`_cross_field_constraints_well_formed`) mirrors the dependency `when` checks (grammar + sibling-only). At submit, `model._check_cross_field_constraints` evaluates each against the **resolved** values *after* the §16.16 post-pass — so a forced/hidden value is what the rule sees — sharing the sum-check's anchor+`sum_parameters` group-highlight mechanism and its "skip when a participant is unfilled or hidden-dropped" tolerance. The author's `error_message` is surfaced verbatim; the raw expression never is (§F30 clarifier 3). Closes the last deferred item in the forms README. Tests: `test_descriptor_constraints.py`, `test_model_constraints.py`.
- **Descriptor-wizard authoring surface (2026-05-17).** The tool-author wizard (`ui/pages/_descriptor_wizard.py`) now authors §16.16 dependencies — no hand-edited JSON. The per-input editor was restructured from a flat five-row layout into a **bordered, shadow-free card** whose body is grouped into labelled sub-cards (`_subcard`): *Identity* (name + label with placeholder copy spelling out the difference and the name rules — lowercase, no spaces — plus help text), *Type & value* (type + unit + the type-conditional editor, reactivity unchanged), *Layout* (group + sub-column), *Upstream* (auto-populate toggle + tool), *Dependencies*, *Required*. The **Dependencies** sub-card holds a repeatable per-input rule list — each rule a `when` expression (with a live sibling-name + operator hint) plus an effect dropdown (`DEPENDENCY_EFFECTS`) whose effect-specific inputs repaint reactively — and the existing cross-field sum-check editor moved in beside it. Round-trip: `_collect_dependencies` aggregates every input's rules into the descriptor's top-level `dependencies` (one `Dependency` per authored rule); `_distribute_dependencies` fans a loaded top-level list back onto the inputs each effect targets. A hand-authored multi-target/multi-effect rule round-trips as one `Dependency` per (rule, effect) — behaviourally identical under the resolver (topological + last-wins), not byte-identical; the wizard is the primary authoring path. Pure round-trip + a NiceGUI render smoke in `test_descriptor_wizard.py`.

- **Expression-driven effects + per-input validity rules, enforced everywhere (2026-06-18).** Generalised §16.16 so an input relationship like "B's minimum is always A" is authored in one place and obeyed in the form, at submit, **and** when studies / optimisation generate inputs. Three coupled changes:
  - **Descriptor model.** `SetMinMaxEffect` gains `minimum_expr` / `maximum_expr`; `SetValueEffect` / `SetDefaultEffect` gain `value_expr` (literal *xor* formula, validated); a new `RequireEffect{field, expr, message?}` is the "must be true, else block" outcome (added to the `Effect` union). Install-time lint (`_dependencies_well_formed`) now parses every effect formula and checks sibling-only refs; the cycle graph (`_dependencies_acyclic`) and the resolver's rule ordering (`_ordered_rules`) treat a formula's referenced names as *reads*, so a `set_value` that feeds a later formula still orders correctly and a genuine value loop is still refused. Helper `effect_expr_strings` is the shared source of an effect's formula strings.
  - **Resolver + submit + live.** `resolve` evaluates formula bounds/values against the running value map (guarded — an unfilled input just means the effect doesn't bite yet) and collects active `RequireEffect`s into `EffectiveForm.requires` (new `ActiveRequire`). `model.validate_inputs` enforces effective (now possibly formula-driven) bounds via the existing post-pass and adds `_check_requires`; `live_messages` surfaces the same require verdicts; `render._cross_field_participants` includes require participants so the live painter manages them.
  - **Generation paths.** New `forms.resolve_for_generation(descriptor, point)` applies the rules to a candidate (forces written in; effective bounds + requires + §F30 cross-field + sum-checks enforced) → studies' `generate` now calls it instead of bare `check_cross_field`. New `forms.selection_constraint_exprs(descriptor)` compiles every relationship (cross-field + require + set_min_max, each gated `(not (when)) or (…)`) into boolean DSL strings; the optimiser's `runtime._descriptor_cross_field` re-validates the pinned snapshot to a `ToolDescriptor` and injects these as `{"kind":"cross_field","expr":…}` for `constraints_ok` (evaluated against `{**fixed_inputs, **candidate}`). Show/hide/disable/set-required/set-options stay form-only by design (they shape the UI, not values).
  - **Wizard.** The separate "Validity Rules" tab (added earlier the same day) was removed; everything is authored in the per-input **Dependencies** card. Outcomes gain a fixed/formula source toggle for value & min/max, plus a `require` outcome with optional message; a blank **When** now stores the DSL literal `True` ("always") and round-trips back to blank. Top-level `cross_field_constraints` are no longer wizard-authored but carried through untouched on edit (no data loss).
  - Auto-generated messages render the expression with input **labels** (`_render_expr_with_labels`), e.g. *"Outer must be at least Inner."* / *"Not allowed: Outer > Inner must be true."*; an author message overrides. Docs: admin `30-adding-tools.md` rewritten ("Rules between inputs"). Tests: `test_dependency_expressions.py` (new) + extended `test_descriptor_wizard.py`; forms/descriptor/wizard/studies-generate suites green, mypy clean.
  - **Optimiser drop-count surfaced (same day).** The optimiser now records how many proposals it rejected at selection time (broke a rule) per batch as `n_dropped` on each `convergence_trace` point — no schema/migration (the trace is already a returned `list[dict]`). `runtime.run_study` computes `len(raw) - len(valid)` per batch, carrying forward all-infeasible batches' drops (and a fully-infeasible LHS seed's) into the next persisted batch; `_seed_candidates` returns its own drop count; `_persist_batch` gained an `n_dropped` param. The optimisation results page (`ui/pages/optimisation.py` `_render_setup_outcome`) sums the trace and shows *"Skipped: N candidate(s) that broke an input rule"* in the Outcome card (the convergence response already carries the trace, so no API change). Parametric studies already surfaced their equivalent skip count/reason. Tests: `test_optimisation_runtime.py::test_trace_counts_rule_dropped_candidates` / `::test_trace_no_drops_without_constraints`.
- **Out-of-range entries flagged, not clamped + one shared Submit gate (2026-06-15).** Two coupled form fixes. (1) **No silent clamp.** `render._build_widget` no longer passes `min`/`max` to `ui.number` — NiceGUI's `Number` registers a `blur → sanitize` handler that *clamps* an out-of-range entry to the bound with no explanation. The bounds are instead surfaced as a red message under the field via a new live pass and enforced authoritatively at submit (unchanged). `_apply_number_bounds` (which re-set `min`/`max` props for rule-driven tools, re-enabling the clamp) is removed; effective rule-tightened bounds are honoured by the live pass through `resolve`. (2) **Unified live validation.** `_wire_cross_field_validation` → `_wire_live_validation`, now painting per-field **numeric-bound + option-membership + cross-field** messages from a new `model.live_messages(descriptor, values)` (the live sibling of `validate_inputs`: same effective-bounds/cross-field code, but it *excludes* required-missing so a pristine field isn't nagged). Only fields where a message is possible are "managed" (`_managed_live_fields`: declared-bound numerics, all numerics on a rule-bearing tool, and cross-field participants); a bound-free/constraint-free tool gains no runner (regression-safe). The painter only *clears* labels it set (`painted` set), so a submit-time error on an unmanaged field survives a later live pass. (3) **Shared gate.** `FormHandle.submit_blockers(require_job_name, job_name)` is now the single gate **both** `my_jobs.py` (standalone, `require_job_name=True`) and `workflow_run.py` (workflow, `require_job_name=False`) consult — job name (optional), required-empty (skipping `locked_fields`), out-of-range/invalid (via `live_messages`, deduped, locked-excluded), and the `require_all_groups_opened` section-review gate. Previously the two pages hand-rolled divergent gates (the workflow path silently skipped the section-review gate); any future gate added to `submit_blockers` is now inherited by both. Tests: `test_render_bounds_and_gate.py`; existing `test_render_cross_field_live.py` contracts preserved.

### 16.17 Project access model rework — ADR-0002 (shipped 2026-05-17)

Supersedes Phase 1's team-keyed project ACL. **Per-user four-role
model**: an immutable `project.creator_user_id` (write-once;
provenance + Owner-equivalent authority floor) plus a `project_acl`
table (`(project_id, user_id)` PK, `role ∈ {owner,member,viewer}`).
Access resolution is a single user-id lookup —
`max(Creator⇒OWNER, projects.see_all⇒OWNER, ACL-role tier)`; **teams
are no longer ACL principals** (a share-dialog bulk-pick convenience
only). `project_team` is dropped.

`project` / `scenario` gain the §D20 no-delete `state`
(`active`/`archived`/`cancelled`) + `state_changed_at`/`_by` trio —
archive (Creator/Owner-only) is the only "remove"; nothing is ever
deleted. New admin-configurable typed metadata taxonomy
(`project_metadata_field` / `_value` / `project_metadata`:
single/multi-select + date + number; retire = `active=False`,
no-delete). Ownership transfer never touches the Creator: it grants
the successor `owner` and revokes all **other** non-Creator `owner`
grants; gated on Creator / explicit Owner / `projects.transfer_ownership`
(super_user only — `see_all` alone is intentionally insufficient).

Migrations `0033` (drop `project_team`,
`owner_user_id`→`creator_user_id`, state trio — split-batch on SQLite
so the rename and the constraints that reference the new column are
in separate `batch_alter_table` blocks), `0034` (`project_acl`),
`0035` (metadata trio), `0036` (seed the new
`projects.manage_metadata` permission onto `administrator`). Project
API gains `PATCH /projects/{id}`, `…/archive`, `…/transfer`, `…/acl`
(GET/POST/PUT/DELETE), `…/metadata` (GET/PUT); a metadata-taxonomy
router adds `GET /metadata/fields` (any user, for the form) and the
admin `GET/POST/PATCH /admin/metadata/fields[/{id}]` +
`…/fields/{id}/values` + `PATCH /admin/metadata/values/{id}`. UI: the
dashboard gains a filter row (search + show-archived) and per-row
metadata chips; the project page gains Edit / Share / Archive dialogs
and a "Created by" line; a new `Admin → Project metadata` page
(`projects.manage_metadata`-gated) administers the taxonomy. Decision:
`decisions/0002-…`; build spec: `designs/project-access-and-governance.md`.
A read-only `GET /api/users/directory` (any authenticated user;
`{user_id, username, full_name}`) backs the share dialog's people
picker and the project page's access list, both rendering
`Full name (username)`. The metadata form is one shared module
(`matofx.ui.metadata_form`) used by the dashboard *New Project*, both
Home Quick-Access *Launch in new project* flows, and the project
*Edit* dialog, so metadata renders/serialises identically everywhere.
The project page shows description + resolved metadata + the access
list grouped Creator / Owners / Members / Viewers, and its body is a
`ui.refreshable` so Edit/Share changes render in place (no browser
refresh / `ui.navigate` round-trip). A shared `matofx.ui.share_form`
people-picker also lets a project be shared **at creation time** from
the dashboard *New Project* and both Home Quick-Access *Launch in new
project* flows — not only via Edit afterwards.

**Deferred (v1.x):** per-scenario ACL tightening; creator-dormant
flag; clone; user-level legal hold.

---

### 16.18 Live Jobs — install-wide in-flight monitor + bulk cancel (shipped 2026-05-25)

The admin **Usage Statistics** page is renamed **Usage & Statistics** (nav label in `_chrome.py` + on-page heading in `admin_usage.py`; URL stays `/admin/usage`; the exported-report/email title stays "MATOFX — Usage Statistics") and gains a seventh tab, **Live Jobs**: a live, install-wide list of every in-flight job, workflow run, and parametric study, with multi-select bulk cancellation behind a confirmation modal. Unlike the six aggregate analytics tabs (gated by `usage.view`, no individual records), Live Jobs surfaces individual records and an operational action, so it sits behind its own permissions.

**Two new permissions** (`users/permissions.py`, closed catalog → auto-render under a "Live Jobs" section in the permission editor, which groups by slug prefix):

- `live_jobs.view` — see the tab + the list. Default on `administrator` (migration `0047`), `super_user` via the catalog sentinel.
- `live_jobs.cancel` — cancel install-wide, **bypassing project ACL**. `super_user` via the sentinel; **not** in the administrator default set — a super-user grants it per role in *Edit teams/groups & permissions* before an administrator can cancel from this surface. Distinct from `jobs.cancel_any` (single-job, elsewhere).

**What counts as in-flight / cancellable.** Shown: jobs in `queued` / `running` / `pending_approval` / `pending_results`; workflow runs with any in-flight child (cancel-requested runs excluded); studies in `running` / `pending_approval`. Cancellable mirrors what the existing cancel flows can actually act on: `queued` / `running` jobs (the runner's cancel loop only acts on those), every in-flight workflow run, and `running` / `pending_approval` studies. A standalone job paused at an approval gate is **shown but not cancellable here** (resolved on the Approvals page); the row carries a `cancellable` flag and `cancel_many` skips an ineligible id with a plain-English reason. Workflow runs / studies are **one row each** — cancelling cascades to in-flight children; child jobs never appear as separate rows.

**Service** (`matofx/live_jobs/service.py`, `LiveJobsService`): `list_inflight(...)` → `LiveJobsView{items, submitters}` (one query per kind, batch-resolves tool/workflow/study name + submitter + project + scenario; kind / submitter / submitted-window filters; submitter options computed before the submitter filter so the dropdown stays stable). `cancel_many(...)` → `CancelResult{outcomes}` reuses the existing per-item flows — `JobService.request_cancel`, `WorkflowRunService.cancel_run`, `StudyService.cancel` — each gained an additive `bypass_access: bool = False` param so the bulk path (already gated on `live_jobs.cancel` at the router) cancels regardless of the actor's project ACL. Per-item isolation (one failure never aborts the batch) + a `live_jobs.bulk_cancel` summary audit on top of each flow's own per-item audit. Same SIGTERM → SIGKILL / `taskkill` runner mechanics as a single cancel.

**API** (`api/routers/live_jobs.py`, mounted `/api`): `GET /admin/live-jobs` (gated `live_jobs.view`; returns rows + `can_cancel`) and `POST /admin/live-jobs/cancel` (gated `live_jobs.cancel`). Dep `get_live_jobs_service`. **UI**: `ui/pages/_live_jobs_tab.py::render_live_jobs_tab` (filters + Refresh + `ui.table(selection="multiple")` + confirm modal); `admin_usage.py` reads `live_jobs.view` / `live_jobs.cancel` from `GET /auth/me` to gate the tab + cancel controls (server enforces both regardless).

**Permissions added 2026-05-25:** `live_jobs.view` (default `administrator` + sentinel), `live_jobs.cancel` (sentinel only) — migration `0047`.

**Key files** — `matofx/live_jobs/` (new), `api/routers/live_jobs.py` (new), `api/deps.py` (`get_live_jobs_service`), `api/app.py` (mount), `api/schemas.py` (`LiveJob*`), `ui/pages/_live_jobs_tab.py` (new), `ui/pages/admin_usage.py` (rename + tab), `ui/pages/_chrome.py` (nav rename), `ui/api_client.py` (`live_jobs_list` / `live_jobs_cancel`), `users/permissions.py` (two slugs), `hierarchy/service.py` + `workflows/run_service.py` + `studies/service.py` (`bypass_access`), migration `0047_live_jobs_permissions.py`. Tests: `tests/live_jobs/test_service.py`, `tests/api/test_live_jobs_router.py`, `tests/users/test_roles.py` (defaults), `tests/ui/test_mount.py` (admin_usage import).

### 16.19 File library — governed reusable tool-input files, ADR-0006 (in progress, slices 1–7 + 8a shipped 2026-05-25)

Build-level spec: `designs/file-library.md`. A **library file** is a reusable, governed tool-input file (geometry / mesh / boundary-condition CSV, …) with immutable, content-hashed versions, organised on two **independent** axes: tiered **scope** (`personal` → `project` → `global`) and **tool assignment** (`tool_alias[, field_name]`, multi-tool). It adds a 4th "Pick from library" file-source mode to tool forms, reusing the existing upload-promotion path so the runner contract is unchanged.

**Data model (slice 1, migration `0045`)** — four tables in `matofx/library/orm.py`: `library_file` (logical entry; mutable metadata, optimistic-lock `version`, current `scope`/`state`; portable CHECK ties `project_id` ⇔ `scope='project'`), `library_file_version` (append-only immutable versions; UNIQUE `(library_file_id, ordinal)`; sha-256 `content_hash`; `publish_state` draft/pending/published/rejected), `library_file_tool` (the `(file, tool_alias, field_name)` assignment layer; `tool_alias` is a **soft string ref**, not an FK — resolved against the selected tool version at pick time), `job_library_file_use` (per-field provenance, composite PK `(job_id, field_name)`, FKs *to* `job`). Closed vocabularies (`LIBRARY_SCOPES` / `LIBRARY_FILE_STATES` / `VERSION_PUBLISH_STATES`) are CHECK-mirrored.

**Approvals extension (slice 4, migration `0046`)** — `approval_request` gains `designated_approver_user_id` (the one named approver) and `library_file_version_id` (a **third** approval target alongside `job_id` / `parametric_study_id`); the exactly-one-target CHECK widens to "one of three" and `tool_*` columns relax to nullable (a library gate has no tool). `approvals/orm.py` + `approvals/service.py` carry the directed-approver model (`create_request(designated_approver_user_id=…)` rejects self-approval). Shared with ADR-0007 — built once.

**Service (slice 2)** — `matofx/library/service.py` `LibraryService` (`@dataclass(frozen=True)`, `make_library_service(audit, *, environment=…)`), audited like the jobs/studies services. Methods: `create_file` / `add_version` (immutable versioning; bytes stored on the active `StorageLocation` mount), `update_metadata` (optimistic-locked), `assign_tool` / `unassign_tool` (last assignment protected), `set_scope` (governed transition), `decide_approval` (apply curator approve/reject), `retire` / `restore` (state-only, never hard delete), `get_file` / `list_files` / `list_versions` / `list_tool_assignments`, `list_for_picker` (the §3 query — latest published version of each visible, active, tool-assigned file; field-pin vs NULL-field extension filter), `open_version_bytes` (access-checked + audited), `record_job_use`. **Visibility** (`_visible`): owner always; curator sees any *non-personal* file (incl. pending-global awaiting review); else active-only by scope (global→all, project→ADR-0002 Viewer+, personal→none). **Mutate access**: personal→owner; project→EDIT; global→steward or curator. **Global-publish gate** (`_apply_landing_gate`, ADR-0006 §5): toggle ON → version `pending` + a directed `ApprovalRequest` (submit blocked without an approver; no self-approval); toggle OFF → curator publishes directly, non-curator `GlobalPublishNotPermittedError`. The install toggle (`library_global_publish_approval`) and chosen approver are resolved by the **caller** and passed in (same split as skip-queue) — General-Settings stays out of the service.

**On-disk bytes** — `matofx/library/storage.py`: write-once under `<mount>/library/<env>/file_<id>/v<ordinal>/<filename>`, sha-256 hashed, sanitised filenames + traversal-checked (mirrors `tools/exec/uploads.py`). `LibraryStorageError` is the byte-IO error family.

**General Setting (slice 3)** — `library_global_publish_approval` (bool, default **ON**, `requires_role='super_user'`) in the **Data governance** section of `config/general_settings.py`; resolved by the API layer (`config.general_service.load_general_settings`) and passed into the service, never read inside it.

**Capability** — `LIBRARY_CURATE` (`library.curate`) via the `library_curator` capability role, both seeded by ADR-0005's migration `0044`.

**API (slice 5)** — `Feature.FILE_LIBRARY` flag (router-wide `_require_library_feature` 404s the surface when off); `api/routers/library.py` with the §7 endpoints — `GET/POST /library/files`, `GET/PATCH /library/files/{id}`, `POST /library/files/{id}/versions`, `POST .../scope`, `POST .../tools`, `DELETE .../tools/{mapId}`, `POST .../retire`, `GET .../versions/{vid}/download`, `GET /library/picker?tool=&field=&version=`, `GET /library/approvers`. **Create + add-version are multipart** (a JSON `metadata` form field — `LibraryFileCreatePayload` / `LibraryVersionCreatePayload` — beside the `file` upload, mirroring the tool-install endpoint); the rest are JSON. Create/add-version/scope-change resolve the `library_global_publish_approval` toggle + validate the nominated approver is an active curator before passing both to the service. The picker resolves the *selected* tool version's `accepted_file_types` (`_field_accepted_extensions`, from `ToolVersion.descriptor_snapshot`) to filter NULL-field assignments. `get_library_service` dep, schemas in `api/schemas.py`, router mounted in `api/app.py`, and the `LibraryError` + `LibraryStorageError` (→ 500) families registered in `api/errors.py` (full status map; `ToolAssignmentRequiredError` → 400, shared by create-without-tool and remove-last-assignment). **Write-once ordering fix:** `create_file` / `add_version` now run a pre-flight `_require_landing_permitted` (approver-present / curator) **before** the byte store, because `storage.store_version_bytes` is write-once + non-transactional — a failure raised after it would leak orphaned bytes (and, on SQLite, a later file reusing the rolled-back surrogate id collides). Residual: a self-approval rejection still leaks bytes (the approvals service owns that check); harmless on Postgres, rare on SQLite.

**Form integration / submit freeze (slice 6)** — the 4th "Pick from library" file-source mode. A draft file field set to it carries `{"source": "library", "version_id": N}` in `Job.inputs[field]`. `JobService.queue_for_runner` calls `_resolve_library_inputs` (after `_resolve_material_inputs`, before staging): for each `file` field whose value is that reference, it fetches the chosen version's bytes (access-checked via `open_version_bytes`), stages them into the draft upload area via the existing `stage_draft_upload`, rewrites the field value to the staged path, and writes a `job_library_file_use` provenance row. The existing `promote_draft_uploads_into_working_dir` path then lands the bytes at `inputs/uploaded/<field>/<filename>` — **the runner contract is unchanged**. Non-library file values (typed paths, drag-drop, previous-job picks) pass through; a re-queue / retry sees an already-promoted path (not a reference) and is a no-op. The library import is lazy (acyclic `hierarchy → library`).

**Approvals router for tool-less gates (slice 8a)** — shared with materials: `api/routers/approvals.py` renders + dispatches the library gate (`target_kind="library"`, `_library_approval_detail`, eligibility = `LIBRARY_CURATE` for the designated curator, `LibraryService.decide_approval` on decision). See §16.20 slice 8a.

**UI (slice 7, most shipped)** — feature-gated nav entry ("File library", `_chrome.py`, gated by `ui/_features.file_library_enabled`). `ui/pages/library.py`: the **workspace** (`/library` — filter by scope / tool / include-retired, browse rows, "New library file" create dialog with upload + scope + one tool assignment + the approver picker on a global landing) and the **detail page** (`/library/{id}` — entry-metadata edit (optimistic), version history with per-version download + an add-version dialog, tool-assignment add/remove with the last-one guard surfaced, the governed scope-change dialog with project / approver pickers, and retire). UI gating reads the caller's resolved `library.curate` from `/me`. `ui/api_client.py` gained the library methods (multipart create / add-version, list / get / patch / scope / tools / retire / picker / approvers / download). The **approvals inbox** (`ui/pages/approvals.py`) now renders tool-less gates: `_TARGET_LABEL` badges, a `target_kind`-aware row header (no job-id / tool for library / material), and `_render_tool_less_detail` (name + requestor + note + the `input_groups` review rows). NiceGUI's upload event uses the current `event.file.read()` shape (the old `e.name`/`e.content` form silently AttributeErrors). No `/restore` UI control — the API §7 surface has no restore endpoint (parity with materials).

**Form-field "Pick from library" picker (slice 7b)** — the 4th file-source mode in the job-form rich file widget (`tools/forms/render.py`). A new `matofx/library/refs.py` (dependency-free) defines the **string sentinel** `matofx-library://<version_id>` (`build_library_ref` / `parse_library_ref` / `is_library_ref`); a string — not a nested dict — so the value survives the str-typed form pipeline unchanged. `FileInputContext` gained an optional `list_library` callback (wired in `my_jobs.py` to `ApiClient.library_picker`, **feature-gated** — `None` hides the mode); the widget shows a "Pick from library" button → a candidate dialog → records `build_library_ref(version_id)` as the field value + caches a display label for the preview chip. `tools/forms/model._file_extension_validator` skips the sentinel (its type was vetted at pick time); `JobService._resolve_library_inputs` now resolves **both** the sentinel string and the dict form via the shared `parse_library_ref` (replacing the old dict-only `_library_version_ref`). Submit then promotes the chosen version's bytes + writes `job_library_file_use` exactly as slice 6 already did. `validate_inputs` runs client-side only, so the sentinel reaches `queue_for_runner` and resolves before any server-side path use.

**Not yet built** — only the `ROADMAP.md` tick / README docs (slice 8).

**Key files** — `matofx/library/{orm,errors,storage,service,refs}.py`, `matofx/library/__init__.py`, migrations `0044`/`0045`/`0046`, `approvals/{orm,service}.py` (directed approver); slice 3 `config/general_settings.py`; slice 5 adds `features.py` (`FILE_LIBRARY`), `api/routers/library.py`, `api/{deps,schemas,app,errors}.py`; slice 6 touches `hierarchy/service.py` (`_resolve_library_inputs`); slice 7a adds `ui/pages/library.py` + touches `ui/{_features,api_client}.py`, `ui/app.py`, `ui/pages/{_chrome,approvals}.py`; slice 7b adds `library/refs.py` + touches `tools/forms/{render,model}.py`, `hierarchy/service.py`, `ui/pages/my_jobs.py`. Tests: `tests/library/test_orm.py` (slice 1 constraints), `tests/library/test_service.py` (slice 2; 17 cases — versioning, scope visibility, tool resolution, the publish gate, retire/restore, optimistic lock, byte reads), `tests/library/test_refs.py` (slice 7b; sentinel round-trip + parse), `tests/library/test_submit_freeze.py` (slice 6/7b; promote + provenance for both dict + string sentinel + passthrough), `tests/api/test_library_router.py` (slice 5; gating, lifecycle, global gate, optimistic lock, picker, download, approvers), `tests/api/test_library_approvals.py` (slice 8a; curator approves a global gate through the inbox), `tests/ui/test_library_client.py` (slice 7; the workspace/detail client methods end-to-end + feature gating), `tests/tools/forms/test_model.py` (slice 7b; the extension validator lets the sentinel through).

### 16.20 Materials database — versioned, curator-governed material data, ADR-0007 (in progress, slices 1-8 shipped 2026-05-25)

Build-level spec: `designs/materials-database.md`. A **material** is a reusable material card (fluid / metal / polymer / …) with immutable, ordinal-versioned **scalar** property sets, validated against a per-class expected-property schema. Scope is **global, curator-governed** — no personal/project tiering (a deliberate contrast with the file library). A later slice adds a `material` descriptor input type whose chosen version is resolved + **frozen** into `inputs.json` at submit so re-runs are reproducible.

**Data model (slice 1, migration `0048`)** — six tables in `matofx/materials/orm.py`: `material_class` (name UNIQUE; `state` active/retired) + `material_class_property` (expected `(prop_name, unit, required)`; UNIQUE `(class_id, prop_name)`) form the **schema**; `material` (logical card; `name` intentionally **not** unique — pin by id; optimistic-lock `version`; `state` active/pending/retired), `material_version` (append-only; UNIQUE `(material_id, ordinal)`; `publish_state` + optional `data_pedigree` CHECKs), `material_property` (per-version scalar `value` `Numeric`; UNIQUE `(material_version_id, prop_name)`), `job_material_use` (per-field provenance, composite PK `(job_id, field_name)`, FKs *to* `job`). Curves (temperature/condition-dependent) are phase-2; the model is scalar now.

**Service (slice 2)** — `matofx/materials/service.py` `MaterialsService` (`@dataclass(frozen=True)`, `make_materials_service(audit)`), audited. **Class schema:** `create_class`, `add_class_property`, `retire_class` + reads (`list_classes`/`get_class`/`class_schema`). **Material cards:** `create_material` / `add_version` (immutable ordinal versioning + per-version properties), `update_material_metadata` (optimistic-locked), `retire`/`restore` (state-only), reads (`get`/`list_materials`/`search` by name·class·tag/`list_versions`/`version_properties`), and `resolve_for_submit` (freezes a chosen version into the `inputs.json` block: `{material_id, name, class, version_ordinal, properties:{name:{value,unit}}}`). **Schema validation** (`_validate_properties`): every `required` class property must be present with a matching unit; extras allowed. **Authorisation:** reads open to any authenticated user (materials are global — no per-row visibility); create/edit/retire require `MATERIALS_CURATE` (`PermissionDeniedError` otherwise). Method named `list_materials`, not `list`, so it doesn't shadow the builtin inside the class. **Slice boundary:** curator **direct-publish** (new versions land `published`); the toggleable directed-approver review (`draft`/`pending`/`rejected` + `materials_change_approval` + a `material_version` approval target) is deferred to the General-Settings + approvals-extension slices — the `pending`/`rejected` publish states aren't produced yet.

**Descriptor input type (slice 3)** — `tools/descriptor.py` adds `"material"` to `InputFieldType` and two `InputField` fields: `material_class` (required class name — required for, and only valid on, a `material` input) and `required_properties` (optional; defaults to the class's `required` props). Descriptor-level validation (`_validate_material_constraints`) enforces the frozen-options rule: a `material` field declares a *requirement*, so it carries no `options` and no `default` (the chosen material is install-specific, picked at submit). **Install-time validation** lives in `ToolService._validate_material_fields` (called from `install` + `update_descriptor`): each `material` field's `material_class` must resolve to an **active** `material_class`, and any explicit `required_properties` must be declared by that class — else `UnknownMaterialClassError` / `UnknownMaterialPropertyError` (both → HTTP 400). The form layer already tolerates the new type via `forms/model.py`'s defensive `Any` fallback for unknown types; the actual **picker widget** + the **submit-time resolve/freeze** (calling `MaterialsService.resolve_for_submit` + writing `job_material_use`) are the UI (slice 8) and submit-wiring (slice 7) slices.

**General Setting (slice 4)** — `materials_change_approval` (bool, default **ON**, `requires_role='super_user'`) in the **Data governance** section of `config/general_settings.py`, surfaced on the GS page via `admin_general_settings.py`'s `SECTIONS` alongside `library_global_publish_approval`. Defined now; **not yet consumed** — like the library toggle, the consumer is the approval-gate path that lands with the approvals extension (slice 5). Catalog count test (`tests/api/test_general_settings_router.py`) bumped 38 → 39; vocab guard (`tests/config/test_general_settings.py`) extended.

**Approvals extension + gate (slice 5)** — migration `0049` adds `material_version_id` as a **4th** target on `approval_request` (the exactly-one-target CHECK widens 3→4; a partial unique index keeps one pending `(material_version, kind)` gate); `designated_approver_user_id` + nullable `tool_*` already existed from `0046`. `approvals/orm.py` + `approvals/service.py` (`create_request`) carry the new target. `MaterialsService.create_material` / `add_version` gain `approval_required` (default **ON**, the caller-resolved `materials_change_approval` toggle) + `designated_approver_user_id`/`_username`: with it on, the version lands `pending` behind a directed `ApprovalRequest` (`_apply_landing_gate`) and a brand-new material goes `pending` too; with it off the curator publishes directly. `decide_approval` applies a curator's approve (→ `published`, brand-new material → `active`) / reject (→ `rejected`, prior published version stays usable), reusing `ApprovalService.decide`'s self-approval/eligibility defence (the approvals import is lazy to avoid the `approvals.orm → materials` side-effect-import cycle). Since materials create/edit already require `MATERIALS_CURATE`, the gate's purpose is a *second* curator's review.

**API (slice 6)** — `Feature.MATERIALS_DATABASE` flag (router-wide `_require_materials_feature` 404s the surface when off); `api/routers/materials.py` with the §6 endpoints — `GET/POST /materials`, `GET/PATCH /materials/{id}`, `POST /materials/{id}/versions`, `POST /materials/{id}/retire`, `GET /materials/picker?class=&required_props=`, `GET /materials/approvers`, and the class-schema admin (`GET/POST /materials/classes`, `GET /materials/classes/{id}`, `POST /materials/classes/{id}/properties`, `.../retire`). Reads are open; create/edit/retire are curator-gated in the service. Create/add-version resolve the `materials_change_approval` toggle (`config.general_service.load_general_settings`) and validate the nominated approver is an active curator before passing both to the service. `get_materials_service` dep, schemas in `api/schemas.py`, router mounted in `api/app.py`, and the `MaterialsError` + `ApprovalError` umbrellas registered in `api/errors.py` (full status map). `MaterialsService.list_for_picker` + `MaterialPickerEntry` added for the picker.

**Submit freeze (slice 7)** — `JobService.queue_for_runner` calls `_resolve_material_inputs` before staging: each `material` descriptor field whose draft value is a selected `material_version_id` is expanded via `MaterialsService.resolve_for_submit` into a frozen `{material_id, name, class, version_*, properties}` block that replaces the field's value (so it flows through staging, upload-promotion, the on-disk `inputs.json` rewrite, *and* the persisted `Job.inputs`), and a `job_material_use` provenance row is written. The tool reads the frozen block — never the live catalog — so a re-run is reproducible; a re-queue / retry sees an already-frozen block, keeps it verbatim, and re-records provenance (`clear_job_uses` first, so the composite PK doesn't collide). The materials import is lazy (acyclic `hierarchy → materials`).

**Approvals router for tool-less gates (slice 8a)** — `api/routers/approvals.py` now renders + dispatches library / material gates, not just job / study. `ApprovalListItem` / `ApprovalDetail` gained `target_kind` (`job`/`study`/`library`/`material`) and made `tool_alias` / `tool_name` / `tool_version` optional; `_enrich` resolves the library-file / material name through the gated version; new `_library_approval_detail` / `_material_approval_detail` build the (tool-less) review panels. Eligibility went **kind-aware** (`_can_act_on`, async): a tool-less gate is visible/decidable by an admin or by the designated user who holds the matching curate capability (`LIBRARY_CURATE` / `MATERIALS_CURATE`) — *not* the tool-approver flag; the inbox `list` no longer gates on `is_approver`. `submit_decisions` routes the decision to `LibraryService` / `MaterialsService.decide_approval` with the correct per-kind `decider_is_approver`, and skips the job-centric requestor email for tool-less gates. `get_library_service` dep added. **This clears the pre-existing nullable-`tool_alias` mypy debt in `approvals.py`** (the residual `jobs.py` / `notifications.py` / `workflows.py` type debt is unrelated and predates this work).

**Capability** — `MATERIALS_CURATE` (`materials.curate`) via the `materials_curator` capability role, both seeded by migration `0044`.

**UI (slice 8b)** — feature-gated nav entry ("Materials", `_chrome.py`, gated by `ui/_features.materials_database_enabled`). `ui/pages/materials.py`: the **catalog** (`/materials` — filter by class / name / include-retired, browse rows, a "New material" create dialog with a **class-driven property grid** + approver picker, and a "Manage classes" dialog = the class + expected-property schema admin) and the **detail page** (`/materials/{id}` — card-metadata edit (optimistic), version history with each version's property table, an add-version dialog, retire). UI gating reads the caller's resolved `materials.curate` from `/me`. `ui/api_client.py` gained the materials methods (classes CRUD, materials list/create/get/patch/version/retire, picker, approvers). The **form picker** for the `material` input type (`tools/forms/render.py`): `MaterialInputContext` (a `list_materials` callback wired in `my_jobs.py` to `ApiClient.materials_picker`, **feature-gated**) threads through `render_form` → `_render_group` → `_render_field`; `_render_material_field` is a bound-holder picker (mirrors the file widget) recording the chosen `material_version_id` (as a string) — `_resolve_material_inputs` already freezes it at submit (accepts int / str / frozen-dict). The **approver inbox** rendering of `target_kind="material"` was done in the library's slice 7c (shared `ui/pages/approvals.py`).

**Descriptor wizard `material`-field editor (8b extension)** — `ui/pages/_descriptor_wizard.py` now lists `material` in the input-type picker (feature-gated via `_input_type_options(field)`: only when `materials_database_enabled()`, or when the field is already `material` so editing an existing tool never silently drops the type). A `_render_material_row2` editor on the input-card row 2 collects `material_class` + an optional CSV of `required_properties` (mirrors the file-types CSV pattern with `_sync_required_properties_csv` / `_flush_required_properties_csv`). `_input_to_descriptor` emits a clean `material` block (`material_class` + `required_properties`, `options=None`, `default=None` so the descriptor passes `_validate_material_constraints`); `_normalise_type_dependent_state` clears the material fields when the type switches away. Admins can now author a `material` input end-to-end through the UI — no hand-edited descriptor JSON.

**Example-materials seed (slice 9)** — `example_materials/seed.json` (two classes — `Fluid`, `Metal` — and four materials: `Water`, `Air`, `Steel S355`, `Aluminium 6061-T6` with reference values from NIST / EN / MMPDS / Engineering Toolbox), plus `matofx/materials/seed.py` `import_example_materials(session, *, actor_user_id, audit, source=None)`. Operator helper — **not** auto-run by Alembic (a fresh install starts empty, and migrations can't reference users that don't exist yet); run once after install via CLI / REPL / a one-off admin script so the catalog + the form picker have something useful out of the box. Idempotent (skips classes already by name + materials already in their class), bypasses the publish-approval gate (`approval_required=False`) since seeding is an operator action by design.

**Not yet built** — the `material_properties` example-tool re-port (the legacy tool ported to consume a `material` input + freeze its properties at submit; meaningfully larger because the current `material_properties` is what the test fixtures install). The summary/badge endpoint (`/users/me/approvals/summary`) still keys `can_approve` off the tool-approver flag — a curator-only badge refinement is a small follow-up.

**Key files** — `matofx/materials/{orm,errors,service}.py`, `matofx/materials/__init__.py`, migrations `0048` (tables) + `0049` (approval target); slice 3 also touches `tools/descriptor.py`, `tools/errors.py` (`UnknownMaterial{Class,Property}Error`), `tools/service.py` (`_validate_material_fields`), `tools/__init__.py`, `api/errors.py`; slice 4 touches `config/general_settings.py` + `ui/pages/admin_general_settings.py`; slice 5 also touches `approvals/{orm,service}.py` (the `material_version_id` target); slice 6 adds `features.py` (`MATERIALS_DATABASE`), `api/routers/materials.py`, `api/{deps,schemas,app,errors}.py`; slice 7 touches `hierarchy/service.py` (`_resolve_material_inputs`) + `materials/service.py` (`clear_job_uses`); slice 8a touches `api/routers/approvals.py` + `api/schemas.py` (`target_kind`, optional tool fields) + `api/deps.py` (`get_library_service`); slice 8b adds `ui/pages/materials.py` + touches `ui/{_features,api_client}.py`, `ui/app.py`, `ui/pages/_chrome.py`, and `tools/forms/{render,__init__}.py` + `ui/pages/my_jobs.py` (the `material` form picker); the wizard `material`-field editor touches `ui/pages/_descriptor_wizard.py`. Slice 9 adds `example_materials/seed.json` + `matofx/materials/seed.py`. Tests: `tests/materials/test_orm.py` (slice 1; 11 cases), `tests/materials/test_service.py` (slices 2+5; CRUD/versioning/resolve/curate-gate + the directed-approval gate), `tests/tools/test_descriptor.py` + `tests/tools/test_material_field_install.py` (slice 3), `tests/config/test_general_settings.py` + `tests/api/test_general_settings_router.py` (slice 4), `tests/api/test_materials_router.py` (slice 6), `tests/materials/test_submit_freeze.py` (slice 7/8b; freeze for int + string version id), `tests/api/test_materials_approvals.py` (slice 8a), `tests/ui/test_materials_client.py` (slice 8b; catalog/class/detail client methods end-to-end + feature gating), `tests/tools/forms/test_render.py` (slice 8b; the material picker renders + falls back), `tests/ui/test_descriptor_wizard.py` (8b wizard; material round-trip + descriptor validation + CSV flush + type-switch cleanup), `tests/materials/test_seed.py` (slice 9; shipped JSON seeds the expected classes + materials, idempotent on re-run, skips materials whose class isn't in the JSON). The `0048`/`0049` round-trips are also exercised by `tests/db/test_alembic.py`.

### 16.21 Base roles vs capabilities — UI + enforcement distinction, ADR-0005 (shipped 2026-05-25)

ADR-0005 split a user's privileges into one **base role** (`Role.kind='base'` — privilege rank + hierarchy/ACL semantics, assigned via `User.role_id`) plus stacked **capability roles** (`kind='capability'` — additive, permission-only grants via the `user_role` join). This change makes the distinction explicit and **strictly non-overlapping** across the admin surface, by convention: a permission is **capability-tier** iff its slug ends `.curate` (`matofx.users.permissions.is_capability_permission` / `CAPABILITY_PERMISSION_SUFFIX`), surfaced per-slug on the catalog API as `AdminPermissionInfo.is_capability`.

- **Enforcement** — `RoleService.create_role(kind=…)` + `set_permissions` reject a base role holding any `*.curate` slug and a capability holding any non-`*.curate` slug (`PermissionKindMismatchError` → 400). The current seeds already comply (`administrator` has no curate slugs; `library_curator`/`materials_curator` hold only theirs; `super_user` is sentinel-managed), so no backfill.
- **Group management** (`ui/pages/admin_teams.py`) — the New-group dialog has a **Base role / Capability** toggle that filters the tickable permissions by kind; the overview splits into **Base roles** / **Capabilities** cards (capability member counts come from the `user_role` join); the Permissions-tab picker groups the two kinds; the role editor renders **Base-role permissions** + **Capability permissions** cards and disables+unticks the card that doesn't match the edited group's kind (super_user fully read-only).
- **User add/edit** (`admin_user_new.py` / `admin_user_edit.py`) — base roles list in canonical order (Super User → Administrator → Beta Tester → Regular User → custom A→Z, via `_chrome.order_base_role_names`); capabilities live in their own card (`_chrome.render_capabilities_card`).

**Key files** — `users/permissions.py` (`is_capability_permission`), `users/errors.py` (`PermissionKindMismatchError`), `users/role_service.py` (`create_role(kind=…)` + kind-aware validation), `api/schemas.py` (`AdminRoleCreate.kind`, `AdminPermissionInfo.is_capability`), `api/routers/admin.py` (create passes kind; per-kind member counts), `ui/api_client.py`, `ui/pages/_chrome.py`, `ui/pages/admin_teams.py`, `ui/pages/admin_user_{new,edit}.py`. Tests: `tests/users/test_role_service.py` + `tests/api/test_admin.py` (kind on create, cross-kind rejection, `is_capability` catalog flag, kind listing).

#### 16.21.1 Teams/roles page redesign + permanence (reworked 2026-06-26)

`ui/pages/admin_teams.py` was reworked to the home-shell idiom and split the single "Access rights groups" card into **three** side-by-side, transparent, responsive sections: **Teams**, **Access Rights Groups - Base Roles**, **Access Rights Groups - Capabilities** (each titled + icon'd, each group an expandable bordered sub-card). The tab is renamed **"Teams & Access Rights Groups"** and left-aligned; panels are forced transparent (Compare-Jobs idiom). Behaviour changes:

- **Global instant search** — one search bar over all three sections; matches first/last name + username in-memory (team memberships are **preloaded** at page load) and collapses each section to just the groups holding a matched user, showing only the matched member(s), auto-expanding survivors. Empty box restores everything.
- **Sort preserves expansion** — the per-section *Sort members* select re-renders only each group's inner member box (closures keep all state per-request in a local `search_state`/`sections`), so the expansion wrapper — and its open/closed state — survives a re-sort. The old panel cleared+rebuilt every expansion, collapsing them.
- **Add-members for every group** — the multi-select "add many, Add once" control is now shown for **base roles too** (and always rendered, disabled-with-note when no candidates remain). Base-role add calls `set_user_role` per user, which reassigns their single base role (auto-removing the old one); team/capability use the existing bulk endpoints.
- **Base-role remove → Regular User** — base-role member rows get a Remove that calls `set_user_role(role="regular_user")` (a user must always hold one base role); `regular_user` itself has no Remove. Teams keep their member Remove; capabilities keep Edit-user only (no revoke endpoint).
- **Nothing is deletable** — `RoleService.delete_role` now **always refuses** (`RoleDeletionForbiddenError` → 409, audited `denied`); the UI delete affordance is gone. Teams already had no delete. Groups are permanent for audit even when empty (mirrors the Tutorials never-delete rule).
- **Permissions tab** — selector moved into a titled card; placeholder "Choose the group you want to edit"; the editor now renders **only** the permission card matching the picked group's kind (base *or* capability) instead of showing both with one disabled.
- **V&V label** — `_chrome.format_role_label` gained a `_ROLE_LABEL_OVERRIDES` map so `vv_curator` renders as "Verification & Validation (V&V) curator" everywhere (group panel, permissions picker, user add/edit); DB name unchanged. ADR-0005 references were dropped from the page text. New errors: `RoleDeletionForbiddenError` (`users/errors.py` + `api/errors.py` 409 map). Tests updated: `test_no_role_can_be_deleted`, `test_delete_custom_role_refused`.

#### 16.21.2 Create-vs-curate permission split (shipped 2026-06-26, migration 0083)

A uniform **create (base) / curate (capability)** split across the studies + governed surfaces, plus follow-on UI polish on the Teams page. Decisions: new base creates default to **super-user / administrator only** (Parametric Studies keeps its existing all-roles grant); a create action is allowed to a base `*.create` holder **or** the surface's `*.curate` capability holder (curators can always create), via the new `users.permissions.user_has_any_permission`.

- **New base-tier permissions** (`permissions.py`): `optimisation.create`, `models.create`, `cohorts.create`, `vv.create`, `library.create`, `materials.create` — each with a `PERMISSION_DESCRIPTIONS` entry and added to `ADMINISTRATOR_DEFAULT_PERMISSIONS` only.
- **`optimisation.approve` reclassified to capability-tier** — `is_capability_permission` now matches the suffix tuple `(".curate", ".approve")` (`CAPABILITY_PERMISSION_SUFFIXES`). Migration 0083 drops `optimisation.approve` from the `administrator` base role (admins who approve get the `optimisation_approver` capability).
- **API/service enforcement** — `optimisation.py` (`_require_create` on both create endpoints), `models.py` (create gated on `models.create` ∨ `models.curate`), `cohort/service.py` (`_require_create` = `cohorts.create` ∨ `cohorts.curate` on `create_cohort`; edit/archive stay `_require_curate`), `vv/service.py` (`_require_author` = `vv.create` ∨ `vv.curate` on record/update/submit; approve/promote/retire stay `_require_curate`; module docstring updated — the author-vs-curator split is now live), `library/service.py` (`_require_create` on `create_file`), `materials/service.py` (`_require_create` on `create_material` + `add_version`).
- **Nav gating** (`_chrome._NavItem.permission` now accepts a tuple = any-of) — Studies items + the three governed workspaces gate on create-or-curate: Parametric Studies=`parametric_studies.create`, Optimisation=`optimisation.create`, Surrogate Models=`("models.create","models.curate")`, Job Cohorts=`("cohorts.create","cohorts.curate")`, File Library=`("library.create","library.curate")`, Materials=`("materials.create","materials.curate")`, V&V records=`("vv.create","vv.curate")`. So a base role without the perm hides the feature.
- **Contributor-mode UI** — `vv_records.py` gained `is_author` (vv.create ∨ vv.curate): create buttons + the draft **Submit** action show for authors, while approve/reject/retire and the curator inbox stay curate-only. Materials/Library already always-showed their "New" button and gate governance on the curate flag, so they work as contributor surfaces once the nav lets authors in.
- **Renames + acronyms** — sidebar "File library" → "**File Library**"; `admin.py._permission_label` is now acronym/rename-aware (`vv`→"V&V", `hpc`→"HPC", `library`→"File library", `models`→"Surrogate models"); the permissions-tab section headers (`admin_teams._section_label`) spell `vv`→"Verification & Validation", `library`→"File Library", `models`→"Surrogate Models", acronyms HPC/AOS/KPI.
- **Teams-page polish** — left-aligned tabs now carry the brand teal active-colour + indicator and `no-caps` (Compare-Jobs idiom); the global search uses `on_value_change` so clearing restores all; base-role cards show per-tier icons (shield / manage_accounts / science / person, badge fallback); the permission-editor selector card title is "Choose a group to edit their permissions".
- **ADR scrub** — migration 0083 also strips `ADR-####` references from every `role.description` (runs after the seeding migrations, so fresh installs end clean too); the only seeded role description that carried one was `optimisation_approver` (0070).

#### 16.21.3 Visible-but-view-only surfaces + grouped permission editor (2026-06-26)

A follow-on refinement to the create/curate split:

- **Visible-but-view-only** — the seven feature surfaces (Parametric Studies, Optimisation, Surrogate Models, Job Cohorts, File Library, Materials, V&V records) are **feature-gated only** in the sidebar again (the `*.create`/`*.curate` nav `permission=` gates added in §16.21.2 were removed). A user without create rights still sees the item (if the customer has the feature on) but gets a **view-only catalogue** — each page hides its own create UI: `studies.py` / `optimisation.py` / `models.py` show the catalogue and `return` before the tab strip when the user lacks create (cohorts.py already did this — its gate moved from `cohorts.curate` to `cohorts.create ∨ cohorts.curate`); `materials.py` / `library.py` hide the "New" button (`can_create = *.create ∨ *.curate`, "Manage classes" stays curate-only); `vv_records.py` already gates create on `is_author`. The create APIs still enforce the permission server-side, so this is purely hiding buttons that would 403. `_NavItem.permission` (str | tuple any-of) is retained but currently unused.
- **Grouped base-role permission editor** — `admin_teams._render_base_perm_cards` renders the base-role permissions as one **non-collapsible card per sidebar group** (Dashboard / Studies / Libraries / Governance / Admin, in rail order) via `_PERM_SECTION_GROUPS`, each card reusing `_render_perm_card` (so checkbox harvesting + super-user read-only behave identically). `_render_perm_card` gained an optional `section_order` so each group lays its sections in a chosen order (Studies = `cohorts, parametric_studies, optimisation, models` so the balanced CSS columns read well). The Capability editor stays a single flat card. Library/Surrogate page headings now read "File Library" / "Surrogate Models".

### 16.22 Data lineage — provenance graph, ADR-0010 (shipped 2026-05-27)

ADR-0010 added a read-only **provenance graph** assembled in-memory from existing rows — no new tables. Every per-job FK + `job_library_file_use` + `job_material_use` + `approval_request` row that already exists in the DB becomes an edge in a directed acyclic graph the user can centre on any object they can already see. Compliance-critical for regulated industries (defence / aerospace / pharma / automotive) where "where did this output come from?" must be answerable without a SQL session.

Design: `designs/data-lineage.md`. Implementation in `matofx.lineage`.

- **Closed vocabularies** (`matofx.lineage.types`) — 14-member `NodeKind` enum (`JOB` / `WORKFLOW_RUN` / `PARAMETRIC_STUDY` / `OPTIMISATION_STUDY` / `RUN_COHORT` / `TOOL_VERSION` / `LIBRARY_FILE_VERSION` / `MATERIAL_VERSION` / `SURROGATE_MODEL_VERSION` / `SCENARIO` / `PROJECT` / `USER` / `APPROVAL_REQUEST` / `VV_RECORD`) + 13-member `EdgeKind` (one per relation). Reserved-for-later kinds (`OPTIMISATION_STUDY` / `RUN_COHORT` / `SURROGATE_MODEL_VERSION` / `VV_RECORD`) ship empty until their owning ADRs land — keeps the API contract stable. `LineageNode` / `LineageEdge` / `LineageGraph` / `LineageCentre` are frozen + slot-tight dataclasses.
- **`LineageService.build_graph`** — depth-bounded BFS in both directions from a centre node. Per-kind resolvers (`_resolve_job` / `_resolve_user` / …) delegate to existing domain services so ACL **intersects** with whatever the domain enforces (project ACL for jobs / scenarios / studies / workflows / projects; library scope for library file versions; broadcast-readable for material versions; `tools.see_*_state` for tool versions; the §4.3 predicate for approval requests). Hidden nodes drop silently from `nodes`; their incident edge carries a bumped `truncated_count` and the `hidden_by_acl` counter increments — never leaks existence. Soft-cap from the General Setting `lineage_max_nodes` (default 500, super-user-only): graphs that hit the cap drop to table view. `_assert_dag` is a defensive cycle guard — production never sees it fire.
- **`LineageService.list_centres`** — drives the admin lineage page's centre dropdown. Per-kind dispatch to each domain's listing API (`JobService.list_visible_to_user`, `WorkflowRunService.list_visible`, `ProjectService.list_for_user`, etc.) so the picker shows exactly what each user can already see in the owning surface.
- **HTTP API** (`api/routers/lineage.py`, prefix `/api`) —
  - `GET /lineage/{kind}/{obj_id}?upstream=&downstream=` → JSON `LineageGraphResponse`.
  - `GET /lineage/{kind}/{obj_id}/export?format=json|graphml&…` → attachment download (auth-required, so the UI fetches via the bearer-aware `ApiClient.lineage_export()` and pushes via `ui.download.content`, not via `ui.navigate.to(new_tab=True)` which can't carry headers).
  - `GET /lineage/picker/{kind}?limit=` → `[{id, label, state}]`. **Route order matters** — declared *before* `/lineage/{kind}/{obj_id}` so the path segment `"picker"` doesn't get matched as a `kind` and `{obj_id}` doesn't int-parse `"job"`.
  - All endpoints gate on `lineage.view` (seeded onto `administrator` by migration `0052`; `super_user` holds it via the catalog sentinel; broadly grantable per ADR §4 — most installs grant it to a compliance/audit role).
- **Serialisers** (`matofx.lineage.serialise`) —
  - `graph_to_json` → the canonical wire dict (stable `kind:id` ids; matches `LineageGraphResponse`).
  - `graph_to_graphml` → a self-contained XML doc with inline `<key>` declarations; loads cleanly into Cytoscape Desktop / Gephi / d3-force without an external schema.
- **Server-side SVG renderer** (`matofx.lineage.svg_render`, slice 6) — pure-Python layered-DAG layout (longest-path ranking + even within-row distribution; no crossing minimisation) + SVG writer. ~250 LOC, no external deps. The per-job HTML report (ADR-0004) gains a `?include_provenance=true` query toggle that calls `build_graph` with `surface="report"` and inlines the SVG. Returns `None` past `lineage_max_nodes` so the report can fall back to a tabular section.
- **UI** (`matofx.ui.lineage_view` + `ui.pages.admin_lineage`) —
  - **`LineageView` component** — shared shell with two view modes: **Graph** (Cytoscape.js 3.30 + dagre 0.8.5 + cytoscape-dagre 2.5, dynamically loaded via `document.createElement('script')` from a self-contained bootstrap, pushed via `ui.run_javascript` — works regardless of when the lineage view renders) and **Table** (per-kind sectioned `ui.table` with row-click navigation; the accessibility default and the soft-cap fallback). Filter row: upstream/downstream **depth** sliders (each with a hover `help_outline` `?` tooltip via `_label_with_help` explaining what it traces), view toggle, per-kind show/hide chips (chip-filter is client-side; edges that cross a hidden node render dashed). The depth sliders re-fetch on **commit** — `on_value_change` keeps the live value in `state` during drag, Quasar's `change` (pointer-release) fires the single re-fetch at the chosen depth (a fetch per drag-tick would storm the API; fixed 2026-06-15 — the sliders previously updated state but had no re-fetch wired, so depth changes did nothing until **Reload**). Error panel: `_render_error` shows status-aware plain-English copy from the pure `_friendly_lineage_error(exc, kind)` (404 → "Nothing to show here yet"; 401/403 → "You don't have access"; 5xx → "Something went wrong, try again"; with an icon + colour), no HTTP jargon in the user's face — the raw `HTTP {status}: {detail}` sits in the muted reference line's tooltip for support. **Dangling-edge guard (2026-06-15)** — `_graph_to_cytoscape_elements` drops any edge whose source/target node isn't in the emitted set (a node hidden by ACL/depth or a show/hide chip leaves its edge behind). Cytoscape *hard-throws* on a missing endpoint ("Can not create edge … with nonexistent target"), which previously killed the whole graph; the guard renders the connected part instead. The Cytoscape bootstrap's `.catch` (`_cytoscape._cytoscape_bootstrap_js`) was also rewritten from dumping the raw JS exception to friendly copy ("The diagram couldn't be drawn … switch to the **Table** view"), with the technical error sent to `console.error` for support. Footer: node/edge counts, hidden-by-ACL / hidden-by-depth / hidden-by-chip breakdown, JSON / GraphML download buttons.
  - **Standalone admin page** — `/admin/lineage` (centre picker only) + `/admin/lineage/{kind}/{obj_id}` (centred view). Sidebar entry under **Admin Space → Lineage** (icon `device_hub`). The picker has a populated `ui.select(with_input=True)` for IDs driven by `/api/lineage/picker/{kind}` — typing filters the dropdown.
  - **Inline tabs on host pages** (slice 5) — collapsible `render_provenance_section` expander on job, workflow run, library file detail, material detail; full tab on `/studies/{id}` (Progress / Results / Provenance); `↗ Provenance` link-out in each Compare column header; `↗ Activity provenance` link on `Admin → Users → User detail`. All centre-aware — the host page passes its own `(kind, id)`.
- **Audit** — every `build_graph` writes a `lineage.viewed` event with a `surface` discriminator (`tab` / `admin_page` / `json` / `graphml` / `report`). Payload carries node + edge counts but never raw graph data.
- **Cytoscape CDN strategy** — slice 4b loads cytoscape + dagre + cytoscape-dagre from `cdn.jsdelivr.net` (pinned versions; MIT/BSD). Air-gap-bundle vendoring per the design's §6.1 intent is a separate follow-up: drop the three files under `ui/static/vendor/` and switch the CDN URLs to relative paths.

**Key files** — `lineage/{types,service,errors,serialise,svg_render,__init__}.py`; `api/routers/lineage.py` + `api/schemas.py` (Lineage{Node,Edge,Graph,PickerEntry}Response); `api/deps.py` (`get_lineage_service`); `db/migrations/versions/0052_lineage_permission.py`; `users/permissions.py` (`LINEAGE_VIEW`); `config/general_settings.py` (`lineage_max_nodes`); `ui/lineage_view.py` + `ui/pages/admin_lineage.py` + `ui/api_client.py` (`lineage_graph` / `lineage_picker` / `lineage_export`); `ui/pages/_chrome.py` (nav entry); inline-tab call sites in `ui/pages/{job,workflow_run,studies,library,materials,compare/render,admin_user_edit}.py`; `reports/builder.py` + `reports/templates/report.html.j2` + `api/routers/reports.py` (slice 6 toggle). Tests: `tests/lineage/{test_service_job_resolver,test_service_resolvers,test_serialise,test_svg_render,test_permission_seed}.py` (61 sqlite); `tests/api/test_lineage_router.py` (8 router endpoints + error contract); `tests/ui/{test_lineage_client,test_lineage_view}.py` (3 API-client + 16 pure-helper + slider-commit & help-tooltip component tests + 3 friendly-error mapping tests + dangling-edge guard + friendly-graph-fallback bootstrap test).

### 16.23 Workflow structure graph, ADR-0013 (shipped 2026-05-27)

ADR-0013 added a read-only **workflow-structure graph** — the descriptor-inferred process flow for one workflow, rendered on the same Cytoscape canvas as the lineage view (`matofx.ui._cytoscape`). Where lineage walks recorded provenance ("what fed this row?"), the workflow graph projects declared structure ("what would feed what?"). **No new tables, no new permissions, no new migration** — the graph is a projection over `workflow` + `workflow_member` + `tool.descriptor`, with an optional `workflow_run` overlay for runtime state.

Design: `designs/workflow-graph.md`. Implementation in `matofx.workflows.graph_service` + `matofx.ui.workflow_graph_view`.

- **Frozen value types** (`matofx.workflows.graph_types`) — `WorkflowGraphNode` (tool_alias, label, position, tool_state, archived, deep_link, optional `run_state`, optional `child_job_id`, payload), `WorkflowGraphEdge` (source / target aliases, sorted `field_labels` tuple — the descriptor's human-facing input labels, falling back to wire names, rendered one-per-line above the arrow), `WorkflowGraph` (workflow header + nodes + edges + optional run rollup). `WorkflowNodeRunState` is a closed 6-member enum (`ready` / `waiting` / `queued` / `running` / `complete` / `failed`) mirroring the workflow-run page's tab vocabulary.
- **`WorkflowGraphService.build(workflow_id, actor_user_id, run_id=None)`** — composes the existing edge-inference helper (`workflows.ordering.infer_edges_with_fields`, extracted from the in-place `_infer_edges` so ordering + graph share a single source of truth). ACL via a per-row predicate that combines `WorkflowService.list_visible` (admin workflows: state + team + new-tool cascade) ∪ `list_for_user` (custom workflows: owner / shared / `CUSTOM_WORKFLOWS_INSPECT`). When `run_id` is supplied, the run's scenario must pass VIEW on its project, AND the run's `workflow_id` must match the build target — a hostile caller can't smuggle workflow A's run onto workflow B's graph to read foreign child-job states. Failure existence-hides as `WorkflowNotFoundError` → 404 / `NotFoundError` → 404, matching the rest of the workflow surface.
- **Per-node `run_state` resolver** mirrors `ui.pages.workflow_run._tab_status_for` — a child Job in `created` state becomes `ready` when every workflow-internal upstream is `complete`, `waiting` otherwise. Upstreams that aren't members of *this* workflow fall back to free-entry and don't gate readiness (matches `_all_upstream_ready`).
- **HTTP API** (`api/routers/workflows.py`) — two new endpoints: `GET /api/workflows/{workflow_id}/graph?run_id=` → `WorkflowGraphResponse` for saved workflows, and `POST /api/workflows/graph/preview` (body: `{tool_aliases: [...]}`) → `WorkflowGraphResponse` with `workflow_id=None` for *unsaved* tool selections — the create / edit dialogs need a preview path that doesn't require a saved workflow row. The preview endpoint validates each alias via the standard tool state+team gate (`workflows._custom_helpers.user_can_see_tool`) and rejects duplicates as `DuplicateMemberError` (matches the save-path semantics). No new permission, no new dependency. Audit categories: `workflows.graph.viewed` for the saved-workflow endpoint and `workflows.graph.preview` for the ephemeral one. No export endpoint in v1; defer JSON/GraphML download to v1.x if asked for.
- **Shared canvas** — the same `_cytoscape.render_cytoscape_canvas` shell ADR-0010 ships; workflow-specific stylesheet (`LR` rank direction; per-state node colour matching the run-page tab strip; archived nodes at 0.5 opacity). Standalone members (no descriptor wiring within the chosen set) render as floating nodes — dagre's natural layout drops them onto their own rank with no special-casing.
- **UI view** (`matofx.ui.workflow_graph_view`) — `render_workflow_graph(container, workflow_id, run_id=…)` is the primary entry; `render_workflow_graph_section(workflow_id, run_id=…)` is the collapsible-expansion wrapper for embedded saved-workflow surfaces; `render_workflow_graph_preview_button(get_tool_aliases=…)` is the on-demand "Preview structure" button that reads the editor's current tool picks at click time and renders the ephemeral graph against the unsaved selection. Run-mode auto-polls every 3 seconds until `_all_nodes_terminal` (every node `complete`/`failed`); structure-only / preview mode doesn't poll.
- **Four surface integrations** —
  - **`/workflow-runs/{id}`** — the page state grows a `view_kind` flag (`"tool"` default ↔ `"graph"`); the tab strip appends a final "Workflow" button (icon `account_tree`); clicking it swaps the body for the graph view with the run overlay. The signature gate is extended to include `view_kind`, so the page's poll-driven re-render doesn't wipe the user's in-progress form input on switch-back.
  - **Admin → New / edit Workflow** — `_open_workflow_dialog` mounts `render_workflow_graph_preview_button` below the tool list, reading `state["tool_aliases"]` at click time. Works on both create and edit — no workflow id required.
  - **Admin → Custom Workflows inspector** — `_open_details_drawer` mounts `render_workflow_graph_section` between the "Forked from" footer and the archived banner (the inspector always has a real workflow id, so the lazy-expansion section is the right tool here).
  - **My Workflows editor** — `_open_workflow_dialog` mounts the preview button between the tools picker and the share list, reading `tools_select.value` at click time. Same as the admin path: create and edit both supported.
- **No new tab, no new migration head** — the migration head after this slice ships is still `0052`. The workflow graph is purely additive on the read side.

**Key files** — `workflows/{graph_types,graph_service}.py`; `workflows/ordering.py` (`infer_edges_with_fields`); `workflows/__init__.py` (exports); `api/schemas.py` (`WorkflowGraph{Node,Edge,Response}`); `api/deps.py` (`get_workflow_graph_service`); `api/routers/workflows.py` (`GET /workflows/{id}/graph`); `ui/workflow_graph_view.py` (`render_workflow_graph` + `render_workflow_graph_section`); `ui/api_client.py` (`workflow_graph`); surface call sites in `ui/pages/{workflow_run,admin_workflows,admin_custom_workflows,my_workflows}.py`. Tests: `tests/workflows/test_graph_service.py` (7 sqlite — structure-only chain, per-field edge names, floating node, visibility-hide, run overlay happy path + waiting + cross-workflow mismatch); `tests/api/test_workflow_graph_api.py` (3 router smoke — auth, 404, happy path); existing `tests/workflows/test_ordering.py` continues to cover `_infer_edges` via the new wrapper.

### 16.24 V&V tracking — verification & validation records, ADR-0011 (shipped 2026-05-29)

ADR-0011 added a curator-governed V&V ledger: per-tool-version and per-job claims, evidence-backed, with directed-approver review, cohort-wide inheritance across byte-identical bundles, an operating-envelope soft-warn at submit, and badges threaded through every surface that displays a tool version or job result.

Design: `designs/vv-tracking.md`. Implementation in `matofx.vv` (service, ORM, errors, policy, bootstrap) + `matofx.api.routers.vv` (16 endpoints) + four UI surfaces.

- **Two record tables** — `tool_vv_record` (a claim about a `tool_version`; kinds `verification` / `validation`) and `job_vv_record` (a claim about a specific run; kinds `validated_against` / `calibrated_from` / `sanity_checked`). Shared lifecycle: `draft → pending → approved | rejected | retired`, with `pending → draft` as the author's recall path. Optimistic locking on every row; `kind` / `state` / `decided_when_terminal` CHECK-pinned; partial unique on `(record_id, kind)` for pending gates.
- **Cohort inheritance (Option C)** — `tool_version.bundle_hash` (migration `0053`, SHA-256 of the bundle directory, computed in `matofx.tools.versioning.compute_bundle_hash`; OS / mtime / permission invariant; empty-string singleton sentinel). `VVService.summarise_tool_status(tool_version_id)` reads every approved record whose target shares the same `(alias, bundle_hash)` cohort — one query against the composite `(alias, bundle_hash)` index plus the `(tool_version_id, state, kind)` index, returning a single `VVStatusBadge` with `kind`, `contributing_version_ids`, `cohort_bundle_hash`, `latest_envelope`, `inherited` flag. Job records do **not** inherit — a claim about one run is per-run.
- **Toggleable directed-approver gate** — submitting a draft for review creates an `approval_request` with `target_kind ∈ {vv_tool, vv_job}` (migration `0055` widened the target set; the existing approvals inbox shows V&V records alongside library / material / job / study gates). With the `vv_record_approval` toggle ON (the secure default; resolved in `api/routers/vv._resolve_approval_toggle` — currently a placeholder returning `True`, becomes a real General-Settings read when the catalog row lands), the author names one *other* curator; with it OFF, a curator approves directly. The sole-curator-and-toggle-ON guard raises `VVSoleCuratorError` with a plain-English message.
- **`vv_curator` capability role + `vv.curate` permission** (migration `0056`) — capability-style role that stacks on a base role without changing privilege rank, granted to `administrator` and `vv_curator`. Reads are open to any authenticated user; create / edit / submit / decide / retire all gate on `vv.curate`. Note: in v1 even *draft authorship* requires the permission — a deliberate deviation from §5 of the design (called out in `matofx/vv/service.py`'s module docstring) that frames V&V claims as evidence-backed compliance artefacts; easy to relax if a customer pushes back.
- **Per-project policy** — `vv_required_for_submit` metadata field (seeded at runtime via `matofx.vv.bootstrap`) blocks `JobService.queue_for_runner` when set to `yes` for a project AND the tool's current badge is anything other than `verified_only` / `verified_validated`. `matofx.vv.policy.check_submit_policy` enforces the gate server-side and audits `vv.policy.block` on rejection. The submit-form badge + envelope-warn banner are advisory; this policy is the blocking mechanism (design §6.4).
- **Operating-envelope soft-warn** — `VVService.resolve_envelope_warning(tool_version_id, form_values)` returns `None` when no approved validation exists, no envelope is declared, or every supplied numeric falls inside the cohort union; otherwise returns an `EnvelopeWarning` with per-factor `(submitted, envelope_min, envelope_max)`. The submit form polls `POST /api/vv/envelope-check` every 1.5s (deduping on a sorted-numeric-values signature), and renders an amber banner under the form when the warn fires. Soft-warn only — never blocks (policy gate is separate).
- **HTTP API** (`api/routers/vv.py`) — 16 endpoints behind a router-wide `features.vv_tracking` 404-when-off gate: status badge (`GET /api/vv/status/tool/{id}`), envelope check (`POST /api/vv/envelope-check`), per-target list / one-record (`GET /api/vv/{tool,job}-records[?…]`, `GET /api/vv/{tool,job}-records/{id}`), curator inbox + eligible-curator picker (`GET /api/vv/{pending,curators}`), author / patch / submit / approve / reject / retire on both record kinds (`POST` / `PATCH` paths). Approvals router (`api/routers/approvals.py`) widens `_can_act_on` / `_enrich` / `submit_decisions` / detail panels to cover the two new V&V `target_kind` values; one decision endpoint serves library / material / job / study / V&V identically. Schemas in `api/schemas.py` (+14 DTOs). `VVError` umbrella + status map in `api/errors.py`.
- **UI** (4 surfaces, all feature-gated on `vv_tracking_enabled()`) —
  - **Workspace** (`ui/pages/vv_records.py`) — `/vv-records` with three tabs (Tool records / Job records / Curator inbox, the last hidden for non-curators), tool-version / job filter, state + kind multi-filter, per-record detail pages at `/vv-records/{tool,job}/{record_id}` with create / submit / approve / reject / retire dialogs. Approver picker pulls from `GET /api/vv/curators`.
  - **Admin index** (`ui/pages/admin_vv_records.py`) — `/admin/vv-records` cross-install pending curator inbox with search + record-kind filter; jumps off to the per-record detail page.
  - **Submit form** (`ui/pages/my_jobs.py`) — V&V badge chip in the form header (click → `/vv-records`), with an "inherited" tooltip listing contributing cohort versions; amber envelope-warn banner under the form, repainted by a 1.5 s polling tick that compares numeric form values against the cohort union envelope. `tool_version_id` resolved server-side and surfaced on `ToolDetailResponse` so the UI doesn't need an extra round-trip.
  - **Per-tool admin tab** (`ui/pages/admin_tool_edit.py`) — fourth "V&V" tab on `/admin/tools/{alias}/edit`. Shows the inheritance-aware badge with cohort hash, a count, and the records on the current released version with per-record state chips and "Open" deep-links. `AdminToolDetailResponse.tool_version_id` resolved server-side.
  - **Per-job V&V card** (`ui/pages/job.py`) — collapsible "V&V" expansion on `_render_complete` and `_render_failed` (skipped on inflight — V&V is post-run); lists the job's records with a "Record V&V" deep-link for curators.
  - **Compare column-header badge** (`ui/pages/compare/render.py`) — compact `V&V` / `V` / `V&V pending` / `No V&V` chip under the tool-version line on every column. Batched in `_load_jobs` against the distinct `tool_version_id` set across selected columns — one `GET /api/vv/status/tool/{id}` per distinct version, never N+1.
  - **Nav** — Dashboard → "V&V records" (curator + feature gated) and Admin Space → "V&V records (inbox)" (admin + feature gated).
- **Lineage integration** (ADR-0010 §10 / slice 12) — `NodeKind.VV_RECORD` (existed as reserved; now wired) + new `EdgeKind.VERIFIED_BY` (with `EDGE_LABELS` entry "verified by"); existing `EdgeKind.VALIDATED_BY` keeps its label. `LineageService._resolve_vv_record` is polymorphic over the two tables — a tool record emits the node plus one upstream edge to its `tool_version` via `VERIFIED_BY` (verification kind) or `VALIDATED_BY` (validation kind); a job record emits an upstream edge to its `job` via `VALIDATED_BY`. Centre-list handler `_list_vv_records` returns the most-recent records across both tables (one `UNION ALL`). ACL is install-global on the V&V side — the BFS still intersects the target tool / job's project ACL during expansion. The reserved-kinds test (`tests/lineage/test_service_resolvers.py`) drops VV_RECORD from the loop.
- **Tests** — `tests/vv/` (5 files, 91 tests) + `tests/api/test_vv_router.py` (9 tests) + `tests/api/test_vv_approvals.py` (3 tests) = 118 V&V tests passing across sqlite (postgres-marked skipped, will run on CI).
- **Three soft edges to remember** —
  - `_safe_compute_bundle_hash` in `tools/service.py` silently falls back to `""` (singleton sentinel) on a missing bundle dir; existing test fixtures stay green but a botched production install loses inheritance silently. Worth a startup health check.
  - Router `_resolve_approval_toggle` is a `True` placeholder; becomes a real General-Settings read when `vv_record_approval` catalog row lands (no slice currently plans it).
  - V&V workspace lists records *only when a tool_version_id or job_id is supplied* — the per-tool tab on `admin_tool_edit.py` and per-job card on `job.py` are the natural entry points; an unscoped "all records" view was not in the design.

**Migrations shipped** — `0053` (`tool_version.bundle_hash` + composite `(alias, bundle_hash)` index), `0054` (V&V tables), `0055` (approval-target widening), `0056` (`vv_curator` role + `vv.curate` grants).

**Key files** — `matofx/vv/{__init__,orm,errors,service,bootstrap,policy,README}.py`; `tools/versioning.py` (`compute_bundle_hash`, `EMPTY_BUNDLE_HASH`); `tools/orm.py` (`ToolVersion.bundle_hash`); `users/permissions.py` (`VV_CURATE`); `features.py` (`VV_TRACKING`); `db/migrations/versions/0053..0056_*.py`; `api/routers/vv.py` + `api/routers/approvals.py` (V&V branches) + `api/schemas.py` (+14 DTOs) + `api/errors.py` (`VVError` umbrella) + `api/deps.py` (`get_vv_service`) + `api/app.py` (router registration); `hierarchy/service.py` (`queue_for_runner` policy hook); `lineage/types.py` (`EdgeKind.VERIFIED_BY` + label) + `lineage/service.py` (`_resolve_vv_record` + `_list_vv_records`); `ui/_features.py` (`vv_tracking_enabled`); `ui/pages/{vv_records,admin_vv_records}.py`; `ui/pages/{my_jobs,job,admin_tool_edit}.py` (V&V surfaces); `ui/pages/compare/{render,page}.py` (column badge + batched fetch); `ui/pages/_chrome.py` (nav entries); `ui/api_client.py` (17 `vv_*` methods).

#### 16.24.1 Range editor, version migration, retire-from-any-state (2026-07-23)

Three additions to the V&V surface, no migration (the ORM already carried `applicable_range_json`; the approvals status vocabulary was reused, not widened):

- **Applicable-range editor in the New record dialog** — the authoring dialog (`ui/pages/vv_records.py:open_vv_create_dialog`) now has an **Applicable range** expansion, shown only for a tool **validation** claim. It loads the *chosen version's* numeric inputs from a new read endpoint `GET /api/vv/numeric-factors/{tool_version_id}` (`api/routers/vv.py:numeric_factors`, reading `ToolVersion.descriptor_snapshot`, filtering `type ∈ {integer,float}`, returning `{name,label,unit,minimum,maximum}` via `VVNumericFactorResponse`) and renders a Min/Max pair per factor by **label**, keyed by **name** so it matches the submit-time envelope check. `_refresh_range_editor` rebuilds it reactively on version/kind/type changes (incl. the pre-filled-target case); `_collect_range` (module-level) validates min≤max and drops empty factors. The create body now sets `applicable_range`. So the previously API-only envelope feature is authorable in the UI.
- **Migrate approved records to a new version** — `VVService.migrate_tool_records(actor, source_tv, target_tv, record_ids)` copies chosen **approved** records forward onto a target version as fresh **drafts** owned by the actor (kind/range/confidence/standards/notes/evidence copied; re-review required — the bundle changed, so the sign-off doesn't transfer, mirroring the migrate-import "reset to new" rule). Whole-request invalids (same version, different alias, missing version) raise the new `VVMigrationError` (400); per-record problems (not approved, wrong source, exact-duplicate already on target) come back as `(id, reason)` **skips**. Idempotent via `_tool_record_content_key` (a claim-content fingerprint compared against the target's live records). Curate-gated. Endpoint `POST /api/vv/tool-records/migrate` (`VVMigrateToolRecordsRequest` → `VVMigrateToolRecordsResponse{migrated,skipped}`); UI **"Migrate to new version"** button + `open_vv_migrate_dialog` (tool → source version → target version → tick approved records).
- **Retire from any live state (incl. pending)** — `_ALLOWED_TRANSITIONS["pending"]` gained `retired` (`vv/orm.py`). `VVService.retire_{tool,job}_record` now `_withdraw_open_gate`s a pending record's open `approval_request` before flipping to retired, via a new `ApprovalService.withdraw(request_id, actor, actor_is_requestor, reason)` — closes the gate terminally as `denied` (the vocabulary isn't widened) but audits distinctly as `approval.request.withdrawn` and sets `decided_via = self|admin_override`. No requestor denial email (withdrawal runs in the owning service, not the decide router path). UI: `_render_actions` adds a **Retire** button to the pending branch (never blocked by the self-approval rule — a withdrawal isn't a decision); `_confirm_retire` copy is now state-aware. New client methods `vv_numeric_factors`, `vv_migrate_tool_records`.

**Tests** — `tests/vv/test_service.py` (retire-pending-withdraws-gate for tool + job; migrate copies-as-draft / skips-non-approved-and-wrong-source / same-version-and-cross-tool-raise / idempotent), `tests/vv/test_lifecycle_helpers.py` (`pending → retired` allowed), `tests/api/test_vv_router.py` (numeric-factors lists numeric inputs by label).

### 16.25 KPI extraction — per-tool results warehouse, ADR-0012 (shipped 2026-05-29)

ADR-0012 added the cross-job KPI discovery layer — descriptor-declared headline outputs are captured at job completion into a dedicated table; a per-tool `/results/{alias}` page surfaces them as a filterable, plottable, exportable warehouse. The bulk `outputs.json` ages out per ADR-0003; KPI rows persist forever as the permanent headline index.

Design: `designs/kpi-extraction.md`. Implementation in `matofx.kpi` (service, ORM, errors, in-memory re-extraction lock) + `matofx.api.routers.kpi` (5 endpoints) + `matofx.reports.warehouse_report` (PDF builder) + per-job + descriptor-wizard + ADR-0004 report surfaces.

- **One new table** — `job_kpi_value` (migration `0057`): `(job_id ON DELETE CASCADE, tool_alias, tool_version_id ON DELETE RESTRICT, kpi_name, value_numeric Numeric(38,12), unit, extracted_at)` + composite UNIQUE `(job_id, kpi_name)` + two perf indexes `(tool_alias, kpi_name)` and `extracted_at`. Denormalised `tool_alias` / `tool_version_id` skip the join on the hot warehouse query path. No SQLAlchemy `relationship()` declarations — keeps the range-scan free of N+1 traps.
- **Descriptor extension** — four new fields on `OutputField`: `kpi: bool = False`, `kpi_label: str | None`, `kpi_unit: str | None`, `kpi_category: str | None`. `_kpi_consistency` model validator: `kpi=true` requires `type == "number"`; the three override fields may only be set when `kpi=true`. `ToolDescriptor._kpi_labels_unique` field validator: two KPI outputs may not share the same effective warehouse column label (`kpi_label` when set, else `label`). Defaults preserve back-compat for existing descriptors.
- **Runner extraction hook** — `extract_kpis_for_job(audit, session, job, outputs_doc)` slots into `runner.supervisor._post_complete` immediately after `record_study_result` and before `_apply_cleanup_for_tool`. Reads the frozen `tool_version.descriptor_snapshot` to know which outputs are KPIs; UPSERTs one `job_kpi_value` row per declared KPI keyed on `(job_id, kpi_name)`. Sentinel-NULL classification: missing / non-numeric / `bool` / NaN / ±Inf each map to `value_numeric = NULL` with a WARNING log + audit detail; the row still lands so the warehouse can answer "did this job complete with KPI X populated?" without joining `outputs.json`. **Never-raise contract**: the body runs inside `session.begin_nested()` so an unexpected DB error rolls back just the KPI work without poisoning the outer transaction.
- **Re-extraction sweep** — `reextract_tool_version(audit, session, tool_version_id, triggered_by_user_id, trigger)` walks every completed job of the version, re-reads its `outputs.json` from durable storage (`storage_location.mount_path + job.working_dir_relative + "outputs/outputs.json"`; ADR-0003 retention floor keeps the file), re-runs the extractor per job, rate-limits 50-jobs-per-yield. Serialised per `tool_version_id` via the in-memory `matofx.kpi._reextract_state.acquire()` lock — raises `KpiReextractInProgressError` if another sweep is in flight; `is_reextract_in_progress(tv_id)` is the visibility surface the warehouse page reads for its "refreshing" banner. Process-local for v1; multi-process runners (v1.x) swap in a DB advisory lock with the same surface. Audits `kpi.reextracted` with the trigger discriminator (`admin_manual` / `descriptor_promote`). `compute_kpi_signature(descriptor)` + `should_reextract_after_descriptor_change(prev, new)` are the auto-trigger condition helpers; the actual `ToolService.promote_state` call-site wire-up is a small follow-up.
- **HTTP API** (`api/routers/kpi.py`) — 5 endpoints behind `features.kpi_extraction` 404-when-off gate:
  - `GET /api/tools/{alias}/kpis` — catalog. `?version_id=` answers for that one version; without it, the catalog is **the newest version's declared KPIs plus any `kpi_name` that still has rows in `job_kpi_value`**, deduped on `kpi_name` alone (see the *Catalog semantics* note below).
  - `GET /api/tools/{alias}/results` — paginated warehouse rows; per-row pivoted KPI values + `hidden_by_acl` rollup.
  - `GET /api/tools/{alias}/results/timeseries?kpi_name=...` — ascending-extracted-at points for one KPI.
  - `GET /api/tools/{alias}/results/export.csv` — streaming CSV (100k row cap); supports `?job_ids=` allow-list for the unbounded-selection export.
  - `GET /api/tools/{alias}/results/export.pdf` — self-contained printable HTML with `window.print()` on load; same ADR-0004 pattern as the per-job report (browser does the actual PDF conversion).
- **ACL** — no new permission; composes the existing per-project ACL via `_visible_project_ids(session, user_id)` (set of accessible project ids, or `None` for `projects.see_all` holders). The warehouse query walks both scenario-homed and study-homed jobs through OUTER JOINs to either home's project. `hidden_by_acl` count is computed via a parallel ACL-bypassed COUNT query — surfaces "12 hidden by access controls" in the page footer (design §8.1).
- **V&V status filter** (slice 9) — `WarehouseFilters.vv_status` ∈ `any` / `verified_only` / `verified_validated`. Resolves via `VVService.summarise_tool_status(tool_version_id)` so cohort-key inheritance is honoured. Filter applied pre-pagination via `Job.tool_version_id IN (passing_ids)`; per-row `vv_status` badge populated by batched fetch (one per distinct `tool_version_id`, never N+1). Slice-9 cleanup of the V&V build checkpoint's slice 13 marker.
- **UI** — `ui/pages/results.py`:
  - **Landing** at `/results` — tool picker listing every tool that declares any KPI.
  - **Warehouse page** at `/results/{tool_alias}` — header (tool name + version dropdown), filter card (completed window + Reset; project/scenario/requestor pickers are a polish follow-up), AG-Grid table (one row per visible completed job; identity cols + one column per KPI with `kpi_label (kpi_unit)` headers; pagination 50/page; "Showing N–M of T • Q hidden by access controls"), **unbounded tick-box selection** with persistent "N selected" pill + Clear link, "View in Compare (N)" button auto-disabled past 6 with the cap tooltip `"Compare is limited to 6 rows — narrow your selection"` (see `feedback_unbounded_selection_capped_actions`).
  - **Export bar** — three buttons: "Download CSV (filtered)" (always enabled), "Download selected as CSV (N)" (disabled at N=0 with tooltip), "Download PDF" (the printable HTML). Selection survives filter and pagination — rows that leave the filter stay ticked but visually hidden.
  - **Plot card** (collapsible, default open) — Mode dropdown (Scatter / Time-series) + X axis + Y axis pickers; scatter mode renders the table's currently-loaded rows; time-series mode calls `/results/timeseries` and renders ascending-time. Pure-logic plot helpers in `ui/pages/_results_plot.py` (`default_plot_state`, `build_scatter_data`, `build_timeseries_data`, `axis_label`).
  - **Selection helper** (`ui/pages/_results_selection.py`) — `SelectionState` (frozenset-backed; `tick`/`untick`/`clear` return new instances), `CompareAction` with the disabled-button + tooltip matrix, `build_compare_href` routes through `compare.selection.build_query` so the handoff shares Compare's URL contract.
  - **Per-job Headline KPIs card** (`ui/pages/job.py::_render_complete`) — large-numeric tiles per KPI + 30-day inline SVG sparkline per KPI (no Plotly bundle weight for the indicator chart). Skipped on inflight / failed.
  - **Descriptor wizard** (`ui/pages/_descriptor_wizard.py::_render_one_output` + `_kpi_wizard_helpers.py`) — "Mark as KPI" checkbox + popover (`kpi_label` / `kpi_unit` / `kpi_category`) inside the per-output card; row hidden for non-numeric outputs (`is_number` visibility gate); "KPIs declared" badge in the Outputs tab header.
  - **ADR-0004 report integration** — `ReportContext.kpi_cards: list[dict]` + new "Headline KPIs" section in `report.html.j2` (stylesheet rules `.kpi-card-grid` / `.kpi-card-value` / `.kpi-card-unit` extending the existing report stylesheet). Cards populated by `_load_kpi_cards(session, job_id, tool_alias, tool_version_id, settings)` — reads `job_kpi_value` not the outputs doc so the report renders correctly after ADR-0003 retention has dropped `outputs.json`. Audits `kpi.viewed surface=report` with `kpi_count` payload.
  - **Nav** — Dashboard → "Results warehouse" (feature-gated, open to any authenticated user).
- **HTTP API client** (`ui/api_client.py`) — 5 KPI methods: `list_tool_kpis`, `list_tool_results`, `tool_results_timeseries`, `tool_results_csv_url`, `tool_results_pdf_url`. The two `_url` methods return URLs (not bodies) — the browser handles the actual download via `Content-Disposition: attachment` for CSV and the print dialog for PDF.
- **Audit events** — `kpi.extracted` (per-job; payload `{tool_alias, tool_version_id, extracted, missing, total}`), `kpi.reextracted` (per-sweep; `{tool_version_id, trigger, job_count, extracted, missing, skipped_no_outputs}`), `kpi.viewed surface=report` (per ADR-0004 render).
- **Tests** — ~146 across `tests/kpi/` (slices 0/1/2/3/4/9 — skeleton, ORM, service, reextract, V&V filter), `tests/api/test_kpi_router.py` (slices 5+7 — 5 endpoints), `tests/ui/test_results_selection.py` + `test_results_plot.py` + `test_kpi_client.py` + `test_job_kpi_card.py` + `test_kpi_wizard_helpers.py` (slices 6/7/10/11 — pure logic + end-to-end client), `tests/reports/test_kpi_card_render.py` + `test_warehouse_report.py` (slice 8 + PDF builder), `tests/tools/test_descriptor_extended.py` (slice 1 — descriptor validation).
- **Carry-overs to v1.x / follow-ups** —
  - **`ToolService.promote_state` auto-trigger** — `should_reextract_after_descriptor_change` ships as a ready-to-wire helper; the actual call-site wire-up wasn't bundled (semantics of which version to back-fill on a promote depend on the bundle-hash cohort question the V&V infra already solves; cleanest to land it alongside the descriptor wizard or a small dedicated slice).
  - ~~**Per-KPI value-range filters** — the warehouse page surfaces a UI placeholder~~ — **DONE 2026-07-22** (see §16.45): `WarehouseFilters.kpi_ranges` + a correlated `EXISTS`-per-KPI subquery, the `kpi_range=name:min:max` query param on the list + CSV + PDF endpoints, and real min/max inputs on the filter card. The V&V-status filter (backend-ready since slice 9) is now surfaced too. Exports honour both (V&V post-filter factored into `_apply_vv_status_filter`, shared by the table + CSV paths).
  - **Project / scenario / requestor multi-select pickers** on the warehouse filter card — the API accepts the params (slice 5); the page UI doesn't surface the widgets yet.
  - **Plot series / marker-size pickers + cross-tool dashboard + project-rollup scorecards + saved warehouse queries + scheduled exports + custom KPI formulae + KPI-value time-series alerting** — all explicitly v1.x per design §13.

**Catalog semantics (corrected 2026-09-21).** `list_kpis_for_tool` originally returned the union over *every* `ToolVersion`, deduped on `(kpi_name, kpi_label, kpi_unit)`. Both halves were wrong:

- **The union resurrected dead columns.** `bracket_stress` advertised "2 KPI(s)" and drew two all-`—` columns over 650 runs, because two retired versions (`0.1.0` / `1.0.0`, zero jobs between them) had declared them while every version the jobs actually ran declared none. That contradicts `designs/kpi-extraction.md` §109 — a descriptor with no `kpi = true` output is meant to be invisible to the KPI layer. The catalog now takes the **newest version's** KPIs and adds back only names that still have captured values, resolving each one's label / unit from the newest version that declared it. A KPI un-ticked after runs captured it keeps its column; one that was never captured is gone.
- **The `(name, label, unit)` dedup key doubled columns.** Un-ticking sweeps `kpi_label` (`clear_kpi_overrides`), so a re-tick's effective label falls back to the output's own `label` and the key diverges — two entries for one output. Every downstream surface keys on the name alone (AG-Grid `field: kpi__{kpi_name}`, the row pivot, the CSV header, the per-KPI range filter), so two entries sharing a name can only ever render as **duplicate** columns over the same values, never as distinct ones. Dedup is now on `kpi_name`, newest-version-wins on the label.

**Per-job card pins the job's own version.** `job.py::_render_job_kpi_card` called `list_tool_kpis(alias)` with no `version_id`, so a run was judged against the tool-wide catalog: a job whose pinned descriptor declared no KPI still drew tiles reading `—`. It now passes `job["tool_version_id"]` (populated by `GET /api/jobs/{id}`), falling back to the tool-wide catalog only when the version didn't resolve.

**Descriptor-wizard KPI wipe (data incident, repaired 2026-09-21).** From the initial commit until `2d59fd1` (2026-08-12), `_descriptor_wizard._output_to_payload` never serialised the `kpi` / `kpi_label` / `kpi_unit` / `kpi_category` keys, so **any** save through the editor silently cleared every KPI tick — including edits that only touched inputs. Four demo tools lost their KPIs this way (`bracket_stress` at v1.0.1, `thermal_soak` at v1.0.2, `composite_laminate` at v1.0.2, `simple_fermentation` at v2.1.1). The serialisation bug is fixed; the demo DB was repaired in place (live `tool.descriptor` + every `tool_version.descriptor_snapshot` from the wipe forward — the snapshots matter because extraction reads the job's *pinned* snapshot) and back-filled through `reextract_tool_version`, capturing 2,858 values across 1,044 completed runs with zero skips. The same shape of repair is what any install that ran a pre-`2d59fd1` wizard needs.

**Migrations shipped** — `0057` (`job_kpi_value` table + composite UNIQUE + two perf indexes).

**Key files** — `matofx/kpi/{__init__,orm,service,errors,_reextract_state}.py`; `tools/descriptor.py` (`OutputField.kpi*` fields + `_kpi_consistency` + `ToolDescriptor._kpi_labels_unique`); `features.py` (`KPI_EXTRACTION`); `db/migrations/versions/0057_job_kpi_values.py`; `runner/supervisor.py` (`_post_complete` hook); `api/routers/kpi.py` (5 endpoints) + `api/routers/reports.py` (KPI cards) + `api/schemas.py` (+6 DTOs) + `api/app.py` (router registration); `reports/warehouse_report.py` (PDF builder) + `reports/builder.py` (`ReportContext.kpi_cards`) + `reports/templates/report.html.j2` (Headline KPIs block); `ui/_features.py` (`kpi_extraction_enabled`); `ui/pages/results.py` + `ui/pages/_results_selection.py` + `ui/pages/_results_plot.py`; `ui/pages/job.py` (Headline KPIs card + sparkline); `ui/pages/_descriptor_wizard.py` + `ui/pages/_kpi_wizard_helpers.py` (wizard KPI editor); `ui/pages/_chrome.py` (nav entry); `ui/api_client.py` (5 KPI methods).

---

### 16.26 Surrogate family — run cohorts, surrogate model registry, surrogate-as-tool, ADR-0008 (shipped 2026-06-03)

ADR-0008 turns a fitted surrogate from a per-study analytics convenience into a first-class, governed, reusable asset: a **run cohort** gathers past completed runs of one tool into a live dataset; a **surrogate model** is fitted from a cohort or parametric study and persisted into a versioned, scope-tiered registry; a **checked + released** global model can be **exported as a runnable tool** that predicts in milliseconds and flows through the whole job/workflow/KPI/lineage machinery like any other tool.

Design: `designs/surrogate-family.md` (+ the shared `designs/studies-engine.md` foundation). Implementation across `matofx.studies` (cohort + surrogate engine), `matofx.models` (registry), and integration points in `tools`, `hierarchy`, `lineage`, plus API + UI. All behind `features.surrogate_family` (404-when-off / nav-absent).

- **`study_result` foundation** (migrations `0058`–`0060`) — the old `parametric_study_result` is renamed to `study_result` with a `(source_kind, source_id)` discriminator (`parametric` / `optimisation` / `cohort`), so one unified store backs the Results explorer regardless of what generated the runs. `0060` drops the legacy `parametric_study_id` column after consumers moved over.
- **Run cohorts** (migration `0059`; `matofx.studies.cohort`) — _**reworked into Job Cohorts — see §16.30 for the current (workspace-level, version-banded, live-or-fixed) model; the description below is the original 2026-06-03 shipment.**_ `run_cohort` is a *saved query* over completed jobs of one tool (tolerated-version list + optional filters); membership is **computed live, never materialised** (non-negotiable #1). `CohortService` does create / update / resolve / lazy-populate / archive / list with cross-project ACL **intersection** (Viewer+ on every referenced project, re-checked at each resolution). Surfaces: `/cohorts` workspace + `/cohorts/{id}` detail with the member-results matrix; `api/routers/cohorts.py`.
- **Source-agnostic surrogate engine** (`matofx.studies.surrogate` package) — `collect_training_data(source_kind, source_id, response_spec, factor_filter)` feeds the existing scalar GP/poly fitter; the parametric flow is byte-for-byte unchanged. A **field-surrogate** path (`field.py`: POD/PCA via SVD + per-mode GPs) handles `OutputField.kind='field'` ensembles (VTK / CGNS / Ensight / `.npy`), with the retention floor extended so descriptor-declared field outputs are unconditionally kept.
- **Model registry** (migrations `0061`–`0063`; `matofx.models`) — `surrogate_model` (logical entry: name, tags, the single `tool_alias` it predicts for, scope tier, steward, lifecycle state, published-version pointer) + append-only `surrogate_model_version` (immutable per-fit: ordinal, scalar/field kind, frozen factors + responses, source discriminator, on-disk **sha256-verified** bundle, CV quality, publish state). `published_version_id` and `Job.surrogate_model_version_id` are **logical pointers** (DB FK in migration only, no ORM `relationship` — avoids cross-module mapper coupling). `ModelService`: create / refit / get / list (catalogue) / list-for-picker / coverage / update / scope / retire / open_bundle / load_for_runtime + the governance verbs below. Bundle integrity is verified on **every** load (`ModelBundleCorruptError`); `pickle.load` only ever happens inside `joblib` in the runner subprocess (non-negotiables #2/#3/#9).
- **Two-gate global release** (§6.3) — personal/project scope publishes directly; `global` runs **gate 1** (technical sign-off — toggleable via the `models_global_publish_approval` General Setting): on ⇒ the builder nominates one Model Curator *checker* (≠ builder) and a directed `approval_request` opens (`surrogate_model_version_id` target, decided through the **unified** `/api/approvals/decisions` → `ModelService.decide_check`); off ⇒ the builder-curator checks directly to `checked`. Then **gate 2** (mandatory, non-toggleable): a super-user/admin `release_version` flips `checked → published`, advances the pointer, activates the model. The builder is never the checker or releaser (non-negotiable #7).
- **Capability role** (migration `0062`) — `MODELS_CURATE` (`models.curate`) carried by the `model_curator` capability role (ADR-0005 pattern); super-user holds it via the catalog sentinel. Gates: global publish, scope demote/promote across the global boundary, refit of a global model, checker eligibility.
- **Surrogate-as-tool synthesis** (`matofx.models.tool_synthesis` + `exporter.py`) — `synthesise_descriptor` builds a normal `ToolDescriptor` (one `float` input per factor, one `number` output per response + a `<response>_std` sibling for GP uncertainty) and `synthesise_orchestrator` emits a v2 tool main script (reads `inputs/inputs.json`, loads the **pinned** bundle from disk sha256-verified, writes `outputs/outputs.json` — no DB in the run subprocess). `register_as_tool` writes the orchestrator under `<models_dir>/exported_tools/<env>/<alias>/`, installs via the existing `ToolService.install`, and promotes to **`test`** — the su/admin then edits the safe surface and promotes `test → released` (that promotion *is* the gate-2 release for the exported-tool path). The descriptor carries a new `surrogate_model_version_id` field (round-trips into `ToolVersion.descriptor_snapshot`, so the pin is per tool version).
- **Coverage gauge + extrapolation policy** (§7.3, non-negotiable #8) — advisory by default: synthesised inputs are left **unbounded** so the user can extrapolate, and the in-form gauge (`ui/_coverage_gauge.py`, classifying via `classify_coverage` / `worst_coverage`) warns per factor (in-range / near-edge / extrapolation / far-extrapolation) with a worst-of summary, mounted on the My Jobs surrogate-tool form from `GET /api/models/versions/{id}/coverage`. The optional `block_extrapolation` toggle instead *tightens* the input bounds to the training range so the ordinary form validation hard-rejects out-of-range submissions — no special rejection path.
- **Submit-time lineage tag** (§7.4, non-negotiable #4) — `JobService.create` reads the resolved tool version's `descriptor_snapshot`; when it carries a `surrogate_model_version_id` the child `Job.surrogate_model_version_id` is set. A surrogate tool is rejected as a study's *fitting* tool (§7.5) — `StudyService.create` reads the same pin and refuses (no surrogate-of-a-surrogate).
- **Lineage integration** (§10) — `LineageService._resolve_job` emits a `RAN_SURROGATE_VERSION` edge to the `SURROGATE_MODEL_VERSION` node; `_resolve_surrogate_model_version` resolves that node with `PUBLISHED_BY` (fitter) + `DERIVED_FROM` (source parametric study) edges; `_list_surrogate_model_versions` backs the admin lineage picker. The cytoscape view already styled the kind (purple rectangle). KPI extraction needed **no change** — a surrogate tool's synthesised outputs carry the same `kpi` markers and flow through the tool-agnostic read path identically.
- **Models UI** (`ui/pages/models.py`) — `/models` catalogue (scope filter, status-badged rows, "fit a surrogate from a study/cohort" form) + `/models/{id}` detail (version history with per-response Q²/RMSE, the two-gate submit-review/release controls with the curator-checker picker, refit, scope-change, retire, export-as-tool, metadata edit). Curate/release controls resolve against the cached permission so they're *hidden*, not just server-rejected. Feature-gated "Surrogate Models" nav entry; 11 `ApiClient` model methods.
- **API** (`api/routers/models.py`, `cohorts.py`) — model lifecycle + catalogue (`GET /api/models`), picker, `approvers`, coverage, submit-review / release, export-as-tool, scope/retire/refit/update; `ModelError` family → 400 in the central handlers; `get_model_service` wires `models.models_dir` + environment.
- **Tests** — registry + governance + flow (`tests/studies/test_model_registry.py`, `tests/api/test_models.py` + `test_model_release.py`), synthesis (`tests/studies/test_tool_synthesis.py`), cohorts + ingestion + field surrogate (`tests/studies/test_cohort_service.py`, `test_surrogate_ingest.py`, `test_field_surrogate.py`, `tests/api/test_cohorts.py`), submit-tag + study-guard (`tests/hierarchy/test_job_service.py`, `tests/studies/test_study_service.py`), lineage (`tests/lineage/test_service_resolvers.py`), coverage gauge + UI clients (`tests/ui/test_coverage_gauge.py`, `test_models_client.py`).
- **Deferred** (per design) — the V&V `vv_surrogate` target (§10, reserved for a V&V follow-up); refit-available "N new points" system badges; field-surrogate viewer + diagnostics page; carry-forward on refit re-export. `meshio` / `pyarrow` SBOM + lockfile bump is the pre-release maintainer step.

**Migrations shipped** — `0058` (study_result rename + discriminator), `0059` (run_cohort), `0060` (drop parametric_study_id + composite unique), `0061` (surrogate_model + surrogate_model_version + `Job.surrogate_model_version_id`), `0062` (model_curator role + `models.curate` grants), `0063` (approval target widening to surrogate). Single linear head `0063`.

**Key files** — `matofx/studies/cohort/{orm,errors,service}.py` + `matofx/studies/surrogate/{__init__,ingest,field}.py`; `matofx/models/{__init__,orm,errors,bundle,service,runtime,tool_synthesis,exporter}.py`; `tools/descriptor.py` (`ToolDescriptor.surrogate_model_version_id`); `hierarchy/service.py` (submit-time tag) + `studies/service.py` (surrogate-tool guard); `lineage/service.py` (job edge + version resolver + centre list); `config/{models,general_settings}.py` (`ModelsSettings`, `models_global_publish_approval`); `features.py` (`SURROGATE_FAMILY`); `db/migrations/versions/0058`–`0063`; `api/routers/{models,cohorts}.py` + `api/schemas.py` + `api/deps.py` + `api/errors.py`; `ui/pages/{models,cohorts}.py` + `ui/_coverage_gauge.py` + `ui/pages/my_jobs.py` (gauge mount) + `ui/pages/_chrome.py` (nav) + `ui/api_client.py`.

---

## UI Conventions (v2)

These are durable, app-wide rules. They override per-page preferences and apply to every page added under `OUTPUTS/matofx/src/matofx/ui/pages/`.

### Filters and search controls live above the results, never below

On any page that has a filter bar, search box, "show archived" toggle, owner picker, contains-tool picker — the controls render **at the top of the page body**, above any result list / cards / table they affect. Reasons:

- Discoverability — users scan top-down. Filters below results are easy to miss.
- Mental model — "narrow then read" matches the way the rest of the application works (My Jobs, Compare, Admin Tools all put their filter row above the list).
- Implementation guard — in NiceGUI, the order you `ui.column()` / `ui.card()` is the order they render. Creating the list container *before* the filter card is a common bug (the list ends up on top); always build the filter card first, then the result container.

If a page needs in-list filters too (e.g. a per-row search-as-you-type), that's fine — but the primary filter row stays at the top.

### A nav target's query is part of its identity

Two sidebar entries may point at the same page and differ only by a query
(`V&V records` → `/vv-records`; `V&V records inbox` → `/vv-records?tab=queue`).
The active-entry highlight therefore compares **path + query**, not path alone:
`_current_nav_path` keeps `request.url.query`, and `_active_target` treats a
target's query as parameters it *pins* — the target matches only when the current
URL sets each of them to the same value, while a target with no query ignores the
current one entirely and still lights on any other tab (matching a query too
strictly would leave the sidebar with nothing highlighted). Longest match wins, so
the more specific query-bearing entry beats its bare parent.

Fixed 2026-09-21 (reported): `_current_nav_path` returned `request.url.path` and
dropped the query, so the current path was always plain `/vv-records` and the
inbox entry — whose target contains `?` — could never equal or prefix it. Clicking
the inbox opened the queue but highlighted its parent, every time. It was the only
query-bearing nav target, so the whole class of bug had exactly one instance;
`tests/ui/test_login_redirect.py` now walks **every** nav target through
`_current_nav_path` → `_active_target` and asserts it resolves to itself, so a
future one can't be added unreachable.

### Tool labels in pickers show the human name, not the alias

When a user is picking a tool from a dropdown / multi-select / chip group, the option label is the tool's display **name** only (e.g. "Material Properties"). The alias is the wire identifier and belongs in the URL / API / audit log, not in front of end users. The `matofx.ui.format` layer already enforces this for tool labels generally; new pages must follow suit.

### Plotly figures route through `matofx.ui.theme`

Never pin `template="plotly_white"` (or any other fixed template) directly on a figure. Pass it through `apply_theme_to_plotly(fig)` so it tracks the active palette. For `ui.plotly` use `themed_plotly(fig)` (returns `{data, layout, config}` — `MATOFX_PLOTLY_CONFIG` disables the Plotly logo and enables responsive). For "Download as HTML" buttons use `export_plotly_html(fig)` (CDN-embed, same config). PDF export is browser-print via auto-print HTML + `window.open` on a blob URL — never re-add kaleido / weasyprint.

### Box surfaces use a thin white border, never a drop shadow

Quasar's `elevation` / `box-shadow` props are off-limits on cards, dialogs, popovers, and any other framed surface. Replace with a 1px ~35%-opacity white border (palette token). Durable user preference — see `feedback_no_box_shadows`.

### Workspace pages follow the Compare / home shell idiom

Full-page workspaces (the **Studies** nav group — Compare Jobs, Job Cohorts, Parametric Studies, Optimisation, Surrogate Models — and any new sibling) share one chrome idiom so they read as one app rather than a stack of bespoke pages:

- **Full-bleed body** — outer column is `w-full` with `padding: 0.85rem 1.5rem 1.5rem;` (no `max-w-* mx-auto p-6` cap).
- **Header** — bold title at `font-size: 1.6rem; font-weight: 650; line-height: 1.1`, with a muted one-line subtitle in `var(--matofx-muted-text)` at `0.9rem`.
- **Tabs** — `ui.tabs(...).props("align=left active-color=primary indicator-color=primary")` with a `border-bottom: 1px solid var(--matofx-card-border)`; every `ui.tab(...)` carries an icon and `.props("no-caps")` (no Quasar ALL-CAPS / blue indicator). Tab panels are `background-color: transparent` so the page background shows through.
- **Section panels** — lead each top-level block with `section_card("icon", "Title")` (`_chrome.py`) for the icon-chip + hairline header rather than a bare `ui.card()` with a bold label.
- **Greys** — page chrome uses `var(--matofx-muted-text)` (theme-aware), never hard-coded `#888` (the exception is Plotly trace colours inside a figure).

The reference implementation is `ui/pages/compare/page.py`; the four Studies-group pages (`cohorts.py`, `studies.py`, `optimisation.py`, `models.py`) were aligned to it (landing + detail surfaces) on 2026-06-24.

---

## Parametric Studies — Results analytics (v2)

Parametric Studies are new in v2 (no `legacy_code/` equivalent; the legacy app
had only an ad-hoc `*_repeat` tool). Full design: `designs/parametric-studies.md`
and `decisions/0001-parametric-studies.md`. The analytics architecture:

- **Pure analysis core** — `matofx.studies.analysis` holds the statistics as
  pure, exhaustively-tested functions over plain Python sequences (no DB, no
  IO): `summarize` (per-response descriptives), `compute_sensitivity`
  (Pearson/Spearman + standardized regression with statsmodels OLS inference —
  SE / p / 95% CI / adj-R² / RMSE), `varied_columns`, `pareto_indices`
  (multi-objective non-dominated set), `main_effects` + `anova_contributions`
  (Type-I ANOVA % contribution for designed studies), and `fit_rsm` +
  `rsm_predict` (quadratic response surface + classified stationary point). It
  is robust to the partial / **diverged** data real solvers produce:
  NaN/inf and finite-but-extreme (`> 1e12`) values are excluded with a caveat,
  and standardization is overflow-safe.
- **Numeric dependencies** (all BSD-3/MIT, air-gap-bundled; lockfile + CycloneDX
  SBOM regen is a tracked maintainer follow-up): numpy, scipy, pyDOE3 (design
  generation), scikit-learn (the 2-PS-c surrogate), statsmodels (OLS inference,
  ANOVA, RSM). mypy treats the partially-typed ones as `Any` via a per-module
  override; the module's own surface stays strictly typed.
- **Read-side endpoints** — thin glue in `matofx.api.routers.studies` over a
  `ResultsService` + `SeriesService` (both project-VIEW gated): `/results`,
  `/results.csv`, `/csv-series`, `/csv-aggregate`, `/sensitivity`,
  `/sensitivity-matrix`, `/anova`, `/rsm`. The durable per-child result store
  (`parametric_study_result`, written pre-cleanup) is the single source; ADR-0003
  retention floor keeps `inputs.json` / `outputs.json` / `csv_plot` files.
- **Results tab** (`matofx.ui.pages.studies`) — a stack of independently
  error-bounded cards: full-grid matrix (AG Grid: sort + per-column filter +
  grouped show/hide; its filters drive the plots and its row-selection
  highlights them), Summary/distribution, unified Plot (X/Y/Z + colour, 2D/3D /
  parallel-coords / csv overlay), Sensitivity, Main-effects & ANOVA, Response
  surface, Prediction profiler, and Trade-offs (Pareto). Descriptor **labels +
  units** are shown everywhere — raw input/output names never reach the user.
- **Scalar surrogate** (`matofx.studies.surrogate`) — a Gaussian-process
  (uncertainty-aware) or degree-2 polynomial fit over the varied inputs, with
  cross-validated Q²; the Results tab's **Prediction Profiler** fits one
  surrogate per response (off the event loop via `run.io_bound`, cached in the
  page session), then per-factor **sliders** re-`predict` every response's
  profile live (JMP-style cross-linking; GP uncertainty bands), with a
  differential-evolution **optimiser**. Slider drags never refit — they only
  predict, so the interaction stays snappy. A **surrogate contour profiler**
  (two-factor contour, held-factor sliders) and **variance-based / Sobol global
  sensitivity** (`sobol_indices`: Saltelli sampling via `scipy.stats.qmc` on the
  surrogate → first-order S1 + total-effect ST; no SALib dependency) round out
  the surrogate analytics.
- **Deferred** (later phase): a field/non-scalar (CFD/FEA) surrogate — predicting
  whole simulation fields across the design space, not just scalar outputs.

**Catalogue + detail + Results-look follow-up (2026-06-25, no migration):**

- **Enriched `StudyResponse`** — gained `tool_name`, `project_name`, `created_by_name`
  and `tool_version` (the version *string*). `StudyDetailResponse` gained
  `input_labels` / `input_units` from the pinned descriptor. Both filled by
  `_enrich_studies` (batched Tool / Project / User / ToolVersion lookups) on every
  read path (list / get / submit / cancel / create). The catalogue rows and the
  detail caption now read by **label**, never the alias / version id
  ([[feedback_use_labels_not_names]]).
- **Catalogue** (`ui/pages/studies.py`) — a **search box** (name / tool / project /
  owner, client-side over the already-fetched studies) and a **status filter** with
  five plain-English buckets: All · Draft · Running (⊇ pending_approval) · Complete
  (⊇ partial) · Cancelled (⊇ rejected, failed). Rows show tool label · project ·
  owner · run count.
- **Detail** — caption shows tool label + pinned **version string** + mode; the
  state chip + Cancel are left-clustered with the title (off the fixed top-right
  controls); the **Progress** list shows descriptor labels (`label=value unit`).
- **Results tables** — numeric columns are right-aligned + tidily formatted
  (grouping separators, ~5 s.f.); the Quasar stat tables (sensitivity / ANOVA /
  best runs) are de-shadowed + restyled to the card idiom via shared chrome CSS.
- **Theming follow-up (2026-06-25, second pass).** The first attempt themed the
  AG-Grid matrix via legacy `.ag-theme-balham` CSS — inert, because **NiceGUI 3.x
  ships AG Grid's Theming API** (quartz + a *variable* colour scheme, switched
  light/dark by the `data-ag-theme-mode` attribute the element JS sets from
  `body.body--dark`). Corrected to drive the `--ag-*` theme-parameter variables on
  `.nicegui-aggrid` (background / foreground / header / accent / hover) so the
  matrix matches the card surface; the grid already re-themes on toggle for free.
- **Live Plotly re-theme on toggle** — `apply_theme_to_plotly` leaves paper/plot
  backgrounds transparent (CSS repaints them live) but **bakes `font.color` + the
  template gridlines** at render, so text + gridlines stayed frozen after a
  no-reload theme flip. `_chrome._inject_plotly_theme_hook` adds a single
  `MutationObserver` on `body`'s class (the same hook AG Grid uses) that
  `Plotly.relayout`s every on-page chart's font + axis colours (2-D axes, 3-D
  scenes, subplot axes) to the new palette side. App-wide; trace/marker colours
  left alone (palette-neutral). The Trade-offs (Pareto) plot was also rebranded —
  brand-teal optimal points + front line, muted dominated points, marker outlines.
  The Pareto non-dominated **run list** (a hand-built table, for its per-row
  click-to-focus + tints) was re-themed to hairline rules + muted header.
- **Duplicate-runs regression fix.** The study results matrix (and CSV-series)
  read joined `study_result` on `job_id` **alone**. Harmless until the cohort
  lazy-populate (ADR-0008 §3.3) started writing a second `source_kind='cohort'`
  row per job — after which a parametric child that also joined a cohort fanned
  out into a **duplicate run** in the matrix, best-runs, Pareto list and CSV
  series. Both reads (`results_service.get_results_for_study`,
  `series_service._children_with_results`) now constrain the join to
  `source_kind='parametric' AND source_id=study.id` (kept as an *outer* join in
  the matrix so result-less children still surface). Regression test:
  `test_results_matrix_ignores_cohort_result_rows`. The optimisation readers
  already filtered `source_kind`, so were unaffected.

#### 16.26a Field surrogates — POD/PCA field path + governed bundle + viewer (shipped 2026-06-08, incl. time-resolved)

The deferred field-surrogate path above is now built end-to-end on the
pre-existing POD engine (`matofx/studies/surrogate/field.py` —
`fit_field_surrogate` SVD-POD + per-mode GP, `FieldSurrogate.predict` with
per-node 1-sigma, streaming `svds` above 50k nodes). No migration: the
`surrogate_model_version.kind` CHECK already permitted `field`, and field
metadata rides in the `responses` JSON + the bundle manifest.

- **Deps** — `pyvista` / `meshio` / `pyarrow` added to `pyproject.toml`
  `[project.dependencies]` (permissive licences); the loaders read `.npy` on
  numpy alone and VTK / CGNS / Ensight via pyvista. Hashed-lockfile + SBOM
  regen is the maintainer `workflow_dispatch` step.
- **Descriptor + retention** — a field output is `type='file'` +
  `file_type='field'` carrying `field_format` / `field_node_array_name` /
  `field_remap_reference_mesh` / `field_timesteps` (validated by
  `OutputField._field_consistency`). `runner.supervisor._field_retained_paths`
  adds the snapshot — and a time-resolved `field_timesteps` glob's whole
  series — to the ADR-0003 retention floor so the POD fit has data.
- **Ingestion** — `studies.surrogate.ingest.collect_field_training_data`
  joins each `study_result` to its job, resolves the retained snapshot(s) on
  disk (reusing the `series_service` outputs-dir helpers), and loads them.
  **Time-resolved fields** (the 2026-06-07 requirement) contribute one
  `(run, timestep)` row each with a reserved `time` factor; the POD ensemble
  spans the full transient and the per-mode GPs take `time` as an input, so
  `predict({…, 'time': t})` reconstructs the field at any step.
- **Registry** — `ModelService` fits a governed `kind='field'`
  `surrogate_model_version`. The bundle (`models/bundle.py:write_field_bundle`)
  lays out `field/` — `mu.npy`, `modal_basis.npy`, `mode_energy.npy`, per-mode
  `mode_<m>.joblib`, and an optional reference mesh for viewer geometry — under
  the same `_bundle_sha256` integrity envelope verified on every load. `create`
  routes to the field path by descriptor via `resolve_field_spec`; `refit`
  reconstructs the spec from the version; `field_predict` loads the sha-verified
  bundle off the event loop (`asyncio.to_thread`). API:
  `POST /api/models/{id}/versions/{vid}/field-predict`.
- **Viewer** — `ui/_field_surrogate_viewer.py` mounts a lazy expansion on
  `/models/{id}` for field versions: factor sliders bounded to the frozen
  training ranges, a **time slider with play/pause** for time-resolved
  surrogates, a live `field-predict` frame, and POD scree / reconstruction-RMSE
  / mode-coefficient panels. A geometry-bearing field (VTK / CGNS / Ensight)
  also renders a **coloured 3-D `Mesh3d` surface** (viridis, per-node intensity,
  fixed-range toggle): `bundle.extract_field_geometry` pulls the reference
  mesh's surface triangulation server-side (`pyvista.extract_surface` →
  `vtkOriginalPointIds` mapped back to the **full** node set so a per-node field
  aligns by index), served once via
  `GET /api/models/{id}/versions/{vid}/field-geometry` and re-coloured each
  frame. `.npy` fields (no geometry) use the value-vs-node trace with a 1-sigma
  band — the universal fallback. Lineage resolves field versions unchanged
  (surfaces `kind` in node metadata).
- **Per-job field render (incl. transient animation)** — the Job page renders
  the *one run's own* field output inline (not a surrogate). A server-side
  endpoint `GET /api/jobs/{id}/field-frames?output=` (jobs router) resolves the
  named field output from the captured descriptor, finds its retained
  snapshot(s) on disk (a steady file, or a `field_timesteps` series globbed
  under the working dir), and returns the shared mesh + per-frame node values
  (`_load_field_frames` reuses `extract_field_geometry` + `load_field_snapshot`,
  off-thread, same output-lockdown gate as the file download). `job.py::
  _render_field_output` draws a coloured `Mesh3d` (geometry) or value-vs-node
  line (`.npy`); a **transient** output gets a **time slider + play/pause**
  (`_field_time_controls`) that scrubs the frames with a fixed colour range —
  the per-run counterpart to the model viewer. Two field example tools exercise
  this: `field_demo` (steady `.vtp`) and `transient_plate` (a `.vtp` timestep
  series).

### 16.27 Optimisation framework — adaptive design-space search, ADR-0009 (shipped 2026-06-08)

The **third study sibling** (`matofx.studies.optimisation`), parallel to
`Scenario` / `ParametricStudy` / `RunCohort` under a `Project`: an adaptive
outer loop (propose → run → learn → repeat) that drives a tool — or a whole
workflow chain — toward an objective until a stopping criterion fires. Built
in 14 phases against `designs/optimisation-framework.md`; **amended 2026-06-08**
to re-orient single-objective Bayesian optimisation onto MATOFX's *own*
surrogate engine (ADR-0008) rather than Optuna (dropped); pymoo retained.

- **Data model** — `optimisation_study` (frozen `config_json`: target / factors /
  responses / constraints / strategy / run-mode / stopping; durable optimiser
  state ≤1 MB inline else a sha256-checked joblib blob; append-only
  `convergence_trace`; `current_best_json`). Children are ordinary `Job`s with
  `optimisation_study_id` (`scenario_id` NULL) — the job 1-of-3 home CHECK
  (`scenario` XOR `parametric_study` XOR `optimisation_study`, migration 0066).
- **Strategy registry** — pluggable `Strategy` ask/tell Protocol: the
  MATOFX-surrogate **`bo`** (three acquisition modes — *optimise* / *balanced* /
  *explore* — over the GP's μ ± σ); the pymoo classical spectrum (`cmaes` / `de`
  / `pso` / `ga` single-objective; `nsga2` / `nsga3` / `moea_d` multi-objective);
  `random` / `grid` baselines. LHS **or** cohort seeding. Zero new dependency
  beyond Optuna's removal + pymoo.
- **Runtime** — a runner-hosted async loop per running study: seed → ask →
  constraint-filter → submit batch → await → collect → tell → persist trace /
  best / state → stop (`max_runs` / target / wall-clock / convergence; multi-obj
  hypervolume). Crash-resumable (rehydrate the durable envelope + idempotent
  re-tell). Target-agnostic runner seam: `ToolJobRunner` (a tool `Job` per
  candidate), `SurrogateRunner` (in-process predict against an attached model,
  no jobs), `WorkflowRunRunner` (a headless `WorkflowRun` per candidate, §3.4).
- **Headless workflow orchestration** (the workflow-target prerequisite) —
  `WorkflowRun.auto_advance` + a runner post-completion hook
  (`WorkflowRunService.advance`) self-cascade a run: queue each member as its
  wired upstreams complete, transferring upstream outputs into downstream inputs
  server-side (migration 0067). Manual UI runs are unchanged.
- **MOP / CoP** (§5.7) — auto-best-metamodel by cross-validated Q²; per-factor
  Coefficient of Prognosis (Sobol total-effect × Q²); low-CoP factors frozen out
  of the search space (faster convergence). A `bo`-only flag; tool-target only.
- **Start-approval gate** (§14.1) — a per-tool `optimisation_start_approval_required`
  lever routes a campaign through one directed sign-off (the `optimisation_approver`
  capability + an `optimisation_study` approval target, migration 0070) before
  hundreds of children queue; the cost estimate rides in the request.
- **Surfaces** — a feature-gated (`OPTIMISATION_FRAMEWORK`) NiceGUI workspace +
  create wizard (target → factors → responses → strategy + acquisition + the
  Navigator's "Recommended" badge → seed/budget → launch) + live detail
  (convergence trace, current best / Pareto, Halt / Archive / Publish-surrogate,
  the MOP CoP chart, the pending-approval panel). Lineage resolves
  `optimisation_study` as a provenance root; Live Jobs groups its in-flight
  children as one campaign item.
- **Deferred** (later slices): the scipy local/annealing strategies
  (`nelder_mead` / `slsqp` / `dual_annealing`, surrogate-mode polish); run-level
  `study_result` for the workflow target (results-explorer browse + workflow
  surrogate publish); `surrogate_assisted_real` run mode (subsumed by real-mode
  `bo`, which is surrogate-assisted by construction).
- **Integer parameter handling — Phases A–D** (shipped 2026-06-15; ADR-0009
  amendment, `designs/integer-parameter-handling.md`). **(A)** Every automated
  sampler — the parametric generator, the LHS/cohort seed, and all optimisation
  strategies (`bo` / pymoo EAs / grid / random) — produces integer-typed values
  through one helper, `matofx.studies._intsnap.snap_int` (**round half away from
  zero**, then `ceil(lo)`/`floor(hi)` clamp), replacing a scattered mix of `round`
  (banker's), `floor`, and `int()`-truncation. An integer factor's `min`/`max`
  must be whole numbers — `_validate_numeric_factor` rejects a fractional bound at
  config time. Integer step is fixed at 1 (a fixed value set is modelled as a
  `select` factor). **(B)** The optimisation runtime drops proposals whose design
  repeats one already evaluated (rounding collisions waste job budget), runs the
  unique remainder rather than re-asking (which would desync pymoo's ask/tell),
  and stops a fully-explored finite space with a new `space_exhausted` reason;
  the dedupe set is seeded from recovered + seed evals for resume. **(C)** The
  pymoo EAs keep their population on the lattice via a `Repair` operator
  (`_make_lattice_repair`) — not native mixed variables, which the continuous
  algorithms can't take; CMA-ES rejects `repair` so stays relax+snap; the `bo`
  pool was already lattice-sampled and now returns top-*distinct* designs. **(D)**
  Both the optimisation create-wizard factor builders (tool + workflow) **and** the
  parametric-studies form (`ui/pages/studies.py` — grid range/by-values,
  screening/RSM, space-filling uniform + log-uniform bounds, run-table cells, and
  held constants) step integer min/max by 1 with no decimals and snap on collect
  (`_int_number_kw` / `_snap_int_value` / `_dedupe_numeric`), so a fraction can't
  reach the validator/generator. The parametric form previously let an integer
  input take a fractional bound (rounded silently downstream) — that gap is closed.

### 16.28 Optimisation results explorer + hardening (shipped 2026-06-09)

Wiring + UX/robustness pass over §16.27 after the first live runs. All UI is
feature-gated by `OPTIMISATION_FRAMEWORK` and built/tested against
`OUTPUTS/matofx/.venv` (the env with `pymoo`/`ruff` — *not* `venv_MATOFX`, which
lacks them; see `feedback_use_dotvenv_for_tests`).

- **Dispatch + access fix** — `_job_project_id` (`hierarchy/service.py`) gained
  the optimisation home (it only knew `scenario` / `parametric_study`), so an
  optimisation child job's `queue_for_runner` no longer 404s and the runner can
  actually run campaigns. The runner's `TargetRegistry` is still local-only
  out-of-box (AOS/HPC unwired — `project_poc_windows_mshpc`).
- **Per-run results API** — `GET /api/optimisation-studies/{id}/run-results`
  (`OptimisationService.run_results` → `OptimisationRunResult{Row,sResponse}`):
  one row per child job `{job_id, batch, state, inputs, scalars}` (Job ⟕
  `study_result` on `optimisation_study_id`). Objective/constraint/feasibility/
  Pareto are derived UI-side from the study config.
- **Results explorer** (`ui/pages/optimisation.py`, `_render_live` split into a
  polled lightweight zone — setup/outcome summary + convergence — and a
  render-once interactive zone with a manual **Refresh results** + "N new runs"
  hint, so the 3 s poll never resets the user's view; per
  `feedback_studies_results_tab_no_teardown`): per-run table (objective + factor
  columns, feasible/Pareto badges, CSV export, row-click → run); **Best designs**
  table (Pareto front for true multi-objective, ranked-by-weighted-score for
  weighted-sum/single) with **live per-objective weight inputs** that re-rank
  instantly; **design-space scatter** with X/Y/**Z (3D)** + independent **Colour**
  pickers (objective or factor), batch colouring, infeasible markers, Pareto
  rings on any axes, 0–1 feasibility colour bar, rich hover; convergence plot
  labelled per mode (hypervolume / weighted objective / best). **Labels + units +
  4-sig-fig rounding everywhere** (`feedback_use_labels_not_names`); the seed
  batch reads "seed".
- **Objective weights** — optional per-objective `weight` in `config.responses`
  (validated > 0). A **single-objective strategy with ≥2 objectives is a
  weighted-sum search** (`config.is_weighted_sum` / `weighted_sum_value` =
  `Σ wᵢ·signᵢ·fᵢ`): `_validate_strategy_shape` allows it, `build_context` hands
  the strategy one synthetic objective, and the runtime scalarises each run in
  `_with_output_feasibility` while `study_result` keeps the real outputs (so the
  explorer still shows real objectives). Weight entry on the wizard Responses
  step (tool + workflow). For true Pareto, weights drive the display preference
  ranking, not the search.
- **Sensitivity (MOP/CoP) on demand** — the card shows for any `bo`+tool study
  (gate moved from the `mop.enabled` flag to `strategy=='bo'`); `compute_mop` no
  longer requires `enabled`. **Freezing fixed**: `_apply_mop_reduction` applies
  frozen factors whenever set (not only when `enabled`), and the runtime
  re-reads the frozen set **each batch** (`_frozen_values_now`, frozen-wins
  merge) so a freeze/unfreeze takes effect on the next batch.
- **Runaway protection** — new admin runner setting `local_job_walltime_minutes`
  (Admin → Paths → Runner knobs; 0 = unlimited; a tool descriptor `walltime_minutes`
  overrides); the heartbeat poll kills + fails a local job past the limit
  (`supervisor._enforce_walltime`). A killed eval is skipped by the optimiser
  (`_tell` only feeds points with every objective) so the study advances. The
  `bo` candidate-pool is now the per-study `surrogate_candidate_pool` setting
  (16–1e6) instead of a hard-coded 512.
- **Navigation + surfaces** — study-homed jobs (no scenario) render their result
  **standalone** at `/jobs/{id}` (scenario jobs still defer to `/my-jobs`), with
  a left meta column of **Project + Optimisation-study cards** (`JobResponse`
  gained `optimisation_study_id` / `parametric_study_id`). A sortable
  **Optimisation studies table** on `/optimisation`; a Home **Optimisation
  Studies** card (and the parametric Home card switched to the tool label). The
  lineage Kind picker dropped ADR suffixes from its labels.

### 16.29 Auto-generated results summaries (shipped 2026-06-11)

A deterministic, air-gap-safe **templated narrative** for every terminal result —
single jobs, workflow runs, parametric studies, optimisation studies — plus an
optional, governed **LLM enrichment** that re-proses the *same audited facts*
behind a fail-closed guard stack. Feature row (no ADR); design
`OUTPUTS/designs/auto-summaries.md`. Built/tested against `OUTPUTS/matofx/.venv`.

- **`matofx.summaries`** — `facts.py` defines the `SummaryFacts` grounding
  contract (closed `SURFACE_KINDS`, headline numbers, JSON-serialisable `details`,
  SHA-256 `facts_hash`); `collectors/` composes each surface's *existing* services
  into facts with no new queries (`collect_job_facts` via the ADR-0012 KPI view;
  `collect_workflow_facts` via `WorkflowRunService` + `compute_state`;
  `collect_parametric_facts` reusing `studies.analysis` for a **method-aware**
  `analysis` block; `collect_optimisation_facts` off `config_json` /
  `current_best_json` / `convergence_trace` / `mop_state_json`);
  `collectors/_descriptor.py` resolves names → labels/units
  (`feedback_use_labels_not_names`); `collectors/_kpi.py` is the shared headline-KPI
  loader (job + workflow-final-step). `narrative.py` holds the
  pure per-surface generators (`render_job/workflow/parametric/optimisation`) — the
  parametric generator dispatches on `analysis.kind` (`sobol_global` /
  `response_surface` / `screening_effects` / `anova` / `src_regression` / `pareto`
  / `none`) so prose depth scales with the DOE method.
- **Summary depth (insight, not just setup)** — the collectors mine the
  engineering-meaningful highlights, not bare counts: a **job/workflow** surfaces
  the headline KPIs (a workflow reports its **final completed step's** results); a
  **parametric** study names the best/worst **design coordinates** for each
  response extreme; an **optimisation** study reports the **best design** (objective
  value + each constraint's value with a *binding-vs-comfortable* margin call + the
  design inputs), the **Pareto spread** per objective for multi-objective runs, the
  **CoP sensitivity** drivers + metamodel Q² from `mop_state_json`, and how far the
  search **moved** across batches. Two correctness fixes landed here:
  `current_best_json` is keyed by **response name** (the best objective value is
  `best[objective_name]`, not a literal `"objective"` key), and output constraints
  read `op`/`value` (`<=`/`>=`/…), not `operator`/`limit` — earlier drafts silently
  dropped both.
- **Method-aware parametric analysis** — mode → kind: `ccd`/`box_behnken` →
  `fit_rsm` response surface (R² + stationary point); `plackett_burman`/
  `frac_factorial` → `main_effects` screening effects; `grid` → `anova_contributions`;
  else `compute_sensitivity` |SRC| regression; degrades to `none`.
- **Cache + service** — `ResultSummary` (`orm.py`, migration `0071`) caches the
  rendered narrative per result keyed by `facts_hash` (derived data, rebuildable,
  no FK on the polymorphic `surface_id`). `SummaryService.build` renders the
  templated baseline via the per-kind generator, caches it, and — when an
  `EnrichmentContext` is supplied — attempts enrichment, storing both texts on the
  one row (the per-user opt-out selects which to render). Concurrent inserts
  resolve via a savepoint to the winner's identical row.
- **`matofx.llm`** — one `LlmClient` protocol; `make_llm_client(settings.llm)`
  returns `None` when `provider="disabled"` (default). Adapters: OpenAI-compatible
  + Azure OpenAI + Ollama over the existing `httpx`; AWS Bedrock via a
  lazily-imported `boto3` (`matofx[bedrock]` optional extra) run off the loop with
  `asyncio.to_thread`. Every transport/decode failure maps to an `LlmError`.
- **Grounding + guard stack** (`summaries/grounding.py`, kept in `summaries` not
  `llm` to avoid an import cycle) — `assemble_prompt` hands the model the facts
  (as *data*) + the templated draft and forbids derived figures; `run_guards`
  re-checks the output with deterministic **fail-closed** guards (numeric incl.
  derived-number rejection over the draft's ∪ facts' number set; comparative/
  baseline; outcome-polarity; format) and the first failure → templated fallback,
  no auto-repair. The `SummaryService` enrichment branch is **never-raise**;
  failures audit `summary.enrichment_failed`, successes `summary.generated`.
- **Config + governance** — `[llm]` `LlmSettings` block (secrets in TOML, provider
  `disabled` default, `temperature` 0.0); the `llm_summaries_enabled` General
  Setting (Data-governance, super-user, default off) is the runtime master switch;
  per-user `User.summary_enrichment_enabled` opt-out (migration `0072`, on
  `/auth/me` + `POST /auth/me/summary-preferences` + Account toggle). The
  install-level gate (`enrichment_context_if_enabled`) needs provider-wired **and**
  setting-on; the viewer's opt-out only selects the rendered text.
- **Surfaces** — `GET /api/summaries/{kind}/{id}` (per-surface VIEW ACL, 404
  collapses unknown/not-visible/non-terminal); a shared `ui/_summary_card.py`
  Summary card on the job / workflow-run / parametric / optimisation pages (renders
  only for terminal results, "AI-assisted summary" tag when enriched); a Summary
  block on the ADR-0004 per-job HTML report (`ReportContext.summary`). Help docs in
  `docs/{public,user,admin}` updated per slice.

### 16.30 Job Cohorts — workspace-level, version-banded, live-or-fixed (shipped 2026-06-11)

A rework of §16.26's "Run Cohorts" into **Job Cohorts**: renamed everywhere
(incl. the DB table), promoted from a project-homed saved query to a
**workspace-level object** keyed on a tool + its tolerated `major.minor` version
bands, with explicit-selection *or* auto-grow membership, a build-time preview
across the whole workspace, and an in-place editor. Design
`OUTPUTS/designs/job-cohorts.md`; still behind `features.surrogate_family`.
Built/tested against `OUTPUTS/matofx/.venv`.

- **Rename + DB** (migration `0073`) — `run_cohort` → `job_cohort` (table + indexes;
  `pk_/fk_/ck_` constraint names preserved per the `0058` precedent), `RunCohort`
  → `JobCohort`, lineage `NodeKind.RUN_COHORT` → `JOB_COHORT` (still reserved/empty),
  nav + page + help strings. Route `/cohorts` unchanged.
- **Cross-source fix** — `_candidate_rows` (was `_select_candidates`) resolves a job's
  project across **all three homes** via `COALESCE(scenario, parametric_study,
  optimisation_study)` and a `CASE` source label. Workflow-step jobs were already
  captured (they home under `scenario_id`); the actual gap was **optimisation-study**
  jobs (`scenario_id`/`parametric_study_id` NULL) — now joined. `ck_job_exactly_one_home`
  is 1-of-3, not 4 (an earlier audit was wrong).
- **Workspace-level + governance** (migrations `0074`/`0075`) — the home `project_id`
  is dropped (`0075`, backfilling `project_ids = [project_id]` first to preserve
  scope); `project_ids` survives as an optional positive project *filter* (`NULL` =
  whole workspace). New `Permission.COHORTS_CURATE` (`cohorts.curate`) seeded onto a
  built-in **`cohort_curator`** capability role + administrator (`0074`, the
  `model_curator` seed shape). Service **writes** (create/edit/archive/preview) gate on
  `cohorts.curate` → `AccessDeniedError`; **reads** (get/`list_all`/resolve) are open to
  any authenticated caller; the per-project **result-ACL intersect** is retained, so an
  open read never exposes runs from projects the caller can't see (D7).
- **Membership model** (migration `0074`) — `auto_grow` BOOL + `member_job_ids` /
  `excluded_job_ids` JSON. `resolve_members` branches: a **live** cohort runs the
  workspace-wide §3.1 query and drops `excluded_job_ids`; a **fixed** cohort returns its
  frozen `member_job_ids` (validated at save against the live candidate universe —
  completed · tool · tolerated version · visible). The candidate query is parameterised
  (`_CandidateFilter`) and shared by `resolve_members` and `preview_candidates`.
  **Switching a cohort off auto-grow (fixed 2026-09-21).** The builder demanded an
  explicit tick set: turning the switch off and saving answered *"Preview and tick at
  least one run, or turn auto-grow on."* — a demand that arrived only **after** Save,
  named rows, and so read as though ticking a row were what got disabled (the switch is
  cohort-wide). Worse, it was a permanent dead end for a cohort matching nothing:
  `_preview` sets `self.table = None` and returns *before* building the table when there
  are no rows, so there was nothing to tick and every save was refused, forever. Now
  "off" means "keep what you have" — with nothing ticked, `_submit` calls
  `_current_match_ids` (the same preview query) and freezes **everything the query
  currently matches**; ticking narrows that set rather than being a precondition. The
  switch runs the preview on change, so the list you are about to freeze is on screen
  before you commit, and `_refresh_grow_hint` keeps a plain-language sentence under it
  true to the switch's position. A zero-match cohort still cannot go fixed (the service's
  `CohortConfigError` stands — a fixed cohort with nothing in it is meaningless), but the
  builder says so the moment the switch moves and names **Archive** as the way out
  instead of stranding the curator. `_current_match_ids` warns when `acl_hidden > 0`:
  a live cohort resolves per reader, a frozen one is fixed by whoever froze it, so runs
  the curator cannot see would otherwise be dropped silently.
  **Membership chip** — `membership_badge` (extracted for test) now has three states, not
  two. A non-growing cohort with `auto_include_study_ids` still unions in those studies'
  completed runs at resolve time (ADR-0020 §11.5 / migration `0096`), so a bare
  "Fixed set" was a promise the resolver does not keep; it reads **"Fixed +
  commissioned"** with a tooltip saying those runs still arrive. The detail page already
  explained this; the list row flattened it away. Covered by
  `tests/ui/test_cohort_membership_switch.py` (13 tests).
- **Version bands + future-patch tolerance** (migration `0076`) — a cohort stores
  chosen `major.minor` bands in `tolerated_version_bands`; live membership **re-expands**
  them to the *current* matching `tool_version.id`s on every read
  (`_effective_version_ids` → `_expand_bands`), so a patch installed *after* creation
  auto-joins. `tolerated_tool_version_ids` is kept as a last-known snapshot (display +
  fixed-cohort validation + the band-less legacy path). `_major_minor` folds patches;
  no backfill (existing cohorts stay on the id path until next edited).
- **Builder + preview** — curate-gated `GET /api/cohorts/tools/{alias}/build-meta`
  returns `version_bands` (grouped, newest-first) + numeric `numeric_inputs` (label,
  not name — `feedback_use_labels_not_names`). `POST /api/cohorts/preview` runs the
  workspace-wide query without saving, returning candidate rows (run · project · source ·
  completed · inputs) + an `acl_hidden` count. The NiceGUI `_CohortBuilder` (one class
  driving **create and edit**) does tool → version-band multi-select → advanced filters
  (per-numeric-input min/max ranges + completed-on window) → **Preview** (selectable
  table) → live/fixed toggle → save. The detail page has an **Edit** button reopening the
  builder in edit mode (tool fixed, pre-filled).
- **API** (`api/routers/cohorts.py`) — moved list/create from `/api/projects/{id}/cohorts`
  to workspace-level `/api/cohorts`; added `/cohorts/preview` + `/cohorts/tools/{alias}/build-meta`;
  `PATCH /api/cohorts/{id}` now edits membership/bands/filters. Schemas dropped `project_id`,
  added `auto_grow` / `member_job_ids` / `excluded_job_ids` / `tolerated_version_bands` +
  the preview/build-meta shapes.
- **Tests** — `tests/studies/test_cohort_service.py` (governance, live/fixed, exclusions,
  optimisation-source, preview, version-band grouping, numeric inputs, future-patch
  tolerance, auto-grow flips) + `tests/api/test_cohorts.py` (workspace routes, build-meta,
  band create, edit). Repaired a pre-existing stale `test_built_in_roles_are_seeded`
  (it predated `vv_curator`/`model_curator`/`optimisation_approver`).

**Migrations shipped** — `0073` (rename), `0074` (membership cols + `cohort_curator`),
`0075` (drop home `project_id`), `0076` (`tolerated_version_bands`). Linear head `0076`.

**Key files** — `matofx/studies/cohort/{orm,service,errors,__init__}.py`;
`users/permissions.py` (`COHORTS_CURATE`); `api/routers/cohorts.py` + `api/schemas.py`;
`ui/pages/cohorts.py` + `ui/api_client.py`; `db/migrations/versions/0073`–`0076`;
lineage kind rename across `lineage/{types,service,svg_render}.py` + `ui/lineage_view.py`.

**Display + results-keying follow-up (2026-06-25, no migration):**

- **Catalogue / detail display fields** — `CohortResponse` gained `tool_name`
  (the tool's display label, [[feedback_use_labels_not_names]]), `created_by_name`
  (creator), and `member_count` (the cohort's live, ACL-intersected membership size
  *for the caller*). The list + get endpoints enrich via `_enrich_cohorts` (one
  `Tool.name` / `User.full_name` batch lookup + a per-cohort `resolve_members`). The
  catalogue row and the Definition card now read by label, never alias.
- **Results matrix de-duplication** — the cohort scalar store is now normalised to
  the **cohort tool's bare output names**. A cohort can mix run sources, and those
  key `study_result.scalars` differently: parametric / standalone runs use the bare
  output name, while an optimisation *workflow* run aggregates members keyed
  `<member_tool>.<name>` (`runtime._record_run_result`). Left as-is the same output
  showed twice — and a surrogate fit saw two columns for one variable.
  `_normalise_cohort_scalars` strips the cohort-tool prefix, drops other members'
  outputs (a different tool's response), and merges. `populate_study_results` writes
  new rows bare **and re-keys legacy rows in place** on the next read, so stored
  scalars (what the surrogate fit consumes) are clean — not just the display.
- **Results columns + job links** — `CohortResultsResponse` gained `columns`
  (`key` / `label` / `unit`, descriptor order) so the matrix renders
  `"Heat Dissipation (W)"` headers; the UI links each run number to `/my-jobs?job=`.
- **UI chrome** — detail page reworked: a back-arrow "Back to cohorts" link above the
  title (clear of the fixed top-right theme/help cluster), Edit / Archive promoted to
  the top, label-led catalogue rows.


**Optimisation + Surrogate-Models catalogue parity (2026-06-25, no migration):**

Applied the Parametric-Studies catalogue/detail treatment to the other two
Studies-group pages. The app-wide pieces (AG-Grid/Quasar-table theming, de-shadow,
live Plotly re-theme on toggle) are in `_chrome`, so both pages inherit them for free.

- **Schema enrichment** — `OptimisationStudyResponse` gained `tool_name` /
  `workflow_name` / `project_name` / `created_by_name` / `tool_version` /
  `workflow_version`; `ModelResponse` gained `tool_name` / `project_name` /
  `created_by_name`. Filled by `_enrich_studies` / `_enrich_models` (batched
  Tool / Workflow / Project / User / version lookups) on the list + get read paths.
- **Optimisation catalogue** — converted from a sortable AG-grid-ish `ui.table`
  to the Parametric-Studies **row-list**: search (name / target / project / owner),
  a status filter (All · Draft · Running ⊇ pending-approvals · Complete · Cancelled
  ⊇ failed), target **label** + project + owner per row, and the State as a
  coloured chip (it was a plain cell).
- **Models catalogue** — now shows the tool **label** (was the alias) + project +
  owner; added a search box + a status filter (Active / Pending / Retired) alongside
  the existing scope filter (server-side; retired pulled in so the filter can reach
  them).
- **Detail headers** — both pages' "Back" buttons moved off the top-right (clash
  with the fixed theme/help cluster) to a left-aligned back-arrow above the title,
  and gained a subtitle caption (Optimisation: target label · pinned version · kind;
  Models: tool label · surrogate model).
- **Chips** — Optimisation already used vivid palette colours; Models too. No change.
- Tests: enrichment assertions in `test_optimisation.py` + `test_models.py`.


### 16.31 Security audit remediation (shipped 2026-06-11)

Remediation of the 2026-06-10 internal security audit (no Critical/High; 3 Medium + 5 Low). No new tables/migrations — all defensive hardening over existing surfaces. Tracker: memory `project-v2-security-audit-2026-06-10`.

**Mediums (all fixed):**

- **Preview-endpoint horizontal file access** — `GET /api/jobs/{id}/file-preview` and `/cad-preview` authorised only the URL's draft `job_id` while serving any `path` under the shared `drafts/`/`jobs/` mount, so a forged `path` read another user's files. New `_resolve_preview_path` (`api/routers/jobs.py`) resolves the path under the mount, confirms the `drafts/`/`jobs/` namespace, and parses the **owning** `job_<id>` segment; both endpoints now `JobService.get` on **both** the URL draft id and the owning job (existence-hidden on no access), so the legitimate "pick a file from a previous job" preview still works while cross-user reads are refused. (`..` is collapsed by `resolve()` before parsing.)
- **Stored XSS via un-sandboxed `srcdoc` iframes** — tool-produced HTML was embedded in the app origin. Added `sandbox="allow-scripts"` (deliberately **no** `allow-same-origin`) to all four embeds (`ui/pages/job.py`, `ui/pages/compare/{render,overlay,report}.py`) — script-driven plots still render but in an opaque origin with no access to the session cookie / DOM / storage. The downloadable ADR-0004 report carries the attribute too.
- **Upload memory-exhaustion DoS** — `await UploadFile.read()` buffered the whole body before any size check. New `api/_uploads.py:read_upload_capped` does an early `Content-Length` reject plus a bounded chunked read (never holds more than cap + one chunk), wired into the draft upload, the General-Settings image upload, and the migrate import (which now enforces the previously-unenforced `tools.max_upload_bytes` 256 MiB cap). 413 on breach.

**Lows 4/5/6 (fixed):**

- **(4) Login user-enumeration via 403-vs-401** — `UserService.authenticate` now verifies the password **before** the `is_active` check, so a wrong password against a disabled account collapses to `InvalidCredentialsError` (401) like any other failure; `AccountDisabledError` (403) is reachable only with the correct password. `SessionService.validate` is unchanged (already-authenticated context).
- **(5) No IP-level login rate limit** — new `matofx/users/login_throttle.py:IpLoginThrottle`: a sliding-window per-IP **failed**-login counter that blocks an IP for a cooldown once tripped, independent of the targeted username (closes the spray-across-usernames gap that per-account lockout misses). `TooManyLoginAttemptsError` → **429**. Lives on `app.state.login_throttle` (built eagerly in `make_app`), read via `get_login_throttle`, enforced in the login endpoint; `_client_ip` uses `request.client.host` (honours uvicorn `--forwarded-allow-ips`, never trusts raw `X-Forwarded-For`). In-process state — the daemon is single-worker; per-account lockout remains the durable cross-process guarantee. Tunable via `[auth].login_ip_rate_limit_*` (defaults 30 failures / 15 min window / 15 min block).
- **(6) Ephemeral UI session secret + unpinned cookie flags** — NiceGUI's `app.storage.user` cookie (which carries the bearer token) was signed with a per-process random secret, logging everyone out on restart and breaking multi-worker. `[auth].ui_session_secret` (`SecretStr`) now signs it; `mount_ui(settings=...)` threads it through and passes `session_middleware_kwargs={https_only, same_site}` from `[auth].ui_session_cookie_secure` / `ui_session_cookie_same_site` (HttpOnly is always on in Starlette's `SessionMiddleware`). An unset secret falls back to ephemeral **with a logged warning**.

**Remaining Lows (not code fixes):** (7) `joblib`/pickle bundle loads — already mitigated by the DB-anchored SHA-256 pre-load check; standing rule is to keep surrogate bundles out of migrate/import paths. (8) demo page's external iframe (`ui/pages/demo.py`) — confirm gated/stripped in production.

**Config additions** — `AuthSettings` gains `login_ip_rate_limit_{max_failures,window_minutes,block_minutes}`, `ui_session_secret`, `ui_session_cookie_{secure,same_site}`; documented in `etc/matofx.example.toml`'s new `[auth]` block.

**Key files** — `api/_uploads.py` (new); `api/routers/jobs.py` (`_resolve_preview_path`); `ui/pages/job.py` + `ui/pages/compare/{render,overlay,report}.py` (iframe sandbox); `users/service.py` (auth reorder); `users/login_throttle.py` (new) + `users/errors.py` (`TooManyLoginAttemptsError`); `api/{app,deps,errors}.py` + `api/routers/auth.py` (throttle wiring); `ui/app.py` + `config/models.py` (session secret/cookie). Tests: `tests/api/test_uploads.py`, `tests/users/test_login_throttle.py`, additions to `tests/api/test_jobs.py` / `test_auth.py` / `tests/users/test_user_service.py` / `tests/ui/test_mount.py`. Help docs: public `10-signing-in.md`, admin `50-moving-tools-workflows.md`.

### 16.32 Execution scheduling — local / GPU / HPC admission lanes, Part A (shipped 2026-06-12)

Rework of the runner pickup loop's job-admission logic (`designs/execution-scheduling.md`, Part A). Replaces the single `available = local_concurrency - len(running)` counter — which over-counted AOS/HPC jobs against the local cap, had no GPU arbitration, and treated a free slot as free capacity — with **three admission lanes** classified off two cheap `tool` columns (`execution_target`, `requires_gpu`), no descriptor-JSON parse. **No new tables.** Migration `0077` only seeds runner-setting rows.

**Lanes** (`RunnerSupervisor._admit_queued` / `_admit_local` / `_build_lane_state` in `runner/supervisor.py`):

- **Local CPU** — counted against `local_concurrency` *and* a host CPU/RAM budget: a job starts only when the unused budget covers its descriptor `resource_request.cores` / `memory_mb`, not merely a free slot.
- **Local GPU** (`requires_gpu` + local) — additionally counted against `gpu_concurrency` (default 1, strict one-at-a-time so GPU jobs don't fight over device memory); also consumes a local slot, so `local_concurrency=2, gpu_concurrency=1` runs at most 1 GPU + 1 CPU, never 2 GPU.
- **AOS / HPC** — exempt from the local lanes (the remote host / cluster scheduler owns its own queue); handed off unconditionally. This fixes the P4 over-count (running HPC jobs no longer starve local work). *In Part A no AOS/HPC target is registered, so such jobs still fail at pickup — the change is only that they no longer burn a local slot first; live dispatch is Part B.*

**Resource budget** (`runner/hostcaps.py`, new): host CPU/RAM detected once via `psutil` (BSD-3, new dep) and cached for the process lifetime; `resolve_local_budget` applies the admin settings over it. A request larger than the whole budget (can never fit) is **failed at pickup** with a plain-English reason rather than wedged forever. Lane-full is a *skip*, not a stop — a different-lane job queued behind it still gets a look the same tick. Existing ordering (non-study → skip-queue priority → FIFO) and the per-study concurrency cap are unchanged; **skip-queue still only reorders, never bypasses a lane cap or the budget** (`feedback_priority_flag_not_bypass`).

**Settings** (super-user amendable, hot-reloaded, `RUNTIME_SETTINGS_CATALOG` domain `runner`, seeded by `0077`): new `gpu_concurrency` (1), `local_cpu_cores` (0 = auto-detect), `local_memory_mb` (0 = auto-detect), `local_memory_reserve_mb` (2048, reserve held back from the auto memory budget). Editable via the existing `runtime_settings.manage` surface.

**Key files** — `runner/hostcaps.py` (new); `runner/settings.py` (catalog + `RuntimeSettings` fields + loader); `runner/supervisor.py` (`_LaneState`, `_admit_queued`, `_admit_local`, `_build_lane_state`, `_load_tools_by_alias`); migration `0077`; `pyproject.toml` (psutil dep + mypy override). Tests: `tests/runner/test_hostcaps.py` (new) + lane/budget cases in `tests/runner/test_supervisor.py` + `tests/runner/test_settings.py`. Help docs: admin `10-installing-and-setup.md` ("How MATOFX decides when to start a job").

### 16.33 Execution layer Part B — AOS/HPC dispatch, secrets store, per-host gating, host registry (shipped 2026-06-12)

Part B of `designs/execution-scheduling.md` — makes AOS / HPC jobs actually dispatch (Part A only built the scheduler lanes; the backends were written but unregistered) and adds the registration + credential surfaces to use them.

**Encrypted secrets store** (`matofx/secrets/encrypted_file.py`, new). Every AOS-host / HPC-cluster row carries an opaque `secret_ref`; the credential itself lives in a Fernet-encrypted file (`cryptography`), keyed by a master key in the new `[secrets]` config block (`backend` = `memory` | `encrypted_file`, `file_path`, `master_key: SecretStr`). `make_secrets_store(settings.secrets)` returns the configured `SecretsStore`. The file at rest reveals only ref names (non-secret by design); wrong key / tamper fails closed (`SecretsDecryptError`). New `matofx secrets {init|set|get|list|remove}` CLI (`cli/main.py`) — `init` generates the Fernet key; operators populate each ref. **No DB** (file + config only).

**Dispatch targets** (`tools/exec/dispatch.py`, new). The registry is keyed by three kinds but each of `aos`/`hpc` has two concrete impls that reject a mismatched row, so `AosDispatchTarget` (routes by `host.transport`) and `HpcDispatchTarget` (routes by `cluster.scheduler`) sit under the single `"aos"`/`"hpc"` slot and delegate. `runner/daemon.py` builds the secrets store + both dispatchers and `registry.register("aos"/"hpc", …)`.

**AOS per-host gating** (`runner/supervisor.py` `_AosHostLane` / `_build_aos_lanes` / `_admit_aos`; migration `0078` adds `aos_host.max_concurrency` [default 1] + nullable `cpu_cores`/`memory_mb`). An AOS box has no scheduler of its own, so the runner gates it **per host**: a concurrency cap always, plus optional CPU/RAM budget when the admin sets it (a remote host can't be auto-detected like local). HPC stays fully exempt (cluster scheduler owns its queue). Built only when an AOS job is actually queued/running (no host query for local-only installs).

**Host/cluster admin CRUD** (`tools/hosts_service.py` + `api/routers/admin.py` + `ui/pages/admin_paths.py` two new tabs). `AosHostsService` / `HpcClustersService` list/create/update/activate-deactivate behind `aos_hosts.manage` / `hpc_clusters.manage`; the connection `transport` is **derived** from `os`/`scheduler` (linux→ssh, windows→winrm; slurm→ssh, mshpc→winrm) so an inconsistent pair can't be entered. Responses carry `secret_present` (does the `secret_ref` resolve in the store — computed without revealing it). The two tabs live on the existing `/admin/paths` page alongside storage locations / python envs.

**WinRM hardening** (audit S4 + B6, `tools/exec/aos_winrm.py` + `hpc_mshpc.py`): WinRM now **always uses HTTPS + certificate validation** — the old HTTP-fallback-for-non-5986-ports is gone (NTLM credentials never traverse plaintext HTTP; a self-signed cert must be trusted on the runner host, not ignored). `WinRmConnectOptions` gains a **redacted `__repr__`** so a stray log / exception-with-locals can't leak the password bytes.

**Key files** — `secrets/encrypted_file.py` + `secrets/errors.py` (new errors) + `[secrets]` in `config/models.py` + `config/loader.py` (path resolution); `cli/main.py` (`matofx secrets`); `tools/exec/dispatch.py`; `runner/daemon.py`; `tools/exec_orm.py` (`AosHost` capacity) + migration `0078`; `runner/supervisor.py` (AOS lane); `tools/hosts_service.py` + `api/{schemas,deps,routers/admin}.py` + `ui/{api_client,pages/admin_paths}.py`; `tools/exec/{aos_winrm,hpc_mshpc}.py` (S4/B6). Also `favourites/service.py` — made one `config.general_service` import lazy to harden a latent import cycle that `runner.supervisor`'s `all_orm` import can expose. Tests: `tests/secrets/test_encrypted_file.py`, `tests/tools/test_dispatch.py`, `tests/tools/test_hosts_service.py`, AOS-gating cases in `tests/runner/test_supervisor.py`, WinRM-HTTPS + repr-redaction in `tests/tools/test_aos_winrm.py`. Help docs: new admin chapter `65-remote-execution.md`. **Live MS-HPC validation against a real cluster + SMB share is operator-run** (the WinRM handshake / `Microsoft.HPC` module can't be exercised from CI).

### 16.34 Complete ORM registry — `matofx.db.all_orm` (shipped 2026-06-12)

Fixes a deployed-runner crash class: SQLAlchemy resolves **every** foreign key in the shared MetaData when it sorts tables for a flush, so a process that imports only part of the model set crashes with `NoReferencedTableError` on the first flush touching a table whose FK target was never registered. The standalone runner hit this in production on `approval_request.tool_vv_record_id` → `tool_vv_record` (`matofx.vv.orm` never imported in the runner process); the resulting crash-loop also produced knock-on `StaleDataError`s on `job` (its `version_id_col` optimistic lock). Previous instances (`parametric_study`, `workflow_run`) had been patched with one-off side-effect imports in `runner/supervisor.py`.

**`matofx/db/all_orm.py`** (new) imports every module that defines a mapped table (21 modules), so importing it registers the complete FK closure. `runner/supervisor.py` imports it at module level, replacing the accumulated one-off imports. Caveat: the supervisor is imported during `matofx.config` initialisation (config → `runner.settings` → `runner.__init__`), so an ORM package `__init__` that eagerly imports config back becomes a circular import — break the cycle in the offending package (as done in `favourites/service.py`), never by deferring the registry import. Tests (`tests/db/test_all_orm.py`): completeness guard (any module defining `__tablename__` must be listed in `all_orm`), full FK-closure resolution, a source-level guard that the supervisor keeps the import, and a fresh-interpreter subprocess test reproducing the deployed import graph. User-invisible (no help-doc change).

### 16.35 Approval-detail completeness — labelled review, bulk decide, optimisation results gate (shipped 2026-06-15)

Closes the gap that the approver inbox was built gate-by-gate and two gates were never wired in while others under-rendered (`designs/approval-detail-completeness.md`). Three workstreams over `api/routers/approvals.py` + `api/schemas.py` + `ui/pages/approvals.py`; one migration.

**A. Detail completeness.** The review panel is now snapshot-faithful, fully-resolved, and self-contained for every gate — always descriptor **labels + units**, never internal names. New structured DTOs (`ApprovalVariable` / `Objective` / `Constraint` / `Strategy` / `Stopping` / `Footprint` / `OptimisationReview`; `SurrogateReview` + `SurrogateMetric`) hang off `ApprovalDetail`. Two **previously-unwired** gates are now in the inbox `_enrich` and the detail dispatch: the **optimisation start gate** (`optimisation_study_id` — created at configure since §16.27 but rendered as "Job None" and 404 on detail) and the **surrogate-publish gate** (`surrogate_model_version_id`). The optimisation block reads the frozen `OptimisationStudy.config_json` (factors → ranges/levels, responses → objectives+sense, output constraints → "label op value unit", strategy/stopping in words) via the shared `summaries.collectors._descriptor.descriptor_maps`, plus a best-effort **compute-footprint estimate** (run-count range from `max_runs`+seed, per-run resources from the tool snapshot; core-hours left `None` — no walltime basis). The surrogate block carries model kind, predicted responses, training samples + source, validity domain, and per-response Q²/RMSE from the version `quality` dict. Parametric-study factors, previously shown by raw name, now resolve to labels+units+ranges/levels too (`_study_factor_variable`). `target_kind` literal widened to add `optimisation` / `surrogate_model`; `_can_act_on` gets an optimisation-pre branch (start-gate authority is `OPTIMISATION_APPROVE`, not the tool-approver flag).

**B. Bulk decide.** The inbox (already a batch surface over `POST /approvals/decisions`) gains **Select all shown** → Approve / Deny (one shared reason) that set every filter-visible row's decision, then the existing Submit applies the batch; bulk-approve confirms first. UI-only — `_render_row` now returns a control handle (card/radio/reason/state/visible) the page's filter + bulk actions drive.

**C. Optimisation results-release gate.** Optimisation honoured a tool's *pre* policy but **silently dropped `post`/`both`** — a real governance gap. Now the runner's `_finish` (`studies/optimisation/runtime.py`) checks the pinned tool snapshot: on a clean completion of a post/both-policy tool it routes the study to the new **`pending_results_approval`** state and opens a campaign-level post gate (`optimisation_study_id`, `kind="post"`) instead of completing. Results stay withheld (the state is non-terminal, so summary/results collectors don't surface them) until release. `OptimisationService.decide_approval` (routed from `submit_decisions`) handles both gates — pre → running/cancelled, post → complete (released) / cancelled (`stop_reason="results_rejected"`). The study + optimisation **post** panels carry a **condensed runs matrix** (`_build_run_matrix`, fed by the cheap `StudyResult` store): one row per run with varied inputs + key outputs (labels+units), failed/outlier flagging (mean ± 3σ), per-output min/max/mean aggregates, one release decision, and a click-to-expand per-run drill-in.

**Migration shipped** — `0079` widens the `ck_optimisation_study_state` CHECK to admit `pending_results_approval` (batch recreate; the constraint name is `op.f`-final, mirroring `0066`). No other schema change — the `optimisation_study_id` approval column, `kind` discriminator, and `uq_approval_request_pending_optimisation_kind` index already existed.

**Tests** — `tests/api/test_approval_review_blocks.py` (7 cases) pins the pure builders (labels/units/ranges, human strategy + budget, footprint run-range + workflow-approximate flag, study factor domains, surrogate metrics); the existing `tests/studies/test_optimisation_start_gate.py` exercises migration `0079` + the unchanged start-gate path. Help docs: `docs/admin/90-approvals.md` (richer review, bulk decide, results-release gate) + `docs/user/80-optimisation.md` (results held pending release).

**Key files** — `api/routers/approvals.py` (new `_enrich`/detail branches, `_build_optimisation_review` + `_opt_*` helpers, `_surrogate_approval_detail` + `_surrogate_metrics`, `_study_factor_variable`, `_build_run_matrix`, optimisation branch in `submit_decisions`, `_can_act_on` pre branch), `api/schemas.py` (the structured DTOs + widened `target_kind`), `ui/pages/approvals.py` (structured renderers + select-all + runs-matrix table with drill-in), `studies/optimisation/runtime.py` (`_finish` post-gate hook + `_post_gate_applies`), `studies/optimisation/service.py` (`decide_approval` + `_decide_results`), `studies/optimisation/orm.py` (`pending_results_approval` state), `db/migrations/versions/0079_optimisation_results_approval_state.py`.

**Post-ship fixes (2026-06-15).** First live use of a `both`-policy parametric study surfaced three linked bugs, all one root cause: the runner's `_open_post_approval_if_required` (`runner/supervisor.py`) opened a **per-child** post gate for *every* job, with no study/optimisation-child exclusion. Each child was held in `pending_results` behind its own gate, so (a) the approver faced N per-job decisions instead of one; (b) the parent never saw all children *terminal*, so its single rollup gate (`studies.refresh_state` / optimisation `_finish`) never opened; and (c) the study stayed `running` forever (and an optimisation outer loop would wait on non-terminal children indefinitely). Fix: `_open_post_approval_if_required` returns `None` when `job.parametric_study_id` or `job.optimisation_study_id` is set — the parent owns the single results-release gate; children complete normally so the rollup fires. Test: `tests/runner/test_supervisor.py::test_open_post_approval_skips_parametric_study_child`. Separately, the approvals inbox refreshed via `ui.navigate.to("/approvals")` from the `/approvals` page — a same-URL client-side no-op, so a decided row lingered; changed to `ui.navigate.reload()`. (Pre-existing studies already stuck in the broken per-child state aren't auto-repaired by the code change; re-viewing the study re-runs `refresh_state`, which now opens the single study gate.)

### 16.36 Unified tool/workflow visibility predicate + member cascade (shipped 2026-06-16)

Closes a leak where the "a user who can't see a tool shouldn't see it anywhere" rule was honoured for the tool catalog but not consistently for **workflows containing the tool**, nor at the **launch/submit** boundary. The tool-level rule (archived + state + team) was already correct, but its three gates were re-implemented partially in several places, and the gates drifted:

- The workflow visibility **cascade** (`workflows.service._workflow_contains_new_tool`) only hid a workflow when a member was in `new` — so a `released` workflow containing a **`test`-state** or **team-restricted** member tool leaked to users who couldn't see that member directly. The same `new`-only check was copy-pasted in `workflows.graph_service._user_can_see_workflow`.
- `WorkflowRunService.start_run` (the real launch path — it creates the child `Job` rows directly, not via `JobService.create`) had **no visibility gate at all**: only project-EDIT + archived + has-members.
- The submit/stage gate (`hierarchy.service.queue_or_hold_for_approval`) checked state + archived but **not the team gate**, so a `released` but team-restricted tool would still accept a job from a non-member who knew the alias.

**Single source of truth.** The canonical tool rule now lives once, in `tools/service.py`: a pure `_tool_visible_given(tool, *, visible_states, actor_team_names, privileged, include_archived)` fed by `_visibility_context()` (computed once per actor), wrapped by the async `can_user_see_tool(session, user_id, tool, *, include_archived=False)`. `ToolService.get` / `list_visible` and `workflows._custom_helpers.user_can_see_tool` all delegate to it. The canonical **workflow** rule is `workflows.service.can_user_see_workflow(session, *, actor_user_id, wf)` — kind-aware: **admin** workflows pass their own archived/state/team gates **and the member cascade** (every member tool visible via `can_user_see_tool`); **custom** workflows keep owner / share-recipient / `custom_workflows.inspect` scope (the cascade deliberately does *not* hide a custom workflow from its owner — losing access to a member tool leaves it visible-but-read-only, with the per-job submit gate refusing the affected tool). `WorkflowService.list_visible`, `graph_service._user_can_see_workflow`, and `start_run` all route through this one predicate; the `new`-only `_workflow_contains_new_tool` is deleted.

**Behaviour notes.** Hidden tools/workflows are **existence-hidden** (404 / "not found", never "forbidden") so their existence doesn't leak. History is unaffected — a user's own past jobs for a now-hidden tool stay visible (read-only); the submit/stage gate is what blocks a *re-run*. Latent leak fixed in passing: `WorkflowService.list_visible` (the admin-workflow chooser surface) never filtered `kind`, so it returned **every** custom workflow (they're `released` + un-teamed) to every user; it now restricts to admin-kind rows, matching the documented split (custom workflows are surfaced only by `list_my_custom_workflows`). The kind-aware `can_user_see_workflow` predicate itself still covers custom rows (owner / share-recipient / inspector) for the graph + launch paths, which must work for a user's own custom workflows.

**Tests** — `tests/workflows/test_visibility_cascade.py` (cascade hides a released workflow with a `test`/`new` member from a regular user; visible to a beta tester for a `test` member; hides on a team-restricted member; control where all members visible). **Help docs** — `docs/admin/30-adding-tools.md` (a hidden/team-restricted tool drops the whole containing workflow from a user's lists; past jobs stay read-only) + `docs/admin/40-building-workflows.md` (a released workflow is never more visible than its member tools).

**Key files** — `tools/service.py` (`_visibility_context`, `_tool_visible_given`, `can_user_see_tool`; `get`/`list_visible` refactor), `workflows/service.py` (`can_user_see_workflow`, `list_visible` rewrite, `_workflow_contains_new_tool` removed), `workflows/graph_service.py` (`_user_can_see_workflow` delegates), `workflows/run_service.py` (`start_run` visibility gate), `workflows/_custom_helpers.py` (`user_can_see_tool` delegates), `hierarchy/service.py` (`queue_or_hold_for_approval` uses `can_user_see_tool`). No migration.

### 16.37 Tool descriptor short + long description split (shipped 2026-06-16)

The single `description` field on the tool descriptor became two: a mandatory **`short_description`** (≤200 chars) and an optional **`long_description`** (≤2048). The short one is the *only* description on the home-page Quick Access card (keeps the list tidy); the long one shows above the input form on the run page (`submit.py`, `my_jobs.py`, `workflow_run.py` headers render `long_description or short_description`) and behind a new **info (ⓘ) button** on the Quick Access card — `home._show_tool_info_dialog`, rendered only when a long description exists. Workflow descriptors keep their single `description` (out of scope); the shared launch picker reads `short_description or description` to serve both.

**Storage / back-compat.** The description lives *inside* the `descriptor` JSON blob (not a column), so there's no `ALTER TABLE`. Migration `0080` rewrites `tool.descriptor` + `tool_version.descriptor_snapshot` in place (`description` → `short_description`, with text past 200 chars spilling into `long_description` and an ellipsised short). A `ToolDescriptor` `@model_validator(mode="before")` (`_back_compat_description`) applies the *same* fold so any blob the migration can't reach — old export/import bundles, pre-split version snapshots, test fixtures — still loads; `description` stays in `admin._METADATA_OWNED_FIELDS` so a re-uploaded legacy bundle's stray key is dropped rather than tripping `extra="forbid"`. Routers read raw blobs via the `tools.descriptor.tool_descriptions(dict) -> (short, long)` helper. `versioning.METADATA_FIELDS` carries both new keys for the change-log diff; the per-version `changes` audit map is left historically intact.

**Key files** — `tools/descriptor.py` (fields, `SHORT_DESCRIPTION_MAX`, `_back_compat_description`, `tool_descriptions`), `tools/versioning.py`, `api/schemas.py` (Tool/Admin tool payloads + responses), `api/routers/{tools,admin}.py`, `models/tool_synthesis.py` (surrogate short/long), `ui/pages/{home,submit,my_jobs,workflow_run,admin_tool_new,admin_tool_edit,admin_tools}.py`, migration `0080_tool_description_split.py`. **Help docs** — `docs/admin/30-adding-tools.md` (short/long authoring) + `docs/user/00-getting-started.md` (the info button).

### 16.38 Optimisation fixed inputs + workflow/results hardening (shipped 2026-06-17)

Four fixes, all on the optimisation surface; the first is a feature, the rest close gaps that left studies producing nothing or looking stuck.

**A. Held-constant "fixed inputs" (§3.5, the headline).** The search only ever submitted the *varied* factors as a candidate's job inputs, so any **required input that can't be a factor** — most importantly a `material` selection (the heat-sink → material-properties workflow needs three) — went in as `null` and every run died (`'NoneType' object is not subscriptable`). A study now carries a top-level `config.fixed_inputs` map: keyed by **input name** for a tool, **`member.input`** for a workflow. A new **Fixed inputs** wizard step (`_FixedInputsControls` in `ui/pages/optimisation.py`) renders right after *Factors* — it knows the varied set only then — listing every *required* input not chosen as a factor and demanding a value, reusing the real form widgets including the material picker (`tools/forms/render._render_material_field` + a `MaterialInputContext`). Values are held as the same primitives a normal submission stores (a material → its `material_version_id` string), so both runners just merge `{**fixed, **candidate}` at job-build time and `JobService.queue_for_runner` freezes materials exactly as for a hand-submitted job. `StudyRef.fixed_inputs` threads them to `ToolJobRunner` (`inputs={**fixed, **cand}`) and `WorkflowRunRunner` (pure helpers `_split_member_inputs` + `_candidate_open_inputs` distribute them into per-member `open_inputs`, factors winning any overlap). `config.validate_config` / `validate_workflow_config` gate launch: every required, non-varied (and, for a workflow, non-wired) input must have a non-blank held value — the workflow check matches the varied set by `(member, input)` pair, since a factor's `name` is an arbitrary key. **No migration** — `fixed_inputs` lives inside the existing `config_json` blob.

**B. Failed-member workflow hang.** `WorkflowRunRunner._await_runs_terminal` required *every* child job terminal; a failed upstream leaves its downstream stuck `created` (`advance` only promotes once upstreams are *complete*), so a batch blocked until `batch_timeout_s` and the study sat in `running` forever. It now keys off the **run-level** state via `workflows.run_service.compute_state` (`_TERMINAL_RUN_STATES` = complete/failed/cancelled) — a run with any failed member is terminal, matching how the workflow itself rolls up.

**C. Workflow label resolution.** The live/setup view built descriptor label maps only for tool targets, so a workflow study showed raw member-qualified names (`heat_transfer.T_core`). New `_workflow_label_maps` loads each member tool and builds `member.input` → "Tool · field" maps (objectives registered under both the bare output name and the member-qualified form), and the header reads "Workflow:" not "Tool:".

**D. `pending_results_approval` read as "stuck on Running".** The detail page's `_TERMINAL_STATES` (poll-stop set) omitted the §16.35 results-gate state, so a finished, gated study polled forever and never said why. New `_SETTLED_STATES = _TERMINAL_STATES | {pending_results_approval}` stops the poll, the chip colours amber, and an "awaiting results approval" banner explains the wait.

**E. Per-member post-approval gate stalled workflow campaigns.** `_open_post_approval_if_required` (`runner/supervisor.py`) skips the per-job post gate for a study child via `job.optimisation_study_id` — but a *workflow*-target candidate runs the chain as a headless `WorkflowRun`, so that id lives on the **run**, not the member job. A post/both-policy member (e.g. `heat_transfer`) therefore opened its own gate and sat in `pending_results`; the run never reached a terminal state, so the optimiser (even with fix B) collected nothing and the page stayed empty. The guard now also returns `None` when the job's `WorkflowRun.optimisation_study_id` is set. A workflow target has no campaign-level results gate either (`_post_gate_applies` is tool-only), so these members are intentionally ungated — the optimiser owns the autonomy. (Normal, non-optimisation workflow runs still gate per member as before.)

**Key files** — `studies/optimisation/runtime.py` (`StudyRef.fixed_inputs`, both runners' merge, `_split_member_inputs`/`_candidate_open_inputs`, `_await_runs_terminal` via `compute_state`), `studies/optimisation/config.py` (`_validate_fixed_inputs` + both validators), `studies/optimisation/service.py` (`update_draft` docstring), `ui/pages/optimisation.py` (`_FixedInputsControls`, Fixed-inputs step, `_workflow_label_maps`, `_SETTLED_STATES` + banner), `runner/supervisor.py` (`_open_post_approval_if_required` workflow-run guard). **Tests** — `tests/studies/test_optimisation_config.py`, `test_optimisation_workflow_config.py`, `test_optimisation_workflow_runtime.py` (terminal-state + merge helpers), `tests/ui/test_optimisation_page_helpers.py`, `tests/runner/test_supervisor.py` (workflow-member gate skip). **Help docs** — `docs/user/80-optimisation.md` (the Fixed inputs step).

### 16.39 Sum-check-aware optimisation (shipped 2026-06-18)

A tool descriptor can declare a cross-field **sum-check** (`InputField.sum_check` = `{value, sum_parameters}`, e.g. three component percentages must total 100). The job form enforces it, but the optimiser varied the members independently, so almost every candidate broke the sum and produced an invalid material / failed run (the real blocker behind the `material_properties` workflow studies). A sum is an **equality** constraint and the optimiser's reject-and-retry constraint mechanism can never satisfy an equality. The fix searches the group's **true degrees of freedom**: one member (the *balance*) is **derived**, not searched — the strategy varies the other k−1 members and the balance is worked out as `value − sum(others)`. The derived member is dropped from the search space at study load (the same way MOP variable-freezing works), so the optimiser never wastes iterations exploring the redundant direction the rule removes. Which member is derived is chosen once by the widest-range heuristic — it doesn't change *which* blends are reachable, only how cleanly they sample (deriving the widest member maximises the share of samples whose residual lands in range). *(An earlier vary-all-and-repair variant was reworked to this reduced search at the user's request — "don't keep exploring needlessly" — which also made the deferred CoP "least-impact balance" idea moot: the choice of derived member provably can't change the result, only sampling efficiency.)*

**Pure core** — new `studies/optimisation/sumcheck.py`: `derive_balance` (sets `balance = value − sum(others)`, dropping the candidate when that lands outside the balance's box or isn't integral for an int balance — clamping would silently break the sum; balance bounds are read off the group record, so no factor-spec map is needed), `apply_sum_derivation` (a batch, drops infeasible), `project_sum_groups` (single candidate, never drops — for in-strategy decode), `pick_balance_heuristic` (widest-range varied member, float over int, name tie-break), `sum_check_notes` (wizard banner text).

**Config** — `config.validate_config` / `validate_workflow_config` *derive + validate* the groups and return them; `service.configure()` freezes them into `config_json["sum_groups"]` (one record per group: `{value, members:[{factor, input_key, role:varied|fixed, fixed_value}], balance, balance_kind, balance_low, balance_high}`). Tool axis key == input name; workflow axis key == the factor's `name` (matched by `(member, input)` pair) and held members key by `member.input`. Validation rejects an infeasible group (the balance's reachable residual range can't overlap its box) and a fully-held group that doesn't already total the value. The descriptor is only read at configure (pinned via `tool_version_id`), so the runtime never reloads it.

**Runtime reduction** — `_load_runnable` runs `_apply_sum_reduction` (drops each group's `balance` factor from the search space) right after the MOP reduction, so `build_context` sees only the free members. `_seed_candidates` LHS-samples the free members then `apply_sum_derivation` fills the derived one in *before* the constraint filter; each batch re-derives after the MOP-frozen merge and **before** `_evaluate` (idempotent on already-projected strategy output, except where a frozen value shifts the residual), so the point that runs is the point told back to the strategy (told X == run X). Infeasible candidates drop into the existing empty-streak guard.

**In-strategy projection (no pymoo desync)** — `StrategyContext.sum_groups` carries the groups; `PymooStrategy._decode` and `SurrogateBOStrategy._decode` call `project_sum_groups` so a proposal already has its derived member filled in. This is load-bearing for the generational strategies: `tell` matches the told candidates against the raw `ask` (`_pending_cands`) by signature, so a point that gained its derived member only at runtime would hit `_PENALTY` for the whole generation; adding it in decode makes pending == told. The surrogate (`bo`) is fit only over the free members, which is correct since the objective is a function of them (the derived member is determined by them).

**Key files** — `studies/optimisation/sumcheck.py` (new), `config.py` (`_collect_sum_groups` + feasibility validators + balance bounds on the record; both validators return the groups), `service.py` (`configure` persists `sum_groups`), `runtime.py` (`_apply_sum_reduction`, seed + batch derivation hooks), `strategies/{base,multi,surrogate_bo}.py` (`sum_groups` on the context + decode projection), `ui/pages/optimisation.py` (Variables-step banner via `sum_check_notes`). **Tests** — `tests/studies/test_sumcheck.py`, sum-group cases in `test_optimisation_config.py` / `test_optimisation_workflow_config.py`, `test_optimisation_sum_runtime.py` (every evaluated candidate totals the value for bo/random/ga + decode-projection). **Help docs** — `docs/user/80-optimisation.md` (Variables step). **No migration** — `sum_groups` lives in `config_json`.

### 16.40 Workflow-optimisation per-run results + summary labels (shipped 2026-06-18)

A *workflow*-target optimisation ran fine but its per-run results were invisible: the convergence trace + `current_best` Pareto front showed, but the Results explorer read 0 runs and the auto-summary said "0 evaluations" with member-qualified *names* instead of labels. Two root causes — (1) `WorkflowRunRunner` wrote **no `study_result` rows** (a workflow candidate is a `WorkflowRun`, not a single job), and the run-results reader queried `Job.optimisation_study_id`, which member jobs don't carry; (2) the summary collector and UI keyed objectives by the bare output name while the runtime keys workflow responses by `member.output`.

**Per-run results.** `WorkflowRunRunner._record_run_result` (called from `_collect`) now writes one `study_result` row per run, keyed on the chain's **final member job** (a workflow candidate has no single job), with `scalars` = *every* member output flattened to `member.output` (so the explorer can plot/inspect any of them) and the candidate + batch stamped onto that anchor job's `optimisation_metadata_json` for the reader. Idempotent (skips if the row exists). `OptimisationService.run_results` gains a workflow branch: it finds rows via `(source_kind='optimisation', source_id=study.id)` (not `Job.optimisation_study_id`) and reads inputs from the anchor's `metadata.candidate`. **Member jobs are deliberately *not* stamped with `optimisation_study_id`** — that would violate the job's 1-of-3-home CHECK (they already home on the scenario via the run).

**Label / key alignment.** `ui/pages/optimisation.py:_scalar_key_for_response` now returns `member.output` for a workflow scalar response (matching the runtime `response_names`, `current_best`, and the new `study_result` scalars), so objectives resolve their values *and* their labels (via `_workflow_label_maps`, which registers both the bare and member-qualified key). The summary collector resolves workflow names through a new `summaries/collectors/_descriptor.workflow_descriptor_maps` (loads each member tool's descriptor → `member.field` → `Tool name · field label`), replacing the old "degrade to names" path.

**Key files** — `studies/optimisation/runtime.py` (`WorkflowRunRunner._record_run_result` + `_collect` signature), `studies/optimisation/service.py` (`run_results` workflow branch), `ui/pages/optimisation.py` (`_scalar_key_for_response`), `summaries/collectors/_descriptor.py` (`workflow_descriptor_maps`), `summaries/collectors/optimisation.py` (use it for workflow targets). **Tests** — `tests/studies/test_optimisation_workflow_runtime.py` (`_record_run_result` write/idempotency, `run_results` reader, `workflow_descriptor_maps` labels). **No migration** — `study_result` rows are written at runtime. *Note: studies that completed before this change have no rows and won't backfill; a fresh run is needed.*

### 16.41 Optimisation: continue a finished study + sticky explorer picks (shipped 2026-06-18)

Two general-purpose optimisation improvements.

**Continue a completed study.** A study that stops on its run budget can now be extended rather than re-created. `OptimisationService.extend_budget` raises the `max_runs` stopping criterion by N, clears `stop_reason`, and flips a *non-archived* `complete` study back to `running`; the runner's `scan` loop then picks it up and `run_study` **resumes from the persisted optimiser state** (the strategy rebuilds its archive / refits its surrogate from the told history; `fresh` is false so the seed isn't re-run), runs more batches to the new budget, and finishes again. **Hypervolume reference point now persists** in the optimiser-state envelope (`_persist_batch` writes `ref_point`, `run_study` restores it): without this a resumed multi-objective study re-anchored its reference to the now-better front, so the hypervolume *number* dropped at the resume boundary even though the Pareto front only improved — the metric is now comparable across a continue. Exposed as `POST /api/optimisation-studies/{id}/continue` (`OptimisationContinueRequest{additional_runs ≥ 1}`) → `ApiClient.continue_optimisation_study`; the live view's controls show a **+runs number field + Continue button** for a completed, non-archived study, then re-render to the live polling view. Rejected for any non-`complete` (or archived) study.

**Sticky design-space explorer picks.** The scatter's X / Y / Z / Colour selectors reset to defaults on every Refresh/poll rebuild. `_render_objective_scatter` now takes a `prefs` dict held in `_render_live`'s scope (`scatter_prefs`): picks are saved on change and restored on rebuild (validated against the current options so a stale value can't break the plot). Applies to every optimisation study, single- or multi-objective.

**Key files** — `studies/optimisation/service.py` (`extend_budget`), `api/{schemas,routers/optimisation}.py` (`OptimisationContinueRequest` + `continue` route), `ui/api_client.py` (`continue_optimisation_study`), `ui/pages/optimisation.py` (`_build_controls` Continue control, `_render_objective_scatter` `prefs` + `scatter_prefs`). **Tests** — `tests/studies/test_optimisation_service.py` (extend-budget accept/reject), `test_optimisation_runtime.py` (continue resumes + runs more, no re-seed), `tests/ui/test_optimisation_client.py` (continue refused on a draft). **No migration** — budget lives in `config_json`.

### 16.42 Home page + app-shell redesign (ADR-0016, shipped 2026-06-22)

Replaces the legacy teal-banner shell + three-column Home with the `home_v3` design (`designs/home-redesign.md`): a grouped, gated, pinnable + resizable sidebar; a slim utility header (global search · theme · help); and a Home body of action tiles + a favourites Quick Access card + an approver-only Approvals card + four My-Work tables.

**Server-side shell preferences (§9).** A single `user.ui_preferences` JSON blob (migration **0081**) joins `theme_preference` as durable per-user state that survives logout and follows the user across devices — keys `sidebar_pinned`, `sidebar_width` (clamped 188–460), `sidebar_groups_collapsed`, `quick_access_filter`. `UserService.set_ui_preferences` merges a *partial* update and drops unknown keys (`_normalise_ui_preferences`); surfaced on `/api/auth/me` and written through `POST /api/auth/me/ui-preferences` (`MeUiPreferencesUpdate`, all-optional with an at-least-one validator). The chrome mirrors the blob into `app.storage.user` for synchronous reads and writes through on change (`_persist_ui_pref`).

**Sidebar (`_chrome.py`).** Declarative `_NavGroup`/`_NavItem` model (`_NAV_GROUPS`) with per-item gates resolved at render (`_nav_item_visible`): Super-User (padlock), approver, feature-flag (`_FEATURE_FLAGS`), and — for the three curator workspaces (File library / Materials / V&V records) — the curate capability, preserving today's behaviour. The Admin group is `su_group` and floated to the bottom (a flex `ui.space()`). Pinned vs. the hover-expanding icon rail is Quasar `mini` + `mini-to-overlay`; drag-resize is a client-side handle that live-adjusts the drawer + page padding and emits the final width once (`_install_sidebar_pin_resize` → `matofx_sidebar_width` → `_on_sidebar_resized`). A footer carries the account avatar/name (→ `/account`) and a Logout control.

**Header (`_chrome.py`).** `_render_global_search` is a debounced split search (query + `in:` scope, off-feature scopes hidden) over `/api/search`; `_render_help_selector` is a Help documentation / Tutorials menu; a theme toggle persists via `/api/auth/me/theme`. The legacy bell is gone; identity + logout moved to the sidebar footer.

**Home body (`home.py`).** Greeting + a live summary line from `/api/home/summary` (each clause shown only when relevant); action tiles (Create / Studies / Job Search / My Account) wired to the existing create dialogs + routes; a single Quick Access card listing only **favourited** tools + workflows with a remembered Tools/Workflows/All filter, Launch split and Tutorials button; an approver-only Approvals card (`list_my_approvals`, Review → queue); and four My-Work tables (My Projects with **Owner** + **My role**, My Jobs, flag-gated Parametric Studies + Optimisations) rendered via a shared `_render_work_table_card` with state-pill + subtext slots.

**New endpoints.** `GET /api/home/summary` (`HomeSummary` — active jobs / running optimisations / running studies / pending approvals, ACL-filtered); `GET /api/search` (`SearchHit[]` — projects/jobs/studies/optimisations, ACL + feature gated, labels not aliases; tools/workflows/materials/files are a documented follow-up). `ProjectResponse` gains `owner_user_id` / `owner_name` / `my_role`, resolved in batch by `ProjectService.list_for_user_with_meta`. New page `/tutorials` (`ui/pages/tutorials.py`).

**Key files** — `users/orm.py` + migration `0081` + `users/service.py` (`set_ui_preferences`, `_normalise_ui_preferences`); `api/{schemas,routers/auth,routers/home,routers/projects}.py`; `hierarchy/service.py` (`ProjectWithMeta`, `list_for_user_with_meta`); `ui/{state,api_client}.py`; `ui/pages/{_chrome,home,tutorials,login}.py`; `ui/app.py` (tutorials route). **Tests** — `tests/api/test_auth.py` (ui-preferences merge/validate), `test_projects_scenarios_tools.py` (owner/my_role), `test_home_summary.py`. **Verified end-to-end** against the demo DB (prefs round-trip, summary counts, owner/role, search). **Not browser-verified** — NiceGUI's async render needs a browser pass. **Follow-up** — remove the superseded Home renderers left as dead code; per-item Approvals deep-link; tools/workflows/materials/files search scopes.

### 16.43 Tool guide images — static parameter-help pictures (shipped 2026-07-04)

Static images a tool author attaches to explain what the inputs mean (e.g. a labelled diagram showing where a measurement is taken), shown as a captioned gallery **above the input form** during parameter entry. 0..N per tool. Modelled exactly on the existing tool **icon**: bytes live in the tool's bundle directory, only references live on the descriptor, so they pin per job/study and travel with export/import for free (no DB bloat, no new table, no migration).

**Data model (`tools/descriptor.py`).** `GuideImage{filename, caption}` (frozen, extra-forbidding like every descriptor model) + `ToolDescriptor.input_guides: list[GuideImage]` (`default_factory=list`, `max_length=MAX_GUIDE_IMAGES=10`; caption `≤ GUIDE_CAPTION_MAX=200`). Additive + defaulted, so every pre-existing descriptor still validates and the wizard/metadata round-trips preserve it via the `{**existing, **overrides}` merge in `update_tool_descriptor` (`input_guides` is never in the update-partial nor the wizard payload, so a metadata or Inputs/Outputs edit leaves it alone — same mechanism that keeps `icon_filename`).

**Service (`tools/service.py`).** `ToolService.update_input_guides(edits: list[GuideImageEdit])` — a **replace-the-whole-set** operation (add / keep / reorder / delete in one call), one patch-bump + change-log snapshot, mirroring `update_icon`. Each `GuideImageEdit` is either *keep* (`existing_filename`, validated to be a current guide of this tool — no referencing an arbitrary bundle file) or *new* (`new_bytes`, written to the bundle under a **content-addressed** name `guide-<sha1_16>.<ext>` so re-uploading the same picture is idempotent). Bundle files that were guides but are no longer referenced are deleted. Helpers `_resolve_guide_edits` / `_guide_filename_for` keep it under the branch/statement caps. Allowed formats `png/jpg/jpeg/webp`.

**API.** `POST /api/admin/tools/{alias}/guide-images` (`routers/admin.py`, `tools.install`): multipart — a `manifest` JSON of the ordered `{caption, existing_filename?}` desired list plus `images` files consumed in order for the new entries. Sends as urlencoded when there are no new files (caption-only edit / reorder / clear-all). `GET /api/tools/{alias}/guide-images/{index}` (`routers/tools.py`): serves by list position from the bundle, media-type by extension, ACL via `ToolService.get` — the icon-endpoint pattern.

**UI.** Authoring: a **Parameter guide images** card on the tool editor's **Metadata tab** (`ui/pages/admin_tool_edit.py`, `_render_guides_section`) — multi-upload, per-image caption, reorder, delete, applied on the single **Save changes** click (pending dict → `_guides_payload` → `update_tool_guides`); thumbnails loaded bytes→data-URL like the icon preview. Display: `render_form(..., guide_images: list[GuideImageView])` renders a captioned thumbnail strip above the columns; a click opens a **native `<img>`** lightbox (`ui.html(..., sanitize=False)` — a bare `ui.image`/q-img with only max-width/height collapses tiny in a shrink-wrapping dialog, so the lightbox uses a raw `<img>` sized `width/height:auto` capped to the viewport). The caller (`my_jobs.py`) fetches bytes→data-URLs before any form widget exists (endpoints are Bearer-auth, so the app's data-URL convention, not `ui.image(url)`) and passes them in; wired into the main job form only so far (workflow-run / standalone-submit are the same pattern).

**Key files** — `tools/descriptor.py` (`GuideImage`, `input_guides`, `MAX_GUIDE_IMAGES`, `GUIDE_CAPTION_MAX`); `tools/service.py` (`GuideImageEdit`, `update_input_guides`); `api/routers/{admin,tools}.py`; `ui/api_client.py` (`update_tool_guides`, `get_tool_guide_image_bytes`); `ui/pages/admin_tool_edit.py` (`_render_guides_section`); `tools/forms/render.py` (`GuideImageView`, `_render_guide_gallery`, `_open_guide_lightbox`); `ui/pages/my_jobs.py`. **Docs** — `docs/admin/30-adding-tools.md`, `docs/user/20-running-a-tool.md`. **Tests** — `tests/api/test_tool_guide_images.py` (add → serve → reorder → drop → reject-invalid-reference → clear). **Not browser-verified** — the authoring interactions and gallery/lightbox need a browser pass.

### 16.44 V&V records workspace rework — browsable register + name-based authoring (shipped 2026-07-22)

The V&V workspace (ADR-0011 §8) shipped as an **id-lookup form**: both tabs stayed empty until you typed an internal `tool_version_id` / `job_id`, there was **no cross-install "all records" view** (§16.24's build note called this out explicitly — "an unscoped all-records view was not in the design"), and every surface rendered raw ids (`v#123`, `user #7`). This rework turns it into a real register.

**Backend — the missing browse.** New `VVService.list_all_records(record_kind, states, kinds, author_user_id, since, until, include_retired)` (`vv/service.py`) scans both tables (the `list_pending_for_curator` two-select pattern minus the pending predicate), returning `VVRecordRow` domain rows — raw columns only; the V&V package still never imports `ToolVersion`/`Job`/`LibraryFile` (the downstream-circular-import rule). New `GET /api/vv/records` (`routers/vv.py`) owns the cross-package **label enrichment** in batched, N+1-free helpers (`_tool_version_labels` via `Tool.name` + `ToolVersion.version`, `_job_labels`, `_user_labels`, `_evidence_labels` via `LibraryFileVersion → LibraryFile.name`), applies a resolved-label `search`, then paginates. Reads are open to any authenticated user (V&V records are global). Response schemas `VVRecordSummaryEntry` / `VVRecordListPage`. The single-record GETs (`get_tool_record` / `get_job_record`) now also populate optional `target_label` / `recorded_by_label` / `decided_by_label` / `evidence_label` (additive, default `None` — mutating responses leave them unset), so the detail pages read in names too.

**UI (`ui/pages/vv_records.py`, rewritten).** One filterable, paginated register (Type · State · Kind · Search facets) replacing the two id boxes; a curator **Review queue** tab = `/vv/pending` (who-can-act) intersected with `/vv/records?state=pending` (labels). A single shared `open_vv_create_dialog(...)` authors **by name**: pick a tool (→ `GET /api/tools/{alias}` resolves the current released version's id) or a job (tool → `search_jobs_by_inputs(alias, {})`), plus an optional evidence picker (library file+version via `list_library_files`/`get_library_file`, or URL). The dialog is **pre-fillable**, so the tool editor's V&V tab (`admin_tool_edit.py` — "Add V&V record") and the per-job V&V card (`job.py` — "Record V&V") open it with their target fixed instead of bouncing to `/vv-records`. The old duplicate admin index (`admin_vv_records.py`) now just redirects to `/vv-records`.

**Key files** — `vv/service.py` (`VVRecordRow`, `list_all_records`) + `vv/__init__.py` export; `api/routers/vv.py` (`list_records`, enrichment helpers, detail-label population); `api/schemas.py` (`VVRecordSummaryEntry`, `VVRecordListPage`, label fields on the two record responses); `ui/api_client.py` (`vv_list_records`); `ui/pages/vv_records.py` (rewrite), `admin_vv_records.py` (redirect), `job.py` + `admin_tool_edit.py` (pre-filled create). **Docs** — `docs/user/110-capabilities.md`. **Tests** — `tests/api/test_vv_records_list.py`.

**Feedback polish pass (same day).** (1) Restyled the whole workspace to the home-shell idiom — `section_card` blocks, theme CSS variables (no hardcoded greys), no-caps left-aligned tabs with a bottom border, `var(--matofx-row-bg)` rows, title-cased state chips with named Quasar colours — matching Job Cohorts. (2) **Version picker**: new non-admin `GET /api/tools/{alias}/versions` (`ToolVersionOption{tool_version_id, version, is_current}`, ACL = `tool_service.get`) + `list_tool_version_options` client method, so a tool record can target *any* version (defaulting to the current release), not just the current one. (3) **Evidence upload**: the create dialog's evidence section now offers None / Upload / Library file / URL; Upload creates a global `LibraryFile` via `create_library_file` (immutable + hashed, optional reviewer picker via `library_approvers`) and references its version — fixing a bug where the URL input, created outside its container, stacked a new empty field on every re-render. (4) **Sidebar**: "V&V records" gated on `permission=("vv.create","vv.curate")`, "V&V records inbox" gated on `vv.curate` (dropped `su=True`) and repointed to `/vv-records?tab=queue` (new `tab` query param opens the curator Review-queue tab directly; the tab itself stays curator-only). **Tests** — `tests/api/test_projects_scenarios_tools.py` (`/versions`).

**Second polish pass (same day).** (1) **New-record button moved** off the top-right (it collided with the shell's theme/help controls) onto the tabs row. (2) **Version picker** wired (see above). (3) **Multiple evidence items per record** — migration **0087** adds `evidence_json` (list of `{"kind":"library"|"url","file_version_id","url"}`) to both V&V tables; the legacy scalar `evidence_file_version_id`/`evidence_url` are kept as first-item mirrors and the read path synthesises a list from them for pre-0087 rows. `VVService` normalises the list on create/update (`_normalise_evidence`/`_apply_evidence_update`) and exposes it via `VVRecordRow.evidence_items`; the router resolves every item's label in one batched pass (`_evidence_views`, still N+1-free) and returns `evidence_items: list[VVEvidenceItem]` on the register + single-record GETs (plus `evidence_label` as a "N evidence items" summary). Create/patch schemas gained `evidence: list[VVEvidenceItemInput]` (pydantic-validated: library needs a file id, url needs a url). The create dialog's evidence section is now a **repeatable list** — "Add evidence" appends a row (External URL / Library file / Upload), each removable, mixed freely; uploads become global library files at submit. Detail pages list every item. (4) **Stale test fixed**: `test_record_tool_vv_rejects_non_curator` → `test_record_tool_vv_rejects_non_author`, now strips the default `vv.create` grant so the user genuinely has no authoring right (the pre-existing failure noted in the previous pass). **Tests** — `tests/api/test_vv_records_list.py` (+ multi-evidence, validation), `tests/vv/test_service.py` (renamed).

### 16.45 Results-warehouse filtering + shared number formatting + study/opt job project card (shipped 2026-07-22)

Three fixes prompted by feedback that the warehouse filtering was "awful", numbers over-long, and study/opt job cards missing project details.

**Warehouse filtering — made real + matched to the docs.** The user docs promised "filter by KPI value range" and "by V&V status", but the UI only wired version + completed-window, the KPI-range card was a dead placeholder, and the API had no `kpi_range` param. Now: `WarehouseFilters.kpi_ranges` + a correlated `EXISTS`-per-KPI subquery in `_build_warehouse_base_stmt`; a repeatable `kpi_range=name:min:max` query param (`_parse_kpi_ranges`, 400 on bad bounds) on the list + CSV + PDF endpoints; the V&V-status post-filter factored into `_apply_vv_status_filter` and reused by the table **and** CSV so exports match the filtered view. UI (`ui/pages/results.py`): the filter card gained real per-KPI min/max inputs (unit-labelled) + a V&V-status select (gated on `vv_tracking_enabled()`), one Apply/Reset for everything, restyled to theme vars + calendar-popup date inputs; the placeholder is gone. `list_tool_results` / `download_tool_results_{csv,pdf}` / `_build_results_export_url` all carry `kpi_ranges` + `vv_status`. **Tests** — `tests/api/test_kpi_router.py` (KPI-range narrowing by min/max/both, bad-bound 400, CSV honours the filter, bad vv_status 400).

**Shared number formatting (`ui/format.py:format_number`).** There was no shared numeric formatter — every page rolled its own and only the job page respected a descriptor's `precision`. New `format_number(value, *, precision=None)`: honours a declared descriptor precision (step → decimals), else trims to 5 significant figures; whole numbers read as integers; non-numeric passes through. **Studies results grid leak fixed** (`ui/pages/studies.py`): the JS value-formatter was dropped whenever a column had *any* non-numeric cell, leaking full-precision doubles (`3.141592653589793`); it's now attached per **cell** to every `in_`/`out_` column (the formatter already passes text through), so a stray blank no longer un-formats the rest. Optimisation already rounds via `_cell` (no leak). *Follow-up:* per-column **descriptor** precision in the study/opt result grids still needs each output's `precision` threaded into those results payloads — the shared formatter is precision-ready; the grids currently use the 5-sig-fig fallback.

**Study/optimisation job cards show the project (`ui/pages/job.py`).** The job meta-panel (rendered by `_render_body`, shared by `/jobs/{id}` **and** the My Jobs right pane) resolved the project via `scenario_id` (standalone) or `optimisation_study_id`, but had **no `parametric_study_id` branch** — so parametric-study children showed no Project card. Added the branch; note `get_study` returns `{study, children}`, so the project id is `study["study"]["project_id"]` (a first cut read the wrong level and stayed None). Optimisation branch hardened with `.get()`.

**Single-job number formatting (`ui/pages/job.py`).** The studies grid fix above was the wrong surface for the My Jobs single-job view: there, a parametric/optimisation child's varied **inputs** rendered raw because `_format_input_value` did `str(value)` for numbers. Now routes int/float through the shared `format_number` (outputs already round via `_format_number`). **Tests** — `tests/ui/test_format.py` (`format_number`), `tests/ui/test_job_outputs.py` (`test_format_input_value` covers the numeric cases).

### 16.46 Post-run cleanup — unified keep-list model (shipped 2026-07-30)

Post-run working-directory cleanup was authored through **two** descriptor fields that read as opposites but weren't: `retain_files` (a keep-list scoped to the *top level only*) and `delete_io_files` (a deny-list scoped to `inputs/`/`outputs/`). The split was confusing — the wizard even claimed the deny-list "overrides the keep-list", which was false (disjoint scopes) — and it left `outputs/` implicitly *keep-everything* while the top level was trimmed. Collapsed to a single **keep-list**.

**The model (`tools/exec/cleanup.py`).** After a successful run MATOFX keeps a generous **floor** — never listed by the author — and deletes everything else:

- `inputs/` **in full** (the submission record) — the keep sweep never descends into it; `logs/`, `heartbeat/`, `launcher.py` likewise wholesale-kept (`_PROTECTED_TOP_LEVEL`).
- `inputs/inputs.json` + `outputs/outputs.json` — the I/O contract floor (ADR-0003).
- **Every declared output.** The runner (`supervisor.py:_apply_cleanup_for_tool`) now passes *all* descriptor `type: file` outputs as `extra_protected` (generalising the earlier `csv_plot`-only + `field`-only flooring), plus each time-resolved `field_timesteps` series expanded on disk. So an author who lists nothing still never loses a promised result.

`retain_files` is now the **only** authored knob: glob patterns (working-dir-relative, e.g. `outputs/*.vtk`) for *undeclared* leftovers worth keeping. **Empty → nothing is swept** (whole working dir kept; back-compat with the old "empty retain = keep all"). **Non-empty → undeclared top-level entries and undeclared `outputs/` files that don't match a pattern and aren't floored are deleted** — the behavioural change is that the sweep now reaches into `outputs/` (which is deliberately *not* in `_PROTECTED_TOP_LEVEL`; `_sweep_outputs` keeps the dir and its floored contents, drops the rest).

**`delete_io_files` is legacy.** Kept in the model + engine (still an `inputs/`+`outputs/` deny-list, minus the floor) so descriptors authored before this model keep working; dropped from the wizard UI; new descriptors leave it empty (no-op). The surgical "keep everything but scrub one file" case is intentionally not offered in the new UI (author decision) — guidance is: don't write secrets into the working dir.

**Touchpoints.** `tools/exec/cleanup.py` (engine), `runner/supervisor.py` (widened floor), `tools/descriptor.py` (docstrings; `delete_io_files` marked legacy), `ui/pages/_descriptor_wizard.py` Advanced tab (two cards → one field + an explicit auto-floor note), `docs/admin/30-adding-tools.md` (§"Which files to keep after a run"). **Tests** — `tests/tools/test_exec_cleanup.py` (rewritten for the keep-list semantics; floor, `outputs/` sweep, `inputs/` wholesale-kept, legacy deny-list back-compat), `tests/runner/test_field_retention.py` (unchanged — field series still floored).

### 16.47 Idle / expired sessions always land on the login page (shipped 2026-08-09)

Session expiry had **one** exit path — the shell's keepalive timer noticing client-side idleness — and it only worked while a NiceGUI client was attached and the two clocks agreed. Every other way a session dies (tab closed past the idle window, browser asleep long enough for NiceGUI to prune the client, hard timeout, session revoked, account deactivated) left the browser holding a dead token: `require_logged_in()` only checks whether a token is *stored*, so the page rendered fully and then every call 401'd with "Your session has expired." on a screen the user couldn't use. Three changes close it.

**1. Any authenticated 401 → `/login` (`ui/api_client.py` + `ui/pages/_chrome.py`).** `_raise_for_status` now fires a process-wide hook before raising, but *only* when the failing request carried an `Authorization` header — read off `response.request.headers`, so the login form's own 401 (wrong password) is untouched. `_chrome._handle_expired_session` (registered at import) clears local state and navigates to `/login`. It is idempotent by construction: the first call drops the token, so parallel 401s from one page render fall out at the `is_logged_in()` guard rather than queuing redirects. The hook is invoked under `contextlib.suppress` — it's chrome, and must never mask the `ApiError` the calling page is about to handle.

**2. The keepalive validates on page load, not just every 60s.** `_install_session_keepalive` adds a one-shot `ui.timer(1.0, …, once=True)` that POSTs `/api/auth/touch`. A page load *is* activity, so the touch is honest, and a token that died while the tab was shut bounces the user before they start filling in a form they'd lose. Both paths share `_touch_session()`.

**3. Idle age is measured entirely in the browser.** The old check compared a browser `Date.now()` stamp against the server's `time.time()` — any clock skew between the two signed an active user out early (browser behind) or never at all (browser ahead). The JS now returns `Date.now() - window._matofxLastActivity` and Python only compares that delta to the policy threshold.

**Why the URL, not a toast.** `ui.notify` immediately before `ui.navigate.to` dies with the page that raised it. The reason travels as `?reason=idle|expired` (alongside the existing `?next=`, composed by the new `_chrome._login_url`) and `login_page` renders it as an amber note above the form — so the return trip is explained and signing back in resumes where the user was. Unrecognised values render nothing.

**Touchpoints.** `ui/api_client.py` (`set_unauthenticated_hook`, `_notify_unauthenticated`), `ui/pages/_chrome.py` (`_login_url`, `_handle_expired_session`, `_touch_session`, keepalive + `_logout_idle`), `ui/pages/login.py` (`reason` param, `_SIGN_OUT_NOTICES`), `docs/user/00-getting-started.md`, `docs/admin/70-site-settings-and-branding.md`. **Tests** — `tests/ui/test_session_expiry.py` (revoked token fires the hook against the real API; bad password and non-401s don't; URL composition; teardown + idempotence; notice lookup).

Unrelated in the same change: the `model_curator` capability role renders as **Surrogate Model Curator** everywhere (`_ROLE_LABEL_OVERRIDES`) — "Model Curator" read ambiguously next to the tool and material catalogues. DB role name unchanged; help docs aligned to the visible label.

### 16.48 Compliance attestation — signed, anchored licence reports, ADR-0019 slice 1 (shipped 2026-08-09)

First slice of ADR-0019, deliberately ahead of the licence subsystem so something vendor-facing exists before enforcement lands. New package `matofx/compliance/` (migration **0088**: `install_identity`, `compliance_report`).

**The mechanism is anchoring, not the signature.** A report cites the previous report's chain head *and the hash that record carries right now* (`anchor.previous_head_rehashed_now`). Funis keeps a per-install JSONL ledger of heads received; report N+1 must show report N's head still present and unchanged. An install that rewrote its history can sign a perfectly self-verifying report and still fail, because it cannot make record N hash a value it has no way to read. `designs/examples/` holds three generated artefacts proving exactly that — report 3 passes `signature` and `counter`, fails `anchoring` and `genesis`.

**Install identity (`identity.py`).** Funis issues `funis-install-identity.json` out-of-band — `install_id`, customer, and an Ed25519 report keypair — signed by an issuer key trusted via the build-time map in `issuer.py` (empty in-tree; release engineering populates it) plus `[compliance].issuer_public_keys` for dev/self-hosting. **The private key is never persisted** — it stays in the file and is read at signing time, so it can't leak through a DB backup or the admin DB explorer. Adoption refuses to re-badge a database that already belongs to a different `install_id` (a clone needs a fresh identity and a fresh chain), while a *re-issue* for the same install keeps the report counter: restarting the sequence would look, to Funis, exactly like a withheld period. `next_report_counter` uses a conditional `UPDATE … RETURNING` so concurrent generations can't claim the same number.

**Both licence types are first-class from day one (`schema.py`).** CSL and FAL measure genuinely different things, so `usage` carries a discriminated block for each with the authoritative one named; the cap is applied **only** to the authoritative block, so a CSL install's FAL figures can never raise a false breach. CSL peak concurrency is computed from `auth_session` intervals (a sweep line, ends before starts at equal timestamps), which is why it is meaningful before any enforcement exists. FAL assignments fall back to the enabled-accounts count with `source: "enabled_accounts"` stamped on it — under FAL semantics a user who can log in *is* a user holding an assignment — so a pre-slice-5 proxy can never be silently compared against a real count. `LICENCE_EVENT_ACTIONS` is zero-filled from report 1, so the document's shape doesn't shift as slices 2–7 start emitting events.

**Report generation never fails closed (§B9).** A broken chain, a missing licence or a breach still produces an artefact *saying so* — refusing would destroy the evidence being requested. Generation is one transaction (counter + archive row + audit event), and the `compliance.report.generated` event is written *after* the head is captured, so it belongs to the next report's period rather than having to predict its own hash.

**Verdicts (`verify.py`).** Six checks — signature, counter, **anchoring**, genesis, chain self-verification, issuer — plus licence *findings*. A breach is **not** a contradiction: an install honestly reporting that it exceeded its cap is behaving correctly, so `CONSISTENT` + `BREACH` is the normal shape of an over-cap month, and break-glass admissions are broken out so a Super User rescue reads differently from users quietly exceeding the licence. A first report is `INCONCLUSIVE` — it establishes the baseline and cannot yet prove anything.

**Surfaces.** `matofx compliance adopt|report|verify` (the verifier ships in-product so Funis can run it anywhere), canonical JSON + a self-contained HTML twin the customer's admin reads before sending, and `[compliance]` settings. **Tests** — `tests/compliance/` (56: identity/issuer trust, generation against a real migrated chain incl. the rewritten-history case, both licence types' caps parametrised, vendor-side verdicts, ledger, canonical bytes, HTML escaping). **Docs** — new admin chapter `125-licence-compliance.md`, plus cross-links from the audit-log and inactivity sections. **Not yet built**: the admin page (CLI only), and slices 2–7 (manifest, enforcement, read-only mode).

### 16.49 Licence manifest + verification, ADR-0019 slice 2 (shipped 2026-08-10)

New package `matofx/licence/` (migration **0089**: `licence_install`) — the module brief §C16 designates as the *single authority* on what an install may do. Nothing else reads the licence file; everything routes through `resolve_licence()`.

**Four gates, in order (`loader.py`).** Readable → **trusted** (Ed25519 issuer signature over the canonical bytes of the `licence` object, same trust root as install identities — one supplier, one key map) → structurally valid → **for this host**. The order matters: each gate's error message only means anything once the previous one passed.

**The enforcement split is the design's spine (§C11).** `LicenceInvalidError` (edited file, untrusted issuer, wrong machine) **fails closed** — someone tampered, and that is not an availability problem. `LicenceMissingError` (no file) degrades to **read-only**. Expiry is *neither*: an expired licence verifies perfectly, so `active → grace → expired` are **states carrying `read_only`**, never exceptions. §C16 forbids a hard stop on expiry, and a test asserting a lapsed renewal can't take a site down guards it. `LicenceResolution` never raises — its callers are startup paths and (slice 6) request middleware that must degrade deliberately.

**Machine binding is opt-in and deliberately weak (`machine.py`).** Hashed `/etc/machine-id` / `MachineGuid`, compared only. An *unreadable* machine id on a bound licence is treated as a **mismatch**: "we cannot tell which host this is" must not silently satisfy "this licence is for host X". Because that breaks on container rebuilds, install *identity* stays the DB-persisted `install_id` (§A3) and binding answers only "has this licence moved host?".

**Forwards/backwards compatible (§C16).** Unknown manifest keys are **preserved** in `extra` (they are inside the signed bytes, so a future field must survive a round-trip through an older install); a *newer* `schema_version` is refused rather than half-read.

**The compliance report goes real.** `licence_snapshot()` fills the block that read `not_installed`, so usage stops being observation and is measured against an actual cap — per licence type, authoritative block only. `report_identity()` moves the report signing key into the manifest (it arrives through the same signed channel and expires with the licence); the standalone identity file remains the fallback and returns `None` rather than raising so the CLI falls back cleanly. `designs/examples/` now ships `matofx-licence.json` and report 2's `licence` block is verified, not hand-built.

**Bug found by the tests, worth remembering:** `install_licence` read `previous.licence_id` for the "replaced" audit field *after* mutating the row — but `previous` and `row` are the same ORM object on a replacement, so a renewal recorded itself as replacing itself. Snapshot before mutating.

**Surfaces.** `matofx licence install|reload|show` (install verifies **before** copying, so a bad file can never displace a working one), `[licence].file` settings + example TOML block. **Tests** — `tests/licence/` (45: trust/tamper/untrusted-issuer/unsigned, structural failures parametrised, machine binding incl. the unreadable-id case, the expiry timeline at its boundaries, install/replace auditing, validation events, and the report integration for both licence types × over/under cap). **Docs** — new admin chapter `15-your-licence.md` (the two licence types as a table, installing, renewing, grace, read-only, and the three refusal reasons), glossary entries, cross-links. **Not yet built at the time**: slices 3–7 (slice 3 landed the same day — see §16.50).


### 16.50 Licence enforcement cadence + feature gating from the manifest, ADR-0019 slice 3 (shipped 2026-08-10)

§C16's cadence is "at startup; every N minutes; at every login". Slice 3 lands the first two (the login check arrives with slot enforcement in slice 4) and switches feature gating to the licence.

**Startup is where fail-closed bites.** `check_licence_at_startup()` runs at the top of `make_app`, *before* any router is wired: a tampered / untrusted / wrong-machine licence raises and the process never serves a request. A **missing or expired** licence only logs — §C16 forbids a lapsed renewal taking a site down — and a test asserts each of those four cases separately so the distinction can't erode.

**The hourly re-check lives in the runner**, not Arq. The brief chose Arq; the implementation has a supervisor with `_sleepable` loops driving every other periodic task (pickup, heartbeat, retention, inactivity), so this is another one — `_licence_loop`, default 3600s per §C16. The runner is the one guaranteed long-lived process and already owns the audit writer. `recheck_licence()` **never raises**: a licence file edited under a running install must not kill jobs mid-flight, so it is recorded as `licence.validated.failed`, logged, and escalates through the banner and the next compliance report instead. The loop is armed only when `app_settings` is passed (the daemon does; tests constructing a bare supervisor don't), so no existing runner test changes. Every tick is recorded, not only transitions — §C16 asks for it, and the steady drum of `licence.validated.ok` is itself evidence a report counts (the worked example carries 744 for a month).

**Feature gating switches source at one chokepoint.** `apply_licence_features()` in `make_app` rewrites `settings.features` from the manifest, so all ~10 `feature_enabled(settings.features, Feature.X)` gate points — and the UI's `_features.py`, which reads `app.state.settings` — are untouched. Two fallbacks chosen so a licence problem can never look like a product outage: **no usable licence** → keep TOML (a lapse already costs read-only; blanking the feature list would be a second, more confusing penalty for the same lapse), and an **absent / empty** `features` map → keep TOML (§C16 ships every install with everything on, so "no map" means "no opinion", not "nothing is licensed"). A map that is *present but all-false* IS an opinion and disables everything. Unknown slugs are **ignored, not rejected** — the opposite of the rule for `[features]` in TOML, because a newer licence must keep working on an older build (§C16 forwards-compatibility).

**Docs correction found on the way:** the admin reference claimed the feature switches were controlled in General Settings. They never were — there are no such catalog rows — and from this slice they come from the licence. Corrected in `140-reference-and-glossary.md`, with the full story in `15-your-licence.md` (what the licence switches on, the two fallbacks, and the check cadence).

**Tests** — `tests/licence/test_enforcement_cadence.py` (14: the four startup cases; re-check recording every tick, not raising when the file goes bad, and noticing a licence replaced in place; six feature-precedence cases including all-false vs absent). **Not yet built**: slices 4–7 — CSL slots at login, FAL assignments, read-only middleware, `SeatsStats.cap`.


### 16.51 CSL slot enforcement at login + the break-glass surface, ADR-0019 slice 4 (shipped 2026-08-10)

The first slice a *user* feels. `matofx/licence/slots.py` decides; the auth router only acts on the answer (§C16 — the licence module is the single authority, nothing else counts sessions).

**Four outcomes, not two.** `granted` / `breakglass` / `denied` / `unmetered`. The gate runs **after** the password is verified (so a refusal can't leak whether an account exists) and **before** the session row is created (so a refused login leaves nothing behind — there's a test asserting the row count is unchanged).

**Liveness must match `validate()`.** `count_live_sessions` uses the same rule as `SessionService.validate` — un-revoked, inside the idle window, inside the hard window, via `effective_session_policy` so an admin's General-Settings override counts. If it drifted and counted rows `validate` would reject, the licence would refuse logins on behalf of dead sessions, which looks exactly like being over-licensed. Test covers all four shapes at once.

**Break-glass (§C13)** admits Super Users over the cap — an Administrator at the cap waits like everyone else (tested). The event carries `reason="admitted over the cap as a Super User"` plus `in_use`/`n_slots`, which is what lets the verifier print "peak 11/10 — 1 break-glass admission(s) account for the excess" instead of flagging an unexplained breach.

**Read-only mode is unmetered (§C12).** "Every registered user may sign in and view results" cannot hold if an expired licence still turns people away at the cap. FAL installs are likewise unmetered *for sessions* — under FAL the places are assignments (slice 5).

**503, not 403.** `LicenceSlotsExhaustedError` → `HTTP_503_SERVICE_UNAVAILABLE`: the password was right, this is capacity. The login page renders the API's detail verbatim, which names the cap and the way out.

**The break-glass surface.** `LoginResponse` gains `breakglass` / `slots_in_use` / `n_slots`; login stashes the notice via `store_breakglass_notice` and the chrome renders it as a **read-and-clear** banner (a `ui.notify` at login dies with the page — the same lesson as §16.47's `?reason=`). The banner links to the new **Admin → Active sessions** page (`users.manage`), which shows capacity, lists sessions **longest-idle first** (the API returns most-recently-active, which is backwards for "who can I safely sign out?"), and offers per-row **Sign out**. The caller's own row shows "You" instead of a button — an admin who signs themselves out mid-rescue has made it worse; the API refuses that too.

**Three bugs found while wiring.** (1) `Request` was used in the new admin endpoints but never imported — the routes still registered, because FastAPI fell back to treating the unresolved annotation as a *query parameter*; ruff's F821 caught what the import smoke test didn't. (2) `LicenceSlotsExhaustedError` mapped to 503 in `_STATUS_MAP` but no handler was registered — `install_exception_handlers` registers one handler per *umbrella* class, and `LicenceError` wasn't in the list, so the exception escaped as a 500. (3) The self-revoke guard and the "You" marker both keyed on `request.state.session_id`, which **nothing ever set** — so the guard compared against `None` and never fired, and the listing offered an administrator the button to sign themselves out of the page they'd opened to rescue a full licence. `get_current_user` now stashes it (the only place that resolves a bearer token to a row), with a test asserting exactly one row is marked as the caller's and that revoking it 400s. All three are the kind of thing only an end-to-end test catches — the first two passed an import smoke test, the third passed *every* test, because nothing asserted the guard actually fired.

**Touchpoints.** `licence/slots.py`, `licence/errors.py` (+`LicenceSlotsExhaustedError`), `licence/models.py` (`CSL`/`FAL` constants — deliberately duplicated from `compliance.schema`, since `licence` imports `compliance` and the reverse would be a cycle), `api/routers/auth.py` (gate + release on logout), `api/routers/admin.py` (`GET/POST /admin/sessions`), `api/errors.py`, `api/schemas.py`, `users/service.py` (`peek`), `ui/pages/admin_sessions.py` (new), `ui/pages/_chrome.py` (banner + nav), `ui/state.py`, `ui/pages/login.py`, `ui/api_client.py`. **Tests** — `tests/licence/test_slots.py` (10: liveness, all four outcomes, Administrator-is-not-break-glass, FAL + read-only unmetered, release semantics) and `tests/api/test_licence_slots_api.py` (6, end-to-end: capacity in the login response, break-glass flag, 503 + no session row, unmetered default, admin list/revoke, 404s). **Docs** — `15-your-licence.md` ("When all the places are in use", freeing a place) and `00-getting-started.md` (what an end user sees).


### 16.52 FAL floating-assignment enforcement, ADR-0019 slice 5 (shipped 2026-08-10)

`matofx/licence/assignments.py`. The mirror of slice 4, and deliberately **not** symmetric with it.

**No new concept, no new column.** §C16's FAL description — "users without an assignment exist in the system (their account, projects and settings persist) but cannot log in" — is a precise description of a *deactivated user*, which MATOFX has modelled since migration 0009 (`user.is_active` + `logon_granted_at` / `logon_revoked_at` + the append-only `user_logon_event` history). So slice 5 puts a **cap** on the lifecycle that already existed rather than inventing a parallel one. That is also why slice 1's `enabled_accounts` proxy was the right number all along: under FAL, an account that can sign in *is* an assignment.

**Granting is metered; revoking never is.** The gate runs where an admin *activates* an account — `POST /admin/users` (new accounts are active on creation) and `POST /admin/users/{id}/reactivate` — and always **before** the write, so a refusal leaves nothing half-changed (tested: the refused username is absent from the listing afterwards). `deactivate` is never gated and records `licence.assignment.revoked`. The asymmetry is load-bearing: an install that has hit its cap (or been downgraded below its current headcount) must always be able to get back under it, or an admin can lock the licence into a state only the supplier can undo.

**409, not 503.** Unlike CSL's capacity blip, this is a decision the admin has to take — free one, or extend the licence. Retrying unchanged will never succeed, so it must not look transient.

**The report's FAL figure stops being a proxy.** `fal_usage(enforced=…)` flips `source` from `enabled_accounts` to `fal_assignments` when a FAL licence is actually in force, so Funis never compares a pre-enforcement count against a metered one without knowing which is which. The count itself is unchanged — only its status.

**Surfaces.** `GET /api/admin/licence/capacity` feeds a line on the Users page ("14 of 25 licence places assigned (11 left)", amber at the cap), so an admin sees where they stand *before* filling in a new-user form rather than discovering the cap when the save is refused. Silent on CSL installs, where a "places used" line against accounts would be actively misleading.

**Tests** — `tests/licence/test_assignments.py` (7: active-only counting, granted/denied, the actionable refusal wording, revoke-never-gated *while over cap*, CSL + read-only + no-licence unmetered) and two end-to-end cases in `tests/api/test_licence_slots_api.py` — the full round trip (fill the cap → 409 + nothing created → deactivate → succeed → reactivate now refused) and the unmetered default. **Docs** — `15-your-licence.md` ("When all the assignments are taken", how to free one) and a pointer in `20-people-and-access.md`.


### 16.53 Read-only mode — the first middleware in the codebase, ADR-0019 slice 6 (shipped 2026-08-10)

Two independent causes, one answer: a licence that is **missing or expired past grace** (§C16), and a **failed audit-chain verification** until a Super User acknowledges it (§C14). Migration **0090** (`integrity_state`).

**Middleware, not a per-route dependency — and it was a real choice**, because this codebase had no middleware at all before now. A dependency is more precise but fails **open**: the day someone adds a router and forgets it, an unlicensed install writes again and nobody notices. Central gating by HTTP method fails **closed** — a read-only-safe POST missing from the allow-list breaks loudly and immediately, which is the error you want. `_read_only_reason` never raises. *Narrowed in §16.56*: the **licence** half now fails **closed** — if deciding whether the licence permits writes itself errors, treating the site as writable would let an unlicensed install keep working by breaking the check. The integrity half still fails open, so a transient database blip cannot 503 every write.

**The allow-list is "what must keep working", not "harmless writes".** Login/logout/touch (§C12 promises sign-in; blocking logout would strand users and, under CSL, hold licence places open), licence install and integrity acknowledge (an admin locked out of the fix has no way back), session revoke and compliance report (freeing a place; answering the supplier while degraded), and per-user display preferences (view state, not governed data).

**The design problem this slice surfaced.** A missing licence is read-only — so with the middleware in, *every developer checkout, CI run and test harness became read-only*, and the whole API suite failed. Resolved by distinguishing **never configured** (`[licence].file` unset ⇒ a dev install, writable) from **configured and broken/expired** (a real lapse ⇒ degraded), and closing the loophole from the other end: the config model now refuses to load `environment = "prod"` without `[licence].file`. Both halves are needed — the exemption alone would let a customer run unlicensed for ever by simply never configuring it. **The prod requirement had a consequence worth recording:** `etc/matofx.example.toml` ships with `environment = "prod"` and had the licence line commented out, so the rule made the shipped example config fail to load — the file a customer copies as their starting point would have died on first boot. `tests/config/test_loader.py::test_example_config_loads` caught it. The line is now uncommented and marked REQUIRED, matching how `audit.signing_key_path` is already presented, and the prod-config tests supply a licence path via a `_licence_file()` helper. The site-mode endpoint applies the identical exemption, or an unlicensed install would *report* read-only while writes sailed through.

**Integrity state is persisted, not recomputed** (`compliance/integrity.py`): read-only is decided on every mutating request and a full chain walk is far too expensive for that path. Startup does an incremental check (head + a bounded window) and the hourly runner tick does the full walk, riding the licence loop — both answer "is this install still trustworthy?". *The startup half was specified here from the start but only actually built in §16.56; until then an install whose runner never ran checked nothing.* Attestations also expire — see §16.56. Only *transitions* are audited: an hourly `failed` event for a month would bury the one that matters. **A fresh failure clears any previous acknowledgement** — acknowledging one problem must not silently absolve the next (tested).

**Acknowledgement is role-gated, not permission-gated.** `super_user` only, checked by role rather than a grantable permission, because this is the one action that sets aside the tamper-evidence guarantee. The note is mandatory (min 10 chars, enforced API-side and pre-validated in the dialog) and lands in the compliance report beside the failure — "ok" tells the reader nothing six months later. Acknowledging never erases: `integrity_ok` stays false, the site simply becomes writable.

**UI.** `GET /api/site/mode` is open to *any* signed-in user, not just admins — the difference between "the button did nothing" and "the site is in a known state" is the entire point. The chrome renders a red banner for read-only and amber for an acknowledged-but-standing failure. `render_shell` is sync and called from both sync and async page builders, so the fetch rides a one-shot `ui.timer` into a reserved container — the same idiom as the keepalive's first tick.

**Tests** — `tests/api/test_readonly_mode.py` (9: the §C12 shape end-to-end — sign in, read, write refused with `read_only_reason`; the allow-list ways out; a valid licence stays writable; the dev exemption *and* the prod config refusal; integrity failure → read-only → acknowledge → writable; a fresh failure clearing an old acknowledgement; Super-User-only; the note minimum). **Docs** — `125-licence-compliance.md` (what keeps working, and "Acknowledging a failure").


### 16.54 Transition events + the seats cap, ADR-0019 slice 7 (shipped 2026-08-10)

The last two gaps: the moments the site's posture changed, and the licensed cap the Usage tab had rendered as "—" since Phase 1.

**Transitions, not ticks.** The hourly re-check already writes `licence.validated.ok` every hour — that proves the check *ran*. What a compliance reader scans for is the handful of moments something *changed*: the day it entered grace, the day it went read-only, the day a renewal lifted it. `_record_transitions` compares consecutive resolutions and emits `licence.expiry.transition` (payload naming both ends — `active -> grace`, not "something changed"), `licence.readonly.entered` and `licence.readonly.left`. A tick in an unchanged state writes nothing, and the *first* check writes nothing either — a boot is not a transition.

**`SeatsStats.cap` stops being `None`.** It comes from the manifest, alongside `licence_type` and `licence_state`, because the two types bound **different figures**: CSL caps peak concurrency, FAL caps enabled seats. A tile showing a bare number next to both lines would be quietly wrong, so `_cap_suffix` says which — "CSL — limits peak concurrent sessions", and appends a non-active state ("(grace)") where it applies. `UsageService` stays **stateless**: settings are threaded through the `seats()` call rather than stored on the instance. `_licence_entitlement` never raises — a usage tab must not be the thing that surfaces a licence problem, so a broken licence yields `(None, None, None)` and the tile shows "—" (tested against a tampered manifest).

**One catalog entry has no emitter, and says so.** §C16 lists "admin overrides" among the licence events, but this build has no such concept — the two things that could be called an override are already their own events (`licence.slot.breakglass`, `audit.verify.acknowledged`). `licence.override` is kept in the catalog so the report's shape stays stable if one is ever added, and is commented as reserved rather than quietly zero-filled for a reader to puzzle over.

**Tests** — `tests/licence/test_transitions_and_seats.py` (9: transition recorded once and not repeated, entered *and* left both recorded, payload names both ends, first check is not a transition, cap from the manifest for both licence types, `None` without a licence, survives a tampered licence, and the suffix naming).


### 16.55 Test suite: one migrated template per session, not per test (shipped 2026-08-10)

Not a product change, but it was blocking verification of everything above, so it belongs in the record.

**The problem.** 27 test modules each ran `alembic upgrade head` followed by `downgrade base` **per test**. At 90 migrations that is 20-40 s of setup for a test asserting one thing. `tests/licence` + `tests/compliance` took 28 minutes; `tools`, `approvals`, `migrate`, `config` and `audit` couldn't finish inside a 9-minute window at all. Long runs were being killed before they reported, which meant changes were landing unverified.

**The fix (`tests/_migrated_db.py`).** Migrations still run — **once** — into a session-scoped template file; each test gets a **byte copy**, which is milliseconds. Registered as a plugin from a new root `tests/conftest.py`, so no conftest has to import the fixture (an import plus a same-named parameter is a redefinition ruff rightly flags).

Three properties deliberately preserved: migrations are still *exercised* (`metadata.create_all()` would be faster but would stop testing the chain and skip everything the migrations seed — roles, permissions, the World team — which most tests depend on); isolation is still per test (a copy is a fresh database, and no `downgrade base` is needed because the copy is thrown away with `tmp_path`); and **Postgres is untouched** — there is no cheap file copy for a server database, and those runs skip without `MATOFX_TEST_POSTGRES_URL`, so that path migrates per test exactly as before.

**WAL matters.** `matofx.db.engine` enables `journal_mode = WAL`, so part of the database lives in `-wal` / `-shm` sidecars. Copying only the `.db` would silently lose the tail of the migration run; all three travel together.

**Three fixture shapes needed converting**, not one: the single-line conftest signature, a multi-line variant (`approvals`, `migrate`, `workflows`, `audit`), and fixtures defined *inside* a test module (six `tests/tools/test_*.py`, `config/test_general_settings.py`). The worst case was `migrate/test_export_import_e2e.py`, which builds a source and a destination install side by side — 180 migrations per test.

**Results.** licence+compliance 28:28 → 22.5s; ui 14:18 → 2:28 (with *identical* 9 failed / 593 passed, which is the control proving nothing changed but the clock); tools >9:00 → 1:38; approvals >6:40 → 25.6s; migrate, config, audit each ~19s from timing out; `api/test_jobs.py` 5:52 → 58.7s.

`tests/api` still takes 28 minutes: migrations are no longer its cost, the tests themselves are (HTTP round trips, staging directories, real app construction). The next lever there is `pytest-xdist` across the box's 20 cores, which isn't currently a dependency. `tests/db/test_alembic.py` deliberately still migrates per test — exercising the chain is its whole job.

**A caution for whoever extends this.** Reaching for regex across 27 files went badly the first time: the fixture parameter landed in unrelated functions, the helper's own signature was damaged, and a `ruff --fix` scoped to all of `tests/` auto-fixed 93 things in files belonging to other in-flight work. That was reverted file by file after inspecting each diff. Convert these one at a time.

### 16.56 Anchoring rework: Merkle consistency proofs, and the fixes from the ADR-0019 stress test (shipped 2026-08-11)

A stress-test pass over everything in §16.47–16.55 found four defects that testing during the build had not, plus the three gaps the previous session had already flagged. All are closed here. The pattern worth carrying forward: **every one of the new findings was a false accusation, not a missed detection**. The attack cases all worked; it was the honest cases that failed.

**F1 — an undelivered report poisoned verification for ever.** Anchoring compared the install's self-reported previous head against the vendor's latest ledger entry, and hard-failed when they differed. Reports are generated on request, so an install routinely produces one that is never sent — a preview, a failed send, a dry run. From then on *every* report verified `CONTRADICTED`, with no way back: `--append` refuses to record a contradicted report, so the ledger could never advance again. The verdict was also byte-identical to a real clone or a real rollback, so the vendor could not tell fraud from a customer who previewed a report.

**The fix — RFC 6962 (`compliance/merkle.py`).** A Merkle tree over the audit-record hashes, with a **consistency proof** from the tree size of every report the install has generated. The vendor looks up whichever size their ledger holds and checks that proof. Undelivered reports become irrelevant by construction. One primitive replaces three weaker checks — shrinkage, alteration and extension are all the same question at different sizes.

Certificate Transparency's format rather than a bespoke one, so the artefact is checkable by tooling that knows nothing about MATOFX; `tests/compliance/test_merkle.py` pins the published RFC vectors, so a divergence from the standard fails the build. Size is the reason it works at the agreed **one-or-two reports a year**: the worked example proves a 343-record extension in **7 hashes**, and the proof grows logarithmically — a 90,000-record chain needs ~17. Shipping every record hash since the last report would have been megabytes.

Report schema → **v2**; migration **0091** adds `tree_size` / `tree_root` per report (an install must be able to prove from any size it once attested); `LedgerEntry` gains the same pair. `designs/examples/` regenerated.

**F2 — a fresh install accused itself on its second report.** `entry_from_report` coerced a null head to `head_id=0, head_hash=""`, so a report taken before any audit record existed recorded a head of 0. The next report was then told `record 0 no longer exists in the chain — history has been truncated`. Adopt an identity and report before anyone logs in — the natural commissioning order — and the customer is accused of deleting history on day one. `head_id` / `head_hash` are now nullable and an empty baseline skips anchoring, exactly as a first report does.

**F3 — the first report's period ran backwards.** `_default_period_start` returned a freshly-computed `now()` for an empty chain, evaluated *after* the report's own timestamp, giving `from > to` by a few milliseconds. Everything between adopting the identity and the first report therefore fell in a negative window and was counted by no report ever. Report 1 now starts at the earlier of the first audit record and `adopted_at`, clamped so it can never invert.

**F4 — two read-only exemptions protected nothing.** `/api/admin/compliance` had **zero** routes and `/api/admin/licence` only a GET; both were exempt, justified by comments describing CLI-only operations. They pre-approved whatever might land beneath them later. Removed, and `tests/api/test_readonly_allowlist.py` now enumerates the app's real routes and fails if the exempt set changes, if a prefix goes dead, or if a mutating verb hides behind a GET. §6.2's "the allow-list will rot" was not a prediction — it had already happened.

**F5 — `licence show` reported the manifest, not reality.** With an empty `features` map it printed `(none)` while the install actually ran the TOML set; with an unknown slug it advertised a feature that is silently ignored. That is the command an administrator runs to answer "why is this feature missing?". It now prints the **effective** set and explains any divergence. (The gating logic itself was correct throughout.)

**Gap 6.1 — CSL admission is no longer a race.** `admit_session` counted and the caller inserted, two statements with no lock between them; five concurrent logins against a three-slot licence all passed. Admission is now two-phase: the pre-check stays provisional, and `confirm_admission` re-checks once the row exists. The loser is chosen by **rank** — this session's position among live sessions by `(created_at, id)` — not by re-count, because bouncing everyone who *observes* an over-cap count would over-correct and leave the place empty. No dialect-specific lock, which matters for the SQLite first client. Break-glass is never withdrawn. The audit event moved to the confirmation, so a bounced login leaves no `granted` event inflating the customer's own usage evidence against them.

**Gap 6.3 — the startup integrity check now exists, and attestations expire.** The ADR and §16.53 both claimed an incremental startup check; only the runner's hourly full walk was ever built, so an install whose runner was not running never checked its chain at all. `verify_recent` (head + `RECENT_WINDOW_RECORDS`) now runs from the API lifespan — both processes attest, neither is a single point of failure. A failure brings the site up read-only rather than refusing to boot, because refusing would remove the only surface for acknowledging it.

The second half is more important. A recorded verdict says "the chain was intact **when it was checked**", so it cannot stand indefinitely: an install whose checker died would keep serving on an arbitrarily old verdict with nothing to say so. The attestation now **expires** — banner at 3 hours naming the likely cause, read-only at 24, liftable by the same Super User acknowledgement so planned maintenance is not a brick. This is the OCSP `nextUpdate` pattern: absence of checking becomes self-enforcing instead of something a separate watchdog must notice. The report carries `last_verified_at` / `verification_is_current`, so an install running with checking off is visible to the vendor.

**What was given up, deliberately.** Clone detection (S8) no longer rests on a diverging head — a clone legitimately contains the shared history and passes anchoring. It now rests on **counter collision**: both forks issue counter *N* and the second to arrive replays. That fires only on a genuine duplicate, but needs both forks to report; if only the clone ever reports it is indistinguishable from the original going quiet. The trade is worth it because the old signal only fired on clones *and* on every honest install that skipped a delivery, which made it unactionable. ADR-0019 §A5 now records the frame: enforcement is contractual (the IBM ILMT posture), and the software's job is to make inconsistency impossible to avoid without sustained provable lying — never to accuse an honest customer, because a false `CONTRADICTED` destroys the value of a true one.

**Also fixed:** the read-only middleware's licence half now fails **closed** (a failure to resolve the licence is read-only, rather than letting writes through), while the integrity half still fails open so a transient database blip cannot 503 every write. `matofx` CLI config errors print the loader's already well-written message instead of burying it under a Python traceback.

### 16.57 Issuer enforcement is a property of the build, not of the config (shipped 2026-08-11)

`require_issuer` was `settings.environment == "prod"`. `environment` lives in the customer's `matofx.toml`, so gate 2 of the licence loader — *is this signed by Funis?* — was switched off by one word:

```toml
environment = "dev"     # was "prod"
```

After which `_check_issuer` accepted a licence with **no signature at all**, and one signed by a **key the build has never heard of**, both with `issuer_verified: false`. Writing yourself a licence with any slot count and every feature was a text edit.

That sat badly beside the ceiling ADR-0019 §A4 knowingly accepts — extract the report key, rebuild the chain, and keep every consistency proof aligned for ever. One of those is a determined attack; the other is a config change. A model should not contain both.

**The fix (`loader.issuer_required`).** Enforcement now keys off whether the *artefact* carries Funis issuer keys: `TRUSTED_ISSUER_KEYS` is populated at release-build time, so a released copy always demands a signature it can verify, and nothing in the customer's config can relax it. A source checkout has an empty map and stays tolerant — the only case the old rule was actually serving. `environment == "prod"` is retained as an additional trigger so a source-run production install is still held to the rule.

Applied at all five call sites, not just the one: `resolve_licence`, `licence install` (both the pre-copy verification and the post-copy re-read), the report-key lookup, and `compliance adopt` — the install-identity path had the identical hole.

**Why the map was already the right shape.** `TRUSTED_ISSUER_KEYS` is a `key_id → public key` dict whose docstring already anticipated rotation ("old keys stay trusted until customers naturally re-issue"). Making its *emptiness* the dev signal needed no new configuration surface, which matters: any new setting here would have recreated the bypass in a different spelling.

**Install binding (added the same day).** The manifest had always carried an `install_id`; nothing compared it against the install running the licence. So copying `matofx-licence.json` to a second machine and installing MATOFX fresh simply worked — same slots, same features, second install — and the only trace was a report-counter collision, which needs *both* installs to eventually report before anyone notices.

`verify_install_binding` now refuses a licence issued for another install. It needs a database session, which `check_licence_at_startup` deliberately does not have, so it runs from the API lifespan before the first request is served (uncaught — the same class of problem as a wrong-machine licence, which already refuses) and from the runner's hourly tick (logged, not raised — the runner cannot refuse without killing jobs mid-flight).

**Why install binding rather than machine binding** (maintainer, 2026-08-11): `/etc/machine-id` is a root-writable file, so machine binding stops casual duplication but not anyone actually trying — while breaking on every container rebuild, VM clone and host migration. `install_id` is DB-persisted, so it survives all three, and still stops a fresh parallel install cold. Machine binding remains available as an issuer-chosen extra. Neither stops a whole-database clone; nothing local can.

**The issuing rule this creates:** a customer's second and later licences must carry the `install_id` established by their first, or the site refuses at go-live. `test_the_go_live_licence_keeps_the_setup_install_id` pins it.

**The install now names itself (same day, superseding the paragraph above).** Install binding as first built did *not* stop a second instance, for two reasons the maintainer was right to probe. First, `verify_install_binding` returned early when no identity had been adopted — and adoption only ever happened via `matofx compliance adopt`, run by hand, so a shadow install that simply never ran it was exempt for ever. Second, and more fundamental: `install_id` came *from* Funis inside the licence, so it identified the **licence**. Two deployments fed the same licence both legitimately claimed the same id and both passed.

Inverted in migration **0092**: `ensure_install_identity` mints a random `inst_…` (Crockford base32, `secrets`) at first boot, and Funis issues a licence *for* it — the node-locked model FlexLM and every CAE vendor uses. `matofx licence request` prints the id for the customer to send. A second deployment mints a different id, so a copied licence is refused there; and because there is always a local identity, the "no identity yet" branch that could be hidden in no longer exists.

The Funis-supplied columns on `install_identity` become **nullable**: an install has an identity from first boot, before any licence exists to say who the customer is or which report key they hold. `bind_identity_to_licence` fills those in when a licence is installed — and `install_licence` refuses a licence naming another install *before* recording anything, so the administrator finds out at the terminal rather than at the next restart.

**The ceiling, unchanged and worth stating:** root can `UPDATE install_identity SET install_id=…` on a second database and copy the licence across. Nothing local prevents that. What changes is the bar — from *copy a file*, which is deniable and might be an accident, to *perform surgery on your compliance identity*, which is neither, and which counter collision catches if both installs ever report.

### 16.58 Licence issuing, and two defects the round trip found (shipped 2026-08-12)

Everything in §16.57 assumed a licence could be *issued*. Nothing could issue one: the only thing that had ever minted a manifest was `scripts/make_compliance_examples.py`, using seeds (`0x11…`, `0x22…`) explicitly marked never-to-be-real. `designs/licence-and-compliance-overview.md` §13 recorded it as gap 2, blocking the first sale outright.

**`scripts/issue_licence.py`** closes it — `keygen`, `pubkeys`, `issue`, `show`.

**Why a script and not `matofx licence issue`.** A CLI subcommand lands in every built artefact, so the ability to mint licences would ship to the customers it is meant to constrain. The *verifier* ships deliberately (one codebase, one definition of the format); the issuer must not. It still imports `LicenceManifest` and `canonical_bytes` from the package rather than reimplementing them, because a second definition of what a licence *is* would drift, and the first symptom would be a customer's install rejecting a licence Funis believes is valid.

**Two keys are minted together.** The working key signs; the backup key is never used until it has to be. `TRUSTED_ISSUER_KEYS` is a `key_id → key` map whose docstring already anticipated rotation, so baking both public halves in at release time means a compromised working key can be replaced by re-issuing under the backup — **without shipping a new release to every customer in the field**. Without a spare there is no revocation path at all. Private keys are encrypted PKCS#8 (`BestAvailableEncryption`, passphrase from `MATOFX_ISSUER_PASSPHRASE` or a prompt); signing goes through a narrow `Signer` protocol so a YubiKey drops in later without touching the issuing logic.

**The registry** (`~/.matofx-funis/registry.json`, deliberately outside the repo — it holds customer names) pins per customer the `install_id` their licences are issued for and the **report key reused across renewals**. That reuse is load-bearing and easy to get wrong: the head ledger pins a report public key per install, so rotating it at renewal would make every later report fail its signature check against the pinned key.

**The round trip is the test.** Mint keys → `matofx licence request` → `issue --install-id` → `licence install` → `licence show` reports `ACTIVE` with the real feature set; and a licence issued for a different id is refused. Running it end to end found two defects that no unit test would have.

**D1 — the issuer crashed *after* succeeding.** `cmd_issue` writes the licence and saves the registry, then prints a summary. One `→` in one f-string raised `UnicodeEncodeError` on a Windows console (cp1252), so a fully successful issue ended in a traceback and exit 1. The danger is not the traceback: the obvious response is to run the command again, which increments the sequence and mints a **second licence** against the same install. Fixed at both levels — ASCII in the summary, and `main()` reconfigures `stdout`/`stderr` with `errors="replace"` so no future print can fail after bytes are on disk. Degrade the character, never the command. `tests/cli/test_licence_cli.py` fails on any non-cp1252 character in the script.

**D2 — the refusal that matters was buried in a stack trace.** `_licence_install` catches `LicenceError` around its pre-copy verification, but the **install-binding** check runs inside `install_licence`, past that handler — so §16.57's primary defence surfaced as 25 lines of asyncio traceback with the actionable sentence at the bottom. `main()` now renders `LicenceError` the way it already rendered `ConfigError`, for the same stated reason, and the test asserts the *absence* of the stack rather than the presence of the message.

D2 is §9's lesson from the previous handover recurring in a new place: the message was correct and well-written throughout, and every test asserted on its text — which passes identically whether or not it is legible to the person reading the terminal. Nothing had ever run the command.

**Still open (overview §13):** the Funis-side ledger has no tooling, process or backup story (gap 3) and is unsigned and unverified by code (gap 4). A licence can now be issued and installed; the evidence chain it will later be checked against is still operational-only.

### 16.59 The ledger protects everyone else's evidence; now something protects it (shipped 2026-08-12)

ADR-0019's Consequences hand Funis a duty it had no way to discharge: *retain the ledger per install — lose it and the anchoring evidence for that install is gone.* Every compliance verdict is measured against that file, and it was a plain JSONL appended to by `compliance verify --append`, with no tooling, no integrity protection and no backup story (overview §13, gaps 3 and 4). A truncation, a half-written line or a botched merge of two copies was indistinguishable from a short history.

**Chained and signed.** Each entry now carries `prev_hash` — the audit chain's own idiom, so there is one chaining concept in the codebase — and an Ed25519 signature over its canonical bytes, using `compliance/canonical.py` rather than a second recipe. `prev_hash` is derived from the file inside `append_entry`, never accepted from the caller, so a caller cannot get the chain wrong.

**The head marker, and why a chain is not enough.** A hash chain detects an entry edited, reordered or removed from the *middle*: every later `prev_hash` stops matching. It cannot detect a ledger **cut short at the end**, because the remaining prefix is a valid chain one entry shorter. That is both the most likely accident — a partial copy, a truncated write, a restore from an older snapshot — and the most useful deliberate edit, namely dropping the report you would rather not have received. So a signed sidecar (`<name>.jsonl.head`) records the entry count and last entry hash, rewritten after each append, and verification compares the chain it actually read against what the marker says it should have read. This was found by running the attack matrix, not by reasoning: truncation was the one attack that passed silently, and `test_truncation_is_invisible_to_the_chain_alone` pins it so the marker never looks redundant.

**Two deliberate softnesses, one hard rule.** Unsigned entries written before this existed stay readable and verify as `unsigned` rather than failing — refusing to read them would destroy exactly the history being protected — and a missing marker warns rather than accuses. The hard rule is **no silent downgrade**: once a ledger holds signed entries, an unsigned append is refused, because otherwise signing is bypassed by not passing a key. An entry signed by a key id the verifier has no public key for **fails** rather than skipping: a signature nobody can check is not evidence, and quietly passing it is the false reassurance the whole mechanism exists to prevent.

**`scripts/ledger.py`** (vendor-only, same reasoning as the issuer): `keygen` mints working + backup ledger keys — a *separate* pair from the issuing keys, because the two have opposite handling. The issuing key signs a few times a year and belongs offline; the ledger key signs on every report received and must be reachable wherever reports are processed. One key for both would drag the issuing key onto the day-to-day machine, which is precisely what §16.58's two-key split avoids. `verify` recomputes the chain and checks every signature and the marker; `list` shows what has been received per install, flagging counter gaps (a question, never a finding) and installs with no report key pinned; `backup` **verifies before copying and refuses a failing ledger**, so a scheduled backup doubles as a scheduled integrity check — the day it starts failing is the day an older intact copy still exists. It copies the marker with the ledger, since a ledger restored without one silently loses truncation detection at the moment it is most needed.

**A workflow trap surfaced while testing.** The report key is only pinned into the ledger from `--public-key` on a report, so an install whose first report was verified without one never gets pinned — and every later report then *skips* its signature check instead of performing it. By ADR §B8's design that lands `INCONCLUSIVE` rather than a false `CONSISTENT`, so it is safe, but the check silently never runs. `ledger.py list` now says so explicitly, and the runbook makes `--public-key` part of the first-report procedure.

**Gap 3's other half is a document**, not code: `designs/funis-vendor-runbook.md` covers issuing, renewals, receiving a report, reading each verdict, backups, key rotation and disaster recovery — including the instruction to rule out the innocent causes of `CONTRADICTED` before contacting a customer.

**The ceiling, stated as plainly as the rest of ADR-0019:** signing does not make the ledger unforgeable *by Funis*. Whoever holds the key can re-sign a rewritten file, and keeping that key off the machine holding the ledger is an operational control, not a cryptographic one. What this closes is *silent* corruption — turning a loss that surfaces a year later in a dispute into one that surfaces at the next backup — and it is what lets the ledger stand up to somebody who does not simply take Funis's word for it.

### 16.60 Admin → Licence & compliance: the report stops needing a shell (shipped 2026-08-12)

ADR-0019 §B7 specified two surfaces for producing a compliance report — `matofx compliance report` **and** "an Admin → Licence & compliance page button, both producing the same artefact". Only the CLI was built; the page was deferred to slice 6's UI work and never written. So for two months the artefact a customer's supplier asks for could be produced only by someone with shell access to the server, which plenty of MATOFX administrators do not have. Overview §13 recorded it as gap 5, and it was the only open gap a customer actually felt.

**Three endpoints on the existing admin router**, gated on `audit.read` — the permission whose docstring already names the "compliance-focused role", and whose holder can read the entire audit log, which is strictly more sensitive than a report of aggregates and licence events. `GET …/compliance/reports` lists the archive plus the install identity; `POST` generates; `GET …/{counter}/document?fmt=json|html` downloads.

**Downloads come from the archive, not from a regeneration.** Reports were already persisted whole (`ComplianceReport.document`), so the bytes an administrator reads are the bytes that were signed. Regenerating to serve a download would produce a *different* document under a different counter — and the HTML twin's entire purpose is that it says exactly what the JSON says.

**The read-only exemption is the point, not an oversight.** §16.56 removed `/api/admin/compliance` from the allow-list because it exempted nothing while pre-approving whatever landed beneath it later. This is what landed beneath it. §B9 requires that generation never fails closed, and the two conditions that trigger read-only — an expired licence and a failed integrity check — are precisely the two a supplier asks for a report *about*; gating it would withhold the evidence at the one moment it is wanted. The admin help chapter had already promised this behaviour while it was still true only because the CLI bypasses middleware. Listed as the exact collection path rather than the `/api/admin/compliance` stem, so the next mutating route under compliance is not silently pre-approved. `test_readonly_allowlist.py` failed on the addition exactly as designed, and the entry carries its justification.

**A latent gap the page surfaced.** `ensure_install_identity` mints the install's own id (§16.57), but the only startup path calling it is licence binding — which **returns early when no licence is configured**. An install being commissioned before its licence arrives therefore had no identity, and generating would fail with *"Run 'matofx compliance adopt'"*: the command §A3's inversion superseded. The generate endpoint now ensures the identity itself. It is idempotent, and it makes §16.57's claim — *there is always a local identity* — true on this path too.

**Never fails closed, in the response shape.** A report recording a failed chain check returns **201 with a `warning`**, not an error: the report was produced and is exactly the evidence worth having. Only a failure to *build* one at all (no signing key) is a 409. The page surfaces the warning as a sticky notification and marks the row *check failed* rather than letting it read like a clean report.

**Verified against the running application**, not only in tests: the page serves, and the API lists the six reports an earlier CLI session had generated — one archive, one numbered sequence, two front ends — then generated #7 through the API and downloaded both halves with correct `Content-Disposition` filenames.

### 16.61 A licence changes mid-year: live features, and fail-closed for a running process (shipped 2026-08-12)

Licences are yearly, but they are also re-issued *during* a year to add users or features (maintainer, 2026-08-12). Running that flow against a live server — rather than reading the code — found two defects.

**Features were frozen at process start.** `make_app` resolved the licence once and baked the feature set into `Settings`, so a licence issued to switch a feature on did nothing until somebody restarted: silently, immediately after the administrator was told the licence installed successfully. The slot count from the *same file* applied at once, because `resolve_licence` re-reads it on every login and every mutating request. So "add users" worked and "add features" did not, which is the kind of asymmetry nobody discovers until a customer is on the phone.

`licence/live.py` resolves the feature set at request time, cached on the licence file's `(path, mtime_ns, size)`, so the steady-state cost is one `stat` and the signature is re-verified only when the file actually changes. Safe to make live because **nothing registers routers conditionally on features** — every gate is a per-request dependency reading `settings.features` (`_require_optimisation_feature` is the pattern) and the UI's `_features` helpers read the same object per render, so this changes *when* the answer is computed, not where. Both read points go through the one helper, so the sidebar and the routes it links to cannot disagree.

A file that stops verifying **holds the last good feature set** rather than falling back to TOML `[features]`. Falling back would mean tampering could *widen* what an install runs.

**A tampered licence did not fail closed on a running process — the more serious of the two.** `LicenceResolution.read_only` returned `not fail_closed`, on the reasoning that tampering is handled by refusing to boot. That holds at startup, where `check_licence_at_startup` raises; there is no boot in the middle of a process's life. Editing `n_slots` in the licence file of a running server therefore left writes flowing *and* removed the concurrency cap altogether — `status` is None for an unverifiable licence, so `slots.py` reports the install unmetered. Verified against a live server: `metered: False`, `n_slots: None`, writes returning 201.

That is the runtime form of the objection §16.57 raised against `environment = "dev"`: one number in a text file, with no restart, was a cheaper bypass than the ceiling §A4 knowingly accepts, and a model should not contain both. `read_only` is now true whenever no valid status could be resolved. Metering staying suspended is consistent rather than a residual hole — §C12 suspends CSL accounting in read-only deliberately, because an install that cannot submit work has no metered use to protect.

The middleware and `/api/site/mode` both distinguish `licence_invalid` from `licence`, because the two need different actions from an administrator: a lapsed licence is renewed, an invalid one is *restored*. Reporting both as "licence" sent someone to buy a renewal for a file that had been edited. Recovery is self-healing and needs no acknowledgement — put the correct file back and the site returns to normal, again without a restart. Read-only rather than a hard stop, so a site running all morning does not vanish because a file changed underneath it.

**Host migration, documented rather than built:** the identity lives in the database, so lift-and-shift, a new VM or a rebuilt container all keep the same `install_id` and the existing licence keeps working. Only a MATOFX installed against an *empty* database mints a new id and needs a re-issue — and its report counter restarts, which the vendor needs told so it is not read as a clone.

### 16.62 Two quiet defects in the evidence: a zero that lied, and a proof window that dropped the wrong end (shipped 2026-08-12)

**`licence.override` (overview gap 9).** The action sat in `LICENCE_EVENT_ACTIONS` marked RESERVED, never emitted. That would be harmless if the report merely omitted it — but `event_counts` **zero-fills the catalog** so a missing key is a signal rather than a schema difference, which means every report ever generated carried `"licence.override": 0`. To Funis reading the artefact that is not a placeholder, it is a statement: *no overrides occurred* — about a concept the build does not have and cannot record. A false assurance in the one document whose entire purpose is assurance. Removed. The two things §C16 might mean by "admin overrides" already have their own events (`licence.slot.breakglass`, `audit.verify.acknowledged`), and adding an action later is additive and needs no placeholder. `designs/examples/` regenerated.

**The proof window (overview gap 10).** `_consistency_proofs` selected `ORDER BY counter DESC LIMIT PROOF_CAP` — the hundred *most recent* attested tree sizes. Every report writes its own audit event, so no two reports share a tree size and the list only grows; past a hundred reports the oldest sizes silently stopped being provable.

Dropping the oldest is exactly backwards. The vendor anchors against whichever size **their ledger** holds, and a ledger holding an early entry belongs to a customer who has *delivered little* — the one who most needs to be able to prove anything at all. A customer who sent report 3 and then generated 200 previews would drop the size Funis was holding and verify `INCONCLUSIVE` for ever, with no route back. Safe, in that it never accuses; but it is the same practical dead end F1 produced by a different route, and this design's whole posture is that an honest install must never become unverifiable.

`select_proof_sizes` keeps the oldest and the newest and strides the interior, so thinning leaves evenly spread gaps rather than one enormous hole at the far end. Proofs are also deduplicated by tree size, since the verifier looks them up by size and a repeat is a second answer to the same question.

The spread narrows the odds; **`matofx compliance report --prove-from <size>` closes the case**. A vendor whose ledger entry falls in a gap asks for a report anchored to exactly that size, and the verifier's own "no proof from the N-record tree" message now names that command rather than only suggesting a re-baseline. That turns the one remaining failure into a request with an answer, which is the difference between a gap and a dead end.

### 16.63 Licence trouble becomes visible while it is happening (shipped 2026-08-12)

Two overview gaps that turned out to be one problem. **Gap 11**: nothing anywhere alerted, so an install over its licence was invisible until a compliance report was generated *and* sent — at the agreed one-or-two-a-year cadence, up to a year of latency on the fact a customer had outgrown their licence. **Gap 7**: break-glass was unlimited, so a site could sit over its cap indefinitely. The second only matters because of the first: an over-cap admission that somebody is told about is a conversation, not a leak.

**Break-glass stays unlimited in number, deliberately.** Capping the count was considered and rejected: two stale Super User sessions holding the cap would lock out the administrator break-glass exists to admit, reintroducing exactly the failure §C13 was built to prevent. The bound is on **duration** instead, where there is no such trade — admission is never refused, it simply does not last for ever. `BREAKGLASS_MAX_HOURS = 12` covers any plausible session spent fixing a licence and still expires within a day; the site's own hard timeout wins when it is shorter, so this only ever tightens.

That needed the session to know what it was, so migration **0093** adds `auth_session.breakglass`, set at `confirm_admission`. It earns its keep twice: `SessionService.validate` uses it for the time-box, and **Admin → Active sessions** now marks the over-cap rows — on the page whose entire purpose is getting back under the cap, which previously could not point at the sessions putting the site over it.

**Three alerts**, through the notifier the codebase already has: a break-glass admission, a sign-in refused because every place was in use, and the licence entering grace or expiry. Recipients are holders of `audit.read` — already the "trusted with this install's compliance evidence" right, and administrators hold it by default. Not the whole user base: an ordinary user can do nothing about a full licence except be alarmed by it.

**Every alert is best-effort and cannot fail its caller.** Two of the three fire on the sign-in path, and the break-glass one fires when a Super User is reaching a site that *already* has a licence problem — the last moment to introduce a new way for login to break. `_alert_licence` swallows everything and logs.

**Transitions only, never per tick.** The expiry alert fires from the runner's hourly licence tick but only when the *state* changes, because an hourly "still in grace" email for a month trains everyone to filter the one that matters. This is the same rule §16.53 applies to the chain-integrity events, for the same reason.

The new `licence_alert_emails_enabled` General Setting turns them off without silencing the underlying events — they remain in the audit log and in compliance reports either way, which is the distinction between a notification preference and evidence.

### 16.64 Machine binding on containers: a warning where somebody will read it (shipped 2026-08-12)

Machine binding compares an OS machine id (`/etc/machine-id`, `MachineGuid`), and a container rebuild mints a new one. So a machine-bound licence on a containerised install works perfectly until the first redeploy and then refuses to start — during a deployment, which is the worst moment to discover it. The guidance to avoid this existed only in ADR-0019 and in `machine.py`'s own docstring, neither of which an administrator will ever read (overview gap 8).

`looks_containerised()` checks the usual markers (`/.dockerenv`, `/run/.containerenv`, `/proc/1/cgroup`), and `matofx licence install` warns when a manifest carries a machine binding on a host that matches. **Advisory only, never a gate**: container detection is a heuristic in every implementation that exists, and a false positive that refused a valid licence would be far worse than a warning somebody did not need.

The other two audiences are covered where each will actually look: *Your licence* gains a "Machine-bound licences and containers" section telling the customer to ask for an unbound licence and explaining they lose nothing by it (`install_id` already stops a second deployment), and `funis-vendor-runbook.md` gains a "when not to use it" — the better fix being not to issue one.

### 16.65 The 16 "pre-existing" test failures were hiding three real defects (shipped 2026-08-12)

The full suite had carried 16 failures long enough to become furniture. The previous handover recorded them as *"pre-existing… they cluster in areas other sessions have uncommitted work in"* — accurate, and enough to stop anyone looking. Examining them found three genuine defects. The suite is now green: **3951 passed, 0 failed**, with random ordering on.

**`presets` was never diffed, so editing it did nothing.** The field is accepted by `AdminToolDescriptorUpdate` and persisted in the descriptor, but was missing from `versioning.METADATA_FIELDS` — so an edit changing *only* the presets diffed to nothing and `update_descriptor` short-circuited as a no-op, discarding the author's work behind a success message. `tests/tools/test_metadata_fields_invariant.py` exists precisely to catch this and had been reporting it correctly the whole time; the adjacent `dependencies` comment warns about the same trap, sprung a second time. See §16.16 and the descriptor-touchpoints map.

**Publishing a surrogate from an optimisation study raised `ValueError`.** `create_model` returns `(model, version, excluded)` since ADR-0017's per-output validity filtering; `optimisation/service.py` still unpacked two. The feature was simply broken. `excluded` now also lands in the audit payload — a surrogate published having silently dropped a response is exactly the thing somebody needs to be able to find out about afterwards.

**Alembic disabled the application's loggers.** `db/migrations/env.py` called `fileConfig(...)` without `disable_existing_loggers=False`, and that argument defaults to **True** — switching off every logger already created and not named in `alembic.ini`, i.e. the whole `matofx.*` tree. This is not a test concern: `matofx migrate` and the demo launcher's auto-upgrade both migrate **in-process** after the app is imported, so a migration silently took application logging out for the rest of the run. An operator upgrading a customer install would lose their logs with nothing to say so — the thing that would have reported the problem was the problem.

It presented as a UI test that passed alone and failed after anything that migrated a database, and had been written off across two sessions as *"an ordering artefact — it passes in isolation"*. That phrase describes the symptom and diagnoses nothing. `tests/db/test_alembic.py` now asserts a `matofx.*` logger is still enabled after `command.upgrade`, which puts the check where the cause is rather than where the symptom surfaced.

**Two more were a policy change misread as an authorization hole.** Migration 0083 deliberately reclassified `cohorts.create` and `materials.create` as base-tier grants seeded onto every role; the tests still asserted curator-only creation. Re-tightening the services would have reverted the decision. Both now assert the real rule and then **strip the grant to prove the gate still bites** — "everyone holds it by default" and "nothing checks it" are indistinguishable if only the allowed path is tested, which is how the expectations went stale unnoticed. This is the same fix a previous session applied to the V&V equivalent (§16.24's polish pass), so the reclassification has now cost three tests in three separate sessions.

**The rest asserted implementations the code had deliberately moved past**: the light palette darkens its accent for contrast on white (now a WCAG ratio check rather than string equality, and the misleading field docstring corrected); `_titlecase` capitalises author labels while preserving acronyms; KPI CSV headers use labels never internal names (now asserted in both directions); migration 0077's four runtime settings never reached the vocab guard; the surrogate orchestrator delegates to `run_surrogate_tool`. Two `format_number` rows had **never passed since the initial commit** — not a regression but an expectation written against a 4-significant-figure default the code has always had.

**Help documentation needed no change**, stated explicitly rather than skipped: all three defects were the code failing to do what the docs already described (presets editable, surrogates publishable from a study, logs written during an upgrade). The fixes restore documented behaviour rather than altering it.

**The lesson worth carrying.** A failure that is *explained* is not a failure that is *understood*. "Pre-existing", "other sessions' work" and "ordering artefact" are all true statements that answer a different question from "what is wrong". Every one of the three defects sat behind one of those phrases.

### 16.66 Stress-testing ADR-0019 again: four defects, three of them in a day-old build (shipped 2026-08-12)

A second adversarial pass, this time aimed at the code written on 2026-08-12 — vendor tooling, live features, runtime fail-closed, alerting — since slices 1–7 had already been attacked on 08-11. Four defects. The pattern from the previous pass held: **the ones that mattered were failures of the honest case or of a check that existed but did not run where it was needed.**

**A licence issued for another install worked on a running server.** The worst of the four. §16.57 built `verify_install_binding` for §A3's one-licence-one-install rule, and it runs at API startup and on the runner's hourly tick — neither of which is the request path. Copying a validly-signed foreign licence over the configured file on a live server applied its entitlements at once: a 10-slot install served **50 slots with writes flowing**, no banner, no error. It needs no forgery, only `cp`, and a customer running two installs already holds both files; the startup check would refuse only at the next restart, months away on a long-lived server. This is the third instance of one shape — §16.57's `environment = "dev"`, §16.61's tampered licence at runtime, and now this. The write gate compares the manifest's `install_id` against an id cached in `app.state` at startup (immutable, so a string compare rather than a query), and `/api/site/mode` reports it so the banner explains the 503.

**The live-features cache could miss a real licence change.** §16.61's cache keyed on `(path, mtime_ns, size)`. Timestamps are preservable — `cp -p`, `rsync --times`, a restore — and two licences differing only in a slot count are routinely the *same byte length* (10 → 50). Demonstrated: entirely different bytes, identical fingerprint. The stale value is the *old* one, so no attacker gains anything; the victim is an administrator who installs a licence and watches the features they just paid for fail to appear while the install insists all is well. Now keyed on a sha256 of the content — microseconds on a file that is permanently in page cache, against the Ed25519 verify it still avoids.

**Deleting the ledger's head marker hid a truncation.** §16.59 added the marker precisely because a hash chain cannot see a ledger cut short at the end, and treated its absence as "legacy, warn only". So *delete the marker, then truncate* verified clean. But `append_entry` writes a marker every time it signs, which makes the two cases distinguishable: a **signed** ledger cannot lack a marker by accident. Absent marker + signed entries is now a failure naming the likely cause; absent marker + unsigned entries stays the mild legacy warning it was meant to be.

**A licence alert could stall a sign-in for minutes.** §16.63's alerts fire on the login path. `notify_licence_alert` walks recipients sequentially and SMTP's timeout defaults to 30s, so an unreachable relay blocked the login for 30s *per administrator* — measured at exactly 30.0s for one. The case that makes it serious is break-glass: that alert fires when a Super User is signing in to a site whose licence is already broken, plausibly during the same outage that took the relay down. Bounded at 5s total (`_ALERT_BUDGET_SECONDS`) rather than detached, because the notifier needs the request's session and a background task would race its teardown. A hang reads to a user as an outage; a missing email reads as a missing email.

**Also:** `compliance report --prove-from <size>` silently accepted a size the install had never attested, so an operator would send a report believing it answered their supplier's request. It now warns.

**Confirmed as documented, not fixed:** a ledger rewritten *and re-signed with the real ledger key* verifies clean. That is ADR-0019's stated ceiling — keeping the key off the machine holding the ledger is an operational control, not a cryptographic one — and the stress test verifying it behaves exactly as the documentation claims is the point. Report-key reuse across renewals also holds, which the ledger's per-install key pinning depends on.

### 16.67 Slurm submission assumed a cluster shaped like ours (shipped 2026-08-13)

The first real deployment profile (`DEPLOYMENT_VALIDATION_PLAN.md` §2) contradicted the Slurm path in two places at once, and both would have surfaced as "MATOFX cannot submit to this cluster" on install day. The customer's cluster has **no partitions or QOS**, and charges every run to a project named with `-wckey`. `_build_sbatch_command` emitted no accounting flag of any kind — zero hits for `wckey` or `--account` in the source — while `ExecutionBackend` *required* a `queue` for `kind = "hpc_batch"`, so a descriptor targeting that cluster could not be authored without inventing a partition name that doesn't exist on it. Neither is exotic: accounting is how most shared clusters recover cost, and a single unpartitioned pool is the normal shape of a small one.

**Accounting rides the cluster row, not the tool** (migration `0094`, nullable `hpc_cluster.account` + `hpc_cluster.wckey`). The identity belongs to the relationship between this install and that cluster, and the admin registering the cluster is the person who knows it — putting it on the descriptor would make every tool author re-answer a question about somebody else's accounting system. Slurm's `--account` (an association in the accounting database) and `--wckey` (a free-form label used where no such association tree exists) are **separate identities, not aliases**, and a site may legitimately set both, so neither is modelled as the other's fallback and both are emitted independently. `NULL` emits nothing, which is byte-for-byte the command line every existing install already got. Both are shell-quoted — they cross SSH into a remote shell. The admin form shows them only for a Slurm cluster and clears them on a switch to MS-HPC, which has no equivalent directive, rather than storing values its scheduler would ignore.

**`queue` became optional for `hpc_batch`.** The requirement was never load-bearing: both HPC targets already omit the `--partition` / `-NodeGroups` flag when there is no queue, so the validator was rejecting a configuration the runtime handles correctly. A cluster's `default_queue` still fills it in where the site does partition, and a descriptor's `queue` still wins over that.

Also removed a dead `"--parsable" if False else ""` from the flag list, with the reason we don't pass `--parsable` written down instead: the human `"Submitted batch job N"` form is what the jobid parser reads, and it carries any warning Slurm prints alongside the id.

**Key files** — `tools/exec_orm.py` + migration `0094`; `tools/exec/hpc_slurm.py`; `tools/hosts_service.py`; `api/{schemas,routers/admin}.py`; `ui/pages/admin_paths.py`; `tools/descriptor.py` + `ui/pages/_descriptor_wizard.py`. Tests: accounting-flag emission, the customer's wckey-only-no-partition shape, and shell-quoting in `tests/tools/test_hpc_slurm.py`; persistence + blank-to-NULL in `tests/tools/test_hosts_service.py`; optional queue in `tests/tools/test_descriptor_extended.py`. Help docs: `admin/65-remote-execution.md` gains a "Charging jobs to a project (Slurm only)" section. **Neither flag has yet been sent to a real Slurm controller** — that is `DEPLOYMENT_VALIDATION_PLAN.md` S-05 / S-06 on ENV-A and ENV-B.

### 16.68 The UI stopped asking the internet for permission to draw (shipped 2026-08-13)

`designs/air-gap-asset-vendoring.md`, built because a customer confirmed a runtime air-gap (`DEPLOYMENT_VALIDATION_PLAN.md` P-03 / D-03). MATOFX was otherwise air-gap-clean — no telemetry, offline-signed licences, everything remote off by default — but five `<script src="https://…">` references meant the *browser* still reached out. That failure mode is the nasty one: the page loads, the login works, and one feature renders a blank rectangle with nothing in any log. Until this landed, "no external JavaScript required" was not a claim we could make.

**Vendored, not proxied.** Cytoscape 3.30.0 + dagre 0.8.5 + cytoscape-dagre 2.5.0 (lineage graph, workflow-structure graph) and `<model-viewer>` 3.5.0 (animated glTF/GLB preview) are checked in under `ui/assets/vendor/` and served from `/matofx-assets/vendor/…` by the existing `static_assets` mount. Hatchling bundles everything under `src/matofx`, so they travel in the wheel with no packaging change — verified by building one and listing it. No build step, no minifier, no npm: exact upstream bytes with their source URL, SHA-256, and licence recorded in `ui/assets/vendor/README.md`.

**The trap was model-viewer's lazy decoders.** It reads `self.ModelViewerElement.dracoDecoderLocation` / `.ktx2TranscoderLocation` at module-evaluation time, defaulting both to `https://www.gstatic.com/…`, and only *loads* them when a model actually uses Draco mesh compression or KTX2 textures. So vendoring the viewer alone passes every offline test and then fails on the first Draco-compressed `.glb` a user uploads — which is what most glTF exporters emit by default. The Draco 1.5.6 decoder and the pinned basis transcoder are vendored too (`vendor/draco/`, `vendor/basis/`, fetched from the exact URLs model-viewer names, since builds differ between distributions), and both locations are set in a `<script>` emitted *before* the module tag. An earlier comment claiming `add_head_html` deduplicates was wrong and is corrected: it appends unconditionally, which is harmless here only because the browser's module map keys on URL and the config block is idempotent.

**Plotly is inlined from the installed package, not vendored as a file.** Both consumers — `ui/theme.py:export_plotly_html` (was `include_plotlyjs="cdn"`) and the compare report (was a pinned `cdn.plot.ly` URL) — now take the bundle from `plotly.offline.get_plotlyjs()`, the pattern `reports/builder.py` already used. This removes a whole class of maintenance ("bump the CDN pin in lockstep") and fixed a live defect on the way past: the compare report pinned plotly.js **2.35.2** while plotly 6.7.0 serialises **3.5.0** figure schema — a full major version apart, with no mechanism that would ever have noticed. Downloads grow from ~5 kB to ~4.8 MB, which is the right trade for a file whose entire purpose is to leave the application and be opened somewhere else.

**Two messages changed, because the diagnosis changed.** The cytoscape load-failure copy blamed the user's network for a failed download; the library is now served by MATOFX itself, so it says the server didn't serve it and points at reporting rather than at a firewall. And `<model-viewer>` gained a `<p>` fallback child — the design had this as "optional polish", but the deployment plan's own G-06 criterion is that nothing may degrade silently, and a viewer that fails to upgrade collapses to a blank rectangle indistinguishable from an empty model.

**The regression guard is the durable part.** `tests/ui/test_no_external_assets.py` parses every UI module and fails on an external URL in any *live* string literal. It strips comments and bare string-expression statements (module, class, function **and** attribute docstrings) so prose stays free to name a CDN while explaining why we dropped it — a guard that can't tell code from commentary would force you to mangle the explanation. It also asserts it isn't vacuous (a probe URL must be seen, the same URL in a docstring must not), that `pages/demo.py`'s exemption is still *needed*, that each vendored file is present and non-trivial, and — through the real `StaticFiles` mount over ASGI — that the URLs resolve and `.wasm` is served as `application/wasm`.

**Known residual, recorded rather than hidden:** plotly.js carries a built-in `topojsonURL` default of `https://cdn.plot.ly/un/`, used only by geographic map traces (`scattergeo` / `choropleth`). MATOFX produces none, so nothing fetches it, but the string is inside the inlined bundle and a scanner looking at bytes rather than requests will flag it. Stated in `INSTALL.md` so it surfaces as a known answer rather than a surprise finding.

**Key files** — `ui/assets/vendor/**` (new, 16 files incl. licences + README); `ui/_cytoscape.py`; `ui/cad_preview.py`; `ui/theme.py`; `ui/pages/compare/report.py`. Tests: `tests/ui/test_no_external_assets.py` (new), plus updated URL + error-copy assertions in `tests/ui/test_lineage_view.py` and an inline-plotly assertion in `tests/ui/compare/test_report.py`. Docs: `admin/10-installing-and-setup.md` gains "Running with no internet access"; `INSTALL.md` states the runtime claim and the residual. **Not yet verified in a browser with egress blocked** — that is `DEPLOYMENT_VALIDATION_PLAN.md` G-01…G-05 on ENV-B.

### 16.69 The Funis issuer keys exist, and live in source (shipped 2026-08-13)

`TRUSTED_ISSUER_KEYS` had been an empty dict since ADR-0019 shipped, which meant the entire licence subsystem — seven slices, a vendor runbook, a gap-closure pass — had never once run against a key anybody actually held. A licence issued the day before this landed would have failed verification everywhere. `DEPLOYMENT_VALIDATION_PLAN.md` P-08 / P-09 / G19 / G20.

**Custody was decided before anything was minted**, and that ordering is the point: generating a key into an undecided location and moving it afterwards is how copies get left behind in shadow copies, AV quarantine, and shell history. `designs/funis-key-custody.md` (new) records where each of the four keys lives, what each loss costs, and the routine that touches them. Minting went **straight onto removable media**, so no private key ever touched the dev box's disk; the passphrases were entered by the maintainer in his own terminal and never crossed into tooling that logs.

**The keys live in source, not in a build step — a reversal of what the code claimed.** `issuer.py` said release engineering populated the map "at artefact-build time"; `scripts/build_bundle.sh` has no such hook and never had one, so the promised mechanism did not exist (only the runbook's "paste into `issuer.py`" ever did). Rather than build the injection step, we chose the constant, for a reason worth keeping: **a constant cannot silently fail to apply.** An injection step that no-ops produces an artefact that trusts *nobody*, and its symptom is a customer's site refusing its identity at go-live — which is precisely the failure this key material exists to prevent. There is no secrecy cost either way; these are public halves.

**The accepted consequence** is that `licence.loader.issuer_required` — which keys off `bool(TRUSTED_ISSUER_KEYS)` as its "is this a release artefact?" test — is now `True` for every run of this tree. The escape hatch that kept a source checkout tolerant of unsigned licences is gone for this repository. The blast radius was **measured before committing to it, not estimated**: exactly one test failed, the one asserting that tolerance. It was rewritten to assert the new behaviour, and a second test now covers the key-less branch via monkeypatch so a fork's path stays covered. Local development is unaffected in practice because an install with no `[licence].file` is never degraded at all (`api.readonly._licence_enforced`).

**A guard, because every failure mode here is silent.** `tests/compliance/test_issuer_keys_baked_in.py` pins that the map is populated, that both the working *and* backup ids are present, that each value is a valid Ed25519 public key, that the two are distinct, and that no placeholder or test-stub value survived. Emptying the map, dropping the backup, or pasting the same hex twice all look plausible in a diff, break nothing in CI, and cost a customer their go-live. None of it is caught by the licence tests, which monkeypatch the map to whatever they need.

**Vendor tooling gained `ledger.py pubkeys` + `backup --public-key`.** The ledger's public halves could previously only be obtained by decrypting the private keys, and `backup` accepted only `--keys` — so with the ledger key on removable media (as custody requires), every routine monthly verification would have dragged it back onto the machine holding the ledger. That is the "key off the ledger machine" rule inverted by its own tooling. Resolution order is unit-tested: recorded hexes win, an explicit `--keys` is still honoured, and the default key directory is consulted only when no hexes were supplied — so passing them never triggers a passphrase prompt for keys the caller already has.

**Key files** — `compliance/issuer.py` (populated + docstring corrected); `licence/loader.py` (docstrings, no logic change); `scripts/ledger.py`; `designs/funis-key-custody.md` (new); `designs/funis-vendor-runbook.md` §1/§4. Tests: `tests/compliance/test_issuer_keys_baked_in.py` (new); `tests/licence/test_live_features.py` (one test inverted, one added). **No licence has yet been issued to a real install** — that is `DEPLOYMENT_VALIDATION_PLAN.md` L-06 on ENV-B, and it is what turns all of this from unit-tested into proven.

### 16.70 Tools were asking the scheduler for nothing they declared (shipped 2026-08-13)

Found on ENV-A, the first environment where MATOFX has ever submitted to a real Slurm controller — and found only because `sacct` reports what the controller was *actually asked for*, rather than what we believed we sent. A tool declaring `cpu_cores=1, ram_mb=256` arrived as `--cpus-per-task=1 --mem=512M` with no `--time` at all. Those are `ResourceRequest`'s defaults. **Every HPC job ever submitted asked for one core, 512 MB and unlimited time, whatever its descriptor said** (`DEPLOYMENT_VALIDATION_PLAN.md` D-11, S-04).

**Three faults compounding, none visible from inside the unit tests.** Nothing on the install path populates `Tool.resource_request` — `install_from_path` and `tools/service.py` have zero references to it, and the only writer is the cross-install `migrate` import, which can merely propagate a value that was already there. `_resource_request_for` read that column and never consulted `descriptor.resources`. And the key names disagreed three ways: the descriptor says `cpu_cores`/`ram_mb`/`gpu_memory_mb`, the reader wanted `cores`/`memory_mb`/`gpu_count`, and the ORM docstring advertised `cores`/`memory_gb`/`walltime_hours` — units nothing has ever read. Every existing test passed a `ResourceRequest` in by hand, so all three were invisible.

**Resolution order is descriptor first, override on top.** `Tool.resource_request` keeps its meaning as an operator override and still wins *per key*; where it is silent — the normal case, since it is normally `NULL` — the tool author's declaration shows through. That preserves the migrate-import behaviour rather than trading one silent surprise for another.

**`resources.walltime_minutes` is new, optional, and deliberately has no default.** There was nowhere to declare a wall-clock limit at all. It lives on the descriptor because the tool's author knows how long their solve takes, where an administrator setting it per-install would be guessing. It stays optional because a default would silently kill legitimate long solves on installs that never asked for one — and `--time` is emitted only when set, so a tool that declares nothing behaves exactly as before.

**The GPU mapping is lossy on purpose.** The descriptor states GPU *memory*; `--gres` and admission both count whole devices. Only the sign is used — any positive requirement asks for one GPU — which matches how the scheduler already routes GPU work. An override naming `gpu_count` says exactly what it means and wins.

**What this cost, had it survived:** a multi-node CFD tool asking 16 cores would have run on one (T-08); a tool asking 64 GB would have been OOM-killed by the scheduler with a failure that reads as the tool's fault (S-11); and — the one that changes a written risk assessment — a runaway optimiser could not be contained by Slurm, because Slurm only enforces a walltime it is given. The deployment plan's G46 had claimed exactly that containment; it is corrected there, twice, and the correction history is left visible.

**Key files** — `runner/supervisor.py` (`_resource_request_for` + `_declared_resources`); `tools/descriptor.py` (`ResourceRequirements.walltime_minutes`); `tools/orm.py` (docstring). Tests: `tests/runner/test_resource_request.py` (new, 14 cases — descriptor→request, per-key override precedence, bool-is-not-an-int, malformed override, missing `resources` block). Help: `admin/30-adding-tools.md` gains "Saying what the tool needs to run (and how long it may take)". Verified end-to-end on ENV-A: a tool declaring `cpu_cores=3, ram_mb=1536, walltime_minutes=7` was recorded by the controller as `ReqCPUS=3 | ReqMem=1.50G | Timelimit=00:07:00`.

### 16.71 Two more the deployment shape found for us (shipped 2026-08-13)

Both came out of ENV-A — the first environment where MATOFX has run as an installed wheel, as a non-root service account, under systemd, on Linux. Neither is reachable from a Windows dev run, and neither was caught by a test suite that passes every value in by hand.

**An optional demo surface stopped the whole API from starting.** `mount_demo_static_route` called `root.mkdir()` unconditionally. On an installed wheel `resolve_demo_root()` falls back to `cwd / "demo_assets"`, and a systemd unit with no `WorkingDirectory=` runs with cwd `/` — so the service account tried to create `/demo_assets`, got `PermissionError`, and it propagated through `mount_ui` → `make_app`. **The entire product taken down by a marketing page**, on exactly the production shape. The mkdir is now best-effort and the mount is *skipped* rather than half-made — `StaticFiles` raises on a missing directory too, so mounting anyway would only move the crash. The demo pages already render an empty state when there is nothing to list. Units also set `WorkingDirectory=`, not because the code needs it any more but because the next relative path will.

**A tool's output was invisible until its job ended.** The launcher invoked the tool as `[interpreter, "-B", script]`. Python block-buffers when stdout is a file — which, under the launcher, it always is — so `stdout.log` stayed **empty for the entire run** and appeared complete at exit. Measured rather than inferred: a 90-second job wrote its log at the 90-second mark. Live log streaming therefore showed nothing on precisely the long jobs people most want to watch, and an operator could not distinguish a working solve from a wedged one. Adding `-u` costs a write syscall per line against a file on the shared mount, which is the right trade by a wide margin.

**Two related defects are open, deliberately** (`DEPLOYMENT_VALIDATION_PLAN.md` D-10, D-12). A job whose launcher never starts can sit at `running` for ever: `poll` falls back to `squeue` then `sacct`, and where accounting is not configured `sacct` returns nothing, which `_scheduler_state` reports as `None` and `poll` treats as *unknown*, therefore non-terminal. It is the failure mode where silence is least acceptable, and the fix needs a judgement about how long "unknown" may be tolerated rather than a patch. Separately, the terminal-state message is always the same three-way guess — "walltime, memory, or manual scancel" — though `sched_state` already says which; tailoring it per state is cheap and pending.

**Key files** — `ui/pages/_demo_assets.py`; `tools/exec/launcher.py`. Tests: `tests/ui/test_demo_assets.py` gains an uncreatable-root case. Both verified on ENV-A rather than only in unit tests: the API starts, and `stdout.log` carries the tool's first line while Slurm still reports `RUNNING`.

### 16.72 The buffering fix stopped at the WSL boundary (shipped 2026-08-14)

§16.71 added `-u` to the launcher and called the invisible-output problem solved. It was solved for one process. Two jobs were cancelled by hand the next morning in the belief that WSL had broken; neither had. Both were healthy, and both were cancelled **because they could not show it**.

**`-u` does not cross an interpreter boundary.** `simple_fermentation` re-execs itself inside WSL — the FEniCSx solver only exists in a Linux conda env — as `exec python -B <script>`. The launcher's `-u` applies to the *Windows* pass; the Linux process is a **fresh interpreter** whose stdout is the inherited log file, so it block-buffers exactly as before. The tool emits a detailed per-step line (`step 1450 | t = 79.26 h (60.7%) | T_avg = 30.8 C`); not one of them reached disk until the run ended. Measured both ways: unfixed, zero progress lines at the 30-second mark of an 85-second run; fixed, 41 lines already on disk 20 seconds in, while the run was still going. **The general rule: every hop that starts a new interpreter needs its own unbuffering. A fix applied at the outermost process is not inherited by anything it launches.**

**A captured subprocess is a silent one.** `batch_mixing` ran its OpenFOAM stage through `subprocess.run(capture_output=True)`, so nothing at all appeared for the entire solve — and the OpenFOAM tool logs it might have shown were written inside the WSL-native stage directory, invisible from Windows until the run finished and copied them back. The job page showed one line at +1.9 s and then silence for twenty minutes. `_run` now takes `stream=True`, teeing the child's output line by line, and `run_openfoam.sh` emits a `[stage]` marker per step and `tee`s the solver's log to `work/case/log.run` on the shared mount so the residuals are readable *while they are being produced*. The evidence that this mattered: the cancelled run had reached `Time = 10.5` of a 30-second `end_time` with residuals converging cleanly — roughly 35% through a healthy solve, about 55 minutes from finishing.

**Cancelling looked identical to crashing.** `JOB_STATES` has no `cancelled` member, so `_cancel_job` drives a cancelled run to `failed` — correct as a lifecycle decision (the run did not produce results), but the job page then said only *"This job didn't finish successfully — see the logs below"*. The logs' last stderr line was `wsl: Failed to translate 'C:Python311\Scripts'` — a harmless warning from a malformed entry in the machine `PATH`, present on every WSL call including successful ones, which reads exactly like a cause. Rather than add a state and a migration, the job page now reads `cancel_requested_at` (already on the API response) and says *"Stopped by a cancel request at &lt;time&gt;"*, and the `runner.job.cancelled` audit event carries an explicit `reason`. **A terminal state that conflates two causes must explain which one happened, or the operator will find an explanation in the logs whether or not one is there.**

**A cleanup that only runs on success is not cleanup.** `run_openfoam.sh` reached its `rm -rf "$STAGE"` only on the happy path, so every cancelled or failed run abandoned a full staged OpenFOAM case in the WSL filesystem — 18 of them, 23.7 GB, going back five weeks. It is now a `trap ... EXIT` that copies the logs out to the job directory *before* deleting, so the forensic record of a killed run survives the cleanup that removes its bulk.

**Key files** — `demo_run/tools/simple_fermentation/2.2.0/main_script.py` (`-u` on the inner interpreter); `demo_run/tools/batch_mixing/8.0.0/batch_mixing.py` (`_run(..., stream=True)`) and `openfoam/run_openfoam.sh` (stage markers, `tee` to the case dir, EXIT trap); `runner/supervisor.py` (`_cancel_job` reason); `ui/pages/job.py` (`_render_failed`). Help: `user/30-tracking-results.md` gains the cancelled-lands-in-Failed explanation and a "Before you cancel a long job" section.

### 16.73 Design-space coverage, slice 1: the engine, and what writing the tests first found (shipped 2026-08-24)

ADR-0020 introduces coverage as a first-class concept because **Q² actively conceals
poor coverage**: it is leave-out cross-validation on the points that exist, so a tight
cluster scores well precisely because every held-out point has a near neighbour. High
Q², no coverage, and the user is then invited to publish, export-as-tool, and optimise
against that model. Slice 1 is the **response-agnostic, model-free** half —
`matofx/studies/coverage/`: point-set resolution, the declared envelope, the model-free
metrics, split-or-pool diagnostics, and the report. Deliberately **no GP**, so the path
cohort preview and every cold-start case depend on stands alone and is testable without
a fitted model.

`designs/design-space-coverage.md` §19 recommended writing slice 1's tests before any
more design, on the argument that executing against the real types would find interface
mismatches faster than re-reading. It did — **eleven**, two of them provable only by a
failing test.

**`TrainingData` could not express the thing the ADR exists to catch.**
`collect_training_data` builds `factor_columns` from `_as_number(value) is not None`, so
a varied `select` / `bool` / `file` input **never reaches the training data at all**.
Categorical imbalance — 40 CFRP runs and 2 GFRP, which looks perfectly covered on every
continuous axis — is the *silent* one of ADR-0020's three diseases, and it was
unmeasurable as specified. Separately, `FactorFilter` drops were bare `continue`s, so
"fitted on 62 of 100" had no breakdown to offer. Both are now additive fields
(`categorical_columns`, `n_excluded_filtered`) on `TrainingData` **and**
`FieldTrainingData`, expanded per `(run, timestep)` on the field path so the two
collectors stay symmetric. Every existing caller is untouched.

**Three mismatches were in files an earlier review pass had already read closely.**
`time` is a **0-based snapshot index, not seconds** — the design illustrated the derived
axis with "a field predicted at t = 5000 s from runs that soaked for 600 s", a sentence
the platform cannot produce, because it has no seconds to quote; every derived-axis
string now speaks in snapshots. Integer-ness is a **descriptor** fact (`InputFieldType`),
not a `FactorSpec` one (`numeric | select | bool | file`), so `build_envelope` takes the
`ToolDescriptor` — and integer-ness is never *guessed* from whole-valued observations,
because a thickness that happens to have been run at 1, 2, 3 mm is not a lattice. And
`source_kind` in the design's report dataclass (`scenario | parametric_study | …`)
matched nothing in the codebase; the real vocabulary is
`Literal["parametric", "optimisation", "cohort"]`.

**One finding was a genuine self-contradiction in the design.** §3.3 says a fully
occupied `[1,5]` lattice is *completely covered*; §5.1 says suggestions are generated on
the lattice. But the **joint** covering radius of §4 samples candidates over the
envelope — and if those candidates are continuous it measures distance to points like
ply count 2.7 and reports holes at 1.5 and 2.5, so the headline number would call the
design gappy in the same breath as the per-factor row called it complete.
`envelope.sample()` therefore snaps integer coordinates from slice 1, and §5.1 inherits
the rule rather than introducing it.

**The lesson is narrower than "read more".** §19 named three unread areas as the likely
next mismatch; none of them was where the breaks were. A type read for one purpose is
not a type understood for another, and only executing against it closes that gap.

Also: `insufficient` is a distinct verdict from `poor` throughout — a degenerate point
set returns it with a plain-English message and **never raises**, because "we cannot
tell" is an answer and raising would deny the user one. Correlations are omitted
(`None`) rather than zero-filled below two continuous factors, for the same reason.
Thresholds all live in `CoverageSettings`; the engine hardcodes no numbers.

**Key files** — `studies/coverage/{points,envelope,metrics,diagnose,report,errors}.py`;
`studies/surrogate/ingest.py` (the two additive fields); `config/models.py`
(`CoverageSettings`); `features.py` (`design_space_coverage`). Tests:
`tests/studies/test_coverage_*.py` (148). Help: slice 1 has **no user-facing surface**,
so only `admin/140-reference-and-glossary.md` changes (the new feature switch); the
user chapter on reading a coverage verdict ships with slice 3, which is the first slice
that draws one.

### 16.74 Design-space coverage, slice 2: the GP pass, and the sentence it falsified (shipped 2026-08-24)

Slice 2 adds the per-response half of ADR-0020 — `analyse_responses`, the variance
field, greedy sequential suggestion, and method-driven criterion selection — plus
`ResponseModel`, which is deliberately not a `Surrogate` because a **field** response is
fitted per POD mode and has no single GP to interrogate.

**The design's most load-bearing sentence was false.** ADR-0020 §5 and design §5 both
rest on *"σ contains no `y` term ... it depends only on where the design was sampled"*.
That is true of a textbook GP. It is **not** true of the one this codebase fits:
`_make_estimator` builds `GaussianProcessRegressor(..., normalize_y=True)`, so
`predict(return_std=True)` multiplies its standard deviation back up by the training
response's spread before returning it. A fifteen-line probe settled it in seconds — `y`
and `3y` fit the *identical* kernel and return σ in exact 3:1 ratio.

Three things broke at once. A configured threshold on σ means something different for
mass in kg than for peak temperature in K. Two responses could not be compared, which is
the entire purpose of the per-response pass. And the field aggregate would have been
wrong invisibly: POD mode 1 carries the largest coefficients by construction, so
weighting **raw** σ by mode energy counts that mode's magnitude twice and turns the
aggregate into a restatement of the energy spectrum rather than a measurement of
coverage. Everything now measures **relative σ** — `σ / std(y_train)` — which is `y`-free
under a rescaling, dimensionless, and the only form in which a configured threshold means
the same thing twice.

**Freezing the kernel was necessary but not sufficient.** Greedy selection conditions on
a candidate as though it had returned a value, and the "plan a whole batch with zero
solver calls" claim requires that the made-up value cannot matter. `optimizer=None`
alone does not deliver that: with `normalize_y=True` still on, a pseudo-response of 10⁶
changes `std(y)` and rescales every σ. `Surrogate.conditioned_on` therefore refits with
`normalize_y=False` on an already-standardised response — verified `y`-free to eight
decimal places against pseudo values of 0 and 10⁶ — and carries `y_scale_override=1.0`
so nothing divides by the spread a second time.

**One finding ran the other way.** The implementation grew a
`suggest_points(..., existing=)` argument; design §5 had explicitly forbidden it —
*"Existing points come from the surrogate's own training data; there is no separate
`existing` argument to fall out of step with it."* A test asserting that already-run
lattice points are never re-proposed failed, because the caller had not passed the list.
Already-run points are now read from the model's own `training_inputs`. It is the first
case in this feature where the design caught the implementation rather than the reverse.

Also: `poly` and `gbm` responses select the model-free criterion and report
`available_statuses == ("inside", "outside")` — `edge` is not computable without a
posterior variance, and saying so beats silently never returning one. A field response
with **any** non-GP mode falls back wholesale rather than aggregating a partial set.
`ResponseCoverage.ard_relevance` finally surfaces what the ADR named as the GP
criterion's whole advantage.

**Key files** — `studies/coverage/{gp,responses}.py`; `studies/coverage/envelope.py`
(`denormalise`); `studies/surrogate/__init__.py` (`has_variance`, `y_scale`,
`ard_length_scales`, `conditioned_on`, `y_scale_override`). Tests:
`tests/studies/test_coverage_{gp,suggest,responses}.py` (59). Help: still **no
user-facing surface** — slice 2 is engine, and the user chapter ships with slice 3.

### 16.75 Design-space coverage, slice 3: the cohort-preview panel (shipped 2026-08-24)

The first surface. `CohortService.coverage_preview` →
`POST /api/cohorts/{id}/coverage` → a panel on the cohort detail page: the envelope
form, the verdict card, the per-factor strip and the pairwise scatter matrix (ADR-0020
§11). Read-only and open to anyone who can see the cohort — committing runs from the
suggestions is a separate right and belongs to slice 4.

Notably **quieter than slices 1 and 2**: four findings, none of them a false claim, all
of them plumbing. That is the expected shape once the engine's contract with the
codebase has stopped moving, and it is the first slice where that has been true.

**The preview needs a training set before a response exists.**
`collect_training_data` requires a `ResponseSpec`, but §2.1's whole point is that the
design pass is response-agnostic — asking after a response was chosen would mean fitting
a model to decide whether to fit one. The preview therefore passes a spec naming no
response: only `TrainingData.response` depends on it, and the factor columns the design
pass measures are identical either way. The consequence is stated rather than hidden —
a per-response pass at fit time may train on *fewer* points than the preview measured,
which is the honest difference between "could this teach anything" and "what did this
particular output learn".

**A verdict cannot be turned back into a scatter plot.** §11.4 calls the pairwise matrix
"the visual payoff", but `CoverageReport` carried only measurements. It now carries
`design_points`, so one call answers the page.

**`CoverageError` reached the user as a 500.** An inverted envelope is the user's own
form input and its message was written to be shown verbatim — but the error was not in
`api/errors.py`. Two separate lists needed it: the status map *and* the handler
registration tuple. Adding only the first is silently inert, which is how the first
attempt still returned 500.

**Two feature gates, not one.** The cohorts router is already gated on
`surrogate_family`; the coverage route carries `design_space_coverage` as well. An
install can have cohorts and surrogates without having bought coverage, and one gate
would have to choose which of those two customers to get wrong.

The panel's wording lives in `ui/_coverage_panel.py` as pure helpers rather than inline
in the page, because the wording is where `feedback_users_are_not_modellers` actually
bites and it needs to be testable: no "Q²", no "VIF = 8.4", no "covering radius". A
derived axis speaks in **snapshots**, never seconds. `insufficient` gets a neutral chip
rather than a red one — "we cannot tell" and "we can tell, and it is bad" must not look
alike.

**Key files** — `studies/cohort/service.py` (`coverage_preview`);
`api/routers/cohorts.py` + `api/schemas.py` + `api/errors.py`; `ui/_coverage_panel.py`,
`ui/_features.py`, `ui/api_client.py`, `ui/pages/cohorts.py`. Tests:
`tests/studies/test_coverage_cohort_preview.py` (11),
`tests/api/test_cohort_coverage.py` (10), `tests/ui/test_coverage_panel.py` (25). Help:
`user/90-cohorts-and-surrogates.md` gains "Is this cohort fit to learn from?" — the
first slice with a user-visible surface, so the first with a user chapter.

### 16.76 Design-space coverage, slice 4 — and what 254 real runs said about the maths (shipped 2026-08-24)

Slice 4 completes the demo beat: "suggest runs to fill the gaps" → preview → **create a
parametric study**. `CohortService.coverage_suggest` (open, read-only) and
`create_study_from_coverage` (gated on `parametric_studies.create`, hidden rather than
disabled in the UI), landing as a **draft** `table`-mode study so somebody reviews the
batch before any solver time is spent.

Then it was pointed at real data — 254 completed `bracket_stress` runs from the NAFEMS
chain, 12 varying factors — and **the maths was wrong in two ways that no unit test on a
two-factor design could have found.** Both were found the same way: by asking what the
engine says about an input whose right answer is already known, and by racing it against
a null.

**The coverage score was measuring the number of factors, not the design.** It referenced
the covering radius against the unit hypercube's half-diagonal, `sqrt(d)/2`. That looks
dimension-aware and is not: an achievable covering radius grows with dimension far faster
than `sqrt(d)` does. Scored against a *perfect* 254-point Latin hypercube — the best
design that many runs can be — the verdict read `good` at 2 factors, `thin` at 4, and
**`poor` from 8 up**. That is precisely §7.1's always-red light, arriving through a door
the design never anticipated. The yardstick is now `reference_radius`: what the same
machinery achieves laying out the same number of points evenly over the same envelope, so
the score answers *"is this as well spread as this many runs could be?"* A perfect LHS now
reads `good` at every dimension from 2 to 12, and a corner cluster `poor` at every one.

**The greedy optimised the wrong criterion.** "Take the candidate furthest from everything
chosen so far" is a **maximin** rule. The covering radius is **minimax**. They nearly
coincide in two or three dimensions and come apart badly above about six, because maximin
drives every pick onto a corner of the cube — maximally distant from everything, covering
almost none of the interior the radius is dominated by. Against 200 random batches of the
same size the old walk won **100 % at two factors and 0 % at twelve**: worse than picking
at random, on the very number the panel headlines. Each pick now minimises the *resulting*
covering radius (greedy k-centre), which wins 97–100 % at every dimension tested, measured
on an **independent** candidate sample.

On the real archive the effect is direct: coverage improvement from a batch of 8 went from
15.4 % to **27.9 %**, and the batch went from beating 66 of 200 random batches to beating
**200 of 200** — better than the best random batch, not merely the average.

Two smaller findings from the same run. Suggestions were planned over one candidate sample
and measured against another, which with two probe points per axis barely overlap; they
now share the report's seed. And a joint radius over 4096 candidates in 12 dimensions *is*
two points per axis, so the report now carries a `coarse_estimate` warning pointing the
reader at the per-factor rows, which stay exact however many factors there are.

**What the archive got right, unprompted.** The engine found 13 varying factors and 5
pinned ones; flagged the six axes where 253 of 254 runs share a single value as `poor`;
reported `max |r| = 1.0` and a condition number of 2.5e47, because those six axes flip
together on the same one run and are perfectly collinear; and the suggestions spread
across every one of them (`[0.24, 0.97]` normalised) instead of sitting with the crowd.
That is the ADR's three diseases — sparsity, confounding, imbalance — all three detected
on data nobody prepared for it.

**Key files** — `studies/coverage/gp.py` (`suggest_design_points`, greedy k-centre),
`studies/coverage/metrics.py` (`reference_radius`, `candidate_resolution`),
`studies/coverage/report.py` (score + `coarse_estimate`), `studies/cohort/service.py`
(`coverage_suggest`, `create_study_from_coverage`), `api/routers/cohorts.py`,
`ui/pages/cohorts.py`. Tests: `test_coverage_suggest_design.py` (23),
`test_coverage_study_from_suggestions.py` (10), plus API (5) and UI (5) and the two
dimensionality regressions in `test_coverage_metrics.py`. Help:
`user/90-cohorts-and-surrogates.md` gains the suggest-runs section.

### 16.77 Design-space coverage, slice 5: the verdict becomes evidence (shipped 2026-08-24)

Coverage stops being something someone looked at once. Migration **0095** adds
`coverage_report_json` + an indexed `coverage_verdict` to `surrogate_model_version`; the
report is computed at fit and travels with the version, so the model record, the per-job
report and the V&V trail read one answer instead of each recomputing it and drifting
apart. Plus the §9.1 cohort-preview cache, and full JSON round-tripping for the report.

Four findings. **Two of them settle questions §19 had carried as unverified since the
first draft, through four slices.**

**The refit lifecycle — confirmed.** §9 assumed a refit creates a new version rather than
mutating one; nothing had checked. It does: `refit` calls
`_add_version(ordinal=latest.ordinal + 1)`. A stored report therefore always describes the
fit it was computed for, there is no path by which one goes stale, and that is why no
invalidation logic exists on the version. A test now pins it.

**The cache key — assembled, with a correction that mattered.** Keying on the cohort's
stored *definition* would have been the obvious reading of §9.1, and would have
reintroduced the exact failure this feature exists to report: an `auto_grow` cohort's
membership is a **query result**, so a definition-keyed cache keeps serving yesterday's
verdict after new runs start matching. The key is the *resolved rows*, plus the declared
envelope **and** the verdict thresholds — both change the answer, and a verdict reached
under different floors is an answer to a different question. Bounded and LRU-evicted,
because a dict keyed on a moving membership has no ceiling of its own.

**`coverage` was already taken — twice.** ADR-0008 §7.3 owns
`ModelService.coverage_for_version` *and* `GET /models/versions/{id}/coverage`, both
meaning the in-form training-*range* gauge. Reusing either name would have silently
changed what an existing surface shows. The new ones are
`coverage_report_for_version` and `GET /models/versions/{id}/design-coverage`. Two things
in this codebase are called coverage; they now stay separately correct.

**The fit path discarded exactly what the feature exists to catch.** `_fit_responses`
returns only the fitted surrogates, and a `Surrogate`'s `training_inputs` is numeric-only
— so a report rebuilt at fit would have been blind to a 40-CFRP / 2-GFRP split, precisely
the gap finding 21 closed on the cohort path four slices earlier. The same mistake, in a
second place, found by writing the test that asserts a categorical imbalance survives the
fit. `_FitResult` now carries the `TrainingData` the fit consumed.

Two smaller decisions worth recording. The stored report deliberately **omits** the
training points and the nearest-neighbour distances — both are live views of a cohort the
version already references, and a stored copy would bloat the row while going stale as the
cohort grows. It deliberately **includes** the verdict *thresholds*, which are
config-tunable: a bare `poor` means nothing a year later without the floor that produced
it. And computing a report never fails a fit — the verdict is evidence *about* a model,
not a gate on producing one, so a failure logs and leaves the columns NULL.

NULL is load-bearing everywhere: it means **not assessed**, never "good". Every version
fitted before this feature existed has NULL, and defaulting it to a verdict would convert
silence about coverage into a clean bill of health for every historical row.

**Key files** — `db/migrations/versions/0095_coverage_report_on_version.py`;
`models/orm.py`, `models/service.py` (`_coverage_columns`, `coverage_report_for_version`,
`_FitResult.training`); `studies/coverage/{report,envelope,diagnose}.py` (round-tripping);
`studies/cohort/service.py` (the cache); `api/routers/models.py` + `api/schemas.py`.
Tests: `test_coverage_persistence.py` (17), `test_coverage_stored_on_version.py` (10),
`test_coverage_cache.py` (7). Help: no user-facing change — the stored verdict is the same
verdict the panel already shows, now durable.

### 16.78 Design-space coverage, slice 6: the guard, validated on a real fit (shipped 2026-08-24)

The predict-time guard — what ADR-0020 calls the highest-value half, because a build-time
check fires once while a prediction guard fires every time anyone leans on the model.
`evaluate_support` classifies a point `inside` / `edge` / `outside`; the exported
surrogate tool declares `support_sigma` (numeric) and `support_status` (string) so the
verdict reaches `outputs.json` in a workflow nobody is watching.

**This slice was validated against a real 250-run `bracket_stress` fit, and that is the
only reason three of its seven findings exist.** Every unit test on fixtures passed
before any of them surfaced.

**Every GP response scored `poor`, whatever the design.** The per-response score was
`threshold / worst_sigma` — sigma *at* the training data over sigma at the emptiest corner
of the envelope. Measured on the real model those are 0.0014 and ~1–30, so the ratio is
always ≈0 and the verdict always `poor`. This is finding 44's always-red light, reappearing
in the pass that was written *after* finding 44 was fixed. The score is now the position
between two **measured** ends — as-good-as-the-data and no-data-at-all — and the prior end
is measured rather than assumed: it is the kernel amplitude relative to the response's own
spread, ~33 here, and fit-dependent. All three responses then read `good`, matching the
design pass, which is correct for a well-designed 250-run study.

**A tenth of the training runs read `edge`.** The threshold is a percentile *of the
training distribution*, so `edge_percentile=90` puts 10 % of the training runs above it by
construction — and a GP interpolates its own data, so those sigmas are near-identical and
which side of the line a run falls on is arbitrary. Predicting at a design point you had
already run reported "the model is less certain here than usual", which is the opposite of
true. Default raised to 99, with the property documented rather than hidden: it is a
percentile, so a small fraction always sits above, which is exactly why `edge` is advisory
and not a gate.

**The stored report had no per-response half at all.** §12 says surrogate create runs both
passes; slice 5 stored only the design pass, so `edge_sigma_threshold` was never written
and the guard could **never** return `edge` on any real model — silently binary on GP fits
fully capable of the third state. Found by noticing that every verdict on the real model
was `inside` or `outside`.

**The last unverified area of §19 is closed.** `tool_synthesis.py` does accept a string
output beside a numeric — and checking it surfaced something the document never considered:
`block_extrapolation` (ADR-0008 §7.1) already existed, tightening inputs to the **raw
training range**. Once a user narrows the declared envelope the two controls disagree about
what "the range" means, and the form would accept points the guard then reports `outside`.
`block_extrapolation` now tightens to the declared envelope; the two stay complementary —
one prevents a submission, the other reports what one turned out to be — but agree on the
range.

Two smaller ones. An exported tool runs in its own subprocess with no database, so the
report is baked into the bundle manifest — read from the manifest rather than the
sha-verified payload, because it is metadata *about* the fit, not part of the model whose
bytes the checksum protects. And a source tool whose own response is called
`support_status` keeps it; the support outputs yield rather than producing a descriptor
with two outputs sharing a name.

**On the real model afterwards:** training points read `inside`, the centre of the envelope
(legal, but between the clusters) reads `edge`, and thickness×10 / a changed pinned input /
a missing input all read `outside` with a message naming the offending input.

**Key files** — `studies/coverage/guard.py` (new), `studies/coverage/gp.py`
(`prior_sigma`), `studies/coverage/responses.py` (`_support_score`), `config/models.py`
(`edge_percentile`), `models/{bundle,runtime,tool_synthesis,exporter,service}.py`,
`ui/_coverage_panel.py`. Tests: `test_coverage_guard.py` (18),
`test_coverage_exported_tool.py` (15), plus per-response and UI regressions. Help:
`user/90-cohorts-and-surrogates.md` gains the support badge.

### 16.79 Design-space coverage, slice 7: the envelope becomes the V&V claim (shipped 2026-08-24)

The last slice, and the smallest. `vv_record.applicable_range_json` is authored by hand
today and purely documentary — a curator's assertion about what a validation covers, which
nothing checks. A surrogate model version already carries a **declared envelope**: the same
object the predict-time guard enforces on every prediction. Offering it as the applicable
range turns the field into the thing the platform already tests against, and closes a loop
that was already half-built — `VVService.resolve_envelope_warning` has always soft-warned
at submit when a job's inputs fall outside an approved validation's envelope; it just had
nothing trustworthy to read.

**The two schemas cannot express the same things, and §10 never checked.**
`applicable_range_json` is `{factor: {"min", "max"}}` — numeric bounds only. An envelope
also constrains **categorical levels** and **non-numeric pinned values**. Transferring only
what fits would leave the V&V record silently claiming applicability to materials the model
has never seen: a *wider* claim than the guard enforces, which is this feature's own failure
mode turned inside out. So what cannot be expressed is **named** — in `unexpressible` and
spelled out in the note ("material only covers cfrp, gfrp") — and the curator completes it
by hand rather than being told nothing was lost.

Two smaller calls. A **pinned numeric** input becomes a zero-width bound `[v, v]`: the
validation covers exactly that value and no other, and that is what the column's only
vocabulary can say. And a **derived** axis is included despite not being an "operating
condition" in the ordinary V&V sense — a field validated over snapshots 0–5 does not cover
snapshot 500, and a claim that omits the bound implies it does.

On the real 250-run bracket model: 17 of 18 axes transfer (6 real ranges, 11 zero-width
pinned), and the one that cannot — `temperature_field`, a pinned string — is named in the
note. The note also carries the date and the coverage verdict, so a curator reading it a
year later can tell a pre-filled range from one somebody typed.

**Key files** — `vv/coverage_range.py` (new), `vv/service.py`
(`suggested_applicable_range`). Tests: `tests/vv/test_coverage_applicable_range.py` (15).
Help: `user/35-verification-and-validation.md` gains the pre-fill note.

---

### 16.80 Design-space coverage: the adversarial stress pass (2026-08-25)

Seven slices shipped, ~1013 tests green, `mypy --strict` clean, and the feature already
validated against the real demo archive. The pass then attacked the finished thing from
four directions, and found **seventeen more defects** (design §15.1, findings 62–78) — seven in
production paths no test exercised.

**The two that matter most were disagreements between surfaces**, and a test that exercises
one surface at a time cannot see either by construction:

- **The guard rejected 138 of 143 real training runs.** Containment was implemented twice
  — `_axis_contains` in `envelope.py` and `_axis_violation` in `guard.py` — and the two
  drifted the moment one was fixed: the envelope keyed categorical levels by
  `level_label` (so a material record is the level `"Iron"`), the guard by `str()` (so the
  level was the whole dict). A model refusing to vouch for the very runs it was fitted on,
  with an unreadable message dumping the record at the user. Fixed **structurally**: the
  guard now delegates the decision and owns only the wording.
- **Every exported surrogate tool with a categorical input read `outside` on every
  prediction**, naming an input its own form did not have. Categorical factors never became
  descriptor inputs (the training matrix is numeric-only) and non-numeric held constants
  were explicitly skipped, so the guard received an *incomplete* point — which it can only
  report as unsupported. On the real archive: `bracket_stress` and `material_properties`
  always-outside, `heat_transfer` (all-numeric) unaffected. This is the always-red light of
  §7.1 in the single surface that runs unattended, where nobody is watching a badge and the
  guard is the only thing that can catch a runaway extrapolation. The exported descriptor
  now carries every envelope axis, and the generated orchestrator stops coercing every
  input with `float()`.

**A missing value was being counted as a run.** `_unit` returned `0.0` for a NaN
coordinate, placing the row at the axis minimum — a run that does not exist, sitting in
exactly the region the analysis exists to report as empty. A NaN *improved* the score. Now
such rows are dropped from every metric and **counted** as `n_unmeasurable`, reported
separately from "outside the declared range" (different problems, different fixes) and said
out loud in the panel headline. An input with nothing usable in it no longer becomes a
NaN-valued pinned factor — `NaN != NaN` had made `contains` permanently False — and is
named in a warning rather than vanishing.

**The score is a ratio, and that is now recorded.** `reference_radius` (the yardstick a
design of the same size would achieve) rises with the number of runs, so an archive topped
up with badly placed runs scores *lower* while covering more. Measured over 60 archives:
running the batch the feature itself suggests regressed the verdict in **0 of 60**; random
top-up regressed it in 1 of 60; the covering radius improved in every case. The behaviour
is right (it is what makes the score dimension-independent — finding 44); what was missing
was the yardstick on the record and an answer in the help for "I ran more jobs and the
verdict didn't improve".

**And §8's user-facing half was rendering as an error message.** `string` has been a legal
output type since ADR-0008; the job page dispatched `number` / `table` / `file` and dropped
the rest into `"(unsupported output type)"`. The synthesised surrogate declares
`support_status` as a string, so every exported surrogate job displayed **"Prediction
support: (unsupported output type 'string')"**. The chip helpers written and tested in
slice 6 were imported by nothing — the tests passed, the wiring did not exist, and the two
facts together made the gap invisible. **A tested helper that nothing calls is not a
shipped feature**, and neither the suite nor the type checker can tell you which one you
have. The verdict now renders as a badge in the job record, with the reason naming the
input that took the prediction out of range.

### 16.81 The guard reaches the surfaces it was written for (2026-08-25)

Wiring §8's verdict into the two places a prediction actually reaches a person.

**The job record.** An exported surrogate runs as a job, and its `support_status` output
now renders as a badge with the reason underneath naming the offending input — rather than
as `"(unsupported output type 'string')"`, which is what it had been showing (finding 73).

**The predicted-field viewer**, which had no support statement at all (finding 74). It is
the *other* place a prediction happens and the one where somebody is actively dragging
sliders — exactly how you leave the region the runs covered without noticing. Dragging
`time` past the last trained snapshot is the textbook §8 case, and it went unremarked. The
badge now redraws on every slider move. Its sigma is the field response's **energy-weighted
relative** sigma across the mode surrogates (§2.2.1), the same quantity whose distribution
set the stored `edge` threshold — so `edge` is genuinely reachable rather than the viewer
collapsing to inside-or-outside. Measured on a clustered fixture: 0.0011 at training
points, 0.0051 in the gap.

**One decision, two payload shapes.** `_coverage_panel.support_view` owns "which badge,
what colour, what note"; the job page adapts its `value`/`note` output entry onto it and
the viewer passes the wire payload straight through. After findings 65 and 70 — both a rule
implemented twice — a third copy was not on.

**And `outside` was carrying two meanings** (finding 75). A model with no recorded envelope
returns `outside`, correct for a validity rule bounding a number, but the badge read
"Outside the trained range" — asserting something nobody checked. This is finding 67's
shape a second time. Rather than add a fourth status and ripple it through the `Literal`,
the schema, the runtime outputs and any rule already written, the fact rides on the field
that already existed for it: `available_statuses` is **empty** when nothing can be
assessed. The conservative status stays; the badge does not. An *absent* status list stays
deliberately different from an empty one.

### 16.82 Two defects that were never about a wrong answer (2026-08-25)

The last pass asked a different question — not "is the answer right" but "is this fit to
put in front of a customer" — and the two it found are of a kind the whole preceding
effort was structurally unable to see.

**Coverage was blocking the event loop** (finding 76). `coverage_preview`,
`coverage_suggest` and the fit-time `_coverage_columns` — which runs the *GP* pass — all
called NumPy-heavy analysis inline in an `async def`. **1.85 s** on the real 708-run
archive, during which the server answers nobody else, and it scales with the archive, so
the biggest customer hurts most. The convention existed a few lines away: `models/service`
already offloads the surrogate fit with an explicit comment about the page otherwise
"breaking and refreshing". **Every functional test passed throughout** — the answer was
never wrong, the server just stopped serving. Now guarded by an AST test that flags any
analysis call lexically inside an `async def`, with a self-check proving the detector
catches both regression shapes and permits all three legal offload shapes.

**The headline number was more precise-looking than it is** (finding 77). The covering
radius is sampled over `n_candidates` probes, so it is an estimate — and a **low-biased**
one, which is the flattering direction: the archive looks better covered than it is. The
`coarse_estimate` warning existed but fired only below a resolution of 3.0, meaning
**eight or more factors**. Measured properly (6 designs per dimension, uniform / clustered
/ Latin-hypercube), the median error is −3.8% at four factors, **−6.5% at five**, −8.1% at
seven — so five, six and seven varying inputs got a silent 6–8% optimism. The threshold
now sits at 5.5, where the evidence puts the boundary, and it moved into `matofx.config`
because a value deciding what a customer is warned about is customer-tunable.

**What redeems the number is that the verdict does not move.** Across 40 designs × 8 probe
seeds the verdict flipped **0 times**, while the score underneath moved by >0.05 on 36 of
40; and it survived a 32× denser probe on all five real archives. So the traffic light is
a measurement and the score is not — which is now said in the schema and in the help,
because the day someone builds a KPI on `coverage_score` they will be trending ±10% noise.

**The lesson.** Both defects were invisible to every test written up to that point, and
not because the tests were weak: correctness tests ask "is this answer right", and neither
of these produces a wrong answer. One degrades a shared resource; the other is right on
average and imprecise in a way that only matters to a *second* consumer who does not exist
yet. **Before shipping, ask what the feature does to things other than its own output** —
the event loop, the database, the next person to read the number.

**And one more, found by writing the guide** (finding 78). Documenting the expected output of the cross-surface harness meant *running* it and stating the number — which was 1, not the 0 the draft claimed. A missing **categorical** value was counted nowhere: findings 66–68 had fixed that for numeric axes and the categorical half was never done, so one real run with a null `temperature_field` was measured as fine by the panel and called `outside` by the guard. **Writing down what a check should print is itself a test** — an expectation you cannot state exactly is one nobody is checking.

**The transferable shape.** Both cross-surface defects are *one rule implemented twice* —
containment in two places, "what inputs exist" in two places. Where two modules must agree,
make one call the other; a synchronised copy is a copy that has not drifted yet. And four
of the eleven end in **silence**: a run placed where it isn't, a run dropped from a count,
an input with no axis anywhere. For a feature whose whole purpose is to say "your archive
is thinner than it looks", quietly dropping data attacks its own premise.

## ADR-0020 complete

All seven slices shipped 2026-08-24, and an adversarial stress pass followed on
2026-08-25 (§16.80). **58 findings surfaced during build and stress** (design §15,
findings 21–78) against 23 from three pre-build review passes — and the split between them
is the most transferable thing this work produced:

- **Interface mismatches (21–42, 48–51, 59–61).** The argument held; the interface with
  existing machinery kept not holding. Numeric-only training columns, a snapshot index that
  is not seconds, `coverage` already meaning something else *twice*, two extrapolation
  controls disagreeing about "the range", a V&V schema that cannot say what an envelope
  says. **Writing the slice's tests against the real types finds these. Re-reading the
  design does not** — several were in files an earlier review pass had already read closely.
- **Numerical defects (43–47, 52–58).** Confidently wrong *numbers* with correct types,
  passing every fixture test, surviving three design reviews. A score that read `poor` for a
  perfect Latin hypercube. A suggestion algorithm that lost to random. The same
  always-red-light mistake made **twice**, in the pass written after the first one was
  fixed. A threshold that flagged a tenth of its own training data. **Every one was found by
  running against a real 250-run archive**, in minutes, by two cheap moves: score an input
  whose right answer is already known, and race the null.
- **Cross-surface disagreements (65, 70), found only after everything was green.** Two
  surfaces giving *opposite* answers about the same data — the coverage pass calling every
  training run inside the envelope while the guard called every one of them outside. No
  single-surface test can see this, and both were in the code path that runs unattended.
  **Comparing surfaces against each other is a distinct kind of test**, and it was the last
  one added rather than the first.

### 16.83 What a user saw in ten minutes that 534 tests did not (2026-08-26)

The coverage feature was switched on against the live demo archive for the first time
and looked wrong immediately. Five separate defects, all found by reading one panel —
`heat_sink`, 531 runs, six varying inputs — and none of them visible to a suite that
had grown to 534 passing tests.

**The verdict contradicted its own rows.** The panel read *Patchy*, said *"the biggest
gap is in thermal_conductivity"*, and then listed six per-factor rows every one of which
was green — including `thermal_conductivity` at 90%. Two causes, and they compounded:

1. **The yardstick was sized to rows the measurement never saw.** 240 of the 531 runs
   carry a missing value, so `covering_radius` measured 291 of them while
   `reference_radius` was asked what an evenly-spread design of *531* would achieve.
   The score is the ratio of the two, so the design was being marked against a bar set
   for a design nearly twice its size — pessimistic in direct proportion to how much
   data was missing. 0.68 instead of 0.81, and `thin` instead of `good`. This is §3's
   F2 (*something disappearing quietly*) one level up: the rows vanished from the
   measurement but not from the standard it was held to. The fix is `measured_rows`,
   which counts by **calling the same filter** `covering_radius` uses rather than
   restating its rule.
2. **The headline named a weak axis unconditionally.** `min(per_factor)` was announced
   as "the biggest gap" whatever its value, so a 0.90 axis with a `good` row of its own
   got called out as the problem. An axis is now named only when its own verdict is not
   `good`.

That second fix exposed the question underneath, which is the interesting one. Per-factor
rows measure **one input's own range**; the verdict measures the **joint space**. Both
readings are correct and they routinely disagree — six inputs can each be beautifully
varied while most of their combinations are untried. The panel had no sentence for this,
so it presented two numbers that looked like a contradiction. It now says so outright
("each input on its own is well covered, but their combinations are not"), and names the
remedy that actually applies: runs varying several inputs at once, not a wider range on
any one.

Worse, the `coarse_estimate` warning had been telling users **"the per-input rows below
are the reliable read"** — actively directing them to disbelieve the verdict. That
confused *precision* with *scope*: the rows are exact, and they answer a different
question. Both the warning and the user guide said it; both are corrected.

**Labels never reached the panel** — every row, warning, plot axis and suggestion column
printed the internal input name (`thermal_conductivity`, `base_temperature_rise`), in
plain breach of `[[feedback_use_labels_not_names]]`. The engine had a descriptor in hand
the whole time. `FactorEnvelope.label` is now resolved in exactly one place —
`build_envelope`, where `_descriptor_fields` was already being read — and every surface
asks `display_name`. Resolving it a second time anywhere is the F1 drift again.

**The plots were chosen by the alphabet.** `scatter_pairs` took the first four axes in
envelope order, which is sorted by internal name, so it drew `air_velocity`,
`base_width`, `fin_count`, `fin_height` and *never* `thermal_conductivity` — the one axis
the headline had just named. Axes are now ranked worst-covered first: the matrix exists
to show where the holes are, so the axes with the holes get the space.

**"98% covered across its range" meant nothing checkable.** It is a 1-D reading —
`1 - 2x` the largest gap between consecutive values — and readers took it as "98% of the
space is covered", which is far more flattering than the truth. The row now quotes the
gap itself: *"the biggest untried stretch is 1% of it."*

#### The spinner asked the user the question they came to ask

"Suggest runs to fill the gaps" opened with a **How many runs** spinner. Nobody knows
that number; it is the thing they opened the panel to find out. `suggestion_plan` now
prices a ladder of batch sizes and lets the answer be read off the curve, with the runs
themselves — actual input values, headed by labels — shown for whichever size is picked.

Cheap despite appearances: the suggestion walk is a greedy max-min, so the first `k`
picks of an `n`-point batch **are** the `k`-point batch. One walk at the largest size,
then a covering radius per prefix.

The table leads with the **gap**, not the score, because the gap is the monotone
quantity. The score is an *efficiency* — is this as well spread as this many runs could
be — so a larger archive is held to a higher bar and the rating can dip while the holes
genuinely close. `stress_4`'s invariant 6 ("a strict superset never scores lower") fails
on 20 of 30 designs and always has; it asserts something `reference_radius`'s own
docstring says will not hold. **Left open deliberately** — whether the headline number
should be the efficiency or the gap is a design decision, not a bug to paper over.

#### A cohort filter could not be removed

Unrelated to coverage, found in the same session. `input_ranges` and the date window
could be added but never cleared: three layers each dropped `None` before the service saw
it — the edit form, `ApiClient.update_cohort`, and the PATCH router — and the service
reads an absent key as "leave unchanged". The user emptied the boxes, Save reported
success, and the cohort went on filtering; on the demo archive that pinned a heat-sink
cohort at 19 of its 531 runs with nothing on screen to explain why.

The fix is proper PATCH semantics, which JSON and Pydantic already express perfectly
well: a key **absent** from the body is left alone, a key present and `null` is
**cleared**. The router selects on `model_fields_set` rather than `is not None`, and the
form now states its whole filter set — an emptied box travels as `null`, because that is
what the form means.

**The lesson, and it is not a new one.** Every defect here was a *sentence* — a verdict,
a headline, a label, a spinner label — and the suite tested none of them because a
sentence is not a return value. The one that would have caught most of it is not a unit
test at all: switch the feature on against real data and read the screen.

### 16.84 The level between one input and all of them (2026-08-26)

Same session, second pass, and the finding that mattered most. The user opened
the pairwise plots, looked at **Base width** against **Material conductivity**, and
saw a cross: one horizontal line of runs, one vertical, the rest of the square empty.
The panel above it read **Well covered**, with six green per-input rows and no warning
of any kind.

Measured directly, fourteen of that archive's fifteen pairs score `thin` or `poor` and
five score below 0.35. `Base width × Material conductivity` scores **0.16**. Only one
pair of fifteen is `good`. The panel said the opposite.

**Both existing measures were right, and both were structurally blind.** Coverage was
measured at two scales and the failure lived between them:

- the **per-input rows** measure one input's own range. In a cross both inputs *are*
  varied end to end, so both rows read ~98% and both are correct. Marginals cannot see
  structure — that is what a marginal is;
- the **joint score** asks "as well spread as this many runs could be?". With six
  inputs varying, 291 placeable runs cannot fill the space, so an evenly-spread
  reference leaves nearly as large a hole as the cross does and the ratio flatters
  everything. The measure is not wrong; it is *uninformative at that width*.

In **two** dimensions 291 points is plentiful, the reference is demanding, and the
cross is exposed instantly. So `studies/coverage/pairs.py` scores every pair of movable
numeric axes on its own 2-D projection.

**Calibrated before it was allowed near the verdict.** A new criterion that can demote
an archive is one bad threshold away from being F3 — a light that is always red. Across
3, 6 and 10 factors at n = 60 and 291: Latin hypercubes and uniform-random designs
produce **zero** poor pairs; one-factor-at-a-time and tight clusters produce poor on
**every** pair. Good designs bottom out near 0.58, bad ones top out near 0.36, and the
existing 0.45 floor sits in the gap without being moved for the occasion.

Only a **poor** pair counts towards the verdict. A genuine hypercube produces `thin`
pairs once there are enough factors — its worst sits near 0.58 at ten — and letting one
demote the archive would mark good designs down for being wide, which is F3 in a new
hat. A `poor` pair is different: an entire two-input face of the space was never
explored, and no amount of width makes that expected.

`one_at_a_time` is a separate diagnosis from "one bad pair", because the remedy differs.
A handful of gap-filling runs fixes an awkward corner; a cross needs a designed set that
moves several inputs together, and saying "add some runs" there would be actively wrong.

#### Four smaller things the same session found

- **A held-constant input was badged "Well covered".** The engine scores a pinned axis
  1.0 — nothing left to cover — which is defensible internally and rendered as the most
  reassuring badge on the panel, directly above a sentence explaining the model cannot
  vary the input at all. `row_chip` now returns a grey **Not varied**: coverage was not
  assessed there, and the badge says so. An internal sentinel reaching the screen
  unexamined is its own class of bug.
- **The plots were chosen by the wrong ranking.** 16.83 replaced alphabetical order with
  worst-covered *axes*; that is still wrong, because a pair can fail while both halves
  look fine — which is the whole premise of §4.3. Panels are now ranked by **pairwise**
  score, and the caption says how many pairs are drawn and how many scored better and
  were left out. "Why is Number of fins not plotted anywhere?" now has an answer on the
  page instead of being a silent truncation.
- **"98% covered across its range"** became "across the **simulated** range" — "its
  range" left the reader guessing whether that meant the input's physical range or the
  declared envelope.
- **The batch ladder was a constant.** `(3, 5, 8, 12, 20)` wasted its top rungs at two
  inputs and was a rounding error at twelve. `default_plan_sizes` scales it with the
  design's width, in **runs per input** — the unit the user guide already reasons in —
  and the user can type any size they like and be told what it would buy.

#### The plan and the report have to agree, so one calls the other

Wiring pairs into the verdict immediately broke something 16.83 had just built: the
suggestion plan computed its own score from the joint radius alone, so it offered "Well
covered" in a table sitting directly beneath a card reading "Poorly covered" — about the
same archive, in the same panel. Exactly the class of contradiction this whole session
was about, reintroduced by the fix for it.

`suggestion_plan` now calls `analyse_design` per offered size rather than recomputing a
ratio. What the table promises is what the panel will say once the runs land, **by
construction** rather than by two implementations agreeing. It costs 0.64s per option on
the 531-run archive, which is the right price for that guarantee.

Note what the plan surfaces: the gap column is monotone and leads each row, while the
verdict can move either way, because the score is an *efficiency* — a larger archive is
held to a higher bar. On the demo archive 60 runs reaches `good` and 120 reads `poor`
again. That is the measure behaving as documented, not a defect, and the table says so
in a line beneath it.

**Left open.** All five demo archives now read `poor`, and each verdict is individually
justified — they are genuinely one-factor-at-a-time. Whether "your data is bad" is the
right first impression for a customer is the product question §5 already flagged, and it
is now a sharper one, because the answer is both more accurate and less welcome.

### 16.85 The verdict was answering a different question (2026-08-27)

The user asked why 60 suggested runs read `good` and 120 read `poor`. "Surely more
runs would be better." They were right, and the answer was not a subtlety to explain
away — the headline number was measuring the wrong thing.

`coverage_score` was `reference_radius(n) / covering_radius`, where the reference is an
evenly spread design **of the archive's own size**. That makes it an *efficiency* —
"are these runs arranged well for how many there are?" — and an efficiency legitimately
falls when runs are added. Traced to the exact pair:

```
Fin thickness x Material conductivity, +60 -> +120 runs
  gap        0.1793 -> 0.1539   genuinely BETTER
  yardstick  0.0848 -> 0.0666   the bar rose faster
  score      0.473  -> 0.433    so the score fell
```

Reproduced in a controlled setting, on strictly **nested** point sets, which is the
proof that it is the arithmetic and not the data:

```
             n=60    n=120   n=180   n=240   n=300
moving bar  1.000    0.941   1.000   0.978   1.000   <- backwards twice
fixed bar   0.933    0.987   1.000   1.000   1.000   <- never
```

Using an efficiency to answer "is this fit to learn from?" is a category error, and it
had a second consequence nobody had connected to the first: in six dimensions the
reference is *itself* terrible, so the ratio of two terrible numbers is a good one —
which is how a one-factor-at-a-time cross scored `good` for as long as it did.

The bar is now pinned to a standard that depends only on the dimension. Only the radius
moves, and a covering radius never rises as points are added: **monotone by arithmetic**
rather than by hope. The efficiency reading survives as its own labelled number, because
"did you get your money's worth from these runs?" is a fair question — it just is not
this one, and it no longer sets the light.

#### The calibration found two more problems

The standard was *measured*, not chosen (`checks/coverage/calibrate_standard.py`) —
seven design families at four sizes with eight replicates, sweeping 8 to 60. Two things
fell out that no amount of arithmetic would have:

**The standard cannot separate anything.** Since `score = bar / radius` and the bar is
the same for every design, changing it rescales *every* score by one factor. It cannot
improve separation — that is fixed by the radii — it decides **where the floors fall**.
Restating the requirement as placement ("a good archive must never read `poor`; a bad
one must") is what made the sweep answerable at all. At 30 runs per input: worst good
0.529, best bad 0.368, the 0.45 floor between them with a balanced +0.079 / −0.082
margin and no floor moved.

**The yardstick itself was a single random draw.** `reference_radius` built one Latin
hypercube and measured it, and an LHS's own covering radius is a random variable — its
axes are permuted independently, so they can align badly. Sweeping the standard, the bar
read 0.3283 at ten runs per input and 0.2443 at twelve: a *larger* ideal design giving a
26% smaller bar, which is impossible in expectation and was landing directly on every
verdict. Now the median of fifteen layouts, memoised on the envelope's **shape** — the
reference never depends on the archive, so a whole pairwise sweep needs two or three
distinct layouts and every option in a suggestion plan reuses one bar.

#### The joint reading stops discriminating at seven inputs

Margin between a Latin hypercube and the worse of one-factor-at-a-time / clustered, at
20 runs per input:

```
d=2: 0.44   d=3: 0.34   d=4: 0.33   d=5: 0.34   d=6: 0.32
d=7: 0.20   d=8: 0.20   d=9: 0.19
```

It holds to six and collapses at seven, where a clustered design scores 0.79 —
comfortably `good`. Letting the joint number drive the light there would not be
conservative, it would be **wrong in the flattering direction**. Above the boundary it is
reported with the caveat and the pairwise pass carries the verdict instead: two
dimensions always have enough runs to judge, whatever the tool's width.

#### Everything was measured against the range you happened to run

The envelope defaults to the observed spread, so every figure the panel produced was
self-referential: "98% covered" always meant *98% of the range you chose to run*, and
could never report that a tenth of the tool's own range had been explored. On the demo
heat-sink archive `base_temperature_rise` sat at 60 out of a declared 10–120, and every
reading called it fine.

The per-input strip is now a two-range table — simulated range and coverage, input range
and coverage — and a row opens both histograms side by side. `FactorEnvelope` carries
`declared_lo` / `declared_hi` from the descriptor, and a **literal** bound only:
`minimum_expr` / `maximum_expr` are formulas over sibling inputs, so the allowed range is
a different interval on every row and there is no single denominator to quote.

#### "Always 60" is a fact, not a rule

A held-constant input was `pinned` and no suggestion could move it — right about the
archive, wrong about the future. Giving such an axis a real range now **promotes** it to
a varying one, so it joins the design space properly rather than being a special case
every measurement has to know about. Offered only where the tool states bounds and the
input is a number: a pinned material name has no number line to sweep.

That promotion exposed a latent bug worth recording. `PointSet.rows` carries only the
*varied* factors — a constant lives once in `constants`, not repeated per row — so the
promoted column normalised to all-`NaN`: the axis dropped out of the joint radius and
every pair containing it went **silently unjudged**, reported as "15 of 21 examined" with
no reason given. `Envelope.normalise` now reads through `points.values()`, which falls
back to the constant. The distinction had no consequence while such an axis was excluded
from every measurement, and became wrong the moment one could be opened up.

#### The button had to mean what it said

"Create a parametric study **and add to Cohort**" would have been a lie for some cohorts.
A fixed cohort is frozen to `member_job_ids` and would never take the new runs; an
auto-grow one usually would, but its own `input_ranges` / `project_ids` / date window can
perfectly well reject the very runs it commissioned — a study finishing with the verdict
unmoved and nothing on screen to explain it.

Migration **0096** adds `job_cohort.auto_include_study_ids`, and membership resolution
unions in those studies' completed jobs. **Resolution-time, not event-time**: no hook to
fire from the runner, no crash mid-batch to survive, no idempotency to get right — a run
joins the moment it is complete enough to resolve, which is the rule `auto_grow` already
works by. `excluded_job_ids` still wins: a curator who removed a run meant it.

#### The test layer that was missing

Two render-time defects shipped past a green suite in consecutive rounds — a `string`
output branch no page could render, then `cell.style("cursor: pointer;", add=True)`,
whose first positional parameter is *itself* named `add`. That one drew the first input
row and then stopped, with the rest of the table simply absent rather than erroring
visibly.

The helpers were tested because they were pure; the render functions that consume them
were tested by nothing, on the grounds that "the suite cannot drive NiceGUI". It can:
elements need a **slot**, not a browser or a server or a socket, and a bare `Client`
supplies one. `tests/ui/test_coverage_panel_renders.py` now draws the whole panel. It was
verified by reintroducing the defect — four of its tests fail on it — and then restoring
the fix.

**The pattern across this whole run.** Every defect was a *sentence* or a *number nobody
could check by eye*: a verdict, a headline, a label, a badge on an input that never
varied, a spinner that never rendered because its handler was synchronous and the update
never reached the browser. None were reachable by a unit test of a pure function, and all
were obvious within seconds of looking at the real page with real data. That remains the
cheapest test in this area and the last one anybody runs.

**Left open.** All five demo archives read `poor`, each verdict individually justified —
they are genuinely one-factor-at-a-time. Whether "your data is bad" is the right first
impression for a customer is the product question the handover's §5 already flagged, and
it is now a sharper one, because the answer is both more accurate and less welcome.

### 16.86 The button that produced runs nobody could run (2026-08-27)

Two defects, and the first was hiding the second.

**"Where are the runs? Why haven't they been kicked off?"** The user priced a batch of
two, pressed *Create a parametric study and add to Cohort*, landed on the studies page
and read **"Progress: Not submitted yet"**. `create_study_from_coverage` created a
`draft` — deliberately, per the original decision that solver time should be reviewed
before it is spent.

The review had already happened. The user priced the batch, saw both runs with their
input values, and pressed a button naming the cohort it feeds. What the draft added was
not a decision but a second manual step, on a page they had been sent to expressly to
watch it run. It now calls `StudyService.submit` — the same call the studies page makes,
so the pinned tool's `approvals_policy` still decides: `none` / `post` fan the points
into child jobs and queue them, `pre` / `both` raise the study gate. **The gate is
unchanged; the user is no longer standing in front of it.**

The run cap moved too. It exists to stop a *generator* running away; here the count is
exact, priced and shown run by run, so it must not be the thing that rejects a batch the
user has just approved.

#### What the draft had been concealing

The user's next question was the good one: *"do the recommended run values take into
consideration sum checks and input rules/dependencies? They definitely should."*

They did not. The suggestion path sampled each varying axis inside its own declared box,
independently, and proposed the result — while a descriptor says a great deal more than
that: `set_value` forces, formula-driven bounds over sibling inputs, `require` validity
rules, §F30 cross-field constraints, and **sum-checks**.

Measured on `material_properties`, whose three percentages must total 100:

```
1: REJECTED  sum= 83.8      5: REJECTED  sum= 51.8
2: REJECTED  sum=117.1      6: REJECTED  sum=148.2
3: REJECTED  sum= 71.0      7: REJECTED  sum=125.9
4: REJECTED  sum=119.9      8: REJECTED  sum= 96.8

0 of 8 proposed runs are actually submittable.
```

Not a tail case — every one. And **invisible precisely because of the draft**: the batch
was priced, listed and committed, then its points died inside a generator nobody was
watching. Submitting the study is what turned a silent defect into "I asked for eight
runs and got none", which is the only reason it was found on the same afternoon.

#### Two rules, because they are not the same kind of thing

**A sum-check is repaired.** It is an *equality*, and filtering independently sampled
points against an equality rejects essentially all of them — converting a broken feature
into an empty one. The group's true degrees of freedom are searched instead, one member
derived as `value - sum(others)`. ADR-0009 §3.6 already made exactly this fix for the
optimiser, so `coverage/rules.py` **calls that module** rather than restating it. Repair
has a limit: a residual the balance cannot absorb without leaving its own range is a
rejection, not a silent clamp — clamping would emit a run breaking the rule the
derivation exists to keep.

**Everything else is rejected by `forms.resolve_for_generation`** — not an equivalent
check, the *identical function the study generator calls* on the identical point. A batch
that passes here cannot be dropped there, which is the entire failure being closed.
Survivors come back resolved, with forces written in, because that is what would run.

Result on the same tool: **8 of 8**, every one summing to exactly 100, with the free
members still varying rather than collapsing onto a single blend.

#### The control is the part worth remembering

Asked whether the *other* rule kinds were covered, the honest answer needed a measurement
rather than a reading of the code. A tool was built carrying four rules of different
shapes — a cross-field constraint, a `require`, a formula-driven bound and a `set_value`
force — and the suggestions checked against all four. **8 of 8 conforming.**

That number was worthless until the control ran. The first fixture clustered its existing
runs in a corner of the space where every rule happened to hold; withholding the
descriptor produced **0 of 8** violations, meaning the test could not have failed and
would have reported success whether the filter existed or not. Spreading the runs across
the full declared ranges made it discriminate: **6 of 8** violations without the pass, **0
of 8** with it. Both are committed, the second documented as what gives the first its
meaning.

*A test that cannot fail is not evidence.* It is the same lesson as reintroducing the
`add=True` defect to prove the render tests caught it (§16.85), arriving from the
opposite direction.

#### Fixture debt, paid

Making the study actually run walked into three fixtures that had been getting away with
stubs because nothing in them ever generated or queued: a descriptor of
`{"alias": "demo"}`, a bundle path that did not exist on disk, and no storage location at
all. Each is now real. They had not merely been *lax* — they had been actively concealing
decision 17's defect, because a study that can never be generated can never reveal that
its points are infeasible.

#### Smaller things in the same pass

- **The study form.** *Project ID* became a picker over the caller's own projects — a
  typed id asks the user to know a database key and accepts any integer, including one
  they cannot see, which surfaces as a permission error only after the batch is priced.
  The name pre-fills `"<tool label> - Fill coverage gaps"` (a project accumulates these,
  and three called "Fill coverage gaps" say nothing), and a name already used in the
  chosen project turns the field red **before** the click rather than failing on save.
- **The scope control.** `q-btn-toggle` joins its segments into one pill by design, and
  hand-fighting that produced two sets of edges and a light-theme-only background. Two
  real buttons with a gap, filled for the active choice and outlined for the rest, is what
  was wanted and what it now draws.
- **The spinner that never appeared.** Its handler was synchronous, so the spinner was
  still queued when the event loop was taken by the work it was announcing. Yielding once
  before the build flushes it.

---

## NAFEMS demo hardening — 2026-08-27 → 2026-09-01

Building the conference demo end to end exercised the surrogate → cohort → coverage →
optimisation chain against a real archive for the first time in one sitting. It found
**nine defects**, seven of them silent. The shape of them is worth keeping, because it
repeats: every one was a **producer/consumer disagreement between two surfaces that were
each internally consistent and separately tested**.

### The Finned Cold Plate showcase tool

`example_tools/finned_plate/` — a steady FE conduction solve on a fan-cooled baseplate,
built for the NAFEMS talk (`designs/finned-plate-tool.md`). Design notes worth carrying:

- **The fins are not meshed.** They enter as an effective surface conductance on the top
  face, which is what keeps the mesh topology fixed while `fin_count` / `fin_height` vary
  — the property a POD field surrogate depends on. The FE solve earns its place on
  **spreading resistance**, which a lumped model cannot produce and which is what makes
  base thickness a real design variable.
- **The demo needs the response surface to curve**, not just the software to work. A
  laminar/turbulent crossover cuts diagonally across `fin_count` × `fan_duty`, so a cohort
  clustered on one side has no information about the other. That requirement drove the
  physics choice; it is not discoverable from the tool contract.
- Two findings are recorded in the design doc because they were *measured, not reasoned*:
  the fully-developed laminar Nusselt number made `fan_duty` inert over most of the box
  (short channels never thermally develop), and the material categorical is one-sided for
  a single mass objective (`sqrt(k)/rho`) but **two-sided under two objectives** — 21
  magnesium and 15 aluminium points on the Pareto front, within a gram of each other at
  the temperature limit.

### The nine defects

**1. An exported surrogate wrote an `outputs.json` the runner could not parse.**
`run_surrogate_tool` emitted `file_type` / `field_format` / `field_node_array_name` on the
field output; `OutputValue` forbids extra keys. Validation failed for the *whole document*,
so `record_study_result` stored `scalars = {}`, `_tell` dropped every evaluation for
lacking its objectives, and a 40-run NSGA-II study **completed successfully with an empty
Pareto front and no error anywhere**. Field metadata is a descriptor concern; the tool
document carries values only. The regression test is the one that was missing: round-trip
the orchestrator's output through `OutputsDocument`. Every existing test read that file as
raw JSON — one of them *asserted the offending key*.

**2. `support_status` carried a `note` key** — same illegal-key failure, same blast radius.

**3. Only `write_scalar_bundle` baked in the coverage report.** A combined or field export
therefore declared `support_status` / `support_sigma` in its descriptor (synthesis reads
the report from the database) that the orchestrator could never write, because it reads
the report from the *bundle*.

**4. The fit path resolved no descriptor.** `models/service.py` called `build_envelope(points)`
with no descriptor while the cohort preview path always passed one, so every coverage axis
label was `""` and `display_name` fell back to the internal name — in the model record, the
V&V trail and the exported tool's coverage panel. Second cost: **integer axes silently
degraded to continuous**.

**5. The coverage gauge had no labels to use.** A model version's `factors` record only
`{name, min, max}` — the label is a descriptor fact — so the submit form's training-data
chips read `fan_duty: far extrapolating` beside a field labelled "Fan speed". Fixed by
passing the descriptor's labels from the form that is already rendering with them; no refit
needed.

**6. A fixed cohort discarded its exclusions.** `excluded_job_ids` was cleared on create and
update for `auto_grow=False`, and resolution never consulted it. Invisible until a fixed
cohort *also* auto-included a study: de-selecting one of those runs removed it from
`member_job_ids` (so the summary count fell) and `_commissioned_rows` put it straight back.
The summary and the plots then disagreed, and coverage, the results matrix and any
surrogate fitted from the cohort all took the larger set. The page also printed **two
different counts** — the frozen list, and the resolved membership.

**7. `uncertainty_for` was dropped on every descriptor save.** `_output_to_descriptor` builds
from an explicit allowlist. So *any* edit — adding a group, renaming a label — unpaired a
surrogate's `value ± std` reporting permanently. Grouping was a red herring; it was just the
reason the editor got opened. The same function already carried a comment about this exact
failure for field-output keys.

**8. Model scope → Project could never succeed.** The dialog had no project picker and sent
the model's *current* project id, which is `None` for a personal model.

**9. Preview-only cohort filters landed on the wrong request schema.** A text replacement
anchored on `owner_user_ids` matched `CohortCreateRequest` first, so the preview route
rejected them with `extra_forbidden` while the create route silently accepted three filters
it has no columns to store. Now pinned by a test asserting which schema holds them.

### What generalises

- **Test the contract between surfaces, not each surface.** Defects 1, 2, 3, 5 and 9 all
  pass every single-surface test. The cheap catch is a round-trip assertion: parse what the
  producer writes with the consumer's own parser.
- **An allowlist is a slow leak.** `_output_to_descriptor` has now lost two different key
  families this way. Anything that rebuilds a validated object field-by-field will drop the
  next field somebody adds.
- **Silence and health look identical.** Defect 6, the empty pairwise sweep, a dangling
  `uncertainty_for` and an unjudgeable categorical all rendered as *nothing* — which reads
  as "no problems found". Each now states what it could not determine, and why.
- **A validator that only checks a field in isolation is half a validator.** `uncertainty_for`
  checked `type == "number"` but not that the target existed, so a rename hid a result.

### Smaller changes, same session

- **Declared precision is honoured** when a parametric study or an optimisation picks values
  (`studies/_intsnap.snap_precision`, `FactorSpec.snap_float`). The step is frozen into the
  optimisation config at `configure` time, as `sum_groups` already was. Grid axes now
  de-duplicate: a coarse step can collapse neighbouring levels.
- **Coverage envelope carries `declared_levels`** so a categorical axis states its options
  rather than "No stated range", and counts how many were tried.
- **`min_points_per_pair` travels on the report** so an unjudgeable pairwise sweep can say
  how many runs a pair needs.
- **POD modes are explained** wherever they are shown, and the count is presented as what it
  is: a reading on the archive, not a setting.
- **Cohort preview** names the study each run came from, shows who launched it, and offers
  Launched-by / Source / study-name filters. The launcher and study options are **facets
  derived from the same candidate query the preview draws from**, so an option can never
  match nothing. Source and study-name shipped **preview-only** on 2026-09-01 (no columns to
  hold them) and were **promoted to stored filters the next day** — migration **0097** —
  because losing them on re-open made them useless for exactly the case they were built
  for, and a live cohort could not be expressed as "the runs of these studies" at all.

### A tenth defect, found 2026-09-02

**`populate_study_results` topped the table up but never reconciled it.** It wrote a
`study_result` row for each *current* member lacking one and left rows for jobs that had
since left the membership. On a real cohort: 80 members, **100 rows**, so the page showed
80 while the pairwise table showed 100 — and `collect_training_data` reads those rows, so a
surrogate fitted from that cohort would have **trained on twenty runs the curator had
removed**. These rows are a *projection* of membership, not the record (the run's own result
lives on its own study's row and on disk), so the fix is to delete what no longer belongs.
Same family as defect 6: an incremental cache that only ever grows.


### An eleventh defect, and the worst of them — 2026-09-02

**A varied categorical was silently dropped from the fit.** `TrainingData` grew
`categorical_columns` for ADR-0020 coverage, and its own docstring said `fit_surrogate`
ignores them — coverage could *see* a material imbalance that the model could not learn.
So two runs with identical numbers and different materials became one point with two
answers, and the fit carried that as **irreducible error**.

Found because widening a cohort from one material to two made the model **worse**, which
inverts the entire argument of the NAFEMS talk. The tell was `mass`: an exactly
representable algebraic product (`density x volume`) scoring Q² 1.000 on the narrow
cohort and **0.855, RMSE 41.7 g** on the wide one. No region is "harder" for a closed
form. Measured on the real 80-run archive, the fix takes `mass` to Q² 1.0000 / 0.29 g and
`t_source` from 0.880 / 4.66 °C to 0.9665 / 2.46 °C — and the before-numbers reproduce the
stored quality of version 18 exactly, which is what confirmed the diagnosis.

Worse than the metric: the **exported tool offered a `material` picker and ignored it**.
Same geometry, both materials, identical predictions — 255.01 g against a truth of 297.00
and 191.40. The synthesised form even carried help text saying *"It doesn't predict
differences between them"*, which was honest at the time and is now false.

**The fix.** Categoricals are one-hot encoded onto the end of the model matrix — every
level gets a column, so no level sits implicitly at the origin and becomes special for no
physical reason. They stay out of `factors` / `ranges` / `means`, which remain numeric so
the coverage gauge and profiler are untouched; `Surrogate.predict` re-encodes each point.
An **unseen level is refused**, not encoded as all-zeros: all-zeros is a point the model
never saw, and a confident number for it is the failure the encoding exists to stop.
Fields inherit it because they fit one scalar per POD mode.

Two things the test run caught that would otherwise have shipped:

- `conditioned_on` (coverage's GP pass) rebuilt the matrix from the numeric columns and
  crashed against the wider scaler — so the training level per row had to be stored too.
- `length_scales` compared the ARD kernel size to `len(factors)` and, on mismatch, fell
  back to **one shared scale**. Not a crash — a silent loss of exactly the per-axis signal
  coverage uses to steer budget.

**Categorical pair plots** landed with it: axis selection filtered to
`continuous` / `integer`, so an option axis appeared in no panel at all. A choice against a
number is now a **strip** (one column per option, runs nudged apart by a deterministic
offset so a screenshot reproduces), and a choice against a choice a **tally grid** where a
zero cell is a combination nobody has run. They sort last, because the score that orders
the rest is a covering distance and a set of options has none — ranking them by a number
they do not have would be worse than putting them at the end. A coverage *score* for these
pairs is deliberately **not** invented: the numeric pair scores are calibrated, and an
uncalibrated verdict sitting next to them would be an overclaim.

That gap mattered concretely here: the twenty magnesium runs all came from one study over
the narrow box, so material was confounded with the rest of the space — invisible on every
continuous panel, and obvious on a strip.

### A twelfth defect — the same blind spot, one surface along — 2026-09-02

**The design-space explorer could not plot a categorical either.** Reported from the UI:
*"when I choose Plate material in the Design Space Explorer everything is being plotted as
0. Not only does 0 not make sense, but it's all one thing too."*

One root cause, `_num()` returning `None` for a string, with two different faces:

- as the **colour**, `_num(...) or 0.0` gave every run the value 0 on a continuous bar —
  so two materials drew as one flat colour, which is both symptoms in one line;
- as an **axis**, `if xv is None or yv is None: continue` dropped every point and the
  panel reported *"No numeric runs to plot yet"* — truthful about what it did, wrong
  about why, and the worse failure because it looks like an empty archive.

The levels were never missing. The wizard has always saved a categorical factor as
`{"name", "type": "categorical", "choices": [...]}`; `_factors_from_config` reduced each
factor to `{key, label, unit}` and threw the options away **one function short of the
plot**. That is the same shape as defect 5 (the coverage gauge had no labels to use): the
data was in the record, the projection dropped it.

**The fix.** `levels` rides along in the factor dict, and a categorical axis maps each
level to an evenly-spaced position with the level's *name* on the tick. Colour gets a
stepped scale — flat equal bands, `cmin=-0.5 .. cmax=n-0.5`, tick text per level — because
a continuous ramp over category indices invites the eye to interpolate between two
materials, which means nothing.

Three decisions worth keeping:

- **Declared order, not alphabetical.** `Low / Medium / High` must not sort into
  `High / Low / Medium`. The tool author's order is the meaningful one.
- **Options nobody ran still get a column.** An empty column says the search never went
  there — the same coverage story the cohort panels tell, showing up in the optimiser's
  own plot.
- **The nudge is off by default**, unlike the cohort coverage strips. Those exist to show
  density; the explorer is read run-by-run (hover, Pareto rings, feasibility crosses) and
  a nudged marker sits where it does not belong. It turns on only when *every* positional
  axis is categorical, where the cloud would otherwise collapse onto a handful of dots and
  the exact position carries no information anyway.

A run with no value for the factor gets a named `(not set)` column rather than vanishing —
the same defect in miniature, and the reason the empty-plot message was so misleading.

**Checked and deliberately not changed:** the runs table and best-designs table were fine
(`_fmt` / `_cell` fall through to `str`), so only the scatter was affected.

### The same blind spot, third occurrence: the parametric Explore plots — 2026-09-02

The study page had the *quieter* form. `_varied_inputs` filters to `_numeric_inputs`, so a
swept `select` never appeared in the X / Y / Z / colour pickers **at all** — not a wrong
plot, a missing option, for the one variable the study was actually varying. Fixed on the
maintainer's instruction straight after the explorer.

Because this is the third occurrence, the five helpers moved out of `optimisation.py` into
`ui/_categorical_axis.py` and both pages now import them. A fourth surface should reuse
that module rather than grow a third implementation of the same idea.

What was added:

- `_option_inputs()`, kept **separate** from `_varied_inputs()` rather than widening it.
  `_varied_inputs` mirrors the server's `varied_columns` and feeds sensitivity, ANOVA and
  the coverage metrics, all of which need real numbers; widening it would push options into
  analyses that cannot use them. The Explore plots ask for both lists and sort the option
  axes last, so the default X / Y picks stay numeric.
- `input_options` on `StudyResultsResponse` (`{input name: declared options}`), so a
  *declared but never run* option still gets its empty column. Without it the UI could only
  see the levels present in the runs — and the gap the column exists to show would be the
  one thing invisible.
- `_var_text()` beside `_var_value()`. `_var_value` still returns `None` for a choice **by
  design**, because the analyses that call it need numbers.

The hover mattered more than it looks. Both scatter templates format with `%{x:.3f}`, so a
categorical axis would have produced a tooltip reading `Plate material: 0.0` — the original
defect wearing a different hat. Choice axes push their level text into `customdata`, with
the index assigned as the template is built so the two cannot drift apart.

**A follow-up the same day.** The first cut also copied `_varied_inputs`'s *two-or-more
distinct values* rule, and the maintainer caught it immediately: a study run over a single
material still could not plot the input it had selected. Reported as — *"I appreciate that
these were single material runs, but the user selected it that way, so it should still
show. It is in the Run/response matrix after all."*

That rule was borrowed from the wrong place. `_varied_inputs` needs a spread because it
feeds things that **rank** an input against a response; a plot axis does not, and the
matrix itself filters nothing (`sorted({k for c in children for k in c["inputs"]})`). No
spread disqualifies an input from a ranking, not from a picture — one column is a true
statement about every run in the study. `_option_inputs` now returns every choice-valued
input the runs carry, restricted to `str` values so a multiselect list or a mapping cannot
stringify itself into an unreadable column.

**And a third pass**, on the maintainer's instruction: the same relaxation for numeric
inputs held constant. `_plot_inputs()` now returns every input the runs carry, numeric and
choice alike, so the earlier asymmetry (a constant choice offered, a constant number not)
is gone. `_varied_inputs` is untouched and still guards the analyses. The cost is a longer
picker on a numeric-heavy tool, and a flat axis per held input on the parallel plot —
accepted deliberately in exchange for being able to plot what the study actually chose.

Two ordering fixes landed with it, both reported from the UI:

- **Inputs before outputs.** `_plot_variables` had appended the option inputs *after* the
  scalar outputs, so `Plate material` drew at the right-hand edge of the parallel plot,
  away from the inputs it belongs with. The dict is the axis order, so the append site was
  the bug. Inputs now lead, in **descriptor order** (`input_labels` keys), so the parallel
  axes read left to right like the tool's own form.
- **Sections in the axis pickers.** `sectioned_variables()` inserts `INPUTS` / `OUTPUTS`
  headings with a trailing rule, skipping a heading whose section is empty.

The headings are worth a note. Quasar *can* grey out an individual option, but only via a
per-option `disable` flag, and NiceGUI has no public way to set one: `ChoiceElement`
rebuilds `_props['options']` from the labels alone and **normalises away** anything written
into it — verified both nested (`options[0]['disable'] = True`) and wholesale
(`_props['options'] = [...]`); the key is gone on the next read either way. Rather than
ship a private reach-in that a NiceGUI upgrade would break silently, a heading is
selectable and **snaps back**: `guard_section_headers` restores the previous axis and
suppresses the redraw, so picking one is a no-op rather than an empty chart. It returns its
handler so the rule can be tested directly instead of testing NiceGUI's event plumbing, and
the draw functions treat a heading key as "no axis chosen" as a second line of defence.

**Still numeric-only, deliberately:** input influence, ANOVA/interactions and the
prediction profiler. They fit and rank a response against a numeric range and there is no
meaningful range between two materials. Documented in the user chapter rather than left as
a silent omission — that silence is exactly what made this defect class hard to see three
times over.

### SMTP: one login per email was the bug — 2026-09-02

Reported from the runner terminal during the 400-run optimisation: a repeating
`aiosmtplib.errors.SMTPServerDisconnected: Unexpected EOF received` on every job-state
notification. It reads like a network fault and is not one.

Probing the real handshake ruled the obvious causes out — connect, `EHLO`, `STARTTLS` and
the second `EHLO` all succeeded and Gmail advertised `LOGIN` / `PLAIN`. Forcing the error
to speak gave the answer:

```
454 4.7.0 Too many login attempts, please try again later.
```

`notify/smtp.py:send_one` was documented as *"one-shot connect → (login) → send → quit"* —
**one authentication per email**. Relays rate-limit logins, not messages. A study that
changes job state hundreds of times in a few minutes therefore authenticates hundreds of
times, trips the limit, and once hard-throttled the relay stops answering `AUTH` at all,
which is the bare EOF.

The reasoning was already in the module docstring — *"Gmail and most relays rate-limit
logins, so re-connecting per recipient would throttle a broadcast"* — and `SmtpSession`
already existed to solve it. It was only ever applied to the **broadcast fan-out**, which
has an enclosing scope to hold a connection in. A burst of unrelated job notifications has
no such scope, so nobody noticed the same argument applied.

**The fix.** `_SmtpPool` holds one authenticated connection and revalidates it per send.
Three rules worth keeping:

- **Reopen, don't trust.** A connection is reused only if the settings still match, the
  socket still reports connected, and it has been idle under 60 s. Relays close idle
  sessions without saying so, and reconnecting is cheap next to a lost notification.
- **Retry only a *reused* connection.** A pooled socket failing is almost always staleness,
  so retry once on a fresh one. A connection we just opened failing is a real failure, and
  retrying it would risk delivering the same email twice for nothing.
- **Settings are compared, not assumed.** An admin changing the host or rotating the
  password in General Settings reopens on the next email — previously that only took effect
  on a process restart, and nothing would have said so.

Sends are serialised by a lock (one connection cannot interleave two transactions); job
emails are far slower to produce than to send, so it is not a bottleneck. The pool is
closed in the API lifespan.

The existing notify tests faked `aiosmtplib.send`, which the pool no longer calls. Rather
than rewrite twelve expectations, `tests/notify/conftest.py:patch_smtp` adapts the
class-based client back to the `send(message, **connection_kwargs)` shape the tests already
describe — and, importantly, gives each test its own pool, since a pooled connection is
module state that would otherwise leak between tests.

### Five workspace changes, and the flex-grow arithmetic behind one of them — 2026-09-02

A batch of user-reported items. Four were small; the first was a rendering bug whose cause
sits in the CSS spec rather than in any of our code.

**The Outputs panel rendered at half width — for some tools.** `job._flex_weights_from_layout`
turned the descriptor's `output_column_layout` into `flex-grow` factors with `flex-basis: 0`.
Flexbox only hands out *all* the free space when the grow factors **sum to at least 1**; below
that it distributes only that fraction, and the rest of the row stays empty. So the weights
were never really weights — they were absolute shares that happened to be right whenever they
summed to one.

Two-column tools always summed to one, so nothing showed. The failure needed a **stale key**:
the wizard's layout editor renders only when there is more than one column
(`_render_layout_widget`), so a tool whose groups collapsed back to a single column kept its
old `column_2` weight forever, and `column_1` still read `0.5`. `finned_plate` 1.0.2 rendered
its only output column at half width; `material_properties` 1.3.2 did the same to its *input
form* at 40%, through the identical helper in `tools/forms/render.py`. Same bug, two surfaces,
and the wizard's own help text ("they're normalised on render") described the behaviour we
did not have.

The fix is to make that sentence true — normalise to sum to 1 — with one ordering detail:
**normalise first, then clamp** to the 0.05 visibility floor. Clamping first lets the floor
drag the total *below* 1 again on a `{0.01, 0.99}` layout, which is the bug back in miniature;
applying it afterwards can only push the total over 1, which still fills the row. Relative
widths are untouched (2:1 was already 67/33). The wizard now also prunes weights for columns
no group uses, so the dead keys stop accumulating.

**Reordering by drag.** Inputs, outputs, input/output groups and table columns are all ordered
lists edited through the same `_row_toolbar`. They now carry a drag handle as well, using the
native HTML5 idiom already proven in the Project Metadata values editor — no JS library, so
nothing new to vendor for an air-gapped install. Three decisions worth keeping:

- **Only the handle is `draggable`.** A draggable ancestor breaks text selection inside the
  row's own inputs, and every one of these rows is a form.
- **The handle sits *outside* the expansion** for inputs and outputs (`_reorderable_row`).
  Their toolbar lives in the expansion body, which a collapsed row hides — and collapsed is
  exactly when you want to reorder a long list.
- **Highlighting is a `js_handler` with no `emit`.** `dragover` fires continuously; a server
  round-trip per pixel would flood the socket. The drop is the only event that reaches Python.

The arrows stay, as the keyboard-reachable path. Verified in a real browser by dispatching
synthetic `dragstart`/`dragover`/`drop` against the actual helpers: down, up, dragleave
clearing and self-drop-is-a-no-op.

**A cohort can now start a surrogate.** The fit form already had a cohort branch and
`_prefill_source` already handled `kind == "cohort"`; the models router already materialises a
cohort's lazily-populated `study_result` rows before a fit reads them. All that was missing was
the affordance a finished parametric study has had all along, so the cohort detail page gets
the same **Build a surrogate** button, gated on `models.create` / `models.curate` rather than
on `cohorts.curate` — fitting from a cohort and curating one are different jobs.

**The job page never said which job it was.** The status row carries the tool, state and
timestamps; the meta column described the project and scenario. Nothing named the run. A **Job**
card (name / id / created) now sits under Project and Scenario, and — since the in-flight and
failed views have no meta column — as a width-capped card under their status row. It is built
outside the poller's `status_box`, which is cleared and rebuilt on a state change.

**Job emails are opt-in per tool.** `email_notifications` defaulted to `True` at every layer,
so a newly added tool started mailing its submitter before anyone decided it should. The
default is now `False` in the descriptor model, both admin payloads and the new-tool form —
and, deliberately, in the runtime gate too: `notify/email._tool_email_enabled` used to read a
*missing* key as "send", which would have left an imported or hand-written bundle mailing
people while every UI surface showed the box unticked. Every descriptor authored through the
app stores the flag explicitly (103 true / 38 false / 0 missing in the demo database), so no
existing tool changes behaviour; only silence-by-omission does.

### Axis-picker sections, and the flat-axis toggle — 2026-09-02

Two follow-ups, both asked for directly.

**The optimisation explorer got the same sections.** Its list is objectives then factors
rather than inputs then outputs, but the complaint is identical — one undivided run with no
clue where the goals end and the search variables begin. That made three implementations of
one idea likely, so the section machinery moved to **`ui/_axis_picker.py`**
(`sectioned_options`, `guard_section_headers`, `is_section_header`) and both pages import
it; `studies.sectioned_variables` is now a four-line wrapper naming its two groups.

Heading keys are **numbered**, not slugged from the title, so two groups can never collide
on a shared word — which matters here because the explorer builds two *differently keyed*
option maps from the same axes (`int` keys for X/Y/Z, `"a{i}"` strings for colour).

Two guards the explorer needed that the study page did not: `_pick` (which restores axis
choices from saved prefs) must reject a stored heading, and `_draw` returns early if one
reaches it — otherwise `int(x_sel.value)` raises on a string. The explorer's selects also
moved from `on_value_change` to the shared `update:model-value` guard, so the snap-back
does not re-trigger a redraw.

**`varying_keys()` + a "Hide unvaried axes" checkbox** on the parallel plot. Once every
held input gets an axis (above), a tool with twenty inputs and two varied draws eighteen
flat lines beside the two that carry the comparison. Offering the axis is right; *drawing*
all of them by default is not readable. The checkbox is **off** by default — the inputs the
study chose stay visible until the reader says otherwise — and it never hides everything: a
study where nothing varies still draws its picture rather than an empty frame with no
explanation.

A test-quality note worth keeping: the first version of the toggle's test asserted
`"Fan duty" not in labels` against a fixture whose label map did not contain `fan_duty`, so
it passed against the raw-name fallback rather than against the behaviour. Caught only
because the paired "off by default" test failed for the same reason. A negative assertion
about a label needs a fixture that actually produces that label.

### A commissioned study could never be detached — 2026-09-02

Reported from the UI: a fixed cohort edited down to a single 20-run study still resolved to
**60 runs**, the detail page read *"20 run(s) picked, plus completed runs of 1 commissioned
study(ies)"* while the coverage panel counted 60, and the plate-material plots had vanished.
Three symptoms, two causes.

**Cause 1 — the field was accepted and discarded.** `auto_include_study_ids` (the studies a
cohort commissions to fill its own gaps, ADR-0020 §11.5) was declared on
`CohortUpdateRequest` and present in **neither** `_FILTER_FIELDS` tuple — the router's copy
nor the service's. The router builds its filter dict by iterating that tuple, and the
service validates against its own, so the field passed schema validation and then fell
through both layers without touching the database. Nothing — UI, API client, or raw REST —
could detach a study once commissioned. `_commissioned_rows` then re-added its runs on
every resolve, which is correct behaviour on top of a value nobody could change.

Two hand-maintained copies of one list is the whole defect. A test now asserts they are
equal, and a second asserts that **every** field on `CohortUpdateRequest` is either handled
by name in `update_cohort` or present in the tuple — so the next field added to the schema
and forgotten in the plumbing fails at test time rather than in a curator's cohort.

Adding it to the shared tuple immediately broke **creation** with an `AttributeError`:
`_FILTER_FIELDS` is shared by create, preview and update, and `CohortCreateRequest` has no
such field — a cohort cannot commission a study before it exists. The create and preview
paths now skip what the body does not carry, and the update-only set is pinned by a test.

**Cause 2 — a choice that never varied was invisible.** A factor with one observed level is
recorded `kind="pinned"`, and `pinned` is not `actionable` (no suggestion can move it),
which excluded it from the coverage grid's axes. `axis_levels` compounded it by testing
`kind == "categorical"`, so even when admitted it had no levels to draw. Every run in the
cohort used aluminium and magnesium had never been tried — 100 % on one level, 0 % on the
other, the **most extreme categorical imbalance there is** — and it was the one case the
panel built to show imbalance could not draw.

Both now key on `is_option_axis`, which tests for `declared_levels` rather than the kind.
That distinction matters in both directions: a pinned *number* (`base_temperature_rise`
held at one value) has bounds and no levels, and reading `pinned` as categorical threw
those bounds away once already.

**Cause 3, milder — the page did not reconcile its own numbers.** "20 picked" and "60" sat
on one page with nothing joining them. The summary now says plainly that the commissioned
runs are counted below and that the resolved total is larger.

**A behaviour worth knowing, pinned by test rather than fixed.** Detaching a study from a
**live** cohort clears the link but need not shrink the membership: a study-homed run *is*
in the auto-grow candidate universe (`_candidate_rows` joins `ParametricStudy`), so once
the commissioning link goes those runs are re-admitted on their own merits — same tool, a
tolerated version, complete. Detaching removes the *reason* they were force-included, not
the runs. Excluding them is what removes them, and the builder's helper text now says so.

This is the fifth occurrence of the categorical blind spot (fit, coverage pairs,
optimisation explorer, parametric Explore plots, and now pinned axes in the coverage grid).

**Two follow-ups the same afternoon, both reported from the UI.**

*The payload went to the wrong endpoints.* `_gather_filters` feeds **preview, create and
update**, and all three bodies forbid extras — so putting the update-only
`auto_include_study_ids` in that shared dict made every preview of an edited cohort fail
with `extra_forbidden`. Exactly the shape of the earlier source-filter defect, which landed
those fields on `CohortCreateRequest` and 422'd the same endpoint. It is now
`_commissioned_payload()`, added only to the update call, and
`tests/ui/test_cohort_builder_payload.py` calls the real method and checks its keys against
all three schemas — the test neither defect had.

*The axis drew and the runs did not land on it.* `resolve_points` builds each row from the
factors that actually **vary**; an input held constant is carried once on the envelope as
`pinned_value`. So a pinned material is absent from every point, and a strip built from
those points put nothing on its column. Fixed at the edge with
`with_pinned_values(points, envelope)` rather than by widening the point rows — those rows
feed the metrics, the suggestions and the GP conditioning, none of which should change
because a chart wants a value. A point that already carries the axis wins over the
envelope constant, and a pinned axis with no recorded value is skipped rather than
inventing a `None` level.

*And the same axis list existed twice.* The material panels appeared in the cohort's own
coverage grid and not in the fill preview's existing-against-proposed grid, because that
second matrix still filtered to `continuous` / `integer`. Rather than widen the second
copy, the rule moved to `_coverage_panel.grid_axes(envelope)` and both call sites use it —
the same two-copies failure as `_FILTER_FIELDS` two hours earlier, and the third time in
one session that one idea living in two places produced a defect.

The fill preview genuinely needs **two** lists, though, which is why the drift was easy to
miss: its per-input histograms bin numbers, so an option axis has nothing to draw there,
while its pairwise matrix does. It now computes `axes` (numeric, for the histograms) and
`matrix_axes = grid_axes(...)` separately. A test reads the source and asserts both
matrices call `grid_axes` and that no inline axis comprehension has come back.

### Anything rendered is a file you can take away — 2026-09-02

Reported from the UI: the images on a job's results page had no download. Neither did the
CSV behind a plotted chart, nor a transient field's raw data, nor an inline table, nor the
picture previewed under an image *input*. The page could show you a result and give you no
way to get it off the screen.

`_output_download_link` already existed and was already the documented single path (fetch
server-side with the bearer, hand to the browser via `ui.download.content` — a bare
`<a href>` to the outputs endpoint 401s, which is why every earlier download bug was the
same bug). The renderers that *fetch bytes to embed them* had simply never called it: an
image builds a `data:` URL and stops, and a CSV chart offered only a re-exported HTML
chart. The helper was there; the discipline of ending every renderer on it was not.

So each `file_type` renderer now ends on it, on the failure paths too — a *"couldn't parse
this CSV"* is precisely when someone wants the file. Three renderers offer **two**
downloads, because there are two distinct artefacts: the CSV *and* the chart drawn from it;
the tool's own HTML file *and* the themed re-export of it; the raw field *and* the animated
series. The picture of a chart needs no button of ours — Plotly's modebar camera does it.

Two smaller repairs fell out of the same pass. `_render_html_plot`'s oversize branch said
*"open it via the download button below"* and then returned without rendering one. And the
image-input preview holder was a bare `<div>`, so the `self-end` on its new button had no
flex parent to right-align against; it is a `ui.column` now.

`tests/ui/test_job_outputs.py` parametrises over every file renderer and asserts each one's
source reaches a download helper, so the next `file_type` added cannot quietly ship without
one.

**The Plotly logo, while in there.** `MATOFX_PLOTLY_CONFIG` carries `displaylogo: False`,
but a config only reaches the browser when the figure is passed as a **payload dict** —
`themed_plotly(fig)`. Five charts on the Job Cohorts page called
`ui.plotly(apply_theme_to_plotly(fig))`, which is themed but config-less, so Plotly fell
back to its defaults and the "Produced with Plotly" link came back on those modebars alone.
The fix is one word at each site; the durable part is `test_theme.py`, which now scans the
package for any `ui.plotly(` call whose argument is not a payload builder. A per-call-site
convention that nothing checks is a convention that drifts.

### The Setup card named the search without defining it — 2026-09-02

Reported from the UI: an optimisation's **Setup & outcome** card listed *"Factors: Fin
height, Number of fins, Plate material"* and said nothing else about them. It named what
moved and never said between what limits, and it never mentioned the inputs that were held
still at all — yet those two facts are exactly what makes a study reproducible. The bounds
were in `config_json` the whole time; `_factors_from_config` read `choices` and dropped
`min`/`max` one line short of the card, and `fixed_inputs` was never read on the results
side at all.

So the card now states the whole search definition: **Factors varied (n)** with each
factor's declared range (`5 to 45`), or its levels where the factor is a choice; and **Held
constant (n)** with every `fixed_inputs` entry at the value each run used. Both under
descriptor labels with units, per `feedback_use_labels_not_names`.

**The material was the interesting case.** A held material rides as a bare
`material_version_id`, so printing the value would have put a database id in a governance
record. `/api/materials/picker` resolves ids to names, but only for each material's *latest
published* version — a study holding an older one cannot resolve. Rather than guess, the
lookup matches on the exact version id and the display degrades honestly:
`Aluminium 6061 (material version 77)` when it resolves, `material version 12` when it does
not. A name is never shown against a version it does not belong to. `_material_classes` /
`_workflow_material_classes` pick the material inputs out of the descriptor (member-
qualified for a workflow target, since `fixed_inputs` keys those `member.input`).

`tests/ui/test_optimisation_setup_card.py` covers both halves, including the two cases most
likely to regress: a categorical factor must not acquire a range from a stray `min`/`max`,
and a multiselect held value must not reach the user as a Python list repr.

### A failing heartbeat poll took the whole cycle down — 2026-09-02

From a runner log on the dev box: a transient `sqlite3.OperationalError: database is locked`
on a heartbeat `UPDATE`, then `MissingGreenlet`, then *"Runner loop body raised; continuing"*.
The lock is ordinary contention and the code was written to survive it — `_heartbeat_once`
wraps each job's poll in a savepoint precisely so one poisoned poll costs that job its turn
and nothing more.

The guard leaked through its own handler. A rolled-back savepoint **expires every attribute**
on the instance it isolated, so `_log.exception("... job %s ...", job.id)` in the `except`
fired a lazy refresh — sync IO on the async path — which raised `MissingGreenlet` *out of the
except clause*, past the per-job isolation, and aborted the cycle for every sibling job. The
containment was real; reading an ORM attribute to report the failure is what escaped it.

Fix: hoist `job_id` to a plain `int` before the try and log that, and expunge the instance
after a failed poll so the next statement in the cycle cannot autoflush the same doomed
UPDATE. `test_one_poisoned_heartbeat_poll_does_not_take_the_cycle_down` reproduces the exact
shape — dirty the row, raise inside the savepoint, assert the sibling still gets polled — and
fails with `MissingGreenlet` against the old handler.

**The generalisation:** an `except` block around ORM work is not a safe place to touch the
ORM. Capture whatever the handler needs to say before entering the `try`.

### Choosing what a suggested batch explores — 2026-09-03

Reported: *"there is no option in the UI to 'Also vary the inputs held constant so far'"*.
The control existed and was correct. It renders only when something is promotable, and
the promotable test was **is it a number** — so on a cohort whose only held-constant input
was the plate material, a `select`, nothing was promotable and the switch never drew.

**The rule was wrong twice.** It excluded genuine choices, which is why the switch was
invisible; and it excluded a *discrete numeric* input authored as a `select` of `6 / 8 / 10`,
whose content is perfectly numeric. Replaced with:

> Promotable iff the descriptor declares a domain the form would accept — bounds for a
> number, options for a choice.

`integer` / `float` / `select` / `boolean` qualify (a boolean enumerates itself as
`False` / `True`, matching the strings every level comparison downstream keys on).
`material` stays out for a real reason rather than an incidental one: the frozen-options
rule means it may not enumerate its options at all, so there is genuinely nothing to
sweep. `multiselect` stays out because its domain is the power set of its options.

**The ladder had to learn a second currency.** It prices batches by *covering radius*,
which is numeric geometry a categorical axis contributes nothing to — so a batch sweeping
two materials would move the gap barely at all while doing exactly what was asked, and the
table would read as though the runs bought nothing. `SuggestionOption.levels_short`
reports option balance beside the gap (*"gap closes to 21 % · options under-run 1 → 0"*)
rather than blended into it, which would have put a calibrated-looking number on
categorical balance — the overclaim the pairwise pass deliberately refuses to make.

**Per-input choice, and what it is measured against.** Both switches are replaced by a
tick per input — varying ones ticked, promotable held-constant ones offered unticked — and
a scope control. Unticking is expressed as `FactorOverride.hold`, which demotes a varying
axis to `pinned`: an already-understood state that the joint radius, the pairwise sweep
and the suggestions all handle with no new special case, and which still rides along in
every suggested run so the point stays submittable.

The scope control decides what unticking *means*, and this is the load-bearing decision:

- **The whole design space** (default) — unticked axes are still measured, so a batch
  exploring two of eight inputs will not reach `good`, and should not.
- **Only the inputs I chose** — the axes leave the space and every figure describes the
  narrower one.

The second is offered because "I only care about these two" is a legitimate reading, and
because a space-filling walk needs runs growing with dimension, so narrowing is what keeps
a batch affordable. It is **not** the default: a verdict that quietly changed meaning is
the false-confidence failure the whole feature exists to prevent.

**Two defects the new tests caught before release.** `hold` initially fired for any
non-pinned axis, which demoted a `derived` axis to `pinned` — and a pinned axis rides
along in every suggested run, so a snapshot index would have been proposed as an input
(§1.2.1 forbids exactly this). And the guard test written the previous day for the
pairwise matrices fired on the new `explorable_axes`; rather than retire or scope it, the
kinds became `VARYING_KINDS` and the guard now permits the literal exactly once — at the
definition. That is the third time in two days one idea living in two places produced a
defect, and the first time the guard caught it rather than a user.

**And a fourth, found by the maintainer running it.** `CoverageEnvelopeFactor.varying_kind`
was `Literal["continuous", "integer"] | None` — the schema silently mirroring the engine's
old numeric-only rule. Widening the rule therefore made the **entire coverage endpoint**
raise a 500 for any cohort with a held-constant choice, which is every cohort the feature
exists for. Neither the engine tests nor the UI tests crossed that boundary, so both suites
stayed green while the page was unusable.

`test_varying_kind_contract` now compares the set `_varying_kind` can return against the
set the response literal accepts, and was verified to fail against the old schema rather
than pass vacuously.

The failure also surfaced badly: the page sat on *"Measuring coverage…"* indefinitely. The
UI talks to the API **in-process**, so a server-side fault arrives as the raw exception
rather than an HTTP error — it escaped the handler's `except ApiError`, and blanked the
page with the spinner still running. `_load` now also catches broadly, logs, and says so on
the panel ([[feedback_page_body_pre_render_catch_all]], the third time that lesson has been
needed).

### "20 patchy, 40 well covered, 80 patchy" — 2026-09-03

Reported from the UI on the Third-study cohort with the material opened up: a bigger batch
rating **worse**, twice. Reproduced exactly against the live demo database, and it is two
defects compounding.

**`PointSet.values` resolved a held constant only when *no* row carried the name.** True of
an archive on its own; false the moment it is projected. The suggestion planner measures
`existing rows + proposed rows`, and the proposed rows **do** carry a value for the
promoted axis — so one row having it switched the fallback off for the other twenty, which
came back `None`. §4.7's earlier fix addressed the un-projected case only, and the
distinction was invisible until a categorical could be promoted.

**`_categorical_row` then counted those `None`s as a level of its own.** `counts.get(key,
0) + 1` invents a level for any value outside the declared set, which did two things at
once: it inflated `len(counts)` so the even share fell from ½ to ⅓, and it put a **fixed**
count of twenty into the `min()`. The balance therefore *decreased* as real runs were
added. The arithmetic reproduces the report to two decimals:

| batch | counts | balance |
|---|---|---|
| 20 | `None`&nbsp;20, Al&nbsp;6, Mg&nbsp;14 | (6/40) / (1/3) = **0.45** → patchy |
| 40 | `None`&nbsp;20, Al&nbsp;19, Mg&nbsp;21 | (19/60) / (1/3) = **0.95** → well covered |
| 80 | `None`&nbsp;20, Al&nbsp;42, Mg&nbsp;38 | (20/100) / (1/3) = **0.60** → patchy |

**The fixes.** The fallback is per row rather than all-or-nothing. And a value outside the
axis's levels is **dropped**, exactly as `_unit` drops a row with no numeric coordinate — a
run that cannot be placed on an axis is not evidence about how its levels are shared out.
Dropped rows are reported alongside the thin levels rather than swallowed. After both, the
same cohort reads poor / poor / good / good / good, with the balance climbing 0.08 → 0.36 →
0.70 → 0.70 → 0.76.

**Also fixed, same session, same root:** promotion dropped `pinned_value`. The engine reads
through `PointSet.values` and was unaffected, but the *charts* read the raw rows via
`with_pinned_values`, which needs that value — so every existing run vanished from the
promoted axis's panels ("the previous Al runs are not showing in the pairwise plots").

**What this says about the testing.** Three of these four defects lived at a boundary two
correct components meet: engine vs response schema, engine vs chart, archive vs projected
archive. Each component's own tests passed. The tests that now exist assert the *seam* —
what one side can emit against what the other accepts.

### The fill-the-gaps controls, reworked — 2026-09-03

Feedback after using them: *"FILL ACROSS is fine. INPUTS TO EXPLORE is also fine. What I
don't understand is MEASURE COVERAGE AGAINST… this feels like there should be some things
which are mutually exclusive."*

**The label was the problem.** *"Measure coverage against — the whole design space / only
the inputs I chose"* reads as a **third, independent scope**, and collides with *Fill
across*: one names a **range**, the other names a **set of inputs**, and both said
something like "the whole space". It is now *"Coverage figures describe — Every input /
Only the ones I ticked"*, whose options name the ticks directly above them, so it reads as
a follow-on question rather than a new one.

**The mutual exclusivity was real, and it was missing.** With every input ticked the two
options describe the same space, so the control could not mean anything — and offering a
live control whose options are identical invites a reader to hunt for a difference that is
not there. It is now disabled, forced to *Every input*, and replaced by the line "every
input is ticked, so there is nothing to leave out". The test is a **varying** input being
unticked: one held constant is already outside the design space, so unticking it leaves
nothing behind for the figures to keep reporting.

**Layout.** Three stacked headings read as three unrelated settings. *Inputs to explore*
now comes first — it is the question answered first — with the two scope controls beneath
it side by side, each with its own explanation. Two shared helpers (`_heading`, `_hint`)
replace the four copies of the same inline style.

### The ladder always offered the same numbers — 2026-09-03

*"Why do I always get the same number of runs being suggested?"* Because
`default_plan_sizes` was a pure function of `len(envelope.metric_axes)`. Two consequences:

- **Opening up a held-constant choice changed nothing.** A categorical is not a metric
  axis, so the one control the user had just used left every rung identical. The ladder
  now counts every axis a batch can move — `metric_axes + categorical_axes`.
- **The cohort size was ignored entirely.** The same five numbers came back for a
  twenty-run archive and a five-hundred-run one. A rung far smaller than what is already
  there cannot move a measure computed over the union, so it spends a row on a batch that
  provably buys nothing. Rungs below a tenth of the archive are now dropped, with at least
  two always kept so the table still reads as a curve.

Ticking the material on the demo cohort moves the ladder from 4/8/20/40/80 to
5/10/25/50/100; a two-hundred-run archive drops to 20/40/80. A twenty-run archive is
deliberately unchanged — the demo's own numbers do not move.

**And "See where these runs would go" now says "— click to open"**, inside an accent-bordered
tinted bar with an explicit chevron. It read as a heading and went unclicked.

### "Nothing new to suggest on that input" — 2026-09-03

The worst of the round, because the wording was the least of it. Choosing *the range these
runs already cover* and asking how many runs were needed answered:

> Every value of `fin_count` inside the range you set has already been run, so there is
> nothing new to suggest on that input.

Three faults, in increasing order of seriousness.

1. **The internal name, not the label** — [[feedback_use_labels_not_names]], reported as
   *"you used name not label (again!!!)"*.
2. **One input singled out** as though the other three had no room either. With several
   lattice axes it is the *combination* that is used up, not each axis; saying "every value
   of A, B and C" claims something stronger, and probably false, about each separately.
3. **It was not true.** `_admissible` treated a point's small-integer coordinates as the
   point's **identity**, so a candidate was discarded for repeating a fin count even when
   its fin height, base thickness and fan speed were entirely different. On a cohort whose
   seven attainable fin counts had all been run, that rejected *every* candidate and the
   panel proposed nothing at all.

The maintainer's question found the general case before the code did: *"what would happen
if the cohort range was over two distinct clusters? We would need to fill in the space in
between."* Precisely so — the gap between two clusters is reachable **only** by re-using
integer values that already exist at the edges, which is what the filter forbade.

**The fix.** Deduplicate on the lattice only when the lattice *is* the whole design. With
a continuous axis present an exact duplicate has measure zero and needs no filter; the
greedy max-min walk is what keeps the batch spread. Cohort 5 goes from **0 proposed points
with a misleading warning** to **80 with none**. The all-integer case keeps the original
rule, where the lattice key really is the point.

Two existing tests asserted the old behaviour on a *mixed* design and had to be rewritten
rather than the code softened — they encoded the defect. What replaces them is the
invariant that actually matters: no proposal repeats an existing run **whole**.

### Three states for the coverage scope, and options in the histograms — 2026-09-03

**The scope control needed a third state.** *"Every input"* is a claim of completeness, and
it is false while a promotable input sits unticked: that input is outside the design space
either way, so the figures never described it. The first option now reads **"Every explored
input"** until every promotable input is ticked, at which point it earns the plainer
**"Every input"**. Both states stay greyed out — there is still nothing to leave out — and
the explanatory line names which state it is in.

**The before/after histograms only binned numbers.** An input you have opened up is one of
the inputs the batch explores, so it belongs in the same row. `_stacked_levels` is the
categorical twin of `_stacked_histogram`, drawn at the same size: a column per declared
option, existing stacked with proposed, and a footer naming any option the archive has
never run. An empty column is the point, exactly as on the pairwise strips.

### Unticking an input did nothing under the default scope — 2026-09-03

Reported: *"I unticked Fin height, then chose Every input for Coverage figures, and I get
suggestions for Fin height."* Correct, and a design flaw of mine rather than a slip.

Leaving an input out of a batch is expressed by **pinning** it: a pinned axis is not in
`metric_axes`, so the walk cannot move it, and `denormalise` gives it its value so the
proposed run is still submittable. But pinning also removes the axis from **every
measurement** — and I had gated the pins on the narrowed scope to avoid that. Under
*"Every input"*, therefore, no pin was sent at all and the untick was inert.

The two questions are independent and were being answered by one mechanism:

* **which inputs the batch may move** — the tick, and it must always take effect;
* **which inputs the figures describe** — the scope, and it must not change the batch.

`suggestion_plan` now takes a separate `measure_envelope`, threaded through the service
(`measure_overrides`), the API (`CohortCoveragePlanRequest.measure_envelope`) and the
client. The page always sends the pins in `envelope`, and under *"Every input"* sends the
same rows **minus the pins** as `measure_envelope` — so the batch stays narrow and the
assessment stays whole. `measurement_envelope()` is a module-level function precisely so
that rule is testable rather than buried in a closure.

Verified against the live cohort: unticking Fin height now yields sixty proposed points
all at its midpoint, and the ladder drops from 4/8/20/40/80 to 3/6/15/30/60 because three
axes are explored rather than four.

One existing test — `test_the_default_scope_changes_nothing_about_the_measurement` —
asserted `hold_overrides(..., measure_chosen=False) == []`, which *is* the defect written
down. It is replaced by two tests that separate the questions: the pin is unconditional,
and the measurement envelope is what strips it back out.

### One axis list for the fill preview — 2026-09-03

*"If an input parameter is plotted in the pairwise plots, it needs to be in the histograms
too."* The two rows of the preview answer one question between them — what would the
archive become? — and they were built from **separate lists**: numbers only for the
histograms, `grid_axes` for the matrix. A held-constant choice therefore drew a strip in
the matrix and no bar above it.

Both now walk `matrix_axes`, and each axis is drawn the way its kind allows —
`_stacked_levels` where the envelope gives it levels, `_stacked_histogram` otherwise. The
second list is gone rather than corrected, which is the only fix that stops the pair
drifting again; a test reads the function and fails if a second `for name in axes:` comes
back.

### Sweeping the fill-the-gaps combinations — 2026-09-03

A systematic pass over every reachable combination of the three controls (ticks × *Fill
across* × *Coverage figures describe*, ~30 states). Six defects, ordered by what they cost
the reader.

**1. The headline percentage was arithmetically wrong under the default.** `gap_fraction`
divides a covering radius by the diagonal of the space it was taken over, and
`suggestion_plan` read the axis count off the **batch** envelope while every radius came
from `measure()`, which uses `measure_envelope`. Untick one numeric input of three and a
three-axis radius was divided by a two-axis diagonal: 25% reported where 20% is honest,
inflating in the alarming direction and clamping towards 100% as more inputs were left
out. One line. It is the number the whole card is built around, and there was no test on
`suggestion_plan` at all — `tests/studies/test_coverage_plan.py` now exists.

**2. An unticked input was held at a value the tool may not step to.** `hold_value`
returned the raw midpoint, and `denormalise` snaps the axes the batch *moves* while
copying pinned values straight through — so the one route into a proposed run that skipped
the precision snap was the one nobody could see. An input stepping in halves over an
observed 2.0–3.5 put `2.75` on every run in the batch. `_snap_precision` is now a thin
wrapper over a public `snap_to_step`, which `hold_value` uses too.

**3. And that value was invisible and unchangeable**, despite `hold_value`'s own docstring
claiming otherwise. The fill preview cannot show it either: the preview is drawn over the
batch's space, where the axis has been pinned out of existence. A "held at" box per
unticked input now sits under the tick list, seeded with the midpoint and editable —
because "hold it at the value we actually ship" is the common case and there was no way to
say it. Values live in a dict keyed by name, so re-ticking and unticking brings back what
the user chose.

**4. A batch made only of choices was refused.** Both `suggest_design_points` and
`suggestion_plan` bailed on `not envelope.metric_axes`, so unticking every number and
leaving `material` ticked returned nothing — the one batch shape that fixes the
forty-runs-on-one-material disease the panel makes a point of warning about. The geometric
walk genuinely has nothing to do in zero dimensions, so that case has its own filler:
`_fill_option_gaps` scores each cell of the option grid by how many runs sit in it and
takes the emptiest first, **re-chosen every pick rather than once per pass** — a pass gives
every cell one run, which is the wrong answer when one option has never been run and the
others have ten each. Repeating a combination is allowed here and only here: on a grid
whose cells are all occupied a repeat is the only move that improves the balance, and the
batch says so. `_has_actionable_axis` had to widen to match, or the card stayed hidden on
exactly those cohorts — it also gated out an archive where every number was held constant,
which is the cohort the tick list exists for.

**5. The refusal blamed the archive for the user's choice.** *"They are either held
constant across every run or come from the solver"* is a statement about the data, and
after the user has unticked everything it is untrue. The two cases are told apart by
whether an axis the archive **varied** is pinned in this envelope — only a hold can do
that.

**6. Three labels overclaimed, and the docs described a control that no longer exists.**
*Fill across* does not govern option-valued inputs (`_levels` prefers the descriptor's
full option list on purpose) nor a promoted held-constant input (which has no observed
range to stay inside); both now carry a caveat line. The plan table's "Today" figure is
measured over the space the *figures* describe while the verdict badge above is measured
over the report's — they routinely disagree under a widened scope, and the table now says
which it used. And `docs/user/90` still documented *"Also vary the inputs held constant so
far"* as a **switch**, claiming it updated the verdict, the per-input rows and every plot;
that switch became the tick list, and the claim was never true of it.

**Not done, and deliberate:** no select-all on the tick list, the ticks still reset when
the panel reloads, and the plan click still costs four rebuilds of the cohort's points and
envelope. All three are cost or convenience, not correctness.

### One number was answering two questions — 2026-09-04

*"Why are all pairwise combinations shown as 'Rarely varied together'? The cohort was made
from an LHS of 20 runs, so they should have been varied together."* Right, and the Runs
column said 20 on every row.

**The label described the arrangement; the number measured the run count.** A pair's
covering radius is divided by a **fixed** 60-run yardstick (30 per input, and a pair has
two), so a clean twenty-run hypercube reads `thin` on every pair for that reason alone.
Confirmed on the real cohort: all six pairs 0.53–0.66 with all twenty runs on each.

Each row now carries **two** readings over the same range, differing only in the
comparison — `coverage_score` against the fixed 60, `spread_score` against what the pair's
*own* runs could achieve. On cohort 5 that reads *Not many / Well spread* across the board:
the design is fine, there is not much of it. A cross reads the other way and stays that way
however many runs it gets.

**The pairwise pass was uncalibrated below ~60 runs, and it is the designated fallback
above six inputs.** `max_discriminating_axes` hands it the verdict there on the grounds
that "two dimensions always have enough points to judge, whatever the tool's width" — true
about the width, false about the count. Re-measured with `calibrate_standard`'s own
methodology (unit-cube envelope, seven families, four densities), worst-good against
best-bad per pair:

| runs on the pair | 20 | 40 | 60 | 80 | 150+ |
|---|---|---|---|---|---|
| density worst-good / best-bad | 0.30 / 0.37 | 0.39 / 0.37 | 0.53 / 0.37 | 0.66 / 0.37 | 0.88 / 0.37 |
| spread worst-good / best-bad | 0.49 / 0.60 | 0.51 / 0.48 | 0.53 / 0.37 | 0.63 / 0.35 | 0.62 / 0.26 |

Below 60 they overlap; at 20 they **invert**. Hence `min_discriminating_pair_runs = 60`:
below it a pair is shown, dimmed, and does not vote. A first sweep suggested the spread
reading was size-robust and could carry the light at any count — that was wrong, and it was
wrong because it let each design derive its own envelope, so a tight cluster was measured
over its own bounding box and scored 1.0. The calibration script holds the envelope at the
unit cube for exactly this reason.

**Only the spread reading votes.** The density one is a run-count statement, which the
joint score and the per-input rows already make; letting it vote is what said "an entire
two-input face was never explored" about a hypercube whose every pair carried every run.

**And if both stand down, the verdict is `insufficient`.** Above six inputs the joint score
already abstains; below 60 runs a pair now does too. What is left is the per-factor strip,
which is marginal and passes a cross every time — a green badge there would be flattering
on exactly the cohort least able to justify one.

### Choices joined the pairwise table — 2026-09-04

*"Why is Plate material not included in this table? Shouldn't categorical and boolean
parameters also be in this table?"* The sweep walked `metric_axes` alone, so a varying
choice contributed no pair at all: the Fourth Study runs 40 jobs across two materials and
still showed exactly the six numeric pairs.

Nothing else covered it. Every categorical check in the report is **marginal** — counts per
option, "an option barely used" — so an archive that ran aluminium only at low fin heights
and magnesium only at high ones passes all of them.

- **Number × choice** — the number's coverage *within each option*, worst option setting the
  row. One dimension at a time rather than a Gower-style mixed distance: both produce a
  number, only this one names the failing option by construction. A level with one run is
  not measured — a point is not a spread, and the option-balance row already says it.
- **Choice × choice** — the combination grid, reusing the cell counting `_fill_option_gaps`
  already does. `spread` divides by `min(runs, cells)`, so twelve runs over twenty
  combinations are judged on landing in twelve different ones rather than blamed for not
  being twenty.

Validated on three synthetic archives, and the third is why two columns earn their keep:

| archive | Enough runs? | Well spread? |
|---|---|---|
| Al only low, Mg only high | poor (0.05) | poor (0.08) |
| both swept fully | good (1.00) | good (1.00) |
| 35 Al / 5 Mg, both swept | poor (0.17) | **good (0.90)** |

The last reads "you need more magnesium runs, and the five you have are in the right
places", which no single number could say. `analyse_pairs` takes an optional `points` —
choices cannot be read off the normalised matrix — and `n_pairs_total` counts them, or a
capped sweep would look complete.

### The efficiency score reached the API and stopped — 2026-09-04

Computed on every report since §4.4, carried through `CoverageResponse`, and rendered
**nowhere**; the only mention in the UI was a comment. It is the cohort-level twin of the
new spread column, and on cohort 5 it reads a perfect **1.000** against the 0.667 that is
actually on screen — the runs are as well spread as twenty runs could be.

Now a sentence under the verdict, shown only when the verdict is not already `good`, in
three bands: commission more of the same, mostly more, or the batch needs to go somewhere
else. Deliberately prose rather than a second badge — unlike the verdict it can fall when
runs are added, and a light that moves backwards for doing the right thing is one people
learn to ignore.

### Held inputs stopped disappearing from the preview plots — 2026-09-04

*"I've unticked Fin height and left Plate material unticked, but I get a Plate material
histogram and not a Fin height one. It's either showing in both or none."*

`grid_axes` admitted a pinned **choice** and refused a pinned **number**, on the grounds
that a number held at one value draws a single meaningless tick while "every run used
aluminium" is the most extreme imbalance there is. Defensible while the only pinned axes
were ones the archive held constant — and backwards the moment §11.5's tick list let a user
pin a number themselves, because that is the axis whose spike they most need to see. It is
what the "held at" box exists to disclose.

Both rows now show every axis a run carries a value for. `derived` is still out — a
snapshot index is not a design input. One further fix fell out: the histogram branch read
the **raw** point rows, where a pinned axis is absent, so the newly-admitted axis would
have drawn an empty blue bar beside a full amber spike. It reads `with_pinned_values` now,
as the matrix already did.

### The ladder went backwards — 2026-09-04

Same day, reported against the fix above: *"if I choose all the inputs over the full range,
I get poorly covered, poorly covered, patchy, poorly covered, poorly covered with an
increasing number of runs, which doesn't make sense. And looking at the pairwise plots it
doesn't look bad at all."* Both halves right. Reproduced exactly on cohort 5.

**Fault 1 — a non-monotone term in the verdict, which I had just put there.** §4.4's own
comment says it plainly: *"an efficiency legitimately falls when runs are added — which
produced a plan table reading `good` at 60 suggested runs and `poor` at 120"*. That is why
`efficiency_score` was demoted from the verdict. I then wired the pair-level twin
(`spread_score`, whose bar is the pair's own run count) straight into `pair_verdict`, and
reproduced the identical symptom one level down. The joint score climbed 0.612 → 0.675 →
0.858 → 1.000 → 1.000 the whole time.

`pair_verdict` reads `coverage_score` again — fixed bar, moves only when the radius does,
monotone by arithmetic. `spread_score` is reported and never judges. **The guarantee had no
test**; `test_the_ladder_never_goes_backwards` now checks verdict, gap and score across
three scopes, including the reported one.

**Fault 2 — the within-option measure was miscalibrated, and only real data showed it.**
`_number_by_option_pair` scored a 1-D radius against `reference_radius` of the same size. A
stratified reference puts one point in every `1/n` slice; the 1-D projection of a
space-filling batch is closer to uniform-random, whose largest gap runs about `ln(n)/n`. The
ratio therefore sits near `1/ln(n)` however good the design is. On the +50 rung:

```
Aluminium  43 runs, 2.50..14.80 of a declared 2..15, biggest gap 0.138  ->  scored 0.31 poor
Magnesium  27 runs, 2.20..14.20 of a declared 2..15, biggest gap 0.115  ->  scored 0.27 poor
```

Both cover the range; both read poor. It now uses `1 - 2 × largest gap`, which is the
formula the per-input strip already uses for this exact question — so "the number's
coverage within each option" is literally the per-input row computed per option. The
*Enough runs* half became the thinnest option's count against the 30-per-input standard,
which is a count and therefore monotone.

The synthetic validation had passed because its `_sweep` helper stepped `i/n` rather than
`i/(n-1)`, leaving a fifth of the range uncovered at the top and masking the systematic
penalty. Fixed, and the three archives now read: confounded → spread poor; both swept →
spread good; 35/5 unequal → density poor, spread good.

**Fault 3, latent, same family.** `one_at_a_time` asks a structural question — "is this a
cross?" — and tested it against `coverage_score`, the run-count reading, with no
discrimination floor. A clean eight-input hypercube of 24 runs measures 0.443 against a
0.45 floor; it escaped only because *every* pair must fall below. It reads `spread_score`
now, over discriminating pairs only.

Cohort 5 over the full declared range now reads **poor → poor → poor → thin → good → good**.

### The density blind spot, and the sentence built on top of it — 2026-09-04

Asked to look at the joint score's density blind spot and report rather than change, with
the prompt *"maybe we can make the statement more qualified rather than so categorical?"*
That instinct was right and my earlier proposal — flip small cohorts to `insufficient` —
was wrong. Measured across the calibration script's own families:

| inputs | best *bad* design, any density | reads |
|---|---|---|
| 3 / 4 / 5 / 6 | 0.503 / 0.562 / 0.620 / 0.650 | **thin**, always |
| 7 / 8 | 0.727 / 0.738 | **good** |

**The two blind spots are opposite in character.** Width is *flattering* — above six
inputs a cluster reads `good`, which is why the joint figure stands down there. Density is
*conservative*: at six inputs or fewer, best-bad plateaus in the 0.50-0.65 band, so a
hypercube and a cluster both read thin and nothing overclaims. Standing the verdict down
would swap a true finding ("this archive is thin") for a grey shrug. `SUFFICIENT_DENSITY`
in `calibrate_standard` says as much already: *"below it, thin data is a true finding, not
a false alarm."*

So the verdict is untouched. `min_discriminating_runs_per_input = 20` gates *claims about
arrangement* and emits a `low_density` caveat naming the counts and what would fix them —
and stating that the figure never reads better for a bad design, so nothing is hidden.
Suppressed once the verdict is `good`: the blind spot is that a good design cannot be
praised, not that a bad one can pass.

**And it turned up a live regression of my own from the day before.** `arrangement_note`
read `efficiency_score`, which is taken over the *declared* envelope — defaulting to the
range the runs happen to span. A tight cluster is therefore measured over its own bounding
box and scored **0.957, higher than the clean hypercube's 0.902** — and was told *"the runs
themselves are well arranged; what is missing is runs, not a better design"*. The exact
opposite of the truth, phrased as advice, on the archive least able to survive it. Across
families the margin at 5 runs per input runs −0.05 to +0.06; it only separates from about
20. The sentence is now silent below the floor, and the causal half is gone at every
density — it compares against the best arrangement of that many runs, which says nothing
about whether more runs would help.

**Provenance fills the gap measurement cannot.** Cohort 5's twenty runs all came from
parametric study 26, `mode: lhs`. No geometric reading can establish that at twenty points;
the database simply knows it. `CohortService._sampling_modes` reports the distinct modes
when **every** contributing run belongs to a study — one ad-hoc job and the answer is
"unknown", because a mixed set is not a designed one. `DesignCoverage.sampling_modes`
carries it, and `provenance_note` renders it only for the three space-filling modes, only
below the density floor, and only while the verdict is not already `good`. An empty list
means *cannot establish*, never *was not designed*, and nothing may read it the other way.

Cohort 5 now reads: **Patchy**, with *"These 20 runs all came from a Latin hypercube study,
so they are space-filling by construction. What the figures above are short of is runs, not
arrangement."*

### The ladder went backwards again, for a different reason — 2026-09-04

*"Untick Fin height and I get 4, 8, 40 and 80 runs = Poorly covered, but 20 runs = Patchy."*
Second report of a non-monotone ladder, second distinct cause.

**A gate, not a reading.** Every measure feeding the verdict is monotone. But the pairwise
term is *ignored* below `min_discriminating_pair_runs` and *counted* above it, and it can
only subtract — so a rung big enough to cross that line reads worse than the rung below.
On cohort 5 with Fin height held and the range widened: at +20 the pairs abstain, at +40 the
projected archive reaches 60 runs, three `X x Fin height` pairs start voting at 0.29, and
the verdict drops back to poor. The readings were right; the *comparison* was not.

`suggestion_plan` now settles the entitlement **once, on the archive as it stands**, and
judges every rung by it — the same principle §4.4 applies to the bar, which is pinned rather
than moving with the archive. `analyse_design` takes `pairs_may_vote` for that purpose.

The cost is a plan quieter than the panel will be once the runs land, which §5 elsewhere
calls out as worse than no plan if left unsaid — so a `pairs_unlock_later` warning says it.

**A methodology error of mine, worth recording.** Every probe in this saga read
`job.inputs` for a cohort's `member_job_ids`. The app measures the ingested `study_result`
rows for the *resolved* membership, which includes runs adopted from commissioned studies
(`auto_include_study_ids`). Cohort 6 has 40 fixed members and **80** resolved runs. So the
figures I quoted for it — and the test protocol I wrote — were for a set the panel never
measures. Read `study_result` for `source_kind='cohort'`, not the member list.

### The choice pairs found real confounding, and it was dismissed — 2026-09-04

*"Why is Base thickness x Fan speed Unevenly spread but all others (excluding the ones for
Plate material) Well spread?"* The parenthesis is the finding: four rows reading **0.000**
were set aside as noise. They were not noise. On cohort 6's 80 runs:

| input | Aluminium (60 runs) | Magnesium (20 runs) |
|---|---|---|
| base thickness | 2.20-14.10 (full) | 5.10-7.90 |
| fan speed | 21-100 (full) | 41-60 |
| fin count | 6-28 (full) | 18-24 |
| fin height | 5.80-44.70 (full) | 15.40-24.50 |

Magnesium sits in the middle third of *every* input. A surrogate would answer confidently
about Magnesium at 40 mm fin height having never seen it above 24.5 — the confounding this
pass was built to catch, found on the first real cohort with a varying choice, and read as
noise because a red chip says nothing. `PairCoverage.detail` now carries the finding in
words and the row prints it: *"Magnesium was only run between 15.4 and 24.5, out of 5.8 to
44.7."*

**And the pair that prompted the question is real.** `Base thickness x Fan speed` scores
0.681 against a 0.70 line — the worst of the six numeric pairs, where at 40 runs it had been
the best at 1.000. That is the arrangement reading behaving as designed: its bar rises with
the run count, and the union of two independent 40-run hypercubes is not an 80-run
hypercube. Marginal, correct, and exactly why it is reported and never allowed to judge.

Two pre-existing failures in `test_coverage_suggest_design.py` (`..._lattice_points_are_
never_proposed`, `..._remaining_lattice_shrinks_with_a_warning`) reproduce with this round
reverted: they are the two tests the lattice-dedup fix above says "had to be rewritten
rather than the code softened", and the rewrite did not land.

---

## Coverage: where this feature stands — 2026-09-03

The design-space coverage work (ADR-0020) was built before this session and then driven
through the UI for the first time, which found **eleven defects in two days**. They are
written up individually above; this is the shape of them, for whoever picks it up next.

**Nine of the eleven lived at a seam** where two individually-correct components meet:

| seam | what went wrong |
|---|---|
| engine ↔ response schema | `varying_kind` widened in one and not the other — 500 on every affected cohort |
| engine ↔ chart | promotion dropped `pinned_value`; charts read raw rows, the engine reads through `PointSet.values` |
| archive ↔ *projected* archive | the constants fallback was all-or-nothing, so projection lost held values |
| router ↔ service | two `_FILTER_FIELDS` tuples; a field accepted by the API and stored by nobody |
| matrix ↔ matrix | one axis rule, two implementations, drifted within a day |
| histograms ↔ matrix | same again, one row against the other |
| control ↔ payload | update-only field in a shared payload — 422 on preview |
| tick ↔ scope | two questions answered by one mechanism, so the tick was inert |

Every component's own tests passed each time. The tests that now exist assert the **seam**:
what one side can emit against what the other accepts, and that a rule has exactly one
implementation. That is the transferable lesson — see
[[feedback_test_the_contract_between_surfaces]], of which this is now the largest instance.

**Known outstanding**

- `tests/studies/test_coverage_study_from_suggestions.py::test_a_user_without_the_study_permission_cannot_commit_runs`
  fails and is **not** from this session's work — ruled out against three separate change
  sets by reverting each independently. The reading is that the test grants a custom role
  with only `LIVE_JOBS_VIEW` and never grants project visibility, so the ACL intersect
  empties the membership and coverage legitimately has nothing to suggest. That would make
  the test wrong rather than the code; it is untracked WIP and was left alone.
- The coverage package is **untracked in git**, so there is no HEAD to diff against when
  triaging. Worth committing before the next round.
- The NAFEMS deck's fill-the-gaps screenshot is stale — the card's controls and layout have
  changed several times since it was captured. The coverage *plots* are unaffected.

### Two sentences the coverage round left behind — 2026-09-04

Found while re-scoping the NAFEMS runbook, and both the same failure: a string that was
true when written and was not revisited when the behaviour under it moved.

**"Covering it needs a new cohort that varies it."** The `pinned_factor` warning
(`coverage/report.py::_pinned_warning`) has said that since the warning existed, and it
was correct while a held-constant input could not be swept from where you were standing.
Promotable choices (§11.5, 2026-09-03) made it false, and worse than false: the panel
told the reader to go and build a new cohort **directly above a card offering to sweep
that very input into this one**. Now: *"Covering it needs runs that vary it."*

It deliberately stops there. Naming the tick would be the same class of error in the
other direction — a pinned input is promotable only where the descriptor declares a
domain, so a `material` or `multiselect` held constant has no tick to point at, and the
warning would then promise a control that is not on the page. The division is: the
warning states the finding, the *Fill the gaps* card makes the offer, and the card
already renders one exactly when there is one to make.
`test_a_pinned_factor_warning_asks_for_varying_runs_not_a_new_cohort` asserts the new
phrasing **and** the absence of the old, so the retired claim cannot come back.

**"…and are not saved on the cohort."** The caption under *Where the runs came from*
(`ui/pages/cohorts.py`) outlived migration 0097, which promoted source and study from
preview-only controls to stored filters. So a live cohort's owner was told a filter it
was still honouring had been discarded — the worst direction for this error, since it
invites re-adding a constraint that is already in force. The user chapter
(`docs/user/90-cohorts-and-surrogates.md`) has said *"saved with the cohort"* since the
migration; this is the surface catching up to the documentation rather than the other
way round.

**Neither was found by a test, and neither could have been.** Both are strings whose
correctness depends on behaviour elsewhere in the system, which is the same shape as the
nine seam defects above — just with prose on one side of the seam instead of a schema.
The cheap guard is what the new test does: assert the retired claim is *absent*, not only
that the current one is present.

### A cohort of ad-hoc runs had no fields — the alias was not the key — 2026-09-04

*"Field surrogates — No field method could fit this data. Only 0 usable run(s) with a
retained 'temperature_field' snapshot; need at least 2."* On a 20-member cohort whose
every member had a `temperature_field.vtu` sitting in its outputs directory.

**`session.get(Tool, job.tool_alias)`.** `Tool.id` is the primary key and `alias` is a
separate unique column, so that asked for the row whose *id* is the string
`"finned_plate"` and got `None`. `_scalars_from_disk` then took its
`if tool is not None else []` branch and wrote `retained_files = []`.

Three things kept it hidden for months:

- **The branch it fell into reads as a legitimate state.** "This job's tool was
  uninstalled" is a real condition with a real empty answer, so the guard looked like
  care rather than a swallowed lookup failure.
- **Scalars need no tool.** `_extract_scalars(doc)` takes the document alone, so the
  Results matrix, the coverage panel and the scalar surrogate were all fully populated.
  Only the file half was empty.
- **Nothing took this branch until now.** A parametric child already has a
  `study_result` row written by `record_study_result` at completion, so
  `populate_study_results` reuses it and never reads disk. The disk path exists *for*
  ad-hoc members, and the NAFEMS runbook's re-scope to a hand-run archive
  (2026-09-04) was the first cohort made entirely of them.

The symptom surfaced three steps from the cause — cohort build → populate → fit — and
named a *file retention* problem, which is the one thing that was fine.

**The fix is two changes, because the first one alone helps nobody who already has a
cohort.** `populate_study_results` skips a member it already holds, so every row written
before the fix keeps its empty list forever and the field fit keeps failing. The existing-
row branch now re-reads disk when `retained_files` is empty and backfills it — via
`_scalars_from_disk` and deliberately **not** `_scalars_for_job`, whose "prefer an
existing `study_result`" rule would hand back the very row being healed. A member that
genuinely produced no files re-reads a small JSON document each populate and stays empty;
that is the cost of not being able to tell "no files" from "extraction failed" from the
row alone, and it is the right side to err on.

**Two tests, at the seam rather than either side of it.** One asserts the mechanism
directly — `session.get(Tool, alias) is None` while the aliased select finds the row —
then that an ad-hoc member's file refs survive populate, *and* that scalars still land
(so a "fix" cannot trade one half of the document for the other). The other writes a
damaged row first and asserts populate repairs it while reporting `written == 0`.

**The generalisation, and it is the same one as the nine seam defects above.** A lookup
whose failure mode is indistinguishable from a legitimate empty result will not be caught
by either side's own tests. `Tool` is the only model in the tree keyed by a surrogate `id`
while every FK to it travels as `alias` — `Job.tool_alias`, `ToolVersion.alias`,
`StudyResult` via the job — so this call site was the one place the two conventions met.
It was also the only `session.get(Tool, ...)` in the codebase.

### A surrogate stopped being the tool it stood in for — 2026-09-07

Five defects on one page — the model detail page and the tool it exports —
found by opening *Finned Cold Plate - User runs surrogate*, a `combined` model
over four numbers and one categorical (`material`: Aluminium / Magnesium).
They are one theme: **the synthesised artefact kept the model's view of the
world and dropped the tool's.**

**1 — The exported tool asked for a design the source tool would refuse.**
`synthesise_descriptor` built every numeric factor as a bare `float` with the
training range. `finned_plate` declares `fin_count` as an `integer` on a step of
1, bounded 6–28, and declares `material` with radio buttons; the export produced
a float on no step, unbounded, in a dropdown. A descriptor's *rules* — type,
`precision`, `minimum`/`maximum`, `widget`, `choice_widget`, and the cross-field
`sum_check` — are as much a part of what makes a design valid as the numbers
are, and none of them travelled. The exporter carried `label` and `unit` only,
because those were the two the form *reads*; the rest are what it *enforces*,
and nothing on the page showed their absence.

The fix threads the whole source declaration through as `source_inputs` and
splits the range question in two, which is the part worth keeping. The training
/ declared envelope is a **coverage** limit: advisory by default (§7.3), a hard
gate only under `block_extrapolation`, because a user may knowingly extrapolate
and the badge is what warns them. The source tool's `minimum`/`maximum` is a
**validity** limit — a negative fin count was never a legal design, surrogate or
not — so `_hard_bounds` enforces it either way and intersects the two when both
apply. Integer bounds are pulled *inward* to whole numbers (the descriptor
refuses a fractional bound on an integer input, and rounding outward would admit
a value the source tool rejects).

`sum_check` is re-attached in a post-pass rather than in the factor loop,
because a check may span a varied factor and a held constant and the members are
only all known once both lists exist. It is carried **only** when every member
survived into the exported form: a partial check names an input the descriptor
does not declare, which the descriptor validator rightly refuses.

**2 — A categorical factor was rendered as a numeric slider.** The predicted-
field viewer built one `ui.slider` per factor. `material` has no `min`/`max`, so
`_bound` returned its 0.0/1.0 defaults, `_initial_point` took the midpoint, and
the viewer opened on *"'material' was '0.5', which this model was not trained on.
It saw: Aluminium, Magnesium."* — a correct refusal from the encoder, three
layers below where the mistake was made. The API schema was complicit:
`point: dict[str, float]` could not have carried `"Aluminium"` even if the UI
had sent it. Now a factor with `levels` gets a picker, the point starts at the
first level, and the schema takes `float | str`.

**3 — Every control and axis showed an identifier.** `version.factors` and
`version.responses` freeze *names*; labels live in the source descriptor and had
to be resolved and threaded in. `_tool_meta` did that for outputs — but only
those with `type == "number"`, so a **field** output (declared as a `file`) fell
through and the field panels, viewer title and axes all read `temperature_field`.
It also appended the identifier in brackets (`Mass (kg) (mass)`), which is the
opposite of the rule. It now returns input labels as well, both maps cover every
declared field, and the bracketed identifier is gone.

**4 — Two plots were unreadable.** `_render_diagnostics` put four items in one
flex row: caption, scree chart (`flex-1`), caption, coefficient chart
(`flex-1`). The captions took their content width, the two Plotly divs shared
what was left, and each rendered as a slice of an axis — the user's report was
that they *"can't even figure out what they should be."* Each caption now sits
above its own chart in a `flex-1 min-w-0` column with a `280px` floor, so the
row wraps to one chart per line rather than compressing both. `min-w-0` is the
load-bearing half: a flex item defaults to `min-width: auto`, which is why the
plots overflowed their share instead of fitting it.

**5 — The page led with the wrong half.** A `combined` version rendered its
field panels first — POD modes, a 3-D viewer — pushing the scalar Q² table, the
thing most readers open the page for, below several screens. Scalars now come
first, and each half is a `section_card` of its own rather than a run of loose
panels; the per-field quality panel drops its own card (a card nested in a card
reads as a framing error) and becomes a headed block inside the field card.

**What generalises.** Defects 1–3 are the same shape as the nine seam defects of
2026-09-01: a producer and a consumer that are each individually correct.
`synthesise_descriptor` builds a valid descriptor; `ToolDescriptor` validates
faithfully; the field encoder refuses an unknown level exactly as designed. What
was missing was any assertion that the *derived* artefact still means what the
original meant. The cheap guard is to round-trip a real descriptor through the
synthesis and compare the rules field by field, which is what the new tests do —
not to test the synthesiser against a hand-written fixture that shares its
author's blind spot.

**Three more, from the same session, all in the same viewer.** The first two are
the same kind of mistake as #4 above — a chart that is arithmetically right and
readable as wrong.

- **A value-vs-node line beside a 3-D mesh.** A geometry-bearing bundle drew
  both: the `Mesh3d` over real coordinates *and* a 59,049-point line whose
  x-axis is the mesh's internal node ordering. The line is the honest universal
  view for a `.npy` field with no mesh; beside a real geometry it is the same
  data plotted against an index that means nothing. It is now hidden — and not
  recomputed — whenever the mesh view builds.
- **`POD mode` ticked at 1.5.** Plotly's automatic ticks put half-modes on any
  fit that retained two or three, which is the common case. `_mode_axis` pins
  `tickvals` to the actual mode numbers.
- **A negative mode coefficient read as a fault.** It is not: the snapshots are
  mean-centred before the SVD, so `field = mu + Σ coeff·basis` makes a
  coefficient a *signed* displacement from the average field, and a POD basis
  vector's direction is defined only up to a sign. About half of all designs sit
  below zero on any mode. The number was right; the caption — *"how much of each
  pattern this design needs"* — described a non-negative quantity, so the reader
  was correct to distrust it. The axis, the caption and the help chapter now say
  *leaning from the average field*, and the zero line is drawn explicitly so the
  reference the bars are measured against is visible.

The through-line for all three: a chart is a claim about the data, and a caption
that overstates it is a defect in the same way a wrong number is.

### "Track only" was recorded everywhere except where it was declared — 2026-09-07

`goal: track` means *reported but not chased*, and the reporting half was
missing from the study page itself. The value was never lost: the runtime
builds a run's response vector from **every** declared response
(`runtime.py:944` takes all of `config["responses"]`, no goal filter), and for a
real-tool target `record_study_result` writes every tool output onto the run's
`study_result` row. It simply had no column.

All three result surfaces were built from one list —
`_objectives_from_config`, which skips anything that isn't minimise/maximise:

- the **Runs table** and its **CSV** (objectives + factors),
- the **Best designs / Pareto** table (same),
- the **design-space explorer**, whose `axes` list is objectives + factors, so a
  tracked response was not even offered as an axis or a colour.

The result: a tracked value could be read on the job page, in Compare, or
through a Job Cohort over the study — anywhere except the study that asked for
it. The user's question was "where can I see the track-only outputs?", and the
honest answer was "not here".

**The fix keeps the two lists separate rather than merging them.** A new
`_tracked_from_config` parses the `track` responses; `_derive_runs` reads their
values into `track_vals` alongside `obj_vals`; the two tables and the explorer
take a `tracked` argument. Merging tracked into `objectives` would have been a
smaller diff and quietly wrong — `objectives` feeds `_pareto_indices`,
`_weighted_scores` and the best-design ranking, so a tracked response would have
changed the shape of the front and the ranking of every design. Two tests pin
that: Pareto membership and the weighted score must be *identical* with and
without the tracked list.

The explorer gets a third picker section (`OBJECTIVES` / `TRACKED` / `FACTORS`)
rather than folding tracked into either — `sectioned_options` already drops an
empty group, so a study with no tracked responses looks exactly as it did.

Two things needed no change and are worth recording so nobody "fixes" them:
the scatter's **hover** already dumped every scalar on the run, so tracked
values were always in the tooltip; and the **`_axis_value`** dispatch was
already keyed on `axis["kind"]`, so adding a third kind was a branch, not a
rework.

**Not fixed, and adjacent.** The **Results warehouse** lists only tools that
declare a KPI, and `synthesise_descriptor` does not carry the source tool's
`kpi` flags onto an exported surrogate — every output of
`surrogate_finned_plate_21` is `kpi: False`. So an exported surrogate never
appears on that page at all. Same family as the input-rule gap fixed earlier
today: the export keeps the model's view and drops the tool's.

### Surrogates learn each result on the scale that fits it — ADR-0021 — 2026-09-16

**Trigger.** The Finned Cold Plate surrogate built on 70 runs predicted peak
component temperature *worse* than the one built on 20 (misses of 2.2 and
3.8 °C). The data were clean; the response is a power law that climbs steeply
into the few-fins, low-fan corner, and one set of GP length scales cannot serve
both that corner and the flat region the user tested.

**Mechanism.** `Surrogate.output_scale` (`as_measured` | `log`). A log fit
learns `log(y)`; `predict` returns `exp(μ)` and the delta-method `exp(μ)·σ`.
Cross-validation runs through a `TransformedTargetRegressor`, so Q², RMSE and
the parity pairs are always in the result's own units and the two scales are
comparable like for like. `relative_sigma` stays on the learned scale
(`σ / std(log y)`), so the ADR-0020 coverage thresholds keep their calibration;
`coverage/gp.py` now calls it instead of dividing the delta-method σ by
`std(y)`, which would have made hot corners look less supported.
`conditioned_on` fits its frozen GP on `log(y)`. `__setstate__` defaults
old bundles to `as_measured`, so nothing already exported changes.

**The decision** lives in `studies/surrogate/scale.py:fit_with_output_scale`,
per result, returning the surrogate plus a `ScaleDecision` that the service
writes into `version.responses` (what refit needs) and `version.quality`
(the evidence the UI shows). No migration.

**Three things the build found.**
1. *The rule could contradict the table.* The first rule used repeated CV only;
   on real data it picked log for fan power while the displayed error for log
   was the larger number. The displayed check now comes first — which also
   made it the cost fix below.
2. *`Both` was 8× slower than as measured* (169 s on the 373-run model — four
   methods would have neared the 600 s in-app request ceiling). Displayed check
   first plus stopping at the first lost repeat: 65 s (3×).
3. *Refit silently changed the method.* `ModelRefitRequest.surrogate_kind`
   defaulted to `"gp"`; the service treats any value as explicit; the button
   sends `{}`. Model 18 in the demo database shows it (v1 Matérn, v2 smooth GP).
   Default is now `None`. Refit also gained a dialog, because without one a model
   built before this change could never be refitted on a log scale.

**Measured, not assumed.** The shipped code was benchmarked over every training
set in the demo database (outer 5-fold, decision taken on each training fold):
GP 60 better / 65 same / 13 worse of 138 (mean −14%, worst 1.75×), polynomial
25 / 36 / 2 (−20%, worst 1.07×), gradient boosting 9 / 59 / 1 (−3%). Forcing log
everywhere was 32 worse of 82 GP fits and up to 10,843× worse on a polynomial
mass fit — hence per result, visible, overridable, and the ⚠ at 1.5×.

**Alongside:** exported field `.vtu` files carried the reference training run's
own arrays (a stale `temperature` beside the `temperature_field` prediction);
`models/runtime.py:_write_field_file` now calls `mesh.clear_data()` first.

### Each result fits its own way — ADR-0022 — 2026-09-17

**Why.** ADR-0021 let a result choose its output scale, but a model still used one
method for every result and inputs as measured. Three benchmarks over every
training set in the demo database (outer 5-fold CV, choices made on training
folds only) showed each restriction costs accuracy, and that combining a
per-result method, output scale and — for Gaussian processes — input scale under
the ADR-0021 "clearly and consistently" rule helped 45 of 69 results, left 17
alone and hurt 7 (typical −12%, worst 1.68×). The "lowest CV error wins" version
hurt 16 of 69 (worst 3.4×): single CV scores are too noisy to pick by alone.

**Engine** — `studies/surrogate/tuning.py`. `ResultTuner` fits a result's
candidates once (method × output scale × input scale; log inputs only for GP
methods), caches repeated-check scores, and can `decide(primary, override)` for
any primary method without refitting — which is what lets Fit & compare re-render
the table when the user changes the kept method. The **baseline** is the
starting method exactly as measured — "before tuning" in every table — and log
output is a tuning option like any other (it briefly included the ADR-0021
log-output choice, so a "before tuning" table showed log scales). `scale.py` is gone.
The starting method is **suggested from before-tuning scores**: ranked after tuning,
every method whose results all moved elsewhere tied for best, so a "best" polynomial
fitted none of its results. A baseline whose own repeated checks vary more than 3-fold
is judged on average and worst case, not repeat by repeat (Matérn on a 20-run air
velocity had been kept at a displayed Q² of 0.35).

**Log inputs** — `Surrogate.log_factors`; `_encode` takes the log, so every caller
(profiler, coverage, exported tool, BO) keeps passing real units, and
`training_inputs`/`ranges`/`means` stay real. Eligibility needs positive training
values **and** a declared tool minimum above zero — a tool that allows 0 could hand
an exported surrogate a value with no log; `_encode` raises rather than emit NaN.
`conditioned_on` now rebuilds real-unit training inputs from the points (it used
the encoded matrix, which would have been logged).

**One version, several methods.** `surrogate_method` stays the primary method;
each result's `method`/`output_scale`/`input_scale`/`log_factors`/`tuning_*` live in
`responses`, the full evidence in `quality[r].tuning`. No migration. Consumers
that assumed one method changed: the exporter declares `<name>_std` per result
(`has_std`), the runtime support guard takes sigma from the first result **with
variance** (a polynomial-first model silently lost `support_sigma`), and displays
say *Mixed methods*.

**Background fits** — `models/fit_jobs.py`. Tuning is up to 12 fits per result
plus repeats: 129 s for six results over 70 runs, 258 s for three results over 133
runs. Compare/create/refit gained `…/jobs` endpoints running the same helper in its
own session; `GET /api/models/fit-jobs/{id}` reports progress; the page shows a
progress dialog. Jobs are in-process and per-user, forgotten an hour after
finishing — a restart loses a running fit, which is safe because nothing persists
until it completes. The synchronous endpoints remain for scripts and tests.

*Fits in progress* (added 2026-09-17): each job keeps a `title` (model or source
name) and its `context` (request body, plus `model_id` for a refit).
`GET /api/models/fit-jobs` lists the caller's jobs without result bodies; the
Surrogate Models page shows them in a panel (polls every 3 s while one runs).
`/models?fit_job=<id>` reopens a comparison: the form is refilled from `context`
(the output filter too, so Build trains on the runs it compared) and the result
rendered, after a deferred `ui.timer` so watching a running fit doesn't hold the
page build. The poller treats a failed progress check as a miss (30 allowed), not
a failed fit.

*Lock hygiene found with it:* a cohort source's `populate_study_results` always
runs a DELETE, so the fit's session held SQLite's write lock for the whole fit;
`_ensure_cohort_results` now commits straight after. `resolve_members(stamp=False)`
for read-only callers (cohort list, results, ingestion): stamping
`last_resolved_at` made the list a write, and two concurrent lists raised
`StaleDataError`. Session validation touches `last_activity_at` with a core
UPDATE (no row-count check) for the same reason.

**Refit** reproduces the version: `_recorded_tuning` reads ADR-0022 rows, maps
ADR-0021 rows (`both`/`log` → automatic, user output-scale → an override on the
primary method) and treats older versions as *off*.

**Warning.** Any user choice with a higher displayed error than the best
combination tried shows ⚠ — no ratio (the ADR-0021 1.5× rule is gone). MATOFX's own
picks never warn, even when they aren't the lowest number, because the strict rule
deliberately declines lucky winners.


### Migrate carries the whole tool, and completes its links — ADR-0023 — 2026-10-06

**Why.** A report that "upstream/downstream links were lost" after moving three
showcase tools turned out not to be Migrate at all: the destination
(`C:\ProgramData\matofx`) had installed them with `install_showcase_tools.py`,
whose `example_tools/*/descriptor.toml` carried no link fields by design. The
real July bundle, imported into a fresh install via the service and via HTTP,
kept all 6 + 16 wired inputs. But the audit that chased it found the *rest* of
the tool was incomplete or unchecked: `run_cap` and the optimisation
start-approval flag dropped; teams, the approver flag and material classes not
checked (an unresolved team list silently becomes **visible to everyone** once
released — empty `teams` means World); links never reconciled; surrogate tools
carrying a foreign `surrogate_model_version_id`; no `bundle_hash` (V&V
inheritance broken); silent downgrade / demotion / overwrite of archived tools;
workflows without icon, change log, update path or member-order validation — and
**Preview 500'd** for any workflow-plus-members bundle (tool savepoints rolled
back, then the `workflow_member` FK insert failed; only `ItemSkippedError` was
caught).

**Shape** (`migrate/service.py`, spec `designs/migrate-completeness.md`):

- **Bundle v2** (`bundle.py`): `WorkflowManifestEntry.icon_png_b64` (inlined,
  signed, ≤ 1 MB); tool `settings` gains `run_cap` /
  `optimisation_start_approval_required`; workflow members carry `tool_version`.
  A newer `bundle_version` is refused in plain English before schema validation.
- **Export**: surrogate tools → `SurrogateToolNotExportableError` (409);
  `ExportService.resolve_selection(include_required=…)` adds workflow members and
  transitive upstream sources (header `X-Matofx-Included-Tools`; CLI
  `--include-required`; UI checkbox, default on).
- **Import, per tool**: surrogate skip; by-name FKs (unchanged); teams resolved
  (`_resolve_teams` — none left → `teams_pending`); approvers must exist, be
  active and `is_approver`; `ToolService.validate_material_references` (skip, like
  install); licences listed (no registry exists); version rules via
  `versioning.compare_versions` (older/archived/same-but-different → skip; newer →
  update with an "offline until re-promoted" warning); `bundle_hash` from
  `versioning.compute_bundle_hash_from_files` (byte-identical to the directory
  hash — the files reach disk after the savepoint).
- **Links** (`_reconcile_links`): never removed; missing half added — on a tool
  this run created, in place; on a tool already here, through
  `ToolService.update_descriptor` (patch bump + change log + audit), recorded as
  `link_repairs` on the result and the import audit row. "Same content" for
  idempotency ignores `downstream_tools` (`_without_downstream`), since that list
  is reconciled additively.
- **Workflows**: members resolved against this run's tools or the DB; validated
  with the extracted pure `workflows.service.check_upstream_ordering`; admin alias
  → update / no-op / conflict / downgrade (custom alias → skip); a
  `WorkflowVersion` row per import/update; icon written to `workflows.icons_dir`.
- **Transactions**: every per-item exception is now a per-item skip; preview runs
  the real import in one outer savepoint and rolls it back (audit chain head is a
  DB row, so the rollback is clean).
- **Teams pending** (migration `0098`): `tool.teams_pending` /
  `workflow.teams_pending`; `promote_state` refuses to leave `new`
  (`TeamsPendingError` 409 / `WorkflowStateError` 400); cleared by the first save
  with a non-empty team list. Amber banner on Edit Tool, chip on the workflow card,
  "no teams yet" on the tools list.
- **Also fixed on the way**: `InvalidStateTransitionError`,
  `InvalidPromotionError`, `ToolArchivedError` were unmapped and surfaced as a
  generic 500; now 400/409 with their message.

**Showcase chain ships pre-wired** (same day, maintainer's call): the three
`example_tools` descriptors now carry the links copied from the demo install;
`test_showcase_tools_ship_wired_and_still_run_alone` replaces the old
"no wiring" guard. Tests: `tests/migrate/test_migrate_completeness.py` (28).
