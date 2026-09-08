---
title: Project information fields
audience: admin
capability: null
slug: project-information-fields
summary: What project metadata is for, creating the fields, and making them required.
---

# Project information fields

You can ask users to record extra information about each project — a customer
name, a programme code, a classification, and so on. These are **project
information fields** (project metadata), managed in **Admin → Project
metadata**.

## What project metadata is for

Project information fields let you capture the details your organisation needs to
keep alongside the engineering work — for searching, reporting, and good
record-keeping — in a consistent way across every project.

## Creating metadata fields

Select **New field** and fill in the form. Each field has:

- **Key** — a short internal identifier (lowercase, no spaces, e.g. `region`).
- **Label** — the human-friendly name users see on the project form, and the
  column heading on the Projects list if you choose to show it there (e.g.
  *Region*).
- **Type** — the kind of value the field holds:
  - **Single select** — the user picks one option from a list you define.
  - **Multi-select** — the user can pick several options from your list.
  - **Date** — a calendar date (entered as `YYYY-MM-DD`).
  - **Number** — a numeric value; you choose the format (whole number, decimal,
    or a custom mask such as `AA-000-0000`).
- **Display order** — where the field sits on the project form, chosen from a
  **1–20** drop-down (1 = first).
- **Show this field's value on the Projects list** — when ticked, the field's
  value appears beneath each project on the **Projects** list (Dashboard →
  Projects), so you can see it at a glance without opening the project. (It shows
  for projects that have a value for the field; projects without one show
  nothing.)

> **The Key and Type can't be changed once the field is created** — projects
> store their values against them. For example, a field with key `region` and
> type Single select stays that way. If you need a different key or type, retire
> the field and create a new one. Everything else (label, values, order, whether
> it's required or shown in the table) stays editable.

### Adding the values for a choice list

For a **Single select** or **Multi-select** field you add its allowed values
**in the same form, before saving**. A single-select needs **at least one**
value; a multi-select needs **at least two**. Use **Add another value** for more,
and the bin icon to drop one. The field and its values are created together, so
it's ready to use the moment you save — there's no second step.

**Date** and **Number** fields take no value list — the user types the value
directly when creating a project — so the values editor doesn't appear for them.

After saving, you can **drag the value rows to reorder them** on the Project
metadata page, and **Add value** appends a new one to the end of the list.

**Prefer a choice list over free text wherever you can.** A field like
*Classification* or *Programme* is far more useful as a fixed set of options than
as a free entry: it's consistent across projects, it filters and reports cleanly,
and users can't spell it three different ways.

## Making fields required

You can mark a field as **required**, so users must fill it in before they can
create the project. Use this for the information you can't afford to be missing.

## Retiring fields and values

Nothing is ever deleted — you **retire** what you no longer want offered. Retired
fields and values stay on projects that already use them but are never offered
for new projects.

- **Retiring a whole field** (the **Retire** button next to its name) hides the
  field *and all of its values* from new projects. On the Project metadata page
  the values are then shown struck through, so it's clear they're no longer
  live.
- **Retiring only some values** — keep the field **active** and retire just the
  individual values you no longer want, leaving the rest available.
- **Reactivating a field** brings the field back, but its values **do not**
  reactivate automatically — you reactivate the ones you still want **one by
  one**. This is deliberate: if you bring a field back after a while, some of its
  old values may no longer apply, so you choose which to restore.
