"""
CLIMADAPT-NL — Model (Phase 1 MVP)
==================================

The Mesa-style Model object for ABM_DESIGN.md: it builds a synthetic household
population from the *existing* data files (no new data collection — §5), owns the
climate driver and the lender, runs the annual schedule (§3), and records agent-
and model-level series via a lightweight DataCollector.

Pure stdlib. A `RandomScheduler` plays the role of Mesa's RandomActivation; swap
this object's three or four hooks for `mesa.Model`/`mesa.time.RandomActivation`
and the rest is unchanged.
"""

from __future__ import annotations

import json
import math
import os
import random
from dataclasses import dataclass, asdict

from agents import HouseholdAgent, LenderAgent, DEFAULTED, UNDERWATER
from climate import Climate, chronic_discount, warming, HAZARDS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NL_DATA = os.path.join(ROOT, "data", "nl_data.json")
CLIMATE_RISK = os.path.join(ROOT, "data", "climate_risk.json")

# National calibration anchors (must be reproduced at t=0 — ABM_DESIGN §1 pattern 4, §8.1).
BASE_LTV_MEAN = 68.0    # % (DNB 2022/2023)
LTV_STD = 18.0          # % (reused from app.js LTV_STD)
DEFAULT_MORTGAGE_PEN = 0.57


@dataclass
class Params:
    """All free parameters in one place (reproducibility / Sobol-ready — §8.4-8.5)."""
    n_agents: int = 25_000          # synthetic population size (weighted to national totals)
    start_year: int = 2024
    end_year: int = 2080
    woz_sigma: float = 0.35         # lognormal spread of home value within a gemeente
    # --- household decision rule (agents.py) ---
    # Stress convention: hold nominal house prices flat absent climate and assume a
    # near-interest-only portfolio, so the climate channel is the dominant LTV driver
    # rather than being washed out by paydown over a 56-year horizon (conservative).
    amortization_rate: float = 0.003    # principal repaid per year
    price_adjust_speed: float = 0.25    # speed prices drift toward chronic target
    damage_fraction: float = 0.18       # max home-value loss from a full-severity event
    income_shock_prob: float = 0.04     # idiosyncratic annual payment-shock probability
    default_base: float = 0.08          # default prob given a shock when just underwater
    default_slope: float = 0.30         # sensitivity to depth of negative equity
    buffer_relief: float = 0.5          # default-prob multiplier for well-buffered households
    recovery_rate: float = 0.85         # share of (depressed) value recovered on default sale
    # --- stochastic climate events (climate.py) ---
    event_rate: float = 0.6             # scales annual event probability with warming x exposure
    event_cap: float = 0.5              # max annual event probability for a gemeente
    # --- local price contagion (the cascade feedback, §3 step 4) ---
    contagion_strength: float = 0.8     # how strongly local defaults depress local prices
    contagion_cap: float = 0.20         # max extra local discount from contagion in a year
    # --- Phase 2: adaptation + social learning (agents.py) ---
    adaptation_enabled: bool = True     # master switch (off => Phase 1 behaviour exactly)
    adapt_effectiveness: float = 0.5    # adaptation cuts a household's discount + damage by this
    adapt_cost_frac: float = 0.04       # up-front adaptation cost as a share of home value
    adapt_budget_frac: float = 0.5      # share of wealth a household will spend to adapt
    adapt_social_weight: float = 2.5    # strength of neighbour social influence (drives the S-curve)
    adapt_horizon: int = 15             # yrs of avoided damage a household anticipates
    rp_init_base: float = 0.12          # baseline risk perception at t=0 (people under-perceive)
    rp_exposure_w: float = 0.35         # exposure contribution to initial perception
    rp_edu_w: float = 0.15              # education contribution to initial perception
    rp_event_learn: float = 0.5         # perception jump after personally experiencing an event
    rp_social_learn: float = 0.08       # perception drift toward neighbour adoption level
    rp_decay: float = 0.03              # annual fading of perception
    # --- Phase 2: endogenous lender risk-repricing (§6 submodel 5) ---
    lender_premium_scale: float = 1.5   # local price pressure per unit of local realized-loss ratio
    lender_premium_cap: float = 0.10    # max extra local discount from lender repricing
    # --- Phase 4: government resilience levers (§7; all off by default = baseline) ---
    policy_ltv_cap: float = 0.0         # cap origination LTV at this % (0 = no cap)
    policy_adapt_subsidy: float = 0.0   # fraction of adaptation cost paid by government (0..1)
    policy_foundation_repair: bool = False  # pre-repair high-foundation-exposure homes at t=0
    policy_foundation_threshold: float = 0.6   # foundation-exposure cutoff for the program
    policy_disclosure: bool = False     # mandatory risk disclosure -> earlier risk perception
    policy_disclosure_boost: float = 1.5    # disclosed perception floor = boost x mean exposure
    policy_insurance: bool = False      # climate-risk insurance pools acute damage
    policy_insurance_coverage: float = 0.8  # share of acute damage covered when insured
    policy_insurance_premium: float = 0.0015  # annual premium as a share of home value


class RandomScheduler:
    """Stand-in for mesa.time.RandomActivation: steps all agents in random order."""

    def __init__(self, model):
        self.model = model
        self.agents = []

    def add(self, agent):
        self.agents.append(agent)

    def step(self, year):
        random.shuffle(self.agents)
        for a in self.agents:
            a.step(year)


class DataCollector:
    """Records one row of model-level metrics per year (Mesa DataCollector analogue)."""

    def __init__(self):
        self.rows = []

    def record(self, model, year):
        underwater_value = 0.0   # € balance currently > 100% LTV (mortgage-at-risk)
        underwater_count = 0.0
        active_count = 0.0
        adapted_count = 0.0      # weighted households that have adapted
        rp_weighted = 0.0        # weighted sum of risk perception (for the mean)
        for a in model.schedule.agents:
            if a.state == DEFAULTED:
                continue
            active_count += a.weight
            rp_weighted += a.risk_perception * a.weight
            if a.adapted:
                adapted_count += a.weight
            if a.ltv > 100:
                underwater_count += a.weight
                underwater_value += a.mortgage_balance * a.weight
        total = model.total_mortgages
        self.rows.append({
            "year": year,
            "pct_underwater": 100.0 * underwater_count / active_count if active_count else 0.0,
            "mortgage_at_risk_eur": underwater_value,
            "active_count": active_count,
            "annual_defaults": model.lender.defaults_total - model._defaults_prev,
            "cumulative_default_rate": 100.0 * model.lender.defaults_total / total if total else 0.0,
            "lender_cumulative_loss_eur": model.lender.cumulative_loss,
            "lender_capital_ratio": model.lender.capital_ratio,
            "adaptation_uptake": 100.0 * adapted_count / active_count if active_count else 0.0,
            "mean_risk_perception": rp_weighted / active_count if active_count else 0.0,
        })
        model._defaults_prev = model.lender.defaults_total


class ClimadaptModel:
    def __init__(self, params: Params, seed=None):
        if seed is not None:
            random.seed(seed)
        self.params = params
        self.schedule = RandomScheduler(self)
        self.climate = Climate(params)
        self.datacollector = DataCollector()
        self._defaults_prev = 0.0
        self.current_warming = 0.0
        self.gemeentes = {}      # GM -> runtime gemeente record (exposure, prices, events)
        self.total_mortgages = 0.0
        self._build_population()
        # Lender capital buffer sized at a DNB-style ~4% of total outstanding balance.
        opening_balance = sum(a.mortgage_balance * a.weight for a in self.schedule.agents)
        self.lender = LenderAgent(capital_buffer=0.04 * opening_balance)

    # ---- §5 Initialization: synthetic population from existing data ------------
    def _build_population(self):
        with open(NL_DATA) as f:
            gem_data = json.load(f)["Gemeente"]
        with open(CLIMATE_RISK) as f:
            cr = json.load(f)
        overrides = cr["gemeente_overrides"]
        prov_base = cr["province_baseline"]

        # 1) Assemble per-gemeente marginals + mortgage counts.
        recs = {}
        for gm, g in gem_data.items():
            housing = g.get("Housing", {})
            iw = g.get("IncomeWealth", {})
            hc = g.get("HumanCapital", {})
            svc = g.get("Services", {})
            ov = overrides.get(gm, {})
            prov = prov_base.get(g.get("Provincie"), {})
            exposure = {h: ov.get(h, prov.get(h, 0.30)) for h in HAZARDS}
            woz = (housing.get("Avg WOZ value (x€1k)") or g.get("_woz_value") or 300.0) * 1000.0
            hh_size = housing.get("Avg household size") or 2.2
            pop = g.get("Population") or 0
            pen = ov.get("mortgage_penetration") or DEFAULT_MORTGAGE_PEN
            households = (pop / hh_size) if hh_size else 0
            mortgages = households * pen
            if mortgages <= 0:
                continue
            recs[gm] = {
                "name": g.get("Naam", gm),
                "exposure": exposure,
                "woz": woz,
                "income": (iw.get("Avg household income (x€1k)") or 35.0) * 1000.0,
                "wealth": (iw.get("Median household wealth (x€1k)") or 50.0) * 1000.0,
                "base_ltv": ov.get("base_ltv") or BASE_LTV_MEAN,
                "mortgages": mortgages,
                # risk-perception drivers (§5: education + degree of urbanisation):
                "edu_high": (hc.get("High education (%)") or 30.0) / 100.0,
                "urban": (svc.get("Degree of urbanisation (1–5)") or 3.0) / 5.0,
                # runtime fields used during stepping:
                "price_pressure": 0.0,
                "chronic_d": 0.0,        # this year's chronic climate discount (set each step)
                "adopt_frac": 0.0,       # share of local households adapted (social signal)
                "opening_balance": 0.0,  # € t=0 outstanding balance (for lender loss ratio)
                "defaults_this_year": 0.0,
                "defaults_cumulative": 0.0,
                "event_this_year": False,
                "event_severity": 0.0,
            }
        self.total_mortgages = sum(r["mortgages"] for r in recs.values())
        self.gemeentes = recs

        # 2) Allocate n_agents proportionally to mortgage count; each agent carries a
        #    weight so €-aggregates equal the true national portfolio.
        p = self.params
        mean_exp_by_gem = {gm: sum(r["exposure"].values()) / len(r["exposure"])
                           for gm, r in recs.items()}
        uid = 0
        for gm, r in recs.items():
            share = r["mortgages"] / self.total_mortgages
            n = max(1, round(p.n_agents * share))
            weight = r["mortgages"] / n
            # Initial risk perception: low baseline + exposure + education signal (§5).
            rp_gem = (p.rp_init_base + p.rp_exposure_w * mean_exp_by_gem[gm]
                      + p.rp_edu_w * r["edu_high"])
            foundation_exp = r["exposure"].get("foundation", 0.0)
            for _ in range(n):
                home_value = r["woz"] * math.exp(random.gauss(0, p.woz_sigma)
                                                 - 0.5 * p.woz_sigma ** 2)
                ltv0 = min(140.0, max(10.0, random.gauss(r["base_ltv"], LTV_STD)))
                if p.policy_ltv_cap > 0:                  # §7 lever: LTV cap at origination
                    ltv0 = min(ltv0, p.policy_ltv_cap)
                mortgage = home_value * ltv0 / 100.0
                income = max(8000.0, random.gauss(r["income"], r["income"] * 0.30))
                wealth = max(0.0, random.gauss(r["wealth"], r["wealth"] * 0.60))
                rp0 = rp_gem + random.gauss(0, 0.05)
                if p.policy_disclosure:                   # §7 lever: mandatory disclosure
                    rp0 = max(rp0, p.policy_disclosure_boost * mean_exp_by_gem[gm])
                agent = HouseholdAgent(uid, self, gm, weight, home_value, mortgage,
                                       income, wealth, dict(r["exposure"]),
                                       min(1.0, max(0.0, rp0)))
                # §7 lever: foundation-repair program pre-adapts high-risk homes at t=0.
                if p.policy_foundation_repair and foundation_exp >= p.policy_foundation_threshold:
                    agent.adapted = True
                self.schedule.add(agent)
                r["opening_balance"] += mortgage * weight
                uid += 1

    # ---- §3 Process overview and scheduling ------------------------------------
    def step(self, year):
        p = self.params
        self.lender.begin_year()
        # 1-2. Climate path + stochastic events (per gemeente).
        self.current_warming = warming(year)
        for r in self.gemeentes.values():
            r["chronic_d"] = chronic_discount(year, r["exposure"])
            r["defaults_this_year"] = 0.0
        self.climate.draw_events(year, self.gemeentes)

        # Social signal: per-gemeente adoption fraction from last year's states, and
        # flag households in gemeenten struck this year. One pass over agents.
        for r in self.gemeentes.values():
            r["_adapt_w"] = 0.0
        for a in self.schedule.agents:
            gem = self.gemeentes[a.gemeente]
            a.hit_this_year = gem["event_this_year"]
            if a.adapted:
                gem["_adapt_w"] += a.weight
        for r in self.gemeentes.values():
            r["adopt_frac"] = r["_adapt_w"] / r["mortgages"] if r["mortgages"] else 0.0

        # 3. Household decisions (random activation).
        self.schedule.step(year)

        # 4-5. Local price feedback: this year's default density (cascade) PLUS the
        #      lender's endogenous risk premium from cumulative local realized losses
        #      (§6 submodel 5) -> next year's local price pressure.
        for gm, r in self.gemeentes.items():
            density = r["defaults_this_year"] / r["mortgages"] if r["mortgages"] else 0.0
            cascade = min(p.contagion_cap, p.contagion_strength * density)
            ob = r["opening_balance"]
            loss_ratio = self.lender.gemeente_loss.get(gm, 0.0) / ob if ob else 0.0
            lender_premium = min(p.lender_premium_cap, p.lender_premium_scale * loss_ratio)
            r["price_pressure"] = cascade + lender_premium

        # 7. Record metrics.
        self.datacollector.record(self, year)

    def run(self):
        # t=0 snapshot (calibration check happens here, before any climate effect).
        self.datacollector.record(self, self.params.start_year)
        self._defaults_prev = 0.0
        for year in range(self.params.start_year + 1, self.params.end_year + 1):
            self.step(year)
        return self.datacollector.rows

    def gemeente_default_rates(self):
        """End-of-run cumulative default rate per gemeente (for the spatial cluster
        map and the concentration check, §1 pattern 3 / §7 outputs)."""
        out = {}
        for gm, r in self.gemeentes.items():
            rate = 100.0 * r["defaults_cumulative"] / r["mortgages"] if r["mortgages"] else 0.0
            out[gm] = {
                "name": r["name"],
                "default_rate": rate,
                "mortgages": r["mortgages"],
                "worst_exposure": max(r["exposure"].values()) if r["exposure"] else 0.0,
            }
        return out
