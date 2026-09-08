"""Governed file library — reusable, versioned tool-input files (ADR-0006).

A library file is a curated tool-input file (geometry / mesh /
boundary-condition CSV, …) with immutable, content-hashed versions,
organised on two independent axes — tiered **scope** (personal →
project → global) and **tool assignment** (tool → field → file,
multi-tool). It adds a 4th "Pick from library" file-source mode to tool
forms, reusing the existing upload-promotion path so the runner contract
is unchanged.

Built in slices (design ``designs/file-library.md`` §10):

- **Slice 1:** the data model — :class:`LibraryFile`,
  :class:`LibraryFileVersion`, :class:`LibraryFileTool`,
  :class:`JobLibraryFileUse` + the closed scope / state / publish-state
  vocabularies. Migration ``0045`` ships the four tables.
- **Slice 2 (this module set):** the :class:`LibraryService` —
  CRUD + immutable versioning + scope/tool governance + the
  directed-approver publish gate (ADR-0006 §5, reusing
  :mod:`matofx.approvals`) + the §3 picker query + access-checked
  byte reads. On-disk bytes live under the active ``StorageLocation``
  mount via :mod:`matofx.library.storage`.

Later slices add the General-Settings publish-approval toggle, the API,
the form integration, and the UI. ``LIBRARY_CURATE`` and the
``library_curator`` capability role are seeded by ADR-0005's migration
``0044``.
"""

from __future__ import annotations

from matofx.library.errors import (
    ApproverRequiredError,
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
from matofx.library.orm import (
    LIBRARY_FILE_STATES,
    LIBRARY_SCOPES,
    VERSION_PUBLISH_STATES,
    JobLibraryFileUse,
    LibraryFile,
    LibraryFileState,
    LibraryFileTool,
    LibraryFileVersion,
    LibraryScope,
    VersionPublishState,
)
from matofx.library.service import (
    LibraryService,
    PickerCandidate,
    ToolAssignmentSpec,
    make_library_service,
)
from matofx.library.storage import (
    LibraryStorageError,
    StoredVersion,
)

__all__ = [
    "LIBRARY_FILE_STATES",
    "LIBRARY_SCOPES",
    "VERSION_PUBLISH_STATES",
    "ApproverRequiredError",
    "DuplicateToolAssignmentError",
    "GlobalPublishNotPermittedError",
    "InvalidScopeError",
    "JobLibraryFileUse",
    "LibraryAccessDeniedError",
    "LibraryConcurrentModificationError",
    "LibraryError",
    "LibraryFile",
    "LibraryFileNotFoundError",
    "LibraryFileRetiredError",
    "LibraryFileState",
    "LibraryFileTool",
    "LibraryFileVersion",
    "LibraryScope",
    "LibraryService",
    "LibraryStorageError",
    "LibraryValidationError",
    "LibraryVersionNotFoundError",
    "PickerCandidate",
    "ProjectScopeError",
    "StoredVersion",
    "ToolAssignmentNotFoundError",
    "ToolAssignmentRequiredError",
    "ToolAssignmentSpec",
    "VersionPublishState",
    "make_library_service",
]
