#!/usr/bin/env python3
"""
Cross-check every shipped clip against BirdNET — an independent classifier.

The worst failure this project can have is teaching someone the wrong bird. The
species label on a clip comes from whoever uploaded the recording, so it is worth
exactly as much as their identification. This asks a second, independent opinion.

WHAT A DISAGREEMENT MEANS — read before acting on the output:
BirdNET disagreeing does NOT mean the clip is wrong. Measured here, its top
prediction for the Barred Owl clip was Spotted Owl — a sister species that
hybridises with it — and for Chipping Sparrow it was Black-chinned Sparrow,
another dry-trilling sparrow. Both clips are fine. Auto-rejecting on
disagreement would have thrown away correct audio and kept nothing better.

So the useful question is not "is the label top-1" but "does the labelled
species appear at all, and how strongly". Three outcomes:

  CONFIRMED  labelled species is BirdNET's top detection
  PRESENT    labelled species detected, but something scored higher
             -> usually a confusion pair; review only if the top is unrelated
  ABSENT     labelled species not detected at all
             -> the real review queue

Nothing here edits data. It prints a queue for a human.

SETUP (BirdNET is not a project dependency; keep it in its own venv):
    python3.12 -m venv .venv-birdnet
    .venv-birdnet/bin/pip install birdnetlib librosa ai-edge-litert audioread resampy

    # birdnetlib imports tflite_runtime, which has no macOS arm64 wheel.
    # ai-edge-litert is the same LiteRT Interpreter API at 177 KB rather than
    # TensorFlow's ~400 MB, so alias it:
    SP=$(.venv-birdnet/bin/python -c "import site;print(site.getsitepackages()[0])")
    mkdir -p $SP/tflite_runtime
    echo 'from ai_edge_litert.interpreter import *' > $SP/tflite_runtime/interpreter.py
    touch $SP/tflite_runtime/__init__.py

    .venv-birdnet/bin/python build/verify_audio.py

LICENCE: BirdNET-Analyzer's code is MIT but its MODELS are CC BY-NC-SA 4.0.
This project is free and non-commercial, so offline validation is within those
terms. It is a build-time check only — no model or prediction ships to users.
"""

import json
import os
import sys
import warnings

warnings.filterwarnings("ignore")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")

# Species BirdNET routinely confuses with each other. A swap inside a pair is
# weak evidence of a mislabel, so it is reported but not treated as suspicious.
# Not an exhaustive list — it grows as the review queue is worked through.
KNOWN_CONFUSIONS = [
    {"Barred Owl", "Spotted Owl"},
    {"Chipping Sparrow", "Black-chinned Sparrow", "Dark-eyed Junco",
     "Pine Warbler", "Worm-eating Warbler"},
    {"Downy Woodpecker", "Hairy Woodpecker"},
    {"House Finch", "Purple Finch", "Cassin's Finch"},
    {"Willow Flycatcher", "Alder Flycatcher"},
    {"Common Raven", "American Crow", "Fish Crow"},
]

# BirdNET also emits non-bird classes; these mean "I heard noise, not a bird".
NON_BIRD = {"Engine", "Siren", "Human vocal", "Human non-vocal", "Human whistle",
            "Dog", "Power tools", "Fireworks", "Gun", "Environmental", "Noise"}


def confusable(a, b):
    return any(a in g and b in g for g in KNOWN_CONFUSIONS)


def norm(s):
    return " ".join((s or "").lower().replace("-", " ").split())


def same_species(my_sci, my_name, their_sci, their_name):
    """
    Do these two names refer to the same bird?

    Neither key alone is enough, because the two sides use different taxonomic
    backbones. GBIF calls Evening Grosbeak *Hesperiphona vespertina*; BirdNET
    follows eBird/Clements with *Coccothraustes vespertinus* — same bird, and a
    scientific-name comparison alone reported it ABSENT while BirdNET was
    naming it at 1.00 confidence. Common names drift too (case, hyphens, and
    recent splits like "Northern House Wren" vs "House Wren"), so accept
    agreement on any one of three signals and let the rest fall to review.
    """
    if norm(my_sci) == norm(their_sci):
        return True
    a, b = norm(my_name), norm(their_name)
    if a == b:
        return True
    ta, tb = a.split(), b.split()
    # one is a qualified form of the other: "northern house wren" / "house wren"
    return bool(ta) and bool(tb) and (ta[-len(tb):] == tb or tb[-len(ta):] == ta)


def main():
    try:
        from birdnetlib import Recording
        from birdnetlib.analyzer import Analyzer
    except ImportError as e:
        print(f"BirdNET not installed in this interpreter ({e}).\n"
              f"See the setup block at the top of this file.", file=sys.stderr)
        return 2

    species = json.load(open(os.path.join(DATA, "species.json")))
    clips = [(k, v) for k, v in species.items() if v.get("clip")]
    analyzer = Analyzer()

    rows = []
    for i, (key, sp) in enumerate(clips, 1):
        path = os.path.join(ROOT, sp["clip"])
        if not os.path.exists(path):
            continue
        try:
            rec = Recording(analyzer, path, min_conf=0.03)
            rec.analyze()
        except Exception as e:  # noqa: BLE001
            rows.append((sp["name"], "ERROR", 0.0, str(e)[:40], 0))
            continue

        # Match on SCIENTIFIC name, not common name. Comparing common names
        # produced a wall of false alarms that were pure naming drift:
        # "Sandhill crane" vs "Sandhill Crane" (case), "Black-crowned Night
        # Heron" vs "Night-Heron" (hyphen), "Northern House Wren" vs "House
        # Wren" (a recent split), and rows where our own name had fallen back
        # to the binomial. Latin is the stable key both sides agree on.
        best, sci_of = {}, {}
        for d in rec.detections:
            s = d["scientific_name"]
            if d["confidence"] > best.get(s, 0.0):
                best[s] = d["confidence"]
                sci_of[s] = d["common_name"]
        ranked = sorted(best.items(), key=lambda t: -t[1])

        name = sp["name"]
        mine = max([c for sc, c in best.items()
                    if same_species(sp["sci"], name, sc, sci_of[sc])] or [0.0])
        if ranked:
            top_sci, topc = ranked[0]
            top = sci_of[top_sci]
        else:
            top_sci, top, topc = "", "(nothing)", 0.0

        if ranked and same_species(sp["sci"], name, top_sci, top):
            status = "CONFIRMED"
        elif mine > 0:
            status = "PRESENT"
        elif confusable(name, top):
            status = "CONFUSABLE"
        elif top in NON_BIRD or top == "(nothing)":
            status = "NO-BIRD"
        else:
            status = "ABSENT"

        rank = next((j for j, (s_, _) in enumerate(ranked, 1)
                     if same_species(sp["sci"], name, s_, sci_of[s_])), 0)
        rows.append((name, status, mine, top + f" {topc:.2f}", rank))
        if i % 25 == 0:
            print(f"  … {i}/{len(clips)}", file=sys.stderr)

    order = {"ABSENT": 0, "NO-BIRD": 1, "CONFUSABLE": 2, "PRESENT": 3,
             "CONFIRMED": 4, "ERROR": 0}
    rows.sort(key=lambda r: (order.get(r[1], 9), -r[2]))

    counts = {}
    for _, st, *_ in rows:
        counts[st] = counts.get(st, 0) + 1

    print("\n=== REVIEW QUEUE (worst first) ===")
    print(f"{'species':<26}{'status':<12}{'own':>6}{'rank':>5}  birdnet top")
    for name, st, mine, top, rank in rows:
        if st in ("CONFIRMED", "PRESENT"):
            continue
        print(f"{name:<26}{st:<12}{mine:>6.2f}{rank:>5}  {top}")

    total = len(rows)
    print(f"\n=== SUMMARY ({total} clips) ===")
    for st in ["CONFIRMED", "PRESENT", "CONFUSABLE", "NO-BIRD", "ABSENT", "ERROR"]:
        if counts.get(st):
            print(f"  {st:<12}{counts[st]:>4}  {counts[st] * 100 / total:>5.1f}%")
    agree = counts.get("CONFIRMED", 0) + counts.get("PRESENT", 0)
    print(f"\n  labelled species detected in {agree}/{total} "
          f"({agree * 100 / total:.1f}%)")

    with open(os.path.join(DATA, "verification.json"), "w") as f:
        json.dump([{"name": n, "status": s, "own_conf": round(c, 3),
                    "rank": r, "birdnet_top": t} for n, s, c, t, r in rows],
                  f, indent=1)
    print("  wrote data/verification.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
