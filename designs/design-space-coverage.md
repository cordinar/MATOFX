# Design-space coverage — build spec

Build-level spec for **ADR-0020**. Source of truth for the work; read the ADR first
for the decision and rationale. Extends `designs/surrogate-family.md` (ADR-0008),
`designs/optimisation-framework.md` (ADR-0009), `designs/job-cohorts.md`,
`designs/vv-tracking.md` (ADR-0011), `designs/parametric-studies.md` (ADR-0001),
`designs/output-validity.md` (ADR-0017 — the validity gate that shapes the point set)
and `designs/integer-parameter-handling.md` (the integer-lattice invariant).

*Revised 2026-08-24 after **two** design-review passes. First pass: the point set is
defined explicitly (§1), analysis splits into a response-agnostic design pass and a
per-response model pass (§2), and the report carries per-factor and per-response
breakdowns (§7). Second pass: derived axes (§1.2.1), log-scaled axes (§3.2),
method-driven criterion selection for non-GP surrogates (§2.3), per-mode field
responses (§2.2.1), and a persisted `edge` threshold (§8). All twenty findings and
their resolutions are tabulated in §15; residual risk and the recommended first action
are in §19.*

*Revised again 2026-08-24 by **writing slice 1's tests** — §19's recommended first
action, which found what further re-reading would not have: eleven mismatches with the
real types, two of them proven by a failing test (`TrainingData` carries no
categorical columns and no filtered-exclusion count). Findings 21–31 and their
resolutions are in §15.*

*Revised a fourth time 2026-08-24 by **slice 2's tests**, which found the design's most
load-bearing sentence to be false as written: raw GP posterior sigma is **not** `y`-free
in this codebase, because the fit uses `normalize_y=True` and multiplies its standard
deviation back up by the response's own spread. Everything now measures **relative
sigma**. Findings 32–38 in §15.*

*Revised a fifth time 2026-08-24 by **slice 3** (cohort preview), which is where the
design meets a real surface for the first time. Four findings, all interface rather than
argument: the preview needs a training set before a response exists, the report has to
carry the points the scatter matrix draws, `CoverageError` needed registering with the
API's error map, and the second feature gate needed stating. Findings 39–42 in §15.*

*Revised a ninth and final time 2026-08-24 by **slice 7** (the V&V tie-in), which
completes ADR-0020. Three findings, all about a schema mismatch the ADR had not noticed:
`applicable_range_json` is `{factor: {min, max}}` and can express only numeric bounds,
while an envelope also constrains categorical levels and non-numeric pinned values.
Dropping those silently would have made the V&V record claim a **wider** applicability
than the guard enforces — the exact inversion this feature exists to prevent. Findings
59–61 in §15.*

*Revised an eighth time 2026-08-24 by **slice 6** (the predict-time guard), run against
a real 250-run bracket fit. Seven findings, **three of them quantitative** and none
findable on a fixture: every GP response scored `poor` regardless of the design (the
always-red light of finding 44, reappearing in the per-response pass); a tenth of the
training runs read `edge` by construction; and the stored report was missing its
per-response half entirely, so the guard could never return `edge` at all. The last
unverified area of §19 — `exporter.py` / `tool_synthesis.py` — is now checked. Findings
52–58 in §15.*

*Revised a seventh time 2026-08-24 by **slice 5** (persistence). Four findings, and the
two §19 has listed as unverified since the first draft are finally settled: a refit
**does** mint a new version, so no stored report can go stale; and the §9.1 cache key
**can** be assembled at the cohort call site, but only from the *resolved* rows rather
than the cohort definition. Findings 48–51 in §15.*

*Revised a sixth time 2026-08-24 by **slice 4 and its first run against real data** —
254 `bracket_stress` runs from the NAFEMS chain, 12 varying factors. Two quantitative
defects that no unit test on a 2-factor design could have found: the coverage **score**
was dimension-driven rather than design-driven (a *perfect* 254-point Latin hypercube
read `poor` from eight factors up), and the greedy suggestion used a **maximin**
criterion to optimise a **minimax** quantity — beating 100 % of random batches at two
factors and **0 %** at twelve. Findings 43–47 in §15; the corrected criterion is §5.2.*

## Problem

A surrogate can be fitted from any Job Cohort. Cohorts built from a parametric study
are space-filling by construction; cohorts built from ad-hoc single jobs are
clustered, often confounded, and sometimes categorically unbalanced.

Every existing quality signal is **post-fit** — `SurrogateQuality.q2` / `rmse_cv`
(`studies/surrogate/__init__.py:94`) and the field variants
(`studies/surrogate/field.py`). Worse than absent, Q² is *misleading* here: computed
by leave-out CV on the points that exist, a tight cluster scores well precisely
because every held-out point has a near neighbour. High Q², no coverage, false
confidence — and the user is then invited to publish, export-as-tool, and optimise
against that model.

## Principle

**Coverage is a property of the design, not of the fit.** It is measurable before any
response is looked at, it travels with the model as versioned evidence, and it is
enforced again at prediction time. Three diseases, reported separately: sparsity,
confounding, categorical imbalance.

---

## 1. The point set and its provenance

Everything downstream depends on answering one question precisely: **which points, in
which space, are we measuring?** Getting this wrong would overstate coverage, which is
the exact failure this design exists to prevent.

### 1.1 The points are the training set, not the cohort

Cohort membership is **not** the point set. `studies/surrogate/ingest.py` already
drops runs before they reach a fit:

- the **ADR-0017 validity gate** — *"never train on a run flagged invalid"* — counted
  in `TrainingData.n_excluded_invalid`;
- **`FactorFilter`** — `input_ranges`, `cross_field_predicate` and `output_ranges`,
  the user's on-the-fly outlier exclusions.

A cohort of 100 jobs may train on 62 points. Coverage measured on 100 would overstate
it by exactly the runs the platform already decided were unfit to learn from.

**Rule.** Coverage consumes the **`TrainingData`** produced by `ingest.py`, never raw
cohort membership. The exclusion counts are surfaced in the report so a user reading
"62 points" understands where the other 38 went. This is a genuine strengthening of
ADR-0017: invalid runs are not merely excluded from the fit, their absence is now
visible in the coverage verdict.

#### 1.1.1 Two additive fields `TrainingData` does not carry yet

Writing the tests proved the rule above cannot be satisfied by today's dataclass. Both
gaps are additive fields with defaults, so `fit_surrogate` and every existing caller
are untouched:

- **`categorical_columns: dict[str, list[Any]]`.** `collect_training_data` builds
  `factor_columns` from `_as_number(value) is not None`, so a varied `select` / `bool`
  / `file` input **never reaches the training data at all**. Categorical imbalance is
  the *silent* failure of the three diseases — 40 CFRP / 2 GFRP looks perfect on every
  continuous axis — and it cannot be measured from a numeric-only view. Row-aligned
  with `factor_columns`; a constant non-numeric input stays in `input_constants` and
  does **not** appear here (that is the `pinned` case, §1.2).
- **`n_excluded_filtered: int`.** `FactorFilter` drops are bare `continue`s, so §6's
  `CoverageDiagnostics.n_excluded_filtered` has no source and "62 of 100" cannot be
  broken down. The two gates are counted separately on purpose: an invalid result is a
  solver problem, a filtered one is the user's own on-the-fly exclusion, and the
  remedies differ.

**`n_rows` is not the point count.** It means different things on the two collectors —
`len(kept)` (post-validity) on the scalar path, `n_considered` (pre-validity) on the
field path. Coverage derives its own `n_points` from the column lengths and never reads
`n_rows`. Reconciling the two collectors is worth doing, but it is not this design's
change to make.

### 1.2 The factors are inferred, not declared

An ad-hoc cohort has **no `FactorSpec`** — nobody authored a study. The codebase
already resolves this: `varied_columns` selects the inputs that actually vary, and
`_constant_inputs` (`ingest.py`) captures the rest.

| Source | Factor set from |
|---|---|
| Parametric / optimisation study | the study's `FactorSpec` (`studies/factors.py`) |
| **Ad-hoc cohort** | **inferred — `varied_columns` over the member runs** |
| An existing model version | the persisted `factors` + `input_constants` (`models/orm.py`) |

**Constant inputs are recorded, never silently dropped.** An input held at one value
across the whole training set is not a factor and gets no coverage measure — but the
model cannot extrapolate along it at all, so it is carried into the envelope as a
**pinned** factor and enforced by the guard (§8): predicting at a different value of a
never-varied input is pure extrapolation and must read `outside`. This is a real
scenario — someone reuses a model on a job with a different mesh density — and silence
would be the dangerous answer.

### 1.2.1 Derived axes — real predictors you cannot commission

`ingest.py:51` reserves `TIME_FACTOR = "time"`: a time-resolved field surrogate
contributes one `(run, timestep)` row per snapshot so that `predict({…, 'time': t})`
reconstructs the field at any step. Ingest rejects a real input that collides with the
name.

`time` is a genuine **predictor** — a prediction past the last trained snapshot really
is extrapolation — but it is **not a design variable**. Nobody can commission "a job at
snapshot 347"; timesteps arrive as solver output.

**The column holds a 0-based snapshot index, not seconds.** `ingest.py` is explicit:
*"`time` is the 0-based index of the snapshot within the run's sorted series"*. Earlier
drafts of this document illustrated the axis with "t = 5000 s from 600 s runs" — a
sentence the platform **cannot** produce, because it has no seconds to quote. Every
user-facing string about a derived axis therefore speaks in **snapshots**
("this model covers snapshots 0–23 of each run"). Mapping an index back to physical
time would need the solver's time values, which the field ingest does not retain;
carrying them is a possible later refinement, not an assumption this design may make.

**Rule.** Such axes carry `kind="derived"`:

- **measured and guarded** — they appear in the envelope, count toward `contains`, and
  a prediction beyond their trained span reads `outside` (§8);
- **never suggested on** — `suggest_points` skips them entirely (§5.1). Proposing a
  design point at a time coordinate would be meaningless output;
- **remedied differently** — where a derived axis is thinly covered the report says so
  in the right language: the fix is a longer run or finer snapshot retention, **not a
  new design point**. Naming the wrong remedy would be worse than staying silent. The
  remedy text has nowhere to live on `FactorCoverage`, so it rides on a
  `CoverageWarning` carrying the factor name (§7).

This is not hypothetical: the NAFEMS demo's transient thermal-soak field surrogate is
exactly this shape.

### 1.3 Points outside the declared envelope

The envelope is user-editable (§3), so **narrowing it is the expected action**: "we
ran 1–8 mm but only trust 2–6". The moment that happens, training points sit outside
the declared region.

**Rule.** Such points **still train the model** — they are real data and improve
behaviour at the boundary — but they are **excluded from coverage measurement**, which
is only meaningful inside the region being claimed. They are counted as
`n_outside_envelope` and shown. Normalisation is never clamped: clamping would pile
out-of-range points onto the boundary and manufacture false duplicates.

### 1.4 Degenerate point sets

Real archives contain trivial cohorts, and every metric here has a floor:

| Condition | Behaviour |
|---|---|
| `n_points < coverage.min_points_for_analysis` | verdict `insufficient`; no metrics computed; plain-English message ("Too few runs to judge coverage — at least N are needed") |
| fewer than 2 continuous factors | correlation and condition number omitted, not zero-filled |
| `n_points` below the cold-start ratio (§2.3) | model-free criterion, stated in the report |
| every factor constant (nothing varies) | verdict `insufficient`; "these runs are all the same design point" |

`insufficient` is a first-class verdict, distinct from `poor`. "We cannot tell" and
"we can tell, and it is bad" are different answers and must not be conflated.

---

## 2. Two analyses, because coverage is response-dependent

`SurrogateModelVersion.responses` is a **list** — *"per-response specs; a scalar
version lists scalar responses"* — and `fit_surrogate(factor_columns, response, …)`
fits **one** response at a time. A version therefore carries N GPs, each with its own
ARD length scales.

This matters: mass may depend almost entirely on thickness, while peak temperature
depends on everything. Over an **identical point set** those two GPs yield **different
coverage verdicts**. There is no single "GP coverage" for a design.

There is also a sequencing problem. Cohort preview asks *"is this fit to learn from?"*
**before** anything is committed — at which point no response has been chosen and no
GP exists. A GP-only design would require fitting a model to decide whether to fit a
model.

Both resolve the same way: **split the analysis.**

### 2.1 Design coverage — response-agnostic (`analyse_design`)

```python
def analyse_design(
    points: PointSet, envelope: Envelope, *, settings: CoverageSettings | None = None
) -> DesignCoverage
```

Purely geometric: model-free metrics (§4), diagnostics (§6), no GP, no response. This
is what **cohort preview** runs, and it is meaningful precisely because it makes no
claim about any particular output. It is also the cold-start path (§2.3).

**Every threshold arrives as `settings`.** §16 puts the tunables in `matofx.config`,
and `feedback_no_hardcoded_defaults_outside_config` forbids a second layer of them at a
function signature — so `settings=None` resolves to `CoverageSettings()`, whose
defaults are the config model's own. Nothing in `studies/coverage/` hardcodes a
number. The same parameter appears on `diagnose` and on `analyse_responses`.

### 2.2 Response coverage — per-response (`analyse_responses`)

```python
def analyse_responses(
    per_response: Mapping[str, ResponseModel],
    envelope: Envelope,
    *,
    points: PointSet,
    settings: CoverageSettings | None = None,
) -> tuple[ResponseCoverage, ...]
```

`points` is not redundant with the models. The per-factor strip is geometry, and
geometry is geometry — recomputing it per response would be the wrong kind of
independence. More concretely, a `Surrogate`'s `training_inputs` is **numeric-only**
(finding 21 again), so the categorical and pinned coordinates the strip and the
`edge` threshold need exist only on the `PointSet`. What genuinely differs per response
is the criterion, the headline number, the ARD relevance, and the runs proposed.

Run at **fit time**, when the responses are known and their models exist. One
`ResponseCoverage` per response, each carrying its own verdict, covering radius under
that response's metric, and suggested points. The report keeps them all and surfaces
the **worst** as the headline — a model is only as trustworthy as its least-covered
response.

Suggestions default to the worst-covered response. A user may choose another, and the
UI names which response drove the proposal — "these runs improve *peak temperature*"
is actionable; an unattributed list is not.

### 2.2.1 A response is not always one GP

`ResponseModel` is deliberately not `Surrogate`. Two shapes exist:

- **Scalar response** — one fitted surrogate.
- **Field response** (ADR-0008 §4 / ADR-0018) — fitted **per POD mode**: `field.py`
  carries `per_mode`, `mode_q2` and the mode energy weights. There is no single GP to
  interrogate.

For a field response, σ is the **energy-weighted aggregate** across the mode
surrogates, reusing the weights already computed for `field_q2`. That is the right
aggregation: a mode carrying 1 % of the field's energy should not drive a coverage
verdict as hard as the dominant one. If any mode was fitted with a method that has no
variance (§2.3), the whole response falls back to the model-free criterion rather than
aggregating over a partial set.

Field responses are therefore **in scope from slice 2**, not deferred — the NAFEMS
demo's temperature and failure-index field surrogates are exactly this case, and a
design that excluded them would exclude the demo.

### 2.3 Which criterion applied, and why

`SurrogateKind = Literal["gp", "gp_matern", "poly", "gbm"]` — and **only the two GP
kinds have a posterior variance.** `poly` and `gbm` have none, which is why the
exporter's ± gate is already GP-only. A GP-variance path that assumed otherwise would
break on a user-selectable option, and MOP's auto-best-metamodel picks by Q² without
regard to whether the winner carries uncertainty.

| Situation | Criterion | Reason |
|---|---|---|
| Cohort preview (no response yet) | `model_free` | nothing to fit a GP against |
| Response fitted `poly` or `gbm` | `model_free` | **no posterior variance exists** |
| Field response, any mode non-GP | `model_free` | aggregating over a partial set would misreport |
| `n_points < cold_start_ratio × n_active_factors` | `model_free` | ARD length scales are not yet trustworthy — steering by a compass calibrated with the compass |
| Fit time, GP kind, sufficient data | `gp_variance` **per response** | ARD spends budget on the axes the response actually responds to |
| Field response, all modes GP | `gp_variance` (energy-weighted, §2.2.1) | as above, aggregated by mode energy |

The criterion is always reported, never hidden. A model-free response still gets a
full verdict and usable suggestions — it loses ARD weighting, not the feature.

---

## 3. The envelope (`envelope.py`)

```python
@dataclass(frozen=True)
class FactorEnvelope:
    name: str
    kind: Literal["continuous", "integer", "categorical", "pinned", "derived"]
    scale: Literal["linear", "log"]  # continuous + integer; §3.2
    lo: float | None                 # continuous + integer + derived
    hi: float | None                 # continuous + integer + derived
    levels: tuple[str, ...] | None   # categorical
    pinned_value: Any | None         # pinned (constant input, §1.2)
```

`Envelope` = an ordered tuple of `FactorEnvelope` plus helpers: `normalise(points)`
maps continuous and integer factors to `[0,1]` (essential — raw units make distance
meaningless when thickness is in mm and load in N), `contains(point)`, and
`sample(n)` / `lattice()` for candidate generation.

Bounds default to the training set's observed spread and are then **user-editable**
(§7) — the ADR's declared-envelope decision.

```python
def build_envelope(
    points: PointSet,
    *,
    factor_spec: FactorSpec | None = None,
    descriptor: ToolDescriptor | None = None,
    overrides: Mapping[str, FactorOverride] | None = None,
) -> Envelope
```

**The descriptor is a required input, not a convenience.** `FactorType` is
`numeric | select | bool | file` — whether a numeric input is an **integer** is a
descriptor fact (`InputFieldType`), so §3.1's "descriptor-`integer`" rows cannot be
resolved from a `FactorSpec` alone. Without a descriptor an integer axis degrades to
`continuous`, which is a real loss (§3.3) but never a wrong answer; **integer-ness is
never guessed** from whole-valued observations, because a thickness that happens to
have been run at 1, 2, 3 mm is not a lattice.

`FactorOverride(lo, hi, scale, levels)` is the envelope form's backing type (§11) —
each field `None` meaning "leave as resolved". Overrides are validated: an inverted
range and a `log` scale over a non-positive span are both refused with a plain-English
message rather than silently producing `nan`.

### 3.1 Factor kind resolution

| Declared as | Kind | Domain from |
|---|---|---|
| `RangeVary` / `DistributionVary` on a continuous numeric | `continuous` | declared or observed min/max |
| `RangeVary` / `DistributionVary` on a descriptor-`integer` input | `integer` (§3.3) | min/max, snapped to the lattice |
| `ListVary` / `SubsetVary` / `FileListVary` | `categorical` | declared options |
| `bool` | `categorical` | two levels |
| `ColumnVary` | **by the factor's underlying `type`** (§3.4) | observed values |
| constant across the training set | `pinned` | the single observed value |
| the reserved `time` factor (`ingest.TIME_FACTOR`) | `derived` (§1.2.1) | observed timestep span |

### 3.2 Log-scaled axes are measured in log space

`DistributionVary` supports `uniform`, `normal` and **`loguniform`** (which validates
`low > 0`). A log-sampled factor normalised *linearly* is measured in the wrong space:
over `[1, 10000]`, the whole decade 1–1000 compresses into the first 10 % of the
normalised axis and reads as a tight cluster, while 5000–10000 reads as comfortably
covered. Suggestions would then pile into the top decade, and the verdict would be
confidently wrong — silently, which is the worse kind.

**Rule.** A factor sampled `loguniform` carries `scale="log"`; `normalise` maps it via
`log`, and `sample()` / suggestions are generated in log space and mapped back. The
scale is inferred from the `DistributionVary.dist` where a study exists, defaults to
`linear` for an inferred ad-hoc factor, and is **user-overridable on the envelope
form** — an archive of ad-hoc runs carries no record of how someone chose their values,
and a factor spanning several orders of magnitude is the visible clue.

`normal` distributions stay `linear`: they are unbounded in principle but the envelope
supplies real bounds, and a linear measure over those bounds is correct.

### 3.3 Integer factors are ordered discrete — a third treatment

Aligns with `designs/integer-parameter-handling.md` (Phases A–D shipped 2026-06-15)
and reuses `studies/_intsnap.py:snap_int`. Integer step is **always 1** per that
design's locked decision.

An input declared `integer` in the descriptor (`InputFieldType`) is **neither**
continuous **nor** categorical:

- **Ordered, so distance survives.** Ply count 3 is genuinely near 4 and far from 12.
  Treating integers as unordered levels would discard ordering the GP's ARD length
  scales actively use. The metrics of §4 and the GP of §5 operate on an integer axis
  normally.
- **But the domain is a finite lattice, so occupancy replaces radius when the lattice
  is small.** If the envelope is `[1,5]` and runs exist at 1, 2, 3, 4 and 5, the axis
  is **completely covered**. A naive continuous treatment would report holes at 1.5
  and 2.5, then propose running ply count 2.7 — not a valid input at all.

**Rule.** For an integer factor whose lattice cardinality is at or below
`coverage.max_lattice_cardinality`, coverage on that axis is **lattice occupancy** —
the fraction of attainable values present, with the missing values named explicitly
(they are the suggestions). Above that threshold — a mesh seed over `[1,1000]` — the
lattice is dense enough to behave continuously and the axis falls back to distance
metrics with snapping applied at suggestion time.

**And `envelope.sample()` generates candidates *on the lattice*, from slice 1.** §5.1
states this rule for *suggestions*, which arrive in slice 2 — but the rule is needed
one layer earlier, at candidate generation, or the **joint** covering radius of §4
measures distance to points like ply count 2.7 and reports holes at 1.5 and 2.5 that
§3.3 has just declared are not holes. A fully occupied `[1,5]` lattice would then read
as gappy in the headline number while its own per-factor row read `complete` — the two
halves of the report contradicting each other before any suggestion exists. So
`sample()` snaps every integer coordinate through `snap_int`, and §5.1 inherits the
behaviour rather than introducing it.

### 3.4 `ColumnVary` resolves by the underlying factor type

`column` is a pure marker with no declared domain — in `table` mode values come from
the explicit rows of `sampling_params.runs` — and `_TYPE_KINDS` permits it on
`numeric`, `select`, `bool` and `file` factors alike. The domain is recovered from
observed values; the *treatment* follows the factor type:

| Factor `type` | Treatment |
|---|---|
| `select` / `bool` / `file` | categorical levels |
| `numeric`, descriptor-`integer` | ordered discrete (§3.3) |
| `numeric`, continuous | continuous |

Bucketing a *continuous* numeric column into levels would be actively harmful: a
hand-built run table with thickness values 1.0 / 2.0 / 3.5 / 4.0 is a sparse
continuous sample, and calling it four balanced levels would report perfect coverage
on an axis that is barely sampled.

---

## 4. Model-free metrics (`metrics.py`)

Operate on the normalised continuous + integer sub-space, **excluding `derived` and
`pinned` axes** (see below). Normalisation honours each factor's `scale` (§3.2), so a
log-sampled axis is measured in log space.

| Metric | Meaning | Use |
|---|---|---|
| `maximin_distance(X)` | min pairwise distance — how tightly points crowd | clustering signal |
| `covering_radius(X, envelope)` | max over the envelope of distance-to-nearest-point — **the biggest empty hole** | the headline sparsity number |
| `centered_l2_discrepancy(X)` | deviation from uniform | overall spread |
| `nn_distance_distribution(X)` | nearest-neighbour distances | drives the UI histogram |
| `lattice_occupancy(X, factor)` | fraction of attainable integer values present | small integer axes (§3.3) |
| `trained_span(X, factor)` | observed min/max against the declared envelope | `derived` axes (§4.1) |

`covering_radius` is evaluated over a Sobol or LHS candidate set from
`envelope.sample(n)`, not a full grid — grid cost explodes past ~4 factors.

### 4.2 The score is referenced against what this many runs could achieve

> **Superseded by §4.4 (2026-08-27).** The reasoning below about `sqrt(d)/2`
> still holds — it is the *replacement* that turned out to be wrong, because a
> yardstick that moves with the archive's own size measures efficiency rather
> than coverage. Kept because §4.4 only makes sense against it.

A covering radius is meaningless without a yardstick, and the obvious one is wrong. An
earlier version scored against the unit hypercube's half-diagonal, `sqrt(d)/2` — the
radius a single centred point leaves. That looks dimension-aware and is not: an
achievable covering radius grows with dimension far faster than `sqrt(d)` does, so the
score collapsed as factors were added **however good the design was**. A *perfect*
254-point Latin hypercube scored `good` at 2 factors, `thin` at 4, and `poor` from 8 up.
That is §7.1's always-red light arriving through a door this document did not anticipate.

**Rule.** The yardstick is `reference_radius` — the covering radius the *same* machinery
achieves laying out the *same number of points* evenly over the *same envelope* — and
`coverage_score = reference / actual`, capped at 1. The score then answers the question
the user actually has: **"is this as well spread as this many runs could be?"** rather
than "is this dense in an absolute sense", to which for any interesting number of factors
the honest answer is always no.

### 4.3 Say when the headline is an estimate

`n_candidates` probes the space at `n_candidates ** (1/d)` points per axis: 4096
candidates is 64 per axis in two dimensions and **two** in twelve. Below
`_MIN_CANDIDATE_RESOLUTION` per axis the joint radius is a coarse estimate rather than a
measurement, and the report says so — pointing the reader at the per-factor rows, which
stay exact however many factors there are. Presenting a noisy number as precise is its
own species of false confidence.

### 4.1 Non-actionable axes are measured apart from the joint radius

A `derived` axis (§1.2.1) sits in the model's input space but not in the design space.
Folding it into the **joint** covering radius would report a hole that no design point
can fill, dragging the headline number down for a reason no suggestion can address —
and the headline number's whole job is to be actionable.

So derived axes are **excluded from the joint metrics** and reported individually with
the `span` measure: the trained min/max against the declared envelope, which is exactly
what the guard needs (§8) and exactly what tells a user their model covers 0–600 s of
soak. `pinned` axes are likewise excluded, carrying the `pinned` measure. Both set
`actionable=False` on their `FactorCoverage` so the UI offers the right remedy instead
of a suggestion button (§11).

### 4.8 Categorical factors: split or pool, decided by density

Splitting the point set per categorical level group is correct only while the groups
stay populated. Two selects of three levels is nine groups; three is twenty-seven. A
perfectly reasonable 40-run design becomes 1.5 runs per group and **every group reads
`poor`** — a false alarm, since a GP with encoded categoricals shares information
across levels.

**Rule.** Let `n_groups` be the product of level counts over categorical factors.

- `n_points / n_groups >= coverage.min_points_per_group` → **split**: continuous
  coverage computed per group, worst group drives the verdict.
- otherwise → **pool**: a single analysis over a mixed metric where a categorical
  mismatch contributes `coverage.categorical_mismatch_distance` (Gower-style) to the
  normalised distance.

**Balance is reported either way** — per-level counts never depend on this choice.
The report names which mode ran, because "worst group" and "pooled" verdicts are not
directly comparable.

---

### 4.4 The yardstick is a fixed standard, not the archive's own size

*(2026-08-27. Supersedes §4.2, which is kept above because its reasoning about
`sqrt(d)/2` still holds — it is only the replacement that changed.)*

§4.2 fixed an always-red light by referencing the score against an evenly spread design
**of the same size**. That removed the dimension collapse and introduced a subtler fault:
a ratio against a yardstick that moves with `n` is an **efficiency** — *"are these runs
arranged well for how many there are?"* — not a coverage measure. An efficiency
legitimately falls when runs are added, so the panel could answer *"is this fit to learn
from?"* worse after more data arrived.

It reached users as a suggestion plan reading `good` at 60 proposed runs and `poor` at
120. The pair responsible:

```
Fin thickness x Material conductivity, +60 -> +120 runs
  gap        0.1793 -> 0.1539   genuinely BETTER
  yardstick  0.0848 -> 0.0666   the bar rose faster
  score      0.473  -> 0.433    so the score fell
```

Confirmed on strictly **nested** point sets, which is what makes it arithmetic rather
than data:

```
             n=60    n=120   n=180   n=240   n=300
moving bar  1.000    0.941   1.000   0.978   1.000   <- backwards twice
fixed bar   0.933    0.987   1.000   1.000   1.000   <- never
```

There is a second consequence the first did not make obvious. In six dimensions the
reference design is *itself* poor, so the ratio of two poor numbers is a good one — which
is how a one-factor-at-a-time cross (§4.6) scored `good` for as long as it did.

**Rule.** The yardstick is `reference_radius(envelope, standard_runs_per_input * d)` —
an evenly spread design of a **stated** size, depending only on the dimension. Only the
radius then moves, and a covering radius is non-increasing as points are added, so
**monotonicity is a property of the arithmetic** rather than something to hope for. The
old number survives as `efficiency_score`: a labelled diagnostic that answers a real
question and does not set the light.

#### 4.4.1 The standard is measured, not chosen

`checks/coverage/calibrate_standard.py`. Seven design families (Latin hypercube, uniform,
full factorial; one-factor-at-a-time, clustered, diagonal ridge, two-level corners) at
four sizes with eight replicates, sweeping the standard from 8 to 60.

The sweep only became answerable after noticing what a standard **cannot** do. Since
`score = bar / radius` and the bar is identical for every design, changing it rescales
*every* score by one factor: it cannot improve separation — that is fixed by the radii —
it decides **where the floors fall**. So the requirement is placement, not margin:

- a well-designed archive must never read `poor`;
- a badly-designed one must read `poor`, or it cannot set the light.

At **30 runs per input**: worst good 0.529, best bad 0.368, the 0.45 floor between them
with a balanced +0.079 / −0.082 margin. No floor was moved to make this fit.

#### 4.4.2 The reference was a single random draw

The sweep exposed a defect in the thing being calibrated. `reference_radius` built **one**
Latin hypercube and measured it — and an LHS's own covering radius is a random variable,
its axes permuted independently and free to align badly. Across the sweep the bar read
`0.3283` at ten runs per input and `0.2443` at twelve: a *larger* ideal design producing a
26% smaller bar, impossible in expectation and landing directly on every verdict.

**Rule.** The median of `coverage.reference_replicates` layouts. Affordable because the
reference depends only on the envelope's **shape** and a point count — never on the
archive — so it is memoised on that shape: a whole pairwise sweep needs two or three
distinct layouts, and every option in a suggestion plan reuses one bar.

#### 4.4.3 The joint reading stops discriminating at seven inputs

Margin between a Latin hypercube and the worse of one-factor-at-a-time / clustered, at 20
runs per input:

```
d=2: 0.44   d=3: 0.34   d=4: 0.33   d=5: 0.34   d=6: 0.32
d=7: 0.20   d=8: 0.20   d=9: 0.19
```

It holds to six and collapses at seven, where a clustered design scores 0.79 —
comfortably `good`. This is not sampling noise (§4.3): with that many inputs no realistic
number of runs can fill the space, so every design leaves an enormous hole and the
readings converge.

**Rule.** Above `coverage.max_discriminating_axes` the joint score is reported with its
caveat and **does not enter the verdict**. Erring here would err *flattering*, which is
the one direction this ADR exists to prevent. The per-input rows and the pairwise pass
carry the verdict instead — two dimensions always have enough runs to judge, whatever the
tool's width.

---

### 4.5 Two ranges: what was simulated, and what the tool allows

The envelope defaults to the observed spread (§3), which makes every figure derived from
it **self-referential**: "98% covered" has always meant *98% of the range you chose to
run*. No reading in the report could say that a tenth of the tool's own range had ever
been tried. On the demo heat-sink archive `base_temperature_rise` sat at 60 out of a
declared 10–120, and every reading called it fine.

**Rule.** `FactorEnvelope` carries `declared_lo` / `declared_hi` from the descriptor
alongside the working `lo` / `hi`, and each factor row carries a **second** coverage
reading measured over those declared bounds. The panel shows both, per input, with their
own tags.

Two constraints fall out of honesty rather than convenience:

- only a **literal** bound is taken. `minimum_expr` / `maximum_expr` are formulas over
  sibling inputs, so the allowed range is a different interval on every row — there is no
  single denominator, and inventing one would misreport how much was explored. Such an
  input reads *no stated range*, which is distinct from *poorly covered*;
- values **outside** the declared bounds are clamped rather than dropped. A run at 406
  against a declared maximum of 400 still demonstrates the top of the range, and
  discarding it would report *less* coverage because of a run that proves *more*. It is
  the descriptor that is out of date, and §11 flags that separately.

---

### 4.6 Pairwise coverage — the level between one input and all of them

Coverage was measured at two scales: each input's own range (the per-factor strip) and
the whole joint space (the verdict). A real archive failed **in between**, and neither
scale could see it.

The demo heat-sink archive: 531 runs, six varying inputs, every per-factor row green,
overall verdict `good` — and *Base width* against *Material conductivity* plotted as a
cross, one horizontal line of runs and one vertical, the rest of the square empty.
Fourteen of its fifteen pairs score `thin` or `poor`; five below 0.35.

Why each neighbour is blind:

- a **marginal** is blind to structure by construction. In a cross both inputs *are*
  varied across their full range, so both rows read ~98% and both are right;
- the **joint** score is uninformative at that width for the reason in §4.4.3.

In **two** dimensions a few hundred points is plentiful, the reference is demanding, and
the cross is exposed immediately.

**Rule.** `analyse_pairs` scores every pair of movable numeric axes on its own 2-D
projection, against the same fixed standard (§4.4). Capped on **axes** rather than pairs
so the sweep stays a complete sweep of whatever it covers, and the report carries both
`n_pairs_examined` and `n_pairs_total` — a capped sweep presented as a full one is the
silent truncation this ADR is about.

**Only a `poor` pair enters the verdict.** A genuine hypercube produces `thin` pairs once
there are enough factors — its worst sits near 0.58 at ten — and letting one demote the
archive would mark good designs down for being *wide*, which is §7.1's always-red light
in a new hat. A `poor` pair is different: an entire two-input face was never explored.

`one_at_a_time` is a **separate diagnosis** from "one bad pair", because the remedies
differ. A handful of gap-filling runs fixes an awkward corner; a cross needs a designed
set that moves several inputs together, and telling somebody to "add some runs" there
would be actively wrong.

Calibrated before being allowed near the verdict, on the §4.4.1 design set: Latin
hypercubes and uniform-random designs produce **no** poor pair at 3, 6 or 10 factors;
one-factor-at-a-time and tight clusters produce poor on **every** pair.

---

### 4.7 A held-constant input can be opened up

An input that never varied is `pinned` (§1.2) and no suggestion moves it. That is right
about the archive and wrong about the *future*: "this was always 60" is a fact about what
has been run, not a decision that it must stay 60, and a user filling gaps may well want
the batch to open it up.

**Rule.** Giving a `pinned` axis an explicit range **promotes** it to a varying axis —
`continuous` or `integer` per the descriptor, recorded as `varying_kind` when the envelope
is built because by override time the descriptor is gone. It then joins the design space
properly: counted in the joint radius, swept in the pairwise pass, plotted, and moved by
suggestions, rather than being a special case each of those has to know about. Refused
where there is no numeric kind to return to — a pinned material name has no range to
sweep.

**The latent defect this exposed.** `PointSet.rows` carries only the *varied* factors — a
constant lives once in `constants`, not repeated per row — so a promoted column normalised
to all-`NaN`: the axis dropped out of the joint radius and every pair containing it went
**silently unjudged**, reported as "15 of 21 examined" with no reason given.
`Envelope.normalise` now reads through `points.values()`, which falls back to the
constant. The distinction had no consequence while such an axis was excluded from every
measurement, and became wrong the moment one could be opened up.

---

## 5. GP-variance coverage (`gp.py`)

Reuses the fitted GP from `fit_surrogate` (`studies/surrogate/__init__.py:221`).

```python
def variance_field(surrogate, envelope, *, n_candidates: int) -> VarianceField
def suggest_points(surrogate, envelope, *, n: int) -> list[dict]
```

`variance_field` evaluates posterior σ over a candidate sample. **σ contains no `y`
term** for fixed hyperparameters — it is distance-to-data warped by the kernel, so it
*is* a coverage measure, with ARD length scales telling it which axes deserve budget.

### 5.0 The claim is true of *relative* σ, not of raw σ

The sentence above is the load-bearing one in this design, and as written it is **false
of the GP this codebase actually fits.** `_make_estimator` builds
`GaussianProcessRegressor(..., normalize_y=True)`, so `predict(return_std=True)`
multiplies its standard deviation back up by the training response's spread before
returning it. Rescale a response by 3 — the same design, the same physics, different
units — and every σ triples. Measured directly: `y` and `3y` fit the *identical* kernel
and return σ in exact 3:1 ratio.

That breaks three things at once. A configured threshold on σ means something different
for mass in kg than for peak temperature in K. Two responses cannot be compared, which
is the whole point of §2.2. And the field aggregate of §2.2.1 would be wrong in a way
nobody would notice (§5.0.1).

**Rule.** Coverage uses **relative σ** — `σ(x) / std(y_train)` — everywhere. It is
`y`-free under a rescaling, dimensionless, comparable across responses, and the only
form in which `coverage.verdict_thresholds` and `coverage.edge_percentile` mean the same
thing twice. `Surrogate.y_scale` exposes the divisor.

Hyperparameters *are* still estimated from `y`, exactly as decision 6 of the ADR
acknowledges — a genuinely different response function learns different length scales
and so gets a different σ field. That is intended: it is what makes the criterion
response-specific. What is now excluded is the part that was never intended, where the
*units* of a response moved the verdict.

#### 5.0.1 The field aggregate must weight relative σ, not raw σ

POD mode 1 carries the largest coefficients by construction, so its raw σ is the largest
in absolute terms *regardless of how well that mode is covered*. Weighting raw σ by mode
energy therefore counts mode magnitude **twice** — once through the coefficient scale
and again through the energy weight — and the aggregate becomes a restatement of the
energy spectrum rather than a measurement of coverage. Relative σ removes the first,
leaving energy to do the weighting alone. `mode_energy_spectrum` is also renormalised
over the *retained* modes: it holds each mode's share of the **total** energy and so sums
to the retention cutoff (~0.99), not to 1.

#### 5.0.2 Conditioning must freeze the hyperparameters *and* skip y-normalisation

Greedy selection conditions on a candidate as though it had returned a value. For the
"zero solver calls" claim to hold, the pseudo-value must not matter. Freezing the fitted
kernel (`optimizer=None`) is necessary but **not sufficient**: with `normalize_y=True`
still on, a pseudo-response of 10⁶ changes `std(y)` and so rescales every σ. The
conditioned copy is therefore fitted with `normalize_y=False` on an already-standardised
response, which makes σ exactly `y`-free — verified to 8 decimal places against pseudo
values of 0 and 10⁶. Because its σ is *already* relative, the copy carries
`y_scale_override=1.0` so nothing divides by the spread a second time.

`suggest_points` is **greedy sequential**: take the max-σ candidate, condition on it
as a pseudo-observation, recompute σ, repeat. Never top-*N* — top-*N* returns *N*
adjacent points in the same hole. Because σ is `y`-free, an entire batch is planned
with zero solver calls. Existing points come from the surrogate's own training data;
there is no separate `existing` argument to fall out of step with it.

### 5.0.3 The greedy criterion is minimax, not maximin

The obvious walk is *"take the candidate furthest from everything chosen so far"*. That
is a **maximin** criterion — spread the points out — and it is the wrong one. The number
this feature reports is the **covering radius**: the furthest any point of the design
space sits from a run, which is **minimax**.

The two nearly coincide in two or three dimensions and come apart badly above about six,
because maximin drives every pick onto a **corner** of the cube — maximally distant from
everything, and covering almost none of the interior that the covering radius is
dominated by. Random points, which concentrate in the interior, cover better.

Measured against 200 random batches of the same size on a deliberately clustered design:

| factors | maximin walk beats | minimax greedy beats |
|---|---|---|
| 2 | 100 % | 98 % |
| 4 | 100 % | 100 % |
| 6 | 57 % | 100 % |
| 8 | 64 % | 100 % |
| 12 | **0 %** | **100 %** |

**Rule.** Each pick is the candidate that **minimises the resulting covering radius**
over a probe sample of the envelope — the textbook greedy for the k-centre problem, and
the one whose objective is the number the user is actually shown. The selection pool is
capped (`_MAX_SELECTION_CANDIDATES`) while the probe set stays the full sample: a coarser
search over the right objective beats a finer search over the wrong one.

The suggestion and the report **share a candidate seed**, so the greedy fills the holes
the headline number counts rather than holes measured on a different sample — which with
a sparse high-dimensional candidate set is not a subtle difference.

### 5.1 Suggestions obey the integer invariant

`designs/integer-parameter-handling.md` states the non-negotiable: *whenever a
parameter is sampled automatically, an integer parameter is always respected.*
Suggested runs are an automated sampler, so:

- the candidate pool is generated **on the integer lattice** for integer factors, and
  every emitted value passes `snap_int` (round half away from zero, then clamp);
- **already-run lattice points are excluded** — proposing a duplicate wastes a solver
  run and adds no information;
- when the remaining lattice is smaller than the requested batch, **the batch shrinks
  and a warning fires** — the established Phase-B behaviour (`[1,3]` with
  `batch_size=8` yields 3, no infinite loop). A fully occupied lattice yields **zero**
  suggestions on that axis, which is the correct answer: it is finished;
- `pinned` factors are emitted at their pinned value and never varied;
- **`derived` axes are skipped entirely** (§1.2.1) — a suggested point never carries a
  `time` coordinate, because no one can run a job at a time coordinate.

---

### 5.2 A suggested run must be one the tool would accept

*(2026-08-27.)*

§5.1 made suggestions obey the **integer** invariant. That was one rule out of a
family, and the rest were missing: the path sampled each varying axis inside its own
declared bounds, independently, and proposed the result. A descriptor says a great deal
more than *"each input lies in this box"* — `set_value` forces, formula-driven bounds
over sibling inputs, `require` validity rules, §F30 cross-field constraints, and
**sum-checks**.

Measured on `material_properties`, whose three percentages must total 100: **0 of 8**
proposed runs were submittable. Every one summed to something else — 83.8, 117.1, 71.0,
119.9, 51.8, 148.2, 125.9, 96.8.

It was invisible because §11.5 left the study as a draft. The batch was priced, shown
run by run and committed, and the points died later inside a generator nobody was
watching. The moment the commissioned study is *submitted* (§11.5) it becomes "I asked
for eight runs and got none".

**Rule.** `coverage/rules.py` applies two passes, in this order, because they are
different kinds of thing.

**A sum-check is repaired, not rejected.** It is an *equality*: filtering independently
sampled candidates against it rejects essentially all of them, which turns a broken
feature into an empty one. The group's **true degrees of freedom** are searched
instead — one member (the *balance*) derived as `value - sum(others)`. This is the fix
ADR-0009 §3.6 already made for the optimiser, and this module **reuses that machinery**
(`pick_balance_heuristic`, `apply_sum_derivation`) rather than restating it: a second
implementation of "what does this sum-check mean" is the drift §3's F1 is about.

Repair has a limit, and reaching it is a rejection rather than a silent clamp. Two
members at 90 leave the third needing −80, outside its own range; clamping would produce
a run violating the very rule the derivation exists to keep, so the point goes and the
reason is recorded.

**Everything else is rejected**, via `forms.resolve_for_generation` — **the same call
the study generator makes** on the same point. A batch that passes here cannot be
dropped there, which is precisely the failure this closes. Survivors come back
*resolved*, with forces written in, because that is what would actually run and showing
the user anything else would be showing them a run they are not about to make.

Two consequences of the mechanism worth stating:

- **the batch is over-drawn.** A sum-check rejects almost every independently sampled
  point, so the greedy plans `n × overdraw` candidates and the feasible ones are taken in
  greedy order — keeping the best of what survives rather than an arbitrary slice;
- **static `minimum` / `maximum` are not re-checked per point.** `resolve_for_generation`
  short-circuits to the cross-field layer when a descriptor declares no `dependencies`,
  and that is sound rather than a gap: points are generated inside the envelope, which is
  itself bounded by those limits, so they hold by construction. The guarantee is *"every
  proposed run passes the same check the generator applies"* — not *"every conceivable
  rule is re-verified from scratch"*.

**What is covered, and when.** Sum-checks and cross-field constraints run on every
point. Forces, effective (formula-driven) bounds, `require` rules and conditional
membership run whenever the descriptor declares `dependencies` — which is exactly when
those rules exist, so the short-circuit loses nothing.

Verified on a tool carrying four rules of different shapes (a cross-field constraint, a
`require`, a formula bound and a force), with the existing runs spread across the **full**
declared ranges so the rules genuinely bite: **8 of 8** proposed runs satisfy all four.
The control matters as much as the result — withholding the descriptor on the same seed
gives **6 of 8** violations, which is what makes the first number evidence rather than a
coincidence. The first version of that fixture clustered its runs where every rule
happened to hold; its control read 0, meaning the test could not have failed.

---

## 6. Diagnostics (`diagnose.py`)

```python
@dataclass(frozen=True)
class CoverageDiagnostics:
    n_points: int                     # after every exclusion below
    n_excluded_invalid: int           # ADR-0017 gate, from TrainingData
    n_excluded_filtered: int          # FactorFilter
    n_outside_envelope: int           # §1.3
    n_near_duplicates: int
    factor_correlations: dict[tuple[str, str], float] | None
    max_abs_correlation: float | None
    confounded: bool
    condition_number: float | None
    level_counts: dict[str, dict[str, int]]
    underpopulated_levels: list[tuple[str, str, int]]
    grouping_mode: Literal["split", "pooled", "n/a"]
```

`confounded` is the thresholded verdict (`max_abs_correlation > settings.
max_abs_correlation`), kept beside the raw number so the caller does not re-apply a
threshold the engine already owns — and so `None` (cannot tell) stays distinguishable
from `False` (checked, fine).

**Serialisation.** `factor_correlations` is keyed by a **tuple**, which JSON cannot
express, and §9 persists the whole report as JSON. The in-memory shape stays tupled —
it is the natural one for a pairwise matrix — and the report gains an explicit
`to_json_obj()` / `from_json_obj()` pair that flattens those keys to `"a|b"` on the way
out. Doing this at the boundary rather than changing the type keeps the engine
pleasant to use and puts one obvious place to look when slice 5 lands.

- **Confounding** — Pearson correlation over normalised continuous + integer factors
  plus the design-matrix condition number, omitted (`None`) below two such factors.
  Above `coverage.max_abs_correlation` the verdict carries a confounding warning
  **independent of** the sparsity verdict.
- **Near-duplicates** — a duplicate is **exact equality on every discrete axis**
  (integer, categorical, pinned) **and** within `coverage.duplicate_eps` on the
  continuous ones. Epsilon must never be applied to an integer axis: on `[1,5]` the
  normalised spacing is 0.25, and a generously tuned epsilon would flag genuinely
  distinct adjacent lattice points as duplicates.
- **Categorical balance** — per-level counts; any level below
  `coverage.min_level_points` flagged. This is the silent failure of the three (40
  CFRP / 2 GFRP looks perfect on every continuous axis), so it is a **first-class
  verdict input**, not a footnote.

---

## 7. The report

```python
@dataclass(frozen=True)
class FactorCoverage:
    name: str
    kind: Literal["continuous", "integer", "categorical", "pinned", "derived"]
    measure: Literal["covering_radius", "lattice_occupancy", "level_balance", "pinned", "span"]
    scale: Literal["linear", "log"]
    value: float                      # normalised radius | occupied fraction | min-level share
    missing_values: tuple[str, ...]   # named gaps: integer lattice or categorical levels
    actionable: bool                  # False for derived/pinned — no design point can fix it
    verdict: Verdict

@dataclass(frozen=True)
class DesignCoverage:
    criterion: Literal["model_free"]
    verdict: Verdict
    message: str                      # plain-English headline, §1.4's required text
    covering_radius: float
    coverage_score: float             # 0..1
    per_factor: tuple[FactorCoverage, ...]
    warnings: tuple[CoverageWarning, ...]
    diagnostics: CoverageDiagnostics

@dataclass(frozen=True)
class ResponseCoverage:
    response_name: str
    response_kind: Literal["scalar", "field"]
    surrogate_method: str                  # gp | gp_matern | poly | gbm
    criterion: Literal["gp_variance", "model_free"]
    verdict: Verdict
    message: str                           # plain-English, like DesignCoverage's
    covering_radius: float                 # worst *relative* sigma when gp_variance
    coverage_score: float
    per_factor: tuple[FactorCoverage, ...]
    suggested_points: tuple[dict, ...]
    warnings: tuple[CoverageWarning, ...]  # e.g. a lattice this response has exhausted
    edge_sigma_threshold: float | None     # §8 — computed once here, never at predict
    ard_relevance: dict[str, float] | None # which axes this response responds to

    @property
    def available_statuses(self) -> tuple[str, ...]:
        """('inside','outside') without a posterior variance, else all three."""

`ard_relevance` is `1 / length_scale` normalised to sum to 1. The ADR names ARD as the
GP criterion's whole advantage over a geometric one — *"it has learned which factors the
response actually responds to"* — but the earlier dataclass gave that knowledge nowhere
to surface, so the advantage was claimed and then discarded.

`available_statuses` makes §8's *"the response states which statuses are available"*
into a field rather than a sentence. A `poly` or `gbm` response returns
`('inside', 'outside')`; a GP response returns all three.

@dataclass(frozen=True)
class CoverageWarning:
    code: str                         # confounding | categorical_imbalance | ...
    message: str                      # plain-English, per feedback_users_are_not_modellers
    factor: str | None = None         # the axis it is about, where there is one

@dataclass(frozen=True)
class CoverageReport:
    verdict: Verdict                  # the worst of design + every response
    design: DesignCoverage
    per_response: tuple[ResponseCoverage, ...]   # empty at cohort preview
    envelope: Envelope
    source_kind: SourceKind           # parametric | optimisation | cohort
    warnings: tuple[CoverageWarning, ...]
    computed_at: datetime
```

`Verdict = Literal["good", "thin", "poor", "insufficient"]`.

`source_kind` is `matofx.studies.orm.SourceKind` —
`Literal["parametric", "optimisation", "cohort"]`, the closed vocabulary
`StudyResult.source_kind` already enforces. An earlier draft of this section invented
`scenario | parametric_study | optimisation_study | cohort`, which matches nothing in
the codebase; §7.1's framing keys off the real value.

`CoverageWarning.factor` is what lets §11's per-factor strip say *"a longer run or
finer snapshot retention"* against the right axis. `FactorCoverage` deliberately keeps
no message field: a row of the strip is a measurement, and prose about it belongs with
the other prose.

A single scalar could not carry per-axis occupancy (§3.3) or per-response results
(§2), which is why the report is structured rather than flat. Thresholds are
config-tunable.

**The verdict never blocks a fit** — it is recorded and travels with the model (§9). A
user may knowingly build on thin data; they may not do so unknowingly.

### 7.1 The verdict is framed by where the data came from

An **optimisation study concentrates its runs near the optimum** — that is the
algorithm working correctly, not a design flaw. A cohort drawn from one will read
`poor` every time, and a red light that is always red is a light users learn to ignore.

`source_kind` is already carried on the version, so the report states the reason
alongside the verdict: *"These runs came from an optimisation study, so they cluster
near the optimum. That is expected — it just means this model should be trusted near
there, not across the whole envelope."* Same measurement, honest framing, and it points
at the real remedy (a space-filling batch) instead of implying someone did something
wrong.

Every warning carries plain-English text per `feedback_users_are_not_modellers` — "Two
of your inputs move together, so the model can't tell their effects apart", not
"VIF = 8.4".

---

## 8. The extrapolation guard (predict time)

The highest-value half of the feature: a build-time check fires once, a prediction
guard fires every time someone leans on the model.

On every scalar predict (`models/runtime.py`,
`POST /models/{id}/versions/{vid}/predict` and the field-predict sibling) the response
gains:

```json
"support": {"status": "inside" | "edge" | "outside", "sigma": 0.0123, "reason": "..."}
```

- `outside` — the point fails `envelope.contains`, **including a `pinned` factor
  supplied at a different value** (§1.2) and a `derived` axis beyond its trained span
  (§1.2.1 — a field predicted at `t = 5000 s` from runs that soaked for 600 s).
  Deterministic, cheap, no model needed.
- `edge` — inside the envelope, but local σ exceeds the `coverage.edge_percentile`
  percentile **of σ evaluated at the training points**. That reference set is the
  definition of "the trained region"; a percentile taken over an envelope-wide sample
  would move with the envelope and is not the same threshold.

**The threshold is computed once at fit and stored** as
`ResponseCoverage.edge_sigma_threshold` (§7). It is never recomputed at predict time:
that would cost a training-set evaluation per prediction, and `keep_training_data` is
optional — a version that discarded its training data could not recompute it at all.

**Non-GP responses report `inside` / `outside` only.** `poly` and `gbm` have no
posterior variance (§2.3), so `edge` is not computable and
`edge_sigma_threshold` is `None`. The envelope half of the guard — which is the half
that catches the genuinely dangerous case — works for every method. The response
states which statuses are available rather than silently never returning `edge`.

### 8.1 What the exported tool emits

`models/exporter.py` / `tool_synthesis.py` add **two** declared outputs:

| Output | Kind | Purpose |
|---|---|---|
| `support_sigma` | numeric | the predictive σ — **numeric, so ADR-0017 output validity can carry a declared max** and flag runaway extrapolation automatically |
| `support_status` | string | `inside`/`edge`/`outside` for a human reading the job record |

The earlier plan to make the *status* the validity-checked output does not compose:
ADR-0017 operates on numeric scalars and field arrays, not status strings. The numeric
σ is the checkable quantity; the string is informational.

Adding outputs changes the synthesised descriptor, so an exported tool gains these
only on **re-export** (a new `ToolVersion` through the normal `test → released` gate).
Existing exported tools are unaffected and keep working.

The exported-tool case matters most: that surrogate runs unattended inside a workflow,
where nobody is watching a badge.

---

## 9. Persistence (`models/orm.py`, migration `0095`)

New nullable JSON column `coverage_report_json` plus `coverage_verdict` (short string,
indexed for filtering) on **`surrogate_model_version`** (`models/orm.py:140`).
Nullable so existing versions are untouched — an absent report renders as
"not assessed", **never** as "good".

The declared envelope is stored inside the report and is the single source for §8 and
§10. No second copy.

No new table: the report is a few KB, read whenever the version is read, and has no
independent lifecycle.

**Written once, never amended.** A refit mints a *new* version
(`_add_version(ordinal=latest.ordinal + 1)` — confirmed in slice 5, finding 50), so a
stored report always describes the fit it was computed for. There is no path by which one
goes stale, which is why no invalidation logic exists here.

**What is deliberately *not* stored.** The training points (the scatter matrix is a live
view of a cohort the version already references, and a stored copy would bloat the row and
go stale as the cohort grows) and the nearest-neighbour distances (one float per run,
driving a histogram of the same live data). **What is stored beyond the obvious:** the
verdict *thresholds* the report was measured against — they are config-tunable, so a bare
`poor` means nothing a year later without the floor that produced it.

**The fit path has to carry the training data.** `_fit_responses` returns only the fitted
surrogates, and a `Surrogate`'s `training_inputs` is numeric-only — so a report rebuilt
from the fit alone would be blind to categorical imbalance, the silent one of the three
diseases (finding 49). `_FitResult` therefore carries the `TrainingData` the fit consumed.

**Computing the report never fails a fit.** The verdict is evidence *about* a model, not a
gate on producing one (§7), so a coverage failure logs and leaves both columns NULL rather
than losing a fit that otherwise succeeded.

### 9.1 Cohort-preview caching

An `auto_grow` cohort re-resolves its query on **every read**. Recomputing coverage
per page load — a GP fit in the worst case — is unacceptable per
`feedback_performance_and_security_equal`.

Cohort-level reports are cached against a key of
`(cohort_id, hash(resolved rows), hash(envelope), hash(settings))` and recomputed only
when that key changes. The design pass (§2.1) is cheap enough that this is mostly a
courtesy; it becomes load-bearing if a response pass is ever run at preview.

**Keyed on the resolved rows, not the cohort definition.** An `auto_grow` cohort's
membership *is* a query result, so a key built from the saved query would keep serving
yesterday's verdict after new runs started matching it — the precise failure this feature
exists to report, reintroduced by its own cache. The **settings** are in the key for the
same reason: the floors are tunable, and a verdict reached under different floors is an
answer to a different question.

**Bounded and LRU-evicted.** A dict keyed on a moving membership has no natural ceiling;
the working set is whichever cohorts someone is currently looking at, so a small cap is
right.

---

## 10. V&V tie-in (ADR-0011)

`vv_record.applicable_range_json` is today authored by hand and documentary. When a
V&V record is created against a surrogate model version carrying a coverage report,
the declared envelope is **offered as the applicable range** (pre-filled, editable,
provenance noted). The envelope stops being an assertion and becomes the same object
the guard enforces.

---

## 11. UI — cohort preview panel

Filters and controls **above** results per `feedback_filters_at_top_tool_name_only`.

1. **Envelope form** — one row per factor, pre-populated from the training set's
   observed spread. Continuous/integer: min/max. Categorical: levels in scope. Pinned
   factors listed read-only with a note that the model cannot vary them. Header asks
   the plain-English question: *"Over what range should this model be trusted?"*
2. **Verdict card** — traffic-light verdict, the headline "biggest gap" in plain
   terms, the criterion used, the grouping mode, and the warning list. Where points
   were excluded (§1.1, §1.3) the counts appear here, so "62 of 100 runs" is never a
   surprise.
3. **Per-factor strip** — one row per factor from `per_factor`: continuous axes show
   the gap, integer axes show occupancy with the missing values named, categorical
   axes show level balance.
4. **Pairwise scatter matrix** — the visual payoff. Existing points per factor pair,
   empty regions shaded. Plotly, themed via `apply_theme_to_plotly`
   (`project_dark_light_theme_toggle`). Capped at the top-N factors by Sobol index
   with a selector for the rest — an 11-factor matrix is unreadable.
5. **"Suggest runs to fill the gaps"** — spinner for *N*, a response selector when
   more than one response exists (defaulting to the worst-covered, and naming it),
   then a preview of the proposed points and a **"Create parametric study"** button
   handing them to ADR-0001 as a governed, auditable batch.

   **Gated on the existing study-create permission**, and hidden rather than disabled
   for a user without it. At cohort preview the batch is model-free (§5, finding 43) and
   names no response; the response selector appears only where responses exist. This button spawns real solver jobs and spends real compute
   — the analysis itself is read-only and stays open to anyone who can see the cohort,
   but committing runs is a separate right and the design says so rather than
   inheriting it silently.

Where a factor is `derived` or `pinned` (`actionable=False`), the per-factor strip says
what would actually fix it — a longer run or finer snapshot retention for a thin `time`
axis, a new cohort for a pinned input — instead of offering a suggestion that cannot
exist.

Per `feedback_no_box_shadows`, cards use a 1px ~35%-opacity white border.

---

### 11.5 Fill the gaps — pricing a batch, and committing it

*(Referenced throughout the engine since slice 5; written up 2026-08-27 with the
2026-08-26/27 rework folded in.)*

The one control on the panel that *acts* on everything above it, and therefore the one
that must not read as a fifth heading. It is a bordered card with an accent rule, not a
dense expansion.

**It does not ask the user how many runs they want.** *How many?* is the thing they
opened the panel to find out; a spinner asks them for the answer. `suggestion_plan`
prices a ladder of batch sizes against the same measurement the verdict uses and lets the
answer be read off the curve.

- **Cheap despite appearances.** The suggestion walk is a greedy max-min, so the first
  `k` picks of an `n`-point batch **are** the `k`-point batch. One walk at the largest
  size, then one analysis per prefix.
- **The ladder scales with the tool** (`default_plan_sizes`): about 1, 2, 5, 10 and 20
  runs per varying input. A fixed `(3, 5, 8, 12, 20)` wasted its top rungs at two inputs
  and was a rounding error at twelve. Runs-per-input is the unit the user guide already
  reasons in.
- **A typed size is honoured.** Somebody who knows they can afford seven runs asks about
  seven.
- **The table leads with the gap, not the score.** The gap is monotone in `n`; the
  efficiency reading (§4.4) is not, and the table says so rather than looking broken when
  a larger batch rates lower.
- **`reaches_good` marks the knee** — the smallest offered batch that reaches `good`, and
  only when there is a threshold left to cross. An archive already reading `good` has
  none, and flagging a row "enough" there would invent one.

**The plan calls `analyse_design`, it does not recompute a score.** An earlier version
derived its own number from the joint radius; the moment the pairwise pass (§4.6) could
move the verdict, the two disagreed and the table offered *"Well covered"* directly
beneath a card reading *"Poorly covered"* — about the same archive, in the same panel.
What the table promises is now what the panel will say once the runs land, by
construction rather than by two implementations agreeing.

#### Which space gets filled

Two controls, because they change the answer rather than the presentation.

- **Fill across** — *the range these runs already cover* (default) or *the full range the
  tool allows*. The second commissions runs in regions the archive has never visited and
  its figures are measured over the wider space, so the same batch reads worse against it.
  It is not the default: widening what a model claims should never happen because a
  control defaulted to it.
- **Also vary the inputs held constant so far** — §4.7. Offered only for numeric inputs
  the tool gives bounds for.

Both are expressed as **envelope overrides**, which is why they need no engine special
case: the plan, the suggestions, the report behind the preview and the committed study all
already take an envelope. Changing either invalidates a priced batch on screen, because a
batch belongs to the scope it was priced in.

#### Reading the batch before committing it

- Values are quoted — and generated — at the input's **own precision**. A proposed
  `4.360013149188567` for an input stepping in hundredths is not a display blemish; it is
  a design point the form would reject, quoted to fifteen digits the tool does not have.
- **See where these runs would go** draws the result: a stacked histogram per input and
  the full pairwise matrix, existing runs against proposed. A table of numbers cannot show
  that a batch lands squarely in the empty half of a square, which is the only thing worth
  knowing before spending solver time.
- The preview re-reads the report **under the chosen envelope**, since a widened scope or
  an opened-up constant changes which axes exist.

#### Committing

**Create a parametric study and add to Cohort** — permission-gated on study creation
(unlike the analysis, which stays open), landing a **draft** in a project the caller
chooses. Nothing reaches the solver until somebody submits it.

The form answers three things before the click rather than after it:

- the project is a **picker over the caller's own projects**, not a typed id. A typed id
  asks the user to know a database key and accepts any integer they enter — including one
  they have no access to, which surfaces as a permission error only once the batch has
  been priced;
- the name is pre-filled `"<tool label> - Fill coverage gaps"`. Named after the *tool*
  because a project accumulates these, and three studies called "Fill coverage gaps" say
  nothing about which is which;
- a name already used in the chosen project turns the field **red with a reason**. The
  server would reject it anyway, but only after the user pressed a button they had every
  reason to expect to work. The check is a read of the project's studies, cached on
  selection so typing costs no further requests.

The study is committed against the envelope **stored with the priced batch**, not re-read
from the controls: a scope toggled after pricing would otherwise commission runs over a
region the user never saw costed.

**And the runs come back.** `job_cohort.auto_include_study_ids` (migration `0096`) records
the link, and membership resolution unions in those studies' completed jobs. Needed for
both modes and for different reasons: a fixed cohort is frozen to `member_job_ids` and
would never take them, and an auto-grow cohort's own filters — an input range, a project
list, a date window — can perfectly well reject the very runs it commissioned, which
presents as a study finishing with the verdict unmoved and nothing on screen to explain
it.

**Resolution-time, not event-time.** No completion hook: it would have had to fire
reliably from the runner, survive a crash mid-batch and be idempotent. A union at read
time is none of those and cannot drift out of step with what the jobs actually did — and
it is the rule `auto_grow` already works by. `excluded_job_ids` still wins; a curator who
removed a run meant it.

---

## 12. Where it is invoked

| Surface | Call | Purpose |
|---|---|---|
| **Cohort preview** (`studies/cohort/service.py`) | `analyse_design` | "Is this fit to learn from?" before anyone commits — response-agnostic, cached (§9.1) |
| **Surrogate create** (`models/service.py:ModelService`) | `analyse_design` + `analyse_responses` | full report persisted onto the version (§9) |
| **Model detail page** | stored report | evidence, not recomputation |
| **Predict** (`models/runtime.py`) | `envelope.contains` + point σ | the extrapolation guard (§8) |
| **Cohort `auto_grow`** | cache-key change → re-analyse | flag when new runs changed the verdict |

### 12.1 The cohort-preview call site

`CohortService.coverage_preview` resolves membership, lazily populates
`study_result`, takes the training set through `collect_training_data`, and runs
`analyse_design` over it. Three details are load-bearing:

- **No response is chosen yet**, so the `ResponseSpec` names none. Only
  `TrainingData.response` depends on it — the factor columns the design pass measures
  are the same either way. This means a per-response pass at fit time may train on
  *fewer* points than the preview measured, where that response is missing on some
  runs. That is the honest difference between *"could this teach anything"* and
  *"what did this particular output learn"*, and it is stated rather than papered over.
- **The cohort's own `input_ranges` / `cross_field_predicate` are applied**, so the
  preview measures the set a fit would actually see rather than a more flattering
  superset.
- **The tool descriptor is loaded, and a stale one degrades rather than breaks.**
  Coverage needs it only to tell an integer input from a float; a snapshot that no
  longer parses drops integer axes to continuous — a real loss, never a wrong answer —
  and must not take the panel down with it.

**Two feature gates, not one.** The cohorts router is already gated on
`surrogate_family`; the coverage route carries `design_space_coverage` as well. An
install can have cohorts and surrogates without having bought coverage, and a single
gate would have to choose which of those two customers to get wrong.

---

## 13. Slicing

| # | Slice | Content |
|---|---|---|
| 1 | **Engine** | `studies/coverage/` — point-set resolution (§1), envelope, model-free metrics, categorical split/pool, diagnostics, report. Pure unit-tested code, no surfaces, **no GP** |
| 2 | **GP pass** | `analyse_responses` incl. **field responses** (energy-weighted per-mode aggregate) and method-driven criterion selection (`poly`/`gbm` → model-free), variance field, greedy suggestion with the integer invariant and derived-axis skip |
| 3 | **Cohort preview** | Envelope form + verdict card + per-factor strip + scatter matrix |
| 4 | **Suggestion → study** | "Suggest runs" → response selector → preview → create parametric study |
| 5 | **Persistence** | Migration `0095`, report written at fit, model detail, cache + `auto_grow` re-analysis |
| 6 | **Predict guard** | `support` on predict responses; viewer badge; `support_sigma` / `support_status` on re-export |
| 7 | **V&V tie-in** | Pre-fill `applicable_range_json` from the envelope |

Slice 1 deliberately excludes the GP so the response-agnostic path — the one cohort
preview and every cold-start case depend on — stands alone and is testable without a
fitted model.

Slices 1–4 are the NAFEMS demo beat. Slices 5–7 are what makes it a customer
capability — **the conference chooses what gets rehearsed, not what gets built**
(`NAFEMS_Talk_Plan.md` §7).

**All seven shipped 2026-08-24.** 41 findings surfaced during the build (§15, findings
21–61) against 23 from three pre-build review passes. The split is the useful part:
findings 21–42 and 48–51 were *interface* mismatches, which tests against the real types
catch; 43–47 and 52–58 were **numerical** — confidently wrong numbers with correct types,
passing every fixture test — and every one of those was found by running against a real
250-run archive rather than by reading.

---

## 14. Tests

**Point set**
- Coverage consumes `TrainingData`, not membership: a cohort of 100 with 38 dropped
  (invalid + filtered) measures 62 points, and the exclusion counts appear in the report.
- Factors inferred via `varied_columns` for a cohort with no `FactorSpec`.
- A constant input becomes a `pinned` factor, gets no coverage measure, and makes the
  guard return `outside` when a prediction supplies a different value.
- Points outside a narrowed envelope: still counted in `n_outside_envelope`, excluded
  from the metric, and **not** clamped onto the boundary.
- Degenerate sets — n = 0, 1, 2, and all-factors-constant — return `insufficient`
  with a message, never an exception and never `poor`.

**Metrics**
- Known designs, known answers: a full-factorial grid scores well; all points in one
  corner scores `poor`; covering radius shrinks monotonically as points are added;
  maximin is invariant to factor ordering and to unit scaling after normalisation.
- Categorical split vs pool: above the density threshold the mode is `split`; below it
  `pooled`; a 40-run design over 27 groups must **not** report every group `poor`.
  Balance is reported in both modes.

**Derived axes and scale**
- The reserved `time` factor is `derived`: it appears in the envelope, a prediction
  beyond its trained span reads `outside`, and **no suggested point ever carries a
  `time` coordinate**. Assert on a time-resolved field surrogate, the demo's shape.
- A thin `derived` axis produces a remedy message about run length / snapshot
  retention, never a proposed design point.
- A `loguniform` factor is normalised in log space: over `[1, 10000]`, points at
  1, 10, 100, 1000, 10000 read as **evenly covered**, and the same points read as
  severely clustered under linear normalisation. Assert both, so the fix is pinned by
  the contrast.
- Scale is inferred from `DistributionVary.dist` where a study exists, defaults to
  `linear` when inferred from an ad-hoc cohort, and honours a user override.

**Surrogate methods**
- `poly` and `gbm` responses select `model_free` and still return a full verdict with
  usable suggestions — assert the GP path is never entered and nothing raises.
- Those responses report `edge_sigma_threshold is None` and their guard returns only
  `inside` / `outside` — never a silently-absent `edge`.
- A field response aggregates σ by **mode energy weight**; a field response with any
  non-GP mode falls back to `model_free` rather than aggregating a partial set.
- MOP auto-selecting a `poly` metamodel does not break coverage.

**GP**
- **The `y`-free property.** Fit two GPs on identical `X` with different `y`; assert
  the variance field is unchanged for fixed hyperparameters. This is the load-bearing
  claim of the whole design — test it explicitly.
- **Per-response divergence.** One point set, two responses with different ARD length
  scales, must produce different `ResponseCoverage` verdicts, and the report's headline
  must be the worse of the two.
- Greedy vs top-N: greedy spreads, top-N clusters, on a design with one large hole.
- Cold-start fallback: below the ratio `model_free`, above it `gp_variance`.
- Cohort preview runs with **no fitted model at all** and still returns a verdict.

**Integer axes**
- A fully occupied lattice (`[1,5]`, all five present) reports **complete** coverage
  and yields **zero** suggestions on that axis — the test that fails under a naive
  continuous treatment.
- A partially occupied lattice names the missing values exactly.
- Every suggested value is integral (assert no `2.7` escapes); duplicates of existing
  runs are never proposed; a batch larger than the remaining lattice shrinks with a
  warning rather than looping.
- Above `max_lattice_cardinality` the axis uses distance metrics with snapping —
  assert the crossover at the boundary.
- Ordering preserved: distance from ply count 3 to 12 exceeds 3 to 4.
- Duplicate detection uses **exact equality** on integer axes — adjacent lattice
  points are never duplicates however `duplicate_eps` is tuned.

**Diagnostics**
- Confounding: a design that is perfectly space-filling *along a diagonal* produces a
  confounding warning while sparsity reads `good` — the two verdicts must not collapse.
- Correlation omitted (`None`, not `0.0`) with fewer than two continuous factors.
- Categorical imbalance: a 40/2 split flags the underpopulated level even with
  excellent continuous coverage.

**Envelope**
- Built from `FactorSpec` across all six vary kinds, including the `ColumnVary` split:
  `select`/`bool`/`file` → categorical levels; continuous `numeric` → a continuous
  range (assert it is **not** bucketed into levels); descriptor-`integer` → lattice.

**Surfaces**
- Guard: outside envelope → `outside` regardless of σ; inside + high σ → `edge`;
  `edge` threshold derived from σ at training points, not an envelope-wide sample, and
  **read from the stored report** — assert predict never touches the training set, and
  works on a version with `keep_training_data=False`.
- An optimisation-sourced cohort reads `poor` **and** carries the source-framing
  message (§7.1) — assert the message is present, not just the verdict.
- Suggestion → study is hidden without the study-create permission; the read-only
  analysis remains visible.
- Exported tool emits numeric `support_sigma` and string `support_status`; ADR-0017
  can declare a max on the former; existing exports are unaffected until re-export.
- Persistence round-trips through migration `0095`; an absent report renders
  "not assessed", never "good".
- Cache: an `auto_grow` membership change invalidates; an unchanged read does not
  recompute.
- **Seeding via the ORM**, never raw SQL (`feedback_test_seeding_via_orm`).

---

## 15. Review findings (2026-08-24) and resolutions

| # | Finding | Resolution |
|---|---|---|
| 1 | "The fitted GP" ambiguous — coverage is per-response | §2: `analyse_design` + `analyse_responses`; report keeps all, headlines the worst |
| 2 | Cohort preview had no GP to use — chicken and egg | §2.1: preview is model-free and response-agnostic by construction |
| 3 | Envelope provenance named `FactorSpec`, which ad-hoc cohorts lack | §1.2: factors inferred via `varied_columns`; three sources tabulated |
| 4 | Coverage measured on cohort membership would overstate | §1.1: consumes `TrainingData` post ADR-0017 and `FactorFilter` |
| 5 | Flat report could not express integer occupancy or per-response results | §7: `FactorCoverage` / `DesignCoverage` / `ResponseCoverage` |
| 6 | Training points outside a narrowed envelope unhandled | §1.3: still train, excluded from the metric, counted, never clamped |
| 7 | Categorical group-splitting fragments combinatorially | §4.2: split-or-pool by density, mode reported |
| 8 | `support` string could not be validity-checked by ADR-0017 | §8.1: numeric `support_sigma` + informational `support_status` |
| 9 | `auto_grow` re-analysis cost unstated | §9.1: cache keyed on members + envelope |
| 10 | Degenerate point sets unhandled | §1.4: `insufficient` verdict with floors |
| 11 | `duplicate_eps` could swallow adjacent lattice points | §6: exact equality on discrete axes |
| 12 | `edge` percentile ambiguous | §8: percentile of σ **at the training points** |
| 13 | Stale column-resolution note | folded into §3.4 |

**Second review pass (same day).** Three of these were only visible by reading
`factors.py` / `ingest.py` closely — the pattern is that the *core argument* held while
the *interface with existing machinery* kept breaking, because MATOFX already has more
shape than a fresh design assumes.

| # | Finding | Resolution |
|---|---|---|
| 14 | Reserved `time` factor would be treated as a design variable and generate "run a job at t = 347 s" | §1.2.1: new `derived` kind — measured and guarded, never suggested on, remedy stated in the right language |
| 15 | `poly` and `gbm` have no posterior variance; GP path impossible for two of four kinds | §2.3: method-driven criterion selection; §8: those responses guard on `inside`/`outside` only |
| 16 | `loguniform` factors measured in linear space — silently wrong coverage and suggestions | §3.2: `scale` on the envelope, log normalisation, user-overridable |
| 17 | Field responses are fitted per POD mode — no single GP to interrogate | §2.2.1: energy-weighted aggregate over mode surrogates; fields in scope from slice 2, not deferred |
| 18 | `edge` threshold recomputed per predict; impossible when `keep_training_data=False` | §7 / §8: computed once at fit, stored as `edge_sigma_threshold` |
| 19 | Optimisation-sourced cohorts always read `poor` — a permanently red light gets ignored | §7.1: `source_kind` framing alongside the verdict |
| 20 | "Create study" spawns real compute with no stated gate | §11: gated on the existing study-create permission, hidden not disabled |

**Third pass — writing slice 1's tests (§19's recommended first action).** Two of these
are proven by a failing test rather than argued; the rest surfaced from reading the real
types while writing assertions against them. The pattern from the second pass repeated
exactly: the argument held, the interface with existing machinery did not.

| # | Finding | Resolution |
|---|---|---|
| 21 | `TrainingData.factor_columns` is **numeric-only** — a varied `select`/`bool`/`file` input never reaches the training data, so categorical balance (the silent failure) is unmeasurable | §1.1.1: additive `categorical_columns`, row-aligned; **proven by a failing test** |
| 22 | `FactorFilter` drops are silently `continue`d — §6's `n_excluded_filtered` has no source, so "62 of 100" cannot be broken down | §1.1.1: additive `n_excluded_filtered`, counted separately from the validity gate; **proven by a failing test** |
| 23 | `n_rows` means post-validity on the scalar collector and pre-validity on the field one | §1.1.1: coverage derives `n_points` from the columns and never reads `n_rows` |
| 24 | §7's `source_kind` vocabulary (`scenario \| parametric_study \| …`) matches nothing in the codebase | §7: use `studies.orm.SourceKind` — `parametric \| optimisation \| cohort` |
| 25 | The `time` column is a **0-based snapshot index**, not seconds — "t = 5000 s from 600 s runs" is a sentence the platform cannot produce | §1.2.1: every derived-axis string speaks in snapshots; index→time mapping is a possible later refinement, not an assumption |
| 26 | Integer-ness is a **descriptor** fact (`InputFieldType`), not a `FactorSpec` one (`numeric \| select \| bool \| file`) — §3.1's signature could not resolve its own table | §3: `build_envelope` takes the `ToolDescriptor`; without it an integer axis degrades to continuous, and integer-ness is never guessed from whole-valued observations |
| 27 | Candidates were continuous, so the **joint** covering radius would report holes at 1.5 / 2.5 on a lattice §3.3 had just called complete — the two halves of the report contradicting each other | §3.3: `envelope.sample()` snaps integer coordinates from slice 1; §5.1 inherits rather than introduces the rule |
| 28 | `factor_correlations` is tuple-keyed and §9 persists the report as JSON | §6: keep the tuple shape in memory, flatten to `"a\|b"` in `to_json_obj()` at the boundary |
| 29 | `analyse_design`'s signature carried no `settings`, but §16 puts every threshold in config | §2.1: `settings: CoverageSettings \| None = None`, resolving to the config model's own defaults; nothing in the engine hardcodes a number |
| 30 | `DesignCoverage` had nowhere for §1.4's required plain-English `insufficient` message | §7: `message: str` on `DesignCoverage` |
| 31 | `FactorCoverage` had nowhere for §11's remedy text on a non-actionable axis | §7: `CoverageWarning.factor`; the strip stays a measurement, prose lives with prose |

**Fourth pass — writing slice 2's tests.** The first finding here is the most serious in
the document's history: the sentence the entire GP criterion rests on is false of the GP
this codebase actually fits. It was invisible to four readings and obvious on the first
run.

| # | Finding | Resolution |
|---|---|---|
| 32 | **Raw GP σ is not `y`-free.** `_make_estimator` uses `normalize_y=True`, so `predict` multiplies its std back up by the response's spread — rescale `y` by 3 and every σ triples, breaking cross-response comparison and any configured threshold | §5.0: measure **relative σ** (`σ / std(y_train)`) everywhere; `Surrogate.y_scale` exposes the divisor. **Measured, not argued**: identical kernel, exact 3:1 σ ratio |
| 33 | The field aggregate weighted **raw** σ by mode energy, so mode 1's coefficient magnitude counted twice and the aggregate restated the energy spectrum instead of measuring coverage | §5.0.1: weight *relative* σ; also renormalise `mode_energy_spectrum` over the retained modes, since it sums to the ~0.99 cutoff rather than to 1 |
| 34 | Freezing the kernel is **not sufficient** for `y`-free conditioning — with `normalize_y=True` still on, a pseudo-response of 10⁶ rescales every σ, and the "zero solver calls" claim fails | §5.0.2: `Surrogate.conditioned_on` refits with `optimizer=None` **and** `normalize_y=False` on a standardised response; verified `y`-free to 8 dp. `y_scale_override=1.0` stops the copy being divided twice |
| 35 | `analyse_responses(per_response, envelope)` could not build the per-factor strip or the `edge` threshold — `training_inputs` is numeric-only, so categorical and pinned coordinates live only on the `PointSet` | §2.2: the signature takes `points` and `settings` |
| 36 | ARD was named as the GP criterion's whole advantage, then given nowhere to surface | §7: `ResponseCoverage.ard_relevance` |
| 37 | §8's *"the response states which statuses are available"* was a sentence, not a field | §7: `available_statuses` property |
| 38 | The implementation grew a `suggest_points(..., existing=)` argument, which §5 had **explicitly forbidden** (*"there is no separate `existing` argument to fall out of step with it"*) — caught by a test asserting already-run lattice points are never re-proposed | Removed; already-run points are read from the model's own `training_inputs`. Recorded because it is the one case so far where the *design* caught the *implementation*, rather than the other way round |

**Fifth pass — slice 3, the first real surface.** Notably *quieter* than slices 1 and 2:
four findings, none of them a false claim, all of them plumbing. That is the expected
shape once the engine's contract with the codebase has stopped moving.

| # | Finding | Resolution |
|---|---|---|
| 39 | `collect_training_data` **requires a `ResponseSpec`**, but cohort preview is response-agnostic by construction (§2.1) — there is no response to name yet | §12.1: the preview passes a spec naming no response. Only `TrainingData.response` depends on it; the factor columns, which is all the design pass measures, are identical either way. The consequence is stated rather than hidden: a per-response pass at fit time may train on **fewer** points than the preview measured |
| 40 | `CoverageReport` carried no points, so §11.4's pairwise scatter matrix — *"the visual payoff"* — could not be drawn from a report | §7: `CoverageReport.design_points`. A verdict and a radius cannot be turned back into a scatter plot |
| 41 | `CoverageError` was not in the API's error map, so a user's own inverted envelope surfaced as a 500 instead of the plain-English 400 the message was written to be | Registered in `api/errors.py` (both the status map and the handler registration — the two are separate lists, and only adding to the first is silently inert) |
| 42 | §16 named the feature toggle but not that the cohorts router **already** has a different one; a single gate would either expose coverage to installs without it or hide cohorts from installs with them | §12.1: two gates, `surrogate_family` router-wide and `design_space_coverage` on the coverage route alone |

**Sixth pass — slice 4, run against real data.** 254 `bracket_stress` runs from the
NAFEMS chain, 12 varying factors. The first two findings are **quantitative** — the
engine gave confidently wrong *numbers*, not wrong types — and no unit test on a
two-factor design could have surfaced either. They were found by scoring a known-perfect
design and by racing the suggestions against a random control.

| # | Finding | Resolution |
|---|---|---|
| 43 | §11.5's suggest button lives on **cohort preview**, where by construction no model is fitted and no response chosen — but `suggest_points` requires a `ResponseModel` | New `suggest_design_points`: same candidate machinery, same §5.1 invariants, driven by geometry alone. Its batch carries **no** `response_name`, because attributing it to a response would be a claim the path cannot make |
| 44 | **The coverage score was dimension-driven, not design-driven.** Referenced against `sqrt(d)/2`, a *perfect* 254-point Latin hypercube scored `good` at 2 factors and **`poor` from 8 up** — §7.1's always-red light, arriving by an unanticipated route | §4.2: reference against `reference_radius`, what an evenly-spread design of the *same size* achieves. A perfect LHS now reads `good` at every dimension tested (2–12) and a corner cluster `poor` at every one |
| 45 | **The greedy optimised the wrong criterion.** Max-min-distance is *maximin*; the covering radius is *minimax*. Above ~6 factors maximin drives every pick onto a corner: against 200 random batches it won 100 % at 2 factors and **0 %** at 12 — worse than random, on the metric the panel headlines | §5.0.3: greedy k-centre — each pick minimises the *resulting* radius. Now 97–100 % at every dimension tested, measured on an **independent** candidate sample |
| 46 | Suggestions were planned over one candidate sample and measured against another; with a sparse high-dimensional set the two barely overlap | The suggestion seed is now the report's `_CANDIDATE_SEED`, so the greedy fills the holes the headline counts |
| 47 | A joint radius over 4096 candidates in 12 dimensions is **two probe points per axis** — an estimate presented as a measurement | §4.3: `candidate_resolution`, and a `coarse_estimate` warning pointing the reader at the per-factor rows, which stay exact |

**Seventh pass — slice 5, persistence.** Two of these settle questions §19 had carried as
unverified since the first draft.

| # | Finding | Resolution |
|---|---|---|
| 48 | **`coverage` was already taken — twice.** ADR-0008 §7.3 has `ModelService.coverage_for_version` and `GET /models/versions/{id}/coverage`, both meaning the in-form *training-range* gauge. Reusing either name would have silently changed what an existing surface shows | Distinct names throughout: `coverage_report_for_version`, `GET /models/versions/{id}/design-coverage`, `ModelVersionCoverageResponse`. Two things are called coverage in this codebase and they stay separately correct |
| 49 | **The fit path discards the categorical columns.** `_fit_responses` returns only the fitted surrogates, and a `Surrogate`'s `training_inputs` is numeric-only — so a report rebuilt at fit would have been blind to a 40-CFRP / 2-GFRP split, exactly the gap finding 21 closed on the cohort path | §9: `_FitResult` carries the `TrainingData` the fit consumed, and `_add_version` builds the report from it |
| 50 | **The refit lifecycle — confirmed, at last.** §9 assumed a refit creates a new version rather than mutating one, and §19 listed it as unverified through four slices | `refit` calls `_add_version(ordinal=latest.ordinal + 1)`. A report is therefore written once and never amended, and there is no stale-report path to guard. Pinned by a test so it stays true |
| 51 | **The §9.1 cache key — assembled, with a correction.** Keying on the cohort's stored *definition* would keep serving yesterday's verdict after new runs matched an `auto_grow` query, which is the precise failure this feature exists to report | §9.1: key on the **resolved rows**, plus the declared envelope *and* the verdict thresholds — both change the answer. LRU-bounded, because a dict keyed on a moving membership has no ceiling of its own |

**Eighth pass — slice 6, the guard, run on a real fit.** The pattern of slice 4 repeated
exactly: unit tests on fixtures passed, and pointing it at a real 250-run model found
three *numerical* defects in minutes.

| # | Finding | Resolution |
|---|---|---|
| 52 | **The stored report had no per-response half.** §12 says surrogate create runs `analyse_design` **and** `analyse_responses`; slice 5 stored only the design pass, so `edge_sigma_threshold` was never written and the guard could **never return `edge`** on any real model — silently binary on GP fits fully capable of the third state | `_coverage_columns` takes the fitted surrogates and runs both passes. Found by noticing every verdict on the real model was `inside`/`outside` |
| 53 | **Every GP response scored `poor`, whatever the design.** The score was `threshold / worst_sigma` — sigma *at* the training data (~0.0014 measured) over sigma at the emptiest corner (~1–30) — always ~0. Finding 44's always-red light, in the per-response pass | §2.2: score between two measured ends — `1 - (worst - trained) / (prior - trained)`. The **prior is measured, not assumed**: ~33 on the real bracket GP, and fit-dependent because it is the kernel amplitude relative to the response's spread |
| 54 | **A tenth of the training runs read `edge`.** The threshold is a percentile *of the training distribution*, so `edge_percentile=90` puts 10 % of the training runs above it **by construction** — and a GP interpolates its own data, so those sigmas are near-identical and the split is arbitrary. Predicting at a run you had already done said "the model is less certain here than usual" | §16: default raised to **99**, with the property documented rather than hidden. It is a percentile, so a small fraction always sits above; that is why `edge` is advisory, not a gate |
| 55 | **`exporter.py` / `tool_synthesis.py` — verified at last.** §19 listed it as unverified from the first draft through five slices: whether the synthesised descriptor accepts a string output beside a numeric | It does — `OutputFieldType` allows both, and the descriptor round-trips through the model that governs installed tools. Pinned by a test so it cannot regress |
| 56 | **A second extrapolation control already existed.** `block_extrapolation` (ADR-0008 §7.1) tightens the input bounds to the **raw training range** so form validation hard-rejects. Once an envelope is declared — and narrowing it is the expected edit — the two disagree about what "the range" is, and the form would accept points the guard reports `outside` | §8.1: where a coverage report exists, `block_extrapolation` tightens to the **declared envelope**. The two controls stay complementary (one prevents, one reports) but now agree on the range |
| 57 | An exported tool runs in its own subprocess with **no database**, so the guard has nothing to read | §8.1: the report is baked into the bundle manifest. Read from the manifest, not the sha-verified payload — it is metadata *about* the fit, not part of the model whose bytes the checksum protects |
| 58 | A tool whose own response is named `support_status` would have produced two outputs sharing one name — an invalid descriptor | The support outputs yield to an existing response of the same name |

**Ninth pass — slice 7, the V&V tie-in.** The last slice, and its findings are all one
shape: §10 said "the declared envelope is offered as the applicable range" without
checking that the two could express the same things. They cannot.

| # | Finding | Resolution |
|---|---|---|
| 59 | **The schemas do not match.** `applicable_range_json` is `{factor: {"min", "max"}}` — numeric bounds only. An envelope also carries **categorical levels** and **non-numeric pinned values**, which those bounds cannot express. Transferring only what fits would leave the V&V record silently claiming applicability to materials the model has never seen: a *wider* claim than the guard enforces, which is this feature's own failure mode turned inside out | §10: what cannot be expressed is **named** — `unexpressible` on the offer, and spelled out in the note ("material only covers cfrp, gfrp"), so the curator completes it by hand rather than being told nothing was lost |
| 60 | A **pinned numeric** input has no range in the ordinary sense, and skipping it would drop a real constraint | It becomes a **zero-width** bound, `[v, v]`. The validation covers exactly that value and no other, and that is what the column's only vocabulary can say. On the real bracket model, 11 of 18 axes transfer this way |
| 61 | A **derived** axis (a snapshot index) is not an "operating condition" in the ordinary V&V sense, so it looked like it should be excluded | Included. A field validated over snapshots 0–5 does not cover snapshot 500, and a V&V claim that omits the bound implies it does. The guard already enforces it; the record should say the same thing |

### 15.1 Adversarial stress pass (2026-08-25)

All seven slices had shipped, ~1013 tests were green, `mypy --strict` was clean and the
feature had been validated against the real demo archive. The pass below deliberately
attacked the finished thing: every tool in the archive, hostile and degenerate inputs,
invariant properties over hundreds of random designs, and each surface checked against
every other. It found **seventeen** further defects, seven of them in production paths that
no test exercised. None was a typing error; none raised in the fixture suite.

| # | Finding | Resolution |
|---|---|---|
| 62 | **A dict-valued input crashed the whole analysis.** A `material` input (ADR-0007) arrives as `{'material_id': 1, 'name': 'Iron', ...}`, and `_n_distinct` put it in a `set` — `TypeError: unhashable type: 'dict'`. The cohort panel 500s for every tool that uses the materials database; **143 runs in the demo archive do exactly this**. Every fixture used floats and strings | `canonical_key()` — a stable, hashable identity for any input value, via `json.dumps(sort_keys=True)`. Anything that groups or de-duplicates input values goes through it, never through the value |
| 63 | A dict level's `str()` is the whole record, and those strings are shown to users in the level-balance row and the "barely any runs used X" warning | `level_label()` prefers `name` / `label` / `alias` / `id`, so the level is `"Iron"` |
| 64 | **Suggested runs omitted categorical axes entirely**, so a suggested point failed the envelope's own `contains()` check and was missing an input the tool requires — a run nobody could submit. Slice 4's tests covered *pinned* passthrough but never a *varied* categorical | `denormalise` emits every categorical axis, **cycling levels across the batch** — which is also what an imbalanced categorical axis needs |
| 65 | **Fixing 63 created a divergence.** `level_label` was single-sourced across `envelope` / `diagnose` / `report` but `guard.py` still keyed levels by `str()`. Containment was implemented **twice**, and the two copies drifted: the guard rejected **138 of 143 real training runs** as a level "this model never saw", dumping the entire material record into the message. A model refusing to vouch for the runs it was fitted on | `_axis_violation` now **delegates the decision** to `_axis_contains` and owns only the wording. One rule, one implementation — the structural fix, not another synchronised copy |
| 66 | **A missing value became a phantom run at the envelope minimum.** `_unit` returned `0.0` for a NaN/inf coordinate, so a row with a missing value was placed at the bottom of the axis — a run that does not exist, sitting in exactly the region the analysis is supposed to report as *empty*. A NaN therefore **improved** the score | `_unit` returns NaN; `finite_rows()` drops such rows from every metric; `CoverageDiagnostics.n_unmeasurable` counts them. Adding a NaN now makes the score slightly *worse*, which is correct — one fewer usable run |
| 67 | "Cannot be placed" and "outside the declared range" were conflated, which would tell a user to widen an envelope when the real problem is missing data | `_n_outside` measures only placeable rows; `n_unmeasurable` is reported separately, in the API and in the panel headline |
| 68 | **An all-missing input became a pinned factor whose value was NaN**, and `NaN != NaN`, so `contains` could never return True again — the tool's own suggestions failed its own guard | `_usable()` gates what may become a constant. An input with nothing usable in it constrains nothing |
| 69 | **An input with no value recorded anywhere vanished without a word** — neither factor nor constant, so no axis, no per-factor row, no warning, no count, and the report still read `good`. A user looking for it would simply not find it | `PointSet.unusable_names` + a `unusable_input` warning naming it. It is precisely the input a model will later be asked to extrapolate along |
| 70 | **The exported tool's guard reported `outside` on every prediction**, including at its own training points, naming an input the form did not have. Categorical factors never became descriptor inputs (the training matrix is numeric-only — finding 21) and non-numeric held constants were explicitly skipped, so the guard received an **incomplete point** — and an incomplete point cannot be claimed as inside. Measured on the real archive: `bracket_stress` and `material_properties` both always-outside, `heat_transfer` (all-numeric) unaffected. **The always-red light of §7.1, in the one surface that runs unattended** | `synthesise_descriptor` emits categorical envelope axes as `select` inputs and non-numeric held values as single-option `select`s; the generated orchestrator stops coercing every input with `float()` (bools excluded, matching the guard's own `_as_float`). All three real archives now read `inside` at their own training points |
| 71 | **An unknown verdict passed straight through the tolerant reader** into a response model whose `Literal` rejects it — so a report written by a *newer* release 500s an older API. The forward-compatible read path defeated itself one layer down | `_as_verdict()` clamps to `insufficient` — "we cannot tell" — the only degradation that cannot overclaim |
| 72 | **`coverage_score` is a ratio, and a ratio alone is not comparable between two reports.** The yardstick (`reference_radius`) rises with the number of runs, so an archive topped up with badly placed runs scores *lower* while genuinely covering more. Nobody reading two stored reports a month apart could tell which of the two had moved | `reference_radius` recorded on the report and exposed in the API. The behaviour itself is correct and deliberate (finding 44) and is now **explained in the help** — "I ran more jobs and the verdict didn't improve" |
| 73 | **The guard's verdict rendered as an error message.** `string` has been a legal `OutputFieldType` since ADR-0008, but the job page dispatched only `number` / `table` / `file` and dropped everything else into `"(unsupported output type)"`. The synthesised surrogate declares `support_status` as a string — so every exported surrogate job displayed **"Prediction support: (unsupported output type 'string')"** where the verdict should be. §8's entire user-facing half, in the one place a prediction reaches a person. Compounding it, `support_chip` / `support_note` / `support_is_reportable` had been written and tested in slice 6 and imported by **nothing** | A `string` branch on the job page (so *any* tool may declare one), with `support_status` rendered as the badge plus the reason that names the offending input. The decision lives in a pure `support_badge()` helper, because this module's tests cannot drive a NiceGUI page context and a badge whose logic sat inside the renderer would be a badge nothing could check |
| 74 | **The 3-D predicted-field viewer had no support statement at all.** §8's guard reached users only through an exported tool's job outputs. The viewer is the *other* place a prediction happens — and the one where somebody actively drags sliders, which is exactly how you leave the region the runs covered without noticing. Dragging `time` past the last trained snapshot is the textbook §8 case and the viewer said nothing | `field_predict` now evaluates support and returns it; the badge sits above the plots and redraws on every slider move. The sigma is the field response's **energy-weighted** relative sigma across its mode surrogates (§2.2.1) — the same quantity whose distribution set the stored threshold — so `edge` is genuinely reachable and not merely inside-or-outside. Measured on a clustered fixture: 0.0011 at training points, 0.0051 in the gap |
| 75 | **`outside` was carrying two meanings.** A model with no recorded envelope returns `outside`, which is the right conservative answer for an ADR-0017 validity rule — but the badge built from it read **"Outside the trained range"**, asserting something nobody checked. Finding 67's shape again: there, "cannot be placed" was being counted as "outside the declared range" | No fourth status (which would ripple through the `Literal`, the API schema, the runtime outputs and any validity rule already written). The fact rides on the field that already exists for it: `available_statuses` is **empty** when nothing can be assessed. The conservative status stays for rules; a surface holding the full verdict draws no badge. An *absent* status list is deliberately different from an empty one — an exported tool's job outputs do not carry it and keep their verdict |
| 76 | **Coverage blocks the event loop.** `coverage_preview`, `coverage_suggest` and the fit-time `_coverage_columns` (which runs the *GP* pass) all call NumPy-heavy analysis directly inside an `async def`, with no `to_thread`. Measured on the real archive: **1.85 s** for the panel and 0.84 s for suggestions on 708 runs — during which every other request on the server stalls. The convention exists in the same file (`models/service.py` offloads the surrogate fit with an explicit comment about the page "breaking and refreshing"); coverage did not follow it | **Fixed.** All three call sites offloaded with `asyncio.to_thread`. Guarded by an **AST** test that flags any call to `analyse_design` / `analyse_responses` / `suggest_design_points` / `_coverage_columns` lexically inside an `async def` — with a self-check proving the detector flags both regression shapes (inline, nested-async) and permits all three legal offload shapes (by-name, closure, sync helper). A wall-clock threshold was rejected: flaky on a loaded box, and it does not say what went wrong |
| 77 | **The headline radius is a sampled estimate, and it under-reads.** Against a 64x denser probe it is low by up to **13%** at 4–10 factors, and low is the *flattering* direction — the archive looks better covered than it is. On the real archives, −2.8% to −10.2%. Across 8 arbitrary probe seeds the `coverage_score` moves by >0.05 on **36 of 40** designs | Verified not to reach the user's decision: the **verdict flipped on 0 of 40** designs across probe seeds, and survived a 32x denser probe on all five real archives — the thresholds are far enough apart that probe noise never crosses one. The traffic light is sound. But `coverage_score` is exposed in the API and stored on the version as a bare 3-decimal number, so **anything that consumes it as a precise quantity — a ranking, a KPI, an automated gate — is reading ±10% noise.** **Fixed in three parts.** (a) The `coarse_estimate` warning fired only below a resolution of 3.0 — i.e. **8+ factors** — so five, six and seven varying inputs showed a precise-looking number 6–8% optimistic in silence. Re-measured properly (6 designs per dimension over uniform / clustered / Latin-hypercube shapes): median error −3.8% at 4 factors, −6.5% at 5, −8.1% at 7. Threshold moved to 5.5, firing from 5 factors, and moved **into `matofx.config`** where a customer-tunable value belongs. (b) The report now records `n_candidates`, so two reports written under different settings are not mistaken for comparable. (c) The API schema states the precision explicitly on `coverage_score` and `covering_radius`, and the help explains it |
| 78 | **A missing *categorical* value was counted nowhere.** Findings 66–68 did this for numeric axes; the categorical half was never done. A categorical contributes no column to the normalised matrix, so a run whose `material` is `null` was measured as though nothing were wrong — while the predict-time guard called that same run **outside**, `None` not being a level the model ever saw. One real run in the demo archive (`bracket_stress`, a null `temperature_field`) does exactly this, and the two surfaces disagreed about it | `_n_unmeasurable` now takes the point set and the envelope, and a run unplaceable on **any** axis — numeric or categorical — is counted. Found by the cross-surface harness *while writing the testing guide*, because the guide claimed “0 problems” and the harness said 1 |

**Is the non-monotonicity a defect?** Measured, not argued. Over 60 random archives:
running the batch **the feature itself suggests** lowered the score in 2 of 60 and
regressed the *verdict* in **0 of 60**. Topping up with the same number of *random* runs
lowered the score in 25 of 60 and the verdict in 1. The covering radius — the absolute
measure — improved in every single case. So the advertised loop is sound; the surprise is
real but confined to random top-up, and the honest fix was documentation plus recording
the yardstick, not changing a score that is right.

**What this pass says about the method.** The eleven split cleanly by *what found them*:

- **Real data found 62–65 and 70.** Fixtures contained floats and strings; the archive
  contained dicts, 12 pinned inputs and 253-of-254 degenerate axes. No amount of fixture
  work would have produced a `material` dict.
- **Hostile inputs found 66–69 and 71** — NaN, inf, an all-missing column, a malformed
  stored row. Each is one line of input and each broke something.
- **Invariant properties found 72.** Determinism, row-order, unit and origin invariance
  all held over 150 random designs; monotonicity did not, and the failure was informative
  rather than a bug.
- **Cross-surface comparison found 65 and 70**, and nothing else could have. Both are
  *disagreements* — two surfaces giving opposite answers about the same data — and a
  test that exercises one surface at a time cannot see them by construction.

The recurring shape is **a rule implemented twice**. 65 and 70 are both that: containment
in two places, and "what inputs exist" in two places. Where two modules must agree, the
durable fix is to make one of them call the other, not to synchronise two copies — a
synchronised copy is a copy that has not drifted *yet*.

The second shape is **silence**. 66, 67, 68 and 69 all end with something disappearing:
a run placed where it isn't, a run dropped from a count, an input with no axis. For a
feature whose entire purpose is to say "your archive is thinner than it looks", quietly
dropping data is the one bug class that attacks its own premise.

---

**What the earlier pass said about the method.** Slices 1–3 found *interface* mismatches, which
tests against real types catch. These two are **numerical**, and the only things that
found them were a known-good input (a perfect Latin hypercube — what should this score?)
and an adversarial control (200 random batches — can we beat chance?). Neither is a unit
test in the usual sense; both are cheap. Where a feature reports a *number*, the design
review should ask what that number does on an input whose right answer is already known,
and whether it beats the null.

---

## 16. Config

New keys in `matofx.config` — no defaults at function signatures
(`feedback_no_hardcoded_defaults_outside_config`):

`coverage.min_points_for_analysis` · `coverage.cold_start_ratio` ·
`coverage.duplicate_eps` · `coverage.min_level_points` ·
`coverage.min_points_per_group` · `coverage.categorical_mismatch_distance` ·
`coverage.max_abs_correlation` · `coverage.verdict_thresholds` ·
`coverage.n_candidates` · `coverage.edge_percentile` ·
`coverage.max_lattice_cardinality`

Gated by `features.design_space_coverage` per
`feedback_per_customer_feature_toggles` — 404-when-off on the API, hidden in the UI.

## 17. Dependencies

**None new.** GP from `scikit-learn` (already in stack, BSD), maximin / discrepancy /
correlation in `numpy`, candidate sampling from the existing space-filling generator
in `studies/generate.py`, plots via Plotly (already vendored).

## 18. Documentation (ships with the code, per CLAUDE.md)

- `docs/user/` — a new chapter on reading a coverage verdict and acting on it; a
  paragraph in the surrogate chapter on the predict-time support badge; a note in the
  cohort chapter that coverage is measured on the runs that will actually train.
- `docs/admin/` — the config keys and the feature toggle.
- `ARCHITECTURE.md` §16 — a sub-section for the coverage engine and the guard.
- `ROADMAP.md` — status entry.
- `designs/vv-tracking.md` — note the applicable-range pre-fill.
- `designs/output-validity.md` — note that exclusions now surface in coverage.

## 19. Where the residual risk sits (read this before slice 1)

Three review passes found 13, then 7, then 3 defects. The last pass found only
self-inflicted ones — numbering, a missing rule for a concept the same pass had added —
rather than fresh mismatches with the codebase. The two earlier passes each found real
interface breaks. That change in the *kind* of finding is the signal: the core argument
has stabilised, and further re-reading of this document has hit diminishing returns.

**What the reviews kept finding was not flawed reasoning but unexamined machinery.**
MATOFX already has more shape than a fresh design assumes: four surrogate kinds (two
without variance), three distributions (one logarithmic), a reserved `time` factor,
per-mode field fits, optional training-data retention. Each was assumed away by an
earlier draft and each is now handled.

**The code not yet read closely, and therefore where the next mismatch most likely
hides:**

- `models/exporter.py` / `tool_synthesis.py` — the two new declared outputs (§8.1).
  Whether the synthesised descriptor accepts a string output alongside a numeric, and
  how re-export interacts with the `test → released` gate, is asserted here but not
  verified.
- The **refit lifecycle** — whether a refitted version recomputes coverage, or inherits
  a stale report. §9 assumes refit creates a new version; that is not confirmed.
- `studies/cohort/service.py` — the preview call site and whether the cache key of §9.1
  can actually be assembled there.

**Recommended first action: write slice 1's tests, not more design.** Tests against the
real types will surface remaining interface mismatches faster and more reliably than
further reading — the `time` factor, the `poly` kind and the `loguniform`
normalisation would each have failed on first run.

**Done, 2026-08-24 — and the prediction held.** 135 slice-1 tests found eleven further
mismatches (§15, findings 21–31), two of them caught by a failing test rather than by
argument. Notably, none of the three places this section named as the likely next
mismatch was where they were: the breaks were in `ingest.py`'s numeric-only column
filter and in `factors.py`'s type vocabulary — both files an earlier pass had *already*
read closely. The lesson is narrower than "read more": a type read for one purpose is
not a type understood for another, and only executing against it closes that gap. The
three unread areas above (`exporter.py` / refit lifecycle / `cohort/service.py`) remain
unverified and belong to slices 5–6.

**Slice 2 confirmed it again, and harder.** Seven more findings (32–38), the first of
which invalidated the sentence the whole GP criterion rests on — *"σ contains no `y`
term"* — because `normalize_y=True` had never been read as load-bearing. Four review
passes and a slice-1 build all walked past it; a single `assert` comparing `y` to `3y`
found it in one run. **Where a design makes a quantitative claim, the cheapest possible
experiment against the real library beats any amount of re-reading** — the probe that
settled it was fifteen lines and ran in seconds.

## 20. Open items

- ~~Coverage blocks the event loop~~ — **fixed 2026-08-25** (finding 76).
- ~~`coverage_score` is precise-looking~~ — **addressed 2026-08-25** (finding 77). The
  number is still a sampled estimate and always will be; what changed is that the caveat
  now fires where the error is, the probe density is recorded, and the imprecision is
  stated in the schema and the help. **It remains unfit for ranking, trending or gating** —
  use the verdict, which did not flip once across 40 designs × 8 probe seeds.
- **Postgres is still unexercised at runtime.** Every coverage test that touches the
  database is parametrised sqlite/postgres and the postgres half skips without
  `MATOFX_TEST_POSTGRES_URL`, so migration `0095` and its two columns have only ever *run*
  on SQLite. Partially closed 2026-08-25 by `tests/db/test_coverage_migration_dialects.py`,
  which compiles the DDL for both dialects without a server and asserts no migration
  anywhere reaches for a `postgresql.*` type. That catches a dialect mistake; it does not
  replace one run of the suite against a real Postgres, which remains **the last thing to
  do before a customer install**.
- **Nothing exercises concurrent load.** The offload fixed the stall; what is untested is
  several users on the panel at once, and the deliberate race the cache now permits (two
  concurrent misses both compute, identically, and the second overwrites the first).
- **The scalar GP fit path has not been re-validated on a real archive** since the NaN /
  `finite_rows` changes. The model-free path has, on all five archives; the field GP path
  has, on a fixture.

- ~~The guard's wording helpers are not rendered anywhere~~ — **resolved 2026-08-25**. They
  are now wired into the **job record**, which is where a prediction actually reaches a
  person: an exported surrogate runs as a job. Investigating it turned up finding 73 — the
  verdict was not merely unstyled, it was rendering as `"(unsupported output type
  'string')"`. The 3-D **field-predict** viewer gained its own badge the same day
  (finding 74, §16.81), redrawn on every slider move, so §8 now reaches both places a
  prediction happens.

- ~~Field surrogates out of scope~~ — **resolved 2026-08-24**: in scope from slice 2
  via the energy-weighted per-mode aggregate (§2.2.1). What remains genuinely open is
  narrower: whether a POD **mode coefficient** deserves its own support statement
  independent of the factor space. Revisit after slice 6.
- Cohorts tolerate `major.minor` version bands, so a training set may span tool
  versions whose inputs shifted meaning. Coverage pools them — the same assumption the
  surrogate itself already makes when it trains across the band, so this is inherited
  rather than introduced, and `tool_version_ids_at_fit` preserves traceability. Worth
  revisiting only if a customer hits it.
- IMSE / maximum-entropy augmentation as a sharper alternative to greedy max-variance
  (ADR-0020 defers; revisit on customer demand).
- Whether a user may opt into a GP pass at cohort preview by nominating a response
  early. Deferred — the model-free path answers the preview question, and the extra
  control is not obviously worth the cost until someone asks.
