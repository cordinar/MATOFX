# MATOFX v2 — Installation on a fresh machine

Step-by-step install for a clean target machine (Windows or Linux). The procedure has two halves:

- **Part A — Build the deployable bundle on the dev machine.** Produces a single folder you copy to the target. Wheel + hashed lockfile + SBOM + example config + example tools + migration helper + seed scripts, all in one place.
- **Part B — Install the bundle on the target machine.** Numbered steps from a freshly-imaged box to a running login screen.

Tested against the as-built state of `` (Alembic migrations through the current head, `0094_hpc_cluster_accounting`). The bundle is self-contained: no internet access is required on the target machine if you populate the offline wheelhouse described in Part A step 7.

**At runtime the installed site requires no internet at all.** Every piece of JavaScript the browser needs is served by the MATOFX host — the charting library, the Cytoscape provenance/workflow diagrams, and the `<model-viewer>` 3-D viewer with its Draco and KTX2 decoders all ship inside the wheel under `matofx/ui/assets/vendor/` and are served from `/matofx-assets/vendor/…`. Downloaded charts and comparison reports inline their own copy of plotly.js so they plot on a machine that has never seen this server. The only exclusions are features you enable yourself (email relay, remote execution, the optional LLM summariser) and the `/ui/demo` marketing page, which deliberately embeds remote content and is expected to look broken on an isolated network. A regression guard (`tests/ui/test_no_external_assets.py`) fails the build if a new external URL appears in the UI source.

One residual, recorded so it isn't discovered later: plotly.js carries a built-in default of `https://cdn.plot.ly/un/` for the *topojson* files used by geographic map traces (`scattergeo` / `choropleth`). MATOFX does not produce those trace types, so nothing fetches it — but the string is present in the inlined bundle, and a scanner looking for URLs rather than requests will flag it.

---

## Part A — Build the bundle (on the dev machine)

These steps run **once per release**, on the machine where the source tree lives. They produce a directory at `dist/matofx-bundle-<version>/` that contains everything the target machine needs.

### A1. Prerequisites on the dev machine

- Python 3.12 or 3.13 already installed and on PATH (`python --version` prints `3.12.x` or `3.13.x`).
- A checkout of this repository.
- A working virtualenv for the project (the build commands run inside it). If you don't already have one:

  ```powershell
  cd <your checkout>
  python -m venv .venv
  .\.venv\Scripts\Activate.ps1
  pip install -e ".[dev,supply-chain]"
  ```

  On Linux/macOS:

  ```bash
  cd <your checkout>
  python3 -m venv .venv
  source .venv/bin/activate
  pip install -e ".[dev,supply-chain]"
  ```

### A2. Run the bundle script

From the repository root:

```powershell
.\scripts\build_bundle.ps1
```

…or on Linux/macOS:

```bash
./scripts/build_bundle.sh
```

The script does, in order:

1. Validates the dev venv has `build`, `pip-tools`, and `cyclonedx-py` installed (from the `supply-chain` extras).
2. Calls `python -m build --wheel` against `` → produces `dist/matofx-<version>-py3-none-any.whl`.
3. Calls `pip-compile --generate-hashes --output-file requirements.lock pyproject.toml` → produces a fully pinned, SHA256-hashed dependency lockfile.
4. Calls `cyclonedx-py requirements --of JSON --output-file sbom.json requirements.lock` → produces a CycloneDX SBOM (software bill of materials) listing every dependency with its licence.
5. Stages the bundle directory `dist/matofx-bundle-<version>/` with this layout:

   ```
   matofx-bundle-<version>/
   ├── matofx-<version>-py3-none-any.whl     # the application wheel
   ├── requirements.lock                      # hashed, pinned deps (no internet needed at install time if you populate wheelhouse/)
   ├── sbom.json                              # CycloneDX SBOM (audit / compliance)
   ├── matofx.toml                            # production config template (copy of etc/matofx.example.toml)
   ├── example_tools/                         # three reference tools (material_properties / colour_check / heat_transfer)
   ├── branding/                              # default header_icon.* + favicon.* (optional; from etc/branding/)
   ├── scripts/
   │   ├── matofx_migrate.py                  # standalone alembic runner (resolves the installed package's migrations)
   │   ├── seed_example_tools.py              # installs the three example tools and a local StorageLocation
   │   ├── seed_default_branding.py           # copies branding/ files into icons_dir + seeds DB rows when empty
   │   ├── install_windows.ps1                # one-shot Windows installer (calls every step)
   │   └── install_linux.sh                   # one-shot Linux installer
   ├── INSTALL.md                             # this file
   ├── HARDWARE_SIZING.md                     # recommended CPU/RAM/GPU/storage + reasoning
   └── BUNDLE_INFO.txt                        # version, git SHA, build date, dev machine hostname
   ```
6. Prints the absolute path of the bundle directory and its total size.

If you want a single archive instead of a folder, pass `-Zip` (PowerShell) or `--zip` (bash):

```powershell
.\scripts\build_bundle.ps1 -Zip
```

…which additionally produces `dist/matofx-bundle-<version>.zip`.

### A3. Optional — populate an offline wheelhouse

If the target machine has no internet access, run this **on a machine with the same OS and Python version as the target** before stepping away:

```powershell
cd dist\matofx-bundle-<version>
mkdir wheelhouse
pip download --no-deps --dest wheelhouse --requirement requirements.lock
pip download --no-deps --dest wheelhouse matofx-<version>-py3-none-any.whl
```

The bundle is now fully self-contained — Part B step 6 below uses `--no-index --find-links wheelhouse/` so the target never touches PyPI.

### A4. Copy or transfer the bundle to the target machine

Any reliable transport: SFTP, SCP, USB drive, file share. The bundle path on the target is up to you — the install scripts auto-detect their own location, so `C:\matofx-install\matofx-bundle-0.1.0\` or `/opt/matofx-install/matofx-bundle-0.1.0/` both work.

---

## Part B — Install on the target machine

**Sizing the target machine:** see `HARDWARE_SIZING.md` (alongside this file) for recommended CPU/RAM/GPU/storage and the reasoning. Short version: 4 cores / 8 GB / 100 GB SSD for a trial; 8–16 cores / 32 GB / 500 GB+ SSD for production; no GPU needed by MATOFX itself — the machine only grows if tool jobs execute locally.

Numbered steps, every flag, every keystroke. The walk-through assumes you copied the bundle to `C:\matofx-install\matofx-bundle-0.1.0\` (Windows) or `/opt/matofx-install/matofx-bundle-0.1.0/` (Linux). Adjust paths to taste.

Throughout: **`<BUNDLE>` is the absolute path to the bundle directory**. `<APP_HOME>` is wherever you decide MATOFX itself will live on the target (the venv, the SQLite DB if you're using SQLite, the audit key, and the shared-mount for job working directories). The conventional locations are:

| Platform | `<APP_HOME>`              | Config file path                         |
|---|---|---|
| Linux    | `/opt/matofx`             | `/etc/matofx/matofx.toml`                |
| Windows  | `C:\ProgramData\matofx`   | `C:\ProgramData\matofx\matofx.toml`      |

### B1. Verify OS prerequisites

Run as the user who will own the install (not root / not Administrator unless you're deliberately doing a system-wide install).

**Linux (Debian / Ubuntu / RHEL):**

```bash
python3 --version    # must print 3.12.x or 3.13.x — if older, see B2
which python3
uname -a             # record kernel + arch for support
```

**Windows:**

```powershell
python --version     # must print 3.12.x or 3.13.x — if older, see B2
where.exe python
[System.Environment]::OSVersion
```

If you plan to use Postgres rather than SQLite, also verify `psql --version` is **16 or newer** and that you can reach the server: `psql -h <host> -U matofx -d matofx -c '\q'` should return cleanly.

### B2. Install Python 3.12+ if missing

**Linux (Ubuntu 22.04+):**

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3.12-dev
```

**Linux (RHEL 9 / Alma 9):**

```bash
sudo dnf install -y python3.12 python3.12-pip
```

**Windows:**
Download the 64-bit installer from <https://www.python.org/downloads/windows/>, run it, and tick **"Add python.exe to PATH"** on the first screen. Re-open PowerShell so PATH refreshes, then re-run B1.

### B3. Create the application home directory

**Linux:**

```bash
sudo mkdir -p /opt/matofx /etc/matofx /var/lib/matofx /var/log/matofx /srv/matofx/jobs
sudo chown -R "$USER:$USER" /opt/matofx /var/lib/matofx /var/log/matofx /srv/matofx
```

**Windows (PowerShell as a normal user — UAC will prompt if needed):**

```powershell
New-Item -ItemType Directory -Force `
  C:\ProgramData\matofx, `
  C:\ProgramData\matofx\db, `
  C:\ProgramData\matofx\logs, `
  C:\ProgramData\matofx\jobs `
| Out-Null
```

### B4. Create a dedicated virtualenv

**Linux:**

```bash
cd /opt/matofx
python3.12 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
```

**Windows:**

```powershell
cd C:\ProgramData\matofx
python -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

Verify: `python -c "import sys;print(sys.prefix)"` prints the venv directory.

### B5. Confirm pip hash-checking mode

This is the security-critical step. We install **only** from the hashed lockfile so no man-in-the-middle or PyPI typosquat can substitute a malicious version.

```powershell
pip config set global.require-hashes true     # PowerShell
```

```bash
pip config set global.require-hashes true     # Linux/macOS
```

Verify: `pip config list` shows `global.require-hashes='True'`.

### B6. Install MATOFX from the bundle

The lockfile contains both the third-party dependencies **and** the matofx wheel itself (the build script appends a `file:` entry with the wheel's SHA-256 hash). A single command installs everything under `--require-hashes`.

> Run this command from **inside** the bundle directory — the lockfile's `file:` reference is relative, so `cd <BUNDLE>` is required.

**Online install (target has internet):**

```powershell
cd <BUNDLE>
pip install --requirement requirements.lock
```

**Offline install (target has no internet — requires Part A step A3 was run):**

```powershell
cd <BUNDLE>
pip install --no-index --find-links wheelhouse --requirement requirements.lock
```

Verify three console scripts now exist:

```powershell
Get-Command matofx, matofx-api, matofx-runner       # Windows
which matofx matofx-api matofx-runner               # Linux
matofx --version
```

`matofx --version` should print the version recorded in `BUNDLE_INFO.txt` (e.g. `matofx 0.0.0`).

**If you see** `Hashes are required in --require-hashes mode, but they are missing from some requirements`:
- You're running an older bundle where the wheel hash wasn't appended to the lockfile. Either re-run the build script (newer versions auto-append), or apply this one-line patch to `requirements.lock` inside the bundle:

  ```powershell
  $wheel = (Get-ChildItem .\matofx-*.whl).Name
  $hash  = (Get-FileHash -Algorithm SHA256 $wheel).Hash.ToLower()
  Add-Content requirements.lock "`nmatofx @ file:./$wheel ``$([Environment]::NewLine)    --hash=sha256:$hash"
  ```

  Then re-run the `pip install --requirement requirements.lock` command above.

### B7. Place the configuration file

Copy the template that came with the bundle:

**Linux:**

```bash
sudo cp <BUNDLE>/matofx.toml /etc/matofx/matofx.toml
sudo chmod 640 /etc/matofx/matofx.toml
```

**Windows:**

```powershell
Copy-Item <BUNDLE>\matofx.toml C:\ProgramData\matofx\matofx.toml
```

Now **edit it** (`/etc/matofx/matofx.toml` or `C:\ProgramData\matofx\matofx.toml`) and set, at minimum:

> **Cross-platform path gotcha (read first):** TOML treats backslash as an escape character. **Always use forward slashes in file paths**, even on Windows. The application reads them fine on both OSes. A Windows path written with backslashes will either fail to parse or silently mis-interpret (e.g. the shipped Linux default `/etc/matofx/audit-signing.key` becomes `C:\etc\matofx\audit-signing.key` on Windows, not what you want).

1. **`environment`** — `"prod"` for a production install, `"staging"` / `"test"` / `"dev"` for everything else. **Required**; no default.
2. **`[db].dialect`** — `"sqlite"` (single-team installs only) or `"postgres"` (everything else).
3. **`[db].url`** — for SQLite, use an absolute forward-slash path:
   - Linux: `url = "sqlite:////var/lib/matofx/matofx.db"`
   - Windows: `url = "sqlite:///C:/ProgramData/matofx/db/matofx.db"`

   For Postgres, prefer to set this via env var rather than disk: leave the file with a placeholder and export `MATOFX__DB__URL=postgres://matofx:<password>@host:5432/matofx` in the service environment.
4. **`[audit].signing_key_path`** — where the Ed25519 key will live (still forward slashes on Windows):
   - Linux: `signing_key_path = "/etc/matofx/audit-signing.key"`
   - Windows: `signing_key_path = "C:/ProgramData/matofx/audit-signing.key"`
5. **`[audit].require_signing`** — `true` for production (cross-field validator refuses prod with this off). For dev / staging set to `false` if you haven't generated a key yet.
6. **`[tools].bundles_dir`** — where the API stages uploaded tool bundles. Each tool gets its own `<bundles_dir>/<alias>/` subdirectory. Created on demand.
   - Linux: `bundles_dir = "/var/lib/matofx/tools"`
   - Windows: `bundles_dir = "C:/ProgramData/matofx/tools"`

   You can also set this to a **relative** path — values like `"./tools"` resolve against the directory the `matofx.toml` file lives in. So `bundles_dir = "./tools"` next to `C:/ProgramData/matofx/matofx.toml` lands at `C:/ProgramData/matofx/tools` automatically. (The earlier behaviour resolved relative paths against the service's working directory — `C:\Windows\System32` under NSSM — which produced an Access Denied error the first time someone uploaded a tool. The DWIM anchoring is the fix.)

   If you don't add a `[tools]` section at all, the default `./tools` still applies and still anchors to the config-file directory — so a fresh install Just Works™ without an explicit entry.

7. **`[tutorials].tutorials_dir`** — same shape, for PDFs / videos uploaded via Admin → Tutorials.
   - Linux: `tutorials_dir = "/var/lib/matofx/tutorials"`
   - Windows: `tutorials_dir = "C:/ProgramData/matofx/tutorials"`

   The default `./tutorials` follows the same config-dir anchoring rule as `[tools]`.

8. **`[email]`** — outbound SMTP, if you want job-state and broadcast emails. Leave the section out entirely (or `enabled = false`) for a mail-free install. To enable:
   - `enabled = true`
   - `smtp_host` / `smtp_port` / `use_starttls` / `use_tls` for your relay. STARTTLS submission (Gmail, SES, SendGrid, M365): port `587`, `use_starttls = true`, `use_tls = false`. Implicit TLS / SMTPS: port `465`, `use_tls = true`, `use_starttls = false`. Plain internal relay: port `25`, both `false`, no credentials.
   - `smtp_username` / `smtp_password` — for Gmail the username is the full address and the password is a Google **App Password** (the account needs 2-Step Verification; the normal password will not authenticate).
   - `from_address` — must be an address the credentials are authorised to send as.
   - `public_url` — the URL users actually browse to (e.g. `https://matofx.your-company.com`); it is embedded in every email's deep links.

   `smtp_password` sits in this file in clear text, so the file-permission step matters — on Linux `sudo chmod 640 /etc/matofx/matofx.toml` (already done in B7's copy step). If you'd rather keep the secret off disk, omit `smtp_password` here and set `MATOFX__EMAIL__SMTP_PASSWORD` in the service environment instead (NSSM `AppEnvironmentExtra` / systemd `Environment=`). Host / port / username / from-address / `public_url` / STARTTLS can also be changed live from **Admin → General Settings** after first start; `enabled` and `use_tls` are TOML-only and need a restart.

> **Where do these paths anchor?** Relative paths in `[tools].bundles_dir`, `[tutorials].tutorials_dir`, and `[audit].signing_key_path` all resolve against the directory containing the `matofx.toml` that was loaded — not against the process's working directory. Absolute paths are honoured verbatim. The lookup chain for the config file itself is unchanged: `$MATOFX_CONFIG` → OS system path → per-user fallback.

### B8. Set the `MATOFX_CONFIG` environment variable

So every MATOFX process finds the same config file:

**Linux (systemd users — also add to the unit file under `Environment=` in B14):**

```bash
echo 'export MATOFX_CONFIG=/etc/matofx/matofx.toml' | sudo tee /etc/profile.d/matofx.sh > /dev/null
export MATOFX_CONFIG=/etc/matofx/matofx.toml
```

**Windows — choose one scope:**

> Machine-wide (best for production / Windows Services) needs an elevated PowerShell:

```powershell
# Right-click PowerShell → Run as administrator, then:
[Environment]::SetEnvironmentVariable("MATOFX_CONFIG", "C:\ProgramData\matofx\matofx.toml", "Machine")
```

> Per-user (no admin required, persists across PowerShell windows for *this* user — fine when you use NSSM with explicit `AppEnvironmentExtra` later, or the convenience launchers in §B13b which set it for you):

```powershell
[Environment]::SetEnvironmentVariable("MATOFX_CONFIG", "C:\ProgramData\matofx\matofx.toml", "User")
$env:MATOFX_CONFIG = "C:\ProgramData\matofx\matofx.toml"
```

If the Machine-scope call returns `"Required registry access is not allowed"` you're not elevated — drop to User scope, or re-run from an elevated PowerShell.

Verify in a **new** PowerShell window: `$env:MATOFX_CONFIG` should print the path. Then sanity-check the config itself parses:

```powershell
python -c "from matofx.config import load_settings;s=load_settings();print(s.environment, s.db.dialect)"
```

…should print e.g. `Environment.PROD postgres`. If it raises `ConfigError`, the message tells you exactly which field is wrong.

### B9. Generate the audit signing key

The MATOFX CLI doesn't yet ship a `rotate signing-key` subcommand (planned), so call the underlying API directly:

**Linux:**

```bash
python -c "from pathlib import Path; from matofx.audit import generate_signing_key; generate_signing_key(Path('/etc/matofx/audit-signing.key'))"
sudo chmod 600 /etc/matofx/audit-signing.key
```

**Windows:**

```powershell
python -c "from pathlib import Path; from matofx.audit import generate_signing_key; generate_signing_key(Path(r'C:\ProgramData\matofx\audit-signing.key'))"
```

Confirm: the file exists, is ~120 bytes, starts with `-----BEGIN PRIVATE KEY-----`. **Back this file up offline.** Loss = inability to verify pre-rotation audit records.

### B10. Initialise the database schema

Run the standalone migration helper that came in the bundle (it resolves the migrations from the installed wheel — you do **not** need an `alembic.ini` on the target):

```powershell
python <BUNDLE>\scripts\matofx_migrate.py upgrade head
```

```bash
python <BUNDLE>/scripts/matofx_migrate.py upgrade head
```

> **Do not run bare `alembic upgrade head`.** It looks for an `alembic.ini` in the current directory — which the bundle deliberately does not ship — and fails with `No 'script_location' key found in configuration`. The migrations live *inside* the installed `matofx` wheel; `matofx_migrate.py` is the supported way to reach them. `MATOFX_CONFIG` must be set first (step B8) — the helper reads the target database URL from it, so the same command works for SQLite and Postgres alike.

You'll see the full migration chain apply (`0001_baseline` through the current head). For Postgres, the same command works against the configured URL — the env's `MATOFX_CONFIG` is what the helper reads.

Verify with `python <BUNDLE>/scripts/matofx_migrate.py current` — should print the latest revision id.

### B11. Create the first super-user

```powershell
matofx admin create-first-user `
    --username admin `
    --email admin@yourcompany.com `
    --first-name Site `
    --last-name Administrator
```

You'll be prompted for the password (omit `--password` to avoid leaving it in shell history). **The minimum password length is 8.**

Expected output: `Created super-user 'admin' (id=1).`

If the call fails because the audit chain head doesn't exist yet on Postgres, run:

```powershell
python -c "import asyncio; from matofx.audit import ensure_chain_head; from matofx.config import load_settings; from matofx.db import make_engine_scope, make_session_factory, session_scope; \
async def m():\n  s=load_settings()\n  async with make_engine_scope(s.db) as e:\n    f=make_session_factory(e)\n    async with session_scope(f) as ss:\n      await ensure_chain_head(ss)\nasyncio.run(m())"
```

…then retry B11.

### B12. (Optional) Install the example tools

Skip for a clean production install. Useful for verifying the install before any customer-specific tools are loaded.

```powershell
python <BUNDLE>\scripts\seed_example_tools.py
```

This registers a local `StorageLocation` pointing at the jobs directory you created in B3 (`/srv/matofx/jobs` or `C:\ProgramData\matofx\jobs`) and installs three reference tools (`material_properties`, `colour_check`, `heat_transfer`) at state = `released`.

### B12b. Seed default branding (header icon + favicon)

The `install_*` scripts run this for you when `<BUNDLE>/branding/` exists. To run by hand against an existing install:

```powershell
python <BUNDLE>\scripts\seed_default_branding.py
```

```bash
python <BUNDLE>/scripts/seed_default_branding.py
```

Behaviour:

- Reads source files from `<BUNDLE>/branding/` — `header_icon.png` (or `.svg`) and `favicon.ico` (or `.png`). Either file is optional.
- Copies each found file into `<icons_dir>/<key>.<ext>` (where `icons_dir` defaults to `<config_dir>/branding/`) and writes the matching basename into the `runtime_setting` row, going through the same `GeneralSettingsService.set_image` path the live admin upload uses — same mime-allowlist, same per-key size cap, same audit shape.
- **Idempotent:** only writes a `runtime_setting` row when it's currently null, so re-running the installer over an admin's customised branding is a no-op. If an admin "Reset to default" from Admin → General Settings, the next install run re-seeds.

To customise the bundled defaults for your customer pack, drop replacement files into `etc/branding/` on the dev machine and rebuild the bundle (Part A2). The build script copies the whole directory verbatim. See `etc/branding/README.md` for the accepted filenames and constraints.

### B13. First-run smoke test (manual)

You have two options here. **B13a** is manual (one window per service) and is how I describe the underlying flow. **B13b** uses the bundled one-click launchers, which set `MATOFX_CONFIG`, activate the venv, and spawn both services for you — preferred for dev / demo / pilot installs.

#### B13a — manual start (both services, two windows)

`matofx-api` and `matofx-runner` are installed inside the venv, so they're **only on PATH while the venv is activated**. A fresh PowerShell or shell window won't see them.

**Windows — two PowerShell windows:**

Window 1 (API + UI):

```powershell
& C:\ProgramData\matofx\venv\Scripts\Activate.ps1
$env:MATOFX_CONFIG = "C:\ProgramData\matofx\matofx.toml"
matofx-api
```

Window 2 (runner):

```powershell
& C:\ProgramData\matofx\venv\Scripts\Activate.ps1
$env:MATOFX_CONFIG = "C:\ProgramData\matofx\matofx.toml"
matofx-runner
```

Or call the `.exe`s by full path (no activation needed):

```powershell
$env:MATOFX_CONFIG = "C:\ProgramData\matofx\matofx.toml"
& C:\ProgramData\matofx\venv\Scripts\matofx-api.exe
# in another window:
& C:\ProgramData\matofx\venv\Scripts\matofx-runner.exe
```

**Linux — two terminals:**

```bash
source /opt/matofx/venv/bin/activate
export MATOFX_CONFIG=/etc/matofx/matofx.toml
matofx-api
# in another terminal:
source /opt/matofx/venv/bin/activate
export MATOFX_CONFIG=/etc/matofx/matofx.toml
matofx-runner
```

Open <http://127.0.0.1:8000/ui/login>, sign in as `admin`, and confirm the dashboard loads. Stop with `Ctrl+C` in each window when done.

#### B13b — one-click launchers (recommended for dev / pilot)

The installer (`install_windows.ps1` / `install_linux.sh`) copies these into `<APP_HOME>` automatically. If you installed manually you can copy them yourself from `<BUNDLE>/scripts/`:

**Windows:**

| File installed to `C:\ProgramData\matofx\` | What it does |
|---|---|
| `matofx-start.bat`  | Double-click. Spawns two PowerShell windows (one for API, one for runner). Sets `MATOFX_CONFIG`, activates the venv, opens the browser. |
| `matofx-start.ps1`  | Same as above; right-click → **Run with PowerShell**. Pass `-NoBrowser` to skip the auto-open. |
| `matofx-stop.bat`   | Double-click. Force-stops both processes if a window has been lost. |
| `matofx-stop.ps1`   | Same as above; right-click → Run with PowerShell. |

```powershell
# From a normal PowerShell window — no venv activation needed:
C:\ProgramData\matofx\matofx-start.bat
```

**Linux:**

| File installed to `/opt/matofx/` | What it does |
|---|---|
| `matofx-start.sh` | Backgrounds both services with `nohup`. Logs land at `<APP_HOME>/logs/{api,runner}.log`, PIDs at `<APP_HOME>/run/{api,runner}.pid`. |
| `matofx-stop.sh`  | Reads the PID files, sends SIGTERM (then SIGKILL after 10 s), removes the PID files. |

```bash
/opt/matofx/matofx-start.sh
# Tail logs while it warms up:
tail -f /opt/matofx/logs/api.log /opt/matofx/logs/runner.log
# Stop:
/opt/matofx/matofx-stop.sh
```

The launchers are self-locating — they discover the venv and config relative to their own directory. Copy them anywhere inside `<APP_HOME>` and they still work.

> **Production use:** Use systemd / NSSM (B14) instead. The launchers are convenience wrappers; they don't restart on failure and they don't survive a host reboot.

### B14. Register as system services (production)

Manual `matofx-api` / `matofx-runner` invocations don't survive a reboot. Wire them up as services.

**Linux (systemd) — `/etc/systemd/system/matofx-api.service`:**

```ini
[Unit]
Description=MATOFX SPDM API + UI
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
User=matofx
Group=matofx
Environment=MATOFX_CONFIG=/etc/matofx/matofx.toml
WorkingDirectory=/opt/matofx
ExecStart=/opt/matofx/venv/bin/matofx-api
Restart=on-failure
RestartSec=5
StandardOutput=append:/var/log/matofx/api.log
StandardError=append:/var/log/matofx/api.log

[Install]
WantedBy=multi-user.target
```

…and `/etc/systemd/system/matofx-runner.service`:

```ini
[Unit]
Description=MATOFX SPDM Runner
After=network-online.target matofx-api.service

[Service]
Type=simple
User=matofx
Group=matofx
Environment=MATOFX_CONFIG=/etc/matofx/matofx.toml
WorkingDirectory=/opt/matofx
ExecStart=/opt/matofx/venv/bin/matofx-runner
Restart=on-failure
RestartSec=5
StandardOutput=append:/var/log/matofx/runner.log
StandardError=append:/var/log/matofx/runner.log

[Install]
WantedBy=multi-user.target
```

Activate:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now matofx-api matofx-runner
sudo systemctl status matofx-api matofx-runner
```

**Windows (NSSM — Non-Sucking Service Manager — recommended):**

```powershell
# Install NSSM first: choco install nssm  or download from https://nssm.cc/
nssm install MATOFX-API   "C:\ProgramData\matofx\venv\Scripts\matofx-api.exe"
nssm set    MATOFX-API    AppEnvironmentExtra "MATOFX_CONFIG=C:\ProgramData\matofx\matofx.toml"
nssm set    MATOFX-API    AppStdout "C:\ProgramData\matofx\logs\api.log"
nssm set    MATOFX-API    AppStderr "C:\ProgramData\matofx\logs\api.log"
nssm set    MATOFX-API    Start SERVICE_AUTO_START

nssm install MATOFX-Runner "C:\ProgramData\matofx\venv\Scripts\matofx-runner.exe"
nssm set    MATOFX-Runner  AppEnvironmentExtra "MATOFX_CONFIG=C:\ProgramData\matofx\matofx.toml"
nssm set    MATOFX-Runner  AppStdout "C:\ProgramData\matofx\logs\runner.log"
nssm set    MATOFX-Runner  AppStderr "C:\ProgramData\matofx\logs\runner.log"
nssm set    MATOFX-Runner  Start SERVICE_AUTO_START

Start-Service MATOFX-API, MATOFX-Runner
Get-Service MATOFX-API, MATOFX-Runner
```

### B15. Reverse proxy + TLS (production)

MATOFX listens on plain HTTP on port 8000 by design — terminate TLS at a reverse proxy (Nginx, Caddy, IIS) that fronts it. Minimal Nginx config:

```nginx
server {
    listen 443 ssl http2;
    server_name matofx.your-company.com;
    ssl_certificate     /etc/letsencrypt/live/matofx.your-company.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/matofx.your-company.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade           $http_upgrade;
        proxy_set_header Connection        "upgrade";
        proxy_read_timeout                 300s;     # NiceGUI long-poll
    }
}
```

### B16. Verify the install end-to-end

1. `systemctl status matofx-api matofx-runner` (Linux) or `Get-Service MATOFX-*` (Windows) — both `active (running)` / `Running`.
2. `curl -s http://127.0.0.1:8000/api/health` returns `{"status":"ok"}`.
3. Browse to the proxy URL, sign in, dashboard loads.
4. `matofx audit verify` exits 0.
5. Tail the log files for one minute; nothing at WARNING or above should appear in steady state.

### B17. Lock down — production checklist

- [ ] `[audit].require_signing = true` and the key file is `chmod 600` and backed up offline.
- [ ] `MATOFX__DB__URL` is set as a service-environment variable, not committed to disk in clear text (Postgres only).
- [ ] DB user `matofx` has `CREATE, SELECT, INSERT, UPDATE, DELETE` on the matofx database — **not** SUPERUSER.
- [ ] `/var/log/matofx/` rotates via logrotate (Linux) or NSSM's built-in rotation (Windows).
- [ ] Audit signing key, the database, and `matofx.toml` are included in the host's backup policy.
- [ ] First-user password rotated; super-user reserved for break-glass; day-to-day admin uses an `administrator`-role account.
- [ ] Outbound SMTP credentials in `[email]` set; `[email].enabled = true` only after a test mail succeeds; `matofx.toml` is `chmod 640` (Linux) or ACL-restricted (Windows) because `smtp_password` is on disk in clear text.
- [ ] Reverse proxy enforces HTTPS-only with HSTS; the underlying `matofx-api` listens on `127.0.0.1` only (no inbound exposure of port 8000).

---

## Rollback / re-install

To start completely over without re-imaging the box:

```bash
sudo systemctl stop matofx-api matofx-runner
sudo rm /var/lib/matofx/matofx.db        # SQLite — for Postgres, DROP / CREATE the database
# Re-run from B10 onwards.
```

```powershell
Stop-Service MATOFX-Runner, MATOFX-API
Remove-Item C:\ProgramData\matofx\db\matofx.db
# Re-run from B10 onwards.
```

The audit key (B9) and `matofx.toml` (B7) are kept across re-installs by design.

---

## Upgrading to a newer bundle

1. Stop both services.
2. Activate the venv (`source /opt/matofx/venv/bin/activate` or `.\venv\Scripts\Activate.ps1`).
3. `cd <NEW_BUNDLE>` then `pip install --upgrade --requirement requirements.lock` (the lockfile contains the new wheel's `file:` entry).
4. `python scripts/matofx_migrate.py upgrade head` — applies any new migrations.
5. Start the services.

The audit chain, the DB, the signing key, and the config file are preserved unchanged.
