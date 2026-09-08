# `matofx.hierarchy` — Project → Scenario → Job

Every job lives in a scenario (or a parametric study), every scenario
lives in a project, every project has a Creator. Per **ADR-0002** the
project access model is a **per-user four-role ACL** with an immutable
Creator, the §D20 no-delete `state` lifecycle, and an
admin-configurable typed metadata taxonomy. This supersedes Phase 1's
team-keyed `project_team` ACL (decision record:
`decisions/0002-project-access-and-governance.md`; build spec:
`designs/project-access-and-governance.md`).

## Roles

| Role | Source | Capability |
|---|---|---|
| **Creator** | `project.creator_user_id` — written once, never changed | Owner-equivalent authority + provenance; permanent floor (last-owner lockout is impossible) |
| **Owner** | `project_acl` row, `role='owner'` | Edit project/metadata, manage the ACL, archive, add scenarios/jobs, run |
| **Member** | `project_acl` row, `role='member'` | Add scenarios + tools, run jobs; **not** project/metadata edit, ACL, or archive |
| **Viewer** | `project_acl` row, `role='viewer'` | Full read-only incl. job inputs/outputs/logs |

Effective tier = `max(Creator⇒OWNER, projects.see_all⇒OWNER, ACL role)`
where `viewer→VIEW`, `member→EDIT`, `owner→OWNER`. Resolution is a
single `(project_id, user_id)` lookup — **teams are not ACL
principals** (they are a bulk-pick UX convenience that the share dialog
expands to individual rows). Read needs VIEW; create-scenario/run-job
needs EDIT; project edit / ACL / archive / metadata need OWNER.

`NONE` → `NotFoundError` (existence-hiding). Some access but below the
required tier → `AccessDeniedError`. `ProjectService.access_for(...)`
returns the tier without raising for UI branching.

## Tables

- `project` — `creator_user_id` (write-once, FK user RESTRICT),
  `state` (`active`/`archived`/`cancelled`, CHECK) + `state_changed_at`
  / `state_changed_by_user_id`, `version` (optimistic lock). Unique
  `(creator_user_id, name)`.
- `project_acl` — composite PK `(project_id, user_id)`,
  `role ∈ {owner,member,viewer}` (CHECK). One row per user; the
  Creator is never a row here.
- `scenario` — same `state` trio; inherits the project ACL wholesale
  (per-scenario tightening is deferred to v1.x).
- `job` — unchanged; `scenario_id` XOR `parametric_study_id`.
- `project_metadata_field` / `project_metadata_value` /
  `project_metadata` — admin taxonomy: single/multi-select + date +
  number fields; retiring a field/value flips `active` (no-delete);
  `project_metadata` holds one typed value per row (CHECK enforces
  exactly one of `value_id`/`value_num`/`value_date`).

## ProjectService surface

- `create` / `get` / `list_for_user(include_archived=False)` /
  `update` (OWNER, optimistic-lock aware).
- `set_state` — archive/cancel (OWNER; no-op transition →
  `InvalidStateError`; nothing is ever deleted).
- `transfer_ownership` — `creator_user_id` untouched; successor gets
  an `owner` grant and **all other non-Creator `owner` grants are
  revoked** (Members/Viewers untouched). Allowed for the Creator, an
  explicit Owner, or `projects.transfer_ownership` (super_user only —
  `projects.see_all` alone is deliberately *not* enough).
- `list_acl` / `add_acl_entry` / `update_acl_entry` /
  `remove_acl_entry` / `set_acl` (batch replace). The Creator can
  never be an ACL target (`CreatorImmutableError`).
- `get_metadata` (VIEW) / `set_metadata` (OWNER) — validates against
  the live taxonomy: required fields present, value type matches the
  field, select values must be an active option; all-or-nothing.

## Audit

Every mutation records an event: `hierarchy.project.create` /
`.update` / `.archive` / `.transfer_ownership`;
`hierarchy.project.acl.add` / `.update` / `.remove`;
`hierarchy.project.metadata.update`; `hierarchy.scenario.create`;
`hierarchy.job.create` / `hierarchy.job.state.<new>`. Reads are not
audited at this layer (per-record read auditing is the separate
Phase 2 §D20/§D21 item).

## Status

ADR-0002 fully landed: ORM + migrations `0033`–`0036` (drop
`project_team`, `owner_user_id`→`creator_user_id`, `project_acl`, the
metadata trio, the `projects.manage_metadata` permission), service
(single-lookup resolution, ACL CRUD+batch, archive, transfer, metadata
validate/read/write + taxonomy admin), the full project + admin-
taxonomy API, the UI (dashboard metadata columns/filters, project
edit/share/archive, `Admin → Project metadata`), and the rewritten +
extended test suite.

## Deferred (v1.x — recorded, not silent)

- Per-scenario / per-job ACL tightening (§D20 tighten-never-loosen).
- Creator-authority-dormant flag for a re-enabled disabled Creator.
- Clone (project / scenario); user-level legal hold.
