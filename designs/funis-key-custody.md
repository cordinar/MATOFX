# Funis key custody — where the four keys live

**Internal. Nothing here ships.** Companion to
[`funis-vendor-runbook.md`](funis-vendor-runbook.md), which says *what to do*;
this says *where the things are*. Decisions taken 2026-08-13
(`DEPLOYMENT_VALIDATION_PLAN.md` P-09 / L-04 / L-05 / P-10).

This document exists because the runbook's three non-negotiables are about
*location*, and a rule about location that nobody wrote down is a rule that
lasts until the next time you're in a hurry.

> **Status: keys minted 2026-08-13** and moved to their drives. The As-built
> section below is partly filled in — the remaining blanks are physical facts
> only Robert can supply (drive labels, who holds the passphrases). Fill them
> in: a custody document with gaps reads as a record of the whole, and the
> gaps are exactly what somebody needs at 2am.

---

## The decisions

| Artefact | Lives on | Why there |
|---|---|---|
| **Issuer working key** (`funis-2026-01.pem`) | **USB-A**, encrypted, kept with the working set | Signs a handful of times a year. Offline between uses, per runbook rule 1. |
| **Issuer backup key** (`funis-2026-02.pem`) | **USB-B**, encrypted, **different physical location** | The *only* revocation path. Separate location so one fire, theft, or failed stick cannot take both. |
| **Ledger working key** (`funis-ledger-2026-01.pem`) | **USB-A** | Runbook rule 2: not on the machine that stores the ledger. The ledger lives on the dev box; the key does not. |
| **Ledger backup key** (`funis-ledger-2026-02.pem`) | **USB-B** | Same reasoning as the issuer backup. |
| **Issuing registry** (`registry.json`) | Dev box, `~/.matofx-funis/` | Needs to be readable whenever a licence is issued. Backed up with the ledger — they are two halves of one record. |
| **Head ledgers** (`*.jsonl` + `.head`) | Dev box, `~/.matofx-funis/ledgers/` | Appended to whenever a report arrives. **No recovery path if lost.** |
| **Backups of registry + ledgers** | **Encrypted external drive**, scheduled | See *Routine* below. |
| **Both passphrases** | Written down, held by a person of trust | See *Passphrases*. |

Two passphrases, not one: the issuer key and the ledger key are separate
secrets, because they have opposite handling — one signs rarely and lives
offline, the other signs whenever a report arrives.

### Why USB-A holds one key of each pair rather than "all working keys together"

It does hold all working keys together; that is the point. The split that
matters is **working vs backup**, not issuer vs ledger. USB-A is the stick you
plug in to do the job; USB-B is the one you hope never to touch.

`~/.matofx-funis/keys/` and `~/.matofx-funis/ledger-keys/` — the tool
defaults — are deliberately **not used** for private keys here. They are
convenient and they are on the always-on machine, which is precisely what
rules 1 and 2 forbid. Pass `--out` / `--keys` explicitly, every time.

---

## Passphrases

**Decision:** both passphrases are written down and lodged with a person of
trust, so Funis can still issue and renew if the maintainer is unavailable.

This is the answer to L-05, and it is a real answer rather than a formality:
Funis is a one-person operation, and without it the loss of one person means
**no customer can ever be issued or renewed again**, including customers with
live contracts. The keys on USB-A are useless without the passphrase.

Practical points for whoever holds them:

- They need the **passphrases only** — not the keys. Passphrase without the
  USB is harmless; USB without the passphrase is harmless. That separation is
  what makes handing them to a non-technical person acceptable.
- Label them by purpose (`MATOFX issuer key` / `MATOFX ledger key`), not by
  value, so a future reader knows which is which.
- **Re-lodge them if either passphrase is ever changed.** A stale sealed
  envelope is the failure mode here, and nothing will warn you.

---

## Routine

### Whenever a report arrives (1–2 × per customer per year)

Needs **USB-A** plugged in — appending signs a new ledger entry, which needs
the private ledger key.

```bash
export MATOFX_LEDGER_PASSPHRASE=...
matofx compliance verify report.json \
  --ledger ~/.matofx-funis/ledgers/<customer>.jsonl \
  --append --ledger-key <USB-A>/matofx-funis/ledger-keys/funis-ledger-2026-01.pem
```

**The first report for any install also needs `--public-key <hex>`** from the
registry. Omit it and the report key is never pinned, so every later report
silently skips its signature check (runbook §3, gotcha G22).

### Monthly — verify and back up

Needs the **external backup drive**. Does **not** need USB-A, provided the
ledger public hexes were recorded at mint time (below) — verification uses
public halves only.

```bash
python scripts/ledger.py backup \
  --ledger ~/.matofx-funis/ledgers/<customer>.jsonl \
  --to <backup-drive>/matofx-ledgers \
  --public-key funis-ledger-2026-01=<hex> \
  --public-key funis-ledger-2026-02=<hex>
```

`backup` **verifies first and refuses to copy a ledger that fails**, so the
scheduled backup doubles as a scheduled integrity check. Back up
`~/.matofx-funis/registry.json` in the same job — the ledger says what was
received, the registry says who it was issued to, and either alone answers
half the question.

### Record the ledger public hexes

**Outstanding — do this next time USB-A is plugged in:**

```bash
python scripts/ledger.py pubkeys --keys <USB-A>/matofx-funis/ledger-keys
```

It prints both hexes already formatted as `--public-key` arguments. Paste them
into *As-built* below and into the monthly backup command.

They are **public** — safe to keep on the dev box — and recording them is what
lets routine verification run without plugging in USB-A. Without them every
monthly check drags the ledger key back onto the machine holding the ledger,
which is the exact thing rule 2 forbids. (`pubkeys`, and `--public-key` on
`backup`, were added 2026-08-13 for precisely this; before that there was no
way to obtain the public halves without decrypting the private keys.)

---

## If something is lost

| Lost | Consequence | Do |
|---|---|---|
| **USB-A** | Cannot issue, renew, or append. Nothing is compromised (keys are encrypted). | Restore from USB-B: re-issue under the *backup* key id, mint a fresh pair, bake both new public halves into the next release. Treat the lost id as retired. |
| **USB-B** | No revocation path left, and no spare. | Mint a fresh backup pair immediately and bake its public half into the next release. |
| **Both USBs** | Cannot issue or renew for anybody, ever, under the trusted ids. | New keys, new release, re-issue every live customer. Recoverable but expensive and visible. |
| **A passphrase** | Same as losing the corresponding USB — the key is unreadable. | Recover from the person of trust. This is the only path; there is no reset. |
| **Registry** | You no longer know which `install_id` each customer holds. Issuing a licence with the wrong one makes their site refuse to start. | Recover from backup. Painfully reconstructable from previously issued licence files. |
| **A head ledger** | **That install's anchoring evidence is gone permanently.** | Nothing. This is the one artefact with no recovery path — which is why it is backed up monthly and why the backup verifies before it copies. |

Compromise (as distinct from loss) is a different procedure — runbook §5.

---

## As-built

- **Minted on:** 2026-08-13, straight onto USB-A (nothing was written to the
  dev box's internal disk at any point).
- **Issuer key ids:** `funis-2026-01` (working), `funis-2026-02` (backup).
- **Issuer public halves** — public, reproduced here so the registry and the
  build can be cross-checked without unlocking anything:
  - `funis-2026-01` = `7e79b143492f1b04e134e2aab720834c4f76dd3eaaa59d4a646956f5293c84a9`
  - `funis-2026-02` = `3acf1620928e628a2422bab6e5db4adc5a97dcdb88222eb82e086da9357cb3b7`
  - Both verified as well-formed, distinct Ed25519 public keys before use.
- **Ledger key ids:** `funis-ledger-2026-01` (working), `funis-ledger-2026-02`
  (backup).
- **Ledger public hexes:** ⚠ **still to record** — run `ledger.py pubkeys`
  (see *Routine* above). Until this is done, every monthly verify needs USB-A.
- **Baked into `compliance/issuer.py`:** yes, 2026-08-13. See the note under
  *Why the keys are in source* below.
- **USB-A label / location:** _(to fill in)_
- **USB-B label / location:** _(to fill in)_
- **Passphrases lodged with:** _(to fill in — name the person and the date)_
- **Backup drive label / location:** _(to fill in)_

## Why the keys are in source

`compliance/issuer.py` holds the two **public** halves directly, rather than
having a build step inject them. Three reasons, recorded because the file's own
docstring used to promise the opposite:

1. **The injection step never existed.** `scripts/build_bundle.sh` has no
   issuer-key hook and never had one. Editing source is the only mechanism
   there has ever been.
2. **A constant cannot silently fail to apply.** A build-time injection that
   no-ops ships an artefact trusting *nobody*, and the symptom appears at a
   customer's go-live, not in our CI. That is the exact failure this key
   material exists to prevent.
3. **There is no secrecy cost.** These are public halves. Nothing is protected
   by keeping them out of the repository.

The accepted consequence: `licence.loader.issuer_required` now returns `True`
for every build of this tree, so an unsigned licence is refused in development
too. Measured cost was one test, which was rewritten to assert the new
behaviour. Local work is unaffected in practice because an install with no
`[licence].file` is never degraded at all.
