---
title: Reference and glossary
audience: admin
capability: null
slug: reference-and-glossary
summary: A quick reference to roles and capabilities, the feature switches, and plain-English definitions of administrator terms.
---

# Reference and glossary

## Roles and capabilities reference

**Roles** (each account has exactly one):

- **Super User** — highest access; the first account; can do everything.
- **Administrator** — day-to-day administration.
- **Regular User** — uses tools, runs jobs, sees own and shared work.
- *(plus any custom roles your organisation has added.)*

**Capabilities** (extra rights added on top of a role — each is granted by adding
the matching capability to an account; see *People and access*):

- **File library curator** — approves, assigns and retires files in the shared
  file library.
- **Materials curator** — approves, retires and manages classes in the shared
  materials database.
- **Verification and Validation (V&V) curator** — reviews, approves, rejects and
  retires V&V records.
- **Cohort curator** — edits job cohorts and their membership (the data a
  surrogate is fitted from).
- **Surrogate Model Curator** — checks a surrogate model before a super user
  releases it for general use.
- **Optimisation approver** — signs off the start of an optimisation campaign
  against a governed tool.
- **Approver** — signs off jobs and parametric studies (and, where required,
  their results), plus the results release of optimisations, for the tools
  they're **listed on**. A user must first have **Can approve runs** ticked on their account to be
  listed. Unlike the curator capabilities, this is set per tool rather than
  granted as a blanket right — see *Approvals*.

## Feature switches reference

These larger features are turned on or off **by your licence**. When one is off
it is absent everywhere — no menu entry, no home-page card, and the underlying
web addresses return "not found" — so there is no half-enabled state to explain
to users. (On installs without a licence file yet, the `[features]` list in
`matofx.toml` is used instead. General Settings does *not* control these; it
holds day-to-day site settings.)

- **Parametric Studies** — run a tool many times across a range of inputs.
- **Optimisation** — search automatically for the best inputs.
- **Job Cohorts and Surrogate Models** (one switch) — group completed jobs into
  cohorts and fit **surrogate models** from them: fast statistical stand-ins that
  approximate a slow tool.
- **Design-space coverage** — checks whether a set of completed jobs is spread
  widely enough to build a trustworthy surrogate model from, and says so before
  anyone builds one. Needs *Job Cohorts and Surrogate Models* on as well.
- **Results warehouse** — collected key numbers (KPIs) from completed jobs.
- **File library** — shared, approved input files.
- **Materials database** — shared materials catalogue.
- **Verification and Validation records** — recorded tool checks.

**Favourites** is *not* one of these. It is a day-to-day site setting on the
General Settings page — see *Site settings and branding* — and your licence has
no say over it.

## Glossary of terms

- **On-premises** — software that runs on a machine you operate, not a shared
  external service.
- **The two services** — the web interface and job server (`matofx-api`) and the
  job runner (`matofx-runner`); both must run.
- **Database** — where MATOFX SPDM™ keeps its records; either the simple built-in file
  (SQLite) or a separate database server (PostgreSQL).
- **Reverse proxy** — software placed in front of MATOFX to provide encrypted
  (HTTPS) web access.
- **Audit log** — a tamper-evident record of who did what.
- **Concurrent Session Licence (CSL)** — a licence counting people **signed in at
  once**. A place frees up when someone signs out, or is signed out for
  inactivity. See *Your licence*.
- **Floating Access Licence (FAL)** — a licence counting people an administrator
  has **granted access** to. A place frees up only when that access is revoked.
- **Grace period** — the window after a licence expires during which everything
  still works and administrators are warned. After it the site becomes read-only.
- **Read-only mode** — everyone can sign in and view results, but nothing can be
  submitted, edited or granted. Reached by an expired licence or a failed
  integrity check.
- **Compliance report** — a signed file showing licence usage and record
  integrity, generated on request for your supplier. See *Licence compliance
  reports*.
- **Data lineage** — how a result traces back to its inputs.
- **Capability** — an extra right added on top of an account's role (see the
  capabilities reference above).
- **Storage location** — a shared folder path you register for jobs to run under;
  remote servers must be able to see the same path.
- **Always-on server (AOS)** — a persistent machine MATOFX logs into to run a
  tool's jobs, as an alternative to running on the MATOFX machine itself.
- **Credential reference** — the short name on a host row that points to a secret
  held in MATOFX's encrypted store, so passwords and keys never sit in the database.
- **Descriptor** — a tool's description file: its inputs, units, and file handling.
- **Output validity** — a tool's checks that a result is finite and within
  sensible physical bounds before it's kept.
- **KPI (key performance indicator)** — a headline number a tool marks for
  collection; the **results warehouse** gathers KPIs across completed jobs.
- **Job cohort** — a saved grouping of completed jobs for a tool, used to compare
  runs and to fit surrogate models.
- **Bundle** — a package of tools/workflows for moving between installations; also
  the installation package for the software itself.
- **Graphics processor (GPU)** — a specialised chip that speeds up certain
  calculations.
- **Computing cluster (HPC)** — a group of machines sharing out heavy work.
- **Tutorial** — a document or video you upload about your own tools, distinct from
  this built-in documentation.
