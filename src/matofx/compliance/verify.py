"""Vendor-side verification of a returned compliance report (ADR-0019 §B8).

Six checks, of which three carry the weight:

===============  =========================================================
signature        the report was written by the install Funis issued a key to
counter          no report in the sequence was quietly dropped
**anchoring**    **the log provably extends the one previously attested**
genesis          the database was not wholesale replaced
chain            the install's own integrity walk passed
breaches         licence findings (not tampering — reported separately)
===============  =========================================================

The anchoring check is the one that cannot be defeated locally: it
verifies an RFC 6962 consistency proof against a Merkle root **Funis**
holds and the customer's install has no way to read. A rebuilt chain can
self-verify perfectly and still fail here — which is the whole point of
the design.

Anchoring is indexed by **tree size**, not by "the previous report". That
distinction is load-bearing: reports are generated on request, so an
install routinely produces reports that are never delivered (a preview, a
failed send). Keying the check to the last report the install generated
made every such install permanently CONTRADICTED — a fraud verdict on
honest customers, and one indistinguishable from the real thing. Keying
it to a size lets the vendor verify against whichever head they actually
recorded.

**A missing proof is never a contradiction.** Anything the verifier
cannot check degrades to ``INCONCLUSIVE``. Only a *failed* proof — a log
that demonstrably no longer extends what it attested — is
``CONTRADICTED``.

A breach (peak over cap) is *not* a contradiction. An install honestly
reporting that it exceeded its slot count is behaving correctly; that is
a commercial conversation, not a tampering one. Only integrity failures
produce ``CONTRADICTED``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from matofx.compliance.canonical import canonical_bytes, content_sha256
from matofx.compliance.errors import ReportFormatError
from matofx.compliance.ledger import LedgerEntry, known_public_key, latest_for
from matofx.compliance.merkle import proof_from_hex, verify_consistency
from matofx.compliance.schema import CSL, FAL, REPORT_SCHEMA_VERSION

CheckStatus = Literal["ok", "fail", "warn", "skip"]
Verdict = Literal["CONSISTENT", "CONTRADICTED", "INCONCLUSIVE"]

CONSISTENT: Final[str] = "CONSISTENT"
CONTRADICTED: Final[str] = "CONTRADICTED"
INCONCLUSIVE: Final[str] = "INCONCLUSIVE"

#: Checks whose absence makes a verdict unsafe to call "consistent".
_LOAD_BEARING: Final[frozenset[str]] = frozenset({"signature", "anchoring"})


@dataclass(frozen=True)
class Check:
    """One verification step's outcome."""

    name: str
    status: CheckStatus
    detail: str


@dataclass(frozen=True)
class Finding:
    """A licence finding — over-cap, denials, break-glass use."""

    severity: Literal["breach", "signal"]
    summary: str


@dataclass(frozen=True)
class ReportVerdict:
    """The full outcome of checking one report."""

    verdict: Verdict
    install_id: str
    counter: int
    checks: list[Check] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    content_sha256: str = ""
    public_key: str | None = None

    @property
    def ok(self) -> bool:
        """True only for a fully-consistent report."""
        return self.verdict == CONSISTENT


def verify_report(
    document: dict[str, Any],
    *,
    ledger: list[LedgerEntry],
    public_key_hex: str | None = None,
) -> ReportVerdict:
    """Check a report against the ledger. Never raises on a *bad* report.

    Args:
        document: The parsed report JSON.
        ledger: Every entry Funis holds (all installs; filtered here).
        public_key_hex: Overrides the key pinned in the ledger — needed
            for report 1, where there is nothing pinned yet.

    Raises:
        ReportFormatError: The document isn't a MATOFX report at all, or
            declares a schema version this build can't verify. A report
            that is *structurally* fine but *fails* its checks returns a
            ``CONTRADICTED`` verdict instead — that is a result, not an
            error.
    """
    _require_shape(document)

    install = _mapping(document.get("install"))
    anchor = _mapping(document.get("anchor"))
    install_id = str(install.get("install_id") or "")
    counter = int(document.get("counter") or 0)
    previous = latest_for(ledger, install_id)
    pinned_key = public_key_hex or known_public_key(ledger, install_id)

    checks: list[Check] = [
        _check_signature(document, pinned_key),
        _check_counter(counter, previous),
        _check_anchoring(anchor, previous),
        _check_genesis(anchor, previous),
        _check_chain(_mapping(document.get("chain_verification"))),
        _check_issuer(install),
    ]
    findings = _evaluate_usage(_mapping(document.get("usage")), _mapping(document.get("licence")))

    return ReportVerdict(
        verdict=_verdict(checks),
        install_id=install_id,
        counter=counter,
        checks=checks,
        findings=findings,
        content_sha256=content_sha256(document),
        public_key=pinned_key,
    )


def _require_shape(document: dict[str, Any]) -> None:
    version = document.get("report_schema_version")
    if version is None or "install" not in document or "anchor" not in document:
        raise ReportFormatError(
            "This file is not a MATOFX compliance report (no report_schema_version / "
            "install / anchor). Check you were sent the .json artefact, not the .html twin."
        )
    if int(version) > REPORT_SCHEMA_VERSION:
        raise ReportFormatError(
            f"Report declares schema version {version}, but this build understands up to "
            f"{REPORT_SCHEMA_VERSION}. Upgrade the verifier before checking this report — "
            "verifying it with an older recipe could pass a document you cannot fully read."
        )


def _check_signature(document: dict[str, Any], public_key_hex: str | None) -> Check:
    signature = _mapping(document.get("signature"))
    value = str(signature.get("value") or "")
    key_id = str(signature.get("key_id") or "")
    if not value:
        return Check("signature", "fail", "the report carries no signature")
    if public_key_hex is None:
        return Check(
            "signature",
            "skip",
            f"no public key pinned for this install (key_id {key_id or '?'}) — "
            "pass --public-key for the first report",
        )
    try:
        public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        public.verify(bytes.fromhex(value), canonical_bytes(document))
    except (ValueError, InvalidSignature):
        return Check(
            "signature",
            "fail",
            f"signature does not verify under the pinned key (report claims key_id {key_id})",
        )
    return Check("signature", "ok", f"ed25519, key {key_id}")


def _check_counter(counter: int, previous: LedgerEntry | None) -> Check:
    if previous is None:
        return Check("counter", "ok", f"{counter} — first report on record for this install")
    expected = previous.counter + 1
    if counter == expected:
        return Check("counter", "ok", f"{counter} follows {previous.counter} — no gap")
    if counter <= previous.counter:
        return Check(
            "counter",
            "fail",
            f"{counter} does not advance past {previous.counter} — the counter has gone "
            "backwards. Three things do that: the same report was sent twice; the "
            f"database was restored from a backup taken before report {previous.counter}; "
            "or two installs are running from the same identity (a clone). A clone is the "
            "one to rule out first — it is the only one that is not an accident.",
        )
    return Check(
        "counter",
        "warn",
        f"{counter} skips {expected}..{counter - 1} — {counter - expected} report(s) in "
        "the sequence were never received. Reports are generated on request, so this is "
        "usually a report that was produced and not sent; it is a question, not a finding.",
    )


def _check_anchoring(  # noqa: PLR0911 — one return per distinct verdict, each with its own message.
    anchor: dict[str, Any], previous: LedgerEntry | None
) -> Check:
    """Verify an RFC 6962 consistency proof from the size the ledger holds.

    The question is *"is the log I'm looking at an append-only extension
    of the one I recorded?"* — indexed by tree size, so it is unaffected
    by reports the install generated and never delivered. Deciding it by
    comparing "the install's previous report" against "the vendor's
    latest entry" (as v1 did) conflated a missing delivery with a
    rewritten history and accused honest installs of the latter.
    """
    if previous is None:
        return Check(
            "anchoring",
            "skip",
            "nothing previously recorded — this report establishes the baseline",
        )
    if previous.tree_size <= 0 or not previous.tree_root:
        # The recorded report attested an empty chain (or predates the
        # tree). There is genuinely nothing it committed to, so there is
        # nothing this report can contradict.
        return Check(
            "anchoring",
            "skip",
            f"the recorded report (counter {previous.counter}) attested an empty record "
            "set — this report establishes the baseline",
        )

    tree = _mapping(anchor.get("tree"))
    size = _as_int(tree.get("size"))
    root_hex = str(tree.get("root") or "")
    if not size or not root_hex:
        return Check(
            "anchoring",
            "skip",
            "the report carries no Merkle tree — it cannot be anchored (schema too old?)",
        )

    if size < previous.tree_size:
        return Check(
            "anchoring",
            "fail",
            f"the record set has shrunk: {previous.tree_size} records were attested at "
            f"counter {previous.counter}, this report attests {size}. Records cannot be "
            "removed from an append-only log — this is a restore from an older backup, a "
            "rolled-back database, or a deletion.",
        )

    entry = _find_proof(anchor, previous.tree_size)
    if entry is None:
        return Check(
            "anchoring",
            "skip",
            f"this report carries no proof from the {previous.tree_size}-record tree "
            f"recorded at counter {previous.counter}, so it cannot be anchored. Ask the "
            "customer to run 'matofx compliance report --prove-from "
            f"{previous.tree_size}', which anchors to exactly this entry — or "
            "re-baseline deliberately.",
        )
    if entry.get("proof") is None:
        return Check(
            "anchoring",
            "fail",
            f"the install states it cannot prove consistency from the {previous.tree_size}-"
            f"record tree recorded at counter {previous.counter} — its log no longer "
            "extends the one previously attested.",
        )

    proof = proof_from_hex([str(node) for node in entry.get("proof") or []])
    root_now = _as_bytes(root_hex)
    root_then = _as_bytes(previous.tree_root)
    if proof is None or root_now is None or root_then is None:
        return Check(
            "anchoring",
            "fail",
            "the consistency proof is malformed and cannot be checked",
        )

    if not verify_consistency(
        m=previous.tree_size, root_m=root_then, n=size, root_n=root_now, proof=proof
    ):
        return Check(
            "anchoring",
            "fail",
            f"the consistency proof from the {previous.tree_size}-record tree recorded at "
            f"counter {previous.counter} does not verify — the log has been rewritten, not "
            "merely extended. Records attested previously are no longer present unchanged.",
        )

    grew = size - previous.tree_size
    return Check(
        "anchoring",
        "ok",
        f"proved append-only from the {previous.tree_size}-record tree recorded at counter "
        f"{previous.counter} (root {previous.tree_root[:12]}…); extended by {grew} record(s)",
    )


def _find_proof(anchor: dict[str, Any], tree_size: int) -> dict[str, Any] | None:
    """The consistency-proof entry for ``tree_size``, if the report carries one."""
    raw = anchor.get("consistency_proofs")
    if not isinstance(raw, list):
        return None
    for item in raw:
        entry = _mapping(item)
        if _as_int(entry.get("tree_size")) == tree_size:
            return entry
    return None


def _as_int(value: object) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


_SHA256_BYTES: Final[int] = 32


def _as_bytes(value: str) -> bytes | None:
    try:
        raw = bytes.fromhex(value)
    except (TypeError, ValueError):
        return None
    return raw if len(raw) == _SHA256_BYTES else None


def _check_genesis(anchor: dict[str, Any], previous: LedgerEntry | None) -> Check:
    genesis = _mapping(anchor.get("genesis_record"))
    current = genesis.get("hash")
    if previous is None or not previous.genesis_hash:
        return Check("genesis", "skip", "no genesis hash previously recorded")
    if current is None:
        return Check("genesis", "fail", "the chain has no first record — the log is empty")
    if str(current) != previous.genesis_hash:
        return Check(
            "genesis",
            "fail",
            f"record 1 hashes {str(current)[:12]}…, first seen {previous.genesis_hash[:12]}… — "
            "the database has been replaced wholesale",
        )
    return Check("genesis", "ok", "record 1 unchanged since the first report")


def _check_chain(chain: dict[str, Any]) -> Check:
    if not chain:
        return Check("chain", "fail", "the report contains no self-verification result")
    checked = int(chain.get("records_checked") or 0)
    if chain.get("ok") is True:
        return Check("chain", "ok", f"install self-verified {checked} records, 0 failures")
    failures = chain.get("failures")
    count = len(failures) if isinstance(failures, list) else 0
    first = ""
    if isinstance(failures, list) and failures:
        head = _mapping(failures[0])
        first = f" — first: record {head.get('record_id')}, {head.get('reason')}"
    return Check("chain", "fail", f"install self-verification FAILED ({count} failures){first}")


def _check_issuer(install: dict[str, Any]) -> Check:
    if install.get("issuer_verified") is True:
        return Check("issuer", "ok", f"identity issued under {install.get('issuer_key_id')}")
    return Check(
        "issuer",
        "warn",
        "the install could not verify its own identity file against a trusted Funis key "
        "— treat the install_id as self-asserted",
    )


def _evaluate_usage(usage: dict[str, Any], licence: dict[str, Any]) -> list[Finding]:
    """Licence findings for whichever type is authoritative."""
    findings: list[Finding] = []
    licence_type = usage.get("licence_type")
    if licence_type is None:
        state = licence.get("state") or "unknown"
        findings.append(
            Finding(
                "signal",
                f"no licence installed ({state}) - usage reported as observation only",
            )
        )
        return findings

    block = _mapping(usage.get(str(licence_type)))
    n_slots = block.get("n_slots")

    if licence_type == CSL:
        peak = int(block.get("peak_concurrent_sessions") or 0)
        breakglass = int(block.get("breakglass_admissions") or 0)
        denied = int(block.get("slots_denied") or 0)
        if block.get("over_cap") is True:
            # Break-glass admissions are Super Users let in over the cap
            # by design (ADR-0019 §C13). Say how much of the excess they
            # explain, so a legitimate admin rescue reads differently
            # from users quietly exceeding the licence.
            excess = peak - int(n_slots or 0)
            if breakglass >= excess:
                tail = f"{breakglass} break-glass admission(s) account for the excess"
            elif breakglass:
                tail = (
                    f"{breakglass} break-glass admission(s); "
                    f"{excess - breakglass} over cap unexplained"
                )
            else:
                tail = f"{excess} over cap, no break-glass admissions"
            findings.append(
                Finding(
                    "breach",
                    f"peak {peak} concurrent sessions against {n_slots} slots - {tail}",
                )
            )
        if denied:
            findings.append(
                Finding("signal", f"{denied} logins refused at the cap — the licence is binding")
            )
    elif licence_type == FAL:
        current = int(block.get("assignments_current") or 0)
        denied = int(block.get("assignments_denied") or 0)
        if block.get("over_cap") is True:
            findings.append(
                Finding("breach", f"{current} floating assignments against {n_slots} slots")
            )
        if denied:
            findings.append(Finding("signal", f"{denied} assignment attempts refused at the cap"))
        if block.get("source") == "enabled_accounts":
            findings.append(
                Finding(
                    "signal",
                    "assignment count is the enabled-accounts proxy (install predates "
                    "floating-right enforcement)",
                )
            )
    return findings


def _verdict(checks: list[Check]) -> Verdict:
    if any(c.status == "fail" for c in checks):
        return CONTRADICTED  # type: ignore[return-value]
    if any(c.status == "skip" and c.name in _LOAD_BEARING for c in checks):
        return INCONCLUSIVE  # type: ignore[return-value]
    return CONSISTENT  # type: ignore[return-value]


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
