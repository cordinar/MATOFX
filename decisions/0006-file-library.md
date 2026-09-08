# 0006. File library — reusable, governed tool-input files

Date: 2026-05-21
Status: Accepted (decided 2026-05-21 through the Databases planning conversation — see Revision history)

## Context

Tool input forms already support three file-source modes
(`tools/exec/uploads.py`): **upload**, **pick a file from a previous job**, and
**type a path**. "Pick from a previous job" is the only reuse mechanism today,
and it is ad-hoc: it reaches into prior job history, scoped to whatever the user
can see, with no naming, description, versioning, governance, or integrity
guarantees. Engineers re-running the same baseline geometry / mesh / boundary-
condition CSV across many jobs have no curated place to keep it.

This is the SPDM "data vault / managed file library" gap. The ROADMAP records a
related deferral (Phase 2 "File staging — … CAS …" and the SPDM-scope-deferral
"Managed reference/material data"); the file library is the *user-facing,
governed reuse* layer over the staging mechanism, not the staging mechanism
itself.

Constraints surfaced in the planning conversation:

- A library file must be **assigned to a tool** — a STEP geometry for
  `cad_upload` is meaningless in a heat-transfer field. The library therefore
  needs **layers**, organised by tool (and, within a tool, by file input field),
  so the in-form picker is precise and validation reuses the field's existing
  constraints (`accepted_file_types`, `max_file_size_mb` — `descriptor.py`).
- Reuse must be **reproducible**: a re-run cannot silently change because someone
  edited the underlying file. This mirrors the whole codebase's immutable-
  versioning discipline (tool bundles are per-version immutable —
  `tools/staging.py`).
- Governance must let **subject-matter experts who are regular users** curate the
  shared library without being admins (see ADR-0005).

## Decision

Build a first-class, governed **file library** as a new top-level package
`matofx/library/`, mirroring `tutorials/` and `tools/`. The build-level spec is
`designs/file-library.md`; this ADR records the decisions and rationale.

**1. Two independent axes.** Scope and tool-assignment are orthogonal:

- **Scope (visibility / governance) — tiered:** `personal` → `project` →
  `global`. Personal is the author's own; project is visible to the project ACL
  (ADR-0002); global is the org-wide shared library. A file can be e.g.
  global-scope *and* assigned to `cad_upload`; the two axes never compete.
- **Tool assignment (organisation / applicability) — the "layers":**
  **Tool → file field → file → versions**. Assignment is to the tool **alias**
  (logical tool), not a frozen version; the field is resolved against the tool
  *version* selected at pick time, so a file gracefully stops appearing for a
  field a later version removed. A file **may be assigned to multiple tools**
  (a file↔tool mapping); the add flow defaults to the tool the user is in.
  Field pinning is optional — when unpinned, the field's `accepted_file_types`
  auto-filters which fields the file can fill. This **subsumes** the earlier
  "descriptor-aware categories" idea: the tool/field *is* the category, so every
  file input field gets a library picker with no new descriptor flag.

**2. Immutable, content-hashed versions.** Each library file has 1..N immutable
versions; each version stores a content hash (integrity + a "same bytes"
signal). Publishing a new version never mutates an old one. Bytes live on the
configured `StorageLocation` mount (`tools/paths_service.py`), reusing the
`tutorials/storage.py` blob-on-disk + DB-row pattern.

**3. A 4th file-source mode: "Pick from library".** Added beside upload /
previous-job / path. At submit, the chosen version's bytes are copied into the
job working directory at `inputs/uploaded/<field>/<filename>` exactly as uploads
are today (reusing `promote_draft_uploads_into_working_dir`), so the **runner
contract is unchanged**. Which library version fed which field is recorded for
provenance (a `job_library_file_use` row), and the value frozen into
`inputs.json` is what guarantees reproducibility.

**4. Governance.** A new `LIBRARY_CURATE` permission (closed catalog), carried by
the `library_curator` capability role (ADR-0005). Tiered authority:

- *Personal* scope — any authenticated user.
- *Project* scope — any user with project access (ADR-0002 Member+).
- *Global* scope — requires `LIBRARY_CURATE`.

**5. Toggleable approval for global additions.** A General-Settings runtime
toggle `library_global_publish_approval` (default **ON**) gates anything
*landing in global* — a new global file or a new version of a global file. When
ON, the publish creates an approval request through the existing
`matofx.approvals` workflow; the file sits pending at global scope (not pickable
by regular users) until approved. The submitter **chooses one named approver**
at submit time from the eligible pool (`LIBRARY_CURATE` holders, minus self —
**no self-approval**, so a real gate needs ≥2 library curators). When OFF, a
library curator publishes to global directly. Personal/project additions are
never gated. The directed-approver model is a small extension to
`matofx.approvals` (a `designated_approver_user_id` on the request), shared with
ADR-0007.

**6. Audit everything.** Every mutation — create, edit metadata, new version,
publish, scope change, tool assignment, retire, propose/approve/reject — is
written via the audit writer, for every actor (admins and super-users included).

**7. Metadata** (full field list in the design doc): entry-level
(name, description, tags, file kind, tool assignment(s), steward, handling
label) and version-level (provenance/source, reference, version notes, plus
auto-captured filename/hash/size/extension/status).

**Alternatives considered.** (1) Keep "pick from previous job" as the only reuse
path — rejected: no curation, naming, governance, or reproducibility guarantees.
(2) A flat, tool-agnostic file bag — rejected: a file's validity is tied to a
specific tool's field constraints; a flat bag makes the picker noisy and
validation impossible. (3) One-tool-per-file — rejected: a shared geometry would
have to be re-uploaded per tool, undercutting the point of reuse; multi-tool
assignment with a default-to-current-tool add flow is the chosen middle. (4)
Mutable "latest" files — rejected: breaks reproducibility, against the
codebase's immutable-versioning grain.

## Consequences

- **Depends on ADR-0005** (the `library_curator` capability role + multi-role
  resolver) — built after it.
- **New package** `matofx/library/` (`orm.py`, `service.py`, storage helper,
  `errors.py`); a leaf-ish module the API + UI depend on.
- **New tables:** `library_file`, `library_file_version`, `library_file_tool`
  (file↔tool[,field] mapping), `job_library_file_use` (per-field provenance).
  Migrations chain off head `0042` (after ADR-0005's `0043`). `job_library_file_use`
  FKs *to* `job` (not `job` → it), so the runner needs no new side-effect import.
- **New permission** `LIBRARY_CURATE` in the closed catalog; granted only via the
  `library_curator` capability role (ADR-0005), not to any base role by default.
- **New feature flag** `Feature.FILE_LIBRARY`; routes/nav absent when off.
- **New General Setting** `library_global_publish_approval` (bool, default ON,
  `requires_role="super_user"`) in a new "Data governance" section of
  `GENERAL_SETTINGS_CATALOG` — no migration (lands in `runtime_setting`,
  `domain='general'`).
- **Approvals extension:** `matofx.approvals` gains a directed
  `designated_approver_user_id` (shared with ADR-0007).
- **Form integration:** a 4th file-source mode in `tools/forms` + the UI; submit
  promotion path reused unchanged.
- **ROADMAP:** new Phase 2 row; the "File staging" / "Managed reference data"
  deferral notes cross-reference this ADR.
- **Out of scope for first ship** (deliberate): cross-tool *automatic*
  suggestion of a file to unrelated tools; quota enforcement on library storage
  (advisory only, per the §F31 stance); approval *reassignment* rules beyond
  "an admin can reassign".

## Revision history

- **2026-05-21 — created, Accepted.** Databases planning conversation: two axes
  (tiered scope × tool-assignment layers); immutable content-hashed versions;
  4th "Pick from library" file-source mode reusing the upload-promotion path;
  `LIBRARY_CURATE` via the `library_curator` capability role; toggleable
  (default-ON) directed-approver gate on global additions reusing
  `matofx.approvals`; full audit; metadata per the design doc. Build not started.
