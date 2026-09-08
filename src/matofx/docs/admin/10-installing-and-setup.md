---
title: Installing and setting up MATOFX SPDM™
audience: admin
capability: null
slug: installing-and-setup
summary: What you need, where MATOFX SPDM™ can run, how big a machine to choose, the two services, the configuration file, the first super user, choosing a database, file storage, email, secure access, backups, and updates.
---

# Installing and setting up MATOFX SPDM™

This chapter explains, in plain terms, how MATOFX SPDM™ is installed and set up. It
covers the **what** and **why** of each step. The exact commands for your
platform come with the installation bundle (the `INSTALL.md` notes shipped
alongside the software) and the one-click installer scripts; this chapter is the
map that makes those steps make sense.

## What you need before you start

- A computer to run MATOFX on (see the next section).
- **Python 3.12 or newer** installed on it.
- The MATOFX **installation bundle** for your version — a single folder built for
  release that contains the application, its pinned dependencies, a configuration
  template, and installer scripts.

## Where MATOFX SPDM™ can run

MATOFX runs on a single machine that you control:

- your own **server**, or
- a **Windows or Linux** computer, or
- a **cloud computer** (a virtual machine you rent).

This is what "**on-premises**" means — the software runs on a machine *you*
operate, not as a shared service run by someone else. All your data stays on that
machine.

## How big a machine do you need?

MATOFX itself is deliberately lightweight. What actually drives the size of the
machine is **where your simulation tools run**: if jobs are sent to your existing
compute servers or HPC cluster, the MATOFX machine stays small; if jobs run *on*
the MATOFX machine, size it for the tools themselves.

| | For a trial or small team | Recommended for production |
|---|---|---|
| **Processor** | 4 cores | 8–16 cores |
| **Memory** | 8 GB | 32 GB |
| **Graphics card** | Not needed | Not needed |
| **Disk** | 100 GB SSD | 500 GB – 2 TB SSD, with room to grow |

A few points worth knowing:

- **No graphics card is needed.** All 3D viewing happens in your users' web
  browsers, not on the server. You only need one if you install a tool that
  itself requires a graphics card *and* you run that tool on the MATOFX machine.
- **Memory matters most for surrogate models.** Fitting a surrogate over large
  simulation result fields holds the whole training set in memory at once — this
  is the main reason for the 32 GB recommendation. Installs that only use scalar
  results run comfortably in much less.
- **Disk fills up with job files, and grows steadily.** MATOFX keeps completed
  jobs rather than deleting them, so plan for storage to grow over time rather
  than trying to size it perfectly on day one. A fast disk (SSD) keeps the
  database responsive.

For the detailed reasoning behind these numbers — and guidance for larger or
unusual installations — see the **hardware sizing guide** that ships with the
installation bundle.

## Installing the application

Installation copies the bundle onto the machine, creates a dedicated Python
environment, and installs MATOFX into it from the pinned, checksum-verified
package list (so nothing can be quietly substituted). The bundle includes a
one-click installer for Windows and one for Linux that runs these steps for you.

## The two parts that run

MATOFX runs as **two background services**:

- **The web interface and job server** (`matofx-api`) — serves the pages your
  users sign in to, and the behind-the-scenes interface they talk to.
- **The job runner** (`matofx-runner`) — picks up submitted jobs and actually runs
  the tools.

Both must be running for the site to work. On a production machine you set them up
as **system services** so they start automatically and restart if they stop.

## How MATOFX SPDM™ decides when to start a job

When several jobs are waiting, the runner doesn't start all of them at once — it
checks each against the machine's capacity first. How a job is governed depends on
**where it runs**:

- **Jobs that run on the MATOFX machine itself ("local").** The runner only starts
  a local job when *both* of these hold: there's a free **job slot**, and the
  machine still has enough **spare CPU cores and memory** for what that tool
  declares it needs. So a generous job-slot limit can't by itself overload the box
  — a free slot isn't enough if the cores or memory aren't there.
- **Local jobs that need a graphics card (GPU).** These are limited separately, to
  **one at a time by default**, so two GPU jobs can't fight over the same card's
  memory. (A GPU job also uses a normal job slot while it runs.)
- **Jobs sent to an always-on server or an HPC cluster.** These are handed off to
  that system, which runs its own queue — so they don't use up the MATOFX
  machine's local slots or budget.

A super user can adjust all of these without restarting anything, from the
**runner settings** (the job-runner knobs alongside the heartbeat and cancel
timings):

| Setting | What it does | Default |
|---|---|---|
| **Local concurrency** | Most jobs to run at once on the MATOFX machine | 2 |
| **Local GPU concurrency** | Most graphics-card jobs to run at once | 1 |
| **Local CPU budget** | Total cores the runner may hand out to local jobs | Auto-detect the machine |
| **Local memory budget** | Total memory the runner may hand out to local jobs | Auto-detect the machine |
| **Local memory reserve** | Memory held back for the operating system and MATOFX itself when the memory budget is auto-detected | 2048 MB |

Leave the CPU and memory budgets at their **auto-detect** setting (shown as `0`)
and MATOFX reads the machine's own cores and memory; set an explicit number to cap
how much the runner may use — useful when the machine shares duty with other
software. If a tool asks for more cores or memory than the whole budget allows, the
job is **stopped with a clear message** instead of waiting forever for space that
can never appear — lower the tool's request, or raise the budget.

## Starting and stopping MATOFX SPDM™

For a quick trial, the bundle includes **start** and **stop** scripts that launch
both services together. For production, you register the two services with the
operating system (Windows Services, or systemd on Linux) so they run
unattended and survive a reboot.

## The first sign-in: creating the first super user

A fresh install has no accounts. You create the **first super user** with a single
setup command, giving it a username, email, and name; you're prompted for a
password. This first account is the highest level of access — use it to sign in,
then create everyday administrator and user accounts from inside the app.

## Choosing where data is stored

MATOFX keeps its records in a **database**. You choose one of two options at setup:

- **The simple built-in database (SQLite)** — a single file on the machine. Ideal
  for a single team or a trial; nothing extra to install.
- **A separate database server (PostgreSQL)** — a dedicated database service.
  Better for larger or busier installations.

You can run perfectly well on the built-in option and move to a database server
later if you outgrow it.

## The shared file area

Jobs need somewhere to do their work and store files. At setup you point MATOFX at
a **shared file area** (a folder, or a network share). This is where each job's
working files live. The *Where files live* chapter covers managing this once
you're running.

## Setting up email so MATOFX SPDM™ can send notifications

If you want MATOFX to email users when their jobs start, finish, or fail — and to
send broadcast notices — you provide your **email relay** details (the mail server,
the port, and any username and password). You can leave email switched off for a
mail-free install, and turn it on later. Some email settings can be adjusted live
from **General Settings** once you're running.

**One connection, many messages.** MATOFX keeps a single connection to your relay
open and reuses it, rather than signing in again for every email. This matters
because most relays — Google Workspace and Microsoft 365 among them — limit how
often an account may *sign in*, not how many messages it may send. A large study
can change job state hundreds of times in a few minutes, and signing in for each
notification is what trips that limit; when it does, the relay starts refusing
with a message like *"Too many login attempts"* or simply stops responding, and
no further email gets through until the block clears.

If you do see that in the logs, it is a relay-side rate limit rather than a
misconfiguration — wait for it to clear, and consider whether every job state
needs to email every user. The connection is reopened automatically if it goes
idle, if it is dropped, or if you change the relay details in General Settings,
so a settings change takes effect on the next email and needs no restart.

## Turning on secure web access (encrypted connections)

MATOFX itself serves plain web pages on the machine; for production you put a
**reverse proxy** (such as Nginx, Caddy, or IIS) in front of it to add
**HTTPS** — the padlock in the browser that encrypts traffic between your users and
the site. Your proxy holds the security certificate for your site's web address.

## Running with no internet access

MATOFX is built to run on an isolated network. **Once installed, it makes no
outbound connections of its own** — nothing phones home, and the browser never
fetches anything from the public internet to display a page. Every part of the
interface, including the interactive charts, the provenance and workflow
diagrams, and the 3-D model viewer, is served by your own MATOFX machine.

Two consequences worth knowing:

- **Downloaded charts and comparison reports work offline too.** They carry
  their own charting code inside the file, so they plot correctly when opened on
  a laptop with no network. This makes those files large (several megabytes even
  for a simple chart) — that is the deliberate trade for a file that opens
  anywhere.
- **A blank diagram or viewer is not a network problem.** Because nothing is
  fetched externally, a chart or diagram that fails to draw points at this
  MATOFX server or something between you and it, not at your firewall. The
  on-screen message says so. Report it rather than opening ports.

Features you switch on yourself are the exception, and each is off by default:
email notifications need your mail relay, remote execution needs your cluster,
and the optional AI summary feature needs whichever model service you point it
at. The **demonstration page** is also excluded — it deliberately shows content
from the internet and is expected to look broken on an isolated network.

Internet access **during installation** is a separate question: the installer can
either download the pinned dependencies or take them from an offline bundle
prepared in advance. See the installation notes shipped with your release.

## Backups and keeping data safe

Make sure your routine backups include:

- the **database** (the file, or your database-server backups),
- the **configuration file**,
- the **audit signing key** (a small security file generated at setup — keep an
  offline copy; without it, older audit records can't be verified). If you ever
  rotate the key, back up its **retired-keys folder** too (a `*.retired` folder
  beside the key file) — the integrity check needs the retired keys to verify
  records signed before the rotation. **Lock down the folder's permissions:**
  the key file *and* the `*.retired` folder hold live signing keys (rotation
  keeps the previous private key so old records stay verifiable), so restrict
  read access to the MATOFX service account only. On Windows this must be set
  through the folder's security (ACL) settings — the app can't enforce it for
  you — and the key directory should never sit on a broadly-readable share,
- the **shared file area** if you need to retain job files.

## Updating to a new version

To move to a newer version: stop the two services, install the new bundle over the
old one, apply any **database updates** that ship with it (a single command brings
the database up to date), then start the services again. Your data, configuration,
and audit key are kept across the update.

## Setup checklist

1. Install Python and the MATOFX bundle.
2. Run the installer to create the environment and install MATOFX.
3. Place and edit the **configuration file** (database, file areas, email).
4. Generate and back up the **audit signing key** (set its location in the
   configuration file, then run `matofx rotate signing-key --init`). The same
   command without `--init` rotates the key later while keeping older records
   verifiable. Restart the services after generating or rotating a key.
5. Initialise the **database**.
6. Create the **first super user**.
7. Start the two services (and register them as system services for production).
8. Put a **reverse proxy with HTTPS** in front for production.
9. Sign in, confirm the dashboard loads, and set your **branding** and **support
   contact**.
10. Confirm your **backups** cover the items listed above.
