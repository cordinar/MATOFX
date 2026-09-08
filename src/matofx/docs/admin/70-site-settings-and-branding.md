---
title: Site settings and branding
audience: admin
capability: null
slug: site-settings-and-branding
summary: The General Settings page; turning features on and off; branding; default timezone; the maintenance banner; the support contact; sign-out timing; automatic cleanup of old jobs and files; approval-request emails; AI-assisted result summaries; and conveniences like favourites.
---

# Site settings and branding

**Admin → General settings** is where you shape how your MATOFX SPDM™ site looks
and behaves.

## The General Settings page explained

General Settings is a single page of site-wide options, grouped into sections.
Changes here apply to everyone, and most take effect without restarting the
software.

## Turning features on or off (feature switches)

Several larger features are **switched on or off** per site — for example
parametric studies, optimisation, surrogate models, the file library, the
materials database, Verification and Validation records, and the results
warehouse. When a feature is off, it's completely absent from users' menus. Turn
on only the ones your site uses.

## Branding: application name, header logo, and browser tab icon

You can set your site's **name**, upload a **header logo**, and upload the small
**icon shown on the browser tab** (the favicon). This makes the site look like
your organisation's own. The page shows a preview of the current header logo and
favicon — the uploaded image when you've set one, otherwise the bundled default.

The **login screen** shows a **shipped default logo** so there's always a
recognisable image before anyone signs in — it is deliberately **separate** from
the header logo you upload here, which applies to the signed-in application. To
change the login-screen logo, replace the packaged logo file in the deployment
(a one-off install step); it isn't set from this page.

## Default timezone

Set the **default timezone** (an IANA name such as `Europe/London` or
`America/New_York`) and the application shows dates and times in that zone for
everyone who hasn't picked their own. Individual users can override it on their
**Account page**; that personal choice wins for them. Times are stored internally
as UTC and converted for display. Leave it at UTC if unsure; an unrecognised name
falls back to UTC.

## Housekeeping: automatic cleanup of old jobs and files

Under *Job housekeeping* you set how long things are kept before an automatic
background cleanup removes them — independently for **unsubmitted draft jobs**,
**job logs**, **completed-job working directories**, and **failed-job working
directories**. Set any of these to **0 to keep that kind of data forever** (never
delete). The cleanup runs periodically on the job server, so changes take effect
on its next pass.

There is deliberately **no** setting to delete audit-log entries: the audit log is
tamper-evident (a signed hash chain), so it is always kept in full.

## The maintenance banner

You can post a **maintenance banner** — a notice that appears across the top of
every page for all users, with an optional time. Use it to warn people of planned
downtime. Clear the text to remove it.

## The support contact line

Set a **support contact** (an email address) and it appears at the foot of every
page, so users always know who to ask for help.

## Inactivity / sign-out timing

You control how long a user can be **inactive** before MATOFX signs them out
automatically. Shorter is more secure; longer is more convenient. Pick what suits
your site.

Inactivity is measured from the user's last real interaction with the page —
mouse, keyboard, scroll or touch — not from the last background request, so a
page that refreshes itself doesn't keep a walked-away session alive. When the
limit is reached the user's session is revoked and their browser is taken to the
sign-in page with a note explaining why; signing back in returns them to the page
they were on. A user whose session had already expired (a tab left closed
overnight, say) is sent to sign in as soon as they open MATOFX again, rather than
being shown a page whose every action fails.

A change to this setting applies from each user's next request — no restart
needed — but a browser already open uses the value it was given at sign-in until
that user signs in again.

On a concurrent-session licence, signing out is also what returns a session slot,
so a shorter inactivity limit means fewer slots tied up by people who have walked
away. See *Licence compliance reports* for how session use is reported.

## Approval-request emails

When work is submitted for approval, MATOFX can email the person whose decision it
needs — a job/study/optimisation approver, or a library / materials / surrogate-model
/ V&V curator. Under *Approval notifications* a **master switch** turns all of these
on or off, and a **per-type switch** lets you silence one kind (for example, email
for material approvals but not library-file approvals). These are independent of the
job-state emails (Running / Complete / Failed). Approvers always see pending items in
their in-app Approvals inbox regardless, so nothing is missed when email is off. See
*Approvals* for the full picture.

## AI-assisted result summaries

Every finished job and study already shows a short **plain-language summary** at
the top of its results — generated on the box from the run's own recorded
figures, with no network involved. That baseline is always on.

If you want those summaries written more fluently, you can connect an **AI
(LLM) service** — your own internal gateway, Azure OpenAI, AWS Bedrock, or a
local Ollama. This takes **two deliberate steps**, by design, because it means a
result's figures are sent to that service:

1. **Wire the endpoint** in the `[llm]` section of the configuration file (the
   provider, address, model, and any key). This needs config-file access and a
   restart — it can't be done from the web UI.
2. **Switch on** the **AI-enriched result summaries** setting under *Data
   governance* on this page (Super Users only). Until both are done, every
   summary stays the plain version and nothing leaves the box.

When it's on, the AI only rephrases the **same audited figures** — MATOFX checks
the result and discards anything that introduces a number the run didn't produce,
falling back to the plain summary. Each AI call is recorded in the audit log.
Individual users can still opt out on their own Account page.

## Favourites and other conveniences

You can switch on conveniences such as **favourites**, which lets users pin the
tools and workflows they use most so they float to the top of their lists.
