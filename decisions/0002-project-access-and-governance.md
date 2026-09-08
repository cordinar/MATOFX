# 0002. Project access, sharing & governance

Date: 2026-05-17
Status: Accepted (decided 2026-05-17 through the Projects planning conversation — see Revision history)

## Context

`PROJECT_BRIEF.md` §D20 locked the permissions model: full RBAC + flat
teams + **project-scoped ACLs granting to users only**, three object
roles (Owner / Editor / Viewer), nothing ever deletable, ownership
transfer as an admin path (clarifier 4), globally-configured default
ACLs (Q5).

Phase 1 shipped something materially different from the §D20 decision —
not just less complete:

- **Team-keyed ACLs.** `hierarchy/orm.py` has a `project_team` table
  (`view`/`edit` per team) and `service.py:_resolve_access()` walks
  `UserTeam` membership at request time. §D20 Q4 explicitly *rejected*
  teams as ACL principals, with a written rationale: adding a user to a
  team must not retroactively grant project access, leavers must not
  auto-lose it, and resolution should be a single user-id lookup. Phase
  1 built the rejected model; the hierarchy README defers "per-user
  (non-team) project ACLs" — i.e. the *decided* model — to Phase 2.
- **No lifecycle state.** §D20 mandates a `state`
  (`active`/`archived`/`cancelled`) + `state_changed_at`/`state_changed_by`
  pair on every user-facing object. `project`/`scenario` have none.
- **Edit/share unreachable.** `ProjectService` has `update`,
  member-management and `transfer_ownership`, but the projects router
  exposes only list/create/get and the UI has no edit or share surface.
  In the running app a project can be created and viewed, never edited
  or shared.

Separately, the planning conversation surfaced a product requirement
that §D20 did **not** capture: per-project, admin-configurable
**typed metadata** (controlled-vocabulary + date/number fields) used
for project classification, the home-table columns/filters, export and
audit.

A knowledgeable evaluator opening v2 today finds projects that cannot
be shared at all, governed by an access model the brief argued against.
This needs a single, recorded decision before any Phase 2 hierarchy
code lands, because Phase 2's "user ACLs" line item is not additive on
top of `project_team` — it replaces the resolution path, the audit
vocabulary, and the membership semantics.

## Decision

Adopt a **per-user, four-role project ACL** with an immutable Creator,
admin-configurable typed project metadata, and the §D20 no-delete /
state model. This supersedes §D20 Q4's three-role/user-only wording and
refines clarifier 4; it is otherwise inside §D20's intent (it is a
*return* to §D20 Q4's user-only principle, with a sharper role set).

**Roles — per-user, per-project, exactly one ACL row per (project, user).**

| Role | Assigned | Capability | Floor |
|---|---|---|---|
| **Creator** | Auto at creation; exactly one; `project.creator_user_id` written once and **frozen forever**; resolves to Owner-equivalent authority automatically. | Everything an Owner can. | Cannot be changed, removed, or deleted. Permanent authority floor — last-owner lockout is structurally impossible. |
| **Owner** | Granted by Creator/Owner/admin; 0..N. | Edit name/description/metadata; manage the ACL; archive scenarios/jobs; add scenarios+tools; run jobs. | Cannot delete the project or alter `creator_user_id`. |
| **Member** | Granted by Creator/Owner. | View the project; add scenarios+tools; run jobs. | Cannot edit project name/description/metadata, manage the ACL, or archive anything. |
| **Viewer** | Granted by Creator/Owner. | **Full read-only**: project, scenarios, every job incl. inputs, outputs/results and logs. | No mutation of any kind. |

Effective level = `max(Creator⇒Owner, ACL role, projects.see_all⇒Owner)`.

**Visibility.** The home-page table lists only projects where the user
is Creator/Owner/Member/Viewer. `projects.see_all` (admin / super-user,
already in the permission catalog) sees all. Default hides archived;
filterable by state, role and metadata.

**No deletion, ever.** Archive (the §D20 `state` model) is the only
"remove", and it is **Creator/Owner only** — Members create and run but
do not archive. This rides in with the missing `state` /
`state_changed_at` / `state_changed_by` columns on `project` and
`scenario`.

**Ownership transfer (admin, retained per §D20 clarifier 4).**
`creator_user_id` never changes. Admin "transfer ownership" is a
distinct, named, heavily-audited action whose effect is to **grant the
designated successor Owner and revoke all other non-Creator Owner
grants** (Members/Viewers untouched). It never touches the Creator. It
is the operational escape hatch for a disabled Creator account (whose
authority is dormant only because the account cannot log in).
*Accepted consequence:* if a disabled Creator account is later
re-enabled it silently regains full project authority — a
"creator-authority-dormant" flag was considered and **deferred**;
mitigation in v1 is procedural (don't re-enable without intent).

**Admin-configurable typed project metadata.** A first-class custom
fields system, admin-owned:

- `project_metadata_field` — key, label, **type ∈
  {single_select, multi_select, date, number}**, required flag,
  display order, show-in-home-table flag.
- `project_metadata_value` — allowed values for the select types, with
  an `active` flag.
- `project_metadata` — per-project typed selections (a `value_id` ref
  for selects, `value_num` / `value_date` for the typed fields; one row
  per value so multi-select is N rows).
- No-delete consistency: retiring a field or value **deactivates** it
  (not offered for new selections) but it is retained on projects that
  already use it. Required fields are enforced at project create *and*
  edit. Metadata is editable by Creator/Owner only. Every
  field/value/selection change is a state-changing, audited action.

**Teams revert to their §D20-intended role:** an organisational / UX
convenience for *bulk-picking users* in the share dialog (expands to
individual ACL rows at click time) — never an ACL principal. Access
resolution becomes a single user-id lookup, exactly the §D20 Q4
rationale.

**Scenario/job ACL = wholesale project inheritance.** §D20's
per-scenario tighten-never-loosen is **explicitly deferred** — the
agreed model is project-scoped throughout.

**Alternatives considered.** (1) Keep Phase 1's team-keyed ACL and add
user ACLs alongside — rejected: dual principals reintroduce exactly the
retroactive-grant / leaver semantics §D20 Q4 rejected, and double the
resolution and audit surface. (2) §D20's literal three roles
(Owner/Editor/Viewer, mutable owner) — superseded: the customer model
needs an *immortal* attribution anchor (Creator) distinct from the
grantable Owner set, which the three-role model cannot express. (3)
Creator fully replaces ownership transfer (no admin reassign) — rejected
in conversation: the named, audited admin hand-off is still wanted for
the departed-Creator case. (4) Free-text metadata — rejected: the
requirement is controlled vocabularies; name/description already cover
free text.

## Consequences

- **Accepted for the v1.0 must-ship hierarchy work, 2026-05-17.**
  Supersedes §D20 Q4's role/principal wording and refines clarifier 4;
  §D20 gets an amendment footer pointing here (per §I47, mirroring the
  ADR-0001 amendment).
- **Comprehensive build spec captured in
  `designs/project-access-and-governance.md`** — schema + migration,
  access-resolution algorithm, API surface, UI surfaces, the
  team→user-ACL migration, phased sequence. The ADR records *why*; the
  design doc records *what to build*.
- **Phase 1 hierarchy is partly superseded, not extended.**
  `project_team` is dropped; `project_acl(project_id, user_id, role)`
  replaces it; `_resolve_access()` collapses to a single user-id
  lookup; `Project.owner_user_id` becomes write-once
  `creator_user_id`; the project uniqueness constraint moves to
  `(creator_user_id, name)`. Migrations are cumulative + idempotent per
  C13; the existing 41 hierarchy tests are rewritten to the new model.
- New tables: `project_acl`, `project_metadata_field`,
  `project_metadata_value`, `project_metadata`; new `state` /
  `state_changed_at` / `state_changed_by` columns on `project` and
  `scenario`.
- Permission catalog: `projects.see_all`,
  `projects.create_for_others`, `projects.transfer_ownership` retained
  (transfer semantics now "successor Owner + revoke other non-Creator
  Owners"); add an explicit `projects.create` verb (default-on for
  Regular user+, making the implicit gate explicit).
- New API + UI surface: project edit, share/ACL management, admin
  metadata-taxonomy admin page, admin transfer-ownership action —
  **all built 2026-05-17** (see Revision history).
- `ROADMAP.md` Phase 2 "Hierarchy" row updated to *Landed*; the
  team-ACL note in the Phase 1 row is a known supersession.
- `ARCHITECTURE.md` §16.17 carries the as-built note (that doc tracks
  as-built state, not design).
- **Revisit when:** the first pilot customer's metadata taxonomy and
  org structure are known — drives whether the deferred per-scenario
  ACL tightening and the creator-authority-dormant flag are pulled
  forward.

## Revision history

- **2026-05-17 — created**, Status Accepted. Decided through the
  Projects planning conversation: four-role per-user ACL with immutable
  Creator; admin transfer = successor Owner + revoke other non-Creator
  Owners; Viewer full read-only; archive Creator/Owner-only;
  admin-configurable typed metadata (single/multi-select + date +
  number); team-keyed ACL superseded; per-scenario ACL tightening and
  creator-authority-dormant flag deferred.
- **2026-05-17 — built (full scope)**. ORM + migrations `0033`–`0036`
  (`0036` seeds the new `projects.manage_metadata` permission),
  service incl. taxonomy admin, full project + admin-taxonomy API, and
  the UI (dashboard filter row + metadata chips; project
  Edit/Share/Archive; `Admin → Project metadata` page). Tests
  rewritten + extended; ruff clean.
- **2026-05-17 — UX follow-up shipped**. `GET /api/users/directory`
  + a people picker in the share dialog (`Full name (username)`, not
  raw ids); project page renders description + resolved metadata + the
  access list grouped Creator/Owners/Members/Viewers; one shared
  `matofx.ui.metadata_form` reused by the dashboard, both Home
  Quick-Access *Launch in new project* flows, and the project Edit
  dialog. Fixed a `me["id"]`→`me["user_id"]` page bug found in
  manual testing.
- **2026-05-17 — create-time share + live edit**. A shared
  `matofx.ui.share_form` people-picker lets a project be shared *as
  it's created* (dashboard *New Project* and both Home Quick-Access
  launchers), not only via Edit afterwards. The project page body is
  now a `ui.refreshable`, so Edit / Share changes show in place with
  no browser refresh (no `ui.navigate` round-trip). Backend unchanged
  (reuses the existing ACL upsert). Only v1.x items remain
  (per-scenario ACL tightening, creator-dormant flag, clone,
  user-level legal hold).
