#!/usr/bin/env python3
r"""
rig_preview.py — an Art-Net receiver that draws the four fixtures as they
would look at the venue, in a window on this screen.

It stands in for Daslight + the DVC GOLD + the lights: it binds the Art-Net
port, decodes ArtDmx exactly as a DMX node would, reads the patched channels
(Luz 1 @1, Luz 2 @10, Luz 4 @20, Luz 3 @30: Dimmer/R/G/B/Strobe) and renders
    - the four fixtures' EMITTED colour (what the LED face shows)
    - the SCENE: rust-red water wheel lit by the wheel pair, palms uplit by the
      forest pair — so you see what the light does ON the surfaces
    - live DMX numbers, packet rate, and a NO SIGNAL warning

Pure standard library (socket + tkinter). No numpy, no pip.

    python3 rig_preview.py                # listen on 6454 (stand-in for Daslight)
    python3 rig_preview.py --port 6455    # when Daslight already holds 6454
    python3 rig_preview.py --no-window    # terminal bars only (no tkinter)

Then, in another terminal:
    python run_show.py --artnet-port 6455          # the real show, real Art-Net
    python run_show.py --artnet-test --artnet-port 6455

The window has MODE, PALETTE, SONGS, PAUSE, SKIP and STOP buttons (keys
m / c / l / p / n / q). SONGS opens a browser of the set lists (subfolders
of songs/) and their tracks: click a folder to loop only that folder,
double-click a song to play it now. They send "mode" / "palette" / "pause" / "next" / "quit" over UDP to the show's control
port (6460) on whichever machine the Art-Net is coming from — the same as
pressing those keys in the show's terminal. MODE cycles the
scene modes (base / mid / mid-instrumental / v2 / punchy / fable / fable-2) and PALETTE cycles the colour palettes
(base / neon / ember / ocean / tropical); the show replies with what it
adopted, which is shown in the status line.

Note: needs a Python with tkinter AND Tk >= 8.6. On macOS, Homebrew python has
no tkinter, and Apple's /usr/bin/python3 has Tk 8.5, which draws a BLACK window
on recent macOS. Use a conda/miniforge or python.org Python instead.
"""
import argparse
import os
import queue
import socket
import subprocess
import struct
import sys
import threading
import time

ARTNET_PORT = 6454
CONTROL_PORT = 6460        # the show listens here for "next" / "quit"

# Patched fixtures — identical to rueda_lights.LIGHTS, duplicated here so this
# file has zero imports from the engine and runs under any Python.
FIXTURES = [
    # name        daslight name            addr  zone      side
    ("wheel_a",  "1.Luz 1 - Rueda",        1,   "wheel",  "right"),
    ("wheel_b",  "4.Luz 2 - rueda izq",    10,  "wheel",  "left"),
    ("forest_a", "2.Luz 3 - bosque",       30,  "forest", "left"),
    ("forest_b", "3.Luz 4 - Bosque",       20,  "forest", "right"),
]
OFF = {"dimmer": 0, "red": 1, "green": 2, "blue": 3, "strobe": 6}

# What the light lands on (linear reflectance, R G B).
ALBEDO_WOOD = (0.78, 0.30, 0.14)     # rust-red wheel
ALBEDO_LEAF = (0.22, 0.62, 0.18)     # palm fronds
ALBEDO_GRASS = (0.28, 0.50, 0.16)
ALBEDO_STONE = (0.55, 0.55, 0.52)
AMBIENT = 0.012                      # moonless garden is not perfectly black


# ---------------------------------------------------------------------------
# Receiver
# ---------------------------------------------------------------------------
class ArtNetReceiver:
    def __init__(self, port, universe=None, bind_ip="0.0.0.0"):
        self.port, self.universe = port, universe
        self.dmx = bytearray(512)
        self.lock = threading.Lock()
        self.packets = 0
        self.last_seq = -1
        self.last_uni = None
        self.last_src = None
        self.last_time = 0.0
        self.polls = 0
        self.rate = 0.0
        self._rate_mark = (time.time(), 0)
        # Is somebody ALREADY on this port? SO_REUSEPORT below lets us bind
        # anyway, but the kernel then hands each unicast datagram to only ONE
        # of the sockets — so a leftover preview (or a second window) steals
        # every frame and this one sits at black "NO SIGNAL" forever with no
        # explanation. Probe with a plain socket first so we can SAY so.
        self.port_shared = False
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.bind((bind_ip, port))
        except OSError:
            self.port_shared = True
        finally:
            probe.close()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if hasattr(socket, "SO_REUSEPORT"):      # let --artnet-discover co-bind
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        self.sock.bind((bind_ip, port))
        self.sock.settimeout(0.5)
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                data, src = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) < 12 or data[:8] != b"Art-Net\0":
                continue
            op = int.from_bytes(data[8:10], "little")
            if op == 0x5000 and len(data) >= 18:                 # ArtDmx
                seq, uni = data[12], int.from_bytes(data[14:16], "little")
                if self.universe is not None and uni != self.universe:
                    continue
                length = int.from_bytes(data[16:18], "big")
                payload = data[18:18 + length]
                with self.lock:
                    self.dmx[:len(payload)] = payload
                    self.packets += 1
                    self.last_seq, self.last_uni = seq, uni
                    self.last_src, self.last_time = src, time.time()
            elif op == 0x2000:                                  # ArtPoll
                self.polls += 1
                self._poll_reply(src)

    def _poll_reply(self, src):
        """Answer discovery like a real node, so --artnet-discover can see us."""
        try:
            my_ip = socket.inet_aton(socket.gethostbyname(socket.gethostname()))
        except Exception:
            my_ip = socket.inet_aton("127.0.0.1")
        r = bytearray(239)
        r[0:8] = b"Art-Net\0"
        r[8:10] = struct.pack("<H", 0x2100)
        r[10:14] = my_ip
        r[14:16] = struct.pack("<H", self.port)
        r[16:18] = struct.pack(">H", 1)
        r[23] = 0xd0
        r[26:44] = b"rig_preview".ljust(18, b"\0")
        r[44:108] = b"La Rueda rig preview (software DMX node)".ljust(64, b"\0")
        r[108:172] = b"#0001 [0000] OK".ljust(64, b"\0")
        r[172:174] = struct.pack(">H", 1)
        r[174] = 0x80                   # port type: DMX512 output
        r[182] = 0x80                   # good output
        r[200] = 0x00                   # style: node
        for dest in {(src[0], self.port), src}:
            try:
                self.sock.sendto(bytes(r), dest)
            except OSError:
                pass

    def send_control(self, cmd, control_port):
        """Send a command to the show on the host the Art-Net came from.

        Waits briefly for a reply so a mode switch can report what the show
        actually adopted, rather than what we hoped it would.
        """
        host = self.last_src[0] if self.last_src else "127.0.0.1"
        try:
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tx.settimeout(0.35)
            tx.sendto(cmd.encode(), (host, control_port))
            reply = ""
            try:
                data, _ = tx.recvfrom(256)
                reply = data.decode(errors="replace").strip()
            except (socket.timeout, OSError):
                pass
            tx.close()
            if reply:
                return f"{reply}   ({host}:{control_port})"
            return f"sent {cmd.upper()} -> {host}:{control_port}"
        except OSError as e:
            return f"could not send {cmd}: {e}"

    def request_list(self, control_port, timeout=2.0):
        """Fetch the library, reassembling the numbered chunks it arrives in.

        The listing does not fit one datagram once a library gets real (macOS
        caps them at 9216 bytes), so the show sends 'LIST i/n' parts.
        """
        host = self.last_src[0] if self.last_src else "127.0.0.1"
        try:
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tx.settimeout(0.6)
            tx.sendto(b"list", (host, control_port))
        except OSError:
            return "", 0, 0
        parts, total, deadline = {}, None, time.time() + timeout
        while time.time() < deadline:
            try:
                data, _ = tx.recvfrom(65535)
            except (socket.timeout, OSError):
                break
            head, _, rest = data.decode(errors="replace").partition("\n")
            if not head.startswith("LIST"):
                continue
            try:
                idx, tot = head.split()[1].split("/")
                idx, tot = int(idx), int(tot)
            except (IndexError, ValueError):
                idx, tot = 1, 1
            total = tot
            parts[idx] = rest
            if len(parts) == total:
                break
        tx.close()
        if not parts:
            return "", 0, 0
        body = "\n".join(parts[k] for k in sorted(parts))
        return body, len(parts), total or len(parts)

    def request(self, cmd, control_port, timeout=0.8, bufsize=65535):
        """Send a command and return the show's reply text (may be multi-line)."""
        host = self.last_src[0] if self.last_src else "127.0.0.1"
        try:
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tx.settimeout(timeout)
            tx.sendto(cmd.encode(), (host, control_port))
            data, _ = tx.recvfrom(bufsize)
            tx.close()
            return data.decode(errors="replace")
        except Exception:
            return ""

    def snapshot(self):
        with self.lock:
            now = time.time()
            t0, n0 = self._rate_mark
            if now - t0 >= 1.0:
                self.rate = (self.packets - n0) / (now - t0)
                self._rate_mark = (now, self.packets)
            vals = {}
            for name, dname, addr, zone, side in FIXTURES:
                b = addr - 1
                vals[name] = {k: self.dmx[b + o] for k, o in OFF.items()}
            age = now - self.last_time if self.last_time else None
            return vals, dict(packets=self.packets, seq=self.last_seq, uni=self.last_uni,
                              src=self.last_src, age=age, rate=self.rate, polls=self.polls)


# ---------------------------------------------------------------------------
# Colour maths
# ---------------------------------------------------------------------------
def emitted(v):
    """Linear RGB (0-1) actually leaving the fixture: dimmer scales RGB."""
    d = v["dimmer"] / 255.0
    return (v["red"] / 255.0 * d, v["green"] / 255.0 * d, v["blue"] / 255.0 * d)


def lit(surface, light, gain=1.0):
    return tuple(min(1.0, AMBIENT + gain * s * l) for s, l in zip(surface, light))


def add(*cols):
    return tuple(min(1.0, sum(c[i] for c in cols)) for i in range(3))


def hexcol(lin):
    """sRGB hex for a linear colour, quantised to steps of 4/255: invisible on
    screen, and it lets the preview skip redrawing items that did not
    visibly change."""
    # linear light -> sRGB for the screen, so a 10% LED is visible like in life
    return "#%02x%02x%02x" % tuple(
        min(255, 4 * int(round(255 * max(0.0, min(1.0, c)) ** (1 / 2.2) / 4))) for c in lin)


# Redraw period. The show runs at 40 fps; drawing the scene at 30 fps is
# indistinguishable on screen and is the one lever on the preview's CPU —
# the canvas repaints whole every tick because the lights move every tick,
# so skipping unchanged items barely helps (measured 24.6% -> 24.2%).
TICK_MS = 33


def strobe_on(strobe_val, frame):
    """Approximate the fixture's strobe channel: higher value = faster flash."""
    if strobe_val <= 0:
        return True
    hz = 1.0 + 11.0 * strobe_val / 255.0
    half = max(1, int(round(1000.0 / TICK_MS / hz / 2)))
    return (frame // half) % 2 == 0


# ---------------------------------------------------------------------------
# Window
# ---------------------------------------------------------------------------
def ui_font(size, weight="normal", slant="roman"):
    """A UI font family that exists on the host.

    "Helvetica" is a macOS face and "Menlo" is macOS-only; on Windows Tk
    substitutes something arbitrary. The show runs on a Windows laptop, so
    pick the native face per platform.
    """
    if sys.platform == "win32":
        fam = "Segoe UI"
    elif sys.platform == "darwin":
        fam = "Helvetica"
    else:
        fam = "DejaVu Sans"
    return (fam, size, weight, slant) if slant != "roman" else (fam, size, weight)


def mono_font(size):
    fam = ("Consolas" if sys.platform == "win32"
           else "Menlo" if sys.platform == "darwin" else "DejaVu Sans Mono")
    return (fam, size)


def _button(parent, text, command, font=None,
            padx=16, pady=7, base="#1b222b", fg="#dfe6ee"):
    """A button whose WHOLE area is clickable.

    macOS renders tk.Button as a native Aqua control with its own fixed hit
    region: enlarging it with padx/pady and a bigger font makes the drawn
    button bigger than the clickable one, so only the middle responds. A
    Frame+Label with explicit bindings has no such split, and styles
    consistently on a dark background.
    """
    import tkinter as tk
    font = font or ui_font(12, "bold")
    hover, press = "#26303c", "#2d6cdf"
    frame = tk.Frame(parent, bg=base, highlightthickness=1,
                     highlightbackground="#2a323d", cursor="hand2")
    label = tk.Label(frame, text=text, bg=base, fg=fg, font=font,
                     padx=padx, pady=pady, cursor="hand2")
    label.pack(fill="both", expand=True)

    def paint(colour):
        for w in (frame, label):
            try:
                w.configure(bg=colour)
            except tk.TclError:
                pass

    def on_press(_e):
        paint(press)

    def on_release(e):
        paint(hover if _inside(e) else base)
        if _inside(e):
            command()
        return "break"

    def _inside(e):
        x, y = e.x_root - frame.winfo_rootx(), e.y_root - frame.winfo_rooty()
        return 0 <= x < frame.winfo_width() and 0 <= y < frame.winfo_height()

    for w in (frame, label):
        w.bind("<Button-1>", on_press)
        w.bind("<ButtonRelease-1>", on_release)
        w.bind("<Enter>", lambda _e: paint(hover))
        w.bind("<Leave>", lambda _e: paint(base))
    return frame



def run_window(rx, title, control_port=CONTROL_PORT, keep_show=False):
    import tkinter as tk
    if sys.platform == "darwin" and tk.TkVersion < 8.6:
        print(f"WARNING: Tk {tk.TkVersion} on macOS renders a black window. "
              f"Run this with a conda/miniforge or python.org Python (Tk 8.6).", flush=True)
    W, H = 1180, 680
    root = tk.Tk()
    root.title(title)
    root.configure(bg="#07090c")
    # Come to the front. A Tk window started from a background shell on macOS
    # otherwise opens behind everything and looks like it never appeared.
    root.lift()
    root.attributes("-topmost", True)
    root.after(1500, lambda: root.attributes("-topmost", False))
    root.focus_force()
    if sys.platform == "darwin":
        import subprocess
        subprocess.Popen(["osascript", "-e",
                          'tell application "System Events" to set frontmost of '
                          '(first process whose unix id is %d) to true' % __import__("os").getpid()],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cv = tk.Canvas(root, width=W, height=H, bg="#07090c", highlightthickness=0)
    cv.pack()
    frame = [0]
    alive = [True]

    def on_root_close():
        # Closing this window ends the whole show: leaving the audio and the
        # rig running with no window is exactly the orphan we must not have.
        alive[0] = False
        try:
            cv.itemconfig(status, text="stopping the show …")
            root.update_idletasks()
        except tk.TclError:
            pass
        shutdown_show(rx, control_port, hard=not keep_show)
        try:
            root.destroy()
        except tk.TclError:
            pass
    root.protocol("WM_DELETE_WINDOW", on_root_close)
    notice = {"text": "", "until": 0.0}

    # Every UDP round-trip (send_control waits up to 0.35 s for a reply,
    # request_list up to 2 s) used to run ON the Tk thread. While it waited
    # the event loop was frozen: clicks landed on a dead window, the button
    # never repainted, the redraw stalled. Everything network now runs in a
    # worker thread and hands its result back with root.after().
    # Results come back through a queue the redraw tick drains on the Tk
    # thread — not root.after() from the worker, which Tkinter only allows
    # while the main thread is inside mainloop() and is racy even then.
    inbox = queue.Queue()

    def _bg(fn, done=None):
        def work():
            try:
                res = fn()
            except Exception as exc:          # never lose a failure silently
                res = f"error: {exc}"
            if done is not None:
                inbox.put((done, res))
        threading.Thread(target=work, daemon=True).start()

    def _drain_inbox():
        while True:
            try:
                done, res = inbox.get_nowait()
            except queue.Empty:
                return
            try:
                done(res)
            except tk.TclError:
                pass                           # the window it was for is gone

    # --- transport: SKIP / STOP talk to the show's control port ---------------
    bar = tk.Frame(root, bg="#07090c", pady=6)
    bar.pack(fill="x")

    def control(cmd):
        notice["text"] = f"{cmd.upper()} …"
        notice["until"] = time.time() + 2.5

        def done(text):
            notice["text"] = text
            notice["until"] = time.time() + 2.5
        _bg(lambda: rx.send_control(cmd, control_port), done)

    def mk(text, cmd, side):
        b = _button(bar, text, lambda: control(cmd))
        b.pack(side=side, padx=8, pady=2)
        return b
    mk("MODE   (m)", "mode", "left")
    mk("PALETTE   (c)", "palette", "left")
    # lambda, not a direct reference: open_library is defined further down,
    # and Button() would evaluate the name right here
    def _open_library_safe():
        try:
            open_library()
        except Exception as exc:                  # never fail silently
            notice["text"] = f"library window failed: {exc}"
            notice["until"] = time.time() + 5
            print(f"library window failed: {exc}", flush=True)

    _button(bar, "SONGS   (l)", _open_library_safe).pack(side="left", padx=8, pady=2)
    mk("PAUSE / RESUME   (p)", "pause", "left")
    mk("SKIP TRACK   (n)", "next", "left")
    mk("STOP SHOW   (q)", "quit", "right")
    tk.Label(bar, text=f"control -> UDP {control_port} on the Art-Net source",
             fg="#5f6872", bg="#07090c", font=ui_font(9)).pack(side="left", padx=8)
    root.bind("<n>", lambda e: control("next"))
    root.bind("<q>", lambda e: control("quit"))
    root.bind("<p>", lambda e: control("pause"))
    root.bind("<m>", lambda e: control("mode"))
    root.bind("<c>", lambda e: control("palette"))
    root.bind("<l>", lambda e: _open_library_safe())
    root.bind("<space>", lambda e: control("pause"))

    # --- static layout -------------------------------------------------------
    cv.create_text(W // 2, 18, text="LA RUEDA — rig preview  (what the lights would do)",
                   fill="#cfd6df", font=ui_font(15, "bold"))
    # fixture panels
    PX = [90, 340, 720, 970]
    panel, ptext, pname = {}, {}, {}
    for (name, dname, addr, zone, side), x in zip(FIXTURES, PX):
        cv.create_text(x + 70, 46, text=f"{dname}   DMX {addr}", fill="#8a93a0", font=ui_font(11))
        panel[name] = cv.create_rectangle(x, 58, x + 140, 128, fill="#000000", outline="#2a2f36", width=2)
        ptext[name] = cv.create_text(x + 70, 146, text="", fill="#9aa3ae", font=mono_font(10))
        pname[name] = cv.create_text(x + 70, 162, text=f"{name}  ({zone}, {side})", fill="#5f6872", font=ui_font(9))
    cv.create_line(560, 40, 560, 170, fill="#1c2128")
    cv.create_text(215, 182, text="WHEEL — bass (b lags half a beat)", fill="#6f7883", font=ui_font(10, "normal", "italic"))
    cv.create_text(845, 182, text="FOREST — mids / highs (b lags a quarter)", fill="#6f7883", font=ui_font(10, "normal", "italic"))

    # scene: ground
    cv.create_rectangle(0, 560, W, H, fill="#0b0f0a", outline="")
    # river (static, faintly lit)
    cv.create_polygon(0, 575, 560, 560, 560, 600, 0, 620, fill="#10161c", outline="")
    # weir (white water, static)
    weir = cv.create_rectangle(440, 330, 480, 560, fill="#1a2028", outline="")
    # wheel: hub at (250, 420), r 125
    WX, WY, WR = 250, 420, 125
    wheel_l = cv.create_arc(WX - WR, WY - WR, WX + WR, WY + WR, start=90, extent=180, fill="#000", outline="")
    wheel_r = cv.create_arc(WX - WR, WY - WR, WX + WR, WY + WR, start=-90, extent=180, fill="#000", outline="")
    spokes = []
    import math
    for k in range(16):
        a = 2 * math.pi * k / 16
        spokes.append(cv.create_line(WX, WY, WX + WR * math.cos(a), WY + WR * math.sin(a), fill="#000", width=3))
    rim = cv.create_oval(WX - WR, WY - WR, WX + WR, WY + WR, outline="#000", width=6)
    hub = cv.create_oval(WX - 14, WY - 14, WX + 14, WY + 14, fill="#000", outline="")
    # wheel beams from two ground fixtures
    beam_wb = cv.create_polygon(110, 560, WX - 60, WY - 40, WX - 110, WY + 60, fill="#000", outline="", stipple="gray25")
    beam_wa = cv.create_polygon(400, 560, WX + 110, WY + 60, WX + 60, WY - 40, fill="#000", outline="", stipple="gray25")
    fix_wb = cv.create_rectangle(100, 552, 120, 562, fill="#000", outline="#333")
    fix_wa = cv.create_rectangle(390, 552, 410, 562, fill="#000", outline="#333")

    # forest: two palm groups
    def palm(x, base_y, h, lean):
        trunk = cv.create_line(x, base_y, x + lean, base_y - h, fill="#000", width=5, smooth=True)
        fronds = []
        tipx, tipy = x + lean, base_y - h
        for ang in (-150, -120, -90, -60, -30, 0, 180):
            a = math.radians(ang)
            fx, fy = tipx + 70 * math.cos(a), tipy + 35 * math.sin(a) - 10
            fronds.append(cv.create_polygon(tipx, tipy, fx - 8, fy - 10, fx + 8, fy + 4, fill="#000", outline="", smooth=True))
        return trunk, fronds
    grpA = [palm(650, 560, 200, 10), palm(720, 560, 250, -15), palm(790, 560, 170, 20)]
    grpB = [palm(900, 560, 230, -10), palm(980, 560, 190, 15), palm(1060, 560, 260, -20)]
    pool_fa = cv.create_oval(660, 540, 780, 575, fill="#000", outline="")
    pool_fb = cv.create_oval(910, 540, 1030, 575, fill="#000", outline="")
    fix_fa = cv.create_rectangle(710, 552, 730, 562, fill="#000", outline="#333")
    fix_fb = cv.create_rectangle(960, 552, 980, 562, fill="#000", outline="#333")
    benches = [cv.create_rectangle(600 + i * 150, 585, 660 + i * 150, 595, fill="#000", outline="") for i in range(4)]
    # status
    status = cv.create_text(W // 2, H - 16, text="", fill="#8a93a0", font=mono_font(11))
    nosig = cv.create_text(W // 2, 360, text="", fill="#d9534f", font=ui_font(26, "bold"))

    def _tick_body():
        # The window may be closed (or the process signalled) between frames;
        # a pending after() callback would then fire against a destroyed
        # canvas and raise TclError: invalid command name ".!canvas".
        if not alive[0]:
            return
        try:
            if not root.winfo_exists():
                alive[0] = False
                return
        except tk.TclError:
            alive[0] = False
            return
        frame[0] += 1
        _drain_inbox()
        _ping()
        vals, st = rx.snapshot()
        # Text is the expensive part of the canvas: Tk re-lays out a text
        # item every time it changes, and the readouts and the status line
        # (sequence number!) changed every frame. Colours run at full rate,
        # text at ~4 Hz — nobody reads a number 30 times a second.
        text_tick = frame[0] % 8 == 0

        # Only touch a canvas item when what it shows actually changed: ~70
        # itemconfig calls per frame at 40 fps, most of them no-ops, is what
        # kept the fans busy.
        def put(item, **kw):
            for k, v in kw.items():
                if drawn.get((item, k)) != v:
                    drawn[(item, k)] = v
                    cv.itemconfig(item, **{k: v})

        dead = st["age"] is None or st["age"] > 1.0
        em = {}
        for name, v in vals.items():
            e = emitted(v) if not dead else (0, 0, 0)
            if not strobe_on(v["strobe"], frame[0]):
                e = (0, 0, 0)
            em[name] = e
            put(panel[name], fill=hexcol(e))
            if text_tick:
                put(ptext[name], text=f"dim {v['dimmer']:3d}  R {v['red']:3d} G {v['green']:3d} B {v['blue']:3d}"
                                      + (f"  STROBE {v['strobe']}" if v["strobe"] else ""))
        # wheel halves: left lit by wheel_b (izq), right by wheel_a
        put(wheel_l, fill=hexcol(lit(ALBEDO_WOOD, em["wheel_b"], 1.25)))
        put(wheel_r, fill=hexcol(lit(ALBEDO_WOOD, em["wheel_a"], 1.25)))
        both = add(em["wheel_a"], em["wheel_b"])
        dark_wood = hexcol(lit((0.30, 0.10, 0.05), both, 0.8))
        for s_ in spokes:
            put(s_, fill=dark_wood)
        put(rim, outline=dark_wood)
        put(hub, fill=dark_wood)
        put(beam_wb, fill=hexcol(tuple(c * 0.8 for c in em["wheel_b"])))
        put(beam_wa, fill=hexcol(tuple(c * 0.8 for c in em["wheel_a"])))
        put(fix_wb, fill=hexcol(em["wheel_b"]))
        put(fix_wa, fill=hexcol(em["wheel_a"]))
        # white water catches spill from both wheel lights
        put(weir, fill=hexcol(lit((0.85, 0.88, 0.92), both, 0.35)))
        # forest
        for (trunk, fronds), light in [(g, em["forest_a"]) for g in grpA] + [(g, em["forest_b"]) for g in grpB]:
            put(trunk, fill=hexcol(lit((0.35, 0.28, 0.20), light, 0.7)))
            leaf = hexcol(lit(ALBEDO_LEAF, light, 1.4))
            for f in fronds:
                put(f, fill=leaf)
        put(pool_fa, fill=hexcol(lit(ALBEDO_GRASS, em["forest_a"], 1.6)))
        put(pool_fb, fill=hexcol(lit(ALBEDO_GRASS, em["forest_b"], 1.6)))
        put(fix_fa, fill=hexcol(em["forest_a"]))
        put(fix_fb, fill=hexcol(em["forest_b"]))
        fb = add(tuple(c * 0.5 for c in em["forest_a"]), tuple(c * 0.5 for c in em["forest_b"]))
        stone = hexcol(lit(ALBEDO_STONE, fb, 0.6))
        for b_ in benches:
            put(b_, fill=stone)
        # status  (all canvas writes above/below may race with a close)
        if not text_tick:
            pass
        elif notice["text"] and time.time() < notice["until"]:
            put(status, text=notice["text"])
            put(nosig, text="" if not dead else ("NO SIGNAL" if st["packets"] == 0 else "SIGNAL LOST"))
        elif dead and rx.port_shared and st["packets"] == 0:
            # The commonest cause of a black window: another copy of the
            # preview (usually one left over from an earlier run) already
            # holds this port and is receiving every frame instead of us.
            put(nosig, text=f"UDP {rx.port} IS ALREADY IN USE")
            put(status, text=f"another program already holds UDP {rx.port} and is taking the "
                             f"frames — close the other preview window (or: pkill -f rig_preview.py) "
                             f"and start again")
        elif dead:
            put(nosig, text="NO SIGNAL" if st["packets"] == 0 else "SIGNAL LOST")
            put(status, text=f"listening on UDP {rx.port} — waiting for Art-Net …   "
                             f"(total {st['packets']} packets, {st['polls']} polls)")
        elif health["control"] is False:
            # Frames are arriving, so the show is running — but it never got
            # the control port, so nothing we click can reach it.
            put(nosig, text="")
            put(status, text=f"the show is NOT listening on UDP {control_port} — buttons and song "
                             f"picks will do nothing (another copy is probably still running)")
        else:
            put(nosig, text="")
            put(status, text=f"UDP {rx.port}   {st['rate']:5.1f} pkt/s   universe {st['uni']}   "
                             f"seq {st['seq']:3d}   from {st['src'][0]}   {st['packets']} packets")
        try:
            root.after(TICK_MS, tick)
        except tk.TclError:
            alive[0] = False

    drawn = {}                 # canvas item -> last options written
    # Is the show actually LISTENING for our buttons? It only replies to some
    # commands, so "no reply" cannot be used as a general failure signal —
    # but a dedicated ping can. Without this a show that failed to bind the
    # control port (another copy already had it) leaves every button and
    # every song click silently doing nothing.
    health = {"control": None, "pending": False, "next": 0.0}

    def _ping():
        if health["pending"] or time.time() < health["next"]:
            return
        health["pending"] = True
        health["next"] = time.time() + 3.0

        def done(res):
            health["pending"] = False
            health["control"] = bool(isinstance(res, str) and res.startswith("pong"))
        _bg(lambda: rx.request("ping", control_port, timeout=0.7), done)

    lib_win = [None]

    def open_library():
        """Browser for the set lists (subfolders of songs/) and their songs."""
        if lib_win[0] is not None:          # already open: just raise it
            try:
                lib_win[0].deiconify(); lib_win[0].lift(); lib_win[0].focus_force()
                return
            except tk.TclError:
                lib_win[0] = None
        win = tk.Toplevel(root)
        lib_win[0] = win
        win.title("La Rueda — set lists and songs")
        win.configure(bg="#0b0e12")
        win.minsize(760, 420)
        # On macOS a Toplevel often opens BEHIND its parent, which looks
        # exactly like the button doing nothing.
        try:
            win.geometry(f"820x520+{root.winfo_rootx() + 60}+{root.winfo_rooty() + 90}")
        except tk.TclError:
            pass
        win.lift()
        win.attributes("-topmost", True)
        win.after(700, lambda: win.attributes("-topmost", False))
        win.focus_force()

        def _closed():
            lib_win[0] = None
            win.destroy()
        win.protocol("WM_DELETE_WINDOW", _closed)

        # Selecting a row programmatically (to mark what is playing) also
        # fires <<ListboxSelect>>. Without this guard that re-sent a folder
        # command, which skipped a track and scheduled another refresh —
        # a feedback loop that made every click feel unreliable.
        quiet = [False]

        win.grid_rowconfigure(1, weight=1)
        win.grid_columnconfigure(0, weight=0, minsize=250)
        win.grid_columnconfigure(1, weight=1)

        tk.Label(win, text="SET LIST", fg="#cfd6df", bg="#0b0e12",
                 font=ui_font(11, "bold")).grid(row=0, column=0, sticky="w",
                                                      padx=(14, 6), pady=(12, 4))
        tk.Label(win, text="SONGS", fg="#cfd6df", bg="#0b0e12",
                 font=ui_font(11, "bold")).grid(row=0, column=1, sticky="w",
                                                      padx=(6, 14), pady=(12, 4))

        lf = tk.Frame(win, bg="#0b0e12")
        lf.grid(row=1, column=0, sticky="nsew", padx=(14, 6))
        fsb = tk.Scrollbar(lf)
        fsb.pack(side="right", fill="y")
        folders = tk.Listbox(lf, bg="#12171d", fg="#dfe6ee", selectbackground="#2d6cdf",
                             highlightthickness=0, activestyle="none", exportselection=False,
                             selectmode="browse", font=ui_font(14), yscrollcommand=fsb.set)
        folders.pack(side="left", fill="both", expand=True)
        fsb.config(command=folders.yview)

        rf = tk.Frame(win, bg="#0b0e12")
        rf.grid(row=1, column=1, sticky="nsew", padx=(6, 14))
        ssb = tk.Scrollbar(rf)
        ssb.pack(side="right", fill="y")
        songs = tk.Listbox(rf, bg="#12171d", fg="#dfe6ee", selectbackground="#2d6cdf",
                           highlightthickness=0, activestyle="none", exportselection=False,
                           selectmode="browse", font=ui_font(14), yscrollcommand=ssb.set)
        songs.pack(side="left", fill="both", expand=True)
        ssb.config(command=songs.yview)

        status = tk.Label(win, text="loading …", fg="#8a93a0", bg="#0b0e12",
                          font=ui_font(10), anchor="w")
        status.grid(row=2, column=0, columnspan=2, sticky="ew", padx=14, pady=(8, 2))

        data = {"folders": [], "songs": [], "active": None, "playing": None}
        busy = [False]
        # A picked track does not start instantly: the show has to ANALYSE it
        # the first time (librosa, ~20 s, and in fable-2 a transcription too).
        # The show says so in its reply, but the refresh 900 ms later used to
        # overwrite that line — so the operator saw a normal status bar and a
        # rig that ignored them for twenty seconds. Hold the waiting state
        # until the track actually starts, and keep watching for it.
        pending = {"rel": None, "since": 0.0, "note": ""}

        def refresh(_evt=None):
            """Ask the show for the listing in the background; fill the lists
            when it arrives. Never blocks the window."""
            if busy[0]:
                return
            busy[0] = True
            status.config(text="refreshing …")
            _bg(lambda: rx.request_list(control_port), _fill)

        def _fill(res):
            busy[0] = False
            try:
                if not win.winfo_exists():
                    return
            except tk.TclError:
                return
            if not isinstance(res, tuple):
                status.config(text=f"list failed: {res}")
                return
            body, got, total = res
            if not body:
                status.config(text=f"no reply from the show on UDP {control_port} — it is "
                                   f"probably not listening there (another copy still running?). "
                                   f"Songs cannot be picked until that is cleared.")
                return
            quiet[0] = True
            try:
                data["folders"], data["songs"] = [], []
                folders.delete(0, "end")
                songs.delete(0, "end")
                for line in body.splitlines():
                    parts = line.split("\t")
                    if parts[0] == "FOLDER" and len(parts) >= 3:
                        rel, n = parts[1], parts[2]
                        live = len(parts) > 3 and parts[3] == "*"
                        label = "All songs" if rel == "all" else rel
                        data["folders"].append(rel)
                        folders.insert("end", f" {'●' if live else '○'}  {label}   ({n})")
                        if live:
                            data["active"] = rel
                            folders.selection_clear(0, "end")
                            folders.selection_set(folders.size() - 1)
                    elif parts[0] == "SONG" and len(parts) >= 3:
                        rel, name = parts[1], parts[2]
                        live = len(parts) > 3 and parts[3] == "*"
                        data["songs"].append(rel)
                        songs.insert("end", f" {'▶' if live else ' '}  {name}")
                        if live:
                            data["playing"] = rel
                            songs.selection_clear(0, "end")
                            songs.selection_set(songs.size() - 1)
                            songs.see(songs.size() - 1)
            finally:
                win.after_idle(lambda: quiet.__setitem__(0, False))
            lost = "" if got == total else f"   ·   {total - got} chunk(s) lost, list may be short"
            now = data["playing"] or "—"
            if pending["rel"] and data["playing"] == pending["rel"]:
                pending["rel"] = None            # it started — back to normal
            if pending["rel"]:
                waited = time.time() - pending["since"]
                if waited > 180:
                    pending["rel"] = None
                    status.config(text=f"{os.path.basename(now)} — the pick never started; "
                                       f"try again or check the show's terminal")
                else:
                    status.config(text=f"starting {os.path.basename(pending['rel'])} … "
                                       f"{waited:.0f}s   ·   {pending['note']}")
                    win.after(1500, refresh)     # keep watching until it starts
                    return
            status.config(text=f"set list: {data['active'] or 'all'}   ·   "
                               f"{len(data['songs'])} song(s)   ·   playing: {now}{lost}")

        def pick_folder(_evt=None):
            if quiet[0]:
                return                      # our own selection_set, not a click
            sel = folders.curselection()
            if not sel:
                return
            rel = data["folders"][sel[0]]
            if rel == (data["active"] or "all"):
                return                      # already the active set list
            status.config(text=f"switching to set list {rel} …")
            _bg(lambda: rx.send_control(f"folder {rel}", control_port), _sent)

        def _sent(text):
            try:
                status.config(text=text)
                win.after(900, refresh)
            except tk.TclError:
                pass

        def pick_song(_evt=None):
            sel = songs.curselection()
            if not sel:
                status.config(text="click a song first, then PLAY (or double-click it)")
                return
            rel = data["songs"][sel[0]]
            pending.update(rel=rel, since=time.time(),
                           note="a track never played before is analysed first (~20 s)")
            status.config(text=f"starting {os.path.basename(rel)} …")
            _bg(lambda: rx.send_control(f"play {rel}", control_port), _sent_play)

        def _sent_play(text):
            """The show's reply says whether this track is cached or has to be
            analysed first; keep it as the reason shown while we wait."""
            try:
                if isinstance(text, str) and "analysing" in text:
                    pending["note"] = "first play: analysing the track (~20 s), then it starts"
                elif isinstance(text, str) and "(ready)" in text:
                    pending["note"] = "already analysed — starting now"
                status.config(text=f"starting {os.path.basename(pending['rel'] or '')} …   ·   "
                                   f"{pending['note']}")
                win.after(700, refresh)
            except tk.TclError:
                pass

        def show_pick(_evt=None):
            if quiet[0]:
                return
            sel = songs.curselection()
            if sel:
                status.config(text=f"selected: {os.path.basename(data['songs'][sel[0]])}"
                                   "   —   double-click, Enter or PLAY SELECTED to play it")

        folders.bind("<<ListboxSelect>>", pick_folder)
        folders.bind("<Return>", pick_folder)
        songs.bind("<<ListboxSelect>>", show_pick)
        songs.bind("<Double-Button-1>", pick_song)
        songs.bind("<Return>", pick_song)
        win.bind("<F5>", refresh)

        bar = tk.Frame(win, bg="#0b0e12")
        bar.grid(row=3, column=0, columnspan=2, sticky="ew", padx=14, pady=(4, 12))
        _button(bar, "PLAY SELECTED", pick_song,
                font=ui_font(11, "bold")).pack(side="left")
        _button(bar, "REFRESH", refresh,
                font=ui_font(11, "bold")).pack(side="left", padx=8)
        tk.Label(bar, text="double-click a song to play it (first play of a new track analyses it, ~20 s) "
                           "· click a set list to loop only that folder",
                 fg="#5f6872", bg="#0b0e12", font=ui_font(9)).pack(side="left", padx=12)
        _button(bar, "CLOSE", _closed,
                font=ui_font(11, "bold")).pack(side="right")
        refresh()

    def tick():
        try:
            _tick_body()
        except tk.TclError:
            alive[0] = False       # window went away mid-redraw; stop quietly

    def on_close():
        alive[0] = False
        try:
            root.destroy()
        except tk.TclError:
            pass
    root.protocol("WM_DELETE_WINDOW", on_close)

    tick()
    try:
        root.mainloop()
    except tk.TclError:
        pass                       # window torn down mid-callback


# ---------------------------------------------------------------------------
# Terminal fallback + stats
# ---------------------------------------------------------------------------
def bar(vals, st):
    cells = []
    for name, dname, addr, zone, side in FIXTURES:
        v = vals[name]; e = emitted(v); n = int(v["dimmer"] / 255 * 10)
        col = "\033[38;2;%d;%d;%dm" % tuple(int(255 * c ** (1 / 2.2)) for c in e)
        cells.append(f"{col}{'█' * n}{' ' * (10 - n)}\033[0m{'*' if v['strobe'] else ' '}")
    sig = "NO SIGNAL" if st["age"] is None or st["age"] > 1 else f"{st['rate']:4.0f}pkt/s"
    return f"{sig:>9s} WHEEL {' '.join(cells[:2])}  FOREST {' '.join(cells[2:])}"


def stats_printer(rx, interval=1.0):
    def loop():
        while True:
            time.sleep(interval)
            vals, st = rx.snapshot()
            if st["age"] is None:
                line = f"[preview] UDP {rx.port}: waiting for Art-Net (0 packets)"
            else:
                line = (f"[preview] {st['rate']:5.1f} pkt/s uni {st['uni']} seq {st['seq']:3d} | "
                        + "  ".join(f"{n}:d{vals[n]['dimmer']:3d}/{vals[n]['red']:3d},{vals[n]['green']:3d},{vals[n]['blue']:3d}"
                                    + ("/S" if vals[n]["strobe"] else "") for n, *_ in FIXTURES))
            print(line, flush=True)
    threading.Thread(target=loop, daemon=True).start()


def shutdown_show(rx, control_port, hard=True):
    """Stop the show when the preview goes away — no orphaned processes.

    Asks politely first: "quit" over the control port makes the show black
    the rig out and exit cleanly, which a kill cannot guarantee. The hard
    kill is only a fallback, and ONLY for a show on this machine — never
    reach across the network and kill someone else's.
    """
    try:
        rx.send_control("quit", control_port)
    except Exception:
        pass
    if not hard:
        return
    host = rx.last_src[0] if rx.last_src else "127.0.0.1"
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(f"show is on {host}; asked it to quit, not killing a remote machine",
              flush=True)
        return
    time.sleep(0.8)                       # give the clean exit a chance
    try:
        if os.name == "nt":
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | "
                 "Where-Object { $_.CommandLine -like '*rueda_lights.py*' } | "
                 "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        else:
            subprocess.run(["pkill", "-f", "rueda_lights.py"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15)
    except Exception:
        pass


def _install_signal_handlers(on_exit=None):
    """Exit quietly on SIGTERM/SIGINT — `pkill -f rig_preview.py` is the
    documented way to restart it, and that should not print a traceback."""
    import signal

    def _bye(signum, frame):
        if on_exit is not None:
            try:
                on_exit()
            except Exception:
                pass
        raise SystemExit(0)
    for sig in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, sig):
            try:
                signal.signal(getattr(signal, sig), _bye)
            except (ValueError, OSError):
                pass


# the receiver, so a signal handler can reach it to stop the show
_rx_ref = [None]


def main():
    if os.name == "nt":
        os.system("")        # enable ANSI escapes for the --no-window bars
    p = argparse.ArgumentParser(description="Art-Net receiver that previews the La Rueda rig")
    p.add_argument("--port", type=int, default=ARTNET_PORT)
    p.add_argument("--universe", type=int, default=None, help="only accept this universe (default: any)")
    p.add_argument("--no-window", action="store_true", help="terminal bars only")
    p.add_argument("--stats", action="store_true", help="print a stats line every second (also with window)")
    p.add_argument("--control-port", type=int, default=CONTROL_PORT,
                   help="the show's control port for the SKIP/STOP buttons")
    p.add_argument("--keep-show", action="store_true",
                   help="do NOT stop the show when this window closes "
                        "(default: closing the preview ends the show)")
    a = p.parse_args()

    def _on_signal():
        if not a.keep_show and _rx_ref[0] is not None:
            shutdown_show(_rx_ref[0], a.control_port)
    _install_signal_handlers(_on_signal)
    try:
        rx = ArtNetReceiver(a.port, a.universe)
        _rx_ref[0] = rx
    except OSError as e:
        sys.exit(f"Cannot bind UDP {a.port}: {e}\n"
                 f"Another app (Daslight?) holds it. Quit it, or use --port 6455 and run the\n"
                 f"show with:  python run_show.py --artnet-port 6455")
    print(f"rig_preview listening on UDP {a.port}"
          + (f" universe {a.universe}" if a.universe is not None else " (any universe)"))

    if a.no_window:
        try:
            while True:
                time.sleep(0.1)
                vals, st = rx.snapshot()
                print("\r" + bar(vals, st), end="", flush=True)
        except KeyboardInterrupt:
            print()
        return

    if a.stats:
        stats_printer(rx)
    try:
        run_window(rx, f"La Rueda rig preview — UDP {a.port}", a.control_port,
                   keep_show=a.keep_show)
    except ImportError:
        print("tkinter is not available in this Python. Falling back to terminal bars.\n"
              "On macOS try:  /usr/bin/python3 rig_preview.py")
        try:
            while True:
                time.sleep(0.1)
                vals, st = rx.snapshot()
                print("\r" + bar(vals, st), end="", flush=True)
        except KeyboardInterrupt:
            print()


if __name__ == "__main__":
    main()
