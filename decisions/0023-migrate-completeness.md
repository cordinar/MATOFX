# 0023. Migrate completeness: links, metadata and dependency rules

Date: 2026-10-06
Status: Accepted + **BUILT 2026-10-06**. Build-level spec: `designs/migrate-completeness.md`.

## Context

Cross-install export/import carried the tool descriptor faithfully but left the
surrounding metadata incomplete and unchecked: two settings dropped, teams /
approver flag / materials unchecked (an unresolved team list becomes visible to
everyone once released), links never reconciled, surrogate tools carrying a
foreign database id, no `bundle_hash`, silent downgrade/demotion, workflows without
icon, change log, update path or validation — and a preview that returned 500 for
any workflow-plus-members bundle.

## Decision

1. Bundle v2 adds run cap, the optimisation start-approval flag, the workflow icon
   and member tool versions. v1 bundles still import; newer-than-known are refused
   plainly.
2. Import mirrors a normal install's checks (teams, approver flag, materials) and
   never removes a link: it adds the missing half on whichever side exists and
   names every link it can't complete.
3. **An import over a released tool still lands it in `new`** (the existing
   "always new" rule), with an explicit warning in preview and report.
4. **A tool or workflow whose teams all fail to resolve imports with no teams and
   cannot be promoted out of `new` until teams are set** (`teams_pending`).
5. **Surrogate tools are refused at export.**
6. **Workflows that already exist are updated** (higher version), no-op when
   identical, refused on conflict or downgrade.
7. Preview runs the real import in one rolled-back savepoint; any per-item
   exception skips that item instead of aborting the import.

## Consequences

Re-importing the same bundle stays idempotent (`downstream_tools` is excluded from
the "same content" test because it is reconciled additively). Repairs to tools
already on the destination are ordinary metadata edits — patch bump, change log,
audit — so they are visible and reversible.
