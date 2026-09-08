# Worked compliance-report examples (ADR-0019)

Real artefacts, not mock-ups. Every file here was produced by
`matofx.compliance` against a migrated database and checked by the
Funis-side verifier — regenerate them with:

```powershell
matofx/.venv/Scripts/python.exe scripts/make_compliance_examples.py
```

They double as **reference test vectors**: anyone reimplementing the
format, or the vendor-side checker, can verify against these.

| File | What it is |
|---|---|
| `funis-install-identity.json` | The out-of-band material Funis issues per install: `install_id`, customer, and the Ed25519 report keypair, signed by the Funis issuer key. **Slice 1's bridge** — kept as the fallback for installs that predate their licence. |
| `matofx-licence.json` | **Slice 2** — the signed CSL licence: 10 concurrent sessions, 30-day grace, three features, and the same report keypair. This is now the preferred home for the signing key. Install with `matofx licence install`. |
| `compliance-report-1.json` / `.html` | The customer's **first** report — before any licence is installed. Establishes the baseline. |
| `compliance-report-2.json` / `.html` | An honest follow-up a month later, under a **CSL** licence, with peak concurrency over the cap. |
| `compliance-report-3-tampered.json` | The same install after **rewriting its own chain**. Correctly signed, self-verifies perfectly, and provably not the same history. |
| `funis-heads.jsonl` | The vendor-side ledger after reports 1 and 2 — the state that makes report 3 detectable. |
| `verify-transcripts.txt` | Exactly what `matofx compliance verify` prints for all three. |

## What the three reports demonstrate

**Report 1 → `INCONCLUSIVE`.** With nothing previously recorded, the
anchoring and (without `--public-key`) signature checks are skipped. A
first report asserts; it cannot yet prove. The scheme gets stronger with
every report filed, which is why the ledger should start at install
rather than when a dispute begins.

**Report 2 → `CONSISTENT`, with a breach.** Its `licence` block is real from
slice 2 onwards — verified from `matofx-licence.json`, not hand-built. An
RFC 6962 consistency proof shows the 463-record log is an append-only
extension of the 120-record tree the ledger holds — **in seven hashes**,
which is why this scales to a 90,000-record chain reported once a year.
Peak concurrency of 11 against 10 slots is flagged as a **breach** —
but the verdict stays `CONSISTENT`, because an install honestly
reporting that it exceeded its cap is behaving correctly. That is a
commercial conversation, not a tampering one. The single break-glass
admission (a Super User let in over the cap by design) accounts for the
excess, and the verifier says so.

**Report 3 → `CONTRADICTED`.** This is the case the whole design exists
for. Note what still passes:

- `signature OK` — correctly signed with the issued key
- `counter OK` — 3 follows 2, no gap

and what fails:

- `anchoring FAIL` — no consistency proof exists from the 463-record tree
  the ledger holds to this rebuilt log
- `genesis FAIL` — record 1 was replaced too

The install cannot produce a proof against a root it has no way to read.
Anchoring, not the signature, is what carries the evidential weight.

**What a `FAIL` here does *not* mean.** Anchoring is indexed by tree
size, not by "the previous report", so a report that was generated and
never delivered — a preview, a failed send — is invisible to it. Only the
counter notices, and it says so as a `WARN`. Anything the verifier cannot
check degrades to `INCONCLUSIVE`; only a *failed proof* contradicts.

## The keys in here are examples

`ISSUER_SEED` and `REPORT_SEED` in the generator are fixed so re-running
produces diff-able output. They are **example** keys in a design folder
and must never become real issuing material — production issuer keys are
hardware-backed (§C16) and the trusted map in
`matofx/compliance/issuer.py` is populated at release-build time.
