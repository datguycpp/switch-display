import http.client
import inspect
import json
import select
import socket
import threading
import time
import unittest
import unittest.mock
import struct
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import switchdisplay


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

    def test_frame_splitter(self):
        frames = list(switchdisplay.split_jpeg_stream([b"junk\xff", b"\xd8one\xff\xd9\xff\xd8two", b"\xff\xd9"]))
        self.assertEqual(frames, [b"\xff\xd8one\xff\xd9", b"\xff\xd8two\xff\xd9"])

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
        self.assertEqual(switchdisplay.resolve_capture(args)[2], "echo 2 3 4 65 11")

    def test_encoder_flags_and_static_filter(self):
        cmd = switchdisplay.default_capture_cmd(960, 540, 20, 65)
        self.assertIn("yuvj420p", cmd); self.assertIn("optimal", cmd)
        static = switchdisplay.default_capture_cmd(960, 540, 20, 65, True)
        self.assertIn("mpdecimate,setpts=N/FRAME_RATE/TB", static)
        mac = switchdisplay.mac_capture_cmd(1, 960, 540, 15, 65, True)
        vf = mac[mac.index("-vf") + 1]
        self.assertLess(vf.index("scale="), vf.index("mpdecimate"))
        # without passthrough ffmpeg duplicates AVFoundation frames to ~680 fps
        self.assertEqual(mac[mac.index("-fps_mode") + 1], "passthrough")

    def test_text_profile_uses_clear_scaling(self):
        args = SimpleNamespace(source=None, preset="mac-mirror", screen=None, display_index=None,
                               width=1280, height=648, fps=30, quality=85, static=True, profile="text")
        cmd = switchdisplay.resolve_capture(args)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("flags=lanczos,unsharp=3:3:0.5", vf)
        self.assertLess(vf.index("unsharp"), vf.index("mpdecimate"))
        self.assertEqual(cmd[cmd.index("-q:v") + 1], "5")
        args.profile = None
        cmd = switchdisplay.resolve_capture(args)
        self.assertIn("flags=fast_bilinear", cmd[cmd.index("-vf") + 1])

    def test_profile_sizes_and_explicit_override(self):
        self.assertEqual((1280, 720), {"sharp": (1280, 720),
                                       "balanced": (960, 540), "low": (800, 450)}["sharp"])
        args = SimpleNamespace(source=None, preset="test", profile="low", width=123,
                               height=77, fps=20, quality=65, static=False)
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

    def test_pull_and_push_websocket_flow_control(self):
        handler = inspect.getsource(switchdisplay.make_handler)
        self.assertIn('if text == "pull":', handler)
        self.assertIn('pull = True', handler)
        self.assertIn('viewer cannot render yet', handler)
        self.assertIn('requested = True', handler)
        self.assertIn('client.wakeup_fd', handler)
        self.assertNotIn('timeout = 0.02 if not pull else 0.2', handler)

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

    def test_cliclick_absolute_negative_coordinates(self):
        c = switchdisplay.Clicker("s", None, (0, 0), True)
        with unittest.mock.patch("shutil.which", return_value="/bin/cliclick"):
            self.assertEqual(c.synthesizer(-1920, 27)[1], ["/bin/cliclick", "c:=-1920,27"])
            self.assertEqual(c.synthesizer(10, 20)[1], ["/bin/cliclick", "c:10,20"])

    def test_click_refused_without_accessibility(self):
        c = switchdisplay.Clicker("s", None, (0, 0), False, min_gap_s=0)
        with unittest.mock.patch.object(switchdisplay, "accessibility_trusted", return_value=False), \
                unittest.mock.patch("subprocess.run") as run, \
                unittest.mock.patch.object(switchdisplay, "quartz_click") as quartz:
            code, body = c.click("a", 1, 1, (2, 2))
        self.assertEqual(code, 503)
        self.assertIn("Accessibility", body["error"])
        self.assertEqual((c.stats["failed"], c.stats["synthesized"]), (1, 0))
        run.assert_not_called(); quartz.assert_not_called()
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
        args = SimpleNamespace(source=None, preset="mac-mirror", screen=None, display_index=None,
                               width=1280, height=648, fps=30, quality=85, static=False,
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
            for t in texts:
                t = t.encode(); mask = b"\x01\x02\x03\x04"
                sock.sendall(bytes([0x81, 0x80 | len(t)]) + mask +
                             bytes(c ^ mask[i % 4] for i, c in enumerate(t)))
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
        args = SimpleNamespace(source=None, preset="mac-mirror", screen=None, display_index=None,
                               width=960, height=540, fps=15, quality=65, static=False)
        self.assertIn("Capture screen 0:none", switchdisplay.resolve_capture(args))
        args.preset = "mac-extended"
        self.assertIn("Capture screen 1:none", switchdisplay.resolve_capture(args))
        args.screen = 2
        self.assertIn("Capture screen 2:none", switchdisplay.resolve_capture(args))
        args.display_index = 5
        self.assertIn("5:none", switchdisplay.resolve_capture(args))

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


if __name__ == "__main__":
    unittest.main()
