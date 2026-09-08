---
title: Where files live
audience: admin
capability: null
slug: where-files-live
summary: The folders MATOFX SPDM™ uses, registering storage locations and Python environments, setting the job working area, and the shared file area for users.
---

# Where files live

MATOFX SPDM™ stores several kinds of files on the machine. This chapter explains the
main locations and how to manage them from **Admin → Execution environments**.

## The folders MATOFX SPDM™ uses

MATOFX keeps separate areas for different purposes:

- **Job working folders** — one per job, created under a shared **storage
  location** (below) when the job runs.
- **Uploaded content** — tutorials, tool packages (bundles) and file-library
  files you and your users upload.
- **The database** — MATOFX's records (a SQLite file, or a separate PostgreSQL
  server; see *Installing and setting up*).
- **Logs and the audit signing key** — kept alongside the application.

Keeping them separate makes them easy to manage and back up.

## The Execution environments page

**Admin → Execution environments** is where you register *where* jobs run. It has
five tabs:

- **Storage locations** — the shared folder paths jobs run under (below).
- **Python envs** — the Python interpreters your tools may pin (below).
- **Always-on servers** and **HPC clusters** — remote machines to send jobs to;
  these are covered in *Running jobs on other servers*.
- **Runtime settings** — the job runner's own tunables (concurrency, heartbeat,
  cancel grace); the job-start budgets are covered in *Installing and setting up*.

Nothing here is ever hard-deleted — you **retire** or **deactivate** a row instead,
so historical jobs keep resolving to the location that produced them.

## Registering a storage location

A **storage location** is a shared folder path that jobs run under. You need at
least one before jobs can run, and remote servers require one both the MATOFX
machine and the remote can see (see *Running jobs on other servers*).

On the **Storage locations** tab, select **New storage location** and give it:

- a **Friendly name** (e.g. `fsx-prod`), and
- a **Mount path** — the folder as the MATOFX machine sees it (e.g. `/mnt/fsx` or
  a UNC share).

Make sure the path exists, has enough space for your workload, and — for remote
execution — that the remote host's service account can read and write it. To stop
new jobs using a location without losing history, **Retire** it.

## Registering a Python environment

Tools that pin a specific interpreter draw it from a **Python env** you register
here. On the **Python envs** tab, select **New Python env** and give it a name, the
**interpreter path**, and (optionally) the storage location it lives on. As with
storage locations, you **deactivate** rather than delete an env when it's no longer
used.

## Runtime settings

The **Runtime settings** tab holds the job runner's own tunables. Each row is
edited **inline** and validated as you save, so you can't set a nonsensical value.
They fall into three groups:

- **Resource budgets and concurrency** — how many jobs (and GPU jobs) run at once
  on the MATOFX machine, and the CPU-core and memory budgets a local job is checked
  against before it starts. These are the same job-start controls introduced in
  *Installing and setting up* — see that chapter for choosing sensible values.
- **Timing** — how often the runner re-reads a job's **heartbeat** (separately for
  local/always-on jobs and for HPC jobs, running or queued), and the **cancel grace
  period** it waits before forcing a cancelled job to stop.
- **Safety limits** — the **parametric-study concurrency cap** (how many of a
  study's runs may execute at once) and the **local job walltime limit** (after
  which a runaway local job is stopped and marked failed).

The shipped defaults suit most sites — change these only when you have a specific
reason, such as tuning throughput on a larger machine or reining in long-running
jobs.

## Setting the job working area

Each job runs in its own **working folder** under a storage location. That area
needs enough space for the jobs you run and should be included in your backups if
you need to keep job files.

### How much space, and keeping it under control

A job can produce far more during a run than is worth keeping, so MATOFX **cleans
up leftover files** when a job finishes. It automatically keeps each tool's
**declared outputs** (plus the job's inputs and records) and removes everything
else the run left behind — a tool's author can name extra files to keep on the
tool's **Inputs & Outputs → Advanced** tab. That keeps the working area from
ballooning — but it still grows with every job, and studies or optimisations
create *many* jobs at once. Size the area for your busiest workload, watch it over
time, and remember that a tool whose outputs are large (3D fields, animations)
will fill space faster than one that produces a few numbers.

Note that any **files a user uploads** as inputs are kept in full for the life of
the job's working folder — they're part of the job's record, so per-run cleanup
never removes them. A big uploaded model (CAD, mesh) therefore stays until the
job itself is cleared. That's reclaimed by the **automatic housekeeping** of old
jobs and files (set on the General Settings page), not by per-run cleanup — so if
uploads dominate your storage, tune the housekeeping retention windows.

## The shared file area for users

Where your site uses the **file library**, the shared, approved files users pick
from are also stored in a managed area. As with the job area, make sure it has
room and is covered by your backup policy.
