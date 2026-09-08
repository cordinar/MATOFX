---
title: The materials database
audience: admin
capability: null
slug: materials-database
summary: Curating material classes and material cards — defining a class's properties, adding materials, and browsing the catalogue.
---

# The materials database

The **materials database** holds reusable, curator-governed **material cards** —
each a named material (a grade, a fluid, an alloy…) with a versioned, validated
set of **properties**. Once a material is in the database, users can pick it
straight from a tool's material field. The workspace lives at **Libraries →
Materials** and is available to people who hold the **materials-curator**
capability (when the feature is switched on for your site).

## Material classes

A **class** describes the **expected properties** a material of that kind should
carry — for example a *Fluid* class might expect *thermal conductivity* and
*density*, each with a unit. Materials are validated against their class, so
required properties can't be left out.

### Creating a class and its properties — all in one place

Select **Manage classes**. At the top of the box you create a class and add its
properties together, without leaving to find the class afterwards:

1. Enter the **New class name** and an optional **Description**.
2. Fill in the first property: **Display name**, **Identifier** (auto-filled in
   snake_case from the display name — immutable once added), **Unit**, and
   whether it's **Required**.
3. Select **Add property**. This **saves the property — and creates the class on
   the first one**. Keep adding properties the same way; each is saved as you go.
4. When you're finished, select **Close** to return to the Materials catalogue.

If you want a class with no properties yet, use **Create class (no properties)**.
**Start another class** clears the form so you can create the next one.

Below the create form, every class is listed as an **expandable card**. Expand a
class to **edit or delete** its properties, add more, or **retire** the class.
Editing a property is *forward-only*: existing material versions keep the
property names and units they were saved with (their versions are immutable), so
a rename or removal only affects new versions.

## Adding a material

Select **New material**. Required fields are marked with an asterisk (\*):
**Name**, **Class**, and **Provenance** (its source). Pick the class and its
expected properties appear as value boxes — required ones are marked with an
asterisk too. Fill in what you have and select **Create**.

## Browsing the catalogue

Each material is shown as a **card** with its name, class, version count, aliases
and tags. Above the cards you can narrow the list:

- A **search box** matches a material's name, aliases, tags, class and
  description.
- A **Class** filter and an **Include retired** toggle.
- A **Group by** control — *No grouping* (the default) or *by class*.

The search and the filters are **cumulative** — a material must match all of them
to appear.

## A material's details

Selecting a card opens that material's own page, showing its **class**,
description, aliases, tags and confidentiality, and its full **version history**.
Each version lists the **properties** recorded on it (or notes when none were);
you can add a new version, edit the card details, or retire the material.
Retiring keeps the history but stops the material being offered in pickers — it's
a state change, never a delete.
