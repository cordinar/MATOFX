---
title: Licence compliance reports
audience: admin
capability: null
slug: licence-compliance
summary: What a compliance report contains, how to generate one for your software supplier, and what it deliberately leaves out.
---

# Licence compliance reports

Your software supplier may ask you, from time to time, to send a **compliance
report**. It is a small file that shows your licence usage over a period and
demonstrates that MATOFX SPDM™'s tamper-evident records have not been altered.

Nothing is ever sent automatically. MATOFX makes no outbound connections of any
kind — no licence server, no phone-home, no telemetry. A report exists only when
an administrator generates one, and it goes to your supplier by whatever route
you already use to receive releases.

## What is in a report — and what is not

A report contains:

- **Which install it is** — an identifier, your organisation name, the
  environment, and the MATOFX version.
- **Your licence** — its identifier, type, number of places, and expiry. (Before
  a licence is installed the report says so, and reports usage as observation
  only — see *Your licence*.)
- **Usage figures for the period** — peak simultaneous sessions (or, on a
  floating-access licence, how many access assignments were held), how many
  sign-ins were refused because the licence was full, and how many times a Super
  User was let in over the limit.
- **Integrity evidence** — the result of MATOFX's own check of its audit
  records, plus fingerprints proving the record history is the same one your
  previous report described.
- **A short list of licence events** — a licence being installed, an expiry
  warning, a Super User admitted over the limit — each with the time and who did
  it.

It does **not** contain any of your engineering data. No project or scenario
names, no job names, no tool inputs or results, no files, no email addresses. The
only people named are the administrators who performed the licence events listed
above.

## Reading it before you send it

Every report is generated as a pair: a `.json` file (the one to send) and a
`.html` file with exactly the same content laid out as a readable page. Open the
`.html` in any browser — it needs no internet connection — and you can see the
whole report before deciding to send anything.

Nothing about generating a report sends it anywhere. Reading one you decide not
to send is a perfectly normal thing to do; see *Generating a report you do not
send* below.

## Generating a report

Go to **Admin → Licence & compliance**. The page lists every report this
install has produced, newest first, and has a **Generate report** button.

A report covers the period since your last one. To cover a specific window
instead, put a date in the optional box beside the button before pressing it.

Generating takes a moment — MATOFX checks every audit record as part of it —
and the new report then appears at the top of the list with two buttons:

- **Read (HTML)** — the readable twin. Open this first.
- **Send (JSON)** — the signed file to send to your supplier.

Both are downloaded from the copy MATOFX stored when the report was made, so
what you read is exactly what you send.

You need the **audit log** permission to see the page, which administrators
have by default.

## The reminder to produce one

It is easy to forget a task that comes round once a quarter, so MATOFX
reminds you. When a report falls due, an **amber bar appears at the top of
every page** with a **Generate report** button on it.

A few things worth knowing:

- **Nothing stops working.** The bar is a reminder and nothing more. If you
  ignore it, the site keeps running exactly as before — no read-only mode, no
  features switched off, no warnings to your users. It is a different kind of
  message from the red *read-only* bar, which does mean something is wrong.
- **Only people who can act on it see it.** The bar is shown to users with the
  **audit log** permission — the same people who can open the page it points
  at. Everyone else sees nothing.
- **It clears for everyone as soon as a report is generated.** There is no
  "dismiss" button and no per-person setting: the moment anybody produces a
  report, the bar goes for all of you on your next page. Two administrators can
  never see different answers.
- **The page tells you when the next one is due**, just under the install id,
  in the same words as the bar.

The clock runs from the last report you **generated** — whether you then sent it
makes no difference, since MATOFX has no way of knowing. Before your first
report it runs from the day this install was set up, so a site that has never
reported does eventually say so.

### How often

The interval is part of your licence rather than a setting on this site, so it
matches what was agreed with your supplier. It is normally **three months**. If
you would like it changed — or switched off entirely — ask your supplier to
re-issue the licence; there is nothing to configure here.

### From the server instead

The same report can be produced on the server, which is useful for scripting
or when nobody can sign in:

```
matofx compliance report
```

This writes the two files into the current folder, named after your install
and the date. Add `--since 2026-08-01` to cover a specific window. Reports
made this way appear on the page as well — there is one numbered sequence,
not two.

## Why the reports link together

Each report carries a **report number** that only ever counts upwards, and a
fingerprint of your record history at the moment it was taken. Your supplier
keeps these. The next report has to be consistent with the last one, which is
what makes the evidence meaningful — and it is also why **you should keep
generating reports even during a quiet period**. A missing report number is
visible; nothing else about it is.

If a report is ever regenerated for the same period, the numbering makes that
plain too. This is normal and not a problem — say so when you send it.

### Generating a report you do not send

This is fine, and it will not cause you any trouble later. You can produce a
report to read it, to check a figure before a renewal conversation, or simply to
see what one looks like, and then never send it. Each report proves it follows on
from **every** earlier report, so your supplier can check it against whichever
one they last received — not merely the one immediately before it.

The only visible trace is a gap in the report numbers, which your supplier reads
as a question, not a problem. If they ask, "we generated that one to look at it"
is a complete answer.

## If your records fail their integrity check

MATOFX checks that its audit records have not been altered — once when the
server starts, and every hour after that. If that check ever fails — most often
after a restore from backup, or a disk fault — MATOFX moves the site into
**read-only mode** and shows a banner to administrators. Existing work is safe and everything can still be read; new work
is paused until a Super User acknowledges the problem.

The compliance report still generates in this state, and says the check failed,
listing which records are affected. That is deliberate: refusing to produce a
report at exactly the moment something looks wrong would destroy the evidence
needed to explain it. Send the report, and contact your supplier — a genuine
hardware fault looks quite different from tampering, and the record detail is
what tells them apart.

### Acknowledging a failure

A **Super User** — and only a Super User — can lift read-only mode by
acknowledging the failure from the banner at the top of any page. You are asked
to write down what happened, and that explanation is required: it goes into the
audit record and appears in the next compliance report next to the failure
itself. "Restored from the 03:00 backup after the disk failure on node 2" is
useful six months later; "ok" is not.

Acknowledging does **not** erase anything. The failure stays in the record
permanently, the site simply becomes writable again. And if the check fails again
afterwards, the site returns to read-only — acknowledging one problem never
absolves the next one.

## If the check stops running

The hourly check is done by the MATOFX **runner** — the same background service
that runs your jobs. If the runner stops, nothing is checking your records any
more, and MATOFX will not quietly carry on as though everything were fine.

- After **3 hours** without a check, administrators see a banner saying so.
- After **24 hours**, the site becomes read-only, exactly as it would for a
  failed check.

Almost always the fix is simply to start the runner again; the site recovers by
itself at the next check, with nothing to clear. If you need to keep working
while you sort it out, a Super User can acknowledge the banner in the same way as
a failed check.

This is deliberate. A report that says "records verified" is only worth something
if the verifying is actually happening, so MATOFX treats "we stopped checking"
as a state worth telling you about rather than one to leave unnoticed. Your
compliance reports say when the last check ran, so your supplier sees it too.

## Read-only mode

Whatever puts the site into read-only mode — an expired licence past its grace
period, or a failed integrity check — the rules are the same:

- Everyone who already has an account can sign in and **view results**.
- No new jobs, studies or optimisations can be submitted, and nothing can be
  edited.
- No new user accounts can be registered, and access rights cannot be changed.
- Jobs already running finish normally.

A few things deliberately keep working, because blocking them would leave you
stuck: signing in and out, installing a licence, signing someone out to free a
licence place, generating a compliance report, and personal display settings
such as light/dark mode. Everything else answers with a message explaining that
the site is read-only and why.

A banner at the top of every page tells everyone — not just administrators —
that the site is read-only and which of the two causes it is.

See *Your licence* for installing and renewing the licence itself, and *Site
settings and branding* for the inactivity setting that governs how quickly a
concurrent-session place is released.
