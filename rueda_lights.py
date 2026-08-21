#!/usr/bin/env python3
r"""
rueda_lights.py — Music-reactive light engine for La Rueda.

RIG (Daslight 5, Universe 1, Iluminarc Ilumipanel ML, 8ch "Arc Full" each)
    addr  1  Luz 1 - Rueda        \  WHEEL zone
    addr 10  Luz 2 - rueda izq    /
    addr 30  Luz 3 - bosque       \  FOREST zone
    addr 20  Luz 4 - Bosque       /
    channels per fixture: 1 Dimmer, 2 Red, 3 Green, 4 Blue,
                          5 Preset Colors, 6 Colour Temp, 7 Strobe, 8 Dimmer Speed

MUSICAL DESIGN
    The wheel is the pulse: both wheel lights ride the BASS, but the second one
    lags half a beat behind the first, so light appears to roll across the
    wheel instead of both blinking together.
    The forest is the detail: one light takes the MIDS (vocals, melody), the
    other the HIGHS (hats, shimmer).

COLOUR DESIGN (two-level harmony)
    Level 1 - the two ZONES sit at contrasting points on the colour circle, so
              the wheel and the forest never wash into the same colour.
    Level 2 - inside each zone the pair splits slightly around its zone hue,
              giving each object a two-tone, dimensional look rather than a
              flat wash.
    A final pass guarantees all four hues stay apart on the circle.

USAGE
    python rueda_lights.py --map-list                # print the OSC addresses to map
    python rueda_lights.py --learn /wheel_a/hue      # send one address for Map OSC
    python rueda_lights.py /path/to/music            # run the show
    python rueda_lights.py /path/to/music --simulate # test with no hardware
    python rueda_lights.py /path/to/music --mode rgb # if you mapped R/G/B instead

    While running:  [n] next track   [q] quit

DEPS  pip install librosa python-osc sounddevice numpy
"""

import argparse
import colorsys
import hashlib
import os
import pickle
import queue
import random
import socket
import struct
import sys
import threading
import time

import numpy as np

# ---------------------------------------------------------------------------
# RIG CONFIG
# ---------------------------------------------------------------------------
DASLIGHT_IP = "127.0.0.1"
DASLIGHT_PORT = 7000   # Daslight 5 OSC input; verify with: python3 osc_probe.py --ports
FPS = 40

# Art-Net: raw DMX straight at the patched addresses. Needs no Daslight mapping.
ARTNET_IP = "127.0.0.1"
ARTNET_PORT = 6454
ARTNET_UNIVERSE = 0        # Daslight "Universe 1" is usually Art-Net universe 0

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".cache")
# Bump whenever SongAnalysis gains/loses a field, or a cached value changes
# meaning. Without this an updated script silently loads an old pickle.
CACHE_VERSION = 5

AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac")

BANDS = {
    "bass": (20, 250),
    "mids": (250, 4000),
    "highs": (4000, 16000),
}

# zone      = which object it lights
# band      = which part of the music drives it
# lag_beats = delay its reaction (creates motion across a zone)
# osc       = OSC address prefix
# events    = which musical events make this light BLOOM, and how hard
LIGHTS = [
    {"name": "wheel_a",  "zone": "wheel",  "band": "bass",  "lag_beats": 0.0, "osc": "/wheel_a",  "addr": 1,
     "events": {"kick": 0.60}},
    {"name": "wheel_b",  "zone": "wheel",  "band": "bass",  "lag_beats": 0.5, "osc": "/wheel_b",  "addr": 10,
     "events": {"kick": 0.60}},
    {"name": "forest_a", "zone": "forest", "band": "mids",  "lag_beats": 0.0, "osc": "/forest_a", "addr": 30,
     "events": {"hit": 0.45, "ding": 0.40}},
    {"name": "forest_b", "zone": "forest", "band": "highs", "lag_beats": 0.25, "osc": "/forest_b", "addr": 20,
     "gain": 1.2,    # hats are sparse; lift so this light is not the dim one of the pair
     "events": {"tick": 0.15, "ding": 1.00}},   # bells/dings SHINE here
]
ZONES = ["wheel", "forest"]

# Offset of each control from a fixture's start address, per the Arc Full
# personality: 1 Dimmer, 2 Red, 3 Green, 4 Blue, 5 Preset, 6 Temp, 7 Strobe, 8 Speed
DMX_OFFSET = {"dimmer": 0, "red": 1, "green": 2, "blue": 3, "strobe": 6}

# ---------------------------------------------------------------------------
# THE SPACE — what each zone's light actually lands on
# ---------------------------------------------------------------------------
# WHEEL   A large rust-red wooden water wheel beside a white cascading weir,
#         grey river-stone walls behind it. Warm light makes the wood glow;
#         blue or green on red-brown wood reads as mud. So the wheel lives on
#         a WARM arc:  gold -> amber -> red -> crimson -> magenta.
# FOREST  Palms and tall wispy trees uplit from a lawn with stone benches,
#         river mist hanging in the air (beams will be visible). Foliage
#         flatters green, teal, cyan, blue and gold; it kills red/magenta.
#         So the forest lives on a COOL arc: chartreuse -> green -> teal -> blue.
#
# The arcs never overlap, so the zones can never wash into each other, and
# they are complementary — the contrast a red wheel in a green garden wants.
# The MUSIC decides how far each zone travels along its arc:
#     quiet  -> both near the gold/chartreuse meeting point (intimate garden)
#     loud   -> crimson wheel against a blue forest (maximum drama)
# Each arc is (start_hue, signed_span): hue = start + t * span,  t in 0..1.
ZONE_ARC = {
    "wheel":  (0.13, -0.18),   # 0.13 gold -> 0.00 red -> 0.95 crimson/magenta
    "forest": (0.29, +0.30),   # 0.29 chartreuse -> 0.50 cyan -> 0.59 azure-blue
}
CONTRAST_LOUDNESS = (0.30, 0.80)   # section loudness that maps to t = 0 .. 1

# Per-zone dynamics. The wheel is the kinetic centrepiece — it pulses. The
# forest is atmosphere — it breathes. People sit in the forest near a river
# edge at night, so it keeps a higher floor: the garden dims, it never dies.
# attack/release are (slow, fast) pairs; the music's DENSITY picks where
# between them each moment sits — sparse passages glow, busy passages snap.
ZONE_FEEL = {
    "wheel":  dict(attack=(0.14, 0.45), release=(0.04, 0.14), floor=0.10, sat=(0.55, 1.00), strobe=True),
    "forest": dict(attack=(0.08, 0.30), release=(0.03, 0.09), floor=0.16, sat=(0.50, 0.90), strobe=False),
}

# ---------------------------------------------------------------------------
# EVENTS — the instrument layer
# ---------------------------------------------------------------------------
# Discrete musical events are detected per band, each with a strength 0..1.
# A light BLOOMS on the events it listens to: a quick rise, then an
# exponential fade with that event's own half-life. Kicks thump, hats tick,
# and a bell DING is a light that shines and slowly fades — not a flash.
# This replaces driving the lights off raw spectral flux every frame, which
# is what made them flicker.
EVENT_BANDS = {
    "kick": (20, 250),      # bass drum / bass attacks       -> wheel
    "hit":  (250, 4000),    # snare, chord stabs, vocal hits -> forest_a
    "tick": (4000, 16000),  # hats, shimmer                  -> forest_b
    "bell": (1200, 7000),   # bells, chimes, plucks live here; classified into DING
}
BLOOM_HALF_LIFE = {"kick": 0.25, "hit": 0.45, "tick": 0.10, "ding": 1.60}   # seconds
DING_SHINE = 0.45          # how far a ding pulls the light toward white at the strike
# A bell-band onset is a DING when it is tonal (low spectral flatness), rings
# (energy holds after the hit) and is strong — judged against that track's
# own bell-band onsets, so it adapts per song.
DING_SUSTAIN_Q, DING_FLAT_Q, DING_STRENGTH_Q = 0.60, 0.50, 0.50
# density = how hard the DRUMS are driving right now: percussive energy from a
# harmonic/percussive split, smoothed over DENSITY_WINDOW_S and normalised per
# track (0 = strings and bells, 1 = full kit). Onset COUNT was tried and is
# wrong: a clean intro has many crisp onsets, a dense chorus masks them.
DENSITY_WINDOW_S = 2.0
ENERGY_SMOOTH_FRAMES = 5   # 125 ms box on band energy before it drives the body
BLOOM_DENSITY_FLOOR = 0.30  # kick/hit/tick blooms at density 0 are this fraction of full

# LEDs are linear in DMX but the eye is not: a linear fade looks like it
# jumps bright then lingers. Gamma on the dynamic part makes beats pop and
# fades read smoothly in a dark garden. 1.0 = off, 2.0 = display standard.
DIM_GAMMA = 2.0

MIN_HUE_GAP = 0.06         # minimum hue distance between ANY two lights
INTRA_ZONE_SPREAD = 0.10   # hue split between the two lights in one zone
HUE_SMOOTH = 0.02          # how slowly the palette drifts (low = cinematic)
HUE_WOBBLE_BRIGHT = 0.30   # how much spectral brightness moves t within a section
HUE_WOBBLE_TONAL = 0.12    # how much chord/key changes move t

# Strobe is an accent, not a texture. It fires only on the strongest
# transients in the track, and never twice inside STROBE_MIN_GAP_S.
# NOTE: flashing above ~3 Hz is a photosensitive-epilepsy risk in a public
# venue. STROBE_LEVEL is deliberately low; raise it only with care.
STROBE_PERCENTILE = 99.5   # only transients in this top slice may fire
STROBE_MIN_ENERGY = 0.55   # ...and only while the band is already loud
STROBE_MIN_GAP_S = 2.5     # refractory period per light, seconds
STROBE_HOLD_S = 0.18       # how long one burst lasts
STROBE_LEVEL = 0.28        # fixture strobe channel value (low = slower flash)
STROBE_MAX_PER_MIN = 12    # hard ceiling, per light
SECTION_SECONDS = 22       # target length of a detected song section


# ---------------------------------------------------------------------------
# ANALYSIS
# ---------------------------------------------------------------------------
class SongAnalysis:
    def __init__(self, path):
        import librosa
        self.path = path
        self.name = os.path.basename(path)

        y, sr = librosa.load(path, sr=22050, mono=True)
        self.duration = len(y) / sr
        hop = max(1, sr // FPS)

        S = np.abs(librosa.stft(y, n_fft=2048, hop_length=hop))
        freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)

        self.energy, self.flux, self.flux_raw = {}, {}, {}
        box = np.ones(ENERGY_SMOOTH_FRAMES) / ENERGY_SMOOTH_FRAMES
        for band, (lo, hi) in BANDS.items():
            mask = (freqs >= lo) & (freqs < hi)
            raw = S[mask].sum(axis=0)
            # the BODY of a light is the band's loudness, not its frame-level
            # jitter; transients are handled by the event blooms. Smoothing
            # here is what stops the lights trembling.
            self.energy[band] = _norm(np.convolve(raw, box, mode="same"))
            d = np.diff(raw, prepend=raw[:1])
            d[d < 0] = 0
            self.flux_raw[band] = d.copy()   # unclipped — strobe thresholds need the tail
            self.flux[band] = _norm(d)

        rms = librosa.feature.rms(S=S, frame_length=2048, hop_length=hop)[0]
        self.loudness = _norm(rms)
        cent = librosa.feature.spectral_centroid(S=S, sr=sr)[0]
        self.brightness = _norm(np.log1p(cent))
        chroma = librosa.feature.chroma_stft(S=S, sr=sr)
        self.tonal = chroma.argmax(axis=0) / 12.0

        tempo, beats = librosa.beat.beat_track(y=y, sr=sr, hop_length=hop)
        self.tempo = float(np.atleast_1d(tempo)[0])
        self.beat_frames = set(int(b) for b in beats)
        # frames per beat -> used for the wheel's roll delay
        self.frames_per_beat = max(1.0, 60.0 / max(self.tempo, 1e-6) * FPS)

        mfcc = librosa.feature.mfcc(S=librosa.power_to_db(S ** 2), n_mfcc=13)
        self.section_of = self._segment(mfcc)

        self.events, self.density = self._detect_events(S, freqs, sr, hop)

        self.n = min(S.shape[1], len(self.loudness), len(self.brightness),
                     len(self.tonal), len(self.section_of),
                     *[len(v) for v in self.energy.values()])

    def _detect_events(self, S, freqs, sr, hop):
        """Per-band onsets with strength; bell-band onsets classified into DINGs.
        Returns ({frame: [(kind, strength), ...]}, density array)."""
        import librosa
        n = S.shape[1]
        events, all_on = {}, np.zeros(n)
        for kind, (lo, hi) in EVENT_BANDS.items():
            Sb = S[(freqs >= lo) & (freqs < hi)]
            if Sb.shape[0] == 0:
                continue
            env = librosa.onset.onset_strength(S=librosa.power_to_db(Sb ** 2 + 1e-10),
                                               sr=sr, hop_length=hop)
            on = librosa.onset.onset_detect(onset_envelope=env, sr=sr, hop_length=hop,
                                            units="frames")
            on = np.asarray([f for f in on if 0 <= f < n], dtype=int)
            if len(on) == 0:
                continue
            ref = float(np.percentile(env[on], 95)) or 1.0
            if kind == "bell":
                E = Sb.sum(axis=0)
                flat = librosa.feature.spectral_flatness(S=Sb)[0]
                sus = np.array([E[f + 6:f + 14].mean() / (E[f:f + 3].max() + 1e-9)
                                if f + 14 < n else 0.0 for f in on])
                fl = np.array([flat[f:f + 3].mean() for f in on])
                st = env[on]
                keep = ((sus > np.quantile(sus, DING_SUSTAIN_Q))
                        & (fl < np.quantile(fl, DING_FLAT_Q))
                        & (st > np.quantile(st, DING_STRENGTH_Q)))
                for f, s_ in zip(on[keep], st[keep]):
                    events.setdefault(int(f), []).append(("ding", min(1.0, float(s_) / ref)))
                continue
            for f in on:
                events.setdefault(int(f), []).append((kind, min(1.0, float(env[f]) / ref)))
                all_on[f] += 1
        # density: percussive energy share, smoothed
        _, P = librosa.decompose.hpss(S)
        perc = P.sum(axis=0)[:n]
        w = max(1, int(DENSITY_WINDOW_S * FPS))
        density = _norm(np.convolve(perc, np.ones(w) / w, mode="same"))
        return events, density

    def _segment(self, feat):
        import librosa
        k = max(2, int(round(self.duration / SECTION_SECONDS)))
        try:
            bounds = librosa.segment.agglomerative(feat, k)
        except Exception:
            bounds = np.array([0])
        labels = np.zeros(feat.shape[1], dtype=int)
        for i, b in enumerate(bounds):
            labels[b:] = i
        return labels


def analyse_cached(path, verbose=True):
    """Analyse a track, reusing a cached result when the file is unchanged.

    Analysis costs ~30s per track; the show should not pay that at the venue.
    Cache key covers file bytes + size + the tunables that change the output.
    """
    # Key on file CONTENT, not path/mtime, so a cache built on one machine
    # still hits after the songs are copied to the show laptop.
    st = os.stat(path)
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))          # first 1 MB is plenty to identify it
    h.update(f"{st.st_size}|v{CACHE_VERSION}|{FPS}|{SECTION_SECONDS}|"
             f"{sorted(BANDS.items())}".encode())
    key = h.hexdigest()[:16]
    cache_file = os.path.join(CACHE_DIR, key + ".pkl")

    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                a = pickle.load(f)
            required = ("energy", "flux", "flux_raw", "loudness", "brightness",
                        "tonal", "beat_frames", "frames_per_beat", "section_of", "n",
                        "events", "density")
            if all(hasattr(a, attr) for attr in required):
                if verbose:
                    print(f"  (cached) {os.path.basename(path)}")
                return a
        except Exception:
            pass    # corrupt or stale format — just re-analyse

    a = SongAnalysis(path)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache_file, "wb") as f:
            pickle.dump(a, f, protocol=4)
    except Exception:
        pass        # cache is an optimisation, never fatal
    return a


def _norm(x):
    x = np.asarray(x, dtype=float)
    ref = np.percentile(x, 95)
    if ref <= 0:
        ref = x.max() or 1.0
    return np.clip(x / ref, 0.0, 1.0)


# ---------------------------------------------------------------------------
# COLOUR + INTENSITY ENGINE
# ---------------------------------------------------------------------------
def zone_hue_at(zone, t):
    """Hue for a zone at position t (0 = quiet end, 1 = loud end) of its arc."""
    start, span = ZONE_ARC[zone]
    return (start + max(0.0, min(1.0, t)) * span) % 1.0


def _zone_members():
    return {z: [l for l in LIGHTS if l["zone"] == z] for z in ZONES}


def idle_values():
    """A gentle, lit-but-waiting look: gold wheel, green forest, low level.

    Shown before the first track, between tracks, and while analysing, so the
    garden is never dropped into black between songs.
    """
    out = {}
    members = _zone_members()
    for light in LIGHTS:
        zone = light["zone"]
        feel = ZONE_FEEL[zone]
        k = members[zone].index(light)
        offset = (k - (len(members[zone]) - 1) / 2.0) * INTRA_ZONE_SPREAD
        hue = (zone_hue_at(zone, 0.15) + offset) % 1.0   # gold/orange + green, not lime
        sat = feel["sat"][0] + 0.15
        r, g, b = colorsys.hsv_to_rgb(hue, sat, 1.0)
        out[light["name"]] = {
            "dimmer": feel["floor"] + 0.08, "hue": hue, "sat": sat,
            "red": r, "green": g, "blue": b, "strobe": 0.0,
        }
    return out


class ShowEngine:
    def __init__(self, analysis):
        self.a = analysis
        self.env = {l["name"]: 0.0 for l in LIGHTS}
        self.bloom = {l["name"]: {k: 0.0 for k in l.get("events", {})} for l in LIGHTS}
        self._decay = {k: 0.5 ** (1.0 / max(1.0, hl * FPS)) for k, hl in BLOOM_HALF_LIFE.items()}
        # Fire only on transients in the top slice of THIS track, so the rate
        # does not swing wildly between a sparse ballad and a dense mix.
        self._strobe_gate = {
            b: float(np.percentile(analysis.flux_raw[b][:analysis.n], STROBE_PERCENTILE))
            for b in BANDS
        }
        self._last_strobe = {l["name"]: -10 ** 9 for l in LIGHTS}
        self._strobe_times = {l["name"]: [] for l in LIGHTS}
        self._members = _zone_members()
        self._bright_mid = float(np.median(analysis.brightness[:analysis.n]))
        self.section_t = {}
        self._plan_sections()
        # Start the palette where the first section wants it, so a song never
        # opens with a swing from some unrelated colour.
        first = int(analysis.section_of[0])
        self.pos = self.section_t.get(first, 0.3)
        self.hue = {}
        for light in LIGHTS:
            k = self._members[light["zone"]].index(light)
            off = (k - (len(self._members[light["zone"]]) - 1) / 2.0) * INTRA_ZONE_SPREAD
            self.hue[light["name"]] = (zone_hue_at(light["zone"], self.pos) + off) % 1.0

    def _plan_sections(self):
        """Each section gets a contrast position t from its loudness, plus a
        small deterministic offset so two equally-loud sections differ."""
        a = self.a
        lo, hi = CONTRAST_LOUDNESS
        golden = 0.381966
        for sec in sorted(set(int(s) for s in a.section_of[:a.n])):
            idx = np.where(a.section_of[:a.n] == sec)[0]
            energy = float(a.loudness[idx].mean()) if len(idx) else 0.0
            t = (energy - lo) / max(1e-6, hi - lo)
            t += ((sec * golden) % 1.0 - 0.5) * 0.16      # +-0.08 variety
            self.section_t[sec] = float(np.clip(t, 0.0, 1.0))

    def _sample(self, arr, i, lag_frames):
        j = int(i - lag_frames)
        return float(arr[j]) if j >= 0 else float(arr[0])

    def _strobe_value(self, light, i, energy):
        """Fire rarely, hold briefly, then refuse to fire again for a while."""
        name, band = light["name"], light["band"]
        if not ZONE_FEEL[light["zone"]]["strobe"]:
            return 0.0
        hold = STROBE_HOLD_S * FPS
        if i - self._last_strobe[name] < hold:
            return STROBE_LEVEL                      # still inside the burst
        if i - self._last_strobe[name] < STROBE_MIN_GAP_S * FPS:
            return 0.0                               # refractory
        lag = light["lag_beats"] * self.a.frames_per_beat
        raw = self._sample(self.a.flux_raw[band], i, lag)
        if raw < self._strobe_gate[band] or energy < STROBE_MIN_ENERGY:
            return 0.0
        recent = [t for t in self._strobe_times[name] if i - t < 60 * FPS]
        self._strobe_times[name] = recent
        if len(recent) >= STROBE_MAX_PER_MIN:        # hard ceiling
            return 0.0
        self._last_strobe[name] = i
        self._strobe_times[name].append(i)
        return STROBE_LEVEL

    def frame(self, i):
        """Return {name: dict(dimmer, hue, sat, r, g, b, strobe)} for frame i."""
        a = self.a
        sec = int(a.section_of[i])

        # --- contrast position: section anchor, wobbled by the music -------
        target = (self.section_t.get(sec, 0.3)
                  + HUE_WOBBLE_BRIGHT * (float(a.brightness[i]) - self._bright_mid)
                  + HUE_WOBBLE_TONAL * (float(a.tonal[i]) - 0.5))
        target = float(np.clip(target, 0.0, 1.0))
        self.pos += (target - self.pos) * HUE_SMOOTH

        loud = float(a.loudness[i])
        dens = float(a.density[i])             # 0 sparse .. 1 busy, right now
        zone_hue = {z: zone_hue_at(z, self.pos) for z in ZONES}

        # --- per light: envelope + blooms + hue slot ----------------------
        dims, sats = {}, {}
        for light in LIGHTS:
            name, band, zone = light["name"], light["band"], light["zone"]
            feel = ZONE_FEEL[zone]
            lag = light["lag_beats"] * a.frames_per_beat
            j = int(i - lag)                   # this light's (lagged) time

            # 1. body: smoothed band energy, speed follows musical density
            e = self._sample(a.energy[band], i, lag)
            on_beat = zone == "wheel" and j in a.beat_frames
            target = min(1.0, light.get("gain", 1.0) * (e + (0.08 if on_beat else 0.0)))
            att = feel["attack"][0] + (feel["attack"][1] - feel["attack"][0]) * dens
            rel = feel["release"][0] + (feel["release"][1] - feel["release"][0]) * dens
            coeff = att if target > self.env[name] else rel
            self.env[name] += (target - self.env[name]) * coeff

            # 2. blooms: events this light listens to, each fading at its own rate
            bl = self.bloom[name]
            for kind in bl:
                bl[kind] *= self._decay[kind]
            for kind, strength in a.events.get(j, ()):
                if kind in bl:
                    g = light["events"][kind]
                    if kind != "ding":          # drum-ish blooms scale with how hard the drums drive;
                        g *= BLOOM_DENSITY_FLOOR + (1.0 - BLOOM_DENSITY_FLOOR) * dens   # bells always shine
                    bl[kind] = min(1.0, bl[kind] + strength * g)
            bloom = min(1.0, sum(bl.values()))
            ding = bl.get("ding", 0.0)

            body = min(1.0, max(0.0, self.env[name]))
            level = body + (1.0 - body) * bloom      # peak at 1, fade back to body
            dims[name] = feel["floor"] + (1.0 - feel["floor"]) * level ** DIM_GAMMA

            # hue: zone arc position, split within the pair, nudged by blooms
            members = self._members[zone]
            k = members.index(light)
            offset = (k - (len(members) - 1) / 2.0) * INTRA_ZONE_SPREAD
            want = (zone_hue[zone] + offset + 0.02 * bloom) % 1.0
            self.hue[name] = _hue_lerp(self.hue[name], want, 0.18)

            # saturation: vivid when this band dominates, pastel when quiet,
            # clamped to the zone's surface; a DING pulls toward white (shine)
            tot = sum(float(a.energy[b][i]) for b in BANDS) or 1e-6
            dominance = float(a.energy[band][i]) / tot * len(BANDS)
            raw_sat = 0.45 + 0.40 * dominance + 0.20 * loud
            lo_s, hi_s = feel["sat"]
            sats[name] = float(np.clip(raw_sat, lo_s, hi_s)) * (1.0 - DING_SHINE * ding)

        # --- guarantee all four hues stay apart ---------------------------
        names = [l["name"] for l in LIGHTS]
        spread = enforce_separation([self.hue[n] for n in names], MIN_HUE_GAP)
        for n, h in zip(names, spread):
            self.hue[n] = h

        # --- build output --------------------------------------------------
        out = {}
        for light in LIGHTS:
            name, band = light["name"], light["band"]
            lag = light["lag_beats"] * a.frames_per_beat
            e = self._sample(a.energy[band], i, lag)
            strobe = self._strobe_value(light, i, e)
            r, g, b = colorsys.hsv_to_rgb(self.hue[name], sats[name], 1.0)
            out[name] = {
                "dimmer": dims[name], "hue": self.hue[name], "sat": sats[name],
                "red": r, "green": g, "blue": b, "strobe": strobe,
            }
        return out


def _hue_lerp(cur, target, amount):
    d = (target - cur + 0.5) % 1.0 - 0.5
    return (cur + d * amount) % 1.0


def enforce_separation(hues, gap):
    """Spread hues so every pair is >= gap apart on the colour circle.

    Deterministic: sorts, opens tight gaps, and if the fan can no longer close
    on itself, falls back to even spacing around the circular mean. Relative
    order is preserved so lights never swap colours with each other.
    """
    n = len(hues)
    if n < 2 or gap * n >= 1.0:
        return list(hues)

    order = sorted(range(n), key=lambda i: hues[i])
    vals = [hues[i] for i in order]
    for i in range(1, n):
        if vals[i] - vals[i - 1] < gap:
            vals[i] = vals[i - 1] + gap

    if (vals[0] + 1.0) - vals[-1] < gap:
        centre = _circular_mean(hues)
        vals = [centre + (i - (n - 1) / 2.0) / n for i in range(n)]

    out = [0.0] * n
    for slot, idx in enumerate(order):
        out[idx] = vals[slot] % 1.0
    return out


def _circular_mean(hues):
    ang = [h * 2 * np.pi for h in hues]
    x = sum(np.cos(a) for a in ang)
    y = sum(np.sin(a) for a in ang)
    return float(np.arctan2(y, x) / (2 * np.pi)) % 1.0


# ---------------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------------
HSV_FADERS = ("dimmer", "hue", "sat", "strobe")
RGB_FADERS = ("dimmer", "red", "green", "blue", "strobe")


def osc_addresses(mode):
    faders = HSV_FADERS if mode == "hsv" else RGB_FADERS
    return [f"{l['osc']}/{f}" for l in LIGHTS for f in faders]


class OSCOut:
    def __init__(self, mode="rgb", simulate=False):
        self.mode = mode
        self.simulate = simulate
        self.faders = HSV_FADERS if mode == "hsv" else RGB_FADERS
        self.client = None
        if not simulate:
            from pythonosc.udp_client import SimpleUDPClient
            self.client = SimpleUDPClient(DASLIGHT_IP, DASLIGHT_PORT)

    def send_frame(self, values):
        if self.simulate:
            return
        for light in LIGHTS:
            v = values[light["name"]]
            for f in self.faders:
                self.client.send_message(f"{light['osc']}/{f}", float(v[f]))

    def blackout(self):
        if self.simulate:
            return
        for light in LIGHTS:
            for f in self.faders:
                self.client.send_message(f"{light['osc']}/{f}", 0.0)


class ArtNetOut:
    """Raw DMX over Art-Net — writes the patched channels directly.

    Needs no Map OSC work: channel numbers come straight from the Daslight
    patch (Luz 1 at 1, Luz 2 at 10, Luz 4 at 20, Luz 3 at 30).
    """

    def __init__(self, ip=None, port=None, universe=None, simulate=False):
        self.ip = ip or ARTNET_IP
        self.port = port or ARTNET_PORT
        self.universe = ARTNET_UNIVERSE if universe is None else universe
        self.simulate = simulate
        self.seq = 0
        self.sock = None
        if not simulate:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    def _packet(self, dmx):
        self.seq = self.seq % 255 + 1
        return (b"Art-Net\0"
                + struct.pack("<H", 0x5000)              # OpDmx
                + struct.pack(">H", 14)                  # protocol version
                + bytes([self.seq, 0,
                         self.universe & 0xFF, (self.universe >> 8) & 0xFF])
                + struct.pack(">H", len(dmx))
                + bytes(dmx))

    def send_frame(self, values):
        if self.simulate:
            return
        dmx = bytearray(512)
        for light in LIGHTS:
            v = values[light["name"]]
            base = light["addr"] - 1          # DMX addresses are 1-based
            for key, off in DMX_OFFSET.items():
                # RGB go out at full value and the fixture's own Dimmer channel
                # scales them. Pre-multiplying here as well would square the
                # intensity (0.5 dimmer -> 0.25 output) and crush the low end.
                dmx[base + off] = max(0, min(255, int(round(v[key] * 255))))
        self.sock.sendto(self._packet(dmx), (self.ip, self.port))

    def blackout(self):
        if self.simulate:
            return
        for _ in range(3):                    # UDP: send a few, none are acked
            self.sock.sendto(self._packet(bytearray(512)), (self.ip, self.port))
            time.sleep(0.02)


def artnet_discover(timeout=2.0, ip="255.255.255.255", port=ARTNET_PORT):
    """Broadcast an ArtPoll and collect ArtPollReply from any listening node.

    This is how we find out whether Daslight will accept Art-Net at all,
    without needing a fixture plugged in to watch.
    """
    poll = (b"Art-Net\0" + struct.pack("<H", 0x2000)
            + struct.pack(">H", 14) + bytes([0x02, 0x00]))
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        # Daslight already holds 6454; REUSEPORT lets us hear the broadcast
        # replies alongside it instead of failing to bind.
        try:
            rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    try:
        rx.bind(("0.0.0.0", port))
        bound = True
    except OSError:
        bound = False          # something else already holds 6454 exclusively
    rx.settimeout(0.4)

    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    tx.settimeout(0.4)
    for target in {ip, "127.0.0.1"}:
        try:
            tx.sendto(poll, (target, port))
        except OSError:
            pass

    # Nodes reply either to the Art-Net port (spec) or to the sender's own
    # port (common). Listen on both; on the same host as another Art-Net app
    # the 6454 socket may never see the reply, so the tx socket matters.
    found, seen, deadline = [], set(), time.time() + timeout
    socks = [tx] + ([rx] if bound else [])
    while time.time() < deadline:
        for sk in socks:
            try:
                data, src = sk.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                continue
            if data[:8] == b"Art-Net\0" and int.from_bytes(data[8:10], "little") == 0x2100:
                short = data[26:44].split(b"\0")[0].decode(errors="replace")
                long_ = data[44:108].split(b"\0")[0].decode(errors="replace")
                key = (src[0], short)
                if key not in seen:
                    seen.add(key)
                    found.append((src[0], short or long_ or "(unnamed)"))
    rx.close(); tx.close()
    return found, bound


def _bar(values):
    cells = []
    for light in LIGHTS:
        v = values[light["name"]]
        n = int(v["dimmer"] * 10)
        col = "\033[38;2;%d;%d;%dm" % (int(v["red"] * 255), int(v["green"] * 255), int(v["blue"] * 255))
        cells.append(f"{col}{'█' * n}{' ' * (10 - n)}\033[0m{'*' if v['strobe'] else ' '}")
    return "WHEEL " + " ".join(cells[:2]) + "  FOREST " + " ".join(cells[2:])


# ---------------------------------------------------------------------------
# PLAYBACK
# ---------------------------------------------------------------------------
def load_audio(path):
    """Decode a track for playback. Done ahead of time so songs start instantly."""
    import librosa
    y, sr = librosa.load(path, sr=44100, mono=False)
    return (y.T if y.ndim > 1 else y), sr


def play_song(analysis, out, keys, simulate=False, audio=True, audio_data=None):
    engine = ShowEngine(analysis)
    player = None
    if audio:
        try:
            import sounddevice as sd
            y, sr = audio_data if audio_data is not None else load_audio(analysis.path)
            sd.play(y, sr)
            player = sd
        except Exception as e:
            print(f"  (audio playback unavailable: {e} — start the track manually)")

    print(f"\n♪ {analysis.name} — {analysis.tempo:.0f} BPM, {analysis.duration/60:.1f} min, "
          f"{len(set(int(s) for s in analysis.section_of[:analysis.n]))} sections")

    t0 = time.perf_counter()
    result = "done"
    try:
        while True:
            while not keys.empty():
                k = keys.get_nowait()
                if k in ("n", "q"):
                    result = "next" if k == "n" else "quit"
            if result != "done":
                break
            t = time.perf_counter() - t0
            i = int(t * FPS)
            if i >= analysis.n:
                break
            values = engine.frame(i)
            out.send_frame(values)
            if simulate and i % 3 == 0:
                print(f"\r{t:6.1f}s {_bar(values)}", end="", flush=True)
            time.sleep(max(0.0, t0 + (i + 1) / FPS - time.perf_counter()))
    except KeyboardInterrupt:
        result = "quit"
    finally:
        if player is not None:
            player.stop()
        if result == "quit":
            out.blackout()
        else:
            out.send_frame(idle_values())      # hold the garden lit between songs
        if simulate:
            print()
    return result


def key_listener(q):
    try:
        import msvcrt
        while True:
            ch = msvcrt.getch().decode(errors="ignore").lower()
            if ch in ("n", "q"):
                q.put(ch)
    except ImportError:
        import select
        while True:
            if select.select([sys.stdin], [], [], 0.3)[0]:
                ch = sys.stdin.readline().strip().lower()
                if ch in ("n", "q"):
                    q.put(ch)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def collect_tracks(path, shuffle=False):
    if os.path.isfile(path):
        return [path]
    files = [os.path.join(path, f) for f in sorted(os.listdir(path))
             if f.lower().endswith(AUDIO_EXTS)]
    if shuffle:
        random.shuffle(files)
    return files


def print_map_list(mode):
    print(f"\nOSC addresses to map in Daslight 5  (mode: {mode})")
    print("Mappings > Map OSC, click the fader, then run --learn with the address.\n")
    for light in LIGHTS:
        print(f"  {light['name']:9s} (addr {light['addr']:>2}, {light['zone']}, {light['band']})")
        faders = HSV_FADERS if mode == "hsv" else RGB_FADERS
        for f in faders:
            print(f"      {light['osc']}/{f}")
    print()


def learn_mode(address):
    from pythonosc.udp_client import SimpleUDPClient
    c = SimpleUDPClient(DASLIGHT_IP, DASLIGHT_PORT)
    print(f"Sending {address} -> {DASLIGHT_IP}:{DASLIGHT_PORT}, wiggling 0.1/0.9")
    print("In Daslight: Mappings > Map OSC > click the fader. Ctrl+C when learned.")
    v = 0.1
    try:
        while True:
            v = 0.9 if v < 0.5 else 0.1
            c.send_message(address, v)
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\nStopped.")


def artnet_test(ip=ARTNET_IP, universe=ARTNET_UNIVERSE, hold=1.6, port=ARTNET_PORT):
    """Walk each fixture through R/G/B/White so a human can watch.

    This is the definitive answer to "does Daslight accept Art-Net input".
    If a light changes colour on cue, it works. If nothing moves, it does not.
    """
    out = ArtNetOut(ip=ip, port=port, universe=universe)
    steps = [("RED", 1, 0, 0), ("GREEN", 0, 1, 0), ("BLUE", 0, 0, 1), ("WHITE", 1, 1, 1)]
    blank = {l["name"]: dict(dimmer=0.0, red=0.0, green=0.0, blue=0.0, strobe=0.0)
             for l in LIGHTS}

    print(f"\nArt-Net test -> {ip}:{port}, universe {universe}")
    print("Watch the rig. Each fixture lights alone, in order.\n")
    try:
        for light in LIGHTS:
            print(f"  {light['name']:9s} (DMX {light['addr']:>2}, {light['zone']}) ", end="", flush=True)
            for label, r, g, b in steps:
                print(f"{label} ", end="", flush=True)
                vals = dict(blank)
                vals[light["name"]] = dict(dimmer=1.0, red=float(r), green=float(g),
                                           blue=float(b), strobe=0.0)
                t_end = time.time() + hold
                while time.time() < t_end:       # keep refreshing; Art-Net is stateless
                    out.send_frame(vals)
                    time.sleep(1.0 / FPS)
            print()
        print("\n  All four together, fading up and down ...")
        for k in range(80):
            lvl = (1 - abs(k / 40.0 - 1))
            vals = {l["name"]: dict(dimmer=lvl, red=1.0, green=0.6, blue=0.0, strobe=0.0)
                    for l in LIGHTS}
            out.send_frame(vals)
            time.sleep(1.0 / FPS)
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        out.blackout()
    print("\n  Did the lights follow along?")
    print("    yes -> Art-Net works, just run:  python run_show.py")
    print("    no  -> try a different universe:  --artnet-universe 1")
    print("           still no -> use OSC:  python run_show.py --osc-setup\n")


def main():
    global DASLIGHT_PORT, DASLIGHT_IP
    p = argparse.ArgumentParser(description="La Rueda music-reactive light engine")
    p.add_argument("path", nargs="?", default=os.path.join(HERE, "songs"),
                   help="Folder of songs, or one file (default: ./songs)")
    p.add_argument("--mode", choices=["artnet", "rgb", "hsv"], default="artnet",
                   help="artnet = raw DMX, no mapping needed (default); "
                        "rgb/hsv = OSC onto faders you mapped in Daslight")
    p.add_argument("--no-audio", action="store_true",
                   help="Do not play the track (something else is feeding the speakers)")
    p.add_argument("--artnet-universe", type=int, default=ARTNET_UNIVERSE,
                   help="Art-Net universe; Daslight 'Universe 1' is usually 0")
    p.add_argument("--artnet-port", type=int, default=ARTNET_PORT,
                   help="Art-Net UDP port (6454). Use another to target rig_preview.py")
    p.add_argument("--shuffle", action="store_true")
    p.add_argument("--simulate", action="store_true", help="No hardware, draw in terminal")
    p.add_argument("--learn", metavar="ADDR")
    p.add_argument("--map-list", action="store_true", help="Print all OSC addresses to map")
    p.add_argument("--artnet-test", action="store_true",
                   help="Walk each fixture through R/G/B/W to prove Art-Net works")
    p.add_argument("--artnet-discover", action="store_true",
                   help="Broadcast ArtPoll and list any Art-Net nodes that answer")
    p.add_argument("--ip", default=DASLIGHT_IP)
    p.add_argument("--port", type=int, default=DASLIGHT_PORT)
    args = p.parse_args()

    DASLIGHT_IP, DASLIGHT_PORT = args.ip, args.port

    if args.artnet_test:
        artnet_test(ip=args.ip, universe=args.artnet_universe, port=args.artnet_port)
        return
    if args.artnet_discover:
        found, bound = artnet_discover(port=args.artnet_port)
        if not bound:
            print(f"Could not also listen on {args.artnet_port} (another app holds it); "
                  f"listening on the poll socket only.")
        print(f"Art-Net nodes answering: {len(found)}")
        for ip_, nm in found:
            print(f"  {ip_}  {nm}")
        if not found:
            print("No replies. Daslight may still accept DMX without answering"
                  " ArtPoll — confirm with --artnet-test at the rig.")
        return
    if args.map_list:
        print_map_list(args.mode)
        return
    if args.learn:
        learn_mode(args.learn)
        return
    tracks = collect_tracks(args.path, args.shuffle)
    if not tracks and os.path.isdir(args.path):
        sys.exit(f"No audio files in {args.path}")
    if not tracks:
        sys.exit("No audio files found.")
    print(f"{len(tracks)} track(s) queued. [n] next  [q] quit")

    if args.mode == "artnet":
        out = ArtNetOut(ip=args.ip, port=args.artnet_port,
                        universe=args.artnet_universe, simulate=args.simulate)
        print(f"Output: Art-Net -> {args.ip}:{args.artnet_port} universe "
              f"{args.artnet_universe} (raw DMX, no Daslight mapping needed)")
    else:
        out = OSCOut(mode=args.mode, simulate=args.simulate)
        print(f"Output: OSC -> {args.ip}:{args.port} (needs Map OSC in Daslight)")
    keys = queue.Queue()
    threading.Thread(target=key_listener, args=(keys,), daemon=True).start()

    # Lights up immediately — the garden should not sit dark while we analyse.
    out.send_frame(idle_values())

    ahead = {}
    want_audio = not args.no_audio

    def preload(path):
        try:
            a = analyse_cached(path)
            audio = load_audio(path) if want_audio else None
            ahead[path] = (a, audio)
        except Exception as e:
            ahead[path] = e

    print(f"Preparing {os.path.basename(tracks[0])} ...")
    preload(tracks[0])

    for idx, track in enumerate(tracks):
        item = ahead.pop(track, None)
        if item is None:
            print(f"Preparing {os.path.basename(track)} ...")
            preload(track)
            item = ahead.pop(track)
        if isinstance(item, Exception):
            print(f"  skipping {os.path.basename(track)}: {item}")
            continue
        a, audio = item
        if idx + 1 < len(tracks):
            threading.Thread(target=preload, args=(tracks[idx + 1],), daemon=True).start()
        if play_song(a, out, keys, simulate=args.simulate,
                     audio=want_audio, audio_data=audio) == "quit":
            break

    out.blackout()
    print("Show finished. Lights blacked out.")


if __name__ == "__main__":
    main()
