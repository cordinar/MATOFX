"""Local user accounts, password authentication, sessions, and roles.

Phase 1 ships:

- :class:`User` ORM model + ``user`` table.
- :class:`AuthSession` ORM model + ``auth_session`` table.
- :class:`Role` + :class:`RolePermission` ORM models + tables.
- Argon2id password hashing via :class:`PasswordHasher`.
- :class:`UserService` — create / authenticate / deactivate.
- :class:`SessionService` — create / validate / revoke sessions.
- :class:`RoleService` — create / edit / delete custom roles, assign
  roles to users, with the install-wide invariants enforced.
- :class:`Permission` — closed permission catalog (every gate point
  the application enforces). New roles are runtime data; new
  permissions require a code change.
- :func:`user_has_permission` — the single access-check primitive
  every other service uses.
- Plain-English errors that are safe to render to end-users.

Deferred to Phase 2: SSO (SAML / OIDC), MFA, password reset,
risk-based / step-up authentication, SCIM, IdP group mapping. These
land alongside the first customer's IdP choice.
"""

from __future__ import annotations

from matofx.users.errors import (
    AccountDisabledError,
    AccountLockedError,
    AlreadyInTeamError,
    CannotEditBuiltInRoleError,
    CannotEditSuperUserPermissionsError,
    DuplicateRoleError,
    DuplicateTeamError,
    DuplicateUserError,
    InvalidCredentialsError,
    InvalidEmailError,
    LastSuperUserError,
    NotACapabilityRoleError,
    NotInTeamError,
    PermissionDeniedError,
    PermissionKindMismatchError,
    RoleDeletionForbiddenError,
    RoleInUseError,
    RoleNotFoundError,
    SessionExpiredError,
    SessionInvalidError,
    SuperUserRoleChangeRequiresSuperUserError,
    TeamInUseError,
    TeamNotFoundError,
    UnknownPermissionError,
    UserError,
    WeakPasswordError,
)
from matofx.users.orm import AuthSession, User, UserLogonEvent
from matofx.users.passwords import (
    PasswordHasher,
    make_password_hasher,
    validate_password,
    validate_password_complexity,
)
from matofx.users.permissions import (
    ADMINISTRATOR_DEFAULT_PERMISSIONS,
    BETA_TESTER_DEFAULT_PERMISSIONS,
    REGULAR_USER_DEFAULT_PERMISSIONS,
    SUPER_USER_PERMISSIONS,
    Permission,
    effective_permissions,
    role_permissions,
    user_has_any_permission,
    user_has_permission,
)
from matofx.users.role_orm import Role, RolePermission, UserCapabilityRole
from matofx.users.role_service import RoleService, make_role_service
from matofx.users.roles import (
    DEFAULT_ROLE,
    USER_ROLES,
    UserRole,
    can_see_test_tools,
    has_role_at_least,
    is_known_role,
)
from matofx.users.service import (
    CreatedSession,
    SessionService,
    UserService,
    make_session_service,
    make_user_service,
)
from matofx.users.settings_overlay import (
    EffectiveLockoutPolicy,
    EffectiveSessionPolicy,
    effective_lockout_policy,
    effective_session_policy,
)
from matofx.users.team_orm import Team, UserTeam
from matofx.users.team_service import TeamService, make_team_service
from matofx.users.tokens import hash_session_token, new_session_token

__all__ = [
    "ADMINISTRATOR_DEFAULT_PERMISSIONS",
    "BETA_TESTER_DEFAULT_PERMISSIONS",
    "DEFAULT_ROLE",
    "REGULAR_USER_DEFAULT_PERMISSIONS",
    "SUPER_USER_PERMISSIONS",
    "USER_ROLES",
    "AccountDisabledError",
    "AccountLockedError",
    "AlreadyInTeamError",
    "AuthSession",
    "CannotEditBuiltInRoleError",
    "CannotEditSuperUserPermissionsError",
    "CreatedSession",
    "DuplicateRoleError",
    "DuplicateTeamError",
    "DuplicateUserError",
    "EffectiveLockoutPolicy",
    "EffectiveSessionPolicy",
    "InvalidCredentialsError",
    "InvalidEmailError",
    "LastSuperUserError",
    "NotACapabilityRoleError",
    "NotInTeamError",
    "PasswordHasher",
    "Permission",
    "PermissionDeniedError",
    "PermissionKindMismatchError",
    "Role",
    "RoleDeletionForbiddenError",
    "RoleInUseError",
    "RoleNotFoundError",
    "RolePermission",
    "RoleService",
    "SessionExpiredError",
    "SessionInvalidError",
    "SessionService",
    "SuperUserRoleChangeRequiresSuperUserError",
    "Team",
    "TeamInUseError",
    "TeamNotFoundError",
    "TeamService",
    "UnknownPermissionError",
    "User",
    "UserCapabilityRole",
    "UserError",
    "UserLogonEvent",
    "UserRole",
    "UserService",
    "UserTeam",
    "WeakPasswordError",
    "can_see_test_tools",
    "effective_lockout_policy",
    "effective_permissions",
    "effective_session_policy",
    "has_role_at_least",
    "hash_session_token",
    "is_known_role",
    "make_password_hasher",
    "make_role_service",
    "make_session_service",
    "make_team_service",
    "make_user_service",
    "new_session_token",
    "role_permissions",
    "user_has_any_permission",
    "user_has_permission",
    "validate_password",
    "validate_password_complexity",
]
