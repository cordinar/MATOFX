# Releasing MATOFX

This document is the maintainer's checklist for cutting a release. It will harden as v1.0 approaches; right now most items are placeholders to ensure the steps land before the pilot.

## Versioning

MATOFX follows **semver 2.0.0** with a project-specific reading:

- **Major** — backwards-incompatible change in *deployed-install behaviour*. Any change that requires customer action to upgrade safely (data migration outside the automatic Alembic chain, manual config rewrite, breaking the descriptor schema) is major.
- **Minor** — new features that are additive and backwards-compatible. New permissions, new feature-flag slugs, new optional config sections, new ORM tables, new descriptor fields with sensible defaults.
- **Patch** — bug fixes, security fixes, internal-only changes that are observable to no customer.

Pre-1.0 releases use `0.MINOR.PATCH`. Anything before v1.0 may break in any release.

## Release lines

After v1.0:

- `main` is the integration line.
- `release/x.y` branches are cut **lazily** when a customer needs a backport. See `CONTRIBUTING.md` (§ trunk-based + lazy maintenance branch).
- The **two most recent** `release/x.y` lines receive bug-fix releases. Earlier lines receive only **Critical** security fixes (see `SECURITY.md`).

## Release tag format

`v<major>.<minor>.<patch>`. Pre-release tags use `v<major>.<minor>.<patch>-<label>.<n>`, e.g. `v1.0.0-rc.1`.

## Pre-release checklist

1. All quality gates green: `ruff format --check`, `ruff check`, `mypy`, `pytest` on the matrix. CodeQL + Gitleaks runs on the release commit must also be clean.
2. **Lockfile current.** Trigger the **Regenerate lockfile** workflow (`.github/workflows/lockfile.yml`) on `main`, merge the PR if one opens. CI's `verify-lockfile` job must pass on the release commit.
3. Release notes drafted in `CHANGELOG.md` following [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format.
4. Permission catalog and feature-flag catalog reviewed: any new slugs added in this release are documented in the changelog under "Added".
5. Migration chain re-tested by upgrading from the previous release on both SQLite and Postgres.
6. The customer-facing docs (install / admin / tool-author / end-user) reflect every change in this release.
7. Pilot customer notified for any change that affects their install.
8. Bump the version in `pyproject.toml` to match the tag you are about to push.

(SBOM generation runs automatically as part of the release workflow — there is no manual SBOM step.)

## Build and sign

The release pipeline (`.github/workflows/release.yml`) fires on every `v<major>.<minor>.<patch>` tag push and produces the following artefacts on the GitHub Release page:

- The wheel (`matofx-<version>-py3-none-any.whl`).
- The source tarball (`matofx-<version>.tar.gz`).
- The CycloneDX SBOM (`sbom.cdx.json`) capturing every transitive dependency at build time.
- An SLSA build-provenance attestation covering the wheel, the sdist, and the SBOM. The attestation is generated via Sigstore's keyless OIDC flow (no long-lived signing key) and is published alongside the artefacts; reviewers can verify with `gh attestation verify <artefact> --owner funisoft`.

The maintainer never runs `python -m build` or `cyclonedx-py` by hand for a release — every signed artefact comes from the GitHub Actions runner so the SLSA provenance accurately attests "this came from this workflow run on this commit".

## Cutting the release

```bash
# From a clean main checkout:
git pull --ff-only origin main
git tag -s v<major>.<minor>.<patch> -m "MATOFX v<major>.<minor>.<patch>"
git push origin v<major>.<minor>.<patch>
# CI builds + signs + publishes artefacts to the release page.
```

Maintainer's PGP key for tag signing: see `SECURITY.md` for the published key id.

## Hotfix releases

For a `release/x.y` line:

```bash
git switch release/x.y
git cherry-pick -x <commit>           # backport the fix
git tag -s v<x>.<y>.<patch+1>
git push origin release/x.y v<x>.<y>.<patch+1>
```

Hotfixes follow the same checklist (SBOM, lockfile, changelog) but are typically smaller.

## Pilot vs general release

Per `PROJECT_BRIEF.md` §I46: a paid pilot with at least one real customer precedes the general v1.0 release. The pilot uses a normal time-limited MATOFX licence with reduced expiry — there is **no separate "trial mode" code path**. The pilot release is tagged `v1.0.0-pilot.<n>` and follows this same checklist; only after pilot success criteria are met (signed off before the pilot starts) does `v1.0.0` go general.
