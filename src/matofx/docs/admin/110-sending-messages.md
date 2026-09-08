---
title: Sending messages to users
audience: admin
capability: null
slug: sending-messages
summary: An overview of email notifications, sending a broadcast notice, and setting the maintenance banner.
---

# Sending messages to users

MATOFX SPDM™ can reach your users in a couple of ways. This chapter covers the ones you
control. The quick rule for choosing between them:

| You want to… | Use |
|---|---|
| Tell everyone something they should keep (an announcement, a policy change) | A **broadcast** notice (email) |
| Warn people *right now*, on screen, about something temporary (downtime) | The **maintenance banner** |
| Let MATOFX tell a user about *their own* job automatically | **Job emails** (set up once, then automatic) |

## Email notifications overview

MATOFX sends **job emails** automatically — telling a user when their job starts,
finishes, or fails — for tools whose author enabled emails, and for users who've
kept job emails switched on. You set up email itself at install time (see
*Installing and setting up MATOFX*), and some email settings can be adjusted in
General Settings.

## Sending a broadcast notice

From **Admin → Send Notifications**, you can send a **broadcast** — a message
to your users — when you need to tell everyone something. Use it for announcements
that belong in people's inboxes. The page has two tabs: **compose** (who and what
to send) and **header & footer template** (the surround applied to every send).

### Choosing the audience

On the **Compose** tab, the **Audience** card has two parts:

**Recipients** — the base set of people:

- **Users with access currently enabled** — everyone except deactivated accounts.
- **All registered users** — includes deactivated accounts.

**Target a specific group (optional)** — narrows those recipients to a group:

- **Everyone in the selected recipients** — no narrowing (the default).
- **Specific teams** — only members of the teams you pick.
- **Specific access-rights groups** — only users who hold the roles you pick.
  Built-in roles are listed first in descending order of rights (Super User,
  Administrator, Beta Tester, Regular User), then any custom roles A–Z. Curator
  and approver roles aren't listed here — pick them under *Specific curators and
  approvers* instead.
- **Specific curators and approvers** — only the holders of the groups you pick
  (Library, Materials, V&V, Models, or Cohort curators, and Optimisation
  approvers).

When you choose a group option, a multiselect appears — pick one or more entries.
The two parts combine: choosing **Users with access currently enabled** together
with a team sends only to that team's currently-enabled members. MATOFX works out
the recipients for you and never sends the same person twice, even when your
selections overlap.

### Writing and sending

Add a **subject** and **message body**, optionally attach files (each is sent
with every email), then **Send broadcast**. You're asked to confirm — a broadcast
reaches real inboxes — and then shown an immediate summary of how many were sent
and any that failed. If email is turned off for the install, nothing is sent and
you're told so.

## Header & footer template

The **header & footer template** tab sets the surround wrapped around every
broadcast — a header image and header text shown above your message, and a footer
image and footer text shown below it. Set them once and forget.

For each of the header and footer:

- **Image** — upload one PNG / JPEG / GIF (uploading replaces the current one).
  It saves immediately. Use **Remove image** to clear it.
- **Text** — type the wording in the box. Click **Save** to persist it.
- **Preview** — shows exactly what recipients see for that block: the image and
  the text together, updating live as you type. When you remove the image, the
  preview updates to match — so the preview is always an honest picture of what
  is currently set.

## Setting the maintenance banner

For something that should be visible **on screen** rather than emailed — such as a
heads-up about planned downtime — set the **maintenance banner** in General
Settings. It appears across the top of every page until you clear it. (See *Site
settings and branding*.)
