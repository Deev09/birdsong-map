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

from audio import (CLIPS, DATA, best_window, classify, decode,  # noqa: E402
                   features, fetch, log)
from fetch_data import commons_fileinfo, get, valid_audio_file  # noqa: E402

# species -> the tag its best-known vocalisation should produce
#
# On American Robin: measured across the FULL recordings none of the 14 Commons
# takes reads as a whistle — but the 6 s window that actually ships does, once
# the cleanest source is chosen (flatness 0.128 whole-file -> 0.064 windowed).
# The lesson was that the audition has to measure the same audio the pipeline
# ships, not the raw file.
WANT = {
    "Strix varia": "hoot",             # "who cooks for you"
    "Turdus migratorius": "whistle",   # "cheerily, cheer-up, cheerio"
    "Bubo virginianus": "hoot",
    "Zenaida macroura": "hoot",        # mournful coo
    "Cardinalis cardinalis": "whistle",  # "birdy birdy birdy"
    "Branta canadensis": "honk",
    "Contopus virens": "whistle",      # "pee-a-wee"
    "Spizella passerina": "trill",     # dry mechanical trill
    "Cyanocitta cristata": "screech",  # harsh jeer
    "Baeolophus bicolor": "whistle",   # "peter peter peter"
}


def candidates(sci, common, limit=20):
    # Well-recorded species have more than a dozen takes on Commons and the
    # cleanest is not near the top: American Robin's best (flatness 0.128) was
    # the fourteenth result, outside an earlier limit of 12.
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
    """
    Measure the candidate exactly as it would ship.

    Measuring the whole file is misleading: audio.py ships only the densest
    ~6 s window, which is cleaner and more tonal than the full recording. On
    American Robin the full file measured flatness 0.128 ("chatter") while the
    window it would actually ship measured 0.064 ("whistle"), so the picker and
    the pipeline disagreed and would have fought each other on every rerun.
    """
    path = os.path.join(tmp, "cand" + os.path.splitext(url.split("?")[0])[1][:6])
    fetch(url, path)
    x = decode(path)
    os.remove(path)
    if len(x) < 22050 // 2:
        return None
    start, want = best_window(x, clip_secs)
    f = features(x[start:start + want])
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

            # Audition all of them, then take the CLEANEST match rather than the
            # first. Flatness doubles as a recording-quality proxy: wind, traffic
            # and cicadas all push it up, so the lowest-flatness match is both
            # the most tonal and the least polluted take. Taking the first match
            # left American Robin on the harshest of its fourteen recordings.
            scored = []
            for fname in names:
                i = info.get(fname)
                if not i:
                    continue
                try:
                    f = audition(i["url"], tmp)
                except Exception:  # noqa: BLE001
                    continue
                if f and want in f["tags"]:
                    scored.append((f["flatness"], fname, i, f))

            if scored:
                scored.sort(key=lambda t: t[0])
                _, fname, i, f = scored[0]
                picks[key] = (fname, i)
                log(f"  FIX  {sp['name']:<24}-> {fname[:42]}  {','.join(f['tags'])} "
                    f"(cent={f['centroid']}, flat={f['flatness']}, "
                    f"best of {len(scored)} matching)")
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
