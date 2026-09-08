# Design — File library

Status: **Design locked 2026-05-21** (Databases planning conversation). Implementation not started.
Decision record: `decisions/0006-file-library.md`. Foundation: `decisions/0005-multi-role-capabilities.md`.
Brief sections: `PROJECT_BRIEF.md` §F31 (file staging / CAS), §D20 (permissions, amended by ADR-0002/0005).
Delivery tracking: `ROADMAP.md` Phase 2.

This is the build-level spec. The ADR records *why*; this records *what to build*.
Everything here is inside ADR-0006 + ADR-0005.

---

## 1. Scope at a glance

| Capability | v1 | Deferred |
|---|---|---|
| Library file with immutable, content-hashed versions | ✅ | |
| Tiered scope: personal → project → global | ✅ | |
| Tool-assignment layers: tool → field → file (multi-tool) | ✅ | |
| 4th file-source mode "Pick from library" in tool forms | ✅ | |
| Entry + version metadata (provenance, reference, …) | ✅ | |
| `LIBRARY_CURATE` via `library_curator` capability role | ✅ | |
| Toggleable directed-approver gate on global additions (default ON) | ✅ | |
| Full audit of every mutation | ✅ | |
| Per-field provenance (`job_library_file_use`) | ✅ | |
| Hard storage-quota enforcement | | ✅ v1.x (advisory only, per §F31) |
| Auto-suggesting a file to unrelated tools | | ✅ v1.x |
| Approval reassignment policy beyond admin-reassign | | ✅ v1.x |

The two axes — **scope** (who can see/use) and **tool assignment** (what it's
for) — are independent.

---

## 2. Data model + migration

New package `matofx/library/`. Migrations chain off head `0042`, after
ADR-0005's `0043`; cumulative + idempotent. PK pattern
`sa.BigInteger().with_variant(sa.Integer(), "sqlite")`; optimistic lock via
`version` + `version_id_col` where a row is mutable.

### 2.1 `library_file` (the logical entry)

| col | type | notes |
|---|---|---|
| `id` | PK | |
| `name` | str(255) | display title (R) |
| `description` | text | (R) |
| `file_kind` | str(64) nullable | e.g. geometry / mesh / boundary_conditions (O) |
| `tags` | JSON list | (O) |
| `scope` | str(16) | CHECK `IN ('personal','project','global')` |
| `project_id` | FK → `project.id` RESTRICT, nullable | set iff `scope='project'` |
| `owner_user_id` | FK → `user.id` RESTRICT | steward; defaults to creator |
| `handling_label` | str(64) nullable | e.g. proprietary / export-controlled (O) |
| `state` | str(16) | `active` / `pending` / `retired` (no hard delete) |
| `created_at` / `created_by_user_id` | | |
| `version` | int | optimistic lock |

A portable CHECK enforces `project_id` non-null **iff** `scope='project'`. The
entry's `scope` is the *current* scope; scope changes are audited state changes,
never deletions.

### 2.2 `library_file_version` (immutable)

| col | type | notes |
|---|---|---|
| `id` | PK | |
| `library_file_id` | FK → `library_file.id` CASCADE | |
| `ordinal` | int | 1,2,3… per file; UNIQUE `(library_file_id, ordinal)` |
| `stored_path` | str | absolute path under the StorageLocation mount |
| `original_filename` | str(255) | sanitised on disk, original kept here |
| `content_hash` | str(64) | sha-256 hex; integrity + dedup signal |
| `size_bytes` | int | |
| `extension` | str(32) | lower-case, no dot |
| `provenance` | text | source/origin (R) |
| `reference` | text nullable | drawing/spec/doc id, contract, URL (O) |
| `version_notes` | text nullable | what changed + why (O) |
| `publish_state` | str(16) | `draft` / `pending` / `published` / `rejected` |
| `created_at` / `created_by_user_id` | | |

Versions are append-only; an edit mints a new version. `content_hash` lets the
UI flag "these two versions are byte-identical".

### 2.3 `library_file_tool` (the tool-assignment layer)

| col | type | notes |
|---|---|---|
| `id` | PK | |
| `library_file_id` | FK → `library_file.id` CASCADE | |
| `tool_alias` | str | logical tool (NOT a version) |
| `field_name` | str nullable | optional pin to a specific file input field |

UNIQUE `(library_file_id, tool_alias, field_name)`. Multi-tool = N rows. A
NULL `field_name` means "any file field on this tool whose `accepted_file_types`
the file's extension satisfies". `tool_alias` is a soft reference (string), not
an FK, because tools are immutably versioned by alias and the descriptor's field
set is resolved at pick time, not assignment time.

### 2.4 `job_library_file_use` (provenance)

| col | type | notes |
|---|---|---|
| `job_id` | FK → `job.id` CASCADE | composite PK part |
| `field_name` | str | composite PK part |
| `library_file_version_id` | FK → `library_file_version.id` RESTRICT | what fed the field |

Written at submit/promote time (API process) for traceability ("which jobs used
version X?"). FKs *to* `job`; the runner maps no new FK *from* `job`, so no
`runner/supervisor.py` side-effect import is required.

---

## 3. Scope + tool resolution

**Visible-files query** for user U filling tool T, field F (tool version V
selected):

```
candidates = library_file_version
  JOIN library_file lf
  JOIN library_file_tool lft ON lft.library_file_id = lf.id
WHERE lf.state = 'active'
  AND version.publish_state = 'published'
  AND lft.tool_alias = T
  AND (lft.field_name = F OR (lft.field_name IS NULL
        AND version.extension IN accepted_file_types(V, F)))
  AND scope_visible(lf, U)            # see below
```

`scope_visible(lf, U)`:
- `personal` → `lf.owner_user_id == U`
- `project`  → U has ADR-0002 access to `lf.project_id` (Viewer+)
- `global`   → visible to all authenticated users

The picker shows the latest `published` version by default with a "choose
version" affordance. A pending (un-approved) global file is **not** visible here
— it surfaces only in the curator approval inbox until approved.

---

## 4. Service (`matofx/library/service.py`)

`LibraryService` (async, audited). Methods:

- `create_file(...)` — new `library_file` + first `library_file_version`; scope
  defaults to `personal`; assigns ≥1 `library_file_tool` row (defaults to the
  tool the add flow came from).
- `add_version(file_id, …)` — append an immutable version; mints `ordinal+1`.
- `update_metadata(file_id, …)` — entry-level fields; optimistic-locked.
- `assign_tool` / `unassign_tool(file_id, tool_alias, field_name=None)`.
- `set_scope(file_id, new_scope, …)` — the governed transition; raises into the
  approval path when `new_scope='global'` and the toggle is ON (see §5).
- `retire(file_id)` — `state='retired'`; never hard-deletes.
- `list_for_picker(user, tool_alias, field_name, tool_version)` — §3 query.
- `open_version_bytes(version_id, user)` — access-checked read for download +
  promotion.

Permission gates: personal = authenticated; project = ADR-0002 Member+ on the
project; global publish = `LIBRARY_CURATE`. Every method audits
`library.file.<verb>`.

---

## 5. Approval flow (global additions)

Toggle: `library_global_publish_approval` (General Settings, default ON,
`requires_role='super_user'`). "Landing in global" = a new file created at
global scope **or** a new version of an already-global file **or** a
`set_scope(... 'global')` transition.

**Toggle ON:**
1. The action sets the affected `library_file_version.publish_state='pending'`
   (and `library_file.state='pending'` for a brand-new global file).
2. The submitter picks **one** approver at submit from `LIBRARY_CURATE` holders
   **minus self**; the request is created via `matofx.approvals` with
   `designated_approver_user_id` set.
3. Submit is **blocked** until all required fields are valid **and** an approver
   is chosen.
4. Approve → `publish_state='published'`, file becomes pickable; audited
   `library.file.approve`. Reject → `publish_state='rejected'`, the file stays at
   its prior scope/version; audited `library.file.reject`.

**Toggle OFF:** a `LIBRARY_CURATE` holder publishes to global directly; no
picker, no request.

Edge (deferred): if the chosen approver is unavailable, an admin can reassign;
finer rules are v1.x.

---

## 6. Form integration (4th file-source mode)

`tools/forms` + the UI file-input widget gain a **"Pick from library"** mode
beside upload / previous-job / path. Selecting a library version records the
chosen `library_file_version_id` against the draft field. At
`JobService.queue_for_runner` promotion time, the chosen version's bytes are
copied into `inputs/uploaded/<field>/<filename>` via the existing
`promote_draft_uploads_into_working_dir` path (the runner contract is
unchanged), and a `job_library_file_use` row is written. Validation reuses the
selected tool version's `accepted_file_types` / `max_file_size_mb`.

---

## 7. API surface

All audited, all `*Error` → plain-English HTTP, all under a feature-gated router
(absent when `Feature.FILE_LIBRARY` off).

```
GET    /api/library/files                       list (scope+tool filters, ACL-filtered)
POST   /api/library/files                       create file + first version
GET    /api/library/files/{id}                  detail + versions
PATCH  /api/library/files/{id}                  edit entry metadata (optimistic lock)
POST   /api/library/files/{id}/versions         add immutable version
POST   /api/library/files/{id}/scope            change scope (may enter approval)
POST   /api/library/files/{id}/tools            assign tool[/field]
DELETE /api/library/files/{id}/tools/{mapId}    unassign
POST   /api/library/files/{id}/retire           retire (no delete)
GET    /api/library/files/{id}/versions/{vid}/download
GET    /api/library/picker?tool=&field=&version= picker query (§3)
GET    /api/library/approvers?scope=global       eligible approvers for the picker
```

Approve/reject reuse the existing approvals endpoints with the directed-approver
extension.

---

## 8. UI surfaces

| Surface | File | Change |
|---|---|---|
| Library workspace | `ui/pages/library.py` (new) | Browse tree (tool → field → files), filters (scope, tool, tag, kind); create/upload-version; metadata + provenance forms; scope-change with approver picker. |
| Tool form file field | `ui/pages/my_jobs.py` + form widget | 4th "Pick from library" mode; version chooser; honours field constraints. |
| Approvals inbox | existing approvals UI | Pending global library items appear for the designated approver. |
| Nav | `ui/_chrome.py` | Feature-gated "File library" entry. |

UI gating uses the resolved permission so curate/publish controls are hidden,
not just server-rejected.

---

## 9. Audit

`library.file.create | update | version.add | scope.change | tool.assign |
tool.unassign | retire | approve | reject`, plus `job.library_file.use` at
promotion. Every actor, including admins/super-users. Reads of library files for
download are audited at the same level as job-output downloads.

---

## 10. Phased build sequence

1. **ORM + migration** — the four tables; `library_curator`/`LIBRARY_CURATE`
   already seeded by ADR-0005's `0043`, so this migration is additive table
   creation. Tests for model + migration round-trip.
2. **Service** — CRUD, versioning, scope/tool transitions, picker query, audit.
3. **General Setting** — add `library_global_publish_approval` to the catalog +
   the "Data governance" section; consumer reads it in the scope-change path.
4. **Approvals extension** — `designated_approver_user_id` (shared with ADR-0007;
   build once, in whichever feature lands first).
5. **API** — §7 endpoints + schemas + error mapping + feature gate.
6. **Form integration** — 4th file-source mode + promotion-path wiring +
   `job_library_file_use`.
7. **UI** — library workspace, form picker, approvals-inbox rendering, nav.
8. **Docs** — `ARCHITECTURE.md` as-built note; `ROADMAP.md` tick; package README.

Each step independently testable; (1)+(2) deliver the governed model before any
UI. Gates between slices: `ruff format --check . && ruff check . && mypy &&
pytest`, both DB engines.

---

## 11. Test plan

- Version immutability: editing mints a new version; old `content_hash`/bytes
  unchanged; `ordinal` monotonic + unique.
- Scope visibility: personal/project/global each resolve correctly per user;
  ADR-0002 project access honoured; pending global file invisible in picker.
- Tool resolution: field-pinned vs extension-auto-filter; multi-tool; alias
  resolves against the *selected* tool version (field removed in a later version
  → file absent for it).
- Approval gate: toggle ON → pending + directed approver required; submit blocked
  without required fields or approver; no self-approval; approve publishes;
  reject reverts; toggle OFF → direct publish by curator; non-curator global
  publish rejected.
- Promotion: chosen version's bytes land at `inputs/uploaded/<field>/`;
  `job_library_file_use` written; field constraints enforced.
- Audit: every mutation + download emits the expected action, including admin
  actors.
- No hard delete anywhere; retire is reversible-by-state only.

---

## 12. Open / deferred (recorded, not silent)

- Hard storage-quota enforcement on library bytes — advisory only in v1 (§F31).
- Auto-suggesting a file across unrelated tools — v1.x.
- Approval reassignment policy beyond "admin can reassign" — v1.x.
- CAS-style cross-job dedup of library bytes against §F31's `cas_refs` — v1.x
  optimisation (content_hash is already captured, so this is additive later).
