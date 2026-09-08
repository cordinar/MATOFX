# Security policy

MATOFX SPDM is shipped by **Funis Consulting Ltd** and runs inside the trust boundary of every customer install (single-tenant per customer per §A1 of the project brief). Vulnerabilities matter.

## Reporting a vulnerability

**Please do not open public GitHub issues for security vulnerabilities.**

To report a suspected vulnerability:

- Email **security@funisoft.com** with the subject line beginning ``[MATOFX security]``.
- Encrypt your report with our PGP key (key id and fingerprint published at <https://funisoft.com/security> once available; until then, plain TLS-protected email is acceptable).
- Include enough detail for us to reproduce the issue: affected version, environment, reproduction steps, and the impact you've observed.

You can expect:

| Stage | Target SLA |
|---|---|
| Initial acknowledgement | within 2 working days |
| Triage outcome (severity + plan) | within 5 working days |
| Fix or workaround for **Critical** issues | within 14 days |
| Fix or workaround for **High** issues | within 30 days |
| Coordinated disclosure window | 90 days from initial report (negotiable) |

## Scope

In scope:

- All code shipped from this repository under ``.
- The MATOFX command-line interface (`matofx`).
- The descriptor schema and tool execution model.
- Third-party dependencies pinned in `pyproject.toml`, where the issue manifests through MATOFX's use of them.

Out of scope:

- Reports against the legacy code under `legacy_code/` (kept as read-only reference; see `CLAUDE.md`).
- Vulnerabilities that require physical access to the host or already-compromised admin credentials.
- Denial-of-service via resource exhaustion when the operator has not configured the documented resource limits.
- Issues in customer-supplied tool bundles. Tool authors are responsible for the security of the code inside their bundles; MATOFX is responsible for the sandbox around them.

## Supported versions

Until v1.0 ships, every commit on `main` is the supported version. After v1.0:

- The current `release/x.y` line receives security fixes.
- The previous `release/x.y` line receives security fixes for 6 months from the release of `x.(y+1)`.
- Earlier lines receive fixes only for **Critical** issues, on a best-effort basis.

## Cryptographic posture

- Passwords: Argon2id (argon2-cffi).
- Audit log: SHA-256 hash chain with Ed25519 signatures (cryptography).
- TLS: terminated by the operator's reverse proxy or the customer-managed installer (TLS material is not generated or stored by the application itself in v1.0).
- Random tokens: `secrets.token_urlsafe`.
- Customer secrets in configuration: not encrypted at rest in the application's own data; expected to be supplied via environment variables in production (see the install guide once published).

A FIPS-mode build path (cryptography backed by an OpenSSL FIPS module) is on the v1.0 hardening list.

## Supply-chain integrity

The v1.0 hardening list (`ROADMAP.md`) tracks SBOM (CycloneDX), SLSA L2 release signing (cosign), reproducible builds, lockfile-with-hashes, CodeQL SAST, Gitleaks secret scanning, Dependabot, and SHA-pinned GitHub Actions. SHA-pinning is already in place; the rest land before pilot.

## Recognition

Security researchers who report valid issues following this policy are credited (with consent) in the relevant release notes. Funis is willing to provide a recognition letter on company letterhead on request.
