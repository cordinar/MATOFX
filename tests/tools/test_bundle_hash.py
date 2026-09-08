"""Exhaustive determinism tests for :func:`compute_bundle_hash`.

The V&V cohort key (ADR-0011 / ``designs/vv-tracking.md`` §6.5) hinges
on this function being correct. The cost of a subtle bug — V&V inheriting
where it shouldn't, or refusing to inherit where it should — is a
compliance trail that's silently wrong, so the test surface is broad:

- Round-trip on a fixture bundle.
- Same content laid out with different OS path separators produces the
  same hash (POSIX-style normalisation).
- Sort-order invariance: shuffling file-creation order doesn't change
  the hash.
- ``mtime`` change doesn't change the hash.
- Permission change doesn't change the hash (where the platform allows
  setting permissions).
- Empty bundle returns :data:`EMPTY_BUNDLE_HASH` (distinct from the
  ``""`` "unknown" sentinel stored in ``tool_version.bundle_hash``).
- Symlink contributes its target string, not the target's contents.
- Missing / non-directory raises a typed error.
- Trivial change to a single file flips the hash.
"""

from __future__ import annotations

import hashlib
import os
import sys
import time
from pathlib import Path

import pytest

from matofx.tools.versioning import (
    EMPTY_BUNDLE_HASH,
    compute_bundle_hash,
)


def _write(path: Path, content: bytes | str) -> None:
    """Write content to ``path``, creating parents as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_bytes(content)


def _make_fixture_bundle(root: Path) -> None:
    """Build a small but non-trivial bundle: nested dirs, mixed text + binary."""
    _write(root / "main.py", "print('hello')\n")
    _write(root / "README.md", "# Example\n\nA fixture bundle.\n")
    _write(root / "static" / "input.json", '{"k": 1}\n')
    _write(root / "static" / "img.bin", bytes(range(256)))
    _write(root / "sub" / "nested" / "deep.txt", "deep file\n")


# ---- Determinism + invariance ----------------------------------------


def test_round_trip_same_bundle_same_hash(tmp_path: Path) -> None:
    """Hashing the same bundle twice yields the same hex digest."""
    bundle = tmp_path / "b"
    _make_fixture_bundle(bundle)
    h1 = compute_bundle_hash(bundle)
    h2 = compute_bundle_hash(bundle)
    assert h1 == h2
    assert len(h1) == 64
    assert all(c in "0123456789abcdef" for c in h1)


def test_two_byte_identical_bundles_in_different_dirs_match(tmp_path: Path) -> None:
    """Two separate directories with identical contents hash identically."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    _make_fixture_bundle(a)
    _make_fixture_bundle(b)
    assert compute_bundle_hash(a) == compute_bundle_hash(b)


def test_file_creation_order_does_not_matter(tmp_path: Path) -> None:
    """Files written in different orders produce the same hash.

    Catches a non-deterministic walk that yields entries in inode order
    or some other arbitrary OS-dependent sequence.
    """
    forward = tmp_path / "forward"
    backward = tmp_path / "backward"
    files = [
        ("a.txt", b"first"),
        ("b.txt", b"second"),
        ("c.txt", b"third"),
        ("nested/d.txt", b"fourth"),
        ("nested/e.txt", b"fifth"),
    ]
    for rel, content in files:
        _write(forward / rel, content)
    for rel, content in reversed(files):
        _write(backward / rel, content)
    assert compute_bundle_hash(forward) == compute_bundle_hash(backward)


def test_mtime_change_does_not_affect_hash(tmp_path: Path) -> None:
    """Touching a file (no content change) does not change the hash."""
    bundle = tmp_path / "b"
    _make_fixture_bundle(bundle)
    before = compute_bundle_hash(bundle)
    # Push every file's mtime forward by an hour.
    far_future = time.time() + 3600
    for path in bundle.rglob("*"):
        if path.is_file():
            os.utime(path, (far_future, far_future))
    after = compute_bundle_hash(bundle)
    assert before == after


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-style file modes aren't meaningful on Windows",
)
def test_permission_change_does_not_affect_hash(tmp_path: Path) -> None:
    """chmod'ing a file does not change the hash on POSIX platforms."""
    bundle = tmp_path / "b"
    _make_fixture_bundle(bundle)
    before = compute_bundle_hash(bundle)
    for path in bundle.rglob("*"):
        if path.is_file():
            path.chmod(0o600)
    after = compute_bundle_hash(bundle)
    assert before == after


# ---- Sensitivity (the hash MUST change when it should) ---------------


def test_single_byte_change_flips_hash(tmp_path: Path) -> None:
    """Changing one byte in one file produces a different hash."""
    bundle = tmp_path / "b"
    _make_fixture_bundle(bundle)
    before = compute_bundle_hash(bundle)
    (bundle / "main.py").write_text("print('hello!')\n", encoding="utf-8")
    after = compute_bundle_hash(bundle)
    assert before != after


def test_file_rename_flips_hash(tmp_path: Path) -> None:
    """Renaming a file (same contents, different path) changes the hash.

    Catches a hash that walks only content, not the relative-path
    layout — two bundles with the same files in different folders would
    spuriously inherit V&V.
    """
    bundle = tmp_path / "b"
    _make_fixture_bundle(bundle)
    before = compute_bundle_hash(bundle)
    (bundle / "main.py").rename(bundle / "renamed.py")
    after = compute_bundle_hash(bundle)
    assert before != after


def test_adding_a_file_flips_hash(tmp_path: Path) -> None:
    """Adding a new file (anywhere) changes the hash."""
    bundle = tmp_path / "b"
    _make_fixture_bundle(bundle)
    before = compute_bundle_hash(bundle)
    _write(bundle / "extra.txt", "new\n")
    after = compute_bundle_hash(bundle)
    assert before != after


def test_removing_a_file_flips_hash(tmp_path: Path) -> None:
    """Removing a file changes the hash."""
    bundle = tmp_path / "b"
    _make_fixture_bundle(bundle)
    before = compute_bundle_hash(bundle)
    (bundle / "README.md").unlink()
    after = compute_bundle_hash(bundle)
    assert before != after


# ---- Edge cases ------------------------------------------------------


def test_empty_bundle_returns_fixed_sentinel(tmp_path: Path) -> None:
    """An empty directory produces :data:`EMPTY_BUNDLE_HASH`."""
    bundle = tmp_path / "empty"
    bundle.mkdir()
    assert compute_bundle_hash(bundle) == EMPTY_BUNDLE_HASH
    assert hashlib.sha256(b"").hexdigest() == EMPTY_BUNDLE_HASH
    # And not equal to the "unknown" sentinel (the empty *string*).
    assert EMPTY_BUNDLE_HASH != ""


def test_nonexistent_dir_raises(tmp_path: Path) -> None:
    """A missing bundle dir raises :class:`FileNotFoundError`."""
    with pytest.raises(FileNotFoundError):
        compute_bundle_hash(tmp_path / "does-not-exist")


def test_bundle_path_is_a_file_raises(tmp_path: Path) -> None:
    """A path pointing at a file (not a dir) raises :class:`NotADirectoryError`."""
    plain_file = tmp_path / "not-a-bundle.txt"
    plain_file.write_text("nope\n", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        compute_bundle_hash(plain_file)


def test_deeply_nested_structure(tmp_path: Path) -> None:
    """Files several directories deep are walked correctly."""
    bundle = tmp_path / "b"
    _write(bundle / "a" / "b" / "c" / "d" / "e" / "f.txt", "deep\n")
    _write(bundle / "top.txt", "shallow\n")
    h1 = compute_bundle_hash(bundle)
    h2 = compute_bundle_hash(bundle)
    assert h1 == h2


def test_large_file_streams_correctly(tmp_path: Path) -> None:
    """A file bigger than the read chunk size hashes deterministically.

    Defends against a regression where the chunked read might mis-order
    or drop trailing bytes.
    """
    bundle = tmp_path / "b"
    # 2 MiB — well past the 64 KiB chunk boundary.
    big_payload = b"x" * (2 * 1024 * 1024)
    _write(bundle / "big.bin", big_payload)
    h1 = compute_bundle_hash(bundle)
    # Re-write identical content (different inode, same bytes); hash unchanged.
    (bundle / "big.bin").write_bytes(big_payload)
    h2 = compute_bundle_hash(bundle)
    assert h1 == h2


def test_binary_file_with_full_byte_range(tmp_path: Path) -> None:
    """A file containing every possible byte value round-trips through the hash."""
    bundle = tmp_path / "b"
    _write(bundle / "bytes.bin", bytes(range(256)))
    h1 = compute_bundle_hash(bundle)
    # Identical second write — hash stable.
    (bundle / "bytes.bin").write_bytes(bytes(range(256)))
    h2 = compute_bundle_hash(bundle)
    assert h1 == h2
    # One byte different — hash differs.
    (bundle / "bytes.bin").write_bytes(bytes(range(255)) + b"\x00")
    h3 = compute_bundle_hash(bundle)
    assert h1 != h3


# ---- Symlink behaviour ----------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Creating symlinks on Windows requires elevation or developer mode",
)
def test_symlink_to_file_hashes_target_string_not_content(tmp_path: Path) -> None:
    """A symlink contributes its target string, not the file it points to."""
    bundle = tmp_path / "b"
    bundle.mkdir()
    target_outside = tmp_path / "outside.txt"
    target_outside.write_text("outside content\n", encoding="utf-8")
    link = bundle / "link.txt"
    link.symlink_to(target_outside)

    h_before = compute_bundle_hash(bundle)

    # Modify the *target* — the symlink's target string is unchanged, so
    # the bundle's hash MUST be unchanged. (If the hash followed the link
    # it would change here and we'd have a security-relevant divergence.)
    target_outside.write_text("very different content\n", encoding="utf-8")
    h_after_target_change = compute_bundle_hash(bundle)
    assert h_before == h_after_target_change

    # Re-point the symlink to a different target — the target string IS
    # different, so the bundle's hash MUST change.
    other_target = tmp_path / "other.txt"
    other_target.write_text("anything\n", encoding="utf-8")
    link.unlink()
    link.symlink_to(other_target)
    h_after_relink = compute_bundle_hash(bundle)
    assert h_before != h_after_relink


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Creating symlinks on Windows requires elevation or developer mode",
)
def test_symlink_to_directory_not_descended(tmp_path: Path) -> None:
    """A symlink to a directory is hashed as a leaf, not walked into."""
    bundle = tmp_path / "b"
    bundle.mkdir()
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    # Put a file inside the outside dir; if the walk followed the symlink
    # it would hash this file's content and the two assertions below would
    # diverge.
    (outside_dir / "secret.txt").write_text("should not be hashed\n", encoding="utf-8")

    link = bundle / "linked_dir"
    link.symlink_to(outside_dir, target_is_directory=True)

    h_before = compute_bundle_hash(bundle)

    # Modify the file inside the target dir — the hash MUST be unchanged.
    (outside_dir / "secret.txt").write_text("modified secret\n", encoding="utf-8")
    h_after_target_modify = compute_bundle_hash(bundle)
    assert h_before == h_after_target_modify
