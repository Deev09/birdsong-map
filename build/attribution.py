#!/usr/bin/env python3
"""
Generate ATTRIBUTION.md — the per-clip credits the licenses actually require.

Publishing the clips is a different act from streaming them. Each file in
data/clips/ is Adapted Material: trimmed to its densest few seconds, loudness
normalised, transcoded. 166 of the 204 sources are ShareAlike, so redistributing
the adaptations carries both attribution and same-license obligations.

Wikimedia Commons accepts only free licenses, so there is no NonCommercial or
NoDerivatives in the set — otherwise redistributing an edited clip would be
flatly prohibited rather than merely conditional.

Usage:  python3 build/attribution.py
"""

import json
import os
import urllib.parse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")


def main():
    species = json.load(open(os.path.join(DATA, "species.json")))
    rows = sorted(
        ((v["name"], v["sci"], v["clip"], v["audio"]) for v in species.values()
         if v.get("clip") and v.get("audio")),
        key=lambda r: r[0].lower(),
    )

    lic = {}
    for _, _, _, a in rows:
        k = a.get("license") or "see source"
        lic[k] = lic.get(k, 0) + 1

    out = [
        "# Attribution",
        "",
        "Every audio clip in `data/clips/` is a **modified** version of a freely licensed",
        "recording from Wikimedia Commons: trimmed to its densest few seconds, loudness",
        "normalised to -16 LUFS, faded, and transcoded to 56 kbps mono MP3 by",
        "[`build/audio.py`](build/audio.py).",
        "",
        "Because these are adaptations, ShareAlike applies to most of them. **The clips in",
        "`data/clips/` are redistributed under each recording's own license as listed below;",
        "adaptations of ShareAlike sources are offered under CC BY-SA 4.0.** The source code",
        "is MIT and is licensed separately — see [LICENSE](LICENSE).",
        "",
        "## Licenses in this set",
        "",
        "| License | Clips |",
        "|---|---|",
    ]
    for k, c in sorted(lic.items(), key=lambda x: -x[1]):
        out.append(f"| {k} | {c} |")

    out += [
        "",
        "No NonCommercial or NoDerivatives licenses appear here — Wikimedia Commons accepts",
        "only free licenses. That is what makes redistributing edited clips possible at all.",
        "",
        "## Other sources",
        "",
        "- **Occurrence data** — [GBIF eBird Observation Dataset](https://doi.org/10.15468/aomfnb),",
        "  CC BY 4.0. Cite: Levatich T., Ligocki S. (2024). *EOD – eBird Observation Dataset*.",
        "  Cornell Lab of Ornithology. https://doi.org/10.15468/aomfnb",
        "- **Photographs** — [Avicommons](https://avicommons.org), mixed CC (mostly CC BY-NC).",
        "  Hot-linked at display time, **not** redistributed in this repository. Photographer and",
        "  license are shown on every card.",
        "- **Descriptions** — English Wikipedia, CC BY-SA 4.0. Lead extracts are stored truncated",
        "  in `data/species.json`; each links back to its article.",
        "- **County geometry** — US Census Bureau cartographic boundary files, public domain",
        "  (17 U.S.C. §105). GADM contributes identifier *codes* only; no GADM geometry is",
        "  redistributed, as its license does not permit that.",
        "- **Ecoregion geometry** — US EPA, Office of Research and Development, Level III/IV",
        "  Ecoregions of the Continental United States. A US federal work; EPA's metadata",
        "  states \"Use Constraints: None\". Simplified for display and for spatial queries.",
        "",
        f"## Clips ({len(rows)})",
        "",
        "| Species | Clip | Recordist | License | Source |",
        "|---|---|---|---|---|",
    ]

    for name, sci, clip, a in rows:
        by = (a.get("by") or "").replace("|", "/").strip() or "see source page"
        page = a.get("page") or ""
        fname = urllib.parse.unquote(page.split("/")[-1]) if page else ""
        src = f"[{fname[:52]}]({page})" if page else "—"
        out.append(
            f"| {name} (*{sci}*) | `{os.path.basename(clip)}` | {by} | "
            f"{a.get('license') or 'see source'} | {src} |"
        )

    out.append("")
    path = os.path.join(ROOT, "ATTRIBUTION.md")
    with open(path, "w") as f:
        f.write("\n".join(out))
    print(f"wrote ATTRIBUTION.md — {len(rows)} clips, {len(lic)} distinct licenses")


if __name__ == "__main__":
    main()
