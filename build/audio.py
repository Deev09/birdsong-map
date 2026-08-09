#!/usr/bin/env python3
"""
Turn the linked Commons recordings into something you can play on HOVER.

Two problems with streaming Commons directly:
  1. Files are 1-20 MB, often uncompressed WAV, and upload.wikimedia.org does not
     serve byte ranges usefully -- so <audio> pulls the whole file before it makes
     a sound. That is fine for click, fatal for hover.
  2. Field recordings start with 3 seconds of wind. On hover you need the bird
     immediately or the interaction feels broken.

So: download once at build time, find the densest few seconds of actual
vocalisation, loudness-normalise, and transcode to a small mono MP3 we host
ourselves. ~35 KB per species means hover playback starts effectively instantly.

While the PCM is in memory we also measure what the sound *is like* -- pitch,
bandwidth, and how fast it pulses -- which is what powers browse-by-sound.
That is the part no bird app does.

Usage:  python3 build/audio.py [--clip 6] [--limit N]
"""

import argparse
import concurrent.futures as futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
CLIPS = os.path.join(DATA, "clips")
UA = "birdsong-map/0.2 (https://github.com/example/birdsong-map) python-urllib"

SR = 22050          # plenty for birds; most song energy sits under 10 kHz
FRAME = 512         # ~23 ms hop
SPEC_T, SPEC_F = 56, 28   # thumbnail spectrogram resolution


def log(m):
    print(m, file=sys.stderr, flush=True)


def run(cmd):
    return subprocess.run(cmd, capture_output=True, check=False)


# ---------------------------------------------------------------- decode

def decode(path):
    """Whole file -> float32 mono at SR, via ffmpeg."""
    p = run(["ffmpeg", "-v", "error", "-i", path,
             "-f", "f32le", "-ac", "1", "-ar", str(SR), "-"])
    if p.returncode != 0 or not p.stdout:
        raise RuntimeError((p.stderr[:200] or b"decode failed").decode("utf8", "replace"))
    return np.frombuffer(p.stdout, dtype=np.float32)


def spectrogram(x, n_fft=1024, hop=FRAME):
    """Magnitude STFT. Hand-rolled so we don't need scipy/librosa."""
    if len(x) < n_fft:
        x = np.pad(x, (0, n_fft - len(x)))
    n = 1 + (len(x) - n_fft) // hop
    win = np.hanning(n_fft).astype(np.float32)
    frames = np.lib.stride_tricks.as_strided(
        x, shape=(n, n_fft), strides=(x.strides[0] * hop, x.strides[0])
    ) * win
    return np.abs(np.fft.rfft(frames, axis=1)).astype(np.float32)


# ---------------------------------------------------------------- clip choice

def best_window(x, seconds):
    """
    Pick the most vocally dense window.

    Loudest-window alone picks up wind gusts and handling noise, which are
    low-frequency. Weighting by energy in the 1.5-8 kHz band -- where birds
    actually are -- picks the bird instead.
    """
    want = int(seconds * SR)
    if len(x) <= want:
        return 0, len(x)

    S = spectrogram(x)
    freqs = np.fft.rfftfreq(1024, 1 / SR)
    band = (freqs >= 1500) & (freqs <= 8000)
    env = S[:, band].sum(axis=1)

    # Ignore the quiet half so a long silent tail can't dominate the average.
    env = np.maximum(env - np.median(env), 0)

    frames_per_window = max(1, want // FRAME)
    if len(env) <= frames_per_window:
        return 0, want

    csum = np.concatenate([[0.0], np.cumsum(env)])
    totals = csum[frames_per_window:] - csum[:-frames_per_window]
    start_frame = int(np.argmax(totals))
    start = min(start_frame * FRAME, len(x) - want)
    return max(0, start), want


# ---------------------------------------------------------------- features

ENV_SR = 500  # envelope sample rate; must be >> 2x the fastest trill we look for


def pulse_rate(x):
    """
    Repetitions per second, e.g. ~15 for a chipping-sparrow trill, ~0 for a held
    whistle.

    This CANNOT be measured from the STFT envelope: at hop 512 / 22050 Hz that
    envelope is sampled at 43 Hz, whose Nyquist is 21.5 Hz -- below the trills we
    care about. So build a proper amplitude envelope straight off the waveform at
    500 Hz instead, and autocorrelate that.
    """
    env = np.abs(x)
    # 15 ms smoothing. Deliberately heavy: a 200 Hz hoot otherwise ripples the
    # envelope at its own fundamental and gets "detected" as a 30 Hz trill.
    win = max(1, int(SR * 0.015))
    env = np.convolve(env, np.ones(win, np.float32) / win, mode="same")
    step = max(1, SR // ENV_SR)
    env = env[::step]
    if len(env) < 64:
        return 0.0, 0.0

    env = env - env.mean()
    ac = np.correlate(env, env, mode="full")[len(env) - 1:]
    ac = ac / (ac[0] + 1e-9)

    # Real bird trills top out near 30 Hz. Searching higher only finds artefacts.
    lo = int(ENV_SR / 30)                     # 30 Hz -> lag 16
    hi = min(len(ac) - 1, int(ENV_SR / 2))    # 2 Hz  -> lag 250
    if hi <= lo + 2:
        return 0.0, 0.0

    seg = ac[lo:hi]
    # A genuine local peak, not the tail of the zero-lag lobe.
    peaks = np.where((seg[1:-1] > seg[:-2]) & (seg[1:-1] >= seg[2:]))[0] + 1
    if len(peaks) == 0:
        return 0.0, 0.0
    k = int(peaks[np.argmax(seg[peaks])])
    strength = float(seg[k])
    if strength <= 0:            # anti-correlated: no rhythm, not a slow one
        return 0.0, 0.0
    return float(ENV_SR / (k + lo)), strength


def features(x):
    """
    Describe the sound the way a person would.

    centroid  -> perceived pitch                (hoot vs whistle)
    flatness  -> tonal vs noisy                 (whistle vs buzz/screech)
    pulse     -> repetitions per second         (trill vs steady note)

    Flatness rather than bandwidth for tonality: a cardinal's whistle sweeps and
    carries harmonics, so its *bandwidth* looks wide even though it is obviously
    a pure tone. Spectral flatness is near 0 for tones and near 1 for noise, and
    doesn't get fooled by that.
    """
    S = spectrogram(x)
    freqs = np.fft.rfftfreq(1024, 1 / SR)

    power = S.sum(axis=1)
    if power.max() <= 0:
        return None
    loud = power > (power.max() * 0.15)   # measure the bird, not the gaps
    if loud.sum() < 3:
        loud = power > 0

    # Measure only the band birds actually occupy. Field recordings carry
    # traffic and wind rumble below ~200 Hz with enormous energy: including it
    # dragged one Chipping Sparrow's centroid from 8.2 kHz down to 307 Hz and
    # retagged an unmistakable trill as a "buzz". The floor sits just under the
    # lowest real voices here (Great Horned Owl, bittern).
    voice = (freqs >= 200) & (freqs <= 11000)
    fv = freqs[voice]

    Sl = S[loud][:, voice]
    mag = Sl.sum(axis=1, keepdims=True)
    mag[mag == 0] = 1e-9
    prob = Sl / mag

    centroid = float((prob * fv).sum(axis=1).mean())
    spread = float(np.sqrt((prob * (fv - centroid) ** 2).sum(axis=1)).mean())

    B = Sl + 1e-9
    flatness = float(np.mean(np.exp(np.log(B).mean(axis=1)) / B.mean(axis=1)))

    pulse, strength = pulse_rate(x)

    # Thumbnail spectrogram, log-frequency so low notes aren't squashed into
    # one row. Quantised 0-9 and stored as a digit string -- ~1.5 KB/species.
    edges = np.geomspace(200, 10000, SPEC_F + 1)
    idx = np.clip(np.searchsorted(edges, freqs) - 1, 0, SPEC_F - 1)
    fb = np.zeros((S.shape[0], SPEC_F), dtype=np.float32)
    for i in range(SPEC_F):
        m = idx == i
        if m.any():
            fb[:, i] = S[:, m].mean(axis=1)

    # Subtract each band's own noise floor. Field recordings carry broadband
    # hiss, wind and traffic that otherwise fill the thumbnail and bury the
    # shape -- and the shape is the entire point of showing it.
    # 40th percentile, not 25th: summer recordings are wall-to-wall cicada, so a
    # low floor leaves the whole frame lit and the shape unreadable.
    fb = np.maximum(fb - np.percentile(fb, 40, axis=0, keepdims=True), 0)

    t = np.linspace(0, fb.shape[0] - 1, SPEC_T).astype(int)
    thumb = fb[t]
    thumb = np.log1p(thumb * 60)
    hi = np.percentile(thumb, 99)
    if hi > 0:
        thumb = np.clip(thumb / hi, 0, 1)
    thumb = thumb ** 1.7          # push mid-greys down, keep the peaks hot
    q = np.clip((thumb * 9).round(), 0, 9).astype(int)
    spec = "".join("".join(str(v) for v in row) for row in q.T)  # freq-major

    return {
        "centroid": round(centroid),
        "spread": round(spread),
        "flatness": round(flatness, 3),
        "pulse": round(pulse, 1),
        "pulseStrength": round(strength, 2),
        "spec": spec,
    }


def classify(f):
    """
    Heuristic sound-shape buckets from the measured features.

    This is signal processing, not species knowledge -- it describes the clip we
    actually shipped, which is exactly what someone hovering the map hears. A
    species with several very different calls gets tagged for the one we chose.
    """
    c, fl, p, ps = f["centroid"], f["flatness"], f["pulse"], f["pulseStrength"]
    tags = []

    # Rhythm is independent of timbre -- a trill can be musical or buzzy.
    #
    # But a trill needs notes you can tell apart, which means the note pitch has
    # to be far above the repetition rate. A 197 Hz owl hoot "pulsing" at 14 Hz
    # is the carrier leaking through the envelope, not a trill. Require a wide
    # margin before believing any rhythm tag.
    rhythmic = ps > 0.30 and p > 0 and (c / p) > 40
    if rhythmic and p >= 9:
        tags.append("trill")
    elif rhythmic and 2.5 <= p < 9:
        tags.append("chatter")

    tonal = fl < 0.10
    noisy = fl > 0.30

    if tonal and c < 1100:
        tags.append("hoot")           # owl, dove, cuckoo
    elif tonal and c < 1900:
        tags.append("honk")           # goose, crane -- tonal but low and blaring
    elif tonal:
        tags.append("whistle")        # cardinal, titmouse, pewee
    elif noisy and c > 2600:
        tags.append("screech")        # jay, hawk, tern
    elif noisy:
        tags.append("buzz")           # grackle, blackbird
    elif c < 1400:
        tags.append("honk")           # goose, crane, heron

    if not tags:
        tags.append("whistle" if fl < 0.18 else "chatter")
    return tags


# ---------------------------------------------------------------- per species

_throttle = threading.Semaphore(2)   # be a good citizen on upload.wikimedia.org
_last = [0.0]
_lock = threading.Lock()


def fetch(url, dest, tries=6):
    """
    Download politely.

    Wikimedia will 429 you with "Your bot is making too many requests" long
    before you notice -- 5 concurrent downloads was enough. Serialise to a
    steady ~1.5 req/s and back off exponentially when told to.
    """
    for attempt in range(tries):
        with _throttle:
            with _lock:
                gap = time.time() - _last[0]
                if gap < 0.7:
                    time.sleep(0.7 - gap)
                _last[0] = time.time()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
                    shutil.copyfileobj(r, f)
                return
            except urllib.error.HTTPError as e:
                if e.code not in (429, 500, 502, 503):
                    raise
                last = e
            except Exception as e:  # noqa: BLE001
                last = e
        time.sleep(min(60, 3 * (2 ** attempt)))
    raise RuntimeError(f"download failed: {last}")


def process(code, url, clip_secs, tmpdir):
    src = os.path.join(tmpdir, code + os.path.splitext(url.split("?")[0])[1][:6])
    fetch(url, src)

    x = decode(src)
    if len(x) < SR // 2:
        raise RuntimeError("too short")

    start, want = best_window(x, clip_secs)
    clip = x[start:start + want]

    out = os.path.join(CLIPS, code + ".mp3")
    p = run([
        "ffmpeg", "-v", "error", "-y",
        "-ss", f"{start / SR:.2f}", "-t", f"{want / SR:.2f}", "-i", src,
        # loudnorm so hovering across species doesn't blast you; short fades
        # kill the click at the splice point.
        "-af", f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=in:d=0.05,"
               f"afade=t=out:st={want / SR - 0.12:.2f}:d=0.12",
        "-ac", "1", "-ar", "32000", "-b:a", "56k", "-map_metadata", "-1", out,
    ])
    if p.returncode != 0:
        raise RuntimeError((p.stderr[:200]).decode("utf8", "replace"))

    f = features(clip)
    if not f:
        raise RuntimeError("silent")
    f["tags"] = classify(f)
    f["bytes"] = os.path.getsize(out)
    try:
        os.remove(src)
    except OSError:
        pass
    return f


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", type=float, default=6.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--force", action="store_true", help="redo species already done")
    args = ap.parse_args()

    os.makedirs(CLIPS, exist_ok=True)
    species = json.load(open(os.path.join(DATA, "species.json")))

    todo = [(k, s) for k, s in species.items() if s.get("audio")]
    if not args.force:
        # Resume: a species already clipped and analysed is done. Rate limits
        # make a from-scratch rerun expensive, so never redo work.
        todo = [(k, s) for k, s in todo
                if not (s.get("sound") and
                        os.path.exists(os.path.join(CLIPS, (s["code"] or k) + ".mp3")))]
    if args.limit:
        todo = todo[:args.limit]
    log(f"{len(todo)} species to process")
    if not todo:
        return

    out, fails = {}, []
    with tempfile.TemporaryDirectory() as tmp:
        with futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
            fut = {ex.submit(process, s["code"] or k, s["audio"]["url"], args.clip, tmp): (k, s)
                   for k, s in todo}
            done = 0
            for fu in futures.as_completed(fut):
                k, s = fut[fu]
                try:
                    out[k] = fu.result()
                except Exception as e:  # noqa: BLE001
                    fails.append((s["name"], str(e)[:90]))
                done += 1
                if done % 25 == 0:
                    log(f"  {done}/{len(todo)}")

    for k, f in out.items():
        species[k]["clip"] = "data/clips/" + (species[k]["code"] or k) + ".mp3"
        species[k]["sound"] = {
            "tags": f["tags"], "pulse": f["pulse"],
            "centroid": f["centroid"], "spread": f["spread"],
        }

    with open(os.path.join(DATA, "species.json"), "w") as f:
        json.dump(species, f, separators=(",", ":"))

    # Merge, don't clobber -- this script is resumable and runs in several passes.
    specs_path = os.path.join(DATA, "spectrograms.json")
    specs = json.load(open(specs_path)) if os.path.exists(specs_path) else {}
    specs.update({k: v["spec"] for k, v in out.items()})
    with open(specs_path, "w") as f:
        json.dump(specs, f, separators=(",", ":"))

    total = sum(v["bytes"] for v in out.values())
    log(f"\nclipped {len(out)}/{len(todo)} this pass  ({total / 1e6:.1f} MB new, "
        f"avg {total / max(1, len(out)) / 1024:.0f} KB)")

    counts = {}
    for s in species.values():
        for t in (s.get("sound") or {}).get("tags", []):
            counts[t] = counts.get(t, 0) + 1
    log("sound shapes: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))
    if fails:
        log(f"\n{len(fails)} failed:")
        for n, e in fails[:12]:
            log(f"  {n}: {e}")


if __name__ == "__main__":
    main()
