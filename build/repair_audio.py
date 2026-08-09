#!/usr/bin/env python3
"""
Re-verify every audio link that came from the Commons *search* fallback.

The first build searched Commons unquoted, which returned confidently wrong
species (a Grey Butcherbird filed under Loggerhead Shrike, a Virginia Rail under
Rusty Blackbird, a LibriVox magazine reading under Bobolink). Links that came
from Wikidata P51 are hand-curated and are left alone.

Anything that no longer resolves loses its audio entirely. A species with no
player is honest; a species playing the wrong bird is not.

Usage:  python3 build/repair_audio.py [--apply]
"""

import argparse
import concurrent.futures as futures
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import urllib.parse  # noqa: E402

from fetch_data import (CACHE, DATA, commons_audio_search, commons_fileinfo,  # noqa: E402
                        log, valid_audio_file)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes")
    args = ap.parse_args()

    species = json.load(open(os.path.join(DATA, "species.json")))
    search_cache = json.load(open(os.path.join(CACHE, "commons-search.json")))
    wd = json.load(open(os.path.join(CACHE, "wikidata.json")))

    # Only the search-derived links are suspect.
    suspect = {k: v for k, v in species.items()
               if v.get("audio") and v.get("sci") in search_cache
               and not (wd.get(v["sci"], {}) or {}).get("audio")}
    log(f"{len(suspect)} search-derived links to re-verify "
        f"({sum(1 for v in species.values() if v.get('audio'))} total with audio)")

    old = {k: urllib.parse.unquote(species[k]["audio"]["url"].split("/")[-1].split("?")[0])
           for k in suspect}

    def check(item):
        k, v = item
        # If what we already have passes the gate, keep it. Re-searching would
        # swap one perfectly good Blue Jay recording for another and force a
        # pointless re-download and re-clip.
        if valid_audio_file(old[k].replace("_", " "), v["sci"], v["name"]):
            return k, old[k]
        return k, commons_audio_search(v["sci"], v["name"])

    found = {}
    with futures.ThreadPoolExecutor(max_workers=3) as ex:
        for k, fname in ex.map(check, suspect.items()):
            found[k] = fname

    changed = [k for k, f in found.items() if f and f.replace(" ", "_") != old[k].replace(" ", "_")]
    dropped = [k for k, f in found.items() if not f]
    kept = [k for k in found if k not in changed and k not in dropped]

    log(f"  kept {len(kept)} · replaced {len(changed)} · dropped {len(dropped)}")
    for k in dropped:
        log(f"    DROP {species[k]['name']:<26} (was {old[k][:52]})")
    for k in changed:
        log(f"    SWAP {species[k]['name']:<26} {old[k][:34]} -> {found[k][:40]}")

    if not args.apply:
        log("\ndry run — rerun with --apply to write")
        return

    info = commons_fileinfo({found[k] for k in changed})
    for k in changed:
        i = info.get(found[k])
        if not i:
            dropped.append(k)
            continue
        species[k]["audio"] = {"url": i["url"], "page": i["page"],
                               "by": i.get("author"), "license": i.get("license")}
        search_cache[species[k]["sci"]] = found[k]

    for k in dropped:
        # Drop the stale clip too, so audio.py refetches nothing and the UI
        # simply shows no player.
        code = species[k].get("code") or k
        clip = os.path.join(DATA, "clips", code + ".mp3")
        if os.path.exists(clip):
            os.remove(clip)
        species[k]["audio"] = None
        species[k].pop("clip", None)
        species[k].pop("sound", None)
        search_cache.pop(species[k]["sci"], None)

    for k in changed:
        code = species[k].get("code") or k
        clip = os.path.join(DATA, "clips", code + ".mp3")
        if os.path.exists(clip):
            os.remove(clip)          # force audio.py to re-clip the new source
        species[k].pop("clip", None)
        species[k].pop("sound", None)

    with open(os.path.join(DATA, "species.json"), "w") as f:
        json.dump(species, f, separators=(",", ":"))
    with open(os.path.join(CACHE, "commons-search.json"), "w") as f:
        json.dump(search_cache, f)

    log(f"\nwritten. now rerun:  python3 build/audio.py  &&  python3 build/reanalyze.py")


if __name__ == "__main__":
    main()
