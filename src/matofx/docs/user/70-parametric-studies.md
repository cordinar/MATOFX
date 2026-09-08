---
title: Parametric studies
audience: user
capability: null
slug: parametric-studies
summary: Running a tool many times across a range of inputs — choosing how the runs are spread (a run table, a grid, a screening or space-filling design), keeping the run count sensible, reading the results, and the input-influence measures (Sensitivity / ANOVA / Sobol) and how they differ. Only available when switched on.
---

# Parametric studies

> This feature is only available if your administrator has switched it on. If you
> don't see **Parametric Studies** in the menu, your site isn't using it.

## What a parametric study is

A **parametric study** runs the same tool **many times**, each time with slightly
different inputs, so you can see how the results change as you vary one or more
values. Instead of submitting fifty jobs by hand, you describe the range you want
to explore and MATOFX SPDM™ generates and runs them for you.

The difference from **optimisation** is intent: a study *explores* a space you
define and shows you everything; optimisation *searches* for a single best answer
and skips the rest. Use a study when you want to understand behaviour; use
optimisation when you just want the winner.

## Setting up a study

The **Parametric Studies** page has two tabs: **Catalogue** (browse your existing
studies) and **New Parametric Study** (the create form). Open the second tab to
start one. You choose the **tool** to run, give the study a name, pick **how the
runs are spread** (the mode, below), and then say which inputs to vary.

The **Catalogue** lists each study with its **tool**, **project**, **owner** and
run count. A **search box** filters by name, tool, project or owner as you type,
and a **status** filter narrows the list to **Draft**, **Running**, **Complete**
or **Cancelled** runs (or **All**). Click any study to open it.

## Choosing which inputs to vary

You pick which of the tool's inputs should **change** across the study, and the
range or set of values each should take. The inputs you don't vary stay fixed at
the value you set. As with optimisation, vary the inputs that matter — every extra
varied input multiplies the number of runs.

**Inputs are grouped like the tool's own form.** The factor list is laid out under
the same section headings the tool uses on its job form (for example *Geometry* and
*Material*), in the same order, so a long input list is easy to scan and you can
find the parameter you want by its group. Any inputs the tool didn't put in a group
appear together under *Other inputs*. Tools that don't define groups show a plain
flat list, exactly as before.

**Ranges stay within the input's limits.** When an input declares a minimum and/or
maximum, the range or values you sweep must sit inside those limits — the form
holds the min/max boxes to that band, and a study that somehow steps outside is
refused with a message naming the input. This isn't red tape: a sweep that runs
past a limit used to be quietly pulled back onto it, which piled a cluster of runs
onto the boundary and skewed the whole sampling plan. Keeping the sweep inside the
limits keeps the spread honest.

**File inputs can vary too.** In the **Run table**, **Value list** and **Grid**
modes a file input can vary over a **set of file references** (a library CAS hash
or a path, comma-separated); the study runs once per file, crossed with the other
factors. The space-filling and response-surface modes can't vary a file.

## Choosing how the runs are spread

This is the choice that most affects what you get out, and the one the original
short version of this page glossed over. MATOFX offers several **modes**, grouped
by what they're good for:

### When you already know the runs you want

| Mode | What it does | Use it when |
|---|---|---|
| **Run table (exact configurations)** | You type each run as a row; MATOFX runs exactly those rows, no automatic combinations. | You have a specific list of named configurations to try. |
| **Value list (all combinations)** | You give a set of values for each chosen input; MATOFX runs **every combination**. | You want a full sweep over a handful of specific values. |

In the **run table**, each input you marked *Vary* becomes a column. The column
headings use the input's **name and units** exactly as they appear in the tool's
own form — for example *Inlet velocity (m/s)* — so you can check you are typing
values into the right column. Use **Add run** for each extra row, and the bin
icon at the end of a row to remove it.

### When you want a grid

| Mode | What it does | Use it when |
|---|---|---|
| **Grid (full factorial)** | Per numeric input: a min, a max, a number of levels and a spacing; runs every combination of levels. | You want an even, exhaustive sweep over a few inputs. |

A grid is thorough but grows **explosively**: three inputs at five levels each is
already 125 runs (5 × 5 × 5). Keep the number of varied inputs small.

### When you want to find out which inputs matter (screening)

| Mode | What it does | Use it when |
|---|---|---|
| **Fractional factorial** | A 2-level (low/high) screening design at a chosen resolution. | You have many inputs and want to find the few that matter, in **far fewer** runs than a full grid. Numeric inputs only. |
| **Plackett–Burman** | A compact 2-level screening design; the run count is set automatically. | Same goal, even fewer runs — a quick first sift. Numeric inputs only. |

### When you want to fill the space evenly (space-filling)

| Mode | What it does | Use it when |
|---|---|---|
| **Latin hypercube** | Spreads a set number of samples evenly across the whole space. | You want broad, even coverage with a run count *you* choose — and a great basis for fitting a surrogate model later. |
| **Sobol sequence** | Low-discrepancy space-filling; most even when the run count is a power of 2 (8, 16, 32…). | Like Latin hypercube, with very even coverage. |
| **Random** | Independent random draws within each input's range. | A simple baseline, or when you genuinely want randomness. |

### When you want to fit a smooth response surface (RSM)

| Mode | What it does | Use it when |
|---|---|---|
| **Central composite (RSM)** | Corner, axial and centre points to fit a **curved** response surface. Numeric inputs only. | You want to model how a result curves across two or more inputs. |
| **Box–Behnken (RSM)** | A 3-level response-surface design; needs at least 3 numeric inputs. | Same goal, avoiding the extreme corners of the space. |

> **Not sure?** For "how does the result behave across this range" with a couple
> of inputs, a **Grid** or **Latin hypercube** is the easy default. For "which of
> my many inputs actually matter", start with **Fractional factorial** or
> **Plackett–Burman**. To later fit a fast stand-in model, **Latin hypercube** is
> the natural feeder.

### Spacing and distributions

A **grid** *places* discrete levels, so for a numeric input you choose a
**spacing** — Linear (even), logarithmic (dense near the min or near the max), or
dense at the edges / in the middle — to concentrate levels where the interesting
behaviour is. A **space-filling** mode instead *draws samples from a density*, so
you choose a **distribution** (Uniform, Normal, Log-uniform) per input. These are
the same idea in two idioms, and they line up:

| Grid spacing | Space-filling distribution |
|---|---|
| Linear (even) | Uniform |
| Log — dense near min / max | Log-uniform |
| Dense in the middle | Normal |

**Normal is truncated to the input's limits.** A normal distribution has no
natural bounds, so on its own it would draw the occasional value far past an
input's min/max — which then piled up on the limit. MATOFX instead samples a
*truncated* normal: the bell shape is kept but confined to the input's declared
range, so no run lands outside the limits and nothing stacks on a boundary.

**By values, in either idiom.** Just as a grid input can be given an explicit
**value list** instead of a range, a space-filling input can be switched from *By
distribution* to *By values* — the sampler then draws from that discrete set.
Useful when one input only takes a few meaningful values while the rest sweep
continuously.

**Whole-number inputs** (for example *number of fins*) only ever take whole
values. If you ask for more levels than there are whole numbers in the range — say
ten levels between 1 and 3 — MATOFX runs the distinct whole values that exist (1,
2, 3) and tells you it did so, rather than repeating runs.

### The values MATOFX picks

Generated values respect **the resolution the tool declares**. If an input steps
in tenths, the study asks for `24.6` — not `24.606065714271846`. A tool cannot act
on digits it doesn't have, and quoting them would put numbers in the run record
that the run never really used.

Integer inputs are handled the same way: they are only ever asked for at whole
numbers, and a range too narrow to contain one is rejected rather than rounded
into something you didn't ask for.

The same rule applies to the designs an **optimisation** proposes.

## Keeping the run count sensible

Because every varied input multiplies the runs, studies can balloon into hundreds
or thousands of jobs. MATOFX shows the **run count** as you set things up and your
site has a **cap**: if you exceed it, MATOFX asks you to reduce the ranges or
levels (or an administrator to raise the cap) before you can launch. Treat a
sudden jump in the count as a prompt to drop an input or use a screening design.

## Previewing the runs before you launch

As you set the study up, MATOFX shows a live **run count**, and **Preview** opens
the full matrix of runs it would generate — before a single job is submitted. The
preview has two views:

- **Table** — every generated run, one row per job, with the input columns shown by
  their labels and units. **Download CSV** saves it.
- **Plot** — a scatter of the generated points over **up to four inputs at once**:
  pick an **X** and **Y**, optionally add a **Z** to make it 3-D, and optionally
  colour the points by a fourth input. This is the quickest way to *see* how the
  inputs actually vary — an even spread reads as an even cloud, and a lop-sided
  plan (everything bunched along one edge, or a distribution that isn't doing what
  you expected) jumps straight out. Only numeric inputs can be plotted; the axes
  default to the inputs that actually vary.

  In a **grid (full factorial)**, several runs land on the same plotted position —
  they differ only on the inputs you didn't put on an axis — which would otherwise
  hide their colours under one another. MATOFX fans those overlapping runs out into
  a small cluster so each colour shows; the hover always reports each run's true
  values. Space-filling plans never overlap, so they're drawn exactly.

Previewing is the moment to catch a sampling plan that isn't spreading the way you
intended, while it's still free to change.

## Running and watching progress

MATOFX works out the full set of runs from your choices, then submits them. The
study page shows how many runs have completed and how many are still going. Each
run is an ordinary job you can open individually.

## Reading the results

A **Study settings** panel sits at the top of every open study, on both the
Progress and Results views. It records exactly how the study was set up — the
mode, the run cap, how many runs were generated (and how many points were skipped
by the tool's cross-field rules), the seed and sample count for a space-filling
study, and every input's role: held constant at a value, or varied over a stated
range, value list, distribution, option subset or file set. The inputs are laid out
under the same group headings the tool uses on its form (the ones you saw when
choosing what to vary), so the summary reads in the same order and structure as the
setup. It reads back in plain language with the inputs shown by their labels and
units, so months later you can see at a glance what a study actually did without
decoding its configuration.

When the runs finish, the study gathers them together so you can see how the
results responded to the inputs you varied — the heart of what a parametric study
is for. Depending on the mode, you can read this as a table, as charts of a result
against a varied input, or — for the response-surface modes — as a contour of how
a result varies across two inputs at once.

At the top of the results, a short **plain-language summary** describes what the
study found — how many points ran, the range a result spanned, and the headline
finding from the analysis. The wording follows the method you chose: a
space-filling or screening study reports which inputs matter most; a
response-surface study reports how well the surface fitted and where its best
point sits. Every figure in the summary comes straight from the runs.

If any run produced **non-physical** results (a value that came back as
not-a-number or outside its sensible range — usually a solver that didn't
converge for that combination of inputs), the results table flags it in a
**Health** column with a one-line summary of how many are affected. These runs
are kept so you can inspect them, but they're left out of the analysis and of any
surrogate or optimisation built from the study — so a few bad points can't quietly
distort the picture.

### Plotting an input that is a choice

Inputs that are a **choice** rather than a number — a material, a solver, a
coolant — can now go on any plot axis and be used as the colour, on both the
scatter and the parallel-coordinates chart. Each option gets its own column,
labelled with the option's name, and as a colour each option gets its own solid
band on the key; hovering a point names the option rather than showing a
position number.

Options the study never ran still get a column, left empty. That gap is worth
seeing: it says the conclusions you are reading only cover the options that were
actually swept.

A study that used a **single** option still shows it. You chose that option, and
one labelled column stating that every run used it is worth having — both on
screen and in a screenshot months later. The same goes for a numeric input you
held constant: it is offered as an axis and draws as one flat line, which is a
true statement about every run in the study.

### Finding an axis in the picker

The X / Y / Z / colour lists are split into an **INPUTS** section and an
**OUTPUTS** section, each under its own heading, with the inputs in the same
order as the tool's own form. The headings are labels rather than choices — if
you pick one, the list simply returns to what it was.

The parallel-coordinates chart draws its axes in that same order, so every input
appears before the first result, left to right.

Because held-constant inputs get an axis too, that chart can end up with a row
of flat lines beside the few that actually vary. **Hide unvaried axes** drops
them for as long as it is ticked. It starts unticked — the inputs your study
chose are worth seeing until you say otherwise — and it never hides everything:
a study where nothing varied still draws.

One deliberate limit: **input influence, interactions and the prediction
profiler still work on numbers only**, so a choice-valued input won't appear
there. Those analyses fit and rank a response against a numeric range, and there
is no meaningful "range" between two materials. Use the plots to compare options,
and read the influence ranking as a statement about the numeric inputs.

## Which inputs matter (input influence)

A key question a parametric study answers is **which inputs drive the result, and
which way**. MATOFX offers a few measures of this, and they're *complementary, not
duplicates* — each looks at influence through a different statistical lens, so they
usually agree and, where they disagree, that itself tells you the response is
non-linear or driven by interactions. To keep things simple, the results lead with
the one that best fits your study's design and tuck the rest under **Advanced
statistics**. Each card explains what it is and when to rely on it:

- **Sensitivity** — a quick, always-available ranking pulled straight from the runs.
  It tells you the **strength and direction** of each input's effect (does raising it
  raise or lower the result?). Best when the result changes smoothly. This leads for
  space-filling and table/list studies.
- **Main effects & ANOVA** — splits how much of the variation each input (and each
  pair of inputs) explains. Best for **designed** studies with repeated, discrete
  levels (grid, factorial, screening); it captures interactions but not a single
  direction — read the main-effects plot for that. This leads for designed studies.
- **Global sensitivity (Sobol)** — the most thorough measure when the result is
  non-linear: it counts everything an input does, *including through interactions*,
  which the plain Sensitivity can't see. It needs a fitted surrogate model, so it
  appears once one is built. Always under Advanced statistics.

## Exploring the fitted surface (prediction profiler)

Once a study has enough runs, the results include a **prediction profiler** — a
quick surrogate fitted on the spot so you can explore *between* the points you
actually ran. Each response gets a small chart per varied input; drag the
**sliders** to move the operating point and every chart re-predicts instantly, so
you can see how a result responds without launching more runs. **Set to optimum**
drives the sliders to the input combination that maximises (or minimises) the
response you choose.

Predictions are **clamped to a result's declared physical range** (the min/max an
administrator set on that output). A surrogate is an unconstrained model, so left
to itself it can predict a value the quantity can't take — a negative failure
index, say. When a prediction would fall outside that range it's pinned to the
nearest limit and marked *"clamped to physical range"* — which is also a useful
signal that the model is extrapolating beyond the runs it learned from.

For a model you want to keep, publish, reuse or export as a tool — rather than a
quick on-page explore — use the **Build a surrogate** button (shown once the study
has finished, even if a few runs failed). See *Job cohorts and surrogate models*.
