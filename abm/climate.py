"""
CLIMADAPT-NL — Climate driver (Phase 1 MVP)
===========================================

Turns the existing tool's *static* scenario set into a *dynamic intensification
path* (ABM_DESIGN §3.1–3.2, §5). The four scenarios in js/app.js (baseline 2024,
moderate 2050, severe 2070, extreme 2080) are reinterpreted as anchor points on a
warming trajectory; per-hazard discount factors are linearly interpolated between
anchors for every simulated year. This keeps the ABM numerically consistent with
the front-end while adding the time + stochasticity that an ABM requires.

Two channels, exactly as in the design doc:
  - CHRONIC: a gradual, deterministic devaluation toward WOZ × (1 - discount(year)),
    using the same weighted-exposure discount formula as `municipalDiscount()`.
  - ACUTE:   stochastic hazard *events* per gemeente per year whose probability and
    severity rise with warming and local exposure. Events are drawn once per
    gemeente so all its households are hit together -> spatial/temporal clustering.
"""

from __future__ import annotations

import random

HAZARDS = ("flood", "foundation", "drought", "heat", "pluvial")

# Scenario anchors copied verbatim from js/app.js SCENARIOS (the *_Factor fields),
# keyed by their representative year. These are the per-hazard chronic-discount
# weights at each anchor; intermediate years are interpolated.
SCENARIO_ANCHORS = {
    2024: {"flood": 0.00, "foundation": 0.00, "drought": 0.00, "heat": 0.00, "pluvial": 0.00},
    2050: {"flood": 0.06, "foundation": 0.10, "drought": 0.04, "heat": 0.03, "pluvial": 0.04},
    2070: {"flood": 0.14, "foundation": 0.22, "drought": 0.09, "heat": 0.07, "pluvial": 0.09},
    2080: {"flood": 0.25, "foundation": 0.38, "drought": 0.16, "heat": 0.12, "pluvial": 0.15},
}
_ANCHOR_YEARS = sorted(SCENARIO_ANCHORS)
DISCOUNT_CAP = 0.60  # same cap as municipalDiscount() in app.js


def _interp_factor(year, hazard):
    """Piecewise-linear interpolation of a hazard's discount factor along the path."""
    years = _ANCHOR_YEARS
    if year <= years[0]:
        return SCENARIO_ANCHORS[years[0]][hazard]
    if year >= years[-1]:
        return SCENARIO_ANCHORS[years[-1]][hazard]
    for a, b in zip(years, years[1:]):
        if a <= year <= b:
            t = (year - a) / (b - a)
            fa, fb = SCENARIO_ANCHORS[a][hazard], SCENARIO_ANCHORS[b][hazard]
            return fa + t * (fb - fa)
    return SCENARIO_ANCHORS[years[-1]][hazard]


def chronic_discount(year, exposure):
    """Weighted-exposure chronic discount for a gemeente in a given year — the time
    -varying analogue of municipalDiscount() (app.js §Scenario computation)."""
    d = sum(_interp_factor(year, h) * exposure.get(h, 0.0) for h in HAZARDS)
    return min(d, DISCOUNT_CAP)


def warming(year):
    """Normalized warming level, 0.0 at the 2024 baseline -> 1.0 at the 2080 tail,
    driving acute-event frequency and severity."""
    lo, hi = _ANCHOR_YEARS[0], _ANCHOR_YEARS[-1]
    return max(0.0, min(1.0, (year - lo) / (hi - lo)))


class Climate:
    """Stochastic hazard-event generator. `draw_events` is called once per model
    step and decides, per gemeente, whether a damaging event occurs and how severe
    it is (ODD §3 steps 1–2)."""

    def __init__(self, params):
        self.params = params

    def draw_events(self, year, gemeentes):
        w = warming(year)
        for gem in gemeentes.values():
            # Annual event probability rises with warming and the gemeente's worst
            # single-hazard exposure (one bad hazard is enough to trigger an event).
            max_exp = max(gem["exposure"].values()) if gem["exposure"] else 0.0
            p_event = min(self.params.event_rate * w * max_exp, self.params.event_cap)
            if random.random() < p_event:
                gem["event_this_year"] = True
                # Severity in (0,1], heavier-tailed as warming intensifies.
                gem["event_severity"] = min(1.0, random.expovariate(1.0) * (0.4 + 0.6 * w))
            else:
                gem["event_this_year"] = False
                gem["event_severity"] = 0.0
