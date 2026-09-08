# 0014. In-app + standalone documentation system

Date: 2026-06-09
Status: Accepted (design discussion with the maintainer, 2026-06-09). **Design only — no code yet.** Build-level spec: `designs/documentation.md`.

## Context

MATOFX (the Simulation Process and Data Management platform) has thorough
*maintainer* documentation — `INSTALL.md`, `MIGRATE_GUIDE.md`, the ADRs in
`decisions/`, the design docs in `designs/` — but **nothing written for the
people who actually use the running application.** `ROADMAP.md` flags the gap
explicitly: operator docs exist, but "End-user + tool-author guides still to
write."

Two distinct audiences need help, and the navigation already splits along the
same line (`ui/pages/_chrome.py`): a **Dashboard** group visible to every
signed-in user, and an **Admin Space** group gated to `super_user` /
`administrator` via `is_admin()`. On top of the base role sit *capabilities*
(library / materials / V&V curators) and the approver flag, which conditionally
reveal extra surfaces.

There is already a **Tutorials** feature (`tutorials/` module + `admin_tutorials`
page + Home-page cards), but it is a different thing: admin-uploaded PDF / video
*assets*, scoped per-tool / per-workflow / general — the customer's own material
about their own tools. It is binary upload + activate, not authored, maintainable,
searchable prose about the platform itself.

There is no in-app Markdown-rendering surface today, and no `/help` route.

The request: documentation **split into two sets** (regular-user usage and
super-user / admin usage), accessible **both inside MATOFX and as standalone
documents.**

## Decision

Build a **single-source documentation system**: author once as Markdown in the
repository, render it **natively in-app** and **build standalone artifacts at
release time.** No new database tables — documentation ships with the application
version as files on disk.

**1. Two product manuals plus a small public set.**
- **User guide** — every Dashboard-group surface; visible to every signed-in user.
- **Admin guide** — every Admin Space surface **plus real install/setup steps**;
  visible **only to admins** in-app (`is_admin()` gate).
- **Public "Getting started"** — a small, self-contained trim shown on the
  **login screen**, before sign-in.

Capability-holder topics (approver, library / materials / V&V curators) live in
the user guide as clearly-marked "if you have this capability" call-outs — **not**
a third manual.

**2. Single Markdown source, multiple targets.** Source under `matofx/docs/`
(`user/`, `admin/`, `public/`, `shared/`). Numeric filename prefixes drive order;
YAML front-matter declares `title` / `audience` / `capability` / `slug` /
`summary`. The `slug` is the stable anchor that contextual help links target.

**3. In-app rendering is native and dependency-light.** A new `docs` module
discovers the files, parses front-matter, builds the role-filtered tree, and
renders selected topics with NiceGUI `ui.markdown` — **no new runtime
dependency.** Surfaced three ways:
- a **"?" button in the top header** (global entry, every page);
- a **"Help & documentation" nav entry** (browsable destination at `/help`);
- **contextual deep-links** — a page passes `help_topic="<slug>"`; the header "?"
  then deep-links into the matching topic. Incremental; pages opt in over time.

Client-side search over titles + body, from a JSON index built at page load.

**4. Standalone artifacts are built in release CI, not at runtime.** The same
Markdown compiles to (a) a browsable, searchable **static HTML site** and (b) a
**combined PDF per set** (user manual / admin manual). Heavier build-only
dependencies (a Markdown→HTML step, an HTML→PDF step) live in the maintainer's
release pipeline — the same `workflow_dispatch` box that produces the canonical
lockfile — and never touch the on-prem runtime install. Artifacts are
version-stamped and dropped into `dist/`.

**5. Version-pinned.** Documentation version == application version. The Help hub
footer shows "Documentation for vX.Y.Z"; standalone files are filename-stamped.
A single-tenant install at v1.3 ships exactly the v1.3 manuals.

**6. Separate from Tutorials, cross-linked.** Documentation is Funis-authored and
ships with the version; Tutorials stay the customer's per-tool uploads. On a
tool's surface they sit side by side. The admin guide explains the difference.

**7. Plain-English house style.** Short sentences, "you" not "the user", acronyms
written in full with a one-line plain meaning on first use, and a **glossary**
closing each guide. The admin guide carries the install/setup steps as real,
plainly-written content rather than a pointer into `INSTALL.md`.

## Alternatives considered

- **MkDocs / Sphinx as the in-app renderer** — a polished static site embedded
  in-app via an iframe. Rejected for the in-app surface: it frames an external
  site rather than rendering natively in the NiceGUI shell, and adds a toolchain
  dependency to the runtime. (A generator *may* still be used as a build-time tool
  for the standalone site; that's a `designs/` build detail, not the in-app path.)
- **Admin-uploaded docs via the Tutorials mechanism** — no build step, but loses
  single-source maintainability, version-pinning, and search; documentation would
  become per-install blobs that drift from the code.
- **Three manuals (separate curator/power-user guide)** — cleaner separation for
  power users, but more surfaces to maintain and a fuzzier boundary for readers.
  Rejected in favour of capability call-outs inside the user guide, mirroring how
  the nav conditionally reveals those surfaces.
- **A separate standalone installer/operator manual** — folded the setup steps
  into the admin guide (Chapter 2) so everything an admin needs is in one place.
  Can be split out later if an installer needs docs before any admin account
  exists.
- **Generating PDFs at runtime in the app** — rejected: pulls heavy native
  dependencies into the on-prem install for a release-time artifact. Built in CI
  instead.

## Consequences

- New `matofx/docs/` Markdown tree (content), a new `docs` discovery/render
  module, one new `/help` page, one new nav entry, and a header "?" button. **No
  new database tables, no new migration.** Role gating reuses `is_admin()`; the
  public set renders on the existing login page.
- The release pipeline gains a docs-build step producing the static HTML site +
  per-set PDFs into `dist/`, version-stamped.
- Documentation and the shipped PDFs are generated from identical sources and
  cannot drift.
- Contextual help is incremental: a page with no `help_topic` simply falls back to
  the Help hub root — no big-bang wiring.
- The user guide must be kept honest against feature flags: feature-gated chapters
  (parametric studies, optimisation, surrogates, results warehouse) are marked
  "only if switched on for your site."
- The Demo menu group is intentionally **left undocumented** until those
  placeholder pages become real features.
