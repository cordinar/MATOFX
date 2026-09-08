"""Loader: TOML on disk → env-var overrides → validated, frozen ``Settings``.

Environment-variable overrides use a double-underscore separator so that
nested sections can be addressed without ambiguity:

    MATOFX__ENVIRONMENT=prod
    MATOFX__LOG__LEVEL=DEBUG
    MATOFX__DB__URL=postgres://...
    MATOFX__AUDIT__REQUIRE_SIGNING=true

Booleans accept ``true``/``false``/``yes``/``no``/``on``/``off``/``1``/``0``
case-insensitively. Integers and floats are coerced by Pydantic. Anything
else passes through as a string and Pydantic validates it.
"""

from __future__ import annotations

import os
import tomllib
import warnings
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from matofx.config.models import Settings
from matofx.config.paths import candidate_paths

ENV_PREFIX = "MATOFX__"
ENV_DELIM = "__"

_BOOL_TRUE = frozenset({"1", "true", "yes", "on"})
_BOOL_FALSE = frozenset({"0", "false", "no", "off"})


class ConfigError(Exception):
    """Raised when configuration cannot be loaded or fails validation.

    The message is plain English (per the F30 "users are not modellers"
    rule) — it tells the operator what's wrong and what to do next.
    """


def load_settings(path: Path | None = None) -> Settings:
    """Load and validate the MATOFX configuration.

    Args:
        path: Explicit path to the TOML file. When ``None``, the loader
            tries (in order): ``$MATOFX_CONFIG``, the OS system path, the
            per-user fallback. If none exist, defaults are used — no file
            is required for a fresh dev install.

    Returns:
        A frozen :class:`Settings`.

    Raises:
        ConfigError: When the file is unreadable, not valid TOML, or the
            merged configuration fails validation.

    Filesystem-path fields (``audit.signing_key_path``,
    ``tools.bundles_dir``, ``tutorials.tutorials_dir``) carry built-in
    defaults that are *relative* (``./tools`` etc.). Resolved naively
    against the process's working directory those land in surprising
    places — on Windows under NSSM the API service's CWD is
    ``C:\\Windows\\System32``, so ``./tools`` becomes
    ``C:\\Windows\\System32\\tools`` which the service can't write to.
    To make the relative defaults DWIM (do-what-I-mean), we resolve
    any relative value against the **config file's own directory**
    after validation. An operator who set
    ``bundles_dir = "./tools"`` next to ``matofx.toml`` in
    ``C:/ProgramData/matofx/`` gets ``C:/ProgramData/matofx/tools``,
    not whatever happened to be the service CWD.

    Absolute paths in the config are left alone — the operator has
    spoken. When no config file is found at all (rare; dev / test),
    relative paths still resolve against the CWD as a last resort.
    """
    file_data, config_path = _load_file(path)
    env_data = _env_overrides()
    merged = _deep_merge(file_data, env_data)
    try:
        settings = Settings.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(exc)) from exc
    return _resolve_relative_paths(settings, config_path)


def _load_file(explicit: Path | None) -> tuple[dict[str, Any], Path | None]:
    """Return ``(file_data, source_path)`` — ``source_path`` is the file the
    data was loaded from, or ``None`` when no config file was found.

    The source path is later used as the anchor for resolving any
    relative path fields in the merged configuration.
    """
    if explicit is not None:
        return _read_toml(explicit), explicit

    for candidate in candidate_paths():
        if candidate.is_file():
            return _read_toml(candidate), candidate

    # No file — proceed with defaults; env-var overrides still apply.
    return {}, None


def _resolve_relative_paths(settings: Settings, config_path: Path | None) -> Settings:
    """Resolve relative path fields against the config file's directory.

    See :func:`load_settings` for the rationale. Returns a (possibly
    new) :class:`Settings` with absolute paths; if nothing needs
    rewriting the input is returned unchanged.
    """
    if config_path is None:
        return settings
    anchor = config_path.resolve().parent

    audit = settings.audit
    new_audit = audit
    if audit.signing_key_path is not None and not audit.signing_key_path.is_absolute():
        new_audit = audit.model_copy(
            update={"signing_key_path": (anchor / audit.signing_key_path).resolve()}
        )

    tools = settings.tools
    new_tools = tools
    if not tools.bundles_dir.is_absolute():
        new_tools = tools.model_copy(update={"bundles_dir": (anchor / tools.bundles_dir).resolve()})

    tutorials = settings.tutorials
    new_tutorials = tutorials
    if not tutorials.tutorials_dir.is_absolute():
        new_tutorials = tutorials.model_copy(
            update={"tutorials_dir": (anchor / tutorials.tutorials_dir).resolve()}
        )

    workflows = settings.workflows
    new_workflows = workflows
    if not workflows.icons_dir.is_absolute():
        new_workflows = workflows.model_copy(
            update={"icons_dir": (anchor / workflows.icons_dir).resolve()}
        )

    models = settings.models
    new_models = models
    if not models.models_dir.is_absolute():
        new_models = models.model_copy(
            update={"models_dir": (anchor / models.models_dir).resolve()}
        )

    secrets = settings.secrets
    new_secrets = secrets
    if secrets.file_path is not None and not secrets.file_path.is_absolute():
        new_secrets = secrets.model_copy(
            update={"file_path": (anchor / secrets.file_path).resolve()}
        )

    if (
        new_audit is audit
        and new_tools is tools
        and new_tutorials is tutorials
        and new_workflows is workflows
        and new_models is models
        and new_secrets is secrets
    ):
        return settings
    return settings.model_copy(
        update={
            "audit": new_audit,
            "tools": new_tools,
            "tutorials": new_tutorials,
            "workflows": new_workflows,
            "models": new_models,
            "secrets": new_secrets,
        }
    )


def _read_toml(path: Path) -> dict[str, Any]:
    _maybe_warn_permissions(path)
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"Config file not found: {path}. "
            "Set MATOFX_CONFIG, place a matofx.toml at the system path, "
            "or pass --config explicitly."
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Config file is not valid TOML: {path}\n  {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read config file {path}: {exc}") from exc


_PERM_SUPPRESS_ENV = "MATOFX_SUPPRESS_PERM_WARNING"


def _maybe_warn_permissions(path: Path) -> None:
    """Warn (via :mod:`warnings`) if the config file is readable by other users.

    POSIX-only — Windows uses ACLs that don't map onto stat-mode bits;
    skip there rather than warn confusingly. The check is loose: any
    bits set in the group/other portion (``st_mode & 0o077``) trigger
    the warning. Operators set ``MATOFX_SUPPRESS_PERM_WARNING=1`` to
    silence (e.g. for shared dev machines).

    The warning is intentionally non-fatal — some environments
    legitimately rely on group-readable configs (containers with
    init-supplied configs, multi-user dev hosts). The point is to
    surface the issue early; the operator decides.
    """
    import os  # noqa: PLC0415 — kept local to the platform branch
    import sys  # noqa: PLC0415

    if sys.platform == "win32":
        return
    if os.environ.get(_PERM_SUPPRESS_ENV):
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & 0o077:
        warnings.warn(
            (
                f"Config file {path} is readable by other users "
                f"(mode {oct(mode & 0o777)}). It may contain DB credentials "
                "and the audit signing-key path. Tighten with `chmod 600` "
                f"or set {_PERM_SUPPRESS_ENV}=1 to silence this warning."
            ),
            stacklevel=3,
        )


def _env_overrides() -> dict[str, Any]:
    """Build a nested dict from ``MATOFX__SECTION__KEY=value`` env vars."""
    out: dict[str, Any] = {}
    for raw_key, raw_value in os.environ.items():
        if not raw_key.startswith(ENV_PREFIX):
            continue
        path = raw_key[len(ENV_PREFIX) :].split(ENV_DELIM)
        if not path or any(not part for part in path):
            continue
        cursor = out
        for part in path[:-1]:
            existing = cursor.get(part.lower())
            if not isinstance(existing, dict):
                existing = {}
                cursor[part.lower()] = existing
            cursor = existing
        cursor[path[-1].lower()] = _coerce_scalar(raw_value)
    return out


def _coerce_scalar(value: str) -> bool | int | float | str:
    lowered = value.strip().lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _deep_merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` into ``base``; overlay wins on conflict."""
    out: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        existing = out.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            out[key] = _deep_merge(existing, value)
        else:
            out[key] = value
    return out


def _format_validation_error(exc: ValidationError) -> str:
    """Render a Pydantic ValidationError without echoing the input value.

    Pydantic by default includes the user-supplied value in error
    messages (``input_value=...``), which is fine for most fields but
    risky when the value is a DB URL with an embedded password. We
    pass ``include_input=False`` to strip it; the location + message
    are enough for an operator to fix the issue.
    """
    lines = ["Configuration validation failed:"]
    for err in exc.errors(include_input=False):
        location = ".".join(str(p) for p in err["loc"])
        lines.append(f"  {location}: {err['msg']}")
    lines.append("Fix these in matofx.toml or override them via MATOFX__SECTION__KEY env vars.")
    return "\n".join(lines)
