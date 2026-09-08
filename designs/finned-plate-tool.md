# Finned Cold Plate — a purpose-built tool for the NAFEMS 2026 demo

Status: **BUILT, 2026-08-27.** Bundle at `matofx/example_tools/finned_plate/`; 23
physics tests plus an end-to-end staging test, all passing; ruff clean. Every decision
in §11 survived the build except the material pair, which the build disproved — see
§13 for what was found and what it means for the talk.

Companions:
- `NAFEMS_Talk_Plan.md` — the talk this exists to serve (needs revision to the one-tool arc)
- `decisions/0020-design-space-coverage.md` — the feature the talk turns on
- `matofx/example_tools/heat_sink/` — existing closed-form fin model; **source of the
  correlations reused here**
- `matofx/example_tools/thermal_soak/` — existing FE tool; **source of the bundle
  pattern, mesh-scaling trick and `.vtu` field plumbing**

---

## 1. Why a new tool

The revised talk is a single-tool narrative: ad-hoc runs → cohort → surrogate that
looks fine → prediction that is confidently wrong → coverage explains why → suggested
runs → refit → optimise. V&V throughout.

The dramatic centre of that arc is **a confidently wrong prediction**. Whether such a
point exists is a property of the *response surface*, not of MATOFX. A smooth,
near-linear response extrapolates happily into any gap and the beat never lands.

None of the three showcase tools was selected against that requirement. `thermal_soak`
in particular is benign in its main design variables, has no competing objective (no
mass output — every change that lowers temperature is free), exposes ten inputs where
the coverage plots need four, and is transient when the narrative wants steady state.

So the tool is specified here **from the narrative's requirements**, not adapted to
them.

## 2. Requirements this tool must satisfy

| # | Requirement | Why |
|---|---|---|
| R1 | Two competing scalars — one minimised, one constrained | Without a trade-off there is no optimisation beat |
| R2 | One 3-D nodal field output | Half the surrogate story; the best visual in the deck |
| R3 | Solve 10–60 s | Slow enough that "seconds → milliseconds" is honest; fast enough to generate ~400 runs |
| R4 | Four design factors, no more | Coverage scatter matrix must be readable from the back row |
| R5 | Genuine curvature and factor interaction | Makes clustered fits fail and spread fits succeed |
| R6 | A regime change inside the box | Makes a *hole* dangerous, not merely empty — this is the money shot |
| R7 | Physics statable in one sentence | Maintainer must be able to defend it live |
| R8 | Fixed mesh topology across all runs | Field surrogate needs node-for-node correspondence |
| R9 | One categorical factor | Exercises ADR-0020 categorical balance; ad-hoc archives are naturally unbalanced across materials |

## 3. The tool in one sentence

> A cold plate carries a heat-dissipating component on its underside and sheds that
> heat through a finned upper surface into a forced air stream; the tool solves the
> steady temperature field through the plate and reports how hot the component gets
> and what the plate weighs.

That is R7 satisfied. Every delegate in the room understands it, and there is no
composite failure criterion or transient integration to defend.

## 4. How R8 and the fin count are reconciled

**The problem.** Fin count is the factor carrying most of the interesting behaviour,
but varying it changes the mesh *topology* — and then fields from different runs have
no node correspondence, which breaks the field surrogate.

**The resolution.** The finite-element domain is **the baseplate only** — a 3-D solid
box of fixed plan dimensions. The fins are not meshed. They enter the solve as an
**effective convective boundary condition** on the top surface:

    -k dT/dn = h_eff * (T - T_air)     on the top face

where `h_eff` is computed closed-form from fin geometry, spacing and flow, reusing the
correlations already in `heat_sink.py` (channel Nusselt number, fin efficiency
`tanh(mL)/mL`, total wetted area).

This buys everything at once:

- **Topology is constant** — fin count only changes a scalar coefficient. R8 ✓
- **Base thickness scales the mesh in z without renumbering** — the same trick
  `bracket_mesh.py` already uses for laminate thickness. R8 ✓
- **All the nonlinearity survives**, because it lives in `h_eff`. R5, R6 ✓
- **The field is the one an engineer actually wants** — the temperature map on the
  plate, showing where the hot spot sits under the component. R2 ✓
- The solve is one linear steady conduction problem: no time stepping, no Newton
  iteration, nothing to explain. R7 ✓

**Honesty note for the talk.** This is a legitimate and conventional modelling
simplification (fins as a lumped surface conductance), but it *is* a simplification.
If asked in Q&A, say so plainly — it is the kind of owned limitation the plan's §9
already commits to.

## 5. Inputs (as built — ranges calibrated 2026-08-27)

Four varied factors plus a categorical, in two groups.

### Group: Fin geometry

| name | label | unit | default | min | max | notes |
|---|---|---|---|---|---|---|
| `fin_height` | Fin height | mm | 20 | 5 | 45 | Drives area **and** fin-efficiency roll-off |
| `fin_count` | Number of fins | – | 16 | 6 | 28 | Integer. Sets channel spacing on the fixed base width. Capped at 28 so the Reynolds crossover cuts across the box (§13.1) |
| `base_thickness` | Base thickness | mm | 6 | 2 | 15 | Spreading resistance vs. mass |

### Group: Operating conditions

| name | label | unit | default | min | max | notes |
|---|---|---|---|---|---|---|
| `fan_duty` | Fan speed | % of rated | 70 | 20 | 100 | **Not velocity** — see §5.1. Carries the regime change with `fin_count` |
| `material` | Plate material | – | `Aluminium` | — | — | **Categorical**: Aluminium (k 170, rho 2700) / Magnesium AZ31 (k 96, rho 1740). Not copper — see §13.2 |

### Fixed (not exposed)

Base plan dimensions 100 × 100 mm, fin thickness 1.0 mm, heat load 60 W over a
30 × 30 mm patch, air inlet 25 °C, one 80 mm axial fan. Fixing these is what keeps R4
at four continuous knobs; they can be promoted to inputs later without changing the
mesh.

**Design rationale.** Four continuous factors give a readable 4 × 4 scatter matrix,
with `material` rendered as **colour** rather than a fifth panel — which is exactly how
an unbalanced categorical should look when coverage flags it.

### 5.1 Why `fan_duty` and not `air_velocity` — the fan curve

**Decision: the tool models a real fan, not an imposed velocity.**

A fixed air velocity is a lie that flatters the design: it says you can pack in as many
fins as you like and the air will still come. Real fans do not work that way. Narrower
channels raise the pressure drop, the operating point slides down the fan curve, and
the flow collapses. **That collapse is what creates a genuine interior optimum** —
adding fins helps until the flow loss outweighs the added area — and it does so
physically, without needing an artificial cost constraint bolted on.

The model is textbook and defensible under questioning:

- **Fan curve** at full duty: `dp_fan(Q) = dp_max * (1 - (Q/Q_max)^2)`.
  80 mm axial fan — `dp_max` = 120 Pa, `Q_max` = 0.038 m³/s (**as built**; calibrated up
  from a 60 mm unit so the Reynolds crossover cuts diagonally across the box, §13.1).
- **Affinity laws** scale it with duty `d`: `Q_max ∝ d`, `dp_max ∝ d²`. Fan power is
  taken at the operating point as `dp * Q / eta_fan`, which scales as `d³` naturally.
- **System curve** `dp_sys(Q)` from channel friction, which depends on `fin_count`
  (spacing) and `fin_height` (channel depth).
- **Operating point** is the intersection, found by a 1-D root solve. Channel velocity,
  and hence `h`, follow from it.

**Why this keeps the demo intact.** Velocity is now *derived*, so it can no longer be a
design factor — but `fan_duty` replaces it one-for-one. The laminar/turbulent diagonal
of §7.3 therefore survives, simply expressed in the `fin_count` × `fan_duty` plane
instead. Four knobs preserved, choking physics gained, nothing lost.

**Bonus for the talk.** Derived velocity is a genuinely nice thing to show: *"you asked
for a fan, and the tool tells you what flow you actually get."* It also makes the
surrogate's job harder in an honest way, because the operating-point solve is itself a
nonlinearity the model has to learn.

## 6. Outputs

### Scalars

| name | label | unit | kpi | validity | role in the talk |
|---|---|---|---|---|---|
| `t_source` | Peak component temperature | degC | ✓ | 0–500 | **The constraint** (≤ 85 °C) |
| `mass` | Plate + fin mass | g | ✓ | 0–5000 | **The objective** (minimise) |
| `thermal_resistance` | Thermal resistance | K/W | ✓ | 0–20 | Secondary KPI; the number engineers actually quote |
| `air_velocity` | Channel air velocity | m/s | – | 0–50 | **Derived** from the fan operating point (§5.1). Shown, never set |
| `fan_power` | Fan power draw | W | – | 0–50 | **Reported, not constrained** — see §6.1 |

R1 satisfied: `mass` versus `t_source` is a genuine, physically inescapable trade-off.

### 6.1 Fan power is reported, never constrained — decision and reasoning

**Decision: `fan_power` is a displayed output. The optimisation has exactly one
constraint — peak temperature.**

Three reasons, in order of weight:

1. **The fan curve already did its job.** Under a fixed velocity, an air-side power
   constraint was the only thing preventing "just add more fins" from being a free
   win, so it was load-bearing. With a real fan, the choking is *physical*: flow
   collapses on its own. The constraint is no longer needed to make the optimum
   interesting, so it is now pure explanation cost.
2. **Speaking cost in a 15-minute talk.** Every constraint you show must be set up,
   justified and read back off the result. The plan's §10 cut list is already
   aggressive for exactly this reason. One objective and one constraint —
   *"lightest plate that keeps the chip under 85 °C"* — is a sentence. Two constraints
   is a paragraph, and the paragraph comes out of the coverage beat.
3. **Q&A exposure.** Fan static pressure, operating points and system curves are a
   specialist corner. Showing the number is safe; *optimising against it* invites
   questions on ground the maintainer has said is unfamiliar. Reporting it looks
   thorough and commits you to nothing.

Keeping it visible still pays: a delegate who cares can see the fan was genuinely
modelled rather than hand-waved, and it costs zero seconds of stage time.

**Held in reserve.** If range calibration (§8) shows the optimum is trivial — the
optimiser always slams to one corner — promoting `fan_power` to a second constraint is
the fallback that restores an interior answer. Decide that after seeing real runs, not
before.

### Field

| name | label | type | notes |
|---|---|---|---|
| `temperature_field` | Plate temperature field | `.vtu`, nodal, `temperature` | Steady state. One file per run — **no timestep series**. **Baseplate nodes only — never add fin geometry to this file** (§7a) |
| `design_schematic` | Design schematic | `image` (PNG) | Drawn from the inputs: plate, fins to scale, fan, component. The per-run picture of *what was run* (§7a) |

R2 satisfied, and the absence of a series is deliberate: it removes the
time-as-a-factor question from the talk entirely (see `NAFEMS_Talk_Plan.md` open
question 3).

## 7. Where the curvature comes from — R5 and R6

This section is the reason the tool exists; it should be verified empirically before
any presentation work.

**1. Fin efficiency (strong curvature + interaction).** Fin efficiency follows
`tanh(mL)/mL` with `m = sqrt(2h/(k*t))`. Tall fins on a lower-conductivity material
stop carrying heat to their tips, so `fin_height` and `material` **interact** — the
benefit of height depends on which metal you chose. An archive that only ever used
aluminium cannot learn what copper does at 50 mm. This is the categorical-imbalance
demo, made physical.

**On the two materials.** Aluminium (`k` ≈ 170 W/m·K, `rho` ≈ 2700 kg/m³) and copper
(`k` ≈ 400, `rho` ≈ 8960). This pair is chosen deliberately: **neither dominates.**
Copper conducts far better, so it tolerates taller fins and shorter conduction paths —
but it is 3.3× denser, and mass is the objective. So the categorical is a *real
decision* in the optimisation rather than a foregone conclusion, and the optimiser's
answer is genuinely not guessable from the podium. Both are also what heat sinks are
actually made from, which costs nothing in credibility. (A low-`k`, high-density metal
such as steel would show a bigger efficiency contrast but would simply lose at
everything, making the categorical decorative.)

For the coverage story: aluminium is the default choice an engineer reaches for, so a
realistic ad-hoc archive is heavily aluminium with a couple of stray copper runs —
precisely the imbalance ADR-0020's categorical balance check exists to flag.

**2. Fan-curve choking (interior structure).** More fins add area but narrow the
channels, raising pressure drop and pulling the operating point down the fan curve
until flow collapses (§5.1). Combined with efficiency roll-off, the lightest design
meeting a temperature limit is a genuinely non-obvious combination of few-tall versus
many-short fins, on one of two metals. This is now *physical* rather than imposed —
which is why `fan_power` need not be a constraint (§6.1).

**3. Laminar → turbulent transition (the regime change, R6).** Channel Reynolds number
`Re = rho*V*D_h/mu`, with `D_h ≈ 2s`, `s` set by `fin_count` on the fixed base, and `V`
now derived from the fan operating point. For air at ~40 °C across the proposed box:

- 40 fins at 20 % duty → low flow through narrow channels → firmly **laminar**
  (`Re` of order a few hundred)
- 6 fins at 100 % duty → high flow through wide channels → firmly **turbulent**
  (`Re` of order several thousand)

So the transition at `Re` ≈ 2,300 **cuts diagonally through the `fin_count` ×
`fan_duty` plane**, inside the design box. Heat transfer changes character across it. A
cohort clustered on one side has no information whatever about the other — and a
surrogate fitted to it will extrapolate across the transition with total confidence and
be badly wrong.

*(The exact `Re` values must be recomputed once the fan curve is calibrated, since
velocity is no longer a free input. The diagonal's existence is robust — it follows
from spacing and flow moving in opposite directions — but its position in the box is
not, and it needs to land where Phase A can plausibly sit entirely on one side.)*

**That diagonal is the demo.** It is invisible to Q², obvious in a coverage scatter,
and it gives the talk a physically real reason why the gap matters — not a contrived
one. It is also easy to draw on a slide.

## 7a. What the audience actually sees — decision on rendering

**Decision (maintainer, overriding an earlier draft): draw the fins. Caption what was
solved.**

The earlier draft argued for hiding the fins to avoid implying they were meshed. That
was wrong, and for a reason that matters more than the concern it was protecting
against: **`fin_height` and `fin_count` are two of the four design variables.** A
picture with no fins in it cannot show what the design *is*, cannot show what changed
between two runs, and cannot make "28 short fins in aluminium" mean anything to a room
seeing it for the first time. Hiding the fins does not avoid a question — it provokes a
worse one.

A caption handles the modelling point better than concealment ever could.

### The visual grammar

**Colour is the solve; grey is geometry.** The baseplate carries the temperature
contour. The fins are drawn to scale in neutral grey with no contour on them. This is
standard FE post-processing idiom and it reads correctly without being explained — the
colour boundary *is* the model boundary. The caption then says it out loud anyway.

**As built (2026-08-27).** The plate is painted with the **solved field** on the
mid-plane slice through the component (`plate_mesh.cross_section_grid`), not a single
flat colour keyed to the peak. That change is what makes the outlined footprint mean
something: there is now a visible hot spot directly above it. It also puts the
spreading resistance — the entire reason the finite-element solve exists — into the one
picture every run carries.

The scale is **fixed at 25-125 degC and never autoscaled per run**, so a shade means
the same temperature in every schematic and two designs can be compared by eye. A
colour bar states the range. The contrast this produces is itself a talk beat: a 12 mm
aluminium plate reads as almost uniform, while a 2 mm magnesium plate under the same
load shows a hard red spot.

Output is **2340 x 1356** (a 780 x 452 layout space at 3x, drawn on a 2x-supersampled
canvas), ~125 KB per run — about 50 MB across a 400-run campaign. The design is
anchored to a **fixed baseline** so the plate sits in the same place in every image and
only the geometry above it changes; sizing by a percentage of headroom instead let the
tallest designs overrun the headline.

### The caption

One line, on the slide and on the results view:

> **Solved: temperature through the baseplate. Fins drawn to scale, modelled as a
> surface conductance rather than meshed.**

That covers both halves of what a viewer needs — how it was solved, and what the
picture represents — in a single readable line, and it pre-empts the fin-tip question
rather than waiting for it.

### Where the fins actually earn their place

The strongest argument for drawing them is not the results view at all — it is the
**setup**. `heat_sink` already establishes the pattern: a `design_schematic` PNG output
(`file_type = "image"`), generated by the tool from its own inputs. Adopt it here. Then
every job carries a picture of the design it ran, the audience sees the object change
as the design changes, and it costs one matplotlib figure.

That is the direct answer to *"how do we visualise the inputs"*: a per-run schematic
drawn **from** the inputs, not a contour that happens to sit near them.

### Two implementation hazards this creates

**1. The solved field `.vtu` must stay pure.** The field surrogate ingests that file's
nodal array. Adding unsolved fin geometry into it would introduce nodes carrying no
temperature and corrupt the ingest — breaking the exact machinery the talk is built on.
Fin geometry therefore rides in a **separate display output**, never in the field file.
Preference order:

1. A separate geometry file alongside the field (display-only, not `file_type = "field"`)
2. The `design_schematic` PNG (guaranteed safe, static, already-proven pattern)

**2. Solved and predicted views must match.** The field appears twice in the talk —
solved, then predicted by the surrogate — and the *comparison* is the point. If the
solved view has fins and the exported surrogate's view does not, the comparison breaks
visually at the moment it matters most.

Fin geometry is a pure function of the inputs, so both the real tool and the exported
surrogate tool *can* in principle emit it. **Whether export-as-tool carries a non-field
display output through is unverified** — check it before relying on it (§12).

Safe fallback if it does not: use the finned view for *"here is the design"*, and
baseplate-only contours for the *predicted-vs-solved comparison*. That split is natural
anyway — one shot is an object, the other is a measurement — and it keeps the
comparison honest and matched.

## 8. Implementation shape

Bundle layout follows `thermal_soak/` exactly:

    example_tools/finned_plate/
      descriptor.toml      # ~230 lines, mostly shaped like thermal_soak's
      plate_mesh.py        # fixed-topology box mesh, z scaled by base_thickness
      fin_model.py         # h_eff, fin efficiency, mass, pressure drop
      fan_curve.py         # fan + system curve intersection -> flow, velocity, power
      finned_plate.py      # reads inputs/inputs.json, solves, writes .vtu + outputs.json
      finned_plate.png     # tool icon

Notes:

- `fin_model.py` is largely **lifted from `heat_sink.py`** — correlations already
  written and exercised.
- `fan_curve.py` is the one genuinely new piece: a 1-D root solve for the operating
  point (§5.1). Small, but **it must be robust** — it runs inside every job, and a
  failed or non-converged intersection produces a silently wrong `h_eff` rather than an
  error. Bracket the root explicitly, assert convergence, and fail the job loudly if it
  does not. A confidently wrong solver would poison the archive in exactly the way the
  talk warns about.
- `finned_plate.py` is *simpler* than `thermal_soak.py`: one assembly, one linear
  solve, no backward-Euler loop, one `.vtu` written.
- Solve time is set by mesh density. Target ~15–25 s (R3); tune the element count to
  land there deliberately rather than accepting whatever falls out.
- Install via the existing `demo_run/install_showcase_tools.py` pattern.
- `duration_class = "medium"`.
- Output validity bounds on every scalar and on the field (ADR-0017) so the
  invalid-run exclusion beat still works.

### Build checklist

- [ ] `descriptor.toml` with the inputs/outputs above
- [ ] `plate_mesh.py` — verify node count and ordering are invariant to every input
- [ ] `fin_model.py` — port from `heat_sink.py`, unit-test against its outputs
- [ ] `fan_curve.py` — operating-point solve, with convergence asserted, not assumed
- [ ] `finned_plate.py` — solve, write field + `outputs.json`
- [ ] Calibrate the fan curve so the operating point moves *materially* across the
      `fin_count` range — if flow barely changes, the interior optimum never appears
- [ ] Calibrate ranges so `t_source` spans roughly 40–120 °C across the box (the
      85 °C constraint must be genuinely binding, not trivially met or trivially missed)
- [ ] Confirm neither material dominates — both should win somewhere in the box (§7.1)
- [ ] Confirm the laminar/turbulent diagonal actually bends the response, and lands
      where Phase A can sit on one side of it — §7.3
- [ ] Component footprint outlined on the field output — §7a
- [ ] `design_schematic` PNG drawn from the inputs, fins to scale — port the pattern
      from `heat_sink.py` — §7a
- [ ] Fins rendered as grey geometry beside the coloured baseplate, **without** adding
      their nodes to `temperature_field.vtu` — §7a hazard 1
- [ ] Verify export-as-tool carries display outputs through, or adopt the fallback —
      §7a hazard 2
- [ ] Tests alongside the other example tools
- [ ] Help documentation — a tool page under `docs/user/` (CLAUDE.md requires this in
      the same change)
- [ ] Icon — schematic finned cold plate, per §7a

**Estimate: a few days of build, assuming the mesh behaves.** The surrounding
machinery — cohorts, coverage, surrogates, optimisation, V&V, lineage — is all shipped.

## 9. The run campaign — the real schedule risk

The tool is the small part. The archive is the deliverable, and it cannot start until
the tool is stable.

**Phase A — the ad-hoc history (~30–50 runs).** Built from a *plausible narrative*, not
adversarial selection: three design reviews, each asking one question, each answered by
a handful of single jobs. Aluminium only, or nearly so. Clustered at low velocity.
Record the narrative — you will be asked how these were chosen, and "an engineer
answered the question in front of them" is the true and disarming answer.

**Phase B — find the failing prediction.** Pick candidate points across the
laminar/turbulent diagonal, predict with the Phase-A surrogate, solve for real, and find
one where the error is dramatic but not cartoonish. **Budget several attempts.** This
is the single highest-risk item in the whole plan, because it is the one thing that
cannot be worked around by editing slides.

**Phase C — the coverage-suggested set (~100–200 runs).** Whatever ADR-0020 proposes,
run it honestly. Do not curate.

**Phase D — refit and verify.** Same field and scalar surrogates, same held-out point
from Phase B, now correct. Applicable range recorded from the coverage envelope.

**Phase E — the optimisation run.** Minimise `mass` subject to `t_source` ≤ 85 °C
(optionally `pumping_power` ≤ limit).

At ~20 s per run, Phases A + C + E are on the order of a few hours of wall clock — but
Phase B is exploratory and should be given real calendar time.

## 10. What this replaces, and what happens to the existing chain

The three-tool bracket chain stays built, tested and installed. It is **not deleted** —
it remains the workflow-wiring showcase (ADR-0015) and the multi-tool story for
customer demos. It simply stops being the NAFEMS vehicle.

Consequences for the talk documents:
- `NAFEMS_Talk_Plan.md` §5 beat table → the four-act structure
- `NAFEMS_Talk_Plan.md` §7 → coverage is shipped, not pending; the dependency note is stale
- `NAFEMS_Showcase_Tools_Guide.md` §7 → still needs the competitive framing removed
- Neither abstract needs changing — nothing in either promises more than one tool

## 11. Decisions — settled 2026-08-27

| # | Question | Decision | Where |
|---|---|---|---|
| 1 | How many materials? | **Two — aluminium and copper.** Chosen so neither dominates: copper conducts better, aluminium is far lighter, and mass is the objective | §7.1 |
| 2 | Fixed velocity or fan curve? | **Fan curve.** Velocity becomes a derived output; `fan_duty` replaces it as the fourth knob so the §7.3 diagonal survives | §5.1 |
| 3 | Is fan power constrained? | **Reported, not constrained.** The fan curve already creates the interior optimum, so the constraint is pure explanation cost — and it is unfamiliar Q&A ground. Held in reserve if calibration shows a trivial optimum | §6.1 |
| 4 | How recognisable must the plate be? | **Draw the fins, caption the solve.** Colour = solved baseplate, grey = fin geometry, plus a per-run `design_schematic` and a one-line caption. Fin geometry must stay out of the field `.vtu` | §7a |

Decision 3 was delegated to judgement. Decision 4 was too, and the judgement was
**wrong and overridden** — the draft proposed hiding the fins to protect a modelling
caveat, missing that fin height and fin count are themselves design variables and so
must be visible. Recorded because the corrected reasoning is the useful artefact: a
caption defuses a modelling caveat far better than concealment, and the thing that most
needs visualising is the *input*, not the result.

## 12. Still open

1. **Fan curve calibration.** `dp_max` and `Q_max` in §5.1 are illustrative numbers for
   a 60 mm axial fan. They set where the choking bites and where the laminar/turbulent
   diagonal falls, so both §7.2 and §7.3 depend on getting them right. This is the
   first thing to tune once the tool runs.
2. **Whether the interior optimum is real.** Asserted from the physics, not yet
   demonstrated. §6.1's reserve plan exists precisely because this might not hold.
3. **Where Phase A sits relative to the diagonal.** The ad-hoc archive must plausibly
   fall entirely on one side of the transition *without* being placed there
   artificially. If a realistic engineer's-choice cluster straddles it, the box ranges
   need adjusting rather than the cluster.
4. **Does export-as-tool carry non-field display outputs through?** The exported
   surrogate must be able to emit the same fin geometry (or schematic) as the real
   tool, or the solved-vs-predicted comparison in the talk will not match visually.
   Unverified — check against the shipped exporter, and fall back to §7a's split if
   not. **Cheap to check, expensive to discover late.**

---

## 13. What the build found — 2026-08-27

Five things the spec asserted turned out to be wrong or incomplete. All five were found
by *running the model*, not by review; none required a design change beyond the tool
itself.

### 13.1 The laminar Nusselt number made `fan_duty` inert

**Found:** with the fully-developed laminar correlation (a constant `Nu = 8.23`), `h`
depends only on hydraulic diameter — so across the whole laminar region, which is most
of the box, changing fan duty changed **nothing**. A quarter of the design space was a
dead variable, and the spec's §7.3 diagonal only clipped one edge of the box.

**Cause:** the channels are short. At these flows the thermal entry length is comparable
to the 100 mm channel, so the flow never thermally develops and the fully-developed
value is simply the wrong correlation for this geometry.

**Fixed:** a Hausen-type thermal-entry correlation in the Graetz number, blended with
Dittus-Boelter by taking the larger (which also removes a non-physical downward step at
the crossover — pinned by a monotonicity test). Duty now moves conductance by ~30 %
across its range, and both regimes are reachable inside the box. The fan was also
re-specified from a 60 mm to an 80 mm unit so the crossover cuts diagonally across
`fin_count` x `fan_duty` rather than clipping a corner.

**Worth noting for the talk:** the regime change is now real *and* the honest reason it
exists is a short-channel entry effect — a more interesting answer to "why does the
surrogate fail there?" than an arbitrary correlation switch.

### 13.2 The material categorical is one-sided — and that is better for the talk

**Found:** copper is strictly dominated (about +20 % conductance for about +230 % mass),
so it can never be chosen by a mass-minimising optimiser. Replacing it with magnesium
AZ31 flipped the direction but not the problem: magnesium wins at **every** base
thickness (brute force over 1,512 real solves).

**Cause, and why the obvious fix failed.** The relevant figure of merit is `sqrt(k)/rho`
— magnesium 5.6e-3, aluminium 4.8e-3, copper 2.2e-3. The spec expected spreading
resistance to claw this back for aluminium, since spreading scales as `1/k` and is
strongly material-dependent (measured: 28.6 K for aluminium vs 44.3 K for magnesium at a
2 mm base). It does not, and the reason is structural: **the constrained optimum
self-selects into the corner where the categorical cannot matter.** Minimum mass drives
base thickness and fin height to their lower bounds, where fin efficiency is ~0.99 and
conductivity is nearly irrelevant — so the optimiser never visits the tall-fin regime
where aluminium would win.

**Decision: keep it, and use it.** A two-sided categorical cannot be obtained here
without changing the problem (a second objective, or a fan-power constraint), and doing
that would cost the talk more than it buys. What matters is that the one-sidedness is
**an asset for this particular narrative**:

> The ad-hoc archive is aluminium-heavy, because aluminium is what an engineer reaches
> for. Coverage flags the categorical imbalance. The suggested runs fill in magnesium.
> The optimiser then finds a materially lighter answer **that the original archive could
> never have produced** — not because the surrogate was bad, but because the data had
> never been there.

That is a stronger and more concrete payoff than a balanced Pareto front would have
been, and it lands the coverage argument on a result rather than a diagnostic.

### 13.2a Correction, 2026-09-01 — the categorical IS two-sided, under two objectives

13.2's finding stands: for a **single** mass objective, magnesium dominates and the
material choice is decorative. Its stated implication — that a two-sided categorical
"cannot be obtained here without changing the problem" — was over-generalised, and the
maintainer was right to push back.

Measured on 360 real solves, the two-objective Pareto front (minimise mass, minimise
peak temperature) carries **both** materials: 21 magnesium points and 15 aluminium.
Magnesium owns the light end (40 g at 153 degC); aluminium owns the cool end (640 g at
37 degC), because once mass stops being penalised its better fin efficiency wins.

The sharpest number: **where the front crosses the 85 degC limit, the two materials are
within one gram of each other** — magnesium 85 g, aluminium 84 g. The material decision
is live precisely at the constraint, which is the most useful place for it to be and is
not guessable without running it.

**Consequence for the talk.** Use **two objectives** and draw the 85 degC constraint on
the front. One plot then carries the trade-off curve, the material crossover and a
concrete constrained answer. This costs no more stage time than the single-objective
version — "lighter or cooler, pick one; here is the exchange rate" is one sentence — and
it is a better visual.

*(Numbers from a coarse grid: 20x20x2 mesh, fan duty fixed at 0.7, coarse factor steps.
The structure is robust; the exact values will shift on the production mesh.)*

### 13.3 `fan_duty` will pin to its upper bound in the optimisation

**Found:** fan power is reported, not constrained (§6.1), so more airflow is free and
100 % duty is always optimal. As a *design variable* it is therefore degenerate.

**Recommended:** in the optimisation study, fix `fan_duty` at a nominal value (70 %) and
optimise only the three geometry variables plus material. This is also the more
realistic framing — the fan is a fixed part of the installation, not something the
designer tunes. `fan_duty` still varies across the **archive**, where it does real work
for coverage and the surrogate. Promoting `fan_power` to a constraint remains the
alternative, but it is no longer needed to keep the optimum interesting.

### 13.4 Solve time — measure in a fresh process

**Found:** three mesh densities timed back-to-back in one process gave 5.2 / 17.6 /
51.7 s; the same meshes in fresh processes gave 7.4 / 20.1 / 30.5 s. The sequential
figures were inflated enough to have picked the wrong production mesh.

**Settled:** **80 x 80 x 8, 59,049 nodes, ~21 s** per solve — inside the 10-60 s target
(R3). Peak temperature is converged to under 0.01 K on a mesh a quarter that size, so
the density buys **field resolution** (a 6,561-point top surface that renders as a
smooth contour) and a solve time that makes the surrogate comparison fair, not accuracy.
That reasoning is recorded in the source so nobody later "optimises" it away.

Run-campaign cost: ~21 s x ~400 runs is about 2.5 hours of wall clock.

### 13.5 A misaligned mesh silently moved the heat load

**Found:** the component footprint is selected by grid index, so a mesh whose element
count is not a multiple of 20 places the 30 mm patch at, say, 33 mm — the run completes,
reports plausible numbers, and is quietly wrong.

**Fixed:** rejected outright rather than rounded, with a message naming the fix. This is
the same class of defect the talk is about, found inside the tool built to demonstrate
it, which is worth a wry line if the rehearsal needs one.

---

## 14. Verified behaviour

Measured on the built tool, for the record and for the talk's "real numbers" rule (§3.5
of `NAFEMS_Talk_Plan.md`):

| Quantity | Range across the design box |
|---|---|
| Peak component temperature | ~30 - 135 degC (85 degC limit is genuinely binding) |
| Plate mass | 42 - 2,330 g |
| Channel air velocity (derived) | ~0.2 - 10 m/s |
| Channel Reynolds number | ~30 - 11,000 — crosses the transition inside the box |
| Fin efficiency | 0.57 - 1.00 |
| Spreading (peak minus coolest node) | 6.8 - 44.3 K, strongly material- and thickness-dependent |
| Solve time | ~21 s |

Lightest design meeting 85 degC, by brute force over 1,512 solves: **magnesium, 22 fins,
5 mm tall, 3 mm base — 71.3 g**. Aluminium's best is 81.0 g.

## 15. Still open

1. **Phase A placement.** The ad-hoc archive must plausibly sit on one side of the
   Reynolds crossover without being put there artificially. Now checkable against the
   real tool — this is the next thing to do.
2. **The failing prediction (Phase B).** Still the highest-risk item: it must be found
   empirically, and it cannot be worked around by editing slides.
3. **Does export-as-tool carry non-field display outputs through?** Unverified. If the
   exported surrogate cannot emit `design_schematic`, use §7a's fallback split. Cheap to
   check, expensive to discover late.
4. **Help documentation.** Deliberately none: no example tool bundle in the repo has a
   help chapter — the help docs cover platform features, not bundled content. Flagged
   here rather than skipped silently, per CLAUDE.md.
