"""Admin: Licence & compliance (``/ui/admin/compliance``) — ADR-0019 §B7.

Generating the artefact a customer's supplier asks for. ADR-0019
specified this page from the start and deferred it to slice 6's UI work,
where it was never built — so until now the only way to produce a
compliance report was `matofx compliance report` on the server, which
assumes an administrator with shell access. Plenty of customers' MATOFX
administrators have no such thing.

Two things shape the design.

**The report is a pair, and the pair matters.** Every report is a
signed `.json` (the one to send) and an `.html` twin with identical
content laid out as a readable page. The customer is expected to *read*
the HTML before deciding to send the JSON — that is what makes sending
it a routine act rather than a legal conversation — so both downloads
sit on every row with equal weight, and the page says which is which.

**Generating must never feel dangerous.** Extra reports cost nothing:
each one proves it follows on from *every* earlier report, so a gap in
the numbering is a question, not a problem. The page says so plainly,
because an administrator who is nervous about pressing the button will
instead not generate the report their supplier asked for.

Permissions: ``audit.read``, already documented as the right a
compliance-focused role holds. The API enforces it.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.format import format_user_datetime
from matofx.ui.pages._chrome import render_shell, require_logged_in
from matofx.ui.state import current_token


@ui.page("/admin/compliance")
async def admin_compliance_page() -> None:
    """Render the licence-compliance surface."""
    if not require_logged_in():
        return
    render_shell()

    client = ApiClient(get_app(), token=current_token())

    with ui.column().classes("w-full gap-4").style("padding: 0.85rem 1.5rem 1.5rem;"):
        with ui.column().classes("gap-1"):
            ui.label("Licence & compliance").style(
                "font-size: 1.6rem; font-weight: 650; line-height: 1.1; margin-top: -2px;"
            )
            ui.label(
                "Generate the compliance report your software supplier asks for. "
                "Nothing is ever sent automatically — a report exists only when you "
                "make one, and it leaves this server only if you send it."
            ).style("color: var(--matofx-muted-text); font-size: 0.9rem;")

        body = ui.column().classes("w-full gap-4")

        async def refresh() -> None:
            body.clear()
            try:
                data = await client.list_compliance_reports()
            except ApiError as exc:
                with body:
                    ui.label(f"Couldn't load reports: {exc.detail}").style(
                        "color: var(--matofx-danger-text, #f0716b);"
                    )
                return
            except Exception as exc:  # pragma: no cover - defensive
                with body:
                    ui.label(f"Couldn't load reports: {type(exc).__name__}: {exc}")
                return
            with body:
                _render_identity(data)
                _render_generate(data, client, refresh)
                _render_table(data, client)

        await refresh()


def _render_identity(data: dict[str, Any]) -> None:
    """Which install this is — the first thing a supplier will ask."""
    install_id = data.get("install_id")
    customer = data.get("customer")

    with ui.card().classes("w-full").style("padding: 0.9rem 1.1rem;"):
        with ui.row().classes("items-center no-wrap").style("gap: 0.7rem;"):
            ui.icon("fingerprint").style("color: var(--matofx-muted-text); font-size: 1.4rem;")
            with ui.column().classes("gap-0"):
                if install_id:
                    ui.label(f"Install {install_id}").style("font-weight: 600;")
                else:
                    ui.label("This install has no identity yet.").style("font-weight: 600;")
                ui.label(
                    str(customer)
                    if customer
                    else "No licence installed yet — reports are still generated, and say so."
                ).style("color: var(--matofx-muted-text); font-size: 0.82rem;")
    _render_schedule(data)


def _render_schedule(data: dict[str, Any]) -> None:
    """When the next report is due, in the same words as the banner.

    The listing endpoint computes this with the very function the chrome
    banner is drawn from (``compliance.schedule.reporting_status``), so
    this page cannot say "not due yet" underneath a banner saying
    "overdue". Working it out again here would be one rule implemented
    twice, and those drift the moment either is touched.
    """
    due_at = data.get("reporting_due_at")
    if not due_at:
        return
    overdue = bool(data.get("reporting_due"))
    icon, colour = ("event_busy", "#d6a93b") if overdue else ("event_available", None)
    with ui.row().classes("items-center no-wrap").style("gap: 0.45rem; padding-left: 0.2rem;"):
        ui.icon(icon).style(f"font-size: 1.05rem; color: {colour or 'var(--matofx-muted-text)'};")
        prefix = "A report is due" if overdue else "Next report due"
        ui.label(f"{prefix} — {format_user_datetime(due_at)}").style(
            f"font-size: 0.82rem; color: {colour or 'var(--matofx-muted-text)'};"
        )


def _render_generate(data: dict[str, Any], client: ApiClient, refresh: Any) -> None:
    """The button, and everything needed to press it without anxiety."""
    can_generate = bool(data.get("can_generate"))

    with ui.card().classes("w-full").style("padding: 0.9rem 1.1rem;"):
        with ui.column().classes("w-full gap-2"):
            ui.label("Generate a report").style("font-weight: 600;")

            if not can_generate:
                ui.label(str(data.get("blocked_reason") or "Reports cannot be generated.")).style(
                    "color: #d6a93b; font-size: 0.86rem;"
                )
                return

            ui.label(
                "Covers the period since your last report. Generating one you do not "
                "send is fine and causes no trouble later — every report proves it "
                "follows on from all the earlier ones, so your supplier can check it "
                "against whichever they last received."
            ).style("color: var(--matofx-muted-text); font-size: 0.86rem;")

            with ui.row().classes("items-center gap-2"):
                since = (
                    ui.input(placeholder="YYYY-MM-DD (optional)")
                    .props("dense outlined")
                    .style("width: 200px;")
                )
                since.tooltip(
                    "Leave empty to cover the period since the last report. "
                    "Set a date to cover a specific window instead."
                )

                async def generate() -> None:
                    button.disable()
                    notice = ui.notification(
                        "Generating — this checks every audit record, so it can take a moment…",
                        type="ongoing",
                        timeout=None,
                    )
                    try:
                        result = await client.generate_compliance_report(
                            since=(since.value or "").strip() or None
                        )
                    except ApiError as exc:
                        ui.notify(f"Couldn't generate the report: {exc.detail}", type="negative")
                        return
                    finally:
                        # ``type="ongoing"`` never auto-dismisses; without
                        # this the spinner outlives the request.
                        notice.dismiss()
                        button.enable()

                    if result.get("warning"):
                        # Not an error: the report was produced, and what it
                        # records is exactly why it was worth producing.
                        ui.notify(
                            str(result["warning"]),
                            type="warning",
                            timeout=0,
                            close_button=True,
                        )
                    else:
                        ui.notify(
                            f"Report #{result.get('counter')} generated. "
                            "Read the HTML twin before sending the JSON.",
                            type="positive",
                        )
                    await refresh()

                button = ui.button("Generate report", icon="description", on_click=generate).props(
                    "color=primary no-caps"
                )


def _render_table(data: dict[str, Any], client: ApiClient) -> None:
    """One row per archived report, newest first."""
    rows = list(data.get("reports") or [])
    if not rows:
        ui.label(
            "No reports yet. Generating one is safe at any time — it is the only way "
            "to see what your supplier receives."
        ).style("color: var(--matofx-muted-text);")
        return

    with ui.card().classes("w-full").style("padding: 0; overflow: hidden;"):
        with ui.column().classes("w-full").style("gap: 0;"):
            _header_row()
            for row in rows:
                _report_row(row, client)


def _header_row() -> None:
    with (
        ui.row()
        .classes("w-full items-center no-wrap")
        .style(
            "padding: 0.55rem 1.1rem; gap: 0.75rem; font-size: 0.78rem; font-weight: 650; "
            "letter-spacing: 0.02em; text-transform: uppercase; "
            "color: var(--matofx-muted-text); border-bottom: 1px solid var(--matofx-border);"
        )
    ):
        ui.label("Report").style("flex: 0 0 80px;")
        ui.label("Generated").style("flex: 1.4 1 0;")
        ui.label("Period covered").style("flex: 1.8 1 0;")
        ui.label("Records").style("flex: 1 1 0;")
        ui.label("").style("flex: 0 0 210px;")


def _report_row(row: dict[str, Any], client: ApiClient) -> None:
    counter = int(row.get("counter") or 0)
    chain_ok = bool(row.get("chain_ok"))

    async def download(fmt: str) -> None:
        try:
            payload = await client.fetch_compliance_report(counter, fmt=fmt)
        except ApiError as exc:
            ui.notify(f"Couldn't download the report: {exc.detail}", type="negative")
            return
        suffix = "html" if fmt == "html" else "json"
        ui.download.content(payload, f"compliance-report-{counter:04d}.{suffix}")

    with (
        ui.row()
        .classes("w-full items-center no-wrap")
        .style(
            "padding: 0.6rem 1.1rem; gap: 0.75rem; font-size: 0.88rem; "
            "border-bottom: 1px solid var(--matofx-border);"
        )
    ):
        ui.label(f"#{counter}").style("flex: 0 0 80px; font-weight: 600;")
        with ui.column().classes("gap-0").style("flex: 1.4 1 0; min-width: 0;"):
            ui.label(format_user_datetime(str(row.get("generated_at") or ""))).style(
                "overflow: hidden; text-overflow: ellipsis;"
            )
            ui.label(str(row.get("generated_by") or "")).style(
                "color: var(--matofx-muted-text); font-size: 0.78rem;"
            )
        ui.label(
            f"{_date_only(row.get('period_from'))} → {_date_only(row.get('period_to'))}"
        ).style("flex: 1.8 1 0; color: var(--matofx-muted-text);")

        with ui.row().classes("items-center no-wrap").style("flex: 1 1 0; gap: 0.35rem;"):
            if chain_ok:
                ui.icon("verified_user").style("color: var(--matofx-muted-text); font-size: 1rem;")
                ui.label("verified").style("color: var(--matofx-muted-text);")
            else:
                # Never soften this. A report recording a failed check is
                # still valid and still worth sending — but the row must
                # not read the same as a clean one.
                ui.icon("warning").style("color: #d6a93b; font-size: 1rem;")
                ui.label("check failed").style("color: #d6a93b; font-weight: 550;")

        with (
            ui.row()
            .classes("items-center justify-end no-wrap")
            .style("flex: 0 0 210px; gap: 0.3rem;")
        ):
            ui.button("Read (HTML)", on_click=lambda _e=None: download("html")).props(
                "flat dense no-caps color=primary"
            ).tooltip("The readable twin — open this before sending anything.")
            ui.button("Send (JSON)", on_click=lambda _e=None: download("json")).props(
                "flat dense no-caps"
            ).tooltip("The signed file to send to your supplier.")


def _date_only(value: object) -> str:
    """``2026-08-12`` from an ISO timestamp, or an em dash."""
    text = str(value or "")
    return text[:10] if text else "—"
