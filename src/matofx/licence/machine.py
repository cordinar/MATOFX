"""Soft machine binding (brief §B11, ADR-0019 §A3).

Binding is **optional and issuer-chosen**: a manifest either declares a
``machine_binding`` or it doesn't. When it does, this module supplies the
observed value to compare against.

The identifier is a stable OS-level machine id — ``/etc/machine-id`` on
Linux, ``MachineGuid`` on Windows — hashed, never stored raw. Both
survive reboots and don't depend on MAC or disk serials, which change on
cloud VMs. What they do *not* survive is a container rebuild, which is
exactly why binding is optional and why install **identity** is a
DB-persisted ``install_id`` instead (ADR-0019 §A3): identity is for
"which install is this?", binding is for "has this licence moved host?".

Deliberately not used for anything but comparison. It is not an identity,
not a key input (that was brief §B11's derived-key scheme, superseded by
ADR-0019 §A1), and not a fallback when the licence has no binding.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess  # reading a registry value on Windows
from pathlib import Path

_LINUX_MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))
_WINDOWS_CRYPTO_KEY = r"HKLM\SOFTWARE\Microsoft\Cryptography"
_REG_TIMEOUT_SECONDS = 5


def machine_fingerprint() -> str | None:
    """The hashed machine id for this host, or ``None`` when unavailable.

    ``None`` is a real answer, not an error: minimal containers often
    ship without a machine id. The caller decides what that means — and
    :func:`matofx.licence.loader.load_licence_file` treats it as a
    mismatch when the manifest declares a binding, because "we cannot
    tell which host this is" must not silently satisfy "this licence is
    for host X".
    """
    raw = _raw_machine_id()
    if not raw:
        return None
    return hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()


def _raw_machine_id() -> str | None:
    if platform.system() == "Windows":
        return _windows_machine_guid()
    return _linux_machine_id()


def _linux_machine_id() -> str | None:
    for path in _LINUX_MACHINE_ID_PATHS:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return None


def _windows_machine_guid() -> str | None:
    """Read ``MachineGuid`` via ``reg query`` (no winreg import on POSIX)."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["reg", "query", _WINDOWS_CRYPTO_KEY, "/v", "MachineGuid"],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=_REG_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        if "MachineGuid" in line:
            parts = line.split()
            if parts:
                return parts[-1]
    return None


_CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))


def looks_containerised() -> bool:
    """Best-effort "is this a container?", for a warning and nothing else.

    Machine binding compares an OS-level machine id, and a container
    rebuild produces a new one — so a machine-bound licence on a
    containerised install works right up until the first redeploy, then
    refuses to start. That is a miserable thing to discover during a
    deployment, and the guidance to avoid it previously lived only in
    ADR-0019 where no administrator would ever see it.

    Deliberately advisory. This never gates anything: a false positive
    that refused a valid licence would be far worse than a warning
    somebody did not need, and container detection is a heuristic in
    every implementation that exists.
    """
    if any(marker.exists() for marker in _CONTAINER_MARKERS):
        return True
    cgroup = Path("/proc/1/cgroup")
    try:
        content = cgroup.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(hint in content for hint in ("docker", "kubepods", "containerd", "lxc"))
