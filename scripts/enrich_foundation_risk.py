#!/usr/bin/env python3
"""
Derive real foundation/peat risk per municipality using:
  1. AHN4 NAP elevation already stored in climate_risk.json (elevation proxy for peat presence)
  2. Province soil type modifier from nl_data.json (peat/clay/sandy province classification)

Scientific basis:
  Dutch areas below NAP are almost always Holocene peat/clay polders — the land sank because
  the peat oxidised and compressed after drainage. Current NAP elevation is therefore a direct
  proxy for peat depth and foundation subsidence risk. Province further adjusts for known soil
  geographies (Randstad peat vs. Drenthe/Limburg sand/rock).

Also updates mortgage penetration per municipality using actual CBS owner-occupation rates
from nl_data.json instead of the unit-count proxy.

Run: python3 scripts/enrich_foundation_risk.py
"""

import json, statistics

# ── Province soil type modifier ───────────────────────────────────────────────
# Positive = peat/clay-rich (amplify foundation risk)
# Negative = sandy/rocky (reduce foundation risk)
PROVINCE_SOIL = {
    "Zuid-Holland":  +0.16,   # deepest Holocene peat polders; Groene Hart
    "Noord-Holland": +0.13,   # Amsterdam/Haarlemmermeer peat; old bog districts
    "Utrecht":       +0.11,   # Groene Hart peat; Woerden/De Ronde Venen
    "Friesland":     +0.06,   # coastal marine clay + some peat
    "Groningen":     +0.05,   # heavy clay (klei), gas-extraction subsidence
    "Flevoland":     +0.07,   # reclaimed IJsselmeer; marine clay, some peat
    "Overijssel":    +0.03,   # mixed; Vechtdal peat
    "Gelderland":    +0.00,   # mixed sand/clay; river clay in Betuwe
    "Zeeland":       -0.02,   # marine clay, not much organic peat
    "Noord-Brabant": -0.05,   # Pleistocene sand plateau; Peel moors limited
    "Drenthe":       -0.10,   # coversand plateau; good drainage; very low peat
    "Limburg":       -0.14,   # Maastricht Formation limestone; loess; no peat
}


def elev_to_foundation_risk(elev_m, province):
    """
    Combine NAP elevation with province soil modifier to produce foundation risk.
    Elevation below zero means almost certainly reclaimed peat/clay polder.
    """
    if elev_m < -4.0:   base = 0.90
    elif elev_m < -2.0: base = 0.82
    elif elev_m < -0.5: base = 0.73
    elif elev_m < 1.0:  base = 0.60
    elif elev_m < 3.0:  base = 0.44
    elif elev_m < 8.0:  base = 0.28
    elif elev_m < 20.0: base = 0.16
    else:               base = 0.08

    bonus = PROVINCE_SOIL.get(province, 0.0)
    return round(min(0.95, max(0.05, base + bonus)), 3)


# ── Load data ─────────────────────────────────────────────────────────────────
with open("data/climate_risk.json") as f:
    risk = json.load(f)

with open("data/nl_data.json") as f:
    nl = json.load(f)["Gemeente"]    # { "GM0363": { "Naam":..., "Provincie":..., "Housing":... } }

overrides = risk["gemeente_overrides"]

# ── Process each municipality ─────────────────────────────────────────────────
updated_foundation = 0
updated_mortgage   = 0

for code, gm_info in nl.items():
    province    = gm_info.get("Provincie", "")
    housing     = gm_info.get("Housing", {})
    owner_pct   = housing.get("Owner-occupied (%)")   # CBS % owner-occupied
    social_pct  = housing.get("Social housing (%)")

    if code not in overrides:
        overrides[code] = {}

    ov = overrides[code]

    # ── Foundation risk from elevation + province ─────────────────────────────
    elev = ov.get("flood_risk_ahn_m")
    if elev is not None:
        ov["foundation"] = elev_to_foundation_risk(elev, province)
        ov["foundation_source"] = f"AHN4 {elev:+.2f}m NAP + {province} soil modifier"
        updated_foundation += 1

    # ── Mortgage penetration from CBS owner-occupation rate ───────────────────
    # Among owner-occupied Dutch homes, ~68% carry a bank mortgage (CBS 2022).
    # Social housing carries no individual mortgage; private rental carries none at this level.
    if owner_pct is not None:
        mort_penetration = round((owner_pct / 100) * 0.68, 3)
        ov["mortgage_penetration"] = mort_penetration
        if owner_pct is not None and social_pct is not None:
            ov["owner_occupied_pct"] = owner_pct
            ov["social_housing_pct"] = social_pct
        updated_mortgage += 1

# ── Update meta ───────────────────────────────────────────────────────────────
risk["gemeente_overrides"] = overrides
risk["meta"]["foundation_source"] = (
    "AHN4 NAP elevation (proxy: below-NAP = peat/clay polder) "
    "+ province soil modifier (Randstad peat vs. Drenthe/Limburg sand)"
)
risk["meta"]["foundation_formula"] = (
    "foundation_risk = f(elev_NAP) + province_soil_bonus; "
    "elev<-4m→0.90, -4to-2→0.82, -2to-0.5→0.73, -0.5to1→0.60, "
    "1to3→0.44, 3to8→0.28, 8to20→0.16, >20→0.08; clamped [0.05,0.95]"
)
risk["meta"]["mortgage_source"] = (
    "CBS Woononderzoek 2022: owner-occupied (%) × 0.68 (fraction with bank mortgage)"
)

with open("data/climate_risk.json", "w") as f:
    json.dump(risk, f, indent=2, ensure_ascii=False)

print(f"Foundation risk updated for {updated_foundation} municipalities")
print(f"Mortgage penetration added for {updated_mortgage} municipalities")

# ── Spot-check ────────────────────────────────────────────────────────────────
checks = {
    "GM0363": "Amsterdam",
    "GM0599": "Rotterdam",
    "GM0344": "Utrecht",
    "GM0772": "Eindhoven",
    "GM0935": "Maastricht",
    "GM0034": "Almere",
    "GM0505": "Dordrecht",
    "GM0995": "Lelystad",
}
print(f"\n{'City':15s} {'Code':8s} {'elev_m':>8s} {'province':20s} {'foundation':>10s} {'mort_pen':>8s} {'owner%':>7s}")
print("-" * 90)
for code, name in checks.items():
    ov = overrides.get(code, {})
    elev  = ov.get("flood_risk_ahn_m", "?")
    found = ov.get("foundation", "?")
    mort  = ov.get("mortgage_penetration", "?")
    own   = ov.get("owner_occupied_pct", "?")
    prov  = nl.get(code, {}).get("Provincie", "?")
    elev_s = f"{elev:+.2f}m" if isinstance(elev, float) else str(elev)
    print(f"{name:15s} {code:8s} {elev_s:>8s} {prov:20s} {str(found):>10s} {str(mort):>8s} {str(own):>7s}")
