# Integer parameter handling — varying integer inputs in studies & optimisation

Status: **Phases A–D shipped 2026-06-15** (ARCHITECTURE §16.27, ADR-0009 amendment).
Two build-time deviations from the original plan, both documented in §2.4 and §2.5
below: Phase B runs fewer rather than re-asking (the re-ask backfill would desync
pymoo's generational ask/tell); Phase C uses a pymoo **Repair** operator (lattice
snapping) rather than native mixed-variable types — those aren't supported by the
continuous algorithms (CMA-ES / DE / PSO), and the `bo` pool was already sampled on
the integer lattice.

Decisions locked with the maintainer up front: rounding rule = **round half away
from zero** (`2.5→3`, `-2.5→-3`); the overriding requirement is that **any
automated sampler — parametric or optimisation — only ever emits integer values
for an integer parameter**;
integer step is **always 1** (no stride); scope spans **all three** strands
(validation tightening, dedupe, native integer optimisation), built **A→B→C→D**
with each phase reviewable; this note + an ADR-0009 amendment land first.

The non-negotiable: **whenever a parameter is sampled automatically — parametric
sweep, LHS/cohort seed, or any optimisation strategy — an integer parameter is
always respected (integer value, within an integral range).** Everything below
serves that invariant.

This covers what happens when a user picks a tool input declared `integer`
(`descriptor.py` `InputFieldType`) as a **varied** factor in a parametric study or
an optimisation study. The rule the user stated: *an integer input, when varied,
must only ever take integer values* — and, by extension, its declared range must
itself be integral.

## 1. The problem & current state

A varied integer travels: **descriptor → factor definition → sampler/strategy →
candidate → job `inputs.json`**. The question is where "float-ness" survives.

**Audit result (2026-06-15, code read).** At the *emitted-value* level the system
is already correct — every candidate source coerces integer factors to `int`
before a job is created:

| Path | Coercion today | Location |
|---|---|---|
| Parametric grid levels | `round(v)` + de-dupe + warn if distinct < levels | `studies/generate.py:285` |
| Parametric `_coerce` (all modes) | `round`, then `ceil(min)`/`floor(max)` clamp | `studies/generate.py:534` |
| Surrogate `bo` decode | `max(int(low), min(int(high), round(v)))` | `strategies/surrogate_bo.py:117` |
| Evolutionary / NSGA (pymoo) | `round` + clamp on decode | `strategies/multi.py:173` |
| Random / grid strategy | native `rng.integers`, `int()` bounds | `strategies/random_search.py:54`, `grid.py:81` |
| LHS seed | `floor(lo + u·(hi−lo+1))` + clamp | `seed.py:58` |
| Cohort-seed midpoint | `round(mid) if type=="int"` | `runtime.py:1139` |

So **no float reaches an integer parameter's `inputs.json` from the study itself.**
A float observed in an integer input would come from a *non-varied* input (the base
scenario as authored — out of scope) or be a display artifact, not the sampler.

Three real defects remain, and they are the subject of this note:

**D1 — Integer factor *bounds* are not constrained to whole numbers.**
`_validate_numeric_factor` (`config.py:451`) is shared by `float` and `int`
factors; it only checks the bounds are numeric, finite, ordered, and inside the
descriptor box. A user can define an integer factor with `min=1.9, max=10.5`.
The descriptor's *own* bounds are forced whole at `descriptor.py:438`, but a
factor range — which may legitimately *tighten* the descriptor box — is not.
This is the one genuine "float where it shouldn't be."

**D2 — The snap rule is inconsistent across paths.** Grid/BO/multi use Python
`round()` (banker's / half-to-even — `round(2.5)==2`), LHS uses `floor`,
parametric clamps with `ceil`/`floor`, several decode sites coerce bounds with
`int()` (truncation toward zero — differs from `floor` for negatives). The same
continuous `2.5` becomes a different integer depending on which strategy runs.
Harmless to validity, bad for reproducibility and for explaining a result.

**D3 — Optimisers reason in continuous space and round on decode (relaxation).**
pymoo strategies encode every factor into a real vector and round integers only
when decoding; the `bo` surrogate fits and samples its candidate pool in float
space. Two costs: (a) distinct continuous points round to the **same** integer →
duplicate jobs that **burn evaluation budget**, and (b) the GP/EA optimises a
misleading continuous landscape instead of the integer one.

## 2. Design

### 2.1 One snap rule — round half away from zero (D2)

A single shared helper, the only place an integer factor value is produced:

```python
# matofx/studies/_intsnap.py  (new)
import math

def snap_int(value: float, lo: int, hi: int) -> int:
    """Round half **away from zero** to the nearest integer, then clamp to [lo, hi]."""
    v = float(value)
    nearest = math.floor(v + 0.5) if v >= 0 else math.ceil(v - 0.5)
    return max(lo, min(hi, int(nearest)))
```

Round half away from zero gives `2.5→3`, `3.5→4`, and symmetrically `-2.5→-3`,
`-3.5→-4` — the rule a user expects when reading a result, and symmetric about
zero (no directional bias for ranges that straddle it). Pinned in test fixtures.
Every site in the audit
table that currently does its own `round`/`floor`/`int()` switches to `snap_int`
(or, for grid enumeration, to integer-native generation — §2.4). LHS keeps its
`floor(lo + u·(hi−lo+1))` *cell* mapping (that is interval bucketing, not
rounding — it stays, but its final clamp uses the shared bounds-as-int).

### 2.2 Integer bounds are integral (D1)

Split `_validate_numeric_factor` so an `int` factor additionally requires `min`
and `max` to be whole numbers, with a plain-English error
("Factor 'X' is an integer input — its min/max must be whole numbers; got 1.9").
Rationale over silently snapping: a fractional integer bound is almost always a
UI/authoring slip, and snapping it would *widen or shift* the range the user
believes they set (`1.9` → `2` raises the floor; `1.9` → `1` via `int()` lowers
it). Reject, don't guess. The UI (§2.5) prevents it being entered in the first
place; this validator is the backstop for API/import/migrate paths.

Once bounds are guaranteed integral, all the `int(f.low)` / `int(f.high)` /
`ceil`/`floor` bound-coercions in strategies collapse to a clean `int(f.low)` that
is exact (no truncation surprise), and `FactorSpec.low/high` can be documented as
"integral when `kind == 'int'`".

### 2.3 Step is always 1

Confirmed: no stride. An integer factor varies over every integer in `[lo, hi]`.
This matches the descriptor rule that integer `precision` must be `1.0`
(`descriptor.py:441`). If a user wants "every 5th value" they model it as a
`select`/categorical factor, not an integer with a stride — keeps one mental model.

### 2.4 Dedupe rounded collisions (D3, Phase B)

After a strategy decodes a batch, drop candidates whose integer point duplicates
(a) another point already in this batch, or (b) a point already evaluated this
study, and ask the strategy to backfill. Guard rails:

- **Bounded retries.** The integer space can be smaller than the batch (e.g.
  `[1,3]` with `batch_size=8`). Cap backfill attempts; when the distinct integer
  space is exhausted, **shrink the batch** and surface the same "distinct values <
  requested" warning the parametric path already emits (`generate.py:285`) rather
  than spinning. The exhaustion bound is `∏(hi−lo+1)` over integer factors ×
  categorical cardinality — computable up front for a hard cap.
- **Scope of "already evaluated".** Reuse the durable study state / `study_result`
  rows that the resume contract (§10 of ADR-0009) already maintains — no new store.
- **Interaction with seeding.** Cohort/previous-run seeds are *told*, not run, so
  they populate the "already evaluated" set and a seeded point is never re-run.

### 2.5 Native integer optimisation (D3, Phase C)

- **pymoo strategies** (`multi.py` + the `cmaes`/`de`/`pso`/`ga`/`nsga2`/`nsga3`/
  `moea_d` registry, all on the shared ask/tell wrapper §5.2 of ADR-0009): declare
  variables with pymoo's `Integer` / `Choice` / `Real` mixed-variable types and use
  the mixed-variable operators, instead of an all-`Real` vector with round-on-decode.
  pymoo then proposes integer points directly — fewer collisions, a truthful search
  landscape. CMA-ES is the exception (continuous by construction) — it keeps
  relaxation + `snap_int`, documented as such.
- **`bo` surrogate**: sample the acquisition **candidate pool**
  (`surrogate_candidate_pool`, `base.py:165`) on the integer lattice for integer
  factors (and choice indices for categoricals) so the argmax is chosen among
  realisable points; the GP encoding stays float (a GP over integers is still fit
  on the integer coordinates — no relaxation gap because the pool is integral).

This phase is behaviour-changing for optimiser *quality/trajectory* (not validity),
so it ships last, behind the existing optimisation feature gate, with before/after
notes in the ADR.

## 3. Phasing

| Phase | Content | Risk |
|---|---|---|
| **A** ✅ | `snap_int` helper; replace all snap/clamp sites; integral-bounds validation (D1+D2) — **shipped 2026-06-15** | low — no algorithm change, values unchanged except half-away-from-zero vs half-even edge |
| **B** ✅ | Decode-time dedupe; **run-fewer** (not re-ask) + `space_exhausted` stop — **shipped 2026-06-15** | medium — touched runtime batch loop |
| **C** ✅ | pymoo lattice **Repair** (not native mixed-variable) + already-lattice `bo` pool now returns distinct points — **shipped 2026-06-15** | medium — optimiser trajectory changes |
| **D** ✅ | UI integer-only (step 1, no decimals) min/max + snap-on-collect — optimisation wizard (both builders) **and** the parametric-studies form (grid/design/sampling/run-table/constant) — **shipped 2026-06-15** | low |

**Phase D scope correction.** The plan assumed the *parametric* UI already
enforced whole-number entry and only the optimisation wizard needed parity. In
fact the parametric form (`ui/pages/studies.py`) let an integer input take a
fractional bound too (the generator rounded it silently downstream) — reported
by the maintainer. Fixed there as well: every numeric widget for an integer
descriptor input steps by 1 with no decimals (`_int_number_kw`), and every
collect path snaps via `_snap_int_value` + de-dupes (`_dedupe_numeric`) so the
submitted spec is whole — across grid (range + by-values), screening/RSM,
space-filling (uniform + log-uniform bounds), the run-table cells, and held
constants. Float factors are untouched; non-numeric constants pass through.

**Deviation B — run-fewer, not re-ask.** The plan said "bounded backfill". In
build, re-calling `strategy.ask` to top a batch back up would desync the pymoo
generational ask/tell contract (the wrapper penalises un-told candidates of a
generation). The runtime already truncates a generation to the remaining budget
and runs fewer (`runtime.py`), so dedupe follows the same pattern: drop the
repeats, run the unique remainder, let the strategy propose afresh next batch.
When a fully-discrete space is exhausted (`_discrete_capacity`), or a strategy
stalls re-proposing seen points for `_MAX_CONSECUTIVE_EMPTY_BATCHES`, the study
stops with the new `space_exhausted` reason. The dedupe set is seeded from
recovered + seed evals so a resumed study doesn't re-run prior points.

**Deviation C — repair, not native mixed-variable.** pymoo's `Integer`/`Choice`
mixed-variable machinery only fits the GA family; **CMA-ES, DE and PSO are
continuous algorithms** that can't take a mixed-variable problem. The universal,
low-risk equivalent is a pymoo `Repair` that snaps integer / categorical-index
columns onto their lattice after each generation's variation — verified to apply
under ask/tell for GA/DE/PSO/NSGA-II/NSGA-III/MOEA-D. **CMA-ES rejects a `repair`
operator** (it wraps the `cma` package, which has its own `integer_variables`
hook), so it keeps relaxation + `snap_int` on decode — integer values are still
emitted, only its internal population isn't lattice-constrained. The `bo` pool was
**already** sampled on the integer lattice (`_random_encoded` uses `rng.integers`),
so Phase C there only added returning the top *distinct* designs.

## 4. Edge cases to pin in tests

- Half-away-from-zero boundary: `2.5→3`, `3.5→4`, `-2.5→-3`, `-3.5→-4`.
- Integer factor with `[1,3]`, `batch_size=8` → batch shrinks to 3, warning fired,
  no infinite loop.
- Fractional integer bound via API/import → rejected with clear message (D1).
- Tightened-but-integral bound (`descriptor [0,100]`, factor `[10,20]`) → accepted.
- Seeded point equal to a later proposal → not re-run (dedupe + seed interaction).
- CMA-ES integer factor → relax + `snap_int`, values integral, dedupe still applies.
- Parametric `levels=10` on integer `[1,3]` → 3 distinct, existing warning intact.

## 5. Tests to add / touch

- New `tests/studies/test_intsnap.py` — `snap_int` rounding + clamp + negatives.
- `tests/studies/optimisation/test_config.py` — integral-bounds validation.
- Strategy tests (`test_surrogate_bo`, `test_multi`, `test_random_search`,
  `test_grid`, `test_seed`) — integer points only, half-up rule, dedupe.
- `tests/studies/optimisation/test_runtime.py` — batch dedupe + shrink + warning.
- Parametric `test_generate.py` — unchanged behaviour confirmed under shared helper.

Run targeted per `feedback_test_suite_speeds` / `feedback_use_dotvenv_for_tests`
(studies suites cycle migrations; use `.venv` for pymoo).

## 6. Docs to update (ships with the code, per CLAUDE.md)

- `docs/user/` — optimisation & parametric chapters: integer factors vary over
  whole numbers only; min/max must be whole; "every Nth value → use a select".
- `docs/admin/` — if any admin surface exposes factor definition.
- `ARCHITECTURE.md` §16 (optimisation) + the ADR-0009 amendment (below).
- Memory checkpoint `project_optimisation_build_checkpoint` once shipped.

## 7. Files in play

`config.py` (validation), new `studies/_intsnap.py`, `studies/generate.py`,
`studies/optimisation/strategies/{surrogate_bo,multi,random_search,grid}.py`,
`seed.py`, `runtime.py` (dedupe loop + `:1139`), `strategies/base.py`
(`FactorSpec` doc), plus the variable-builder UI. **Coordinate on `runtime.py`
and the optimisation results UI — flagged as shared with another session in
`project_optimisation_results_explorer_gaps`.**
