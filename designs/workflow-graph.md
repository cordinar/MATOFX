# Design — Workflow structure graph

Status: **Shipped 2026-05-27** (Phase 2 expansion follow-up to ADR-0010).
Decision record: `decisions/0013-workflow-graph.md`.
Cross-ADRs: `0010` (data lineage — shares the Cytoscape canvas).
Brief sections: `PROJECT_BRIEF.md` §I46 (must-ship list).
Delivery tracking: `ROADMAP.md` Phase 2.

This is the build-level spec. The ADR records *why*; this records *what to build*.
Pure read-side; no new tables; no new permissions.

---

## 1. Scope at a glance

| Capability | v1 | Deferred |
|---|---|---|
| `WorkflowGraphService.build` over an existing workflow's descriptors | ✅ | |
| Run-overlay (`run_id=`): per-node `ready` / `waiting` / `queued` / `running` / `complete` / `failed` | ✅ | |
| Visibility intersect with the existing workflow surface (admin: state+team+cascade; custom: owner / shared / inspector) | ✅ | |
| `GET /api/workflows/{id}/graph?run_id=` JSON | ✅ | |
| Shared `render_workflow_graph` UI component (Cytoscape.js + dagre, `LR` layout) | ✅ | |
| Lazy `render_workflow_graph_section` expansion for embedded surfaces | ✅ | |
| Floating-node rendering for standalone tools (no descriptor wiring within the workflow) | ✅ | |
| Per-edge field-name tooltip (downstream input fields that declared the wiring) | ✅ | |
| Surface wiring: `/workflow-runs/{id}` tab + admin edit + custom inspector + my-workflows editor | ✅ | |
| `workflows.graph.viewed` audit event | ✅ | |
| Auto-polling in run mode until every node reaches a terminal state | ✅ | |
| Live preview from un-saved tool picks (POST /api/workflows/graph/preview + button UI) | ✅ | |
| JSON / GraphML export (operator-driven) | | ✅ v1.x |
| Drag-rewire from the graph (edit-on-canvas) | | Out of scope |
| Cross-workflow merge view (multiple workflows on one canvas) | | Out of scope |

The whole feature is **read-only**. Edges come from tool descriptors; the workflow editor cannot mutate them.

---

## 2. Data model + migration

**No new tables, no new permissions, no new migration.** The graph is a
projection over:

- `workflow` + `workflow_member` (workflow rows + ordered membership).
- `tool` + `tool.descriptor` (member tools and their wiring declarations).
- `workflow_run` + `job` (only when `run_id=` is supplied — used to
  resolve per-node `run_state` and `child_job_id`).

The migration head is unchanged from ADR-0010's `0052_lineage_permission`.

---

## 3. Node + edge taxonomy

| Node field | Notes |
|---|---|
| `tool_alias` | Unique inside a workflow (`uq_workflow_member_workflow_id_tool_alias`); used as the Cytoscape DOM id. |
| `label` | `f"{position}. {tool.name}"` — matches the run-page tab strip. |
| `position` | 1-based, dense per workflow. |
| `tool_state` | `"new"` / `"test"` / `"released"` / `"archived"` at build time. |
| `archived` | Derived from `tool.archived_at`. |
| `deep_link` | `/admin/tools/{alias}` in structure mode; `/jobs/{child_job_id}` in run mode. |
| `run_state` | Closed enum (see §4). `None` outside run mode. |
| `child_job_id` | The workflow-run child Job this node represents. `None` outside run mode. |
| `payload` | `{tool_name, tool_version}` — opaque extras for the UI. |

| Edge field | Notes |
|---|---|
| `source` | Upstream tool alias. |
| `target` | Downstream tool alias. |
| `field_labels` | Sorted tuple of the descriptor `label` text for each downstream input field that declared this wiring (falls back to the wire `name` if no label set). The UI renders these one-per-line above the arrow. Empty for forward-only edges. |

The inference rule (`workflows.ordering.infer_edges_with_fields`):

- **Forward** — for each member, add an edge to every alias in
  `descriptor.downstream_tools` that is also in the chosen set. No
  field name attached.
- **Backward** — for each member, for each `inputs[i]` with
  `downstream_input=True`, add an edge from `inputs[i].upstream_tool`
  to this member if the upstream is also in the chosen set. The input
  field name is attached.

The two signals union; duplicates collapse into a single edge with
the field names collected.

---

## 4. Per-node `run_state` vocabulary

Closed 6-member enum (`matofx.workflows.graph_types.WorkflowNodeRunState`).
Mirrors `ui.pages.workflow_run._tab_status_for`:

| `run_state` | Child Job state | Notes |
|---|---|---|
| `complete` | `complete` | Terminal. |
| `failed` | `failed` | Terminal — workflow won't progress until re-submitted. |
| `running` | `running` | In flight. |
| `queued` | `queued` | Submitted to the runner. |
| `ready` | `created` + every workflow-internal upstream complete | User can submit. |
| `waiting` | `created` + at least one workflow-internal upstream not complete | Locked until upstream finishes. |

Upstreams that aren't part of *this* workflow are treated as free-entry
fields (matches `_all_upstream_ready` in the run page).

---

## 5. Service surface

```python
@dataclass(frozen=True)
class WorkflowGraphService:
    audit: AuditWriter

    async def build(
        self,
        session,
        *,
        workflow_id: int,
        actor_user_id: int,
        run_id: int | None = None,
    ) -> WorkflowGraph: ...
```

Errors:

- `WorkflowNotFoundError` (→ 404) — workflow doesn't exist, or
  workflow exists but isn't visible to the caller.
- `NotFoundError` (→ 404) — `run_id` supplied but the run doesn't
  exist / belongs to a different workflow / sits on a project the
  caller can't VIEW. The cross-workflow case is an explicit safety
  check; it prevents reading foreign child-job states by passing an
  unrelated `run_id`.

Visibility predicate (`_user_can_see_workflow` in `graph_service.py`):

- **Admin workflows** — state gate (`user_can_see_state`) ∧ new-tool
  cascade (`_workflow_contains_new_tool` ⇒ requires
  `TOOLS_SEE_NEW_STATE`) ∧ team gate (`World` ⇒ any; otherwise the
  caller must be in at least one of the workflow's teams).
- **Custom workflows** — `owner_id == actor` ∨ row in `workflow_share`
  for the actor ∨ actor holds `CUSTOM_WORKFLOWS_INSPECT`.

Audit: every build writes a `workflows.graph.viewed` event with the
workflow alias, node + edge counts, and the resolved `run_id`.

---

## 6. HTTP API

Two endpoints:

```
GET /api/workflows/{workflow_id}/graph?run_id=
→ WorkflowGraphResponse                          (saved workflow)

POST /api/workflows/graph/preview
  body: {tool_aliases: [...]}
→ WorkflowGraphResponse  (workflow_id=None)       (unsaved tool picks)
```

The preview endpoint exists so the create / edit dialogs can render
the descriptor-inferred wiring before the user commits the workflow.
Body validation: `tool_aliases` is `min_length=1, max_length=64` (well
above the custom-workflow ceiling so admin-workflow previews fit too).
Errors:

- **400** — unknown alias, hidden alias (existence-hidden as "unknown"
  per `_assert_member_tools_visible` semantics), or duplicate alias.

Audit: `workflows.graph.preview` with the alias list + counts.

### Saved-workflow endpoint response

| Field | Type | Notes |
|---|---|---|
| `workflow_id` | int | The graphed workflow's id. |
| `workflow_alias` | str | Stable alias (custom-workflow aliases are synthesised; see ADR-0010 §3.4 cross-ref). |
| `workflow_name` / `workflow_version` / `workflow_state` | str | Page-header fields. |
| `nodes` | list[`WorkflowGraphNodeResponse`] | One per member in position order. |
| `edges` | list[`WorkflowGraphEdgeResponse`] | Inferred wiring, sorted by `(source, target)` for stability. |
| `run_id` | int \| null | Echoes the request when supplied. |
| `run_state` | str \| null | `compute_state(run, children)` rollup when run mode. |

All endpoints gate on the existing user-session auth + the
visibility predicate above. No new permission.

ApiClient: `ApiClient.workflow_graph(workflow_id, run_id=None)`.

---

## 7. UI

`matofx.ui.workflow_graph_view`:

- **`render_workflow_graph(container, *, workflow_id, run_id=None,
  rank_dir="LR", height_px=480, show_header=True, on_navigate=None)`** —
  the primary entry point. Clears + repopulates `container` with an
  optional header, the Cytoscape canvas, and a footer with node/edge
  counts. Polls `/api/workflows/{id}/graph` every 3 seconds when
  `run_id` is set, until every node reaches a terminal `run_state`.
- **`render_workflow_graph_section(*, workflow_id, run_id=None,
  label="Structure preview", initially_open=False, height_px=360)`** —
  collapsible-expansion wrapper for surfaces where the graph isn't
  the primary content (admin edit dialog, my-workflows edit dialog,
  custom-workflow inspector drawer). Lazy: the graph fetch only fires
  when the user opens the expansion.

**Stylesheet** (`_workflow_stylesheet`):

- Default node — round-rectangle, muted grey background.
- Run-state overrides — per-state Material colours matching the run-page
  tab strip (`ready: primary blue`, `waiting: grey`, `queued: info`,
  `running: warning amber`, `complete: positive green`,
  `failed: negative red`).
- Archived nodes — opacity 0.5.
- Edges — directional bezier with the field-name list as a small
  rotated label along the edge.

**Click-through** (cytoscape `tap` handler) routes to the node's
`deep_link` — `/admin/tools/{alias}` in structure mode,
`/jobs/{child_job_id}` in run mode. The standard browser-nav path,
not an in-page intercept (matches lineage view; see ADR-0010 §6.1
on the JS bridge limitation).

**Polling stops automatically** once `_all_nodes_terminal(graph)` is
true — empty graphs, structure-only graphs, and runs where every
node is `complete`/`failed`.

---

## 8. Surface integration

### 8.1 `/workflow-runs/{id}` (slice 2)

`ui.pages.workflow_run.render_workflow_run_workspace` gains a
`view_kind` field in its state dict (default `"tool"`, set to
`"graph"` when the new Workflow tab is clicked).

The tab strip renders the per-tool tabs unchanged, then appends a
final "Workflow" button (icon `account_tree`). Clicking it calls
`_on_graph_tab_click` which flips `view_kind` and forces a re-render
without URL navigation (the Workflow tab is a view switch, not a
job switch).

`_render_active_tab` branches on `view_kind`:

- `"tool"` (default) — the existing per-child form / status / job
  embedding.
- `"graph"` — calls `_render_graph_body` which lazy-mounts
  `render_workflow_graph(workflow_id=run.workflow_id, run_id=run.id,
  rank_dir="LR", show_header=False)`. The page header already names
  the workflow, so the canvas's own header is suppressed.

The signature gate (`_compute_signature`) is extended to include
`view_kind`, so poll-driven re-renders preserve the Workflow-tab
selection.

### 8.2 Admin → New / edit Workflow (slices 3 + 5)

`ui.pages.admin_workflows._open_workflow_dialog` mounts
`render_workflow_graph_preview_button` below the tool-picker row,
reading `state["tool_aliases"]` (the dialog's working selection) at
click time. Works on both create (`existing is None`) and edit —
the preview endpoint is ephemeral and doesn't need a saved workflow
row. The user clicks again after adding / removing tools to refresh
the rendering.

Saved-state graph (the lazy `render_workflow_graph_section`) is
*not* mounted in this dialog — once the workflow is saved the
preview button continues to work, and the user can also see the
graph in the inspector / on a run page. Avoiding the dual-mount
keeps the dialog simple and removes the create-vs-edit branching
from the layout.

### 8.3 Admin → Custom Workflows inspector (slice 3)

`ui.pages.admin_custom_workflows._open_details_drawer` — between the
"Forked from" line and the archived-banner block, the drawer
mounts `render_workflow_graph_section(workflow_id=workflow_id)`. Always
present (the drawer only opens for existing rows).

### 8.4 My Workflows editor (slices 3 + 5)

`ui.pages.my_workflows._open_workflow_dialog` mounts
`render_workflow_graph_preview_button` between the tools picker and
the share list, reading `tools_select.value` at click time. Works on
both create and edit. Same on-demand semantics as the admin path —
one click per refresh; no debounce, no per-keystroke API churn.

---

## 9. Audit

`build` writes one `workflows.graph.viewed` row per call:

```json
{
  "actor": "user:42",
  "actor_type": "user",
  "action": "workflows.graph.viewed",
  "resource_type": "workflow",
  "resource_id": "17",
  "outcome": "success",
  "payload": {
    "workflow_alias": "thermal_chain",
    "node_count": 3,
    "edge_count": 2,
    "run_id": null
  }
}
```

No payload-size cap is needed — the counts are small ints and the
alias is bounded by the `workflow.alias` 64-char column. Matches the
lineage view's audit shape.

---

## 10. Tests

Service tests (`tests/workflows/test_graph_service.py`):

- `test_build_structure_only_chain` — A → B → C chain, asserts node
  order, edge identities (sorted), structure-mode `deep_link`, no
  `run_state`.
- `test_build_per_field_edge_names` — `downstream_input` declaration
  populates `field_labels` on the matching edge (the descriptor
  `label`, not the wire `name`).
- `test_build_with_floating_node` — Two unrelated members → both
  nodes present, edges empty.
- `test_build_hides_unowned_private_custom_workflow` — Owner sees
  the graph; another regular user gets `WorkflowNotFoundError`
  (existence-hide).
- `test_build_with_run_id_overlays_states` — Mixed-state run; per-node
  `run_state` mapping correct; `compute_state` rollup matches; run-mode
  `deep_link` points at `/jobs/{child_job_id}`.
- `test_build_with_run_id_waiting_when_upstream_not_complete` — Upstream
  `running`, downstream `created` → downstream is `waiting`.
- `test_build_with_run_id_for_different_workflow_existence_hides` —
  run_id from workflow A on a build for workflow B → `NotFoundError`.

API tests (`tests/api/test_workflow_graph_api.py`):

- `test_graph_endpoint_requires_auth` — anonymous → 401.
- `test_graph_endpoint_unknown_workflow_is_404` — bad id → 404.
- `test_graph_endpoint_returns_nodes_and_edges_for_visible_workflow` —
  create custom workflow → graph endpoint returns matching shape.

Plus the existing `tests/workflows/test_ordering.py` continues to
cover the underlying `_infer_edges` (now via `infer_edges_with_fields`).

---

## 11. Key files

- `src/matofx/workflows/graph_types.py` — `WorkflowGraph` /
  `WorkflowGraphNode` / `WorkflowGraphEdge` / `WorkflowNodeRunState`.
- `src/matofx/workflows/graph_service.py` — `WorkflowGraphService.build`
  + `_user_can_see_workflow` + per-node run-state resolver.
- `src/matofx/workflows/ordering.py` — `infer_edges_with_fields` (new
  public helper); `_infer_edges` now wraps it.
- `src/matofx/workflows/__init__.py` — package exports.
- `src/matofx/api/schemas.py` — `WorkflowGraph{Node,Edge,Response}`.
- `src/matofx/api/deps.py` — `get_workflow_graph_service`.
- `src/matofx/api/routers/workflows.py` — `GET /workflows/{id}/graph`.
- `src/matofx/ui/_cytoscape.py` — shared canvas (slice 4b of ADR-0010,
  used as-is).
- `src/matofx/ui/workflow_graph_view.py` — `render_workflow_graph` +
  `render_workflow_graph_section`.
- `src/matofx/ui/api_client.py` — `ApiClient.workflow_graph`.
- `src/matofx/ui/pages/workflow_run.py` — Workflow tab + `view_kind`
  branching.
- `src/matofx/ui/pages/admin_workflows.py` — structure preview in
  edit dialog.
- `src/matofx/ui/pages/admin_custom_workflows.py` — structure preview
  in inspector drawer.
- `src/matofx/ui/pages/my_workflows.py` — structure preview in edit
  dialog.
- `tests/workflows/test_graph_service.py` — service tests.
- `tests/api/test_workflow_graph_api.py` — API smoke tests.
