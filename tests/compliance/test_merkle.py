"""Merkle tree hashing and consistency proofs (RFC 6962).

Anchoring is the load-bearing claim in ADR-0019, and it now rests
entirely on these functions, so they are tested three ways:

1. against the **published RFC 6962 test vectors** — if our tree hash
   disagrees with the standard, the artefact is not the standard and a
   third party's tooling would reject it;
2. **exhaustively** over every ``(m, n)`` pair up to a useful size, since
   the proof algorithm's edge cases live at perfect-power boundaries;
3. **adversarially** — a proof must not verify for a log that was
   rewritten, truncated, or that supplies no evidence at all.

The third group is the one that matters. A consistency check that only
ever sees honest inputs proves nothing; the earlier design passed all its
tests while failing on real installs.
"""

from __future__ import annotations

import hashlib

import pytest

from matofx.compliance.merkle import (
    consistency_proof,
    hex_proof,
    proof_from_hex,
    tree_hash,
    verify_consistency,
)

#: RFC 6962 §2.1.3 reference inputs.
_RFC_LEAVES = [
    bytes.fromhex(h)
    for h in (
        "",
        "00",
        "10",
        "2021",
        "3031",
        "40414243",
        "5051525354555657",
        "606162636465666768696a6b6c6d6e6f",
    )
]

#: MTH of the first n reference leaves, n = 0..8.
_RFC_ROOTS = [
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d",
    "fac54203e7cc696cf0dfcb42c92a1d9dbaf70ad9e621f4bd8d98662f00e3c125",
    "aeb6bcfe274b70a14fb067a5e5578264db0fa9b51af5e0ba159158f329e06e77",
    "d37ee418976dd95753c1c73862b9398fa2a2cf9b4ff0fdfe8b30cd95209614b7",
    "4e3bbb1f7b478dcfe71fb631631519a3bca12c9aefca1612bfce4c13a86264d4",
    "76e67dadbcdf1e10e1b74ddc608abd2f98dfb16fbce75277b5232a127f2087ef",
    "ddb89be403809e325750d3d263cd78929c2942b7942a34b77e122c9594a74c8c",
    "5dc9da79a70659a9ad559cb701ded9a2ab9d823aad2f4960cfe370eff4604328",
]


def _chain(count: int) -> list[bytes]:
    return [hashlib.sha256(f"record-{i}".encode()).digest() for i in range(count)]


@pytest.mark.parametrize("size", range(len(_RFC_ROOTS)))
def test_tree_hash_matches_rfc6962_vectors(size: int) -> None:
    """Our tree is *the* standard tree, not merely a tree."""
    assert tree_hash(_RFC_LEAVES[:size]).hex() == _RFC_ROOTS[size]


def test_every_pair_up_to_64_verifies() -> None:
    """Exhaustive: proofs exist and verify for every m <= n."""
    leaves = _chain(64)
    for n in range(1, 65):
        root_n = tree_hash(leaves[:n])
        for m in range(1, n + 1):
            proof = consistency_proof(leaves[:n], m)
            assert verify_consistency(
                m=m, root_m=tree_hash(leaves[:m]), n=n, root_n=root_n, proof=proof
            ), f"consistency {m} -> {n} failed to verify"


def test_proof_size_is_logarithmic() -> None:
    """The reason this scales to annual reporting on a 90k-record chain."""
    leaves = _chain(1024)
    assert len(consistency_proof(leaves, 1)) <= 10
    assert len(consistency_proof(leaves, 512)) <= 10


@pytest.mark.parametrize("rewritten", [0, 1, 17, 63])
def test_a_rewritten_record_breaks_the_proof(rewritten: int) -> None:
    """The core claim: altering attested history cannot be proved away."""
    honest = _chain(120)
    root_then = tree_hash(honest[:64])

    tampered = list(honest)
    tampered[rewritten] = hashlib.sha256(b"rewritten").digest()

    # The install builds a perfectly coherent tree over its new history.
    proof = consistency_proof(tampered, 64)
    assert not verify_consistency(
        m=64, root_m=root_then, n=len(tampered), root_n=tree_hash(tampered), proof=proof
    )


def test_a_truncated_log_cannot_produce_a_proof() -> None:
    """Records cannot be removed from an append-only log."""
    leaves = _chain(40)
    with pytest.raises(ValueError, match="smaller size"):
        consistency_proof(leaves, 80)
    assert not verify_consistency(
        m=80, root_m=tree_hash(_chain(80)), n=40, root_n=tree_hash(leaves), proof=[]
    )


def test_an_empty_proof_does_not_verify_an_extension() -> None:
    """Asserting extension without evidence must fail."""
    leaves = _chain(32)
    assert not verify_consistency(
        m=5, root_m=tree_hash(leaves[:5]), n=32, root_n=tree_hash(leaves), proof=[]
    )


def test_a_proof_for_the_wrong_root_does_not_verify() -> None:
    """A valid-shaped proof against a root the vendor didn't record fails."""
    leaves = _chain(50)
    proof = consistency_proof(leaves, 20)
    assert not verify_consistency(
        m=20,
        root_m=hashlib.sha256(b"not the recorded root").digest(),
        n=50,
        root_n=tree_hash(leaves),
        proof=proof,
    )


def test_equal_sizes_require_an_identical_root() -> None:
    leaves = _chain(16)
    assert verify_consistency(
        m=16, root_m=tree_hash(leaves), n=16, root_n=tree_hash(leaves), proof=[]
    )
    assert not verify_consistency(
        m=16, root_m=tree_hash([*_chain(15), b"x"]), n=16, root_n=tree_hash(leaves), proof=[]
    )


def test_an_empty_baseline_commits_to_nothing() -> None:
    """A report taken before any record existed cannot be contradicted."""
    assert verify_consistency(m=0, root_m=b"", n=9, root_n=tree_hash(_chain(9)), proof=[])


def test_malformed_proofs_are_rejected_not_raised() -> None:
    """Untrusted input degrades to 'cannot verify', never to a crash."""
    assert proof_from_hex(["zz" * 32]) is None
    assert proof_from_hex(["ab"]) is None  # right hex, wrong length
    assert proof_from_hex([]) == []
    assert proof_from_hex(hex_proof(consistency_proof(_chain(8), 3))) is not None
