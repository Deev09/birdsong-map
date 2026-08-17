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

import wave  # noqa: E402

import numpy as np  # noqa: E402

from audio import CLIPS, DATA, SR, best_window, decode, fetch, log  # noqa: E402
from fetch_data import commons_fileinfo, get, valid_audio_file  # noqa: E402
from verify_audio import same_species  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# NOT a confidence threshold. BirdNET's scores are "unitless scores that are
# (generally) positively related to prediction accuracy in species-specific
# ways", explicitly "not the result of a probabilistic model" and "not
# necessarily transferrable among studies of the same species" (Wood & Kahl
# 2024). Every published cutoff (0.3, 0.55, 0.65) comes from soundscape work
# and does not transfer to clean focal clips.
#
# An absolute cutoff is therefore biased by species: a loud cardinal clears 0.5
# trivially while a correct Turkey Vulture hiss never will, so the previous
# MIN_CONF = 0.5 was quietly rejecting exactly the quiet, atypical birds this
# queue is made of. Decisions below are RELATIVE instead — rank and margin
# within a restricted label set. This value only discards silence.
NOISE_FLOOR = 0.05


def candidates(sci, common, limit=20):
    d = get("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "format": "json", "list": "search",
        "srnamespace": 6, "srsearch": f'"{sci}" filetype:audio', "srlimit": limit,
    })
    return [r["title"].replace("File:", "") for r in d.get("query", {}).get("search", [])
            if valid_audio_file(r["title"].replace("File:", ""), sci, common)]


def restrict_labels(analyzer, species):
    """
    Cut BirdNET's 6,522 global labels down to the birds this project covers.

    The full label space contributes absurd competitors: earlier runs had a
    Turkey Vulture clip beaten by "Fiji Bush Warbler" and a sandpiper by
    "Scale-throated Earthcreeper". Nothing is learned by ranking an Iowa
    recording against Andean miners, and those distractors displace the real
    answer. Mapping is by same_species() because the two sides use different
    taxonomic backbones.
    """
    ours, matched = [], set()
    for lab in analyzer.labels:
        sci, _, common = lab.partition("_")
        for sp in species.values():
            if same_species(sp["sci"], sp["name"], sci, common):
                ours.append(lab)
                matched.add(sp["name"])
                break
    return ours, matched


def shipped_window(src, dest, clip_secs=6.0):
    """
    Write the exact 6 s window audio.py would ship, so BirdNET judges that.

    Auditioning the whole source file is misleading and was actively wrong here:
    an Eastern Screech-Owl candidate ranked 1st with margin +0.66 across the
    full recording, then failed verification after clipping, because the
    energy-weighted window landed on a different part of the file. Same mistake
    pick_clip.py already had — judge what ships, not what was downloaded.
    """
    x = decode(src)
    if len(x) < SR // 2:
        return False
    start, want = best_window(x, clip_secs)
    seg = np.clip(x[start:start + want], -1.0, 1.0)
    with wave.open(dest, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((seg * 32767).astype("<i2").tobytes())
    return True


def score_candidate(rec, sci, name):
    """
    How confidently is this recording our bird — on a scale-free basis?

    Returns (rank, margin). rank 1 means the target outranks every other
    species in the restricted set. margin is the target's score minus the best
    competitor's, which is comparable ACROSS recordings of the same species in
    a way the raw score is not, and is what picks between two candidates that
    both rank 1.
    """
    best = {}
    for d in rec.detections:
        k = (d["scientific_name"], d["common_name"])
        best[k] = max(best.get(k, 0.0), d["confidence"])
    if not best:
        return None

    ranked = sorted(best.items(), key=lambda kv: -kv[1])
    mine = [c for (s, c_), c in best.items() if same_species(sci, name, s, c_)]
    if not mine:
        return None
    ours = max(mine)
    if ours < NOISE_FLOOR:
        return None

    rank = next(i for i, ((s, c_), _) in enumerate(ranked, 1)
                if same_species(sci, name, s, c_))
    others = [c for (s, c_), c in best.items() if not same_species(sci, name, s, c_)]
    return rank, ours - (max(others) if others else 0.0)


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

    # Restrict the label space before analysing anything.
    probe = Analyzer()
    ours, matched = restrict_labels(probe, species)
    log(f"restricted BirdNET from {len(probe.labels)} labels to {len(ours)} "
        f"({len(matched)}/{len(species)} of our species are in its taxonomy)")
    analyzer = Analyzer(custom_species_list=ours) if ours else probe

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

            # Score every candidate, then take the best — rather than stopping
            # at the first to clear a threshold, which made the result depend on
            # Commons' search order.
            scored = []
            for fname in names[:args.max_candidates]:
                i = info.get(fname)
                if not i:
                    continue
                p = os.path.join(tmp, "c" + os.path.splitext(i["url"].split("?")[0])[1][:5])
                w = os.path.join(tmp, "win.wav")
                try:
                    fetch(i["url"], p)
                    ok = shipped_window(p, w)
                    os.remove(p)
                    if not ok:
                        continue
                    rec = Recording(analyzer, w, min_conf=NOISE_FLOOR)
                    rec.analyze()
                    os.remove(w)
                except Exception:  # noqa: BLE001
                    continue
                s = score_candidate(rec, sci, name)
                if s and s[0] == 1:          # must outrank every other species
                    scored.append((s[1], fname, i))

            if scored:
                scored.sort(key=lambda t: -t[0])   # widest margin wins
                margin, fname, i = scored[0]
                picks[key] = (fname, i)
                log(f"  FIX  {name:<26}{fname[:42]:<44}rank=1 margin={margin:+.2f} "
                    f"({len(scored)}/{len(names[:args.max_candidates])} ranked 1st)")
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
