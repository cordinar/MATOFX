---
title: Job cohorts and surrogate models
audience: user
capability: null
slug: cohorts-and-surrogates
summary: Building a batch of runs to learn from, choosing and fitting a surrogate model (and knowing which type to pick), reading its accuracy, making predictions with uncertainty, viewing predicted fields in 3D, and publishing for shared use. Only available when switched on.
---

# Job cohorts and surrogate models

> This feature is only available if your administrator has switched it on. If you
> don't see **Job Cohorts** or **Surrogate Models** in the menu, your site isn't
> using it.

## What these are, simply

A slow tool can take a long time for every run. A **surrogate model** is a fast
**stand-in** that learns from a batch of real runs and can then *predict* what the
tool would say — in a fraction of the time. It won't replace the real tool, but
it's invaluable for quick exploration, for filling in the gaps between runs, and
for feeding an optimisation that would otherwise be too slow to run for real.

A **job cohort** is the batch of real runs the model learns from.

## Building a job cohort

The **Job Cohorts** page has two tabs: **Catalogue** (browse existing cohorts) and
**New Job Cohort** (the create form). Open the second tab and create one by
**picking a tool**. MATOFX SPDM™ then finds every
completed run of that tool from *anywhere you can see* — standalone jobs, workflow
steps, parametric studies, and optimisations, across all your projects — and shows
them in a preview. A cohort is keyed on the tool (and its version), **not** on a
project, so you're never limited to one project's runs.

You can **narrow** which runs are gathered:

- **Tool versions** — pick which *major.minor* versions count (e.g. **1.2**).
  Every patch within a version is included — and so are *future* patches: if `1.2.4`
  is installed later, an auto-growing cohort that tolerates **1.2** picks up its runs
  automatically. By default **every installed version is selected**, so a new cohort
  gathers all of the tool's completed runs; un-tick the lines you don't want if you
  only care about one version (say, just **1.2**).
- **Advanced filters** — limit by **input ranges** (a min/max on any numeric tool
  input), by **who launched the run**, and by a **completed-on** date window. Use
  **Preview matching runs** to see what a filter actually does before saving — the
  count updates to match. To **remove** a filter later, clear its boxes and save:
  an empty box means "no bound here", and the cohort stops filtering on it.
- **Launched by** lists only the people who have actually run *this* tool and whose
  runs you can see, so every name in it can match something.

### Finding the runs you want

Underneath those, two more pickers help you *find* runs in a long list:

- **Source** — Standalone, Workflow, Parametric study, or Optimisation.
- **Parametric studies** / **Optimisation studies** — appear once you pick the
  matching source, and list the studies **by name**, so you can pull in the runs of
  one study out of several for the same tool.

These are **saved with the cohort**, like the filters above them. So an
auto-growing cohort can be *"the runs of these three studies"* and keeps honouring
that as more of their runs land — and re-opening a cohort to edit it shows you
exactly what was chosen, rather than a blank form.

The preview table names the **study** each run came from and **who launched it**,
so you are not matching run numbers up by hand.

You then choose how the cohort decides its membership:

- **Auto-grow (live)** — the cohort keeps re-checking for matching runs, so new
  runs join it automatically over time. Best when you want the model to keep
  learning as more runs land.
- **Fixed set** — turn auto-grow off, tick exactly the runs you want in the
  preview, and the cohort is frozen to that curated set.

Either way, runs from a study the cohort itself commissioned (see *Filling the
gaps* below) join as they complete — a fixed cohort stays otherwise frozen, and
an auto-growing one takes them even if its own filters would have missed them.

You can come back and **edit** a cohort later — rename it, change its versions or
filters, switch between auto-grow and a fixed set, or re-pick its runs — from its
detail page.

(Anyone can view cohorts and their results; *building* or editing one needs the
**cohort curator** capability — ask your administrator if you don't see the create
option. You only ever see runs from projects you have access to.)

The **Catalogue** lists each cohort with its **tool**, how many **runs** it
currently holds (for you — runs in projects you can't see aren't counted), and
**who created it**. Each cohort is also marked **Auto-growing** (new matching
runs keep joining) or **Fixed set** (a frozen, hand-picked set). Open one to
reach its detail page: **Edit** and **Archive** sit at the top, the
**Definition** card summarises the saved query, and the **Members & results**
table shows one row per member run — its **input parameters** first, then its
**results**, each column headed with the name and unit (e.g. *Heat Dissipation
(W)*). Each run number links straight to its **My Jobs** page. A cohort can
gather runs that came from different places — a standalone job and an
optimisation workflow, say — but the same result always shares one column,
never two.

A surrogate is only ever as good as the runs behind it, so:

- **Spread the runs, don't cluster them.** A model learns the space it has *seen*.
  An even spread across the whole input range (a **Latin hypercube** study is
  ideal for this) teaches it far more than the same number of runs bunched
  together.
- **Give it enough to learn from.** As a rough guide, aim for at least ~10 runs
  per varied input before expecting a trustworthy model, and more for wiggly
  responses. Too few and the model will guess confidently but wrongly.
- **You don't have to start from scratch.** A finished **parametric study** or
  **optimisation** is already a cohort — you can fit a model straight from one.

## Is this cohort fit to learn from?

Open a cohort and you'll find a panel headed **"Is this fit to learn from?"**.
It answers the question before you spend anything on a model.

This matters because the usual quality score can't tell you. A model's
**accuracy score** is worked out by hiding one run at a time and seeing whether
the model predicts it. If all your runs are bunched together, every hidden run
has a near neighbour to copy from — so the score comes out excellent while the
model is unreliable a short step outside the bunch. A high score with poor
coverage is the most misleading combination there is, and this panel is what
catches it.

### The traffic light

- **Well covered** — the runs spread across the range. A model built from these
  will be interpolating, not guessing.
- **Patchy** — there are noticeable gaps a model would have to guess across.
  Usable, but treat predictions in the gaps with care.
- **Poorly covered** — large parts of the range are untried.
- **Not enough to tell** — too few runs, or nothing was varied. This is *not* a
  bad verdict; it means the question can't be answered yet.

Underneath, one line per input tells you where the gaps are. A whole-number
input (like a number of layers) says which values are missing by name. An input
with fixed options says whether any option is barely represented — that one is
worth reading carefully, because a model that has seen forty runs of one
material and two of another will still answer confidently about the second.

### Two ranges, not one

**Each input on its own** is a table with two halves, and they answer different
questions:

| Input | Simulated range | Coverage | Input range | Coverage |
|---|---|---|---|---|
| Air velocity | 0.6 – 15.0 | Well covered | 0.5 – 15.0 | Well covered |
| Base temperature rise | 60 | Not varied | 10 – 120 | Barely covered |

- **Simulated range** is the span your runs actually used, and its coverage
  says how evenly they're spread *within that span*;
- **Input range** is what the tool itself accepts, and its coverage says how
  much of *that* anybody has ever tried.

The second one matters more than it looks. Everything else on this panel is
measured against the range you happened to run, which can never tell you that
you've explored a tenth of what the tool allows. `Base temperature rise` in the
row above is the case: it was 60 in every run, so on the left it simply isn't
varied — but on the right, 60 out of an allowed 10–120 is *barely covered*, and
that's a fact worth knowing before anyone asks the model about 100.

An input the tool sets no bounds for reads **No stated range** on the right:
there's no denominator, and a percentage of an unknown range would be invented.

**Inputs with a fixed set of options appear in the plots too: against a number
they get **one column per option**, and against each other a **count of the runs
at each combination**. That is where a lopsided choice shows — a material only
ever run in one corner of the space looks perfectly healthy on every other
panel. They are drawn after the numbered inputs, because the score that orders
those is a distance and a set of options has none.

An input with a fixed list of options** — a material, a solver choice — is
counted rather than measured, because "well covered" means nothing over a set of
options. Its rows read as the options themselves:

| Input | Simulated range | Coverage | Input range | Coverage |
|---|---|---|---|---|
| Plate material | Aluminium | Not varied | Aluminium, Magnesium | 1 of 2 options tried |

The left column lists what your runs actually used, the right lists everything
the tool offers. "1 of 2 options tried" is the honest reading: nothing here is
broken, but the model has never seen magnesium and cannot tell you anything
about it.

**Click any row** to see the values themselves — two histograms side by side,
one framed on the simulated range and one on the input range. The same runs,
two frames; a pinned input is a single bar in the middle of an empty axis on
the right, which is the whole point.

### Three questions, not one

The panel measures coverage at three scales, and they can disagree without any
of them being wrong. Knowing which is which is most of what makes the panel
readable.

1. **Each input on its own** — take every run, ignore all the other inputs, and
   ask how evenly this one was varied. That's the first block of rows.
2. **Each pair of inputs, against each other** — were these two ever changed
   *together*, or only one at a time? That's the second block.
3. **All of them at once** — the traffic light at the top.

You can vary six inputs beautifully one at a time and still leave nearly every
combination untried. Picture two inputs, each run at ten evenly spaced values,
but never moved together: run A varies while B sits at its nominal value, then
B varies while A sits at its nominal value. Both **rows** read perfectly
spread, both are correct — and plotted against each other the runs form a
cross, one horizontal line and one vertical, with the entire rest of the square
empty. A model built from that can follow the two lines and is guessing
everywhere else, which is most of the space.

Under **How the inputs combine** you'll find a row per pair with **two** tags, a
legend explaining what each means, and — in its own section — a plot for
**every** pair, laid out as a triangle so a row or column reads as "everything
against this input".

#### Why each pair gets two tags

Two different things can be wrong with a pair, they need different remedies, and
one number cannot tell them apart. Both are measured over the range you've
declared for those two inputs; they differ only in what they're compared against.

- **Enough runs?** — against an evenly spread design of 60 runs, which is the
  standard of 30 runs per input, and a pair has two. This answers *have I run
  enough here*.
- **Well spread for N runs?** — against the best your own N runs could have
  managed over the same range. This answers *are the runs I have in the right
  places*, and it's the one that says whether the design itself is any good.

Only the first of the two can move the traffic light. The second is compared
against a bar that rises with your own run count, so it can legitimately **fall**
as you add runs — and a verdict that got worse for doing the right thing is one
nobody would trust. It's reported because it's the more useful of the two to read;
it isn't allowed to judge.

A small cohort of a well-made design reads **Not many** and **Well spread**, and
together those mean: nothing is wrong with the design, there just isn't much of it
yet. A one-at-a-time cross reads the other way round — **Enough** and **Barely
spread** — and that one more runs of the same shape will not fix.

There used to be a single tag reading *"rarely varied together"*, which described
the arrangement while measuring the run count. On a twenty-run Latin hypercube,
where every run is on every pair, it said the opposite of the truth.

#### Choices are in this table too

An input that takes a fixed set of options is paired with every other input, not
just numbers with numbers.

- **A number against a choice** asks whether the number was swept *within every
  option* — did we try the full range of fin heights for magnesium, or only for
  aluminium? The worst option sets the *Well spread* half, because a model asked
  about magnesium at a fin height nobody ran there is guessing however good the
  aluminium coverage was. The *Enough runs* half is the thinnest option's run
  count against the same 30-per-input standard. An option with only one run isn't
  measured for spread; there's nothing to spread.

  When such a row reads badly it **says what it found**, underneath the tag —
  *"Magnesium was only run between 15.4 and 24.5, out of 5.8 to 44.7"*. Several of
  these at once, all on the same choice, is the single most serious thing the panel
  can tell you: one option was confined to a corner of every input, and no amount of
  further runs elsewhere fixes it.
- **A choice against a choice** asks how much of their combination grid has been
  run. Every aluminium job on the coarse mesh and every magnesium one on the fine
  looks perfectly balanced on each input alone, and half the grid has never been
  tried.

This is the failure that hides best: every other check on an option-valued input
looks at it *on its own* — how many runs each option got, whether any option is
barely used — so an archive confounded this way passes all of them.

#### Why a batch's projected verdicts leave the pair rows out

The batch-size table in **Fill the gaps** judges every row the same way, and that
means using the checks that apply to the cohort *as it stands*. If your cohort is
below the sixty runs a pair the pairwise check needs, the projected verdicts leave
it out — including for the larger batches, which would take you past that line.

Otherwise the table would go backwards: a check that is ignored at one row and
applied at the next can only ever subtract, so a bigger batch would read worse than
a smaller one purely because the panel had started looking at something new. Adding
runs must never make the verdict worse, and that guarantee is worth more here than
squeezing the last check into the projection.

The table says so when it applies, because it means the panel will be stricter than
the plan once those runs actually land.

#### When the pair rows don't set the verdict

Below about **sixty runs on a pair** neither tag can reliably tell a well-arranged
design from a badly arranged one — measured across seven design families, the two
overlap there, and at twenty runs they invert. So the pairwise check stands aside
rather than guessing, the rows are dimmed, and a line underneath says so with the
counts. The rows are still worth reading; they just aren't evidence yet.

**With too few runs there are no pair rows at all**, and the panel says so rather
than showing an empty section. A pair is only scored once enough runs vary *both*
of its inputs (twenty by default), because below that "we cannot tell" and "we can
tell, and it is bad" are different answers and only one of them is honest. If you
want the pair rows on a small cohort, add runs — the note tells you how many a
pair needs. The plot axes span the *tool's* full range, not the range
your runs used: scaled to the values present, a cross fills its panel corner to
corner and looks covered, and the empty space only appears when the frame is the
space the tool actually allows.

That is what the pair rows exist to catch, and it is a failure neither of
its neighbours can see. The per-input rows are blind to it by construction. The
overall figure is blind to it for a subtler reason: with six or more inputs
varying, no realistic number of runs can fill the space, so the yardstick it is
measured against is a very forgiving one and a cross scores nearly as well as a
well-designed set. With **two** inputs at a time, a few hundred runs is plenty,
the yardstick is demanding, and the cross has nowhere to hide.

If you see *"they were varied one at a time"*, the remedy is a **designed set**
that moves several inputs together — a Latin hypercube study over the ranges
you care about is the usual answer. More runs of the same kind will not fix it.

### Runs and inputs the assessment couldn't use

Two things get said plainly rather than quietly dropped, because a run that
disappears from a count is exactly the problem this panel exists to catch:

- **"2 runs couldn't be included because a value was missing."** Those runs
  had no value recorded for one of the inputs being measured, so there is no
  way to say where they sit. They are not counted as covering anything —
  which is different from being outside the range, and the panel keeps the two
  apart.
- **"'preload' has no value recorded in any of these runs."** An input that
  was never filled in anywhere can't be assessed at all, so it has no row of
  its own. It's named here instead, because a model built from these runs
  can't account for it either.

### "The overall figure is a rough estimate"

You'll see this once about **five or more inputs vary at once**, and it's worth
understanding rather than ignoring.

The overall figure asks "how far is the emptiest part of the range from the
nearest run?" With a handful of inputs that can be checked thoroughly. With
many, there are far more combinations than can be checked, so the panel probes
a sample instead — and a sample tends to *miss* the emptiest spot, which makes
coverage look slightly better than it is. Typically by a few per cent; more as
the number of varying inputs grows.

Two practical consequences:

- **the per-input rows are exact** — they're measured one input at a time, so
  no sampling is involved. That makes them more *precise* than the overall
  figure, but they answer a **different question**, not a better version of the
  same one (see *Each input on its own, versus their combinations* above). Green
  rows above an amber light are not the rows contradicting the light;
- treat the overall figure as "about right", not as a precise score. It's meant
  for reading the traffic light, not for comparing two cohorts to two decimal
  places.

The traffic light itself is unaffected by that sampling: the boundaries sit
far enough apart that it never moves one.

### "The overall figure can't tell good from bad here"

A stronger caveat, and you'll see it above **seven or more inputs varying**.

It isn't about sampling. With that many inputs, no realistic number of runs can
fill the space — so an evenly spread design leaves nearly as large a hole as a
badly arranged one, and the overall figure stops being able to tell them apart.
Measured, it rates a tight cluster about as highly as an even spread, which is
the *flattering* direction and therefore the dangerous one.

When that happens the overall figure is still shown, but it no longer sets the
traffic light. The verdict comes from the per-input and per-pair readings
instead, which stay meaningful however many inputs a tool has — two inputs at a
time always have enough runs to judge.

### Adding runs can never make the verdict worse

It used to be able to, and that was a fault rather than a subtlety.

The verdict is measured against a **fixed standard** — what an evenly spread
design of about thirty runs per varying input would achieve. That standard
depends only on how many inputs vary, never on how many runs you have, so
adding runs only ever moves the thing being measured and not the bar it is
measured against. More runs, or better-placed runs, can only help.

What *doesn't* follow is that every run helps equally. Runs added **where the
gaps are** move the verdict; runs clustered near ones you already have may move
it barely at all. That's what **Fill the gaps** is for.

### "Is it patchy because it's small, or because it's badly designed?"

Two very different problems, and the traffic light on its own cannot tell you
which you have. It is measured against a fixed standard — an evenly spread design
of about thirty runs per varying input — so a small cohort reads *Patchy* however
well it was put together. A twenty-run study over four inputs is being marked
against a 120-run bar.

**The verdict is still right, and it isn't hiding anything.** A badly-designed
archive never reads *Well covered* at these sizes either — measured across seven
design families, a clustered set tops out well inside *Patchy* whatever its size.
So a small cohort and a bad one both read *Patchy*: uninformative about the design,
but never flattering. And once a cohort does reach *Well covered*, that reading is
sound however few runs earned it, because nothing badly designed can get there.

What the panel adds below the verdict depends on what it can honestly say.

**Where there are enough runs to judge** — about twenty per varying input — you get
a sentence about the **arrangement**, measured against the best that many runs could
have managed:

- *"the runs are about as well spread as 80 runs could be"* — the placement is fine;
- *"reasonably well placed … a better-spread set of the same size would not gain a
  great deal"*;
- *"a better-spread set of 80 runs would cover noticeably more"* — the placement is
  part of the problem, not just the count.

It reports and leaves the decision to you. It deliberately does not set the traffic
light: unlike the verdict it **can** fall when you add runs, because a larger set is
held to a higher standard, and a light that moves backwards for doing the right
thing is one people learn to ignore.

**Below that** the panel says nothing about arrangement, because it cannot: at five
runs per input a clean Latin hypercube and a tight cluster score within noise of each
other. Instead you get a line saying what the figure is and isn't able to tell you,
and how many runs it would take before it could.

**And where the runs came from a study, it says so instead.** If every run in the
cohort came from a Latin hypercube, Sobol or random-sampling study, the panel reports
that as a fact — *"These 20 runs all came from a Latin hypercube study, so they are
space-filling by construction. What the figures above are short of is runs, not
arrangement."* That's provenance rather than measurement, and it's the only thing
that can answer the question on a small cohort. It stays quiet when the runs came from
a mix of sources, or include any ad-hoc job, or came from a grid or run table whose
shape is yours to judge — an absent line means *we can't establish it*, never *it
wasn't designed*.

### When nothing can judge the design

Two of the three checks stand down in circumstances they were measured not to cope
with: the overall figure above six varying inputs, and the pairwise rows below
about sixty runs on a pair. If **both** stand down at once — a wide tool with a
small cohort — the only check left is the per-input rows, and those pass a
one-at-a-time cross without noticing.

Rather than show a reassuring badge it cannot justify, the panel says **Not enough
to tell** and greys it. Everything measurable is still on screen, and **Fill the
gaps** still prices what it would take to move it.

### "Over what range should this model be trusted?"

A section near the foot of the panel, closed by default, holding one row per
input filled in with the range your runs actually cover. **Narrow it if you only trust part of that range** —
say you ran 1–8 mm but only ever intend to use 2–6 mm. Coverage is then measured
over the range you're claiming rather than the one you happened to run.

Runs outside the range you declare **still train the model**; they simply stop
counting towards coverage of the part you're claiming. The panel says how many
those are.

Some rows can't be edited, and the panel says why:

- an input that was **the same in every run** — the model can't vary it at all,
  so it isn't part of any range you can declare;
- an input with **fixed options** — the options come from the tool, not from you;
- a **snapshot number** on a time-varying result — that comes from the solver,
  not from the design. Covering more of it needs a longer run or more snapshots
  kept, not a new job.

### Things the spread alone won't show you

Two problems hide in a set of past runs that a "well covered" verdict wouldn't
catch on its own, so they're reported separately:

- **Inputs that move together.** If thickness and load were always increased
  together, no model can separate their effects, however many runs you have.
  Fixing it needs runs that break the pattern — vary one while holding the other
  — not simply more runs.
- **An option barely used.** Forty runs on one material and two on another looks
  perfectly spread on every other input, and the model will still answer
  confidently about the second material. This one is silent, which is why the
  panel calls it out.

### Filling the gaps

At the foot of the panel is a **Fill the gaps** card — the one thing here that
does something about everything above it.

It doesn't ask you for a number. *How many runs?* is the thing you came to the
panel to find out, so MATOFX prices several batch sizes instead and lets you
read the answer off the table:

| | | |
|---|---|---|
| **3 runs** | Patchy | gap closes to 29% of the space |
| **5 runs** | Well covered | gap closes to 24% — enough to call it well covered |
| **8 runs** | Well covered | gap closes to 21% |
| **12 runs** | Well covered | gap closes to 20% |

The **gap** is how big the emptiest corner of the space is. It always shrinks as
you add runs, so that column only ever improves — read it to see where the curve
flattens off and stop there. The marked row is the smallest batch that reaches
*well covered*; past it you are buying diminishing returns.

The sizes offered scale with the tool: about one, two, five, ten and twenty runs
per varying input, because a batch that would transform a two-input space is a
rounding error in a twelve-input one. You can also **type a number** — if you
know you can afford seven runs, ask about seven and get the same answer for it.

Nothing is committed by asking. Pick a size and the runs themselves appear as a
table — one row per proposed run, every input value shown, with the inputs that
actually differ between them first. Values are quoted at the input's own
precision, because a proposed run has to be one you could actually submit.

**See where these runs would go** draws the result: a histogram per input and
the full pairwise matrix, with your existing runs in blue and the proposed ones
in amber. That picture answers the question a table of numbers can't — whether
the batch lands in the empty half of the space or alongside what you already
have.

#### Choosing what to fill

Three controls above the button decide *which space* gets filled, and they
change the answer. **Inputs to explore** and **Coverage figures describe** are
covered in their own section further down; the third one is here.

**Fill across** — either **The range these runs already cover**, which tightens
a model over the operating window you've already chosen, or **The full range
the tool allows**, which extends it across everything the tool accepts. The
second commissions runs in regions nobody has ever visited, and the figures for
the inputs it widens are measured over that wider space, so the same batch reads
worse against it. It isn't the default: widening what a model claims shouldn't
happen because a button defaulted to it.

Two things this control does not govern, and the panel says so underneath it:

- an input that takes a **fixed set of options** always spans every option the
  tool offers, whichever range you pick. An option nobody has run is a gap, not
  something outside the range — so even the narrow setting can propose the
  material you have never tried;
- an input you have **opened up** (see below) had the same value in every run,
  so there is no covered range for it to stay inside. It sweeps the tool's own
  range either way.

Because the table's figures are measured over the space the *figures* describe
and the verdict at the top of the panel is measured over the range your runs
actually span, the two can disagree — most obviously under the wide setting.
The table says which one it used, right under the "Today" line.

#### Committing the batch

If you have permission to create parametric studies, **Create a parametric study
and add to Cohort** turns the batch into a **draft study** in a project you
choose from a list — only projects you can actually see appear in it.

The study name is pre-filled as *"<tool> - Fill coverage gaps"* and is yours to
edit. If the name is already used by a study in the chosen project, the box
turns red and says so **before** you press the button, rather than letting the
save fail. It is a draft on purpose: you review it, edit it if you want, and submit
it yourself. Nothing reaches the solver until you say so. If you don't have that
permission the button simply isn't there — the analysis above it stays fully
available.

**The runs start straight away.** You have already seen each of them, so there
is nothing left to review — the study is submitted for you and its runs queue.
If the tool requires approval before running, the study waits at that gate
instead; that policy is the tool's and this doesn't bypass it.

**And it comes back.** The study is linked to this cohort, so its runs join the
membership as they complete and the coverage above updates without you adding
them by hand. That link matters even for an auto-growing cohort: its own filters
— an input range, a project list, a completed-on window — can perfectly well
reject the very runs it asked for, and without the link you'd watch a study
finish and the verdict not move. A run you've explicitly excluded stays
excluded; the link never puts one back.

A few things the suggestions always respect:

- **the tool's own input rules are obeyed** — all of them, not just the obvious
  ones. Whatever your tool declares about its inputs is respected:

  - inputs that must **total a fixed value** (three percentages adding to 100);
  - one input's **limits depending on another** ("the minimum width is three
    times the thickness");
  - **validity rules** that only apply in certain conditions ("in fine mode the
    thickness must be 5 or less");
  - inputs whose value is **set for you** once another is chosen;
  - any other **combination the tool forbids**.

  These are checked with the very same code the solver form uses, so a run you
  are shown is a run you could submit. Where a rule leaves fewer runs than you
  asked for, the panel says which rule and quotes its own message;
- an input that only takes **whole numbers** never gets a fractional value, and if
  every whole number in range has already been run, that input is finished and the
  batch says so rather than proposing a duplicate;
- an input that was **the same in every run** is carried through unchanged, so the
  new runs are comparable with the ones they're completing — unlike an input you
  unticked yourself, whose held value is yours to set;
- a **snapshot number** never appears — you can't ask the solver to run at one.

### If the verdict is discouraging

That's allowed, and sometimes right. Nothing stops you building a model from a
patchy cohort — the verdict is recorded and travels with the model, so anyone
using it later can see what it was built from.

One case looks worse than it is: **runs that came from an optimisation** cluster
near the best design found, because that's the optimiser working properly. The
panel says so when that's the reason. It just means the model should be trusted
near that optimum, not across the whole range.

## Choosing which inputs to fill in

When you ask how many more runs you need, you also choose **which inputs the batch should
explore**. Every input the study varied starts ticked. An input that was the *same in
every run* is offered too, unticked — nothing in your archive can say what changing it
would do, so it is your decision whether to open it up. Tick it and the batch sweeps it:
numbers across the tool's range, choices across their options.

Two reasons to untick something. A batch that explores fewer inputs needs far fewer runs,
so if two of your eight inputs are what you actually care about, say so and the numbers
come down sharply. And an input you have deliberately fixed for this design does not need
runs spent on it.

Beneath the ticks sit two related choices. **Fill across** decides how wide a *range* the
proposed runs may use. **Coverage figures describe** decides what the resulting numbers are
a statement about:

- **Every input** (the default) — inputs you unticked keep reporting their own gaps, so a
  batch exploring only some of them will not reach *well covered*: it has not covered the
  rest. This describes your whole archive.
- **Only the ones I ticked** — the unticked inputs are set aside and the figures describe
  the smaller space you picked. Easier to satisfy, so read a verdict here as a statement
  about those inputs rather than about the archive as a whole.

If every input is ticked there is nothing to leave out, so that second choice is greyed
out until you untick something. While an input that was held constant is still unticked
the first option reads **Every explored input** rather than *Every input* — that input sits
outside the assessment either way, so calling it "every input" would overstate what the
figures cover.

The before-and-after charts cover **every input the pairwise panels show**, drawn the way
each one allows: a spread for a number, a column per option for a choice — showing what you
have and what the batch would add, with any option you have never run called out
underneath.

An input you untick is always left out of the batch, whichever of those two options you
pick — the choice above only decides whether it still counts towards the figures. It is
held at a single value for the whole batch, so every new run shares it, and that input's
own spread gets narrower rather than wider.

### The value an unticked input is held at

Because every run in the batch shares that value, the panel shows it rather than leaving
it implied: a box appears under the ticks for each input you have unticked, filled in with
the midpoint of its range (or its first option, for a choice). **Change it if a different
value is what you actually build** — holding six of your eight inputs at values nobody
ships is a good way to spend a batch on runs you cannot use.

The values offered are always ones the tool would accept: a number is rounded to the input's
own step, so an input that moves in halves is never held at 2.75.

### If the numbers barely move

Under **Every input**, the figures keep covering the inputs you unticked — and this batch
cannot move those, so their gaps cannot close at any size. Every row of the table can then
read the same, with no row reaching *well covered*. That is the honest answer rather than a
fault, and asking for more runs will not change it. The panel names the inputs responsible
above the table, and points you at **Only the ones I ticked** if what you want to read is
what this batch actually buys.

### Filling a space made only of choices

If the only inputs you tick take a fixed set of options, there is no distance to measure,
so the gap column stays at 0% however many runs you add — it is not saying the space is
full. What the batch buys is shown by the option counts instead: it proposes runs for the
combinations nobody has tried, emptiest first, so the options that have too few runs to
learn from get them. Once every combination has been run at least once the batch starts
repeating them on purpose, and says so — with nothing else left to vary, evening out how
often each option is used is the only thing more runs can buy.

The batch sizes offered adapt to what you have chosen. Exploring more inputs needs more
runs to fill the space, so the numbers rise; and on a large archive the smallest batches
are dropped, because a handful of runs cannot move a measure taken over hundreds.

## Studies your cohort asked for

When a cohort's coverage panel proposes runs and you accept them, the study it builds is
recorded as **commissioned by that cohort**. Its completed runs then join the cohort
automatically, whether the cohort is live or fixed — that is the point, since you asked
for those runs precisely to fill this cohort's gaps.

Two things follow, and the cohort page states both:

- **The picked count is not the membership.** A fixed cohort that picked 20 runs and
  commissioned a 40-run study resolves to 60. The coverage and results panels show the
  resolved number; the summary line at the top shows what you picked by hand.
- **You can detach a study again.** Edit the cohort and untick it under *Commissioned
  studies*. The runs themselves are never touched — only this cohort's claim on them.

On a **live** cohort, detaching may not shrink anything: those runs can still match the
cohort's own filters and stay in on their own merits. Drop them individually if you want
them out.


## Fitting a surrogate model

The **Surrogate Models** page has two tabs: **Catalogue** (browse the models
that already exist) and **New Surrogate Model** (the fit form). Open the second
tab to build one. The Catalogue lists each model with its **tool**, **project**
and **owner**, and carries a **search box** (name, tool or owner), a **scope**
filter (Personal / Project / Global) and a **status** filter (Active / Pending /
Retired). Each model also shows its scope and state as coloured chips.

There's a shortcut from the cohort itself: a cohort's page has a **Build a
surrogate** button at the top, next to Edit and Archive, which opens this form
with that cohort already picked as the source and a name suggested for you.
(A finished parametric study offers the same button on its results tab.) You
don't have to open the cohort's results first — the fit always works from the
cohort's live membership.

Once you have a cohort (or a study), you **fit** a surrogate model to it. The
form is grouped into three parts, separated by divider lines:

1. **Name** — what you'll call the model.
2. **Source** — first pick the **Source kind** (a *parametric study* or a *job
   cohort*). For a parametric study you then choose the **Project** — only
   projects that actually contain a study are listed — and then the study
   itself. Cohorts are workspace-wide and belong to no project, so the Project
   picker is hidden for them; you pick the cohort straight from **Source**.
3. **What to learn** — which results to learn and which fitting **method(s)** to
   try (pick several at once to **fit and compare** them), plus the model's
   **scope**. If the tool has **3-D field** outputs (a failure-index or
   temperature field over the part), the form splits into two columns — **Scalar
   responses to learn** and **Field responses to learn**, each with its own
   **Methods to try** — so numbers and fields can use *different* methods, and
   each choice sticks to its side. (If the tool has only numeric results, you just
   see the single scalar column.) You can pick **numbers and fields together** —
   the model then predicts both (see *One model for numbers and fields* below).

   The field methods are the same regressors as the scalars, **plus _Auto (best
   per mode)_** — which lets MATOFX pick the best regressor for each of the
   field's patterns individually.

Two buttons build the model — both produce the **same kind** of model; they only
differ in *which* method they use:

- **Fit & compare** first bakes off the chosen methods so you can keep the best,
  then build the winner. **Scalars** are ranked by predicted-vs-actual accuracy;
  **fields** by **Field Q²** (with a per-pattern breakdown and a mean-field
  predicted-vs-actual plot). You keep a scalar method *and* a field method, then
  click **Build with kept methods** in the comparison.
- **Build model** skips the comparison and builds straight away with the first
  picked method on each side — the quickest path when you already know what you
  want.

Either way a spinner shows while it fits and the buttons disable until it's done;
on success you land on the new model's page.

### Which model type, when

| Method | Best for | Notes |
|---|---|---|
| **Gaussian process (smooth)** | Smooth, well-behaved responses | The usual default. Also gives an **uncertainty band** — it tells you how sure it is. |
| **Gaussian process (Matérn — rougher)** | Smooth-ish responses with sharper features | Like the above but tolerates a rougher surface. Also gives an uncertainty band. |
| **Polynomial (quadratic)** | Gentle, bowl-shaped trends | A fast quadratic fit; good when you expect a simple curve. |
| **Gradient boosting (non-smooth)** | Jumpy or discontinuous responses | Handles steps and kinks the others smooth over — but gives **no uncertainty band**. |

> **Rule of thumb:** start with **Gaussian process**, and switch to **Gradient
> boosting** only if your response has genuine steps or discontinuities it can't
> follow. When in doubt, fit two or three and let the accuracy decide.

The same choice of method backs a **3-D field** surrogate too: POD always finds
the field's dominant patterns, and your chosen field method is the regressor that
learns how each pattern's strength varies with the inputs (so a field can use
Gaussian process for smooth behaviour or gradient boosting for jumpy behaviour,
just like a single number). If you'd rather not choose, pick **Auto (best per
mode)** as the field method and MATOFX selects the best-fitting regressor for each
pattern individually.

### Reading the accuracy

When you fit and compare, MATOFX shows each method's **goodness-of-fit** — how
well it reproduces runs it was trained on (and held back from). For **numeric**
results that's a Q² table plus a predicted-vs-actual plot; for **3-D fields**
it's **Field Q²** (one score per method, weighting the patterns that carry most
of the field), a per-pattern Q² breakdown for the best method, and a mean-field
predicted-vs-actual plot. Keep the method that fits best on each side; a poor
score is a sign you need more or better-spread runs, not a different button.
Don't trust a model whose accuracy is weak, however convenient it is.

Any **non-physical runs** in the source (results that came back as not-a-number or
outside their sensible range) are **left out of the fit** automatically, and the
fit tells you how many it excluded and why. This applies to both numeric and field
surrogates, so a handful of bad runs can't poison the model — but if a lot are
excluded, treat it as a sign to clean up the source runs (or the tool that
produced them) before relying on the surrogate.

### Filtering runs by output value (excluding outliers)

Sometimes a few *finite but extreme* runs — a design point where the solver
strained but didn't fail — skew a fit even though they pass the validity checks.
Open **Filter runs by output value (optional)** on the fit form and add one or
more conditions (e.g. *peak stress* between 0 and 10, *deflection* below 5). The
fit — scalar **and** field — then trains **only** on runs whose outputs fall in
those ranges. This is an *on-the-fly* filter for this fit only: it doesn't change
the tool or its validity bounds, so you can try a tighter band, refit, and compare
without touching anything permanent. Reach for it when the accuracy is poor and a
scatter plot shows a few far-flung points dragging the model around.

### Fitting a field (3-D) surrogate

Some tools produce a **field** result — a value spread across the whole shape (a
failure-index or temperature map over the part), not a single number. You can
learn one of these too: in **Field responses to learn**, pick the field output,
choose a **field method** (including **Auto (best per mode)**), and click either
**Fit & compare** (to see its accuracy first) or **Build model**.

A field surrogate is fit differently from a numeric one — it learns the dominant
*patterns* of the field (POD) and then a regressor per pattern — so its accuracy
shows as **Field Q²** rather than a single predicted-vs-actual scatter. Once it's
fitted you land on the model's page, where you can view and predict the field
**in 3D** (see below). Everything else — scope, publishing, refit, export-as-tool
— works the same as a numeric model.

> As with any surrogate, you need enough well-spread runs *that kept their field
> results*. A parametric study (ideally a Latin hypercube) over the tool is an
> ideal source.

### One model for numbers and fields

You don't have to choose between numbers and fields. Select **both** some numeric
results **and** one or more 3-D field outputs (each side with its own method), use
**Fit & compare** to check both if you like, then **Build model** — MATOFX fits
them together into a single **combined** model that predicts *all* of them. This
is the model to build when you want a fast stand-in for the real tool. Its detail
page puts the two halves in two cards: **Scalar fits** first (the accuracy of
each number, with the predicted-vs-actual plots), then **Field fits** below it —
one panel per field with its Field Q² + per-mode breakdown, a hint if the fit is
weak, and a 3-D viewer. Everything on both cards is named the way the tool names
it (*Peak component temperature (degC)*, *Fin height (mm)*), never by an internal
identifier. And when you **export it as a tool** the exported tool produces the
same numbers *and* field files (e.g. a `.vtu`) the original does — so you can
drop it into a workflow in place of the slow solver.

Each result is learned from the runs that are valid *for it*, and the fit reports
how many runs (if any) it left out, per the data-quality rules above.

Runs whose field result is **non-finite** (a tool that didn't converge) are
skipped automatically. If too many runs are unusable — or some hold extreme,
non-physical values — the fit stops with a clear message rather than building a
meaningless model; clean or exclude those runs (or fix the tool that produced
them) and refit.

## Making predictions

With a fitted model you can ask for a **prediction** at new inputs and get an
answer immediately, without waiting for the real tool to run. Where the model
supports it (the Gaussian-process types), the prediction comes with an
**uncertainty band** — wide where the model is extrapolating beyond what it has
seen, narrow where it has nearby runs to lean on. Treat a confident-looking
prediction in a region the cohort never covered with healthy suspicion.

The same badge sits above the **3-D predicted-results viewer**, and it updates
every time you move a slider — so if you drag a value beyond what the runs
covered, or ask for a **snapshot later than the model was trained on**, it
turns red and says which control took you there. That's the easiest way to
leave the trusted range without noticing, which is why the badge is right next
to the controls.

### Using the model as a tool

**Export as tool** on the model's page turns the surrogate into an ordinary
tool: it appears in the catalogue, takes a submit form, runs as a job, and drops
into a workflow wherever the slow original would go.

Its form asks for the same kind of design the real tool does, because the input
rules come across with it. A count that had to be a **whole number** still has
to be; the decimal steps, sliders and option buttons look as they did; the
tool's own **minimum and maximum** still apply; and a rule like *"these three
percentages must total 100"* is still checked before the job is accepted. On top
of that, each input says the range the surrogate was actually trained over, and
the coverage badge tells you when you have asked outside it.

The exported tool starts in **test** so a super user can review it and promote
it to **released**; that promotion is what makes it available to everyone else.

## Viewing predicted results in 3D

Where a model predicts results across a shape (a **field** — a temperature map
over a part, say, rather than a single number), you can view the predicted result
**in 3D** — rotating, zooming and colouring it in your browser, just like a real
result. Time-varying fields can be played back as well. A combined model that
learned several fields gives you **one viewer per field** on its page.

Each input the model learned gets its own control, labelled the way the tool
labels it. A **number** gets a slider across the range the runs covered; a
**choice** the model was fitted on — a material, a solver option — gets a picker
listing exactly the options it saw. Move any of them and the field is
reconstructed for that design straight away.

### What the "modes" mean

A field model doesn't learn the shape point by point. It first finds the handful
of **patterns** the field actually varies in across your runs — each one is a
*mode* — and then learns how much of each pattern a given design needs.
Predicting is adding those patterns back together.

Two small charts show this, side by side under the viewer: how much of the
variation each pattern accounts for, and how far the design you're looking at
leans from the *average* field along each pattern (the second moves as you change
the controls).

**A negative bar on the second chart is normal.** A field is rebuilt as *the
average field plus so much of each pattern*, so the bars are measured from that
average — zero means this design matches it, and a bar below the line simply
means it leans the other way. Roughly half of all designs do, on any pattern.
Each pattern's "direction" is arbitrary; only the combination it builds is
meaningful.

**The number of patterns is a fact about your runs, not a setting.** MATOFX SPDM™
keeps as many as it takes to account for nearly all the variation, so:

- **one pattern** means the field kept essentially the same shape across every run
  and only changed in magnitude;
- **several** mean the shape itself changed — each one is an independent way the
  field moved.

That makes it a useful reading on the cohort as well as the fit: a tightly
clustered set of runs tends to need fewer patterns than a well-spread one.

## How far can you trust a single prediction?

The coverage verdict describes the model as a whole. Each prediction made by a
**surrogate that's been exported as a tool** also comes back with its own
**support** verdict, shown as a coloured badge on the job record, saying how far
*that point* sits from the runs the model actually learned from:

- **Well supported** — the point sits among the runs. The model is
  interpolating.
- **Near the edge** — inside the range you declared, but further from the
  actual runs than most of them are from each other. Treat the number with
  more caution than usual.
- **Outside the trained range** — the point falls outside the range the model
  was built to be trusted over. The number you get is a guess, not a
  prediction, and the message says which input took it out of range.

Underneath the badge is the reason, and it names the input responsible —
"'thickness' is 42, outside the 1 to 8 range this model was trained to be
trusted over" — because "outside the trained range" on its own isn't something
anybody can act on.

The prediction is still produced either way. The guard tells you what you're
looking at; it doesn't refuse to answer.

A model fitted before coverage existed has no verdict to give, and shows no
badge at all rather than an empty one — the same when a model has no record of
what range it was trained over. Nothing shown means nothing was checked, which
is different from a clean bill of health.

### It works when nobody's watching, too

This matters most for a surrogate that's been **exported as a tool** and runs
inside a workflow overnight. Those jobs produce two extra results:

- **Prediction support** — the same three words, in the job record.
- **Prediction uncertainty** — a number.

The number is the important one. Because it's a number rather than a phrase,
you can put a **limit** on it the same way you would on any other result, so a
run that drifts far outside what the model knows is flagged automatically
without anyone reading the job.

An exported tool only gains these two results when it's **re-exported** after
its model was checked for coverage. Existing exported tools keep working
exactly as before.

The exported form also carries every input the model needs in order to judge
its own answer — including **fixed-option** inputs (a material, a mesh file)
and anything that was **held constant** while the model was trained. Held
values show with the single option they were trained on, so you can see the
context the model was fitted in. Without them the tool has no way to tell
whether a prediction is supported, and would have to report every one of them
as outside its range.

## Publishing a model for others

A model you're happy with can be **published** so colleagues can use it too,
rather than each person fitting their own. Models carry a **scope**:

- **Personal** — only you can use it.
- **Project** — shared with everyone on that project.
- **Global** — available to the whole site, but only after a **two-gate review**:
  a global model is trusted org-wide, so it must be reviewed and released before
  anyone else can pick it.

Publish once it's earned its keep, and prefer the narrowest scope that's useful —
you can always widen it later.

Use **Change scope** on the model's page. Choosing **Project** asks for the
destination, and the list offers the projects **you created or own** — moving a
model into a project needs write access to it, so a project you can merely see
isn't offered. If you own none, the dialog says so rather than showing an empty
list.
