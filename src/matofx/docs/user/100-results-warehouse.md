---
title: The results warehouse and key numbers
audience: user
capability: null
slug: results-warehouse
summary: Browsing the key numbers MATOFX SPDM™ pulls out of completed jobs — filtering by tool, version and value range, spotting trends over time, checking validation status, and exporting. Only available when switched on.
---

# The results warehouse and key numbers

> This feature is only available if your administrator has switched it on. If you
> don't see **Results warehouse** in the menu, your site isn't using it.

## What the results warehouse is

As jobs finish, MATOFX SPDM™ can automatically pull out the **key numbers** from each
one and collect them in a single table — the **results warehouse**. Instead of
opening dozens of jobs to copy out one figure from each, you read them all in one
place, sort them, filter them, and watch how they trend over time.

## What a key number (KPI) is, in one line

A **Key Performance Indicator (KPI)** is just an important result value worth
tracking — a peak temperature, a total mass, an efficiency — chosen by whoever set
the tool up. A tool only appears in the warehouse if its author has marked at
least one output as a KPI.

## Browsing the key numbers by tool

Open **Results warehouse** and you'll see a tile for each tool that declares KPIs,
with how many it tracks. Choose a tool to open its table: one row per completed
job, a column for each key number, so you can scan and sort them. Alongside the key
numbers, each row shows readable context — the **project**, **scenario**,
**requestor** and **tool version** by name (not internal id numbers) — and when the
job completed. Where a tool has several **versions**, a dropdown lets you focus on
one version at a time so you're not comparing numbers that were produced by
different builds of the tool.

Tick the box on any rows to build a **selection**: the *N selected* count, the
**Clear selection** button, and the **View in Compare** / **Download selected**
actions all update as you tick and untick. **Clear selection** empties the whole
set — including rows ticked on other pages.

## Filtering to what matters

The warehouse table can grow long, so the **filter** card narrows it down:

- **By when the job completed** — a recent window rather than all history.
- **By value range on any KPI** — a minimum and/or maximum, so you can ask *"show
  me only the runs where peak temperature was above 90 °C"* and see just those.
- **Reset** clears the lot in one go.

A useful habit: tick the rows you care about as you filter. Your **selection
survives** filtering and paging — a row you've ticked stays selected even when a
filter temporarily hides it — so you can gather a set across several filter passes.

## Spotting trends over time

Beyond the table, the warehouse can chart a KPI **over time**, so a slow drift or
a step change after a tool update becomes obvious at a glance — something you'd
never spot reading individual jobs.

## Checking validation status

Where your site uses verification & validation, each row can carry a **V&V status
badge** and you can filter by it — so you can, for example, look only at results
that have been validated, or pick out the ones still pending a check.

## Exporting

When you want the numbers elsewhere, **export** the table — as **CSV** for a
spreadsheet, or as **PDF** for a report — and you get exactly the rows and columns
you've filtered to, not the raw firehose.
