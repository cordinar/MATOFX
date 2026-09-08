# 0011. V&V (Verification & Validation) tracking

Date: 2026-05-26
Status: Accepted (architect ruling 2026-05-26 — full v1.0 scope per "Phase 2 expansion" conversation; pilot date re-baselined accordingly)

## Context

Regulated industries — defence, aerospace, pharma, automotive, medical
devices, civil-engineering safety analysis — operate under standards
(ASME V&V, NAFEMS QSS, FDA 21 CFR Part 11, DO-178C, ISO 26262, IEC 62304)
that mandate **auditable records** of how each simulation tool has been
*verified* (the math is right) and *validated* (the tool agrees with
physical reality on cases representative of its intended use).

MATOFX today records nothing about V&V. A tool either exists in the
catalogue or it doesn't; the only "trust signal" is the tool's lifecycle
state (`new` / `test` / `released` / `archived`) which is a deployment
maturity gate, not a V&V claim. Procurement at a regulated customer will
ask, on day one:

- "Show me the V&V record for tool `heat_transfer` version 3.2."
- "Show me every job run on a tool that wasn't validated for its operating
  range."
- "Show me the validation test data that demonstrates `material_properties`
  agrees with Mil-Handbook-5 within ±2%."

Today the answer is "we don't track that". Most customers tolerate this for
an internal-tool POC; none tolerate it for a production deployment in a
regulated environment. The V&V capability is a **gate**, not a polish item.

The ADR-0008 (surrogate family) work makes the gap worse, not better: a
surrogate model is by definition an *approximation*; consuming its output
in a workflow without a V&V record creates a traceability hole even more
visible to compliance auditors than the missing tool-level record.

The customer is the sole maintainer ([[user_role]]) and has ruled this ADR
into v1.0 at full scope as part of the 2026-05-26 Phase 2 expansion.

## Decision

Build a **V&V tracking layer** that records, governs, and surfaces V&V
status across **tool versions**, **individual jobs**, and **surrogate
model versions** (the three artefact kinds where a V&V claim is
meaningful). Module `matofx/vv/`. The build-level spec lands as
`designs/vv-tracking.md`; this ADR records the decisions and rationale.

**1. The data model.** Two new tables; one in support of each granularity:

- **`tool_vv_record`** — V&V claims about a tool version. Columns: `id`
  PK · `tool_version_id` FK (NOT NULL) · `kind ∈ {verification, validation}`
  CHECK · `state ∈ {draft, pending, approved, rejected, retired}` CHECK ·
  `recorded_by_user_id` FK · `recorded_at` · `evidence_file_version_id`
  FK → `library_file_version` (optional — the V&V evidence document lives
  in the file library) · `evidence_url` (optional — an external link, if
  the evidence isn't a file we own) · `applicable_range_json` (factor →
  `{min, max}` map, the operating envelope this V&V claim covers) ·
  `confidence_band` (numeric, optional — the ±% the validation
  demonstrates) · `standards_cited_json` (list of strings — "ASME V&V 10",
  "NAFEMS QSS-2023", etc.) · `notes` (TEXT) · `decided_by_user_id`,
  `decided_at`, `decision_reason` (for the approval gate).

- **`job_vv_record`** — V&V claims about a single job (e.g. "this run was
  validated against test data X"). Columns: `id` PK · `job_id` FK
  (NOT NULL) · `kind ∈ {validated_against, calibrated_from, sanity_checked}`
  · `state ∈ {draft, pending, approved, rejected, retired}` · same
  `evidence_*`, `recorded_*`, `decided_*` columns as above.

The `state` machine mirrors `library_file_version`'s publish flow exactly
(`draft → pending → approved | rejected`, plus `retired` as a soft archive)
— so the existing `matofx.approvals` machinery handles the approval gate
with one new target kind.

**Surrogate-model V&V** reuses `tool_vv_record` — a published surrogate
model IS a `tool_version` (per ADR-0008's surrogate-as-tool synthesis), so
its V&V records live in the same table. No third table needed.

**2. Permission model — `vv_curator` capability role.** Mirrors
`library_curator` and `materials_curator` (ADR-0005). A new
`VV_CURATE` permission (`vv.curate` slug), granted via the `vv_curator`
capability role. Anyone can **record** V&V (a draft record); promoting
`draft → pending → approved` requires `VV_CURATE`. Toggleable General
Setting `vv_record_approval` (default **ON**) gates approval through the
existing directed-approver mechanism (no self-approval; minimum two
`vv_curator` holders for a real gate). When OFF, a curator approves
directly. Mirrors ADR-0006 / ADR-0007 patterns exactly.

**3. The `approval_request` widens to a sixth target kind.** Adds
`tool_vv_record_id` AND `job_vv_record_id` as two new nullable FKs (one of
the now-six-of-seven target kinds — job, study, library_file_version,
material_version, surrogate_model_version, tool_vv_record, job_vv_record —
must be non-null per the CHECK). The approval-router slice-8a pattern
(ADR-0006 §16.19) extends to cover V&V approvals; eligibility = the
designated `vv_curator`; same admin override.

**4. V&V status surfaces — visible everywhere a tool is referenced.**

- **Admin → Tools** — the tool listing gains a V&V column with status
  badges:
  - 🟢 **Verified + Validated** — at least one approved record of each
    kind, applicable-range covers a sensible envelope.
  - 🟡 **Verified only** — verification present, no validation.
  - 🟠 **Pending** — at least one V&V record in pending state, no approved
    records yet.
  - 🔴 **No V&V** — no records, only the bare lifecycle state.
  - ⚫ **Retired V&V** — V&V records exist but have been retired (e.g.
    superseded by a newer tool version's records).
- **Submit form** — the tool name in the form header carries the V&V
  badge; clicking expands to the full V&V record details. For a tool with
  **No V&V** in a context where the operator has set
  `vv_required_for_submit` (a per-project metadata field — see ADR-0002 §
  metadata taxonomy), the submit button is disabled with the message "This
  project requires V&V'd tools only".
- **Compare** — the per-column header (§16.3) shows the V&V badge for
  each tool version in the comparison, so a reviewer sees at a glance which
  jobs ran on V&V'd tools.
- **Lineage (ADR-0010)** — V&V record nodes appear in the graph beside the
  tool / job they're bound to.
- **KPI extraction (ADR-0012)** — the per-tool KPI table filters by V&V
  status, so a quality manager can "show me all production KPIs from
  V&V'd-only runs".
- **Model registry (ADR-0008)** — every surrogate model version surfaces
  its V&V status alongside the fit-diagnostics card. Surrogates are
  approximations; running an un-V&V'd surrogate in a workflow that has a
  V&V'd-only project policy fails submission with a clear message.

**5. V&V evidence storage — reuses the file library.** A V&V record's
evidence document (test report, hand-calculation, validation dataset, peer
review) is uploaded into the **file library** with the `vv_evidence` tag
applied (descriptor-driven) and the `tool_vv_record` / `job_vv_record`
references that file's `library_file_version_id`. This means:

- Evidence is immutable + content-hashed (ADR-0006).
- Evidence is governed + scoped (project-private or global).
- Cross-record reuse: the same validation dataset can underpin many V&V
  records.
- Lineage (ADR-0010) shows the evidence document as an attached node.
- No new storage primitive needed.

The `evidence_url` column is for the case where the evidence is genuinely
external (a published standards document, a customer's internal Wiki page);
it's a plain TEXT URL, the user accepts external dependency.

**6. Per-project V&V policy.** New per-project metadata field
(`vv_required_for_submit` ∈ {`yes`, `no`, `not_set`}) via the existing
metadata taxonomy (ADR-0002). When `yes`, the submit-form gate fires;
when `no` or `not_set`, no gate (so a R&D project can run un-V&V'd tools
freely while a production-validation project enforces the rule). Default
on a freshly created project: `not_set`. Admins set the project policy
under the existing project metadata UI.

**7. Tool-version → V&V applicability.** A V&V record's
`applicable_range_json` declares the operating envelope (`{factor_name:
{min, max}}`) the validation covers. Every job submission against a
V&V'd tool **soft-warns** if the submitted inputs fall outside the
validated envelope ("This run is at 800 K — the validation envelope is
300–600 K; results may not be reliable"). Warning, never blocking — but
captured in the job's metadata for the lineage trace and the report.

This requires zero per-tool wiring — the envelope is captured in
`applicable_range_json`; the submit-time check is a single comparison
against the form values.

**8. Out-of-scope for v1.0** (no-add-back lock):

- **Workflow-level V&V records.** A workflow is composed of tools; if each
  tool is V&V'd, the workflow's V&V is **derived**, not authored. v1.x may
  add a "workflow V&V" record for the composition itself (e.g. "the
  coupled CFD→FEA path has been validated against test X"); v1.0 limits
  records to tools and jobs.
- **V&V status as an additional approval target on jobs.** v1.0 doesn't
  *require* V&V'd tools to pass a separate approval gate beyond the
  existing approvals system; that conflates two separate flows.
- **Automated regression-test execution as part of a V&V record.** A V&V
  record cites evidence (a file); it doesn't *run* the evidence. Tool
  authors should ship a verification test suite separately
  (`tools/<alias>/test/`); the V&V record may *cite* the test results.
- **N-of-M V&V** (e.g. "two independent validators must approve"). The
  existing directed-approver model is one-named-approver-or-admin; N-of-M
  is the same v1.x deferral as for ordinary approvals.
- **External V&V exchange formats** — importing V&V claims from other
  PLM / SPDM systems via standardised XML / JSON. v1.x once the v1.0
  primitive is mature.

**9. Cross-cutting impacts.**

- **ADR-0006 / ADR-0007** — the file library is reused for evidence
  storage; no schema change to the file library itself. The existing
  `vv_evidence` tag is added to the seeded tag list.
- **ADR-0008** — surrogate model versions are V&V'd via `tool_vv_record`
  (they ARE tool versions); no new wiring needed.
- **ADR-0009** — optimisation studies inherit the same per-project policy;
  a study against a un-V&V'd tool in a V&V-required project fails
  configuration with a clear message.
- **ADR-0010** — V&V records become nodes; the `validated_by` /
  `verified_by` edges land per §1.
- **ADR-0012** — V&V status filters into the KPI extraction table.

## Consequences

- **Two new tables** plus the `approval_request` widening (6→7 target
  kinds; CHECK + the partial unique index extend).
- **One new capability role** (`vv_curator`) + one permission
  (`VV_CURATE`). Additive per ADR-0005.
- **One new General Setting** (`vv_record_approval`); secure-by-default.
- **One new per-project metadata field** (`vv_required_for_submit`) via
  the existing ADR-0002 taxonomy — no schema change, just a seeded row.
- **No new storage primitive** — evidence rides on the file library.
- **A new admin surface** (`/admin/vv-records` — list + filter + bulk-retire)
  plus per-tool / per-job V&V cards on existing detail pages.

## Build phases

Per house style:

- **Phase 1** — `matofx/vv/` package; both tables + the approval-target
  widening migration; ORM + service + errors + a `vv.curate` permission
  seed migration.
- **Phase 2** — Approval routing extension (`api/routers/approvals.py`
  slice-8a pattern): V&V records show in the curator's inbox; decide
  approves / rejects with the existing audit + notification machinery.
- **Phase 3** — Submit-form V&V badge + the per-project
  `vv_required_for_submit` policy gate; envelope-check soft warning.
- **Phase 4** — Admin surfaces: `/admin/vv-records`; per-tool V&V card on
  `/admin/tools/{id}`; per-job V&V card on `/jobs/{id}`.
- **Phase 5** — Cross-ADR surface integration: Compare column headers,
  Lineage nodes (ADR-0010), KPI table filters (ADR-0012), Model registry
  cards (ADR-0008).
- **Phase 6** — Docs + the ROADMAP tick + V&V chapter in the admin
  guide + cross-ADR consistency review.

## Revision history

- **2026-05-26** — initial ruling at full v1.0 scope. Decision driven by
  procurement reality: a regulated customer's first question is the V&V
  record, and the answer "we don't track that" loses the deal. Cost is
  small (two tables + governance reuse) relative to the gate-or-no-deal
  customer signal.
- **2026-05-27 (initial)** — V&V inheritance rule first drafted as "inherit
  across patch + minor versions within a major; reset at major" — the
  cohort key was `(alias, major)`. Discussion surfaced that minor bumps
  cover in-UI file edits which can change bundle bytes without re-V&V,
  making the rule a soft invariant dependent on curator judgement.
- **2026-05-27 (Option C — superseding)** — **V&V inheritance is keyed on
  bundle-byte equality, not version-class.** The V&V cohort is
  `(alias, bundle_hash)`, where `bundle_hash` is the SHA-256 fingerprint
  of the bundle directory's contents computed at version-bump time by
  `tools.versioning.compute_bundle_hash` and stored on the new column
  `tool_version.bundle_hash`. A V&V record applies to every `tool_version`
  sharing the same `(alias, bundle_hash)`; cohort membership is determined
  by the hash, not by the bump class. Whatever the bump class (patch /
  minor / major), if bundle bytes are byte-identical to a sibling's, V&V
  inherits; if different, it doesn't. This makes the rule a *hard*
  invariant resistant to bump-class drift — the badge means exactly what
  it appears to mean. The bump policy in `tools/versioning.py` stays
  unchanged; the V&V rule is independent of it. The Option A patch+minor
  rule from the earlier 2026-05-27 entry is **superseded**. ADR §2 / §3 /
  §8 are amended implicitly via the build-level spec — see
  `designs/vv-tracking.md` §1 (scope row), §2.0 (the new column +
  hash algorithm + bump-path integration), §5.1 (badge query — single
  statement over the composite `(alias, bundle_hash)` index), §6.5 (the
  rule itself with version-boundary truth table), §13 (per-version retire
  + cross-cohort merge both stay deferred — bundle-hash inheritance makes
  the first largely unnecessary; the second is a v1.x ask if a customer
  drives it), §14 (residual risk is in the hash compute, not curator
  judgement), §15 (bump-policy independence).
