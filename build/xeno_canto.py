#!/usr/bin/env python3
"""
Upgrade audio using xeno-canto — the systematic fix for wrong vocalisations.

Commons has whatever someone happened to upload: often an alarm call, a
juvenile, or a noisy take, with no way to ask for the song. xeno-canto has ~900K
recordings WITH the metadata that matters — `type:song` and a quality grade —
so instead of auditioning whatever exists and hoping, we can ask for the right
vocalisation directly.

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


def search(sci, key, want_type="song", quality="A", limit=12):
    """Highest-quality song recordings first."""
    q = f'sp:"{sci}" q:{quality} type:{want_type} len:4-40'
    d = get(API, {"query": q, "key": key, "per_page": 100})
    recs = d.get("recordings") or []
    out = []
    for r in recs:
        if not usable(r):
            continue
        url = r.get("file")
        if not url:
            continue
        if url.startswith("//"):
            url = "https:" + url
        out.append({
            "id": r.get("id"), "url": url,
            "by": r.get("rec"), "lic": "https:" + r["lic"] if r["lic"].startswith("//") else r["lic"],
            "type": r.get("type"), "q": r.get("q"), "len": r.get("length"),
            "page": f"https://xeno-canto.org/{r.get('id')}",
        })
        if len(out) >= limit:
            break
    return out


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
        d = get(API, {"query": 'sp:"Turdus migratorius" q:A type:song', "key": key, "per_page": 5})
    except Exception as e:  # noqa: BLE001
        log(f"Key rejected or API unreachable: {e}")
        log("A 401 means the key is wrong; 403 can mean it is not activated yet.")
        return 1
    recs = d.get("recordings") or []
    log(f"key OK — {d.get('numRecordings', '?')} American Robin song recordings available")
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
    elif args.all:
        targets = sorted(by_sci)
    else:
        log("give --species NAME, or --all")
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
