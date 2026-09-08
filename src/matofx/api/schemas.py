"""Request / response Pydantic models for the HTTP API.

Distinct from the ORM and domain models so the wire format can evolve
independently of the database schema. Frozen + extra-forbid so a stray
field on either side surfaces as a clean 422 rather than silent
data loss.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    """Frozen + extra-forbid base — the API's wire-shape contract."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# --- auth ----------------------------------------------------------------


class LoginRequest(_Frozen):
    """POST /api/auth/login body."""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1)


class LoginResponse(_Frozen):
    """POST /api/auth/login response — bearer token + lifetime."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 — OAuth2 wire-format constant, not a credential
    expires_at: _dt.datetime
    user_id: int
    session_idle_timeout_minutes: int = Field(ge=1, le=1440)
    """Effective idle-timeout policy. The dashboard's per-tab keepalive
    timer uses this to redirect to ``/login`` once the client-side idle
    window exceeds the policy, so the user lands on the sign-in page
    instead of bumping into 401 errors page-by-page."""

    breakglass: bool = False
    """True when this Super User was admitted **over** the licence cap
    (ADR-0019 §C13). The dashboard tells them so and points at Active
    sessions, because an unexplained over-cap admission is exactly the
    thing that later shows up as a breach in a compliance report."""

    slots_in_use: int | None = None
    """Concurrent sessions held at the moment of login (CSL only)."""

    n_slots: int | None = None
    """The licence's concurrent-session cap (CSL only; ``None`` when
    unmetered — no CSL licence, or read-only mode)."""


class CurrentUserTeamMembership(_Frozen):
    """One team the authenticated user belongs to."""

    id: int
    name: str


class CurrentUserResponse(_Frozen):
    """GET /api/auth/me — identity + team membership + preferences.

    Bundles everything the Account page renders so the UI doesn't have
    to make follow-up calls per pane.
    """

    user_id: int
    username: str
    email: str
    full_name: str
    first_name: str = ""
    last_name: str = ""
    role: str
    permissions: list[str] = Field(default_factory=list)
    """Every permission slug the caller effectively holds — the union of
    their base role and any stacked capability roles (ADR-0005). Lets
    the UI hide controls the caller can't use (e.g. the capability-roles
    picker, which needs ``roles.manage``) instead of round-tripping a
    check per control."""
    is_approver: bool
    approver_tools: list[str] = Field(default_factory=list)
    """Names of the active tools this user is actually listed as an
    approver for (empty unless ``is_approver``). Lets the Account page
    show *which* tools, not just the eligibility flag."""
    email_notifications_enabled: bool
    summary_enrichment_enabled: bool = True
    """Per-user opt-in for AI-enriched result summaries
    (``designs/auto-summaries.md``). ``False`` always shows the
    templated baseline for this user."""
    theme_preference: Literal["light", "dark"]
    locale_preference: str = "auto"
    """BCP-47 tag (``'en-GB'``) or ``'auto'``."""
    timezone_preference: str = "auto"
    """IANA tz id (``'Europe/London'``) or ``'auto'``."""
    ui_preferences: dict[str, Any] = Field(default_factory=dict)
    """Dashboard-shell preferences blob (home-redesign §9): sidebar
    pinned / width / per-group collapse + Quick Access kind filter.
    Stored server-side so it survives logout and follows the user
    across devices. Shape is normalised + clamped by the service; the
    chrome treats any missing key as "use the default"."""
    teams: list[CurrentUserTeamMembership] = Field(default_factory=list)
    created_at: _dt.datetime
    last_login_at: _dt.datetime | None


class MePreferencesUpdate(_Frozen):
    """POST /api/auth/me/preferences body — self-service preference edits."""

    email_notifications_enabled: bool


class MeSummaryEnrichmentUpdate(_Frozen):
    """POST /api/auth/me/summary-preferences body — toggle AI summaries."""

    summary_enrichment_enabled: bool


class MeThemeUpdate(_Frozen):
    """POST /api/auth/me/theme body — flip the dashboard theme."""

    theme: Literal["light", "dark"]


class MeUiPreferencesUpdate(_Frozen):
    """POST /api/auth/me/ui-preferences body — partial shell-pref edit.

    Every field is optional so the chrome can write through just the
    one preference that changed (e.g. only ``sidebar_width`` after a
    drag-resize) without clobbering the others. ``None`` means "leave
    this key as it was"; the service merges the supplied keys onto the
    stored blob, validates / clamps each, and drops anything unknown.
    At least one field must be set (an all-``None`` body is a 422).
    """

    sidebar_pinned: bool | None = None
    sidebar_width: int | None = Field(default=None, ge=188, le=460)
    """Pinned-rail width in px; clamped to the design's 188–460 range."""
    sidebar_groups_collapsed: dict[str, bool] | None = None
    """Per-group collapse state, keyed by group label."""
    quick_access_filter: Literal["tool", "workflow", "all"] | None = None

    @model_validator(mode="after")
    def _at_least_one(self) -> MeUiPreferencesUpdate:
        """Reject an empty body — a no-op write is a client bug."""
        if all(
            getattr(self, field) is None
            for field in (
                "sidebar_pinned",
                "sidebar_width",
                "sidebar_groups_collapsed",
                "quick_access_filter",
            )
        ):
            raise ValueError("at least one preference field must be supplied")
        return self


class MeRegionalSettingsUpdate(_Frozen):
    """POST /api/auth/me/regional-settings body.

    Both fields accept the literal ``'auto'`` so a user can defer to
    the browser's locale / timezone without us hard-coding their
    region. ``locale`` is checked against a permissive BCP-47 pattern
    plus the curated allow-list the Account page surfaces;
    ``timezone`` must resolve via :class:`zoneinfo.ZoneInfo`. Both
    constraints are re-checked in the service layer for defence in
    depth.
    """

    locale: str = Field(min_length=1, max_length=35)
    timezone: str = Field(min_length=1, max_length=64)


class MePasswordChange(_Frozen):
    """POST /api/auth/me/change-password body — self-service password change.

    Requires the *current* password as well as the new one — proof of
    possession beyond the bearer session, matching every other "change
    your own password" form on the web. Re-typing the new password is
    enforced client-side; the API only takes the single ``new_password``.
    """

    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=1)


# --- home dashboard ------------------------------------------------------


class HomeSummary(_Frozen):
    """GET /api/home/summary — the header greeting's live summary line.

    A single cheap aggregate (home-redesign §8.6) so the Home page
    doesn't fan out across every project → scenario → job at load. Every
    count is ACL-filtered to what the caller can see. The header shows
    each clause only when its feature is enabled *and* the count is > 0
    (the approvals clause additionally only for approvers), so a zero or
    an off-feature simply drops the clause.
    """

    active_jobs: int = Field(ge=0)
    """In-flight jobs the caller can see (not in a terminal state)."""
    running_optimisations: int = Field(ge=0)
    """Optimisation studies currently in the ``running`` state."""
    running_parametric_studies: int = Field(ge=0)
    """Parametric studies currently in the ``running`` state."""
    pending_approvals: int = Field(ge=0)
    """Approvals addressed to the caller (0 unless they're an approver)."""
    can_approve: bool = False
    """Whether the caller carries the approver flag — gates the
    approvals clause + card the same way the nav entry is gated."""


class SearchHit(_Frozen):
    """One result row from the global quick-find search (home §6).

    All fields are display-ready: ``label`` is always the friendly
    name (never an alias), ``sublabel`` is the context line, ``status``
    is an optional state pill, and ``route`` is the mount-relative path
    the UI navigates to on click. Results are produced server-side with
    the same ACL / feature-flag / role gating the rest of the API
    enforces, so the search can never widen what the caller can see.
    """

    type: str
    """Entity kind: ``job`` / ``project`` / ``study`` / ``optimisation``
    / ``tool`` / ``workflow`` / ``material`` / ``file``."""
    type_label: str
    """Plural display heading for the group (e.g. ``Projects``)."""
    label: str
    sublabel: str = ""
    status: str = ""
    route: str


class ProjectCreate(_Frozen):
    """POST /api/projects body."""

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)


class ProjectUpdate(_Frozen):
    """PATCH /api/projects/{pid} body. All fields optional."""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2048)
    expected_version: int | None = None


class ProjectResponse(_Frozen):
    """A project as seen over the wire (ADR-0002)."""

    id: int
    name: str
    description: str
    creator_user_id: int
    owner_user_id: int | None = None
    """Current owner — the holder of an explicit ``owner`` ACL grant
    after a transfer, else the Creator. ``None`` on the create/get/patch
    responses, which don't resolve it; populated on the list endpoint
    (home-redesign §8.1) so My Projects shows Owner without per-row
    lookups. ``creator_user_id`` stays as the immutable provenance."""
    owner_name: str = ""
    """Friendly display name of ``owner_user_id`` (empty when not resolved)."""
    my_role: str = ""
    """The caller's resolved ACL role on the project —
    ``"owner"`` / ``"member"`` / ``"viewer"`` (Creator and
    ``projects.see_all`` resolve to ``"owner"``). Empty on responses
    that don't resolve it; populated on the list endpoint for the
    "My role" column."""
    state: str
    version: int
    created_at: _dt.datetime
    updated_at: _dt.datetime


class ProjectStateChange(_Frozen):
    """POST /api/projects/{pid}/archive body."""

    state: str = Field(default="archived")


class TransferOwnership(_Frozen):
    """POST /api/projects/{pid}/transfer body."""

    new_owner_user_id: int


class AclEntryUpsert(_Frozen):
    """POST /api/projects/{pid}/acl body (single grant)."""

    user_id: int
    role: str = Field(pattern="^(owner|member|viewer)$")


class AclBatch(_Frozen):
    """PUT /api/projects/{pid}/acl body (full replace)."""

    grants: list[AclEntryUpsert] = Field(default_factory=list)


class AclEntryResponse(_Frozen):
    """One per-user grant as seen over the wire."""

    user_id: int
    role: str


class ProjectAclResponse(_Frozen):
    """The full ACL of a project: the frozen Creator + the grants."""

    creator_user_id: int
    entries: list[AclEntryResponse]


class UserDirectoryEntry(_Frozen):
    """A pickable user for the project share dialog.

    Minimal identity only (no email/role) — enough to render
    ``"Full name (username)"`` and grant an ACL row. Available to any
    authenticated user (sharing is an Owner action; the picker itself
    needs no extra permission).
    """

    user_id: int
    username: str
    full_name: str


class MetadataSelection(_Frozen):
    """One typed metadata selection on a project."""

    field_id: int
    value_id: int | None = None
    value_num: float | None = None
    value_text: str | None = None
    value_date: _dt.date | None = None


class ProjectMetadataUpdate(_Frozen):
    """PUT /api/projects/{pid}/metadata body."""

    selections: list[MetadataSelection] = Field(default_factory=list)


class MetadataValueResponse(_Frozen):
    """An allowed value of a select metadata field."""

    id: int
    value: str
    active: bool


class MetadataFieldResponse(_Frozen):
    """An admin-defined metadata field + its (active) values."""

    id: int
    key: str
    label: str
    field_type: str
    number_format: str | None = None
    format_mask: str | None = None
    required: bool
    display_order: int
    show_in_table: bool
    active: bool
    values: list[MetadataValueResponse] = Field(default_factory=list)


class MetadataFieldCreate(_Frozen):
    """POST /api/admin/metadata/fields body."""

    key: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    field_type: str = Field(pattern="^(single_select|multi_select|date|number)$")
    number_format: str | None = Field(default=None, pattern="^(integer|decimal|pattern)$")
    format_mask: str | None = Field(default=None, min_length=1, max_length=64)
    required: bool = False
    display_order: int = 0
    show_in_table: bool = False


class MetadataValueCreate(_Frozen):
    """POST /api/admin/metadata/fields/{fid}/values body."""

    value: str = Field(min_length=1, max_length=255)
    display_order: int = 0


class MetadataFieldUpdate(_Frozen):
    """PATCH /api/admin/metadata/fields/{fid} body. All optional.

    ``key`` and ``field_type`` are immutable (project selections
    depend on them) and are deliberately absent.
    """

    label: str | None = Field(default=None, min_length=1, max_length=128)
    required: bool | None = None
    display_order: int | None = None
    show_in_table: bool | None = None
    number_format: str | None = Field(default=None, pattern="^(integer|decimal|pattern)$")
    format_mask: str | None = Field(default=None, min_length=1, max_length=64)
    active: bool | None = None


class MetadataValueUpdate(_Frozen):
    """PATCH /api/admin/metadata/values/{vid} body. All optional."""

    value: str | None = Field(default=None, min_length=1, max_length=255)
    display_order: int | None = None
    active: bool | None = None


class MetadataValueReorder(_Frozen):
    """PATCH /api/admin/metadata/fields/{fid}/values/order body.

    ``value_ids`` is the field's full value-id list in the new display
    order (every value of the field, each once, retired included).
    """

    value_ids: list[int] = Field(min_length=1)


class ScenarioCreate(_Frozen):
    """POST /api/projects/{pid}/scenarios body."""

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)


class ScenarioResponse(_Frozen):
    """A scenario as seen over the wire."""

    id: int
    project_id: int
    name: str
    description: str
    created_at: _dt.datetime


# --- tools ---------------------------------------------------------------


class ToolSummaryResponse(_Frozen):
    """Compact tool entry shown in lists."""

    alias: str
    name: str
    version: str
    state: str
    short_description: str
    long_description: str | None = None
    teams: list[str]
    icon_filename: str | None = None
    """``"<alias>.png"`` when an admin has uploaded an icon for this tool,
    ``None`` otherwise. The home page Quick Access card uses presence to
    decide between fetching the image and falling back to the default
    wrench glyph."""


class ToolDetailResponse(_Frozen):
    """Full descriptor payload for the UI form renderer."""

    alias: str
    name: str
    version: str
    state: str
    descriptor: dict[str, Any]
    tool_version_id: int | None = None
    """The ``ToolVersion`` row id matching ``(alias, version)`` — the
    immutable snapshot the UI passes to V&V badge / envelope-check
    endpoints (ADR-0011 §6.5 cohort key). ``None`` when no snapshot row
    matches (a test fixture that registered a ``Tool`` without ever
    bumping its version); production installs always have one."""


class ToolVersionOption(_Frozen):
    """One selectable version of a tool (``GET /api/tools/{alias}/versions``).

    A non-admin listing that backs pickers (e.g. the V&V "record against
    version X" flow) — just the id + human version string + a flag for the
    tool's current released version, newest first.
    """

    tool_version_id: int
    version: str
    is_current: bool


# --- jobs ----------------------------------------------------------------


class JobCreate(_Frozen):
    """POST /api/scenarios/{sid}/jobs body.

    ``inputs`` is a flat ``{field_name: value}`` map that will be
    consulted to populate the v2 :class:`InputsDocument` written into
    the working directory at queue time.
    """

    name: str = Field(min_length=1, max_length=128)
    tool_alias: str = Field(min_length=1, max_length=64)
    inputs: dict[str, Any] = Field(default_factory=dict)
    justification: str | None = Field(default=None, max_length=2000)
    """Optional free-text the requestor adds when the tool needs
    approval (§16.15). Persisted on the ``approval_request`` and shown
    in the approver's review panel; ignored for non-approval tools."""


class JobResponse(_Frozen):
    """A job as seen over the wire."""

    id: int
    scenario_id: int | None
    """``None`` for a parametric-study child (homed under a study, not
    a scenario — see ``Job.parametric_study_id`` / the
    ``ck_job_exactly_one_home`` XOR). These job surfaces don't return
    study children today, but the type is honest now a job's home is a
    scenario XOR a study."""
    name: str
    tool_alias: str
    tool_version: str | None = None
    """The tool version this job actually ran — the pinned snapshot
    (``Job.tool_version_id``), e.g. ``"1.2.0"`` — which can differ from
    the tool's currently-released version. ``None`` when the endpoint
    doesn't resolve it: only ``GET /api/jobs/{id}`` populates it today
    (the Compare view shows it; the list endpoints skip the per-row
    version lookup)."""
    tool_version_id: int | None = None
    """``Job.tool_version_id`` — the snapshot row id (ADR-0011 §6.5 V&V
    cohort key). Only populated by ``GET /api/jobs/{id}`` today, alongside
    ``tool_version``; ``None`` from the list endpoints."""
    state: str
    heartbeat_state: str | None
    created_at: _dt.datetime
    started_at: _dt.datetime | None
    completed_at: _dt.datetime | None
    exit_code: int | None
    cancel_requested_at: _dt.datetime | None
    workflow_run_id: int | None = None
    """``None`` for standalone jobs; the parent run id for jobs that
    are children of a workflow run."""

    workflow_position: int | None = None
    """``None`` for standalone jobs; the 1-based tab position within
    the workflow run."""

    parametric_study_id: int | None = None
    """The parent parametric study when the job is a study child (else ``None``)."""
    optimisation_study_id: int | None = None
    """The parent optimisation study when the job is an optimisation child
    (else ``None``). Lets a study-homed run's page resolve its study + project
    meta instead of a scenario."""

    storage_location_id: int | None
    working_dir_relative: str | None
    inputs: dict[str, Any] = Field(default_factory=dict)
    """The form values the job was submitted with — surfaced so the UI can
    render an "Inputs" pane alongside the outputs without having to fetch
    inputs.json out of the working directory separately."""

    priority: bool = False
    """Skip-queue flag — ``True`` when an admin/super-user submitted the
    job via the workspace's Skip Queue button. Used by the runner's
    pickup loop to drain priority jobs ahead of FIFO peers."""


class JobLogsResponse(_Frozen):
    """Full stdout / stderr text for a finished or in-flight job.

    ``launcher`` (when non-empty) is the auto-generated launcher's own
    output, captured to ``logs/launcher.log`` separately from the
    tool's stdout/stderr. Most jobs have an empty ``launcher`` — it
    only carries content when the launcher itself crashed before the
    tool script started running (interpreter mis-pointed at, bad
    descriptor pin, etc.), which is exactly when ``stdout``/``stderr``
    would otherwise be empty.
    """

    stdout: str
    stderr: str
    launcher: str = ""

    # ---- tail mode (offsets) ----------------------------------------
    #
    # A live job's log page polls every couple of seconds. Re-reading and
    # re-sending the whole file each time is O(size) per tick per viewer,
    # which a CFD run writing hundreds of MB makes untenable. With
    # ``since_*`` the body carries only what is new, and these offsets say
    # where to resume. They are always present, so a caller that ignores
    # ``since_*`` can still start using them at any time.
    stdout_offset: int = 0
    stderr_offset: int = 0
    launcher_offset: int = 0

    truncated: bool = False
    """A log shrank below the offset asked for — it was rotated or the
    job was re-run. The chunk restarts from byte 0 and the caller must
    clear what it is showing rather than append to it."""


class JobDraftCreate(_Frozen):
    """``POST /api/scenarios/{sid}/draft-jobs`` body.

    Just enough to allocate a row in the DB. The form lives in the
    workspace and the inputs are sent later via the queue endpoint
    (``POST /api/jobs/{id}/queue``). ``name`` defaults to a
    placeholder if the caller doesn't supply one — the workspace
    will let the user edit it before the queue submission.

    ``inputs`` is optional pre-seeded form data for the
    "Use this run as a starting point" flow on ``/job-search``: the
    caller passes the source job's inputs dict and the new draft is
    created with those values pre-populated. The endpoint is still
    idempotent — if a draft already exists for ``(scenario, tool)``
    the existing draft is returned **unchanged** and any supplied
    ``inputs`` are ignored. Callers that need to overwrite an
    existing draft must do so via the queue endpoint instead.
    """

    tool_alias: str = Field(min_length=1, max_length=64)
    name: str = Field(default="Untitled run", min_length=1, max_length=128)
    inputs: dict[str, Any] = Field(default_factory=dict)


class JobQueueRequest(_Frozen):
    """``POST /api/jobs/{id}/queue`` body.

    Promotes a draft job from ``created`` to ``queued`` with the
    supplied inputs. The ``name`` is optional — when present it
    overwrites the draft's existing name; absent leaves it intact.

    ``priority=True`` requests skip-queue treatment — the runner's
    pickup loop drains priority jobs ahead of everything else.
    Requires the ``jobs.skip_queue`` permission; the API rejects the
    request from a non-privileged caller rather than silently dropping
    the flag (so the workspace can surface a clear error).
    """

    inputs: dict[str, Any] = Field(default_factory=dict)
    name: str | None = Field(default=None, min_length=1, max_length=128)
    priority: bool = False
    justification: str | None = Field(default=None, max_length=2000)
    """Optional free-text shown to the approver when the tool's policy
    holds the job (§16.15). Persisted on the ``approval_request``;
    ignored for non-approval tools."""


class JobDraftUpdateRequest(_Frozen):
    """``POST /api/jobs/{id}/draft-update`` body.

    Persists the workspace's typed values onto an existing draft job
    without queueing it for the runner. Backs the "Save Inputs"
    button — same payload shape as :class:`JobQueueRequest` so the
    workspace can use the same form payload for both. Strictly DB-only:
    no staging happens here, no working directory is touched.

    Both fields are optional so the caller can update name-only or
    inputs-only without overwriting the other side. ``inputs`` of
    ``None`` means "don't touch the inputs"; an empty dict ``{}`` is a
    legitimate "save an empty form" payload.
    """

    inputs: dict[str, Any] | None = None
    name: str | None = Field(default=None, min_length=1, max_length=128)


class JobOutputsListResponse(_Frozen):
    """List of output filenames the launcher wrote under ``outputs/``."""

    filenames: list[str]


class JobFieldFramesResponse(_Frozen):
    """``GET /api/jobs/{id}/field-frames`` — one run's field output for inline render.

    A field output's retained snapshot(s) parsed server-side (ADR-0008 §4.4):
    ``frames`` holds the per-node field values for each timestep (length 1 for
    a steady field, N for a ``field_timesteps`` series). ``points`` + ``i``/
    ``j``/``k`` are the shared surface mesh for a geometry-bearing format (a
    per-node frame aligns by index for Plotly ``Mesh3d``); ``has_geometry`` is
    ``False`` for ``.npy`` and the Job page draws a value-vs-node line instead.
    """

    output: str
    has_geometry: bool = False
    n_frames: int = 0
    n_nodes: int = 0
    points: list[list[float]] = Field(default_factory=list)
    i: list[int] = Field(default_factory=list)
    j: list[int] = Field(default_factory=list)
    k: list[int] = Field(default_factory=list)
    frames: list[list[float]] = Field(default_factory=list)


class VisibleJobResponse(_Frozen):
    """Compact job entry for the My Jobs workspace's cross-project list.

    Used by both the global Job-ID search-as-you-type picker and the
    empty-state "Recent activity" panel. Carries the **friendly,
    pre-formatted** project / scenario / tool labels so the UI can
    render directly without secondary lookups — `tool_name` (not
    alias), full project + scenario names, friendly state already
    mapped via ``matofx.ui.format`` in the workspace render path.

    ``workflow_run_id`` is non-null when the job is a workflow child.
    The workspace uses it to filter the Tool selector (workflow
    children belong to the Workflow selector, not the Tool one).
    """

    id: int
    name: str
    state: str
    created_at: _dt.datetime
    project_id: int
    project_name: str
    scenario_id: int
    scenario_name: str
    tool_alias: str
    tool_name: str
    submitter_user_id: int
    submitter_name: str
    """Friendly display name (``full_name`` if set, else ``username``).

    Drives the *Submitter* column on the Job Search results table and
    is forward-compatible for any other surface that wants to attribute
    a job to its creator without a follow-up ``/api/admin/users``
    lookup. Empty string when the source ``user`` row no longer
    exists (e.g. account deactivated then deleted) — defensive default
    rather than a 500."""
    workflow_run_id: int | None = None


class PresetResponse(_Frozen):
    """One preset entry returned by ``GET /api/tools/{alias}/presets``."""

    name: str
    description: str
    values: dict[str, Any]


class JobSearchRequest(_Frozen):
    """Body of ``POST /api/tools/{alias}/job-search``.

    ``filters`` is a flat ``{field_name: value}`` map of the user's
    chosen filter widgets — exactly the same shape as ``Job.inputs``.
    Empty / ``None`` / empty-list / empty-string values are skipped
    server-side (treated as "no constraint on this field"). The
    server splits the dict into equality vs numeric-range buckets
    using the tool's descriptor, then forwards both to
    :meth:`JobService.search_by_inputs`.

    ``numeric_tolerance_pct`` applies to every numeric filter as a
    symmetric ± percentage band (legacy default: 25%). Range
    matching is ``value*(1 - p/100) <= job_value <= value*(1 + p/100)``.
    """

    filters: dict[str, Any] = Field(default_factory=dict)
    numeric_tolerance_pct: float = 25.0


class JobSearchSimilarRequest(_Frozen):
    """Body of ``POST /api/tools/{alias}/job-search/similar``.

    The "find similar runs" extension to Job Search (§16.5): instead of
    a boolean filter, rank completed runs of the tool by how close
    their inputs sit to a single **reference point**.

    The reference point is resolved server-side as:

    1. ``reference_job_id`` — when set, that job's ``inputs`` seed the
       reference (ACL-checked; a job the actor can't see is rejected).
       This is the "seed from an existing run" mode.
    2. ``reference_inputs`` — a flat ``{field: value}`` map, overlaid on
       top of (1) if both are given. On its own it's the "typed values"
       mode. Same shape as ``Job.inputs``.

    Ranking (mirrors the roadmap spec): weighted-Euclidean distance in
    min-max-normalised numeric-factor space; categorical / string /
    select / file factors present in the reference act as exact-match
    gates. Weighting is sensitivity-derived when a recent study of the
    tool exposes standardised regression coefficients, uniform
    otherwise — the server decides and reports which in the response.

    ``top_n`` caps the ranked list (closest first). ``exclude_job_id``
    drops one job from the results — used to keep the seed run itself
    out of its own similarity list.
    """

    reference_inputs: dict[str, Any] = Field(default_factory=dict)
    reference_job_id: int | None = None
    top_n: int = Field(default=25, ge=1, le=500)
    exclude_job_id: int | None = None


class SimilarJobResponse(VisibleJobResponse):
    """One ranked row from ``job-search/similar``.

    Extends :class:`VisibleJobResponse` (so the page re-uses the exact
    same row-render path as the boolean Job Search) with the two
    ranking fields the Similar mode surfaces: the raw weighted distance
    and a 0-100 ``closeness_pct`` for the results-table column. Rows
    arrive pre-sorted closest-first.
    """

    distance: float
    """Weighted-Euclidean distance to the reference in normalised factor
    space. Smaller is closer; 0.0 is an exact match on every weighted
    factor."""
    closeness_pct: float
    """``100 * (1 - distance / sqrt(sum_of_weights))`` clamped to
    ``[0, 100]`` — a human-readable "how similar" figure for the
    Closeness column."""


class JobSearchSimilarResponse(_Frozen):
    """Response for ``POST /api/tools/{alias}/job-search/similar``.

    Carries the ranked rows plus the *provenance of the weighting* so
    the UI can be honest about how "similar" was measured: ``weighting``
    is ``"sensitivity"`` (weights came from a study's standardised
    regression coefficients) or ``"uniform"`` (every varied numeric
    factor weighted equally — no usable study found). ``weights`` is the
    per-factor weight map actually applied (``{}`` for uniform), and
    ``weighting_source`` is a short human label for the note shown under
    the results table (e.g. ``"Study 42 — Heat Sink sweep"``).
    """

    weighting: str
    weights: dict[str, float] = Field(default_factory=dict)
    weighting_source: str | None = None
    results: list[SimilarJobResponse] = Field(default_factory=list)


class DistinctInputValuesResponse(_Frozen):
    """Response for ``GET /api/tools/{alias}/distinct-input-values``.

    Powers the file-input dropdown on the Job Search page: the value
    list is the distinct set seen in ``Job.inputs[<field>]`` across
    every visible run of the tool, ordered "first observation first"
    so familiar values surface near the top.
    """

    field: str
    values: list[Any]


class FileInputHistoryEntry(_Frozen):
    """One previously-used file value with its source-job context.

    Used by the workspace's file-input picker — bundles the path the
    user previously submitted with the job + project + scenario that
    produced it, so the picker can render
    ``"<filename>  ·  Job 0001234 (Project, Scenario)"`` instead of a
    raw path string.
    """

    value: str
    job_id: int
    project_name: str
    scenario_name: str


class FileInputHistoryResponse(_Frozen):
    """Response for ``GET /api/tools/{alias}/file-input-history``."""

    field: str
    entries: list[FileInputHistoryEntry]


class FileOutputCandidate(_Frozen):
    """One file *output* produced by a visible completed job (any tool).

    Powers the file-input picker's "use a file from another job" mode:
    a standalone run can consume a *different* tool's result file (e.g.
    Bracket Stress reusing Thermal Soak's temperature field) without the
    user re-typing a mount path. The picker groups these tool → job →
    output and labels each by ``output_label`` + ``filename``.
    """

    value: str
    filename: str
    file_type: str
    output_name: str
    output_label: str
    tool_alias: str
    tool_name: str
    job_id: int
    project_name: str
    scenario_name: str
    field_array: str = ""


class FileOutputCandidatesResponse(_Frozen):
    """Response for ``GET /api/tools/file-output-candidates``."""

    entries: list[FileOutputCandidate]


# --- admin: users + roles -------------------------------------------------


class AdminUserTeamMembership(_Frozen):
    """One team the user belongs to, as shown in the admin user view."""

    id: int
    name: str


class AdminUserCreate(_Frozen):
    """POST /api/admin/users body.

    Identity is captured as split first / last name (legacy parity).
    ``full_name`` is optional — when omitted the service derives it as
    ``"{first} {last}".strip()``. Pre-existing callers that pass
    ``full_name`` without first/last (e.g. one-off scripts) keep
    working.
    """

    username: str = Field(min_length=1, max_length=64)
    email: str = Field(min_length=3, max_length=254)
    first_name: str = Field(default="", max_length=128)
    last_name: str = Field(default="", max_length=128)
    full_name: str = Field(default="", max_length=255)
    password: str = Field(min_length=1)
    role: str = Field(min_length=1, max_length=64)
    is_approver: bool = False
    team_ids: list[int] = Field(default_factory=list)
    """Team membership assigned at create time. Caller must hold ``teams.manage``."""
    capability_roles: list[str] = Field(default_factory=list)
    """Capability-role names (ADR-0005) stacked on the user at create
    time, after the account is made. Non-empty additionally requires the
    caller to hold ``roles.manage``; only ``capability``-kind roles are
    accepted. Applied in the same request transaction as the create, so
    a bad name rolls the whole creation back."""


class AdminUserResponse(_Frozen):
    """A user row as exposed over the admin API.

    No password material; role rendered as the role name (not id) so
    the UI doesn't have to do a second round-trip to map id → name.
    Team membership is bundled in so the edit page renders without a
    second list-teams call.
    """

    id: int
    username: str
    email: str
    first_name: str
    last_name: str
    full_name: str
    role: str
    is_active: bool
    is_approver: bool
    teams: list[AdminUserTeamMembership]
    capability_roles: list[str] = Field(default_factory=list)
    """Names of the capability roles (ADR-0005) stacked on this user, on
    top of their single base ``role``. Bundled in so the edit page
    renders the picker without a second round-trip."""
    created_at: _dt.datetime
    last_login_at: _dt.datetime | None
    logon_granted_at: _dt.datetime | None
    logon_revoked_at: _dt.datetime | None


class AdminSetRoleRequest(_Frozen):
    """POST /api/admin/users/{id}/set-role body."""

    role: str = Field(min_length=1, max_length=64)


class AdminSetApproverRequest(_Frozen):
    """POST /api/admin/users/{id}/set-approver body."""

    is_approver: bool


class AdminUserIdentityUpdate(_Frozen):
    """POST /api/admin/users/{id}/update-identity body.

    Edit the identity fields without touching role / approver / active
    flags (those have their own endpoints). ``full_name`` is optional;
    when blank the service re-derives it from first + last.
    """

    first_name: str = Field(default="", max_length=128)
    last_name: str = Field(default="", max_length=128)
    full_name: str = Field(default="", max_length=255)
    email: str = Field(min_length=3, max_length=254)


class AdminUserPasswordSet(_Frozen):
    """POST /api/admin/users/{id}/set-password body.

    Admin-initiated password reset. The submitted password is run
    through :func:`validate_password` against the current
    ``AuthSettings``; weak passwords are rejected with a friendly
    message rather than silently accepted.
    """

    password: str = Field(min_length=1)


class AdminUserSetTeams(_Frozen):
    """POST /api/admin/users/{id}/set-teams body.

    Bulk-replace the user's team membership with ``team_ids``. The
    service computes the diff and runs add/remove cascades so the
    audit log captures each individual change rather than a single
    "teams replaced" event.
    """

    team_ids: list[int] = Field(default_factory=list)


class AdminUserSetCapabilityRoles(_Frozen):
    """POST /api/admin/users/{id}/set-capability-roles body.

    Bulk-replace the user's capability-role set (ADR-0005) with
    ``role_names``. The service computes the diff and runs grant/revoke
    so each membership change is audited individually. Only
    ``capability``-kind role names are accepted; the base role is set
    via /set-role. Requires ``roles.manage``.
    """

    role_names: list[str] = Field(default_factory=list)


class AdminRoleResponse(_Frozen):
    """A role row as exposed over the admin API.

    ``user_count`` is bundled in so the Roles panel on the admin page
    can render member counts without a second list-users round-trip.
    ``kind`` discriminates base roles from stackable capability roles
    (ADR-0005) so the UI can present them in separate pickers.
    """

    id: int
    name: str
    description: str
    is_built_in: bool
    kind: str
    permissions: list[str]
    user_count: int


class AdminRoleCreate(_Frozen):
    """POST /api/admin/roles body.

    Built-in role names (``super_user``, ``administrator``,
    ``beta_tester``, ``regular_user``) are reserved. Permissions must
    all be slugs from the closed catalog
    (:class:`matofx.users.permissions.Permission`). ``kind`` discriminates
    a one-per-user ``base`` role from a stackable ``capability`` role
    (ADR-0005); the permission set must match the kind (a capability
    holds only ``*.curate`` slugs, a base role only the rest).
    """

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=2048)
    permissions: list[str] = Field(default_factory=list)
    kind: Literal["base", "capability"] = "base"


class AdminRolePermissionsSet(_Frozen):
    """POST /api/admin/roles/{id}/set-permissions body.

    Bulk-replace a role's permission set. ``super_user`` is locked
    (any attempt returns 403); other built-ins can be tuned freely.
    """

    permissions: list[str] = Field(default_factory=list)


class AdminRoleDescriptionUpdate(_Frozen):
    """POST /api/admin/roles/{id}/update-description body."""

    description: str = Field(default="", max_length=2048)


class AdminRoleRenameRequest(_Frozen):
    """POST /api/admin/roles/{id}/rename body.

    Renames a *custom* role. Built-in role names are reserved/locked
    (using one, or renaming a built-in, returns 409).
    """

    name: str = Field(min_length=1, max_length=64)


class AdminMembersBulkAddRequest(_Frozen):
    """Bulk add-members body, shared by the team and capability cards.

    The operator selects several users and presses *Add* once. Users
    already in the team / already holding the capability are skipped
    silently rather than erroring.
    """

    user_ids: list[int] = Field(default_factory=list)


class AdminPermissionInfo(_Frozen):
    """One entry in the catalog returned by GET /api/admin/permissions.

    The slug is what gets stored in ``role_permission`` and checked at
    gate points. The label is the title-cased rendering for the
    permission editor; the description is the plain-English summary from
    :data:`matofx.users.permissions.PERMISSION_DESCRIPTIONS`, surfaced
    inline as helper text. ``is_capability`` flags a capability-tier
    (``*.curate``) permission so the editor can group base vs capability
    permissions without hardcoding the convention (ADR-0005).
    """

    slug: str
    label: str
    description: str
    is_capability: bool


# --- admin: tool create / edit (multipart + descriptor edit) -----------


class AdminToolMetadataPayload(_Frozen):
    """JSON metadata field on the multipart create / replace-bundle endpoints.

    The form values that are *authoritative* for descriptor metadata.
    The uploaded ``descriptor.json`` file contributes the inputs /
    outputs / groups / layout sections; this payload covers everything
    else and overrides the JSON if both supply a value.
    """

    alias: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    short_description: str = Field(min_length=1, max_length=200)
    long_description: str | None = Field(default=None, max_length=2048)
    main_script: str = Field(min_length=1, max_length=255)
    teams: list[str] = Field(default_factory=list)
    downstream_tools: list[str] = Field(default_factory=list)
    cpu_cores: int = Field(default=1, ge=1, le=128)
    ram_mb: int = Field(default=512, ge=32, le=1024 * 1024)
    """RAM in megabytes (matches descriptor's ``resources.ram_mb``)."""

    gpu_memory_mb: int = Field(default=0, ge=0, le=512 * 1024)
    """GPU memory in megabytes; 0 = no GPU required."""

    licences_required: list[dict[str, Any]] = Field(default_factory=list)
    """Each entry: ``{"name": "ANSYS-Mechanical", "count": 1}``."""

    duration_class: Literal["snappy", "medium", "long", "very_long"] = "medium"
    approvals_policy: Literal["none", "pre", "post", "both"] = "none"
    approver_usernames: list[str] = Field(default_factory=list)
    email_notifications: bool = False
    """Job-state emails for this tool. Off unless asked for, matching
    ``ToolDescriptor.email_notifications`` — a caller that doesn't
    mention it doesn't get mail sent on its behalf."""


class AdminToolDescriptorUpdate(_Frozen):
    """POST /api/admin/tools/{alias}/update-descriptor body.

    Metadata-only edit — same shape as :class:`AdminToolMetadataPayload`
    minus ``alias`` (immutable) and ``main_script`` (changing the entry
    point would require a bundle change too, so it's reserved for the
    replace-bundle path).
    """

    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=32)
    short_description: str = Field(min_length=1, max_length=200)
    long_description: str | None = Field(default=None, max_length=2048)
    teams: list[str] = Field(default_factory=list)
    downstream_tools: list[str] = Field(default_factory=list)
    cpu_cores: int = Field(default=1, ge=1, le=128)
    ram_mb: int = Field(default=512, ge=32, le=1024 * 1024)
    gpu_memory_mb: int = Field(default=0, ge=0, le=512 * 1024)
    licences_required: list[dict[str, Any]] = Field(default_factory=list)
    duration_class: Literal["snappy", "medium", "long", "very_long"] = "medium"
    approvals_policy: Literal["none", "pre", "post", "both"] = "none"
    approver_usernames: list[str] = Field(default_factory=list)
    email_notifications: bool = False
    """As on :class:`AdminToolMetadataPayload`. The tool editor always
    sends the checkbox's actual value, so this default only applies to
    a caller that omits the field entirely."""

    # --- Optional descriptor-body fields for the wizard's edit flow.
    # When any of these is present in the request the endpoint
    # overlays it onto the existing descriptor; absent fields stay
    # unchanged. Backwards-compatible for metadata-only callers.
    inputs: list[dict[str, Any]] | None = None
    input_groups: list[dict[str, Any]] | None = None
    input_column_layout: dict[str, float] | None = None
    outputs: list[dict[str, Any]] | None = None
    output_groups: list[dict[str, Any]] | None = None
    output_column_layout: dict[str, float] | None = None
    retain_files: list[str] | None = None
    delete_io_files: list[str] | None = None
    execution_backend: dict[str, Any] | None = None
    input_groups_start_collapsed: bool | None = None
    require_all_groups_opened: bool | None = None
    # §16.16 conditional rules + §F30 cross-field constraints. Loose
    # dicts here (like inputs / groups); the shapes are validated
    # downstream by ``ToolDescriptor.model_validate`` after the merge.
    dependencies: list[dict[str, Any]] | None = None
    cross_field_constraints: list[dict[str, Any]] | None = None
    # Tool-author-supplied presets — authored on the wizard's Presets
    # tab. Same overlay semantics: ``None`` leaves the existing
    # descriptor's presets alone, ``[]`` clears them.
    presets: list[dict[str, Any]] | None = None


class AdminToolSetApprovers(_Frozen):
    """POST /api/admin/tools/{alias}/set-approvers body.

    Bulk-replace the tool's ``approver_usernames``. Used by the
    user-edit page's sole-approver replacement flow — letting the
    admin swap a leaving approver for someone else *before* the
    deactivate / set-approver call would otherwise fail with
    :class:`SoleApproverError`.
    """

    approver_usernames: list[str] = Field(default_factory=list)


class AdminToolVersionEntry(_Frozen):
    """One row in the change-log modal — drives the per-snapshot bullets.

    ``created_by_label`` is pre-resolved to ``"Full Name (username)"``
    so the UI doesn't need a second list-users call to render the
    log. ``changes`` is the per-field old/new map; the modal renders
    each entry as a bullet (``name: 'X' → 'Y'``).
    """

    id: int
    version: str
    created_at: _dt.datetime
    created_by_label: str
    change_summary: str
    changes: dict[str, Any]


class AdminToolVersionList(_Frozen):
    """GET /api/admin/tools/{alias}/versions response — newest first."""

    versions: list[AdminToolVersionEntry]


class ToolDraftSaveRequest(_Frozen):
    """POST /api/admin/tool-drafts body — create, or update when ``draft_id`` set.

    Holds the New Tool form's typed metadata plus the wizard's
    descriptor-shaped contribution under ``payload``. Never carries the
    uploaded bundle files — those are re-attached at the final install.
    """

    draft_id: int | None = None
    title: str = Field(default="", max_length=128)
    alias: str = Field(default="", max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolDraftSummary(_Frozen):
    """One row in the New Tool "Drafts" list (compact)."""

    id: int
    title: str
    alias: str
    updated_at: _dt.datetime


class ToolDraftResponse(_Frozen):
    """Full draft — drives both the save round-trip and the resume prefill."""

    id: int
    title: str
    alias: str
    payload: dict[str, Any]
    created_at: _dt.datetime
    updated_at: _dt.datetime


class ToolDraftList(_Frozen):
    """GET /api/admin/tool-drafts response — the caller's drafts, newest first."""

    drafts: list[ToolDraftSummary]


class AdminToolDetailResponse(_Frozen):
    """GET /api/admin/tools/{alias} — wrapper metadata + full descriptor JSON.

    The list endpoint returns a compact :class:`AdminToolResponse` so
    the listing page stays snappy; this endpoint exists for the edit
    page which needs every descriptor field to pre-populate the form.
    """

    alias: str
    name: str
    version: str
    state: str
    short_description: str
    long_description: str | None = None
    archived_at: _dt.datetime | None
    bundle_path: str
    descriptor: dict[str, Any]
    run_cap: int | None = None
    """Per-tool parametric-study run cap (admin/SU-editable). ``None``
    -> fall through to the install-wide default (500). See ADR-0001
    2026-05-20 revision."""
    python_env_id: int | None = None
    """The ``python_env`` row this tool's jobs run under (admin/SU-editable
    on the Edit Tool page). ``None`` -> the launcher falls back to the
    MATOFX server's own interpreter (the "main MATOFX environment")."""
    tool_version_id: int | None = None
    """The ``ToolVersion`` row id matching ``(alias, version)`` — the
    V&V cohort key (ADR-0011 §6.5). ``None`` when no snapshot row
    matches (only in tests where a Tool was constructed without a
    bump); production installs always have one."""


class ToolRunCapRequest(_Frozen):
    """``POST /api/admin/tools/{alias}/run-cap`` body.

    ``run_cap = None`` clears the override (study creation falls back
    to the install default). Otherwise the integer is the per-tool cap.
    """

    run_cap: int | None = Field(default=None, ge=1)


class ToolPythonEnvRequest(_Frozen):
    """``POST /api/admin/tools/{alias}/python-env`` body.

    ``python_env_id = None`` clears the pin (jobs fall back to the
    MATOFX server's own interpreter). Otherwise the id must reference
    an active ``python_env`` row.
    """

    python_env_id: int | None = Field(default=None, ge=1)


class AdminToolFileEntry(_Frozen):
    """One file in the bundle, as listed by the Files-tab tree."""

    relative_path: str
    size_bytes: int
    is_text: bool


class AdminToolFileList(_Frozen):
    """GET /api/admin/tools/{alias}/files — every file in the current bundle.

    The UI filters ``is_text`` to populate the in-browser editor's tree;
    binary files are still listed (so the operator can see they exist)
    but the click-to-edit action is disabled.
    """

    bundle_path: str
    files: list[AdminToolFileEntry]


class AdminToolFileContent(_Frozen):
    """GET /api/admin/tools/{alias}/files/{path} — UTF-8 contents of a single file."""

    relative_path: str
    contents: str
    size_bytes: int


class AdminToolFileSaveRequest(_Frozen):
    """POST /api/admin/tools/{alias}/files/{path} body — the new contents."""

    contents: str
    expected_row_version: int | None = None


# --- admin: teams ---------------------------------------------------------


class AdminTeamCreate(_Frozen):
    """POST /api/admin/teams body."""

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=2048)


class AdminTeamUpdate(_Frozen):
    """PUT /api/admin/teams/{id} body — rename and/or re-describe a team."""

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=2048)


class AdminTeamResponse(_Frozen):
    """A team row as exposed over the admin API."""

    id: int
    name: str
    description: str
    member_count: int


class AdminTeamMemberResponse(_Frozen):
    """One member of a team."""

    user_id: int
    username: str
    full_name: str


class AdminTeamMemberAddRequest(_Frozen):
    """POST /api/admin/teams/{id}/members body."""

    user_id: int


# --- admin: tools (extended view) ----------------------------------------


class AdminToolResponse(_Frozen):
    """Tool row for the admin tools list — includes archived + state info.

    ``approver_usernames`` / ``teams`` / ``approvals_policy`` /
    ``downstream_tools`` are bundled in so the admin pages can render
    the per-tool summary lines (Visible by, Approvers, Downstream tools)
    + the user-edit page's sole-approver pre-flight without making
    one round-trip per tool.
    """

    alias: str
    name: str
    version: str
    state: str
    short_description: str
    long_description: str | None = None
    archived_at: _dt.datetime | None
    bundle_path: str
    approver_usernames: list[str] = Field(default_factory=list)
    teams: list[str] = Field(default_factory=list)
    approvals_policy: str = "none"
    downstream_tools: list[str] = Field(default_factory=list)


class AdminPromoteRequest(_Frozen):
    """POST /api/admin/tools/{alias}/promote body."""

    new_state: str = Field(min_length=1, max_length=16)


class AdminArchiveRequest(_Frozen):
    """POST /api/admin/tools/{alias}/archive body."""

    reason: str = Field(default="", max_length=2048)


# --- admin: storage_location + python_env + runtime_settings -------------


class AdminStorageLocationCreate(_Frozen):
    """POST /api/admin/storage-locations body."""

    name: str = Field(min_length=1, max_length=64)
    mount_path: str = Field(min_length=1, max_length=512)


class AdminStorageLocationResponse(_Frozen):
    """A storage_location row as exposed over the admin API."""

    id: int
    name: str
    mount_path: str
    is_active: bool
    retired_at: _dt.datetime | None
    created_at: _dt.datetime


class AdminPythonEnvCreate(_Frozen):
    """POST /api/admin/python-envs body."""

    name: str = Field(min_length=1, max_length=64)
    interpreter_path: str = Field(min_length=1, max_length=512)
    storage_location_id: int | None = None


class AdminPythonEnvResponse(_Frozen):
    """A python_env row as exposed over the admin API."""

    id: int
    name: str
    interpreter_path: str
    storage_location_id: int | None
    is_active: bool
    created_at: _dt.datetime


class AdminAosHostCreate(_Frozen):
    """POST /api/admin/aos-hosts body. ``transport`` is derived from ``os``."""

    name: str = Field(min_length=1, max_length=64)
    hostname: str = Field(min_length=1, max_length=255)
    os: Literal["linux", "windows"]
    username: str = Field(min_length=1, max_length=64)
    secret_ref: str = Field(min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    max_concurrency: int = Field(default=1, ge=1, le=4096)
    cpu_cores: int | None = Field(default=None, ge=1, le=4096)
    memory_mb: int | None = Field(default=None, ge=1)


class AdminAosHostUpdate(_Frozen):
    """PUT /api/admin/aos-hosts/{id} body — full editable state (name immutable)."""

    hostname: str = Field(min_length=1, max_length=255)
    os: Literal["linux", "windows"]
    username: str = Field(min_length=1, max_length=64)
    secret_ref: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    max_concurrency: int = Field(ge=1, le=4096)
    cpu_cores: int | None = Field(default=None, ge=1, le=4096)
    memory_mb: int | None = Field(default=None, ge=1)


class AdminAosHostResponse(_Frozen):
    """An aos_host row as exposed over the admin API."""

    id: int
    name: str
    hostname: str
    os: str
    transport: str
    port: int
    username: str
    secret_ref: str
    is_active: bool
    max_concurrency: int
    cpu_cores: int | None
    memory_mb: int | None
    secret_present: bool
    """Whether ``secret_ref`` resolves in the configured secrets store."""
    created_at: _dt.datetime


class AdminHpcClusterCreate(_Frozen):
    """POST /api/admin/hpc-clusters body. ``transport`` is derived from ``scheduler``."""

    name: str = Field(min_length=1, max_length=64)
    head_node: str = Field(min_length=1, max_length=255)
    scheduler: Literal["slurm", "mshpc"]
    username: str = Field(min_length=1, max_length=64)
    secret_ref: str = Field(min_length=1, max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    default_queue: str | None = Field(default=None, max_length=64)
    account: str | None = Field(default=None, max_length=64)
    """Slurm ``--account``. Omitted from the submission when unset."""
    wckey: str | None = Field(default=None, max_length=64)
    """Slurm ``--wckey``. Omitted from the submission when unset."""


class AdminHpcClusterUpdate(_Frozen):
    """PUT /api/admin/hpc-clusters/{id} body — full editable state (name immutable)."""

    head_node: str = Field(min_length=1, max_length=255)
    scheduler: Literal["slurm", "mshpc"]
    username: str = Field(min_length=1, max_length=64)
    secret_ref: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    default_queue: str | None = Field(default=None, max_length=64)
    account: str | None = Field(default=None, max_length=64)
    wckey: str | None = Field(default=None, max_length=64)


class AdminHpcClusterResponse(_Frozen):
    """An hpc_cluster row as exposed over the admin API."""

    id: int
    name: str
    head_node: str
    scheduler: str
    transport: str
    port: int
    username: str
    secret_ref: str
    default_queue: str | None
    account: str | None
    wckey: str | None
    is_active: bool
    secret_present: bool
    """Whether ``secret_ref`` resolves in the configured secrets store."""
    created_at: _dt.datetime


class AdminRuntimeSettingsResponse(_Frozen):
    """Snapshot of every admin-tunable runner setting."""

    settings: dict[str, Any]


class AdminRuntimeSettingValue(_Frozen):
    """POST /api/admin/runtime-settings/{key} body."""

    value: Any


# --- admin: general settings --------------------------------------------


class AdminGeneralSettingCatalogEntry(_Frozen):
    """One row in the General Settings catalog as the UI needs it.

    Mirrors :class:`matofx.runner.settings.SettingSpec` minus the
    Python-only callable / class fields. Carries everything the form
    renderer needs to draw the input control (kind, range, choices,
    accepted mime types) plus the role gate it should show as a
    lock icon when the caller can't edit it.
    """

    key: str
    kind: Literal["int", "float", "str", "bool", "secret", "enum", "image"]
    label: str
    description: str
    default: Any
    requires_role: str
    delegatable_to: str | None
    """When set, super-users may grant callers of this role write
    access to the row at runtime. ``None`` for settings that aren't
    delegatable."""

    unit: str
    min_value: float | None
    max_value: float | None
    choices: list[str] | None
    accepted_mime_types: list[str] | None
    redacted: bool
    pending_restart: bool
    zero_means_never: bool


class AdminGeneralSettingsResponse(_Frozen):
    """GET /api/admin/general-settings — full snapshot + catalog.

    ``catalog`` is the static description of every row (stable
    across calls); ``values`` is the effective value per key, with
    secret fields replaced by a sentinel so they never leave the
    server. ``delegations`` carries the currently-granted
    per-row delegations (``{key: delegated_to_role}``); keys not in
    the map have no active delegation.
    """

    catalog: list[AdminGeneralSettingCatalogEntry]
    values: dict[str, Any]
    delegations: dict[str, str] = {}


class AdminGeneralSettingValue(_Frozen):
    """POST /api/admin/general-settings/{key} body."""

    value: Any


class AdminGeneralSettingDelegation(_Frozen):
    """POST /api/admin/general-settings/{key}/delegate body.

    ``delegated_to_role=None`` clears the grant. Otherwise the role
    string must match the catalog spec's ``delegatable_to`` exactly;
    any mismatch is rejected by the service so the delegation can
    never widen beyond what the catalog advertises.
    """

    delegated_to_role: str | None


class AdminAutoDisablePreviewRow(_Frozen):
    """One user that the next sweep would auto-disable, for the preview list.

    Surfaced to the operator on the General Settings save flow when
    they reduce ``auto_disable_inactive_days`` so they can spot any
    name they need to re-activate / exempt before the change takes
    effect.
    """

    user_id: int
    username: str
    full_name: str
    last_login_at: _dt.datetime | None
    created_at: _dt.datetime
    days_inactive: int
    role: str


class AdminAutoDisablePreviewResponse(_Frozen):
    """GET /api/admin/inactive-users/preview?threshold_days=N response.

    ``count`` is the total number of users the sweep would disable
    at the proposed threshold; ``users`` is up to ``limit`` of them
    (newest-inactive first) so the operator can scan critical names
    without paginating. ``threshold_days`` echoes the query param so
    the UI doesn't drift from what it asked for.
    """

    threshold_days: int
    count: int
    users: list[AdminAutoDisablePreviewRow]


# --- admin: db explorer --------------------------------------------------


class AdminDbTableInfo(_Frozen):
    """One entry in the explorer's table listing.

    ``columns`` are the columns the explorer will surface (i.e. every
    column on the underlying table EXCEPT the redacted ones). The
    redacted column names are listed separately so the UI can mark
    those cells with an opaque placeholder.
    """

    name: str
    row_count: int
    columns: list[str]
    redacted_columns: list[str]


class AdminDbTableListResponse(_Frozen):
    """GET /api/admin/db/tables response."""

    tables: list[AdminDbTableInfo]


class AdminDbTablePageResponse(_Frozen):
    """GET /api/admin/db/tables/{name} response — one page of rows."""

    columns: list[str]
    """Visible columns first, then redacted columns appended at the end."""

    redacted_columns: list[str]
    rows: list[dict[str, Any]]
    total_rows: int
    page: int
    page_size: int


class AdminDbColumnDistinctResponse(_Frozen):
    """GET /api/admin/db/tables/{name}/columns/{column}/distinct response.

    Drives the filter builder's value-picker: when ``too_many`` is false
    the UI offers ``values`` (plus an "(empty)" choice if ``has_null``);
    otherwise it falls back to free-text search only.
    """

    column: str
    too_many: bool
    values: list[str]
    has_null: bool
    limit: int


# --- admin: user activity ------------------------------------------------


class AdminUserActivitySubject(_Frozen):
    """Identity slice for the picked user."""

    id: int
    username: str
    full_name: str
    role: str
    is_active: bool
    is_approver: bool


class AdminUserActivityProject(_Frozen):
    """One row in the per-user "projects owned" section."""

    id: int
    name: str
    description: str
    created_at: _dt.datetime
    scenario_count: int


class AdminUserActivityJob(_Frozen):
    """One row in the per-user "jobs" section.

    Only jobs the user submitted appear here — cancellation events
    don't bring a job into this list. (Audit-trail still records the
    user's cancel actions; check the audit section for those.)
    """

    id: int
    name: str
    tool_alias: str
    state: str
    created_at: _dt.datetime
    completed_at: _dt.datetime | None
    scenario_id: int | None  # None for a parametric-study child job


class AdminUserActivityScenario(_Frozen):
    """One row in the per-user "scenarios created" section.

    The ``scenario`` ORM table has no ``created_by_user_id`` column, so
    we attribute creation by the audit log: rows with
    ``action=hierarchy.scenario.create`` and ``actor=user:{id}`` and
    ``outcome=success``. The ``resource_id`` is then resolved against
    the live ``scenario`` table to surface name / project info.
    """

    id: int
    name: str
    project_id: int
    project_name: str
    created_at: _dt.datetime


class AdminUserActivityAuditEvent(_Frozen):
    """One row in the per-user audit-trail section."""

    id: int
    timestamp: _dt.datetime
    action: str
    resource_type: str
    resource_id: str
    outcome: str
    reason: str | None


class AdminUserActivityLogonEvent(_Frozen):
    """One row in the per-user logon-grant/revoke history section."""

    id: int
    event: str
    at: _dt.datetime
    actor_user_id: int | None
    reason: str | None


class AdminUserActivityWorkflow(_Frozen):
    """One row in the per-user "workflows" section.

    Covers both admin-released (``kind='admin'``) and custom
    (``kind='custom'``) workflows that the user either *owns* — the
    current owner of a custom workflow, or the installer of record (an
    admin who installed an admin-released workflow) — or *has run* (they
    submitted at least one :class:`~matofx.workflows.orm.WorkflowRun` of
    it). ``run_count`` / ``last_run_at`` describe *this user's* runs
    only, not the workflow's global activity.
    """

    id: int
    alias: str
    name: str  # display label (Workflow.name), never the alias
    kind: str  # 'admin' | 'custom'
    state: str
    owned: bool
    archived: bool
    run_count: int
    last_run_at: _dt.datetime | None
    created_at: _dt.datetime


class AdminUserActivityNamedRecord(_Frozen):
    """One row in a per-user "records you created" section.

    Shared shape for the parametric-studies, optimisations,
    surrogate-models and job-cohorts sections — each lists the records
    the user owns / created, newest first, carrying just enough to
    identify and open them (the dedicated pages hold the full detail).
    """

    id: int
    name: str  # display label
    state: str
    created_at: _dt.datetime


class AdminUserActivityResponse(_Frozen):
    """GET /api/admin/users/{user_id}/activity response.

    Bundles every per-user data slice the UI surfaces. Each list is
    capped at the requested ``limit`` (default 50) — large tail
    histories will need a follow-up "page through the full audit log
    for user X" affordance, but Phase C ships top-N.
    """

    subject: AdminUserActivitySubject
    projects: list[AdminUserActivityProject]
    scenarios: list[AdminUserActivityScenario]
    jobs: list[AdminUserActivityJob]
    audit_events: list[AdminUserActivityAuditEvent]
    logon_events: list[AdminUserActivityLogonEvent]
    workflows: list[AdminUserActivityWorkflow]
    """Workflows the user owns or has run — admin-released and custom,
    merged into one row per workflow, newest activity first. Empty when
    the user has neither authored nor run any workflow."""
    parametric_studies: list[AdminUserActivityNamedRecord]
    """Parametric studies the user created, newest first."""
    optimisations: list[AdminUserActivityNamedRecord]
    """Optimisation studies the user created, newest first."""
    surrogate_models: list[AdminUserActivityNamedRecord]
    """Surrogate models the user owns, newest first."""
    cohorts: list[AdminUserActivityNamedRecord]
    """Job cohorts the user created, newest first."""


# --- admin: audit log browser -------------------------------------------


class AdminAuditLogEntry(_Frozen):
    """One row in the audit log browser.

    Mirrors the 18-field ``audit_log`` schema. ``payload`` is returned
    as a Python dict so the UI can render it as pretty-printed JSON;
    ``hash`` / ``prior_hash`` / ``signature`` / ``key_id`` are included
    so an admin can hand them to ``matofx audit verify`` for follow-up.
    """

    id: int
    timestamp: _dt.datetime
    actor: str
    actor_label: str | None = None
    """Human-friendly rendering of ``actor`` — for ``user:<id>`` actors this
    is ``Full Name (username)`` (or the username alone); for non-user actors
    (``system``, ``service:*``, ``anonymous``) it echoes ``actor``. ``None``
    only when the referenced user no longer exists."""
    actor_type: str
    action: str
    resource_type: str
    resource_id: str
    outcome: str
    request_id: str | None
    session_id: str | None
    source_ip: str | None
    prior_hash: str
    hash: str
    signature: str | None
    key_id: str | None
    schema_version: int
    payload: dict[str, Any]
    reason: str | None
    correlation_id: str | None


class AdminAuditLogPage(_Frozen):
    """GET /api/admin/audit/log response — one page of entries."""

    rows: list[AdminAuditLogEntry]
    total_rows: int
    page: int
    page_size: int


class AdminAuditFiltersResponse(_Frozen):
    """GET /api/admin/audit/filters response — dropdown option lists.

    Distinct values actually present in the log, so the filter bar only
    ever offers actions / resource types that exist. ``actor_types`` is
    the closed vocabulary (``user``/``service``/``system``/``anonymous``)
    restricted to those in use.
    """

    actions: list[str]
    resource_types: list[str]
    actor_types: list[str]


class AdminAuditVerifyFailure(_Frozen):
    """One verification failure inside :class:`AdminAuditVerifyResponse`."""

    record_id: int
    reason: str


class AdminAuditVerifyResponse(_Frozen):
    """POST /api/admin/audit/verify response."""

    ok: bool
    records_checked: int
    failures: list[AdminAuditVerifyFailure]
    signing_key_configured: bool
    """``True`` when the install has a signing key on disk and signature
    checks were therefore performed. ``False`` means the chain's
    hash-link integrity was checked but signatures were skipped."""


# --- admin: diagnostics --------------------------------------------------


class AdminDiagnosticsFailedJobExample(_Frozen):
    """One example row inside the "failed jobs (7d)" card."""

    id: int
    name: str
    tool_alias: str
    scenario_id: int | None  # None for a parametric-study child job
    completed_at: _dt.datetime | None
    updated_at: _dt.datetime


class AdminDiagnosticsFailedJobs(_Frozen):
    """Failed-jobs-in-the-last-7-days card data."""

    count: int
    examples: list[AdminDiagnosticsFailedJobExample]
    """Up to 10 newest examples; empty when ``count == 0``."""


class AdminDiagnosticsQueueDepth(_Frozen):
    """Current runner queue snapshot (live counts)."""

    queued: int
    running: int


class AdminDiagnosticsRecentUser(_Frozen):
    """One entry in the recently-active-users card.

    Recency is taken from the audit log: ``MAX(timestamp)`` of rows
    whose ``actor`` matches ``"user:{id}"``. Users with no audit
    activity at all simply don't appear.
    """

    user_id: int
    username: str
    full_name: str
    last_active_at: _dt.datetime


class AdminDiagnosticsOrphanedDir(_Frozen):
    """One orphaned working directory found on a storage location."""

    storage_location_id: int
    storage_location_name: str
    relative_path: str
    """Path under ``storage_location.mount_path`` (e.g. ``jobs/job_0099999``)."""


class AdminDiagnosticsOrphanedDirs(_Frozen):
    """Orphan-detection result for the working-dirs card.

    The check is best-effort: when the API host can't read a storage
    location's mount path we surface ``checked=False`` with a friendly
    note, rather than 500ing the whole diagnostics page. ``items`` is
    capped at 100 so a runaway directory listing doesn't blow up the
    response.
    """

    checked: bool
    note: str
    items: list[AdminDiagnosticsOrphanedDir]


class AdminWorkflowMemberEntry(_Frozen):
    """One member in a workflow's ordered tool list, as seen over the wire."""

    position: int = Field(ge=1)
    tool_alias: str
    tool_name: str = ""
    """Pretty name resolved from the linked tool row. Empty when the
    tool was archived underneath the workflow (the workflow archive
    cascade should catch this, so it's defensive)."""

    tool_version: str = ""
    """The currently-released version of the tool (floats — workflows
    don't pin)."""

    tool_state: str = ""
    """The tool's lifecycle state (``new`` / ``test`` / ``released`` /
    or ``archived`` for a previously-removed tool). Used by the
    admin page to surface "this workflow contains a New-state tool"
    warnings."""


class AdminWorkflowResponse(_Frozen):
    """A workflow as exposed over the admin API.

    Bundles the descriptor metadata + ordered member list so the
    listing + edit pages render with a single round-trip.
    """

    id: int
    alias: str
    name: str
    version: str
    state: str
    kind: str = "admin"
    """``'admin'`` or ``'custom'``. Custom workflows live in ``released``
    until archived and don't ramp through the state machine — the admin
    listing uses this to hide promote / rollback controls for them."""
    description: str
    teams: list[str]
    members: list[AdminWorkflowMemberEntry]
    archived_at: _dt.datetime | None
    archive_reason: str | None
    created_at: _dt.datetime
    updated_at: _dt.datetime
    icon_filename: str | None = None
    """Currently-uploaded icon filename, or ``None`` when none has been
    set. The admin edit page uses this to show "Current icon" vs "No
    icon set yet"."""


class WorkflowChainEntry(_Frozen):
    """One member tool in a workflow summary's flow-ordered chain.

    Carries the tool's human-facing **display name** (never the alias —
    ``feedback_use_labels_not_names``) plus :attr:`flows_from_prev`: a
    flag the Workflow Quick Access card uses to pick the separator drawn
    *before* this entry. ``True`` => the previous member's output feeds
    this one (a descriptor-inferred edge exists between the adjacent
    pair) and the card draws an arrow ``→``; ``False`` => the two aren't
    directly chained and the card draws a neutral dot ``·`` so a
    standalone tool reads as separated rather than sequenced. The first
    entry always carries ``flows_from_prev=False`` (nothing precedes
    it).
    """

    label: str
    flows_from_prev: bool
    archived: bool = False
    """``True`` when the member tool is archived — the card renders it
    dimmed/struck-through. Resolved from the tool's ``archived_at``."""


class WorkflowSummaryResponse(_Frozen):
    """Compact workflow entry shown in user-facing lists.

    Matches :class:`ToolSummaryResponse` in spirit — just enough for
    the scenario page's chooser to render without a follow-up round
    trip.
    """

    alias: str
    name: str
    version: str
    state: str
    description: str
    teams: list[str]
    tool_aliases: list[str]
    member_chain: list[WorkflowChainEntry] = Field(default_factory=list)
    """The member tools in workflow ``position`` order, each with its
    display label and an arrow-vs-dot ``flows_from_prev`` flag. Drives
    the Workflow Quick Access card's tool-chain line (replaces the old
    bare ``N tools`` count). Empty only for a workflow with no
    resolvable members."""
    icon_filename: str | None = None
    """``"<alias>.png"`` when an admin has uploaded an icon for this
    workflow, ``None`` otherwise. Drives the Workflow Quick Access
    card's image-vs-schema-glyph choice on the home page."""


class WorkflowCreateRequest(_Frozen):
    """``POST /api/admin/workflows`` body.

    Simple shape: identity + description + teams + ordered tool
    aliases. Output→input wiring isn't supplied — it's discovered
    at run time from each member tool's descriptor.
    """

    alias: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)
    teams: list[str] = Field(default_factory=list)
    tool_aliases: list[str] = Field(min_length=1)


class WorkflowUpdateRequest(_Frozen):
    """``POST /api/admin/workflows/{alias}/update`` body.

    Same shape as the create request minus ``alias`` (immutable).
    Membership changes bump the minor version; metadata-only edits
    bump the patch version (service layer decides).
    """

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)
    teams: list[str] = Field(default_factory=list)
    tool_aliases: list[str] = Field(min_length=1)


class WorkflowPromoteRequest(_Frozen):
    """``POST /api/admin/workflows/{alias}/promote`` body."""

    new_state: Literal["new", "test", "released"]


class WorkflowArchiveRequest(_Frozen):
    """``POST /api/admin/workflows/{alias}/archive`` body."""

    reason: str = Field(default="", max_length=2048)


class WorkflowVersionEntry(_Frozen):
    """One row in the workflow change-log."""

    id: int
    version: str
    created_at: _dt.datetime
    created_by_label: str
    change_summary: str
    changes: dict[str, Any]
    members_snapshot: list[dict[str, Any]]


class WorkflowVersionList(_Frozen):
    """``GET /api/admin/workflows/{alias}/versions`` response — newest first."""

    versions: list[WorkflowVersionEntry]


class WorkflowRunStartRequest(_Frozen):
    """``POST /api/scenarios/{sid}/workflow-runs`` body.

    Just the workflow's alias and an optional friendly name for the
    run. Member tools are derived from the workflow definition; their
    versions float to the currently-released snapshot.
    """

    workflow_alias: str = Field(min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=128)


class WorkflowRunChildJob(_Frozen):
    """One child job, as exposed inside a workflow-run response."""

    id: int
    workflow_position: int
    tool_alias: str
    tool_name: str
    name: str
    state: str
    created_at: _dt.datetime
    started_at: _dt.datetime | None
    completed_at: _dt.datetime | None


class WorkflowRunResponse(_Frozen):
    """Full workflow-run shape for the run page.

    Bundles the run header + workflow identity + ordered child jobs +
    computed run-level state. The page renders one tab per child;
    gating + auto-fill is driven by the child states + the workflow's
    descriptor (read lazily on the form-render path).
    """

    id: int
    workflow_id: int
    workflow_alias: str
    workflow_name: str
    workflow_version_snapshot: str
    scenario_id: int
    scenario_name: str = ""
    project_id: int
    project_name: str = ""
    name: str
    submitted_by_user_id: int
    submitted_by_label: str
    state: str
    """Computed: ``draft`` / ``running`` / ``complete`` / ``failed`` /
    ``cancelled``. Not stored on the row."""

    children: list[WorkflowRunChildJob]
    cancel_requested_at: _dt.datetime | None
    created_at: _dt.datetime
    updated_at: _dt.datetime


class WorkflowRunSummary(_Frozen):
    """Compact workflow-run entry for cross-scenario listings."""

    id: int
    workflow_alias: str
    workflow_name: str
    scenario_id: int
    scenario_name: str
    project_id: int
    project_name: str
    name: str
    state: str
    child_count: int
    created_at: _dt.datetime


# ----- custom workflows (user-owned) -------------------------------------


class CustomWorkflowMemberEntry(_Frozen):
    """One member tool inside a custom-workflow response."""

    position: int
    tool_alias: str
    tool_name: str
    tool_state: str
    """The tool's current state: ``new`` / ``test`` / ``released`` /
    ``archived``. The owner-form filters down to tools the user can
    see, but the response surfaces the live state so the run page can
    flag a downstream tool that's been pulled since save time."""


class CustomWorkflowShareEntry(_Frozen):
    """One share recipient inside a custom-workflow response."""

    user_id: int
    username: str
    full_name: str
    shared_at: _dt.datetime


class CustomWorkflowResponse(_Frozen):
    """Full custom-workflow shape for the ``/my-workflows`` page.

    Bundles identity + members + share list + lineage metadata so the
    edit form renders without follow-up round trips.
    """

    id: int
    alias: str
    name: str
    description: str
    version: str
    visibility: str
    """``'private'`` or ``'shared'`` — derived from the share list at
    save time; the field's still surfaced so the UI doesn't have to
    re-derive it."""

    owner_id: int
    owner_username: str
    owner_full_name: str
    is_owner: bool
    """Convenience: ``True`` when the caller of the GET is the row's
    owner. Drives the Edit/Archive/Share action gating in the UI."""

    members: list[CustomWorkflowMemberEntry]
    shares: list[CustomWorkflowShareEntry]
    copied_from_workflow_id: int | None
    archived_at: _dt.datetime | None
    archive_reason: str | None
    archived_by_user_id: int | None
    created_at: _dt.datetime
    updated_at: _dt.datetime


class CustomWorkflowSummary(_Frozen):
    """Compact entry for the ``/my-workflows`` list.

    Smaller than :class:`CustomWorkflowResponse` so the list page can
    render dozens of rows without dragging the full share list onto
    the wire per row.
    """

    id: int
    alias: str
    name: str
    visibility: str
    owner_id: int
    owner_username: str
    owner_full_name: str
    is_owner: bool
    member_count: int
    member_tools: list[str]
    """Member tool *names* (labels, not aliases) in run order, so the
    list row can show which tools a workflow chains without a follow-up
    fetch of the full row. Surfaces a tool's name even when it's since
    been archived; falls back to the alias if the tool row is gone."""

    share_count: int
    archived_at: _dt.datetime | None
    updated_at: _dt.datetime


class CustomWorkflowCreateRequest(_Frozen):
    """``POST /api/workflows/custom`` body.

    The service computes the run order from descriptors, so the
    caller's ``tool_aliases`` list is a *selection*, not an order —
    members are written in the computed order on save. Sharing flows
    through ``share_with_user_ids``; an empty list means the workflow
    starts private.
    """

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)
    tool_aliases: list[str] = Field(min_length=2, max_length=10)
    share_with_user_ids: list[int] = Field(default_factory=list, max_length=128)


class CustomWorkflowUpdateRequest(_Frozen):
    """``POST /api/workflows/custom/{id}/update`` body — same shape as create."""

    name: str = Field(min_length=1, max_length=128)
    description: str = Field(default="", max_length=2048)
    tool_aliases: list[str] = Field(min_length=2, max_length=10)
    share_with_user_ids: list[int] = Field(default_factory=list, max_length=128)


class CustomWorkflowArchiveRequest(_Frozen):
    """``POST /api/workflows/custom/{id}/archive`` body.

    ``reason`` is optional for owner-archives, required for
    inspector-archives. The service re-checks; this schema just
    carries the value.
    """

    reason: str = Field(default="", max_length=2048)


class CustomWorkflowShareCandidate(_Frozen):
    """One entry in the share-picker dropdown.

    Returned by ``GET /api/workflows/custom/_meta/share-candidates``.
    Excludes the caller and any deactivated accounts. The picker is
    open to every authenticated user — sharing semantics are scoped
    by the service-layer recipient-visibility check, not by hiding
    candidates.
    """

    user_id: int
    username: str
    full_name: str


class TutorialResponse(_Frozen):
    """A tutorial row as exposed to the home page + admin tutorials list.

    Scope is encoded by which of ``tool_alias`` / ``workflow_alias``
    is set; both ``None`` ⇒ a general tutorial. ``download_url`` is
    pre-computed so the UI can render the open-in-new-tab link without
    composing it itself (a single seam means changes to the streaming
    endpoint don't ripple into every page).
    """

    id: int
    tool_alias: str | None
    tool_name: str | None
    """Pretty name resolved from the linked tool row, ``None`` when
    the tutorial isn't tool-scoped. Saves the home page a second
    list-tools call."""

    workflow_alias: str | None
    workflow_name: str | None
    """Pretty name resolved from the linked workflow row, ``None``
    when the tutorial isn't workflow-scoped. Saves the home page a
    second list-workflows call."""

    media_type: Literal["document", "video"]
    version: str
    title: str
    description: str
    original_filename: str
    mime_type: str
    size_bytes: int
    is_active: bool
    lineage_id: int | None
    """Groups versions of the same tutorial (see the ORM). The admin
    list buckets a current version together with its archived previous
    versions by this id."""

    is_archived: bool
    """``True`` when this row is an archived previous version
    (``superseded_at`` set) — kept for history, never shown to users,
    and not activatable."""

    uploaded_by_user_id: int | None
    uploaded_by_label: str | None
    """Pre-resolved ``"Full Name (username)"`` of the uploader, or
    ``None`` if the user account no longer exists / never did."""

    uploaded_at: _dt.datetime
    created_at: _dt.datetime
    download_url: str
    """Path the UI uses to fetch / open the file (no host).
    Currently ``/api/tutorials/{id}/file``."""


class AdminDiagnosticsResponse(_Frozen):
    """GET /api/admin/db/diagnostics response — one card per metric."""

    failed_jobs_7d: AdminDiagnosticsFailedJobs
    queue_depth: AdminDiagnosticsQueueDepth
    recent_users: list[AdminDiagnosticsRecentUser]
    orphaned_working_dirs: AdminDiagnosticsOrphanedDirs


# --- favourites ---------------------------------------------------------


class FavouritesSnapshot(_Frozen):
    """GET /api/me/favourites response (also returned by PUT/DELETE).

    ``enabled`` reflects the site-wide ``favourites_enabled`` general
    setting at the moment of the call; the UI uses it to decide
    whether to draw the star icons at all. ``tools`` and
    ``workflows`` are alphabetical alias lists — empty when the user
    has no favourites yet, never ``None``.
    """

    enabled: bool
    tools: list[str] = Field(default_factory=list)
    workflows: list[str] = Field(default_factory=list)


# --- notifications (admin broadcast) ------------------------------------


class NotificationTemplateUpdate(_Frozen):
    """POST /api/admin/notifications/template body — header/footer text."""

    header_text: str = Field(default="", max_length=20_000)
    footer_text: str = Field(default="", max_length=20_000)


class NotificationTemplateResponse(_Frozen):
    """The persisted broadcast header/footer template.

    ``header_image`` / ``footer_image`` are booleans — whether an
    image is currently saved for that slot. The bytes are fetched
    separately via the per-slot image route so this stays a small
    JSON payload.
    """

    header_text: str
    footer_text: str
    header_image: bool
    footer_image: bool


class BroadcastAudienceOption(_Frozen):
    """One selectable entity for a group-targeted broadcast.

    ``id`` is a string for a uniform multiselect value across the three
    group kinds — a stringified team/role id, or a curator category key.
    """

    id: str
    label: str


class BroadcastAudienceOptions(_Frozen):
    """Dropdown choices for the Send Notifications audience picker.

    Served by ``GET /api/admin/notifications/audience-options`` (gated by
    ``notifications.send``) so the compose page can populate the
    teams / access-rights-groups / curators multiselects without needing
    the ``teams.manage`` / ``users.manage`` permissions those entities'
    own admin endpoints require.
    """

    teams: list[BroadcastAudienceOption] = Field(default_factory=list)
    roles: list[BroadcastAudienceOption] = Field(default_factory=list)
    curators: list[BroadcastAudienceOption] = Field(default_factory=list)


class BroadcastFailure(_Frozen):
    """One recipient the broadcast could not be delivered to."""

    email: str
    reason: str


class BroadcastResponse(_Frozen):
    """POST /api/admin/notifications/broadcast result summary.

    ``disabled`` is True when the email subsystem is off
    (``email.enabled = false`` in config); nothing was attempted and
    the admin should be told to enable email rather than shown a
    misleading "sent 0".
    """

    disabled: bool
    attempted: int
    sent: int
    failed: list[BroadcastFailure] = Field(default_factory=list)


# --- job approvals (§16.15) ----------------------------------------------


class ApprovalListItem(_Frozen):
    """One pending request in the approver inbox table (compact row)."""

    request_id: int
    kind: Literal["pre", "post"]
    target_kind: Literal[
        "job",
        "study",
        "library",
        "material",
        "vv_tool",
        "vv_job",
        "optimisation",
        "surrogate_model",
    ] = "job"
    """What this gate is for. ``library`` / ``material`` / ``vv_*``
    gates have no tool (ADR-0006/0007/0011), so ``tool_alias`` /
    ``tool_name`` are ``None``. ``vv_tool`` is a V&V claim about a tool
    version; ``vv_job`` is a V&V claim about a specific job run."""
    job_id: int | None = None
    """``None`` for a study / library / material gate."""
    study_id: int | None = None
    """Set for a parametric-study gate; ``None`` otherwise."""
    optimisation_study_id: int | None = None
    """Set for an optimisation start / results gate; ``None`` otherwise."""
    job_name: str
    """The target's display name — the job / study / library-file /
    material name as appropriate."""
    tool_alias: str | None = None
    tool_name: str | None = None
    project_name: str
    scenario_name: str
    requestor_name: str
    requested_at: _dt.datetime
    justification: str = ""


class ApprovalInputRow(_Frozen):
    """One input the requestor submitted, with its descriptor metadata."""

    name: str
    label: str
    unit: str | None = None
    value: Any = None
    default: Any = None
    changed_from_default: bool = False


class ApprovalInputGroup(_Frozen):
    """A descriptor input group (or the synthetic 'Other' bucket)."""

    name: str | None = None
    label: str
    rows: list[ApprovalInputRow] = Field(default_factory=list)


class ApprovalLicence(_Frozen):
    """A licence the tool consumes at run time."""

    name: str
    count: int


class ApprovalResultRow(_Frozen):
    """One output the job produced (post-approval review only).

    ``filename`` is set for ``file`` outputs (value is then ``None``)
    so the panel can offer a download; scalar outputs carry ``value``.
    """

    name: str
    label: str
    unit: str | None = None
    value: Any = None
    filename: str | None = None


class ApprovalResultGroup(_Frozen):
    """A descriptor output group (or the synthetic 'Other' bucket)."""

    name: str | None = None
    label: str
    rows: list[ApprovalResultRow] = Field(default_factory=list)


class ApprovalVariable(_Frozen):
    """A design variable / study factor / surrogate-domain bound, resolved.

    One shape reused by the optimisation review (varied factors), the
    surrogate validity domain, and parametric-study factors. Always
    carries the descriptor **label** + unit, never the internal name.
    """

    name: str
    label: str
    unit: str | None = None
    role: Literal["vary", "constant"] = "vary"
    var_type: Literal["continuous", "integer", "categorical"] | None = None
    lower: float | None = None
    """Numeric ``vary`` factor → range lower bound."""
    upper: float | None = None
    """Numeric ``vary`` factor → range upper bound."""
    levels: list[Any] = Field(default_factory=list)
    """Categorical factor → its allowed level set."""
    value: Any = None
    """``constant`` factor → the fixed value."""


class ApprovalObjective(_Frozen):
    """One optimisation objective, resolved to label + unit + sense."""

    name: str
    label: str
    unit: str | None = None
    sense: Literal["minimise", "maximise", "track"]


class ApprovalConstraint(_Frozen):
    """One optimisation constraint, rendered in human terms.

    For an ``output`` constraint the panel reads "``label`` ``op``
    ``value`` ``unit``". ``cross_field`` / ``box`` constraints carry a
    plain-language ``expression`` instead.
    """

    label: str
    unit: str | None = None
    kind: Literal["output", "cross_field", "box"]
    op: str | None = None
    value: float | None = None
    expression: str | None = None


class ApprovalStrategy(_Frozen):
    """The optimisation strategy, in human terms (not the internal code)."""

    algorithm: str
    """Human label, e.g. "Bayesian optimisation" (not ``bo``)."""
    acquisition: str | None = None
    batch_size: int | None = None
    run_mode: str | None = None
    variable_reduction: str | None = None
    """MOP/CoP note when variable reduction is enabled."""


class ApprovalStopping(_Frozen):
    """The run budget + seeding, in plain language."""

    criteria: list[str] = Field(default_factory=list)
    """e.g. ["Max 100 evaluations", "Converge on best-so-far"]."""
    seeding: str | None = None
    """e.g. "10 LHS seed points"."""


class ApprovalFootprint(_Frozen):
    """Best-effort compute blast-radius the approver is authorising.

    ``approximate`` + ``note`` flag when the estimate is soft (a workflow
    target, or no walltime basis for ``est_core_hours``). Core-hours is
    ``None`` rather than a guess when there's no walltime to base it on.
    """

    est_runs_min: int | None = None
    est_runs_max: int | None = None
    cpu_cores_per_run: int | None = None
    ram_mb_per_run: int | None = None
    gpu_memory_mb_per_run: int | None = None
    est_core_hours: float | None = None
    licences: list[ApprovalLicence] = Field(default_factory=list)
    approximate: bool = False
    note: str | None = None


class ApprovalOptimisationReview(_Frozen):
    """The structured optimisation start-gate review block."""

    variables: list[ApprovalVariable] = Field(default_factory=list)
    objectives: list[ApprovalObjective] = Field(default_factory=list)
    constraints: list[ApprovalConstraint] = Field(default_factory=list)
    strategy: ApprovalStrategy | None = None
    stopping: ApprovalStopping | None = None
    footprint: ApprovalFootprint | None = None


class ApprovalSurrogateMetric(_Frozen):
    """One validation metric for a surrogate response (e.g. R² / RMSE)."""

    response_label: str
    unit: str | None = None
    metric: str
    value: float


class ApprovalSurrogateReview(_Frozen):
    """The structured surrogate-model publish review block (D5)."""

    model_kind: str
    """Human fitting-method label, e.g. "Gaussian process"."""
    predicts: list[str] = Field(default_factory=list)
    """Response labels the model predicts."""
    training_samples: int | None = None
    training_source: str | None = None
    """Source study / cohort name the fit was trained on."""
    domain: list[ApprovalVariable] = Field(default_factory=list)
    """Input ranges the model is valid over."""
    metrics: list[ApprovalSurrogateMetric] = Field(default_factory=list)


class ApprovalRunRow(_Frozen):
    """One run in a condensed multi-run post-gate review (§9).

    ``cells`` are the run's varied-input + key-output values keyed by
    column key (see :class:`ApprovalRunColumn`), pre-formatted for
    display. ``failed`` / ``outlier`` drive row flagging.
    """

    job_id: int
    label: str
    state: str
    failed: bool = False
    outlier: bool = False
    cells: dict[str, Any] = Field(default_factory=dict)


class ApprovalRunColumn(_Frozen):
    """One column header in the condensed runs matrix (label + unit)."""

    key: str
    label: str
    unit: str | None = None
    kind: Literal["input", "output"] = "output"


class ApprovalRunAggregate(_Frozen):
    """Per-output min/max/mean over the completed runs."""

    label: str
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None


class ApprovalRunMatrix(_Frozen):
    """Condensed N-run review for a study / optimisation post gate (§9)."""

    columns: list[ApprovalRunColumn] = Field(default_factory=list)
    rows: list[ApprovalRunRow] = Field(default_factory=list)
    aggregates: list[ApprovalRunAggregate] = Field(default_factory=list)
    total: int = 0
    failed: int = 0
    hidden: int = 0
    """Runs from projects the approver can't see — counted, not shown."""


class ApprovalDetail(_Frozen):
    """The rich review panel payload for one pending request.

    ``result_groups`` is populated **only for ``kind="post"``** — the
    approver is deciding whether to release the run's results, so the
    panel surfaces what the job produced. Empty for ``pre`` (the job
    hasn't run yet).

    The structured blocks are target-specific: ``optimisation`` for an
    optimisation gate, ``surrogate`` for a surrogate-publish gate,
    ``factors`` for a parametric-study gate, and ``run_matrix`` for a
    multi-run post gate (study / optimisation).
    """

    request_id: int
    kind: Literal["pre", "post"]
    target_kind: Literal[
        "job",
        "study",
        "library",
        "material",
        "vv_tool",
        "vv_job",
        "optimisation",
        "surrogate_model",
    ] = "job"
    """What this gate is for. ``library`` / ``material`` / ``vv_*``
    gates have no tool (ADR-0006/0007/0011)."""
    status: str
    job_id: int | None = None
    """``None`` for a study / library / material gate."""
    study_id: int | None = None
    """Set for a parametric-study gate; ``None`` otherwise."""
    optimisation_study_id: int | None = None
    """Set for an optimisation start / results gate; ``None`` otherwise."""
    job_name: str
    """The target's display name — job / study / library-file / material."""
    tool_alias: str | None = None
    tool_name: str | None = None
    tool_version: str = ""
    project_name: str
    scenario_name: str
    requestor_name: str
    requested_at: _dt.datetime
    justification: str = ""
    cpu_cores: int
    ram_mb: int
    gpu_memory_mb: int
    licences: list[ApprovalLicence] = Field(default_factory=list)
    input_groups: list[ApprovalInputGroup] = Field(default_factory=list)
    result_groups: list[ApprovalResultGroup] = Field(default_factory=list)
    optimisation: ApprovalOptimisationReview | None = None
    surrogate: ApprovalSurrogateReview | None = None
    factors: list[ApprovalVariable] = Field(default_factory=list)
    run_matrix: ApprovalRunMatrix | None = None


class ApprovalDecisionItem(_Frozen):
    """One row of the batch Submit-decisions payload.

    ``reason`` is mandatory for a ``deny`` (the requestor is told why);
    optional on an ``approve``. The service enforces the deny-reason
    rule — surfaced per-item so one missing reason doesn't sink the
    whole batch.
    """

    request_id: int
    decision: Literal["approve", "deny"]
    reason: str | None = None


class ApprovalDecisionsRequest(_Frozen):
    """``POST /api/approvals/decisions`` body — the whole batch."""

    decisions: list[ApprovalDecisionItem] = Field(min_length=1)


class ApprovalDecisionResult(_Frozen):
    """Per-item outcome. ``ok=False`` carries a plain-English ``error``.

    Each decision is applied in its own savepoint so an
    already-decided / not-permitted / missing-reason item fails in
    isolation and the rest of the batch still lands.
    """

    request_id: int
    ok: bool
    status: str | None = None
    job_id: int | None = None
    job_state: str | None = None
    error: str | None = None


class ApprovalDecisionsResponse(_Frozen):
    """Batch result — one entry per submitted decision, input order."""

    results: list[ApprovalDecisionResult] = Field(default_factory=list)


# --- usage statistics (usage.view) --------------------------------------


class UsageKpi(_Frozen):
    """One headline figure with its period-over-period comparison.

    ``value`` is ``None`` when the figure is undefined for the window
    (e.g. a success rate with no terminal jobs); ``delta_pct`` is
    ``None`` when no comparison can be made (no prior value, or a prior
    value of zero).
    """

    value: float | None = None
    previous: float | None = None
    delta_pct: float | None = None


class UsageStatePoint(_Frozen):
    """One (day, job-state) bucket of the stacked time series."""

    date: _dt.date
    state: str
    count: int


class UsageToolCount(_Frozen):
    """A tool's job count for the Overview leaderboard."""

    tool_alias: str
    tool_name: str
    count: int


class UsageOverviewResponse(_Frozen):
    """GET /api/admin/usage/overview — everything the Overview tab draws.

    ``since`` / ``until`` echo the resolved window so the UI can label
    the period and the deltas. ``by_state`` is the window's job-count
    breakdown; ``series`` is the per-day stacked data; ``top_tools`` is
    the leaderboard.
    """

    since: _dt.datetime
    until: _dt.datetime
    jobs_total: UsageKpi
    active_users: UsageKpi
    success_rate: UsageKpi
    core_hours: UsageKpi
    gpu_hours: UsageKpi
    registered_users: int
    by_state: dict[str, int] = Field(default_factory=dict)
    series: list[UsageStatePoint] = Field(default_factory=list)
    top_tools: list[UsageToolCount] = Field(default_factory=list)


class UsageFilterTool(_Frozen):
    """A selectable tool in the Usage Statistics filter bar."""

    alias: str
    name: str


class UsageFilterTeam(_Frozen):
    """A selectable team in the Usage Statistics filter bar."""

    id: int
    name: str


class UsageFiltersResponse(_Frozen):
    """GET /api/admin/usage/filters — the filter-bar dropdown options."""

    tools: list[UsageFilterTool] = Field(default_factory=list)
    teams: list[UsageFilterTeam] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    execution_targets: list[str] = Field(default_factory=list)


class UsageToolConsumption(_Frozen):
    """Compute consumed by one tool over the window."""

    tool_alias: str
    tool_name: str
    core_hours: float
    gpu_hours: float
    jobs: int


class UsageTeamConsumption(_Frozen):
    """Compute consumed by one team (``team_id`` ``None`` ⇒ "(No team)")."""

    team_id: int | None
    team_name: str
    core_hours: float
    gpu_hours: float
    jobs: int


class UsageRuntimeDist(_Frozen):
    """Per-tool wall-clock runtime distribution, in hours."""

    tool_alias: str
    tool_name: str
    p50_hours: float
    p95_hours: float
    max_hours: float
    runs: int


class UsageDayValue(_Frozen):
    """A single (day, value) point — the queue-wait trend uses hours."""

    date: _dt.date
    value: float


class UsageThroughputPoint(_Frozen):
    """Jobs submitted vs completed on one day."""

    date: _dt.date
    submitted: int
    completed: int


class UsageComputeResponse(_Frozen):
    """GET /api/admin/usage/compute — the Compute & Throughput tab payload."""

    since: _dt.datetime
    until: _dt.datetime
    by_tool: list[UsageToolConsumption] = Field(default_factory=list)
    by_team: list[UsageTeamConsumption] = Field(default_factory=list)
    runtime: list[UsageRuntimeDist] = Field(default_factory=list)
    queue_wait: list[UsageDayValue] = Field(default_factory=list)
    throughput: list[UsageThroughputPoint] = Field(default_factory=list)


class UsageFailingTool(_Frozen):
    """A tool's failure record over the window."""

    tool_alias: str
    tool_name: str
    failures: int
    failure_rate: float
    last_failed_at: _dt.datetime | None = None


class UsageExitCodeCount(_Frozen):
    """How many jobs ended with a given process exit code."""

    exit_code: int
    count: int


class UsageApprovalKindFunnel(_Frozen):
    """Approval-request outcomes for one gate kind (``pre`` / ``post``)."""

    kind: str
    requested: int
    approved: int
    denied: int
    pending: int


class UsageReliabilityResponse(_Frozen):
    """GET /api/admin/usage/reliability — the Reliability tab payload."""

    since: _dt.datetime
    until: _dt.datetime
    success_rate: UsageKpi
    success_trend: list[UsageDayValue] = Field(default_factory=list)
    top_failing: list[UsageFailingTool] = Field(default_factory=list)
    exit_codes: list[UsageExitCodeCount] = Field(default_factory=list)
    approval_funnel: list[UsageApprovalKindFunnel] = Field(default_factory=list)


class UsageNamedCount(_Frozen):
    """A display label and its job count (People-tab breakdowns)."""

    name: str
    count: int


class UsagePeopleResponse(_Frozen):
    """GET /api/admin/usage/people — the People & Adoption tab payload."""

    since: _dt.datetime
    until: _dt.datetime
    active_users: UsageKpi
    new_users: UsageKpi
    peak_concurrency: int
    registered_users: int
    active_trend: list[UsageDayValue] = Field(default_factory=list)
    by_user: list[UsageNamedCount] = Field(default_factory=list)
    by_team: list[UsageNamedCount] = Field(default_factory=list)
    by_role: list[UsageNamedCount] = Field(default_factory=list)


class UsageLogonEventPoint(_Frozen):
    """Logon-access grants vs revocations on one day."""

    date: _dt.date
    granted: int
    revoked: int


class UsageSeatsResponse(_Frozen):
    """GET /api/admin/usage/seats — the Seats & Licence tab payload.

    ``cap`` is the licensed-seat entitlement, ``None`` until the signed-
    licence subsystem lands (Phase 2); the UI renders "—" and a note.
    """

    since: _dt.datetime
    until: _dt.datetime
    registered_users: int
    enabled_seats: int
    peak_concurrency: int
    cap: int | None = None
    licence_type: str | None = None
    """``csl`` / ``fal`` — decides which figure the cap governs:
    peak concurrency for CSL, enabled seats for FAL. Showing a cap
    against the wrong line would be quietly misleading."""

    licence_state: str | None = None
    logon_events: list[UsageLogonEventPoint] = Field(default_factory=list)
    concurrency_trend: list[UsageDayValue] = Field(default_factory=list)


class UsageTargetUsage(_Frozen):
    """Jobs + compute on one execution target (local / aos / hpc)."""

    target: str
    jobs: int
    core_hours: float
    gpu_hours: float


class UsageInfraResponse(_Frozen):
    """GET /api/admin/usage/infra — the Infrastructure tab payload."""

    since: _dt.datetime
    until: _dt.datetime
    by_target: list[UsageTargetUsage] = Field(default_factory=list)
    cpu_jobs: int = 0
    gpu_jobs: int = 0
    cpu_core_hours: float = 0.0
    gpu_core_hours: float = 0.0


class UsageDigestSendRequest(_Frozen):
    """POST /api/admin/usage/digest/send body.

    ``recipients`` are explicit email addresses; ``tabs`` chooses which
    tabs the emailed report includes (empty ⇒ all). The report window +
    narrowings come from the shared filter query params.
    """

    recipients: list[str] = Field(min_length=1, max_length=200)
    tabs: list[str] = Field(default_factory=list)


class UsageDigestFailure(_Frozen):
    """One recipient the digest could not be delivered to."""

    email: str
    reason: str


class UsageDigestResponse(_Frozen):
    """POST /api/admin/usage/digest/send result summary.

    ``disabled`` is True when the email subsystem is off
    (``email.enabled = false``); nothing was attempted.
    """

    disabled: bool
    attempted: int
    sent: int
    failed: list[UsageDigestFailure] = Field(default_factory=list)


class ApprovalSummary(_Frozen):
    """Powers the home card + the chrome nav gate.

    ``can_approve`` mirrors legacy parity — the card/link shows
    whenever the user carries the approver flag, even with zero
    pending (then it reads "No pending approvals").
    """

    can_approve: bool
    pending: int


# --- parametric studies (ADR-0001 / designs/parametric-studies.md) -------


class StudyCreateRequest(_Frozen):
    """``POST /api/projects/{project_id}/studies`` body.

    ``factor_spec`` / ``sampling_params`` may be partial (a draft is
    edited incrementally); full validation happens at submit.
    """

    name: str = Field(min_length=1, max_length=128)
    tool_alias: str = Field(min_length=1, max_length=64)
    mode: str = Field(min_length=1, max_length=20)
    description: str = Field(default="", max_length=2000)
    factor_spec: dict[str, Any] = Field(default_factory=dict)
    sampling_params: dict[str, Any] = Field(default_factory=dict)
    seed: int | None = None
    run_cap: int | None = Field(default=None, ge=1)


class StudyResponse(_Frozen):
    """A parametric study as seen over the wire."""

    id: int
    project_id: int
    project_name: str = ""
    """Display name of the owning project; '' if unresolved (read paths)."""
    name: str
    description: str
    tool_alias: str
    tool_name: str = ""
    """Display label of the tool ([[feedback_use_labels_not_names]]); ''
    if unresolved. Use this over ``tool_alias`` in the UI."""
    tool_version_id: int
    """The pinned tool version — shown in the study header + exports."""
    tool_version: str = ""
    """The pinned version *string* (e.g. ``"1.2.0"``); '' if unresolved.
    Prefer this over the meaningless ``tool_version_id`` in the UI."""
    created_by_name: str = ""
    """Display name of the creator (``full_name``/username); '' if
    unresolved (read paths)."""
    mode: str
    state: str
    """Stored lifecycle: ``draft`` / ``pending_approval`` / ``running`` /
    ``complete`` / ``partial`` / ``failed`` / ``cancelled`` / ``rejected``."""
    factor_spec: dict[str, Any] = Field(default_factory=dict)
    sampling_params: dict[str, Any] = Field(default_factory=dict)
    run_cap: int
    generated_count: int
    skipped_invalid_count: int
    seed: int | None
    created_by_user_id: int
    created_at: _dt.datetime
    updated_at: _dt.datetime
    state_changed_at: _dt.datetime | None
    cancel_requested_at: _dt.datetime | None


class StudyChildJob(_Frozen):
    """One study child job, as exposed inside a study-detail response.

    Study children never surface in My Jobs (design §9); this is the
    only wire shape they have.
    """

    id: int
    name: str
    tool_alias: str
    state: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    created_at: _dt.datetime
    started_at: _dt.datetime | None
    completed_at: _dt.datetime | None
    exit_code: int | None
    cancel_requested_at: _dt.datetime | None


class StudyDetailResponse(_Frozen):
    """Full study + its child jobs for the study-detail page.

    ``input_labels`` / ``input_units`` come from the *pinned* descriptor
    (the same source the Results tab uses) so the Progress list can show
    descriptor labels rather than raw input names
    ([[feedback_use_labels_not_names]]).

    ``input_groups`` / ``input_group_of`` carry the pinned descriptor's input
    grouping so the Study-settings summary can lay the factors out under the
    same section headings the tool's job form (and the study setup) use.
    ``input_groups`` is each group's ``{name, label, column, row}``;
    ``input_group_of`` maps an input name to its group name (ungrouped inputs
    are simply absent).
    """

    study: StudyResponse
    children: list[StudyChildJob]
    input_labels: dict[str, str] = Field(default_factory=dict)
    input_units: dict[str, str] = Field(default_factory=dict)
    input_groups: list[dict[str, Any]] = Field(default_factory=list)
    input_group_of: dict[str, str] = Field(default_factory=dict)


class FactorSpecPreviewRequest(_Frozen):
    """``POST /api/studies/preview`` body — the live pre-submit count.

    Design §9 step 4/5. Pure: no study row is created.
    """

    tool_alias: str = Field(min_length=1, max_length=64)
    mode: str = Field(min_length=1, max_length=20)
    factor_spec: dict[str, Any] = Field(default_factory=dict)
    sampling_params: dict[str, Any] = Field(default_factory=dict)
    seed: int | None = None
    run_cap: int | None = Field(default=None, ge=1)
    """The form's effective cap; the matrix is returned only when the
    design fits within it (design §9 step 5). ``None`` → 200 default."""


class FactorSpecPreviewResponse(_Frozen):
    """What a study *would* generate, before it's created/submitted."""

    generated_count: int
    """Points that would run (passed cross-field validation)."""
    skipped_invalid_count: int
    """Points dropped by the descriptor's cross-field constraints."""
    raw_count: int
    """Total the mode produced before filtering."""
    skip_reasons: dict[str, int]
    """``{plain_english_reason: count}`` for the confirmation copy."""
    level_warnings: list[str] = Field(default_factory=list)
    """Plain-English level warnings (e.g. integer grid de-duplication)."""
    run_cap: int
    """The cap the preview was evaluated against."""
    over_cap: bool
    """True when ``generated_count`` exceeds ``run_cap`` — the modal
    then shows the refusal + count instead of the matrix (design §9.5)."""
    points: list[dict[str, Any]] = Field(default_factory=list)
    """The full run matrix to render in the Preview modal. Empty when
    ``over_cap`` (no points are built past the cap)."""


class AvailableModesResponse(_Frozen):
    """Which sampling modes the chosen tool's input types can support.

    ``modes[m]`` is ``None`` when offerable, else a plain-English
    reason the selector greys it (design §9 step 3, two-layer rule).
    """

    modes: dict[str, str | None]


# --- parametric study results (2-PS-b0 / ADR-0001 §10.0) -----------------


class ResponseSourceScalar(_Frozen):
    """A scalar output the study can pick as a response axis.

    Numeric scalars are eligible as a numeric response/plot axis;
    string scalars are usable as filters / labels only. The UI's
    response-source picker offers all of these; only ``number``
    scalars are selectable for the plot axis.
    """

    name: str
    label: str
    unit: str | None = None
    type: str = Field(min_length=1)
    """``"number"`` or ``"string"`` — mirrors the descriptor's
    ``OutputField.type``."""


class ResponseSourceCsvPlot(_Frozen):
    """A descriptor-declared ``csv_plot`` file output.

    The response-from-column picker (b0) lets the user choose a
    column + aggregate to use as the scalar response; the
    curve-overlay surface (2-PS-b) plots the whole series across
    children. Column roles are picked at view time — the optional
    ``xaxis_column`` / ``yaxis_column`` are descriptor *hints* the
    UI uses as defaults.
    """

    name: str
    label: str
    filename: str
    xaxis_column: int | None = None
    yaxis_column: int | None = None
    plot_title: str | None = None
    xaxis_title: str | None = None
    yaxis_title: str | None = None


class ResponseSourcesDescriptor(_Frozen):
    """Available response sources for one study.

    Derived from the *pinned* tool descriptor (§5) — never changes
    over the study's life.
    """

    scalars: list[ResponseSourceScalar] = Field(default_factory=list)
    csv_plots: list[ResponseSourceCsvPlot] = Field(default_factory=list)


class StudyChildResultEntry(_Frozen):
    """One child's row in the results matrix.

    ``scalars`` is empty for failed / cancelled children (D3 partial
    -data banner; never silently dropped) and for children that
    pre-date the store / haven't completed yet. ``retained_files``
    references file outputs that survived cleanup (ADR-0003 floor +
    descriptor ``retain_files``); each entry is
    ``{"name": ..., "filename": ..., "kind": ...}``.
    """

    job_id: int
    state: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    scalars: dict[str, Any] = Field(default_factory=dict)
    retained_files: list[dict[str, Any]] = Field(default_factory=list)
    validity: str = "valid"
    """ADR-0017 output-validity verdict: ``valid`` | ``invalid``."""
    validity_reasons: list[dict[str, Any]] = Field(default_factory=list)
    """Why the row is invalid (``[{output, code, detail}]``); empty when valid."""


class StudyResultsResponse(_Frozen):
    """``GET /api/studies/{study_id}/results`` — the matrix + response sources."""

    study_id: int
    tool_alias: str
    tool_version_id: int
    """The descriptor pinned at study creation (§5) — clients may
    cross-reference the change-log to label the response sources."""

    response_sources: ResponseSourcesDescriptor
    input_labels: dict[str, str] = Field(default_factory=dict)
    """Input name → display label (from the pinned descriptor; falls back to
    the name). The UI shows labels everywhere, never raw input names."""
    input_units: dict[str, str | None] = Field(default_factory=dict)
    """Input name → unit (or ``None``) from the pinned descriptor."""
    input_options: dict[str, list[str]] = Field(default_factory=dict)
    """Input name → its declared options, for inputs that are a *choice*.

    Only fields that enumerate options appear. It lets the Explore plots put
    a swept ``select`` input on an axis as one named column per option — and
    show an option **nobody ran** as an empty column, which is the same gap
    the coverage panels exist to surface.
    """
    input_groups: dict[str, str] = Field(default_factory=dict)
    """Input name → form group name (descriptor ``InputField.group``; ``""``
    when ungrouped). Lets the UI group columns like the job form does."""
    output_groups: dict[str, str] = Field(default_factory=dict)
    """Output name → results group name (descriptor ``OutputField.group``)."""
    input_group_labels: dict[str, str] = Field(default_factory=dict)
    """Input group name → display label (descriptor ``InputGroup.label``)."""
    output_group_labels: dict[str, str] = Field(default_factory=dict)
    """Output group name → display label (descriptor ``OutputGroup.label``)."""
    output_bounds: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    """Numeric output name → ``{"min", "max"}`` physical bounds (ADR-0017), for
    outputs that declare one. The surrogate profiler clamps predictions to these
    so it never reports a physically-impossible value (e.g. a negative failure
    index). Absent for outputs with no declared bound."""
    children: list[StudyChildResultEntry] = Field(default_factory=list)


# --- surrogate Model registry (ADR-0008 §5 / §11.2) ----------------------


class ModelCreateRequest(_Frozen):
    """``POST /api/models`` body — fit + persist a new surrogate model.

    The model is fitted from a study source (``source_kind`` /
    ``source_id``) over the named scalar ``response_names``. ``scope``
    ``project`` needs ``project_id``; ``global`` needs the Model Curator
    capability (resolved server-side from ``models.curate``).
    """

    name: str = Field(min_length=1, max_length=255)
    tool_alias: str = Field(min_length=1, max_length=64)
    scope: Literal["personal", "project", "global"] = "personal"
    source_kind: Literal["parametric", "optimisation", "cohort"]
    source_id: int
    response_names: list[str] = Field(min_length=1)
    project_id: int | None = None
    surrogate_kind: Literal["gp", "gp_matern", "poly", "gbm"] = "gp"
    description: str = Field(default="", max_length=2000)
    tags: list[str] = Field(default_factory=list)
    keep_training_data: bool = False
    field_auto_select: bool = False
    """Opt-in (ADR-0018): pick the best regressor **per POD mode** for each
    field output by cross-validated Q², instead of using ``surrogate_kind``.
    Superseded by ``field_surrogate_kind='auto'``; kept for back-compat."""
    field_surrogate_kind: Literal["gp", "gp_matern", "poly", "gbm", "auto"] | None = None
    """Field outputs' per-mode regressor (ADR-0018), independent of the scalar
    ``surrogate_kind`` so the two can differ. ``'auto'`` picks the best per mode.
    ``None`` falls back to ``field_auto_select`` then ``surrogate_kind``."""
    output_filter: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    """On-the-fly run filter: fit only on runs whose named scalar outputs fall in
    ``{output: {min, max}}``. Excludes outlier runs that skew the fit **without**
    changing the tool's descriptor validity bounds (applies to scalar + field)."""


class ModelUpdateRequest(_Frozen):
    """``PATCH /api/models/{model_id}`` body — mutable entry metadata."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    tags: list[str] | None = None


class ModelScopeRequest(_Frozen):
    """``POST /api/models/{model_id}/scope`` body (§6.4 — personal↔project)."""

    new_scope: Literal["personal", "project", "global"]
    project_id: int | None = None


class ModelRefitRequest(_Frozen):
    """``POST /api/models/{model_id}/refit`` body."""

    surrogate_kind: Literal["gp", "poly"] = "gp"


class ModelExportRequest(_Frozen):
    """``POST /api/models/{model_id}/export-as-tool`` body (§7)."""

    block_extrapolation: bool = False
    with_std: bool = True


class ModelSubmitReviewRequest(_Frozen):
    """``POST /api/models/{mid}/versions/{vid}/submit-review`` body (§6.3 gate 1).

    ``checker_user_id`` nominates the technical checker (a Model Curator,
    ≠ builder). Required when ``models_global_publish_approval`` is on;
    omitted when it's off (the builder, who must be a Model Curator,
    checks directly).
    """

    checker_user_id: int | None = None


class ModelVersionResponse(_Frozen):
    """One immutable surrogate model version over the wire (§5.3)."""

    id: int
    ordinal: int
    kind: str
    factors: list[dict[str, Any]] = Field(default_factory=list)
    responses: list[dict[str, Any]] = Field(default_factory=list)
    source_kind: str
    source_id: int
    n_training_points: int
    quality: dict[str, Any] = Field(default_factory=dict)
    publish_state: str
    surrogate_method: str | None = None
    fitted_at: _dt.datetime
    fitted_by_user_id: int


class SurrogateExclusionSummary(_Frozen):
    """Runs left out of a fit for output-validity reasons (ADR-0017).

    ``reason_counts`` maps a reason code (``non_finite`` / ``below_min`` /
    ``above_max`` / ``field_non_finite`` / ``field_out_of_bounds``) to how many
    runs it excluded — so the UI can show "K excluded (J non-finite, …)".
    """

    n_excluded: int = 0
    reason_counts: dict[str, int] = Field(default_factory=dict)


class ModelResponse(_Frozen):
    """A surrogate model entry over the wire (§5.2)."""

    id: int
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    tool_alias: str
    tool_name: str = ""
    """Tool display label ([[feedback_use_labels_not_names]]); '' if
    unresolved. Use over ``tool_alias`` in the UI."""
    scope: str
    project_id: int | None = None
    project_name: str = ""
    """Owning project's display name (project-scoped models); '' otherwise."""
    owner_user_id: int
    state: str
    published_version_id: int | None = None
    created_by_user_id: int
    created_by_name: str = ""
    """Creator's display name (full_name/username); '' if unresolved."""
    created_at: _dt.datetime
    version: int
    excluded: SurrogateExclusionSummary | None = None
    """Set only on the create response: runs excluded from this fit for
    output-validity reasons (ADR-0017). ``None`` on list / detail rows."""


class ModelDetailResponse(_Frozen):
    """``GET /api/models/{model_id}`` — the model + its version history."""

    model: ModelResponse
    versions: list[ModelVersionResponse] = Field(default_factory=list)


class ModelExportResponse(_Frozen):
    """``POST .../export-as-tool`` — the synthesised tool's identity."""

    tool_alias: str
    tool_state: str
    version_id: int


class ModelApproverOption(_Frozen):
    """``GET /api/models/approvers`` — one nominatable checker (§6.3 gate 1)."""

    user_id: int
    username: str
    full_name: str = ""


class ModelCompareRequest(_Frozen):
    """``POST /api/models/compare-fits`` body — fit several methods, persist none.

    A pre-commit bake-off: fits each method in ``methods`` over the same
    source + responses and returns their cross-validated quality + held-out
    pairs so the UI can overlay parity plots and highlight the best. The
    caller then creates the model with the chosen single method.
    """

    tool_alias: str = Field(min_length=1, max_length=64)
    source_kind: Literal["parametric", "optimisation", "cohort"]
    source_id: int
    response_names: list[str] = Field(default_factory=list)
    methods: list[Literal["gp", "gp_matern", "poly", "gbm"]] = Field(default_factory=list)
    field_response_names: list[str] = Field(default_factory=list)
    """3-D field outputs to bake off (ADR-0018). A field has its own methods
    (``field_methods``) and is ranked on Field Q², separate from the scalars."""
    field_methods: list[Literal["gp", "gp_matern", "poly", "gbm", "auto"]] = Field(
        default_factory=list
    )
    """Per-mode regressors to compare for the fields; ``'auto'`` picks the best
    regressor per POD mode."""
    output_filter: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    """On-the-fly run filter (same as ModelCreateRequest): compare only on runs
    whose named scalar outputs fall in ``{output: {min, max}}``."""
    project_id: int | None = None

    @model_validator(mode="after")
    def _at_least_one_bakeoff(self) -> ModelCompareRequest:
        """Need a scalar bake-off or a field one (either side may be empty, not both)."""
        scalar = bool(self.response_names and self.methods)
        field = bool(self.field_response_names and self.field_methods)
        if not scalar and not field:
            raise ValueError(
                "Pick at least one response with a method to compare "
                "(scalar responses + methods, or field responses + field methods)."
            )
        return self


class ModelMethodFit(_Frozen):
    """One method's result in a compare-fits bake-off."""

    method: str
    fitted: bool
    message: str | None = None
    mean_q2: float | None = None
    quality: dict[str, Any] = Field(default_factory=dict)
    """``{response: {q2, rmse_cv, n, cv_actual, cv_predicted}}`` (empty when unfitted)."""


class ModelFieldMethodFit(_Frozen):
    """One field method's result in a compare-fits bake-off (ADR-0018)."""

    method: str
    fitted: bool
    message: str | None = None
    mean_field_q2: float | None = None
    responses: dict[str, Any] = Field(default_factory=dict)
    """``{field: {field_q2, recon_rmse, modes_retained, mode_energy, mode_q2,
    mode_energy_spectrum, cv_field_actual, cv_field_predicted, n}}``."""


class ModelCompareResponse(_Frozen):
    """``POST /api/models/compare-fits`` — per-method quality + the best picks."""

    responses: list[str] = Field(default_factory=list)
    methods: list[ModelMethodFit] = Field(default_factory=list)
    best_overall: str | None = None
    best_per_response: dict[str, str] = Field(default_factory=dict)
    field_responses: list[str] = Field(default_factory=list)
    field_methods: list[ModelFieldMethodFit] = Field(default_factory=list)
    best_field_overall: str | None = None
    excluded: SurrogateExclusionSummary = Field(default_factory=SurrogateExclusionSummary)
    """Runs left out of every method's fit for output-validity reasons (ADR-0017)."""


class ModelCoverageFactor(_Frozen):
    """One factor's frozen training range for the in-form coverage gauge (§7.3)."""

    name: str
    min: float | None = None
    max: float | None = None
    unit: str | None = None


class ModelCoverageResponse(_Frozen):
    """``GET /api/models/versions/{version_id}/coverage`` — the gauge's factor ranges."""

    version_id: int
    factors: list[ModelCoverageFactor] = Field(default_factory=list)


class FieldPredictRequest(_Frozen):
    """``POST /api/models/{id}/versions/{vid}/field-predict`` body (ADR-0008 §4.5).

    ``point`` is the ``{factor: value}`` the predicted-field viewer is
    asking for — design factors plus ``time`` for a time-resolved surrogate.
    ``response_name`` picks which field of a ``combined`` model to predict
    (ADR-0018); ``None`` uses the version's first/only field.

    A value is a number **or a string**: a varied categorical (a material, a
    solver choice) is a real factor the model is fitted on, and it is named by
    its level. A number-only ``point`` couldn't carry one at all, so the viewer
    had no way to ask for the Magnesium version of a design.
    """

    point: dict[str, float | str] = Field(default_factory=dict)
    response_name: str | None = None


class PredictSupportResponse(_Frozen):
    """How far one prediction sits from the runs the model learned from (§8).

    ``sigma`` is **numeric on purpose**: ADR-0017 output validity operates on
    numbers, never on status strings, so only a number can carry a declared
    maximum. ``available_statuses`` says which verdicts this fit can actually
    produce — a response with no posterior variance never returns ``edge``,
    and saying so beats leaving a caller to wonder why it never appears.
    """

    status: Literal["inside", "edge", "outside"]
    reason: str = ""
    sigma: float | None = None
    available_statuses: list[str] = Field(default_factory=list)


class FieldPredictResponse(_Frozen):
    """A reconstructed nodal field at one input point (the viewer's frame)."""

    version_id: int
    n_nodes: int
    field: list[float] = Field(default_factory=list)
    field_std: list[float] | None = None
    mode_coefficients: list[float] = Field(default_factory=list)
    has_geometry: bool = False
    """Whether the bundle carries a reference mesh the viewer can colour
    (``.npy`` fields have none — the viewer falls back to the scree panels)."""

    support: PredictSupportResponse | None = None
    """The ADR-0020 §8 support verdict at this point.

    ``None`` for a version fitted before coverage existed. The viewer renders
    that as **no badge at all** — an empty badge invites the reader to assume
    the blank means fine, and silence about support is not a clean bill of
    health."""


class FieldGeometryResponse(_Frozen):
    """``GET /api/models/{id}/versions/{vid}/field-geometry`` — the viewer mesh.

    ``points`` are full node coordinates (a per-node field aligns by index);
    ``i``/``j``/``k`` index the surface triangles. ``has_geometry=False``
    (everything else empty) for a ``.npy`` field — the viewer then draws the
    value-vs-node line instead of a coloured 3-D surface.
    """

    version_id: int
    has_geometry: bool = False
    points: list[list[float]] = Field(default_factory=list)
    i: list[int] = Field(default_factory=list)
    j: list[int] = Field(default_factory=list)
    k: list[int] = Field(default_factory=list)


# --- job cohorts (ADR-0008 §3 / §11.1) -----------------------------------


class CohortCreateRequest(_Frozen):
    """``POST /api/cohorts`` body — a workspace-level Job Cohort.

    ``tolerated_tool_version_ids`` must be non-empty (the service
    enforces it + that every version belongs to ``tool_alias``); omit it
    to anchor on the tool's latest version. ``auto_grow`` (default) keeps
    membership live; set it ``False`` and pass ``member_job_ids`` to
    freeze an explicit, curated set. Every filter is optional
    (omitted = no filter); ``project_ids`` empty/omitted = whole workspace.
    """

    name: str = Field(min_length=1, max_length=128)
    tool_alias: str = Field(min_length=1, max_length=64)
    tolerated_tool_version_ids: list[int] = Field(default_factory=list)
    """Omit / empty ⇒ the service anchors on the tool's latest version
    (§3.2). Ignored when ``tolerated_version_bands`` is given."""
    tolerated_version_bands: list[str] | None = None
    """``major.minor`` bands to tolerate (e.g. ``["1.2"]``); patches and
    future patches within a band are included (§3.2)."""
    description: str = Field(default="", max_length=2000)
    auto_grow: bool = True
    """``True`` ⇒ live saved query; ``False`` ⇒ frozen ``member_job_ids``."""
    member_job_ids: list[int] | None = None
    """The picked runs for a fixed (``auto_grow=False``) cohort."""
    excluded_job_ids: list[int] | None = None
    """Runs to permanently drop from a live cohort's matches."""
    project_ids: list[int] | None = None
    scenario_ids: list[int] | None = None
    owner_user_ids: list[int] | None = None
    source_kinds: list[str] | None = None
    """``scenario`` / ``workflow`` / ``parametric_study`` / ``optimisation_study``."""
    parametric_study_ids: list[int] | None = None
    optimisation_study_ids: list[int] | None = None
    completed_after: _dt.datetime | None = None
    completed_before: _dt.datetime | None = None
    library_file_version_ids: list[int] | None = None
    material_version_ids: list[int] | None = None
    input_ranges: dict[str, Any] | None = None
    cross_field_predicate: str | None = Field(default=None, max_length=2000)


class CohortUpdateRequest(_Frozen):
    """``PATCH /api/cohorts/{cohort_id}`` body — every field optional.

    A field left ``None`` is unchanged; pass an explicit value to edit
    it. Editing is refused on an archived cohort (the service guards it).
    """

    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    tolerated_tool_version_ids: list[int] | None = Field(default=None, min_length=1)
    tolerated_version_bands: list[str] | None = None
    auto_grow: bool | None = None
    member_job_ids: list[int] | None = None
    excluded_job_ids: list[int] | None = None
    project_ids: list[int] | None = None
    scenario_ids: list[int] | None = None
    owner_user_ids: list[int] | None = None
    source_kinds: list[str] | None = None
    """``scenario`` / ``workflow`` / ``parametric_study`` / ``optimisation_study``."""
    parametric_study_ids: list[int] | None = None
    optimisation_study_ids: list[int] | None = None
    completed_after: _dt.datetime | None = None
    completed_before: _dt.datetime | None = None
    library_file_version_ids: list[int] | None = None
    material_version_ids: list[int] | None = None
    input_ranges: dict[str, Any] | None = None
    cross_field_predicate: str | None = Field(default=None, max_length=2000)
    auto_include_study_ids: list[int] | None = None
    """Studies this cohort commissioned to fill its own gaps (ADR-0020 §11.5).

    Their completed runs join the membership whichever mode the cohort is in.
    Exposed rather than kept internal: a cohort whose membership has a source
    the page never mentions is the same silent-drop problem coverage exists to
    report, one level up."""


class CohortPreviewRequest(_Frozen):
    """``POST /api/cohorts/preview`` body — try a query without saving.

    Same shape as the filter half of a create request. Returns the
    matching completed runs across the workspace (ACL-intersected) so the
    builder can show them for selection.
    """

    tool_alias: str = Field(min_length=1, max_length=64)
    tolerated_tool_version_ids: list[int] = Field(default_factory=list)
    tolerated_version_bands: list[str] | None = None
    project_ids: list[int] | None = None
    scenario_ids: list[int] | None = None
    owner_user_ids: list[int] | None = None
    source_kinds: list[str] | None = None
    """``scenario`` / ``workflow`` / ``parametric_study`` / ``optimisation_study``."""
    parametric_study_ids: list[int] | None = None
    optimisation_study_ids: list[int] | None = None
    completed_after: _dt.datetime | None = None
    completed_before: _dt.datetime | None = None
    library_file_version_ids: list[int] | None = None
    material_version_ids: list[int] | None = None
    input_ranges: dict[str, Any] | None = None
    cross_field_predicate: str | None = Field(default=None, max_length=2000)


class CohortCandidate(_Frozen):
    """One candidate run in a cohort preview."""

    job_id: int
    project_id: int
    source: str
    """``scenario`` / ``workflow`` / ``parametric_study`` / ``optimisation_study``."""
    source_name: str | None = None
    """Which study the run came from; ``None`` for a standalone run."""
    created_by_user_id: int | None = None
    """Who launched the run."""
    tool_version_id: int
    completed_at: _dt.datetime | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)


class CohortPreviewResponse(_Frozen):
    """``POST /api/cohorts/preview`` result."""

    rows: list[CohortCandidate] = Field(default_factory=list)
    acl_hidden: int = 0


class CohortVersionBand(_Frozen):
    """A ``major.minor`` version band the builder offers (patches ignored)."""

    major_minor: str
    latest_version: str
    version_ids: list[int] = Field(default_factory=list)


class CohortNumericInput(_Frozen):
    """A numeric tool input the builder can offer a min/max filter on."""

    name: str
    label: str


class CohortFacet(_Frozen):
    """One selectable value for a builder filter — an id and what to call it."""

    id: int
    name: str


class CohortBuildMetaResponse(_Frozen):
    """``GET /api/cohorts/tools/{alias}/build-meta`` — builder helper data."""

    version_bands: list[CohortVersionBand] = Field(default_factory=list)
    numeric_inputs: list[CohortNumericInput] = Field(default_factory=list)
    launchers: list[CohortFacet] = Field(default_factory=list)
    """Users who actually launched a visible run of this tool."""
    parametric_studies: list[CohortFacet] = Field(default_factory=list)
    optimisation_studies: list[CohortFacet] = Field(default_factory=list)


class CohortResponse(_Frozen):
    """A job cohort as seen over the wire."""

    id: int
    name: str
    description: str
    tool_alias: str
    tolerated_tool_version_ids: list[int] = Field(default_factory=list)
    tolerated_versions: list[str] = Field(default_factory=list)
    """The actual version strings (e.g. ``["1.0.0", "1.2.0"]``) the
    ``tolerated_tool_version_ids`` resolve to, newest first — for display.
    Enriched on the read paths (list / get)."""
    tolerated_version_bands: list[str] | None = None
    """``major.minor`` bands the cohort tolerates (patches incl. future)."""
    auto_grow: bool = True
    """Whether membership re-resolves live (vs a frozen picked set)."""
    member_job_ids: list[int] | None = None
    """The frozen run set for a fixed cohort (``None`` while ``auto_grow``)."""
    excluded_job_ids: list[int] | None = None
    project_ids: list[int] | None = None
    scenario_ids: list[int] | None = None
    owner_user_ids: list[int] | None = None
    source_kinds: list[str] | None = None
    """``scenario`` / ``workflow`` / ``parametric_study`` / ``optimisation_study``."""
    parametric_study_ids: list[int] | None = None
    optimisation_study_ids: list[int] | None = None
    completed_after: _dt.datetime | None = None
    completed_before: _dt.datetime | None = None
    library_file_version_ids: list[int] | None = None
    material_version_ids: list[int] | None = None
    input_ranges: dict[str, Any] | None = None
    cross_field_predicate: str | None = None
    auto_include_study_ids: list[int] | None = None
    """Studies this cohort commissioned to fill its own gaps (ADR-0020 §11.5).

    Their completed runs join the membership whichever mode the cohort is
    in. Exposed rather than kept internal: a cohort whose membership has a
    source the page never mentions is the same silent-drop problem coverage
    exists to report, one level up."""
    state: str
    """``active`` / ``archived``."""
    last_resolved_at: _dt.datetime | None = None
    created_by_user_id: int
    created_by_name: str = ""
    """Display name of the creator (``User.full_name``/username); '' if
    unresolved. Enriched on the read paths (list / get)."""
    member_count: int | None = None
    """How many runs the cohort currently resolves to *for the caller*
    (ACL-intersected, D7). ``None`` on write responses where it isn't
    computed; an int on the list / get read paths."""
    created_at: _dt.datetime
    version: int
    tool_name: str = ""
    """Display label of the cohort's tool (``Tool.name``); '' if
    unresolved. Use this over ``tool_alias`` in the UI
    ([[feedback_use_labels_not_names]])."""


class CohortMembersResponse(_Frozen):
    """``POST /api/cohorts/{cid}/resolve`` + ``GET .../members`` — live membership.

    ``acl_hidden`` is the count of members in projects the caller can't
    see (the "(N jobs hidden by access controls)" footnote, never an
    error).
    """

    cohort_id: int
    job_ids: list[int] = Field(default_factory=list)
    acl_hidden: int = 0
    last_resolved_at: _dt.datetime


class CohortResultRow(_Frozen):
    """One ``study_result`` row materialised for a cohort member.

    ``inputs`` are the run's input parameter values (from ``Job.inputs``)
    so the matrix can show the inputs alongside the response ``scalars``.
    """

    job_id: int
    inputs: dict[str, Any] = Field(default_factory=dict)
    scalars: dict[str, Any] = Field(default_factory=dict)


class CohortResultColumn(_Frozen):
    """One response column of the cohort results matrix.

    ``key`` is the (normalised, bare) scalar key the rows are keyed on;
    ``label`` + ``unit`` are the descriptor's human label and unit for
    that output ([[feedback_use_labels_not_names]]), so the UI renders
    ``"Heat Dissipation (W)"`` headers rather than internal names. An
    extra scalar the descriptor doesn't declare falls back to
    ``label = key`` and an empty unit.
    """

    key: str
    label: str
    unit: str = ""


class CohortResultsResponse(_Frozen):
    """``GET /api/cohorts/{cid}/results`` — lazy-populated member results.

    ``written`` is how many new rows this call materialised (§3.3 lazy
    populate); ``rows`` is the full visible result set; ``columns`` is the
    ordered response-column metadata (label + unit) the matrix renders.
    """

    cohort_id: int
    written: int
    acl_hidden: int = 0
    rows: list[CohortResultRow] = Field(default_factory=list)
    input_columns: list[CohortResultColumn] = Field(default_factory=list)
    """Input-parameter columns (label + unit), descriptor order — shown
    to the left of the response ``columns`` in the matrix."""
    columns: list[CohortResultColumn] = Field(default_factory=list)


# --- design-space coverage (ADR-0020) --------------------------------------


class CoverageEnvelopeOverride(_Frozen):
    """One edited row of the envelope form (§11.1).

    Every field is optional: ``None`` means "leave as the engine resolved
    it". Narrowing is the *expected* edit — "we ran 1-8 mm but only trust
    2-6" — which is why the form is editable rather than merely displayed.
    """

    name: str
    lo: float | None = None
    hi: float | None = None
    scale: Literal["linear", "log"] | None = None
    levels: list[str] | None = None
    hold: Any = None
    """Pin a varying axis at this value, taking it out of the design space.

    The inverse of promotion. A space-filling batch needs runs growing with
    the number of axes, so narrowing the batch to the inputs actually of
    interest is what keeps the count affordable."""


class CohortCoverageRequest(_Frozen):
    """``POST /api/cohorts/{cid}/coverage`` — the declared design envelope."""

    envelope: list[CoverageEnvelopeOverride] = Field(default_factory=list)


class CoverageEnvelopeFactor(_Frozen):
    """One axis of the declared region, as the form should pre-populate it."""

    name: str
    label: str = ""
    """The descriptor's human-readable label — what every surface must show
    ([[feedback_use_labels_not_names]]). ``name`` stays on the wire because
    the envelope-override payload is keyed by it; it is never displayed."""
    kind: Literal["continuous", "integer", "categorical", "pinned", "derived"]
    scale: Literal["linear", "log"] = "linear"
    lo: float | None = None
    hi: float | None = None
    levels: list[str] | None = None
    pinned_value: Any = None
    declared_levels: list[str] = Field(default_factory=list)
    """Every option a ``select`` input declares — the categorical range."""
    declared_lo: float | None = None
    declared_hi: float | None = None
    precision: float | None = None
    varying_kind: Literal["continuous", "integer", "categorical"] | None = None
    """What a ``pinned`` axis would become if the user chose to vary it.

    ``None`` for anything the descriptor declares no domain for, which is how
    a surface knows an input cannot be opened up at all (ADR-0020 §11.5).

    Includes ``categorical``: a held-constant ``select`` or ``boolean`` is
    opened up across its declared options, not across a range. This literal
    was numeric-only and silently mirrored the engine's old rule, so widening
    that rule made the whole coverage endpoint raise a 500 for any cohort with
    a held-constant choice — which is every cohort the feature was built for.
    ``test_varying_kind_contract`` now pins the two together."""
    actionable: bool = True
    """``False`` for ``derived`` / ``pinned`` — no design point can move it,
    so the UI offers the right remedy instead of a suggestion."""


class CoverageFactorRow(_Frozen):
    """One row of the per-factor strip — a measurement, not prose."""

    name: str
    label: str = ""
    """Display label ([[feedback_use_labels_not_names]]); '' falls back to
    ``name`` at the point of render."""
    kind: Literal["continuous", "integer", "categorical", "pinned", "derived"]
    measure: Literal["covering_radius", "lattice_occupancy", "level_balance", "pinned", "span"]
    scale: Literal["linear", "log"] = "linear"
    value: float = 0.0
    verdict: Literal["good", "thin", "poor", "insufficient"] = "good"
    missing_values: list[str] = Field(default_factory=list)
    actionable: bool = True

    simulated_lo: float | None = None
    simulated_hi: float | None = None
    """Lowest and highest values these runs actually used."""

    declared_lo: float | None = None
    declared_hi: float | None = None
    """Bounds the tool itself allows; ``None`` where it declares none."""

    declared_value: float = 0.0
    declared_verdict: Literal["good", "thin", "poor", "insufficient"] = "insufficient"
    """Coverage of the **tool's** range rather than of the range these runs
    happened to span (ADR-0020 §4.5) -- the reading that can say "you have
    explored none of what this tool accepts"."""

    precision: float | None = None
    """The input's own increment, so a surface can show a value to the
    precision the tool actually has."""


class CoveragePairRow(_Frozen):
    """How well two inputs were varied **against each other** (ADR-0020 §4.6).

    The level between one input's own range and the whole joint space. A
    one-factor-at-a-time archive passes both neighbours and fails here.
    """

    a: str
    b: str
    a_label: str = ""
    b_label: str = ""
    n_points: int = 0
    """Runs placeable on both axes; pairs legitimately differ."""
    coverage_score: float = 0.0
    covering_radius: float = 0.0
    reference_radius: float = 0.0
    verdict: Literal["good", "thin", "poor", "insufficient"] = "good"
    spread_score: float | None = None
    spread_verdict: Literal["good", "thin", "poor", "insufficient"] | None = None
    """The same radius against a design of this pair's own run count.

    `coverage_score` divides by a fixed 60-run yardstick and so answers "are
    there enough runs here"; this answers "are the runs there arranged well".
    `None` on a report stored before the second reading existed -- rendered as
    a dash, never as a zero, which would read as the worst arrangement."""
    detail: str = ""
    """The finding in words, where the numbers alone would be dismissed."""
    discriminating: bool = True
    """Whether this pair carries enough runs for either reading to order a
    good design against a bad one (`min_discriminating_pair_runs`). When
    false the row is shown and does not vote."""


class CoverageWarningResponse(_Frozen):
    """One plain-English thing the user should know (never jargon)."""

    code: str
    message: str
    factor: str | None = None
    factor_label: str | None = None
    """Display label for ``factor``; ``message`` already reads in labels."""


class CoverageDiagnosticsResponse(_Frozen):
    """The bookkeeping behind the verdict — where the excluded runs went."""

    n_points: int = 0
    n_excluded_invalid: int = 0
    n_excluded_filtered: int = 0
    n_outside_envelope: int = 0
    n_unmeasurable: int = 0
    """Runs that could not be placed at all — a missing or non-finite value on
    a measured input. Exposed because the surface has to be able to *say so*:
    a run silently dropped from a coverage count is the failure this whole
    feature exists to catch, and it would be absurd to commit it here."""
    n_near_duplicates: int = 0
    max_abs_correlation: float | None = None
    confounded: bool = False
    condition_number: float | None = None
    level_counts: dict[str, dict[str, int]] = Field(default_factory=dict)
    underpopulated_levels: list[list[Any]] = Field(default_factory=list)
    grouping_mode: Literal["split", "pooled", "n/a"] = "n/a"
    factor_correlations: dict[str, float] | None = None
    """Pairwise correlations keyed ``"a|b"``. ``None`` — not ``{}`` — below
    two continuous factors: "we cannot tell" and "they are uncorrelated"
    are different answers."""


class CoverageResponseRow(_Frozen):
    """One response's verdict on a fitted version (empty at cohort preview)."""

    response_name: str
    response_kind: Literal["scalar", "field"]
    surrogate_method: str
    criterion: Literal["model_free", "gp_variance"]
    verdict: Literal["good", "thin", "poor", "insufficient"]
    message: str = ""
    coverage_score: float = 0.0
    suggested_points: list[dict[str, Any]] = Field(default_factory=list)
    edge_sigma_threshold: float | None = None
    available_statuses: list[str] = Field(default_factory=list)
    ard_relevance: dict[str, float] | None = None


class ModelVersionCoverageResponse(_Frozen):
    """``GET /models/versions/{vid}/design-coverage`` — the stored report (§9).

    A **different** thing from ``ModelCoverageResponse``, which is the
    ADR-0008 §7.3 in-form gauge (the frozen training *ranges* a submitted
    input is classified against). Two things are called coverage in this
    codebase; keeping them on separate routes keeps them separately correct.

    ``assessed=False`` with ``report=None`` is the honest answer for a version
    fitted before coverage existed. The surface renders that as **"not
    assessed"**, never "good" — silence about coverage and a clean bill of
    health are opposite claims, and the schema keeps them distinguishable
    rather than collapsing both into an empty report.
    """

    version_id: int
    assessed: bool = False
    report: CoverageReportResponse | None = None


class CoverageReportResponse(_Frozen):
    """The whole coverage assessment for a cohort or a model version.

    ``verdict`` is the worst of the design pass and every response — a model
    is only as trustworthy as its least-covered response. It **never blocks**
    anything; it is recorded and travels with the model.
    """

    verdict: Literal["good", "thin", "poor", "insufficient"]
    message: str
    criterion: Literal["model_free", "gp_variance"]
    efficiency_score: float = 0.0
    """Are the runs arranged well **for how many there are**? (§4.4)

    A separate question from coverage, and the number that used to be the
    headline. Scored against a bar that moves with the archive's own size, so
    it can fall when runs are added -- meaningful about efficiency, nonsense
    about coverage, which is why it no longer sets the light."""

    discriminating: bool = True
    """Whether the joint figure is meaningful at this width (§4.4). ``False``
    above six varying inputs, where a covering radius cannot separate a
    cluster from an even spread; the verdict then comes from the per-input
    and per-pair readings."""

    coverage_score: float = 0.0
    """How well spread the runs are, against a **fixed** standard, 0..1.

    A ratio of two *sampled* estimates, so it is not a precise quantity:
    across eight probe seeds it moves by more than 0.05 on most designs. The
    **verdict** is the stable read (it did not flip once across 40 designs x 8
    seeds); this number is not fit for ranking archives, trending, or gating
    an automated pipeline."""

    covering_radius: float = 0.0
    """The worst distance from anywhere in the declared range to the nearest
    run, in normalised units.

    Estimated over ``n_candidates`` probe points and therefore **low-biased**
    — the flattering direction, since the archive then looks better covered
    than it is. Median error against a 64x denser probe runs from -1.4% at two
    factors to about -9% at eight; ``warnings`` carries a ``coarse_estimate``
    entry once the probe is too thin to call it a measurement."""

    n_candidates: int = 0
    """Probe points the radius was estimated over — recorded so two reports
    written under different settings are not mistaken for comparable."""

    reference_radius: float = 0.0
    """What ``covering_radius`` would have been had the same number of runs
    been laid out evenly. ``coverage_score`` is the ratio of the two, so
    without this a caller cannot tell whether a changed score means the design
    moved or simply that the archive grew."""
    source_kind: str = "cohort"
    computed_at: _dt.datetime | None = None
    envelope: list[CoverageEnvelopeFactor] = Field(default_factory=list)
    per_factor: list[CoverageFactorRow] = Field(default_factory=list)
    per_pair: list[CoveragePairRow] = Field(default_factory=list)
    """Pairwise coverage, worst first (§4.6)."""
    n_pairs_examined: int = 0
    n_pairs_total: int = 0
    min_points_per_pair: int = 0
    min_discriminating_pair_runs: int = 0
    density_discriminating: bool = True
    min_discriminating_runs_per_input: int = 0
    sampling_modes: list[str] = Field(default_factory=list)
    """How the runs were sampled, where every one came from a study. Empty
    means "not established", never "not designed"."""
    """Runs a pair needs on both axes before it is judged, so an empty sweep
    can say why rather than reading as a clean bill of health."""
    """Pairs scored, and pairs that exist -- they differ when the axis cap
    bites, and the surface must say so rather than imply a full sweep."""
    per_response: list[CoverageResponseRow] = Field(default_factory=list)
    warnings: list[CoverageWarningResponse] = Field(default_factory=list)
    diagnostics: CoverageDiagnosticsResponse = Field(default_factory=CoverageDiagnosticsResponse)
    points: list[dict[str, Any]] = Field(default_factory=list)
    """The training points, for the pairwise scatter matrix (§11.4) — the
    visual payoff, and not reconstructable from the verdict alone."""


class CohortCoverageSuggestRequest(_Frozen):
    """``POST /api/cohorts/{cid}/coverage/suggest`` — plan a batch (§11.5).

    Read-only: planning costs nothing. ``n`` is the spinner's value.
    """

    envelope: list[CoverageEnvelopeOverride] = Field(default_factory=list)
    n: int = Field(default=5, ge=1, le=500)


class CohortCoverageStudyRequest(_Frozen):
    """``POST /api/cohorts/{cid}/coverage/study`` — commit the batch (§11.5).

    Spawns real solver jobs eventually, so this one is permission-gated
    while the suggestion above is not.
    """

    envelope: list[CoverageEnvelopeOverride] = Field(default_factory=list)
    n: int = Field(default=5, ge=1, le=500)
    project_id: int
    name: str = Field(min_length=1, max_length=200)


class CohortCoveragePlanRequest(_Frozen):
    """``POST /api/cohorts/{cid}/coverage/plan`` -- price a spread of batch sizes.

    Read-only: pricing costs nothing. ``sizes`` overrides the default ladder.
    """

    envelope: list[CoverageEnvelopeOverride] = Field(default_factory=list)
    """The space the proposed batch may **move** in."""
    measure_envelope: list[CoverageEnvelopeOverride] | None = None
    """The space the figures **describe**, when it differs from the above.

    Leaving an input out of a batch is expressed by pinning it, which is what
    stops the walk moving it — and also removes it from every measurement. A
    user who narrowed the batch has not necessarily asked to stop being told
    about that input's gaps, so the two spaces are sent separately. ``None``
    means "same as ``envelope``", the narrowed reading."""
    sizes: list[int] | None = Field(default=None, max_length=8)
    """Batch sizes to price. Omit to let the engine choose a ladder scaled to
    the number of varying inputs; pass one value for a size the user typed."""


class CoveragePlanOption(_Frozen):
    """One row of the "how many runs?" table -- what ``n`` runs would buy."""

    n: int
    verdict: Literal["good", "thin", "poor", "insufficient"]
    coverage_score: float = 0.0
    covering_radius: float = 0.0
    gap_after: float = 0.0
    """Biggest untried stretch afterwards, as a share of the whole space."""
    reaches_good: bool = False
    """The smallest offered batch that reaches ``good`` -- the row to
    recommend, because past it the user is buying diminishing returns."""
    levels_short: int = 0
    """Option levels still under-run after this batch.

    The gap is a covering radius, which a categorical axis contributes
    nothing to. Once a held-constant choice can be opened up, a batch that
    sweeps two materials moves the gap barely at all while doing exactly what
    was asked -- so the categorical price is reported beside the gap rather
    than blended into it."""


class CoveragePlanResponse(_Frozen):
    """The batch-size cost/benefit table, plus the runs themselves (§11.5).

    ``points`` is the batch at the **largest** offered size; the batch for
    option ``n`` is its first ``n`` rows exactly, because the suggestion walk
    is greedy. Sent whole so choosing a size needs no second round trip and
    the user can read the actual input values before committing anything.
    """

    options: list[CoveragePlanOption] = Field(default_factory=list)
    points: list[dict[str, Any]] = Field(default_factory=list)
    factor_labels: dict[str, str] = Field(default_factory=dict)
    """``{input name: display label}`` for the proposed-runs table
    ([[feedback_use_labels_not_names]]); the rows stay keyed by name."""
    current_verdict: Literal["good", "thin", "poor", "insufficient"] = "insufficient"
    current_score: float = 0.0
    current_gap: float = 0.0
    current_levels_short: int = 0
    already_good: bool = False
    """Whether the archive already reads ``good`` -- if so no row is the
    "enough" one, because there is no threshold left to cross."""
    measured_axes: int = 0
    """Numeric axes the figures were measured over -- zero for a space made
    only of choices, where the gap is `0.0` because there is no geometry to
    take a radius over, not because there is no gap."""
    warnings: list[CoverageWarningResponse] = Field(default_factory=list)


class CoverageSuggestionResponse(_Frozen):
    """Proposed runs, and anything the user should know about the batch."""

    response_name: str = ""
    """Which response the batch improves. Empty at cohort preview, where no
    response has been chosen — attributing it to one would be a claim the
    model-free path cannot make."""
    points: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[CoverageWarningResponse] = Field(default_factory=list)


# --- optimisation studies (ADR-0009) ---------------------------------------


class OptimisationStudyCreateRequest(_Frozen):
    """``POST /api/projects/{pid}/optimisation-studies`` body — mint a draft."""

    name: str = Field(min_length=1, max_length=128)
    tool_alias: str = Field(min_length=1, max_length=64)
    tool_version_id: int
    description: str = Field(default="", max_length=2000)


class OptimisationConfigPatchRequest(_Frozen):
    """``PATCH /api/optimisation-studies/{sid}`` body — merge a config step (draft-only).

    The patch is a shallow top-level merge into ``config_json`` (factors /
    responses / constraints / strategy / acquisition / run_mode / seed /
    batch_size / stopping_criteria / …); the ``target`` key is frozen.
    """

    config_patch: dict[str, Any] = Field(default_factory=dict)


class OptimisationStudyResponse(_Frozen):
    """An optimisation study as seen over the wire."""

    id: int
    project_id: int
    project_name: str = ""
    """Owning project's display name; '' if unresolved (read paths)."""
    name: str
    description: str
    target_kind: str
    tool_alias: str | None = None
    tool_name: str = ""
    """Tool display label ([[feedback_use_labels_not_names]]); '' if
    unresolved or the target is a workflow."""
    tool_version_id: int | None = None
    tool_version: str = ""
    """Pinned tool version *string*; '' if unresolved / workflow target."""
    workflow_alias: str | None = None
    workflow_name: str = ""
    """Workflow display label; '' if unresolved or the target is a tool."""
    workflow_version_id: int | None = None
    workflow_version: str = ""
    """Pinned workflow version string; '' if unresolved / tool target."""
    scenario_id: int | None = None
    config_json: dict[str, Any] = Field(default_factory=dict)
    state: str
    stop_reason: str | None = None
    last_batch_number: int = 0
    current_best_json: dict[str, Any] | None = None
    mop_state: dict[str, Any] | None = None
    halt_requested: bool = False
    created_by_user_id: int
    created_by_name: str = ""
    """Creator's display name (full_name/username); '' if unresolved."""
    created_at: _dt.datetime
    archived_at: _dt.datetime | None = None
    version: int


class OptimisationConvergenceResponse(_Frozen):
    """``GET /api/optimisation-studies/{sid}/convergence`` — the live trace."""

    study_id: int
    state: str
    stop_reason: str | None = None
    last_batch_number: int = 0
    current_best_json: dict[str, Any] | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)


class OptimisationRunResultRow(_Frozen):
    """One evaluated run in an optimisation study's results table."""

    job_id: int
    batch: int | None = None
    state: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    scalars: dict[str, Any] = Field(default_factory=dict)


class OptimisationRunResultsResponse(_Frozen):
    """``GET /api/optimisation-studies/{sid}/run-results`` — the per-run matrix."""

    study_id: int
    runs: list[OptimisationRunResultRow] = Field(default_factory=list)


class OptimisationWorkflowStudyCreateRequest(_Frozen):
    """``POST /api/projects/{pid}/optimisation-studies/workflow`` — a workflow-target draft."""

    name: str = Field(min_length=1, max_length=128)
    workflow_alias: str
    workflow_version_id: int | None = None
    description: str = ""


class OptimisationRecommendationResponse(_Frozen):
    """``GET /api/optimisation-studies/{sid}/recommendation`` — the Navigator's pick (§5.6)."""

    strategy: str
    run_mode: str
    batch_size: int
    rationale: str
    acquisition: str | None = None


class OptimisationConfigureRequest(_Frozen):
    """``POST /api/optimisation-studies/{sid}/configure`` — optional start approver."""

    start_approver_user_id: int | None = None


class OptimisationContinueRequest(_Frozen):
    """``POST /api/optimisation-studies/{sid}/continue`` — extra runs to add (§14)."""

    additional_runs: int = Field(ge=1)


class OptimisationCostEstimateResponse(_Frozen):
    """``GET /api/optimisation-studies/{sid}/cost-estimate`` — campaign footprint (§14.1)."""

    max_runs: int
    seed_runs: int
    max_total_children: int
    batch_size: int


class OptimisationStartDecisionRequest(_Frozen):
    """``POST /api/optimisation-studies/{sid}/start-approve|start-reject`` body."""

    reason: str | None = None


class OptimisationApproverOption(_Frozen):
    """One eligible start-gate approver for the create-wizard picker (§14.1)."""

    user_id: int
    username: str


class OptimisationMopComputeRequest(_Frozen):
    """``POST /api/optimisation-studies/{sid}/mop`` — optional cutoff override."""

    cutoff: float | None = None


class OptimisationMopFrozenFactorsRequest(_Frozen):
    """``POST /api/optimisation-studies/{sid}/mop/frozen-factors`` body."""

    frozen_factors: list[str] = Field(default_factory=list)


class OptimisationMopResponse(_Frozen):
    """The MOP/CoP analysis + frozen-factor decision on a study (§5.7)."""

    best_kind: str | None = None
    q2: float | None = None
    cop: list[dict[str, Any]] = Field(default_factory=list)
    kind_q2: dict[str, float | None] = Field(default_factory=dict)
    cutoff: float | None = None
    suggested_prune: list[str] = Field(default_factory=list)
    frozen_factors: list[str] = Field(default_factory=list)
    message: str | None = None
    n_runs: int = 0
    objective: str | None = None
    updated_at: str | None = None


class OptimisationPublishSurrogateRequest(_Frozen):
    """``POST /api/optimisation-studies/{sid}/publish-surrogate`` body."""

    name: str | None = Field(default=None, max_length=255)
    scope: str = "personal"
    surrogate_kind: str | None = None


class OptimisationPublishSurrogateResponse(_Frozen):
    """The registry version minted from the study's working surrogate."""

    model_id: int
    version_id: int
    ordinal: int
    scope: str


class CsvChildSeries(_Frozen):
    """One child's down-sampled ``(x, y)`` series for the curve-overlay."""

    job_id: int
    x: list[float] = Field(default_factory=list)
    y: list[float] = Field(default_factory=list)


class CsvSeriesResponse(_Frozen):
    """``GET /api/studies/{id}/csv-series`` — per-child series for the overlay (2-PS-b)."""

    study_id: int
    output_name: str
    x_column: int
    y_column: int
    children: list[CsvChildSeries] = Field(default_factory=list)


class CsvAggregateValue(_Frozen):
    """One child's column reduced to a scalar (``None`` = no file / no data)."""

    job_id: int
    value: float | None = None


class CsvAggregateResponse(_Frozen):
    """``GET /api/studies/{id}/csv-aggregate`` — the computed response column (2-PS-b)."""

    study_id: int
    output_name: str
    column: int
    aggregate: str
    values: list[CsvAggregateValue] = Field(default_factory=list)


class FactorSensitivityEntry(_Frozen):
    """One factor's association with the chosen response (2-PS-b5)."""

    factor: str
    pearson: float | None = None
    """Pearson r over pairwise-complete points; ``None`` if undefined."""
    spearman: float | None = None
    """Spearman rank correlation; ``None`` if undefined."""
    std_coef: float | None = None
    """Standardized regression coefficient — the tornado-bar value
    (magnitude = ``abs``); ``None`` when the multivariate fit could not run."""
    n: int = 0
    """Pairwise-complete points behind the correlations."""
    pearson_p: float | None = None
    """Two-sided p-value for the Pearson correlation."""
    spearman_p: float | None = None
    """Two-sided p-value for the Spearman correlation."""
    std_coef_se: float | None = None
    """Standard error of the standardized regression coefficient."""
    std_coef_p: float | None = None
    """p-value for the standardized regression coefficient (H0: = 0)."""
    std_coef_ci_low: float | None = None
    """Low end of the SRC 95% confidence interval (tornado error bar)."""
    std_coef_ci_high: float | None = None
    """High end of the SRC 95% confidence interval."""


class SensitivityResponse(_Frozen):
    """``GET /api/studies/{id}/sensitivity`` — factor ranking for one response (2-PS-b5)."""

    study_id: int
    response: str
    """Human label of the response axis (scalar name, or
    ``output - colN - agg`` for a csv_plot column)."""
    factors: list[FactorSensitivityEntry] = Field(default_factory=list)
    """Ordered for a tornado chart (``|std_coef|`` desc, ``|pearson|`` fallback)."""
    r_squared: float | None = None
    """R² of the standardized linear model, or ``None`` if it did not run."""
    adj_r_squared: float | None = None
    """Adjusted R² (penalises extra factors)."""
    rmse: float | None = None
    """RMSE of the standardized-model residuals."""
    n_samples: int = 0
    sufficient: bool = False
    """``False`` when too few children have a response value (UI greys out)."""
    message: str | None = None
    """Why the result is insufficient, or a caveat (e.g. SRC unavailable)."""


class SensitivityMatrixRow(_Frozen):
    """One varied input's standardized regression coefficient per output (2-PS-b')."""

    factor: str
    coefficients: dict[str, float | None] = Field(default_factory=dict)
    """Output name → standardized regression coefficient (``None`` when the
    fit could not run for that output)."""
    pvalues: dict[str, float | None] = Field(default_factory=dict)
    """Output name → p-value for that coefficient (for significance marking)."""


class RsmResponse(_Frozen):
    """``GET /api/studies/{id}/rsm`` — quadratic response surface (2-PS-c-stats #6)."""

    study_id: int
    response: str
    factors: list[str] = Field(default_factory=list)
    """Varied factor names available for the X/Y contour axes."""
    x: str = ""
    y: str = ""
    x_grid: list[float] = Field(default_factory=list)
    y_grid: list[float] = Field(default_factory=list)
    z: list[list[float]] = Field(default_factory=list)
    """Predicted response over the (y_grid x x_grid) mesh, others at their mean."""
    r_squared: float | None = None
    adj_r_squared: float | None = None
    rmse: float | None = None
    n: int = 0
    stationary: dict[str, float] | None = None
    stationary_kind: str | None = None
    stationary_value: float | None = None
    message: str | None = None


class AnovaTermEntry(_Frozen):
    """One ANOVA term's % contribution (2-PS-c-stats #5)."""

    term: str
    contribution: float


class MainEffectPoint(_Frozen):
    """Mean response at one factor level (main-effects plot point)."""

    level: float
    mean: float


class AnovaResponse(_Frozen):
    """``GET /api/studies/{id}/anova`` — ANOVA % contribution + main effects (2-PS-c-stats #5)."""

    study_id: int
    response: str
    terms: list[AnovaTermEntry] = Field(default_factory=list)
    residual: float = 100.0
    """Percent of total variance left unexplained."""
    n: int = 0
    message: str | None = None
    main_effects: dict[str, list[MainEffectPoint]] = Field(default_factory=dict)
    """Categorical factor name → mean response per level (UI maps to labels)."""


class SensitivityMatrixResponse(_Frozen):
    """``GET /api/studies/{id}/sensitivity-matrix`` — varied-inputs x outputs SRC grid (2-PS-b')."""

    study_id: int
    factors: list[str] = Field(default_factory=list)
    """Varied input names, row order (UI maps to labels)."""
    responses: list[str] = Field(default_factory=list)
    """Selected output names, column order (UI maps to labels)."""
    rows: list[SensitivityMatrixRow] = Field(default_factory=list)
    r_squared: dict[str, float | None] = Field(default_factory=dict)
    """Per-output linear-model R² (``None`` when the fit could not run)."""
    message: str | None = None


# --- Live Jobs (Admin → Usage & Statistics) ---------------------------------


class LiveJobItem(_Frozen):
    """One in-flight row in the Live Jobs list.

    ``kind`` discriminates the three row types (``job`` / ``workflow`` /
    ``study``); ``name`` is the tool name (job), the workflow name
    (workflow run), or the study name. ``cancellable`` reflects whether
    the underlying cancel flow can act on it now — the UI disables the
    row's tick when it can't.
    """

    kind: Literal["job", "workflow", "study"]
    id: int
    name: str
    state: str
    submitter_user_id: int | None = None
    submitter_name: str = ""
    submitted_at: _dt.datetime
    project_name: str | None = None
    scenario_name: str | None = None
    child_count: int | None = None
    cancellable: bool = False


class LiveJobsSubmitter(_Frozen):
    """A distinct submitter present in the in-flight set (a filter option)."""

    user_id: int
    name: str


class LiveJobsListResponse(_Frozen):
    """``GET /api/admin/live-jobs`` — the in-flight rows + filter options.

    ``can_cancel`` echoes whether the caller holds ``live_jobs.cancel``
    so the UI can show / hide the cancel controls without a second
    round-trip.
    """

    items: list[LiveJobItem] = Field(default_factory=list)
    submitters: list[LiveJobsSubmitter] = Field(default_factory=list)
    can_cancel: bool = False


class LiveJobsCancelRequest(_Frozen):
    """``POST /api/admin/live-jobs/cancel`` body — ids to cancel per kind."""

    job_ids: list[int] = Field(default_factory=list)
    run_ids: list[int] = Field(default_factory=list)
    study_ids: list[int] = Field(default_factory=list)


class LiveJobsCancelOutcome(_Frozen):
    """The result of attempting to cancel one item."""

    kind: Literal["job", "workflow", "study"]
    id: int
    cancelled: bool
    reason: str = ""


class LiveJobsCancelResponse(_Frozen):
    """``POST /api/admin/live-jobs/cancel`` — per-item outcomes + a count."""

    cancelled: int = 0
    outcomes: list[LiveJobsCancelOutcome] = Field(default_factory=list)


# --- materials database (ADR-0007) ---------------------------------------


class MaterialPropertyPayload(_Frozen):
    """One scalar property in a create/add-version request body.

    ``slug`` is optional — when omitted the service derives it from
    ``prop_name`` via :func:`matofx.materials.slugify` (the standard
    "lowercase, non-alphanumeric → ``_``" rule). Provide it explicitly
    only when the display label can't unambiguously slugify to the
    class's expected identifier.
    """

    prop_name: str = Field(min_length=1, max_length=64)
    value: float
    unit: str = Field(min_length=1, max_length=32)
    slug: str | None = Field(default=None, min_length=1, max_length=64)
    source: str | None = None
    uncertainty: str | None = Field(default=None, max_length=64)
    notes: str | None = None


class MaterialPropertyResponse(_Frozen):
    """One scalar property as returned by the API.

    ``slug`` is the immutable identifier; ``prop_name`` is the editable
    display label snapshotted into this version at creation.
    """

    slug: str
    prop_name: str
    value: float
    unit: str
    source: str | None = None
    uncertainty: str | None = None
    notes: str | None = None


class MaterialClassPropertyResponse(_Frozen):
    """One expected-property row of a material class.

    ``slug`` is the immutable snake_case identifier (referenced by tool
    descriptors and used as the key in the frozen ``inputs.json``
    block); ``prop_name`` is the editable display label.
    """

    id: int
    slug: str
    prop_name: str
    unit: str
    required: bool


class MaterialClassResponse(_Frozen):
    """A material class + its expected-property schema."""

    id: int
    name: str
    description: str
    state: str
    properties: list[MaterialClassPropertyResponse] = Field(default_factory=list)


class MaterialClassCreateRequest(_Frozen):
    """``POST /api/materials/classes`` body."""

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=2048)


class MaterialClassPropertyCreateRequest(_Frozen):
    """``POST /api/materials/classes/{id}/properties`` body.

    ``slug`` is the immutable identifier (snake_case). When omitted the
    service derives it from ``prop_name``; you can override the derived
    slug by providing one explicitly (e.g. to match an established
    convention used by your existing tool descriptors).
    """

    prop_name: str = Field(min_length=1, max_length=64)
    unit: str = Field(min_length=1, max_length=32)
    required: bool = True
    slug: str | None = Field(default=None, min_length=1, max_length=64)


class MaterialClassPropertyPatch(_Frozen):
    """``PATCH /api/materials/classes/{id}/properties/{pid}`` body.

    Any combination of ``prop_name``, ``unit``, ``required`` (or none —
    a no-op). Edits the class schema only; past material versions keep
    their immutable snapshots (see :meth:`MaterialsService.update_class_property`).
    """

    prop_name: str | None = Field(default=None, min_length=1, max_length=64)
    unit: str | None = Field(default=None, min_length=1, max_length=32)
    required: bool | None = None


class MaterialVersionResponse(_Frozen):
    """One immutable material version + its properties."""

    id: int
    ordinal: int
    provenance: str
    reference: str | None
    data_pedigree: str | None
    conditions: str | None
    version_notes: str | None
    effective_date: _dt.date | None
    publish_state: str
    created_at: _dt.datetime
    properties: list[MaterialPropertyResponse] = Field(default_factory=list)


class MaterialResponse(_Frozen):
    """A material card (no versions). ``class_name`` is denormalised in."""

    id: int
    name: str
    aliases: list[str]
    class_id: int
    class_name: str
    description: str
    tags: list[str]
    steward_user_id: int
    confidentiality: str | None
    state: str
    version: int
    created_at: _dt.datetime
    # The count of immutable ``MaterialVersion`` rows for this material,
    # surfaced on the list/detail row so the catalog reads at a glance
    # ("Water v3" = 3 immutable revisions). Distinct from ``version``,
    # which is the optimistic-lock counter on the card itself.
    version_count: int = 0


class MaterialDetailResponse(_Frozen):
    """A material card + its full version history (newest first)."""

    material: MaterialResponse
    versions: list[MaterialVersionResponse] = Field(default_factory=list)


class MaterialCreateRequest(_Frozen):
    """``POST /api/materials`` body — create a material + first version.

    ``approver_user_id`` is the curator the author nominates to review
    the change; required only when the ``materials_change_approval``
    toggle is ON (the router resolves the toggle).
    """

    name: str = Field(min_length=1, max_length=255)
    class_id: int
    description: str = Field(default="", max_length=4096)
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    confidentiality: str | None = Field(default=None, max_length=64)
    provenance: str = Field(min_length=1, max_length=4096)
    reference: str | None = None
    data_pedigree: str | None = None
    conditions: str | None = None
    version_notes: str | None = None
    effective_date: _dt.date | None = None
    properties: list[MaterialPropertyPayload] = Field(default_factory=list)
    approver_user_id: int | None = None


class MaterialAddVersionRequest(_Frozen):
    """``POST /api/materials/{id}/versions`` body — append a version."""

    provenance: str = Field(min_length=1, max_length=4096)
    reference: str | None = None
    data_pedigree: str | None = None
    conditions: str | None = None
    version_notes: str | None = None
    effective_date: _dt.date | None = None
    properties: list[MaterialPropertyPayload] = Field(default_factory=list)
    approver_user_id: int | None = None


class MaterialMetadataPatch(_Frozen):
    """``PATCH /api/materials/{id}`` body — card-level metadata edit."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    aliases: list[str] | None = None
    tags: list[str] | None = None
    confidentiality: str | None = Field(default=None, max_length=64)
    expected_version: int | None = None


class MaterialPickerCandidate(_Frozen):
    """One material offered in a ``material``-field picker (latest published)."""

    material_id: int
    name: str
    class_name: str
    version_id: int
    version_ordinal: int
    properties: dict[str, MaterialPropertyResponse] = Field(default_factory=dict)


class MaterialApproverOption(_Frozen):
    """An eligible curator the author can nominate to review a change."""

    user_id: int
    username: str
    full_name: str


# --- file library (ADR-0006) ---------------------------------------------

LibraryScope = Literal["personal", "project", "global"]


class LibraryToolAssignmentPayload(_Frozen):
    """One ``(tool_alias[, field_name])`` mapping in a create request."""

    tool_alias: str = Field(min_length=1, max_length=64)
    field_name: str | None = Field(default=None, max_length=128)


class LibraryFileCreatePayload(_Frozen):
    """JSON ``metadata`` form-field for ``POST /api/library/files``.

    Sent as a JSON string in the multipart ``metadata`` field alongside
    the ``file`` upload (mirrors the tool-install endpoint). ``scope``
    defaults to ``personal``; ``approver_user_id`` is required only when
    landing in ``global`` with the publish-approval toggle ON (the
    router resolves the toggle).
    """

    name: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=4096)
    provenance: str = Field(min_length=1, max_length=4096)
    scope: LibraryScope = "personal"
    project_id: int | None = None
    file_kind: str | None = Field(default=None, max_length=64)
    tags: list[str] = Field(default_factory=list)
    handling_label: str | None = Field(default=None, max_length=64)
    reference: str | None = None
    version_notes: str | None = None
    tool_assignments: list[LibraryToolAssignmentPayload] = Field(default_factory=list)
    approver_user_id: int | None = None


class LibraryVersionCreatePayload(_Frozen):
    """JSON ``metadata`` form-field for ``POST .../files/{id}/versions``."""

    provenance: str = Field(min_length=1, max_length=4096)
    reference: str | None = None
    version_notes: str | None = None
    approver_user_id: int | None = None


class LibraryMetadataPatch(_Frozen):
    """``PATCH /api/library/files/{id}`` body — entry-level metadata edit."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=4096)
    file_kind: str | None = Field(default=None, max_length=64)
    tags: list[str] | None = None
    handling_label: str | None = Field(default=None, max_length=64)
    expected_version: int | None = None


class LibraryScopeChangeRequest(_Frozen):
    """``POST /api/library/files/{id}/scope`` body — the governed move.

    ``approver_user_id`` is required only when moving to ``global`` with
    the publish-approval toggle ON.
    """

    new_scope: LibraryScope
    project_id: int | None = None
    expected_version: int | None = None
    approver_user_id: int | None = None


class LibraryToolAssignRequest(_Frozen):
    """``POST /api/library/files/{id}/tools`` body."""

    tool_alias: str = Field(min_length=1, max_length=64)
    field_name: str | None = Field(default=None, max_length=128)


class LibraryVersionResponse(_Frozen):
    """One immutable library-file version."""

    id: int
    ordinal: int
    original_filename: str
    content_hash: str
    size_bytes: int
    extension: str
    provenance: str
    reference: str | None
    version_notes: str | None
    publish_state: str
    created_at: _dt.datetime


class LibraryToolAssignmentResponse(_Frozen):
    """One tool assignment of a library file."""

    id: int
    tool_alias: str
    field_name: str | None


class LibraryFileResponse(_Frozen):
    """A library-file entry card (no versions)."""

    id: int
    name: str
    description: str
    file_kind: str | None
    tags: list[str]
    scope: str
    project_id: int | None
    owner_user_id: int
    handling_label: str | None
    state: str
    version: int
    created_at: _dt.datetime
    updated_at: _dt.datetime
    # The count of immutable ``LibraryFileVersion`` rows for this file
    # (distinct from ``version`` which is the optimistic-lock counter on
    # the entry). Surfaced on the list/detail rows so the catalog reads
    # at a glance ("inlet.stp v2" = 2 versions stored).
    version_count: int = 0
    # Tool assignments of this file (``tool_alias[, field_name]``), so the
    # workspace list can show — and search by — which tools/fields a file
    # is assigned to without a per-row detail fetch. Empty when none.
    tools: list[LibraryToolAssignmentResponse] = Field(default_factory=list)


class LibraryFileDetailResponse(_Frozen):
    """A library-file card + its full version history + tool assignments."""

    file: LibraryFileResponse
    versions: list[LibraryVersionResponse] = Field(default_factory=list)
    tools: list[LibraryToolAssignmentResponse] = Field(default_factory=list)


class LibraryPickerCandidate(_Frozen):
    """One library file offered in a tool form's file-field picker (§3)."""

    file_id: int
    name: str
    description: str
    scope: str
    file_kind: str | None
    tags: list[str]
    version_id: int
    ordinal: int
    original_filename: str
    extension: str
    size_bytes: int
    content_hash: str


class LibraryApproverOption(_Frozen):
    """An eligible curator the author can nominate to review a publish."""

    user_id: int
    username: str
    full_name: str


# --- lineage (ADR-0010) --------------------------------------------------


class LineageNodeResponse(_Frozen):
    """One node in the lineage graph response (matches LineageNode)."""

    kind: str
    id: int
    label: str
    state: str | None
    archived: bool
    deep_link: str | None
    payload: dict[str, Any]
    placeholder: bool


class LineageEdgeResponse(_Frozen):
    """One directed edge in the lineage graph response (matches LineageEdge).

    ``source`` and ``target`` are ``[kind, id]`` two-element arrays —
    the same shape :func:`matofx.lineage.graph_to_json` serialises.
    """

    source: tuple[str, int]
    target: tuple[str, int]
    relation: str
    label: str
    field_name: str | None
    truncated_count: int


class LineageGraphResponse(_Frozen):
    """The full ``GET /api/lineage/{kind}/{id}`` response body."""

    centre: str
    """Stable ``kind:id`` identifier of the inspected node."""
    nodes: list[LineageNodeResponse]
    edges: list[LineageEdgeResponse]
    hidden_by_acl: int
    hidden_by_depth: int
    soft_capped: bool


class LineagePickerEntry(_Frozen):
    """One option in the admin lineage centre-picker (slice 5b fix)."""

    id: int
    label: str
    state: str | None


# --- workflow graph (ADR-0013) -------------------------------------------


class WorkflowGraphNodeResponse(_Frozen):
    """One node in the workflow-structure graph (matches WorkflowGraphNode)."""

    tool_alias: str
    label: str
    position: int
    tool_state: str | None
    archived: bool
    deep_link: str | None
    run_state: str | None
    """``"ready"`` / ``"waiting"`` / ``"queued"`` / ``"running"`` /
    ``"complete"`` / ``"failed"`` when the graph was built with a
    ``run_id``; ``None`` otherwise."""
    child_job_id: int | None
    payload: dict[str, Any]


class WorkflowGraphEdgeResponse(_Frozen):
    """One descriptor-inferred edge in the workflow graph."""

    source: str
    """Upstream tool alias."""
    target: str
    """Downstream tool alias."""
    field_labels: list[str]
    """Human-facing labels of the downstream input fields that
    declared this wiring (sorted; descriptor ``label`` text, falling
    back to ``name`` when the descriptor has no label). Empty when
    the edge came from a forward ``downstream_tools`` declaration
    without a per-field claim."""


class WorkflowGraphRequest(_Frozen):
    """``POST /api/workflows/graph/preview`` request body.

    Carries an *unsaved* tool selection — the user is still picking
    tools in the create / edit dialog and wants to see the resulting
    structure before committing. The aliases are validated for
    existence + visibility server-side; duplicates are rejected.
    """

    tool_aliases: list[str] = Field(min_length=1, max_length=64)
    """The chosen tools, in user-supplied order. Capped well above
    the custom-workflow ``max_length=10`` so admin-workflow previews
    fit too; the preview is cheap enough that a generous upper bound
    isn't a footgun."""


class WorkflowGraphResponse(_Frozen):
    """The full ``GET /api/workflows/{id}/graph`` response body.

    Also the response shape for the preview endpoint
    (``POST /api/workflows/graph/preview``) — in preview mode
    ``workflow_id`` is ``None`` and the header fields carry
    placeholder values; the UI suppresses the header rather than
    rendering misleading metadata.
    """

    workflow_id: int | None
    """The graphed workflow's id; ``None`` for previews of unsaved
    tool selections."""

    workflow_alias: str
    workflow_name: str
    workflow_version: str
    workflow_state: str
    nodes: list[WorkflowGraphNodeResponse]
    edges: list[WorkflowGraphEdgeResponse]
    run_id: int | None
    run_state: str | None
    """Rollup state computed by ``compute_state`` — ``"draft"`` /
    ``"running"`` / ``"complete"`` / ``"failed"`` / ``"cancelled"``;
    ``None`` when ``run_id`` is ``None``."""


# --- V&V tracking (ADR-0011) --------------------------------------------


class VVStatusBadgeResponse(_Frozen):
    """The display badge for one tool version's V&V state (inheritance-aware).

    Mirror of :class:`matofx.vv.VVStatusBadge`. Surfaced by every UI
    consumer that wants to render a V&V indicator (submit-form header,
    Compare column header, lineage node, KPI filter, per-job report).
    ``contributing_version_ids`` lists every tool version in the cohort
    that carries at least one approved record — the UI uses this to
    render "Inherited from v1.0.0" provenance tooltips.
    """

    tool_version_id: int
    kind: Literal["verified_validated", "verified_only", "pending", "none", "retired"]
    has_approved_verification: bool
    has_approved_validation: bool
    has_pending: bool
    latest_envelope: dict[str, dict[str, float]] | None
    cohort_bundle_hash: str
    contributing_version_ids: list[int]
    inherited: bool


class VVEnvelopeWarningResponse(_Frozen):
    """A soft warning that submitted inputs fall outside the V&V envelope.

    Mirror of :class:`matofx.vv.EnvelopeWarning`. The submit surface
    renders this as a banner; the gate never blocks on envelope drift
    (the per-project ``vv_required_for_submit`` policy is the blocking
    mechanism — design §6.4).
    """

    tool_version_id: int
    out_of_range: list[dict[str, float | str]]
    """Per-violation: ``{"factor": str, "value": float, "envelope_min":
    float, "envelope_max": float}``."""


class VVEnvelopeCheckRequest(_Frozen):
    """Request body for ``POST /api/vv/envelope-check`` (design §6.5)."""

    tool_version_id: int
    form_values: dict[str, float]
    """``{factor_name: value}`` — the form's current numeric inputs."""


class VVEvidenceItemInput(_Frozen):
    """One evidence entry supplied on create / edit.

    ``kind`` is ``library`` (a governed File Library version, via
    ``file_version_id``) or ``url`` (an external link, via ``url``). A record
    may carry several, of mixed kinds.
    """

    kind: Literal["library", "url"]
    file_version_id: int | None = None
    url: str | None = None

    @model_validator(mode="after")
    def _check(self) -> VVEvidenceItemInput:
        if self.kind == "library" and self.file_version_id is None:
            raise ValueError("A 'library' evidence item needs a file_version_id.")
        if self.kind == "url" and not (self.url or "").strip():
            raise ValueError("A 'url' evidence item needs a url.")
        return self


class VVEvidenceItem(_Frozen):
    """One resolved evidence entry returned to the UI (with a display label)."""

    kind: Literal["library", "url"]
    file_version_id: int | None = None
    url: str | None = None
    label: str


class ToolVVRecordResponse(_Frozen):
    """One tool-version V&V record (design §2.1)."""

    id: int
    tool_version_id: int
    kind: Literal["verification", "validation"]
    state: Literal["draft", "pending", "approved", "rejected", "retired"]
    evidence_file_version_id: int | None
    evidence_url: str | None
    applicable_range: dict[str, dict[str, float]]
    confidence_band: float | None
    standards_cited: list[str]
    notes: str | None
    recorded_by_user_id: int
    recorded_at: _dt.datetime
    decided_by_user_id: int | None
    decided_at: _dt.datetime | None
    decision_reason: str | None
    row_version: int = Field(alias="version")
    # Resolved human labels — populated on the single-record GET so the
    # detail page never shows a raw id; None on the mutating responses.
    target_label: str | None = None
    recorded_by_label: str | None = None
    decided_by_label: str | None = None
    evidence_label: str | None = None
    evidence_items: list[VVEvidenceItem] = Field(default_factory=list)
    """The full evidence list (populated on the single-record GET)."""


class JobVVRecordResponse(_Frozen):
    """One job-run V&V record (design §2.2)."""

    id: int
    job_id: int
    kind: Literal["validated_against", "calibrated_from", "sanity_checked"]
    state: Literal["draft", "pending", "approved", "rejected", "retired"]
    evidence_file_version_id: int | None
    evidence_url: str | None
    notes: str | None
    recorded_by_user_id: int
    recorded_at: _dt.datetime
    decided_by_user_id: int | None
    decided_at: _dt.datetime | None
    decision_reason: str | None
    row_version: int = Field(alias="version")
    # Resolved human labels — populated on the single-record GET only.
    target_label: str | None = None
    recorded_by_label: str | None = None
    decided_by_label: str | None = None
    evidence_label: str | None = None
    evidence_items: list[VVEvidenceItem] = Field(default_factory=list)
    """The full evidence list (populated on the single-record GET)."""


class ToolVVRecordCreateRequest(_Frozen):
    """Author a draft V&V claim about a tool version (design §2.1)."""

    tool_version_id: int
    kind: Literal["verification", "validation"]
    applicable_range: dict[str, dict[str, float]] | None = None
    confidence_band: float | None = None
    standards_cited: list[str] | None = None
    evidence_file_version_id: int | None = None
    evidence_url: str | None = None
    evidence: list[VVEvidenceItemInput] | None = None
    """Full mixed evidence list; when given it supersedes the legacy single
    ``evidence_*`` fields above."""
    notes: str | None = None


class ToolVVRecordPatchRequest(_Frozen):
    """Edit a draft tool V&V record. Optional fields use None = no change."""

    notes: str | None = None
    applicable_range: dict[str, dict[str, float]] | None = None
    confidence_band: float | None = None
    standards_cited: list[str] | None = None
    evidence_file_version_id: int | None = None
    evidence_url: str | None = None
    evidence: list[VVEvidenceItemInput] | None = None


class VVNumericFactorResponse(_Frozen):
    """One numeric input a tool version exposes, for the range editor.

    Sourced from the tool version's descriptor snapshot so the applicable-
    range editor offers the exact factors (by label) that the submit-time
    envelope check compares against (by ``name``). ``minimum`` / ``maximum``
    are the descriptor's own declared bounds — handy defaults/hints.
    """

    name: str
    label: str
    unit: str | None = None
    minimum: float | None = None
    maximum: float | None = None


class VVMigrateToolRecordsRequest(_Frozen):
    """Copy approved V&V records from one tool version to another (design §13)."""

    source_tool_version_id: int
    target_tool_version_id: int
    record_ids: list[int] = Field(min_length=1)
    """The specific source records to bring forward — approved-only,
    chosen individually by the curator. Empty is rejected."""


class VVMigrateSkip(_Frozen):
    """One source record that couldn't be migrated, with a plain reason."""

    source_record_id: int
    reason: str


class VVMigrateToolRecordsResponse(_Frozen):
    """Outcome of a migrate call — the new drafts plus any skipped sources."""

    migrated: list[ToolVVRecordResponse]
    skipped: list[VVMigrateSkip] = Field(default_factory=list)


class JobVVRecordCreateRequest(_Frozen):
    """Author a draft V&V claim about a single job run (design §2.2)."""

    job_id: int
    kind: Literal["validated_against", "calibrated_from", "sanity_checked"]
    evidence_file_version_id: int | None = None
    evidence_url: str | None = None
    evidence: list[VVEvidenceItemInput] | None = None
    """Full mixed evidence list; supersedes the legacy single fields."""
    notes: str | None = None


class JobVVRecordPatchRequest(_Frozen):
    """Edit a draft job V&V record. Optional fields use None = no change."""

    notes: str | None = None
    evidence_file_version_id: int | None = None
    evidence_url: str | None = None
    evidence: list[VVEvidenceItemInput] | None = None


class VVSubmitRequest(_Frozen):
    """Submit a V&V draft for review.

    With the ``vv_record_approval`` toggle ON (default), both
    ``designated_approver_user_id`` and ``designated_approver_username``
    must be set to a curator other than the author. With it OFF, both
    may be omitted — the toggle resolution lives in the router so the
    service stays oblivious to General Settings.
    """

    designated_approver_user_id: int | None = None
    designated_approver_username: str | None = None


class VVDecideRequest(_Frozen):
    """Approve / reject a pending V&V record. Reason required on reject."""

    reason: str | None = None


class VVPendingRecordResponse(_Frozen):
    """One entry in the curator's pending-review inbox (design §6)."""

    record_id: int
    record_kind: Literal["tool", "job"]
    vv_kind: str
    recorded_by_user_id: int
    recorded_at: _dt.datetime
    target_id: int


class VVCuratorOption(_Frozen):
    """One eligible-curator entry surfaced for the approver picker."""

    user_id: int
    username: str


class VVRecordSummaryEntry(_Frozen):
    """One row of the cross-install V&V register (``GET /api/vv/records``).

    Polymorphic over both tables via ``record_kind``. Every id the register
    would otherwise show raw is resolved to a human label server-side:
    ``target_label`` (``"Bracket Stress v3"`` for a tool version, the job
    name for a job), ``recorded_by_label`` / ``decided_by_label``
    (``"Full Name (username)"``), and ``evidence_label`` (the library file
    name or the external URL). ``target_id`` is still returned so the UI can
    deep-link and, for jobs, prefix the formatted job id.
    """

    record_id: int
    record_kind: Literal["tool", "job"]
    vv_kind: str
    vv_kind_label: str
    state: str
    target_id: int
    target_label: str
    recorded_by_user_id: int
    recorded_by_label: str
    recorded_at: _dt.datetime
    decided_by_label: str | None
    decided_at: _dt.datetime | None
    evidence_label: str | None
    """Short summary for the row's attach-icon tooltip (first item, or a
    count when there are several)."""
    evidence_items: list[VVEvidenceItem] = Field(default_factory=list)
    standards_cited: list[str]
    notes: str | None


class VVRecordListPage(_Frozen):
    """A page of the cross-install V&V register."""

    rows: list[VVRecordSummaryEntry]
    total: int
    page: int
    page_size: int


# --- KPI extraction (ADR-0012) -----------------------------------------


class KpiCatalogEntryResponse(_Frozen):
    """One declared KPI surfaced by ``GET /api/tools/{alias}/kpis``."""

    kpi_name: str
    kpi_label: str | None = None
    kpi_unit: str | None = None
    kpi_category: str | None = None


class ToolKpiCatalogResponse(_Frozen):
    """Tool-scope catalog wrapper for the warehouse column metadata."""

    tool_alias: str
    tool_version_id: int | None = None
    entries: list[KpiCatalogEntryResponse]


class WarehouseRowResponse(_Frozen):
    """One job's identity columns + KPI values for the warehouse table."""

    job_id: int
    project_id: int
    scenario_id: int | None = None
    parametric_study_id: int | None = None
    requestor_user_id: int
    completed_at: _dt.datetime | None = None
    tool_version_id: int
    kpi_values: dict[str, float | None]
    kpi_units: dict[str, str | None]
    vv_status: str = "none"
    """ADR-0011 V&V badge kind: ``verified_validated`` /
    ``verified_only`` / ``pending`` / ``none`` / ``retired``."""
    project_name: str | None = None
    scenario_name: str | None = None
    requestor_name: str | None = None
    tool_version_label: str | None = None
    """Display labels for the identity columns (feedback_use_labels_not_names);
    ``None`` when unresolved — the UI falls back to the id."""


class WarehousePageResponse(_Frozen):
    """Paginated warehouse-page response wrapper."""

    rows: list[WarehouseRowResponse]
    page: int
    page_size: int
    total: int
    hidden_by_acl: int


class TimeseriesPointResponse(_Frozen):
    """One ``(extracted_at, value, job_id)`` point for the trend chart."""

    extracted_at: _dt.datetime
    value_numeric: float | None
    job_id: int


class TimeseriesResponse(_Frozen):
    """Time-series wrapper — the points in ascending ``extracted_at`` order."""

    tool_alias: str
    kpi_name: str
    points: list[TimeseriesPointResponse]


class AdminActiveSessionRow(_Frozen):
    """One live session on the Active sessions page (ADR-0019 slice 4)."""

    id: int
    user_id: int
    username: str
    full_name: str
    role: str
    ip_address: str | None = None
    started_at: _dt.datetime
    last_activity_at: _dt.datetime
    idle_minutes: int
    is_you: bool = False
    """The caller's own session — the page must not offer to sign you
    out of the page you are using."""

    breakglass: bool = False
    """Admitted *over* the licence cap as a Super User (§C13).

    Surfaced because this page exists to get a site back under its cap,
    and these are the sessions that put it over. They also expire sooner
    than the site policy allows."""


class AdminActiveSessionsResponse(_Frozen):
    """Live sessions + the licence capacity they are measured against."""

    sessions: list[AdminActiveSessionRow]
    licence_type: str | None = None
    n_slots: int | None = None
    in_use: int
    over_cap: bool = False
    metered: bool = False
    """False when no CSL licence applies (or the site is read-only), in
    which case the counts are informational rather than a cap."""


class AdminLicenceCapacityResponse(_Frozen):
    """Floating-assignment capacity for the Users page (ADR-0019 slice 5)."""

    metered: bool = False
    """False unless a Floating Access Licence is actually in force."""

    licence_type: str | None = None
    n_slots: int | None = None
    in_use: int = 0
    remaining: int | None = None


class SiteModeResponse(_Frozen):
    """Whether the site is writable, and why not (ADR-0019 slice 6)."""

    read_only: bool = False
    reason: str | None = None
    """``licence`` / ``licence_invalid`` / ``integrity`` / ``None``.

    ``licence_invalid`` is held apart from ``licence`` because the two
    need different actions: a lapsed licence is renewed, a licence that
    does not verify is *restored*. Reporting both as "licence" sent an
    administrator to buy a renewal for a file somebody had edited."""

    banner: str | None = None
    licence_state: str | None = None
    """``active`` / ``grace`` / ``expired`` / ``None`` when unusable."""

    integrity_ok: bool = True
    integrity_acknowledged: bool = False
    can_acknowledge: bool = False
    """True only for a Super User facing an unacknowledged failure."""

    # ---- compliance-report reminder ---------------------------------
    #
    # Carried here rather than on an endpoint of its own because the
    # chrome already fetches this once per page render, and it is
    # already per-user. A second call would double the cost of drawing
    # every page to deliver one banner.
    compliance_due: bool = False
    """True only for a user who may actually generate the report
    (``audit.read``). Everyone else gets False and no banner: a reminder
    aimed at someone who cannot act on it is just noise."""

    compliance_banner: str | None = None
    compliance_last_generated_at: _dt.datetime | None = None
    compliance_due_at: _dt.datetime | None = None


class IntegrityAcknowledgeRequest(_Frozen):
    """Super User acknowledgement of a failed chain check."""

    note: str = Field(min_length=10, max_length=1000)
    """Why it is being cleared. Required, and long enough to be a
    sentence: this lands in the compliance report, where "ok" six months
    later tells the reader nothing."""


# ---------- compliance reports (ADR-0019 §B7) -----------------------------


class ComplianceReportRow(_Frozen):
    """One archived report, as the admin list shows it."""

    counter: int
    generated_at: str
    period_from: str
    period_to: str
    chain_ok: bool
    licence_type: str | None = None
    content_sha256: str
    generated_by: str
    tree_size: int = 0


class ComplianceReportListResponse(_Frozen):
    """Every report this install has generated, newest first."""

    reports: list[ComplianceReportRow]
    install_id: str | None = None
    customer: str | None = None
    can_generate: bool = False
    """False when no report signing key is available — the page explains
    what to do rather than offering a button that always fails."""
    blocked_reason: str | None = None

    # The same reporting-schedule answer the chrome banner draws from,
    # computed by the same function. A page that says "not due yet"
    # under a banner saying "overdue" is worse than no reminder at all.
    reporting_due: bool = False
    reporting_due_at: _dt.datetime | None = None
    reporting_cadence_months: int = 0
    """0 when the licence turns the reminder off, or when there is no
    readable licence to take a cadence from."""


class ComplianceReportGenerateRequest(_Frozen):
    """Optional window override, mirroring the CLI's ``--since``."""

    since: str | None = None
    """ISO-8601 date or timestamp. Absent means "since the last report"."""


class ComplianceReportGeneratedResponse(_Frozen):
    """What the page needs after generating, without re-fetching."""

    counter: int
    content_sha256: str
    chain_ok: bool
    generated_at: str
    warning: str | None = None
    """Present when the report itself records a problem — a failed chain
    check, most importantly. The report is still produced and still
    valid; ADR §B9 requires that. The caller must not treat this as an
    error, but must not hide it either."""
