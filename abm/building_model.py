"""
CLIMADAPT-NL — Building-level spatial model (Phase 3)
=====================================================

The national model (model.py) treats each gemeente as a single collective with one
shared price. Phase 3 (ABM_DESIGN §2.2, §9 step 3) drops to **building resolution**
for the five cities whose PDOK footprints are already downloaded, and makes the
price-contagion feedback **spatial**: a default depresses prices for geographically
*adjacent* buildings (a ~250 m neighbourhood), so distress spreads block-by-block
rather than uniformly across the municipality. Hazard events are also drawn at the
local-cell level, so only part of a city floods in a given year. The result is
emergent, street-level default clusters — the spatial analogue of the national
cascade, and the data layer for the animated map.

Each agent is one BAG building; `bouwjaar` sets a vulnerability multiplier (older
stock = worse foundations / lower flood-defence compliance — §6.1) and the building
polygon centroid places it in space. Behavioural rules (damage → adaptation →
finances → default) mirror the national HouseholdAgent, kept compact inline so the
spatial machinery is self-contained.

Usage:
    python3 abm/building_model.py                  # Amsterdam (~5 s)
    python3 abm/building_model.py --city rotterdam
    python3 abm/building_model.py --all            # all five cities
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random

from climate import chronic_discount, warming, HAZARDS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "outputs")
LTV_STD = 18.0
CELL = 0.0035          # spatial grid cell size in degrees (~250 m at NL latitude)

CITY_GM = {"amsterdam": "GM0363", "rotterdam": "GM0599", "den_haag": "GM0518",
           "utrecht": "GM0344", "eindhoven": "GM0772"}


def age_vulnerability(bouwjaar):
    """Older buildings are more vulnerable (foundations, flood-defence compliance)."""
    if not bouwjaar:
        return 1.15
    if bouwjaar < 1945:
        return 1.30
    if bouwjaar < 1980:
        return 1.12
    if bouwjaar < 2005:
        return 1.00
    return 0.90


def centroid(geom):
    ring = geom["coordinates"][0]
    n = len(ring)
    return sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n


class BuildingAgent:
    __slots__ = ("base_value", "home_value", "mortgage_balance", "wealth", "exposure",
                 "mean_exp", "ltv", "defaulted", "default_year", "adapted",
                 "risk_perception", "cell", "lon", "lat", "bouwjaar")

    def __init__(self, home_value, mortgage, wealth, exposure, rp, lon, lat, bouwjaar):
        self.base_value = home_value
        self.home_value = home_value
        self.mortgage_balance = mortgage
        self.wealth = wealth
        self.exposure = exposure
        self.mean_exp = sum(exposure.values()) / len(exposure)
        self.ltv = 100.0 * mortgage / home_value if home_value > 0 else 0.0
        self.defaulted = False
        self.default_year = None
        self.adapted = False
        self.risk_perception = rp
        self.lon, self.lat = lon, lat
        self.cell = (round(lon / CELL), round(lat / CELL))
        self.bouwjaar = bouwjaar


class BuildingModel:
    def __init__(self, city, params, seed=0):
        random.seed(seed)
        self.city = city
        self.p = params
        self.start, self.end = 2024, 2080
        self._build(city)

    def _build(self, city):
        p = self.p
        gm = CITY_GM[city]
        nl = json.load(open(os.path.join(ROOT, "data", "nl_data.json")))["Gemeente"][gm]
        cr = json.load(open(os.path.join(ROOT, "data", "climate_risk.json")))
        ov = cr["gemeente_overrides"].get(gm, {})
        prov = cr["province_baseline"].get(nl.get("Provincie"), {})
        base_exp = {h: ov.get(h, prov.get(h, 0.30)) for h in HAZARDS}
        woz = (nl.get("Housing", {}).get("Avg WOZ value (x€1k)") or 300.0) * 1000.0
        base_ltv = ov.get("base_ltv") or 68.0
        wealth0 = (nl.get("IncomeWealth", {}).get("Median household wealth (x€1k)") or 50.0) * 1000.0
        edu = (nl.get("HumanCapital", {}).get("High education (%)") or 30.0) / 100.0

        feats = json.load(open(os.path.join(ROOT, "data", "buildings", f"{city}.geojson")))["features"]
        areas = [f["properties"].get("oppervlakte_max") or 0 for f in feats]
        med_area = sorted(a for a in areas if a > 0)[len(areas) // 2] or 100.0

        self.agents = []
        for f in feats:
            pr = f["properties"]
            units = pr.get("aantal_verblijfsobjecten") or 1
            if units < 1:
                continue
            vuln = age_vulnerability(pr.get("bouwjaar"))
            exposure = {h: min(1.0, base_exp[h] * vuln) for h in HAZARDS}
            area = pr.get("oppervlakte_max") or med_area
            area_factor = min(2.5, max(0.5, (area / med_area) ** 0.5))
            home_value = woz * area_factor * math.exp(random.gauss(0, 0.20) - 0.02)
            ltv0 = min(140.0, max(10.0, random.gauss(base_ltv, LTV_STD)))
            mortgage = home_value * ltv0 / 100.0
            wealth = max(0.0, random.gauss(wealth0, wealth0 * 0.6))
            mean_exp = sum(exposure.values()) / len(exposure)
            rp = min(1.0, max(0.0, p["rp_init_base"] + p["rp_exposure_w"] * mean_exp
                              + p["rp_edu_w"] * edu + random.gauss(0, 0.05)))
            lon, lat = centroid(f["geometry"])
            self.agents.append(BuildingAgent(home_value, mortgage, wealth, exposure,
                                             rp, lon, lat, pr.get("bouwjaar")))

        # Spatial index: how many buildings sit in each ~250 m cell (denominator for
        # local default density). Neighbourhood = the 3x3 block of cells around a cell.
        self.cell_count = {}
        for a in self.agents:
            self.cell_count[a.cell] = self.cell_count.get(a.cell, 0) + 1

        # Persistent flood-susceptibility field: spatially CORRELATED (cells share a
        # coarse ~1 km "district" base) so flood-prone neighbourhoods are contiguous
        # and stable over time — the geography that makes default cascades cluster in
        # space, not just in time. (A proxy for low-lying districts / dike rings we
        # don't have per-building elevation for.)
        self.suscept = {}
        for cell in self.cell_count:
            district = (cell[0] // 4, cell[1] // 4)
            base = random.Random(hash(district) & 0xffffffff).random()
            self.suscept[cell] = min(1.0, max(0.05, base + random.gauss(0, 0.12)))

        # Couple flood/pluvial exposure to local susceptibility, so flood-prone
        # districts lose more value BOTH chronically (repricing) and acutely (damage).
        # This gives the chronic channel a persistent spatial gradient — without it,
        # the near-uniform city-wide devaluation swamps the spatial flood signal.
        for a in self.agents:
            s = 0.4 + 1.3 * self.suscept[a.cell]      # ~0.4x (safe) .. ~1.7x (exposed)
            a.exposure["flood"] = min(1.0, a.exposure["flood"] * s)
            a.exposure["pluvial"] = min(1.0, a.exposure["pluvial"] * s)
            a.mean_exp = sum(a.exposure.values()) / len(a.exposure)

        self.cell_pressure = {}       # cell -> current local price pressure
        self.yearly = []              # per-year city aggregates (for the animation timeline)

    def _neighbourhood(self, cell, table, default=0.0):
        cx, cy = cell
        return sum(table.get((cx + dx, cy + dy), default)
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1))

    def step(self, year):
        p = self.p
        w = warming(year)
        # 1. Spatially-coherent hazard: a city-wide "bad year" intensity multiplier
        #    times each cell's persistent susceptibility. Flood-prone districts get
        #    hit together and repeatedly; quiet years hit almost nobody.
        year_intensity = random.expovariate(1.0)
        cell_event = {}
        for cell in self.cell_count:
            p_flood = min(p["event_cap"], p["event_rate"] * w * self.suscept[cell] * year_intensity)
            if random.random() < p_flood:
                cell_event[cell] = min(1.0, self.suscept[cell] * (0.5 + 0.5 * w)
                                       * random.uniform(0.6, 1.4))

        # 2. Social signal: per-cell adoption fraction (neighbourhood).
        adapt_by_cell = {}
        for a in self.agents:
            if a.adapted and not a.defaulted:
                adapt_by_cell[a.cell] = adapt_by_cell.get(a.cell, 0) + 1

        defaults_by_cell = {}
        random.shuffle(self.agents)
        for a in self.agents:
            if a.defaulted:
                continue
            adapt_factor = (1.0 - p["adapt_effectiveness"]) if a.adapted else 1.0

            # 3a. Acute damage from a local event.
            hit = a.cell in cell_event
            if hit:
                dmg = p["damage_fraction"] * cell_event[a.cell] * a.mean_exp * adapt_factor
                a.home_value *= max(0.0, 1.0 - dmg)

            # 3b-c. Learning + adaptation (neighbourhood social influence -> S-curve).
            nb_units = self._neighbourhood(a.cell, self.cell_count)
            nb_adapt = self._neighbourhood(a.cell, adapt_by_cell)
            adopt_frac = nb_adapt / nb_units if nb_units else 0.0
            if hit:
                a.risk_perception += p["rp_event_learn"] * (1 - a.risk_perception)
            a.risk_perception += p["rp_social_learn"] * (adopt_frac - a.risk_perception)
            a.risk_perception = min(1.0, max(0.0, a.risk_perception * (1 - p["rp_decay"])))
            if not a.adapted and p["adapt_cost_frac"] * a.base_value <= p["adapt_budget_frac"] * a.wealth:
                d_now = chronic_discount(year, a.exposure)
                ev = p["damage_fraction"] * a.mean_exp * a.mean_exp * w * p["event_rate"] * p["adapt_horizon"]
                benefit = a.risk_perception * p["adapt_effectiveness"] * (d_now + ev)
                if benefit * (1 + p["adapt_social_weight"] * adopt_frac) > p["adapt_cost_frac"]:
                    a.adapted = True
                    a.wealth -= p["adapt_cost_frac"] * a.base_value

            # 3d. Finances: chronic discount (reduced if adapted) + LOCAL spatial price
            #     pressure from neighbouring defaults; recompute LTV. The chronic
            #     channel is dampened relative to the municipal average so that the
            #     spatial flood channel — not a uniform city-wide trend — drives the
            #     timing of default, which is what produces street-level clusters.
            d = chronic_discount(year, a.exposure) * adapt_factor * p["chronic_dampen"]
            local_pressure = self.cell_pressure.get(a.cell, 0.0)
            target = a.base_value * (1 - d) * (1 - local_pressure)
            a.home_value += p["price_adjust_speed"] * (target - a.home_value)
            a.mortgage_balance *= (1 - p["amortization_rate"])
            a.ltv = 100.0 * a.mortgage_balance / a.home_value if a.home_value > 0 else 999.0

            # 3e. Default: only when underwater AND hit by a payment shock.
            if a.ltv <= 100:
                continue
            shock = hit or (random.random() < p["income_shock_prob"])
            if not shock:
                continue
            neg = (a.ltv - 100.0) / 100.0
            buf = 1.0 if a.wealth < a.mortgage_balance * 0.1 else p["buffer_relief"]
            if random.random() < min(0.95, (p["default_base"] + p["default_slope"] * neg) * buf):
                a.defaulted = True
                a.default_year = year
                defaults_by_cell[a.cell] = defaults_by_cell.get(a.cell, 0) + 1

        # 4. Spatial price feedback: neighbourhood default density -> next-year pressure.
        new_pressure = {}
        for cell in self.cell_count:
            nb_def = self._neighbourhood(cell, defaults_by_cell)
            nb_units = self._neighbourhood(cell, self.cell_count)
            density = nb_def / nb_units if nb_units else 0.0
            new_pressure[cell] = min(p["contagion_cap"], p["contagion_strength"] * density)
        self.cell_pressure = new_pressure

        # record city aggregate
        act = [a for a in self.agents if not a.defaulted]
        uw = sum(1 for a in act if a.ltv > 100)
        self.yearly.append({
            "year": year,
            "pct_underwater": 100.0 * uw / len(act) if act else 0.0,
            "cumulative_default_rate": 100.0 * sum(1 for a in self.agents if a.defaulted) / len(self.agents),
            "adaptation_uptake": 100.0 * sum(1 for a in act if a.adapted) / len(act) if act else 0.0,
        })

    def run(self):
        # t=0 baseline snapshot so the dashboard has a 2024 row.
        uw0 = sum(1 for a in self.agents if a.ltv > 100)
        self.yearly.append({
            "year": self.start,
            "pct_underwater": 100.0 * uw0 / len(self.agents) if self.agents else 0.0,
            "cumulative_default_rate": 0.0, "adaptation_uptake": 0.0,
        })
        for year in range(self.start + 1, self.end + 1):
            self.step(year)
        return self.yearly

    # --- spatial clustering test: Moran's I on per-cell default rate --------------
    def clustering(self):
        """Are high-default cells next to other high-default cells? Computes Moran's I
        — the standard spatial-autocorrelation statistic — on the per-cell default
        rate, with a 3x3 contiguity weight. I≈0 means defaults are scattered at
        random; I>0 means they form contiguous clusters (§1 pattern 3). Also returns
        the share of all defaults falling in the worst-decile cells (concentration)."""
        d_by_cell, n_by_cell = {}, dict(self.cell_count)
        for a in self.agents:
            if a.defaulted:
                d_by_cell[a.cell] = d_by_cell.get(a.cell, 0) + 1
        cells = list(n_by_cell)
        if len(cells) < 10:
            return None
        rate = {c: d_by_cell.get(c, 0) / n_by_cell[c] for c in cells}
        xbar = sum(rate.values()) / len(cells)
        denom = sum((rate[c] - xbar) ** 2 for c in cells) or 1e-9
        num = W = 0.0
        for c in cells:
            cx, cy = c
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    nb = (cx + dx, cy + dy)
                    if nb in rate:
                        num += (rate[c] - xbar) * (rate[nb] - xbar)
                        W += 1
        moran = (len(cells) / W) * (num / denom) if W else 0.0
        # Concentration: defaults in the worst-decile cells (by default count).
        ranked = sorted(d_by_cell.values(), reverse=True)
        k = max(1, len(cells) // 10)
        n_def = sum(d_by_cell.values())
        concentration = 100.0 * sum(ranked[:k]) / n_def if n_def else 0.0
        return moran, concentration, n_def, len(self.agents)

    def to_geojson(self):
        """Slim point FeatureCollection (one point per building) for the animated map:
        carries default_year (None if it survived), final state and bouwjaar."""
        feats = []
        for a in self.agents:
            feats.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [round(a.lon, 5), round(a.lat, 5)]},
                "properties": {
                    # 9999 sentinel (not null) for survivors -> safe in MapLibre <= expressions
                    "default_year": a.default_year if a.default_year else 9999,
                    "underwater": 1 if (a.ltv > 100 and not a.defaulted) else 0,
                    "adapted": 1 if a.adapted else 0,
                    "bouwjaar": a.bouwjaar or 0,
                },
            })
        return {"type": "FeatureCollection", "features": feats}


# Behavioural params mirrored from model.Params defaults (kept as a plain dict so this
# module is import-light and independently runnable).
PARAMS = dict(
    amortization_rate=0.003, price_adjust_speed=0.25, damage_fraction=0.18,
    income_shock_prob=0.012, default_base=0.08, default_slope=0.30, buffer_relief=0.5,
    chronic_dampen=0.5,   # property-level repricing < municipal average (Phase 3)
    event_rate=0.6, event_cap=0.5, contagion_strength=0.8, contagion_cap=0.20,
    adapt_effectiveness=0.5, adapt_cost_frac=0.04, adapt_budget_frac=0.5,
    adapt_social_weight=2.5, adapt_horizon=15, rp_init_base=0.12, rp_exposure_w=0.35,
    rp_edu_w=0.15, rp_event_learn=0.5, rp_social_learn=0.08, rp_decay=0.03,
)


def run_city(city):
    print(f"  building-level run: {city} ({CITY_GM[city]}) ...")
    m = BuildingModel(city, PARAMS, seed=0)
    series = m.run()
    last = series[-1]
    moran, concentration, n_def, total = m.clustering()
    print(f"    {total} buildings | {last['year']}: underwater {last['pct_underwater']:.1f}%, "
          f"cum.default {last['cumulative_default_rate']:.1f}%, adapt {last['adaptation_uptake']:.1f}%")
    print(f"    spatial clustering: Moran's I = {moran:.2f} (0=random, >0=clustered); "
          f"worst-decile cells hold {concentration:.0f}% of defaults")
    os.makedirs(OUT, exist_ok=True)
    gj = m.to_geojson()
    gj["timeline"] = series
    lons = [a.lon for a in m.agents]
    lats = [a.lat for a in m.agents]
    gj["meta"] = {
        "city": city, "gm": CITY_GM[city], "buildings": total,
        "center": [round(sum(lons) / len(lons), 5), round(sum(lats) / len(lats), 5)],
        "morans_i": round(moran, 3), "concentration_pct": round(concentration, 1),
        "final": last,
    }
    path = os.path.join(OUT, f"abm_buildings_{city}.geojson")
    with open(path, "w") as f:
        json.dump(gj, f)
    print(f"    wrote {path} ({len(gj['features'])} points)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", default="amsterdam", choices=list(CITY_GM))
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    print("=" * 64)
    print("CLIMADAPT-NL building-level spatial cascades (Phase 3)")
    print("=" * 64)
    for city in (CITY_GM if args.all else [args.city]):
        run_city(city)


if __name__ == "__main__":
    main()
