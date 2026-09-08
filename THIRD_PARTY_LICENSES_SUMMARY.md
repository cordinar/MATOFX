# MATOFX SPDM™ — Third-Party Licence Summary

**Build:** 0.0.8+local20260724.2  **Components:** 136  **Source:** CycloneDX SBOM (`sbom.json`)  
**Full licence texts:** see `THIRD_PARTY_NOTICES.txt`. Draft — regenerate at the release tag.

> MATOFX SPDM™ itself (Funis Consulting Ltd) is proprietary and is **not** covered here. This file credits the third-party open-source components distributed with the platform, as their licences require. Each simulation tool installed into MATOFX runs under its own environment; a tool's own dependencies are attributed separately.

## What this means in plain English

MATOFX is built partly on free, open-source building blocks written by others. **128 of the 136** components are under the most relaxed kind of open-source licence (MIT, BSD, Apache and similar): they can be used in a commercial product freely, with no fee and no restriction — the only obligation is to credit them, which this file does. Your own code stays private.

The remaining **8** components carry somewhat stricter terms and are listed below for review. In every case the component is used **as-is, as a separately-installed library** — MATOFX does not copy their code into its own, so your proprietary code is unaffected. These terms are still compatible with selling MATOFX as a commercial, closed-source product; they mainly require that the components stay credited and replaceable. Note that a small number of highly regulated buyers (e.g. some defence/aerospace procurement) restrict the *library-type* (LGPL) licences on principle, so it is worth being able to point to this list and, if needed, confirm the position with legal counsel.

> **Not legal advice.** This note is a plain-English summary to aid review, not a legal opinion. Confirm the position with qualified counsel before relying on it with a regulated customer.

### Components under stricter terms

| # | Component | Version | Licence | What it means |
|---|---|---|---|---|
| 15 | asyncssh | 2.24.0 | EPL-2.0 OR GPL-2.0-or-later | Dual-licensed (Eclipse Public Licence or GPL). MATOFX relies on the Eclipse option — a mild open-source licence that is fine to distribute inside a commercial product. |
| 18 | bidict | 0.23.1 | MPL-2.0 | Mild, file-level open-source licence (Mozilla). Fine to distribute inside a commercial product; only direct edits to this component's own files would ever need sharing. |
| 20 | cadquery-ocp | 7.9.3.1.1 | UNKNOWN | Packaging metadata declares no licence. This is the OpenCASCADE CAD toolkit, distributed under the LGPL-2.1 with the OpenCASCADE exception — a *library* open-source licence. Usable within a commercial product as a separately-installed, unmodified library. Confirm and record the exact text at release. Used by the CAD-file preview. |
| 22 | casadi | 3.7.2 | LGPL-3.0-or-later | *Library* open-source licence (LGPL). Usable within a commercial product provided the component is used unmodified as a separately-installed library and the customer can replace it with their own version. MATOFX uses it unmodified and does not incorporate its source; your own code stays private. Used by the optimiser (evolutionary/optimisation engine). |
| 23 | certifi | 2026.7.22 | MPL-2.0 | Mild, file-level open-source licence (Mozilla). Fine to distribute inside a commercial product; only direct edits to this component's own files would ever need sharing. |
| 35 | docutils | 0.23 | Public Domain OR BSD-3-Clause OR GNU General Public License (GPL) | Offered under a choice of licences; MATOFX relies on the fully permissive option (no obligations beyond attribution). |
| 62 | moocore | 0.3.2 | LGPL-2.1-or-later | *Library* open-source licence (LGPL). Usable within a commercial product provided the component is used unmodified as a separately-installed library and the customer can replace it with their own version. MATOFX uses it unmodified and does not incorporate its source; your own code stays private. Used by the optimiser (multi-objective engine). |
| 72 | orjson | 3.11.9 | MPL-2.0 AND (Apache-2.0 OR MIT) | Mild, file-level open-source licence (Mozilla). Fine to distribute inside a commercial product; only direct edits to this component's own files would ever need sharing. |

### Full component list

Components marked **†** are the stricter-terms ones explained above.

| # | Component | Version | Declared licence |
|---|---|---|---|
| 1 | [about-time](https://github.com/rsalmei/about-time) | 4.2.1 | MIT |
| 2 | [aiofiles](https://github.com/Tinche/aiofiles) | 25.1.0 | Apache-2.0 |
| 3 | [aiohappyeyeballs](https://github.com/aio-libs/aiohappyeyeballs) | 2.7.1 | PSF-2.0 |
| 4 | [aiohttp](https://github.com/aio-libs/aiohttp) | 3.14.3 | Apache-2.0 |
| 5 | [aiosignal](https://github.com/aio-libs/aiosignal) | 1.4.0 | Apache-2.0 |
| 6 | aiosmtplib | 5.1.2 | MIT |
| 7 | aiosqlite | 0.22.1 | MIT |
| 8 | [alembic](https://alembic.sqlalchemy.org) | 1.18.5 | MIT |
| 9 | [alive-progress](https://github.com/rsalmei/alive-progress) | 3.3.0 | MIT |
| 10 | [annotated-doc](https://github.com/fastapi/annotated-doc) | 0.0.4 | MIT |
| 11 | [annotated-types](https://github.com/annotated-types/annotated-types) | 0.8.0 | MIT |
| 12 | [anyio](https://github.com/agronholm/anyio) | 4.14.2 | MIT |
| 13 | argon2-cffi | 25.1.0 | MIT |
| 14 | argon2-cffi-bindings | 25.1.0 | MIT |
| 15 | [asyncssh](http://asyncssh.timeheart.net) | 2.24.0 | EPL-2.0 OR GPL-2.0-or-later † |
| 16 | attrs | 26.1.0 | MIT |
| 17 | [autograd](https://github.com/HIPS/autograd) | 1.9.1 | MIT |
| 18 | [bidict](https://github.com/jab/bidict) | 0.23.1 | MPL-2.0 † |
| 19 | [cadquery](https://github.com/CadQuery/cadquery) | 2.8.0 | Apache-2.0 |
| 20 | cadquery-ocp | 7.9.3.1.1 | UNKNOWN † |
| 21 | cadquery-ocp-proxy | 7.9.3.1.1 | NOT INSTALLED |
| 22 | [casadi](http://casadi.org) | 3.7.2 | LGPL-3.0-or-later † |
| 23 | [certifi](https://github.com/certifi/python-certifi) | 2026.7.22 | MPL-2.0 † |
| 24 | [cffi](https://github.com/python-cffi/cffi) | 2.1.0 | MIT |
| 25 | charset-normalizer | 3.4.9 | MIT |
| 26 | [click](https://github.com/pallets/click/) | 8.4.2 | BSD-3-Clause |
| 27 | [cma](https://github.com/CMA-ES/pycma) | 4.4.4 | BSD-3-Clause |
| 28 | [colorama](https://github.com/tartley/colorama) | 0.4.6 | BSD-3-Clause |
| 29 | [contourpy](https://github.com/contourpy/contourpy) | 1.3.3 | BSD-3-Clause |
| 30 | [cryptography](https://github.com/pyca/cryptography) | 49.0.0 | Apache-2.0 OR BSD-3-Clause |
| 31 | [cycler](https://matplotlib.org/cycler/) | 0.12.1 | BSD-3-Clause |
| 32 | [cyclopts](https://github.com/BrianPugh/cyclopts) | 4.22.1 | Apache-2.0 |
| 33 | [deprecated](https://github.com/laurent-laporte-pro/deprecated) | 1.3.1 | MIT |
| 34 | [docstring-parser](https://github.com/rr-/docstring_parser) | 0.18.0 | MIT |
| 35 | [docutils](https://docutils.sourceforge.io) | 0.23 | Public Domain OR BSD-3-Clause OR GNU General Public License (GPL) † |
| 36 | [ezdxf](https://github.com/mozman/ezdxf) | 1.4.4 | MIT |
| 37 | [fastapi](https://github.com/fastapi/fastapi) | 0.139.2 | MIT |
| 38 | [fonttools](http://github.com/fonttools/fonttools) | 4.63.0 | MIT |
| 39 | [frozenlist](https://github.com/aio-libs/frozenlist) | 1.8.0 | Apache-2.0 |
| 40 | [graphemeu](https://github.com/timendum/grapheme) | 0.7.2 | Copyright (c) 2017 The Python Packaging Authority (PyPA) |
| 41 | [greenlet](https://greenlet.readthedocs.io) | 3.5.4 | MIT AND PSF-2.0 |
| 42 | [h11](https://github.com/python-hyper/h11) | 0.16.0 | MIT |
| 43 | [httpcore](https://www.encode.io/httpcore/) | 1.0.9 | BSD-3-Clause |
| 44 | [httptools](https://github.com/MagicStack/httptools) | 0.8.0 | MIT |
| 45 | [httpx](https://github.com/encode/httpx) | 0.28.1 | BSD-3-Clause |
| 46 | [idna](https://github.com/kjd/idna) | 3.18 | BSD-3-Clause |
| 47 | [ifaddr](https://github.com/pydron/ifaddr) | 0.2.0 | MIT |
| 48 | [itsdangerous](https://github.com/pallets/itsdangerous/) | 2.2.0 | BSD-3-Clause |
| 49 | [jinja2](https://github.com/pallets/jinja/) | 3.1.6 | BSD-3-Clause |
| 50 | [joblib](https://joblib.readthedocs.io) | 1.5.3 | BSD-3-Clause |
| 51 | [kiwisolver](https://github.com/nucleic/kiwi) | 1.5.0 | BSD-3-Clause |
| 52 | llvmlite | 0.48.0 | NOT INSTALLED |
| 53 | [lxml](https://lxml.de/) | 6.1.1 | BSD-3-Clause |
| 54 | [lxml-html-clean](https://github.com/fedora-python/lxml_html_clean/) | 0.4.5 | BSD-3-Clause |
| 55 | [mako](https://www.makotemplates.org/) | 1.3.12 | MIT |
| 56 | [markdown-it-py](https://github.com/executablebooks/markdown-it-py) | 4.2.0 | MIT |
| 57 | [markdown2](https://github.com/trentm/python-markdown2) | 2.5.5 | MIT |
| 58 | [markupsafe](https://github.com/pallets/markupsafe/) | 3.0.3 | BSD-3-Clause |
| 59 | [matplotlib](https://matplotlib.org) | 3.11.1 | PSF-2.0 |
| 60 | [mdurl](https://github.com/executablebooks/mdurl) | 0.1.2 | MIT |
| 61 | [meshio](https://github.com/nschloe/meshio) | 5.3.5 | MIT |
| 62 | [moocore](https://multi-objective.github.io/moocore/python/) | 0.3.2 | LGPL-2.1-or-later † |
| 63 | [more-itertools](https://github.com/more-itertools/more-itertools) | 11.1.0 | MIT |
| 64 | [msgpack](https://msgpack.org/) | 1.2.1 | Apache-2.0 |
| 65 | [multidict](https://github.com/aio-libs/multidict) | 6.7.1 | Apache-2.0 |
| 66 | [multimethod](https://github.com/coady/multimethod) | 1.12 | Apache-2.0 |
| 67 | [narwhals](https://github.com/narwhals-dev/narwhals) | 2.24.0 | MIT |
| 68 | [nicegui](https://nicegui.io) | 3.15.0 | MIT |
| 69 | nlopt | 2.11.0 | MIT |
| 70 | numba | 0.66.0 | NOT INSTALLED |
| 71 | [numpy](https://numpy.org) | 2.4.6 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| 72 | [orjson](https://github.com/ijl/orjson) | 3.11.9 | MPL-2.0 AND (Apache-2.0 OR MIT) † |
| 73 | [packaging](https://github.com/pypa/packaging) | 26.2 | Apache-2.0 OR BSD-2-Clause |
| 74 | [pandas](https://pandas.pydata.org) | 3.0.5 | BSD-3-Clause |
| 75 | [patsy](https://github.com/pydata/patsy) | 1.0.2 | BSD-3-Clause |
| 76 | [pillow](https://python-pillow.github.io) | 12.3.0 | MIT-CMU |
| 77 | [platformdirs](https://github.com/tox-dev/platformdirs) | 4.11.0 | MIT |
| 78 | [plotly](https://plotly.com/python/) | 6.9.0 | MIT |
| 79 | [pooch](https://github.com/fatiando/pooch) | 1.9.0 | BSD-3-Clause |
| 80 | [propcache](https://github.com/aio-libs/propcache) | 0.5.2 | Apache-2.0 |
| 81 | [psutil](https://github.com/giampaolo/psutil) | 7.2.2 | BSD-3-Clause |
| 82 | [pyarrow](https://arrow.apache.org/) | 25.0.0 | Apache-2.0 |
| 83 | [pycparser](https://github.com/eliben/pycparser) | 3.0 | BSD-3-Clause |
| 84 | [pydantic](https://github.com/pydantic/pydantic) | 2.13.4 | MIT |
| 85 | [pydantic-core](https://github.com/pydantic/pydantic) | 2.46.4 | MIT |
| 86 | [pydoe3](https://github.com/relf/pyDOE3) | 1.6.2 | BSD-3-Clause |
| 87 | [pygments](https://pygments.org) | 2.20.0 | BSD-2-Clause |
| 88 | [pymoo](https://pymoo.org) | 0.6.2 | Apache-2.0 |
| 89 | [pyparsing](https://github.com/pyparsing/pyparsing/) | 3.3.2 | MIT |
| 90 | [pyspnego](https://github.com/jborean93/pyspnego) | 0.12.1 | MIT |
| 91 | [python-dateutil](https://github.com/dateutil/dateutil) | 2.9.0.post0 | BSD-3-Clause OR Apache-2.0 |
| 92 | [python-dotenv](https://github.com/theskumar/python-dotenv) | 1.2.2 | BSD-3-Clause |
| 93 | [python-engineio](https://github.com/miguelgrinberg/python-engineio) | 4.13.3 | MIT |
| 94 | [python-multipart](https://github.com/Kludex/python-multipart) | 0.0.32 | Apache-2.0 |
| 95 | [python-socketio](https://github.com/miguelgrinberg/python-socketio) | 5.16.3 | MIT |
| 96 | [pyvista](https://github.com/pyvista/pyvista) | 0.48.4 | MIT |
| 97 | [pywinrm](http://github.com/diyan/pywinrm/) | 0.5.0 | MIT |
| 98 | [pyyaml](https://pyyaml.org/) | 6.0.3 | MIT |
| 99 | [requests](https://github.com/psf/requests) | 2.34.2 | Apache-2.0 |
| 100 | [requests-ntlm](https://github.com/requests/requests-ntlm) | 1.3.0 | ISC |
| 101 | [rich](https://github.com/Textualize/rich) | 15.0.0 | MIT |
| 102 | [rich-rst](https://github.com/wasi-master/rich-rst) | 2.1.0 | MIT |
| 103 | [runtype](https://github.com/erezsh/runtype) | 0.5.3 | MIT |
| 104 | [scikit-learn](https://scikit-learn.org) | 1.9.0 | BSD-3-Clause |
| 105 | [scipy](https://scipy.org/) | 1.18.0 | BSD-3-Clause |
| 106 | [scooby](https://github.com/banesullivan/scooby) | 0.11.2 | MIT |
| 107 | [simple-websocket](https://github.com/miguelgrinberg/simple-websocket) | 1.1.0 | MIT |
| 108 | [six](https://github.com/benjaminp/six) | 1.17.0 | MIT |
| 109 | [sqlalchemy](https://www.sqlalchemy.org) | 2.0.51 | MIT |
| 110 | [sspilib](https://github.com/jborean93/sspilib) | 0.5.0 | MIT |
| 111 | [starlette](https://github.com/Kludex/starlette) | 1.3.1 | BSD-3-Clause |
| 112 | [statsmodels](https://www.statsmodels.org/) | 0.14.6 | BSD-3-Clause |
| 113 | [threadpoolctl](https://github.com/joblib/threadpoolctl) | 3.6.0 | BSD-3-Clause |
| 114 | [tinycss2](https://www.courtbouillon.org/tinycss2) | 1.5.1 | BSD-3-Clause |
| 115 | trame | 3.13.2 | Apache-2.0 |
| 116 | trame-client | 3.13.2 | MIT |
| 117 | trame-common | 1.2.4 | Apache-2.0 |
| 118 | trame-components | 2.5.0 | Apache-2.0 |
| 119 | trame-server | 3.12.5 | Apache-2.0 |
| 120 | trame-vtk | 2.11.13 | BSD-3-Clause |
| 121 | trame-vuetify | 3.2.5 | MIT |
| 122 | [trimesh](https://github.com/mikedh/trimesh) | 4.12.2 | MIT |
| 123 | [typing-extensions](https://github.com/python/typing_extensions) | 4.16.0 | PSF-2.0 |
| 124 | [typing-inspection](https://github.com/pydantic/typing-inspection) | 0.4.2 | MIT |
| 125 | [tzdata](https://github.com/python/tzdata) | 2026.3 | Apache-2.0 |
| 126 | urllib3 | 2.7.0 | MIT |
| 127 | [uvicorn](https://uvicorn.dev/) | 0.51.0 | BSD-3-Clause |
| 128 | [vtk](https://vtk.org) | 9.6.2 | BSD-3-Clause |
| 129 | [watchfiles](https://github.com/samuelcolvin/watchfiles) | 1.2.0 | MIT |
| 130 | [webencodings](https://github.com/SimonSapin/python-webencodings) | 0.5.1 | BSD-3-Clause |
| 131 | [websockets](https://github.com/python-websockets/websockets) | 16.1.1 | BSD-3-Clause |
| 132 | [wrapt](https://github.com/GrahamDumpleton/wrapt) | 2.2.2 | BSD-2-Clause |
| 133 | wslink | 2.5.7 | BSD-3-Clause |
| 134 | [wsproto](https://github.com/python-hyper/wsproto/) | 1.3.2 | MIT |
| 135 | [xmltodict](https://github.com/martinblech/xmltodict) | 1.0.4 | MIT |
| 136 | [yarl](https://github.com/aio-libs/yarl) | 1.24.5 | Apache-2.0 |
