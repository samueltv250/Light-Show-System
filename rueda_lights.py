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
CACHE_VERSION = 11

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

# v2 of the instrument mode: same idea, tighter to the instrument. Kept as a
# separate mode so the original stays available to compare against.
SCENE_MODES["mid-instrumental-v2"] = dict(
    SCENE_MODES["mid-instrumental"],
    INSTRUMENT_V2=True,
    INSTR_BLOOM_GAIN=1.00,
    # the body tracks the instrument, so it must rise fast
    ZONE_FEEL={
        "wheel":  dict(attack=(0.45, 0.75), release=(0.12, 0.30),
                       floor=0.02, sat=(0.58, 1.00), strobe=True),
        "forest": dict(attack=(0.38, 0.66), release=(0.10, 0.24),
                       floor=0.03, sat=(0.52, 0.92), strobe=False),
    },
    BLOOM_HALF_LIFE={"perc": 0.17, "kick": 0.18, "hit": 0.30, "tick": 0.08,
                     "ding": 1.25, "beat": 0.16, "instr": 0.16},
    GATE_ATTACK_S=0.03,
    GATE_RELEASE_S=0.30,
)

# ---------------------------------------------------------------------------
# FABLE-MODE — structure-aware, beat-locked choreography
# ---------------------------------------------------------------------------
# Everything above reacts to the music as it happens. A programmed show at a
# festival does something more: it KNOWS the song — where the drop is, when
# the build starts, which bar the chorus lands on — and choreographs toward
# it. We analyse the whole track before it plays, so we know it too.
#
# Fable-Mode sits on top of mid-instrumental-v2 (each light still follows its
# own instrument and lands on its attacks) and adds:
#   * a BEAT GRID with downbeats and 16-beat phrases — movement is locked to
#     the bar, not just the beat, and cues commit on the downbeat
#   * STRUCTURE TIERS per section (breakdown / groove / peak), each with its
#     own figure: breakdown breathes at tempo, zones in antiphase; groove
#     runs a four-light chase whose figure changes every phrase; peak rocks
#     the wheel beat by beat while the forest answers on the off-beat
#   * BUILDS and DROPS found by look-ahead: the bars of rising loudness
#     before a step up are the build — the chase doubles in rate, the colour
#     sweeps to the loud end of the arc, saturation whitens — then half a
#     beat of black, then the drop: all four at full, a strobe burst on the
#     wheel, and the palette snaps on that exact frame
#   * IMPACTS: the track's biggest transients hit all four lights at once
#   * envelope speed that follows the tier, and an OUTPUT LEAD so the rig
#     is not a DMX refresh behind the speakers
# The safety rules are unchanged: strobe caps, forest never strobes, forest
# floor holds even through the pre-drop gap.
FABLE_MODE = False
FABLE_PHRASE_BEATS = 16          # a phrase = 4 bars
FABLE_PULSE_HALF = 0.35          # figure pulse half-life, in beats
FABLE_TIER_THRESH = (0.30, 0.62) # section energy -> breakdown | groove | peak
FABLE_TIER_GAIN = {"breakdown": 0.0, "groove": 0.55, "peak": 0.92}   # figure strength
FABLE_TIER_SPEED = {"breakdown": 0.70, "groove": 1.00, "peak": 1.35}  # envelope speed x
FABLE_PATTERN_LOUD = 0.35        # the figure fades out when loudness drops under this
FABLE_PEAK_MAX_SHARE = 0.50      # at most this much of a track is "peak" (contrast survives)
# The beat reads through DARKNESS between hits, not just brightness on them:
# in a figure the lights that are not on the hit are ducked to this fraction
# of their instrument level. Breakdown never ducks.
FABLE_DUCK = {"groove": 0.75, "peak": 0.55}
FABLE_BREATHE = 0.24             # breakdown: depth of the tempo-locked breath
FABLE_BREATHE_BARS = 2           # ...one breath every N bars
FABLE_DROP_MIN_JUMP = 0.16       # loudness step (2 bars after - 2 bars before) = a drop
FABLE_DROP_MIN_AFTER = 0.45      # ...and it must land somewhere loud
FABLE_DROP_MIN_GAP_BARS = 8
FABLE_BUILD_MIN_BARS = 1         # even a hard cut gets a bar of tension
FABLE_BUILD_MAX_BARS = 8
FABLE_BUILD_WHITEN = 0.40        # saturation pulled toward white at the top of a build
FABLE_DROP_GAP_BEATS = 0.5       # the silence before the drop (wheel black, forest at floor)
FABLE_DROP_HOLD_BEATS = 1.0      # all four at full for this long after a drop
FABLE_IMPACT_Q = 0.995           # loudness jumps in this top slice are impacts
FABLE_IMPACT_MIN_GAP_S = 3.0
FABLE_IMPACT_GAIN = 0.85
FABLE_IMPACT_WHITEN = 0.45       # an impact pulls the colour toward white, like a ding
FABLE_HUE_SWAP = True            # peak: the pair swaps hue every phrase (colour crosses the zone)
FABLE_CHASE_ORDER = ("wheel_a", "forest_a", "wheel_b", "forest_b")   # across the garden
# ZONE CHOREOGRAPHY. The wheel and the forest are two separate pictures a
# few metres apart; both beating all night is noise. Every track opens with
# BOTH zones dark, one zone comes in alone, and from then on each phrase
# hands the zones a pair of states — beat / solid glow / rest — so the show
# moves between "both beating", "one beating while the other holds a glow"
# (kept a little longer) and "one resting". Switches land on the downbeat
# and crossfade. A build or a drop always brings both zones in.
# "rest" for the forest means its safety floor, never black.
FABLE_OPEN_DARK_BARS = 2         # both zones dark at the top of every track
FABLE_OPEN_SOLO_BARS = 6         # ...then the lead zone alone for at least this
FABLE_ZONE_OPTIONS = {           # (wheel, forest, phrases, weight) per tier
    # 0 = rest, 1 = solid glow, 2 = beat
    "peak":      [((2, 2), 1, 50), ((2, 1), 2, 20), ((1, 2), 2, 20), ((2, 0), 1, 5), ((0, 2), 1, 5)],
    "groove":    [((2, 2), 1, 25), ((2, 1), 2, 25), ((1, 2), 2, 25), ((2, 0), 1, 8), ((0, 2), 1, 8),
                  ((1, 1), 1, 9)],
    "breakdown": [((1, 1), 1, 30), ((1, 0), 1, 15), ((0, 1), 1, 15), ((2, 1), 1, 15), ((1, 2), 1, 15),
                  ((2, 0), 1, 5), ((0, 2), 1, 5)],
}
FABLE_ZONE_ON_S = (0.35, 0.70)   # crossfade in / out of a rest
FABLE_ZONE_BEAT_S = (0.40, 0.40) # crossfade between beating and solid
FABLE_SOLID_MIN = 0.30           # a solid glow never sits under this level
# Frames are sent this far AHEAD of the music. Art-Net -> Daslight -> USB DMX
# costs roughly one DMX refresh (~23 ms) before the LEDs move, so the rig
# reads a hair late otherwise. Tune at the rig; 0 = off.
OUTPUT_LEAD_S = 0.0

SCENE_MODES["fable"] = dict(
    SCENE_MODES["mid-instrumental-v2"],
    FABLE_MODE=True,
    OUTPUT_LEAD_S=0.025,
    BLOOM_HALF_LIFE={"perc": 0.17, "kick": 0.18, "hit": 0.30, "tick": 0.08,
                     "ding": 1.25, "beat": 0.16, "instr": 0.16, "impact": 0.35},
    # the figures carry the beat; no extra bloom on the bare beat
    BEAT_BLOOM=0.0,
    # strobe: accents on the biggest transients, plus one burst on every
    # drop. Measured ~25-35 bursts/min on dance tracks (mid ~23, punchy ~70).
    STROBE_PERCENTILE=98.0,
    STROBE_MIN_GAP_S=0.80,
    STROBE_HOLD_S=0.12,
    STROBE_LEVEL=0.38,
    STROBE_MAX_PER_MIN=40,
    STROBE_MIN_ENERGY=0.45,
    DIM_GAMMA=1.60,
    HUE_SMOOTH=0.04,
    ANTICIPATION_MIN_GAP_S=12.0,
)

# ---------------------------------------------------------------------------
# FABLE-2 — Fable-Mode that listens to the WORDS
# ---------------------------------------------------------------------------
# Every track is transcribed (faster-whisper, word timestamps) into the same
# cache the analysis lives in, by the prewarm process and the preload thread.
# In this mode each new word REPAINTS one light: the word's colour is taken
# from the word itself (a hash along the zone's own arc, so the chorus word
# comes back in the same colour every time it is sung), on a light that
# rotates through whichever zone is currently on, with a small glint of
# saturation on the word. The colour then washes back to the song's over
# WORD_HOLD_S, so the words paint and the song washes. Everything else is
# Fable-Mode. The transcript is advisory: a wrong word gives a
# wrong-but-consistent colour, which nobody can tell.
LYRICS_MODE = False
WORDS_MODEL = "base"      # faster-whisper size: base ~11 s/track, small ~35 s (better words)
WORDS_VERSION = 2         # bump when the transcript format changes
# Filtering happens at LOAD, not at transcription, so these are live knobs:
# drop words whisper is unsure of, and words from segments it thinks are not
# speech at all. Measured: Beethoven 5 yields 0 words at any setting, Daft
# Punk's vocoded "around the world" needs no-speech up to 0.95 (it sits at
# 0.8), Viva La Vida keeps ~170 of 242 words.
WORDS_MIN_PROB = 0.30
WORDS_MAX_NOSPEECH = 0.95
WORD_MIX = 0.85           # how far a word pulls its light along the arc (0 = ignore words)
WORD_HOLD_S = 4.0         # the word's colour washes back to the song's over this long
WORD_MIN_GAP_S = 0.20     # never repaint faster than this, whole rig (fast rap ~5 words/s)
WORD_GLINT = 0.30         # saturation dip on the word itself, fades in ~0.4 s

SCENE_MODES["fable-2"] = dict(SCENE_MODES["fable"], LYRICS_MODE=True)

# keep the cycle ordered by intensity: base -> mid -> mid-instrumental -> punchy -> fable
SCENE_MODES = {k: SCENE_MODES[k]
               for k in ("base", "mid", "mid-instrumental",
                         "mid-instrumental-v2", "punchy", "fable", "fable-2")}

BEAT_BLOOM = 0.0            # base: no bloom on the bare beat
INSTRUMENT_MODE = False     # lights follow discovered instruments, not bands
INSTRUMENT_V2 = False       # tighter: near-raw body, backtracked onsets
# v2 body smoothing, in frames (25 ms each). Lower tracks the instrument's
# articulation more exactly but moves more: measured on the test track,
# 3 -> 79.2% of notes hit within 75 ms at 5.26 moves/s, 7 -> 77.8% at 4.68,
# 9 -> 77.6% at 4.59. 7 keeps nearly all the accuracy for a calmer rig.
INSTR_BODY_SMOOTH_FRAMES = 7
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

        try:
            y, sr = librosa.load(path, sr=22050, mono=True)
        except Exception as e:
            raise RuntimeError(decode_hint(path, e)) from None
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
            # v2 extras. The existing fields above are left exactly as they
            # are, so mid-instrumental and every other mode are unchanged.
            #  - a TIGHT envelope (75 ms, not 300 ms) so the body can track
            #    the instrument's articulation instead of a blur
            #  - onsets BACKTRACKED to the attack rather than the detection
            #    peak, which is what made the late tail (p90 lag 375 ms)
            #  - each part normalised to ITSELF, so a quiet instrument does
            #    not leave its light dim for a whole period
            # Store the RAW activation, normalised to itself, and smooth it
            # in the engine: the smoothing width is then a live knob
            # (INSTR_BODY_SMOOTH_FRAMES) instead of something baked into the
            # cache that costs a full re-analysis to retune.
            ref_r = float(np.percentile(act, 97)) or float(act.max()) or 1.0
            raw_n = np.clip(act / ref_r, 0.0, 1.0)
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    on_bt = librosa.onset.onset_detect(
                        onset_envelope=act, sr=sr, hop_length=self._hop,
                        units="frames", backtrack=True)
                on_bt = np.asarray([f for f in on_bt if 0 <= f < n], dtype=int)
                # strength from the note's PEAK, not the backtracked foot
                bt_strength = np.array([
                    float(np.clip(act[f:min(n, f + 10)].max() / (ref or 1.0), 0, 1))
                    for f in on_bt]) if len(on_bt) else np.zeros(0)
            except Exception:
                on_bt, bt_strength = np.zeros(0, dtype=int), np.zeros(0)
            parts.append({
                "activation": sm,
                "onsets": on,
                "onset_strength": strengths,
                "activation_raw": raw_n,
                "onsets_bt": on_bt,
                "onset_bt_strength": bt_strength,
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
    """True if a cached analysis predates the per-part fields we now need."""
    parts = getattr(a, "parts", None) or []
    if not parts:
        return False
    return not {"onsets", "activation_raw", "onsets_bt"} <= set(parts[0])


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
    if LYRICS_MODE:
        todo = [t for t in tracks if not is_transcribed(t)]
        if verbose:
            print(f"[prewarm] {len(todo)} track(s) need transcribing", flush=True)
        for k, t in enumerate(todo, 1):
            if is_transcribed(t):
                continue
            try:
                t0 = time.perf_counter()
                w = words_cached(t, verbose=False)
                if verbose:
                    print(f"[prewarm] words {k}/{len(todo)}  {time.perf_counter() - t0:5.1f}s  "
                          f"{len(w) if w else 0:4d} words  {os.path.basename(t)}", flush=True)
                if w is None:
                    break                # faster-whisper missing: no point continuing
            except Exception as e:
                if verbose:
                    print(f"[prewarm] words failed {os.path.basename(t)}: {e}", flush=True)
    return done


# ---------------------------------------------------------------------------
# WORDS — transcription with timestamps, cached beside the analysis
# ---------------------------------------------------------------------------
_WORDS_HINTED = False


def words_cache_file_for(path):
    """Sidecar cache for the transcript: same content key as the analysis,
    plus the model and transcript version, so a model change re-transcribes
    without touching the (expensive) analysis cache."""
    base = cache_file_for(path)[:-4]          # strip .pkl
    return f"{base}.{WORDS_MODEL}.w{WORDS_VERSION}.pkl"


def load_words(path):
    """The cached transcript: [(start_s, end_s, word, prob, no_speech)], or None."""
    try:
        with open(words_cache_file_for(path), "rb") as f:
            return pickle.load(f)
    except Exception:
        return None


def is_transcribed(path):
    try:
        return os.path.exists(words_cache_file_for(path))
    except OSError:
        return False


def transcribe_words(path, verbose=True):
    """Word-level transcript of a track with faster-whisper, or None.

    Runs on CPU (int8). Every word is kept with its probability and its
    segment's no-speech score; _attach_words() filters. The model is
    downloaded on first use (needs internet once; ~150 MB for base).
    """
    global _WORDS_HINTED
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        if verbose and not _WORDS_HINTED:
            _WORDS_HINTED = True
            print("  (no transcription: pip install faster-whisper — "
                  "fable-2 runs as fable until then)")
        return None
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = WhisperModel(WORDS_MODEL, device="cpu", compute_type="int8")
        segs, info = model.transcribe(path, word_timestamps=True, vad_filter=False,
                                      beam_size=1, condition_on_previous_text=False)
        words = []
        for seg in segs:
            ns = float(getattr(seg, "no_speech_prob", 0.0))
            for w in (seg.words or []):
                word = w.word.strip()
                if word:
                    words.append((float(w.start), float(w.end), word,
                                  float(w.probability), ns))
    if verbose:
        print(f"  words: {len(words)} ({getattr(info, 'language', '?')})")
    return words


def words_cached(path, verbose=True):
    """Transcript for a track, transcribing and caching it if needed."""
    words = load_words(path)
    if words is not None:
        return words
    words = transcribe_words(path, verbose=verbose)
    if words is None:
        return None
    cache_file = words_cache_file_for(path)
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = f"{cache_file}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            pickle.dump(words, f, protocol=4)
        os.replace(tmp, cache_file)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
    return words


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
        if FABLE_MODE:                           # drops and impacts hit all four
            for l in LIGHTS:
                self.bloom[l["name"]]["impact"] = 0.0
        self._decay = {k: 0.5 ** (1.0 / max(1.0, hl * FPS)) for k, hl in BLOOM_HALF_LIFE.items()}
        # Scene modes replace BLOOM_HALF_LIFE wholesale, so a mode's dict may
        # not carry every kind the engine can bloom on. Fall back rather than
        # KeyError halfway through a track.
        for kind, default in (("beat", 0.13), ("instr", 0.30), ("impact", 0.35)):
            self._decay.setdefault(kind, 0.5 ** (1.0 / max(1.0, default * FPS)))
        self._word_decay = 0.5 ** (1.0 / max(1.0, 0.15 * FPS))   # glint half-life
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
        self._fable = self._plan_fable() if FABLE_MODE else None
        self._solid = {l["name"]: FABLE_SOLID_MIN for l in LIGHTS}
        self._fable_sec = None            # the section whose colour is committed
        self._fable_pending = None        # (section, frame it was first seen)
        # words (fable-2): per-light colour target along the arc, its age,
        # the glint, and a round-robin pointer over the lights
        self._word_at = {}
        self._word_pos = {l["name"]: None for l in LIGHTS}
        self._word_age = {l["name"]: 10 ** 9 for l in LIGHTS}
        self._word_glint = {l["name"]: 0.0 for l in LIGHTS}
        self._word_rr = 0
        self._last_word = -10 ** 9
        self.word_count = 0
        if LYRICS_MODE:
            self._attach_words(load_words(analysis.path))
            if not self._word_at:
                # not transcribed yet (mode switched mid-song): do it in the
                # background and start listening the moment it lands
                def _later():
                    try:
                        self._attach_words(words_cached(analysis.path))
                    except Exception:
                        pass
                threading.Thread(target=_later, daemon=True).start()
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
        key_act = "activation_raw" if INSTRUMENT_V2 else "activation"
        key_on = "onsets_bt" if INSTRUMENT_V2 else "onsets"
        key_st = "onset_bt_strength" if INSTRUMENT_V2 else "onset_strength"
        parts = [p for p in (getattr(a, "parts", None) or [])
                 if len(p.get(key_act, ())) >= n and key_on in p]
        order = ["wheel_b", "wheel_a", "forest_a", "forest_b"]   # low -> high
        if len(parts) < len(order):
            return                       # not enough instruments; fall back
        src = {nm: np.zeros(n) for nm in order}
        onsets = {nm: {} for nm in order}
        if INSTRUMENT_V2 and INSTR_BODY_SMOOTH_FRAMES > 1:
            box = np.ones(INSTR_BODY_SMOOTH_FRAMES) / INSTR_BODY_SMOOTH_FRAMES
            smoothed = [np.convolve(p[key_act], box, mode="same") for p in parts]
        else:
            smoothed = [np.asarray(p[key_act]) for p in parts]
        win = max(int(INSTR_PERIOD_BEATS * a.frames_per_beat), 4 * FPS)
        for start in range(0, n, win):
            end = min(n, start + win)
            activity = [(float(p[key_act][start:end].mean()), k)
                        for k, p in enumerate(parts)]
            top = [k for _, k in sorted(activity, reverse=True)[:len(order)]]
            top.sort(key=lambda k: parts[k]["centroid"])          # low -> high
            for nm, k in zip(order, top):
                p = parts[k]
                src[nm][start:end] = smoothed[k][start:end]
                sel = (p[key_on] >= start) & (p[key_on] < end)
                for f, stg in zip(p[key_on][sel], p[key_st][sel]):
                    onsets[nm][int(f)] = float(stg)
        if not INSTRUMENT_V2:
            # v1 normalised the stitched composite, which leaves a light dim
            # for any period spent on a quiet instrument. v2's parts are each
            # already normalised to themselves, so it must NOT be re-scaled.
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

    def _section_features(self):
        """Per-section loudness, drive and band balance, each spread 0..1.

        Returns (loud, dens, bright) dicts keyed by section, or None. Short
        sections are excluded from the normalising scale but still scored.
        """
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
            return None
        scale = big or feats

        def spread(k):
            vals = np.array([scale[s][k] for s in scale])
            lo, hi = float(vals.min()), float(vals.max())
            if hi <= lo:
                return {s: 0.5 for s in feats}
            return {s: float(np.clip((feats[s][k] - lo) / (hi - lo), 0.0, 1.0))
                    for s in feats}
        return spread(0), spread(1), spread(2)

    def _classify_sections(self):
        """Give each section a palette from its energy, drive and brightness."""
        feats = self._section_features()
        if feats is None:
            return {}
        loud, dens, bright = feats
        out = {}
        for sec in loud:
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

    # ------------------------------------------------------------------
    # FABLE-MODE planning: grid, tiers, drops, builds, impacts
    # ------------------------------------------------------------------
    def _plan_fable(self):
        """Read the song's structure ahead of time and lay out the choreography.

        Returns a dict of per-frame arrays (beat index/phase, bar position,
        phrase, tier, build progress, frames since the last drop) plus the
        drop and impact lists. Everything is derived from the cached
        analysis, so no re-analysis is needed to retune it.
        """
        a, n = self.a, self.a.n
        fpb = float(a.frames_per_beat)
        bar_f = max(1, int(round(4 * fpb)))
        frames = np.arange(n)

        # --- beat grid: index + continuous phase per frame -----------------
        beats = np.asarray(sorted(b for b in a.beat_frames if 0 <= b < n), dtype=int)
        if len(beats) < 8:                       # no usable grid: synthesise one
            beats = np.arange(0, n, max(1, int(round(fpb))), dtype=int)
        # The tracker only marks beats where it hears them; a quiet intro or
        # a held outro has none, and a chase cannot run on a grid that is not
        # there. Extend the grid to both ends at the track's own beat period.
        period = float(np.median(np.diff(beats))) if len(beats) > 1 else fpb
        period = max(1.0, period)
        if beats[0] > period:
            k = int(beats[0] // period)
            beats = np.concatenate([np.round(beats[0] - period * np.arange(k, 0, -1)).astype(int), beats])
        if beats[-1] < n - period:
            k = int((n - beats[-1]) // period)
            beats = np.concatenate([beats, np.round(beats[-1] + period * np.arange(1, k + 1)).astype(int)])
        beats = beats[(beats >= 0) & (beats < n)]
        idx = np.searchsorted(beats, frames, side="right") - 1
        ci = np.clip(idx, 0, len(beats) - 1)
        starts = beats[ci]
        nexts = np.append(beats[1:], beats[-1] + max(1, int(round(fpb))))[ci]
        phase = np.clip((frames - starts) / np.maximum(1, nexts - starts), 0.0, 1.0)
        phase[idx < 0] = 0.0
        idx = np.maximum(idx, 0)

        # --- downbeat: the beat offset (mod 4) carrying the most kick/perc --
        kick = np.zeros(n)
        for f, evs in a.events.items():
            if 0 <= f < n:
                kick[f] += sum(s for k, s in evs if k in ("kick", "perc"))
        bass = np.asarray(a.energy["bass"][:n], dtype=float)
        score = np.zeros(4)
        for k, b in enumerate(beats):
            score[k % 4] += kick[max(0, b - 2):b + 3].sum() + 0.5 * bass[b]
        off = int(np.argmax(score))
        rel = idx - off
        bpos = rel % 4                           # 0 = downbeat
        bar = np.floor_divide(rel, 4)
        phrase = np.floor_divide(rel, FABLE_PHRASE_BEATS)
        bar_frac = bar + (bpos + phase) / 4.0    # bars elapsed, continuous

        # --- tiers: breakdown / groove / peak per section --------------------
        secs = a.section_of[:n]
        feats = self._section_features()
        tier = np.ones(n, dtype=int)
        if feats is not None:
            loud_s, dens_s, _ = feats
            lo_t, hi_t = FABLE_TIER_THRESH
            e_of = {s: 0.60 * loud_s[s] + 0.40 * dens_s[s] for s in loud_s}
            e_fr = np.array([e_of.get(int(s), 0.5) for s in secs])
            # "peak" is the top of THIS song, not everything above a line: a
            # track that never drops would otherwise rock the wheel the same
            # way for four minutes. Cap peak at a share of the duration.
            cap = float(np.quantile(e_fr, 1.0 - FABLE_PEAK_MAX_SHARE))
            tier_of = {}
            for s, e in e_of.items():
                tier_of[s] = 0 if e < lo_t else (2 if (e >= hi_t and e > cap) else 1)
            tier = np.array([tier_of.get(int(s), 1) for s in secs], dtype=int)

        # --- drops: a step up in loudness, snapped to the beat ---------------
        loud = np.asarray(a.loudness[:n], dtype=float)
        wb = max(1, int(round(fpb)))
        L = np.convolve(loud, np.ones(wb) / wb, mode="same")
        cs = np.concatenate([[0.0], np.cumsum(L)])
        W = 2 * bar_f
        hi_i = np.minimum(n, frames + W)
        lo_i = np.maximum(0, frames - W)
        after = (cs[hi_i] - cs[frames]) / np.maximum(1, hi_i - frames)
        before = (cs[frames] - cs[lo_i]) / np.maximum(1, frames - lo_i)
        step = after - before
        bounds = set(int(f) for f in np.where(np.diff(secs) != 0)[0] + 1)
        peaks = np.where(_local_max(step, bar_f) & (step >= np.quantile(step, 0.99)))[0]
        cands = bounds | set(int(f) for f in peaks)
        scored = []
        for f in cands:
            if f < bar_f or f >= n - wb:
                continue
            if step[f] < FABLE_DROP_MIN_JUMP or after[f] < FABLE_DROP_MIN_AFTER:
                continue
            b = int(beats[np.argmin(np.abs(beats - f))])      # snap to the grid
            scored.append((float(step[f]), b))
        scored.sort(reverse=True)
        drops = []
        for s, b in scored:
            if all(abs(b - d) >= FABLE_DROP_MIN_GAP_BARS * bar_f for d in drops):
                drops.append(b)
        drops.sort()

        # --- builds: the bars of rising energy before each drop ---------------
        # A build is heard as loudness AND drive rising together (a snare roll
        # gets busier before it gets louder), so the rise is judged on both.
        dens = np.asarray(a.density[:n], dtype=float)
        rise = 0.5 * L + 0.5 * dens
        build_t = np.full(n, -1.0)               # -1 = not in a build
        builds = []
        for d in drops:
            nb = 0
            while nb < FABLE_BUILD_MAX_BARS:
                s1, e1 = d - (nb + 1) * bar_f, d - nb * bar_f         # this bar
                s0, e0 = d - (nb + 2) * bar_f, d - (nb + 1) * bar_f   # the one before
                if s0 < 0:
                    break
                if rise[s0:e0].mean() > rise[s1:e1].mean() + 0.03:    # not rising
                    break
                nb += 1
            nb = max(FABLE_BUILD_MIN_BARS, min(FABLE_BUILD_MAX_BARS, nb))
            start = max(0, d - nb * bar_f)
            if d > start:
                build_t[start:d] = np.linspace(0.0, 1.0, d - start, endpoint=False)
            builds.append((start, d))
        since_drop = np.full(n, np.inf)          # frames since the last drop
        for d in drops:
            since_drop[d:] = frames[d:] - d

        # --- impacts: the biggest transients, all four lights hit ------------
        Lf = np.convolve(loud, np.ones(4) / 4, mode="same")
        jump = np.zeros(n)
        jump[10:] = Lf[10:] - Lf[:-10]                            # rise over 250 ms
        cand = np.where(_local_max(jump, 20)
                        & (jump >= np.quantile(jump, FABLE_IMPACT_Q)))[0]
        hit_near = np.convolve(kick, np.ones(5), mode="same") > 0   # a drum within +-2 frames
        ref = float(jump[cand].max()) if len(cand) else 1.0
        impacts, last = {}, -10 ** 9
        for f in cand:
            f = int(f)
            if not hit_near[f] or f - last < FABLE_IMPACT_MIN_GAP_S * FPS:
                continue
            if since_drop[f] < 2 * bar_f or 0.6 <= build_t[f]:       # the drop IS the impact
                continue
            last = f
            impacts[f] = float(np.clip(jump[f] / (ref or 1.0), 0.4, 1.0))

        # the figure fades with the music's presence — a 250 ms view, so a
        # sidechain-pumped track does not kill the hit right on the beat
        presence = np.clip(np.convolve(loud, np.ones(10) / 10, mode="same")
                           / FABLE_PATTERN_LOUD, 0.0, 1.0)

        zone_state = self._plan_zones(tier, build_t, since_drop, phrase, bar_f, n)
        zone_on = {z: _envelope(zone_state[z] != 0, *FABLE_ZONE_ON_S) for z in ZONES}
        zone_beat = {z: _envelope(zone_state[z] == 2, *FABLE_ZONE_BEAT_S) for z in ZONES}

        return dict(beats=beats, idx=idx, phase=phase, bpos=bpos, bar=bar,
                    phrase=phrase, bar_frac=bar_frac, tier=tier, build_t=build_t,
                    since_drop=since_drop, drops=drops, builds=builds,
                    impacts=impacts, presence=presence, bar_f=bar_f, fpb=fpb,
                    downbeat=off, zone_state=zone_state, zone_on=zone_on,
                    zone_beat=zone_beat)

    def _plan_zones(self, tier, build_t, since_drop, phrase, bar_f, n):
        """Phrase by phrase, decide which zone beats, glows solid or rests.

        Returns {zone: int array} with 0 = rest, 1 = solid, 2 = beat. The
        sequence is drawn per track from a seeded generator, so a song gets
        its own choreography and the same one every time it is played.
        """
        a = self.a
        rng = random.Random(int(n) * 7919 + int(round(float(a.tempo) * 100)))
        REST, SOLID, BEAT = 0, 1, 2
        state = {z: np.full(n, BEAT, dtype=int) for z in ZONES}
        ph = np.asarray(phrase)
        spans, i = [], 0
        while i < n:
            j = i
            while j < n and ph[j] == ph[i]:
                j += 1
            spans.append((i, j))
            i = j
        # --- the opening: both dark, then the zone whose voice is stronger
        dark_end = min(n, FABLE_OPEN_DARK_BARS * bar_f)
        solo_end = min(n, dark_end + FABLE_OPEN_SOLO_BARS * bar_f)
        for s0, e0 in spans:                 # ...rounded up to a phrase end
            if s0 <= solo_end < e0:
                solo_end = e0
                break
        wheel_v = float(np.mean(a.energy["bass"][dark_end:solo_end] + a.density[dark_end:solo_end])) \
            if solo_end > dark_end else 0.0
        forest_v = float(np.mean(a.energy["mids"][dark_end:solo_end] + a.energy["highs"][dark_end:solo_end])) \
            if solo_end > dark_end else 0.0
        lead = "wheel" if wheel_v >= forest_v else "forest"
        other = "forest" if lead == "wheel" else "wheel"
        for z in ZONES:
            state[z][:dark_end] = REST
        state[lead][dark_end:solo_end] = BEAT
        state[other][dark_end:solo_end] = REST
        # --- the body of the track, one choice per phrase
        names = ("breakdown", "groove", "peak")
        beat_frames = {z: 0 for z in ZONES}
        prev, hold, same_run = None, 0, 0
        for s0, e0 in spans:
            if e0 <= solo_end:
                continue
            s0 = max(s0, solo_end)
            if hold > 0:                     # a longer state carries on
                hold -= 1
                pair = prev
            else:
                t_dom = int(np.bincount(tier[s0:e0], minlength=3).argmax())
                opts = []
                for pair_, dur, w in FABLE_ZONE_OPTIONS[names[t_dom]]:
                    w = float(w)
                    wz, fz = pair_
                    # fairness: the zone that has beaten less gets the next
                    # asymmetric turn; never the same asymmetric pair twice
                    if wz == BEAT and fz != BEAT:
                        w *= 2.0 if beat_frames["wheel"] <= beat_frames["forest"] else 0.5
                    if fz == BEAT and wz != BEAT:
                        w *= 2.0 if beat_frames["forest"] <= beat_frames["wheel"] else 0.5
                    if prev == pair_ and pair_ != (BEAT, BEAT):
                        w = 0.0
                    if pair_ == (BEAT, BEAT) and same_run >= 2:
                        w = 0.0                # never both beating for 3 phrases
                    if w > 0:
                        opts.append((pair_, dur, w))
                total = sum(w for _, _, w in opts)
                r, acc = rng.random() * total, 0.0
                pair, dur = opts[-1][0], opts[-1][1]
                for pair_, dur_, w in opts:
                    acc += w
                    if r <= acc:
                        pair, dur = pair_, dur_
                        break
                hold = dur - 1
            same_run = same_run + 1 if pair == (BEAT, BEAT) == prev else (1 if pair == (BEAT, BEAT) else 0)
            prev = pair
            state["wheel"][s0:e0] = pair[0]
            state["forest"][s0:e0] = pair[1]
            for z, v in zip(("wheel", "forest"), pair):
                if v == BEAT:
                    beat_frames[z] += e0 - s0
        # --- a build or a drop always brings both zones in, beating
        both = (build_t >= 0.0) | (since_drop < FABLE_PHRASE_BEATS * self.a.frames_per_beat)
        for z in ZONES:
            state[z][both] = BEAT
        return state

    def fable_summary(self):
        """One line for the operator: what the planner found in this track."""
        F = self._fable
        if not F:
            return ""
        n = self.a.n
        share = [float((F["tier"] == t).mean()) for t in (0, 1, 2)]
        drops = ", ".join(f"{d / FPS // 60:.0f}:{d / FPS % 60:02.0f}" for d in F["drops"]) or "none"
        bars = "/".join(str(int(round((d - s) / F["bar_f"]))) for s, d in F["builds"])
        zs = F["zone_state"]
        both = float(((zs["wheel"] == 2) & (zs["forest"] == 2)).mean())
        one = float(((zs["wheel"] == 2) ^ (zs["forest"] == 2)).mean())
        rest = float(((zs["wheel"] == 0) | (zs["forest"] == 0)).mean())
        return (f"  fable: drops at {drops}" + (f" (builds {bars} bars)" if bars else "")
                + f" · {len(F['impacts'])} impacts · "
                f"breakdown {share[0]:.0%} / groove {share[1]:.0%} / peak {share[2]:.0%}"
                f"\n         zones: both beat {both:.0%} · one beats {one:.0%} · one rests {rest:.0%}"
                f" · downbeat offset {F['downbeat']}")

    def _fable_state(self, i):
        """Everything the frame needs from the plan, for frame i."""
        F = self._fable
        tier = int(F["tier"][i])
        bt = float(F["build_t"][i])
        phase = float(F["phase"][i])
        bpos = int(F["bpos"][i])
        bar = int(F["bar"][i])
        phrase = int(F["phrase"][i])
        beat_i = int(F["idx"][i])
        fpb = F["fpb"]
        since = float(F["since_drop"][i])
        drop_now = since == 0.0
        # the gap before the drop: inside FABLE_DROP_GAP_BEATS of the next drop
        gap = False
        for d in F["drops"]:
            if 0 < d - i <= FABLE_DROP_GAP_BEATS * fpb:
                gap = True
                break
        hold_f = max(1.0, FABLE_DROP_HOLD_BEATS * fpb)
        hold = max(0.0, 1.0 - 0.35 * since / hold_f) if since < hold_f else 0.0
        impact = F["impacts"].get(i, 0.0)
        if drop_now:
            impact = 1.0

        # --- which zones are beating this frame -----------------------------
        zs = {z: int(F["zone_state"][z][i]) for z in ZONES}
        beating = [z for z in ZONES if zs[z] == 2]
        solo = beating[0] if len(beating) == 1 else None
        mem = {z: [l["name"] for l in LIGHTS if l["zone"] == z] for z in ZONES}

        # --- the figure: per-light pattern level for this frame -----------
        names = [l["name"] for l in LIGHTS]
        pat = {nm: 0.0 for nm in names}
        speed = FABLE_TIER_SPEED[("breakdown", "groove", "peak")[tier]]
        duck, top = 1.0, 1.0                 # no ducking, figure scale 1
        if bt >= 0.0:
            # BUILD: a chase across the garden that doubles in rate, and the
            # whole rig lifts toward the drop
            sub = 1 if bt < 0.45 else (2 if bt < 0.80 else 4)
            p = int(beat_i * sub + phase * sub)
            lp = (phase * sub) % 1.0
            pulse = 0.5 ** (lp / FABLE_PULSE_HALF)
            gain = 0.55 + 0.45 * bt
            pat[FABLE_CHASE_ORDER[p % 4]] = pulse * gain
            for nm in names:
                pat[nm] = max(pat[nm], 0.35 * bt)
            speed = 1.0 + 0.6 * bt
            duck = FABLE_DUCK["groove"] + (FABLE_DUCK["peak"] - FABLE_DUCK["groove"]) * bt
            top = gain
        elif tier == 2:
            # PEAK, two figures alternating by phrase so a long chorus keeps
            # moving:
            #   A  the wheel rocks beat by beat (both on the downbeat); the
            #      forest answers on the off-beat, alternating each bar
            #   B  diagonals: wheel_a+forest_b on the beat, wheel_b+forest_a
            #      on the next, the other diagonal answering on the off-beat
            g = FABLE_TIER_GAIN["peak"]
            duck, top = FABLE_DUCK["peak"], g
            on = 0.5 ** (phase / FABLE_PULSE_HALF)
            offb = 0.5 ** ((phase - 0.5) / FABLE_PULSE_HALF) if phase >= 0.5 else 0.0
            if solo:
                # one zone carries the beat alone: its pair rocks, both on
                # the downbeat, the other light answering on the off-beat
                A, B = mem[solo]
                hit, rest_ = (A, B) if bpos % 2 == 0 else (B, A)
                pat[hit] = on * g
                pat[rest_] = max(on * g * (1.0 if bpos == 0 else 0.30), offb * g * 0.50)
            elif not beating:
                duck = 1.0
            elif phrase % 2 == 0:
                wa, wb = ("wheel_a", "wheel_b") if bpos % 2 == 0 else ("wheel_b", "wheel_a")
                pat[wa] = on * g
                pat[wb] = on * g * (1.0 if bpos == 0 else 0.35)
                fa, fb = ("forest_a", "forest_b") if bar % 2 == 0 else ("forest_b", "forest_a")
                pat[fa] = offb * g * 0.80
                pat[fb] = offb * g * 0.30
            else:
                d1, d2 = (("wheel_a", "forest_b"), ("wheel_b", "forest_a"))
                if bpos % 2 == 1:
                    d1, d2 = d2, d1
                for nm in d1:
                    pat[nm] = on * g
                for nm in d2:
                    pat[nm] = max(on * g * (1.0 if bpos == 0 else 0.25), offb * g * 0.70)
        elif tier == 1:
            # GROOVE: one light per beat; the figure changes every phrase
            g = FABLE_TIER_GAIN["groove"]
            duck, top = FABLE_DUCK["groove"], g
            pulse = 0.5 ** (phase / FABLE_PULSE_HALF)
            fig = phrase % 4
            if solo:
                # one zone alone: its two lights alternate beat by beat
                A, B = mem[solo] if phrase % 2 == 0 else mem[solo][::-1]
                lit = [A] if bpos % 2 == 0 else [B]
                if fig >= 2 and bpos == 0:
                    lit = [A, B]
            elif not beating:
                lit, duck = [], 1.0
            elif fig == 0:
                lit = [FABLE_CHASE_ORDER[bpos]]
            elif fig == 1:
                lit = [FABLE_CHASE_ORDER[::-1][bpos]]
            elif fig == 2:
                lit = ["wheel_a", "wheel_b"] if bpos % 2 == 0 else ["forest_a", "forest_b"]
            else:
                lit = ["wheel_a", "forest_a"] if bpos % 2 == 0 else ["wheel_b", "forest_b"]
            for nm in lit:
                pat[nm] = pulse * g
        else:
            # BREAKDOWN: a slow breath locked to the bar, zones in antiphase,
            # the two lights of a pair a little apart
            cyc = float(F["bar_frac"][i]) / FABLE_BREATHE_BARS
            for light in LIGHTS:
                if light["zone"] not in beating:
                    continue                 # a solid or resting zone does not breathe
                ph = (0.0 if light["zone"] == "wheel" else 0.5) + \
                     (0.0 if light["name"].endswith("_a") else 0.08)
                pat[light["name"]] = FABLE_BREATHE * (0.5 + 0.5 * np.sin(2 * np.pi * (cyc + ph)))
        # The figure's SHAPE (0..1) is what ducks the other lights; its LEVEL
        # is what lifts the lit one. The figure stays under the music: it
        # fades when the track goes quiet — except in a build, where moving
        # ahead of the music is the whole point.
        shape = {nm: (pat[nm] / top if top > 0 else 0.0) for nm in names}
        k = float(F["presence"][i])
        if bt >= 0.0:
            k = max(0.6, k)
        if tier > 0 or bt >= 0.0:
            for nm in names:
                pat[nm] *= k
            duck = 1.0 - (1.0 - duck) * k
        return dict(tier=tier, build_t=bt, phase=phase, bpos=bpos, bar=bar,
                    phrase=phrase, gap=gap, hold=hold, impact=impact,
                    drop_now=drop_now, speed=speed, pat=pat, shape=shape,
                    duck=duck,
                    zone_on={z: float(F["zone_on"][z][i]) for z in ZONES},
                    zone_beat={z: float(F["zone_beat"][z][i]) for z in ZONES})

    def _fable_anchor(self, i, st, sec):
        """Section colour that commits on the downbeat, not mid-bar."""
        if self._fable_sec is None:
            self._fable_sec = sec
            return self.section_t.get(sec, 0.3)
        if sec != self._fable_sec:
            if self._fable_pending is None or self._fable_pending[0] != sec:
                self._fable_pending = (sec, i)
            on_downbeat = st["bpos"] == 0 and st["phase"] < 0.15
            waited = i - self._fable_pending[1] > self._fable["bar_f"]
            if on_downbeat or waited or st["drop_now"]:
                self._fable_sec, self._fable_pending = sec, None
        return self.section_t.get(self._fable_sec, 0.3)

    def _attach_words(self, words):
        """Index a transcript by frame: {frame: [(word, prob), ...]}."""
        if not words:
            return
        at = {}
        for start, _end, word, prob, ns in words:
            if prob < WORDS_MIN_PROB or ns > WORDS_MAX_NOSPEECH:
                continue                 # hallucinated on an instrumental
            f = int(round(float(start) * FPS))
            if 0 <= f < self.a.n:
                at.setdefault(f, []).append((word, float(prob)))
        self._word_at = at           # one assignment: safe from the thread

    @staticmethod
    def _word_arc_pos(word):
        """Where on its zone's arc a word sits: the same word, the same colour."""
        key = "".join(ch for ch in word.lower() if ch.isalnum())
        return hashlib.md5(key.encode()).digest()[0] / 255.0

    def _hear_words(self, i, st):
        """On a new word, repaint one light — rotating through the lights of
        whichever zone is on, never faster than WORD_MIN_GAP_S."""
        names = [l["name"] for l in LIGHTS]
        for nm in names:
            self._word_age[nm] += 1
            self._word_glint[nm] *= self._word_decay
        evs = self._word_at.get(i)
        if not evs or i - self._last_word < WORD_MIN_GAP_S * FPS:
            return
        word = max(evs, key=lambda e: e[1])[0]
        lit = [l["name"] for l in LIGHTS
               if st is None or st["zone_on"][l["zone"]] > 0.3]
        if not lit:
            return
        for k in range(len(names)):
            nm = names[(self._word_rr + k) % len(names)]
            if nm in lit:
                self._word_rr = (self._word_rr + k + 1) % len(names)
                break
        self._word_pos[nm] = self._word_arc_pos(word)
        self._word_age[nm] = 0
        self._word_glint[nm] = 1.0
        self._last_word = i
        self.word_count += 1

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
        return STROBE_LEVEL if self._try_strobe(name, i) else 0.0

    def _try_strobe(self, name, i):
        """Start a burst now if the refractory period and the per-minute
        ceiling allow it. Every strobe — transient or cued — goes through
        here, so the safety caps hold whatever asks for the flash."""
        if not ZONE_FEEL[next(l for l in LIGHTS if l["name"] == name)["zone"]]["strobe"]:
            return False
        if i - self._last_strobe[name] < STROBE_MIN_GAP_S * FPS:
            return False                             # refractory
        recent = [t for t in self._strobe_times[name] if i - t < 60 * FPS]
        self._strobe_times[name] = recent
        if len(recent) >= STROBE_MAX_PER_MIN:        # hard ceiling
            return False
        self._last_strobe[name] = i
        recent.append(i)
        return True

    def frame(self, i):
        """Return {name: dict(dimmer, hue, sat, r, g, b, strobe)} for frame i."""
        a = self.a
        sec = int(a.section_of[i])
        st = self._fable_state(i) if self._fable else None

        # --- contrast position: section anchor, wobbled by the music -------
        if st is None:
            anchor, wobble = self.section_t.get(sec, 0.3), 1.0
        else:
            # Fable: the section colour commits on the downbeat (a cue, not a
            # drift), a build sweeps toward the loud end, a drop snaps there.
            anchor, wobble = self._fable_anchor(i, st, sec), 0.5
        target = (anchor
                  + wobble * HUE_WOBBLE_BRIGHT * (float(a.brightness[i]) - self._bright_mid)
                  + wobble * HUE_WOBBLE_TONAL * (float(a.tonal[i]) - 0.5))
        if st is not None and st["build_t"] >= 0.0:
            target = max(target, 0.25 + 0.75 * st["build_t"])
        target = float(np.clip(target, 0.0, 1.0))
        if st is not None and st["drop_now"]:
            self.pos = target
        else:
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
            if st is not None and st["drop_now"]:
                # a drop is the one place a palette SNAPS instead of morphing
                self._arc = {z: tuple(target[z]) for z in ZONES}
            for z in ZONES:                    # crossfade the arc, don't snap
                cs, cp = self._arc[z]
                ts, tp = target[z]
                self._arc[z] = (_hue_lerp(cs, ts, AUTO_ARC_MORPH),
                                cp + (tp - cp) * AUTO_ARC_MORPH)
        else:
            self._arc = {z: tuple(ZONE_ARC[z]) for z in ZONES}
        zone_hue = {z: self._zone_hue(z, self.pos) for z in ZONES}
        if self._word_at:
            self._hear_words(i, st)

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
            if st is not None:
                coeff = min(1.0, coeff * st["speed"])   # breakdown glides, peak snaps
            self.env[name] += (target - self.env[name]) * coeff

            # 2. blooms: events this light listens to, each fading at its own rate
            bl = self.bloom[name]
            for kind in bl:
                bl[kind] *= self._decay[kind]
            if st is not None and st["impact"] > 0.0:
                bl["impact"] = min(1.0, bl["impact"] + st["impact"] * FABLE_IMPACT_GAIN)
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

            swap = False
            if st is not None:
                # Fable figure: the beat-locked pattern fills in UNDER the
                # instrument layer (max, not sum), so a chase punches through
                # a closed gate but never doubles a note the light is already
                # playing. Then the drop gap and the post-drop hold.
                duck = st["duck"]
                if duck < 1.0:
                    dims[name] *= duck + (1.0 - duck) * st["shape"][name]
                p = st["pat"][name]
                if p > 0.0:
                    # the figure is already a perceptual intent: no gamma, and
                    # in a peak/build the figure IS the hierarchy (no role cap)
                    cap = 1.0 if (st["tier"] == 2 or st["build_t"] >= 0.0) else self.bright[name]
                    dims[name] = max(dims[name], (feel["floor"] + (1.0 - feel["floor"]) * p) * cap)
                if st["hold"] > 0.0:
                    dims[name] = max(dims[name], st["hold"])
                # ZONE STATE: a zone that is not beating holds a SOLID glow —
                # its instrument's running level, no blooms, no figure — and
                # a resting zone goes out (the forest to its floor). Both are
                # crossfades, so a state change is a cue, not a snap.
                self._solid[name] += (max(FABLE_SOLID_MIN, self.env[name]) - self._solid[name]) * 0.03
                solid = (feel["floor"] + (1.0 - feel["floor"]) * self._solid[name] ** DIM_GAMMA) \
                    * self.bright[name]
                z_on, z_beat = st["zone_on"][zone], st["zone_beat"][zone]
                dims[name] = (solid + (dims[name] - solid) * z_beat) * z_on
                if st["gap"]:
                    dims[name] = 0.0            # the forest floor lifts it back
                swap = FABLE_HUE_SWAP and st["tier"] == 2 and st["phrase"] % 2 == 1

            # hue: zone arc position, split within the pair, nudged by blooms
            members = self._members[zone]
            k = members.index(light)
            offset = (k - (len(members) - 1) / 2.0) * INTRA_ZONE_SPREAD
            if swap:
                offset = -offset                # the pair trades hues each phrase
            zh = zone_hue[zone]
            if self._word_pos[name] is not None:
                # fable-2: the last word this light heard pulls its colour to
                # the word's own place on the arc, washing back over WORD_HOLD_S
                w = WORD_MIX * max(0.0, 1.0 - self._word_age[name] / (WORD_HOLD_S * FPS))
                if w > 0.0:
                    zh = self._zone_hue(zone, self.pos + (self._word_pos[name] - self.pos) * w)
            want = (zh + offset + 0.02 * bloom) % 1.0
            self.hue[name] = _hue_lerp(self.hue[name], want, 0.18)

            # saturation: vivid when this band dominates, pastel when quiet,
            # clamped to the zone's surface; a DING pulls toward white (shine)
            tot = sum(float(a.energy[b][i]) for b in BANDS) or 1e-6
            dominance = float(a.energy[band][i]) / tot * len(BANDS)
            raw_sat = 0.45 + 0.40 * dominance + 0.20 * loud
            pal_sat = auto_sat if CURRENT_PALETTE == "auto" else PALETTE_SAT
            lo_s, hi_s = pal_sat if pal_sat else feel["sat"]
            sats[name] = float(np.clip(raw_sat, lo_s, hi_s)) * (1.0 - DING_SHINE * ding)
            if self._word_glint[name] > 0.01:
                sats[name] *= 1.0 - WORD_GLINT * self._word_glint[name]
            if st is not None:
                # a build whitens toward the drop; an impact flashes toward white
                white = FABLE_IMPACT_WHITEN * bl.get("impact", 0.0)
                if st["build_t"] >= 0.0:
                    white = max(white, FABLE_BUILD_WHITEN * st["build_t"])
                sats[name] *= (1.0 - min(0.8, white))

        if st is not None and st["drop_now"]:
            for light in LIGHTS:                # the burst on the drop — capped
                self._try_strobe(light["name"], i)

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
            if st is not None and st["zone_beat"][light["zone"]] < 0.5 and not st["drop_now"]:
                strobe = 0.0                # a solid or resting zone does not flash
            else:
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
    try:
        y, sr = librosa.load(path, sr=44100, mono=False)
    except Exception as e:
        raise RuntimeError(decode_hint(path, e)) from None
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
    if FABLE_MODE:
        print(engine.fable_summary())
    if LYRICS_MODE:
        nw = sum(len(v) for v in engine._word_at.values())
        print(f"  words: {nw} timestamped" if nw else
              "  words: none yet (transcribing in the background)")

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
                        if FABLE_MODE:
                            print(engine.fable_summary())
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
            # OUTPUT_LEAD_S: compute the frame the rig should be SHOWING when
            # this packet has made it through Daslight and the DMX refresh
            lead = int(round(OUTPUT_LEAD_S * FPS))
            values = engine.frame(min(analysis.n - 1, i + lead))
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


# A UDP datagram cannot be arbitrarily large: macOS caps it at 9216 bytes
# (net.inet.udp.maxdgram) and anything off-box is limited by the path MTU. An
# 81-track library already builds a 10 KB listing, so the send failed with
# EMSGSIZE and the browser showed nothing. The listing is chunked instead.
LIST_CHUNK_BYTES = 1200


def _library_lines(state):
    """Listing of folders and songs for the preview's browser, as lines."""
    folders, tracks = scan_library(state.root)
    lines = [f"FOLDER\tall\t{len(tracks)}\t{'*' if not state.folder or state.folder=='all' else ''}"]
    for f in folders:
        n = len([t for t in tracks
                 if t.startswith(os.path.join(state.root, f) + os.sep)])
        lines.append(f"FOLDER\t{f}\t{n}\t{'*' if state.folder == f else ''}")
    shown = scan_tracks(state.root, state.folder)[:500]
    for t in shown:
        rel = os.path.relpath(t, state.root)
        mark = "*" if t == state.current else ""
        lines.append(f"SONG\t{rel}\t{os.path.basename(t)}\t{mark}")
    return lines


def _send_library(sock, dest, state):
    """Send the listing as numbered chunks, each safely inside one datagram."""
    lines = _library_lines(state)
    chunks, cur, size = [], [], 0
    for ln in lines:
        b = len(ln.encode()) + 1
        if cur and size + b > LIST_CHUNK_BYTES:
            chunks.append(cur)
            cur, size = [], 0
        cur.append(ln)
        size += b
    if cur:
        chunks.append(cur)
    if not chunks:
        chunks = [[]]
    for idx, chunk in enumerate(chunks, 1):
        payload = f"LIST {idx}/{len(chunks)}\n" + "\n".join(chunk)
        try:
            sock.sendto(payload.encode(), dest)
        except OSError as e:
            print(f"  (library chunk {idx}/{len(chunks)} not sent: {e})")
            return


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
        if cmd == "ping":
            # Lets the preview prove the control port really reached the
            # show, so dead buttons are visible instead of silent.
            try:
                s.sendto(f"pong {CURRENT_SCENE} {CURRENT_PALETTE}".encode(), src)
            except OSError:
                pass
            continue
        if cmd == "list" and state is not None:
            _send_library(s, src, state)
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
                # Say whether this is instant or a first play: analysing a
                # new track runs librosa flat out for ~20 s, which is the
                # fan noise the operator hears right after the click.
                ready = is_analysed(os.path.join(state.root, state.pending_play))
                s.sendto((f"play={state.pending_play}  "
                          + ("(ready)" if ready else
                             "(first play: analysing ~20 s, then it starts)")).encode(), src)
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
_MAGIC = [
    (b"ID3", "MP3"), (b"\xff\xfb", "MP3"), (b"\xff\xf3", "MP3"),
    (b"RIFF", "WAV"), (b"fLaC", "FLAC"), (b"OggS", "OGG"),
]


def decode_hint(path, exc):
    """Explain a decode failure in terms the operator can act on.

    librosa's own message ("Format not recognised") does not say that the
    file is really an AAC/M4A named .mp3, which is the usual cause.
    """
    ext = os.path.splitext(path)[1].lower()
    real = None
    try:
        with open(path, "rb") as f:
            head = f.read(12)
        if head[4:8] == b"ftyp":
            real = "M4A/AAC"
        else:
            for magic, name in _MAGIC:
                if head.startswith(magic):
                    real = name
                    break
    except OSError:
        pass
    msg = f"{exc}"
    if real == "M4A/AAC":
        msg = (f"this is an M4A/AAC file"
               + (f" named {ext}" if ext != ".m4a" else "")
               + ". No AAC decoder is installed. Convert it, e.g.\n"
               f"        macOS:   afconvert -f WAVE -d LEI16 in.m4a out.wav\n"
               f"        or install ffmpeg and re-run")
    elif real and ext and not ext.endswith(real.lower()):
        msg = f"this is really a {real} file named {ext} — rename it to .{real.lower()}"
    return msg


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
    p.add_argument("--palette", choices=list(PALETTES), default="auto",
                   help="colour palette (default auto: follows the song's "
                        "sections); base is the fixed surface-aware one")
    p.add_argument("--scene", choices=list(SCENE_MODES), default="mid-instrumental-v2",
                   help="default mid-instrumental-v2 (each light follows one "
                        "discovered instrument and lands on its attacks); "
                        "base = the calm garden show; mid = lively pop/rock; "
                        "punchy = dancefloor; fable = structure-aware "
                        "choreography (builds, drops, beat-locked figures); "
                        "fable-2 = fable + every sung word repaints a light")
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
            if LYRICS_MODE:
                words_cached(path)
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
