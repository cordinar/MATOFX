"""Common engineering / physics / chemistry units for the descriptor wizard.

The descriptor wizard's per-input and per-output unit picker (Session
C C3) draws from this module's :data:`UNIT_OPTIONS` table. Two
sentinel choices live alongside the catalogue:

- :data:`UNIT_NONE_VALUE` — the operator picked "no unit" (the
  underlying ``InputField.unit`` is set to ``None``).
- :data:`UNIT_CUSTOM_VALUE` — the operator picked "Custom unit…",
  meaning the wizard reveals a free-text input next to the dropdown
  whose value gets written through to the descriptor.

The dropdown is rendered as a :class:`nicegui.ui.select` with
``with_input=True`` (typeahead). The category prefix appears in each
label so an operator scrolling the list sees it grouped, and so a
typeahead search like ``MPa`` filters down even with ~300 entries.

Adding a unit: edit :data:`_UNIT_TABLE` in place. Order within each
category is by magnitude where intuitive (mm before m before km),
otherwise alphabetical. The label is what the operator sees in the
dropdown ("Pressure / stress: MPa"); the *value* is what lands in the
descriptor (``MPa``). Values must be unique across the whole table —
the wizard depends on a unique value to round-trip back to the
right dropdown entry when an existing tool is loaded for editing.
"""

from __future__ import annotations

UNIT_NONE_VALUE = "__none__"
"""Sentinel value: operator chose 'No unit'. Maps to ``InputField.unit = None``."""

UNIT_CUSTOM_VALUE = "__custom__"
"""Sentinel value: operator chose 'Custom unit…'. Wizard reveals a text input."""


# Source-of-truth table. Each entry is ``(category, [units...])``;
# the order here drives the dropdown order. Custom + None are added
# at the top by :func:`unit_select_options` so they're always one
# scroll away.
_UNIT_TABLE: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Length", ("m", "cm", "mm", "μm", "nm", "Å", "km", "in", "mil", "ft", "yd", "mi")),
    ("Area", ("m²", "cm²", "mm²", "km²", "in²", "ft²", "ha", "acre")),
    (
        "Volume",
        (
            "m³",
            "cm³",
            "mm³",
            "dm³",
            "L",
            "mL",
            "μL",
            "in³",
            "ft³",
            "gal (US)",
            "gal (UK)",
            "fl oz",
            "bbl",
        ),
    ),
    ("Mass", ("kg", "g", "mg", "μg", "ng", "t", "Da", "lb", "oz", "slug")),
    ("Time", ("s", "ms", "μs", "ns", "ps", "fs", "min", "h", "d", "wk", "yr")),
    ("Temperature", ("K", "°C", "°F", "°R")),
    (
        "Pressure / stress",
        (
            "Pa",
            "hPa",
            "kPa",
            "MPa",
            "GPa",
            "bar",
            "mbar",
            "atm",
            "psi",
            "ksi",
            "Torr",
            "mmHg",
            "mmH₂O",
            "inHg",
        ),
    ),
    ("Force", ("N", "kN", "MN", "mN", "lbf", "kgf", "dyne")),
    ("Torque / moment", ("N·m", "N·mm", "kN·m", "mN·m", "lbf·ft", "lbf·in")),
    ("Strain", ("m/m", "mm/mm", "%ε", "με")),
    ("Stiffness / spring rate", ("N/mm", "kN/mm", "lbf/in")),
    ("Fracture toughness", ("MPa·√m", "ksi·√in")),
    (
        "Energy",
        (
            "J",
            "kJ",
            "MJ",
            "GJ",
            "mJ",
            "μJ",
            "Wh",
            "kWh",
            "MWh",
            "cal",
            "kcal",
            "Btu",
            "ft·lbf",
            "erg",
            "eV",
        ),
    ),
    ("Power", ("W", "kW", "MW", "GW", "mW", "μW", "hp", "Btu/h", "VA", "kVA")),
    (
        "Electrical",
        (
            "V",
            "mV",
            "kV",
            "μV",
            "V/m",
            "V/mm",
            "A",
            "mA",
            "μA",
            "kA",
            "A/mm²",
            "Ω",
            "mΩ",
            "kΩ",
            "MΩ",
            "F",
            "μF",
            "nF",
            "pF",
            "H",
            "mH",
            "μH",
            "S",
            "C",
            "mC",
            "μC",
            "nC",
            "pC",
            "Ah",
            "mAh",
            "Wb",
            "T",
            "mT",
            "μT",
            "G",
        ),
    ),
    ("Frequency / rate", ("Hz", "kHz", "MHz", "GHz", "THz", "rpm", "rad/s")),
    ("Speed", ("mm/s", "cm/s", "m/s", "km/h", "mph", "ft/s", "knots")),
    ("Acceleration", ("m/s²", "g", "ft/s²")),
    ("Density", ("kg/m³", "g/cm³", "kg/L", "g/L", "lb/ft³", "lb/in³")),
    (
        "Flow rate (volumetric)",
        (
            "m³/s",
            "m³/min",
            "m³/h",
            "L/s",
            "L/min",
            "L/h",
            "mL/min",
            "gpm",
            "ft³/min",
            "sccm",
            "slpm",
        ),
    ),
    (
        "Flow rate (mass)",
        ("t/h", "kg/s", "kg/h", "kg/min", "g/s", "g/min", "lb/h", "lb/min"),
    ),
    (
        "Concentration",
        (
            "mol/L (M)",
            "mmol/L (mM)",
            "μM",
            "nM",
            "mol/m³",
            "kmol/m³",
            "mmol/m³",
            "μmol/m³",
            "%",
            "wt%",
            "vol%",
            "mol%",
            "ppm",
            "ppb",
            "mg/kg",
            "mg/L",
            "μg/L",
            "mg/mL",
            "μg/mL",
        ),
    ),
    ("Substance", ("kmol", "mol", "mmol", "μmol", "nmol")),
    ("Molar mass / molality", ("g/mol", "kg/mol", "mol/kg")),
    ("Angle", ("rad", "mrad", "°", "arcmin", "arcsec", "grad")),
    ("Solid angle", ("sr",)),
    (
        "Heat / thermal",
        (
            "J/K",
            "J/(kg·K)",
            "J/(mol·K)",
            "W/(m·K)",
            "W/(m²·K)",
            "K/W",
            "m²·K/W",
            "J/m³",
            "kJ/kg",
            "kJ/(kg·K)",
            "Btu/(lb·°F)",
            "1/K",
            "ppm/K",
            "ppm/°C",
        ),
    ),
    ("Heat flux / irradiance", ("W/m²", "kW/m²", "W/cm²", "Btu/(h·ft²)")),
    ("Specific quantities", ("J/kg", "m³/kg")),
    ("Diffusivity", ("m²/s", "cm²/s", "mm²/s")),
    ("Permeability", ("darcy", "mD")),
    ("Viscosity (dynamic)", ("Pa·s", "mPa·s", "cP", "P")),
    ("Viscosity (kinematic)", ("m²/s (kinematic)", "cSt", "St")),
    ("Surface tension", ("N/m", "mN/m", "dyn/cm")),
    (
        "Electrical resistivity / conductivity",
        ("Ω·m", "Ω·cm", "S/m", "S/cm", "mS/cm", "μS/cm"),
    ),
    ("Optical / luminous", ("cd", "lm", "lx", "cd/m² (nit)", "W/m²/sr")),
    ("Acoustic", ("dB", "dBA", "dBC")),
    ("Ionising radiation", ("Bq", "Ci", "Gy", "Sv", "R")),
    ("Wavenumber", ("cm⁻¹", "m⁻¹")),
    ("Hardness (scales)", ("HV", "HRC", "HRB", "HBW", "Shore A", "Shore D", "Mohs")),
    ("Dimensionless", ("%", "fraction (0–1)", "pH", "ratio", "a.u.")),  # noqa: RUF001
    ("Count / cycles", ("count", "#", "cycles")),
)


def _build_options() -> dict[str, str]:
    """Return ``{value: label}`` for the unit dropdown.

    Two sentinels appear first so the operator never has to scroll
    past the catalogue to find them; the catalogue itself follows in
    table order (which is the carefully-chosen display order, not
    alphabetical across categories).

    Duplicate values within the catalogue (e.g. ``%`` appears in both
    Concentration and Dimensionless, ``m²/s`` in both Diffusivity and
    Viscosity) are de-duplicated by the dict keys; the *first* entry
    wins so the more domain-specific category gets the slot.
    """
    options: dict[str, str] = {
        UNIT_NONE_VALUE: "No unit",
        UNIT_CUSTOM_VALUE: "Custom unit…",
    }
    seen: set[str] = set()
    for category, units in _UNIT_TABLE:
        for unit in units:
            if unit in seen:
                continue
            seen.add(unit)
            options[unit] = f"{category}: {unit}"
    return options


UNIT_OPTIONS: dict[str, str] = _build_options()
"""``{value: label}`` map ready for ``ui.select(options=UNIT_OPTIONS, with_input=True)``."""


def is_known_unit(value: str | None) -> bool:
    """Return ``True`` when ``value`` is one of the catalogue's listed units.

    Used by the wizard's edit flow: when an existing tool's
    ``InputField.unit`` is loaded, the wizard checks the catalogue
    first and falls back to the Custom mode (with the literal value
    pre-filled) when the descriptor's unit isn't a recognised entry.
    """
    if value is None:
        return False
    return value in UNIT_OPTIONS and value not in {UNIT_NONE_VALUE, UNIT_CUSTOM_VALUE}


def resolve_unit(choice: str | None, custom: str | None) -> str | None:
    """Collapse a ``(choice, custom)`` pair from the unit picker into one string.

    The two-widget picker (a select over :data:`UNIT_OPTIONS` plus a
    reveal-on-Custom text input) lets the user pick either a known unit
    or type a free-form one. This helper is the single resolver both
    consumers (the descriptor wizard + the materials class-property
    editor) should call at submit so the rules stay consistent:

    - ``__none__`` (``UNIT_NONE_VALUE``) → ``None`` ("no unit").
    - ``__custom__`` (``UNIT_CUSTOM_VALUE``) → the stripped custom text,
      or ``None`` when the box was left blank.
    - Anything else → the choice value itself (a known unit code).
    """
    if choice is None or choice == UNIT_NONE_VALUE:
        return None
    if choice == UNIT_CUSTOM_VALUE:
        clean = (custom or "").strip()
        return clean or None
    return str(choice)
