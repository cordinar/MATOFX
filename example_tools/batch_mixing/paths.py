"""Host configuration + Windows<->WSL path helpers for the Batch Mixing tool.

Centralises the few machine-specific paths (FreeCAD, the WSL distro, the OpenFOAM
env) and the ``/mnt/c`` translation the orchestrator needs. All overridable via
env vars so the tool is portable to another box without code edits.
"""

from __future__ import annotations

import os
from pathlib import Path

# FreeCAD headless binary (Windows).
FREECADCMD = os.environ.get(
    "MATOFX_FREECADCMD", r"C:\Program Files\FreeCAD 1.0\bin\freecadcmd.exe"
)

# WSL distro + OpenFOAM environment + custom solver.
WSL_DISTRO = os.environ.get("MATOFX_WSL_DISTRO", "Ubuntu")
OF_BASHRC = os.environ.get("MATOFX_OF_BASHRC", "/usr/lib/openfoam/openfoam2606/etc/bashrc")
SOLVER = os.environ.get("MATOFX_SOLVER", "matofxMixingFoam")

# How many subdomains the parallel solve decomposes into.
N_PROCS = int(os.environ.get("MATOFX_N_PROCS", "8"))

# This tool's own directory (so the orchestrator finds geometry/ and openfoam/).
TOOL_DIR = Path(__file__).resolve().parent


def to_wsl(path: str | Path) -> str:
    """Translate a Windows path to its WSL ``/mnt/<drive>`` form.

    ``C:\\Users\\x`` -> ``/mnt/c/Users/x``. Already-POSIX paths pass through.
    """
    p = str(Path(path).resolve()).replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        return "/mnt/" + p[0].lower() + p[2:]
    return p
