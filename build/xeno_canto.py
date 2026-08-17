#!/usr/bin/env python3
"""
Upgrade audio using xeno-canto — the systematic fix for wrong vocalisations.

Commons has whatever someone happened to upload: often an alarm call, a
juvenile, or a noisy take, with no way to ask for the song. xeno-canto has ~1M
recordings WITH the metadata that matters, so the right vocalisation can be
requested instead of hoped for.

WHAT THE METADATA DOES AND DOES NOT PROMISE:
  id?:no      the only ID-trust filter. Excludes questioned, unconfirmed and
              mystery recordings. NOT applied by default — measured, plain
              search returns 978 American Robin recordings and id?:no returns
              974, so disputed IDs are served unless you ask otherwise.
  q:A         audio clarity ONLY. 513 grade-A recordings currently carry a
              disputed ID. A loud, clean recording of a misidentified bird is a
              legitimate grade A.
  seen:yes    the recordist saw the bird. The best non-acoustic corroboration
              available — but self-reported by whoever assigned the label.

No corpus-level label-error rate has ever been published for xeno-canto,
Macaulay, or any bird sound archive, so "identified" means "nobody has
successfully challenged this", not "verified". That is why every clip still
goes through build/verify_audio.py afterwards.

This became usable only once the project committed to free-forever: 98.9% of
xeno-canto is NonCommercial.

    GETTING A KEY (2 minutes, free — a human has to do this):
      1. create an account at https://xeno-canto.org/account
      2. verify the email
      3. the API key is shown on that same account page
      4. export XC_API_KEY=...        (or write it to build/.xc_key, gitignored)

    python3 build/xeno_canto.py --check          # verify the key works
    python3 build/xeno_canto.py --species "Turdus migratorius"
    python3 build/xeno_canto.py --all --apply    # upgrade everything upgradable

Then ALWAYS re-run build/verify_audio.py. Better metadata narrows the risk; it
does not remove it.

LICENSING — the one hard rule:
NoDerivatives recordings are skipped outright. We trim to the densest window and
loudness-normalise, which makes an adaptation, and ND forbids distributing one.
About a fifth of xeno-canto is BY-NC-ND, so this is not a corner case. Every
other CC licence is fine for a free app as long as the recordist is credited,
which ATTRIBUTION.md does.
"""

import argparse
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio import CLIPS, DATA, best_window, classify, decode, features, fetch, log  # noqa: E402
from fetch_data import get  # noqa: E402

API = "https://xeno-canto.org/api/3/recordings"
KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".xc_key")


def api_key():
    key = os.environ.get("XC_API_KEY", "").strip()
    if not key and os.path.exists(KEY_FILE):
        key = open(KEY_FILE).read().strip()
    return key or None


def usable(rec):
    """
    Can we legally ship a trimmed, normalised copy of this recording?

    Everything here is an adaptation, so ND is a hard stop. The licence arrives
    as a protocol-relative URL like //creativecommons.org/licenses/by-nc-sa/4.0/
    """
    lic = (rec.get("lic") or "").lower()
    if not lic:
        return False          # unknown licence: don't risk it
    # ND is the only disqualifier. NonCommercial and ShareAlike are both fine
    # for a free app, and public-domain/CC0 obviously so.
    return "-nd" not in lic


def tiers(sci):
    """
    Query tiers, strictest first.

    id?:no is the load-bearing filter and the one that is easy to miss. The
    xeno-canto docs and the founders' paper both say a challenged recording is
    hidden from search until resolved; measured Aug 2026 that is not true —
    sp:"turdus migratorius" returns 978 and adding id?:no returns 974. Disputed
    IDs are served by default. It must be passed explicitly.

    q:A is NOT an identification signal. The scale is defined as "A (highest
    quality) to E (lowest quality)" purely on audio clarity, and 513 grade-A
    bird recordings currently carry a disputed ID. It buys a clean clip, never
    a correct label — so it is relaxed before id?:no ever is.

    lic: is a positive filter because there is no negation operator: ND cannot
    be excluded, only other licences included. 25.1% of xeno-canto is BY-NC-ND,
    which forbids the trimmed, normalised clips this project ships.
    """
    base = f'sp:"{sci}" grp:birds id?:no type:song lic:BY-NC-SA len:"4-40"'
    return [
        # seen:yes = recordist actually saw the bird. Best non-acoustic
        # corroboration available, and no audio model trained on XC can launder
        # it. Costs the 20.1% of recordings where the field is blank.
        ("strict", base + " q:A seen:yes playback:no"),
        ("relaxed", base + ' q:">C" seen:yes playback:no'),   # >C means A and B only
        ("loose", base + ' q:">C"'),
    ]


def search(sci, key, limit=12):
    """Walk the tiers until one yields usable recordings."""
    for tier, q in tiers(sci):
        try:
            d = get(API, {"query": q, "key": key, "per_page": 100})
        except Exception:  # noqa: BLE001
            continue
        out = []
        for r in d.get("recordings") or []:
            # Belt and braces: id?:no should already have excluded these, but
            # the field ships in the payload so there is no reason not to check.
            if r.get("status") and r["status"] != "identified":
                continue
            if not usable(r):
                continue
            url = r.get("file")
            if not url:
                continue
            if url.startswith("//"):
                url = "https:" + url
            lic = r.get("lic") or ""
            out.append({
                "id": r.get("id"), "url": url, "by": r.get("rec"),
                "lic": ("https:" + lic) if lic.startswith("//") else lic,
                "type": r.get("type"), "q": r.get("q"), "len": r.get("length"),
                "seen": r.get("bird-seen") or r.get("animal-seen"),
                # 'also' lists background species. Under-reported by xeno-canto's
                # own admission, so an empty list is weak evidence of a clean
                # recording — used only to sort, never to filter.
                "also": [a for a in (r.get("also") or []) if a],
                "tier": tier,
                "page": f"https://xeno-canto.org/{r.get('id')}",
            })
        if out:
            out.sort(key=lambda r: len(r["also"]))   # cleanest first
            return out[:limit]
    return []


def audition(url, tmp, clip_secs=6.0):
    """Measure the window that would actually ship (see pick_clip.py)."""
    path = os.path.join(tmp, "xc" + os.path.splitext(url.split("?")[0])[1][:6])
    fetch(url, path)
    x = decode(path)
    try:
        os.remove(path)
    except OSError:
        pass
    if len(x) < 22050 // 2:
        return None
    start, want = best_window(x, clip_secs)
    f = features(x[start:start + want])
    if not f:
        return None
    f["tags"] = classify(f)
    return f


def check(key):
    if not key:
        log("No key. Set XC_API_KEY or write build/.xc_key — see the docstring.")
        return 1
    try:
        _, strict = tiers("Turdus migratorius")[0]
        d = get(API, {"query": strict, "key": key, "per_page": 5})
    except Exception as e:  # noqa: BLE001
        log(f"Key rejected or API unreachable: {e}")
        log("A 401 means the key is wrong; 403 can mean it is not activated yet.")
        return 1
    recs = d.get("recordings") or []
    log(f"key OK — {d.get('numRecordings', '?')} American Robin recordings pass the strict query")
    for r in recs[:3]:
        log(f"   XC{r.get('id')}  q={r.get('q')}  {r.get('length')}  {r.get('lic')}")
    n_nd = sum(1 for r in recs if not usable(r))
    log(f"   ({n_nd}/{len(recs)} of this sample are NoDerivatives and would be skipped)")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify the key and exit")
    ap.add_argument("--species", help="scientific name, e.g. \"Turdus migratorius\"")
    ap.add_argument("--all", action="store_true", help="every species with audio")
    ap.add_argument("--unverified", action="store_true",
                    help="only species BirdNET could not confirm (needs verification.json)")
    ap.add_argument("--missing", action="store_true",
                    help="only species with no audio source at all")
    ap.add_argument("--apply", action="store_true", help="write changes")
    ap.add_argument("--clip", type=float, default=6.0)
    args = ap.parse_args()

    key = api_key()
    if args.check:
        return check(key)
    if not key:
        log("No XC_API_KEY. Run --check for setup instructions.")
        return 1

    import tempfile
    species = json.load(open(os.path.join(DATA, "species.json")))
    by_sci = {v["sci"]: k for k, v in species.items()}

    targets = []
    if args.species:
        if args.species not in by_sci:
            log(f"{args.species} is not in the dataset")
            return 1
        targets = [args.species]
    elif args.unverified:
        # Target the clips an independent classifier could not confirm. These
        # are where better provenance actually buys something; the rest are
        # already corroborated, and swapping them risks regression for nothing.
        vpath = os.path.join(DATA, "verification.json")
        if not os.path.exists(vpath):
            log("run build/verify_audio.py first")
            return 1
        # NOT "CONFUSABLE". That status means BirdNET named a known
        # confusion partner — Spotted Owl for Barred Owl, sister species that
        # hybridise — which verify_audio.py deliberately treats as weak
        # evidence, not error. Including it here replaced a carefully chosen
        # Barred Owl hoot (765 Hz, flatness 0.001) with a "call, song"
        # recording that no longer reads as a hoot at all, undoing an earlier
        # fix. Only genuinely unconfirmed clips belong in this queue.
        bad = {v["name"] for v in json.load(open(vpath))
               if v["status"] in ("ABSENT", "NO-BIRD")}
        targets = sorted(v["sci"] for v in species.values() if v["name"] in bad)
        log(f"{len(targets)} species BirdNET could not confirm")
    elif args.missing:
        # Pure gain: these species currently have nothing, so anything usable
        # xeno-canto returns is strictly better than silence.
        targets = sorted(v["sci"] for v in species.values() if not v.get("audio"))
        log(f"{len(targets)} species with no audio source")
    elif args.all:
        targets = sorted(by_sci)
    else:
        log("give --species NAME, --unverified, or --all")
        return 1

    picks = {}
    with tempfile.TemporaryDirectory() as tmp:
        for sci in targets:
            k = by_sci[sci]
            sp = species[k]
            try:
                recs = search(sci, key)
            except Exception as e:  # noqa: BLE001
                log(f"  !  {sp['name']:<24}{e}")
                continue
            if not recs:
                log(f"  --   {sp['name']:<24}no usable q:A song recording")
                continue

            best = None
            for r in recs[:6]:
                try:
                    f = audition(r["url"], tmp, args.clip)
                except Exception:  # noqa: BLE001
                    continue
                if f and (best is None or f["flatness"] < best[0]["flatness"]):
                    best = (f, r)
            if not best:
                log(f"  --   {sp['name']:<24}candidates failed to decode")
                continue

            f, r = best
            picks[k] = r
            log(f"  OK   {sp['name']:<24}XC{r['id']} q={r['q']} {r['type']:<12}"
                f"-> {','.join(f['tags'])} (flat={f['flatness']})")

    if not args.apply:
        log(f"\ndry run — {len(picks)} would change. rerun with --apply")
        return 0

    for k, r in picks.items():
        species[k]["audio"] = {"url": r["url"], "page": r["page"],
                               "by": r["by"], "license": r["lic"], "source": "xeno-canto"}
        code = species[k].get("code") or k
        clip = os.path.join(CLIPS, code + ".mp3")
        if os.path.exists(clip):
            os.remove(clip)
        species[k].pop("clip", None)
        species[k].pop("sound", None)

    with open(os.path.join(DATA, "species.json"), "w") as fh:
        json.dump(species, fh, separators=(",", ":"))
    log(f"\nrewired {len(picks)}. now: python3 build/audio.py && python3 build/reanalyze.py"
        f" && python3 build/attribution.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
