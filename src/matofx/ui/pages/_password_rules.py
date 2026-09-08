"""Live password-policy hints rendered under password fields.

Three forms touch user passwords — admin "new user", admin "edit user",
and the self-service "change password" card on the account page. They
all share the same UX: under the new-password field, a small list of
rule lines flips from red ✗ to green ✓ as each rule is satisfied; under
the re-type field, a "Passwords match" line does the same. Save / Update
stays disabled until every visible rule passes.

This module is the shared rendering primitive. Each form calls
:func:`render_password_rule_list` once for the new-password field and
:func:`render_password_match_indicator` once for the re-type field,
then wires both returned bindings into its own button-gating callback.

Rule selection mirrors :func:`matofx.users.passwords.validate_password_complexity`:
the minimum-length and allowed-alphabet checks always render; the four
``password_require_*`` toggles each contribute a rule **only when
enabled** in General Settings, so the list shrinks or grows as an admin
tightens or relaxes the policy without any UI change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nicegui import ui

_RED = "#c0392b"
_GREEN = "#27ae60"


@dataclass(frozen=True)
class _Rule:
    label_text: str
    predicate: Callable[[str], bool]


def _at_least(n: int) -> Callable[[str], bool]:
    """A predicate asserting the password is at least ``n`` characters."""

    def check(pw: str) -> bool:
        return len(pw) >= n

    return check


def _contains_any(allowed: set[str]) -> Callable[[str], bool]:
    """A predicate asserting the password contains one of ``allowed``."""

    def check(pw: str) -> bool:
        return any(c in allowed for c in pw)

    return check


def _build_rules(snapshot: dict[str, Any]) -> list[_Rule]:
    """Build the active rule list from a General Settings snapshot.

    Rules whose ``require_*`` toggle is off in ``snapshot`` are omitted
    entirely (per the agreed UX: don't surface non-rules as muted
    placeholders). The min-length and allowed-alphabet checks always
    render because both apply unconditionally on the server.
    """
    rules: list[_Rule] = []

    min_len_raw = snapshot.get("password_min_length", 12)
    min_len = (
        int(min_len_raw)
        if isinstance(min_len_raw, int) and not isinstance(min_len_raw, bool)
        else 12
    )
    rules.append(
        _Rule(
            label_text=f"At least {min_len} characters",
            predicate=_at_least(min_len),
        )
    )

    if bool(snapshot.get("password_require_uppercase", False)):
        rules.append(
            _Rule(
                label_text="Contains an uppercase letter (A-Z)",
                predicate=lambda pw: any("A" <= c <= "Z" for c in pw),
            )
        )
    if bool(snapshot.get("password_require_lowercase", False)):
        rules.append(
            _Rule(
                label_text="Contains a lowercase letter (a-z)",
                predicate=lambda pw: any("a" <= c <= "z" for c in pw),
            )
        )
    if bool(snapshot.get("password_require_digit", False)):
        rules.append(
            _Rule(
                label_text="Contains a digit (0-9)",
                predicate=lambda pw: any("0" <= c <= "9" for c in pw),
            )
        )

    allowed_symbols = str(snapshot.get("password_allowed_symbols", ""))
    allowed_set = set(allowed_symbols)
    if bool(snapshot.get("password_require_symbol", False)):
        rules.append(
            _Rule(
                label_text=f"Contains an allowed symbol ({allowed_symbols})",
                predicate=_contains_any(allowed_set),
            )
        )

    def _only_allowed(pw: str, allowed: set[str] = allowed_set) -> bool:
        for ch in pw:
            if "A" <= ch <= "Z" or "a" <= ch <= "z" or "0" <= ch <= "9":
                continue
            if ch in allowed:
                continue
            return False
        return True

    rules.append(
        _Rule(
            label_text="Uses only letters, digits, and allowed symbols",
            predicate=_only_allowed,
        )
    )
    return rules


@dataclass
class PasswordRulesBinding:
    """Handle returned by :func:`render_password_rule_list`.

    Consumers call :meth:`on_change` to register a refresh callback
    (typically a function that re-evaluates whether the Save button
    should be enabled), and :meth:`valid` / :meth:`relevant` from
    inside that callback.

    ``relevant()`` is the "do the rules currently matter?" gate —
    edit-user passes ``show_when_empty=False`` so an admin who isn't
    changing the password sees no hints and gets a still-enabled
    Save button. Other consumers leave it ``True`` and get the rules
    visible from first render.
    """

    password_input: Any
    rules: list[_Rule]
    container: Any
    show_when_empty: bool = True
    on_change_callbacks: list[Callable[[], None]] = field(default_factory=list)

    def on_change(self, cb: Callable[[], None]) -> None:
        self.on_change_callbacks.append(cb)

    def valid(self) -> bool:
        pw = str(self.password_input.value or "")
        return all(rule.predicate(pw) for rule in self.rules)

    def relevant(self) -> bool:
        if self.show_when_empty:
            return True
        return bool(self.password_input.value)


@dataclass
class PasswordMatchBinding:
    """Handle returned by :func:`render_password_match_indicator`."""

    pw_a: Any
    pw_b: Any
    label: Any
    show_when_empty: bool = True
    on_change_callbacks: list[Callable[[], None]] = field(default_factory=list)

    def on_change(self, cb: Callable[[], None]) -> None:
        self.on_change_callbacks.append(cb)

    def valid(self) -> bool:
        a = str(self.pw_a.value or "")
        b = str(self.pw_b.value or "")
        return bool(a) and a == b

    def relevant(self) -> bool:
        if self.show_when_empty:
            return True
        return bool(self.pw_a.value) or bool(self.pw_b.value)


def render_password_rule_list(
    password_input: Any,
    snapshot: dict[str, Any],
    *,
    show_when_empty: bool = True,
) -> PasswordRulesBinding:
    """Render a live rule list under ``password_input``.

    The list updates on every keystroke (``update:model-value``); each
    line flips green when its rule fires. ``show_when_empty=False``
    hides the whole list while the field is blank — used by the admin
    edit-user form, where a blank password means "don't change it".
    """
    rules = _build_rules(snapshot)
    with (
        ui.column()
        .classes("w-full gap-0")
        .style("margin-top: -4px; margin-bottom: 4px;") as container
    ):
        rows: list[ui.label] = [
            ui.label("").style("font-size: 0.8rem; min-height: 1.1em;") for _ in rules
        ]

    binding = PasswordRulesBinding(
        password_input=password_input,
        rules=rules,
        container=container,
        show_when_empty=show_when_empty,
    )

    def _refresh() -> None:
        pw = str(password_input.value or "")
        visible = show_when_empty or bool(pw)
        container.set_visibility(visible)
        if visible:
            for rule, row in zip(rules, rows, strict=True):
                ok = rule.predicate(pw)
                row.text = f"{'✓' if ok else '✗'}  {rule.label_text}"
                row.style(f"color: {_GREEN if ok else _RED}; font-size: 0.8rem; min-height: 1.1em;")
        for cb in binding.on_change_callbacks:
            cb()

    _refresh()
    # NB: ``.on("update:model-value", ...)`` fires before NiceGUI syncs
    # the element's ``.value`` attribute, so a handler that reads
    # ``password_input.value`` sees the *previous* keystroke and the
    # rule list lags one character behind. ``on_value_change`` runs
    # after the sync, so it reads the just-typed character.
    password_input.on_value_change(lambda _e=None: _refresh())
    return binding


def render_password_match_indicator(
    pw_a: Any,
    pw_b: Any,
    *,
    show_when_empty: bool = True,
) -> PasswordMatchBinding:
    """Render a "Passwords match" line bound to two password inputs.

    Green when both fields are non-empty AND equal; red otherwise.
    ``show_when_empty=False`` hides the line while both fields are
    blank (used by the admin edit-user form so a no-change save stays
    clean).
    """
    label = ui.label("").style("font-size: 0.8rem; min-height: 1.1em;")
    binding = PasswordMatchBinding(
        pw_a=pw_a, pw_b=pw_b, label=label, show_when_empty=show_when_empty
    )

    def _refresh() -> None:
        a = str(pw_a.value or "")
        b = str(pw_b.value or "")
        any_filled = bool(a) or bool(b)
        visible = show_when_empty or any_filled
        label.set_visibility(visible)
        if visible:
            matches = bool(a) and a == b
            label.text = f"{'✓' if matches else '✗'}  Passwords match"
            label.style(
                f"color: {_GREEN if matches else _RED}; font-size: 0.8rem; min-height: 1.1em;"
            )
        for cb in binding.on_change_callbacks:
            cb()

    _refresh()
    # See note in render_password_rule_list — on_value_change runs after
    # NiceGUI has synced .value, so the read inside _refresh is current.
    pw_a.on_value_change(lambda _e=None: _refresh())
    pw_b.on_value_change(lambda _e=None: _refresh())
    return binding
