# 0003. Job I/O-document retention floor + `csv_plot` declared output kind

Date: 2026-05-19
Status: Accepted (spun out of the ADR-0001 2026-05-19 amendment — the
parametric-studies results explorer forced two platform-wide changes that are
not study-scoped and deserve their own record)

## Context

The Parametric Studies results explorer (ADR-0001, `designs/parametric-studies.md`
§10) needs every finished child's outputs to be **durably investigable**. The
as-built reality blocks that:

- A tool writes `<wd>/outputs/outputs.json` (an `OutputsDocument`); inputs are
  in `<wd>/inputs/inputs.json` (an `InputsDocument`). **Neither is persisted to
  the DB.** They are parsed on demand from the per-job working directory
  (`hierarchy/service.py` `_read_output_document`).
- The runner's `_post_complete` parses `outputs.json`, then `clean_directory`
  **deletes everything outside the descriptor's `retain_files`**, plus any
  `delete_io_files` the descriptor names inside `inputs/`/`outputs/`; working
  directories are later reaped.
- A `file` output is **opaque** — the `OutputsDocument` carries a `filename`
  with `value = None`; nothing tells MATOFX a `.csv` holds numeric columns.

So today a study (or any job) can complete and leave nothing reliably
inspectable, and CSV/table file outputs cannot be plotted because the platform
cannot know they are tabular numeric data. These two gaps are **platform-wide**
(every job, not just study children), so they are recorded here rather than
buried inside ADR-0001.

## Decision

**1. `csv_plot` declared output-file kind.** The tool descriptor declares, per
output *file*, what it is — adding a **`csv_plot`** kind: a CSV/table whose
columns are numeric and which is intended to be plotted. This declaration (in
the descriptor and the `OutputsDocument` contract) is the single gate for both
plottability and retention. Column roles (independent `x` vs series) are **not**
hard-declared — the results explorer picks them at view time, so different
analyses use different columns of the same file. The H41 descriptor
meta-schema lint learns the new kind; existing descriptors are unaffected
(opt-in).

**2. Unconditional I/O-document + `csv_plot` retention floor.** For **every
job** — standalone, workflow child, or parametric-study child — these paths are
**always retained**, overriding both the non-`retain_files` default *and* any
descriptor `delete_io_files` that targets them:

- `inputs/inputs.json`
- `outputs/outputs.json`
- every output file the descriptor declares as `csv_plot`

`clean_directory` enforces this floor before applying the descriptor's
`retain_files`/`delete_io_files` rules. Everything else is unchanged: tools may
still drop large intermediates; the floor is small and is the I/O contract.

**3. Durable per-child results store (study-scoped, depends on 1+2).** At a
study child's terminal completion the runner — at the existing `_post_complete`
parse, **before** `clean_directory` — persists per-child results into the
`parametric_study_result` store (ADR-0001 §10.0): all numeric + string scalars,
the varied input values, and references to the retained `retain_files` /
`csv_plot` artefacts. Failed/cancelled children are stored with a null
response. This makes results survive cleanup and reaping regardless of
`retain_files`. (Delivered with 2-PS-b0; the store migration chains after the
2-PS-a / `0039` head.)

## Consequences

- **Touches shipped, gate-validated code across all jobs**: `clean_directory`
  and its `retain_files`/`delete_io_files` logic + tests; the tool-descriptor
  schema + the H41 descriptor meta-schema lint; the `OutputsDocument`/contract
  models; `runner/supervisor.py` `_post_complete`. Regression surface is
  platform-wide, not study-only — called out so it is tested as such.
- `CLAUDE.md` and `ARCHITECTURE.md` describe the *old* cleanup rule ("everything
  except `retain_files`, plus `delete_io_files`"); both get the retention-floor
  note **when the change is built** (ARCHITECTURE tracks as-built state).
- Storage cost is negligible — `inputs.json`/`outputs.json` are small and are
  the integration contract; `csv_plot` files are opt-in per descriptor.
- The `parametric_study_result` store is the single read source for 2-PS-b0/b/c;
  b/c add no new write path.
- **Revisit when:** first pilot's `csv_plot` file sizes are known — decide
  whether the b0 store snapshots a small head of each `csv_plot` for fast
  overlay preview or always reads the retained file on demand (ADR-0001 §16).

## Revision history

- **2026-05-19 — created, Accepted.** Spun out of the ADR-0001 2026-05-19
  amendment; build spec in `designs/parametric-studies.md` §10 + §17.
