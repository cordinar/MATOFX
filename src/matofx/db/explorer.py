"""Read-only DB explorer used by the Admin → Explore Database page.

What this module is for
-----------------------

The legacy app exposed every SQLite table as a free-form dataframe
explorer for admins. v2 ports that idea but adds:

- A **closed allowlist** of tables. Anything internal (live session
  tokens, Alembic's bookkeeping) stays invisible regardless of role.
- A **column-redaction registry**. Sensitive columns
  (``password_hash``, ``secret_ref``, audit-chain hashes / signatures)
  are *never selected from the database* — the SELECT statement
  excludes them up front, and the response payload carries an opaque
  placeholder in their place. The plaintext never reaches Python.
- **Per-column filtering + sorting + pagination** so the UI can show
  large tables without freezing.

Per the durable design rule, raw SQL lives inside ``matofx.db``. The
API router calls into the helpers below; the router contains no SQL
or table names of its own.

Threat model
------------

The explorer is gated by the ``database.explore`` permission (granted
to ``administrator`` and implicit on ``super_user``). Even so, we
don't trust the API caller's input on top of that:

- Table names are validated against the in-code allowlist before any
  reflection — never interpolated into SQL.
- Sort columns are validated against the reflected (non-redacted)
  column list.
- Filter values bind through the SQLAlchemy parameterised-query
  machinery.

If a future column is sensitive, list it in :data:`_REDACTED_COLUMNS`
and it stops leaving the DB.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import MetaData, Table, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

# ---------- policy registry -------------------------------------------------

_HIDDEN_TABLES: Final[frozenset[str]] = frozenset(
    {
        # Live session tokens — never readable, even with redaction.
        "auth_session",
        # Alembic bookkeeping; not user-meaningful.
        "alembic_version",
    }
)
"""Tables that the explorer pretends don't exist. Hidden from list,
404 on direct fetch."""

_REDACTED_COLUMNS: Final[dict[str, frozenset[str]]] = {
    "user": frozenset({"password_hash"}),
    "aos_host": frozenset({"secret_ref"}),
    "hpc_cluster": frozenset({"secret_ref"}),
    # Hash-chain bytes for audit records — visible structurally but the
    # raw bytes don't help an admin and showing them invites accidents.
    "audit_log": frozenset({"prior_hash", "payload_hash", "signature", "signing_key_id"}),
    "audit_chain_head": frozenset({"head_hash", "head_signature"}),
}
"""Columns whose values never leave the database. The SELECT omits them
entirely; the response substitutes :data:`REDACTION_PLACEHOLDER`."""

REDACTION_PLACEHOLDER: Final[str] = "•••"
"""User-visible stand-in for a redacted column value."""

_USER_ID_ENRICHMENTS: Final[dict[str, frozenset[str]]] = {
    "tool": frozenset({"installed_by_user_id", "archived_by_user_id"}),
    "project": frozenset({"creator_user_id", "state_changed_by_user_id"}),
    "job": frozenset({"created_by_user_id", "cancel_requested_by_user_id"}),
    "user_logon_event": frozenset({"actor_user_id"}),
    "project_acl": frozenset({"user_id", "granted_by_user_id"}),
}
"""Columns that carry a foreign-key user id and should render as
``"Full Name (username)"`` in the explorer UI. The raw int is replaced
in the response payload after a single bulk lookup against ``user``.

Adding a new entry: list the (table, column) pair here. The resolver
ignores ``NULL`` and unknown ids gracefully (they pass through as-is)
so missing rows don't cause an explorer page to fail."""

_DEFAULT_PAGE_SIZE: Final[int] = 100
_MAX_PAGE_SIZE: Final[int] = 1000
"""Hard cap on rows-per-page to keep the UI responsive on big tables."""

DISTINCT_VALUE_LIMIT: Final[int] = 50
"""Distinct-value count below which the UI offers a value-picker. At or
above this the column falls back to free-text search only — listing
thousands of distinct ids/timestamps in a dropdown helps nobody."""

_MAX_FILTER_DEPTH: Final[int] = 20
"""How deeply filter groups may nest. A defensive cap — real queries
never approach it, but the tree comes from an (authenticated) client so
we don't let it recurse without bound."""

_MAX_FILTER_NODES: Final[int] = 200
"""Total nodes (groups + leaves) one filter tree may contain."""

_MAX_VALUES_PER_LEAF: Final[int] = 500
"""Cap on how many picked values a single ``values`` leaf may carry."""


# ---------- error types -----------------------------------------------------


class ExplorerError(RuntimeError):
    """Base for friendly errors surfaced to the API caller."""


class TableNotFoundError(ExplorerError):
    """Requested table isn't in the allowlist (or is hidden)."""


class UnknownColumnError(ExplorerError):
    """Sort / filter column doesn't exist on the requested table."""


class FilterError(ExplorerError):
    """The filter tree is malformed (bad node shape, unknown op, etc.)."""


class FilterTooComplexError(ExplorerError):
    """The filter tree exceeds the depth / node / value-count caps."""


# ---------- response shapes -------------------------------------------------


@dataclass(frozen=True)
class TableInfo:
    """Listing entry for one allowed table."""

    name: str
    row_count: int
    columns: tuple[str, ...]
    """All columns the explorer is willing to surface — i.e. every
    column on the underlying table *except* the ones in
    :data:`_REDACTED_COLUMNS` for this table. The redacted columns
    are reported separately via :attr:`redacted_columns` so the UI can
    render the placeholder cell with a hover hint."""

    redacted_columns: tuple[str, ...]


@dataclass(frozen=True)
class TablePage:
    """One page of rows from an allowed table."""

    columns: tuple[str, ...]
    """Column order: visible columns first (in their schema order),
    then redacted columns appended at the end. The ordering is stable
    across pages so the UI can lock the column header layout."""

    redacted_columns: tuple[str, ...]
    rows: list[dict[str, Any]]
    total_rows: int
    page: int
    page_size: int


@dataclass(frozen=True)
class ColumnDistinct:
    """Distinct-value summary for one column, driving the UI value-picker."""

    column: str
    too_many: bool
    """True when the column has more than :data:`DISTINCT_VALUE_LIMIT`
    distinct values — the UI then offers free-text search only and
    :attr:`values` is empty."""

    values: tuple[str, ...]
    """Distinct values rendered as text (``CAST(col AS TEXT)``), sorted.
    Empty when :attr:`too_many` is true. NULLs are excluded here and
    surfaced via :attr:`has_null` instead."""

    has_null: bool
    """Whether the column contains at least one NULL — the UI offers an
    explicit "(empty)" choice that maps to ``col IS NULL``."""

    limit: int


# ---------- public API ------------------------------------------------------


def is_table_visible(table_name: str) -> bool:
    """Pure check — is ``table_name`` allowed to be surfaced by the explorer?"""
    return table_name not in _HIDDEN_TABLES


async def list_visible_tables(session: AsyncSession) -> list[TableInfo]:
    """Return every table that's allowed to be surfaced.

    Sorted alphabetically by name. Each entry includes the row count
    (cheap on SQLite, fast enough on Postgres for small admin tables;
    consider caching if a future table grows into millions of rows).
    """
    metadata = await _reflect(session)
    out: list[TableInfo] = []
    for table in metadata.sorted_tables:
        if not is_table_visible(table.name):
            continue
        redacted = _redacted_for(table.name)
        all_columns = tuple(c.name for c in table.columns)
        visible = tuple(c for c in all_columns if c not in redacted)
        row_count = await _count_rows(session, table)
        out.append(
            TableInfo(
                name=table.name,
                row_count=row_count,
                columns=visible,
                redacted_columns=tuple(sorted(redacted)),
            )
        )
    out.sort(key=lambda t: t.name)
    return out


async def read_table_page(
    session: AsyncSession,
    *,
    table_name: str,
    sort_by: str | None = None,
    sort_order: str = "asc",
    filters: dict[str, Any] | None = None,
    page: int = 1,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> TablePage:
    """Read one page of rows from ``table_name``.

    Args:
        session: Open async DB session.
        table_name: Must be in the allowlist.
        sort_by: Column to sort on. Must be a *non-redacted* column.
            Defaults to the primary key (descending) when omitted.
        sort_order: ``"asc"`` or ``"desc"`` (case-insensitive).
        filters: A recursive boolean filter tree whose root is a *group*
            node. A group is ``{"op": "and"|"or", "children": [node,
            ...]}``; a leaf is ``{"column": str, "match": "values" |
            "text", ...}``. A ``"values"`` leaf matches
            ``CAST(col AS TEXT) IN (...)`` (its ``values`` OR-combined),
            plus ``col IS NULL`` when ``include_null`` is set; a
            ``"text"`` leaf matches ``LOWER(CAST(col AS TEXT)) LIKE
            %text%``. Empty groups / leaves are ignored. See
            :func:`_build_filter_clause`.
        page: 1-based page number.
        page_size: Clamped to ``[1, 1000]``.

    Raises:
        TableNotFoundError: ``table_name`` isn't allowed.
        UnknownColumnError: Sort column, or a filter leaf's column,
            doesn't exist or is redacted.
        FilterError: The filter tree is malformed.
        FilterTooComplexError: The filter tree exceeds the safety caps.
    """
    if not is_table_visible(table_name):
        raise TableNotFoundError(f"Unknown table {table_name!r}.")

    metadata = await _reflect(session)
    if table_name not in metadata.tables:
        raise TableNotFoundError(f"Unknown table {table_name!r}.")
    table = metadata.tables[table_name]

    redacted = _redacted_for(table_name)
    visible_cols = [c for c in table.columns if c.name not in redacted]
    visible_names = {c.name for c in visible_cols}

    # Build (and validate) the filter clause up front so the caller gets
    # a clear error before we issue any SQL.
    filter_clause = _build_filter_clause(filters, table, visible_names) if filters else None

    # Sort column: caller-supplied or fall back to the primary key.
    if sort_by is None:
        sort_by = _default_sort_column(table)
        # Default to descending so newest rows appear first (Job ID 0042
        # before 0001 etc.) — friendlier than ascending IDs.
        sort_order = "desc"
    elif sort_by not in visible_names:
        raise UnknownColumnError(f"Cannot sort on column {sort_by!r} (not in table or redacted).")
    if sort_order.lower() not in ("asc", "desc"):
        sort_order = "asc"

    page = max(1, page)
    page_size = max(1, min(_MAX_PAGE_SIZE, page_size))

    base = select(*visible_cols)
    count_base = select(func.count()).select_from(table)
    if filter_clause is not None:
        base = base.where(filter_clause)
        count_base = count_base.where(filter_clause)

    sort_col = table.columns[sort_by]
    base = base.order_by(sort_col.desc() if sort_order.lower() == "desc" else sort_col.asc())
    base = base.limit(page_size).offset((page - 1) * page_size)

    total_rows = (await session.execute(count_base)).scalar_one()
    result = await session.execute(base)
    rows: list[dict[str, Any]] = []
    for row in result.mappings().all():
        merged: dict[str, Any] = dict(row)
        for redacted_name in redacted:
            merged[redacted_name] = REDACTION_PLACEHOLDER
        rows.append(merged)

    await _enrich_user_id_columns(session, table_name, rows)

    columns_in_payload = tuple(c.name for c in visible_cols) + tuple(sorted(redacted))
    return TablePage(
        columns=columns_in_payload,
        redacted_columns=tuple(sorted(redacted)),
        rows=rows,
        total_rows=int(total_rows),
        page=page,
        page_size=page_size,
    )


def _build_filter_clause(
    node: Any,
    table: Table,
    visible_names: set[str],
    *,
    _depth: int = 0,
    _count: list[int] | None = None,
) -> sa.ColumnElement[bool] | None:
    """Compile one filter-tree node into a SQLAlchemy boolean clause.

    Recursively walks a group node (``{"op", "children"}``) or compiles
    a leaf node (``{"column", "match", ...}``). Returns ``None`` for a
    node that contributes no constraint (an empty group, or a leaf with
    no values / blank text) so empty branches simply drop out of the
    WHERE clause rather than matching everything-or-nothing.

    Args:
        node: The raw (JSON-decoded) node — a dict.
        table: The reflected table the filter runs against.
        visible_names: Non-redacted column names; a leaf naming anything
            else is rejected.
        _depth: Internal recursion depth, checked against
            :data:`_MAX_FILTER_DEPTH`.
        _count: Internal one-element node counter, checked against
            :data:`_MAX_FILTER_NODES`.

    Raises:
        FilterError: The node is the wrong shape or names a bad op /
            match mode.
        UnknownColumnError: A leaf names a missing / redacted column.
        FilterTooComplexError: Depth, node-count, or per-leaf value caps
            exceeded.
    """
    if _count is None:
        _count = [0]
    if _depth > _MAX_FILTER_DEPTH:
        raise FilterTooComplexError(f"Filter nests deeper than the limit of {_MAX_FILTER_DEPTH}.")
    _count[0] += 1
    if _count[0] > _MAX_FILTER_NODES:
        raise FilterTooComplexError(
            f"Filter has more than the limit of {_MAX_FILTER_NODES} conditions."
        )
    if not isinstance(node, dict):
        raise FilterError("Each filter node must be a JSON object.")

    if "op" in node:
        return _build_group_clause(node, table, visible_names, _depth=_depth, _count=_count)
    return _build_leaf_clause(node, table, visible_names)


def _build_group_clause(
    node: dict[str, Any],
    table: Table,
    visible_names: set[str],
    *,
    _depth: int,
    _count: list[int],
) -> sa.ColumnElement[bool] | None:
    """Compile a group node by AND/OR-combining its non-empty children."""
    op = node.get("op")
    if op not in ("and", "or"):
        raise FilterError("A filter group's 'op' must be 'and' or 'or'.")
    children = node.get("children", [])
    if not isinstance(children, list):
        raise FilterError("A filter group's 'children' must be a list.")

    clauses: list[sa.ColumnElement[bool]] = []
    for child in children:
        clause = _build_filter_clause(child, table, visible_names, _depth=_depth + 1, _count=_count)
        if clause is not None:
            clauses.append(clause)
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return sa.and_(*clauses) if op == "and" else sa.or_(*clauses)


def _build_leaf_clause(
    node: dict[str, Any],
    table: Table,
    visible_names: set[str],
) -> sa.ColumnElement[bool] | None:
    """Compile a leaf node into an ``IN`` / ``LIKE`` / ``IS NULL`` clause.

    ``match="values"`` → ``CAST(col AS TEXT) IN (...)`` over the picked
    values (themselves OR-combined), OR-ed with ``col IS NULL`` when
    ``include_null`` is set. ``match="text"`` → case-insensitive
    substring ``LIKE``. Casting to text on both the column and the
    bound values keeps matching consistent across int / bool / datetime
    columns (the value-picker reports the same text representation).
    """
    column = node.get("column")
    if not isinstance(column, str) or not column:
        raise FilterError("Each filter condition must name a column.")
    if column not in visible_names:
        raise UnknownColumnError(f"Cannot filter on column {column!r} (not in table or redacted).")
    col = table.columns[column]
    match = node.get("match", "text")

    if match == "values":
        values = node.get("values", [])
        if not isinstance(values, list):
            raise FilterError("A 'values' condition's 'values' must be a list.")
        if len(values) > _MAX_VALUES_PER_LEAF:
            raise FilterTooComplexError(
                f"A condition lists more than {_MAX_VALUES_PER_LEAF} values."
            )
        parts: list[sa.ColumnElement[bool]] = []
        if values:
            parts.append(cast(col, sa.Text).in_([str(v) for v in values]))
        if node.get("include_null"):
            parts.append(col.is_(None))
        if not parts:
            return None
        if len(parts) == 1:
            return parts[0]
        return sa.or_(*parts)

    if match == "text":
        raw = node.get("text", "")
        text = (str(raw) if raw is not None else "").strip()
        if not text:
            return None
        return func.lower(cast(col, sa.Text)).like(f"%{text.lower()}%")

    raise FilterError("A filter condition's 'match' must be 'values' or 'text'.")


async def column_distinct_values(
    session: AsyncSession,
    *,
    table_name: str,
    column: str,
) -> ColumnDistinct:
    """Summarise a column's distinct values for the UI value-picker.

    Returns up to :data:`DISTINCT_VALUE_LIMIT` distinct non-null values
    (as text). If the column has more than that, ``too_many`` is true
    and ``values`` is empty — the UI then offers free-text search only.
    A separate cheap probe reports whether any NULLs exist so the UI can
    offer an explicit "(empty)" choice.

    Raises:
        TableNotFoundError: ``table_name`` isn't allowed.
        UnknownColumnError: ``column`` is missing or redacted.
    """
    if not is_table_visible(table_name):
        raise TableNotFoundError(f"Unknown table {table_name!r}.")
    metadata = await _reflect(session)
    if table_name not in metadata.tables:
        raise TableNotFoundError(f"Unknown table {table_name!r}.")
    table = metadata.tables[table_name]

    redacted = _redacted_for(table_name)
    if column in redacted or column not in {c.name for c in table.columns}:
        raise UnknownColumnError(
            f"Cannot read distinct values for column {column!r} (not in table or redacted)."
        )
    col = table.columns[column]

    # Cast to text so the returned strings match what the IN filter
    # compares against (cast(col, Text)), and select one past the limit
    # to detect the over-cap case without a full COUNT(DISTINCT).
    distinct_stmt = (
        select(cast(col, sa.Text))
        .where(col.is_not(None))
        .distinct()
        .limit(DISTINCT_VALUE_LIMIT + 1)
    )
    rows = (await session.execute(distinct_stmt)).all()
    too_many = len(rows) > DISTINCT_VALUE_LIMIT
    values: tuple[str, ...] = ()
    if not too_many:
        values = tuple(sorted({str(r[0]) for r in rows}))

    null_stmt = select(func.count()).select_from(table).where(col.is_(None))
    has_null = bool((await session.execute(null_stmt)).scalar_one())

    return ColumnDistinct(
        column=column,
        too_many=too_many,
        values=values,
        has_null=has_null,
        limit=DISTINCT_VALUE_LIMIT,
    )


async def _enrich_user_id_columns(
    session: AsyncSession, table_name: str, rows: list[dict[str, Any]]
) -> None:
    """Replace user-id ints with ``"Full Name (username)"`` strings in-place.

    Looks at :data:`_USER_ID_ENRICHMENTS` for which columns to enrich
    on this table, collects every distinct user id referenced in those
    columns across all rows, issues a single ``SELECT`` against
    ``user``, and rewrites each matching cell in-place. Unknown or
    null ids pass through untouched so a deleted user still renders
    something meaningful (the bare id) rather than crashing the page.
    """
    enrich_cols = _USER_ID_ENRICHMENTS.get(table_name, frozenset())
    if not enrich_cols:
        return

    user_ids: set[int] = set()
    for row in rows:
        for col in enrich_cols:
            value = row.get(col)
            if isinstance(value, int):
                user_ids.add(value)
    if not user_ids:
        return

    # ``user`` is a reserved keyword in Postgres; double-quoting it
    # makes the lookup work on both SQLite and Postgres. Allowlist
    # machinery vouches for the *enriched* table; the lookup target
    # is a fixed internal one and doesn't need the same treatment.
    stmt = sa.text('SELECT id, full_name, username FROM "user" WHERE id IN :ids').bindparams(
        sa.bindparam("ids", list(user_ids), expanding=True)
    )
    user_rows = (await session.execute(stmt)).all()
    label_by_id: dict[int, str] = {}
    for uid, full_name, username in user_rows:
        full = (full_name or "").strip()
        username_str = str(username or "")
        label_by_id[int(uid)] = f"{full} ({username_str})" if full else username_str

    for row in rows:
        for col in enrich_cols:
            value = row.get(col)
            if isinstance(value, int) and value in label_by_id:
                row[col] = label_by_id[value]


# ---------- internals -------------------------------------------------------


def _redacted_for(table_name: str) -> frozenset[str]:
    return _REDACTED_COLUMNS.get(table_name, frozenset())


async def _reflect(session: AsyncSession) -> MetaData:
    """Reflect the live schema into a fresh :class:`MetaData`.

    Reflection runs sync against the bound connection. We re-reflect
    on every call rather than caching — the explorer is rarely opened
    and a one-shot reflection cost is tiny next to the round-trip
    latency of the page itself.
    """
    metadata = MetaData()

    def _do_reflect(sync_conn: sa.Connection) -> None:
        metadata.reflect(bind=sync_conn)

    await session.run_sync(lambda s: _do_reflect(s.connection()))
    return metadata


async def _count_rows(session: AsyncSession, table: Table) -> int:
    """``SELECT COUNT(*)`` for one table — used by the listing endpoint."""
    result = await session.execute(select(func.count()).select_from(table))
    return int(result.scalar_one())


def _default_sort_column(table: Table) -> str:
    """Pick the primary key column to default-sort on.

    Falls back to the first column when the table has no primary key
    (rare in our schema — exists mainly as a defensive backstop).
    """
    if table.primary_key.columns:
        first_pk = next(iter(table.primary_key.columns))
        return str(first_pk.name)
    return str(table.columns[0].name)
