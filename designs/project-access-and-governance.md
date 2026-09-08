# Design — Project access, sharing & governance

Status: **Design locked 2026-05-17** (Projects planning conversation). Implementation not started.
Decision record: `decisions/0002-project-access-and-governance.md`. Brief section: `PROJECT_BRIEF.md` §D20 (amended 2026-05-17).
Delivery tracking: `ROADMAP.md` Phase 2 (Hierarchy line).

This is the build-level spec. The ADR records *why*; this records *what to
build*. Everything here is inside the locked brief decisions as amended by
ADR-0002.

---

## 1. Scope at a glance

| Capability | v1 | Deferred |
|---|---|---|
| Per-user 4-role ACL (Creator / Owner / Member / Viewer) | ✅ | |
| Immutable `creator_user_id` (write-once) | ✅ | |
| Create / edit project (name, description, metadata) | ✅ | |
| Share: add/remove/change Owner/Member/Viewer | ✅ | |
| Team-based bulk-pick in the share dialog | ✅ | |
| Admin transfer-ownership (successor Owner + revoke other non-Creator Owners) | ✅ | |
| Archive project/scenario (no-delete; `state` model) | ✅ | |
| Admin-configurable typed metadata (single/multi-select, date, number) | ✅ | |
| Metadata drives home-table columns + filters + export | ✅ | |
| Per-scenario / per-job ACL tightening | | ✅ v1.x |
| Creator-authority-dormant flag (re-enabled disabled Creator) | | ✅ v1.x |
| Free-text / file / user-ref metadata field types | | ✅ v1.x |
| Per-user (non-team) *audit* of read access (§D21 read auditing) | | ✅ Phase 2 separate |

Scenario/job access = wholesale project inheritance. The ACL lives only on
the project.

---

## 2. Data model + migration

Supersedes Phase 1 `hierarchy/orm.py`. Drops `project_team`. Migrations
continue the existing Alembic chain (latest is `0031_job_approvals`);
new revisions start at `0032`. Cumulative + idempotent per C13.

### 2.1 `project` (altered)

| col | change | notes |
|---|---|---|
| `owner_user_id` | **rename → `creator_user_id`** | write-once; never updated after insert |
| `state` | **new** enum | `active` / `archived` / `cancelled`; default `active` |
| `state_changed_at` | **new** ts nullable | §D20 audit pair |
| `state_changed_by_user_id` | **new** FK user nullable | §D20 audit pair |
| `uq_project_owner_name` | **rename → `uq_project_creator_name`** | constraint now `(creator_user_id, name)` |
| `version` | unchanged | optimistic lock retained |

`creator_user_id` is enforced write-once at the service layer (the update
paths never set it) — there is no DB trigger; the service is the only writer.

### 2.2 `project_acl` (new — replaces `project_team`)

| col | type | notes |
|---|---|---|
| `project_id` | FK → `project.id` CASCADE | composite PK part |
| `user_id` | FK → `user.id` RESTRICT | composite PK part — one row per (project,user) |
| `role` | str(16) | CHECK `IN ('owner','member','viewer')` — Creator is *not* stored here |
| `granted_at` | ts | default utcnow |
| `granted_by_user_id` | FK → `user.id` SET NULL | |

Composite PK `(project_id, user_id)` guarantees one role per user per
project. `role` deliberately excludes `creator` — the Creator is the
`project.creator_user_id` column, resolved to Owner-equivalent in code.
Index on `user_id` for the home-table "projects visible to me" query.

### 2.3 `scenario` (altered)

Add the same `state` / `state_changed_at` / `state_changed_by_user_id`
trio. No ACL columns — scenarios inherit the project ACL wholesale.

### 2.4 Metadata taxonomy (new)

**`project_metadata_field`**

| col | type | notes |
|---|---|---|
| `id` | PK | |
| `key` | str(64) unique | stable slug |
| `label` | str(128) | display |
| `field_type` | str(16) | CHECK `IN ('single_select','multi_select','date','number')` |
| `required` | bool | enforced at create + edit |
| `display_order` | int | ordering in forms + table |
| `show_in_table` | bool | surfaces as a home-table column/filter |
| `active` | bool | retire = set false; retained on existing projects |
| audit | | `created_at`, `created_by`, `state_changed_at/by` |

**`project_metadata_value`** (allowed values; select types only)

| col | type | notes |
|---|---|---|
| `id` | PK | |
| `field_id` | FK → `project_metadata_field.id` RESTRICT | |
| `value` | str(255) | |
| `display_order` | int | |
| `active` | bool | retire = set false; retained on existing projects |

Unique `(field_id, value)`.

**`project_metadata`** (per-project selections)

| col | type | notes |
|---|---|---|
| `project_id` | FK → `project.id` CASCADE | |
| `field_id` | FK → `project_metadata_field.id` RESTRICT | |
| `value_id` | FK → `project_metadata_value.id` RESTRICT, nullable | set for select types |
| `value_num` | numeric, nullable | set for `number` |
| `value_date` | date, nullable | set for `date` |

One row per scalar value; a multi-select with 3 chosen values = 3 rows.
A portable CHECK enforces exactly-one-of `(value_id, value_num,
value_date)` is non-null per row. The service validates that the
populated column matches `field.field_type` before insert.

### 2.5 Migration of existing Phase 1 data

There is no production data (Phase 1 is pre-pilot). The migration:

1. Rename `project.owner_user_id` → `creator_user_id` (+ constraint
   rename).
2. Create `project_acl`; for any existing `project_team` rows, the
   migration **does not** auto-expand to users (teams were never the
   intended principal) — instead it logs a one-time data note. With no
   real installs this is a clean cut; documented in the migration
   docstring.
3. Drop `project_team`.
4. Add `state` columns to `project`/`scenario` (default `active`).
5. Create the three metadata tables (empty; admin seeds the taxonomy).

---

## 3. Access resolution

Single function, single user-id lookup (the §D20 Q4 rationale realised):

```
resolve(project, user) -> Level:
    if project.creator_user_id == user.id:        return OWNER
    if user_has_permission(user, projects.see_all): return OWNER   # admin/su
    row = project_acl[(project.id, user.id)]       # one indexed lookup
    if row is None:                                return NONE
    return {owner: OWNER, member: MEMBER, viewer: VIEWER}[row.role]
```

Level ladder: `NONE < VIEWER < MEMBER < OWNER`. Gate mapping:

| Action | Required |
|---|---|
| See project in home table / open it / read jobs+results | VIEWER |
| Add scenario/tool, run job | MEMBER |
| Edit name/description/metadata, manage ACL, archive scenario/job | OWNER |
| Archive the project | OWNER |
| Delete anything | *never* |
| Admin transfer-ownership | `projects.transfer_ownership` or Creator/Owner-of-record per §D20 |

`NONE` → `NotFoundError` (existence-hiding, unchanged Phase 1 behaviour).
Below-required-but-some-access → `AccessDeniedError`. Keep
`ProjectService.access_for()` returning the level without raising for UI
branching.

Last-owner lockout is structurally impossible: the Creator always
resolves to OWNER and `creator_user_id` cannot be cleared.

---

## 4. API surface

The projects router today is list/create/get only. Add (all audited,
all `*Error` → plain-English HTTP):

```
PATCH  /api/projects/{pid}                 edit name/description (OWNER; optimistic-lock via If-Match/version)
POST   /api/projects/{pid}/archive         state → archived (OWNER)
POST   /api/projects/{pid}/acl             add/replace a user's role (OWNER)
DELETE /api/projects/{pid}/acl/{user_id}   revoke a grant (OWNER; cannot target Creator)
GET    /api/projects/{pid}/acl             list grants incl. resolved Creator (VIEWER)
POST   /api/projects/{pid}/transfer        admin transfer-ownership (perm-gated)
GET    /api/projects/{pid}/metadata        current selections (VIEWER)
PUT    /api/projects/{pid}/metadata        replace selections (OWNER; validates required + type)
GET    /api/metadata/fields                taxonomy for the create/edit form (any authed user)
# Admin taxonomy CRUD (admin perm):
GET/POST/PATCH /api/admin/metadata/fields[/{id}]
POST/PATCH     /api/admin/metadata/fields/{id}/values[/{vid}]
```

`POST /api/projects/{pid}/acl` carries `{user_id, role}`; the share
dialog's team bulk-pick is a pure client-side expansion that fans out to
N of these calls (or a batch `PUT /acl` body of `[{user_id, role}...]`,
mirroring the Phase 1 `set_member_teams` shape — implement the batch and
let the dialog use it).

Scenario create already gates on project EDIT; rename the internal
required level constant EDIT→MEMBER but the wire behaviour is unchanged.

---

## 5. UI surfaces

| Surface | File | Change |
|---|---|---|
| Home table | `ui/pages/dashboard.py` | Add columns: Creator, *your role*, `show_in_table` metadata fields, state. Filters: state (default=active only), role, metadata. Rows from the visible-projects query (§3). |
| New project dialog | `ui/pages/dashboard.py` | Add the metadata form (fields from `GET /api/metadata/fields`, required enforced client+server). |
| Project page | `ui/pages/project.py` | Add an **Edit** affordance (name/description/metadata, OWNER-gated, optimistic-lock) and a **Share** panel. Show "Created by …" (frozen). Archive button (OWNER). |
| Share panel | `ui/pages/project.py` (new section) | List grants (Creator pinned, non-removable); add user by search; **team bulk-pick** ("add Team Engineering as Viewers" → expands to user rows); change/revoke role. |
| Admin: metadata taxonomy | `ui/pages/admin_project_metadata.py` (new) + register in `admin.py` nav | CRUD fields + values; retire = deactivate (no delete). |
| Admin: transfer ownership | admin space (new action on a project admin view) | Pick successor → confirm → audited; revokes other non-Creator Owners. |

UI gating uses `access_for()` so buttons are hidden, not just
server-rejected (matches Phase 1 pattern).

---

## 6. Audit

Reuse the existing audit writer. Actions (extending the Phase 1
`hierarchy.*` namespace):

- `hierarchy.project.create` / `.update` / `.archive`
- `hierarchy.project.acl.add` / `.update` / `.remove`
- `hierarchy.project.transfer_ownership` (payload: previous Owners
  revoked + new Owner + frozen `creator_user_id`)
- `hierarchy.project.metadata.update`
- `admin.metadata.field.create|update|deactivate`,
  `admin.metadata.value.create|update|deactivate`

Reads remain unaudited at this layer (per-record read auditing is the
separate §D21/D20 Phase 2 item).

---

## 7. Phased build sequence

1. **Migration + ORM** — `0032` rename+state+drop `project_team`;
   `0033` `project_acl`; `0034` metadata trio. New ORM models; rewrite
   the 41 hierarchy tests to the 4-role model.
2. **Service** — `_resolve_access` single-lookup rewrite; write-once
   `creator_user_id` guard; ACL add/update/remove/batch; archive;
   transfer-ownership (successor + revoke other non-Creator Owners);
   metadata validation + read/write. Audit each.
3. **API** — the §4 endpoints + schemas + error mapping.
4. **UI** — home table columns/filters; create/edit metadata form;
   project edit + share panel + archive; admin taxonomy page; admin
   transfer action.
5. **Docs** — `ARCHITECTURE.md` as-built note; `ROADMAP.md` Phase 2
   tick; hierarchy `README.md` rewrite.

Each step is independently testable; (1)+(2) deliver the governed model
even before UI.

---

## 8. Test plan (extends the existing 41 hierarchy tests)

- Creator auto-assigned; `creator_user_id` immutable across every update
  path; cannot be ACL-removed; resolves to OWNER even when account
  disabled.
- Role resolution: each of Creator/Owner/Member/Viewer/none →
  expected level; `projects.see_all` → OWNER; one-row-per-user
  constraint.
- Member can create scenario + run job, **cannot** edit metadata /
  manage ACL / archive.
- Viewer full read incl. job outputs; zero mutation paths.
- Archive: OWNER-only; Member archive rejected; archived hidden from
  default home query, visible with filter; nothing hard-deletes.
- Transfer-ownership: successor gets Owner; other non-Creator Owners
  revoked; Members/Viewers untouched; Creator untouched; audited with
  full payload; non-admin non-owner → NotFound/AccessDenied.
- Metadata: required enforced at create + edit; type/column-match
  validation; multi-select N-rows; deactivated value retained on
  existing project but absent from the new-project form; every change
  audited.
- Optimistic lock preserved on project edit.
- Existence-hiding: `NONE` user → NotFound for get/edit/share/archive.

---

## 8a. Build status (2026-05-17)

**Fully built and green.** §2–§7 shipped: ORM + migrations
`0033`–`0036` (the extra `0036` seeds the new
`projects.manage_metadata` permission), the service surface incl. the
metadata-taxonomy admin methods, the full project + admin-taxonomy
API, and the UI (dashboard filter row + metadata chips, project
Edit/Share/Archive dialogs, the `Admin → Project metadata` page).
Tests rewritten + extended (`tests/hierarchy/test_metadata_admin.py`,
`tests/api/test_admin_metadata.py`, `tests/api/test_user_directory.py`).

**UX follow-up also shipped 2026-05-17:** a read-only
`GET /api/users/directory` powers a **people picker** in the share
dialog (select `Full name (username)`, not raw user-ids) and the
project page's access list; the project page now renders description +
resolved metadata + the access list grouped Creator / Owners /
Members / Viewers; the metadata form is a single shared module
(`matofx.ui.metadata_form`) reused by the dashboard *New Project*,
both Home Quick-Access *Launch in new project* flows, and the project
*Edit* dialog (consistent render + serialise everywhere).

## 9. Open / deferred (recorded, not silent)

- **Per-scenario / per-job ACL tightening** (§D20 tighten-never-loosen)
  — v1.x; v1 is project-scoped wholesale inheritance.
- **Creator-authority-dormant flag** — v1.x; v1 accepts that a
  re-enabled disabled Creator regains authority (procedural mitigation).
- **Metadata field types beyond select/date/number** (free-text, file,
  user-reference) — v1.x pending customer signal.
- **Read-access auditing** per §D20/§D21 — tracked separately as a
  Phase 2 audit item, not part of this work.
