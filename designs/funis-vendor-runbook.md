# Funis vendor runbook — issuing licences and keeping the ledger

**Internal. This describes what Funis does, not what a customer does.**
Nothing here ships. The customer-facing equivalents are the help
chapters *Your licence* and *Licence compliance reports*.

Orientation and the reasoning behind all of it:
[`licence-and-compliance-overview.md`](licence-and-compliance-overview.md).
To explain the system to somebody who does not want the internals — a
prospect's evaluator, a customer's security reviewer — hand them
[`licence-and-compliance-explained.md`](licence-and-compliance-explained.md).
The decisions: [ADR-0019](decisions/0019-licence-and-compliance-attestation.md).

This runbook exists because ADR-0019 hands Funis two duties it cannot
discharge from memory: **retain the head ledger for every install**, and
**record every report received**. Losing the ledger makes that install's
history unprovable, permanently — and the failure is silent until the
day somebody disputes a report.

---

## 0. What you are holding

Four things, and losing any of them costs something different.

| Artefact | Default location | Lose it and… |
|---|---|---|
| **Issuer keys** | `~/.matofx-funis/keys/funis-YYYY-NN.pem` | you cannot issue or renew any licence, for anybody |
| **Issuing registry** | `~/.matofx-funis/registry.json` | you no longer know which `install_id` or report key each customer was issued (recoverable from the issued licences, painfully) |
| **Head ledger** | wherever you keep it, per deployment | that install's anchoring evidence is gone permanently — **not recoverable** |
| **Ledger keys** | `~/.matofx-funis/ledger-keys/funis-ledger-YYYY-NN.pem` | you cannot sign new ledger entries; existing ones still verify |

Back up the **registry and the ledger together**. They are two halves of
one record: the ledger proves what was received, the registry says who it
was issued to. A backup of either alone answers half the question.

---

## 1. First-time setup

> **Done 2026-08-13.** All four keys are minted and in custody; the issuer
> public halves are baked into `compliance/issuer.py`. Where each key lives is
> recorded in [`funis-key-custody.md`](funis-key-custody.md) — read that before
> running anything below, because the tool-default `--out` / `--keys`
> directories are deliberately *not* what this deployment uses.
>
> This section is retained for a future rotation, and for whoever has to do
> this again after a compromise.

Once, ever. Both commands mint a **working** key and a **backup** key.

```bash
export MATOFX_ISSUER_PASSPHRASE=...      # or let it prompt
python scripts/issue_licence.py keygen --out ~/.matofx-funis/keys

export MATOFX_LEDGER_PASSPHRASE=...
python scripts/ledger.py keygen --out ~/.matofx-funis/ledger-keys
```

Then bake **both** issuer public halves into the shipped build:

```bash
python scripts/issue_licence.py pubkeys --keys ~/.matofx-funis/keys
# paste into matofx/src/matofx/compliance/issuer.py
```

Three rules that are not negotiable:

- **The issuing key lives offline.** It signs a handful of times a year.
- **The ledger key does not live on the machine that stores the ledger.**
  A key sitting beside the file it protects protects it from nothing.
- **Both backup keys go somewhere separate and stay unused.** The backup
  is the *only* revocation path: because `TRUSTED_ISSUER_KEYS` is a map
  keyed by id, a compromised working key can be replaced by re-issuing
  under the backup without shipping a new release to every customer in
  the field. Without a spare there is no path at all.

---

## 2. Issuing a licence

The install names itself first — Funis issues *for* an id the install
already has. This is the node-locked model, and it is what stops one
licence file running two deployments.

**The customer runs:**

```
matofx licence request
```

and sends you the `inst_…` id it prints.

**You run:**

```bash
python scripts/issue_licence.py issue \
  --customer "Northwind Aerospace Ltd" \
  --install-id inst_PZE4SMMWC8S7 \
  --slots 10 --type csl \
  --key ~/.matofx-funis/keys/funis-2026-01.pem \
  --out NORTHWIND-AEROSPACE-0001.json
```

Send them the `.json`; they run `matofx licence install <file>`.

**Every later licence for that customer must carry the same
`install_id`.** The registry remembers it and the tool refuses a
different one unless you pass `--new-install`. If you get this wrong the
customer's site refuses to start at go-live — which is the correct
behaviour, and an appalling time to discover it.

### Machine binding — when not to use it

`--machine-binding` ties a licence to an OS machine id as well as to the
install. **Do not use it for a customer running MATOFX in containers**:
the id changes on every rebuild, so the licence dies at their next
deployment. The customer's install warns them if they install one on
something that looks containerised, but the better fix is not issuing it.

Binding buys little in any case — `install_id` already stops a second
deployment, and it is the thing that survives VM moves and host
migrations. Reach for machine binding only when a customer explicitly
asks to pin a licence to one physical box.

### Renewals and reissues

Same command, same `--install-id`. The registry reuses the customer's
**report key** automatically; do not override it. The head ledger pins a
report public key per install, so rotating it at renewal would make every
later report fail its signature check against the pinned key.

### When a customer genuinely rebuilds their deployment

A rebuild against an empty database mints a **new** `install_id` and
restarts the report counter at 1. Pass `--new-install`, and expect the
counter reset — it is not a clone signal. Note the date, because the old
install's ledger entries stop at whatever counter it reached and the two
histories cannot be joined.

---

## 3. Receiving a report

Reports arrive one or two times a year, by whatever private channel the
customer's contract specifies. **Verify, then record — always both.**

```bash
export MATOFX_LEDGER_PASSPHRASE=...
matofx compliance verify report.json \
  --ledger ~/funis/ledgers/northwind.jsonl \
  --append \
  --ledger-key ~/.matofx-funis/ledger-keys/funis-ledger-2026-01.pem
```

**The first report for an install needs `--public-key <hex>`** — take it
from the registry. Without it the ledger records no report key, the pin
never happens, and every later report *skips* its signature check rather
than performing it. That never produces a false `CONSISTENT` (it lands
`INCONCLUSIVE` by design), but the check silently never runs.
`ledger.py list` warns when an install has no key pinned.

### Reading the verdict

| Verdict | Means | Do |
|---|---|---|
| `CONSISTENT` | proofs check out against your ledger | record it, move on |
| `INCONCLUSIVE` | something could not be checked — no ledger entry yet, an empty baseline, a proof the report does not carry | ask for the missing report; **do not treat as a finding** |
| `CONTRADICTED` | a proof *failed*, or the counter went backwards | investigate before contacting the customer — see below |

`INCONCLUSIVE` is load-bearing, not a middle ground. The asymmetry is
deliberate: a false accusation destroys the evidential weight of every
true one. If you cannot tell a fraud from a customer who previewed a
report, the verdict cannot be acted on at all.

**Before treating `CONTRADICTED` as a finding**, rule out the innocent
causes in this order: a database restored from backup; the same report
sent twice; a rebuilt deployment reported under a new identity. Only
then consider a clone. Enforcement is contractual — you are producing
evidence for a conversation, not an automated judgement.

**A gap in the counters is a question, not a finding.** Reports are
generated on request, so an install routinely produces ones that are
never sent.

**If a report carries no proof from the size you hold**, the install has
generated more reports than its proof window covers and your entry fell
in a thinned gap. Ask the customer to run:

```
matofx compliance report --prove-from <the tree size in your ledger>
```

That anchors a fresh report to exactly your entry. It is a request with
an answer, not a finding — the verifier's own message names the command.

---

## 4. Keeping the ledger

### Every append, automatically

Each entry is chained to the one before it (`prev_hash`) and signed, and
a sidecar `….jsonl.head` records the signed length. You do not maintain
these; `--append` does.

### Scheduled — verify and back up

```bash
python scripts/ledger.py backup \
  --ledger ~/funis/ledgers/northwind.jsonl \
  --to /secure/backups/matofx-ledgers \
  --public-key funis-ledger-2026-01=<hex> \
  --public-key funis-ledger-2026-02=<hex>
```

**Prefer `--public-key` over `--keys` here.** Verification needs only public
halves, so passing the recorded hexes keeps the private ledger key off the
machine that stores the ledger — which is the whole of rule 2. Get the hexes
once with `ledger.py pubkeys --keys <where the keys are>` and record them;
they are public. Passing `--keys` instead still works and prompts for the
passphrase.

`backup` **verifies first and refuses to copy a ledger that fails**, so a
scheduled backup doubles as a scheduled integrity check: the day it starts
failing is the day you still have an older good copy. It writes a
timestamped copy, the head marker, and a `.sha256` sidecar.

Run it on a schedule — monthly is ample at one or two reports a year —
and **back up `~/.matofx-funis/registry.json` in the same job**.

### If verification fails

```
python scripts/ledger.py verify --ledger <file> --keys ~/.matofx-funis/ledger-keys --verbose
```

| Message | Almost always means |
|---|---|
| *"N entries SHORT of the head marker"* | truncated, partially written, or restored from an older copy |
| *"holds N entries but the marker records only M"* | two copies of the ledger were merged, or something appended without the tooling |
| *"signature does not match"* | that line was edited after it was signed |
| *"chain break"* | a line was inserted, removed or reordered |
| *"no head marker"* | a ledger predating the marker, or the sidecar was lost. Not a failure — but restore it if you have a backup |

**Do not overwrite your backups while investigating.** An older backup
may be the last intact copy, and `backup --force` exists only for
deliberately snapshotting the damage.

---

## 5. Rotating a compromised key

**Issuer key.** Issue under the backup key (`--key …-02.pem`). Existing
licences keep verifying under the old id, because the trusted map is
keyed by id and old keys stay trusted. Mint a fresh pair, bake both
public halves into the next release, and treat the compromised id as
retired.

**Ledger key.** Sign new entries with the backup key. Old entries stay
valid — verification looks up the key per entry, so a ledger signed under
two keys over its life is normal and verifies cleanly. Keep the retired
public key available or old lines will report *"unknown key"*.

---

## 6. The honest limits — say these plainly

Do not overstate any of this in a security review or a sales
conversation. Overstating it is the one way the design fails one.

- The system is **tamper-evident and rollback-detectable, never
  tamper-proof.** Someone who extracts the issued report key, rebuilds
  the chain *and* keeps every consistency proof aligned across every
  future report defeats it.
- **Ledger signing does not make the ledger unforgeable by Funis.**
  Whoever holds the key can re-sign a rewritten file. Keeping the key off
  the machine holding the ledger is an operational control, not a
  cryptographic one. What signing genuinely closes is *silent*
  corruption, and it is what lets the ledger stand up to somebody who
  does not simply take Funis's word for it.
- **Clone detection needs both copies to report.** It rests on counter
  collision. If only the clone ever reports, that is indistinguishable
  from the original going quiet.
- **The evidential weight is in the ledger you keep**, not in the
  signature.

Never write "tamper-proof".
