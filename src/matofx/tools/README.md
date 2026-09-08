# `matofx.tools` — tool registry, descriptors, and state machine

Phase 1 ships the **catalog half** of the tool plugin model: descriptor schema (port from legacy `*_descriptors.json`), a Tool ORM with the New / Test / Released state machine + soft-archive lifecycle, and a `ToolService` that installs / updates / archives / lists / gets / promotes tools with permission-based visibility filtering and audited mutations.

The execution backend (subprocess-based MVP for Phase 1; synthesised-image containerised execution for v1.0 per §F29) wires up in a follow-up step alongside the file-staging layer.

## Why this module exists

Per §I46 of the brief: "Descriptor → image synthesis → containerised execution; the SDK; warm container pools" is v1.0 must-ship. Per the project rules, **tool authors never write Dockerfiles** — descriptors declare high-level runtime needs and MATOFX synthesises images.

The brief also locks "MATOFX does the heavy lifting": tool authors write tiny orchestrators (tens to low-hundreds of lines); the framework owns forms, validation, staging, sandbox, audit, licensing, retries, notifications. The descriptor is the contract that makes that possible — it tells the framework everything it needs.

## Tool states (New / Test / Released)

Every installed tool carries one of three states:

| State | Author | Privileged tier (super_user / administrator) | beta_tester | regular_user |
|---|---|---|---|---|
| `new` | yes | yes | no | no |
| `test` | yes | yes | yes | no |
| `released` | yes | yes | yes | yes |

Promotion (`new → test → released`) is privileged-tier-only and **must follow the forward graph**: `new → released` is rejected (every tool goes through `test` so beta testers actually validate it). **Rollback** is permissive — when a release turns out to be broken, an administrator can demote it back to `test` or `new` regardless of the gap.

Tools are **never deleted**: when there's no further use for one, an admin **archives** it via `ToolService.archive(alias, reason)`. Archived tools stay in the DB so existing jobs' `tool_alias` FK keeps resolving, but they're hidden from `list_visible`, refuse promotion / descriptor edit, and reject new job creation. `unarchive` brings them back.

State visibility is the *only* practical difference between `beta_tester` and `regular_user`. Both have identical hierarchy / data-access rights; beta testers exist specifically to validate pre-release tools.

## Descriptor schema

Pydantic-validated, single source of truth, persisted as JSON at install time. The schema is a port of the legacy two-source model (`*_descriptors.json` + the legacy `tools` DB table) unified into one Pydantic class so there's one place to look and one place to validate.

### Identity and presentation

- `alias` — stable URL-safe identifier; what jobs reference.
- `name`, `version`, `description` — what users see.
- `teams` — list of team names used to group the tool in the home-page browser. Each entry must match an existing `team` row (validated at install / update time). Empty list means the tool isn't associated with any team — it appears in an "Ungrouped" section. The legacy "categories" field has been removed; tools group only by team.
- `icon_filename` — bundle-relative path to a 100x100 PNG (convention: `"<alias>.png"`).
- `presets_filename` — optional `"<alias>_presets.json"` with preset input sets.

### Inputs

- `inputs` — ordered list of `InputField`s. Each field has:
  - Programmatic `name` (must be a Python-style identifier) and human `label`.
  - `type` — `string` / `integer` / `float` / `boolean` / `select` / `multiselect` / `file`.
  - `unit` — display unit (`"mm"`, `"degC"`, ...).
  - `required`, `default`, `help_text`.
  - `options` — required for `select` / `multiselect`.
  - `minimum`, `maximum`, `precision` — numeric bounds and step.
  - `group`, `sub_column` — UI placement.
  - `downstream_input` + `upstream_tool` + `upstream_output` — for fields populated from another tool's outputs (workflow integration). `upstream_tool` names the source tool; `upstream_output` names *which* of that tool's outputs feeds this field (chosen by its label in the editor, stored as the output `name`). When `upstream_output` is omitted the binding falls back to the legacy rule — matching on this field's own `name` — so older descriptors keep working.
  - `sum_check` — cross-field constraint (e.g. component percentages must sum to 100).
- `input_groups` — visual groupings for the form, with `column` / `row` / `sub_columns` layout.
- `input_column_layout` — relative column widths (e.g. `{"column_1": 0.4, "column_2": 0.6}`).

### Outputs

- `outputs` — list of `OutputField`s with `type` (`number` / `string` / `file`), `unit`, `precision`, group placement, and for files: `file_type` (e.g. `csv_plot`, `html_plot`), `filename`, plus plot config (`xaxis_column`, `yaxis_column`, `plot_title`, `xaxis_title`, `yaxis_title`).
- `output_groups`, `output_column_layout` — analogous to inputs.

### Resources, licences, execution

- `resources` — `cpu_cores`, `ram_mb`, `gpu_memory_mb`. `cpu_cores` and `ram_mb` gate local admission (a job over the runner's budget is failed; a lane won't admit past its budget). `requires_gpu` is a derived property (`gpu_memory_mb > 0`) that routes the job to the GPU lane — but only the *sign* is used: GPU admission is by slot count, so the `gpu_memory_mb` **quantity** is not yet consumed by the scheduler (per-memory GPU reservation is unbuilt — `exec/aos_gpu.py:reserve` is a stub).
- `licences_required` — list of `LicenceRequirement(name, count)`. Each licence name must match an entry in the install's licence manager; the scheduler refuses to dispatch the job until the requested counts are available. The list itself answers the user's three questions: *if* licences are required (`len(...) > 0`), *how many* (sum of counts), and *what they are* (the names).
- `execution_backend` — `kind` is one of `subprocess` (Phase 1 default), `hpc_batch`, `aos_calculator`, `aos_repeat_calculator`, `web_app`. Backend-specific fields (`server`, `url`, `port`) are validated in context — `aos_*` requires `server`, `web_app` requires both `url` and `port`. `queue` is **optional** for `hpc_batch`: a cluster with no partitions has no queue name to give, and the HPC targets omit the `--partition` / `-NodeGroups` flag when there is none.
- `duration_class` — `snappy` / `medium` / `long` / `very_long`. **Reserved — no scheduler reads it today** (it does *not* affect queue placement). Stored and preserved on round-trip, but not exposed in the tool editor. Wiring it into lane ordering is a tracked future item.

### Workflow

- `downstream_tools` — aliases of tools whose inputs may be populated from this tool's outputs (the workflow-builder uses this). This is one half of a two-sided handshake: a downstream tool's "auto-populated from upstream tool" input only offers a tool as a source when *that* tool lists the downstream alias here. The tool editor can add the entry for you (the "Make another tool a source" affordance reconciles it on save), so you rarely edit this by hand.
- `retain_files` — the post-run **keep-list**: globs for *undeclared* files to keep on top of the always-kept floor (the job's `inputs/` in full, every declared output, and `logs/`/`heartbeat/`/`launcher.py`/the I/O contract docs). Empty keeps the whole working dir; non-empty deletes undeclared top-level and `outputs/` files that don't match. See `exec/cleanup.py`.
- `delete_io_files` — **legacy** deny-list scrubbed from `inputs/` and `outputs/` on cleanup (minus the floor). Retained for back-compat with descriptors authored before the keep-list model; dropped from the tool editor; empty (a no-op) on new tools.

### Approvals and notifications

- `approvals_policy` — `none` / `pre` / `post` / `both` (per §I46).
- `approver_usernames` — list of users allowed to approve. Required when `approvals_policy != "none"`; rejected when policy is `none`. Each entry must resolve at install / update time to a real user with `is_active=True` and `is_approver=True` — `UnknownApproverError` and `ApproverNotEnabledError` surface bad references with plain-English messages.

  Approvers are kept in sync with the user catalog automatically:

  - When a user is **deactivated** (`UserService.deactivate`) or has their **approver flag turned off** (`UserService.set_approver(False)`), they're auto-removed from every tool's approver list, with one audit event per affected tool.
  - If they're the **only** approver on any tool, the deactivation / flag-off is **refused** with `SoleApproverError` carrying a plain-English list of affected tools (`'Demo Tool' (demo-tool), …`). Add another approver to each affected tool first.

  This invariant lives in `matofx.tools.approvers` (`list_tools_referencing_approver`, `remove_approver_from_tools`); `UserService` lazy-imports it to avoid circular imports.
- `email_notifications` — whether successful / failed completions send email by default.

### Validation

Extra fields are rejected (no silent typos) and the model is frozen (immutable after validation). Cross-field validators check:

- Input / output / group names are unique within a tool.
- Inputs and outputs only reference declared groups.
- `select` / `multiselect` fields supply `options`.
- `downstream_input=true` requires `upstream_tool` (`upstream_output` is optional; absent ⇒ name-match fallback).
- `sum_check` targets must exist as siblings.
- `approvals_policy` and `approver_usernames` agree.
- `execution_backend.kind` and the backend-specific fields agree.

## Loading from disk

Real installs load the descriptor + bundle from a directory on disk:

```python
from matofx.tools import install_from_path, load_descriptor

descriptor = load_descriptor("/var/lib/matofx/tools/render-frames")
# Or in one step:
async with session_scope(factory) as session:
    tool = await install_from_path(
        session, tools, actor_user_id=admin_id,
        bundle_dir="/var/lib/matofx/tools/render-frames",
    )
```

`load_descriptor` looks for `descriptor.toml` first, then `descriptor.json`. On POSIX hosts it warns (with the same `MATOFX_SUPPRESS_PERM_WARNING` opt-out as the config-file warning) when the descriptor file is group/world-readable.

`bundle_path` is validated at every install / update: empty paths, NUL bytes, paths with `..` segments, and paths longer than 512 chars are all rejected with `InvalidBundlePathError`.

## Public surface

```python
from matofx.audit import make_audit_writer
from matofx.config import load_settings
from matofx.db import make_engine, make_session_factory, session_scope
from matofx.tools import ToolDescriptor, make_tool_service

settings = load_settings()
audit = make_audit_writer(settings.audit)
tools = make_tool_service(audit)

descriptor = ToolDescriptor(
    alias="render-frames",
    name="Render frames",
    version="1.0.0",
    short_description="Render an animation sequence.",
    long_description="Renders a GLTF animation to a frame sequence, one PNG per frame.",
    main_script="static/main.py",
    inputs=[
        InputField(name="frames", label="Frames", type="integer", default=24),
    ],
)

engine = make_engine(settings.db)
factory = make_session_factory(engine)

async with session_scope(factory) as session:
    tool = await tools.install(
        session, actor_user_id=admin_id, descriptor=descriptor,
        bundle_path="/var/lib/matofx/tools/render-frames",
    )
    await tools.promote_state(
        session, actor_user_id=admin_id, alias="render-frames", new_state="test",
    )
    visible = await tools.list_visible(session, actor_user_id=alice_id)
```

## Access model

- **Install / update_descriptor / update_bundle / archive / unarchive**: gated on `Permission.TOOLS_INSTALL` (default: super_user, administrator). Anyone else gets `ToolPermissionError`.
- **Promote state**: gated on `Permission.TOOLS_PROMOTE_STATE` (default: super_user, administrator). Forward graph enforced; rollback permissive.
- **Read (`get` / `list_visible`)**: filtered by tool state per `user_can_see_state`. A regular user querying a `new`-state tool by alias gets `ToolNotFoundError` — existence isn't leaked. Archived tools are also hidden by default; pass `include_archived=True` (admin history pane) to see them.
- **Optimistic locking**: `update_descriptor` and `update_bundle` accept an optional `expected_row_version`; pass it in to opt into stale-write rejection (raises `ConcurrentModificationError`). Omitting it falls back to last-writer-wins.
- **Audit**: every install / update / archive / unarchive / state transition / approver cascade records an event. Reads are not audited at this layer; per-record read auditing per §D21 attaches at the UI layer in Phase 2.

## Phase 1 status

- `ToolDescriptor` + `InputField` Pydantic models with validation (URL-safe alias, unique input names, select-requires-options).
- `Tool` ORM with the New / Test / Released CHECK constraint.
- `ToolService` — install / get / list_visible / promote_state — all audited.
- `can_user_see_tool(role, state)` — the visibility matrix in one function.
- Migration `0005_tools.py`.
- 35 tests covering: descriptor validation, state-vocabulary, install permissions across all four roles, duplicate-alias detection, promotion + rollback, invalid state targets, role-based visibility filtering for each role, hidden-by-state on `get`.

## Deferred

**Next turn (still Phase 1):**

- Subprocess-based execution backend that takes a Job + Tool and runs the tool's `main_script`, capturing stdout/stderr to per-job log files and transitioning the job through `created → running → complete/failed`.
- File staging: copy the tool's bundle into a per-job working directory; honour `retain_files` and `delete_io_files` after the run.

**Phase 2 / v1.0:**

- Synthesised-image containerised execution per §F29 (curated base-image library; descriptors declare runtime needs only).
- Warm container pools.
- Resource limits, no-network-by-default, read-only mounts.
- Tool-author SDK (`DDP`-style wrapper, port from legacy `legacy_code/DDP/`).
- Customer-side cryptographic tool signing.
- Workflow definitions (single `workflow_definitions` table per §I46).
- Approvals (pre/post/both/none, signed records, delegation).
- True parallel-DAG workflow execution (v1 is sequential-with-dependency-aware-halt).
