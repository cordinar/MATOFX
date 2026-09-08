---
title: Comparing jobs
audience: user
capability: null
slug: comparing-jobs
summary: Putting several jobs side by side — overlaid charts, value differences, comparing across scenarios and workflows, and saving a comparison.
---

# Comparing jobs

Engineering work is full of "what changed?" questions. **Compare Jobs** lets you
put several runs next to each other and see the differences at a glance.

## Why compare

Comparing is how you answer questions like *"how did doubling the thickness
change the result?"* or *"which of these three designs performed best?"* — without
flipping back and forth between separate job pages.

## Choosing jobs to compare

Open **Compare Jobs** from the left-hand menu and pick the jobs you want to look
at together. At the top of the **Select** tab you choose *what kind* of thing to
compare (jobs of one tool, scenarios, or runs of one workflow), then use the
selectors on the controls row to narrow down — for jobs, that's the scope ("all I
can access" or "jobs I submitted") and the tool.

Each item in the list shows its **name** with its job number and project /
scenario beneath, so you can tell similar runs apart at a glance before ticking
them. When the list is long, use the **Search** box on the controls row to filter
it by name, number, or project / scenario. A line above the list shows how many
items there are and how many you've selected, and **Clear** unticks everything in
one click.

The comparison builds **as you tick** — there's nothing to submit. Open the
**Compare** tab whenever you like and it shows the runs currently selected; the
**"Compare these"** button is just a shortcut that jumps you there. You need at
least two runs (and at most six) for a comparison to appear.

You can also start a comparison straight from **My Jobs** — tick the runs there
and choose to compare them, and Compare Jobs opens with those runs already
selected. Either way, your selection **sticks**: flip to the **Compare** tab to
see the results, then back to **Select**, and the same runs are still ticked.
Your picks only clear when you leave the Compare Jobs page or switch to a
different tool (the comparison wouldn't make sense across tools).

A comparison is only as meaningful as the jobs you put in it. For a fair "what
changed?" read:

- **Compare like with like.** Put runs of the **same tool** (and ideally the same
  tool version) side by side. Comparing two different tools' outputs rarely tells
  you anything — their numbers don't mean the same thing.
- **Change one thing at a time.** The clearest comparisons differ in a single
  input. If two runs differ in five inputs, you can see *that* the result changed
  but not *why*.
- **Two for a difference, several for a trend.** Two runs answer "did this change
  help?"; a handful of runs across a sweep show you the shape of the trend.

## Overlaying results on one chart

Where the jobs produced charts, MATOFX SPDM™ can **overlay** them on a single set of
axes, so the lines sit on top of each other and the differences jump out.

## Side-by-side differences

For values and inputs, MATOFX shows the figures **side by side**, highlighting
where the runs differ. This is the quickest way to confirm exactly what was
changed between two runs.

**Material inputs** are shown as a compact card per material — the material's
name, version and class, with its frozen properties listed as *Property: value
unit* — rather than as raw data, so a change of material (or of a material's
properties) reads at a glance. This applies in both the on-screen view and the
downloaded report.

## Animations

When a tool produces a **time-resolved field** — for example a transient
temperature field that evolves over the run — the animation appears **in that
run's Outputs card**, in the same place as every other output, so it sits
directly alongside the other runs' animations. Press play (or drag the step
slider) on each to scrub through the series and watch how the runs diverge over
time, and use **Download animation (HTML)** to save the whole series as a single
self-contained file — exactly the one offered on the job's own page.

## Comparing across scenarios or workflows

You're not limited to jobs in a single place. You can compare runs grouped by
**scenario** or by **workflow**, which is useful when the same study was carried
out in several scenarios. Comparing by workflow lines up the matching **step** in
each run, so you can see how one stage of the chain behaved across several
attempts rather than comparing whole workflows as a lump.

> If you find yourself comparing the *same* set of runs again and again, it's a
> sign the question is really a **parametric study** or an **optimisation** —
> features built to vary inputs systematically and gather the results for you.

## Saving or sharing a comparison

When a comparison is worth keeping, you can save or share it as a **report**, so
a colleague sees exactly the same side-by-side view you set up.

There are two flavours. The **HTML report** is fully interactive — it includes
the **field animations** and any **videos**, which play right inside the file
when a colleague opens it in their browser. The **PDF report** is for printing
and archiving, so it leaves those out (a printed page can't play them) and keeps
the charts, tables and images that do make sense on paper.
