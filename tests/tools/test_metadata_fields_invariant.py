"""Guard: the no-op diff must track every wizard-editable descriptor field.

``ToolService.update_descriptor`` skips persisting an edit when
:func:`matofx.tools.versioning.diff_descriptor` finds no change among
``METADATA_FIELDS``. That tuple is hand-maintained. If a new
descriptor-body field is added to the Edit-Tool DTO
(:class:`AdminToolDescriptorUpdate`) but *not* to ``METADATA_FIELDS``,
an edit that changes only that field diffs to nothing and is silently
discarded — the Save button looks like it did nothing. That was the
original "conditional dependency rules don't save" bug
(``dependencies`` / ``cross_field_constraints`` were missing from the
tuple).

This test fails loudly the moment the two drift apart, so the trap
can't silently re-arm when someone adds the next wizard section.
"""

from __future__ import annotations

from matofx.api.schemas import AdminToolDescriptorUpdate
from matofx.tools.versioning import METADATA_FIELDS


def test_every_optional_descriptor_body_field_is_diff_tracked() -> None:
    """Every None-defaulted DTO field (the overlay block) must be diffed.

    The edit DTO's required/metadata fields are required, have scalar
    defaults, or use ``default_factory``; only the optional
    descriptor-body overlay fields default to ``None``. Each of those,
    if it is the *only* thing an edit changes, must register in the
    no-op diff or the change is thrown away.
    """
    overlay_fields = {
        name
        for name, info in AdminToolDescriptorUpdate.model_fields.items()
        if info.default is None
    }
    # Sanity-check the selector still picks the block we expect — if a
    # refactor changes the DTO so this set goes empty, the assertion
    # below would vacuously pass and the guard would be useless.
    assert {"dependencies", "cross_field_constraints", "inputs"} <= overlay_fields

    missing = sorted(overlay_fields - set(METADATA_FIELDS))
    assert not missing, (
        "These wizard-editable descriptor fields are accepted by "
        "AdminToolDescriptorUpdate but are missing from "
        f"versioning.METADATA_FIELDS: {missing}. An edit that changes "
        "only one of them will be silently dropped as a no-op. Add "
        "them to METADATA_FIELDS in src/matofx/tools/versioning.py."
    )
