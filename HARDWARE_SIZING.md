# MATOFX v2 — Hardware sizing guide

Recommended computing requirements for a MATOFX server, with the reasoning
behind each number. The plain-English summary lives in the admin guide's
*Installing and setting up MATOFX* chapter; this document is the long-form
version for whoever has to justify a procurement request or size a VM.

**The one framing that matters:** the MATOFX application itself is deliberately
lightweight. What drives the size of the machine is **where tool jobs execute**.
MATOFX dispatches jobs to one of six backends (`local`, AOS over SSH/WinRM,
AOS-GPU, Slurm, MS-HPC). If solvers run on remote backends, the MATOFX server is
a small web/database box that rarely needs revisiting. If solvers run on the
`local` backend, the machine must be sized for the solvers — and that sizing
belongs to the tools, not to MATOFX.

---

## Recommended configurations

| Resource | Minimum (trial / pilot) | Recommended (production team) |
|---|---|---|
| CPU | 4 cores | 8–16 cores |
| RAM | 8 GB | 32 GB |
| GPU | None | None |
| Storage | 100 GB SSD | 500 GB – 2 TB SSD, expandable |
| OS | Windows or Linux, Python 3.12+ | Same |

Add to the production budget: ~2 GB RAM if PostgreSQL runs on the same box, and
whatever the heaviest locally-executed tool needs (cores × concurrent jobs,
solver RAM, tool-specific GPU).

---

## CPU — why 4 minimum, 8–16 recommended

What runs on the server:

- **`matofx-api`** — the NiceGUI UI + FastAPI API in one async Python process.
  Async I/O-bound; idles on 1–2 cores even with a few dozen concurrent users.
  Page rendering, AG-Grid tables, and Plotly charts are assembled server-side
  but are cheap; the heavy rendering happens in the browser.
- **`matofx-runner`** — the job-runner daemon: queue polling, staging, cleanup,
  KPI extraction, the inactive-user sweeper. Negligible CPU on its own.
- **Burst loads that *do* use cores:**
  - **Surrogate fitting** — Gaussian-process training (scikit-learn) and POD/PCA
    decomposition (numpy SVD) saturate several cores for seconds to minutes per
    fit. These run in-process (off the event loop, but on this machine).
  - **Optimisation studies** — in `surrogate` run mode, every candidate
    evaluation (pymoo populations, BO acquisition optimisation, MOP/CoP
    metamodel sweeps) is in-process CPU work. A running study keeps a core or
    two busy for its duration.
  - **Locally-executed tool jobs** — each `local`-backend job is a subprocess
    that uses whatever the tool uses. This is the unbounded term: a CFD solver
    will take every core it can get. The `local_job_walltime_minutes` setting
    kills runaway jobs but does not limit their core usage while running
    (per-job resource limits are a v1.x roadmap item).

**Reasoning:** 4 cores covers UI + runner + one modest local job or one
surrogate fit at a time — fine for a trial. 8–16 cores gives a production
install headroom for concurrent study batches, surrogate fits, and a few local
tool jobs without starving the UI. Beyond 16 cores you are sizing for solvers,
and should consider moving them to an AOS/HPC backend instead.

## RAM — why 8 GB minimum, 32 GB recommended

Baseline footprint is small: the two services plus SQLite sit comfortably under
2 GB. The recommendation is driven by burst loads:

- **Field-surrogate fitting is the dominant term.** POD over an ensemble of
  CFD/FEA snapshots holds an `n_nodes × n_snapshots` float64 matrix in memory,
  plus working copies during the SVD. Worked example: a 500,000-node mesh over
  a 250-snapshot ensemble is 500,000 × 250 × 8 bytes ≈ **1 GB per field
  variable** for the data matrix alone; with SVD workspace and the per-mode GP
  fits, peak usage is roughly 3–4× that. Time-resolved surrogates multiply the
  snapshot count by timesteps per run — a 50-run × 20-timestep transient
  ensemble is 1,000 snapshots, so the same mesh costs ~4 GB for the matrix and
  >10 GB at peak.
- **Scalar work is cheap.** Parametric-study statistics, scalar GP/polynomial
  surrogates, Sobol sensitivity, and optimisation on scalar objectives all
  operate on run-count-sized arrays — megabytes, not gigabytes.
- **Per-user session state** (NiceGUI) and **report generation** (self-contained
  HTML, bounded size by design) are small.
- **Local tool jobs** again inherit the tool's own footprint.

**Reasoning:** 8 GB serves a scalar-only install indefinitely. 32 GB lets a
production install fit field surrogates over realistic CAE meshes without the
operator thinking about it. If your meshes exceed ~1M nodes or your transient
ensembles run long, scale RAM with the worked example above — it is linear in
nodes × snapshots.

## GPU — why none

- **All 3D rendering is client-side.** The CAD viewer, the field-surrogate
  Mesh3d viewer, and animation playback run in the user's browser (Plotly /
  three.js via NiceGUI). The server only serves geometry and per-node values as
  data.
- **The ML stack is CPU-based.** scikit-learn GPs, numpy POD, pymoo — none use
  a GPU, and at MATOFX's training-set sizes (hundreds of runs) a GPU would not
  help.
- **GPU-requiring tools** should run on the AOS-GPU backend, where a remote
  agent owns the device. If such a tool must run on the local backend, the GPU
  is a *tool* requirement: size it to the tool, and note the local GPU queue is
  deliberately depth-1 (one GPU job at a time; no device sharing).

## Storage — why 100 GB minimum, 500 GB–2 TB recommended, and "expandable" above all

Consumers, largest first:

1. **Job working directories** (the shared file area). The dominant and
   *monotonically growing* term: MATOFX is archive-by-design — projects and
   jobs are archived, never deleted. Post-job cleanup removes intermediates,
   but the retention floor always keeps `inputs.json` / `outputs.json` /
   declared plot files, and tools that retain result artefacts keep those too:
   - Scalar-only tools: ~1–10 MB per job.
   - Tools retaining CFD/FEA field snapshots (anything feeding a field
     surrogate): tens of MB to several GB per job; a transient series
     multiplies by timestep count.
   - A 300-point DOE with retained fields can therefore be 50–100+ GB on its
     own.
2. **Model bundles** — fitted surrogates (POD basis + per-mode models +
   optional reference mesh): tens to hundreds of MB each, versioned and
   immutable.
3. **Library files and materials** — content-hashed, immutable versions;
   growth proportional to curation activity, typically modest.
4. **Database, audit chain, logs** — small. The DB is metadata, not bulk data;
   the hash-chained audit log grows by a row per audited action; JSON-Lines
   logs rotate.

**Why SSD:** SQLite latency under concurrent writers is the practical
bottleneck on spinning disks (the busy-timeout and write-coalescing mitigations
help, but seek-bound storage makes them work harder). Postgres benefits
equally.

**Reasoning:** 100 GB carries a trial with scalar tools for a long time. For
production, the honest answer is *you cannot size this up front* — it depends
entirely on whether tools retain field data and how many studies run. 500 GB–
2 TB is a sensible starting bracket; the real requirement is a volume you can
grow (LVM, VM disk expansion, or a network share for the file area). Storage
lifecycle tiering (archive-to-cold) is an acknowledged v1.x roadmap item —
until it lands, plan for monotonic growth.

## Database choice

- **SQLite** (default): zero extra footprint, one file, ideal up to a
  single-team install. Keep it on local SSD, not a network share.
- **PostgreSQL 16+**: for larger or busier installs. On the same box, budget
  ~2 GB extra RAM; on existing DB infrastructure, the MATOFX box shrinks
  accordingly. Both engines are exercised in CI; switching is a config change
  plus a data migration.

## Compute backends — sizing that is *not* the MATOFX server's problem

AOS hosts, Slurm/MS-HPC clusters, and GPU agents are sized entirely by the
tools that run on them; MATOFX only stages files and dispatches. The
recommended deployment pattern for customers with existing compute: start with
the production single-server spec above, point heavy solvers at the existing
cluster, and treat the MATOFX box as a stable fixture. The shared file area
must be reachable from wherever jobs run (SMB/NFS for remote backends) — its
throughput matters more than the server's CPU once jobs are remote.

## When to revisit

Re-size when any of these happen, in likelihood order:

1. Tools begin retaining large field data → storage first, then RAM (if those
   fields feed surrogates).
2. Heavy solvers land on the `local` backend → cores + tool RAM, or better,
   move them to a remote backend.
3. Concurrent optimisation studies become routine → cores.
4. User count grows past ~50 concurrent → consider PostgreSQL; the app
   processes themselves are unlikely to be the limit.
