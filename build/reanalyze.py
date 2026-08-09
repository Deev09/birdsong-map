#!/usr/bin/env python3
"""
Recompute sound features and spectrograms from the clips we already shipped.

Analysis and clipping are separable: the MP3s in data/clips are the exact audio
users hear, so re-measuring them needs no network at all. That makes tuning the
classifier or the spectrogram a seconds-long loop instead of an hour of polite
re-downloading -- and it guarantees the tags describe the shipped clip rather
than the original field recording.

Usage:  python3 build/reanalyze.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio import CLIPS, DATA, classify, decode, features, log  # noqa: E402


def main():
    species = json.load(open(os.path.join(DATA, "species.json")))
    specs = {}
    counts = {}
    done = fail = 0

    for key, sp in species.items():
        code = sp.get("code") or key
        path = os.path.join(CLIPS, code + ".mp3")
        if not os.path.exists(path):
            continue
        try:
            f = features(decode(path))
            if not f:
                raise RuntimeError("silent")
        except Exception as e:  # noqa: BLE001
            log(f"  ! {sp['name']}: {e}")
            fail += 1
            continue

        tags = classify(f)
        sp["clip"] = "data/clips/" + code + ".mp3"
        sp["sound"] = {
            "tags": tags,
            "pulse": f["pulse"],
            "centroid": f["centroid"],
            "spread": f["spread"],
            "flatness": f["flatness"],
        }
        specs[key] = f["spec"]
        for t in tags:
            counts[t] = counts.get(t, 0) + 1
        done += 1

    with open(os.path.join(DATA, "species.json"), "w") as f:
        json.dump(species, f, separators=(",", ":"))
    with open(os.path.join(DATA, "spectrograms.json"), "w") as f:
        json.dump(specs, f, separators=(",", ":"))

    log(f"reanalyzed {done} clips ({fail} failed)")
    log("sound shapes: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    main()
