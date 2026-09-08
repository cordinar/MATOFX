---
title: Tracking and reading results
audience: user
capability: null
slug: tracking-results
summary: The My Jobs page, job states, live progress, logs, output files, the plain-language summary, the job report, 3D viewers, V&V badges, and what to do when a job fails.
---

# Tracking and reading results

Once you've submitted a job, the **My Jobs** page is where you follow it and read
what it produced.

## The "My Jobs" page

Open **My Jobs** from the left-hand menu. Across the top is a row of selectors —
**Project**, **Scenario**, **Tool**, **Workflow**, and **Job ID** — that narrow
down to the run you want. Pick a **project**, then a **scenario**, then a
**tool**, and My Jobs jumps straight to that tool's run in that scenario and
shows its inputs and results — there's normally just the one, so you don't have
to choose a Job ID by hand. (Choosing a **workflow** instead opens its run, with
the first step selected.)

If you already know a run's number, you don't have to work down the selectors at
all: type it into the **Job ID** field and press **Enter**, and My Jobs opens
that job and fills in its Project, Scenario and Tool (or Workflow) for you.
Leading zeros are fine — `0000008` and `8` both work.

Below the selectors you can also **search your jobs by name**, and — when no job
is open — see your most **recent activity**. Select any job to open its detail
view, where you'll find its inputs, its progress, its log, and its results.

## What each job state means

- **Created** — saved but not yet submitted. Your inputs are kept, but nothing
  runs until you submit.
- **Queued** — submitted and waiting for its turn. A job queues while it waits for
  a free slot to run in — and, where the tool requires it, while it waits for an
  **approver** to sign it off or for the right **resources** (such as a GPU) to
  become free. A job can sit here a while on a busy site; that's normal.
- **Running** — actively being worked on.
- **Complete** — finished successfully.
- **Failed** — stopped before it produced results. Usually that means something
  went wrong, but a job you **cancelled** also lands here, because cancelling
  stops a run rather than completing it. The job page tells you which: a
  cancelled job says *"Stopped by a cancel request"* and gives the time, so you
  never have to work it out from the log.

If a job seems stuck in **Queued**, it's almost always waiting for one of the
three things above rather than broken — check whether it needs approval before
assuming something's wrong.

## Before you cancel a long job

Cancelling is immediate and cannot be undone — the run stops where it is and
you keep only whatever it had already written. A long solve that appears to have
gone quiet has usually not stalled: some tools report progress in bursts, and a
big calculation can work for many minutes between messages. Before cancelling,
open the job's **log** and check whether its last message is consistent with
work still in progress. If you cancel and then want the results after all, the
job has to run again from the beginning.

## Watching a job run live

The page updates on its own — you don't need to refresh it. The status label
changes as the job moves along (**Queued** → **Running** → **Complete** or
**Failed**), the start time appears when the job actually starts, and the list
of recent jobs on the "My Jobs" home view keeps its labels up to date too.

## Reading the job log

Every job keeps a **log** — the running commentary the tool produced as it
worked. The log is the first place to look if a job behaves unexpectedly or
fails: it usually says exactly what happened.

While a job is running you can open the log and **read it as it fills up**.
A few things to know:

- The view **follows new output while you're at the bottom**. Scroll up to read
  something and it stops moving, so you can take your time; scroll back to the
  bottom and it starts following again.
- Opening a section keeps it open. It won't close under you while the job runs.
- A very chatty tool can produce more output than a browser can comfortably
  hold, so the live view shows the **most recent lines** and says so at the top.
  The complete log is there in full once the job finishes.
- There are up to three sections. **stdout** is the tool's normal output and
  opens automatically. **stderr** is where warnings and errors usually go.
  **launcher (diagnostic)** only appears if MATOFX itself had trouble starting
  the tool — if a job fails with nothing in stdout, look there.

## Reading the results in the page

Down the left of a finished job are small information cards saying what the run
*is*: the **Project** and **Scenario** it belongs to, and — underneath them — a
**Job** card with the run's own **name**, its job number and when it was created.
The Job card is there in every state, so a job that's still queued, running or
failed is just as easy to identify as a finished one.

The rest of the page shows the results **grouped and labelled** the way the tool's
author laid them out, so you rarely need to open a raw file. Each result is shown
with the viewer that suits its kind:

- **Single numbers** — shown with their **units** and label (for example
  *Peak temperature: 84.2 °C*), so headline figures read at a glance.
- **Tables** — rows and columns of values shown as a neat table in the page.
- **Charts from data** — where the tool marks a data file for plotting, MATOFX
  draws the **chart** for you (line or scatter) rather than making you open the
  numbers.
- **Interactive plots** — richer plots the tool produced can be **zoomed and
  panned** and read with hover tooltips, right in the page.
- **3D models, images, animations and time-varying fields** — see the next
  section.

## Viewing and downloading output files

Alongside the viewers above, a finished job lists the **files it produced**. You
can preview many of them in the page and **download** any of them to your
computer — individually, or as a single bundle. Two convenient downloads are the
**inputs-and-outputs summary** and the full **job package** (everything the run
kept), handy for archiving or sharing.

**Anything shown in the page can also be taken away.** Every previewed file —
image, chart, table, 3D model, animation, video, PDF or field — carries its own
**Download** button underneath it, saving the file exactly as the tool wrote it.
What you see in the page is a preview: it may be scaled to fit, re-themed to
match the dashboard, or capped in size, so the download is the authoritative
copy to attach to a report or hand to a colleague.

A couple of the viewers offer **two** downloads, because there are two different
things you might want:

- **Charts drawn from a data file** — one button saves the **data file** (the
  CSV as the tool wrote it), the other saves the **chart** as a standalone
  interactive HTML file. To save the chart as a *picture*, use the small camera
  icon on the chart's own toolbar (hover over the chart to reveal it).
- **Time-varying fields** — one button saves the **field file** itself, the
  other saves the whole series as a playable **HTML animation** (see below).

Image inputs are treated the same way: where the job's **Inputs** panel previews
a picture you supplied, a download button sits beneath it.

## The plain-language summary

When a job finishes, MATOFX SPDM™ writes a short **plain-language summary** at the top
of the results page — a sentence or two describing what happened: whether the run
succeeded, how long it took, its headline results, and what it produced. If the
job failed, the summary says so and points you at the log. It's there to give you
the gist at a glance, before you dig into the individual numbers and files. The
same summary appears at the top of the job **report** (below).

The summary is generated automatically from the run's own recorded results — every
figure in it comes straight from the job, nothing is invented.

## The job report page

Each job has a tidy **report** that gathers the key inputs and results into one
readable summary — handy for sharing or keeping a record of what a run did. It
opens with the same plain-language summary described above.

## Where a result came from (Provenance)

A job's detail page has a **Provenance** section that traces how the result
connects to everything around it — the inputs and earlier results that fed into
it, and anything later that was built from it. It's the quickest way to answer
"where did this number come from?".

Two sliders control how far the picture reaches:

- **Upstream** — how many steps *back* towards the things that fed this result.
- **Downstream** — how many steps *forward* towards results built on top of it.

Each slider has a small **?** next to it — hover over it for a reminder of what
it does.

Drag a slider and the picture refreshes when you let go (so a small change doesn't
redraw on every nudge). Anything beyond the depth you've chosen is summarised as
"hidden by depth" at the bottom, so you know there's more to see if you widen the
range. You can also switch between a **Graph** and a **Table** view, show or hide
particular kinds of item, and press **Reload** to refresh without changing
anything.

In the **Graph** view each item is drawn as a labelled box, sized to fit its
text and labelled with the item's everyday name (for example a tool's display
name, not its internal code), so you can read the picture at a glance.

If there's nothing to show yet, or you don't have access to an item, the section
says so in plain language rather than showing a technical error. And if the
**Graph** is too large or tangled to draw, it tells you so and points you to the
**Table** view, which always works — nothing is lost, it's just shown as a list
instead of a picture.

## Viewing 3D models, images, and animations

Where a tool produces visual output, MATOFX can show it in the page:

- **3D models** — rotate and zoom shapes and results directly in your browser.
- **Images** — shown inline, with a **Download** button beneath.
- **Animations and videos** — played in the page, with a **Download** button
  beneath.
- **Time-varying fields** (for example a temperature map that evolves over time)
  — shown as a 3D plot with a **time slider** and a **play** button to scrub
  through the steps. Use **Download animation (HTML)** to save the whole series
  as a single file that plays (and, for a 3D result, rotates) in any web
  browser — handy for sharing or dropping into a report.

## Understanding verification and validation badges

You may see a **Verification and Validation (V&V)** badge on a tool or a result.
*Verification and Validation* simply means the tool has been checked — that it
was built correctly and that its answers can be trusted for the job in hand. The
badge is a quick signal of that status. You don't have to do anything with it;
it's there to give you confidence in the result (and, if a tool hasn't been
checked, to flag that too).

For the full picture — how those checks are recorded and reviewed, what the
different kinds of claim mean, and how an approved check is inherited across
identical tools — see the *Verification and validation (V&V)* chapter.

## When a job fails: what to do

1. Open the failed job and read its **log** — it usually names the problem. Scroll
   to the **end** first; the actual error is normally on the last few lines.
2. Check your **inputs** for anything out of range or mistyped.
3. Fix the inputs and submit again, or ask your administrator if the tool itself
   seems to be at fault.

Most failures fall into a few familiar buckets:

| What the log suggests | Likely cause | What to try |
|---|---|---|
| A value is out of range, or the solver "didn't converge" | An input the tool can't handle (often an extreme value) | Pull that input back toward a sensible value and resubmit. |
| A missing or unreadable file | A required upload wasn't provided, or the wrong file was used | Re-upload the correct file, or pick one from the file library. |
| It ran a long time, then stopped | The run was too large/slow and hit a time limit | Simplify the inputs (coarser settings, smaller range) or ask your administrator. |
| An error deep in the tool with nothing wrong in your inputs | A problem with the tool itself, not your job | Send the job number to your administrator. |

The same inputs that fail for the real tool will also defeat a study or
optimisation that wanders into them — so fixing the failing case is worth it
before you automate around it.
