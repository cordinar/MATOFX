# MATOFX Migration Guide — Tools & Workflows

**Audience:** super-users and administrators of a MATOFX SPDM install who need to copy tested tools and admin-released workflows from one install (typically Staging) to another (typically Production).

**Scope:** this feature copies **tool definitions** and **admin-released workflow definitions** between MATOFX installs. It does **not** copy users, projects, scenarios, submitted jobs, the audit log, tutorials, runtime settings, or custom (user-owned) workflows. If you need a full clone of one install onto another, use the backup → restore tooling instead.

**Mental model:** you are not migrating *data*. You are migrating *capabilities* — "this tool, exactly as it works on Staging, now also exists on Prod." Jobs that users run on Prod against the imported tool are still Prod's jobs; Staging's job history does not come with it.

---

## 1. Before you start

### 1.1 What you need

- **Two MATOFX installs** of the same major release. Call them the **source** (where the tool/workflow is working today) and the **destination** (where you want it to land). The standard case is Staging → Prod, but A → B is fine for any A and B.
- **Network connectivity** between you and both installs. The installs do **not** need to talk to each other — the bundle file moves over whatever transport you already use (SCP, SFTP, a USB stick, an internal file share, an attached email — your choice).
- **The `migrate.tools_workflows` permission** on both installs. Super-users have it by default. A super-user can grant it to an administrator under **Admin → Users → (user) → Permissions**.
- **The feature must be enabled** in the install's licence. If you cannot see **Admin → Migrate Tools/Workflows** in the sidebar of either install, your licence does not include this capability — contact Funis.

### 1.2 What changes on the destination

Reading this section first will save surprises later.

- **Tool descriptor and `static/` files** arrive byte-for-byte from the source. MATOFX rebuilds the tool's container image locally on the destination from the same descriptor, so the destination ends up with its own image — no foreign binaries are ever imported.
- **Tool settings** (AOS host, HPC cluster, Python environment, storage location, resource requirements, approval policy, email notifications, **run cap**, **optimisation start-approval**) **are preserved from the source**, as long as the destination has matching entries (matched by name, not by row ID). A missing AOS host / HPC cluster / Python env / storage location, or an unknown material class, skips the tool with a clear message — see §6. Required **licences** travel with the tool but can't be checked automatically; the status log lists them.
- **Teams and approvers** are checked like a normal install: unknown teams are dropped; approvers who don't exist, are deactivated, or don't have *Can approve runs* are dropped (a tool needing approval with none left is skipped). If **none** of an item's teams exist, it imports with no teams and **can't be promoted until teams are set**.
- **Workflow links** (which tool feeds which) arrive intact. Where one side of a link is already on the destination, the import completes the other side (an ordinary edit, with a patch bump and change-log entry, reported as *Also updated tool …*); links it can't complete are named in the log.
- **Tool lifecycle state is reset to `new`** on the destination, regardless of what state the tool was in on the source. You promote it through `test` and `released` on the destination yourself when you are satisfied it works. This is deliberate: a cross-install import is exactly when you want a human sign-off before users see it.
- **Workflows arrive in `new` state** too, with their icon, and are validated like a save in the workflow editor (member order). Approvers live on member tools, not on workflows.
- **Surrogate tools are not exportable** — they run a fitted model held on the source install.

### 1.3 What does NOT change on the destination

- Users, teams, projects, scenarios, jobs, the audit log, tutorials, branding, runtime settings, custom workflows. None of these touch the destination.
- Nothing is ever removed. An existing tool or workflow is **replaced only by a newer version** from the bundle — it then goes back to `new` (the preview warns if it was `test`/`released`). The same version with identical content is a no-op (re-importing is safe); the same version with different content, an older version, or an item archived here is skipped. A user's personal (custom) workflow is never replaced.
- The only other change to existing items is completing a workflow link (adding an imported tool to an existing tool's downstream list), reported in the log.

---

## 2. One-time setup — trust the source install

This step happens **once per source-destination pair**. After it's done, you can do as many migrations as you like without repeating it.

### 2.1 Why a trust step exists

A signed bundle proves *who signed it*. Without a trust list, the destination has no way to know that "signed by the Staging install" is meaningful — anyone with their own MATOFX install could sign a bundle. The trust list is the destination admin's statement: "yes, bundles from this specific source install are allowed here." Adding and removing trusted sources is audit-logged on the destination.

### 2.2 Reading the source install's fingerprint

On the **source** install:

1. Go to **Admin → Migrate Tools/Workflows**.
2. Expand the **Trusted peers** panel (collapsed by default).
3. Click **Show this install's fingerprint**.
4. Copy the displayed fingerprint. It looks like:

   ```
   9F:2E:A1:7B:04:3D:8C:E1:55:6A:2F:97:CB:0D:18:7C
   ```

   The fingerprint is also shown alongside every bundle this install exports, so you can double-check it later.

### 2.3 Adding the source's fingerprint to the destination's trust list

On the **destination** install:

1. Go to **Admin → Migrate Tools/Workflows**.
2. Expand the **Trusted peers** panel.
3. Click **Add trusted peer**.
4. Paste the fingerprint into the **Fingerprint** field.
5. Enter a short human-readable **Label** — typically the name of the source install, e.g. `Staging` or `Funis Dev`. The label is purely for your future reference; it does not affect verification.
6. Click **Add**.

The destination will now accept bundles signed by that source install. The trust-add event is recorded in the destination's audit log with the fingerprint, the label, who added it, and when.

### 2.4 Removing trust

If a source install is decommissioned or its key rotates, remove the fingerprint via the same panel. Future imports signed by that fingerprint will be refused; already-imported tools and workflows are not affected.

---

## 3. Exporting from the source

You do this every time you want to ship a new bundle from source to destination.

### 3.1 What to bundle together

A single bundle can contain any combination of tools (each at one or more versions) and workflows. The rule of thumb: **bundle together everything that belongs together.** If workflow `thermal_baseline` references tools `material_properties` and `heat_transfer`, put all three in the same bundle — the destination will resolve the references in one go and skip nothing.

Leave **Include required tools** ticked (the default) and the export adds every member of a chosen workflow and every tool a chosen tool takes inputs from, at their current versions. If you untick it and bundle a workflow without one of its tools — and the destination doesn't already have that tool — the workflow is skipped on import with a "missing dependency" message.

### 3.2 Steps on the source

1. Go to **Admin → Migrate Tools/Workflows** on the source install.
2. Open the **Export** panel.
3. In the **Tools** picker, select each tool you want to export. Each row shows the tool's name and version. Multi-select is fine — you can mix versions of the same tool if needed.
4. In the **Workflows** picker, select each admin-released workflow you want to export.
5. (Optional but recommended) Fill in **Notes** with a one-line description — for example, `"Promote thermal pipeline after QA sign-off (ticket MTX-412)"`. The notes are visible at import time on the destination, and they help the destination admin understand what they're about to import.
6. Click **Generate bundle**.

The browser downloads a file named like:

```
matofx-migrate-2026-05-15T14-22-08Z-staging-9f2ea17b.tar.gz
```

The filename embeds the export timestamp (UTC) and a short fragment of the source install's fingerprint. It is purely informational — the real signing material lives inside the tarball.

### 3.3 What's in the bundle

You do not need to open the tarball — but if you ever want to satisfy your curiosity:

```
manifest.json          # bundle metadata, tool settings, workflow definitions + icons, file hashes
manifest.sig           # detached Ed25519 signature over manifest.json
tools/
  material_properties-1.5.0/
    descriptor.json    # authoritative, from the source database
    static/...
```

Only `manifest.json` is signed. Workflow definitions (and their icons) are inlined in it; the manifest carries the hash of every tool file, so per-file integrity is verified against the signed manifest at import time. The bundle format is versioned (currently 2); an install refuses a bundle made by a newer MATOFX and asks you to upgrade it first.

### 3.4 Transferring the bundle to the destination

Whatever you use to move files between the two hosts. Same subnet? `scp` is the obvious choice:

```powershell
scp matofx-migrate-2026-05-15T14-22-08Z-staging-9f2ea17b.tar.gz `
    admin@prod-host:/tmp/
```

The bundle does not contain secrets. It is signed, not encrypted. If the transport itself is sensitive, use a transport that encrypts (SCP/SFTP do).

---

## 4. Importing on the destination

### 4.1 Dry-run first

Always dry-run. The dry-run reads the bundle, verifies the signature, parses the manifest, checks every reference against the destination's current state, and renders a plain-English preview of what *would* happen — without writing anything to the destination's database.

1. Go to **Admin → Migrate Tools/Workflows** on the destination install.
2. Open the **Import** panel.
3. Drop the `.tar.gz` file onto the file-drop area (or use the file picker).
4. Click **Preview (dry-run)**. The preview runs the real import and then undoes it, so it reports exactly what **Confirm import** will do.

A preview looks like this (the wording is what the page shows):

```
Verifying signature...
Signed by 'Staging' (fingerprint 9F:2E:A1:7B:...). Trusted.
Bundle contents: 3 tool(s), 1 workflow(s). Exported 2026-05-15T14:22:08+00:00 by 'alice@funisoft.com'.
Importing tools...
  Tool material_properties 1.5.0: needs licence(s) ANSYS_CFD (x2). This install can't check
    licence availability automatically — confirm the licence manager provides them.
  IMPORTED  Tool material_properties 1.5.0 imported (state: new).
  Tool heat_transfer 2.1.0: dropped approver(s) 'bob' (doesn't exist here).
  Tool heat_transfer 2.1.0: replaces version 2.0.0, which is 'released' here. The tool goes
    back to 'new' and is unavailable to users until you promote it again.
  IMPORTED  Tool heat_transfer 2.1.0 updated (state: new).
  SKIPPED   Tool stress_analysis 1.2.0 is bound on the source to HPC cluster 'london_hpc', which
    does not exist on this install. Create it under Admin -> Execution Backends -> HPC Clusters,
    then re-import.
Checking workflow links...
  Would also update tool mesh_prep (1.3.0 → 1.3.1): it now lists material_properties as a
    downstream tool, so material_properties's inputs can take its outputs.
Importing workflows...
  SKIPPED   Workflow 'thermal_baseline' depends on tool 'stress_analysis', which was not imported
    (see earlier log entries) and is not already installed here.
Tools: 2 imported, 1 skipped, 0 already present (no-op).
Workflows: 0 imported, 1 skipped, 0 already present (no-op).
This was a preview — nothing has been written.
```

An untrusted bundle is refused before anything else:

```
This bundle was signed by an install that is not in this install's trusted peer list
(fingerprint 1A:2B:3C:...). Add it under Admin -> Migrate Tools/Workflows -> Trusted peers.
```

The dry-run **never modifies the destination**. You can dry-run the same bundle as many times as you like.

### 4.2 Commit

When the preview looks right:

1. Click **Confirm import**.
2. The page streams live status to a scrollable log as each item is processed. Each item gets one line, then a multi-line block if there's anything to report about it.

The log has the same lines as the preview, without "would", followed by next steps —
promote imported items, and set teams on any item imported without teams.

### 4.3 Atomicity guarantees

- **Per-item:** each tool and each workflow imports inside its own database transaction. A failure on one item — including an unexpected error — rolls back only that item; everything else proceeds.
- **Cascading skips:** if a tool is skipped, any workflow in the same bundle that depends on that tool is also skipped — the dependency reason is cited in the log.
- **Re-importing the same bundle is safe.** Items already present on the destination at the same descriptor hash are no-ops. Items that were skipped last time but now resolve will be imported on the second pass.
- **A failed bundle never leaves the destination half-changed.** Every item is either fully imported or fully skipped. There is no "imported the descriptor but not the static files" state.

---

## 5. Post-import checklist

After a successful import:

1. **Promote new tools.** Imported tools land in `new` state and are not visible to regular users. Under **Admin → Tools**, exercise each new tool with a test job, then promote `new → test → released` when satisfied. The state machine is identical to a tool installed via `matofx tool install`.
2. **Verify execution bindings.** Open each imported tool's settings page and confirm the AOS host / HPC cluster / Python env / storage location / licence look right for the destination environment. The import preserved these by name; if your environments differ deliberately (e.g. Prod uses a bigger HPC cluster than Staging), this is the moment to adjust.
3. **Check approvers and teams.** Dropped approvers and teams are listed in the status log. Fix approver lists on the tools' Edit Tool pages; any item imported **with no teams** shows an amber notice and can't be promoted until you choose its teams.
4. **Test a real job end-to-end** against each imported tool or workflow before announcing the change to users.
5. **Delete the bundle file** from the destination host. It's no longer needed; the audit log already records the bundle hash and contents.

---

## 6. Common skipped-item scenarios and how to resolve them

The plain-English error messages below are the actual strings the UI shows. They come from `matofx.migrate.errors` — if you ever see one not listed here, send it to Funis support; the message catalog should cover every case.

### 6.1 "This bundle was signed by an install that is not in this install's trusted peer list."

The source's fingerprint hasn't been added to the destination's trust list. See §2.3.

### 6.2 "Tool X version Y already exists here with a different descriptor."

The destination already has `(alias, version)` installed, but the descriptor is different from the bundled one. Almost always this means somebody edited the tool on the source after the destination first received an earlier version of it. The fix:

1. On the source, bump the tool's version (e.g. `1.5` → `1.5.1`).
2. Re-export the bundle.
3. Retry the import.

Forcing a re-install of the same version with different contents would break job reproducibility for any past job pinned to the existing version — which is why this is blocked outright rather than presented as an override option.

### 6.3 "This tool is bound on the source to AOS host 'NAME', which does not exist on this install."

The tool's runtime is pinned to a named AOS host. The destination doesn't have a host with that name. Either:

- Create the host on the destination first under **Admin → Execution Backends → AOS Hosts** (using the same name), then re-import the bundle, **or**
- Decide that the tool should bind to a different host on the destination — in which case, import the tool first by exporting it on the source with no AOS host bound, and re-bind it on the destination after import.

The same shape of message and resolution applies for HPC clusters, Python envs, storage locations, and material classes.

### 6.4 "Depends on tool X, which was not imported (see above)."

A workflow references a tool that was skipped (for any of the reasons above) or that isn't present in the bundle and isn't already installed on the destination. Resolve the underlying tool issue and re-import.

### 6.5 "A user's personal (custom) workflow on this install already uses that alias."

An existing **admin** workflow with the alias is updated by a newer version instead. Only a user's own custom workflow blocks the import. Rename the workflow on the source and re-export.

### 6.6 "Dropped approver(s) 'NAME' (reason)."

A **warning, not an error**: the approver doesn't exist here, is deactivated, or doesn't have *Can approve runs*. The tool is still imported unless it needs approval and no approvers remain. Fix the list on the tool's Edit Tool page.

### 6.7 "This install already has the newer version …; importing would downgrade it."

The bundle carries an older version than the destination has. Export the current version from the source, or leave the item out.

### 6.8 "… unexpected error while importing …"

Something failed that MATOFX didn't anticipate. Only that item was skipped and nothing was changed for it; the server log has the details. Send the message to Funis support.

---

## 7. CLI parity

Every UI action has a CLI equivalent for use in scripts or operator runbooks. The CLI produces the same plain-English status output that the UI shows.

```powershell
# Export
matofx migrate export `
  --tools material_properties:1.5,heat_transfer:2.1 `
  --workflows thermal_baseline `
  --notes "Promote thermal pipeline after QA sign-off (ticket MTX-412)" `
  --include-required `
  --out bundle.tar.gz

# Dry-run an import without writing
matofx migrate import bundle.tar.gz --dry-run

# Commit
matofx migrate import bundle.tar.gz

# Trust list management
matofx migrate trust add 9F:2E:A1:7B:04:3D:8C:E1:55:6A:2F:97:CB:0D:18:7C --label "Staging"
matofx migrate trust list
matofx migrate trust remove 9F:2E:A1:7B:04:3D:8C:E1:55:6A:2F:97:CB:0D:18:7C
```

The CLI is gated by the same `migrate.tools_workflows` permission as the UI and writes the same audit events.

---

## 8. Audit trail

Every migration-related action is audit-logged.

- On the source: `migrate.bundle.exported` (bundle hash, contents inventory, notes, exporting user).
- On the destination: `migrate.bundle.imported` (bundle hash, per-item outcomes including skipped items and reasons, link repairs made to existing tools, source fingerprint, importing user). Each link repair is also an ordinary `tools.tool.update_descriptor` event.
- Trust-list mutations: `migrate.peer.trusted` / `migrate.peer.untrusted` (fingerprint, label, acting user, free-text reason).

The bundle hash appears in both the source's export entry and the destination's import entry, so the two ends can be cross-referenced during an investigation.

---

## 9. What this feature deliberately does not do

- **No automatic transfer between installs.** The two installs never talk to each other directly. This is a security property, not an inconvenience — it means a compromised source install cannot push anything to a destination without an operator deciding to move the bundle.
- **No automatic promotion on the destination.** Imported items always land in `new` state. Promoting to `test` or `released` is an explicit human decision on the destination.
- **No carrying over of users, projects, jobs, audit history, custom workflows, tutorials, runtime settings, or branding.** Use backup → restore if that's what you actually want.
- **No same-version overwrites or downgrades.** `(alias, version)` with different content, or an older version, is a hard skip; bump the version on the source.
- **No surrogate tools, V&V records or tutorials.** Re-create surrogates on the destination.
- **No bundle encryption (only signing).** Bundles are signed for authenticity. They are not encrypted. If the transport between source and destination is sensitive, use an encrypted transport (SCP, SFTP, an internal share over TLS).
- **No partial bundle.** Either the bundle parses and verifies cleanly as a whole, or no items are imported. Per-item atomicity applies once verification succeeds.
