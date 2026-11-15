import http.client
import json
import os
import select
import shutil
import socket
import tempfile
import threading
import time
import unittest
import unittest.mock
import struct
import subprocess
import sys
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import switchdisplay

# a capture that never writes a frame, on any OS
IDLE = [sys.executable, "-c", "import time; time.sleep(30)"]


def shell_line(cmd):
    """The command a --source resolved to: the last argv item, or all of it on Windows."""
    return cmd if isinstance(cmd, str) else cmd[-1]


class Tests(unittest.TestCase):
    def test_jpeg_size(self):
        data = b"\xff\xd8\xff\xc0\x00\x11\x08\x02\x1c\x03\xc0" + b"\xff\xd9"
        self.assertEqual(switchdisplay.jpeg_size(data), (960, 540))

    def test_click_mapping_and_rate_limit(self):
        c = switchdisplay.Clicker("s", (100, 80), (-10, 5), True,
                                  max_per_window=10, min_gap_s=0)
        self.assertEqual(c.click("a", 50, 40, (100, 80))[1]["click"]["px"], 40)
        self.assertEqual(c.click("a", 0, 0, (100, 80))[1]["click"]["px"], -10)
        self.assertEqual(c.click("a", 100, 80, (100, 80))[1]["click"]["py"], 84)
        self.assertEqual(c.click("a", 101, 80, (100, 80))[0], 400)
        limited = switchdisplay.Clicker("s", None, (0, 0), True,
                                        max_per_window=1, min_gap_s=0)
        limited.click("a", 1, 1, (2, 2))
        self.assertEqual(limited.click("a", 1, 1, (2, 2))[0], 429)

    def test_secret_rejection_counts(self):
        c = switchdisplay.Clicker("secret", None, (0, 0), True, min_gap_s=0)
        c.reject("1")
        self.assertEqual(c.stats["rejected"], 1)

    def test_incremental_frame_splitter_awkward_chunks(self):
        splitter = switchdisplay.JpegStreamSplitter()
        chunks = [b"junk\xff", b"\xd8first\xff", b"\xd9\xff\xd8se", b"cond\xff\xd9"]
        frames = [frame for chunk in chunks for frame in splitter.feed(chunk)]
        self.assertEqual(frames, [b"\xff\xd8first\xff\xd9", b"\xff\xd8second\xff\xd9"])

    def test_dns_answer(self):
        name = b"\x08conntest\x0cnintendowifi\x03net\x00"
        packet = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + name + b"\x00\x01\x00\x01"
        answer = switchdisplay.dns_answer(packet, "192.0.2.1", {"conntest.nintendowifi.net"})
        self.assertIsNotNone(answer)
        self.assertIn(b"\xc0\x0c\x00\x01", answer)

    def test_source_placeholders(self):
        args = SimpleNamespace(source="echo {w} {h} {fps} {q} {qv}", width=2, height=3,
                                fps=4, quality=65, preset=None)
        self.assertEqual(shell_line(switchdisplay.resolve_capture(args)), "echo 2 3 4 65 11")

    def test_encoder_flags(self):
        cmd = switchdisplay.default_capture_cmd(960, 540, 20, 65)
        self.assertIn("yuvj420p", cmd); self.assertIn("optimal", cmd)
        mac = switchdisplay.mac_capture_cmd("Capture screen 1", 960, 540, 15, 65)
        self.assertTrue(mac[mac.index("-vf") + 1].startswith("scale=960:540:"))
        # without passthrough ffmpeg duplicates AVFoundation frames to ~680 fps
        self.assertEqual(mac[mac.index("-fps_mode") + 1], "passthrough")

    def test_text_profile_uses_clear_scaling(self):
        args = SimpleNamespace(source=None, preset="mac-mirror", screen=None,
                               width=1280, height=648, fps=30, quality=85, profile="text")
        cmd = switchdisplay.resolve_capture(args)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("flags=lanczos,unsharp=3:3:0.5", vf)
        self.assertEqual(cmd[cmd.index("-q:v") + 1], "5")
        args.profile = None
        cmd = switchdisplay.resolve_capture(args)
        self.assertIn("flags=fast_bilinear", cmd[cmd.index("-vf") + 1])

    def test_explicit_size_overrides_profile(self):
        args = SimpleNamespace(source=None, preset="test", profile="low", width=123,
                               height=77, fps=20, quality=65)
        self.assertIn("testsrc=size=123x77", " ".join(switchdisplay.resolve_capture(args)))

    def test_websocket_client_parser(self):
        def frame(op, payload, final=True):
            first = (0x80 if final else 0) | op
            mask = b"abcd"
            return bytes([first, 0x80 | len(payload)]) + mask + bytes(
                c ^ mask[i % 4] for i, c in enumerate(payload))
        p = switchdisplay.WSClientParser()
        raw = frame(1, b"pu", final=False)
        self.assertEqual(p.feed(raw[:3]), [])
        self.assertEqual(p.feed(raw[3:] + frame(0, b"ll")), [("text", b"pull")])
        self.assertEqual(p.feed(frame(9, b"x")), [("ping", b"x")])
        self.assertEqual(p.feed(frame(8, b"")), [("close", b"")])
        with self.assertRaises(ValueError):
            p.feed(bytes([0x81, 0x80 | 126]) + struct.pack(">H", 5000))

    def test_render_escaping(self):
        page = switchdisplay.render_viewer(True, "a'\\</script>").decode()
        self.assertNotIn("a'\\</script>", page)
        self.assertIn("\\u003c/script\\u003e", page)

    def test_client_wakeup_on_publish_without_idle_spin(self):
        hub = switchdisplay.Hub([])
        client = hub.add_client()
        try:
            readable, _, _ = select.select([client.wakeup_fd], [], [], 0)
            self.assertEqual(readable, [])
            hub._publish(b"\xff\xd8" + b"x" * 64 + b"\xff\xd9")
            started = time.monotonic()
            readable, _, _ = select.select([client.wakeup_fd], [], [], 0.1)
            self.assertTrue(readable)
            self.assertLess(time.monotonic() - started, 0.1)
            self.assertIsNotNone(client.queue.get_nowait())
        finally:
            hub.remove_client(client)

    def test_publish_keeps_newest_frame_and_keeps_waking(self):
        hub = switchdisplay.Hub([])
        client = hub.add_client()
        try:
            frame = lambda tag: b"\xff\xd8" + tag * 64 + b"\xff\xd9"
            hub._publish(frame(b"a"))
            hub._publish(frame(b"b"))
            self.assertEqual(client.queue.get_nowait(), frame(b"b"))
            # pull mode never drains the queue; publishes must still wake it
            hub._publish(frame(b"c"))
            client.drain_wakeup()
            hub._publish(frame(b"d"))
            self.assertTrue(select.select([client.wakeup_fd], [], [], 0.1)[0])
        finally:
            hub.remove_client(client)

    def test_click_refused_without_accessibility(self):
        c = switchdisplay.Clicker("s", None, (0, 0), False, min_gap_s=0)
        with unittest.mock.patch.object(switchdisplay, "accessibility_trusted", return_value=False), \
                unittest.mock.patch.object(switchdisplay, "quartz_click") as quartz:
            code, body = c.click("a", 1, 1, (2, 2))
        self.assertEqual(code, 503)
        self.assertIn("Accessibility", body["error"])
        self.assertEqual((c.stats["failed"], c.stats["synthesized"]), (1, 0))
        quartz.assert_not_called()
        dry = switchdisplay.Clicker("s", None, (0, 0), True, min_gap_s=0)
        with unittest.mock.patch.object(switchdisplay, "accessibility_trusted", return_value=False):
            self.assertEqual(dry.click("a", 1, 1, (2, 2))[0], 200)

    def test_keymap_parsing(self):
        km = switchdisplay.parse_keymap(["btn-a=e, up=up", "btn-minus=none"])
        self.assertEqual((km["btn-a"], km["up"], km["left"]), ("e", "up", "a"))
        self.assertNotIn("btn-minus", km)
        with self.assertRaises(SystemExit):
            switchdisplay.parse_keymap(["btn-a=command"])  # Cmd is deliberately not mappable
        with self.assertRaises(SystemExit):
            switchdisplay.parse_keymap(["btn-a"])

    def test_key_injector_holds_maps_and_expires(self):
        posted = []
        inj = switchdisplay.KeyInjector(dict(switchdisplay.DEFAULT_KEYMAP), dry_run=False,
                                        hold_s=0.3, poster=lambda code, down: posted.append((code, down)))
        W = switchdisplay.MAC_KEYCODES["w"]
        try:
            with unittest.mock.patch.object(switchdisplay, "accessibility_trusted", return_value=True):
                one, two = object(), object()
                inj.press(one, "up", True)
                inj.press(two, "w", True)        # second holder of the same Mac key
                inj.press(one, "up", False)
                self.assertEqual(posted, [(W, True)])  # still held by the other
                inj.press(two, "w", False)
                self.assertEqual(posted, [(W, True), (W, False)])
                self.assertIsNone(inj.press(one, "k999", True))  # unmapped: ignored
                self.assertEqual(inj.snapshot()["seen"]["k999"], 1)
                inj.press(one, "btn-a", True)    # key-up never arrives
                time.sleep(0.7)
                space = switchdisplay.MAC_KEYCODES["space"]
                self.assertEqual(posted[-2:], [(space, True), (space, False)])
                self.assertEqual(inj.stats["expired"], 1)
                inj.press(one, "left", True)
                inj.release_all(one)             # socket closed
                self.assertEqual(posted[-1], (switchdisplay.MAC_KEYCODES["a"], False))
            with unittest.mock.patch.object(switchdisplay, "accessibility_trusted", return_value=False):
                self.assertIn("Accessibility", inj.press(one, "up", True))
        finally:
            inj.close()

    def test_mouse_buttons_hold_expire_and_turn_motion_into_drags(self):
        buttons, moves = [], []
        inj = switchdisplay.KeyInjector(
            switchdisplay.parse_keymap(["btn-zl=mouse-right"]), dry_run=False, hold_s=0.3,
            button_poster=lambda b, down: buttons.append((b, down)),
            mover=lambda *a: moves.append(a), bounds=(0, 0, 100, 100))
        try:
            with unittest.mock.patch.object(switchdisplay, "accessibility_trusted", return_value=True):
                conn = object()
                inj.move(5, -3)
                self.assertEqual(moves[-1], (5, -3, (0, 0, 100, 100), 5, 0))
                inj.press(conn, "btn-zr", True)
                inj.press(conn, "btn-rstick", True)  # both map to the left button
                self.assertEqual(buttons, [(0, True)])
                inj.move(1, 1)
                self.assertEqual(moves[-1][3:], (6, 0))
                inj.press(conn, "btn-zr", False)
                inj.press(conn, "btn-rstick", False)
                self.assertEqual(buttons[-1], (0, False))
                inj.press(conn, "btn-zl", True)
                inj.move(1, 1)
                self.assertEqual(moves[-1][3:], (7, 1))
                time.sleep(0.7)                       # release never arrives
                self.assertEqual(buttons[-1], (1, False))
                inj.move(100000, -100000)             # a viewer can't fling it
                self.assertEqual(moves[-1][:2], (400, -400))
            with unittest.mock.patch.object(switchdisplay, "accessibility_trusted", return_value=False):
                count = len(moves)
                self.assertIn("Accessibility", inj.move(1, 1))
                self.assertEqual(len(moves), count)
        finally:
            inj.close()

    def test_frontmost_pid_parses_both_lsappinfo_formats(self):
        for output in ('"pid"=4242\n', '[ NULL ]\n    pid = 4242 !cgsConnection type=[ NULL ]\n'):
            result = SimpleNamespace(stdout=output)
            with unittest.mock.patch("subprocess.run", return_value=result):
                self.assertEqual(switchdisplay._frontmost_pid(), 4242)
        with unittest.mock.patch("subprocess.run", return_value=SimpleNamespace(stdout="nope")):
            self.assertIsNone(switchdisplay._frontmost_pid())

    def test_clean_text_removes_control_characters(self):
        self.assertEqual(switchdisplay.clean_text("hi\r\n\tthere\x1b\u2028 \U0001F600"),
                         "hithere \U0001F600")
        self.assertEqual(len(switchdisplay.clean_text("x" * 5000)), switchdisplay.MAX_TEXT)

    def test_text_needs_opt_in_and_accessibility(self):
        typed = []
        off = switchdisplay.KeyInjector({}, dry_run=False, text_poster=typed.append)
        on = switchdisplay.KeyInjector({}, dry_run=False, text_poster=typed.append, text_enabled=True)
        try:
            with unittest.mock.patch.object(switchdisplay, "accessibility_trusted", return_value=True):
                off.type_text("no")
                self.assertEqual(typed, [])
                on.type_text("h\ri")
                self.assertEqual(typed, ["hi"])
            with unittest.mock.patch.object(switchdisplay, "accessibility_trusted", return_value=False):
                self.assertIn("Accessibility", on.type_text("x"))
                self.assertEqual(typed, ["hi"])
        finally:
            off.close(); on.close()

    def test_click_prompts_keyboard_only_for_text_fields(self):
        checks = [True, False]
        reader, writer = socket.socketpair()
        inj = switchdisplay.KeyInjector(dict(switchdisplay.DEFAULT_KEYMAP), dry_run=True,
                                        text_enabled=True, focus_check=lambda: checks.pop(0),
                                        focus_delay_s=0.01)
        inj.add_socket(writer)
        try:
            conn = object()
            inj.press(conn, "btn-zr", True)
            inj.press(conn, "btn-zr", False)        # text field focused: prompt
            time.sleep(0.15)
            self.assertEqual(reader.recv(16), b"\x81\x02kb")
            inj.after_click()                        # something else focused: no prompt
            time.sleep(0.15)
            reader.setblocking(False)
            with self.assertRaises(BlockingIOError):
                reader.recv(16)
            self.assertEqual(inj.stats["keyboard_prompts"], 1)
        finally:
            inj.close(); reader.close(); writer.close()

    def test_pointer_position_reported_for_viewer(self):
        inj = switchdisplay.KeyInjector({}, dry_run=False, bounds=(1920, 27, 1920, 1080),
                                        mover=lambda *a: (2880.0, 567.0))
        none = switchdisplay.KeyInjector({}, dry_run=False, mover=lambda *a: (5.0, 5.0))
        try:
            self.assertIsNone(inj.pointer_message())
            with unittest.mock.patch.object(switchdisplay, "accessibility_trusted", return_value=True):
                inj.move(1, 1); none.move(1, 1)
            self.assertEqual(inj.pointer_message(), b"c:0.5000,0.5000")
            self.assertIsNone(none.pointer_message())  # unknown screen: no marker
        finally:
            inj.close(); none.close()

    def test_stretch_encodes_the_exact_frame_size(self):
        args = SimpleNamespace(source=None, preset="mac-mirror", screen=None,
                               width=1280, height=648, fps=30, quality=85,
                               profile="text", fill="stretch")
        cmd = switchdisplay.resolve_capture(args)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertTrue(vf.startswith("scale=1280:648:flags=lanczos"))
        args.fill = "fit"
        cmd = switchdisplay.resolve_capture(args)
        self.assertIn("force_original_aspect_ratio", cmd[cmd.index("-vf") + 1])

    def test_input_socket_requires_secret_and_releases_on_close(self):
        posted = []
        moved = []
        inj = switchdisplay.KeyInjector(dict(switchdisplay.DEFAULT_KEYMAP), dry_run=False,
                                        poster=lambda code, down: posted.append((code, down)),
                                        mover=lambda *a: moved.append(a[:2]))
        clicker = switchdisplay.Clicker("sekrit", None, (0, 0), True)
        hub = switchdisplay.Hub([])
        server = ThreadingHTTPServer(("127.0.0.1", 0), switchdisplay.make_handler(
            hub, clicker, {"127.0.0.1"}, set(), inj))
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def ws(texts):
            sock = socket.create_connection(("127.0.0.1", server.server_port))
            sock.sendall(b"GET /input HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
                         b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                         b"Sec-WebSocket-Version: 13\r\n\r\n")
            self.assertIn(b"101", sock.recv(4096))
            frames = b""
            for t in texts:
                t = t.encode(); mask = b"\x01\x02\x03\x04"
                frames += (bytes([0x81, 0x80 | len(t)]) + mask +
                           bytes(c ^ mask[i % 4] for i, c in enumerate(t)))
            # one send: the server hangs up on a bad secret, and writing the
            # later frames to that closed socket is a broken pipe
            sock.sendall(frames)
            time.sleep(0.2)
            return sock
        try:
            with unittest.mock.patch.object(switchdisplay, "accessibility_trusted", return_value=True):
                bad = ws(["auth:wrong", "d:up", "m:7,-2"])
                self.assertIn(b"err:bad secret", bad.recv(4096))
                bad.close()
                self.assertEqual((posted, moved), ([], []))
                good = ws(["auth:sekrit", "d:up", "p:mousemove=3", "p:wheel=x", "m:7,-2", "m:bad"])
                self.assertIn(b"ok", good.recv(4096))
                self.assertEqual(moved, [(7, -2)])
                self.assertEqual(inj.snapshot()["probe"], {"mousemove": 3})
                self.assertEqual(posted, [(switchdisplay.MAC_KEYCODES["w"], True)])
                good.close()                      # no key-up sent: closing must release
                time.sleep(0.3)
                self.assertEqual(posted[-1], (switchdisplay.MAC_KEYCODES["w"], False))
        finally:
            server.shutdown(); server.server_close(); inj.close()

    def test_viewer_input_flag(self):
        on = switchdisplay.render_viewer(True, "s", True).decode()
        off = switchdisplay.render_viewer(True, "s").decode()
        self.assertIn("var INPUT_ON = true;", on)
        self.assertIn("var INPUT_ON = false;", off)
        self.assertIn("padSeen && name === 'enter'", on)  # A must not also press Return
        self.assertIn("var TEXT_ON = false;", on)
        self.assertIn('var fill = "fit";', on)
        self.assertIn('var fill = "crop";', switchdisplay.render_viewer(True, "s", fill="crop").decode())
        self.assertIn("var TEXT_ON = true;", switchdisplay.render_viewer(True, "s", True, True).decode())
        self.assertIn("var TEXT_ON = false;", switchdisplay.render_viewer(False, "", False, True).decode())

    def test_mac_presets_capture_screens_by_name(self):
        args = SimpleNamespace(source=None, preset="mac-mirror", screen=None,
                               width=960, height=540, fps=15, quality=65)
        self.assertIn("Capture screen 0:none", switchdisplay.resolve_capture(args))
        args.preset = "mac-extended"
        self.assertIn("Capture screen 1:none", switchdisplay.resolve_capture(args))
        args.screen = 2
        self.assertIn("Capture screen 2:none", switchdisplay.resolve_capture(args))

    def test_viewer_frees_replaced_images(self):
        page = switchdisplay.VIEWER_HTML.decode()
        self.assertIn("old.parentNode.removeChild(old)", page)
        self.assertNotIn("mediaEl = canvas;\n           fit();", page)

    def test_nonblocking_websocket_send_preserves_large_frames(self):
        server, reader = socket.socketpair()
        server.setblocking(False)
        reader.setblocking(False)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        frames = [bytes([i]) * 200000 for i in range(3)]
        received = bytearray()

        def send():
            for payload in frames:
                switchdisplay.ws_send_frame(server, payload)

        thread = threading.Thread(target=send)
        thread.start()
        try:
            while thread.is_alive() or select.select([reader], [], [], 0)[0]:
                if not select.select([reader], [], [], 0.1)[0]:
                    continue
                chunk = reader.recv(137)
                if not chunk:
                    break
                received.extend(chunk)
                time.sleep(0.0005)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            pos = 0
            for payload in frames:
                self.assertEqual(received[pos], 0x82)
                self.assertEqual(received[pos + 1], 127)
                length = struct.unpack(">Q", received[pos + 2:pos + 10])[0]
                self.assertEqual(length, len(payload))
                start = pos + 10
                self.assertEqual(bytes(received[start:start + length]), payload)
                pos = start + length
            self.assertEqual(pos, len(received))
        finally:
            server.close()
            reader.close()

    def test_capture_never_reads_the_terminal_and_warns_without_frames(self):
        hub = switchdisplay.Hub(IDLE)
        hub.no_frames_warn_s = 0.05
        with unittest.mock.patch("builtins.print") as printed:
            hub.start()
            try:
                self.assertEqual(hub.proc.stdin, None)
                # no reader threads: they would spin forever on a fake process's output
                with unittest.mock.patch("subprocess.Popen") as popen, \
                        unittest.mock.patch.object(switchdisplay.threading, "Thread"):
                    switchdisplay.Hub(["x"]).start()
                self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
                time.sleep(0.3)
            finally:
                hub.stop()
        self.assertIn("no frames", " ".join(str(c.args[0]) for c in printed.call_args_list))

    def test_second_interrupt_while_stopping_still_kills_capture(self):
        hub = switchdisplay.Hub(IDLE)
        hub.no_frames_warn_s = 30
        hub.start()
        real_wait = hub.proc.wait
        calls = []

        def interrupted(timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise KeyboardInterrupt
            return real_wait(timeout=timeout)
        with unittest.mock.patch.object(hub.proc, "wait", interrupted):
            hub.stop()
        real_wait(timeout=2)
        self.assertIsNotNone(hub.proc.poll())

    def test_mjpeg_viewer_leaving_is_noticed_without_frames(self):
        hub = switchdisplay.Hub([])
        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     switchdisplay.make_handler(hub, None, {"127.0.0.1"}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            sock = socket.create_connection(("127.0.0.1", server.server_port))
            sock.sendall(b"GET /mjpeg HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            sock.recv(4096)
            deadline = time.monotonic() + 2  # headers go out just before the viewer registers
            while not hub.clients and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(len(hub.clients), 1)
            sock.close()
            deadline = time.monotonic() + 5
            while hub.clients and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertEqual(len(hub.clients), 0)
        finally:
            server.shutdown(); server.server_close()

    def test_host_validation(self):
        hub = switchdisplay.Hub([])
        server = ThreadingHTTPServer(("127.0.0.1", 0),
                                     switchdisplay.make_handler(hub, None, {"127.0.0.1", "localhost"}))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_port
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/health", headers={"Host": "evil.example"})
            self.assertEqual(conn.getresponse().status, 403)
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", port)
            conn.request("GET", "/health", headers={"Host": "localhost"})
            self.assertEqual(conn.getresponse().status, 200)
            conn.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_wildcard_bind_allows_lan_ip_without_wildcard_literal(self):
        hosts = switchdisplay._allowed_hosts("0.0.0.0", "192.0.2.9", [])
        self.assertIn("192.0.2.9", hosts)
        self.assertNotIn("0.0.0.0", hosts)
        self.assertNotIn("::", switchdisplay._allowed_hosts("::", "192.0.2.9", []))


    def test_control_locks_to_first_remote_viewer(self):
        with unittest.mock.patch("builtins.print"):
            gate = switchdisplay.ControlGate(local={"192.0.2.1"})
            self.assertFalse(gate.ok("192.0.2.50"))        # nobody has opened the viewer yet
            self.assertTrue(gate.claim("192.0.2.1"))       # the Mac itself: allowed, no lock taken
            self.assertIsNone(gate.owner)
            self.assertTrue(gate.claim("192.0.2.50"))      # first remote device takes it
            self.assertFalse(gate.claim("192.0.2.66"))     # a later one is view-only
            self.assertFalse(gate.ok("192.0.2.66"))
            self.assertTrue(gate.ok("192.0.2.50") and gate.ok("127.0.0.1"))
            self.assertEqual(gate.snapshot()["refused"], ["192.0.2.66"])
            strict = switchdisplay.ControlGate({"192.0.2.7"}, local={"192.0.2.1"})
            self.assertTrue(strict.claim("192.0.2.7"))
            self.assertFalse(strict.claim("192.0.2.50") or strict.ok("192.0.2.1"))
            self.assertIsNone(strict.owner)

    def test_view_only_client_gets_no_secret_and_cannot_click(self):
        gate = switchdisplay.ControlGate()
        gate.local, gate.owner = set(), "192.0.2.50"       # someone else holds control
        clicker = switchdisplay.Clicker("sekrit", None, (0, 0), True)
        inj = switchdisplay.KeyInjector({}, dry_run=True)
        hub = switchdisplay.Hub([])
        server = ThreadingHTTPServer(("127.0.0.1", 0), switchdisplay.make_handler(
            hub, clicker, {"127.0.0.1"}, set(), inj, gate=gate))
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def request(method, path, body=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
            conn.request(method, path, body=body)
            response = conn.getresponse()
            data = response.read()
            conn.close()
            return response.status, data
        try:
            with unittest.mock.patch("builtins.print"):
                status, page = request("GET", "/")
                self.assertEqual(status, 200)
                self.assertNotIn(b"sekrit", page)
                self.assertIn(b"var CLICK_ON = false;", page)
                click = json.dumps({"x": 1, "y": 1, "secret": "sekrit"})
                self.assertEqual(request("POST", "/click", click)[0], 403)
                self.assertEqual(request("GET", "/input")[0], 403)
                self.assertEqual(clicker.stats["received"], 0)
                inj.press(object(), "h", True)             # a key the controller pressed
                clicker.last_click = {"px": 5, "py": 6}
                health = json.loads(request("GET", "/health")[1])
                self.assertEqual(sorted(health["input"]), ["connections", "stats"])
                self.assertIsNone(health["last_click"])    # not for a view-only device
                gate.owner = "127.0.0.1"
                self.assertIn(b"sekrit", request("GET", "/")[1])
                health = json.loads(request("GET", "/health")[1])
                self.assertEqual(health["control"]["owner"], "127.0.0.1")
                self.assertEqual(health["input"]["seen"], {"h": 1})
                self.assertEqual(health["last_click"], {"px": 5, "py": 6})
        finally:
            server.shutdown(); server.server_close(); inj.close()

    def test_rate_limit_is_per_client(self):
        c = switchdisplay.Clicker("s", None, (0, 0), True, min_gap_s=60)
        self.assertEqual(c.click("a", 1, 1, (2, 2))[0], 200)
        self.assertEqual(c.click("a", 1, 1, (2, 2))[0], 429)
        c.reject("noisy")                                  # another device failing auth
        self.assertEqual(c.click("b", 1, 1, (2, 2))[0], 200)

    def test_duplicate_frames_are_skipped_and_new_viewers_get_the_current_one(self):
        hub = switchdisplay.Hub([])
        frame = lambda tag: b"\xff\xd8" + tag * 64 + b"\xff\xd9"
        first = hub.add_client()
        try:
            hub._publish(frame(b"a"))
            first.queue.get_nowait(); first.drain_wakeup()
            hub._publish(frame(b"a"))                      # unchanged screen
            self.assertTrue(first.queue.empty())
            self.assertEqual((hub.frames, hub.duplicates, hub.frame_id), (2, 1, 1))
            late = hub.add_client("192.0.2.50")            # must not wait for the next change
            try:
                self.assertEqual(late.queue.get_nowait(), frame(b"a"))
                self.assertTrue(select.select([late.wakeup_fd], [], [], 0)[0])
            finally:
                hub.remove_client(late)
            hub._publish(frame(b"b"))
            self.assertEqual(first.queue.get_nowait(), frame(b"b"))
        finally:
            hub.remove_client(first)

    def test_viewer_reports_show_up_per_client(self):
        client = switchdisplay.Client("192.0.2.50")
        try:
            client.count(b"x" * 2048); client.count(b"x" * 2048)
            for bad in ("nan,canvas", "9999,canvas", "abc", ""):
                client.report(bad)
            self.assertIsNone(client.render_fps)
            client.report("11.5,img")
            client.report("7.0,<script>")                  # mode outside the known set is ignored
            snap = client.snapshot()
            self.assertEqual((snap["render_fps"], snap["mode"], snap["sent"], snap["kb_per_frame"]),
                             (7.0, "img", 2, 2.0))
        finally:
            client.close()

    def _wait(self, condition, seconds=4.0):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.02)
        return condition()

    def test_capture_restarts_after_it_exits(self):
        hub = switchdisplay.Hub([sys.executable, "-c", "import sys, time; time.sleep(0.1); sys.exit(3)"])
        with unittest.mock.patch("builtins.print") as printed:
            hub.start()
            try:
                self.assertTrue(self._wait(lambda: hub.restarts >= 1))
            finally:
                hub.stop()
        self.assertIn("capture exited (3)", " ".join(str(c.args[0]) for c in printed.call_args_list))

    def test_capture_without_frames_is_restarted_when_asked(self):
        hub = switchdisplay.Hub(IDLE, restart_stalled=True)
        hub.no_frames_warn_s = 0.05
        with unittest.mock.patch("builtins.print"):
            hub.start()
            first = hub.proc
            try:
                self.assertTrue(self._wait(lambda: hub.restarts >= 1))
                self.assertIsNotNone(first.poll())         # the stuck one was killed
                self.assertIsNot(hub.proc, first)
            finally:
                hub.stop()
        self.assertTrue(self._wait(lambda: hub.proc.poll() is not None))

    def test_dns_responder_survives_a_send_error(self):
        name = b"\x08conntest\x0cnintendowifi\x03net\x00"
        packet = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + name + b"\x00\x01\x00\x01"
        sent, done = [], threading.Event()

        class Sock:
            calls = 0

            def recvfrom(self, _):
                self.calls += 1
                if self.calls > 2:
                    done.set()
                    time.sleep(60)
                return packet, ("192.0.2.50", 5353)

            def sendto(self, data, addr):
                if self.calls == 1:
                    raise OSError(65, "No route to host")
                sent.append(addr)
        with unittest.mock.patch("builtins.print"):
            threading.Thread(target=switchdisplay.dns_responder,
                             args=(Sock(), "192.0.2.1", ["conntest.nintendowifi.net"]),
                             daemon=True).start()
            self.assertTrue(done.wait(3))
        self.assertEqual(sent, [("192.0.2.50", 5353)])     # the query after the error

    def test_new_viewer_is_not_left_on_a_frame_older_than_the_latest(self):
        hub = switchdisplay.Hub([])
        frame = lambda tag: b"\xff\xd8" + tag * 64 + b"\xff\xd9"
        hub._publish(frame(b"a"))
        offer, publisher = switchdisplay.Client.offer, []

        def racing_offer(client, data):
            if not publisher:                              # the capture publishes meanwhile
                publisher.append(threading.Thread(target=hub._publish, args=(frame(b"b"),)))
                publisher[0].start()
                time.sleep(0.2)
            offer(client, data)
        with unittest.mock.patch.object(switchdisplay.Client, "offer", racing_offer):
            client = hub.add_client()
            publisher[0].join()
        try:
            self.assertEqual(client.queue.get_nowait(), frame(b"b"))
        finally:
            hub.remove_client(client)

    def _viewer_case(self, name, tiles=None):
        """Run one tools/viewer_test.js case against the rendered viewer script."""
        page = switchdisplay.render_viewer(True, "s", True, True, tiles=tiles).decode()
        script = page[page.index("<script>") + 8:page.index("</script>")]
        # a directory, not NamedTemporaryFile: Windows won't let Node open that one
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "viewer.js")
            with open(path, "w", encoding="utf-8") as out:
                out.write(script)
            harness = os.path.join(os.path.dirname(__file__), "..", "tools", "viewer_test.js")
            run = subprocess.run([shutil.which("node"), harness, name, path],
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)

    @unittest.skipUnless(shutil.which("node"), "needs Node.js")
    def test_viewer_mjpeg_reconnects_only_when_frames_stop(self):
        self._viewer_case("mjpeg")

    @unittest.skipUnless(shutil.which("node"), "needs Node.js")
    def test_viewer_slow_stick_moves_pointer_beside_a_second_gamepad(self):
        self._viewer_case("twopads")

    @unittest.skipUnless(shutil.which("node"), "needs Node.js")
    def test_viewer_asks_for_each_frame_unless_told_to_push(self):
        self._viewer_case("onrequest")

    @unittest.skipUnless(shutil.which("node"), "needs Node.js")
    def test_viewer_draws_tiles_in_place_and_asks_after_the_batch(self):
        self._viewer_case("tiles", tiles=(4, 4))

    def test_tile_and_gray_capture_commands(self):
        vf = switchdisplay._scale_filter(960, 540, False, gray=True, tiles=(4, 3))
        self.assertEqual(vf, "scale=960:540:force_original_aspect_ratio=decrease:"
                             "force_divisible_by=24:flags=fast_bilinear,format=gray,untile=4x3")
        self.assertIn("force_divisible_by=2:", switchdisplay._scale_filter(960, 540, False))
        args = SimpleNamespace(source=None, preset="test", width=960, height=536, fps=20,
                               quality=65, tiles=(4, 4), gray=False)
        cmd = switchdisplay.resolve_capture(args)
        self.assertEqual(cmd[cmd.index("-vf") + 1], "untile=4x4")
        args.tiles = None
        self.assertNotIn("-vf", switchdisplay.resolve_capture(args))
        args.source, args.tiles = "cap -vf untile={tiles}", (2, 2)
        self.assertEqual(shell_line(switchdisplay.resolve_capture(args)), "cap -vf untile=2x2")
        self.assertEqual(switchdisplay._parse_grid("4x4"), (4, 4))
        for bad in ("1x1", "9x2", "4"):
            with self.assertRaises(Exception):
                switchdisplay._parse_grid(bad)
        self.assertIn(b"var TILES = [4, 4];", switchdisplay.render_viewer(False, "", tiles=(4, 4)))
        self.assertIn(b"var TILES = null;", switchdisplay.render_viewer(False, ""))

    def test_hub_keeps_only_changed_tiles_and_shows_a_picture_at_once(self):
        hub = switchdisplay.Hub([], tiles=(2, 1))
        tile = lambda tag: (b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x10\x00\x20" + tag * 64 +
                            b"\xff\xd9")           # SOF: 32 wide, 16 high
        client = hub.add_client()
        try:
            hub._publish_tile(tile(b"a"), 0)
            self.assertEqual(hub.tiles_since({}), [])      # half a picture is not shown
            self.assertFalse(select.select([client.wakeup_fd], [], [], 0)[0])
            hub._publish_tile(tile(b"b"), 1)
            self.assertTrue(select.select([client.wakeup_fd], [], [], 0)[0])
            client.drain_wakeup()
            self.assertEqual(hub.tiles_since({}), [(0, tile(b"a"), 1), (1, tile(b"b"), 1)])
            self.assertEqual(hub.frame_size, (64, 16))
            hub._publish_tile(tile(b"a"), 0); hub._publish_tile(tile(b"b"), 1)   # still screen
            self.assertFalse(select.select([client.wakeup_fd], [], [], 0)[0])
            hub._publish_tile(tile(b"a"), 0); hub._publish_tile(tile(b"c"), 1)   # right half changed
            self.assertEqual(hub.tiles_since({0: 1, 1: 1}), [(1, tile(b"c"), 2)])
            self.assertEqual((hub.frames, hub.duplicates, hub.tile_updates), (3, 1, 3))
        finally:
            hub.remove_client(client)

    def test_tile_stream_sends_changed_tiles_on_request(self):
        hub = switchdisplay.Hub([], tiles=(2, 1))
        tile = lambda tag: b"\xff\xd8" + tag * 64 + b"\xff\xd9"
        server = ThreadingHTTPServer(("127.0.0.1", 0), switchdisplay.make_handler(
            hub, None, {"127.0.0.1"}))
        self.assertTrue(server.RequestHandlerClass.disable_nagle_algorithm)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def say(sock, text):
            mask = b"\x01\x02\x03\x04"
            sock.sendall(bytes([0x81, 0x80 | len(text)]) + mask +
                         bytes(c ^ mask[i % 4] for i, c in enumerate(text)))

        def message(sock):                                 # one unfragmented server frame
            def exactly(count):                            # no MSG_WAITALL on Windows
                data = b""
                while len(data) < count:
                    data += sock.recv(count - len(data))
                return data
            return exactly(exactly(2)[1])
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
            conn.request("GET", "/mjpeg")
            self.assertEqual(conn.getresponse().status, 503)
            conn.close()
            hub._publish_tile(tile(b"a"), 0); hub._publish_tile(tile(b"b"), 1)
            sock = socket.create_connection(("127.0.0.1", server.server_port))
            sock.settimeout(5)
            sock.sendall(b"GET /ws HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
                         b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                         b"Sec-WebSocket-Version: 13\r\n\r\n")
            self.assertIn(b"101", sock.recv(4096))
            say(sock, b"pull")
            magic = switchdisplay.TILE_MAGIC
            self.assertEqual(message(sock), bytes((magic, 0, 1)) + tile(b"a"))
            self.assertEqual(message(sock), bytes((magic, 1, 0)) + tile(b"b"))
            hub._publish_tile(tile(b"a"), 0); hub._publish_tile(tile(b"c"), 1)
            self.assertFalse(select.select([sock], [], [], 0.3)[0])   # not asked for yet
            say(sock, b"n")
            self.assertEqual(message(sock), bytes((magic, 1, 0)) + tile(b"c"))
            self.assertEqual(hub.viewers()[0]["sent"], 2)  # two updates, not three tiles
            sock.close()
        finally:
            server.shutdown(); server.server_close()

    def _fake_x11(self):
        """An X server stand-in: a US-like layout for a few keys, and a call log."""
        import ctypes
        calls, state = [], {"pointer": (10, 20), "map": {}}
        layout = {38: (ord("a"), ord("A")), 25: (ord("w"), ord("W")), 50: (0xFFE1, 0),
                  36: (0xFF0D, 0), 65: (0x20, 0), 10: (ord("1"), ord("!"))}

        class X:
            def XKeysymToKeycode(self, dpy, keysym):
                for code, syms in list(layout.items()) + list(state["map"].items()):
                    if keysym and keysym in syms:
                        return code
                return 0

            def XkbKeycodeToKeysym(self, dpy, code, group, level):
                return layout.get(code, state["map"].get(code, (0, 0)))[level]

            def XQueryPointer(self, dpy, root, w, child, x, y, wx, wy, mask):
                x._obj.value, y._obj.value = state["pointer"]

            def XDisplayKeycodes(self, dpy, low, high):
                low._obj.value, high._obj.value = 8, 255

            def XGetKeyboardMapping(self, dpy, first, count, per):
                per._obj.value = 2
                table = [0] * (count * 2)
                for code, syms in list(layout.items()) + list(state["map"].items()):
                    table[(code - first) * 2:(code - first) * 2 + 2] = syms
                return table

            def XChangeKeyboardMapping(self, dpy, code, per, syms, count):
                calls.append(("map", code, syms[0]))
                state["map"][code] = (syms[0], 0)
                if not syms[0]:
                    del state["map"][code]

            def XFree(self, table): pass
            def XFlush(self, dpy): pass
            def XSync(self, dpy, discard): pass

        class XTest:
            def XTestFakeKeyEvent(self, dpy, code, down, delay):
                calls.append(("key", code, bool(down)))

            def XTestFakeButtonEvent(self, dpy, button, down, delay):
                calls.append(("button", button, bool(down)))

            def XTestFakeMotionEvent(self, dpy, screen, x, y, delay):
                calls.append(("move", x, y))
                state["pointer"] = (x, y)
        return SimpleNamespace(x=X(), xt=XTest(), dpy=1, screen=0, root=1,
                               lock=threading.Lock(), ctypes=ctypes), calls, state

    def test_x11_input_backend(self):
        q, calls, state = self._fake_x11()
        code = switchdisplay.MAC_KEYCODES
        with unittest.mock.patch.object(switchdisplay, "_x11", return_value=q), \
                unittest.mock.patch.object(switchdisplay, "X11_REMAP_S", 0):
            switchdisplay.x11_click(300, 200)
            self.assertEqual(calls, [("move", 300, 200), ("button", 1, True), ("button", 1, False)])
            del calls[:]
            switchdisplay.x11_key(code["w"], True)         # keymap names -> this layout's keys
            switchdisplay.x11_key(code["return"], False)
            switchdisplay.x11_mouse_button(1, True)        # right button is X button 3
            self.assertEqual(calls, [("key", 25, True), ("key", 36, False), ("button", 3, True)])
            with self.assertRaises(OSError):
                switchdisplay.x11_key(code["slash"], True) # not on the fake layout
            del calls[:]
            self.assertEqual(switchdisplay.x11_mouse_move(-500, 7, (0, 0, 800, 600)), (0, 207))
            self.assertEqual(calls, [("move", 0, 207)])    # clamped to the screen
            del calls[:]
            switchdisplay.x11_text("aA€")             # plain, shifted, not on the layout
            self.assertEqual(calls, [
                ("key", 38, True), ("key", 38, False),
                ("key", 50, True), ("key", 38, True), ("key", 38, False), ("key", 50, False),
                ("map", 255, 0x010020AC), ("key", 255, True), ("key", 255, False),
                ("map", 255, 0)])                          # the borrowed key is given back
            self.assertEqual(state["map"], {})

    def test_linux_uses_the_x11_backend(self):
        with unittest.mock.patch.object(sys, "platform", "linux"), \
                unittest.mock.patch.object(switchdisplay, "x11_click") as click, \
                unittest.mock.patch.object(switchdisplay, "accessibility_trusted", return_value=None):
            clicker = switchdisplay.Clicker("s", (1920, 1080), (0, 0), dry_run=False)
            self.assertEqual(clicker.click("a", 50, 50, (100, 100))[0], 200)
            click.assert_called_once_with(960, 540)
            inj = switchdisplay.KeyInjector({}, dry_run=True)
            try:
                self.assertEqual((inj.poster, inj.mover, inj.text_poster),
                                 (switchdisplay.x11_key, switchdisplay.x11_mouse_move,
                                  switchdisplay.x11_text))
            finally:
                inj.close()

    def test_x11_input_on_a_live_x_server(self):
        """Against a real X server (CI runs the Linux jobs under Xvfb)."""
        try:
            if not sys.platform.startswith("linux") or not os.environ.get("DISPLAY"):
                raise OSError("needs Linux with DISPLAY set")
            switchdisplay._x11.cache_clear()
            q = switchdisplay._x11()
        except OSError as exc:
            if os.environ.get("SWITCHDISPLAY_REQUIRE_X11"):
                raise
            self.skipTest(str(exc))
        import ctypes
        width, height = switchdisplay.x11_screen_size()
        self.assertGreater(min(width, height), 200)
        switchdisplay.x11_click(100, 120)
        self.assertEqual(switchdisplay._x11_pointer(q), (100, 120))
        self.assertEqual(switchdisplay.x11_mouse_move(5, -3, (0, 0, width, height)), (105, 117))
        self.assertEqual(switchdisplay.x11_mouse_move(-9999, 0, (0, 0, width, height))[0], 0)
        self.assertEqual(switchdisplay._x11_pointer(q), (0, 117))

        def held(keycode):                                 # the server's own view of the keys
            keys = ctypes.create_string_buffer(32)
            q.x.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            q.x.XQueryKeymap(q.dpy, keys)
            return bool(keys.raw[keycode // 8] & (1 << (keycode % 8)))
        w = q.x.XKeysymToKeycode(q.dpy, ord("w"))
        self.assertTrue(w)
        switchdisplay.x11_key(switchdisplay.MAC_KEYCODES["w"], True)
        self.assertTrue(held(w))
        switchdisplay.x11_key(switchdisplay.MAC_KEYCODES["w"], False)
        self.assertFalse(held(w))
        spare = switchdisplay._x11_spare_keycode(q)
        switchdisplay.x11_text("aB €é")
        self.assertEqual(switchdisplay._x11_spare_keycode(q), spare)   # given back
        self.assertFalse(held(spare))

    def test_windows_host_pieces(self):
        args = SimpleNamespace(source=None, preset="windows", width=960, height=540, fps=15,
                               quality=65)
        cmd = switchdisplay.resolve_capture(args)
        self.assertEqual(cmd[cmd.index("-f") + 1:cmd.index("-f") + 2], ["gdigrab"])
        self.assertEqual(cmd[cmd.index("-i") + 1], "desktop")
        args.source = 'cap.exe --title "My Window" -q {qv}'
        with unittest.mock.patch.object(sys, "platform", "win32"):
            # the command line as written: no /bin/sh there, and no re-quoting
            self.assertEqual(switchdisplay.resolve_capture(args), 'cap.exe --title "My Window" -q 11')
        self.assertEqual(switchdisplay._shown("a b"), "a b")

        class NoSendmsg:                                   # a Windows socket
            def __init__(self, sock):
                self.sock = sock

            def send(self, data):
                return self.sock.send(data[:1000])         # short writes

            def fileno(self):
                return self.sock.fileno()
        a, b = socket.socketpair()
        try:
            payload = bytes(range(256)) * 20
            switchdisplay.ws_send_frame(NoSendmsg(a), payload)
            got = b""
            while len(got) < len(payload) + 4:
                got += b.recv(65536)
            self.assertEqual(got, b"\x82\x7e" + struct.pack(">H", len(payload)) + payload)
        finally:
            a.close(); b.close()

        # stopping a capture where killpg and SIGKILL don't exist
        hub = switchdisplay.Hub(IDLE)
        hub.no_frames_warn_s = 30
        hub.start()
        with unittest.mock.patch.object(switchdisplay.os, "killpg", create=True,
                                        side_effect=AttributeError):
            hub.stop()
        self.assertTrue(self._wait(lambda: hub.proc.poll() is not None))

    def test_silent_input_socket_is_pinged_then_closed(self):
        inj = switchdisplay.KeyInjector({}, dry_run=True)
        clicker = switchdisplay.Clicker("sekrit", None, (0, 0), True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), switchdisplay.make_handler(
            switchdisplay.Hub([]), clicker, {"127.0.0.1"}, set(), inj))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        auth, mask = b"auth:sekrit", b"\x01\x02\x03\x04"
        try:
            with unittest.mock.patch.object(switchdisplay, "INPUT_IDLE_S", 0.1):
                sock = socket.create_connection(("127.0.0.1", server.server_port))
                sock.settimeout(5)
                sock.sendall(b"GET /input HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
                             b"Connection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                             b"Sec-WebSocket-Version: 13\r\n\r\n")
                self.assertIn(b"101", sock.recv(4096))
                sock.sendall(bytes([0x81, 0x80 | len(auth)]) + mask +
                             bytes(c ^ mask[i % 4] for i, c in enumerate(auth)))
                data = b""
                while True:                                # never answers: a device that left
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                sock.close()
                self.assertIn(b"\x81\x02ok", data)
                self.assertTrue(data.endswith(b"\x89\x00\x89\x00"))   # two pings, then closed
                self.assertTrue(self._wait(lambda: inj.connections == 0))
        finally:
            server.shutdown(); server.server_close(); inj.close()

    def test_oversized_click_body_is_not_read_as_a_second_request(self):
        clicker = switchdisplay.Clicker("sekrit", None, (0, 0), True)
        server = ThreadingHTTPServer(("127.0.0.1", 0), switchdisplay.make_handler(
            switchdisplay.Hub([]), clicker, {"127.0.0.1"}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        body = b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n" + b" " * 5000
        try:
            sock = socket.create_connection(("127.0.0.1", server.server_port))
            sock.settimeout(5)
            sock.sendall(b"POST /click HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: " +
                         str(len(body)).encode() + b"\r\n\r\n" + body)
            data = b""
            try:
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    data += chunk
            except ConnectionResetError:                   # closed with our body unread
                pass
            sock.close()
            self.assertTrue(data.startswith(b"HTTP/1.1 413"))
            self.assertEqual(data.count(b"HTTP/1."), 1)
        finally:
            server.shutdown(); server.server_close()

    def test_auto_detected_display_geometry_follows_changes(self):
        clicker = switchdisplay.Clicker("s", (1000, 500), (0, 0), True, min_gap_s=0, screen=1)
        inj = switchdisplay.KeyInjector({}, dry_run=True, bounds=(0, 0, 1000, 500),
                                        geometry=clicker)
        moved = [{"screen": 1, "id": 7, "origin": (-2000, 100), "size": (2000, 1000)}]
        try:
            with unittest.mock.patch.object(switchdisplay, "mac_displays", return_value=moved), \
                    unittest.mock.patch("builtins.print"):
                self.assertEqual(clicker.click("a", 50, 50, (100, 100))[1]["click"]["px"], 500)
                clicker._refreshed = float("-inf")         # more than a second later
                click = clicker.click("a", 50, 50, (100, 100))[1]["click"]
                self.assertEqual((click["px"], click["py"]), (-1000, 600))
                inj.move(1, 1)
                self.assertEqual(inj.bounds, (-2000, 100, 2000, 1000))
            fixed = switchdisplay.Clicker("s", (1000, 500), (0, 0), True, min_gap_s=0)
            fixed._refreshed = float("-inf")               # --click-display-size: left alone
            with unittest.mock.patch.object(switchdisplay, "mac_displays", return_value=moved):
                self.assertEqual(fixed.click("a", 50, 50, (100, 100))[1]["click"]["px"], 500)
        finally:
            inj.close()

    def test_capture_that_stops_sending_frames_is_restarted(self):
        hub = switchdisplay.Hub(IDLE, restart_stalled=True)
        hub.no_frames_warn_s, hub.stall_s = 30, 0.2
        with unittest.mock.patch("builtins.print"):
            hub.start()
            first = hub.proc
            try:
                hub._publish(b"\xff\xd8" + b"a" * 64 + b"\xff\xd9")   # it worked, then went quiet
                self.assertTrue(self._wait(lambda: hub.restarts >= 1))
                self.assertIsNotNone(first.poll())
            finally:
                hub.stop()

    def test_startup_refuses_a_missing_screen_and_a_switch_without_lan(self):
        one = [{"screen": 0, "id": 1, "origin": (0, 0), "size": (1512, 982)}]
        with unittest.mock.patch.object(switchdisplay, "mac_displays", return_value=one), \
                unittest.mock.patch("sys.argv", ["switchdisplay", "--preset", "mac-extended"]):
            with self.assertRaises(SystemExit) as stop:
                switchdisplay.main()
            self.assertIn("screen 1 does not exist", str(stop.exception))
            self.assertIn("mac-mirror", str(stop.exception))
        with unittest.mock.patch.object(switchdisplay, "_lan_ip", return_value="127.0.0.1"), \
                unittest.mock.patch("sys.argv", ["switchdisplay", "--switch"]):
            with self.assertRaises(SystemExit) as stop:
                switchdisplay.main()
            self.assertIn("no LAN address", str(stop.exception))

    def test_websocket_pong_is_ignored(self):
        pong = bytes([0x8A, 0x80]) + b"abcd"
        self.assertEqual(switchdisplay.WSClientParser().feed(pong), [])

    def test_frame_splitter_skips_end_markers_inside_header_segments(self):
        def segment(marker, payload):
            return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload
        scan = b"\x12\xff\x00\x34\xff\xd0\x56"            # stuffed byte, restart marker
        jpeg = (b"\xff\xd8" + segment(0xE1, b"thumb\xff\xd8..\xff\xd9") +
                segment(0xFE, b"\xff\xd9") + segment(0xDA, b"\x01\x02") + scan + b"\xff\xd9")
        stream = b"junk" + jpeg + jpeg
        for size in (1, 3, len(stream)):
            splitter = switchdisplay.JpegStreamSplitter()
            frames = [f for i in range(0, len(stream), size)
                      for f in splitter.feed(stream[i:i + size])]
            self.assertEqual(frames, [jpeg, jpeg], size)
        # a frame cut short by a capture restart is dropped, not glued to the next one
        splitter = switchdisplay.JpegStreamSplitter()
        self.assertEqual(list(splitter.feed(jpeg[:-6] + jpeg)), [jpeg])



if __name__ == "__main__":
    unittest.main()
