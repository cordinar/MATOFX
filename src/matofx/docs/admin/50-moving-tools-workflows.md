---
title: Moving tools and workflows between installations
audience: admin
capability: null
slug: moving-tools-workflows
summary: Why and how to move tools and workflows between MATOFX SPDM™ installations — exporting, importing, how references are matched, and reading the import report.
---

# Moving tools and workflows between installations

If you run more than one MATOFX SPDM™ installation — say a **test** site and a **live**
site — the **Migrate** feature moves tools and workflows between them.

## Why move tools and workflows

The common case is promoting work you've prepared and checked on a test site to
the live site, without rebuilding it by hand. It's also useful for sharing tools
between separate installations.

## Exporting a bundle

On the source installation, you **export** the tools or workflows you want into a
**bundle** — a single package you can carry to the other installation.

## Importing a bundle

On the destination installation (**Admin → Migrate tools/Workflows**), you
**import** the bundle. Each item is brought in on its own, so one problem item
doesn't stop the rest.

Drop the `.tar.gz` onto the upload area (or click to browse). Once it finishes
uploading it's **accepted automatically** and shown as a named, listed file with
a **Remove** button if you want to swap it for a different one. Then use
**Preview (dry-run)** to verify the bundle and see what *would* happen without
changing anything, and **Confirm import** to apply it.

Bundles are size-capped (256 MB by default) to protect the server; an oversized
upload is refused with a clear "too large" message. If you hit the cap, ask
whoever runs the installation to raise it, or export fewer items per bundle. A
bundle that unpacks to more than 1 GB once decompressed is also refused (a guard
against maliciously over-compressed archives); real bundles are nowhere near
this, so hitting it means the bundle isn't a genuine MATOFX export.

## How references are matched by name

A tool or workflow often refers to other things — an environment, a storage
location, an approver, and so on. These references travel **by name** and are
matched to the equivalent item on the destination. Make sure the destination has
the matching items set up.

### Destination pre-flight checklist

Because references match by name, the most common reason an import is skipped is a
referenced item the destination doesn't have under the same name. Before you
import, confirm the destination already has — with **matching names** — every:

- **software environment** the tool runs in;
- **storage location / file path** it writes to;
- **compute target** it uses (an always-on server, HPC queue, or GPU agent);
- **approver** named in its approval settings;
- **licence** it depends on.

Line these up first and the import goes cleanly; skip the check and you'll be
reading skip reasons and importing again.

## Why imported items arrive as "new"

Imported tools and workflows always arrive in the **new** (draft) state, whatever
state they were in on the source. This is deliberate: you review and **release**
them on the destination rather than have them go live automatically.

## Reading the import report

After an import, MATOFX shows a **report** of what came in and what was **skipped**,
with a plain-English reason for each skip — for example, a missing reference the
destination didn't have. Use it to fix anything that didn't come across and import
again.
