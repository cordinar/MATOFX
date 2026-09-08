---
title: The file library
audience: admin
capability: null
slug: file-library
summary: Curating reusable, governed tool-input files — uploading, assigning them to a tool's input field, and browsing the catalogue.
---

# The file library

The **file library** holds reusable, governed tool-input files — geometry,
meshes, boundary conditions and the like — with an immutable, versioned history.
Once a file is in the library, users can pick it straight from a tool's file
field instead of uploading their own copy, so everyone works from the same
trusted inputs. The workspace lives at **Libraries → File library** and is
available to people who hold the **library-curator** capability (and when the
feature is switched on for your site).

## Adding a file

Select **New library file** and fill in the form:

- **Name**, **Description**, **Provenance** (where the file came from) and
  optional **Kind** and **Tags**.
- **Scope** — *Personal* (only you), *Project* (members of a chosen project), or
  *Global* (everyone; curator-reviewed before it publishes).
- **Assign to tool** — the tool that may use this file.
- **Field** — once you pick a tool, this becomes a drop-down of that tool's
  **file inputs**, each shown as **"Label (name)"**. Choose the specific field
  the file is for, or leave it on **"any compatible file field"** to let the file
  be offered to every file field on that tool whose accepted types it matches.
- Upload the file's first version, then **Create**.

## Browsing the catalogue

Each file is shown as a **card** with its name, kind, tags, scope, state, how
many versions it has, and **the tool(s) and field it's assigned to — always
shown as labels**, never the internal aliases or field names.

Above the cards you can narrow the list:

- A **search box** matches a file's name, description, tags and the labels of the
  tool(s)/field(s) it's assigned to.
- **Scope**, **Tool** and **Include retired** filters.
- A **Group by** control — *No grouping* (the default), *by scope*, or *by tool*.

The search and the filters are **cumulative** — a file must match all of them to
appear. Under *Group by → tool*, a file assigned to several tools appears under
each of those tools, and files with no assignment fall under **Unassigned**.

## A file's details

Selecting a card opens the file, where you can add new versions (download any of
them), edit the details, change the scope (the governed transition, with a
reviewer picker for a global publish), and **assign or remove tools**. Tool
assignments here use the same **"Label (name)"** field picker, and are listed by
their tool and field **labels**. **Retiring** a file (the *Retire* button, shown
with an archive icon rather than a wastebasket, because nothing is deleted) keeps
its history but stops it being offered in pickers and locks it against edits.

Retiring is fully reversible: switch on the **Include retired** filter to find a
retired file, open it, and use **Restore** to make it active again — back in the
pickers and open to edits. Neither retire nor restore ever deletes anything.
