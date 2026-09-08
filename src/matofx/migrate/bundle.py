"""Migration-bundle pack / unpack and the signed manifest schema.

A migration bundle is a gzipped tarball with this layout::

    manifest.json                                       # canonical JSON, signed
    manifest.sig                                        # raw Ed25519 signature (64 bytes)
    tools/<alias>-<version>/descriptor.json             # authoritative (from the DB)
    tools/<alias>-<version>/static/...                  # arbitrary blob files

Tool ``static/`` files can be megabytes (mesh inputs, FreeCAD assets,
sample data) so they live as ordinary tarball members rather than
embedded base64 in the manifest. **Workflow definitions are inlined**
in the manifest because they are small structured JSON; that also
removes the "tarball member without a manifest hash" exploit class.

The manifest is the only signed artefact. Per-file integrity of every
tool blob is enforced via SHA-256 hashes carried *inside* the signed
manifest — so tampering with any tool file breaks the bundle even
though the file itself isn't signed directly.

This module is **pure**:

* No DB access.
* No filesystem I/O — :func:`pack_bundle` returns bytes; :func:`unpack_bundle`
  accepts bytes. The service layer reads/writes files.
* No knowledge of the destination install's trust list. The signer /
  verifier are passed as callables so unit tests can use in-memory
  keys and so the service layer can wire in the install's persistent
  keystore without changing this module.

Per-item *content* (the structure of ``tool.settings`` or
``workflow.definition``) is opaque here — the bundle stores it
through the manifest verbatim. The service layer owns the schema of
those payloads because that schema is tightly bound to the rest of
``matofx.tools`` and ``matofx.workflows`` and would otherwise drag
those packages into bundle.py.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import io
import json
import re
import tarfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from matofx.migrate.signing import (
    FINGERPRINT_HEX_LEN,
    PUBLIC_KEY_HEX_LEN,
    BundleSigningError,
)

_DRIVE_PREFIX_LEN = 3
"""``C:/`` — the shortest Windows drive prefix worth rejecting."""

_SHA256_HEX_LEN = 64
"""A SHA-256 digest is 64 hex characters."""

BUNDLE_VERSION: Final[int] = 1
"""Schema version of the manifest. Bumped on any incompatible change."""

MANIFEST_MEMBER: Final[str] = "manifest.json"
SIGNATURE_MEMBER: Final[str] = "manifest.sig"
TOOL_PREFIX: Final[str] = "tools/"
SIGNATURE_LEN: Final[int] = 64
"""Raw Ed25519 signature length in bytes."""

# Strict allow-pattern for any in-tarball path. No leading slash, no
# parent traversal, no Windows drive letters, no backslashes, no null
# bytes. Component characters limited to a permissive but bounded set.
_PATH_COMPONENT_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._+-]+$")
_TOOL_DIR_RE: Final[re.Pattern[str]] = re.compile(
    r"^tools/([A-Za-z0-9._+-]+)-([A-Za-z0-9._+-]+)/(.+)$"
)


class BundleError(Exception):
    """Raised on any malformed-bundle condition during unpack.

    Plain-English message — the same string surfaces in the import
    status log and the CLI's stderr.
    """


# ---------------------------------------------------------------------------
# Manifest schema (Pydantic)
# ---------------------------------------------------------------------------


class ToolManifestEntry(BaseModel):
    """One tool inside a bundle manifest.

    ``files`` maps every blob file's path (relative to the tool's
    directory inside the tarball — e.g. ``"descriptor.json"`` or
    ``"static/main.py"``) to its SHA-256 hex digest. The manifest
    therefore signs the contents of every tool blob transitively.

    ``settings`` is an opaque dict the service layer fills with the
    by-name references to AOS host / HPC cluster / Python env /
    storage location / licence and the carried-over tool settings
    (resource requirements, approval policy, email-notifications flag,
    etc.). Bundle code never interprets it.
    """

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1, max_length=64)
    version: str = Field(min_length=1, max_length=64)
    files: dict[str, str] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
    state_on_source: str | None = Field(default=None, max_length=32)

    @field_validator("alias", "version")
    @classmethod
    def _safe_component(cls, value: str) -> str:
        if not _PATH_COMPONENT_RE.match(value):
            raise ValueError("must contain only letters, digits, dot, underscore, plus, and hyphen")
        return value

    @field_validator("files")
    @classmethod
    def _hashes_well_formed(cls, value: dict[str, str]) -> dict[str, str]:
        for path, hex_hash in value.items():
            _validate_relative_path(path)
            if len(hex_hash) != _SHA256_HEX_LEN or not all(
                c in "0123456789abcdef" for c in hex_hash
            ):
                raise ValueError(f"file hash for {path!r} is not a 64-char lowercase hex SHA-256")
        return value


class WorkflowManifestEntry(BaseModel):
    """One admin-released workflow inside a bundle manifest.

    Workflow definitions are inlined here rather than in separate
    tarball files: they are small structured JSON, and putting them
    inside the signed manifest removes the "tarball member without a
    manifest hash" attack surface entirely.
    """

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=255)
    definition: dict[str, Any] = Field(default_factory=dict)
    approvers: list[str] = Field(default_factory=list)
    state_on_source: str | None = Field(default=None, max_length=32)

    @field_validator("alias")
    @classmethod
    def _safe_alias(cls, value: str) -> str:
        if not _PATH_COMPONENT_RE.match(value):
            raise ValueError("must contain only letters, digits, dot, underscore, plus, and hyphen")
        return value


class BundleManifest(BaseModel):
    """The bundle's top-level signed manifest.

    Carries everything needed to verify and reconstruct the bundle's
    contents on the destination install.
    """

    model_config = ConfigDict(extra="forbid")

    bundle_version: int = Field(default=BUNDLE_VERSION, ge=1)
    source_install_id: str = Field(min_length=1, max_length=64)
    """Free-form install identifier (typically a UUID). Informational —
    verification rides on the fingerprint, not the install id."""

    source_fingerprint: str = Field(min_length=FINGERPRINT_HEX_LEN, max_length=FINGERPRINT_HEX_LEN)
    """Canonical lowercase hex of the source install's public key
    fingerprint. The destination's trust-list lookup uses this value."""

    source_public_key_hex: str = Field(min_length=PUBLIC_KEY_HEX_LEN, max_length=PUBLIC_KEY_HEX_LEN)
    """The source install's Ed25519 public key as 64-char hex. Embedded
    in the bundle so the destination can verify the signature without
    pre-shared key material; the trust list then decides whether the
    *identity* is acceptable."""

    created_at_utc: _dt.datetime
    created_by: str = Field(min_length=1, max_length=255)
    """Identity of the user who triggered the export on the source.
    Free-form (typically email or username). Recorded in the
    destination's audit log so the cross-install trail is intact."""

    notes: str = Field(default="", max_length=4000)
    tools: list[ToolManifestEntry] = Field(default_factory=list)
    workflows: list[WorkflowManifestEntry] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pack / unpack payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ToolPayload:
    """A tool's blob files alongside the manifest entry that describes it.

    ``files`` is a mapping of *relative path inside the tool's directory*
    (e.g. ``"descriptor.json"``, ``"static/main_model.py"``) to bytes.
    The pack step computes each file's SHA-256 and writes the hash into
    ``manifest.json``; the unpack step verifies the hashes match.
    """

    entry: ToolManifestEntry
    files: dict[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkflowPayload:
    """Convenience holder — workflows have no out-of-band files."""

    entry: WorkflowManifestEntry


@dataclass(frozen=True, slots=True)
class UnpackedBundle:
    """Result of :func:`unpack_bundle` — manifest + per-item payloads."""

    manifest: BundleManifest
    tools: list[ToolPayload]
    workflows: list[WorkflowPayload]


# ---------------------------------------------------------------------------
# Pack
# ---------------------------------------------------------------------------


def pack_bundle(
    *,
    source_install_id: str,
    source_fingerprint: str,
    source_public_key_hex: str,
    created_by: str,
    notes: str,
    tools: list[ToolPayload],
    workflows: list[WorkflowPayload],
    signer: Callable[[bytes], bytes],
    clock: Callable[[], _dt.datetime] | None = None,
) -> bytes:
    """Build a signed migration bundle and return the gzipped-tarball bytes.

    The caller is responsible for:

    * Resolving the install's signing key (via the persisted keystore)
      and exposing it as a ``signer(manifest_bytes) -> signature_bytes``
      callable. :mod:`matofx.migrate.signing` provides the underlying
      Ed25519 primitives.
    * Pre-computing each tool's payload ``files`` and any settings /
      reference-by-name metadata that belongs in
      :attr:`ToolManifestEntry.settings` and
      :attr:`WorkflowManifestEntry.definition`. This module does not
      look up Tool / Workflow rows.

    Duplicate aliases (within the same kind) raise :class:`BundleError`
    rather than producing a silently-corrupt bundle.
    """
    _check_unique([t.entry.alias + ":" + t.entry.version for t in tools], "tool")
    _check_unique([w.entry.alias for w in workflows], "workflow")

    # Re-derive every file hash inside the manifest from the actual
    # bytes the caller passed. This guarantees the signed manifest
    # describes the bytes the bundle actually contains, regardless of
    # whatever ``entry.files`` came in with.
    rebuilt_tools = [_rebuild_tool_entry(t) for t in tools]

    now = (clock or _utcnow)()
    manifest = BundleManifest(
        bundle_version=BUNDLE_VERSION,
        source_install_id=source_install_id,
        source_fingerprint=source_fingerprint.lower(),
        source_public_key_hex=source_public_key_hex.lower(),
        created_at_utc=now,
        created_by=created_by,
        notes=notes,
        tools=[t.entry for t in rebuilt_tools],
        workflows=[w.entry for w in workflows],
    )

    manifest_bytes = _canonical_json_bytes(manifest.model_dump(mode="json"))
    signature = signer(manifest_bytes)
    if not isinstance(signature, (bytes, bytearray)) or len(signature) != SIGNATURE_LEN:
        raise BundleSigningError(
            "Signer returned an invalid signature (expected 64 bytes of Ed25519 output)."
        )

    return _write_tarball(manifest_bytes, bytes(signature), rebuilt_tools)


def _rebuild_tool_entry(payload: ToolPayload) -> ToolPayload:
    """Return ``payload`` with its ``files`` hash map regenerated.

    The hashes come from the actual file bytes, not from what the manifest
    claimed.
    """
    fresh_hashes = {path: _sha256_hex(data) for path, data in payload.files.items()}
    # Validate path shapes early — defence in depth against a caller
    # passing exotic strings the tar layer might mishandle.
    for path in fresh_hashes:
        _validate_relative_path(path)
    new_entry = payload.entry.model_copy(update={"files": fresh_hashes})
    return ToolPayload(entry=new_entry, files=payload.files)


def _write_tarball(
    manifest_bytes: bytes,
    signature: bytes,
    tools: list[ToolPayload],
) -> bytes:
    """Write the manifest + signature + tool blobs into a gzipped tarball."""
    buf = io.BytesIO()
    # ``mtime=0`` and a fixed uname/gname keep the bundle byte-stable
    # across machines for the same logical content. This makes idempotent
    # re-exports detectable by hashing the bundle bytes.
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        _add_bytes_member(tar, MANIFEST_MEMBER, manifest_bytes)
        _add_bytes_member(tar, SIGNATURE_MEMBER, signature)
        for tool in tools:
            dir_name = f"{TOOL_PREFIX}{tool.entry.alias}-{tool.entry.version}/"
            for rel_path, data in sorted(tool.files.items()):
                _add_bytes_member(tar, dir_name + rel_path, data)
    return buf.getvalue()


def _add_bytes_member(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.mode = 0o644
    info.type = tarfile.REGTYPE
    tar.addfile(info, io.BytesIO(data))


# ---------------------------------------------------------------------------
# Unpack
# ---------------------------------------------------------------------------


def unpack_bundle(
    bundle_bytes: bytes,
    *,
    verifier: Callable[[bytes, bytes, str], None],
) -> UnpackedBundle:
    """Parse, verify, and return the contents of a migration bundle.

    The ``verifier`` callback receives ``(manifest_bytes, signature,
    source_public_key_hex)`` and must raise on signature failure. The
    canonical implementation is
    :func:`matofx.migrate.signing.verify_manifest` wrapped to derive an
    :class:`Ed25519PublicKey` from the hex — separating that out keeps
    this module crypto-agnostic and trivial to fake in tests.

    Trust-list lookup is **not** done here — the caller (service layer)
    compares ``manifest.source_fingerprint`` against the trust DB after
    this function returns successfully. Verification proves
    *authenticity*; the trust check decides *acceptability*.

    Raises :class:`BundleError` for malformed bundles and
    :class:`BundleSigningError` for signature / fingerprint mismatches.
    Both messages are plain English and surface verbatim in the UI.
    """
    members = _read_tarball(bundle_bytes)
    manifest_bytes = _require_member(members, MANIFEST_MEMBER)
    signature = _require_member(members, SIGNATURE_MEMBER)
    if len(signature) != SIGNATURE_LEN:
        raise BundleError(
            f"The bundle's signature file is the wrong size "
            f"(expected {SIGNATURE_LEN} bytes, got {len(signature)}). "
            "The bundle is corrupted."
        )

    try:
        manifest_payload = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundleError(
            "The bundle's manifest could not be read as UTF-8 JSON. "
            "The bundle is corrupted or was produced by a different "
            "tool."
        ) from exc

    try:
        manifest = BundleManifest.model_validate(manifest_payload)
    except Exception as exc:
        raise BundleError(
            f"The bundle's manifest does not match the expected schema. Details: {exc}"
        ) from exc

    verifier(manifest_bytes, signature, manifest.source_public_key_hex)

    # Sanity check: the embedded public key must hash to the claimed
    # fingerprint. This catches a malformed bundle where the manifest's
    # source_fingerprint and source_public_key_hex disagree (which would
    # let a trust-list lookup succeed against the wrong key).
    derived = hashlib.sha256(bytes.fromhex(manifest.source_public_key_hex)).hexdigest()
    if derived != manifest.source_fingerprint:
        raise BundleSigningError(
            "The bundle's claimed source fingerprint does not match the "
            "public key embedded in the bundle. The bundle is malformed "
            "and cannot be trusted."
        )

    tools = _extract_tools(manifest, members)
    workflows = [WorkflowPayload(entry=w) for w in manifest.workflows]

    return UnpackedBundle(manifest=manifest, tools=tools, workflows=workflows)


def _extract_tools(
    manifest: BundleManifest,
    members: Mapping[str, bytes],
) -> list[ToolPayload]:
    """Pull each tool's blob files out of the tar and verify their hashes."""
    tool_index: dict[tuple[str, str], dict[str, bytes]] = {}
    for member_name, data in members.items():
        if not member_name.startswith(TOOL_PREFIX):
            continue
        match = _TOOL_DIR_RE.match(member_name)
        if not match:
            raise BundleError(
                f"The bundle contains a tool file at {member_name!r} that "
                "does not follow the expected layout "
                "(tools/<alias>-<version>/<path>). The bundle is malformed."
            )
        alias, version, rel_path = match.group(1), match.group(2), match.group(3)
        _validate_relative_path(rel_path)
        tool_index.setdefault((alias, version), {})[rel_path] = data

    out: list[ToolPayload] = []
    for entry in manifest.tools:
        key = (entry.alias, entry.version)
        present = tool_index.pop(key, {})
        # Every file claimed by the manifest must be present, and every
        # file present must be claimed by the manifest — both directions
        # so an attacker can't smuggle an unhashed file alongside the
        # legitimate ones.
        manifest_paths = set(entry.files)
        tarball_paths = set(present)
        if manifest_paths != tarball_paths:
            missing = manifest_paths - tarball_paths
            extra = tarball_paths - manifest_paths
            details = []
            if missing:
                details.append(f"missing files: {sorted(missing)}")
            if extra:
                details.append(f"unexpected files: {sorted(extra)}")
            raise BundleError(
                f"Tool {entry.alias} {entry.version} has a mismatch "
                f"between its manifest and the bundle's contents: "
                f"{'; '.join(details)}. The bundle is malformed."
            )
        for rel_path, expected_hash in entry.files.items():
            actual = _sha256_hex(present[rel_path])
            if actual != expected_hash:
                raise BundleError(
                    f"Tool {entry.alias} {entry.version} has a tampered "
                    f"file at {rel_path!r} — its SHA-256 does not match "
                    "the value recorded in the signed manifest. The "
                    "bundle has been modified after it was exported."
                )
        out.append(ToolPayload(entry=entry, files=present))

    if tool_index:
        stray = sorted(f"{a}-{v}" for (a, v) in tool_index)
        raise BundleError(
            "The bundle contains tool directories that the manifest "
            f"does not describe: {stray}. The bundle is malformed."
        )
    return out


# ---------------------------------------------------------------------------
# Tar reading + path validation
# ---------------------------------------------------------------------------

# Upper bound on the *uncompressed* total we will hold in memory when
# unpacking a migration bundle. The upload cap (settings.tools.max_upload_bytes,
# default 256 MiB) bounds only the compressed bytes, so a well-formed gzip can
# expand orders of magnitude larger (a "decompression bomb") and exhaust the
# shared UI/API process. 1 GiB is far above any real tools/workflows bundle
# (descriptors + code + small assets) yet caps the blast radius. tar headers
# declare each member's size authoritatively, so we can refuse before reading.
_MAX_UNCOMPRESSED_BUNDLE_BYTES = 1024 * 1024 * 1024


def _read_tarball(bundle_bytes: bytes) -> dict[str, bytes]:
    """Read every regular-file member of the tar into memory.

    Hardened against path traversal, odd member types, and decompression
    bombs.
    """
    try:
        tar = tarfile.open(fileobj=io.BytesIO(bundle_bytes), mode="r:gz")  # noqa: SIM115 - closed in the finally below; the try wraps open + read
    except tarfile.ReadError as exc:
        raise BundleError(
            "The bundle file is not a valid gzipped tarball. "
            "It may be corrupted, truncated, or not actually a "
            "MATOFX migration bundle."
        ) from exc

    out: dict[str, bytes] = {}
    total_uncompressed = 0
    with tar:
        for info in tar.getmembers():
            if info.islnk() or info.issym():
                raise BundleError(
                    f"The bundle contains a symlink or hard link "
                    f"({info.name!r}), which is not allowed in migration "
                    "bundles."
                )
            if info.isdir():
                continue
            if not info.isreg():
                raise BundleError(
                    f"The bundle contains an entry of an unsupported "
                    f"type at {info.name!r}. Only regular files are "
                    "allowed."
                )
            _validate_tar_member_name(info.name)
            total_uncompressed += max(info.size, 0)
            if total_uncompressed > _MAX_UNCOMPRESSED_BUNDLE_BYTES:
                raise BundleError(
                    "The bundle expands to more than "
                    f"{_MAX_UNCOMPRESSED_BUNDLE_BYTES // (1024 * 1024)} MiB "
                    "uncompressed, which is refused as a decompression-bomb "
                    "guard. If this is a legitimate bundle, split it or ask an "
                    "administrator to raise the limit."
                )
            extracted = tar.extractfile(info)
            if extracted is None:
                raise BundleError(  # pragma: no cover — defence in depth
                    f"Could not read bundle member {info.name!r}."
                )
            out[info.name] = extracted.read()
    return out


def _require_member(members: Mapping[str, bytes], name: str) -> bytes:
    if name not in members:
        raise BundleError(
            f"The bundle is missing the required {name!r} member. "
            "It is not a valid MATOFX migration bundle."
        )
    return members[name]


def _validate_tar_member_name(name: str) -> None:
    """Reject absolute paths, parent traversal, drive letters, backslashes."""
    if not name:
        raise BundleError("The bundle contains an entry with an empty name.")
    if "\x00" in name:
        raise BundleError("The bundle contains an entry whose name has a null byte.")
    if name.startswith("/") or name.startswith("\\"):
        raise BundleError(
            f"The bundle contains an entry with an absolute path "
            f"({name!r}). Migration bundles must use relative paths only."
        )
    if "\\" in name:
        raise BundleError(
            f"The bundle contains an entry with a backslash in its "
            f"name ({name!r}). Migration bundles must use forward slashes."
        )
    if len(name) >= _DRIVE_PREFIX_LEN and name[1:3] == ":/":
        raise BundleError(
            f"The bundle contains an entry that looks like a Windows "
            f"drive path ({name!r}). Migration bundles must use "
            "relative paths only."
        )
    parts = name.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise BundleError(
                f"The bundle contains an entry with an unsafe path "
                f"component ({name!r}). Migration bundles must not use "
                "'.' or '..' or empty path segments."
            )
        if not _PATH_COMPONENT_RE.match(part):
            raise BundleError(
                f"The bundle contains an entry whose path has an "
                f"unsupported character ({name!r}). Path components "
                "may contain only letters, digits, dot, underscore, "
                "plus, and hyphen."
            )


def _validate_relative_path(rel: str) -> None:
    """Run the same checks against a manifest-declared relative path."""
    # _validate_tar_member_name covers everything we need.
    _validate_tar_member_name(rel)


# ---------------------------------------------------------------------------
# Canonical JSON + hashing
# ---------------------------------------------------------------------------


def _canonical_json_bytes(payload: Any) -> bytes:
    """UTF-8 bytes of a sorted, no-whitespace JSON encoding.

    Same input → same bytes byte-for-byte, on every platform and
    Python version. Required because the signature is over the
    *bytes*, not the structure.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_json_default,
    ).encode("utf-8")


def _json_default(value: Any) -> Any:
    if isinstance(value, _dt.datetime):
        # Pydantic emits ISO 8601 in mode="json" already; this is a
        # belt-and-braces fallback for anything else that slips through.
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.UTC)
        return value.isoformat()
    raise TypeError(f"Unserialisable value of type {type(value).__name__}")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(tz=_dt.UTC)


def _check_unique(items: list[str], kind: str) -> None:
    seen: set[str] = set()
    dups: list[str] = []
    for item in items:
        if item in seen:
            dups.append(item)
        else:
            seen.add(item)
    if dups:
        raise BundleError(
            f"The bundle has duplicate {kind} entries: {sorted(set(dups))}. "
            "Each (alias, version) for tools and each alias for workflows "
            "must appear at most once."
        )
