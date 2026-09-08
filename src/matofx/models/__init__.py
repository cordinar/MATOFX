"""Surrogate Model registry (ADR-0008 §5/§6).

The governed store a fitted surrogate is persisted into: a logical
:class:`SurrogateModel` entry + append-only, sha256-verified
:class:`SurrogateModelVersion` bundles, mirroring the file-library /
materials registries layer-for-layer.

Phase 5a ships the data model + :class:`ModelService` core (fit + persist
+ bundle integrity + read + retire + refit) for personal / project scope.
The global two-gate release flow, scope transitions, surrogate-as-tool
synthesis, and the API/UI land in following slices.
"""

from __future__ import annotations

from matofx.models.errors import (
    ModelBundleCorruptError,
    ModelConfigError,
    ModelError,
    ModelStateError,
)
from matofx.models.exporter import register_as_tool
from matofx.models.orm import (
    MODEL_SCOPES,
    MODEL_STATES,
    PUBLISH_STATES,
    VERSION_KINDS,
    ModelScope,
    ModelState,
    PublishState,
    SurrogateModel,
    SurrogateModelVersion,
    VersionKind,
)
from matofx.models.runtime import load_surrogate_model_version
from matofx.models.service import (
    ModelRuntime,
    ModelService,
    make_model_service,
)
from matofx.models.tool_synthesis import (
    CoverageClass,
    classify_coverage,
    surrogate_tool_alias,
    synthesise_descriptor,
    synthesise_orchestrator,
    worst_coverage,
)

__all__ = [
    "MODEL_SCOPES",
    "MODEL_STATES",
    "PUBLISH_STATES",
    "VERSION_KINDS",
    "CoverageClass",
    "ModelBundleCorruptError",
    "ModelConfigError",
    "ModelError",
    "ModelRuntime",
    "ModelScope",
    "ModelService",
    "ModelState",
    "ModelStateError",
    "PublishState",
    "SurrogateModel",
    "SurrogateModelVersion",
    "VersionKind",
    "classify_coverage",
    "load_surrogate_model_version",
    "make_model_service",
    "register_as_tool",
    "surrogate_tool_alias",
    "synthesise_descriptor",
    "synthesise_orchestrator",
    "worst_coverage",
]
