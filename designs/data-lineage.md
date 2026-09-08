# Design — Data lineage / provenance graph

Status: **Design locked 2026-05-26** (Phase 2 expansion conversation). Implementation not started.
Decision record: `decisions/0010-data-lineage.md`.
Cross-ADRs: `0002` (project ACL), `0004` (per-job HTML report), `0005` (multi-role capabilities), `0006` (file library), `0007` (materials), `0008` (surrogate family), `0009` (optimisation framework), `0011` (V&V tracking), `0012` (KPI extraction).
Brief sections: `PROJECT_BRIEF.md` §I46 (must-ship list), SPDM-scope-deferral table row "Traceability to source artefacts".
Delivery tracking: `ROADMAP.md` Phase 2.

This is the build-level spec. The ADR records *why*; this records *what to build*.
Everything here is inside ADR-0010 + the ADRs it composes (no new tables; pure read-side).

---

## 1. Scope at a glance

| Capability | v1 | Deferred |
|---|---|---|
| `LineageService.build_graph` over every existing edge | ✅ | |
| ACL-aware per-node visibility intersect with caller | ✅ | |
| Configurable upstream / downstream depth with truncation footnotes | ✅ | |
| `lineage.view` permission (default on `administrator` + `super_user`) | ✅ | |
| `GET /api/lineage/{kind}/{id}` JSON graph | ✅ | |
| `GET /api/lineage/{kind}/{id}/export` JSON + GraphML download | ✅ | |
| Shared `LineageView` component mounted inline as a "Provenance" tab on each host page | ✅ | |
| Cytoscape.js node-link UI + DAG layered layout | ✅ | |
| Standalone admin-space page `/admin/lineage/{kind}/{id}` for centres without a host page + canonical deep-link target | ✅ | |
| Tabular fallback view (accessibility + large graphs) | ✅ | |
| Provenance entry points: job / workflow / study / library / material / surrogate / compare (inline tabs) + admin user-activity (standalone) | ✅ | |
| Optional inlined SVG in ADR-0004 per-job HTML report | ✅ | |
| `lineage.viewed` audit event | ✅ | |
| Soft cap + graceful degradation past the cap | ✅ | |
| Archive-aware rendering (greyed `(archived)` nodes, toggleable) | ✅ | |
| PLM / external CAD-revision linkage (Windchill / Teamcenter / 3DEXPERIENCE) | | ✅ v1.x+ |
| Time-travel queries ("show lineage as of date D") | | ✅ v1.x |
| Lineage diff between two graphs | | ✅ v1.x |
| Saved / pre-canned lineage queries | | ✅ v1.x (export API covers ad-hoc external) |
| Cross-install lineage (federated across multiple deployments) | | ✅ v1.x+ |

The whole feature is **read-only**. There is no write path; lineage cannot mutate any object it reads.

---

## 2. Data model + migration

**No new tables.** Every edge the lineage layer reads is already in the database
under ADRs 0001–0009. This design adds:

- **One new permission slug**: `lineage.view`.
- **One Alembic migration**: `0052_lineage_permission.py`, chaining off head `0051`.
  Seeds `lineage.view` onto the `administrator` role; `super_user` holds it
  implicitly via the catalog sentinel. Pattern identical to `0047` (live jobs).
- **One General Setting** (added to the existing catalog, no schema change):
  `lineage_max_nodes` (int, default `500`, `requires_role='super_user'`, section
  `Data governance`) — the soft cap above which the UI degrades to table-only.

That's it. The migration head after this design ships is `0052`.

---

## 3. Node + edge taxonomy

The lineage graph is **virtual** — assembled in-memory by `LineageService` from
existing rows. The node and edge kinds below are the exhaustive v1.0 set; new
kinds land as additional `NodeKind` / `EdgeKind` enum members in later ADRs (e.g.
ADR-0011's `vv_record`).

### 3.1 Node kinds

```python
class NodeKind(StrEnum):
    JOB                       = "job"
    WORKFLOW_RUN              = "workflow_run"
    PARAMETRIC_STUDY          = "parametric_study"
    OPTIMISATION_STUDY        = "optimisation_study"  # ADR-0009
    RUN_COHORT                = "run_cohort"          # ADR-0008
    TOOL_VERSION              = "tool_version"
    LIBRARY_FILE_VERSION      = "library_file_version"
    MATERIAL_VERSION          = "material_version"
    SURROGATE_MODEL_VERSION   = "surrogate_model_version"  # ADR-0008
    SCENARIO                  = "scenario"
    PROJECT                   = "project"
    USER                      = "user"
    APPROVAL_REQUEST          = "approval_request"
    VV_RECORD                 = "vv_record"           # reserved for ADR-0011
```

Each node carries a **stable composite key** `(kind, id)` and a small render
payload (display name, state badge, archived flag, deep-link path). The render
payload is the only thing serialised to the client — no raw column blobs, no
free-text spillover.

### 3.2 Edge kinds

```python
class EdgeKind(StrEnum):
    SUBMITTED_BY                 = "submitted_by"                  # job → user
    BELONGS_TO_SCENARIO          = "belongs_to_scenario"           # job → scenario
    SCENARIO_IN_PROJECT          = "scenario_in_project"           # scenario → project
    BELONGS_TO_WORKFLOW_RUN      = "belongs_to_workflow_run"       # job → workflow_run
    SEEDED_BY_STUDY              = "seeded_by_study"               # job → study sibling
    RAN_TOOL_VERSION             = "ran_tool_version"              # job → tool_version
    CONSUMED_LIBRARY_FILE_VERSION = "consumed_library_file_version" # job → library_file_version (per field)
    CONSUMED_MATERIAL_VERSION    = "consumed_material_version"     # job → material_version (per field)
    RAN_SURROGATE_VERSION        = "ran_surrogate_version"         # job → surrogate_model_version
    GATED_BY                     = "gated_by"                      # job → approval_request
    PUBLISHED_BY                 = "published_by"                  # version → user (creator/approver)
    DERIVED_FROM                 = "derived_from"                  # surrogate → study/cohort (training source)
    VALIDATED_BY                 = "validated_by"                  # tool_version | job → vv_record (ADR-0011)
```

Every edge carries `(source: (kind, id), target: (kind, id), relation: EdgeKind,
label: str, field_name: str | None)`. `field_name` is populated for
`consumed_library_file_version` / `consumed_material_version` because the same
job can consume the same file/material on more than one field — preserving the
field name keeps the picture engineering-meaningful.

### 3.3 The graph is a DAG by construction

Each table's foreign keys flow one direction (older → newer); nothing lets a
`Job` reference a later object. The one cycle-risky relation —
`surrogate_model_version → derived_from → study → seeded_jobs →
ran_surrogate_version → ...` — does **not** close because the study's seeded
jobs are the surrogate's *training set* (they predate the surrogate), while
surrogate-tool jobs that use the model (i.e. set `Job.surrogate_model_version_id`)
are submitted *after* the surrogate exists and don't loop back to the training
data.

`LineageService` includes a defensive `_assert_dag` step in build that walks the
assembled edge list with DFS + colour marking and raises
`LineageInvariantError` if it discovers a back-edge. This is a development-time
guard, not a runtime expectation; in production it should never fire.

### 3.4 Edges by ORM source-of-truth

The query layer never invents an edge. Each kind has exactly one ORM source:

| Edge kind | ORM source |
|---|---|
| `submitted_by` | `Job.created_by_user_id` |
| `belongs_to_scenario` | `Job.scenario_id` |
| `scenario_in_project` | `Scenario.project_id` |
| `belongs_to_workflow_run` | `Job.workflow_run_id` |
| `seeded_by_study` | `Job.parametric_study_id` / `Job.optimisation_study_id` (ADR-0009) |
| `ran_tool_version` | `Job.tool_version_id` |
| `consumed_library_file_version` | `job_library_file_use` rows (ADR-0006) |
| `consumed_material_version` | `job_material_use` rows (ADR-0007) |
| `ran_surrogate_version` | `Job.surrogate_model_version_id` (ADR-0008) |
| `gated_by` | `approval_request` rows where `target_kind='job'` and `target_id=job.id`, plus the pre/post discriminator |
| `published_by` | `*_version.created_by_user_id`, plus the approver via the resolved `approval_request.designated_approver_user_id` |
| `derived_from` | `surrogate_model_version.source_kind` + `source_id` (ADR-0008 studies-engine §10) |
| `validated_by` | `tool_vv_record.tool_version_id` / `job_vv_record.job_id` (ADR-0011) |

Cohort membership is special: a run cohort is a *query*, not a parent FK on
`Job` (per `designs/studies-engine.md` §3). The cohort's `seeded_by_study` edges
materialise by **running the cohort query** and listing matched jobs at lineage
build time. Cohort jobs therefore appear as `seeded_by_study` edges from each
matched `Job` → `RunCohort`, identical in shape to the other study siblings.

---

## 4. Service (`matofx/lineage/service.py`)

`LineageService` (async, read-only, audited at the build entry point). One
public method.

```python
@dataclass(frozen=True, slots=True)
class LineageNode:
    kind: NodeKind
    obj_id: int
    label: str
    state: str | None            # "active" / "archived" / "retired" / ... (per-kind)
    archived: bool               # convenience boolean for rendering
    deep_link: str               # absolute path on this install
    payload: dict[str, str | int | float | bool | None]  # render-only extras

@dataclass(frozen=True, slots=True)
class LineageEdge:
    source: tuple[NodeKind, int]
    target: tuple[NodeKind, int]
    relation: EdgeKind
    label: str
    field_name: str | None = None
    truncated_count: int = 0    # >0 when this edge stands in for N hidden nodes

@dataclass(frozen=True, slots=True)
class LineageGraph:
    centre: tuple[NodeKind, int]
    nodes: tuple[LineageNode, ...]
    edges: tuple[LineageEdge, ...]
    hidden_by_acl: int           # total nodes the caller can't see
    hidden_by_depth: int         # total nodes beyond the depth limit
    soft_capped: bool            # True iff |nodes| reached lineage_max_nodes

class LineageService:
    async def build_graph(
        self,
        *,
        kind: NodeKind,
        obj_id: int,
        actor: User,
        upstream_depth: int = 6,
        downstream_depth: int = 3,
        include_archived: bool = True,
        soft_cap: int | None = None,   # None → read from General Settings
    ) -> LineageGraph: ...
```

### 4.1 Build algorithm

A breadth-first walk in two passes (upstream, then downstream) from the centre
node, with a single shared resolution cache keyed by `(kind, id)`. Each pass:

1. Initialise `frontier = {(centre_kind, centre_id)}`, `visited = {}`, `depth = 0`.
2. While `frontier` non-empty and `depth < depth_limit` and `|visited| <
   soft_cap`:
   a. For each node in the frontier, fetch via the per-kind resolver (see §4.2).
   b. Skip if the caller can't see it (`acl_visible == False`); record one toward
      `hidden_by_acl`; if it was reached via an edge, increment that edge's
      `truncated_count`.
   c. Add the visible node to `visited`; enumerate its upstream-or-downstream
      neighbours (per pass direction); add them to the next frontier.
3. After the loop, any node in the *next* frontier but not yet resolved counts
   toward `hidden_by_depth`; the edge to it is rendered with `truncated_count`.

`upstream` and `downstream` are defined relative to the **DAG arrow** direction:
upstream = follow edges *into* the centre (ancestors); downstream = follow edges
*out of* the centre (descendants). For the centre's job-typical case, upstream
gets you to the tool / library / material / study / approver; downstream gets you
to the workflow children and the surrogates trained on this job's results.

### 4.2 Per-kind resolvers — composition, not direct SQL

The lineage layer **never** writes ad-hoc joins. Each `NodeKind` has a resolver
that delegates to the existing service for that domain:

| Resolver | Existing service it calls |
|---|---|
| `_resolve_job` | `JobService.get`, `JobService.list_outputs_consumed_by` (new helper — see §4.4) |
| `_resolve_workflow_run` | `WorkflowRunService.get`, `WorkflowRunService.list_child_jobs` |
| `_resolve_parametric_study` | `ParametricStudyService.get`, `.list_children` |
| `_resolve_optimisation_study` | `OptimisationStudyService.get`, `.list_children` (ADR-0009) |
| `_resolve_run_cohort` | `RunCohortService.get`, `.list_members_for(actor)` (ADR-0008) |
| `_resolve_tool_version` | `ToolService.get_version` |
| `_resolve_library_file_version` | `LibraryService.get_version` + `.list_consumers` (new helper — §4.4) |
| `_resolve_material_version` | `MaterialsService.get_version` + `.list_consumers` |
| `_resolve_surrogate_model_version` | `ModelRegistryService.get_version` + `.list_consumers` (ADR-0008) |
| `_resolve_scenario` / `_resolve_project` | `HierarchyService.get_*` |
| `_resolve_user` | `UserService.get_profile` |
| `_resolve_approval_request` | `ApprovalService.get`, `.list_for_target` |
| `_resolve_vv_record` | `VVService.get` (ADR-0011) |

Each resolver returns `(LineageNode, list[upstream_edges], list[downstream_edges])`.
Resolution is cached for the lifetime of the build call — a `tool_version`
fetched once for one job is the same object every other job points at. The cache
is per-request, not global; a separate lineage build for a different actor
re-resolves with that actor's ACL.

### 4.3 ACL enforcement — intersect at the node level

ACL is enforced **per node**, not per edge. The existing per-service
`visible_to(actor)` predicates are the source of truth:

- `Job` / `WorkflowRun` / parametric / optimisation / cohort: per-job + parent
  project ACL (ADR-0002) + study home-project ACL.
- `LibraryFile` / `LibraryFileVersion`: scope-based (personal / project / global)
  per `designs/file-library.md` §3.
- `Material` / `MaterialVersion`: all authenticated users can see; the version's
  publish-state is read-only metadata.
- `SurrogateModelVersion`: governed Model registry scope (ADR-0008 §5) — mirrors
  the library scope.
- `User`: any authenticated user can see another user's display name + role;
  full profile lineage (a user's whole activity graph) needs `lineage.view` +
  `users.read_all` (admin-only by convention).
- `ApprovalRequest`: visible to actor iff the actor was the requester, the
  designated approver, an admin, or has visibility on the target job.
- `Scenario` / `Project`: ADR-0002 ACL.
- `ToolVersion`: visible iff `tool.visibility_state IN ('test','released')` to
  non-admin actors (existing ToolService rule); admins see everything.

Hidden nodes are **dropped silently** from `nodes`; the edge that would have
reached them is preserved with `truncated_count >= 1` and label suffix
"(restricted)". The render layer shows this as a stub node ("N restricted") on
the edge's far side, not an error.

### 4.4 Reverse-edge helpers needed in existing services

The build walks edges in **both** directions (upstream + downstream). The
existing services already expose the forward lookups (e.g.
`JobService.get(id) → Job` exposes `parametric_study_id`); we add the reverse
helpers each service needs to answer "what points at me?":

| New helper | Lives on | Reads |
|---|---|---|
| `JobService.list_visible_consumers_of_output(job_id, actor)` | `matofx.hierarchy.services` | Future-reserved for workflow output→input wiring lookup; v1.0 stub returns `[]` since workflow output→input is descriptor-resolved at queue time, not row-recorded |
| `LibraryService.list_consumers(version_id, actor, limit)` | `matofx.library.service` | `job_library_file_use` filtered through `JobService.visible_to` |
| `MaterialsService.list_consumers(version_id, actor, limit)` | `matofx.materials.service` | `job_material_use` similarly |
| `ModelRegistryService.list_consumers(model_version_id, actor, limit)` | `matofx.studies.surrogate.registry` (ADR-0008) | `Job` where `surrogate_model_version_id = …` |
| `WorkflowRunService.list_child_jobs(run_id, actor)` | `matofx.workflows.service` | already exists or trivial extension |
| `ParametricStudyService.list_children(study_id, actor)` | already exists | already exists |
| `OptimisationStudyService.list_children(study_id, actor)` | new with ADR-0009 | new with ADR-0009 |
| `RunCohortService.list_members_for(cohort_id, actor)` | new with ADR-0008 | new with ADR-0008 |
| `ApprovalService.list_for_target(target_kind, target_id, actor)` | `matofx.approvals.service` | filter by `actor`-visibility |
| `UserService.list_recent_activity_for(user_id, actor)` | `matofx.users.service` | jobs / approvals / curated artefacts the actor can see |

Every `list_consumers` accepts a `limit` (default 50, hard cap from General
Setting `lineage_max_nodes`) — a globally-shared library file referenced by
thousands of jobs must not blow the graph.

### 4.5 Performance

- **Resolution cache** per request. Touching the same `tool_version` row 30
  times in a graph hits the DB once.
- **Bulk-fetch friendly**. Resolvers expose a `bulk_resolve(ids)` shape so a BFS
  layer with K neighbours is one query, not K.
- **Soft cap**. Default `lineage_max_nodes = 500`. When the BFS exhausts the
  budget mid-layer, the remaining frontier is dropped, `hidden_by_depth` is
  incremented by the dropped count, and `soft_capped=True`. The UI degrades
  to **table view only** with a "Graph too large — showing tabular view" notice.
  Operators can raise the cap via the General Setting if they have unusual
  graphs; defaults stay engineering-typical.
- **No N+1 queries**. Each pass through the BFS is at most one DB round-trip per
  participating service (`asyncio.gather` across resolvers).

---

## 5. API surface

Feature gating: `lineage.view` permission required on all endpoints. The router
is **not** feature-flagged (lineage is always-on for v1.0).

```
GET    /api/lineage/{kind}/{id}                    JSON LineageGraph
GET    /api/lineage/{kind}/{id}/export?format=json|graphml
                                                   Attachment download
```

### 5.1 Query parameters (both endpoints)

| Param | Default | Notes |
|---|---|---|
| `upstream` | `6` | int, 0–20 clamped |
| `downstream` | `3` | int, 0–20 clamped |
| `include_archived` | `true` | when `false`, archived nodes are walked through but elided from the rendered result |
| `format` (export only) | `json` | `json` (LineageGraph dataclass) or `graphml` (Cytoscape / Gephi / d3 import) |

### 5.2 Response shape (JSON)

```jsonc
{
  "centre": {"kind": "job", "id": 4213},
  "nodes": [
    {
      "kind": "job", "id": 4213, "label": "heat_transfer #4213",
      "state": "complete", "archived": false,
      "deep_link": "/jobs/4213",
      "payload": {"tool_alias": "heat_transfer", "submitted_at": "2026-05-26T10:14:02Z"}
    },
    /* ... */
  ],
  "edges": [
    {
      "source": ["job", 4213], "target": ["tool_version", 117],
      "relation": "ran_tool_version", "label": "ran tool version",
      "field_name": null, "truncated_count": 0
    },
    /* ... */
  ],
  "hidden_by_acl": 0,
  "hidden_by_depth": 2,
  "soft_capped": false
}
```

### 5.3 Error mapping

- `LineageNotFoundError(kind, id)` → 404 with plain-English message.
- `LineagePermissionError` → 403 ("You don't have permission to view lineage.").
- `LineageInvariantError` → 500 (development guard; should never reach prod).
- `LineageDepthOutOfRange` → 422 (depth > 20 or < 0).

All audited via `lineage.viewed` on success and `lineage.view_denied` on 403/404.

### 5.4 GraphML serialisation

GraphML is the lingua franca of Cytoscape / Gephi / d3-force tooling. The
serialiser maps:

- Each `LineageNode` → `<node id="kind:id">` with attributes `label`, `kind`,
  `state`, `archived`, `deep_link`.
- Each `LineageEdge` → `<edge source="kind:id" target="kind:id">` with attribute
  `relation`, `label`, `field_name` (omitted when null), `truncated_count`.

The file is a self-contained XML document with a UTF-8 BOM-less encoding. No
external schema reference; consumers parse the inline `<key>` declarations.

JSON serialisation is the same `LineageGraph` shape as the live endpoint —
producers can swap a fetched response between live + saved without translation.

---

## 6. UI surfaces

### 6.0 Where lineage renders — inline tab by default, admin-space page as fallback

Lineage is **not** a top-level dashboard destination. It's a view *of* a
specific object, and the v2 house convention for that is a tab on the object's
host page (Outputs / Logs on a job; Children / Outputs on a workflow run; etc.).
This design follows that convention:

- **Default render path** — a shared `LineageView` component mounts inline as
  a **"Provenance" tab** on every host page (job / workflow_run / studies /
  library / material / surrogate detail). Same component, same Cytoscape.js
  canvas, same filter row, same tabular fallback. The user never leaves the
  host page.
- **Standalone fallback** — `/admin/lineage/{kind}/{id}` exists in the admin
  space for the cases the inline pattern can't cover:
  1. Centres that have no natural host page (the **admin user-activity view** —
     "everything user X has touched");
  2. A canonical deep-link URL for the export API and the ADR-0004 reports
     section's "Open as PDF report" link to point at;
  3. Audit / compliance contexts where the lineage URL is shared in an email
     and needs to render without first navigating into a host page.
  Page chrome adopts the admin shell (admin sidebar + breadcrumb) per the
  existing `ui/pages/admin_*.py` filename convention. Appears under
  **Admin → Lineage** in the admin nav.

The standalone page's *route* is gated by `lineage.view` like everything else
— a non-admin with `lineage.view` granted can deep-link into it; they just
don't see it in their nav. The *nav presence* is admin-only (admin sidebar
membership), the *permission* is broadly grantable per ADR-0010 §4.

### 6.1 The `LineageView` component — `ui/components/lineage_view.py`

The component the tab and the admin page both render. Layout:

```
┌──────────────────────────────────────────────────────────────────┐
│  Provenance — Job #4213 "heat_transfer"          [Back to job]   │
│                                                                  │
│  ┌─── Filter row ────────────────────────────────────────────┐   │
│  │ Upstream [6▼]  Downstream [3▼]  ☐ Hide archived          │   │
│  │ Show: ☑Tool  ☑Library  ☑Material  ☑User  ☑Approval ...   │   │
│  │ View: [● Graph] [○ Table]                                 │   │
│  └───────────────────────────────────────────────────────────┘   │
│                                                                  │
│  ┌─── Cytoscape.js DAG ─────────────────────────────────────┐    │
│  │                                                          │    │
│  │   [tool_v 117]   [lib_v 88]   [mat_v 12]                 │    │
│  │         ↓             ↓            ↓                     │    │
│  │         ╰─────────→ JOB #4213 ←──────╯                   │    │
│  │                       ↓                                  │    │
│  │             [workflow_run #11]    [approval #88]         │    │
│  │                       ↓                                  │    │
│  │                  (3 child jobs)                          │    │
│  │                                                          │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                  │
│  Footer: 12 nodes, 14 edges, 0 hidden by ACL, 2 by depth         │
│  [Download JSON]  [Download GraphML]  [Open as PDF report]       │
└──────────────────────────────────────────────────────────────────┘
```

**Cytoscape integration.** Cytoscape.js bundle (~1 MB minified) lands at
`matofx/ui/static/vendor/cytoscape.min.js` + `cytoscape-dagre.min.js` (the
layered-DAG layout extension). Both are MIT-licensed; both bundle into the
air-gap pack via the existing `tools/scripts/bundle_vendor.py` mechanism.
A new `obs.update_sbom_for_lineage()` hook injects them into the
`sbom.cdx.json` produced by `package` in `ci.yml`.

The page wires Cytoscape via `ui.html` (per the **always-pass-sanitize=False**
note in [[feedback_ui_html_sanitize]]). A small JS module
`matofx/ui/static/lineage.js` mounts the graph and bridges click events back to
NiceGUI via the existing `window.matofx.openDeepLink(path)` shim used by the
csv_plot renderer.

Layout: `dagre` (layered-DAG; engineering-typical) with
`rankDir: 'TB'` (top-to-bottom). Centre node fixed at vertical centre, upstream
above, downstream below. Edge labels render on hover only (keeps the visual
clean). Each node is keyboard-focusable; Enter activates its `deep_link`.

**Node styling** by `kind`:

| Kind | Shape | Colour role |
|---|---|---|
| `job` | rounded rectangle | primary |
| `workflow_run` / studies | hexagon | accent |
| `tool_version` | parallelogram | neutral |
| `library_file_version` / `material_version` / `surrogate_model_version` | rectangle (version tag) | neutral |
| `approval_request` | shield | warning |
| `user` | circle | muted |
| `scenario` / `project` | rounded rectangle, dashed border | muted |
| `vv_record` | rectangle with check-mark badge | success (ADR-0011) |

Archived nodes render at 50% opacity with a `(archived)` suffix in the label.

### 6.2 Tabular fallback view

The same `LineageGraph` rendered as one table per `NodeKind`, in the order the
BFS visited them. Each row hyperlinks to the node's `deep_link`. Above the
table, a per-edge-kind summary ("12 ran-tool-version edges, 3 consumed-material
edges, ..."). This is the **default view** when `soft_capped=True`.

Accessibility-wise, the tabular view is always available via the View toggle —
screen reader users should never be forced through a Cytoscape canvas. The
existing AG-Grid component renders the table.

### 6.3 Filter behaviour

- **Show only** toggles per-kind: hides nodes of that kind from rendering;
  edges that crossed them collapse to dashed "via N hidden" stubs.
- **Hide archived** toggles `include_archived` and triggers a fresh API call
  (cheap — the build cache is per-request).
- **Upstream / Downstream** sliders re-call the API.
- **View toggle** is purely client-side.

Filter state lives in URL query params (`?show=job,tool_version&upstream=4`) so
links are shareable and deep-links from the API export carry the same context.

---

## 7. Entry points

All inline tabs mount the same `LineageView` component (§6.1) against the host
object as the centre. No navigation away from the host page.

| From | UI change | File |
|---|---|---|
| Job page | New "Provenance" tab beside Outputs / Logs, mounts `LineageView(centre=(job, id))` | `ui/pages/job.py` |
| Workflow run page | New "Provenance" tab beside Children / Outputs | `ui/pages/workflow_run.py` |
| Parametric / Optimisation / Cohort detail | New "Provenance" tab in the studies explorer | `ui/pages/studies.py` |
| Library file detail | "Provenance" tab on the file-detail page; version-selector inside the tab when the file has multiple versions | `ui/pages/library.py` |
| Material detail | "Provenance" tab; version-selector inside | `ui/pages/materials.py` |
| Surrogate model detail (ADR-0008) | "Provenance" tab; version-selector inside | `ui/pages/surrogate.py` |
| Compare report | "Provenance" link in each column header that opens that job's `/admin/lineage/job/{id}` in a new tab (compare has no per-column tab room) | `matofx/reports/compare.py` per §16.3 column-header context |
| KPI table (ADR-0012) | Per-row "↗ provenance" icon → `/admin/lineage/job/{id}` (no inline real-estate in the row) | (lands with ADR-0012) |
| **Admin → Lineage** (sidebar) | Top-level admin nav entry; opens the standalone page with a centre-picker (kind dropdown + id search) | `ui/pages/admin_lineage.py` (new) |
| **Admin → Users → User detail** | "Activity provenance" link launches `/admin/lineage/user/{id}` (the only case the User centre kind appears in the UI) | `ui/pages/admin_users.py` |

Links to the standalone page use `/admin/lineage/{kind}/{id}`; inline tabs use
the host page's own URL with a `?tab=provenance` query param so the back-button
and deep-link semantics remain consistent with the host page.

---

## 8. ADR-0004 per-job HTML report integration

The per-job self-contained HTML report (ADR-0004) gains a new toggle on the
"Generate report" form: **"Include provenance graph"** (default off — keeps file
size predictable). When on:

1. The report builder calls `LineageService.build_graph(kind=JOB, id=…,
   actor=report_owner, upstream_depth=6, downstream_depth=3)`.
2. Renders the graph **server-side** to a static SVG via a headless Cytoscape
   render — the existing `matofx.reports.renderers.cytoscape_to_svg` helper
   (new, ~120 LOC, no JS runtime needed: uses `cytoscape-headless` Python
   wrapper or, more likely, a pre-computed `dagre`-laid-out coordinate set
   piped into a small Python SVG writer).
3. Inlines the SVG into the report's "Provenance" section, with a "Generated
   from …" attribution.

The SVG is bounded: at most `lineage_max_nodes` (default 500). If the graph
would exceed that, the report renders a tabular section instead (same content,
report-friendly typography).

No external assets, no JavaScript in the report file — ADR-0004's
**fully-inlined, no-network, no-script** guarantee is preserved.

---

## 9. Audit

Every lineage view writes a `lineage.viewed` event. The payload is minimal —
**not** the graph itself (would balloon audit storage):

```jsonc
{
  "event": "lineage.viewed",
  "actor_user_id": 17,
  "centre_kind": "job",
  "centre_id": 4213,
  "upstream_depth": 6,
  "downstream_depth": 3,
  "include_archived": true,
  "node_count": 12,
  "edge_count": 14,
  "hidden_by_acl": 0,
  "hidden_by_depth": 2,
  "surface": "tab" /* or "admin_page", "json", "graphml", "report" */
}
```

A denied view (403/404 from missing `lineage.view` or missing centre) writes
`lineage.view_denied` with `reason`. The `reports.include_provenance` path
writes its own `report.generated` event (existing ADR-0004 path); a *separate*
`lineage.viewed` is also written, distinguished by `surface='report'`. The
`surface` discriminator distinguishes inline-tab views (`tab`) from admin
standalone-page views (`admin_page`) so compliance audits can tell which
surface drove a given query.

---

## 10. Permission, capability, and General Settings

### 10.1 Permission slug

```python
# matofx/users/permissions.py
LINEAGE_VIEW = "lineage.view"
```

Default grants:

- `super_user` — implicit via catalog sentinel (no role-permission row needed).
- `administrator` — seeded by migration `0052_lineage_permission.py`.
- Any other role — granted via the existing role editor (ADR-0005 base-role +
  capability mechanics; lineage is base-role-grantable, **not** capability-only).

### 10.2 No new capability role

The brief explicitly endorsed compliance / audit personas needing lineage. Two
options were on the table when ruling ADR-0010: (a) a dedicated `compliance`
capability role that bundles `lineage.view` + `audit.view`, or (b) plain
permission. Decision (per ADR-0010 §3, decision (a)): **plain permission**.
Customers granular-grant via the existing role editor. A `compliance`
capability bundle is a cheap additive follow-on if customer signal warrants.

### 10.3 General Settings

`lineage_max_nodes` (int, default `500`) lands in the **Data governance**
section of `GENERAL_SETTINGS_CATALOG`. Per
[[feedback_general_settings_two_places]], it needs three edits:
catalog, SECTIONS tuple, hard-coded count in
`test_general_settings_router.py`. The CHANGELOG entry for the lineage slice
documents all three.

---

## 11. Phased build sequence

Each slice is independently testable. Gates between slices: `ruff format
--check . && ruff check . && mypy && pytest`, both DB engines.

**Slice 1 — Service skeleton + node/edge dataclasses + the `job` resolver.**
Lands `matofx/lineage/__init__.py` + `service.py` + `errors.py` + `types.py`.
Unit tests over `_resolve_job` upstream-only (ACL allow + deny paths, the
empty-graph case, the cycle-guard). No API, no UI.

**Slice 2 — Remaining resolvers and the full DAG walk.**
All other kinds; the BFS with shared cache; soft-cap behaviour. Unit tests per
kind in isolation + at least four cross-kind integration tests using the
existing test fixtures (heat-sink job through the heat-transfer workflow with a
library file and a material).

**Slice 3 — API + permission migration.**
`GET /api/lineage/{kind}/{id}` and `.../export`; the `0052_lineage_permission`
migration; the `lineage.view` permission seed test. Plain-English HTTP errors;
audit hookup; serialiser unit tests for both JSON and GraphML.

**Slice 4 — `LineageView` component + admin standalone page.**
The shared `LineageView` component (`ui/components/lineage_view.py`) with
Cytoscape.js + dagre layout; bundled vendor JS; filter row; tabular fallback.
The admin standalone page (`ui/pages/admin_lineage.py`) at
`/admin/lineage/{kind}/{id}` that mounts the component inside the admin shell
with a centre-picker (kind dropdown + id search) for direct navigation.
Visual playwright test for the layered DAG render; keyboard navigation tests;
URL-state round-trip test. Uses the
[[feedback_nicegui_async_click_chain]] async-click convention end-to-end.
The component is mountable standalone for tests — no host-page coupling.

**Slice 5 — Inline tab mounts.**
"Provenance" tab added to job / workflow_run / studies / library / material /
surrogate detail pages, each mounting `LineageView(centre=(host_kind, host_id))`
inside their existing tab strip. The Compare column header + the future
KPI-table row get "↗" link-out icons to the admin standalone page. The
admin Users page gets the "Activity provenance" link. Pure wiring; no new
server logic. Per-host-page playwright test that the tab activates, the
graph mounts, and the URL carries `?tab=provenance` for round-trip.

**Slice 6 — Reports integration (ADR-0004).**
`include_provenance` toggle + server-side SVG renderer + bounded fallback to
tabular section. Snapshot tests against three sample graph shapes.

**Slice 7 — Polish: General Setting, performance pass, docs.**
`lineage_max_nodes` setting + consumer; bulk-resolve helpers on each service;
ARCHITECTURE.md §16.22 (new); package README; ROADMAP.md tick; deferral-row
flip on the "Data lineage / provenance graph" row.

---

## 12. Test plan

**Per-resolver tests (slice 1–2).** For every `NodeKind`:

- Hits cache on second call within a build.
- ACL deny → node absent, `hidden_by_acl++`, edge `truncated_count++`.
- Archived node included by default; excluded under `include_archived=False`.
- Unknown id → `LineageNotFoundError` only at the *centre*; mid-walk unknowns
  drop with a log warning (FK should make this impossible, so the warning is a
  drift indicator).

**Walk tests.** For each centre kind, verify:

- A simple job (no workflow, no library, no material, no study, no approval)
  produces exactly `{job → tool_version, job → user, job → scenario → project}`.
- A workflow-child job exposes `belongs_to_workflow_run` upstream; the workflow
  run exposes `seeded_by_study` if it ran inside a parametric study.
- A study-seeded job exposes `seeded_by_study` and not `belongs_to_scenario`
  (per the existing XOR check on `Job`).
- A job using two library files and three materials produces five `consumed_*`
  edges with field names populated.
- A surrogate-tool job exposes `ran_surrogate_version` and the surrogate's own
  `derived_from` to its training source.
- An approval-gated job exposes `gated_by` with pre/post discriminator.

**Performance tests.**

- A synthetic graph of 1000 nodes builds in < 500 ms on the CI Linux runner.
- The soft-cap path triggers cleanly at 500 nodes; `soft_capped=True`; no DB
  query past the boundary.
- A library file referenced by 5000 jobs returns only `limit=50` consumers per
  the helper's contract.

**API tests.**

- Permission gate denies 403 + audits `lineage.view_denied`.
- Depth out of range → 422.
- Both serialisation formats round-trip identical node + edge sets.
- GraphML loads cleanly into Cytoscape Desktop (manual smoke; not automated).

**UI tests.**

- Cytoscape mounts and renders the layered DAG.
- Click on a node navigates to the deep link.
- View toggle switches graph ↔ table without re-fetching.
- Filter toggles re-fetch with new query params; URL state survives reload.
- Tabular fallback renders identical content; row hyperlinks work.

**Report integration tests.**

- `include_provenance=true` inlines a valid SVG inside the report HTML.
- The report file has zero external network references (existing ADR-0004
  invariant — re-asserted).
- Graphs over the soft cap fall back to tabular section in the report.

**Audit tests.**

- One `lineage.viewed` per ui / json / graphml / report build.
- Payload carries node + edge counts but never raw graph data.

---

## 13. Open / deferred (recorded, not silent)

- **PLM / CAD-revision linkage.** Connecting MATOFX-internal lineage to external
  PLM (Windchill, Teamcenter, 3DEXPERIENCE). The brief calls this out as v1.x+;
  not in scope here.
- **Time-travel queries.** "Show the lineage as it was on 2025-12-01." The
  underlying data exists (`audit` is hash-chained), but the surface — re-running
  every ACL predicate against historical role grants, materialising deleted
  nodes from audit trail — is a much larger build. Defer.
- **Lineage diff.** "What's different between job A's provenance and job B's?"
  Powerful for regression triage; out of scope for v1.0. The export API
  supports this externally (diff two saved JSON files in a separate tool).
- **Saved / pre-canned lineage queries.** "All jobs that consumed material
  version X." The export API + GraphML covers ad-hoc external query; in-product
  saved queries land in v1.x.
- **Cross-install federated lineage.** A single graph spanning Staging +
  Production. Out of scope — single-install only for v1.0.
- **The "via N hidden" stub UX.** First-pass renders these as dashed edges with
  a tooltip; finer UX iteration is one of the polish bullets if customer signal
  arrives.
- **Headless Cytoscape rendering for reports.** Slice 6 picks between
  (a) writing a small dagre layout + SVG writer in pure Python (no JS runtime;
  ~150 LOC), or (b) shelling out to `node` with a tiny Cytoscape script. Final
  choice in the slice itself; the design prefers (a) for air-gap simplicity
  and zero new system dependencies.
- **`vv_record` node + edge.** Reserved here; lands with ADR-0011. The enum
  member ships in slice 2 with a no-op resolver until ADR-0011 wires it up.

---

## Revision history

- **2026-05-26** — initial draft locked alongside ADR-0010 (architect ruling).
  Cytoscape.js + dagre selected as the rendering library; no new tables; one
  migration (`0052_lineage_permission`); one new permission (`lineage.view`);
  one new General Setting (`lineage_max_nodes`). Slice plan and per-slice gates
  agreed.
- **2026-05-26** — UI placement revised: rendering moves from a top-level
  dashboard page to a shared `LineageView` component mounted inline as a
  "Provenance" tab on each host page. The standalone page survives only in the
  **admin space** at `/admin/lineage/{kind}/{id}` — covering centres without a
  natural host (admin user-activity view), canonical deep-link URLs for the
  export API + report links, and audit/compliance share-by-URL contexts. Slice
  4 now builds the component + admin page; slice 5 wires the inline tabs.
  Audit `surface` discriminator added to distinguish tab vs admin-page views.
