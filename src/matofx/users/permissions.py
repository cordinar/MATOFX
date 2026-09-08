"""Permission catalog and runtime check.

Permissions are the access-control gate points the codebase consults.
Each permission is a stable slug (``"projects.see_all"``,
``"tools.install"``, ...). The catalog defined here is the **closed
set** of slugs the application can enforce — adding a new permission
is a code change because a permission that no code line checks can't
gate anything.

Roles, by contrast, are runtime data. Admins create roles, assign
permissions to them, and assign roles to users — all without touching
the codebase. The four built-in roles are seeded with sensible
defaults; their *names* are immutable (the codebase references them),
but their permission sets (except ``super_user``) can be tuned by any
admin holding ``roles.manage``.

Adding a new permission slug:

1. Add a new ``Permission`` member below.
2. Update :data:`SUPER_USER_PERMISSIONS` (always grants everything).
3. Decide whether the seeded default for ``administrator`` /
   ``beta_tester`` / ``regular_user`` should include it; update the
   relevant ``*_DEFAULT_PERMISSIONS`` set.
4. Ship a migration that adds the new permission row to roles whose
   defaults include it.
5. Add the actual ``user_has_permission`` check at the gate point.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Final

from sqlalchemy import or_, select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class Permission(StrEnum):
    """Closed catalog of permission slugs the application enforces."""

    PROJECTS_SEE_ALL = "projects.see_all"
    """See projects owned by anyone (not just self)."""

    PROJECTS_CREATE_FOR_OTHERS = "projects.create_for_others"
    """Create a project owned by another user (reassign ownership at create time)."""

    PROJECTS_TRANSFER_OWNERSHIP = "projects.transfer_ownership"
    """Transfer an existing project to a different owner. Owners can
    always transfer their own project to anyone (no permission needed);
    this gate covers admin-driven transfer of someone else's project."""

    PROJECTS_MANAGE_METADATA = "projects.manage_metadata"
    """Administer the project metadata taxonomy (ADR-0002): create /
    edit / retire metadata fields and their allowed values in
    ``Admin → Project Metadata``. Selecting metadata *on* a project is
    a normal project-OWNER action and needs no extra permission; this
    slug gates only the install-wide taxonomy definition. Granted by
    default to ``administrator`` and to ``super_user`` via the catalog
    sentinel."""

    TOOLS_INSTALL = "tools.install"
    """Install / uninstall tools."""

    TOOLS_PROMOTE_STATE = "tools.promote_state"
    """Move a tool through the New / Test / Released states."""

    TOOLS_SEE_NEW_STATE = "tools.see_new_state"
    """See tools in the New (author-only / privileged) state."""

    TOOLS_SEE_TEST_STATE = "tools.see_test_state"
    """See tools in the Test (pre-release / beta) state."""

    USERS_CREATE_WITH_ROLE = "users.create_with_role"
    """Create a user account and assign it any role (subject to invariants)."""

    USERS_MANAGE = "users.manage"
    """Deactivate / reactivate users; set the approver flag; tune
    user-level attributes that don't change the role assignment."""

    ROLES_MANAGE = "roles.manage"
    """Create, edit, and delete custom roles; tune built-in role permission sets."""

    TEAMS_MANAGE = "teams.manage"
    """Create / delete teams; add or remove users from teams."""

    RUNTIME_SETTINGS_MANAGE = "runtime_settings.manage"
    """Read/write the admin-tunable runtime knobs (concurrency, intervals, grace)."""

    AOS_HOSTS_MANAGE = "aos_hosts.manage"
    """Register / edit / deactivate always-on-server entries."""

    HPC_CLUSTERS_MANAGE = "hpc_clusters.manage"
    """Register / edit / deactivate HPC head-node entries."""

    STORAGE_LOCATIONS_MANAGE = "storage_locations.manage"
    """Register / edit / retire shared mount locations."""

    PYTHON_ENVS_MANAGE = "python_envs.manage"
    """Register / edit / deactivate Python interpreter entries tools may declare."""

    JOBS_CANCEL_ANY = "jobs.cancel_any"
    """Cancel any job regardless of project access. Sentinel-managed
    (super_user only); other roles cancel only via the project-access
    ladder (owner / admin grant / team grant)."""

    JOBS_SKIP_QUEUE = "jobs.skip_queue"
    """Push a job to the front of the runner's pickup queue.

    Holder sees a "Skip Queue" button next to "Submit Job" on the
    workspace submit form. Clicking it sets ``job.priority=True``,
    which makes the runner pickup loop drain the row ahead of every
    ``priority=False`` job (ordered by ``priority DESC, created_at
    ASC``). Used for urgent runs (hot-fix retest, customer demo, etc.).
    Granted by default to ``administrator`` and to ``super_user`` via
    the catalog sentinel."""

    LIVE_JOBS_VIEW = "live_jobs.view"
    """Open the **Live Jobs** tab on ``Admin → Usage & Statistics`` and
    see every in-flight item across the whole install — standalone jobs,
    workflow runs, and parametric studies that are queued, running, or
    waiting at an approval gate — regardless of project access.

    Read-only on its own: cancelling from the surface additionally
    requires :attr:`LIVE_JOBS_CANCEL`. Held separately from
    :attr:`USAGE_VIEW` (the aggregate analytics tabs) so the live
    operational list — which surfaces individual records, not
    aggregates — is gated to operators, not to a read-only "manager"
    analytics role. Granted by default to ``administrator`` and to
    ``super_user`` via the catalog sentinel."""

    LINEAGE_VIEW = "lineage.view"
    """Open the lineage / provenance graph for any object the holder can
    already see (ADR-0010).

    The graph is read-only and assembled from existing FK + per-field
    provenance rows (``job_library_file_use``, ``job_material_use``,
    ``approval_request``, study/workflow/tool FKs on ``Job``, etc.).
    Per-node ACL still applies — a holder of this permission only sees
    nodes in the graph they could already see through the owning
    domain's service (project ACL for jobs/scenarios/studies, library
    scope visibility for library files, etc.). Hidden nodes drop from
    the rendered graph; the incident edge carries a "(restricted)"
    badge and a ``truncated_count`` so compliance audits can tell when
    a graph was filtered.

    Granted by default to ``administrator`` and to ``super_user`` via
    the catalog sentinel. Custom roles can grant it to any user via the
    standard role editor (most installs grant it broadly — compliance
    and audit personas need it). Sits under the **Admin → Lineage**
    nav entry when the role also has admin presence; non-admin holders
    reach lineage via the inline "Provenance" tab on each host page
    (job / workflow run / study / library / material) without needing
    admin nav membership."""

    LIVE_JOBS_CANCEL = "live_jobs.cancel"
    """Cancel in-flight jobs, workflow runs, and parametric studies from
    the **Live Jobs** tab, install-wide and regardless of project
    access. Cancelling a workflow run or study cascades to its in-flight
    child jobs (the existing per-item cancel flow). Requires
    :attr:`LIVE_JOBS_VIEW` to reach the surface at all.

    Held by ``super_user`` via the catalog sentinel; **not** in the
    ``administrator`` default set. A super-user grants it per role in
    ``Teams, roles & permissions`` before an administrator can
    cancel from this surface — without it the cancel controls stay
    hidden and the cancel endpoint returns 403. Distinct from
    :attr:`JOBS_CANCEL_ANY` (which covers single-job cancellation
    elsewhere); this slug authorises the bulk Live-Jobs surface across
    all three item kinds."""

    DATABASE_EXPLORE = "database.explore"
    """Open the Admin → Explore Database page.

    Lets the holder browse a curated allowlist of tables (sensitive
    columns are redacted), the per-user activity tab, and the runtime
    diagnostics tab. Granted by default to ``administrator`` and every
    permission of ``super_user`` implicitly."""

    AUDIT_READ = "audit.read"
    """Open the Admin → Audit log page and run chain verification.

    Lets the holder browse the hash-chained, signed ``audit_log``
    (filter by actor / action / resource / time, page through results,
    expand a row to inspect payload + chain links) and trigger the same
    integrity check as ``matofx audit verify`` on the CLI. Read-only —
    the audit log itself is append-only and cannot be edited from any
    UI surface. Granted by default to ``administrator`` and to
    ``super_user`` via the catalog sentinel.

    Held separately from ``database.explore`` so a compliance-focused
    role can be granted audit-log access without unlocking the wider
    database-browser surface (and vice-versa)."""

    USAGE_VIEW = "usage.view"
    """Open ``Admin → Usage Statistics`` and read the install's usage
    analytics: job throughput, compute consumption (core/GPU-hours),
    reliability (success rates / failures), adoption (active users /
    concurrency), seat / licence usage, and the infrastructure mix.

    Read-only and aggregate-only — the surface summarises the ``job``,
    ``user``, ``auth_session`` and ``tool`` tables; it never lists
    individual audit records (that is :attr:`AUDIT_READ`) or exposes the
    raw table browser (that is :attr:`DATABASE_EXPLORE`). Held
    separately from both so a non-technical "manager" role can be given
    usage analytics without unlocking the audit log or the database
    explorer. Granted by default to ``administrator`` and to
    ``super_user`` via the catalog sentinel."""

    TUTORIALS_MANAGE = "tutorials.manage"
    """Upload, activate, or delete tutorials (PDFs / videos) attached
    to a tool or surfaced as general guides on the home page. Granted
    by default to ``administrator`` and to ``super_user`` via the
    catalog sentinel."""

    WORKFLOWS_MANAGE = "workflows.manage"
    """Create, edit, promote-state, or archive admin-released
    workflows. Workflows are ordered groupings of tools surfaced as
    tabbed runs on a scenario; output→input wiring is auto-discovered
    from each member tool's descriptor. Granted by default to
    ``administrator`` and to ``super_user`` via the catalog sentinel.

    Note this is separate from running a workflow — running follows
    the same access ladder as a tool job (project-access + the
    workflow's own state/team visibility)."""

    CUSTOM_WORKFLOWS_CREATE = "custom_workflows.create"
    """Create user-owned custom workflows on ``/my-workflows``.

    A custom workflow is a private 2-10 tool grouping the user picks
    from their accessible tools and can optionally share with named
    users. The owner can edit, share, archive, or duplicate their own
    rows; share recipients get view + run only. Distinct from
    ``workflows.manage`` (admin-released workflows). Granted by default
    to ``administrator``, ``beta_tester``, ``regular_user``, and to
    ``super_user`` via the catalog sentinel."""

    CUSTOM_WORKFLOWS_INSPECT = "custom_workflows.inspect"
    """Open ``/admin/custom-workflows`` and view every user's custom
    workflows for troubleshooting.

    Holder sees the workflow's owner, member tools, share list,
    version history, and recent runs. May archive (with a written
    reason, audit-logged) and may duplicate a workflow into their own
    list to reproduce a bug. **Never** promotes other users' workflows
    into the holder's personal picker. Independent of
    ``workflows.manage``: managing released workflows is not authority
    to read every user's private workflow. Granted by default to
    ``administrator`` and to ``super_user`` via the catalog sentinel."""

    MIGRATE_TOOLS_WORKFLOWS = "migrate.tools_workflows"
    """Open ``Admin → Migrate Tools/Workflows`` and use the full
    cross-install migration surface: export a signed bundle of
    admin-released tools and admin-released workflows on this install,
    import a bundle from a trusted source install, and manage the
    trusted-peer list.

    Granted by default to ``super_user`` (via the catalog sentinel).
    **Not** in the administrator default set: a super-user must grant
    it case-by-case through the standard role-edit UI before an
    administrator can perform cross-install migration. Reason: a
    cross-install operation crosses the security boundary between two
    MATOFX installs (typically Staging → Prod) and is intentionally
    held to a higher bar than ordinary admin-of-this-install work.

    The whole surface is also gated behind the
    ``features.cross_install_migration`` licence flag — customers
    without a Staging tier never see it.

    Imports preserve source-side tool settings (licence, AOS, HPC,
    Python env, storage, resources) by resolving every cross-install
    reference by stable string identifier on the destination; tool /
    workflow lifecycle state is always reset to ``new`` on import; the
    per-bundle behaviour is *per-item atomicity* — items that can be
    imported are, the rest are skipped with a plain-English reason.
    Full rules: PROJECT_BRIEF.md §Q5 (2026-05-15 amendment); operator
    walkthrough: MIGRATE_GUIDE.md."""

    PARAMETRIC_STUDIES_CREATE = "parametric_studies.create"
    """Create parametric studies on the Parametric Studies surface
    (§G39a / ADR-0001 / design §12).

    A parametric study fans one tool over a generated input matrix.
    Default-on for ``regular_user`` (same ladder as
    :attr:`CUSTOM_WORKFLOWS_CREATE`), ``beta_tester``, and
    ``administrator``; ``super_user`` holds it via the catalog
    sentinel. Creating a study additionally requires the user to
    already be authorised to run the underlying tool (anti-escalation,
    §G35 Q2, re-checked at submit). The whole surface is also gated
    behind the ``features.parametric_studies`` licence flag."""

    OPTIMISATION_CREATE = "optimisation.create"
    """Create optimisation studies on the Optimisation surface.

    Base-tier sibling of :attr:`PARAMETRIC_STUDIES_CREATE`. Gates both
    the sidebar item and the create-study action; ``optimisation.approve``
    (capability-tier) is the separate sign-off for starting a flagged
    campaign. Default-granted to every base role (regular / beta /
    administrator); ``super_user`` holds it via the catalog sentinel.
    Also gated behind ``features.optimisation_framework``."""

    MODELS_CREATE = "models.create"
    """Create / fit surrogate models on the Surrogate Models surface.

    Base-tier create gate; the separate ``models.curate`` capability is
    the technical-checker sign-off for *global* publishes. Default-granted
    to every base role (regular / beta / administrator); ``super_user`` via
    the sentinel. Gated behind ``features.surrogate_family``."""

    COHORTS_CREATE = "cohorts.create"
    """Create Job Cohorts on the Job Cohorts surface.

    Base-tier create gate; the separate ``cohorts.curate`` capability
    governs editing and archiving cohorts. Default-granted to every base
    role (regular / beta / administrator); ``super_user`` via the sentinel.
    Gated behind ``features.surrogate_family``. Viewing cohorts stays open
    to everyone."""

    VV_CREATE = "vv.create"
    """Author / edit / submit draft V&V records on the V&V surface.

    Base-tier create gate for V&V record authoring; the separate
    ``vv.curate`` capability is the approve / promote / retire authority
    (the directed-approver pool). Default-granted to every base role
    (regular / beta / administrator); ``super_user`` via the sentinel.
    Gated behind ``features.vv_tracking``."""

    LIBRARY_CREATE = "library.create"
    """Add / version files in the File Library (contributor surface).

    Base-tier create gate; the separate ``library.curate`` capability is
    the governance authority (assign-to-tool, retire, global publish,
    directed-approver pool). Default-granted to every base role (regular /
    beta / administrator); ``super_user`` via the sentinel. Gated behind
    ``features.file_library``."""

    MATERIALS_CREATE = "materials.create"
    """Add / version materials in the Materials database (contributor surface).

    Base-tier create gate; the separate ``materials.curate`` capability is
    the governance authority (retire, restore, class management,
    directed-approver pool). Default-granted to every base role (regular /
    beta / administrator); ``super_user`` via the sentinel. Gated behind
    ``features.materials_database``."""

    NOTIFICATIONS_SEND = "notifications.send"
    """Open ``Admin → Send Notifications`` and broadcast an email to
    every registered user (or only those whose access is currently
    enabled), plus manage the reusable header/footer template applied
    to every broadcast.

    Distinct from the automated job-state notifications, which need no
    permission (they fire from the runner). This slug gates the
    *admin-initiated* broadcast surface only. A broadcast can reach
    every user's inbox at once, so it is held to an explicit
    permission rather than folded into a broader admin slug. Granted
    by default to ``administrator`` and to ``super_user`` via the
    catalog sentinel."""

    LIBRARY_CURATE = "library.curate"
    """Curate the governed file library (ADR-0006): add, version,
    assign, and retire reusable tool-input files, and stand in the
    directed-approver pool for global-tier publications.

    Carried by the ``library_curator`` **capability** role (ADR-0005),
    stacked on top of a user's base role — never granted to any base
    role by default, so a subject-matter steward curates files without
    gaining any hierarchy or user-management power. ``super_user``
    holds it via the catalog sentinel. The file-library feature gate
    points that consult this slug land with ADR-0006."""

    MATERIALS_CURATE = "materials.curate"
    """Curate the governed materials properties database (ADR-0007):
    add, version, and retire materials, and stand in the directed-
    approver pool for material changes.

    Carried by the ``materials_curator`` **capability** role
    (ADR-0005), stacked on top of a user's base role — never granted to
    any base role by default, so a materials engineer curates material
    data without gaining any hierarchy or user-management power.
    ``super_user`` holds it via the catalog sentinel. The materials
    feature gate points that consult this slug land with ADR-0007."""

    VV_CURATE = "vv.curate"
    """Curate the V&V (Verification & Validation) record ledger
    (ADR-0011): promote draft V&V claims to ``approved`` on tools,
    jobs, and surrogate models, and stand in the directed-approver
    pool for V&V record review.

    Carried by the ``vv_curator`` **capability** role (ADR-0005),
    stacked on top of a user's base role — never granted to any base
    role by default, so a domain expert curating V&V records gains no
    hierarchy or user-management power. ``super_user`` holds it via the
    catalog sentinel. Granted by default to ``administrator`` so an
    existing admin install picks up the surface without manual fiddling.
    The V&V feature gate points that consult this slug land with
    ADR-0011 / ``designs/vv-tracking.md`` §6."""

    OPTIMISATION_APPROVE = "optimisation.approve"
    """Approve the start of an optimisation campaign (ADR-0009 §14.1).

    Carried by the ``optimisation_approver`` **capability** role (ADR-0005),
    stacked on a user's base role — the directed-approver pool that signs
    off the compute footprint before a study whose target tool has
    ``optimisation_start_approval_required`` queues its (potentially
    hundreds of) children. Never granted to a base role by default;
    ``super_user`` holds it via the catalog sentinel; granted to
    ``administrator`` by default. The optimisation feature gate consults
    this slug; lands with ADR-0009 Phase 13."""

    MODELS_CURATE = "models.curate"
    """Act as the technical checker for global surrogate-model publishes
    (ADR-0008 §5.7): stand in the directed-approver pool that signs off a
    surrogate model version before a super-user releases it to general
    use.

    Carried by the ``model_curator`` **capability** role (ADR-0005),
    stacked on top of a user's base role — install-global (per-tool
    curation is v1.x), never granted to any base role by default, so a
    domain expert checks surrogate models without gaining hierarchy or
    user-management power. ``super_user`` holds it via the catalog
    sentinel. Granted by default to ``administrator``. Release authority
    is *separate* (a su/admin action, not this permission). The
    surrogate-family feature gate points that consult this slug land with
    ADR-0008."""

    COHORTS_CURATE = "cohorts.curate"
    """Create, edit, and archive **Job Cohorts** — the saved-query batches
    of completed runs of one tool that feed a surrogate fit
    (``designs/job-cohorts.md`` D6).

    Carried by the ``cohort_curator`` **capability** role (ADR-0005),
    stacked on a user's base role — install-global, never granted to a
    base role by default, so a domain expert can curate cohorts without
    gaining hierarchy or user-management power. ``super_user`` holds it
    via the catalog sentinel; granted by default to ``administrator``.
    **Reads are open** to any authenticated user — viewing a cohort and
    its results needs no capability (a cohort's member results stay
    intersected with the caller's per-project access regardless, D7); the
    capability gates *writes* only."""


# Concise, plain-English summaries surfaced as helper text beneath each
# permission's checkbox in the admin permission editor. The rich
# docstrings on the Permission members above are developer-facing detail
# and are *not* readable at runtime (Python doesn't store attribute
# docstrings on the object), so the UI cannot use them — these one-liners
# are the runtime source. Every member must have an entry; the
# completeness invariant is enforced by tests/users/test_roles.py.
PERMISSION_DESCRIPTIONS: Final[dict[Permission, str]] = {
    Permission.PROJECTS_SEE_ALL: "See projects owned by anyone, not just your own.",
    Permission.PROJECTS_CREATE_FOR_OTHERS: "Create a project on behalf of another user.",
    Permission.PROJECTS_TRANSFER_OWNERSHIP: (
        "Transfer someone else's project to a different owner (owners can always "
        "transfer their own project)."
    ),
    Permission.PROJECTS_MANAGE_METADATA: (
        "Administer the install-wide project metadata taxonomy: create, edit, and "
        "retire metadata fields and their allowed values."
    ),
    Permission.TOOLS_INSTALL: "Install and uninstall tools.",
    Permission.TOOLS_PROMOTE_STATE: "Move a tool through the New, Test, and Released states.",
    Permission.TOOLS_SEE_NEW_STATE: "See tools still in the New (author-only) state.",
    Permission.TOOLS_SEE_TEST_STATE: "See tools in the Test (pre-release / beta) state.",
    Permission.USERS_CREATE_WITH_ROLE: "Create user accounts and assign them a role.",
    Permission.USERS_MANAGE: (
        "Deactivate or reactivate users, set the tool-approver flag, and edit user "
        "attributes that don't change their role."
    ),
    Permission.ROLES_MANAGE: (
        "Create, edit, and delete access-rights groups and tune the built-in "
        "groups' permission sets."
    ),
    Permission.TEAMS_MANAGE: "Create or delete teams and add or remove their members.",
    Permission.RUNTIME_SETTINGS_MANAGE: (
        "Read and edit the runtime knobs: job concurrency, polling intervals, and grace periods."
    ),
    Permission.AOS_HOSTS_MANAGE: "Register, edit, and deactivate always-on-server hosts.",
    Permission.HPC_CLUSTERS_MANAGE: "Register, edit, and deactivate HPC head-node clusters.",
    Permission.STORAGE_LOCATIONS_MANAGE: "Register, edit, and retire shared storage locations.",
    Permission.PYTHON_ENVS_MANAGE: (
        "Register, edit, and deactivate the Python interpreters tools can declare."
    ),
    Permission.JOBS_CANCEL_ANY: "Cancel any job, regardless of who owns its project.",
    Permission.JOBS_SKIP_QUEUE: "Push a job to the front of the runner's pickup queue.",
    Permission.LIVE_JOBS_VIEW: (
        "Open the Live Jobs tab to see every in-flight job, workflow run, and parametric "
        "study across the install (queued, running, or awaiting approval) — read-only."
    ),
    Permission.LINEAGE_VIEW: (
        "Open the lineage / provenance graph for any object you can already see — "
        "shows how a job, workflow run, study, library file, or material was produced "
        "and what consumed it (read-only)."
    ),
    Permission.LIVE_JOBS_CANCEL: (
        "Cancel in-flight jobs, workflow runs, and parametric studies from the Live Jobs "
        "tab, install-wide and regardless of who owns the project."
    ),
    Permission.DATABASE_EXPLORE: (
        "Open Admin → Explore Database to browse curated tables, the activity log, "
        "and runtime diagnostics."
    ),
    Permission.AUDIT_READ: (
        "Open Admin → Audit Log and run hash-chain integrity verification (read-only)."
    ),
    Permission.USAGE_VIEW: (
        "Open Admin → Usage Statistics to read aggregate usage analytics: job "
        "throughput, compute consumption, reliability, adoption, and seat usage (read-only)."
    ),
    Permission.TUTORIALS_MANAGE: (
        "Upload, activate, and delete tutorials attached to tools or shown on the home page."
    ),
    Permission.WORKFLOWS_MANAGE: "Create, edit, promote, or archive admin-released workflows.",
    Permission.CUSTOM_WORKFLOWS_CREATE: (
        "Create your own custom workflows on the My Workflows page."
    ),
    Permission.CUSTOM_WORKFLOWS_INSPECT: (
        "Open Admin → Custom Workflows to view every user's custom workflows for troubleshooting."
    ),
    Permission.MIGRATE_TOOLS_WORKFLOWS: (
        "Open Admin → Migrate Tools/Workflows to export and import signed bundles "
        "between installs and manage the trusted-peer list."
    ),
    Permission.PARAMETRIC_STUDIES_CREATE: (
        "Create parametric studies that fan one tool over a generated matrix of inputs."
    ),
    Permission.OPTIMISATION_CREATE: (
        "Create optimisation studies on the Optimisation surface (separate from the "
        "Optimisation Approver capability, which signs off a campaign's start)."
    ),
    Permission.MODELS_CREATE: (
        "Create and fit surrogate models on the Surrogate Models surface (separate from "
        "the Model Curator capability, which checks global publishes)."
    ),
    Permission.COHORTS_CREATE: (
        "Create Job Cohorts (separate from the Cohort Curator capability, which edits "
        "and archives them). Viewing cohorts is open to everyone."
    ),
    Permission.VV_CREATE: (
        "Author, edit, and submit draft V&V records (separate from the V&V Curator "
        "capability, which approves, promotes, and retires them)."
    ),
    Permission.LIBRARY_CREATE: (
        "Add and version files in the File Library (separate from the Library Curator "
        "capability, which assigns to tools, retires, and approves global publishes)."
    ),
    Permission.MATERIALS_CREATE: (
        "Add and version materials in the Materials database (separate from the "
        "Materials Curator capability, which retires, restores, and manages classes)."
    ),
    Permission.NOTIFICATIONS_SEND: (
        "Open Admin → Send Notifications to broadcast an email to users and manage the "
        "broadcast template."
    ),
    Permission.LIBRARY_CURATE: (
        "Curate the governed file library: add, version, assign, and retire reusable "
        "tool-input files (held by the Library Curator capability role)."
    ),
    Permission.MATERIALS_CURATE: (
        "Curate the materials properties database: add, version, and retire materials "
        "(held by the Materials Curator capability role)."
    ),
    Permission.VV_CURATE: (
        "Curate the V&V record ledger: promote draft V&V claims to approved on tools, "
        "jobs, and surrogate models (held by the V&V Curator capability role)."
    ),
    Permission.MODELS_CURATE: (
        "Act as the technical checker for global surrogate-model publishes, before a "
        "super-user releases them to general use (held by the Model Curator capability role)."
    ),
    Permission.COHORTS_CURATE: (
        "Create, edit, and archive Job Cohorts — batches of completed runs of one tool "
        "used to fit a surrogate (held by the Cohort Curator capability role). Viewing "
        "cohorts is open to everyone."
    ),
    Permission.OPTIMISATION_APPROVE: (
        "Approve the start of an optimisation campaign before its runs queue, when the "
        "target tool requires sign-off (held by the Optimisation Approver capability role)."
    ),
}


# Sentinel: the full set of permissions, always granted to ``super_user``.
SUPER_USER_PERMISSIONS: Final[frozenset[Permission]] = frozenset(Permission)

# Default permission sets for the other three built-in roles. Seeded by
# migration 0006 and editable at runtime by admins (except super_user's,
# which is locked to ``SUPER_USER_PERMISSIONS``).
ADMINISTRATOR_DEFAULT_PERMISSIONS: Final[frozenset[Permission]] = frozenset(
    {
        Permission.PROJECTS_SEE_ALL,
        Permission.PROJECTS_MANAGE_METADATA,
        Permission.TOOLS_INSTALL,
        Permission.TOOLS_PROMOTE_STATE,
        Permission.TOOLS_SEE_NEW_STATE,
        Permission.TOOLS_SEE_TEST_STATE,
        Permission.USERS_CREATE_WITH_ROLE,
        Permission.USERS_MANAGE,
        Permission.ROLES_MANAGE,
        Permission.TEAMS_MANAGE,
        Permission.RUNTIME_SETTINGS_MANAGE,
        Permission.AOS_HOSTS_MANAGE,
        Permission.HPC_CLUSTERS_MANAGE,
        Permission.STORAGE_LOCATIONS_MANAGE,
        Permission.PYTHON_ENVS_MANAGE,
        Permission.DATABASE_EXPLORE,
        Permission.AUDIT_READ,
        Permission.USAGE_VIEW,
        Permission.TUTORIALS_MANAGE,
        Permission.WORKFLOWS_MANAGE,
        Permission.CUSTOM_WORKFLOWS_CREATE,
        Permission.CUSTOM_WORKFLOWS_INSPECT,
        Permission.PARAMETRIC_STUDIES_CREATE,
        Permission.OPTIMISATION_CREATE,
        Permission.MODELS_CREATE,
        Permission.COHORTS_CREATE,
        Permission.VV_CREATE,
        Permission.LIBRARY_CREATE,
        Permission.MATERIALS_CREATE,
        Permission.JOBS_SKIP_QUEUE,
        Permission.LIVE_JOBS_VIEW,
        Permission.LINEAGE_VIEW,
        Permission.NOTIFICATIONS_SEND,
    }
)

# The base-tier ``*.create`` grants are ordinary creation gates available to
# any base role (an admin tunes them per role). They ship default-on for
# regular and beta users alongside their siblings ``custom_workflows.create``
# and ``parametric_studies.create``, so the create surfaces stay open by
# default rather than being locked to administrators. Migration 0083 seeds
# these onto every base role to match (and to preserve access on upgrade).
_BASE_CREATE_PERMISSIONS: Final[frozenset[Permission]] = frozenset(
    {
        Permission.OPTIMISATION_CREATE,
        Permission.MODELS_CREATE,
        Permission.COHORTS_CREATE,
        Permission.VV_CREATE,
        Permission.LIBRARY_CREATE,
        Permission.MATERIALS_CREATE,
    }
)

BETA_TESTER_DEFAULT_PERMISSIONS: Final[frozenset[Permission]] = frozenset(
    {
        Permission.TOOLS_SEE_TEST_STATE,
        Permission.CUSTOM_WORKFLOWS_CREATE,
        Permission.PARAMETRIC_STUDIES_CREATE,
    }
    | _BASE_CREATE_PERMISSIONS
)

REGULAR_USER_DEFAULT_PERMISSIONS: Final[frozenset[Permission]] = frozenset(
    {
        Permission.CUSTOM_WORKFLOWS_CREATE,
        Permission.PARAMETRIC_STUDIES_CREATE,
    }
    | _BASE_CREATE_PERMISSIONS
)

CAPABILITY_PERMISSION_SUFFIX: Final[str] = ".curate"
"""Primary slug suffix marking a permission as **capability-tier** (ADR-0005).

See :data:`CAPABILITY_PERMISSION_SUFFIXES` for the full set."""

CAPABILITY_PERMISSION_SUFFIXES: Final[tuple[str, ...]] = (".curate", ".approve")
"""Slug suffixes marking a permission as **capability-tier**.

The role model splits a user's privileges into one ``base`` role plus
any stacked ``capability`` roles. By convention, a capability role
carries *only* governance-tier permissions — the curator ``*.curate``
grants (e.g. ``library.curate``) plus the directed-approver ``*.approve``
grant (``optimisation.approve``) — and a base role carries *only* the
rest, so the two tiers grant strictly non-overlapping permission sets and
stay easy to tell apart in the admin UI. Using a suffix convention means
any future curate-/approve-style slug is classified automatically, with
no second list to keep in sync. The split is enforced in
:meth:`matofx.users.role_service.RoleService.create_role` /
``set_permissions`` and surfaced (per-slug ``is_capability``) on the
permission catalog API."""


def is_capability_permission(permission: Permission | str) -> bool:
    """``True`` if ``permission`` is a capability-tier grant (ADR-0005).

    See :data:`CAPABILITY_PERMISSION_SUFFIXES`. Accepts a
    :class:`Permission` member or a raw slug.
    """
    slug = permission.value if isinstance(permission, Permission) else permission
    return slug.endswith(CAPABILITY_PERMISSION_SUFFIXES)


async def user_has_permission(
    session: AsyncSession,
    user_id: int,
    permission: Permission | str,
) -> bool:
    """``True`` when the user holds ``permission`` via any of their roles.

    Effective permissions are the **union** of the user's single base
    role (``User.role_id``) and every stackable capability role joined
    via ``user_role`` (ADR-0005). The ``super_user`` base role grants
    every permission in the catalog (sentinel short-circuit); for every
    other base role the check is a direct lookup across the base role
    plus the user's capability roles in the ``role_permission`` table.

    Unknown user ids and unassigned roles return ``False``.
    """
    # Lazy imports to avoid the user→role circular import at module load.
    from matofx.users.orm import User  # noqa: PLC0415
    from matofx.users.role_orm import (  # noqa: PLC0415
        Role,
        RolePermission,
        UserCapabilityRole,
    )

    user = await session.get(User, user_id)
    if user is None or user.role_id is None:
        return False

    base_role = await session.get(Role, user.role_id)
    if base_role is None:
        return False

    if base_role.name == "super_user":
        return _normalise(permission) in {p.value for p in Permission}

    slug = _normalise(permission)
    capability_role_ids = select(UserCapabilityRole.role_id).where(
        UserCapabilityRole.user_id == user_id
    )
    stmt = (
        select(RolePermission.role_id)
        .where(
            RolePermission.permission == slug,
            or_(
                RolePermission.role_id == base_role.id,
                RolePermission.role_id.in_(capability_role_ids),
            ),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


async def user_has_any_permission(
    session: AsyncSession,
    user_id: int,
    *permissions: Permission | str,
) -> bool:
    """``True`` when the user holds **any** of ``permissions``.

    Used by the create-vs-curate split: an authoring action is allowed to
    a base ``*.create`` holder *or* to a ``*.curate`` capability holder
    (curators can always create), so callers pass both and accept either.
    """
    for permission in permissions:
        if await user_has_permission(session, user_id, permission):
            return True
    return False


async def effective_permissions(session: AsyncSession, user_id: int) -> set[str]:
    """Return every permission slug ``user_id`` effectively holds.

    The union of the user's single base role and every stacked
    capability role (ADR-0005), matching :func:`user_has_permission`'s
    resolution. The ``super_user`` base role returns the full catalog.
    Unknown user ids and unassigned base roles return an empty set.

    Useful where a surface needs the *whole* set at once — e.g. the
    ``/auth/me`` payload, so the UI can hide controls the caller can't
    use rather than round-tripping a check per control.
    """
    from matofx.users.orm import User  # noqa: PLC0415
    from matofx.users.role_orm import (  # noqa: PLC0415
        Role,
        RolePermission,
        UserCapabilityRole,
    )

    user = await session.get(User, user_id)
    if user is None or user.role_id is None:
        return set()
    base_role = await session.get(Role, user.role_id)
    if base_role is None:
        return set()
    if base_role.name == "super_user":
        return {p.value for p in Permission}

    capability_role_ids = select(UserCapabilityRole.role_id).where(
        UserCapabilityRole.user_id == user_id
    )
    stmt = select(RolePermission.permission).where(
        or_(
            RolePermission.role_id == base_role.id,
            RolePermission.role_id.in_(capability_role_ids),
        )
    )
    return set((await session.execute(stmt)).scalars().all())


async def role_permissions(
    session: AsyncSession,
    role_id: int,
) -> set[str]:
    """Return the permission slugs granted by ``role_id`` (excluding super-user wildcard)."""
    from matofx.users.role_orm import Role, RolePermission  # noqa: PLC0415

    role = await session.get(Role, role_id)
    if role is None:
        return set()
    if role.name == "super_user":
        return {p.value for p in Permission}
    stmt = select(RolePermission.permission).where(RolePermission.role_id == role_id)
    return set((await session.execute(stmt)).scalars().all())


def _normalise(permission: Permission | str) -> str:
    return permission.value if isinstance(permission, Permission) else permission
