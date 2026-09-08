# 0019. Licence subsystem + vendor-verifiable compliance attestation

Date: 2026-08-09
Status: Accepted (design discussion with the maintainer, 2026-08-09). **Slices 1–2 SHIPPED** — slice 1 2026-08-09 (`matofx/compliance/`, migration 0088, `matofx compliance adopt|report|verify`), slice 2 2026-08-10 (`matofx/licence/`, migration 0089, `matofx licence install|reload|show`), slice 3 2026-08-10 (startup gate + hourly re-check + features from the manifest), slice 4 2026-08-10 (CSL slots at login + break-glass + Active sessions page), slice 5 2026-08-10 (FAL assignment slots), slice 6 2026-08-10 (read-only middleware + integrity acknowledge), slice 7 2026-08-10 (transition events + seats cap). **All seven slices shipped.** **Anchoring reworked 2026-08-11** after a stress-test pass (report schema **v2**, migration 0091, `compliance/merkle.py`): RFC 6962 consistency proofs replace head comparison, the startup integrity check specified in §C14 is now actually built, and attestations expire. See the amendment notes in §A2/§A3/§A5/§C14 and the **Amendments** section at the end. Implements PROJECT_BRIEF §C16 (licensing model) and revises §B11's audit-key derivation. Extends ADR-0005 (capabilities) at the enforcement points and the existing audit chain (`matofx/audit/`). **Gap list closed 2026-08-12**: all eleven known gaps in the overview's §13 are now shut — issuing + ledger-custody tooling, the admin report page, mid-year licence changes applying without a restart, a tampered licence failing closed on a *running* process, the dead `licence.override` counter, the proof window's selection, break-glass bounds + licence alerting, and container guidance for machine binding. See ARCHITECTURE §16.58–16.64. Worked artefacts + test vectors: `designs/examples/`. Plain-English orientation for the whole subsystem, including the gap history and the two decisions that look like omissions: `designs/licence-and-compliance-overview.md`. Funis-side procedure: `designs/funis-vendor-runbook.md`. Plain-English explainer for non-specialist readers (evaluators, security reviewers, new maintainers): `designs/licence-and-compliance-explained.md`.

## Context

Two gaps, one design.

**No licence subsystem exists.** `features.py` gates surfaces but says the manifest
"lands with the licence subsystem in Phase 2"; `UsageService.seats` returns
`cap=None` with the same note. Nothing reads a licence, nothing counts slots,
nothing expires. §C16 settled the commercial model (CSL / FAL, signed JSON
manifest, YubiKey-backed Funis signing, grace → read-only, no trials) but none of
it is built.

**The audit chain cannot answer the vendor's question.** MATOFX has a real
hash-chained, Ed25519-signed audit log — genesis hash, `prior_hash` links, a
singleton `audit_chain_head` locked `SELECT … FOR UPDATE` so the chain cannot
fork, and a verifier (`matofx audit verify`, `POST /api/admin/audit/verify`)
that recomputes every hash and checks signatures across key rotations. What it
cannot do is let **Funis** ask a customer "prove nothing was tampered with, and
that you didn't circumvent the licence":

1. There is nothing licence-related in the chain to attest to (gap one).
2. **The signing key is customer-held.** `audit/keys.py` generates a random
   Ed25519 key on the customer's disk. A valid signature proves only "this
   install's key signed this" — the licensee can regenerate the entire chain and
   it verifies perfectly. §B11's plan (key *derived* from `licence_id +
   machine_id`) is strictly worse: both inputs are known to the install, so the
   key is reproducible by anyone who has the box.
3. The CSV export (`GET /api/admin/audit/export`) is filtered, row-capped
   (`_AUDIT_EXPORT_CAP`, with an `X-Audit-Export-Truncated` header) and unsigned
   as a whole. A subset is indistinguishable from a pruned chain.

The governing constraint: on an air-gapped install where the customer has root,
**no purely local scheme is tamper-proof.** Signatures alone cannot bind an
install to a history it wants to rewrite. What *is* achievable is tamper-evidence
plus **cross-report anchoring** — Funis retains a fingerprint of each report's
record set, and a later report has to *prove* it extends one of them, so a
rewound or rebuilt chain cannot produce the proof. The anchoring, not the
signature, is what carries the evidential weight.

*(As built, that fingerprint is a Merkle root and the proof is an RFC 6962
consistency proof; the original design compared chain heads directly. See §A2.)*

## Decision

### A. Trust model — Funis-issued key + anchoring

1. **Funis issues the report keypair.** The private half is delivered *inside*
   signed material (below); the app signs compliance reports with it. Replaces
   §B11's deterministic derivation, which is hereby rejected: an install that can
   derive its own signing key gives the licensee a forgery oracle. The existing
   per-install audit key keeps signing *audit records* unchanged.

2. **Every report proves it extends what Funis already holds.** A report carries
   a **Merkle tree** over the audit-record hashes (RFC 6962), an RFC 6962
   **consistency proof** from the tree size of every report the install has ever
   generated, the verify result over the full range, and a **monotonic report
   counter** persisted in the DB and written to the chain as its own event. Funis
   keeps a ledger of `(tree size, root)` received. The proof matching the size in
   that ledger demonstrates today's log is an append-only extension of the one
   recorded; a rewind, a rebuild or a deletion cannot produce one. **This is the
   mechanism that makes the report meaningful**, and it works even though the
   customer controls the box.

   > **Amended 2026-08-11.** Originally the report carried only the previous
   > head and Funis compared it against its ledger's latest entry. That
   > conflated two different things — *a report that was never delivered* and
   > *a history that was rewritten* — and failed on the first. Reports are
   > generated on request, so an install routinely produces ones that are never
   > sent (a preview, a failed send); every such install then verified
   > `CONTRADICTED` for ever, with no recovery path, and the failure was
   > indistinguishable from real tampering. Indexing by **tree size** rather
   > than by "the previous report" removes the conflation: the install proves
   > from every size it has attested and the vendor checks whichever one they
   > hold. Two consequences accepted deliberately: a *missing* proof is now
   > `INCONCLUSIVE` rather than a contradiction (only a **failed** proof
   > contradicts), and clone detection falls back to counter collision — see
   > §A5. Report schema → **v2**; `designs/examples/` regenerated.

3. **Install identity is a DB-persisted `install_id`, generated by the install
   itself at first boot**, and carried on every report — it survives container
   rebuilds and host moves, which a host fingerprint does not. Funis issues a
   licence *for* an id the install already has (`matofx licence request` →
   supplier → licence), so a second deployment mints a different id and a copied
   licence is refused there.

   > **Amended 2026-08-11.** Originally the id came *from* Funis, inside the
   > signed licence or identity file. That made it an identifier for the
   > **licence**, not the install: feed one licence to two deployments and both
   > legitimately claimed the same id, so binding a licence to an id proved
   > nothing. And because adoption only ever happened by hand, an install that
   > never ran `compliance adopt` had no identity at all and was exempt from
   > binding entirely — precisely what a second instance would do. Inverting it
   > (the node-locked model every CAE vendor uses) closes both, at the cost of
   > one enrolment round-trip that the setup-licence step was already paying.
   > Migration 0092; the Funis-supplied columns become nullable because an
   > install has an identity before any licence exists to describe it. Remaining
   > ceiling: root can copy the id into a second database by hand — deliberate,
   > undeniable, and caught by counter collision if both ever report. §B11's soft machine binding (`/etc/machine-id`,
   `MachineGuid`) is retained as an **optional licence field**, not as identity.
   A cloned deployment keeps the `install_id`, so the **report counter** is what
   catches the clone: both forks issue counter *N*, and whichever reaches Funis
   second replays a counter already in the ledger.

   > **Amended 2026-08-11.** Under the head-comparison scheme a clone was caught
   > by a diverging head — but so was every honest install that skipped a
   > delivery, so the signal could not be acted on. Counter collision is the
   > honest replacement: it fires only on a genuine duplicate, at the cost of
   > needing **both** forks to report. If only the clone ever reports, it is
   > indistinguishable from the original going quiet. That gap is not closable
   > in software on hardware the customer controls (§A4/§A5), and is why
   > enforcement is contractual.

4. **Honesty about the ceiling.** This is tamper-*evident* + rollback-*detectable*,
   not tamper-proof. A licensee who extracts the issued key, rebuilds the chain
   **and** keeps the proofs consistent across every report defeats it. TPM
   sealing (`matofx tpm enrol`, §C16) remains the future upgrade and is
   deliberately out of scope: enrolment cost, host-migration reissue and
   container-rebuild breakage are a poor fit for the Linux/SQLite/Slurm first
   client. Sales material must not overstate this.

5. **Enforcement is contractual; the software supplies evidence.** Added
   2026-08-11, and it is the frame the rest of §A sits inside. On air-gapped
   hardware the customer roots, no purely local scheme resists a determined
   licensee — the mechanisms that genuinely do are a hardware root of trust
   (TPM NV counter, dongle) or online attestation with a vendor-issued nonce,
   and the deployment profile rules both out for now.

   The industry answer for exactly this shape is **evidence-based, contractual
   enforcement**: IBM's ILMT, Oracle LMS, Flexera. The vendor does not prevent
   under-reporting cryptographically; it *requires* the tool to be run and the
   reports retained, and a customer who cannot produce consistent evidence
   defaults to the worst-case licence position. MATOFX should adopt the same
   posture, which changes what the software must be good at: not catching a
   determined attacker, but making **inconsistency impossible to avoid without
   deliberate, sustained, provable lying** — and never accusing an honest
   customer, because a false `CONTRADICTED` destroys the evidential value of a
   true one.

   Two upgrades kept on the shelf: RFC 3161 trusted timestamps (or
   OpenTimestamps) applied at Funis when a report arrives, which would close
   backdating and is legally recognised under eIDAS; and TPM-backed monotonic
   counters where the platform offers one.

### B. The compliance report

5. **Scope: licence attestation, not the audit log.** Customers will resist
   shipping raw audit records (usernames, project and job names, source IPs), and
   a refused report is worth nothing. The artefact carries:
   install/licence identity; the full-range verify result (including failing
   record ids verbatim, and when the install last verified itself outside this
   report); the Merkle root + consistency proofs; the report counter; **licence
   events**
   (installs, validation pass/fail, slot grant/deny, break-glass admissions, FAL
   assignment changes, expiry transitions); and aggregate
   counts (peak concurrency vs `n_slots`, assignments vs `n_slots`). No project
   or job data. The existing full-chain CSV export stays as a separate, explicitly
   opt-in admin action.

6. **Format: canonical JSON + detached signature, with a human-readable HTML
   twin.** The customer's admin can read exactly what they are about to send
   before sending it — which is what makes approval routine rather than a legal
   conversation. Canonicalisation rules are pinned in the ADR-adjacent spec and
   versioned (`report_schema_version`), like the audit hashing contract.

7. **Delivery: on-demand, pull-based, two surfaces.** `matofx compliance report
   --out <file>` and an **Admin → Licence & compliance** page button, both
   producing the same artefact, returned through the existing private per-customer
   channel (§C15). **No phone-home** (§B10): nothing is transmitted by the app.
   Generated reports are archived in the DB so a re-request is answerable.

8. **Funis verifies with `matofx compliance verify <file> --ledger <heads>`**,
   shipped in the product repo: checks the signature against the issued public
   key, verifies the consistency proof and counter continuity against the local
   ledger, and prints the breach summary and a `CONSISTENT` / `CONTRADICTED` /
   `INCONCLUSIVE` verdict. One codebase, one definition of the format.

   **`INCONCLUSIVE` is load-bearing, not a middle ground.** Anything the
   verifier cannot check — no ledger entry yet, a baseline that attested no
   records, a proof the report does not carry — lands there. Only a *failed*
   proof or a counter that goes backwards produces `CONTRADICTED`. The
   asymmetry is deliberate: a false accusation is worse than a missed one,
   because it destroys the evidential weight of every true accusation. If the
   vendor cannot tell a fraud from a customer who previewed a report, the
   verdict cannot be acted on at all.

9. **Report generation never fails closed.** A failing verify, a broken licence
   or a detected breach must still produce a report saying so — refusing to emit
   the artefact would destroy exactly the evidence being requested.

### C. Licence subsystem (§C16, unchanged decisions carried into build)

10. **Manifest, types, enforcement cadence** are as §C16 specifies: one signed
    JSON file, `licence_type` ∈ {`csl`, `fal`}, unlimited registered users,
    slot consumed at login (CSL) or at access-rights assignment (FAL), re-check
    at startup / Arq cron (default 60 min) / each login against cached state.
    `LicenceSettings` joins the `Settings` tree beside `AuditSettings`;
    `features.py` switches its authoritative source from `[features]` to the
    manifest, keeping TOML for dev/test.

11. **Invalid ≠ expired — split behaviour.** Bad signature, malformed manifest or
    machine-binding mismatch → **refuse to serve** (fail-closed; a deliberately
    edited licence is not an availability problem). Missing file, or expiry past
    grace → **read-only mode** with a loud banner and admin emails, never a hard
    stop (§C16). All four transitions are chain events and appear in the report.

12. **Read-only mode means:** every *registered* user may sign in and **view
    results only**. No new user registration, no access-rights or role changes,
    no job submission, no data edits, no admin settings changes. In-flight jobs
    run to completion. CSL slot accounting is **suspended** while read-only —
    there is no metered work to protect, and "all registered users may log in"
    cannot hold otherwise. This resolves §C16's internal contradiction
    ("users can log in and view" vs "new user logins blocked") in the customer's
    favour.

13. **Break-glass: Super Users are admitted over cap.** At `n_slots`, a Super
    User still gets in — otherwise an admin is locked out precisely when the
    licence needs fixing. The admission is its own chain event
    (`licence.slot.breakglass`) and the report shows peak concurrency **with the
    break-glass count broken out**, so an excess over `n_slots` is explained
    rather than hidden or silently permitted.

14. **A failed chain verification degrades the install to read-only** until a
    Super User **acknowledges** it (maintainer's call, against the
    recommendation). Three mitigations are part of the decision, not optional:
    verification at startup is **incremental** (head + a recent window; the full
    walk runs on the runner's hourly tick and on demand) so boot time doesn't
    scale with chain length; the acknowledgement is itself a chain event that
    appears in the next compliance report — so lifting it is visible to Funis,
    and a disk fault or a restore cannot permanently brick a customer; and the
    **attestation expires**.

    > **Amended 2026-08-11.** The incremental startup check was specified here
    > but never built — only the runner's hourly full walk existed. An install
    > whose runner was not running therefore never checked its chain *at all*,
    > and an API-only deployment could never enter integrity read-only. Now
    > built (`compliance.integrity.verify_recent`, called from the API
    > lifespan), so both processes attest and neither is a single point of
    > failure.
    >
    > Expiry is the second half, and is new. A recorded verdict says "the chain
    > was intact **when it was checked**"; treating that as permanent meant an
    > install whose checker died kept serving on an arbitrarily old verdict
    > with nothing to say so. Past `STALE_AFTER_HOURS` (3) administrators see a
    > banner naming the likely cause; past `UNVERIFIED_AFTER_HOURS` (24) the
    > site degrades exactly as a failure would, liftable by the same
    > acknowledgement so planned maintenance is never a brick. This is the
    > OCSP `nextUpdate` pattern — absence of checking becomes self-enforcing
    > rather than something a separate watchdog must detect. The report carries
    > `last_verified_at` / `verification_is_current`, so an install that has
    > stopped checking is visible to Funis too.

## Build order

Report first, so something vendor-facing exists before enforcement lands.

| # | Slice | Notes |
|---|-------|-------|
| 1 | ✅ **Shipped 2026-08-09** — compliance report + anchoring over today's chain | `install_id`, report counter, canonical JSON + HTML twin, `compliance adopt/report/verify`, head ledger. Admin page deferred to slice 6's UI work — CLI only for now |
| 2 | ✅ **Shipped 2026-08-10** — licence manifest: schema, verify, `matofx licence install/reload/show` | Report key now preferred from the manifest (identity file remains the fallback); report's `licence` block + cap are real; tampered → fail closed, missing/expired → read-only |
| 3 | ✅ **Shipped 2026-08-10** — startup gate + hourly re-check + `features.py` reads the manifest | Re-check lives in the runner supervisor, not Arq — that is how every other periodic task in this codebase already works. Fail-closed at startup only; the re-check never raises |
| 4 | ✅ **Shipped 2026-08-10** — CSL slots at login, break-glass, Active sessions page | Gate sits in the auth router (after auth, before session creation); 503 not 403; read-only + FAL are unmetered; releases on logout / idle / hard timeout / admin revoke |
| 5 | ✅ **Shipped 2026-08-10** — FAL assignment slots | Maps onto the existing `is_active` logon-access lifecycle (no new column). Granting metered (409 at the cap), revoking never gated; report's `source` flips to `fal_assignments` |
| 6 | ✅ **Shipped 2026-08-10** — grace → read-only mode + banners + integrity acknowledge | One middleware (fails closed) + migration 0090. Adds a **dev exemption**: an install with no `[licence].file` configured at all stays writable, and prod config now *requires* one |
| 7 | ✅ **Shipped 2026-08-10** — expiry / read-only transition events; `SeatsStats.cap` from the manifest | Transitions only, never per-tick. The cap carries `licence_type` because CSL caps peak concurrency and FAL caps enabled seats. `licence.override` documented as reserved — no such concept exists in this build. **Removed 2026-08-12**: a zero-filled count for a concept that cannot occur reads as an assurance rather than a placeholder |
| — | ✅ **Reworked 2026-08-11** — anchoring → RFC 6962 consistency proofs; startup integrity check actually built; attestations expire; CSL admission race closed | Report schema **v2**, migration **0091**, `compliance/merkle.py`. Driven by a stress-test pass, not new requirements — four of the defects it found were **false accusations of honest installs**, and one of them (§C14's startup check) was documented as shipped but had never been written. See ARCHITECTURE §16.56 |
| — | ✅ **Gap list closed 2026-08-12** — mid-year licence changes apply live; tampered licence fails closed at runtime; `licence.override` removed; proof window re-selected + `--prove-from`; break-glass time-boxed + licence alerting; container warning | Six overview gaps in one pass. Two deliberate non-changes recorded rather than silently skipped: break-glass stays **unlimited in number** (a count cap locks out the administrator §C13 exists to admit) and a missing proof stays `INCONCLUSIVE`. Migration **0093**. See ARCHITECTURE §16.61–16.64 |
| — | ✅ **Admin report page 2026-08-12** — `Admin → Licence & compliance`; list / generate / download both halves | Closes §B7's second surface, specified from the start and never built (overview gap 5). Gated on `audit.read`; **allow-listed in read-only mode**, because §B9 requires generation never to fail closed and read-only is triggered by the two conditions a supplier asks about. Also fixed a latent gap: an install with no licence yet had never minted its identity. See ARCHITECTURE §16.60 |
| — | ✅ **Ledger custody 2026-08-12** — `scripts/ledger.py` (`keygen`/`verify`/`list`/`backup`); ledger entries chained + Ed25519-signed; signed head marker | Closes the Consequences' "retaining the ledger per install" duty, which had tooling for neither integrity nor backup. The head marker exists because a hash chain cannot see a ledger **truncated at the end** — found by running the attacks, not by reasoning. Process: `designs/funis-vendor-runbook.md`. See ARCHITECTURE §16.59 |
| — | ✅ **Issuing tooling 2026-08-12** — `scripts/issue_licence.py` (`keygen`/`pubkeys`/`issue`/`show`) | Vendor-only by construction: a `matofx licence issue` subcommand would ship the ability to mint licences to every customer. Working **+ backup** issuer key, so a compromised key has a revocation path that does not require a new release. Running the round trip found two defects — an issue that crashed *after* writing the licence (inviting a duplicate re-issue), and §16.57's install-binding refusal arriving as a traceback. See ARCHITECTURE §16.58 |

**Slice 1 needs a key before any licence exists.** Funis issues a small
`funis-install-identity.json` (Ed25519-signed: `install_id`, customer,
`issued_at`, report keypair) out-of-band. Slice 2 folds the same fields into the
licence manifest; the loader prefers the manifest and the standalone file becomes
legacy. This keeps slice 1 genuinely vendor-verifiable on day one rather than
signing with a key the customer generated.

## Consequences

- Funis can request, receive and *check* a compliance report — with a real
  verdict — before the licence subsystem is finished.
- Funis takes on an operational duty it does not have today: **retaining the
  ledger per install** — `(counter, tree size, root)` per report received. Lose
  it and the anchoring evidence for that install is gone. It belongs in
  release-engineering's per-customer record (§C15/H41) alongside the delivery
  log. Note the second duty this creates: **record every report you receive**,
  because the ledger's newest entry is what later proofs are checked against.

  > **Discharged 2026-08-12.** The duty had no tooling for two months.
  > `scripts/ledger.py` now verifies, lists and backs up the ledger; entries are
  > chained and signed with a signed head marker (a chain alone cannot see a
  > ledger truncated at the end); and the procedure — issuing, receiving,
  > backups, key rotation, how to read each verdict — is written up in
  > `designs/funis-vendor-runbook.md`. See ARCHITECTURE §16.59.
- §B11's derived-key scheme is superseded (decision 1). The brief should be
  updated rather than left contradicting this ADR.
- New schema: `install_identity` (install_id, issued material, report counter)
  and `compliance_report` (archived artefacts) — migrations from head `0087`.
  Licence tables land with slice 2.
- New read-only write-gate touches every mutating API route; it is a middleware
  concern, not per-router edits, and must not gate reads or in-flight runners.
- The idle-logout path shipped 2026-08-09 (§16.47) becomes load-bearing: under
  CSL it is what returns a concurrent-session slot when a user walks away.
- Help docs: a new admin chapter (licence, read-only mode, generating a
  compliance report) and a user-facing note on read-only mode and "all sessions
  in use".
- Sales/positioning: the claim is "tamper-evident with vendor-held anchoring",
  never "tamper-proof". Overstating it is the one way this design fails a
  customer's security review. Per §A5 the enforcement story is **contractual**
  — the same posture as IBM ILMT and Oracle LMS: the software supplies
  evidence, and a customer who cannot produce consistent evidence defaults to
  the worst-case licence position.
- Reporting cadence is **one or two reports a year** (maintainer, 2026-08-11),
  not monthly. That is why anchoring had to become logarithmic rather than
  shipping the record hashes since the last report: across a year those are
  megabytes, while a consistency proof over a 90,000-record chain is ~17
  hashes.

## Amendments

### 2026-08-11 — anchoring rework, after a stress-test pass

A session spent attacking what slices 1–7 had built, rather than extending it.
The attack cases all behaved. **Every defect found was a false accusation of an
honest install**, which is the failure mode this design can least afford: a
verdict that fires on innocent customers cannot be acted on when it fires on a
guilty one. Implementation record: ARCHITECTURE §16.56.

| # | Defect | Resolution |
|---|---|---|
| F1 | A report generated and **never delivered** made every later report `CONTRADICTED`, permanently and unrecoverably (`--append` refuses to record a contradicted report, so the ledger could never advance again). Routine: a preview, a failed send. | §A2 — anchoring re-indexed to **tree size** via RFC 6962 consistency proofs |
| F2 | The first report on an empty audit chain recorded `head_id = 0`; the next report was told `record 0 no longer exists — history has been truncated`. A fresh install accused of deleting history on day one. | Ledger head is nullable; an empty baseline skips anchoring |
| F3 | Report 1's period ran **backwards** (`from > to`), so everything between adopting the identity and the first report was covered by no report at all. | Period starts at the earlier of the first record and `adopted_at`, clamped |
| §6.1 | CSL admission counted and inserted without a lock — five concurrent logins passed a three-slot cap. | Two-phase admission; loser chosen by **rank**, not re-count |
| §6.2 / F4 | Two read-only allow-list prefixes matched **no route at all**, silently pre-approving anything added beneath them. | Removed; a route-enumeration test now fails on any change |
| §6.3 | §C14's incremental startup check was **specified here but never built** — an install whose runner never ran checked its chain not at all. | Built (`verify_recent`, API lifespan) **and** attestations now expire |
| F5 | `licence show` printed the raw manifest, so it reported `(none)` while the install ran features, and advertised features it ignores. | Prints the **effective** set and explains any divergence |

**What this cost, deliberately.** Clone detection no longer rests on a diverging
head — a clone legitimately contains the shared history and passes anchoring. It
now rests on counter collision (§A3), which fires only on a genuine duplicate but
needs both forks to report. The old signal was strictly worse in practice: it
fired on clones *and* on every honest install that skipped a delivery, so it
could not be distinguished or acted on.

**The generalisable lesson**, worth applying beyond this ADR: the build tested
that attacks are caught, and every attack test passed. Nothing tested that
*honest* behaviour is **not** caught. For any check that accuses, the false-positive
case needs a test as deliberately as the true-positive one — and where they
produce identical output, that is itself the defect.

**Left open, not built.** RFC 3161 trusted timestamps applied at Funis on
receipt (closes backdating; legally recognised under eIDAS), and TPM-backed
monotonic counters where the platform offers one. Both are §A5 upgrades, neither
is required for the first client.
