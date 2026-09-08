"""Manifest parsing, trust, machine binding, and the expiry timeline.

No database — these are the pure gates. The distinction under test
throughout is ADR-0019 §C11's: **tampering fails closed, expiry does
not**. A test that blurred those two would let a lapsed renewal take a
customer's site down, which §C16 explicitly forbids.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from matofx.licence import (
    LicenceInvalidError,
    LicenceMissingError,
    load_licence_file,
    loader,
    parse_manifest,
)
from matofx.licence.models import DEFAULT_COMPLIANCE_MONTHS, MANIFEST_SCHEMA_VERSION
from tests.licence.conftest import ISSUER_KEY_ID, public_hex, write_licence


def _keys(issuer: Ed25519PrivateKey) -> dict[str, str]:
    return {ISSUER_KEY_ID: public_hex(issuer)}


# ----- trust ----------------------------------------------------------


def test_valid_licence_loads(licence_file: Path, issuer: Ed25519PrivateKey) -> None:
    loaded = load_licence_file(licence_file, extra_issuer_keys=_keys(issuer))
    assert loaded.manifest.licence_id == "LIC-TEST-0001"
    assert loaded.manifest.licence_type == "csl"
    assert loaded.manifest.n_slots == 10
    assert loaded.issuer_verified is True
    assert loaded.manifest.enabled_features() == ["parametric_studies"]


def test_edited_after_signing_is_refused(tmp_path: Path, issuer: Ed25519PrivateKey) -> None:
    """Inflating n_slots after signing is the obvious attack."""
    path = write_licence(tmp_path / "l.json", issuer_private=issuer, tamper=True)
    with pytest.raises(LicenceInvalidError, match="altered since it was signed"):
        load_licence_file(path, extra_issuer_keys=_keys(issuer))


def test_untrusted_issuer_is_refused(tmp_path: Path, issuer: Ed25519PrivateKey) -> None:
    """A self-minted issuer key gets nowhere."""
    path = write_licence(
        tmp_path / "l.json", issuer_private=Ed25519PrivateKey.generate(), issuer_key_id="rogue"
    )
    with pytest.raises(LicenceInvalidError, match="does not trust"):
        load_licence_file(path, extra_issuer_keys=_keys(issuer))


def test_unsigned_licence_refused_when_required(tmp_path: Path) -> None:
    path = write_licence(tmp_path / "l.json", issuer_private=None)
    with pytest.raises(LicenceInvalidError, match="no issuer signature"):
        load_licence_file(path, require_issuer=True)


def test_unsigned_licence_allowed_outside_prod_but_flagged(tmp_path: Path) -> None:
    path = write_licence(tmp_path / "l.json", issuer_private=None)
    loaded = load_licence_file(path, require_issuer=False)
    assert loaded.issuer_verified is False


def test_missing_file_is_a_different_error_class(tmp_path: Path) -> None:
    """Missing → read-only; invalid → fail closed. They must not be conflated."""
    with pytest.raises(LicenceMissingError):
        load_licence_file(tmp_path / "absent.json")


def test_not_a_licence_file(tmp_path: Path) -> None:
    path = tmp_path / "l.json"
    path.write_text('{"something": "else"}', encoding="utf-8")
    with pytest.raises(LicenceInvalidError, match="no 'licence' object"):
        load_licence_file(path, require_issuer=False)


# ----- structure ------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"drop": ("licence_id",)}, "missing required field"),
        ({"drop": ("expires_at",)}, "missing required field"),
        ({"overrides": {"licence_type": "perpetual"}}, "unknown licence_type"),
        ({"overrides": {"n_slots": 0}}, "positive whole number"),
        ({"overrides": {"n_slots": "ten"}}, "positive whole number"),
        ({"overrides": {"grace_period_days": -1}}, "cannot be negative"),
        ({"overrides": {"expires_at": "not-a-date"}}, "not an ISO-8601"),
        ({"overrides": {"features": ["a", "b"]}}, "map of slug"),
    ],
)
def test_structural_failures(
    tmp_path: Path, issuer: Ed25519PrivateKey, kwargs: dict, match: str
) -> None:
    path = write_licence(tmp_path / "l.json", issuer_private=issuer, **kwargs)
    with pytest.raises(LicenceInvalidError, match=match):
        load_licence_file(path, extra_issuer_keys=_keys(issuer))


def test_expiry_before_issue_is_refused(tmp_path: Path, issuer: Ed25519PrivateKey) -> None:
    path = write_licence(
        tmp_path / "l.json",
        issuer_private=issuer,
        issued_at="2026-06-01T00:00:00Z",
        expires_at="2026-01-01T00:00:00Z",
    )
    with pytest.raises(LicenceInvalidError, match="is not after issued_at"):
        load_licence_file(path, extra_issuer_keys=_keys(issuer))


def test_newer_schema_version_refuses_rather_than_guessing() -> None:
    with pytest.raises(LicenceInvalidError, match="newer than this MATOFX build"):
        parse_manifest(
            {
                "schema_version": MANIFEST_SCHEMA_VERSION + 1,
                "licence_id": "L",
                "customer": "C",
                "licence_type": "csl",
                "n_slots": 1,
                "issued_at": "2026-01-01T00:00:00Z",
                "expires_at": "2027-01-01T00:00:00Z",
            },
            source="test",
        )


def test_unknown_fields_are_preserved_not_dropped() -> None:
    """A future field must survive an older install without changing the signed bytes."""
    manifest = parse_manifest(
        {
            "licence_id": "L",
            "customer": "C",
            "licence_type": "fal",
            "n_slots": 25,
            "issued_at": "2026-01-01T00:00:00Z",
            "expires_at": "2027-01-01T00:00:00Z",
            "future_field": {"nested": 1},
        },
        source="test",
    )
    assert manifest.extra == {"future_field": {"nested": 1}}
    assert manifest.licence_type == "fal"


# ----- machine binding ------------------------------------------------


def test_machine_bound_licence_for_another_host_is_refused(
    tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    path = write_licence(tmp_path / "l.json", issuer_private=issuer, machine_binding="ab" * 32)
    with pytest.raises(LicenceInvalidError, match="issued for a different machine"):
        load_licence_file(path, extra_issuer_keys=_keys(issuer))


def test_machine_binding_check_can_be_skipped_for_inspection(
    tmp_path: Path, issuer: Ed25519PrivateKey
) -> None:
    path = write_licence(tmp_path / "l.json", issuer_private=issuer, machine_binding="ab" * 32)
    loaded = load_licence_file(path, extra_issuer_keys=_keys(issuer), check_machine_binding=False)
    assert loaded.manifest.machine_binding == "ab" * 32


def test_unreadable_machine_id_is_treated_as_a_mismatch(
    tmp_path: Path, issuer: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "We can't tell which host this is" must not satisfy "this is host X"."""
    monkeypatch.setattr(loader, "machine_fingerprint", lambda: None)
    path = write_licence(tmp_path / "l.json", issuer_private=issuer, machine_binding="ab" * 32)
    with pytest.raises(LicenceInvalidError, match="no readable machine id"):
        load_licence_file(path, extra_issuer_keys=_keys(issuer))


def test_matching_machine_binding_passes(
    tmp_path: Path, issuer: Ed25519PrivateKey, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(loader, "machine_fingerprint", lambda: "cd" * 32)
    path = write_licence(tmp_path / "l.json", issuer_private=issuer, machine_binding="cd" * 32)
    assert load_licence_file(path, extra_issuer_keys=_keys(issuer)).manifest.machine_binding


# ----- the expiry timeline (states, never exceptions) -----------------


def _manifest(expires: str, grace: int = 30) -> object:
    return parse_manifest(
        {
            "licence_id": "L",
            "customer": "C",
            "licence_type": "csl",
            "n_slots": 10,
            "issued_at": "2026-01-01T00:00:00Z",
            "expires_at": expires,
            "grace_period_days": grace,
        },
        source="test",
    )


@pytest.mark.parametrize(
    ("now", "expected_state", "expected_read_only"),
    [
        ("2026-06-01T00:00:00Z", "active", False),
        ("2026-12-31T23:00:00Z", "active", False),
        ("2027-01-05T00:00:00Z", "grace", False),  # expired, inside grace
        ("2027-01-30T23:00:00Z", "grace", False),  # last hour of grace
        ("2027-01-31T00:00:00Z", "expired", True),  # grace ends exactly here
        ("2027-02-05T00:00:00Z", "expired", True),  # past grace → read-only
    ],
)
def test_expiry_timeline(now: str, expected_state: str, expected_read_only: bool) -> None:
    manifest = _manifest("2027-01-01T00:00:00Z")
    status = manifest.evaluate(_dt.datetime.fromisoformat(now.replace("Z", "+00:00")))
    assert status.state == expected_state
    assert status.read_only is expected_read_only


def test_grace_banner_names_the_deadline_and_the_consequence() -> None:
    manifest = _manifest("2027-01-01T00:00:00Z")
    status = manifest.evaluate(_dt.datetime(2027, 1, 10, tzinfo=_dt.UTC))
    assert status.banner is not None
    assert "read-only" in status.banner
    assert "01 January 2027" in status.banner
    assert status.days_remaining == 21


def test_active_licence_has_no_banner() -> None:
    manifest = _manifest("2027-01-01T00:00:00Z")
    assert manifest.evaluate(_dt.datetime(2026, 6, 1, tzinfo=_dt.UTC)).banner is None


def test_zero_grace_goes_straight_to_read_only() -> None:
    manifest = _manifest("2027-01-01T00:00:00Z", grace=0)
    status = manifest.evaluate(_dt.datetime(2027, 1, 1, 0, 1, tzinfo=_dt.UTC))
    assert status.state == "expired"
    assert status.read_only is True


# ----- the `environment` escape hatch (ADR-0019 Gap 1) ----------------


def test_a_release_build_demands_a_signature_whatever_the_config_says(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`environment = "dev"` must not switch issuer verification off.

    ``environment`` lives in the customer's ``matofx.toml``. While it was
    the switch, one word turned gate 2 off and a self-written licence with
    any slot count was accepted. That is a far cheaper bypass than the
    ceiling ADR-0019 §A4 knowingly accepts, so the switch is now a
    property of the build: an artefact carrying Funis keys always demands
    a signature it can verify.
    """
    from matofx.licence import loader as loader_module  # noqa: PLC0415

    monkeypatch.setattr(loader_module, "TRUSTED_ISSUER_KEYS", {"funis-2026-01": "ab" * 32})
    for environment in ("dev", "test", "staging", "prod"):
        assert loader_module.issuer_required(environment) is True, (
            f"a release build must enforce the issuer signature in {environment!r}"
        )


def test_a_source_checkout_stays_tolerant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Development and CI are the only thing the old behaviour was for.

    With no keys baked in there is nothing to verify against, so demanding
    a signature would make every checkout unusable — which is why the old
    rule existed at all. That case is preserved; only the customer-editable
    switch is gone.
    """
    from matofx.licence import loader as loader_module  # noqa: PLC0415

    monkeypatch.setattr(loader_module, "TRUSTED_ISSUER_KEYS", {})
    assert loader_module.issuer_required("dev") is False
    assert loader_module.issuer_required("test") is False
    # A source-run production install is still held to the rule.
    assert loader_module.issuer_required("prod") is True


def test_an_unsigned_licence_is_refused_by_a_release_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, issuer: Ed25519PrivateKey
) -> None:
    """The whole attack, end to end: self-written licence + environment=dev."""
    import json as _json  # noqa: PLC0415

    from matofx.licence import loader as loader_module  # noqa: PLC0415
    from matofx.licence.errors import LicenceInvalidError  # noqa: PLC0415

    signed = write_licence(tmp_path / "real.json", issuer_private=issuer, n_slots=10)
    document = _json.loads(signed.read_text(encoding="utf-8"))
    # The customer rewrites the terms and drops the signature entirely.
    document["licence"]["n_slots"] = 10_000
    document.pop("issuer", None)
    forged = tmp_path / "forged.json"
    forged.write_text(_json.dumps(document, indent=2), encoding="utf-8")

    monkeypatch.setattr(loader_module, "TRUSTED_ISSUER_KEYS", {"funis-2026-01": "ab" * 32})
    with pytest.raises(LicenceInvalidError, match="no issuer signature"):
        loader_module.load_licence_file(forged, require_issuer=loader_module.issuer_required("dev"))


# ----- the compliance-reporting cadence -------------------------------
#
# A reporting cadence is a term of the agreement, so it rides on the
# signed manifest rather than sitting in site settings where an
# administrator could quietly extend the interval that exists to prompt
# them. It drives a banner and nothing else — see
# ``matofx.compliance.schedule``.


def _with_compliance(value: object | None) -> object:
    payload: dict[str, object] = {
        "licence_id": "L",
        "customer": "C",
        "licence_type": "csl",
        "n_slots": 10,
        "issued_at": "2026-01-01T00:00:00Z",
        "expires_at": "2027-01-01T00:00:00Z",
    }
    if value is not None:
        payload["compliance_report_months"] = value
    return parse_manifest(payload, source="test")


def test_compliance_months_defaults_to_quarterly_when_absent() -> None:
    """Every licence issued before the field existed still gets a cadence."""
    assert _with_compliance(None).compliance_report_months == DEFAULT_COMPLIANCE_MONTHS
    assert DEFAULT_COMPLIANCE_MONTHS == 3


def test_compliance_months_is_read_from_the_manifest() -> None:
    assert _with_compliance(6).compliance_report_months == 6


def test_zero_compliance_months_switches_the_reminder_off() -> None:
    """Not the same as absent — this is a deliberate "do not remind"."""
    assert _with_compliance(0).compliance_report_months == 0


def test_a_negative_cadence_is_refused_with_the_way_out() -> None:
    with pytest.raises(LicenceInvalidError, match="use 0 to turn the reminder off"):
        _with_compliance(-1)


def test_compliance_months_is_a_known_key_not_an_unknown_one() -> None:
    """Otherwise it lands in ``extra`` and is silently ignored."""
    manifest = _with_compliance(6)
    assert "compliance_report_months" not in manifest.extra
