"""Tests for the sandboxed expression evaluator (§16.16 / §F30)."""

from __future__ import annotations

import pytest

from matofx.tools.forms import safe_expr
from matofx.tools.forms.safe_expr import (
    UnsafeExpressionError,
    evaluate,
    evaluate_condition,
    parse,
    referenced_names,
)

# --- Allowed grammar ------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "names", "expected"),
    [
        ("a == 1", {"a": 1}, True),
        ("a == 1", {"a": 2}, False),
        ("a + b + c == 100", {"a": 30, "b": 30, "c": 40}, True),
        ("a == 1 and b == 2", {"a": 1, "b": 2}, True),
        ("a == 1 and b == 2", {"a": 1, "b": 9}, False),
        (
            "a == 1 and (b == 1 or c == 1 or d == 1)",
            {"a": 1, "b": 0, "c": 1, "d": 0},
            True,
        ),
        (
            "a == 1 and (b == 1 or c == 1 or d == 1)",
            {"a": 1, "b": 0, "c": 0, "d": 0},
            False,
        ),
        ("not done", {"done": False}, True),
        ("grade in ['A', 'B']", {"grade": "B"}, True),
        ("grade not in ['A', 'B']", {"grade": "C"}, True),
        ("0 <= a <= 100", {"a": 50}, True),
        ("0 <= a <= 100", {"a": 150}, False),
        ("len(items) > 2", {"items": [1, 2, 3]}, True),
        ("'x' in tags", {"tags": ["x", "y"]}, True),
        ("-a < 0", {"a": 5}, True),
        ("a / b > 1", {"a": 10, "b": 2}, True),
    ],
)
def test_allowed_expressions(expr: str, names: dict[str, object], expected: bool) -> None:
    assert evaluate_condition(expr, names) is expected


def test_evaluate_returns_natural_value() -> None:
    # `and` / `or` keep Python truthiness semantics for constraint reuse.
    assert evaluate("a + b", {"a": 2, "b": 3}) == 5
    assert evaluate("a or b", {"a": 0, "b": 7}) == 7


# --- Rejected grammar -----------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "a.foo",  # attribute access
        "a['k']",  # subscript
        "__import__('os')",  # import via call
        "open('x')",  # disallowed call
        "(lambda: 1)()",  # lambda
        "[x for x in y]",  # comprehension
        "f'{a}'",  # f-string
        "a ** 2",  # power operator
        "a % 2",  # modulo
        "a // 2",  # floor div
        "a & b",  # bitwise
        "a if b else c",  # ternary
        "{'k': 1}",  # dict literal
        "{1, 2}",  # set literal
        "a := 1",  # walrus
        "",  # empty
        "   ",  # whitespace only
        "a ==",  # syntax error
    ],
)
def test_rejected_expressions(expr: str) -> None:
    with pytest.raises(UnsafeExpressionError):
        parse(expr)
    # evaluate_condition also surfaces grammar errors (install-time bug,
    # not a data-shaped one).
    with pytest.raises(UnsafeExpressionError):
        evaluate_condition(expr, {"a": 1, "b": 2, "c": 3, "y": []})


def test_node_cap_enforced() -> None:
    # A deeply chained boolean blows the node cap and is refused.
    huge = " or ".join(["a == 1"] * (safe_expr.MAX_AST_NODES))
    with pytest.raises(UnsafeExpressionError):
        parse(huge)


# --- Data-shaped failure → condition is False, never raises ---------------


@pytest.mark.parametrize(
    ("expr", "names"),
    [
        ("a < 1", {"a": None}),  # None ordering
        ("a + 1 == 2", {"a": None}),  # None arithmetic
        ("a / b > 0", {"a": 1, "b": 0}),  # divide by zero
        ("len(a) > 0", {"a": None}),  # len of None
        ("a in b", {"a": 1, "b": None}),  # membership in None
        ("missing == 1", {}),  # unknown name
    ],
)
def test_condition_false_on_data_failure(expr: str, names: dict[str, object]) -> None:
    assert evaluate_condition(expr, names) is False


def test_evaluate_is_strict_on_unknown_name() -> None:
    with pytest.raises(NameError):
        evaluate("ghost == 1", {})


# --- referenced_names -----------------------------------------------------


def test_referenced_names_excludes_len_callable() -> None:
    tree = parse("len(items) > n and flag")
    assert referenced_names(tree) == {"items", "n", "flag"}


def test_referenced_names_collects_from_containers() -> None:
    tree = parse("grade in [a, b] and c == 1")
    assert referenced_names(tree) == {"grade", "a", "b", "c"}
