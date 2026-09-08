---
title: Running a tool (submitting a job)
audience: user
capability: null
slug: running-a-tool
summary: How to launch a tool, fill in its form, save your work, and submit a job.
---

# Running a tool (submitting a job)

Running a tool is the heart of MATOFX SPDM™. You choose a tool, tell it what you want
by filling in a short form, and submit it. MATOFX does the rest.

## What a "tool" and a "job" are

- A **tool** is a piece of simulation or calculation software your administrator
  has added to MATOFX — for example a heat-transfer solver or a material-property
  calculator.
- A **job** is one run of a tool with a particular set of inputs. Every time you
  fill in a tool's form and submit it, you create a job.

Jobs live inside **scenarios**, which live inside **projects** — see the
*Organising your work* chapter for how those fit together.

## Starting a job from the home page

The quickest way to start is the **Create** card on your home page:

- Select **Job(s)** to open a chooser. Pick the **project** and **scenario** the
  work belongs to (or create a new scenario on the spot), then **tick** every
  tool — and every ready-made workflow — you'd like to add. You can add several
  at once. When you choose **New scenario**, MATOFX pre-fills the next name in
  sequence — if the project already has *Scenario 1* to *Scenario 3*, it
  suggests *Scenario 4* — so you can usually just click through. (If the
  project's scenarios use custom names, it starts at *Scenario 1*.)
- The right-hand **Quick Access** lists also let you launch a single tool
  directly, either **in an existing project** or **in a new project**.

## Starting a job inside a scenario

If you're already looking at a project and scenario, you can add a tool from
there too. Either route lands you on the same form.

## Filling in the form

Each tool shows its own form, built from the tool's definition. The fields you
see depend entirely on the tool.

### Parameter guide images

Some tools show a strip of **guide images** at the top of the form — labelled
diagrams that explain what the inputs mean (for example, exactly where a length
is measured from). Click any thumbnail to see it full size. They're there purely
to help you fill the form in correctly; not every tool has them.

### Required and optional fields

Required fields must be filled in before you can submit. If you miss one, MATOFX
tells you which field needs attention.

### Units of measurement

Where a value has a unit (millimetres, degrees Celsius, and so on), the unit is
shown next to the field so there's no guesswork. Enter the number in the unit
shown.

### Values with a sensible range

Some numeric fields have a lowest and/or highest value the tool will accept. If
you type a number outside that range, MATOFX leaves your entry as you typed it
and shows a short message in red under the field — for example *"Temperature
can't be more than 100 °C."* — so you can see exactly what to change. It does
**not** quietly snap your value back to the limit. Correct the number and the
message clears itself.

### Choosing from a list

Some fields are drop-down lists with a fixed set of choices. Pick the one you
want rather than typing free text.

### Filling in a table

Some tools ask for a **table** — a small grid you fill in row by row (a set of
layers, a lookup curve, and so on). Each column expects its own kind of value (a
number, some text, or a tick box). Use **+ Row** to add a row and the **✕** at the
end of a row to remove it. Numeric cells are checked against the column's limits
when you submit, just like a normal field — if a value is out of range, MATOFX
names the column and row so you can fix it. A table can also be pre-filled from a
preset or a previous job, the same way the other fields are.

If the table can be charted, a **View as plot** button sits next to it. Click it to
see your rows drawn as a graph in a pop-up: the **first column is the horizontal
axis** and each numeric column after it is plotted against it, with a legend. When
two quantities are on very different scales, the second one is drawn against a
second axis on the right (and if there are several unrelated scales, you'll get more
than one chart). It plots exactly what's in the grid right now, so you can sanity-
check your numbers before submitting — a value typed into the wrong row usually
jumps out as a kink in the line.

### Uploading a file

Some tools need a file from you — a geometry file, a data table, and so on. Use
the upload control to choose the file from your computer.

### Reusing a file from an earlier job

You don't have to re-upload a file you've already used. A file field offers two
ways to pull in a file an earlier job already has:

- **Use file from a previous job** — re-uses a file you previously supplied for
  *this same input* on an earlier run of *this tool*.
- **Use a file from another job** — picks a **result file produced by another
  job**, including a *different* tool. Choose the **tool**, then the **job**,
  then the **output file**. Only files of a type this input accepts are shown,
  and each option names the field it carries (for example *field: temperature*)
  so you can tell similar files apart. This is handy when you want to feed one
  tool's output straight into another — for example, running a stress tool on a
  temperature field produced earlier by a separate heat tool, without
  re-uploading anything. Pick the file that carries the field the tool expects;
  if you choose an incompatible one, the tool will stop with a clear message
  rather than producing a wrong result.

When a file input is **provided automatically by an upstream step** (inside a
workflow), the field is locked and simply says so — there's nothing to upload or
pick; MATOFX moves the file for you.

### Picking from the shared libraries

If your site uses them, some fields draw straight from the shared catalogues
instead of asking you to supply a value:

- **File fields** may let you pick an existing, approved file (and version) from
  the **File Library** instead of uploading your own.
- **Material fields** let you choose a material from the **Materials database**, so
  its properties come from an approved source.

Both keep everyone using the same trusted inputs. See *The shared file and
materials libraries* for browsing the catalogues and how versions and approval
work.

## Saving your work before submitting

You don't have to finish in one sitting. You can **save your progress** and come
back later — your entries are kept so you can pick up where you left off. (Saving
keeps your work safely recorded; the tool only actually runs once you submit.)

## Submitting the job

When the form is complete, select **Submit**. MATOFX checks your inputs, then
queues the job to run.

The **Submit** button stays greyed out until everything it needs is in order,
and a muted line beneath it spells out *every* outstanding reason at once — a
missing job name, required fields still to fill, a value outside its range, or
(for tools that ask you to) a section you haven't opened and reviewed yet. As you
fix each one the line updates, and **Submit** becomes available once nothing is
left. These same checks apply whether you run the tool on its own or as a step
inside a workflow.

## What happens next

After you submit, your job moves through a few states:

1. **Queued** — waiting for its turn to run.
2. **Running** — actively being worked on.
3. **Complete** — finished successfully, with results ready to view.
4. **Failed** — something went wrong; the log explains what.

You can watch all of this from the **My Jobs** page (see the next chapter). If
you've turned on job emails, MATOFX also emails you as the job starts, finishes,
or fails.

## Jobs that need approval first

Some tools are set up so that a job must be **approved** by a nominated person
before it runs. If that applies, your job waits in an approval step until an
approver signs it off. You don't need to do anything except wait — the approver
is notified, and your job continues automatically once approved.
