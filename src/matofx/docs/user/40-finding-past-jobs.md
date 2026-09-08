---
title: Finding past jobs
audience: user
capability: null
slug: finding-past-jobs
summary: Searching earlier runs by tool and input values, and opening a result from the search.
---

# Finding past jobs

Over time you (and your colleagues) build up a lot of completed jobs. **Job
Search** helps you find a particular earlier run without scrolling through
everything.

## Why search past jobs

The **My Jobs** page is for getting around your current work. **Job Search** is
for discovery — answering questions like *"which runs used this material at this
temperature?"* across everything you're allowed to see.

## Searching by tool and input values

1. Open **Job Search** (there's a card for it on your home page, and it has its
   own page).
2. Choose the **tool** you're interested in.
3. Narrow down by the **input values** that matter to you.

MATOFX SPDM™ shows the matching completed runs.

A few things worth knowing:

- **Start broad, then narrow.** Pick the tool first and look at what comes back
  before adding input filters — it's easy to over-narrow and end up with nothing.
- **You see what you're allowed to see.** Search spans your colleagues' runs too,
  but only the ones your access lets you open — so a run you know exists may not
  appear if it belongs to work you're not on.
- **It only finds completed runs.** Jobs still queued, running or failed won't show
  up here; use **My Jobs** for those.
- **Numbers match within a tolerance.** Numeric filters don't need an exact hit:
  the **Numeric match tolerance** sets how far a stored value may differ from the
  number you type and still count as a match — at 25%, a filter of 100 matches any
  run between 75 and 125.

## Finding *similar* runs

Sometimes you don't have an exact filter in mind — you just want the runs most
like a particular one. Switch the mode to **Similar** and MATOFX ranks completed
runs by how close their inputs are to a reference, closest first, with a
**Closeness** score on each row.

You can set the reference two ways:

- **Typed values** — enter the input values you care about, just like the filter
  form. You don't have to fill in everything; leave the rest blank.
- **Existing run** — pick a completed run and find the ones most like it. That run
  is left out of its own results.

How "close" is measured:

- **Numbers** are compared on a normalised scale, so a big-number input (say a
  temperature) doesn't automatically drown out a small one (say a thickness) —
  what matters is how far off each is *relative to the runs you're searching*.
- **Text and choice inputs** (materials, options, files) must match exactly — they
  act as a filter on top of the ranking.
- **Weighting.** If a recent *study* of the tool is available, MATOFX weights the
  ranking by how much each input actually affects the results, so similarity
  focuses on the inputs that matter. Otherwise every input counts equally. The
  note under the results always tells you which was used.

Unlike the plain filter — where a run is either a match or not — Similar always
gives you the nearest runs, so you get something useful even when nothing matches
exactly.

## Opening a result from the search

Select any result in the list to open that job — from there you can read its
inputs, log, and output files just as you would for one of your own recent jobs.

## Reusing what you find

Finding an old run is often the start of new work, not the end of a search. Once
you've opened a past job you can use it as a reference point for the inputs of a
fresh run — a quick way to repeat a known-good setup with one value changed,
rather than filling in a form from a blank page.
