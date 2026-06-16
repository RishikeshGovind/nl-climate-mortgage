# CLIMADAPT-NL — ABM (working implementation, Phases 1–4)

A runnable agent-based model of Dutch mortgage-portfolio resilience to physical
climate risk, implementing **all four phases** of the design in
[`../ABM_DESIGN.md`](../ABM_DESIGN.md). It makes the portfolio's climate risk an
**emergent** property of heterogeneous agent decisions over time, rather than a
closed-form discount read off a normal CDF (which is what the front-end in
`../js/app.js` does).

- **Phase 1** (hard ABM gate): heterogeneous households + stochastic climate +
  shock-driven default rule with a **default→price→default cascade**.
- **Phase 2**: **adaptation + social learning** (uptake is S-shaped, §1 pattern 1)
  and **endogenous lender risk-repricing**.
- **Phase 3**: **building-level spatial cascades** for the five cities with
  downloaded footprints (per-building agents, ~250 m spatial price contagion,
  persistent flood-prone districts) + an **animated map** (`../abm_dashboard.html`).
- **Phase 4**: **intervention experiments** across the five §7 resilience levers
  + a **Sobol global sensitivity analysis**.

It is deliberately **framework-free (pure Python stdlib)** but written in the
canonical ABM idiom — explicit `Agent` classes with their own `step()`, a
scheduler (`RandomScheduler` ≈ Mesa's `RandomActivation`), a `DataCollector`, and
a Monte-Carlo ensemble runner — so it ports to [Mesa](https://github.com/projectmesa/mesa)
almost mechanically if desired.

## Run it

```bash
# National ensemble (Phases 1–2)
python3 abm/run.py             # 20-rep ensemble, 25k agents, 2024–2080, adaptation ON (~17 s)
python3 abm/run.py --compare   # adaptation OFF vs ON -> the resilience benefit (~33 s)
python3 abm/run.py --no-adapt  # Phase 1 behaviour (adaptation disabled)

# Phase 4: resilience-lever experiments + global sensitivity analysis
python3 abm/experiments.py     # baseline vs 5 §7 levers, ranked (~40 s)
python3 abm/validation.py      # Sobol S1/ST indices, N=48 (~60 s)

# Phase 3: building-level spatial cascades (feeds the animated map)
python3 abm/building_model.py --all          # all five cities (~40 s)
python3 -m http.server                       # then open abm_dashboard.html in a browser
```

All scripts take `--quick` for a fast smoke run and write to `outputs/`. No
third-party dependencies (pure stdlib). The national run writes
`outputs/abm_results.json` (per-year ensemble percentiles incl. `adaptation_uptake`
/ `mean_risk_perception`, per-gemeente clusters, and a `resilience_comparison`
block under `--compare`); experiments → `abm_experiments.json`; Sobol →
`abm_sobol.json`; building runs → `abm_buildings_<city>.geojson`.

## What makes it an ABM (not the calculator)

| ODD concept | Where it lives |
|---|---|
| **Heterogeneous agents** | `HouseholdAgent` — per-agent home value, mortgage, income, wealth, 5 hazard exposures, LTV, risk perception, adaptation state (`agents.py`) |
| **Time** | annual `step()` loop 2024→2080 (`model.py:step`) |
| **Stochasticity** | per-gemeente hazard events drawn each year; idiosyncratic income shocks; Monte-Carlo ensemble (`climate.py`, `run.py`) |
| **Adaptation** | households install protection when perceived risk × avoided damage clears cost & budget (`agents.py:_decide_adaptation`) |
| **Learning / Sensing** | risk perception updates from personal event experience + neighbour adoption signal (`agents.py:_update_risk_perception`) |
| **Interaction / feedback** | defaults → local price contagion, **plus** lender realized-loss → risk premium → local prices; neighbour adoption → social influence (`model.py:step`) |
| **Emergence** | portfolio mortgage-at-risk, default clusters, **S-shaped adaptation uptake**, and lender insolvency are *outputs*, never imposed |
| **Collectives** | gemeente = shared hazard field + shared price + shared adoption signal |
| **Observation** | `DataCollector` → `outputs/abm_results.json` → existing MapLibre/chart front-end |

## Files

- `agents.py` — `HouseholdAgent` (damage → learning → adaptation → finances → default), `LenderAgent` (DNB-style loss/capital + per-gemeente realized-loss tally)
- `climate.py` — scenario **path** (interpolates the app's 2050/2070/2080 anchors into a yearly trajectory) + stochastic event generator
- `model.py` — synthetic-population builder from the existing `data/*.json`, the annual schedule (events → social signal → decisions → price/lender feedback), the `DataCollector`, all free `Params` (incl. the Phase 4 policy levers)
- `run.py` — t=0 calibration gate + Monte-Carlo ensemble + `--compare` resilience experiment + cluster aggregation + JSON output
- `experiments.py` — Phase 4: runs the baseline + each §7 lever and tabulates the marginal resilience benefit
- `validation.py` — Phase 4: variance-based **Sobol** sensitivity analysis (Saltelli/Jansen estimators, pure stdlib)
- `building_model.py` — Phase 3: building-resolution spatial ABM for the five cities; Moran's-I clustering test; writes per-building geojson for the map
- `../abm_dashboard.html` — Phase 3: MapLibre dashboard that animates the building-level default cascade year by year

## Validation built in (`run.py`)

- **t=0 calibration gate** (ABM_DESIGN §8.1): reproduces national pop-weighted
  **LTV ≈ 67%** (target 68%) and **≈ 4% underwater** (target ~3%) from the
  synthetic population before any climate effect — printed and asserted each run.
- **Consistency with the static tool**: run to 2080 on the extreme path with
  adaptation OFF (`--no-adapt`) the ABM lands at ~39% standing-underwater vs the
  calculator's 66% — *lower because the ABM lets distressed mortgages resolve*
  (default + exit, post-event price recovery, amortization) instead of
  accumulating them forever. The static cross-section overstates the standing
  underwater stock; this flow effect is a concrete thing the ABM captures and the
  formula cannot.
- **Spatial clustering** (§1 pattern 3): the worst-exposure gemeente decile carries
  ~3× its uniform share of all defaults; hardest-hit gemeenten are reported and
  written per-gemeente for the map.
- **S-shaped adaptation uptake** (§1 pattern 1): national uptake is latent through
  ~2035, takes off ~2037→2058 (social influence + accumulating event experience),
  and saturates near ~88% — a textbook logistic curve, not an imposed schedule.

## Headline emergent results (extreme path, 2024→2080, adaptation ON)

National % underwater 4% → ~14%; cumulative default rate ~12%; adaptation uptake
~88% (S-curve); defaults concentrated in the exposed Randstad/delta gemeenten —
*none imposed; all emergent.*

**Resilience experiment (`--compare`, adaptation OFF vs ON):** household
adaptation lowers the 2080 underwater share by **~25 pp** (≈39% → 14%), cumulative
defaults by **~12 pp** (≈25% → 12%), and avoids **≈€29 bn** of lender losses.
Notably the lender's opening capital buffer is still exhausted even *with*
adaptation — i.e. household-level adaptation alone does not keep the portfolio
solvent in the tail, which is exactly the motivation for the Phase 4 policy levers.

## Phase 4 — resilience-lever experiments (`experiments.py`)

All levers run *on top of* autonomous household adaptation, so each figure is the
**marginal** gain of the policy. Ranked by lender loss avoided by 2080 (extreme path):

| Lever | Lender loss avoided | Δ underwater | Δ cum. default |
|---|--:|--:|--:|
| Climate-risk insurance | ≈ €24 bn | +5 pp* | −10 pp |
| Foundation-repair program | ≈ €14 bn | +2 pp* | −5 pp |
| LTV cap at origination (90%) | ≈ €7 bn | −1 pp | −2 pp |
| Adaptation subsidy (50%) | ≈ €5 bn | −2 pp | −2 pp |
| Mandatory disclosure | ≈ €0 bn | ~0 | ~0 |

\*Insurance and foundation-repair *raise* the standing underwater share while
slashing defaults and losses — an emergent nuance: preventing default leaves more
underwater-but-surviving mortgages in the pool. Disclosure is a weak *endpoint*
lever because autonomous social diffusion eventually saturates adaptation anyway;
its value is front-loaded timing a 2080 snapshot understates.

## Phase 4 — global sensitivity analysis (`validation.py`)

Sobol decomposition of the 2080 cumulative default rate onto six behavioural
parameters (N=48 → 384 runs, common random seed so variance is parametric).
`sum(S1) ≈ 1.0` (near-additive). **Adaptation effectiveness** dominates
(S1 ≈ 0.83, ST ≈ 0.43), followed by the default-rule parameters — i.e. *how well
adaptation works* is the single largest uncertainty for portfolio default risk,
which reinforces the thesis focus.

## Phase 3 — building-level spatial cascades (`building_model.py`)

Per-building agents for the five cities, with ~250 m spatial price contagion and a
persistent flood-susceptibility field. Defaults form contiguous street-level
clusters — positive **Moran's I** on per-cell default rate, strongest where default
rates are highest:

| City | cum. default 2080 | Moran's I | worst-decile share | adaptation |
|---|--:|--:|--:|--:|
| Amsterdam | 25% | 0.28 | 38% | 3% |
| Rotterdam | 17% | 0.17 | 42% | 13% |
| Utrecht | 14% | 0.18 | 40% | 50% |
| Den Haag | 10% | 0.13 | 40% | 50% |
| Eindhoven | 3% | 0.01 | 41% | 68% |

An emergent affordability gradient falls out: the exposed, expensive delta cities
adapt least (adaptation is unaffordable at high WOZ) and cluster most, while
low-risk inland Eindhoven adapts most. `abm_dashboard.html` animates the cascade.

> **Verification note:** the Python (Phases 1–4 incl. `building_model.py`) is run
> and verified end-to-end; the numbers above are reproduced from live runs. The
> `abm_dashboard.html` map is validated for JS syntax and data wiring (loads the
> geojson over HTTP), but its visual MapLibre rendering should be eyeballed in a
> browser (`python3 -m http.server`, open the page).
