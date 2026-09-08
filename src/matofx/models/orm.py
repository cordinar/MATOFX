"""SQLAlchemy ORM for the surrogate Model registry (ADR-0008 §5.2/§5.3).

Two tables mirror the file-library / materials registries layer-for-layer
(ADR-0006 / 0007):

- :class:`SurrogateModel` — the logical entry with *mutable* metadata
  (name, tags, scope, steward, lifecycle state, published-version
  pointer). One ``tool_alias`` per model (multi-tool is v1.x).
- :class:`SurrogateModelVersion` — the *immutable* per-fit record. A
  refit appends a new version (ordinal 1, 2, 3…); the published version
  never mutates. The on-disk ``bundle_path`` + ``bundle_sha256`` are the
  governed artefact; the sha is verified on every load (non-negotiable
  #2).

``published_version_id`` and ``Job.surrogate_model_version_id`` are
*logical* pointers (no DB FK — see migration 0061); their integrity is
service-enforced.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata

ModelScope = Literal["personal", "project", "global"]
"""Scope tier (ADR-0008 §6.1). ``personal`` → owner only; ``project`` →
project Viewer+; ``global`` → all authenticated users."""

MODEL_SCOPES: tuple[ModelScope, ...] = ("personal", "project", "global")

ModelState = Literal["active", "pending", "retired"]
"""Model lifecycle. ``pending`` = a brand-new global model awaiting its
first publish gate; ``retired`` = withdrawn (no hard delete, D20)."""

MODEL_STATES: tuple[ModelState, ...] = ("active", "pending", "retired")

VersionKind = Literal["scalar", "field", "combined"]
VERSION_KINDS: tuple[VersionKind, ...] = ("scalar", "field", "combined")
"""``combined`` (ADR-0018) holds N scalar surrogates + M field surrogates in one
version — predicts numbers and fields together, exportable as one tool."""

PublishState = Literal["draft", "in_review", "checked", "published", "rejected"]
"""Version publish lifecycle (ADR-0008 §6.2). Personal / project versions
publish directly; global runs ``draft → in_review → checked → published``
(the two-gate flow, governance slice)."""

PUBLISH_STATES: tuple[PublishState, ...] = (
    "draft",
    "in_review",
    "checked",
    "published",
    "rejected",
)


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class SurrogateModel(_Base):
    """One logical surrogate model entry (mutable metadata, §5.2)."""

    __tablename__ = "surrogate_model"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(sa.JSON, nullable=False, default=list)

    tool_alias: Mapped[str] = mapped_column(
        sa.String(64),
        sa.ForeignKey("tool.alias", ondelete="RESTRICT"),
        nullable=False,
    )
    """The single tool this model predicts for."""

    scope: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="personal")
    project_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("project.id", ondelete="RESTRICT"),
        nullable=True,
    )
    """Set **iff** ``scope='project'`` (CHECK-enforced)."""

    owner_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    state: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="active")

    published_version_id: Mapped[int | None] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        nullable=True,
    )
    """Logical pointer to the default-pickable version (no DB FK — the
    model ↔ version reference is mutual; migration 0061)."""

    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    created_by_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
    __table_args__: Any = (
        sa.CheckConstraint(
            f"scope IN ({','.join(repr(s) for s in MODEL_SCOPES)})",
            name="scope",
        ),
        sa.CheckConstraint(
            f"state IN ({','.join(repr(s) for s in MODEL_STATES)})",
            name="state",
        ),
        sa.CheckConstraint(
            "(scope = 'project') = (project_id IS NOT NULL)",
            name="project_iff_scope",
        ),
        sa.Index("ix_surrogate_model_tool_alias_scope", "tool_alias", "scope"),
    )


class SurrogateModelVersion(_Base):
    """One immutable fit of a surrogate model (§5.3).

    Append-only — a refit mints a new ordinal; the published version never
    mutates. ``bundle_sha256`` is verified on every load, so a corrupt
    bundle raises rather than returning bad predictions (non-negotiable #2).
    """

    __tablename__ = "surrogate_model_version"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    surrogate_model_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("surrogate_model.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    """1, 2, 3… per model — UNIQUE ``(surrogate_model_id, ordinal)``."""

    kind: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="scalar")
    factors: Mapped[list[dict[str, Any]]] = mapped_column(sa.JSON, nullable=False, default=list)
    """``[{name, type, unit, min, max}]`` frozen from the descriptor at fit time."""

    responses: Mapped[list[dict[str, Any]]] = mapped_column(sa.JSON, nullable=False, default=list)
    """``[{name, kind, unit, ...}]`` — per-response specs. A ``scalar`` version
    lists scalar responses, a ``field`` version one field-output spec, and a
    ``combined`` version (ADR-0018) lists both kinds together."""

    source_kind: Mapped[str] = mapped_column(sa.String(16), nullable=False)
    source_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False
    )
    tool_version_ids_at_fit: Mapped[list[int]] = mapped_column(
        sa.JSON, nullable=False, default=list
    )
    n_training_points: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)

    bundle_path: Mapped[str] = mapped_column(sa.String(1024), nullable=False)
    bundle_sha256: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    quality: Mapped[dict[str, Any]] = mapped_column(sa.JSON, nullable=False, default=dict)
    # Inputs held constant across every training run (ADR-0008 §7.6) —
    # ``{name: value}``. Added in migration 0064, so nullable for rows
    # fitted before it; read through ``... or {}``.
    input_constants: Mapped[dict[str, Any] | None] = mapped_column(
        sa.JSON, nullable=True, default=dict
    )
    # The fitting strategy this version used (``gp`` / ``gp_matern`` / ``poly``
    # / ``gbm``, ADR-0008 §4). Added in migration 0065; nullable for older
    # rows → treated as ``gp``. Drives the exporter's ± gate (only GP kinds
    # yield uncertainty) and the detail page's method label.
    surrogate_method: Mapped[str | None] = mapped_column(sa.String(16), nullable=True, default="gp")
    keep_training_data: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    publish_state: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="draft")
    notes: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")

    coverage_report_json: Mapped[dict[str, Any] | None] = mapped_column(
        sa.JSON, nullable=True, default=None
    )
    """The design-space coverage report for this fit (ADR-0020 §9, migration 0095).

    The whole structured report — verdict, criterion, declared envelope,
    per-factor breakdown, diagnostics, per-response results — so the model
    record, the per-job report and the V&V trail read one answer rather than
    each recomputing it and drifting apart. The **envelope lives here and
    nowhere else**: the predict-time guard (§8) and the V&V applicable range
    (§10) both read this copy.

    ``None`` means *not assessed*, **never** "good": every version fitted
    before this feature existed has NULL, and rendering that as a clean bill
    of health would invert its meaning. Written once at fit and never amended
    — a refit mints a new version rather than mutating this row.
    """

    coverage_verdict: Mapped[str | None] = mapped_column(
        sa.String(16), nullable=True, default=None, index=True
    )
    """``good`` / ``thin`` / ``poor`` / ``insufficient``, or ``None``.

    One field of :attr:`coverage_report_json`, duplicated as a short indexed
    string purely so a catalogue can filter and sort on it without parsing
    every report on every row.
    """
    fitted_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    fitted_by_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )

    __table_args__: Any = (
        sa.UniqueConstraint(
            "surrogate_model_id", "ordinal", name="uq_surrogate_model_version_model_ordinal"
        ),
        sa.CheckConstraint(
            f"kind IN ({','.join(repr(k) for k in VERSION_KINDS)})",
            name="kind",
        ),
        sa.CheckConstraint(
            f"publish_state IN ({','.join(repr(p) for p in PUBLISH_STATES)})",
            name="publish_state",
        ),
    )
