"""Form-validation errors with per-field plain-English messages.

The descriptor → Pydantic model factory in :mod:`matofx.tools.forms.model`
runs submitted job inputs through a per-tool Pydantic v2 model. When
validation fails, we translate Pydantic's technical errors (``"Input
should be greater than or equal to 0"``) into the friendly per-field
messages required by brief §F30 clarifier 3 ("Operating temperature
can't exceed the material's max safe temperature.") and surface them
via :class:`FormValidationError`.

The shape is deliberately small:

- :class:`FieldError` — one per offending input. Carries the input's
  programmatic ``name`` (so the UI knows which widget to flag), the
  human ``label`` (so any plain-text rendering reads naturally), and
  the friendly ``message``.
- :class:`FormValidationError` — wraps a list of :class:`FieldError`
  for ``raise`` -ing out of validation. ``str(exc)`` returns a one-line
  summary that is safe to log.

Sum-check errors (descriptor §F30 cross-field constraint) attach to
the **first** input listed in the ``sum_parameters`` so the UI has a
deterministic place to render them; the ``sum_parameters`` list is
preserved on the error so the renderer can also highlight the other
participants.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class FieldError:
    """One per-field validation error, ready to render under the input.

    ``name`` is the descriptor's programmatic identifier (``object_temperature``);
    ``label`` is the human label shown next to the field
    (``"Starting temperature"``); ``message`` is the plain-English
    explanation displayed inline.

    ``sum_parameters`` is non-empty only for sum-check failures and
    lists every input name the constraint involves — useful for the
    renderer to highlight the whole group rather than just the first
    member.
    """

    name: str
    label: str
    message: str
    sum_parameters: tuple[str, ...] = field(default_factory=tuple)


class FormValidationError(Exception):
    """One or more inputs failed validation.

    ``field_errors`` is the per-field list. ``str(self)`` summarises
    the first error plus a count of any others — short enough to land
    in a log line, never enough to leak input values.
    """

    def __init__(self, field_errors: list[FieldError]) -> None:
        """Construct with a non-empty list of field errors."""
        if not field_errors:
            raise ValueError("FormValidationError requires at least one FieldError")
        self.field_errors = list(field_errors)
        first = field_errors[0]
        if len(field_errors) == 1:
            super().__init__(first.message)
        else:
            super().__init__(
                f"{first.message} (and {len(field_errors) - 1} more issue"
                f"{'s' if len(field_errors) > 1 else ''})"
            )

    def by_name(self) -> dict[str, FieldError]:
        """Return ``{field_name: FieldError}`` for cheap renderer lookup.

        When two errors share a name (rare — usually only sum-checks
        and a per-field error coinciding) the **first** wins. The
        ordering of :attr:`field_errors` is preserved.
        """
        out: dict[str, FieldError] = {}
        for err in self.field_errors:
            out.setdefault(err.name, err)
        return out
