# Execution scheduling — local / GPU / HPC job admission

Status: **Part A shipped 2026-06-12.** Part B (AOS/HPC dispatch wiring + AOS
per-host gating) deferred — see §5.

This design covers how the runner's pickup loop (`matofx.runner.supervisor`)
decides *which* queued jobs to start *when*. It does not cover how a started
job is executed (the `ExecutionTarget` backends) or staged.

## 1. The problem

Before Part A the pickup loop was a single counter:

```python
available = local_concurrency - len(running)   # every running job, any target
```

Three faults:

1. **AOS/HPC jobs counted against the local cap** (the "P4" over-count). A job
   handed off to a cluster isn't a local process, yet it consumed a local slot —
   so a few cluster jobs could starve local work.
2. **No GPU arbitration.** Two local GPU jobs could start at once and exhaust a
   card's memory (the descriptor validator refuses *AOS*+GPU but allows
   *local*+GPU).
3. **A free slot is not free capacity.** `local_concurrency=10` would start ten
   memory-hungry jobs on a box that fits three, and they'd OOM each other.

## 2. The model — three admission lanes

A job's lane is read from two cheap `tool` columns — `execution_target` and
`requires_gpu` — no descriptor-JSON parse.

| Lane | Who owns the queue | Admission control |
|---|---|---|
| **Local CPU** (`local`, not GPU) | MATOFX | a free local slot **and** the host CPU/RAM budget covers the declared request |
| **Local GPU** (`local` + `requires_gpu`) | MATOFX | the above **plus** a free GPU slot; also consumes a local slot |
| **AOS / HPC** (`aos` / `hpc`) | the remote host / cluster scheduler | exempt from the local lanes — handed off, the remote queues it |

Key property: **a local GPU job consumes *both* a local slot and a GPU slot**, so
`local_concurrency=2, gpu_concurrency=1` runs at most 1 GPU + 1 CPU, never 2 GPU.

## 3. Resource budget (local lanes)

Each tool declares `resource_request.cores` / `memory_mb` (already read by
`_resource_request_for`). The runner sums the requests of currently-running local
jobs and only admits a candidate when the **unused** budget covers its request —
checked per-candidate within a tick so one pickup pass can't over-commit.

The budget comes from admin-tunable runner settings (`matofx.runner.hostcaps`):

- `local_cpu_cores` — `0` ⇒ auto-detect logical cores (psutil).
- `local_memory_mb` — `0` ⇒ auto-detect total RAM minus `local_memory_reserve_mb`.
- `local_memory_reserve_mb` — RAM held back for the OS + MATOFX (auto path only).

Host capacity is detected once and cached for the process lifetime (the machine
doesn't change); the *settings* hot-reload on the 30 s timer.

**Over-large guard.** A request that can't fit even on an idle host would sit
queued forever, so it's failed at pickup with a plain-English reason rather than
silently stuck.

## 4. Settings (super-user amendable)

All live in `RUNTIME_SETTINGS_CATALOG` (domain `runner`), seeded by migration
`0077`, editable from the runner settings admin surface (`runtime_settings.manage`,
which a super-user satisfies), hot-reloaded without a restart:

| Key | Default | Meaning |
|---|---|---|
| `local_concurrency` | 2 | max local jobs at once (existing) |
| `gpu_concurrency` | 1 | max local GPU jobs at once |
| `local_cpu_cores` | 0 (auto) | local CPU-core budget |
| `local_memory_mb` | 0 (auto) | local memory budget |
| `local_memory_reserve_mb` | 2048 | RAM reserve for the auto memory budget |

Existing ordering is unchanged: non-study before study children, then the
skip-queue priority flag, then FIFO. **Skip-queue still only reorders — it never
bypasses a lane cap or the resource budget** (cf. `feedback_priority_flag_not_bypass`).

## 5. Part B — deferred

- **AOS/HPC dispatch wiring.** The backends (`hpc_slurm`, `hpc_mshpc`, `aos_ssh`,
  `aos_winrm`) exist but aren't registered in the live `TargetRegistry`; in Part A
  an AOS/HPC job still fails at pickup (no target) — the point of Part A is only
  that it no longer burns a local slot first. Wiring needs a persistent
  `SecretsStore`.
- **AOS per-host gating.** Unlike a real scheduler, an always-on server has no
  queue of its own — MATOFX must gate it per-host (concurrency + CPU/RAM budget,
  as new `aos_host` columns). This lands with the AOS dispatch wiring, since an
  AOS job can't run until then.

## 6. Tests

`tests/runner/test_hostcaps.py` (budget resolution, reserve, floors) and the
lane/budget cases in `tests/runner/test_supervisor.py` (strict GPU cap, GPU
consumes both lanes, CPU budget, memory budget, over-large fail, HPC not counted,
HPC exempt when local full).
