# Design — Tool environment provisioning (auto-venv)

Status: **DRAFT — for review, NOT locked.** Nothing here is built. This document
exists so the maintainer can review the approach and rule on the open decisions
(§17) before any code lands. Once the decisions are locked, a paired ADR-0014
records the *why* and this becomes the build-level *what*.

Decision record (planned): `decisions/0014-tool-environment-provisioning.md`.
Brief section: `PROJECT_BRIEF.md` (env/deployment). Delivery tracking: `ROADMAP.md`.
Builds directly on the just-shipped per-tool `python_env` pin (see
`src/matofx/tools/service.py:set_python_env`, the launcher interpreter resolution
in `src/matofx/hierarchy/service.py:queue_for_runner`, and the `python_env` /
`storage_location` registries in `src/matofx/tools/exec_orm.py`).

This document records *what to build*. The ADR will record *why*. Where this
adds an architectural decision that needs the maintainer's sign-off, it is
flagged **[DECISION]** inline and collected in §17.

---

## 1. Scope at a glance

| Capability | v1 | Fast-follow | v1.x |
|---|---|---|---|
| Declare a tool's Python + deps inline in `main_script` (PEP 723) | ✅ | | |
| Same spec declarable in the descriptor (`environment_spec`) as an alternative | ✅ | | |
| Auto-provision `venv_<alias>` when a tool is installed with no env pinned | ✅ | | |
| Deterministic env path on the shared storage mount (no path guessing) | ✅ | | |
| Auto-register the built env in the `python_env` registry + auto-pin the tool | ✅ | | |
| Build with stdlib `venv` + `pip` from a compiled, hashed lockfile (pip-tools) | ✅ | | |
| Build-job lifecycle (queued → building → ready / failed) with streamed logs | ✅ | | |
| Promotion-to-`released` gated on a green env build | ✅ | | |
| Rebuild on demand (spec changed / corruption / new node) | ✅ | | |
| Per-OS env variants (`linux` / `windows`) for mixed fleets | | ✅ | |
| `uv` as an alternative builder (faster; native PEP 723) | | ✅ | |
| Auto-download the base Python interpreter for `requires-python` | | ✅ | |
| Build on the remote execution target (AOS / HPC node) rather than the mount | | ✅ | |
| Shared env de-duplication (two tools, identical spec → one env) | | | ✅ |
| Container/OCI image build path (instead of a venv) | | | ✅ |

**Guiding principle.** Provisioning is a *governed, observable, reproducible*
step that slots into the existing tool lifecycle — not a magic side-effect. An
auto-built env is an ordinary `python_env` registry row afterwards; everything
downstream (the launcher, the pin, retirement) already works on it unchanged.

---

## 2. The environment spec

A tool declares the Python version and dependencies it needs. Two equivalent
ways to supply it; the loader picks whichever is present (descriptor wins if
both, with a warning):

### 2.1 PEP 723 inline metadata (preferred)

Python's standardised "inline script metadata" block at the top of the tool's
`main_script`. Authors can run the script standalone with `uv run` / `pipx run`
outside MATOFX, and we reuse a published format instead of inventing one:

```python
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#   "numpy==2.1.0",
#   "pyvista==0.44.1",
# ]
# ///
```

We parse it with the stdlib-only reference regex from PEP 723 (vendored, ~15
lines) + `tomllib`. No third-party parser required for v1.

### 2.2 Descriptor field (alternative)

For tools whose `main_script` is compiled/encrypted (the POC `.pyd` Cython path
means the source isn't readable at install time), the same data rides on the
descriptor:

```jsonc
"environment_spec": {
  "requires_python": ">=3.12,<3.13",
  "dependencies": ["numpy==2.1.0", "pyvista==0.44.1"]
}
```

**[DECISION 17.1]** Is PEP 723 the canonical source with the descriptor field as
a fallback, or descriptor-only (simpler, but loses the "runs standalone"
benefit)? Recommendation: support both, descriptor wins on conflict.

### 2.3 What "no spec + no pin" means

If a tool is installed with **neither** an `environment_spec`/PEP 723 block
**nor** an explicit `python_env_id`, behaviour is unchanged from today: the tool
falls back to the **main MATOFX environment** (the server's own interpreter) at
submit time. Auto-provisioning triggers *only* when a spec is present and no env
is pinned. This keeps the feature strictly additive.

---

## 3. Data model + migration

Migration **0071** (next free; current head is 0070).

### 3.1 `python_env` — add provenance columns

The registry row stays the integration point; new nullable columns record *how*
a row came to exist so auto-built envs are distinguishable from hand-registered
ones and can be safely rebuilt/retired:

| col | type | notes |
|---|---|---|
| `origin` | str | `manual` (existing rows; default) or `auto` |
| `source_tool_alias` | str \| null | which tool's spec produced it (auto only) |
| `spec_hash` | str \| null | sha256 of the normalised spec — change detection + de-dup key |
| `os_family` | str | `linux` / `windows`; CHECK-pinned. Existing rows backfilled to the install OS |
| `build_id` | FK → `env_build.id` \| null | the build that produced the live interpreter |

`interpreter_path` semantics are unchanged: absolute, or relative to
`storage_location.mount_path` when `storage_location_id` is set — which is
exactly how auto-built envs are registered (see §4).

### 3.2 `env_build` (new table) — one row per build attempt

| col | type | notes |
|---|---|---|
| `id` | PK BigInt | |
| `python_env_id` | FK → `python_env.id` | the env this build targets |
| `spec_hash` | str | sha256 of the normalised spec it was asked to build |
| `state` | enum | `queued` `building` `ready` `failed` `cancelled` |
| `os_family` | str | `linux` / `windows` |
| `builder` | str | `pip_venv` (v1) / `uv` (fast-follow) |
| `requested_by_user_id` | FK → `user.id` | who triggered (installer / rebuild) |
| `log_path` | str \| null | streamed build log under the job-log area |
| `error_summary` | text \| null | plain-English failure reason for the UI |
| `started_at` / `ended_at` | datetime \| null | |
| audit/no-delete | | append-only; builds are never hard-deleted |

A build is **idempotent on `spec_hash`**: re-requesting a build whose hash
already has a `ready` row is a no-op that re-pins rather than rebuilds.

### 3.3 `Tool` — no new columns

`Tool.python_env_id` (just shipped) is the pin. Auto-provisioning **sets** it.
No schema change on `tool`.

---

## 4. Resolution & path strategy — the "how do we set the path automatically" answer

The path is **deterministic, not guessed**, by leaning on the storage-location
registry the submit path already resolves against:

```
<storage_location.mount_path>/
  envs/
    <os_family>/
      venv_<tool_alias>/
        bin/python            (linux)
        Scripts/python.exe    (windows)
```

The auto-registered `python_env` row stores:
- `storage_location_id` = the active storage location,
- `interpreter_path` = the **relative** sub-path
  `envs/<os_family>/venv_<alias>/bin/python` (or `.../Scripts/python.exe`),
- `os_family` from the build target.

At submit time, `queue_for_runner` already joins `mount_path / interpreter_path`
for storage-relative envs — so **no new resolution logic is needed**; the
launcher invokes the right interpreter for free. Building the env *on the shared
mount* means it is installed once and every node that mounts the share sees it
at the identical path — the same property the `python_env` docstring already
promises.

**The OS-portability caveat (must be designed around, not wished away):** a venv
is **not** portable across OS families — a Linux venv on the mount will not run
on a Windows node and vice-versa. Hence `os_family` on the env row and the
`envs/<os_family>/` path segment. For a single-OS fleet (the Windows + MS-HPC
POC) this is a non-issue: one env, one family. A mixed fleet builds one variant
per family (fast-follow), and the tool pin resolves to the variant matching the
node's OS. **[DECISION 17.2]** v1 = single `os_family` per install (matches the
POC); multi-family is fast-follow. Confirm that ordering.

---

## 5. The build

### 5.1 Toolchain — pip + venv default, uv optional

**v1 builder = stdlib `venv` + `pip`, fed a compiled + hashed lockfile.** This
matches the app's *own* governance posture (pip-tools `pip-compile
--generate-hashes`, already a dev dependency — `pyproject.toml`). Flow:

1. Resolve a base interpreter for `requires_python` (see §8).
2. `python -m venv <env_path>`.
3. `pip-compile` the spec's `dependencies` → a pinned, **hashed** lockfile
   stored alongside the env (`requirements.lock.txt`).
4. `pip install --require-hashes -r requirements.lock.txt`.
5. Smoke-check: `python -c "import <top-level pkgs>"` + record `pip freeze`.

No new heavy runtime dependency; reproducible by hash; same supply-chain story
as the main app.

**Fast-follow builder = `uv`.** A single static binary that reads PEP 723
natively, is dramatically faster, produces a `uv.lock`, and (bonus) can
auto-download interpreters (§8). Gated behind a config switch so adopting it is a
deliberate choice, not a silent new dependency. **[DECISION 17.3]** Approve `uv`
as the fast-follow accelerator, or stay pip-only?

### 5.2 Where the build runs — **[DECISION 17.4]**

The interpreter must exist where the job will run (or on a mount visible from
there). Options, in increasing portability and complexity:

- **(A) On the MATOFX server, writing to the shared mount.** Simplest; works
  when the server and compute nodes share an OS family and mount. **Recommended
  for v1 / the POC.**
- **(B) On the remote execution target** (AOS over WinRM/SSH, or a one-shot HPC
  build job). Needed when the build host must match the run host exactly (native
  extensions, GPU wheels). Reuses the existing AOS/HPC transports
  (`tools/exec/aos_*.py`, `hpc_*.py`). Fast-follow. **Depends on the persistent
  SecretsStore, which is not built yet** (POC note) — building on a remote needs
  the host credentials.
- **(C) In a container.** v1.x; ties into the deferred OCI path.

### 5.3 Build-job lifecycle

A build is modelled as a first-class, observable job (not a hidden subprocess):
`queued → building → ready | failed | cancelled`, with the log streamed to the
job-log area and surfaced live in the UI (same heartbeat-style polling the tool
runner already uses). **[DECISION 17.5]** Reuse the existing runner/heartbeat
machinery for env builds, or a dedicated lightweight build worker? Recommendation:
a dedicated `EnvBuildWorker` that borrows the heartbeat-file pattern but runs
off its own small queue, so a long `pip install` never competes with user jobs
for runner concurrency slots.

---

## 6. Lifecycle integration

### 6.1 On install (`tools/loader.py:install_from_path` → `ToolService.install`)

After the tool row is created, if `(spec present) AND (no python_env_id)`:
1. Compute `spec_hash`.
2. If a `ready` `auto` env with the same `spec_hash` + `os_family` exists →
   re-pin to it (de-dup; v1.x widens this to cross-tool sharing).
3. Else create a `python_env` row (`origin=auto`, deterministic path) + an
   `env_build` row in `queued`, and enqueue the build.
4. Pin `Tool.python_env_id` to the new env immediately; the env is simply
   *not ready* until the build completes.

Install returns promptly; the build runs async. The tool is visible but not
runnable-to-`released` until the build is green (§6.2).

### 6.2 On promote to `released` (`ToolService.promote_state`)

Refuse promotion to `released` while the pinned env's latest build isn't
`ready`, with a plain-English reason ("the tool's Python environment is still
building / failed to build — see the build log"). Promotion to `test` is allowed
(beta testers can exercise a tool whose env is built but unreleased). This mirrors
the existing "tool must pass V&V to submit" gating shape.

### 6.3 On spec change (bundle replace / file edit that touches the spec)

Recompute `spec_hash`; if changed, enqueue a fresh `env_build` against the same
env row (new build supersedes old on success — old interpreter dir is retained
until the new one is `ready`, then swapped, so in-flight jobs aren't disrupted).

---

## 7. Governance, reproducibility & security

This is the part that matters most — `pip install`-ing from a tool author's spec
is the **same trust boundary as running their code**, and must ride the same
governance.

- **Reproducibility.** Every build pins to a hashed lockfile committed beside
  the env. A rebuild from the same `spec_hash` yields the same closure. `pip
  freeze` is recorded on the `env_build` row for forensic diffing.
- **Approval.** **[DECISION 17.6]** Should env builds route through an approval
  gate (an admin/curator signs off the dependency set before it's installed),
  reusing the optimisation/V&V start-gate pattern? Recommendation: **yes**, gated
  behind a config flag — off for the trusted single-maintainer POC, available for
  multi-author installs. Off = build proceeds automatically on install.
- **Allowed sources.** Index URL is install config (default PyPI). An air-gapped
  / on-prem install points it at an internal mirror. No arbitrary `--index-url`
  from the tool spec.
- **Code-encryption interplay.** When the POC `.pyd`/Cython compile is on, the
  `main_script` source isn't readable → the spec **must** come from the
  descriptor field (§2.2). The loader detects a compiled main_script and requires
  `environment_spec`.
- **Audit.** Build request, completion, failure, and re-pin all emit audit
  records (actor, tool, spec_hash, outcome) via the existing `AuditWriter`.
- **No secrets in the spec.** Specs are dependency lists only; private-index
  credentials live in install config / the SecretsStore, never the tool bundle.

---

## 8. Auto-downloading the base Python interpreter (optional, opt-in)

The heaviest, most optional piece — and the second half of the maintainer's
"download Python for linux/windows and the nodes" thought.

- **Default (no download):** resolve `requires_python` against interpreters
  already registered / discoverable on the build host. If none satisfies the
  constraint, the build fails with a clear "no interpreter matching `>=3.12`;
  register one on File paths management or enable auto-download" message.
- **Opt-in download:** `uv python install <version>` (fast-follow, needs §5.3
  builder = uv) or python-build-standalone tarballs fetch a relocatable
  interpreter per `(version, os_family, arch)` into
  `<mount>/pythons/<os_family>/<version>/`. The built venv then uses it as its
  base. **[DECISION 17.7]** Enable auto-download at all in the near term, or
  defer entirely? Recommendation: defer to fast-follow; it adds a download +
  supply-chain surface and is unnecessary when the install already has a base
  Python of the right version (the common case).

---

## 9. Service surface (`matofx/tools/env_provision.py`, new)

- `parse_environment_spec(main_script_text, descriptor) -> EnvSpec | None`
- `spec_hash(spec: EnvSpec) -> str`
- `EnvProvisionService.provision_for_tool(session, *, tool, requested_by) -> EnvBuild`
  — the install/promote hook (create-or-reuse env row + enqueue build + pin).
- `EnvProvisionService.rebuild(session, *, python_env_id, requested_by) -> EnvBuild`
- `EnvBuildWorker.run_once(build_id)` — the actual venv/pip work, heartbeat-style.
- Reuses `PythonEnvsService` (registry CRUD) and `set_python_env` (pin), both
  already shipped.

## 10. API surface

- `GET  /admin/tools/{alias}/environment` — current spec, env, latest build state.
- `POST /admin/tools/{alias}/environment/rebuild` — force a rebuild.
- `GET  /admin/env-builds/{id}` — build detail + state.
- `GET  /admin/env-builds/{id}/log` — streamed build log.
- All `tools.install`-gated (admin/SU), matching the rest of the tool-admin API.

## 11. UI surfaces

- **Edit Tool → new "Environment" sub-section** (next to the just-added
  "Execution environment" picker): show the detected spec, the resolved env, the
  build state badge (Queued / Building / Ready / Failed), a live log pane, and a
  **Rebuild** button. When auto-provisioned, the existing env picker shows
  `venv_<alias>` selected and labelled "auto-built".
- **File paths management → Python envs:** auto-built rows badged `auto` with
  their source tool + spec hash; manual rows unchanged.
- Feature-flagged off → none of the above appears; the existing manual env
  workflow is the whole story.

## 12. Audit

`tools.env.spec_detected`, `tools.env.build_requested`, `tools.env.build_ready`,
`tools.env.build_failed`, `tools.env.repinned`, `tools.env.rebuild_requested` —
each carrying `alias`, `python_env_id`, `spec_hash`, `os_family`, outcome.

## 13. Failure modes & operability

| Failure | Behaviour |
|---|---|
| Spec unparseable | Install succeeds; tool falls back to main MATOFX env; warning surfaced on Edit Tool. **[DECISION 17.8]** warn-and-fallback vs hard-fail the install? |
| No base interpreter for `requires_python` | Build → `failed` with a clear message; tool can't promote to released. |
| `pip install` fails (bad dep, network) | Build → `failed`, full log retained; Rebuild available after fixing the spec. |
| Mount unreachable at build time | Build → `failed`; retryable. |
| Env dir corrupted later | Rebuild from the retained lockfile reproduces it. |
| Two installs race the same `spec_hash` | `env_build` idempotent on hash; second observes the first's `ready` and re-pins. |

## 14. Feature flag + config

- New `Feature.TOOL_ENV_PROVISIONING = "tool_env_provisioning"` (in
  `matofx/features.py`), gating UI + API exactly like the other features.
- Config block (`matofx.toml`): builder (`pip_venv` | `uv`), `index_url`,
  `auto_download_python` (bool), `build_target` (`server` | `remote`),
  `require_build_approval` (bool), `os_families` (list).

## 15. Phased build sequence

1. **Spec + hash** — PEP 723 + descriptor parsing, `spec_hash`, unit tests. (No
   behaviour change; pure functions.)
2. **Migration 0071** — `python_env` provenance cols + `env_build` table.
3. **Build worker** — `pip_venv` builder on the server-writes-to-mount path
   (option A), heartbeat-style state, log streaming.
4. **Lifecycle hooks** — auto-provision on install; promote-to-released gate;
   spec-change rebuild; auto-pin.
5. **API + Edit-Tool UI** — environment sub-section, build badge, log, rebuild.
6. **Feature flag + config wiring**, docs.
7. **Fast-follow** — `uv` builder; per-OS variants; remote build target; optional
   Python auto-download.

Each phase is independently shippable and leaves the tree green.

## 16. Test plan

- Spec parsing: PEP 723 happy/edge (missing block, both sources, malformed
  TOML), descriptor field, compiled-main_script-requires-descriptor.
- `spec_hash` stability + normalisation (ordering, whitespace).
- Provision-on-install: spec+no-pin → env row + queued build + pin; no-spec →
  unchanged fallback; explicit pin → no auto-provision.
- De-dup: identical spec_hash re-pins, doesn't rebuild.
- Promote gate: released blocked until build ready; test allowed.
- Path resolution: auto env's relative path joins to the right interpreter at
  submit (extends the existing `queue_for_runner` interpreter tests).
- Build worker (integration, marked slow): real `venv` + tiny dep from a local
  index → `ready`; bad dep → `failed` with log.
- API auth + 404-when-flag-off.

## 17. Open / deferred decisions — **for maintainer sign-off at review**

1. **Spec source** (§2.1) — PEP 723 + descriptor fallback (recommended), or
   descriptor-only?
2. **OS scope for v1** (§4) — single `os_family` per install for v1 (recommended,
   matches POC), multi-family fast-follow?
3. **uv** (§5.1) — approve as fast-follow accelerator, or stay pip-only?
4. **Build location** (§5.2) — server-writes-to-mount for v1 (recommended), remote
   build fast-follow?
5. **Build execution model** (§5.3) — dedicated `EnvBuildWorker` (recommended) or
   reuse the job runner?
6. **Build approval gate** (§7) — config-flagged approval (recommended; off for
   POC), or always-automatic?
7. **Python auto-download** (§8) — defer to fast-follow (recommended), or in v1?
8. **Unparseable spec** (§13) — warn-and-fallback (recommended) or hard-fail
   install?
9. **Naming** — feature slug `tool_env_provisioning`, env naming `venv_<alias>`,
   doc/ADR titles — any preference?

Nothing in §15 starts until these are settled.
