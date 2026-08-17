#!/usr/bin/env python3
"""
Build the national dataset: all 85 EPA Level III ecoregions of the lower 48.

Why ecoregions rather than counties for the national build:

  counties     3,143 units, 37,716 GBIF calls. GBIF's own guidance says
               anything over ~15 minutes of search API should be a download
               request instead, and this project measured HTTP 429 at 8
               concurrent on just 1,188 calls. It also needs SQL-download
               access, which has to be requested by email.
  ecoregions      85 units, 1,020 calls, ~20 minutes, no permission needed,
               and 100% coverage of the continental US with no gaps.

Ecoregions are also the better answer ecologically. Measured on the Iowa build:
grouping counties by Level III ecoregion separates their bird communities
significantly better than chance (permutation p = 0.0045). County lines are
surveyor's arithmetic; an ecoregion boundary is a habitat boundary, which is
what actually decides which birds are present.

GBIF is queried with a simplified WKT polygon per ecoregion. Verified live: a
45-vertex Driftless Area polygon returns 777,021 June records in 0.9 s, and
simplifying further to 23 vertices moves the count by 0.3% — far inside the
noise of what "which birds live here" means.

Usage:
    python3 build/national.py --geometry     # fetch + simplify polygons
    python3 build/national.py --occurrence   # 1,020 GBIF calls
    python3 build/national.py --all
"""

import argparse
import concurrent.futures as futures
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_data import AVES_TAXON_KEY, DATA, EOD_DATASET, cached, get, log  # noqa: E402
from ecoregions import LAYER, nice  # noqa: E402

CONUS = (-125.0, 24.0, -66.5, 49.5)
MONTHS = [str(m) for m in range(1, 13)]


# ---------------------------------------------------------------- geometry

def dp(pts, eps):
    """Douglas-Peucker, iterative so a 1,000-vertex ring can't blow the stack."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        (x1, y1), (x2, y2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1
        den = dx * dx + dy * dy
        best, bi = -1.0, a
        for i in range(a + 1, b):
            x, y = pts[i]
            if den == 0:
                d = math.hypot(x - x1, y - y1)
            else:
                t = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / den))
                d = math.hypot(x - (x1 + t * dx), y - (y1 + t * dy))
            if d > best:
                best, bi = d, i
        if best > eps:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def ring_area(r):
    return abs(sum(r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1]
                   for i in range(len(r) - 1)) / 2)


def fetch_geometry(offset="0.05"):
    d = get(LAYER, {
        "geometry": json.dumps({"xmin": CONUS[0], "ymin": CONUS[1],
                                "xmax": CONUS[2], "ymax": CONUS[3],
                                "spatialReference": {"wkid": 4326}}),
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "US_L3CODE,US_L3NAME,NA_L2NAME,NA_L1NAME",
        "returnGeometry": "true", "outSR": "4326",
        "maxAllowableOffset": offset, "geometryPrecision": "3", "f": "json",
    })
    return d.get("features") or []


def build_geometry():
    feats = fetch_geometry()
    log(f"{len(feats)} polygons from EPA")

    ecos = {}
    for f in feats:
        a = f["attributes"]
        code = str(a["US_L3CODE"])
        e = ecos.setdefault(code, {
            "code": code, "name": a["US_L3NAME"],
            "l2": nice(a.get("NA_L2NAME")), "l1": nice(a.get("NA_L1NAME")),
            "rings": [],
        })
        for r in f["geometry"]["rings"]:
            e["rings"].append([(round(p[0], 3), round(p[1], 3)) for p in r])

    for e in ecos.values():
        # Drop slivers: tiny rings add vertices and change no answers.
        rings = [r for r in e["rings"] if ring_area(r) > 0.004]
        rings.sort(key=ring_area, reverse=True)
        e["display"] = [dp(r, 0.02) for r in rings[:40]]
        # The query shape is coarser still and keeps only the major parts, so
        # every URL stays comfortably inside GBIF's limits.
        e["query"] = [dp(r, 0.06) for r in rings[:6]]
        del e["rings"]

    disp = sum(len(r) for e in ecos.values() for r in e["display"])
    qv = sum(len(r) for e in ecos.values() for r in e["query"])
    log(f"{len(ecos)} ecoregions · display {disp:,} verts · query {qv:,} verts")
    return ecos


def closed(r):
    r = list(r)
    if r[0] != r[-1]:
        r.append(r[0])
    return r


def clockwise(r):
    r = closed(r)
    return sum((r[i + 1][0] - r[i][0]) * (r[i + 1][1] + r[i][1])
               for i in range(len(r) - 1)) > 0


def hull(pts):
    """Monotone-chain convex hull — the last-resort shape that cannot self-intersect."""
    p = sorted(set(pts))
    if len(p) < 3:
        return closed(p)
    def half(seq):
        out = []
        for q in seq:
            while len(out) > 1 and ((out[-1][0] - out[-2][0]) * (q[1] - out[-2][1])
                                    - (out[-1][1] - out[-2][1]) * (q[0] - out[-2][0])) <= 0:
                out.pop()
            out.append(q)
        return out
    return closed(half(p)[:-1] + half(reversed(p))[:-1])


def wkt(rings):
    """
    MULTIPOLYGON of EXTERIOR rings only, wound counter-clockwise for GBIF.

    Esri encodes holes as rings too — clockwise is exterior, counter-clockwise
    is a hole. Treating every ring as its own exterior polygon (the obvious
    first attempt) makes a hole overlap its parent, and GBIF rejects the whole
    query: "Invalid geometry: Self-intersection". That silently emptied 36 of
    85 ecoregions. Holes are dropped rather than modelled: they very slightly
    overstate an ecoregion's area, which does not change which birds live in it.
    """
    ext = [r for r in rings if clockwise(r)] or [max(rings, key=ring_area)]
    parts = []
    for r in ext:
        r = closed(r)[::-1]          # Esri clockwise -> WKT counter-clockwise
        parts.append("((" + ",".join(f"{x} {y}" for x, y in r) + "))")
    return "MULTIPOLYGON(" + ",".join(parts) + ")"


def shape_variants(rings):
    """
    Progressively safer geometries, best first.

    Simplification can still fold a single ring across itself, so validity is
    checked against GBIF rather than assumed: try the full multipolygon, then
    the largest part alone, then its convex hull, which cannot self-intersect.
    """
    ext = [r for r in rings if clockwise(r)] or list(rings)
    ext.sort(key=ring_area, reverse=True)
    big = ext[0]
    return [
        ("multi", wkt(ext)),
        ("largest", wkt([big])),
        ("hull", wkt([hull([tuple(p) for p in big])[::-1]])),
    ]


# ---------------------------------------------------------------- occurrence

def eco_month(code, shape, month, top):
    d = get("https://api.gbif.org/v1/occurrence/search", {
        "taxonKey": AVES_TAXON_KEY, "datasetKey": EOD_DATASET,
        "geometry": shape, "month": month, "limit": 0,
        "facet": "speciesKey", "facetLimit": top,
        "license": ["CC_BY_4_0", "CC0_1_0"],
    })
    count = d.get("count", 0)
    # Same guard as the county build: GBIF drops unrecognised parameters
    # silently and answers with the whole database.
    if count > 500_000_000:
        raise RuntimeError(f"implausible count {count:,} for {code} m{month}")
    facets = d.get("facets") or []
    if not facets:
        if count > 0:
            raise RuntimeError(f"{count:,} records but no facets for {code}")
        return []
    return [(int(c["name"]), c["count"]) for c in facets[0]["counts"]]


def pick_shape(code, e):
    """Choose the most faithful geometry GBIF will actually accept."""
    for kind, g in shape_variants(e["query"]):
        try:
            eco_month(code, g, 6, 1)
            return kind, g
        except Exception as ex:  # noqa: BLE001
            if "Self-intersection" in str(ex) or "400" in str(ex):
                continue
            raise
    return None, None


def build_occurrence(ecos, top, workers):
    # Validate every polygon once up front rather than discovering per month
    # that a shape is rejected — that is what turned one bad geometry into
    # twelve silent empty months.
    log("validating geometry against GBIF")
    shapes, kinds = {}, {}
    with futures.ThreadPoolExecutor(max_workers=workers) as ex:
        fut = {ex.submit(pick_shape, c, e): c for c, e in ecos.items()}
        for f in futures.as_completed(fut):
            c = fut[f]
            try:
                kind, g = f.result()
            except Exception:  # noqa: BLE001
                kind, g = None, None
            if g:
                shapes[c], kinds[c] = g, kind
            else:
                log(f"  ! {c} {ecos[c]['name'][:30]}: no usable geometry")
    from collections import Counter
    log("  " + ", ".join(f"{k}:{n}" for k, n in Counter(kinds.values()).items()))

    jobs = [(c, m) for c in shapes for m in MONTHS]
    longest = max(len(s) for s in shapes.values())
    log(f"{len(jobs)} GBIF calls · longest WKT {longest:,} chars")

    out, done = {}, 0
    with futures.ThreadPoolExecutor(max_workers=workers) as ex:
        fut = {ex.submit(eco_month, c, shapes[c], m, top): (c, m) for c, m in jobs}
        for f in futures.as_completed(fut):
            c, m = fut[f]
            try:
                out[f"{c}|{m}"] = f.result()
            except Exception as e:  # noqa: BLE001
                log(f"  ! {c} m{m}: {str(e)[:70]}")
                out[f"{c}|{m}"] = []
            done += 1
            if done % 100 == 0:
                log(f"  {done}/{len(jobs)}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--geometry", action="store_true")
    ap.add_argument("--occurrence", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    ecos = cached("national-geometry.json", build_geometry)
    if args.geometry and not (args.all or args.occurrence):
        return 0

    if args.occurrence or args.all:
        occ = cached(f"national-occ-top{args.top}.json",
                     lambda: build_occurrence(ecos, args.top, args.workers))
        empty = sum(1 for v in occ.values() if not v)
        keys = {k for v in occ.values() for k, _ in v}
        log(f"\n{len(keys)} distinct species · {empty}/{len(occ)} empty unit-months")
        with open(os.path.join(DATA, "national-raw.json"), "w") as f:
            json.dump({"ecoregions": ecos, "occurrence": occ}, f, separators=(",", ":"))
        log("wrote data/national-raw.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
