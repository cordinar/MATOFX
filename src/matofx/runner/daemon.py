"""Top-level entry point for the ``matofx-runner`` process.

Wired into ``[project.scripts]`` in ``pyproject.toml`` as
``matofx-runner = matofx.runner.daemon:main``.

Responsibilities:

- Build the engine + session factory + audit writer from
  application config (the same path the API uses).
- Construct a :class:`RunnerSupervisor` and start it.
- Install signal handlers so SIGTERM / SIGINT request a graceful
  drain rather than ripping the loop down mid-job.
- Block until ``stop()`` returns.

The runner runs forever; the only legitimate exits are an explicit
shutdown signal or a fatal startup error.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from matofx.audit.writer import make_audit_writer
from matofx.config import load_settings
from matofx.db import make_engine_scope, make_session_factory
from matofx.notify import make_notifier
from matofx.obs import configure as configure_obs
from matofx.runner.registry import TargetRegistry
from matofx.runner.supervisor import RunnerSupervisor
from matofx.secrets import make_secrets_store
from matofx.tools.exec.dispatch import AosDispatchTarget, HpcDispatchTarget

_log = logging.getLogger(__name__)


def main() -> int:
    """Synchronous entry point installed under the ``matofx-runner`` console script."""
    try:
        asyncio.run(run_daemon())
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Logging may not be configured yet (load_settings could raise); fall
        # back to stderr so the operator sees the failure either way.
        _log.exception("matofx-runner failed to start")
        return 1
    return 0


async def run_daemon() -> None:
    """Build the supervisor, run forever, drain on signal."""
    settings = load_settings()
    # S2: route runner logs through the redacting JSON-Lines formatter so
    # AOS / HPC error paths can't leak credentials / hostnames in stderr.
    configure_obs(settings.log)
    # S1: load the signing key from settings; AuditSettings + the prod-env
    # validator already refuse unsigned audit chains in production.
    audit = make_audit_writer(settings.audit)
    # NoopNotifier when EmailSettings.enabled is False (Phase 1 default).
    notifier = make_notifier(settings.email, audit)
    # The secrets store resolves AOS/HPC host credentials at dispatch time;
    # ``memory`` for local-only installs, ``encrypted_file`` once a host with
    # a secret_ref is registered (see ``matofx secrets``).
    secrets_store = make_secrets_store(settings.secrets)
    async with make_engine_scope(settings.db) as engine:
        factory = make_session_factory(engine)
        # Register the AOS / HPC dispatchers alongside the default local
        # target so jobs whose tool targets a host / cluster actually run.
        # Each dispatcher routes by the host row's transport / scheduler.
        registry = TargetRegistry()
        registry.register(
            "aos", AosDispatchTarget(session_factory=factory, secrets_store=secrets_store)
        )
        registry.register(
            "hpc", HpcDispatchTarget(session_factory=factory, secrets_store=secrets_store)
        )
        supervisor = RunnerSupervisor(
            factory=factory,
            audit=audit,
            notifier=notifier,
            registry=registry,
            # ADR-0019 slice 3: the runner is the long-lived process, so
            # it owns the hourly licence re-check. Passing settings is
            # what arms that loop.
            app_settings=settings,
        )
        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)
        await supervisor.start()
        _log.info("matofx-runner started")
        try:
            await stop_event.wait()
        finally:
            _log.info("matofx-runner draining")
            await supervisor.stop()
            _log.info("matofx-runner stopped")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    """Wire SIGTERM / SIGINT to set the stop event so loops drain cleanly.

    On Windows ``add_signal_handler`` raises ``NotImplementedError``;
    we fall back to ``signal.signal`` which is best-effort but still
    delivers SIGINT (Ctrl+C) into the event loop.
    """
    loop = asyncio.get_running_loop()
    try:
        for sig_name in ("SIGTERM", "SIGINT"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            loop.add_signal_handler(sig, stop_event.set)
    except NotImplementedError:
        # Windows path: ``signal.signal`` only fires for the main
        # thread, but ``asyncio.run`` runs on it so this is fine.
        for sig_name in ("SIGTERM", "SIGINT", "SIGBREAK"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, lambda *_: stop_event.set())
            except (OSError, ValueError):
                continue


if __name__ == "__main__":
    sys.exit(main())
