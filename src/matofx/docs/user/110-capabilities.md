---
title: If you have extra responsibilities (capabilities)
audience: user
capability: null
slug: capabilities
summary: What a capability is, and the extra tasks it unlocks — approving simulations, recording and curating V&V, curating the file library, the materials database, Job Cohorts and surrogate models, and signing off optimisation campaigns.
---

# If you have extra responsibilities (capabilities)

Most of this guide applies to everyone. This chapter covers extra jobs that only
**some** people are given. If a section below doesn't match what you can see in
MATOFX SPDM™, it simply means you haven't been given that responsibility — which is
perfectly normal.

## What a "capability" is

A **capability** is an extra right added on top of your normal account. It lets a
regular user take on a specific extra task — such as looking after a shared
catalogue — without becoming a full administrator. Your administrator grants
capabilities, and each one quietly adds the relevant page to your menu.

## Approving simulations (if you are an approver)

If you're an **approver**, certain jobs can't run until you sign them off. You'll
see an **Approvals** area, and your home page shows how many simulations are
waiting for you. Open one, review what's being run, and approve or reject it. Once
you approve, the job continues on its own.

## Looking after the shared file library (if you are a library curator)

If you're a **file library curator**, you have a **File library** page where you
add and manage the shared, approved files that other people pick from when filling
in tool forms. Curating the library means everyone draws on the same trusted
inputs.

## Looking after the materials database (if you are a materials curator)

If you're a **materials curator**, you have a **Materials** page for managing the
shared catalogue of materials and their properties, so that material choices
across the site come from one approved source.

## Recording verification and validation (if you have a V&V capability)

Verification and Validation (V&V) comes in two capabilities. **V&V create** lets
you *author* a draft record — capturing a check you've run. **V&V curator** lets
you *submit, review, approve, reject and retire* records. You may hold one or both;
either one gives you the **V&V records** page. *Verification and Validation* means
checking that a tool was built correctly and that its results can be trusted, and
approved records are what produce the V&V badges other users see on tools and
results.

The **V&V records** page is a single browsable **register** of every record —
claims about a **tool version** and claims about a specific **job run**, in one
list. You filter by **type**, **state**, and **kind**, and **search** by name;
click a row to open the record, see its evidence and history, and (as a curator)
**submit**, **approve**, **reject**, or **retire** it (retire works from any
state — it also withdraws a record still in review). Curators also get a **Review
queue** tab showing just the records waiting on *their* decision — and you can't
approve a record you submitted yourself. When a tool gains a new version, **Migrate
to new version** carries its approved records forward as fresh drafts to re-review.

The full walkthrough — the two record kinds and every option that goes with them
(including the applicable-range editor), the difference between *verification* and
*validation* (and the three kinds of job claim), evidence types, the draft → review
→ approved lifecycle, retiring and migrating records, and how an approved claim is
inherited across identical tools — is in the *Verification and validation (V&V)*
chapter.

## Building Job Cohorts (if you are a cohort curator)

If you're a **cohort curator**, you can create and manage **Job Cohorts** — batches
of past completed runs of one tool, gathered into a dataset you can fit a surrogate
model on. (See the *Job cohorts and surrogate models* chapter for what they're for.)
Anyone can *view* a cohort and its results; this capability is what lets you *build*
and edit them.

## Releasing surrogate models (if you are a Surrogate Model Curator)

If you're a **Surrogate Model Curator**, you check a **surrogate model** before it
is released for general use across the site. Fitting and using models is open more
widely, but a model only becomes a **globally** available stand-in after a super
user releases it — and your review is the check on that step. See *Job cohorts and
surrogate models* for what surrogate models do.

## Signing off optimisation campaigns (if you are an optimisation approver)

If you're an **optimisation approver**, an **optimisation study** against a governed
tool can't *start* until you sign off its setup. You review what's being optimised —
the target tool, the variables and the goals — and approve or reject the campaign
before it consumes compute. (This is separate from an ordinary job **approver**;
see *Optimisation* for where the gate appears.)
