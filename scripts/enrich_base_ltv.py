#!/usr/bin/env python3
"""
Derive per-municipality baseline LTV (Loan-to-Value) estimates from CBS data.

Methodology:
  Dutch national average mortgage LTV ≈ 68% (DNB/CBS 2022-2023).
  Three CBS drivers create within-country variation:

  1. Price-to-income (P/I) ratio (WOZ ÷ avg household income):
     Higher P/I → borrowers needed more leverage → higher LTV
     (+1.8 pp per unit above pop-weighted national P/I of 8.52)

  2. Household wealth relative to property value (median wealth ÷ WOZ):
     More accumulated wealth → larger down-payments → lower LTV
     (-22 pp per unit above pop-weighted national wealth/WOZ of 0.355)

  3. Post-2000 housing stock share:
     Newer loans have not been paid down as long → slightly higher LTV
     (+0.18 pp per percentage-point above pop-weighted mean of 17.8%)

  Centering anchors are pop-weighted national means so the
  population-weighted national average LTV comes out to exactly 68%.

  Formula:
    base_ltv = clamp(68
      + (pi - 8.521) * 1.8
      - (wr - 0.355) * 22
      + (post2000 - 17.823) * 0.18,
    45, 90)

  where  pi = woz / hh_income,  wr = median_wealth / woz

Run: python3 scripts/enrich_base_ltv.py
"""

import json

# ── Population-weighted centering anchors (pre-computed from CBS 2022 data) ──
PI_CENTER  = 8.521   # pop-weighted national mean P/I
WR_CENTER  = 0.355   # pop-weighted national mean wealth/WOZ
P2K_CENTER = 17.823  # pop-weighted national mean post-2000 stock %
NATIONAL_LTV = 68.0

def estimate_base_ltv(woz_k, income_k, wealth_k, post2000_pct):
    """Return per-municipality baseline LTV estimate (%), clamped [45, 90]."""
    pi  = woz_k / income_k
    wr  = wealth_k / woz_k
    pi_adj  = (pi  - PI_CENTER)  * 1.8
    wp_adj  = -(wr - WR_CENTER)  * 22
    age_adj = (post2000_pct - P2K_CENTER) * 0.18
    return round(min(90, max(45, NATIONAL_LTV + pi_adj + wp_adj + age_adj)), 1)


# ── Load data ─────────────────────────────────────────────────────────────────
with open("data/climate_risk.json") as f:
    risk = json.load(f)

with open("data/nl_data.json") as f:
    nl = json.load(f)["Gemeente"]

overrides = risk["gemeente_overrides"]

# ── Enrich ────────────────────────────────────────────────────────────────────
updated = 0
skipped = 0

for code, gm_info in nl.items():
    iw      = gm_info.get("IncomeWealth", {})
    housing = gm_info.get("Housing", {})

    woz    = gm_info.get("_woz_value")
    income = iw.get("Avg household income (x€1k)")
    wealth = iw.get("Median household wealth (x€1k)")
    p2k    = housing.get("Post-2000 stock (%)")

    if not all([woz, income, wealth, p2k]):
        skipped += 1
        continue

    if code not in overrides:
        overrides[code] = {}

    overrides[code]["base_ltv"] = estimate_base_ltv(woz, income, wealth, p2k)
    updated += 1

# ── Update meta ───────────────────────────────────────────────────────────────
risk["gemeente_overrides"] = overrides
risk["meta"]["base_ltv_source"] = (
    "CBS Woononderzoek 2022 (WOZ, household income, median wealth, post-2000 stock%) "
    "calibrated so pop-weighted national mean = 68% (DNB 2022 average)"
)
risk["meta"]["base_ltv_formula"] = (
    "base_ltv = clamp(68 + (pi-8.521)*1.8 - (wr-0.355)*22 + (post2000-17.823)*0.18, 45, 90); "
    "pi=WOZ/hh_income, wr=median_wealth/WOZ; centering anchors are pop-weighted national means"
)

with open("data/climate_risk.json", "w") as f:
    json.dump(risk, f, indent=2, ensure_ascii=False)

print(f"base_ltv written for {updated} municipalities  ({skipped} skipped — missing CBS fields)")

# ── Verify weighted mean ──────────────────────────────────────────────────────
total_pop = 0
total_ltv_pop = 0
for code, ov in overrides.items():
    ltv = ov.get("base_ltv")
    pop = nl.get(code, {}).get("Population", 0)
    if ltv and pop:
        total_ltv_pop += ltv * pop
        total_pop += pop

if total_pop:
    print(f"Pop-weighted national mean LTV: {total_ltv_pop/total_pop:.2f}%  (target: 68%)")

# ── Spot-check ────────────────────────────────────────────────────────────────
CHECKS = [
    ("GM0363", "Amsterdam"),
    ("GM0599", "Rotterdam"),
    ("GM0518", "Den Haag"),
    ("GM0344", "Utrecht"),
    ("GM0772", "Eindhoven"),
    ("GM0034", "Almere"),
    ("GM0935", "Maastricht"),
    ("GM1680", "Aa en Hunze"),
    ("GM0505", "Dordrecht"),
    ("GM0995", "Lelystad"),
]
print(f"\n{'Municipality':25s} {'base_ltv':>8s}")
print("-" * 35)
for code, name in CHECKS:
    ltv = overrides.get(code, {}).get("base_ltv", "—")
    print(f"{name:25s} {str(ltv):>8s}%")
