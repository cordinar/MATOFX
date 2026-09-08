"""Mid-term licence changes take effect without a restart.

Licences are yearly, but they are also re-issued *during* a year to add
users or features. Two defects made that not work, both found by running
the flow against a live server rather than by reading the code:

* **Features were frozen at process start.** `make_app` resolved the
  licence once and baked the feature set into `Settings`, so a licence
  issued to switch a feature on did nothing until somebody restarted —
  silently, right after the administrator was told it installed fine.
  The slot count from the same file applied immediately, so "add users"
  worked while "add features" did not.

* **A tampered licence did not fail closed on a running process.**
  `LicenceResolution.read_only` returned ``not fail_closed``, on the
  reasoning that tampering is handled by refusing to boot. There is no
  boot in the middle of a process's life, so editing ``n_slots`` in the
  file removed the concurrency cap and left writes flowing until the
  next restart.

The second is the one to keep an eye on: it made a text edit cheaper
than the ceiling ADR-0019 §A4 knowingly accepts.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from matofx.config import Settings
from matofx.features import Feature, feature_enabled
from matofx.licence.live import live_settings, reset_cache
from matofx.licence.service import resolve_licence
from tests.licence.conftest import write_licence


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    """The resolver caches on the licence file's mtime/size."""
    reset_cache()


def _features(settings: Settings) -> set[str]:
    return set(live_settings(settings).features.enabled)


# ----- features follow the file ---------------------------------------


def test_a_new_licence_adds_a_feature_without_a_restart(
    settings: Settings, licence_file: Path, issuer: Ed25519PrivateKey
) -> None:
    """The mid-term upgrade case: same install, more features."""
    assert _features(settings) == {"parametric_studies"}

    write_licence(
        licence_file,
        issuer_private=issuer,
        features={"parametric_studies": True, "optimisation_framework": True},
    )

    assert _features(settings) == {"parametric_studies", "optimisation_framework"}


def test_a_new_licence_removes_a_feature_without_a_restart(
    settings: Settings, licence_file: Path, issuer: Ed25519PrivateKey
) -> None:
    """And the reverse, or a downgrade would be unenforceable."""
    write_licence(
        licence_file,
        issuer_private=issuer,
        features={"parametric_studies": True, "optimisation_framework": True},
    )
    assert "optimisation_framework" in _features(settings)

    write_licence(licence_file, issuer_private=issuer, features={"parametric_studies": True})

    assert _features(settings) == {"parametric_studies"}


def test_an_unchanged_file_is_not_re_verified(settings: Settings) -> None:
    """The steady-state cost must stay a stat, not a signature check."""
    first = live_settings(settings)
    second = live_settings(settings)
    assert first is second


def test_no_licence_configured_leaves_toml_features_alone(
    settings: Settings,
) -> None:
    """The dev/CI exemption: unlicensed installs keep [features]."""
    unlicensed = settings.model_copy(
        update={"licence": settings.licence.model_copy(update={"file": None})}
    )
    assert live_settings(unlicensed) is unlicensed


# ----- a broken licence must never widen anything ---------------------


def test_a_tampered_licence_does_not_broaden_features(
    settings: Settings, licence_file: Path, issuer: Ed25519PrivateKey
) -> None:
    """Falling back to the TOML set would let tampering *add* features.

    The install keeps whatever it last resolved instead.
    """
    write_licence(
        licence_file,
        issuer_private=issuer,
        features={"parametric_studies": True},
    )
    before = _features(settings)

    write_licence(licence_file, issuer_private=issuer, tamper=True)

    assert _features(settings) == before


def test_a_deleted_licence_does_not_broaden_features(
    settings: Settings, licence_file: Path
) -> None:
    before = _features(settings)
    licence_file.unlink()
    assert _features(settings) == before


# ----- fail-closed applies to a running process -----------------------


def test_a_tampered_licence_is_read_only(
    settings: Settings, licence_file: Path, issuer: Ed25519PrivateKey
) -> None:
    """The write gate reads this; before the fix it returned False.

    Asserting `fail_closed` as well, because the two carry different
    messages: a lapsed licence is renewed, an invalid one is restored.
    """
    write_licence(licence_file, issuer_private=issuer, tamper=True)

    resolution = resolve_licence(settings)

    assert resolution.status is None
    assert resolution.fail_closed is True
    assert resolution.read_only is True


def test_an_unsigned_licence_is_refused_now_the_keys_are_minted(
    settings: Settings, licence_file: Path
) -> None:
    """An unsigned licence fails closed — including here, in source.

    This test used to assert the opposite, and the change is the point.
    Issuer enforcement keys off whether the *build* carries Funis keys
    (§16.57), and until 2026-08-13 this tree carried none, so an unsigned
    licence was accepted and merely flagged unverified. The keys are now
    baked into `compliance.issuer` (P-08), so this tree is a build that
    can verify — and therefore must.

    Kept pointed at the real map rather than monkeypatching one in: this
    is the assertion that would notice if the keys were ever removed from
    source, which would silently restore the tolerant path for every
    release built afterwards.
    """
    write_licence(licence_file, issuer_private=None)

    resolution = resolve_licence(settings)

    assert resolution.read_only is True
    assert resolution.fail_closed is True


def test_an_unsigned_licence_is_tolerated_by_a_build_with_no_keys(
    settings: Settings, licence_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The key-less build stays tolerant — for a fork, not for us.

    With no keys baked in there is nothing to verify against, so demanding
    a signature would make such a checkout unusable. That path is still
    live code in `issuer_required`, so it is still covered; it just needs
    the map monkeypatched empty now that this repository ships real keys.

    Pinned because it looks like a hole and is not one — the hole was the
    old rule, where `environment = "dev"` in the customer's own config
    switched verification off.
    """
    from matofx.licence import loader as loader_module  # noqa: PLC0415

    monkeypatch.setattr(loader_module, "TRUSTED_ISSUER_KEYS", {})
    write_licence(licence_file, issuer_private=None)

    resolution = resolve_licence(settings)

    assert resolution.read_only is False
    assert resolution.loaded is not None
    assert resolution.loaded.issuer_verified is False


def test_a_missing_licence_is_read_only_but_not_fail_closed(
    settings: Settings, licence_file: Path
) -> None:
    """The distinction has to survive: a lapse is not tampering."""
    licence_file.unlink()

    resolution = resolve_licence(settings)

    assert resolution.read_only is True
    assert resolution.fail_closed is False


def test_a_healthy_licence_is_writable(settings: Settings) -> None:
    """The false-positive case, asserted deliberately."""
    resolution = resolve_licence(settings)

    assert resolution.read_only is False
    assert feature_enabled(live_settings(settings).features, Feature.PARAMETRIC_STUDIES)


def test_a_same_size_same_mtime_replacement_is_still_noticed(
    settings: Settings, licence_file: Path, issuer: Ed25519PrivateKey
) -> None:
    """The cache keys on content, not on ``stat``.

    Found by stress-testing on 2026-08-12. Timestamps are preservable
    (``cp -p``, ``rsync --times``, a timestamp-preserving restore) and
    two licences differing only in a slot count are frequently the same
    byte length, so ``(mtime, size)`` collided on a genuinely different
    file.

    The stale value is the *old* one, so this was never an attacker
    gaining features — it was an administrator installing a licence and
    watching the features they had just paid for fail to appear.
    """
    write_licence(licence_file, issuer_private=issuer, features={"parametric_studies": True})
    assert _features(settings) == {"parametric_studies"}
    before = licence_file.stat()

    write_licence(
        licence_file,
        issuer_private=issuer,
        features={"parametric_studies": True, "optimisation_framework": True},
    )
    # Put the timestamp back exactly, as a preserving copy would.
    os.utime(licence_file, ns=(before.st_atime_ns, before.st_mtime_ns))

    assert _features(settings) == {"parametric_studies", "optimisation_framework"}
