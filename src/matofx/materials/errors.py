"""User-facing errors for the materials service (ADR-0007).

Plain English; safe to render directly to the end user. Shape parallels
:mod:`matofx.library.errors` / :mod:`matofx.hierarchy.errors` so the API
exception handler can register one umbrella handler
(:class:`MaterialsError`) and treat the domain uniformly. The
curator-permission gate reuses
:class:`matofx.users.errors.PermissionDeniedError` (already mapped to
403), so there's no materials-specific access error here.
"""

from __future__ import annotations


class MaterialsError(Exception):
    """Base class for materials domain errors."""


class MaterialNotFoundError(MaterialsError):
    """No material with that id (or it's been retired out of view)."""

    def __init__(self, material_id: int) -> None:
        """Construct with the missing material id."""
        super().__init__("We couldn't find that material.")
        self.material_id = material_id


class MaterialVersionNotFoundError(MaterialsError):
    """No material version with that id."""

    def __init__(self, version_id: int) -> None:
        """Construct with the missing version id."""
        super().__init__("We couldn't find that material version.")
        self.version_id = version_id


class MaterialClassNotFoundError(MaterialsError):
    """No material class with that id or name."""

    def __init__(self, identifier: int | str) -> None:
        """Construct with the missing class id or name."""
        super().__init__("We couldn't find that material class.")
        self.identifier = identifier


class MaterialClassPropertyNotFoundError(MaterialsError):
    """No expected-property row with that id on this class."""

    def __init__(self, property_id: int) -> None:
        """Construct with the missing property row id."""
        super().__init__("We couldn't find that material-class property.")
        self.property_id = property_id


class MaterialValidationError(MaterialsError):
    """A material/class field failed a basic validation check.

    Covers a missing name or provenance note. The message is already
    user-facing.
    """

    def __init__(self, message: str) -> None:
        """Construct with an already-user-facing message."""
        super().__init__(message)


class MaterialSchemaError(MaterialsError):
    """A material's properties don't satisfy its class's expected schema.

    Raised at create / add-version time and at
    :meth:`MaterialsService.resolve_for_submit`: a ``required`` class
    property is missing, or a property's unit doesn't match the unit the
    class declares for it. The message names the offending property.
    """

    def __init__(self, message: str) -> None:
        """Construct with an already-user-facing message."""
        super().__init__(message)


class DuplicateMaterialClassError(MaterialsError):
    """A material class with this name already exists."""

    def __init__(self, name: str) -> None:
        """Construct with the conflicting class name."""
        super().__init__(
            f"A material class called {name!r} already exists. Choose a different name."
        )
        self.name = name


class DuplicateClassPropertyError(MaterialsError):
    """This class already declares an expected property with this name."""

    def __init__(self, prop_name: str) -> None:
        """Construct with the conflicting property name."""
        super().__init__(
            f"This class already expects a property called {prop_name!r}. "
            "Edit the existing one instead of adding it again."
        )
        self.prop_name = prop_name


class DuplicateMaterialPropertyError(MaterialsError):
    """A material version was given the same property twice."""

    def __init__(self, prop_name: str) -> None:
        """Construct with the duplicated property name."""
        super().__init__(f"Property {prop_name!r} was given more than once for this material.")
        self.prop_name = prop_name


class MaterialRetiredError(MaterialsError):
    """A mutation was attempted on a retired material.

    Retired materials are read-only; restore it first to edit it.
    """

    def __init__(self, material_id: int) -> None:
        """Construct with the retired material id."""
        super().__init__(
            "This material has been retired and can't be changed. Restore it first to edit it."
        )
        self.material_id = material_id


class MaterialClassRetiredError(MaterialsError):
    """A new material was created against a retired class.

    A retired class is kept for existing materials but can't back new
    ones (design §11).
    """

    def __init__(self, name: str) -> None:
        """Construct with the retired class name."""
        super().__init__(
            f"The material class {name!r} has been retired, so new materials can't "
            "be created under it. Pick an active class."
        )
        self.name = name


class ApproverRequiredError(MaterialsError):
    """A material change needs approval but no approver was chosen.

    With the ``materials_change_approval`` toggle ON, creating a material
    or adding a version requires the author to name one curator to review
    it (ADR-0007 §5.3 — submit is blocked until an approver is chosen).
    Self-approval is rejected separately by the approvals service
    (:class:`matofx.approvals.errors.SelfApprovalNotAllowedError`).
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "This material change needs a reviewer. Choose one materials curator "
            "(other than yourself) to approve it."
        )


class MaterialConcurrentModificationError(MaterialsError):
    """Another writer changed this material since the caller loaded it.

    Raised when the optimistic-lock ``version`` on the row has moved on.
    Reload and re-apply the change.
    """

    def __init__(self) -> None:
        """Construct with the canonical user-facing message."""
        super().__init__(
            "This material was changed by someone else while you were editing. "
            "Please reload and try again."
        )
