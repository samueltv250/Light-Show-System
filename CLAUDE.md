# La Rueda — music-reactive lighting

Python engine that analyses a song and drives 4 DMX lights in real time.
Primary output is **Art-Net** (raw DMX at the patched addresses, no mapping
needed). OSC onto hand-mapped Daslight faders is the fallback.

Developed on a Mac; the show runs on the HP. Keep everything cross-platform.

## The rig (do not guess these — they are confirmed from the Daslight patch)

Interface: Daslight DVC GOLD, USB, in PC mode. Laptop: HP, Windows 11.
Audio reaches the venue speakers via the laptop's aux output.
Daslight 5 project: `Pro_La Rueda_vr_thl.dvc`, Universe 1.

Fixtures — all 4 are Iluminarc Ilumipanel ML, 8ch "Arc Full" personality:

| Daslight name       | Addr | Zone   | Driven by |
|---------------------|------|--------|-----------|
| 1.Luz 1 - Rueda     | 1    | wheel  | bass      |
| 4.Luz 2 - rueda izq | 10   | wheel  | bass, lagged half a beat |
| 2.Luz 3 - bosque    | 30   | forest | mids      |
| 3.Luz 4 - Bosque    | 20   | forest | highs, lagged quarter beat |

Channel layout within each fixture (offset from its start address):
`1 Dimmer, 2 Red, 3 Green, 4 Blue, 5 Preset Colors, 6 Colour Temperature,
7 Strobe, 8 Dimmer Speed`

Two lights point at the hydro wheel, two light the forest area.

## Architecture

`rueda_lights.py` is a single file, three layers:

1. `SongAnalysis` — precomputes per-frame (40 fps) band energy, spectral flux,
   loudness, brightness (spectral centroid), tonality (chroma), tempo/beat
   grid, and structural sections (agglomerative clustering on MFCCs).
2. `ShowEngine` — turns analysis into colour + intensity per light.
   **Palette is surface-aware.** Each zone owns a hue arc (`ZONE_ARC`):
   wheel = warm (gold→red→magenta) because it is rust-red wood; forest =
   cool (chartreuse→cyan→blue) because it is foliage. Arcs never overlap.
   A single contrast position `t` (0 quiet … 1 loud), anchored per section
   by loudness and wobbled by brightness/chroma, places both zones along
   their arcs — so quiet = gold+green, loud = crimson+blue. Pairs split
   ±`INTRA_ZONE_SPREAD` around the zone hue; `enforce_separation` is the
   final guarantee. Intensity is an asymmetric envelope over band energy +
   flux with per-zone attack/release/floor (`ZONE_FEEL`), then `DIM_GAMMA`
   for perceptual dimming. Forest never strobes. `idle_values()` is the
   gold/green hold shown at startup and between tracks (never black).
   `forest_b` has `gain` 1.3 because hats are sparse.
3. Output backends:
   - `ArtNetOut` — raw DMX bytes at the patched channels (default). Writes
     only Dimmer/R/G/B/Strobe per fixture; leaves Preset Colors, Colour
     Temperature and Dimmer Speed at 0 so RGB is not overridden.
   - `OSCOut` — floats 0.0–1.0 to Daslight over UDP, fallback.

`run_show.py` is the venue-facing launcher: checks Python, auto-installs
packages, detects Daslight and its ports, then runs the show. `--check`
diagnoses without launching; `--osc-setup` walks the 20 OSC mappings.

`analyse_cached()` memoises analysis to `.cache/` (~30s -> ~2s per track).
The key covers file mtime/size plus FPS, SECTION_SECONDS and BANDS, so
changing those invalidates it; the feel knobs apply live and do not.

Strobes are gated three ways: a per-track percentile on UNCLIPPED flux
(`flux_raw`), a per-light refractory period, and a hard per-minute ceiling.
The old z-score-on-normalised-flux fired ~125x/min — `_norm()` clips at the
95th percentile, so the z-score had no usable tail to threshold against.
Do not go back to thresholding `flux`; use `flux_raw`.

`enforce_separation()` guarantees no two of the four hues land within
`MIN_HUE_GAP` of each other. It is deterministic and was brute-force verified
over 300k random inputs. **If you modify it, re-run that verification** — an
earlier iterative-push version silently allowed collisions.

## Constraints worth knowing

- Daslight's project format is proprietary. Scenes and patching CANNOT be
  created from code. OSC only drives faders that a human has already mapped
  via Mappings > Map OSC — which is why Art-Net is the default path. Do not
  attempt to write .dvc files.
- Daslight's OSC input is on port **7000**, not 8000. Art-Net is 6454.
- OSC values are floats 0.0–1.0; Art-Net values are bytes 0–255.
- Art-Net universe 0 corresponds to Daslight "Universe 1".
- The script plays the audio itself (`sounddevice`) and starts its frame clock
  at the same instant, which is what keeps lights and music in sync. Don't
  split those into separate processes. Audio for the next track is decoded
  in the preload thread alongside analysis, so tracks start instantly.
- Venue is a public garden at night by a river: the forest floor and the
  no-black-between-songs rule are safety/ambience choices, not taste. Strobe
  stays wheel-only and rare.
- Windows Firewall may block OSC packets to Daslight; that's a known snag.

## Status

Verified on a real track (librosa 1.0, numpy 2.5): analysis, engine, hue
separation, 40 fps frame clock, Art-Net packet structure, channel placement,
blackout-on-exit, and the analysis cache all work.

**Still unverified: whether Daslight accepts Art-Net INPUT and forwards it to
the DVC GOLD.** Port 6454 is open, but Daslight did NOT answer an ArtPoll
broadcast, which is weak evidence against input support (not conclusive —
some receivers take DMX without implementing discovery). Settle it at the
rig with `python run_show.py --artnet-test`.
If it fails, the fallback ladder is:
  1. `python run_show.py --osc-setup` — map the 20 faders, then `--mode rgb`
  2. `--mode hsv` — map per-group Hue/Sat/Dimmer (needs one group per fixture)

## Testing without hardware

`rig_preview.py` (stdlib-only: socket + tkinter, needs Tk 8.6 — Apple's
/usr/bin/python3 has Tk 8.5 and draws a black window; use miniforge/python.org
Python) is a software DMX node: it
binds the Art-Net port, decodes ArtDmx, answers ArtPoll, and draws the four
fixtures plus the lit scene in a window. Run it on `--port 6455` and the show
with `--artnet-port 6455` (Daslight swallows all unicast on 6454 if running).
This exercises the real wire path; prefer it over `--simulate` for anything
that touches output. It duplicates the fixture table on purpose so it has no
import from the engine — keep the two in sync if the patch ever changes.
`python run_show.py --simulate` renders coloured bars in the terminal; use
it for quick engine checks. `python run_show.py --check` diagnoses the
environment without launching.
