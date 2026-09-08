# Design — V&V (Verification & Validation) tracking

Status: **Design locked 2026-05-27**. Implementation **in flight — slices 0–6 + slice 8 shipped, slice 7 backend-only (UI pending), slices 9–14 not started, all 2026-05-27**.
Decision record: `decisions/0011-vv-tracking.md`. Foundation: `decisions/0005-multi-role-capabilities.md`.
Related: `decisions/0006-file-library.md` (evidence storage + directed-approver mechanism), `decisions/0007-materials-database.md` (governance pattern this mirrors), `decisions/0008-surrogate-family.md` (surrogate model versions reuse `tool_vv_record`), `decisions/0009-optimisation-framework.md` (optimisation studies inherit per-project policy), `decisions/0010-data-lineage.md` (V&V records become lineage nodes), `decisions/0012-kpi-extraction.md` (KPI table filters by V&V status).
Delivery tracking: `ROADMAP.md` Phase 2 + SPDM-scope-deferral table.

This is the build-level spec. The ADR records *why*; this records *what to build*.
Everything here is inside ADR-0011 + ADR-0005.

---

## Implementation checkpoint (2026-05-27)

The server-side V&V system is **functional end-to-end**. A curator
can author records, submit for review, decide through the unified
approvals inbox, projects can opt into the V&V-required policy via the
seeded `vv_required_for_submit` metadata field, and submission gets
blocked appropriately at `JobService.queue_for_runner`. **118 V&V
tests passing** across the 8 slice suites (sqlite; postgres-marked
skipped, will run on CI).

What's still to land for v1.0 (slice 7's UI half + slices 9–14 in
§11): the submit-form badge + envelope-warn banner in
`my_jobs.py` (the API backing it is live; slice 7's UI just hasn't
been wired yet — naturally folds into slice 9's NiceGUI work), the
user-facing NiceGUI workspace (`/vv-records` + `/admin/vv-records`),
per-tool / per-job V&V card surfaces, Compare column-header badges,
lineage integration (the `NodeKind.VV_RECORD` +
`EdgeKind.VERIFIED_BY/VALIDATED_BY` extensions), KPI table filter
integration, ARCHITECTURE §16.24 + ROADMAP tick + SPDM-deferral-table
flip, and the cross-ADR consistency review. None of those are
blocking — the API is complete; UI consumes it.

**Files shipped in slices 0–7** (entry points for resuming):

- Foundations: `tools/versioning.py` (`compute_bundle_hash`,
  `EMPTY_BUNDLE_HASH`); `tools/orm.py` (`ToolVersion.bundle_hash` +
  composite `(alias, bundle_hash)` index); `tools/service.py`
  (`_safe_compute_bundle_hash` wiring in `_record_snapshot`).
- Package: `matofx/vv/__init__.py`, `orm.py`, `errors.py`,
  `service.py`, `bootstrap.py`, `policy.py`, `README.md`.
- Migrations: `0053_tool_version_bundle_hash`, `0054_vv_records`,
  `0055_approval_vv_target`, `0056_vv_curator_role`.
- Permission catalog: `Permission.VV_CURATE` in
  `users/permissions.py`.
- Approvals widening: `approvals/orm.py` columns +
  `approvals/service.py` `create_request` extended for V&V targets.
- API: `api/routers/vv.py` (16 endpoints), `api/routers/approvals.py`
  extended with V&V branches in `_can_act_on` / `_enrich` /
  detail-panel helpers / `submit_decisions`, `api/schemas.py` +14
  DTOs, `api/errors.py` umbrella + status map, `api/deps.py`
  `get_vv_service`, `features.py` `VV_TRACKING`,
  `api/app.py` router registration.
- Hook into job submission: `hierarchy/service.py`
  `JobService.queue_for_runner` now calls
  `matofx.vv.policy.check_submit_policy`.
- Tests: `tests/vv/` (5 files, 91 tests) + `tests/api/test_vv_router.py`
  (9 tests) + `tests/api/test_vv_approvals.py` (3 tests).

**Three pre-existing soft edges to remember when resuming:**

1. `VVService` **requires curate even for draft authorship** (design
   deviation from §5 — the design said "any user records draft, curate
   gates promotion"). Called out in `service.py`'s module docstring;
   easy to revert if a customer pushes back.
2. The router's `_resolve_approval_toggle` is a `True` placeholder.
   When the `vv_record_approval` General Setting catalog row lands
   (currently no slice plans this), the function becomes a real read
   against `general_setting`. One-line change.
3. `bundle_hash` write in `_record_snapshot` is **defensive** —
   missing/malformed bundle paths land as `""` (singleton cohort
   sentinel) rather than raising. Existing test fixtures pass
   `bundle_path="/tools/demo"` (non-existent) so this defensiveness
   kept the existing suite green; a real install with a botched
   bundle path silently loses V&V inheritance for that version.
   Documented; revisit if a customer asks for stricter semantics.

---

## 1. Scope at a glance

| Capability | v1 | Deferred |
|---|---|---|
| `tool_vv_record` — verification + validation claims per tool version | ✅ | |
| `job_vv_record` — V&V claims about an individual job run | ✅ | |
| Surrogate model V&V via the same `tool_vv_record` (no third table) | ✅ | |
| V&V inherits across tool versions with byte-identical bundles (`tool_version.bundle_hash`) | ✅ | |
| Evidence storage via the file library (`vv_evidence` tag) | ✅ | |
| `VV_CURATE` via `vv_curator` capability role | ✅ | |
| Toggleable (default-ON) directed-approver review | ✅ | |
| Approval-target widening on `approval_request` (4 → 6) | ✅ | |
| Per-project `vv_required_for_submit` policy (via ADR-0002 taxonomy) | ✅ | |
| Operating-envelope soft-warn at submit | ✅ | |
| V&V status badges across Admin Tools / Submit / Compare / Lineage / KPI / Model registry | ✅ | |
| `/admin/vv-records` admin surface | ✅ | |
| Standards-cited list (free-text JSON of standard names) | ✅ | |
| Full audit of every mutation | ✅ | |
| Per-version retire (retire from version X.Y.Z onward while keeping the record live for earlier versions) | | ✅ v1.x (bundle-hash inheritance makes this far less necessary — see §6.5) |
| Workflow-level V&V records (composition records) | | ✅ v1.x |
| N-of-M V&V (multiple independent validators required) | | ✅ v1.x |
| Automated regression-test execution **as** a V&V record | | ✅ v1.x |
| External V&V exchange formats (import from PLM / SPDM) | | ✅ v1.x |
| V&V as a *separate* approval gate on every job submission | | Out of scope (conflates flows) |

Governance scope is **global, curator-governed** — no personal/project tiering
(deliberate contrast with the file library; mirrors materials-database).

---

## 2. Data model + migration

New package `matofx/vv/`. Four migrations chain off head `0052` in order:

| Rev | Purpose |
|---|---|
| `0053_tool_version_bundle_hash.py` | Add `tool_version.bundle_hash` (str(64), NOT NULL, server_default `''`) — the SHA-256 fingerprint of the bundle directory's contents. Hash compute is owned by `tools.versioning` (§2.0). The V&V cohort key for inheritance per §6.5. Greenfield-friendly: empty string default means "not yet computed"; `summarise_tool_status` treats empty as a singleton cohort (no inheritance — safe default). |
| `0054_vv_records.py` | Create `tool_vv_record` + `job_vv_record` tables. |
| `0055_approval_vv_target.py` | Widen `approval_request`: add `tool_vv_record_id` + `job_vv_record_id` nullable FKs; widen `ck_approval_request_exactly_one_target` from "one of 4" to "one of 6"; add two pending-only partial unique indexes. |
| `0056_vv_curator_role.py` | Seed the `vv_curator` capability role + grant `vv.curate` to it + grant `vv.curate` onto `administrator` (frozen literals). The `vv_required_for_submit` project-metadata field seed is **runtime-deferred** (see §3.3) — the migration cannot perform that seed because `project_metadata_field.created_by_user_id` is `NOT NULL` and no users exist at migration time on a greenfield install. |

All migrations follow house style: `_BIGINT = sa.BigInteger().with_variant(sa.Integer(), "sqlite")`; CHECK bodies as **frozen literals** (never imported from the ORM); `batch_alter_table(recreate="always")` for any SQLite CHECK change; idempotent seeds (skip rows that already exist); one-way-lossy downgrade for any new target rows (`DELETE … WHERE new_fk IS NOT NULL` first), matching migrations 0046 / 0049.

### 2.0 `tool_version.bundle_hash` — the V&V cohort key

The Option C inheritance model (§6.5) is keyed on bundle-byte equality. Each `tool_version` snapshot lives at `<bundles_dir>/<alias>/<version>/` per the existing tool-versioning ORM doc; this migration adds a single new column to `tool_version`:

| col | type | notes |
|---|---|---|
| `bundle_hash` | str(64) NOT NULL | SHA-256 hex of the bundle directory contents, computed by `tools.versioning.compute_bundle_hash` (§2.0.1). server_default `''` so the migration is greenfield-safe; populated by the bump path on every new version creation |

Indexes:

- `ix_tool_version_alias_bundle_hash` — composite `(alias, bundle_hash)`. The V&V inheritance query in §5.1 hits this index directly: "give me all `tool_version.id` for this alias with this hash".

Semantic: `bundle_hash = ''` means "fingerprint not yet computed" and is treated by `summarise_tool_status` as a **singleton cohort** (no inheritance to or from). This is the safe default — empty-string rows do not contaminate any real cohort. On a greenfield install no `tool_version` rows exist at migration time, so the default never applies in practice; it exists for migration safety only.

#### 2.0.1 `compute_bundle_hash` algorithm

Lives in `matofx/tools/versioning.py` (the existing semver-bump module — bundle fingerprinting is a versioning concern). Deterministic across OS:

```python
def compute_bundle_hash(bundle_dir: Path) -> str:
    """SHA-256 hex of the bundle's contents, deterministic across OS.

    Walks the directory, sorts entries lexicographically by POSIX-style
    relative path, and hashes ``f"{rel_path}\\0{sha256(content)}\\n"`` for
    each file. Empty bundle dirs return the empty-payload hash (a fixed
    sentinel — never confused with the "unknown" empty-string value in
    ``tool_version.bundle_hash`` which is the empty *string*, not the
    empty-payload *hash*).
    """
```

Key properties:

- **OS-invariant**: relative paths normalised to forward slashes; entry order is lexicographic on the normalised string. A Windows bundle and a Linux bundle of the same content produce identical hashes.
- **Metadata-invariant**: mtime, atime, permissions ignored. Only filename + content bytes contribute.
- **Symlink-safe**: symlinks within the bundle aren't followed; the symlink target string contributes as content (cheap protection against accidental cross-bundle leakage).
- **Bounded cost**: O(total bytes); for a typical tool bundle (<10 MB) the hash takes well under 100 ms on modest hardware. Computed once at bump time, stored, never recomputed for reads.

#### 2.0.2 Bump-path integration

`matofx/tools/service.py` already calls `bump_patch` / `bump_minor` / `bump_major` and writes a new `tool_version` row. The bump path gains one new responsibility: after the new bundle directory is staged at its final path, compute and store `bundle_hash`:

```python
new_version.bundle_hash = compute_bundle_hash(new_version_bundle_dir)
```

Patch bumps copy bundle bytes verbatim from the parent — the hash matches the parent's by construction. Minor bumps may or may not match depending on what was edited. Major bumps almost always differ (the bundle was replaced). The hash itself is the source of truth; we never trust the bump class to predict inheritance.

#### 2.0.3 Exposing the hash beyond V&V

`bundle_hash` is small public infrastructure — V&V is the consumer that motivates it but it's useful elsewhere too. Out of scope for *this* design, but worth recording:

- **Compare column header** (per §16.3) could surface the hash so a reviewer sees "these two columns ran on byte-identical bundles" at a glance.
- **Per-job HTML report** (ADR-0004) could include the hash in the tool-version fingerprint block, making byte-level reproducibility a first-class audit fact.
- **Migrate bundle export** (§16.13) could verify hash equality after a cross-install import.

These are follow-on tickets, not Slice 1 work — but the column lands once and serves multiple consumers.

### 2.1 `tool_vv_record` — V&V claims about a tool version

| col | type | notes |
|---|---|---|
| `id` | PK (`_BIGINT`) | |
| `tool_version_id` | FK → `tool_version.id` RESTRICT, NOT NULL | the artefact the claim is about (a surrogate model version IS a `tool_version` per ADR-0008 — same table covers both) |
| `kind` | str(16) NOT NULL | CHECK `IN ('verification','validation')` |
| `state` | str(16) NOT NULL | CHECK `IN ('draft','pending','approved','rejected','retired')` |
| `evidence_file_version_id` | FK → `library_file_version.id` RESTRICT, nullable | the V&V evidence document, governed + immutable via the file library |
| `evidence_url` | text nullable | external link when evidence is not file-library content (a published standards doc, a customer wiki) |
| `applicable_range_json` | JSON, NOT NULL, server_default `'{}'` | `{factor_name: {min, max}}` operating envelope this claim covers; empty `{}` means unbounded |
| `confidence_band` | float nullable | the ±% the validation demonstrates (e.g. 0.02 = ±2%) |
| `standards_cited_json` | JSON, NOT NULL, server_default `'[]'` | list of free-text strings, e.g. `["ASME V&V 10","NAFEMS QSS-2023"]` |
| `notes` | text nullable | |
| `recorded_by_user_id` | FK → `user.id` RESTRICT, NOT NULL | the V&V record author |
| `recorded_at` | DateTime(tz) NOT NULL | author timestamp |
| `decided_by_user_id` | FK → `user.id` RESTRICT, nullable | populated on approve/reject |
| `decided_at` | DateTime(tz) nullable | |
| `decision_reason` | text nullable | required on reject; optional on approve |
| `version` | int NOT NULL, server_default `'0'` | optimistic lock for mutable metadata fields (notes, applicable_range_json, confidence_band, standards_cited_json, evidence_*) |

Indexes:

- `ix_tool_vv_record_tool_version_id` — list by tool version (the most common read).
- `ix_tool_vv_record_state` — filter by `pending` for the admin badge count.
- Composite `ix_tool_vv_record_tv_state_kind (tool_version_id, state, kind)` — badge computation reads "any approved verification?" + "any approved validation?" in one scan.

CHECK invariants:

- `ck_tool_vv_record_kind` — `kind IN ('verification','validation')`.
- `ck_tool_vv_record_state` — `state IN ('draft','pending','approved','rejected','retired')`.
- `ck_tool_vv_record_decided_when_terminal` — `state NOT IN ('approved','rejected') OR (decided_by_user_id IS NOT NULL AND decided_at IS NOT NULL)`.

### 2.2 `job_vv_record` — V&V claims about a single job run

| col | type | notes |
|---|---|---|
| `id` | PK (`_BIGINT`) | |
| `job_id` | FK → `job.id` CASCADE, NOT NULL | the job the claim is about (delete-job cascades V&V records — the claim has no meaning without its subject) |
| `kind` | str(24) NOT NULL | CHECK `IN ('validated_against','calibrated_from','sanity_checked')` |
| `state` | str(16) NOT NULL | CHECK `IN ('draft','pending','approved','rejected','retired')` |
| `evidence_file_version_id` | FK → `library_file_version.id` RESTRICT, nullable | evidence dataset (e.g. the rig-test result the job is being validated against) |
| `evidence_url` | text nullable | |
| `notes` | text nullable | |
| `recorded_by_user_id` | FK → `user.id` RESTRICT, NOT NULL | |
| `recorded_at` | DateTime(tz) NOT NULL | |
| `decided_by_user_id` | FK → `user.id` RESTRICT, nullable | |
| `decided_at` | DateTime(tz) nullable | |
| `decision_reason` | text nullable | |
| `version` | int NOT NULL, server_default `'0'` | optimistic lock |

Note: deliberately **no** `applicable_range_json` / `confidence_band` / `standards_cited_json` on `job_vv_record` — those are tool-claim concepts. Per-job claims attest to a single run against a single dataset.

Indexes: `ix_job_vv_record_job_id`, `ix_job_vv_record_state`.
CHECKs: `kind`, `state`, `decided_when_terminal` (same shape as §2.1).

### 2.3 `approval_request` widening (migration `0054`)

Adds two nullable FK columns to the existing approval-request table and widens the exactly-one-target CHECK. Matches the technique in migrations 0046 (library) and 0049 (materials):

```python
_TARGET_ONE_OF_SIX = (
    "(CASE WHEN job_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN parametric_study_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN library_file_version_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN material_version_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN tool_vv_record_id IS NOT NULL THEN 1 ELSE 0 END "
    "+ CASE WHEN job_vv_record_id IS NOT NULL THEN 1 ELSE 0 END) = 1"
)
```

Plus two pending-only partial unique indexes (mirror the materials/library
shape — at most one open gate per record + kind):

```python
op.create_index(
    "uq_approval_request_pending_tool_vv_kind",
    "approval_request",
    ["tool_vv_record_id", "kind"],
    unique=True,
    sqlite_where=sa.text("status = 'pending'"),
    postgresql_where=sa.text("status = 'pending'"),
)
# … same for job_vv_record_id
```

Downgrade is one-way lossy on V&V-targeted rows (`DELETE WHERE tool_vv_record_id IS NOT NULL OR job_vv_record_id IS NOT NULL` first), matching the established convention.

**Forward compat with ADR-0008.** ADR-0008's surrogate-model approval target will widen further (4 → 6 → 7). The widening migration the surrogate work ships should expect the post-V&V state as its base; CHECK strings are frozen literals so there's no coupling at code level.

### 2.4 `vv_curator` capability role + `vv.curate` permission (migration `0055`)

Idempotent seed. The capability role is built-in (cannot be renamed/deleted, per ADR-0005's invariant). Frozen literals — do NOT import the catalog:

```python
_CAPABILITY_ROLES: tuple[dict[str, object], ...] = (
    {
        "name": "vv_curator",
        "description": (
            "Capability role: curate the V&V (Verification & Validation) record "
            "ledger — promote draft V&V claims to approved on tools, jobs, and "
            "surrogate models, and stand in the directed-approver gate. Stacks on "
            "top of a base role; carries no privilege rank."
        ),
        "permissions": ("vv.curate",),
    },
)
```

The permission slug `vv.curate` follows the established `*.curate` convention (ADR-0005 §UI/enforcement distinction, ARCHITECTURE §16.21) and goes into `matofx/users/permissions.py` alongside `library.curate` / `materials.curate` (see §4 below). The migration also grants `vv.curate` onto `administrator` (mirrors the lineage-permission seed in `0052`) so an existing admin install picks up the surface without manual fiddling; `super_user` holds it implicitly via the catalog sentinel.

---

## 3. Permission catalog + general settings

### 3.1 `matofx/users/permissions.py` additions

```python
VV_CURATE = "vv.curate"
"""Curate the V&V record ledger (ADR-0011): promote draft V&V claims to
approved on tools, jobs, and surrogate models, and stand in the directed-
approver gate. Granted via the ``vv_curator`` capability role."""
```

Placed alphabetically next to `LIBRARY_CURATE` / `MATERIALS_CURATE` so a glance at the file reveals the three governance scopes.

### 3.2 General Settings catalog row

One new row in `GENERAL_SETTINGS_CATALOG` ("Data governance" section):

| key | type | default | requires_role | description |
|---|---|---|---|---|
| `vv_record_approval` | bool | `true` | `super_user` | When ON, V&V record promotion to `approved` is gated by the directed-approver mechanism (author + designated `vv_curator` minus self). When OFF, a `vv_curator` holder approves directly. |

Per the always-three-edits invariant (`feedback_general_settings_two_places`): catalog row + `SECTIONS` tuple membership + the hard-coded section count in `test_general_settings_router.py`. The new row also extends the test sweeper's expected catalog size by 1.

### 3.3 Per-project metadata field (no schema change)

New seeded row in `project_metadata_field` (the ADR-0002 metadata taxonomy):

| col | value |
|---|---|
| `key` | `vv_required_for_submit` |
| `label` | "Require V&V'd tools only" |
| `field_type` | `single_select` |
| `required` | `false` |
| `show_in_table` | `false` |
| (option values) | `"yes"`, `"no"`, `"not_set"` (three `project_metadata_value` rows) |

**Seed timing — runtime, not migration (revised 2026-05-27).** The design originally placed this seed in migration `0056` alongside the role + permission seeds. That doesn't work: `project_metadata_field.created_by_user_id` is `NOT NULL` with a RESTRICT FK to `user`, and on a greenfield install no users exist at migration time (the first one is created by `matofx admin create-first-user` *after* migrations run). The seed therefore lands at runtime via `matofx.vv.bootstrap.ensure_vv_required_for_submit_field`, called by the slice-7 gate the first time the project metadata catalog is read by the submit pipeline — idempotent + greenfield-safe. The migration itself stays purely role + permission.

Read at submit time by the gate in §6.4. Admins set it via the existing Admin → Project metadata page (no new UI).

---

## 4. ORM (`matofx/vv/orm.py`)

Mirrors `matofx/materials/orm.py` in shape:

- `ToolVVRecord` (`__tablename__ = "tool_vv_record"`).
- `JobVVRecord` (`__tablename__ = "job_vv_record"`).
- Two enums (frozen `tuple[str, ...]`, not Python `Enum` — matches the catalog-style elsewhere): `TOOL_VV_KINDS`, `JOB_VV_KINDS`, `VV_STATES`.
- Lifecycle helpers as module-level functions, not methods: `is_terminal(state)`, `can_transition(from_, to)`.

Relationships:

- `ToolVVRecord.tool_version` → `ToolVersion` (no back-populates needed yet — the consumer access pattern is "list V&V records for tool version X", served by service-layer query).
- `ToolVVRecord.evidence_file_version` → `LibraryFileVersion`, lazy.
- `JobVVRecord.job` → `Job`, lazy.
- `JobVVRecord.evidence_file_version` → `LibraryFileVersion`, lazy.

No `back_populates` on `ToolVersion` / `Job` / `LibraryFileVersion`: V&V is a downstream concern; upstream models should not import the V&V module (avoids the circular-import risk that's bitten earlier).

---

## 5. Service (`matofx/vv/service.py`)

`VVService` (async, audited). Method surface:

```python
class VVService:
    # Read (any authenticated user)
    async def list_for_tool_version(db, *, tool_version_id) -> list[ToolVVRecord]
    async def list_for_job(db, *, job_id) -> list[JobVVRecord]
    async def get_tool_record(db, *, record_id) -> ToolVVRecord
    async def get_job_record(db, *, record_id) -> JobVVRecord
    async def summarise_tool_status(db, *, tool_version_id) -> VVStatusBadge
    async def list_pending(db, *, actor_user_id) -> list[VVRecord]  # for the curator inbox

    # Author (any authenticated user — produces draft)
    async def record_tool_vv(db, *, actor_user_id, tool_version_id, kind, ...) -> ToolVVRecord
    async def record_job_vv(db, *, actor_user_id, job_id, kind, ...) -> JobVVRecord

    # Edit draft (author only, while state='draft')
    async def update_tool_record(db, *, actor_user_id, record_id, **fields) -> ToolVVRecord
    async def update_job_record(db, *, actor_user_id, record_id, **fields) -> JobVVRecord

    # Submit for review (author; flips draft → pending; creates approval_request)
    async def submit_tool_record(db, *, actor_user_id, record_id, designated_approver_user_id) -> ToolVVRecord
    async def submit_job_record(db, *, actor_user_id, record_id, designated_approver_user_id) -> JobVVRecord

    # Decide (VV_CURATE; via matofx.approvals — service forwards, never duplicates the gate)
    async def approve_tool_record(db, *, actor_user_id, record_id, reason=None) -> ToolVVRecord
    async def reject_tool_record(db, *, actor_user_id, record_id, reason) -> ToolVVRecord
    # … same for job records.

    # Retire (VV_CURATE)
    async def retire_tool_record(db, *, actor_user_id, record_id) -> ToolVVRecord
    async def retire_job_record(db, *, actor_user_id, record_id) -> JobVVRecord
```

### 5.1 `summarise_tool_status` — the badge model (inheritance-aware)

Returns:

```python
@dataclass(frozen=True, slots=True)
class VVStatusBadge:
    tool_version_id: int                                   # the version queried
    kind: Literal["verified_validated", "verified_only", "pending", "none", "retired"]
    has_approved_verification: bool
    has_approved_validation: bool
    has_pending: bool
    latest_envelope: dict[str, dict[str, float]] | None    # union over approved validations
    cohort_bundle_hash: str                                # the hash this badge is keyed on
    contributing_version_ids: tuple[int, ...]              # which version(s) the approved records live on
    inherited: bool                                        # True iff no approved record on tool_version_id itself
```

Closed-vocabulary kind so the UI mapping is exhaustive (per house style — `feedback_closed_vocabulary_invariants`). The query runs in **a single statement** thanks to the composite `(alias, bundle_hash)` index on `tool_version`:

```sql
SELECT vv.*
FROM tool_vv_record vv
JOIN tool_version tv     ON tv.id = vv.tool_version_id
JOIN tool_version target ON target.id = :target_tool_version_id
WHERE tv.alias = target.alias
  AND tv.bundle_hash = target.bundle_hash
  AND tv.bundle_hash <> ''           -- singletons do not inherit
  AND vv.state = 'approved'
```

The badge is the union over approved records across every `tool_version` sharing the target's `(alias, bundle_hash)`; envelope is the union over all approved validations' `applicable_range_json`. `contributing_version_ids` lets the UI render "Inherited from v1.0.0" tooltips when `inherited=True`. `cohort_bundle_hash` is surfaced verbatim so the per-job HTML report and Compare column header can show the fingerprint. Cached on the request (per-request memo dict bound on `request.state` — `@functools.cache` is wrong for async).

**Inheritance scope.** Per §6.5, V&V inherits across **every `tool_version` with byte-identical bundle bytes** (`(alias, bundle_hash)` is the V&V cohort). Cohort membership is determined by the hash on the row, not by version-string semantics — a 1.0.0 → 1.1.0 patch-style minor that didn't actually edit any bundle file inherits naturally because the hashes match; a 1.4.0 minor that changed a numerical scheme falls out of v1.0.0's cohort because its hash differs. `bundle_hash = ''` is treated as a singleton (no inheritance to or from); on a real install this only happens before the bump path has run for the first time. `job_vv_record` does **not** inherit — a job claim is about a specific run on a specific frozen `tool_version_id`.

### 5.2 `resolve_envelope_warning` — submit-time soft-warn

Helper (used by the submit pipeline, not exposed as an endpoint):

```python
async def resolve_envelope_warning(
    db, *, tool_version_id, form_values: dict[str, Any]
) -> EnvelopeWarning | None:
    """Return a soft warning when form values fall outside any approved
    validation's applicable_range. None when no validation, no envelope, or
    inputs fall inside every approved envelope."""
```

Returns a structured object (not a string) so the submit page can render it richly + the job report can serialise it. Never blocks — the gate is the per-project `vv_required_for_submit` policy in §6.4, which is a separate concern.

### 5.3 Audit events

```
vv.tool_record.create / update / submit / approve / reject / retire
vv.job_record.create / update / submit / approve / reject / retire
vv.envelope.warn   (at job submission, when out-of-envelope; payload: tool_version_id, factor, value, envelope)
```

Every actor including admins/super-users.

---

## 6. Approval flow

Reuses `matofx.approvals` with the V&V record FKs added to `approval_request`. The directed-approver mechanism is **already built** (shared with ADR-0006 / ADR-0007); V&V adds no new approvals code, only two new target FKs and the consumer's routing logic.

### 6.1 Toggle ON (default)

1. Author records a V&V claim (`state='draft'`); edits freely.
2. Author submits — picks **one** approver at submit from `VV_CURATE` holders **minus self**; record flips to `state='pending'`; an `approval_request` row is created with `designated_approver_user_id` set + the V&V FK populated.
3. Approve → `state='approved'`, `decided_*` populated, audit `vv.*.approve`. Reject → `state='rejected'`, prior approved record (if any) remains the authoritative one for badge computation, audit `vv.*.reject`.

### 6.2 Toggle OFF

A `VV_CURATE` holder records and approves in one step (`state='draft' → 'approved'` direct), audited as both `vv.*.create` and `vv.*.approve`. Non-curators still produce drafts.

### 6.3 Sole-curator guard

If toggle ON and exactly one `VV_CURATE` holder exists, the submit path raises `VVSoleCuratorError` with plain-English wording ("Cannot submit for review — at least one other `vv_curator` is required to act as approver, or turn off `vv_record_approval` to allow self-approval"). Mirrors the sole-super-user / sole-approver guards already in the codebase.

### 6.4 Per-project `vv_required_for_submit` gate

Read once at submit-form render + once at `JobService.queue_for_runner`:

| Setting | Tool V&V state | Outcome |
|---|---|---|
| `not_set` or `no` | any | No gate; job submits normally. |
| `yes` | tool has ≥1 approved validation **and** ≥1 approved verification | Submit allowed. |
| `yes` | tool lacks one or both | Submit **blocked**; clear message "This project requires V&V'd tools only — `tool_alias` v3.2 has no approved validation yet". |

Form-side check is advisory (cheap badge read); server-side check at `queue_for_runner` is authoritative (matches the established pattern from approvals / library / materials). The badge read used here is the inheritance-aware `summarise_tool_status` from §5.1 — a v1.4.0 tool whose only approved validation lives on v1.0.0 still passes a `vv_required_for_submit=yes` project.

### 6.5 V&V inheritance across versions

**Rule.** A V&V record on `tool_version` X applies to every `tool_version` Y where `(Y.alias, Y.bundle_hash) == (X.alias, X.bundle_hash)` and the hash is non-empty. The cohort key is byte-identical bundle contents; the version-number bump class does not enter into it.

**Why this rule (Option C, ruled 2026-05-27).** The earlier Option A (inherit across all minor + patch versions sharing the same major) was a *soft* invariant — it relied on tool authors and curators correctly mapping behaviour-changing edits to the major bump class. Option C is a *hard* invariant: the V&V cohort is defined by bundle-byte equality, computed at bump time and stored on `tool_version.bundle_hash`. Whatever the bump class, if the bundle bytes are identical to a sibling's, V&V inherits; if different, it doesn't. The badge means exactly what it appears to mean — the bytes that produce the simulation result are the bytes V&V was performed against.

This makes the inheritance rule resistant to bump-class drift. A curator who edits `static/main_model.py` through the in-UI editor (today a minor bump) and changes a numerical tolerance gets a different hash → different cohort → V&V correctly does not inherit. A curator who edits only a comment in the same file gets the same hash → same cohort → V&V inherits naturally. Either way the answer is computed from facts, not from compliance with a procedural rule.

**Where the rule lives in code.** `VVService.summarise_tool_status` (§5.1) is the single read-side enforcer; every consumer (badge, submit-form gate, server-side gate at `queue_for_runner`, Compare column header, lineage badge, KPI filter) reads through it. No caller computes V&V status from raw `tool_vv_record` rows. The hash itself is computed once at bump time by `tools.versioning.compute_bundle_hash` (§2.0.1) — never recomputed for reads.

**Inheritance is transparent in the UI.** The badge component renders an "Inherited from v1.0.0" caption when `VVStatusBadge.inherited=True`, and the V&V record listing on `/admin/tools/{id}` (per-tool V&V tab, §8 slice 10) lists records across every version sharing the current `bundle_hash` — each row labelled with its origin version. The badge tooltip exposes the eight-char hash prefix so a curious operator can confirm "yes, this is the same bundle". The curator never has to chase versions to understand why a badge is green.

**What happens at version boundaries:**

| Edit | Old hash | New hash | V&V inherits? |
|---|---|---|---|
| Patch — `description` rewrite | `abc123…` | `abc123…` (unchanged — no bundle file touched) | ✅ yes |
| Minor — comment-only `static/main_model.py` edit | `abc123…` | `abc123…` (byte-identical content) | ✅ yes |
| Minor — `static/main_model.py` tolerance change | `abc123…` | `def456…` (bytes differ) | ❌ no — fresh cohort |
| Major — bundle replacement | `abc123…` | `xyz789…` (new bundle) | ❌ no — fresh cohort |
| Major — bundle re-upload of identical bytes (rare) | `abc123…` | `abc123…` (identical) | ✅ yes — cohort survives |

The last row is a deliberate property: if a curator re-uploads the same bundle bytes under a new major (perhaps a contractual repackaging), V&V continues to apply. This is *correct* — the simulation behaviour is unchanged. A skeptical curator can always retire the V&V record manually if they don't want the inheritance.

**Curator escape hatch.** If a curator judges that an inherited V&V no longer holds (e.g. they've reviewed new validation evidence and concluded the old envelope is too generous):

1. Open the inherited record on the version it lives on (e.g. v1.0.0).
2. Click *Retire* — the record's `state` flips to `retired`, removing it from `summarise_tool_status`'s contribution for **every** version in the cohort.
3. (Optionally) record a fresh V&V claim against the current version with the new evidence.

A future affordance ("retire only for versions ≥ X.Y.Z") is a v1.x consideration; v1.0 retirement is cohort-wide because that matches the inherit-cohort-wide semantics symmetrically. Bundle-hash inheritance makes per-version retire substantially less necessary — most real cases where you'd want it correspond to a bundle change, which already splits the cohort automatically.

**Audit.** V&V badge derivation is a read-side computation, not a write event — there's no `vv.inherited` audit row per badge fetch (would flood the log). Inheritance provenance lives in `VVStatusBadge.contributing_version_ids` and `cohort_bundle_hash`; the per-job HTML report (ADR-0004) serialises both so a compliance reader can see, from the static report alone, which version's V&V record covered the job and what bundle fingerprint defined the cohort.

---

## 7. API surface

Feature-gated router (absent when `Feature.VV_TRACKING` off, mirrors the materials / library gating). `*Error` → plain-English HTTP per house style.

```
# Reads
GET    /api/vv/tool-records?tool_version_id=&state=        list (filterable)
GET    /api/vv/tool-records/{id}                           detail + evidence file metadata
GET    /api/vv/job-records?job_id=&state=                  list
GET    /api/vv/job-records/{id}                            detail
GET    /api/vv/status/tool/{tool_version_id}               VVStatusBadge JSON
GET    /api/vv/pending                                     curator inbox (VV_CURATE)
GET    /api/vv/curators                                    eligible approver picker

# Authoring (any user)
POST   /api/vv/tool-records                                create draft
PATCH  /api/vv/tool-records/{id}                           edit draft (optimistic lock)
POST   /api/vv/tool-records/{id}/submit                    flip draft → pending + create approval_request
POST   /api/vv/job-records                                 create draft
PATCH  /api/vv/job-records/{id}                            edit draft
POST   /api/vv/job-records/{id}/submit                     flip draft → pending

# Decision (VV_CURATE) — forwards through matofx.approvals
POST   /api/vv/tool-records/{id}/approve                   reason optional
POST   /api/vv/tool-records/{id}/reject                    reason required
POST   /api/vv/job-records/{id}/approve
POST   /api/vv/job-records/{id}/reject
POST   /api/vv/tool-records/{id}/retire                    VV_CURATE; soft archive
POST   /api/vv/job-records/{id}/retire
```

Approve / reject also work through the existing `/api/approvals/{id}/decide` for the unified curator inbox — both paths converge on the same service call.

---

## 8. UI surfaces

| Surface | File | Change |
|---|---|---|
| Tool record CRUD | `ui/pages/vv_records.py` (new) | Browse/filter by tool / state / kind; record detail with state history; create/edit forms; approver picker; retire button (curator). Two tab modes: Tool records / Job records. |
| Admin index | `ui/pages/admin_vv_records.py` (new) | `/admin/vv-records` — list + filter + bulk-retire; aligns with the materials/library admin pattern. |
| Admin → Tools (existing) | `ui/pages/admin_tools.py` | Add a V&V badge column to the listing; per-row click expands to that tool version's V&V records. |
| Submit form | `ui/pages/my_jobs.py` | V&V badge in form header (clickable → record detail in popover); per-project policy gate; envelope soft-warn banner. |
| Compare | `ui/pages/compare.py` (already shipped) | Add V&V badge to each column header (single-line read; the badge is in the per-column metadata payload). |
| Lineage | `ui/_cytoscape.py` + `matofx/lineage/resolvers.py` | New `NodeKind.VV_RECORD` + new `EdgeKind.VERIFIED_BY` / `EdgeKind.VALIDATED_BY` (closed vocab extension); resolver composes `VVService.list_for_tool_version` / `list_for_job`. |
| KPI table | `ui/pages/results_tool.py` (per ADR-0012) | "V&V status" facet in the filter rail. |
| Model registry | per ADR-0008 | Surrogate version card shows V&V badge + record links. |
| Job detail | `ui/pages/job.py` | New right-pane "V&V" card listing this job's records + a "Record V&V" action button. |
| Tool detail | `ui/pages/admin_tool_edit.py` | New "V&V" tab on the per-tool admin page. |
| Curator inbox | existing approvals UI | Pending V&V records appear in the unified inbox alongside library/material/job/study approvals. |
| Nav | `ui/_chrome.py` | Feature-gated "V&V records" entry under the user-facing nav + the admin-side entry. |

UI gating uses the resolved permission so curate-only controls are **hidden** when the user lacks `vv.curate`, not just server-rejected (per the ADR-0005 UI/enforcement-distinction pattern).

---

## 9. Audit

Mutation events listed in §5.3 plus the badge-driven derivations:

- `vv.tool_record.create | update | submit | approve | reject | retire`
- `vv.job_record.create | update | submit | approve | reject | retire`
- `vv.envelope.warn` (per-job, on out-of-envelope submission)
- `vv.policy.block` (per-job, when `vv_required_for_submit=yes` blocks)

Every actor logged (admins / super-users included).

---

## 10. Cross-ADR integration touchpoints

| ADR | Touchpoint | What V&V owns | What the other ADR owns |
|---|---|---|---|
| ADR-0006 (File library) | Evidence storage | Adds `vv_evidence` to the seeded tag list; references `library_file_version_id` | No library-side code change beyond the seed |
| ADR-0007 (Materials) | None (independent) | — | — |
| ADR-0008 (Surrogate) | Surrogate V&V uses `tool_vv_record` (surrogate version IS a tool version) | Nothing new on the V&V side — FK already covers it | Adds the surrogate-model-version target to `approval_request` (4→6→7); shows V&V badge on the model registry card |
| ADR-0009 (Optimisation) | Per-project policy gate inherits | Provides the gate at study-config time via the same `vv_required_for_submit` check | Calls `VVService.summarise_tool_status` during study setup |
| ADR-0010 (Lineage) | V&V records become graph nodes | Provides the resolver + envelope payload | Extends `NodeKind` (`VV_RECORD`) + `EdgeKind` (`VERIFIED_BY`, `VALIDATED_BY`); registers the resolver |
| ADR-0012 (KPI extraction) | KPI table filter | Provides `summarise_tool_status` query | Adds the filter widget + query composition |

The two **ADR-0010 enum additions** are the only changes that require coordinated edit across modules — V&V's design lands the resolver code, and `matofx/lineage/kinds.py` gains the two new enum members. Per the lineage design's "closed vocabulary" invariant, the additions need a passing exhaustiveness test in the lineage suite.

---

## 11. Phased build sequence

Maps ADR-0011's six Build Phases onto concrete slices. Each slice is its own commit + green tests. Gates between slices: `ruff format --check . && ruff check . && mypy && pytest`, both DB engines.

| Slice | Phase | Deliverable | Acceptance |
|---|---|---|---|
| **0** ✅ | 1 | `tools/versioning.py` gains `compute_bundle_hash`; migration `0053_tool_version_bundle_hash.py` adds the column + composite index; `tools/service.py` bump path writes the hash on every new version | **Shipped 2026-05-27.** 14/17 tests passing (3 skipped = POSIX-only chmod/symlink). |
| **1** ✅ | 1 | `matofx/vv/__init__.py` + `orm.py` + `errors.py` + `README.md`; migration `0054_vv_records.py` | **Shipped 2026-05-27.** 28 lifecycle-helper + 17 ORM tests passing. |
| **2** ✅ | 1 | Migration `0055_approval_vv_target.py` (+ `approvals/orm.py` + `service.py` extended to accept the two new V&V target kwargs) | **Shipped 2026-05-27.** 11 schema/CHECK tests passing. |
| **3** ✅ | 1 | `matofx/users/permissions.py` adds `VV_CURATE`; migration `0056_vv_curator_role.py` seeds capability role + `vv.curate` grant on `vv_curator` and on `administrator`. (The `vv_required_for_submit` project-metadata-field seed is runtime-deferred per §3.3 and lands with slice 7/8.) | **Shipped 2026-05-27.** 5 seed tests passing. |
| **4** ✅ | 1 | `matofx/vv/service.py` — full CRUD + state transitions + `summarise_tool_status` (bundle-hash inheritance per §5.1 + §6.5) + audit | **Shipped 2026-05-27.** 22 service tests passing including the cohort-inheritance invariant. **Deviation flagged in module docstring:** authoring requires curate too (design said "any user records draft"). |
| **5** ✅ | 2 | API router `matofx/api/routers/vv.py` + schemas + error mapping + feature gate | **Shipped 2026-05-27.** 16 endpoints, 8 router tests + 1 envelope-check test (added during slice 7). `VV_TRACKING` feature flag added. |
| **6** ✅ | 2 | Approval routing extension — `api/routers/approvals.py` `decide` path widens to V&V targets; curator inbox query covers both V&V record kinds | **Shipped 2026-05-27.** `VVService.decide_tool_approval` + `decide_job_approval`; approvals router branches on the two V&V columns; `target_kind` Literal widened to include `vv_tool` + `vv_job`. 3 integration tests passing. |
| **7** 🟡 | 3 | Submit-form V&V badge in `ui/pages/my_jobs.py` + per-project `vv_required_for_submit` gate + envelope soft-warn banner | **Backend shipped 2026-05-27.** `POST /api/vv/envelope-check` endpoint live; `VVStatusBadge` exposed via `GET /api/vv/status/tool/{id}`. **UI in `my_jobs.py` not yet wired** — picks this slice up when the NiceGUI work in slice 9 begins. |
| **8** ✅ | 3 | `JobService.queue_for_runner` server-side V&V policy gate + `vv_required_for_submit` field bootstrap | **Shipped 2026-05-27.** `matofx/vv/policy.py` + `bootstrap.py` modules; hook in `queue_for_runner` between tool-version validation and storage staging. 9 policy/bootstrap tests passing; 15 hierarchy regression tests passing (gate hook doesn't break the happy path). |
| **9** ⬜ | 4 | `ui/pages/vv_records.py` (user-facing CRUD) + `ui/pages/admin_vv_records.py` (admin index) | Not started. Substantial NiceGUI work — sized similar to materials catalog page. |
| **10** ⬜ | 4 | Per-tool V&V tab on `ui/pages/admin_tool_edit.py` + per-job V&V card on `ui/pages/job.py` | Not started. |
| **11** ⬜ | 5 | Compare column-header badge (in `ui/pages/compare.py`'s per-column metadata payload) | Not started. |
| **12** ⬜ | 5 | Lineage integration: extend `NodeKind` + `EdgeKind`; register V&V resolver in `matofx/lineage/resolvers.py`; node renderer in `_cytoscape` | Not started. Coordinated edit across `matofx/lineage/` — additive enum extension + new resolver. |
| **13** ⬜ | 5 | KPI filter integration (per ADR-0012) — guarded; lands when KPI work begins | Deferred until ADR-0012 work is in flight; this slice is a "ready to integrate" marker, not a build step. |
| **14** ⬜ | 6 | Docs: `matofx/vv/README.md` package overview; `ARCHITECTURE.md` §16.24 (V&V tracking); `ROADMAP.md` tick (Landed table); SPDM-scope-deferral row flip (V&V tracking → Resolved for v1) | Package README already shipped in slice 1. ARCHITECTURE §16.24 + ROADMAP tick + SPDM-table flip are the remaining doc work. |

**Surrogate (ADR-0008) coupling.** Slice 12's lineage integration assumes the surrogate work has not yet landed; if it has, the design extends `tool_vv_record`'s consumers (the surrogate-model-version card in the registry) and the `approval_request` widening should already be at 4→6→7. Re-baseline the migration numbers if the surrogate work has bumped the head past `0055`.

---

## 12. Test plan

Per house style — fast tests run first; one slow integration test per major slice.

**ORM + migration (slices 1–3):**

- Round-trip create / read / state transition on both tables; both DB engines.
- FK constraint: a `tool_vv_record` cannot reference a non-existent `tool_version`; deleting the tool version raises (RESTRICT).
- FK constraint: a `job_vv_record` cascades on job delete.
- CHECK: `kind`, `state`, `decided_when_terminal` reject invalid inputs.
- Partial unique index: two pending gates for the same `(tool_vv_record_id, kind)` rejected; rejected/approved+pending is OK.
- Optimistic lock: stale-write raises.
- Migration idempotency: re-running `0055` does not duplicate `vv_curator` or its permission grant.
- Migration downgrade: `0054` downgrade deletes V&V-targeted rows then reverts CHECK without error.

**Service (slice 4):**

- Author draft → edit → submit → approve happy path; audit events match.
- Non-author edit-draft rejected (`VVPermissionError`).
- Non-curator approve rejected.
- Self-approval rejected (toggle ON).
- Sole-curator-and-toggle-ON guard.
- Toggle OFF: curator records + approves direct (one event chain, no `approval_request`).
- Reject sets `state='rejected'` + requires reason; prior approved record (if any) still authoritative for `summarise_tool_status`.
- Retire is state-only (no hard delete).
- `summarise_tool_status` runs in **one query** (asserted via SQLAlchemy event listener counting statements).
- `resolve_envelope_warning` returns `None` when no validation / no envelope / inside envelope; returns structured warning when outside.
- **Inheritance (§5.1 + §6.5)**: a V&V record approved on `tool_version` X with `bundle_hash='abc123…'` is reflected in `summarise_tool_status(tool_version_id=Y)` when Y shares the same `(alias, bundle_hash)`, with `inherited=True`, `contributing_version_ids=(X,)`, `cohort_bundle_hash='abc123…'`; a record on a version with a different hash does NOT contribute; `bundle_hash=''` is treated as a singleton (no inheritance to or from); retiring X removes it from Y's badge in the same request; envelope is the union over all approved validations in the cohort.

**Bundle hash (slice 0):**

- `compute_bundle_hash` is deterministic across two equivalent directories on the same OS.
- Same content laid out with Windows backslashes vs POSIX forward slashes produces the same hash.
- File mtime change does not affect the hash; permission change does not affect the hash.
- Sort-order invariant: shuffling file-creation order in a fixture bundle does not affect the hash.
- Empty directory produces the fixed empty-payload sentinel; that sentinel is distinct from the `''` "not yet computed" marker.
- Symlink to a file inside the bundle hashes the symlink target string, not the followed content.
- Bump path: `bump_patch` produces the same hash as the parent (no bundle file touched); `bump_minor` and `bump_major` produce a hash matching the actually-staged bundle bytes.

**API (slices 5–6):**

- Every endpoint round-trips; auth enforced; `*Error` mapped to documented HTTP status.
- Decision via `/api/approvals/{id}/decide` flips V&V record state identically to `/api/vv/.../approve`.
- Curator inbox query covers both V&V kinds + interleaves with library / material / job / study correctly.

**Submit-form integration (slices 7–8):**

- Form badge renders for each `kind` value; click expands.
- Out-of-envelope form value produces banner, does not block submit.
- `vv_required_for_submit=yes` blocks form-side **and** server-side; `no` / `not_set` allows.
- `vv.policy.block` audited on server-side block.

**UI smoke (slices 9–11):**

- Each page renders without error for a logged-in user; curate-only controls hidden when user lacks `vv.curate`; visible when user has it.
- Compare column V&V badge does not produce N+1 queries (asserted).

**Lineage (slice 12):**

- Closed-vocabulary exhaustiveness test passes after enum extension.
- Lineage centred on a tool version with V&V records shows the right nodes + edges.
- V&V node visibility intersects with the resolver's ACL (a record on a tool the user cannot see is filtered out).

---

## 13. Open / deferred (recorded, not silent)

- **Workflow-level V&V records** — a workflow's V&V is derived from member tools' V&V; v1.x may add a composition-level record. v1.0: derivation is read-only in the UI ("Workflow V&V" badge = AND over member badges).
- **N-of-M V&V** — same v1.x deferral as for ordinary approvals (uses the same approvals infrastructure).
- **Automated regression-test execution as the V&V record** — a record cites evidence; it does not run the evidence. Tool authors ship `tools/<alias>/test/` separately; record may cite test results.
- **External V&V exchange formats** — XML/JSON import from PLM/SPDM systems. v1.x.
- **V&V as a separate approval gate on jobs** — out of scope; conflates with the existing per-job approvals flow.
- **Per-version retire** (retire an inherited V&V record for versions ≥ X.Y.Z while keeping it live for earlier versions in the same cohort) — out for v1.0. Retire is cohort-wide, matching inherit's cohort-wide semantics. The Option C bundle-hash inheritance (§6.5) makes this far less necessary: most cases where a curator would want per-version retire correspond to a bundle change, which already splits the cohort. v1.x if a customer drives it for a residual case.
- **Cross-cohort merge** (declare "this 2.0.0 bundle reuses 1.x's validation evidence even though the bytes differ") — out for v1.0. A "carry forward with reason" affordance would let a curator manually attach an existing V&V record to a new cohort. Not a v1.0 ask; v1.x if a customer drives it.
- **Envelope multi-dimensional warnings** — v1.0 warns per-factor independently. Joint-envelope (a polytope rather than a per-factor min/max) is v1.x; first customer who has e.g. coupled temperature-pressure validation envelopes drives it.
- **Class-level V&V** (e.g. "all `cad_upload` family tools are verified") — not designed; v1.x if needed.

---

## 14. Risk + mitigation

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Bundle-hash compute mis-implementation (non-deterministic across OS, sensitive to mtime, etc.) silently breaks inheritance | Low | High | `compute_bundle_hash` is small, pure, and isolated in `tools.versioning` (§2.0.1) — straightforward to test exhaustively. Test plan §12 includes: round-trip on a fixture bundle; same content with Linux vs Windows path separators produces the same hash; mtime change does not change the hash; symlink contributes the target string not the target file; empty directory produces the fixed empty-payload sentinel. SHA-256 collision risk is negligible (this is not an adversarial context) |
| Bundle-hash backfill on a real install missed some `tool_version` rows | Low | Low | Greenfield install — no production data to backfill. Empty-string default is the safe singleton-cohort fallback; any row that ends up with `''` simply doesn't participate in inheritance. A `matofx admin recompute-bundle-hashes` CLI is a v1.x affordance if a customer needs it (trivial to add — walk every tool_version, recompute, update) |
| ADR-0008 surrogate work lands first and bumps migration head past `0055` | Medium | Low | Migration numbers are sequential; V&V chains off whatever the head is at the time. The `_TARGET_ONE_OF_SIX` literal becomes `_TARGET_ONE_OF_SEVEN` in that order |
| Lineage `NodeKind`/`EdgeKind` extension breaks the closed-vocab exhaustiveness test in a coupled module | Low | Medium | Extension is **additive**; existing match-statements over the enums need a new case added but the test surface flags exactly where. Slice 12 includes the exhaustiveness fixup |
| Sole-curator install (one `vv_curator` holder + toggle ON) blocks all V&V work | Medium | High | Sole-curator guard with plain-English error message + a docs callout in the V&V chapter; toggle defaults to ON but operator can flip when bootstrapping |
| Envelope soft-warn fires too often and operators learn to ignore it | Medium | Low | Warn only when **outside** the approved envelope, not "approaching"; never warn when no validation exists. v1.x: per-project mute |
| File-library evidence file retired while a V&V record references it | Low | Low | `library_file_version_id` FK is RESTRICT; the library retire path raises a clear error pointing to the referencing V&V records (mirrors how materials block tool-deletion on use) |

---

## 15. Out-of-band notes

- **Wording.** Use "approved" not "published" for V&V state (per ADR), even though the file library uses "published". Reason: V&V is a *claim* (the record IS approved or not); a library file is a *deliverable* (the file IS published or not). Different mental models, different verbs.
- **Surrogate special case.** When a `tool_vv_record` references a surrogate model version (which IS a `tool_version`), the badge UI may want to label `kind='validation'` as "Validation vs source data" rather than just "Validation" — handled in the UI layer's badge label resolver, not the data model. Defer until the surrogate work lands.
- **"V&V" vs "VV".** The package + permission slug uses `vv` (lowercase, no ampersand — URL/identifier-safe). Human-readable surfaces (UI labels, audit messages) use "V&V". The dataclass / class names use `VV` (mixed-case identifier).
- **Bump-policy independence.** §6.5's bundle-hash inheritance rule is *independent* of the bump-policy contract in `tools/versioning.py`. The bump class (patch / minor / major) drives version-string semantics and the change-log narrative; the hash drives V&V cohort membership. They don't need to agree — and the doc deliberately doesn't try to align them, because the hash is the harder invariant. A future tightening of the bump policy (e.g. "in-UI file edits that change the hash should be major bumps") would be a `versioning.py` change orthogonal to this design.
