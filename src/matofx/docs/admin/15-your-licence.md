---
title: Your licence
audience: admin
capability: null
slug: your-licence
summary: Installing and renewing the licence file, the two licence types, what happens as it approaches expiry, and what read-only mode means.
---

# Your licence

MATOFX SPDM™ runs on a **licence file** your supplier issues: a single signed
file you place on the server. There is no licence server, no activation, and no
outbound connection of any kind — the file is checked on the machine, offline.

## The two licence types

Your licence is one of two kinds. Both allow **unlimited registered accounts**;
they differ in what actually consumes a place.

| | **Concurrent Session Licence (CSL)** | **Floating Access Licence (FAL)** |
|---|---|---|
| A place is used up… | when someone **signs in** | when an administrator **grants** a person access |
| …and released | when they sign out, or are signed out for inactivity | when an administrator **revokes** that access |
| Who can sign in | anyone, until all places are in use | only the people who hold a place |
| Feels like | a shared pool, changing minute to minute | named places, changing only when you decide |

A CSL suits a large team who each use MATOFX occasionally. A FAL suits a smaller,
settled group of regular users. Your licence file says which one you have — see
**Reference and glossary** if you're unsure what yours means in practice.

## Getting a licence issued

Every licence is issued for **one specific MATOFX install**, so your supplier
needs to know which install before they can issue anything.

Run this on the server after installing MATOFX:

```
matofx licence request
```

It prints an install id like `inst_0V9H0FJ0RTTY`. Send that to your supplier.
The licence they issue names it, and MATOFX will only run on the install it was
issued for.

**Keep the id.** Every later licence — a renewal, a change of seat count, the
full licence after a trial — has to carry the same one. Running the command
again is safe and prints the same id.

You can request a licence before or after MATOFX is running; the id is created
the first time either happens and never changes after that.

### Why licences are tied to an install

It stops one licence quietly running two deployments. A second install generates
a different id of its own, so a copied licence file is refused there.

If you genuinely need a second deployment — a test system alongside production,
say — ask your supplier for a second licence. They can issue one for it.

## Installing a licence

Copy the file your supplier sent to the server, then run:

```
matofx licence install /path/to/matofx-licence.json
```

The command **checks the file before it installs it** — the signature, the dates,
the install id, and (if your licence is tied to a particular machine) that it
belongs to this one. If anything is wrong it refuses and changes nothing, so a
bad file can never displace a working licence. On success it prints the licence
type, the number of places, and the expiry date.

The refusal you are most likely to see is a licence meant for a different
install:

```
matofx: This licence was issued for install 'inst_...', but this is install
'inst_...'. Run 'matofx licence request' and ask your supplier to issue a
licence for this install.
```

That usually means the file was sent for another server, or the install id
changed because MATOFX was reinstalled against an empty database. Send your
supplier the id the message names as *this* install and ask for a replacement —
nothing on the server needs undoing first.

To see the current state at any time:

```
matofx licence show
```

## Renewing, and changing your licence mid-year

A renewal is the same operation: install the new file over the old one. Nothing
else needs to change and there is no downtime. Both the old and new licence are
recorded in the audit log, so the history of what was installed and when is
always available.

The same is true of a licence issued **during** the year — to add places, or to
switch on a feature you have since bought. Ask your supplier for a new licence
naming the same install, and install it the usual way.

**The change takes effect immediately, without restarting MATOFX.** That covers
both halves of a licence:

- **Places** — a licence with more (or fewer) places applies from the next sign-in.
- **Features** — a newly licensed feature appears for users on their next page
  load, and one that is no longer licensed disappears just as quickly.

You do not need to plan an outage to take delivery of extra capacity.

> Anyone already signed in keeps their session. If a renewal *reduces* your
> places below the number currently in use, nobody is thrown out — the site is
> simply over its cap until enough people sign out. See *When all the places are
> in use*.

If you have replaced the file by hand rather than through the command, run
`matofx licence reload` to have MATOFX record and re-check it. The running site
picks the file up either way; `reload` is what writes the outcome to the audit
log.

### Machine-bound licences and containers

Most licences are tied only to your **install**, which lives in the database and
survives anything. A licence can optionally also be tied to the **machine**, and
that one behaves differently: it compares an operating-system machine id, and
**rebuilding a container produces a new one**.

So on a containerised install a machine-bound licence works perfectly until the
first redeploy and then refuses to start — during a deployment, which is the
worst possible moment to find out. MATOFX warns you about this when you install
such a licence on something that looks like a container.

If you run MATOFX in containers, ask your supplier for a licence **without**
machine binding. You lose nothing: it is still tied to your install, so it still
cannot be used to run a second deployment.

### Moving to a different server

A licence is issued for one **install**, not one machine, and the install id
lives in the database. So moving MATOFX to new hardware while bringing the
database with it — a lift-and-shift, a new VM, a rebuilt container — keeps the
same id, and your existing licence keeps working untouched.

What needs a new licence is a MATOFX installed against an **empty** database,
because that mints a new install id. If you are rebuilding rather than moving,
run `matofx licence request` on the new install and send your supplier the id it
prints. Tell them it is a rebuild: your report numbering restarts, and they need
to know that is expected rather than a sign of something wrong.

## As expiry approaches

Licences do not stop working the moment they expire. There is a **grace period**
(usually 30 days) written into the licence itself:

1. **Active** — everything works normally.
2. **Grace** — the licence has expired but the grace period is running.
   Everything still works, and administrators see a banner counting down the days
   remaining.
3. **After grace** — the site becomes **read-only**.

At no point does MATOFX simply stop. A missed renewal reminder must never take
your simulation work offline, so the worst case is read-only access to everything
you already have.

## Read-only mode

Whether it is reached through expiry or a failed integrity check (see *Licence
compliance reports*), read-only mode behaves the same way:

- Everyone who already has an account can **sign in and view results**.
- Nothing new can be submitted — no jobs, studies or optimisations — and nothing
  can be edited.
- **No new accounts** can be registered, and access rights cannot be changed.
- Work already running finishes normally.

Installing a valid licence lifts it immediately.

## What your licence switches on

As well as the number of places, the licence says which of the larger features
this site has — parametric studies, optimisation, the file library, and so on.
They are listed under **Feature switches** in *Reference and glossary*, and a
feature that isn't licensed is absent everywhere rather than visible-but-blocked.

Two deliberate safety rules, so a licence problem never looks like a product
fault:

- If **no licence is installed**, the features listed in the configuration file
  are used. A lapsed renewal already costs you read-only mode; it does not also
  strip your features.
- If your licence **doesn't mention features at all**, the same applies — an
  absent list means "this licence has no opinion", not "nothing is licensed".

A licence naming a feature your version of MATOFX doesn't have yet is fine: the
unknown entry is ignored, and it starts working when you upgrade.

The licence also carries **how often you are reminded to produce a compliance
report** — normally every three months, and it can be switched off. That
reminder is an amber bar and nothing else; it never blocks anyone's work. See
*Licence compliance reports* for what the bar does and who sees it.

To see what is actually switched on, run `matofx licence show`. It lists the
features **this site is really running with**, not simply what the licence file
says — and where the two differ it tells you why, whether that is the
configuration file supplying the list or an entry being ignored because this
version doesn't have it yet. That distinction is the usual answer to "why can't I
see this feature?".

## When all the places are in use

On a **concurrent-session** licence, someone trying to sign in when every place
is taken is turned away with a message telling them so — their password was
fine, and nothing is wrong with their account. They can wait for someone to
sign out, or ask you.

If several people sign in at the same instant with only one place left, exactly
one of them gets it and the others see the same "all places are in use" message.
Nobody is ever admitted over your limit by a coincidence of timing — the only
way past the limit is the deliberate Super User route below.

**Super Users are always let in**, even when the licence is full. This is
deliberate: the alternative is an administrator locked out of their own site
exactly when the licence needs attention. When it happens:

- the Super User sees a banner saying they were admitted over the limit, with a
  link to **Active sessions**;
- the event is recorded, and appears in the next compliance report as a
  break-glass admission — so a peak above your licence limit is explained
  rather than looking like a breach.

### Freeing a place

**Admin → Active sessions** lists everyone signed in, longest-idle first, with
how many places are in use against your limit. **Sign out** on any row ends that
session immediately and frees the place. You can't sign yourself out from here —
use the normal sign-out for that.

Places also free themselves: a session ends when the person signs out, when they
have been inactive for the period set in *Site settings and branding*, or when
their session reaches its maximum age. Shortening the inactivity period is the
usual way to make a busy licence go further.

Sessions admitted **over** the limit — see below — are marked *over the limit*
on this page, so you can see at a glance what is putting the site over.

### Being let in over the limit

If every place is in use and a **Super User** signs in, MATOFX lets them in
anyway rather than lock an administrator out of a site that may need its licence
fixing. This is deliberate, and there is no limit on how often it happens: a
limit would lock out exactly the person it exists to let in.

Two things keep it honest instead:

- **The session ends within 12 hours**, whatever your usual session length. It
  is emergency access to fix something, not an extra place.
- **Administrators are emailed** when it happens, and it appears in your next
  compliance report with the over-limit count shown separately — so a peak of 11
  against 10 places reads as *"one break-glass admission"* rather than looking
  like a breach.

## Alerts about your licence

MATOFX emails administrators when something happens to your licence that you
would otherwise only find out about much later:

- a Super User admitted over the limit,
- a sign-in refused because every place was in use,
- your licence entering its grace period, or expiring.

These go to everyone who can read the audit log, which administrators can by
default. They are sent when the situation **changes**, not repeatedly — you will
not get an hourly reminder that a licence is still in its grace period.

You can turn them off with **Send licence alerts to administrators** on *Admin →
General settings*. That only stops the email: every one of these events is still
recorded in the audit log and still appears in your compliance reports.

## When all the assignments are taken

On a **floating-access** licence, a place is used by an *account that can sign
in*. Deactivated accounts keep everything — their projects, their settings,
their history — but don't hold a place and can't sign in.

The metered moment is therefore an administrative one: **creating a user**, or
**re-enabling** a deactivated one. If every place is assigned, MATOFX refuses
and tells you so — nothing is created, and nothing is half-changed. **Removing**
access is never blocked, so you can always get back within your licence.

The **Users** page shows how many places are assigned and how many are left, so
you can see where you stand before filling in a new-user form.

To free a place, deactivate an account that no longer needs access
(**Admin → New/edit User → Edit**). The person keeps all their work; they simply
can't sign in until you re-enable them.

## How often the licence is checked

- **When MATOFX starts.**
- **Every hour**, by the job runner, while it's running.

Each check is recorded in the audit log, which is where the figures in a
compliance report come from. That means a renewal you drop into place is picked
up within the hour without a restart — and equally, a licence that stops being
valid is noticed on its own, without waiting for someone to try something.

## If a licence is refused

Five problems are treated as serious rather than as expiry, and MATOFX will
**not run** on them:

- **The file has been altered since it was signed.** Even a single changed
  character — a different number of places, a later expiry date — invalidates the
  signature.
- **It was signed by a key MATOFX doesn't recognise.** Usually this means the
  file didn't come from your supplier; occasionally it means your MATOFX build
  predates their current signing key, which upgrading fixes.
- **It was issued for a different machine.** If your licence is machine-bound and
  you have moved MATOFX to a new server, ask your supplier to re-issue it. See
  *Machine-bound licences and containers* below before asking for one.

- **It carries no signature at all.** A released copy of MATOFX always requires
  one, whatever the `environment` setting in your configuration file says.
- **It was issued for a different MATOFX install.** Every licence names the
  install it belongs to, so a licence cannot be moved to a second deployment.
  This is checked when MATOFX starts, not when you install the file.

In each case the command tells you which of the five it is. Contact your
supplier with that message — none of them can be fixed by editing the file, and
no configuration setting relaxes these checks.

### If the file becomes invalid while MATOFX is running

MATOFX re-reads the licence as it works, so it notices a file that has been
edited or replaced without waiting for a restart. When that happens the site
moves to **read-only mode** straight away, with a banner saying the licence file
is not valid. This is a different message from an expired licence, because it
needs a different response: restore the file your supplier sent, rather than ask
them for a renewal.

Nothing is lost, and no acknowledgement is needed. Put the correct licence back
and the site returns to normal by itself, again without a restart.

Read-only is used here rather than stopping outright because a site that has
been running all morning should not vanish because a file changed underneath it
— everything already produced stays readable and downloadable.
