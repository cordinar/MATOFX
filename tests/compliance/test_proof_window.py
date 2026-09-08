"""Which consistency proofs a report carries when it cannot carry all.

The vendor anchors against whichever tree size their ledger holds, and
the install cannot know which one that is — a report may have been
previewed and never sent. So when the proof list has to be thinned, the
*shape* of the loss decides who becomes unverifiable.

Until 2026-08-12 the report kept the most recent `PROOF_CAP` sizes and
dropped the oldest. That is backwards: a customer who delivered report 3
and then generated 200 previews would drop the very size the vendor was
holding, and every later report would verify `INCONCLUSIVE` with no way
back. Safe — it never accuses — but it silently stops proving anything,
which is the same practical dead end F1 produced by a different route.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from matofx.compliance.schema import PROOF_CAP, select_proof_sizes


def test_everything_is_kept_below_the_cap() -> None:
    sizes = list(range(1, 51))
    assert select_proof_sizes(sizes, PROOF_CAP) == sizes


def test_exactly_at_the_cap_keeps_everything() -> None:
    sizes = list(range(1, PROOF_CAP + 1))
    assert select_proof_sizes(sizes, PROOF_CAP) == sizes


@pytest.mark.parametrize("count", [101, 250, 1000, 5000])
def test_the_oldest_is_never_dropped(count: int) -> None:
    """The case the old behaviour got wrong.

    A ledger holding an early entry belongs to a customer who has
    delivered little — exactly the one who most needs to be able to
    prove anything at all.
    """
    sizes = list(range(1, count + 1))
    kept = select_proof_sizes(sizes, PROOF_CAP)
    assert kept[0] == sizes[0]


@pytest.mark.parametrize("count", [101, 250, 1000, 5000])
def test_the_newest_is_never_dropped(count: int) -> None:
    """The vendor's most recent delivery is the likeliest single match."""
    sizes = list(range(1, count + 1))
    kept = select_proof_sizes(sizes, PROOF_CAP)
    assert kept[-1] == sizes[-1]


@pytest.mark.parametrize("count", [101, 250, 1000, 5000])
def test_the_cap_is_respected(count: int) -> None:
    """The cap exists to bound report size; it has to actually bound it."""
    kept = select_proof_sizes(list(range(1, count + 1)), PROOF_CAP)
    assert len(kept) <= PROOF_CAP


def test_the_result_is_sorted_and_unique() -> None:
    kept = select_proof_sizes(list(range(1, 1000)), PROOF_CAP)
    assert kept == sorted(set(kept))


def test_gaps_are_spread_not_concentrated() -> None:
    """Thinning must not chop one end off.

    With 1000 sizes thinned to 100, an even stride leaves gaps of about
    10. A strategy that kept a contiguous run would leave one enormous
    gap instead, which is the failure being fixed — so assert no single
    gap dominates.
    """
    kept = select_proof_sizes(list(range(1, 1001)), PROOF_CAP)
    gaps = [later - earlier for earlier, later in pairwise(kept)]
    # An even spread over 1000 with 100 kept is ~10 per gap. Allow slack
    # for the endpoints, but nothing like a 500-wide hole.
    assert max(gaps) <= 30, f"one gap dominates the spread: {max(gaps)}"


def test_a_single_slot_keeps_the_newest() -> None:
    """Degenerate, but it must not raise or return the wrong end."""
    assert select_proof_sizes([10, 20, 30], 1) == [30]


def test_two_slots_keep_both_ends() -> None:
    assert select_proof_sizes([10, 20, 30, 40], 2) == [10, 40]


def test_a_zero_cap_returns_nothing_rather_than_raising() -> None:
    assert select_proof_sizes([1, 2, 3], 0) == []


def test_an_empty_history_is_not_an_error() -> None:
    """A first report has no past sizes to prove from."""
    assert select_proof_sizes([], PROOF_CAP) == []
