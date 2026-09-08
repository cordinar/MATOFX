---
title: Building workflows
audience: admin
capability: null
slug: building-workflows
summary: Creating an administrator workflow, choosing its tools and order, passing results between tools, releasing it, and inspecting users' custom workflows.
---

# Building workflows

A **workflow** chains several tools together so they run in order. As an
administrator you can build official workflows for your whole site, in addition to
the personal ones users build for themselves.

## What an administrator workflow is

An administrator workflow is a released, site-wide chain of tools that appears in
users' Quick Access lists, ready to launch — like a tool, but it runs several
steps. On the Quick Access card a workflow lists the **tools it contains**, in
the running order you set, so users can see at a glance what it does. An arrow
(`→`) between two tools means the first feeds its result into the second; a dot
(`·`) marks tools that aren't directly linked (for example two independent steps,
or one that simply runs after another without passing data). The order shown is
exactly the order users meet the steps when they launch the workflow.

## Creating a workflow and choosing its tools

In **Admin → New/edit Workflow**, create a workflow and choose the **tools**
that make it up.

## Setting the running order

You arrange the **order** the tools run in. MATOFX SPDM™ also reads how the tools'
inputs and outputs relate, to help get the order right — a tool that produces
something is placed before the tool that needs it, and steps that don't depend on
each other can run alongside one another rather than waiting needlessly.

## Passing results from one tool to the next

Where one tool's output feeds the next tool's input, MATOFX **passes the result
along** automatically, so the chain runs end to end without manual copying.

This link is set up on the **downstream tool**, not on the workflow. In the tool
editor's **Inputs** tab, tick **"Auto-populated from upstream tool"** on the input
you want filled in, then choose two things:

1. **Which tool** supplies it. The list only shows tools that have agreed to be a
   source for this one. If the tool you want isn't listed, use **"Make another tool
   a source"** to add it — MATOFX wires up the agreement on both sides when you
   save (which also nudges the source tool's version up by a small amount).
2. **Which output** of that tool feeds the input. You pick the output by its
   label, so the two no longer have to be named identically for the result to flow
   — you decide explicitly what connects to what.

Make sure the output and input describe the **same quantity in compatible units**.
If a chain isn't passing data the way you intended, an output wired to the wrong
input — or a unit mismatch — is the usual cause. The fix is on the *tools* (in the
tool editor), not on the workflow.

### Passing result **files** between steps

This works for **result files**, not just numbers. If an upstream tool produces a
file output — a mesh, a result field (such as a nodal temperature map), a CSV — you
can wire it into a downstream tool's **file input** exactly the same way: tick
**"Auto-populated from upstream tool"** on the file input and pick the upstream tool
and its file output. When the workflow runs, MATOFX moves the actual file along for
you, so the downstream step reads it without anyone hand-building an intermediate
file or re-uploading anything. A time-resolved field (a series of snapshots, one per
timestep) is carried across whole; the downstream tool receives the representative
snapshot and can read the rest of the series alongside it.

As with numbers, **compatibility is the tools' job**: MATOFX transports the bytes
but doesn't reconcile file formats or mesh layouts. Wire a file output into a file
input the downstream tool actually knows how to read, and — for a result field —
make sure both steps share the same mesh.

When a file input is wired this way, the person running the workflow sees it
**locked** — the field says it's *provided automatically by an upstream tool* and
offers no upload or file-picker. This mirrors how wired number inputs are locked,
so nobody can accidentally override a value the workflow supplies.

> **Tip — without a workflow.** If someone wants to feed one tool's result file
> into another tool as a one-off (not in a workflow), they don't need wiring at
> all: on the downstream tool's file input they can choose **"Use a file from
> another job"** and pick that upstream job's output directly. See the user guide,
> *Running a tool → Reusing a file from an earlier job*.

## When a step fails

A workflow is only as complete as its steps. If a step fails, the steps that
depend on it can't run and the workflow is marked failed; independent branches
that had already finished keep their results. Point users at the failed step's
**log** — the workflow itself rarely fails for its own reasons, almost always
because one tool in it did.

## Releasing a workflow

Like a tool, a workflow has states. When it's ready, you **release** it so users
can launch it. Until then it stays out of their lists.

A workflow is never more visible than the tools inside it. Even after you release
it, a workflow stays hidden from any user who can't see **one of its member
tools** — because that tool is still pre-release (`new`/`test`), or is restricted
to a team they don't belong to. Such a user won't see the workflow anywhere:
not in Quick Access, not in the scenario chooser, and they can't launch it. The
workflow reappears for them once every member tool is visible to them (the tool is
released, or they're added to its team). So if a released workflow isn't showing up
for the people you expect, check the **states and teams of the tools it contains**,
not just the workflow's own state.

## Inspecting users' custom workflows

Users can build their own workflows. The **Custom Workflows (inspector)** page lets
you see what they've built — useful for support and oversight.

By default the page lists **every** custom workflow in the system, from all owners
(your own appear under *My custom workflows*). Narrow the list with:

- **Search** — a free-text box at the top of the Filters card. Type a workflow name,
  an owner (username or full name), or a tool; a row matches if any of those contain
  what you typed. The search is *additive* to the dropdowns below it.
- **Owner** — restrict to a single user's workflows. Leave it on *(any owner)* to see
  everyone's.
- **Contains tool** — restrict to workflows that include a particular tool.
- **Include archived** — on by default, since troubleshooting often involves an
  archived row.

Each row shows the workflow's name, its owner, and the **tools it chains, in run
order** — so you can see what a workflow does without opening it. An archived workflow
is marked with an amber **Archived** chip.

For any row you can **View details** (tools in run order, share list, lineage and a
structure preview), **Make a copy** (forks it into your own list so you can reproduce
an issue without touching the original), and — for an active workflow — **Archive…**
on the owner's behalf with a required, audit-logged reason. For a workflow that's
already archived, **Release** returns it to active on the owner's behalf (also
audit-logged).
