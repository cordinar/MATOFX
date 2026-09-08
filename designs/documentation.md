# Design — Documentation system (in-app + standalone)

Status: **Designed 2026-06-09 — not yet built.**
Decision record: `decisions/0014-documentation-system.md`.
Delivery tracking: `ROADMAP.md` (Customer-facing docs row).

This is the build-level spec. The ADR records *why*; this records *what to build* —
the source layout, the table of contents, the build pipeline, and the in-app wiring.

---

## 1. Scope at a glance

| Capability | v1 | Deferred |
|---|---|---|
| Single Markdown source under `matofx/docs/` (`public/` `user/` `admin/` `shared/`) | ✅ | |
| In-app native render via `ui.markdown` (no runtime dependency) | ✅ | |
| `/help` hub page — role-filtered TOC + content pane + client-side search | ✅ | |
| Header "?" button (global entry) | ✅ | |
| Contextual deep-links (`help_topic="<slug>"` → `/help?topic=<slug>`) | ✅ (incremental) | |
| Public "Getting started" on the login screen (pre-sign-in) | ✅ | |
| Admin guide gated to `is_admin()` in-app | ✅ | |
| Standalone static HTML site (built in release CI) | ✅ | |
| Per-set PDF manuals (user / admin), version-stamped, in `dist/` | ✅ | |
| Cross-links to the Tutorials feature on tool/workflow surfaces | ✅ | |
| In-app full-text (not just title) search | ✅ (client-side index) | |
| In-app PDF export of a single topic | | ✅ v1.x |
| Localised / translated docs | | ✅ later |
| Demo-group pages documented | | Until they're real |

---

## 2. Source layout

```
matofx/docs/
  public/                 # shown on the login screen, pre-sign-in
    00-welcome.md
    10-signing-in.md
    20-quick-tour.md
    30-getting-help.md
  user/                   # every signed-in user
    00-getting-started.md
    10-your-account-page.md
    20-projects-and-scenarios.md
    ...
  admin/                  # admins only (is_admin())
    00-admin-overview.md
    10-installing-and-setup.md
    20-people-and-access.md
    ...
  shared/
    images/
    _includes/
  docs.toml               # site title, version stamp, ordering overrides
```

Numeric filename prefixes set order within a set. Front-matter on every file:

```yaml
---
title: Submitting a job
audience: user            # public | user | admin
capability: null          # null, or e.g. "approver" / "library.curate" → call-out
slug: jobs/submitting     # stable anchor for contextual "?" deep-links
summary: How to launch a tool and fill in its form.
---
```

`slug` is the contract between a page's `help_topic` and the doc topic. It is
stable even when the filename or order changes.

---

## 3. House style (enforced in review)

- Plain, simple English; short sentences; second person ("you").
- Few acronyms. On first use in a file, write the term in full with a one-line
  plain meaning, e.g. *Verification and Validation (V&V) — checking a tool was
  built correctly and gives trustworthy answers*. Thereafter the short form is fine.
- Every guide closes with a **Glossary** chapter so nothing is left undefined.
- Feature-gated chapters open with a one-line "only if switched on for your site."
- Screenshots live in `shared/images/`, referenced relatively.

---

## 4. Table of contents

### 4.1 Public "Getting started" (login screen, no sign-in)

A deliberately small on-ramp.

1. **Welcome to MATOFX** — 1.1 What MATOFX is for · 1.2 What "Simulation Process
   and Data Management (SPDM)" means, in one paragraph
2. **Signing in** — 2.1 Signing in for the first time · 2.2 Setting and changing
   your password (and the password rules) · 2.3 If you can't sign in (who to contact)
3. **A quick tour of the screen** — 3.1 The top bar · 3.2 The left-hand menu ·
   3.3 Where your work appears
4. **Where to get more help** — 4.1 The help button inside MATOFX · 4.2 Your
   administrator and the support contact

### 4.2 User guide (every signed-in user)

**1. Getting started**
1.1 What MATOFX does for you · 1.2 Signing in and out · 1.3 The main screen
explained (top bar, left menu, home page) · 1.4 Your home page at a glance (job
status, projects, quick access, tutorials) · 1.5 Starring favourites (pinning the
tools and workflows you use most, when your site enables it) · 1.6 Automatic
sign-out after a period of inactivity · 1.7 Getting help inside MATOFX

**2. Your account page** *(the "Account info" page — one subsection per card)*
2.1 Opening your account page (My Account → Account info) · 2.2 Your identity —
username, email, full name, role, member-since date, last sign-in, user number;
and that these are view-only here (ask an administrator to change your name or
email) · 2.3 Your approver status — whether you can approve, and the exact tools
you're listed on · 2.4 Your teams — the teams you belong to, managed by an
administrator · 2.5 Display — choosing a dark or light appearance, saved to your
account so it follows you to any machine · 2.6 Region & time — language/number
formatting and time zone, including "use my browser's setting", and how they drive
the clock at the top · 2.7 Notification preferences — the job-email switch, and the
caveat that it only covers tools whose author enabled emails · 2.8 Changing your
password — current + new, the live rule checklist and the "passwords match"
indicator, and that the rules come from your site's policy

**3. Running a tool (submitting a job)**
3.1 What a "tool" and a "job" are · 3.2 Starting a job from the home page ·
3.3 Adding several jobs at once (the "Create Job(s)" chooser) · 3.4 Starting a job
inside a scenario · 3.5 Filling in the form — 3.5.1 Required and optional fields ·
3.5.2 Units of measurement · 3.5.3 Choosing from a list · 3.5.4 Uploading a file ·
3.5.5 Picking a file from the shared file library · 3.5.6 Choosing a material from
the materials database · 3.6 Saving your work before submitting · 3.7 Submitting
the job · 3.8 What happens next (queued, running, finished) · 3.9 Jobs that need
approval first

**4. Tracking and reading results**
4.1 The "My Jobs" page · 4.2 What each job state means (created, queued, running,
complete, failed) · 4.3 Watching a job run live · 4.4 Reading the job log ·
4.5 Viewing and downloading output files · 4.6 The job report page · 4.7 Viewing
3D models, images, and animations · 4.8 Understanding Verification and Validation
(V&V) badges — what the badge on a tool/result means for non-curators ·
4.9 When a job fails: what to do

**5. Finding past jobs**
5.1 Why search past jobs · 5.2 Searching by tool and input values · 5.3 Opening a
result from the search

**6. Comparing jobs**
6.1 Why compare · 6.2 Choosing jobs to compare · 6.3 Overlaying results on one
chart · 6.4 Side-by-side differences · 6.5 Comparing across scenarios or
workflows · 6.6 Saving or sharing a comparison

**7. Building your own workflows**
7.1 What a workflow is (several tools run in order) · 7.2 Creating one (choosing
2–10 tools) · 7.3 How MATOFX works out the running order · 7.4 Running your
workflow · 7.5 Watching a workflow run (seeing each tool's status) · 7.6 When one
tool in a workflow fails · 7.7 Sharing a workflow with named colleagues

**8. Parametric studies** *(only if switched on)*
8.1 What a parametric study is (running a tool many times with different inputs) ·
8.2 Setting one up · 8.3 Choosing which inputs to vary · 8.4 Running and watching
progress · 8.5 Reading the results

**9. Optimisation** *(only if switched on)*
9.1 What optimisation does (finding good inputs automatically) · 9.2 Setting one
up · 9.3 Goals and limits, in plain terms · 9.4 Choosing a method (a short,
jargon-light guide) · 9.5 Running and watching progress · 9.6 Reading the results
and charts

**10. Run cohorts and surrogate models** *(only if switched on)*
10.1 What these are, simply (a fast stand-in for a slow tool) · 10.2 Building a run
cohort (a batch of runs to learn from) · 10.3 Fitting a surrogate model ·
10.4 Making predictions · 10.5 Viewing predicted results in 3D · 10.6 Publishing a
model for others

**11. The results warehouse and key numbers** *(only if switched on)*
11.1 What the results warehouse is · 11.2 What a Key Performance Indicator (KPI)
is, in one line · 11.3 Browsing the key numbers by tool · 11.4 Filtering and
exporting

**12. If you have extra responsibilities (capabilities)**
12.1 What a "capability" is (extra rights on top of a normal account) ·
12.2 Approving simulations (if you are an approver) · 12.3 Looking after the shared
file library (if you are a library curator) · 12.4 Looking after the materials
database (if you are a materials curator) · 12.5 Recording Verification and
Validation (if you are a V&V curator)

**13. Staying informed**
13.1 Email notifications (job started, finished, failed) and what their links do ·
13.2 Site notices and maintenance banners · 13.3 Your organisation's tutorials
(uploaded guides and videos)

**14. Help and glossary**
14.1 Getting help inside MATOFX · 14.2 Contacting support · 14.3 Glossary of terms

### 4.3 Admin guide (admins only; includes setup steps)

**1. Administrator overview**
1.1 What an administrator does · 1.2 The "Admin Space" menu explained · 1.3 Super
user vs administrator (the difference) · 1.4 A safe first-day checklist

**2. Installing and setting up MATOFX**
2.1 What you need before you start · 2.2 Where MATOFX can run — your own server, a
Windows or Linux machine, or a cloud computer (what "on-premises" means) ·
2.3 Installing the application · 2.4 The two parts that run: the web interface and
the job server · 2.5 Starting and stopping MATOFX · 2.6 The first sign-in: creating
the first super user · 2.7 Choosing where data is stored: the simple built-in
database vs a separate database server (one line on each) · 2.8 The shared file
area · 2.9 Setting up email so MATOFX can send notifications · 2.10 Turning on
secure web access (encrypted connections) · 2.11 Backups and keeping data safe ·
2.12 Updating to a new version · 2.13 Setup checklist

**3. People and access**
3.1 How accounts and roles work, in plain English · 3.2 Base roles vs
capabilities · 3.3 Creating a user · 3.4 Editing a user and resetting a password ·
3.5 Making someone an approver · 3.6 Granting capabilities (library, materials,
V&V) · 3.7 Teams and groups · 3.8 Permissions: who can do what · 3.9 Deactivating
or removing a user

**4. Adding tools**
4.1 What a tool is, and how MATOFX runs it · 4.2 The tool description file
(descriptor), explained simply · 4.3 Creating a tool with the guided wizard ·
4.4 Defining the input form — 4.4.1 Field types · 4.4.2 Units · 4.4.3 Drop-down
options · 4.4.4 Required fields and defaults · 4.4.5 Grouping fields into sections ·
4.5 Connecting the tool's program and its environment · 4.6 Which files to keep and
which to clean up · 4.7 Adding a tool icon · 4.8 Pulling out key numbers and charts
automatically · 4.9 Linking Verification and Validation records · 4.10 Testing a
tool before release · 4.11 Releasing, updating, and versioning · 4.12 Archiving a
tool

**5. Building workflows**
5.1 What an administrator workflow is · 5.2 Choosing its tools · 5.3 Setting the
running order · 5.4 Passing results from one tool to the next · 5.5 Releasing a
workflow · 5.6 Inspecting users' custom workflows

**6. Moving tools and workflows between installations**
6.1 Why move them (e.g. from a test system to the live one) · 6.2 Exporting a
bundle · 6.3 Importing a bundle · 6.4 How references are matched by name · 6.5 Why
imported items arrive as "new" · 6.6 Reading the import report (what was skipped,
and why)

**7. Where files live**
7.1 The folders MATOFX uses · 7.2 Setting the job working area · 7.3 The shared
file area for users

**8. Site settings and branding**
8.1 The General Settings page explained · 8.2 Turning features on or off (feature
switches) · 8.3 Branding: application name, header logo, and browser tab icon ·
8.4 The maintenance banner · 8.5 The support contact line · 8.6 Inactivity /
sign-out timing · 8.7 Favourites and other conveniences

**9. Project information fields**
9.1 What these fields are for · 9.2 Creating them · 9.3 Making fields required

**10. Approvals**
10.1 How approvals work · 10.2 Requiring approval before a tool or study runs ·
10.3 Choosing approvers

**11. Tutorials (uploaded guides and videos)**
11.1 Tutorials vs this built-in documentation (the difference) · 11.2 Uploading a
document or video · 11.3 Scope: general, per-tool, or per-workflow · 11.4 Activating
and replacing tutorials

**12. Sending messages to users**
12.1 Email notifications overview · 12.2 Sending a broadcast notice · 12.3 Setting
the maintenance banner

**13. Monitoring and records**
13.1 The audit log — a tamper-evident record of who did what · 13.2 Usage and
statistics · 13.3 Data lineage — how a result traces back to its inputs ·
13.4 Exploring the database (read-only)

**14. Looking after MATOFX day to day**
14.1 Routine checks · 14.2 The job queue and freeing stuck jobs · 14.3 Computing
resources: processors and graphics processors (GPUs) for heavy jobs · 14.4 Running
jobs on other machines — an always-on job server and computing clusters (defining
each) · 14.5 Licensing · 14.6 Logs and troubleshooting · 14.7 Getting support from
Funis

**15. Reference and glossary**
15.1 Roles and capabilities reference · 15.2 Feature switches reference ·
15.3 Glossary of terms

---

## 5. In-app wiring

**5.1 Discovery + model.** A `docs` module (proposed `matofx/docs/__init__.py`
loader, separate from the content tree) walks the Markdown files at startup,
parses front-matter, and builds an in-memory tree keyed by set → chapter → topic.
Content is read-only and shipped with the package, so it can be cached for the
process lifetime.

**5.2 The `/help` page** (`ui/pages/help.py`). Renders the chrome, then a
two-pane layout: a left TOC (sectioned **User guide** always; **Admin guide** only
when `is_admin()`), and a content pane that renders the selected topic with
`ui.markdown`. `?topic=<slug>` selects a topic directly. A footer line shows
"Documentation for vX.Y.Z".

**5.3 Search.** A JSON index (slug, title, summary, body text) is built once and
shipped to the client; the search box filters titles + body without a server
round-trip. Admin topics are excluded from the index served to non-admins.

**5.4 Header "?" button.** Added in `_chrome._render_header`, next to the live
clock / Log out. Reads the current route's declared `help_topic` (if any) and
navigates to `/help?topic=<slug>`, else `/help`.

**5.5 Contextual links.** `render_shell(help_topic: str | None = None)`. Pages pass
their slug; pages that don't are unaffected (fall back to the hub root). Roll out
incrementally, highest-traffic pages first (submit, my-jobs, job, account).

**5.6 Nav entry.** "Help & documentation" gets **its own sidebar group** (a single
`_HELP_ITEMS` entry rendered as its own collapsible group, placed after My Account),
visible to every signed-in user. The admin tree *inside* the hub is what's gated by
`is_admin()`, not the entry itself.

**5.7 Public set on login.** `ui/pages/login.py` gains a "Getting started" link /
panel that renders the `public/` set with `render_header_only()` (no sidebar, no
auth) — the only docs reachable before sign-in.

**5.8 Role gating.** `user/` + `public/` open; `admin/` requires `is_admin()`,
enforced both in the TOC builder and on direct `?topic=` access (an admin slug
requested by a non-admin falls back to the hub root with a quiet notice).

---

## 6. Standalone build (release CI only)

Runs on the maintainer's release pipeline (same box that builds the canonical
lockfile); **no runtime dependency on the on-prem install.**

1. **Static HTML site** — render each Markdown file to HTML with a shared template
   (left TOC + content + search), one site per set plus the public pages. Output:
   `dist/docs-site/`. A client-side index powers search offline.
2. **PDF manuals** — concatenate each set in TOC order and render to PDF
   (HTML→PDF). Output: `dist/matofx-user-guide-vX.Y.Z.pdf`,
   `dist/matofx-admin-guide-vX.Y.Z.pdf`.
3. **Version stamp** — `docs.toml` version is set from the build tag; it feeds both
   the in-app footer and the artifact filenames so a given install's docs always
   match its code.

The static site should render from the **same** Markdown the app renders, so the
two views stay faithful; minor rendering differences between `ui.markdown` and the
site renderer are acceptable and noted here so a future contributor doesn't chase
them as bugs.

---

## 7. Relationship to the Tutorials feature

Documentation (this system) is Funis-authored, version-pinned, and ships with the
release. Tutorials (`tutorials/` module) stay the customer's own per-tool /
per-workflow / general uploads. They are complementary and sit side by side on a
tool's surface. Admin guide Chapter 11 explains which lever to pull. No code in the
Tutorials feature changes.

---

## 8. Build slices

Status as of 2026-06-09 — the **in-app system is complete and the framework is
done**; what remains is bulk content authoring and the release-time standalone
build.

1. ✅ **Shipped.** `docs` loader + front-matter model (`matofx/docs/`: `models.py`,
   `loader.py`, `search.py`) + `public/` (4 chapters), `user/` (Ch. 1–2), `admin/`
   (Ch. 1) content; `/help` hub rendering the role-filtered tree; own sidebar
   group. Tests in `tests/docs/`.
2. ✅ **Shipped.** Header "?" button + `render_shell(help_topic=...)` plumbing
   (`_chrome.py`) + first contextual link on the Account page. (`home.py` link
   deferred — that file carries unrelated pre-existing lint debt; wire it when its
   chapter is authored.)
3. ✅ **Shipped.** Admin-tree gating (`DocTree.in_app_sets` / `visible_topic` +
   `is_admin()`) plus the full admin guide content (see slice 6).
4. ✅ **Shipped.** In-app search — reusable `matofx/docs/search.py` (`searchable_text`
   / `search_topics`) wired into the hub left pane (server-side filter over the
   in-memory tree, scoped to the visible sets). Tests in `tests/docs/test_search.py`.
5. ✅ **Shipped.** Public `/getting-started` route (pre-sign-in, `public` set only,
   `?topic=` scoped to that set) + link on the login page.
6. ✅ **Shipped (full draft, 2026-06-09).** All chapters of both guides authored per
   §4 — **public 4, user 14, admin 15 (33 topics total)** — in plain English,
   acronyms defined, each guide closing with a glossary. Validated by the loader
   consistency test. **Pending Robert's review for accuracy.** Per-page `help_topic`
   wiring beyond the Account page is still incremental (optional; the header "?"
   opens the hub on every page meanwhile).
7. ☐ **Remaining.** Release-CI standalone build — static HTML site + per-set PDFs
   from the same Markdown, version-stamped into `dist/` (reuses `search.py` for the
   site index).

The only remaining slice is the release-time standalone build (7). The in-app
system is complete and fully populated; new or revised chapters are just Markdown
edits — no code change makes a chapter go live.
