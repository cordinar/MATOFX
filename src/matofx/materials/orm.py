"""SQLAlchemy ORM models for the custom materials database (ADR-0007).

A **material** is a reusable, curator-governed material card (a fluid, a
metal, a polymer, …) with immutable, ordinal-versioned property sets.
Per ``designs/materials-database.md`` the model is **global,
curator-governed** — there is no personal/project tiering (a deliberate
contrast with the file library, ADR-0006).

Six tables, in two groups:

*The class schema* — what a material of a given class is expected to
expose:

- :class:`MaterialClass` — a class name (Fluid / Metal / Polymer / …).
- :class:`MaterialClassProperty` — the expected ``(prop_name, unit,
  required)`` rows for a class. A tool can require "a Fluid" and the
  system checks the chosen material exposes the required props in the
  right units.

*The material cards* — the data itself:

- :class:`Material` — the logical card (mutable metadata, optimistic
  lock, current state). No hard delete — ``state='retired'``.
- :class:`MaterialVersion` — append-only immutable versions; each
  carries provenance + a ``publish_state``.
- :class:`MaterialProperty` — per-version scalar properties
  (name / value / unit, + optional source/uncertainty/notes).
- :class:`JobMaterialUse` — per-field provenance written at submit
  ("which material version fed which field of which job"). It FKs *to*
  ``job`` (the runner maps no new FK *from* ``job``), so no
  ``runner/supervisor.py`` side-effect import is needed.

Curves (temperature/condition-dependent properties) are phase-2;
:class:`MaterialProperty` is scalar now (``designs/materials-database.md``
§12).
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Literal

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from matofx.db.metadata import metadata as shared_metadata

MaterialClassState = Literal["active", "retired"]
"""Closed vocabulary for ``material_class.state``.

- ``active`` — usable for new materials.
- ``retired`` — kept for existing materials, unavailable for new ones;
  never hard-deleted.
"""

MATERIAL_CLASS_STATES: tuple[MaterialClassState, ...] = ("active", "retired")

MaterialState = Literal["active", "pending", "retired"]
"""Closed vocabulary for ``material.state`` (design §2.2).

- ``active`` — normal, pickable (subject to version publish state).
- ``pending`` — a brand-new material awaiting approval (§5); not
  pickable by regular users until approved.
- ``retired`` — soft-deleted; never hard-deleted, reversible by state.
"""

MATERIAL_STATES: tuple[MaterialState, ...] = ("active", "pending", "retired")

VersionPublishState = Literal["draft", "pending", "published", "rejected"]
"""Closed vocabulary for ``material_version.publish_state`` (design §2.3).

Mirrors the file library's version lifecycle (ADR-0006): a material
change is reviewed *before* it becomes pickable.

- ``draft`` — created, not yet published.
- ``pending`` — awaiting a directed approver's decision (§5).
- ``published`` — selectable in tool forms.
- ``rejected`` — the approval was declined; the prior published version
  (if any) remains the usable one.
"""

VERSION_PUBLISH_STATES: tuple[VersionPublishState, ...] = (
    "draft",
    "pending",
    "published",
    "rejected",
)

DataPedigree = Literal["measured", "typical", "estimated"]
"""Closed vocabulary for ``material_version.data_pedigree`` (optional).

Statistical allowables (A/B-basis) beyond this free-ish pedigree marker
are v1.x (design §12).
"""

DATA_PEDIGREES: tuple[DataPedigree, ...] = ("measured", "typical", "estimated")


class _Base(DeclarativeBase):
    metadata = shared_metadata


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


class MaterialClass(_Base):
    """A material class (Fluid / Metal / Polymer / …) — design §2.1.

    ``name`` is unique. ``state`` is CHECK-constrained from
    :data:`MATERIAL_CLASS_STATES`; a class is retired, never deleted, so
    materials that reference it keep resolving.
    """

    __tablename__ = "material_class"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    name: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    state: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="active")

    __table_args__: Any = (
        sa.CheckConstraint(
            f"state IN ({','.join(repr(s) for s in MATERIAL_CLASS_STATES)})",
            name="ck_material_class_state",
        ),
    )


class MaterialClassProperty(_Base):
    """An expected property of a material class (design §2.1).

    Carries two name fields:

    - ``slug`` is the **immutable snake_case identifier** set at creation
      (e.g. ``thermal_conductivity``). UNIQUE ``(class_id, slug)``.
      Every tool descriptor's ``required_properties`` references the
      slug; the frozen block in ``inputs.json`` keys properties by the
      slug too. Stable across renames so the wire format never breaks.
    - ``prop_name`` is the editable **display label** (e.g.
      ``"Thermal Conductivity"``). Curators can rename it to fix typos
      or adjust wording; the slug stays put. UNIQUE ``(class_id, prop_name)``
      so two properties can't share the same display label either.
    """

    __tablename__ = "material_class_property"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    class_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("material_class.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    slug: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    prop_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    unit: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    required: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=True)

    __table_args__: Any = (
        sa.UniqueConstraint("class_id", "slug", name="uq_material_class_property_slug"),
        sa.UniqueConstraint("class_id", "prop_name", name="uq_material_class_property"),
    )


class Material(_Base):
    """The logical material card — mutable metadata + current state.

    ``version`` is the optimistic-lock counter (``version_id_col``),
    matching ``LibraryFile`` / ``Project`` / ``ParametricStudy``.
    ``name`` is intentionally **not unique** — different grades / sources
    may share a designation; callers pin by id (design §2.2).
    """

    __tablename__ = "material"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    name: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(sa.JSON, nullable=False, default=list)
    """Trade / common names (optional)."""

    class_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("material_class.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    description: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    tags: Mapped[list[str]] = mapped_column(sa.JSON, nullable=False, default=list)
    steward_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    """The steward; defaults to the creator."""

    confidentiality: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    """e.g. ``ITAR`` / ``proprietary`` (optional, display-only)."""

    state: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="active")
    created_by_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __mapper_args__ = {"version_id_col": version}  # noqa: RUF012
    __table_args__: Any = (
        sa.CheckConstraint(
            f"state IN ({','.join(repr(s) for s in MATERIAL_STATES)})",
            name="ck_material_state",
        ),
    )


class MaterialVersion(_Base):
    """One immutable version of a material (design §2.3).

    Append-only: an edit mints a new version with ``ordinal + 1`` rather
    than mutating an existing row. ``provenance`` (the data source) is
    required at the service layer; the rest are optional context.
    """

    __tablename__ = "material_version"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    material_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("material.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    ordinal: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    """1, 2, 3 … per material; UNIQUE with ``material_id``."""

    provenance: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    """Data source: test report / datasheet / supplier / literature /
    handbook / simulation (required at the service layer)."""

    reference: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    """Document no., DOI, standard, datasheet id (optional)."""

    data_pedigree: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    """One of :data:`DATA_PEDIGREES`, or NULL (optional)."""

    conditions: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    """Context note; full curves are phase-2 (optional)."""

    version_notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    effective_date: Mapped[_dt.date | None] = mapped_column(sa.Date, nullable=True)
    publish_state: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="draft")
    created_by_user_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("user.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[_dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__: Any = (
        sa.UniqueConstraint("material_id", "ordinal", name="uq_material_version_ordinal"),
        sa.CheckConstraint(
            f"publish_state IN ({','.join(repr(s) for s in VERSION_PUBLISH_STATES)})",
            name="ck_material_version_publish_state",
        ),
        sa.CheckConstraint(
            "data_pedigree IS NULL OR data_pedigree IN "
            f"({','.join(repr(p) for p in DATA_PEDIGREES)})",
            name="ck_material_version_data_pedigree",
        ),
    )


class MaterialProperty(_Base):
    """One scalar property of a material version (design §2.4).

    ``value`` is a scalar (curves are phase-2). Validation against the
    class schema happens at the service layer: every ``required`` class
    property must be present with a matching unit; extra properties are
    allowed.

    Carries the same ``(slug, prop_name)`` pair as
    :class:`MaterialClassProperty` — ``slug`` is the immutable
    snake_case key the frozen block in ``inputs.json`` indexes by;
    ``prop_name`` is the editable display label snapshotted at version
    creation. UNIQUE ``(material_version_id, slug)`` *and*
    ``(material_version_id, prop_name)``.
    """

    __tablename__ = "material_property"

    id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    material_version_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("material_version.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slug: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    prop_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    value: Mapped[float] = mapped_column(sa.Numeric, nullable=False)
    unit: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    source: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    """The property may cite its own source (optional)."""

    uncertainty: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    """Tolerance / ± (optional)."""

    notes: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    __table_args__: Any = (
        sa.UniqueConstraint("material_version_id", "slug", name="uq_material_property_slug"),
        sa.UniqueConstraint("material_version_id", "prop_name", name="uq_material_property"),
    )


class JobMaterialUse(_Base):
    """Per-field provenance: which material version fed which job field.

    Written at submit time (the API process) for traceability ("which
    jobs used material version X?"). Composite PK on ``(job_id,
    field_name)`` — at most one material version per field per job. FKs
    *to* ``job`` and ``material_version``; nothing FKs *from* ``job`` to
    here, so the runner needs no new side-effect import.
    """

    __tablename__ = "job_material_use"

    job_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("job.id", ondelete="CASCADE"),
        primary_key=True,
    )
    field_name: Mapped[str] = mapped_column(sa.String(128), primary_key=True)
    material_version_id: Mapped[int] = mapped_column(
        sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
        sa.ForeignKey("material_version.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
