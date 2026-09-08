# MATOFX SPDM v2 — Project Brief

This file captures the user's answers to the rewrite-discovery questions, one section per topic. It is the source of truth for design decisions on the rewrite. Update it as answers come in; do not delete prior answers without the user's say-so — supersede with a dated note instead.

Loaded automatically each session via `CLAUDE.md`.

---

## A. Audience, scale, and deployment model

### A1. Who installs and runs it?
**(b) Multiple separate customer organisations, each on their own isolated install** — one install per customer org, single-tenant per install. No shared multi-tenant service.

Implications to carry forward:
- No cross-org tenancy logic needed in the app itself; isolation is at the install level.
- Each install is its own trust boundary — its own DB, its own secrets, its own users, its own licence.
- Installer/update story matters more than a hosted-service story (see C12–C13).
- Licensing is per-install / per-customer (see C16).

### A2. Where does it run?
**Primary targets (must all be supported):**
- (a) On-prem Windows Server
- (b) On-prem Linux server
- (c) Customer-owned private cloud VM (AWS / Azure / GCP)

**Secondary (should be possible but not the focus):**
- (d) Funis-hosted (one VM/container per customer) — possible but uncommon

**Out of scope:**
- (e) End-user workstation / laptop installs — only ever as a developer/build environment

Implications to carry forward:
- Must run on **both Windows and Linux** as first-class targets — no OS-specific shortcuts (e.g. Windows-only `subprocess` shells, Linux-only paths). Specific OS versions still TBD (revisit alongside C12).
- Cloud-VM friendly: deploy via SSH/RDP onto a customer-controlled VM; do **not** assume any specific cloud's managed services (no hard dependency on AWS S3, Azure Blob, etc.) — anything cloud-native must be optional/swappable.
- Infrastructure assumed available on the box: a local filesystem, outbound HTTPS (unless air-gapped — see C15), and whatever the customer's IT provides for SMTP/identity.
- Deployment artefacts should be portable enough that the same build works on-prem and on a customer cloud VM (pushes toward containers or a self-contained installer — confirm in B10).

### A3. Concurrent users per deployment
**Concurrent users:** Tiny to Medium for v2 — i.e. roughly **1–200 concurrent users** per install, with most installs likely at the lower end.
**Total accounts per install:** materially more than concurrent users (variable, customer-dependent — no fixed cap, but assume "hundreds" rather than "thousands" by default).
**Usage pattern:** depends on the customer.
- **Local customer (single site/timezone):** bursty — peaks around start-of-day, lunch return, and end-of-day; quiet overnight.
- **Global customer (multi-region):** steady, near-24/7 load with no clear quiet window.

Implications to carry forward:
- Must comfortably serve **up to ~200 concurrent users** — a single-process Streamlit-style app won't cut it; design for a real web framework with async + multiple workers from day one.
- Cannot rely on overnight quiet windows for maintenance (some customers are global) — backups, migrations, restarts must be online or near-online.
- Design for **horizontal scalability** but don't *require* it; tiny installs should still run fine on one modest VM.
- Database choice (B8) should comfortably handle ~200 concurrent connections plus background workers — pushes toward Postgres for prod, even if SQLite stays for dev/tiny.

### A4. Concurrent jobs and job profile
**Concurrency:** up to **~50 concurrent jobs** per install (peak), counting everything from in-process calculators through to externally-submitted HPC jobs the app is tracking.

**Duration spread:** the **full range** must be supported in a single install:
- Snappy (sub-second to a few seconds) — calculator-style tools.
- Medium (seconds to ~5 min) — most legacy tools.
- Long (minutes to hours).
- Very long (hours to days).

**Resource profile:**
- ~50% medium-CPU, ~50% long-CPU.
- Cross-cut: roughly **~50% GPU-using, ~50% non-GPU** across the workload.
- **Long / very-long jobs are typically dispatched to an HPC cluster, not run on the app server.** The HPC is **customer-controlled** (their own cluster); a managed cloud HPC like **Rescale** is a possible alternative target.

Implications to carry forward:
- Need a **proper job-queue + worker model** — not FastAPI BackgroundTasks. 50 concurrent in-process jobs would saturate a single web process and is incompatible with mixing snappy and very-long jobs in one queue. Pushes toward Celery / RQ / Arq / Dramatiq (decide in B9), with multiple worker pools (e.g. "fast", "long", "gpu").
- Job runner must support **multiple execution backends** behind one abstraction: local subprocess (with and without GPU), remote HPC submission (Slurm / PBS / HPC Pack / Rescale), and possibly cloud batch. Treat "where this job runs" as a property of the tool/queue, not hard-coded into the runner.
- HPC integration must be **pluggable per install** — different customers have different schedulers; design an "execution backend" interface so we can add Rescale, Slurm, PBS, HPC Pack without forking the core.
- For long/very-long jobs the app is mostly a **tracker**: poll status, stream logs, surface results. Submission and status updates may need to survive app restarts → durable job state in Postgres + idempotent reconciliation loop, no in-memory queues for long jobs.
- GPU work: the legacy "remote GPU agent + in-memory queue" pattern is too fragile for 50/50 GPU load. Either schedule GPU jobs through the same execution backend (a "GPU worker pool" or HPC partition) or integrate with a real scheduler.
- Snappy jobs (sub-second) are an interesting edge: routing them through an external queue can add more latency than the job itself. Worth supporting an **inline / synchronous** execution path for trivially fast tools, while still recording them in the job history.

### A5. Single machine vs distributed
**(b) Single box by default, but components are separable.** One VM hosts everything for tiny installs; the same artefact can be split (web / workers / DB / file storage on separate machines) for larger installs through configuration alone — no code changes, no parallel "scale-out" build.

Guiding principles:
- **Optimise for performance and minimise complexity.** Prefer one well-tuned component to two mediocre ones.
- **Single artefact** covers both tiny (5 users) and medium (200 users) installs — only configuration differs.
- **HPC machines are external** and customer-managed. They are never part of the install; the app only talks to them via their submission API/CLI.
- **Availability target:** important, but **~1 day of unplanned downtime per incident is acceptable**. Not a high-availability / zero-downtime product. No need for active-active, hot standby, or multi-region failover in v1.

Implications to carry forward:
- Architecture must cleanly separate **web / workers / DB / object-or-file storage** as logical components communicating over the network, even when they all live on one box. No in-process queues, no "shared module global" coordination, no localhost-only assumptions baked into the code.
- All inter-component links (DB connection, queue broker URL, object-store endpoint, SMTP, identity provider, HPC backends) are **configurable via a single config file / env-vars** — same binary, different config per deployment shape.
- Single-box default should still be **easy to install** (one installer / one compose file) — no requirement for the admin to understand the split-out shape until they need it.
- Backups, restarts, and migrations don't have to be zero-downtime; we can take a maintenance window. But they do need to be **scripted and predictable** so a 1-day outage is the worst case, not the average.
- No HA-specific tech in v1: skip Postgres replication, Redis Sentinel/Cluster, k8s pod-disruption-budgets etc. unless they fall out for free.
- HPC integration is a **client** of the app, not a component of it: secrets/credentials for the customer's HPC are config; failures there must not take down the web/job tier.

### A6. Browser-only vs desktop / CLI / API-first
**Primary (and only end-user surface): (a) Browser web UI.** Simplicity for users is the highest-priority UX goal — no public CLI, no public REST API, no Python SDK, no desktop client, no webhooks **as products** in v1. Users always click; never script.

**Outbound / integration channels the app itself uses (not exposed as a product):**
- **SSO / identity provider** — the app talks outbound to the customer's IdP for login (revisit the protocol in D17).
- **HPC clusters / Rescale** — outbound submission, status polling, log retrieval.
- **Remote tool servers** — some tools may be deployed on separate machines (potentially using custom per-tool APIs). The app is the client; tools expose endpoints.
- **SMTP / email** — outbound notifications.

**Data exposure outside the app:** none in v1. Any future "expose data to BI tools" would only ever live inside the customer's own controlled environment; do not design for it now.

Implications to carry forward:
- The app is **not API-first in the public sense** — no obligation to keep a stable, documented external HTTP API, no OpenAPI/Swagger as a contract product.
- Internally, however, the web UI **should still talk to the backend over a clean HTTP API** (single-page app or server-rendered with a structured backend) so the web tier and worker/job tier can be separated cleanly per A5. Just don't promise that API to outside consumers.
- All outbound integrations (IdP, HPC, remote tools, SMTP) sit behind a small set of **typed adapter interfaces**, not scattered HTTP calls — one place to add timeouts, retries, circuit-breakers, secret loading, and audit logging.
- **Remote tools** are a real first-class concept: a tool's "execution backend" can be "this server" *or* "this remote tool server's API". Plan the tool plugin contract (the v2 successor to today's `*_descriptors.json`) to declare its execution backend up front.
- **Machine-to-machine auth is internal-only** in v1: any service-account / API-key style credentials exist for the app to authenticate *to* external systems (HPC, tool servers, IdP), and for those systems to authenticate *to* the app's webhook-style callback endpoints if HPC pushes status updates. No customer-facing API keys.
- If we ever expose a public API later, it should be a deliberate addition layered on top of the same internal API — design with that path open, but don't build it.

---

## B. Tech-stack preferences

### B7. Stay on Python or change stack
**Decision: All-Python, with NiceGUI on top of FastAPI.**

Hard constraints:
- **All-Python project.** One language across UI, API, workers, and tool plugins.
- Some JS may be needed (3D viewers, animation, etc.) but **injected/generated from Python**, never maintained as a parallel JS codebase.
- **No Node toolchain on the build server.** Anything requiring `npm`/`pnpm`/`yarn` at build time is out.
- **Deployable as a single artefact.** Ideally one installer / one container; PyInstaller-style single-binary is acceptable.
- **Debuggable from VS Code without extensions** (plain Python debugger only).
- **Look-and-feel:** modern, sleek, minimal — explicitly *not* the dated "enterprise SPDM" aesthetic.
- Keep the project as simple as possible.

Chosen stack:
- **UI:** NiceGUI (Python API → Quasar/Vue under the hood; Material-grade components OOTB; built-in `ui.scene` for three.js, `ui.echart`, `ui.aggrid`, `ui.pdf_viewer`, `ui.upload`, `ui.tree`, `ui.tabs`, `ui.stepper`, dark mode + theming).
- **Backend:** FastAPI — NiceGUI runs on top of FastAPI, so the UI and the internal HTTP/API/workers share one app object, one DI graph, one async loop.
- **No Node** anywhere; Quasar assets ship pre-built inside the NiceGUI Python package.
- **Custom JS** is allowed where Python-native components don't exist, but always injected from Python (e.g. `ui.add_head_html`, `ui.run_javascript`).

Implications to carry forward:
- Per-user UI state lives on the server (NiceGUI is WebSocket-backed). Be deliberate about per-session memory and **plan for sticky sessions** if/when the web tier is split (per A5). Fine on a single VM up to ~200 concurrent users.
- We're committed to the **Quasar component family** for visual identity — bespoke design beyond Quasar theming is possible but extra effort. v1 should lean into Quasar's defaults to ship fast and look modern.
- If a page ever outgrows NiceGUI we can drop to a raw FastAPI route + minimal template alongside, since they share the same app — but treat that as an escape hatch, not a pattern.
- All long-running work, HPC submission, tool execution, etc. lives in **separate worker processes** (decided in B9), not inside the NiceGUI/FastAPI web process. Web process stays responsive.

### B8. Database choice
**Decision: SQLite as the default, PostgreSQL as a first-class alternative — both behind one async ORM (SQLAlchemy 2.x), one set of migrations (Alembic), one config switch.**

Why both:
- Most customers fit SQLite's profile fine (bursty per-user load, ~50 jobs, sub-second writes, single VM). Per A3/A4, occasional ms-scale waits are acceptable.
- Some customers will hard-require Postgres (security policy, cloud-managed DB, "no file DBs in prod"). Forcing them onto SQLite is a non-starter.
- Forcing every install onto Postgres adds setup friction for the 80% who don't need it.

Discipline that keeps "supporting both" cheap:
1. **SQLAlchemy 2.x in async mode**, with `aiosqlite` and `asyncpg` drivers. Same models, same query API.
2. **Stick to the common SQL subset.** No JSONB / `tsvector` / array columns in core models — plain `JSON`/`TEXT` everywhere.
3. **Engine-specific features go behind small adapters** (e.g. `search.py` with SQLite-FTS5 and Postgres-`to_tsvector` implementations exposing one function). Never sprinkle dialect-specific code across the codebase.
4. **Alembic** for all migrations; one set of revisions, dialect-aware DDL.
5. **CI matrix runs every test on both** (`DB=sqlite` and `DB=postgres`).
6. **Single config switch:** `DATABASE_URL=sqlite+aiosqlite:///...` vs `DATABASE_URL=postgresql+asyncpg://...`. No branching code paths in app logic.
7. **SQLite tuning baked into one place** (`engine_setup.py`): WAL mode, `synchronous=NORMAL`, `busy_timeout` set on connect, reasonable connection pool. Avoid the legacy pattern of late/scattered `PRAGMA` calls.
8. **Backups via one CLI command** (`matofx-backup` — placeholder name): SQLite → `VACUUM INTO` to a file; Postgres → `pg_dump`. Same UX for the admin.

Postgres-mode requirements (when chosen):
- TLS to the DB (configurable; required by default for non-localhost connections).
- Secret-rotation-friendly password loading (re-read on reconnect; pluggable secret source per E25).
- Resilience to transient `OperationalError` (retries with backoff on the connection wrapper, not in business logic).
- Tolerate 5–20 ms round-trips (no per-row `SELECT` chatter from app code; design queries to fetch what's needed in one round-trip).
- Connection pooling sized for the deployment shape (per A5).

What this rules out for v1:
- No JSONB indexing, full-text-search-as-Postgres-feature, listen/notify, partial indexes, row-level security, or array columns in core schema. Add them later as engine-specific adapters if a real need appears.

Implications to carry forward:
- The data layer is its own module with a thin, typed repository API; the rest of the code never imports SQLAlchemy directly. Lets us swap or extend engines without touching call sites.
- Job queue (B9) must coexist with whichever DB is chosen — pushes toward a queue that doesn't *force* Postgres or SQLite (i.e. either an external broker like Redis, or a queue layer that runs on top of either DB).
- Audit log (E26), licence chain (C16), and any tamper-evident structures should not assume Postgres-only features.

### B9. Job queue / worker model
**Decision: Arq + Valkey.** Broker-based, async-native, matches the FastAPI / NiceGUI stack. Valkey (BSD-licensed Redis-protocol fork) is the broker, bundled with the install by default; customers can point at their own managed Redis/Valkey via config.

> **NOT AS BUILT — recorded 2026-08-12.** Neither Arq nor Valkey is a dependency of the v2 codebase, and nothing imports them. What shipped is a **single `matofx-runner` process** whose
> `RunnerSupervisor` drives every periodic concern as a `_sleepable` loop (job pickup, heartbeats,
> cancellation, settings reload, the hourly licence re-check + audit-chain walk), with the
> **database as the queue** and admission gated by per-lane concurrency + host resource budgets
> (ARCHITECTURE §16.32/§16.33). This removes the broker from the install entirely, which suits the
> air-gapped, lightweight-install stance in §A1/§F28 better than bundling Valkey — but it is a
> divergence from a *decision*, not an unimplemented plan, so it is flagged rather than quietly
> left to be discovered.
>
> Everything below in this section describing named broker queues, Arq cron and broker/DB
> reconciliation therefore describes an intended design that was not built. Re-open it as an ADR if
> a customer's scale makes the single-runner model insufficient; do not treat it as current.

Worker design:
- **Library:** Arq (`async def` task functions, native asyncio, small footprint).
- **Named queues by job shape:** start with `inline`, `cpu-fast`, `cpu-long`, `gpu`, `hpc-poll`, `cron`. Each queue has its own worker pool with its own concurrency cap; per-install tuning.
- **Durable state in the DB.** Broker tracks enqueued/claimed/in-flight; DB holds canonical job state. On worker startup, a reconciliation loop compares the two and re-enqueues anything that should still be running (recovers from crashes — clarifier 4).
- **Cancellation (clarifier 3):** UI sets `jobs.cancel_requested = True` and publishes a Valkey pub/sub message. The worker checks the flag between subprocess `wait()` ticks and sends a kill signal locally, or a cancel call to the HPC scheduler for remote jobs.
- **Retry policy (clarifier 4):** transient errors (network blip, tool-server unreachable) → automatic retry with exponential backoff; tool-level errors (non-zero exit, validation failure) → fail fast, mark `Failed`, no retry. Per-tool retry config.
- **Priority queues (clarifier 5):** Arq priority parameters per submit; combined with the queue split above, gives us "VIP fast lane" without bespoke code.
- **Cron / scheduled jobs (clarifier 2 — maybe):** Arq has built-in cron, so we get this for free. Use it from day one for the obvious cases (HPC status sweeps, retention purge, licence-usage snapshots) and ignore the feature otherwise.
- **HPC tracking** is a `hpc-poll` queue worker with a circuit breaker per HPC backend, so an HPC outage doesn't melt the queue or the web tier.
- **Snappy / sub-second tools** can take an inline path (sync execution inside the request, still recorded in the job history) or be enqueued on a high-concurrency `inline` queue. Decide per-tool.

Implications to carry forward:
- Web process **must never run subprocesses** itself — all execution flows through Arq workers. NiceGUI sessions stay light.
- Workers can run on the same VM (single-box default per A5) or on separate machines (large installs); the only thing that needs to change is the Valkey URL.
- Worker code must be **idempotent** end-to-end so retries are safe. Each task gets a unique `job_attempt_id`; side effects (DB writes, file moves) are guarded against double-execution.
- Tool plugins must expose a **kill-friendly main loop** (regular check-ins / signal-handling) so cancellation is fast and clean.
- Valkey is part of the install footprint: installer/compose bundles it; backup/restore docs cover it (although Valkey state is recoverable from the DB on restart, so it's effectively a cache for queue mechanics).

---

## Project-wide rule: no paid / restrictive-licence dependencies
**No component of MATOFX SPDM may require a paid licence, a commercial agreement, or a restrictive licence (SSPL, RSALv2, BUSL, AGPL, etc.) to deploy at a customer site.**

Acceptable: BSD, MIT, Apache-2.0, ISC, MPL-2.0, LGPL (dynamically linked), and similar permissive OSI-approved licences.

Watch out for:
- **Redis** (post-2024) — use **Valkey** instead.
- **MongoDB Community Server** (SSPL) — avoid; use Postgres if a doc-store-shaped need ever appears.
- **Elasticsearch / Kibana** (post-2021) — avoid; use OpenSearch if needed, or Postgres FTS.
- **HashiCorp Vault** (BUSL since 2023) — avoid as a hard dependency; use OpenBao (the LF fork) or a customer-supplied secrets backend if a real need arises.
- **MinIO** (re-licensed to AGPLv3 commercially) — review carefully if used; prefer S3-compatible adapters that work against any provider.

This rule applies to runtime dependencies, build-time dependencies, and any service we expect a customer to install alongside the app. Whenever a library or service is added to the stack, its licence must be checked and recorded.

### B10. Containers / installer expectations
**Decision: option (c) — containers primary, native installer for customers who can't run containers; native installer Linux-only.**

Delivery shape:
- **Primary path: OCI container images + a `docker-compose` / `podman-compose` file** that brings up the whole stack (web, workers, Valkey, optional Postgres) with one command. Same compose file works on Windows hosts (via WSL2 / Podman / Rancher Desktop) and Linux hosts.
- **Secondary path: native Linux installer** (`.deb` / `.rpm` / `.tar.gz` + systemd unit) for customers whose security policy forbids containers but who run Linux. Bundles a Python runtime, Valkey, and the app via PyInstaller-style packaging.
- **No native Windows installer.** Windows customers use the container path. (Halves the installer surface; sidesteps Windows-service signing, MSI packaging, AV false positives, and Windows-specific Python runtime hassles.)

Container runtime (informed by the no-paid-licence rule):
- We **do not require Docker Desktop**. Document and support, in priority order: **Podman + podman-compose** (Apache-2.0, free for any org size), **Rancher Desktop** (Apache-2.0), and **Docker Engine on Linux/WSL2** (free for any size — only Docker Desktop has the company-size restriction). All speak OCI; our images and compose file run on any of them.
- The compose file uses **Compose v2 spec**, which both Docker Compose and Podman Compose understand.

App OS-compatibility (separate from packaging):
- The **app itself must run on both Linux and Windows hosts** — even though the installer is Linux-only, the app/container is regularly run on Windows hosts (via container runtimes) and on Linux. No OS-specific paths, syscalls, or shells in app code:
  - Use `pathlib.Path` for paths; never hand-roll separators.
  - Subprocess code must handle Windows + POSIX differences (signal handling, exit codes, no `os.fork` assumptions, no shell-only helpers).
  - File-locking must use a cross-platform primitive (or `portalocker`-style abstraction).
  - No reliance on Linux-only filesystem features (case-sensitivity, symlinks, fifos, `chmod`-as-permission).
  - No Windows-only tooling (PowerShell-as-shell, CMD-specific quoting) baked into core paths.
- This applies inside containers too — base our images on a Linux distro that works equivalently on Windows-host runtimes (no host-OS coupling beyond what the runtime guarantees).

Updates / upgrades:
- **Periodic, on-command** — never self-updating. Admin pulls a new version when ready and runs a single command (`docker compose pull && up -d` or the installer's upgrade subcommand). The app must not phone home or check for updates.
- Upgrades must preserve all state: DB (file or external), job-storage volume, Valkey state (best-effort — recoverable from DB anyway), config, secrets, audit log, licence material.
- Migrations (Alembic, per B8) run automatically on app startup against the new schema; failures abort the start and revert to the previous version.
- Rollback path documented: keep the previous image / installer artefact and a DB snapshot taken immediately before the upgrade.

Air-gapped installs (confirming C15 ahead of time):
- **Every install is treated as air-gapped.** No outbound calls to PyPI, Docker Hub, GitHub, package mirrors, or licence servers at install or upgrade time.
- Release artefact is **a single tarball** containing: pre-built OCI images (`docker save`/`podman save`), or the native installer; the compose file / config templates; release notes; checksums + signatures.
- All Python dependencies are **vendored** into the app image / installer at build time — no runtime `pip install`.
- Documentation walks the admin through `docker load < matofx-vX.Y.Z.tar` then `docker compose up -d`.

Implications to carry forward:
- Build pipeline (CI per H41) produces both a container bundle and a Linux installer from the same source, on every release.
- Cross-platform testing is part of CI: matrix runs on Windows + Linux hosts, against both runtimes (Docker Engine, Podman) for the container path.
- The compose file becomes a real artefact, version-controlled and shipped, not "an example" — it *is* the supported runtime topology for tiny installs.
- Anything that needs the host OS (e.g. GPU device passthrough) must be declared cleanly in compose so the install docs can explain runtime-specific knobs.
- Even though the installer is Linux-only, the **app codebase remains OS-agnostic** — we don't get to take Linux-only shortcuts.

### B11. Anything to keep from legacy

**Domain concepts to keep (port directly):**
- **Project → Scenario → Job/Workflow** hierarchy is the spine of the data model.
- **Tool plugin model**: tools live in their own directory with a `*_descriptors.json` (inputs, layout, retain/delete file lists) plus a `static/` folder of files copied per job. Format is preserved; **how tools are executed is modernised** (per A4/A6/B9 — workers in Arq, with pluggable execution backends including remote tool servers, HPC, etc.).
- **Workflow "master" pattern**: a workflow composes multiple tool tabs and chains outputs to downstream inputs. Same pattern used for **custom workflows** (user-defined variants).
- **Custom workflows** with per-workflow permissions are kept.
- **Approvals** (job-level approver sign-off, before/after execution as the tool dictates) are kept.
- **Permissions matrix** (feature × access-right) drives UI nav and API authorisation. Kept conceptually; the schema/model will be tightened in v2 (no JSON-strings-in-columns, proper FK relationships).

**To remove completely:**
- **DDP wrapper** (`legacy_code/DDP/`). Ansys-vendored, copyright-encumbered, doesn't fit our no-restrictive-licence rule. Replaced by a clean MATOFX-owned tool I/O contract (see open items below).
- **Snapshots / archives**: `tools - Copy/`, `workflows - Copy/`, `tools_archive/`, `database/staging_database - Copy.db`. Not ported.
- **Two-process Streamlit + FastAPI split.** v2 is one app process (NiceGUI on FastAPI) + Arq workers.
- **`session_state` as global config.** Replaced by typed config objects + dependency injection.
- **`inactivity_check.py` subprocess.** Replaced by proper session/idle handling in the web tier.
- **Legacy schema as-is.** Rewritten with proper FKs, constraints, and no JSON-encoded lists in columns.

**Redesign needed (recommendations recorded below):**
- **FreeCAD bridge** — replace with a Python-native CAD library that can read STEP and STL and support interactive viewing (zoom, rotate, recolour).
- **3D rendering** — keep the capability; pick a clean Python + browser stack.
- **Licence audit log** — keep the *concept* (tamper-evident, hash-chained, signed); rewrite the code from scratch.
- **Licence binding model** — open to a different approach than legacy hardware fingerprinting.
- **Password hashing** — must remain hashed; modernise the algorithm.

**Tool I/O contract (DDP replacement) — confirmed:**
- A small, MATOFX-owned Python wrapper that gives tool authors the same UX the DDP wrapper provided: read `inputs.json`, write `outputs.json`, log structured events, and surface failures cleanly.
- JSON-Schema validation of inputs + outputs (per F30, ahead of time).
- Explicit, documented contract — no "Ansys vendor" baggage.
- Same wrapper used by every tool (local subprocess, remote tool server, HPC).

**3D / CAD rendering — confirmed stack:**
- **Server-side STEP parsing & tessellation:** **OCP** (`cadquery-ocp`, Apache-2.0 OpenCascade bindings). Reads STEP, tessellates to triangle meshes the browser can render. Fully Python-native, fits the no-restrictive-licence rule.
- **Mesh handling (STL + general):** **trimesh** (MIT). Loads STL, OBJ, GLB; handy for transformations, bounding boxes, exporting.
- **Browser rendering:** **NiceGUI `ui.scene`** (three.js, MIT, bundled with NiceGUI — no Node, no extra licence). Built-in pan/zoom/rotate, per-object material/colour, click/hover events. Surface a Python API that takes meshes from OCP/trimesh and pushes them into the scene.
- **Pipeline:** uploaded STEP → server reads with OCP → tessellate → push triangle data over the NiceGUI WebSocket → user interacts in the browser. STL bypasses the OCP step (trimesh only).
- **Export:** glTF/GLB output via trimesh for download/storage; legacy `pygltflib` and `pyrender` are dropped.

**Licence binding & audit log — confirmed:**
- **Offline-signed licence files** only (air-gap requirement, B10/C15). No phone home, no online activation.
- **Asymmetric crypto:** Ed25519 via PyNaCl (`libsodium`, ISC/MIT). Funis holds the private key; the app embeds the public key. A licence is a JSON manifest (customer, expiry, feature flags, max users, etc.) + signature.
- **Soft hardware binding** (optional, configurable): bind to a stable OS-level machine identifier — `/etc/machine-id` on Linux, `HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid` on Windows. These persist across reboots, do not depend on MAC/disk serial (which break on cloud VMs), and are stable enough for our use. If the customer needs to migrate hosts, Funis re-issues the licence file.
- **Hash-chained, signed audit log:** append-only file, each entry contains the previous entry's hash, the event payload, and an Ed25519 signature using a key derived deterministically from licence id + machine id. Concept inherited from the legacy implementation; code rewritten from scratch with proper schema, rotation, and verification tooling.
  - **AMENDED 2026-08-09 by ADR-0019, revised 2026-08-11/12** — the *derived* key is superseded and must not be built. An install that can derive its own signing key hands the licensee a forgery oracle: both inputs (licence id, machine id) are known to the box. What shipped instead is a random per-install key signing the chain in the DB (not a file). Compliance **reports** are signed with a **Funis-issued** key delivered inside the signed licence, and evidential weight rests on **anchoring**, not on the signature alone.
    - The anchoring mechanism named in the original amendment (comparing chain *heads*) was itself replaced on 2026-08-11 by **RFC 6962 Merkle consistency proofs** (report schema v2). Head comparison could not distinguish a report that was never delivered from a history that was rewritten, and permanently accused any install that generated a report and did not send it.
    - **Install identity is generated by the install**, not issued by Funis (2026-08-11, §A3): a licence is issued *for* an id the install already minted. The soft machine binding described above survives only as an **optional licence field**, never as identity — it breaks on container rebuilds, which is why identity is DB-persisted instead.
    - As-built detail: ARCHITECTURE §16.47–16.65. Orientation + the closed gap list: `designs/licence-and-compliance-overview.md`.
- **Verification CLI** (Funis-side, possibly customer-side too) reads the chain and confirms continuity + signatures. *(Shipped: `matofx audit verify` + `POST /api/admin/audit/verify`. ADR-0019 adds `matofx compliance report` / `matofx compliance verify` for the vendor-facing artefact.)*

**Password hashing — confirmed:**
- **Argon2id** via `argon2-cffi` (MIT). OWASP's current top recommendation; memory-hard; resists GPU/ASIC attacks far better than bcrypt.
- Default parameters per OWASP: `time_cost=2`, `memory_cost≈19 MiB`, `parallelism=1`; tuneable per install.
- Wrap in a small password-policy module with **upgrade-on-login** semantics: if a hash uses old params or older algorithm, transparently rehash with the new params on next successful login. Lets us tighten params over time without forced password resets.
- All hashes stored as PHC-formatted strings (so the algorithm + params live in the hash, supporting future migration).

Implications to carry forward:
- A clean MATOFX-owned tool wrapper is one of the first things built in v2.
- 3D rendering is a server-side mesh pipeline + NiceGUI scene, not a wrapped desktop renderer. Plan a small `viewer/` module.
- Licence material (private key on Funis side, public key embedded in app, audit-log signing key **randomly generated** per install — *not* derived, per B11 as amended) is part of the build/sign pipeline. **Settled 2026-08-12:** the issuing private key lives offline in `scripts/issue_licence.py`'s encrypted keystore with a never-used backup as the only revocation path, and the trusted public halves are baked into the build at release time (`compliance/issuer.py`). Procedure: `designs/funis-vendor-runbook.md`.

---

## C. Installation & operations

### C12. Who is the installer
**Decision: (d) mix, but most often (c) Funis-led.** Some installs are run end-to-end by Funis (you/your team) on the customer's server; others are run by the customer's own IT/DevOps team with Funis remote support. A purely "non-technical domain expert installs it" path is **not** in scope for v1.

Operational assumptions:
- **A privileged operator (root / Administrator) is always present** during install — either Funis on-site/remote, or the customer's sysadmin. No "drop a file in a share" install workflow.
- **TLS:** must support **both** customer-provided certificates (internal CA, often the common case in air-gapped enterprise environments) **and** a self-signed cert generated at install (sensible default). Document a `matofx tls install /path/to/fullchain.pem /path/to/key.pem` style command for swapping in customer certs later. Public ACME / Let's Encrypt is not assumed (most installs are intranet-only).
- **DNS / access pattern:** typical access is via a **customer-managed vanity URL** (e.g. `matofx.customer.com`) resolving to a fixed IP. Users connect from corporate laptops, directly on the LAN or via VPN / Zscaler / similar zero-trust gateway. The app must:
  - Be reachable on a configurable hostname (not bound to a hard-coded one) — needed for IdP redirect URLs and HSTS to work cleanly.
  - Tolerate proxies in front (X-Forwarded-* headers handled correctly; trust list of upstream proxies configurable).
  - Not assume direct internet exposure — never call out to public services for routine operation (per air-gap rule).
- **First-run configuration:** **everything via config files** for v1. No web wizard yet. The install hands the admin a starter `matofx.toml` (or similar) with sensible defaults and clearly-marked required fields (DB URL, Valkey URL, hostname, secret keys, licence file path, SMTP, IdP). A friendly **first-run web wizard** is on the v2-after-v1 roadmap, not blocking.
- **Initial admin user** is created via a one-off CLI command after first start (`matofx admin create-first-user --email ... --name ...`), which prints a one-time-use enrolment link the user opens in the browser to set their password. (Replaces the legacy "first user fills a form on the home page" pattern, which was awkward and time-of-check-time-of-use racy.)

Implications to carry forward:
- Install docs must be **clear, terse, copy-pasteable** — written for a knowledgeable operator, not a wizard-led flow. Step-by-step commands in a fixed order; no GUI screenshots required for v1.
- The **config file is the source of truth** for everything except live DB state. Validate it on startup; refuse to start with clear, actionable errors when invalid.
- We need a **`matofx` admin CLI** for: first-user creation, licence install, TLS swap, backup/restore, smoke-tests, log dump, version info. CLI is internal — not a public product, just the operator's interface.
- A **`matofx doctor`** style command (validate config, check DB/Valkey reachability, test SMTP, test IdP, check licence) is high-value for both Funis-led installs and customer-IT-led installs; commit to building it early.
- **Reverse-proxy aware** from day one. Document and test behind nginx, Caddy, Traefik, Apache; provide example config snippets in install docs.
- Hostname/URL configuration is a single setting (`PUBLIC_BASE_URL=https://matofx.customer.com`); used by IdP redirects, email links, audit-log entries, and CSRF/CORS policy.

### C13. Update mechanism
**Decision summary:** whole-bundle upgrades, scheduled maintenance windows, hard-stop in-flight jobs, restore-from-backup as the rollback story, signed releases but no version-skew enforcement. Release cadence ~1–2 per year.

1. **Upgrade unit: whole bundle (a).** A release is one signed tarball; web + workers + Valkey + bundled binaries upgrade together. No mix-and-match. Simplifies the test matrix to a single line: `vX.Y.Z` works as a coherent set.
2. **Maintenance window (i).** App stops, upgrade runs, app starts. Admin pre-announces the window. Target end-to-end is "minutes, not hours"; the ~1-day tolerance from A5 is the worst-case ceiling, not a goal. No online-migration heroics.
3. **In-flight jobs: hard-stop by default (a).** `matofx upgrade apply ...` kills running local subprocess jobs at the start of the window; HPC-tracked jobs continue on the HPC and reconcile their state when the app comes back (the reconciliation loop from B9 already covers this).
   - **Optional `--drain --max-wait <duration>` flag** for admins who'd rather wait out short jobs before stopping; after the timeout, falls back to hard-stop. Implemented as an opt-in convenience, not the default.
4. **Migrations and rollback (as recommended):**
   - Forward migrations are automatic (Alembic on startup); failure aborts the start.
   - **Rollback = restore-from-backup**, not downward migrations. Each `matofx upgrade apply` automatically takes a pre-upgrade snapshot (DB + job-storage volume + config + licence material). The output prints the snapshot path and the exact `matofx restore <snapshot>` command needed to roll back.
   - We do **not** maintain `down_revision` Alembic logic for production rollback; it's far too easy to ship buggy down-migrations.
5. **Signed releases: yes. Version skew: no enforcement.**
   - Every release tarball is **Ed25519-signed by Funis** (same key infrastructure as licence files, per B11). `matofx upgrade verify` checks signature + checksums before doing anything else; `matofx upgrade apply` re-checks before swapping images.
   - **No skip-version enforcement.** Customers may go from any released version directly to any newer released version. Migrations must be cumulative and idempotent so this stays safe — design constraint on the team rather than a runtime guard.
   - Implication: every Alembic revision must remain replayable from any prior schema version we've ever shipped. Removing a revision is forbidden after release.

**Release cadence:** **1–2 releases per year** as the planning assumption.
- Implies each release is a "minor" or "feature" release, not weekly point fixes; we can invest more in each one and amortise the upgrade overhead.
- Critical-security patches between releases will still occasionally be needed — same upgrade UX, just a smaller diff. We don't need a separate "hotfix" channel for v1.

**Locked upgrade UX (CLI shape):**
```
# verify a bundle (signature, checksums, config compatibility) — read-only, safe to run anytime
matofx upgrade verify /path/to/matofx-vX.Y.Z.tar.gz

# apply a bundle (the actual upgrade)
matofx upgrade apply /path/to/matofx-vX.Y.Z.tar.gz [--drain --max-wait 30m]
  1. signature + checksum + bundle-integrity check
  2. config + licence compatibility check
  3. pre-upgrade snapshot (DB + storage + config + licence) → prints snapshot path
  4. drain (if --drain) up to --max-wait, else proceed
  5. stop workers, stop web
  6. swap images / binaries
  7. run Alembic migrations
  8. start web + workers; health-check
  9. print summary + the exact rollback command for the snapshot just taken

# rollback (manual; admin-initiated)
matofx restore /path/to/snapshot-<timestamp>.tar.gz
```

Implications to carry forward:
- Build/release pipeline (H41) produces a single signed tarball per release containing: container images (saved), Linux installer artefacts, compose templates, default config, release notes, checksums, and an Ed25519 detached signature.
- Snapshot/restore tooling is part of the core install, not an optional add-on; backups (C14) and upgrade snapshots share the same machinery.
- Alembic migration discipline: cumulative, idempotent, never deleted post-release; CI runs every migration chain end-to-end against both SQLite and Postgres on every PR.
- The upgrade CLI must be safe to run repeatedly; partial-upgrade recovery is "restore the snapshot, fix the bundle, retry" — there's no "resume halfway through" mode.

### C14. Backups
**Decision: shared responsibility — MATOFX produces consistent, configurable, deduplicated backups; customer IT handles long-term offsite retention. Encryption is optional, storage destinations are configurable, both local and remote backends supported.**

1. **Responsibility (c) — both.** MATOFX produces application-level consistent backups on a schedule and writes them to one or more configured destinations. The customer's IT is responsible for any further offsite shipping, retention beyond the configured local horizon, and disaster-site copies. We deliver clean handoffs; we don't try to be a full DR product.

2. **Backup contents — configurable.** Defaults match the C13 snapshot scope:
   - Database (`pg_dump` for Postgres / `VACUUM INTO` for SQLite, applied at point-in-time consistency).
   - Job storage volume (per-job working dirs: inputs, outputs, retained files).
   - Config (`matofx.toml`).
   - Licence material (licence file + audit-log chain).
   - Excluded by default: Valkey state (transient — reconciled from DB), per-process logs (replaceable), container images (recoverable from the release tarball).
   The **`backup.include` and `backup.exclude` lists in the config file** let customers add or drop components per install (e.g. omit job storage when an external blob store already holds it; include a customer-supplied extra path).

3. **Schedule:** Arq cron (per B9). Defaults to a daily full snapshot; configurable to e.g. hourly DB-only + daily full + weekly full. All schedules expressed in the config file with cron syntax.

4. **Format:** signed `.tar.gz` (or restic repo, see point 5). Always includes a manifest (`manifest.json`) listing components, sizes, source paths, app version, schema revision, timestamps, optional licence id. Manifest is Ed25519-signed by the running install (its **per-install audit signing key** — the same key that signs exported tool/workflow bundles; *not* derived from licence material, per B11 as amended) so a backup can be integrity-verified offline before restore: `matofx restore verify <path>`.

5. **Deduplication / deltas — recommended tool: `restic`.**
   - Job working directories can be many GBs each; "tar everything every day" doesn't scale. Native incremental + content-addressed dedupe is required.
   - **`restic`** (BSD-2-Clause, single static Go binary, no per-customer licence) is the right fit: chunked content-addressed store, dedupes across snapshots, fast incrementals, optional encryption, supports local + SFTP + S3-compatible + Azure Blob + GCS + B2 + rclone backends out of the box. Licence-clean per our project-wide rule.
   - **Bundling:** the restic binary ships inside the MATOFX install (in the container image and the Linux installer); the admin never invokes restic directly. The MATOFX `backup` CLI wraps it.
   - **Pure-Python fallback (no restic):** if a customer policy forbids bundling a non-Python binary, we provide a simpler "tarball + dir-tree dedupe via hard links" mode behind a config flag. Less efficient, but plain Python. Default is restic.
   - **Config:** `backup.engine = "restic"` (default) | `"tarball"`. `backup.dedupe = true` (only meaningful for restic).

6. **Encryption:** optional, configurable.
   - `backup.encryption.enabled = false` is the default for v1 to keep the lossy-passphrase risk explicit.
   - When enabled, restic's native repository encryption (AES-256-CTR + Poly1305-AES) is used; the passphrase is loaded from a customer-controlled source (env var, file, or OS keychain — see E25).
   - The CLI prints a prominent warning when first enabling encryption: "If you lose this passphrase you lose every backup. Escrow it now."

7. **Storage destinations: both local and remote, multiple at once.**
   - Default: a local directory on the host (configurable, e.g. `/var/lib/matofx/backups` or a host-mounted volume).
   - Optional remote destinations configurable as a list: any restic-supported backend (S3 / Azure Blob / GCS / SFTP / B2 / rclone-anything), or for the tarball mode, a local-mount path the admin sets up (NFS / SMB share, etc.).
   - Each destination is a separate config block with its own credentials, schedule, and retention policy. Multiple destinations can run in parallel (primary local + secondary remote is the recommended pattern).

8. **Retention: configurable per destination.**
   - Defaults: "keep last 7 daily + 4 weekly + 12 monthly". Customer can override.
   - Old backups are pruned by the same scheduled job that creates new ones.

9. **DB consistency.** A backup run snapshots the DB first to a temp file (`pg_dump` directory format / `VACUUM INTO`), then snapshots the job-storage tree (which doesn't need DB-level consistency — files are immutable once a job is `Complete`). This avoids long-running DB locks even on large installs.

**Locked CLI shape:**
```
matofx backup run                        # one-off backup now (uses configured destinations)
matofx backup list                       # list snapshots across destinations
matofx backup verify <snapshot-id>       # verify integrity + manifest signature
matofx backup prune                      # apply retention policy now (also runs on schedule)
matofx restore <snapshot-id>             # full restore (replaces install state)
matofx restore verify <snapshot-id>      # dry-run; checks integrity + version compat only
```

Implications to carry forward:
- restic binary is bundled in container images and Linux installers; build pipeline pins a specific restic version + checksum + verifies it at install time.
- Backup destinations and credentials live in the config file; secrets follow the rules in E25.
- Pre-upgrade snapshots (C13) and routine backups share the same engine — one backup mechanism, one restore mechanism.
- Restore is destructive: `matofx restore` stops the app, replaces state, restarts. Print a clear confirmation prompt; document explicitly. Use snapshot signature verification before any state is touched.
- Test plan (per H40) includes restoring a real backup into a fresh VM as a CI step — backups that can't be restored are worse than no backups.

### C15. Air-gapped installs
**Decision: every install is treated as air-gapped. No outbound network at runtime by default. All assets bundled. Releases delivered to each customer through a private, secure channel — no public download portal.**

1. **Runtime outbound policy: deny by default.**
   - No telemetry, no error reporting, no version-check, no analytics, no third-party CDNs, no font/library fetching, no licence phone-home.
   - The only outbound connections the app ever makes are **explicitly configured by the admin** and reach customer-controlled destinations:
     - The customer's IdP (SAML / OIDC).
     - The customer's SMTP server.
     - The customer's HPC scheduler (Slurm / PBS / HPC Pack / Rescale).
     - The customer's Valkey/Postgres if hosted externally.
     - Customer-installed remote tool servers (per A6).
   - The codebase has **no hard-coded external URLs**. CI lints for accidental external hostnames (`github.com`, `pypi.org`, `cdn.*`, `fonts.googleapis.com`, etc.) in source, templates, and bundled assets.
   - At runtime, an outbound-host **allow-list** is computed from config; egress to anything not on it is refused at the HTTP-client layer (one shared client wrapper, not per-callsite).

2. **All UI assets are bundled.**
   - NiceGUI's bundled Quasar/three.js/fonts ship inside the Python package.
   - We do not introduce any `<link>`/`<script>` tags pointing at external CDNs in any custom HTML/JS we inject.
   - Fonts, icons, and any third-party JS we add (e.g. extra three.js loaders for STEP-derived meshes) are vendored into the app's static directory at build time and verified offline.
   - CI checks built artefacts for external URL references (regex sweep over the final image's static tree); release fails if any non-allowlisted URL is found.

3. **Release distribution: option (b) — direct, per-customer secure delivery.**
   - **No public download portal.** Funis does not publish release tarballs at a publicly reachable URL.
   - Each release is delivered to each customer through a **private, authenticated channel** — typical examples (the customer/Funis decide per relationship): secure file-transfer service with per-customer credentials, customer-specific SFTP drop, encrypted email, secure data room (e.g. customer-supplied secure share), or physical media.
   - The release tarball is **always Ed25519-signed** by Funis (per C13) regardless of channel; the customer verifies the signature with the embedded public key before applying. The signature is the trust anchor — the channel only controls who *receives* the artefact.
   - Funis maintains a **per-customer release-delivery record**: which customer received which version, when, by whom, hash of artefact delivered. Used for support, security-advisory notifications, and licence-compliance tracking.
   - **No anonymous / shareable URLs.** This rules out "shared S3 link" or "public funisconsulting.com download page" approaches.

Implications to carry forward:
- A small **release-delivery workflow** is needed on the Funis side: signed tarball production, per-customer packaging if needed (e.g. embedding a customer-specific licence file), secure transport choice, delivery log. Out of scope for the v1 product itself but part of release engineering (see H41).
- Funis is committing to never running a public download portal for MATOFX releases. If that ever changes, customer agreements / security posture must be renegotiated first.
- The "no outbound at runtime" rule is an explicit **security property** of the product — call it out in the security review (cross-link to E22+) and in customer-facing docs as a feature.
- Egress allow-listing is a built-in mechanism, not just a recommendation. Implementing it once (single shared HTTP client) is cheaper than auditing each callsite forever.
- Vendor any binary, asset, or resource we use into the build at the artefact level, not via "first-run download" patterns.

### C16. Licensing model
**Decision: two licence types, single product (v1) with feature-flag mechanism built in for future modularity, signed JSON manifests, periodic enforcement, YubiKey-backed Funis signing, no trials — paid PoCs use ordinary time-limited licences.**

**Two licence types — both have unlimited registered users; differ in what consumes a slot:**

- **Concurrent Session Licence (CSL).**
  - Unlimited users may be **registered**, all of whom hold access rights.
  - Licence specifies **N concurrent sessions**.
  - A slot is **consumed at login** and **released at logout / session timeout / explicit revocation**.
  - User attempting to log in when N slots are full → refused with a clear "all sessions in use" message. (Optional UX later: queueing or "kick a stale session" tooling for admins.)
  - Operationally fluctuating — slot counter changes minute-to-minute.

- **Floating Access Licence (FAL).**
  - Unlimited users may be **registered**.
  - Licence specifies **N access-rights assignments**.
  - A slot is **consumed when the admin assigns the floating right to a user**, **released when the admin revokes it.**
  - Users without an assignment exist in the system (their account, projects, settings persist) but **cannot log in**.
  - All N assignees may be logged in concurrently.
  - Operationally stable — slot counter changes only on admin actions.

A given install runs **one** licence type at a time; the licence manifest declares which.

**Feature gating: single product for v1, with a working flag mechanism from day one.**
- Every install gets all features enabled by default.
- The licence manifest carries a `features` map (e.g. `{ "hpc": true, "custom_workflows": true, "advanced_reporting": true }`); the app reads it but ships the manifest with everything `true`.
- **Cost:** ~50 LOC and a config block, not a feature subsystem. Lets us move to modular add-ons (option (c)) later by simply issuing manifests with selective `false` values, without re-engineering anything.
- We do **not** build pricing-tier UX or marketing tier names ("Starter / Pro / Enterprise") into the product — those are commercial-side concerns layered on the same machinery later.

**Expiry behaviour:**
- Every licence has an `expires_at` field.
- **Grace period** (configurable in the manifest, default e.g. 30 days): big banner warnings in the UI, repeated email warnings to admins; functionality unchanged.
- After grace: **read-only mode** — users can log in and view existing data; cannot submit jobs, edit data, or change admin settings. New user logins blocked once read-only is active (avoids burning concurrent-session slots reading old reports).
  - **CLARIFIED 2026-08-09 by ADR-0019** (the two sentences above conflicted): every **already-registered** user may sign in and **view results only**. What "blocked" means is *no new user registration* and *no access-rights / role changes* — not a login block. CSL slot accounting is **suspended** while read-only, since there is no metered work to protect. Separately, an **invalid** licence (bad signature / malformed / machine-binding mismatch) is not the expiry case: it **fails closed** and refuses to serve.
- **Never a hard stop.** A flaky email pipeline missing renewal reminders must not take down a customer's simulation org.
- All transitions logged in the tamper-evident audit log.

**Enforcement intensity (per-install):**
- **Periodic re-check** of: signature validity, expiry, soft machine-id binding (per B11), licence-type consistency, and slot caps.
- Schedule: at app startup; every N minutes via Arq cron (default 60); at every login (cheap call against in-memory cached state).
- Tamper-evident audit log records every licence event (validation success/failure, slot consumed/released, assignment changes, expiry transitions, admin overrides).
- **No continuous metering at every action** — relationship + contract + audit log handle the rest.

**Trials / PoCs:**
- **No public/anonymous trials**, ever — matches SPDM industry practice.
- **Pilot / Proof-of-Concept deployments** are run with a **normal MATOFX licence** that has a shorter `expires_at` and (optionally) reduced N. Same machinery, no separate "trial mode" code path or UI.
- A `licence.banner_text` optional field in the manifest is reserved for future use (e.g. "EVALUATION — do not use for production data") but not implemented in v1.

**Issuance & delivery:**
- A licence is a **single signed JSON file** (`<customer>-<env>.matofx-licence.json`).
- Manifest fields: `licence_id`, `customer`, `environment`, `licence_type` (`csl`|`fal`), `n_slots`, `issued_at`, `expires_at`, `grace_period_days`, `features` (map), `machine_binding` (optional, soft fingerprint), `banner_text` (optional).
- Signed with **Ed25519** by Funis; the public key is embedded in the app build.
- Delivered through the **same private per-customer channel** as release tarballs (per C15) — no public download portal, no anonymous URLs.
- The admin places the file in a configured directory; the app picks it up on the next periodic re-check (or `matofx licence reload`).
- A `matofx licence install <path>` CLI verifies signature, expiry, and machine binding before activating; refuses to install an invalid file.

**Funis signing-key infrastructure:**
- **Hardware-backed Ed25519:** YubiKey 5 NFC (preferred) or Nitrokey Pro 2 (alternative — open-source firmware). Generated on-device; **non-extractable**. PIN-protected.
- **2 production keys minimum** (one primary, one offline backup, both holding the same key material) plus an **optional third "vault" key** stored offsite for disaster recovery.
- **Same key signs both** licence files and release tarballs.
- **One-time cost ≈ $100–200 USD** to Funis; zero customer cost; zero ongoing cost.
- **Signing CLI** (`matofx-sign licence ...`, `matofx-sign release ...`) talks to the YubiKey via PIV/OpenPGP; produces detached signatures alongside the artefact.
- **Key rotation:** if the primary key is suspected compromised, rotate to a new keypair; existing licences remain valid (still signed by the old key, which the app keeps in a "previously trusted" set until customers naturally rotate to fresh licences). Plan for graceful key rotation in the v1 design even though we don't expect to use it.

Implications to carry forward:
- The `licence` module in the app is the **single authority** on "what is the customer allowed to do?" — it's queried by the auth layer (login slot allocation), the admin UI (who can be assigned a floating slot), and the read-only-mode middleware. No business logic checks the licence directly.
- The audit-log writer (per B11) is shared between licence events and any other tamper-evident events the system needs (admin actions, approvals, etc. — see E26).
- Funis-side release engineering must wire up the YubiKey signing flow into the build pipeline (per H41) and produce a clean, signed artefact for every release.
- The licence schema is versioned; future licence files can carry new fields without breaking older installs (forwards-compatibility) and older licence files keep validating on newer apps (backwards-compatibility).

---

## D. AuthN / AuthZ

### D17. Authentication providers
**Decision: local accounts + SSO (SAML 2.0 *and* OpenID Connect). Per-install configuration: either "local-auth mode" (no SSO) or "SSO mode" (with a small set of break-glass local admins).** SAML was aspirational in the legacy app and is now first-class.

**Modes (one is active per install, configured at install time, switchable later):**

- **Local-auth mode (no SSO configured):**
  - Every user (including non-admins) is a **local account**.
  - **Admin creates the account** in the MATOFX admin UI — including a temporary password (or the system generates one and shows it to the admin once).
  - **First-login forced password change**: the user must change their password before doing anything else.
  - Passwords are Argon2id-hashed (per B11) with PHC-formatted strings; upgrade-on-login keeps params current.

- **SSO mode (SAML *or* OIDC configured):**
  - **Non-admin / non-super-user accounts authenticate via SSO only.** No password is set, no password login screen for these users.
  - **Super-user / admin accounts can additionally be local** — they exist as **break-glass** identities so the install is recoverable when the customer's IdP is unavailable. These local admin accounts are loudly flagged in the UI and audit log; their usage requires MFA (per D18).
  - Customers pick **either SAML or OIDC** for their IdP integration. Both are supported; only one is configured per install (a customer may mandate one or the other). Switching protocols is an admin action with the usual reissue-of-MATOFX-side-mappings cost.

**Provisioning model: pre-provisioning required.**
- An admin creates the MATOFX user record **before** that user can log in — both in local-auth mode and SSO mode.
- In SSO mode, the admin enters the user's identity attribute(s) (email + a stable IdP user identifier — `NameID`/`sub` — once known); the IdP claim must match on first login.
- **No auto-provisioning on first SSO hit.** Random people in the customer's IdP cannot create MATOFX accounts by clicking the login URL.
- Bulk import (CSV) for initial onboarding is a useful CLI/admin-UI helper. SCIM is not in scope for v1.

**Role mapping: MATOFX-side only.**
- The IdP **identifies** the user (which person is this?); MATOFX **authorises** the user (what can they do?).
- We do **not** read group claims to assign roles. An admin assigns each MATOFX user their access right and any team membership inside MATOFX.
- The IdP's `email` and `sub` (or SAML `NameID`) are the only claims we trust. Extra claims (groups, names) may be displayed informationally but never grant rights.
- Rationale: prevents accidental privilege escalation when the customer reorganises their IdP groups; keeps MATOFX's audit log self-contained.

**Library choice (licence-clean per project rule):**
- **SAML 2.0:** `pysaml2` (Apache-2.0). Supports SP-initiated and IdP-initiated flows, NameID formats (persistent + email-address common), signed/encrypted assertions.
- **OpenID Connect:** `authlib` (BSD-3-Clause). Authorization-code flow with PKCE for the web UI; ID-token signature verification; userinfo endpoint optional.
- **Local password hashing:** `argon2-cffi` (MIT) — already locked in B11.

**Identity matching rules:**
- **OIDC:** match by `sub` claim (immutable, IdP-issued unique identifier). `email` is informational; helpful for admin readability. Email-only matching is a fallback during initial pre-provisioning when the admin doesn't yet know the user's `sub`; the first successful login binds the `sub` to the MATOFX user record permanently.
- **SAML:** match by `NameID` with `persistent` or `emailAddress` format (configurable per install — depends on what the customer's IdP issues). Same first-login-binding behaviour.
- We never match by display name or by claims that can change.

**Session management:**
- After successful authentication (local or SSO), MATOFX issues its **own server-side session** (HTTP-only, secure, SameSite=Lax cookie + a server-side session record in the DB). NiceGUI's WebSocket session is bound to this.
- Session lifetime configurable per install; defaults conservative (e.g. 8h hard, 30min idle).
- IdP `SLO` (Single Logout) supported but not required (it's flaky in practice); the local logout always terminates the MATOFX session.

**Configurable per install (in `matofx.toml`):**
```
[auth]
mode = "sso"           # "local" | "sso"
allow_local_admins = true   # only meaningful in sso mode
session_idle_minutes = 30
session_hard_hours = 8

[auth.sso]
protocol = "oidc"      # "oidc" | "saml"

[auth.sso.oidc]
issuer = "https://login.customer.com/realms/main"
client_id = "matofx"
client_secret_env = "MATOFX_OIDC_CLIENT_SECRET"
scopes = ["openid", "email"]

[auth.sso.saml]
metadata_path = "/etc/matofx/idp-metadata.xml"
sp_entity_id = "https://matofx.customer.com"
nameid_format = "persistent"   # | "emailAddress"
sign_authn_requests = true
require_signed_assertions = true
```

**Out of scope for v1 (recorded as future work):**
- SCIM provisioning, just-in-time auto-provisioning, IdP group → MATOFX role mapping, social login (Google/Microsoft personal), public registration, password reset via email-only (admin-mediated reset only in v1).

Implications to carry forward:
- The auth subsystem is **one module** with two pluggable identity sources (local, SSO) and one common session/identity model. Business logic only sees `current_user`, never an "is-this-SSO-or-local?" flag.
- `matofx admin create-first-user` from C12 also handles "create the first local admin in SSO mode" use case — the bootstrap path is identical.
- The audit log (per E26) records every authentication attempt — successful or not — with enough metadata to investigate suspicious patterns. Break-glass local-admin logins in SSO mode are flagged at log time.
- The licence layer (per C16) integrates with the auth layer at login time: CSL slot allocation happens after successful authentication and before session establishment; FAL pre-checks happen at session-establishment too.
- Customer-side IdP setup is documented end-to-end for at least Microsoft Entra ID (OIDC + SAML), Okta, and Keycloak — the three most common in our audience.

### D18. MFA
**Decision: TOTP-only in v1, mandatory and non-disableable for super-users/admins, off for normal local-auth users; in SSO mode, normal users' MFA is the customer IdP's concern. Recovery codes are the only fallback; lost-device + lost-codes is the customer's problem.**

**Single unified rule (covers both modes):**

> **Any account that holds super-user or admin privileges must enrol in TOTP. Period. There is no per-user toggle to disable it.**

- In **local-auth mode**: admins must enrol; non-admin users do not need MFA (password only).
- In **SSO mode**: the **break-glass local admin** accounts are by definition super-user/admin accounts (per D17), so the rule applies and is **mandatory** for them. Normal users authenticate via SSO; their MFA is enforced by the customer's IdP — MATOFX does not impose a second factor on top.

**Factors supported in v1:**
- **TOTP only** (RFC 6238). Standard 30-second window, 6-digit codes, SHA-1 (the spec). Works with Google Authenticator, Authy, 1Password, Bitwarden, etc.
- Air-gap-safe (no SMS, email codes, push notifications, or external services).
- WebAuthn / hardware keys / passkeys are **deferred** to a future release; the auth module is built so adding them later is an extension, not a rewrite.

**Enrolment flow:**
- On first login (or first time the user is granted admin privilege), the user is presented with a QR code (otpauth URI) + a numeric secret fallback.
- The user must enter a valid TOTP from their authenticator before enrolment is finalised.
- Immediately after enrolment, the system displays **10 single-use recovery codes** that the user must save / print (clarifier 1 confirmed).

**Recovery codes:**
- 10 single-use backup codes generated at enrolment.
- Stored hashed (Argon2id) in the DB, just like passwords — never recoverable from the server.
- Each code marks itself "used" at first valid use.
- A user can regenerate the full set from their account settings (after re-authenticating); old codes are invalidated.
- Admin can also force regeneration when resetting a user's MFA (see below).

**Admin reset of MFA (clarifier 2 confirmed):**
- A super-user can reset another user's MFA enrolment (clears the TOTP secret + invalidates all unused recovery codes).
- The reset is **loudly audit-logged**: who reset whom, when, from where (IP / session id).
- The reset target is forced to re-enrol on next login.

**No-IdP / lost-device / lost-codes scenario (clarifier 3 — option iii):**
- If a user loses **both** their TOTP device and their unused recovery codes, **another admin** must reset them. Standard.
- If the **last remaining admin** loses both their TOTP device and their codes:
  - There is no Funis-side "remote reset token" mechanism. We do not weaken the security model with a vendor-controlled bypass.
  - The customer's recovery path is: **restore from a recent backup** (per C14) taken before the loss, or **engage Funis under the support contract** for a documented manual recovery procedure (which would involve direct DB intervention with appropriate authorisation and audit-log preservation).
- **Strongly recommended operational practice (documented in install guide, not enforced):** every install has **at least two admin accounts** so neither is a single point of failure. We do not enforce this in code — the customer chooses.

**Login flow with MFA:**
1. User enters username + password (local) or completes SSO (sso mode break-glass admin: also local password).
2. If the account requires MFA, prompt for TOTP code or a recovery code.
3. On success: issue session, allocate licence slot (per C16), log the event.
4. Failure handling: rate-limited (per D19's policy work), audit-logged, generic error message ("invalid credentials") that does not reveal which step failed.

**Configurable per install (in `matofx.toml`):**
```
[auth.mfa]
totp_issuer = "MATOFX SPDM"   # shown in the user's authenticator app
totp_window = 1               # tolerated +/- intervals (default 1 = ±30s)
recovery_codes_count = 10
```

**Out of scope for v1:**
- WebAuthn / passkeys / hardware tokens for end-user MFA (Funis's signing YubiKey is a different concern, per C16).
- SMS, email, or push-based factors.
- Risk-based / step-up authentication.
- Per-action re-authentication ("sudo mode") — not in v1; admin actions are protected by session + audit log.

Implications to carry forward:
- The auth module needs a clean **state machine** for credential collection: password → MFA → session. Each step is independently rate-limited and logged.
- TOTP secret storage: encrypted at rest using a per-install key (loaded via E25's secret mechanism). The secret never leaves the server unencrypted.
- Audit log entries for MFA events are part of the same tamper-evident chain as licence and admin events (E26, B11).
- The first-admin bootstrap (`matofx admin create-first-user` from C12) prints the TOTP enrolment URL/QR after the admin sets their first password — so the bootstrap admin can never operate without MFA.

### D19. Password policy & compliance bar
**Decision: configurable modern password policy (NIST 800-63B by default) with bundled breached-password check; rate-limit per account and per IP; OWASP ASVS Level 2 as the security baseline, SOC 2 / ISO 27001-compatible design, GDPR-friendly defaults; admin-mediated password reset only; admin password rotation off by default but customer-configurable; non-admins capped at one concurrent session; per-user-type idle timeout.**

**Password complexity (Q1, option c):**
- Default policy follows **NIST 800-63B / OWASP ASVS L2**:
  - Minimum length **12** characters.
  - **No** forced character-class rules (no "must contain digit/symbol/case").
  - **No** forced password rotation.
  - **Mandatory check against a list of known-breached passwords.**
- **Bundled HIBP top-N list** (the Have I Been Pwned password hash list, plain SHA-1 prefixes — public domain). Ships inside the install; no phone home; air-gap-safe. Updated lists arrive in release tarballs.
- "Classic mode" knobs available for customers whose auditors demand checkbox-style policies:
  ```
  [auth.password]
  min_length = 12
  require_uppercase = false
  require_lowercase = false
  require_digit = false
  require_symbol = false
  rotation_days = 0          # 0 = no forced rotation
  history_count = 0          # 0 = no history check
  breached_check = true      # always recommended on
  ```
- Password hashing uses **Argon2id (PHC strings) with upgrade-on-login** (locked in B11).

**Rate-limiting and account lockout (Q2, confirmed):**
- **Per-account:** ≤5 failed login attempts in 5 minutes → 15-minute soft lock. Counter resets on a successful login or after the lock window. Defaults; tunable in config.
- **Per-IP:** ≤20 login attempts per minute per IP across all accounts → throttle (slow responses) + audit log entries; ≤100/min → temp ban for 5 minutes. Defaults; tunable.
- **Real client IP from trusted reverse proxies only**: the `X-Forwarded-For` chain is parsed against a configurable `auth.trusted_proxies` list. Untrusted proxies are ignored — the connecting IP is used.
- **Generic error messages** ("invalid credentials") regardless of which step failed (username unknown, password wrong, MFA wrong, account locked) — no enumeration leaks. The audit log records the actual reason.
- **Admin unlock:** super-users can unlock a locked account from the admin UI; loudly audit-logged.
- **Lockout never applies to break-glass local admins for the rate-limit only "lockout" semantics** — that would be a self-DoS during an outage. Instead they get **exponential delay** on failed attempts (1s, 2s, 4s, …) plus a much louder audit-log alert. They're never *blocked*, just slowed.

**Compliance bar (Q3, recommendation accepted):**
- **OWASP ASVS Level 2** as the security baseline; design choices stay compatible with future **SOC 2 Type II** or **ISO 27001** certification if Funis or a customer ever pursues it.
- **GDPR-friendly defaults**: every personal data field documented in a "data inventory"; deletion-on-request honoured by an admin-mediated process (delete user / scrub PII while preserving audit-log integrity by hashing replaced fields rather than removing audit rows).
- **No formal certifications claimed in v1.** Customer security questionnaires answered against the documented baseline.
- Specific frameworks **out of scope for v1** unless a real customer requirement appears: NIST 800-53/171, ITAR/EAR, FDA 21 CFR Part 11, GxP. Architecturally we don't preclude them — same audit log, same encryption-at-rest hooks, same access controls — but we don't promise any of them yet.

**Password reset (Q4):**
- **Admin-mediated only.** A super-user clicks "reset password" on a user; the system either generates a strong temporary password or accepts an admin-supplied one; the user is forced to change it on next login.
- **No self-service email-link reset** in v1 — it's a phishing surface and adds an SMTP coupling to the auth path.
- Audit-logged: who reset whom, when, from which session.
- A user who has lost both password and TOTP must be reset by an admin (per D18 — same flow).

**Admin password rotation (Q4):**
- **No forced rotation by default** (`rotation_days = 0`).
- **Configurable per install** when the customer's policy demands it (`rotation_days = 90` etc.).
- Applies uniformly to all local-auth users via the same `[auth.password]` block; we do not maintain a per-role rotation schedule in v1.

**Concurrent sessions per user (Q4):**
- **Normal users (non-admin): hard cap at 1 active session.** Logging in from a second device terminates the first session (with a clear "you have been logged out from your previous session" notice). Reduces shared-credential abuse and keeps the CSL slot accounting honest.
- **Super-user / admin accounts: multiple concurrent sessions allowed.** Common pattern: laptop + workstation + remote console.
- **Each session counts as one CSL slot regardless of role** (so an admin with three sessions occupies three slots in CSL mode). This keeps the licence-counting unambiguous; admins are aware of the implication.
- An admin can list all active sessions for a given user and revoke any of them; audit-logged.

**Idle timeout granularity (Q4):**
- **Per-user-type configurable.** Different defaults and limits per role:
  ```
  [auth.session.idle_minutes]
  super_user = 15
  admin = 30
  user = 60
  ```
  Hard session lifetime is a separate per-role setting:
  ```
  [auth.session.hard_hours]
  super_user = 4
  admin = 8
  user = 12
  ```
- Idle countdown resets on meaningful activity (an HTTP request to an authenticated route or a NiceGUI WebSocket message attributable to user action — not server-driven heartbeats).
- On idle expiry: WebSocket session is closed, cookie invalidated server-side, user redirected to login on next interaction.

**What's deliberately not in v1 (recorded as future work):**
- Self-service password reset via email link.
- Risk-based / step-up authentication (e.g. require re-auth before sensitive admin actions).
- Per-action "sudo mode" timers for admin operations.
- Geographic / device-fingerprint anomaly detection.
- IP allow-listing for specific users or roles.

Implications to carry forward:
- The breached-password list is a build artefact — versioned, sized in MB, loaded into memory at startup. Decide its update cadence in release planning.
- Rate-limiting state needs to survive web-process restarts; it lives in Valkey (already in the stack per B9), keyed by `account:<id>` and `ip:<addr>`.
- Concurrent-session cap requires a server-side session registry (not just a cookie) — already implied by D17's session model.
- Audit-log schema (E26) must be detailed enough to satisfy SOC 2 / ISO 27001 evidence collection if/when pursued: actor, action, target, timestamp, IP, session id, outcome.

### D20. Permissions model
**Decision: full RBAC + flat teams + project-scoped ACLs (granting to users only); five built-in roles; nothing is ever deleted except draft jobs older than X days; project visibility / sharing defaults are globally configured by the admin.**

**Q1. Shape — full RBAC + groups + project ACLs (option c).**
- Users have a **global role** (drives the legacy-style feature × access-right matrix — see "feature permissions" below).
- Users belong to one or more **teams** (flat — no nesting).
- **Object-level access** (Owner / Editor / Viewer) on projects, with inheritance to scenarios/jobs.
- **Approver** is an orthogonal grant on a per-tool or per-workflow basis (clarifier 1).

**Q2. Permission dimensions:**

- **Feature permissions (global, role-scoped).** Direct successor to the legacy `permissions` table — but with proper schema (no JSON-encoded lists, real FK relationships). Used by the navigation gating, admin UI access, and API authorisation. Order ~20 verbs (matching legacy granularity); all editable per role.
- **Object permissions on projects (Owner / Editor / Viewer).**
  - **Owner**: full control over the project except deletion (see below) — can edit data, manage scenarios/jobs, and manage the project's ACL.
  - **Editor**: can create / modify scenarios, submit jobs, edit data within the project; cannot reshare or change the ACL.
  - **Viewer**: read-only.
  - Inheritance: **scenarios and jobs inherit the project's ACL by default**; a scenario or job can be **further restricted** but never loosened beyond the project's grant.
- **Approver permission** (per-tool, per-workflow): orthogonal to Owner/Editor/Viewer. A user can be Approver on a specific tool/workflow without being an Editor on the project.

**Q2 — deletion is a non-default privileged action.**
- **Projects, scenarios, and submitted jobs cannot be deleted by non-admin / non-super-user accounts at all.**
- Even for super-users / admins, **deletion is not a default UI action** — it is gated behind a separate, explicit, high-friction admin pathway (multiple confirmations, audit-logged loudly, possibly requires re-authentication).
- The "delete" verb in the feature matrix is granted only to super-user / admin roles, **off by default even for them**, and an admin must opt in via a global setting before the option appears.

**Q3. Roles shipped by default:**

| Role | Description |
|---|---|
| **Super-user** | All global feature permissions, including managing other admins. Created via the bootstrap CLI (per C12). Always exists. |
| **Admin** | Broad system permissions (user management, tool import, system settings), but cannot create or modify super-users. |
| **Regular user** | Combined "power user + regular user" — can create projects, run jobs, build custom workflows in projects they're Owners/Editors of. Cannot administer the system. *(Default role for new local-auth or SSO users until further configured.)* |
| **Read-only / Auditor** | Can view everything they're shared on; cannot modify anything. For compliance / audit personas. |
| **Approver** | **Not a stand-alone role**; it is an additional grant layered on any of the above roles, scoped per-tool / per-workflow. |

A more restrictive "Basic user" role (e.g. cannot create projects, can only run jobs in projects they're shared on) is **easy to add later** by editing the feature matrix; not shipped in v1.

**Q4. Teams (flat) and ACL targets (user-only):**
- **Every user belongs to ≥ 1 team.** Multiple teams allowed. Team membership is required, not optional.
- **Teams are flat** — no nested team hierarchies in v1.
- **Project ACLs reference users only — never teams.** Teams are an organisational concept and a UX convenience, not an ACL principal. Implications:
  - When sharing a project, the admin/owner picks individual users (with team-based filtering / bulk-pick UX to make this efficient).
  - Adding a user to a team **does not** retroactively grant them access to projects already shared with their team-mates. Departing team members do **not** automatically lose access.
  - Permissions resolution does not need to walk team membership at request time — single user-id lookup against the project ACL. Faster, simpler, fully auditable.
- Teams power filtering and reporting in the UI ("show me projects where someone in my team is an Owner") — read-side convenience only.

**Q5. Defaults and inheritance:**
- **Default project visibility / sharing is configured globally by the admin** (not "private to creator" hard-coded). Examples of admin-configurable defaults:
  - "All new projects start private to creator."
  - "All new projects start visible to all users in the creator's team(s) as Viewers."
  - "All new projects start visible to a named team(s) as Viewers."
  - Editing rights default is similarly configurable (e.g. creator's team gets Editor by default).
- Defaults are seeded into the project's ACL **once at creation** — they expand to concrete user entries (since ACLs are user-only). Subsequent team-membership changes do not retroactively update ACLs.
- **Inheritance:** scenarios/jobs inherit the project's ACL; can be tightened, never loosened.

**Cross-team sharing (clarifier 3, confirmed):** an Owner can share their project with any individual user, regardless of team membership. Audit-logged.

**Ownership transfer (clarifier 4, confirmed but low priority):** an admin can reassign project ownership. Standard pattern: admin selects project → "transfer ownership" → picks new owner → audit-logged. Not a frequent UI flow; lives in the admin space, not the project owner's UX.

---

**Project-wide rule: data is preserved forever (per clarifier 2).**

> **Nothing in MATOFX is deletable**, except *draft jobs* that were created but never submitted and are older than `X` days (default 30, configurable).

What this means concretely:
- **User accounts** are never deleted. Disabled / deactivated accounts persist in the DB along with all their authored projects, scenarios, jobs, audit-log entries, etc. "Disable" hides them from active-user lists and revokes login capability; "delete" is not exposed.
- **Projects, scenarios, submitted jobs** are never deleted. They have a `state` field (`active` / `archived` / `cancelled` / etc.); state changes hide them from default views but never remove them from the DB.
- **The single exception:** a job that was *started* (form opened / partial inputs filled) but **never submitted** can be purged after `X` days. Default `X = 30`. Configurable in `matofx.toml`. Implemented as a scheduled cron job (per B9). Older drafts are hard-deleted along with their working-dir and any uploaded files; the audit log records the deletion event.
- Even when an admin pathway exists for super-users to "delete" something (per Q2 above), the implementation is a **soft-delete + scrub** — the row is marked deleted but kept; a separate reaper (gated behind a global setting that's off by default) is the only thing that ever does a hard-delete. v1 does not enable any reaper for projects/scenarios/jobs.

**Reconciliation with GDPR "right to erasure" (recorded in D19):**
- GDPR-mandated erasure is honoured via an admin-mediated **PII-scrub pathway**: replace name, email, and any other identifying fields on the user record with a deterministic hash of the original (so audit-log foreign keys still resolve to a stable opaque id), and clear free-text fields where the user might have entered personal data (notes, comments).
- The user record itself, the audit-log entries, the projects/scenarios/jobs the user authored — all stay. The audit log's tamper-evident chain is preserved.
- This is the only "removal" path users have under v1; it's executed by an admin under a documented procedure, not exposed self-service.

**Storage growth:** projects/scenarios/jobs accumulate forever. SPDM customers expect this (engineering history is the product). Backup strategy (C14, restic deltas) keeps backup size manageable. Operational guidance documents disk-sizing assumptions for typical install sizes.

---

**Configurable in `matofx.toml`:**
```
[permissions]
default_project_acl = "creator_only"     # | "creator_team_viewer" | "creator_team_editor" | "named_team_viewer:engineering" ...
allow_admin_delete = false               # gates the admin-only delete UI
draft_job_retention_days = 30            # only drafts (never-submitted jobs) are ever hard-deleted
```

Implications to carry forward:
- Schema models tables for `users`, `teams`, `team_members`, `roles`, `feature_permissions`, `project_acls`, `tool_approvers`, `workflow_approvers`. All have proper FKs and explicit unique constraints; no JSON-encoded permission lists in columns (cleaning up the legacy pattern).
- Every user-facing object has a `state` column and a `state_changed_at`/`state_changed_by` audit pair; "delete" is never the default action in the UI for that object — "archive" / "cancel" / "disable" is.
- Permissions evaluation is a single function (`can(user, verb, object) -> bool`) with one place to add caching and one place to log denials.
- The cron-driven draft-job reaper is one of the first scheduled jobs we wire up; it must respect tenancy and licence boundaries (no surprise mass-deletes).
- Admin UI for permissions makes the "user-only ACL + team-based bulk pick" UX comfortable — without that affordance, sharing 30-person projects is painful.

**Amendment 2026-05-17 (ADR-0002):** the project object-permission model is refined to a **per-user four-role ACL** — **Creator** (auto-assigned at creation, `creator_user_id` frozen forever, resolves to Owner-equivalent authority), **Owner**, **Member** (≈ the Editor role above), **Viewer** (full read-only incl. job results). This *returns to* Q4's user-only principle: Phase 1's interim team-keyed ACL is superseded; teams revert to a bulk-pick UX convenience, never an ACL principal. Clarifier 4 is refined: `creator_user_id` is immutable, so admin "transfer ownership" grants the successor Owner and revokes all other non-Creator Owner grants (it never alters the Creator). Archive (the `state` model above) is Creator/Owner-only; per-scenario ACL tightening is deferred to v1.x. Projects also gain **admin-configurable typed metadata** (single/multi-select + date + number fields, controlled vocabularies, no-delete value lifecycle) driving the home-table columns/filters, export and audit. The brief stays the source of truth for decisions; `decisions/0002-project-access-and-governance.md` records the rationale and `designs/project-access-and-governance.md` the build.

**Amendment 2026-05-21 (ADR-0005):** Q3's single global role is refined to a **base-role + stackable capability-role** model. Every user keeps exactly **one base role** (`super_user` / `administrator` / `beta_tester` / `regular_user`) on `User.role_id` — privilege rank and all hierarchy/ACL resolution (incl. ADR-0002) derive solely from it and are unchanged. On top, a user may hold zero or more **capability roles** (a new `role.kind='capability'`, assigned via a `user_role` join table): additive, permission-only, no rank. Effective permissions = base ∪ capabilities. The first two are `library_curator` (ADR-0006) and `materials_curator` (ADR-0007); more steward types are expected as customer requirements refine. There is no bundled "databases curator" role — granularity comes from composing roles (e.g. `regular_user + materials_curator`). The tool-approver list (D21) is unchanged. `decisions/0005-multi-role-capabilities.md` records the rationale.

### D21. Approval workflows
**Decision: per-tool / per-workflow approval policy (pre / post / both / none); per-tool global approver list; single-approver gate in v1; never self-approve; data-hash-signed approvals; rejection freezes the job and the submitter clones; workflow-level approval gate when any constituent tool needs approval; delegation supported.**

**Q1. Approval gate timing — option (c), per-tool configurable.**
- Each tool's `*_descriptors.json` declares: `approvals: "none" | "pre" | "post" | "both"`.
- **Pre-approval:** job goes from `Created` → `Pending Approval` → `Queued` (on approve) | `Rejected` (on reject). Tool runs only after approval.
- **Post-approval:** job runs to `Complete`, then state moves to `Pending Post-Approval` → `Approved` | `Rejected`. Outputs are visible to the submitter throughout but are not "Approved" until sign-off.
- **Both:** pre-gate must clear before run; post-gate must clear after.
- **None:** no gate.

**Q2. Approver list — per-tool / per-workflow only (option a).**
- Admin maintains a global **approver list per tool** and **per workflow** (workflow has its own list, separate from any of its constituent tools).
- v1 does not support per-project override of approver lists; the schema leaves room for it (a future per-project ACL on approvals can be added without migration pain).
- Approver assignments are managed in the admin UI; changes are audit-logged.

**Q3. Number of approvers required — single in v1 (option c).**
- Exactly **one** approval is required to clear a gate. Any one of the eligible approvers may approve.
- Schema field `approvals_required: int = 1` exists from day one so future N-of-M policies are a config change, not a migration.

**Q4. Self-approval forbidden (option a) — and how it's enforced/tested.**

> **A user can never approve a job they submitted, regardless of whether they're on the approver list.**

Defense in depth (three layers; tested at each):
1. **Permissions check** — the canonical authority is `can(user, "approve", job_or_workflow)`. It returns `False` when `user.id == job.submitted_by_id`. Lives in one function with one place to change behaviour.
2. **Database constraint** — the `approvals` table has a `BEFORE INSERT` trigger (or, where engine-supportable, a `CHECK` against a denormalised `submitted_by_id` column) that aborts any insert where `approver_id == submitted_by_id`. Defense even if the application logic is bypassed.
3. **API/route guard** — the HTTP endpoint serving the approval action re-checks before doing anything; never trusts the UI.

How we test it (concrete):
- **Unit test** on `can(user, "approve", job)` — author and approver are the same → False; different → True. Exhaustive over delegation cases (see below).
- **Integration test**: simulate an HTTP POST to the approve endpoint as the submitter on their own job → expect 403 with the audit-log entry recorded.
- **Repository test**: attempt to insert an `approvals` row directly via SQL where `approver_id == submitted_by_id` → expect the trigger/constraint to abort with a clear error.
- **Property test** (optional, nice-to-have): generate random (submitter, approver) pairs and confirm the system rejects every approve attempt where they're equal.
- The audit log must record every *attempted* self-approval (success or denial) — denials are first-class events so we can detect probing.

**Q5. Approval record content (data-hash signature included).**

Each approval record stores:
- `approver_id`, `submitted_by_id`, `target_id` (job or workflow id), `target_kind` (`job` | `workflow`), `gate` (`pre` | `post`), `decision` (`approved` | `rejected`), `comment` (free text), `decided_at`.
- **`data_hash`** — SHA-256 of the canonical serialisation of the data the approver reviewed:
  - For pre-approval: hash of inputs.json + tool/workflow descriptor version + relevant project metadata.
  - For post-approval: hash of outputs.json + the same upstream descriptors.
- **`signature`** — Ed25519 signature over `(approver_id, target_id, gate, decision, data_hash, decided_at)`. The key is per-install (loaded via E25's secret mechanism) so signatures verify offline; no per-user keys.
- **Verification:** if the underlying data ever changes, recomputing `data_hash` won't match the stored hash → tamper visible. The `matofx audit verify` CLI walks the chain and reports any mismatches.
- The approval record is appended to the tamper-evident audit log (per B11/E26) — both into the relational `approvals` table (for fast queries) and the hash-chained log (for integrity).

**Q6. Rejection and revision (recommendation accepted).**
- On rejection, the job moves to a **frozen `Rejected` state** — read-only, audit-preserved, never modified.
- To proceed, the submitter **clones** the rejected job into a new draft (`From rejected job #N`); inputs are pre-populated from the rejected job. The new job has a fresh job id and goes through approval afresh.
- The relationship is captured (`source_job_id` on the new job) so reviewers can see the lineage and any reviewer comments from the prior rejection.
- Workflows behave the same way: a rejected workflow is frozen; the submitter clones the entire workflow to retry.

**Q7. Notifications (confirmed).**
- Approvers receive in-UI notifications + email when a job/workflow needs their attention.
- Submitters receive in-UI + email when their job/workflow is approved or rejected (with the comment).
- Delegates (see below) receive notifications during the delegation window in addition to the original approver.
- Uses the same notification machinery as job-status updates (per legacy `tools.email_notifications`, modernised in §G37).

**Clarifier 1 — policy versioning (standard answer):**
- Each approval cycle records a snapshot of the policy *as it was at the time the cycle started* (`policy_snapshot_id` on the approval cycle).
- Changes to a tool's approver list / `approvals` field apply only to **new** approval cycles started after the change.
- In-flight cycles retain their original policy until they conclude.
- This is mandatory for clean audit; without it, "who could approve and why?" becomes unanswerable later.

**Clarifier 2 — workflow-level approval (confirmed: whole workflow approved at once).**
- A workflow that contains **any** approval-required tool becomes an **approval-required workflow**.
- The workflow gets a **single approval gate** (pre, post, or both — derived from the union of constituent tools' policies):
  - If any tool needs `pre`, the workflow has a `pre` gate (fires before any job in the workflow runs).
  - If any tool needs `post`, the workflow has a `post` gate (fires after all jobs in the workflow complete).
- The **workflow's own approver list** (from Q2) is the eligible approver pool — this is independent of any constituent tool's approver list. Avoids union/intersection ambiguity.
- The single approval covers all constituent jobs; per-tool approval gates inside the workflow are skipped.
- A standalone (non-workflow) job using the same tool still uses the tool's own gate and approver list.

**Clarifier 3 — delegation (confirmed for v1).**
- A user with approval rights can mark themselves **out-of-office** by setting a delegate (another user, who must already hold the right to approve at least the same scope) and a date range.
- During the delegation window, the delegate may approve on behalf of the original approver. The original approver retains their own ability to approve (delegation is additive, not exclusive).
- Approval records always store both **`approved_by`** (the actual signer) and **`approving_on_behalf_of`** (the original approver, when applicable). Audit log shows both.
- Self-approval rule (Q4) treats the **actual signer** as the approver — i.e. if the submitter is delegated to by an approver, they still cannot approve their own job. The constraint is on the *signer's* identity.
- Delegation cannot extend authority: the delegate must already be eligible to approve the tool/workflow. We do not let delegation grant new approver rights.
- The delegation feature surfaces as a small "I'm out — delegate to X from D1 to D2" panel in user settings.

---

**Locked CLI / config sketch:**

```
# in a tool descriptor
approvals = "pre"               # | "post" | "both" | "none"
approvals_required = 1          # placeholder for future N-of-M

# admin manages global approver lists per tool / per workflow in the admin UI
# (no config-file approver lists — that's a runtime, audit-logged setting)
```

State machine for an approval-gated job:
```
Draft  →  Created  → (pre gate: Pending Approval)
                          ├── Approved  →  Queued  →  Running  →  Complete
                          │                                       │
                          │                                       └── (post gate, if both: Pending Post-Approval) → Approved | Rejected
                          │
                          └── Rejected   (frozen, clone to retry)
```

Implications to carry forward:
- The `approvals` table and the audit-log signing chain share the per-install Ed25519 key — same secret, same rotation story, one place to manage.
- The "Pending Approval" state must respect the licence's read-only-on-expiry rule (per C16) — when the licence is in read-only mode, no new approvals can be made (existing data is viewable, no decisions accepted).
- The approver UI is high-priority for ergonomics: an approver wading through 20 pending jobs needs efficient bulk-review affordances. Worth designing carefully early.
- Notification routing (email vs in-UI) becomes a small per-user preference panel — admins set defaults, users can opt in/out within bounds.
- The clone-on-reject pattern means draft job count grows over time; covered by the draft-retention reaper from D20.

---

## E. Data, secrets, confidentiality

### E22. Data sensitivity
**Decision: treat every install as containing IP-sensitive engineering data + PII + potentially export-controlled or pharma/GxP-adjacent data; uniform sensitivity within an install (no per-project labels in v1); ACL is the project-isolation boundary; no formal compliance claims (PCI, FedRAMP, classified) and not in scope.**

**Categories that may be present in any install (per user's confirmation):**

- **(a) IP-sensitive engineering / formulation data** — recipes, material formulations, FEA models, simulation inputs/outputs representing trade secrets. Default assumption: every install holds this. Stolen → customer's competitive advantage gone.
- **(b) PII** — at minimum, names + emails of every user with access; potentially more (work locations, phone, free-text notes). GDPR considerations apply universally.
- **(c) Export-controlled technical data** — ITAR (US), EAR (US dual-use), EU/UK dual-use list. Aerospace, defence, semiconductor, advanced-materials customers will mandate compliance with their export-control regime. We do **not** formally certify against any specific framework in v1, but the design must be **strict enough to satisfy a customer's auditor** when one of them shows up:
  - Per-user audit-loggable access to every record (per E26).
  - No outbound data flows by default (per C15 — already locked).
  - At-rest encryption available (E23).
  - Strong AuthN with MFA for privileged users (per D17/D18).
  - Detailed access-control records that survive forever (per D20).
- **(d) Health / pharma / GxP-adjacent data** — drug-formulation simulations, medical-device modelling, food-safety formulation. We do **not** target FDA 21 CFR Part 11 / EU Annex 11 / GxP certification in v1, but the same "strict enough that an auditor isn't horrified" principle applies — tamper-evident audit log, signed approval records (per D21), preserved history (per D20: nothing deletable), reproducible job inputs/outputs.

**Out of scope for v1:**
- **(e) Financial / payment data, PCI-DSS** — MATOFX has no billing/payment surface; we never accept card data; no PCI scope.
- **(f) Classified / national-security data, FedRAMP, IL2–IL4** — not designed for, not certified against, not sold into.

**Q2 — Uniform sensitivity within an install (option a).**
- Every project / scenario / job in a given install is treated as **equally sensitive**: protected by the install's ACL and encrypted-at-rest configuration uniformly.
- No per-project sensitivity tiers ("Standard / Confidential / Highly Confidential") in v1.
- Schema leaves space for a future `project.classification` enum if a customer ever drives a real need; until then, the field doesn't exist and no UI or rule depends on it.
- Most SPDM customers can't and won't maintain a classification scheme; pretending otherwise creates audit burden without security gain.

**Q3 — ACL is the project-isolation boundary (option a).**
- A user can see exactly the projects/scenarios/jobs their ACL grants — no per-project key escrow, no per-project encryption.
- Enforcement is centralised in the `can(user, verb, object)` function (per D20). Every read/write path goes through it; no business logic queries the DB without this gate.
- Defense in depth: CI test suite includes "every public route returns 403 or filters appropriately when accessed by a user without the right" — automated, run on every PR.
- Per-project crypto remains an option if a future customer requires hardware-bound key separation; not built in v1.

**Reconciliation with locked decisions:**
- D19 (compliance baseline): OWASP ASVS L2 stays the explicit baseline. (c) and (d) above add operational guidance, not new certification commitments.
- D20 (no deletion + preserve audit chain) directly serves the (c) / (d) regulatory expectations.
- C15 (no outbound at runtime) is a hard requirement for (c) export-controlled environments — already locked.
- C14 (encrypted backups configurable) is now upgraded from "nice to have" to "expected for any (c)/(d) customer" — flagged in implications below.

**Practical security posture summary (one-liner for security reviews):**

> "MATOFX is a single-tenant on-prem application designed to hold trade-secret-grade engineering data and PII, with operational defaults strict enough to support customer obligations under GDPR, EU/UK/US export control, and pharma/GxP regimes. We do not formally certify against any specific compliance framework in v1; design choices preserve forward compatibility with future SOC 2 / ISO 27001 certification."

Implications to carry forward:
- E23 (at-rest encryption) must be **on by default** for any non-trivial install — pushes the recommendation past "optional, advanced".
- E26 (audit log scope) must include **every read** of project / scenario / job data, not just writes, to support (c) export-control auditing — significantly more events than a typical app.
- E27 (data retention) gains a "legal hold" concept: when a regulator or customer's legal team flags a project for hold, the standard archival workflow must not move data out of reach. Already implied by D20's "nothing deletable" rule.
- Documentation deliverable: a customer-facing "Security & Compliance Posture" document that we update per release. Drives security-questionnaire responses (per H40/H42) and gives prospects a single artefact to share with their auditors.
- Build a small **data-inventory** doc inside the project: every PII field, every place engineering data lives, every place audit data lives. Drives E27 retention rules and any future GDPR DSAR support. Don't try to maintain it as code comments; one source-controlled file.

### E23. At-rest encryption
**Decision: defend against (a) disk theft + (b) backup theft by default; (c) operational separation as opt-in. SQLCipher for SQLite (DB layer 1), FS-level encryption documented as customer responsibility (DB on Postgres + job storage), four pluggable key-management backends, every threshold customer-configurable.**

**Q1. Threat model:**
- **(a) Disk theft / lost laptop / stolen VM image — defended by default.**
- **(b) Backup tampering / theft — defended by default.** Backup encryption is configurable (per C14); now that data sensitivity is locked (E22), the default flips on for sensitive installs (see clarifier 4 below).
- **(c) Operational separation (host-admin who shouldn't read engineering data) — supported as opt-in.** Achieved by binding keys to a TPM/HSM (option d in Q3) or a startup passphrase (option c) so that filesystem-level access alone doesn't yield decrypted data.
- **(d) Compromised app super-user reading data via the app — out of scope of encryption.** Authorisation problem, not crypto.

**Q2. Layered protection:**

**Layer 1 — Database:**
- **SQLite installs: SQLCipher** (community edition, BSD-3-Clause — licence-clean) is the default. Drop-in via the SQLCipher build of the SQLite driver; transparent file-level encryption with negligible CPU overhead. The single SQLite DB file is unreadable without the key.
- **Postgres installs: filesystem-level encryption** is the recommended approach (LUKS on Linux for the data directory; BitLocker on Windows). Customer-side responsibility, documented in install guide. We do not bundle a Postgres TDE solution (open-source TDE is fragmented; commercial options conflict with the no-paid-licence rule per project memory).

**Layer 2 — Job storage volume:**
- **Filesystem-level encryption (option i)** is the supported approach. Customer is responsible for setting up LUKS / BitLocker / equivalent on the volume mounted at the job-storage path; install guide includes step-by-step instructions for both OSes.
- **App-level per-file encryption is not built in v1** — kept as a known future option behind a config flag if a customer brings a real need (e.g. multi-tenant-on-shared-volume scenarios, which are explicitly out of scope per A1).

**Layer 3 — Secrets and key material:**
- Handled in §E25. Never written to logs or audit records; loaded into runtime memory from configurable sources.

**Layer 4 — Backups:**
- Already configurable in §C14 (`backup.encryption.enabled`).
- Default flipped to **`true` for any install where the customer is in (c) or (d) data territory** (export-controlled or pharma/GxP-adjacent), per E22. Tiny non-regulated installs can opt out. The install wizard / config file makes this an explicit choice, not a silent default.

**Q3. Key-management backends — fully customer-configurable:**

A single config block (`encryption.key_source`) selects the backend. All four supported in v1:

```
[encryption]
key_source = "file"   # | "env" | "passphrase" | "tpm"

[encryption.file]
path = "/etc/matofx/keys/at_rest.key"

[encryption.env]
var = "MATOFX_AT_REST_KEY"

[encryption.passphrase]
prompt_on_start = true       # admin enters passphrase at boot
key_derivation = "argon2id"  # KDF parameters configurable
salt_path = "/etc/matofx/keys/at_rest.salt"

[encryption.tpm]
device = "/dev/tpm0"         # Linux; Windows uses CNG/PCP
pcr_policy = "pcr=7"         # bind unsealing to specific PCR values
```

| Backend | Use case | Threat coverage | Operational cost |
|---|---|---|---|
| `file` | Default for tiny / non-regulated installs | (a)+(b) only | Low — fully unattended |
| `env` | Containerised installs where the env var is loaded by the orchestrator from a secrets store | (a)+(b) | Low — depends on orchestrator's secret hygiene |
| `passphrase` | High-security installs willing to require human-attended restarts | (a)+(b)+(c) | High — admin must be present for every restart |
| `tpm` | High-security installs that want unattended boot with operational separation | (a)+(b)+(c) | Medium — one-time TPM enrolment, hardware-bound |

The codebase keeps **one** `KeyProvider` interface (with these four implementations); business logic only ever calls `provider.get_at_rest_key()`. Adding a fifth backend later (e.g. customer-supplied KMS bridge) is local to one module.

**Q4. Key rotation — recommended approach:**

The realistic recommendation: **build automated tooling only for the keys we expect to rotate periodically; document manual procedures for the rare ones.**

| Key | Cadence | Tooling | Who runs it |
|---|---|---|---|
| **Per-install Ed25519 signing key** (audit log, approvals — per B11/D21) | Annual practice + on incident | `matofx rotate signing-key` CLI; old key retained in a "previously trusted" verification set so historical signatures stay valid; rotation event recorded in the audit chain itself | Customer admin |
| **At-rest DB / FS encryption key** (this section) | On incident only (≥annual recommended) | Documented manual procedure: backup → re-key → restore. Not automated in v1. The complexity of safely re-keying a live DB is out of scope. | Customer admin (with Funis support if needed) |
| **Funis private signing key** (licence + release — per C16) | On suspected compromise only | Documented runbook; new keypair on YubiKey, public key embedded in next release, customers transition naturally on next licence/release cycle. Old key retained for verifying historical artefacts. | Funis only |
| **Backup encryption passphrase** (per C14) | Annual recommended | Customer-side: take a fresh backup with new passphrase, retain old + new through a transition window, prune old encrypted backups per retention policy. Documented; not automated. | Customer IT |
| **TPM-bound key** (option d above) | Hardware-lifetime; rotate on hardware replacement | TPM enrolment procedure (`matofx tpm enrol`); key never leaves hardware | Customer admin |
| **TOTP secrets, user passwords, session cookies** | Already covered in D17/D18/D19 | Existing tooling | Various |

**Single rotation principle for the things we own in v1:**
- Build a clean `matofx rotate <key-type>` CLI for the per-install signing key (frequent operation).
- Build `matofx tpm enrol` / `matofx tpm unenrol` for the TPM-bound at-rest key (one-time per hardware lifecycle).
- Document manual procedures for everything else; keep the runbooks under version control.

**Q5. Honest scope (confirmed):**

The Security & Compliance Posture doc (per E22) explicitly lists what at-rest encryption does **not** defend against:
- An authenticated user reading data their ACL grants — that's the ACL's job.
- A memory dump of the running app — keys and decrypted data are in RAM.
- A root-level insider with the encryption key — no encryption scheme defends here without hardware attestation.
- A compromised TPM (extracted secrets, vendor backdoors) — beyond our threat model.

Implications to carry forward:
- **`KeyProvider` is one of the first modules built.** Everything else (signing chain, audit log, DB driver, backup engine) consumes it. Tested heavily.
- The install wizard (when it eventually arrives — v1.x) walks through key-source selection; for v1, the documented config-file path is the install procedure.
- CI must run the test suite **once per backend** (`file`, `env`, `passphrase`, `tpm` if a TPM is available in the runner — otherwise mocked) to catch backend-specific regressions.
- Performance budget: SQLCipher's overhead is single-digit % CPU; FS encryption is hardware-accelerated on modern CPUs (AES-NI). No measurable user-visible impact at our load.
- If a customer ever needs (b) per-project crypto separation, we add a fifth `KeyProvider` ("per-object") — but **not** in v1.

**Configurable thresholds (per the user's "all clarifiers should be configurable" instruction):**
```
[encryption]
db_encryption = true                    # SQLCipher for SQLite; FS-level expected for Postgres
filesystem_encryption_required = false  # if true, app refuses to start unless FS-level encryption is detected
backup_encryption_default = true        # default for new installs in (c)/(d) territory
key_source = "file"                     # | "env" | "passphrase" | "tpm"
```

The `filesystem_encryption_required` flag is a **defence-in-depth check**: when set, the app reads `/proc/mounts` (Linux) or queries BitLocker (Windows) on startup to confirm the job-storage volume is on encrypted media. Default off (we don't want to surprise small installs); customers who need it (export-controlled, pharma) flip it on.

### E24. In-transit / TLS
**Decision: TLS-everywhere by default. Reverse-proxy termination is the documented default but direct TLS termination is also supported. TLS 1.2 minimum / 1.3 preferred. Customer-provided certs primary, self-signed (1-year) on first install. No built-in ACME. Internal traffic to DB and Valkey uses TLS for non-localhost. Standard hardening headers and CSRF protection on by default. Outbound TLS strictly verified. No HSTS preload.**

**Q1. TLS termination — both supported, reverse proxy is the documented default.**
- **Default pattern: customer-managed reverse proxy** (nginx / Caddy / Traefik / Apache / corporate load balancer / F5 / similar) terminates TLS and forwards to MATOFX over plain HTTP on a private/loopback network. Most enterprise customers already have one; reusing it is the right call.
- **Direct TLS termination at MATOFX** is supported for tiny installs without a proxy: the app listens on HTTPS itself, with cert and key paths in config.
- A single config flag (`network.tls_termination = "proxy" | "app"`) selects the mode; both code paths share the same cert handling.

**Q2. Protocol versions and ciphers — TLS 1.2+ with Mozilla "intermediate" defaults; configurable.**
- **Minimum: TLS 1.2.** TLS 1.3 preferred and negotiated when both sides support it.
- **Forbidden: SSLv2, SSLv3, TLS 1.0, TLS 1.1.** Hard-coded; the app refuses to negotiate them.
- **Cipher suites:** Mozilla "intermediate" baseline by default — modern AEAD suites only (AES-GCM, ChaCha20-Poly1305), forward secrecy required. Configurable for customers whose security policies mandate exact suites (configurable list, validated against the forbidden set on startup).
- Settings live in `[network.tls]` block:
  ```
  [network.tls]
  min_version = "1.2"
  ciphers = "MOZILLA_INTERMEDIATE"   # | explicit colon-separated list
  ```

**Q3. Certificate management — customer-provided + self-signed default; no ACME.**
- **`matofx tls install <fullchain.pem> <privkey.pem>`** — installs a customer-supplied cert; validates it (parses chain, checks key match, expiry > 30 days warning), reloads the listener (or signals the reverse proxy if external).
- **Self-signed cert generated at first install:** **1-year validity** (per clarifier 1), 4096-bit RSA *or* P-256 ECDSA (auto-selected based on host capability). Subject Alt Names include the configured `PUBLIC_BASE_URL` hostname. The install logs print: "TLS is currently using a self-signed certificate. Browsers will warn. Replace with `matofx tls install ...` before production use." Self-signed cert is regenerated only by explicit admin action.
- **`matofx tls status`** — prints current cert subject, issuer, expiry, fingerprint. Useful for support.
- **No built-in ACME / Let's Encrypt client.** Most installs are intranet-only and can't reach public ACME servers. Customers with an internal ACME CA are documented to run ACME at their reverse proxy, not in MATOFX.
- **`matofx tls renew-self-signed`** is provided for the corner case where a customer never replaces the self-signed cert and it's about to expire — issues a new self-signed cert with the same key (preserving any custom hostname/CN) for another year.

**Q4. Internal traffic between components.**
- **Postgres:** **TLS required for non-`localhost` connections by default** (per clarifier 3). Setting is `db.tls = "require" | "verify-ca" | "verify-full" | "disable"`; default is `verify-full` for remote, `disable` for `localhost`. Customer can override.
- **Valkey:** TLS supported via the standard Valkey TLS port. Default behaviour: `off` for `localhost`, `on` (with `verify-full`) for non-localhost. Setting is `queue.tls = "on" | "off"` plus optional CA path.
- **Web ↔ Workers:** they communicate **via Valkey**, not directly. So worker-to-web encryption is whatever Valkey's TLS setting is; we never need a peer-to-peer trust channel.
- **No mutual TLS between web/workers/queue/DB in v1** — adds substantial certificate-management overhead with little real-world benefit on a customer-controlled private network.

**Q5. HTTP security headers — on by default, configurable.**
- **HSTS:** `Strict-Transport-Security: max-age=31536000; includeSubDomains` by default. **No `preload` directive** (per clarifier 2 — MATOFX URLs are customer-owned, not Funis-owned).
- **CSP:** strict default. `default-src 'self'`; `script-src 'self' 'unsafe-inline'` only where NiceGUI/Quasar requires it (we audit and tighten as far as possible during implementation); no `eval`; no remote origins. Customer can extend the policy via config (e.g. allow embedding from their corporate portal).
- **X-Content-Type-Options:** `nosniff` — always.
- **X-Frame-Options:** `DENY` by default; configurable to `SAMEORIGIN` if a customer needs to iframe MATOFX inside their corporate portal.
- **Referrer-Policy:** `same-origin` — default.
- **Permissions-Policy:** all browser APIs denied by default (`camera=(), microphone=(), geolocation=(), usb=()`, etc.). The 3D viewer needs **none** of these — no special-case allowances.
- Headers can be customised in `[network.security_headers]` for niche integration scenarios.

**Q6. Cookies + CSRF — locked.**
- **Session cookies:** `Secure`, `HttpOnly`, `SameSite=Lax`. We use `Lax` (not `Strict`) so OIDC/SAML redirect flows continue to work cleanly; sensitive endpoints rely on CSRF tokens for protection on top of `SameSite`.
- **CSRF tokens:** synchroniser-token pattern on every state-changing endpoint. NiceGUI's WebSocket channel has its own session-bound protection; any pure HTTP form/POST endpoint we add is wrapped by a CSRF middleware.

**Q7. Outbound TLS — verified by default.**
- All outbound HTTPS clients (IdP, SMTP, HPC, remote tool servers) verify peer certificates by default.
- **Trusted CA bundle:** uses the system store by default; configurable via `network.outbound_ca_bundle = "/path/to/ca-bundle.pem"` for customers running internal CAs.
- **Strict hostname matching** — we never disable hostname verification, even for "internal" destinations.
- Outbound HTTP client wrapper (the same one that enforces the egress allow-list per C15) is the single place where TLS config lives — one place to audit, one place to change.

**Self-signed cert validity (clarifier 1): 1 year.** Short enough to nudge customers to install a real cert; long enough that small installs aren't surprised by mid-pilot expiry.

**HSTS preload (clarifier 2): no.** Domains are customer-owned; preload submission is a customer choice if they want it, not Funis's call.

**Internal Postgres / Valkey TLS default (clarifier 3): strict by default for non-localhost, opt-out only.** Aligns with E22's data-sensitivity baseline.

Implications to carry forward:
- The TLS configuration is one of the **first things `matofx doctor` checks** (per C12) — a misconfigured cert is a top-10 install support call. Doctor verifies cert validity, expiry, key match, hostname coverage, chain completeness.
- Cert reload must be **online** for direct-TLS mode (no full app restart for a routine cert renewal). Achievable with the standard ASGI server's reload-cert hook.
- The CSP we ship needs explicit testing against NiceGUI in CI — strict-CSP-vs-Quasar interaction is the most likely place for a regression to slip in.
- All install docs include a "Behind a reverse proxy" walkthrough for nginx/Caddy/Traefik (snippets shipped in the release tarball).
- Test plan (per H40) covers TLS 1.0/1.1 negotiation refusal, weak-cipher refusal, hostname-mismatch outbound rejection, and CSRF token validation.

### E25. Secret storage
**Decision: tiered SecretProvider with file-based (Tier 1) and env-var (Tier 2) backends shipped in v1; OS keychain (Tier 3) deferred; customer-supplied backend (Tier 4) interface kept as documented extension point. One central access path. SecretStr typing + CI grep test for log leakage. Partial reload model — secret hot-reload for SMTP/IdP/HPC/remote-tool; full restart required for DB/Valkey credential changes.**

**Q1 — Tiered backends (v1 ships Tiers 1 + 2; Tier 3 deferred per clarifier 1):**

- **Tier 1: file-based loading.** Each secret either has a value in `matofx.toml` (only for non-sensitive defaults) or a `_file` path pointing to a separate file. The secrets directory is `0700`, owned by the MATOFX service account.
  ```
  [smtp]
  username = "matofx@customer.com"
  password_file = "/etc/matofx/secrets/smtp.pwd"
  ```
- **Tier 2: environment variables.** Each secret also has an `_env` form, suitable for `docker secrets` injection or container orchestrator-provided env.
  ```
  [smtp]
  password_env = "MATOFX_SMTP_PASSWORD"
  ```
- **Tier 3 (OS keychain — Windows Credential Manager / libsecret) is NOT shipped in v1** (per clarifier 1). The `SecretProvider` interface accommodates it; we can add backends later without touching call sites. Headless server reality is that file/env covers >95% of customers.
- **Tier 4: customer-supplied backend** is a documented extension point. The `SecretProvider` ABC is published in the codebase with a documented method contract. A customer needing Vault/OpenBao/AWS-SM/Azure-KV implements the interface; we don't ship any.

Per-secret config follows a uniform pattern: every credential-shaped field has both `_file` and `_env` forms, plus an inline value as a last resort for tiny installs. The provider layer normalises all three at startup. **Inline literal secrets in `matofx.toml` are accepted but loudly warned about** in `matofx doctor` output ("plaintext secret in config file").

**Q2 — Rule of one: every secret accessed through a single SecretProvider.**
- All secrets reach app code via `SecretProvider.get(name) -> SecretStr` (and `get_str(name) -> str` for places where the underlying library demands a plain string — those callsites are explicit and audit-tagged).
- Business logic must **never** read `os.environ` or open a file in the secrets directory directly.
- **CI lint** enforces this: a custom check runs `ruff` rules + a small AST walker that fails the build if anything outside `matofx.config.secrets` references `os.environ.get`/`os.getenv` or opens a path under the configured secrets dir. Listed in H40/H41 as a required gate.

**Q3 — Secrets must never appear in logs, audit, or error pages.**
- Every secret in the config model is typed as `pydantic.SecretStr` (or our equivalent thin wrapper). `repr()` / `str()` / `__format__` all return `"***"`. Structured-log encoders treat `SecretStr` specially.
- Stack traces from authentication / DB / SMTP / HPC failures are filtered through a redaction layer that scans the message for any loaded `SecretStr` value and replaces matches with `***`.
- **Pre-merge CI test:** a "secret-leakage canary" test loads recognisable canary values for every credential field, runs the app's smoke-test suite (login, job submit, IdP redirect, HPC poll, SMTP send, etc., all against mocks), then greps every produced stdout/stderr/log file/audit-log entry for any of the canaries. Any match fails the build. Confirmed as a v1 commitment.
- Audit-log entries record **events**, not secrets. Login events store the username and outcome; never the password or any portion of it.

**Q4 — Rotation model: simple (per clarifier 3).**
- **No app-managed rotation** in v1. Rotation = config change + reload.
- **`matofx config reload`** does a partial live reload of secret-bearing configuration:
  - **Hot-reloadable:** SMTP, IdP (OIDC client secret / SAML signing material), HPC scheduler credentials, remote tool server credentials, outbound CA bundle, security-header overrides, draft-job retention setting.
  - **Restart-required:** DB connection string / password (the connection pool can't be hot-rewired safely), Valkey URL / password (broker reconnection must reset all worker bindings), at-rest encryption key (entire data layer needs to re-open).
  - `matofx config reload` returns a clear summary of what changed, what was applied, and what still needs a restart.
- **`matofx restart --graceful`** is the documented path for rotating restart-required secrets: drains in-flight HTTP requests, finishes or hard-stops in-flight jobs per the C13 rules, then restarts.
- **Key rotation** specifically (per-install signing key, etc.) — already covered in E23/B11 and uses dedicated CLIs (`matofx rotate ...`).

**Q5 — Bootstrap secret problem.**

| Backend (per E23 KeyProvider) | First boot |
|---|---|
| `file` | Install script generates a 32-byte random key → writes to `/etc/matofx/keys/at_rest.key` with `0600` perms, owned by service account. **Install guide makes "back up this key separately" a top-of-page hard requirement.** |
| `env` | Orchestrator provides the key on first start; same hard backup requirement applies but on the orchestrator side. |
| `passphrase` | Admin enters the passphrase at first start; salt is generated and persisted; passphrase is not. Subsequent boots prompt or accept via env. |
| `tpm` | `matofx tpm enrol` runs once at install; key is sealed to TPM; subsequent boots unattended. |

`matofx doctor` checks key-file permissions, owner, presence of off-host backup metadata (a small marker file the admin places after copying the key offline), and warns if anything looks wrong.

**Per-secret list (v1 secrets and where they live):**

| Secret | Default backend | Hot-reloadable? |
|---|---|---|
| At-rest encryption key | KeyProvider (per E23) | No — restart |
| Per-install Ed25519 signing key | File or KeyProvider-bound | No — restart |
| Funis public key (verify) | Embedded in app build | N/A — code, not secret |
| Licence file | File path | Yes — `matofx licence reload` |
| DB password | File / env | No — restart |
| Valkey password | File / env | No — restart |
| SMTP credentials | File / env | Yes |
| OIDC client secret | File / env | Yes |
| SAML SP signing key | File / env | Yes |
| HPC scheduler creds | File / env | Yes |
| Remote tool server creds | File / env | Yes |
| TLS private key (direct mode) | File | Yes — `matofx tls install` |
| Backup encryption passphrase | File / env / passphrase prompt | At backup write/restore time |
| TPM PIN (if used) | Stdin at boot | N/A |

Implications to carry forward:
- `SecretProvider` and `KeyProvider` (E23) are **two separate interfaces** but live in the same module (`matofx.config.secrets`). Both ship from day one of the build.
- Default install lays out a standard secrets directory layout: `/etc/matofx/secrets/*.pwd` and `/etc/matofx/keys/*.key`, with `0700` / `0600` perms enforced by the install script and re-checked by `matofx doctor`.
- Documentation for each integration (SMTP, IdP, HPC, remote tools) points to the per-secret config block and shows both `_file` and `_env` forms.
- Test-suite coverage targets:
  - `SecretProvider` correctly resolves file/env/inline forms with the documented precedence.
  - Secret values never appear in logs (canary test).
  - `matofx config reload` correctly identifies hot-reloadable vs restart-required changes and applies / surfaces them appropriately.
- The "customer-supplied SecretProvider" path is documented but not tested by us; if a customer ships their own implementation, integration is their responsibility under support.

### E26. Audit log scope
**Decision: tamper-evident hash-chained Ed25519-signed log records authentication, authorisation, every state change, every approval/delegation event, every licence/admin/system event, and per-session-per-record reads (default, configurable). Standard 18-field schema. Visible to super-users, auditors, and Funis-support-on-customer-grant only — not regular users. Never purged. Synchronous for writes, batched-async for reads. No SIEM export in v1.**

**Q1 — Event categories logged:**

**Bucket A — Authentication & session (always-on):**
- Login attempt (success / failure / locked / MFA failure / break-glass admin login).
- Logout (explicit / idle-timeout / forced by another session).
- MFA enrolment, recovery-code use, admin reset of MFA.
- Password change, password reset by admin.
- Session creation / revocation.
- IdP redirect outcomes (SSO success, claim mismatch, pre-provisioning required, etc.).

**Bucket B — Authorisation & data access (always-on; reads configurable):**
- **Every state-changing action:** project / scenario / job create, edit, archive, ACL change, ownership transfer, tool / workflow definition change, user / team / role / permissions changes, licence install / reload, config reload, key rotation, TPM enrolment.
- **Approval events:** approval request, approve, reject, delegation set / cleared, denied self-approval attempts.
- **Access denials:** any time `can(user, verb, object)` returns False *and* the user attempted the action — captures probing.
- **Read access** to project / scenario / job content. Default mode: **(iii) per-session-per-record** — first read of a record in a session is logged; subsequent reads in the same session are not (per clarifier 1). Configurable globally:
  ```
  [audit.read_logging]
  mode = "session"   # | "always" | "off"
  ```
- **Job execution events:** submit, queue, start, complete, fail, cancel, retry, HPC submit, HPC poll outcome.

**Bucket C — System operations (always-on):**
- Service start / stop / upgrade.
- Backup run / verify / prune outcomes.
- Cron job runs (start, finish, failure).
- Queue worker start / stop / crash / restart.
- HPC connection failures, circuit-breaker trips.
- Licence validation outcomes (valid / grace / read-only / failed).
- Audit-log rotation / signing-key rotation.

**Bucket D — Tool plugin events (mostly always-on, some configurable):**
- Tool registration / update / removal — always on.
- Custom workflow creation / update — always on.
- Tool input validation failures — always on.
- Tool subprocess crash / exit code — always on.
- **Per-job tool-side events** (heartbeats, progress messages from inside a tool) — configurable:
  ```
  [audit.tool_events]
  per_job_progress = false   # turn on per-tool if needed
  ```

**Q2 — Standard 18-field schema for every entry:**

| Field | Notes |
|---|---|
| `event_id` | UUIDv4 |
| `event_seq` | monotonic, gap-free per install — supports chain integrity |
| `prev_hash` | hash of previous entry's canonical serialisation |
| `event_hash` | hash of this entry's canonical serialisation |
| `signature` | Ed25519 signature over `event_hash + prev_hash + event_seq` |
| `timestamp` | UTC, ISO 8601 with sub-second |
| `actor_id` | user / service / system id (nullable for `system`) |
| `actor_kind` | `user` \| `service` \| `system` \| `funis-support` |
| `actor_session_id` | nullable |
| `actor_ip` | real client IP via trusted-proxies chain |
| `actor_user_agent` | truncated to 512 chars |
| `action` | dotted verb, e.g. `project.create`, `auth.login.success`, `job.read` |
| `target_kind` | `project` \| `scenario` \| `job` \| `user` \| `tool` \| `workflow` \| `system` etc. |
| `target_id` | id of the target object (nullable for system events) |
| `target_label` | **denormalised, frozen at write time** — old entries stay readable even if the live record is renamed / disabled (legacy app's audit bug) |
| `outcome` | `success` \| `failure` \| `denied` |
| `metadata` | small JSON blob; hard cap (e.g. 4 KiB) of event-specific extras |
| `licence_id` | current licence under which the event occurred — ties audit to licence chain |
| `app_version` | semver of the app at write time |
| `schema_version` | of the audit-log entry format itself |

**Q3 — Audit log readers:**
- **Super-user role:** full read access through admin UI + CLI.
- **Auditor / read-only role** (per D20): full read access; no write rights elsewhere in the system. "If you have audit access, you see everything" — auditors exist precisely because they need cross-project visibility.
- **Funis support:** **only when the customer explicitly grants time-boxed access.** Mechanism: an admin issues a `support_access` grant (with an expiry timestamp and an audit-logged reason) that allows a designated Funis-support account / role to read the audit log for the duration. Revocable any time. The grant itself is, of course, audit-logged. **Never automatic, never persistent.**
- **Regular users:** **no audit-log access** — including no view of their own actions (per clarifier 2). Simplifies the access model and matches the user expectation that audit is an operations / compliance tool, not a self-service feature.

**Q4 — Retention and storage:**
- **Never purged.** Same "nothing deletable" rule as in D20. The audit log accumulates forever.
- **Storage:** **file-based primary store** — append-only files rolled monthly (e.g. `audit-2026-05.log.jsonl`). Each file's last entry's hash is the `prev_hash` of the next file's first entry — chain spans files cleanly.
- **DB index:** a denormalised index in the main DB for fast queries (filter by actor / target / time range / action). The index is rebuildable from the file store.
- **Backup:** audit-log files are part of the standard backup (C14). Restic dedupes the append-only files efficiently.
- **`matofx audit verify [--from DATE] [--to DATE]`** — walks the chain, validates every signature and `prev_hash`, reports inconsistencies. Runs as part of `matofx doctor`. Designed to be run periodically by the customer (e.g. nightly cron).
- **`matofx audit export [--from DATE] [--to DATE] [--format csv|jsonl]`** — exports a date range for hand-off to auditors. Output preserves the chain fields so the recipient can re-verify.

**Q5 — Write performance and reliability:**
- **Synchronous append** for state-changing actions, authentication, approvals, admin actions, and access denials. The action and its audit entry are committed in the same transaction; if the audit write fails, the action fails. Preserves "no action without an audit record" guarantee.
- **Batched async append** for read events (mode `session`/`always`). Reads are buffered in memory and flushed every ~250 ms or every 1000 entries (whichever comes first). The buffer is bounded; on overflow we drop with a `audit.read_buffer_overflow` event of its own (so the loss is itself audit-logged).
- **Audit-log writer is in-process** (a small async background task in the web/worker process), not a separate service. Avoids the "audit service is down so we can't service requests" failure mode.
- On a clean shutdown, any pending buffered reads are flushed before the process exits. On a crash, buffered reads are lost — acceptable trade-off for the latency benefit, and the loss is detectable from gaps in the read pattern.

**Clarifier 3 (SIEM export): not in v1.**
- No JSON-Lines tail / Splunk forwarder / Sentinel connector pattern shipped or supported.
- The file-based JSONL audit store is *itself* tail-friendly, so a customer can point their own SIEM forwarder at the directory if they wish (and many SIEMs ingest JSONL natively). We don't promise compatibility, format stability across versions, or support — but we don't actively prevent it. Documented as "best-effort" if asked.
- Native SIEM connectors / formats / push integrations are deferred to a future release if real demand materialises.

Implications to carry forward:
- Audit-log signing key is the per-install Ed25519 key (per B11). Rotating it (per E23's tooling) writes a `audit.key.rotate` event and starts the next file with the new key; verification keeps the old key in the "trusted past" set so historical signatures still verify.
- The denormalised `target_label` and `licence_id` denormalisations on each entry are mandatory — old entries must remain self-describing forever.
- Performance budget: synchronous-write events should add < 5 ms to the action's transaction; batched read events should add < 0.5 ms p99 to the request. Tracked in benchmarks (per H42).
- The "Funis support time-boxed grant" mechanism is built into the auth/permissions layer (one capability among others); it's not a separate subsystem.
- Test plan covers: chain integrity verification, signature rotation continuity, target-label denormalisation, denial-attempt logging, batched-read overflow behaviour, support-grant expiry, audit-log access-control.

### E27. Data retention
**Decision: nothing is deletable except never-submitted draft jobs (default 30 days), pre-upgrade snapshots (rolling 5, min 14 days), routine backups (per C14 schedule), and diagnostic logs (90 days / 1 GB). Legal hold supported at user, project, scenario, and job levels. Admin-mediated user/project export. No self-service GDPR DSAR portal in v1; admin-mediated.**

**Q1 — What can ever leave the system (confirmed exhaustive list):**
- **Never-submitted draft jobs** older than `draft_jobs_days` (default 30) — reaped by a cron worker; working dir + uploaded files removed alongside the row.
- **Pre-upgrade snapshots** (per C13) — pruned by retention policy.
- **Routine backups** (per C14) — pruned by per-destination retention policy.
- **Diagnostic logs** — the application's own stdout/stderr (not the audit log), rotated by age and size.

**Never deleted, ever:** user accounts, projects, scenarios, submitted jobs, audit log entries, licence material, approvals/rejections, tool/workflow definitions, ACL records.

**Q2 — Configurable retention windows (defaults shown):**
```
[retention]
draft_jobs_days = 30                # only never-submitted drafts; per D20
upgrade_snapshots_keep = 5          # rolling — keep last N pre-upgrade snapshots
upgrade_snapshots_min_days = 14     # never auto-prune one less than 14 days old
backup_keep_daily = 7               # see also C14
backup_keep_weekly = 4
backup_keep_monthly = 12
diagnostic_logs_days = 90           # web/worker stdout/stderr
diagnostic_logs_max_size_mb = 1024  # rotate at this size regardless of age
```
All customer-configurable.

**Q3 — Legal hold:**
- A super-user can apply a **legal hold** flag at any of: **user**, **project**, **scenario**, **job** levels (per clarifier 2 — user-record-level hold is included).
- A user-level hold pins **all** activity authored by or attributable to that user (their drafts, their projects' draft-reapable items, their referenced backups).
- A project / scenario / job hold pins that record and everything beneath it in the hierarchy.
- While anything is held, the retention sweeps **skip held material** with an audit entry per skipped item (so the suppression itself is auditable).
- Backups that contain held material are exempted from pruning until the hold is released. Pre-upgrade snapshots that contain held material are similarly preserved.
- Apply/release events are audit-logged with a mandatory free-text reason.
- A super-user-only "Held items" admin view lists all current holds, when applied, by whom, the stated reason, and the impacted scope.

**Q4 — Export tooling (v1, admin-only):**
```
matofx export user <user-id>          # archive of one user's authored content + their personal data
matofx export project <project-id>    # archive of one project, its scenarios, jobs, inputs, outputs, ACL, audit subset
```
- Output: structured tarball with manifest, signed by the per-install Ed25519 key (so a recipient can verify integrity).
- Includes a snapshot of relevant audit-log entries (chain context preserved enough to verify in isolation).
- Excludes inherited material the export target doesn't own (no "export project X" leaking content owned by other projects).
- Admin-mediated for both: there is no self-service export endpoint in v1.
- The export action itself is audit-logged with reason.

**Q5 — Import (v1.x, not v1):**
- Symmetric `matofx import ...` deferred. Cross-install migration in v1 uses backup → restore (per C14). Re-evaluate based on customer demand.

**Q5 amendment — 2026-05-15 — Tools & Workflows migration moved into v1:**

The blanket deferral above is superseded for the **tools + admin-released workflows** subset. Backup→restore is too heavy-handed for the dominant real-world use case — promoting a freshly-tested tool or workflow from a Staging install to a Production install on the same network — and would clobber Prod's users, projects, jobs, and audit chain. Cross-install migration of users / projects / jobs / scenarios / audit / tutorials / runtime settings remains deferred to v1.x (backup→restore still covers those).

**Scope (in v1):**
- Admin-released tools (descriptor + `static/`, at one or more versions).
- Admin-released workflows.

**Out of scope (not in v1 of this feature):**
- Custom workflows (user-owned scratch; different user base on the dest install).
- Jobs, projects, scenarios, users, teams, audit log entries, tutorials, runtime settings, branding, retention configuration. Use backup→restore if a full clone is wanted.

**Transport:**
- **Signed tarball only.** No peer-to-peer HTTP between installs, even on the same subnet. Preserves the "no outbound at runtime" security property (line 462). Same-subnet hosts use SCP/SFTP — that is an operator-IT concern, not a MATOFX feature.
- Bundle = `matofx-migrate-<utc-timestamp>-<short-source-id>.tar.gz` containing a single signed `manifest.json` + signature + per-tool descriptors/`static/` + per-workflow definitions. Per-file integrity is covered by hashes carried in the manifest; only the manifest is signed.
- Signing key is the source install's existing per-install Ed25519 key (the same key used to sign exports per Q4).

**Trust:**
- The dest admin maintains a small **trusted-peer list** of source install Ed25519 fingerprints + human label + added-by + added-at. Adding/removing fingerprints is audit-logged.
- Bundle import verifies the signature against this list. A signed bundle from an untrusted source install is refused with a clear "fingerprint X is not in the trust list; add it first" message. Prevents a random signed bundle from a foreign install slipping in.

**Permission and gating:**
- A single permission `migrate.tools_workflows` covers export, import, and trust-list management.
- Default-on for `super_user`; `super_user` can grant it to `administrator`. Default-off for every other role.
- Feature-flagged via `features.cross_install_migration` (per the project-wide rule, line 1435) so customers without a staging tier never see the UI.

**Atomicity — per-item, not per-bundle:**
- Each tool and each workflow imports independently in its own DB transaction.
- One failure does not abort the rest. Skipped items are listed in the import status log with a plain-English reason and a "how to resolve" line.
- Cascading skips: a workflow whose referenced tool failed to import is itself skipped, with the dependency cited.

**Cross-install reference resolution — by stable string identifier, never by row ID:**
- Tool → AOS host: by **host name**.
- Tool → HPC cluster: by **cluster name**.
- Tool → Python env: by **env name**.
- Tool → storage location: by **name**.
- Tool → licence config: by **name**.
- Workflow → tool: by **tool alias** (already true in v2).
- Workflow → approver: by **username/email**.
- On import, the resolver matches by name on the destination. Matched → bind. Unmatched for a tool dependency (host/cluster/env/storage/licence) → that tool is skipped. Unmatched approver in a workflow → that approver entry is dropped (with a status-log note); the workflow itself is still imported.

**On-import behaviour summary:**

| Carried over | What happens |
| --- | --- |
| Tool descriptor + `static/` | Imported byte-for-byte; descriptor hash recorded; container image rebuilt locally from the descriptor on the dest (same as a fresh `matofx tool install`). |
| Tool settings (licence, AOS, HPC, Python env, storage, resource requirements, queue, approval-policy flag, email-notifications flag) | **Preserved exactly from source** via reference-by-name resolution. |
| Tool lifecycle state | **Reset to `new`** on dest, regardless of source state. Admin must explicitly promote through `test`/`released` on the dest. |
| Workflow definition (member tools, ordering, naming, descriptions) | Imported as-is; tool refs resolved by alias on dest. |
| Workflow approver list | Matched by username/email; unmatched entries dropped with a status-log note. |
| Workflow lifecycle state | Reset to `new` on dest (consistent with tools). |

**Conflict policy:**
- `(alias, version)` already exists on dest with the **same descriptor hash** → no-op (idempotent re-import is safe).
- `(alias, version)` already exists on dest with a **different descriptor hash** → tool skipped with: "version 1.5 already exists here with a different descriptor; bump the version on the source and re-export."
- Workflow alias collision → workflow skipped with the equivalent message.

**Audit:**
- Source-side: `migrate.bundle.exported` with bundle hash, contents inventory, free-text reason.
- Dest-side: `migrate.bundle.imported` with bundle hash, per-item outcomes, source install fingerprint.
- Trust list changes: `migrate.peer.trusted` / `migrate.peer.untrusted` with fingerprint + reason.

**UI surface — single page, `Admin → Migrate Tools/Workflows`:**
1. Trusted peers panel (collapsed by default) — list, add, remove.
2. Export panel — multi-select tools (by name + version) and workflows (by name), optional notes, **Generate bundle** → browser download.
3. Import panel — file-drop for `.tar.gz`; two-stage flow:
   - **Dry-run** (no writes): verify signature, parse manifest, predict per-item outcomes. Renders a plain-English preview.
   - **Commit**: streams live per-item status to a scrollable log; final summary with next-step checklist.
- The whole page is designed for an operator who has never done this before to follow without external help. Plain-English status lines and errors are mandatory; technical detail (hashes, fingerprints) appears in collapsible "details" toggles, never as the primary message.

**Documentation:**
- A dedicated `MIGRATE_GUIDE.md` walks the staging→prod flow end-to-end. The plain-English error messages in `matofx.migrate.errors` are the single source of truth for what the UI shows and what the guide quotes — kept in sync by lint.

**CLI parity:**
- `matofx migrate export --tools <alias[:ver],…> --workflows <alias,…> [--notes "…"] --out bundle.tar.gz`
- `matofx migrate import bundle.tar.gz [--dry-run]`
- `matofx migrate trust add <fingerprint> --label "Staging"` / `trust list` / `trust remove <fingerprint>`
- Mirror of the UI; same plain-English status output to stdout.

**Clarifier 4 — GDPR DSAR (admin-mediated):**
- For Article 15 access requests, an admin runs `matofx export user <id>` and hands the archive to the data subject through customer-controlled channels.
- For Article 16 (rectification): standard admin user-edit flow (audit-logged).
- For Article 17 (erasure): the PII-scrub pathway from D20 — replace identifiable fields with deterministic hashes, keep the row + audit chain intact.
- No public-facing self-service "download my data" UI in v1. Same admin-mediated principle as password reset (D19) and MFA reset (D18).

Implications to carry forward:
- The cron worker that reaps draft jobs respects every legal-hold flag; if it ever skips one, it logs the reason. CI test: "creating a hold on a draft prevents reaping."
- Export tooling shares serialisation logic with the backup engine — keeps the manifest format consistent and avoids double-implementation.
- The "diagnostic logs" path is configurable so that the customer's centralised logging tooling (rsyslog / Windows Event Forwarding / etc.) can pick up the files without conflict; this is independent of (and lower-stakes than) the audit log.
- Documented data inventory (per E22) is updated whenever a new entity is added; retention rules and the export tooling reference it as the source of truth for "what fields belong to a user / project".

---

## Project-wide rule: every feature is Funis-toggleable per customer at ship time

**Every non-essential feature in MATOFX SPDM v2 must be individually toggleable by Funis when preparing a customer-specific licence file. The customer never sees a feature that they have not paid for or asked for.**

This generalises the feature-flag mechanism from C16. Flags live in the customer's signed licence manifest, evaluated at runtime by a single feature-flag service. Disabling a flag must:

- Hide the feature's UI (admin and user pages) — gone, not "greyed out".
- Refuse the corresponding API/internal calls.
- Skip its scheduled jobs / cron tasks.
- Skip its install-time checks (e.g. don't validate HPC config if HPC is off).
- Not appear in the configuration documentation that customer admins see.

Why: not every customer needs every capability. Some customers will not want HPC integration, won't run approvals, won't enable read-logging in audit, won't use custom workflows, won't enable backup encryption, won't use SSO, etc. Every such capability must be cleanly removable from the live install via the licence file alone — no rebuild, no separate installer, no bespoke customer fork.

**How features are catalogued.** A single, versioned **feature catalog** (`features.toml` in the source) lists every toggleable capability, its default state, and its dependencies on other features. Examples (illustrative — exact set evolves as we build):

```
[features.hpc]
default = true
description = "Submission and tracking of jobs to external HPC schedulers and Rescale."

[features.approvals]
default = true
description = "Pre/post-approval gates on jobs and workflows."

[features.custom_workflows]
default = true
description = "User-defined workflow variants (custom_workflows table)."

[features.audit.read_logging]
default = true
description = "Per-session-per-record read events in the audit log."

[features.backup.encryption]
default = false
description = "Built-in passphrase encryption of backup tarballs."

[features.sso]
default = true
description = "SAML and OIDC authentication providers (in addition to local accounts)."

[features.delegation]
default = true
description = "Approver delegation windows (per D21)."

[features.parametric_studies]
default = true
description = "Parametric studies — fan a tool over a generated input matrix (per G39a / ADR-0001)."
```

Funis preparing a customer's licence file selects the subset that customer is paying for / has requested; the licence manifest's `features` map carries the flags; the signed manifest ships with the install.

**What is NOT toggleable** (always-on, structural to the product):
- Authentication itself (some auth must be present).
- The audit log itself (only the *scope* of what it records is partially toggleable; the log existence is not).
- The licence subsystem itself.
- The permissions / RBAC system.
- Project → Scenario → Job hierarchy.
- TLS / encryption-at-rest infrastructure (the *option* of which key-source backend is configurable; the encryption capability is not "removable").

**Implementation discipline:**
- A single `feature_enabled("name") -> bool` function (cached for the request's lifetime) is the only authority on flag state. Business logic and UI components ask it; every rendering path branches on it cleanly.
- Adding a new toggleable feature means updating `features.toml` first, then writing the code behind a flag check from the start. CI lints for "feature defined but never queried" and "code path gated on a flag not in the catalog."
- The licence manifest's `features` map can carry only flags that exist in the catalog; unknown flags are rejected at licence-load time with a clear error.
- Tests run against multiple flag profiles (e.g. "all-on", "all-off-where-allowed", "minimum viable") to catch regressions where disabling a feature breaks a sibling.

---

## F. Job execution security

### F28. Tool sandboxing
**Decision: customer-developed (or third-party-built / customer-reviewed) tools, container-sandboxed by default for *every* tool — snappy or long; tools never run in-process or as raw subprocess of the worker. A simple Python orchestrator per tool in `static/`. MATOFX does all heavy lifting; the tool author writes the smallest possible amount of glue code. Resource limits, no-network-by-default, no-cross-job-access, GPU passthrough opt-in, and read-only "shared mounts" supported.**

**Q1 — Trust model: customer-developed tools (option c, generalised).**
- Customers (or **third-party suppliers / integrators developing for them**, which the customer reviews before deployment) write tools.
- Funis does **not** review or sign tools; the customer's admin is responsible for what they install. The licence file does not gate tool installation.
- Tools are therefore treated as **untrusted code that the customer chose to install** — sandboxed strictly, even when the customer "trusts" them.
- A future "Funis-signed tool catalog" remains a possibility but is out of scope for v1.

**Q2 — Sandbox: container per tool, no exceptions.**
- **Every tool execution runs inside its own container instance** — regardless of expected duration or resource profile. No in-process Python SDK fast path.
- Reason: even sub-second tools commonly require **specific Python versions and module sets** the worker process cannot host; a uniform sandbox model means one place to enforce isolation, one execution code path to test, and no "snappy fast path" that drifts from the strict path's security properties.
- **Performance compensation** (essential — performance and security are equally important per the project-wide rule below):
  - **Pre-pulled, cached images.** Tool images live in the local image store; never pulled at job time.
  - **Warm container pools per tool.** Worker keeps N idle pre-started containers per registered tool (configurable per-tool); a job claims a warm container, runs, and the container is rolled (start-up cost amortised, not per-job).
  - **Lightweight base images.** Minimal Python image (Alpine + slim Python or Wolfi-based) by default; the tool author can override with their own base if they need bulkier deps.
  - **Container runtime: Podman first** (faster startup than Docker, rootless-friendly, no daemon, OCI-spec compatible). Compose remains v2-spec for portability (per B10).
  - **Snappy-tool target latency:** warm-pool overhead in the low tens of milliseconds; well below user-perceptible thresholds for snappy calculator-style tools.
- **Tool authors NEVER write a Dockerfile or otherwise interact with containers.** The container is an implementation detail of MATOFX's sandboxing, completely invisible to the tool author. The tool descriptor declares high-level needs (Python version, pip dependencies, system packages, GPU requirements, network egress, shared mounts), and MATOFX **synthesises the container image automatically at tool-install time** from a curated set of base runtimes shipped with the install. (Project-wide rule recorded below.)
- Container image lifecycle is fully MATOFX-managed: build (synthesised from descriptor) at install, store in the local image cache, prune on uninstall, refresh on tool update. Never reaches the network — base runtimes are pre-bundled per the air-gap rule.

**Tool execution always orchestrated by the tool's Python file in `static/`.**
- Each tool ships a Python orchestrator (e.g. `static/main_model.py`) that runs *inside* the container.
- The orchestrator is short and simple: it imports the MATOFX tool SDK (B11 / DDP-replacement), reads pre-validated inputs, generates whatever the underlying tool wants (an APDL deck, an Abaqus `.inp`, a custom binary), launches the underlying tool, captures outputs, writes outputs.json. **MATOFX does the rest:** licence checks, queueing, container start, file staging, audit logging, retries, cancellation, GPU allocation, HPC routing, results delivery, notifications.
- Target line count for a typical tool orchestrator: tens to low hundreds of lines, not thousands.

**Q3 — Resource limits and runtime needs, declared in the tool descriptor (`*_descriptors.json`) — high-level only, no Dockerfile:**

```
"runtime": {
  "base": "matofx/python:3.12",        // from curated MATOFX-shipped base library
  "pip_requirements": ["numpy==2.3.0", "scipy==1.16.0", "trimesh==4.8.0"],
  "system_packages": ["libgl1"]        // rarely used; only when the tool truly needs OS packages
},
"resources": {
  "cpu_cores": 2,
  "memory_mb": 4096,
  "wall_clock_seconds": 3600,
  "disk_quota_mb": 8192,
  "gpu_required": false,
  "gpu_memory_mb": 0
},
"network": {
  "egress": "deny"            // | "deny" | "tool-server-allowlist" | "explicit-list"
},
"mounts": {
  "shared_read": [
    "matofx://material-libraries:/data/materials",   // symbolic name maps to admin-configured host path
    "matofx://reference-data:/data/reference"
  ]
}
```

The `runtime.base` value selects from a **curated library of base images** that Funis ships and maintains. Customers and tool authors do **not** ship their own Dockerfiles. If a tool needs something not yet covered (e.g. a CAD-bundle base, a specific Fortran runtime), Funis adds a new base to the curated library; the tool author then references it by name.

Enforced by the container runtime:
- **CPU and memory limits** map directly to cgroup / Job Object constraints.
- **Wall-clock timeout** kills the container if exceeded; the job moves to `Failed` with a clear reason.
- **Disk quota** caps the per-job working dir (loop device or filesystem quota — implementation detail, but enforced).
- **Network egress denied by default**, lifted only when explicitly listed (e.g. for tools that legitimately need to call out to a customer-controlled licence server, or for "remote tool server" tools that talk to their backend).
- **No access to other jobs' working dirs** — bind-mount the *one* job dir, nothing else.
- **No access to MATOFX's DB / Valkey / secrets / licence material / audit log** — strict allow-list of what the container can see.
- **GPU access is opt-in** per tool (`gpu_required: true` plus optional `gpu_memory_mb`); the runtime requests GPU device passthrough (per Q4 confirmation) using the standard NVIDIA / vendor container toolkit.

**Q4 — Remote tool servers (confirmed):**
- Some tools live on separate machines and talk back via a custom API; MATOFX is the client.
- TLS to the remote tool server is mandatory (per E24); credentials live in `SecretProvider` (per E25).
- Egress is **per-tool-server allow-listed** in the network egress config (per C15).
- The remote tool server is treated as untrusted input on the way back: outputs are validated against the tool's output JSON schema before being persisted as job results.
- A compromised remote server can corrupt the job's results, but cannot reach back into MATOFX's data layer or other jobs — same blast-radius limit as a compromised local container.

**Q5 — HPC tools (confirmed):**
- HPC submission, polling, and result retrieval are wrapped behind the same execution-backend abstraction (per A4) as local containers.
- Sandboxing of the actual job is the HPC cluster's responsibility — that's how customers run HPC.
- MATOFX-side guarantees: no MATOFX secrets are shipped with the job; outputs are validated against schema on return.
- HPC backend credentials in `SecretProvider`; per-HPC-backend circuit breakers (B9) protect the queue.

**Clarifier 3 — tool SDK and "MATOFX does the heavy lifting":**
- A tool SDK is shipped (the DDP-replacement from B11) — but it is **non-restrictive and does not impose a heavy programming model.** Tool authors that need to drop down to raw subprocess calls inside their orchestrator can do so; the SDK provides convenience, not a cage.
- The SDK *is* the canonical pathway for: reading validated inputs, writing validated outputs, structured logging that surfaces in MATOFX's UI, heartbeats for cancellation, progress events.
- **Project-wide goal (recorded as a rule below): MATOFX does the heavy lifting; tool authoring is as simple as possible.** The orchestrator Python file should be the smallest piece of glue the tool reasonably needs to be.

**Clarifier 5 — read-only shared mounts:**
- Tools may legitimately need access to **material libraries, reference data, calibration files, etc.** that live outside the job's working dir.
- Declared in the tool descriptor under `mounts.shared_read` (host-path : container-path : read-only). Bind-mounted as read-only into the container.
- Customer admins manage the host-side directories; per-tool descriptors specify what each tool needs to see.
- Cross-tool sharing of mount paths is fine — material libraries are typically shared across many tools.
- **Read-only enforced by the container runtime** — a tool cannot scribble back to a shared mount.

---

## Project-wide rule: performance and security are equally important

> **No design, dependency, or implementation choice may sacrifice one for the other. When the two pull in opposite directions, surface the trade-off explicitly and pick the option that gives the best of both — usually a more thoughtful design rather than a corner cut.**

This is the user's explicit guidance, repeated multiple times: "performance and security are equally important." It biases:
- Container choice (Podman + warm pools, not "skip the sandbox to be fast").
- Database tuning (WAL + connection pooling, not "skip transactions").
- Audit log (synchronous writes for state-changes, batched async for reads — not "skip read logging because it's slow").
- Encryption (modern AEAD ciphers with hardware acceleration — not "skip TLS internally").
- The web tier (NiceGUI on FastAPI with sticky sessions — not "skip session affinity to scale horizontally" or "skip the sandbox").

Whenever a recommendation seems to be "either fast or safe", flag it; the right answer is almost always a third option that is both.

## Project-wide rule: MATOFX does the heavy lifting; tool authoring is dead simple

> **One of the major hurdles of existing SPDM solutions is their complexity. MATOFX v2 must be radically simpler for tool authors than competitors.** A tool's orchestrator file should focus on what makes the tool unique (which solver to call, how to translate inputs into the solver's format, how to extract results) and inherit everything else from MATOFX.

What MATOFX provides automatically (tool authors do **not** write any of this):
- Form rendering from `*_descriptors.json`.
- Input validation against the tool's schema.
- File staging (working dir creation, `static/` copy, shared-mount setup).
- Container build, warm-pool management, container launch with the right limits.
- GPU allocation / HPC routing.
- Cancellation propagation.
- Retry / failure handling.
- Audit logging.
- Licence enforcement.
- Notifications.
- Result persistence and downstream-tool wiring (workflows).
- Approval gating.
- Cleanup (working-dir trimming per descriptor).

What the tool author writes:
- The `*_descriptors.json` (form layout, input schema, output schema, resource needs, mounts).
- A short Python orchestrator in `static/` that reads inputs (via SDK), runs whatever the tool's underlying mechanism is, and writes outputs (via SDK).
- (Optional) Tool-specific assets in `static/`.

The brief tool orchestrator pattern (illustrative — not the final SDK shape):
```python
from matofx.tool_sdk import Tool

with Tool() as t:
    t.log.info("Starting heat transfer model")

    deck = render_apdl_deck(t.inputs)             # tool-specific
    result = run_solver(deck, cwd=t.workdir)      # tool-specific
    t.outputs.update(extract_results(result))     # tool-specific
```

If a tool author can write more than ~200 lines of orchestrator code without being obviously domain-specific, MATOFX is failing them — fold the repeated work into the SDK or the framework.

---

Implications to carry forward:
- Container runtime choice (Podman primary, Docker also supported per B10) needs benchmarking early — warm-pool latency targets validated on real hardware.
- Tool image lifecycle (build at install, prune on uninstall, refresh on update, never pull from network) needs a clean CLI: `matofx tool install <bundle>`, `matofx tool update <bundle>`, `matofx tool uninstall <alias>`. Bundles are tarballs containing the **descriptor + static/** only — never a Dockerfile. MATOFX builds the image automatically from the descriptor and a curated base.
- The tool SDK is one of the first deliverables — many other modules depend on its contract.
- Egress allow-list integration: the per-tool `network.egress` setting is honoured at the container-network layer (deny-by-default network namespace + iptables / nftables rules) and at the MATOFX-side outbound HTTP wrapper for remote tool servers.
- Resource-limit enforcement is exercised in CI: a malicious-tool test fixture (a tool that tries to fork-bomb, exhaust RAM, write outside its working dir, open arbitrary network connections) must be confined as expected.
- Shared-mount UX: an admin UI page for managing host-side shared directories, with per-mount notes ("material libraries v3, last updated by Alice"); referenced from tool descriptors by symbolic name to avoid hard-coded paths.

### F29. Tool signing
**Decision: hash-record-at-install + verify-at-submit (option b) is the default integrity check; customer-side cryptographic signing (option c) deferred to v1.x. Verification fires at install, update, and job-submit. Tools have version pins; submitted jobs are locked to the tool version that existed at submit time. Old tool versions are retained forever.**

**Q1 — Default integrity mode: (b) hash-record-at-install + verify-at-submit.**
- At `matofx tool install <bundle>` time, MATOFX computes a deterministic hash of the bundle (the descriptor + static/, in canonical serialisation per Q5) **and** of the synthesised container image manifest. Both hashes are recorded in the DB and the audit log.
- At every `job submit` time, MATOFX re-verifies the on-disk hashes match the recorded hashes before allocating a container. Any drift → job is refused with a `tool.tampering.suspected` audit event, and the admin is notified.
- Default-on for every install. Cost is a few milliseconds per submit; no UX impact.
- **Does not prevent tampering** (a privileged actor can edit anything on the box) — but **detects** it loudly.

**Q2 — Customer-side cryptographic tool signing (option c): deferred to v1.x.**
- The interface is reserved in the codebase (`tool.signed_by` field in the descriptor + an optional detached signature next to the bundle), but signature verification is not wired to actually gate installation in v1.
- When the customer demand surfaces, the workflow will be: customer designates signing identities, generates Ed25519 keypairs (preferably YubiKey-backed), signs bundles with `matofx tool sign <bundle>`, MATOFX is configured with the customer's public keys and verifies signatures at install / update / submit.
- Per-signer revocation, signed audit-log entries on every install/update/submit, and key rotation are part of the v1.x design.
- For v1, hash-based integrity (b) is the only mechanism shipped.

**Q3 — Verification fires at three points:**
- **Install time** — bundle hash + image hash recorded; if anything fails (bundle malformed, image build failure, descriptor schema-invalid), installation is refused.
- **Update time** — same as install; on success the new version's hashes are recorded *alongside* the old (old version retained per clarifier 4).
- **Job-submit time** — both hashes re-checked. Mismatch → job refused, audit event emitted.

**Q4 — Tool versioning and provenance (confirmed):**
- Each tool descriptor declares a `version` field (semver).
- Each `matofx tool install` / `matofx tool update` event is audit-logged with version, installer identity, hashes.
- **Submitted jobs are pinned to the tool version that existed at submit time** (clarifier 3 confirmed). If an admin upgrades the tool between submit and run, the queued job still launches the original version.
- Admins can also pin a specific project / scenario to a specific tool version (locked behaviour for reproducibility), independently of submit-time pinning.

**Q5 — Bundle format (confirmed):**
- A tarball containing the tool's directory tree: descriptor, static/, any tool-specific assets. **No Dockerfile** (per the Funis-builds-the-image rule above and the project-wide rule below).
- Reproducible serialisation: file order canonical, mtimes normalised, no platform-specific bits — same shape we use for licence files and release tarballs.
- Hash = SHA-256 of the canonical tarball.
- Optional `.sig` Ed25519 detached signature (used when v1.x customer-side signing arrives).

**Clarifier 4 — old-version retention (forever):**
- When a tool is upgraded (e.g. 1.3.0 → 1.4.0), **the 1.3.0 image and bundle are retained forever.**
- Old-version retention is a hard rule, mirroring D20's "nothing deletable" principle for substantive system data.
- Disk-space implication: tool image storage grows monotonically. SPDM customers expect this — reproducibility of historical jobs is the product.
- A future "image-store compaction" feature (deduplication of identical layers across versions) is a possible v2 optimisation; not in v1.

Implications to carry forward:
- Tool install / update / submit-time hash verification is implemented in one module; the same code path is later extended to verify customer-supplied signatures when v1.x signing arrives.
- Audit-log entries for tool events include both hashes (bundle + image) and the tool version, making any future investigation straightforward.
- Backups (per C14) cover the tool image store; the restic deduplication is critical here, since tool images can be GBs and many versions accumulate.
- A `matofx tool inspect <alias>[:<version>]` admin command shows the recorded hashes, install history, and any tampering events from the audit log — useful for support diagnostics.

---

## Project-wide rule: tool authors never write Dockerfiles or interact with containers

**Tool authors and tool integrators must never need to write a Dockerfile, build a container image, or otherwise know that containers exist as part of MATOFX's sandboxing.** The tool plugin contract surfaces only high-level concepts: Python version, pip dependencies, optional system packages, resource needs, network egress, shared mounts. MATOFX synthesises the container image from these declarations using a curated library of base images that Funis ships and maintains.

**Why:** The user said on 2026-05-02: *"I would definitely prefer not to use Docker at all, ever. It complicates things for people building tools and integrating them into MATOFX. If a Docker container is generated automatically by MATOFX, that could be acceptable."* This complements the "MATOFX does the heavy lifting" rule: tool authors should never have to learn or maintain DevOps tooling to ship a tool. The simpler the tool-authoring surface, the more tools customers will write, and the faster MATOFX delivers value.

**Implications:**
- Tool descriptors declare runtime needs in **high-level fields** (`runtime.base`, `runtime.pip_requirements`, `runtime.system_packages`); never raw Dockerfiles.
- MATOFX maintains a **curated base-image library** shipped inside the install (e.g. `matofx/python:3.10`, `matofx/python:3.11`, `matofx/python:3.12`, plus specialised variants like `matofx/python+cad:1.0`). The library expands per release as new tool needs surface.
- The tool plugin SDK and documentation deliberately avoid Docker terminology in tool-author-facing surfaces (admin-side / sysadmin-side content can refer to containers when relevant — the *audience* is the distinguishing factor).
- If a tool genuinely needs an unsupported base, the resolution is **Funis adding a new curated base**, not the tool author writing a Dockerfile.
- **Note (audience separation):** This rule is about *tool authors*. The MATOFX install itself is delivered to customer admins via container images (per B10) — that's a different audience (sysadmin, not tool author) and is acceptable.

**What the tool author actually ships (full picture):**
- `tools/<alias>/<alias>_descriptors.json` — form layout, input/output schema, runtime needs, resource limits, mounts.
- `tools/<alias>/static/` — Python orchestrator file + any tool-specific assets.
- (Optional) `tools/<alias>/static/requirements.txt` — pip deps, if preferred over inline list in the descriptor.

That's it.

### F30. Input schema validation
**Decision: descriptor is the authoring surface; MATOFX auto-derives Pydantic models + JSON-Schema for runtime validation. Inputs + outputs validated at descriptor-load, form-render, job-submit, tool-launch, and tool-completion. Strict / closed schemas. Schemas frozen per `(tool_alias, tool_version)`. Cross-field constraints expressed in a small sandboxed DSL. Per-field, plain-English error messages.**

**Q1 — Schema location: (c) descriptor → auto-derived schema.**
- Tool authors edit only the `*_descriptors.json`. They never write a JSON-Schema file by hand.
- At descriptor-load time, MATOFX generates:
  - A **Pydantic v2 model** for fast runtime validation (used by the form-render path, the submit endpoint, and the SDK inside the container).
  - A **JSON-Schema (Draft 2020-12) document** auto-derived from the Pydantic model, exposed as machine-readable documentation and used for any external schema-aware tooling (e.g. an admin's "validate this inputs.json against tool X v1.2" CLI helper).
- Both representations are kept in sync by construction — single source of truth is the descriptor.

**Q2 — Validation coverage (confirmed):**
- **Inputs:** field types, field-level constraints (`min`, `max`, `pattern`, `enum`/`options`, `required`), file-shape constraints (extension allow-list, size cap, optional magic-byte check), cross-field constraints, authorisation context (user can reference the materials / shared assets they've selected).
- **Outputs:** same shape (types, constraints, presence, file-shape) plus content hashes for any output file participating in the approval data-hash signature (per D21).
- Cross-field constraint syntax: a small **expression DSL** evaluated in a sandboxed evaluator (e.g. `simpleeval`, MIT — licence-clean) with access to the input fields by name and a fixed set of operators (`+`, `-`, `*`, `/`, `==`, `!=`, `<`, `<=`, `>`, `>=`, `and`, `or`, `not`, `in`, `len`). No imports, no builtins, no attribute access. Each constraint carries a custom human-readable error message.

```jsonc
"cross_field_constraints": [
  {
    "expression": "component_1_percent + component_2_percent + component_3_percent == 100",
    "error_message": "Component percentages must add up to 100%."
  },
  {
    "expression": "operating_temperature <= max_safe_temperature",
    "error_message": "Operating temperature can't exceed the material's max safe temperature."
  }
]
```

**Q3 — Validation fires at five points (recommendation accepted):**
- **Descriptor-load time** (tool install / update): the descriptor is validated against MATOFX's meta-schema; bad descriptors are refused at install and never reach a running install.
- **Form-render time:** the UI applies constraints to NiceGUI inputs (`min`, `max`, `required`, option lists, regex patterns) so users get instant in-line feedback. Display-side; not authoritative.
- **Job-submit time:** authoritative server-side revalidation against the Pydantic model. Failure → submission refused with **per-field, plain-English error messages** surfaced next to each field in the form.
- **Tool-launch time** (inside the container, via the SDK): the SDK re-validates inputs as a safety net against any drift introduced between submit and launch.
- **Tool-completion time:** outputs validated against the output schema before persistence; failure → job → `Failed` with `tool.output.invalid` audit event and a clear admin-readable explanation.

**Q4 — Strict / closed by default.**
- Unknown / extra fields rejected on both inputs and outputs. Catches typos and prevents extra-field smuggling.
- Tool versions add fields explicitly; old jobs replay against their pinned version's frozen schema (per F29).

**Q5 — Schema versioning (confirmed):**
- Schemas are referenced by **`(tool_alias, tool_version)`** — never by alias alone.
- Each tool version's schema is **frozen** at install. New jobs against an upgraded tool use the new schema; in-flight jobs (submitted before the upgrade) continue with their pinned version's schema.

**Q6 — Schema language: Pydantic v2 → JSON-Schema.**
- Pydantic v2 (MIT — licence-clean, already in the stack via FastAPI) is the runtime engine.
- The auto-derived JSON-Schema (Draft 2020-12) is the documentation/export representation.
- The `jsonschema` library (MIT) is available for any place we need to validate against the JSON-Schema directly (e.g. external tooling); not on the hot path.

**Clarifier 1 — Cross-field constraints: small DSL (locked).**
- Expression DSL via `simpleeval` (MIT). No Python imports, no attribute access, no builtins, no eval, no f-strings.
- Tool authors do not need to ship Python predicate functions for cross-field rules — the DSL is enough for ~all real cases. If a tool genuinely needs more, it can validate inside the orchestrator (via the SDK) and emit a descriptive error.
- Cross-field rules run server-side at submit time. The UI surfaces the error message under the offending field group, not as a standalone toast.

> **Supersede note (2026-05-16, maintainer-approved):** the evaluator is **not** the `simpleeval` package. It is an in-house ~150-line audited `ast`-walking evaluator (`matofx.tools.forms.safe_expr`) implementing the *same* fixed grammar (`+ - * / == != < <= > >= and or not in len`, parentheses, names → submitted field values, literals; **no** imports, attribute access, builtins, calls other than `len`, comprehensions, or f-strings). Rationale: the grammar is small and fixed, and the project's supply-chain posture (vendoring, hashed lockfile, SBOM, reproducible builds, "minimise dependencies") makes a self-owned evaluator on the input-validation hot path preferable to a new third-party runtime dependency. Behaviourally identical to the locked decision; only the implementation owner changes. The same evaluator powers §16.16 conditional-dependency rule conditions (see `ARCHITECTURE.md` §16.16) and the §F30 cross-field constraints.

**Clarifier 2 — File-upload checks (limited):**
- **Yes:** size cap, extension allow-list, magic-byte sniffing for STEP/STL/PDF/JPEG/PNG and similar well-known formats.
- **No:** virus scanning, deeper content-type analysis, file-content sandboxed parsing. Not needed for v1; can add later if a customer requires it (e.g. a ClamAV adapter behind a feature flag).

**Clarifier 3 — Error message UX (confirmed):**
- Per-field, plain-English error messages, surfaced next to the offending field in the form.
- Cross-field errors surfaced under the offending field-group, with the custom `error_message` from the descriptor (never raw expression text).
- Tool authors writing `error_message` strings are guided (in docs and lint) to use a friendly, action-oriented voice ("Operating temperature can't exceed the material's max safe temperature" — not `op_temp > max_safe (FAIL)`).

Implications to carry forward:
- The Pydantic model generation from the descriptor is one of the first SDK-adjacent modules built — many components depend on it (form rendering, submit validation, SDK validation).
- An admin-facing tool `matofx tool validate <bundle>` runs the descriptor through the meta-schema and reports issues *before* the admin tries to install — saves a lot of round-trip pain.
- The "frozen schema per tool version" property requires the descriptor + auto-generated schema to be **stored alongside the version** in the install (the same way old container images are retained per F29). On startup, MATOFX hydrates the schema for every installed-and-retained version.
- CI must include a "schema authoring lint" step that checks `*_descriptors.json` against the meta-schema and fails the build on any in-tree tool that drifts.

---

## Project-wide rule: SPDM end-users are not modellers — keep everything simple and helpful

**Most MATOFX SPDM users have no modelling, simulation, or domain-expert background. They want to enter inputs and get answers.** Every user-facing surface — forms, error messages, status text, navigation, dashboards, help, notifications — must be as **simple, plain-English, and helpful as possible**, with sensible defaults, inline guidance, and never any jargon a non-engineer wouldn't understand.

**Why:** The user said on 2026-05-02: *"most of the people using an SPDM might have no actual modelling and simulation experience, they just want to enter inputs and get an answer, so everything must be as simple and helpful as possible."* This is a major differentiator from existing SPDM products, which are notorious for assuming domain expertise.

**How to apply:**
- **Plain-English everywhere.** "Submit" not "Initiate solver dispatch". "This calculation will take about 5 minutes" not "Wall-clock estimate: 300s".
- **Per-field error messages**, action-oriented and field-localised. Never "Validation error: 422" or "input.foo.bar.baz: required".
- **Sensible defaults** for every optional field. The user should be able to fill the minimum and submit.
- **Inline tooltips / help icons** on each input explaining what it is and what units it uses. The descriptor format includes a `description` and `help` field per input.
- **Empty states with next-step guidance** — first time a user lands on a page, they see what to do next, not a blank screen.
- **Progress + estimated-time indicators** on long-running jobs.
- **Explicit confirmation prompts** for destructive or irreversible actions, with clear "this cannot be undone" language.
- **Status text in human terms** — "Waiting for HPC slot" not "queued/HPC-poll/circuit_breaker_open".
- Avoid disclosing internal mechanics in user-facing surfaces (no stack traces, no technical error codes, no MATOFX-internal terminology). Admin-facing surfaces can be more technical.
- When designing any new UI element, the test is: *would a person who has never seen a simulation tool understand what this is and what to do next?* If not, redesign.

This is a layered companion to the "MATOFX does the heavy lifting" rule — that one was about *tool authors*, this one is about *end users*. Both push toward radical simplicity.

### F31. File staging
**Decision: read-only bind-mount of tool static (default) or per-job copy (optional, per descriptor); content-addressed store for user-uploaded input files; per-job working dir is transient scratch space cleaned via an orchestrator-declared `retain` list at job end (with substantive content preserved at the tool-version and CAS layers); single job-storage volume; per-job quotas advisory in v1; cleanup events audit-logged with what/when/why.**

**The reproducibility ↔ working-dir-cleanup tension, resolved.**

Reproducibility (per D20: "nothing deletable except never-submitted drafts") doesn't conflict with cleaning the per-job working directory — because nothing reproducibility-relevant *only* lives there:

- **Tool static files** are stored at the `(tool_alias, tool_version)` level (per F29 — old versions retained forever). They are mounted/copied into a job's working dir for that one run, and then can be safely cleaned because the canonical copy never went away.
- **User-uploaded input files** live in a **content-addressed store** (CAS); the per-job working dir holds only symlinks or copies. The CAS is immutable and reference-counted; files referenced by any surviving job are retained forever.
- **Output files** worth keeping are listed in the orchestrator's `retain` list; those are *moved out* of the per-job working dir into permanent job-output storage at job end (see Q5).

So the per-job working directory is **transient scratch space**: everything substantive is anchored elsewhere. Cleaning it after the orchestrator's `retain` list is honoured loses nothing reproducibility-relevant.

**Q1 — Default static-file delivery: read-only bind-mount, with per-descriptor copy override.**
- **Default mode (b):** the tool's `static/` directory at the pinned version is bind-mounted **read-only** into the container at `/static`. No per-job duplication.
- **Opt-in mode (a):** descriptor field `static.copy_into_job: true` triggers a per-job byte-for-byte copy of `static/` into the working dir at submit time. Useful for tools that need a writable copy of an asset (rare) or for customers who require "the working dir contains a complete self-contained snapshot."
- A per-customer global setting (`file_staging.default_static_mode = "mount" | "copy"`) lets a customer flip the default for all tools at install time without per-tool descriptor edits.
- **At end-of-job, any copied static files are removed** as part of the per-job working-dir cleanup (per Q5). Bind-mounts are simply unmounted; no cleanup needed there.

**Q2 — Working-dir layout (locked):**
```
/static       (read-only)   — bind-mount of tools/<alias>/<version>/static/  (or copy-in if descriptor opts in)
/job          (read-write)  — per-job working directory
  inputs.json                (validated user inputs; written by MATOFX at submit time)
  outputs.json               (written by the tool; validated by MATOFX on completion)
  tool_log.jsonl             (structured progress events from the SDK)
  inputs/                    (uploaded files staged here at submit time — symlinks into the CAS)
  outputs/                   (tool writes result files here)
/data         (read-only)   — bind-mount of any shared mounts declared in the descriptor (per F28 mounts.shared_read)
```

**Q3 — Content-addressed store for user-uploaded inputs (option b, retained per D20).**
- Every uploaded file is hashed (SHA-256) at submit time. The CAS is `<job_storage>/cas/<hash[:2]>/<hash>` (sharded by hash prefix to keep directory sizes sane).
- The per-job `inputs/` directory contains **symlinks** (per clarifier 1 — works on both Linux and Windows since Vista with `SeCreateSymbolicLinkPrivilege`) pointing into the CAS.
- **Reference counting:** a `cas_refs` table tracks `(content_hash, referencing_job_id)`. A CAS file is retained as long as **any surviving job (including drafts kept by retention policy) references it.** Since submitted jobs are never deleted (per D20), CAS files referenced by them are retained forever.
- **Reaping:** when a draft job is reaped (per D20's draft-retention policy), its CAS references are decremented. A CAS file with zero references and no surviving holders can be deleted by the same draft-reaper. Deletion logged with hash, file size, and reason ("CAS file orphaned by draft job retention").
- This balances the user's "I like option b" with "nothing deletable for reproducibility" cleanly: anything reachable from a real job is reachable forever; only true orphans (uploaded only by a never-submitted draft that's now expired) ever get cleaned.

**Q4 — Single job-storage volume (confirmed).**
- One configured `<job_storage>` path per install. Per-tool / per-project volumes deferred.
- **Per-job quotas are advisory in v1 (per clarifier 3):** the descriptor's `disk_quota_mb` is a *warning threshold*, not a hard kill. When a job exceeds it, MATOFX:
  - Logs a `tool.quota.exceeded` audit event with job id, tool, configured quota, observed size.
  - Surfaces the warning in the admin UI's "Warnings" view.
  - Lets the job continue.
- Hard quota enforcement (kill on exceed) is deferred to v1.x — needs proper FS-quota / overlayfs / loop-device support which is OS-specific and harder to make reliable across Linux + Windows. We don't ship a half-baked version.

**Q5 — Cleanup behaviour (confirmed):**
- The **tool's Python orchestrator declares a `retain` list** — paths (relative to `/job`) that survive cleanup. Everything else in the working dir is deleted at job end.
- Default `retain` (when not declared): `outputs.json`, `tool_log.jsonl`, `outputs/**`. Nothing else.
- **On success:** apply the retain list. Retained files are moved to permanent job-output storage; the working dir is removed.
- **On failure or cancellation:** **skip cleanup entirely** — keep the full working dir intact for diagnosis. Admin can later trigger cleanup via `matofx job archive <id>` if they're satisfied the diagnosis is complete.
- **Working-dir created at form-open time (per clarifier 2):** opening the submission form creates the per-job dir so uploaded files have somewhere to live during input. The dir exists for drafts; if the draft is reaped (per D20), the dir is reaped with it.
- All cleanup actions write detailed audit-log entries (see "Cleanup audit logging" below).

**Q6 — `FileStorage` abstraction (confirmed):**
- Single `FileStorage` interface; one implementation in v1 (`LocalFileStorage`, on the configured `<job_storage>` path).
- Future implementations (S3-compatible, network share, etc.) plug in through the same interface without touching call sites — same pattern as `KeyProvider` (E23) and `SecretProvider` (E25).
- Symlinks-vs-fetch differences are encapsulated inside the implementation: an S3-backed storage would stage files into the container at launch time rather than via symlink.

**Cleanup audit logging (per the user's general comment):**
- Every working-dir cleanup event writes a `job.workdir.cleaned` (or `job.draft.reaped` for drafts, `cas.orphaned.deleted` for CAS reaps) **audit-log entry** with:
  - Path of the directory or file that was deleted.
  - Job id (and draft / submitted state).
  - Age at deletion (e.g. "32 days since creation").
  - Reason (e.g. "draft job retention: `draft_jobs_days = 30`", "successful job cleanup against orchestrator retain list", "admin-triggered archive").
  - Inventory summary: number of files, total bytes, list of retained items (the orchestrator's `retain` list as it ran).
  - Operator (the cron worker, the admin user, or the system identity).
- Admin UI exposes a **"Cleanup activity" view** that filters the audit log to these events — answers "what got deleted, when, and why" at a glance, without having to grep raw audit entries.
- Drafts that were created and never populated still produce an audit entry on reap (with the inventory summary saying "no files staged" / "0 bytes"), so the admin sees that too.

**Clarifier 1 — symlinks (locked):** symlinks on both Linux and Windows. CI tests both. Hard-link fallback only inside an installer-guided path on filesystems that don't support symlinks (rare).

**Clarifier 2 — working-dir at form-open (locked):** dir created when the user opens the submission form so uploads have a destination during draft authoring. "Never-submitted draft" cleanup includes its working dir.

**Clarifier 3 — quotas advisory in v1 (locked):** warning-only; hard enforcement deferred.

Implications to carry forward:
- The CAS is one of the first storage primitives built; tested for race conditions on concurrent uploads of the same content.
- The `retain` list mechanism is part of the SDK API surface (`Tool.retain(path)` or descriptor-declared); both forms supported, with the descriptor's static list being the primary authoring surface and the SDK call available for tools that compute the retain set dynamically.
- The "Cleanup activity" admin view is a small wrapper over the audit log — no new datastore. Reuses E26's query infrastructure.
- `matofx job inspect <id>` admin command exposes the working-dir contents (live or archived), the retain list as it ran, and any quota warnings — useful for tool-author debugging.
- Draft-reaper cron job (per D20) coordinates with the CAS reference counter — decrement first, then delete the working dir, then delete any orphaned CAS entries. Idempotent so a crashed reaper resumes cleanly.

### F32. HPC integration
**Decision: pluggable `HPCBackend` interface; v1 ships three first-class backends (Slurm, Rescale, HPC Pack); REST primary with SSH/CLI fallback; per-cluster credentials via SecretProvider; three file-transfer modes (REST upload / shared FS / SCP); reconciliation + circuit-breaker per backend; cancellation + pre-staging supported in v1; 1-minute default polling; HPC events recorded in MATOFX's own audit log.**

**Q1 — First-class HPC backends in v1: Slurm + Rescale + HPC Pack.**
- **Slurm** — modern open-source default; dominant in newer engineering shops; clean REST via `slurmrestd`.
- **Rescale** — managed cloud HPC option; full REST API.
- **HPC Pack** (Microsoft) — included in v1 per clarifier 1, since some simulation software is Windows-only and only runs on a Windows HPC environment. REST API since HPC Pack 2019.
- All three implement a single `HPCBackend` interface — same contract for submit / status / cancel / transfer-in / transfer-out / reconcile. Adding others (PBS Pro, LSF, AWS Batch, Azure Batch, Grid Engine variants) later is implementation-only, no architectural change.
- Each backend is its own feature flag in the catalog (per E27 project-wide rule) — Funis can ship a customer with any subset enabled.

**Q2 — Submission protocol: REST primary, SSH/CLI fallback.**
- For each backend, the **REST API path is the primary integration**; documented as a prerequisite the customer's HPC team enables (`slurmrestd` for Slurm, the HPC Pack web service for HPC Pack, Rescale's hosted API).
- **SSH + CLI fallback** is supported for Slurm clusters that haven't enabled REST and for any future backend without one. SSH keys live in `SecretProvider` (per E25); session reuse via `controlmaster` to avoid per-call SSH overhead.
- Per-cluster config picks the protocol:
  ```
  [hpc.clusters.acme_engineering]
  backend = "slurm"
  protocol = "rest"          # | "ssh"
  endpoint = "https://slurmrestd.acme.local:6820"
  auth = "jwt"               # backend-specific
  ```

**Q3 — Submission payload and status flow (confirmed):**

Submission payload to the HPC:
- Validated `inputs.json`.
- Tool bundle's `static/` (or a pointer to a pre-staged copy — see clarifier 2).
- A small **wrapper script** generated by MATOFX that activates the tool's runtime environment on the cluster, sets working dir, and runs the orchestrator.
- Resource requests (CPU, memory, walltime, GPU) derived from the descriptor.
- Job metadata (MATOFX job id, tool `alias:version`, scenario id, project id) attached as scheduler-side comments / labels for cross-reference and accounting.

Status mapping:
- HPC `Pending`/`Queued` → MATOFX `Queued (HPC)`.
- HPC `Running` → MATOFX `Running`.
- HPC `Completed` → MATOFX retrieves outputs, validates against schema, marks `Complete`.
- HPC `Failed` / `Cancelled` / `Timeout` → MATOFX `Failed` with the corresponding reason and a copy of any stderr/log fragments retrievable.
- HPC `Disappeared` (job not found on next poll, no terminal record) → MATOFX `Failed` with `hpc.job.disappeared`.

**Q4 — Per-cluster credentials (confirmed):**
- Each `HPCBackend` declares its supported auth methods (Slurm: JWT, basic, mTLS; HPC Pack: NTLM/Kerberos, mTLS; Rescale: API token).
- Admin configures per-cluster auth in `matofx.toml`; credentials always loaded via `SecretProvider` (`_file` / `_env` forms — never inline).
- All outbound TLS strictly verified (per E24); per-cluster custom CA bundles supported.
- Audit-log: cluster connection events (success / failure / circuit-breaker trips) per E26 Bucket C.

**Q5 — File transfer (all three modes — confirmed):**
- `HPCBackend` has `transfer_in(job_id, files)` and `transfer_out(job_id, paths)` methods.
- Three implementations behind the same interface:
  - **REST upload** (where the backend supports it — Slurm `/jobs/.../files`, Rescale's storage API, HPC Pack's web service). Default for Rescale.
  - **Shared filesystem** (NFS / SMB the customer has set up so both MATOFX and the cluster see). Default for many on-prem Slurm/HPC Pack installs.
  - **SSH/SCP/SFTP** fallback. Default when nothing else is configured.
- Per-cluster choice in config (`hpc.clusters.<name>.transfer = "rest" | "shared_fs" | "ssh"`).
- Large-file optimisation (parallel SCP / chunked REST upload) is built into each implementation; tools shipping multi-GB inputs aren't blocked.

**Q6 — Failure handling and reconciliation (confirmed):**
- **Per-cluster circuit breaker** (per B9): repeated transfer or polling failures trip the breaker; new submissions to that cluster are deferred (and surfaced to the user as "HPC cluster X is currently unreachable") until the breaker closes again.
- **Reconciliation loop** on worker startup (per B9): every job in `Queued (HPC)` or `Running` state is re-checked against the cluster; state is updated to match reality. Survives MATOFX crashes and cluster outages cleanly.
- **Vanished jobs** (gone from the cluster, no terminal record) → `hpc.job.disappeared` event in audit + job moves to `Failed` after a configurable grace period (default 5 minutes — accommodates brief scheduler glitches without immediately writing off a job).
- **Output-retrieval failures** → exponential-backoff retry; eventually `hpc.outputs.unreachable` if the cluster never gives them back. The job is marked `Failed`; admin can manually retry retrieval later via `matofx job retrieve <id>`.

**Q7 — Cancellation (confirmed):**
- Cancellation propagation is part of the `HPCBackend` interface (`cancel(job_id)`).
- On user / admin cancel, MATOFX:
  1. Marks the MATOFX job `Cancelling`.
  2. Calls `backend.cancel()` (REST `DELETE` / `scancel` / `qdel`).
  3. Waits up to a configurable timeout for confirmation; once seen, marks `Cancelled`.
  4. Best-effort retrieves any partial outputs / logs from the cluster.
  5. Audit-logs the full sequence.

**Clarifier 1 — HPC Pack included (above).**

**Clarifier 2 — Pre-staging supported in v1:**
- Some customers want to install a tool's runtime environment on the cluster *ahead of time* — typical for solvers that need a heavy module load, license file path, or large fixed assets. Shipping the runtime with every job is wasteful or impossible.
- Two pre-staging modes per cluster:
  - **Auto pre-stage:** `matofx hpc stage <cluster> <tool:version>` pushes the tool's runtime + static into a known location on the cluster and registers it. Subsequent submissions reference the pre-staged copy.
  - **Customer-managed pre-stage:** the cluster admin installs the runtime out-of-band and writes a small mapping file MATOFX reads (`/path/to/runtimes.json` on the cluster); MATOFX's submission script picks the right pre-installed runtime per tool version.
- Submissions for a pre-staged tool ship only `inputs.json`, the wrapper, and any user-uploaded files — not the static directory. Significant transfer reduction for tools with large static assets.
- A **`matofx hpc verify-stage`** command exists to confirm a pre-staged runtime matches the expected hash (per F29's hash records) — protects against drift / cluster-side tampering.

**Clarifier 3 — Polling cadence: 1 minute default.**
- `hpc-poll` worker (per B9) ticks every 60 seconds by default. Configurable per cluster (`hpc.clusters.<name>.poll_interval_seconds`); minimum 10s to keep load reasonable on the cluster.
- Per-job adaptive polling deferred — overengineering for v1.
- The audit-log Bucket B "job execution events" (per E26) records only **state transitions** observed by polling, not every poll tick — keeps the audit log signal-rich and avoids drowning it in heartbeats.

**Clarifier 4 — HPC events in MATOFX's own audit log only.**
- We do **not** mirror the cluster's own scheduler log into MATOFX. The cluster's accounting/log records are the cluster's responsibility.
- MATOFX records every HPC interaction (submit, status change observed, cancel, transfer in/out, auth event, circuit-breaker trip) in its own audit log per E26.
- For diagnosis, the admin can also retrieve scheduler-side log fragments on demand (`matofx job hpc-log <id>`) — that's an interactive query, not a routine mirror.

Implications to carry forward:
- The `HPCBackend` interface is one of the larger v1 deliverables; testing requires either real clusters or solid mocks. Plan a Slurm-in-a-container test harness for CI; HPC Pack and Rescale tests use recorded-response mocks.
- Each backend is a separate Python module under `matofx.execution.hpc.<backend>`; only those modules import backend-specific dependencies (`pyslurm`, `rescale-sdk`, etc.). Feature flag controls whether the module is loaded — avoids importing Rescale's SDK when HPC Pack is the only backend a customer uses.
- The wrapper script generator is shared across backends — single template that fills in cluster-specific bits (module loads, scheduler directives) at submit time.
- Pre-stage tooling integrates with the tool image-store (per F29): `matofx hpc stage` reads the pinned tool version's bundle and pushes it; verifies against the recorded hash on the cluster side.
- Outbound egress allow-list (per C15) needs each cluster's endpoint registered explicitly. The hpc cluster config feeds into the egress allow-list automatically — admin doesn't have to register endpoints in two places.

### F33. GPU scheduling
**Decision: GPU work routed to local GPUs / dedicated GPU workers / HPC / Rescale through the same execution-backend abstraction; static routing per tool with per-project admin override; per-GPU exclusive scheduling locally; multi-GPU jobs supported (declared `gpu_count` in descriptor); a job is never executed until enough GPUs are simultaneously free, otherwise it waits in the `gpu` queue; NVIDIA-only in v1; CPU fallback opt-in per tool; no GPU-specific licence gating; GPU monitoring/UI out of scope.**

**Q1 — GPU placements (recommendation confirmed):**
- **Local GPUs on the MATOFX host(s)** (host has cards attached).
- **Dedicated GPU worker hosts** (separable per A5 — same MATOFX worker pointed at the same Valkey + DB, on a host with GPUs).
- **HPC GPU partitions** via the F32 backends (Slurm GPU partitions, HPC Pack with GPU nodes).
- **Rescale GPU instances** via the F32 Rescale backend.
- All four behind the same execution-backend abstraction; the tool author doesn't change anything to use a different placement.

**Q2 — Routing: static (a) + admin override (c). Jobs never run until resources are available.**
- **Static routing default per tool** (descriptor `execution.placement = "local-gpu" | "hpc:cluster_name:partition" | "rescale" | "auto"`).
- **Per-project / per-tool admin override** sets a different placement when policy demands it (e.g. "Project A always goes to HPC; Project B's GPU work runs locally").
- **No automatic load-balancing** across placements in v1 — placement is decided at submit time and stays.
- **Wait-until-resources-available is a hard rule.** A job that needs N GPUs (locally) does not start until N GPUs are simultaneously free. It sits in the `gpu` queue (per B9) with the user seeing "Waiting for GPU resources" (plain English per the end-user UX rule). No premature execution; no partial-resource starts.
- For HPC and Rescale placements, "wait" is the cluster's concern — MATOFX's `Queued (HPC)` state already covers it.

**Q3 — Local scheduling: per-GPU exclusive (c).**
- Each physical GPU is held by **exactly one running job at a time.** No memory-packing, no oversubscription.
- Multi-GPU jobs hold N adjacent (in the simple "set of GPU ids" sense) physical GPUs simultaneously while running.

**Local GPU allocator (the mechanism that enforces Q2 + Q3):**
- A small allocator inside the worker process tracks GPU inventory and per-job allocations.
- **Inventory** is configured at install time (`[gpu] devices = [0, 1, 2, 3]`) — a list of physical GPU device indices the customer has dedicated to MATOFX. Auto-detection (`nvidia-smi -L`) is available as a convenience flag but never the default; customers may want to reserve some cards for non-MATOFX use.
- **Atomic allocation:** when a `gpu`-queue worker picks up a job, it asks the allocator for the requested `gpu_count`. The allocator either returns a set of free GPU ids (allocation recorded in DB / Valkey set) or returns "insufficient" — in which case the worker re-queues the job with a small delay (e.g. 5 s) and tries again. Single source of truth for "which GPU is held by which job"; no double-allocation possible.
- **Release:** on job terminate (success / fail / cancel), the allocator returns the GPUs to the free set.
- **Impossible requests** (e.g. job needs 4 GPUs but install has 2) are rejected at **submit time** with a clear plain-English error: "This tool needs 4 GPUs, but only 2 are available on this server. Ask your administrator to add more GPUs or run this tool on the HPC cluster instead."
- **Fairness:** simple FIFO across the `gpu` queue is enough for v1. Priority queues (per B9) can preempt at the queue level if a customer needs VIP-fast-lane behaviour.

**Q4 — Container GPU passthrough (confirmed):**
- Containers requesting GPU are launched with the runtime's GPU-passthrough flag (`--device nvidia.com/gpu=<id>` for Podman with the NVIDIA Container Device Interface; equivalent for Docker engine if used).
- **NVIDIA Container Toolkit** is a documented prerequisite on any host with GPUs MATOFX should use; install-guide section provides install commands per supported OS.
- The container sees the assigned GPU(s) via the standard NVML / CUDA stack; the tool's orchestrator just imports its preferred framework and finds the device(s).
- `matofx doctor` checks the toolkit is present and a CUDA-capable runtime is wired up when the install has GPUs configured.

**Q5 — Vendor support: NVIDIA only in v1.**
- AMD (ROCm) and Intel (oneAPI) are deferred. The `GPUAllocator` interface keeps the door open for non-NVIDIA backends (the device-id concept generalises) but no implementation ships in v1.
- Apple Silicon out of scope (servers, not workstations).

**Q6 — Behaviour when no GPUs are available anywhere (confirmed):**
- **Default: wait indefinitely.** Job stays in the `gpu` queue with status "Waiting for GPU resources" until the allocator can satisfy it. No time-out, no auto-fail. The user can cancel via the standard cancellation path (per B9) if they no longer want to wait.
- **CPU fallback opt-in per tool.** If the descriptor sets `runtime.gpu_optional: true` plus a sensible CPU fallback (`runtime.cpu_only_orchestrator_arg: "..."` or similar), the user is offered a "run on CPU instead" choice when no GPU is available within a configurable wait window. This is a tool-author affordance, not a default behaviour.
- **HPC / Rescale waits** are the cluster's responsibility; MATOFX simply reflects their queue state.

**Q7 — No GPU licence gating (confirmed):**
- The licence file (per C16) does **not** count or cap GPU usage. Hardware is the customer's; the licence is per-user (CSL / FAL).
- Should commercial demand for a "GPU-job seat" model ever materialise, the feature flag mechanism (per E27) can introduce it without a re-architecture — but v1 ships with no such cap.

**Clarifier 1 — Multi-GPU jobs supported in v1:**
- Tool descriptor declares `resources.gpu_count: int` (default 1).
- The allocator handles multi-GPU allocation atomically (all N GPUs allocated in one transaction or none).
- The container is launched with the right device list (`--device nvidia.com/gpu=<id1>,nvidia.com/gpu=<id2>,...`).
- Tool authors writing multi-GPU code use whatever framework convention applies (torch.distributed, NCCL, etc.); MATOFX's job is to ensure the right number of devices are visible inside the container — no further framework-specific support.
- **HPC routing** with multi-GPU: the wrapper script requests `--gpus-per-node N` from the scheduler. Cluster-side configuration (which partition / queue) is the customer's call.

**Clarifier 2 — `gpu_memory_mb` retained as a hint:**
- Useful for HPC routing (request a partition with ≥ N MB GPU memory) and for documentation / capacity-planning.
- Not used by the local allocator (we're per-GPU exclusive — full card per job).
- Tool authors include it in the descriptor when the tool has a real minimum; otherwise it's optional.

**Clarifier 3 — GPU monitoring out of scope.**
- No live "GPU 0 currently running job 1234567, 87% memory" panel in MATOFX's UI. Customers use their existing monitoring stack (Prometheus + DCGM exporter, vendor tooling, etc.).
- The audit log records GPU allocations / releases per job (Bucket B per E26), so historical "which job was using GPU 0 at 14:32?" answers are queryable; that's separate from live monitoring.

Implications to carry forward:
- Local GPU allocator is one of the core scheduling primitives. Tested for: concurrent allocation requests, multi-GPU atomic allocation, allocator state recovery after worker crash (allocations recorded in DB so a restart sees the truth).
- Inventory configuration is a `matofx doctor`-checked item — count of `[gpu] devices` matches `nvidia-smi -L` output (or warns clearly if not).
- The `gpu` queue worker concurrency is set to the inventory size; multi-GPU jobs claim multiple slots from the queue at once via the allocator's all-or-nothing logic.
- The "GPU optional / CPU fallback" path is implemented as **two separate execution profiles per tool**, both defined by the tool author — MATOFX picks based on availability + user choice. Documented in the SDK / descriptor reference.
- `matofx doctor` and the install guide both call out the NVIDIA Container Toolkit prerequisite loudly — getting GPU support wrong is a top-N install support call.

---

## G. UX and product scope

### G34. Project → Scenario → Job/Workflow hierarchy
**Decision: hierarchy preserved as-is; workflow as a first-class object referencing constituent jobs; single `workflow_definitions` table for both released and custom templates; per-tool descriptor declares "upstream-capable" inputs, workflow definition declares the dependency graph; sequential execution with dependency-aware halt (independent branches keep running on a partial failure); released-workflow approvers named by the admin at definition time; custom-workflow approvers come from an admin-managed global pool; scenarios clonable + movable but only by users who are creator/owner of both source and destination projects.**

**Q1 — Hierarchy locked.**
- `Project` → `Scenario` → (`Job` | `Workflow`).
- A `Workflow` is itself made of `Job`s (one per tool step).
- ACL inheritance, no-deletion, soft-archive, draft-reaping (D20) apply throughout.

**Q2 — Workflow as a first-class object (option b).**
- A new `workflows` table holds workflow instances. Each `jobs` row has a nullable `workflow_id` FK.
- Workflows have their own state (`Pending Approval` / `Running` / `Complete` / `Partial` / `Failed` / `Cancelled`), separate from individual job states.
- Implementation discipline: the model stays small — `workflow_id` + `workflow_state` + `workflow_definition_id` + `workflow_definition_version` + a few timestamps. No exotic graph structures in the relational schema; the DAG lives in the workflow *definition* (a JSON document), not in the jobs table.

**Q3 — Workflow templates: one table for both released and custom.**
- **`workflow_definitions`** table holds both released (admin-created) and user-created custom workflow templates. Single source of truth.
  - `kind: "released" | "custom"`.
  - `version` field; old versions retained forever (per F29 / D20).
  - Owners/permissions follow the same model as projects — released definitions are admin-owned; custom definitions have a creator and are shared per their permissions.
- **`workflows`** table holds per-scenario *instances*, each pointing at a `workflow_definitions` row + a pinned `version` (so an in-flight or historical workflow always replays against the template version it was started with).
- The legacy `workflows` and `custom_workflows` tables disappear; `workflow_yes_no` on `jobs` is replaced by the `workflow_id` FK.

**Q4 — Wiring: descriptor declares "upstream-capable" inputs; workflow declares dependencies.**

Both sides matter:

- **Tool descriptor (per-tool — written by the tool author):**
  ```jsonc
  "outputs": {
    "thermal_conductivity": { "type": "number", "unit": "W/m·K" },
    "specific_heat":         { "type": "number", "unit": "J/kg·K" }
  },
  "inputs": {
    "thermal_conductivity": {
      "type": "number", "unit": "W/m·K",
      "upstream_capable": true       // can be filled from an upstream tool when in a workflow
    }
  }
  ```
- **Workflow definition (per-workflow — written by the integrator/admin):** declares the **dependency graph** between tools.
  ```jsonc
  "steps": [
    { "id": "step1", "tool": "material_properties", "label": "Define material" },
    { "id": "step2", "tool": "heat_transfer",       "label": "Heat transfer", "depends_on": ["step1"] }
  ]
  ```
- **Wiring rule (default — name-matching):** for each `upstream_capable` input on a downstream step, MATOFX automatically wires from any **directly upstream step** whose **output of the same field name and matching type** exists. The integrator does not need to write field-level mappings for 90% of cases.
- **Explicit override (when names diverge):** the workflow definition can specify a per-input mapping using a simple dotted path:
  ```jsonc
  "input_mapping": {
    "thermal_conductivity": "step1.outputs.k"   // upstream output is named differently
  }
  ```
  Resolved at workflow-instance start; ambiguity (e.g. two upstreams produce the same field) is a definition-time error.
- **Validation at definition load:** every `upstream_capable` input on every step must have *either* a matching upstream output by name *or* an explicit `input_mapping` entry (when the workflow runs as a workflow). If a tool requires a field upstream-only and nothing wires it, the definition is rejected at install/save time with a clear plain-English error.

**Clarifier 1 — input-mapping examples (resolves the dotted-path question):**

Approach picked: **name-matching by default, dotted-path override.**

Examples:

```jsonc
// 1. Name-matching default (no mapping needed):
"steps": [
  { "id": "mp", "tool": "material_properties" },
  { "id": "ht", "tool": "heat_transfer", "depends_on": ["mp"] }
]
// → MATOFX auto-wires `material_properties.outputs.thermal_conductivity`
//   into `heat_transfer.inputs.thermal_conductivity` because both share the name + type.

// 2. Explicit mapping when names differ:
"steps": [
  { "id": "mp", "tool": "material_properties" },
  {
    "id": "ht", "tool": "heat_transfer", "depends_on": ["mp"],
    "input_mapping": {
      "thermal_conductivity": "mp.outputs.k",
      "density":              "mp.outputs.rho"
    }
  }
]

// 3. Multi-source (two upstreams, mixed wiring):
"steps": [
  { "id": "mp",  "tool": "material_properties" },
  { "id": "geo", "tool": "geometry_processor" },
  {
    "id": "ht", "tool": "heat_transfer", "depends_on": ["mp", "geo"],
    "input_mapping": {
      "thermal_conductivity": "mp.outputs.thermal_conductivity",
      "mesh_file":            "geo.outputs.tetra_mesh"
    }
  }
]
```

Dotted paths (`<step_id>.outputs.<field>`) are easier to read/write than JSONPath (`$.steps[0].outputs.thermal_conductivity`) and good enough for v1.

**Q5 — Execution: sequential, dependency-aware (kept simple per user's request).**
- Single tool runs at a time, in topological order of the dependency graph.
- When a step fails, MATOFX **halts only the descendants of the failed step** in the dependency graph; **independent steps that are not downstream of the failure continue to run** (per Q6 clarification).
- Steps that did not run because their parent failed end up in a `Skipped` state (audit-logged with the upstream failure reason).
- Workflow ends in:
  - `Complete` — all steps `Complete`.
  - `Partial` — some steps `Complete`, some `Skipped`, none `Failed` (rare; only if a `Skipped` parent had no failure, which can't happen — so realistically `Partial` means "some descendants were skipped due to a failure, but other independent branches succeeded"). Surfaced as `Failed` to the user with details if any branch failed.
  - `Failed` — at least one step `Failed`.
- True parallel-DAG execution (running independent ready steps simultaneously) is **deferred to v1.x**. v1 still walks topologically but executes sequentially — keeps the implementation lean while supporting the dependency semantics.

**Q6 — Failure handling (locked):**
- **Halt the descendants of the failed step**, not the entire workflow. Independent branches continue.
- **Tweak-and-retry-from-failed-step**: the user opens the failed step, adjusts inputs, resubmits *that step*. Downstream steps re-execute from there using the new outputs (in dependency order); steps that already completed and aren't downstream of the retried step are not re-run.
- The retried step gets a fresh approval cycle if its tool / workflow requires approval.

**Q7 — Project / scenario operations:**
- **Empty projects allowed**; UI prompts to create a first scenario.
- **Scenarios clonable** within or across projects.
- **Scenarios movable between projects** — admin-mediated, audit-logged. **Constraint (per the user's clarification):** *the user performing the move must be the creator/owner of **both** the source project **and** the destination project.* If they are not creator/owner of either side, the move is refused. Super-users / admins can move scenarios across projects regardless (audit-logged, naturally).
- **Scenarios are never deleted** (per D20). On clone, the original scenario is preserved; the clone has its own ACL inherited from the destination project's defaults (no override carry-over from the original — clarifier 3).
- **Project ownership transfer** (per D20 clarifier 4) is the admin path for cases where the original owner is gone but the project must keep going.

**Clarifier 2 — Workflow approvers (released vs custom):**

This was open in D21; we resolve it here.

**Released workflows (admin-created):**
- When the admin creates or edits a released workflow definition, MATOFX checks whether any constituent tool has `approvals != "none"`.
- If yes, MATOFX **prompts the admin to designate one or more workflow-level approvers** for the whole workflow as a single approval gate (per D21).
- These approvers can be **any users** (not constrained to the union or intersection of constituent tools' approver lists).
- **Only one approval is needed** even if multiple approvers are listed. First eligible approver to sign off wins.
- The workflow definition cannot be saved if any constituent tool requires approval and no workflow-level approvers are designated.

**Custom workflows (end-user-created):**
- End users typically don't have visibility into who is authorised to approve what — making them choose approvers is the wrong UX.
- **Solution: an admin-managed "custom-workflow approver pool"** maintained per install (a flat list of users designated as eligible to approve any custom workflow). One pool per install — no per-tool / per-workflow customisation in v1.
- When an end user saves a custom workflow whose constituent tools include any approval-required tool, MATOFX automatically attaches the custom-workflow approver pool as the workflow's approver list. The user does not choose.
- **Edge case:** if the admin has not configured the pool, end users cannot save custom workflows that include approval-required tools — MATOFX surfaces a clear message: *"Custom workflows that need approval can't be saved until your administrator sets up the custom-workflow approver pool. Please contact your admin."* Custom workflows that contain only non-approval tools save normally.
- The pool's contents are managed in the admin space; changes follow the policy-versioning rule (per D21 clarifier 1) — in-flight custom-workflow approvals stick to the pool as it was when the cycle started.

**Approval data-hash on workflows:**
- The Ed25519-signed approval record's `data_hash` (per D21) covers the **whole workflow's** concatenated, canonical inputs (for a pre-gate) or outputs (for a post-gate) — not per-step. One approval, one hash, one signature.

Implications to carry forward:
- The workflow execution engine is a small topological-sort + per-step dispatcher inside the worker tier — straightforward in v1, naturally extends to parallel-DAG in v1.x without re-architecting.
- The `workflow_definitions` table replaces the legacy split (`workflows` + `custom_workflows`) cleanly. Migration of legacy data (per I44) maps both into this single table with the appropriate `kind`.
- The "wiring rule" (name-matching by default with dotted-path override) is implemented in one place, exercised in CI tests with both happy paths and ambiguity cases.
- The custom-workflow approver pool is one config block + one admin UI page — small and well-contained.
- Scenario-move authorisation is part of the `can(user, "scenario:move", scenario)` check, with the dual-project-ownership rule baked in.

### G35. Custom workflows feature
**Decision: any user with the (default-on) `create_custom_workflow` permission can build one from tools they're authorised to run; user-only ACL (Owner/Editor/Viewer); workflow definitions reference tools by alias only — tool-version pinning happens at instance start; deactivate-not-delete; admin promotion of custom → released; per-user/per-shared visibility; globally unique names; "start from released workflow" supported as optional template; no hard caps.**

**Q1 — Who can create one (option b).**
- A `create_custom_workflow` permission lives in the feature × access-right matrix (per D20).
- Default-on for the `Regular user` role; `Admin` and `Super-user` always have it; `Read-only / Auditor` never does.
- Customer admins can revoke the permission per role if they want to centralise workflow authorship.

**Q2 — What a custom workflow can contain (option a).**
- Steps may reference any tool the user is **authorised to run** (Owner/Editor on at least one project where the tool is usable).
- A user cannot include a tool they themselves can't run — closes the privilege-escalation-via-composition gap.
- Re-checked at definition save time (catches scope changes between authoring sessions).
- **Released workflows are not nestable** as steps inside custom workflows in v1 (deferred — recursive workflows add edge cases not worth solving yet).

**Q3 — Sharing (locked as recommended).**
- Custom workflow has a single Owner (the creator) plus an ACL of `(user, role)` pairs.
- Roles: **Owner** (full control + reshare), **Editor** (modify definition), **Viewer** (run it, see results).
- Sharing a workflow as Editor does **not** grant the editor the right to run the workflow's tools — they still need project-level access to do that.
- Cross-team sharing is allowed, individual users only (no team principals on workflow ACLs — same rule as project ACLs in D20).

**Q4 — Tool versioning inside workflows (important clarification):**
- **Workflow definitions reference tools by `alias` only**, not by `alias + version`.
- **Tool-version capture happens at workflow-instance start time**, not at definition time. When the user runs an instance of a custom workflow, MATOFX:
  1. For each step, captures the **currently deployed version** of the referenced tool.
  2. Records the captured `(alias, version)` pairs in the instance's manifest.
  3. Pins the instance to those versions for its full lifetime — even if the tool is updated mid-run, in-flight steps use the pinned version (same rule as F29 for standalone jobs).
- Net effect: when an admin updates a tool, **subsequent runs** of any custom (or released) workflow that references that tool naturally use the new version. **In-flight runs** continue with the version they captured at start.
- **Released workflow definitions may optionally pin specific tool versions per step** (`"tool": "material_properties", "tool_version": "1.5"`) — for compliance / reproducibility scenarios where the admin wants to freeze a known-good composition. Default behaviour for both released and custom: latest version at start.
- The workflow *definition* itself is also versioned (per F29 / G34) — old definition versions retained forever; an instance pins both its definition version and its tool versions at start.

**Q5 — Lifecycle (locked):**
- A custom workflow can be **deactivated** by its owner (no new runs; existing in-flight runs continue; historical instances remain inspectable).
- Deactivated workflows hide from the active list; admins and the owner can still find them via the "Archived custom workflows" view.
- **Never deleted** (per D20).
- **Ownership transfer** when the owner leaves the org: admin reassigns ownership to another user; original owner's role on the workflow is reset to Viewer (or removed). Audit-logged.

**Q6 — Promotion to released (admin action):**
- An admin can **clone a custom workflow into a released workflow definition** via `Admin → Workflows → Promote custom`.
- The released version starts at version `1.0.0`; the original custom workflow stays in place.
- During the promotion, the admin:
  - Picks the new released name (must be globally unique among released workflow names).
  - Designates released-workflow approvers (per G34 — released workflows have admin-named approver lists).
  - Optionally pins specific tool versions per step (per Q4 above).
  - Optionally enables `tool_version` pinning to freeze the exact tool composition that worked when the user nominated it for promotion.
- Audit-logged: who promoted, when, which custom workflow was the source, what edits were made during promotion.

**Q7 — Visibility (locked):**
- A custom workflow appears in:
  - The owner's library.
  - Each user's library to whom it was shared (Viewer/Editor/Owner).
  - The admin space (always — per the admin global view).
- It does **not** appear globally to users it wasn't shared with. No "browse all custom workflows" UI for non-admins.

**Clarifier 1 — Names: globally unique.**
- Custom workflow names are unique **across the whole install** — no two custom workflows can share a name regardless of owner.
- The save dialog enforces uniqueness with a clear error message ("A custom workflow named 'Heat Transfer Pipeline' already exists; please choose a different name.").
- Renames are allowed (audit-logged). On promotion to released, the released name must also be globally unique among released workflow names; the released and custom name spaces are independent.

**Clarifier 2 — Templates / "start from a released workflow":**
- The custom-workflow editor supports a **"Start from a released workflow"** action: pick any released workflow the user can run, the editor loads its dependency graph as a starting point, the user edits and saves under a new (unique) name as a custom workflow.
- This is **optional**, not mandatory — users can also start from blank.
- A "Start from one of my custom workflows" duplicate action is the natural sibling; both ship together.

**Clarifier 3 — No hard caps in v1.**
- No per-user limit on custom workflow count.
- No per-workflow step limit.
- UI guidance: short tooltip in the editor ("Workflows of 2–6 steps work best for clarity"). No enforcement.

Implications to carry forward:
- The "tool referenced by alias only, version captured at instance start" rule is implemented in the workflow execution engine; tested with: tool updated mid-run (in-flight uses pinned version), tool updated between two instance starts (each instance captures its own version), tool deleted (impossible per D20, but a tool in `inactive` state should refuse new instance starts with a clear message).
- The custom-workflow editor's "start from released workflow" affordance is one of the more visible UI flows; design it carefully — a wizard / clone-and-edit pattern is common in PLM and works well here.
- The promotion workflow (custom → released) needs clear admin UX with reset-defaults and the optional tool-version-pin step. It's not frequent, but when it happens it should feel deliberate and well-instrumented.
- Globally unique names + per-user-shared visibility creates an interesting UX: a user can't see workflow "X" but they also can't create one named "X". The save-side error message must be helpful without leaking the existence of a private workflow they shouldn't know about (e.g. "A custom workflow with this name already exists in this install" — without revealing who owns it). Worth handling carefully at design time.

### G36. Comparison & reporting
**Decision: same-tool / same-workflow-definition comparisons only (cross-tool refused with a clear message); 2–10 items per comparison; manual selection + named, shareable saved comparison sets; tables + bar/line/scatter/sensitivity charts + 3D overlay + PDF viewer all in v1; logs collapsed by default; CSV + PDF report export; cross-project comparison allowed (ACL-filtered).**

**Q1 — Three comparison modes from one Compare page:**
- **Compare Jobs** — multiple runs of **the same tool** (same `tool_alias`; tool versions may differ — version diffs are flagged in the header).
- **Compare Workflows** — multiple instances of **the same workflow definition** (definition versions may differ; per-step inputs + outputs side-by-side).
- **Compare Scenarios** — multiple scenarios; the view aggregates the comparable jobs/workflows within them, side-by-side per matching tool.

All three reachable from one consolidated Compare page in the user's dashboard. Selection state persists when switching modes.

**Q2 — Selection: manual + saved comparisons (option c).**
- **Manual selection:** browse jobs/workflows/scenarios, tick to add to a comparison set, click "Compare".
- **Saved comparisons** are first-class DB objects: name, owner, ACL, the list of compared item ids, optional description, last-opened timestamp.
- A saved comparison **resolves stale references gracefully**: if one of its items has been archived / soft-deleted (per D20 — never hard-deleted, but state may have changed), the saved comparison still loads with a clear note ("2 of 5 items are archived"); the user can decide whether to remove or keep them.
- Saved comparisons are **shareable** (per clarifier 2) using the same user-only ACL pattern as custom workflows (D20 / G35) — Owner / Editor / Viewer roles.

**Q3 — What gets compared, field-by-field:**
- **Inputs** — every input field, side-by-side. Differences highlighted (cell colouring + delta annotation for numeric fields).
- **Outputs** — every output field, side-by-side. Numeric fields show absolute and percent deltas (`A − B` and `(A − B) / B × 100%`) against a user-chosen reference run.
- **Tool / workflow version** — shown in the header; version diffs explicitly flagged with a small warning ("These items use different versions of the same tool — results may differ for that reason").
- **Status / timing** — submitted / started / completed timestamps; wall-clock duration; queue wait time. Always shown.
- **Approval state** — approver, decision, comment, decided-at, gate (pre/post). Always shown when any compared item is approval-gated.
- **Output files** — STEP/STL/mesh outputs render side-by-side in `ui.scene` with the option to overlay; PDF outputs render side-by-side in `ui.pdf_viewer`.
- **Logs** — included but **collapsed by default** (per the user's clarification). User can expand individual items' logs on demand. Big-log truncation with "load full log" affordance to keep the UI fast.

**Q4 — Cross-tool comparison: refused (option a).**
- Comparing two jobs of **different tools** is not allowed. Selection UI prevents the action; if the user assembles such a set programmatically (e.g. opens an outdated saved comparison), MATOFX surfaces a clear plain-English message: *"This comparison includes jobs from different tools. Comparing across tools isn't supported — please pick jobs from the same tool, or open a Scenario comparison instead."*
- Same tool, different tool versions: **allowed** (encourages "did v2.0 change the answer?" analyses) — the comparison header flags the version mix.
- Workflow comparisons require the **same workflow definition** (definition versions may differ — flagged).
- Scenario comparisons aggregate per-tool; within a scenario comparison, individual job-level breakdowns are still same-tool-only.

**Q5 — Visualisations: full set in v1 (legacy + recommended additions).**
- **Tables** — sortable, filterable, with computed deltas. `ui.aggrid` for large/dense data; `ui.table` for compact summaries.
- **Bar charts** — single-output across N runs.
- **Line charts** — array outputs (e.g. temperature profile over time) overlaid across runs.
- **Scatter plots** — input-vs-output, useful when comparing parametric variants.
- **Sensitivity / parametric plots** — when the compared set sweeps one input, plot output vs input. Auto-detected when the set has a single varying input field.
- **3D overlay in `ui.scene`** — STEP/STL/mesh outputs from multiple runs rendered in a single scene with per-run colour coding, opacity controls, toggle visibility per run.
- **PDF side-by-side** — `ui.pdf_viewer` with synchronised zoom/scroll where useful.
- All charts use **ECharts via NiceGUI's `ui.echart`** (Apache-2.0 — licence-clean) and three.js via `ui.scene`. No new chart-library dependencies.
- Visualisations respect the end-user UX rule (per F30) — defaults sensible, axis labels in plain English with units, tooltips explain what the chart shows.

**Q6 — Export: CSV + PDF in v1, PowerPoint deferred.**
- **CSV export** of the comparison table (one row per compared item, columns are inputs/outputs/metadata as currently displayed).
- **PDF report export** — renders the active Compare view (tables, charts, 3D overlay snapshots, version metadata, timestamps, the user-chosen reference run) into a single PDF for compliance / reporting use.
- **PowerPoint export** is **deferred to v1.x**; flagged as a known follow-up.
- Export actions are audit-logged (per E26 Bucket B as state-changing-ish events — they're export events).

**Q7 — Permissions: ACL-filtered (locked).**
- A user can compare only items they have view access to. The selection UI lists items naturally filtered by `can(user, "view", item)`.
- A saved comparison loaded by a user without access to one or more items shows them as "Item not visible to you" placeholders (the saved comparison doesn't break; it just hides what the current user can't see).

**Clarifier 1 — Min 2, max 10 items per comparison.**
- Hard min: 2.
- Hard max: 10. Above that the UI gets noisy and table rendering hurts.
- The selection UI shows a "8 of 10 selected" counter; further clicks are blocked with a tooltip explanation.

**Clarifier 2 — Saved comparisons shareable.**
- Same user-only ACL pattern as custom workflows (per G35 / D20): Owner, Editor, Viewer.
- Owner can share with any user (subject to D20's cross-team rules); shared users only see items they have access to (per Q7's filter).
- Saved comparisons can be deactivated by their owner; never deleted (per D20).

**Clarifier 3 — Cross-project comparison allowed.**
- A user with view access to items in different projects can compare them. Common engineering need.
- The Compare page surfaces project context for each item ("Project: Cookie Packaging Q3 / Scenario: Variant A") so cross-project sets aren't confusing.

Implications to carry forward:
- The Compare page is one of the highest-value end-user views and deserves polished UX. Plan for design iteration during build.
- The "deltas" pattern (absolute + percent against a chosen reference run) is generic enough to be a small reusable component used elsewhere too (e.g. job results page can use the same delta widget against an upstream value).
- 3D overlay in `ui.scene` requires careful colour-management UX (default contrasting palette + per-run opacity + visibility toggles). Worth a design pass.
- Saved comparison ACLs reuse the same authorisation primitives as custom workflows — same code path, no new policy logic.
- "Across tool versions, same tool" comparisons need the version diff flagged prominently — a subtle but real source of confusion otherwise.

### G37. Notification channels
**Decision: email + in-app only in v1; per-event admin defaults + per-tool overrides + per-user preferences (three layers); SMTP best-effort with retry; in-app stored in DB with N-most-recent retention; HTML+plain-text email with deep links and plain-English subjects; one-email-per-event with optional per-user digest mode; admins receive system events regardless of project access.**

**Q1 — Channels in v1: email + in-app only (option a).**
- No Slack / Teams / outbound-webhook channels in v1. Adds outbound HTTPS to chat-platform endpoints (complicates the C15 egress allow-list and the air-gap story) for marginal benefit beyond what email already covers.
- Slack/Teams/webhooks deferred to v1.x; the architecture leaves the door open via a small `Notifier` interface in the notifications module so additional channels are an extension.

**Q2 — Events that fire notifications (locked):**

| Class | Events | Default recipient(s) |
|---|---|---|
| Job lifecycle | submitted (opt), running (opt), complete (on), failed (on), cancelled (on) | Submitter |
| Approval lifecycle | "needs your approval" / "approved" / "rejected" / "delegated to you" | Approver / submitter / delegate (as relevant) |
| Workflow lifecycle | same shape as job lifecycle, scoped to workflow | Submitter |
| Admin / system | licence expiring, backup failure, cert expiring, audit-chain integrity warning, HPC-cluster circuit-breaker trip | Admins only |
| User account | welcome / first-login link, password reset, MFA reset, account locked | Affected user |

**Q3 — Notification preferences (three layers, all shipped):**
- **Per-event admin defaults** — admin sets defaults at install time (per `[notifications.defaults]` block + admin UI).
- **Per-tool admin overrides** — replaces the legacy single `email_notifications` flag with a per-class toggle; useful to silence chatty snappy calculators while keeping notifications loud for long simulations.
- **Per-user preferences** — each user can opt in/out of notification classes in their account settings, within bounds the admin allows. Some classes (security-sensitive — account locked, MFA reset) cannot be silenced.
- Resolution order at delivery time: per-event admin default → per-tool override (if applicable) → per-user preference. Lowest-allowed wins.

**Q4 — Delivery guarantees:**
- **Email: best-effort** with exponential-backoff retry (3 attempts: ~1m, ~5m, ~30m). After all retries fail, an `email.delivery.failed` audit event records the give-up; the user still sees the in-app notification.
- **In-app:** delivered live to an open NiceGUI session via WebSocket; queued in the DB for users who aren't online and surfaced on next login.
- **DB-stored notifications retention:** last **200** per user by default; configurable per install (`[notifications.in_app_retention] per_user_max = 200`). Older entries age out cleanly — they are **display state**, not audit data, so the "nothing deletable" rule (D20) does not apply. The actual events themselves remain in the audit log forever (per E26).

**Q5 — Email content (locked):**
- HTML + plain-text fallback (multipart/alternative).
- **Plain-English subject lines and bodies** per the end-user UX rule (F30): "Your heat-transfer simulation has finished" not "Job 1234567 state: Q→C".
- **Deep links** into the MATOFX UI built from `PUBLIC_BASE_URL` (per C12). Links carry only opaque ids — never sensitive content (no inputs, no outputs, no PII beyond names where unavoidable).
- **Per-customer branding** via config (logo path + accent colour); minor — defaults are a clean MATOFX-branded template.
- A small templating system (Jinja2 — BSD-3-Clause, already in the stack) renders HTML + plain-text from a single source. Templates per event class, customisable per install.

**Q6 — SMTP configuration (locked):**
- Customer provides their own SMTP server (outbound to customer-controlled per A6/C15).
- Auth: username/password (most common), OAuth2 (e.g. M365 with mailbox-bound OAuth), or no-auth/IP-allow-listed.
- TLS: STARTTLS (default) or implicit TLS; **certificate verification on by default** per E24, customer-supplied CA bundle supported.
- From-address configurable (`matofx@customer.com` typical).
- Credentials live in `SecretProvider` per E25.
- All outbound SMTP calls go through the single shared HTTP/SMTP client wrapper enforcing the egress allow-list — same gate that protects every other outbound integration.

**Q7 — Rate limiting / digest:**
- **One-email-per-event by default.** Simplest, predictable.
- **Per-user opt-in digest mode:** a user can switch any class to "Hourly digest" or "Daily digest" in their preferences. Digest emails group all batched events into a single message with a summary table.
- No global hard rate limits in v1; if a customer reports flooding issues we can add them.
- In-app notifications are not digested — they appear individually with their own timestamps; the user sees the bell badge incrementing.

**Clarifier 1 — In-app retention (200 default, configurable, ages out):**
- Display state, not audit. Source-of-truth events live in the audit log (E26).
- Configurable per install; user can manually mark old entries as read or clear them.

**Clarifier 2 — Bell UI pattern:**
- Standard top-bar **notification bell** with an **unread-count badge**.
- Click opens a dropdown panel with the recent notifications, each linking to the relevant item (job, workflow, scenario, project, admin page).
- "Mark all read" affordance.
- "View all" link to a full notifications page (paginated).

**Clarifier 3 — Recipient scoping:**
- **Admin / system events** (licence, backup, cert, audit, HPC) → **all super-users + admins**, regardless of project access.
- **Job / workflow / approval / scenario events** → only users with **direct involvement**: submitter, designated approver(s), delegated approver(s), scenario / project Owners (when they care about all activity in their project — opt-in via per-user preference).
- **No notifications about content the recipient cannot view.** A user does not learn about a project they have no access to via a notification.
- **User account events** → the affected user only (with super-user audit visibility, naturally).

Implications to carry forward:
- The `Notifier` interface (per Q1) is one of the early framework primitives; both the email and in-app notifiers implement it. Adding a Slack/Teams/webhook notifier later is a drop-in module.
- Templates live in a versioned templates directory (`/etc/matofx/notifications/templates/`); customer-branding overrides are file-based, easy to back up and audit.
- The "lowest-allowed wins" preference resolution is implemented in one function with comprehensive tests — ambiguity in this layer is a frequent source of "why didn't I get an email?" support calls.
- Notification delivery never blocks the action that triggered it. State change writes go to the DB synchronously; notification dispatch is enqueued via Arq and runs asynchronously. A failed notification never fails the underlying action.
- The notification bell respects the F30 end-user UX rule — labels are plain-English, status text avoids jargon, deep links are unambiguous.

### G38. Localisation
**Decision: English-only UI in v1, but i18n-ready from day one (Japanese already a known upcoming need); per-user language/locale + per-user timezone shipped at launch; tool descriptor labels not localised in v1; ISO 8601 timestamps with UTC under the hood; numbers formatted per user's locale; relative time in the UI with absolute timestamp on hover; RTL deferred.**

**Q1 — i18n-ready, English-only ship.**
- Every user-facing string in the codebase goes through a translation function (`_("Submit")`) from day one, even though the catalog only contains English at v1 ship. Adding a language later is dropping in a `.po` / `.mo` catalog with **no code changes**.
- **Library: Babel** (BSD-3-Clause — licence-clean) for translation catalog management + `gettext`-style runtime lookup.
- **Catalog scope:** all UI labels, button text, status messages, error messages, email templates, system/admin messages. Anything a user might read.
- A potential customer is already asking about **Japanese** translation; that drives the i18n-ready requirement and gives us a concrete first non-English target. Translation work itself is out of scope until the customer commits.

**Q2 — Per-user language preference (shipped from day one):**
- Each user picks their preferred language/locale in their account settings (`Account → Preferences → Language`).
- Admin sets a per-install default (in `[i18n] default_locale = "en-US"`).
- Language preference flows through every user-facing surface: UI strings, email subject + body, error messages, system notifications.
- v1 ships with one option (`en-US`) in the dropdown; the dropdown grows as catalogs are added.
- Storage format: a single locale identifier (e.g. `en-US`, `en-GB`, `ja-JP`) following BCP 47 / POSIX conventions.

**Q3 — Tool descriptor labels: English-only in v1 (option a).**
- Tool authors write descriptors with English labels and help text; those labels are **not** translated by MATOFX.
- Future option: a `i18n` block in the descriptor with per-locale overrides (`"i18n": { "ja-JP": { "label": "送信" } }`); not implemented in v1.
- Tool authors can localise tool docs separately at the customer's discretion (out of MATOFX's scope).

**Q4 — Dates, numbers, timezones:**

- **Canonical timestamp format on the wire and in audit logs: `yyyy-mm-dd hh:mm:ss UTC`** (ISO 8601 with seconds, UTC always). Stored in DB as UTC. Audit log entries use this format unconditionally — never localised, never timezone-shifted.
- **User-facing display:**
  - **Relative time in the UI** ("5 minutes ago", "yesterday at 14:30", "Mar 12, 2026") — friendly, scannable.
  - **Absolute timestamp on hover** — full `yyyy-mm-dd hh:mm:ss` rendered in the **user's preferred timezone** with the timezone clearly labelled (e.g. `2026-05-02 14:30:11 JST` or `2026-05-02 05:30:11 UTC`).
  - The hover pattern follows the GitHub-style approach (clarifier 3 confirmed).
- **Numbers: formatted per the user's locale** — `1,234.56` (en-US), `1.234,56` (de-DE), `1 234,56` (fr-FR), etc. Babel handles this. Tool input/output values are stored as raw numbers and rendered through the locale formatter at display time.
  - **Caveat for engineering data:** units (e.g. `W/m·K`, °C, kPa) are not localised — they're SI / engineering conventions. Number-only formatting is locale-aware; unit strings are not.
- **Per-user timezone preference (shipped from day one):**
  - Each user picks their timezone in their account settings; admin sets the install default.
  - Storage: IANA tz identifier (`Asia/Tokyo`, `Europe/London`, etc.).
  - All UI rendering of historical timestamps respects the user's timezone; emails include the user's timezone where they show timestamps.

**Q5 — Right-to-left support: deferred.**
- RTL layouts (Hebrew, Arabic, Persian, Urdu) not supported in v1.
- Quasar (the NiceGUI component library) does support RTL, so it's an extension rather than a re-architecture if a customer drives it.
- v1 layout decisions can stay LTR-only; we don't add `dir`-awareness work in v1.

**Implementation specifics — locked from day one:**

- **Babel** runtime in the codebase; `_()` wraps every UI string from the first commit.
- A single `i18n.py` module loads the user's locale on each request and exposes `_()`, `format_number(n)`, `format_datetime(dt, *, relative=True)`, `format_currency(n, code)` etc.
- CI lint: any `print` / `st.write` / `ui.label` containing a literal English string outside the `_()` wrapper fails the build (with allow-list for log lines, debug, etc.).
- Translation extraction is a `pybabel extract` step in the build pipeline; the resulting `.pot` template ships with the source so future translators have a clean starting point.
- The audit log is **never** localised — its strings (action verbs, target kinds, etc.) are stable English identifiers regardless of who's reading. The audit-log *viewer* in the UI translates them for display.

**Configurable in `matofx.toml`:**
```
[i18n]
default_locale = "en-GB"        # UK English is the default
default_timezone = "UTC"
available_locales = ["en-GB", "en-US"]   # grows as catalogs are added (Japanese already on the roadmap)
```

**Default locale: `en-GB` (UK English)**, with `en-US` available as an option. Most user-facing copy is written in UK English (colour, behaviour, organisation, optimise, etc.); the `en-US` catalog is a thin override layer over the `en-GB` canonical strings. Other languages (starting with Japanese) are added as full catalogs.

Implications to carry forward:
- Adding Japanese (or any future language) is a "ship a `.mo` file + add to `available_locales`" exercise, not an engineering project. The first non-English language drop is a small, well-bounded test of the i18n-ready discipline; plan to do it deliberately as a v1.x milestone.
- Email templates use Jinja2 `_()` calls plus the user's locale loaded from their preferences when the email is composed — not the install's default.
- The user-preference layer (`account → preferences`) is where `language`, `timezone`, and `notification preferences` (per G37) live. One settings page, three sets of fields.
- A single `current_user_locale()` / `current_user_timezone()` pair of dependency-injection helpers keeps every render path on the right footing without boilerplate.

### G39. Theming / per-customer branding (incl. multiple landing dashboards)
**Decision: full theme customisation (logo, accent colour, fonts, layout tweaks, favicon, welcome/footer text, email branding); light/dark mode with admin default + per-user override; co-branded — MATOFX SPDM and Funis Consulting Ltd appear somewhere; logo placement defaulted to top-bar + footer with customer override; accent colour drives Quasar primary; email branding inline (no CDN). Plus: multiple landing dashboards admin-defined, assigned per user, switchable only by super-users/admins.**

**Q1 — Branding scope: full theme (option c).**
- **Logo** (top bar + email).
- **Accent colour** (drives Quasar `primary` colour — buttons, active nav, highlights).
- **Secondary / status colours** (success / warning / error / info) — optional override; sensible defaults if untouched.
- **Fonts** — optional; one or two web-safe / system fonts the customer wants. Self-hosted only (per air-gap and "no CDN" rules from C15).
- **Favicon** — optional file path.
- **Welcome / login-page copy** — optional plain-text or basic-Markdown strings.
- **Footer text** — optional. Default footer always carries the co-branding line (see Q4).
- **Email template overrides** — optional, per-template files in `/etc/matofx/notifications/templates/` (per G37).
- **Layout tweaks** — limited and well-bounded: top-bar height, sidebar width, dashboard density (compact / normal / comfortable). Not arbitrary CSS — keeps the UI maintainable.

**Q2 — Branding assets location (locked):**
```
[branding]
logo_path = "/etc/matofx/branding/logo.png"               # top-bar logo
logo_email_path = "/etc/matofx/branding/logo-email.png"   # email-friendly variant (smaller, higher DPI)
favicon_path = "/etc/matofx/branding/favicon.ico"         # optional
accent_color = "#40A1A4"                                  # Quasar primary
welcome_message = "Welcome to ACME Engineering Hub."      # optional plain-text/Markdown
footer_extra = "Internal use only."                       # optional, appended to the co-branding line
fonts = ["/etc/matofx/branding/fonts/AcmeSans.woff2"]     # optional, self-hosted
```
- All asset files are bundled into backups (per C14) so a restore brings the customer's branding back automatically.
- Asset changes hot-reloadable via `matofx config reload` (per E25).

**Q3 — Light / dark mode: admin default + per-user override (option c).**
- Admin sets the install default (`[ui] default_theme = "light"`).
- Each user can override in their account settings (`Account → Preferences → Theme: System / Light / Dark`).
- "System" follows the user's OS preference via the standard `prefers-color-scheme` media query.
- The accent colour from `[branding]` works in both modes (Quasar handles the dark-mode adjusted shading).

**Q4 — Co-branding (locked):**
- The product is **co-branded**: the customer's branding (logo, accent colour) is featured prominently, AND the **MATOFX SPDM** product name and **Funis Consulting Ltd** company name appear somewhere visible on every page.
- Default placement: in the page footer (e.g. "Powered by MATOFX SPDM™ — © Funis Consulting Ltd").
- Customers cannot fully white-label in v1 (i.e. cannot remove MATOFX / Funis Consulting attribution). White-labelling is a commercial decision deferred to v1.x or beyond.

**Clarifier 1 — Logo placement (configurable):**
- **Default:** customer logo in top-bar + co-branding line in footer.
- **Configurable:** `[branding.layout]` lets customers move things around within the bounded options:
  ```
  [branding.layout]
  customer_logo_position = "top_bar"       # | "top_bar" | "sidebar" | "both"
  cobrand_position = "footer"               # | "footer" | "topbar_right"
  ```
- Cannot remove the co-brand entirely (per Q4).

**Clarifier 2 — Accent colour usage (locked):** primary buttons, active nav items, key highlights — all driven by Quasar's `$primary` SCSS variable, set at runtime from `[branding] accent_color`. Confirmed.

**Clarifier 3 — Email branding (locked):** logo embedded inline (CID-attached or base64-data-URI — never an external URL fetch); accent colour applied to email template headings; plain-text fallback contains no images. Air-gapped customers can read mail without anything trying to phone home.

---

**New feature folded in: multiple landing dashboards.**

> **An install can have more than one landing dashboard configuration. Regular users see exactly one (admin-assigned) with no switcher; super-users and admins may switch between any of the available dashboards from a top-bar selector.**

**Concept:**
- A **landing dashboard** is an admin-defined arrangement of widgets shown on a user's home page after login: e.g. recent jobs, my approvals, projects I own, system health, licence status, recent comparisons, quick-launch tools, etc.
- Different audiences need different views. An FEA engineer wants their jobs queue front-and-centre; a project manager wants a portfolio summary; an auditor wants approval activity; a system admin wants system-health.
- Rather than baking one home page per role, MATOFX lets the admin **define multiple landing-dashboard configurations** in the admin space and **assign one per user** (defaulting per role).

**Behaviour:**
- **Regular users:** see only their assigned landing dashboard. No switcher in the top bar. Their assigned dashboard can be changed by an admin (audit-logged).
- **Super-users / admins:** see a **dashboard switcher** in the top bar (compact dropdown) listing all available landing-dashboard configurations; can switch ad-hoc to any of them.
- All available landing dashboards in the install are listed in `Admin → Dashboards`; admin creates / edits / deactivates configurations there.

**Schema sketch:**
```
landing_dashboards
  id, name, description, layout_json, kind ("system" | "custom"),
  state ("active" | "deactivated"), creator_id, created_at, updated_at

users
  ... existing fields ...
  landing_dashboard_id  (FK, nullable — when null, fall back to the role-default)

roles
  ... existing fields ...
  default_landing_dashboard_id  (FK, nullable — install-wide fallback)
```

**Widgets:**
- Built from a **fixed catalogue of widget types** (e.g. `recent_activity`, `my_jobs`, `my_approvals`, `projects_i_own`, `licence_health`, `system_health`, `quick_launch`, `audit_summary`, `recent_comparisons`).
- The admin designs a layout by picking widgets from the catalogue and arranging them into a grid (with sensible defaults).
- Each widget respects the current user's ACL — never shows content the user can't see, regardless of which dashboard they're assigned.
- v1 ships with a **starter set of widgets** matching the most common needs; new widgets are added in a future release as the product matures. Tool authors do not write widgets in v1.

**Default dashboards shipped out-of-the-box:**
- `Engineer` — `my_jobs`, `recent_activity`, `quick_launch`, `my_approvals`.
- `Admin` — `system_health`, `licence_health`, `audit_summary`, `recent_activity`.
- `Project lead` — `projects_i_own`, `recent_comparisons`, `my_approvals`, `recent_activity`.
- `Auditor` — `audit_summary`, `recent_activity`, `licence_health`.

The admin can edit, clone, or add to these. They are `kind = "system"` records — protected from deletion (per D20) but always editable.

**Permissions / feature-flag:**
- Multi-dashboard support is per-customer toggleable (per E27 project-wide rule). When the feature flag is off, every user sees a single fixed dashboard (the install's `default_landing_dashboard_id` from the relevant role) with no switcher anywhere — even for admins.

Implications to carry forward:
- The widget catalogue is part of the early build; widget authoring beyond the built-in set is a future SDK surface, not a v1 deliverable.
- Layouts are stored as small JSON documents (`layout_json`); the layout-editor UX is admin-side, with a drag-and-drop grid (NiceGUI's Quasar grid components).
- Every widget enforces ACL and licence-status checks (read-only mode in expiry grace per C16) — landing dashboards must not bypass any of the standard authorisation paths.
- The dashboard switcher is a small top-bar dropdown; appears for super-users / admins only when more than one landing dashboard is active.
- The "starter" landing dashboards must be designed by someone who knows the user roles intimately — design pass with real customer / engineer feedback before locking the v1 widget set.

---

### G39a. Design-space exploration — parametric studies (added 2026-05-16 via ADR-0001, refined same day)
**Decision: ship a first-class Parametric Study in v1.0 (must-ship). It is a child of Project (parallel to Scenario), fans one tool over a generated input matrix, and has its own results-exploration surface. v1 includes all sampling + classical RSM designs and a minimal surrogate fit. D-optimal designs and study-extension are fast-follow; closed-loop optimisation, surrogate beyond the minimal fit, and variance-based sensitivity are v1.x. Full design in `designs/parametric-studies.md`; rationale in `decisions/0001-parametric-studies.md`.**

This closes the single largest SPDM-domain gap from the 2026-05-16 review: no design-space-exploration feature, the Compare sensitivity/parametric chart (§G36 Q5) had no producer, and the legacy app had bolted on an ad-hoc `_repeat` tool for the same need.

- **Object model.** `ParametricStudy` is a **child of Project, parallel to Scenario** (`Project → { Scenario → Job/Workflow } | { ParametricStudy → Job }`). It inherits the project ACL exactly as a scenario does (audit, no-delete, draft-reaping all apply at study granularity). Child jobs reference it via `Job.parametric_study_id` and carry `scenario_id = NULL`, which **sidesteps the `one-tool-per-scenario` UNIQUE constraint entirely** (NULL `scenario_id` rows are distinct in SQLite + Postgres by default — no constraint-scoping code needed). A portable CHECK keeps every job homed to exactly one of scenario / study. A study is single-shot (no definition/run split — unlike Workflow); "clone-and-refine" makes a new study, preserving D20 immutability. **Study children are viewed from the Parametric Studies surface, never from My Jobs** (a study can have hundreds of children — deliberate asymmetry vs workflow children).
- **Sampling — all modes in v1.** **run table** (user-authored matrix of exact named configurations); explicit value list; range/grid (full-factorial, with per-factor levels + 5 spacing methods — linear/log/edges/center, defined as position-warps so no min>0 constraint); fractional-factorial + Plackett–Burman screening; LHS / Sobol / random; Central-Composite + Box–Behnken (RSM, offered only when all varied factors are continuous-numeric). The mode selector is pre-filtered by the chosen tool's descriptor and re-validated at submit; Preview shows the full run matrix. Configurable hard run cap (default 500; per-tool override via the Edit Tool Metadata tab, admin/SU only) + mandatory plain-English confirmation per F30. D-optimal and study-extension are fast-follow; closed-loop optimisation / surrogate-beyond-minimal / Sobol-variance sensitivity are v1.x.
- **Execution & fairness.** Children enqueue on existing Arq queues with the tool's existing profile; runner unchanged. All children pinned to the tool version (descriptor frozen) captured at study creation. Generated points validated against descriptor `cross_field_constraints` before enqueue — invalid points dropped and counted for the user. Per-study concurrency cap + lower-priority study lane so a big study can't starve interactive users. States `Complete / Partial / Failed / Cancelled`; cancel cascades; failed children retryable.
- **Results.** A **durable per-child results store** is written at child completion (from the runner's `outputs.json` parse, *before* cleanup) so a finished study is always investigable regardless of file retention/reaping; it persists all numeric+string scalars; failed/cancelled children are stored null-with-banner, never dropped. The response axis is chosen at view time: a named numeric scalar **or** a column of a descriptor-declared `csv_plot` (CSV/table) output reduced by an aggregate. Dedicated Study Results surface (ad-hoc Compare's 2–10 cap unchanged), rendered with Plotly (already in stack — no new chart dep): 2D scatter/line, 3D scatter/surface, parallel-coordinates (Plotly's trace, not HiPlot), and per-child **`csv_plot` curve-overlay** across the sweep (legacy sensitivity-chart / `_repeat` parity). Sensitivity v1 = correlation + standardized regression + tornado; Sobol-variance is fast-follow (needs Saltelli sampling). Minimal surrogate in v1: scikit-learn GP/polynomial, cross-val R², predict-at-point, fitted-surface overlay (no optimisation-on-surrogate / export-as-tool / multi-output). Progressive partial-data exploration with statistical-safety warnings. CSV/PDF via the Compare report machinery; ACL-filtered. Delivery split **2-PS-b0** (store + Results table + response picker + export + drill-down) → **2-PS-b** (plots + overlay + sensitivity) → **2-PS-c** (surrogate).
- **Approvals.** Approval-gated tool → one study-level gate (pre reviews the plan, post the aggregate); never per-child; reject → frozen, clone-new-study to retry. `data_hash` covers the canonical plan/results.
- **Permissions / licence / flag.** New `parametric_studies.create` verb, default-on for `Regular user`, gated on the user being authorised to run the tool (anti-escalation, §G35 Q2). Audited (§E26); blocked in licence read-only (§C16); behind `features.parametric_studies`.

Implications to carry forward:
- One Alembic migration: `parametric_study` table, `Job.parametric_study_id` FK, `Job.scenario_id` nullable, the home-exactly-one CHECK. No constraint-scoping code. Cumulative + idempotent per C13.
- New deps, all permissive + air-gap-bundled, no Node: `pyDOE3` (BSD-3) + `scikit-learn` (BSD-3); `scipy.stats.qmc` + `plotly` already in stack; `SALib` not pulled (Sobol-variance is fast-follow); HiPlot rejected.
- Reuses the runner, the Compare engine, the audit chain, and the single-gate approval model — net new surface is small relative to product impact.
- `ARCHITECTURE.md` §16 gains the NULL-scenario / `parametric_study_id` note when built, not before. Revisit D-optimal / extend / optimisation weighting once the first pilot's sweep sizes are known.
- **2026-05-19 amendment (ADR-0001 rev + ADR-0003):** adds migration `0039` (`StudyMode += table`) on the shipped 2-PS-a head and a `parametric_study_result` store migration with 2-PS-b0 (`factor_spec`/`sampling_params` shape additions are JSON-internal). Two **cross-cutting platform changes** (recorded in `decisions/0003-job-io-document-retention.md`, not study-scoped): a **`csv_plot` declared output-file kind** in the tool-descriptor / `OutputsDocument` contract (also touches the H41 descriptor meta-schema lint); and an **unconditional retention floor** — every job retains `inputs/inputs.json` + `outputs/outputs.json` + any declared `csv_plot` file, superseding the non-`retain_files` default and `delete_io_files`. Both touch shipped runner cleanup across *all* jobs — regression-tested platform-wide.

---

## H. Quality, testing, observability

### H40. Test bar
**Decision: pragmatic coverage with an explicit list of must-be-exhaustively-tested modules; six test types (unit / integration / E2E / property / security / performance); Playwright for E2E; shipped synthetic-data generator; full CI gate (tests + lints + migration round-trip + backup/restore + tool-image build + SCA + code review); release-checklist and upgrade rehearsal as pre-release rituals; documented performance commitments; mutation testing on critical paths; Locust for load testing; CI matrix runs on Linux and Windows.**

**Q1 — Coverage (option a, pragmatic with an exhaustive-test list).**

The following modules are **must-be-exhaustively-tested** — high-quality unit + integration + property + security tests, with branch coverage reviewed in PRs:
- **Auth** — local login, SSO claim handling (SAML + OIDC), MFA enrolment / verification / recovery / admin reset, session management, idle/hard timeouts, rate-limit + lockout / break-glass exponential-delay.
- **Permissions / ACL** — `can(user, verb, object)`, project ACL, scenario/job inheritance, team membership effects (where applicable for filtering), role-feature-matrix evaluation, deny-by-default semantics.
- **Tool sandbox** — resource limit enforcement (CPU / memory / wall-clock / disk advisory), no-network-by-default, no-cross-job-access, GPU passthrough, cancellation propagation, multi-GPU atomic allocation.
- **Approval signing chain + audit-log signing chain** — chain integrity, signature verification, key-rotation continuity, target-label denormalisation, the "no self-approval" three-layer defence.
- **Licence verification** — Ed25519 signature checks, expiry / grace / read-only transitions, soft machine-id binding, CSL slot allocation, FAL access-rights assignment.
- **Data layer** — transactions, FK constraints, schema-migration round-trips on both SQLite and Postgres, JSON-encoded list serialisation, repository-API contract.
- **File staging / CAS** — reference counting, atomic upload of identical-hash files, draft-reaper coordination with CAS-orphan cleanup, working-dir cleanup against the orchestrator `retain` list.
- **Backup + restore** — full round-trip on synthetic install, manifest signing/verification, encrypted backup round-trip with passphrase rotation.

For everything else: meaningful happy-path + key-edge-case tests; no line-coverage gate; PRs that obviously skip testing are rejected at review.

**Q2 — Six test types (locked):**
- **Unit tests** — fast, isolated, run on every save in dev and on every push. Pytest (MIT — licence-clean) as the runner.
- **Integration tests** — DB + Valkey + worker; real transactions; in-CI with disposable containers.
- **End-to-end tests** — full stack incl. UI; **Playwright** (Apache-2.0).
- **Property-based tests** — Hypothesis (BSD-3-Clause); for signing chain, ACL evaluator, schema validator, allocator atomic operations.
- **Security tests** — authn bypass attempts, IDOR (insecure direct object references), CSRF, log injection, secret-leakage canary (per E25), descriptor-injection attempts in tool installs.
- **Performance tests** — baseline benchmarks for the critical paths (Q7); CI nightly.

Merge-gating: unit + integration + security pass on every PR. E2E + property + performance run on nightly / pre-release schedules.

**Q3 — Playwright for E2E (locked).**
- Apache-2.0; Python bindings; headless-friendly; cross-browser if ever needed.
- Avoid Selenium for the v1 surface.

**Q4 — Shipped synthetic-data generator (locked).**
- `matofx test seed-fixtures [--scale=small|medium|large]` populates a fixture install with realistic-shape data: a handful of users with different roles, a few teams, several projects with scenarios and jobs at various states, sample tool installations, sample workflow definitions (released + custom), realistic audit log entries.
- Useful for: dev-local manual UI exploration, automated E2E runs, performance benchmark warm-up, customer demos.
- The generator is deterministic with seed input — same seed → identical fixtures.

**Q5 — CI gating (locked):**

| Check | Blocks merge? |
|---|---|
| Unit + integration + security tests pass on Linux **and** Windows | yes |
| CI lints pass — import discipline, secret-leakage canary, no-external-URL canary, `_()`-wrap canary, no-`os.environ` outside `matofx.config.secrets`, descriptor meta-schema, feature-flag-catalog completeness | yes |
| Migration round-trip: fresh DB → all migrations applied → seed → tests pass — on **both SQLite and Postgres** | yes |
| Backup → restore round-trip on a fixture install | yes |
| Tool-image build smoke test (at least one synthesised image) | yes |
| `pip-audit` (or similar SCA) reports no new high-severity advisories | yes |
| Code-review approval from ≥1 other reviewer | yes |
| E2E (Playwright) | nightly / pre-release |
| Property-based + performance benchmarks | nightly / pre-release |
| **Mutation testing** on the must-be-exhaustively-tested modules (clarifier 2) | nightly |

**Q6 — Pre-release rituals (locked):**
- **Documented release-checklist** the release engineer walks through before tagging:
  - TLS round-trip (cert install, browser trust check).
  - SSO flow against a test IdP (both SAML and OIDC).
  - HPC submission to a test cluster (Slurm / Rescale / HPC Pack — at least one each release).
  - Backup → restore on a real VM (not just CI fixtures).
  - Fresh-install smoke test on Linux and Windows hosts.
  - Performance benchmark vs the documented commitments (Q7).
  - Licence-flow walkthrough (install → grace → read-only → renewal).
- **Upgrade rehearsal:** install previous release → run sample workload to populate state → apply upgrade → confirm functionality + data preservation → document any quirks for release notes.

**Q7 — Performance commitments (locked):**

| Path | Target |
|---|---|
| Login (post-MFA) | < 500 ms p95 |
| Form render (medium tool, ~30 fields) | < 800 ms p95 |
| Job submit (input validation + queue) | < 300 ms p95 |
| Snappy tool execution (warm container, sub-second tool) | < 1.5 s p95 end-to-end |
| Audit-log write (synchronous, state-changing) | < 5 ms p95 |
| Audit-log read (per-session-per-record, async-batched) | < 0.5 ms p99 added to request |
| Backup of 10 GB job-storage tree | < 5 min initial / < 1 min incremental (restic) |
| Container warm-pool launch latency | low tens of ms p95 |

Tracked by Locust + custom microbenchmarks; failures alert the release engineer.

**Clarifier 1 — CI runner OS coverage: Linux + Windows.**
- Both gate the merge.
- Mac is a nice-to-have for individual developers' machines; **not** part of the CI matrix and not officially supported as a build/test environment.

**Clarifier 2 — Mutation testing on critical paths (yes):**
- Use `mutmut` or `cosmic-ray` (both MIT — licence-clean).
- Limited to the must-be-exhaustively-tested module list (Q1) — running mutation on the entire codebase is prohibitively slow.
- Nightly schedule, not per-PR. A drop in mutation score below a threshold (configurable, e.g. 80%) on the critical modules raises an alert.

**Clarifier 3 — Locust for load testing (locked).**
- MIT licence; Python-native; produces actionable plots.
- Scenarios scripted alongside the codebase; rerun against staging / production-like environments before each release.

Implications to carry forward:
- Test infrastructure (Pytest config, fixture generators, Playwright harness, Locust scenarios, mutation runners) is part of the v1 codebase from week one — built alongside the first features, not bolted on later.
- The CI matrix is meaningful (4–6 jobs per PR: SQLite × Linux, Postgres × Linux, SQLite × Windows, Postgres × Windows, plus lint and SCA jobs). Test runtime budget per PR target: **under 15 minutes** to keep iteration fast.
- Performance commitments are documented in the customer-facing Security & Compliance Posture doc (per E22) so customers know what to expect; missing them in the field gives customers a way to tell us "this isn't right" with concrete numbers.
- The "must-be-exhaustively-tested" list is reviewed at every minor release — modules grow into it as the system matures.

### H41. CI/CD
**Decision: GitHub Actions for source + workflows; self-hosted Funis runner for release builds (YubiKey signing); trunk + maintenance branches release model; signed release tarball with full artefact bundle (containers, installers, base images, HIBP list, restic, templates, notes, checksums); SBOM in CycloneDX/SPDX format; SLSA Level 2 build provenance; <15-minute per-PR test budget; deployment never automated — releases delivered manually per C15.**

**Q1 — CI/CD platform: GitHub Actions.**
- Source repo + workflows on **GitHub** (private repo, Funis org).
- **GitHub-hosted runners** for PR / test workflows (lint, unit, integration, security tests, SBOM diff). No special hardware needed there; standard ubuntu-latest + windows-latest matrix.
- **Self-hosted Funis-controlled runner** for the release-build workflow that produces signed tarballs. The YubiKey carrying the Ed25519 private key (per C16) is physically attached to that runner; signing requires the release engineer's PIN entry at sign time.
- Branch-protection rules on `main` and any `vN.x` maintenance branch: require PR + green CI + reviewer approval before merge.

**Q2 — Branching / release model: trunk + maintenance branches (option c).**
- `main` is always shippable.
- Release tag (`v1.0.0`, `v1.1.0`, etc.) cuts the release; from that tag a `v1.x` maintenance branch is created lazily when (and only when) a security patch is needed for an already-shipped version.
- Patch releases (`v1.0.1`) ship from the relevant `vN.x` branch and are also cherry-picked into `main` when applicable.
- v1.1 ships from `main` when ready (per C13's 1–2 releases per year cadence).
- Long-lived feature branches discouraged; squash-merge by default; commit history on `main` is linear and readable.

**Q3 — Release artefact production and signing (locked):**

The signed release tarball contains:

| Bundle component | Notes |
|---|---|
| OCI container images (saved) | Per B10's primary container path; web, workers, Valkey, etc. |
| Linux installer artefacts (`.deb` / `.rpm` / `.tar.gz`) | Per B10's secondary native-installer path |
| Compose templates + sample `matofx.toml` | First-install starter |
| Default `features.toml` (catalog) | Per E27 |
| Curated tool base images | Per F28 |
| Bundled HIBP password list | Per D19 |
| `restic` binary (BSD-2) | Per C14 |
| Default email templates, branding placeholders | Per G37 / G39 |
| `pysaml2` + `authlib` + all Python dependencies, vendored | Per C15 — air-gapped install |
| Release notes (Markdown) | Operator-facing |
| `MANIFEST.json` + SHA-256 checksums | Integrity verification |
| `provenance.intoto.jsonl` | SLSA L2 attestation, per Q5 |
| `sbom.cdx.json` | CycloneDX SBOM, per Q4 |
| `release.sig` (Ed25519 detached signature) | YubiKey-signed by Funis |

**Signing flow:**
1. Self-hosted runner builds the artefact bundle from a signed Git commit.
2. Runner halts and prompts the release engineer for the YubiKey PIN.
3. Ed25519 signature produced on-device; `release.sig` written alongside the tarball.
4. Output uploaded to a Funis-controlled secure store (not public).
5. **Per-customer delivery** (per C15) is a separate manual step — Funis takes the standard signed tarball + the per-customer signed licence file and ships them to the customer through the agreed private channel.

**Q4 — SBOM (locked):**
- Generated as **CycloneDX JSON** (the more widely-supported format in 2026), with **SPDX** as a future addition if a customer's auditor demands it.
- Lists every Python dependency, version, licence, plus the curated base image's OS packages and their licences.
- Built by `cyclonedx-bom` (Apache-2.0 — licence-clean) integrated into the build workflow.
- Ships inside the release tarball; also included in any customer security questionnaire response.
- Supports the no-paid-licence rule (per the project memory) — every dependency on the SBOM must satisfy the permissive-licence policy. CI lint compares the SBOM's licences against an allow-list and fails the build if anything restrictive sneaks in.

**Q5 — Provenance / attestation: SLSA Level 2 (locked).**
- The trusted build platform (the self-hosted Funis runner) generates the attestation as part of the build.
- Provenance file (`provenance.intoto.jsonl`) declares: source repo + commit hash, build runner identity, build start/end timestamps, dependency hashes, the resulting artefact hash.
- Signed with the same YubiKey at the same moment as the artefact.
- SLSA Level 3 (hermetic, fully-isolated build) deferred — the operational lift isn't justified for v1.

**Q6 — Per-PR / per-commit overhead (locked):**
- **Per-PR target: < 15 minutes total** for the gating workflows.
- Per-PR running:
  - Lint (formatting, import discipline, type checks via mypy, ruff, our custom lints — secret-leak canary, no-external-URL canary, `_()`-wrap, no-`os.environ` outside secrets module, descriptor meta-schema, feature-flag-catalog completeness).
  - Unit + integration + security tests on Linux + Windows × SQLite + Postgres (4-cell matrix).
  - Migration round-trip on both engines.
  - Backup → restore round-trip (small fixture).
  - Tool-image build smoke test.
  - SCA via `pip-audit` (Apache-2.0).
  - SBOM diff vs `main`.
- Nightly:
  - Playwright E2E.
  - Property-based tests (Hypothesis).
  - Performance benchmarks (Locust).
  - Mutation testing (mutmut / cosmic-ray) on the must-be-exhaustively-tested module list.

**Q7 — PR feedback (locked):**
- Test results, coverage delta, lint findings, SBOM diff, and migration round-trip output posted as a comment on the PR by the workflow bot.
- Failures link to the relevant CI run.
- A small dashboard (internal-only, on a Funis-controlled host) aggregates trends — flaky tests, perf regressions, dependency churn — for retrospective / planning use.

**Clarifier 1 — Source hosting: GitHub.**

**Clarifier 2 — Self-hosted runner: Funis-controlled hardware (office or VM you own).**
- Either works; the YubiKey just needs to be physically attached to it.
- The runner is **only used for release builds** — PR test runs go to GitHub-hosted runners. This minimises the YubiKey-attached machine's exposure surface.
- Runner OS: Linux (Ubuntu LTS or similar) for ergonomics, even though MATOFX targets both Linux and Windows for the *product*. Cross-platform builds happen via Docker / cross-build steps inside the workflow, not by running the workflow on a Windows host.

**Clarifier 3 — Deployment never automated.**
- CI **produces** the signed release artefact; humans then deliver it to customers via the private per-customer channel (per C15).
- **No automated deployment to customer environments**, ever — including no auto-deploy to "Funis's own staging" outside of dev environments.
- A small dev-only "staging" deployment exists on a Funis-controlled VM for E2E + manual exploration; it gets deployed by `matofx upgrade apply` like any other install, run by a developer manually. Not part of CD.
- This rule is part of the trust posture: **only humans deliver MATOFX to customers**. Aligns with C15's air-gapped, per-customer, signed-channel delivery model.

Implications to carry forward:
- Self-hosted runner setup is a first-week-of-build task: provision the machine, harden it, install the YubiKey toolkit (`ykman`, `pkcs11`-compatible OpenSSL), test signing end-to-end with a throwaway key before generating the production keypair.
- The "minimal exposure" of the YubiKey-attached runner is a real security commitment — that runner runs only the release-build workflow, has limited inbound network, gets updated promptly, and is monitored for unusual access. (A small internal SOP captures this.)
- Per-customer delivery tooling (separate from CI) is a small Funis-side workflow: take the standard signed tarball, the customer's signed licence file, the customer's identifier, package them, hand off to the agreed channel. Manual to start; can be scripted (still YubiKey-attended for licence signing) once delivery cadence justifies it.
- The SBOM-licence-check lint and the "vendored deps" requirement together enforce the no-restrictive-licence rule (project memory) at build time.

### H42. Observability stack
**Decision: structured JSON-Lines diagnostic logs (separate from audit log); Prometheus-compatible metrics at `/metrics` with auth token off by default; OpenTelemetry instrumentation off by default with a one-line opt-in; three-tier health endpoints (`live`/`ready`/`deep`); in-MATOFX error log + admin "Errors" view (no third-party error tracker); admin "System health" dashboard page; per-request HTTP logging at INFO with extra context on errors at WARN.**

**Q1 — In-scope observability surfaces (locked):**
- **Diagnostic logs** (web + workers; separate from audit log per E26).
- **Metrics** (Prometheus-compatible).
- **Traces** (OpenTelemetry; off by default).
- **Health checks** (`/health/live`, `/health/ready`, `/health/deep`).

All four ship with sensible defaults; customers can plug their own stacks in via standard formats.

**Q2 — Logging (locked):**
- **Structured JSON-Lines** — every log line is a JSON object: `timestamp_utc`, `level`, `logger`, `message`, `request_id`, `user_id` (where known), `session_id` (where known), plus per-call structured fields (`job_id`, `tool_alias`, `tool_version`, etc.).
- **Output:** stdout/stderr by default (captured by container runtime / systemd journal); optional file output (`/var/log/matofx/<process>.jsonl`) for native installs.
- **Default log levels (clarifier 1):**
  - Application loggers: `INFO`.
  - Library noise (`uvicorn.access`, `sqlalchemy.engine`, `aiosqlite`, `arq`, `httpx`, `fastapi`): `WARNING`.
  - `DEBUG` is admin-opt-in (`[logging.levels]` block in `matofx.toml`); `matofx doctor` warns when it's enabled in production.
  - `matofx.audit` logger: fixed (the audit log uses its own subsystem per E26 — these "logs" are unrelated).
- **No secrets, no PII beyond user ids, no request bodies, no audit-log content** (the secret-leakage canary test from E25 enforces this in CI).
- **Retention** (per E27): default 90 days / 1 GB rolling; customer-configurable.
- **Per-request log line at INFO** (clarifier 3): one line per HTTP request with `method`, `path`, `status`, `duration_ms`, `user_id`, `session_id`, `route_name`. Errors get an additional WARN line with the exception summary + stack trace reference (full trace in the structured field, not in the message string).

**Q3 — Metrics (locked):**
- **Prometheus-compatible `/metrics` endpoint** via `prometheus-client` (Apache-2.0 — licence-clean).
- **Authentication: off by default** (clarifier 2). Customers who deploy MATOFX behind a private network / VPN expect to scrape `/metrics` from internal monitoring without a token. Turning auth on is a single config flag for customers whose policy requires it:
  ```
  [observability.metrics]
  endpoint_enabled = true
  require_token = false              # default — relying on network boundary
  token_env = "MATOFX_METRICS_TOKEN" # used when require_token = true
  ```
  Trade-off acknowledged: metrics endpoints contain operational metadata (queue depths, login attempt counters, error rates), not secrets — leaking it is operationally embarrassing but not a confidentiality breach.
- **Standard metric set:**
  - HTTP request count + latency histograms by route/status.
  - Active sessions, login attempts (success/failure), MFA outcomes.
  - Arq queue depth per queue (`inline`, `cpu-fast`, `cpu-long`, `gpu`, `hpc-poll`, `cron`), worker count, jobs running, job duration histograms.
  - Job state-transition counters by `(tool_alias, from_state, to_state)`.
  - DB connection-pool usage, query duration, error rate (per engine type).
  - Valkey latency, command rate.
  - Outbound integrations (SMTP / IdP / HPC / remote tool server): success / failure / latency, per backend identity.
  - Licence state — gauge of licence type, slots used vs available, days-until-expiry.
  - Cleanup events: drafts reaped, working dirs cleaned, CAS orphans deleted (counters with reason labels).
  - GPU allocator: GPUs allocated / available, multi-GPU allocation latency.
- All metric names follow the `matofx_<subsystem>_<unit>` convention (Prometheus-style).

**Q4 — Tracing (locked):**
- **OpenTelemetry** (Apache-2.0 — licence-clean) auto-instrumentation for FastAPI, SQLAlchemy, httpx, Arq, Valkey clients.
- **OTLP/HTTP exporter** (the most-broadly-supported modern protocol).
- **Off by default**; flip on with config:
  ```
  [observability.tracing]
  enabled = false
  exporter = "otlp_http"
  endpoint = "https://otel-collector.customer.local:4318"
  sample_rate = 0.1   # head-based, 10%
  ```
- When enabled, span attributes follow OTel semantic conventions; no secrets / PII attached.
- Helps Funis support diagnose a "slow form-render" or "stuck job" complaint without grepping logs.

**Q5 — Health checks (locked):**

| Endpoint | Frequency target | Checks |
|---|---|---|
| `GET /health/live` | every few seconds | process responding |
| `GET /health/ready` | every 10–30 s | live + DB reachable + Valkey reachable + licence in valid/grace state |
| `GET /health/deep` | every minute or two | ready + worker heartbeat received recently + HPC backends pingable + last backup ran successfully + audit-chain integrity OK |

- **No authentication** on any of the three — reverse proxies / load balancers / orchestrators / on-call monitoring need to hit them without credentials.
- **Responses are minimal** — pass/fail per check, no detailed context. (`/health/deep` returns a JSON list of (`check`, `status`) pairs but not error messages — full diagnostics live in the admin "System health" page.)
- Returns HTTP 200 on pass, 503 on fail.

**Q6 — Error reporting: in-MATOFX only (option c, locked).**
- **No Sentry / GlitchTip / third-party error tracker** by default — air-gapped customers can't reach them; even non-air-gapped customers' data shouldn't flow to a third party without explicit consent.
- **In-MATOFX error log:** unhandled exceptions get full structured detail in the JSON-Lines diagnostic log + a row in an internal `error_events` DB table (timestamp, request id, user id, route, exception class, redacted stack trace, fingerprint).
- **Admin "Errors" view** in the admin space: lists recent error events with filtering (by route, exception class, fingerprint), shows occurrence counts, lets admins acknowledge / mark resolved. Useful for support hand-off — admins can copy a specific error event id when contacting Funis.
- Customers who want external error tracking can pipe the JSON-Lines logs into their own tooling — that's their integration, not ours.

**Q7 — Admin "System health" page (locked):**
- A dedicated page in the admin space surfacing the most useful at-a-glance view:
  - **Licence:** type, slots used / available, days until expiry, current state (valid / grace / read-only).
  - **Queues:** depth per Arq queue, worker counts, longest-waiting job age.
  - **Recent errors:** count by hour, top fingerprints (links to admin "Errors" view).
  - **Backups:** last run timestamp, last verify timestamp, retention summary.
  - **HPC connectivity:** per-cluster status (healthy / degraded / circuit-broken) + last poll outcome.
  - **Disk usage:** job-storage volume used / free, CAS size, image-store size, audit-log size.
  - **Audit chain:** last verification timestamp, status (intact / suspected drift).
- The same widgets are available for use in custom landing dashboards (per G39) so super-users / admins can blend health into their landing view.
- Refresh-on-demand and auto-refresh-every-30s with a clear refresh indicator.

Implications to carry forward:
- The `/metrics` and `/health/*` routes are in the FastAPI app from week one — added before any business feature so observability accretes alongside features rather than as an afterthought.
- A small `observability.py` module wires Prometheus + OTel + the structured logger + the health-check registry. Each subsystem (auth, queue, DB, etc.) registers its own gauges/counters/checks via this module.
- The admin "Errors" view is a small CRUD-y page that does not interfere with the audit log; error events are diagnostic state and can be aged out per the retention policy (per E27 for diagnostic logs); resolved errors retained until the retention cap.
- The "System health" admin page reuses the metric registry — no duplicate counter definitions; the page just renders the gauges/counters in a friendly UI.
- Default-off for both the metrics-endpoint auth AND OTel tracing is a deliberate "frictionless on intranet" choice; documented honestly in the Security & Compliance Posture so customers know the trade-off.

### H43. Error reporting
**Decision: critical errors notify admins immediately, others go into a daily digest; errors fingerprinted by exception class + top frame + route; support hand-off via error event ids and a customer-reviewed `matofx support bundle`; opt-in crash dumps; tool inputs/outputs never included in support bundles; customer sets the time-boxed support-access duration at grant time.**

**Q1 — Admin notification of errors: critical-immediate + daily digest (option c).**

**Critical errors (immediate notification to all super-users + admins):**
- Signing-chain integrity failures (audit-log or approval chain).
- HPC circuit-breaker trip.
- Backup run failure.
- Licence state transitions (about-to-expire, entered grace, entered read-only).
- Audit-log write failures (a state-changing action could not be persisted to the chain).
- DB connection sustained loss (more than a configurable threshold).
- Worker process repeated crash (more than N within a window).
- Cert about to expire.

**Everything else (daily digest):**
- Aggregated by fingerprint (per Q2): "47 new occurrences of `IntegrityError at /api/jobs.create`, last seen 03:42 UTC".
- Sent once per UTC day at a configurable hour (default 06:00 UTC); skipped when there's nothing new.

The **set of critical fingerprints is hard-coded in v1**; configurable per-fingerprint thresholds are a future option (deferred — H43 originally noted `(d)` as such).

**Recipients (clarifier 1):**
- **Default:** all super-users + admins.
- **Configurable** to a smaller, per-install list (`[notifications.admin_alerts]` block): e.g. a system-alerts mailing list, or a specific subset of admins. Useful for customers with many admins where only a subset is on-call.

**Q2 — Error fingerprinting: exception class + top frame + route (option c).**
- Two error events with the **same exception class** (e.g. `IntegrityError`), **same top frame** (the most-relevant non-library line in the trace), and **same HTTP route** (or queue worker entry-point for non-HTTP code paths) are grouped under one fingerprint.
- Lets admins see "this `KeyError` in `submit_job` has happened 47 times today" rather than 47 distinct events.
- Drill-into-individual-occurrences supported in the admin Errors view for cases where the variant matters (different request bodies, different users hit by the same fingerprint).
- Stack traces are stored full + redacted; the fingerprint key is computed from the redacted form.

**Q3 — Support hand-off path (locked):**
1. Admin sees an issue, opens the admin "Errors" view in MATOFX.
2. Admin filters / picks the relevant error event(s); copies the **error event id** (and/or the job id / session id from the audit log).
3. Admin contacts Funis support (out-of-band — email, customer's normal support channel) with: a brief description of what they were doing, the error event id(s) / job id, optionally a `matofx support bundle` (Q4).
4. If deeper investigation is needed, the admin grants Funis support **time-boxed read access** to the install's audit log (per E26's `support_access` grant mechanism). Customer sets the duration (clarifier 3).
5. Funis investigates with both the support bundle and the audit log; communicates findings; the support-access grant expires.

This is the **only path** by which Funis ever sees customer-side data. There is no Funis-controlled "phone home", no auto-uploaded telemetry.

**Q4 — Self-diagnostic helpers (locked):**

- **`matofx doctor`** — system-wide self-check. Validates: config syntax, DB/Valkey reachability, secret resolution, TLS cert validity + expiry, internal/external CA bundles, GPU toolkit (when GPUs configured), HPC connectivity (when HPC configured), licence status, audit-chain integrity (recent fragment), key-file permissions, NVIDIA Container Toolkit presence, breached-password list freshness. Prints actionable issues with fix suggestions.

- **`matofx support bundle [--out <path>] [--days N]`** — generates a redacted, customer-reviewable diagnostic archive. Contents:
  - Last N days of diagnostic logs (PII-scrubbed: user emails replaced with stable hashes, IPs masked optionally per config).
  - Current system-health snapshot (per H42's admin page).
  - Error fingerprints + counts from the admin Errors view (top 50 most active).
  - Sanitised `matofx.toml` (with all secrets, paths to secret files, and licence-file content stripped — only structural config remains).
  - Licence id and licence-state metadata (NOT the licence file itself).
  - App version, schema version, deployed tool aliases + versions (no descriptors).
  - SBOM (cyclonedx, already produced per H41).
  - **Audit-chain integrity status** (verification result, NOT the audit log itself).

- **`matofx support bundle verify <bundle>`** — opens a generated bundle and shows the admin exactly what it contains, with size/line counts per file. The customer reviews this before deciding whether to send.

- **Bundle never includes (per clarifier 2): tool inputs, tool outputs, job working dir contents, the audit log itself, secrets, the licence file, content-addressed-store contents, user PII beyond hashed email/id.** This is a hard rule — the bundle is for system-level diagnostics, not customer IP.

- The bundle generation itself is audit-logged: when, by whom, what the bundle covered, output path. Sending the bundle is the customer's decision; we never auto-upload.

**Q5 — Crash dumps: opt-in (locked).**
- Off by default.
- Opt-in via:
  ```
  [observability.crash_dumps]
  enabled = false
  output_dir = "/var/lib/matofx/crash-dumps"
  python_faulthandler = true   # safe; logs Python tracebacks on segfault
  core_dumps = false           # heavier; requires container/host privileges
  retention_days = 14
  ```
- When on, Python's `faulthandler` writes a traceback on hard signals; container-level core dumps require explicit host configuration which we document but don't auto-set.
- Crash dumps go to a customer-controlled directory (NOT auto-included in `support bundle` — admin must explicitly add files when sending).

**Clarifier 1 — Recipients of immediate critical notifications: all super-users + admins by default, configurable.**

**Clarifier 2 — Tool inputs / outputs NEVER in the support bundle.** Locked. Customer IP is sacred. Funis support investigating a bug that requires seeing inputs/outputs must do so via the time-boxed audit-log grant on the customer's install — not by receiving exfiltrated data in a bundle.

**Clarifier 3 — Support-access duration: customer-set at grant time.**
- The admin issuing the grant chooses the duration (and a free-text reason).
- Documentation guidance: a few hours for a live debugging session, at most 7 days for a longer investigation.
- Grant + revocation events audit-logged.
- Grants auto-expire; nothing renews automatically.

Implications to carry forward:
- The "critical-error fingerprints" hard-coded list is the single place to add or remove triggers; reviewed at every minor release as the system grows.
- The admin "Errors" view (per H42) becomes the primary front-line tool for customer admins; design it to be useful to non-developers (clear plain-English summaries, "this is what happened" + "this is what to do" guidance per the F30 user-UX rule).
- `matofx support bundle` is one of the highest-leverage tools we ship; getting redaction right matters. Tested in CI: a generated bundle is greppable for canary secrets / canary inputs and any match fails the build.
- The support-access-grant flow (per E26) is built early since H43 depends on it.
- Customers' security teams will read the support bundle's contents in detail before letting it leave their network — the explicit "what's in here" verify command + clear documentation is essential.

---

## I. Migration & timeline

### I44. Migration path from legacy
**Decision: clean break (option a). No migration tool, no data carry-over, no schema/file-format compatibility burden. The legacy `legacy_code/` directory is a reference implementation only — used to learn from when designing v2, never to migrate data out of.**

**Reality (per the user, 2026-05-02): no real customers on the legacy app yet — it's still in development.** This removes the entire migration problem.

**Locked consequences:**
- **No `matofx migrate from-legacy` CLI.** None designed, none planned for v1, none needed.
- **No data-format compatibility constraints on v2.** v2's schema, file formats, audit log, licence model, tool descriptors, etc. can be designed for what's right — no need to preserve any legacy shape.
- **No backwards-compatibility shims** for legacy SQLite schema, legacy `tools/<alias>/` layout, legacy `permissions` JSON-blob columns, legacy `workflow_yes_no` flag, legacy `Streamlit session_state` patterns, etc. The v2 designs already locked in this brief stand on their own without compromise.
- **`legacy_code/` is a reference implementation only.** Useful to look at when designing or porting concepts (we already extracted the architecture into `ARCHITECTURE.md`), but never the source of any production artefact in v2.
- **The clean-break also applies to legacy artefacts that exist in `legacy_code/`:** the SQLite databases under `legacy_code/database/`, any `legacy_code/job_logs/`, `legacy_code/usage_logs/`, etc. are dev fixtures of the legacy app, not data we need to preserve.

**What remains in scope for "migration-ish" work in v1:**
- **Re-implement / port concepts** the user explicitly wants to keep (per B11): the project → scenario → job/workflow hierarchy; tool plugin model with `*_descriptors.json` + static folder; workflow master pattern; custom workflows; approvals; permissions matrix. These are *designed afresh in v2* with clean schemas and the locked decisions in this brief — not "migrated" from the legacy code, just informed by it.
- **Re-implement and harden** the licence / audit / tool-IO concepts — already covered in B11 / C16 / D21 / E26.

**Q2 — Reality check answers (recorded for future readers):**
- Real legacy installs in production: **zero**.
- Active users on the legacy app today: **zero / dev only**.
- Contractual entitlements to migration: **none**.
- Time freed up by skipping migration: significant — months of engineering effort that can go to other features.

**Q3, Q4, Q5 — not applicable** in their original "we'll build a migration tool" form. Recorded as out-of-scope:
- No migration validation tool.
- No side-by-side running plan needed.
- No "migration report" CLI artefact.

If a customer were ever to surface late in v1 development with legacy data they'd want to bring forward, we'd treat that as a one-off bespoke engagement (Funis writes a one-off script for that customer's specific data, signed off by them, run once). Not a product feature.

Implications to carry forward:
- **The `legacy_code/` tree continues to be read-only reference per the existing project rule** (already in memory + CLAUDE.md). Its role becomes purely "look here to understand what was tried before"; nothing in v2 should ever depend on it at runtime.
- **`ARCHITECTURE.md`** (the legacy architecture doc generated earlier) is the canonical reference summary; consult it when porting a concept rather than re-reading the legacy code from scratch.
- **The brief's own decisions are designed afresh** — there is no "but the legacy did it this way" constraint pulling on v2 architecture. Where we *chose* to keep a legacy concept (e.g. descriptor JSON format, retain-list-on-cleanup pattern), it's because we judged it good design, not because we had to preserve compatibility.

### I45. Active users on legacy / parallel run
**Decision: not applicable — there are no real legacy users.**

Direct consequences:
- No parallel-running plan needed.
- No cutover date to design around.
- No data-export-from-legacy step in the v1 install playbook.
- v1 ships as a fresh product, with no need to dual-maintain legacy alongside.

### I46. Timeline / definition-of-done for v1
**Decision: ship v1.0 as fast as possible; v1.0 must-ship core feature list locked (below); v1.x deferred list locked (below); paid pilot with a real customer before general release; the deferred list is firm — no add-backs to v1.0.**

**Q1 — Timeline: ship as fast as possible.**
- The user's intent is to **get v1.0 to a customer's hands ASAP** to start generating revenue and validating the design in the field.
- **Honest scope-vs-time observation (recorded for future readers):** the scope locked across Sections A–H is substantial — enterprise-grade auth, RBAC + ACLs, sandboxed tool execution with warm container pools, signed audit chains, two DB backends, three HPC backends, GPU scheduling, file staging + CAS, backups with restic, encrypted-at-rest, comparison + reporting, multi-landing-dashboards, i18n-ready, observability stack, signing key infra, etc. Realistic minimum at one full-time-developer-equivalent of effort is in the **9–12 month** range; "as fast as possible" should not become "skip the must-ship core" or "skip the H40 test bar."
- **Commitment instead of a date:** review the must-ship list every couple of months as build progresses; if the calendar pressure is real, the right lever is **deferring more items to v1.x**, not lowering the quality bar on what does ship. The locked must-ship list (Q2 below) is the floor.

**Q2 — Definition of "v1.0 ready to ship" (must-ship core, locked):**

| Area | v1.0 must-ship |
|---|---|
| Auth | Local + SSO (one of SAML or OIDC — pick the first customer's preference); MFA mandatory for admins; session management with idle/hard timeouts; rate-limit + lockout |
| Permissions | Full RBAC + project ACLs + flat teams + per-user landing-dashboard assignment; permissions matrix (D20) |
| Hierarchy | Project → Scenario → Job/Workflow with clone, archive, no-delete (D20); user-level legal hold |
| Tool plugin model | Descriptor → image synthesis → containerised execution; the SDK; warm container pools; resource limits; no-network-by-default; read-only shared mounts |
| Approvals | Pre/post/both/none; signed approval records with data-hash; delegation; rejection-and-clone retry |
| Workflows | Single `workflow_definitions` table; sequential dependency-aware-halt execution; released + custom workflow flavours |
| Design exploration | Parametric studies (per G39a / ADR-0001 + 2026-05-19 amendment / ADR-0003): Project-level study; run-table + all sampling + classical RSM designs (per-factor grid levels/spacing) + minimal surrogate; tool-version-pinned, constraint-aware fan-out; durable per-child results store; dedicated results explorer (Plotly 2D/3D + parallel-coords + `csv_plot` curve-overlay + sensitivity); single study-level approval gate |
| HPC integration | At least **one** of Slurm / Rescale / HPC Pack — pick the first customer's preference; circuit breakers + reconciliation; cancellation; pre-staging |
| GPU scheduling | Local NVIDIA with multi-GPU atomic allocation; per-GPU exclusive; wait-until-resources-available |
| File staging | Read-only-bind-mount default; CAS for user uploads; orchestrator retain-list; cleanup audit-logging; advisory per-job quotas |
| Audit log | Full hash-chained signed log; 18-field schema; per-session-per-record reads (default); admin "Cleanup activity" view |
| Licence | Ed25519 offline-signed manifests; CSL + FAL; soft machine-id binding; grace → read-only; periodic re-check |
| Backups | restic-backed dedup; signed manifests; configurable destinations; optional encryption; restore tooling |
| Compare | Same-tool / same-workflow-definition comparisons; 2–10 items; tables + charts + 3D overlay + PDF; CSV + PDF export; saved-comparison sharing |
| Notifications | Email + in-app; three-layer preferences (admin defaults / per-tool / per-user); digest opt-in |
| Theming | Logo, accent colour, light/dark with admin default + user override; co-branding mandatory; multiple landing dashboards (behind feature flag) with starter set |
| Install | Container path + Linux native installer; air-gapped; signed releases; first-user CLI bootstrap |
| I18n | Babel scaffolding; English-only catalog (en-GB default + en-US option); per-user locale + timezone |
| Observability | JSON-Lines logs; Prometheus metrics; OTel instrumentation off-by-default; three-tier health endpoints; admin "Errors" + "System health" pages |
| Admin CLI | `matofx doctor`, `matofx support bundle`, `matofx backup/restore`, `matofx upgrade`, `matofx tool install/update/uninstall`, `matofx licence install/reload`, `matofx tls install/status/renew-self-signed`, `matofx rotate signing-key`, `matofx admin create-first-user`, `matofx tpm enrol` (when key_source = tpm) |
| Quality | All must-be-exhaustively-tested modules per H40; CI gating on Linux + Windows; both DB engines; signed releases with SBOM + SLSA L2 |

**v1.x and beyond (deferred — locked, no v1.0 add-backs per clarifier 2):**

- Customer-side cryptographic tool signing (F29).
- HPC backends beyond the one chosen for v1.0 (e.g. add Rescale to a Slurm-first install, or vice versa); PBS Pro / LSF / cloud Batch / Grid Engine.
- AMD ROCm / Intel oneAPI GPU support.
- Self-service GDPR DSAR portal.
- SCIM provisioning; IdP group → MATOFX role mapping; just-in-time auto-provisioning.
- Slack / Teams / outbound webhook notification channels.
- PowerPoint comparison export.
- True parallel-DAG workflow execution (v1 is sequential-with-dependency-aware-halt).
- Design-space exploration beyond the v1 set (per G39a): **D-optimal** designs and **study-extension** ("add N more points" / "clone-and-refine ranges") are fast-follow; **closed-loop optimisation**, **surrogate capability beyond the minimal v1 fit** (optimisation-on-surrogate, export-surrogate-as-tool, multi-output co-kriging), and **variance-based / Sobol sensitivity** (needs a Saltelli sampling design) are v1.x. (v1.0 ships all sampling + classical RSM modes + a minimal surrogate.)
- Hard per-job disk-quota enforcement (kill-on-exceed).
- Web-based first-run install wizard.
- Additional language catalogs beyond English (**Japanese first** when the customer is committed).
- White-labelling.
- Tool-author-supplied custom landing-dashboard widgets.
- Sensitivity / 3D overlay UX polish iterations.
- Browser-side DSAR / "download my data" portal.
- Tool I/O API (public) — any future move from "browser UI only" to a public REST API.
- Per-project / per-tool override of approver lists.
- N-of-M approval policies.
- Risk-based / step-up authentication.

**Q3 — Paid pilot before general release (locked):**
- Before general v1.0 release, **run a paid pilot with at least one real customer** (the Japanese prospect or whichever customer commits first).
- Pilot uses a normal time-limited MATOFX licence with reduced expiry / N (per C16 — no separate "trial mode" code path).
- Pilot scope is documented; success criteria signed off before the pilot starts.
- Pilot feedback drives any final adjustments before v1.0 ships generally.

**Q4 — `ROADMAP.md` (will be created next):**
- A public-to-Funis roadmap document, separate from this `PROJECT_BRIEF.md`.
- Captures: v1.0 must-ship list (Q2), v1.x deferred list (Q2), longer-horizon ideas, target dates / status as they firm up, the pilot's status.
- Brief is the source of truth for *decisions*; roadmap is the source of truth for *what's next, when*.

**Clarifier 1 — Timeline guidance (recorded):** ASAP, with the recognition that ASAP for the locked scope is realistically 9–12 months at one full-time-developer-equivalent of effort. Pace decisions to be made as build progresses; lever is "defer more to v1.x", not "drop quality."

**Clarifier 2 — No v1.0 add-backs from the deferred list (locked).**

**Clarifier 3 — Paid pilot before general release (confirmed).**

Implications to carry forward:
- The roadmap document is the natural living artefact that tracks status against this plan; the brief itself is mostly stable.
- "ASAP" is also a function of focus — every feature *not* in the must-ship list is not built in v1.0. Any "shouldn't we just add..." moment in build is met with "is it in the must-ship list?" If no, it goes to the roadmap as v1.x.
- The paid pilot's feedback could surface a need to add or change something; that's expected and good — the brief gets updated, the roadmap reflects the new shape.
- Test coverage and the H40 quality bar **does not** trade against shipping speed. Cutting H40 to ship faster is a false economy that will cost more in support and rework than it saves.

### I47. Solo or team build
**Decision: solo build with an AI coding assistant as collaborator (option d). All three documentation categories shipped (foundational + public customer + internal). Lightweight ADRs in `decisions/` capture future architectural decisions beyond what's in the brief. `CONTRIBUTING.md` shipped from day one even though build is solo.**

**Q1 — Build shape: solo + AI assistant (option d).**
- **One human (you), one AI collaborator.**
- The user does the architectural thinking, makes the decisions, reviews / approves all code, runs the deployments, and owns the customer relationship. The assistant does code generation, refactoring, test scaffolding, doc drafts, and design exploration under the user's direction.
- All design decisions live in this brief or future ADRs (Q3); commit history captures the small daily choices.
- **Implication:** code generation throughput can be high but **review burden is the rate-limiter.** Build pace is bounded by how fast the user can read, sanity-check, and integrate generated code. Worth structuring work so that small, reviewable PRs (or commits) are the rule — not large, sprawling change-sets.
- **Memory + brief discipline matters more than usual:** future-you (and any assistant in a fresh session) can only pick up cold from what's written down. The memory entries already saved + this brief + the ADR habit (below) are the institutional knowledge.

**Q2 — Documentation: all three categories (locked).**

**Foundational (already exist or will, at the repository root):**
- `CLAUDE.md` — project root; auto-loaded each session; high-level rules + pointer to PROJECT_BRIEF.
- `PROJECT_BRIEF.md` — this document; source of truth for all design decisions.
- `ARCHITECTURE.md` — legacy app architecture, used as a reference; will gain a v2 sibling once build progresses.
- `ROADMAP.md` — what's next, when; living delivery tracker.
- `decisions/` — ADRs going forward (per Q3 below).
- `CONTRIBUTING.md` — dev onboarding, even for solo build (per Q4).

**Public customer docs (deliverables for v1.0):**
- **Install guide** — air-gapped install from the signed tarball, on Linux + Windows + cloud VM; reverse-proxy walkthroughs (nginx / Caddy / Traefik); TLS setup; first-user CLI bootstrap; SMTP / IdP / HPC configuration; backup / restore; upgrade procedure.
- **Admin guide** — day-2 operations: user management, permissions, tools, workflows, approvals, monitoring (System health + Errors view), licence management, support-bundle generation, support-access grants.
- **Tool-author guide** — `*_descriptors.json` reference, SDK reference, the runtime base library catalogue, examples, debugging tips. **Highest-leverage doc in the product** (per the F28 "MATOFX does the heavy lifting" memory rule — every page that saves tool-author confusion multiplies across customers).
- **End-user guide** — short, friendly, plain-English; for the engineers and non-engineers who actually use MATOFX day-to-day. Aligns with the F30 "users are not modellers" memory rule.

**Internal docs:**
- **Per-module READMEs** — short (a screen of text) per major package describing purpose, key types, where to start reading. Lives in the source tree.
- **ADRs** — per Q3.

**Q3 — Decision records: lightweight ADRs (option b).**
- Going forward, **every architectural decision not already captured in `PROJECT_BRIEF.md`** is recorded as a short Markdown file under `decisions/<NNNN>-short-name.md`.
- ADR template (kept short on purpose):
  ```
  # NNNN. Short decision title

  Date: YYYY-MM-DD
  Status: Proposed / Accepted / Superseded by NNNN

  ## Context
  Why is this decision needed? What's the situation?

  ## Decision
  What did we choose, and why this option vs the alternatives.

  ## Consequences
  What changes as a result. Anything to revisit later.
  ```
- Numbering is monotonic; ADRs are append-only (superseded ones stay, with a Status pointer to the new one).
- ADRs that affect anything in `PROJECT_BRIEF.md` ALSO update the brief — the brief stays the source of truth; the ADR explains *why* a brief decision changed.
- Trivial daily choices (variable names, helper structure, etc.) stay in commit messages, not ADRs. ADRs are for *architectural* decisions: a new dependency, a new public interface, a new module boundary, a behaviour change with downstream implications.

**Q4 — `CONTRIBUTING.md` from day one (locked).**
- Even for solo + AI build, `CONTRIBUTING.md` captures: branch rules (the trunk-based + lazy-maintenance-branch model from H41), PR template, lint + test commands, how to bring up the dev stack locally (DB, Valkey, the test fixture seeder), how to run a single test, how to add an ADR, where the brief and roadmap live.
- Solo today, possibly not solo tomorrow (a contractor, an intern, an acquired team) — this doc is what makes onboarding cheap when the moment arrives.
- Explicitly notes that AI-assisted contributions follow the same rules: review-before-merge, lint-clean, test-covered.

**Clarifier 1 — build shape: solo + AI assistant.**

**Clarifier 2 — public customer docs are v1.0 deliverables, not "future work".** Install / admin / tool-author / end-user guides ship with the product at v1.0.

**Clarifier 3 — ADRs in `decisions/<NNNN>-short-name.md`.** Confirmed pattern.

Implications to carry forward:
- The **review-as-rate-limiter** insight should shape how we plan work: prefer many small, reviewable changes over large change-sets. Match the unit of work to the unit of review.
- The **memory + brief + ADR + commit discipline** is the entire institutional knowledge of the project. Treat it accordingly. When in doubt: write it down. Future sessions pick up where this one leaves off, but only if the trail is clear.
- Onboarding `CONTRIBUTING.md` and per-module READMEs are best built **as the corresponding code is built**, not retrofitted at v1.0 — same energy, less friction.
- **Customer-facing docs** are part of the v1.0 must-ship list — explicitly so, since omitting them turns "ship v1.0" into "give a customer code they can't operate." Documentation is product.

---

# Brief complete

All 47 questions answered and locked. Sections A–I closed. The brief now contains the full set of design decisions for MATOFX SPDM v2.0 with no `_(pending)_` markers remaining.

**Amendment 2026-05-16 (ADR-0001):** §G39a added — parametric studies accepted as v1.0 must-ship (v1-minimal scope). The §I46 must-ship table + deferred list and the feature catalog were updated accordingly. The brief stays the source of truth for decisions; `decisions/0001-parametric-studies.md` records the design rationale.

**Amendment 2026-05-17 (ADR-0002):** §D20 project object-permission model refined to a per-user four-role ACL (immutable Creator + Owner/Member/Viewer), superseding Phase 1's interim team-keyed ACL and adding admin-configurable typed project metadata. The brief stays the source of truth for decisions; `decisions/0002-project-access-and-governance.md` records the rationale, `designs/project-access-and-governance.md` the build.

**Amendment 2026-05-19 (ADR-0001 revision + ADR-0003):** §G39a tightened after 2-PS-a shipped — added the `table` sampling mode, per-factor grid levels + 5 position-warp spacing methods (no min>0 constraint), a descriptor-filtered mode selector, a durable per-child results store, and a `csv_plot`-column response axis + curve-overlay. Two cross-cutting platform changes spun out to `decisions/0003-job-io-document-retention.md`: a `csv_plot` declared output-file kind and an unconditional `inputs.json`/`outputs.json`/`csv_plot` retention floor for every job. The brief stays the source of truth; `decisions/0001-parametric-studies.md` (revision history) + `decisions/0003-…` record rationale, `designs/parametric-studies.md` (§17) the build.

**Project-wide rules captured (in brief + saved to memory):**
1. Legacy code is read-only; write only to `OUTPUTS/` (saved 2026-05-01).
2. No paid or restrictive-licence dependencies (saved 2026-05-01).
3. Every feature is Funis-toggleable per customer at ship time (saved 2026-05-01).
4. Performance and security are equally important — never trade one for the other (saved 2026-05-02).
5. MATOFX does the heavy lifting; tool authoring stays dead simple (saved 2026-05-02).
6. Tool authors never write Dockerfiles or interact with containers (saved 2026-05-02).
7. SPDM end-users are not modellers — radical end-user simplicity is non-negotiable (saved 2026-05-02).

**Companion artefacts:**
- `ROADMAP.md` — living delivery-progress tracker.
- `ARCHITECTURE.md` — reference summary of the legacy app's architecture.
- `decisions/` — ADRs (created on first ADR).
- `CONTRIBUTING.md` — to be authored as build begins.

---

## Derived design decisions

_(populated as answers cluster — fills in as we go)_
