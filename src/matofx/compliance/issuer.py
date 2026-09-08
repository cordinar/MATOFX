"""Trusted Funis issuer keys (ADR-0019 §A1).

An install-identity file is only worth anything if the app can tell that
**Funis** issued it. The public halves of the Funis issuing keys are
baked in here, in source.

**Populated 2026-08-13** with the real Funis keys (minted per
``designs/funis-vendor-runbook.md`` §1; custody in
``designs/funis-key-custody.md``). Before that the map was empty and this
docstring said release engineering would fill it in "at artefact-build
time" — a step that was never built and no longer planned. Source is the
mechanism.

**These are public halves.** There is no secrecy cost to holding them in
source, and doing so removes the failure this map exists to prevent: a
build-time injection step that silently no-ops ships an artefact that
trusts *nobody*, and the symptom is a customer's site refusing its
identity at go-live. A constant cannot silently fail to be applied.

**Consequence, deliberate:** a populated map means
:func:`matofx.licence.loader.issuer_required` is ``True`` for every run
of this tree — dev, CI and release alike — so an unsigned licence is
refused everywhere. That was previously the "source checkout" escape
hatch. Local development is unaffected in practice because an install
with no ``[licence].file`` configured is never degraded at all (see
``api/readonly.py:_licence_enforced``); what changes is that a licence
file, if present, must now actually verify.

``[compliance].issuer_public_keys`` in ``matofx.toml`` still supplements
this map for self-hosted or test issuers. It can only *add* trust, never
remove it.

**Fail-closed in production.** With no trusted key from either source,
``environment = "prod"`` refuses to adopt an identity at all; other
environments adopt it but stamp ``issuer_verified: false`` into every
report, so an unverified identity can never be mistaken for a verified
one on the Funis side.
"""

from __future__ import annotations

from typing import Final

TRUSTED_ISSUER_KEYS: Final[dict[str, str]] = {
    "funis-2026-01": "7e79b143492f1b04e134e2aab720834c4f76dd3eaaa59d4a646956f5293c84a9",
    "funis-2026-02": "3acf1620928e628a2422bab6e5db4adc5a97dcdb88222eb82e086da9357cb3b7",
}
"""Key id → Ed25519 public key, hex-encoded raw 32 bytes.

``-01`` is the **working** key: every licence and identity Funis issues
is signed with it. ``-02`` is the **backup**, and is trusted here while
never being used — that is the entire point of listing it. Because this
is a map keyed by id, a compromised working key can be retired by
issuing under the backup, and **every install already in the field
accepts the result without a software update**. Ship only ``-01`` and
there is no revocation path at all.

For the same reason, **never remove an id from this map** to "tidy up".
Old keys stay trusted until customers naturally re-issue; dropping one
invalidates every licence signed with it, retroactively.

Key ids are human-chosen and dated so a rotation is legible in the
artefacts it signed (§C16).
"""
