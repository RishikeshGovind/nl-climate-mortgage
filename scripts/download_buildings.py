#!/usr/bin/env python3
"""
Download residential building footprints from PDOK BAG for major Dutch cities.
Tiles each city bbox into 0.01°×0.01° cells (PDOK caps at ~1000 per request).
Run once: python3 scripts/download_buildings.py
Re-run a specific city: python3 scripts/download_buildings.py amsterdam
"""
import requests, json, os, time, sys

# Each city bbox covers the visible area at zoom 14-15 around the city center.
# Cell size 0.01°×0.01° keeps each PDOK request well under the ~1000 feature cap.
CITIES = {
    "amsterdam": {"south": 52.346, "west": 4.856, "north": 52.402, "east": 4.950, "name": "Amsterdam"},
    "rotterdam": {"south": 51.888, "west": 4.430, "north": 51.948, "east": 4.540, "name": "Rotterdam"},
    "den_haag":  {"south": 52.053, "west": 4.265, "north": 52.105, "east": 4.360, "name": "Den Haag"},
    "utrecht":   {"south": 52.068, "west": 5.070, "north": 52.115, "east": 5.155, "name": "Utrecht"},
    "eindhoven": {"south": 51.415, "west": 5.420, "north": 51.475, "east": 5.510, "name": "Eindhoven"},
}

CELL = 0.01          # degrees per tile edge
BASE = "https://service.pdok.nl/lv/bag/wfs/v2_0"


def fetch_cell(s, w, n, e, retries=3):
    url = (
        f"{BASE}?service=WFS&version=2.0.0"
        f"&request=GetFeature&typeName=bag:pand"
        f"&outputFormat=application%2Fjson"
        f"&count=1000&srsName=EPSG:4326"
        f"&bbox={s},{w},{n},{e},EPSG:4326"
    )
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=30)
            if r.ok:
                return r.json().get("features", [])
            print(f"    HTTP {r.status_code}, retry {attempt+1}")
        except requests.RequestException as exc:
            print(f"    error: {exc}, retry {attempt+1}")
        time.sleep(1)
    return []


def fetch_city(city_id, cfg):
    s0, w0, n0, e0 = cfg["south"], cfg["west"], cfg["north"], cfg["east"]
    print(f"\n  {cfg['name']}  bbox ({s0},{w0}) → ({n0},{e0})")

    # Build grid of CELL×CELL tiles
    lat = s0
    cells = []
    while lat < n0 - 1e-9:
        lon = w0
        while lon < e0 - 1e-9:
            cells.append((lat, lon, min(lat + CELL, n0), min(lon + CELL, e0)))
            lon = round(lon + CELL, 6)
        lat = round(lat + CELL, 6)
    print(f"  {len(cells)} cells to fetch")

    seen = set()
    all_features = []

    for i, (cs, cw, cn, ce) in enumerate(cells):
        feats = fetch_cell(cs, cw, cn, ce)
        residential = [
            f for f in feats
            if f["properties"].get("status") == "Pand in gebruik"
            and "woonfunctie" in (f["properties"].get("gebruiksdoel") or "")
        ]
        new = 0
        for f in residential:
            iid = f["properties"].get("identificatie", "")
            if iid and iid not in seen:
                seen.add(iid)
                all_features.append(slim(f))
                new += 1
        print(f"    cell {i+1:3d}/{len(cells)}  fetched={len(feats):4d}  residential={len(residential):4d}  new={new:4d}  total={len(all_features):6d}")
        time.sleep(0.1)

    return all_features


def slim(feature):
    """Keep research-relevant properties, round coords to 5 dp (~1 m)."""
    p = feature["properties"]
    geom = feature["geometry"]
    if geom and geom.get("type") == "Polygon":
        geom = {
            "type": "Polygon",
            "coordinates": [
                [[round(x, 5), round(y, 5)] for x, y in ring]
                for ring in geom["coordinates"]
            ]
        }
    return {
        "type": "Feature",
        "geometry": geom,
        "properties": {
            "identificatie":            p.get("identificatie", ""),
            "bouwjaar":                 p.get("bouwjaar", 0),
            "oppervlakte_min":          p.get("oppervlakte_min", 0),
            "oppervlakte_max":          p.get("oppervlakte_max", 0),
            "aantal_verblijfsobjecten": p.get("aantal_verblijfsobjecten", 1),
        },
    }


os.makedirs("data/buildings", exist_ok=True)
targets = sys.argv[1:] or list(CITIES.keys())

for city_id in targets:
    if city_id not in CITIES:
        print(f"Unknown city: {city_id}. Options: {list(CITIES.keys())}")
        continue
    cfg = CITIES[city_id]
    features = fetch_city(city_id, cfg)

    out = {
        "type": "FeatureCollection",
        "meta": {
            "city": city_id,
            "name": cfg["name"],
            "bbox": [cfg["west"], cfg["south"], cfg["east"], cfg["north"]],
        },
        "features": features,
    }
    path = f"data/buildings/{city_id}.geojson"
    with open(path, "w") as fp:
        json.dump(out, fp, separators=(",", ":"))

    size_kb = os.path.getsize(path) / 1024
    print(f"  → {len(features):,} buildings  {size_kb:.0f} KB  →  {path}")
