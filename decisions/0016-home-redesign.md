# 0016. Home page + app-shell redesign

Date: 2026-06-22
Status: Accepted (design discussion with the maintainer, 2026-06-22). **Design only — no code yet.** Build-level spec: `designs/home-redesign.md`. Interactive reference mockup: `home_designs/home_v3.html`.

## Context

The v2 Home page (`ui/pages/home.py`) is a faithful port of the legacy 5-column
dashboard, simplified to three columns (Create/Approvals/Job-Search/Tutorials
rail · Job-Status + Projects tables · Workflow/Tool Quick-Access). The app shell
(`ui/pages/_chrome.py`) is a teal banner + a left drawer whose groups
(Dashboard / Admin Space / My Account / Help / Demo) have grown organically.

The maintainer wanted a deliberate redesign of both the landing page and the
shell: a clearer grouped navigation, role/feature-aware gating made obvious, a
global search, fewer but richer cards, and remembered per-user preferences. The
direction was explored as a series of HTML mockups (`home_designs/`), iterated
to a working prototype, and locked.

## Decision

Adopt the layout and interactions captured in `home_designs/home_v3.html` and
specified in `designs/home-redesign.md`. Key points:

- **Sidebar** with five groups — **Dashboard, Studies, Libraries, Admin,
  Governance** — and the agreed sub-items. Items are gated: **RU** items show
  for everyone; **SU** items (and the whole **Admin** group, which is ordered to
  the bottom of the rail) show only for super-user / administrator and carry a
  **padlock** marker (no colour change). Per-feature flags hide entries as today.
  The **Approvals** surface (nav entry + Home card + summary clause) is
  **approver-only**. Labels only, never aliases.
- **Sidebar behaviour:** pinnable; unpinned = an icon-only rail that expands on
  hover (overlay, no reflow); a drag handle resizes it (188–460px). Sidebar text
  is black in light mode, teal only on the active page.
- **Header:** greeting + a summary line ("N jobs active · M optimisations
  running · K parametric studies running · P approvals waiting" — each clause
  shown only when relevant); a **split global search** (query + `in:` scope) with
  live grouped, gated results; a light/dark toggle; a **Help** selector
  (Help Documentation / Tutorials). No notifications bell.
- **Action tiles:** Create, Studies, Job Search, My Account (Create / Studies /
  My Account have hover+click dropdowns). My Account → **My account settings** /
  Log out.
- **Cards:** **Quick Access** (favourite tools/workflows; Tools/Workflows/All
  filter pill; per-item **Tutorials** button when tutorials exist; **Launch**
  hover-reveals *In existing project* / *In new project*; no Manage) and
  **Approvals** (approver-only; **Review deep-links** to the item) — equal width.
- **My-Work tables:** My Projects (Project · **Owner** · **My role** ·
  # Scenarios), My Jobs (Job · **Project / Scenario** · State · When; no progress
  %), Parametric Studies (Study · **Project** · Runs · State · When), and
  Optimisations. **All columns left-aligned**, headers aligned exactly with their
  content. The old "Overview / Recent activity" section is dropped.
- **Sidebar footer:** avatar + name → My account settings; a right-aligned
  **logout** button beside them.
- **Preferences persist server-side, per user, and survive logout** (extend the
  mechanism behind `theme_preference`): theme, sidebar pin/width/group-collapse,
  Quick Access filter, and any future "what to show" toggle. (The mockup's
  `localStorage` and its "Preview as" role/approver control are demo-only and do
  not ship.)

## Consequences

- **API additions** (`designs/home-redesign.md` §8): `owner_name` + `my_role` on
  the project response; a `/api/home/summary` aggregate for the header counts; a
  global `/api/search` (ACL/feature/role-filtered) or merged per-scope lists; a
  user-facing `/tutorials` page + listing; an extended per-user preferences
  store; a "has-active-tutorials" flag for Quick Access items.
- **Shell rework** in `_chrome.py` (nav model, gating, pin/resize, header) and a
  rewrite of `home.py`'s body. Built in slices (§12 of the design doc).
- **Help docs** updated alongside each slice (ADR-0014): Home/landing,
  navigation, and search chapters (user); any admin-surface relocation (admin).
- Open at acceptance (minor, non-blocking): search empty-focus behaviour
  (recent items vs nothing) and off-feature scope hiding — design recommends
  recent items + hide.

## References

- `designs/home-redesign.md` — build-level spec (card contents, click
  behaviours, endpoints, gating, persistence, slices).
- `home_designs/home_v3.html` — locked interactive mockup.
- ADR-0002 (project ACLs / ownership), ADR-0005 (roles + capabilities),
  ADR-0014 (documentation system).
