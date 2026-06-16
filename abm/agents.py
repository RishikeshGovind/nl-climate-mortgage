"""
CLIMADAPT-NL — Agents (Phase 2)
===============================

Agent definitions for the mortgage-portfolio climate-risk ABM described in
ABM_DESIGN.md (§2). Deliberately framework-free (pure stdlib) but written in the
canonical ABM idiom — explicit Agent classes each holding their own state and a
`step()` method invoked by the model scheduler — so it is a near-mechanical port
to Mesa (`mesa.Agent` + `model.schedule`) if desired.

Phase 1 (the hard ABM gate): HouseholdAgent default decision + price-contagion
cascade. Phase 2 (this file) adds the decisive empirical-ABM features:

  * Risk perception that updates from personal experience + neighbour signals
    (ODD §4 Learning / Sensing).
  * An adaptation decision (install property-level protection) gated by perceived
    risk, affordability, and *social influence* — which makes uptake S-shaped in
    time (§1 pattern 1, §6 submodel 2). Adapting lowers a household's own climate
    discount and acute damage, so it actually strengthens portfolio resilience.
  * Endogenous lender risk-repricing: realized local losses raise a per-gemeente
    risk premium that further depresses local prices (§6 submodel 5) — a second
    feedback channel on top of the default cascade.

Government levers + building-level cascades remain Phase 3/4.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import random

from climate import chronic_discount


# Household lifecycle states (ODD: state variable `state`, ABM_DESIGN §2.1).
CURRENT = "current"          # LTV <= 100, servicing normally
UNDERWATER = "underwater"    # LTV > 100 but still servicing
DEFAULTED = "defaulted"      # absorbed by lender as a loss; leaves the active pool


class HouseholdAgent:
    """One owner-occupier mortgage. Multiple real households are represented by a
    single agent carrying a `weight` (synthetic-population sampling, §5), so that
    €-denominated portfolio aggregates remain nationally representative even though
    we simulate ~10^4 agents rather than ~10^6 mortgages."""

    __slots__ = (
        "uid", "model", "gemeente", "weight",
        "base_value", "home_value", "mortgage_balance", "income", "wealth",
        "exposure", "mean_exp", "ltv", "state",
        "risk_perception", "adapted", "insured",
        "hit_this_year", "default_year",
    )

    def __init__(self, uid, model, gemeente, weight,
                 home_value, mortgage_balance, income, wealth, exposure,
                 risk_perception):
        self.uid = uid
        self.model = model
        self.gemeente = gemeente            # GM code (collective key, §4 Collectives)
        self.weight = weight                # real mortgages represented by this agent
        self.base_value = home_value        # € pre-climate reference value (fixed)
        self.home_value = home_value        # € current market value (mutates over time)
        self.mortgage_balance = mortgage_balance  # € outstanding principal
        self.income = income                # € annual household income
        self.wealth = wealth                # € liquid/illiquid buffer (stress resistance)
        self.exposure = exposure            # dict: flood/foundation/drought/heat/pluvial in [0,1]
        self.mean_exp = sum(exposure.values()) / len(exposure)
        self.ltv = 100.0 * mortgage_balance / home_value if home_value > 0 else 0.0
        self.state = UNDERWATER if self.ltv > 100 else CURRENT
        self.risk_perception = risk_perception   # 0..1, updated by experience + neighbours
        self.adapted = False                # has installed property-level protection
        self.insured = model.params.policy_insurance   # climate-risk insurance (Phase 4 lever)
        self.hit_this_year = False          # set by climate event resolution each step
        self.default_year = None

    @property
    def adapt_factor(self):
        """Multiplier on climate discount + acute damage once adapted (<1 = protected)."""
        return (1.0 - self.model.params.adapt_effectiveness) if self.adapted else 1.0

    # --- ODD process overview, §3 steps 3a–3e -----------------------------------
    def step(self, year):
        if self.state == DEFAULTED:
            return

        gem = self.model.gemeentes[self.gemeente]
        p = self.model.params

        # 3a. Realize acute damage from any hazard event that struck this gemeente
        #     this year (events are drawn at the model level so they are spatially
        #     correlated within a gemeente — the source of default clustering).
        #     Adapted households take proportionally less damage; insured households
        #     (Phase 4 lever) have most of the damage covered by the pool.
        if self.hit_this_year:
            severity = gem["event_severity"]            # 0..1, drawn this year
            damage_frac = p.damage_fraction * severity * self.mean_exp * self.adapt_factor
            if self.insured:
                damage_frac *= (1.0 - p.policy_insurance_coverage)
            self.home_value *= max(0.0, 1.0 - damage_frac)
        if self.insured:                                 # annual premium drains buffer
            self.wealth = max(0.0, self.wealth - p.policy_insurance_premium * self.home_value)

        # 3b–3c. Learning + adaptation (Phase 2). Skipped when disabled so the model
        #        collapses exactly to the Phase 1 behaviour for comparison runs.
        if p.adaptation_enabled:
            self._update_risk_perception(gem, p)
            if not self.adapted:
                self._decide_adaptation(year, gem, p)

        # 3d. Update finances: chronic climate devaluation (reduced if adapted) +
        #     local price contagion, slow amortization, recompute LTV. Prices drift
        #     toward the household's own climate-adjusted target off its base value.
        d = gem["chronic_d"] * self.adapt_factor
        target = self.base_value * (1.0 - d) * (1.0 - gem["price_pressure"])
        self.home_value += p.price_adjust_speed * (target - self.home_value)
        self.mortgage_balance *= (1.0 - p.amortization_rate)
        self.ltv = 100.0 * self.mortgage_balance / self.home_value if self.home_value > 0 else 999.0

        # 3e. Decide default. Dutch mortgages are full-recourse (and largely NHG-
        #     guaranteed), so households do NOT walk away from negative equity alone.
        #     Default requires a payment shock the household cannot absorb — a damage
        #     event or an idiosyncratic income shock — amplified by the depth of
        #     negative equity and a thin wealth buffer (defaults cluster after
        #     hazards in time — §1 pattern 3).
        if self.ltv <= 100:
            self.state = CURRENT
            return
        self.state = UNDERWATER

        # Insurance covers the post-event payment shock (claim pays the repair bill),
        # so an event no longer forces default for insured households — only an
        # uninsured income shock does (this dampens the post-hazard default cascade).
        event_shock = self.hit_this_year and not self.insured
        shock = event_shock or (random.random() < p.income_shock_prob)
        if not shock:
            return

        neg_equity = (self.ltv - 100.0) / 100.0          # fractional shortfall
        buffer_factor = 1.0 if self.wealth < self.mortgage_balance * 0.10 else p.buffer_relief
        p_default = (p.default_base + p.default_slope * neg_equity) * buffer_factor
        p_default = min(p_default, 0.95)

        if random.random() < p_default:
            self._default(year)

    # --- §4 Learning: risk perception updates from experience + neighbours -------
    def _update_risk_perception(self, gem, p):
        rp = self.risk_perception
        if self.hit_this_year:                       # salient personal experience
            rp += p.rp_event_learn * (1.0 - rp)
        rp += p.rp_social_learn * (gem["adopt_frac"] - rp)   # neighbour signal
        rp *= (1.0 - p.rp_decay)                     # memory fades
        self.risk_perception = min(1.0, max(0.0, rp))

    # --- §6 submodel 2: adaptation decision (social-influenced -> S-curve) -------
    def _decide_adaptation(self, year, gem, p):
        # A government subsidy (Phase 4 lever) lowers the out-of-pocket adaptation cost.
        cost_frac = p.adapt_cost_frac * (1.0 - p.policy_adapt_subsidy)
        # Can the household afford it from its adaptation budget (a share of wealth)?
        if cost_frac * self.base_value > p.adapt_budget_frac * self.wealth:
            return
        # Expected avoided loss as a fraction of home value: the chronic discount it
        # removes plus expected acute damage over a planning horizon.
        w = self.model.current_warming
        ev = p.damage_fraction * self.mean_exp * self.mean_exp * w * p.event_rate * p.adapt_horizon
        avoided_rate = p.adapt_effectiveness * (gem["chronic_d"] + ev)
        benefit = self.risk_perception * avoided_rate
        social = 1.0 + p.adapt_social_weight * gem["adopt_frac"]   # neighbour pull
        if benefit * social > cost_frac:
            self.adapted = True
            self.wealth -= cost_frac * self.base_value            # spend down budget

    def _default(self, year):
        self.state = DEFAULTED
        self.default_year = year
        # Loss given default booked by the lender; recovery = sale at current
        # (depressed) market value net of costs.
        recovery = self.model.params.recovery_rate * self.home_value
        loss = max(0.0, self.mortgage_balance - recovery)
        self.model.lender.book_default(self, loss)
        # Register the default in its gemeente so it pressures local prices next
        # year (interaction via the local housing market, §4 Interaction) and so we
        # can map default clusters at the end of the run (§1 pattern 3).
        gem = self.model.gemeentes[self.gemeente]
        gem["defaults_this_year"] += self.weight
        gem["defaults_cumulative"] += self.weight


@dataclass
class LenderAgent:
    """Single portfolio lender (DNB-style stress accounting, §2.1). Books losses,
    tracks capital, and (Phase 2) maintains a per-gemeente realized-loss tally that
    the model turns into an endogenous risk premium on local prices (§6 submodel 5)."""

    capital_buffer: float                 # € loss-absorbing capital at t=0
    cumulative_loss: float = 0.0          # € realized losses to date
    defaults_total: float = 0.0           # weighted count of defaulted mortgages
    loss_this_year: float = 0.0           # € losses booked in the current step
    gemeente_loss: dict = field(default_factory=dict)  # GM -> € cumulative realized loss

    def begin_year(self):
        self.loss_this_year = 0.0

    def book_default(self, household, loss):
        # `loss` is the per-agent loss-given-default; scale by the agent's weight so
        # losses are on the same nationally-representative € basis as capital_buffer.
        wloss = loss * household.weight
        self.cumulative_loss += wloss
        self.loss_this_year += wloss
        self.defaults_total += household.weight
        gm = household.gemeente
        self.gemeente_loss[gm] = self.gemeente_loss.get(gm, 0.0) + wloss

    @property
    def capital_ratio(self):
        """Remaining capital as a share of the opening buffer (a >0 figure means
        solvent; <=0 means cumulative losses have exhausted the buffer)."""
        if self.capital_buffer <= 0:
            return 0.0
        return max(0.0, (self.capital_buffer - self.cumulative_loss) / self.capital_buffer)
