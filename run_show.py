#!/usr/bin/env python3
r"""
run_show.py — one-command launcher for the La Rueda light show.

Tomorrow, on the venue laptop, this should be the whole procedure:

    1. install Python 3.10+  (tick "Add Python to PATH")
    2. put the mp3s in the songs\ folder next to this file
    3. double-click run_show.py   (or: python run_show.py)

It installs its own dependencies, finds Daslight, picks an output path,
analyses every track, and plays them one after another with the lights.

    python run_show.py              # do everything
    python run_show.py --check      # diagnose only, touch nothing
    python run_show.py --simulate   # no DMX, coloured bars in the terminal
    python run_show.py --preview     # live show AND the preview window
    python run_show.py --prewarm     # analyse the whole library, then exit
    python run_show.py --artnet-test # light each fixture in turn — proves Art-Net
    python run_show.py --osc-setup   # guided Map OSC wizard (fallback path)
    python run_show.py --artnet-port 6455   # target rig_preview.py instead of Daslight
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SONGS = os.path.join(HERE, "songs")
DEPS = ["librosa>=1.0", "numpy>=2.0", "python-osc>=1.8", "sounddevice>=0.4"]
PREVIEW_PORT = 6455
AUDIO_EXTS = (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".aac")

C_OK, C_WARN, C_ERR, C_DIM, C_OFF = "\033[32m", "\033[33m", "\033[31m", "\033[90m", "\033[0m"
if os.name == "nt":
    os.system("")          # enable ANSI on Windows terminals


def say(sym, colour, msg):
    print(f"  {colour}{sym}{C_OFF} {msg}")


def ok(m):   say("OK  ", C_OK, m)
def warn(m): say("WARN", C_WARN, m)
def err(m):  say("FAIL", C_ERR, m)


# ---------------------------------------------------------------------------
# 1. Python + dependencies
# ---------------------------------------------------------------------------
def check_python():
    v = sys.version_info
    if v < (3, 10):
        err(f"Python {v.major}.{v.minor} is too old — need 3.10+")
        return False
    ok(f"Python {v.major}.{v.minor}.{v.micro}")
    return True


def missing_deps():
    missing = []
    for mod, spec in [("librosa", DEPS[0]), ("numpy", DEPS[1]),
                      ("pythonosc", DEPS[2]), ("sounddevice", DEPS[3])]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(spec)
    return missing


def ensure_deps(auto=False):
    missing = missing_deps()
    if not missing:
        ok("All Python packages present")
        return True
    warn(f"Missing packages: {', '.join(m.split('>')[0] for m in missing)}")
    print(f"\n  These need to be installed once (~200 MB, a few minutes).")
    if not auto:
        if input("  Install them now? [Y/n] ").strip().lower() in ("n", "no"):
            print("  Skipped. Install manually:  pip install -r requirements.txt")
            return False
    print()
    r = subprocess.run([sys.executable, "-m", "pip", "install", *missing])
    if r.returncode != 0:
        err("pip failed — try:  pip install -r requirements.txt")
        return False
    still = missing_deps()
    if still:
        err(f"Still missing after install: {still}")
        return False
    ok("Packages installed")
    return True


# ---------------------------------------------------------------------------
# 2. Daslight
# ---------------------------------------------------------------------------
def daslight_ports():
    """UDP ports Daslight holds open, cross-platform. Empty if not running."""
    try:
        if os.name == "nt":
            tl = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Daslight*", "/FO", "CSV"],
                                capture_output=True, text=True, timeout=15).stdout
            pids = {ln.split('","')[1] for ln in tl.splitlines()[1:] if '","' in ln}
            if not pids:
                return None, []
            ns = subprocess.run(["netstat", "-ano", "-p", "UDP"],
                                capture_output=True, text=True, timeout=20).stdout
            ports = []
            for ln in ns.splitlines():
                f = ln.split()
                if len(f) >= 4 and f[-1] in pids and ":" in f[1]:
                    try:
                        ports.append(int(f[1].rsplit(":", 1)[1]))
                    except ValueError:
                        pass
            return True, sorted(set(ports))
        else:
            pg = subprocess.run(["pgrep", "-f", "Daslight"],
                                capture_output=True, text=True, timeout=10).stdout.split()
            if not pg:
                return None, []
            ports = []
            for pid in pg:
                out = subprocess.run(["lsof", "-nP", "-iUDP", "-a", "-p", pid],
                                     capture_output=True, text=True, timeout=15).stdout
                for ln in out.splitlines()[1:]:
                    try:
                        ports.append(int(ln.rsplit(":", 1)[-1].strip()))
                    except ValueError:
                        pass
            return True, sorted(set(ports))
    except Exception:
        return None, []


def check_daslight():
    running, ports = daslight_ports()
    if not running:
        err("Daslight is not running — start it and open Pro_La Rueda_vr_thl.dvc")
        return None
    ok(f"Daslight running (UDP {', '.join(str(p) for p in ports) or 'no ports'})")
    if 6454 in ports:
        ok("Art-Net port 6454 open — raw DMX path available, no mapping needed")
    else:
        warn("Art-Net port 6454 NOT open — enable Art-Net input in Daslight, "
             "or fall back to --osc-setup")
    if 7000 in ports:
        ok("OSC port 7000 open (fallback path)")
    return ports


# ---------------------------------------------------------------------------
# 3. Songs
# ---------------------------------------------------------------------------
def check_songs():
    """Songs anywhere under songs/, including set-list subfolders."""
    if not os.path.isdir(SONGS):
        os.makedirs(SONGS, exist_ok=True)
        warn(f"Created {SONGS} — put the mp3s in it")
        return []
    tracks, folders = [], []
    for dirpath, dirnames, filenames in os.walk(SONGS):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        hits = [f for f in sorted(filenames)
                if f.lower().endswith(AUDIO_EXTS) and not f.startswith(".")]
        tracks += [os.path.join(dirpath, f) for f in hits]
        rel = os.path.relpath(dirpath, SONGS)
        if hits and rel not in (".", os.curdir):
            folders.append((rel, len(hits)))
    if not tracks:
        err(f"No audio files in {SONGS} (or any folder inside it)")
        return []
    ok(f"{len(tracks)} track(s) found")
    if folders:
        ok(f"{len(folders)} set list(s): "
           + ", ".join(f"{name} ({n})" for name, n in folders))
    loose = [t for t in tracks if os.path.dirname(t) == SONGS]
    for t in loose[:6]:
        print(f"       {C_DIM}{os.path.basename(t)}{C_OFF}")
    if len(tracks) > len(loose[:6]):
        print(f"       {C_DIM}... {len(tracks) - len(loose[:6])} more"
              f"{' in set lists' if folders else ''}{C_OFF}")
    return tracks


def cached_count():
    d = os.path.join(HERE, ".cache")
    return len([f for f in os.listdir(d) if f.endswith(".pkl")]) if os.path.isdir(d) else 0


# ---------------------------------------------------------------------------
# 3b. The preview window
# ---------------------------------------------------------------------------
def find_tk_python():
    """A Python that can actually open the preview window (needs Tk >= 8.6).

    On Windows the running interpreter is normally fine. On macOS it often is
    not: Homebrew Python ships no tkinter at all, and Apple's /usr/bin/python3
    has Tk 8.5, which draws a black window.
    """
    probe = ("import tkinter,sys; "
             "sys.exit(0 if tkinter.TkVersion >= 8.6 else 1)")
    candidates = [sys.executable]
    if os.name == "nt":
        candidates += ["python", "pythonw"]
    else:
        candidates += [
            os.path.expanduser("~/miniforge3/bin/python"),
            os.path.expanduser("~/miniconda3/bin/python"),
            os.path.expanduser("~/anaconda3/bin/python"),
            "/usr/local/bin/python3", "/opt/homebrew/bin/python3", "python3",
        ]
    seen = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        try:
            if subprocess.run([cand, "-c", probe], capture_output=True,
                              timeout=20).returncode == 0:
                return cand
        except Exception:
            continue
    return None


def start_prewarm():
    """Analyse any un-analysed tracks in the background while the show plays.

    A separate process, not a thread: analysis is heavy numpy/librosa work and
    a thread would compete with the 40 fps frame clock. Niced down so the show
    always wins the CPU.
    """
    cmd = [sys.executable, os.path.join(HERE, "rueda_lights.py"), SONGS, "--prewarm"]
    if os.name != "nt":
        cmd = ["nice", "-n", "15"] + cmd
    try:
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, **kw)
    except Exception:
        return None


def start_preview(port, control_port):
    """Launch rig_preview.py alongside the show. Returns the process or None."""
    py = find_tk_python()
    if not py:
        warn("No Python with Tk 8.6 found — skipping the preview window.")
        print("       (the show itself is unaffected; install a python.org or"
              " conda Python to get it)")
        return None
    cmd = [py, os.path.join(HERE, "rig_preview.py"), "--port", str(port)]
    if control_port:
        cmd += ["--control-port", str(control_port)]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok(f"Preview window started (Tk python: {os.path.basename(py)})")
        return proc
    except Exception as e:
        warn(f"Could not start the preview: {e}")
        return None


# ---------------------------------------------------------------------------
# 4. OSC mapping wizard (only needed if Art-Net does not work)
# ---------------------------------------------------------------------------
def osc_setup(port=7000):
    import rueda_lights as R
    from pythonosc.udp_client import SimpleUDPClient
    import threading, time

    addresses = R.osc_addresses("rgb")
    print(f"\n{'='*66}\n  GUIDED OSC MAPPING — {len(addresses)} faders\n{'='*66}")
    print("""
  For each address below:
     1. In Daslight:  Mappings > Map OSC
     2. Click the fader named in the prompt
     3. Press Enter here to move to the next one

  The address wiggles continuously while you map it, so Daslight can
  see it. Press Ctrl+C at any point to stop; progress is kept.
""")
    client = SimpleUDPClient("127.0.0.1", port)
    stop = threading.Event()
    current = [addresses[0]]

    def wiggler():
        v = 0.1
        while not stop.is_set():
            v = 0.9 if v < 0.5 else 0.1
            try:
                client.send_message(current[0], v)
            except Exception:
                pass
            time.sleep(0.35)

    threading.Thread(target=wiggler, daemon=True).start()
    done = []
    try:
        for i, addr in enumerate(addresses, 1):
            current[0] = addr
            light, fader = addr.strip("/").split("/")
            nice = {"wheel_a": "Luz 1 - Rueda (addr 1)",
                    "wheel_b": "Luz 2 - rueda izq (addr 10)",
                    "forest_a": "Luz 3 - bosque (addr 30)",
                    "forest_b": "Luz 4 - Bosque (addr 20)"}[light]
            input(f"  [{i:2d}/{len(addresses)}] {addr:22s} -> {nice}, "
                  f"{fader.upper()} fader ... Enter when mapped ")
            done.append(addr)
    except KeyboardInterrupt:
        print("\n  Stopped.")
    finally:
        stop.set()
    print(f"\n  Mapped {len(done)}/{len(addresses)}. Save the Daslight project now.")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    print(f"\n{'='*66}\n  LA RUEDA — light show launcher\n{'='*66}\n")

    print(" Environment")
    py_ok = check_python()
    deps_ok = ensure_deps(auto="--yes" in args) if py_ok else False

    print("\n Daslight")
    simulate = "--simulate" in args
    ports = [] if simulate else (check_daslight() or [])
    if simulate:
        print(f"  {C_DIM}(skipped — simulate mode){C_OFF}")

    print("\n Music")
    tracks = check_songs()
    n_cached = cached_count()
    if n_cached:
        ok(f"{n_cached} track(s) already analysed (instant start)")
    elif tracks:
        warn("No analysis cache — first song waits ~30s while librosa "
             "compiles (once per install), then ~2s per track")

    # value flags handed straight through to rueda_lights.py
    extra = []
    for vf in ("--artnet-universe", "--artnet-port", "--ip", "--control-port", "--scene", "--palette"):
        if vf in args and args.index(vf) + 1 < len(args):
            extra += [vf, args[args.index(vf) + 1]]
    custom_target = "--artnet-port" in args or "--ip" in args

    if "--artnet-test" in args or "--artnet-discover" in args:
        if not deps_ok:
            sys.exit("\nInstall packages first.")
        flag = "--artnet-test" if "--artnet-test" in args else "--artnet-discover"
        return subprocess.run([sys.executable, os.path.join(HERE, "rueda_lights.py"),
                               flag, *extra]).returncode

    if "--prewarm" in args:
        if not deps_ok:
            sys.exit("\nInstall packages first.")
        print()
        return subprocess.run([sys.executable, os.path.join(HERE, "rueda_lights.py"),
                               SONGS, "--prewarm"]).returncode

    if "--osc-setup" in args:
        if not deps_ok:
            sys.exit("\nInstall packages first.")
        return osc_setup(7000 if 7000 in ports else 7000)

    if "--check" in args:
        print(f"\n{C_DIM}  Diagnostic only, nothing launched.{C_OFF}\n")
        return

    if not (py_ok and deps_ok and tracks):
        print(f"\n{C_ERR}  Not ready — fix the FAIL lines above.{C_OFF}\n")
        sys.exit(1)

    mode = "artnet"
    for flag in ("--mode",):
        if flag in args:
            mode = args[args.index(flag) + 1]
    if not simulate and not custom_target and 6454 not in ports and mode == "artnet":
        warn("Art-Net port not open; lights may not respond.")
        print("  If nothing lights up, enable Art-Net input in Daslight,")
        print("  or run:  python run_show.py --osc-setup")

    cmd = [sys.executable, os.path.join(HERE, "rueda_lights.py"), SONGS, "--mode", mode, *extra]
    if simulate:
        cmd.append("--simulate")
    for passthru in ("--shuffle", "--no-audio", "--no-loop"):
        if passthru in args:
            cmd.append(passthru)

    # --preview: the show keeps driving the real rig and ALSO mirrors every
    # frame to the preview window, which can pause / skip / switch mode.
    # Warm the rest of the library in the background, so skipping into a
    # track nobody has played yet does not stall the show.
    warmer = None
    if "--no-prewarm" not in args and not simulate:
        try:
            sys.path.insert(0, HERE)
            import rueda_lights as _R
            _, _tracks = _R.scan_library(SONGS)
            _missing = [t for t in _tracks if not _R.is_analysed(t)]
            if _missing:
                warmer = start_prewarm()
                if warmer is not None:
                    ok(f"Analysing {len(_missing)} track(s) in the background "
                       f"(low priority; the show is unaffected)")
        except Exception:
            pass

    preview = None
    if "--preview" in args and not simulate:
        pport = int(args[args.index("--preview-port") + 1]) if "--preview-port" in args else PREVIEW_PORT
        cport = int(args[args.index("--control-port") + 1]) if "--control-port" in args else 6460
        print()
        preview = start_preview(pport, cport)
        if preview is not None:
            cmd += ["--preview-port", str(pport)]

    print(f"\n{'='*66}\n  Starting show — [n] next  [p] pause  [m] mode  [q] quit\n{'='*66}\n")
    show = None
    try:
        show = subprocess.Popen(cmd)
        # Supervise both: whichever exits first takes the other with it, so
        # closing the preview window can never leave the audio and the rig
        # running with nothing driving them, and quitting the show can never
        # leave an orphaned window. Signals do not reach a Tk mainloop on
        # macOS, so this cannot be left to the preview alone.
        while True:
            if show.poll() is not None:
                break
            if preview is not None and preview.poll() is not None:
                print("\n  Preview window closed — stopping the show.")
                break
            time.sleep(0.25)
    except KeyboardInterrupt:
        pass
    finally:
        for proc in (show, preview, warmer):
            if proc is None or proc.poll() is not None:
                continue
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
