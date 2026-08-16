#!/usr/bin/env python3
"""
Re-pick clips that BirdNET could not confirm, choosing ones it CAN.

verify_audio.py finds clips whose labelled species an independent classifier
cannot hear. This fixes them: for each, audition the Commons candidates through
BirdNET and keep the first one it confirms as the right species.

Why this matters more than it sounds. Filenames are not evidence. Measured on
Commons for Spinus tristis: a file named exactly "American Goldfinch.ogg" is a
Ruby-crowned Kinglet at 1.00 confidence, and "American Goldfinch (enwiki).ogg"
is a human talking. The clip this project shipped for its single
highest-traffic bird — the top species in Polk County in August — was called
30goldfinch.ogg and BirdNET identified it as a Prothonotary Warbler at 1.00.
Eight other candidates were confirmed goldfinch at 0.81-1.00.

pick_clip.py chooses by acoustic SHAPE, which decides whether a clip sounds like
a hoot or a trill. This chooses by IDENTITY, which decides whether it is the
right bird at all. Identity is the one that has to be right.

Requires the BirdNET venv — see build/verify_audio.py for setup.

Usage:
    .venv-birdnet/bin/python build/repick_verified.py            # dry run
    .venv-birdnet/bin/python build/repick_verified.py --apply
"""

import argparse
import json
import os
import sys
import tempfile
import warnings

warnings.filterwarnings("ignore")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio import CLIPS, DATA, fetch, log  # noqa: E402
from fetch_data import commons_fileinfo, get, valid_audio_file  # noqa: E402
from verify_audio import same_species  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIN_CONF = 0.5   # below this BirdNET is guessing; not evidence of identity


def candidates(sci, common, limit=20):
    d = get("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "format": "json", "list": "search",
        "srnamespace": 6, "srsearch": f'"{sci}" filetype:audio', "srlimit": limit,
    })
    return [r["title"].replace("File:", "") for r in d.get("query", {}).get("search", [])
            if valid_audio_file(r["title"].replace("File:", ""), sci, common)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--max-candidates", type=int, default=8)
    args = ap.parse_args()

    from birdnetlib import Recording
    from birdnetlib.analyzer import Analyzer

    species = json.load(open(os.path.join(DATA, "species.json")))
    vpath = os.path.join(DATA, "verification.json")
    if not os.path.exists(vpath):
        log("run verify_audio.py first")
        return 1
    verdicts = {v["name"]: v["status"] for v in json.load(open(vpath))}

    queue = [(k, v) for k, v in species.items()
             if verdicts.get(v.get("name")) in ("ABSENT", "NO-BIRD")]
    log(f"{len(queue)} clips BirdNET could not confirm\n")

    analyzer = Analyzer()
    picks, failed = {}, []

    with tempfile.TemporaryDirectory() as tmp:
        for key, sp in queue:
            sci, name = sp["sci"], sp["name"]
            try:
                names = candidates(sci, name)
            except Exception as e:  # noqa: BLE001
                log(f"  !    {name:<26}{e}")
                continue
            info = commons_fileinfo(set(names))

            found = None
            for fname in names[:args.max_candidates]:
                i = info.get(fname)
                if not i:
                    continue
                p = os.path.join(tmp, "c" + os.path.splitext(i["url"].split("?")[0])[1][:5])
                try:
                    fetch(i["url"], p)
                    rec = Recording(analyzer, p, min_conf=0.05)
                    rec.analyze()
                    os.remove(p)
                except Exception:  # noqa: BLE001
                    continue
                det = sorted(rec.detections, key=lambda d: -d["confidence"])
                if not det:
                    continue
                top = det[0]
                if (top["confidence"] >= MIN_CONF
                        and same_species(sci, name, top["scientific_name"], top["common_name"])):
                    found = (fname, i, top["confidence"])
                    break

            if found:
                fname, i, conf = found
                picks[key] = (fname, i)
                log(f"  FIX  {name:<26}{fname[:42]:<44}conf={conf:.2f}")
            else:
                failed.append(name)
                log(f"  --   {name:<26}no candidate BirdNET confirms")

    log(f"\n{len(picks)} fixable, {len(failed)} not")
    if failed:
        log("still unconfirmed (mostly birds that rarely vocalise): "
            + ", ".join(failed[:12]) + ("…" if len(failed) > 12 else ""))

    if not args.apply:
        log("\ndry run — rerun with --apply")
        return 0

    for key, (fname, i) in picks.items():
        species[key]["audio"] = {"url": i["url"], "page": i["page"],
                                 "by": i.get("author"), "license": i.get("license")}
        code = species[key].get("code") or key
        clip = os.path.join(CLIPS, code + ".mp3")
        if os.path.exists(clip):
            os.remove(clip)
        species[key].pop("clip", None)
        species[key].pop("sound", None)

    with open(os.path.join(DATA, "species.json"), "w") as f:
        json.dump(species, f, separators=(",", ":"))
    log(f"\nrewired {len(picks)}. now: python3 build/audio.py && python3 build/reanalyze.py"
        f" && python3 build/attribution.py, then re-run verify_audio.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
