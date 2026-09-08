"""Tests for Ed25519 signing-key handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from matofx.audit import (
    AuditKeyError,
    collect_public_keys,
    compute_key_id,
    generate_signing_key,
    load_signing_key,
    retired_keys_dir,
    rotate_signing_key,
)


def test_generate_creates_pem_file(tmp_path: Path) -> None:
    keyfile = tmp_path / "signing.key"
    generate_signing_key(keyfile)
    contents = keyfile.read_bytes()
    assert b"BEGIN PRIVATE KEY" in contents
    assert keyfile.exists()


def test_generate_refuses_to_overwrite(tmp_path: Path) -> None:
    keyfile = tmp_path / "signing.key"
    generate_signing_key(keyfile)
    with pytest.raises(AuditKeyError) as exc_info:
        generate_signing_key(keyfile)
    assert "Refusing to overwrite" in str(exc_info.value)


def test_round_trip_load(tmp_path: Path) -> None:
    keyfile = tmp_path / "signing.key"
    generated = generate_signing_key(keyfile)
    loaded = load_signing_key(keyfile)
    assert compute_key_id(generated.public_key()) == compute_key_id(loaded.public_key())


def test_load_missing_file_raises_actionable_error(tmp_path: Path) -> None:
    with pytest.raises(AuditKeyError) as exc_info:
        load_signing_key(tmp_path / "nope.key")
    msg = str(exc_info.value)
    assert "rotate signing-key" in msg


def test_load_garbage_file_raises(tmp_path: Path) -> None:
    keyfile = tmp_path / "junk.key"
    keyfile.write_bytes(b"not a key")
    with pytest.raises(AuditKeyError):
        load_signing_key(keyfile)


def test_key_id_stable_across_loads(tmp_path: Path) -> None:
    keyfile = tmp_path / "signing.key"
    generate_signing_key(keyfile)
    a = compute_key_id(load_signing_key(keyfile).public_key())
    b = compute_key_id(load_signing_key(keyfile).public_key())
    assert a == b
    assert len(a) == 16


# --- rotation --------------------------------------------------------------


def test_rotate_init_creates_first_key(tmp_path: Path) -> None:
    keyfile = tmp_path / "signing.key"
    result = rotate_signing_key(keyfile, init=True)
    assert keyfile.is_file()
    assert result.old_key_id is None
    assert result.new_key_id == compute_key_id(load_signing_key(keyfile).public_key())


def test_rotate_init_refuses_when_key_exists(tmp_path: Path) -> None:
    keyfile = tmp_path / "signing.key"
    generate_signing_key(keyfile)
    with pytest.raises(AuditKeyError) as exc_info:
        rotate_signing_key(keyfile, init=True)
    assert "--init refused" in str(exc_info.value)


def test_rotate_without_init_requires_existing_key(tmp_path: Path) -> None:
    with pytest.raises(AuditKeyError) as exc_info:
        rotate_signing_key(tmp_path / "signing.key", init=False)
    assert "--init" in str(exc_info.value)


def test_rotate_replaces_key_and_retires_old(tmp_path: Path) -> None:
    keyfile = tmp_path / "signing.key"
    first = generate_signing_key(keyfile)
    old_id = compute_key_id(first.public_key())

    result = rotate_signing_key(keyfile, init=False)

    # A fresh key now sits at the path, with a different id.
    new_id = compute_key_id(load_signing_key(keyfile).public_key())
    assert new_id != old_id
    assert result.old_key_id == old_id
    assert result.new_key_id == new_id
    # Old public key retired for continuity; old private key archived.
    assert result.retired_public_key_path == retired_keys_dir(keyfile) / f"{old_id}.pub"
    assert result.retired_public_key_path.is_file()
    assert result.archived_private_key_path.is_file()


def test_collect_public_keys_covers_active_and_retired(tmp_path: Path) -> None:
    keyfile = tmp_path / "signing.key"
    first = generate_signing_key(keyfile)
    old_id = compute_key_id(first.public_key())
    rotate_signing_key(keyfile, init=False)
    new_id = compute_key_id(load_signing_key(keyfile).public_key())

    keys = collect_public_keys(keyfile)

    assert set(keys) == {old_id, new_id}
    # Returned keys are keyed by their own id.
    assert compute_key_id(keys[old_id]) == old_id
    assert compute_key_id(keys[new_id]) == new_id


def test_collect_public_keys_skips_garbage_retired_file(tmp_path: Path) -> None:
    keyfile = tmp_path / "signing.key"
    generate_signing_key(keyfile)
    retired = retired_keys_dir(keyfile)
    retired.mkdir(parents=True, exist_ok=True)
    (retired / "junk.pub").write_bytes(b"not a key")
    # Only the active key survives; the junk file is skipped, not fatal.
    assert len(collect_public_keys(keyfile)) == 1
