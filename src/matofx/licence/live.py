"""Feature set re-read from the licence while the process runs.

The slot count was always live: `resolve_licence` reads the file on every
login and every mutating request, so installing a licence with more (or
fewer) places takes effect at once. The **feature set** was not. It was
resolved once in `make_app` and frozen into `Settings`, so a licence
issued mid-term to switch a feature on did nothing until someone
restarted the server — silently, with the administrator having just been
told the licence installed successfully.

That asymmetry is the bug. Licences are yearly, but they are also
re-issued *during* a year to add users or features, and "add users"
worked while "add features" did not.

## Why this is safe to make live

Nothing registers routers conditionally on features. Every gate is a
per-request dependency reading ``settings.features`` (the optimisation
router's `_require_optimisation_feature` is the pattern), and the UI's
`_features` helpers read the same object per render. So resolving at
request time changes when the answer is computed, not where.

## Why it is cheap

The steady-state cost is reading a couple of kilobytes that are already
in page cache and hashing them. The licence is re-verified and re-parsed
— the expensive part, an Ed25519 check — only when those bytes actually
change, which is to say when somebody installs a licence.

It keyed on ``(mtime, size)`` until a stress-test on 2026-08-12 showed
that collides: timestamps are preservable (``cp -p``, ``rsync --times``,
a restore) and two licences differing only in a slot count are routinely
the same byte length. See :data:`_Fingerprint`.

## Why a bad licence does not broaden anything

If the file goes missing or stops verifying, this keeps the **last good**
feature set rather than falling back to the TOML `[features]` block.
Falling back would mean a tampered or deleted licence could *widen* what
an install runs, which is precisely backwards. The read-only middleware
independently refuses writes in that state, so the site is already
degraded; features simply stop moving.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Final

from matofx.licence.features import apply_licence_features
from matofx.licence.service import resolve_licence

if TYPE_CHECKING:
    from matofx.config import Settings

_Fingerprint = tuple[str, str] | None
"""``(path, sha256 of the bytes)``, or ``None`` when no file is configured.

Content, not ``(mtime, size)``. Timestamps are preservable — ``cp -p``,
``rsync --times``, a backup restore — and two licences differing only in
a slot count are frequently the *same byte length* (10 → 50). A
stress-test on 2026-08-12 confirmed the collision: entirely different
bytes, identical stat, identical fingerprint.

The stale value is the *old* one, so the failure is not an attacker
gaining features — it is an administrator installing a licence and
watching the features they just paid for fail to appear, with the
install insisting everything is fine. That is the honest-case failure
this design treats as the expensive one.

Hashing a licence is microseconds on a file that is a couple of
kilobytes and permanently in page cache; what the cache exists to avoid
is the Ed25519 verification and manifest parse behind it, and that
saving is unchanged.
"""

#: ``(base settings id, fingerprint, resolved settings)``. Keyed on the
#: base object's identity as well as the file, so a test or a second app
#: instance with different settings cannot read another's cache entry.
_CACHE: Final[list[tuple[int, _Fingerprint, Settings]]] = []


def _fingerprint(settings: Settings) -> _Fingerprint:
    """Content identity of the licence file, or ``None`` when unconfigured."""
    path = settings.licence.file
    if path is None:
        return None
    try:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        # Missing / unreadable: a real state (an expired install whose
        # file was removed). Fingerprint it distinctly so the resolution
        # is recomputed once, then cached like any other.
        return (str(path), "")
    return (str(path), digest)


def live_settings(base: Settings) -> Settings:
    """``base`` with the feature set resolved from the licence on disk.

    Returns ``base`` unchanged when no licence file is configured — the
    dev/CI exemption, where ``[features]`` in TOML is authoritative.
    """
    if base.licence.file is None:
        return base

    fingerprint = _fingerprint(base)
    for cached_id, cached_fp, cached in _CACHE:
        if cached_id == id(base) and cached_fp == fingerprint:
            return cached

    resolution = resolve_licence(base)
    if resolution.status is None:
        # Unreadable or unverifiable. Hold whatever we last resolved
        # rather than reverting to the TOML set — see the module
        # docstring. With nothing resolved yet, `base` is what startup
        # already applied, which is the same conservative answer.
        previous = next(
            (cached for cached_id, _, cached in _CACHE if cached_id == id(base)),
            base,
        )
        _remember(base, fingerprint, previous)
        return previous

    resolved = apply_licence_features(base, resolution)
    _remember(base, fingerprint, resolved)
    return resolved


def _remember(base: Settings, fingerprint: _Fingerprint, resolved: Settings) -> None:
    """Replace this base object's cache entry (one entry per base)."""
    _CACHE[:] = [entry for entry in _CACHE if entry[0] != id(base)]
    _CACHE.append((id(base), fingerprint, resolved))


def reset_cache() -> None:
    """Drop every cached resolution. For tests."""
    _CACHE.clear()
