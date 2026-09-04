#!/usr/bin/env python3
"""
RF TARGET TRACKER
Optimized for smooth UI updates on ClockworkPi uConsole.

Important:
- RSSI alone is scalar. Without a heading source (IMU/compass) or true
  per-antenna phase/AoA data, a real 360° bearing cannot be calculated.
- This UI therefore separates:
    1) proximity / movement gradient
    2) bearing status
  and never invents a left/right angle.
"""

import argparse
import os
import queue
import math
import wave
import struct
import tempfile
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
from collections import deque
from statistics import median

APP_TITLE = "RF TARGET TRACKER"

DISCOVERY_INTERVAL = 1.5
FALLBACK_INTERVAL = 0.35
EMA_ALPHA = 0.38
UI_FPS_MS = 60          # ~12.5 fps: smooth on uConsole without wasting CPU
GRAPH_POINTS = 80
MEDIAN_SAMPLES = 5
GRADIENT_SAMPLES = 12
DIRECTION_HOLD_SEC = 1.6
PING_MIN_INTERVAL = 0.18
PING_MAX_INTERVAL = 1.25

BG = "#03070a"
PANEL = "#071016"
GRID = "#12303a"
CYAN = "#48f5ff"
DIM = "#63858d"
WHITE = "#e5fcff"
GREEN = "#53ff91"
YELLOW = "#ffd55a"
RED = "#ff5364"

BSS_RE = re.compile(r"^BSS\s+([0-9a-fA-F:]{17})")
SIG_RE = re.compile(r"signal:\s*(-?\d+(?:\.\d+)?)\s*dBm")
FREQ_RE = re.compile(r"freq:\s*(\d+)")
SSID_RE = re.compile(r"SSID:\s*(.*)$")
TCP_SIGNAL_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*dBm", re.I)


def freq_to_channel(freq):
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if freq == 2484:
        return 14
    if 5000 <= freq <= 5900:
        return (freq - 5000) // 5
    return 0


def channel_to_freq(ch):
    if 1 <= ch <= 13:
        return 2407 + ch * 5
    if ch == 14:
        return 2484
    if ch > 14:
        return 5000 + ch * 5
    return 0


def quiet(cmd, timeout=5):
    try:
        return subprocess.run(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=timeout
        ).returncode == 0
    except Exception:
        return False


def detect_interface():
    for name in ("wlan1", "wlan0"):
        if os.path.exists(f"/sys/class/net/{name}"):
            return name
    try:
        out = subprocess.check_output(["iw", "dev"], text=True, timeout=3)
        m = re.search(r"Interface\s+(\S+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    return "wlan1"


def scan_iw(iface, freq=None):
    cmd = ["iw", "dev", iface, "scan"]
    if freq:
        cmd += ["freq", str(freq)]

    p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip() or "iw scan failed")

    aps = []
    cur = None

    for raw in p.stdout.splitlines():
        line = raw.strip()

        m = BSS_RE.match(line)
        if m:
            if cur:
                aps.append(cur)
            cur = {
                "bssid": m.group(1).lower(),
                "ssid": "",
                "signal": None,
                "freq": 0,
            }
            continue

        if not cur:
            continue

        m = SIG_RE.search(line)
        if m:
            cur["signal"] = float(m.group(1))
            continue

        m = FREQ_RE.search(line)
        if m:
            cur["freq"] = int(m.group(1))
            continue

        m = SSID_RE.search(line)
        if m:
            cur["ssid"] = m.group(1).strip()

    if cur:
        aps.append(cur)

    for ap in aps:
        ap["channel"] = freq_to_channel(ap["freq"])

    return [a for a in aps if a["signal"] is not None]


class Discovery(threading.Thread):
    def __init__(self, iface, outq):
        super().__init__(daemon=True)
        self.iface = iface
        self.outq = outq
        self.stop_evt = threading.Event()

    def stop(self):
        self.stop_evt.set()

    def run(self):
        while not self.stop_evt.is_set():
            t0 = time.monotonic()
            try:
                self.outq.put(("scan", scan_iw(self.iface)))
            except Exception as e:
                self.outq.put(("error", str(e)))
            wait = max(0.05, DISCOVERY_INTERVAL - (time.monotonic() - t0))
            self.stop_evt.wait(wait)


class FastRSSI(threading.Thread):
    """
    Dedicated-adapter fast RSSI tracker.

    After target selection wlan1 is temporarily converted from managed mode
    to monitor mode on the target channel. This avoids slow/blocking `iw scan`
    calls and reads beacon RSSI directly from received packets.

    On stop, the interface is restored to managed mode.
    """
    def __init__(self, iface, target, outq):
        super().__init__(daemon=True)
        self.iface = iface
        self.target = target.copy()
        self.outq = outq
        self.stop_evt = threading.Event()
        self.proc = None
        self.nm_disabled = False

    def stop(self):
        self.stop_evt.set()
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass

    def disable_networkmanager(self):
        if shutil.which("nmcli"):
            try:
                subprocess.run(
                    ["nmcli", "dev", "set", self.iface, "managed", "no"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=3
                )
                self.nm_disabled = True
            except Exception:
                pass

    def restore_managed(self):
        try:
            quiet(["ip", "link", "set", self.iface, "down"])
            quiet(["iw", "dev", self.iface, "set", "type", "managed"])
            quiet(["ip", "link", "set", self.iface, "up"])
        finally:
            if self.nm_disabled and shutil.which("nmcli"):
                try:
                    subprocess.run(
                        ["nmcli", "dev", "set", self.iface, "managed", "yes"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3
                    )
                except Exception:
                    pass
            self.nm_disabled = False

    def setup_monitor(self):
        if not shutil.which("tcpdump"):
            return False, "tcpdump missing"

        ch = int(self.target.get("channel") or 0)
        if ch <= 0:
            return False, "target channel unknown"

        self.disable_networkmanager()

        quiet(["ip", "link", "set", self.iface, "down"])

        if not quiet(["iw", "dev", self.iface, "set", "type", "monitor"]):
            self.restore_managed()
            return False, "cannot switch adapter to monitor mode"

        if not quiet(["ip", "link", "set", self.iface, "up"]):
            self.restore_managed()
            return False, "cannot bring monitor interface up"

        if not quiet(["iw", "dev", self.iface, "set", "channel", str(ch)]):
            self.restore_managed()
            return False, "cannot set target channel"

        return True, ""

    def run_packet_mode(self):
        bssid = self.target["bssid"].lower()

        # -U = packet-buffered, -l = line-buffered.
        # Restrict to beacon/probe-response management traffic for low CPU load.
        cmd = [
            "tcpdump", "-U", "-l", "-n", "-e",
            "-i", self.iface,
            "type", "mgt"
        ]

        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )

        self.outq.put(("mode", "PACKET RSSI"))
        last_hit = time.monotonic()

        while not self.stop_evt.is_set():
            line = self.proc.stdout.readline()

            if not line:
                if self.proc.poll() is not None:
                    err = ""
                    try:
                        err = self.proc.stderr.read().strip()
                    except Exception:
                        pass
                    if err:
                        self.outq.put(("track_error", err))
                    break
                time.sleep(0.003)
                continue

            if bssid not in line.lower():
                continue

            m = TCP_SIGNAL_RE.search(line)
            if not m:
                continue

            try:
                sig = float(m.group(1))
            except ValueError:
                continue

            last_hit = time.monotonic()
            self.outq.put(("rssi", {
                "ssid": self.target.get("ssid", ""),
                "bssid": bssid,
                "signal": sig,
                "channel": self.target.get("channel", 0),
                "freq": self.target.get("freq", 0),
            }))

        try:
            if self.proc:
                self.proc.terminate()
        except Exception:
            pass
        self.proc = None

    def run(self):
        ok, reason = self.setup_monitor()

        if not ok:
            self.outq.put(("track_error", reason))
            self.outq.put(("mode", "MONITOR ERROR"))
            return

        try:
            self.run_packet_mode()
        finally:
            self.restore_managed()
            if not self.stop_evt.is_set():
                self.outq.put(("mode", "PACKET STREAM LOST"))


class PingSound(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.stop_evt = threading.Event()
        self.wav_path = None
        self.player = shutil.which("aplay") or shutil.which("paplay")
        self.make_ping()

    def make_ping(self):
        fd, path = tempfile.mkstemp(prefix="rf_ping_", suffix=".wav")
        os.close(fd)
        self.wav_path = path

        rate = 44100
        duration = 0.055
        freq = 1450.0
        total = int(rate * duration)

        with wave.open(path, "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            for i in range(total):
                t = i / rate
                env = math.exp(-38.0 * t)
                sample = 0.33 * env * math.sin(2 * math.pi * freq * t)
                wf.writeframesraw(struct.pack("<h", int(max(-1, min(1, sample)) * 32767)))

    def stop(self):
        self.stop_evt.set()

    def interval_from_rssi(self):
        rssi = self.app.ema
        if rssi is None:
            return PING_MAX_INTERVAL
        # -85 dBm -> slow, -30 dBm -> fast.
        x = max(0.0, min(1.0, (rssi + 85.0) / 55.0))
        return PING_MAX_INTERVAL - x * (PING_MAX_INTERVAL - PING_MIN_INTERVAL)

    def play(self):
        if not self.player or not self.wav_path:
            return
        try:
            if self.player.endswith("paplay"):
                subprocess.Popen(
                    [self.player, self.wav_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
            else:
                subprocess.Popen(
                    [self.player, "-q", self.wav_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
        except Exception:
            pass

    def run(self):
        while not self.stop_evt.is_set():
            if self.app.target_bssid and self.app.ema is not None:
                self.play()
                self.stop_evt.wait(self.interval_from_rssi())
            else:
                self.stop_evt.wait(0.2)

        if self.wav_path:
            try:
                os.unlink(self.wav_path)
            except Exception:
                pass


class App:
    def __init__(self, root, iface, ssid=None, bssid=None):
        self.root = root
        self.iface = iface
        self.target_ssid = ssid
        self.target_bssid = bssid.lower() if bssid else None

        self.q = queue.Queue()
        self.discovery = None
        self.tracker = None

        self.ap_list = []
        self.selection = 0

        self.raw = deque(maxlen=MEDIAN_SAMPLES)
        self.history = deque(maxlen=GRAPH_POINTS)
        self.gradients = deque(maxlen=GRADIENT_SAMPLES)

        self.ema = None
        self.peak = -120.0
        self.last_seen = 0.0
        self.last_ap = None
        self.mode = "DISCOVERY"
        self.error = ""
        self.direction_state = "ACQUIRING"
        self.direction_candidate = "ACQUIRING"
        self.direction_candidate_since = time.monotonic()

        root.title(APP_TITLE)
        root.configure(bg=BG)
        root.attributes("-fullscreen", True)
        try:
            root.state("zoomed")
        except Exception:
            pass
        root.overrideredirect(False)

        root.bind("<F11>", lambda e: root.attributes("-fullscreen", not bool(root.attributes("-fullscreen"))))
        root.bind("<Escape>", lambda e: self.close())
        root.bind("q", lambda e: self.close())
        root.bind("<Up>", lambda e: self.move(-1))
        root.bind("<Down>", lambda e: self.move(1))
        root.bind("<Return>", lambda e: self.lock())
        root.bind("r", lambda e: self.reset())

        self.canvas = tk.Canvas(root, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.start_discovery()
        self.ping = PingSound(self)
        self.ping.start()
        self.poll()
        self.redraw()

    def start_discovery(self):
        self.stop_tracker()
        if not self.discovery:
            self.discovery = Discovery(self.iface, self.q)
            self.discovery.start()
        self.mode = "DISCOVERY"

    def stop_discovery(self):
        if self.discovery:
            self.discovery.stop()
            self.discovery = None

    def stop_tracker(self):
        if self.tracker:
            self.tracker.stop()
            self.tracker = None

    def close(self):
        self.stop_discovery()
        self.stop_tracker()
        try:
            self.ping.stop()
        except Exception:
            pass
        self.root.after(100, self.root.destroy)

    def reset(self):
        self.stop_tracker()
        self.target_ssid = None
        self.target_bssid = None
        self.last_ap = None
        self.raw.clear()
        self.history.clear()
        self.gradients.clear()
        self.ema = None
        self.peak = -120.0
        self.last_seen = 0.0
        self.direction_state = "ACQUIRING"
        self.direction_candidate = "ACQUIRING"
        self.direction_candidate_since = time.monotonic()
        self.start_discovery()

    def move(self, delta):
        if self.target_bssid or not self.ap_list:
            return
        self.selection = max(0, min(len(self.ap_list) - 1, self.selection + delta))

    def lock(self):
        if self.target_bssid or not self.ap_list:
            return
        ap = self.ap_list[self.selection]
        self.target_bssid = ap["bssid"]
        self.target_ssid = ap["ssid"]
        self.update_signal(ap)
        self.start_tracker(ap)

    def start_tracker(self, ap):
        self.stop_discovery()
        self.stop_tracker()
        self.error = ""
        self.mode = "SWITCHING TO MONITOR"
        self.tracker = FastRSSI(self.iface, ap, self.q)
        self.tracker.start()

    def update_signal(self, ap):
        self.raw.append(ap["signal"])
        med = median(self.raw)

        old = self.ema
        self.ema = med if old is None else (EMA_ALPHA * med + (1 - EMA_ALPHA) * old)

        if old is not None:
            self.gradients.append(self.ema - old)

        self.history.append(self.ema)
        self.peak = max(self.peak, self.ema)
        self.last_seen = time.monotonic()
        self.last_ap = ap.copy()

    def gradient(self):
        if not self.gradients:
            return 0.0
        vals = list(self.gradients)
        weights = range(1, len(vals) + 1)
        return sum(v * w for v, w in zip(vals, weights)) / sum(weights)

    def movement_state(self):
        if len(self.gradients) < 5:
            self.direction_state = "ACQUIRING"
            return self.direction_state, DIM

        g = self.gradient()

        # Wider dead-band prevents multipath noise from flipping the arrow.
        if g > 0.30:
            candidate = "CLOSING"
        elif g < -0.30:
            candidate = "MOVING AWAY"
        else:
            candidate = "HOLD / SEARCH"

        now = time.monotonic()

        if candidate != self.direction_candidate:
            self.direction_candidate = candidate
            self.direction_candidate_since = now

        # Only commit a new direction after it has remained stable.
        if candidate != self.direction_state:
            if now - self.direction_candidate_since >= DIRECTION_HOLD_SEC:
                self.direction_state = candidate

        if self.direction_state == "CLOSING":
            return self.direction_state, GREEN
        if self.direction_state == "MOVING AWAY":
            return self.direction_state, RED
        if self.direction_state == "HOLD / SEARCH":
            return self.direction_state, YELLOW
        return self.direction_state, DIM

    def poll(self):
        # Drain the complete queue in one UI cycle. This avoids stale RSSI
        # values building up and appearing as delayed bursts.
        newest_rssi = None

        while True:
            try:
                kind, payload = self.q.get_nowait()
            except queue.Empty:
                break

            if kind == "rssi":
                newest_rssi = payload
            elif kind == "mode":
                self.mode = payload
            elif kind == "error":
                if self.mode == "DISCOVERY":
                    self.error = payload
            elif kind == "track_error":
                self.error = payload
            elif kind == "scan":
                aps = sorted(payload, key=lambda a: a["signal"], reverse=True)
                self.ap_list = aps[:16]
                self.selection = min(self.selection, max(0, len(self.ap_list) - 1))

                if self.target_bssid or self.target_ssid:
                    hit = None
                    if self.target_bssid:
                        hit = next(
                            (a for a in aps if a["bssid"] == self.target_bssid),
                            None
                        )
                    elif self.target_ssid:
                        matches = [a for a in aps if a["ssid"] == self.target_ssid]
                        if matches:
                            hit = max(matches, key=lambda a: a["signal"])

                    if hit:
                        self.target_bssid = hit["bssid"]
                        self.target_ssid = hit["ssid"]
                        newest_rssi = hit
                        self.start_tracker(hit)

        if newest_rssi is not None:
            self.update_signal(newest_rssi)

        self.root.after(20, self.poll)

    def box(self, x1, y1, x2, y2, title=""):
        c = self.canvas
        c.create_rectangle(x1, y1, x2, y2, outline=GRID, width=2, fill=PANEL)
        if title:
            c.create_text(
                x1 + 14, y1 + 12, anchor="nw", text=title,
                fill=DIM, font=("DejaVu Sans Mono", 10, "bold")
            )

    def draw_scan(self, w, h):
        c = self.canvas
        c.create_text(
            42, 34, anchor="w", text=APP_TITLE,
            fill=CYAN, font=("DejaVu Sans Mono", 24, "bold")
        )
        c.create_text(
            w - 42, 38, anchor="e", text=f"{self.iface} // DISCOVERY",
            fill=DIM, font=("DejaVu Sans Mono", 10, "bold")
        )

        self.box(36, 78, w - 36, h - 44, "AVAILABLE ACCESS POINTS")

        cols = [
            ("SSID", 62),
            ("BSSID", int(w * 0.45)),
            ("CH", int(w * 0.72)),
            ("RSSI", int(w * 0.82))
        ]

        for t, x in cols:
            c.create_text(
                x, 116, anchor="w", text=t,
                fill=DIM, font=("DejaVu Sans Mono", 10, "bold")
            )

        y0 = 150
        rh = 31

        for i, ap in enumerate(self.ap_list[:13]):
            y = y0 + i * rh
            sel = i == self.selection

            if sel:
                c.create_rectangle(54, y - 4, w - 54, y + 23, outline=CYAN)

            col = WHITE if sel else "#8fb6bd"

            c.create_text(
                62, y, anchor="nw", text=(ap["ssid"] or "<hidden>")[:32],
                fill=col, font=("DejaVu Sans Mono", 11, "bold" if sel else "normal")
            )
            c.create_text(
                int(w * 0.45), y, anchor="nw", text=ap["bssid"],
                fill=col, font=("DejaVu Sans Mono", 10)
            )
            c.create_text(
                int(w * 0.72), y, anchor="nw", text=str(ap["channel"]),
                fill=col, font=("DejaVu Sans Mono", 10)
            )
            c.create_text(
                int(w * 0.82), y, anchor="nw", text=f'{ap["signal"]:.0f} dBm',
                fill=col, font=("DejaVu Sans Mono", 10, "bold")
            )

        c.create_text(
            w / 2, h - 18, anchor="center",
            text="UP/DOWN SELECT   ENTER LOCK   ESC EXIT",
            fill=DIM, font=("DejaVu Sans Mono", 9, "bold")
        )

    def draw_compass(self, cx, cy, radius):
        c = self.canvas

        # 360° compass face
        c.create_oval(cx-radius, cy-radius, cx+radius, cy+radius, outline=GRID, width=2)
        c.create_oval(cx-radius+22, cy-radius+22, cx+radius-22, cy+radius-22,
                      outline=GRID)

        for angle in range(0, 360, 15):
            import math
            r = math.radians(angle)
            outer_x = cx + math.sin(r) * radius
            outer_y = cy - math.cos(r) * radius
            inner = radius - (16 if angle % 45 == 0 else 8)
            inner_x = cx + math.sin(r) * inner
            inner_y = cy - math.cos(r) * inner
            c.create_line(inner_x, inner_y, outer_x, outer_y, fill=DIM)

        for label, angle in [("N", 0), ("E", 90), ("S", 180), ("W", 270)]:
            import math
            r = math.radians(angle)
            x = cx + math.sin(r) * (radius - 35)
            y = cy - math.cos(r) * (radius - 35)
            c.create_text(
                x, y, text=label, fill=WHITE,
                font=("DejaVu Sans Mono", 12, "bold")
            )

        # Homing arrow: movement-gradient indicator, not an absolute RF bearing.
        state, arrow_col = self.movement_state()
        import math

        if state == "CLOSING":
            angle = 0.0
            label = "KEEP DIRECTION"
        elif state == "MOVING AWAY":
            angle = 180.0
            label = "TURN AROUND"
        else:
            # Animated search sweep when the gradient is inconclusive.
            angle = math.sin(time.monotonic() * 0.85) * 42.0
            label = "SWEEP / SEARCH"

        r = math.radians(angle)
        ux, uy = math.sin(r), -math.cos(r)
        px, py = -uy, ux

        tip = (cx + ux * 76, cy + uy * 76)
        neck = (cx + ux * 18, cy + uy * 18)
        tail = (cx - ux * 55, cy - uy * 55)

        pts = [
            tip[0], tip[1],
            neck[0] + px*32, neck[1] + py*32,
            neck[0] + px*13, neck[1] + py*13,
            tail[0] + px*13, tail[1] + py*13,
            tail[0] - px*13, tail[1] - py*13,
            neck[0] - px*13, neck[1] - py*13,
            neck[0] - px*32, neck[1] - py*32,
        ]
        c.create_polygon(*pts, fill=arrow_col, outline=WHITE, width=2)
        c.create_text(
            cx, cy + radius - 54, text=label,
            fill=arrow_col, font=("DejaVu Sans Mono", 9, "bold")
        )

    def draw_track(self, w, h):
        c = self.canvas
        ap = self.last_ap or {
            "ssid": self.target_ssid or "UNKNOWN",
            "bssid": self.target_bssid or "--",
            "channel": 0
        }

        movement, mov_col = self.movement_state()
        grad = self.gradient()

        c.create_text(
            40, 34, anchor="w", text=APP_TITLE,
            fill=CYAN, font=("DejaVu Sans Mono", 24, "bold")
        )
        c.create_text(
            w - 40, 38, anchor="e", text=f"{self.mode} // {self.iface}",
            fill=GREEN if self.mode == "PACKET RSSI" else DIM,
            font=("DejaVu Sans Mono", 10, "bold")
        )

        self.box(36, 76, w - 36, 164, "TARGET")
        c.create_text(
            58, 107, anchor="nw", text=ap.get("ssid") or "<hidden>",
            fill=WHITE, font=("DejaVu Sans Mono", 18, "bold")
        )
        c.create_text(
            58, 138, anchor="nw",
            text=f'BSSID {ap.get("bssid")}    CH {ap.get("channel")}',
            fill=DIM, font=("DejaVu Sans Mono", 10, "bold")
        )

        split = int(w * 0.56)

        self.box(36, 181, split, h - 44, "SIGNAL / PROXIMITY")
        cx = (36 + split) / 2

        c.create_text(
            cx, 246, text="-- dBm" if self.ema is None else f"{self.ema:.1f} dBm",
            fill=WHITE, font=("DejaVu Sans Mono", 39, "bold")
        )
        c.create_text(
            cx, 292, text=movement,
            fill=mov_col, font=("DejaVu Sans Mono", 15, "bold")
        )
        c.create_text(
            cx, 320, text=f"GRADIENT {grad:+.2f} dB/update",
            fill=DIM, font=("DejaVu Sans Mono", 9, "bold")
        )

        bx1, bx2 = 70, split - 34
        by1, by2 = 344, 376
        c.create_rectangle(bx1, by1, bx2, by2, outline=GRID, width=2)

        level = 0.0 if self.ema is None else max(0.0, min(1.0, (self.ema + 90) / 60))
        if level:
            c.create_rectangle(
                bx1 + 4, by1 + 4,
                bx1 + 4 + (bx2 - bx1 - 8) * level, by2 - 4,
                outline="", fill=mov_col
            )

        gx1, gy1, gx2, gy2 = 70, 400, split - 34, h - 78
        c.create_rectangle(gx1, gy1, gx2, gy2, outline=GRID)

        vals = list(self.history)
        if len(vals) > 1:
            pts = []
            for i, val in enumerate(vals):
                x = gx1 + i * (gx2 - gx1) / max(1, GRAPH_POINTS - 1)
                y = gy2 - max(0, min(1, (val + 90) / 60)) * (gy2 - gy1)
                pts += [x, y]
            c.create_line(*pts, fill=CYAN, width=2)

        right_x1 = split + 18
        self.box(right_x1, 181, w - 36, h - 44, "DIRECTION")

        self.draw_compass(
            (right_x1 + w - 36) / 2,
            360,
            min(142, int((w - right_x1 - 70) / 2))
        )

        c.create_text(
            (right_x1 + w - 36) / 2, h - 95,
            text=f"PEAK {self.peak:.1f} dBm",
            fill=WHITE, font=("DejaVu Sans Mono", 11, "bold")
        )

        age = 0 if not self.last_seen else time.monotonic() - self.last_seen
        age_col = GREEN if age < 0.5 else YELLOW if age < 1.5 else RED
        c.create_text(
            (right_x1 + w - 36) / 2, h - 70,
            text=f"LAST SAMPLE {age:.2f}s",
            fill=age_col, font=("DejaVu Sans Mono", 9, "bold")
        )
        c.create_text(
            (right_x1 + w - 36) / 2, h - 48,
            text="PING ACTIVE",
            fill=CYAN, font=("DejaVu Sans Mono", 8, "bold")
        )

        if self.error:
            c.create_text(
                w / 2, 174, text=self.error[:100],
                fill=RED, font=("DejaVu Sans Mono", 8, "bold")
            )

        c.create_text(
            w / 2, h - 18, text="R NEW TARGET   ESC EXIT",
            fill=DIM, font=("DejaVu Sans Mono", 9, "bold")
        )

    def redraw(self):
        self.canvas.delete("all")

        w = max(800, self.canvas.winfo_width())
        h = max(480, self.canvas.winfo_height())

        for x in range(0, w, 48):
            self.canvas.create_line(x, 0, x, h, fill="#071116")
        for y in range(0, h, 48):
            self.canvas.create_line(0, y, w, y, fill="#071116")

        if self.target_bssid and self.last_ap:
            self.draw_track(w, h)
        else:
            self.draw_scan(w, h)

        self.root.after(UI_FPS_MS, self.redraw)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iface", default=None)
    parser.add_argument("--ssid", default=None)
    parser.add_argument("--bssid", default=None)
    args = parser.parse_args()

    if os.geteuid() != 0:
        raise SystemExit(
            f"Run as root:\n  sudo python3 {os.path.abspath(__file__)} "
            f"--iface {args.iface or 'wlan1'}"
        )

    if shutil.which("iw") is None:
        raise SystemExit("Missing iw: sudo apt install iw")

    iface = args.iface or detect_interface()

    root = tk.Tk()
    App(root, iface, ssid=args.ssid, bssid=args.bssid)
    root.mainloop()


if __name__ == "__main__":
    main()
