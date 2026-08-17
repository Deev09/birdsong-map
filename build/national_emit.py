#!/usr/bin/env python3
"""
Emit the national dataset in the shape the app already understands.

The app was built around "a place with a month-by-month species list", so going
national needs no new client concepts — ecoregions simply become the units and
their polygons become the map. The Iowa county files stay untouched beside
these, so both scopes can ship from one codebase.

Writes:
    data/us-ecoregions.geojson   85 Level III polygons (map geometry)
    data/us-regions.json         unit -> month -> [[speciesKey, count], ...]
    data/us-meta.json            counts and provenance

Usage:  python3 build/national_emit.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_data import CACHE, DATA, log  # noqa: E402

MONTHS = [str(m) for m in range(1, 13)]


def main():
    ecos = json.load(open(os.path.join(CACHE, "national-geometry.json")))
    occ = json.load(open(os.path.join(CACHE, "national-occ-top40.json")))
    species = json.load(open(os.path.join(DATA, "species.json")))

    # Geometry as GeoJSON so the client draws it the same way it draws counties.
    feats = []
    for code, e in ecos.items():
        rings = [[[round(x, 3), round(y, 3)] for x, y in r] for r in e["display"] if len(r) >= 4]
        if not rings:
            continue
        feats.append({
            "type": "Feature",
            "properties": {"gid": code, "name": e["name"],
                           "l2": e["l2"], "l1": e["l1"]},
            # Each ring is its own polygon: holes are dropped for the same
            # reason as in the query geometry, and at this zoom nobody can see
            # the difference.
            "geometry": {"type": "MultiPolygon", "coordinates": [[r] for r in rings]},
        })
    geo = {"type": "FeatureCollection", "features": feats}
    with open(os.path.join(DATA, "us-ecoregions.geojson"), "w") as f:
        json.dump(geo, f, separators=(",", ":"))

    regions = {}
    for code, e in ecos.items():
        months = {}
        for m in MONTHS:
            rows = occ.get(f"{code}|{m}", [])
            months[m] = [[str(k), n] for k, n in rows if str(k) in species]
        regions[code] = {"name": e["name"], "l2": e["l2"], "l1": e["l1"],
                         "months": months}
    with open(os.path.join(DATA, "us-regions.json"), "w") as f:
        json.dump(regions, f, separators=(",", ":"))

    keys = {k for r in regions.values() for m in r["months"].values() for k, _ in m}
    clips = sum(1 for k in keys if species.get(k, {}).get("clip"))
    meta = {
        "scope": "Continental US · EPA Level III ecoregions",
        "units": len(regions), "species": len(keys), "withAudio": clips,
        "sources": {
            "occurrence": "GBIF eBird Observation Dataset (CC BY 4.0), doi:10.15468/aomfnb",
            "geometry": "US EPA ORD Level III Ecoregions (public domain)",
        },
    }
    with open(os.path.join(DATA, "us-meta.json"), "w") as f:
        json.dump(meta, f, indent=2)

    gsz = os.path.getsize(os.path.join(DATA, "us-ecoregions.geojson"))
    rsz = os.path.getsize(os.path.join(DATA, "us-regions.json"))
    log(f"{len(regions)} ecoregions · {len(keys)} species · {clips} with audio")
    log(f"geometry {gsz/1024:.0f} KB · regions {rsz/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
