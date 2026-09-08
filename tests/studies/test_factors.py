"""Exhaustive tests for the factor specification + mode gating (H40).

Pure — no DB. Covers parsing of the design-§3 JSON shape, per-type /
per-kind coherence, and the mode-compatibility gate (design §3/§9).
"""

from __future__ import annotations

import pytest

from matofx.studies import FactorSpec, FactorSpecError, ModeNotApplicableError

# The canonical design-§3 example.
_SPEC_OBJ = {
    "thermal_conductivity": {
        "role": "vary",
        "type": "numeric",
        "vary": {"kind": "range", "min": 0.1, "max": 2.5},
    },
    "component_a_pct": {
        "role": "vary",
        "type": "numeric",
        "vary": {"kind": "list", "values": [10, 20, 30]},
    },
    "mesh_density": {
        "role": "vary",
        "type": "select",
        "vary": {"kind": "subset", "options": ["coarse", "fine"]},
    },
    "use_radiation": {"role": "vary", "type": "bool"},
    "ambient_temp": {"role": "constant", "value": 20.0},
}


def test_parses_canonical_shape() -> None:
    spec = FactorSpec.from_obj(_SPEC_OBJ)
    assert set(spec.varied) == {
        "thermal_conductivity",
        "component_a_pct",
        "mesh_density",
        "use_radiation",
    }
    assert spec.constants == {"ambient_temp": 20.0}


def test_empty_mapping_rejected() -> None:
    with pytest.raises(FactorSpecError, match="non-empty"):
        FactorSpec.from_obj({})


def test_all_constant_rejected() -> None:
    with pytest.raises(FactorSpecError, match="at least one varied"):
        FactorSpec.from_obj({"x": {"role": "constant", "value": 1}})


def test_constant_with_vary_rejected() -> None:
    with pytest.raises(FactorSpecError):
        FactorSpec.from_obj(
            {"x": {"role": "constant", "value": 1, "vary": {"kind": "list", "values": [1]}}}
        )


def test_varied_numeric_without_vary_rejected() -> None:
    with pytest.raises(FactorSpecError):
        FactorSpec.from_obj({"x": {"role": "vary", "type": "numeric"}})


def test_wrong_kind_for_type_rejected() -> None:
    with pytest.raises(FactorSpecError):
        FactorSpec.from_obj(
            {"x": {"role": "vary", "type": "select", "vary": {"kind": "range", "min": 0, "max": 1}}}
        )


def test_range_max_must_exceed_min() -> None:
    with pytest.raises(FactorSpecError):
        FactorSpec.from_obj(
            {
                "x": {
                    "role": "vary",
                    "type": "numeric",
                    "vary": {"kind": "range", "min": 5, "max": 5},
                }
            }
        )


def test_bool_vary_is_implicit() -> None:
    spec = FactorSpec.from_obj({"flag": {"role": "vary", "type": "bool"}})
    assert spec.varied["flag"].vary is None


def _num_range(lo: float, hi: float) -> dict[str, object]:
    return {"role": "vary", "type": "numeric", "vary": {"kind": "range", "min": lo, "max": hi}}


def test_rsm_requires_continuous_numeric() -> None:
    spec = FactorSpec.from_obj(
        {
            "x": _num_range(0, 1),
            "m": {
                "role": "vary",
                "type": "select",
                "vary": {"kind": "subset", "options": ["a", "b"]},
            },
        }
    )
    with pytest.raises(ModeNotApplicableError, match="Response-surface"):
        spec.validate_for_mode("ccd")


def test_screening_requires_continuous_numeric() -> None:
    spec = FactorSpec.from_obj({"x": _num_range(0, 1), "flag": {"role": "vary", "type": "bool"}})
    with pytest.raises(ModeNotApplicableError, match="Screening"):
        spec.validate_for_mode("plackett_burman")


def test_file_factor_only_list_or_grid() -> None:
    spec = FactorSpec.from_obj(
        {"g": {"role": "vary", "type": "file", "vary": {"kind": "file_list", "cas_hashes": ["h1"]}}}
    )
    spec.validate_for_mode("list")  # ok
    spec.validate_for_mode("grid")  # ok
    with pytest.raises(ModeNotApplicableError, match="file"):
        spec.validate_for_mode("lhs")


def test_distribution_only_space_filling() -> None:
    spec = FactorSpec.from_obj(
        {
            "x": {
                "role": "vary",
                "type": "numeric",
                "vary": {"kind": "distribution", "dist": "normal", "mean": 0, "std": 1},
            }
        }
    )
    spec.validate_for_mode("lhs")  # ok
    with pytest.raises(ModeNotApplicableError, match="distribution"):
        spec.validate_for_mode("grid")


def test_list_mode_rejects_continuous() -> None:
    spec = FactorSpec.from_obj({"x": _num_range(0, 1)})
    with pytest.raises(ModeNotApplicableError, match="explicit-list"):
        spec.validate_for_mode("list")


def test_normal_distribution_needs_mean_std() -> None:
    with pytest.raises(FactorSpecError):
        FactorSpec.from_obj(
            {
                "x": {
                    "role": "vary",
                    "type": "numeric",
                    "vary": {"kind": "distribution", "dist": "normal"},
                }
            }
        )
