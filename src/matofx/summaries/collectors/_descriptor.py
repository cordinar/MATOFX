"""Shared descriptor label/unit resolution for study collectors.

Maps a tool version's internal input/output names to their descriptor
**labels** + units (``feedback_use_labels_not_names``). Returns empty maps when
the version is unknown, so collectors degrade to raw names rather than failing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.tools.orm import ToolVersion


@dataclass(frozen=True)
class DescriptorMaps:
    """Name → label / unit maps for one tool version's inputs + outputs."""

    input_labels: dict[str, str]
    input_units: dict[str, str]
    output_labels: dict[str, str]
    output_units: dict[str, str]
    output_order: list[str]

    def input_label(self, name: str) -> str:
        return self.input_labels.get(name, name)

    def output_label(self, name: str) -> str:
        return self.output_labels.get(name, name)

    def output_unit(self, name: str) -> str | None:
        return self.output_units.get(name) or None

    def input_unit(self, name: str) -> str | None:
        return self.input_units.get(name) or None


_EMPTY = DescriptorMaps({}, {}, {}, {}, [])


async def descriptor_maps(session: AsyncSession, tool_version_id: int | None) -> DescriptorMaps:
    """Load the label/unit maps for a tool version (empty when unknown)."""
    if tool_version_id is None:
        return _EMPTY
    version = await session.get(ToolVersion, tool_version_id)
    if version is None:
        return _EMPTY
    descriptor = dict(version.descriptor_snapshot or {})
    in_labels, in_units, _ = _field_maps(descriptor.get("inputs"))
    out_labels, out_units, out_order = _field_maps(descriptor.get("outputs"))
    return DescriptorMaps(in_labels, in_units, out_labels, out_units, out_order)


async def workflow_descriptor_maps(
    session: AsyncSession, workflow_alias: str | None
) -> DescriptorMaps:
    """Member-qualified label/unit maps for a workflow target's member tools.

    Keys are ``member.field`` (matching the study's member-qualified factor /
    response names) and labels read ``Tool name · field label`` so a workflow
    summary shows labels, not aliases (``feedback_use_labels_not_names``).
    Empty when the workflow / its members can't be resolved.
    """
    if not workflow_alias:
        return _EMPTY
    from matofx.workflows.orm import Workflow, WorkflowMember  # noqa: PLC0415

    workflow = (
        await session.execute(select(Workflow).where(Workflow.alias == workflow_alias))
    ).scalar_one_or_none()
    if workflow is None:
        return _EMPTY
    members = (
        (
            await session.execute(
                select(WorkflowMember)
                .where(WorkflowMember.workflow_id == workflow.id)
                .order_by(WorkflowMember.position)
            )
        )
        .scalars()
        .all()
    )
    in_labels: dict[str, str] = {}
    in_units: dict[str, str] = {}
    out_labels: dict[str, str] = {}
    out_units: dict[str, str] = {}
    out_order: list[str] = []
    for member in members:
        alias = member.tool_alias
        version = (
            await session.execute(
                select(ToolVersion)
                .where(ToolVersion.alias == alias)
                .order_by(ToolVersion.created_at.desc(), ToolVersion.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if version is None:
            continue
        descriptor = dict(version.descriptor_snapshot or {})
        member_name = str(descriptor.get("name") or alias)
        il, iu, _ = _field_maps(descriptor.get("inputs"))
        ol, ou, oo = _field_maps(descriptor.get("outputs"))
        for name, label in il.items():
            in_labels[f"{alias}.{name}"] = f"{member_name} · {label}"
        for name, unit in iu.items():
            in_units[f"{alias}.{name}"] = unit
        for name, label in ol.items():
            out_labels[f"{alias}.{name}"] = f"{member_name} · {label}"
        for name, unit in ou.items():
            out_units[f"{alias}.{name}"] = unit
        out_order.extend(f"{alias}.{n}" for n in oo)
    return DescriptorMaps(in_labels, in_units, out_labels, out_units, out_order)


def _field_maps(fields: Any) -> tuple[dict[str, str], dict[str, str], list[str]]:
    labels: dict[str, str] = {}
    units: dict[str, str] = {}
    order: list[str] = []
    if not isinstance(fields, list):
        return labels, units, order
    for field in fields:
        if not isinstance(field, dict):
            continue
        name = field.get("name")
        if not isinstance(name, str):
            continue
        order.append(name)
        labels[name] = str(field.get("label") or name)
        unit = field.get("unit")
        if unit:
            units[name] = str(unit)
    return labels, units, order
