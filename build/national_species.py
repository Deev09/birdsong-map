#!/usr/bin/env python3
"""
Resolve every species the national build added, and give it media.

Going national grew the species pool from 257 to 408. Audio is the expensive
axis — it is per-species and shared across every map unit, so it scales with
the species pool rather than with the number of regions. This resolves the new
species' taxonomy, photo, description and audio in one pass, preferring
xeno-canto (which can be asked for an *identified* recording of a *song*) and
falling back to Wikimedia Commons.

Nothing here decides whether a recording is correct — build/verify_audio.py
does that afterwards, against an independent classifier.

Usage:  python3 build/national_species.py [--limit N]
"""

import argparse
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fetch_data import (CACHE, DATA, commons_audio_search, commons_fileinfo,  # noqa: E402
                        load_avicommons, log, resolve_species, trim,
                        wikidata_batch, wikipedia_extracts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    occ = json.load(open(os.path.join(CACHE, "national-occ-top40.json")))
    species = json.load(open(os.path.join(DATA, "species.json")))

    keys = sorted({str(k) for v in occ.values() for k, _ in v})
    new = [k for k in keys if k not in species]
    if args.limit:
        new = new[:args.limit]
    log(f"{len(keys)} species nationally · {len(new)} new to resolve")
    if not new:
        return 0

    log("taxonomy")
    tax = resolve_species([int(k) for k in new])

    avi = load_avicommons()
    scis = sorted({t["sci"] for t in tax.values() if t.get("sci")})

    log("wikidata")
    wd = wikidata_batch(scis)

    log("wikipedia")
    extracts = wikipedia_extracts([wd.get(s, {}).get("title") for s in scis])

    # Audio comes later via xeno_canto.py / audio.py; here we only record the
    # Commons fallback so every species has something to try.
    log(f"commons audio fallback for {len(scis)} species")
    audio_names = {}
    for i, s in enumerate(scis, 1):
        a = avi.get(s.lower())
        name = wd.get(s, {}).get("audio")
        if not name:
            try:
                name = commons_audio_search(s, a["name"] if a else None)
            except Exception:  # noqa: BLE001
                name = None
        if name:
            audio_names[s] = name
        if i % 40 == 0:
            log(f"  {i}/{len(scis)}")

    info = commons_fileinfo(set(audio_names.values()))

    added = 0
    for key, t in tax.items():
        sci = t.get("sci")
        if not sci:
            continue
        a = avi.get(sci.lower())
        title = wd.get(sci, {}).get("title")
        af = audio_names.get(sci)
        ai = info.get(af) if af else None
        species[key] = {
            "sci": sci,
            "name": (a["name"] if a else None) or title or sci,
            "code": a["code"] if a else None,
            "family": t.get("family"),
            "photo": {
                "url": f"https://static.avicommons.org/{a['code']}-{a['key']}-480.jpg",
                "thumb": f"https://static.avicommons.org/{a['code']}-{a['key']}-240.jpg",
                "by": a["by"], "license": a["license"],
            } if a else None,
            "audio": {"url": ai["url"], "page": ai["page"],
                      "by": ai.get("author"), "license": ai.get("license")} if ai else None,
            "blurb": trim(extracts.get(title)) if title else None,
            "wiki": (f"https://en.wikipedia.org/wiki/"
                     f"{urllib.parse.quote(title.replace(' ', '_'))}") if title else None,
        }
        added += 1

    with open(os.path.join(DATA, "species.json"), "w") as f:
        json.dump(species, f, separators=(",", ":"))

    have_photo = sum(1 for k in tax if species.get(k, {}).get("photo"))
    have_audio = sum(1 for k in tax if species.get(k, {}).get("audio"))
    log(f"\nadded {added} species · photo {have_photo}/{added} · "
        f"audio source {have_audio}/{added}")
    log("next: build/xeno_canto.py --all --apply, then audio.py, reanalyze.py, verify_audio.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
