"""The ledger's own integrity: the chain, the signatures, the head marker.

The head ledger is what every compliance verdict is checked against, and
until now nothing checked *it*. These tests cover the two failure modes
that matter and are easy to conflate:

* **An interior edit** — a line altered, reordered or removed. The chain
  catches this, because every later ``prev_hash`` stops matching.
* **A tail truncation** — the file cut short. The chain cannot catch
  this at all: what remains is a valid chain, one entry shorter. Only
  the head marker catches it, which is the entire reason it exists.

Both true-positive and false-positive cases are here deliberately. The
generalisable lesson recorded in ADR-0019's 2026-08-11 amendment is that
every one of that session's defects was a *false accusation* — so for
any check that accuses, the honest case needs a test as deliberately as
the guilty one.
"""

from __future__ import annotations

from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from matofx.compliance import LedgerError
from matofx.compliance.ledger import (
    LEDGER_GENESIS_HASH,
    LedgerEntry,
    append_entry,
    head_marker_path,
    read_head_marker,
    read_ledger,
    verify_ledger,
    write_head_marker,
)


class _Signer:
    """An in-memory ledger signer, seeded so tests are deterministic."""

    def __init__(self, key_id: str = "funis-ledger-test-01", seed: int = 0x33) -> None:
        self.key_id = key_id
        self._private = Ed25519PrivateKey.from_private_bytes(bytes([seed]) * 32)

    def sign(self, payload: bytes) -> bytes:
        return self._private.sign(payload)

    def public_hex(self) -> str:
        return self._private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()


@pytest.fixture
def signer() -> _Signer:
    return _Signer()


@pytest.fixture
def keys(signer: _Signer) -> dict[str, str]:
    return {signer.key_id: signer.public_hex()}


def _entry(counter: int, install: str = "inst_TEST0001") -> LedgerEntry:
    return LedgerEntry(
        install_id=install,
        counter=counter,
        head_id=counter * 10,
        head_hash=f"{counter:064x}",
        tree_size=counter * 10,
        tree_root=f"{counter:064x}",
    )


def _build(path: Path, signer: _Signer | None, count: int = 3) -> list[LedgerEntry]:
    for counter in range(1, count + 1):
        append_entry(path, _entry(counter), signer=signer)
    return read_ledger(path)


# ----- chaining -------------------------------------------------------


def test_first_entry_chains_to_genesis(tmp_path: Path, signer: _Signer) -> None:
    path = tmp_path / "heads.jsonl"
    written = append_entry(path, _entry(1), signer=signer)
    assert written.prev_hash == LEDGER_GENESIS_HASH


def test_each_entry_chains_to_the_one_before(tmp_path: Path, signer: _Signer) -> None:
    entries = _build(tmp_path / "heads.jsonl", signer)
    for earlier, later in pairwise(entries):
        assert later.prev_hash == earlier.entry_hash()


def test_caller_supplied_prev_hash_is_ignored(tmp_path: Path, signer: _Signer) -> None:
    """The chain is derived from the file, never trusted from the caller."""
    path = tmp_path / "heads.jsonl"
    append_entry(path, _entry(1), signer=signer)
    written = append_entry(path, replace(_entry(2), prev_hash="f" * 64), signer=signer)
    assert written.prev_hash != "f" * 64


# ----- the honest case ------------------------------------------------


def test_an_untouched_ledger_verifies(
    tmp_path: Path, signer: _Signer, keys: dict[str, str]
) -> None:
    path = tmp_path / "heads.jsonl"
    entries = _build(path, signer)
    result = verify_ledger(entries, public_keys=keys, marker=read_head_marker(path))
    assert result.ok
    assert result.failed == 0
    assert result.signed == 3
    assert result.head_status == "ok"


def test_legacy_unsigned_entries_are_readable_not_failures(tmp_path: Path) -> None:
    """A ledger written before signing existed is still evidence.

    Refusing to read it would destroy exactly the history being
    protected, so unsigned entries warn rather than fail.
    """
    path = tmp_path / "heads.jsonl"
    entries = _build(path, None)
    result = verify_ledger(entries, marker=read_head_marker(path))
    assert result.ok
    assert result.unsigned == 3
    assert result.failed == 0


def test_a_missing_marker_on_a_legacy_ledger_warns_rather_than_accusing(
    tmp_path: Path, keys: dict[str, str]
) -> None:
    """No signatures means the ledger predates marker-writing. Warn only."""
    path = tmp_path / "heads.jsonl"
    entries = _build(path, None)
    head_marker_path(path).unlink(missing_ok=True)

    result = verify_ledger(entries, public_keys=keys, marker=read_head_marker(path))

    assert result.ok
    assert result.head_status == "absent"


def test_a_missing_marker_on_a_signed_ledger_is_an_accusation(
    tmp_path: Path, signer: _Signer, keys: dict[str, str]
) -> None:
    """Found by stress-testing on 2026-08-12.

    `append_entry` writes a marker every time it signs, so a signed
    ledger cannot lack one by accident. Its absence means somebody
    removed it — and removing it is exactly the step that would let
    entries be cut off the end unnoticed, since the chain alone cannot
    see a truncation. Previously this warned, which meant
    "delete the marker, then truncate" verified clean.
    """
    path = tmp_path / "heads.jsonl"
    entries = _build(path, signer)
    head_marker_path(path).unlink()

    result = verify_ledger(entries, public_keys=keys, marker=read_head_marker(path))

    assert not result.ok
    assert result.head_status == "fail"
    assert "MISSING" in result.head_detail


# ----- interior tampering: the chain catches it -----------------------


def test_an_edited_entry_fails_its_signature(
    tmp_path: Path, signer: _Signer, keys: dict[str, str]
) -> None:
    path = tmp_path / "heads.jsonl"
    entries = _build(path, signer)
    entries[0] = replace(entries[0], tree_size=9999)

    result = verify_ledger(entries, public_keys=keys, marker=read_head_marker(path))

    assert not result.ok
    assert result.lines[0].status == "fail"
    assert "altered" in result.lines[0].detail
    # ...and the edit propagates: line 2 no longer chains to line 1.
    assert result.lines[1].status == "fail"
    assert "chain break" in result.lines[1].detail


def test_a_removed_interior_entry_breaks_the_chain(
    tmp_path: Path, signer: _Signer, keys: dict[str, str]
) -> None:
    path = tmp_path / "heads.jsonl"
    entries = _build(path, signer)
    result = verify_ledger(
        [entries[0], entries[2]], public_keys=keys, marker=read_head_marker(path)
    )
    assert not result.ok
    assert any("chain break" in line.detail for line in result.lines)


def test_reordered_entries_break_the_chain(
    tmp_path: Path, signer: _Signer, keys: dict[str, str]
) -> None:
    path = tmp_path / "heads.jsonl"
    entries = _build(path, signer)
    shuffled = [entries[2], entries[1], entries[0]]
    result = verify_ledger(shuffled, public_keys=keys, marker=read_head_marker(path))
    assert not result.ok


# ----- tail truncation: only the marker catches it --------------------


def test_truncation_is_invisible_to_the_chain_alone(
    tmp_path: Path, signer: _Signer, keys: dict[str, str]
) -> None:
    """Pinned deliberately: this is *why* the head marker exists.

    A truncated ledger is a perfectly valid chain. If this test ever
    starts failing because the chain caught it, the marker may look
    redundant — it is not; read the assertion below it.
    """
    # Unsigned, so the *chain* is the only mechanism in play — a signed
    # ledger now also fails on the marker's absence, which would mask
    # what this test is demonstrating.
    path = tmp_path / "heads.jsonl"
    legacy = _build(path, None)
    without_marker = verify_ledger(legacy[:2], public_keys=keys, marker=None)
    assert without_marker.ok, "a truncated chain is still a valid chain"

    # The marker is what notices. Same truncation, signed ledger.
    signed_path = tmp_path / "signed.jsonl"
    entries = _build(signed_path, signer)
    with_marker = verify_ledger(
        entries[:2], public_keys=keys, marker=read_head_marker(signed_path)
    )
    assert not with_marker.ok
    assert "SHORT" in with_marker.head_detail


def test_extra_entries_beyond_the_marker_fail(
    tmp_path: Path, signer: _Signer, keys: dict[str, str]
) -> None:
    """Two copies of the ledger merged, or an append that skipped the marker."""
    path = tmp_path / "heads.jsonl"
    entries = _build(path, signer)
    marker = read_head_marker(path)
    result = verify_ledger([*entries, _entry(4)], public_keys=keys, marker=marker)
    assert not result.ok
    assert "records only" in result.head_detail


def test_a_replaced_final_entry_fails_the_marker(
    tmp_path: Path, signer: _Signer, keys: dict[str, str]
) -> None:
    path = tmp_path / "heads.jsonl"
    entries = _build(path, signer)
    swapped = [*entries[:2], replace(entries[2], counter=99)]
    result = verify_ledger(swapped, public_keys=keys, marker=read_head_marker(path))
    assert not result.ok


def test_a_tampered_marker_fails(
    tmp_path: Path, signer: _Signer, keys: dict[str, str]
) -> None:
    """Re-pointing the marker at a shorter ledger must not launder it."""
    path = tmp_path / "heads.jsonl"
    entries = _build(path, signer)
    forged = replace(read_head_marker(path), entries=2)  # type: ignore[arg-type]
    result = verify_ledger(entries[:2], public_keys=keys, marker=forged)
    assert not result.ok
    assert "head marker" in result.head_detail


# ----- keys -----------------------------------------------------------


def test_an_unknown_key_fails_rather_than_skipping(
    tmp_path: Path, signer: _Signer
) -> None:
    """A signature nobody can check is not evidence.

    Passing it silently would be the false reassurance this whole file
    exists to prevent.
    """
    path = tmp_path / "heads.jsonl"
    entries = _build(path, signer)
    result = verify_ledger(entries, public_keys={}, marker=read_head_marker(path))
    assert not result.ok
    assert "unknown key" in result.lines[0].detail


def test_a_signature_from_the_wrong_key_fails(tmp_path: Path, signer: _Signer) -> None:
    path = tmp_path / "heads.jsonl"
    entries = _build(path, signer)
    other = _Signer(key_id=signer.key_id, seed=0x44)
    result = verify_ledger(
        entries,
        public_keys={signer.key_id: other.public_hex()},
        marker=read_head_marker(path),
    )
    assert not result.ok


# ----- no silent downgrade --------------------------------------------


def test_unsigned_append_to_a_signed_ledger_is_refused(
    tmp_path: Path, signer: _Signer
) -> None:
    """Otherwise signing is bypassed by simply not passing a key."""
    path = tmp_path / "heads.jsonl"
    append_entry(path, _entry(1), signer=signer)

    with pytest.raises(LedgerError, match="must be signed"):
        append_entry(path, _entry(2))

    assert len(read_ledger(path)) == 1


def test_signing_may_begin_on_a_previously_unsigned_ledger(
    tmp_path: Path, signer: _Signer, keys: dict[str, str]
) -> None:
    """The upgrade path must work, or existing ledgers stay unsigned for ever."""
    path = tmp_path / "heads.jsonl"
    append_entry(path, _entry(1))
    append_entry(path, _entry(2), signer=signer)

    entries = read_ledger(path)
    result = verify_ledger(entries, public_keys=keys, marker=read_head_marker(path))
    assert result.ok
    assert result.unsigned == 1
    assert result.signed == 1


def test_marker_is_rewritten_to_match_after_each_append(
    tmp_path: Path, signer: _Signer
) -> None:
    path = tmp_path / "heads.jsonl"
    for expected in (1, 2, 3):
        append_entry(path, _entry(expected), signer=signer)
        marker = read_head_marker(path)
        assert marker is not None
        assert marker.entries == expected
        assert marker.last_entry_hash == read_ledger(path)[-1].entry_hash()


def test_write_head_marker_handles_an_empty_ledger(tmp_path: Path) -> None:
    """A ledger with no entries is a real state, not an error."""
    marker = write_head_marker(tmp_path / "heads.jsonl", [])
    assert marker.entries == 0
    assert marker.last_entry_hash == LEDGER_GENESIS_HASH
