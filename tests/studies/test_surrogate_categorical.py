"""A varied categorical is a real factor of the fit (2026-09-02 regression).

**The defect.** ``fit_surrogate`` fitted on the numeric columns only, so a
varied ``select`` input — a material, a solver choice — was silently dropped.
Two runs with identical numbers and different materials became the same point
with two different answers, which the fit carried as *irreducible error*. On the
real Finned Cold Plate archive that meant:

- widening a cohort from one material to two made the model **worse**
  (``mass`` Q2 1.000 -> 0.855 on an exactly-representable algebraic product);
- the exported tool offered a ``material`` picker and returned the **same
  numbers** whichever you chose — one blended prediction for both.

These tests pin the fix at every level it has to hold: the fit, the prediction,
the recorded factors, and the exported tool's form.
"""

from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pytest

from matofx.models.bundle import write_combined_bundle
from matofx.models.runtime import run_surrogate_tool
from matofx.models.tool_synthesis import synthesise_descriptor
from matofx.studies.surrogate import fit_surrogate
from matofx.studies.surrogate.field import fit_field_surrogate

# Mass is *exactly* density x volume — a product a GP fits to machine precision
# when it can see the density, and cannot fit at all when it cannot.
_DENSITY = {"Aluminium": 2700.0, "Magnesium": 1740.0}
_HEIGHTS = [5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 35.0, 40.0]


def _mass(height_mm: float, material: str) -> float:
    volume = 1.0e-4 * 6.0e-3 + 20 * 1.0e-3 * (height_mm / 1000.0) * 0.1
    return _DENSITY[material] * volume * 1000.0


def _two_material_training() -> tuple[dict, list[float], dict]:
    heights = _HEIGHTS * 2
    materials = ["Aluminium"] * len(_HEIGHTS) + ["Magnesium"] * len(_HEIGHTS)
    masses = [_mass(h, m) for h, m in zip(heights, materials, strict=True)]
    return {"fin_height": heights}, masses, {"material": materials}


# --- the fit ---------------------------------------------------------------


def test_a_dropped_categorical_makes_an_exact_function_unfittable() -> None:
    """The control: without it, the model cannot fit mass at all."""
    numeric, masses, _ = _two_material_training()
    fit = fit_surrogate(numeric, masses, kind="gp")

    assert fit.fitted
    assert fit.quality.q2 is not None
    assert fit.quality.q2 < 0.95, "the defect should be visible in this fixture"


def test_a_varied_categorical_restores_an_exact_fit() -> None:
    numeric, masses, categorical = _two_material_training()
    fit = fit_surrogate(numeric, masses, kind="gp", categorical_columns=categorical)

    assert fit.categorical_levels == {"material": ("Aluminium", "Magnesium")}
    assert fit.quality.q2 == pytest.approx(1.0, abs=1e-3)


def test_the_prediction_actually_depends_on_the_level() -> None:
    """The user-visible symptom: two materials, one answer."""
    numeric, masses, categorical = _two_material_training()
    fit = fit_surrogate(numeric, masses, kind="gp", categorical_columns=categorical)

    (aluminium,), _ = fit.predict([{"fin_height": 22.0, "material": "Aluminium"}])
    (magnesium,), _ = fit.predict([{"fin_height": 22.0, "material": "Magnesium"}])

    assert aluminium == pytest.approx(_mass(22.0, "Aluminium"), rel=0.02)
    assert magnesium == pytest.approx(_mass(22.0, "Magnesium"), rel=0.02)
    assert aluminium > magnesium * 1.4  # the real density ratio is 1.55


def test_an_unseen_level_is_refused_not_guessed() -> None:
    """All-zeros is a point the model never saw; a confident number for it is
    exactly the failure this encoding exists to stop."""
    numeric, masses, categorical = _two_material_training()
    fit = fit_surrogate(numeric, masses, kind="gp", categorical_columns=categorical)

    with pytest.raises(ValueError, match="not trained on"):
        fit.predict([{"fin_height": 22.0, "material": "Titanium"}])


def test_a_constant_categorical_is_not_made_an_axis() -> None:
    """One level is a held constant, not a factor — and a one-hot column of
    ones is a column the scaler cannot scale."""
    fit = fit_surrogate(
        {"fin_height": _HEIGHTS},
        [_mass(h, "Aluminium") for h in _HEIGHTS],
        kind="gp",
        categorical_columns={"material": ["Aluminium"] * len(_HEIGHTS)},
    )
    assert fit.categorical_levels == {}
    assert fit.fitted


def test_omitting_categoricals_behaves_exactly_as_before() -> None:
    """Raw-column callers (parametric analytics, MOP, BO) are untouched."""
    fit = fit_surrogate({"fin_height": _HEIGHTS}, [_mass(h, "Aluminium") for h in _HEIGHTS])
    assert fit.categorical_levels == {}
    assert fit.factors == ["fin_height"]


def test_a_missing_level_falls_back_to_a_trained_one() -> None:
    """A profiler varying one axis must not have to supply every other."""
    numeric, masses, categorical = _two_material_training()
    fit = fit_surrogate(numeric, masses, kind="gp", categorical_columns=categorical)
    means, _ = fit.predict([{"fin_height": 22.0}])
    assert means
    assert np.isfinite(means[0])


# --- the field half --------------------------------------------------------


def test_a_field_surrogate_learns_the_categorical_too() -> None:
    """Fields fit one scalar per POD mode, so they inherit the same axis."""
    heights = _HEIGHTS * 2
    materials = ["Aluminium"] * len(_HEIGHTS) + ["Magnesium"] * len(_HEIGHTS)
    shape = np.sin(np.linspace(0.0, np.pi, 24))
    # The field's magnitude depends on the material as well as the height.
    snapshots = [
        (h / 10.0) * shape * (1.0 if m == "Aluminium" else 2.0)
        for h, m in zip(heights, materials, strict=True)
    ]
    surrogate = fit_field_surrogate(
        snapshots,
        {"fin_height": heights},
        categorical_columns={"material": materials},
    )
    hot = surrogate.predict({"fin_height": 22.0, "material": "Magnesium"}).field
    cool = surrogate.predict({"fin_height": 22.0, "material": "Aluminium"}).field
    assert float(np.max(np.abs(hot))) > float(np.max(np.abs(cool))) * 1.4


# --- what the exported tool offers ----------------------------------------


def test_the_exported_form_offers_the_level_as_a_modelled_choice() -> None:
    descriptor = synthesise_descriptor(
        source_tool_alias="plate",
        model_id=1,
        version_id=1,
        name="Plate surrogate",
        description="d",
        factors=[
            {"name": "fin_height", "min": 5.0, "max": 40.0},
            {"name": "material", "levels": ["Aluminium", "Magnesium"]},
        ],
        responses=[{"name": "mass", "kind": "scalar"}],
        version_ordinal=1,
    )
    material = next(i for i in descriptor.inputs if i.name == "material")
    assert material.type == "select"
    assert material.options == ["Aluminium", "Magnesium"]
    assert material.required is True
    # The old wording promised the opposite and must not survive.
    assert "predict differences" not in (material.help_text or "")
    assert "changes with it" in (material.help_text or "")


def test_the_exported_tool_predicts_differently_per_level(tmp_path: Path) -> None:
    """End to end: the headline symptom, through the real runtime."""
    numeric, masses, categorical = _two_material_training()
    fit = fit_surrogate(numeric, masses, kind="gp", categorical_columns=categorical)

    bundle = tmp_path / "bundle"
    sha = write_combined_bundle(
        bundle,
        {"mass": fit},
        {},
        factors=[
            {"name": "fin_height", "min": 5.0, "max": 40.0},
            {"name": "material", "levels": ["Aluminium", "Magnesium"]},
        ],
        responses=[{"name": "mass", "kind": "scalar"}],
        field_formats={},
    )

    def _predict(material: str) -> float:
        out = tmp_path / f"out_{material}"
        run_surrogate_tool(
            bundle_path=str(bundle),
            expected_sha256=sha,
            version_id=1,
            tool_alias="surrogate_plate_1",
            point={"fin_height": 22.0, "material": material},
            out_dir=str(out),
        )
        doc = json.loads((out / "outputs.json").read_text(encoding="utf-8"))
        return next(o["value"] for o in doc["outputs"] if o["name"] == "mass")

    aluminium, magnesium = _predict("Aluminium"), _predict("Magnesium")
    assert aluminium != pytest.approx(magnesium), "the exported tool ignored the choice"
    assert aluminium == pytest.approx(_mass(22.0, "Aluminium"), rel=0.02)
    assert magnesium == pytest.approx(_mass(22.0, "Magnesium"), rel=0.02)


# --- bundles outlive the class --------------------------------------------


def test_a_bundle_written_before_categoricals_still_predicts(tmp_path: Path) -> None:
    """Every already-exported surrogate must keep working.

    A bundle is a joblib pickle of ``Surrogate``. One written before
    categoricals became factors has no ``categorical_levels``, so ``predict``
    raised ``AttributeError`` on its first use — at run time, in the one
    surface that runs unattended. ``__setstate__`` defaults it.
    """
    fit = fit_surrogate({"x": [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]}, [2.0, 4.0, 6.0, 8.0, 10.0, 12.0])
    # Exactly the shape an older release pickled.
    del fit.__dict__["categorical_levels"]
    del fit.__dict__["training_categoricals"]
    path = tmp_path / "old.joblib"
    joblib.dump(fit, path)

    loaded = joblib.load(path)
    assert loaded.categorical_levels == {}
    assert loaded.training_categoricals == {}
    means, _ = loaded.predict([{"x": 2.5}])
    assert means[0] == pytest.approx(5.0, rel=0.05)
