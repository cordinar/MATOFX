"""Register a surrogate model version as an installable tool (ADR-0008 §7.6 / §10.3).

``register_as_tool`` is the bridge from the synthesis core
(:mod:`matofx.models.tool_synthesis`) to the existing tool machinery: it
writes the generated orchestrator to a bundle directory, then calls
``ToolService.install`` with the synthesised descriptor and promotes the
result ``new → test``. The exported tool is a *normal* ``Tool`` from then
on — it inherits install / ACL / submit-form / workflow / audit wholesale
— so a super-user edits its safe surface and promotes ``test → released``
(which is the §6.3 gate-2 release for the exported-tool path).

The orchestrator references ``matofx.models.runtime.load_surrogate_model_version``
(the runner-subprocess loader) — that entry point + the
``Job.surrogate_model_version_id`` write at submit are the following
slice; this one lands the installed, test-state tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from matofx.audit.schema import RecordInput
from matofx.models.errors import ModelStateError
from matofx.models.tool_synthesis import (
    surrogate_tool_alias,
    synthesise_descriptor,
    synthesise_orchestrator,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.audit import AuditWriter
    from matofx.models.orm import SurrogateModel, SurrogateModelVersion
    from matofx.tools.orm import Tool
    from matofx.tools.service import ToolService

_ORCHESTRATOR_FILENAME = "surrogate_orchestrator.py"


async def _source_descriptor(session: AsyncSession, tool_alias: str) -> dict[str, Any]:
    """The live source ``Tool.descriptor``, or ``{}`` when it can't be read.

    A model outlives the tool it was fitted from, so an absent or malformed
    descriptor yields an empty dict and the export falls back to bare
    identifiers rather than failing.
    """
    from matofx.tools.orm import Tool  # noqa: PLC0415

    tool = (
        await session.execute(select(Tool).where(Tool.alias == tool_alias))
    ).scalar_one_or_none()
    if tool is None or not isinstance(tool.descriptor, dict):
        return {}
    return tool.descriptor


def _field_specs(descriptor: dict[str, Any], section: str) -> dict[str, dict[str, Any]]:
    """``{name: declaration}`` for one descriptor section, skipping junk rows."""
    out: dict[str, dict[str, Any]] = {}
    for field in descriptor.get(section, []) or []:
        if isinstance(field, dict) and field.get("name"):
            out[str(field["name"])] = field
    return out


def _labels_and_units(
    specs: dict[str, dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    """``({name: label}, {name: unit})`` for the declarations that carry them."""
    labels = {n: str(f["label"]) for n, f in specs.items() if f.get("label")}
    units = {n: str(f["unit"]) for n, f in specs.items() if f.get("unit")}
    return labels, units


async def register_as_tool(
    session: AsyncSession,
    *,
    tool_service: ToolService,
    audit: AuditWriter,
    model: SurrogateModel,
    version: SurrogateModelVersion,
    actor_user_id: int,
    models_root: Path | str,
    environment: str,
    block_extrapolation: bool = False,
    with_std: bool = True,
) -> Tool:
    """Synthesise + install the exported tool; promote it to ``test`` (§7.6).

    Writes the orchestrator under
    ``<models_root>/exported_tools/<env>/<alias>/`` and installs the
    derived descriptor. The new ``ToolVersion`` lands in ``test`` (not
    released) for the su/admin to edit its safe surface + promote to
    ``released`` — that promotion is the gate-2 release. ``install`` forces
    the tool ``version`` to its own initial value; the model linkage is
    carried by the **orchestrator** (baked version id + sha256), not the
    descriptor. Re-export of a newer model version (a new ``ToolVersion``
    of the same alias) is a follow-up; a first export of an
    already-exported model raises the tool layer's duplicate error.
    """
    from matofx.tools.orm import Tool  # noqa: PLC0415

    alias = surrogate_tool_alias(model.tool_alias, model.id)
    existing = (
        await session.execute(select(Tool.alias).where(Tool.alias == alias))
    ).scalar_one_or_none()
    if existing is not None:
        # Re-export of a refit (a new ToolVersion of the same alias) is a
        # follow-up (§7.6 carry-forward); a clear message beats the tool
        # layer's low-level duplicate error.
        raise ModelStateError(
            f"'{alias}' is already exported as a tool. Re-exporting a refit "
            "(a new tool version) isn't available yet."
        )
    # Carry the source tool's own declarations across. The label + unit of each
    # factor / response make the exported form read like the original (the
    # frozen version.factors/responses hold only identifiers + ranges); the
    # full input declarations carry the *rules* — integer-ness, precision,
    # declared min/max, widget, sum checks — so the exported form asks for a
    # valid design and not merely a numeric one. Best-effort: a field the
    # source descriptor no longer declares falls back to its name and no rules.
    # §7.6 — all of this is re-derived on every export.
    source = await _source_descriptor(session, model.tool_alias)
    source_inputs = _field_specs(source, "inputs")
    input_labels, input_units = _labels_and_units(source_inputs)
    output_labels, output_units = _labels_and_units(_field_specs(source, "outputs"))
    factors = [
        {
            **f,
            "label": input_labels.get(str(f.get("name")), str(f.get("name"))),
            "unit": f.get("unit") or input_units.get(str(f.get("name"))),
        }
        for f in (version.factors or [])
    ]
    from matofx.studies.surrogate import GP_KINDS  # noqa: PLC0415

    # Only GP-family fits produce a per-point uncertainty; for poly / gbm the
    # ``<response>_std`` output would never be filled, so it isn't declared. Since
    # ADR-0022 a version can mix methods, so this is decided per result (a
    # result recorded before then used the version's one method).
    primary = version.surrogate_method or "gp"
    responses = [
        {
            **r,
            "label": output_labels.get(str(r.get("name")), str(r.get("name"))),
            "unit": r.get("unit") or output_units.get(str(r.get("name"))),
            "has_std": str(r.get("method") or primary) in GP_KINDS,
        }
        for r in (version.responses or [])
    ]
    # Held-constant training inputs → the form's "held constant" group
    # (§7.6), labelled / unit'd from the source tool like the factors.
    constants = [
        {
            "name": cname,
            "value": cval,
            "label": input_labels.get(str(cname), str(cname)),
            "unit": input_units.get(str(cname)),
        }
        for cname, cval in (version.input_constants or {}).items()
    ]
    descriptor = synthesise_descriptor(
        source_tool_alias=model.tool_alias,
        model_id=model.id,
        version_id=version.id,
        name=model.name,
        description=model.description,
        factors=factors,
        responses=responses,
        constants=constants,
        version_ordinal=version.ordinal,
        block_extrapolation=block_extrapolation,
        with_std=with_std,
        # The stored coverage report (ADR-0020 §9) is what makes the exported
        # tool declare `support_sigma` / `support_status`. A version fitted
        # before coverage existed has none, so its export is unchanged — the
        # outputs arrive only on re-export, through the normal gate.
        coverage_report=version.coverage_report_json,
        source_inputs=source_inputs,
    )
    orchestrator = synthesise_orchestrator(
        version_id=version.id,
        bundle_path=version.bundle_path,
        bundle_sha256=version.bundle_sha256,
        tool_alias=alias,
    )
    bundle_dir = Path(models_root) / "exported_tools" / environment / alias
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / _ORCHESTRATOR_FILENAME).write_text(orchestrator, encoding="utf-8")

    tool = await tool_service.install(
        session,
        actor_user_id=actor_user_id,
        descriptor=descriptor,
        bundle_path=str(bundle_dir),
    )
    await tool_service.promote_state(
        session, actor_user_id=actor_user_id, alias=alias, new_state="test"
    )

    await audit.record(
        session,
        RecordInput(
            actor=f"user:{actor_user_id}",
            actor_type="user",
            action="models.exported_as_tool",
            resource_type="surrogate_model",
            resource_id=str(model.id),
            outcome="success",
            payload={
                "version_id": version.id,
                "tool_alias": alias,
                "block_extrapolation": block_extrapolation,
            },
        ),
    )
    return tool
