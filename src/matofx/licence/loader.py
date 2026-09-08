"""Read, verify and trust a licence file (brief §C16, ADR-0019 §C11).

Four gates, in order, and the order matters — each one's error message
is only meaningful once the previous gate has passed:

1. **Readable** — the file exists and is JSON.
2. **Trusted** — the issuer signature verifies under a Funis key this
   build trusts. Everything after this point is asserting things Funis
   said, not things the customer's file claims.
3. **Structurally valid** — required fields, known licence type, sane
   slot count and dates (:func:`matofx.licence.models.parse_manifest`).
4. **For this host** — if the manifest declares a machine binding, the
   observed fingerprint must match.

All four failing modes are :class:`LicenceInvalidError`, which the
enforcement layer treats as fail-closed. A *missing* file is the
separate :class:`LicenceMissingError` — an administrative lapse, not
tampering (ADR-0019 §C11).

Verification is deliberately re-run on every load rather than trusted
from the database: the file on disk is the authority, and slice 3's
periodic re-check exists precisely to notice it changing underfoot.

## What decides whether gate 2 is enforced

:func:`issuer_required` — and deliberately **not** the ``environment``
setting on its own. ``environment`` lives in the customer's
``matofx.toml``, so making it the switch meant one word turned issuer
verification off:

    environment = "dev"   # was "prod"

…after which an unsigned licence, or one signed by a key this build has
never heard of, was accepted with ``issuer_verified: false``. Writing
yourself a licence with any slot count and every feature took a text
edit. That is a far cheaper bypass than the ceiling ADR-0019 §A4 accepts
(extract the report key, rebuild the chain, and keep every consistency
proof aligned for ever), and the two should not coexist.

The switch is a property of the **build**: an artefact carrying Funis
issuer keys in :data:`matofx.compliance.issuer.TRUSTED_ISSUER_KEYS`
always demands a signature it can verify, whatever the config says.

**Since 2026-08-13 that map is populated in source**, so this tree is
always strict — dev, CI and release alike. The "empty map ⇒ tolerant
source checkout" escape hatch described here previously no longer
applies to this repository, and the tolerant path is now reachable only
by monkeypatching the map (which the tests do, deliberately, to cover
the behaviour for a fork or a future empty-map build). Local development
is unaffected in practice: an install with no ``[licence].file`` is
never degraded at all — see ``api.readonly._licence_enforced``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from matofx.compliance.canonical import canonical_bytes
from matofx.compliance.issuer import TRUSTED_ISSUER_KEYS
from matofx.licence.errors import LicenceInvalidError, LicenceMissingError
from matofx.licence.machine import machine_fingerprint
from matofx.licence.models import LicenceManifest, parse_manifest


@dataclass(frozen=True)
class LoadedLicence:
    """A verified manifest plus how it was verified."""

    manifest: LicenceManifest
    issuer_key_id: str
    issuer_verified: bool
    file_sha256: str
    source_path: Path


def issuer_required(environment: str) -> bool:
    """Whether gate 2 is enforced for this build.

    ``True`` when the build carries Funis issuer keys, or when the
    install declares itself production. The first condition is the
    load-bearing one, because it is a property of the *artefact* and
    nothing in the customer's config can switch it off. The second is
    kept so a build without keys still can't run production tolerantly.

    Since the keys were minted (2026-08-13) the first condition holds
    for every build of this tree, so in practice this returns ``True``
    always. It is kept as a condition rather than collapsed to ``True``
    because it is the honest statement of the rule — and because a fork
    or a future key-less build must still get the tolerant behaviour
    rather than a hard-coded lie.

    Note what is deliberately absent: any way for ``matofx.toml`` to make
    a release build tolerant. That was the bypass this function exists to
    remove.
    """
    return bool(TRUSTED_ISSUER_KEYS) or environment == "prod"


def load_licence_file(
    path: Path,
    *,
    extra_issuer_keys: dict[str, str] | None = None,
    require_issuer: bool = True,
    check_machine_binding: bool = True,
) -> LoadedLicence:
    """Load + verify the licence at ``path``.

    Args:
        path: The licence JSON file.
        extra_issuer_keys: Additional trusted issuer keys from
            ``[compliance].issuer_public_keys`` (the same trust root as
            install identities — one issuer, one map).
        require_issuer: True in production; when False an unverifiable
            signature is tolerated and recorded as ``issuer_verified =
            False`` so it can never be mistaken for a verified one.
        check_machine_binding: Off only for tooling that inspects a
            manifest issued for a different host.

    Raises:
        LicenceMissingError: No file at ``path``.
        LicenceInvalidError: Any of the four gates above.
    """
    raw = _read(path)
    document = _parse_json(raw, path)
    payload = document.get("licence")
    if not isinstance(payload, dict):
        raise LicenceInvalidError(
            f"{path} has no 'licence' object — this does not look like a MATOFX licence "
            "file. Check you were sent the licence, not another JSON file."
        )

    issuer_key_id, issuer_verified = _check_issuer(
        document,
        payload,
        path=path,
        extra_issuer_keys=extra_issuer_keys or {},
        require_issuer=require_issuer,
    )
    manifest = parse_manifest(payload, source=str(path))
    if check_machine_binding:
        _check_machine_binding(manifest, path=path)

    return LoadedLicence(
        manifest=manifest,
        issuer_key_id=issuer_key_id,
        issuer_verified=issuer_verified,
        file_sha256=hashlib.sha256(raw).hexdigest(),
        source_path=path,
    )


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise LicenceMissingError(
            f"No licence file at {path}. Put the licence your supplier issued there, or run "
            "'matofx licence install <path>'. Until then the site runs read-only."
        ) from exc
    except OSError as exc:
        raise LicenceInvalidError(f"Could not read the licence file {path}: {exc}") from exc


def _parse_json(raw: bytes, path: Path) -> dict[str, Any]:
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LicenceInvalidError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise LicenceInvalidError(f"{path} must contain a JSON object at the top level.")
    return document


def _check_issuer(
    document: dict[str, Any],
    payload: dict[str, Any],
    *,
    path: Path,
    extra_issuer_keys: dict[str, str],
    require_issuer: bool,
) -> tuple[str, bool]:
    trusted = {**TRUSTED_ISSUER_KEYS, **extra_issuer_keys}
    issuer = document.get("issuer")
    issuer_map = issuer if isinstance(issuer, dict) else {}
    key_id = str(issuer_map.get("key_id") or "")
    signature = str(issuer_map.get("signature") or "")

    if not key_id or not signature:
        if require_issuer:
            raise LicenceInvalidError(
                f"{path} carries no issuer signature. MATOFX will not run on an unsigned "
                "licence — ask your supplier to re-issue it."
            )
        return (key_id or "unsigned", False)

    public_hex = trusted.get(key_id)
    if public_hex is None:
        if require_issuer:
            raise LicenceInvalidError(
                f"{path} is signed by issuer key {key_id!r}, which this MATOFX build does "
                "not trust. Either the file is not from your supplier, or this build "
                "predates the key — upgrading MATOFX is the usual fix."
            )
        return (key_id, False)

    try:
        public = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        public.verify(bytes.fromhex(signature), canonical_bytes({"licence": payload}))
    except (ValueError, InvalidSignature) as exc:
        raise LicenceInvalidError(
            f"{path}: the issuer signature is not valid. The licence has been altered "
            "since it was signed — MATOFX will not run on it."
        ) from exc
    return (key_id, True)


def _check_machine_binding(manifest: LicenceManifest, *, path: Path) -> None:
    """Compare the declared binding against this host's fingerprint."""
    if manifest.machine_binding is None:
        return
    observed = machine_fingerprint()
    if observed is None:
        raise LicenceInvalidError(
            f"{path}: this licence is bound to a specific machine, but this host has no "
            "readable machine id (common in minimal containers). Ask your supplier for a "
            "licence without machine binding."
        )
    if observed != manifest.machine_binding:
        raise LicenceInvalidError(
            f"{path}: this licence was issued for a different machine. If you have moved "
            "MATOFX to a new host, ask your supplier to re-issue the licence for it."
        )
