"""Human-readable twin of a compliance report (ADR-0019 §B6).

The customer's admin reads this before sending the JSON, which is what
turns "can we send this?" into a two-minute check rather than a legal
conversation. It renders *from the signed document only* — never from a
fresh database query — so what they read is exactly what they send.

Self-contained HTML: no external CSS, fonts or scripts. It has to open
on an air-gapped machine and print cleanly.
"""

from __future__ import annotations

from html import escape
from typing import Any

from matofx.compliance.schema import CSL, FAL

_CSS = """
:root { color-scheme: light; }
body { font-family: -apple-system, "Segoe UI", Roboto, sans-serif; color: #07203F;
       background: #fff; margin: 0; padding: 2rem; line-height: 1.45; }
h1 { font-size: 1.4rem; margin: 0 0 0.25rem; }
h2 { font-size: 1rem; margin: 1.6rem 0 0.5rem; padding-bottom: 0.3rem;
     border-bottom: 2px solid #40A1A4; }
.sub { color: #5a6b78; font-size: 0.85rem; margin: 0 0 1.2rem; }
table { border-collapse: collapse; width: 100%; font-size: 0.85rem; margin-bottom: 0.5rem; }
th, td { text-align: left; padding: 0.35rem 0.6rem; border-bottom: 1px solid #e3e8ec;
         vertical-align: top; }
th { width: 34%; font-weight: 600; color: #14384d; }
code { font-family: ui-monospace, Consolas, monospace; font-size: 0.82em; word-break: break-all; }
.pill { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 999px;
        font-size: 0.78rem; font-weight: 600; }
.ok { background: rgba(63,185,80,0.16); color: #1c6b2b; }
.bad { background: rgba(240,113,107,0.18); color: #91231d; }
.warn { background: rgba(214,169,59,0.20); color: #7a5a10; }
.note { background: #f5f8fa; border-left: 3px solid #40A1A4; padding: 0.6rem 0.8rem;
        font-size: 0.82rem; margin: 1rem 0; }
@media print { body { padding: 0.5rem; } }
"""


def render_report_html(document: dict[str, Any]) -> str:
    """Render the signed document as a standalone HTML page."""
    install = _map(document.get("install"))
    licence = _map(document.get("licence"))
    period = _map(document.get("period"))
    anchor = _map(document.get("anchor"))
    chain = _map(document.get("chain_verification"))
    usage = _map(document.get("usage"))

    customer = escape(str(install.get("customer") or "—"))
    title = f"MATOFX compliance report #{document.get('counter')} — {customer}"

    sections = [
        _section("Install", _install_rows(install, document)),
        _section("Licence", _licence_rows(licence)),
        _section("Reporting period", _period_rows(period, document)),
        _section("Chain anchoring", _anchor_rows(anchor)),
        _section("Chain self-verification", _chain_rows(chain)),
        *_usage_sections(usage),
        _section("Licence events", _event_rows(_map(document.get("licence_events")))),
        _notable_section(document),
        _section("Signature", _signature_rows(_map(document.get("signature")))),
    ]

    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<title>{escape(title)}</title><style>{_CSS}</style></head><body>"
        f"<h1>{escape(title)}</h1>"
        f"<p class='sub'>Generated {escape(str(document.get('generated_at') or ''))} · "
        f"install <code>{escape(str(install.get('install_id') or ''))}</code></p>"
        "<div class='note'>This file is the human-readable twin of the signed "
        "<code>.json</code> report. It contains licence and integrity figures only — no "
        "project, job, or file data, and no user names beyond the actors of the listed "
        "events. Send the <code>.json</code>; keep or send this alongside it.</div>"
        + "".join(sections)
        + "</body></html>"
    )


def _install_rows(install: dict[str, Any], document: dict[str, Any]) -> list[tuple[str, str]]:
    verified = install.get("issuer_verified") is True
    return [
        ("Customer", escape(str(install.get("customer") or "—"))),
        ("Install ID", f"<code>{escape(str(install.get('install_id') or '—'))}</code>"),
        ("Environment", escape(str(install.get("environment") or "—"))),
        ("MATOFX version", escape(str(install.get("app_version") or "—"))),
        ("Database", escape(str(install.get("db_dialect") or "—"))),
        ("Report number", escape(str(document.get("counter") or "—"))),
        (
            "Identity issued by",
            _pill(
                escape(str(install.get("issuer_key_id") or "unsigned")),
                "ok" if verified else "warn",
            )
            + ("" if verified else " <span class='sub'>not verified</span>"),
        ),
    ]


def _licence_rows(licence: dict[str, Any]) -> list[tuple[str, str]]:
    state = str(licence.get("state") or "unknown")
    if state == "not_installed":
        return [
            ("State", _pill("not installed", "warn")),
            (
                "Note",
                "No licence manifest on this install yet — usage below is reported as "
                "observation, not measured against a cap.",
            ),
        ]
    features = licence.get("features_enabled")
    return [
        ("State", _pill(escape(state), "ok" if state == "active" else "warn")),
        ("Licence ID", f"<code>{escape(str(licence.get('licence_id') or '—'))}</code>"),
        ("Type", escape(str(licence.get("licence_type") or "—")).upper()),
        ("Slots", escape(str(licence.get("n_slots") or "—"))),
        ("Expires", escape(str(licence.get("expires_at") or "—"))),
        (
            "Features",
            escape(", ".join(str(f) for f in features)) if isinstance(features, list) else "—",
        ),
    ]


def _period_rows(period: dict[str, Any], document: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        ("From", escape(str(period.get("from") or "—"))),
        ("To", escape(str(period.get("to") or "—"))),
        ("Generated", escape(str(document.get("generated_at") or "—"))),
    ]


def _anchor_rows(anchor: dict[str, Any]) -> list[tuple[str, str]]:
    genesis = _map(anchor.get("genesis_record"))
    previous = _map(anchor.get("previous_report"))
    head = _map(anchor.get("head"))
    tree = _map(anchor.get("tree"))
    rows = [
        ("First record", _record_cell(genesis)),
        ("Current head", _record_cell(head)),
    ]
    if tree:
        rows.append(
            (
                "Record tree",
                f"{escape(str(tree.get('size') or 0))} record(s) · "
                f"<code>{escape(str(tree.get('root') or '—'))}</code> "
                f"({escape(str(tree.get('algorithm') or 'rfc6962-sha256'))})",
            )
        )
        proofs = anchor.get("consistency_proofs")
        count = len(proofs) if isinstance(proofs, list) else 0
        note = " (truncated)" if anchor.get("consistency_proofs_truncated") else ""
        rows.append(
            (
                "Consistency proofs",
                f"{count} proof(s) back to earlier reports{escape(note)}",
            )
        )
    if previous:
        rows.append(
            (
                f"Previous report (#{escape(str(previous.get('counter')))})",
                f"head #{escape(str(previous.get('head_id')))} · "
                f"<code>{escape(str(previous.get('head_hash') or ''))}</code>",
            )
        )
        rehashed = escape(str(anchor.get("previous_head_rehashed_now") or "MISSING"))
        rows.append(("That record hashes now", f"<code>{rehashed}</code>"))
    else:
        rows.append(("Previous report", "none — this report establishes the baseline"))
    return rows


def _record_cell(record: dict[str, Any]) -> str:
    """``#id · hash`` for a chain record, or an em dash when absent."""
    ident = escape(str(record.get("id") or "—"))
    digest = escape(str(record.get("hash") or "—"))
    return f"#{ident} · <code>{digest}</code>"


def _chain_rows(chain: dict[str, Any]) -> list[tuple[str, str]]:
    ok = chain.get("ok") is True
    raw_failures = chain.get("failures")
    failures: list[Any] = list(raw_failures) if isinstance(raw_failures, list) else []
    rows = [
        ("Result", _pill("PASS", "ok") if ok else _pill("FAIL", "bad")),
        ("Records checked", escape(str(chain.get("records_checked") or 0))),
    ]
    for failure in failures[:20]:
        item = _map(failure)
        rows.append(
            (
                f"Failure at record {escape(str(item.get('record_id')))}",
                escape(str(item.get("reason") or "")),
            )
        )
    return rows


def _usage_sections(usage: dict[str, Any]) -> list[str]:
    """One section per licence type, authoritative one first and labelled."""
    authoritative = usage.get("licence_type")
    sections: list[str] = []
    order = [CSL, FAL] if authoritative != FAL else [FAL, CSL]
    for kind in order:
        block = _map(usage.get(kind))
        if not block:
            continue
        label = "Concurrent sessions (CSL)" if kind == CSL else "Floating assignments (FAL)"
        tag = (
            _pill("authoritative", "ok")
            if authoritative == kind
            else _pill("observation", "warn")
        )
        rows = _csl_rows(block) if kind == CSL else _fal_rows(block)
        sections.append(_section(f"{label} {tag}", rows))
    return sections


def _csl_rows(block: dict[str, Any]) -> list[tuple[str, str]]:
    over = block.get("over_cap")
    peak = escape(str(block.get("peak_concurrent_sessions") or 0))
    cap = block.get("n_slots")
    peak_cell = f"{peak} / {escape(str(cap))}" if cap is not None else peak
    if over is True:
        peak_cell += " " + _pill("over cap", "bad")
    return [
        ("Peak concurrent sessions", peak_cell),
        ("Sessions started", escape(str(block.get("sessions_started") or 0))),
        ("Slots granted", escape(str(block.get("slots_granted") or 0))),
        ("Logins refused at cap", escape(str(block.get("slots_denied") or 0))),
        ("Break-glass admissions", escape(str(block.get("breakglass_admissions") or 0))),
        ("Daily peaks", _sparkline(_map(block.get("peak_by_day")))),
    ]


def _fal_rows(block: dict[str, Any]) -> list[tuple[str, str]]:
    over = block.get("over_cap")
    current = escape(str(block.get("assignments_current") or 0))
    cap = block.get("n_slots")
    cell = f"{current} / {escape(str(cap))}" if cap is not None else current
    if over is True:
        cell += " " + _pill("over cap", "bad")
    return [
        ("Assignments held", cell),
        ("Granted in period", escape(str(block.get("assignments_granted") or 0))),
        ("Revoked in period", escape(str(block.get("assignments_revoked") or 0))),
        ("Refused at cap", escape(str(block.get("assignments_denied") or 0))),
        ("Registered users", escape(str(block.get("registered_users") or 0))),
        ("Counted from", escape(str(block.get("source") or "—"))),
    ]


def _event_rows(events: dict[str, Any]) -> list[tuple[str, str]]:
    if not events:
        return [("—", "no licence events recorded")]
    return [(escape(action), escape(str(count))) for action, count in sorted(events.items())]


def _notable_section(document: dict[str, Any]) -> str:
    notable = document.get("notable_events")
    if not isinstance(notable, list) or not notable:
        return _section("Notable events", [("—", "none in this period")])
    rows = [
        (
            f"#{escape(str(_map(e).get('record_id')))} · {escape(str(_map(e).get('action')))}",
            f"{escape(str(_map(e).get('timestamp') or ''))} · "
            f"{escape(str(_map(e).get('actor') or ''))}"
            + (f" · {escape(str(_map(e).get('reason')))}" if _map(e).get("reason") else ""),
        )
        for e in notable
    ]
    if document.get("notable_events_truncated") is True:
        rows.append(("…", "list truncated — more events of these kinds occurred"))
    return _section("Notable events", rows)


def _signature_rows(signature: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        ("Algorithm", escape(str(signature.get("alg") or "—"))),
        ("Key ID", f"<code>{escape(str(signature.get('key_id') or '—'))}</code>"),
        ("Value", f"<code>{escape(str(signature.get('value') or '—'))}</code>"),
    ]


def _sparkline(peaks: dict[str, Any]) -> str:
    """Daily peaks as ``date:value`` pairs — readable, printable, no JS."""
    if not peaks:
        return "—"
    return escape(" · ".join(f"{day[5:]}:{value}" for day, value in sorted(peaks.items())))


def _section(title: str, rows: list[tuple[str, str]]) -> str:
    body = "".join(f"<tr><th>{label}</th><td>{value}</td></tr>" for label, value in rows)
    return f"<h2>{title}</h2><table>{body}</table>"


def _pill(text: str, kind: str) -> str:
    return f"<span class='pill {kind}'>{text}</span>"


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}
