# 0013. Workflow structure graph — descriptor-inferred process flow

Date: 2026-05-27
Status: Accepted (architect ruling 2026-05-27 — agreed in the same Phase 2 expansion discussion that produced ADR-0010; scoped as a follow-up because lineage + workflow-graph share the Cytoscape canvas plumbing but model different data).

## Context

MATOFX workflows are ordered groupings of tools. The shape of a
workflow — which tool's output feeds which tool's input — is **not**
stored explicitly; it is **inferred** from each member tool's
descriptor:

- Forward edges from `descriptor.downstream_tools` ("when I run, these
  tools naturally come next").
- Backward edges from each input field that declares
  `downstream_input=True` + `upstream_tool="<alias>"` ("this field is
  fed by some other tool's output").

The pure-Python helper that computes this — `workflows.ordering._infer_edges`
— is already in production, called by `WorkflowService.create_custom` /
`WorkflowService.update_custom` to **order** the chosen tools when a
custom workflow is saved. The wiring is correct; what users can't do
today is **see** the wiring without reading descriptor JSON.

Four surfaces need it:

1. **`/workflow-runs/{id}`** — a user looking at an in-flight run wants
   to see the dependency map at a glance, with each node tagged with
   its current state (ready / waiting / queued / running / complete /
   failed). The per-tool tab strip carries the same information one tab
   at a time; the graph compresses it into a single picture.
2. **Admin → New / edit Workflow** — an admin building a workflow
   needs to confirm "did I get the tool order right?" without driving a
   scenario.
3. **Admin → Custom Workflows inspector** — operator
   troubleshooting a custom workflow needs to see what the owner built.
4. **My Workflows editor** — a user editing their own custom workflow
   wants to see the structure their tool choices produce.

ADR-0010 already shipped the Cytoscape.js + dagre canvas
(`matofx.ui._cytoscape`) for the lineage view. The refactor that
extracted it as a shared module ran first (so neither view's evolution
drags the other along). This ADR finishes the build on top of that
groundwork.

## Decision

Add a **read-only workflow-structure graph** as a thin projection over
existing descriptor data + the existing edge-inference helper. **No new
database tables**; one new service module
(`matofx/workflows/graph_service.py`); one new API endpoint;
**no new permissions** (workflow visibility is already
gated end-to-end by the existing `WorkflowService.list_visible` /
`list_for_user` predicates plus the
`CUSTOM_WORKFLOWS_INSPECT` permission for the admin path). Build-level
spec lands as `designs/workflow-graph.md`.

**1. Service.** `WorkflowGraphService.build(workflow_id, actor_user_id,
run_id=None)` returns a frozen `WorkflowGraph` dataclass —
`(workflow_id, alias, name, version, state, nodes, edges, run_id,
run_state)`. Nodes are `WorkflowGraphNode` rows (tool_alias, position,
tool_state, archived, deep_link, optional `run_state`, optional
`child_job_id`, payload). Edges are `WorkflowGraphEdge` rows
(source / target tool aliases, `field_labels` tuple of human-facing
descriptor labels rendered one-per-line above the arrow). Reuses
`workflows.ordering.infer_edges_with_fields` (extracted from the
existing `_infer_edges`); the existing single-source-of-truth wiring
detector is now used by both ordering and graph layers.

**2. Visibility gate.** Per-workflow predicate combining the per-row
equivalents of `WorkflowService.list_visible` (admin workflows: state
+ team gate + new-tool cascade) ∪ `list_for_user` (custom workflows:
owner / shared / `CUSTOM_WORKFLOWS_INSPECT`). Failure existence-hides
via `WorkflowNotFoundError` → 404, matching the rest of the workflow
surface. When `run_id` is supplied, the run's scenario must pass VIEW
on the owning project, and the run's `workflow_id` must match — a run
belonging to workflow A cannot be smuggled onto workflow B's graph to
read foreign child-job states (`NotFoundError` → 404 on mismatch).

**3. Runtime overlay vocabulary.** Per-node `run_state` is a closed
6-member enum: `ready` / `waiting` / `queued` / `running` /
`complete` / `failed`. Mirrors the workflow-run page's tab strip
(`ui.pages.workflow_run._tab_status_for`) so the badge on the graph
node and the colour on the tab read the same word. The `ready` vs
`waiting` distinction (both backed by `created` child state)
reproduces `_all_upstream_ready` — an upstream that isn't part of the
workflow falls back to free-entry and doesn't block readiness.

**4. Standalone tools render as floating nodes.** A workflow member
whose descriptor declares no wiring to another chosen member has no
incident edges. Dagre's natural layout drops disconnected nodes onto
their own rank — no special-casing in the renderer.

**5. Floating preview, not in-process editor.** The graph is the
**output** of saved workflow state; it does not let the user re-arrange
edges. Edges come from descriptors, which come from tool installs;
re-wiring belongs to the tool author, not the workflow editor. The
"live preview while picking members" surface (My Workflows editor)
shows the graph for the **most-recently-saved** state — saving + re-
opening refreshes it.

**6. HTTP API surface.** One new endpoint:
`GET /api/workflows/{workflow_id}/graph?run_id=`. Returns
`WorkflowGraphResponse` (workflow header + nodes + edges + optional
run overlay). No export endpoint in v1 — the lineage view's JSON /
GraphML export is for compliance evidence; the workflow graph is for
operator + author review, and ad-hoc external export hasn't been asked
for. Defer to v1.x if it's wanted later.

**7. UI.** One new component (`matofx.ui.workflow_graph_view`) — a
shared shell consuming `_cytoscape.render_cytoscape_canvas`. Workflow-
specific stylesheet: `LR` rank direction (workflows read left-to-right;
lineage is `TB`), per-state node fill matching the run-page tab
colours, archived nodes dimmed. Run-mode auto-polls
`/api/workflows/{id}/graph` every 3 seconds until every node reaches a
terminal state (`complete` / `failed`); structure-only mode doesn't
poll. The shared `render_workflow_graph_section` mounts the graph
inside a collapsible `ui.expansion` for surfaces where the graph isn't
the primary content — mirrors `render_provenance_section` from ADR-0010.

**8. Surface integration.**

- **`/workflow-runs/{id}`** — a "Workflow" tab is appended after the
  per-tool tabs. Clicking flips a `view_kind` flag in the page state;
  the tab body switches to the graph view with run overlay.
- **Admin → New / edit Workflow** — when editing (not creating), a
  collapsible "Structure preview" section sits below the tool list,
  loaded lazily once the workflow id is known.
- **Admin → Custom Workflows inspector** — the per-workflow drawer
  gains a "Structure preview" expansion below the members list.
- **My Workflows editor** — the edit dialog (not create) gains the
  same "Structure preview" expansion below the tools picker.

**9. No new permission.** Workflow visibility is already comprehensive
— the graph endpoint reuses the visibility predicate the rest of the
workflow surface uses. There is no "see graphs but not workflows"
edge case; the graph is a projection of data the user already sees.

**10. Audit.** Every `build` writes a `workflows.graph.viewed` event
with `workflow_alias` / `node_count` / `edge_count` / optional
`run_id` in the payload. Matches the lineage view's audit pattern.

## Alternatives considered

- **One unified service** — `LineageService` resolves a
  `WorkflowGraph` node by walking workflow members. Rejected: lineage
  models recorded provenance; the workflow graph models declared
  structure. The two answer different questions ("what fed this row?"
  vs "what would feed what?") and overloading one service for both
  would shape neither cleanly.
- **Live preview from un-saved tool picks** — initially rejected
  for v1 ("save-and-reopen is enough"). **Reversed 2026-05-27** after
  first user feedback flagged the gap: the create dialog couldn't
  show *any* wiring before the first save, and "preview after save"
  asked the user to commit a half-finished decision just to see the
  shape. The reversal shipped as a new `POST /api/workflows/graph/preview`
  endpoint (body `{tool_aliases: [...]}`) plus a
  "Preview structure" button on the create / edit dialogs. **On-demand
  click**, not debounced auto-refresh — cheaper UI, no per-keystroke
  API churn, and the user explicitly asked for the simpler variant.
- **Edit-on-the-graph** — let the admin drag-rewire the graph in the
  edit dialog. Rejected as out of scope. Edges come from tool
  descriptors; rewiring on a workflow surface would either fork the
  descriptor (huge) or be ignored on next save (confusing).
- **Per-node JSON / GraphML export** — defer to v1.x. The graph is for
  human review, not for compliance evidence; the lineage view covers
  that use case for the recorded artefacts.

## Consequences

- One new module under `matofx.workflows/` (~250 LOC service + types
  + ~360 LOC UI view), one new API endpoint, one new ApiClient method.
  No new tables, no new permissions, no new migrations.
- The existing `WorkflowService` is untouched; `graph_service.py`
  composes it via the shared visibility predicate rather than calling
  it directly.
- `workflows.ordering._infer_edges` becomes a thin wrapper over the
  new public `infer_edges_with_fields`. The single-source-of-truth
  wiring detector is now reused by ordering and graph layers; future
  edge-kind additions land in one place.
- Cytoscape's CDN strategy (slice 4b of ADR-0010) is reused as-is —
  the new view doesn't pay for an additional load.
- Workflow graphs render correctly only when the caller can see the
  workflow. Existence-hide via 404 matches the rest of the surface.
- The /workflow-runs/{id} page gains a Workflow tab. The page's
  signature-driven re-render gate is updated to include `view_kind` so
  flipping to the graph + back doesn't wipe the user's in-progress
  form input on the next poll.
