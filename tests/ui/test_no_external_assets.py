"""Guard: the UI must not fetch anything from the public internet at runtime.

MATOFX is deployed on air-gapped networks. A ``<script src="https://…">``
there does not fail loudly — the page loads, and one feature quietly
renders blank. That is how the three CDN dependencies P-03 removed got in
without anyone noticing, and it is why this is a test rather than a
convention: the cost of the regression is paid months later, by a customer,
on a network nobody on this side can reach.

The rule enforced here is narrow and mechanical — **no external URL may
appear in a string literal that the UI could hand to a browser**. Comments
and docstrings are stripped before scanning, so prose stays free to name a
CDN while explaining why we no longer use one; only live code trips the
guard. It makes no attempt to parse HTML or trace data flow, so the
occasional false positive is expected and the fix is an allowlist entry
with a reason.

Vendored copies live under ``src/matofx/ui/assets/vendor/`` and are served
same-origin from ``/matofx-assets/vendor/…``. See that directory's
``README.md`` for versions, digests, and the re-fetch procedure.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

import matofx.ui
from matofx.ui.cad_preview import _MODEL_VIEWER_CONFIG_SCRIPT
from matofx.ui.static_assets import ASSETS_URL_PREFIX, assets_root, mount_static_assets

_UI_ROOT = Path(matofx.ui.__file__).resolve().parent

_URL_RE = re.compile(r"https?://[^\s\"'`)>\\]+")

_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        # Placeholder host in example text. Never fetched.
        "example.com",
        # XML/schema namespace identifiers. These are *identifiers* that
        # happen to look like URLs; nothing dereferences them.
        "www.w3.org",
        # api_client.py's in-process ASGI base URL. httpx requires *some*
        # base URL to build request targets against; the ASGITransport
        # then dispatches straight into the app object. No socket is
        # opened and no DNS lookup happens, so the host name is a
        # formality — deliberately one that could never resolve.
        "matofx",
    }
)

_EXEMPT_FILES: frozenset[str] = frozenset(
    {
        # The marketing/preview page deliberately embeds a remote iframe
        # and remote sample models. It is expected to look broken on an
        # air-gapped box and is out of scope per the design doc.
        "pages/demo.py",
    }
)


def _python_sources() -> list[Path]:
    return sorted(p for p in _UI_ROOT.rglob("*.py") if "__pycache__" not in p.parts)


def _relative(path: Path) -> str:
    return path.relative_to(_UI_ROOT).as_posix()


def _host_of(url: str) -> str:
    return url.split("//", 1)[1].split("/", 1)[0].split(":", 1)[0].lower()


def _live_string_literals(source: str) -> list[str]:
    """Every string constant in ``source`` except prose.

    Comments never reach the AST, so they drop out for free. Prose is
    every **bare string-expression statement** — which covers module,
    class and function docstrings *and* the attribute docstrings this
    codebase uses heavily (a ``\"\"\"…\"\"\"`` block directly beneath an
    assignment). A bare string statement is never used as a value, so
    excluding the whole category is exact rather than approximate.

    The distinction matters: a guard that can't tell prose from code
    would force you to mangle the comment explaining why a CDN was
    dropped in order to satisfy a rule about not using one.
    """
    tree = ast.parse(source)
    prose = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in prose
    ]


def test_ui_sources_contain_no_external_urls() -> None:
    """No ``http(s)://`` reference in live UI code outside the allowlist."""
    offenders: list[str] = []
    for path in _python_sources():
        rel = _relative(path)
        if rel in _EXEMPT_FILES:
            continue
        for literal in _live_string_literals(path.read_text(encoding="utf-8")):
            for url in _URL_RE.findall(literal):
                if _host_of(url) in _ALLOWED_HOSTS:
                    continue
                offenders.append(f"{rel}: {url}")

    assert not offenders, (
        "External URL(s) found in the UI source. On an air-gapped install these "
        "never resolve and the feature renders blank rather than failing loudly. "
        "Vendor the asset under src/matofx/ui/assets/vendor/ and serve it from "
        "/matofx-assets/vendor/, or add a documented entry to _ALLOWED_HOSTS / "
        "_EXEMPT_FILES.\n  " + "\n  ".join(offenders)
    )


def test_the_guard_is_not_vacuous() -> None:
    """The scanner sees live literals and ignores prose.

    A guard whose whole output is "nothing found" is indistinguishable
    from a guard that looks at nothing — and this one does non-trivial
    AST work to separate code from comment, so the failure mode is real.
    Two probes: a URL in a live string must be seen, and the same URL in
    an attribute docstring must not.
    """
    live_only = '\nx = "https://cdn.invalid/lib.js"\n'
    assert _URL_RE.findall(" ".join(_live_string_literals(live_only)))

    prose_only = '\nx = 1\n"""Explains why https://cdn.invalid/lib.js was dropped."""\n'
    assert not _URL_RE.findall(" ".join(_live_string_literals(prose_only)))


def test_exempt_file_would_otherwise_trip_the_guard() -> None:
    """``demo.py`` is exempt because it *needs* to be, not by habit.

    If it ever stops carrying an external URL, the exemption is dead
    weight hiding future regressions in that file, and should go.
    """
    source = (_UI_ROOT / "pages" / "demo.py").read_text(encoding="utf-8")
    found = [
        url
        for literal in _live_string_literals(source)
        for url in _URL_RE.findall(literal)
        if _host_of(url) not in _ALLOWED_HOSTS
    ]
    assert found, "pages/demo.py no longer needs its exemption — drop it from _EXEMPT_FILES."


def test_exempt_and_allowed_entries_are_still_needed() -> None:
    """Every exemption still corresponds to a real file.

    An allowlist nobody prunes stops describing the system. This catches
    the cheap half — a path that no longer exists — so a stale entry
    can't quietly widen the guard.
    """
    for rel in _EXEMPT_FILES:
        assert (_UI_ROOT / rel).is_file(), f"stale _EXEMPT_FILES entry: {rel}"


@pytest.mark.parametrize(
    "name",
    [
        "cytoscape.min.js",
        "dagre.min.js",
        "cytoscape-dagre.min.js",
        "model-viewer.min.js",
        "draco/draco_wasm_wrapper.js",
        "draco/draco_decoder.wasm",
        "basis/basis_transcoder.js",
        "basis/basis_transcoder.wasm",
    ],
)
def test_vendored_asset_is_present(name: str) -> None:
    """Each vendored file the UI references is actually in the package.

    Vendoring has no build step, so the failure mode is a missing or
    truncated file — invisible to every other test until a browser asks
    for it and gets a 404.
    """
    path = assets_root() / "vendor" / name
    assert path.is_file(), f"vendored asset missing: {path}"
    assert path.stat().st_size > 1024, f"vendored asset looks truncated: {path}"


@pytest.mark.asyncio
async def test_vendored_assets_are_actually_served() -> None:
    """The same-origin URLs resolve through the real mount.

    Checking the files exist on disk is not the same as checking the
    HTTP route reaches them — the mount is a single ``StaticFiles`` on
    the *outer* app, and a change to the prefix or to how the directory
    is resolved would leave every vendored URL 404ing with every
    file-presence test still green.

    The ``.wasm`` content type is asserted for a specific reason: the
    Draco and KTX2 decoders are instantiated by the browser as
    WebAssembly, and a wrong MIME breaks the streaming instantiation
    path even though the bytes are served correctly.
    """
    app = FastAPI()
    mount_static_assets(app)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mount") as client:
        for name in ("cytoscape.min.js", "model-viewer.min.js"):
            resp = await client.get(f"{ASSETS_URL_PREFIX}/vendor/{name}")
            assert resp.status_code == 200, name
            assert len(resp.content) > 1024, name

        wasm = await client.get(f"{ASSETS_URL_PREFIX}/vendor/draco/draco_decoder.wasm")
        assert wasm.status_code == 200
        assert wasm.headers["content-type"].startswith("application/wasm")


def test_model_viewer_decoders_are_pointed_at_the_vendored_copies() -> None:
    """model-viewer must not fall back to its gstatic decoder defaults.

    This is the trap that makes the glTF path look air-gap-clean when it
    isn't: model-viewer reads ``dracoDecoderLocation`` /
    ``ktx2TranscoderLocation`` at module-evaluation time, defaults both
    to ``https://www.gstatic.com/…``, and only *loads* them when a model
    happens to use Draco compression or KTX2 textures. Vendoring the
    viewer alone therefore passes every offline test until a user
    uploads a Draco-compressed ``.glb`` — which is what most exporters
    produce by default.
    """
    assert "dracoDecoderLocation" in _MODEL_VIEWER_CONFIG_SCRIPT
    assert "ktx2TranscoderLocation" in _MODEL_VIEWER_CONFIG_SCRIPT
    assert "/matofx-assets/vendor/draco/" in _MODEL_VIEWER_CONFIG_SCRIPT
    assert "/matofx-assets/vendor/basis/" in _MODEL_VIEWER_CONFIG_SCRIPT
    assert "gstatic" not in _MODEL_VIEWER_CONFIG_SCRIPT
