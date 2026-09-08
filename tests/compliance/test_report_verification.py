"""Vendor-side report verification (ADR-0019 §B8).

Pure-function tests over hand-built documents — no database — so the
attack cases are explicit and fast. The scenarios that matter:

- an honest follow-up report → ``CONSISTENT``
- a **rebuilt chain** that self-verifies perfectly → ``CONTRADICTED``
  (the case the whole design exists for)
- a rewound head, a replayed counter, a swapped signing key
- both licence types' breach evaluation
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from matofx.compliance import (
    CONSISTENT,
    CONTRADICTED,
    INCONCLUSIVE,
    LedgerEntry,
    ReportFormatError,
    verify_report,
)
from matofx.compliance.canonical import canonical_bytes
from matofx.compliance.merkle import consistency_proof, hex_proof, tree_hash
from matofx.compliance.schema import REPORT_SCHEMA_VERSION
from tests.compliance.conftest import issuer_public_hex

_INSTALL = "inst_TEST0001"

#: A synthetic audit chain. Anchoring is now a real RFC 6962 consistency
#: proof, so the fixtures build a real tree rather than asserting hashes:
#: a test that hand-waves the proof would pass against a verifier that
#: never checked it.
_LEAVES = [f"record-{i}".encode() for i in range(260)]
_PREV_SIZE = 120
_HEAD_SIZE = 260


def _leaves(count: int, *, rewrite_at: int | None = None) -> list[bytes]:
    """The first ``count`` leaves, optionally with one rewritten."""
    chain = list(_LEAVES[:count])
    if rewrite_at is not None:
        chain[rewrite_at] = b"rewritten"
    return chain


def _root(count: int, *, rewrite_at: int | None = None) -> str:
    return tree_hash(_leaves(count, rewrite_at=rewrite_at)).hex()


def _anchor_for(
    size: int, *, prove_from: int = _PREV_SIZE, rewrite_at: int | None = None
) -> dict[str, Any]:
    """An anchor block whose proofs are genuinely computed for ``size``."""
    chain = _leaves(size, rewrite_at=rewrite_at)
    proofs: list[dict[str, Any]] = []
    if 0 < prove_from <= size:
        proofs.append(
            {
                "counter": 1,
                "tree_size": prove_from,
                "proof": hex_proof(consistency_proof(chain, prove_from)),
            }
        )
    return {
        "genesis_record": {"id": 1, "hash": "0a" * 32},
        "previous_report": {
            "counter": 1,
            "generated_at": "2026-07-31T17:52:03.000000Z",
            "head_id": _PREV_SIZE,
            "head_hash": "a9" * 32,
        },
        "previous_head_rehashed_now": "a9" * 32,
        "head": {
            "id": size,
            "hash": "3c" * 32,
            "timestamp": "2026-08-31T17:58:40.000000Z",
        },
        "tree": {
            "algorithm": "rfc6962-sha256",
            "size": size,
            "root": _root(size, rewrite_at=rewrite_at),
        },
        "consistency_proofs": proofs,
        "consistency_proofs_truncated": False,
    }


def _document(**overrides: Any) -> dict[str, Any]:
    """A report-2 document extending a 120-record tree to 260 records."""
    document: dict[str, Any] = {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": "2026-08-31T18:04:11.000000Z",
        "counter": 2,
        "install": {
            "install_id": _INSTALL,
            "customer": "Northwind Aerospace Ltd",
            "environment": "production",
            "app_version": "1.0.3",
            "db_dialect": "postgres",
            "identity_sha256": "ab" * 32,
            "issuer_key_id": "funis-test-2026",
            "issuer_verified": True,
        },
        "licence": {"state": "not_installed", "licence_type": None, "n_slots": None},
        "period": {"from": "2026-08-01T00:00:00.000000Z", "to": "2026-08-31T18:04:11.000000Z"},
        "anchor": _anchor_for(_HEAD_SIZE),
        "chain_verification": {
            "range": {"start_id": 1, "end_id": _HEAD_SIZE},
            "records_checked": _HEAD_SIZE,
            "ok": True,
            "signing_keys_configured": ["c40b91aa"],
            "failures": [],
            "failures_truncated": False,
        },
        "usage": {
            "licence_type": None,
            "authoritative": None,
            "csl": {
                "n_slots": None,
                "peak_concurrent_sessions": 7,
                "over_cap": None,
                "sessions_started": 210,
                "slots_granted": 0,
                "slots_denied": 0,
                "breakglass_admissions": 0,
                "peak_by_day": {"2026-08-01": 5},
            },
            "fal": {
                "n_slots": None,
                "assignments_current": 41,
                "over_cap": None,
                "assignments_granted": 0,
                "assignments_revoked": 0,
                "assignments_denied": 0,
                "registered_users": 63,
                "source": "enabled_accounts",
            },
        },
        "licence_events": {"compliance.report.generated": 1},
        "notable_events": [],
        "notable_events_truncated": False,
    }
    for key, value in overrides.items():
        document[key] = value
    return document


def _sign(document: dict[str, Any], key: Ed25519PrivateKey) -> dict[str, Any]:
    signed = copy.deepcopy(document)
    signed["signature"] = {
        "alg": "ed25519",
        "key_id": "deadbeefdeadbeef",
        "value": key.sign(canonical_bytes(signed)).hex(),
        "covers": "everything except signature",
    }
    return signed


def _ledger(**overrides: Any) -> list[LedgerEntry]:
    fields: dict[str, Any] = {
        "install_id": _INSTALL,
        "counter": 1,
        "head_id": _PREV_SIZE,
        "head_hash": "a9" * 32,
        "tree_size": _PREV_SIZE,
        "tree_root": _root(_PREV_SIZE),
        "genesis_hash": "0a" * 32,
    }
    fields.update(overrides)
    return [LedgerEntry(**fields)]


@pytest.fixture
def report_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _status(verdict: Any, name: str) -> str:
    return next(c.status for c in verdict.checks if c.name == name)


def test_honest_followup_is_consistent(report_key: Ed25519PrivateKey) -> None:
    document = _sign(_document(), report_key)
    verdict = verify_report(
        document,
        ledger=_ledger(report_public_key=issuer_public_hex(report_key)),
    )
    assert verdict.verdict == CONSISTENT
    assert verdict.ok
    assert _status(verdict, "anchoring") == "ok"


def test_rebuilt_chain_is_caught_despite_self_verifying(report_key: Ed25519PrivateKey) -> None:
    """The core scenario: a perfect, internally-consistent, *different* history.

    Every local check passes — the chain self-verifies, the signature is
    valid, the counter advances. Only the value Funis holds exposes it.
    """
    # Record 40 is rewritten, then a coherent 260-record tree is built on
    # top of it and honestly proved from *its own* history.
    anchor = _anchor_for(_HEAD_SIZE, rewrite_at=40)
    anchor["genesis_record"] = {"id": 1, "hash": "91" * 32}  # new chain from scratch
    document = _sign(_document(anchor=anchor), report_key)

    verdict = verify_report(
        document, ledger=_ledger(report_public_key=issuer_public_hex(report_key))
    )

    assert verdict.verdict == CONTRADICTED
    assert _status(verdict, "chain") == "ok"  # self-verification passed!
    assert _status(verdict, "signature") == "ok"  # correctly signed!
    assert _status(verdict, "anchoring") == "fail"
    assert _status(verdict, "genesis") == "fail"
    assert "rewritten" in next(c.detail for c in verdict.checks if c.name == "anchoring")


def test_truncated_history_is_caught(report_key: Ed25519PrivateKey) -> None:
    """Records attested previously have been removed: the tree shrank."""
    document = _sign(_document(anchor=_anchor_for(90, prove_from=0)), report_key)
    verdict = verify_report(
        document, ledger=_ledger(report_public_key=issuer_public_hex(report_key))
    )
    assert verdict.verdict == CONTRADICTED
    assert "shrunk" in next(c.detail for c in verdict.checks if c.name == "anchoring")


def test_a_report_that_cannot_prove_consistency_contradicts(
    report_key: Ed25519PrivateKey,
) -> None:
    """An explicit ``null`` proof is an admission, not a missing field."""
    anchor = _anchor_for(_HEAD_SIZE)
    anchor["consistency_proofs"] = [{"counter": 1, "tree_size": _PREV_SIZE, "proof": None}]
    document = _sign(_document(anchor=anchor), report_key)
    verdict = verify_report(
        document, ledger=_ledger(report_public_key=issuer_public_hex(report_key))
    )
    assert verdict.verdict == CONTRADICTED
    assert _status(verdict, "anchoring") == "fail"


def test_a_forged_empty_proof_is_rejected(report_key: Ed25519PrivateKey) -> None:
    """Claiming extension without evidence must not verify."""
    anchor = _anchor_for(_HEAD_SIZE)
    anchor["consistency_proofs"] = [{"counter": 1, "tree_size": _PREV_SIZE, "proof": []}]
    document = _sign(_document(anchor=anchor), report_key)
    verdict = verify_report(
        document, ledger=_ledger(report_public_key=issuer_public_hex(report_key))
    )
    assert verdict.verdict == CONTRADICTED
    assert _status(verdict, "anchoring") == "fail"


def test_undelivered_reports_do_not_contradict(report_key: Ed25519PrivateKey) -> None:
    """The F1 regression: reports generated and never sent are harmless.

    The install proves from the tree size the vendor actually recorded,
    not from its own most recent report — so a preview that was never
    delivered cannot turn an honest install into a fraud verdict.
    """
    document = _sign(_document(counter=9), report_key)
    verdict = verify_report(
        document, ledger=_ledger(report_public_key=issuer_public_hex(report_key))
    )
    assert verdict.verdict == CONSISTENT
    assert _status(verdict, "anchoring") == "ok"
    assert _status(verdict, "counter") == "warn"


def test_a_baseline_with_no_records_cannot_contradict(
    report_key: Ed25519PrivateKey,
) -> None:
    """The F2 regression: a first report on an empty chain poisons nothing.

    A ledger entry that attested no records committed to nothing, so the
    follow-up report must be unprovable-either-way rather than accused of
    having deleted a record that never existed.
    """
    document = _sign(_document(), report_key)
    verdict = verify_report(
        document,
        ledger=_ledger(
            tree_size=0,
            tree_root=None,
            head_id=None,
            head_hash=None,
            genesis_hash=None,
            report_public_key=issuer_public_hex(report_key),
        ),
    )
    assert verdict.verdict == INCONCLUSIVE
    assert _status(verdict, "anchoring") == "skip"


def test_replayed_counter_is_caught(report_key: Ed25519PrivateKey) -> None:
    document = _sign(_document(counter=1), report_key)
    verdict = verify_report(
        document, ledger=_ledger(report_public_key=issuer_public_hex(report_key))
    )
    assert verdict.verdict == CONTRADICTED
    assert _status(verdict, "counter") == "fail"


def test_skipped_period_warns_but_does_not_contradict(report_key: Ed25519PrivateKey) -> None:
    """A gap is a question to ask, not proof of tampering."""
    document = _sign(_document(counter=5), report_key)
    verdict = verify_report(
        document, ledger=_ledger(report_public_key=issuer_public_hex(report_key))
    )
    assert verdict.verdict == CONSISTENT
    assert _status(verdict, "counter") == "warn"
    assert "3 report(s)" in next(c.detail for c in verdict.checks if c.name == "counter")


def test_signature_from_a_different_key_fails(report_key: Ed25519PrivateKey) -> None:
    """The customer swaps in a key of their own; the ledger pins the real one."""
    document = _sign(_document(), Ed25519PrivateKey.generate())
    verdict = verify_report(
        document, ledger=_ledger(report_public_key=issuer_public_hex(report_key))
    )
    assert verdict.verdict == CONTRADICTED
    assert _status(verdict, "signature") == "fail"


def test_edited_report_body_fails_the_signature(report_key: Ed25519PrivateKey) -> None:
    document = _sign(_document(), report_key)
    document["usage"]["csl"]["peak_concurrent_sessions"] = 3  # hide an over-cap month
    verdict = verify_report(
        document, ledger=_ledger(report_public_key=issuer_public_hex(report_key))
    )
    assert verdict.verdict == CONTRADICTED
    assert _status(verdict, "signature") == "fail"


def test_first_report_is_inconclusive_without_a_pinned_key() -> None:
    """Report 1 establishes the baseline; it cannot prove anything yet."""
    document = _sign(
        _document(counter=1, anchor=_first_report_anchor()), Ed25519PrivateKey.generate()
    )
    verdict = verify_report(document, ledger=[])
    assert verdict.verdict == INCONCLUSIVE
    assert _status(verdict, "signature") == "skip"
    assert _status(verdict, "anchoring") == "skip"


def test_first_report_with_supplied_key_still_lacks_an_anchor(
    report_key: Ed25519PrivateKey,
) -> None:
    document = _sign(_document(counter=1, anchor=_first_report_anchor()), report_key)
    verdict = verify_report(document, ledger=[], public_key_hex=issuer_public_hex(report_key))
    assert verdict.verdict == INCONCLUSIVE
    assert _status(verdict, "signature") == "ok"
    assert _status(verdict, "anchoring") == "skip"


def test_failed_self_verification_contradicts(report_key: Ed25519PrivateKey) -> None:
    chain = _document()["chain_verification"]
    chain["ok"] = False
    chain["failures"] = [{"record_id": 84999, "reason": "hash mismatch: stored ab…, computed cd…"}]
    document = _sign(_document(chain_verification=chain), report_key)
    verdict = verify_report(
        document, ledger=_ledger(report_public_key=issuer_public_hex(report_key))
    )
    assert verdict.verdict == CONTRADICTED
    assert "84999" in next(c.detail for c in verdict.checks if c.name == "chain")


def test_unverified_issuer_warns(report_key: Ed25519PrivateKey) -> None:
    install = _document()["install"]
    install["issuer_verified"] = False
    document = _sign(_document(install=install), report_key)
    verdict = verify_report(
        document, ledger=_ledger(report_public_key=issuer_public_hex(report_key))
    )
    assert _status(verdict, "issuer") == "warn"
    assert verdict.verdict == CONSISTENT  # a warning, not a contradiction


# ----- licence findings, per type ------------------------------------


def test_csl_over_cap_is_a_breach_with_breakglass_explained(
    report_key: Ed25519PrivateKey,
) -> None:
    usage = _document()["usage"]
    usage["licence_type"] = "csl"
    usage["authoritative"] = "csl"
    usage["csl"].update(
        {
            "n_slots": 10,
            "peak_concurrent_sessions": 11,
            "over_cap": True,
            "breakglass_admissions": 1,
            "slots_denied": 14,
        }
    )
    document = _sign(_document(usage=usage), report_key)
    verdict = verify_report(
        document, ledger=_ledger(report_public_key=issuer_public_hex(report_key))
    )
    breaches = [f for f in verdict.findings if f.severity == "breach"]
    signals = [f for f in verdict.findings if f.severity == "signal"]
    assert verdict.verdict == CONSISTENT  # honest self-reporting isn't tampering
    assert len(breaches) == 1
    assert "11" in breaches[0].summary
    assert "break-glass" in breaches[0].summary
    assert any("14 logins refused" in s.summary for s in signals)


def test_fal_over_cap_is_a_breach(report_key: Ed25519PrivateKey) -> None:
    usage = _document()["usage"]
    usage["licence_type"] = "fal"
    usage["authoritative"] = "fal"
    usage["fal"].update(
        {
            "n_slots": 25,
            "assignments_current": 26,
            "over_cap": True,
            "assignments_denied": 3,
            "source": "fal_assignments",
        }
    )
    document = _sign(_document(usage=usage), report_key)
    verdict = verify_report(
        document, ledger=_ledger(report_public_key=issuer_public_hex(report_key))
    )
    breaches = [f for f in verdict.findings if f.severity == "breach"]
    assert len(breaches) == 1
    assert "26 floating assignments against 25" in breaches[0].summary


def test_fal_proxy_source_is_flagged(report_key: Ed25519PrivateKey) -> None:
    """A pre-slice-5 count must not be silently compared against a real one."""
    usage = _document()["usage"]
    usage["licence_type"] = "fal"
    usage["fal"].update({"n_slots": 50, "assignments_current": 41, "over_cap": False})
    document = _sign(_document(usage=usage), report_key)
    verdict = verify_report(
        document, ledger=_ledger(report_public_key=issuer_public_hex(report_key))
    )
    assert any("enabled-accounts proxy" in f.summary for f in verdict.findings)


def test_csl_figures_are_not_judged_when_fal_is_authoritative(
    report_key: Ed25519PrivateKey,
) -> None:
    """The non-authoritative block is an observation — never a breach."""
    usage = _document()["usage"]
    usage["licence_type"] = "fal"
    usage["fal"].update({"n_slots": 50, "assignments_current": 41, "over_cap": False})
    usage["csl"].update({"peak_concurrent_sessions": 999, "over_cap": None})
    document = _sign(_document(usage=usage), report_key)
    verdict = verify_report(
        document, ledger=_ledger(report_public_key=issuer_public_hex(report_key))
    )
    assert not [f for f in verdict.findings if f.severity == "breach"]


def test_no_licence_reports_observation_only(report_key: Ed25519PrivateKey) -> None:
    document = _sign(_document(), report_key)
    verdict = verify_report(
        document, ledger=_ledger(report_public_key=issuer_public_hex(report_key))
    )
    assert any("no licence installed" in f.summary for f in verdict.findings)


# ----- malformed input ------------------------------------------------


def test_not_a_report_raises() -> None:
    with pytest.raises(ReportFormatError, match="not a MATOFX compliance report"):
        verify_report({"hello": "world"}, ledger=[])


def test_future_schema_version_refuses_rather_than_guessing() -> None:
    document = _document(report_schema_version=REPORT_SCHEMA_VERSION + 1)
    with pytest.raises(ReportFormatError, match="Upgrade the verifier"):
        verify_report(document, ledger=[])


def _first_report_anchor() -> dict[str, Any]:
    anchor = _document()["anchor"]
    anchor["previous_report"] = None
    anchor["previous_head_rehashed_now"] = None
    return anchor
