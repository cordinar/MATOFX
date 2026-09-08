"""Login page (``/ui/login``).

Username + password form. Posts to ``POST /api/auth/login`` via the
in-process API client. On success, stashes the bearer token in
:mod:`matofx.ui.state` and redirects to ``?next=`` (the page the user
was trying to reach — set by :func:`require_logged_in` so deep links
from notification emails survive the sign-in detour), or the home
page when there's no safe target. On failure, shows a plain-English
message under the form.

Visual treatment (a deliberate exception to the dashboard theme): the
login page always renders a **white card on a fixed light-teal
(#6DCDC4) background**, regardless of light/dark preference — there's
no signed-in user yet, so there's no stored theme to honour, and the
brand wants a single recognisable entry screen. The shipped brand logo
(:func:`matofx.ui.static_assets.logo_url`) always shows above the form.
"""

from __future__ import annotations

import json

from nicegui import context, ui

from matofx.ui._holder import get_app
from matofx.ui.api_client import ApiClient, ApiError
from matofx.ui.branding_cache import get_branding
from matofx.ui.state import is_logged_in, store_breakglass_notice, store_login
from matofx.ui.static_assets import logo_url

# Fixed brand colours for the login screen — independent of the user's
# (not-yet-known) theme preference. A white card on a light-teal page,
# with navy text + teal accents throughout.
_LOGIN_BG = "#6DCDC4"  # page background (light teal)
_CARD_BG = "#ffffff"  # card background (white)
_NAVY = "#07203F"  # primary text
_LABEL = "#14384d"  # input labels / secondary text
_TEAL = "#2D7682"  # teal accent (OFX wordmark, focus highlight)
_ERROR = "#c0392b"  # error text
_MUTED = "#5a6b78"  # body text inside the forgot-password dialog

# Release line shown in the footer. The numeric package version is the
# single source of truth (``[project].version`` in pyproject); the
# marketing release name is maintained here until there's a reason to
# data-drive it.
_RELEASE_NAME = "2026 R2"

# Why the user was sent back here, passed as ``?reason=`` by the
# dashboard chrome. A toast raised just before ``ui.navigate.to`` dies
# with the page that raised it, so the explanation has to travel in the
# URL and be rendered here. Anything not in this map shows no notice.
_SIGN_OUT_NOTICES: dict[str, str] = {
    # Client-side idle timer tripped (matofx.ui.pages._chrome._logout_idle).
    "idle": (
        "You were signed out because you'd been inactive for a while. "
        "Sign in again to carry on."
    ),
    # The API rejected the stored token (idle/hard timeout reached
    # server-side, session revoked, or the account was deactivated).
    "expired": "Your session has ended. Sign in again to carry on.",
}


def _sign_out_notice(reason: str) -> str:
    """The banner text for a ``?reason=`` value (empty when unrecognised)."""
    return _SIGN_OUT_NOTICES.get((reason or "").strip().lower(), "")


def _safe_next(dest: str) -> str:
    """Validate a ``?next=`` target; fall back to ``/home``.

    Only same-site relative paths are honoured: must start with a
    single ``/`` and not loop back to the login page. Protocol-
    relative (``//evil.com``) and absolute URLs are rejected so the
    parameter can't be abused as an open redirect.
    """
    if (
        dest
        and dest.startswith("/")
        and not dest.startswith("//")
        and dest.rstrip("/") not in ("", "/login")
    ):
        return dest
    return "/home"


def _wordmark_html(title: str, *, ofx_colour: str) -> str:
    """Brand wordmark: ``MAT`` + tinted ``OFX`` + a ™ superscript.

    Mirrors the sidebar wordmark (:func:`_chrome._brand_title_html`) but
    pins explicit colours because the login card is always light — it
    must not pick up the dark theme's ``--matofx-body-text`` variable.
    """
    import html as _html  # noqa: PLC0415

    base = title.strip().rstrip("™").replace("(TM)", "").replace("(tm)", "").strip()
    safe = _html.escape(base or "MATOFX SPDM")
    safe = safe.replace("OFX", f'<span style="color: {ofx_colour};">OFX</span>', 1)
    return (
        f"{safe}"
        '<sup style="font-size: 0.6em; font-weight: 600; vertical-align: 0.32em; '
        'margin-left: 0.5px;">™</sup>'
    )


@ui.page("/login")
def login_page(next: str = "", reason: str = "") -> None:
    """Render the login form. ``next`` is the post-login destination.

    The parameter shadows the ``next`` builtin deliberately — NiceGUI
    maps query params by parameter name and ``?next=`` is the web
    convention for a post-auth return path.

    ``reason`` (``idle`` / ``expired``) is set by the dashboard chrome
    when it signs the user out itself, and renders a short notice above
    the form so the return trip isn't unexplained.
    """
    target = _safe_next(next)
    notice = _sign_out_notice(reason)
    if is_logged_in():
        ui.navigate.to(target)
        return

    branding = get_branding()
    title = branding.app_display_title or "MATOFX SPDM"
    ui.page_title(f"Sign in — {title}")

    # Light-teal page, white card, navy controls.
    ui.dark_mode(False)
    ui.colors(primary=_NAVY, accent=_TEAL)

    # Page-scoped styling. Each @ui.page is its own document, so this
    # head block only affects the login screen. The !important on the
    # background overrides NiceGUI's default page background.
    ui.add_head_html(
        "<style>"
        f"body, .q-page, .nicegui-content {{ background: {_LOGIN_BG} !important; }}"
        # White card, hairline navy border, soft shadow.
        ".matofx-login-card {"
        f"  background: {_CARD_BG} !important;"
        "  border: 1px solid rgba(7,32,63,0.10) !important;"
        "  border-radius: 16px;"
        "  box-shadow: 0 14px 40px rgba(7,32,63,0.20) !important;"
        "}"
        # Inputs on the white card: navy text, navy-tinted borders.
        f".matofx-login-card .q-field__native {{ color: {_NAVY} !important; }}"
        f".matofx-login-card .q-field__label {{ color: {_LABEL} !important; }}"
        ".matofx-login-card .q-field__control:before {"
        "  border-color: rgba(7,32,63,0.30) !important; }"
        f".matofx-login-card .q-field--focused .q-field__control:after {{"
        f"  border-color: {_NAVY} !important; }}"
        f".matofx-login-card .q-field__append .q-icon,"
        f".matofx-login-card .q-field__prepend .q-icon {{ color: {_LABEL} !important; }}"
        # Caps-lock hint — hidden until the JS below switches it on.
        "#matofx-caps-hint { display: none; }"
        "</style>"
        # Caps-lock detector: a document-level listener (survives the
        # async element mount). Shows the hint only while Caps Lock is
        # on AND the password field has focus.
        "<script>"
        "(function(){"
        "  function upd(e){"
        "    var hint=document.getElementById('matofx-caps-hint'); if(!hint) return;"
        "    var pw=document.querySelector('.matofx-login-pass input');"
        "    var on=false; try{ on=e.getModifierState&&e.getModifierState('CapsLock'); }catch(_){}"
        "    var focused=pw&&document.activeElement===pw;"
        "    hint.style.display=(on&&focused)?'flex':'none';"
        "  }"
        "  document.addEventListener('keydown',upd,true);"
        "  document.addEventListener('keyup',upd,true);"
        "  document.addEventListener('focusout',function(){"
        "    var h=document.getElementById('matofx-caps-hint'); if(h) h.style.display='none';"
        "  },true);"
        "})();"
        "</script>"
    )

    # Forgot-password guidance. MATOFX has no self-service password reset
    # (by design — resets go through an administrator), so the link opens
    # this dialog rather than a reset flow. Defined before the form so the
    # link handler can reference it directly.
    with ui.dialog() as forgot_dialog, ui.card().style("max-width: 380px; padding: 1.4rem;"):
        ui.label("Password help").style(
            f"font-size: 1.05rem; font-weight: 650; color: {_NAVY};"
        )
        ui.label(
            "MATOFX doesn't offer self-service password resets. Ask your "
            "administrator to reset your password — they can set a new "
            "starting password for your account, which you can change once "
            "you're signed in."
        ).style(f"color: {_MUTED}; font-size: 0.9rem; line-height: 1.5;")
        with ui.row().classes("w-full justify-end"):
            ui.button("Close", on_click=forgot_dialog.close).props(
                "flat no-caps color=primary"
            )

    # Logo — the shipped packaged asset, always present. Falls back to
    # the text wordmark if the assets folder was emptied. Rendered above
    # the card (large), on the teal page, via a raw <img> with a
    # host-absolute src: ui.image() rewrote the URL relative to NiceGUI's
    # /ui mount (requesting /ui/matofx-assets/... → 404); a leading-slash
    # src is served from the outer-app mount. sanitize=False so the tag
    # survives NiceGUI's HTML sanitiser (see the ui.html sanitize note).
    logo = logo_url()
    # Centred horizontally, nudged slightly above vertical centre — the
    # block's midpoint sits at 42% of the viewport height (not 50%), so
    # the whole logo + card sits a touch above middle.
    with (
        ui.column()
        .classes("items-center")
        .style(
            "position: absolute; left: 50%; top: 42%; "
            "transform: translate(-50%, -50%); gap: 1rem;"
        )
    ):
        # Logo at 85% of the card width (380px → ~323px), centred on the
        # teal page above the card.
        if logo:
            import html as _html  # noqa: PLC0415

            # sanitize=False leaves this tag un-sanitised, and `title`
            # (admin-set branding) renders on the pre-login page — escape
            # both attribute values so neither can break out of the tag.
            safe_logo = _html.escape(logo, quote=True)
            safe_alt = _html.escape(title, quote=True)
            ui.html(
                f'<img src="{safe_logo}" alt="{safe_alt}" '
                'style="width:323px;max-width:78vw;height:auto;'
                'object-fit:contain;display:block;margin:0 auto;">',
                sanitize=False,
            )
        else:
            ui.html(
                f'<div style="font-size:2rem;font-weight:800;color:{_NAVY};'
                f'text-align:center;">{_wordmark_html(title, ofx_colour=_TEAL)}</div>'
            )

        with ui.card().classes("matofx-login-card items-stretch").style(
            "padding: 2rem 2.1rem; gap: 0.85rem; width: 380px; max-width: 92vw;"
        ):
            ui.html(
                f'<div style="font-size:1.25rem;font-weight:650;color:{_NAVY};'
                'text-align:center;">Sign in</div>'
            )

            # "You were signed out because…" — only when the chrome sent
            # the user here itself. Amber-tinted note, not an error: the
            # sign-out is expected behaviour, not a failure.
            if notice:
                with ui.row().classes("items-start no-wrap matofx-login-notice").style(
                    "gap: 0.5rem; padding: 0.55rem 0.7rem; border-radius: 8px; "
                    "background: rgba(214,169,59,0.14); "
                    "border: 1px solid rgba(214,169,59,0.45);"
                ):
                    ui.icon("schedule").style(f"color: {_NAVY}; font-size: 1.05rem;")
                    ui.label(notice).style(
                        f"color: {_NAVY}; font-size: 0.82rem; line-height: 1.35;"
                    )

            username = (
                ui.input("Username")
                .props("autofocus outlined dense")
                .classes("w-full matofx-login-user")
            )
            password = (
                ui.input("Password", password=True, password_toggle_button=True)
                .props("outlined dense")
                .classes("w-full matofx-login-pass")
            )

            # Caps-lock hint, toggled by the head-script above.
            with ui.row().props("id=matofx-caps-hint").classes("items-center").style(
                f"gap: 0.4rem; color: {_NAVY}; font-size: 0.78rem; margin-top: -0.3rem;"
            ):
                ui.icon("keyboard_capslock").style("font-size: 1rem;")
                ui.label("Caps Lock is on")

            # Remember username (left) + forgot-password help (right).
            with ui.row().classes("w-full items-center justify-between no-wrap").style(
                "margin-top: -0.1rem;"
            ):
                remember = (
                    ui.checkbox("Remember username")
                    .classes("matofx-login-remember")
                    .props("dense color=primary size=sm")
                )
                remember.style(f"color: {_NAVY};")
                ui.link("Forgot password?", "#").classes("matofx-login-forgot").style(
                    f"color: {_NAVY}; font-size: 0.82rem; font-weight: 600; "
                    "text-decoration: none;"
                ).on("click.prevent", forgot_dialog.open)

            message = ui.label("").style(
                f"color: {_ERROR}; font-size: 0.85rem; min-height: 1rem;"
            )

            async def _submit() -> None:
                # Validate client-side first so an empty field gives a
                # plain-English prompt instead of the API's raw 422
                # pydantic validation payload ("string_too_short ...").
                if not username.value or not password.value:
                    message.text = "Enter your username and password."
                    return
                client = ApiClient(get_app())
                try:
                    response = await client.login(
                        username=username.value, password=password.value
                    )
                except ApiError as exc:
                    if exc.status_code == 401:
                        message.text = "That username or password isn't right. Try again."
                    elif exc.status_code == 403:
                        message.text = "Your account is deactivated. Ask an administrator."
                    elif exc.status_code == 503:
                        # Licence places are full — the password was fine.
                        # Show the API's plain-English detail verbatim; it
                        # names the cap and what to do about it.
                        message.text = exc.detail or "All places are in use. Try again shortly."
                    elif exc.status_code == 422:
                        # Field-validation failure (e.g. empty field). The
                        # detail is a raw pydantic error list — never show it.
                        message.text = "Enter your username and password."
                    else:
                        message.text = exc.detail or "Sign-in failed. Please try again."
                    return
                except Exception as exc:
                    # Anything that isn't an ApiError (transport failure,
                    # JSON-decode error, etc.) used to disappear silently
                    # and the operator just saw the form not respond. Show
                    # the error class so a stuck-on-click report is
                    # debuggable from a screenshot.
                    message.text = f"Sign-in failed unexpectedly: {type(exc).__name__}: {exc}"
                    return
                # Resolve the role so the sidebar can render Admin Space at
                # render time without a round-trip on every page. Any error
                # here would otherwise kill the handler silently — surface
                # it instead of pretending login worked.
                authed = ApiClient(get_app(), token=response["access_token"])
                try:
                    me = await authed.me()
                    role = str(me.get("role") or "")
                    theme = str(me.get("theme_preference") or "dark")
                    first_name = str(me.get("first_name") or "")
                    locale = str(me.get("locale_preference") or "auto")
                    timezone = str(me.get("timezone_preference") or "auto")
                    is_approver = bool(me.get("is_approver"))
                    permissions = [str(p) for p in (me.get("permissions") or [])]
                    ui_preferences = me.get("ui_preferences")
                    if not isinstance(ui_preferences, dict):
                        ui_preferences = {}
                except ApiError as exc:
                    message.text = (
                        "Signed in, but couldn't read your account: "
                        f"{exc.detail or exc.status_code}. Try again."
                    )
                    return
                except Exception as exc:
                    message.text = (
                        "Signed in, but loading your account failed: "
                        f"{type(exc).__name__}: {exc}. "
                        "Check the API server log — likely a pending DB migration."
                    )
                    return
                # Persist (or clear) the remembered username in the
                # browser's localStorage. Done here, not via
                # app.storage.browser, because that cookie store can't be
                # written after the page response is sent (i.e. from a
                # click handler).
                if remember.value:
                    ui.run_javascript(
                        f"localStorage.setItem('matofx_remember_user', "
                        f"{json.dumps(username.value)});"
                    )
                else:
                    ui.run_javascript("localStorage.removeItem('matofx_remember_user');")
                idle_timeout_minutes = response.get("session_idle_timeout_minutes")
                store_login(
                    token=response["access_token"],
                    user_id=response["user_id"],
                    username=username.value,
                    role=role,
                    theme=theme,
                    first_name=first_name,
                    locale=locale,
                    timezone=timezone,
                    is_approver=is_approver,
                    permissions=permissions,
                    ui_preferences=ui_preferences,
                    idle_timeout_minutes=(
                        int(idle_timeout_minutes)
                        if isinstance(idle_timeout_minutes, int)
                        and not isinstance(idle_timeout_minutes, bool)
                        else 30
                    ),
                )
                # Admitted over the licence cap as a Super User: stash the
                # notice so the dashboard can explain it on arrival (a
                # toast here would die with this page).
                if response.get("breakglass") is True:
                    store_breakglass_notice(
                        response.get("slots_in_use"), response.get("n_slots")
                    )
                ui.navigate.to(target)

            ui.button("Sign in", on_click=_submit).classes("w-full").props(
                "color=primary unelevated no-caps"
            ).style("margin-top: 0.2rem; height: 42px; font-weight: 600;")
            password.on("keydown.enter", _submit)
            username.on("keydown.enter", _submit)

            # Public 'Getting started' docs — the only documentation
            # reachable before sign-in (ADR-0014). Renders the `public`
            # set only.
            ui.separator().style("margin: 0.3rem 0; background: rgba(7,32,63,0.18);")
            with (
                ui.button(on_click=lambda: ui.navigate.to("/getting-started"))
                .props("flat no-caps color=primary")
                .classes("w-full")
                .style("font-weight: 600;")
            ):
                ui.html(
                    f'<div style="text-align:center;line-height:1.35;color:{_NAVY};">'
                    f"New to {_wordmark_html(title, ofx_colour=_TEAL)}?<br>"
                    "Read the getting-started guide</div>",
                    sanitize=False,
                )

        # Release / copyright line beneath the card, on the teal page.
        ui.html(
            f'<div style="text-align:center;color:{_NAVY};font-size:0.78rem;'
            'font-weight:500;letter-spacing:0.2px;">'
            f"{_wordmark_html(title, ofx_colour=_TEAL)} {_RELEASE_NAME} "
            f"&mdash; Funis Consulting Ltd.</div>"
        )

    # Prefill the remembered username (if any) once the form is mounted,
    # and drop focus on the password field so a returning user can type
    # straight away. Setting the native input value + dispatching 'input'
    # makes NiceGUI/Quasar pick the value up into the Python model.
    #
    # Run this on *client connect* rather than at page-build time: the
    # localStorage value only matters once the browser is attached, and a
    # build-time fire-and-forget ``ui.run_javascript`` schedules a NiceGUI
    # background task — which has no running loop under the in-process ASGI
    # test transport (``Client.on_connect`` callbacks don't fire during the
    # initial GET, so the sync-page probe stays clean).
    _prefill_js = (
        "setTimeout(function(){ try{"
        "  var u=localStorage.getItem('matofx_remember_user'); if(!u) return;"
        "  var inp=document.querySelector('.matofx-login-user input');"
        "  if(inp){"
        "    var s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
        "    s.call(inp,u); inp.dispatchEvent(new Event('input',{bubbles:true}));"
        "    inp.dispatchEvent(new Event('change',{bubbles:true}));"
        "  }"
        "  var cb=document.querySelector('.matofx-login-remember');"
        "  if(cb && cb.getAttribute('aria-checked')!=='true'){ cb.click(); }"
        "  var pw=document.querySelector('.matofx-login-pass input'); if(pw){ pw.focus(); }"
        "}catch(e){} }, 60);"
    )
    context.client.on_connect(lambda: ui.run_javascript(_prefill_js))
