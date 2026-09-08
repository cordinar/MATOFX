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
- **Tool settings** (assigned licence, AOS host, HPC cluster, Python environment, storage location, resource requirements, queue, approval-policy flag, email-notifications flag) **are preserved exactly from the source**, as long as the destination has matching entries (matched by name, not by row ID). If the destination is missing a referenced AOS host or HPC cluster, the tool is skipped with a clear message — see §6.
- **Tool lifecycle state is reset to `new`** on the destination, regardless of what state the tool was in on the source. You promote it through `test` and `released` on the destination yourself when you are satisfied it works. This is deliberate: a cross-install import is exactly when you want a human sign-off before users see it.
- **Workflow approver lists** are matched by username/email. Approvers who exist on the destination are kept. Approvers who do not exist on the destination are dropped from the list, with a note in the status log. The workflow itself is still imported.
- **Workflows arrive in `new` state** too.

### 1.3 What does NOT change on the destination

- Users, teams, projects, scenarios, jobs, the audit log, tutorials, branding, runtime settings, custom workflows. None of these touch the destination.
- Existing tools and workflows on the destination are not modified or removed. The only way an existing tool changes is if the imported bundle contains the **same `(alias, version)` with an identical descriptor** — that case is a no-op (re-importing the same bundle is safe and idempotent).

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

If you bundle a workflow without one of its tools, and the destination doesn't already have that tool installed, the workflow will be skipped on import with a "missing dependency" message. The fix is to add the tool to the bundle and re-export.

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
manifest.json          # bundle metadata + per-item inventory + hashes
manifest.sig           # detached Ed25519 signature over manifest.json
tools/
  material_properties-1.5/
    descriptor.toml
    descriptor.hash
    static/...
workflows/
  thermal_baseline.json
```

Only `manifest.json` is signed. The manifest carries the hash of every per-item file, so per-file integrity is verified against the signed manifest at import time.

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
4. The page automatically runs the dry-run and shows the preview within a few seconds.

A clean dry-run looks like this:

```
Verifying signature...
  Signed by Staging (fingerprint 9F:2E:A1:7B:04:3D:8C:E1:55:6A:2F:97:CB:0D:18:7C)
  Trust status: trusted (added 2026-04-12 by robert@funisoft.com, label: "Staging")

Reading bundle contents...
  2 tools, 1 workflow
  Exported 2026-05-15 14:22:08 UTC by alice@funisoft.com on Staging
  Notes: "Promote thermal pipeline after QA sign-off (ticket MTX-412)"

Checking against this install...

  Tools:
    material_properties 1.5    NEW VERSION    (this install has 1.4 as the latest)
    heat_transfer 2.1          NEW VERSION    (this install has 2.0 as the latest)

  Workflows:
    thermal_baseline           NEW WORKFLOW   (no existing workflow with this alias)

  References (resolved by name on this install):
    AOS host  "ansys_main"          present
    HPC cluster "london_hpc"        present
    Python env "python3_11_ansys"   present
    Approver  "alice@funisoft.com"  present
    Approver  "bob@funisoft.com"    not present (will be dropped from approver list)

Ready to import. Click "Confirm import" to apply.
```

A dry-run with problems looks like this:

```
Verifying signature...
  Signed by Funis Dev (fingerprint 1A:2B:3C:...)
  Trust status: NOT TRUSTED

This bundle was signed by an install that is not in this install's trusted peer list.
To accept it, an administrator must first add fingerprint 1A:2B:3C:... to the trust
list (Admin -> Migrate Tools/Workflows -> Trusted peers -> Add trusted peer).

Import is blocked.
```

Or with partial problems that would still allow the rest through:

```
Verifying signature...
  Signed by Staging  trusted

Reading bundle contents...
  3 tools, 2 workflows

Checking against this install...

  Tools:
    material_properties 1.5    NEW VERSION
    heat_transfer 2.1          WILL BE SKIPPED
       Reason: this tool is bound on the source to HPC cluster "london_hpc",
       which does not exist on this install. Create it under
       Admin -> Execution Backends -> HPC Clusters, then re-import.
    stress_analysis 1.2        NEW VERSION

  Workflows:
    thermal_baseline           NEW WORKFLOW
    thermal_with_heat_xfer     WILL BE SKIPPED
       Reason: depends on tool heat_transfer 2.1, which will not be imported (see above).

If you confirm now, this install will import 2 tools and 1 workflow,
and skip 1 tool and 1 workflow. You can resolve the skipped items
and re-import the same bundle to pick them up.
```

The dry-run **never modifies the destination**. You can dry-run the same bundle as many times as you like.

### 4.2 Commit

When the preview looks right:

1. Click **Confirm import**.
2. The page streams live status to a scrollable log as each item is processed. Each item gets one line, then a multi-line block if there's anything to report about it.

A successful import log:

```
Starting import...

Tool material_properties 1.5
  descriptor verified against bundle hash
  installed at material_properties/1.5/
  container image built from descriptor (hash recorded)
  settings carried over from source:
    AOS host: ansys_main
    Python env: python3_11_ansys
    Resource requirements: 4 CPU / 16 GB RAM
    Approval policy: pre-approval required
    Email notifications: enabled
  state: new
  IMPORTED

Tool heat_transfer 2.1
  ... (similar block)
  IMPORTED

Workflow thermal_baseline
  3 member tools resolved by alias: material_properties, heat_transfer, stress_analysis
  approver list:
    alice@funisoft.com   matched (kept)
    bob@funisoft.com     not present on this install (dropped)
  state: new
  IMPORTED

Summary
  Tools:     2 imported, 0 skipped, 0 failed
  Workflows: 1 imported, 0 skipped, 0 failed

Next steps:
  1. Visit Admin -> Tools to promote material_properties 1.5 and heat_transfer 2.1
     from 'new' to 'test' or 'released'.
  2. Visit Admin -> Workflows to set the approver list for thermal_baseline
     (one approver was dropped during import) and promote it from 'new' to 'released'.
  3. The bundle has been recorded in the audit log. The bundle file itself is no longer
     needed; you can delete it from disk.
```

### 4.3 Atomicity guarantees

- **Per-item:** each tool and each workflow imports inside its own database transaction. A failure on one item rolls back only that item; everything else proceeds.
- **Cascading skips:** if a tool is skipped, any workflow in the same bundle that depends on that tool is also skipped — the dependency reason is cited in the log.
- **Re-importing the same bundle is safe.** Items already present on the destination at the same descriptor hash are no-ops. Items that were skipped last time but now resolve will be imported on the second pass.
- **A failed bundle never leaves the destination half-changed.** Every item is either fully imported or fully skipped. There is no "imported the descriptor but not the static files" state.

---

## 5. Post-import checklist

After a successful import:

1. **Promote new tools.** Imported tools land in `new` state and are not visible to regular users. Under **Admin → Tools**, exercise each new tool with a test job, then promote `new → test → released` when satisfied. The state machine is identical to a tool installed via `matofx tool install`.
2. **Verify execution bindings.** Open each imported tool's settings page and confirm the AOS host / HPC cluster / Python env / storage location / licence look right for the destination environment. The import preserved these by name; if your environments differ deliberately (e.g. Prod uses a bigger HPC cluster than Staging), this is the moment to adjust.
3. **Set workflow approvers.** Imported workflows land with approver lists matched by username; any approvers that did not exist on the destination were dropped. Under **Admin → Workflows**, open each imported workflow and confirm the approver list is correct before releasing it.
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

The same shape of message and resolution applies for HPC clusters, Python envs, storage locations, and licence configs.

### 6.4 "Depends on tool X, which was not imported (see above)."

A workflow references a tool that was skipped (for any of the reasons above) or that isn't present in the bundle and isn't already installed on the destination. Resolve the underlying tool issue and re-import.

### 6.5 "Workflow alias X already exists on this install."

The destination already has a workflow with this alias. Workflows don't have a version axis the way tools do, so a name collision is a hard skip — the destination admin must rename one side. Easiest fix: rename the workflow on the source before re-exporting.

### 6.6 "Approver 'EMAIL' is not present on this install."

This is a **warning, not an error.** The workflow is imported, and the unmatched approver entry is dropped from the approver list. Set the correct approver list under **Admin → Workflows → (workflow) → Approvers** after import.

---

## 7. CLI parity

Every UI action has a CLI equivalent for use in scripts or operator runbooks. The CLI produces the same plain-English status output that the UI shows.

```powershell
# Export
matofx migrate export `
  --tools material_properties:1.5,heat_transfer:2.1 `
  --workflows thermal_baseline `
  --notes "Promote thermal pipeline after QA sign-off (ticket MTX-412)" `
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
- On the destination: `migrate.bundle.imported` (bundle hash, per-item outcomes including skipped items and reasons, source fingerprint, importing user).
- Trust-list mutations: `migrate.peer.trusted` / `migrate.peer.untrusted` (fingerprint, label, acting user, free-text reason).

The bundle hash appears in both the source's export entry and the destination's import entry, so the two ends can be cross-referenced during an investigation.

---

## 9. What this feature deliberately does not do

- **No automatic transfer between installs.** The two installs never talk to each other directly. This is a security property, not an inconvenience — it means a compromised source install cannot push anything to a destination without an operator deciding to move the bundle.
- **No automatic promotion on the destination.** Imported items always land in `new` state. Promoting to `test` or `released` is an explicit human decision on the destination.
- **No carrying over of users, projects, jobs, audit history, custom workflows, tutorials, runtime settings, or branding.** Use backup → restore if that's what you actually want.
- **No version overwrites.** `(alias, version)` with a different descriptor is a hard skip; bump the version on the source.
- **No bundle encryption (only signing).** Bundles are signed for authenticity. They are not encrypted. If the transport between source and destination is sensitive, use an encrypted transport (SCP, SFTP, an internal share over TLS).
- **No partial bundle.** Either the bundle parses and verifies cleanly as a whole, or no items are imported. Per-item atomicity applies once verification succeeds.
