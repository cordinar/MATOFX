# Design — Custom material properties database

Status: **Design locked 2026-05-21** (Databases planning conversation). Implementation not started.
Decision record: `decisions/0007-materials-database.md`. Foundation: `decisions/0005-multi-role-capabilities.md`.
Related: `decisions/0006-file-library.md` (shared approval mechanism). Brief: `PROJECT_BRIEF.md` §F28 (reference-data bind-mount, superseded for managed materials), §G38 (units display-only).
Delivery tracking: `ROADMAP.md` Phase 2 + SPDM-scope-deferral table.

This is the build-level spec. The ADR records *why*; this records *what to build*.
Everything here is inside ADR-0007 + ADR-0005.

---

## 1. Scope at a glance

| Capability | v1 | Deferred |
|---|---|---|
| Material card with immutable versions | ✅ | |
| Per-version scalar properties (name / value / unit) | ✅ | |
| Material classes with an expected property set (schema) | ✅ | |
| New `material` descriptor input type | ✅ | |
| Form catalog picker; resolve + freeze into `inputs.json` at submit | ✅ | |
| Version-pinning + `job_material_use` provenance | ✅ | |
| Material + version + per-property metadata | ✅ | |
| `MATERIALS_CURATE` via `materials_curator` capability role | ✅ | |
| Toggleable (default-ON) directed-approver review | ✅ | |
| Full audit of every mutation | ✅ | |
| Re-port `material_properties` example tool onto the DB | ✅ | |
| Temperature/condition-dependent **curves** | | ✅ phase-2 |
| Cross-repo material discovery/search | | ✅ v1.x |
| Unit **conversion** at the boundary | | ✅ v1.x (§G38) |
| Statistical allowables (A/B-basis) | | ✅ v1.x |

Scope is **global, curator-governed** — no personal/project tiering (deliberate
contrast with the file library).

---

## 2. Data model + migration

New package `matofx/materials/`. Migrations chain off head `0042`, after
ADR-0005's `0043` and any file-library migrations; cumulative + idempotent. PK +
optimistic-lock patterns per house style.

### 2.1 `material_class` + `material_class_property` (the schema)

`material_class`:

| col | type | notes |
|---|---|---|
| `id` | PK | |
| `name` | str(64) unique | Fluid / Metal / Polymer / … |
| `description` | text | |
| `state` | str(16) | active / retired (no delete) |

`material_class_property` (expected properties for a class):

| col | type | notes |
|---|---|---|
| `id` | PK | |
| `class_id` | FK → `material_class.id` RESTRICT | |
| `prop_name` | str(64) | e.g. `thermal_conductivity` |
| `unit` | str(32) | expected unit, e.g. `W/m/K` |
| `required` | bool | required for a material of this class to validate |

UNIQUE `(class_id, prop_name)`. The schema lets a tool require "a Fluid" and the
system check the chosen material exposes the required props in the right units.

### 2.2 `material` (the logical card)

| col | type | notes |
|---|---|---|
| `id` | PK | |
| `name` | str(255) | designation (R); **not** unique (different grades/sources may share a name — pin by id) |
| `aliases` | JSON list | trade/common names (O) |
| `class_id` | FK → `material_class.id` RESTRICT | (R) |
| `description` | text | (R) |
| `tags` | JSON list | (O) |
| `steward_user_id` | FK → `user.id` RESTRICT | defaults to creator |
| `confidentiality` | str(64) nullable | ITAR / proprietary (O) |
| `state` | str(16) | active / pending / retired |
| `created_at` / `created_by_user_id` | | |
| `version` | int | optimistic lock |

### 2.3 `material_version` (immutable)

| col | type | notes |
|---|---|---|
| `id` | PK | |
| `material_id` | FK → `material.id` CASCADE | |
| `ordinal` | int | UNIQUE `(material_id, ordinal)` |
| `provenance` | text | data source: test report / datasheet / supplier / literature / handbook / simulation (R) |
| `reference` | text nullable | document no., DOI, standard, datasheet id (O) |
| `data_pedigree` | str(32) nullable | measured / typical / estimated (O) |
| `conditions` | text nullable | context note; full curves are phase-2 (O) |
| `version_notes` | text nullable | (O) |
| `effective_date` | date nullable | (O) |
| `publish_state` | str(16) | draft / pending / published / rejected |
| `created_at` / `created_by_user_id` | | |

### 2.4 `material_property` (per-version scalar property)

| col | type | notes |
|---|---|---|
| `id` | PK | |
| `material_version_id` | FK → `material_version.id` CASCADE | |
| `prop_name` | str(64) | |
| `value` | numeric | scalar (curves phase-2) |
| `unit` | str(32) | |
| `source` | text nullable | property may cite its own source (O) |
| `uncertainty` | str(64) nullable | tolerance / ± (O) |
| `notes` | text nullable | (O) |

UNIQUE `(material_version_id, prop_name)`. Validation against the class schema:
every `required` class property must be present with a matching unit; extra
properties beyond the schema are allowed.

### 2.5 `job_material_use` (provenance)

| col | type | notes |
|---|---|---|
| `job_id` | FK → `job.id` CASCADE | composite PK part |
| `field_name` | str | composite PK part |
| `material_version_id` | FK → `material_version.id` RESTRICT | what fed the field |

Written at submit (API process). FKs *to* `job`; no runner side-effect import.

---

## 3. Descriptor input type `material`

`InputFieldType += "material"` in `tools/descriptor.py`. A `material` field
declares:

- `material_class` — required class name; the picker filters to materials of
  this class.
- `required_properties` — optional explicit list the field will read (defaults to
  the class's `required` props). Validated at tool-install time against the class
  schema, and at submit against the chosen material version.

The field stores the **selected `material_version_id`** in the draft. At
`JobService.queue_for_runner`, the resolver expands the chosen version's
properties into the job's `inputs.json` (a nested `material` block: id, name,
version ordinal, and the `{prop_name: {value, unit}}` map), and writes
`job_material_use`. The tool reads the frozen values — never the live catalog —
so re-runs are reproducible.

The descriptor's frozen-options rule is untouched: the field declares a
*requirement*, never an enumerated catalog.

---

## 4. Service (`matofx/materials/service.py`)

`MaterialsService` (async, audited). Methods:

- `create_material(...)` — new `material` + first `material_version` +
  properties; validates against the class schema.
- `add_version(material_id, …)` — append immutable version + properties.
- `update_material_metadata(...)` — card-level fields; optimistic-locked.
- `retire(material_id)` — `state='retired'`; no hard delete.
- `list(...)` / `get(...)` / `search(name/class/tag)` — read side (any user).
- `resolve_for_submit(material_version_id, required_properties)` — returns the
  frozen property block for `inputs.json`; raises if a required prop is missing
  or unit-mismatched.

Class/schema admin (`MaterialClassService` or methods on the same service):
`create_class`, `add_class_property`, `retire_class`, gated by
`MATERIALS_CURATE` (or an admin permission — see Open items §12).

Permission gates: read = any authenticated user; create/edit/publish =
`MATERIALS_CURATE`. Every mutation audits `materials.material.<verb>` /
`materials.class.<verb>`.

---

## 5. Approval flow (material-data changes)

Toggle: `materials_change_approval` (General Settings, default ON,
`requires_role='super_user'`). Applies to any new material or new version.

**Toggle ON:**
1. Proposing sets `material_version.publish_state='pending'` (and
   `material.state='pending'` for a brand-new material).
2. The author picks **one** approver at submit from `MATERIALS_CURATE` holders
   **minus self**; request created via `matofx.approvals` with
   `designated_approver_user_id`.
3. Submit blocked until required fields valid **and** approver chosen.
4. Approve → `publish_state='published'`, usable in pickers; audited
   `materials.material.approve`. Reject → `publish_state='rejected'`, prior
   published version remains the usable one; audited `.reject`.

**Toggle OFF:** a `MATERIALS_CURATE` holder publishes directly.

A pending/un-approved material version is **not** offered in the form picker;
only the latest `published` version of each material is selectable.

Edge (deferred): chosen approver unavailable → admin can reassign; finer rules
v1.x. Shared `designated_approver_user_id` extension with ADR-0006 (build once).

---

## 6. API surface

Feature-gated router (absent when `Feature.MATERIALS_DATABASE` off); audited;
`*Error` → plain-English HTTP.

```
GET    /api/materials                         list/search (class, tag, name)
POST   /api/materials                         create material + first version
GET    /api/materials/{id}                    detail + versions + properties
PATCH  /api/materials/{id}                    edit card metadata (optimistic lock)
POST   /api/materials/{id}/versions           add immutable version + properties
POST   /api/materials/{id}/retire             retire (no delete)
GET    /api/materials/picker?class=&field_required_props=  picker query
GET    /api/materials/approvers               eligible approvers for the picker
# Class schema admin (MATERIALS_CURATE / admin):
GET/POST/PATCH /api/materials/classes[/{id}]
POST/PATCH     /api/materials/classes/{id}/properties[/{pid}]
```

Approve/reject reuse the approvals endpoints with the directed-approver
extension.

---

## 7. UI surfaces

| Surface | File | Change |
|---|---|---|
| Materials catalog | `ui/pages/materials.py` (new) | Browse/search by class/tag/name; material detail with version history + property table; create/edit-version forms (property grid + metadata + provenance); approver picker on submit. |
| Class admin | `ui/pages/materials.py` admin section or `admin_*` | Manage classes + expected-property schema (curate-gated). |
| Tool form `material` field | `ui/pages/my_jobs.py` + form widget | Catalog picker filtered by required class; shows resolved property preview; honours required-property validation. |
| Approvals inbox | existing approvals UI | Pending material changes appear for the designated approver. |
| Nav | `ui/_chrome.py` | Feature-gated "Materials" entry. |

UI gating uses the resolved permission so curate controls are hidden, not just
server-rejected.

---

## 8. Audit

`materials.material.create | update | version.add | retire | approve | reject`,
`materials.class.create | update | property.add | retire`, plus
`job.material.use` at submit. Every actor, admins/super-users included.

---

## 9. Re-porting the `material_properties` example tool

The reference integration: replace the inline `_THERMAL_CONDUCTIVITY` /
`_HEAT_CAPACITY` dicts and the hardcoded `Component A…E` select options with a
`material` input field (class e.g. "Component", required props
`thermal_conductivity` + `heat_capacity`). The inline values become a seeded set
of materials (a data seed or an admin import). The tool reads the frozen
`material` block from `inputs.json` and mass-weights as today. This proves the
descriptor input type, resolution/freeze, and reproducibility end-to-end.

---

## 10. Phased build sequence

1. **ORM + migration** — six tables; `materials_curator`/`MATERIALS_CURATE`
   already seeded by ADR-0005's `0043`. Model + migration round-trip tests.
2. **Service** — material/version/property CRUD + class schema + validation +
   `resolve_for_submit`; audit.
3. **Descriptor input type** — `material` type + constraints + install-time
   validation; forms render the picker.
4. **General Setting** — `materials_change_approval` in the "Data governance"
   section; consumer in the publish path.
5. **Approvals extension** — `designated_approver_user_id` (shared; build once if
   not already done in the file-library work).
6. **API** — §6 endpoints + schemas + error mapping + feature gate.
7. **Submit wiring** — resolve + freeze into `inputs.json` + `job_material_use`.
8. **UI** — catalog, class admin, form picker, approvals-inbox rendering, nav.
9. **Example tool re-port** + seed data.
10. **Docs** — `ARCHITECTURE.md` as-built; `ROADMAP.md` tick + deferral-row flip;
    package README.

Gates between slices: `ruff format --check . && ruff check . && mypy && pytest`,
both DB engines.

---

## 11. Test plan

- Class schema: required props enforced; unit-match validated; retired class/prop
  retained on existing materials but unavailable for new ones.
- Version immutability: edit mints a new version; old properties unchanged;
  `ordinal` monotonic + unique; material name intentionally non-unique.
- `material` input type: picker filtered by class; required-property validation;
  install-time descriptor validation against the schema.
- Resolve + freeze: `inputs.json` carries the nested material block;
  `job_material_use` written; a later material edit does **not** change the
  finished job's frozen values (reproducibility).
- Approval gate: toggle ON → pending + directed approver; submit blocked without
  required fields/approver; no self-approval; approve publishes; reject keeps the
  prior published version; toggle OFF → direct publish; non-curator change
  rejected.
- Audit: every mutation + `job.material.use` emits the expected action, including
  admin actors.
- No hard delete; retire is state-only.

---

## 12. Open / deferred (recorded, not silent)

- **Curves** (temperature/condition-dependent properties) — phase-2; the model
  reserves room (`material_property` is scalar now; a sibling table or a
  `value_kind` discriminator lands with curves).
- **Class-schema admin permission** — v1 lets `MATERIALS_CURATE` manage classes;
  if customers want class taxonomy locked to admins, split a
  `MATERIALS_CLASSES_MANAGE` permission later (cheap, additive).
- **Unit conversion** at the boundary — units are stored/displayed/validated for
  *match*, not converted (§G38); conversion is v1.x.
- **Cross-repo discovery/search** beyond name/class/tag filter — v1.x.
- **Statistical allowables** (A/B-basis) beyond the free-text data-pedigree field
  — v1.x.
