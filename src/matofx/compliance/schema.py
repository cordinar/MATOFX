"""Report shape: versions, licence-type usage blocks, event catalog (ADR-0019).

The report is a *document*, not a row, and Funis parses it with a
different program (possibly a different version) from the one that wrote
it. So the shape is pinned here, versioned, and deliberately stable
across the seven build slices: report 1 (before any licence exists) and
report 40 (full CSL enforcement) carry the same keys, with nulls where a
figure isn't knowable yet. A verifier never has to branch on "did this
install have slice 4 yet?" — only on ``licence_type``.

## Both licence types are first-class

§C16 defines two licence types and an install runs exactly one:

- **CSL** — N *concurrent sessions*; a slot is consumed at login and
  released at logout / idle timeout / hard timeout / revocation. The
  binding figure is **peak concurrency** over the period.
- **FAL** — N *access-rights assignments*; a slot is consumed when an
  admin grants the floating right and released when they revoke it. The
  binding figure is **assignments held**, which moves only on admin
  action.

They measure genuinely different things, so the report carries a
discriminated block per type rather than one blurred "seats" number. The
block matching ``licence_type`` is *authoritative*; the other is still
emitted as an **observation**, because both are cheap to compute and a
customer who switches licence type mid-term leaves a coherent history
behind them.

Before the licence subsystem lands (slice 2), ``licence_type`` is
``None`` and both blocks are observations — the report is still
anchoring evidence, it just cannot assert a breach.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Literal

REPORT_SCHEMA_VERSION: Final[int] = 2
"""Bumped whenever the document shape or the canonical-bytes recipe changes.

- **v1** — anchoring compared a self-reported previous head against the
  vendor's ledger. An undelivered report made the two disagree for ever.
- **v2** — anchoring is an RFC 6962 consistency proof from every past
  report's tree size (``anchor.tree``, ``anchor.consistency_proofs``),
  which is checkable rather than asserted and is indifferent to reports
  that were generated but never sent.
"""

PROOF_CAP: Final[int] = 100
"""How many past tree sizes to carry consistency proofs for.

Each proof is ``O(log n)``, so at the agreed one-or-two reports a year
this covers decades. An install that generates many ad-hoc previews can
still exceed it — every report writes its own audit event, so no two
reports share a tree size and the list only grows.

**Which ones are dropped matters more than how many.** Until 2026-08-12
this kept the *most recent* ``PROOF_CAP`` and dropped the oldest, which
is precisely backwards: the vendor anchors against whichever size their
ledger holds, and a ledger holding an old entry is exactly the case that
needs proving — a customer who delivered report 3 and then previewed 200
times would become permanently unanchorable. Selection is now spread by
:func:`select_proof_sizes`, which always keeps the oldest and the
newest, and `matofx compliance report --prove-from` covers any size the
spread happens to miss. That is reported as
``consistency_proofs_truncated`` and degrades to "cannot verify", never
to a false accusation.
"""

LicenceType = Literal["csl", "fal"]

CSL: Final[str] = "csl"
FAL: Final[str] = "fal"


#: Audit actions the report counts, grouped by the slice that starts
#: emitting them. Counting them from slice 1 (all zero) keeps the
#: artefact's shape stable, and means a *missing* key in a later report
#: is a signal rather than a schema difference.
LICENCE_EVENT_ACTIONS: Final[tuple[str, ...]] = (
    # Slice 2 — manifest lifecycle.
    "licence.installed",
    "licence.validated.ok",
    "licence.validated.failed",
    # Slice 4 — CSL concurrency.
    "licence.slot.granted",
    "licence.slot.denied",
    "licence.slot.breakglass",
    "licence.slot.released",
    # Slice 5 — FAL assignments.
    "licence.assignment.granted",
    "licence.assignment.revoked",
    "licence.assignment.denied",
    # Slice 6 — expiry / read-only transitions.
    "licence.expiry.transition",
    "licence.readonly.entered",
    "licence.readonly.left",
    # `licence.override` was carried here as RESERVED and never emitted.
    # Removed 2026-08-12: `event_counts` zero-fills the catalog, so every
    # report stated `"licence.override": 0` — a permanently-zero counter
    # for a concept this build does not have. To a vendor reading the
    # report that is not neutral, it reads as "no overrides occurred"
    # when overrides *cannot* occur, which is a false assurance about the
    # one thing the report exists to attest. The two things §C16 could
    # mean by "admin overrides" already have their own events below and
    # above (`licence.slot.breakglass`, `audit.verify.acknowledged`).
    # Adding an action later is additive and needs no placeholder.
    # Chain integrity (ADR-0019 §C14) — a failed verification degrades
    # the install to read-only; the Super User acknowledgement that
    # lifts it is itself evidence, so both are counted.
    "audit.verify.failed",
    "audit.verify.acknowledged",
    # Slice 1 — the reports themselves. A regenerated or withheld
    # period is visible here as well as in the counter.
    "compliance.report.generated",
)

#: Actions serious enough to list individually (with actor + reason), not
#: merely count. Kept short: the report must stay a licence attestation,
#: not an audit-log extract (ADR-0019 §B5).
NOTABLE_EVENT_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "licence.slot.breakglass",
        "licence.validated.failed",
        "licence.expiry.transition",
        "audit.verify.failed",
        "audit.verify.acknowledged",
    }
)

#: Cap on individually-listed events, so one pathological month can't
#: produce a 40 MB report. Truncation is reported explicitly in the
#: document (``notable_events_truncated``) — never silently.
NOTABLE_EVENT_CAP: Final[int] = 100


@dataclass(frozen=True)
class CslUsage:
    """Concurrent-session figures for the reporting period.

    ``peak_concurrent_sessions`` is computed from overlapping
    ``auth_session`` intervals, which is why it is meaningful from slice
    1 — long before slot enforcement exists. Everything else counts
    chain events and stays at zero until slice 4.
    """

    peak_concurrent_sessions: int
    sessions_started: int
    slots_granted: int
    slots_denied: int
    breakglass_admissions: int
    peak_by_day: dict[str, int]

    def to_dict(self, *, n_slots: int | None) -> dict[str, Any]:
        """Render as the report's ``usage.csl`` block."""
        return {
            "n_slots": n_slots,
            "peak_concurrent_sessions": self.peak_concurrent_sessions,
            "over_cap": None if n_slots is None else self.peak_concurrent_sessions > n_slots,
            "sessions_started": self.sessions_started,
            "slots_granted": self.slots_granted,
            "slots_denied": self.slots_denied,
            "breakglass_admissions": self.breakglass_admissions,
            "peak_by_day": dict(sorted(self.peak_by_day.items())),
        }


@dataclass(frozen=True)
class FalUsage:
    """Floating-access-assignment figures for the reporting period.

    ``source`` says where ``assignments_current`` came from, because the
    answer changes at slice 5 and a verifier must not silently compare
    unlike numbers:

    - ``enabled_accounts`` — pre-slice-5 proxy. Under FAL semantics a
      user who can log in is exactly a user holding the right, so the
      count of active accounts *is* the assignment count; it simply
      isn't yet enforced against a cap.
    - ``fal_assignments`` — the real thing, once the floating right exists.
    """

    assignments_current: int
    assignments_granted: int
    assignments_revoked: int
    assignments_denied: int
    registered_users: int
    source: Literal["enabled_accounts", "fal_assignments"]

    def to_dict(self, *, n_slots: int | None) -> dict[str, Any]:
        """Render as the report's ``usage.fal`` block."""
        return {
            "n_slots": n_slots,
            "assignments_current": self.assignments_current,
            "over_cap": None if n_slots is None else self.assignments_current > n_slots,
            "assignments_granted": self.assignments_granted,
            "assignments_revoked": self.assignments_revoked,
            "assignments_denied": self.assignments_denied,
            "registered_users": self.registered_users,
            "source": self.source,
        }


@dataclass(frozen=True)
class UsageBlock:
    """Both licence types' figures, with the authoritative one named."""

    licence_type: LicenceType | None
    n_slots: int | None
    csl: CslUsage
    fal: FalUsage

    def to_dict(self) -> dict[str, Any]:
        """Render as the report's ``usage`` block.

        The cap is applied only to the authoritative block — asserting
        ``over_cap`` against a licence type the install isn't running
        would be a false breach.
        """
        return {
            "licence_type": self.licence_type,
            "authoritative": self.licence_type,
            "csl": self.csl.to_dict(n_slots=self.n_slots if self.licence_type == CSL else None),
            "fal": self.fal.to_dict(n_slots=self.n_slots if self.licence_type == FAL else None),
        }


def select_proof_sizes(sizes: list[int], cap: int = PROOF_CAP) -> list[int]:
    """Choose at most ``cap`` tree sizes to carry proofs for.

    ``sizes`` is every distinct size this install has ever attested, in
    ascending order. Returns a subset in the same order.

    The vendor anchors against whichever size their ledger holds, and the
    install cannot know which one that is — a report may have been
    previewed and never sent. So when the list must be thinned, the shape
    of the loss matters:

    * **The oldest is always kept.** A ledger that has received little is
      holding an early entry, and that is the customer most in need of
      being able to prove anything at all.
    * **The newest is always kept**, since the vendor's most recent
      delivery is the most likely single match.
    * The middle is thinned by an even stride rather than chopped, so the
      gaps are spread across the history instead of concentrated at one
      end.

    Anything the spread misses is still reachable: ``matofx compliance
    report --prove-from <size>`` produces a report proving from exactly
    the size a vendor asks about.
    """
    if cap <= 0:
        return []
    if len(sizes) <= cap:
        return list(sizes)
    if cap == 1:
        return [sizes[-1]]

    # Reserve the two endpoints, then stride evenly through the interior.
    interior_wanted = cap - 2
    interior = sizes[1:-1]
    if interior_wanted <= 0:
        return [sizes[0], sizes[-1]]

    step = len(interior) / interior_wanted
    picked = {sizes[0], sizes[-1]}
    for index in range(interior_wanted):
        picked.add(interior[int(index * step)])
    return sorted(picked)
