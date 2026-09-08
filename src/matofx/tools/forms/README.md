# `matofx.tools.forms` — descriptor-driven input forms

Single-source-of-truth bridge between a tool's `descriptor.toml` (the *authoring* surface) and the runtime engine that validates user-submitted inputs and renders the input form.

## What's in pass 1 (this turn)

- **`model.py`** — `build_input_model(descriptor) → type[BaseModel]` + `validate_inputs(descriptor, payload) → dict`. The Pydantic v2 model is auto-derived from the descriptor and **cached per `(alias, version)`** per brief §F30 Q5 — an in-flight job started before a tool upgrade keeps validating against its pinned-version schema.
- **`errors.py`** — `FieldError` and `FormValidationError` with **per-field plain-English messages** per §F30 clarifier 3 ("Operating temperature can't exceed the material's max safe temperature.").
- **`render.py`** — `render_form(descriptor) → FormHandle`. Walks the descriptor and emits one `ui.expansion` per `[[input_groups]]` entry (legacy parity, locked 2026-05-08); honours `column` / `row` placement and `sub_columns`. Returns a `FormHandle` exposing `values()` / `seed(...)` / `show_errors(...)` / `clear_errors()` so the My Jobs workspace can drive submit, preset / previous-job / defaults loading, and inline error display.
- **Sum-check post-validation** — the `SumCheck` constraint already in the descriptor (e.g. component percentages summing to 100) is honoured. Failures attach to the first listed sum-parameter and carry the full participant list; `show_errors` highlights every participant.
- **87 unit tests** (`tests/tools/forms/` + `tests/ui/test_format.py`) covering cache behaviour, every descriptor type, every translated Pydantic error code, sum-check positive/negative/dedupe/tolerance/missing-participant paths, layout planning (column/row ordering, sub-column bucketing, ungrouped synthesis, flex-weight clamping), `FormHandle` seed/error/clear flows.

## What's deferred to pass 2

- `seeders.py` — preset / previous-job / defaults *loaders* (read `presets.toml`, fetch a previous job's `inputs.json` ACL-checked, return descriptor defaults). The renderer's `FormHandle.seed(...)` consumes whatever these produce; the loaders themselves are the next module.
- Browser file uploads → CAS staging + magic-byte sniffing + size-cap enforcement (§F31 / §F30 clarifier 2).
- Property-based coverage with Hypothesis (§H40 lists it as a goal but it's not in the project lock file yet).

## §16.16 conditional dependencies (landed 2026-05-17)

The cross-field DSL and live `dependencies` reactivity previously listed here are **done**:

- **`safe_expr.py`** — owned sandboxed `ast` evaluator for the locked §F30 grammar (not `simpleeval`; see the dated supersede note on PROJECT_BRIEF §F30 clarifier 1). Backs both rule conditions and the §F30 cross-field constraints.
- **`resolver.py`** — pure `resolve(descriptor, values) → EffectiveForm`; topological (Kahn) ordering with declaration-order/last-wins; only `set_value` writes create ordering/cycle edges (a condition only reads values).
- **`model.py`** — descriptors *with* rules build a relaxed model (types/parse/file-ext only); a resolver-driven post-pass enforces effective required/options/bounds and applies the `on_hide` value-state matrix + `set_value` forces. Descriptors *without* rules take the original strict path **unchanged** (byte-identical — no regression).
- **`render.py`** — every field is wrapped in a recorded container + a muted auto-note; for rule-bearing tools the form re-resolves on every change (and on `seed`) and applies show/hide, enable/disable, option repopulation (stale value cleared), `set_value` force, and a plain-English "set automatically / locked / options updated" note. `FormHandle.refresh()` exposes the re-resolve. Rule-free tools render and behave exactly as before.

**Cross-field constraints (landed 2026-05-17).** `ToolDescriptor.cross_field_constraints` (top-level `{expression, error_message}` list, the §F30 wire shape) is now wired end-to-end: install-time grammar + sibling-reference lint (`_cross_field_constraints_well_formed`), and submit-time evaluation in `validate_inputs` via `_check_cross_field_constraints`. Evaluated against the **resolved** values (after any §16.16 effects), anchored to the first participant and carrying all participants in `FieldError.sum_parameters` so the renderer highlights the whole group; the author's `error_message` is shown verbatim and the raw expression never is (§F30 clarifier 3). Skipped (not failed) when a participant is unfilled or hidden-dropped — same don't-nag tolerance as `SumCheck`. Nothing in the §F30 input-validation scope is now deferred.

## Field-type → Python annotation mapping

| Descriptor type | Annotation | Notes |
|---|---|---|
| `string` | `str` | Stripped on read (Pydantic `str_strip_whitespace`). |
| `integer` | `int` | `ge` / `le` from descriptor `minimum` / `maximum`. |
| `float` | `float` | `ge` / `le` from descriptor `minimum` / `maximum`. |
| `boolean` | `bool` | — |
| `select` | `Annotated[str, AfterValidator]` | Membership in `options`. |
| `multiselect` | `Annotated[list[str], AfterValidator]` | Every entry must be in `options`. |
| `file` | `str` (or `Annotated[str, AfterValidator]`) | Path string in pass 1; optional extension allow-list. |
| `table` | `list[dict[str, Any]]` | Pass-through; column-aware validation in pass 2. |

## Public API

```python
from matofx.tools.forms import (
    FieldError,
    FormValidationError,
    build_input_model,
    clear_cache,
    validate_inputs,
)

descriptor = load_descriptor("path/to/tool/bundle")
try:
    values = validate_inputs(descriptor, submitted_payload)
except FormValidationError as exc:
    for fe in exc.field_errors:
        # fe.name → which input widget to flag
        # fe.label → human label for plain-text rendering
        # fe.message → friendly per-field message to show under the widget
        ...
```

`clear_cache()` exists for tests; production code never calls it.
