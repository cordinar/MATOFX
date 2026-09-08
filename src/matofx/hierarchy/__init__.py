"""Project → Scenario → Job hierarchy.

Public surface:

- :class:`Project`, :class:`ProjectAcl`, :class:`Scenario`,
  :class:`Job` — core ORM models.
- :class:`ProjectMetadataField`, :class:`ProjectMetadataValue`,
  :class:`ProjectMetadata` — admin-configurable typed metadata.
- :class:`ProjectAccessLevel` — closed enum for the resolved access
  tier; :data:`PROJECT_ACL_ROLES` — the stored ACL role vocabulary.
- :class:`ProjectService`, :class:`ScenarioService`, :class:`JobService`
  — CRUD with audited mutations and per-user ACL access checks.
- :data:`JOB_STATES` / :data:`PROJECT_STATES` — closed state vocab.
- :class:`HierarchyError` and subclasses — plain-English errors safe
  for UI rendering.

Per ADR-0002 the access model is a per-user four-role ACL (immutable
Creator + Owner/Member/Viewer) with the §D20 no-delete ``state``
lifecycle and typed metadata. Phase 1's team-keyed ``project_team``
ACL is superseded; teams are a bulk-pick UX convenience only. Clone
and per-scenario ACL tightening remain v1.x.
"""

from __future__ import annotations

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
    MetadataError,
    NotFoundError,
    UserAlreadyGrantedError,
    UserNotGrantedError,
)
from matofx.hierarchy.orm import (
    ACL_ROLE_TO_LEVEL,
    JOB_STATES,
    NUMBER_FORMATS,
    PROJECT_ACL_ROLES,
    PROJECT_METADATA_FIELD_TYPES,
    PROJECT_STATES,
    Job,
    JobState,
    Project,
    ProjectAccessLevel,
    ProjectAcl,
    ProjectMetadata,
    ProjectMetadataField,
    ProjectMetadataValue,
    ProjectState,
    Scenario,
)
from matofx.hierarchy.service import (
    JobService,
    ProjectService,
    ScenarioService,
    make_job_service,
    make_project_service,
    make_scenario_service,
)

__all__ = [
    "ACL_ROLE_TO_LEVEL",
    "JOB_STATES",
    "NUMBER_FORMATS",
    "PROJECT_ACL_ROLES",
    "PROJECT_METADATA_FIELD_TYPES",
    "PROJECT_STATES",
    "AccessDeniedError",
    "ConcurrentModificationError",
    "CreatorImmutableError",
    "DuplicateNameError",
    "DuplicateToolInScenarioError",
    "HierarchyError",
    "InvalidJobStateError",
    "InvalidRoleError",
    "InvalidStateError",
    "Job",
    "JobAlreadyTerminalError",
    "JobService",
    "JobState",
    "MetadataError",
    "NotFoundError",
    "Project",
    "ProjectAccessLevel",
    "ProjectAcl",
    "ProjectMetadata",
    "ProjectMetadataField",
    "ProjectMetadataValue",
    "ProjectService",
    "ProjectState",
    "Scenario",
    "ScenarioService",
    "UserAlreadyGrantedError",
    "UserNotGrantedError",
    "make_job_service",
    "make_project_service",
    "make_scenario_service",
]
