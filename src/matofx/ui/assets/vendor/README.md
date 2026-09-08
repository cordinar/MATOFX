# Vendored frontend libraries

Third-party JavaScript and WebAssembly served from this install rather than
fetched from a public CDN at runtime. This directory is what lets MATOFX claim
that **no external JavaScript is required to run the UI** — a hard requirement
for the air-gapped deployments this product targets.

Everything under `src/matofx/` is bundled into the wheel by hatchling, so these
files travel to every install automatically. They are served at
`/matofx-assets/vendor/…` by `matofx.ui.static_assets.mount_static_assets`.

## Do not edit, and do not bump casually

These are **exact upstream builds**, byte-for-byte, recorded with their source
URL and SHA-256 below. Each version is the one the calling code was written and
validated against — the dagre layout parameters, the model-viewer attribute set,
and the Plotly figure schema all assume it. Replacing a file means re-running the
offline verification pass in `designs/air-gap-asset-vendoring.md` §5, not just
swapping bytes.

To re-fetch or upgrade, download from the URL recorded here and check the digest.
There is no build step and no minifier in this repo — nothing here is generated.

## Contents

| File | Version | Licence | Source |
|---|---|---|---|
| `cytoscape.min.js` | 3.30.0 | MIT | `https://cdn.jsdelivr.net/npm/cytoscape@3.30.0/dist/cytoscape.min.js` |
| `dagre.min.js` | 0.8.5 | MIT | `https://cdn.jsdelivr.net/npm/dagre@0.8.5/dist/dagre.min.js` |
| `cytoscape-dagre.min.js` | 2.5.0 | MIT | `https://cdn.jsdelivr.net/npm/cytoscape-dagre@2.5.0/cytoscape-dagre.min.js` |
| `model-viewer.min.js` | 3.5.0 | Apache-2.0 (BSD-3-Clause portions) | `https://cdn.jsdelivr.net/npm/@google/model-viewer@3.5.0/dist/model-viewer.min.js` |
| `draco/` | 1.5.6 | Apache-2.0 | `https://www.gstatic.com/draco/versioned/decoders/1.5.6/` |
| `basis/` | build `2021-04-15-ba1c3e4` | Apache-2.0 | `https://www.gstatic.com/basis-universal/versioned/2021-04-15-ba1c3e4/` |

SHA-256, as fetched 2026-08-13:

```
a298b253e4b9b08bd0b2fe222ad67b2b9f42d057d7c17f7a050512079c46fddd  cytoscape.min.js
62eb9787ccfdbdf4148d4d99d31dbf9ee4770eafee81e637d759b52aac22cd51  dagre.min.js
abcc172f5ab9349d8f526fe35316e76cb7f877d00b51a53672592d0b6533c472  cytoscape-dagre.min.js
8923739c8c1b4a02dd9c8cf66da5c2a448235cb5e49e439dd7bbba944ba4fbe1  model-viewer.min.js
e8049906ef3f8f75d3456c22a3f31bfdfe5b5b5bd09ccdec613b9e9a49d554d8  draco/draco_wasm_wrapper.js
c55a594e8ffd18426d36b27fea9618af3df5e173640a3e56d46f09d76f0574f2  draco/draco_decoder.wasm
30e4d486fa020737af10e3c56197693e5b904bdc05b8bfa5d7b06751dec6da04  draco/draco_decoder.js
48a0ef319a28bf0224ee88ded34f74eaf97c175bba9eb18b47fb9720510ad6c4  basis/basis_transcoder.js
79ae97d781e10a566659c689b7bb1de91726453f55f9f5e3bcc07a4e3904070f  basis/basis_transcoder.wasm
```

Full licence texts are in `LICENSES/`.

## Why `draco/` and `basis/` are here

`model-viewer.min.js` does not fetch these eagerly, which is why they are easy to
miss: it reads `self.ModelViewerElement.dracoDecoderLocation` and
`.ktx2TranscoderLocation` at module-evaluation time, defaulting both to
`https://www.gstatic.com/…`, and only *loads* them when a model actually uses
Draco mesh compression or KTX2/basis textures. Vendoring the viewer alone would
therefore look fully offline in testing and then fail on the first
Draco-compressed `.glb` a user uploads — which most glTF exporters produce by
default. `matofx.ui.cad_preview` sets both locations to this directory before the
viewer module loads, so there is no gstatic dependency left on any path.

The files under `draco/` and `basis/` are fetched from the exact URLs
model-viewer names, not from a repackaged copy, because the builds differ between
distributions and the version pairing is what makes them interchangeable.

## Not vendored, deliberately

`matofx/ui/pages/demo.py` — the marketing/preview page — embeds an external
iframe and remote sample models. It is expected to look broken on an air-gapped
box and is out of scope (see the design doc). The CDN-regression guard test
excludes it for that reason.

Plotly is **not** vendored here. The Python `plotly` dependency already ships
`plotly.min.js` inside the wheel; `plotly.offline.get_plotlyjs()` returns it, and
inlining that guarantees the JavaScript matches the exact figure schema the
Python side serialised. A separately-pinned copy here could silently drift from
it — which is precisely the bug the compare report had before this change.
