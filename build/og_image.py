#!/usr/bin/env python3
"""
Render the Open Graph share card: og.png, 1200x630.

Draws the REAL map from the shipped data rather than a mock, using the same
anomaly colouring the app uses, so a shared link previews the actual artifact.
A card that doesn't look like the site is a small lie that costs trust at the
exact moment someone decides whether to click.

Rasterising without a real SVG library: macOS Quick Look renders SVG, but boxes
the output square, so the card is drawn at 1200x630, thumbnailed to 1200x1200
with the content centred, then cropped back with sips. No dependencies beyond
what ships with the OS.

Usage:  python3 build/og_image.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

W, H = 1200, 630
MONTH = "6"          # June: the fullest, most colourful month

SHAPE = {
    "whistle": "#ffd66b", "trill": "#7ddc8a", "chatter": "#ff9a52",
    "hoot": "#6ea8ff", "honk": "#4fd3c4", "buzz": "#c08bff", "screech": "#ff6b7a",
}
ORDER = ["whistle", "trill", "chatter", "hoot", "honk", "buzz", "screech"]

BG, INK, MUTED, ACCENT = "#0d1014", "#eef2f6", "#8a94a3", "#ffb454"


def rings(feature):
    g = feature["geometry"]
    polys = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
    for poly in polys:
        for ring in poly:
            yield ring


def mix(hex_color, pct, base=(16, 21, 28)):
    """Same colour-mix the app applies over the dark map base."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    f = pct / 100.0
    return "#%02x%02x%02x" % tuple(
        int(round(c * f + bc * (1 - f))) for c, bc in zip((r, g, b), base))


def anomaly(regions, species):
    """Which sound each county over-represents versus the state average."""
    vecs = {}
    for gid, r in regions.items():
        w, total = {}, 0
        for k, n in (r["months"].get(MONTH) or [])[:20]:
            s = species.get(k, {}).get("sound")
            if not s or not species[k].get("clip"):
                continue
            for t in s["tags"]:
                w[t] = w.get(t, 0) + n
                total += n
        if total:
            vecs[gid] = [w.get(t, 0) / total for t in ORDER]
    if not vecs:
        return {}
    mean = [sum(v[i] for v in vecs.values()) / len(vecs) for i in range(len(ORDER))]
    out, peak = {}, 1e-6
    for gid, v in vecs.items():
        devs = [v[i] - mean[i] for i in range(len(ORDER))]
        bi = max(range(len(ORDER)), key=lambda i: devs[i])
        out[gid] = (ORDER[bi], devs[bi])
        peak = max(peak, devs[bi])
    return {g: (t, min(1.0, d / peak)) for g, (t, d) in out.items()}


def main():
    geo = json.load(open(os.path.join(DATA, "counties.geojson")))
    species = json.load(open(os.path.join(DATA, "species.json")))
    regions = json.load(open(os.path.join(DATA, "regions.json")))
    anom = anomaly(regions, species)

    # Fit the map to the right-hand side, same equirectangular projection as app.js
    xs = [p[0] for f in geo["features"] for r in rings(f) for p in r]
    ys = [p[1] for f in geo["features"] for r in rings(f) for p in r]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    import math
    k = math.cos(math.radians((miny + maxy) / 2))

    # Keep the map clear of the text column — copy sitting on coloured counties
    # loses too much contrast to read at preview size.
    box_w, box_h = 545, 430
    scale = min(box_w / ((maxx - minx) * k), box_h / (maxy - miny))
    off_x = W - box_w - 34 + (box_w - (maxx - minx) * k * scale) / 2
    off_y = 105 + (box_h - (maxy - miny) * scale) / 2

    def proj(pt):
        return (off_x + (pt[0] - minx) * k * scale, off_y + (maxy - pt[1]) * scale)

    paths = []
    for f in geo["features"]:
        gid = f["properties"]["gid"]
        tag, norm = anom.get(gid, (None, 0))
        fill = mix(SHAPE[tag], 22 + norm * 62) if tag else "#10151c"
        d = ""
        for ring in rings(f):
            d += "".join(("L" if i else "M") + "%.1f %.1f" % proj(p)
                         for i, p in enumerate(ring)) + "Z"
        paths.append(f'<path d="{d}" fill="{fill}" stroke="{BG}" stroke-width="1"/>')

    n_clips = sum(1 for v in species.values() if v.get("clip"))
    dots = "".join(
        f'<circle cx="{62 + i * 34}" cy="497" r="7" fill="{SHAPE[t]}"/>'
        for i, t in enumerate(ORDER))

    # Quick Look always emits a square thumbnail. Handing it a wide canvas made
    # it scale the content to fit the square's *height* and clip the sides.
    # Drawing into a 1200x1200 canvas with the card in a centred 630px band
    # makes the geometry 1:1 and the crop exact.
    PAD = (W - H) // 2
    font = "Helvetica Neue, Helvetica, Arial, sans-serif"
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{W}" viewBox="0 0 {W} {W}">
  <rect width="{W}" height="{W}" fill="{BG}"/>
  <g transform="translate(0 {PAD})">
  {''.join(paths)}
  <text x="60" y="196" font-family="{font}" font-size="76" font-weight="700" fill="{INK}">Birdsong Map</text>
  <text x="60" y="252" font-family="{font}" font-size="32" fill="{ACCENT}">Every county in Iowa.</text>
  <text x="60" y="322" font-family="{font}" font-size="25" fill="{MUTED}">Hover the map to hear its birds.</text>
  <text x="60" y="360" font-family="{font}" font-size="25" fill="{MUTED}">Or search by what a sound was like.</text>
  <text x="60" y="446" font-family="{font}" font-size="22" fill="{INK}">99 counties &#183; {n_clips} bird calls &#183; 12 months</text>
  {dots}
  <text x="60" y="560" font-family="{font}" font-size="21" fill="{MUTED}">deev09.github.io/birdsong-map</text>
  </g>
</svg>'''

    out = os.path.join(ROOT, "og.png")
    with tempfile.TemporaryDirectory() as tmp:
        svg_path = os.path.join(tmp, "og.svg")
        open(svg_path, "w").write(svg)
        subprocess.run(["qlmanage", "-t", "-s", str(W), "-o", tmp, svg_path],
                       capture_output=True, check=False)
        png = svg_path + ".png"
        if not os.path.exists(png):
            print("qlmanage produced no PNG", file=sys.stderr)
            return 1
        shutil.copy(png, out)
        # Quick Look pads to a square; crop the letterboxing back off.
        subprocess.run(["sips", "-c", str(H), str(W), out], capture_output=True, check=False)

    size = os.path.getsize(out)
    print(f"wrote og.png  {W}x{H}  {size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
