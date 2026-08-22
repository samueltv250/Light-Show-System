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

1. `SongAnalysis` — precomputes per-frame (40 fps) band energy (smoothed
   125 ms — the body must not tremble), spectral flux (raw copy kept for the
   strobe gate), loudness, brightness, tonality, tempo/beat grid, structural
   sections (MFCC clustering), **events** (per-band onsets → kick/hit/tick,
   plus bell-band onsets classified into `ding` when tonal + sustained +
   strong, by per-track quantiles; plus **`perc`** — onsets on the PERCUSSIVE
   component of the h/p split, i.e. any drum anywhere in the spectrum, gated
   to the accents by `PERC_ACCENT_Q`/`PERC_MIN_GAP_S` because blooming on
   every stroke of a busy conga pattern reads as flicker) and **density** (percussive energy from
   `librosa.decompose.hpss`, smoothed 2 s, normalised — NOT onset count,
   which inverts on a clean intro vs a dense chorus).
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
   for perceptual dimming. Forest never strobes.
   **Blooms:** each light keeps one accumulator per event kind it listens
   to (`LIGHTS[..]["events"]`), decayed every frame by that kind's half-life
   and bumped on an event by `strength * gain`. Drum blooms are scaled by
   density (`BLOOM_DENSITY_FLOOR`); dings are not. Blooms fill the headroom
   ABOVE the body (`body + (1-body)*bloom`) so a ding peaks at 1 and fades
   back to the body — summing-and-capping pinned the lights at full.
   `attack`/`release` are `(slow, fast)` pairs interpolated by density.
   A ding also pulls saturation toward white (`DING_SHINE`).
   **Gate layer — a light is dark unless its part plays.** `_discover_parts()`
   runs NMF on the HARMONIC spectrogram (`N_PARTS`), giving per-song timbres
   with an activation curve, centroid and contrast. `_build_gates()` gives
   each light a source (wheel_a = percussive energy; wheel_b/forest_a = the
   most on/off-ish parts, split low/high by centroid; forest_b = the DING
   events) and gates it.
   **The threshold is SOLVED, not set** (`_solve_gate`): thresholding at
   `quantile(1-duty)` does not yield that duty, because hysteresis and the
   minimum-run smoothing fill gaps — on the drums a 45% target came out 87%
   lit. Bisection on the quantile fixes it. Do not replace it with a fixed
   threshold.
   NMF on a full mix does NOT separate instruments cleanly (all components
   land ~600-1400 Hz, duty 0.2-0.7); duty-targeting is what makes the effect
   work anyway. Don't expect real stem separation without a proper model.
   `ROLE_EXEMPT_DUTY`: a light lit under 35% is an event and skips the role
   tiering — tiering the bell light down capped a strike at 0.41.
   Both `_gate_bool`'s hysteresis and `_local_max` are vectorised and
   verified against loop references; the loop versions took >60 s on a
   5-minute track, which looked like a hang before playback.

   **Brightness layer (on top of everything above).** Each light has a
   *voice* (`wheel_a` percussion via `density`, `wheel_b` bass, `forest_a`
   mids, `forest_b` highs). `_plan_roles()` ranks the voices every
   `ROLE_PHRASE_BEATS` and assigns lead/main/complement from `ROLE_LEVELS`,
   crossfaded by `ROLE_FADE`; exactly one light leads at a time. Ranking uses
   each voice's PERCENTILE RANK, then normalises the phrase scores per voice
   — a z-score on the raw signal let the peaky highs hog the lead (49%), and
   un-normalised phrase means let the smooth mids never win it (3%).
   `_plan_dropouts()` looks AHEAD for a surge in a light's own voice and dips
   that light in the `ANTICIPATION_LEAD_S` before it, restoring on the hit;
   scheduled offline, strongest surges first, with a global non-overlap rule
   so only one light is ever dipped. `ZONE_SAFETY_FLOOR` then guarantees the
   brightest light of a zone — the forest floor is a safety rule, people sit
   there — scaling the zone's lights together so dynamics survive.
   **Each zone has a rhythm light and a tone light**: `wheel_a`/`forest_a`
   ride `perc` (any drum); `wheel_b` is the deep lagged bass swell that keeps
   the roll; `forest_b` carries shimmer and the dings. This is deliberate —
   the pairs should play against each other, not move as one. `idle_values()` is the
   gold/green hold shown at startup and between tracks (never black).
   `forest_b` has `gain` 1.3 because hats are sparse.
3. Output backends:
   - `ArtNetOut` — raw DMX bytes at the patched channels (default). Writes
     only Dimmer/R/G/B/Strobe per fixture; leaves Preset Colors, Colour
     Temperature and Dimmer Speed at 0 so RGB is not overridden.
   - `OSCOut` — floats 0.0–1.0 to Daslight over UDP, fallback.

`scan_library()` walks `songs/` recursively: each subfolder is a set list.
`ShowState` (root, folder, tracks, current, pending_play) is shared with
`control_listener`, which answers `list` with a tab-separated FOLDER/SONG
listing sent as numbered `LIST i/n` CHUNKS and accepts
`folder <rel>` / `folder all` / `play <rel>`. Picking a song outside the
current set list widens the selection to the whole library rather than
failing. `rig_preview.py`'s SONGS button opens a Toplevel browser over that
protocol — a separate window on purpose, so the scene canvas never has to
be relaid out. **`selection_set()` fires `<<ListboxSelect>>`**, so marking
the active folder re-entered the click handler, sent a `folder` command
(which does `q.put("n")` and therefore SKIPPED A TRACK) and scheduled
another refresh — a loop that made every click feel unreliable. The handler
is guarded by a `quiet` flag and no-ops when the folder is already active;
keep both if you touch it. All controls are built with `_button()`
(Frame+Label with explicit bindings), NOT `tk.Button`: on macOS the native
Aqua control keeps its own fixed hit region, so a padded button with a
larger font is drawn bigger than it is clickable and only the middle
responds. Verified by generating clicks at every corner and edge.
The show runs on Windows, so the UI must stay portable: fonts come from
`ui_font()`/`mono_font()` (Segoe UI/Consolas on Windows, Helvetica/Menlo on
macOS) — do NOT hard-code "Helvetica" or "Menlo" — and button labels are
ASCII, because U+23ED/23EF/23F9/266B and emoji render as tofu in Windows Tk. Verified by driving real Tk: opening sends 0
commands, one click sends exactly 1, and none follow.

**Do not send the listing as one datagram.** macOS caps UDP datagrams at
9216 bytes (`net.inet.udp.maxdgram`) and off-box the path MTU is far lower;
an 81-track library builds a 10 KB listing, so the send failed with EMSGSIZE,
the error was swallowed, and the browser showed nothing.
`_send_library()` splits at `LIST_CHUNK_BYTES` and `request_list()`
reassembles, reporting any lost chunk instead of silently showing a short
list.

The main loop re-scans the songs folder before every track and advances by
the *identity* of the last track played, not by an index — so a list that
changes underneath (song added, song deleted) keeps its position instead of
skipping or repeating. An empty folder waits on `idle_values()`.

`control_listener()` accepts `next`/`pause`/`quit` over UDP (`CONTROL_PORT` 6460)
into the same key queue as `[n]`/`[q]`; `rig_preview.py`'s SKIP/STOP buttons
send to it on the Art-Net source host. SIGTERM/SIGHUP are turned into
KeyboardInterrupt so a killed show still blacks out (Art-Net is stateless —
without it the fixtures freeze on the last frame).

**No orphans:** `run_show.py --preview` supervises both children and kills
one when the other exits, and `rig_preview.py`'s window-close handler calls
`shutdown_show()` (control-port `quit` first so the rig blacks out properly,
then a local-only `pkill` fallback; never kills a show on another host).
Both are needed: **a Python signal handler does NOT run while blocked in
Tk's mainloop on macOS** — measured, the process just dies — so the preview
cannot clean up after itself on SIGTERM and the launcher must supervise.

`run_show.py --preview` runs the real show AND the preview window: the show
mirrors every Art-Net frame to 127.0.0.1:6455 (`--preview-port`, one packet
built with a single sequence number, sent to each target) because Daslight
holds 6454 exclusively and a second listener there gets nothing. The
launcher probes for a Tk>=8.6 interpreter (`find_tk_python()`) instead of
assuming `sys.executable` can open a window, and terminates the preview when
the show exits. The preview's buttons already reach the show over the
control port, so they drive the live rig.

`run_show.py` is the venue-facing launcher: checks Python, auto-installs
packages, detects Daslight and its ports, then runs the show. `--check`
diagnoses without launching; `--osc-setup` walks the 20 OSC mappings.

`prewarm()` analyses every track under a path with no cache entry;
`run_show.py` launches it as a NICED SEPARATE PROCESS alongside the show
(a thread would fight the 40 fps frame clock for the GIL — measured 40.1 fps
with it running) and kills it on exit. `--no-prewarm` disables, `--prewarm`
does the library up front and exits. Cache writes go via a temp file and
`os.replace`, because the prewarm and the show can now target the same key
at once and a half-written pickle would poison the cache.

`analyse_cached()` memoises analysis to `.cache/` (~30s -> ~2s per track).
The key covers file mtime/size plus FPS, SECTION_SECONDS and BANDS, so
changing those invalidates it; the feel knobs apply live and do not.

The user's stated taste (2026-08-21): glow by default, fast only when the
drums are driving, and a bell "ding" should be a light that shines and
fades — never a flash. Measured: visible reversals fell from ~9/s to
2–5/s; a ding fades 1.00 → 0.45 over 1.6 s. Don't reintroduce per-frame
flux into the dimmer drive.

They then asked for one wheel and one forest light to hit on ANY drum
(2026-08-21). That is what `perc` is. Adding it naively pushed the rhythm
lights back to 9/s; the accent gate brought them to 5.3/s and 3.8/s, i.e.
the same rates as the version they approved, while responding to the whole
kit. **If you retune `perc`, re-measure visible reversals/s per section and
keep the rhythm lights under ~7/s.**

`PALETTES` holds the swappable hue arcs (base / neon / ember / ocean /
tropical), each with an optional `sat` range that overrides the zone
saturation clamp, and a `note` saying which surfaces it suits. `auto` is the default; `base` is the
fixed surface-aware one; the others deliberately break that
rule (ocean puts cool light on the rust-red wheel, ember olives the
foliage) — say so rather than quietly presenting them as equivalent.
`apply_palette()` only rewrites `ZONE_ARC`/`PALETTE_SAT`, which `frame()`
reads live, so a palette change needs NO engine rebuild and the hues simply
drift across. Scene modes never override `ZONE_ARC`, so the two selectors
are independent. All five verified at 0 hue-separation violations.

`auto` palette: `_classify_sections()` scores each section on loudness,
density and BAND BALANCE (highs/(highs+bass)) — not `brightness`, which
`_norm()` clips at the 95th percentile so every section lands in 0.89-0.96
and ember never fired. Short sections are excluded from the normalising
scale but still classified. `_auto_timeline()` expands to per-frame choices
and enforces `AUTO_PALETTE_DWELL_S`; the FIRST run has no predecessor and
takes the next run's palette (a 1 s opening section slipped through
otherwise). `frame()` morphs `self._arc` toward the target at
`AUTO_ARC_MORPH` so palettes cross-fade instead of snapping. `ocean` is
excluded from auto on purpose (cool light on rust-red wood reads muddy).
Verified over an 11-track library: 0 dwell violations.

`mid-instrumental` inherits every value from `mid` and sets
`INSTRUMENT_MODE`. `_assign_instruments()` re-picks the four most active
NMF parts every `INSTR_PERIOD_BEATS`, sorts them by centroid (two lowest ->
wheel, two highest -> forest) and stitches each light a composite source
plus a frame->strength map of that part's OWN onsets (stored per part as
`onsets`/`onset_strength`). In this mode `_gate_source`, the body and the
blooms all come from the assigned instrument, `forest_b` is NOT reserved
for dings, and lag is forced to 0 so each light sits exactly on its
instrument. NOTE: scene modes replace `BLOOM_HALF_LIFE` wholesale, so
`_decay` falls back for `beat`/`instr` — a mode whose dict lacks a kind used
to KeyError mid-track.

`mid-instrumental-v2` is v1 made exact and is a SEPARATE mode on purpose —
v1 is untouched so the two can be compared live. Parts carry extra fields
for it: `activation_raw` (unsmoothed, normalised to that part's own p97) and
`onsets_bt`/`onset_bt_strength` (BACKTRACKED to the note's attack, with the
strength taken from the following peak, not the backtracked foot). v2
smooths the raw activation IN THE ENGINE at `INSTR_BODY_SMOOTH_FRAMES`, so
that trade-off is retunable without a re-analysis — do not bake it back into
the cache. v2 also skips v1's global `_norm` of the stitched source, since
each part is already normalised to itself. Measured 66% -> 78% of notes hit
within 75 ms, at the cost of ~4.7 moves/s vs mid's ~3.4.

`SCENE_MODES` holds three shows in cycle order: `base` (the garden), `mid`
(lively pop/rock — every value between the other two) and `punchy` (dancefloor
— fast envelopes, short blooms, `BEAT_BLOOM` on every beat, gates opened to
0.70-0.80 duty, ~70 strobe bursts/min). `apply_scene_mode()` overlays the
overrides onto the module globals (`_SCENE_DEFAULTS` holds the base values
so switching back is exact). A switch mid-song rebuilds `ShowEngine` and
resumes at the same frame. Measured base/mid/punchy on the test track: strobe
5.5 / 22.5 / 70.6 per min, big jumps 0.20 / 0.69 / 1.04 per s, mean dimmer
0.26 / 0.32 / 0.38 — mid verified to sit between on every metric.
The user asked for punchy with "no speed limit" (2026-08-21). It is NOT
uncapped: `STROBE_MIN_GAP_S` 0.40 bounds the instantaneous rate at 2.5 Hz and
`STROBE_MAX_PER_MIN` 80 bounds the average at 1.33 Hz, both under the ~3 Hz
photosensitive-epilepsy threshold, and the forest never strobes. This was
explained to them rather than done silently — keep it that way.

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
- soundfile/libsndfile cannot decode AAC. An M4A named `.mp3` (a common
  iTunes export) fails with "Format not recognised", which says nothing
  useful — `decode_hint()` sniffs the header and tells the operator what the
  file really is and how to convert it. Keep that message actionable.

## Status

Verified on a real track (librosa 1.0, numpy 2.5): analysis, engine, hue
separation, 40 fps frame clock, Art-Net packet structure, channel placement,
blackout-on-exit, and the analysis cache all work.

**Still unverified: whether Daslight accepts Art-Net INPUT and forwards it to
the DVC GOLD.** Port 6454 is open. An ArtPoll from the same machine got no
visible reply, but that is NOT evidence either way: when Daslight and the
poller share a host, Daslight's own 6454 socket can swallow the reply.
`--artnet-discover` now also listens on its sending socket, which helps only
if the node replies to the sender's port. The only authoritative check is
`python run_show.py --artnet-test` at the rig.
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
