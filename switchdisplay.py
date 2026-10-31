#!/usr/bin/env python3
"""switchdisplay — a second display for any browser a Nintendo Switch can run.

Serves a JPEG frame stream three ways so an old WebKit (NetFront NX on the
Switch: WebKit 601.6 / Safari 9.1-class, no JIT) can view it:

    GET /        minimal ES5 viewer (WebSocket + <canvas>, <img> MJPEG fallback)
    GET /ws      binary WebSocket stream of JPEG frames
    GET /mjpeg   multipart/x-mixed-replace JPEG stream
    GET /health  JSON counters (frames produced, clients, source running)

Plus an optional tiny DNS responder so the Switch's captive-portal applet can
be pointed at this machine (see README) — the applet loads whatever
conntest.nintendowifi.net resolves to, and has no URL whitelist.

Capture is a pluggable command that writes a JPEG byte stream to stdout
(default: ffmpeg testsrc, so the tool runs anywhere). See README for real
Wayland/X11 capture presets.

No third-party Python packages. No kernel modules. Read-only towards the
running desktop unless you pick a capture command that captures it.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import queue
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import signal

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

VIEWER_HTML = b"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>switchdisplay</title>
<style>
 html,body{margin:0;padding:0;background:#000;height:100%;overflow:hidden;
   -webkit-tap-highlight-color:rgba(0,0,0,0)}
 #wrap{position:fixed;top:0;left:0;right:0;bottom:0;display:block;background:#000}
 canvas,img{position:absolute;left:0;top:0;background:#000}
 #hud{position:fixed;left:6px;top:6px;font:12px monospace;color:#8f8;opacity:.6;z-index:3}
 #fs{position:fixed;right:6px;top:6px;z-index:3;font:14px monospace;color:#8f8;
   background:rgba(0,0,0,.45);border:1px solid #4a4;border-radius:4px;padding:2px 8px}
 .dot{position:fixed;width:18px;height:18px;margin:-9px 0 0 -9px;border-radius:9px;
   background:rgba(0,255,128,.55);pointer-events:none;z-index:2}
</style></head>
<body>
<div id="wrap"><canvas id="c"></canvas></div>
<div id="hud">connecting...</div>
<div id="fs">&#9974;</div>
<script>
(function () {
  var CLICK_ON = __CLICK_ON__;
  var CLICK_SECRET = __CLICK_SECRET__;
  var hud = document.getElementById('hud');
  var canvas = document.getElementById('c');
  var ctx = canvas.getContext('2d');
  var url = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws';
  var frames = 0, last = new Date().getTime(), taps = 0;
  window.__frames = 0;
  window.__taps = 0;
  window.__mode = 'none';

  // -- object-fit is not available on the Switch's WebKit (Safari 9 class):
  //    letterbox manually so a 960x540 frame fills the screen undistorted.
  var mediaEl = null;
  function fit() {
    if (!mediaEl) return;
    var vw = window.innerWidth, vh = window.innerHeight;
    var sw = mediaEl.naturalWidth || mediaEl.width || 16, sh = mediaEl.naturalHeight || mediaEl.height || 9;
    if (!sw || !sh) return;
    var scale = Math.min(vw / sw, vh / sh);
    var w = Math.round(sw * scale), h = Math.round(sh * scale);
    mediaEl.style.width = w + 'px';
    mediaEl.style.height = h + 'px';
    mediaEl.style.left = Math.round((vw - w) / 2) + 'px';
    mediaEl.style.top = Math.round((vh - h) / 2) + 'px';
  }
  window.addEventListener('resize', fit, false);

  function hudUpdate() {
    var now = new Date().getTime();
    var fps = frames * 1000 / Math.max(1, now - last);
    hud.textContent = window.__mode + ' ' + Math.round(fps) + ' fps' +
      (CLICK_ON ? ' \\u00b7 taps ' + taps : ' \\u00b7 view only');
    if (now - last > 2000) { frames = 0; last = now; }
  }
  window.setInterval(hudUpdate, 500);

  // ---- MJPEG fallback: an <img> needs nothing but image decoding ----
  function mjpegFallback() {
    if (window.__mode === 'mjpeg') return;
    window.__mode = 'mjpeg';
    var img = document.createElement('img');
    var stall;
    mediaEl = img;
    function restart() {
      img.src = '/mjpeg?' + (new Date().getTime());
      window.clearTimeout(stall);
      stall = window.setTimeout(restart, 8000);
    }
    img.onload = function () { frames++; window.__frames++; fit(); restart(); };
    img.onerror = restart;
    restart();
    document.getElementById('wrap').appendChild(img);
    canvas.style.display = 'none';
    hud.textContent = 'mjpeg';
  }

  if (!window.WebSocket) { mjpegFallback(); } else {
    // ---- WebSocket + canvas: the standard live-frame pattern ----
    (function connect() {
      var ws;
      var gotMessage = false;
      var attempts = window.__wsAttempts || 0;
      window.__wsAttempts = attempts + 1;
      try { ws = new WebSocket(url); } catch (e) { mjpegFallback(); return; }
      window.__mode = 'ws';
      ws.binaryType = 'arraybuffer';
      ws.onopen = function () { hud.textContent = 'ws open'; };
      ws.onmessage = function (ev) {
        gotMessage = true;
        frames++;
        if (window.__decoding) {
          window.__pending = ev.data;
          return;
        }
        decode(ev.data);
      };
      function decode(data) {
        window.__decoding = true;
        var blob = new Blob([data], { type: 'image/jpeg' });
        var u = window.URL.createObjectURL(blob);
        var img = new Image();
        img.onload = function () {
          if (canvas.width !== img.width || canvas.height !== img.height) {
            canvas.width = img.width; canvas.height = img.height;
          }
          ctx.drawImage(img, 0, 0);
          window.URL.revokeObjectURL(u);
           mediaEl = canvas;
           fit();
           window.__frames++;
           window.__decoding = false;
           if (window.__pending) { var next = window.__pending; window.__pending = null; decode(next); }
         };
         img.onerror = function () {
           window.URL.revokeObjectURL(u); window.__decoding = false;
           if (window.__pending) { var next = window.__pending; window.__pending = null; decode(next); }
         };
         img.src = u;
      }
      ws.onclose = function () {
        if (!gotMessage && attempts >= 3) { mjpegFallback(); return; }
        hud.textContent = 'reconnecting...';
        window.setTimeout(connect, 1000);
      };
      ws.onerror = function () { hud.textContent = 'error'; };
    })();
  }

  // ---- fullscreen toggle (its own button: body taps are clicks on the Mac) ----
  function fullscreen(ev) {
    ev.stopPropagation();
    var el = document.documentElement;
    if (el.requestFullscreen) { el.requestFullscreen(); }
    else if (el.webkitRequestFullscreen) { el.webkitRequestFullscreen(); }
  }
  document.getElementById('fs').addEventListener('click', fullscreen, false);
  document.getElementById('fs').addEventListener('touchend', fullscreen, false);

  // ---- tap-to-click: tap only. No drag, no hover, no right click, no keys. ----
  function flash(cx, cy) {
    var d = document.createElement('div');
    d.className = 'dot';
    d.style.left = cx + 'px';
    d.style.top = cy + 'px';
    document.body.appendChild(d);
    window.setTimeout(function () {
      if (d.parentNode) { d.parentNode.removeChild(d); }
    }, 220);
  }

  function sendTap(fx, fy) {
    var xhr = new XMLHttpRequest();
    xhr.open('POST', '/click', true);
    xhr.setRequestHeader('Content-Type', 'application/json');
    xhr.send(JSON.stringify({
      x: Math.round(fx), y: Math.round(fy), secret: CLICK_SECRET
    }));
    taps++;
    window.__taps++;
  }

  function tapAt(ev) {
    if (!mediaEl) return;
    var t = (ev.changedTouches && ev.changedTouches[0]) || ev;
    if (typeof t.clientX !== 'number') return;
    var rect = mediaEl.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    // letterboxed element -> frame pixels (the element box is exactly the image)
    var fw = mediaEl.naturalWidth || mediaEl.width;
    var fh = mediaEl.naturalHeight || mediaEl.height;
    if (!fw || !fh) return;
    var fx = (t.clientX - rect.left) * (fw / rect.width);
    var fy = (t.clientY - rect.top) * (fh / rect.height);
    if (fx < 0 || fy < 0 || fx > fw || fy > fh) return;
    sendTap(fx, fy);
    flash(t.clientX, t.clientY);
  }

  if (CLICK_ON) {
    var lastTap = 0;
    var onTap = function (ev) {
      var target = ev.target;
      while (target) {
        if (target === document.getElementById('fs')) { return; }
        target = target.parentNode;
      }
      var now = new Date().getTime();
      if (now - lastTap < 400) { return; }   // swallow the synthetic click after touch
      lastTap = now;
      tapAt(ev);
      if (ev.preventDefault) { ev.preventDefault(); }
    };
    if ('ontouchstart' in window) {
      document.body.addEventListener('touchend', onTap, false);
    } else {
      document.body.addEventListener('click', onTap, false);
    }
  }
})();
</script>
</body></html>
"""


def render_viewer(click_enabled: bool, secret: str) -> bytes:
    """Inject the click flag/secret into the static page (JS literals, JSON-escaped)."""
    literal = json.dumps(secret if click_enabled else "")
    literal = (literal.replace("<", "\\u003c").replace(">", "\\u003e")
               .replace("&", "\\u0026"))
    return (VIEWER_HTML
            .replace(b"__CLICK_ON__", b"true" if click_enabled else b"false")
            .replace(b"__CLICK_SECRET__", literal.encode()))


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    """Width/height from a JPEG's SOF marker (enough for coordinate mapping)."""
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    n = len(data)
    while i + 9 < n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = int.from_bytes(data[i + 5:i + 7], "big")
            w = int.from_bytes(data[i + 7:i + 9], "big")
            return (w, h) if w and h else None
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = int.from_bytes(data[i + 2:i + 4], "big")
        i += 2 + seg
    return None


class Clicker:
    """Tap-to-click: validate a shared secret, rate-limit, synthesise one left click.

    Deliberately single-purpose — no drag, no hover, no right click, no keyboard.
    """

    def __init__(self, secret: str, display_size: tuple[int, int] | None,
                 origin: tuple[int, int], dry_run: bool,
                 max_per_window: int = 10, window_s: float = 3.0,
                 min_gap_s: float = 0.08):
        self.secret = secret
        self.display_size = display_size      # Mac display points; None -> use frame size
        self.origin = origin                  # display origin in global screen coords
        self.dry_run = dry_run
        self.max_per_window = max_per_window
        self.window_s = window_s
        self.min_gap_s = min_gap_s
        self.stats = {"received": 0, "rejected": 0, "limited": 0,
                      "synthesized": 0, "bad_coords": 0, "failed": 0}
        self.last_click: dict | None = None
        self._lock = threading.Lock()
        self._by_ip: dict[str, list[float]] = {}
        self._last_time = 0.0

    def check_secret(self, candidate: str) -> bool:
        return hmac.compare_digest(str(candidate).encode(), self.secret.encode())

    def reject(self, ip: str) -> None:
        """Count a failed authentication attempt against the per-IP limiter."""
        with self._lock:
            self.stats["rejected"] += 1
            self._rate_ok(ip)

    def _rate_ok(self, ip: str) -> bool:
        now = time.monotonic()
        if now - self._last_time < self.min_gap_s:
            return False
        q = self._by_ip.setdefault(ip, [])
        q[:] = [t for t in q if now - t < self.window_s]
        if len(q) >= self.max_per_window:
            return False
        q.append(now)
        self._last_time = now
        return True

    def synthesizer(self, px: int, py: int) -> tuple[str, list[str]]:
        cliclick = shutil.which("cliclick")
        if cliclick:
            return "cliclick", [cliclick, f"c:{px},{py}"]
        script = f'tell application "System Events" to click at {{{px}, {py}}}'
        return "osascript", ["osascript", "-e", script]

    def click(self, ip: str, x: float, y: float,
              frame_size: tuple[int, int] | None) -> tuple[int, dict]:
        with self._lock:
            self.stats["received"] += 1
            if not self._rate_ok(ip):
                self.stats["limited"] += 1
                return 429, {"error": "rate limited"}

        fw, fh = frame_size or (0, 0)
        if not fw or not fh or not (0 <= x <= fw and 0 <= y <= fh):
            with self._lock:
                self.stats["bad_coords"] += 1
            return 400, {"error": "coordinates outside the frame"}

        # frame pixels -> normalised -> display points (scale-invariant, so the
        # ffmpeg downscale and the page letterbox both cancel out)
        nx, ny = x / fw, y / fh
        dw, dh = self.display_size or (fw, fh)
        ox, oy = self.origin
        px = ox + min(max(0, int(round(nx * dw))), dw - 1)
        py = oy + min(max(0, int(round(ny * dh))), dh - 1)

        tool, argv = self.synthesizer(px, py)
        if self.dry_run:
            result = "dry-run"
        else:
            try:
                subprocess.run(argv, check=True, timeout=3,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                result = "clicked"
            except (OSError, subprocess.SubprocessError) as exc:
                with self._lock:
                    self.stats["failed"] = self.stats.get("failed", 0) + 1
                return 502, {"error": f"{tool} failed", "detail": type(exc).__name__,
                             "command": " ".join(argv)}

        with self._lock:
            self.stats["synthesized"] += 1
            self.last_click = {"nx": round(nx, 4), "ny": round(ny, 4),
                               "px": px, "py": py, "tool": tool,
                               "result": result, "ts": round(time.time(), 2)}
        return 200, {"ok": True, "click": self.last_click, "command": " ".join(argv)}


class JpegStreamSplitter:
    """Incrementally split a concatenated JPEG stream at SOI/EOI markers."""

    def __init__(self, max_buffer: int = 8 * 1024 * 1024):
        self.max_buffer = max_buffer
        self.buf = bytearray()

    def feed(self, chunk: bytes):
        self.buf.extend(chunk)
        if len(self.buf) > self.max_buffer:
            del self.buf[:]
            return
        while True:
            start = self.buf.find(b"\xff\xd8")
            if start < 0:
                if len(self.buf) > 1:
                    del self.buf[:-1]
                return
            if start:
                del self.buf[:start]
            end = self.buf.find(b"\xff\xd9", 2)
            if end < 0:
                return
            frame = bytes(self.buf[:end + 2])
            del self.buf[:end + 2]
            yield frame


def split_jpeg_stream(chunks, max_buffer: int = 8 * 1024 * 1024):
    """Yield complete JPEGs from arbitrary chunks of a concatenated stream."""
    splitter = JpegStreamSplitter(max_buffer)
    for chunk in chunks:
        yield from splitter.feed(chunk)


class Hub:
    """One capture subprocess feeding N websocket clients and the MJPEG endpoint."""

    def __init__(self, cmd: list[str]):
        self.cmd = cmd
        self.latest: bytes | None = None
        self.frame_size: tuple[int, int] | None = None
        self.lock = threading.Lock()
        self.clients: set[queue.Queue] = set()
        self.frames = 0
        self.proc: subprocess.Popen | None = None
        self.stderr_tail: list[str] = []
        self.started = time.time()

    def start(self) -> None:
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     start_new_session=True)
        threading.Thread(target=self._read_frames, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for line in self.proc.stderr:
            self.stderr_tail.append(line.decode("utf-8", "replace").rstrip())
            del self.stderr_tail[:-20]

    def _read_frames(self) -> None:
        """Split a concatenated JPEG stream at SOI/EOI markers.

        Bytes before the first SOI are discarded.  EOI is used so each frame is
        published immediately; an 8 MiB cap prevents a broken source growing
        memory without bound.
        """
        assert self.proc and self.proc.stdout
        splitter = JpegStreamSplitter()
        while True:
            # read1 returns whatever is available: read(n) would block until the
            # full 64 KB fills, which throttles a 20 fps stream to a few fps
            # and makes the newest-frame-only client queue drop frames.
            chunk = self.proc.stdout.read1(65536)
            if not chunk:
                break
            for frame in splitter.feed(chunk):
                self._publish(frame)
        if self.proc and self.proc.returncode is None:
            self.proc.poll()
        self.stderr_tail.append(f"capture process exited ({self.proc.returncode})")

    def _publish(self, frame: bytes) -> None:
        if len(frame) < 64:  # truncated tail: not a frame
            return
        with self.lock:
            self.latest = frame
            self.frame_size = jpeg_size(frame) or self.frame_size
            self.frames += 1
            clients = list(self.clients)
        for q in clients:
            try:
                q.put_nowait(frame)
            except queue.Full:
                pass  # drop: the newest frame matters, not every frame

    def add_client(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1)
        with self.lock:
            self.clients.add(q)
        return q

    def remove_client(self, q: queue.Queue) -> None:
        with self.lock:
            self.clients.discard(q)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except OSError:
                    self.proc.kill()


def ws_ping(sock: socket.socket) -> None:
    sock.sendall(b"\x89\x00")


def ws_upgrade(handler) -> bool:
    key = handler.headers.get("Sec-WebSocket-Key")
    if not key:
        handler.send_error(400, "missing Sec-WebSocket-Key")
        return False
    accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept)
    handler.end_headers()
    handler.wfile.flush()
    return True


def ws_send_frame(sock: socket.socket, payload: bytes) -> None:
    """Server->client binary frame, unmasked (RFC 6455 5.1)."""
    header = bytearray([0x82])
    n = len(payload)
    if n < 126:
        header.append(n)
    elif n < (1 << 16):
        header.append(126)
        header += struct.pack(">H", n)
    else:
        header.append(127)
        header += struct.pack(">Q", n)
    sock.sendall(bytes(header) + payload)


def make_handler(hub: Hub, clicker: "Clicker | None" = None,
                 allowed_hosts: set[str] | None = None,
                 click_allow: set[str] | None = None):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # keep stdout clean
            pass

        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _host_ok(self) -> bool:
            host = self.headers.get("Host", "").rsplit("@", 1)[-1]
            if host.startswith("["):
                host = host[1:].split("]", 1)[0]
            else:
                host = host.split(":", 1)[0]
            if host.lower() in (allowed_hosts or set()):
                return True
            self.send_error(403, "host not allowed")
            return False

        def _click_client_ok(self) -> bool:
            return not click_allow or self.client_address[0] in click_allow

        def do_POST(self):
            if not self._host_ok():
                return
            path = self.path.split("?")[0]
            if path != "/click":
                self.send_error(404)
                return
            if clicker is None:
                self._json(403, {"error": "tap-to-click is disabled "
                                          "(start with --click-secret)"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > 4096:
                self._json(413, {"error": "bad body size"})
                return
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._json(400, {"error": "bad json"})
                return
            if not isinstance(payload, dict):
                self._json(400, {"error": "json object required"})
                return
            if not self._click_client_ok():
                self._json(403, {"error": "clicks not allowed from this client"})
                return
            if not clicker.check_secret(payload.get("secret", "")):
                clicker.reject(self.client_address[0])
                self._json(403, {"error": "bad secret"})
                return
            try:
                x = float(payload["x"])
                y = float(payload["y"])
            except (KeyError, TypeError, ValueError):
                self._json(400, {"error": "x and y are required numbers"})
                return
            code, body = clicker.click(self.client_address[0], x, y, hub.frame_size)
            self._json(code, body)

        def do_GET(self):
            if not self._host_ok():
                return
            path = self.path.split("?")[0]
            if path == "/":
                page = render_viewer(clicker is not None and self._click_client_ok(),
                                     clicker.secret if clicker else "")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(page)
            elif path == "/health":
                body = json.dumps({
                    "frames": hub.frames,
                    "clients": len(hub.clients),
                    "uptime_s": round(time.time() - hub.started, 1),
                    "source_alive": bool(hub.proc and hub.proc.poll() is None),
                    "source_exit_code": hub.proc.returncode if hub.proc else None,
                    "source_stderr": hub.stderr_tail[-3:],
                    "frame_size": hub.frame_size,
                    "clicks": clicker.stats if clicker else None,
                    "clicks_enabled": clicker is not None,
                    "last_click": clicker.last_click if clicker else None,
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/mjpeg":
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                q = hub.add_client()
                try:
                    while True:
                        try:
                            frame = q.get(timeout=5.0)
                        except queue.Empty:
                            break
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                        self.wfile.write(b"Content-Length: " + str(len(frame)).encode() + b"\r\n\r\n")
                        self.wfile.write(frame + b"\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    hub.remove_client(q)
            elif path == "/ws":
                if not ws_upgrade(self):
                    return
                q = hub.add_client()
                try:
                    while True:
                        try:
                            frame = q.get(timeout=15.0)
                        except queue.Empty:
                            ws_ping(self.connection)
                            continue
                        ws_send_frame(self.connection, frame)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    hub.remove_client(q)
                self.close_connection = True
            else:
                self.send_error(404)

    return Handler


def dns_answer(data: bytes, answer: str, wanted: set[str]) -> bytes | None:
    """Build a minimal DNS response, or None for malformed/non-query input."""
    try:
        tid, flags, qd = struct.unpack(">HHH", data[:6])
        if flags & 0x8000 or qd != 1:
            return None
        pos, labels = 12, []
        while pos < len(data) and data[pos]:
            ln = data[pos]
            if ln > 63 or pos + 1 + ln > len(data):
                return None
            labels.append(data[pos + 1:pos + 1 + ln].decode("ascii", "replace"))
            pos += 1 + ln
        pos += 1
        if pos + 4 > len(data):
            return None
        qtype, qclass = struct.unpack(">HH", data[pos:pos + 4])
        question = data[12:pos + 4]
        name = ".".join(labels).lower()
        if qtype != 1:  # NOERROR/NODATA for non-A queries
            return struct.pack(">HHHHHH", tid, 0x8180, 1, 0, 0, 0) + question
        if wanted and name not in wanted:
            return struct.pack(">HHHHHH", tid, 0x8183, 1, 0, 0, 0) + question
        rr = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 30, 4) + socket.inet_aton(answer)
        return struct.pack(">HHHHHH", tid, 0x8180, 1, 1, 0, 0) + question + rr
    except (IndexError, struct.error, OSError, UnicodeError):
        return None


def dns_responder(sock: socket.socket, answer: str, names: list[str] | None) -> None:
    """Answer A queries with `answer` so the Switch's portal check lands here.

    Only the plain DNS subset needed for an A lookup is implemented.
    """
    wanted = {n.lower() for n in (names or [])}
    print(f"[dns] answering A queries -> {answer}", flush=True)
    while True:
        data, addr = sock.recvfrom(1500)
        resp = dns_answer(data, answer, wanted)
        if resp:
            sock.sendto(resp, addr)


def _qv(quality: int) -> int:
    """Map a friendly 1..100 quality to ffmpeg's -q:v 1..31 (inverted)."""
    return max(1, min(31, 31 - int(quality * 0.31)))


def default_capture_cmd(width: int, height: int, fps: int, quality: int) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-re", "-f", "lavfi", "-i", f"testsrc=size={width}x{height}:rate={fps}",
        "-c:v", "mjpeg", "-q:v", str(_qv(quality)), "-f", "mjpeg", "-",
    ]


def mac_capture_cmd(index: int, width: int, height: int, fps: int, quality: int) -> list[str]:
    """macOS: AVFoundation screen device -> MJPEG on stdout.

    `index` is the AVFoundation video device index of the display, as printed by
    `ffmpeg -f avfoundation -list_devices true -i ""` — "Capture screen 0" is
    usually 1, and a BetterDisplay dummy shows up as the next index.
    Requires Screen Recording permission for the calling terminal, else frames
    come out black with no error.
    """
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "avfoundation", "-framerate", str(fps),
        "-capture_cursor", "1", "-i", f"{index}:none",
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-c:v", "mjpeg", "-q:v", str(_qv(quality)), "-f", "mjpeg", "-",
    ]


def x11_capture_cmd(display: str, width: int, height: int, fps: int, quality: int) -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "x11grab", "-framerate", str(fps), "-i", display,
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-c:v", "mjpeg", "-q:v", str(_qv(quality)), "-f", "mjpeg", "-",
    ]


PRESETS = {
    # name: (default width, height, fps)
    "test": (960, 540, 20),
    "mac-mirror": (960, 540, 15),
    "mac-extended": (1280, 720, 15),
    "x11": (960, 540, 20),
}


def resolve_capture(args) -> list[str]:
    """Turn CLI flags into the capture argv (or a shell command)."""
    if args.source:
        values = {"{w}": str(args.width), "{h}": str(args.height), "{fps}": str(args.fps),
                  "{q}": str(args.quality), "{qv}": str(_qv(args.quality))}
        source = args.source
        for key, value in values.items():
            source = source.replace(key, value)
        return ["/bin/sh", "-c", source]
    preset = args.preset or "test"
    quality = args.quality
    if preset == "test":
        return default_capture_cmd(args.width, args.height, args.fps, quality)
    if preset == "mac-mirror":
        return mac_capture_cmd(args.display_index, args.width, args.height, args.fps, quality)
    if preset == "mac-extended":
        return mac_capture_cmd(args.display_index, args.width, args.height, args.fps, quality)
    if preset == "x11":
        return x11_capture_cmd(args.display, args.width, args.height, args.fps, quality)
    raise SystemExit(f"unknown preset: {preset}")


def _parse_size(text: str) -> tuple[int, int]:
    if not text:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT")
    w, _, h = text.lower().partition("x")
    try:
        value = (int(w), int(h))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected WIDTHxHEIGHT") from exc
    if min(value) <= 0:
        raise argparse.ArgumentTypeError("size must be positive")
    return value


def _parse_xy(text: str) -> tuple[int, int]:
    x, _, y = text.partition(",")
    try:
        return (int(x), int(y))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected X,Y") from exc


def main() -> int:
    ap = argparse.ArgumentParser(description="Second display for the Nintendo Switch's browser.")
    ap.add_argument("--port", type=int, default=8090, help="HTTP port (default 8090)")
    ap.add_argument("--bind", default="0.0.0.0", help="HTTP bind address")
    ap.add_argument("--preset", choices=sorted(PRESETS), default=None,
                    help="capture preset: test | mac-mirror | mac-extended | x11 (default test)")
    ap.add_argument("--source", default=None,
                    help="explicit capture command writing JPEG to stdout; placeholders "
                         "{w} {h} {fps} {q} (1..100) {qv} (ffmpeg -q:v 1..31)")
    ap.add_argument("--display-index", type=int, default=1,
                    help="AVFoundation screen device index for the mac presets "
                         "(see: ffmpeg -f avfoundation -list_devices true -i \"\")")
    ap.add_argument("--display", default=":0.0", help="X11 display for the x11 preset")
    ap.add_argument("--width", type=int, default=None)
    ap.add_argument("--height", type=int, default=None)
    ap.add_argument("--fps", type=int, default=None)
    ap.add_argument("--quality", type=int, default=65, help="1..100 (default 65)")
    ap.add_argument("--dns-port", type=int, default=0,
                    help="also answer DNS A queries on this port (53 needs root)")
    ap.add_argument("--dns-answer", default=None, help="IP to answer with (default: LAN IP)")
    ap.add_argument("--dns-bind", default=None,
                    help="address to bind the DNS responder to (default: --dns-answer); "
                         "binding the LAN IP avoids mDNSResponder on macOS")
    ap.add_argument("--print-source", action="store_true",
                    help="print the resolved capture command and exit (no server)")
    ap.add_argument("--click-secret", default=None,
                    help="enable tap-to-click with this shared secret "
                         "(or set SWITCHDISPLAY_SECRET); without it /click is refused")
    ap.add_argument("--click-display-size", type=_parse_size, default=None, metavar="WxH",
                    help="target display size in POINTS, e.g. 1512x982 (Retina!) — "
                         "omit to map frame pixels 1:1")
    ap.add_argument("--click-display-origin", type=_parse_xy, default=(0, 0), metavar="X,Y",
                    help="display origin in global screen coordinates (default 0,0)")
    ap.add_argument("--click-dry-run", action="store_true",
                     help="log the synthesizer command instead of clicking (safe test)")
    ap.add_argument("--allow-host", action="append", default=[],
                    help="additional HTTP Host name allowed (repeatable)")
    ap.add_argument("--click-allow", action="append", default=[], metavar="IP",
                    help="only this client IP may click or receive the secret (repeatable)")
    args = ap.parse_args()

    pw, ph, pf = PRESETS[args.preset or "test"]
    if args.width is None:
        args.width = pw
    if args.height is None:
        args.height = ph
    if args.fps is None:
        args.fps = pf

    cmd = resolve_capture(args)
    if args.print_source:
        print(shlex.join(cmd))
        return 0

    from http.server import ThreadingHTTPServer
    import socketserver

    class LocalThreadingHTTPServer(ThreadingHTTPServer):
        # HTTPServer.server_bind calls getfqdn(), which can block on an offline
        # host; the server never needs a reverse-DNS name.
        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
    secret = args.click_secret or os.environ.get("SWITCHDISPLAY_SECRET") or ""
    clicker = None
    if secret:
        clicker = Clicker(
            secret=secret,
            display_size=args.click_display_size,
            origin=args.click_display_origin,
            dry_run=args.click_dry_run,
        )
    lan_ip = _lan_ip()
    if clicker and args.bind == "0.0.0.0":
        args.bind = lan_ip
        print(f"[http] click enabled; binding to LAN IP {lan_ip} instead of 0.0.0.0", flush=True)
    allowed_hosts = _allowed_hosts(args.bind, lan_ip, args.allow_host)
    dns_sock = None
    if args.dns_port:
        answer = args.dns_answer or lan_ip
        dns_bind = args.dns_bind or answer
        dns_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dns_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        dns_sock.bind((dns_bind, args.dns_port))
    hub = Hub(cmd)
    server = LocalThreadingHTTPServer((args.bind, args.port),
                                 make_handler(hub, clicker, allowed_hosts, set(args.click_allow)))
    server.daemon_threads = True
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        uid, gid = int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", "0"))
        try:
            os.setgroups([])
        except OSError:
            pass
        os.setgid(gid)
        os.setuid(uid)
        print(f"[priv] dropped privileges to uid={uid} gid={gid}", flush=True)
    hub.start()
    if dns_sock:
        threading.Thread(target=dns_responder,
                         args=(dns_sock, answer,
                               ["conntest.nintendowifi.net", "ctest.cdn.nintendo.net"]),
                         daemon=True).start()
    shown = args.bind
    print(f"[http] http://{shown}:{args.port}/  (viewer, /ws, /mjpeg, /click, /health)", flush=True)
    print(f"[src ] {shlex.join(cmd)}", flush=True)
    if clicker:
        size = clicker.display_size or "frame size"
        if clicker.display_size is None:
            print("[click] warning: --click-display-size omitted; mapping to frame size", flush=True)
        print(f"[click] tap-to-click ON{' (DRY RUN)' if clicker.dry_run else ''} "
              f"secret=set display={size} origin={clicker.origin}", flush=True)
    else:
        print("[click] tap-to-click OFF (no --click-secret / SWITCHDISPLAY_SECRET)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        hub.stop()
    return 0


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _allowed_hosts(bind: str, lan_ip: str, allow_hosts: list[str]) -> set[str]:
    """Build Host values, replacing wildcard binds with the effective LAN IP."""
    hosts = {"localhost", "127.0.0.1", "conntest.nintendowifi.net",
             "ctest.cdn.nintendo.net", lan_ip.lower()}
    if bind not in ("0.0.0.0", "::"):
        hosts.add(bind.lower())
    hosts.update(h.lower().split(":", 1)[0] for h in allow_hosts)
    return hosts


if __name__ == "__main__":
    sys.exit(main())
