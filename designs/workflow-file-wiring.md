# Design — Workflow file-wiring (pass result files between chained members)

Status: **SHIPPED 2026-06-20.** All v1 scope landed (ARCHITECTURE §16.4 file-wiring note).
Decision record: **ADR-0015** (this spec is the build record).
Builds on: `designs/workflow-graph.md` + `decisions/0013-workflow-graph.md` (the descriptor wiring convention + edge inference), `decisions/0008-surrogate-family.md` §4.4 (field-output retention floor), `decisions/0006-file-library.md` (governed file inputs), `decisions/0009-optimisation-framework.md` §3.4 (workflow-target optimisation — open vs wired inputs).
Drives: the NAFEMS demo chain `Composite Laminate → Thermal Soak → Bracket Stress`, where Thermal Soak's **nodal temperature field** must reach Bracket Stress as its thermal load.

This is the build-level spec. The ADR will record *why*; this records *what to build*.

---

## 1. Problem

The native workflow wiring (`matofx.workflows.wiring`) transfers a downstream member's wired inputs from its upstream members' outputs by **name match** (`downstream_input=true` + `upstream_tool="<alias>"`; the downstream input named `X` receives upstream output `X`). But the runtime transfer (`WorkflowRunService.advance` → `_read_member_outputs` → `resolve_wired_values`) reads only **scalar values from `outputs.json`**. A **file** output (a mesh, a field `.vtp`, a CSV, an image) cannot ride the wiring — so a downstream solver cannot consume an upstream solver's result file.

The requirement (user, 2026-06-18): the user must **never hand-build an orchestration file** — MATOFX moves the bytes — and the mechanism must be **general** (any file output → any like-named wired file input, every tool, not demo-specific).

---

## 2. Key insight — this is an *automatic* "previous-job pick"

MATOFX already stages a tool's file input from several sources, resolved at queue time in `JobService.queue_for_runner` via `matofx.tools.exec.uploads` + `staging`:

- a drag-and-drop **upload** (draft area → `inputs/uploaded/<field>/`),
- a **typed path**,
- a **previous-job pick** (a file produced by an earlier job),
- a governed **library file** ref (ADR-0006).

A **previous-job pick** is already "use file `F` produced by job `J` as this file input." Workflow file-wiring is exactly that, *chosen automatically by the name-match wiring convention instead of by the user*. So the feature is not a new subsystem — it is a new **resolver** that turns a wired file input into the same shape the previous-job-pick path already consumes, and lets the existing staging copy do the work.

Two seams already do the right thing **type-agnostically** and need **no change**:

- **Graph / UI edges** — `ordering.infer_edges_with_fields` draws the edge from any `downstream_input`+`upstream_tool` name match regardless of value type. The New Workflow UI already shows the file edge.
- **Optimisation factor exclusion** — `wiring.wired_inputs` excludes any wired input from the factor pool regardless of type; `studies.factors` also rejects file inputs as factors outright. File-wired inputs are correctly never search factors.

---

## 3. Scope at a glance

| Capability | v1 | Deferred |
|---|---|---|
| Wired **scalar** transfer (today) | ✅ (unchanged) | |
| Wired **file** transfer — single file output → like-named file input | ✅ | |
| Wired **field series** (`field_timesteps` glob) — stage the whole series, value = primary file | ✅ | |
| Reuse existing staging copy (sanitised, traversal-safe, shared-mount) | ✅ | |
| Standalone tool unaffected (same field is an upload/library/pick widget outside a workflow) | ✅ | |
| Missing-upstream-file → friendly "required input missing" (no crash) | ✅ | |
| Lineage edge for the file handoff (produced-by / consumed) | | v1.x |
| Visual distinction of file edges in the graph (vs scalar edges) | | v1.x |
| **Cross-mesh remap** of a wired field onto a different downstream mesh | | v1.x (the ADR-0008 §4.4 `field_remap_reference_mesh` path) |
| Per-handoff size cap surfaced as an operator setting | | v1.x (rely on disk/retention budgeting for v1) |

**Compatibility is the tools' concern**, exactly as units are for scalar wiring today: MATOFX transports the bytes; it does not reconcile mesh topology or file format. The NAFEMS chain's **fixed-mesh spine** (identical node ordering across the chain) makes the field handoff trivially compatible; general cross-mesh remap stays the deferred v1.x field-remap path.

---

## 4. Descriptor schema

Permit `downstream_input` + `upstream_tool` on a `type="file"` input (today they appear on numeric inputs). No new keys — the existing wiring flags simply become valid on file inputs. Validation (`tools.descriptor`): a file input may carry the wiring flags; `accepted_file_types` / `max_file_size_mb` continue to apply to the *upload* path (standalone use) and are advisory for the wired path.

A wired file input still declares a sensible standalone affordance (upload / library / previous-pick) so the tool runs alone. Inside a workflow whose chosen set contains `upstream_tool`, the field is wired and the upload widget is suppressed (the existing per-tab `_all_upstream_ready` / locked-field seeding already hides wired inputs — `wired_inputs` is type-agnostic, so this works for free).

The upstream **file output** is already fully described: `type="file"`, `file_type`, `filename`, and for fields `field_format` / `field_node_array_name` / `field_timesteps`. The name-match is `output.name == input.name`.

---

## 5. Runtime change — the one real seam

All inside `matofx.workflows.run_service.WorkflowRunService.advance`, which already: reads each completed member's outputs, plans ready members (`wiring.ready_member_inputs`), and calls `queue_for_runner(inputs=…)` per ready member.

**5.1 `_read_member_outputs` — surface file outputs as resolved paths.**
Today it returns `{output_name: scalar}` from `outputs.json`. Extend it to also walk the member tool's **descriptor file outputs** and, for each, resolve the on-disk path under the member job's retained working dir (`mount_path / working_dir_relative / outputs / <filename>`; same resolution the runner's `_resolve_staged_working_dir` uses). Add `{output_name: <absolute path str>}` for file outputs. The completed-outputs map thus carries scalars *and* file paths, keyed by name.

- Field outputs are guaranteed present because the **retention floor keeps descriptor-declared field outputs** (ADR-0008 §4.4; `supervisor._field_retained_paths` already preserves `field_timesteps` globs). A genuinely-absent file is simply omitted (→ §5.4).

**5.2 `resolve_wired_values` — unchanged.**
It already copies `produced.get(name)` into the downstream input map. With §5.1 the value for a file-wired input is now the upstream output's absolute path — the *same shape a previous-job pick produces*. No change needed to the pure wiring planner.

**5.3 Staging copy at queue time — reuse, lightly extend.**
`queue_for_runner` already calls `promote_draft_uploads_into_working_dir`, which copies file-input values that live under the draft area into `inputs/uploaded/<field>/` and rewrites the path. Extend the same pass (or add a sibling resolver) to also copy a file-input value that points at **another job's retained output** into `inputs/uploaded/<field>/`, rewriting the value to the staged path. This is the previous-job-pick copy generalised; it reuses `_safe_field_segment` (sanitised, traversal-safe) and `shutil.copy2`, and lands the file on the shared mount inside the downstream working dir so any execution target (subprocess / SSH / WinRM / HPC) reads it uniformly (shared-mount-source-of-truth).

- **Reference vs copy:** we **copy** (not just pass the absolute path) so the handoff survives upstream cleanup and works across targets — consistent with how uploads/library refs are staged. Direct-reference is rejected as fragile.

**5.4 Missing file.**
If §5.1 can't resolve the upstream file (output never produced), it is omitted from the completed-outputs map; `resolve_wired_values` already skips absent values; the downstream job's own input validation then surfaces a plain-English "required input missing" — identical to the scalar-absent path today. No crash, no `None` written.

**5.5 Series (`field_timesteps`).**
When the upstream file output declares a `field_timesteps` glob, §5.3 stages **all** matching files into `inputs/uploaded/<field>/`, and the rewritten input value is the **primary `filename`** (the declared representative — e.g. the final timestep). A tool that wants the whole series globs its siblings in the same dir. (Bracket Stress takes the final temperature field as its thermal load → primary file is sufficient.)

---

## 6. What does *not* change

- `wiring.py` (planner), `ordering.infer_edges_with_fields` (edges), `studies.factors` / `wiring.open_input_fields` (factor exclusion) — all type-agnostic already.
- The New Workflow UI, the workflow graph, and the optimisation factor picker — they consume the above unchanged.
- Scalar wiring — byte-for-byte identical behaviour.
- The `inputs.json` contract — file inputs remain absolute-path strings (as today; `reports.assets` already treats job-input file values as absolute paths).

---

## 7. Build surface

1. `tools/descriptor.py` — allow `downstream_input` + `upstream_tool` on file inputs (validation + any model field gating).
2. `workflows/run_service.py` — `_read_member_outputs` resolves file outputs to paths (§5.1); thread the member descriptor + working dir into that resolution.
3. `tools/exec/uploads.py` (or a sibling in `staging`) — generalise the promote/copy pass to stage an upstream/prior-job retained output into `inputs/uploaded/<field>/` (§5.3, §5.5).
4. **Tests:** unit — file output resolves to a path; series stages all files; missing file omitted. Integration — a two-member workflow with a `.vtp` handoff completes and the downstream job reads the staged file. Optimisation — a workflow-target study with a file-wired edge excludes it from factors (regression of existing behaviour).
5. **Docs (CLAUDE.md rule):** admin workflow help — "passing result files between steps"; `ARCHITECTURE.md` §16.4 wiring note; this design's status flipped on landing.

---

## 7a. Manual (tab-by-tab) run path — added 2026-06-22

The original build resolved + staged wired files only in the headless
`WorkflowRunService.advance`. The **manual** workflow-run UI seeds a downstream
tab's wired inputs from the per-tab output documents, but a **file** output's
`value` is `None` (the path is in `filename`), so the file input arrived empty.
Fix: resolve file-wiring at the shared queue **chokepoint** instead of only in
`advance`. `JobService.queue_for_runner`, when the job is a workflow member and
no explicit hand-off was passed (the manual path), calls
`WorkflowRunService.resolve_wired_files_for_member` to resolve the member's
file-wired inputs from completed siblings, injects the primary path into the
inputs, and stages them via the same `stage_wired_file_inputs`. Headless is
unchanged (it passes `wired_file_inputs` explicitly, so the branch is skipped).
Both run paths now stage wired files identically. *(Minor UX follow-up: the
locked file field shows the "provided automatically" note but renders empty
until submit; could echo the source filename.)*

## 8. Decisions — LOCKED 2026-06-18

1. **Series value** — ✅ **stage the whole series, pass the primary/final `filename`** as the input value; the tool globs siblings in the same dir if it wants the series. Keeps the file-input contract a single path string.
2. **Size guard** — ✅ **v1 relies on existing disk/retention budgeting**; no per-handoff operator cap (deferred to v1.x). Accepted for the demo.
3. **Lineage edge** — ✅ **deferred to v1.x**; the run already records the member→member dependency.
