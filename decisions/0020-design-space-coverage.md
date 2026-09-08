# 0020. Design-space coverage — is this archive fit to learn from?

Date: 2026-08-24
Status: Accepted (design discussion with the maintainer, 2026-08-24; **revised twice the same day after two design-review passes** — first adding decisions 1, 5 and 6; then 4 and 11, with 6 and 9 sharpened. The reviews found that the core argument held while the *interface with existing machinery* repeatedly did not: four surrogate kinds, three distributions, a reserved `time` factor, per-mode field fits and optional training-data retention were each assumed away by the first draft. Full finding-by-finding record in `designs/design-space-coverage.md` §15). **ALL 7 SLICES SHIPPED 2026-08-24.** Every decision below survived the build; none was reversed. What the build *did* find was **41 further findings** (21–61 in `designs/design-space-coverage.md` §15), against 23 from the three pre-build reviews — in two distinct groups. *Interface* mismatches, which tests against the real types catch: `TrainingData` carried neither categorical columns nor a filtered-exclusion count, so decision 3's categorical balance was unmeasurable exactly as specified. And **numerical** defects — confidently wrong numbers with correct types, passing every fixture test and three design reviews — every one of which was found only by running against a real 250-run archive. **An adversarial stress pass on 2026-08-25 found sixteen more** (62–77, §15.1), seven of them in production paths no test exercised — the predict-time guard rejecting 138 of 143 real training runs because containment was implemented twice and the copies drifted, and every exported surrogate tool with a categorical input reporting `outside` on every prediction because the synthesised form could not supply the axes its own guard checks. Both are *cross-surface disagreements*, invisible to any test that exercises one surface at a time. A final **readiness** pass then found the two that were never wrong answers at all: coverage **blocked the event loop** (1.85 s per panel load on a real 708-run archive, with every other request stalled), and the headline radius is a low-biased sampled estimate whose “rough estimate” caveat fired three dimensions later than the error appears. All sixteen fixed and pinned by regression tests; no decision below was reversed. **The verdict itself never moved** — 0 flips across 40 designs × 8 probe seeds, and stable under a 32× denser probe on all five real archives. **Revised again 2026-08-26/27 after the feature was switched on against the live demo archive for the first time** — see *Revision, 2026-08-27* below, adding decisions 12-18. Nothing was reversed; most exist because an earlier decision was right in principle and answered the wrong question in practice. Three headline findings, each measured: the coverage *score* was an **efficiency** measure, so adding runs could make the verdict worse (demonstrated on nested point sets); a one-factor-at-a-time archive — 531 runs, fourteen of fifteen input pairs failing — read `good`; and the suggested runs ignored the tool's own input rules entirely, so on a sum-checked tool **0 of 8** proposed runs were submittable. Session record: `SESSION_HANDOVER_ADR0020.md`. Build-level spec: `designs/design-space-coverage.md`. Supersedes nothing; extends ADR-0008 (surrogate family), ADR-0009 (optimisation framework), ADR-0011 (V&V tracking), ADR-0017 (output validity) and the Job Cohorts rework.

## Context

MATOFX can build a surrogate from any Job Cohort. Cohorts assembled from a
**parametric study** are space-filling by construction — the generator
(`studies/generate.py`) placed the points. Cohorts assembled from **ad-hoc single
jobs** are not: engineers run what the design review asked for, so the points cluster
in the regions that mattered at the time and leave the rest of the space empty.

Every quality signal we have is **post-fit**: `SurrogateQuality.q2` and `rmse_cv`
(`studies/surrogate/__init__.py`), plus the per-mode / field variants in
`surrogate/field.py`. Nothing inspects the geometry of the training set before the fit
is committed.

That is not merely a missing convenience — **Q² actively conceals the problem.** It is
computed by leave-out cross-validation *on the points that exist*. A tightly clustered
cohort will report an excellent Q² precisely because every held-out point has a near
neighbour, while the model is unreliable a short distance outside the cluster. High
Q², no coverage, false confidence. A user is then invited to publish that model,
export it as a tool, and optimise against it.

Three distinct diseases hide in an ad-hoc cohort, and they need different cures:

1. **Sparsity / clustering** — holes in the space.
2. **Confounding** — engineers rarely vary one factor at a time (thickness *and* load
   move together), so the fitted model cannot separate their effects. No amount of
   *coverage* fixes collinearity; it needs points that break the correlation. Q² can
   look excellent while the model is unidentifiable off the correlation ridge.
3. **Categorical imbalance** — 40 CFRP runs and 2 GFRP runs looks perfectly covered on
   every continuous axis, and the surrogate will then confidently predict a material
   it has effectively never seen. This failure is **silent**.

A related gap sits downstream. Even a well-covered model is asked, in normal use, for
predictions at points outside its training region. Nothing currently says so. The V&V
record already carries `applicable_range_json` (ADR-0011) but it is documentary —
authored by hand, unconnected to the model's actual support.

## Decision

Introduce **design-space coverage as a first-class concept spanning the whole chain** —
cohort → surrogate fit → prediction → V&V evidence — rather than a diagnostic panel
bolted onto cohort preview.

1. **The point set is the training set, and the factors are inferred.** Coverage
   consumes the `TrainingData` produced by `studies/surrogate/ingest.py`, never raw
   cohort membership: ingestion already drops ADR-0017-invalid runs (*"never train on
   a run flagged invalid"*) and `FactorFilter` exclusions, so a cohort of 100 may train
   on 62. Measuring the 100 would overstate coverage by exactly the runs the platform
   already judged unfit to learn from — the failure this ADR exists to prevent. The
   exclusion counts are surfaced, not swallowed.

   Factors are likewise **inferred, not declared**: an ad-hoc cohort has no
   `FactorSpec`, and the codebase already resolves this via `varied_columns` /
   `_constant_inputs`. An input constant across the training set is not a factor but is
   carried as a **pinned** value and enforced by the guard (decision 8) — predicting at
   a different value of a never-varied input is pure extrapolation, and silence there
   would be the dangerous answer.

2. **Analyse over the full factor model — continuous, categorical, and integer as a
   third treatment.** `studies/factors.py` carries six vary kinds and its space-filling
   generator already handles *"numeric (range/distribution) + categorical by binning"*.
   An analyser understanding only continuous factors would model the design space
   **less richly than the code that creates designs**.

   The `column` vary kind is a **pure marker with no declared domain** and is permitted
   on `numeric`, `select`, `bool` and `file` factors alike, so it resolves **by the
   factor's underlying type**: categorical levels for `select`/`bool`/`file`,
   continuous for `numeric`, with the domain recovered from observed values.

   **Integer factors are ordered discrete**, aligned with
   `designs/integer-parameter-handling.md` (shipped 2026-06-15). **Ordered**, because
   distance is real — ply count 3 is near 4 and far from 12 — and discarding that
   ordering would throw away information the GP's ARD length scales genuinely use. But
   their domain is a **finite lattice**, so where that lattice is small, coverage is
   **occupancy** rather than distance: an envelope of `[1,5]` with all five values run
   is **completely covered**, whereas a continuous reading would report holes at 1.5 /
   2.5 and propose ply count 2.7 — not a valid input at all. Suggested runs inherit the
   existing non-negotiable — *any automated sampler emits only integer values for an
   integer parameter* — via `_intsnap.snap_int`, exclude already-run lattice points,
   and shrink the batch with a warning when the lattice is exhausted.

3. **Two criteria for two diseases, reported separately.**
   - *Sparsity* → a space-filling criterion.
   - *Confounding* → a factor correlation matrix / condition number.

   Reporting one verdict for both would let a cohort that is beautifully space-filling
   along a diagonal ridge pass as healthy. **Categorical balance is a third, equally
   first-class input** to the verdict, never a footnote — it is the silent failure.

4. **Not every axis is a design variable, and not every axis is linear.** Two kinds of
   factor break the naive treatment:

   - **Derived axes.** `ingest.py` reserves `time` as the factor a time-resolved field
     surrogate fits over. It is a real *predictor* — a field predicted past the last
     trained snapshot is genuine extrapolation — but **not** something anyone can
     commission. (The column holds a **0-based snapshot index, not seconds**; an
     earlier draft of this paragraph illustrated it with "5000 s from 600 s runs",
     a sentence the platform cannot produce — see finding 25.)
     Derived axes are therefore **measured and guarded but never suggested on**, and a
     thin one is reported with the remedy that actually applies (a longer run or finer
     snapshot retention), not a proposed design point. Without this the platform would
     confidently advise "run a job at snapshot 347".
   - **Log-scaled axes.** `DistributionVary` supports `loguniform`. Normalised
     linearly, a factor over `[1, 10000]` compresses the whole 1–1000 decade into the
     first 10 % of the axis: it reads as clustered where it is even, suggestions pile
     into the top decade, and the verdict is confidently — and silently — wrong.
     Log-sampled factors are measured in log space, inferred where a study records the
     distribution and user-overridable where an ad-hoc archive does not.

5. **Coverage is response-dependent, so the analysis splits in two.**
   `SurrogateModelVersion.responses` is a *list* and `fit_surrogate` fits **one**
   response at a time, so a version carries N GPs with N sets of ARD length scales.
   Mass may depend almost entirely on thickness while peak temperature depends on
   everything: over an **identical point set** those GPs give **different verdicts**.
   There is no single "GP coverage" for a design.

   There is also a sequencing problem — cohort preview asks *"is this fit to learn
   from?"* before any response is chosen or any GP exists, so a GP-only design would
   require fitting a model to decide whether to fit a model.

   Therefore: a **response-agnostic design pass** (model-free, geometric) answers the
   preview question and every cold-start case; a **per-response pass** runs at fit
   time, one verdict per response, with the **worst** as the headline. A model is only
   as trustworthy as its least-covered response.

   A "response" is not always one GP. A **field response** is fitted **per POD mode**,
   so its σ is the **energy-weighted aggregate** over the mode surrogates — a mode
   carrying 1 % of the field's energy must not drive the verdict as hard as the
   dominant one. Field responses are in scope from the start: the demo chain's
   temperature and failure-index surrogates are precisely this shape, and excluding
   them would exclude the demo.

6. **GP posterior variance is the per-response metric, with a model-free fallback —
   and the fallback is not only about data volume.** `SurrogateKind` is
   `gp | gp_matern | poly | gbm`, and **only the two GP kinds carry a posterior
   variance** (which is why the exporter's ± gate is already GP-only). A `poly` or
   `gbm` response — a user-selectable option, and one MOP's auto-best-metamodel may
   pick on Q² alone without regard to uncertainty — has no variance to interrogate, so
   it uses the model-free criterion and its guard reports `inside`/`outside` only. It
   loses ARD weighting and the `edge` state; it does not lose the feature.
   For fixed hyperparameters the GP posterior variance
   `σ²(x) = k(x,x) − k(x,X)K⁻¹k(X,x)` **contains no `y` term** — it depends only on
   *where* the design was sampled. GP variance therefore *is* a coverage measure:
   distance-to-data warped by the kernel. Its advantage over a plain geometric
   criterion is **ARD length scales** — it has learned which factors the response
   actually responds to, so it spends an expensive simulation budget on the axes that
   matter.

   The caveat is real: hyperparameters *are* estimated from `y`, so with a small or
   badly clustered cohort the length scales are unreliable and the analysis is steering
   by a compass it calibrated with the compass. Below a threshold of points per active
   dimension the analyser **falls back to model-free maximin distance / centered L2
   discrepancy on normalised ranges**, and states which criterion produced the verdict.

   This fallback is not an edge case. The customer with forty scattered ad-hoc jobs
   wondering whether they have something trainable **is the modal case** — the feature
   must work exactly when the data is worst, or it answers a question nobody asked.

7. **The region of interest is declared, never inferred from the bounding box.**
   Ad-hoc jobs do not fill a hyperrectangle. Hunting for holes inside the cohort's
   bounding box would confidently flag the corner where thickness = 8 mm meets heat
   load = 12 W — a region empty because nobody would ever build it. The analysis
   **asks the user for the design envelope**. One form, honest, and it doubles as the
   model's declared applicable range (decision 9).

   Because the envelope is user-editable, **narrowing it is the expected action** ("we
   ran 1–8 mm but only trust 2–6"). Training points then sit outside it: they **still
   train the model** — real data improves behaviour at the boundary — but are
   **excluded from coverage measurement**, counted, and never clamped onto the
   boundary, which would manufacture false duplicates.

8. **Suggested runs are generated by greedy sequential selection, and land as a
   governed study.** Because GP variance is `y`-free, a whole batch of *N* points can
   be planned without running any of them: pick the max-variance point, condition on it
   as though it had returned a value, recompute, pick again. Naive top-*N* selection
   would return *N* adjacent points in the same hole. The output creates a **parametric
   study** (ADR-0001) — an auditable batch inside the platform, not a CSV emailed
   around — and the UI names which response the suggestion is improving.

9. **A predict-time extrapolation guard, and the V&V envelope becomes enforceable.**
   The same `y`-free quantity evaluated at a single point tells an engineer whether the
   number they just asked for sits inside or outside the region the model was trained
   on. This is the highest-value half of the feature: a build-time check fires once, a
   prediction guard fires every time someone leans on the model.

   An exported surrogate-tool emits a **numeric `support_sigma`** plus an
   informational `support_status` string. The numeric is deliberate: ADR-0017 output
   validity operates on numeric scalars and field arrays, **not status strings**, so
   only a numeric σ can carry a declared maximum and flag runaway extrapolation
   automatically in a workflow nobody is watching.

   The `edge` threshold is **computed once at fit and stored**, never recomputed per
   prediction: that would cost a training-set evaluation on every call, and
   `keep_training_data` is optional — a version that discarded its training data could
   not recompute it at all.

   The declared envelope is written to the model version and offered as the V&V
   record's `applicable_range_json` (ADR-0011), converting a documentary field into an
   enforced one.

10. **Coverage is persisted and versioned, not a transient panel.** The verdict, the
   criterion, the declared envelope, per-factor and per-response breakdowns, and the
   diagnostics are stored on the surrogate model version so they reach the model
   record, the per-job report and the V&V evidence trail. A flat scalar could not carry
   integer occupancy or per-response results, so the stored report is structured.
   Cohorts support `auto_grow`; coverage re-evaluates when membership or envelope
   changes — **cached against that key, not recomputed per read**, since an auto-grow
   cohort re-resolves on every read and a GP fit per page load would violate
   `feedback_performance_and_security_equal`.

11. **The verdict is framed by its source, and acting on it is a separate right.** An
    optimisation study concentrates runs near the optimum — the algorithm working, not
    a design flaw — so a cohort drawn from one reads `poor` every time, and a light
    that is always red is one users learn to ignore. `source_kind` is already on the
    version, so the report states the reason beside the verdict and points at the real
    remedy. Separately, "create a study from these suggestions" spawns real solver jobs
    and spends real compute, so it is **gated on the existing study-create
    permission** — the read-only analysis stays open to anyone who can see the cohort.

12. **"We cannot tell" is a distinct verdict.** Degenerate point sets — too few runs,
    no varied factors, fewer than two continuous factors for a correlation — return
    `insufficient`, never `poor`. Conflating "we can tell, and it is bad" with "we
    cannot tell" would be its own species of false confidence.

**Explicitly deferred:** D-optimal augmentation. It is a *linear-model* criterion —
defined against an assumed regression form — so applying it to a GP surrogate means
optimising new runs against a model we are not fitting. The GP-native equivalents are
**IMSE-optimal** (minimise integrated posterior variance) and **maximum-entropy**
design, of which greedy max-variance is a cheap and well-understood approximation.
Deferred also because space-filling augmentation over a declared envelope breaks
correlation ridges as a side effect — filling the empty off-diagonal region *is*
decorrelation — so the confounding *diagnosis* delivers most of the value while the
sharper *cure* waits for a customer who asks.

## Consequences

- **The dangerous silent failure is closed.** A clustered cohort can no longer present
  a flattering Q² with nothing to contradict it.
- **No new dependency.** Greedy max-variance is a loop over the existing GP; maximin
  and discrepancy are a few lines of numpy. `scikit-learn` (GP) and `numpy` are already
  in the stack.
- **The "how much data do I need?" question becomes answerable.** Today it is a guess;
  it becomes a measurement with a proposed remedy attached.
- **ADR-0017 gets stronger.** Invalid runs were already excluded from fits; their
  absence is now *visible* in the coverage verdict, so a user who wonders why their
  100-run cohort trained on 62 points is told.
- **A new form to fill.** Declaring the envelope is friction, deliberately accepted
  over an inferred region that would produce confidently wrong suggestions. Per
  `feedback_users_are_not_modellers` the wording must be plain-English — "over what
  range should this model be trusted?" — pre-populated from the training set's observed
  spread as a starting point the user adjusts.
- **Coverage verdicts will sometimes be discouraging**, and should be. A cohort that
  cannot support a surrogate must be allowed to say so rather than yielding a model
  with a caveat nobody reads. The fit is not blocked — the verdict is recorded and
  travels with the model.
- **Exported tools change shape on re-export.** The two new outputs mean an existing
  exported surrogate-tool gains the guard only when re-exported, through the normal
  `test → released` gate. Existing exports keep working untouched.
- **Generalises the flywheel beyond optimisation studies.** "The model asks for the
  runs it needs" currently requires an optimisation study in `explore` acquisition
  mode; this makes the same capability available against any archive.
- **Talk dependency.** `NAFEMS_Talk_Plan.md` §7 places this feature ahead of all
  presentation work — it answers the sharpest question the abstract invites.

---

## Revision, 2026-08-27 — decisions 12–18

The feature was switched on against the live demo archive for the first time on
2026-08-26 and read wrong within minutes of somebody looking at it. Nothing below
reverses a decision above. Most exist because a decision above was *right in
principle and answered the wrong question in practice*; one is the test layer that
would have caught them, and the last pair came from switching the feature on and
pressing its own button.

Two properties of this feature made it worth chasing rather than patching, and both
are the reason it is worth talking about publicly:

- **the output is unfalsifiable downstream.** If a tool predicts 240 °C and the truth
  is 400 °C, someone notices. If coverage says *"well covered"* about a tight cluster,
  the surrogate fits, Q² comes back excellent — leave-one-out CV flatters a cluster by
  construction — the predictions look plausible, and the error surfaces months later as
  a bad engineering decision. Nothing contradicts a wrong verdict;
- **every defect found in this pass was a sentence or a number, not an exception.** No
  stack trace, no failing assertion, 600+ green tests throughout. The cheapest test that
  would have caught any of them is switching the feature on against real data and
  reading the screen — which is also the last test anybody runs.

### 12. The verdict is measured against a fixed standard, not against the archive's own size

Decision 2's yardstick (`reference_radius` at the archive's own point count, `designs`
§4.2) removed an always-red light and introduced a subtler fault: a ratio against a
yardstick that moves with `n` is an **efficiency** — *"are these runs arranged well for
how many there are?"* — and an efficiency legitimately falls when runs are added. The
panel could therefore answer *"is this fit to learn from?"* **worse** after more data
arrived, which it did: a suggestion plan reading `good` at 60 proposed runs and `poor`
at 120, while the gap being measured had genuinely shrunk.

The bar is now a **stated** size — `standard_runs_per_input × d` — so only the radius
moves, and a covering radius is non-increasing as points are added. Monotonicity becomes
a property of the arithmetic rather than something to hope for. The old number survives
as `efficiency_score`: a real question, separately labelled, that does not set the light.

**Calibrated, not chosen** (`checks/coverage/calibrate_standard.py`; §4.4.1). The sweep
became answerable only after establishing what a standard *cannot* do — since
`score = bar / radius` with the bar identical across designs, changing it rescales every
score by one factor and cannot improve separation at all. It decides **where the floors
fall**. Restated as placement, seven design families at four sizes with eight replicates
put the answer at 30 runs per input: worst good 0.529, best bad 0.368, the existing 0.45
floor between them with a balanced ±0.08 margin and no threshold moved to fit.

The calibration also found the yardstick itself was a **single random draw** — one Latin
hypercube, whose own covering radius is a random variable. The bar read 0.3283 at ten
runs per input and 0.2443 at twelve: a larger ideal design giving a 26% smaller bar,
impossible in expectation and landing directly on every verdict. Now the median of
several layouts, memoised on the envelope's shape.

### 13. Above six varying inputs the joint reading does not set the verdict

Not a sampling caveat (decision 11 covers that). With enough inputs, no realistic number
of runs can fill the space, so *every* design leaves an enormous hole and the readings
converge. Measured margin between a Latin hypercube and the worse of
one-factor-at-a-time / clustered, at 20 runs per input:

```
d=2: 0.44   d=3: 0.34   d=4: 0.33   d=5: 0.34   d=6: 0.32
d=7: 0.20   d=8: 0.20   d=9: 0.19
```

It holds to six and collapses at seven, where a clustered design scores 0.79 —
comfortably `good`. A number that cannot tell a cluster from an even spread must not
drive a traffic light, and the direction of its error is **flattering**, which is the one
direction this ADR exists to prevent. Above the boundary it is reported with its caveat
and the per-input and per-pair readings carry the verdict.

### 14. Coverage is measured at three scales, because a real archive failed between two of them

The design measured each input's own range and the whole joint space. A one-factor-at-a-
time archive passes **both** and is useless: 531 runs, six inputs, every per-factor row
green, verdict `good` — and *Base width* against *Material conductivity* a cross, one
horizontal line of runs and one vertical, the rest of the square empty. Fourteen of
fifteen pairs `thin` or `poor`.

Each neighbour is blind for its own reason. A marginal cannot see structure by
construction — in a cross both inputs *are* varied end to end, so both rows read ~98% and
both are right. The joint score is uninformative at that width (decision 13). In **two**
dimensions a few hundred runs is plentiful and the cross has nowhere to hide.

`analyse_pairs` therefore scores every pair of movable numeric axes on its own 2-D
projection. **Only a `poor` pair enters the verdict** — a genuine hypercube produces
`thin` pairs once there are enough factors, and demoting an archive for being *wide*
would be decision 7.1's always-red light in a new hat. "Varied one at a time" is a
**separate diagnosis** from "one bad pair", because the remedies differ: a handful of
gap-filling runs fixes an awkward corner, where a cross needs a designed set that moves
several inputs together.

### 15. Both ranges are reported: what was simulated, and what the tool allows

The envelope defaults to the observed spread (decision 4), which makes every figure
derived from it self-referential — *"98% covered"* has always meant *98% of the range you
chose to run*. No reading in the report could say that a tenth of the tool's own range had
ever been tried, and on the demo archive `base_temperature_rise` sat at a single value out
of a declared 10–120 with every reading calling it fine.

Each input now carries a second coverage reading against the **descriptor's** bounds, and
the panel shows both. Two constraints follow from honesty rather than convenience: only a
*literal* bound is taken (a formula bound is a different interval per row, so there is no
denominator to quote, and such an input reads *no stated range* — distinct from *poorly
covered*); and values outside the declared bounds are clamped rather than dropped, since
a run at 406 against a declared 400 proves *more* coverage, not less.

The same decision makes a held-constant input answerable at all (§4.7): *"always 60"* is a
fact about what has been run, not a rule, so giving such an axis a range **promotes** it
to a varying one and it joins the design space properly rather than being a special case
every measurement carries.

### 16. Render paths are tested, because none of these defects were exceptions

Two render-time defects shipped past a green suite in consecutive rounds: a `string`
output branch no page could render, and `cell.style("cursor: pointer;", add=True)` —
whose first positional parameter is *itself* named `add` — which drew the first input row
and then stopped, with the rest of the table simply **absent** rather than erroring
visibly.

The wording helpers were tested because they are pure; the render functions consuming them
were tested by nothing, on the stated grounds that "the suite cannot drive NiceGUI". It
can: elements need a **slot**, not a browser, a server or a socket, and a bare `Client`
supplies one. The regression suite was verified by reintroducing the defect — four of its
tests fail on it — and then restoring the fix, because a regression test that does not
fail on the bug is decoration.

### 17. A suggested run is one the tool would accept, or it is not suggested

Decision 5 gave suggestions the *integer* invariant. That turned out to be one rule from
a family, and the rest were absent: the path sampled each varying axis inside its own
box, independently, and proposed the result. A descriptor says considerably more than
"each input lies in this box" — `set_value` forces, formula-driven bounds over sibling
inputs, `require` validity rules, §F30 cross-field constraints, and **sum-checks**.

Measured on `material_properties`, whose three percentages must total 100: **0 of 8**
proposed runs were submittable. Not a tail case — every single one.

The defect had been invisible because the commissioned study was left as a draft. The
batch was priced, listed run by run and committed, and its points died later inside a
generator nobody was watching. Making the study *run* (decision 18) converts that into
"I asked for eight runs and got none", which is how it surfaced.

Two passes, in order, because the rules are not all the same kind of thing.

**A sum-check is repaired, not rejected.** It is an equality, and filtering independently
sampled points against an equality rejects essentially all of them — turning a broken
feature into an empty one. The group's true degrees of freedom are searched instead, one
member derived as `value - sum(others)`. That is the fix ADR-0009 §3.6 already made for
the optimiser, and coverage **reuses that module** rather than restating it. Repair has a
limit — a residual the balance cannot absorb without leaving its own range is a rejection,
not a silent clamp, because clamping would emit a run breaking the rule the derivation
exists to keep.

**Everything else is rejected by the same call the study generator makes.** Not an
equivalent check — `forms.resolve_for_generation`, the identical function on the identical
point. A batch that passes here cannot be dropped there, which is the whole failure being
closed. Survivors return *resolved*, forces applied, because that is what would run.

**Verified with its control.** A tool carrying four rules of different shapes gives 8 of
8 conforming; withholding the descriptor on the same seed gives 6 of 8 violations. The
first version of that fixture clustered its runs where every rule happened to hold and
its control read 0 — a test that could not have failed, and would have "proved" the
feature worked whether or not it did. The control is committed alongside the assertion
for that reason.

### 18. The commissioned study is run, not parked

Decision 11 landed the batch as a **draft**, on the reasoning that solver time should be
reviewed before it is spent. The review had already happened: the user priced the batch,
saw every proposed run with its input values, and pressed a button naming the cohort it
feeds. What the draft added was not a decision but a second manual step — and a page
reading *"Progress: Not submitted yet"* to somebody who had just been sent there to watch
it run.

`create_study_from_coverage` now calls `StudyService.submit`, the same call the studies
page makes, so **the governance path is unchanged**: the pinned tool's `approvals_policy`
still decides. `none` / `post` fan the points into child jobs and queue them; `pre` /
`both` raise the study-level gate and it waits at `pending_approval`. This removes the
*user* from in front of the gate, not the gate.

The run cap is raised to cover the batch. The cap exists to stop a **generator** running
away; here the point count is exact, already priced, and was shown run by run — so it
must not be the thing that rejects a batch the user just approved.

**What this exposed in the tests.** Three fixtures had been getting away with stubs
because nothing in them ever generated or queued: a descriptor of `{"alias": "demo"}`, a
bundle path that did not exist on disk, and no storage location at all. Each is now real.
A fixture that cannot be parsed cannot exercise the step under test, and had quietly
guaranteed that decision 17's defect stayed hidden.

### What this session changed, at a glance

| | Before | After |
|---|---|---|
| Verdict yardstick | archive's own size (an efficiency) | fixed standard, 30 runs/input |
| Adding runs | could make the verdict worse | never can, by arithmetic |
| Scales measured | 1-D marginals, joint | 1-D marginals, **pairwise**, joint |
| One-factor-at-a-time archive | reads `good` | reads `poor`, remedy named |
| Joint reading above 6 inputs | set the verdict, flatteringly | reported with caveat, does not |
| Range measured against | what you happened to run | that **and** what the tool allows |
| Held-constant inputs | never suggested | can be opened up, everything follows |
| Suggestion sizing | "how many runs do you want?" | priced ladder scaled to the tool |
| Commissioned runs | might never rejoin the cohort | linked and adopted (migration 0096) |
| Suggested runs | ignored the tool's own input rules | sum-checks repaired, every other rule enforced |
| Commissioned study | parked as a draft | submitted; approvals policy unchanged |
| Study target | typed project id | picker over the caller's projects |
| Study name | `Fill coverage gaps` | `<tool> - Fill coverage gaps`, clash flagged before the click |
| Render paths | untested | drawn and asserted |

### Talk-ready framing (NAFEMS)

Three points this work supports directly, each with a measured number behind it:

1. **The dangerous failures in engineering ML are the silent ones.** Coverage's own
   verdict is unfalsifiable downstream — a wrong "well covered" is never contradicted by
   anything, because a clustered archive produces an *excellent* leave-one-out score. The
   governance question is not "is the model accurate?" but "is anyone able to notice when
   it isn't?"
2. **Metrics answer the question they were built for, not the one on the label.** A
   coverage score referenced against the archive's own size is a perfectly good
   *efficiency* measure and a bad *coverage* measure — and the tell was a user asking why
   more runs made the verdict worse. Demonstrable in one table (decision 12).
3. **One-factor-at-a-time archives look healthy on every conventional check.** Full
   marginal spread, high Q², green per-input rows — and no ability to learn a single
   interaction. Fourteen of fifteen pairs failing under a verdict of `good` is a slide on
   its own (decision 14).

4. **A recommendation nobody can act on is worse than none.** The panel proposed eight
   runs, listed them with their input values, priced the coverage they would buy — and on
   a tool whose three percentages must total 100, **not one of them could be submitted**.
   The batch was confident, specific, and unusable, and nothing on the way through said
   so. Governed AI is not only "is the model right?" but "can the thing it recommends
   actually be done, under the rules this tool already declares?" (decision 17).

Supporting material: `ARCHITECTURE.md` §16.83–16.86 (narrative, session by session),
`designs/design-space-coverage.md` §4.4–4.8, §5.2 and §11.5 (the rules, their
calibration, and the surfaces),
`checks/coverage/calibrate_standard.py` (reproduces the threshold evidence).
