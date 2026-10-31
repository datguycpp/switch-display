import http.client
import inspect
import json
import select
import socket
import threading
import time
import unittest
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
