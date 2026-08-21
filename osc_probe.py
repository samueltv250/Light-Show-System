#!/usr/bin/env python3
"""osc_probe.py — zero-dependency OSC connectivity check for Daslight.

Pure stdlib, so it runs before librosa/python-osc are installed.

    python3 osc_probe.py --ports              # what is Daslight listening on?
    python3 osc_probe.py --wiggle /wheel_a/dimmer      # feed Daslight's Map OSC
    python3 osc_probe.py --sniff 9000         # see what Daslight sends back
"""
import argparse, socket, struct, subprocess, sys, time


def osc_msg(address, value):
    """Encode a single-float OSC message."""
    def pad(b):
        return b + b"\0" * (4 - len(b) % 4)
    return pad(address.encode()) + pad(b",f") + struct.pack(">f", float(value))


def find_ports():
    print("UDP sockets held by Daslight:\n")
    try:
        ps = subprocess.run(["pgrep", "-f", "Daslight"], capture_output=True, text=True)
        pids = [p for p in ps.stdout.split() if p]
        if not pids:
            print("  Daslight is not running.")
            return
        for pid in pids:
            out = subprocess.run(["lsof", "-nP", "-iUDP", "-a", "-p", pid],
                                 capture_output=True, text=True).stdout
            for line in out.splitlines()[1:]:
                port = line.rsplit(":", 1)[-1].strip()
                note = {"7000": "  <- OSC input (likely)", "6454": "  <- Art-Net",
                        "2430": "  <- Nicolaudie discovery"}.get(port, "")
                print(f"  UDP {port}{note}")
    except Exception as e:
        print(f"  could not inspect: {e}")


def wiggle(address, ip, port, period):
    fams = [(socket.AF_INET, ip)] if ":" not in ip else [(socket.AF_INET6, ip)]
    if ip in ("127.0.0.1", "localhost"):
        fams.append((socket.AF_INET6, "::1"))   # Daslight binds IPv6; cover both
    socks = [(socket.socket(f, socket.SOCK_DGRAM), a) for f, a in fams]
    print(f"Sending {address} -> {', '.join(a for _, a in socks)}:{port}")
    print("Daslight: Mappings > Map OSC, then click the fader. Ctrl+C to stop.\n")
    v, n = 0.1, 0
    try:
        while True:
            v = 0.9 if v < 0.5 else 0.1
            for s, a in socks:
                s.sendto(osc_msg(address, v), (a, port))
            n += 1
            print(f"\r  sent {n:4d} packets   value={v}", end="", flush=True)
            time.sleep(period)
    except KeyboardInterrupt:
        print("\nStopped.")


def sniff(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    print(f"Listening for OSC on UDP {port}. Ctrl+C to stop.\n")
    try:
        while True:
            data, src = s.recvfrom(4096)
            addr = data.split(b"\0", 1)[0].decode(errors="replace")
            print(f"  {src[0]}:{src[1]}  {addr}  ({len(data)} bytes)")
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--ports", action="store_true", help="show Daslight's UDP ports")
    p.add_argument("--wiggle", metavar="ADDR", help="wiggle one OSC address 0.1/0.9")
    p.add_argument("--sniff", type=int, metavar="PORT", help="listen for incoming OSC")
    p.add_argument("--ip", default="127.0.0.1")
    p.add_argument("--port", type=int, default=7000)
    p.add_argument("--period", type=float, default=0.4)
    a = p.parse_args()
    if a.ports:      find_ports()
    elif a.wiggle:   wiggle(a.wiggle, a.ip, a.port, a.period)
    elif a.sniff:    sniff(a.sniff)
    else:            p.print_help()
