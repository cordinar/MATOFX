# matofx.vv

V&V (Verification & Validation) tracking — ADR-0011 / `designs/vv-tracking.md`.

A governed ledger of who verified what, against what evidence, and when. The
auditable record regulated industries (defence, aerospace, pharma, automotive,
medical devices) need to ship simulation results into production.

## Why it exists

Procurement at a regulated customer asks on day one: *"Show me the V&V record
for `heat_transfer` v3.2."* Today the answer is "we don't track that" — a
deal-killer for production deployment in regulated environments. This module
closes the gap.

## How V&V inheritance works

A V&V record on a tool version applies to **every tool version sharing the same
`(alias, bundle_hash)`**. The hash lives on `tool_version.bundle_hash` (added in
slice 0 by migration `0053`) and is computed at version-bump time by
`matofx.tools.versioning.compute_bundle_hash` — a deterministic, OS-invariant
SHA-256 over the bundle directory's contents.

Whatever the bump class (patch / minor / major), if bundle bytes are identical
to a sibling's, V&V inherits. If different, it doesn't. The badge means
exactly what it appears to mean — the bytes that produce the simulation result
are the bytes V&V was performed against.

`job_vv_record` does **not** inherit — a job claim is about a specific run on
a specific frozen tool version.

## What's in this package

| Module | Contents |
|---|---|
| `orm.py` | `ToolVVRecord`, `JobVVRecord`, closed-vocab `TOOL_VV_KINDS` / `JOB_VV_KINDS` / `VV_STATES`, state-transition helpers (`is_terminal`, `can_transition`) |
| `errors.py` | `VVError` umbrella + per-failure-mode subclasses; all user-facing messages |
| `service.py` | *(slice 4)* `VVService` — CRUD + state machine + bundle-hash inheritance query (`summarise_tool_status`) + audit |
| `README.md` | This file |

## What's NOT in this package

- **Approvals.** V&V records use the existing `matofx.approvals` mechanism —
  submit produces an `approval_request` row, decide forwards through the
  standard endpoint. `vv_curator` is a capability role (ADR-0005), not a
  V&V-specific construct.
- **Evidence storage.** A V&V record references a `LibraryFileVersion` (ADR-0006);
  it does not carry bytes itself.
- **The cohort key.** `tool_version.bundle_hash` lives on the tool version
  itself (slice 0); this module reads it but does not own it.
- **The per-project policy gate.** `vv_required_for_submit` is a
  project-metadata field (ADR-0002 taxonomy); seeded in slice 3 / migration
  `0056`. Enforcement lives in the submit pipeline, not here.

## Lifecycle

```
draft ──submit──▶ pending ──approve──▶ approved ──retire──▶ retired
  │                  │                                          ▲
  │                  └──reject──▶ rejected ─────retire──────────┘
  │                  ▲
  └────recall────────┘   (pending → draft, author only, before decision)
  │
  └────retire────────────▶ retired
```

`approved` and `rejected` carry `decided_by_user_id` + `decided_at` (enforced
by `ck_*_decided_when_terminal`). `retired` is terminal; reversal is by
re-recording, not by state-flipping.

## Cross-ADR touchpoints

| ADR | Touchpoint |
|---|---|
| ADR-0005 (multi-role) | `vv_curator` capability role + `vv.curate` permission |
| ADR-0006 (file library) | Evidence storage; `vv_evidence` seeded tag |
| ADR-0008 (surrogate) | Surrogate model versions ARE tool versions, so their V&V uses `tool_vv_record` natively |
| ADR-0009 (optimisation) | Studies inherit the per-project V&V policy at config time |
| ADR-0010 (lineage) | V&V records become graph nodes with `VERIFIED_BY` / `VALIDATED_BY` edges |
| ADR-0012 (KPI) | KPI table filters by V&V status |
