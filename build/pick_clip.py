#!/usr/bin/env python3
"""
Choose a REPRESENTATIVE recording for species whose signature sound is famous.

The sound-shape chips are the core feature, and they were giving wrong answers
for the birds people are most likely to have heard: American Robin came out
"screech", Barred Owl "trill, screech" rather than the most recognisable hoot in
North America. The cause is not the classifier — it is which recording got
picked. Barred Owl measured 3821 Hz and flatness 0.315; a "who cooks for you"
hoot is ~300-800 Hz and strongly tonal. Commons simply had several recordings
per species and the pipeline took the first that passed validation, which is
often an alarm call, a juvenile, or a noisy take.

The tempting fix is to hand-override the tag. That is worse: the chip would say
"hoot" and then play something that is not a hoot. Instead, audition every
candidate Commons offers, measure each with the same DSP, and keep the one whose
measured shape matches the species' known signature. The tag stays derived from
the audio, so tag and sound cannot disagree.

Only species with an unambiguous signature belong in WANT. Anything with several
equally-typical vocalisations (chickadee "fee-bee" vs "chick-a-dee") is left
alone deliberately.

Usage:  python3 build/pick_clip.py [--apply]
"""

import argparse
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio import CLIPS, DATA, classify, decode, features, fetch, log  # noqa: E402
from fetch_data import commons_fileinfo, get, valid_audio_file  # noqa: E402

# species -> the tag its best-known vocalisation should produce
WANT = {
    "Strix varia": "hoot",             # "who cooks for you"
    "Turdus migratorius": "whistle",   # caroling "cheerily, cheer up"
    "Bubo virginianus": "hoot",
    "Zenaida macroura": "hoot",        # mournful coo
    "Cardinalis cardinalis": "whistle",  # "birdy birdy birdy"
    "Branta canadensis": "honk",
    "Contopus virens": "whistle",      # "pee-a-wee"
    "Spizella passerina": "trill",     # dry mechanical trill
    "Cyanocitta cristata": "screech",  # harsh jeer
    "Baeolophus bicolor": "whistle",   # "peter peter peter"
}


def candidates(sci, common, limit=12):
    d = get("https://commons.wikimedia.org/w/api.php", {
        "action": "query", "format": "json", "list": "search",
        "srnamespace": 6, "srsearch": f'"{sci}" filetype:audio', "srlimit": limit,
    })
    out = []
    for r in d.get("query", {}).get("search", []):
        f = r["title"].replace("File:", "")
        if valid_audio_file(f, sci, common):
            out.append(f)
    return out


def audition(url, tmp, clip_secs=6.0):
    """Measure a candidate without keeping it."""
    path = os.path.join(tmp, "cand" + os.path.splitext(url.split("?")[0])[1][:6])
    fetch(url, path)
    x = decode(path)
    os.remove(path)
    if len(x) < 22050 // 2:
        return None
    f = features(x)
    if not f:
        return None
    f["tags"] = classify(f)
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    species = json.load(open(os.path.join(DATA, "species.json")))
    by_sci = {v["sci"]: k for k, v in species.items()}
    picks = {}

    with tempfile.TemporaryDirectory() as tmp:
        for sci, want in WANT.items():
            key = by_sci.get(sci)
            if not key:
                log(f"  ? {sci} not in dataset")
                continue
            sp = species[key]
            cur = (sp.get("sound") or {}).get("tags", [])
            if want in cur:
                log(f"  ok   {sp['name']:<24}already {','.join(cur)}")
                continue

            names = candidates(sci, sp["name"])
            info = commons_fileinfo(set(names))
            log(f"  ...  {sp['name']:<24}auditioning {len(names)} candidates (want '{want}', have '{','.join(cur)}')")

            best = None
            for fname in names:
                i = info.get(fname)
                if not i:
                    continue
                try:
                    f = audition(i["url"], tmp)
                except Exception as e:  # noqa: BLE001
                    continue
                if not f:
                    continue
                if want in f["tags"]:
                    best = (fname, i, f)
                    break

            if best:
                fname, i, f = best
                picks[key] = (fname, i)
                log(f"  FIX  {sp['name']:<24}-> {fname[:44]}  {','.join(f['tags'])} "
                    f"(cent={f['centroid']}, flat={f['flatness']})")
            else:
                log(f"  --   {sp['name']:<24}no candidate produced '{want}' — leaving as is")

    if not args.apply:
        log("\ndry run — rerun with --apply")
        return

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
    log(f"\nrewired {len(picks)} species. now: python3 build/audio.py && python3 build/reanalyze.py")


if __name__ == "__main__":
    main()
