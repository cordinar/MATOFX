"""Usage Statistics — read-only aggregation over the usage read model.

Public surface for the ``Admin → Usage Statistics`` page (``usage.view``).
See :mod:`matofx.usage.service` for the aggregation logic.
"""

from __future__ import annotations

from matofx.usage.digest import DigestResult, DigestService, make_digest_service
from matofx.usage.service import (
    EXECUTION_TARGETS,
    ApprovalKindFunnel,
    ComputeStats,
    DayValue,
    ExitCodeCount,
    FailingTool,
    FilterOptions,
    InfraStats,
    JobFilters,
    Kpi,
    LogonEventPoint,
    NamedCount,
    OverviewStats,
    PeopleStats,
    ReliabilityStats,
    RuntimeDist,
    SeatsStats,
    StatePoint,
    TargetUsage,
    TeamConsumption,
    TeamOption,
    ThroughputPoint,
    ToolConsumption,
    ToolCount,
    ToolOption,
    UsageService,
    make_usage_service,
)

__all__ = [
    "EXECUTION_TARGETS",
    "ApprovalKindFunnel",
    "ComputeStats",
    "DayValue",
    "DigestResult",
    "DigestService",
    "ExitCodeCount",
    "FailingTool",
    "FilterOptions",
    "InfraStats",
    "JobFilters",
    "Kpi",
    "LogonEventPoint",
    "NamedCount",
    "OverviewStats",
    "PeopleStats",
    "ReliabilityStats",
    "RuntimeDist",
    "SeatsStats",
    "StatePoint",
    "TargetUsage",
    "TeamConsumption",
    "TeamOption",
    "ThroughputPoint",
    "ToolConsumption",
    "ToolCount",
    "ToolOption",
    "UsageService",
    "make_digest_service",
    "make_usage_service",
]
