import http.client
import json
import threading
import unittest
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

    def test_render_escaping(self):
        page = switchdisplay.render_viewer(True, "a'\\</script>").decode()
        self.assertNotIn("a'\\</script>", page)
        self.assertIn("\\u003c/script\\u003e", page)

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


if __name__ == "__main__":
    unittest.main()
