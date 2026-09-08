# `matofx.cli` — admin CLI

The `matofx` command-line entry point used by Funis and customer admins to operate the install.

## Why this module exists

Per §I46 of the brief, v1.0 ships the following subcommands:

| Subcommand | Purpose |
|---|---|
| `matofx doctor` | Health check: config valid? DB reachable? Migrations up-to-date? Licence valid? |
| `matofx support bundle` | Generate a redacted diagnostics bundle (logs + config snapshot + version info) for Funis support. |
| `matofx backup` / `matofx restore` | restic-backed backup/restore (per §G36). |
| `matofx upgrade` | In-place upgrade with migration + rollback safety. |
| `matofx tool install` / `update` / `uninstall` | Manage installed tools (descriptor + static bundle). |
| `matofx licence install` / `reload` | Install or refresh the signed licence manifest. |
| `matofx tls install` / `status` / `renew-self-signed` | TLS certificate management. |
| `matofx rotate signing-key` | Rotate the audit signing key with chain continuity. |
| `matofx admin create-first-user` | First-user bootstrap (per §I46 install row). |
| `matofx tpm enrol` | TPM enrolment when `key_source = tpm`. |

## Design notes

- Built on Click (BSD-3) — permissively licensed, Python-native, no JS toolchain needed.
- Every subcommand emits structured JSON-Lines logs through `matofx.obs` and writes an audit record (where applicable) through `matofx.audit`.
- Every subcommand exits non-zero on any kind of failure; success is silent unless `--verbose`.
- Long-running subcommands (`backup`, `upgrade`) emit progress lines suitable for piping into a monitoring system.

## Where to start reading

- `main.py` — `main(argv)` is the entry point declared in `pyproject.toml`'s `[project.scripts]`.
- `commands/` — one module per subcommand once they're built.

## Phase 0 status

`main.py` is a stub that prints the version. Real subcommands land alongside the modules they operate on.
