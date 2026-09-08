# 0007. Custom material properties database

Date: 2026-05-21
Status: Accepted (decided 2026-05-21 through the Databases planning conversation — see Revision history)

## Context

`ROADMAP.md` records **"Managed reference/material data"** as a known SPDM-scope
deferral — "Simulation **Data** Management: materials/parameters should be
governed, versioned, searchable objects" — disposition "v1.x — pending customer
signal; reserve schema". The brief's standing mechanism for reviving a deferral
is an ADR the architect rules on (§I47); this ADR is that ruling. Customer signal
has arrived: the materials database is wanted in v1.

The microcosm is the `material_properties` example tool. Its descriptor declares
three `select` inputs whose options are hardcoded (`Component A … E`,
`example_tools/material_properties/descriptor.toml`), and the tool script carries
the property values in inline dicts (`_THERMAL_CONDUCTIVITY` / `_HEAT_CAPACITY`)
with the comment *"Real installs would load these from a customer-managed
registry."* That registry is this feature.

The central tension: descriptor `select` options are **static and frozen** —
"Option-sets are enumerated here, never derived at run time — the descriptor is
the single source of truth" (`tools/descriptor.py`). A materials catalog is the
opposite: it grows and changes without re-publishing every tool that uses it.
Feeding the catalog into a `select` field would violate the frozen-descriptor
principle and couple every tool to catalog churn.

## Decision

Build a governed **materials properties database** as a new top-level package
`matofx/materials/`, mirroring `studies/` and `workflows/`. Build-level spec:
`designs/materials-database.md`.

**1. Resolve the tension with a new descriptor input type `material`.** The
descriptor declares the *requirement* — "this field needs a material, of class
X, exposing properties [k, cp]" — which is static and frozen like every other
descriptor field. The **catalog is the dynamic source** the form pulls from. At
submit, the chosen material **version**'s property values are resolved and
**frozen into `inputs.json`**; the tool reads them like ordinary inputs. The
descriptor's frozen-options rule is never touched, and tools are decoupled from
catalog growth.

**2. Global, curator-governed scope — deliberately *not* tiered.** Unlike the
file library (ADR-0006), materials are org-wide *reference data*: a single
governed source of truth curated by a few SMEs. There is no personal/project
tiering. Any user may *use* (pick) a material; proposing/editing material data
requires `MATERIALS_CURATE`.

**3. Immutable versions; version-pinned at submit.** Each material card has
1..N immutable versions; a job records the `material_version_id` it used
(`job_material_use`) and freezes the resolved values into `inputs.json`. A later
edit to the material cannot retroactively change a finished job — non-negotiable
SPDM reproducibility.

**4. Lightweight class schemas.** Material **classes** (Fluid, Metal, Polymer, …)
declare an expected property set (name + unit). A tool's `material` field can
require a class, and the system validates the chosen material exposes the
required properties. Extra free-form properties beyond the schema are allowed.

**5. Scalar properties first; curves are phase-2.** v1 stores scalar properties
(name / value / unit, plus optional per-property source, uncertainty, notes).
Temperature/condition-dependent **curves are deferred** (mirrors how csv_plot /
curve overlay were deferred in parametric studies) — the model reserves room for
them but v1 does not build them.

**6. Governance via `MATERIALS_CURATE`** (closed catalog), carried by the
`materials_curator` capability role (ADR-0005).

**7. Toggleable, default-ON, mandatory review with a directed approver.** A
General-Settings toggle `materials_change_approval` (default **ON**) gates
material-data changes. When ON, an author with `MATERIALS_CURATE` proposes a new
material/version and **chooses one named approver** at submit from
`MATERIALS_CURATE` holders **minus self** (no self-approval → needs ≥2 materials
curators); the change isn't usable until approved. Reuses the `matofx.approvals`
workflow with the shared `designated_approver_user_id` extension (ADR-0006).
Submit is blocked until all required fields are valid and an approver is chosen.
When OFF, a curator publishes directly. The tool-approver system is untouched.

**8. Audit everything** — create, edit, new version, publish, retire, class/schema
change, propose/approve/reject — every actor, admins/super-users included.

**9. Metadata** (full list in the design doc): material-level (name, aliases,
class, description, tags, steward, confidentiality), version-level
(provenance/data-source, reference/citation, data pedigree, conditions, version
notes, effective date, plus auto status/approval/created), and per-property
(name/value/unit + optional source, uncertainty, notes).

**10. Validation/demo:** port the `material_properties` example tool to consume
the database as the reference integration.

**Alternatives considered.** (1) Feed the catalog into `select` options —
rejected: violates the frozen-descriptor principle and couples tools to catalog
churn. (2) Tiered scope like the file library — rejected: materials are org-wide
reference data, not per-user/project work products; a single governed catalog
matches the SPDM model. (3) Curves from day one — deferred: significant extra
model + UI; scalars deliver the feature, curves are an additive phase-2. (4) Free-
form properties only (no class schema) — rejected: tools couldn't *declare and
validate* a required property set; lightweight classes give that with free-form
extras still allowed.

## Consequences

- **Revives a ROADMAP deferral under §I47.** The "Managed reference/material
  data" row flips from "v1.x — pending customer signal" to "ADR-0007 ruled —
  v1.0; design in `designs/materials-database.md`". The related
  "Metadata/tagging/classification" and "Model/dataset revisioning" rows are
  *partially* advanced (versioned, tagged, classified material objects) but their
  cross-repo-discovery scope remains deferred.
- **Depends on ADR-0005** (the `materials_curator` capability role + multi-role
  resolver) — built after it.
- **New package** `matofx/materials/` (`orm.py`, `service.py`, `errors.py`).
- **New tables:** `material`, `material_version`, `material_property`,
  `material_class`, `material_class_property` (schema), `job_material_use`
  (per-field provenance). Migrations chain off head `0042` (after ADR-0005's
  `0043` and any file-library migrations). `job_material_use` FKs *to* `job`, so
  no `runner/supervisor.py` side-effect import is needed.
- **New descriptor input type** `material` in `tools/descriptor.py`
  (`InputFieldType += "material"`), with `material_class` + `required_properties`
  constraints; the form renders a catalog picker; submit resolves + freezes.
- **New permission** `MATERIALS_CURATE`; granted only via `materials_curator`
  (ADR-0005), not to any base role by default.
- **New feature flag** `Feature.MATERIALS_DATABASE`; routes/nav absent when off.
- **New General Setting** `materials_change_approval` (bool, default ON,
  `requires_role="super_user"`) in the "Data governance" section — no migration.
- **Approvals:** reuses the directed-approver extension from ADR-0006.
- **Example tool** `material_properties` re-ported to read the database (the
  inline dicts become a seeded set of materials).
- **ROADMAP:** new Phase 2 row; SPDM-scope-deferral table updated as above.
- **Out of scope for first ship** (deliberate): temperature/condition-dependent
  **curves**; cross-repo material discovery/search beyond name/class/tag filter;
  unit *conversion* (units are stored + displayed + validated for match, not
  converted — consistent with §G38); allowables/statistical bases (A/B-basis)
  beyond a free-text data-pedigree field.

## Revision history

- **2026-05-21 — created, Accepted.** Databases planning conversation: revives
  the "Managed reference/material data" deferral per §I47; new `material`
  descriptor input type resolving the frozen-options tension; global
  curator-governed catalog (not tiered); immutable versions pinned at submit;
  lightweight class schemas; scalar properties first (curves phase-2);
  `MATERIALS_CURATE` via the `materials_curator` capability role; toggleable
  (default-ON) mandatory directed-approver review reusing `matofx.approvals`;
  full audit; metadata per the design doc; `material_properties` example tool
  re-ported as the reference integration. Build not started.
