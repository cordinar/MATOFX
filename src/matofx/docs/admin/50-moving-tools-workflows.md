---
title: Moving tools and workflows between installations
audience: admin
capability: null
slug: moving-tools-workflows
summary: Why and how to move tools and workflows between MATOFX SPDM™ installations — what a bundle carries, exporting, importing, how references and links are matched, the rules for items that already exist, and reading the import report.
---

# Moving tools and workflows between installations

If you run more than one MATOFX SPDM™ installation — say a **test** site and a **live**
site — the **Migrate** feature moves tools and workflows between them.

## Why move tools and workflows

The common case is promoting work you've prepared and checked on a test site to
the live site, without rebuilding it by hand. It's also useful for sharing tools
between separate installations.

## What a bundle carries

- **Tools:** the complete tool description — inputs and outputs (with their groups
  and layout), presets, input rules, KPI settings, files to keep or delete,
  resources, approval settings, email notifications, teams, approvers, required
  licences and **workflow links** (which tools feed which) — plus the tool's files
  (scripts, icon, guide images), its execution target, its **run cap** and its
  **optimisation start-approval** setting.
- **Workflows:** name, description, teams, member tools in order, and the
  workflow's **icon**.

A bundle does **not** carry history: change logs, earlier versions, V&V records,
approvals, jobs and results stay on the installation where they happened.
**Surrogate tools can't be exported** — they run a fitted model held on the source
installation; re-create the surrogate on the destination instead. In the export
picker they are shown greyed out.

## Exporting a bundle

On the source installation (**Admin → Migrate tools/Workflows**), tick the tools
and workflows you want, add an optional note, and click **Generate bundle**.

Leave **Include required tools** ticked (the default) and MATOFX also adds every
tool a chosen workflow uses, and every tool a chosen tool takes inputs from, at
their current versions — so the bundle works on its own. When the bundle is
ready, MATOFX tells you which extra tools it added.

## Importing a bundle

On the destination installation (**Admin → Migrate tools/Workflows**), you
**import** the bundle. Each item is brought in on its own, so one problem item
doesn't stop the rest — even an unexpected error only skips the item it hit.

Drop the `.tar.gz` onto the upload area (or click to browse). Once it finishes
uploading it's **accepted automatically** and shown as a named, listed file with
a **Remove** button if you want to swap it for a different one. Then use
**Preview (dry-run)** to see exactly what *would* happen without changing
anything, and **Confirm import** to apply it. The preview runs the real import and
then undoes it, so what it reports is what the import will do.

Bundles are size-capped (256 MB by default) to protect the server; an oversized
upload is refused with a clear "too large" message. If you hit the cap, ask
whoever runs the installation to raise it, or export fewer items per bundle. A
bundle that unpacks to more than 1 GB once decompressed is also refused (a guard
against maliciously over-compressed archives); real bundles are nowhere near
this, so hitting it means the bundle isn't a genuine MATOFX export. A bundle made
by a **newer** version of MATOFX is refused with a message asking you to upgrade
this installation first.

## How references are matched

A tool or workflow refers to other things on the installation. These travel **by
name** and are checked against the destination the same way a normal install
checks them:

| Reference | If it doesn't match on the destination |
|---|---|
| Software environment, storage location, always-on server, HPC queue | The tool is **skipped** — create the item with the same name, then import again. |
| Material class or property used by a material input | The tool is **skipped** — add it in the materials database first. |
| Approvers | An approver who doesn't exist here, is deactivated, or doesn't have **Can approve runs** ticked is **dropped** (the report says which and why). If that leaves a tool that needs approval with no approvers, the tool is **skipped**. |
| Teams | Unknown teams are **dropped**. If **none** of an item's teams exist here, it is imported **with no teams** and can't be promoted until you choose its teams — see below. |
| Licences | MATOFX can't check licence availability automatically; the report lists the licences each tool needs so you can confirm them. |

### Items imported without teams

An item with no teams would be visible to everyone once released, so an item whose
teams all failed to match is held back: its edit page shows an amber notice, the
tools and workflows lists say *no teams yet*, and promoting it out of **new** is
refused until you pick at least one team and save. Choose **World** if it really is
for everyone.

### Destination pre-flight checklist

Before you import, confirm the destination already has — with **matching names** —
every software environment, storage location, compute target, approver (with *Can
approve runs* ticked), team and material class the bundle's tools use. Line these
up first and the import goes cleanly.

## Workflow links

A link between two tools (one tool's output feeding another's input) is recorded on
**both** tools. On import MATOFX never removes a link; it completes the missing
half wherever it can and tells you about any it can't:

- **Both tools in the bundle** — the link comes across as it is.
- **The feeding tool is already here** — MATOFX adds the new tool to its list of
  downstream tools (an ordinary edit of that tool: a new patch version and a
  change-log entry). The report says *Also updated tool …*.
- **The feeding tool isn't here** — the tool still imports and keeps its link; the
  report names the missing tool. That input can't take the other tool's output
  until the missing tool is imported (it can still be entered by hand), and the
  link completes when it arrives.
- **A tool already here takes this tool's outputs** — the imported tool lists it
  as downstream straight away.

If a link points at an output the feeding tool doesn't have on this installation,
the report says so; re-pick the output on the tool's Edit Tool page.

## Items that already exist

| Situation | Result |
|---|---|
| Same version, same content | Nothing changes (re-importing a bundle is safe). |
| Same version, different content | **Skipped** — bump the version on the source and export again. |
| Older version than the one here | **Skipped** — importing would downgrade it. |
| Archived here | **Skipped** — unarchive it first. |
| Newer version | **Updated.** If it was in *test* or *released*, it goes back to **new** and is unavailable to users until you promote it again; the preview and report warn you. |
| A user's personal (custom) workflow has the same alias | **Skipped** — an imported workflow never replaces someone's own workflow. |

Workflows are also checked the way the workflow editor checks them: member tools
must exist and not be archived, and a tool must come after any member it takes
inputs from.

## Why imported items arrive as "new"

Imported tools and workflows always arrive in the **new** (draft) state, whatever
state they were in on the source. This is deliberate: you review and **release**
them on the destination rather than have them go live automatically.

## Reading the import report

After a preview or an import, MATOFX shows a **report** of what came in, what was
**skipped** (with a plain-English reason), warnings such as dropped approvers or
incomplete links, and any other tools the import updated. Use it to fix anything
that didn't come across and import again — re-importing the same bundle only
picks up what's still missing.
