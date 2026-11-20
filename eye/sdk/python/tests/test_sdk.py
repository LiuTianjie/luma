import http.server
import json
import threading
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from luma_eye import LumaEyeClient  # noqa: E402


class _CaptureHandler(http.server.BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        self.received.append(json.loads(raw.decode("utf-8")))
        self.send_response(204)
        self.end_headers()

    def log_message(self, format, *args):
        return


class PythonSDKTests(unittest.TestCase):
    def setUp(self):
        _CaptureHandler.received = []
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_dual_track_and_ring_buffer(self):
        cli = LumaEyeClient(
            f"http://127.0.0.1:{self.port}",
            "pk_py_test",
            "teachvideo-api",
            flush_interval=10.0,
        )
        try:
            tok = cli.bind_context_breadcrumbs()
            try:
                for i in range(60):
                    cli.add_breadcrumb("sql", f"query-{i}")
                cli.log("debug", "stay-in-ring-only")
                # Nothing flushed yet on Track 1
                self.assertEqual(len(_CaptureHandler.received), 0)

                cli.track("job_started", actor_id="user_42", props={"model": "v1"})

                try:
                    raise RuntimeError("gpu worker oom")
                except RuntimeError as err:
                    ok = cli.capture_exception(err, actor_id="user_42", trace_id="abcd1234")
                    self.assertTrue(ok)

                # Track 2 arrived immediately with 50 wrapped ring buffer breadcrumbs
                self.assertEqual(len(_CaptureHandler.received), 1)
                urgent = _CaptureHandler.received[0]
                self.assertEqual(urgent["track"], "urgent")
                self.assertEqual(len(urgent["errors"]), 1)
                bcs = urgent["errors"][0]["breadcrumbs"]
                self.assertEqual(len(bcs), 50)
                self.assertEqual(bcs[0]["message"], "query-11")
                self.assertEqual(bcs[-1]["message"], "stay-in-ring-only")
                self.assertTrue(len(urgent["errors"][0]["frames"]) >= 1)

                # Manual flush dispatches Track 1 batch
                cli.flush()
                self.assertEqual(len(_CaptureHandler.received), 2)
                normal = _CaptureHandler.received[1]
                self.assertEqual(normal["track"], "normal")
                self.assertEqual(normal["events"][0]["name"], "job_started")
                self.assertEqual(len(normal["logs"]), 0)
            finally:
                cli.reset_context_breadcrumbs(tok)
        finally:
            cli.close()


if __name__ == "__main__":
    unittest.main()
