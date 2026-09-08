"""The Funis-side head ledger (ADR-0019 §A2).

One append-only JSONL file, one line per report ever received, per
install. It is the only piece of state that makes anchoring work: the
customer's install cannot know what Funis wrote down, so a rewound or
rebuilt chain contradicts a value it cannot reach.

Losing this file loses the anchoring evidence for that install — the
ADR puts it in release-engineering's per-customer record alongside the
delivery log for exactly that reason.

``tree_size`` and ``tree_root`` are the load-bearing pair: anchoring is a
Merkle consistency proof *from the size the vendor last recorded*, so the
ledger must remember which tree it saw, not merely which head.

``head_id`` / ``head_hash`` are kept alongside for human legibility, and
are **nullable**: a report generated before the audit chain has any
records attests no head at all. Flattening that to ``0`` (as an earlier
version did) made the next report look like it had deleted record 0, and
a brand-new install was accused of truncating its history.

Line shape::

    {"install_id": "inst_7Q2M4KJ9", "counter": 7, "head_id": 84120,
     "head_hash": "a91f4c…", "tree_size": 84120, "tree_root": "b72e01…",
     "genesis_hash": "0af31c…", "report_public_key": "…",
     "generated_at": "…", "received_at": "…", "content_sha256": "…",
     "prev_hash": "…", "ledger_key_id": "funis-ledger-2026-01",
     "signature": "…"}

The ledger protects everyone else's evidence but had nothing protecting
**it**: a plain JSONL file, appended to by hand, where a truncation or a
half-written line was indistinguishable from a short history. So each
entry is **chained** to the one before it (``prev_hash``, exactly the
audit chain's idiom) and **signed** with a Funis ledger key.

Chaining is what detects the realistic failure — accidental loss,
truncation, a botched merge of two copies. Signing raises the bar on the
deliberate case, and matters most where the ledger is shown to somebody
who does not simply take Funis's word for it: an auditor, or a court, to
whom "Funis could have edited this afterwards" is a fair objection.

**Its honest limit, in the same register as the rest of ADR-0019:**
whoever holds the ledger key can re-sign a rewritten ledger. Separating
the ledger key from the issuing key and keeping it off the machine that
stores the ledger is an operational control, not a cryptographic one.
What this closes is *silent* corruption; what it cannot close is Funis
deliberately lying about its own records.

Legacy unsigned lines stay readable — a ledger written before this
existed is still evidence, and refusing to read it would destroy the
very history being protected. They verify as ``unsigned``, which is a
warning rather than a failure. The one thing refused is a **downgrade**:
once a ledger contains signed entries, an unsigned append is rejected,
because otherwise anyone could sidestep signing by simply not passing a
key.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Final, Protocol

from matofx.compliance.canonical import canonical_bytes
from matofx.compliance.errors import LedgerError

_REQUIRED = ("install_id", "counter", "head_id", "head_hash")

LEDGER_GENESIS_HASH: Final[str] = "0" * 64
"""``prev_hash`` of the first entry — the audit chain's convention."""


class LedgerSigner(Protocol):
    """Anything that can sign a ledger entry as Funis.

    The same narrow shape as the issuing script's signer, so one hardware
    token can serve both without either module knowing how it works.
    """

    key_id: str

    def sign(self, payload: bytes) -> bytes:
        """Return the Ed25519 signature over ``payload``."""
        ...


@dataclass(frozen=True)
class LedgerEntry:
    """One received report, as recorded by Funis."""

    install_id: str
    counter: int
    # None when the report attested an empty audit chain — a real state
    # for a freshly commissioned install, and distinct from "head 0".
    head_id: int | None
    head_hash: str | None
    tree_size: int = 0
    tree_root: str | None = None
    genesis_hash: str | None = None
    report_public_key: str | None = None
    generated_at: str | None = None
    received_at: str | None = None
    content_sha256: str | None = None
    # Chain + signature. All three are None on a legacy line, which is
    # readable but unverifiable — see the module docstring.
    prev_hash: str | None = None
    ledger_key_id: str | None = None
    signature: str | None = None

    def to_json_line(self) -> str:
        """Serialise as one JSONL line (stable key order)."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def signing_payload(self) -> bytes:
        """The bytes this entry's ``signature`` covers.

        Everything except the signature itself — including ``prev_hash``,
        so a signature cannot be lifted onto an entry at a different
        position in the chain. Uses the report canonicaliser rather than
        a second recipe: one canonicalisation idiom in the codebase.
        """
        return canonical_bytes(asdict(self))

    def entry_hash(self) -> str:
        """SHA-256 over :meth:`signing_payload` — what the next entry chains to."""
        return hashlib.sha256(self.signing_payload()).hexdigest()

    @property
    def is_signed(self) -> bool:
        """True when this entry carries both a key id and a signature."""
        return bool(self.signature and self.ledger_key_id)


def read_ledger(path: Path) -> list[LedgerEntry]:
    """Read every entry. A missing file is an empty ledger, not an error.

    A *malformed* line is an error: silently skipping it could hide the
    very entry that would have contradicted the report being checked.
    """
    if not path.exists():
        return []
    entries: list[LedgerEntry] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LedgerError(f"Could not read ledger {path}: {exc}") from exc

    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"{path}:{lineno} is not valid JSON: {exc}") from exc
        missing = [f for f in _REQUIRED if f not in record]
        if missing:
            raise LedgerError(f"{path}:{lineno} is missing field(s): {', '.join(missing)}")
        entries.append(
            LedgerEntry(
                install_id=str(record["install_id"]),
                counter=int(record["counter"]),
                head_id=_opt_int(record["head_id"]),
                head_hash=_opt_str(record["head_hash"]),
                tree_size=_as_int(record.get("tree_size")),
                tree_root=_opt_str(record.get("tree_root")),
                genesis_hash=_opt_str(record.get("genesis_hash")),
                report_public_key=_opt_str(record.get("report_public_key")),
                generated_at=_opt_str(record.get("generated_at")),
                received_at=_opt_str(record.get("received_at")),
                content_sha256=_opt_str(record.get("content_sha256")),
                prev_hash=_opt_str(record.get("prev_hash")),
                ledger_key_id=_opt_str(record.get("ledger_key_id")),
                signature=_opt_str(record.get("signature")),
            )
        )
    return entries


def latest_for(entries: list[LedgerEntry], install_id: str) -> LedgerEntry | None:
    """The highest-counter entry recorded for ``install_id``."""
    candidates = [e for e in entries if e.install_id == install_id]
    return max(candidates, key=lambda e: e.counter) if candidates else None


def known_public_key(entries: list[LedgerEntry], install_id: str) -> str | None:
    """The most recent report key recorded for ``install_id``, if any.

    Pinning the key from the ledger is what stops a customer swapping in
    a key of their own: report N+1 must be signed by the key report N
    was, unless Funis deliberately re-issued and re-pinned it.
    """
    for entry in sorted(
        (e for e in entries if e.install_id == install_id),
        key=lambda e: e.counter,
        reverse=True,
    ):
        if entry.report_public_key:
            return entry.report_public_key
    return None


def append_entry(
    path: Path,
    entry: LedgerEntry,
    *,
    signer: LedgerSigner | None = None,
) -> LedgerEntry:
    """Append one entry, chaining and signing it. Returns what was written.

    ``prev_hash`` is derived from the file's current last line, so the
    caller cannot get the chain wrong by supplying it. Passing no
    ``signer`` writes an unsigned entry, which is refused if the ledger
    already contains signed ones — a ledger must never silently
    downgrade, or signing is optional in practice.
    """
    existing = read_ledger(path)
    if signer is None and any(e.is_signed for e in existing):
        raise LedgerError(
            f"{path} contains signed entries, so this one must be signed too. "
            "Pass the ledger signing key; appending unsigned would leave a gap "
            "in the chain that no later verification could close."
        )

    chained = replace(
        entry,
        prev_hash=existing[-1].entry_hash() if existing else LEDGER_GENESIS_HASH,
        ledger_key_id=signer.key_id if signer else None,
        signature=None,
    )
    if signer is not None:
        chained = replace(chained, signature=signer.sign(chained.signing_payload()).hex())

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(chained.to_json_line() + "\n")
    except OSError as exc:
        raise LedgerError(f"Could not append to ledger {path}: {exc}") from exc

    # After the line is safely on disk, never before: a marker claiming a
    # length the file does not have would report corruption on every
    # subsequent verification.
    write_head_marker(path, [*existing, chained], signer=signer)
    return chained


@dataclass(frozen=True)
class HeadMarker:
    """How long the ledger is, signed — the answer to tail truncation.

    A hash chain detects an edited, reordered or deleted *interior* line,
    because every later line's ``prev_hash`` stops matching. It cannot
    detect a ledger cut short at the end: the remaining prefix is a
    perfectly valid chain, one report shorter. That is both the most
    likely accident (a partial copy, a truncated write, a restore from
    an older snapshot) and the most useful deliberate edit — drop the
    report you would rather not have received.

    So the length is recorded out of band, in a sidecar next to the
    ledger, and signed. Verification compares the chain it actually read
    against what the marker says it should have read.

    The sidecar can of course be deleted along with the truncation. It
    is not a cryptographic guarantee against a determined edit — nothing
    kept beside the file it protects can be. What it does is convert a
    silent loss into a loud one, which is the difference between finding
    out now and finding out when a customer disputes a report in a
    year's time.
    """

    entries: int
    last_entry_hash: str
    ledger_key_id: str | None = None
    signature: str | None = None
    updated_at: str | None = None

    def signing_payload(self) -> bytes:
        """The bytes the marker's signature covers."""
        return canonical_bytes(asdict(self))

    def to_json(self) -> str:
        """Serialise for the sidecar file."""
        return json.dumps(asdict(self), sort_keys=True, indent=2) + "\n"


def head_marker_path(ledger_path: Path) -> Path:
    """The sidecar path for ``ledger_path`` (``<name>.jsonl.head``)."""
    return ledger_path.with_suffix(ledger_path.suffix + ".head")


def read_head_marker(ledger_path: Path) -> HeadMarker | None:
    """Read the sidecar, or ``None`` if there isn't one."""
    path = head_marker_path(ledger_path)
    if not path.exists():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LedgerError(f"Could not read head marker {path}: {exc}") from exc
    return HeadMarker(
        entries=int(record.get("entries", 0)),
        last_entry_hash=str(record.get("last_entry_hash", "")),
        ledger_key_id=_opt_str(record.get("ledger_key_id")),
        signature=_opt_str(record.get("signature")),
        updated_at=_opt_str(record.get("updated_at")),
    )


def write_head_marker(
    ledger_path: Path,
    entries: list[LedgerEntry],
    *,
    signer: LedgerSigner | None = None,
) -> HeadMarker:
    """Rewrite the sidecar to match ``entries``."""
    marker = HeadMarker(
        entries=len(entries),
        last_entry_hash=entries[-1].entry_hash() if entries else LEDGER_GENESIS_HASH,
        ledger_key_id=signer.key_id if signer else None,
        updated_at=_dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds").replace(
            "+00:00", "Z"
        ),
    )
    if signer is not None:
        marker = replace(marker, signature=signer.sign(marker.signing_payload()).hex())
    path = head_marker_path(ledger_path)
    try:
        path.write_text(marker.to_json(), encoding="utf-8")
    except OSError as exc:
        raise LedgerError(f"Could not write head marker {path}: {exc}") from exc
    return marker


@dataclass(frozen=True)
class LedgerLineResult:
    """How one line fared: ``ok``, ``unsigned``, or ``fail``."""

    lineno: int
    install_id: str
    counter: int
    status: str
    detail: str


@dataclass(frozen=True)
class LedgerVerification:
    """The whole-file result. ``ok`` is false if any line failed."""

    ok: bool
    lines: list[LedgerLineResult]
    signed: int
    unsigned: int
    failed: int
    head_status: str = "absent"
    head_detail: str = "no head marker — tail truncation cannot be detected"

    @property
    def summary(self) -> str:
        """One line an operator can act on."""
        if self.failed:
            return f"{self.failed} line(s) FAILED — this ledger cannot be relied on"
        if self.head_status == "fail":
            return f"chain intact but {self.head_detail}"
        if self.unsigned:
            return f"intact; {self.signed} signed, {self.unsigned} unsigned (legacy)"
        return f"intact; all {self.signed} entries signed"


def verify_ledger(
    entries: list[LedgerEntry],
    *,
    public_keys: dict[str, str] | None = None,
    marker: HeadMarker | None = None,
) -> LedgerVerification:
    """Recompute the chain and check every signature.

    Two independent checks per line, because they catch different things:
    the **chain** catches truncation, reordering and a line edited in
    place; the **signature** catches a line rewritten *and* rechained by
    someone without the key. A line can fail either.

    An entry whose ``ledger_key_id`` is not in ``public_keys`` is
    reported as a failure, not skipped — a signature nobody can check is
    not evidence, and quietly passing it would be exactly the false
    reassurance this file exists to prevent.
    """
    keys = public_keys or {}
    lines: list[LedgerLineResult] = []
    expected_prev = LEDGER_GENESIS_HASH
    signed = unsigned = failed = 0

    for lineno, entry in enumerate(entries, start=1):
        problems: list[str] = []

        # A legacy line has no prev_hash at all; don't accuse it of
        # breaking a chain that did not exist when it was written.
        if entry.prev_hash is not None and entry.prev_hash != expected_prev:
            problems.append(
                f"chain break: prev_hash {entry.prev_hash[:12]}… does not match "
                f"the previous entry ({expected_prev[:12]}…)"
            )

        if entry.is_signed:
            problems.extend(_signature_problems(entry, keys))

        if problems:
            failed += 1
            status, detail = "fail", "; ".join(problems)
        elif entry.is_signed:
            signed += 1
            status, detail = "ok", f"signed by {entry.ledger_key_id}"
        else:
            unsigned += 1
            status, detail = "unsigned", "written before ledger signing existed"

        lines.append(
            LedgerLineResult(
                lineno=lineno,
                install_id=entry.install_id,
                counter=entry.counter,
                status=status,
                detail=detail,
            )
        )
        expected_prev = entry.entry_hash()

    head_status, head_detail = _head_problems(entries, marker, keys, expected_prev)

    return LedgerVerification(
        ok=failed == 0 and head_status != "fail",
        lines=lines,
        signed=signed,
        unsigned=unsigned,
        failed=failed,
        head_status=head_status,
        head_detail=head_detail,
    )


def _head_problems(
    entries: list[LedgerEntry],
    marker: HeadMarker | None,
    keys: dict[str, str],
    actual_last_hash: str,
) -> tuple[str, str]:
    """Compare the chain actually read against the signed head marker."""
    if marker is None:
        # A signed entry cannot exist without a marker having been
        # written: `append_entry` writes one every time it signs. So on a
        # signed ledger a *missing* marker is not the legacy case — it
        # means somebody removed it, and removing it is precisely what
        # you would do before cutting entries off the end, since the
        # chain alone cannot see a truncation. Say so.
        if any(entry.is_signed for entry in entries):
            return (
                "fail",
                "the head marker is MISSING from a ledger whose entries are signed. "
                "It is written automatically alongside every signed append, so it "
                "cannot be absent by accident — and removing it is the one step that "
                "would let entries be cut off the end unnoticed. Restore this ledger "
                "and its marker from backup and compare.",
            )
        return (
            "absent",
            "no head marker — tail truncation cannot be detected. This ledger has no "
            "signed entries, so it predates marker-writing; one is written from the "
            "next signed append onwards.",
        )

    mismatch = _head_mismatch(len(entries), marker, actual_last_hash)
    if mismatch:
        return "fail", mismatch

    if marker.signature and marker.ledger_key_id:
        problems = _signature_problems_for(
            marker.ledger_key_id, marker.signature, marker.signing_payload(), keys
        )
        if problems:
            return "fail", f"head marker {problems[0]}"
        return "ok", f"head marker signed by {marker.ledger_key_id}, {marker.entries} entries"

    return "unsigned", f"head marker present but unsigned ({marker.entries} entries)"


def _head_mismatch(count: int, marker: HeadMarker, actual_last_hash: str) -> str | None:
    """Why the marker and the file disagree, or ``None`` if they agree."""
    missing = marker.entries - count
    if missing > 0:
        return (
            f"the ledger is {missing} entr{'y' if missing == 1 else 'ies'} SHORT of "
            f"the {marker.entries} the head marker records. The file has been "
            "truncated, restored from an older copy, or partially written. Do not "
            "overwrite your backups."
        )
    if missing < 0:
        return (
            f"the ledger holds {count} entries but the head marker records only "
            f"{marker.entries}. Entries were appended without updating the marker, or "
            "two copies of the ledger were merged."
        )
    if marker.last_entry_hash != actual_last_hash:
        return (
            "the head marker's last-entry hash does not match the ledger's last entry, "
            "so the final entry has been replaced."
        )
    return None


def _signature_problems(entry: LedgerEntry, keys: dict[str, str]) -> list[str]:
    """Signature failures for one entry, as human sentences."""
    return _signature_problems_for(
        entry.ledger_key_id, entry.signature, entry.signing_payload(), keys
    )


def _signature_problems_for(
    key_id: str | None,
    signature: str | None,
    payload: bytes,
    keys: dict[str, str],
) -> list[str]:
    """Shared Ed25519 check for both ledger entries and the head marker."""
    from cryptography.exceptions import InvalidSignature  # noqa: PLC0415
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: PLC0415
        Ed25519PublicKey,
    )

    key_hex = keys.get(key_id or "")
    if key_hex is None:
        return [
            f"signed by unknown key {key_id!r} — no public key "
            "supplied, so the signature cannot be checked"
        ]
    try:
        public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(key_hex))
        public.verify(bytes.fromhex(signature or ""), payload)
    except InvalidSignature:
        return ["signature does not match — this line has been altered since it was signed"]
    except ValueError as exc:
        return [f"signature could not be checked: {exc}"]
    return []


def entry_from_report(
    document: dict[str, object],
    *,
    public_key: str | None,
    content_sha256: str,
    received_at: _dt.datetime | None = None,
) -> LedgerEntry:
    """Build the ledger entry for a report that has just been accepted."""
    install = document.get("install")
    anchor = document.get("anchor")
    install_map = install if isinstance(install, dict) else {}
    anchor_map = anchor if isinstance(anchor, dict) else {}
    head = anchor_map.get("head")
    head_map = head if isinstance(head, dict) else {}
    genesis = anchor_map.get("genesis_record")
    genesis_map = genesis if isinstance(genesis, dict) else {}
    tree = anchor_map.get("tree")
    tree_map = tree if isinstance(tree, dict) else {}
    received = received_at or _dt.datetime.now(tz=_dt.UTC)
    return LedgerEntry(
        install_id=str(install_map.get("install_id", "")),
        counter=_as_int(document.get("counter")),
        # Preserved as None for an empty chain rather than coerced to 0:
        # the difference is "nothing to anchor to yet" versus "record 0
        # has been deleted", and only one of those is an accusation.
        head_id=_opt_int(head_map.get("id")),
        head_hash=_opt_str(head_map.get("hash")),
        tree_size=_as_int(tree_map.get("size")),
        tree_root=_opt_str(tree_map.get("root")),
        genesis_hash=_opt_str(genesis_map.get("hash")),
        report_public_key=public_key,
        generated_at=_opt_str(document.get("generated_at")),
        received_at=received.isoformat(timespec="seconds").replace("+00:00", "Z"),
        content_sha256=content_sha256,
    )


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)


def _opt_int(value: object) -> int | None:
    """Int from an untyped JSON value, preserving ``None`` as ``None``.

    Distinct from :func:`_as_int` on purpose: for a head, "absent" and
    "zero" mean different things.
    """
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _as_int(value: object) -> int:
    """Best-effort int from an untyped JSON value (0 when absent / unusable)."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0
