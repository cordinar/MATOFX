---
title: Monitoring and records
audience: admin
capability: null
slug: monitoring-and-records
summary: The audit log, usage statistics and its six tabs, the live-jobs view, data lineage, and the read-only database explorer.
---

# Monitoring and records

MATOFX SPDM™ keeps detailed records of what happens, and gives you tools to review them.
There are four, each answering a different question — reach for the right one:

| To answer… | Use |
|---|---|
| "Who did what, and when?" | The **audit log** |
| "How busy is the site, and on what?" | **Usage statistics** |
| "Where did this result come from?" | **Data lineage** |
| "Let me look at the raw records myself" | **Explore database** (read-only) |

## The audit log

The **audit log** (**Admin → Audit log**) is a **tamper-evident** record of
who did what and when — sign-ins, changes to users and tools, approvals, and so
on. "Tamper-evident" means the entries are linked together so that any attempt to
alter or remove one can be detected. It's your trail for security and compliance.
Entries are shown newest first; click any row to expand it and see the full
detail, including the payload and the hash-chain links.

A **filter bar** at the top narrows the list (all filters combine, and are
applied when you press **Apply**):

- **User** — type a person's **id**, **full name**, or **username** and the log
  shows only their entries. You don't need to know any internal identifier.
- **Actor type** — restrict to **user**, **service**, **system**, or
  **anonymous** actions. Use this to see, for example, only automated
  **system** activity such as the retention sweeper.
- **Action** and **Resource type** — pick one or more from drop-down lists that
  contain only the values **actually present** in the log, so you never guess at
  spelling. Selecting several of either matches any of them.
- **Outcome** — success, failure, or denied.
- **Resource id** and **Correlation id** — match an exact record or trace one
  logical operation across entries.
- **Since / Until** — a date range, chosen from a calendar. "Until" includes the
  whole day you pick.

Press **Clear** to reset every filter. **Export CSV** downloads the current
filtered view (up to 50,000 rows) for hand-off to an auditor or spreadsheet;
each row includes the resolved user name alongside the raw fields.

If your software supplier asks you to demonstrate that these records are intact,
don't send the CSV — it is a filtered extract and carries your project and job
names. Use a **compliance report** instead: it proves the same thing about the
whole record chain while containing only licence figures. See *Licence
compliance reports*.

**Chain integrity** — press **Verify chain** to recompute the hashes, check the
links between entries, and (when a signing key is configured) validate the
signatures. A green result confirms nothing has been altered; a failure names
the first record that didn't check out.

## Usage statistics

**Admin → Usage statistics** shows how the site is being used — how many
jobs are run, by whom, and on which tools — so you can see activity and spot
trends.

A **filter bar** at the top narrows every tab at once: choose a time **period**
(or a custom date range) and, optionally, restrict to particular **tools**,
**teams**, **roles**, or **execution targets**. Each of those is a multi-select —
leave one empty to mean "all". Press **Apply** to refresh the tabs.

Selecting **World** in the **Teams** filter means **all teams** (everyone), the
same as leaving Teams empty — "World" is the everyone marker, not a team people
are assigned to, so picking it doesn't narrow the results down to nothing.

The figures are spread across **six tabs**, each answering a different question:

- **Overview** — the headline numbers: how many jobs ran in the period, success
  rate, active users, and the busiest tools.
- **Compute & throughput** — how much compute was consumed and the balance of
  jobs **submitted vs completed** over time, so you can see load and backlog.
- **Reliability** — failure rates and where they cluster (which tools, which
  targets), for spotting a flaky tool or environment.
- **People & adoption** — who is using the site and how widely — active users and
  which tools and features are actually being taken up.
- **Seats & licence** — usage against the limits your licence sets, so you can see
  how much headroom you have.
- **Infrastructure** — activity broken down by where jobs ran (the local machine,
  always-on servers, HPC clusters).

**Export** the current view for a report, and — where enabled — send a periodic
**digest** by email. The filter bar applies to every tab at once.

## Live jobs

When you have the right permission, Usage statistics also carries a **Live jobs**
tab — a real-time list of everything **in flight across the whole site**: running
**jobs**, parametric-study and optimisation runs included. Each row shows what's
running, for whom, and where. Filter the list to find a particular run, and — if
you also hold the cancel permission — **select several at once and cancel them
together** to clear a stuck batch or free resources quickly. It's the install-wide
counterpart to a user's own My Jobs page, and the first place to look when the site
feels busy or a run needs stopping. (See also *Looking after MATOFX SPDM™ day to
day* for freeing a single stuck job.)

## Data lineage

**Data lineage** (**Admin → Lineage**) shows **how a result traces back to
its inputs** — which inputs and earlier results fed into a given output. It answers
"where did this number come from?", which matters for trust and for investigating
a surprising result. (The same view appears as the **Provenance** section on a
job's or study's page.)

Use the **Upstream** and **Downstream** sliders to set how many steps the picture
reaches back towards the things that fed a result and forward towards what was
built from it (each has a hover **?** explaining it). Drag a slider and the graph
refetches when you release it (so it doesn't redraw on every nudge); anything past
the chosen depth is reported as "hidden by depth". You can switch between a
**Graph** and a **Table** view and show or hide particular kinds of item — the
view is fully reactive, so there's no separate reload to press.

Click any item — a node in the graph or a row in a table — to open it in its own
page. Only items you can actually open are shown as links (with an **↗** marker on
table rows and a pointer cursor on graph nodes); restricted or out-of-depth items
are shown as plain, inert text. Beneath the tables, the **Relations** list spells
out each relationship in plain language — for example *Published by → Robert
Cordina* or *Consumed tool → Heat Transfer v3* — again linking only to things you
can open.

You can **download** the graph you're looking at: **Download as Image** saves a
picture (an SVG that opens in any web browser and pastes straight into a report or
email), and **Download JSON** saves the same relationships as data for other tools.

## Exploring the database (read-only)

**Admin → Explore database** lets you look directly at the underlying records
in a **read-only** way — you can browse and search, but not change anything. It's a
power-user tool for answering detailed questions; everyday administration is better
done through the dedicated pages.

Its **Tables** tab browses any one table directly. Pick a table from the list on the
left — type in the box above the list to filter it when there are many. Use the
collapsible **Filter** panel to narrow the rows: build conditions (matching a column
against chosen values or free text, combined with ALL/ANY groups), then **Apply**.
Once applied the panel folds away and each active condition shows as a chip above the
results — click a chip's **✕** to drop just that condition. **Sort** by clicking a
column header in the results (click again to reverse); the sort applies across the
whole filtered table, not just the page on screen. The header row stays pinned while
you scroll, and the table footer pages through results and sets how many rows load at
a time. Clicking a row in the `job` table reveals its run log inline.

Its **User activity** tab pulls one person's footprint together: pick a user and you
see their projects, the scenarios and jobs they created, the **workflows they own or
have run** (both admin-released and custom workflows, with how many times that user
ran each and when), the **parametric studies, optimisations, surrogate models and job
cohorts** they created (each shown only when that feature is switched on), their
audit-trail entries, and their login grant/revoke history. Every list links straight
through to the relevant record so you can jump from "who" to "what".

Use **Export CSV** (top right of the activity view) to save the whole footprint as a
single spreadsheet file — the user's details (name, username, role, approver and
active status) followed by one labelled block per section. It's handy for sharing an
access review or keeping an offline record.
