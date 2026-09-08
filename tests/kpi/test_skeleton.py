"""Slice 0 smoke tests for the KPI package skeleton.

Verifies the package imports, the error base + pre-declared subclasses
land, and the UI helper safely defaults to ``False`` when no app is
mounted. The flag-mechanism tests live in
``tests/features/test_features.py``; this file covers the *package*
side specifically.
"""

from __future__ import annotations

import pytest

from matofx.kpi import (
    KpiError,
    KpiExtractionFailedError,
    KpiNotDeclaredError,
    KpiReextractInProgressError,
)
from matofx.ui._features import kpi_extraction_enabled


def test_package_importable() -> None:
    """The ``matofx.kpi`` package imports cleanly."""
    import matofx.kpi  # noqa: PLC0415

    assert matofx.kpi is not None


def test_error_base_class() -> None:
    """``KpiError`` is the umbrella exception for the domain."""
    assert issubclass(KpiError, Exception)
    with pytest.raises(KpiError, match="boom"):
        raise KpiError("boom")


def test_pre_declared_errors_subclass_kpi_error() -> None:
    """Every public KPI error subclasses :class:`KpiError`.

    The umbrella shape lets the API exception handler register one
    handler for the domain — mirrors the V&V / library / materials
    convention.
    """
    assert issubclass(KpiExtractionFailedError, KpiError)
    assert issubclass(KpiNotDeclaredError, KpiError)
    assert issubclass(KpiReextractInProgressError, KpiError)


def test_ui_helper_defaults_false_when_no_app() -> None:
    """``kpi_extraction_enabled`` returns ``False`` when no app is mounted.

    The safe direction (hide surfaces) when settings can't be
    resolved — matches every other ``ui._features`` helper.
    """
    assert kpi_extraction_enabled() is False
