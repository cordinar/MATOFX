"""MaterialsService — the materials-database lifecycle (ADR-0007 §4).

A material is a global, curator-governed card with immutable,
ordinal-versioned scalar property sets, validated against a per-class
expected-property schema. This service owns the lifecycle and its audit
trail, in two groups:

- **Class schema** — :meth:`create_class`, :meth:`add_class_property`,
  :meth:`retire_class` (+ reads). The schema lets a tool require "a
  Fluid" and have the system check the chosen material exposes the
  required properties in the right units.
- **Material cards** — :meth:`create_material`, :meth:`add_version`
  (append-only immutable versioning), :meth:`update_material_metadata`
  (optimistic-locked), :meth:`retire` / :meth:`restore`, the read side
  (:meth:`get` / :meth:`list_materials` / :meth:`search` / version reads), and
  :meth:`resolve_for_submit` — which freezes a chosen version's
  properties into the shape the runner bakes into ``inputs.json`` so
  re-runs are reproducible.

**Authorisation:** reads are open to any authenticated user (no
per-material visibility — materials are global); create / edit / retire
require :attr:`~matofx.users.permissions.Permission.MATERIALS_CURATE`.
Every mutation audits ``materials.material.<verb>`` /
``materials.class.<verb>``.

**Slice boundary (2):** this builds the curator-gated lifecycle with
**direct publish** — a curator's create / add-version lands as
``publish_state='published'`` immediately. The toggleable directed-
approver review (ADR-0007 §5: ``draft``/``pending``/``rejected`` +
``materials_change_approval`` + a ``material_version`` approval target)
arrives with the General-Settings slice + the approvals extension (§10
steps 4-5). The ``pending`` / ``rejected`` publish states therefore
aren't produced yet — the model reserves them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from matofx.audit.schema import RecordInput
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
    MaterialValidationError,
    MaterialVersionNotFoundError,
)
from matofx.materials.orm import (
    DATA_PEDIGREES,
    JobMaterialUse,
    Material,
    MaterialClass,
    MaterialClassProperty,
    MaterialProperty,
    MaterialVersion,
)
from matofx.users.errors import PermissionDeniedError
from matofx.users.permissions import (
    Permission,
    user_has_any_permission,
    user_has_permission,
)

if TYPE_CHECKING:
    import datetime as _dt

    from sqlalchemy.ext.asyncio import AsyncSession

    from matofx.approvals import ApprovalRequest, Decision
    from matofx.audit import AuditWriter

# The approval kind used for a material-change review (a ``pre`` gate —
# the change is reviewed before it becomes pickable; mirrors the library).
_PUBLISH_KIND = "pre"


@dataclass(frozen=True)
class MaterialPickerEntry:
    """A material + its latest published version, for a ``material``-field picker.

    ``properties`` is the frozen block keyed by **slug** (the immutable
    identifier); each value carries the display ``prop_name`` alongside
    ``value`` / ``unit`` / ``source`` / ``uncertainty`` / ``notes`` so the
    UI can render labels while the form pipeline reads by slug.
    """

    material_id: int
    name: str
    class_name: str
    version_id: int
    version_ordinal: int
    properties: dict[str, dict[str, Any]]


_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Derive a snake_case identifier from a property name.

    Lowercase + collapse non-alphanumeric runs into a single ``_`` + trim
    leading/trailing ``_``. The slug is **immutable once set** on a
    material-class property; the editable ``prop_name`` keeps the
    display label. The service uses this when the caller doesn't pass
    an explicit slug. Empty / all-punctuation input raises
    :class:`MaterialValidationError`.
    """
    base = _NON_ALNUM_RE.sub("_", (name or "").strip().lower()).strip("_")
    if not base:
        raise MaterialValidationError(
            "A property name needs at least one letter or digit so a slug can be derived."
        )
    # 60 leaves room for collision suffixes in the migration backfill; at
    # create time the service rejects collisions outright (UNIQUE constraint).
    return base[:60]


@dataclass(frozen=True)
class PropertySpec:
    """One scalar property supplied when creating a material version.

    ``slug`` is the immutable snake_case key (matched against the class
    schema's slug). When ``None``, the service derives it from
    ``prop_name`` via :func:`slugify`. ``prop_name`` is the display
    label and may differ from the class's stored label (the service
    preserves what the caller submitted as a per-version snapshot).
    """

    prop_name: str
    value: float
    unit: str
    slug: str | None = None
    source: str | None = None
    uncertainty: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class MaterialsService:
    """Lifecycle + audit for the materials database (ADR-0007)."""

    audit: AuditWriter

    # ----- class schema ------------------------------------------------

    async def create_class(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        name: str,
        description: str = "",
    ) -> MaterialClass:
        """Create a material class (Fluid / Metal / …). Requires curate."""
        await self._require_curate(session, actor_user_id)
        clean = name.strip()
        if not clean:
            raise MaterialValidationError("A material class needs a name.")
        row = MaterialClass(name=clean, description=description, state="active")
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateMaterialClassError(clean) from exc
        await self._audit(
            session,
            actor_user_id,
            "materials.class.create",
            "material_class",
            row.id,
            payload={"name": clean},
        )
        return row

    async def add_class_property(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        class_id: int,
        prop_name: str,
        unit: str,
        required: bool = True,
        slug: str | None = None,
    ) -> MaterialClassProperty:
        """Declare an expected property on a class. Requires curate.

        ``slug`` is the immutable snake_case identifier the descriptor
        ``required_properties`` references and that keys the frozen
        block in ``inputs.json``. When ``None``, derived from
        ``prop_name`` via :func:`slugify`. Once set it never changes;
        :meth:`update_class_property` can rename ``prop_name`` (the
        display label) freely but rejects any slug edit by omission.
        """
        await self._require_curate(session, actor_user_id)
        cls = await self._get_class(session, class_id)
        clean_name = prop_name.strip()
        if not clean_name:
            raise MaterialValidationError("A class property needs a name.")
        clean_slug = slugify(slug) if slug else slugify(clean_name)
        row = MaterialClassProperty(
            class_id=cls.id,
            slug=clean_slug,
            prop_name=clean_name,
            unit=unit.strip(),
            required=required,
        )
        session.add(row)
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            # Either the slug or the prop_name collided within this class.
            raise DuplicateClassPropertyError(clean_name) from exc
        await self._audit(
            session,
            actor_user_id,
            "materials.class.property.add",
            "material_class",
            cls.id,
            payload={
                "slug": clean_slug,
                "prop_name": clean_name,
                "unit": unit,
                "required": required,
            },
        )
        return row

    async def update_class_property(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        class_id: int,
        property_id: int,
        prop_name: str | None = None,
        unit: str | None = None,
        required: bool | None = None,
    ) -> MaterialClassProperty:
        """Edit an expected-property row on a class. Forward-only.

        Fixing a typo (renaming ``thermel`` → ``thermal``) or correcting
        a unit is a **forward-only schema change**: existing
        :class:`MaterialProperty` rows on past versions keep their
        recorded ``prop_name`` / ``unit`` snapshots (those versions are
        immutable). New material versions added after the edit will use
        the corrected schema. Pass ``None`` to leave a field unchanged.

        Renaming to a name already declared on this class raises
        :class:`DuplicateClassPropertyError` (the class-property
        UNIQUE constraint).
        """
        await self._require_curate(session, actor_user_id)
        cls = await self._get_class(session, class_id)
        prop = await session.get(MaterialClassProperty, property_id)
        if prop is None or prop.class_id != cls.id:
            raise MaterialClassPropertyNotFoundError(property_id)
        before = {"prop_name": prop.prop_name, "unit": prop.unit, "required": prop.required}
        # Capture the new values into locals before flush — after a
        # rollback the ``prop`` instance is expired and attribute access
        # would trigger a lazy load past the greenlet boundary.
        new_name = str(before["prop_name"])
        new_unit = str(before["unit"])
        new_required = bool(before["required"])
        if prop_name is not None:
            clean = prop_name.strip()
            if not clean:
                raise MaterialValidationError("A class property needs a name.")
            new_name = clean
            prop.prop_name = clean
        if unit is not None:
            clean_unit = unit.strip()
            if not clean_unit:
                raise MaterialValidationError("A class property needs a unit.")
            new_unit = clean_unit
            prop.unit = clean_unit
        if required is not None:
            new_required = bool(required)
            prop.required = new_required
        try:
            await session.flush()
        except IntegrityError as exc:
            await session.rollback()
            raise DuplicateClassPropertyError(new_name) from exc
        await self._audit(
            session,
            actor_user_id,
            "materials.class.property.update",
            "material_class",
            cls.id,
            payload={
                "property_id": property_id,
                "before": before,
                "after": {
                    "prop_name": new_name,
                    "unit": new_unit,
                    "required": new_required,
                },
            },
        )
        return prop

    async def remove_class_property(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        class_id: int,
        property_id: int,
    ) -> None:
        """Remove an expected-property row from a class. Forward-only.

        Existing :class:`MaterialProperty` rows on past versions are
        **not** touched (they're immutable snapshots). New material
        versions added after the removal don't need to carry this
        property; if it was ``required``, the requirement is gone.
        """
        await self._require_curate(session, actor_user_id)
        cls = await self._get_class(session, class_id)
        prop = await session.get(MaterialClassProperty, property_id)
        if prop is None or prop.class_id != cls.id:
            raise MaterialClassPropertyNotFoundError(property_id)
        snapshot = {
            "property_id": property_id,
            "prop_name": prop.prop_name,
            "unit": prop.unit,
            "required": prop.required,
        }
        await session.delete(prop)
        await session.flush()
        await self._audit(
            session,
            actor_user_id,
            "materials.class.property.remove",
            "material_class",
            cls.id,
            payload=snapshot,
        )

    async def retire_class(
        self, session: AsyncSession, *, actor_user_id: int, class_id: int
    ) -> MaterialClass:
        """Retire a class so new materials can't use it. Idempotent."""
        await self._require_curate(session, actor_user_id)
        cls = await self._get_class(session, class_id)
        if cls.state == "retired":
            return cls
        cls.state = "retired"
        await session.flush()
        await self._audit(
            session, actor_user_id, "materials.class.retire", "material_class", cls.id, payload={}
        )
        return cls

    async def list_classes(
        self, session: AsyncSession, *, include_retired: bool = False
    ) -> list[MaterialClass]:
        """Every class (active first), newest name order; read-open."""
        stmt = select(MaterialClass)
        if not include_retired:
            stmt = stmt.where(MaterialClass.state == "active")
        stmt = stmt.order_by(MaterialClass.name)
        return list((await session.execute(stmt)).scalars())

    async def get_class(self, session: AsyncSession, class_id: int) -> MaterialClass:
        """Fetch one class or raise :class:`MaterialClassNotFoundError`."""
        return await self._get_class(session, class_id)

    async def class_schema(
        self, session: AsyncSession, class_id: int
    ) -> list[MaterialClassProperty]:
        """The expected-property rows for a class (required first, then name)."""
        await self._get_class(session, class_id)
        stmt = (
            select(MaterialClassProperty)
            .where(MaterialClassProperty.class_id == class_id)
            .order_by(MaterialClassProperty.required.desc(), MaterialClassProperty.prop_name)
        )
        return list((await session.execute(stmt)).scalars())

    # ----- material create / version -----------------------------------

    async def create_material(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        name: str,
        class_id: int,
        properties: list[PropertySpec],
        provenance: str,
        description: str = "",
        aliases: list[str] | None = None,
        tags: list[str] | None = None,
        confidentiality: str | None = None,
        reference: str | None = None,
        data_pedigree: str | None = None,
        conditions: str | None = None,
        version_notes: str | None = None,
        effective_date: _dt.date | None = None,
        approval_required: bool = True,
        designated_approver_user_id: int | None = None,
        designated_approver_username: str | None = None,
    ) -> Material:
        """Create a material + its first immutable version + properties.

        Requires curate. The class must exist and be active; the supplied
        properties are validated against the class's required schema
        (every required property present with a matching unit).

        When ``approval_required`` (the caller-resolved
        ``materials_change_approval`` toggle, ADR-0007 §5), the new
        material lands ``pending`` behind a directed-approver gate — the
        author names one *other* curator
        (``designated_approver_user_id`` / ``_username``) and the version
        isn't pickable until approved. With it off, the curator publishes
        the material directly.
        """
        await self._require_create(session, actor_user_id)
        clean_name = name.strip()
        if not clean_name:
            raise MaterialValidationError("A material needs a name.")
        clean_provenance = provenance.strip()
        if not clean_provenance:
            raise MaterialValidationError(
                "A material version needs a provenance note (its source)."
            )
        self._check_pedigree(data_pedigree)
        cls = await self._get_class(session, class_id)
        if cls.state == "retired":
            raise MaterialClassRetiredError(cls.name)
        specs = await self._validate_properties(session, cls, properties)

        material = Material(
            name=clean_name,
            aliases=list(aliases or []),
            class_id=cls.id,
            description=description,
            tags=list(tags or []),
            steward_user_id=actor_user_id,
            confidentiality=confidentiality,
            state="active",
            created_by_user_id=actor_user_id,
        )
        session.add(material)
        await session.flush()

        version = MaterialVersion(
            material_id=material.id,
            ordinal=1,
            provenance=clean_provenance,
            reference=reference,
            data_pedigree=data_pedigree,
            conditions=conditions,
            version_notes=version_notes,
            effective_date=effective_date,
            publish_state="draft",
            created_by_user_id=actor_user_id,
        )
        session.add(version)
        await session.flush()
        self._add_properties(session, version.id, specs)
        await session.flush()

        await self._apply_landing_gate(
            session,
            material=material,
            version=version,
            actor_user_id=actor_user_id,
            approval_required=approval_required,
            designated_approver_user_id=designated_approver_user_id,
            designated_approver_username=designated_approver_username,
            is_new_material=True,
        )
        await session.flush()

        await self._audit(
            session,
            actor_user_id,
            "materials.material.create",
            "material",
            material.id,
            payload={
                "name": clean_name,
                "class": cls.name,
                "version_id": version.id,
                "publish_state": version.publish_state,
                "properties": [s.prop_name for s in specs],
            },
        )
        return material

    async def add_version(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        material_id: int,
        properties: list[PropertySpec],
        provenance: str,
        reference: str | None = None,
        data_pedigree: str | None = None,
        conditions: str | None = None,
        version_notes: str | None = None,
        effective_date: _dt.date | None = None,
        approval_required: bool = True,
        designated_approver_user_id: int | None = None,
        designated_approver_username: str | None = None,
    ) -> MaterialVersion:
        """Append a new immutable version (``ordinal + 1``) + properties.

        Requires curate; the material must not be retired. Properties are
        validated against the material's class schema. When
        ``approval_required`` (ADR-0007 §5) the new version lands
        ``pending`` behind a directed-approver gate and isn't pickable
        until approved; the material's prior published version (if any)
        stays usable in the meantime.
        """
        await self._require_create(session, actor_user_id)
        material = await self._get_material(session, material_id)
        if material.state == "retired":
            raise MaterialRetiredError(material_id)
        clean_provenance = provenance.strip()
        if not clean_provenance:
            raise MaterialValidationError(
                "A material version needs a provenance note (its source)."
            )
        self._check_pedigree(data_pedigree)
        cls = await self._get_class(session, material.class_id)
        specs = await self._validate_properties(session, cls, properties)

        ordinal = await self._next_ordinal(session, material_id)
        version = MaterialVersion(
            material_id=material_id,
            ordinal=ordinal,
            provenance=clean_provenance,
            reference=reference,
            data_pedigree=data_pedigree,
            conditions=conditions,
            version_notes=version_notes,
            effective_date=effective_date,
            publish_state="draft",
            created_by_user_id=actor_user_id,
        )
        session.add(version)
        await session.flush()
        self._add_properties(session, version.id, specs)
        await session.flush()

        await self._apply_landing_gate(
            session,
            material=material,
            version=version,
            actor_user_id=actor_user_id,
            approval_required=approval_required,
            designated_approver_user_id=designated_approver_user_id,
            designated_approver_username=designated_approver_username,
            is_new_material=False,
        )
        await session.flush()

        await self._audit(
            session,
            actor_user_id,
            "materials.material.version.add",
            "material",
            material_id,
            payload={
                "version_id": version.id,
                "ordinal": ordinal,
                "publish_state": version.publish_state,
            },
        )
        return version

    async def update_material_metadata(
        self,
        session: AsyncSession,
        *,
        actor_user_id: int,
        material_id: int,
        expected_version: int | None = None,
        name: str | None = None,
        description: str | None = None,
        aliases: list[str] | None = None,
        tags: list[str] | None = None,
        confidentiality: str | None = None,
    ) -> Material:
        """Edit card-level metadata; requires curate + optimistic lock.

        ``None`` arguments are left unchanged; this slice does not support
        clearing a nullable field back to NULL (pass an explicit value).
        """
        await self._require_curate(session, actor_user_id)
        material = await self._get_material(session, material_id)
        if material.state == "retired":
            raise MaterialRetiredError(material_id)
        if expected_version is not None and material.version != expected_version:
            raise MaterialConcurrentModificationError
        if name is not None:
            clean = name.strip()
            if not clean:
                raise MaterialValidationError("A material needs a name.")
            material.name = clean
        if description is not None:
            material.description = description
        if aliases is not None:
            material.aliases = list(aliases)
        if tags is not None:
            material.tags = list(tags)
        if confidentiality is not None:
            material.confidentiality = confidentiality
        try:
            await session.flush()
        except StaleDataError as exc:
            raise MaterialConcurrentModificationError from exc
        await self._audit(
            session,
            actor_user_id,
            "materials.material.update",
            "material",
            material.id,
            payload={"name": material.name},
        )
        return material

    async def retire(
        self, session: AsyncSession, *, actor_user_id: int, material_id: int
    ) -> Material:
        """Soft-delete: ``state='retired'`` (never a hard delete). Idempotent."""
        await self._require_curate(session, actor_user_id)
        material = await self._get_material(session, material_id)
        if material.state == "retired":
            return material
        material.state = "retired"
        await session.flush()
        await self._audit(
            session, actor_user_id, "materials.material.retire", "material", material.id, payload={}
        )
        return material

    async def restore(
        self, session: AsyncSession, *, actor_user_id: int, material_id: int
    ) -> Material:
        """Reverse a retire: ``state='active'``. Idempotent."""
        await self._require_curate(session, actor_user_id)
        material = await self._get_material(session, material_id)
        if material.state != "retired":
            return material
        material.state = "active"
        await session.flush()
        await self._audit(
            session,
            actor_user_id,
            "materials.material.restore",
            "material",
            material.id,
            payload={},
        )
        return material

    # ----- approval decision (ADR-0007 §5.4) ---------------------------

    async def decide_approval(
        self,
        session: AsyncSession,
        *,
        request_id: int,
        decider_user_id: int,
        decider_username: str,
        decision: Decision,
        reason: str | None = None,
        decider_is_approver: bool,
        decider_is_admin: bool,
    ) -> tuple[ApprovalRequest, MaterialVersion]:
        """Apply a curator's approve/reject to a pending material change.

        Delegates the decision + the self-approval / eligibility defence
        to :class:`ApprovalService`, then applies the materials-side
        effect:

        - **approved** → version ``publish_state='published'`` (pickable);
          a brand-new pending material becomes ``active``.
        - **denied** → version ``publish_state='rejected'``; the material
          keeps its prior published version (its ``state`` is left as-is).

        ``decider_is_approver`` is the caller-resolved "is this user still
        a curator" fact (for a directed material gate the API resolves it
        from :attr:`Permission.MATERIALS_CURATE`).
        """
        from matofx.approvals import make_approval_service  # noqa: PLC0415

        request = await make_approval_service(self.audit).decide(
            session,
            request_id=request_id,
            decider_user_id=decider_user_id,
            decider_username=decider_username,
            decision=decision,
            reason=reason,
            decider_is_approver=decider_is_approver,
            decider_is_admin=decider_is_admin,
        )
        if request.material_version_id is None:
            raise MaterialVersionNotFoundError(0)
        version = await session.get(MaterialVersion, request.material_version_id)
        if version is None:
            raise MaterialVersionNotFoundError(request.material_version_id)
        material = await self._get_material(session, version.material_id)

        if request.status == "approved":
            version.publish_state = "published"
            if material.state == "pending":
                material.state = "active"
            action, audit_reason = "materials.material.approve", None
        else:
            version.publish_state = "rejected"
            action, audit_reason = "materials.material.reject", request.decision_reason
        await session.flush()

        await self._audit(
            session,
            decider_user_id,
            action,
            "material",
            material.id,
            payload={"version_id": version.id, "decided_via": request.decided_via},
            reason=audit_reason,
        )
        return request, version

    # ----- reads (open to any authenticated user) ----------------------

    async def get(self, session: AsyncSession, material_id: int) -> Material:
        """Fetch one material or raise :class:`MaterialNotFoundError`."""
        return await self._get_material(session, material_id)

    async def list_materials(
        self,
        session: AsyncSession,
        *,
        class_id: int | None = None,
        include_retired: bool = False,
    ) -> list[Material]:
        """Materials (optionally filtered by class), newest first.

        Named ``list_materials`` (not ``list``) so the method doesn't
        shadow the builtin ``list`` inside the class body.
        """
        return await self.search(session, class_id=class_id, include_retired=include_retired)

    async def search(
        self,
        session: AsyncSession,
        *,
        name: str | None = None,
        class_id: int | None = None,
        tag: str | None = None,
        include_retired: bool = False,
    ) -> list[Material]:
        """Filter materials by name substring / class / tag (design §4).

        Name + class + state are filtered in SQL; the ``tag`` filter is
        applied in Python (JSON-list containment isn't portable across
        SQLite/Postgres — same rationale as the approvals inbox filter).
        """
        stmt = select(Material)
        if not include_retired:
            stmt = stmt.where(Material.state != "retired")
        if class_id is not None:
            stmt = stmt.where(Material.class_id == class_id)
        if name:
            stmt = stmt.where(func.lower(Material.name).like(f"%{name.strip().lower()}%"))
        stmt = stmt.order_by(Material.created_at.desc(), Material.id.desc())
        rows = list((await session.execute(stmt)).scalars())
        if tag:
            wanted = tag.strip()
            rows = [m for m in rows if wanted in (m.tags or [])]
        return rows

    async def list_versions(self, session: AsyncSession, material_id: int) -> list[MaterialVersion]:
        """Every version of a material, newest first."""
        await self._get_material(session, material_id)
        stmt = (
            select(MaterialVersion)
            .where(MaterialVersion.material_id == material_id)
            .order_by(MaterialVersion.ordinal.desc())
        )
        return list((await session.execute(stmt)).scalars())

    async def get_version(self, session: AsyncSession, version_id: int) -> MaterialVersion:
        """Fetch one material version or raise :class:`MaterialVersionNotFoundError`.

        Materials are read-open to any authenticated user (ADR-0007),
        so no ACL gate beyond the existence check. Used by the lineage
        layer (ADR-0010) as the
        :attr:`matofx.lineage.types.NodeKind.MATERIAL_VERSION` resolver.
        """
        version = await session.get(MaterialVersion, version_id)
        if version is None:
            raise MaterialVersionNotFoundError(version_id)
        return version

    async def version_properties(
        self, session: AsyncSession, version_id: int
    ) -> list[MaterialProperty]:
        """The scalar properties of one material version (by name)."""
        if await session.get(MaterialVersion, version_id) is None:
            raise MaterialVersionNotFoundError(version_id)
        stmt = (
            select(MaterialProperty)
            .where(MaterialProperty.material_version_id == version_id)
            .order_by(MaterialProperty.prop_name)
        )
        return list((await session.execute(stmt)).scalars())

    # ----- submit-time resolution (design §3 / §4) ---------------------

    async def resolve_for_submit(
        self,
        session: AsyncSession,
        *,
        material_version_id: int,
        required_properties: list[str] | None = None,
    ) -> dict[str, Any]:
        """Freeze a chosen version's properties into the ``inputs.json`` block.

        Returns a nested ``material`` block — id, name, class, version
        ordinal, and ``{slug: {prop_name, value, unit}}`` — that the
        runner bakes into ``inputs.json`` so the tool reads frozen
        values (re-runs are reproducible). Tools read properties by
        **slug** (the immutable identifier); the display ``prop_name``
        is carried alongside for UI / log surfaces.

        ``required_properties`` defaults to the class's ``required``
        set and is a list of **slugs**. Each must be present on the
        version, and where the class declares a unit for it the
        version's unit must match (:class:`MaterialSchemaError`).
        """
        version = await session.get(MaterialVersion, material_version_id)
        if version is None:
            raise MaterialVersionNotFoundError(material_version_id)
        material = await self._get_material(session, version.material_id)
        cls = await self._get_class(session, material.class_id)

        class_meta = await self._class_property_meta(session, cls.id)
        props_by_slug = {p.slug: p for p in await self.version_properties(session, version.id)}
        required = (
            required_properties
            if required_properties is not None
            else [slug for slug, m in class_meta.items() if m["required"]]
        )
        for slug in required:
            prop = props_by_slug.get(slug)
            if prop is None:
                label = (class_meta.get(slug) or {}).get("prop_name", slug)
                raise MaterialSchemaError(
                    f"{material.name!r} is missing required property {label!r}."
                )
            meta = class_meta.get(slug)
            if meta is not None and prop.unit != meta["unit"]:
                raise MaterialSchemaError(
                    f"Property {meta['prop_name']!r} on {material.name!r} is in "
                    f"{prop.unit!r}, but its class expects {meta['unit']!r}."
                )

        return {
            "material_id": material.id,
            "name": material.name,
            "class": cls.name,
            "version_id": version.id,
            "version_ordinal": version.ordinal,
            "properties": {
                p.slug: {
                    "prop_name": p.prop_name,
                    "value": float(p.value),
                    "unit": p.unit,
                }
                for p in props_by_slug.values()
            },
        }

    async def list_for_picker(
        self,
        session: AsyncSession,
        *,
        class_name: str,
        required_properties: list[str] | None = None,
    ) -> list[MaterialPickerEntry]:
        """Materials of ``class_name`` offered in a ``material``-field picker (§6).

        Returns each ``active`` material's **latest published** version,
        with its frozen property block. A material whose latest published
        version is missing any of ``required_properties`` is skipped, so
        the form only offers usable choices (the same set
        :meth:`resolve_for_submit` would accept). Unknown / retired class
        → empty list.
        """
        cls = (
            await session.execute(select(MaterialClass).where(MaterialClass.name == class_name))
        ).scalar_one_or_none()
        if cls is None or cls.state != "active":
            return []
        required = required_properties or []
        out: list[MaterialPickerEntry] = []
        for material in await self.search(session, class_id=cls.id):
            version = (
                await session.execute(
                    select(MaterialVersion)
                    .where(
                        MaterialVersion.material_id == material.id,
                        MaterialVersion.publish_state == "published",
                    )
                    .order_by(MaterialVersion.ordinal.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if version is None:
                continue
            # Picker block is slug-keyed (the wire format the form pipeline
            # uses); display ``prop_name`` is carried inside each value so
            # the UI can render labels alongside.
            props = {
                p.slug: {
                    "prop_name": p.prop_name,
                    "value": float(p.value),
                    "unit": p.unit,
                    "source": p.source,
                    "uncertainty": p.uncertainty,
                    "notes": p.notes,
                }
                for p in await self.version_properties(session, version.id)
            }
            if any(slug not in props for slug in required):
                continue
            out.append(
                MaterialPickerEntry(
                    material_id=material.id,
                    name=material.name,
                    class_name=cls.name,
                    version_id=version.id,
                    version_ordinal=version.ordinal,
                    properties=props,
                )
            )
        return out

    async def record_job_use(
        self,
        session: AsyncSession,
        *,
        job_id: int,
        field_name: str,
        material_version_id: int,
    ) -> JobMaterialUse:
        """Record that a job field was fed by a material version (§2.5).

        Written at submit by the form-integration path (a later slice).
        Composite PK ``(job_id, field_name)`` — one material per field.
        """
        row = JobMaterialUse(
            job_id=job_id, field_name=field_name, material_version_id=material_version_id
        )
        session.add(row)
        await session.flush()
        return row

    async def clear_job_uses(self, session: AsyncSession, *, job_id: int) -> None:
        """Remove all material-use provenance rows for a job.

        Called by the submit path before re-recording uses so a re-queue
        / retry of a material-using job doesn't collide with the leftover
        ``(job_id, field_name)`` rows from the previous run.
        """
        await session.execute(delete(JobMaterialUse).where(JobMaterialUse.job_id == job_id))

    # ----- internals ----------------------------------------------------

    async def _require_create(self, session: AsyncSession, actor_user_id: int) -> None:
        """Gate adding/versioning materials on ``materials.create`` (or curate).

        Contributing materials is a base-tier action: a
        ``materials.create`` holder may add and version materials, as may a
        ``materials.curate`` curator (who can always contribute). Retiring,
        restoring, and class management stay curate-only.
        """
        if not await user_has_any_permission(
            session, actor_user_id, Permission.MATERIALS_CREATE, Permission.MATERIALS_CURATE
        ):
            raise PermissionDeniedError(Permission.MATERIALS_CREATE.value)

    async def _require_curate(self, session: AsyncSession, actor_user_id: int) -> None:
        if not await user_has_permission(session, actor_user_id, Permission.MATERIALS_CURATE):
            raise PermissionDeniedError(Permission.MATERIALS_CURATE.value)

    async def _apply_landing_gate(
        self,
        session: AsyncSession,
        *,
        material: Material,
        version: MaterialVersion,
        actor_user_id: int,
        approval_required: bool,
        designated_approver_user_id: int | None,
        designated_approver_username: str | None,
        is_new_material: bool,
    ) -> None:
        """Publish a new version directly, or gate it for review (ADR-0007 §5).

        With the toggle off, the curator's change publishes immediately.
        With it on, the version lands ``pending`` behind a directed
        ``ApprovalRequest`` (a brand-new material also goes ``pending``);
        a second curator must approve before it is pickable.
        """
        if not approval_required:
            version.publish_state = "published"
            return
        username = (designated_approver_username or "").strip()
        if designated_approver_user_id is None or not username:
            raise ApproverRequiredError
        version.publish_state = "pending"
        if is_new_material:
            material.state = "pending"
        await session.flush()
        from matofx.approvals import make_approval_service  # noqa: PLC0415

        await make_approval_service(self.audit).create_request(
            session,
            material_version_id=version.id,
            kind=_PUBLISH_KIND,
            requested_by_user_id=actor_user_id,
            eligible_approvers=[username],
            designated_approver_user_id=designated_approver_user_id,
        )

    async def _get_class(self, session: AsyncSession, class_id: int) -> MaterialClass:
        cls = await session.get(MaterialClass, class_id)
        if cls is None:
            raise MaterialClassNotFoundError(class_id)
        return cls

    async def _get_material(self, session: AsyncSession, material_id: int) -> Material:
        material = await session.get(Material, material_id)
        if material is None:
            raise MaterialNotFoundError(material_id)
        return material

    async def _next_ordinal(self, session: AsyncSession, material_id: int) -> int:
        current = (
            await session.execute(
                select(func.max(MaterialVersion.ordinal)).where(
                    MaterialVersion.material_id == material_id
                )
            )
        ).scalar()
        return int(current or 0) + 1

    async def _class_property_meta(
        self, session: AsyncSession, class_id: int
    ) -> dict[str, dict[str, Any]]:
        """``{slug: {"prop_name", "unit", "required"}}`` for every class property."""
        stmt = select(
            MaterialClassProperty.slug,
            MaterialClassProperty.prop_name,
            MaterialClassProperty.unit,
            MaterialClassProperty.required,
        ).where(MaterialClassProperty.class_id == class_id)
        return {
            row[0]: {"prop_name": row[1], "unit": row[2], "required": bool(row[3])}
            for row in (await session.execute(stmt)).all()
        }

    async def _validate_properties(
        self, session: AsyncSession, cls: MaterialClass, properties: list[PropertySpec]
    ) -> list[PropertySpec]:
        """Validate supplied properties against the class's required schema.

        Matching is by **slug** (the immutable identifier), not by
        ``prop_name`` — so a curator renaming the display label doesn't
        break submissions. Every ``required`` class slug must be present;
        units must match the class's declared unit. Extra (non-schema)
        properties are allowed (design §2.4). Returns the cleaned specs
        with every spec's ``slug`` populated.
        """
        class_meta = await self._class_property_meta(session, cls.id)
        cleaned: list[PropertySpec] = []
        seen_slugs: set[str] = set()
        for spec in properties:
            pname = spec.prop_name.strip()
            if not pname:
                raise MaterialValidationError("A material property needs a name.")
            # Explicit slug wins; otherwise derive from prop_name. The
            # picker dialog / wizard send display labels, the API send
            # path lets advanced callers provide a slug directly.
            slug = slugify(spec.slug) if spec.slug else slugify(pname)
            if slug in seen_slugs:
                raise DuplicateMaterialPropertyError(pname)
            seen_slugs.add(slug)
            cleaned.append(
                PropertySpec(
                    prop_name=pname,
                    value=spec.value,
                    unit=spec.unit.strip(),
                    slug=slug,
                    source=spec.source,
                    uncertainty=spec.uncertainty,
                    notes=spec.notes,
                )
            )
        supplied_by_slug = {s.slug: s for s in cleaned}
        for slug, meta in class_meta.items():
            if not meta["required"]:
                continue
            # Rename the inner-loop variable so mypy doesn't conflate it
            # with the earlier ``for spec in properties:`` (which narrowed
            # ``spec`` to ``PropertySpec``).
            match = supplied_by_slug.get(slug)
            if match is None:
                raise MaterialSchemaError(
                    f"Material is missing required property {meta['prop_name']!r} for class "
                    f"{cls.name!r}."
                )
            if match.unit != meta["unit"]:
                raise MaterialSchemaError(
                    f"Property {meta['prop_name']!r} must be in {meta['unit']!r} for class "
                    f"{cls.name!r}, got {match.unit!r}."
                )
        return cleaned

    @staticmethod
    def _check_pedigree(data_pedigree: str | None) -> None:
        if data_pedigree is not None and data_pedigree not in DATA_PEDIGREES:
            raise MaterialValidationError(
                f"{data_pedigree!r} isn't a recognised data pedigree. "
                f"Use one of: {', '.join(DATA_PEDIGREES)}."
            )

    @staticmethod
    def _add_properties(session: AsyncSession, version_id: int, specs: list[PropertySpec]) -> None:
        for spec in specs:
            # The slug is mandatory on the persisted row. ``_validate_properties``
            # populates every spec's slug, so this should never see a None.
            slug = spec.slug or slugify(spec.prop_name)
            session.add(
                MaterialProperty(
                    material_version_id=version_id,
                    slug=slug,
                    prop_name=spec.prop_name,
                    value=spec.value,
                    unit=spec.unit,
                    source=spec.source,
                    uncertainty=spec.uncertainty,
                    notes=spec.notes,
                )
            )

    async def _audit(
        self,
        session: AsyncSession,
        actor_user_id: int,
        action: str,
        resource_type: str,
        resource_id: int,
        *,
        payload: dict[str, Any],
        reason: str | None = None,
    ) -> None:
        await self.audit.record(
            session,
            RecordInput(
                actor=f"user:{actor_user_id}",
                actor_type="user",
                action=action,
                resource_type=resource_type,
                resource_id=str(resource_id),
                outcome="success",
                payload=payload,
                reason=reason,
            ),
        )


def make_materials_service(audit: AuditWriter) -> MaterialsService:
    """Wire up a :class:`MaterialsService` from the ambient audit writer."""
    return MaterialsService(audit=audit)
