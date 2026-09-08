"""Dashboard theme — light/dark palettes + Plotly integration.

The chrome reads the active palette via :func:`current_palette` on
every page render so the colours line up with the user's stored
``theme_preference`` (see ``matofx.users.orm.User.theme_preference``).
The palette holds every colour the dashboard actually uses; pages
should look up tokens here rather than re-introducing hex literals.

Plot integration: :func:`apply_theme_to_plotly` swaps the figure's
template, paper / plot backgrounds and text colour to whichever side
of the palette the user is on. Charts must route through this helper
instead of pinning ``template="plotly_white"`` (legacy MATOFX did
that, which left a glaring white panel inside the dark page).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    import plotly.graph_objects as go


ThemeName = Literal["light", "dark"]
"""The two theme names that the rest of the app passes around."""

DEFAULT_THEME: ThemeName = "dark"
"""Fallback used when the user's preference hasn't been loaded yet."""


@dataclass(frozen=True)
class Palette:
    """Every colour the dashboard renders, for one theme.

    Kept flat (no nested dicts) so call sites read as
    ``palette.page_bg`` rather than ``palette["surfaces"]["page"]`` —
    fewer keystrokes, fewer typos.
    """

    name: ThemeName
    page_bg: str
    """Behind every page; the body background."""

    drawer_bg: str
    """Left navigation rail."""

    card_bg: str
    """Quasar card surface — kept transparent in dark, off-white in light."""

    dialog_bg: str
    """Background for modal pop-ups. **Always solid** so text from the
    page behind doesn't bleed through — the regular ``card_bg`` is
    transparent on the dark theme, which reads fine on the page but
    poorly inside a modal."""

    card_border: str
    """The thin rule line that replaces the box-shadow per durable user pref."""

    body_text: str
    """Primary text colour."""

    muted_text: str
    """Helper / hint text — readable but recessive."""

    label_text: str
    """Form-row labels (the bolder column on identity rows)."""

    error_text: str
    """Error message colour — kept high-contrast on both themes."""

    drawer_item_text: str
    """Sidebar nav-item label colour."""

    drawer_group_label: str
    """Sidebar group-heading colour."""

    accent: str
    """Funis brand teal, in the shade that reads on *this* theme's surface.

    Deliberately **not** the same string on both. Dark uses #40A1A4;
    light darkens it to #2D7682, which is the same brand teal taken down
    far enough to hold contrast against white. Using one literal on both
    would fail contrast on one of them — see
    ``test_accent_contrasts_with_its_own_surface``.
    """

    plotly_template: str
    """Built-in plotly template name to compose with the palette overrides."""


# Dark palette — deep near-black surfaces (unchanged). The brand navy
# (#07203F) is too dark to use as text *on* this theme, so the dark side
# keeps light text + the brand teal accent; navy is reserved for text on
# the light theme. Secondary brand teals appear only as small accents
# (the brand mark, the OFX wordmark, ``--matofx-accent-bright``).
DARK_PALETTE = Palette(
    name="dark",
    page_bg="#15171a",
    drawer_bg="#121417",
    card_bg="#1d2025",
    dialog_bg="#22262c",
    card_border="rgba(255,255,255,0.08)",
    body_text="#e8eaed",
    muted_text="#9aa0a6",
    label_text="#b0b3b8",
    error_text="#f0716b",
    drawer_item_text="#9aa0a6",
    drawer_group_label="#6b7177",
    accent="#40A1A4",
    plotly_template="plotly_dark",
)

# Light palette — neutral-light page + white panels, with the brand navy
# (#07203F) for text and a brand teal (#2D7682) accent that keeps contrast
# on white. The mint #EAF8F6 stays an accent (small doses), not a surface.
LIGHT_PALETTE = Palette(
    name="light",
    page_bg="#eef1f4",
    drawer_bg="#ffffff",
    card_bg="#ffffff",
    dialog_bg="#ffffff",
    card_border="rgba(7,32,63,0.10)",
    body_text="#07203F",
    muted_text="#5a6b78",
    label_text="#1A4B61",
    error_text="#c0392b",
    drawer_item_text="#07203F",
    drawer_group_label="#5a6b78",
    accent="#2D7682",
    plotly_template="plotly_white",
)


_PALETTES: dict[ThemeName, Palette] = {
    "dark": DARK_PALETTE,
    "light": LIGHT_PALETTE,
}


def palette_for(theme: ThemeName | str) -> Palette:
    """Return the palette for ``theme``, falling back to the default."""
    if theme in _PALETTES:
        return _PALETTES[theme]
    return _PALETTES[DEFAULT_THEME]


def current_palette() -> Palette:
    """Resolve the palette from the logged-in user's stored preference.

    Imported lazily because :mod:`matofx.ui.state` reads from NiceGUI's
    per-client storage, which we don't want to require at import time
    (the unit tests poke this module without a NiceGUI app context).
    """
    from matofx.ui.state import current_theme  # noqa: PLC0415

    return palette_for(current_theme())


def apply_theme_to_plotly(fig: go.Figure, theme: ThemeName | None = None) -> go.Figure:
    """Tint a Plotly figure to match the active theme.

    Sets the template, paper / plot backgrounds and font colour so a
    chart reads cleanly regardless of which side of the palette the
    user is on. Pass ``theme`` explicitly for tests; in normal use the
    helper resolves it from the stored preference.

    The paper / plot backgrounds are left **transparent** rather than
    baked to a palette colour. Every chart sits inside a card, and the
    chrome CSS paints the chart's own root div with the card surface
    (``.js-plotly-plot { background-color: var(--matofx-card-bg) }``)
    plus the hairline ``--matofx-card-border`` frame. Because those are
    CSS variables, the background re-tints the instant the user flips
    the theme — whereas a colour baked into the figure JSON here would
    stay frozen at its render-time value (a dark panel left stranded on
    the light page after a no-reload theme toggle). The template + font
    colour are still palette-driven so a freshly-rendered chart reads
    correctly on whichever side it loads on.
    """
    palette = palette_for(theme) if theme is not None else current_palette()
    fig.update_layout(
        template=palette.plotly_template,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": palette.body_text},
    )
    return fig


MATOFX_PLOTLY_CONFIG: dict[str, object] = {
    "displaylogo": False,
    "responsive": True,
}
"""Default Plotly runtime config for every chart in the dashboard.

``displaylogo: False`` strips the "Produced with Plotly" link from
the modebar — durable user preference applied across the app.
``responsive: True`` lets the chart re-flow as the surrounding
column resizes, which matters in the Compare grid where columns
share width across N jobs.

Plotly's runtime config can't live on the figure layout — it's a
separate argument to :js:`Plotly.newPlot`. NiceGUI's plotly element
accepts it when the figure is passed as a ``dict`` with ``data`` /
``layout`` / ``config`` keys, so :func:`themed_plotly` below builds
that shape.
"""


def _finitize(obj: Any) -> Any:
    """Recursively replace non-finite floats (``NaN`` / ``±Inf``) with ``None``.

    ``NaN`` and ``Infinity`` are **not valid JSON**: if they reach the wire in
    a Plotly payload the browser's socket.io parser rejects the whole message,
    drops the connection, and the page reloads — which, on an auto-rendering
    surrogate plot, becomes an endless render→refresh loop. Non-finite values
    turn up legitimately (an unconstrained GP extrapolating out of range; a
    ``predict(return_std=True)`` variance that goes slightly negative → ``sqrt``
    → ``NaN``), so every chart is sanitised here rather than at each call site.
    Plotly renders ``None`` as a gap, which is the honest representation of "no
    valid value at this point". numpy arrays/scalars are handled via ``tolist``.
    """
    if isinstance(obj, bool):  # bool is an int subclass — leave it be
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _finitize(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_finitize(v) for v in obj]
    tolist = getattr(obj, "tolist", None)  # numpy ndarray / scalar
    return _finitize(tolist()) if callable(tolist) else obj


def themed_plotly(fig: go.Figure, theme: ThemeName | None = None) -> dict[str, object]:
    """Apply the theme + return the dict NiceGUI's :func:`ui.plotly` wants.

    Combines :func:`apply_theme_to_plotly` with the runtime config so
    every chart in the dashboard ships with the same look (background
    matches the page) and the same behaviour (no Plotly logo, fluid
    resize). Use it like ``ui.plotly(themed_plotly(fig)).classes(...)``
    in place of bare ``ui.plotly(fig)``.

    The payload is passed through :func:`_finitize` so a stray ``NaN`` /
    ``Inf`` (e.g. an out-of-range surrogate prediction) can never ship
    invalid JSON to the client and force a reconnect / page reload.
    """
    apply_theme_to_plotly(fig, theme)
    payload: dict[str, object] = dict(fig.to_plotly_json())
    payload["config"] = dict(MATOFX_PLOTLY_CONFIG)
    return cast("dict[str, object]", _finitize(payload))


def export_plotly_html(fig: go.Figure, theme: ThemeName | None = None) -> bytes:
    """Render a Plotly figure as a self-contained HTML download.

    ``include_plotlyjs=True`` inlines the whole of plotly.js, so the
    file is ~5 MB rather than ~5 kB — and opens and plots anywhere,
    with no network at all. That trade is deliberate. This is a
    user-initiated one-off download whose whole point is to leave the
    application, so it may well be opened on a machine that has never
    heard of this server; the previous ``'cdn'`` setting produced a
    file that plotted on the author's laptop and showed a blank page
    on an air-gapped site, which is the worst of both.

    Returns UTF-8 bytes ready to feed into
    :func:`nicegui.ui.download.content`. The figure is theme-tinted
    first so the exported file matches the look of the dashboard the
    user just downloaded it from.
    """
    import io  # noqa: PLC0415 — cheap, narrow scope

    import plotly.io as pio  # noqa: PLC0415 — defer plotly until export time

    apply_theme_to_plotly(fig, theme)
    buf = io.StringIO()
    pio.write_html(
        fig,
        file=buf,
        include_plotlyjs=True,
        full_html=True,
        config=dict(MATOFX_PLOTLY_CONFIG),
    )
    return buf.getvalue().encode("utf-8")
