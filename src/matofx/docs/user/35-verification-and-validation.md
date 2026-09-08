---
title: Verification and validation (V&V)
audience: user
capability: null
slug: verification-and-validation
summary: What V&V records are, the register and its filters, the two record kinds and every option that goes with them, evidence types, the full draft → review → approved lifecycle, why you can't approve your own record, how approved claims are inherited across identical tools, and the badges everyone sees.
---

# Verification and validation (V&V)

**Verification and validation** is how MATOFX SPDM™ keeps a durable, evidence-backed
record that a tool — or a particular result — has been *checked*. It answers two
different questions:

- **Verification** — *was the tool built correctly?* Does it solve its equations
  the way it's supposed to, matching a known or analytical answer?
- **Validation** — *does the tool describe reality?* Do its answers agree with a
  physical experiment or a trusted benchmark for the job in hand?

These checks are captured as **V&V records**. Once a record is approved it drives
the **V&V badges** other people see on tools and results, so a claim only ever
becomes a badge after it has been reviewed and signed off — never on one person's
say-so.

> **Who can do what.** Anyone signed in can *browse* the register and open any
> record. Authoring a draft needs the **V&V create** capability; submitting,
> reviewing, approving, rejecting, and retiring need the **V&V curator**
> capability. If you don't see the **V&V records** page or the **New record**
> button, ask your administrator for the capability you need (see *If you have
> extra responsibilities (capabilities)*).

## The V&V records register

Open **V&V records** from the left-hand menu. It's a single browsable **register**
of every record on the site — claims about a **tool version** and claims about a
specific **job run**, together in one list. You never need an internal id: every
row reads in plain names — its subject, who recorded it, any evidence attached,
and its current state.

### Filtering the register

The **Filter** panel sits at the top of the page. All four filters combine (a row
must match every one you set):

- **Type** — *All types*, *Tool versions*, or *Job runs*.
- **State** — pick one or more of *Draft*, *Pending review*, *Approved*,
  *Rejected*, *Retired*. Leaving it empty hides retired records; choosing states
  explicitly lets you see retired ones too.
- **Kind** — the specific kind of claim (see the two tables below). You can pick
  several at once.
- **Search** — a free-text match over the tool or job name, the author, and the
  notes.

Press **Apply** to run the filter, or **Clear** to reset it. Results are paged;
use the arrows to move between pages.

Curators also get a **Review queue** tab, showing only the records that are
waiting on *their* decision — records you authored yourself are deliberately left
out of it (see *Why you can't approve your own record*).

## The two record kinds — and the options for each

Every record is either **about a tool version** or **about a job run**, and each
carries a different set of options. Pick the target by name when you create the
record; you never type an id.

### Records about a tool version

A tool-version record makes a general, reusable claim about a tool at a specific
**version**. There are two kinds:

| Kind | What it claims | When to use it |
|---|---|---|
| **Verification** | The tool solves its equations correctly — it reproduces a known or analytical answer. | You've confirmed the tool's maths against a textbook case, a manufactured solution, or a reference result. |
| **Validation** | The tool's answers match physical reality within a stated range. | You've compared the tool against experiment or a trusted benchmark. |

Alongside the kind, a tool-version record can carry:

- **Notes** — free text describing exactly what was checked and how.
- **Standards cited** — a comma-separated list of the standards or procedures the
  check followed (for example a NAFEMS benchmark or an internal QA procedure).
  These show as tidy chips on the record.
- **Confidence band** — an optional ± figure recording how close the agreement
  was, so a reader can judge how tight the check was.
- **Applicable range** — for a **Validation** claim, the input envelope the
  validation covers. Open the **Applicable range** section and the form lists the
  tool version's numeric inputs by name; set a **Min**, a **Max**, or both on any
  factor you validated over (leave a bound blank for open-ended, or leave a factor
  untouched to say nothing about it). This range powers the submit-time *envelope
  warning* (below): running the tool later with values outside it shows a soft
  nudge. The section only appears for a Validation claim about a tool — a
  Verification claim has no operating envelope.

The **Applicable range** editor loads the factors for the exact **version** you
picked, so the labels always match that version's form.

### Records about a job run

A job record pins a claim to one specific completed run rather than the tool in
general. There are three kinds, from strongest to lightest:

| Kind | What it claims | When to use it |
|---|---|---|
| **Validated against** | This run was checked against a trusted reference — an experiment or benchmark — and agreed with it. | The run reproduces a known-good result. |
| **Calibrated from** | This run's settings or parameters were *tuned* using reference data. | You used measured data to set the run up, rather than to check it afterwards. |
| **Sanity checked** | A lighter, informal plausibility check — the numbers look right, nothing is obviously wrong. | A quick confidence check that doesn't rise to a formal validation. |

Job records carry **Notes** and **evidence**; the standards cited and confidence
band above apply only to tool-version records.

## Recording a surrogate model's range — filled in for you

When you record a V&V entry against a **surrogate tool**, the **applicable range**
is filled in for you from the range that model was measured for coverage over
(see *Job cohorts and surrogate models*). You can widen it, narrow it, or
replace it — it's a starting point, not a decision. The point is that you begin
from what the model actually supports rather than from an empty box.

A note beside it says where the numbers came from and when, so anyone reading
the record a year later can tell a filled-in range from one somebody typed.

Two things the range can't say, and the note names them when they apply:

- **an input with fixed options** — a range of numbers can't express "carbon
  fibre or glass fibre, and nothing else", so if the model only ever saw certain
  options you need to note that limit yourself;
- **an input that was held at one setting** — a text setting like a mesh choice
  has no minimum and maximum to record.

This matters more than it might sound. If those limits go unrecorded, the record
quietly claims the validation covers more than it does — which is the opposite of
what a V&V entry is for.

Numeric inputs that were held constant *are* recorded, as a range with the same
value at both ends. That's not a mistake: the validation covers exactly that
value and no other.

## Attaching evidence

Evidence is optional but is what makes a claim defensible. A record can hold **as
many pieces of evidence as you like, in any mix of types** — press **Add evidence**
once per item and choose its type:

- **External URL** — a link to a report, dataset, or page held elsewhere.
- **Library file** — an existing file from the **File Library**; pick the file and
  the exact **version** you mean.
- **Upload file** — a file from your computer. Uploads are added to the **global
  File Library** (where they're immutable and content-hashed), so they may need a
  **library curator's** sign-off before they can be used — you can name a reviewer
  for that publish when you add it.

Each piece of evidence appears on the record as a link (for URLs) or a named,
downloadable file, so a reviewer can follow it straight from the record.

## The record lifecycle

Every record moves through a fixed set of **states**. The buttons you see on a
record depend on the state it's in and the capability you hold.

1. **Draft** — you've created the record but not yet asked anyone to review it.
   Only you (and other curators) can see it as a work-in-progress. Drafts can be
   edited.
2. **Pending review** — you've **submitted** it for a decision. You choose a
   **reviewer** when you submit (see below).
3. **Approved** or **Rejected** — a reviewer has **decided**. Approving turns the
   claim into a live badge; rejecting records why it was turned down. A rejection
   *requires a reason*; an approval can carry an optional note.
4. **Retired** — a record has been **withdrawn** so it no longer counts. A curator
   can retire a record from **any** live state — draft, pending review, approved,
   or rejected. Retiring is not deletion: the history stays in the audit trail, and
   you can re-record and re-submit to bring the claim back.

> If your site is configured to record approvals **directly** (the review step
> turned off), a submitted record is approved as it's submitted rather than
> waiting in *Pending review*. The default is to require a reviewer.

### Submitting for review — choosing a reviewer

When you **submit** a draft, you must nominate a **reviewer**: a *different* V&V
curator who will make the decision. This is the **directed-approver** rule — you
pick who reviews your work, and it can't be you. If you're the only curator on the
site, submission can't proceed until an administrator grants the V&V curator
capability to at least one other person.

### Why you can't approve your own record

A record you submitted can only be decided by *someone else*. When you open a
pending record you submitted, the **Approve** and **Reject** buttons are shown but
**greyed out and disabled**, with a note explaining why — the directed-approver
rule forbids self-approval, so the system would refuse the action anyway. Any
other curator opening the same record sees those buttons live. For the same
reason, records you authored never appear in your own **Review queue**.

### Retiring a record — from any state

Retiring is the curator's catch-all "withdraw this" action, available on the
record's detail page (**Retire**) wherever the record sits in its lifecycle:

- **A pending record** — retiring also **withdraws it from review**: the reviewer's
  queue entry disappears and the open request is closed, with no approve/reject
  decision recorded. You don't have to reject a record just to get it out of review.
- **An approved tool record** — retirement is **cohort-wide**: every tool version
  that *inherited* the claim (see below) stops showing it.
- **A draft, rejected, or job record** — retiring simply removes that record; a job
  record's retirement only affects that one job's V&V card.

Retiring is never blocked by the self-approval rule — a curator may withdraw a
record they submitted themselves (it's a withdrawal, not a decision).

## How approved tool claims are inherited

A verification or validation of a tool version applies to the **exact tool bundle**
that was checked. MATOFX recognises when another tool version ships a
**byte-identical** bundle and lets it **inherit** the approved claim automatically —
so you don't have to re-verify an unchanged tool every time its version number
moves. This is why a tool you never explicitly recorded against can still show a
V&V badge, and why retiring a tool record clears the badge for the whole cohort of
identical bundles at once. Job records are always per-run and are never inherited.

## Submit-time checks tied to V&V

At *submit* time on a tool that carries V&V, the run form can show you a **nudge** —
it never blocks your run:

- **Envelope warning (advisory).** When an approved validation records an
  **applicable range** (set in the range editor described above), the run form
  watches the values you enter and shows a soft **"Outside the approved V&V
  envelope"** banner if you stray beyond it. It never blocks your run — it's a nudge
  that you're asking the tool for an answer beyond where it was proven. The envelope
  is the *union* of every approved validation's range across the tool's cohort, so
  it widens as more validations are approved.

## Migrating V&V to a new tool version

When a tool is updated, its new version starts with **no** V&V of its own — unless
the new bundle is byte-identical to the old one (in which case it *inherits*
automatically, as above). For a genuinely changed bundle, a curator can **carry the
old version's approved records forward** instead of authoring each from scratch.

Press **Migrate to new version** on the V&V records page (curators only) and:

1. Pick the **tool**.
2. Choose the **source** version (where the approved records live now) and a
   different **target** version (the new one).
3. Tick the **approved records** to bring forward — only approved records are
   eligible, and you choose them individually.
4. Press **Migrate**.

Each chosen record is **copied onto the target version as a fresh draft**, owned by
you, carrying over its kind, applicable range, confidence band, standards, notes,
and evidence. The copies are **drafts on purpose**: because the tool bundle changed,
the original sign-off doesn't transfer — you review each one and **re-submit it for
approval** so a human confirms the claim still holds for the new version. Migrating
never touches the source records; they stay exactly as they were.

Migration is **safe to repeat**: if a record you pick already has an identical copy
on the target, it's skipped rather than duplicated, and any records that can't be
brought forward come back listed with a plain-language reason.

## The badges everyone sees

You don't need any capability to benefit from V&V: approved records surface as
**V&V badges** on tools and results. A badge is a quick, at-a-glance signal that a
tool has an approved **verification**, an approved **validation**, or has a check
**pending** — and, because a claim can be inherited, a badge may reflect a check
recorded against an identical earlier version. If a tool has *no* approved check,
that shows too, so you know to treat its output with the appropriate care.

## Starting a record from where its subject lives

As well as the **New record** button on the register, you can start a record
straight from its subject, already pre-filled:

- **Add V&V record** on a tool's **V&V** tab — targets that tool version.
- **Record V&V** on a completed **job's** page — targets that run.

Both open the same authoring flow described above, with the target fixed so you
only choose the kind, notes, range (for a tool validation), and evidence.
