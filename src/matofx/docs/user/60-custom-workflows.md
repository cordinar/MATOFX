---
title: Building your own workflows
audience: user
capability: null
slug: custom-workflows
summary: Grouping several tools into a workflow that runs in order, running it, watching its progress, the plain-language run summary, handling a failed step, and sharing it.
---

# Building your own workflows

A **workflow** lets you run several tools together, in the right order, instead of
launching each one by hand. Some workflows are built by your administrator;
this chapter is about the ones **you** can build for yourself.

## What a workflow is

A workflow is a small chain of tools where the output of one tool feeds into the
next. Rather than run three tools separately and copy results between them, you
run the workflow once and MATOFX SPDM™ passes the results along for you.

### When a workflow is worth it (and when it isn't)

Build a workflow when the **same chain of tools** runs together regularly and the
output of one is genuinely the input of the next — material properties feeding a
heat-transfer run, say. The pay-off is that you stop hand-copying results between
tools (where mistakes creep in) and you can re-launch the whole chain in one go.

It's *not* worth it for a one-off, or for tools that don't actually pass data to
each other — running them separately is simpler and easier to follow.

## Creating a custom workflow

From your home page (the **Custom Workflow** button) or **My custom Workflows**
in the menu, you choose **2 to 10 tools** you already have access to, and save
them together as a named workflow you can re-launch whenever you like.

## How MATOFX SPDM™ works out the running order

You don't have to wire the tools together by hand. MATOFX reads how each tool's
inputs and outputs relate and **works out the order** they need to run in — so
the tool that produces something runs before the tool that needs it. Tools that
don't depend on each other may run alongside one another rather than waiting in a
needless queue.

For MATOFX to chain two tools, the **output of the upstream tool must match an
input of the downstream one**. If a workflow doesn't pass data the way you
expected, that mismatch is usually why — the tools simply don't have a connecting
input and output, so MATOFX has nothing to hand along.

## Running your workflow

Launch the workflow like any other piece of work — in an existing project or a
new one. MATOFX starts the first tool, then the next, and so on.

The quickest way is the **Launch** button on each card in **My custom
Workflows** (it's there for workflows you built and for ones shared with you).
Your custom workflows also appear on the **home page** — in the **Workflow
Quick Access** card and in the **Create Job(s)** chooser — listed alongside the
workflows your administrator publishes. Only your own and shared-with-you
workflows show there; other people's custom workflows stay private to them.
It asks you to pick the **project** and **scenario** the run belongs to.
Scenarios that already have a run of this workflow are hidden — each scenario
holds one run of a given workflow — so if the workflow is already running
everywhere in the project you want, click **New scenario** to add a fresh one
and launch into that. Once it starts, MATOFX takes you straight to the run so
you can begin filling in the first step.

When you fill in a step's form, it behaves exactly as it does when you run that
tool on its own: the same required fields, the same value-range checks, and the
same **Submit** rules all apply, with one difference — fields fed automatically
by an earlier step are filled in and locked for you, so you don't enter them.
**Submit** stays greyed out, with a line explaining what's outstanding, until the
step is good to go.

## Watching a workflow run

A running workflow shows you **each tool's status** as it goes — which steps are
done, which is running now, and which are still waiting. You can open any step to
see its inputs and results.

Once the whole run finishes, a short **plain-language summary** appears at the top
— whether every step succeeded, the order they ran in, and how long the run took.
If a step failed, the summary names the first step that didn't complete.

## When a step needs approval

If one of the tools in the workflow is set up to need an **approver's sign-off**,
its tab tells you so before you submit it, and offers a **Justification** box
that goes to the approver. Once you submit, that tab reads **Pending approval**
and explains what happens next — it doesn't show the form again, because the run
is already with the approver and there's nothing more for you to fill in. The
steps after it stay locked meanwhile.

You don't need to watch it or refresh anything: the tab updates by itself the
moment the approval comes through, and the workflow carries on from there. See
*Running a tool* for the full picture on approvals.

## When one tool in a workflow fails

If a step fails, the steps that depend on it can't continue, and the workflow is
marked as failed. Open the failed step's **log** to see what went wrong, fix the
inputs, and run it again.

The same applies to a step that was **denied** at approval, or whose results were
**withheld** — those are final for that run. Any step depending on one says
plainly that it can't run, rather than sitting on "waiting" for something that
will never finish. Start a new run once the underlying issue is resolved.

## Sharing a workflow with named colleagues

A workflow you've built can be **shared** with specific colleagues by name, so
they can launch the same chain of tools without rebuilding it themselves.
