# Home page + app-shell redesign

**Status:** **SHIPPED 2026-06-22** — **ADR-0016** (`decisions/0016-home-redesign.md`); built into `_chrome.py` (shell) + `home.py` (body) + `tutorials.py` (new page), ARCHITECTURE §16.42. All slices landed: server-side prefs (migration 0081), grouped/gated/pinnable+resizable sidebar, slim header (global search · theme · help), tiles + favourites Quick Access + Approvals + four My-Work tables, `/api/{home/summary,search}` + `ProjectResponse` owner/role, `/tutorials`. Backend verified end-to-end vs the demo DB; **NiceGUI render still needs a browser pass.** Open follow-ups: remove the superseded Home renderers left as dead code; per-item Approvals deep-link (needs an `/approvals?item=` param on the approvals page); search scopes for tools/workflows/materials/files; Decision D's "recent items on empty focus".
**Design source of truth:** the interactive mockup `home_designs/home_v3.html` (locked 2026-06-22).
**Implements into:** `ui/pages/home.py` (page body) + `ui/pages/_chrome.py` (shell: sidebar, header).
**Related:** ADR-0002 (project ACLs), ADR-0005 (roles), ADR-0014 (help docs). An ADR will be cut once this doc is signed off.

> This doc translates the locked mockup into an implementation spec. The **§7 card tables** are the part needing Robert's input: each lists the *proposed* fields shown and what clicking does. Mark them up directly — ✏️ = please confirm/change.

---

## 1. Goals & non-goals

**Goals**
- Replace the legacy 3-column Home with the `home_v3` layout: a grouped, gated, pinnable sidebar; a split global search; a row of action tiles; and content cards (Quick Access, Approvals, My Projects, My Jobs, Parametric Studies, Optimisations).
- Every datum shown comes from an API call, filtered server-side by the same ACL / feature-flag / role rules already in force — the UI never widens what a user can see.
- Persist the user's shell preferences (sidebar width, theme, pin state).

**Non-goals**
- The mockup's **"Preview as" role/approver control is a demo affordance only** — it does **not** ship. The real app derives role + approver status from the session.
- No new analytics/search infrastructure beyond thin list endpoints (see §8).
- Surrogate/cohort/V&V feature behaviour is unchanged; we only add their nav entries.

---

## 2. Layout overview

```
┌─ sidebar (pinnable, resizable) ─┐┌─ header: split search · theme · help ─┐
│ MATOFX SPDM™                    ││ greeting + summary line                │
│ ▸ Dashboard                     ││ [Create▾][Studies▾][Job Search][Acct▾] │
│ ▸ Studies                       ││ ┌ Quick Access ┐ ┌ Approvals* ┐        │
│ ▸ Libraries                     ││ │ favourites   │ │ pending     │        │
│ ▸ Governance                    ││ └──────────────┘ └─────────────┘        │
│ ⋮ (Admin pinned to bottom, SU)  ││ My Work: Projects · Jobs · Studies · Opt│
│ 〔avatar〕Robert B.             ││                                        │
└─────────────────────────────────┘└────────────────────────────────────────┘
```

`*` Approvals card only renders for approvers.

---

## 3. Sidebar navigation model

Groups render top→bottom; **Admin is ordered to the bottom** and is entirely Super-User-only. Items marked **SU** show only for Super User / Administrator and carry a padlock; **RU** items show for everyone. Each item is feature-flag-gated where noted (entry absent when the flag is off, matching today's `_chrome.py`).

| Group | Item | Access | Route | Feature flag |
|---|---|---|---|---|
| **Dashboard** | Home | RU | `/home` | — |
| | Projects | RU | `/dashboard` | — |
| | My Jobs | RU | `/my-jobs` | — |
| | My Custom Workflows | RU | `/my-workflows` | — |
| | Custom Workflows Inspector | SU | `/admin/custom-workflows` | — |
| | Project Metadata | SU | `/admin/project-metadata` | — |
| **Studies** | Compare Jobs | RU | `/compare` | — |
| | Job Cohorts | RU | `/cohorts` | surrogate_family |
| | Parametric Studies | RU | `/studies` | parametric_studies |
| | Optimisation | RU | `/optimisation` | optimisation_framework |
| | Surrogate Models | RU | `/models` | surrogate_family |
| **Libraries** | File Library | RU | `/library` | file_library |
| | Materials Database | RU | `/materials` | materials_database |
| | Explore MATOFX Database | SU | `/admin/explore-db` | — |
| **Governance** | Approvals | RU¹ | `/approvals` | — |
| | V&V records | RU | `/vv-records` | vv_tracking |
| | Results Warehouse | RU | `/results` | kpi_extraction |
| | Audit Log | SU | `/admin/audit` | — |
| | V&V records - Inbox | SU | `/admin/vv-records` | vv_tracking |
| **Admin** (bottom, SU group) | New/Edit User | SU | `/admin/users` | — |
| | Edit Teams, Groups & Permissions | SU | `/admin/teams` | — |
| | New/Edit Tool | SU | `/admin/tools` | — |
| | New/Edit Workflow | SU | `/admin/workflows` | — |
| | Migrate Tools/Workflows | SU | `/admin/migrate` | — |
| | Send notifications | SU | `/admin/send-notifications` | — |
| | Tutorial Management | SU | `/admin/tutorials` | — |
| | Usage Statistics | SU | `/admin/usage` | — |
| | Lineage | SU | `/admin/lineage` | — |
| | File paths management | SU | `/admin/paths` | — |
| | General Settings | SU | `/admin/general-settings` | — |

¹ **CONFIRMED — approver-only:** the Approvals nav entry (and the Approvals card, and the summary clause) show **only to approvers** (`current_is_approver`); hidden entirely otherwise. RU label means "not SU-gated" — approver status is the separate gate.

**Behaviours:** groups collapse/expand (persisted); pinned = fixed open; unpinned = icon-only rail that expands on hover (overlay, no reflow); drag-to-resize 188–460px. Active page highlighted; sidebar label font slightly smaller than card headers.

---

## 4. App-shell header

| Element | Behaviour |
|---|---|
| Greeting + summary | "Good morning, {first name}." + a live summary line. **CONFIRMED:** keep it, and include clauses for **active jobs · optimisations running · parametric studies running · approvals waiting**. Each clause shows **only when relevant** (count > 0 and that feature enabled; approvals clause only for approvers). Needs a cheap aggregate — see §8. |
| Split search | query field + `in:` scope dropdown — see §6. |
| Theme toggle | light/dark, persisted server-side (§9). |
| Help (?) | hover (and click) → selector: **Help Documentation** (`/help`) · **Tutorials** (`/tutorials`). **CONFIRMED:** build a small user-facing `/tutorials` page (today tutorials only surface per-tool/general on Home + per-item in Quick Access). |

The legacy notifications bell is removed.

---

## 6. Global search

Quick-find / jump-to across entities — distinct from the structured **Job Search** page (which filters completed runs by tool + input values).

- **Scopes** (`in:` dropdown): All · Jobs · Projects · Studies · Optimisations · Tools · Workflows · Materials · Files.
- **Results:** grouped by type, each row = icon + **label** (never alias) + context line + status pill where relevant; live as you type (debounced), keyboard navigable.
- **Gating:** results filtered server-side by ACL + feature flag + role; approver-only items (e.g. "awaiting approval" jobs) only for approvers.
- **Click:** navigates to that entity's detail/launch route (see §7 routes).
- ✏️ **Decision D:** on empty focus, show **recent items** or nothing? *Recommend recent (last 5 visited).* And: does `Files`/`Materials` scope appear when those features are off? *Recommend hide.*

---

## 7. Home body — card contents & click behaviour  ✏️ (your input needed)

Legend: **Row click** = clicking anywhere on the row; **secondary** = an explicit button/icon. All names are display **labels**. "Owner" is resolved to a friendly name.

**Table layout rule (all My-Work tables):** every column — header *and* cell — is **left-aligned**, including counts. Column headers must line up exactly with their column's content (no right-aligned numbers, no drift). The header sits above a scroll area whose scrollbar gutter is reserved so header tracks and row tracks stay aligned.

### 7.1 Create (tile, teal, dropdown)
| Menu item | Click does |
|---|---|
| Project | Opens New-project dialog (metadata + optional share), in place. |
| Scenario | Opens chooser: pick parent project → name → creates, navigates to `/scenarios/{id}`. |
| Job | Opens the project→scenario→tick-tools/workflows chooser; launches into `/my-jobs`. |
| Custom Workflow | → `/my-workflows`. |

### 7.2 Studies (tile, dropdown)
Each item navigates: Compare Jobs→`/compare` · Job Cohorts→`/cohorts` · Parametric Studies→`/studies` · Optimisation→`/optimisation` · Surrogate Models→`/models`. (Items absent when their feature flag is off.)

### 7.3 Job Search (tile) → `/job-search`.

### 7.4 My Account (tile, dropdown) — CONFIRMED
**My account settings**→`/account` · **Log out**→ revoke session + `/login`. (No extra shortcuts.)

**Sidebar footer** mirrors this: the **avatar + name** are clickable → My account settings, then a **vertical divider**, then a **right-aligned logout control** = arrow-out-the-door icon **+ "Logout"** label (red on hover). Both account-settings entry points (this tile + the footer) go to the same `/account` page. In the collapsed icon-rail the name, divider, and logout label hide, leaving just the avatar (which still opens settings).

### 7.5 Quick Access (card) — CONFIRMED
Favourite tools + workflows the user has starred. A **split pill selector next to the title** (`Tools | Workflows | All`) filters the list; the chosen filter is **remembered per user** (§9).

| Field shown | Source |
|---|---|
| Icon (tool vs workflow) | kind |
| **Label** (tool name / workflow `name v<version>`) | catalogue |
| Subtitle: "Tool · {short description}" or "Workflow · {N tools}" | catalogue |
| ★ favourite star (unfavourite in place) | favourites |
| **Tutorials** button — only when active tutorials exist for that tool/workflow | tutorials |
| **Launch** button (hover-reveals split: **In existing project** / **In new project**) | — |

- **Launch** (hover) → two sub-actions: *In existing project* (scenario chooser) and *In new project* (create project+scenario then launch) — the existing two launch flows.
- **Tutorials** → opens the tool/workflow's tutorial(s) (single → open directly; document + video → chooser). Button absent when none uploaded.
- Filter pill default = **All**; remembered across sessions/logout.
- **No Manage button / footer** (removed). Favourites are added by starring a tool/workflow where you encounter it (catalogue / launch surfaces) and removed in place via the star on each Quick Access row.

### 7.6 Approvals (card — approvers only)
| Field shown (proposed) | Source |
|---|---|
| Item-type icon (job result / optimisation start gate / study / etc.) | approval kind |
| Title — what's awaiting | approval |
| Sub: requester name · project/scenario · age | approval |
| **Review** button | — |

- **Review / row click** → **deep-links straight to that item's review panel** (CONFIRMED), e.g. `/approvals?item={id}`, not just the queue.
- Footer "Open approvals queue" → `/approvals`.

### 7.7 My Projects (table) — CONFIRMED
| Column | Source | Notes |
|---|---|---|
| Project | `name` (+ description as subtext) | |
| **Owner** | ACL owner → friendly name | *New:* `ProjectResponse` returns `creator_user_id` only; owner can be transferred — add `owner_name` to the response (see §8). |
| **My role** | caller's ACL role on the project (Owner / Member / Viewer) | resolve the caller's `ProjectAcl` role; add `my_role` to the response (see §8). |
| # Scenarios | computed | |

- **No Updated column** and no State column; archived projects are filtered out by default.
- **Row click** → `/projects/{id}`. Header **New** → New-project dialog. Footer "Show all N projects" → `/dashboard`.

### 7.8 My Jobs (table) — CONFIRMED
| Column | Source | Notes |
|---|---|---|
| Job | tool **label** + `#0000001` id (subtext) | no leading status dot (state pill carries status) |
| Project / Scenario | "Project / Scenario" | column header renamed from "Scenario" |
| State | `state` | pill |
| When | `created_at` | relative |

- **Progress % dropped** (CONFIRMED) — the API has no numeric percent (only `heartbeat_state`); the state pill carries status.
- **Row click** → `/jobs/{id}`; workflow-child jobs → `/my-jobs?workflow_run={rid}&job={id}` (matches today's behaviour).
- Header **New** → Create-job chooser. Footer → `/my-jobs`.

### 7.9 Parametric Studies (table — flag-gated) — CONFIRMED
| Column | Source |
|---|---|
| Study | `name` (+ tool label · `#id` subtext) |
| Project | `project_id` → name |
| Runs | `generated_count` (+ "N skipped") |
| State | `state` pill |
| When | `created_at` relative |

- No Owner column. **Project** column (not scenario): a parametric study is homed at the **project** and fans out many runs — it has no single scenario (CONFIRMED).
- **Row click** → `/studies/{id}`. Header **New** → `/studies`.

### 7.10 Optimisations (table — flag-gated)
| Column | Source |
|---|---|
| Study | `name` (+ target tool/workflow label subtext) |
| Best | `current_best_json` (objective) |
| Batch | `last_batch_number` |
| State | `state` pill |
| ✏️ Owner? | `created_by_user_id` → name |

- **Row click** → `/optimisation/{id}`. Header **New** → `/optimisation`.

> **Body scope — CONFIRMED:** the old "Overview / Recent activity / Awaiting approval" section stays **dropped** (Approvals card + My Jobs cover it). All four My-Work tables stay.

---

## 8. Data / endpoint implications

Most data already has list endpoints (projects, scenarios, jobs, studies, optimisation studies, favourites, tutorials, approvals summary). Gaps this design introduces:

1. **Owner name + caller's role on projects** — `ProjectResponse` has `creator_user_id` only. Add `owner_user_id` + `owner_name` (owner can be transferred) **and** `my_role` (the caller's ACL role: owner/member/viewer) so My Projects can show both Owner and My role without per-row lookups.
2. **Owner/creator name on studies & optimisations** — same pattern (`created_by_user_id` → name) if Decision shows Owner columns.
3. **Header summary counts** (Decision B) — a small `/api/home/summary` (active jobs, pending approvals) avoids fan-out across every project/scenario at page load. Today Home walks projects→scenarios→jobs; a single aggregate is cheaper.
4. **Global search** — either per-scope list endpoints (reuse existing) merged client-side, or one `/api/search?q=&scope=` that fans out server-side with ACL filtering. *Recommend a server endpoint* so ranking + gating live in one place.
5. **Recent items** (Decision D) — needs a small per-user recent-visits store if we show recents on empty focus.
6. **Header summary aggregate** — `/api/home/summary` returns active-job count, running-optimisation count, running-parametric-study count, pending-approval count (the last only computed for approvers); each surfaced only when its feature is on and count > 0.
7. **User preferences store** — extend the per-user prefs (today just `theme_preference`) to hold sidebar pin/width/group-collapse + Quick Access filter; survives logout (§9).
8. **`/tutorials` page + endpoint** — a user-facing list of tutorials the caller can see (general + per-tool/workflow), for the Help selector.
9. **Quick Access tutorials flag** — each favourite needs "has active tutorials?" so the Tutorials button renders conditionally (reuse the active-tutorials bucket already loaded on Home).

---

## 9. Persisted user settings — CONFIRMED REQUIREMENT

**All user preferences must be remembered and survive logout** (and follow the user across devices). That rules out `localStorage` / per-browser `app.storage.user` as the system of record — preferences are stored **server-side, per user, in the DB**, alongside the existing `theme_preference` column (extend with new preference columns or a single `user_preferences` JSON blob). The mockup's `localStorage` is a stand-in only.

| Setting | Persist | Notes |
|---|---|---|
| Theme (light/dark) | `users.theme_preference` | already implemented; survives logout |
| Sidebar pinned | per-user DB pref | new |
| Sidebar width | per-user DB pref | new; clamp 188–460 |
| Sidebar group collapse state | per-user DB pref | extends today's sidebar-section expand persistence |
| Quick Access filter (Tools/Workflows/All) | per-user DB pref | new |
| *(any future "what to show" toggle)* | per-user DB pref | same mechanism |

Loaded once at login/page render into session, written through on change.

---

## 10. Permissions & feature gating (summary)

- **Role:** RU items → everyone; SU items + Admin group → Super User / Administrator only. Resolved from session, not a toggle.
- **Approver:** Approvals card + (per Decision A) the Approvals nav entry + the summary clause.
- **Feature flags:** per §3 table — entry absent when off; API 404s the routes too.
- **Project ACL:** every project/job/study list is already ACL-filtered server-side.

---

## 11. Decisions — resolved 2026-06-22

- **A** ✅ Approvals = **approver-only** (nav + card + summary clause).
- **B** ✅ Keep summary; clauses = jobs active · optimisations running · parametric studies running · approvals waiting, **each shown only when relevant**.
- **C** ✅ Build a small user-facing **`/tutorials`** page; Help selector points there.
- **F** ✅ Quick Access: **no plain row-click navigate** — actions are the **Launch** split (existing/new project) + **Tutorials** button; plus a **Tools/Workflows/All** filter pill (remembered).
- **G** ✅ Approvals Review **deep-links** to the item.
- **H** ✅ My Projects columns: Project · **Owner** · **My role** · # Scenarios (no Updated/State).
- **I** ✅ Drop progress %; "When" = `created_at`.
- **J** ✅ Overview section stays dropped; keep all four My-Work tables. Parametric Studies gains a **Scenario (Project / Scenario)** column, no Owner.
- **Global** ✅ All preferences persist **server-side, survive logout** (§9).

- **E** ✅ My Account dropdown **stays as is** (Account info + Log out; no extra shortcuts).
- **§7.9** ✅ Parametric Studies show **Project** only (no scenario — studies are project-homed).
- **Quick Access Manage** ✅ removed (not required) — favourites starred where encountered, unstarred in place.

**Still open (minor):**
- **D** — search empty-focus: show recent items vs nothing; hide scopes for off-features. *(Recommend: recent items; hide off-feature scopes.)*

---

## 12. Implementation slices (proposed)

1. **Shell — sidebar:** new grouped/gated nav model in `_chrome.py` (groups, SU gating, Admin-to-bottom, padlocks). No behavioural JS yet.
2. **Shell — pin/resize/persistence:** pinnable + hover-rail + drag-resize, persisted to `app.storage.user`.
3. **Shell — header:** split search shell, help selector, account tile, remove bell.
4. **Home body — tiles + Quick Access + Approvals** (per confirmed §7).
5. **Home body — My-Work tables** with confirmed columns (+ owner-name API change).
6. **Global search** endpoint + live dropdown.
7. **Header summary** endpoint (if Decision B = keep).
8. Help-docs update (user/admin chapters) + ARCHITECTURE §16 entry, alongside each slice.

---

## 13. Help-docs impact

Per project policy, every slice updates `docs/{public,user,admin}` in the same change: the Home/landing chapter (user), navigation/sidebar overview (user), search usage (user), and any admin-surface relocation notes (admin).
