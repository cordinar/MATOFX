# 0005. Multi-role users & capability roles

Date: 2026-05-21
Status: Accepted (decided 2026-05-21 through the Databases planning conversation — see Revision history)

## Context

`PROJECT_BRIEF.md` §D20 Q1/Q3 locked a **single global role per user** drawn from
a small set of built-ins. The v2 build realises this faithfully: `User.role_id`
is one FK into the `role` table (`users/orm.py`), with four built-ins —
`super_user`, `administrator`, `beta_tester`, `regular_user` — and a permission
resolver (`users/permissions.py:user_has_permission`) that reads
`role_id → role_permission` rows. Privilege rank for hierarchy/data access is a
separate, hardcoded ladder over those four names (`users/roles.py:_ROLE_RANK`,
`has_role_at_least`).

The codebase already models **orthogonal capabilities separately from the
privilege rank**, twice:

- `beta_tester`'s Test-tool visibility is "orthogonal to the privilege rank"
  (`users/roles.py:can_see_test_tools`).
- Tool **approvers** are designated by a per-tool list that is "independent of
  role" (`users/orm.py` approver flag; `tools/approvers.py`).

Two new features now being designed — the **file library** (ADR-0006) and the
**materials properties database** (ADR-0007) — both need a *curator/steward*
who can manage and approve governed reference data. Critically, the customer
requirement is that these stewards are **subject-matter experts who are regular
users, not admins or super-users** — a materials engineer should curate
material data without gaining any hierarchy or user-management power.

A single global role cannot express "a regular user who is *also* a materials
steward". The options were:

1. **A custom role per combination** (e.g. "Regular + Materials Curator").
   Rejected: combinatorial explosion, and the additive permission set has to be
   re-copied whenever the base role's defaults change.
2. **An orthogonal per-user capability flag** (mirroring the approver pattern).
   Viable and small, but does not generalise — every new steward type becomes a
   new bespoke flag.
3. **Multi-role membership** — a user holds one base role plus zero-or-more
   stackable capability roles; effective permissions are the union.

The customer expects **more stackable "types" as requirements are refined**
(curator today; reviewer/auditor/others later), so option 3 is the model worth
building once, properly. This ADR records that decision because it changes the
core user→role relationship that §D20 locked, and because both new-feature ADRs
depend on it landing first (as ADR-0002 was a prerequisite for parametric
studies).

## Decision

Adopt a **base-role + stackable-capability-role** model. This refines §D20 Q3;
it does not touch the §D20 ACL/teams decisions (those are ADR-0002).

**1. Every user keeps exactly one base role.** `User.role_id` is unchanged and
continues to point at one of the four built-ins. **Privilege rank and all
hierarchy/ACL logic derive solely from the base role** — `_ROLE_RANK`,
`has_role_at_least`, `can_see_test_tools`, and the ADR-0002 access resolution
are untouched. There is deliberately no "max rank across roles" rule to reason
about: rank has exactly one source.

**2. Capability roles are additive, permission-only, and stackable.** They
carry permissions but **no rank** and **no hierarchy/data-access semantics**. A
user holds zero or more of them on top of their base role.

**3. `role.kind` discriminates the two.** A new column on the existing `role`
table: `kind ∈ {'base', 'capability'}`. The four built-ins are `base`; the new
curator roles are `capability`. `base` roles remain assignable only via
`User.role_id` (one per user); `capability` roles are assigned via the join
table below.

**4. New `user_role` join table** carries capability-role membership:
`(user_id, role_id)` composite PK, `granted_at`, `granted_by_user_id`. It holds
**only** `capability`-kind roles (enforced in the service layer). The base role
stays on `User.role_id` — the join table is purely additive.

**5. Effective permissions = base-role permissions ∪ all assigned
capability-role permissions.** This is the only behavioural change to the
resolver: `user_has_permission` (and `role_permissions`) widen from "the base
role's rows" to "the base role's rows ∪ every joined capability role's rows".
The `super_user` sentinel (full grant) short-circuits as today.

**6. First two capability roles, seeded in-migration:**

| Role (`name`, `kind`) | Built-in | Carries |
|---|---|---|
| `library_curator` (capability) | yes | `library.curate` |
| `materials_curator` (capability) | yes | `materials.curate` |

These two permissions are added to the closed `Permission` catalog
(`users/permissions.py`) by ADR-0006 / ADR-0007 respectively; this ADR owns the
*role* machinery, those ADRs own the *permissions* and the features that gate on
them. The roles are `is_built_in = True` (cannot be renamed/deleted) but their
permission sets remain editable at runtime like the other built-ins.

**7. Composition is how granularity is achieved.** There is **no** bundled
"databases curator" role. A library-only steward is `regular_user +
library_curator`; a materials-only steward is `regular_user + materials_curator`;
someone who does both holds both capability roles. Future steward types
(reviewer, auditor, …) slot in as new `capability` roles with no schema change.

**8. The existing tool-approver system is left entirely alone.** Curator
eligibility for the two databases flows from the new capability roles /
permissions, not from the per-tool approver lists.

**Alternatives considered.** (1) Custom-role-per-combination — rejected,
combinatorial and brittle (see Context). (2) Orthogonal per-user capability
flag — rejected as the long-term model because it doesn't generalise to "more
types later", though it is acknowledged as the lighter short-term option the
customer explicitly weighed and declined. (3) Make `User.role_id` itself
many-to-many (no base/capability split) — rejected: it forces a "max rank across
roles" rule and disturbs the untouched ADR-0002 access path for no benefit, since
exactly one base role per user is the desired model.

## Consequences

- **Accepted 2026-05-21**, as the shared foundation for ADR-0006 (file library)
  and ADR-0007 (materials database). Both depend on this landing first; it is
  built and gate-validated as its own slice before either feature.
  **BUILT & gate-validated 2026-05-24** (5 slices) — see the 2026-05-24
  revision-history entry. The shipped migration is **0044** (not 0043: the
  Usage-Statistics `usage.view` migration took 0043 in the interim), and the
  two `*.curate` permission slugs were added to the catalog here (this ADR)
  rather than deferred to ADR-0006/0007, because the seed in 0044 grants them
  and a granted slug must exist in the closed catalog.
- **Schema:** add `role.kind` (CHECK `IN ('base','capability')`, default
  `'base'`); add `user_role(user_id, role_id, granted_at, granted_by_user_id)`.
  New migration **0043** (chains off head `0042`); CHECK on the existing `role`
  table is added via `op.batch_alter_table(..., recreate="always")` per the
  SQLite-can't-ALTER-a-CHECK rule (template: migration 0031). The migration
  seeds the two `capability` roles and grants `library.curate` /
  `materials.curate` to them (querying `role WHERE name=…`; literals frozen — no
  app-enum imports in the migration).
- **Resolver:** `users/permissions.py` `user_has_permission` and
  `role_permissions` widen to the base ∪ capability union. This is the single
  behaviour-bearing code change; everything else is additive.
- **Permission catalog:** `LIBRARY_CURATE = "library.curate"` and
  `MATERIALS_CURATE = "materials.curate"` are added to the closed `Permission`
  StrEnum (formally owned by ADR-0006 / ADR-0007). Default per-base-role sets are
  **unchanged** — neither permission is granted to any base role by default; they
  arrive only through the capability roles.
- **Service:** a small `RoleService` extension (or `users` service method) to
  grant/revoke a capability role to a user, permission-gated by the existing
  `ROLES_MANAGE` / `USERS_MANAGE` admin permission, audited
  (`users.user.capability_role.grant` / `.revoke`).
- **UI:** the admin user-management surface gains a "capability roles"
  multi-select on a user (base role stays a single-select). Buttons hidden
  unless the actor holds the managing permission.
- **No rank ambiguity:** because rank stays on the base role, `has_role_at_least`
  and ADR-0002 resolution need **no** change — verified as a non-goal.
- **Brief:** §D20 Q3 gets an amendment footer pointing here (mirroring the
  ADR-0002 footer at §D20). The brief stays the source of truth for decisions;
  this ADR records the rationale.
- **ROADMAP:** the Phase 2 "Permissions — full RBAC …" row gains a note that
  multi-role/capability roles landed via this ADR.
- **Revisit when:** a future steward type needs *rank* or hierarchy semantics
  (not just extra permissions) — that would be the first capability role that
  doesn't fit "permission-only", and would warrant its own ADR rather than
  bending this model.

## Revision history

- **2026-05-24 — BUILT & gate-validated** (``), in five
  slice-by-slice steps with gates between each:
  1. **Data model + resolver + catalog** — `role.kind` column (CHECK
     `ck_role_kind`, server_default `'base'`); `UserCapabilityRole` ORM (table
     `user_role`, class renamed to avoid colliding with the `UserRole` literal);
     `Permission.LIBRARY_CURATE` / `MATERIALS_CURATE` slugs + descriptions;
     `user_has_permission` widened to the base ∪ capability union; migration
     **0044** (chains off `0043`; seeds `library_curator` / `materials_curator`
     built-in capability roles + grants their curate slug; frozen literals;
     greenfield-safe + idempotent + reversible).
  2. **Service** — `RoleService.grant_capability_role` / `revoke_capability_role`
     (idempotent, `roles.manage`-gated, audited `users.user.capability_role.*`,
     records `granted_by_user_id`) + `list_user_capability_roles` +
     `set_user_capability_roles` (bulk-replace diff) + `NotACapabilityRoleError`
     (rejects stacking a base role).
  3. **API** — role listing exposes `kind`; `AdminUserResponse.capability_roles`;
     `AdminUserCreate.capability_roles` (atomic create-then-grant in one request
     txn); `POST /admin/users/{id}/set-capability-roles`; `NotACapabilityRoleError`
     → 400; `api_client` methods.
  4. **UI** — capability-roles multi-select on the add-user and edit-user forms,
     shown only to holders of `roles.manage`; base-role select filtered to
     `kind == 'base'`. Reusable `effective_permissions()` helper + the caller's
     `permissions` list now surfaced on `GET /auth/me` so the UI can gate
     controls without a per-control round-trip.
  5. **Regression + docs** — whole-tree gates (ruff/mypy zero net-new on every
     changed file; full pytest on SQLite, Postgres in CI) + this entry, the
     ROADMAP note, and the consequences amendment above.
- **2026-05-21 — created, Accepted.** Databases planning conversation:
  base-role + stackable capability-role model; `role.kind` discriminator; new
  `user_role` join table for capability membership only; effective permissions =
  base ∪ capabilities; rank stays on the single base role (ADR-0002 path
  untouched); two seeded capability roles `library_curator` /
  `materials_curator`; tool-approver system left as-is; granularity via
  composition, no bundled databases-curator role. Build not started.
