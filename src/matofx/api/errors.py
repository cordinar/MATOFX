"""Domain-error → HTTP status mapping.

Every domain exception in :mod:`matofx.hierarchy.errors`,
:mod:`matofx.users.errors`, :mod:`matofx.tools.errors`, etc. carries a
plain-English message intended for the end user. The handlers here
turn each into a single :class:`fastapi.HTTPException` with the right
status code; the message becomes the response body.

The centralisation is deliberate — every router gets uniform error
shape without each one having to remember the mapping.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import status
from fastapi.responses import JSONResponse

from matofx.approvals.errors import (
    ApprovalAlreadyDecidedError,
    ApprovalError,
    ApprovalNotPermittedError,
    ApprovalRequestNotFoundError,
    DenialReasonRequiredError,
    DuplicatePendingApprovalError,
    InvalidApprovalKindError,
    SelfApprovalNotAllowedError,
)
from matofx.config.general_errors import (
    GeneralSettingsAccessDeniedError,
    GeneralSettingsError,
    ImageUploadRejectedError,
    InvalidGeneralSettingValueError,
    UnknownGeneralSettingKeyError,
)
from matofx.favourites.errors import (
    FavouritesDisabledError,
    FavouritesError,
    FavouritesUnknownKindError,
)
from matofx.hierarchy.errors import (
    AccessDeniedError,
    ConcurrentModificationError,
    CreatorImmutableError,
    DuplicateNameError,
    DuplicateToolInScenarioError,
    HierarchyError,
    InvalidJobStateError,
    InvalidRoleError,
    InvalidStateError,
    JobAlreadyTerminalError,
    JobNotQueueableError,
    MetadataError,
    NotFoundError,
    ResultsWithheldError,
    StorageLocationUnavailableError,
    ToolNotRunnableError,
    UserAlreadyGrantedError,
    UserNotGrantedError,
)
from matofx.library.errors import (
    ApproverRequiredError as LibraryApproverRequiredError,
)
from matofx.library.errors import (
    DuplicateToolAssignmentError,
    GlobalPublishNotPermittedError,
    InvalidScopeError,
    LibraryAccessDeniedError,
    LibraryConcurrentModificationError,
    LibraryError,
    LibraryFileNotFoundError,
    LibraryFileRetiredError,
    LibraryValidationError,
    LibraryVersionNotFoundError,
    ProjectScopeError,
    ToolAssignmentNotFoundError,
    ToolAssignmentRequiredError,
)
from matofx.library.storage import LibraryStorageError
from matofx.licence.errors import (
    LicenceAssignmentsExhaustedError,
    LicenceError,
    LicenceSlotsExhaustedError,
)
from matofx.materials.errors import (
    ApproverRequiredError as MaterialsApproverRequiredError,
)
from matofx.materials.errors import (
    DuplicateClassPropertyError,
    DuplicateMaterialClassError,
    DuplicateMaterialPropertyError,
    MaterialClassNotFoundError,
    MaterialClassPropertyNotFoundError,
    MaterialClassRetiredError,
    MaterialConcurrentModificationError,
    MaterialNotFoundError,
    MaterialRetiredError,
    MaterialSchemaError,
    MaterialsError,
    MaterialValidationError,
    MaterialVersionNotFoundError,
)
from matofx.migrate.bundle import BundleError
from matofx.migrate.errors import (
    BundleUntrustedError,
    MigrateError,
    SurrogateToolNotExportableError,
    ToolNotFoundForExportError,
    TrustPeerAlreadyTrustedError,
    TrustPeerNotFoundError,
    WorkflowNotFoundForExportError,
)
from matofx.migrate.signing import BundleSigningError
from matofx.models.errors import ModelError
from matofx.runner.errors import (
    InvalidSettingValueError,
    RuntimeSettingsAccessDeniedError,
    RuntimeSettingsError,
    UnknownSettingKeyError,
)
from matofx.studies.cohort.errors import CohortError
from matofx.studies.coverage.errors import CoverageError
from matofx.studies.errors import StudyGenerationError, StudyStateError
from matofx.studies.optimisation.errors import OptimisationError
from matofx.tools.errors import (
    ApproverNotEnabledError,
    DuplicateToolError,
    InvalidFileEditError,
    InvalidPromotionError,
    InvalidStateTransitionError,
    PythonEnvNotUsableError,
    SoleApproverError,
    SyntaxValidationError,
    TeamsPendingError,
    ToolArchivedError,
    ToolError,
    ToolNotFoundError,
    UnknownApproverError,
    UnknownMaterialClassError,
    UnknownMaterialPropertyError,
    UnknownTeamError,
)
from matofx.tools.paths_service import (
    DuplicateNameError as PathsDuplicateNameError,
)
from matofx.tools.paths_service import (
    NotFoundError as PathsNotFoundError,
)
from matofx.tools.paths_service import (
    PathsAdminError,
)
from matofx.users.errors import (
    AccountDisabledError,
    AlreadyInTeamError,
    CannotEditBuiltInRoleError,
    CannotEditSuperUserPermissionsError,
    DuplicateRoleError,
    DuplicateTeamError,
    DuplicateUserError,
    IncorrectCurrentPasswordError,
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
    TooManyLoginAttemptsError,
    UnknownPermissionError,
    UserError,
    WeakPasswordError,
)
from matofx.vv.errors import (
    JobVVRecordNotFoundError,
    ToolVVRecordNotFoundError,
    VVApproverRequiredError,
    VVConcurrentModificationError,
    VVDecisionReasonRequiredError,
    VVError,
    VVEvidenceLockError,
    VVMigrationError,
    VVPolicyError,
    VVSoleCuratorError,
    VVStateTransitionError,
)

if TYPE_CHECKING:
    from fastapi import FastAPI, Request


_log = logging.getLogger(__name__)


def _json(message: str, http_status: int) -> JSONResponse:
    return JSONResponse(status_code=http_status, content={"detail": message})


# Domain-exception → HTTP status. Anything not in the table falls
# through to a generic 500 (and gets logged with full context).
_STATUS_MAP: dict[type[Exception], int] = {
    # --- auth ---
    InvalidCredentialsError: status.HTTP_401_UNAUTHORIZED,
    SessionInvalidError: status.HTTP_401_UNAUTHORIZED,
    SessionExpiredError: status.HTTP_401_UNAUTHORIZED,
    AccountDisabledError: status.HTTP_403_FORBIDDEN,
    TooManyLoginAttemptsError: status.HTTP_429_TOO_MANY_REQUESTS,
    # Capacity, not authorization: the password was right. 503 keeps it
    # clearly distinct from a 401/403 in logs and in the login form.
    LicenceSlotsExhaustedError: status.HTTP_503_SERVICE_UNAVAILABLE,
    # 409, not 503: the admin has a decision to make (free one, or
    # extend the licence). Retrying unchanged will never succeed.
    LicenceAssignmentsExhaustedError: status.HTTP_409_CONFLICT,
    IncorrectCurrentPasswordError: status.HTTP_400_BAD_REQUEST,
    # --- users (identity / credentials validation) ---
    DuplicateUserError: status.HTTP_409_CONFLICT,
    WeakPasswordError: status.HTTP_400_BAD_REQUEST,
    InvalidEmailError: status.HTTP_400_BAD_REQUEST,
    # --- roles + permissions ---
    RoleNotFoundError: status.HTTP_404_NOT_FOUND,
    RoleInUseError: status.HTTP_409_CONFLICT,
    RoleDeletionForbiddenError: status.HTTP_409_CONFLICT,
    DuplicateRoleError: status.HTTP_409_CONFLICT,
    CannotEditBuiltInRoleError: status.HTTP_409_CONFLICT,
    CannotEditSuperUserPermissionsError: status.HTTP_403_FORBIDDEN,
    SuperUserRoleChangeRequiresSuperUserError: status.HTTP_403_FORBIDDEN,
    LastSuperUserError: status.HTTP_409_CONFLICT,
    UnknownPermissionError: status.HTTP_400_BAD_REQUEST,
    NotACapabilityRoleError: status.HTTP_400_BAD_REQUEST,
    PermissionKindMismatchError: status.HTTP_400_BAD_REQUEST,
    PermissionDeniedError: status.HTTP_403_FORBIDDEN,
    # --- teams ---
    TeamNotFoundError: status.HTTP_404_NOT_FOUND,
    DuplicateTeamError: status.HTTP_409_CONFLICT,
    TeamInUseError: status.HTTP_409_CONFLICT,
    AlreadyInTeamError: status.HTTP_409_CONFLICT,
    NotInTeamError: status.HTTP_404_NOT_FOUND,
    # --- hierarchy ---
    NotFoundError: status.HTTP_404_NOT_FOUND,
    AccessDeniedError: status.HTTP_403_FORBIDDEN,
    DuplicateNameError: status.HTTP_409_CONFLICT,
    DuplicateToolInScenarioError: status.HTTP_409_CONFLICT,
    InvalidJobStateError: status.HTTP_400_BAD_REQUEST,
    InvalidRoleError: status.HTTP_400_BAD_REQUEST,
    InvalidStateError: status.HTTP_409_CONFLICT,
    MetadataError: status.HTTP_400_BAD_REQUEST,
    JobAlreadyTerminalError: status.HTTP_409_CONFLICT,
    JobNotQueueableError: status.HTTP_409_CONFLICT,
    ResultsWithheldError: status.HTTP_403_FORBIDDEN,
    StorageLocationUnavailableError: status.HTTP_409_CONFLICT,
    ToolNotRunnableError: status.HTTP_409_CONFLICT,
    ConcurrentModificationError: status.HTTP_409_CONFLICT,
    CreatorImmutableError: status.HTTP_409_CONFLICT,
    UserAlreadyGrantedError: status.HTTP_409_CONFLICT,
    UserNotGrantedError: status.HTTP_404_NOT_FOUND,
    # --- tools ---
    DuplicateToolError: status.HTTP_409_CONFLICT,
    ToolNotFoundError: status.HTTP_404_NOT_FOUND,
    UnknownTeamError: status.HTTP_400_BAD_REQUEST,
    UnknownApproverError: status.HTTP_400_BAD_REQUEST,
    UnknownMaterialClassError: status.HTTP_400_BAD_REQUEST,
    UnknownMaterialPropertyError: status.HTTP_400_BAD_REQUEST,
    ApproverNotEnabledError: status.HTTP_400_BAD_REQUEST,
    PythonEnvNotUsableError: status.HTTP_400_BAD_REQUEST,
    SoleApproverError: status.HTTP_409_CONFLICT,
    InvalidFileEditError: status.HTTP_400_BAD_REQUEST,
    SyntaxValidationError: status.HTTP_422_UNPROCESSABLE_CONTENT,
    # Lifecycle refusals carry a plain-English reason the UI shows as-is;
    # unmapped they fell through to the generic 500 message.
    InvalidStateTransitionError: status.HTTP_400_BAD_REQUEST,
    InvalidPromotionError: status.HTTP_409_CONFLICT,
    TeamsPendingError: status.HTTP_409_CONFLICT,
    ToolArchivedError: status.HTTP_409_CONFLICT,
    # --- runtime settings ---
    RuntimeSettingsAccessDeniedError: status.HTTP_403_FORBIDDEN,
    UnknownSettingKeyError: status.HTTP_400_BAD_REQUEST,
    InvalidSettingValueError: status.HTTP_400_BAD_REQUEST,
    # --- general settings ---
    GeneralSettingsAccessDeniedError: status.HTTP_403_FORBIDDEN,
    UnknownGeneralSettingKeyError: status.HTTP_400_BAD_REQUEST,
    InvalidGeneralSettingValueError: status.HTTP_400_BAD_REQUEST,
    ImageUploadRejectedError: status.HTTP_400_BAD_REQUEST,
    # --- favourites ---
    FavouritesDisabledError: status.HTTP_409_CONFLICT,
    FavouritesUnknownKindError: status.HTTP_400_BAD_REQUEST,
    # --- paths admin (storage_location + python_env registries) ---
    PathsDuplicateNameError: status.HTTP_409_CONFLICT,
    PathsNotFoundError: status.HTTP_404_NOT_FOUND,
    # --- cross-install migrate ---
    TrustPeerNotFoundError: status.HTTP_404_NOT_FOUND,
    TrustPeerAlreadyTrustedError: status.HTTP_409_CONFLICT,
    BundleUntrustedError: status.HTTP_403_FORBIDDEN,
    ToolNotFoundForExportError: status.HTTP_404_NOT_FOUND,
    SurrogateToolNotExportableError: status.HTTP_409_CONFLICT,
    WorkflowNotFoundForExportError: status.HTTP_404_NOT_FOUND,
    # ``BundleError`` covers malformed-tarball / hash-mismatch / path-traversal
    # detected at unpack time. ``BundleSigningError`` covers
    # signature-verify / fingerprint-format problems. Both reflect bad
    # input from the operator's perspective — return 400 so the UI's
    # import preview surfaces them clearly.
    BundleError: status.HTTP_400_BAD_REQUEST,
    BundleSigningError: status.HTTP_400_BAD_REQUEST,
    # --- parametric studies ---
    # Every generation failure (factor-spec malformed, mode not
    # applicable, run-cap exceeded, no valid points) and every
    # lifecycle misuse (submit a non-draft, unknown mode) is a
    # plain-English 400 the UI surfaces verbatim. The base classes
    # cover their subclasses via the isinstance walk below.
    StudyGenerationError: status.HTTP_400_BAD_REQUEST,
    StudyStateError: status.HTTP_400_BAD_REQUEST,
    # Run cohorts (ADR-0008): config + state misuse are plain-English 400s
    # (the base class covers CohortConfigError / CohortStateError via the
    # isinstance walk).
    CohortError: status.HTTP_400_BAD_REQUEST,
    # Design-space coverage (ADR-0020): a malformed envelope — inverted
    # bounds, a log scale over a span that includes zero — is the user's
    # own form input, so the plain-English message goes back verbatim. A
    # *small* or degenerate cohort is deliberately not an error: it returns
    # the `insufficient` verdict, because "we cannot tell" is an answer.
    CoverageError: status.HTTP_400_BAD_REQUEST,
    # Optimisation studies (ADR-0009): config / state / corrupt-state failures
    # are plain-English 400s (base class covers the subclasses via the walk).
    OptimisationError: status.HTTP_400_BAD_REQUEST,
    # Surrogate Model registry (ADR-0008): config / state / bundle-corrupt
    # failures are plain-English 400s (base class covers the subclasses).
    ModelError: status.HTTP_400_BAD_REQUEST,
    # --- approvals (directed-approver gate shared by library / materials) ---
    SelfApprovalNotAllowedError: status.HTTP_400_BAD_REQUEST,
    ApprovalRequestNotFoundError: status.HTTP_404_NOT_FOUND,
    ApprovalAlreadyDecidedError: status.HTTP_409_CONFLICT,
    ApprovalNotPermittedError: status.HTTP_403_FORBIDDEN,
    DenialReasonRequiredError: status.HTTP_400_BAD_REQUEST,
    InvalidApprovalKindError: status.HTTP_400_BAD_REQUEST,
    DuplicatePendingApprovalError: status.HTTP_409_CONFLICT,
    # --- materials database (ADR-0007) ---
    MaterialNotFoundError: status.HTTP_404_NOT_FOUND,
    MaterialVersionNotFoundError: status.HTTP_404_NOT_FOUND,
    MaterialClassNotFoundError: status.HTTP_404_NOT_FOUND,
    MaterialClassPropertyNotFoundError: status.HTTP_404_NOT_FOUND,
    MaterialValidationError: status.HTTP_400_BAD_REQUEST,
    MaterialSchemaError: status.HTTP_400_BAD_REQUEST,
    DuplicateMaterialClassError: status.HTTP_409_CONFLICT,
    DuplicateClassPropertyError: status.HTTP_409_CONFLICT,
    DuplicateMaterialPropertyError: status.HTTP_400_BAD_REQUEST,
    MaterialRetiredError: status.HTTP_409_CONFLICT,
    MaterialClassRetiredError: status.HTTP_409_CONFLICT,
    MaterialConcurrentModificationError: status.HTTP_409_CONFLICT,
    MaterialsApproverRequiredError: status.HTTP_400_BAD_REQUEST,
    # --- file library (ADR-0006) ---
    LibraryFileNotFoundError: status.HTTP_404_NOT_FOUND,
    LibraryVersionNotFoundError: status.HTTP_404_NOT_FOUND,
    LibraryAccessDeniedError: status.HTTP_403_FORBIDDEN,
    LibraryValidationError: status.HTTP_400_BAD_REQUEST,
    InvalidScopeError: status.HTTP_400_BAD_REQUEST,
    ProjectScopeError: status.HTTP_400_BAD_REQUEST,
    LibraryFileRetiredError: status.HTTP_409_CONFLICT,
    ToolAssignmentRequiredError: status.HTTP_400_BAD_REQUEST,
    DuplicateToolAssignmentError: status.HTTP_409_CONFLICT,
    ToolAssignmentNotFoundError: status.HTTP_404_NOT_FOUND,
    LibraryApproverRequiredError: status.HTTP_400_BAD_REQUEST,
    GlobalPublishNotPermittedError: status.HTTP_403_FORBIDDEN,
    LibraryConcurrentModificationError: status.HTTP_409_CONFLICT,
    # A byte-store read/write failure is an infrastructure problem, not
    # bad user input; surface the plain-English message at 500.
    LibraryStorageError: status.HTTP_500_INTERNAL_SERVER_ERROR,
    # --- V&V tracking (ADR-0011) ---
    ToolVVRecordNotFoundError: status.HTTP_404_NOT_FOUND,
    JobVVRecordNotFoundError: status.HTTP_404_NOT_FOUND,
    VVStateTransitionError: status.HTTP_409_CONFLICT,
    VVApproverRequiredError: status.HTTP_400_BAD_REQUEST,
    VVSoleCuratorError: status.HTTP_409_CONFLICT,
    VVDecisionReasonRequiredError: status.HTTP_400_BAD_REQUEST,
    VVPolicyError: status.HTTP_403_FORBIDDEN,
    VVEvidenceLockError: status.HTTP_409_CONFLICT,
    VVMigrationError: status.HTTP_400_BAD_REQUEST,
    VVConcurrentModificationError: status.HTTP_409_CONFLICT,
}


def install_exception_handlers(app: FastAPI) -> None:
    """Register handlers that map each domain exception to its HTTP status."""

    async def _handle(request: Request, exc: Exception) -> JSONResponse:
        del request
        http_status = _STATUS_MAP.get(type(exc))
        if http_status is None:
            for cls, code in _STATUS_MAP.items():
                if isinstance(exc, cls):
                    http_status = code
                    break
        if http_status is None:
            _log.exception("Unhandled exception in API handler")
            return _json("An unexpected error occurred. Please try again.", 500)
        # SyntaxValidationError carries a structured per-line errors
        # list so the in-browser editor can render the issues inline
        # (line / column / message). We tack the list onto the standard
        # ``{"detail": ...}`` shell as ``errors``.
        if isinstance(exc, SyntaxValidationError):
            return JSONResponse(
                status_code=http_status,
                content={"detail": str(exc), "path": exc.path, "errors": exc.issues},
            )
        # ``DuplicateToolInScenarioError`` carries the existing job's
        # id so the workspace can navigate the user there instead of
        # dead-ending on the 409. We tack it on as ``existing_job_id``
        # for the api_client to read back.
        if isinstance(exc, DuplicateToolInScenarioError):
            return JSONResponse(
                status_code=http_status,
                content={"detail": str(exc), "existing_job_id": exc.existing_job_id},
            )
        # ``ResultsWithheldError`` carries the gated state so the UI
        # has a machine-readable discriminator: Compare degrades a
        # withheld job to a "results withheld" cell instead of
        # aborting the whole comparison (§16.15).
        if isinstance(exc, ResultsWithheldError):
            return JSONResponse(
                status_code=http_status,
                content={"detail": str(exc), "results_state": exc.state},
            )
        return _json(str(exc), http_status)

    # Register one handler per umbrella class; FastAPI dispatches by
    # isinstance, so subclasses are caught automatically.
    for exc_cls in (
        HierarchyError,
        UserError,
        ToolError,
        RuntimeSettingsError,
        GeneralSettingsError,
        PathsAdminError,
        FavouritesError,
        MigrateError,
        BundleError,
        BundleSigningError,
        StudyGenerationError,
        StudyStateError,
        CohortError,
        CoverageError,
        OptimisationError,
        ModelError,
        ApprovalError,
        MaterialsError,
        LibraryError,
        LibraryStorageError,
        VVError,
        LicenceError,
    ):
        app.add_exception_handler(exc_cls, _handle)
