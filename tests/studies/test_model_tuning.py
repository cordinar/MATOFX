"""ModelService records and reproduces each result's tuning (ADR-0022).

Schema via the shared ``studies`` conftest. Kept to few DB tests on purpose:
each one cycles the migrations. The rule itself is pinned, without a database,
in ``test_surrogate_tuning.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import matofx.library.orm
import matofx.materials.orm
import matofx.vv.orm  # noqa: F401  -- registers tables approval_request points at
from matofx.api.schemas import (
    ModelCompareRequest,
    ModelCreateRequest,
    ModelRefitRequest,
    ModelTuningChoice,
)
from matofx.audit import AuditWriter, ensure_chain_head
from matofx.db import make_session_factory, session_scope
from matofx.models import make_model_service
from matofx.models.bundle import load_scalar_bundle
from matofx.models.errors import ModelConfigError
from matofx.models.service import _recorded_tuning
from tests.studies.test_model_registry import _seed_source


def _service(tmp_path: Path):
    return make_model_service(
        AuditWriter(signing_key=None, key_id=None),
        models_root=tmp_path / "store",
        environment="test",
    )


# --- request defaults ------------------------------------------------------------


def test_refit_request_keeps_the_method_and_tuning_by_default() -> None:
    """An empty Refit body must not pick a method (it used to force ``gp``)."""
    body = ModelRefitRequest()
    assert body.surrogate_kind is None
    assert body.tuning_mode is None


def test_build_and_compare_default_to_automatic_tuning() -> None:
    common = {"tool_alias": "t", "source_kind": "cohort", "source_id": 1}
    create = ModelCreateRequest(name="m", response_names=["y"], **common)
    assert create.tuning_mode == "automatic"
    assert create.tuning_overrides == {}
    compare = ModelCompareRequest(response_names=["y"], methods=["gp"], **common)
    assert compare.tuning_mode == "automatic"


# --- reproducing a version on refit --------------------------------------------------


def test_recorded_tuning_reads_adr_0022_rows() -> None:
    rows = [
        {
            "name": "y",
            "kind": "scalar",
            "method": "poly",
            "output_scale": "as_measured",
            "input_scale": "as_measured",
            "tuning_source": "user",
            "tuning_mode": "automatic",
            "tuning_methods": ["gp", "poly"],
        },
        {
            "name": "z",
            "kind": "scalar",
            "method": "gp",
            "output_scale": "log",
            "input_scale": "log",
            "tuning_source": "auto",
            "tuning_mode": "automatic",
            "tuning_methods": ["gp", "poly"],
        },
        {"name": "f", "kind": "field"},
    ]
    recorded = _recorded_tuning(rows, "gp")
    assert recorded.mode == "automatic"
    assert recorded.methods == ["gp", "poly"]
    # MATOFX's own pick (z) is re-decided; only the user's choice is carried.
    assert recorded.overrides == {
        "y": {"method": "poly", "output_scale": "as_measured", "input_scale": "as_measured"}
    }


def test_recorded_tuning_maps_adr_0021_and_older_versions() -> None:
    adr21 = [
        {
            "name": "y",
            "kind": "scalar",
            "output_scale": "log",
            "output_scale_source": "user",
            "output_scale_mode": "both",
        },
        {
            "name": "z",
            "kind": "scalar",
            "output_scale": "as_measured",
            "output_scale_source": "auto",
            "output_scale_mode": "both",
        },
    ]
    recorded = _recorded_tuning(adr21, "gp_matern")
    assert recorded.mode == "automatic"
    assert recorded.overrides == {
        "y": {"method": "gp_matern", "output_scale": "log", "input_scale": "as_measured"}
    }
    assert _recorded_tuning([{"name": "y", "kind": "scalar"}], "gp").mode == "off"


# --- the service, on a database ----------------------------------------------------


async def test_tuning_recorded_and_reproduced_on_refit(studies_engine, basics, tmp_path) -> None:
    svc = _service(tmp_path)

    async def _labels(_session: object, _alias: str) -> dict[str, str]:
        return {"y": "Twice x (mm)", "z": "x squared (mm2)"}

    svc._output_labels = _labels  # type: ignore[method-assign]
    factory = make_session_factory(studies_engine)
    progress: list[tuple[int, int, str]] = []
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        source_id = await _seed_source(session, basics)  # y = 2x, z = x²
        model, v1, _ = await svc.create_model(
            session,
            actor_user_id=basics.user_id,
            name="tuned",
            tool_alias=basics.tool_alias,
            scope="personal",
            source_kind="parametric",
            source_id=source_id,
            response_names=["y", "z"],
            surrogate_kind="gp_matern",
            tuning_mode="automatic",
            candidate_methods=["gp", "poly"],
            tuning_overrides={"y": {"method": "poly"}},
            progress=lambda d, t, m: progress.append((d, t, m)),
        )
        assert progress
        assert progress[-1][0] == progress[-1][1]
        # The progress dialog names results by label, never by internal name.
        messages = {m for _, _, m in progress}
        assert "Fitting 'Twice x (mm)' (1 of 2)" in messages
        assert not any("'y'" in m or "'z'" in m for m in messages)

        rows = {r["name"]: r for r in v1.responses}
        assert rows["y"]["method"] == "poly"
        assert rows["y"]["tuning_source"] == "user"
        assert rows["z"]["tuning_source"] in ("auto", "mode")
        assert set(rows["z"]["tuning_methods"]) == {"gp", "gp_matern", "poly"}
        assert v1.surrogate_method == "gp_matern"
        tuning = v1.quality["y"]["tuning"]
        assert tuning["baseline"]["method"] == "gp_matern"
        assert tuning["candidates"]
        assert v1.quality["y"]["method"] == "poly"

        bundle = load_scalar_bundle(Path(v1.bundle_path), v1.bundle_sha256)
        assert bundle["y"].kind == "poly"
        means, _ = bundle["y"].predict([{"x": 3.0}])
        assert means[0] == pytest.approx(6.0, rel=0.05)

        # Refit with no arguments: primary method, mode and the user's choice survive.
        v2 = await svc.refit(session, actor_user_id=basics.user_id, model_id=model.id)
        assert v2.surrogate_method == "gp_matern"
        rows2 = {r["name"]: r for r in v2.responses}
        assert rows2["y"]["method"] == "poly"
        assert rows2["y"]["tuning_source"] == "user"
        assert rows2["z"]["tuning_mode"] == "automatic"

        # The Refit dialog can switch tuning off; the user's own choice still stands.
        v3 = await svc.refit(
            session, actor_user_id=basics.user_id, model_id=model.id, tuning_mode="off"
        )
        rows3 = {r["name"]: r for r in v3.responses}
        assert rows3["z"]["method"] == "gp_matern"
        assert rows3["z"]["output_scale"] == "as_measured"
        assert rows3["y"]["method"] == "poly"


async def test_compare_carries_tuning_for_every_primary_and_rejects_bad_overrides(
    studies_engine, basics, tmp_path
) -> None:
    svc = _service(tmp_path)
    factory = make_session_factory(studies_engine)
    async with session_scope(factory) as session:
        await ensure_chain_head(session)
        source_id = await _seed_source(session, basics)
        per_method, best, _, _, tuning = await svc.compare_fits(
            session,
            actor_user_id=basics.user_id,
            source_kind="parametric",
            source_id=source_id,
            response_names=["y", "z"],
            methods=["gp", "poly"],
            tuning_mode="automatic",
        )
        assert best in ("gp", "poly")
        assert all(m["fitted"] for m in per_method)
        # The table ranks on what keeping a method gives after tuning, with the
        # before-tuning figures alongside — the two must agree with the decisions.
        for m in per_method:
            assert set(m["baseline_quality"]) == {"y", "z"}
            for name in ("y", "z"):
                decision = tuning[name]["decisions"][m["method"]]
                assert m["quality"][name]["rmse_cv"] == pytest.approx(decision["rmse"])
                assert m["baseline_quality"][name]["rmse_cv"] == pytest.approx(
                    decision["baseline_rmse"]
                )
        for name in ("y", "z"):
            assert set(tuning[name]["decisions"]) == {"gp", "poly"}
            assert tuning[name]["candidates"]
            # Every fit tried carries its held-out predictions, for the plot tickbox.
            fitted = [c for c in tuning[name]["candidates"] if c["rmse"] is not None]
            assert all(len(c["cv_actual"]) == len(c["cv_predicted"]) > 0 for c in fitted)
            for decision in tuning[name]["decisions"].values():
                assert decision["source"] == "auto"
                assert decision["warning"] is None

        with pytest.raises(ModelConfigError, match="isn't being fitted"):
            await svc.create_model(
                session,
                actor_user_id=basics.user_id,
                name="bad",
                tool_alias=basics.tool_alias,
                scope="personal",
                source_kind="parametric",
                source_id=source_id,
                response_names=["y"],
                tuning_mode="automatic",
                tuning_overrides={"nope": {"method": "gp"}},
            )
        with pytest.raises(ModelConfigError, match="isn't among the methods tried"):
            await svc.create_model(
                session,
                actor_user_id=basics.user_id,
                name="bad",
                tool_alias=basics.tool_alias,
                scope="personal",
                source_kind="parametric",
                source_id=source_id,
                response_names=["y"],
                tuning_mode="automatic",
                tuning_overrides={"y": ModelTuningChoice(method="gbm").model_dump()},
            )
