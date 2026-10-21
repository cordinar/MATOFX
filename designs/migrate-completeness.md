# Migrate completeness: links, metadata and dependency rules

Status: **Design locked + BUILT 2026-10-06** (decisions from the maintainer, same day). Migration `0098`; tests `tests/migrate/test_migrate_completeness.py`. Not yet clicked through in a browser.
ADR: `decisions/0023-migrate-completeness.md`. Code: `matofx/src/matofx/migrate/`.

## Why

An audit of cross-install export/import (2026-10-06) found the bundle carries the
descriptor faithfully — upstream/downstream links included (verified by importing
the real 2026-07-02 showcase bundle into a fresh install, via the service and via
HTTP) — but that everything *around* the descriptor is incomplete or unchecked:

- two Metadata settings are dropped (`run_cap`, `optimisation_start_approval_required`);
- teams, the approver flag and material classes are not checked the way a normal
  install checks them; an unresolved team list silently becomes **World-visible**
  once released (an empty `teams` list means "everyone");
- links are carried verbatim, never checked or repaired on the other side;
- surrogate tools carry a source-install database id that can point at a different
  model on the destination;
- the imported `tool_version` has no `bundle_hash`, so V&V inheritance breaks;
- an import over an existing tool can downgrade it, demotes a released tool
  silently, and overwrites archived tools;
- workflows lose their icon, get no change-log entry, cannot be updated, and are
  not validated (member order);
- **Preview (dry-run) returns 500** for any bundle with a workflow plus member
  tools the destination lacks (tool savepoints roll back, then the workflow-member
  FK insert fails), and any non-"skip" exception aborts the whole import.

(The maintainer's "links lost" report turned out to be the showcase installer:
`install_showcase_tools.py` installed unwired descriptors. Those now ship pre-wired
— see `designs/showcase-tools.md` step 5.)

## What a bundle carries (v2)

| Item | Carried | Notes |
|---|---|---|
| Tool descriptor (inputs, outputs, groups, layout, presets, rules, KPI flags, retain/delete, resources, backend, duration, approvals policy, email flag, teams, approvers, licences, links) | yes | unchanged |
| Tool files (scripts, icon, guide images) | yes | authoritative `descriptor.json` from the DB |
| Execution target, GPU, resource request | yes | |
| AOS host / HPC cluster / Python env (+ storage location) | by name | missing → skip |
| **Run cap, optimisation start-approval flag** | **yes (new)** | in the opaque `settings` dict — no schema change; absent in v1 bundles → keep destination value / default |
| Workflow name, descriptor (description, teams), members | yes | |
| **Workflow icon** | **yes (new)** | inlined base64 in the signed manifest (≤ 1 MB), like the definition itself |
| **Member tool versions** | **yes (new)** | informational: drives a "different version here" note |
| History, V&V records, approvals, jobs/results, KPI values, favourites, surrogate models, tutorials | no | records of what happened on the source; its audit trail stays there |

`BUNDLE_VERSION` → **2**. Import accepts 1 and 2; a higher version is refused with
"made by a newer MATOFX — upgrade this install first" (today an unknown field
would fail Pydantic validation with an unreadable message).

## Export rules

1. **Surrogate tools are refused** (decision 3): a tool whose descriptor has
   `surrogate_model_version_id` cannot be exported. The picker shows it disabled
   with the reason; the API refuses with a plain message naming the tool.
2. **Include required tools** (picker checkbox, default on): adds the current
   version of every workflow member and, transitively, every upstream tool a
   selected tool's inputs are wired from. Downstream tools are *not* added (they
   aren't needed for the selection to work; their declarations heal later). The
   additions are returned in `X-Matofx-Included-Tools` and shown after export.

## Import rules — per tool

Order of checks; the first failing *skip* wins, *warnings* accumulate.

1. Surrogate tool (old bundle) → **skip**.
2. By-name AOS / HPC / env / storage → **skip** when missing (unchanged).
3. **Teams**: unknown names dropped with a warning. If the descriptor named teams
   and **none** resolve → import with **no teams** and mark **teams pending**
   (decision 2); promotion out of `new` is blocked until teams are set.
4. **Approvers**: dropped when missing, deactivated, or not flagged *Can approve
   runs* (one warning listing each with its reason). Empty list with a policy →
   **skip** (unchanged).
5. **Material inputs**: class must exist and be active; required property slugs
   must be declared → else **skip** (mirrors normal install, which refuses).
6. **Licences**: there is no third-party licence registry to check against, so the
   report lists the licences the tool needs (info) for the operator to confirm.
7. **Version against an existing alias**:
   - none → create;
   - same version, same content → **no-op**;
   - same version, different content → **skip** (conflict, unchanged);
   - lower → **skip** ("would downgrade");
   - archived here → **skip** ("unarchive first");
   - higher → **update**. If it was `test`/`released`, warn that it goes back to
     `new` and is offline until re-promoted (decision 1; preview shows the same).
   "Same content" ignores `downstream_tools` — that list is a permission that is
   reconciled additively (see links), so a repaired list must not turn a
   re-import of the same bundle into a conflict.
8. Settings applied; `tool_version.bundle_hash` computed from the bundle's files
   (same algorithm as `compute_bundle_hash`, over the in-memory files — the disk
   write happens after the savepoint).

## Import rules — links (after all tools)

A link A → B is stored twice: `A.downstream_tools ∋ B` and, on B, inputs with
`downstream_input=True, upstream_tool=A, upstream_output=o`. Rule: **never remove a
link; add the missing half on whichever side exists; name every link that can't be
completed.** No tool is skipped for an incomplete link.

| Case | Situation | Action |
|---|---|---|
| 1 | A and B both in the bundle | kept as-is; warn if `o` isn't among A's outputs |
| 2 | B in the bundle, A already here | if A doesn't list B, add B to `A.downstream_tools` via the normal metadata edit (patch bump + change log + audit), reported as "also updated A"; warn if `o` isn't among A's outputs here; A archived → warn only |
| 3 | B in the bundle, A nowhere | import B, keep the link, warn: "input X is wired to A, which this install doesn't have — it can't take A's output until A is imported" (runs still work: the field falls back to entry) |
| 4 | A in the bundle, B already here | A imports with its list; plus, any tool already here whose inputs are wired from A is added to A's list *as it is created* (so the import of A heals earlier case-3 imports) |
| 5 | A in the bundle, B nowhere | kept; info note — it links up when B arrives |

The preview reports the same actions ("would also update A").

## Import rules — per workflow

1. Members must resolve (in this bundle or already here) — else **skip** (unchanged).
2. **Validated like a save**: members not archived; an upstream member must come
   before the member wired from it (same rule as the workflow editor).
3. **Teams**: as tools (drop unknown; none resolve → teams pending).
4. **Existing alias** (decision 4): custom workflow with that alias → **skip**;
   archived → **skip**; same version + same content → **no-op**; same version,
   different content → **skip** (conflict); lower → **skip** (downgrade); higher →
   **update** (members/descriptor replaced, back to `new` with the same warning as
   tools, change-log entry "Updated from migration bundle").
5. New → created with a change-log entry "Imported from migration bundle".
6. Icon written to `workflows.icons_dir/<alias>.png` after the savepoint commits.
7. A member whose version here differs from the bundle's → info note.

## Transactions and preview

- Each item keeps its own savepoint. **Any** exception inside an item now skips
  that item with "unexpected error: …" (logged with traceback) instead of aborting
  the import with a 500.
- **Preview runs the real import inside one outer savepoint and rolls it back**
  (instead of rolling back each item as it goes). Workflows therefore see the
  tools imported earlier in the same preview, and link repairs are computed by the
  same code that applies them. Disk writes and the import audit row are skipped.

## Teams pending

New boolean `teams_pending` on `tool` and `workflow` (migration 0098). Set only by
import (rule 3). While set: `promote_state` refuses to move the item out of `new`
("set at least one team first — choose World for everyone"). Cleared by any edit
that saves a non-empty team list. Shown as a banner on Edit Tool / Edit Workflow
and a chip in the admin lists.

## Out of scope

Migrating surrogate models, tutorials, V&V records; a third-party licence
registry; exporting custom workflows.
