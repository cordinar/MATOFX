---
title: Approvals
audience: admin
capability: null
slug: approvals
summary: How approvals work, requiring approval before a tool or study runs, and choosing approvers.
---

# Approvals

Approvals let you put a **sign-off step** in front of certain work, so a nominated
person reviews it before it runs.

## How approvals work

A tool's approval policy can gate work at two points: **before it runs**
(approve-to-run) and **before its results are shared** (release-results) — either,
both, or neither. A gated submission **waits** until an approver reviews it. The
approver is notified (by email, if approval notifications are switched on — see
below — and always in their in-app Approvals inbox), opens it, and **approves** or
**rejects**. On approval the work continues (or its results are released) on its
own; on rejection it stops. The submitter doesn't have to do anything but wait.

## What the approver reviews

The approver opens the waiting item and sees exactly what's about to run — the tool,
the inputs, and who submitted it — before deciding. Everything is shown in **plain
terms**: each input by its label and unit (never an internal name), so the review
reads the way the form does.

For an **optimisation** the panel is fuller, because approving one signs off not a
single run but potentially hundreds:

- the **design variables** being swept and their ranges (or the choices, for a
  pick-list variable);
- the **objectives** (what's being minimised or maximised) and any **constraints**,
  in words — "Pressure drop ≤ 0.5 bar";
- the **strategy** and **run budget**; and
- an **estimated compute footprint** — roughly how many runs the campaign will
  launch and what each one costs — so the blast-radius is clear up front.

For a **study or optimisation that has finished** and is waiting on a results
release, the panel shows a **condensed table of every run** — one row each, with the
varied inputs and key results, and failed or unusual runs flagged — plus the
min/max/average across them. One decision releases (or withholds) the whole set;
click any row to see that run's values in full.

Encourage approvers to reject with a short reason rather than silently; the
submitter sees it and can fix the setup.

## Deciding many at once

When several items are waiting, the approver needn't open each one. **Select all
shown** marks every item currently in view to approve (or to deny with one shared
reason), and a single **Submit** applies the batch — each item still succeeds or
fails on its own. Approving in bulk asks for confirmation first, since it skips
opening each panel. Filtering the list first narrows what "all shown" covers.

## Requiring approval before a tool or study runs

You decide which tools (and, where applicable, studies and optimisations) need
approval. For those, every submission goes through the approval step before any
work is done. Reach for it where it earns its place:

- **Expensive runs** — anything that ties up a lot of compute, where a wrong input
  wastes real money.
- **A whole optimisation or study** — which can launch a great many runs at once,
  so the budget is worth a glance first.
- **Safety- or compliance-critical work** — where a second pair of eyes is part of
  the process, not just a nicety.

Approval adds friction, so don't require it on quick, cheap, everyday tools — it'll
only train people to rubber-stamp.

A tool set to gate **results** (release-results, or both gates) is now honoured
inside an **optimisation** too: when the campaign finishes, its results are held
until an approver releases them, rather than appearing the moment each run lands.
The compute-cost start gate stays a separate sign-off, addressed to your nominated
optimisation approver.

## Choosing approvers

You nominate which users are **approvers** and which tools each is responsible for
(see *People and access*). First tick **Can approve runs** on the person's account,
then pick them in the tool's approval settings. When a job, study or optimisation
results release needs sign-off, the right approver is asked.
Approvers see what's waiting for them on their home page.

## Email notifications for approvers and curators

When something is submitted for approval, MATOFX can **email the person whose
decision it needs** — the nominated approver for a job, study or optimisation, or
the chosen curator for a global library file, a material change, a surrogate model,
or a V&V record. The email names the item and links straight to the Approvals
inbox.

You control this on **Admin → General settings**, under *Approval notifications*:

- a **master switch** turns every approval email on or off at once; and
- a **per-type switch** for each kind of approval (jobs, library files, materials,
  surrogate models, V&V records, parametric studies, optimisations), so you can
  silence just one kind.

These are separate from the job-state emails (Running / Complete / Failed) — turning
those off does not silence approval emails, and vice versa. Either way, approvers
and curators always see pending items in their in-app Approvals inbox, so nothing is
missed when email is off. (If the email subsystem itself is disabled in the
configuration file, no email is sent regardless of these switches.)
