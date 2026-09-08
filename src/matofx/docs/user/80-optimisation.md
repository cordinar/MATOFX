---
title: Optimisation
audience: user
capability: null
slug: optimisation
summary: Letting MATOFX SPDM™ search automatically for the best inputs — setting goals and limits, choosing a search method (and knowing which to pick), seeding the search, deciding when to stop, and reading the results. Only available when switched on.
---

# Optimisation

> This feature is only available if your administrator has switched it on. If you
> don't see **Optimisation** in the menu, your site isn't using it.

## What optimisation does

Where a parametric study explores inputs *you* choose, **optimisation** goes a step
further: you tell MATOFX SPDM™ **what a good result looks like**, and it searches for
the inputs that achieve it — running the tool repeatedly, learning as it goes, and
homing in on the best answer.

Think of it as the difference between *"try these fifty fin heights and show me
what happens"* (a study) and *"find me the fin height that gives the lowest
temperature, and don't bother me with the rest"* (an optimisation).

## Setting up an optimisation

The **Optimisation** page has two tabs: **Catalogue** (browse your existing
optimisation studies) and **New Optimisation Study** (the create form). Open the
second tab to start one. You choose the **tool** (or workflow) to drive and give it
a name. You then work through a few steps: which inputs to vary, fixed values for the
inputs you aren't varying, your goals and limits, the search method, and how long to
let it run.

The **Catalogue** lists each optimisation study with its **target** (the tool or
workflow being optimised), **project** and **owner**. A **search box** filters by
name, target, project or owner as you type, and a **status** filter narrows to
**Draft**, **Running**, **Complete** or **Cancelled** (or **All**). Click any one to
open it; its header shows the target, pinned version and what's being optimised.
(These are *optimisation studies* — distinct from the **Parametric Studies** page,
which runs a tool across a fixed set of inputs without searching for a best.)

## Variables: which inputs to vary

You pick which of the tool's inputs the search is allowed to **change** (the
*variables*), and the range each may take. Everything you don't pick is held at a
single value for every run — you set those values on the next step.

A few practical points:

- **Inputs are grouped like the tool's own form.** The variable list — and the
  Fixed-inputs step below — are laid out under the same section headings the tool
  uses on its job form (for example *Geometry* and *Material*), in the same order,
  so you can find a parameter by its group. Inputs the tool didn't group appear
  under *Other inputs*. When you're optimising a **workflow**, each step of the
  chain is its own heading, with that tool's groups nested beneath it. Tools that
  define no groups show a plain flat list, as before.
- **Keep the variable count honest.** Every extra variable enlarges the space the
  search has to cover, so it needs more runs to do a good job. Vary the handful of
  inputs that genuinely matter rather than everything you *could*.
- **If something is wrong with a range**, the message names the input the same way
  the tick-box does — for example *Factor 'Inlet velocity': max is below min* — so
  you can go straight to the row it means.
- **Set realistic ranges.** The search will try the extremes of every range you
  give it. If a range is wider than is physically sensible, you'll waste runs on
  results you'd never use — and occasionally on inputs that make the tool itself
  struggle.
- **Whole-number inputs stay whole.** If an input is a whole-number quantity (for
  example *number of fins*), the search only ever tries whole values, and you must
  give it whole-number low and high bounds — a fractional bound is rejected when
  you launch. The search varies it one unit at a time; if you instead want it to
  try only a fixed set of values (say 2, 4 or 8), model that input as a *choice*
  rather than a whole-number range. The search never re-runs a design it has
  already tried, and if a small whole-number space has only a handful of possible
  designs it stops once they've all been explored rather than spending the rest of
  the budget on repeats.
- **Inputs that must add up to a total are kept consistent for you.** Some tools
  require a group of inputs to total a fixed value — for example three component
  percentages that must sum to 100. When you vary inputs in such a group, the
  optimiser keeps the total exact automatically: it varies the others and works
  one of them out so the sum always holds. You'll see a note on the Variables step
  listing the group, and you can set each input's range independently — you don't
  have to make the ranges add up yourself. (If you'd rather fix some members,
  hold them on the Fixed-inputs step; the optimiser balances whatever is left.)

## Fixed values for inputs you're not varying

A tool usually needs more than just its variables to run — think of a material
choice, a mesh setting, or any required input you've decided *not* to optimise.
The **Fixed inputs** step lists every required input you didn't pick as a variable
and asks you to give each one a value. That value is held constant across every run
in the campaign.

- **Materials and other rich inputs work exactly as they do on the normal run
  form** — for a material input you get the same "Pick material" chooser, and the
  choice is frozen into every run.
- **Defaults are filled in where the tool provides them**, so often you just glance
  over the step and continue.
- **You can't launch until every required input has a value.** If one is still
  blank, the study tells you which, and the same check runs again at launch — this
  is what stops a campaign from quietly producing a pile of failed runs because a
  material was never chosen.
- **Workflow optimisation** does the same thing per member: each tool in the chain
  contributes its own required inputs, shown grouped by member, except those fed
  automatically from an upstream tool.

This step only appears when there's actually something to set; if you're varying
every required input, it's skipped.

## Goals and limits, in plain terms

You describe two different things:

- **Goals** — what you want to make as good as possible. For each response you can
  choose **Minimise** (e.g. *lowest weight*), **Maximise** (e.g. *highest
  efficiency*), or **Track only** (just record it, don't chase it). You can have
  more than one goal.

  **Track only** is for a number you want to *watch* rather than pursue — the fan
  power a design happens to draw while you are minimising mass and temperature.
  It is recorded on every run and shown beside the goals in every results table
  and chart (see *Responses you asked it to track*), it can carry a **limit** like
  any other response, and it never influences the search. At least one goal must
  be Minimise or Maximise; a study of nothing but tracked responses can't start,
  unless you picked one of the *learn about the model* methods, which take no
  goal by design.
- **Limits** — the conditions a result must satisfy to count. A result that breaks
  a limit is rejected as **infeasible**, no matter how good its goal value looks.
  Limits come in three shapes:
  - a **range on an input** (a variable may only move between a low and a high
    value);
  - a **relationship between inputs** (for example, *fin height × fin count must
    stay under a value*);
  - a **limit on a result** (for example, *peak temperature must stay below
    85 °C*).

### One goal or several?

This single choice drives almost everything else, including which methods are
sensible:

- **One goal** — there is a single "best", and the search drives straight at it.
- **Two or more goals** — they usually pull against each other (lighter *and*
  cooler, faster *and* cheaper). There's rarely one winner, so MATOFX looks for
  the **trade-off front**: the set of results where you can't improve one goal
  without giving up another. You then pick the balance you prefer from that front.

You can still optimise several goals with a single-objective method — MATOFX
combines them into one score using the **weights** you set on each goal. That's
simpler to read but commits you to one balance up front. A multi-objective method
keeps your options open by handing back the whole front.

## Choosing a method

MATOFX offers several **search methods**, and they are not interchangeable — the
right one depends on how many goals you have, how many inputs you're varying, and
how slow the tool is. If you're unsure, leave the default (**Surrogate search /
Bayesian optimisation**) and read on only if you want to do better.

The page also shows a **Recommended** badge: based on the goals and variables
you've set up, MATOFX pre-selects a sensible method and explains why. You can
always override it.

### Which method, when

| If you have… | Use | Why |
|---|---|---|
| **One goal**, a slow/expensive tool, a modest number of inputs (~5–20) | **Surrogate search / Bayesian optimisation** | Builds a fast internal stand-in of the tool and uses it to spend each expensive run wisely. The most sample-efficient choice. |
| **One goal**, many inputs (20+), or inputs that are mostly whole-number/list choices | **CMA-ES** | A robust global search that keeps scaling where Bayesian search starts to struggle. |
| **One goal**, a bumpy or noisy response (lots of local dips) | **Differential evolution** | Population-based and hard to fool by local minima. |
| **One goal**, you want a familiar, transparent search | **Particle swarm** or **Genetic algorithm** | Well-understood classics; less sample-efficient than the above but easy to reason about. |
| **Two or three goals** to balance | **NSGA-II** | The standard multi-objective method; returns a well-spread trade-off front. |
| **Four or more goals** | **NSGA-III** (or **MOEA/D**) | Keep a diverse front when there are many goals, where NSGA-II thins out. |
| You just want a **baseline** or a quick sanity check | **Random search** or **Grid search** | Cheap controls — useful to confirm the setup works, or to compare a "real" method against. |

The single-objective methods are **Surrogate search / Bayesian optimisation,
CMA-ES, Differential evolution, Particle swarm** and **Genetic algorithm**. The
multi-objective methods are **NSGA-II, NSGA-III** and **MOEA/D**. If you switch
your goal count, MATOFX nudges the method to match (one goal → Bayesian, several →
NSGA-II) so you don't accidentally pair a single-objective method with a
trade-off problem.

### Acquisition: how Bayesian search picks the next run

When you choose **Surrogate search / Bayesian optimisation**, one extra control
appears — **Acquisition** — which decides how the internal stand-in chooses where
to run next:

- **Find the optimum (exploit)** — head straight for where the model predicts the
  best result. Fastest to polish a promising area, but can miss a better one
  elsewhere.
- **Balanced** *(the default)* — weigh "looks good" against "we're unsure here",
  so the search both improves and keeps exploring. The right choice for most
  problems.
- **Improve the model (explore)** — deliberately run where the model is *least*
  sure. This improves the stand-in itself rather than chasing the optimum — handy
  if you also want a trustworthy surrogate out the other end.

There's also a **Candidate pool per step** (default 512): how many trial points
the stand-in scores before committing to the next real run. Higher means a finer
search but slightly slower proposals; the default is fine for almost everyone.

### Dropping inputs that don't matter (MOP/CoP)

With Bayesian search you can tick **Enable MOP/CoP**. MATOFX then quietly fits a
few different stand-in models, keeps the best, and measures how much each input
actually affects the result. Inputs that barely move the needle are **frozen** so
the search can concentrate on the ones that count — which usually means reaching a
good answer in fewer runs. Leave it off if you're not sure; turn it on when you
suspect several of your variables are along for the ride.

## Seeding the search

Most methods begin with a spread of **exploratory runs** before the clever search
takes over — this "seed" stops the search from committing to one corner too early.
Bayesian search in particular needs a seed batch to learn from. A larger seed
gives a better start but uses up part of your run budget, so the default is a
sensible middle ground scaled to the number of variables.

## Deciding when to stop

An optimisation could run forever, so you set one or more **stopping rules** (any
one of them ending the search):

- **A run budget** *(required)* — stop after a set number of runs. This is your
  cost cap; everything else is optional on top.
- **A target** — stop early if a result reaches a value you'd be happy with (e.g.
  *temperature ≤ 80 °C*).
- **Convergence** — stop when the best result stops meaningfully improving over a
  run of attempts, so you don't pay for runs that aren't buying you anything.
- **A time limit** — a wall-clock safety net for a runaway search.

You can also **stop it yourself** at any time from the page. The run in progress
finishes, no new runs start, and everything found so far is kept.

> **Budgeting tip.** As a rule of thumb, give a single-goal search at least
> ~10–15 runs per variable before expecting a polished answer, and more for
> several goals. If each run is slow, that maths is exactly why the
> sample-efficient methods (and surrogate models) earn their keep.

## Jobs that need approval first

If the tool you're driving is set up to require approval, an optimisation can
launch a great many runs — so MATOFX may hold the whole study until a nominated
person approves it, showing them the run budget you've asked for. You don't need
to do anything except wait; the search starts automatically once it's approved.

Some tools also require approval **of the results** before they're shared. When one
of those is optimised, the finished campaign's results are **held until an approver
releases them** — the search completes, but the best design and charts stay hidden
until sign-off. Again, you only wait; you'll see the results once they're released.

## Running and watching progress

Once started, the optimisation runs on its own, submitting jobs as it searches.
The page shows its progress, including how the best result so far is improving and
how much of the budget remains. Each run is a normal job, so you can open any of
them to see exactly what was tried.

## Reading the results and charts

When it finishes, you can see the **best inputs it found**, along with charts
showing how the search progressed and — for several goals — how they **traded off**
against each other (the trade-off front). For a multi-goal study there isn't a
single "answer": you choose the point on the front whose balance suits you, and
MATOFX shows the inputs behind it. Every point traces back to the run that
produced it, so nothing is a black box.

### The setup, stated in full

The **Setup & outcome** card at the top of the study states the whole search
definition, so you can read what was run without reopening the wizard:

- **Every variable with the limits it was given** — the lowest and highest value
  the search was allowed to try, in the input's own units. An input that is a
  choice rather than a number lists the options it was allowed to pick from
  instead of a range. This is the difference between "it searched fin height" and
  "it searched fin height between 5 and 45 mm", and it's usually the first thing
  a reviewer asks.
- **Every input held constant, with the value every run used** — the values you
  set on the Fixed inputs step. A held material shows the material's name along
  with the exact version that was frozen into the runs.

Together those two lists are what makes a study reproducible: someone reading it
months later can see exactly what was varied, between what limits, and what was
held still while it happened.

A short **plain-language summary** sits at the top of a finished study: what it
was optimising and against which limits, the search method, how many runs it took
and why it stopped, and the best design it found (or, for several goals, the size
of the trade-off front). It's a quick read before you dig into the charts; every
figure comes straight from the search.

If the tool defines rules between its inputs (for example, one input that must
always be larger than another), the summary also notes how many proposed designs
were **skipped because they broke a rule** — the search simply tried other
designs instead, so this is informational, not an error.

### Responses you asked it to track

A response you set to **Track only** is recorded on every run and reported
alongside the goals, but it never influences the search:

- it gets its own column in the **Runs** table, after the goals, and in the
  **Download CSV**;
- it gets a column in the **Best designs / trade-off front** table too — which
  is where it usually earns its keep, since choosing between two designs that
  are equally good on the goals is exactly what a tracked value is for;
- it can go on any axis of the **design-space explorer**, or be used as the
  colour, under its own **TRACKED** heading.

What it never does is change the answer. A tracked response takes no part in the
trade-off front, the weighted score, or the ranking — turning a goal into *Track
only* and back leaves every one of those numbers exactly as it was. Nothing is
starred, ringed or ranked on a tracked value.

### How the weighted score is worked out

When a study has two or more goals, the **Best designs** table ranks runs by a
**weighted score**, and **lower is better**. It's worked out like this:

1. Each goal is rescaled across the runs on screen so its best value becomes
   **0** and its worst becomes **1**. This is what lets a mass in grams and a
   temperature in degrees be added together at all — without it, whichever goal
   happened to have bigger numbers would dominate the ranking.
2. A **Maximise** goal is flipped (`1 − value`), so after this step 0 always
   means "best" no matter which direction the goal points.
3. Each rescaled value is multiplied by that goal's **weight**, and the results
   are added up.

So a run scoring 0 would have to be the best run on every goal at once, which
normally can't happen — the score is a way of ranking compromises, not an
absolute measure. A run missing any goal value (a run that failed) is left out
of the ranking rather than scored as zero.

Two things follow that are worth knowing. The score is **relative to the runs
being shown**, because step 1 rescales against them — so it will shift as more
runs land, and it is not comparable between two studies. And the weights are
**yours to change**: edit them in the table and it re-ranks live, without
re-running anything. Only the *ratio* between weights matters; doubling them all
changes nothing.

In the **design-space explorer** chart, the axes and colour you pick are kept as
you refresh the page, so you don't have to set them up again each time. The
axis lists are split into **OBJECTIVES**, **TRACKED** and **FACTORS** sections —
your goals, the responses you asked it to record, and the inputs the search was
allowed to move — each under its own heading. A section with nothing in it isn't
shown. The headings are labels rather than choices: pick one and the list
simply returns to what it was.

Inputs that are a **choice** rather than a number — a material, a solver, a
coolant — can go on any axis, or be used as the colour. Each option gets its own
column, labelled with the option's name, and as a colour each option gets its own
solid band on the key. Options the search never tried still get a column, left
empty: that gap is worth seeing, because it tells you the answer you are reading
only covers the options that were actually run. If a run has no value recorded for
that input at all, it appears under *(not set)* rather than quietly disappearing.

## Continuing a finished study

If a study stops because it used up its run budget and you'd like it to keep
looking, you don't have to start over. On a completed study, set how many **extra
runs** to add and choose **Continue** — it picks up exactly where it left off
(keeping everything it already learned) and runs more batches until the new
budget, then stops again. This is the easy way to give a promising search more
room without losing its progress.

## Reusing what you learned

A finished optimisation is a ready-made batch of runs spanning the interesting
part of your input space — exactly what a **surrogate model** likes to learn from.
If your site has surrogates switched on, it's worth feeding a completed study into
one (see *Job cohorts and surrogate models*).
