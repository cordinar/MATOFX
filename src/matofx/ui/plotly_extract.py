"""Shared helper for pulling a Plotly figure out of a ``write_html`` dump.

Plotly's ``write_html(..., include_plotlyjs="cdn")`` produces an HTML
page that loads ``plotly.js`` from a CDN and then calls
``Plotly.newPlot(div_id, data, layout, config)``. Embedding that page
inside a NiceGUI ``<iframe srcdoc>`` doesn't render reliably — the
browser's CSP / COEP rules typically block the cross-origin script
fetch, and the frame ends up blank. The robust alternative is to
parse the ``Plotly.newPlot`` call out of the HTML, hand the
``data`` + ``layout`` JSON to NiceGUI's bundled plotly element, and
let the dashboard's existing :func:`matofx.ui.theme.themed_plotly`
helper apply the theme.

This module owns the parser. It used to live as private helpers
inside :mod:`matofx.ui.pages.job` for the My Jobs HTML viewer; the
Compare workspace's cell-level inline render and the merged-overlay
HTML expander now also need it, so it's been promoted to a shared
seam. One implementation, one set of tests, one place to update when
Plotly changes its emit format.
"""

from __future__ import annotations

import json
from typing import Any

_PLOTLY_NEWPLOT_MARKER: str = "Plotly.newPlot("


def extract_plotly_figure(html_text: str) -> dict[str, Any] | None:
    """Pull the figure's ``data`` + ``layout`` out of a plotly-rendered HTML page.

    Returns ``{"data": [...], "layout": {...}}`` when the page came
    from ``plotly.io.write_html``, ``None`` otherwise (the caller
    falls back to an iframe or a "download" link).

    Implementation notes:

    - ``Plotly.newPlot(div_id, data, layout, config)`` — the first
      argument is a JS double-quoted string (the div id we don't care
      about), the next two are JSON literals we want.
    - Plotly's formatter inserts whitespace and newlines between
      arguments. The scanner skips both.
    - Strings inside ``data`` / ``layout`` may contain bracket
      characters; the balanced-bracket scanner tracks quoted strings
      with backslash-escape handling so a brace inside a label
      doesn't shift the depth.
    """
    start = html_text.find(_PLOTLY_NEWPLOT_MARKER)
    if start < 0:
        return None
    after_id = _skip_js_string(html_text, start + len(_PLOTLY_NEWPLOT_MARKER))
    if after_id < 0:
        return None
    data_start = _skip_comma_ws(html_text, after_id)
    data_end = _scan_balanced(html_text, data_start, "[", "]")
    if data_end < 0:
        return None
    layout_start = _skip_comma_ws(html_text, data_end + 1)
    layout_end = _scan_balanced(html_text, layout_start, "{", "}")
    if layout_end < 0:
        return None
    try:
        data = json.loads(html_text[data_start : data_end + 1])
        layout = json.loads(html_text[layout_start : layout_end + 1])
    except json.JSONDecodeError:
        return None
    return {"data": data, "layout": layout}


def _skip_js_string(s: str, pos: int) -> int:
    """Step past a JS double-quoted string starting near ``pos``.

    Whitespace before the opening quote is skipped; backslash escape
    sequences inside the string are honoured (so ``"He said \"hi\""``
    parses without truncating at the inner quote). Returns the index
    *after* the closing quote, or ``-1`` when the text doesn't start
    with a quoted string.
    """
    while pos < len(s) and s[pos].isspace():
        pos += 1
    if pos >= len(s) or s[pos] != '"':
        return -1
    pos += 1
    while pos < len(s):
        if s[pos] == "\\":
            pos += 2  # skip ``\X``; valid for ``\"`` / ``\\`` / others
            continue
        if s[pos] == '"':
            return pos + 1
        pos += 1
    return -1


def _skip_comma_ws(s: str, pos: int) -> int:
    """Skip leading commas + whitespace; return the next non-trivial index."""
    while pos < len(s) and (s[pos] == "," or s[pos].isspace()):
        pos += 1
    return pos


def _scan_balanced(s: str, pos: int, open_c: str, close_c: str) -> int:
    """Index of the bracket that closes the one at ``pos``.

    Strings are honoured (so ``"]"`` inside a label doesn't drop the
    depth to zero); backslash escapes are honoured inside strings.
    Returns ``-1`` if ``s[pos]`` isn't ``open_c`` or if the closing
    bracket is never reached.
    """
    if pos >= len(s) or s[pos] != open_c:
        return -1
    depth = 0
    in_string = False
    escape = False
    while pos < len(s):
        c = s[pos]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return pos
        pos += 1
    return -1
