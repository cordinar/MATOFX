# Default branding assets

Drop the placeholder header icon and favicon that ship with every MATOFX install in this directory. The build script copies the whole folder into the install bundle (as `branding/`) and the installer's `seed_default_branding.py` step copies them into the customer's `branding.icons_dir` and seeds the matching `runtime_setting` rows — but only when the rows are still empty, so a customer admin's later upload via Admin → General Settings is never overwritten.

## Expected files

| Source filename | Lands at | DB key |
|---|---|---|
| `header_icon.png` (or `.svg`) | `<icons_dir>/header_icon_path.<ext>` | `header_icon_path` |
| `favicon.png` (or `.ico`) | `<icons_dir>/favicon_path.<ext>` | `favicon_path` |

Either file is optional — missing files are skipped silently. If you ship neither, fresh installs render the title text only and fall back to the browser's default tab icon (the same behaviour you'd get without this seeding step).

## Constraints

- **Mime type** must match the catalog's accepted set: PNG or SVG for the header icon, ICO or PNG for the favicon. Anything else is rejected by the seed script with a clear message.
- **Size** must fit the per-key cap from `BrandingSettings` (favicon ≤ 2 MiB, header icon ≤ 8 MiB). The seed uses the same `GeneralSettingsService.set_image` validators as the live upload route, so the rules can't drift.
- **Filename inside this directory is what we read**; the destination filename uses the `<key>.<ext>` convention so admin re-uploads via the UI overwrite the seeded file in place. You don't need to match the destination naming yourself.

## Re-running the installer

The seed is idempotent: it only writes when the matching `runtime_setting` row is `null`/missing. So upgrading an install over the top of an admin's customised branding is safe — the seed step is a no-op once the customer has uploaded their own logo. If an admin "Reset to default" from the General Settings UI, the next install run will re-seed the bundled defaults; that's intentional.
