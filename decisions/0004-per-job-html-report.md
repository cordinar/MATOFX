# 0004. Per-job self-contained HTML report

Date: 2026-05-20
Status: Accepted

## Context

Customers asked for a way to share the results of a finished job outside MATOFX
— with reviewers, regulators, or end-clients who don't have a MATOFX account.
The dashboard's job page already renders inputs and outputs in descriptor-
grouped panels, but it requires a logged-in browser session against the
deployment. There is currently no portable, self-contained artefact a user can
download, archive, or attach to an email.

The legacy MATOFX app shipped no such feature; this is a new capability.

Operational constraints surfaced during the discovery conversation:

- The artefact must be **fully self-contained** — every image, plot, video,
  CSS rule, and JS bundle inlined, no external references at all. The recipient
  may open it on an air-gapped machine.
- It must be **modern-looking** but produced with **zero external CSS/JS
  frameworks** at runtime — every byte ships inside the file.
- It must be **branded** by the deployment's existing branding (logo, accent
  colour, app title) so a customer's instance produces customer-branded
  reports without code changes.
- File size has to be **bounded**. Some MATOFX tools produce 30–80 MB rendered
  videos; embedding those verbatim into every report is not viable for email.
- It must mirror the **existing UI's descriptor-group layout** so a reader
  sees the same logical structure as someone using the live dashboard, and so
  changes to the descriptor automatically flow into the report — no per-tool
  report templates.

## Decision

**1. Server-side generator at `GET /api/jobs/{job_id}/report`.** A new
endpoint in a new `matofx.reports` package builds the HTML in-process from the
authoritative job + descriptor + outputs document. Returns `text/html` with
`Content-Disposition: attachment; filename="report_job{id}_{alias}_{YYYYMMDD}.html"`.
Auth gate: same as `GET /api/jobs/{job_id}` (owner / team member / admin).
Returns `404 Not Found` when the job is not in the `Complete` state — partial
reports for in-flight or failed runs are out of scope for the first ship.
Each successful generation writes an audit row with action
`job.report.generate`, resource type `job`, resource id `{job_id}`.

**2. Jinja2 template + inline CSS in a single self-contained document.**
`matofx/reports/templates/report.html.j2` carries one `<style>` block; no
external stylesheets, no `<link>` to fonts, no CDN-loaded JS. A system font
stack (`-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, ...`) gives a
modern look without bundling fonts. The accent colour, header icon, and app
display title come from `matofx.ui.branding_cache` at generation time, so
existing Admin → Branding edits drive the report's look with no extra
configuration surface.

**3. Adaptive descriptor-driven layout.** Each descriptor `input_group` /
`output_group` renders as a card; cards lay out in a CSS Grid container with
`grid-template-columns: repeat(auto-fit, minmax(360px, 1fr))` and
`grid-auto-flow: dense`. Net effect: a tool with three small numeric input
groups renders them side-by-side, a tool with one large group renders it full
width, and the layout reflows on narrow viewports / print without per-tool
tuning. Outputs that produce wide content (Plotly plots, CSV tables, videos,
PDFs) opt out of the grid via `grid-column: 1 / -1` and span all columns, so
no plot is rendered as a 360 px wide sliver next to a 360 px text card. A
`@media print { .card { break-inside: avoid; } }` rule keeps groups from
splitting across pages.

**4. Asset-type-specific embedding policy.** Per output / input file, picked
by the same extension classification used by `matofx.ui.pages.job`:

| Kind                                | Embed strategy                                                                                                          |
|-------------------------------------|-------------------------------------------------------------------------------------------------------------------------|
| Image (`png`, `jpg`, `gif`, `webp`) | Decode with Pillow; if raw bytes > 256 KB and the image has no alpha, re-encode as JPEG quality 85; embed as `data:` URI. |
| Image (`svg`)                       | Inline as-is inside the document.                                                                                       |
| CSV declared as `csv_plot`          | Interactive Plotly figure: a single `plotly.min.js` block at the top of the document (from `plotly.offline.get_plotlyjs()`), plus one `<div>` + figure JSON per chart. Light theme regardless of dashboard theme so the report prints cleanly. |
| CSV declared as `csv_table`         | HTML `<table>` of the first 100 rows; if more rows exist, a `<p class="truncation-note">N more rows — available in MATOFX</p>` line below the table. |
| Video (`mp4`, `webm`, `mov`, ...)   | If raw bytes ≤ 25 MB, embed as a base64 `<video controls>` block. Above 25 MB, show filename + size + "Not embedded — over 25 MB. Available in MATOFX." Never re-encode (no ffmpeg dependency). |
| PDF                                 | Base64 `<embed type="application/pdf">` — supported offline by Chrome / Edge / Firefox.                                  |
| HTML output                         | Base64 `srcdoc` inside a sandboxed `<iframe>`.                                                                          |
| 3D animation (`gltf`, `glb`)        | Filename + size + "Not previewable in report" badge. No three.js bundled; the report stays light.                       |
| Anything else                       | Filename + size + "Available in MATOFX" badge.                                                                          |

Filename is **always** rendered for every input/output file, regardless of
whether the contents are embedded — the report serves as a complete record
of what the job produced, even when individual artefacts can't be inlined.

**5. Workflow context, when applicable.** If the job has a non-null
`workflow_id`, the report renders a workflow context card between the
metadata card and the Inputs section, naming the parent workflow and listing
each sibling job's tool name + descriptor-grouped inputs in a collapsible
`<details>` block. Outputs of siblings are deliberately not pulled in — that
would explode report size and duplicate content; the report is per-job, with
upstream-input traceability.

**6. User-locale-aware timestamps.** All timestamps (job submitted, job
completed, report generated) render formatted under the requesting user's
stored `locale_preference` and `timezone_preference`, the same prefs that
drive the rest of the dashboard's regional display.

**7. UI access from My Jobs and the job detail page.** A `Download report`
button appears on every `Complete` row of `my_jobs.py`, plus a top-of-page
button on the job detail page. Both call a new
`ApiClient.download_job_report(job_id) -> bytes` and trigger a browser
download via NiceGUI's `ui.download(...)`. Non-Complete jobs do not show the
button.

## Consequences

- **New package**: `src/matofx/reports/` (`builder.py`, `assets.py`,
  `templates/report.html.j2`). Treated as a leaf package — depends on the
  job / descriptor / branding services but nothing depends on it except the
  new API router.
- **New router**: `src/matofx/api/routers/reports.py` registered in
  `matofx.api.app`.
- **No new runtime dependencies expected.** Pillow is already pulled in
  transitively (cadquery → trimesh chain), plotly is a top-level dep,
  Jinja2 is a transitive of FastAPI. Verified at slice-1 implementation; if
  any is missing it goes into `pyproject.toml` with the standard licence
  comment.
- **Report size envelope, by construction**: ≤ ~5 MB Plotly JS once + ≤ 25 MB
  per video + N × ≤ 256 KB compressed images + descriptor-bounded textual
  content. A pathological 10-video / 50-plot job is still bounded; a typical
  3-plot / 5-image job will land under 6 MB.
- **Permissions audit** writes one `job.report.generate` audit row per
  download, with `actor=user:{id}`, `resource_type=job`,
  `resource_id={job_id}`, `outcome=success` (or `failure` on a generation
  error — the endpoint returns 500 in that case rather than partial HTML).
- **Branding edits** propagate live: a deployment that changes its accent
  colour or logo on the next-saved branding pulls through to every report
  generated after the cache refresh, no rebuild.
- **No external CDN references** — a self-containedness regression test
  asserts the output contains no `http://`, `https://`, `<link rel`, or
  `<script src=` occurrences pointing outside the document.
- **Out-of-scope for first ship** (deliberate, may revisit):
    - Failed-job reports. Today the button hides for non-Complete jobs.
    - Job log (stdout / stderr) inclusion. The log lives in MATOFX; pulling
      it into a shareable artefact would mean reasoning about log redaction
      for customer-sensitive output.
    - Static-PNG plot variant. Interactive Plotly was chosen during
      discovery; if the JS-bundle weight becomes a problem in field use,
      a `?static=1` query param producing Kaleido PNGs is the planned
      escape hatch.
    - Job-log-style stdout in the metadata card.
    - Sibling-job outputs in the workflow context block.

## Revision history

- **2026-05-20 — created, Accepted.** Discovery-conversation choices:
  interactive Plotly, video cap 25 MB, CSV truncation at 100 rows, workflow
  context includes sibling inputs (not outputs), Complete-only eligibility,
  user-locale timestamps, owner+team+admin permission gate.
