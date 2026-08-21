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

That is the whole procedure. It installs its own packages on first run,
finds Daslight, analyses each track, and plays them one after another.

`[n]` skips a track, `[q]` quits. Lights black out on exit.

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
any Art-Net node that answers. Note that a silent result is **not** proof
of failure — some receivers accept DMX without answering discovery.
`--artnet-test` is the authoritative check.

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

### Full rehearsal on screen (recommended)

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

Between songs, and while the first one loads, the lights hold a low
gold-and-green idle look — the garden is never dropped into black.

## Tuning

All knobs are at the top of `rueda_lights.py`:

- `ZONE_ARC` — each zone's hue arc as `(start, signed_span)`. Widen a span
  for more colour travel, shift a start to re-centre the quiet look.
- `ZONE_FEEL` — per zone: `attack`/`release` (snappy vs breathing),
  `floor` (how dark it may get), `sat` range, and whether it may `strobe`.
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

Changing `FPS`, `SECTION_SECONDS`, or `BANDS` invalidates the cache
automatically; everything above applies live and does not.

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
