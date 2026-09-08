---
title: Organising your work
audience: user
capability: null
slug: organising-your-work
summary: How projects, scenarios and jobs fit together, the projects list, creating and archiving projects, working with scenarios, and sharing a project with colleagues.
---

# Organising your work

MATOFX SPDM™ keeps your work in a simple three-level structure. Understanding it
makes everything else — running tools, finding past results, sharing with
colleagues — easier.

## Projects, scenarios and jobs

- A **project** is the top-level container for a piece of work — a component, a
  study, a customer programme. It's what you share with colleagues, and it's where
  access is controlled.
- A **scenario** lives inside a project and groups the runs that belong together —
  for example one design variant, one load case, or one round of investigation. A
  project can hold as many scenarios as you need.
- A **job** is a single run of a tool with a particular set of inputs, and it lives
  inside a scenario. Running a tool always means picking (or creating) the project
  and scenario the job belongs to. See *Running a tool (submitting a job)* for how
  to launch one.

So the hierarchy reads **project → scenario → job**. When you open a result later,
MATOFX shows you exactly which project and scenario it came from.

## Your projects list

The **Projects** page (open **Projects** in the sidebar, or the **My Projects**
footer link on your home page) lists every project you can see. The **search box**
filters by a project's **name, its metadata values, or its owner** — type any of
these and the list narrows as you go. The **Show archived** switch brings archived
projects into the list too.

Each project card shows its **name** and **description**, the metadata fields your
administrator chose to surface on the list, and — when the project has been
shared — **who it's shared with and their role**. Select a card to open the
project.

## Creating a project

Start a project from the **Create** card on your home page (choose **Project**), or
with the **New** button on the Projects list. Give it a name and description, and
fill in any **project information fields** your administrator has set up — some may
be required before you can save. You can edit these details later from the project
page with **Edit** (visible to the project's Creator and Owners).

## Opening a project

A project page shows its description and metadata, an **Access** panel listing
everyone who can see or work in it (grouped as Creator, Owners, Members and
Viewers), and its **scenarios**. The Creator and Owners also see **Edit**,
**Share** and **Archive** buttons here.

## Working with scenarios

Inside a project, the **Scenarios** list holds each named grouping of jobs.
Select **New scenario** to add one (give it a name and an optional description),
then open a scenario to see its jobs and their states, and to **submit a new job**
into it. Creating scenarios and running jobs needs the **Member** role or higher
on the project (see *Sharing a project* below).

## Archiving a project

When a project is finished, a Creator or Owner can **Archive** it. Archiving tidies
it out of the everyday list without deleting anything — its scenarios, jobs and
results stay intact and remain searchable. Bring archived projects back into view
any time with the **Show archived** switch on the Projects list.

## Sharing a project

Open a project and select **Share** (you'll only see this if you're the
project's Creator or an Owner). The dialog has two parts:

- **People with access** — everyone who can currently see or work in the
  project. The **Creator** is pinned at the top and always keeps full control —
  they can't be changed or removed. For everyone else, change their role from
  the drop-down, or select the **bin** icon to remove them.
- **Add a person** — pick a colleague, choose the role to give them, and select
  **Add**. People who already have access don't appear in this list — to change
  what they can do, use their row under **People with access** instead.

The three roles are:

- **Viewer** — can open the project and read its scenarios and results.
- **Member** — everything a Viewer can do, plus create scenarios and run jobs.
- **Owner** — everything a Member can do, plus share the project with others and
  archive it.

Your changes are **staged** while you work — add several people, adjust roles,
remove someone — and nothing is applied until you select **Save**. The **Save**
button stays greyed out until you've actually changed something. Because nothing
takes effect until you save, removing a person is easy to undo: just add them
back, or select **Cancel** to discard every change and close the dialog.
