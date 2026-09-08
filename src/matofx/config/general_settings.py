"""Catalog of admin-tunable General Settings.

The Admin → General Settings page surfaces a closed catalog of
runtime-overridable settings spanning four sections:

1. **Session & Security** (``requires_role='super_user'``) — idle
   and hard session timeouts, password policy (length, character
   classes, allowed-symbol set), failed-login lockout, registration
   mode, default role for new sign-ups.
2. **Notifications** (``requires_role='administrator'``) — SMTP
   server config and a master switch that gates automated job
   notifications. The switch does **not** affect admin-composed
   broadcast emails (``Admin → Send Notifications``); those send
   through the same SMTP relay regardless.
3. **Job housekeeping** (``requires_role='administrator'``) —
   retention windows for drafts, logs, and completed/failed work
   dirs, consumed by :mod:`matofx.runner.retention_sweeper`. Each
   uses ``zero_means_never=True`` so ``0`` is the sentinel for "keep
   forever"; the sweeper skips that target. There is deliberately no
   audit-log retention — the audit log is a hash chain, kept in full.
4. **Branding & UX** (``requires_role='administrator'``) — app
   title, uploaded header icon + favicon, support contact, default
   timezone, and the maintenance-mode banner.
5. **Data governance** (``requires_role='super_user'``) — governance
   toggles for the file library / materials database, e.g. whether
   global library additions need curator approval.

Catalog defaults mirror the existing
:class:`matofx.config.AuthSettings` / :class:`EmailSettings`
defaults so a fresh install with no DB overrides behaves identically
to one with no Admin edits.

The DB stores any override in the ``runtime_setting`` table with
``domain='general'``; missing rows fall back to the catalog default.
See :mod:`matofx.runner.settings` for the shared ``SettingSpec``
primitive both this catalog and the runner catalog use.
"""

from __future__ import annotations

from typing import Final

from matofx.runner.settings import SettingSpec

REGISTRATION_MODES: Final[tuple[str, ...]] = ("open", "invite_only", "closed")
"""Choices for ``registration_mode``.

* ``open`` — anyone may sign up; new accounts get
  ``new_account_default_role``.
* ``invite_only`` — sign-up requires a one-time admin-issued token.
* ``closed`` — the public registration form is disabled entirely;
  admins create every account explicitly (the default).
"""

NEW_ACCOUNT_ROLE_CHOICES: Final[tuple[str, ...]] = (
    "regular_user",
    "beta_tester",
    "administrator",
)
"""Roles eligible for ``new_account_default_role``.

Deliberately omits ``super_user`` — defaulting every new sign-up to
Super User is never the right call. Sites that need Super User
accounts promote individual users after the fact.
"""

DEFAULT_PASSWORD_ALLOWED_SYMBOLS: Final[str] = "!@#$%^&*()-_=+[]{};:,.<>?/\\|~`"  # noqa: S105 - a symbol allow-list, not a secret
"""Default set of symbol characters permitted in passwords.

Combined with ``a-zA-Z0-9``, this defines the full alphabet a
password may draw from. Chars outside the union are rejected at
validation time. Sites with strict policies can narrow this string;
sites with looser policies can widen it.
"""

IMAGE_MIME_TYPES_HEADER: Final[tuple[str, ...]] = ("image/png", "image/svg+xml")
"""Acceptable upload types for the header icon."""

IMAGE_MIME_TYPES_FAVICON: Final[tuple[str, ...]] = ("image/x-icon", "image/png")
"""Acceptable upload types for the favicon."""

IMAGE_MIME_TYPES_EMAIL: Final[tuple[str, ...]] = (
    "image/png",
    "image/jpeg",
    "image/gif",
)
"""Acceptable upload types for broadcast header/footer images.

PNG / JPEG / GIF only — these are the formats that render reliably
as inline ``cid:`` images across mail clients. SVG is deliberately
excluded (most mail clients strip it)."""


GENERAL_SETTINGS_CATALOG: Final[dict[str, SettingSpec]] = {
    # ---- Section 1: Session & Security (super_user) ---------------------
    "session_idle_timeout_minutes": SettingSpec(
        key="session_idle_timeout_minutes",
        kind="int",
        default=30,
        label="Idle session timeout",
        description=(
            "How many minutes of inactivity before a logged-in user is "
            "automatically signed out. Lower values are safer on shared "
            "machines; higher values reduce nuisance re-logins."
        ),
        min_value=1,
        max_value=1440,
        unit="minutes",
        requires_role="super_user",
    ),
    "session_hard_timeout_hours": SettingSpec(
        key="session_hard_timeout_hours",
        kind="int",
        default=12,
        label="Hard session timeout",
        description=(
            "Absolute lifetime of a session regardless of activity. Users "
            "must re-authenticate after this window even if they have been "
            "continuously active."
        ),
        min_value=1,
        max_value=720,
        unit="hours",
        requires_role="super_user",
    ),
    "password_min_length": SettingSpec(
        key="password_min_length",
        kind="int",
        default=12,
        label="Minimum password length",
        description="Passwords shorter than this are rejected at set time.",
        min_value=8,
        max_value=256,
        unit="characters",
        requires_role="super_user",
    ),
    "password_require_uppercase": SettingSpec(
        key="password_require_uppercase",
        kind="bool",
        default=True,
        label="Require uppercase letter",
        description="Passwords must contain at least one character in A-Z.",
        requires_role="super_user",
    ),
    "password_require_lowercase": SettingSpec(
        key="password_require_lowercase",
        kind="bool",
        default=True,
        label="Require lowercase letter",
        description="Passwords must contain at least one character in a-z.",
        requires_role="super_user",
    ),
    "password_require_digit": SettingSpec(
        key="password_require_digit",
        kind="bool",
        default=True,
        label="Require digit",
        description="Passwords must contain at least one digit (0-9).",
        requires_role="super_user",
    ),
    "password_require_symbol": SettingSpec(
        key="password_require_symbol",
        kind="bool",
        default=True,
        label="Require symbol",
        description=("Passwords must contain at least one character from the Allowed symbols set."),
        requires_role="super_user",
    ),
    "password_allowed_symbols": SettingSpec(
        key="password_allowed_symbols",
        kind="str",
        default=DEFAULT_PASSWORD_ALLOWED_SYMBOLS,
        label="Allowed symbols",
        description=(
            "The full set of non-alphanumeric characters a password may "
            "contain. Passwords may only use a-z, A-Z, 0-9 plus characters "
            "from this string; anything else is rejected."
        ),
        requires_role="super_user",
    ),
    "failed_login_lockout_threshold": SettingSpec(
        key="failed_login_lockout_threshold",
        kind="int",
        default=5,
        label="Failed-login lockout threshold",
        description=(
            "Number of consecutive failed sign-in attempts before the "
            "account is temporarily locked."
        ),
        min_value=1,
        max_value=100,
        unit="attempts",
        requires_role="super_user",
    ),
    "failed_login_lockout_minutes": SettingSpec(
        key="failed_login_lockout_minutes",
        kind="int",
        default=15,
        label="Failed-login lockout duration",
        description="How long a locked account remains unable to sign in.",
        min_value=1,
        max_value=1440,
        unit="minutes",
        requires_role="super_user",
    ),
    "auto_disable_inactive_days": SettingSpec(
        key="auto_disable_inactive_days",
        kind="int",
        default=0,
        label="Auto-disable inactive users (days)",
        description=(
            "When non-zero, accounts that have not signed in for at least "
            "this many days are automatically deactivated by a periodic "
            "background sweep. Brand-new accounts whose holder has never "
            "logged in are measured from their creation date. Super Users "
            "are never auto-disabled (they're the install's break-glass "
            "admins). Set to 0 to disable the feature entirely."
        ),
        min_value=0,
        max_value=3650,
        unit="days",
        zero_means_never=True,
        requires_role="super_user",
        delegatable_to="administrator",
    ),
    "auto_disable_sweep_interval_hours": SettingSpec(
        key="auto_disable_sweep_interval_hours",
        kind="int",
        default=24,
        label="Inactivity sweep interval",
        description=(
            "How often the background sweeper checks for inactive users. "
            "The sweeper also runs once on each runner-daemon startup so a "
            "long downtime doesn't let stale accounts linger past the "
            "threshold. Operational tuning knob — leave at 24 unless you "
            "need finer resolution."
        ),
        min_value=1,
        max_value=168,
        unit="hours",
        requires_role="super_user",
    ),
    "auto_disable_grace_warning_days": SettingSpec(
        key="auto_disable_grace_warning_days",
        kind="int",
        default=0,
        label="Inactivity grace-warning email (days before disable)",
        description=(
            "How many days before the auto-disable cut-off a user is "
            "warned by email. When this is set, anyone who has been "
            "inactive long enough to fall within this many days of the "
            '"Auto-disable inactive users" limit gets a warning email '
            "on the next sweep, telling them they're about to be "
            "auto-disabled. Each user is emailed once per stretch of "
            "inactivity — the warning is only re-sent after a fresh "
            "login resets the clock. Set to 0 to send no warning at all "
            "(users get no notice before deactivation)."
        ),
        min_value=0,
        max_value=365,
        unit="days",
        zero_means_never=True,
        requires_role="super_user",
    ),
    "registration_mode": SettingSpec(
        key="registration_mode",
        kind="enum",
        default="closed",
        label="Registration mode",
        description=(
            "Not yet active in this build — public registration is not "
            "implemented; admins create every account. The setting saves "
            "but has no effect until the registration flow lands. "
            "Reserved values: Open (public sign-up), Invite only "
            "(one-time token), Closed (admin-only)."
        ),
        choices=REGISTRATION_MODES,
        requires_role="super_user",
    ),
    "new_account_default_role": SettingSpec(
        key="new_account_default_role",
        kind="enum",
        default="regular_user",
        label="Default role for new accounts",
        description=(
            "The role pre-selected on the Admin → Users 'Add user' form "
            "when creating a new account. The administrator can still pick "
            "a different role per user before saving; this just sets the "
            "starting choice. Super User is deliberately not offered as a "
            "default."
        ),
        choices=NEW_ACCOUNT_ROLE_CHOICES,
        requires_role="super_user",
    ),
    # ---- Section 2: Notifications (administrator) -----------------------
    "job_email_notifications_enabled": SettingSpec(
        key="job_email_notifications_enabled",
        kind="bool",
        default=True,
        label="Send automated job notifications",
        description=(
            "Master switch for automated job-state notifications "
            "(Running / Complete / Failed). Turning this off does NOT "
            "affect emails composed on Admin → Send Notifications; those "
            "use the same SMTP relay but are sent regardless of this flag. "
            "When the application's email subsystem is disabled at "
            "config-load time (TOML 'email.enabled = false'), this flag "
            "has no effect — no email goes out either way."
        ),
    ),
    "smtp_host": SettingSpec(
        key="smtp_host",
        kind="str",
        default="localhost",
        label="SMTP host",
        description=(
            "Hostname of the SMTP relay used by every outbound email "
            "(both job notifications and admin broadcasts)."
        ),
    ),
    "smtp_port": SettingSpec(
        key="smtp_port",
        kind="int",
        default=25,
        label="SMTP port",
        description="TCP port on the SMTP relay. 25 plain, 465 SMTPS, 587 STARTTLS.",
        min_value=1,
        max_value=65535,
    ),
    "smtp_username": SettingSpec(
        key="smtp_username",
        kind="str",
        default="",
        label="SMTP username",
        description=(
            "Username for SMTP authentication. Leave empty for relays "
            "that accept unauthenticated submission from this host."
        ),
    ),
    "smtp_password": SettingSpec(
        key="smtp_password",
        kind="secret",
        default=None,
        label="SMTP password",
        description=(
            "Password / API key for the SMTP relay. The value is never "
            "returned to the browser; replacing it requires entering the "
            "new value in full."
        ),
        redacted=True,
    ),
    "use_starttls": SettingSpec(
        key="use_starttls",
        kind="bool",
        default=True,
        label="Use STARTTLS",
        description=(
            "Upgrade the SMTP connection to TLS via STARTTLS. Recommended "
            "for any relay reached over an untrusted network."
        ),
    ),
    "smtp_from_address": SettingSpec(
        key="smtp_from_address",
        kind="str",
        default="matofx@localhost",
        label="From address",
        description=(
            "The 'From:' header on every outbound email. Many relays "
            "require this match a sender the SMTP credentials are "
            "authorised for."
        ),
    ),
    "public_url": SettingSpec(
        key="public_url",
        kind="str",
        default="http://localhost:8000",
        label="Public URL",
        description=(
            "The externally-visible base URL embedded in deep links inside "
            "outbound emails (e.g. 'https://matofx.example.com'). Trailing "
            "slashes are stripped."
        ),
    ),
    # ---- Section 3: Job housekeeping (administrator) --------------------
    "retain_draft_jobs_days": SettingSpec(
        key="retain_draft_jobs_days",
        kind="int",
        default=30,
        label="Retain draft jobs",
        description=(
            "How long unsubmitted draft jobs are kept before the automatic "
            "cleanup deletes them. Set to 0 to keep drafts forever (never "
            "delete)."
        ),
        min_value=0,
        max_value=3650,
        unit="days",
        zero_means_never=True,
    ),
    "retain_job_logs_days": SettingSpec(
        key="retain_job_logs_days",
        kind="int",
        default=0,
        label="Retain job logs",
        description=(
            "How long a job's run logs (its console output) are kept "
            "before the automatic cleanup deletes them. Set to 0 to keep "
            "logs forever (never delete) — the safest choice for "
            "auditing and debugging."
        ),
        min_value=0,
        max_value=3650,
        unit="days",
        zero_means_never=True,
    ),
    "retain_completed_workdirs_days": SettingSpec(
        key="retain_completed_workdirs_days",
        kind="int",
        default=0,
        label="Retain completed-job working directories",
        description=(
            "How long the on-disk working directory of a completed job "
            "(its input and output files) is kept before the automatic "
            "cleanup deletes it. Set to 0 to keep them forever (never "
            "delete)."
        ),
        min_value=0,
        max_value=3650,
        unit="days",
        zero_means_never=True,
    ),
    "retain_failed_workdirs_days": SettingSpec(
        key="retain_failed_workdirs_days",
        kind="int",
        default=0,
        label="Retain failed-job working directories",
        description=(
            "How long the on-disk working directory of a failed job is "
            "kept before the automatic cleanup deletes it. Set to 0 to "
            "keep them forever (never delete) — useful for investigating "
            "what went wrong."
        ),
        min_value=0,
        max_value=3650,
        unit="days",
        zero_means_never=True,
    ),
    # NB: there is deliberately no audit-log retention setting. The audit
    # log is a tamper-evident hash chain (Ed25519-signed); deleting old
    # rows would break full-chain verify(). It is retained in full.
    # ---- Section 4: Branding & UX (administrator) -----------------------
    "app_display_title": SettingSpec(
        key="app_display_title",
        kind="str",
        default="MATOFX",
        label="App display title",
        description=(
            "The text shown in the browser tab and in the application "
            "header. Visible to every user on every page."
        ),
    ),
    "header_icon_path": SettingSpec(
        key="header_icon_path",
        kind="image",
        default=None,
        label="Header icon",
        description=(
            "Logo or wordmark shown in the page header beside the title. "
            "Upload a PNG or SVG; ~64 px tall renders best. Falls back to "
            "the bundled default when unset."
        ),
        accepted_mime_types=IMAGE_MIME_TYPES_HEADER,
    ),
    "favicon_path": SettingSpec(
        key="favicon_path",
        kind="image",
        default=None,
        label="Favicon",
        description=(
            "Icon shown in the browser tab. Upload an .ico or PNG; the "
            "system serves it at 16×16 and 32×32. Falls back to the "
            "bundled default when unset."
        ),
        accepted_mime_types=IMAGE_MIME_TYPES_FAVICON,
    ),
    "support_email_footer": SettingSpec(
        key="support_email_footer",
        kind="str",
        default="",
        label="Support email (footer)",
        description=(
            "Contact address rendered in the shell footer. Leave empty to "
            "hide the footer line entirely."
        ),
    ),
    "default_timezone": SettingSpec(
        key="default_timezone",
        kind="str",
        default="UTC",
        label="Default timezone",
        description=(
            "The IANA timezone the application uses to display dates and "
            "times (for example 'Europe/London' or 'America/New_York') "
            "for users who haven't chosen their own on their Account "
            "page. Times are stored internally as UTC and converted for "
            "display. Leave as UTC if unsure; an unrecognised name falls "
            "back to UTC."
        ),
    ),
    "maintenance_banner_text": SettingSpec(
        key="maintenance_banner_text",
        kind="str",
        default="",
        label="Maintenance banner",
        description=(
            "A short message shown to every user across the top of every "
            "page. Leave empty to hide the banner. Use this to announce "
            "planned downtime or critical notices."
        ),
    ),
    "maintenance_window_iso": SettingSpec(
        key="maintenance_window_iso",
        kind="str",
        default="",
        label="Maintenance window",
        description=(
            "Optional ISO-8601 datetime for an upcoming maintenance "
            "window, rendered alongside the banner. Leave empty when no "
            "window is scheduled."
        ),
    ),
    "favourites_enabled": SettingSpec(
        key="favourites_enabled",
        kind="bool",
        default=False,
        label="Quick Access favourites",
        description=(
            "When on, each user can star tools and workflows in Quick "
            "Access; favourited cards float to the top of their "
            "section. Turning this off hides the stars and restores "
            "the default ordering — existing favourites are kept and "
            "re-appear if the feature is turned back on."
        ),
    ),
    # ---- Section 5: Data governance (super_user) -----------------------
    "library_global_publish_approval": SettingSpec(
        key="library_global_publish_approval",
        kind="bool",
        default=True,
        label="Require approval for global library files",
        description=(
            "When on, adding a file — or a new version of a file — to the "
            "GLOBAL file library requires a second library curator to approve "
            "it before it becomes pickable. The submitter chooses one "
            "approver at submit time; self-approval is not allowed, so a real "
            "gate needs at least two library curators. Personal- and "
            "project-scoped library files are never gated. Turning this off "
            "lets a library curator publish to global directly."
        ),
        requires_role="super_user",
    ),
    "materials_change_approval": SettingSpec(
        key="materials_change_approval",
        kind="bool",
        default=True,
        label="Require approval for material changes",
        description=(
            "When on, adding a material — or a new version of a material — to the "
            "materials database requires a second materials curator to approve it "
            "before it becomes usable in tool forms. The author chooses one approver "
            "at submit time; self-approval is not allowed, so a real gate needs at "
            "least two materials curators. Turning this off lets a materials curator "
            "publish material changes directly."
        ),
        requires_role="super_user",
    ),
    "models_global_publish_approval": SettingSpec(
        key="models_global_publish_approval",
        kind="bool",
        default=True,
        label="Require a checker for global surrogate models",
        description=(
            "When on, publishing a surrogate model — or a new version — to GLOBAL "
            "general use requires a technical checker (a Model Curator) to sign it "
            "off before a Super User / Administrator releases it. The builder "
            "nominates one checker at submit time; self-approval is not allowed, so "
            "a real gate needs at least two Model Curators. The mandatory Super User "
            "release is always required regardless of this toggle. Turning this off "
            "lets a Model Curator skip the checker step (the release is still "
            "required). Personal- and project-scoped models are never gated."
        ),
        requires_role="super_user",
    ),
    "lineage_max_nodes": SettingSpec(
        key="lineage_max_nodes",
        kind="int",
        default=500,
        label="Lineage graph soft cap (nodes)",
        description=(
            "Maximum number of nodes the lineage / provenance graph will "
            "assemble before the breadth-first graph traversal stops "
            "walking. Graphs above this cap render as a tabular section "
            "instead of the interactive layered diagram (the layered layout "
            "doesn't scale visually past this point). Engineering-typical "
            "graphs are well below 100 nodes; the default is generous. Lower "
            "the cap if a customer install has unusually tight rendering "
            "budgets; raise it if their graphs are dense and the table "
            "fallback fires too often."
        ),
        requires_role="super_user",
        min_value=10,
        max_value=10000,
    ),
    "llm_summaries_enabled": SettingSpec(
        key="llm_summaries_enabled",
        kind="bool",
        default=False,
        label="AI-enriched result summaries",
        description=(
            "When on, MATOFX may send a result's audited facts to the LLM "
            "endpoint configured in the [llm] section of the config file to "
            "produce a more fluent summary on job and study pages. OFF by "
            "default: the deterministic templated summary is always shown and "
            "never leaves the box; turning this on is what permits prompts + "
            "facts to be sent to the configured provider. Has no effect until "
            "an [llm] provider is wired in the config file. Individual users "
            "can still opt out on their Account page. Governs data egress, so "
            "it is Super User only."
        ),
        requires_role="super_user",
    ),
    # ---- Approval notifications (administrator) -------------------------
    # A master switch plus one per-type switch. An approval-request
    # email is only sent when the master is on AND the per-type switch
    # for that kind of approval is on. These are separate from the
    # job-state switch above: turning off job-state notifications does
    # not silence "needs your approval" emails, and vice versa. All
    # ride the same SMTP relay and the TOML ``email.enabled`` gate.
    # Licence alerts. Held apart from both switches above because they
    # are neither job news nor an approval: they are the only thing that
    # makes a licence problem visible *while it is happening*. Without
    # them an over-cap install is invisible until a compliance report is
    # generated AND sent, which at the agreed cadence is up to a year.
    "licence_alert_emails_enabled": SettingSpec(
        key="licence_alert_emails_enabled",
        kind="bool",
        default=True,
        label="Send licence alerts to administrators",
        description=(
            "Email administrators when something happens to your licence "
            "that they would otherwise only discover much later: a Super "
            "User admitted over the limit (break-glass), a sign-in refused "
            "because every place was in use, or the licence entering its "
            "grace period. These go to holders of the audit-log right, "
            "which administrators have by default. Turning this off does "
            "not stop the events being recorded — they still appear in the "
            "audit log and in compliance reports; it only stops the email."
        ),
        requires_role="administrator",
    ),
    "approval_email_notifications_enabled": SettingSpec(
        key="approval_email_notifications_enabled",
        kind="bool",
        default=True,
        label="Send approval-request notifications",
        description=(
            "Master switch for the emails that tell an approver (or "
            "curator) an item is waiting for their decision, and the "
            "emails that tell a requestor the outcome. Turn this off to "
            "silence every approval email at once — approvers still see "
            "pending items in the in-app Approvals inbox. This is "
            "independent of the job-state switch above; it does not "
            "affect Running / Complete / Failed emails. When the email "
            "subsystem is disabled at config-load time "
            "('email.enabled = false'), no email goes out either way."
        ),
    ),
    "approval_email_jobs": SettingSpec(
        key="approval_email_jobs",
        kind="bool",
        default=True,
        label="Email for job approvals",
        description=(
            "Email the eligible approvers when a job is held for approval "
            "before it runs, or when its results are held for release. "
            "Subordinate to the master switch above."
        ),
    ),
    "approval_email_library": SettingSpec(
        key="approval_email_library",
        kind="bool",
        default=True,
        label="Email for library file approvals",
        description=(
            "Email the chosen library curator when a file is submitted "
            "for publishing to the global file library. Subordinate to "
            "the master switch above."
        ),
    ),
    "approval_email_materials": SettingSpec(
        key="approval_email_materials",
        kind="bool",
        default=True,
        label="Email for material approvals",
        description=(
            "Email the chosen materials curator when a material change is "
            "submitted for approval. Subordinate to the master switch "
            "above."
        ),
    ),
    "approval_email_models": SettingSpec(
        key="approval_email_models",
        kind="bool",
        default=True,
        label="Email for surrogate model approvals",
        description=(
            "Email the chosen Model Curator when a surrogate model is "
            "submitted for checking before global release. Subordinate to "
            "the master switch above."
        ),
    ),
    "approval_email_vv": SettingSpec(
        key="approval_email_vv",
        kind="bool",
        default=True,
        label="Email for V&V record approvals",
        description=(
            "Email the chosen V&V curator when a verification & "
            "validation record is submitted for promotion. Subordinate "
            "to the master switch above."
        ),
    ),
    "approval_email_studies": SettingSpec(
        key="approval_email_studies",
        kind="bool",
        default=True,
        label="Email for parametric study approvals",
        description=(
            "Email the eligible approvers when a parametric study is held "
            "for approval before it runs. Subordinate to the master "
            "switch above."
        ),
    ),
    "approval_email_optimisation": SettingSpec(
        key="approval_email_optimisation",
        kind="bool",
        default=True,
        label="Email for optimisation approvals",
        description=(
            "Email the chosen approver when an optimisation study is held "
            "at its start gate. Subordinate to the master switch above."
        ),
    ),
    # ---- Broadcast email template (administrator) -----------------------
    # Managed from Admin -> Send Notifications, NOT the General Settings
    # page: these keys are deliberately absent from
    # admin_general_settings.py's SECTIONS so they don't render there.
    # They live in this catalog only to reuse GeneralSettingsService's
    # validation / persistence / audit / image-storage machinery.
    "broadcast_header_text": SettingSpec(
        key="broadcast_header_text",
        kind="str",
        default="",
        label="Broadcast header text",
        description=(
            "Text block placed above the message body in every "
            "admin broadcast email. Leave empty to omit the header."
        ),
    ),
    "broadcast_footer_text": SettingSpec(
        key="broadcast_footer_text",
        kind="str",
        default="",
        label="Broadcast footer text",
        description=(
            "Text block placed below the message body in every "
            "admin broadcast email. Typical use: signature, contact "
            "details, legal disclaimer. Leave empty to omit the footer."
        ),
    ),
    "broadcast_header_image": SettingSpec(
        key="broadcast_header_image",
        kind="image",
        default=None,
        label="Broadcast header image",
        description=(
            "Banner image embedded at the top of every broadcast "
            "email (inline, above the header text). PNG / JPEG / GIF. "
            "Leave unset for a text-only header."
        ),
        accepted_mime_types=IMAGE_MIME_TYPES_EMAIL,
    ),
    "broadcast_footer_image": SettingSpec(
        key="broadcast_footer_image",
        kind="image",
        default=None,
        label="Broadcast footer image",
        description=(
            "Image embedded at the bottom of every broadcast email "
            "(inline, below the footer text). PNG / JPEG / GIF. "
            "Leave unset for a text-only footer."
        ),
        accepted_mime_types=IMAGE_MIME_TYPES_EMAIL,
    ),
}
"""Closed catalog of admin-tunable General Settings keys.

Adding a new setting is a code change in this catalog plus a
consumer update (Task #5: wire the new key into the module that
actually reads it). The DB needs no migration — values land in the
existing ``runtime_setting`` table under ``domain='general'``.
"""
