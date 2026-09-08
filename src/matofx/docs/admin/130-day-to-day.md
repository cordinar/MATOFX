---
title: Looking after MATOFX SPDM™ day to day
audience: admin
capability: null
slug: day-to-day
summary: Routine checks, the job queue, computing resources, running jobs on other machines, licensing, logs and troubleshooting, and getting support.
---

# Looking after MATOFX SPDM™ day to day

Once MATOFX SPDM™ is running, keeping it healthy is mostly light-touch. This chapter
covers the recurring tasks.

## Routine checks

A quick periodic check is worth doing: confirm both services are running (see
*Installing and setting up* for starting, stopping and checking them), that the
site responds, that each storage location has space, and that nothing alarming is
appearing in the logs. The **Usage statistics** and **Explore database** pages
(under Admin → Monitoring) give you queue depth, active users and recently failed
jobs at a glance — see *Monitoring and records*.

## The job queue and freeing stuck jobs

Submitted jobs wait in a **queue** and run in turn. To see what's in flight across
the whole site, use the **Live Jobs** view (part of Usage statistics), which lists
running jobs, studies and optimisations and lets you **cancel** one where you have
permission.

If a job appears **stuck**, look first at the job's own **log** and **state** —
the **Explore database** page can show a job's captured output (stdout/stderr) and
flag failed jobs, queue depth and any orphaned working folders. Once you know why,
cancel the job from Live Jobs (or the job's own page) so the queue keeps moving.
Leftover working folders from a restored or rolled-back database are moved aside
into an `_orphaned/` sub-folder rather than overwritten — those are safe to inspect
and remove (see *Running jobs on other servers*).

## Computing resources: processors and graphics processors

Jobs use the machine's **processors** (its CPUs). Some heavy jobs can also use
**graphics processors (GPUs)** — specialised chips that speed up certain kinds of
calculation. Make sure the machine has the resources your tools need.

## Running jobs on other machines

MATOFX can run jobs not just on its own machine but also on:

- an **always-on job server** — a separate machine that's kept running to take
  jobs, and
- a **computing cluster** — a group of machines that share out heavy work (often
  called *High-Performance Computing*, or HPC).

This lets you scale beyond a single machine when your workload grows. Setting these
up is an installation-time task.

## Licensing

Depending on your agreement, your MATOFX site may run under a **licence** that sets
limits such as how many users it covers. Keep your licence current so the site
keeps working as expected.

## Logs and troubleshooting

Both services write **log files**. When something misbehaves, the logs are the
first place to look — they record what each service was doing and any errors.
Alongside them, three in-app records help you narrow a problem down:

- a **job's own log**, on the job page, for a single run that failed;
- the **audit log** (Admin → Monitoring), a tamper-evident record of who did what,
  with an on-demand chain **Verify**; and
- **Explore database** (Admin → Monitoring), a read-only look at failed jobs,
  queue depth, active users and orphaned folders.

See *Monitoring and records* for how to use each.

## Getting support from Funis

For problems you can't resolve, contact **Funis**. Having your version number, the
relevant log extracts, and a clear description of what happened to hand will get
you a faster answer.
