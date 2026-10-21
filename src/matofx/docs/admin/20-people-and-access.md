---
title: People and access
audience: admin
capability: null
slug: people-and-access
summary: How accounts, roles, and capabilities work; creating and editing users; approvers; granting capabilities; teams and groups; permissions.
---

# People and access

This chapter covers how you give people access to MATOFX SPDM™ and control what they can
do.


> **On a floating-access licence**, an active account uses one of your licence
> places. Creating or re-enabling a user is refused once they're all assigned;
> deactivating one frees a place immediately. The Users page shows the count.
> See *Your licence*.


## How accounts and roles work, in plain English

Every person has an **account**. Each account has one **role** that sets its
general level of access:

- **Regular User** — uses the tools, runs jobs, sees their own and shared work.
- **Administrator** — does the day-to-day running of the site.
- **Super User** — the highest level; can do everything, including the most
  sensitive actions.

(Your site may also have custom roles your organisation has added.)

## Base roles vs capabilities

On top of the single role, you can grant **capabilities** — extra, specific rights
that don't require making someone an administrator. For example, you can let a
regular user look after the file library or the materials database. The role is
the foundation; capabilities are add-ons.

**Grant the least that does the job.** If someone needs to curate materials, give
them the *materials curator* capability — not an administrator account. A
capability unlocks exactly one area and nothing else, so it's the safe way to
delegate a responsibility without handing over the keys to the whole site. Reserve
**Administrator** for people who genuinely run the site, and **Super User** for the
one or two who need the most sensitive actions.

## Create vs curate: who can use a feature, who governs it

Several features split their permissions into two tiers:

- A **create** permission (a *base-role* right) lets someone *use* the feature —
  create an optimisation study or a surrogate model, build a Job Cohort, author a
  draft V&V record, or add a file or material.
- A **curate** capability (a stacked add-on) is the *governance* right — approving,
  promoting, retiring, assigning, versioning, or publishing globally.

A curator can always create too (curate implies create), so you don't need to grant
both. **Out of the box the create permissions are held only by Super Users and
Administrators** — give a base role (or a custom one) the relevant create permission
in the **Permissions** tab to open a feature up to those users. The exception is
**Parametric Studies**, whose create permission is granted to all base roles by
default, as before.

**Viewing vs creating.** If a feature is enabled for your site, its page still
appears in the sidebar for everyone — someone without the create permission just
gets a **read-only catalogue** (they can browse what exists, but the "New …" button
or tab is hidden). So the create permission controls who can *add*, not who can
*see*. The governed pages (File Library, Materials, V&V records) work the same way:
the list is visible, but the create and governance controls are hidden without the
matching rights.

**Finding a permission.** On the **Permissions** tab, a base role's permissions are
laid out in cards that mirror the sidebar groups (Dashboard, Studies, Libraries,
Governance, Admin), so a right is where you'd expect it.

## Creating a user

In **Admin → New/edit User**, create an account by giving the person a
username, name, email, and starting password, and choosing their role. They can
change their own password after first sign-in.

## Editing a user and resetting a password

From the same page you can edit someone's details, change their role, or **reset
their password** if they're locked out.

## Making someone an approver

Tick **Can approve runs** on New User or Edit User to make someone available as an
approver. The tick on its own doesn't send them anything — it makes them selectable
in a tool's approval settings, and only users with the tick appear there. Once
you've named them on a tool, they sign off that tool's **jobs**, its **parametric
studies**, and the **results release** of optimisations run against it. While a
run's results are held for release, they can also see those results.

The tick doesn't cover the curator sign-offs (file library, materials, V&V,
surrogate models) or the start of an optimisation campaign — those come from
capabilities, below. (See the *Approvals* chapter.)

## Granting capabilities

When you edit a user, you can grant **capabilities** — for example *file library
curator*, *materials curator*, *Verification and Validation (V&V) curator*, or
*cohort curator* (lets someone build and manage Job Cohorts; viewing cohorts is
open to everyone). Each capability adds the matching page to that person's menu and
lets them look after that shared area without full admin rights.

## Teams and groups

In **Admin → Teams, roles & permissions**, the **Teams & Access Rights Groups** tab
shows three side-by-side sections:

- **Teams** — named groups of people used to scope visibility. Tools and
  administrator-released Workflows can be restricted so only their team members can
  see them. A person can be in any number of teams.
- **Access Rights Groups - Base Roles** — each person's single privilege level
  (Super User, Administrator, Beta Tester, Regular User, plus any custom base roles).
  A person is always in exactly one base role.
- **Access Rights Groups - Capabilities** — curator add-ons that stack on top of the
  base role. A person can hold any number of capabilities.

Each team or group is its own expandable card; open it to see its members.

**Capabilities are a fixed set.** A capability corresponds to a built-in MATOFX
area (the file library, materials, V&V, cohorts, and so on), so you cannot create
or delete capabilities — only teams and base roles have a **New** button. What you
*can* do with a capability is add and remove members and tune its permissions on the
**Permissions** tab.

**Searching.** The search bar at the top of the tab filters all three sections as
you type. Enter part of a first name, last name, or username and the sections
collapse to just the groups that person belongs to, showing only the matching
people — a quick way to see everything one user is a member of at a glance.

**Sorting.** Use a section's **Sort members** box to order members by first name,
last name, or username. Sorting keeps your open cards open — it just re-orders the
people inside them.

**Adding several people at once.** Every team and group has an **Add members** box.
Pick as many people as you like, then add them all with a single **Add**. This works
for teams, base roles, and capabilities alike, and the box is always available —
even when everyone is already a member.

- For a **team** or a **capability**, the people you add simply join it (anyone
  already in it is skipped).
- For a **base role**, adding people *moves* them to that role: because everyone has
  exactly one base role, they're automatically removed from their previous one. You
  can also change a person's base role on their own edit page — both routes do the
  same thing.

**Removing someone.** Remove a person from a **team** or a **capability** and they
simply leave it. A **base role** is different: because a person must always have a
base role, removing them from one automatically moves them to **Regular User** rather
than leaving them with none. (You can't remove people from Regular User itself —
move them by adding them to a different base role.)

**Groups are permanent.** Teams and access-rights groups are never deleted, even
when they have no members left — they're kept so the audit trail can always resolve
a past membership back to a named group. There is no delete button. If a group is no
longer needed, empty it of members and leave it in place.

**Fixing a name.** Made a typo when you created a team or a custom access-rights
group? Expand it and click **Edit** to change its name or description. Renaming is
safe — it doesn't disturb who's in the team or group. The four built-in roles
(Regular User, Beta Tester, Administrator, Super User) keep fixed names, but you can
still edit their descriptions and permissions.

## Permissions: who can do what

The **Permissions** tab controls the fine-grained settings for who is allowed to do
what. Choose the group you want to edit from the picker, then tick the rights it
should hold. The editor shows only the rights that apply to that group's kind — base
roles show the base-role permissions, capabilities show the curator permissions.

Every group's permissions can be tuned except the **Super User** group, whose set is
fixed (it always holds everything). Most sites can rely on the built-in roles and
capabilities; permissions let you adjust the details where you need to.

## Deactivating or removing a user

When someone leaves, you can **deactivate** their account so they can no longer
sign in, while keeping their past work and its history intact.
