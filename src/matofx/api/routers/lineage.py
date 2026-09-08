"""HTTP API for the lineage / provenance graph (ADR-0010).

Two endpoints over :class:`matofx.lineage.LineageService`:

- ``GET /api/lineage/{kind}/{id}`` — build the graph centred on
  ``(kind, id)`` and return it as a :class:`LineageGraphResponse`.
- ``GET /api/lineage/{kind}/{id}/export?format=json|graphml`` —
  attachment download of the same graph in either format, for
  external tooling (Cytoscape Desktop / Gephi / d3-force / saved-to-disk
  audit evidence).

Both endpoints gate on ``lineage.view`` at the service layer; the
service's own :class:`LineageAccessDeniedError` translates to 403 and
its :class:`LineageNotFoundError` to 404 (existence-hidden — design
§4.3). The ``surface`` audit discriminator is set to ``"json"`` on
the live endpoint and ``"json"`` / ``"graphml"`` on the export
endpoint, so compliance can distinguish API calls from UI / report
paths.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.exceptions import HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from matofx.api.deps import get_current_user, get_db, get_lineage_service
from matofx.api.schemas import (
    LineageEdgeResponse,
    LineageGraphResponse,
    LineageNodeResponse,
    LineagePickerEntry,
)
from matofx.lineage import (
    LineageAccessDeniedError,
    LineageDepthOutOfRangeError,
    LineageGraph,
    LineageNotFoundError,
    LineageService,
    NodeKind,
    graph_to_graphml,
    graph_to_json,
    graph_to_svg,
)
from matofx.users.orm import User

__all__ = ["router"]

router = APIRouter()

# The closed set of node kinds accepted on the URL path. Reserved-for-
# later kinds (``optimisation_study`` / ``job_cohort`` /
# ``surrogate_model_version`` / ``vv_record``) are exposed on the type
# so links generated against future ADRs already validate; the service
# returns ``LineageNotFoundError`` until those resolvers ship.
_VALID_KINDS: frozenset[str] = frozenset(k.value for k in NodeKind)


def _coerce_kind(kind: str) -> NodeKind:
    """Validate the ``{kind}`` path segment against the closed set.

    Raises 422 (unprocessable entity) with a plain-English message
    when the value isn't one of :class:`NodeKind`.
    """
    if kind not in _VALID_KINDS:
        sorted_kinds = ", ".join(sorted(_VALID_KINDS))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"'{kind}' isn't a recognised lineage node kind. Expected one of: {sorted_kinds}."
            ),
        )
    return NodeKind(kind)


async def _build(
    service: LineageService,
    db: AsyncSession,
    *,
    kind: NodeKind,
    obj_id: int,
    actor_user_id: int,
    upstream: int,
    downstream: int,
    surface: str,
) -> LineageGraph:
    """Run :meth:`LineageService.build_graph`, mapping errors to HTTP.

    The error contract:

    - :class:`LineageAccessDeniedError` → 403 with the plain-English
      message the error class already carries.
    - :class:`LineageDepthOutOfRangeError` → 422 (depth gate).
    - :class:`LineageNotFoundError` → 404 (existence-hidden).
    """
    try:
        return await service.build_graph(
            db,
            kind=kind,
            obj_id=obj_id,
            actor_user_id=actor_user_id,
            upstream_depth=upstream,
            downstream_depth=downstream,
            surface=surface,
        )
    except LineageAccessDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except LineageDepthOutOfRangeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except LineageNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


def _graph_to_response(graph: LineageGraph) -> LineageGraphResponse:
    """Map a :class:`LineageGraph` to its Pydantic response shape.

    Direct field-by-field mapping rather than re-parsing the JSON
    dict — keeps the type-checker happy without ``cast()`` noise and
    avoids the round-trip cost. The JSON shape is unit-tested
    separately (see ``tests/lineage/test_serialise.py``); any drift
    between the two surfaces a test failure.
    """
    return LineageGraphResponse(
        centre=f"{graph.centre[0].value}:{graph.centre[1]}",
        nodes=[
            LineageNodeResponse(
                kind=n.kind.value,
                id=n.obj_id,
                label=n.label,
                state=n.state,
                archived=n.archived,
                deep_link=n.deep_link,
                payload=n.payload,
                placeholder=n.placeholder,
            )
            for n in graph.nodes
        ],
        edges=[
            LineageEdgeResponse(
                source=(e.source[0].value, e.source[1]),
                target=(e.target[0].value, e.target[1]),
                relation=e.relation.value,
                label=e.label,
                field_name=e.field_name,
                truncated_count=e.truncated_count,
            )
            for e in graph.edges
        ],
        hidden_by_acl=graph.hidden_by_acl,
        hidden_by_depth=graph.hidden_by_depth,
        soft_capped=graph.soft_capped,
    )


@router.get("/lineage/picker/{kind}", response_model=list[LineagePickerEntry])
async def list_centres(
    kind: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[LineageService, Depends(get_lineage_service)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[LineagePickerEntry]:
    """List visible centre options for the admin lineage picker.

    Per design follow-up — the admin standalone page's centre-picker
    needed an actual ID dropdown, not a free-text field. Each kind
    pulls from its owning domain service so the visibility rules
    track whatever the domain enforces (project ACL, library scope,
    tool-state gating, etc.).

    Reserved-for-later kinds (``optimisation_study`` / ``job_cohort``
    / ``surrogate_model_version`` / ``vv_record``) return an empty
    list — their owning service hasn't shipped yet.

    **Route-order note:** this endpoint must be declared *before*
    ``/lineage/{kind}/{obj_id}``. FastAPI matches routes in
    declaration order and would otherwise treat
    ``/lineage/picker/job`` as ``kind="picker", obj_id="job"`` and
    422 on the int-parsing of ``"job"``.
    """
    node_kind = _coerce_kind(kind)
    try:
        centres = await service.list_centres(
            db, kind=node_kind, actor_user_id=current_user.id, limit=limit
        )
    except LineageAccessDeniedError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return [LineagePickerEntry(id=c.obj_id, label=c.label, state=c.state) for c in centres]


@router.get("/lineage/{kind}/{obj_id}", response_model=LineageGraphResponse)
async def get_lineage(
    kind: str,
    obj_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[LineageService, Depends(get_lineage_service)],
    upstream: Annotated[int, Query(ge=0, le=20)] = 6,
    downstream: Annotated[int, Query(ge=0, le=20)] = 3,
) -> LineageGraphResponse:
    """Build + return the lineage graph centred on ``(kind, obj_id)``.

    Per-node ACL intersects with the caller's domain visibility
    (project ACL for job / scenario / project / study / workflow run;
    library scope for library file version; ``tools.see_*_state`` for
    tool version; etc.). Mid-walk hidden nodes drop from ``nodes``;
    their incident edge carries a bumped ``truncated_count`` and the
    response's ``hidden_by_acl`` counter increments.
    """
    node_kind = _coerce_kind(kind)
    graph = await _build(
        service,
        db,
        kind=node_kind,
        obj_id=obj_id,
        actor_user_id=current_user.id,
        upstream=upstream,
        downstream=downstream,
        surface="json",
    )
    return _graph_to_response(graph)


@router.get("/lineage/{kind}/{obj_id}/export")
async def export_lineage(
    kind: str,
    obj_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_current_user)],
    service: Annotated[LineageService, Depends(get_lineage_service)],
    format: Annotated[str, Query()] = "json",
    upstream: Annotated[int, Query(ge=0, le=20)] = 6,
    downstream: Annotated[int, Query(ge=0, le=20)] = 3,
) -> Response:
    """Attachment download of the same graph as an image, JSON, or GraphML.

    ``format=svg`` returns a self-contained SVG image
    (:func:`matofx.lineage.graph_to_svg`) — the same picture shown on
    screen, openable in any web browser and pasteable into documents.
    This is what the UI's **Download as Image** button uses.

    ``format=json`` returns the canonical JSON dict
    (:func:`matofx.lineage.graph_to_json`) for programmatic consumers.
    ``format=graphml`` returns a GraphML XML document for graph-analysis
    tools (Cytoscape Desktop / Gephi); it has no UI button but is kept
    for the rare power-user who wants it.

    All carry a ``Content-Disposition: attachment`` header so the browser
    saves rather than displays. Filename is ``lineage-{kind}-{id}.{ext}``
    — a stable, human-readable convention so compliance teams can collate
    per-target exports without renaming.
    """
    fmt = format.lower().strip()
    if fmt not in {"json", "graphml", "svg"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"'{format}' isn't a recognised export format. "
                "Expected one of: svg, json, graphml."
            ),
        )

    node_kind = _coerce_kind(kind)
    graph = await _build(
        service,
        db,
        kind=node_kind,
        obj_id=obj_id,
        actor_user_id=current_user.id,
        upstream=upstream,
        downstream=downstream,
        surface=fmt,
    )

    filename = f"lineage-{kind}-{obj_id}.{fmt}"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}

    if fmt == "svg":
        svg = graph_to_svg(graph)
        if svg is None:
            # graph_to_svg returns None above its node cap — too dense to
            # draw legibly. Steer the caller to the data export instead.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "This graph is too large to render as an image. "
                    "Download JSON instead."
                ),
            )
        return Response(
            content=svg,
            media_type="image/svg+xml",
            headers=headers,
        )
    if fmt == "graphml":
        return Response(
            content=graph_to_graphml(graph),
            media_type="application/graphml+xml",
            headers=headers,
        )
    # ``json`` — use the serialiser's dict shape verbatim. FastAPI's
    # JSONResponse path would re-encode anyway; we hand it through
    # ``Response`` so the attachment header lands and the content is
    # the canonical bytes a client would compare against.
    import json  # noqa: PLC0415

    return Response(
        content=json.dumps(graph_to_json(graph)),
        media_type="application/json",
        headers=headers,
    )
