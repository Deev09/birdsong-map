#!/usr/bin/env python3
"""
Build the birdsong-map dataset.

Everything here runs at BUILD time and writes static JSON. Nothing in this file
runs at request time -- that is deliberate. It keeps us off eBird's
non-commercial API, keeps us inside GBIF/Wikimedia rate limits, and makes the
shipped app a pile of static files that costs $0 to host.

Pipeline:
  1. counties   GBIF GADM  -> the 99 Iowa counties + their polygons (US Census)
  2. occurrence GBIF EOD   -> top species per county per month, by record count
  3. taxonomy   GBIF + Avicommons -> speciesKey -> sci name -> eBird code
  4. media      Avicommons -> photo;  Wikidata/Commons -> audio;  Wikipedia -> blurb
  5. emit       data/*.json

Usage:  python3 build/fetch_data.py [--state USA.16_1] [--top 30]
"""

import argparse
import concurrent.futures as futures
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(ROOT, "build", ".cache")

# Wikimedia bumps you from 10 req/min to 200 req/min if you identify yourself.
UA = "birdsong-map/0.1 (https://github.com/example/birdsong-map; scaffold) python-urllib"

AVES_TAXON_KEY = 212  # GBIF class Aves
EOD_DATASET = "4fa7b334-ce0d-4e88-aaae-2e0c138d049e"  # eBird Observation Dataset

MONTHS = list(range(1, 13))


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------

def get(url, params=None, tries=4, expect_json=True):
    """GET with backoff. GBIF and Wikimedia both 429 without warning."""
    if params:
        # doseq so we can pass license=[a, b] and get license=a&license=b
        url = url + "?" + urllib.parse.urlencode(params, doseq=True)
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            return json.loads(body) if expect_json else body
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504):
                # No Retry-After header from iNat/Wikimedia, so back off blind.
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
        except Exception as e:  # noqa: BLE001 - transient DNS/TLS/timeouts
            last = e
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"GET failed after {tries}: {url} ({last})")


def cached(name, fn):
    """Disk-cache a build step so reruns are cheap while iterating."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    val = fn()
    with open(path, "w") as f:
        json.dump(val, f)
    return val


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# 1. counties
# --------------------------------------------------------------------------

def fetch_counties(state_gid):
    subs = get(f"https://api.gbif.org/v1/geocode/gadm/{state_gid}/subdivisions", {"limit": 500})
    return [{"gid": s["id"], "name": s["name"]} for s in subs]


def fetch_polygons(counties, state_fips):
    """US Census cartographic boundaries, joined to GADM by county name."""
    gj = get("https://raw.githubusercontent.com/plotly/datasets/master/geojson-counties-fips.json")
    by_name = {}
    for feat in gj["features"]:
        p = feat["properties"]
        if p.get("STATE") == state_fips:
            by_name[p["NAME"].lower()] = feat

    out, missed = [], []
    for c in counties:
        feat = by_name.get(c["name"].lower())
        if not feat:
            missed.append(c["name"])
            continue
        feat = dict(feat)
        feat["properties"] = {"gid": c["gid"], "name": c["name"], "fips": feat["properties"]["GEO_ID"][-5:]}
        out.append(feat)
    if missed:
        log(f"  ! no polygon for: {', '.join(missed)}")
    return {"type": "FeatureCollection", "features": out}


# --------------------------------------------------------------------------
# 2. occurrence
# --------------------------------------------------------------------------

def county_month_species(gid, month, top):
    """
    Top species by record count for one county in one month.

    license= is REQUIRED. Without it GBIF stamps the result set with the most
    restrictive constituent license (CC BY-NC) instead of EOD's own CC BY 4.0.
    """
    d = get("https://api.gbif.org/v1/occurrence/search", {
        "taxonKey": AVES_TAXON_KEY,
        "datasetKey": EOD_DATASET,
        "gadmGid": gid,
        "month": month,
        "limit": 0,
        "facet": "speciesKey",
        "facetLimit": top,
        "license": ["CC_BY_4_0", "CC0_1_0"],
    })

    # GBIF silently DROPS parameters it doesn't recognise rather than rejecting
    # them: one typo (`protectedArea=Yellowstone`) returns HTTP 200 and a count
    # of 3.9 billion -- the entire database -- and the build would happily
    # ingest it as a county. Verified live. There is no error to catch, so the
    # only defence is a plausibility check. The biggest real US county-month is
    # a few million; 50M is far above that and far below a dropped filter.
    count = d.get("count", 0)
    if count > 50_000_000:
        raise RuntimeError(
            f"implausible count {count:,} for {gid} m{month} — a filter was "
            f"probably dropped; check parameter spelling against GBIF's "
            f"OccurrenceSearchParameter enum"
        )

    facets = d.get("facets") or []
    if not facets:
        # An unrecognised facet name also yields HTTP 200 with facets: [].
        # Distinguish "no birds here" from "the facet name was wrong".
        if count > 0:
            raise RuntimeError(
                f"{count:,} records for {gid} m{month} but no facets returned — "
                f"the facet name was likely rejected silently"
            )
        return []
    return [(int(c["name"]), c["count"]) for c in facets[0]["counts"]]


def fetch_occurrence(counties, top, workers=8):
    jobs = [(c["gid"], m) for c in counties for m in MONTHS]
    results = {}
    done = 0
    with futures.ThreadPoolExecutor(max_workers=workers) as ex:
        fut = {ex.submit(county_month_species, gid, m, top): (gid, m) for gid, m in jobs}
        for f in futures.as_completed(fut):
            gid, m = fut[f]
            try:
                results[f"{gid}|{m}"] = f.result()
            except Exception as e:  # noqa: BLE001
                log(f"  ! {gid} m{m}: {e}")
                results[f"{gid}|{m}"] = []
            done += 1
            if done % 100 == 0:
                log(f"  {done}/{len(jobs)}")
    return results


# --------------------------------------------------------------------------
# 3. taxonomy
# --------------------------------------------------------------------------

def resolve_species(keys, workers=8):
    """GBIF speciesKey -> canonical scientific name + vernacular."""
    def one(k):
        d = get(f"https://api.gbif.org/v1/species/{k}")
        return k, {
            "sci": d.get("canonicalName") or d.get("scientificName"),
            "family": d.get("family"),
            "order": d.get("order"),
        }

    out = {}
    with futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for k, v in ex.map(one, keys):
            out[str(k)] = v
    return out


def load_avicommons():
    """
    Hand-cropped CC photos keyed by eBird species code -- which also makes this
    our free sciName -> eBird code crosswalk, skipping the Wikidata P3444 hop.
    """
    rows = get("https://avicommons.org/latest.json")
    by_sci = {}
    for r in rows:
        by_sci.setdefault(r["sciName"].lower(), r)
    return by_sci


# --------------------------------------------------------------------------
# 4. media
# --------------------------------------------------------------------------

SPARQL = "https://query.wikidata.org/sparql"


def wikidata_batch(sci_names, chunk=150):
    """sciName -> {enwiki title, Commons audio filename}. P225 is the join key."""
    out = {}
    names = list(sci_names)
    for i in range(0, len(names), chunk):
        part = names[i:i + chunk]
        values = " ".join('"%s"' % n.replace('"', "") for n in part)
        q = """
        SELECT ?sci ?audio ?enTitle WHERE {
          VALUES ?sci { %s }
          ?item wdt:P225 ?sci .
          OPTIONAL { ?item wdt:P51 ?audio . }
          OPTIONAL {
            ?a schema:about ?item ;
               schema:isPartOf <https://en.wikipedia.org/> ;
               schema:name ?enTitle .
          }
        }""" % values
        d = get(SPARQL, {"query": q, "format": "json"})
        for r in d["results"]["bindings"]:
            sci = r["sci"]["value"]
            e = out.setdefault(sci, {"title": None, "audio": None})
            if "enTitle" in r and not e["title"]:
                e["title"] = r["enTitle"]["value"]
            if "audio" in r and not e["audio"]:
                # P51 gives a Commons file URL; we want the bare filename.
                e["audio"] = urllib.parse.unquote(r["audio"]["value"].split("/")[-1])
        time.sleep(1.0)  # be polite to WDQS
    return out


# Audio files that are *about* a species rather than *of* it. Commons is full
# of both, and they sound like nothing a bird ever did.
NOT_A_BIRD = re.compile(
    r"(^LL-Q\d+"                        # Lingua Libre word pronunciations
    r"|^[A-Za-z]{2,4}-[A-Za-z]"         # Wiktionary/Tyap-style language prefixes
    r"|\(eng\)|pronunciation"
    r"|Birds[ _]and[ _]All[ _]Nature"   # 1898 magazine read aloud by LibriVox
    r"|librivox|audiobook|chapter|_various"
    r"|\bRag\b|\bMarch\b|orchestra|piano|banjo)",   # sheet music named after birds
    re.I,
)

# "Genus species - Common Name ..." -- the near-universal convention for bird
# recordings on Commons. The trailing dash is required: without it, "Snowy owl
# scream" and "Northern shoveler" get read as Latin binomials and thrown away.
BINOMIAL = re.compile(r"^([A-Z][a-z]{2,})[ _]([a-z]{3,})[ _]*-")


def valid_audio_file(fname, sci, common):
    """
    Is this file plausibly a recording OF this species?

    Even a quoted search returns files that merely *mention* the bird — a
    Bullock's Oriole recording cross-listed under Varied Thrush, a ragtime tune
    called "Redhead Rag". Two gates, both cheap:

      1. If the filename opens with someone else's binomial, it is someone
         else's bird. This is what catches the confidently-wrong cases.
      2. Require positive evidence: our genus/species in the name, a xeno-canto
         id, or at least two words of the common name. One word is not enough --
         "Redhead" alone matches a song title.

    Deliberately strict. Losing a real recording costs coverage; keeping a fake
    one means the app lies to someone trying to learn a bird.
    """
    if NOT_A_BIRD.search(fname):
        return False

    flat = re.sub(r"[^a-z]", " ", fname.lower())
    sci_words = [w for w in (sci or "").lower().split() if len(w) > 3]
    com_words = [w for w in re.findall(r"[a-z]+", (common or "").lower()) if len(w) > 2]

    m = BINOMIAL.match(fname.replace("_", " "))
    if m:
        found = f"{m.group(1).lower()} {m.group(2).lower()}"
        if sci and found != sci.lower() and m.group(2).lower() not in sci_words:
            return False

    if any(w in flat for w in sci_words):
        return True
    if re.search(r"xc\s*\d{3,}", flat):
        return True
    return sum(1 for w in com_words if w in flat) >= 2


def commons_audio_search(sci, common):
    """
    Find a real recording of this species on Commons.

    The query MUST be a quoted phrase. An unquoted `Lanius ludovicianus
    filetype:audio` is scored as loose terms and happily returns an Australian
    Grey Butcherbird -- which is how seven wrong species shipped in the first
    build. Quoting forces the species name to actually appear on the file page.

    When the quoted search comes back empty, that is the honest answer: Commons
    has no audio for this bird. Return None and show no player. Silence beats
    confidently playing the wrong bird.
    """
    for term in (sci, common):
        if not term:
            continue
        d = get("https://commons.wikimedia.org/w/api.php", {
            "action": "query", "format": "json", "list": "search",
            "srnamespace": 6, "srsearch": f'"{term}" filetype:audio', "srlimit": 10,
        })
        for r in d.get("query", {}).get("search", []):
            fname = r["title"].replace("File:", "")
            if valid_audio_file(fname, sci, common):
                return fname
    return None


def commons_fileinfo(filenames, chunk=40):
    """Filename -> playable URL + license + author. imageinfo batches 50/call."""
    out = {}
    names = [n for n in filenames if n]
    for i in range(0, len(names), chunk):
        part = names[i:i + chunk]
        titles = "|".join("File:" + n for n in part)
        d = get("https://commons.wikimedia.org/w/api.php", {
            "action": "query", "format": "json", "prop": "imageinfo",
            "iiprop": "url|extmetadata", "titles": titles,
        })
        pages = d.get("query", {}).get("pages", {})
        for p in pages.values():
            if "imageinfo" not in p:
                continue
            ii = p["imageinfo"][0]
            meta = ii.get("extmetadata", {})

            def m(k):
                v = meta.get(k, {}).get("value")
                return re.sub(r"<[^>]+>", "", v).strip() if v else None

            out[p["title"].replace("File:", "")] = {
                "url": ii["url"],
                "page": ii.get("descriptionurl"),
                "license": m("LicenseShortName") or "see Commons",
                "author": m("Artist"),
            }
        time.sleep(0.4)
    return out


def wikipedia_extracts(titles, chunk=20):
    """Lead-paragraph blurbs. CC BY-SA 4.0 -- attribution + link is mandatory."""
    out = {}
    titles = [t for t in titles if t]
    for i in range(0, len(titles), chunk):
        part = titles[i:i + chunk]
        d = get("https://en.wikipedia.org/w/api.php", {
            "action": "query", "format": "json", "prop": "extracts",
            "exintro": 1, "explaintext": 1, "redirects": 1,
            "titles": "|".join(part),
        })
        q = d.get("query", {})
        # Follow redirects so we can key results back to what we asked for.
        alias = {r["to"]: r["from"] for r in q.get("redirects", [])}
        for p in q.get("pages", {}).values():
            ex = (p.get("extract") or "").strip()
            if not ex:
                continue
            title = p["title"]
            for key in {title, alias.get(title, title)}:
                out[key] = ex
        time.sleep(0.3)
    return out


def trim(text, limit=230):
    """First sentence(s) up to ~limit chars, cut on a boundary, never mid-word."""
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "; ", ", "):
        idx = cut.rfind(sep)
        if idx > limit * 0.5:
            return cut[:idx + 1].strip()
    return cut.rsplit(" ", 1)[0] + "…"


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default="USA.16_1", help="GADM state gid (default Iowa)")
    ap.add_argument("--state-fips", default="19", help="US Census state FIPS (default Iowa)")
    ap.add_argument("--top", type=int, default=30, help="species per county-month")
    args = ap.parse_args()

    os.makedirs(DATA, exist_ok=True)

    log("1/5 counties")
    counties = cached("counties.json", lambda: fetch_counties(args.state))
    log(f"  {len(counties)} counties")

    polygons = cached("polygons.json", lambda: fetch_polygons(counties, args.state_fips))
    with open(os.path.join(DATA, "counties.geojson"), "w") as f:
        json.dump(polygons, f)
    log(f"  {len(polygons['features'])} polygons")

    log(f"2/5 occurrence ({len(counties) * 12} GBIF calls)")
    occ = cached(f"occ-top{args.top}.json", lambda: fetch_occurrence(counties, args.top))

    keys = sorted({k for v in occ.values() for k, _ in v})
    log(f"  {len(keys)} distinct species")

    log("3/5 taxonomy")
    tax = cached("taxonomy.json", lambda: resolve_species(keys))
    avi = cached("avicommons.json", load_avicommons)

    sci_names = sorted({t["sci"] for t in tax.values() if t.get("sci")})

    log("4/5 media")
    wd = cached("wikidata.json", lambda: wikidata_batch(sci_names))
    log(f"  wikidata: {sum(1 for v in wd.values() if v['audio'])}/{len(sci_names)} with P51 audio")

    # Fill the audio gap via Commons search, one call per missing species.
    def fill_audio():
        missing = []
        for sci in sci_names:
            if not wd.get(sci, {}).get("audio"):
                a = avi.get(sci.lower())
                missing.append((sci, a["name"] if a else None))
        log(f"  commons search for {len(missing)} species")
        found = {}
        with futures.ThreadPoolExecutor(max_workers=4) as ex:
            fut = {ex.submit(commons_audio_search, s, c): s for s, c in missing}
            for f in futures.as_completed(fut):
                try:
                    r = f.result()
                except Exception:  # noqa: BLE001
                    r = None
                if r:
                    found[fut[f]] = r
        return found

    extra = cached("commons-search.json", fill_audio)
    log(f"  commons search filled {len(extra)}")

    audio_files = {}
    for sci in sci_names:
        audio_files[sci] = wd.get(sci, {}).get("audio") or extra.get(sci)

    info = cached("commons-info.json", lambda: commons_fileinfo(set(audio_files.values())))

    titles = [wd.get(s, {}).get("title") for s in sci_names]
    extracts = cached("extracts.json", lambda: wikipedia_extracts(titles))

    log("5/5 emit")
    species = {}
    for key, t in tax.items():
        sci = t.get("sci")
        if not sci:
            continue
        a = avi.get(sci.lower())
        title = wd.get(sci, {}).get("title")
        af = audio_files.get(sci)
        ai = info.get(af) if af else None

        species[key] = {
            "sci": sci,
            "name": (a["name"] if a else None) or title or sci,
            "code": a["code"] if a else None,
            "family": t.get("family"),
            "photo": {
                "url": f"https://static.avicommons.org/{a['code']}-{a['key']}-480.jpg",
                "thumb": f"https://static.avicommons.org/{a['code']}-{a['key']}-240.jpg",
                "by": a["by"],
                "license": a["license"],
            } if a else None,
            "audio": {
                "url": ai["url"],
                "page": ai["page"],
                "by": ai.get("author"),
                "license": ai.get("license"),
            } if ai else None,
            "blurb": trim(extracts.get(title)) if title else None,
            "wiki": f"https://en.wikipedia.org/wiki/{urllib.parse.quote(title.replace(' ', '_'))}" if title else None,
        }

    regions = {}
    for c in counties:
        months = {}
        for m in MONTHS:
            rows = occ.get(f"{c['gid']}|{m}", [])
            months[str(m)] = [[str(k), n] for k, n in rows if str(k) in species]
        regions[c["gid"]] = {"name": c["name"], "months": months}

    with open(os.path.join(DATA, "species.json"), "w") as f:
        json.dump(species, f, separators=(",", ":"))
    with open(os.path.join(DATA, "regions.json"), "w") as f:
        json.dump(regions, f, separators=(",", ":"))

    have_photo = sum(1 for s in species.values() if s["photo"])
    have_audio = sum(1 for s in species.values() if s["audio"])
    have_blurb = sum(1 for s in species.values() if s["blurb"])
    n = len(species)
    log(f"\ndone: {n} species")
    log(f"  photo {have_photo}/{n} ({have_photo * 100 // n}%)")
    log(f"  audio {have_audio}/{n} ({have_audio * 100 // n}%)")
    log(f"  blurb {have_blurb}/{n} ({have_blurb * 100 // n}%)")

    with open(os.path.join(DATA, "meta.json"), "w") as f:
        json.dump({
            "state": args.state,
            "counties": len(counties),
            "species": n,
            "coverage": {"photo": have_photo, "audio": have_audio, "blurb": have_blurb},
            "sources": {
                "occurrence": "GBIF eBird Observation Dataset (CC BY 4.0), DOI 10.15468/aomfnb",
                "photos": "Avicommons (mixed CC, mostly CC BY-NC)",
                "audio": "Wikimedia Commons (mixed CC)",
                "text": "Wikipedia (CC BY-SA 4.0)",
            },
        }, f, indent=2)


if __name__ == "__main__":
    main()
