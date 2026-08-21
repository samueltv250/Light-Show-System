# La Rueda light engine

Plays the songs in `songs/` and drives the four Ilumipanel fixtures in time
with the music.

---

## Tomorrow, at the venue

1. Install Python 3.10+ from python.org — **tick "Add Python to PATH"**.
2. Put the mp3s in the `songs\` folder next to `run_show.py`.
3. Start Daslight and open `Pro_La Rueda_vr_thl.dvc`.
4. Plug in the DVC GOLD and the aux cable. Set the aux as the Windows
   default playback device **before** starting the show.
5. **Prove Art-Net reaches the rig** (30 seconds, do this first):

   ```
   python run_show.py --artnet-test
   ```

   Each fixture lights alone in turn — red, green, blue, white — then all
   four fade together. If the lights follow along, you are done; run the
   show. If nothing moves, see "If Art-Net does not work" below.

6. Run the show:

   ```
   python run_show.py
   ```

   Or, to get the preview window alongside the real lights:

   ```
   python run_show.py --preview
   ```

That is the whole procedure. It installs its own packages on first run,
finds Daslight, analyses each track, and plays them one after another.

## Set lists and picking songs

Put subfolders inside `songs\` and each becomes a **set list**:

```
songs\
    warmup\      <- a set list
    dinner\      <- a set list
    dancefloor\  <- a set list
    stray-song.mp3
```

The **SONGS** button in the preview (or `[l]`) opens a browser:

- click a **folder** and the show loops only that folder
- double-click a **song** to play it immediately
- "All songs" goes back to the whole library
- picking a song from another folder widens the set list automatically
- ▶ marks the folder and song currently playing; ↻ Refresh re-reads

Over the control port directly: `list`, `folder dancefloor`, `folder all`,
`play dancefloor/track.mp3`.

Folders and songs added while the show runs are picked up between tracks —
no restart.

The set list **loops**, and the `songs\` folder is re-read between tracks —
drop a new mp3 in while the show is running and it joins the rotation
(alphabetically, or at a random spot with `--shuffle`); delete one and it
drops out. If the folder is emptied the lights hold their resting look and
the show waits rather than quitting. `--no-loop` stops after the last track.

`[n]` skips a track, `[p]` pauses/resumes, `[q]` quits. Lights black out on exit — also when the
process is killed or its window closed (SIGTERM/SIGHUP are handled). The
same two commands are accepted as `next` / `quit` over UDP on port 6460,
which is what the preview's buttons use.

### If something looks wrong

```
python run_show.py --check
```

Checks Python, packages, Daslight, ports, and the song list, and changes
nothing. Run this first when anything misbehaves.

---

## How it reaches the lights

Default is **Art-Net**: raw DMX values sent straight at the patched
addresses. Nothing needs mapping in Daslight — the channel numbers come
from the patch itself.

| Fixture | Address | Dimmer | R | G | B | Strobe |
|---------------------|-----|----|----|----|----|----|
| 1.Luz 1 - Rueda     | 1   | 1  | 2  | 3  | 4  | 7  |
| 4.Luz 2 - rueda izq | 10  | 10 | 11 | 12 | 13 | 16 |
| 3.Luz 4 - Bosque    | 20  | 20 | 21 | 22 | 23 | 26 |
| 2.Luz 3 - bosque    | 30  | 30 | 31 | 32 | 33 | 36 |

**Art-Net input must be enabled in Daslight** for this to work.

### If Art-Net does not work

In order, stopping as soon as the lights respond:

1. Enable Art-Net **input** in Daslight's settings, then re-run
   `--artnet-test`.
2. Try the other universe numbering:
   `python run_show.py --artnet-test --artnet-universe 1`
3. Fall back to OSC (below).

`python rueda_lights.py --artnet-discover` broadcasts an ArtPoll and lists
any Art-Net node that answers. A silent result is **not** proof of
failure: some receivers accept DMX without answering discovery, and on the
same machine as Daslight its own socket can swallow the reply before we
see it. `--artnet-test` is the authoritative check.

### Fallback: OSC

If Daslight will not take Art-Net input, OSC still works, but every fader
has to be mapped by hand once. There is a wizard for it:

```
python run_show.py --osc-setup
```

It walks all 20 faders one at a time, wiggling each address while you
click the matching fader under **Mappings > Map OSC**. Save the Daslight
project afterwards so the mappings persist. Then run the show with:

```
python rueda_lights.py songs --mode rgb
```

---

## Testing without the rig

### The preview during a live show

```
python run_show.py --preview
```

The lights run for real **and** the preview window opens, with its MODE /
PAUSE / SKIP / STOP buttons controlling the live show. Use it as the
operator panel: you can see what the rig is doing and drive it from one
place.

It works by *mirroring*: the show sends every frame to Daslight on 6454 and
a second copy to 127.0.0.1:6455. The preview cannot simply listen in on
6454, because Daslight holds that port exclusively — a second listener
there receives nothing. The launcher also picks a Python that can actually
open a window (Tk 8.6) rather than assuming the one running the show can,
and closes the preview when the show ends. If no such Python exists the
show runs normally and only the window is skipped.

### Full rehearsal on screen (no rig)

`rig_preview.py` is a software DMX node: it binds the Art-Net port, decodes
the packets exactly as Daslight + the DVC GOLD would, and draws the four
fixtures **and the scene they light** — the rust-red wheel lit by the wheel
pair, the palms uplit by the forest pair — in a window. So you see what the
show will actually look like, driven by the real output path.

Two terminals:

```
python rig_preview.py --port 6455
```

```
python run_show.py --artnet-port 6455
```

(Port 6455 because Daslight, if running, swallows everything on 6454. At
the venue nothing needs `--artnet-port`.) `--artnet-test --artnet-port 6455`
walks the fixtures through R/G/B/W into the preview the same way.

The window has **SKIP** and **STOP** buttons (keys `n` / `q`). They send
`next` / `quit` over UDP to the show's control port (6460) on whichever
machine the Art-Net is coming from — the same as pressing `[n]` / `[q]` in
the show's terminal. Anything else can drive that port too
(`echo -n next | nc -u -w0 127.0.0.1 6460`).

`rig_preview.py` is pure standard library (socket + tkinter) but needs
**Tk 8.6**. On Windows the python.org installer has it. On macOS the
Homebrew Python has no tkinter at all, and Apple's `/usr/bin/python3` has
Tk 8.5, which draws a **black window** on recent macOS — use a conda /
miniforge / python.org Python instead (`python -c "import tkinter;
print(tkinter.TkVersion)"` should print 8.6). Use `--no-window` for
terminal bars only.

### Quick algorithm check

```
python run_show.py --simulate
```

Plays the music and draws the four lights as coloured bars in the
terminal. No DMX, no Daslight. The bars use the exact values that would go
to DMX, gamma included.

## Speed

The **first ever run on a new machine takes ~30 seconds** before the first
song starts. That is a one-time cost: librosa compiles its beat tracker
with numba and caches the result inside the installed package. Every run
after that is fast, on every track.

Analysis itself is ~2 seconds per track, and results are cached in
`.cache/` keyed on **file content**, so the cache stays valid if the songs
are copied to another machine or renamed. You can analyse the set list at
home with `--simulate` and copy `.cache/` across with the mp3s.

To get the one-time numba cost out of the way before the venue, run
`python run_show.py --simulate` once on the show laptop.

## Two scene modes

| Mode | For | Feel | strobes/min |
|---|---|---|---|
| **base** (default) | the quiet end of the night | glows, gates, bells that shine | ~5 |
| **mid** | lively pop and rock | awake and moving, still a garden | ~23 |
| **punchy** | dancefloor sets (Daft Punk, house, techno) | fast envelopes, short blooms, a bloom on **every beat**, gates opened up | ~70 |

The MODE button cycles base → mid → punchy → base.

## Colour palettes

Independent of scene mode — the PALETTE button (or `[c]`, or
`palette neon` over the control port, or `--palette neon` at launch).
Switching is instant and the colours drift across, so it is safe mid-song.

| Palette | Wheel | Forest | Note |
|---|---|---|---|
| **base** (default) | gold → red → crimson | chartreuse → cyan → azure | **surface-aware** — the one grounded in the real venue |
| **neon** | magenta → violet | green → azure | electric, saturation pinned near full |
| **ember** | orange → red | gold | all warm; flatters the wood, foliage goes olive |
| **ocean** | teal → cyan | blue → violet | striking, but cool light on rust-red wood reads muddy |
| **tropical** | hot pink → orange | chartreuse → teal | vivid garden |

**`base` is the honest one.** It puts warm light on the rust-red wooden
wheel and cool light on the foliage, because blue on red-brown wood reads
as mud and red on leaves kills them. The others are deliberate stylistic
departures and some of them break that rule on purpose — the table says
which. They will look striking in the preview; check them on the real
surfaces before committing to one for the night.

Switch with the **MODE** button in the preview, `[m]` in the show's
terminal, `mode` / `mode punchy` over the control port, or start in one:

```
python run_show.py --scene mid
```

Switching mid-song rebuilds the engine and carries on from the same
position.

**Strobe safety in punchy.** It is much faster, but not unlimited.
Sustained flashing above ~3 Hz is the photosensitive-epilepsy threshold and
people sit in the forest, so two limits keep it under that: bursts can be no
closer than `STROBE_MIN_GAP_S` (0.40 s = 2.5 Hz peak) and no more frequent
on average than `STROBE_MAX_PER_MIN` (80/min = 1.33 Hz). Beat-synced
flashing on a 120–130 BPM track is naturally ~2 Hz, so the music sits under
the line on its own. **The forest never strobes in either mode.** If you
raise these, do it knowing what they are for.

## How the look is designed

The engine knows what each pair of lights lands on:

- **Wheel** — a rust-red wooden water wheel beside white cascading water.
  Warm light makes the wood glow; blue or green on red-brown wood reads as
  mud. The wheel lives on a **warm arc**: gold → amber → red → crimson →
  magenta. It is the kinetic centrepiece, so it pulses with the bass, and
  the second wheel light lags half a beat so light rolls across it.
- **Forest** — palms and tall trees uplit from the lawn, with river mist
  in the air. Foliage flatters green, teal, cyan, blue and gold and kills
  red/magenta. The forest lives on a **cool arc**: chartreuse → green →
  teal → cyan → blue. It is atmosphere, so it breathes rather than
  flashes, keeps a higher floor (people sit there, by a river, at night),
  and never strobes.

The two arcs never overlap, so the zones can never wash into the same
colour, and they are complementary. **The music decides how far along
each arc the zones travel**: quiet passages pull both toward the
gold/chartreuse meeting point (an intimate, golden garden); loud passages
push them apart to a crimson wheel against a blue forest. Within a
section, spectral brightness and chord changes move the palette around
that anchor.

**A rhythm light and a tone light in each pair.** Each zone's two fixtures
play against each other rather than moving together:

| Light | Daslight | Role | Rides |
|---|---|---|---|
| `wheel_a`  | 1.Luz 1 - Rueda     | **rhythm** | any drum or percussion, extra weight on the kick |
| `wheel_b`  | 4.Luz 2 - rueda izq | tone | deep bass swell, half a beat behind — the roll across the wheel |
| `forest_a` | 2.Luz 3 - bosque    | **rhythm** | any drum or percussion, gentler (it is still foliage) |
| `forest_b` | 3.Luz 4 - Bosque    | tone | shimmer and **bells** — dings shine here, undisturbed by drums |

**Every light is DARK unless its own part is playing.** This is what makes
the rig read as four instruments instead of four meters. The bell light
lights only when a bell rings; the drum light only while the kit drives;
the others follow a tonal part of the arrangement.

Parts are discovered **per song**, by NMF on the harmonic spectrogram
(drums removed) — nothing is hard-coded to an instrument, so a track with
a violin gets a violin-ish part and a track with horns gets a horn-ish
part.

One honest caveat: NMF on a finished mix does **not** cleanly isolate
instruments — components overlap and most are active most of the time.
So the gate threshold is not fixed: it is *solved per light per song* to
hit a target duty cycle (`GATE_DUTY`). Each light opens during its own
most prominent moments and is dark the rest of the time, whatever the
source looks like. That is what makes the effect reliable on any track.
Measured across four very different songs, each light is fully dark
between 17% and 65% of the song, and the bell light spends ~80% of its
lit time within 1.8 s of an actual ding.

**A hierarchy, not four lights at full.** Each light has a *voice* —
`wheel_a` percussion, `wheel_b` bass, `forest_a` mids/vocal, `forest_b`
highs/shimmer. Every four bars the four voices are ranked by how far each
is standing above its own usual level, and the lights are tiered:

| Tier | Level | Who gets it |
|---|---|---|
| **lead**       | 1.00 | the voice currently leading the song |
| **main**       | 0.70 | the main chorus behind it |
| **complement** | 0.45 | the chorus complement (two lights) |

Exactly one light leads at any moment. Roles are decided per phrase, not
per frame, and crossfade over about a second, so the hierarchy shifts
musically instead of twitching.

**Anticipation.** A light dips almost out in the ~0.8 s *before its own
voice surges*, then comes back on the hit — the breath before the phrase
lands. Because the whole track is analysed before playback, the engine can
genuinely see the surge coming. These are rare by design (~20 per song,
never twice within 14 s on the same light) and **only one light is ever
dipped at a time**.

**The forest never goes dark.** `ZONE_SAFETY_FLOOR` guarantees the
brightest forest light stays at or above 0.18 no matter what the music
does — people sit there, by a river, at night. When it engages, both
forest lights scale together so their dynamics survive. The wheel has no
such floor and may go almost out; it is a feature, not a footpath.

**Instruments, not flux.** Each track is scanned for discrete musical
events — **perc** (any drum: conga, timbale, bongó, clave, cowbell, snare,
kick, hat — detected as onsets on the *percussive* component of a
harmonic/percussive split, so sustained vocals and chords are ignored),
kicks (bass attacks), hits (snare, chord stabs), ticks (hats) and
**dings** (bells, chimes, plucks: tonal onsets that ring out). A light
*blooms* on the events it listens to: a quick rise, then an exponential
fade with that event's own half-life — a kick thumps for a quarter second,
a bell **shines** for a second and a half and pulls the colour toward white
at the strike. The body of each light is the band's loudness, smoothed, so
the lights never tremble on frame-level noise.

**Speed follows the drums.** How fast the lights move is set by how hard
the drums are driving right now (percussive energy from a
harmonic/percussive split): sparse passages glow and fade slowly; when the
kit comes in, attack and release tighten and the drum blooms hit full
strength. Bells shine at full strength regardless.

Between songs, and while the first one loads, the lights hold a low
gold-and-green idle look — the garden is never dropped into black.

## Tuning

All knobs are at the top of `rueda_lights.py`:

- `ZONE_ARC` — each zone's hue arc as `(start, signed_span)`. Widen a span
  for more colour travel, shift a start to re-centre the quiet look.
- `ZONE_FEEL` — per zone: `attack`/`release` as `(slow, fast)` pairs picked
  by musical density, `floor` (how dark it may get), `sat` range, and
  whether it may `strobe`.
- `"events"` on a light in `LIGHTS` — which events bloom it and how hard
  (`perc`/`kick`/`hit`/`tick`/`ding`). Move `perc` between lights to change
  which fixture is the drummer.
- `GATE_DUTY` — the fraction of a song each light may be lit. **This is the
  main dial for "the lights are on too much".** Lower it and the rig gets
  sparser and more dramatic.
- `GATE_ATTACK_S` / `GATE_RELEASE_S` — how fast a light comes up when its
  part starts and fades when it stops. `GATE_MIN_ON_S` / `GATE_MIN_OFF_S`
  stop it chattering; `GATE_HYSTERESIS` likewise.
- `DING_GATE_HOLD_S` — how long the bell light stays lit after each ding;
  `DING_GATE_MIN` is how many dings a track needs before that light is
  given over to bells at all (otherwise it follows a discovered part).
- `N_PARTS` — how many parts NMF looks for.
- `ROLE_EXEMPT_DUTY` — a light lit less than this is treated as an *event*
  and always runs at full, so a rare bell strike is not tiered down into
  invisibility.
- `ROLE_LEVELS` — the three brightness tiers, and `ROLE_PHRASE_BEATS` how
  often roles are re-ranked (16 = every 4 bars). `ROLE_FADE` is the
  crossfade speed between tiers.
- `ANTICIPATION_LEAD_S` / `ANTICIPATION_FLOOR` / `ANTICIPATION_MIN_GAP_S` /
  `ANTICIPATION_Q` — how long the pre-surge dip lasts, how dark it goes,
  how rarely it may happen, and how big a surge qualifies. `ANTICIPATE =
  False` turns the whole effect off.
- `ZONE_SAFETY_FLOOR` — the brightest-light floor per zone. **Do not lower
  the forest value without a reason**; it is what keeps the seating area
  lit.
- `PERC_ACCENT_Q` — how selective the percussion is. A busy pattern can run
  4–5 strokes a second and blooming on all of them looks like flicker, so
  only onsets above this quantile of the track's own percussive onsets fire
  (0.55 = the stronger half; lower it to catch more of the pattern).
  `PERC_MIN_GAP_S` additionally merges strokes closer than 0.14 s. `BLOOM_HALF_LIFE` sets how long each
  kind of bloom takes to fade; `DING_SHINE` how white a bell strike goes.
- `BLOOM_DENSITY_FLOOR` — how much of a drum bloom survives when the drums
  are not driving (0.3 = gentle); `ENERGY_SMOOTH_FRAMES` — body smoothing.
- `DING_SUSTAIN_Q` / `DING_FLAT_Q` / `DING_STRENGTH_Q` — how picky the bell
  detector is (quantiles of that track's own bell-band onsets).
- `DIM_GAMMA` — perceptual dimming curve. 2.0 makes beats pop in a dark
  garden; 1.0 is linear DMX.
- `CONTRAST_LOUDNESS` — the section loudness range mapped to
  quiet-end … loud-end of the arcs.
- `HUE_WOBBLE_BRIGHT` / `HUE_WOBBLE_TONAL` — how much the music moves the
  palette within a section.
- `MIN_HUE_GAP` — minimum colour separation between any two lights.
- `INTRA_ZONE_SPREAD` — two-tone width within the wheel pair / forest pair.
- `HUE_SMOOTH` — how slowly the palette drifts (low = cinematic).
- `"gain"` on a light in `LIGHTS` — lifts a light whose band is sparse
  (the highs light has 1.3 so it is not the dim one of its pair).
- `STROBE_PERCENTILE` / `STROBE_MIN_GAP_S` — how rare the strobe accents are.

Changing `FPS`, `SECTION_SECONDS`, `BANDS`, the event detector or the
analysis smoothing invalidates the cache automatically (`CACHE_VERSION`);
the feel knobs apply live and do not.

## Strobe safety

Strobes fire ~5-7 times per minute per light, never twice within
`STROBE_MIN_GAP_S` (2.5s), capped at `STROBE_MAX_PER_MIN`, and the fixture
strobe channel is driven at a deliberately low 71/255 so the flash rate
stays slow.

**Flashing above ~3 Hz is a photosensitive-epilepsy risk in a public
venue.** If you raise `STROBE_LEVEL`, check the actual flash rate on the
fixture before running it for an audience, or set `STROBE_LEVEL = 0.0` to
disable strobes entirely.

## Note

Art-Net writes only the 20 channels listed above. Channels 5, 6 and 8 of
each fixture (Preset Colors, Colour Temperature, Dimmer Speed) are left
at 0, and no other universe or fixture is touched.
