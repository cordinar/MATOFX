"""Custom materials database — versioned, curator-governed material data (ADR-0007).

A material is a reusable material card (a fluid, a metal, a polymer, …)
with immutable, ordinal-versioned scalar property sets, validated against
a per-class expected-property schema. Scope is **global,
curator-governed** — no personal/project tiering (a deliberate contrast
with the file library, ADR-0006). A new ``material`` descriptor input
type lets tools require a material of a given class; the chosen version's
properties are resolved + frozen into ``inputs.json`` at submit so
re-runs are reproducible.

Built in slices (design ``designs/materials-database.md`` §10):

- **Slice 1:** the data model — :class:`MaterialClass`,
  :class:`MaterialClassProperty`, :class:`Material`,
  :class:`MaterialVersion`, :class:`MaterialProperty`,
  :class:`JobMaterialUse` + the closed state / publish-state /
  data-pedigree vocabularies. Migration ``0048`` ships the six tables.
- **Slice 2 (this module set):** the :class:`MaterialsService` —
  class-schema admin, material/version/property CRUD with immutable
  versioning, schema validation, optimistic-locked metadata edits,
  retire/restore, the read side, and :meth:`resolve_for_submit` (freezes
  a chosen version into the ``inputs.json`` block). Curator-gated
  (``MATERIALS_CURATE``) with **direct publish**; the directed-approver
  review lands with the General-Settings + approvals-extension slices.

Later slices add the ``material`` descriptor input type, the
General-Settings change-approval toggle, the approvals extension, the
API, the submit wiring, the UI, and the ``material_properties``
example-tool re-port. ``MATERIALS_CURATE`` and the ``materials_curator``
capability role are seeded by ADR-0005's migration ``0044``.
"""

from __future__ import annotations

from matofx.materials.errors import (
    ApproverRequiredError,
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
from matofx.materials.orm import (
    DATA_PEDIGREES,
    MATERIAL_CLASS_STATES,
    MATERIAL_STATES,
    VERSION_PUBLISH_STATES,
    DataPedigree,
    JobMaterialUse,
    Material,
    MaterialClass,
    MaterialClassProperty,
    MaterialClassState,
    MaterialProperty,
    MaterialState,
    MaterialVersion,
    VersionPublishState,
)
from matofx.materials.service import (
    MaterialPickerEntry,
    MaterialsService,
    PropertySpec,
    make_materials_service,
)

__all__ = [
    "DATA_PEDIGREES",
    "MATERIAL_CLASS_STATES",
    "MATERIAL_STATES",
    "VERSION_PUBLISH_STATES",
    "ApproverRequiredError",
    "DataPedigree",
    "DuplicateClassPropertyError",
    "DuplicateMaterialClassError",
    "DuplicateMaterialPropertyError",
    "JobMaterialUse",
    "Material",
    "MaterialClass",
    "MaterialClassNotFoundError",
    "MaterialClassProperty",
    "MaterialClassPropertyNotFoundError",
    "MaterialClassRetiredError",
    "MaterialClassState",
    "MaterialConcurrentModificationError",
    "MaterialNotFoundError",
    "MaterialPickerEntry",
    "MaterialProperty",
    "MaterialRetiredError",
    "MaterialSchemaError",
    "MaterialState",
    "MaterialValidationError",
    "MaterialVersion",
    "MaterialVersionNotFoundError",
    "MaterialsError",
    "MaterialsService",
    "PropertySpec",
    "VersionPublishState",
    "make_materials_service",
]
