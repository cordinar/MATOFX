# Air-gap asset vendoring — scope

**Status: BUILT 2026-08-13** (`DEPLOYMENT_VALIDATION_PLAN.md` P-03 / D-03;
ARCHITECTURE §16.68). Scoped 2026-06-26.
**Owner:** (unassigned)
**Trigger that fired:** Standard Industries confirmed a runtime air-gap, making
all three surfaces below customer-facing rather than hypothetical.

## What was actually built, and where it differed from this scope

The plan below was followed, with three deliberate departures — each recorded
here because each was a real finding, not a preference:

1. **model-viewer needed more than one file.** It reads
   `self.ModelViewerElement.dracoDecoderLocation` / `.ktx2TranscoderLocation`
   at module-evaluation time, defaulting both to `https://www.gstatic.com/…`,
   and loads them *lazily* — only for a model that actually uses Draco mesh
   compression or KTX2 textures. Vendoring only `model-viewer.min.js` would
   have passed every offline test and then failed on the first
   Draco-compressed `.glb` a user uploaded, which is what most exporters emit
   by default. The Draco 1.5.6 decoder and the pinned basis transcoder are
   vendored under `vendor/draco/` and `vendor/basis/`, fetched from the exact
   URLs model-viewer names, and both locations are set before the module loads.
2. **Plotly is not vendored as a file.** Step 1 below suggested pinning a
   `plotly.min.js` copy. Instead both consumers take the bundle from the
   installed Python `plotly` package via `plotly.offline.get_plotlyjs()` —
   the pattern `reports/builder.py` already used. This removes the
   "match the version, bump in lockstep" instruction entirely, and it fixed a
   live defect: the compare report pinned plotly.js **2.35.2** while plotly
   6.7.0 serialises **3.5.0** figure schema, a full major version apart with
   nothing that would ever have noticed.
3. **The glTF viewer gained the fallback that was marked "optional polish".**
   Step 2 called it optional; it isn't, given the plan's own G-06 criterion
   that nothing may degrade silently. A `<p>` child of `<model-viewer>` renders
   whenever the custom element fails to upgrade.

The cytoscape error copy was also rewritten: it used to blame the user's
network for a failed download, which is now the wrong diagnosis — the library
is served by MATOFX itself, so a failure points at this server.

**Known residual:** plotly.js carries a built-in `topojsonURL` default of
`https://cdn.plot.ly/un/`, used only by geographic map traces (`scattergeo` /
`choropleth`). MATOFX produces no such traces, so nothing fetches it, but the
string is present in the inlined bundle and a URL scanner will flag it.

**Not yet verified in a browser with egress blocked** — that is
`DEPLOYMENT_VALIDATION_PLAN.md` G-01…G-05 on ENV-B, and step 5 below still
stands.

---

## Original scope (2026-06-26), retained for the reasoning

**Trigger to build:** Any customer install on a network without outbound internet
where the lineage **Graph** view, **animated** glTF/GLB CAD preview, or
**downloaded** standalone Plotly HTML must render.

## Problem

MATOFX v2 is otherwise air-gap-clean: no telemetry/phone-home, offline-signed
Ed25519 licences, SQLite by default, email/LLM/remote-exec all opt-in and
off by default, and the **core** UI ships its whole frontend toolchain
locally (NiceGUI bundles Vue/Quasar/Tailwind/three.js/**plotly.min.js**/ECharts/
CodeMirror and serves them from the Python package).

But three rendering surfaces still pull JavaScript from a **public CDN at
runtime**. On a fully isolated network these `<script src="https://…">` loads
fail, so those specific renders break or degrade. Until vendored, we cannot
make a clean "no external JavaScript required" claim.

This is a known gap — see the existing in-code comments:
- `ui/_cytoscape.py:28-32` ("The air-gap-bundle vendoring step … is a separate follow-up")
- `ui/lineage_view.py:15-18` ("the air-gap-bundle vendoring step per design §6.1 is a separate follow-up")

## The three offenders

| # | Feature | File / symbol | CDN URL(s) | Behaviour when blocked |
|---|---------|---------------|------------|------------------------|
| 1 | Lineage **Graph** view + workflow-structure graph | `ui/_cytoscape.py` — `_CYTOSCAPE_CDN`, `_DAGRE_CDN`, `_CYTOSCAPE_DAGRE_CDN` | `cdn.jsdelivr.net/npm/cytoscape@3.30.0`, `…/dagre@0.8.5`, `…/cytoscape-dagre@2.5.0` | **Graceful.** Bootstrap catches the load error and shows "switch to the **Table** view" (always-available fallback). No data lost. |
| 2 | **Animated** glTF/GLB CAD preview | `ui/cad_preview.py` — `_MODEL_VIEWER_CDN`, used in `render_gltf_preview` | `ajax.googleapis.com/ajax/libs/model-viewer/3.5.0/model-viewer.min.js` | **Hard blank.** No fallback. NOTE: static STL preview (`render_stl_preview` → `ui.scene.stl`, bundled three.js) is unaffected and works offline. |
| 3 | "Download chart as standalone HTML" | `ui/theme.py` — `export_plotly_html`, `include_plotlyjs="cdn"` | plotly CDN (written into the downloaded file) | **Deferred blank.** In-app `ui.plotly` charts are fine (NiceGUI's bundled plotly). Only the *downloaded* file fails to plot *when opened* offline. |

Non-issues already handled / out of scope:
- HTML **reports** deliberately re-inline plotly — `reports/outputs.py:441-442`
  pulls the figure JSON out of the legacy `html_plot` CDN document and replays
  it against the report's own inline `plotly.min.js`. Already air-gap-safe.
- Usage export (`usage/export.py`) and lineage SVG (`lineage/svg_render.py`) are
  explicitly self-contained (no JS, no CDN, no external font).
- `ui/pages/demo.py` embeds `<iframe src="https://chrommine.com">` and the
  model-viewer CDN — **demo page only**, not part of normal operation. Leave as
  is, but the demo page should be expected to look broken on an air-gapped box
  (acceptable; it's a marketing/preview surface). Mentioned for completeness.

## Approach

Reuse the **existing packaged-asset mount** rather than inventing a new one:

- `ui/static_assets.py` already mounts `ui/assets/` at the absolute URL prefix
  `/matofx-assets` (`ASSETS_URL_PREFIX`), on the **outer** FastAPI app (not the
  `/ui` NiceGUI sub-app) so URLs are absolute.
- Hatchling bundles **every file under `src/matofx/`** into the wheel
  (`[tool.hatch.build.targets.wheel]`), so anything dropped in `ui/assets/`
  travels to every install automatically — exactly what air-gap needs.

> The `_cytoscape.py` comment says `ui/static/vendor/`, but the established,
> already-wired-and-wheel-bundled location is `ui/assets/`. Use
> `ui/assets/vendor/` and reference `/matofx-assets/vendor/<file>.js`. Update the
> stale comment.

### Steps

1. **Add the vendored JS files** under `src/matofx/ui/assets/vendor/`:
   - `cytoscape.min.js` (3.30.0)
   - `dagre.min.js` (0.8.5)
   - `cytoscape-dagre.min.js` (2.5.0)
   - `model-viewer.min.js` (3.5.0) — ES module; keep `type="module"`
   - `plotly.min.js` — match the version NiceGUI bundles / `plotly` pin in
     `.venv` to avoid figure-schema drift (check `plotly.__version__`; current
     tree has plotly in `.venv`, ~5.x/6.x — confirm at build time).
   - Pin exact versions; these are the versions already validated against the
     layout/animation code, so don't bump opportunistically.

2. **Flip the URLs to same-origin:**
   - `ui/_cytoscape.py`: change the three `_*_CDN` constants to
     `/matofx-assets/vendor/<file>.js`. The dynamic `loadScript` Promise-chain
     loader needs no other change. Keep the existing error-fallback (it now
     effectively never fires offline, but leave it for the soft-cap / large-graph
     case which reuses the same catch).
   - `ui/cad_preview.py`: change `_MODEL_VIEWER_CDN` to
     `/matofx-assets/vendor/model-viewer.min.js`. Consider adding a small
     fallback message mirroring the cytoscape pattern, since this path currently
     has none (optional polish, not required for the core fix).
   - `ui/theme.py` `export_plotly_html`: change `include_plotlyjs="cdn"` to
     `include_plotlyjs=True` (inlines full plotly.js → fully self-contained file,
     ~3 MB vs ~5 kB). This is a user-initiated one-off download, so size is an
     acceptable trade for "opens anywhere, offline". Alternative if size matters:
     `include_plotlyjs` set to a served `/matofx-assets/vendor/plotly.min.js`
     URL — but a downloaded file may be opened off-server, so **inline (`True`)
     is the safer default**. Update the docstring (currently claims CDN keeps it
     ~5 kB "at the cost of needing internet").

3. **Licensing / compliance:** drop each library's LICENSE next to it in
   `vendor/` (or a `vendor/LICENSES/` folder). All are permissive:
   cytoscape (MIT), dagre (MIT), cytoscape-dagre (MIT), model-viewer (Apache-2.0),
   plotly.js (MIT). Add them to the SBOM/attribution if the release SBOM
   enumerates frontend assets (it currently covers Python wheels via CycloneDX —
   confirm whether JS vendoring needs a manual attribution entry).

4. **Tests:**
   - `_cytoscape.py` has a pure-Python `_cytoscape_bootstrap_js` whose
     interpolation contract is unit-tested (the docstring says so). Update any
     test that asserts the `cdn.jsdelivr.net` strings to expect the
     `/matofx-assets/vendor/…` URLs.
   - Add a guard test that greps the `ui/` tree for `https://` in `<script src>`
     / `add_head_html` / `_CDN` constants and fails on any new CDN reference
     (excluding `demo.py`). This prevents regressions — a new feature silently
     re-introducing a CDN dep is exactly how this gap appeared.

5. **Verify** on a box with outbound HTTP firewalled (or DevTools "offline"):
   open a lineage Graph tab, an animated GLB preview, and download+open a Plotly
   HTML export. All three render with no network requests to external hosts.

6. **Docs:** update `INSTALL.md` / admin install chapter
   (`docs/admin/10-installing-and-setup.md`) to state plainly that no external
   JavaScript is fetched at runtime once this lands. Update the two stale
   in-code "separate follow-up" comments. Per project rule, ship help-doc
   changes in the same change.

## Effort

Small and contained — no architecture change, reuses the existing mount. Bulk of
the work is dropping ~5 files, flipping ~5 constants/args, fixing tests, and the
offline verification pass. Half a day, mostly verification + license hygiene.

## Out of scope (explicitly)

- The `demo.py` external iframe (`chrommine.com`) and its CDN model-viewer —
  marketing/preview surface, accepted to be internet-dependent.
- Any change to the licence, email, LLM, or remote-exec subsystems — already
  off-by-default and air-gap-clean.
- A new static mount or CSP overhaul. If a strict CSP is later added, these
  same-origin `/matofx-assets/` URLs satisfy `script-src 'self'`, which is a
  bonus reason to vendor.
