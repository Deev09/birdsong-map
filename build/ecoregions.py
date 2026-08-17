#!/usr/bin/env python3
"""
Label every county with its EPA Level III ecoregion, and work out what is
acoustically distinctive about each one.

County lines are surveyor's arithmetic; birds respond to habitat. EPA ecoregions
are drawn from geology, physiography, vegetation, soils, hydrology and land use,
so an ecoregion boundary IS a habitat boundary. Tested on this project's own
data before building anything: grouping Iowa counties by Level III ecoregion
separates their bird communities significantly better than chance (Bray-Curtis
0.3077 within vs 0.3498 between, permutation p = 0.0045, n = 2,000), and the
species doing the separating are textbook — Bald Eagle, chickadee, Red-bellied
Woodpecker and nuthatch in the forested Driftless bluffs, versus Ring-necked
Pheasant (13x), Killdeer, Barn Swallow and Mallard in the row-crop Western Corn
Belt Plains.

That is the point of this layer. The map already shows WHICH birds; the
ecoregion explains WHY they are the ones here.

Source: EPA ORD "US Level III and IV Ecoregions" MapServer, layer 11.
Keyless, no registration. EPA's Level III/IV metadata carries
"Use Constraints: None" — it is a US federal work in the public domain.

Usage:  python3 build/ecoregions.py
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_data import DATA, get, log  # noqa: E402

LAYER = ("https://gispub.epa.gov/arcgis/rest/services/ORD/"
         "USEPA_Ecoregions_Level_III_and_IV/MapServer/11/query")


def nice(name):
    """EPA ships Level I/II names in caps. .title() alone gives
    "Southeastern Usa Plains"; keep known acronyms intact."""
    out = (name or "").title()
    for a in ("Usa", "Nw", "Ne", "Sw", "Se"):
        out = out.replace(a, a.upper())
    return out


def centroid(feature):
    """Average of the outer ring. Iowa counties are near-rectangular, so this
    lands well inside; anything stranger would want a real point-on-surface."""
    g = feature["geometry"]
    polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
    xs, ys = [], []
    for poly in polys:
        for x, y in poly[0]:
            xs.append(x)
            ys.append(y)
    return sum(xs) / len(xs), sum(ys) / len(ys)


def fetch_polygons(bbox):
    """
    Pull every ecoregion polygon overlapping the area, once.

    One request instead of one per county: measured, a per-point query against
    this service took ~60 s, so 99 counties would have been ~100 minutes. The
    envelope query returns all 7 polygons in under a second, and the rest is
    local arithmetic.

    Filtering by STATE_NAME would have been the obvious shortcut and is wrong —
    it returns only 2 ecoregions for Iowa, silently dropping the Driftless Area,
    whose polygon spans four states and is attributed to another. Intersecting
    a bounding box is the correct question to ask.
    """
    xmin, ymin, xmax, ymax = bbox
    d = get(LAYER, {
        "geometry": json.dumps({"xmin": xmin, "ymin": ymin, "xmax": xmax,
                                "ymax": ymax, "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "US_L3CODE,US_L3NAME,NA_L2NAME,NA_L1NAME",
        "returnGeometry": "true",
        "outSR": "4326",
        "maxAllowableOffset": "0.002",   # ~200 m: far finer than a county
        "geometryPrecision": "5",
        "f": "json",
    })
    out = []
    for f in d.get("features") or []:
        a = f["attributes"]
        rings = [[(p[0], p[1]) for p in r] for r in f["geometry"]["rings"]]
        out.append({
            "code": a.get("US_L3CODE"),
            "name": a.get("US_L3NAME"),
            "l2": nice(a.get("NA_L2NAME")),
            "l1": nice(a.get("NA_L1NAME")),
            "rings": rings,
            "bbox": (min(x for r in rings for x, _ in r),
                     min(y for r in rings for _, y in r),
                     max(x for r in rings for x, _ in r),
                     max(y for r in rings for _, y in r)),
        })
    return out


def in_ring(x, y, ring):
    """Ray casting. Esri uses even-odd across all rings, so holes fall out."""
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xi = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xi:
                inside = not inside
    return inside


def lookup(lon, lat, polys):
    for p in polys:
        bx0, by0, bx1, by1 = p["bbox"]
        if not (bx0 <= lon <= bx1 and by0 <= lat <= by1):
            continue
        if sum(in_ring(lon, lat, r) for r in p["rings"]) % 2 == 1:
            return {k: p[k] for k in ("code", "name", "l2", "l1")}
    return None


def distinctive(members, regions, species, others):
    """
    Which birds are over-represented in this ecoregion versus the rest of the
    state — the sentence that answers "why does it sound like this here".

    Proportions, not raw counts: an ecoregion with more counties would otherwise
    look like it simply has more of everything.
    """
    def profile(gids):
        tot = {}
        n = 0
        for g in gids:
            for m in regions[g]["months"].values():
                for k, c in m:
                    if species.get(k, {}).get("clip"):
                        tot[k] = tot.get(k, 0) + c
                        n += c
        return {k: v / n for k, v in tot.items()} if n else {}

    mine, rest = profile(members), profile(others)
    if not mine or not rest:
        return []
    # Ratio, with a floor on the comparison so a species that is merely absent
    # elsewhere doesn't dominate on a divide-by-almost-zero.
    scored = [(k, p / max(rest.get(k, 0.0), 0.0002)) for k, p in mine.items()
              if p > 0.004]
    scored.sort(key=lambda t: -t[1])
    return [{"key": k, "name": species[k]["name"], "ratio": round(r, 2)}
            for k, r in scored[:6]]


def main():
    geo = json.load(open(os.path.join(DATA, "counties.geojson")))
    regions = json.load(open(os.path.join(DATA, "regions.json")))
    species = json.load(open(os.path.join(DATA, "species.json")))

    # One envelope around every county, then all point-in-polygon locally.
    pts = [(f["properties"]["gid"], f["properties"]["name"], *centroid(f))
           for f in geo["features"]]
    pad = 0.5
    bbox = (min(p[2] for p in pts) - pad, min(p[3] for p in pts) - pad,
            max(p[2] for p in pts) + pad, max(p[3] for p in pts) + pad)
    polys = fetch_polygons(bbox)
    log(f"{len(polys)} ecoregion polygons overlap the area "
        f"({len({p['name'] for p in polys})} distinct)")

    byCounty, misses = {}, []
    for gid, cname, lon, lat in pts:
        eco = lookup(lon, lat, polys)
        if eco and eco.get("name"):
            byCounty[gid] = eco
        else:
            misses.append(cname)

    if misses:
        log(f"  ! no ecoregion for: {', '.join(misses)}")

    groups = {}
    for gid, eco in byCounty.items():
        groups.setdefault(eco["name"], []).append(gid)

    ecos = {}
    for name, members in groups.items():
        others = [g for g in byCounty if g not in members]
        any_member = byCounty[members[0]]

        # Roll the member counties up into a month-by-month species list, in the
        # same shape regions.json uses, so the app can treat an ecoregion as
        # just another clickable unit. No new GBIF calls: this is the county
        # data already on disk, summed.
        months = {}
        for m in map(str, range(1, 13)):
            tot = {}
            for g in members:
                for k, c in regions[g]["months"].get(m, []):
                    tot[k] = tot.get(k, 0) + c
            months[m] = [[k, c] for k, c in
                         sorted(tot.items(), key=lambda t: -t[1])[:40]]

        ecos[name] = {
            "code": any_member["code"],
            "name": name,
            "l2": any_member["l2"],
            "l1": any_member["l1"],
            "counties": len(members),
            "members": members,
            "months": months,
            "distinctive": distinctive(members, regions, species, others),
        }

    out = {"byCounty": {g: e["name"] for g, e in byCounty.items()}, "ecoregions": ecos}
    with open(os.path.join(DATA, "ecoregions.json"), "w") as f:
        json.dump(out, f, separators=(",", ":"))

    log(f"\n{len(ecos)} Level III ecoregions across {len(byCounty)} counties")
    for name, e in sorted(ecos.items(), key=lambda t: -t[1]["counties"]):
        top = ", ".join(d["name"] for d in e["distinctive"][:3])
        log(f"  {e['code']:>2} {name:<34}{e['counties']:>3} counties   {top}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
