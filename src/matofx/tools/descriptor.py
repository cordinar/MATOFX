"""Tool descriptor schema.

The descriptor is the contract between a tool author and MATOFX. It
declares everything the framework needs to render the tool's input
form, validate user input, stage the bundle into a job working
directory, allocate the right resources, run it, capture outputs,
notify the right people, route approvals, and clean up afterwards.

The shape is a port of the legacy two-source model (``*_descriptors.json``
+ the ``tools`` DB table). Both sources are unified into a single
Pydantic model so there's one place to look and one place to validate.

Per the project rules, descriptors **never** mention Docker or any
container internals. They declare *what* the tool needs at a high
level (a Python runtime, GPU memory, CPU cores, third-party licences,
etc.); MATOFX synthesises images in v1.0 (Phase 2 of the build).

## Top-level fields

- Identity: ``alias``, ``name``, ``version``, ``short_description``
  (mandatory, card-sized), ``long_description`` (optional, fuller),
  ``teams``.
- Bundle: ``main_script``, ``icon_filename``, ``presets_filename``,
  ``retain_files``, ``delete_io_files``.
- Inputs: ``inputs``, ``input_groups``, ``input_column_layout``.
- Outputs: ``outputs``, ``output_groups``, ``output_column_layout``.
- Resources: ``resources`` (CPU / RAM / GPU memory).
- Licences: ``licences_required`` — list of named licences (with
  optional counts) drawn from the install's licence manager.
- Execution: ``execution_backend`` (kind + backend-specific fields).
- Workflow hints: ``downstream_tools``.
- Approvals: ``approvals_policy`` (``none`` / ``pre`` / ``post`` /
  ``both``) + ``approver_usernames``.
- Notifications: ``email_notifications`` default.
- Scheduling: ``duration_class`` — **reserved**; stored and preserved
  but no scheduler reads it today and the tool editor no longer
  exposes it (wiring it into lane ordering is a future item).
- Surrogate provenance: ``surrogate_model_version_id`` — set only on
  synthesised surrogate-tools (ADR-0008 §7.4); ``None`` otherwise.
"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _reject_unsafe_bundle_path(value: str, *, field: str) -> str:
    """Reject a bundle-relative filename that could escape the bundle dir.

    The descriptor's filename fields (``main_script``, ``icon_filename``,
    ``presets_filename``, and each guide image's ``filename``) are joined
    onto the tool's on-disk bundle directory and then executed (the
    launcher) or served verbatim (the icon / guide-image endpoints). A
    value containing ``..``, an absolute root, a Windows drive, a
    backslash, or a NUL byte could resolve outside the bundle, so it is
    rejected at validation time — the same guard
    :func:`matofx.tools.service` applies to editable file paths, hoisted
    onto the descriptor so a traversal value can never be persisted in
    the first place. Returns the stripped value on success.
    """
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} can't be empty or whitespace")
    if "\x00" in cleaned:
        raise ValueError(f"{field} must not contain NUL bytes")
    if "\\" in cleaned:
        raise ValueError(f"{field} must use forward slashes, not backslashes")
    if PureWindowsPath(cleaned).drive:
        raise ValueError(f"{field} must not contain a drive letter")
    posix_parts = PurePosixPath(cleaned).parts
    win_parts = PureWindowsPath(cleaned).parts
    if any(part == ".." for part in posix_parts + win_parts):
        raise ValueError(f"{field} must not contain '..' path segments")
    if PurePosixPath(cleaned).is_absolute() or PureWindowsPath(cleaned).is_absolute():
        raise ValueError(f"{field} must be a bundle-relative path, not absolute")
    return cleaned


# --- Type aliases ---------------------------------------------------------

InputFieldType = Literal[
    "string",
    "integer",
    "float",
    "boolean",
    "select",
    "multiselect",
    "file",
    "table",
    "material",
]
OutputFieldType = Literal["number", "string", "file", "table"]
TableColumnType = Literal["string", "integer", "float", "boolean"]
DurationClass = Literal["snappy", "medium", "long", "very_long"]
ApprovalsPolicy = Literal["none", "pre", "post", "both"]
NumericWidget = Literal["field", "slider"]
"""How a numeric input renders in the form: a numeric textbox or a slider."""

ChoiceWidget = Literal["dropdown", "radio"]
"""How a select / multiselect input renders: a dropdown list or a row of radio buttons."""

ExecutionBackendKind = Literal[
    "subprocess",
    "hpc_batch",
    "aos_calculator",
    "aos_repeat_calculator",
    "web_app",
]
"""Execution backend kinds (port of legacy ``job_type``).

- ``subprocess`` — Phase 1 default. Run the tool's ``main_script`` in a
  curated subprocess. Phase 2 wraps this in synthesised containers.
- ``hpc_batch`` — submit to an HPC scheduler (Slurm / Rescale / HPC
  Pack). Requires ``queue``.
- ``aos_calculator`` — Always-On-Server calculator. Requires ``server``.
- ``aos_repeat_calculator`` — same, but for tools that loop without a
  final exit (legacy "AOS - repeat calculator").
- ``web_app`` — tool is a long-running web service; MATOFX records the
  reference but doesn't execute. Requires ``url`` and ``port``.
"""

CURRENT_DESCRIPTOR_SCHEMA_VERSION = 1
"""Bump when the field set changes incompatibly; tools record their target version."""

SHORT_DESCRIPTION_MAX = 200
"""Character cap on ``ToolDescriptor.short_description`` — small enough to keep
the home-page Quick Access card tidy. Mirrored by migration ``0080`` and the
admin tool forms; anything longer spills into ``long_description``."""


FLOAT_PRECISION_CHOICES: tuple[float, ...] = (1.0, 0.1, 0.01, 0.001, 0.0001, 0.00001, 0.000001)
"""Decimal-place increments offered for ``float`` inputs in the wizard.

Steps span 0 dp (``1.0``, whole numbers) through 6 dp. The descriptor
doesn't enforce membership of this tuple at validation time — a tool
author shipping ``descriptor.toml`` by hand can still pick an arbitrary
precision — but the wizard restricts the operator to these values for
a consistent visual scale.
"""


# --- Frozen base ----------------------------------------------------------


class _Frozen(BaseModel):
    """Frozen base — extra fields rejected so typos can't hide."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


# --- Inputs ---------------------------------------------------------------


class SumCheck(_Frozen):
    """Cross-field constraint: a set of inputs must sum to a fixed value.

    Used for things like component percentages that must total 100.
    Names listed in ``sum_parameters`` must exist as siblings in the
    same descriptor's ``inputs``.
    """

    value: float
    sum_parameters: list[str] = Field(min_length=2)


class TableColumn(_Frozen):
    """One column inside a ``type == "table"`` input or output field.

    The wizard's table sub-editor lets the operator declare a fixed set
    of columns up front. At job-submit time the user fills in rows; the
    tool reads them back as a list-of-dicts keyed by ``name``. Output
    tables use the same shape — the runner writes a list-of-dicts that
    the result page renders as a grid.

    ``type`` is restricted to scalar types — nested tables aren't
    supported. ``string`` cells are free-text; ``integer`` / ``float``
    use the same numeric input as scalar fields; ``boolean`` renders
    as a checkbox.
    """

    name: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    type: TableColumnType = "string"

    unit: str | None = None
    """Optional unit string — same shape as :class:`InputField.unit`."""

    minimum: float | None = None
    """For numeric columns: lower bound (inclusive)."""

    maximum: float | None = None
    """For numeric columns: upper bound (inclusive)."""

    precision: float | None = None
    """For numeric columns: increment / decimal step.

    Integer columns must use ``precision = 1.0`` if set; float columns
    pick a step in :data:`FLOAT_PRECISION_CHOICES`.
    """

    default: Any = None
    """Per-column default cell value. Validated against ``type``."""

    @field_validator("name")
    @classmethod
    def _name_is_safe_identifier(cls, v: str) -> str:
        if not v[0].isalpha():
            raise ValueError("Table column name must start with a letter")
        if not all(c.isalnum() or c == "_" for c in v):
            raise ValueError("Table column name may only contain letters, digits, and underscores")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> TableColumn:
        if self.type == "integer" and self.precision is not None and self.precision != 1.0:
            raise ValueError(
                f"Table column {self.name!r} is an integer; precision must be 1 (not "
                f"{self.precision!r})"
            )
        if self.type == "integer":
            for bound_name, bound in (("minimum", self.minimum), ("maximum", self.maximum)):
                if bound is not None and not float(bound).is_integer():
                    raise ValueError(
                        f"Table column {self.name!r} is an integer; {bound_name} must be a "
                        f"whole number (got {bound!r})"
                    )
        if self.default is not None:
            self._validate_default()
        return self

    def _validate_default(self) -> None:
        d = self.default
        if self.type == "string" and not isinstance(d, str):
            raise ValueError(f"Table column {self.name!r} default must be a string")
        if self.type == "boolean" and not isinstance(d, bool):
            raise ValueError(f"Table column {self.name!r} default must be a boolean")
        if self.type == "integer" and (isinstance(d, bool) or not isinstance(d, int)):
            raise ValueError(f"Table column {self.name!r} default must be an integer")
        if self.type == "float" and (isinstance(d, bool) or not isinstance(d, int | float)):
            raise ValueError(f"Table column {self.name!r} default must be numeric")


class InputField(_Frozen):
    """One input the tool accepts.

    Renders as a single form field in the UI. Layout (``group``,
    ``sub_column``) is honoured by the framework's form renderer.
    """

    name: str = Field(min_length=1, max_length=64)
    """Programmatic identifier; ``[a-z][a-z0-9_]*`` by convention."""

    label: str = Field(min_length=1, max_length=128)
    """Human-readable label shown in the form."""

    type: InputFieldType
    """Field type. ``select`` / ``multiselect`` require ``options``."""

    unit: str | None = None
    """Optional unit string (e.g. ``"mm"``, ``"K"``, ``"degC"``)."""

    required: bool = True

    default: Any = None
    """Default value; type must match ``type``. ``None`` means no pre-fill."""

    options: list[str] | None = None
    """For ``select`` / ``multiselect``: the allowed values.

    Validated at construction: empty strings rejected, duplicates
    rejected. The list is preserved in input order (tool authors
    sometimes use ordering to guide the user — most-common first).
    """

    minimum: float | None = None
    """For numeric fields: lower bound (inclusive)."""

    maximum: float | None = None
    """For numeric fields: upper bound (inclusive)."""

    precision: float | None = None
    """For numeric fields: increment / decimal step (e.g. ``0.01``)."""

    group: str | None = None
    """Name of the :class:`InputGroup` this field belongs to."""

    sub_column: int | None = Field(default=None, ge=1)
    """1-based sub-column within the group (for two-column groups)."""

    downstream_input: bool = False
    """When True, the field is populated from ``upstream_tool``'s outputs."""

    upstream_tool: str | None = None
    """Tool alias whose output supplies this field; required when ``downstream_input`` is True."""

    upstream_output: str | None = None
    """Name of the upstream tool's output that feeds this field.

    The tool author picks the specific output to wire in (by its
    human-readable label, but the *name* is what's stored), so the
    binding no longer relies on the downstream input and the upstream
    output happening to share a name. ``None`` keeps backward
    compatibility with descriptors authored before explicit output
    selection existed: the wiring + workflow-run paths fall back to
    matching on this field's own ``name`` (the legacy convention).
    Only meaningful when ``downstream_input`` is True.
    """

    sum_check: SumCheck | None = None
    """Cross-field sum constraint (e.g. component percentages totalling 100)."""

    columns: list[TableColumn] | None = None
    """For ``type == "table"``: the column shape the operator will fill in.

    Each column declares its own scalar type. Required for table inputs
    (validator refuses an empty list); ignored for non-table types.
    """

    default_rows: int | None = Field(default=None, ge=0, le=1024)
    """For ``type == "table"``: how many empty rows the form starts with.

    The user can add or remove rows at job-submit time; this is just
    the initial count. ``None`` means "no pre-filled rows" (equivalent
    to 0 — kept distinct so a wizard edit that explicitly sets 0 is
    distinguishable from the field being absent on legacy descriptors).
    """

    plot_enabled: bool = True
    """For ``type == "table"``: whether the job form offers a "View as
    plot" button that charts the entered rows.

    The first column is the shared x-axis; each numeric column after it is
    a dependent series (see :mod:`matofx.tools.forms.table_plot`). Default
    on; ignored for non-table inputs.
    """

    help_text: str | None = None
    """Inline help shown next to the field; per F30 must be plain English."""

    widget: NumericWidget = "field"
    """For ``integer`` / ``float``: render as a textbox (``field``) or a slider.

    Tool authors picking ``slider`` should also set ``minimum`` and
    ``maximum`` so the slider has a fixed range. Ignored for non-numeric
    types — kept on the model regardless so descriptors round-trip
    cleanly.
    """

    choice_widget: ChoiceWidget = "dropdown"
    """For ``select`` / ``multiselect``: render as a dropdown or as radio buttons.

    Radio buttons read better at low option counts (~five or fewer) and
    surface every choice without a click; dropdowns are the right call
    for long option lists. Ignored for non-choice types.
    """

    accepted_file_types: list[str] | None = None
    """For ``file`` inputs: extensions the form will accept (e.g. ``["csv", "xlsx"]``).

    Stored without leading dots; the form renderer prepends ``"."`` when
    building the browser ``accept`` attribute. ``None`` means no
    restriction — any file type the OS allows.
    """

    max_file_size_mb: float | None = None
    """For ``file`` inputs: upload size cap in megabytes; ``None`` means no cap."""

    expected_field_array: str | None = Field(default=None, max_length=128)
    """For ``file`` inputs that consume a **result field**: the nodal array the
    tool expects the file to carry (e.g. ``"temperature"``).

    Matched against an upstream/prior job's file output ``field_node_array_name``
    so the "use a file from another job" picker only offers field files the tool
    can actually read — a temperature-field input never lists a failure-index
    field, even though both are ``.vtu``. Advisory: it filters the picker; it
    doesn't restrict a manual upload or a typed path. ``None`` means no
    field-level filtering (any type-compatible file is offered). Only valid on a
    ``file`` input.
    """

    material_class: str | None = None
    """For ``material`` inputs: the required material-class name (e.g. ``"Fluid"``, ADR-0007).

    The form picker filters to published materials of this class. The
    name is resolved against the live class schema at tool-install time
    (:class:`matofx.tools.errors.UnknownMaterialClassError`); the chosen
    material *version* is validated + frozen into ``inputs.json`` at
    submit. Required for — and only valid on — a ``material`` input. The
    descriptor declares a *requirement*, never an enumerated catalog, so
    a ``material`` field carries no ``options`` and no ``default``.
    """

    required_properties: list[str] | None = None
    """For ``material`` inputs: the properties the tool will read.

    ``None`` defaults to the class's ``required`` properties. When given,
    each name must be a property the class declares (checked at install
    via :class:`matofx.tools.errors.UnknownMaterialPropertyError`) and is
    validated against the chosen material version at submit. Only valid on
    a ``material`` input.
    """

    @field_validator("name")
    @classmethod
    def _name_is_safe_identifier(cls, v: str) -> str:
        if not v[0].isalpha():
            raise ValueError("Input name must start with a letter")
        if not all(c.isalnum() or c == "_" for c in v):
            raise ValueError("Input name may only contain letters, digits, and underscores")
        return v

    @field_validator("accepted_file_types")
    @classmethod
    def _accepted_file_types_clean(cls, v: list[str] | None) -> list[str] | None:
        """Strip leading dots, lowercase, and reject empty entries."""
        if v is None:
            return v
        cleaned: list[str] = []
        for ext in v:
            stripped = ext.strip().lstrip(".").lower()
            if not stripped:
                raise ValueError("Accepted file extensions cannot be empty or whitespace-only")
            cleaned.append(stripped)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Accepted file extensions must be unique")
        return cleaned

    @field_validator("options")
    @classmethod
    def _options_clean_unique(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        cleaned = [opt.strip() for opt in v]
        if any(not opt for opt in cleaned):
            raise ValueError("Option values cannot be empty or whitespace-only")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Option values must be unique")
        return cleaned

    @field_validator("required_properties")
    @classmethod
    def _required_properties_clean_unique(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        cleaned = [p.strip() for p in v]
        if any(not p for p in cleaned):
            raise ValueError("required_properties entries cannot be empty or whitespace-only")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("required_properties must be unique")
        return cleaned

    @model_validator(mode="after")
    def _consistency(self) -> InputField:
        self._validate_choice_and_table_shape()
        self._validate_integer_constraints()
        self._validate_widget_constraints()
        self._validate_file_constraints()
        self._validate_material_constraints()
        if self.downstream_input and self.upstream_tool is None:
            raise ValueError(
                f"Input {self.name!r} marks downstream_input=True but has no upstream_tool"
            )
        if self.default is not None and self.type not in {"table", "material"}:
            self._validate_default()
        return self

    def _validate_material_constraints(self) -> None:
        """Shape rules for ``material`` inputs (ADR-0007 §3).

        A ``material`` field must declare a ``material_class`` and, by the
        frozen-options rule, may not enumerate ``options`` or carry a
        ``default`` (the chosen material is install-specific, picked at
        submit). ``material_class`` / ``required_properties`` are valid
        only on a ``material`` input.
        """
        if self.type == "material":
            if not self.material_class:
                raise ValueError(
                    f"Input {self.name!r} is a material field but declares no material_class"
                )
            if self.options:
                raise ValueError(
                    f"Input {self.name!r} is a material field and can't enumerate options — "
                    "it declares a required class, not a fixed catalog"
                )
            if self.default is not None:
                raise ValueError(
                    f"Input {self.name!r} is a material field and can't carry a default — "
                    "the chosen material is install-specific and picked at submit time"
                )
            return
        if self.material_class is not None:
            raise ValueError(f"Input {self.name!r} sets material_class but is not a material input")
        if self.required_properties is not None:
            raise ValueError(
                f"Input {self.name!r} sets required_properties but is not a material input"
            )

    def _validate_choice_and_table_shape(self) -> None:
        if self.type in {"select", "multiselect"} and not self.options:
            raise ValueError(f"Input {self.name!r} is a {self.type} field but has no options")
        if self.type == "table" and not self.columns:
            raise ValueError(
                f"Input {self.name!r} is a table but has no columns — table inputs need "
                "at least one column declared in the descriptor"
            )
        if self.type == "table" and self.columns:
            names = [c.name for c in self.columns]
            if len(names) != len(set(names)):
                raise ValueError(f"Input {self.name!r} table columns must have unique names")

    def _validate_integer_constraints(self) -> None:
        if self.type != "integer":
            return
        if self.precision is not None and self.precision != 1.0:
            raise ValueError(
                f"Input {self.name!r} is an integer; precision must be 1 (not {self.precision!r})"
            )
        for bound_name, bound in (("minimum", self.minimum), ("maximum", self.maximum)):
            if bound is not None and not float(bound).is_integer():
                raise ValueError(
                    f"Input {self.name!r} is an integer; {bound_name} must be a whole "
                    f"number (got {bound!r})"
                )

    def _validate_widget_constraints(self) -> None:
        if self.widget == "slider" and self.type not in {"integer", "float"}:
            raise ValueError(
                f"Input {self.name!r} requests the slider widget but is not numeric "
                f"(type={self.type!r})"
            )
        if self.choice_widget == "radio" and self.type not in {"select", "multiselect"}:
            raise ValueError(
                f"Input {self.name!r} requests the radio widget but is not select/multiselect "
                f"(type={self.type!r})"
            )

    def _validate_file_constraints(self) -> None:
        if self.accepted_file_types is not None and self.type != "file":
            raise ValueError(
                f"Input {self.name!r} sets accepted_file_types but is not a file input"
            )
        if self.max_file_size_mb is not None and self.type != "file":
            raise ValueError(f"Input {self.name!r} sets max_file_size_mb but is not a file input")
        if self.expected_field_array is not None and self.type != "file":
            raise ValueError(
                f"Input {self.name!r} sets expected_field_array but is not a file input"
            )
        if self.max_file_size_mb is not None and self.max_file_size_mb <= 0:
            raise ValueError(
                f"Input {self.name!r} max_file_size_mb must be positive (got "
                f"{self.max_file_size_mb!r}); use None for no cap"
            )

    def _validate_default(self) -> None:
        """Check that ``default`` matches ``type`` and ``options``.

        Called from :meth:`_consistency`; raises :class:`ValueError`
        when the default disagrees with the declared type. Permissive
        for numeric types: an integer literal is acceptable as a
        ``float`` default.
        """
        d = self.default
        if self.type == "string" and not isinstance(d, str):
            raise ValueError(f"Input {self.name!r} default must be a string")
        if self.type == "boolean" and not isinstance(d, bool):
            raise ValueError(f"Input {self.name!r} default must be a boolean")
        if self.type == "integer" and (isinstance(d, bool) or not isinstance(d, int)):
            raise ValueError(f"Input {self.name!r} default must be an integer")
        if self.type == "float" and (isinstance(d, bool) or not isinstance(d, int | float)):
            raise ValueError(f"Input {self.name!r} default must be numeric")
        if self.type == "select":
            if not isinstance(d, str):
                raise ValueError(f"Input {self.name!r} default must be one of its options")
            if self.options and d not in self.options:
                raise ValueError(f"Input {self.name!r} default {d!r} is not in its options")
        if self.type == "multiselect":
            if not isinstance(d, list) or not all(isinstance(item, str) for item in d):
                raise ValueError(f"Input {self.name!r} default must be a list of option values")
            if self.options and any(item not in self.options for item in d):
                raise ValueError(f"Input {self.name!r} default contains values not in its options")
        if self.type == "file" and not isinstance(d, str):
            raise ValueError(f"Input {self.name!r} default must be a filename string")


class InputGroup(_Frozen):
    """Visual grouping of input fields in the form.

    ``column`` and ``row`` form a grid layout (1-based). ``sub_columns``
    splits the group into N internal columns; child fields then declare
    ``sub_column`` to pick one.
    """

    name: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    column: int = Field(ge=1)
    row: int = Field(ge=1)
    sub_columns: int = Field(default=1, ge=1, le=4)


# --- Conditional dependencies (§16.16) ------------------------------------

OnHidePolicy = Literal["drop", "keep_default", "keep_last"]
"""What happens to a hidden field's value (the §16.16 value-state matrix).

- ``drop`` (default) — value discarded, field not required, omitted
  from ``inputs.json``.
- ``keep_default`` — value forced to the field's descriptor default.
- ``keep_last`` — whatever the user last entered is preserved (written
  out, never required-checked).
"""


class ShowEffect(_Frozen):
    """Reveal one or more inputs while the rule's condition holds."""

    kind: Literal["show"] = "show"
    fields: list[str] = Field(min_length=1)


class HideEffect(_Frozen):
    """Hide one or more inputs while the rule's condition holds.

    ``on_hide`` selects the value-state policy for *every* field this
    effect hides (the §16.16 matrix); the tool author picks per rule.
    """

    kind: Literal["hide"] = "hide"
    fields: list[str] = Field(min_length=1)
    on_hide: OnHidePolicy = "drop"


class SetOptionsEffect(_Frozen):
    """Replace a ``select`` / ``multiselect`` field's allowed options.

    Option-sets are enumerated here, never derived at run time — the
    DSL does no I/O, so the schema stays frozen and the evaluator pure.
    """

    kind: Literal["set_options"] = "set_options"
    field: str = Field(min_length=1, max_length=64)
    options: list[str] = Field(min_length=1)

    @field_validator("options")
    @classmethod
    def _options_clean_unique(cls, v: list[str]) -> list[str]:
        cleaned = [opt.strip() for opt in v]
        if any(not opt for opt in cleaned):
            raise ValueError("Option values cannot be empty or whitespace-only")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Option values must be unique")
        return cleaned


class SetRequiredEffect(_Frozen):
    """Toggle whether a field must be filled while the condition holds."""

    kind: Literal["set_required"] = "set_required"
    field: str = Field(min_length=1, max_length=64)
    required: bool


class SetDefaultEffect(_Frozen):
    """Override a field's pre-filled default while the condition holds.

    The user can still change the field; this only changes the value
    the form starts from. Membership for ``select`` targets is enforced
    at submit against the *effective* options, not here — options may
    themselves be rule-controlled.

    Set *either* a literal ``value`` *or* a ``value_expr`` (the locked
    DSL over sibling inputs, e.g. ``base_length / 2``), never both. An
    expression is evaluated against the current values each time the
    form resolves.
    """

    kind: Literal["set_default"] = "set_default"
    field: str = Field(min_length=1, max_length=64)
    value: Any = None
    value_expr: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _value_xor_expr(self) -> SetDefaultEffect:
        if self.value_expr is not None and self.value is not None:
            raise ValueError(f"{self.kind} for {self.field!r}: give a value or a formula, not both")
        return self


class SetMinMaxEffect(_Frozen):
    """Override a numeric field's bounds while the condition holds.

    Each bound is *either* a literal (``minimum`` / ``maximum``) *or* a
    formula over sibling inputs (``minimum_expr`` / ``maximum_expr``) —
    so an author can say "B's minimum is always A" with
    ``minimum_expr = "a"``. A formula bound is evaluated against the
    current values each time the form resolves, and is the mechanism by
    which a relationship like ``B > A`` is enforced everywhere (live
    form, submit, and the studies / optimisation candidate generators
    drop a point that falls outside the resolved bound).
    """

    kind: Literal["set_min_max"] = "set_min_max"
    field: str = Field(min_length=1, max_length=64)
    minimum: float | None = None
    maximum: float | None = None
    minimum_expr: str | None = Field(default=None, min_length=1, max_length=1024)
    maximum_expr: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _at_least_one_bound(self) -> SetMinMaxEffect:
        if self.minimum is not None and self.minimum_expr is not None:
            raise ValueError(
                f"set_min_max for {self.field!r}: give the minimum as a number or a formula, "
                "not both"
            )
        if self.maximum is not None and self.maximum_expr is not None:
            raise ValueError(
                f"set_min_max for {self.field!r}: give the maximum as a number or a formula, "
                "not both"
            )
        has_min = self.minimum is not None or self.minimum_expr is not None
        has_max = self.maximum is not None or self.maximum_expr is not None
        if not has_min and not has_max:
            raise ValueError("set_min_max needs at least one of minimum / maximum")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError(
                f"set_min_max minimum ({self.minimum}) is above maximum ({self.maximum})"
            )
        return self


class SetValueEffect(_Frozen):
    """Force a field to a value while the condition holds.

    Orthogonal to :class:`DisableEffect` — pair them to force *and*
    lock; use ``set_value`` alone to overwrite an editable field. Set
    *either* a literal ``value`` *or* a ``value_expr`` (DSL over sibling
    inputs, e.g. ``a + 1``), never both.
    """

    kind: Literal["set_value"] = "set_value"
    field: str = Field(min_length=1, max_length=64)
    value: Any = None
    value_expr: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def _value_xor_expr(self) -> SetValueEffect:
        if self.value_expr is not None and self.value is not None:
            raise ValueError(f"{self.kind} for {self.field!r}: give a value or a formula, not both")
        return self


class RequireEffect(_Frozen):
    """A per-input validity rule: ``expr`` must be true, else block.

    Authored inside an input's Dependencies card (so ``field`` is that
    input, used to anchor/highlight the error), this is the "show a
    message instead of auto-adjusting" outcome. ``expr`` is the locked
    DSL over sibling inputs; gated by the owning rule's ``when``. A
    falsy result blocks submission and drops the point in studies /
    optimisation. ``message`` is the author's plain-English copy; when
    omitted a readable message is generated from the inputs' labels.
    The raw expression is never shown to the user.
    """

    kind: Literal["require"] = "require"
    field: str = Field(min_length=1, max_length=64)
    expr: str = Field(min_length=1, max_length=1024)
    message: str | None = Field(default=None, max_length=512)


class DisableEffect(_Frozen):
    """Lock a field (read-only) while the condition holds.

    Without a companion :class:`SetValueEffect` the field keeps its
    current value but the user can't change it; the value is still
    written out and still required-checked.
    """

    kind: Literal["disable"] = "disable"
    field: str = Field(min_length=1, max_length=64)


Effect = Annotated[
    ShowEffect
    | HideEffect
    | SetOptionsEffect
    | SetRequiredEffect
    | SetDefaultEffect
    | SetMinMaxEffect
    | SetValueEffect
    | RequireEffect
    | DisableEffect,
    Field(discriminator="kind"),
]
"""One thing a rule does to the form when its condition holds."""


class Dependency(_Frozen):
    """One conditional rule: ``when`` true → apply ``effects``.

    ``when`` is the locked cross-field DSL (see
    :mod:`matofx.tools.forms.safe_expr`); it may reference only sibling
    inputs by name. Rules chain (a rule reads the post-rule value of
    any field another rule controls); the descriptor's
    :meth:`ToolDescriptor` validators refuse a cyclic rule graph at
    install. When several rules write the same field the **last
    matching rule in declaration order wins**.
    """

    when: str = Field(min_length=1, max_length=1024)
    effects: list[Effect] = Field(min_length=1)


class CrossFieldConstraint(_Frozen):
    """A submit-time validity rule spanning several inputs (§F30).

    ``expression`` is the same locked DSL as a dependency ``when``
    (see :mod:`matofx.tools.forms.safe_expr`) and may reference only
    sibling inputs by name. It is evaluated server-side at submit:
    a falsy result fails the submission with ``error_message`` —
    the tool author's plain-English copy — surfaced under the
    participating fields. Unlike :class:`SumCheck` (a fixed-shape sum
    equality) this is an arbitrary boolean expression, e.g.
    ``operating_temperature <= max_safe_temperature``.

    The raw expression is never shown to the user (§F30 clarifier 3);
    only ``error_message`` is.
    """

    expression: str = Field(min_length=1, max_length=1024)
    error_message: str = Field(min_length=1, max_length=512)


# --- Outputs --------------------------------------------------------------

_FIELD_FORMATS: frozenset[str] = frozenset({"npy", "vtk", "vtu", "vtp", "cgns", "case"})
"""Snapshot formats a ``file_type == "field"`` output may declare (ADR-0008 §4.4).

Kept in lock-step with ``matofx.studies.surrogate.field`` (``.npy`` on numpy
alone; the rest via pyvista). Defined here so descriptor validation has no
import dependency on the studies package."""


class OutputField(_Frozen):
    """One output the tool produces (number or file).

    Numbers are scalar values picked out of the tool's output JSON.
    Files are written to the job's working directory; ``filename``
    points to them, and the renderer uses ``file_type`` to pick a
    viewer (HTML embed, CSV plot, etc.).
    """

    name: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    type: OutputFieldType
    unit: str | None = None
    precision: float | None = None
    """For numeric outputs: display precision."""

    group: str | None = None
    """Name of the :class:`OutputGroup` this field belongs to."""

    file_type: str | None = None
    """For file outputs: renderer hint (``html_plot``, ``csv_plot``, etc.)."""

    filename: str | None = None
    """For file outputs: relative path to the file in the job's working dir."""

    # Plot-specific fields for ``csv_plot`` outputs (port from legacy).
    # Column indexes are **0-indexed** — the first column in the CSV is
    # ``0``, the second is ``1``, and so on. ``None`` falls back to the
    # renderer's default (X=0, Y=1).
    xaxis_column: int | None = Field(default=None, ge=0)
    yaxis_column: int | None = Field(default=None, ge=0)
    plot_title: str | None = None
    xaxis_title: str | None = None
    yaxis_title: str | None = None

    columns: list[TableColumn] | None = None
    """For ``type == "table"``: column metadata for the rendered grid."""

    # KPI extraction (ADR-0012 / `designs/kpi-extraction.md` §2.2).
    # Tool authors mark scalar-numeric outputs as KPIs; the runner's
    # post-completion hook captures the value into ``job_kpi_value``;
    # the ``/results/{tool_alias}`` page surfaces them across all jobs.
    # ``kpi = true`` is only valid on ``type == "number"`` outputs —
    # enforced by ``_kpi_consistency`` below.
    kpi: bool = False
    """Whether this output is a headline KPI for the results warehouse.

    Constraint: only ``type == "number"`` outputs can carry
    ``kpi = true``. Descriptors that set the flag on a non-numeric
    output fail validation at tool install.
    """

    kpi_label: str | None = Field(default=None, max_length=128)
    """Optional short label for the warehouse AG-Grid column header.

    Falls back to :attr:`label` when ``None``. Two KPI fields within
    one descriptor may not share the same ``kpi_label`` (warehouse
    columns must be unambiguous); enforced at :class:`ToolDescriptor`
    level by ``_kpi_labels_unique``.
    """

    kpi_unit: str | None = Field(default=None, max_length=32)
    """Optional unit override for warehouse display.

    Falls back to :attr:`unit` when ``None``. The captured unit is
    stored per-row in ``job_kpi_value`` so a later descriptor edit
    doesn't retroactively mislabel historic values.
    """

    kpi_category: str | None = Field(default=None, max_length=64)
    """Optional grouping label (``"thermal"``, ``"structural"``).

    Drives the future cross-tool dashboard (v1.x); in v1.0 used only
    by the descriptor-wizard grouping affordance + warehouse column
    grouping hint.
    """

    # Field-output support (ADR-0008 §4.4 — POD field surrogates). A field
    # output is a ``type == "file"`` output with ``file_type == "field"``
    # whose file is a whole nodal field (a temperature map, a stress field)
    # rather than a scalar or a CSV. The retained snapshot(s) train a POD
    # field surrogate (matofx.studies.surrogate.field); the engine reads
    # ``.npy`` on numpy alone and VTK / CGNS / Ensight via pyvista.
    field_format: str | None = None
    """For ``file_type == "field"``: snapshot format — one of
    ``npy`` / ``vtk`` / ``vtu`` / ``vtp`` / ``cgns`` / ``case``. Required on
    a field output; validated by ``_field_consistency``."""

    field_node_array_name: str | None = Field(default=None, max_length=128)
    """For pyvista field formats: which point-data array carries the field.

    Optional — the engine falls back to the mesh's active scalars when
    ``None``. Ignored for ``npy`` (a bare value array has no named arrays)."""

    field_remap_reference_mesh: str | None = None
    """Optional path (inside the tool's ``static/``) to a reference mesh.

    When set, every snapshot is remapped onto this common mesh before POD
    (ADR-0008 §4.4) — the opt-in escape from the common-mesh assumption —
    and it is the geometry the predicted-field viewer renders on."""

    field_timesteps: str | None = None
    """Optional glob (relative to the working dir) for a transient series.

    When set, the field is **time-resolved** (ADR-0008, 2026-06-07): the
    glob matches one snapshot file per timestep (e.g.
    ``outputs/temperature_*.npy``), ``time`` becomes an extra surrogate
    factor, and the viewer gains a time slider. ``filename`` still points
    at one representative snapshot so file-consistency holds; the whole
    glob joins the retention floor so the series survives cleanup."""

    # Paired uncertainty (ADR-0008 §7.1 — surrogate-tool ``<response>_std``).
    uncertainty_for: str | None = Field(default=None, max_length=64)
    """Name of the value output this one is the 1-sigma uncertainty of, or ``None``.

    Set on a synthesised surrogate-tool's ``<response>_std`` output to
    name its value sibling. It stays a real ``number`` output (captured
    in ``outputs.json``, available to Compare / programmatic consumers),
    but the job results panel folds it into its sibling's display as
    ``value +/- std`` instead of showing it as a separate row, with a
    one-line "+/- is one standard deviation" footnote. Hand-authored tools
    leave it ``None``. Only valid on ``type == "number"`` outputs.
    """

    # Output validity (ADR-0017 / `designs/output-validity.md`). Every numeric
    # and field output carries an implicit finiteness floor — ``NaN``/``Inf``
    # makes the run invalid — unless ``allow_non_finite`` opts out. Outputs may
    # additionally declare a physical range: ``min``/``max`` on a scalar number,
    # and per-node ``value_min``/``value_max`` on a field. The runner stamps each
    # result's validity at record time; consumers (study matrix, surrogates,
    # optimisation) exclude invalid runs. Enforced by ``_validity_consistency``.
    min: float | None = None
    """For ``type == "number"``: inclusive lower physical bound, or ``None``."""

    max: float | None = None
    """For ``type == "number"``: inclusive upper physical bound, or ``None``."""

    value_min: float | None = None
    """For a field output: inclusive **per-node** lower bound, or ``None``."""

    value_max: float | None = None
    """For a field output: inclusive **per-node** upper bound, or ``None``."""

    allow_non_finite: bool = False
    """Opt out of the finiteness floor for this output (numeric or field).

    Default ``False`` → a ``NaN``/``Inf`` value makes the run invalid. Set
    ``True`` only where a non-finite value is legitimate for this output."""

    @field_validator("name")
    @classmethod
    def _name_is_safe_identifier(cls, v: str) -> str:
        if not v[0].isalpha():
            raise ValueError("Output name must start with a letter")
        if not all(c.isalnum() or c == "_" for c in v):
            raise ValueError("Output name may only contain letters, digits, and underscores")
        return v

    @model_validator(mode="after")
    def _file_consistency(self) -> OutputField:
        if self.type == "file" and not self.filename:
            raise ValueError(f"Output {self.name!r} is a file but has no filename")
        if self.type == "table" and not self.columns:
            raise ValueError(
                f"Output {self.name!r} is a table but has no columns — table outputs need "
                "at least one column declared in the descriptor"
            )
        if self.type == "table" and self.columns:
            names = [c.name for c in self.columns]
            if len(names) != len(set(names)):
                raise ValueError(f"Output {self.name!r} table columns must have unique names")
        return self

    @model_validator(mode="after")
    def _kpi_consistency(self) -> OutputField:
        if self.kpi and self.type != "number":
            raise ValueError(
                f"Output {self.name!r}: kpi=true requires type='number', "
                f"got type={self.type!r}. Only scalar-numeric outputs can be KPIs "
                "(the warehouse table needs uniform numerics for sort + filter + plot)."
            )
        if not self.kpi and (
            self.kpi_label is not None or self.kpi_unit is not None or self.kpi_category is not None
        ):
            raise ValueError(
                f"Output {self.name!r}: kpi_label / kpi_unit / kpi_category may only be "
                "set when kpi=true."
            )
        return self

    @model_validator(mode="after")
    def _uncertainty_consistency(self) -> OutputField:
        if self.uncertainty_for is not None and self.type != "number":
            raise ValueError(
                f"Output {self.name!r}: uncertainty_for requires type='number' "
                f"(got {self.type!r}) — an uncertainty is a scalar paired with a "
                "scalar value."
            )
        return self

    @model_validator(mode="after")
    def _field_consistency(self) -> OutputField:
        is_field = self.file_type == "field"
        field_attrs = (
            self.field_format,
            self.field_node_array_name,
            self.field_remap_reference_mesh,
            self.field_timesteps,
        )
        if not is_field:
            if any(a is not None for a in field_attrs):
                raise ValueError(
                    f"Output {self.name!r}: field_format / field_node_array_name / "
                    "field_remap_reference_mesh / field_timesteps may only be set on a "
                    "field output (type='file', file_type='field')."
                )
            return self
        # A field output is a file output carrying a whole nodal field.
        if self.type != "file":
            raise ValueError(
                f"Output {self.name!r}: file_type='field' requires type='file' (got {self.type!r})."
            )
        if not self.field_format:
            raise ValueError(
                f"Output {self.name!r}: a field output must declare field_format "
                f"(one of {', '.join(sorted(_FIELD_FORMATS))})."
            )
        if self.field_format.lower().lstrip(".") not in _FIELD_FORMATS:
            raise ValueError(
                f"Output {self.name!r}: unsupported field_format {self.field_format!r}. "
                f"Supported: {', '.join(sorted(_FIELD_FORMATS))}."
            )
        return self

    @model_validator(mode="after")
    def _validity_consistency(self) -> OutputField:
        """Scope + order the ADR-0017 output-validity bounds.

        ``min``/``max`` are scalar bounds (``type == "number"`` only);
        ``value_min``/``value_max`` are per-node field bounds (field outputs
        only); ``allow_non_finite`` only means anything on a numeric or field
        output. Bounds must be correctly ordered when both ends are set.
        """
        is_field = self.file_type == "field"
        if (self.min is not None or self.max is not None) and self.type != "number":
            raise ValueError(
                f"Output {self.name!r}: min / max require type='number' "
                f"(got {self.type!r}); use value_min / value_max for a field output."
            )
        if (self.value_min is not None or self.value_max is not None) and not is_field:
            raise ValueError(
                f"Output {self.name!r}: value_min / value_max may only be set on a "
                "field output (type='file', file_type='field')."
            )
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(
                f"Output {self.name!r}: min ({self.min}) must be <= max ({self.max})."
            )
        if (
            self.value_min is not None
            and self.value_max is not None
            and self.value_min > self.value_max
        ):
            raise ValueError(
                f"Output {self.name!r}: value_min ({self.value_min}) must be <= "
                f"value_max ({self.value_max})."
            )
        if self.allow_non_finite and not (self.type == "number" or is_field):
            raise ValueError(
                f"Output {self.name!r}: allow_non_finite only applies to a numeric or "
                f"field output (got type={self.type!r})."
            )
        return self


class OutputGroup(_Frozen):
    """Visual grouping of output fields in the results panel."""

    name: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    column: int = Field(ge=1)
    row: int = Field(ge=1)


# --- Resources, licences, execution ---------------------------------------


class ResourceRequirements(_Frozen):
    """What the tool needs at run time on the execution host.

    Honoured by the scheduler when placing the job and by the runtime
    sandbox when applying limits.
    """

    cpu_cores: int = Field(default=1, ge=1, le=128)
    ram_mb: int = Field(default=512, ge=32, le=1024 * 1024)
    """RAM in megabytes; 1024 = 1 GB."""

    gpu_memory_mb: int = Field(default=0, ge=0, le=512 * 1024)
    """GPU memory in megabytes; 0 means the tool doesn't need a GPU.

    Capped at 512 GB — far above any real GPU we expect to schedule
    against (high-end NVIDIA accelerators are ~80-192 GB at time of
    writing). The cap is a sanity bound: descriptors asking for an
    impossible amount fail validation rather than silently wedging
    the scheduler.
    """

    walltime_minutes: int | None = Field(default=None, ge=1, le=60 * 24 * 30)
    """Wall-clock limit, in minutes. ``None`` sends no limit at all.

    Emitted as ``--time`` (Slurm) — and *only* emitted when set, so a
    tool that declares nothing behaves exactly as it did before this
    field existed.

    It lives on the descriptor because the tool's author is the person
    who knows how long their solve takes; an administrator setting it
    per-install would be guessing. An install that wants a ceiling
    regardless should set one on the scheduler side (a partition
    ``MaxTime`` or a QOS limit), which no descriptor can exceed.

    **Set one on anything an optimiser will drive.** A search exploring
    extreme parameters can find a corner where the solver does not
    converge, and without a wall-clock limit nothing stops it — on a
    metered cluster that is a bill rather than a failed run. Capped at
    30 days: past that the number is a typo, not an intention.
    """

    @property
    def requires_gpu(self) -> bool:
        """Convenience: ``True`` when ``gpu_memory_mb > 0``."""
        return self.gpu_memory_mb > 0


class LicenceRequirement(_Frozen):
    """One named third-party licence the tool consumes at run time.

    The ``name`` must match a licence registered with the install's
    licence manager (legacy: ``Licence Manager``); the scheduler
    won't dispatch the job if the named licences aren't available in
    the requested ``count``.
    """

    name: str = Field(min_length=1, max_length=128)
    """Exact licence name as listed in the licence manager."""

    count: int = Field(default=1, ge=1, le=64)
    """How many of this licence are required for one run."""


class DescriptorPreset(_Frozen):
    """A tool-author-supplied named bundle of input values.

    Surfaces in the My Jobs workspace as a "Use this preset" pick
    that pre-populates the input form; users can still override
    any value before submitting. Equivalent in meaning to the
    runtime :class:`matofx.tools.forms.seeders.Preset` dataclass —
    the difference is that this Pydantic model rides on the
    descriptor itself (authored via the wizard's Presets tab) while
    the dataclass is what the seeder hands to the form. Both shapes
    share the same three fields.

    The legacy authoring path (a separate ``<alias>_presets.json``
    file in the bundle) still works for tools that haven't been
    re-saved through the wizard; :func:`load_presets` prefers this
    in-descriptor list when non-empty.
    """

    name: str = Field(min_length=1, max_length=128)
    """User-visible preset name; shown in the My Jobs picker."""

    description: str = Field(default="", max_length=512)
    """Optional short blurb shown alongside the name."""

    values: dict[str, Any] = Field(default_factory=dict)
    """Map of ``input.name → value``. Inputs absent from this map fall back to
    descriptor defaults; values supplied here pre-populate the form and remain
    user-editable. Tolerant of stale keys — a preset that names an input the
    descriptor later removed simply skips the unmatched entry at submit time.
    """


class ExecutionBackend(_Frozen):
    """How the tool runs.

    Phase 1 supports the ``subprocess`` kind. Other kinds are part of
    the schema so descriptors authored against Phase 1 don't need
    rewriting once the corresponding backends ship.
    """

    kind: ExecutionBackendKind = "subprocess"

    queue: str | None = None
    """For ``hpc_batch``: the HPC queue name.

    Optional. Not every cluster partitions its queue — a site with a
    single unnamed pool has no queue to name, and requiring one there
    would force the author to invent a partition that doesn't exist.
    Left unset, the submission carries no ``--partition`` (Slurm) /
    ``-NodeGroups`` (MS-HPC) flag and the cluster's own default
    applies. The cluster row's default queue still fills it in when
    the site does partition.
    """

    server: str | None = None
    """For ``aos_calculator`` / ``aos_repeat_calculator``: the AOS server identifier."""

    url: str | None = None
    """For ``web_app``: the public URL or IP."""

    port: str | None = None
    """For ``web_app``: the port (string to allow non-numeric configs)."""

    @model_validator(mode="after")
    def _backend_specific_fields(self) -> ExecutionBackend:
        # ``hpc_batch`` deliberately has no required field: a cluster
        # without partitions has no queue name to give, and the target
        # already omits the flag when there is none. See ``queue``.
        if self.kind in {"aos_calculator", "aos_repeat_calculator"} and not self.server:
            raise ValueError(f"execution_backend.kind == {self.kind!r} requires 'server'")
        if self.kind == "web_app" and (not self.url or not self.port):
            raise ValueError("execution_backend.kind == 'web_app' requires 'url' and 'port'")
        return self


MAX_GUIDE_IMAGES = 10
"""Cap on :attr:`ToolDescriptor.input_guides` — enough for a set of
parameter diagrams without letting the descriptor (and the form header)
balloon."""

GUIDE_CAPTION_MAX = 200
"""Character cap on a guide image's caption."""


class GuideImage(_Frozen):
    """A static guide image shown during input entry (design 2026-07-03).

    A visual explainer for the tool's input parameters — e.g. a diagram
    showing what a measurement refers to. Like :attr:`ToolDescriptor.
    icon_filename`, the *bytes* live in the tool's bundle directory and
    only the bundle-relative ``filename`` (+ an optional ``caption``) is
    recorded on the descriptor, so guides pin per job/study and travel
    with export/import for free. Authored on the tool editor's Metadata
    tab; rendered as a captioned gallery above the input form.
    """

    filename: str = Field(min_length=1, max_length=255)
    """Bundle-relative path to the image (PNG/JPG/WebP)."""

    caption: str = Field(default="", max_length=GUIDE_CAPTION_MAX)
    """Optional short explanation shown beneath the image."""

    @field_validator("filename")
    @classmethod
    def _filename_is_bundle_safe(cls, v: str) -> str:
        return _reject_unsafe_bundle_path(v, field="Guide image filename")


# --- The descriptor -------------------------------------------------------


class ToolDescriptor(_Frozen):
    """The complete tool contract.

    Loaded from disk (``descriptor.toml`` / ``descriptor.json``) at
    install time, validated, and persisted as JSON in the ``tool``
    table. The same shape is what the UI consumes to render forms,
    what the scheduler consumes to allocate resources, and what the
    runner consumes to stage and execute the job.
    """

    schema_version: int = CURRENT_DESCRIPTOR_SCHEMA_VERSION

    # Identity.
    alias: str = Field(min_length=1, max_length=64)
    """Stable identifier; URL-safe; what jobs reference."""

    name: str = Field(min_length=1, max_length=128)
    """Human-readable name shown in lists and forms."""

    version: str = Field(min_length=1, max_length=32)
    """Semver-flavoured tool version (``"1.2.3"``); displayed to users."""

    short_description: str = Field(min_length=1, max_length=SHORT_DESCRIPTION_MAX)
    """Mandatory one-line summary. The *only* description shown on the
    home-page Quick Access card, so it stays tight (200 chars) to keep
    the card list tidy. Falls back to standing in for the long form on
    the run page when :attr:`long_description` is empty."""

    long_description: str | None = Field(default=None, max_length=2048)
    """Optional, fuller plain-English description shown above the input
    form on the run page and in the Quick Access *info* dialog. ``None``
    when the tool author didn't write one — surfaces fall back to
    :attr:`short_description`."""

    teams: list[str] = Field(default_factory=list)
    """Teams the tool belongs to — used to group the tool in the home-page browser.

    Each entry must match an existing :class:`matofx.users.team_orm.Team`
    name (validated at install / update time by :class:`ToolService`).
    Empty list means the tool isn't grouped under any team — it appears
    in an "Ungrouped" section in the browser.
    """

    # Bundle.
    main_script: str = Field(min_length=1, max_length=255)
    """Path to the tool's entry point, relative to the bundle root."""

    icon_filename: str | None = None
    """Bundle-relative path to a 100x100 PNG icon. Convention: ``"<alias>.png"``."""

    input_guides: list[GuideImage] = Field(default_factory=list, max_length=MAX_GUIDE_IMAGES)
    """Static guide images (0..N) shown above the input form during
    parameter entry — visual explainers for what the inputs mean. Bytes
    live in the bundle; only filename + caption are recorded here (see
    :class:`GuideImage`). Empty for tools that don't need them."""

    presets_filename: str | None = None
    """Bundle-relative path to an optional ``<alias>_presets.json`` file with preset input sets.

    Legacy authoring path. Tools authored through the descriptor wizard
    carry their presets in :attr:`presets` instead and leave this
    ``None``; the wizard does not write a separate JSON file. The two
    paths can coexist on a single bundle (the in-descriptor list wins).
    """

    presets: list[DescriptorPreset] = Field(default_factory=list)
    """Wizard-authored preset input sets stored on the descriptor itself.

    Each entry is a named bundle of ``{input_name: value}`` overrides
    surfaced as a "Use this preset" pick in the My Jobs workspace.
    Authored on the descriptor wizard's *Presets* tab; round-trips
    through :meth:`build_descriptor_payload` / :func:`state_from_descriptor`.

    Resolution order at submit time (see
    :func:`matofx.tools.forms.seeders.load_presets`):

    1. This list — when non-empty.
    2. The bundle's ``presets.toml`` (v2 file shape).
    3. ``presets_filename`` — the legacy JSON shape.
    """

    retain_files: list[str] = Field(default_factory=list)
    """Keep-list: glob patterns for *undeclared* files to keep after a
    successful run, on top of the always-kept floor (the job's inputs,
    every declared output, and the run's logs/records). Patterns are
    relative to the working directory (use e.g. ``outputs/*.vtk`` for an
    output-side file). Empty keeps the whole working directory; non-empty
    deletes undeclared top-level and ``outputs/`` files that don't match."""

    delete_io_files: list[str] = Field(default_factory=list)
    """**Legacy** deny-list scrubbed from ``inputs/`` and ``outputs/`` on
    cleanup (minus the floor). Retained only so descriptors authored before
    the unified keep-list model keep working; new descriptors leave it empty
    and it is a no-op. Superseded by :attr:`retain_files`."""

    # Inputs.
    inputs: list[InputField] = Field(default_factory=list)
    """Ordered list of input fields shown in the form."""

    input_groups: list[InputGroup] = Field(default_factory=list)
    """Visual groupings for the input form (port of legacy ``input_groups``)."""

    input_column_layout: dict[str, float] = Field(default_factory=dict)
    """Relative column widths for the input form (e.g. ``{"column_1": 0.4, "column_2": 0.6}``)."""

    # Outputs.
    outputs: list[OutputField] = Field(default_factory=list)
    """Ordered list of outputs shown in the results panel."""

    output_groups: list[OutputGroup] = Field(default_factory=list)
    """Visual groupings for the output panel."""

    output_column_layout: dict[str, float] = Field(default_factory=dict)
    """Relative column widths for the output panel."""

    # Resources / licences / execution.
    resources: ResourceRequirements = Field(default_factory=ResourceRequirements)
    """CPU / RAM / GPU memory requirements at run time."""

    licences_required: list[LicenceRequirement] = Field(default_factory=list)
    """Third-party licences the tool consumes at run time, with counts."""

    execution_backend: ExecutionBackend = Field(default_factory=ExecutionBackend)
    """How the tool runs (subprocess in Phase 1; HPC / AOS / web_app land later)."""

    duration_class: DurationClass = "medium"
    """Declared expected-runtime class. **Reserved** — no scheduler reads it
    today (it does *not* affect queue placement) and the tool editor no longer
    exposes it; the value is stored and preserved on round-trip. Wiring it into
    lane ordering is a tracked future item."""

    # Input form behaviour.
    input_groups_start_collapsed: bool = True
    """Whether each input-group section starts collapsed in the job form.

    When ``True`` (the default) every ``[[input_groups]]`` section in
    the job-submit form renders collapsed; the user expands the
    sections they want. When ``False`` every section starts expanded
    (the pre-2026-05-16 behaviour). Tool-wide — there is no per-group
    override.
    """

    require_all_groups_opened: bool = True
    """Whether the user must open every section before Submit activates.

    When ``True`` (the default) **and** ``input_groups_start_collapsed``
    is ``True``, the job form's Submit / Skip-Queue buttons stay
    disabled until the user has opened every input-group section at
    least once (re-collapsing afterwards is fine — the requirement is
    that each section was revealed once, so the user has seen every
    input). Has no effect when ``input_groups_start_collapsed`` is
    ``False``: sections that start expanded are already revealed, so
    there is nothing to gate on.
    """

    # Conditional input dependencies (§16.16).
    dependencies: list[Dependency] = Field(default_factory=list)
    """Ordered conditional rules over the inputs.

    Each rule's ``when`` may reference only sibling inputs; effects
    target only sibling inputs. Evaluated in declaration order against
    resolved values (rules chain); the rule graph must be acyclic
    (enforced at install). See :mod:`matofx.tools.forms.resolver`.
    """

    # Arbitrary cross-field validity rules (§F30).
    cross_field_constraints: list[CrossFieldConstraint] = Field(default_factory=list)
    """Submit-time boolean validity rules spanning several inputs.

    Each ``expression`` may reference only sibling inputs; a falsy
    result rejects the submission with the constraint's
    ``error_message``. Evaluated against the resolved values (after any
    §16.16 effects), and skipped when a participant is missing / hidden
    so the user isn't nagged about half-filled or rule-hidden fields.
    """

    # Workflow.
    downstream_tools: list[str] = Field(default_factory=list)
    """Tool aliases whose inputs may be populated from this tool's outputs."""

    # Approvals & notifications.
    approvals_policy: ApprovalsPolicy = "none"
    """Per §I46: ``none`` / ``pre`` / ``post`` / ``both``."""

    approver_usernames: list[str] = Field(default_factory=list)
    """Usernames allowed to approve when ``approvals_policy != "none"``."""

    email_notifications: bool = False
    """Whether successful / failed completions send email notifications.

    Off unless the tool asks for it: a new tool shouldn't start mailing
    every submitter on every run before anyone has decided it should.
    An admin / super-user turns it on per tool in the tool editor's
    Notifications card. A descriptor that omits the key — a hand-written
    or imported bundle — gets the same answer, so "not stated" never
    means "send".
    """

    # Surrogate provenance (ADR-0008 §7.4).
    surrogate_model_version_id: int | None = None
    """The ``surrogate_model_version`` a synthesised surrogate-tool pins, or ``None``.

    Set only on tools produced by ``matofx.models.register_as_tool`` — it
    marks the tool as a surrogate-of-``alias`` and names the exact fitted
    version its baked orchestrator runs. Because the descriptor round-trips
    verbatim into ``ToolVersion.descriptor_snapshot``, the pin is captured
    per tool version (a refit re-export mints a new version with a new
    pin). Two consumers read it: ``JobService.create`` tags the child
    ``Job.surrogate_model_version_id`` from it (the §7.4 lineage tag), and
    the submit form renders the §7.3 coverage gauge when it's present.
    Hand-authored tools always leave it ``None``.
    """

    # --- Validators ---

    @model_validator(mode="before")
    @classmethod
    def _back_compat_description(cls, data: Any) -> Any:
        """Map the legacy single ``description`` key onto the split fields.

        Before the short/long split every descriptor carried one
        ``description`` string. Old data still reaches this model from
        three directions the data migration can't reach: ``tool_version``
        snapshots written pre-split, migrate export/import bundles
        authored on an older install, and test fixtures. When a payload
        carries the legacy key but not ``short_description`` we fold it
        in: the text becomes the short form, and anything past the
        200-char cap spills into ``long_description`` (with an ellipsised
        short) so nothing is lost and the model still validates. Mirrors
        migration ``0080`` so a blob loads identically whether it was
        rewritten on disk or not.
        """
        if not isinstance(data, dict):
            return data
        if "short_description" in data or "description" not in data:
            return data
        legacy = str(data.get("description") or "")
        data = dict(data)
        data.pop("description", None)
        if len(legacy) <= SHORT_DESCRIPTION_MAX:
            data["short_description"] = legacy
        else:
            data["short_description"] = legacy[: SHORT_DESCRIPTION_MAX - 1].rstrip() + "…"
            data.setdefault("long_description", legacy)
        return data

    @field_validator("teams")
    @classmethod
    def _teams_clean_unique(cls, v: list[str]) -> list[str]:
        cleaned = [t.strip() for t in v]
        if any(not t for t in cleaned):
            raise ValueError("Team names cannot be empty or whitespace-only")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Team names must be unique within a tool")
        return cleaned

    @field_validator("approver_usernames")
    @classmethod
    def _approver_usernames_clean_unique(cls, v: list[str]) -> list[str]:
        cleaned = [u.strip().lower() for u in v]
        if any(not u for u in cleaned):
            raise ValueError("Approver usernames cannot be empty or whitespace-only")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Approver usernames must be unique within a tool")
        return cleaned

    @field_validator("alias")
    @classmethod
    def _alias_is_url_safe(cls, v: str) -> str:
        if not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError(
                "Tool alias may only contain letters, digits, hyphens, and underscores"
            )
        return v.lower()

    @field_validator("main_script")
    @classmethod
    def _main_script_is_bundle_safe(cls, v: str) -> str:
        return _reject_unsafe_bundle_path(v, field="main_script")

    @field_validator("icon_filename", "presets_filename")
    @classmethod
    def _optional_bundle_filename_is_safe(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return _reject_unsafe_bundle_path(v, field="Bundle filename")

    @field_validator("inputs")
    @classmethod
    def _input_names_unique(cls, v: list[InputField]) -> list[InputField]:
        names = [field.name for field in v]
        if len(names) != len(set(names)):
            raise ValueError("Input field names must be unique within a tool")
        return v

    @field_validator("outputs")
    @classmethod
    def _output_names_unique(cls, v: list[OutputField]) -> list[OutputField]:
        names = [field.name for field in v]
        if len(names) != len(set(names)):
            raise ValueError("Output field names must be unique within a tool")
        return v

    @field_validator("outputs")
    @classmethod
    def _kpi_labels_unique(cls, v: list[OutputField]) -> list[OutputField]:
        """KPI columns in the warehouse must be unambiguous.

        Two KPI outputs in one descriptor may not share the same effective
        warehouse column label. The effective label is ``kpi_label`` when
        set, else ``label``. Per ADR-0012 / `designs/kpi-extraction.md`
        §3.2 — mirrors the duplicate-``name`` check above.
        """
        labels = [
            (field.kpi_label if field.kpi_label is not None else field.label)
            for field in v
            if field.kpi
        ]
        if len(labels) != len(set(labels)):
            raise ValueError(
                "KPI labels (kpi_label, or label when kpi_label is unset) must be "
                "unique within a tool — two KPI outputs would otherwise produce "
                "ambiguous warehouse columns"
            )
        return v

    @field_validator("input_groups")
    @classmethod
    def _input_group_names_unique(cls, v: list[InputGroup]) -> list[InputGroup]:
        names = [g.name for g in v]
        if len(names) != len(set(names)):
            raise ValueError("Input group names must be unique within a tool")
        return v

    @field_validator("output_groups")
    @classmethod
    def _output_group_names_unique(cls, v: list[OutputGroup]) -> list[OutputGroup]:
        names = [g.name for g in v]
        if len(names) != len(set(names)):
            raise ValueError("Output group names must be unique within a tool")
        return v

    @model_validator(mode="after")
    def _input_groups_referenced_correctly(self) -> ToolDescriptor:
        group_names = {g.name for g in self.input_groups}
        for field in self.inputs:
            if field.group is not None and field.group not in group_names:
                raise ValueError(
                    f"Input {field.name!r} references unknown input group {field.group!r}"
                )
        return self

    @model_validator(mode="after")
    def _output_groups_referenced_correctly(self) -> ToolDescriptor:
        group_names = {g.name for g in self.output_groups}
        for field in self.outputs:
            if field.group is not None and field.group not in group_names:
                raise ValueError(
                    f"Output {field.name!r} references unknown output group {field.group!r}"
                )
        return self

    @model_validator(mode="after")
    def _uncertainty_targets_exist(self) -> ToolDescriptor:
        """An ``uncertainty_for`` must name a real numeric output of this tool.

        A dangling reference does not degrade — it **hides a result**. The
        job page folds an uncertainty into its parent and suppresses its own
        row, so a reference to an output that was renamed or deleted removes
        the number from the results page altogether, with nothing on screen to
        say so. Rejected at author time, where the person who can fix it is
        already looking at the descriptor.
        """
        by_name = {f.name: f for f in self.outputs}
        for field in self.outputs:
            target = field.uncertainty_for
            if target is None:
                continue
            parent = by_name.get(target)
            if parent is None:
                raise ValueError(
                    f"Output {field.name!r} is the uncertainty of {target!r}, but this tool "
                    f"declares no output called {target!r}. Point it at an existing numeric "
                    "output, or clear the uncertainty link."
                )
            if parent.type != "number":
                raise ValueError(
                    f"Output {field.name!r} is the uncertainty of {target!r}, but {target!r} "
                    f"is a {parent.type!r} output. An uncertainty pairs with a numeric value."
                )
            if parent.uncertainty_for is not None:
                raise ValueError(
                    f"Output {field.name!r} is the uncertainty of {target!r}, which is itself "
                    "an uncertainty. An uncertainty cannot have one."
                )
            if field.name == target:
                raise ValueError(f"Output {field.name!r} cannot be its own uncertainty.")
        return self

    @model_validator(mode="after")
    def _approvers_only_when_required(self) -> ToolDescriptor:
        if self.approvals_policy == "none" and self.approver_usernames:
            raise ValueError("approver_usernames must be empty when approvals_policy == 'none'")
        if self.approvals_policy != "none" and not self.approver_usernames:
            raise ValueError(
                f"approvals_policy == {self.approvals_policy!r} requires at least one approver"
            )
        return self

    @model_validator(mode="after")
    def _aos_gpu_refused(self) -> ToolDescriptor:
        """Phase 1: Always-On-Server execution can't satisfy GPU requirements.

        The MATOFX runner's AOS targets fire the launcher over SSH /
        WinRM but don't talk to a GPU scheduler — they trust the OS to
        place the process. That works fine for CPU jobs but doesn't
        give us any way to reserve, share, or queue GPU memory across
        multiple jobs hitting the same always-on server. Rather than
        ship a half-working AOS GPU path, Phase 1 refuses the
        combination at descriptor-validation time so customers see a
        clear error at install rather than a confused job-failure
        later. GPU jobs go through ``hpc_batch`` (Slurm / MS HPC
        Pack), which knows how to schedule GPUs.

        Phase 2 is expected to land an AOS GPU scheduler placeholder
        (see project brief §F29); when it does, this validator
        relaxes accordingly.
        """
        aos_kinds = {"aos_calculator", "aos_repeat_calculator"}
        if self.execution_backend.kind in aos_kinds and self.resources.gpu_memory_mb > 0:
            raise ValueError(
                "Always-On-Server tools can't request GPU memory in this MATOFX release. "
                "Either remove resources.gpu_memory_mb or move the tool to an HPC backend "
                "(execution_backend.kind == 'hpc_batch'), which schedules GPUs through the "
                "cluster's queue."
            )
        return self

    @model_validator(mode="after")
    def _sum_check_targets_exist(self) -> ToolDescriptor:
        names = {f.name for f in self.inputs}
        for field in self.inputs:
            if field.sum_check is None:
                continue
            for target in field.sum_check.sum_parameters:
                if target not in names:
                    raise ValueError(
                        f"Input {field.name!r} sum_check references unknown input {target!r}"
                    )
        return self

    @model_validator(mode="after")
    def _dependencies_well_formed(self) -> ToolDescriptor:
        """Install-time lint for §16.16 conditional rules.

        Refuses a descriptor (so the problem surfaces at install, never
        at submit) when a rule's ``when`` is outside the locked DSL
        grammar, references a non-sibling input, an effect targets an
        unknown input, or an effect's literal is the wrong shape for
        its target field's type.
        """
        if not self.dependencies:
            return self
        # Lazy import: descriptor.py is imported by matofx.tools.__init__,
        # and forms.safe_expr lives under the forms package whose
        # __init__ imports back into descriptor — deferring the import
        # to validation time breaks that import-time cycle.
        from matofx.tools.forms.safe_expr import (  # noqa: PLC0415
            UnsafeExpressionError,
        )
        from matofx.tools.forms.safe_expr import (  # noqa: PLC0415
            parse as parse_expr,
        )
        from matofx.tools.forms.safe_expr import (  # noqa: PLC0415
            referenced_names as expr_names,
        )

        by_name = {f.name: f for f in self.inputs}
        for idx, dep in enumerate(self.dependencies):
            where = f"dependency #{idx + 1}"
            try:
                tree = parse_expr(dep.when)
            except UnsafeExpressionError as exc:
                raise ValueError(f"{where} 'when' is invalid: {exc}") from exc
            for ref in sorted(expr_names(tree)):
                if ref not in by_name:
                    raise ValueError(
                        f"{where} 'when' references {ref!r}, which is not an input "
                        f"of this tool (rules may only reference sibling inputs)"
                    )
            for effect in dep.effects:
                _validate_effect_targets(where, effect, by_name)
                # Expression-driven effects (set_value/default formula,
                # set_min_max formula bound, require) carry DSL strings;
                # lint each like a 'when' so a bad formula is caught at
                # install, never at submit.
                for expr_str in effect_expr_strings(effect):
                    try:
                        etree = parse_expr(expr_str)
                    except UnsafeExpressionError as exc:
                        raise ValueError(
                            f"{where} {effect.kind} formula is invalid: {exc}"
                        ) from exc
                    for ref in sorted(expr_names(etree)):
                        if ref not in by_name:
                            raise ValueError(
                                f"{where} {effect.kind} formula references {ref!r}, which is "
                                f"not an input of this tool (rules may only reference sibling "
                                f"inputs)"
                            )
        return self

    @model_validator(mode="after")
    def _dependencies_acyclic(self) -> ToolDescriptor:
        """Refuse a cyclic rule graph at install (§16.16).

        Edge ``read → written`` per rule: if a rule's condition reads
        field R and any effect writes field W, then W depends on R. A
        cycle means rule chaining can't be resolved in a single
        topological pass, so the descriptor is rejected rather than
        looping at submit time.
        """
        if not self.dependencies:
            return self
        from matofx.tools.forms.safe_expr import (  # noqa: PLC0415
            parse as parse_expr,
        )
        from matofx.tools.forms.safe_expr import (  # noqa: PLC0415
            referenced_names as expr_names,
        )

        input_names = {f.name for f in self.inputs}
        graph: dict[str, set[str]] = {}
        for dep in self.dependencies:
            # A rule "reads" every input named in its ``when`` *and* in any
            # of its effect formulas (a set_min_max minimum_expr, a
            # set_value value_expr, a require expr). All of those observe
            # field *values*, so a set_value that writes such a field must
            # run first.
            reads = (expr_names(parse_expr(dep.when)) | _dep_effect_read_names(dep)) & input_names
            # Only `set_value` changes what a later rule can observe — a
            # condition / formula reads field *values*, never visibility /
            # options / bounds. So only set_value targets create a read-
            # after-write ordering edge (and therefore a potential cycle).
            # A rule like ``when a == 1 → show a`` is legitimate, not a
            # loop.
            writes = _set_value_targets(dep)
            for r in reads:
                graph.setdefault(r, set()).update(writes)

        cycle = _find_cycle(graph)
        if cycle is not None:
            chain = " → ".join(cycle)
            raise ValueError(
                f"Conditional rules form a cycle ({chain}); a field can't "
                f"transitively depend on itself"
            )
        return self

    @model_validator(mode="after")
    def _cross_field_constraints_well_formed(self) -> ToolDescriptor:
        """Install-time lint for §F30 cross-field constraints.

        Refuses a descriptor when a constraint ``expression`` is
        outside the locked DSL grammar or references an input that
        isn't a sibling of this tool — same guarantees as a dependency
        ``when``, so a bad constraint is caught at install, never at
        submit.
        """
        if not self.cross_field_constraints:
            return self
        from matofx.tools.forms.safe_expr import (  # noqa: PLC0415
            UnsafeExpressionError,
        )
        from matofx.tools.forms.safe_expr import (  # noqa: PLC0415
            parse as parse_expr,
        )
        from matofx.tools.forms.safe_expr import (  # noqa: PLC0415
            referenced_names as expr_names,
        )

        input_names = {f.name for f in self.inputs}
        for idx, constraint in enumerate(self.cross_field_constraints):
            where = f"cross-field constraint #{idx + 1}"
            try:
                tree = parse_expr(constraint.expression)
            except UnsafeExpressionError as exc:
                raise ValueError(f"{where} expression is invalid: {exc}") from exc
            for ref in sorted(expr_names(tree)):
                if ref not in input_names:
                    raise ValueError(
                        f"{where} references {ref!r}, which is not an input "
                        f"of this tool (constraints may only reference sibling "
                        f"inputs)"
                    )
        return self


# --- Descriptor read helpers ----------------------------------------------


def tool_descriptions(descriptor: dict[str, Any]) -> tuple[str, str | None]:
    """Pull ``(short_description, long_description)`` from a raw descriptor dict.

    Routers read the descriptor straight out of the ``tool.descriptor``
    JSON column rather than re-validating it through
    :class:`ToolDescriptor`, so they need the same legacy fallback the
    model's ``_back_compat_description`` validator applies: a pre-split
    blob carries only ``description``. Prefer the split keys; fall back
    to the legacy key for the short form when they're absent. Never
    raises — a missing/odd descriptor yields ``("", None)``.
    """
    short = descriptor.get("short_description")
    long = descriptor.get("long_description")
    if short is None and "description" in descriptor:
        short = descriptor.get("description")
    return str(short or ""), (str(long) if long else None)


# --- Dependency validation helpers ----------------------------------------


def _effect_targets(effect: Effect) -> set[str]:
    """Return the input names an effect targets (any effect kind)."""
    if isinstance(effect, ShowEffect | HideEffect):
        return set(effect.fields)
    return {effect.field}


def _set_value_targets(dep: Dependency) -> set[str]:
    """Fields a rule writes *values* to (the only cycle-relevant writes).

    Conditions read input values, so only :class:`SetValueEffect`
    creates a read-after-write dependency between rules. Visibility /
    options / bounds / required changes are invisible to a condition
    and never participate in a cycle.
    """
    return {effect.field for effect in dep.effects if isinstance(effect, SetValueEffect)}


def effect_expr_strings(effect: Effect) -> list[str]:
    """The DSL formula strings an effect carries (empty for static effects).

    Used by the install-time lint and the resolver's rule-ordering /
    cycle graph: a formula reads sibling input *values*, so the rule
    carrying it must run after any rule that ``set_value``-writes a field
    it names.
    """
    if isinstance(effect, RequireEffect):
        return [effect.expr]
    if isinstance(effect, SetMinMaxEffect):
        return [e for e in (effect.minimum_expr, effect.maximum_expr) if e]
    if isinstance(effect, SetValueEffect | SetDefaultEffect):
        return [effect.value_expr] if effect.value_expr else []
    return []


def _dep_effect_read_names(dep: Dependency) -> set[str]:
    """Every input name read by any of a rule's effect formulas."""
    from matofx.tools.forms.safe_expr import parse as parse_expr  # noqa: PLC0415
    from matofx.tools.forms.safe_expr import referenced_names as expr_names  # noqa: PLC0415

    names: set[str] = set()
    for effect in dep.effects:
        for expr_str in effect_expr_strings(effect):
            names |= expr_names(parse_expr(expr_str))
    return names


def _validate_effect_targets(
    where: str,
    effect: Effect,
    by_name: dict[str, InputField],
) -> None:
    """Reject an effect that targets an unknown input or wrong type.

    Raises ``ValueError`` so Pydantic surfaces it like any other
    schema problem.
    """
    for target in sorted(_effect_targets(effect)):
        field = by_name.get(target)
        if field is None:
            raise ValueError(
                f"{where} {effect.kind} effect targets {target!r}, which is not "
                f"an input of this tool"
            )
        _validate_effect_for_type(where, effect, field)


def _validate_effect_for_type(
    where: str,
    effect: Effect,
    field: InputField,
) -> None:
    """Per-kind shape rules for an effect against its target's type."""
    if isinstance(effect, SetOptionsEffect):
        if field.type not in {"select", "multiselect"}:
            raise ValueError(
                f"{where} set_options targets {field.name!r} (type {field.type}); "
                f"only select / multiselect inputs have options"
            )
        return
    if isinstance(effect, SetMinMaxEffect):
        if field.type not in {"integer", "float"}:
            raise ValueError(
                f"{where} set_min_max targets {field.name!r} (type {field.type}); "
                f"only integer / float inputs have bounds"
            )
        if field.type == "integer":
            for bound_name, bound in (
                ("minimum", effect.minimum),
                ("maximum", effect.maximum),
            ):
                if bound is not None and not float(bound).is_integer():
                    raise ValueError(
                        f"{where} set_min_max {bound_name} must be a whole number "
                        f"for integer input {field.name!r} (got {bound!r})"
                    )
        return
    if isinstance(effect, SetValueEffect | SetDefaultEffect):
        problem = _value_shape_problem(field, effect.value)
        if problem is not None:
            raise ValueError(f"{where} {effect.kind} for {field.name!r}: {problem}")


def _value_shape_problem(field: InputField, value: Any) -> str | None:
    """Return a message if ``value`` is the wrong shape for ``field``.

    ``None`` is always accepted (it means "clear / no pre-fill").
    Option membership for ``select`` / ``multiselect`` is *not* checked
    here — effective options can themselves be rule-controlled, so
    membership is a submit-time concern, not an install-time one.
    """
    if value is None:
        return None
    if field.type == "table":
        return "rules can't set a table value in this release"
    checks: dict[str, tuple[bool, str]] = {
        "string": (isinstance(value, str), "value must be text for a string input"),
        "file": (isinstance(value, str), "value must be text for a file input"),
        "select": (isinstance(value, str), "value must be text for a select input"),
        "boolean": (
            isinstance(value, bool),
            "value must be true or false for a boolean input",
        ),
        "integer": (
            isinstance(value, int) and not isinstance(value, bool),
            "value must be a whole number for an integer input",
        ),
        "float": (
            isinstance(value, int | float) and not isinstance(value, bool),
            "value must be a number for a float input",
        ),
        "multiselect": (
            isinstance(value, list) and all(isinstance(x, str) for x in value),
            "value must be a list of option strings for a multiselect input",
        ),
    }
    ok, message = checks.get(field.type, (True, ""))
    return None if ok else message


def _find_cycle(graph: dict[str, set[str]]) -> list[str] | None:
    """Return one cycle as a node path, or ``None`` if the graph is a DAG.

    Iterative three-colour DFS (white / grey / black) so a pathological
    descriptor can't blow the Python recursion limit. Every node — keys
    *and* edge targets that are never keys — is coloured, so a
    write-only field can't trip a lookup.
    """
    white, grey, black = 0, 1, 2
    nodes: set[str] = set(graph)
    for targets in graph.values():
        nodes |= targets
    colour: dict[str, int] = dict.fromkeys(nodes, white)

    for root in sorted(nodes):
        if colour[root] != white:
            continue
        stack: list[tuple[str, list[str]]] = [(root, [root])]
        while stack:
            node, path = stack[-1]
            if colour[node] == white:
                colour[node] = grey
            progressed = False
            for nxt in sorted(graph.get(node, ())):
                if colour[nxt] == grey:
                    # Back-edge → cycle. Trim the prefix before `nxt`.
                    start = path.index(nxt) if nxt in path else 0
                    return [*path[start:], nxt]
                if colour[nxt] == white:
                    stack.append((nxt, [*path, nxt]))
                    progressed = True
                    break
            if not progressed:
                colour[node] = black
                stack.pop()
    return None
