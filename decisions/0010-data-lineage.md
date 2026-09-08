# 0010. Data lineage — interactive provenance graph

Date: 2026-05-26
Status: Accepted (architect ruling 2026-05-26 — full v1.0 scope per "Phase 2 expansion" conversation; pilot date re-baselined accordingly)

## Context

MATOFX records — in the database, today, without this ADR — every edge of a
job's provenance graph:

- Which tool **version** produced the job (`Job.tool_version_id`).
- Which workflow run, if any, it belonged to (`Job.workflow_run_id`).
- Which parametric / optimisation / cohort study, if any, seeded it
  (`Job.parametric_study_id`, the new `Job.optimisation_study_id` per
  ADR-0009).
- Which library-file versions it consumed (`job_library_file_use` per
  ADR-0006, one row per file input field).
- Which material versions it consumed (`job_material_use` per ADR-0007,
  one row per material input field).
- Which surrogate-model version it ran against, if a surrogate-tool
  (`Job.surrogate_model_version_id` per ADR-0008).
- Which user submitted it, on which scenario, at which time
  (`Job.created_by_user_id`, `Job.scenario_id`, `Job.created_at`).
- Which approval gates it passed (`approval_request` rows).

The data is **all there**. What is missing is the surface that lets a user
**answer "where did this output come from?"** without writing SQL. For
regulated industries (defence / aerospace / pharma / automotive) this is a
hard procurement requirement, not a nice-to-have. The first question of
every compliance audit is "show me the provenance of this artefact".
Today, that question is answered by SSH + sqlite3 + a competent DBA.

The SPDM-scope deferral table flagged this gap (row: "Traceability to
source artefacts (CAD rev / requirement / test / part-BOM)") but bucketed
it with full PLM integration — which is genuinely v1.x+. The **internal**
data-lineage view — every edge of which is already in our own database — is
much smaller in scope and much higher in immediate customer value.

Customers asking for "data lineage" in modern SPDM rarely mean "PLM
round-trip"; they almost always mean **the local provenance graph rendered
visually with click-through to each node**. That is the v1.0 scope of this
ADR.

The customer is the sole maintainer ([[user_role]]) and has ruled this ADR
into v1.0 at full scope as part of the 2026-05-26 Phase 2 expansion.

## Decision

Build a **provenance graph viewer** as a thin query layer + UI page over
existing data. **No new database tables**; one new module
(`matofx/lineage/`); one new permission slug
(`lineage.view`); one new top-level UI page (`/lineage/{kind}/{id}`); two
new API endpoints. The build-level spec lands as `designs/data-lineage.md`;
this ADR records the decisions and rationale.

**1. The graph definition.** Nodes are objects in the v2 data model;
edges are the relations the schema already encodes:

| Node kind | Notes |
|---|---|
| `job` | A single `Job` row |
| `workflow_run` | A `WorkflowRun` row; expands to its child jobs |
| `parametric_study` / `optimisation_study` / `run_cohort` | Study siblings (per studies-engine §1) |
| `tool_version` | The pinned tool version that ran a job |
| `library_file_version` | A library file version consumed via the picker |
| `material_version` | A material version consumed via the picker |
| `surrogate_model_version` | A surrogate the job ran against (ADR-0008) |
| `scenario` / `project` | Hierarchy containers |
| `user` | The submitter / curator / approver |
| `approval_request` | A request that gated the job (pre or post) |
| `vv_record` *(once ADR-0011 lands)* | A V&V record bound to the tool or job |

| Edge kind | Source → Target |
|---|---|
| `submitted_by` | `job` → `user` |
| `belongs_to_scenario` | `job` → `scenario` |
| `belongs_to_workflow_run` | `job` → `workflow_run` |
| `seeded_by_study` | `job` → `parametric_study` / `optimisation_study` / `run_cohort` |
| `ran_tool_version` | `job` → `tool_version` |
| `consumed_library_file_version` | `job` → `library_file_version` (one per file input field) |
| `consumed_material_version` | `job` → `material_version` (one per material input field) |
| `ran_surrogate_version` | `job` → `surrogate_model_version` (ADR-0008) |
| `gated_by` | `job` → `approval_request` |
| `published_by` | `library_file_version` / `material_version` / `surrogate_model_version` → `user` |
| `derived_from` | `surrogate_model_version` → `(parametric_study | optimisation_study | run_cohort)` (the training source — ADR-0008) |
| `validated_by` *(once ADR-0011 lands)* | `tool_version` → `vv_record` ; `job` → `vv_record` |

The graph is **directed acyclic** by construction — each table's foreign keys
flow one way, and nothing in the schema lets a Job reference itself or a
later object. (The one "cycle-risky" relation — `surrogate_model_version
→ derived_from → study → seeded_jobs → ran_surrogate_version → … ` — is
not a cycle because a study's *seeded* jobs run *before* the surrogate
exists; surrogate-tool jobs that use the model come *after* and don't loop
back to the model's training data.) Visualised as a DAG with the inspected
node centred and edges fanning outward upstream + downstream.

**2. Query layer.** A new `matofx/lineage/service.py` exposes one method:

```python
LineageService.build_graph(
    kind: NodeKind,
    obj_id: int,
    *,
    upstream_depth: int = 6,
    downstream_depth: int = 3,
    actor: User,
) -> LineageGraph
```

`LineageGraph` is a frozen dataclass of `nodes: list[LineageNode]` +
`edges: list[LineageEdge]`. ACL is enforced **per node**: a node the caller
can't see is omitted, and the count of hidden nodes is reported on the edge
that would have led to it ("(N upstream nodes hidden by access controls)").
The same intersect-with-caller-ACL pattern run-cohorts already use
(`designs/studies-engine.md` §7).

Depth defaults are tuned for typical engineering graphs: upstream 6 (job →
workflow → study → fed-by surrogate → trained-on cohort → cohort's source
jobs → that job's tool) and downstream 3 (job → outputs used in workflow
step → downstream job → next downstream job). The user can expand any
truncated edge to walk further.

The service composes existing per-domain services:

- `JobService.get`, `JobService.list_visible_to_user`.
- `WorkflowRunService.get`.
- `LibraryService.get_version`, `MaterialsService.get_version`.
- `ModelRegistryService.get_version` (ADR-0008).
- The study services from ADR-0001 / ADR-0009 and the cohort service from
  ADR-0008.
- `ApprovalService.list_for_target`.
- `UserService.get_profile`.

No queries reach across services directly; the lineage service composes.
This keeps the lineage layer **read-only** and **decoupled** from any
service it queries — it can never introduce a write path.

**3. API surface.**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/lineage/{kind}/{id}` | Build + return the graph centred on the target node; query params `upstream`, `downstream`, `include_archived` |
| GET | `/api/lineage/{kind}/{id}/export` | Same graph as a downloadable JSON or GraphML file for external tools (Cytoscape / Gephi / d3) |

Both endpoints permission-gate on `lineage.view`. The first returns the
graph JSON; the second is a 200 with `Content-Disposition: attachment`.

`lineage.view` is granted by default to `administrator` and `super_user`;
custom roles can grant it to any user via the existing role editor
(ADR-0005's mechanics). The brief explicitly endorsed compliance/audit
roles needing this; most installs will grant it broadly.

**4. UI — `/lineage/{kind}/{id}`.**

- Renders the graph as an interactive node-link diagram (Cytoscape.js via
  the existing CDN-bundle pattern, or a Plotly Sankey if Cytoscape is too
  heavy — final library choice in `designs/data-lineage.md`; both are
  permissive-licensed and air-gap-bundleable). Each node is a clickable
  hyperlink to its own primary surface (job page, study page, file-library
  detail, material detail, model-registry detail).
- Layout: **DAG layered**, source nodes at the top, the inspected node
  centred, fan upstream + downstream from there. Edge labels use the
  human-readable relation name ("ran tool version", "consumed material",
  "approved by").
- **Filter row** above the graph (per UI Conventions): "Show only" toggles
  for each node kind, "Hide archived" default-on, "Hide workflow children
  collapsed" default-on.
- **Tabular fallback** — toggle to "Table view" presents the same data as
  a per-node-kind sectioned table; useful for accessibility and for users
  who hate node-link diagrams. Each table row is the same hyperlink.
- **Export buttons** — "Download graph (JSON)", "Download graph (GraphML)",
  "Download report (PDF)" — the last reuses the ADR-0004 self-contained
  HTML report mechanism, rendering the graph as an inlined SVG.

**5. Entry points.** The lineage page is reachable from:

- The job page — new "Provenance" button beside the existing Outputs /
  Logs tabs.
- The workflow-run page — same Provenance button.
- The study detail pages (parametric / optimisation / cohort).
- The file-library / materials / model-registry detail pages — provenance
  centred on the relevant version row, showing what consumed it.
- The user profile page — provenance centred on the user (their submitted
  jobs / approved gates / curated artefacts) — admin-only by default since
  it summarises a user's whole activity (lineage.view + admin together).
- The compare report — every Compare-rendered job header gets a "Provenance"
  link in the column-header context (per §16.3's existing column-header
  context rendering).

**6. Cross-cutting impacts.**

- **Compliance** — the per-job HTML report (ADR-0004) gains an optional
  "Include provenance graph" toggle. Default off (keeps file size
  predictable); when on, the graph SVG is inlined.
- **Audit** — every lineage *view* writes a `lineage.viewed` audit event
  with the inspected node + actor + filter state. The graph itself isn't
  in the payload (volume reasons); the audit just records that a user
  inspected lineage of X.
- **Performance** — graph builds for a deeply-connected job (workflow
  child + studied + uses 8 library files + 4 materials + a surrogate)
  could touch hundreds of rows. The query layer **caches** per-node
  resolution within a request (a tool_version fetched for one job is the
  same tool_version another job points at). For graphs that exceed a soft
  cap (default 500 nodes), the UI degrades to a table-only view with a
  "graph too large" notice; the operator can raise the cap via a General
  Setting.
- **Archive handling** — by default archived projects / scenarios / library
  files / materials are included (provenance is about *what happened*,
  not *what is currently active*), but rendered greyed out + tagged
  "(archived)". Toggleable per session.

**7. Out of scope for v1.0** (no-add-back lock):

- **PLM / CAD-revision linkage** — connecting MATOFX-internal lineage to
  external systems (Windchill, Teamcenter, 3DEXPERIENCE). Still v1.x+.
- **Time-travel queries** — "show me the lineage as it was on 2025-12-01".
  The data exists (`audit` is hash-chained) but the surface is hard.
  Defer.
- **Diff between two lineage graphs** — "what's different between the
  provenance of job A and job B?". Powerful but a much larger UI build.
  Defer.
- **Saved lineage queries** — pre-canned audits ("show me every job that
  consumed material version X"). The export API supports this externally;
  in-product saved queries defer to v1.x.

**8. Cross-references.**

- **ADR-0006 / ADR-0007** — the existing `job_library_file_use` and
  `job_material_use` per-field provenance rows are the file/material edges.
- **ADR-0008** — the `surrogate_model_version` node + the new
  `Job.surrogate_model_version_id` edge land for surrogate-tool jobs.
- **ADR-0009** — the new `Job.optimisation_study_id` edge is just another
  study-seeded-job edge.
- **ADR-0011** — V&V records become an additional node kind + edge kind;
  the V&V ADR adds the implementation; this ADR reserves the slot.
- **ADR-0012** — the per-tool KPI table cross-links to lineage: every KPI
  row is a click-through to lineage centred on the underlying job.

## Consequences

- **Cheapest of the five new ADRs.** No new tables; ~one week of build
  time per the rough sizing. Pulled into v1.0 because the cost is small
  relative to the customer-procurement value.
- **One new permission** (`lineage.view`); default on
  `administrator` + `super_user`. No new capability role.
- **Cytoscape.js (or Plotly Sankey) bundles** — one CDN-aware library
  added to the air-gap bundle (~1 MB). SBOM update.
- **The audit table grows.** `lineage.viewed` events accumulate; the
  existing audit-retention setting governs cleanup.

## Build phases

Per house style:

- **Phase 1** — `matofx/lineage/` package; `LineageService.build_graph`;
  unit tests over each node kind + each edge kind in isolation; ACL
  intersect tests.
- **Phase 2** — API: `GET /api/lineage/{kind}/{id}`; `GET .../export`
  (JSON + GraphML serialisers); `lineage.view` permission seed migration.
- **Phase 3** — UI: `/lineage/{kind}/{id}` page with the chosen graph
  library; filter row; table-view fallback; export buttons.
- **Phase 4** — Entry-point integrations (job / workflow / study / library
  / material / model / compare / user profile).
- **Phase 5** — Performance: per-request resolution cache; soft cap +
  graceful degradation; provenance-in-report toggle for ADR-0004.
- **Phase 6** — Docs + cross-ADR consistency review; ROADMAP tick.

## Revision history

- **2026-05-26** — initial ruling at full v1.0 scope. Decision driven by
  the recognition that the data is already in the DB; the missing piece is
  a UI + thin query layer. Compliance value disproportionate to build
  cost.
