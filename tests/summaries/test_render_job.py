"""Slice 1 — the templated job narrative.

Pure (no DB): build :class:`SummaryFacts` by hand and pin the prose. The
load-bearing invariant — every number in the prose traces to a fact, and no
number appears that isn't in the facts — is what lets the enrichment guard
stack treat this text as ground truth.
"""

from __future__ import annotations

import re

from matofx.summaries import HeadlineNumber, SummaryFacts, render_job


def _facts(**overrides: object) -> SummaryFacts:
    base: dict[str, object] = {
        "surface_kind": "job",
        "surface_id": 7,
        "title": "reports-job",
        "state": "complete",
        "headline": (
            HeadlineNumber("Peak temperature", 91.3, "°C"),
            HeadlineNumber("Mass", 0.182, "kg"),
        ),
        "duration_seconds": 252.0,
        "details": {
            "tool_label": "Heat sink",
            "exit_code": 0,
            "output_file_count": 3,
            "output_scalar_count": 2,
        },
    }
    base.update(overrides)
    return SummaryFacts(**base)  # type: ignore[arg-type]


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


def test_complete_job_prose() -> None:
    text = render_job(_facts())
    joined = " ".join(text.paragraphs)
    assert "Heat sink job completed successfully in 4 min 12 s" in joined
    assert "peak temperature 91.3 °C" in joined
    assert "mass 0.182 kg" in joined
    assert "3 output files and 2 scalar results" in joined


def test_every_number_in_prose_traces_to_a_fact() -> None:
    """The grounding invariant — no fabricated figures in the baseline."""
    text = render_job(_facts())
    prose_numbers = _numbers(" ".join(text.paragraphs))
    fact_numbers = {"91.3", "0.182", "3", "2"} | {"4", "12"}  # + duration tokens
    assert prose_numbers <= fact_numbers


def test_failed_job_leads_with_failure_and_exit_code() -> None:
    text = render_job(
        _facts(
            state="failed",
            headline=(),
            details={"tool_label": "Heat transfer", "exit_code": 1},
        )
    )
    joined = " ".join(text.paragraphs)
    assert "Heat transfer job failed after 4 min 12 s (exit code 1)" in joined
    assert "No headline results were produced" in joined
    assert "completed successfully" not in joined


def test_cancelled_job() -> None:
    text = render_job(_facts(state="cancelled", headline=()))
    assert "was cancelled after 4 min 12 s" in " ".join(text.paragraphs)


def test_complete_without_kpis_omits_headline_clause() -> None:
    text = render_job(_facts(headline=()))
    joined = " ".join(text.paragraphs)
    assert "Headline results" not in joined
    assert "completed successfully" in joined


def test_singular_output_tally() -> None:
    text = render_job(
        _facts(
            details={
                "tool_label": "X",
                "exit_code": 0,
                "output_file_count": 1,
                "output_scalar_count": 1,
            }
        )
    )
    assert "1 output file and 1 scalar result" in " ".join(text.paragraphs)


def test_missing_output_counts_omits_tally() -> None:
    text = render_job(_facts(details={"tool_label": "Heat sink", "exit_code": 0}))
    assert "produced" not in " ".join(text.paragraphs)
