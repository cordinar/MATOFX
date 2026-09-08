"""Unit tests for ``matofx.migrate.bundle``.

Round-trip + every tamper-detection path the design promises:

* Pack then unpack returns the original payload byte-for-byte.
* Tampering with the manifest fails signature verification.
* Tampering with a tool's static file fails the per-file hash check.
* Adding an extra file the manifest doesn't describe fails the
  bidirectional file-set check.
* Tarball with ``..``, absolute path, or backslash is rejected before
  any signature work.
* Duplicate aliases at pack time are refused outright.

These tests pin the plain-English wording of every error message we
expose, because the UI quotes those messages verbatim and
``MIGRATE_GUIDE.md`` §6 catalogues them.
"""

from __future__ import annotations

import io
import json
import tarfile

import pytest

from matofx.migrate.bundle import (
    BUNDLE_VERSION,
    MANIFEST_MEMBER,
    SIGNATURE_MEMBER,
    BundleError,
    ToolManifestEntry,
    ToolPayload,
    UnpackedBundle,
    WorkflowManifestEntry,
    WorkflowPayload,
    pack_bundle,
    unpack_bundle,
)
from matofx.migrate.signing import (
    BundleSigningError,
    KeyPair,
    compute_fingerprint,
    public_key_from_hex,
    public_key_to_hex,
    sign_manifest,
    verify_manifest,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signer(kp: KeyPair):
    def signer(manifest_bytes: bytes) -> bytes:
        return sign_manifest(manifest_bytes, kp.private_key)

    return signer


def _make_verifier():
    """Verifier that derives the public key from the hex embedded in
    the manifest. Same shape the service layer will use."""

    def verifier(manifest_bytes: bytes, signature: bytes, pub_hex: str) -> None:
        pub = public_key_from_hex(pub_hex)
        verify_manifest(manifest_bytes, signature, pub)

    return verifier


def _sample_tools() -> list[ToolPayload]:
    return [
        ToolPayload(
            entry=ToolManifestEntry(
                alias="material_properties",
                version="1.5",
                state_on_source="released",
                settings={
                    "aos_host_name": "ansys_main",
                    "python_env_name": "python3_11_ansys",
                    "resource_requirements": {"cpu": 4, "ram_gb": 16},
                },
            ),
            files={
                "descriptor.toml": b'alias = "material_properties"\nversion = "1.5"\n',
                "static/main_model.py": b"import sys\nprint('hello')\n",
                "static/inputs.template": b"E = 200e9\nnu = 0.3\n",
            },
        ),
        ToolPayload(
            entry=ToolManifestEntry(
                alias="heat_transfer",
                version="2.1",
                state_on_source="released",
                settings={"hpc_cluster_name": "london_hpc"},
            ),
            files={
                "descriptor.toml": b'alias = "heat_transfer"\nversion = "2.1"\n',
            },
        ),
    ]


def _sample_workflows() -> list[WorkflowPayload]:
    return [
        WorkflowPayload(
            entry=WorkflowManifestEntry(
                alias="thermal_baseline",
                name="Thermal Baseline",
                state_on_source="released",
                definition={
                    "members": ["material_properties", "heat_transfer"],
                    "ordering": ["material_properties", "heat_transfer"],
                },
                approvers=["alice@funisoft.com", "bob@funisoft.com"],
            ),
        ),
    ]


def _pack_default(kp: KeyPair, **overrides) -> bytes:
    return pack_bundle(
        source_install_id=overrides.get("source_install_id", "staging-install-uuid"),
        source_fingerprint=overrides.get(
            "source_fingerprint", compute_fingerprint(kp.public_key)
        ),
        source_public_key_hex=overrides.get(
            "source_public_key_hex", public_key_to_hex(kp.public_key)
        ),
        created_by=overrides.get("created_by", "alice@funisoft.com"),
        notes=overrides.get("notes", "test bundle"),
        tools=overrides.get("tools", _sample_tools()),
        workflows=overrides.get("workflows", _sample_workflows()),
        signer=_make_signer(kp),
    )


def _read_member(bundle_bytes: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz") as tar:
        f = tar.extractfile(name)
        assert f is not None
        return f.read()


def _rewrite_member(bundle_bytes: bytes, name: str, new_data: bytes) -> bytes:
    """Return a new tarball with one member replaced."""
    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz") as src,
        tarfile.open(fileobj=out, mode="w:gz", format=tarfile.PAX_FORMAT) as dst,
    ):
        for info in src.getmembers():
            extracted = src.extractfile(info)
            payload = new_data if info.name == name else (extracted.read() if extracted else b"")
            new_info = tarfile.TarInfo(name=info.name)
            new_info.size = len(payload)
            new_info.mtime = 0
            new_info.mode = info.mode
            new_info.type = info.type
            dst.addfile(new_info, io.BytesIO(payload))
    return out.getvalue()


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_pack_then_unpack_round_trips_every_field() -> None:
    kp = KeyPair.generate()
    bundle = _pack_default(kp)
    result = unpack_bundle(bundle, verifier=_make_verifier())

    assert isinstance(result, UnpackedBundle)
    assert result.manifest.bundle_version == BUNDLE_VERSION
    assert result.manifest.source_install_id == "staging-install-uuid"
    assert result.manifest.source_fingerprint == compute_fingerprint(kp.public_key)
    assert result.manifest.source_public_key_hex == public_key_to_hex(kp.public_key)
    assert result.manifest.created_by == "alice@funisoft.com"
    assert result.manifest.notes == "test bundle"

    # Tool round-trip — bytes survive byte-for-byte and settings are passed through.
    original_tools = {t.entry.alias: t for t in _sample_tools()}
    out_tools = {t.entry.alias: t for t in result.tools}
    assert set(out_tools) == set(original_tools)
    for alias, orig in original_tools.items():
        out = out_tools[alias]
        assert out.files == orig.files
        assert out.entry.version == orig.entry.version
        assert out.entry.settings == orig.entry.settings
        assert out.entry.state_on_source == orig.entry.state_on_source

    # Workflow round-trip — definition + approvers preserved.
    out_wf = result.workflows[0].entry
    assert out_wf.alias == "thermal_baseline"
    assert out_wf.name == "Thermal Baseline"
    assert out_wf.approvers == ["alice@funisoft.com", "bob@funisoft.com"]
    assert out_wf.definition == {
        "members": ["material_properties", "heat_transfer"],
        "ordering": ["material_properties", "heat_transfer"],
    }


def test_pack_recomputes_file_hashes_from_actual_bytes() -> None:
    """Even if the caller hands in a manifest with no/stale hashes, the
    packer rebuilds them from the real bytes — so the signed manifest
    always describes what the tarball actually contains."""
    kp = KeyPair.generate()
    bundle = _pack_default(kp)
    result = unpack_bundle(bundle, verifier=_make_verifier())
    mp_entry = next(t.entry for t in result.tools if t.entry.alias == "material_properties")
    # All three files have hash entries.
    assert set(mp_entry.files) == {
        "descriptor.toml",
        "static/main_model.py",
        "static/inputs.template",
    }
    for hex_hash in mp_entry.files.values():
        assert len(hex_hash) == 64
        assert all(c in "0123456789abcdef" for c in hex_hash)


def test_bundle_is_byte_stable_for_same_input() -> None:
    """Same logical input → same bytes. Lets idempotent re-export be
    detectable by hashing the bundle."""
    kp = KeyPair.generate()
    fixed = lambda: __import__("datetime").datetime(2026, 5, 15, 14, 22, 8, tzinfo=__import__("datetime").timezone.utc)

    from matofx.migrate import bundle as bundle_mod

    monkey_clock = fixed
    b1 = pack_bundle(
        source_install_id="staging-install-uuid",
        source_fingerprint=compute_fingerprint(kp.public_key),
        source_public_key_hex=public_key_to_hex(kp.public_key),
        created_by="alice@funisoft.com",
        notes="test bundle",
        tools=_sample_tools(),
        workflows=_sample_workflows(),
        signer=_make_signer(kp),
        clock=monkey_clock,
    )
    b2 = pack_bundle(
        source_install_id="staging-install-uuid",
        source_fingerprint=compute_fingerprint(kp.public_key),
        source_public_key_hex=public_key_to_hex(kp.public_key),
        created_by="alice@funisoft.com",
        notes="test bundle",
        tools=_sample_tools(),
        workflows=_sample_workflows(),
        signer=_make_signer(kp),
        clock=monkey_clock,
    )
    # Manifest member is deterministic; signature is also deterministic for Ed25519.
    assert _read_member(b1, MANIFEST_MEMBER) == _read_member(b2, MANIFEST_MEMBER)
    assert _read_member(b1, SIGNATURE_MEMBER) == _read_member(b2, SIGNATURE_MEMBER)


# ---------------------------------------------------------------------------
# Tampering and corruption
# ---------------------------------------------------------------------------


def test_unpack_rejects_tampered_manifest() -> None:
    kp = KeyPair.generate()
    bundle = _pack_default(kp)
    manifest = _read_member(bundle, MANIFEST_MEMBER)
    tampered = manifest.replace(b'"test bundle"', b'"evil bundle"')
    assert tampered != manifest  # sanity check
    bad_bundle = _rewrite_member(bundle, MANIFEST_MEMBER, tampered)
    with pytest.raises(BundleSigningError) as exc_info:
        unpack_bundle(bad_bundle, verifier=_make_verifier())
    assert "signature does not match" in str(exc_info.value).lower()


def test_unpack_rejects_tampered_tool_file() -> None:
    kp = KeyPair.generate()
    bundle = _pack_default(kp)
    target = "tools/material_properties-1.5/static/main_model.py"
    tampered = b"import os\nos.system('evil')\n"
    bad_bundle = _rewrite_member(bundle, target, tampered)
    with pytest.raises(BundleError) as exc_info:
        unpack_bundle(bad_bundle, verifier=_make_verifier())
    msg = str(exc_info.value)
    assert "tampered" in msg.lower()
    assert "material_properties" in msg


def test_unpack_rejects_corrupted_signature() -> None:
    kp = KeyPair.generate()
    bundle = _pack_default(kp)
    sig = bytearray(_read_member(bundle, SIGNATURE_MEMBER))
    sig[0] ^= 0xFF
    bad_bundle = _rewrite_member(bundle, SIGNATURE_MEMBER, bytes(sig))
    with pytest.raises(BundleSigningError):
        unpack_bundle(bad_bundle, verifier=_make_verifier())


def test_unpack_rejects_fingerprint_pubkey_mismatch() -> None:
    """If a malformed bundle claims fingerprint X but embeds pubkey Y,
    we refuse — otherwise the trust-list lookup would test X while the
    signature was made by Y, letting the attacker fool the trust check."""
    kp_real = KeyPair.generate()
    kp_fake = KeyPair.generate()
    # Pack with kp_real's actual pubkey but lie about the fingerprint.
    fake_fingerprint = compute_fingerprint(kp_fake.public_key)
    bundle = _pack_default(kp_real, source_fingerprint=fake_fingerprint)
    with pytest.raises(BundleSigningError) as exc_info:
        unpack_bundle(bundle, verifier=_make_verifier())
    assert "claimed source fingerprint" in str(exc_info.value).lower()


def test_unpack_rejects_extra_tarball_file_not_in_manifest() -> None:
    """Adding an unhashed file alongside the legitimate ones must fail."""
    kp = KeyPair.generate()
    bundle = _pack_default(kp)
    # Rebuild the tarball, sneaking in an extra file under an existing tool dir.
    out = io.BytesIO()
    with (
        tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as src,
        tarfile.open(fileobj=out, mode="w:gz", format=tarfile.PAX_FORMAT) as dst,
    ):
        for info in src.getmembers():
            extracted = src.extractfile(info)
            payload = extracted.read() if extracted else b""
            new_info = tarfile.TarInfo(name=info.name)
            new_info.size = len(payload)
            new_info.mtime = 0
            new_info.mode = info.mode
            new_info.type = info.type
            dst.addfile(new_info, io.BytesIO(payload))
        # Smuggle in an extra static file under the legitimate tool dir.
        smug = b"echo gotcha"
        info = tarfile.TarInfo(name="tools/material_properties-1.5/static/extra.sh")
        info.size = len(smug)
        info.mtime = 0
        info.mode = 0o644
        info.type = tarfile.REGTYPE
        dst.addfile(info, io.BytesIO(smug))
    bad_bundle = out.getvalue()
    with pytest.raises(BundleError) as exc_info:
        unpack_bundle(bad_bundle, verifier=_make_verifier())
    assert "unexpected files" in str(exc_info.value).lower() or "extra" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Malformed tarball / path traversal
# ---------------------------------------------------------------------------


def _pack_with_raw_members(members: list[tuple[str, bytes]]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for name, data in members:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o644
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def test_unpack_rejects_not_a_tarball() -> None:
    with pytest.raises(BundleError) as exc_info:
        unpack_bundle(b"this is not a tarball", verifier=_make_verifier())
    assert "not a valid gzipped tarball" in str(exc_info.value).lower()


def test_unpack_rejects_missing_manifest() -> None:
    bundle = _pack_with_raw_members([("only.txt", b"hi")])
    with pytest.raises(BundleError) as exc_info:
        unpack_bundle(bundle, verifier=_make_verifier())
    assert MANIFEST_MEMBER in str(exc_info.value)


def test_unpack_rejects_parent_traversal() -> None:
    bundle = _pack_with_raw_members(
        [
            (MANIFEST_MEMBER, b"{}"),
            (SIGNATURE_MEMBER, b"\x00" * 64),
            ("../escape.sh", b"evil"),
        ]
    )
    with pytest.raises(BundleError) as exc_info:
        unpack_bundle(bundle, verifier=_make_verifier())
    assert ".." in str(exc_info.value) or "unsafe path" in str(exc_info.value).lower()


def test_unpack_rejects_absolute_path() -> None:
    bundle = _pack_with_raw_members(
        [
            (MANIFEST_MEMBER, b"{}"),
            (SIGNATURE_MEMBER, b"\x00" * 64),
            ("/etc/passwd", b"oops"),
        ]
    )
    with pytest.raises(BundleError) as exc_info:
        unpack_bundle(bundle, verifier=_make_verifier())
    assert "absolute" in str(exc_info.value).lower()


def test_unpack_rejects_backslash_in_path() -> None:
    bundle = _pack_with_raw_members(
        [
            (MANIFEST_MEMBER, b"{}"),
            (SIGNATURE_MEMBER, b"\x00" * 64),
            ("tools\\evil\\thing", b"oops"),
        ]
    )
    with pytest.raises(BundleError) as exc_info:
        unpack_bundle(bundle, verifier=_make_verifier())
    assert "backslash" in str(exc_info.value).lower() or "forward slash" in str(exc_info.value).lower()


def test_unpack_rejects_malformed_manifest_json() -> None:
    bundle = _pack_with_raw_members(
        [
            (MANIFEST_MEMBER, b"this is not json"),
            (SIGNATURE_MEMBER, b"\x00" * 64),
        ]
    )
    with pytest.raises(BundleError) as exc_info:
        unpack_bundle(bundle, verifier=_make_verifier())
    assert "json" in str(exc_info.value).lower() or "utf-8" in str(exc_info.value).lower()


def test_unpack_rejects_manifest_failing_schema() -> None:
    bundle = _pack_with_raw_members(
        [
            (MANIFEST_MEMBER, json.dumps({"unexpected": "shape"}).encode()),
            (SIGNATURE_MEMBER, b"\x00" * 64),
        ]
    )
    with pytest.raises(BundleError) as exc_info:
        unpack_bundle(bundle, verifier=_make_verifier())
    assert "schema" in str(exc_info.value).lower()


def test_unpack_rejects_short_signature() -> None:
    bundle = _pack_with_raw_members(
        [
            (MANIFEST_MEMBER, b"{}"),
            (SIGNATURE_MEMBER, b"\x00" * 10),
        ]
    )
    with pytest.raises(BundleError) as exc_info:
        unpack_bundle(bundle, verifier=_make_verifier())
    assert "signature" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Pack-time validation
# ---------------------------------------------------------------------------


def test_pack_rejects_duplicate_tool_versions() -> None:
    kp = KeyPair.generate()
    tools = _sample_tools()
    dup = ToolPayload(
        entry=ToolManifestEntry(alias="material_properties", version="1.5"),
        files={"descriptor.toml": b"x"},
    )
    with pytest.raises(BundleError) as exc_info:
        pack_bundle(
            source_install_id="x",
            source_fingerprint=compute_fingerprint(kp.public_key),
            source_public_key_hex=public_key_to_hex(kp.public_key),
            created_by="alice",
            notes="",
            tools=[*tools, dup],
            workflows=[],
            signer=_make_signer(kp),
        )
    assert "duplicate" in str(exc_info.value).lower()
    assert "material_properties:1.5" in str(exc_info.value)


def test_pack_rejects_duplicate_workflow_aliases() -> None:
    kp = KeyPair.generate()
    wfs = _sample_workflows()
    dup = WorkflowPayload(
        entry=WorkflowManifestEntry(alias="thermal_baseline", name="dup"),
    )
    with pytest.raises(BundleError) as exc_info:
        pack_bundle(
            source_install_id="x",
            source_fingerprint=compute_fingerprint(kp.public_key),
            source_public_key_hex=public_key_to_hex(kp.public_key),
            created_by="alice",
            notes="",
            tools=[],
            workflows=[*wfs, dup],
            signer=_make_signer(kp),
        )
    assert "duplicate" in str(exc_info.value).lower()


def test_pack_rejects_bad_signer_output() -> None:
    kp = KeyPair.generate()
    with pytest.raises(BundleSigningError):
        pack_bundle(
            source_install_id="x",
            source_fingerprint=compute_fingerprint(kp.public_key),
            source_public_key_hex=public_key_to_hex(kp.public_key),
            created_by="alice",
            notes="",
            tools=[],
            workflows=[],
            signer=lambda _: b"too short",
        )


# ---------------------------------------------------------------------------
# Manifest schema validation at parse time
# ---------------------------------------------------------------------------


def test_tool_entry_rejects_unsafe_alias() -> None:
    with pytest.raises(Exception):
        ToolManifestEntry(alias="../evil", version="1.0")


def test_tool_entry_rejects_unsafe_version() -> None:
    with pytest.raises(Exception):
        ToolManifestEntry(alias="ok", version="1.0/etc")


def test_tool_entry_rejects_bad_file_hash() -> None:
    with pytest.raises(Exception):
        ToolManifestEntry(
            alias="ok", version="1.0", files={"descriptor.toml": "nothex"}
        )
