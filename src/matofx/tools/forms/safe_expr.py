"""Sandboxed expression evaluator for descriptor cross-field logic.

A single, owned implementation of the fixed expression grammar locked
in PROJECT_BRIEF §F30 clarifier 1 (see the 2026-05-16 supersede note:
the evaluator is this module, not the ``simpleeval`` package). It backs
both:

- §F30 cross-field *constraints* (``a + b + c == 100``), and
- §16.16 conditional-dependency rule *conditions*
  (``mode == 'auto' and (a == 1 or b == 1)``).

## Grammar

Allowed, and nothing else:

- Literals: ``str`` / ``int`` / ``float`` / ``bool`` / ``None``.
- Names — resolve to the submitted value of a sibling input.
- Arithmetic: ``+ - * /`` (binary) and unary ``+`` / ``-``.
- Comparison: ``== != < <= > >=`` plus ``in`` / ``not in``
  (chained comparisons like ``0 <= a <= 100`` work as in Python).
- Boolean: ``and`` / ``or`` / ``not``.
- ``len(x)`` — the *only* permitted call.
- List / tuple literals — so ``grade in ['A', 'B']`` works.

Explicitly refused (raise :class:`UnsafeExpressionError` at parse time):
attribute access, subscripting, any call other than ``len``, lambdas,
comprehensions / generators, f-strings, walrus, ternary, starred args,
dict / set literals, imports, ``**`` / ``%`` / ``//`` / bitwise ops, and
every statement form. No builtins are reachable; no Python object can be
named that the caller did not put in the values mapping.

## Two entry points

- :func:`parse` — validate-only. Used by the descriptor install-time
  lint so a malformed ``when`` / constraint is refused at install,
  never at submit. Raises :class:`UnsafeExpressionError`
  (a :class:`ValueError`, so it composes with Pydantic validators).
- :func:`evaluate` — strict evaluation; raises on bad types / unknown
  names. Used where a hard failure is the right outcome.
- :func:`evaluate_condition` — the safe boolean wrapper used by the
  live form + submit resolver: a transient type mismatch (a numeric
  field still ``None`` because the user has not filled it yet, a
  divide-by-zero, an unexpectedly-missing name) makes the *whole
  condition* evaluate ``False`` rather than crashing the form. A rule
  simply does not fire until its inputs are well-typed.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "MAX_AST_NODES",
    "UnsafeExpressionError",
    "evaluate",
    "evaluate_condition",
    "parse",
    "referenced_names",
]


class UnsafeExpressionError(ValueError):
    """Expression uses syntax outside the locked grammar.

    Subclasses :class:`ValueError` so a descriptor ``@model_validator``
    can let it propagate and Pydantic wraps it into a
    :class:`pydantic.ValidationError` like every other schema problem.
    """


MAX_AST_NODES: Final = 200
"""Upper bound on nodes in one expression.

A descriptor is author-controlled, not end-user input, so this is a
defence-in-depth sanity bound (a pathologically deep expression can't
wedge the install-time walk), not a security boundary. Real cross-field
rules are a handful of nodes; 200 is far above any honest expression.
"""

# Node/operator allow-lists. Anything not present here is refused.
_ALLOWED_NODES: Final[tuple[type[ast.AST], ...]] = (
    ast.Expression,
    ast.BoolOp,
    ast.UnaryOp,
    ast.BinOp,
    ast.Compare,
    ast.Name,
    ast.Load,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Call,
    ast.And,
    ast.Or,
    ast.Not,
    ast.UAdd,
    ast.USub,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.In,
    ast.NotIn,
)
_ALLOWED_CONSTANT_TYPES: Final[tuple[type, ...]] = (str, int, float, bool, type(None))


def parse(expression: str) -> ast.Expression:
    """Parse and grammar-check ``expression``; return its AST.

    Does not evaluate. This is the install-time lint surface: a
    descriptor whose ``when`` / constraint string fails here is
    rejected at tool install, so a bad expression can never reach a
    running submit.

    Args:
        expression: The source string from the descriptor.

    Returns:
        The validated :class:`ast.Expression` (mode ``"eval"``).

    Raises:
        UnsafeExpressionError: The string is not syntactically valid
            Python, is empty, exceeds :data:`MAX_AST_NODES`, or uses a
            construct outside the locked grammar.
    """
    if not expression or not expression.strip():
        raise UnsafeExpressionError("Expression is empty.")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpressionError(f"Expression isn't valid: {exc.msg}.") from exc

    for node_count, node in enumerate(ast.walk(tree), start=1):
        if node_count > MAX_AST_NODES:
            raise UnsafeExpressionError(f"Expression is too complex (>{MAX_AST_NODES} nodes).")
        _check_node(node)
    return tree


def evaluate(expression: str, names: Mapping[str, Any]) -> Any:
    """Parse, grammar-check, then evaluate ``expression``.

    Strict: an unknown name raises :class:`NameError`, an
    ill-typed operation raises :class:`TypeError` /
    :class:`ZeroDivisionError`. Callers that want a rule to simply not
    fire on a transient mismatch should use :func:`evaluate_condition`.

    Args:
        expression: The source string from the descriptor.
        names: Maps each referenceable input name to its current value.

    Returns:
        The Python value the expression evaluates to (natural truthiness
        of ``and`` / ``or`` is preserved — coerce with :func:`bool` if a
        strict boolean is needed).

    Raises:
        UnsafeExpressionError: Grammar / syntax violation (see
            :func:`parse`).
        NameError: A referenced name is absent from ``names``.
        TypeError: An operation got operands of the wrong type.
        ZeroDivisionError: Division by zero.
    """
    tree = parse(expression)
    return _eval(tree.body, names)


def evaluate_condition(expression: str, names: Mapping[str, Any]) -> bool:
    """Evaluate ``expression`` as a rule condition; never raise on data.

    Grammar violations still raise :class:`UnsafeExpressionError` — a
    malformed descriptor is an install-time problem, not a runtime one,
    and installed descriptors are already linted, so this practically
    never fires here. What this *does* absorb is data-shaped failure:
    a numeric field still ``None`` because the user hasn't typed it, a
    divide-by-zero from a partially-filled form, an unexpectedly-missing
    name. Any of those makes the whole condition ``False`` — the rule
    does not fire until its inputs are well-typed.

    Args:
        expression: The rule's ``when`` string.
        names: Maps each sibling input name to its current value.

    Returns:
        ``True`` only if the expression evaluates truthy with
        well-typed operands; ``False`` on any data-shaped failure.

    Raises:
        UnsafeExpressionError: The expression is outside the grammar.
    """
    tree = parse(expression)
    try:
        return bool(_eval(tree.body, names))
    except (TypeError, ZeroDivisionError, NameError):
        return False


def referenced_names(tree: ast.Expression) -> set[str]:
    """Return the set of variable names ``tree`` reads.

    The ``len`` in a ``len(x)`` call is the grammar's only callable and
    is *not* a variable, so it is excluded even if a tool author also
    declares an input literally named ``len``.

    Args:
        tree: A tree returned by :func:`parse`.

    Returns:
        Every name the expression resolves against the values mapping.
    """
    found: set[str] = set()

    def _walk(node: ast.AST) -> None:
        if isinstance(node, ast.Call):
            # Skip node.func (the `len` callable); only its argument
            # can reference inputs.
            for arg in node.args:
                _walk(arg)
            return
        if isinstance(node, ast.Name):
            found.add(node.id)
        for child in ast.iter_child_nodes(node):
            _walk(child)

    _walk(tree)
    return found


# --- Grammar enforcement -------------------------------------------------


def _check_node(node: ast.AST) -> None:
    """Raise :class:`UnsafeExpressionError` unless ``node`` is allowed."""
    if not isinstance(node, _ALLOWED_NODES):
        raise UnsafeExpressionError(
            f"{type(node).__name__} isn't allowed in a cross-field expression."
        )
    if isinstance(node, ast.Constant) and not isinstance(node.value, _ALLOWED_CONSTANT_TYPES):
        raise UnsafeExpressionError(f"{type(node.value).__name__} literals aren't allowed.")
    if isinstance(node, ast.Call):
        _check_call(node)


def _check_call(node: ast.Call) -> None:
    """``len(x)`` is the only permitted call shape."""
    if (
        not isinstance(node.func, ast.Name)
        or node.func.id != "len"
        or len(node.args) != 1
        or node.keywords
    ):
        raise UnsafeExpressionError(
            "The only function call allowed is len(x) with a single argument."
        )


# --- Evaluation ----------------------------------------------------------

_COMPARATORS: Final[dict[type[ast.cmpop], Any]] = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}
_BINOPS: Final[dict[type[ast.operator], Any]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
}


def _eval(node: ast.AST, names: Mapping[str, Any]) -> Any:  # noqa: PLR0911
    """Recursively evaluate a grammar-checked node."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in names:
            raise NameError(node.id)
        return names[node.id]
    if isinstance(node, ast.List | ast.Tuple):
        return [_eval(elt, names) for elt in node.elts]
    if isinstance(node, ast.UnaryOp):
        return _eval_unary(node, names)
    if isinstance(node, ast.BinOp):
        return _BINOPS[type(node.op)](_eval(node.left, names), _eval(node.right, names))
    if isinstance(node, ast.BoolOp):
        return _eval_bool(node, names)
    if isinstance(node, ast.Compare):
        return _eval_compare(node, names)
    if isinstance(node, ast.Call):
        # Grammar already proved this is len(x) with one positional arg.
        return len(_eval(node.args[0], names))
    # Unreachable: parse() rejects anything not handled above.
    raise UnsafeExpressionError(  # pragma: no cover
        f"{type(node).__name__} can't be evaluated."
    )


def _eval_unary(node: ast.UnaryOp, names: Mapping[str, Any]) -> Any:
    operand = _eval(node.operand, names)
    if isinstance(node.op, ast.Not):
        return not operand
    if isinstance(node.op, ast.USub):
        return -operand
    return +operand  # ast.UAdd — only remaining allowed unary op


def _eval_bool(node: ast.BoolOp, names: Mapping[str, Any]) -> Any:
    if isinstance(node.op, ast.And):
        result: Any = True
        for value in node.values:
            result = _eval(value, names)
            if not result:
                return result
        return result
    result = False
    for value in node.values:
        result = _eval(value, names)
        if result:
            return result
    return result


def _eval_compare(node: ast.Compare, names: Mapping[str, Any]) -> bool:
    left = _eval(node.left, names)
    for op, comparator_node in zip(node.ops, node.comparators, strict=True):
        right = _eval(comparator_node, names)
        if not _COMPARATORS[type(op)](left, right):
            return False
        left = right
    return True
