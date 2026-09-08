# `matofx.users` — local accounts, password auth, sessions

Phase 1 ships local username + password authentication with hash-chained-audited sessions. SSO (SAML / OIDC), MFA, password reset, full RBAC, SCIM, and IdP group mapping all land in Phase 2 alongside the first customer's IdP choice and the permissions matrix.

## Why this module exists

§I46 of the brief locks "Local + SSO + MFA + sessions" as v1.0 must-ship. The shape we ship in Phase 1 is the smallest end-to-end slice that actually lets a user log in and stay logged in — auth modes layer on top in Phase 2 without disturbing the local-password path that every install will continue to support as a fallback.

## Public surface

```python
from matofx.config import load_settings
from matofx.audit import make_audit_writer
from matofx.db import make_engine, make_session_factory, session_scope
from matofx.users import make_user_service, make_session_service

settings = load_settings()
audit = make_audit_writer(settings.audit)
user_service = make_user_service(settings.auth, audit)
session_service = make_session_service(settings.auth, audit)

engine = make_engine(settings.db)
factory = make_session_factory(engine)

# Sign in
async with session_scope(factory) as session:
    user = await user_service.authenticate(
        session, username="alice", password="…",
    )
    created = await session_service.create_session(
        session, user_id=user.id, ip_address="…",
    )
    # `created.token` goes to the client; only its hash is persisted.
```

## Roles and permissions

The role / permission system is **runtime data**: admins create new roles and tune permission sets through the admin UI without touching the codebase. The only thing that requires a code change is adding a brand-new permission slug — i.e. a new gate point the application will enforce. Existing permissions can be granted to or revoked from any role at runtime.

**Architecture:**

- **Permissions** are a closed catalog of slugs (`projects.see_all`, `tools.install`, `tools.see_test_state`, ...) defined in `matofx.users.permissions.Permission`. Each slug names a gate point in the codebase.
- **Roles** are rows in the `role` table. Each role has a name, description, `is_built_in` flag, and a set of granted permissions (rows in the `role_permission` join table).
- **`User.role_id`** points to the role the user holds. Permission checks resolve through user → role → role_permission via `await user_has_permission(session, user_id, permission)`.

### Built-in roles

MATOFX ships **four built-in roles** organised into two privilege tiers, plus an orthogonal **tool-state capability**:

**Privileged tier** — see and modify every project, manage users:

| # | Role | Description |
|---|---|---|
| 1 | `super_user` | Full control. Manages users, installs / removes tools, rotates the audit signing key. The exact boundary with `administrator` is sharpened when the relevant administrative surfaces land. |
| 2 | `administrator` | Operational control. Same hierarchy access as `super_user` in Phase 1. |

**Standard tier** — see and modify only their own projects:

| # | Role | Description |
|---|---|---|
| 3 | `beta_tester` | **Same hierarchy rights as `regular_user`.** The only practical difference is visibility of tools in the `Test` state — the class of user that validates pre-release tool deployments before general release. |
| 4 | `regular_user` | Default role for new accounts. Sees only their own projects, runs only `Released` tools. |

Helpers in `matofx.users.roles`:

- `USER_ROLES` — the canonical tuple of all four roles.
- `DEFAULT_ROLE` — `"regular_user"`.
- `has_role_at_least(actual, threshold)` — privilege test for hierarchy / data access. `beta_tester` and `regular_user` are equivalent for this check; unknown role strings never satisfy any threshold.
- `can_see_test_tools(role)` — orthogonal capability test for visibility of tools in the `Test` state. True for the privileged tier and `beta_tester`; false for `regular_user`.
- `is_known_role(role)` — predicate over `USER_ROLES`.

### Custom roles

Admins (anyone holding `roles.manage` — default: super_user + administrator) create custom roles at runtime via `RoleService.create_role(name, description, permissions=[...])`. Permissions are picked from the closed catalog. The new role appears in the system immediately; users assigned to it pick up its permissions on the next request. **No code change is needed** — custom roles are pure data.

### Invariants

Two install-wide invariants are enforced in `RoleService`:

1. **`super_user` permissions are immutable.** It always holds every permission in the catalog (including future ones). An admin cannot edit, restrict, or override the super_user permission set — it's the break-glass role.
2. **At least one active super-user must always exist.** `RoleService.assign_role_to_user` refuses to demote the last active super-user; `LastSuperUserError` surfaces a plain-English message telling the operator to promote someone else first.

Built-in roles cannot be deleted or renamed (their names are referenced by the migration that seeds defaults). The permission sets of `administrator`, `beta_tester`, and `regular_user` **can** be tuned by admins — e.g. you can add `tools.see_test_state` to your `regular_user` role if you want every authenticated user to see Test-state tools.

### Adding a new permission

When MATOFX needs a new gate point (e.g. Phase 2 adds approvals → `approvals.delegate`):

1. Add the new member to the `Permission` enum in `matofx.users.permissions`.
2. Decide which default role permission sets should include it (administrator / beta_tester / regular_user). Never super_user — it gets everything by sentinel.
3. Ship a migration that adds the new permission rows to the relevant built-in roles.
4. Add the actual `await user_has_permission(session, user_id, Permission.APPROVALS_DELEGATE)` check at the gate point.

Customers don't wait on Funis to *create* roles — only to *ship features that come with new gate points*. They can grant any existing permission to any role at any time.

## Tool states

Companion to the role system (lands with `matofx.tools`): every tool has a state, one of:

- **New** — author-only / privileged tier. Work in progress, not yet ready for beta validation.
- **Test** — visible to `beta_tester` plus the privileged tier. Pre-release; this is what beta testers validate.
- **Released** — visible to everyone with access to the tool's project context.

Promotion (New → Test → Released) is gated by privileged-tier action. The tool state is independent of the user role: a `regular_user` cannot see Test-state tools no matter who shares them.

## Tables

- `user` — local account: username (unique), email (unique), first_name, last_name, full_name, password_hash (Argon2id), is_active, is_approver, role_id, failed_login_count, locked_until, last_login_at, logon_granted_at, logon_revoked_at, created_at, updated_at.
- `auth_session` — live sessions identified by SHA-256 of a never-stored client-side token. Has `user_id` (cascading FK), ip_address, user_agent, created_at, last_activity_at, revoked_at.
- `team` — flat catalog of teams: name (unique), description, created_by_user_id (SET NULL), created_at, updated_at.
- `user_team` — many-to-many membership join: composite PK on (user_id, team_id), joined_at, joined_by_user_id (SET NULL). Both FKs cascade-delete.
- `user_logon_event` — append-only history of every grant/revoke transition: user_id (FK CASCADE), event (`granted` | `revoked` — CHECK constraint), at, actor_user_id (FK SET NULL), reason. The denormalised `logon_granted_at` / `logon_revoked_at` on `user` are the latest-cycle pointers; this table is the durable history. A user may move through any number of cycles. `UserService.list_logon_events(user_id)` returns the full history oldest-first for the user-edit page.

### User extras explained

- **`first_name` + `last_name`** — stored separately for sortable lists, salutations, and report generation. `full_name` is auto-derived as `f"{first_name} {last_name}".strip()` when not supplied explicitly to `create_user`.
- **`is_approver`** — orthogonal flag (independent of role). When `True`, the user is eligible to be selected as a tool-run approver under the per-tool approval policies. Tunable at create time and via `UserService.set_approver`.
- **`logon_granted_at` / `logon_revoked_at`** — lifecycle timestamps for IT audit. `logon_granted_at` is stamped at create and refreshed on `reactivate`; `logon_revoked_at` is stamped on `deactivate` and cleared on `reactivate`. The `is_active` flag is the live gate; the timestamp pair is the durable history.

## Teams

Teams are a flat catalog (no hierarchy) used for grouping users. They serve two purposes in v1:

1. **Tool authorisation context** — per-tool `categories` + team-membership rules can restrict who runs a tool.
2. **Notification routing** — workflow-level subscriptions can target whole teams.

`TeamService` (build via `make_team_service(audit)`) provides:

- Read: `list_teams`, `get_by_name`, `list_user_teams(user_id)`, `list_team_members(team_id)`. **Not permission-gated** — team membership is visible to anyone authenticated. Tighten in Phase 2 if a customer needs team confidentiality.
- Mutate (gated on `Permission.TEAMS_MANAGE`, default super_user + administrator): `create_team`, `delete_team` (refuses if the team has members), `add_user_to_team`, `remove_user_from_team`, `set_user_teams(user_id, team_ids)`. All audited.

## Security choices

- **Argon2id** for password hashing (PHC winner; OWASP-recommended). Parameters tunable via `AuthSettings.argon2_*`.
- **Token-hash storage**: a DB compromise can't be used to hijack live sessions because only the SHA-256 of each session token is persisted.
- **No username enumeration**: every authentication failure mode (unknown user, wrong password, locked) raises `InvalidCredentialsError` with the same plain-English message. `AccountDisabledError` is the only distinguishable failure, and only after the password is verified.
- **Lockout** after `failed_login_lockout_threshold` failures, for `failed_login_lockout_minutes`. Both are settings-tunable.
- **Per-IP login throttle** (`login_throttle.IpLoginThrottle`, `login_ip_rate_limit_*` settings) — a sliding-window per-IP failed-login counter that blocks an IP for a cooldown, so an attacker can't spray guesses across many usernames to dodge the per-account lockout. In-process (single-worker daemon); per-account lockout remains the durable cross-process guarantee. Wired into the login endpoint, raises `TooManyLoginAttemptsError` → 429.
- **Idle + hard timeouts** on sessions (`session_idle_timeout_minutes`, `session_hard_timeout_hours`) — both checked on every `validate()` call.
- **Audit on every event**: user create, login success / failure / disabled / locked, session create / revoke, deactivate. The audit writer is part of every service constructor, not optional.

## Bootstrap

A fresh install has no users. The `matofx admin create-first-user` CLI subcommand calls `matofx.users.bootstrap.create_first_user`, which refuses to run if any user already exists. That same helper is the only sanctioned path to create the first super-user.

```bash
matofx admin create-first-user --username root --email root@example.com --first-name Root --last-name Admin
# (prompts for password if --password is omitted)
```

## Phase 1 status

- Pydantic-validated `AuthSettings` (single source of fallback values, per project rule).
- `User` and `AuthSession` ORM models with Alembic migration `0003_users.py`.
- `PasswordHasher` (Argon2id wrap) with rehash detection.
- `UserService` — `create_user`, `authenticate`, `deactivate`, `reactivate`, `set_approver`, `get_by_username`, `list_logon_events`. Audited. `create_user`, `deactivate`, and `reactivate` accept optional `actor_user_id` (FK on the event row) and `reason` (plain-English note shown in the user-edit history pane).
- `SessionService` — `create_session`, `validate`, `revoke`. Audited. Idempotent revoke. `validate` also refuses deactivated accounts (defence-in-depth).
- `TeamService` — `list_teams`, `list_user_teams`, `list_team_members`, `create_team`, `delete_team`, `add_user_to_team`, `remove_user_from_team`, `set_user_teams`. Audited; mutations gated on `teams.manage`.
- `RoleService` — `create_role`, `update_role`, `delete_role`, `assign_role_to_user`, `set_role_permissions`. Built-in role + last-super-user invariants enforced.
- Plain-English `*Error` exceptions, all PEP 8 compliant.
- `bootstrap.create_first_user` + `matofx admin create-first-user` CLI.
- Tests: passwords (8), tokens (3), user service (10 SQLite + 10 Postgres-skipped), session service (8 SQLite + 8 Postgres-skipped), bootstrap (2 SQLite + 2 Postgres-skipped). Tamper-detection through audit chain integrity is exercised by `tests/audit/`.

## Deferred to Phase 2 / v1.x

- SSO (SAML or OIDC — first customer's pick).
- MFA (mandatory for admins per §I46).
- Password reset flow + breach-database checks.
- Composition rules in `validate_password` (mixed case / digits / symbols).
- Risk-based / step-up authentication.
- SCIM provisioning.
- IdP group → MATOFX role mapping.
- Just-in-time auto-provisioning on SSO sign-in.
- Project ACLs (teams + roles ship in v1; project-level ACLs land with `matofx.hierarchy`).
- Per-user landing-dashboard assignment.
- Team confidentiality (currently any authenticated user can list teams + memberships).
