# Birdsong Map

Sweep a map of Iowa and hear what lives there. Every county plays its signature bird on
hover; open one and its birds fan out as a halo you can sweep through. Filter by what a
sound *is like* — whistle, trill, chatter, hoot, honk, buzz, screech — to work backwards
from a sound you remember to the bird that made it.

**99 counties × 12 months × 257 species**, 216 with self-hosted audio. All static JSON.
No API keys, no server, no tile provider.

```bash
python3 -m http.server 8731 --directory .
# open http://localhost:8731
```

Rebuild the occurrence + media dataset (~15 min, hits GBIF/Wikimedia):

```bash
python3 build/fetch_data.py --top 30
```

Clip, normalise and analyse the audio (~25 min, rate-limited on purpose):

```bash
python3 build/audio.py
```

Retune the classifier or spectrograms — reads the local clips, no network, ~9 seconds:

```bash
python3 build/reanalyze.py
```

Point it at another state — Minnesota is `USA.24_1` / FIPS `27`:

```bash
python3 build/fetch_data.py --state USA.24_1 --state-fips 27
```

## The sound-first part

Merlin already answers "what bird is singing *right now*" better than anything else. It
cannot answer "what was that thing I heard an hour ago" — the bird has to be singing into
your microphone. That gap is what this leans into.

**Audio is self-hosted, not streamed.** Each species ships a ~40 KB mono MP3: the densest
6 seconds of the source recording, loudness-normalised, with fades. Clips are decoded once
into Web Audio `AudioBuffer`s, so hover playback starts in **0 ms** on a warm buffer.
Streaming Commons directly cannot do this — files are 1–20 MB, often uncompressed WAV, and
field recordings open with several seconds of wind.

**Sound shapes are measured, not looked up.** `build/audio.py` computes three features
from the clip's PCM and buckets them:

| Feature | What it separates | How |
|---|---|---|
| spectral centroid | pitch — hoot vs whistle | magnitude-weighted mean frequency |
| spectral flatness | tonal vs noisy — whistle vs buzz | geometric ÷ arithmetic mean |
| pulse rate | trill vs steady note | autocorrelation of a 500 Hz amplitude envelope |

Four things that had to be right, and were wrong first:

- The pulse rate **cannot** come from the STFT envelope. At hop 512 / 22050 Hz that
  envelope samples at 43 Hz, whose Nyquist is below the 10–30 Hz trills being looked for,
  so every bird "trilled" at exactly 43.1 Hz. It needs its own envelope off the waveform.
- **Bandwidth is the wrong tonality measure.** A cardinal's whistle sweeps and carries
  harmonics, so it looks wide-band while obviously being a pure tone. Spectral flatness
  isn't fooled.
- A trill needs notes you can tell apart, so the note pitch must be far above the
  repetition rate. Without a `centroid / pulse > 40` guard, a Great Horned Owl hoot gets
  "detected" as a 14 Hz trill — its own carrier leaking through the envelope.
- **Measure only 200 Hz – 11 kHz.** Field recordings carry traffic and wind rumble below
  200 Hz with enormous energy. Including it pulled one Chipping Sparrow's centroid from
  8.2 kHz to 307 Hz and retagged an unmistakable trill as a "buzz"; it was also what made
  Eastern Wood-Pewee — a pure whistle — come out as "chatter". Restricting the band moved
  statewide "buzz" from 26 species to 5. Most of the buzz was road noise.

Spectrograms are stored as a 28 × 56 quantised digit string (~1.5 KB/species) and drawn
tinted by sound shape. Per-band noise-floor subtraction is what makes them readable;
without it, summer recordings are wall-to-wall cicada.

**"Sounds similar"** is nearest-neighbour in **z-scored** feature space (log centroid,
flatness, log spread, √pulse). Standardising matters: the first version used hand-picked
divisors, which stopped meaning anything once the analysis band changed and started
seating a cormorant next to a Chipping Sparrow.

It finds real confusion pairs — Mourning Dove → Rock Pigeon, Canada Goose → Mute and
Tundra Swan, Blue Jay → Swainson's Hawk and Northern Flicker (jays mimic hawk screams).
It is only ever as good as the one clip per species, though: where the clip is atypical or
noisy, the neighbours are too. This is a similarity heuristic, not an identification.

### Audio integrity

The first build shipped **twelve species playing the wrong bird**, because
`commons_audio_search` queried Commons *unquoted*: `Lanius ludovicianus filetype:audio`
scores loose terms and cheerfully returned an Australian Grey Butcherbird. Also shipped: a
Virginia Rail as Rusty Blackbird, a Bullock's Oriole as Varied Thrush, an 1898 magazine
read aloud by LibriVox as Bobolink, an Icelandic saga as Iceland Gull, and a 1911 ragtime
tune called "Redhead Rag" as Redhead.

`build/repair_audio.py` re-verifies every search-derived link against
`valid_audio_file()`, which applies two gates: reject a file whose name opens with someone
else's binomial (`Genus species -` is the Commons convention), and require positive
evidence — our genus/species in the name, a xeno-canto id, or ≥2 words of the common name.
One word is not enough; "Redhead" alone matches a song title.

Quoted search returns **empty** for most of those species — Commons simply has no
recording. That is the honest answer, so those birds now show no player. Coverage went
216 → 204. Silence beats confidently playing the wrong bird at someone trying to learn it.

Wikidata `P51` links are curated and mostly trustworthy, but they skew toward
*scholarly* uploads — the Chipping Sparrow entry was a PLOS ONE supplement of **food-begging
calls**, not the species' trill, while ten proper xeno-canto field recordings sat on
Commons unused. Recordings whose description says begging/juvenile/nestling are now
replaced with field recordings.

### Known limit

Tags describe **the clip we ship**, not the species' full repertoire — a bird with both a
song and a scold call is tagged for whichever one we chose. A xeno-canto key (free) would
let the build filter to `q:A` recordings and pick by `type:song`, which is the real fix.

## Why it's built this way

**Everything is precomputed at build time.** That is the single load-bearing decision.
"Which birds are typical here in August" is a *climatology*, not a live feed — there is
nothing to fetch at request time. Precomputing buys all of this at once:

- Sidesteps the eBird API's non-commercial licence entirely (we never call it)
- Stays inside GBIF and Wikimedia rate limits, permanently
- Makes the map instant and the whole app hostable on any static CDN for $0
- Works offline

## Data sources

| Layer | Source | Licence | Why this one |
|---|---|---|---|
| Species per county/month | [GBIF — eBird Observation Dataset](https://doi.org/10.15468/aomfnb) | CC BY 4.0 | The only free source that can answer "by month, by county". |
| Photos | [Avicommons](https://avicommons.org) | mixed CC, mostly BY-NC | Keyed by **eBird species code** — doubles as the taxonomy crosswalk. |
| Audio | Wikimedia Commons via Wikidata `P51` + Commons search | mixed CC | Needs no API key. See the xeno-canto note below. |
| Descriptions | Wikipedia REST | CC BY-SA 4.0 | Wikidata descriptions are 94% the literal string "species of bird". |
| County shapes | US Census cartographic boundaries | public domain | Joined to GBIF's GADM ids by county name. |

Current coverage: photo **251/257**, audio **216/257**, blurb **251/257**.

### Things that look like they'd work and don't

- **eBird API 2.0 cannot answer this question.** `/v2/product/spplist/{region}` is
  all-time with no query parameters; `back` caps at 30 days. There is no frequency,
  abundance, or barchart endpoint. It is also non-commercial-only.
- **eBird Status & Trends** is the *correct* dataset (52-week modelled abundance at 3km)
  and is explicitly forbidden in "websites, web-based platforms, mobile applications"
  without Cornell's written consent.
- **xeno-canto API v2 is dead** (HTTP 404). v3 needs a free per-app key. This is
  precisely what killed [Trogon](https://github.com/dandavison/trogon), the one indie
  project that had already built this idea — its map still works, its audio doesn't.
  We use Commons instead so the scaffold has no signup on the critical path.
  To upgrade audio quality, get a key at <https://xeno-canto.org/account> and swap the
  audio resolver in `build/fetch_data.py`.
- **GBIF without `license=`** gets stamped CC BY-NC, because GBIF applies the most
  restrictive constituent licence. The build passes it explicitly.

## The top-30 cap is a cap, not an exhaustive list

`--top 30` sets GBIF's `facetLimit`, and **1,165 of 1,188 Iowa county-months come back
holding exactly 30 species** — i.e. clipped, not exhausted. Iowa has ~440 species
statewide; this scaffold carries 257. Buchanan County in May alone has ~191.

That is a defensible product decision for a hover-driven map (nobody sweeps 191 birds),
but it must be a decision rather than a forgotten default, because **every capacity number
derived from `regions.json` inherits the cap.** Untruncated, the national dataset is
~23 MB rather than the ~4 MB a top-30 extrapolation predicts. Raise it with `--top`.

## Going national

The per-county-per-month faceting used here **does not scale** and shouldn't be used for
50 states: it's 37,700 calls, and GBIF's own guidance says anything over ~15 minutes of
search API should be a download request instead. I hit HTTP 429 at 8 concurrent on just
1,188 calls. The right shape is a single **SQL download** (`month`/`year` are reserved —
double-quote them; `HAVING` is unsupported), which needs access from helpdesk@gbif.org.
Iterate the query for free against the public `/occurrence/download/request/validate`
endpoint while waiting.

Two things that only bite at national scale:

- **Commons audio collapses from ~85% (Iowa's 257) to ~51% across the full ~1,200-species
  US list.** Adding iNaturalist's CC0/CC-BY sound observations brings the union to ~70% of
  species and ~98% of actual observations. Plan for a second audio source, and design for
  silence on seabirds and waterfowl that genuinely rarely vocalize.
- **County crosswalks are not clean.** GADM ships Great Lakes water bodies as counties
  (including its own typo, "Lake Hurron"), still lists units dissolved in 1992, and omits
  Baltimore city and St. Louis city. Connecticut cannot be crosswalked at all — 8 legacy
  counties vs 9 planning regions with different geometry. Diff the sets by name per state.

Geometry here is **US Census** (public domain) joined to GADM by county name; only the
`gadmGid` *codes* touch GADM. That distinction matters: GADM's license forbids
redistribution, so shipping its polygons on a public site would not be allowed.

## The honest caveat, stated in the UI

The ranking is **relative record count**, not detection probability. GBIF's eBird export
carries no checklist effort metadata (`samplingProtocol` and `sampleSizeValue` are null),
so there is no denominator. A bird ranked #1 is the most *reported*, which correlates
with abundance but also with how much birders like looking at it.

Real detection frequency needs the eBird Basic Dataset's Sampling Event Data
(free, ~7 day approval, and its non-commercial terms propagate to anything derived).

The EOD snapshot also ends **2024-12-31**. For a seasonal climatology that is fine.
Do not label anything in this app "recent" or "now".

## Licensing

**Code is MIT. The bundled data is not** — see [LICENSE](LICENSE) and
[ATTRIBUTION.md](ATTRIBUTION.md) (per-clip recordist, license and source for all 204).

Publishing the clips is a different act from streaming them. Each one is *Adapted
Material* — trimmed, normalised, transcoded — and 166 of 204 sources are ShareAlike, so
the adaptations carry both attribution and same-license obligations. Nothing here is
NonCommercial or NoDerivatives, because Commons accepts only free licenses; under ND,
redistributing an edited clip would be prohibited outright rather than merely conditional.
Photos are hot-linked, never redistributed.

Free, no ads, no IAP is a design constraint, not an accident. Roughly two-thirds of
Avicommons photos are CC BY-NC and 98.9% of xeno-canto is NonCommercial, so **monetising
means re-sourcing every photo and every recording.** Decide before building further.

Per-card attribution (photographer, recordist, license, Wikipedia link) renders on every
card. That is a license obligation, not decoration — don't remove it.

## Layout

```
index.html          shell
app.js              inline-SVG map, month picker, cards, audio
style.css           light/dark
build/fetch_data.py the whole pipeline
build/.cache/       per-step cache, safe to delete
data/               generated — counties.geojson, species.json, regions.json, meta.json
```

The map is hand-rolled inline SVG with an equirectangular projection fitted to the state
bbox — no Leaflet, no tiles, no attribution overhead, and it works offline. For
multi-state or arbitrary lat/lng clicks you'd want real tiles and point-in-polygon.

## Next, in order

1. **Illustrations.** The charm is the whole differentiator and no open illustrated set
   exists at scale — Cornell's BILLOW is subscription-locked and its terms ban mobile
   apps *and* ML training; the public-domain Audubon plates are 439 files that only
   34% name-match modern eBird taxonomy. Commission ~12 for the seeded region, A/B it,
   then decide. This scales linearly with species count and is the real cost.
2. **More states.** `--state` already works; the pipeline is state-agnostic.
3. **Self-host the audio.** Commons/xeno-canto files don't support HTTP Range requests,
   so `<audio>` downloads the whole file before it can seek. Transcode to ~64kbps mono
   MP3 at build time.
4. **Arbitrary lat/lng** instead of counties ("birds within 25 miles") — a real product
   improvement and a real architecture change, since it can't be precomputed on a fixed
   grid.

## Note

`~/.claude/launch.json` was created so the in-app preview can serve this directory.
Delete it if you don't want it; it has no effect on the app itself.
