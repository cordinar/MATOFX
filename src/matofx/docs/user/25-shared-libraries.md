---
title: The shared file and materials libraries
audience: user
capability: null
slug: shared-libraries
summary: Browsing and using the shared file library and materials database — scopes and versions, filtering, opening a file or material, and picking one inside a tool form.
---

# The shared file and materials libraries

MATOFX SPDM™ can hold two shared, governed catalogues so everyone works from the
same trusted inputs instead of passing files around:

- the **File Library** — approved input files (meshes, geometries, data sets); and
- the **Materials database** — a catalogue of materials and their properties.

Both appear under the **Libraries** group in the sidebar when your site has them
switched on. Anyone signed in can **browse** them; adding and approving content is
governed (see *Who can add and approve*, below). You'll also meet both directly
inside a tool form — see *Using them in a tool form*.

## The File Library

Open **File Library** to see the files you're allowed to view. Each file has:

- a **scope** — **Personal** (just you), **Project** (everyone on a project), or
  **Global** (the whole site);
- a **state** — **Active**, **Pending** review, or **Retired**; and
- a **version history** — files are versioned, so a reference always points at a
  specific, unchanging version.

Use the **filters and search** at the top to narrow the list by scope, state or
text. Select a file to open its **detail** page, where you can see every version
(and download the one you need), which tools it's assigned to, and its scope. A
file is never edited in place or deleted — a new **version** is added, and a file
that's no longer wanted is **retired** rather than removed, so existing references
keep resolving.

## The Materials database

Open **Materials database** to browse the site's materials. Materials are always
**global** (there's no personal or project tiering), and each is **versioned** the
same way files are. Filter or search the list, then select a material to open its
**detail** page: its properties are grouped by the material **class** it belongs
to, and you can see each version's property grid and when it was recorded. As with
files, materials are retired rather than deleted, and a change means a new version.

## Using them in a tool form

You don't have to visit the Libraries pages to use them — a tool's form pulls from
them directly where the tool author allowed it:

- **File fields** that accept a library file let you **pick an existing, approved
  file** (and the exact version) from the File Library instead of uploading your
  own.
- **Material fields** let you **choose a material** from the Materials database, so
  its properties come straight from an approved source rather than being typed in
  by hand.

Picking from a library this way keeps everyone using the same trusted inputs and
records exactly which version a job used. See *Running a tool (submitting a job)*
for where these appear on the form.

## Who can add and approve

- **Files** — depending on the rights your administrator gave you, you may be able
  to add a file at **Personal** or **Project** scope yourself. Publishing a file to
  the **Global** library is governed: it needs a **file-library curator's**
  approval before anyone can rely on it.
- **Materials** — adding, editing and retiring materials (and managing material
  classes) is a **materials curator's** job; everyone else browses and uses them.

If you hold one of those responsibilities, see *If you have extra responsibilities
(capabilities)* for what the curator controls add.
