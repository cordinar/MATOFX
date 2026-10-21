---
title: Adding tools
audience: admin
capability: null
slug: adding-tools
summary: What a tool is and how MATOFX SPDM™ runs it; the tool description file; the guided wizard; saving and resuming drafts; defining the input form; rules between inputs (Dependencies — formula-driven bounds/values and validity rules); connecting the program; file handling; icons; parameter guide images; key numbers; V&V; testing, releasing, versioning, and archiving.
---

# Adding tools

Tools are what your users run. This chapter explains how you add one and what each
part of its definition does. (This is the closest MATOFX SPDM™ has to a "tool author"
guide.)

## What a tool is, and how MATOFX SPDM™ runs it

A **tool** is a program — usually a script — plus a **description** of the inputs
it needs and the outputs it produces. When a user submits a job, MATOFX prepares a
working folder, hands the program the user's inputs, runs it, and collects the
results.

## The tool description file (descriptor), explained simply

Every tool has a **description file** (its *descriptor*). This file is the single
source of truth for the tool's form and behaviour — it lists each input (its
label, type, units, and whether it's required), and says which result files to
keep. Because the form is built from this file, **adding or changing an input
means changing the description, not writing screen code**.

## Creating a tool with the guided wizard

In **Admin → New/edit tool**, a **guided wizard** walks you through creating
a tool step by step, so you don't have to write the description file by hand.

### Saving a draft and resuming later

Setting up a tool can take a while, so the New Tool page has a **Save draft**
button. It keeps your progress — all the typed metadata and everything in the
inputs/outputs wizard — without installing anything. Saved drafts appear in a
**Drafts in progress** section at the top of the **Tools** list, where you can
**Resume** (which reopens the form exactly as you left it) or **Delete** one.
Drafts are private to you, and the draft is cleared automatically once the tool
installs successfully.

One thing a draft does **not** keep is the uploaded files — the descriptor JSON,
the static-files ZIP, and the icon. Those are re-attached when you resume and
install, so you don't need to track down the same files across sessions until
you're ready to finish.

### Alias, name, and descriptions

Every tool needs an **alias** and a **name** — they sound similar but do
different jobs:

- **Alias** (required) — the system's stable internal identifier (lowercase
  letters, digits, and underscores only, e.g. `material_properties`). It becomes
  the tool's bundle directory name on disk and is referenced by workflows and
  other tools, so it is **fixed forever** — you can't change it after creation.
- **Name** (required) — the human-friendly title users actually see, in menus, on
  the home-page **Quick Access** cards, and in reports (e.g. "Material properties
  calculator"). It's free text, needn't match the alias, and you *can* change it
  later.

Every tool also needs a **short description**, and may have an optional
**long description**:

- **Short description** (required, up to 200 characters) — a one-line summary. This
  is the *only* description shown on the home-page **Quick Access** card, so keep it
  tight; it's what helps a user pick the right tool at a glance.
- **Long description** (optional) — a fuller explanation: what the tool does, what
  the inputs mean, any caveats. It appears **above the input form** when a user
  opens the tool to run it, and behind an **info (ⓘ) button** on the Quick Access
  card so people can read more before committing to a launch. When you leave it
  blank, those places simply fall back to the short description.

Write the long description as plain text; line breaks you type are preserved.

## Defining the input form

The biggest part of a tool is its **input form**. For each input you decide:

### Field types

Each input has a **type**, which decides how it's shown and what's accepted:

| Type | What the user sees | Use for |
|---|---|---|
| **Integer** | A whole-number box | Counts, levels — values with no decimal part. |
| **Float** | A decimal-number box | Most physical quantities. |
| **Boolean** | A true/false toggle | On/off choices. |
| **Select** | Pick **one** from a list | A fixed set of mutually exclusive options. |
| **Multiselect** | Pick **several** from a list | Where more than one option can apply at once. |
| **File** | An upload control | Geometry, data tables, any file the program reads. |
| **Material** | A picker into the materials database | Material choices, so properties come from one approved source. *(Only when the materials database is switched on.)* |
| **Table** | A small grid of rows | Repeating structured input (a list of layers, say). |

Choosing the tightest type that fits is the single biggest favour you can do your
users: an integer field won't accept "3.5", a select can't be mistyped, and a
material field guarantees consistent properties — each one removes a class of bad
input before the job ever runs.

#### File input options

A **File** input has a few extra settings:

- **Accepted file types** — the extensions the form will accept (e.g. `vtu`,
  `csv`). This also limits what the "use a file from another job" picker offers.
- **Max size (MB)** — an upload size cap (or none).
- **Expected field array** *(optional)* — when the input consumes a **result
  field** (a `.vtu`/`.vtp` carrying a nodal array), name the array the tool
  needs, such as `temperature`. The "use a file from another job" picker then
  only offers field files carrying that exact array, so a temperature-field
  input never lists, say, a stress or failure field — even though both are
  `.vtu`. It filters the picker; it doesn't restrict a manual upload, so a tool
  should still check the file it actually receives.

#### Table input options

A **Table** input is a small grid the user fills in when submitting a job. You
declare its shape up front:

- **Columns** — each column has a name, a label, a type (string, integer, float
  or boolean), an optional unit, and, for numeric columns, optional bounds and a
  step. The user adds and removes rows at submit time; each cell uses its
  column's type (a number box, a text box, or a checkbox).
- **Default rows** — how many empty rows the form starts with.
- **Show 'view as plot' button** *(on by default)* — when on, the job form puts a
  **View as plot** button by the grid that charts the entered rows. The **first
  column is the x-axis** and every numeric column after it becomes a line series.
  Two series that share a unit and a similar scale are drawn together on one
  y-axis; a series on a very different scale (or a different unit) moves to a
  second y-axis on the right, and if there are more independent scales than two
  axes can hold, the extra series are shown as further stacked charts — so a plot
  never crowds more than two scales together. Every chart has a legend. Turn the
  toggle off for tables where a plot wouldn't make sense (e.g. all-text rows).

### Units

The unit of measurement shown next to a numeric field, so users know what to enter.
Pick from the searchable catalogue of common engineering, physics, and chemistry
units — length, mass, temperature, pressure and stress, strain, force and torque,
thermal, flow, concentration, electrical, and more — or choose **Custom unit…** to
type your own if what you need isn't listed.

### Drop-down options

For a choice field, the set of options the user picks from.

### Required fields and defaults

Whether an input must be filled in, and a sensible **default** value to start from.

### Grouping fields into sections

You can group related inputs into **sections** so a long form stays readable.

### Putting things in the order you want

Inputs, outputs, sections and table columns all appear in the order you list
them. To change that order, **drag a row by the handle** (⠿) on its left and drop
it where you want it — you can do this while the row is collapsed, so reordering
a long form doesn't mean opening every card. The up and down arrows on each row
do the same thing a step at a time.

An input's position is its position *within its section*: dragging a field above
one that sits in a different section doesn't move it between sections. Change its
section on the field itself, and use the section order to decide which section
comes first.

### Rules between inputs (Dependencies)

A form doesn't have to be static. In each input's **Dependencies** card you can
attach **rules** that link it to other inputs — both to make the form react as the
user types *and* to enforce real relationships between values. A rule has a
**When** condition (leave it blank to mean *always*) and one or more **outcomes**
that apply while the condition holds:

| Outcome | What it does |
|---|---|
| **Show / Hide** | Reveal or remove a field depending on another's value. |
| **Disable** | Lock a field so it can't be edited (but stays visible). |
| **Set value / Set default** | Fill a field automatically — a fixed value *or a formula* of other inputs. |
| **Set required** | Make a field mandatory only when it's actually relevant. |
| **Set min / max** | Set a numeric field's allowed range — a fixed number *or a formula* of other inputs. |
| **Require (must be true)** | Block the job with a message unless a condition holds. |

A **When** condition and a formula are written with the input names and the usual
comparisons and connectors: `== != < <= > >=`, `+ - * /`, `and`, `or`, `not`, `in`,
and parentheses `( )`. For example a condition `cooling_type == 'forced'`, or a
minimum formula `inner_diameter` (or `inner_diameter + 1` for *strictly* greater).

#### Example: "B must always be larger than A"

Open input **B**'s Dependencies card, leave **When** blank (always), and add a
**Set min / max** outcome whose **Minimum** is the formula `a` (or `a + 1` to be
strictly greater). Now B's minimum tracks A automatically: the form won't let a
user drop B below A, and the same limit is applied everywhere B is chosen — at
submit, and when parametric studies or the optimiser generate inputs (a
combination with B below A is dropped). If you'd rather *block with an
explanation* than auto-adjust, use a **Require** outcome with the condition
`b > a` instead.

#### How far each outcome reaches

- **Show / Hide / Disable / Set required / Set options** shape the *form* — they
  react to what a person types. They don't change generated values, so they don't
  apply when a study or optimisation produces input combinations automatically.
- **Set value / Set default / Set min / max / Require** describe real
  relationships between values, so MATOFX honours them **everywhere**: live in the
  form, when a user submits, and when parametric studies and optimisation generate
  candidates (a candidate that breaks a min/max or a Require rule is dropped, and a
  formula-set value is applied).

#### Messages

A **Require** outcome can carry an optional message shown when it blocks (for
example, *"Outer diameter must be larger than inner diameter."*). Leave it blank
and MATOFX generates a readable one from the inputs' labels. Either way the user
sees a plain-English message, never the raw rule.

Rules keep a complex form short, stop users filling in fields that don't apply,
and capture the relationships your tool genuinely requires — all of which means
fewer invalid jobs.

## Connecting the tool's program and its environment

You tell MATOFX which **program** to run (the tool's main script) and which
**software environment** it needs (the set of installed components the program
depends on). MATOFX runs the program in that environment when a job is submitted.

## Saying what the tool needs to run (and how long it may take)

A tool declares the resources it needs: how many **CPU cores**, how much
**memory**, and — if it uses one — how much **GPU memory**. On a plain
installation these guide MATOFX in deciding when there is room to start a job.
When the tool runs on an **HPC cluster**, they are passed to the cluster's
scheduler as the job's request, so getting them right matters: ask for too
little and the cluster may stop the job part-way through for exceeding what it
reserved, which looks like the tool crashing.

You can also set a **time limit** — the longest the tool should ever be allowed
to run, in minutes. This is optional and there is no default; leave it blank and
the job runs for as long as it needs.

**Set a time limit on anything an optimiser will drive.** An optimisation search
tries combinations a person would not, and it can find a corner where the
calculation never settles. Without a limit nothing stops it: the job runs until
somebody notices, holding cluster resources the whole time — and on a cluster you
pay for by the hour, that is a bill rather than a failed run. A limit turns a
runaway into an ordinary failed job that the study can move past.

Pick a limit comfortably above the slowest run you would consider normal — its
job is to catch the pathological case, not to trim the merely slow. If a cluster
administrator has set their own ceiling, that always wins; a tool cannot ask for
more than the cluster permits.

## Email notifications for the tool's jobs

Each tool decides whether MATOFX emails the person who submitted a job as it
**starts, completes or fails**. **New tools have this switched off**, so a tool
you have just added stays quiet until you decide otherwise — tick **Send
completion / failure emails for this tool** on the tool's Notifications card,
either when you create it or later from **Edit tool**. A tool installed from a
bundle that doesn't mention notifications is treated the same way: off until
somebody turns it on.

Turning it on is a request, not a guarantee: the site-wide job-email setting has
to be on (Site Settings), and each user can switch job emails off for themselves
on their own account page. Approval emails are governed separately — see the
**Approvals** chapter.

## Which files to keep after a run

A run can produce many intermediate files. On the **Inputs & Outputs → Advanced**
tab you give a single list — **Files to keep after a successful run** — and MATOFX
deletes everything else the tool left behind.

You almost never need to add anything, because MATOFX **always keeps the important
files for you, without listing them**:

- the job's **inputs** (the exact record of what was submitted);
- **every output you declared** on the Outputs tab — your tool's promised results
  are never deleted;
- the run's **logs and records**.

The list is only for *extra* files the tool wrote that you didn't declare as
outputs but still want to keep — for example a mesh or diagnostic file you'd
inspect by hand. Entries are simple patterns matched against the job's working
folder (`*.log`, `outputs/*.vtk`, and so on).

- **Leave the list empty** to keep the whole working folder.
- **Add one or more patterns** to keep those extra files and delete the rest of
  the tool's leftovers.

There's no separate "delete" list: anything not kept is cleaned up automatically,
so you never have to describe what to remove.

## Adding a tool icon

You can give the tool an **icon** so it's easy to recognise in the Quick Access
lists.

## Adding parameter guide images

Some inputs are hard to explain in words alone — *what exactly does "flange
offset" measure from?* On the tool editor's **Metadata** tab, the **Parameter
guide images** section lets you attach a set of static pictures (typically
labelled diagrams) that are shown to the user **above the input form** whenever
they run the tool.

- Add as many as you need (up to ten) as **PNG, JPG or WebP** files, and give each
  a short **caption** — that caption is where you say what the picture shows
  (e.g. *"L is measured from the flange face to the bolt centre"*).
- **Reorder** them with the up/down arrows and remove any with the bin icon. The
  pictures and captions are only saved when you click **Save changes** — like the
  icon, they're written into the tool's bundle and travel with it when the tool is
  exported or moved to another install.
- Leave the section empty if the tool doesn't need any — nothing is shown.

Users see the images as a compact strip of thumbnails at the top of the run form;
clicking one opens it full size. They're purely explanatory — they're not inputs
and don't affect the run.

## Pulling out key numbers and charts automatically

You can tell MATOFX which of a tool's **numeric results** to treat as **key
numbers (KPIs)**. Mark an output as a KPI and it flows automatically into the
**results warehouse**, where users can compare it across every job, filter by its
value, and chart its trend over time. Only mark the handful that genuinely matter
— a peak temperature, a mass, an efficiency — rather than every number the tool
emits, or the warehouse becomes noise.

Key numbers are captured **when a job finishes**, against the tool description
that job ran. Two consequences worth knowing:

- **Marking an output as a KPI doesn't reach backwards.** Jobs that finished
  before the change have no value recorded for it, so its column stays empty for
  them. If you need the history filled in, ask your MATOFX supplier — the values
  can be re-read from the runs' stored results.
- **Un-ticking a KPI stops future collection but keeps the past.** The figures
  already captured keep their column in the warehouse for as long as those jobs
  exist. Re-ticking the same output later picks up where it left off — it does
  **not** create a second column. Note that un-ticking clears the KPI's display
  name and unit overrides, so re-type them if you want them back.

You can also describe how to turn result data into **charts** shown on the job
page (for example, plotting a column of a results file against another). A guided
wizard helps you set these up against a sample result so you can see the chart
before you release the tool.

A result can also be a **field** — a whole nodal result such as a temperature
map or a stress field, rather than a single number. Set a file output's renderer
hint to **field** and choose its format (for example VTK `.vtu`) and the
point-data array that carries the values; MATOFX can then show it in the 3-D
field viewer and use it to build a field surrogate. If the tool writes one
snapshot per time step, give the timestep file pattern as well and the result
animates over time on the job page.

### An exported surrogate keeps its source tool's input rules

A tool exported from a **surrogate model** builds its form from the model, but
the *rules* on each input come from the tool the surrogate was learned from —
so the stand-in asks for the same kind of design the real tool does:

- a **whole-number** input stays a whole number, on the same step of 1 (a fin
  count of 17.4 is refused, as it always was);
- **decimal precision** and the **slider / text box** or **dropdown / radio
  button** choice carry over, so the form reads like the original;
- the tool's own **minimum and maximum** are enforced, alongside — not instead
  of — the surrogate's own trained range. The trained range stays *advisory* by
  default (you may extrapolate, and the coverage badge warns you); the tool's
  range is about what the design can physically be, so it applies either way;
- a **sum check** ("these three fractions must total 100") is re-applied,
  whenever every input it names is on the exported form — as a value you can
  vary, or as one held constant during training.

Each input's own help text is carried across too, with a line added saying the
range the surrogate was trained over.

These rules are read from the source tool **at export time**, so a tool
description you fix afterwards reaches the exported surrogate on its next
export, through the usual *test → released* gate. An input the source tool no
longer declares simply keeps the plain numeric treatment rather than blocking
the export.

### Results that carry an uncertainty (exported surrogates)

A tool exported from a **surrogate model** emits two numbers per result: the
prediction, and its one-standard-deviation uncertainty. MATOFX pairs them and
shows them on the job page as a single **`value ± uncertainty`** row, with a
footnote explaining the ±. The uncertainty never appears as a row of its own.

That pairing is a property of the tool description, so it survives editing:
you can **group and order the outputs** of an exported surrogate exactly as you
would any other tool, and the ± reporting is unaffected. Put the *result* in
whichever group you want; its uncertainty follows it.

Because an uncertainty is folded into its parent rather than shown separately,
the editor restricts what you can set on it:

- **Group** reads *"Group not assignable"* — it appears wherever its result
  appears, so a group of its own could never be honoured;
- **Unit** is hidden — an uncertainty is in the same unit as the value it belongs
  to, by definition;
- **Key number (KPI)** is hidden — a KPI measures the design, not the model's
  confidence in it;
- **Precision** and the **validity bounds** stay editable: both act on the number
  the tool genuinely writes, and bounding an uncertainty is a good way to catch a
  prediction that has blown up.

If an uncertainty points at a result that has been renamed or deleted, the tool
description is **rejected with a clear message** rather than installed. That is
deliberate: because the uncertainty is folded into its parent and hidden, a
dangling link would quietly remove the number from the results page altogether.

## Keeping bad results out (output validity)

A run can finish yet produce **non-physical** numbers — a solver that didn't
converge writing `NaN`/infinity, or a value so large it's clearly meaningless.
MATOFX guards against these polluting study tables, surrogates and optimisation:

- **Finiteness is checked automatically.** Every numeric and field output must be
  finite; a `NaN`/infinity flags the run as **invalid**. (If a non-finite value
  is genuinely legitimate for an output, you can opt it out.)
- **You can declare physical bounds.** On each output in the New Tool
  **descriptor wizard** (or a hand-authored descriptor), give a numeric output a
  **min** and/or **max** (just like an input's range), and a **field** output a
  per-node **value min**/**value max**. A run whose output falls outside is
  flagged invalid. Tip: bound a field's companion peak number (e.g. a *peak
  failure index*) and a blown-up field run is caught cheaply, before its mesh is
  even read.
- **Bounds also keep surrogate predictions honest.** A surrogate is an
  unconstrained model, so it can predict values the quantity can't take (a
  negative failure index, say). The results **prediction profiler** clamps
  predictions to a numeric output's declared min/max and flags when it does — so
  declaring bounds pays off twice: valid runs *and* physical predictions.

Invalid runs are **kept** (so you can inspect them) but marked in the study
results with a **Health** column and a summary, and are **excluded** from
surrogate fits, sensitivity and optimisation — which report how many were left
out. A tool that has no usable result at all should instead **fail** the run.

## Linking Verification and Validation records

You can link a tool to its **Verification and Validation (V&V)** records — the
evidence that it's been checked — which is what shows users the V&V badge.

## Testing a tool before release

A new tool starts in a **draft / test** state, visible only to you, so you can run
it and confirm it behaves before anyone else sees it.

A tool a user can't see stays hidden **everywhere** it would otherwise appear —
not only in their tool list and Quick Access, but in any **workflow that contains
it** (the whole workflow drops out of their lists), and it can't be launched from
anywhere. The same applies when a tool is restricted to **teams** a user doesn't
belong to. The one exception is history: a user's own **past jobs** for a tool stay
visible (read-only) even after the tool is hidden from them, so their record is
never silently erased — they just can't start a new run of it.

## Releasing, updating, and versioning

When you're satisfied, you **release** the tool so users can run it. Later changes
create a new **version**, so existing jobs keep their history while new jobs use
the updated tool.

### Replacing a tool's bundle

To ship new code for an existing tool, use **Replace bundle** on the tool's edit
page. Only the **static-files ZIP** is required. The **descriptor JSON is optional**:
leave it out to keep the tool's current input and output settings unchanged and
only swap the program files — the usual case when you've only changed the code.
Supply a descriptor JSON only when you also want to change the input/output form.
Either way the version is bumped and older jobs keep resolving against the bundle
they captured.

The static-files ZIP is refused if it unpacks to more than 1 GB (a guard against
maliciously over-compressed archives). Filenames referenced in the descriptor —
the program entry point, icon, and guide images — must sit **inside** the bundle;
paths that try to point outside it (for example with `..`) are rejected when the
tool is saved.

## Archiving a tool

When a tool is no longer needed, you **archive** it. It disappears from the lists
of tools users can launch, but its past jobs and their records remain intact. An
archived tool stays in the admin Tools list with an **Archived** badge, and you
can **Re-activate** it at any time to bring it back online.

Both archiving and re-activating are recorded in the tool's **change log**
(**View change log** on the Tools list), alongside edits and releases, so the
history of when a tool was shelved and brought back is always visible.
