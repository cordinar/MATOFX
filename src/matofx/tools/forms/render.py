"""Descriptor → NiceGUI input form renderer.

Walks a :class:`~matofx.tools.descriptor.ToolDescriptor` and emits the
input form as NiceGUI widgets. **Each ``[[input_groups]]`` entry in
the descriptor renders as its own ``ui.expansion``** (legacy parity,
locked on 2026-05-08).

Each section starts collapsed or expanded per the tool's
``input_groups_start_collapsed`` flag (default: collapsed). The
:class:`FormHandle` records which sections the user has opened at
least once (``opened_groups``) so the workspace can gate Submit on
"every section reviewed" when the tool's ``require_all_groups_opened``
flag is set — see :attr:`FormHandle.all_groups_opened`.

## Output shape

```
ui.row()                     ← outer column-grid (uses input_column_layout)
├── ui.column()  (column 1)
│   ├── ui.expansion("Group A")
│   │   └── ui.row()         ← sub_columns within the group
│   │       ├── ui.column()
│   │       │   ├── (widget) ← one InputField
│   │       │   ├── (help text)
│   │       │   └── (error label, hidden until shown)
│   │       └── ui.column()
│   │           └── ...
│   └── ui.expansion("Group B")
│       └── ...
└── ui.column()  (column 2)
    └── ...
```

Inputs without a `group` are placed into a synthetic "Other" group
expansion at the end of the first column so a tool author who hasn't
declared groups still gets a sensible layout.

## Public API

The caller (the My Jobs workspace) gets a :class:`FormHandle` back:

- ``handle.values()`` — read the current submitted values.
- ``handle.seed(values)`` — set widget values from a preset / previous-job
  / defaults seed.
- ``handle.show_errors(form_validation_error)`` — surface validation
  failures inline under each offending widget.
- ``handle.clear_errors()`` — wipe all error displays.

The state dict is the **single source of truth** for current widget
values; widgets are bound to it via NiceGUI's ``bind_value``, so any
mutation through ``handle.seed`` or direct widget interaction is
immediately reflected in ``state``.
"""

from __future__ import annotations

import contextlib
import math
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from nicegui import ui

from matofx.library.refs import build_library_ref, is_library_ref
from matofx.tools.descriptor import (
    InputField,
    InputGroup,
    RequireEffect,
    TableColumn,
    ToolDescriptor,
)
from matofx.tools.forms import safe_expr
from matofx.tools.forms.errors import FormValidationError
from matofx.tools.forms.resolver import FieldState, resolve
from matofx.tools.forms.table_plot import PlotColumn, build_figure, plan_table_plots


@dataclass(frozen=True)
class GuideImageView:
    """One resolved guide image ready to render (see :func:`render_form`).

    The caller (which has the API client) fetches the bytes and hands
    over a ready-to-embed ``data_url`` so this package never imports
    :class:`ApiClient`. ``caption`` may be empty.
    """

    caption: str
    data_url: str


def _render_guide_gallery(guides: list[GuideImageView]) -> None:
    """A captioned thumbnail strip of the tool's guide images, above the form.

    Each thumbnail opens full-size in a lightbox on click — the guides
    are visual explainers for what the inputs mean, kept compact so they
    don't push the actual fields down.
    """
    with (
        ui.element("div")
        .classes("w-full")
        .style(
            "border: 1px solid var(--matofx-card-border); border-radius: 8px; "
            "padding: 10px 12px; margin-bottom: 4px;"
        )
    ):
        ui.label("Parameter guide").style(
            "font-weight: 600; font-size: 0.9rem; margin-bottom: 6px;"
        )
        with ui.row().classes("w-full gap-4 flex-wrap items-start"):
            for guide in guides:
                _render_guide_thumb(guide)


def _render_guide_thumb(guide: GuideImageView) -> None:
    with ui.column().classes("items-center gap-1").style("width: 150px;"):
        img = ui.image(guide.data_url).style(
            "width: 150px; height: 104px; object-fit: cover; border-radius: 6px; "
            "cursor: zoom-in; border: 1px solid var(--matofx-card-border);"
        )
        img.on("click", lambda _e=None, _g=guide: _open_guide_lightbox(_g))
        if guide.caption:
            ui.label(guide.caption).style(
                "font-size: 0.76rem; color: var(--matofx-muted-text); text-align: center; "
                "line-height: 1.2;"
            )


def _open_guide_lightbox(guide: GuideImageView) -> None:
    # A native <img> sized to its natural dimensions, capped to the viewport —
    # a bare ``ui.image`` (Quasar q-img) with only max-width/height collapses to
    # a tiny box in a shrink-wrapping dialog. sanitize=False is required for the
    # raw <img> to survive NiceGUI 3.x's HTML sanitiser (data: src included).
    with ui.dialog() as dialog, ui.card().style("max-width: 94vw; padding: 10px;"):
        ui.html(
            f'<img src="{guide.data_url}" alt="" style="max-width: 90vw; '
            'max-height: 82vh; width: auto; height: auto; display: block; margin: 0 auto;">',
            sanitize=False,
        )
        if guide.caption:
            ui.label(guide.caption).style("color: var(--matofx-muted-text); margin-top: 4px;")
        with ui.row().classes("w-full justify-end"):
            ui.button("Close", on_click=dialog.close).props("flat")
    dialog.open()


@dataclass(frozen=True)
class FileInputContext:
    """Callbacks the rich file-input widget needs to talk to the API.

    Threaded into :func:`render_form` by the workspace so the form
    widget can stage / clear / discover uploads without the form
    package importing :class:`ApiClient` directly. Optional — when
    ``None`` the form falls back to a plain text-path input (tests and
    non-job-context callers).
    """

    upload: Callable[[str, str, bytes], Awaitable[str]]
    """``(field_name, filename, content_bytes) -> absolute_path``.

    Wired to ``ApiClient.upload_draft_file_input``. Returns the
    absolute on-disk path the upload landed at; the widget stores
    that string in ``state[field_name]`` so a subsequent submit
    carries the right value forward."""

    clear: Callable[[str], Awaitable[None]]
    """``(field_name) -> None``. Wired to
    ``ApiClient.clear_draft_file_input``. Wipes the bytes on disk
    and the matching ``Job.inputs[field_name]`` server-side."""

    list_previous: Callable[[str], Awaitable[list[dict[str, Any]]]]
    """``(field_name) -> [history_entry, ...]``.

    Each entry is a dict with ``value`` (the path), ``job_id`` (int),
    ``project_name`` (str), and ``scenario_name`` (str) — wired to
    ``ApiClient.list_file_input_history``. The picker labels each
    option ``"<filename>  ·  Job 0001234 (Project, Scenario)"`` and
    uses ``value`` as the picked path. The button itself is hidden
    when this list comes back empty, so a user with no prior runs of
    the tool never sees a dead-end action."""

    fetch_preview: Callable[[str], Awaitable[bytes]]
    """``(absolute_path) -> file_bytes``. Wired to
    ``ApiClient.fetch_draft_file_preview``. Used by the preview pane
    next to the file input to fetch the bytes of a previously-picked
    file (uploaded bytes are kept in-memory and don't need this
    fetch). The server-side endpoint refuses paths outside the
    storage mount, so this is a narrow read surface, not a
    general-purpose download path."""

    fetch_cad_preview: Callable[[str], Awaitable[bytes]]
    """``(absolute_path) -> stl_bytes``. Wired to
    ``ApiClient.fetch_draft_cad_preview``. Hits the server-side
    converter that turns ``.stp`` / ``.step`` / ``.iges`` / ``.obj``
    into binary STL bytes (and passes ``.stl`` through unchanged) so
    the widget can render every supported CAD format through one
    ``ui.scene.stl`` call. For freshly-uploaded CAD files the widget
    still calls this — uploaded bytes are STL only when the source
    was already STL; STP/STEP/IGES/OBJ need conversion either way."""

    list_library: Callable[[str], Awaitable[list[dict[str, Any]]]] | None = None
    """``(field_name) -> [library_picker_candidate, ...]`` — the 4th
    "Pick from library" file-source mode (ADR-0006 §6). Wired to
    ``ApiClient.library_picker`` for this tool + field. ``None`` (the
    default) hides the mode — set only when the ``file_library`` feature
    is on. Selecting a candidate records the library-version sentinel
    (:func:`matofx.library.refs.build_library_ref`) as the field's draft
    value; the bytes are resolved + frozen at submit."""

    list_other_outputs: (
        Callable[[list[str], str | None], Awaitable[list[dict[str, Any]]]] | None
    ) = None
    """``(accepted_file_types, expected_field_array) -> [output_candidate, ...]``
    — the "use a file produced by another job" cross-tool mode. Wired to
    ``ApiClient.list_file_output_candidates``; the widget passes the field's
    ``accepted_file_types`` (type filter) and ``expected_field_array`` (the
    nodal array it needs, e.g. ``"temperature"``) so only compatible outputs
    come back — a temperature-field input never lists a failure-index field.
    Each entry carries ``value`` (the path) + ``filename`` + ``field_array`` +
    ``output_label`` + ``tool_name`` + ``job_id`` + ``project_name`` +
    ``scenario_name`` so the 3-step picker (tool → job → output) can label every
    choice. ``None`` (the default) hides the mode. Unlike :attr:`list_previous`
    (same-tool *input* history), this surfaces *output* files across every tool
    the user sees — e.g. a standalone Bracket Stress run reusing Thermal Soak's
    field."""


@dataclass(frozen=True)
class MaterialInputContext:
    """Callback the ``material``-typed input picker needs (ADR-0007 §6).

    Threaded into :func:`render_form` by the workspace so a ``material``
    field renders a "Pick material" picker instead of the inert
    ``Any``-typed fallback. Optional — when ``None`` (the feature off, or
    a non-job-context caller) the field falls back to a plain text input.
    """

    list_materials: Callable[[str, list[str] | None], Awaitable[list[dict[str, Any]]]]
    """``(material_class, required_properties) -> [material_picker_candidate]``.

    Wired to ``ApiClient.materials_picker``. Selecting a candidate
    records its ``version_id`` as the field's draft value; the chosen
    version's properties are frozen into ``inputs.json`` at submit by
    :meth:`JobService._resolve_material_inputs`."""


_OTHER_GROUP_NAME = "__ungrouped__"
_OTHER_GROUP_LABEL = "Other"
"""Synthetic group used when descriptor inputs declare no ``group``.

Tool authors *should* declare groups (legacy parity + the user's
2026-05-08 requirement that each group renders as its own expansion);
this synthetic fallback keeps the renderer working when they don't,
without crashing or losing fields.
"""


# --- Public types --------------------------------------------------------


@dataclass
class FormHandle:
    """Returned by :func:`render_form`.

    The workspace uses this to interact with the rendered form:
    submit-time validation, seed loading, error display.
    """

    state: dict[str, Any]
    widgets: dict[str, Any]
    error_labels: dict[str, ui.label]
    descriptor: ToolDescriptor

    # Emitted input-group sections, in render order, as ``(name, label)``
    # pairs. Only groups that actually rendered an expansion are listed
    # (empty groups are skipped); the synthetic "Other" group for
    # ungrouped inputs appears here as ``("__ungrouped__", "Other")``.
    groups: list[tuple[str, str]] = field(default_factory=list)

    # Names of the sections the user has opened at least once. A section
    # that starts expanded is seeded in here at render time (it's
    # already revealed). Re-collapsing a section does NOT remove it —
    # the contract is "opened once", not "currently open".
    opened_groups: set[str] = field(default_factory=set)

    # Field default values (the descriptor's declared defaults). Captured
    # at render time so :meth:`seed` can fall back to them when a partial
    # seed dict omits some fields.
    _defaults: dict[str, Any] = field(default_factory=dict)

    # §16.16 reactivity. ``field_containers`` is the per-field wrapper
    # element (shown/hidden as a unit); ``auto_notes`` is the muted
    # "set automatically …" line under each field (empty/hidden until a
    # rule touches the field). Both are populated for every field
    # regardless of whether the tool has rules — the live re-resolve is
    # only *wired* when ``descriptor.dependencies`` is non-empty.
    field_containers: dict[str, Any] = field(default_factory=dict)
    auto_notes: dict[str, ui.label] = field(default_factory=dict)

    # Per-field lock hooks. A rich ``file`` widget registers a callback that
    # collapses its upload / pick / clear controls into a read-only
    # "provided by an upstream tool" state — :meth:`lock` calls it so a
    # wired file input can't be overridden, matching scalar-input locking.
    lock_hooks: dict[str, Callable[[], None]] = field(default_factory=dict)

    # Per-field seed hooks for composite widgets that aren't a single
    # bindable ``ui`` element (the editable ``table`` grid). :meth:`seed`
    # calls the hook instead of the ``widget.value =`` path so a preset /
    # previous-job / reset can repopulate the grid's rows.
    seed_hooks: dict[str, Callable[[Any], None]] = field(default_factory=dict)

    # Set by :func:`_wire_reactivity` for rule-bearing tools; ``None``
    # for rule-free tools (then :meth:`refresh` is a no-op).
    _reapply: Callable[[], None] | None = None

    # Set by :func:`_wire_live_validation` when the descriptor has any
    # numeric field or cross-field participant; ``None`` otherwise (then
    # :meth:`validate_cross_field` is a no-op). Re-runs the live
    # per-field layer (numeric bounds + membership + cross-field) against
    # the current values and paints the managed fields' error labels.
    _run_cross_field: Callable[[], None] | None = None

    # Field names a caller has locked (e.g. workflow ``downstream_input``
    # fields fed from an upstream tool). Locked fields render disabled and
    # stay disabled even when the §16.16 resolver would otherwise enable
    # them — see :meth:`lock` and :func:`_apply_field_state`.
    locked_fields: set[str] = field(default_factory=set)

    def refresh(self) -> None:
        """Re-resolve the conditional rules against the current values.

        Called automatically on every widget change for a rule-bearing
        tool, and by :meth:`seed` so a loaded preset / previous-job
        snapshot lands in its resolved state (fields a rule would hide
        or lock don't flash their seeded value). No-op when the tool
        has no rules.
        """
        if self._reapply is not None:
            self._reapply()

    def validate_cross_field(self) -> None:
        """Re-run the live per-field layer (bounds + membership + cross-field).

        Fired on every change to a numeric / participating widget so an
        out-of-range entry or a broken sum-check — e.g. component
        percentages totalling 100 — flags inline *as the numbers are
        entered*, not only when Submit is pressed. Also called by
        :meth:`seed` so a loaded preset / previous-job / upstream
        snapshot surfaces a violation immediately. No-op when the tool
        has no numeric field and no cross-field constraint.
        """
        if self._run_cross_field is not None:
            self._run_cross_field()

    def lock(self, names: Iterable[str]) -> None:
        """Disable the named fields and keep them disabled.

        Used by the workflow-run surface for ``downstream_input`` fields
        fed from an upstream tool: the user sees the value but can't edit
        it. Recorded in :attr:`locked_fields` so a §16.16 re-resolve
        doesn't re-enable them. Unknown names are ignored.
        """
        for name in names:
            widget = self.widgets.get(name)
            if widget is not None:
                _set_enabled(widget, False)
            # File inputs carry their interactive controls (upload / pick /
            # clear) outside the bound widget; their hook collapses those to
            # a read-only locked state. No-op for scalar fields.
            hook = self.lock_hooks.get(name)
            if hook is not None:
                hook()
            self.locked_fields.add(name)

    @property
    def all_groups_opened(self) -> bool:
        """True when every emitted section has been opened at least once.

        Trivially ``True`` when the form has no sections (a descriptor
        with no inputs). The workspace consults this only when the
        tool opts into the "review every section" gate; the renderer
        always tracks it regardless so the predicate is cheap to read.
        """
        return all(name in self.opened_groups for name, _ in self.groups)

    def unopened_group_labels(self) -> list[str]:
        """Labels of sections not yet opened, in render order.

        Used by the workspace to tell the user *which* sections still
        need a look when Submit is gated on "review every section".
        """
        return [label for name, label in self.groups if name not in self.opened_groups]

    def values(self) -> dict[str, Any]:
        """Snapshot the current widget values as a plain dict.

        Suitable to pass straight into
        :func:`matofx.tools.forms.validate_inputs`.
        """
        return dict(self.state)

    def submit_blockers(
        self,
        *,
        require_job_name: bool,
        job_name: str | None = None,
    ) -> list[str]:
        """Every reason Submit should stay disabled, in fix-it order.

        The single gate **both** the standalone (My Jobs) and the
        workflow (Workflow Run) submit buttons consult, so a tool's form
        gates identically wherever it is run — and any future gate added
        here is inherited by both surfaces automatically. Reasons, in
        order:

        1. **Job name** — only when ``require_job_name`` is set. The
           standalone panel names each job; a workflow auto-names its
           members and passes ``require_job_name=False``.
        2. **Required fields** — every ``required`` input that is empty,
           skipping locked upstream-fed fields the user can't fill
           (:attr:`locked_fields`).
        3. **Out-of-range / invalid values** — effective numeric-bound,
           option-membership and cross-field problems, via
           :func:`matofx.tools.forms.model.live_messages` (the same
           verdict shown inline under each field).
        4. **Unreviewed sections** — when the tool starts its groups
           collapsed and opts into ``require_all_groups_opened``.

        An empty list means Submit is clear to enable.
        """
        from matofx.tools.forms.model import live_messages  # noqa: PLC0415

        reasons: list[str] = []

        if require_job_name and not (job_name or "").strip():
            reasons.append("Enter a job name.")

        # An "empty" required value is None, the empty string, or an
        # empty list (multiselect with nothing picked). Booleans are
        # never "empty" — ``False`` is a valid submitted checkbox value.
        # Table inputs have their own completeness rule (blank / partial
        # cells), handled separately so a required grid with empty cells
        # can't sneak past the gate.
        missing: list[str] = []
        table_reasons: list[str] = []
        for spec in self.descriptor.inputs:
            if spec.name in self.locked_fields:
                continue
            value = self.state.get(spec.name)
            if spec.type == "table":
                reason = _table_incomplete_reason(spec, value if isinstance(value, list) else [])
                if reason:
                    table_reasons.append(reason)
                continue
            if not spec.required:
                continue
            if value is None or (isinstance(value, str | list) and len(value) == 0):
                missing.append(spec.label)
        if missing:
            reasons.append("Fill in required fields: " + ", ".join(missing) + ".")
        reasons.extend(table_reasons)

        # Out-of-range / invalid values — deduplicated by message so a
        # group constraint anchored to several fields reads once.
        seen: set[str] = set()
        for name, message in live_messages(self.descriptor, dict(self.state)).items():
            if name in self.locked_fields or message in seen:
                continue
            seen.add(message)
            reasons.append(message)

        # The "review every section" gate only bites when the tool starts
        # its sections collapsed: a tool that starts expanded has already
        # revealed everything, so there's nothing to force open.
        if (
            self.descriptor.input_groups_start_collapsed
            and self.descriptor.require_all_groups_opened
            and not self.all_groups_opened
        ):
            unopened = self.unopened_group_labels()
            noun = "section" if len(unopened) == 1 else "sections"
            reasons.append(f"Open and review {len(unopened)} {noun}: " + ", ".join(unopened) + ".")

        return reasons

    def seed(self, values: dict[str, Any], *, fill_missing_with_defaults: bool = False) -> None:
        """Load values into the form.

        Used by the legacy "preset / previous-job / defaults" popovers
        on the My Jobs workspace. Each entry in ``values`` whose key
        matches a rendered input updates that widget; unknown keys are
        ignored silently (a preset declared for an older descriptor
        version may carry fields the current version no longer accepts
        — failing loudly would be unfriendly).

        When ``fill_missing_with_defaults`` is True, any input the
        seed doesn't mention falls back to its descriptor default
        (used by the "Reset to defaults" popover so previously typed
        values get cleared, not preserved).
        """
        if fill_missing_with_defaults:
            seed_full = dict(self._defaults)
            seed_full.update(values)
        else:
            seed_full = dict(values)

        for name, value in seed_full.items():
            # Composite fields (the table grid) repopulate through a hook
            # that rewrites their rows in place and rebuilds — they have no
            # single ``widget.value`` to set.
            hook = self.seed_hooks.get(name)
            if hook is not None:
                hook(value)
                continue
            widget = self.widgets.get(name)
            if widget is None:
                continue
            try:
                widget.value = value
            except (ValueError, TypeError):
                # NiceGUI rejects type-mismatched values for `ui.number`
                # / `ui.checkbox` etc. A bad seed shouldn't crash the
                # form — leave the field at its current value and let
                # the user notice (the per-field error layer surfaces
                # this on submit).
                continue
            # NiceGUI's ``bind_value`` syncs widget → state through the
            # client-side reactivity loop. Programmatic seeding from the
            # server side should be authoritative immediately, so we
            # also write the state dict directly. The double-write is
            # idempotent: the binding's next sync will see the value
            # already matches.
            self.state[name] = value

        # A seeded value can satisfy a rule's condition — re-resolve so
        # the form reflects it (forced/hidden/locked fields settle to
        # their rule state instead of showing the raw seed).
        self.refresh()
        # A seed can also satisfy or break a cross-field rule (e.g. an
        # upstream snapshot whose percentages don't total 100) — surface
        # that immediately rather than waiting for the first keystroke.
        self.validate_cross_field()

    def show_errors(self, exc: FormValidationError) -> None:
        """Surface a :class:`FormValidationError` inline under each field.

        Multi-anchor errors (sum-checks) appear under every
        participating field, not just the anchor — the workspace user
        sees the whole group highlighted so the constraint is obvious.
        """
        self.clear_errors()
        for fe in exc.field_errors:
            self._mark_field(fe.name, fe.message)
            for participant in fe.sum_parameters:
                if participant != fe.name:
                    self._mark_field(participant, fe.message)

    def clear_errors(self) -> None:
        """Wipe every per-field error label."""
        for label in self.error_labels.values():
            label.text = ""
            label.visible = False

    def _mark_field(self, name: str, message: str) -> None:
        label = self.error_labels.get(name)
        if label is None:
            return
        label.text = message
        label.visible = True


# --- Public entry point --------------------------------------------------


def render_form(
    descriptor: ToolDescriptor,
    *,
    file_input_context: FileInputContext | None = None,
    material_input_context: MaterialInputContext | None = None,
    guide_images: list[GuideImageView] | None = None,
) -> FormHandle:
    """Emit the form widgets and return the :class:`FormHandle`.

    Must be called inside an active NiceGUI client / page context —
    the function uses ``ui.row()``, ``ui.column()``, ``ui.expansion()``
    and the per-type input widgets.

    ``file_input_context`` (optional) switches ``file``-typed inputs
    from a plain path-string text field to the rich drag-drop / browse
    / pick-from-previous-job widget. Threaded in by the workspace at
    submit time; left ``None`` by tests and any caller that doesn't
    have an API client to wire into.
    """
    state: dict[str, Any] = {}
    widgets: dict[str, Any] = {}
    error_labels: dict[str, ui.label] = {}
    defaults: dict[str, Any] = {}
    groups: list[tuple[str, str]] = []
    opened_groups: set[str] = set()
    field_containers: dict[str, Any] = {}
    auto_notes: dict[str, ui.label] = {}
    lock_hooks: dict[str, Callable[[], None]] = {}
    seed_hooks: dict[str, Callable[[Any], None]] = {}

    plan = _plan_layout(descriptor)
    column_widths = _flex_weights(descriptor.input_column_layout, plan.num_columns)
    start_collapsed = descriptor.input_groups_start_collapsed

    if guide_images:
        _render_guide_gallery(guide_images)

    with ui.row().classes("w-full items-start gap-4 no-wrap"):
        for col_idx in range(plan.num_columns):
            with (
                ui.column()
                .classes("gap-3")
                .style(f"flex: {column_widths[col_idx]} 1 0; min-width: 0;")
            ):
                for group_plan in plan.columns[col_idx]:
                    _render_group(
                        descriptor,
                        group_plan,
                        state=state,
                        widgets=widgets,
                        error_labels=error_labels,
                        defaults=defaults,
                        groups=groups,
                        opened_groups=opened_groups,
                        start_collapsed=start_collapsed,
                        file_input_context=file_input_context,
                        material_input_context=material_input_context,
                        field_containers=field_containers,
                        auto_notes=auto_notes,
                        lock_hooks=lock_hooks,
                        seed_hooks=seed_hooks,
                    )

    handle = FormHandle(
        state=state,
        widgets=widgets,
        error_labels=error_labels,
        descriptor=descriptor,
        groups=groups,
        opened_groups=opened_groups,
        _defaults=defaults,
        field_containers=field_containers,
        auto_notes=auto_notes,
        lock_hooks=lock_hooks,
        seed_hooks=seed_hooks,
    )

    # §16.16: only wire the live re-resolve when the tool actually has
    # rules. A rule-free tool renders and behaves exactly as before —
    # the wrapper element + (empty, hidden) note are the only structural
    # additions, and nothing observes value changes.
    if descriptor.dependencies:
        _wire_reactivity(handle)

    # Live per-field validation: out-of-range numeric entries (no longer
    # silently clamped) plus sum-checks / §F30 cross-field constraints,
    # each surfaced as a red message under the field. Wired whenever the
    # tool has a numeric field or a cross-field participant; a tool with
    # neither gains no extra change handlers.
    _wire_live_validation(handle)

    return handle


# --- Layout planning (pure functions, easy to test) ----------------------


@dataclass(frozen=True)
class _GroupPlan:
    """One group's placement and the inputs it owns.

    The pure-data shape lets us unit-test layout decisions (how groups
    are bucketed into columns, how inputs are placed into sub-columns)
    without spinning up NiceGUI.
    """

    name: str
    label: str
    column: int
    row: int
    sub_columns: int
    inputs_by_sub_column: tuple[tuple[InputField, ...], ...]

    @property
    def all_inputs(self) -> tuple[InputField, ...]:
        return tuple(field for col in self.inputs_by_sub_column for field in col)


@dataclass(frozen=True)
class _LayoutPlan:
    """The full descriptor laid out as a column-major grid of groups."""

    num_columns: int
    columns: tuple[tuple[_GroupPlan, ...], ...]


def _plan_layout(descriptor: ToolDescriptor) -> _LayoutPlan:
    """Bucket groups into columns; bucket inputs into sub-columns within groups."""
    groups = list(descriptor.input_groups)

    # Synthesise an "Other" group for ungrouped inputs so they still
    # render as an expansion. Placed in column 1 at the end.
    ungrouped = [f for f in descriptor.inputs if f.group is None]
    if ungrouped:
        max_row = max((g.row for g in groups if g.column == 1), default=0)
        groups = [
            *groups,
            InputGroup(
                name=_OTHER_GROUP_NAME,
                label=_OTHER_GROUP_LABEL,
                column=1,
                row=max_row + 1,
                sub_columns=1,
            ),
        ]

    if not groups:
        # Descriptor declared no inputs OR no groups + no ungrouped
        # fields. Render an empty single-column layout so the caller
        # still gets a valid widget tree.
        return _LayoutPlan(num_columns=1, columns=((),))

    num_columns = max(g.column for g in groups)
    by_column: list[list[_GroupPlan]] = [[] for _ in range(num_columns)]

    for group in sorted(groups, key=lambda g: (g.column, g.row)):
        plan = _plan_group(descriptor, group)
        by_column[group.column - 1].append(plan)

    return _LayoutPlan(
        num_columns=num_columns,
        columns=tuple(tuple(col) for col in by_column),
    )


def _plan_group(descriptor: ToolDescriptor, group: InputGroup) -> _GroupPlan:
    """Bucket a group's inputs into sub-columns by their ``sub_column`` field."""
    if group.name == _OTHER_GROUP_NAME:
        members = [f for f in descriptor.inputs if f.group is None]
    else:
        members = [f for f in descriptor.inputs if f.group == group.name]

    sub_cols: list[list[InputField]] = [[] for _ in range(group.sub_columns)]
    for spec in members:
        # Sub-column is 1-based; clamp to the declared range so an
        # over-sized sub_column on a single-column group still lands.
        idx = max(1, min(spec.sub_column or 1, group.sub_columns)) - 1
        sub_cols[idx].append(spec)

    return _GroupPlan(
        name=group.name,
        label=group.label,
        column=group.column,
        row=group.row,
        sub_columns=group.sub_columns,
        inputs_by_sub_column=tuple(tuple(col) for col in sub_cols),
    )


def _flex_weights(layout: dict[str, float], num_columns: int) -> list[float]:
    """Translate the descriptor's ``input_column_layout`` into flex-grow weights.

    Normalised to sum to 1, for the reason spelled out in
    :func:`matofx.ui.pages.job._flex_weights_from_layout`: ``flex-grow``
    factors summing to less than 1 hand out only that share of the free
    space, so a form whose groups collapsed to one column while a stale
    ``column_2`` weight lingered rendered at a fraction of the width.
    """
    weights: list[float] = []
    even = 1.0 / num_columns if num_columns else 1.0
    for col in range(1, num_columns + 1):
        try:
            w = float(layout.get(f"column_{col}", even))
        except (TypeError, ValueError):
            w = even
        weights.append(max(w, 0.0))
    total = sum(weights)
    if total <= 0:
        weights = [even for _ in weights]
        total = sum(weights) or 1.0
    # Normalise first, then clamp — see the job-page twin for why that
    # order matters.
    return [max(w / total, 0.05) for w in weights]


# --- Widget emission -----------------------------------------------------


def _render_group(
    descriptor: ToolDescriptor,
    group: _GroupPlan,
    *,
    state: dict[str, Any],
    widgets: dict[str, Any],
    error_labels: dict[str, ui.label],
    defaults: dict[str, Any],
    groups: list[tuple[str, str]],
    opened_groups: set[str],
    start_collapsed: bool,
    file_input_context: FileInputContext | None,
    material_input_context: MaterialInputContext | None,
    field_containers: dict[str, Any],
    auto_notes: dict[str, ui.label],
    lock_hooks: dict[str, Callable[[], None]],
    seed_hooks: dict[str, Callable[[Any], None]],
) -> None:
    """Emit one expansion containing the group's inputs."""
    _ = descriptor  # reserved for future use (e.g. cross-field hints)
    if not group.all_inputs:
        return  # don't emit empty expansions

    # Register the section so the workspace can gate Submit on "every
    # section reviewed". A section that starts expanded is already
    # revealed, so it counts as opened from render time.
    groups.append((group.name, group.label))
    if not start_collapsed:
        opened_groups.add(group.name)

    def _on_section_toggle(event: Any, _name: str = group.name) -> None:
        # ``ui.expansion`` fires this on every open AND close. We only
        # ever add — "opened once" means a later collapse must not
        # un-review the section.
        if event.value:
            opened_groups.add(_name)

    # Every group gets a thin white outline (1px @ ~35% opacity) so
    # adjacent groups read as distinct cards on the dark page chrome.
    # Matches the durable user pref "no box shadows; thin white border
    # on every box" — same line treatment ``_chrome.py`` applies to
    # ``q-card``, applied here at the group level so each expansion
    # reads as an enclosed unit.
    expansion_style = (
        "border: 1px solid rgba(255,255,255,0.35); border-radius: 4px; padding: 0.25rem 0.5rem;"
    )
    with (
        ui.expansion(group.label, value=not start_collapsed)
        .classes("w-full")
        .style(expansion_style)
    ) as expansion:
        expansion.on_value_change(_on_section_toggle)
        if group.sub_columns <= 1:
            with ui.column().classes("w-full gap-3"):
                for spec in group.inputs_by_sub_column[0]:
                    _render_field(
                        spec,
                        state=state,
                        widgets=widgets,
                        error_labels=error_labels,
                        defaults=defaults,
                        file_input_context=file_input_context,
                        material_input_context=material_input_context,
                        field_containers=field_containers,
                        auto_notes=auto_notes,
                        lock_hooks=lock_hooks,
                        seed_hooks=seed_hooks,
                    )
            return

        with ui.row().classes("w-full items-start gap-4 no-wrap"):
            for col_inputs in group.inputs_by_sub_column:
                with ui.column().classes("gap-3").style("flex: 1 1 0; min-width: 0;"):
                    for spec in col_inputs:
                        _render_field(
                            spec,
                            state=state,
                            widgets=widgets,
                            error_labels=error_labels,
                            defaults=defaults,
                            file_input_context=file_input_context,
                            material_input_context=material_input_context,
                            field_containers=field_containers,
                            auto_notes=auto_notes,
                            lock_hooks=lock_hooks,
                            seed_hooks=seed_hooks,
                        )


def _render_field(
    spec: InputField,
    *,
    state: dict[str, Any],
    widgets: dict[str, Any],
    error_labels: dict[str, ui.label],
    defaults: dict[str, Any],
    file_input_context: FileInputContext | None,
    material_input_context: MaterialInputContext | None,
    field_containers: dict[str, Any],
    auto_notes: dict[str, ui.label],
    lock_hooks: dict[str, Callable[[], None]],
    seed_hooks: dict[str, Callable[[Any], None]],
) -> None:
    """Emit one input widget + help text + auto-note + error slot.

    Widgets bind to ``state[spec.name]``; an initial value is seeded
    from the descriptor's declared default. Everything for the field
    lives inside a single wrapper element recorded in
    ``field_containers`` so a §16.16 rule can show/hide the field as a
    unit. The muted ``auto_notes`` line sits under the widget and stays
    empty/hidden until a rule sets / locks the field or changes its
    options (the "users aren't modellers" rule — never silently mutate
    without saying why).
    """
    label = _label_with_unit(spec)
    initial = _initial_value(spec)
    state[spec.name] = initial
    defaults[spec.name] = initial

    container = ui.column().classes("w-full gap-1")
    field_containers[spec.name] = container
    with container:
        if spec.type == "file" and file_input_context is not None:
            # Rich file widget: drag/drop + browse + previous-job picker
            # wrapped around a (visible-but-read-only) ``ui.input`` so
            # the rest of the form pipeline keeps reading the field's
            # path via ``state[spec.name]`` exactly the way it does for
            # every other input type. The composite UI sets
            # ``path_input.value`` on upload / pick / clear; ``bind_value``
            # propagates that to ``state``.
            path_input = _render_rich_file_field(
                spec, label, file_input_context=file_input_context, lock_hooks=lock_hooks
            )
            path_input.bind_value(state, spec.name)
            widgets[spec.name] = path_input
        elif spec.type == "material" and material_input_context is not None:
            # Rich material picker (ADR-0007 §6) wrapped around a hidden
            # ``ui.input`` so the rest of the pipeline reads the chosen
            # version id via ``state[spec.name]`` like any other field;
            # the submit path freezes its properties into inputs.json.
            value_input = _render_material_field(
                spec, label, material_input_context=material_input_context
            )
            value_input.bind_value(state, spec.name)
            widgets[spec.name] = value_input
        elif spec.type == "table" and spec.columns:
            # Editable grid: manages ``state[spec.name]`` as a list-of-dicts
            # directly (no single widget to ``bind_value``), so it's left out
            # of ``widgets`` — seed/reset/live-validation all read state.
            _render_table_field(
                spec,
                label,
                state=state,
                defaults=defaults,
                seed_hooks=seed_hooks,
                error_labels=error_labels,
            )
        else:
            widget = _build_widget(spec, label)
            widget.bind_value(state, spec.name)
            widgets[spec.name] = widget

        if spec.help_text:
            ui.label(spec.help_text).classes("text-xs").style("color: #888;")

        auto_note = ui.label("").classes("text-xs").style("color: #888;")
        auto_note.visible = False
        auto_notes[spec.name] = auto_note

        error_label = ui.label("").classes("text-xs").style("color: #c0392b;")
        error_label.visible = False
        error_labels[spec.name] = error_label


# --- §16.16 live reactivity ----------------------------------------------


def _wire_reactivity(handle: FormHandle) -> None:
    """Re-resolve the form on every value change and apply the result.

    Only called when the descriptor has rules. One shared resolver pass
    drives show/hide, enable/disable, option repopulation, forced
    values, numeric bounds, and the muted auto-note — the same
    :func:`matofx.tools.forms.resolver.resolve` the submit path uses,
    so the live form can't disagree with what submit will accept.
    """
    descriptor = handle.descriptor
    specs = {s.name: s for s in descriptor.inputs}
    guard = {"applying": False}

    def _apply() -> None:
        # Setting a forced widget value re-fires this handler; the guard
        # makes the inner call a no-op so a set_value chain can't storm.
        if guard["applying"]:
            return
        guard["applying"] = True
        try:
            effective = resolve(descriptor, dict(handle.state))
            for name, fstate in effective.fields.items():
                _apply_field_state(handle, specs.get(name), name, fstate)
        finally:
            guard["applying"] = False

    handle._reapply = _apply

    for widget in handle.widgets.values():
        on_change = getattr(widget, "on_value_change", None)
        if on_change is not None:
            on_change(lambda _e: _apply())

    # Initial pass: a field a rule already controls (e.g. hidden until
    # an opt-in is ticked) must start in its resolved state, not flash
    # its default first.
    _apply()


def _apply_field_state(
    handle: FormHandle,
    spec: InputField | None,
    name: str,
    fstate: FieldState,
) -> None:
    """Apply one field's resolved state to its live widgets."""
    container = handle.field_containers.get(name)
    if container is not None:
        container.set_visibility(fstate.visible)
    widget = handle.widgets.get(name)
    if widget is None or spec is None:
        return
    # A caller-locked field (workflow upstream-fed input) stays disabled
    # regardless of what the rule resolve would otherwise allow.
    _set_enabled(widget, not fstate.disabled and name not in handle.locked_fields)
    if spec.type in {"select", "multiselect"} and fstate.options is not None:
        _apply_select_options(widget, handle, name, spec, fstate.options)
    if fstate.forced and handle.state.get(name) != fstate.forced_value:
        widget.value = fstate.forced_value
        handle.state[name] = fstate.forced_value
    # A rule that tightens a numeric bound is *not* re-applied to the
    # widget as ``min``/``max`` props — that would re-enable NiceGUI's
    # silent blur-clamp (see ``_build_widget``). The effective bound is
    # honoured live by ``_wire_live_validation`` (which resolves it the
    # same way submit does) and shown as a message instead.
    _set_auto_note(handle.auto_notes.get(name), spec, fstate)


def _set_enabled(widget: Any, enabled: bool) -> None:
    """Enable/disable a widget across NiceGUI value-element variants."""
    setter = getattr(widget, "set_enabled", None)
    if setter is not None:
        setter(enabled)
    else:  # pragma: no cover - all widgets we build are DisableableElement
        widget.enabled = enabled


def _apply_select_options(
    widget: Any,
    handle: FormHandle,
    name: str,
    spec: InputField,
    options: list[str],
) -> None:
    """Repopulate a select / multiselect, clearing a now-invalid value.

    A value outside the new option set is reset to "unselected" (the
    §16.16 stale-option rule); effective required-ness then catches it
    at submit if the field is still required.
    """
    current = handle.state.get(name)
    if spec.type == "select":
        new_value: Any = current if current in options else None
    else:
        new_value = [v for v in (current or []) if v in options]
    setter = getattr(widget, "set_options", None)
    if setter is not None:
        setter(list(options), value=new_value)
    handle.state[name] = new_value


def _set_auto_note(
    note: ui.label | None,
    spec: InputField,
    fstate: FieldState,
) -> None:
    """Plain-English "why did this change" line; never expose the rule.

    Per the "users aren't modellers" rule we say *that* the form
    adjusted itself, never the DSL expression behind it.
    """
    if note is None:
        return
    declared_options = list(spec.options) if spec.options is not None else None
    if fstate.forced:
        message = "Set automatically based on your other answers."
    elif fstate.disabled:
        message = "Locked based on your other answers."
    elif (
        spec.type in {"select", "multiselect"}
        and fstate.options is not None
        and fstate.options != declared_options
    ):
        message = "Options updated based on your other answers."
    else:
        message = ""
    note.text = message
    note.visible = bool(message)


# --- Live cross-field validation -----------------------------------------


def _cross_field_participants(descriptor: ToolDescriptor) -> set[str]:
    """Every input name that takes part in a sum-check or §F30 constraint.

    The union drives both which widgets get a live-validation change
    handler and which error labels the live pass is allowed to touch —
    so it never clobbers an unrelated per-field error set on submit.
    """
    names = {f.name for f in descriptor.inputs}
    participants: set[str] = set()
    for spec in descriptor.inputs:
        if spec.sum_check is not None:
            participants.update(spec.sum_check.sum_parameters)
    for constraint in descriptor.cross_field_constraints:
        try:
            tree = safe_expr.parse(constraint.expression)
        except safe_expr.UnsafeExpressionError:  # pragma: no cover - install lint catches this
            continue
        participants.update(safe_expr.referenced_names(tree))
    # §16.16 validity (require) outcomes surface a live message too, so the
    # inputs they reference (and the field they're anchored to) must be
    # live-managed for the painter to set/clear that message.
    for dep in descriptor.dependencies:
        for effect in dep.effects:
            if isinstance(effect, RequireEffect):
                participants.add(effect.field)
                try:
                    tree = safe_expr.parse(effect.expr)
                except safe_expr.UnsafeExpressionError:  # pragma: no cover - install lint
                    continue
                participants.update(safe_expr.referenced_names(tree))
    return participants & names


def _wire_live_validation(handle: FormHandle) -> None:
    """Paint per-field problems live, as the user types.

    The on-screen counterpart to
    :func:`matofx.tools.forms.model.validate_inputs`: on every change to
    a numeric field or a cross-field participant it recomputes
    :func:`matofx.tools.forms.model.live_messages` and sets or clears
    each managed field's red error label. Because the verdict comes from
    the same effective-bounds / cross-field code the submit path runs,
    the live message can't disagree with what Submit will accept.

    Numeric fields are managed so an out-of-range *manual* entry is
    flagged rather than silently clamped — the widget no longer carries
    ``min`` / ``max`` (which made NiceGUI sanitise-on-blur snap the value
    back to the bound with no explanation; see :func:`_build_widget`).

    Locked fields (workflow upstream-fed inputs) are left unpainted: the
    user can't edit them and the submit path still validates them. No
    initial pass — a pristine form shows nothing until a value is
    supplied, or seeded (which calls
    :meth:`FormHandle.validate_cross_field`).
    """
    from matofx.tools.forms.model import live_messages  # noqa: PLC0415

    descriptor = handle.descriptor
    managed = _managed_live_fields(descriptor)
    if not managed:
        return

    # Names the live layer has itself painted. We only ever *clear* a
    # label we set — a per-field error surfaced by
    # :meth:`FormHandle.show_errors` on submit (e.g. "X is required")
    # must survive a later live pass it has no opinion on.
    painted: set[str] = set()

    def _run() -> None:
        messages = live_messages(descriptor, dict(handle.state))
        for name in managed:
            label = handle.error_labels.get(name)
            if label is None:
                continue
            # A locked field's value comes from upstream and can't be
            # edited here — don't nag with a red error the user can't act
            # on; submit-time validation is the backstop.
            message = "" if name in handle.locked_fields else (messages.get(name) or "")
            if message:
                label.text = message
                label.visible = True
                painted.add(name)
            elif name in painted:
                label.text = ""
                label.visible = False
                painted.discard(name)

    handle._run_cross_field = _run

    for name in managed:
        widget = handle.widgets.get(name)
        if widget is None:
            continue
        on_change = getattr(widget, "on_value_change", None)
        if on_change is not None:
            on_change(lambda _e: _run())


def _managed_live_fields(descriptor: ToolDescriptor) -> set[str]:
    """Fields whose error label the live layer owns.

    A field is live-managed only when a message is actually possible for
    it: a numeric field that carries a declared bound, any numeric field
    on a rule-bearing tool (a §16.16 rule can add a bound at runtime),
    or any cross-field participant. A bound-free, rule-free numeric input
    (and every non-numeric, non-participant field) is left alone so a
    constraint-free tool gains no live runner and a submit-time error on
    such a field is never clobbered.
    """
    if descriptor.dependencies:
        numeric = {f.name for f in descriptor.inputs if f.type in {"integer", "float"}}
    else:
        numeric = {
            f.name
            for f in descriptor.inputs
            if f.type in {"integer", "float"} and (f.minimum is not None or f.maximum is not None)
        }
    return numeric | _cross_field_participants(descriptor)


def _build_widget(spec: InputField, label: str) -> Any:
    """Pick the right NiceGUI widget for this input's declared type."""
    if spec.type in {"integer", "float"}:
        # NOTE: ``min`` / ``max`` are deliberately NOT passed. NiceGUI's
        # ``ui.number`` sanitises on blur — given a ``min``/``max`` it
        # silently *clamps* an out-of-range manual entry back to the
        # bound with no explanation. We want the user to see *why* the
        # value is rejected, so the bound is surfaced as a red message
        # under the field (live, via ``_wire_live_validation``) and the
        # Submit gate, while the authoritative
        # :func:`matofx.tools.forms.model.validate_inputs` still enforces
        # it at submit.
        return ui.number(
            label,
            value=spec.default if isinstance(spec.default, int | float) else None,
            step=spec.precision,
        ).classes("w-full")
    if spec.type == "boolean":
        return ui.checkbox(label, value=bool(spec.default))
    if spec.type == "select":
        options = list(spec.options or [])
        value = spec.default if spec.default in options else (options[0] if options else None)
        return ui.select(options, label=label, value=value).classes("w-full")
    if spec.type == "multiselect":
        options = list(spec.options or [])
        value = spec.default if isinstance(spec.default, list) else []
        return ui.select(options, label=label, multiple=True, value=value).classes("w-full")
    if spec.type == "file":
        # Fallback when no :class:`FileInputContext` was supplied (tests,
        # any non-workspace caller). Renders a plain path-string input
        # so descriptors that pre-date the rich widget still work.
        return ui.input(label, value=str(spec.default) if spec.default else "").classes("w-full")
    # string / unknown — fall through to a text input. (``table`` has its
    # own editable-grid renderer, ``_render_table_field``.)
    return ui.input(
        label,
        value=str(spec.default) if spec.default is not None else "",
    ).classes("w-full")


# Fixed cell width so header labels line up with the body inputs.
_TABLE_CELL_W = "w-32"
# A table needs the x column + at least one more before a plot is meaningful.
_TABLE_MIN_PLOT_COLS = 2


def _precision_decimals(precision: float | None) -> int:
    """Decimal places implied by a numeric column's precision (0 for >= 1)."""
    if precision is None or precision >= 1:
        return 0
    return max(0, round(-math.log10(precision)))


def _round_to_precision(value: float, precision: float | None) -> float:
    """Snap a float to the column's precision step and strip float noise."""
    if precision is None or precision <= 0:
        return value
    snapped = round(value / precision) * precision
    return round(snapped, _precision_decimals(precision))


def _blank_cell(col: TableColumn) -> Any:
    """The value a freshly-added cell starts at (column default, else empty)."""
    if col.default is not None:
        return col.default
    if col.type == "boolean":
        return False
    if col.type == "string":
        return ""
    return None  # numeric cells start blank


def _seed_table_rows(spec: InputField) -> list[dict[str, Any]]:
    """Initial rows: an explicit list default if present, else N blank rows."""
    cols = spec.columns or []
    if isinstance(spec.default, list):
        return _coerce_table_rows(spec.default, cols)
    return [{c.name: _blank_cell(c) for c in cols} for _ in range(spec.default_rows or 0)]


def _coerce_seed_cell(raw: Any, col: TableColumn) -> Any:
    """Normalise a seeded cell to its column type; bad values pass through."""
    if raw is None or raw == "":
        return "" if col.type == "string" else (False if col.type == "boolean" else None)
    if col.type in ("integer", "float"):
        try:
            num = float(raw)
        except (TypeError, ValueError):
            return raw
        return round(num) if col.type == "integer" else _round_to_precision(num, col.precision)
    if col.type == "boolean":
        return bool(raw)
    return str(raw)


def _coerce_table_rows(value: list[Any], cols: list[TableColumn]) -> list[dict[str, Any]]:
    """Turn a seeded / default list into grid rows keyed by every column."""
    out: list[dict[str, Any]] = []
    for raw in value:
        row = raw if isinstance(raw, dict) else {}
        out.append({c.name: _coerce_seed_cell(row.get(c.name), c) for c in cols})
    return out


def _table_cell_blank(cell: Any, col: TableColumn) -> bool:
    """A cell counts as unfilled when it's empty — booleans are never blank."""
    if col.type == "boolean":
        return False
    return cell is None or cell == ""


def _table_incomplete_reason(spec: InputField, rows: list[dict[str, Any]]) -> str | None:
    """Submit-gate reason a table isn't ready, or ``None`` when it's fine.

    A **required** table needs at least one row with every cell filled. A
    **partly-filled row** (some cells entered, others blank) is treated as a
    mistake in either a required or an optional table, so the gate asks the
    user to finish or clear it. Fully-blank rows in an optional table are
    left alone — the user just hasn't used that row.
    """
    cols = spec.columns or []
    if spec.required and not rows:
        return f"Add at least one row to {spec.label}."
    for row in rows:
        blanks = sum(1 for c in cols if _table_cell_blank(row.get(c.name), c))
        if blanks == 0:
            continue
        if spec.required:
            return f"Fill in every cell of {spec.label}."
        if blanks < len(cols):  # optional table: only partly-filled rows block
            return f"Finish or clear the partly-filled row in {spec.label}."
    return None


def _render_table_field(  # noqa: PLR0915 — composite grid widget; one builder by design
    spec: InputField,
    label: str,
    *,
    state: dict[str, Any],
    defaults: dict[str, Any],
    seed_hooks: dict[str, Callable[[Any], None]],
    error_labels: dict[str, ui.label],
) -> None:
    """Render a ``table`` input as an editable grid + a "View as plot" button.

    Rows live in ``state[spec.name]`` as a list-of-dicts keyed by column
    ``name`` — the same shape the tool reads back and the submit path stores.
    Cell edits mutate that list in place; add / remove rebuild the body. A
    seed hook (for presets / previous-job / reset) rewrites the rows in place
    and rebuilds. When the descriptor's ``plot_enabled`` is set, a button
    charts the rows in a modal (first column = x-axis; see
    :mod:`matofx.tools.forms.table_plot`).
    """
    cols = spec.columns or []
    rows: list[dict[str, Any]] = _seed_table_rows(spec)
    state[spec.name] = rows
    # Capture the initial rows so "Reset to defaults" can restore them
    # (a separate copy, so a reset never aliases the live rows).
    defaults[spec.name] = [dict(r) for r in rows]

    def _revalidate() -> None:
        # Paint the field's red error label with the first out-of-range /
        # wrong-type cell — the same live per-field feedback a scalar gets
        # (the Submit gate independently blocks via ``live_messages``). The
        # label is created after this builder returns, so look it up lazily.
        label_el = error_labels.get(spec.name)
        if label_el is None:
            return
        from matofx.tools.forms.model import first_table_cell_error  # noqa: PLC0415

        msg = first_table_cell_error(spec, rows) or ""
        label_el.text = msg
        label_el.visible = bool(msg)

    ui.label(label).classes("text-sm").style("font-weight: 500;")

    with ui.row().classes("w-full items-center gap-2 no-wrap"):
        for c in cols:
            head = f"{c.label} ({c.unit})" if c.unit else c.label
            ui.label(head).classes(_TABLE_CELL_W).style(
                "font-size: 0.78rem; color: var(--matofx-muted-text); font-weight: 600;"
            )
        ui.element("div").classes("w-8")  # spacer above the per-row remove button

    body = ui.column().classes("w-full gap-1")

    def _cell_widget(i: int, col: TableColumn) -> None:
        value = rows[i].get(col.name)
        # Precision drives both the step and the displayed / stored decimals so
        # a float cell can't hold more precision than its column declares.
        prec = col.precision if col.type == "float" else None

        def _set(
            e: Any, _i: int = i, _name: str = col.name, _type: str = col.type, _prec: Any = prec
        ) -> None:
            v = e.value
            if isinstance(v, int | float):
                if _type == "integer":
                    v = round(v)
                elif _type == "float":
                    v = _round_to_precision(v, _prec)
            rows[_i][_name] = v
            _revalidate()

        if col.type in ("integer", "float"):
            num_kw: dict[str, Any] = {"step": 1, "format": "%.0f"}
            if col.type == "float":
                num_kw = {"step": prec} if prec else {}
                if prec:
                    num_kw["format"] = f"%.{_precision_decimals(prec)}f"
            ui.number(value=value, on_change=_set, **num_kw).props("dense").classes(_TABLE_CELL_W)
        elif col.type == "boolean":
            with ui.element("div").classes(_TABLE_CELL_W):
                ui.checkbox(value=bool(value), on_change=_set)
        else:
            ui.input(value="" if value is None else str(value), on_change=_set).props(
                "dense"
            ).classes(_TABLE_CELL_W)

    def _rebuild() -> None:
        body.clear()
        with body:
            if not rows:
                ui.label("No rows yet — use + Row to add data.").style(
                    "color: var(--matofx-muted-text); font-size: 0.8rem;"
                )
            for i in range(len(rows)):
                with ui.row().classes("w-full items-center gap-2 no-wrap"):
                    for c in cols:
                        _cell_widget(i, c)
                    ui.button(icon="close", on_click=lambda _e=None, _i=i: _remove(_i)).props(
                        "flat round dense color=negative"
                    ).classes("w-8")

    def _remove(i: int) -> None:
        del rows[i]
        _rebuild()
        _revalidate()

    def _add() -> None:
        rows.append({c.name: _blank_cell(c) for c in cols})
        _rebuild()
        _revalidate()

    def _open_plot() -> None:
        plan_cols = [PlotColumn(c.name, c.label, c.type, c.unit) for c in cols]
        specs = plan_table_plots(plan_cols, rows)
        if not specs:
            ui.notify(
                "Nothing to plot yet — needs a numeric column and at least one row.",
                type="warning",
            )
            return
        from matofx.ui.theme import themed_plotly  # noqa: PLC0415 — avoid a tools->ui import cycle

        # A *definite* card width (not min/max) plus a fixed chart size is
        # essential: a responsive Plotly chart sizes itself to its container
        # while an auto-sized dialog sizes itself to its content, so without a
        # fixed width the two chase each other and the modal grows on open.
        with ui.dialog() as dialog, ui.card().style(
            "width: 840px; max-width: 92vw; max-height: 88vh; overflow-y: auto;"
        ):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                ui.label(f"{spec.label} — plot").style("font-weight: 600; font-size: 1.05rem;")
                ui.button(icon="close", on_click=dialog.close).props("flat round dense")
            for ps in specs:
                ui.plotly(themed_plotly(build_figure(ps))).style("width: 100%; height: 340px;")
        dialog.open()

    def _apply_seed(value: Any) -> None:
        # Rewrite the rows in place (keeping the ``state[spec.name]``
        # reference the grid holds) so a preset / previous-job / reset
        # repopulates the grid. A non-list seed leaves the grid untouched.
        if not isinstance(value, list):
            return
        rows[:] = _coerce_table_rows(value, cols)
        _rebuild()
        _revalidate()

    seed_hooks[spec.name] = _apply_seed

    with ui.row().classes("w-full items-center gap-2"):
        ui.button("+ Row", icon="add", on_click=_add).props("flat dense color=primary")
        if spec.plot_enabled and len(cols) >= _TABLE_MIN_PLOT_COLS:
            ui.button("View as plot", icon="show_chart", on_click=_open_plot).props("flat dense")

    _rebuild()


_IMAGE_EXTENSIONS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "gif", "bmp", "webp", "svg"})
"""Extensions the preview pane renders inline via :func:`ui.image`."""

_CAD_PREVIEW_EXTENSIONS: frozenset[str] = frozenset({"stl", "stp", "step", "iges", "igs", "obj"})
"""Every CAD extension the preview pane renders inline.

All five formats go through the same on-screen viewer
(:func:`matofx.ui.cad_preview.render_stl_preview`) — the server-side
``/api/jobs/{id}/cad-preview`` endpoint tessellates STP/STEP/IGES/OBJ
into binary STL on the fly, and STL passes through unchanged. The
client never sees an STP file; it always receives STL bytes."""

_PDF_PREVIEW_EXTENSIONS: frozenset[str] = frozenset({"pdf"})
"""Extensions the preview pane renders via the browser's native PDF
viewer (``<iframe src="data:application/pdf;...">``)."""

_ANIM3D_PREVIEW_EXTENSIONS: frozenset[str] = frozenset({"gltf", "glb"})
"""Animated 3-D scene formats the preview pane renders inline via
NiceGUI's ``ui.scene.gltf(...)``. Self-contained ``.glb`` is the
reliable path; ``.gltf`` (JSON) can reference external buffers that
won't resolve from a data URL — for those the user gets the model
without textures rather than nothing."""

_VIDEO_PREVIEW_EXTENSIONS: frozenset[str] = frozenset({"mp4", "webm", "ogg", "ogv", "mov", "m4v"})
"""Video formats the preview pane plays inline via HTML5
``<video controls>``."""

_CAD_NEEDS_CONVERT_EXTENSIONS: frozenset[str] = frozenset({"x_t", "x_b"})
"""CAD formats the v2 stack doesn't tessellate yet.

Parasolid (``x_t`` / ``x_b``) needs a Parasolid licence to read; not
worth the licence cost for an inline preview. These render the same
"preview pending" placeholder the rest of the unsupported types do.
Future ParaView-style viewers (volume / contour / streamlines) live
in a separate roadmap item — see ``ROADMAP.md``."""

_IMAGE_MIME_BY_EXT: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "webp": "image/webp",
    "svg": "image/svg+xml",
}


def _extension_of(path: str) -> str:
    """Return the lowercase extension (without dot), or empty string."""
    if not path or "." not in path:
        return ""
    return path.rsplit(".", 1)[-1].lower()


def _basename_of(path: str) -> str:
    """POSIX-safe last-segment of a path string."""
    if not path:
        return ""
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _format_other_job_label(entry: dict[str, Any]) -> str:
    """Render one source job as "Job 0001234 (Project, Scenario)"."""
    job_id_raw = entry.get("job_id")
    try:
        job_id_text = f"Job {int(job_id_raw):07d}"  # type: ignore[arg-type]
    except (TypeError, ValueError):
        job_id_text = "Job ?"
    project = str(entry.get("project_name") or "").strip()
    scenario = str(entry.get("scenario_name") or "").strip()
    context_bits = [p for p in (project, scenario) if p]
    context = f" ({', '.join(context_bits)})" if context_bits else ""
    return f"{job_id_text}{context}"


def _format_output_option(entry: dict[str, Any]) -> str:
    """Render one output file as "Output label  ·  filename  ·  field: name".

    The trailing ``field: <array>`` (when the output declares a nodal array
    name) lets the user tell two look-alike ``.vtu`` files apart — e.g. a
    temperature field from a failure-index field.
    """
    label = str(entry.get("output_label") or entry.get("output_name") or "Output")
    filename = str(entry.get("filename") or _basename_of(str(entry.get("value") or "")))
    text = f"{label}  ·  {filename}" if filename else label
    field_array = str(entry.get("field_array") or "").strip()
    if field_array:
        text += f"  ·  field: {field_array}"
    return text


def _open_other_output_dialog(  # noqa: PLR0915 — one cascading-picker builder by design
    candidates: list[dict[str, Any]],
    *,
    accepted_ext_set: frozenset[str],
    show_error: Callable[[str], None],
    on_confirm: Callable[[str], Awaitable[None]],
) -> None:
    """Build + open the cross-tool "use a file from another job" picker.

    Groups the flat candidate list (newest job first) into a 3-step
    tool → job → output cascade. On confirm the chosen path is validated
    against ``accepted_ext_set`` (defence in depth — the server already
    filtered by suffix) then handed to ``on_confirm``.
    """
    tools_order: list[str] = []
    tool_names: dict[str, str] = {}
    jobs_by_tool: dict[str, list[int]] = {}
    job_labels: dict[int, str] = {}
    outputs_by_job: dict[int, list[dict[str, Any]]] = {}
    for entry in candidates:
        if not entry.get("value"):
            continue
        alias = str(entry.get("tool_alias") or "")
        try:
            jid = int(entry.get("job_id") or 0)
        except (TypeError, ValueError):
            continue
        if alias not in tool_names:
            tool_names[alias] = str(entry.get("tool_name") or alias)
            tools_order.append(alias)
            jobs_by_tool[alias] = []
        if jid not in job_labels:
            job_labels[jid] = _format_other_job_label(entry)
            jobs_by_tool[alias].append(jid)
            outputs_by_job[jid] = []
        outputs_by_job[jid].append(entry)

    if not tools_order:
        ui.notify("No file outputs from other jobs match this input yet.", type="info")
        return

    picked: dict[str, str] = {"path": ""}

    with (
        ui.dialog() as dialog,
        ui.card().style("min-width: 520px; max-width: 760px;"),
    ):
        ui.label("Use a file from another job").style("font-weight: 600; font-size: 1.05rem;")
        ui.label(
            "Pick a result file produced by any job you can see — including a "
            "different tool. Choose the tool, then the job, then the output file. "
            "Only files this input accepts are listed."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

        tool_select = (
            ui.select({a: tool_names[a] for a in tools_order}, value=tools_order[0], label="Tool")
            .props("dense outlined")
            .classes("w-full")
        )
        job_select = ui.select({}, label="Job").props("dense outlined").classes("w-full")
        output_select = (
            ui.select({}, label="Output file").props("dense outlined").classes("w-full")
        )

        def _rebuild_outputs(jid: Any) -> None:
            try:
                jid_int: int | None = int(jid)
            except (TypeError, ValueError):
                jid_int = None
            outs = outputs_by_job.get(jid_int, []) if jid_int is not None else []
            opts = {str(o["value"]): _format_output_option(o) for o in outs if o.get("value")}
            output_select.set_options(opts)
            first = next(iter(opts), "")
            output_select.value = first
            picked["path"] = first

        def _rebuild_jobs(alias: str) -> None:
            jids = jobs_by_tool.get(alias, [])
            job_select.set_options({j: job_labels[j] for j in jids})
            first = jids[0] if jids else None
            job_select.value = first
            _rebuild_outputs(first)

        # Attach handlers after all three selects exist so the cascade
        # closures don't self-reference an in-progress assignment.
        tool_select.on_value_change(lambda: _rebuild_jobs(str(tool_select.value or "")))
        job_select.on_value_change(lambda: _rebuild_outputs(job_select.value))
        output_select.on_value_change(
            lambda: picked.__setitem__("path", str(output_select.value or ""))
        )
        # Seed the cascade (constructor ``value=`` does not fire on_change).
        _rebuild_jobs(tools_order[0])

        async def _confirm() -> None:
            if not picked["path"]:
                return
            if accepted_ext_set:
                ext = _extension_of(picked["path"])
                if ext not in accepted_ext_set:
                    pretty_ext = f".{ext}" if ext else "(no extension)"
                    pretty_allowed = ", ".join(f".{t}" for t in sorted(accepted_ext_set))
                    show_error(
                        f"That {pretty_ext} file isn't accepted for this input. "
                        f"Accepted file types: {pretty_allowed}."
                    )
                    return
            dialog.close()
            await on_confirm(picked["path"])

        with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Use this file", on_click=_confirm).props("color=primary")
    dialog.open()


def _render_rich_file_field(  # noqa: PLR0915 — composite file widget; one builder by design
    spec: InputField,
    label: str,
    *,
    file_input_context: FileInputContext,
    lock_hooks: dict[str, Callable[[], None]] | None = None,
) -> Any:
    """Compose drag/drop + browse + previous-job picker + preview pane.

    Layout (50/50 horizontal split, matching the legacy Streamlit
    form's ``st.columns(2)`` pattern):

    .. code-block::

        ┌────────────────────────────────┬────────────────────────────────┐
        │ <label>                        │ <preview pane>                 │
        │                                │                                │
        │ [Drop file here] [Browse]      │  - Image previews inline       │
        │ [Use file from previous job]   │  - STL via ``ui.scene.stl``    │
        │  -- OR, when a file is set: -- │  - STP/STEP "preview pending"  │
        │ Selected: file.png  [Clear]    │  - Other types: no preview     │
        └────────────────────────────────┴────────────────────────────────┘

    The returned ``ui.input`` is the bound state holder — the rest of
    the form pipeline reads / writes ``state[spec.name]`` through it
    exactly the same way as for any other text field. The composite UI
    around it sets ``path_input.value = ...`` on upload / pick / clear,
    which propagates via NiceGUI's bind_value into ``state``.

    Modes are mutually exclusive: once a file is set (either uploaded
    or picked from a previous job), the upload widget + "Use file from
    previous job" button collapse and a single "Clear" button takes
    their place. Clearing **resets the underlying** ``ui.upload``
    widget AND wipes the server-side draft so the next upload starts
    from a clean slate — both the bytes on disk and the form's
    visible state get fully reset, addressing the locked 2026-05-14
    feedback that the previous version "looked confusing after Clear".
    """
    field_name = spec.name
    initial_value = str(spec.default) if spec.default else ""
    accept_csv = (
        ",".join(f".{ext}" for ext in (spec.accepted_file_types or []))
        if spec.accepted_file_types
        else ""
    )
    max_bytes = (
        int(spec.max_file_size_mb * 1024 * 1024)
        if spec.max_file_size_mb and spec.max_file_size_mb > 0
        else None
    )

    # In-memory cache of the most recently uploaded bytes. Keeps the
    # preview pane independent of the server fetch path for the
    # upload mode (the user just dropped bytes in — we already have
    # them, no need for a round-trip).
    upload_bytes_cache: dict[str, bytes] = {}
    # Display label for a "Pick from library" selection (the field's
    # value is the opaque version sentinel, so the chip text is cached
    # here at pick time).
    library_label_cache: dict[str, str] = {}

    container = (
        ui.column()
        .classes("w-full gap-1 q-pa-sm")
        .style("border: 1px solid var(--matofx-card-border); border-radius: 4px;")
    )
    with container:
        ui.label(label).style("font-weight: 500; font-size: 0.9rem;")
        # 50/50 layout per durable user pref: the file action area on
        # the left, the preview pane on the right at equal flex.
        split_row = ui.row().classes("w-full items-start gap-3 no-wrap")
        with split_row:
            left_col = ui.column().classes("gap-2").style("flex: 1 1 0; min-width: 0;")
            right_col = (
                ui.column().classes("gap-2 items-center").style("flex: 1 1 0; min-width: 0;")
            )

        with left_col:
            # The path holder — driven programmatically by upload /
            # pick / clear callbacks. ``readonly`` so the user can't
            # smuggle in a typed path; the rich UX is the only way
            # values land in this field. We keep it visible (no
            # ``display:none``) but disabled so a user can copy the
            # selected path if they need to — useful for debugging.
            path_input = (
                ui.input(label="Selected file path", value=initial_value)
                .props("readonly dense outlined hide-bottom-space")
                .classes("w-full")
            )
            error_label = ui.label("").style("color: var(--matofx-error-text); font-size: 0.85rem;")
            error_label.visible = False
            # Shown when the field is locked (wired from an upstream tool):
            # the upload / pick / clear controls collapse and this read-only
            # note takes their place, matching how scalar inputs lock.
            locked_note = ui.label(
                "Provided automatically by an upstream tool — it can't be changed here."
            ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
            locked_note.visible = False

            mode_a = ui.column().classes("w-full gap-2")
            mode_b = ui.row().classes("items-center gap-2 q-mt-xs")

    # Hold the upload widget reference so the Clear path can call
    # ``.reset()`` on it — without that the q-uploader keeps its own
    # internal "uploaded files" panel visible even after the form
    # state is wiped, which is the "Clear is confusing" complaint
    # from 2026-05-14.
    upload_widget_holder: dict[str, Any] = {"widget": None}
    # Set True once the field is locked (wired from an upstream tool); the
    # redraw then keeps every interactive control hidden regardless of value.
    locked_state: dict[str, bool] = {"on": False}

    def _show_error(message: str) -> None:
        error_label.text = message
        error_label.visible = True

    def _clear_error() -> None:
        error_label.text = ""
        error_label.visible = False

    def _set_value(new_path: str, *, cached_bytes: bytes | None = None) -> None:
        """Write the new path into the bound input + cache any uploaded bytes."""
        if cached_bytes is not None:
            upload_bytes_cache[field_name] = cached_bytes
        elif new_path == "":
            upload_bytes_cache.pop(field_name, None)
            library_label_cache.pop(field_name, None)
        path_input.value = new_path
        _redraw()

    def _redraw() -> None:
        current = str(path_input.value or "")
        if locked_state["on"]:
            # Locked (upstream-wired): no upload / pick / clear — the value
            # is provided at submit by the workflow, not chosen here.
            mode_a.set_visibility(False)
            mode_b.set_visibility(False)
            locked_note.visible = True
        elif current:
            mode_a.set_visibility(False)
            mode_b.set_visibility(True)
        else:
            mode_a.set_visibility(True)
            mode_b.set_visibility(False)
        # Preview rebuild — always reset + re-render so an old
        # preview never leaks into a new selection.
        right_col.clear()
        with right_col:
            _render_preview(current)

    def _render_preview(current_path: str) -> None:  # noqa: PLR0911 — one branch per preview type
        """Right-hand pane content. Empty when no file is selected."""
        if not current_path:
            ui.label("Preview will appear here once a file is selected.").style(
                "color: var(--matofx-muted-text); font-size: 0.85rem; "
                "padding: 1.5rem 1rem; text-align: center;"
            )
            return
        if is_library_ref(current_path):
            # A library pick carries an opaque version sentinel, not a
            # path — show the cached label instead of trying to preview.
            ui.icon("folder_shared").style("font-size: 2.5rem; color: var(--matofx-muted-text);")
            ui.label(library_label_cache.get(field_name, "Library file")).style(
                "font-weight: 500; font-size: 0.9rem;"
            )
            ui.label(
                "Picked from the file library — the chosen version is fetched "
                "and passed to the tool when you submit."
            ).style("color: var(--matofx-muted-text); font-size: 0.8rem; text-align: center;")
            return
        ext = _extension_of(current_path)
        if ext in _IMAGE_EXTENSIONS:
            _render_image_preview(current_path, ext)
            return
        if ext in _CAD_PREVIEW_EXTENSIONS:
            _render_cad_preview(current_path, ext)
            return
        if ext in _PDF_PREVIEW_EXTENSIONS:
            _render_bytes_preview(
                current_path,
                ext,
                kind="pdf",
                loading_text="Loading PDF preview…",
            )
            return
        if ext in _ANIM3D_PREVIEW_EXTENSIONS:
            _render_bytes_preview(
                current_path,
                ext,
                kind="animation_3d",
                loading_text="Loading 3-D animation preview…",
            )
            return
        if ext in _VIDEO_PREVIEW_EXTENSIONS:
            _render_bytes_preview(
                current_path,
                ext,
                kind="video",
                loading_text="Loading video preview…",
            )
            return
        if ext in _CAD_NEEDS_CONVERT_EXTENSIONS:
            from matofx.ui.cad_preview import render_cad_placeholder  # noqa: PLC0415

            render_cad_placeholder(current_path, ext)
            return
        # Generic fallback — show the filename + a hint.
        ui.icon("description").style("font-size: 2.5rem; color: var(--matofx-muted-text);")
        ui.label(_basename_of(current_path)).style("font-weight: 500; font-size: 0.9rem;")
        ui.label(
            "No inline preview for this file type — the upload is saved and "
            "will be passed to the tool as-is."
        ).style("color: var(--matofx-muted-text); font-size: 0.8rem; text-align: center;")

    def _render_image_preview(current_path: str, ext: str) -> None:
        """Render an inline image preview from the upload cache or via fetch.

        Image styling uses ``object-fit: contain`` (via Quasar's
        ``fit="contain"`` prop on ``q-img``) so a non-square upload
        shows in full — without it the default ``cover`` mode crops
        edges, which surfaced as "I see the image has a border cut
        out all around" in 2026-05-14 feedback.
        """
        cached = upload_bytes_cache.get(field_name)
        if cached is not None:
            _embed_image(cached, ext, current_path)
            return
        # No cached bytes (previous-job pick) — fetch via the API.
        placeholder = ui.label("Loading preview…").style(
            "color: var(--matofx-muted-text); font-size: 0.85rem;"
        )

        async def _load() -> None:
            try:
                blob = await file_input_context.fetch_preview(current_path)
            except Exception:
                placeholder.text = "Couldn't load preview."
                return
            placeholder.delete()
            with right_col:
                _embed_image(blob, ext, current_path)

        ui.timer(0.0, _load, once=True)

    def _embed_image(blob: bytes, ext: str, current_path: str) -> None:
        """Common shape for both upload-cache and fetched-from-API previews."""
        import base64  # noqa: PLC0415

        mime = _IMAGE_MIME_BY_EXT.get(ext, "application/octet-stream")
        data_url = f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"
        # Quasar's q-img default is ``fit="cover"`` which crops the
        # edges to fill the box; we explicitly switch to ``contain``
        # so the whole image renders inside the height bound. Width
        # is full of the right column; height capped at 320 px so a
        # tall image doesn't push the form group offscreen.
        ui.image(data_url).props("fit=contain").classes("w-full").style(
            "max-height: 320px; height: 320px; background: transparent;"
        )
        ui.label(_basename_of(current_path)).style(
            "color: var(--matofx-muted-text); font-size: 0.8rem;"
        )

    def _render_bytes_preview(
        current_path: str,
        extension: str,
        *,
        kind: str,
        loading_text: str,
    ) -> None:
        """Inline PDF / GLTF-GLB / video preview from cache or via fetch.

        Same shape as :func:`_render_image_preview`: prefer the
        in-memory upload cache (the bytes the user just dropped in),
        fall through to the authenticated file-preview endpoint for
        the previous-job-pick path. ``kind`` selects which shared
        renderer in :mod:`matofx.ui.cad_preview` does the embedding.
        """
        from matofx.ui.cad_preview import (  # noqa: PLC0415
            render_gltf_preview,
            render_pdf_preview,
            render_video_preview,
        )

        def _embed(blob: bytes) -> None:
            if kind == "pdf":
                render_pdf_preview(blob, height=320)
            elif kind == "animation_3d":
                render_gltf_preview(blob, extension=extension, height=300)
            else:
                render_video_preview(blob, extension=extension, height=300)
            ui.label(_basename_of(current_path)).style(
                "color: var(--matofx-muted-text); font-size: 0.8rem;"
            )

        cached = upload_bytes_cache.get(field_name)
        if cached is not None:
            _embed(cached)
            return
        placeholder = ui.label(loading_text).style(
            "color: var(--matofx-muted-text); font-size: 0.85rem;"
        )

        async def _load() -> None:
            try:
                blob = await file_input_context.fetch_preview(current_path)
            except Exception as exc:
                detail = getattr(exc, "detail", None) or str(exc)
                placeholder.text = f"Couldn't load preview: {detail}"
                return
            placeholder.delete()
            with right_col:
                _embed(blob)

        ui.timer(0.0, _load, once=True)

    def _render_cad_preview(current_path: str, extension: str) -> None:
        """Inline 3-D preview for any of the five supported CAD formats.

        Always fetches STL bytes via the server-side ``cad-preview``
        endpoint, which converts STP/STEP/IGES/OBJ on the fly and
        passes STL through unchanged. The client only ever calls
        ``ui.scene.stl`` (via :func:`matofx.ui.cad_preview.render_stl_preview`)
        — one renderer, one data URL, regardless of source format.

        The local upload-bytes cache **isn't** used here because the
        cached bytes are the *source* format, not STL — a freshly-
        uploaded STP file would need server-side conversion just like
        the previous-job-pick case. Always going through the endpoint
        keeps both paths identical and the cache hit lives on the
        server (content-hash LRU in :mod:`matofx.tools.cad_convert`).
        """
        from matofx.ui.cad_preview import render_stl_preview  # noqa: PLC0415

        ui.label(_basename_of(current_path)).style(
            "color: var(--matofx-muted-text); font-size: 0.8rem;"
        )
        placeholder = ui.label(
            "Loading 3-D preview…"
            if extension == "stl"
            else f"Tessellating .{extension.upper()} file…"
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")

        async def _load() -> None:
            try:
                stl_bytes = await file_input_context.fetch_cad_preview(current_path)
            except Exception as exc:
                # Surface the server's detail when present so the
                # operator sees the actual conversion error (file
                # too big / unsupported format / OCP not installed)
                # rather than a generic Python repr.
                detail = getattr(exc, "detail", None) or str(exc)
                placeholder.text = f"Couldn't load 3-D preview: {detail}"
                return
            placeholder.delete()
            with right_col:
                render_stl_preview(stl_bytes, height=300)

        ui.timer(0.0, _load, once=True)

    accepted_ext_set: frozenset[str] = frozenset(
        ext.lower() for ext in (spec.accepted_file_types or [])
    )

    def _reset_upload_chip() -> None:
        """Drop the file the user just dragged in from the uploader's UI.

        Called whenever we reject an upload client-side, so the q-uploader
        doesn't display a "selected" chip for a file we refused to send
        to the server.
        """
        widget = upload_widget_holder.get("widget")
        if widget is None:
            return
        with contextlib.suppress(Exception):
            widget.reset()

    async def _on_upload(e: Any) -> None:
        _clear_error()
        try:
            data = await e.file.read()
            filename = e.file.name
        except Exception as exc:
            _show_error(f"Upload failed: {exc}")
            _reset_upload_chip()
            return
        # Client-side extension check. Drag-and-drop bypasses the
        # HTML ``accept`` filter so a user can drop a .gif onto a
        # field that only takes .png / .jpg / .bmp; refuse those
        # here with a friendly message rather than letting the
        # server's 400 propagate via a generic toast. The server
        # still re-checks — this widget guard is a UX courtesy, not
        # the authority.
        if accepted_ext_set:
            ext = _extension_of(filename)
            if ext not in accepted_ext_set:
                pretty_ext = f".{ext}" if ext else "(no extension)"
                pretty_allowed = ", ".join(f".{t}" for t in sorted(accepted_ext_set))
                _show_error(
                    f"That {pretty_ext} file isn't accepted for this input. "
                    f"Accepted file types: {pretty_allowed}."
                )
                _reset_upload_chip()
                return
        if max_bytes is not None and len(data) > max_bytes:
            _show_error(
                f"That file is {len(data):,} bytes — bigger than the "
                f"{max_bytes:,}-byte limit for this input."
            )
            _reset_upload_chip()
            return
        try:
            new_path = await file_input_context.upload(field_name, filename, data)
        except Exception as exc:
            # Pull the server-side ``detail`` out of an ApiError-shaped
            # exception if we got one, so the user sees the server's
            # plain-English explanation (e.g. the file-type rejection
            # message) instead of a Python repr.
            detail = getattr(exc, "detail", None) or str(exc)
            _show_error(f"Server rejected the upload — {detail}")
            _reset_upload_chip()
            return
        _set_value(new_path, cached_bytes=data)

    def _on_rejected(_e: Any) -> None:
        # Triggered when the q-uploader itself refuses a file (max
        # files / max-file-size — and on some browsers, the HTML
        # ``accept`` filter). Surface the same hint the upload path
        # uses so the message is consistent across rejection sources.
        hint_bits: list[str] = []
        if spec.accepted_file_types:
            hint_bits.append(
                "accepted types: " + ", ".join(f".{t}" for t in spec.accepted_file_types)
            )
        if spec.max_file_size_mb:
            hint_bits.append(f"max size: {spec.max_file_size_mb} MB")
        hint = "; ".join(hint_bits) or "check the file's type and size."
        _show_error(f"That file was rejected — {hint}")
        _reset_upload_chip()

    async def _on_clear() -> None:
        _clear_error()
        try:
            await file_input_context.clear(field_name)
        except Exception as exc:
            _show_error(f"Couldn't clear the file: {exc}")
            return
        upload_bytes_cache.pop(field_name, None)
        # Reset the uploader's own UI so the file chip / progress bar
        # the user dropped in disappears — the previous version left
        # those visible which read as "still there".
        widget = upload_widget_holder.get("widget")
        if widget is not None:
            # Older NiceGUI versions don't expose .reset(); best-effort.
            with contextlib.suppress(Exception):
                widget.reset()
        _set_value("")

    # Cache of "previous-job entries for this field" so the button-
    # gating async fetch doesn't need a second round-trip when the
    # user actually clicks the button. Refreshed on click if empty.
    previous_entries_cache: dict[str, list[dict[str, Any]]] = {"entries": []}

    def _format_history_label(entry: dict[str, Any]) -> str:
        """Render one entry as "filename · Job 0001234 (Project, Scenario)"."""
        value = str(entry.get("value") or "")
        job_id_raw = entry.get("job_id")
        if job_id_raw is None:
            job_id_text = "Job ?"
        else:
            try:
                job_id_text = f"Job {int(job_id_raw):07d}"
            except (TypeError, ValueError):
                job_id_text = "Job ?"
        project = str(entry.get("project_name") or "").strip()
        scenario = str(entry.get("scenario_name") or "").strip()
        context_bits = [p for p in (project, scenario) if p]
        context = f" ({', '.join(context_bits)})" if context_bits else ""
        return f"{_basename_of(value)}  ·  {job_id_text}{context}"

    async def _on_pick_previous() -> None:
        _clear_error()
        candidates = previous_entries_cache.get("entries") or []
        if not candidates:
            try:
                candidates = await file_input_context.list_previous(field_name)
            except Exception as exc:
                _show_error(f"Couldn't load previous files: {exc}")
                return
            previous_entries_cache["entries"] = candidates
        if not candidates:
            ui.notify(
                "No files from previous jobs of this tool to pick from yet.",
                type="info",
            )
            return

        with (
            ui.dialog() as dialog,
            ui.card().style("min-width: 480px; max-width: 720px;"),
        ):
            ui.label("Pick a file from a previous job").style(
                "font-weight: 600; font-size: 1.05rem;"
            )
            ui.label(
                "These are files you've previously used for this input on "
                "another job of the same tool. Selecting one re-uses the "
                "file without a fresh upload."
            ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
            picked: dict[str, str] = {"path": ""}
            options = {
                str(entry.get("value") or ""): _format_history_label(entry)
                for entry in candidates
                if entry.get("value")
            }
            ui.radio(
                options,
                on_change=lambda e: picked.__setitem__("path", str(e.value)),
            ).props("dense").classes("w-full")

            async def _confirm() -> None:
                if not picked["path"]:
                    return
                # Gate previous-job picks by the same accepted-types
                # whitelist so a user can't pick e.g. a .stp file
                # into a field that only takes .png / .jpg. The
                # previous job's path was permitted under whatever
                # accepted types existed at submission time; tightening
                # the descriptor since then would otherwise let stale
                # values back in. Refuses with the same friendly
                # message the upload path uses.
                if accepted_ext_set:
                    ext = _extension_of(picked["path"])
                    if ext not in accepted_ext_set:
                        pretty_ext = f".{ext}" if ext else "(no extension)"
                        pretty_allowed = ", ".join(f".{t}" for t in sorted(accepted_ext_set))
                        _show_error(
                            f"That {pretty_ext} file isn't accepted for this "
                            f"input. Accepted file types: {pretty_allowed}."
                        )
                        return
                dialog.close()
                # Persist the picked path through the same upload-clear
                # contract: there's no on-disk file to delete (we
                # didn't upload anything), but tell the server-side
                # draft to wipe any stale draft upload for this field
                # so the queue-time promote step doesn't shadow the
                # picked path with whatever was uploaded before.
                # Best-effort — a stale draft upload is recovered at
                # queue time (promote falls through silently).
                with contextlib.suppress(Exception):
                    await file_input_context.clear(field_name)
                widget = upload_widget_holder.get("widget")
                if widget is not None:
                    with contextlib.suppress(Exception):
                        widget.reset()
                upload_bytes_cache.pop(field_name, None)
                _set_value(picked["path"])

            with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Use this file", on_click=_confirm).props("color=primary")
        dialog.open()

    def _format_library_label(c: dict[str, Any]) -> str:
        """Render one library candidate as "name (vN) · scope · filename"."""
        name = str(c.get("name") or "Library file")
        ordinal = c.get("ordinal")
        head = f"{name} (v{ordinal})" if ordinal else name
        bits = [head]
        scope = str(c.get("scope") or "").strip()
        if scope:
            bits.append(scope)
        filename = str(c.get("original_filename") or "").strip()
        if filename:
            bits.append(filename)
        return "  ·  ".join(bits)

    async def _on_pick_library() -> None:
        _clear_error()
        list_library = file_input_context.list_library
        if list_library is None:
            return
        try:
            candidates = await list_library(field_name)
        except Exception as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            _show_error(f"Couldn't load the file library — {detail}")
            return
        if not candidates:
            ui.notify("No library files are available for this input yet.", type="info")
            return

        with (
            ui.dialog() as dialog,
            ui.card().style("min-width: 480px; max-width: 720px;"),
        ):
            ui.label("Pick from the file library").style("font-weight: 600; font-size: 1.05rem;")
            ui.label(
                "Governed, versioned files assigned to this tool field. Selecting "
                "one re-uses its current published version without a fresh upload."
            ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
            picked: dict[str, Any] = {"version_id": None, "label": ""}
            labels = {int(c.get("version_id") or 0): _format_library_label(c) for c in candidates}

            def _on_choice(e: Any) -> None:
                vid = int(e.value)
                picked["version_id"] = vid
                picked["label"] = labels.get(vid, "Library file")

            ui.radio(labels, on_change=_on_choice).props("dense").classes("w-full")

            async def _confirm() -> None:
                if picked["version_id"] is None:
                    return
                dialog.close()
                # No on-disk draft upload to manage; clear any stale one
                # so the queue-time promote doesn't shadow the library
                # pick (best-effort — recovered at queue time anyway).
                with contextlib.suppress(Exception):
                    await file_input_context.clear(field_name)
                widget = upload_widget_holder.get("widget")
                if widget is not None:
                    with contextlib.suppress(Exception):
                        widget.reset()
                upload_bytes_cache.pop(field_name, None)
                library_label_cache[field_name] = str(picked["label"])
                _set_value(build_library_ref(int(picked["version_id"])))

            with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Use this file", on_click=_confirm).props("color=primary")
        dialog.open()

    async def _apply_other_output(path: str) -> None:
        """Adopt a cross-tool picked output as this field's value."""
        # Clear any stale draft upload so the queue-time promote doesn't
        # shadow the picked path (best-effort — recovered at queue time).
        with contextlib.suppress(Exception):
            await file_input_context.clear(field_name)
        widget = upload_widget_holder.get("widget")
        if widget is not None:
            with contextlib.suppress(Exception):
                widget.reset()
        upload_bytes_cache.pop(field_name, None)
        _set_value(path)

    async def _on_pick_other_output() -> None:
        _clear_error()
        list_other = file_input_context.list_other_outputs
        if list_other is None:
            return
        try:
            candidates = await list_other(
                list(spec.accepted_file_types or []), spec.expected_field_array
            )
        except Exception as exc:
            _show_error(f"Couldn't load files from other jobs: {exc}")
            return
        if not candidates:
            ui.notify(
                "No file outputs from other jobs match this input yet.",
                type="info",
            )
            return
        _open_other_output_dialog(
            candidates,
            accepted_ext_set=accepted_ext_set,
            show_error=_show_error,
            on_confirm=_apply_other_output,
        )

    with mode_a:
        ui.label(
            "Drop a file here, or click to browse — or use a file from a previous run of this tool."
        ).style("color: var(--matofx-muted-text); font-size: 0.85rem;")
        upload_widget = ui.upload(
            on_upload=_on_upload,
            on_rejected=_on_rejected,
            auto_upload=True,
            max_files=1,
        ).classes("w-full")
        upload_widget_holder["widget"] = upload_widget
        accept_clause = f'accept="{accept_csv}" ' if accept_csv else ""
        upload_widget.props(f"{accept_clause}flat dense")
        if max_bytes is not None:
            upload_widget._props["max-file-size"] = max_bytes
        with ui.row().classes("w-full items-center gap-2"):
            # Pass the async function directly. Wrapping in
            # ``asyncio.create_task`` from a sync click handler fails
            # silently because there's no running loop bound to the
            # click handler — NiceGUI accepts async on_click handlers
            # natively, so this is the right pattern.
            previous_job_button = ui.button(
                "Use file from a previous job",
                icon="history",
                on_click=_on_pick_previous,
            ).props("flat color=primary")
            # Render the button hidden by default. A background
            # fetch below resolves "are there any candidates for
            # this field?" and only then makes the button visible —
            # avoids dangling a dead-end action for fresh installs
            # or tools the user hasn't run before.
            previous_job_button.set_visibility(False)
            # The 4th source mode (ADR-0006 §6) — shown only when the
            # file_library feature is on (the context callback is set).
            # Unlike the previous-job button it stays visible: the
            # dialog itself reports an empty library, and the feature
            # being on is the gate.
            if file_input_context.list_library is not None:
                ui.button(
                    "Pick from library",
                    icon="folder_shared",
                    on_click=_on_pick_library,
                ).props("flat color=primary")
            # "Use a file from another job" (cross-tool): reuse any visible
            # job's result file as this input — e.g. a standalone Bracket
            # Stress run consuming Thermal Soak's temperature field. Stays
            # visible when the mode is on; the dialog itself reports an empty
            # set. Sits alongside the same-tool previous-job button.
            if file_input_context.list_other_outputs is not None:
                ui.button(
                    "Use a file from another job",
                    icon="cable",
                    on_click=_on_pick_other_output,
                ).props("flat color=primary")

    async def _check_has_previous() -> None:
        """Reveal the previous-job button only if there's something to pick.

        Fires once on first render via :func:`ui.timer(once=True)`. The
        fetched entries are cached so the click handler doesn't pay a
        second round-trip when the user actually opens the picker.
        """
        try:
            entries = await file_input_context.list_previous(field_name)
        except Exception:
            entries = []
        previous_entries_cache["entries"] = entries
        if entries:
            previous_job_button.set_visibility(True)

    ui.timer(0.0, _check_has_previous, once=True)

    with mode_b:
        ui.button(
            "Clear",
            icon="close",
            on_click=_on_clear,
        ).props("flat dense color=warning")

    def _apply_lock() -> None:
        """Collapse the widget to its read-only 'provided by upstream' state."""
        locked_state["on"] = True
        _redraw()

    if lock_hooks is not None:
        lock_hooks[field_name] = _apply_lock

    _redraw()
    return path_input


def _format_material_label(c: dict[str, Any]) -> str:
    """Render one material candidate as "name (vN) · class"."""
    name = str(c.get("name") or "Material")
    ordinal = c.get("version_ordinal")
    head = f"{name} (v{ordinal})" if ordinal else name
    cls = str(c.get("class_name") or "").strip()
    return f"{head}  ·  {cls}" if cls else head


def _render_picker_properties_table(props_map: dict[str, Any]) -> None:
    """Render a compact ``property · value · unit`` table for one candidate.

    ``props_map`` is the ``MaterialPickerCandidate.properties`` dict
    keyed by ``prop_name`` (the shape :meth:`MaterialsService.list_for_picker`
    returns). Sorted by property name so the rows read the same across
    candidates of the same class.
    """
    rows = []
    for name in sorted(props_map.keys()):
        p = props_map[name] or {}
        rows.append(
            {
                "prop_name": str(p.get("prop_name") or name),
                "value": p.get("value"),
                "unit": str(p.get("unit") or ""),
            }
        )
    ui.table(
        columns=[
            {"name": "prop_name", "label": "Property", "field": "prop_name", "align": "left"},
            {"name": "value", "label": "Value", "field": "value"},
            {"name": "unit", "label": "Unit", "field": "unit"},
        ],
        rows=rows,
    ).classes("w-full").props("dense flat")


def _render_material_field(  # noqa: PLR0915 — composite picker widget; one builder by design
    spec: InputField, label: str, *, material_input_context: MaterialInputContext
) -> Any:
    """Compose a "Pick material" picker around a bound value holder (ADR-0007 §6).

    The returned (hidden) ``ui.input`` holds the chosen
    ``material_version_id`` as a string so the rest of the form pipeline
    reads it via ``state[spec.name]`` like any other field; the submit
    resolver coerces it + freezes the version's properties into
    ``inputs.json``. Mirrors the file widget's bound-holder pattern.
    """
    material_class = spec.material_class or ""
    required_props = list(spec.required_properties or []) or None
    label_cache: dict[str, str] = {}
    # Per-version cache of property dicts captured at pick time. Lets
    # the widget show the chosen material's values without an extra
    # round-trip; a reloaded draft (no cache) just shows the name +
    # "(re-pick to see properties)".
    properties_cache: dict[str, dict[str, Any]] = {}

    container = (
        ui.column()
        .classes("w-full gap-1 q-pa-sm")
        .style("border: 1px solid var(--matofx-card-border); border-radius: 4px;")
    )
    with container:
        ui.label(label).style("font-weight: 500; font-size: 0.9rem;")
        # Hidden bound holder — the rest of the pipeline reads state via this.
        value_input = ui.input().props("readonly").style("display: none;")
        row = ui.row().classes("w-full items-center gap-2")
        properties_box = ui.column().classes("w-full gap-1")
        error_label = ui.label("").style("color: var(--matofx-error-text); font-size: 0.85rem;")
        error_label.visible = False

    def _current_label() -> str:
        cur = str(value_input.value or "")
        return label_cache.get(cur, f"Material #{cur}") if cur else ""

    def _on_clear() -> None:
        cur = str(value_input.value or "")
        if cur:
            properties_cache.pop(cur, None)
        value_input.value = ""
        _redraw()

    async def _on_pick() -> None:
        error_label.visible = False
        try:
            candidates = await material_input_context.list_materials(material_class, required_props)
        except Exception as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            error_label.text = f"Couldn't load materials — {detail}"
            error_label.visible = True
            return
        if not candidates:
            ui.notify(f"No published {material_class} materials to pick from yet.", type="info")
            return
        with (
            ui.dialog() as dialog,
            ui.card().style("min-width: 560px; max-width: 820px;"),
        ):
            ui.label(f"Pick a {material_class} material").style(
                "font-weight: 600; font-size: 1.05rem;"
            )
            ui.label("Click a card to select. Expand it to see the property values.").style(
                "color: var(--matofx-muted-text); font-size: 0.8rem;"
            )
            picked: dict[str, Any] = {"vid": None, "label": ""}
            labels = {int(c.get("version_id") or 0): _format_material_label(c) for c in candidates}
            # Track per-card visual state so selection repaints cheaply.
            cards: dict[int, Any] = {}
            icons: dict[int, Any] = {}
            _unselected_style = "border: 1px solid var(--matofx-card-border);"
            _selected_style = "border: 2px solid var(--q-primary);"

            def _select(vid: int) -> None:
                picked["vid"] = vid
                picked["label"] = labels.get(vid, "")
                for v, card in cards.items():
                    card.style(_selected_style if v == vid else _unselected_style)
                for v, icon in icons.items():
                    icon.props(
                        f"name={'radio_button_checked' if v == vid else 'radio_button_unchecked'}"
                    )

            for c in candidates:
                vid = int(c.get("version_id") or 0)
                with (
                    ui.card()
                    .classes("w-full cursor-pointer")
                    .style(_unselected_style)
                    .on("click", lambda _e=None, _vid=vid: _select(_vid)) as card_el
                ):
                    cards[vid] = card_el
                    with ui.row().classes("w-full items-center gap-2 no-wrap"):
                        icons[vid] = ui.icon("radio_button_unchecked").style(
                            "color: var(--matofx-muted-text);"
                        )
                        ui.label(labels[vid]).style("font-weight: 500;")
                    props_map = c.get("properties") or {}
                    if props_map:
                        # Collapsed by default — operator expands to peek.
                        with (
                            ui.expansion("Show properties", icon="science")
                            .classes("w-full")
                            .props("dense")
                        ):
                            _render_picker_properties_table(props_map)

            def _confirm() -> None:
                if picked["vid"] is None:
                    return
                dialog.close()
                vid_key = str(picked["vid"])
                label_cache[vid_key] = str(picked["label"])
                # Cache the chosen candidate's properties so the widget
                # can render them under the field without a refetch.
                chosen = next(
                    (c for c in candidates if int(c.get("version_id") or 0) == picked["vid"]),
                    None,
                )
                if chosen is not None:
                    properties_cache[vid_key] = dict(chosen.get("properties") or {})
                value_input.value = vid_key
                _redraw()

            with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                ui.button("Cancel", on_click=dialog.close).props("flat")
                ui.button("Use this material", on_click=_confirm).props("color=primary")
        dialog.open()

    def _redraw() -> None:
        row.clear()
        with row:
            if str(value_input.value or ""):
                ui.icon("dataset").style("color: var(--matofx-muted-text);")
                ui.label(_current_label()).style("font-weight: 500;")
                ui.button(icon="close", on_click=_on_clear).props(
                    "flat dense color=warning"
                ).tooltip("Clear")
            else:
                ui.label("No material selected.").style(
                    "color: var(--matofx-muted-text); font-size: 0.85rem;"
                )
                ui.button("Pick material", icon="dataset", on_click=_on_pick).props(
                    "flat color=primary"
                )
        _redraw_properties()

    def _redraw_properties() -> None:
        """Show the picked material's properties under the field (collapsed)."""
        properties_box.clear()
        cur = str(value_input.value or "")
        if not cur:
            return
        props = properties_cache.get(cur)
        with properties_box:
            if props:
                with (
                    ui.expansion("Show properties", icon="science").classes("w-full").props("dense")
                ):
                    _render_picker_properties_table(props)
            else:
                # Reloaded draft (no cache hit) — give the user a nudge.
                ui.label("Re-pick the material to see its properties.").style(
                    "color: var(--matofx-muted-text); font-size: 0.78rem;"
                )

    _redraw()
    return value_input


def _label_with_unit(spec: InputField) -> str:
    """Build a field's display label: ``"Temperature * (degC)"``.

    A required input gets a trailing ``*`` (placed before the unit), the
    install-wide convention for mandatory fields; the unit is appended in
    parentheses when present.
    """
    base = f"{spec.label} *" if spec.required else spec.label
    if spec.unit:
        return f"{base} ({spec.unit})"
    return base


def _initial_value(spec: InputField) -> Any:
    """Pick the initial widget value: descriptor default, else type-shaped empty."""
    if spec.default is not None:
        return spec.default
    if spec.type == "boolean":
        return False
    if spec.type == "multiselect":
        return []
    return None
