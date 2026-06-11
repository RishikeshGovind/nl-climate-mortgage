#!/usr/bin/env python3
"""
Replace hand-coded flood risk scores with real AHN4 elevation data (PDOK WCS).
For each of the 352 municipalities, queries the median NAP elevation at the
centroid, converts it to a flood risk index, and writes an enriched
climate_risk.json.

Flood risk derivation:
  flood_risk = clamp(0.68 - elevation_m * 0.10, 0.05, 0.95)

Calibration:
  -4 m NAP  (Lelystad polder)  → 1.08 → clamped 0.95
  -1 m NAP  (Rotterdam)        → 0.78
   0 m NAP  (sea level)        → 0.68
  +2 m NAP  (Amsterdam)        → 0.48
  +5 m NAP  (Utrecht hill)     → 0.18
 +16 m NAP  (Eindhoven)        → clamped 0.05

Foundation and drought risk are kept from the existing JSON (manually calibrated;
replacing those requires BRO soil data which needs a separate pipeline).

Run: python3 scripts/enrich_risk_from_ahn.py
"""

import json, io, time, sys
import requests
import numpy as np
import rasterio

AHN_WCS = (
    "https://service.pdok.nl/rws/ahn/wcs/v1_0"
    "?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCoverage"
    "&COVERAGEID=dtm_05m&FORMAT=image/tiff"
    "&SUBSET=Lat({s:.4f},{n:.4f})"
    "&SUBSET=Long({w:.4f},{e:.4f})"
    "&SUBSETTINGCRS=http://www.opengis.net/def/crs/EPSG/0/4326"
    "&OUTPUTCRS=http://www.opengis.net/def/crs/EPSG/0/4326"
)
D = 0.001   # half-side of query bbox in degrees (~100 m)


def centroid(feature):
    """Return (lon, lat) centroid of a GeoJSON feature."""
    geom = feature["geometry"]
    coords = geom["coordinates"]
    if geom["type"] == "MultiPolygon":
        coords = coords[0][0]
    elif geom["type"] == "Polygon":
        coords = coords[0]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return sum(lons) / len(lons), sum(lats) / len(lats)


def query_elevation(lon, lat, retries=3):
    url = AHN_WCS.format(s=lat - D, n=lat + D, w=lon - D, e=lon + D)
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=25)
            if not r.ok:
                time.sleep(1); continue
            with rasterio.open(io.BytesIO(r.content)) as src:
                arr = src.read(1)
                nd  = src.nodata
                valid = arr[(arr != nd) & (arr < 1e10)]
                if len(valid) == 0:
                    return None
                return float(np.median(valid))
        except Exception as exc:
            print(f"    retry {attempt+1}: {exc}")
            time.sleep(1)
    return None


def elev_to_flood_risk(elev_m):
    """Linear NAP elevation → flood risk index, clamped to [0.05, 0.95]."""
    return round(min(0.95, max(0.05, 0.68 - elev_m * 0.10)), 3)


# ── Load inputs ───────────────────────────────────────────────────────────────
with open("data/gemeenten.geojson") as f:
    gj = json.load(f)

with open("data/climate_risk.json") as f:
    risk = json.load(f)

overrides = risk.get("gemeente_overrides", {})

# ── Query AHN for every municipality ─────────────────────────────────────────
results = {}   # gmCode → {elev_m, flood_risk}
features = gj["features"]
total = len(features)

print(f"Querying AHN4 elevation for {total} municipalities…\n")

for i, feat in enumerate(features):
    p    = feat["properties"]
    code = p.get("statcode") or p.get("GM_CODE") or ""
    name = p.get("statnaam") or p.get("GM_NAAM") or code

    lon, lat = centroid(feat)
    elev = query_elevation(lon, lat)

    if elev is not None:
        flood = elev_to_flood_risk(elev)
        results[code] = {"elev_m": round(elev, 2), "flood_risk": flood}
        tag = f"{elev:+.2f}m → {flood:.2f}"
    else:
        tag = "no data (keeping existing)"

    print(f"  [{i+1:3d}/{total}] {name:35s} {code}  {tag}")
    time.sleep(0.18)   # ~6 req/s, polite

# ── Build per-municipality overrides ─────────────────────────────────────────
# Start from existing overrides, update/add flood_risk from AHN.
# Preserve foundation, drought, and note fields.
new_overrides = dict(overrides)   # copy existing

for code, r in results.items():
    if code not in new_overrides:
        new_overrides[code] = {}
    existing = new_overrides[code]
    existing["flood_risk_ahn_m"] = r["elev_m"]       # keep NAP elevation for reference
    existing["flood"] = r["flood_risk"]               # replace flood with AHN-derived value
    if "note" not in existing:
        existing["note"] = f"AHN4-derived flood risk from {r['elev_m']:+.2f} m NAP"

# ── Write enriched climate_risk.json ─────────────────────────────────────────
risk["gemeente_overrides"] = new_overrides
risk["meta"] = risk.get("meta", {})
risk["meta"]["flood_source"] = "AHN4 DTM 0.5m (PDOK WCS) — median NAP elevation at municipality centroid"
risk["meta"]["flood_formula"] = "flood_risk = clamp(0.68 - elev_m * 0.10, 0.05, 0.95)"

with open("data/climate_risk.json", "w") as f:
    json.dump(risk, f, indent=2, ensure_ascii=False)

print(f"\n✓ Written {len(new_overrides)} municipality entries to data/climate_risk.json")
print(f"  AHN data retrieved for {len(results)}/{total} municipalities")
