"""Shared Cytoscape.js canvas bootstrap (ADR-0010 slice 4b + workflow-graph slice 1).

Both the lineage / provenance view (:mod:`matofx.ui.lineage_view`) and the
workflow-structure view (:mod:`matofx.ui.workflow_graph_view`) render a
node-link diagram via Cytoscape.js + dagre. This module factors out the
mount + bootstrap JS so the two callers don't duplicate the
script-loading + DOM-id wrangling logic.

The libraries are served from this install (``ui/assets/vendor/``), not
a public CDN — see the constants below.

The bootstrap dynamically loads its own deps via
``document.createElement('script')`` + Promise chain — see
``_cytoscape_bootstrap_js`` for the rationale (NiceGUI's
``add_head_html`` timing isn't reliable post-await, which is exactly
when our render functions run; the dynamic-loader pattern is
oblivious to when the bootstrap fires).

Callers pre-format their elements + stylesheet to Cytoscape's native
shapes. Per-kind styling is the caller's concern.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

from nicegui import ui

from matofx.ui.static_assets import ASSETS_URL_PREFIX

# Cytoscape.js + dagre, served from this install rather than a public
# CDN — the deployments this product targets have no outbound internet,
# and a ``<script src="https://…">`` there simply never resolves. The
# files live under ``ui/assets/vendor/`` (hatchling bundles everything
# beneath ``src/matofx`` into the wheel, so they travel to every
# install) and are mounted at :data:`ASSETS_URL_PREFIX` on the *outer*
# FastAPI app, which is why these URLs are absolute rather than
# relative to the NiceGUI ``/ui`` sub-app.
#
# Versions are pinned by what is checked in, not by a URL: see
# ``ui/assets/vendor/README.md`` for the digests and the reason not to
# bump them opportunistically.
_VENDOR_URL_PREFIX = f"{ASSETS_URL_PREFIX}/vendor"
_CYTOSCAPE_JS = f"{_VENDOR_URL_PREFIX}/cytoscape.min.js"
_DAGRE_JS = f"{_VENDOR_URL_PREFIX}/dagre.min.js"
_CYTOSCAPE_DAGRE_JS = f"{_VENDOR_URL_PREFIX}/cytoscape-dagre.min.js"

# Unique-id seed for the per-mount container. Multiple canvases on
# one page (lineage tab + workflow graph tab, side-by-side compares,
# etc.) need distinct DOM ids so their JS bridges don't collide.
_mount_counter: itertools.count[int] = itertools.count(1)


def render_cytoscape_canvas(
    *,
    elements: list[dict[str, Any]],
    stylesheet: list[dict[str, Any]],
    centre_id: str | None = None,
    height_px: int = 720,
    rank_dir: str = "TB",
) -> None:
    """Mount a Cytoscape.js canvas with ``elements`` + ``stylesheet``.

    ``elements`` is a list of Cytoscape elements
    (``{"data": {"id": ..., ...}}`` for nodes;
    ``{"data": {"id": ..., "source": ..., "target": ..., ...}}`` for
    edges; optional ``"classes": "..."`` per element). ``stylesheet``
    is a list of ``{"selector": "...", "style": {...}}``. Both shapes
    are Cytoscape's native format — no translation here.

    ``centre_id`` is the ``data.id`` of the node the viewport should
    centre on first render. ``None`` lets dagre pick.

    ``rank_dir`` is the dagre layout direction: ``"TB"`` (top→bottom,
    the lineage default), ``"LR"`` (left→right, more typical for
    workflows), etc.

    Node click handlers route to ``data.deep_link`` via
    ``window.location.href`` when one is set — same full-nav
    behaviour the lineage view uses. Caller can omit
    ``deep_link`` to render nodes as inert.
    """
    instance_id = next(_mount_counter)
    mount_id = f"matofx-cy-{instance_id}"

    # Plain container div — no inline script (rejected by NiceGUI 3.x).
    ui.html(
        f'<div id="{mount_id}" style="width:100%; height:{height_px}px;'
        f" border: 1px solid var(--matofx-border, #ccc); border-radius: 4px;"
        f' background: var(--matofx-page-bg, #fff);"></div>',
        sanitize=False,
    ).classes("w-full")

    bootstrap = _cytoscape_bootstrap_js(
        mount_id=mount_id,
        elements=elements,
        stylesheet=stylesheet,
        centre_id=centre_id or "",
        rank_dir=rank_dir,
    )
    # ``ui.run_javascript`` is fire-and-forget (NiceGUI awaits
    # ``client.connected()`` internally per the 3.0+ docstring). The
    # bootstrap loads cytoscape via ``document.createElement('script')``
    # so this works regardless of when the canvas mounts — initial
    # render, post-await page handler, chip-toggle re-render, etc.
    ui.run_javascript(bootstrap)


def _cytoscape_bootstrap_js(
    *,
    mount_id: str,
    elements: list[dict[str, Any]],
    stylesheet: list[dict[str, Any]],
    centre_id: str,
    rank_dir: str,
) -> str:
    """Build the JS that mounts cytoscape against ``mount_id``.

    Self-contained — dynamically loads cytoscape + dagre + cytoscape-dagre
    via ``document.createElement('script')`` + Promise chain, then
    mounts the graph. The dynamic-loader pattern is independent of
    ``ui.add_head_html`` timing (which is unreliable when the view
    renders post-await — see the lineage view's slice-5b fix
    history).

    Pure-Python helper so the test suite can pin the interpolation
    contract without spinning up a browser.
    """
    payload = json.dumps(
        {
            "elements": elements,
            "style": stylesheet,
            "centre": centre_id,
        }
    )
    return f"""
(function() {{
  function loadScript(src) {{
    return new Promise(function(resolve, reject) {{
      var existing = document.querySelector('script[data-matofx-src=' + JSON.stringify(src) + ']');
      if (existing) {{
        if (existing.dataset.matofxLoaded === 'true') return resolve();
        existing.addEventListener('load', function() {{ resolve(); }});
        existing.addEventListener('error', function() {{
          reject(new Error('load failed: ' + src));
        }});
        return;
      }}
      var s = document.createElement('script');
      s.src = src;
      s.dataset.matofxSrc = src;
      s.onload = function() {{ s.dataset.matofxLoaded = 'true'; resolve(); }};
      s.onerror = function() {{ reject(new Error('load failed: ' + src)); }};
      document.head.appendChild(s);
    }});
  }}

  function ensureCytoscape() {{
    var chain = Promise.resolve();
    if (!window.cytoscape) {{
      chain = chain.then(function() {{ return loadScript({_CYTOSCAPE_JS!r}); }});
    }}
    if (!window.dagre) {{
      chain = chain.then(function() {{ return loadScript({_DAGRE_JS!r}); }});
    }}
    chain = chain.then(function() {{
      if (window.cytoscape && window.cytoscape.__matofxDagreLoaded) return;
      return loadScript({_CYTOSCAPE_DAGRE_JS!r}).then(function() {{
        if (window.cytoscape && window.cytoscapeDagre) {{
          window.cytoscape.use(window.cytoscapeDagre);
          window.cytoscape.__matofxDagreLoaded = true;
        }}
      }});
    }});
    return chain;
  }}

  function mount() {{
    var data = {payload};
    var el = document.getElementById({mount_id!r});
    if (!el) {{ return setTimeout(mount, 50); }}
    ensureCytoscape().then(function() {{
      var cy = window.cytoscape({{
        container: el,
        elements: data.elements,
        style: data.style,
        layout: {{ name: 'dagre', rankDir: {rank_dir!r}, nodeSep: 40, rankSep: 70 }},
        wheelSensitivity: 0.2,
        minZoom: 0.2,
        maxZoom: 3,
      }});
      cy.on('tap', 'node', function(evt) {{
        var link = evt.target.data('deep_link');
        if (!link) {{ return; }}
        // NiceGUI is mounted under a sub-path (e.g. ``/ui``) and deep_links
        // are mount-relative (start with '/', no prefix) so the table view's
        // ``ui.navigate.to`` — which is itself mount-relative — resolves them.
        // A raw ``window.location.href`` bypasses NiceGUI's router, so we must
        // prepend the current mount prefix here or the node click 404s.
        var seg = window.location.pathname.split('/')[1] || '';
        var prefix = seg ? '/' + seg : '';
        if (link.charAt(0) === '/' && prefix && link.indexOf(prefix + '/') !== 0) {{
          link = prefix + link;
        }}
        window.location.href = link;
      }});
      // Only nodes that actually lead somewhere viewable get the
      // link cursor — inert nodes (no deep_link) stay default so the
      // affordance matches the click behaviour above.
      cy.on('mouseover', 'node', function(evt) {{
        if (evt.target.data('deep_link')) {{ el.style.cursor = 'pointer'; }}
      }});
      cy.on('mouseout', 'node', function() {{ el.style.cursor = 'default'; }});
      if (data.centre) {{
        var centre = cy.getElementById(data.centre);
        if (centre && centre.length) {{ cy.center(centre); }}
      }}
    }}).catch(function(err) {{
      // Keep the technical reason for support, out of the user's face.
      try {{ console.error('[MATOFX] Provenance diagram could not render:', err); }} catch (e) {{}}
      var msg = (err && err.message) ? String(err.message) : '';
      var loadFailed = msg.indexOf('load failed') !== -1;
      var why = loadFailed
        // The drawing library is served by MATOFX itself, so this is no
        // longer "you are offline" — it means the request to this server
        // failed. Say that, or an administrator goes looking at the
        // firewall for a problem that isn't there.
        ? 'The diagram needs a small drawing library that this MATOFX server '
          + 'didn\\u2019t serve. It is included with MATOFX rather than downloaded '
          + 'from the internet, so this points at the server or something between '
          + 'you and it \\u2014 worth reporting to your administrator.'
        : 'This usually happens when the web of links is very large or has an unusual '
          + 'shape the diagram can\\u2019t lay out.';
      el.innerHTML =
        '<div style="padding:1.25rem;text-align:center;color:var(--matofx-muted-text);">' +
        '<div style="font-weight:600;font-size:1.05rem;margin-bottom:0.4rem;color:inherit;">' +
        'The diagram couldn\\u2019t be drawn</div>' +
        '<div style="max-width:34rem;margin:0 auto;">' + why +
        ' Nothing is lost \\u2014 switch to the <strong>Table</strong> view using the ' +
        'View buttons above to see everything as a list.</div></div>';
    }});
  }}

  mount();
}})();
""".strip()
