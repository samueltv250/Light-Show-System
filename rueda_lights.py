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
CONTROL_PORT = 6460        # UDP: "next" / "quit" (rig_preview buttons, or anything else)

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".cache")
# Bump whenever SongAnalysis gains/loses a field, or a cached value changes
# meaning. Without this an updated script silently loads an old pickle.
CACHE_VERSION = 10

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
#
# Each zone has a RHYTHM light and a TONE light, so the pair plays against
# itself instead of moving together:
#   wheel_a / forest_a  ride "perc" — ANY drum or percussion (congas,
#                       timbales, bongo, clave, snare, kick, hats). In Latin
#                       music the percussion IS the arrangement, so these two
#                       are the ones that hit.
#   wheel_b             the deep lagged swell — bass only, so light still
#                       rolls across the wheel half a beat behind wheel_a.
#   forest_b            tone and shimmer — bells/dings SHINE here, undisturbed
#                       by the drums.
LIGHTS = [
    {"name": "wheel_a",  "zone": "wheel",  "band": "bass",  "lag_beats": 0.0, "osc": "/wheel_a",  "addr": 1,
     "events": {"perc": 0.70, "kick": 0.30}},          # RHYTHM — every drum, extra weight on the kick
    {"name": "wheel_b",  "zone": "wheel",  "band": "bass",  "lag_beats": 0.5, "osc": "/wheel_b",  "addr": 10,
     "events": {"kick": 0.35}},                        # TONE — deep, lagged, smooth
    {"name": "forest_a", "zone": "forest", "band": "mids",  "lag_beats": 0.0, "osc": "/forest_a", "addr": 30,
     "events": {"perc": 0.50, "hit": 0.15}},           # RHYTHM — the forest's drummer, but still foliage
    {"name": "forest_b", "zone": "forest", "band": "highs", "lag_beats": 0.25, "osc": "/forest_b", "addr": 20,
     "gain": 1.2,    # hats are sparse; lift so this light is not the dim one of the pair
     "events": {"tick": 0.15, "ding": 1.00}},          # TONE — bells/dings SHINE here
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

# ---------------------------------------------------------------------------
# PALETTES — the hue arcs, swappable at runtime
# ---------------------------------------------------------------------------
# `base` is SURFACE-AWARE and is the one grounded in the real venue: warm on
# the rust-red wooden wheel, cool on the foliage, because blue on red-brown
# wood reads as mud and red on leaves kills them. The others are deliberate
# stylistic departures — they will look striking, but some of them put light
# on a surface that does not flatter it. The `note` on each says which.
# Changing palette takes effect immediately and the hues drift across; no
# engine rebuild, so it is safe to do mid-song.
PALETTES = {
    "base": {
        "arc": {"wheel": (0.13, -0.18), "forest": (0.29, +0.30)},
        "sat": None,          # keep each zone's own saturation range
        "note": "surface-aware: gold->crimson wheel, chartreuse->azure forest",
    },
    "auto": {
        # Not a palette of its own: the engine classifies each SECTION of the
        # track by energy, percussive drive and brightness, and morphs between
        # the palettes below as the song moves through its parts. Falls back
        # to the base arcs if a track has no usable sections.
        "arc": {"wheel": (0.13, -0.18), "forest": (0.29, +0.30)},
        "sat": None,
        "note": "per-section: quiet->base, warm->ember, airy->tropical, "
                "driving->neon (ocean stays manual: muddy on real wood)",
    },
    "neon": {
        "arc": {"wheel": (0.92, -0.14), "forest": (0.38, +0.22)},
        "sat": (0.95, 1.00),  # full electric saturation, no pastels
        "note": "electric magenta/violet wheel against green->azure forest",
    },
    "ember": {
        "arc": {"wheel": (0.09, -0.09), "forest": (0.21, -0.05)},
        "sat": (0.70, 1.00),
        "note": "all warm: orange->red wheel, gold forest. Flatters the wood; "
                "the foliage goes olive rather than green",
    },
    "ocean": {
        "arc": {"wheel": (0.45, +0.10), "forest": (0.62, +0.12)},
        "sat": (0.60, 0.95),
        "note": "all cool: teal->cyan wheel, blue->violet forest. Striking, "
                "but cool light on the rust-red wheel reads muddy",
    },
    "tropical": {
        "arc": {"wheel": (0.97, +0.10), "forest": (0.30, +0.16)},
        "sat": (0.85, 1.00),
        "note": "hot pink->orange wheel against chartreuse->teal forest",
    },
}
CURRENT_PALETTE = "base"
PALETTE_SAT = None            # (lo, hi) overriding the zone ranges, or None
AUTO_PALETTE_DWELL_S = 14.0   # never sit on one palette for less than this
AUTO_ARC_MORPH = 0.012        # per-frame arc crossfade (~1.5 s to change over)


def apply_palette(name):
    """Switch the colour palette. Takes effect on the next frame."""
    global ZONE_ARC, PALETTE_SAT, CURRENT_PALETTE
    if name not in PALETTES:
        name = "base"
    pal = PALETTES[name]
    ZONE_ARC = dict(pal["arc"])
    PALETTE_SAT = pal["sat"]
    CURRENT_PALETTE = name
    return name


def next_palette():
    names = list(PALETTES)
    return names[(names.index(CURRENT_PALETTE) + 1) % len(names)]

# Per-zone dynamics. The wheel is the kinetic centrepiece — it pulses. The
# forest is atmosphere — it breathes. People sit in the forest near a river
# edge at night, so it keeps a higher floor: the garden dims, it never dies.
# attack/release are (slow, fast) pairs; the music's DENSITY picks where
# between them each moment sits — sparse passages glow, busy passages snap.
ZONE_FEEL = {
    "wheel":  dict(attack=(0.14, 0.45), release=(0.04, 0.14), floor=0.02, sat=(0.55, 1.00), strobe=True),
    "forest": dict(attack=(0.08, 0.30), release=(0.03, 0.09), floor=0.03, sat=(0.50, 0.90), strobe=False),
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
# "perc" is not a band: it is every onset on the PERCUSSIVE component of a
# harmonic/percussive split, so it catches any drum anywhere in the spectrum
# (conga, timbale, bongo, clave, cowbell, snare, kick, hat) while ignoring
# sustained vocals and chords. The band kinds below stay for colour/weight.
EVENT_BANDS = {
    "kick": (20, 250),      # bass drum / bass attacks       -> wheel weight
    "hit":  (250, 4000),    # snare, chord stabs, vocal hits
    "tick": (4000, 16000),  # hats, shimmer                  -> forest_b
    "bell": (1200, 7000),   # bells, chimes, plucks live here; classified into DING
}
BLOOM_HALF_LIFE = {"perc": 0.26, "kick": 0.25, "hit": 0.45,
                   "tick": 0.10, "ding": 1.60, "instr": 0.30}   # seconds
# Light the ACCENTS, not every stroke. A busy conga/timbale pattern can run
# 4-5 strokes a second; blooming on all of them is the flicker we removed.
# Keep only onsets above this quantile of the track's own percussive onsets,
# so the rhythm lights hit the pattern's accents and ride through the filler.
PERC_ACCENT_Q = 0.55
PERC_MIN_GAP_S = 0.14      # and never two perc blooms closer than this
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

# ---------------------------------------------------------------------------
# BRIGHTNESS — hierarchy and anticipation
# ---------------------------------------------------------------------------
# Four lights all at full is a wash, not a design. Every phrase the four
# VOICES are ranked by how far each is standing above its own usual level,
# and the lights are tiered accordingly, so at any moment one light leads and
# the others support it. Roles are decided per phrase (not per frame) and
# crossfaded, so the hierarchy shifts musically instead of twitching.
#   wheel_a  -> percussion      wheel_b  -> bass
#   forest_a -> mids / vocal    forest_b -> highs / shimmer
ROLE_PHRASE_BEATS = 16          # re-rank every 4 bars
ROLE_LEVELS = {
    "lead":       1.00,         # the voice currently leading the song
    "main":       0.86,         # the main chorus behind it
    "complement": 0.70,         # chorus complement
}   # gentler than they look: the GATES below are what darken the rig
ROLE_TIERS = ["lead", "main", "complement", "complement"]
# A light whose gate keeps it dark most of the song is an event, not a layer:
# tiering it down would mute the very moments it exists for (a bell strike
# peaked at 0.41 before this). Such lights are exempt and always run at full.
ROLE_EXEMPT_DUTY = 0.35
ROLE_FADE = 0.02                # per-frame crossfade toward the new tier (~1 s)

# ANTICIPATION: a light dips out just before its OWN voice surges, then comes
# back on the hit — the breath before the phrase lands. Only possible because
# the whole track is analysed up front, so we can look ahead.
ANTICIPATE = True
ANTICIPATION_LEAD_S = 0.8       # how long before the surge the light dips
ANTICIPATION_FLOOR = 0.04       # how far down it goes (0 = fully out)
ANTICIPATION_MIN_GAP_S = 14.0   # per light: an event, never a habit
ANTICIPATION_Q = 0.97           # only surges in this top slice qualify
# Only ONE light is ever dipped at a time, so the garden is never dark.

# ---------------------------------------------------------------------------
# GATES — a light is DARK unless its own part is playing
# ---------------------------------------------------------------------------
# This is what makes the rig read as four instruments rather than four
# meters. Each light follows one PART of the track and stays off the rest of
# the time: the bell light lights only when a bell rings, the drum light only
# while the kit plays, and so on.
#
# Parts are discovered per song by NMF on the HARMONIC spectrogram (drums
# removed), so nothing is hard-coded to an instrument — a song with a violin
# gets a violin part, a song with a horn section gets a horn part.
#
# NOTE: NMF on a full mix does NOT cleanly isolate instruments; components
# overlap and most are active most of the time. So the gate threshold is not
# fixed — it is solved per light per song to hit a TARGET DUTY CYCLE. The
# light opens during that light's most prominent moments and is dark
# otherwise, whatever the source looks like. That is what guarantees the
# effect on any track.
N_PARTS = 10
GATE_DUTY = {          # fraction of the song each light is allowed to be lit
    "wheel_a":  0.45,  # the drums — on when the kit is driving
    "wheel_b":  0.40,  # the bass part
    "forest_a": 0.38,  # the melody / lead instrument
    "forest_b": 0.00,  # bells only — see DING_GATE_HOLD_S below
}
GATE_HYSTERESIS = 0.75    # close at 75% of the opening threshold (no chatter)
GATE_MIN_ON_S = 0.6       # once lit, stay lit at least this long
GATE_MIN_OFF_S = 0.7      # once dark, stay dark at least this long
GATE_ATTACK_S = 0.10      # how fast a light comes up when its part starts
GATE_RELEASE_S = 0.55     # how slowly it fades when the part stops
DING_GATE_HOLD_S = 1.8    # the bell light stays lit this long after each ding
DING_GATE_MIN = 8         # ...but only if the track has at least this many

# Hard floor per zone on the BRIGHTEST light of that zone. People sit in the
# forest, by a river, at night — that area must never fall dark, however the
# music goes. The wheel may go almost out: it is a feature, not a footpath.
# When it engages, the zone's lights are scaled together so their relative
# dynamics survive.
ZONE_SAFETY_FLOOR = {"forest": 0.18, "wheel": 0.0}

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
# SCENE MODES — two different shows from the same analysis
# ---------------------------------------------------------------------------
# "base"   the garden show: glows, gates, bells that shine. The quiet end of
#          the night, and the safe default.
# "mid"    lively pop and rock: awake and moving, still a garden.
# "punchy" the dancefloor show: fast envelopes, short blooms, a bloom on
#          EVERY beat, gates opened up so the lights are free to hit, and
#          strobes that fire on the beat rather than once in a while. Built
#          for four-on-the-floor (Daft Punk, house, techno).
#
# SAFETY: punchy raises the strobe rate a long way, but not without limit.
# Sustained flashing above ~3 Hz is the photosensitive-epilepsy threshold,
# and people sit in the forest. Beat-synced strobing on a 120-130 BPM track
# is ~2 Hz, which stays under that line, so STROBE_MAX_PER_MIN is the
# backstop that keeps it there. Raise it deliberately or not at all, and
# note the forest never strobes in either mode.
SCENE_MODES = {
    "base": {},          # the module-level values above are the base show
    "mid": {
        # The middle of the night: lively pop and rock (Viva La Vida, Little
        # Talks, Drops of Jupiter) — the garden is awake and moving, but it
        # is still a garden. Every value sits between base and punchy.
        "ZONE_FEEL": {
            "wheel":  dict(attack=(0.28, 0.62), release=(0.10, 0.28),
                           floor=0.02, sat=(0.58, 1.00), strobe=True),
            "forest": dict(attack=(0.20, 0.48), release=(0.08, 0.20),
                           floor=0.03, sat=(0.52, 0.92), strobe=False),
        },
        "BLOOM_HALF_LIFE": {"perc": 0.17, "kick": 0.18, "hit": 0.30,
                            "tick": 0.08, "ding": 1.25, "beat": 0.16},
        "BEAT_BLOOM": 0.32,
        "BLOOM_DENSITY_FLOOR": 0.45,
        "PERC_ACCENT_Q": 0.38,
        "PERC_MIN_GAP_S": 0.09,
        "GATE_DUTY": {"wheel_a": 0.62, "wheel_b": 0.56,
                      "forest_a": 0.54, "forest_b": 0.00},
        "GATE_ATTACK_S": 0.07,
        "GATE_RELEASE_S": 0.38,
        "GATE_MIN_ON_S": 0.40,
        "GATE_MIN_OFF_S": 0.45,
        "ROLE_LEVELS": {"lead": 1.00, "main": 0.90, "complement": 0.77},
        "ROLE_PHRASE_BEATS": 12,
        "STROBE_PERCENTILE": 97.5,
        "STROBE_MIN_GAP_S": 1.10,
        "STROBE_HOLD_S": 0.14,
        "STROBE_LEVEL": 0.34,
        "STROBE_MAX_PER_MIN": 34,
        "STROBE_MIN_ENERGY": 0.48,
        "DIM_GAMMA": 1.70,
        "HUE_SMOOTH": 0.033,
        "ANTICIPATION_MIN_GAP_S": 11.0,
        "DING_GATE_HOLD_S": 1.4,
    },
    "punchy": {
        # envelopes: snap instead of breathe
        "ZONE_FEEL": {
            "wheel":  dict(attack=(0.45, 0.85), release=(0.18, 0.45),
                           floor=0.02, sat=(0.60, 1.00), strobe=True),
            "forest": dict(attack=(0.35, 0.70), release=(0.14, 0.35),
                           floor=0.03, sat=(0.55, 0.95), strobe=False),
        },
        # blooms: short and hard, plus one on every beat
        "BLOOM_HALF_LIFE": {"perc": 0.10, "kick": 0.12, "hit": 0.18,
                            "tick": 0.06, "ding": 0.90, "beat": 0.13},
        "BEAT_BLOOM": 0.62,          # wheel lights punch on the beat itself
        "BLOOM_DENSITY_FLOOR": 0.65,  # hit hard even where the kit thins out
        # percussion: take nearly every stroke, not just the accents
        "PERC_ACCENT_Q": 0.20,
        "PERC_MIN_GAP_S": 0.05,
        # gates: open up so the lights are free to move
        "GATE_DUTY": {"wheel_a": 0.80, "wheel_b": 0.72,
                      "forest_a": 0.70, "forest_b": 0.00},
        "GATE_ATTACK_S": 0.04,
        "GATE_RELEASE_S": 0.22,
        "GATE_MIN_ON_S": 0.25,
        "GATE_MIN_OFF_S": 0.25,
        # hierarchy: flatter — everything is allowed to be loud
        "ROLE_LEVELS": {"lead": 1.00, "main": 0.94, "complement": 0.84},
        "ROLE_PHRASE_BEATS": 8,
        # strobe: on the beat, often. STROBE_MAX_PER_MIN is the safety cap.
        "STROBE_PERCENTILE": 94.0,
        "STROBE_MIN_GAP_S": 0.40,
        "STROBE_HOLD_S": 0.10,
        "STROBE_LEVEL": 0.42,
        "STROBE_MAX_PER_MIN": 80,
        "STROBE_MIN_ENERGY": 0.40,
        # brighter overall, faster colour movement, dips still allowed
        "DIM_GAMMA": 1.45,
        "HUE_SMOOTH": 0.05,
        "ANTICIPATION_MIN_GAP_S": 9.0,
        "DING_GATE_HOLD_S": 1.0,
    },
}
# mid-instrumental: same pacing as mid, but instead of fixed frequency bands
# the four lights are handed the four most ACTIVE discovered instruments of
# the moment, and each light hits on its own instrument's note starts.
SCENE_MODES["mid-instrumental"] = dict(
    SCENE_MODES["mid"],
    INSTRUMENT_MODE=True,
    INSTR_BLOOM_GAIN=0.95,          # how hard a note start hits its light
    # all four lights carry an instrument here, so forest_b gets a real duty
    # instead of being reserved for bells
    GATE_DUTY={"wheel_a": 0.60, "wheel_b": 0.56, "forest_a": 0.54, "forest_b": 0.50},
    BLOOM_HALF_LIFE={"perc": 0.17, "kick": 0.18, "hit": 0.30, "tick": 0.08,
                     "ding": 1.25, "beat": 0.16, "instr": 0.22},
    BEAT_BLOOM=0.0,                 # the instruments are the beat here
)

# keep the cycle ordered by intensity: base -> mid -> mid-instrumental -> punchy
SCENE_MODES = {k: SCENE_MODES[k]
               for k in ("base", "mid", "mid-instrumental", "punchy")}

BEAT_BLOOM = 0.0            # base: no bloom on the bare beat
INSTRUMENT_MODE = False     # lights follow discovered instruments, not bands
INSTR_BLOOM_GAIN = 0.85
INSTR_PERIOD_BEATS = 16     # how often the top-4 instruments are re-picked
CURRENT_SCENE = "base"
_SCENE_DEFAULTS = {}


def apply_scene_mode(name):
    """Switch the feel knobs between the scene modes. Returns the mode set."""
    global CURRENT_SCENE
    if name not in SCENE_MODES:
        name = "base"
    if not _SCENE_DEFAULTS:
        keys = set()
        for over in SCENE_MODES.values():
            keys |= set(over)
        for k in keys:
            _SCENE_DEFAULTS[k] = globals()[k]
    globals().update(_SCENE_DEFAULTS)      # back to base, then overlay
    globals().update(SCENE_MODES[name])
    CURRENT_SCENE = name
    return name


def next_scene_mode():
    names = list(SCENE_MODES)
    return names[(names.index(CURRENT_SCENE) + 1) % len(names)]


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

        import librosa as _lb
        try:
            harm, perc = _lb.decompose.hpss(S)      # one split, used by both
        except Exception:
            harm = perc = None
        self.events, self.density = self._detect_events(S, freqs, sr, hop, perc)
        self._hop = hop
        self.parts, self.perc_energy = self._discover_parts(S, sr, harm, perc)

        self.n = min(S.shape[1], len(self.loudness), len(self.brightness),
                     len(self.tonal), len(self.section_of),
                     *[len(v) for v in self.energy.values()])

    def _detect_events(self, S, freqs, sr, hop, P=None):
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
        # PERC: any drum. Onsets on the percussive component of an h/p split.
        if P is None:
            _, P = librosa.decompose.hpss(S)
        penv = librosa.onset.onset_strength(S=librosa.power_to_db(P ** 2 + 1e-10),
                                            sr=sr, hop_length=hop)
        pon = np.asarray([f for f in librosa.onset.onset_detect(
            onset_envelope=penv, sr=sr, hop_length=hop, units="frames")
            if 0 <= f < n], dtype=int)
        if len(pon):
            pref = float(np.percentile(penv[pon], 95)) or 1.0
            gate = float(np.quantile(penv[pon], PERC_ACCENT_Q))
            last = -10 ** 9
            for f in pon:
                if penv[f] < gate or (f - last) < PERC_MIN_GAP_S * FPS:
                    continue
                last = f
                events.setdefault(int(f), []).append(("perc", min(1.0, float(penv[f]) / pref)))

        # density: percussive energy, smoothed
        perc = P.sum(axis=0)[:n]
        w = max(1, int(DENSITY_WINDOW_S * FPS))
        density = _norm(np.convolve(perc, np.ones(w) / w, mode="same"))
        return events, density

    def _discover_parts(self, S, sr, Hs=None, Ps=None):
        """Find the recurring tonal parts in THIS track (NMF on the harmonics).

        Returns ([{activation, centroid, contrast}], percussive_energy).
        Nothing here is tied to a named instrument — whatever the song is made
        of becomes the parts, which is what makes the rig adapt per track.
        """
        import librosa
        n = S.shape[1]
        try:
            import warnings
            if Hs is None or Ps is None:
                Hs, Ps = librosa.decompose.hpss(S)
            perc = _norm(np.convolve(Ps.sum(axis=0)[:n], np.ones(5) / 5, mode="same"))
            mel = librosa.feature.melspectrogram(S=Hs ** 2, sr=sr, n_mels=96)
            with warnings.catch_warnings():
                # NMF is approximate here by design; "did not converge in 200
                # iterations" is not actionable and must not clutter the
                # venue terminal.
                warnings.simplefilter("ignore")
                W, H = librosa.decompose.decompose(np.sqrt(np.maximum(mel, 0)),
                                                   n_components=N_PARTS, sort=True,
                                                   random_state=0)
            mel_f = librosa.mel_frequencies(n_mels=96, fmin=0, fmax=sr / 2)
        except Exception:
            return [], _norm(S.sum(axis=0)[:n])
        parts = []
        for j in range(H.shape[0]):
            act = np.asarray(H[j][:n], dtype=float)
            w = np.asarray(W[:, j], dtype=float)
            if w.sum() <= 0 or act.max() <= 0:
                continue
            sm = np.convolve(act, np.ones(12) / 12, mode="same")   # 300 ms
            thr = float(np.quantile(sm, 0.65))
            hi = sm[sm > thr]
            # This part's OWN note starts, so a light can hit exactly with the
            # instrument rather than with the track's global beat grid.
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    on = librosa.onset.onset_detect(onset_envelope=act, sr=sr,
                                                    hop_length=self._hop,
                                                    units="frames")
                on = np.asarray([f for f in on if 0 <= f < n], dtype=int)
                ref = float(np.percentile(act[on], 95)) if len(on) else 1.0
                strengths = (np.clip(act[on] / (ref or 1.0), 0.0, 1.0)
                             if len(on) else np.zeros(0))
            except Exception:
                on, strengths = np.zeros(0, dtype=int), np.zeros(0)
            parts.append({
                "activation": sm,
                "onsets": on,
                "onset_strength": strengths,
                "centroid": float((mel_f * w).sum() / w.sum()),
                # how cleanly it switches on and off, vs sitting at one level
                "contrast": float(hi.mean() / (np.median(sm) + 1e-9)) if len(hi) else 1.0,
            })
        return parts, perc

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


def a2_needs_onsets(a):
    """True if a cached analysis predates per-part onsets."""
    parts = getattr(a, "parts", None) or []
    return bool(parts) and "onsets" not in parts[0]


def cache_file_for(path):
    """Where this track's analysis lives.

    Keyed on file CONTENT, not path/mtime, so a cache built on one machine
    still hits after the songs are copied to the show laptop.
    """
    st = os.stat(path)
    h = hashlib.sha1()
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))          # first 1 MB is plenty to identify it
    h.update(f"{st.st_size}|v{CACHE_VERSION}|{FPS}|{SECTION_SECONDS}|"
             f"{sorted(BANDS.items())}".encode())
    return os.path.join(CACHE_DIR, h.hexdigest()[:16] + ".pkl")


def is_analysed(path):
    try:
        return os.path.exists(cache_file_for(path))
    except OSError:
        return False


def analyse_cached(path, verbose=True):
    """Analyse a track, reusing a cached result when the file is unchanged.

    Analysis costs ~12-18s per track; the show should not pay that at the
    venue. Cache key covers file bytes + size + the tunables that change the
    output.
    """
    cache_file = cache_file_for(path)

    if os.path.exists(cache_file):
        try:
            with open(cache_file, "rb") as f:
                a = pickle.load(f)
            required = ("energy", "flux", "flux_raw", "loudness", "brightness",
                        "tonal", "beat_frames", "frames_per_beat", "section_of", "n",
                        "events", "density", "parts", "perc_energy")
            if a2_needs_onsets(a):
                raise ValueError("parts predate onset detection")
            if all(hasattr(a, attr) for attr in required):
                # the cache is keyed on CONTENT, so this object may have been
                # built from a different path/name (other machine, renamed file)
                a.path, a.name = path, os.path.basename(path)
                if verbose:
                    print(f"  (cached) {os.path.basename(path)}")
                return a
        except Exception:
            pass    # corrupt or stale format — just re-analyse

    a = SongAnalysis(path)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        # Write via a temp file and rename: the prewarm runs in a second
        # process, so two writers can target the same key at once and a
        # half-written pickle would poison the cache.
        tmp = f"{cache_file}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            pickle.dump(a, f, protocol=4)
        os.replace(tmp, cache_file)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        pass        # cache is an optimisation, never fatal
    return a


def prewarm(path, verbose=True):
    """Analyse every track under `path` that has no cache entry yet.

    Run as its own process alongside the show so a long library cannot stall
    playback when the operator skips into a track nobody has played yet.
    """
    _, tracks = scan_library(path)
    missing = [t for t in tracks if not is_analysed(t)]
    if verbose:
        print(f"[prewarm] {len(tracks)} track(s), {len(missing)} need analysing",
              flush=True)
    done = 0
    for t in missing:
        if is_analysed(t):           # the show may have got there first
            continue
        try:
            t0 = time.perf_counter()
            analyse_cached(t, verbose=False)
            done += 1
            if verbose:
                print(f"[prewarm] {done}/{len(missing)}  "
                      f"{time.perf_counter() - t0:5.1f}s  {os.path.basename(t)}",
                      flush=True)
        except Exception as e:
            if verbose:
                print(f"[prewarm] failed {os.path.basename(t)}: {e}", flush=True)
    if verbose:
        print(f"[prewarm] done — {done} analysed, whole library ready", flush=True)
    return done


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
        if INSTRUMENT_MODE:
            for l in LIGHTS:
                self.bloom[l["name"]]["instr"] = 0.0
        if BEAT_BLOOM > 0:                       # punchy: the wheel hits the beat
            for l in LIGHTS:
                if l["zone"] == "wheel":
                    self.bloom[l["name"]]["beat"] = 0.0
        self._decay = {k: 0.5 ** (1.0 / max(1.0, hl * FPS)) for k, hl in BLOOM_HALF_LIFE.items()}
        # Scene modes replace BLOOM_HALF_LIFE wholesale, so a mode's dict may
        # not carry every kind the engine can bloom on. Fall back rather than
        # KeyError halfway through a track.
        for kind, default in (("beat", 0.13), ("instr", 0.30)):
            self._decay.setdefault(kind, 0.5 ** (1.0 / max(1.0, default * FPS)))
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
        # each light's own voice — what decides whether it is leading
        n = analysis.n
        self._voices = {
            "wheel_a":  np.asarray(analysis.density[:n], dtype=float),
            "wheel_b":  np.asarray(analysis.energy["bass"][:n], dtype=float),
            "forest_a": np.asarray(analysis.energy["mids"][:n], dtype=float),
            "forest_b": np.asarray(analysis.energy["highs"][:n], dtype=float),
        }
        self._instr_source, self._instr_onset = {}, {}
        if INSTRUMENT_MODE:
            self._assign_instruments()
        self._arc = {z: tuple(ZONE_ARC[z]) for z in ZONES}
        self._auto_tl = None
        self._auto_now = None
        self._gate = self._build_gates()
        self._role_cap = self._plan_roles()
        self._dropout = self._plan_dropouts()
        self.bright = {l["name"]: float(self._role_cap[l["name"]][0]) for l in LIGHTS}
        # Start the palette where the first section wants it, so a song never
        # opens with a swing from some unrelated colour.
        first = int(analysis.section_of[0])
        self.pos = self.section_t.get(first, 0.3)
        self.hue = {}
        for light in LIGHTS:
            k = self._members[light["zone"]].index(light)
            off = (k - (len(self._members[light["zone"]]) - 1) / 2.0) * INTRA_ZONE_SPREAD
            self.hue[light["name"]] = (zone_hue_at(light["zone"], self.pos) + off) % 1.0
        self._arc = {z: tuple(ZONE_ARC[z]) for z in ZONES}

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

    def _assign_instruments(self):
        """Hand the four lights the four most active instruments of each period.

        Re-picked every INSTR_PERIOD_BEATS, so the lights follow whatever is
        actually playing rather than fixed frequency bands. Within a period the
        four picks are sorted by pitch: the two lowest light the wheel (warm
        zone), the two highest light the forest.
        """
        a, n = self.a, self.a.n
        parts = [p for p in (getattr(a, "parts", None) or [])
                 if len(p.get("activation", ())) >= n and "onsets" in p]
        order = ["wheel_b", "wheel_a", "forest_a", "forest_b"]   # low -> high
        if len(parts) < len(order):
            return                       # not enough instruments; fall back
        src = {nm: np.zeros(n) for nm in order}
        onsets = {nm: {} for nm in order}
        win = max(int(INSTR_PERIOD_BEATS * a.frames_per_beat), 4 * FPS)
        for start in range(0, n, win):
            end = min(n, start + win)
            activity = [(float(p["activation"][start:end].mean()), k)
                        for k, p in enumerate(parts)]
            top = [k for _, k in sorted(activity, reverse=True)[:len(order)]]
            top.sort(key=lambda k: parts[k]["centroid"])          # low -> high
            for nm, k in zip(order, top):
                p = parts[k]
                src[nm][start:end] = p["activation"][start:end]
                sel = (p["onsets"] >= start) & (p["onsets"] < end)
                for f, stg in zip(p["onsets"][sel], p["onset_strength"][sel]):
                    onsets[nm][int(f)] = float(stg)
        # normalise each light's composite source, so the gate solver sees a
        # comparable signal even though it is stitched from different parts
        for nm in order:
            src[nm] = _norm(src[nm])
        self._instr_source, self._instr_onset = src, onsets

    def _gate_source(self, name):
        """The signal that decides whether this light is lit at all."""
        a, n = self.a, self.a.n
        if INSTRUMENT_MODE and name in self._instr_source:
            return self._instr_source[name]         # this light's instrument
        parts = [p for p in (getattr(a, "parts", None) or [])
                 if len(p.get("activation", ())) >= n]
        if name == "wheel_a":                       # the drums
            return np.asarray(getattr(a, "perc_energy", a.energy["bass"])[:n], float)
        if not parts:
            band = {"wheel_b": "bass", "forest_a": "mids", "forest_b": "highs"}[name]
            return np.asarray(a.energy[band][:n], float)
        # tonal lights take the most on/off-ish parts, split low vs high
        ranked = sorted(parts, key=lambda p: -p["contrast"])[:4]
        ranked.sort(key=lambda p: p["centroid"])
        pick = {"wheel_b": ranked[0], "forest_a": ranked[-1]}.get(name)
        return np.asarray(pick["activation"][:n], float) if pick else None

    def _build_gates(self):
        """Solve each light's threshold for its target duty cycle, then shape it.

        Hysteresis plus minimum on/off runs stop a light flickering around the
        threshold; attack/release turn the on/off decision into a fade.
        """
        a, n = self.a, self.a.n
        gates = {}
        for light in LIGHTS:
            nm = light["name"]
            # In instrument mode every light follows a discovered instrument,
            # so forest_b is not reserved for bells.
            if nm == "forest_b" and not INSTRUMENT_MODE:
                gates[nm] = self._ding_gate()
                if gates[nm] is not None:
                    continue
            src = self._gate_source(nm)
            duty = GATE_DUTY.get(nm, 0.4)
            if src is None or duty <= 0 or duty >= 1:
                gates[nm] = np.ones(n)
                continue
            gates[nm] = _envelope(self._solve_gate(src, duty))
        return gates

    def _gate_bool(self, src, q):
        """Threshold at quantile q, with hysteresis and minimum run lengths.

        Hysteresis is vectorised (forward-fill of the last definite decision)
        rather than a per-frame loop: the solver runs this 14 times per light,
        and on a 10-minute track the loop version took over a minute.
        """
        hi = float(np.quantile(src, q))
        lo = hi * GATE_HYSTERESIS
        decided = np.where(src >= hi, 1, np.where(src < lo, 0, -1))
        pos = np.where(decided >= 0, np.arange(len(src)), -1)
        pos = np.maximum.accumulate(pos)                 # last definite decision
        on = np.where(pos >= 0, decided[pos], 0).astype(bool)
        return _min_runs(on, int(GATE_MIN_ON_S * FPS), int(GATE_MIN_OFF_S * FPS))

    def _solve_gate(self, src, duty):
        """Find the threshold that actually yields the target duty cycle.

        Thresholding at quantile(1-duty) does NOT give a duty of `duty`:
        hysteresis and the minimum-run smoothing both fill in gaps, which on a
        near-continuous source (the drums) pushed a 45% target to 87% lit. So
        solve for the quantile whose FINAL gate hits the target.
        """
        lo_q, hi_q, best = 0.02, 0.995, None
        for _ in range(14):
            q = 0.5 * (lo_q + hi_q)
            on = self._gate_bool(src, q)
            best = on
            if on.mean() > duty:
                lo_q = q            # too much light -> raise the threshold
            else:
                hi_q = q
        return best if best is not None else np.ones(len(src), dtype=bool)

    def _ding_gate(self):
        """The bell light: dark except while a ding is ringing."""
        a, n = self.a, self.a.n
        frames = [f for f, evs in a.events.items()
                  if f < n and any(k == "ding" for k, _ in evs)]
        if len(frames) < DING_GATE_MIN:
            return None                       # too few bells — fall back to a part
        on = np.zeros(n, dtype=bool)
        hold = max(1, int(DING_GATE_HOLD_S * FPS))
        for f in frames:
            on[f:min(n, f + hold)] = True
        return _envelope(on)

    def _zone_hue(self, zone, t):
        """Hue for a zone, using the engine's own arc (auto mode morphs it)."""
        start, span = self._arc[zone]
        return (start + max(0.0, min(1.0, t)) * span) % 1.0

    def _classify_sections(self):
        """Give each section a palette from its energy, drive and brightness."""
        a = self.a
        feats, big = {}, {}
        for sec in sorted(set(int(x) for x in a.section_of[:a.n])):
            idx = np.where(a.section_of[:a.n] == sec)[0]
            if len(idx) == 0:
                continue
            # Warm/cool comes from the BAND BALANCE, not spectral brightness:
            # _norm() clips brightness at the 95th percentile, so on a real
            # track every section lands in 0.89-0.96 and the cue is useless
            # (ember never fired). Highs-vs-bass separates warm from airy.
            hi_e = float(a.energy["highs"][idx].mean())
            lo_e = float(a.energy["bass"][idx].mean())
            feats[sec] = (float(a.loudness[idx].mean()),
                          float(a.density[idx].mean()),
                          hi_e / (hi_e + lo_e + 1e-9))
            if len(idx) >= 4 * FPS:            # only real sections set the scale
                big[sec] = feats[sec]
        if not feats:
            return {}
        scale = big or feats

        def spread(k):
            vals = np.array([scale[s][k] for s in scale])
            lo, hi = float(vals.min()), float(vals.max())
            if hi <= lo:
                return {s: 0.5 for s in feats}
            return {s: float(np.clip((feats[s][k] - lo) / (hi - lo), 0.0, 1.0))
                    for s in feats}
        loud, dens, bright = spread(0), spread(1), spread(2)
        out = {}
        for sec in feats:
            energy = 0.60 * loud[sec] + 0.40 * dens[sec]
            if energy < 0.30:
                out[sec] = "base"          # the quiet garden
            elif energy < 0.62:
                out[sec] = "ember" if bright[sec] < 0.5 else "tropical"
            else:
                # NOTE: "ocean" is deliberately NOT in the auto rotation. It
                # puts cool light on the rust-red wheel, which reads muddy on
                # the real wood — fine as a deliberate manual choice, wrong as
                # something the engine picks for you. Add it here if you
                # decide otherwise.
                out[sec] = "neon"
        return out

    def _auto_timeline(self):
        """Per-frame palette choice, with a minimum dwell so it cannot flicker."""
        assign = self._classify_sections()
        n = self.a.n
        secs = self.a.section_of[:n]
        tl = [assign.get(int(secs[i]), "base") for i in range(n)]
        min_len = int(AUTO_PALETTE_DWELL_S * FPS)
        i = 0
        while i < n:
            j = i
            while j < n and tl[j] == tl[i]:
                j += 1
            if j - i < min_len:
                # too brief to be seen as a change: absorb it into a neighbour.
                # The FIRST run has no predecessor, so it takes the next run's
                # palette instead (otherwise a 1 s opening section slipped
                # through the dwell rule).
                fill = tl[i - 1] if i > 0 else (tl[j] if j < n else tl[i])
                for k in range(i, j):
                    tl[k] = fill
            i = j
        return tl

    def _plan_roles(self):
        """Rank the voices each phrase and hand out lead / main / complement."""
        a, n = self.a, self.a.n
        names = [l["name"] for l in LIGHTS]
        # Prominence as each voice's own percentile rank, NOT a z-score: a
        # z-score divides by the voice's spread, so a peaky band (the highs)
        # scores high constantly and hogs the lead. Percentile rank puts every
        # voice on the same 0..1 footing, so leadership rotates on merit.
        prom = {}
        for nm, v in self._voices.items():
            order = np.argsort(np.argsort(v))
            prom[nm] = order / max(1, len(v) - 1)
        # rarely-lit lights sit out the ranking and always run at full
        exempt = [nm for nm in names
                  if float((self._gate[nm] > 0.5).mean()) < ROLE_EXEMPT_DUTY]
        names = [nm for nm in names if nm not in exempt]
        win = max(int(ROLE_PHRASE_BEATS * a.frames_per_beat), FPS)
        spans = [(s0, min(n, s0 + win)) for s0 in range(0, n, win)]
        # Score every phrase per voice, then normalise each voice ACROSS
        # phrases. A smooth voice (the mids) has a narrower spread of phrase
        # averages than a spiky one, so without this it almost never wins the
        # lead even when it is carrying the song.
        raw = {nm: np.array([prom[nm][s0:e0].mean() for s0, e0 in spans]) for nm in names}
        score = {nm: (v - v.mean()) / (v.std() or 1.0) for nm, v in raw.items()}
        caps = {nm: np.full(n, ROLE_LEVELS["main"]) for nm in names}
        for nm in exempt:
            caps[nm] = np.full(n, ROLE_LEVELS["lead"])
        for k, (s0, e0) in enumerate(spans):
            order = sorted(names, key=lambda nm: -float(score[nm][k]))
            for rank, nm in enumerate(order):
                caps[nm][s0:e0] = ROLE_LEVELS[ROLE_TIERS[min(rank, len(ROLE_TIERS) - 1)]]
        return caps

    def _plan_dropouts(self):
        """Dip a light just before its own voice surges; restore on the hit."""
        n = self.a.n
        names = [l["name"] for l in LIGHTS]
        mult = {nm: np.ones(n) for nm in names}
        if not ANTICIPATE:
            return mult
        lead = max(1, int(ANTICIPATION_LEAD_S * FPS))
        w = max(1, int(1.0 * FPS))
        box = np.ones(w) / w
        cand = []
        for nm in names:
            v = np.convolve(self._voices[nm], box, mode="same")
            rise = np.zeros(n)
            rise[:max(0, n - w)] = v[w:] - v[:max(0, n - w)]   # surge starting here
            thr = float(np.quantile(rise, ANTICIPATION_Q))
            peak = _local_max(rise, w)                     # vectorised
            idx = np.where((rise >= thr) & peak)[0]
            idx = idx[(idx >= lead) & (idx < max(lead, n - w))]
            last = -10 ** 9
            for i in idx:
                if i - last < ANTICIPATION_MIN_GAP_S * FPS:
                    continue
                last = i
                cand.append((int(i), nm, float(rise[i])))
        cand.sort(key=lambda c: -c[2])                         # strongest surges win
        taken = []
        for change, nm, _ in cand:
            start, end = change - lead, change
            if any(not (end <= s2 or start >= e2) for s2, e2 in taken):
                continue                                       # never two lights out at once
            taken.append((start, end))
            mult[nm][start:end] = np.linspace(1.0, ANTICIPATION_FLOOR, end - start)
        return mult

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

        # --- palette: fixed, or auto-morphing between per-section choices ---
        auto_sat = None
        if CURRENT_PALETTE == "auto":
            if self._auto_tl is None:
                self._auto_tl = self._auto_timeline()
            want = self._auto_tl[i]
            if want != self._auto_now:
                self._auto_now = want
            target = PALETTES[want]["arc"]
            auto_sat = PALETTES[want]["sat"]
            for z in ZONES:                    # crossfade the arc, don't snap
                cs, cp = self._arc[z]
                ts, tp = target[z]
                self._arc[z] = (_hue_lerp(cs, ts, AUTO_ARC_MORPH),
                                cp + (tp - cp) * AUTO_ARC_MORPH)
        else:
            self._arc = {z: tuple(ZONE_ARC[z]) for z in ZONES}
        zone_hue = {z: self._zone_hue(z, self.pos) for z in ZONES}

        # --- per light: envelope + blooms + hue slot ----------------------
        dims, sats = {}, {}
        for light in LIGHTS:
            name, band, zone = light["name"], light["band"], light["zone"]
            feel = ZONE_FEEL[zone]
            # In instrument mode every light hits exactly with its own
            # instrument, so nothing is lagged off the global beat grid.
            lag = 0.0 if (INSTRUMENT_MODE and name in self._instr_source) \
                else light["lag_beats"] * a.frames_per_beat
            j = int(i - lag)                   # this light's (lagged) time

            # 1. body: smoothed band energy, speed follows musical density
            if INSTRUMENT_MODE and name in self._instr_source:
                e = self._sample(self._instr_source[name], i, lag)
            else:
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
            if "instr" in bl:
                stg = self._instr_onset.get(name, {}).get(j)
                if stg:
                    bl["instr"] = min(1.0, bl["instr"] + stg * INSTR_BLOOM_GAIN)
            if "beat" in bl and j in a.beat_frames:
                bl["beat"] = min(1.0, bl["beat"] + BEAT_BLOOM)
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

            # brightness layer: gate (is this light's part playing at all?)
            # x phrase role (crossfaded) x anticipation dip
            self.bright[name] += (float(self._role_cap[name][i]) - self.bright[name]) * ROLE_FADE
            b = self.bright[name] * float(self._dropout[name][i]) * float(self._gate[name][i])
            dims[name] = (feel["floor"] + (1.0 - feel["floor"]) * level ** DIM_GAMMA) * b

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
            pal_sat = auto_sat if CURRENT_PALETTE == "auto" else PALETTE_SAT
            lo_s, hi_s = pal_sat if pal_sat else feel["sat"]
            sats[name] = float(np.clip(raw_sat, lo_s, hi_s)) * (1.0 - DING_SHINE * ding)

        # --- a zone is never allowed to go dark (see ZONE_SAFETY_FLOOR) ----
        for z in ZONES:
            fl = ZONE_SAFETY_FLOOR.get(z, 0.0)
            if fl <= 0:
                continue
            mem = [l["name"] for l in LIGHTS if l["zone"] == z]
            mx = max(dims[n] for n in mem)
            if mx <= 1e-6:
                for nm in mem:
                    dims[nm] = fl
            elif mx < fl:
                k = fl / mx
                for nm in mem:
                    dims[nm] = min(1.0, dims[nm] * k)

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


def _local_max(x, w):
    """Points that are the maximum over a symmetric window of +-(w//2).

    Window size is pinned to 2*(w//2)+1 so the scipy path and the fallback
    agree exactly; leaving it as `w` made the two disagree on odd widths.
    """
    size = 2 * max(1, w // 2) + 1
    try:
        from scipy.ndimage import maximum_filter1d
        return x >= maximum_filter1d(x, size=size, mode="nearest") - 1e-12
    except Exception:
        pad = size // 2
        best = np.full(len(x), -np.inf)
        for off in range(-pad, pad + 1):          # w passes, not n*w slices
            shifted = np.roll(x, off)
            if off > 0:
                shifted[:off] = -np.inf
            elif off < 0:
                shifted[off:] = -np.inf
            best = np.maximum(best, shifted)
        return x >= best - 1e-12


def _min_runs(on, min_on, min_off):
    """Remove on/off runs shorter than the minimum, so a gate cannot chatter."""
    out = np.asarray(on, dtype=bool).copy()
    n = len(out)
    i = 0
    while i < n:
        j = i
        while j < n and out[j] == out[i]:
            j += 1
        need = min_on if out[i] else min_off
        if j - i < need and not (i == 0 and j >= n):
            out[i:j] = not out[i]           # too short — absorb into its neighbour
        i = j
    return out


def _envelope(on, attack_s=None, release_s=None):
    """Turn a boolean gate into a smooth 0..1 envelope."""
    attack_s = GATE_ATTACK_S if attack_s is None else attack_s
    release_s = GATE_RELEASE_S if release_s is None else release_s
    ka = 1.0 - 0.5 ** (1.0 / max(1.0, attack_s * FPS))
    kr = 1.0 - 0.5 ** (1.0 / max(1.0, release_s * FPS))
    out = np.zeros(len(on), dtype=float)
    v = 0.0
    for i, want in enumerate(on):
        target = 1.0 if want else 0.0
        v += (target - v) * (ka if target > v else kr)
        out[i] = v
    return out


def _blend_values(a, b, t):
    """Crossfade two frames of light values (t=0 -> a, t=1 -> b)."""
    t = max(0.0, min(1.0, t))
    out = {}
    for name, va in a.items():
        vb = b[name]
        out[name] = {k: va[k] + (vb[k] - va[k]) * t for k in va}
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

    def __init__(self, ip=None, port=None, universe=None, simulate=False, mirror=()):
        self.ip = ip or ARTNET_IP
        self.port = port or ARTNET_PORT
        self.universe = ARTNET_UNIVERSE if universe is None else universe
        self.simulate = simulate
        # Extra destinations for the same frames. Used to feed rig_preview.py
        # during a live show: Daslight holds 6454 exclusively, so the preview
        # cannot sniff that stream — it gets its own copy instead.
        targets = [(self.ip, self.port)] + [tuple(m) for m in mirror]
        seen, self.targets = set(), []
        for t in targets:                     # a mirror pointed at the main
            if t not in seen:                 # target would double every frame
                seen.add(t)
                self.targets.append(t)
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
        packet = self._packet(dmx)            # one packet, same sequence number
        for target in self.targets:
            try:
                self.sock.sendto(packet, target)
            except OSError:
                pass                          # a dead preview must not stop the show

    def blackout(self):
        if self.simulate:
            return
        for _ in range(3):                    # UDP: send a few, none are acked
            packet = self._packet(bytearray(512))
            for target in self.targets:
                try:
                    self.sock.sendto(packet, target)
                except OSError:
                    pass
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
    y = sr = None
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
    paused = False
    pause_mix = 0.0
    idle = idle_values()
    values = dict(idle)
    try:
        while True:
            while not keys.empty():
                k = keys.get_nowait()
                if k in ("n", "q"):
                    result = "next" if k == "n" else "quit"
                elif k.startswith("palette:"):
                    # no rebuild needed: the arcs are read live, so the
                    # colours simply drift across to the new palette
                    apply_palette(k.split(":", 1)[1])
                    print(f"\n  palette: {CURRENT_PALETTE.upper()}"
                          f"  ({PALETTES[CURRENT_PALETTE]['note']})")
                elif k.startswith("mode:"):
                    want = k.split(":", 1)[1]
                    if want != CURRENT_SCENE:
                        out.send_frame(values)      # hold while we rebuild
                        apply_scene_mode(want)
                        engine = ShowEngine(analysis)
                        print(f"\n  scene mode: {CURRENT_SCENE.upper()}")
                elif k == "p":
                    # sounddevice has no pause: stop, remember the position,
                    # and resume from that exact sample so the frame clock and
                    # the music stay locked together.
                    paused = not paused
                    pause_mix = 0.0
                    pos = time.perf_counter() - t0
                    if paused:
                        if player is not None:
                            player.stop()
                        print(f"\r  paused at {pos:5.1f}s  —  [p] resume, [n] next, [q] quit   ",
                              end="", flush=True)
                    else:
                        if player is not None and y is not None:
                            start = min(len(y), max(0, int(pos * sr)))
                            player.play(y[start:], sr)
                        t0 = time.perf_counter() - pos
                        print("\r  resumed                                              ",
                              end="", flush=True)
            if result != "done":
                break
            if paused:
                # Settle into the resting look rather than freezing whatever
                # frame we landed on — pausing during a dark passage would
                # otherwise leave the garden black for the whole pause.
                pause_mix = min(1.0, pause_mix + 1.0 / (1.5 * FPS))
                out.send_frame(_blend_values(values, idle, pause_mix))
                time.sleep(1.0 / FPS)
                continue
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


def _library_reply(state):
    """A compact listing of folders and songs for the preview's browser."""
    folders, tracks = scan_library(state.root)
    lines = ["LIST", f"FOLDER\tall\t{len(tracks)}\t{'*' if not state.folder or state.folder=='all' else ''}"]
    for f in folders:
        n = len([t for t in tracks
                 if t.startswith(os.path.join(state.root, f) + os.sep)])
        lines.append(f"FOLDER\t{f}\t{n}\t{'*' if state.folder == f else ''}")
    shown = scan_tracks(state.root, state.folder)[:300]     # bound the datagram
    for t in shown:
        rel = os.path.relpath(t, state.root)
        mark = "*" if t == state.current else ""
        lines.append(f"SONG\t{rel}\t{os.path.basename(t)}\t{mark}")
    return "\n".join(lines)


def control_listener(q, port, state=None):
    """Accept 'next' / 'quit' over UDP — the same as pressing [n] / [q].

    rig_preview.py's buttons talk to this, so the show can be driven from the
    window you are already looking at, even from another machine.
    """
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
    except OSError as e:
        print(f"  (control port {port} unavailable: {e} — keyboard only)")
        return
    while True:
        try:
            data, src = s.recvfrom(256)
        except OSError:
            return
        raw = data.decode(errors="ignore").strip()
        cmd = raw.lower()
        if cmd == "list" and state is not None:
            try:
                s.sendto(_library_reply(state).encode(), src)
            except OSError:
                pass
            continue
        if cmd.startswith("folder ") and state is not None:
            want = raw.split(None, 1)[1].strip()
            state.folder = None if want.lower() == "all" else want
            state.pending_play = None
            print(f"\n  [control] set list: {state.folder or 'all songs'}")
            try:
                s.sendto(f"folder={state.folder or 'all'}".encode(), src)
            except OSError:
                pass
            q.put("n")                     # jump straight into the new set
            continue
        if cmd.startswith("play ") and state is not None:
            state.pending_play = raw.split(None, 1)[1].strip()
            try:
                s.sendto(f"play={state.pending_play}".encode(), src)
            except OSError:
                pass
            q.put("n")
            continue
        if cmd in ("n", "next", "skip"):
            print(f"\n  [control] next  (from {src[0]})")
            q.put("n")
        elif cmd.startswith("palette") or cmd.startswith("colour") or cmd.startswith("color"):
            want = cmd.split(None, 1)[1].strip() if " " in cmd else next_palette()
            q.put("palette:" + want)
            try:
                s.sendto(f"palette={want}".encode(), src)
            except OSError:
                pass
        elif cmd.startswith("mode"):
            want = cmd.split(None, 1)[1].strip() if " " in cmd else next_scene_mode()
            if want not in SCENE_MODES:
                try:
                    s.sendto(f"unknown mode '{want}'; have: "
                             f"{', '.join(SCENE_MODES)}".encode(), src)
                except OSError:
                    pass
                continue
            q.put("mode:" + want)
            try:
                s.sendto(f"mode={want}".encode(), src)   # tell the caller
            except OSError:
                pass
        elif cmd in ("p", "pause", "play", "toggle"):
            print(f"\n  [control] pause/resume  (from {src[0]})")
            q.put("p")
        elif cmd in ("q", "quit", "stop"):
            print(f"\n  [control] stop  (from {src[0]})")
            q.put("q")


def key_listener(q):
    try:
        import msvcrt
        while True:
            ch = msvcrt.getch().decode(errors="ignore").lower()
            if ch in ("n", "q", "p"):
                q.put(ch)
            elif ch == "m":
                q.put("mode:" + next_scene_mode())
            elif ch == "c":
                q.put("palette:" + next_palette())
    except ImportError:
        import select
        while True:
            if select.select([sys.stdin], [], [], 0.3)[0]:
                ch = sys.stdin.readline().strip().lower()
                if ch in ("n", "q", "p"):
                    q.put(ch)
                elif ch == "m":
                    q.put("mode:" + next_scene_mode())
                elif ch == "c":
                    q.put("palette:" + next_palette())


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------
def scan_library(root):
    """(folders, tracks) for everything under root, recursively.

    A subfolder of songs/ is a set list: pick one and the show loops only
    that folder. Re-read between tracks, so songs and folders added mid-show
    are picked up without restarting.
    """
    if os.path.isfile(root):
        return [], [root]
    tracks, folders = [], set()
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for f in sorted(filenames):
                if f.lower().endswith(AUDIO_EXTS) and not f.startswith("."):
                    tracks.append(os.path.join(dirpath, f))
                    rel = os.path.relpath(dirpath, root)
                    if rel not in (".", os.curdir):
                        folders.add(rel)
    except OSError:
        return [], []
    return sorted(folders), sorted(tracks)


def scan_tracks(path, folder=None):
    """Tracks in the active set list — everything, or just one subfolder."""
    _, tracks = scan_library(path)
    if folder and folder != "all":
        prefix = os.path.join(path, folder) + os.sep
        tracks = [t for t in tracks if t.startswith(prefix)]
    return tracks


class ShowState:
    """What the control port needs to answer questions and steer the show."""

    def __init__(self, root):
        self.root = root
        self.folder = None        # None/"all" = the whole library
        self.tracks = []
        self.current = None
        self.pending_play = None  # a track the operator picked


def collect_tracks(path, shuffle=False):
    files = scan_tracks(path)
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


def _install_term_handler():
    """Make SIGTERM behave like Ctrl+C, so a killed/closed show still blacks out.
    Art-Net is stateless: without this the fixtures freeze on their last frame."""
    import signal

    def _raise(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _raise)
        if hasattr(signal, "SIGHUP"):          # terminal window closed (not on Windows)
            signal.signal(signal.SIGHUP, _raise)
    except (ValueError, OSError):
        pass                                    # not main thread / unsupported


def main():
    global DASLIGHT_PORT, DASLIGHT_IP
    _install_term_handler()
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
    p.add_argument("--preview-port", type=int, default=0,
                   help="Also send every frame to 127.0.0.1 on this port, for rig_preview.py")
    p.add_argument("--control-port", type=int, default=CONTROL_PORT,
                   help="UDP port that accepts 'next'/'quit' (rig_preview buttons)")
    p.add_argument("--shuffle", action="store_true")
    p.add_argument("--palette", choices=list(PALETTES), default="base",
                   help="colour palette; base is the surface-aware one")
    p.add_argument("--scene", choices=list(SCENE_MODES), default="base",
                   help="base = the garden show; mid = lively pop/rock; "
                        "punchy = dancefloor (beat strobes)")
    p.add_argument("--no-loop", action="store_true",
                   help="Stop after the last track (default: loop the set list)")
    p.add_argument("--simulate", action="store_true", help="No hardware, draw in terminal")
    p.add_argument("--learn", metavar="ADDR")
    p.add_argument("--map-list", action="store_true", help="Print all OSC addresses to map")
    p.add_argument("--artnet-test", action="store_true",
                   help="Walk each fixture through R/G/B/W to prove Art-Net works")
    p.add_argument("--prewarm", action="store_true",
                   help="Analyse every track that has no cache entry, then exit")
    p.add_argument("--artnet-discover", action="store_true",
                   help="Broadcast ArtPoll and list any Art-Net nodes that answer")
    p.add_argument("--ip", default=DASLIGHT_IP)
    p.add_argument("--port", type=int, default=DASLIGHT_PORT)
    args = p.parse_args()

    DASLIGHT_IP, DASLIGHT_PORT = args.ip, args.port
    apply_scene_mode(args.scene)
    apply_palette(args.palette)

    if args.prewarm:
        prewarm(args.path)
        return
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
    print(f"Scene mode: {CURRENT_SCENE.upper()}  ([m] switches)")
    print(f"Palette:    {CURRENT_PALETTE.upper()}  ([c] switches)")
    _folders, _all = scan_library(args.path)
    if _folders:
        print(f"Set lists:  {', '.join(_folders)}  (pick one in the preview)")
    print(f"{len(tracks)} track(s) queued. [n] next  [p] pause  [m] mode  [c] colour  [q] quit"
          + ("" if args.no_loop else "  (looping)"))

    if args.mode == "artnet":
        mirror = [("127.0.0.1", args.preview_port)] if args.preview_port else []
        out = ArtNetOut(ip=args.ip, port=args.artnet_port,
                        universe=args.artnet_universe, simulate=args.simulate,
                        mirror=mirror)
        print(f"Output: Art-Net -> {args.ip}:{args.artnet_port} universe "
              f"{args.artnet_universe} (raw DMX, no Daslight mapping needed)")
        if mirror:
            print(f"        mirrored -> 127.0.0.1:{args.preview_port} (rig preview)")
    else:
        out = OSCOut(mode=args.mode, simulate=args.simulate)
        print(f"Output: OSC -> {args.ip}:{args.port} (needs Map OSC in Daslight)")
    state = ShowState(args.path)
    keys = queue.Queue()
    threading.Thread(target=key_listener, args=(keys,), daemon=True).start()
    threading.Thread(target=control_listener, args=(keys, args.control_port, state),
                     daemon=True).start()
    print(f"Control: UDP {args.control_port} accepts 'next' / 'quit' (rig_preview buttons)")

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

    loop = not args.no_loop
    played, failed, last_path = 0, set(), None
    while True:
        # --- pick up songs added to / removed from the folder mid-show -----
        found = scan_tracks(args.path, state.folder)
        if set(found) != set(tracks):
            added = [t for t in found if t not in tracks]
            removed = [t for t in tracks if t not in found]
            tracks = [t for t in tracks if t in found]
            if added:
                if args.shuffle:
                    random.shuffle(added)
                    tracks.extend(added)
                else:
                    tracks.extend(added)
                    tracks.sort()
                for t in added:
                    print(f"  + added: {os.path.basename(t)}")
            for t in removed:
                print(f"  - removed: {os.path.basename(t)}")
                failed.discard(t)
                ahead.pop(t, None)
        if not tracks:
            print("  songs folder is empty — waiting for a track ...")
            out.send_frame(idle_values())
            time.sleep(2.0)
            if not keys.empty() and keys.get_nowait() == "q":
                break
            continue

        # --- an explicitly picked song wins over the running order --------
        forced = None
        if state.pending_play:
            want = state.pending_play
            state.pending_play = None
            cand = want if os.path.isabs(want) else os.path.join(args.path, want)
            cand = os.path.normpath(cand)
            if cand in tracks:
                forced = cand
            else:                       # picked from another set list: widen
                allf = scan_tracks(args.path, None)
                if cand in allf:
                    state.folder = None
                    tracks = allf
                    forced = cand
                else:
                    print(f"  (cannot play {want}: not in the library)")

        # --- advance by identity, so the position survives a changed list --
        if forced:
            idx = tracks.index(forced)
        elif last_path in tracks:
            i = tracks.index(last_path)
            if i + 1 >= len(tracks):
                if not loop:
                    break
                idx = 0
                print(f"\n--- end of set list, looping (played {played}) ---")
            else:
                idx = i + 1
        else:
            idx = 0
        track = tracks[idx]
        last_path = track
        state.tracks, state.current = tracks, track

        item = ahead.pop(track, None)
        if item is None:
            print(f"Preparing {os.path.basename(track)} ...")
            preload(track)
            item = ahead.pop(track)
        if isinstance(item, Exception):
            print(f"  skipping {os.path.basename(track)}: {item}")
            failed.add(track)
            if len(failed) >= len(tracks):
                print("  no playable tracks left.")
                break
            continue
        a, audio = item
        nxt = tracks[(idx + 1) % len(tracks)] if (loop or idx + 1 < len(tracks)) else None
        if nxt is not None and nxt != track and nxt not in ahead:
            threading.Thread(target=preload, args=(nxt,), daemon=True).start()
        played += 1
        if play_song(a, out, keys, simulate=args.simulate,
                     audio=want_audio, audio_data=audio) == "quit":
            break

    out.blackout()
    print("Show finished. Lights blacked out.")


if __name__ == "__main__":
    main()
