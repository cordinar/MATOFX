"""Merkle tree hashing and consistency proofs (RFC 6962 §2.1).

## Why a tree when we already have a hash chain

The audit log is a hash chain: record N's hash covers record N-1's. That
proves *internal* consistency, and :mod:`matofx.audit.verify` checks it.
What it cannot do is prove to somebody else that the log you have now
still contains the log they saw a year ago — for that they would need
every intervening record, which at one report a year is megabytes.

A Merkle tree over the same record hashes answers exactly that question
in ``O(log n)``: a **consistency proof** between tree sizes ``m`` and
``n`` is ~17 hashes for a 90,000-record chain, and stays that size as the
chain grows. One primitive replaces three weaker checks:

- the log **shrank** → no proof from ``m`` to ``n < m`` can exist
- a record was **altered** → the proof cannot reconstruct the old root
- the log merely **grew** → the proof verifies, however many reports
  went undelivered in between

That last point is the one that matters operationally. The previous
design compared the install's self-reported "previous head" against the
ledger's latest entry and failed when they differed — which they do
whenever a report is generated and not sent. A consistency proof is
indexed by *tree size*, so the vendor can verify against whichever head
they happen to hold.

## Why RFC 6962 specifically

Certificate Transparency solved this problem, publicly, in 2013, and its
proof format is implemented and independently reviewed everywhere. Using
it means the artefact is checkable by tools that have nothing to do with
MATOFX, which is worth more to a compliance story than a bespoke scheme
that only our verifier understands.

The one deviation worth knowing: CT hashes certificates as leaves; we
hash the audit record's existing chain hash. So the tree is an index over
the chain, not a replacement for it — both checks still run, and they
fail independently.

## What this does not do

A consistency proof proves the *tree* is append-only. It cannot prove the
leaves describe reality: an install with root access can fabricate a
coherent tree over fabricated records. See ADR-0019 §10 — the evidential
weight is that a fabrication must then be maintained consistently for
ever, against a ledger the install cannot read.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

_LEAF_PREFIX = b"\x00"
_NODE_PREFIX = b"\x01"


def _leaf_hash(data: bytes) -> bytes:
    """RFC 6962 leaf hash: ``SHA-256(0x00 || data)``.

    The domain-separating prefix is what stops a leaf being passed off as
    an internal node (the second-preimage attack the RFC exists to
    prevent).
    """
    return hashlib.sha256(_LEAF_PREFIX + data).digest()


def _node_hash(left: bytes, right: bytes) -> bytes:
    """RFC 6962 interior node: ``SHA-256(0x01 || left || right)``."""
    return hashlib.sha256(_NODE_PREFIX + left + right).digest()


def _split_point(n: int) -> int:
    """The largest power of two strictly less than ``n`` (RFC 6962 ``k``).

    Splitting here rather than in half is what makes the left subtree
    always *perfect*, which is why consistency proofs stay ``O(log n)``.
    """
    return 1 << ((n - 1).bit_length() - 1)


def tree_hash(leaves: Sequence[bytes]) -> bytes:
    """Merkle Tree Hash of ``leaves`` (RFC 6962 ``MTH``)."""
    return _mth(leaves, 0, len(leaves))


def _mth(leaves: Sequence[bytes], start: int, end: int) -> bytes:
    n = end - start
    if n == 0:
        # MTH({}) is the hash of the empty string, per the RFC.
        return hashlib.sha256(b"").digest()
    if n == 1:
        return _leaf_hash(leaves[start])
    k = _split_point(n)
    return _node_hash(_mth(leaves, start, start + k), _mth(leaves, start + k, end))


def consistency_proof(leaves: Sequence[bytes], m: int) -> list[bytes]:
    """Proof that the tree of size ``len(leaves)`` extends the one of size ``m``.

    Args:
        leaves: Every leaf of the *current* tree, in order.
        m: The earlier tree size to prove extension from. ``0`` and
            ``m == len(leaves)`` both yield an empty proof — there is
            nothing to demonstrate.

    Raises:
        ValueError: If ``m`` exceeds the current tree size. That is not a
            provable claim; it is the shrinkage a caller must report as a
            failure rather than ask for a proof of.
    """
    n = len(leaves)
    if m > n:
        raise ValueError(f"cannot prove consistency from size {m} to smaller size {n}")
    if m in (0, n):
        return []
    return _subproof(leaves, 0, n, m, True)


def _subproof(
    leaves: Sequence[bytes], start: int, end: int, m: int, is_root: bool
) -> list[bytes]:
    """RFC 6962 ``SUBPROOF(m, D[start:end], is_root)``."""
    n = end - start
    if m == n:
        # The earlier tree is exactly this subtree. Its root is only
        # needed when this subtree is not the whole tree.
        return [] if is_root else [_mth(leaves, start, end)]
    k = _split_point(n)
    if m <= k:
        return [
            *_subproof(leaves, start, start + k, m, is_root),
            _mth(leaves, start + k, end),
        ]
    return [
        *_subproof(leaves, start + k, end, m - k, False),
        _mth(leaves, start, start + k),
    ]


def verify_consistency(
    *, m: int, root_m: bytes, n: int, root_n: bytes, proof: Sequence[bytes]
) -> bool:
    """Check a consistency proof between tree sizes ``m`` and ``n``.

    Returns ``True`` only when ``proof`` demonstrates that a tree of size
    ``n`` with root ``root_n`` is an append-only extension of a tree of
    size ``m`` with root ``root_m``.

    Deliberately returns a bool rather than raising: the caller turns
    this into a verdict with an explanatory message, and a malformed
    proof from an untrusted party is an expected input, not an error.
    """
    if m > n:
        return False
    if m == 0:
        # Nothing was previously attested, so nothing can contradict.
        return True
    if m == n:
        return not proof and root_m == root_n
    if not proof:
        return False

    path = list(proof)
    # Walk up from leaf index m-1 until we sit on a left-hand edge; that
    # is the highest node the two trees share.
    fn, sn = m - 1, n - 1
    while fn & 1:
        fn >>= 1
        sn >>= 1

    # A perfect earlier tree contributes its own root instead of a proof
    # element, so the path starts one step further along.
    is_perfect = m & (m - 1) == 0
    fr = sr = root_m if is_perfect else path.pop(0)

    for node in path:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            fr = _node_hash(node, fr)
            sr = _node_hash(node, sr)
            while fn and not fn & 1:
                fn >>= 1
                sn >>= 1
        else:
            sr = _node_hash(sr, node)
        fn >>= 1
        sn >>= 1

    return sn == 0 and fr == root_m and sr == root_n


def hex_proof(proof: Sequence[bytes]) -> list[str]:
    """Render a proof for the JSON document."""
    return [node.hex() for node in proof]


def proof_from_hex(values: Sequence[str]) -> list[bytes] | None:
    """Parse a proof from the document. ``None`` when it is malformed.

    Malformed input arrives from an untrusted party, so it must degrade
    to "cannot verify" rather than crashing the verifier.
    """
    parsed: list[bytes] = []
    for value in values:
        try:
            node = bytes.fromhex(str(value))
        except (TypeError, ValueError):
            return None
        if len(node) != hashlib.sha256().digest_size:
            return None
        parsed.append(node)
    return parsed
