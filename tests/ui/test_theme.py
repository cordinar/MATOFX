"""Pure-function tests for the theme module.

The chrome integration (``ui.dark_mode`` toggling, CSS variable
injection) needs a NiceGUI request context; those paths are exercised
by the API + e2e tests. Here we cover the colour-resolution helpers,
which are deterministic and don't need a running app.
"""

from __future__ import annotations

import colorsys

import plotly.graph_objects as go

from matofx.ui.theme import (
    DARK_PALETTE,
    DEFAULT_THEME,
    LIGHT_PALETTE,
    MATOFX_PLOTLY_CONFIG,
    apply_theme_to_plotly,
    palette_for,
    themed_plotly,
)


def test_palette_for_known_themes() -> None:
    assert palette_for("light") is LIGHT_PALETTE
    assert palette_for("dark") is DARK_PALETTE


def test_palette_for_unknown_falls_back_to_default() -> None:
    """Anything off-list resolves to the default theme rather than raising."""
    fallback = palette_for("neon")
    assert fallback.name == DEFAULT_THEME


_TRANSPARENT = "rgba(0,0,0,0)"
"""Plotly paper/plot backgrounds are left transparent so the chrome CSS
(``.js-plotly-plot { background: var(--matofx-card-bg) }``) paints the
card surface reactively — see ``apply_theme_to_plotly``."""


def test_apply_theme_to_plotly_dark() -> None:
    """Dark theme: transparent backgrounds + light font.

    The background is deliberately *not* baked here — the card-coloured
    fill comes from reactive CSS so it survives a live theme toggle.
    """
    fig = go.Figure()
    apply_theme_to_plotly(fig, theme="dark")
    layout = fig.layout
    assert layout.paper_bgcolor == _TRANSPARENT
    assert layout.plot_bgcolor == _TRANSPARENT
    assert layout.font.color == DARK_PALETTE.body_text


def test_apply_theme_to_plotly_light() -> None:
    """Light theme: transparent backgrounds + dark font."""
    fig = go.Figure()
    apply_theme_to_plotly(fig, theme="light")
    layout = fig.layout
    assert layout.paper_bgcolor == _TRANSPARENT
    assert layout.plot_bgcolor == _TRANSPARENT
    assert layout.font.color == LIGHT_PALETTE.body_text


def _relative_luminance(hex_colour: str) -> float:
    """WCAG relative luminance for an ``#rrggbb`` string."""
    raw = hex_colour.lstrip("#")
    channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_ratio(one: str, two: str) -> float:
    lighter, darker = sorted((_relative_luminance(one), _relative_luminance(two)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_accent_contrasts_with_its_own_surface() -> None:
    """Each theme's accent must be legible on that theme's card surface.

    This replaces an older assertion that the two palettes shared one
    accent *string*. They deliberately no longer do: dark uses #40A1A4
    and light darkens it to #2D7682 so it holds contrast against white
    (the palette comments say so). The old test was asserting the
    implementation of the rule rather than the rule, so a correct
    accessibility fix read as a regression.

    3.0:1 is the WCAG AA threshold for large text and UI components,
    which is what an accent is used for here — headings, icons, borders.
    """
    for palette in (LIGHT_PALETTE, DARK_PALETTE):
        ratio = _contrast_ratio(palette.accent, palette.card_bg)
        assert ratio >= 3.0, (
            f"{palette.name} accent {palette.accent} on {palette.card_bg} "
            f"is only {ratio:.2f}:1"
        )


def test_both_accents_are_the_same_brand_hue() -> None:
    """Darkened for contrast, not changed to a different colour."""
    hues = []
    for palette in (LIGHT_PALETTE, DARK_PALETTE):
        raw = palette.accent.lstrip("#")
        rgb = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        hues.append(colorsys.rgb_to_hsv(*rgb)[0] * 360)
    assert abs(hues[0] - hues[1]) <= 15, f"accents are different hues: {hues}"


def test_matofx_plotly_config_hides_logo() -> None:
    """Sanity check: the default config strips the Plotly logo."""
    assert MATOFX_PLOTLY_CONFIG["displaylogo"] is False


def test_themed_plotly_returns_dict_with_config() -> None:
    """themed_plotly bundles theme tints + the runtime config in one payload."""
    fig = go.Figure()
    payload = themed_plotly(fig, theme="dark")
    assert "data" in payload
    assert "layout" in payload
    assert "config" in payload
    config = payload["config"]
    assert isinstance(config, dict)
    assert config["displaylogo"] is False
    # Layout was tinted to the requested palette (transparent paper so
    # the reactive CSS card background shows through).
    layout = payload["layout"]
    assert isinstance(layout, dict)
    assert layout.get("paper_bgcolor") == _TRANSPARENT


def test_themed_plotly_config_is_independent_per_call() -> None:
    """Mutating the returned config doesn't leak into the module constant."""
    payload = themed_plotly(go.Figure(), theme="light")
    payload["config"]["responsive"] = "mutated"
    fresh = themed_plotly(go.Figure(), theme="light")
    assert fresh["config"]["responsive"] is True


def test_no_chart_in_the_app_bypasses_the_plotly_config() -> None:
    """``displaylogo: False`` only takes effect if the chart is handed to
    NiceGUI as a payload *dict carrying the config*.

    ``ui.plotly(apply_theme_to_plotly(fig))`` passes a bare figure — themed,
    but with no config — so Plotly falls back to its defaults and the
    "Produced with Plotly" link reappears on the modebar. That is exactly how
    the Job Cohorts charts drifted back. Scan the source for the mistake
    rather than trusting each call site to remember.
    """
    import re  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    package_root = Path(__file__).resolve().parents[2] / "src" / "matofx"
    # The payload builders: `themed_plotly` and any thin wrapper of it
    # (e.g. studies' `_static_plotly`, or a `theme` callable passed in).
    allowed = re.compile(r"^(themed_plotly|_static_plotly|theme)\b")
    offenders: list[str] = []
    for path in package_root.rglob("*.py"):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            # Prose, not code: comments and the ``ui.plotly(fig)`` rst
            # literal in theme.py's own docstring explaining this rule.
            if stripped.startswith("#") or "``" in line:
                continue
            match = re.search(r"\bui\.plotly\(\s*(\S.*)?$", line)
            if match is None:
                continue
            argument = (match.group(1) or "").strip()
            if not argument or allowed.match(argument):
                continue
            offenders.append(f"{path.relative_to(package_root)}:{lineno}: {line.strip()}")
    assert not offenders, "charts bypassing MATOFX_PLOTLY_CONFIG:\n" + "\n".join(offenders)
