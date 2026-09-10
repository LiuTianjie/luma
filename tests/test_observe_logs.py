import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

from tests.test_observe import load_observe


class LogShipperTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_observe("log_shipper.py", "log_shipper")

    def _source(self, **values):
        return self.mod.Source(
            alloc=values.get("alloc", "alloc-1"),
            job=values.get("job", "granary"),
            task=values.get("task", "web"),
            task_group=values.get("task_group", "web"),
            stream=values.get("stream", "stdout"),
            node=values.get("node", "worker"),
        )

    def test_unknown_source_starts_at_end_minus_tail(self):
        files = [(0, "alloc/logs/web.stdout.0", 200000)]
        cursor = self.mod.start_cursor(files, tail=65536)
        self.assertEqual(cursor.file, "alloc/logs/web.stdout.0")
        self.assertEqual(cursor.offset, 200000 - 65536)

    def test_unknown_source_without_files_is_none(self):
        self.assertIsNone(self.mod.start_cursor([]))

    def test_cursor_advances_by_bytes_read_and_follows_rotation(self):
        files = {
            "alloc-1": [
                {"Name": "web.stdout.0", "Size": 4, "IsDir": False},
                {"Name": "web.stdout.1", "Size": 5, "IsDir": False},
            ]
        }
        reads = []

        def readat(alloc, path, offset, limit):
            reads.append((path, offset, limit))
            if path.endswith(".0"):
                return b"one\n"[offset:]
            return b"two\n"[offset:]

        inserted = []
        shipper = self.mod.Shipper(
            list_allocs=lambda: [
                {
                    "ID": "alloc-1",
                    "JobID": "granary",
                    "TaskGroup": "web",
                    "ClientStatus": "running",
                    "NodeName": "worker",
                    "TaskStates": {"web": {}},
                }
            ],
            list_files=lambda alloc: files[alloc],
            readat=readat,
            insert=inserted.append,
        )
        shipper.cursors["alloc-1/web/stdout"] = self.mod.Cursor(file="alloc/logs/web.stdout.0", offset=0)
        shipper.poll()
        cursor = shipper.cursors["alloc-1/web/stdout"]
        self.assertEqual(cursor.file, "alloc/logs/web.stdout.1")
        self.assertEqual(cursor.offset, 4)
        self.assertEqual([row["_msg"] for row in inserted[0] if row["stream"] == "stdout"], ["one", "two"])

    def test_truncated_file_rewinds_without_crash(self):
        files = [{"Name": "web.stdout.0", "Size": 3, "IsDir": False}]
        shipper = self.mod.Shipper(
            list_allocs=lambda: [
                {
                    "ID": "alloc-1",
                    "JobID": "granary",
                    "TaskGroup": "web",
                    "ClientStatus": "running",
                    "TaskStates": {"web": {}},
                }
            ],
            list_files=lambda alloc: files,
            readat=lambda alloc, path, offset, limit: b"hi\n",
            insert=lambda rows: None,
        )
        shipper.cursors["alloc-1/web/stdout"] = self.mod.Cursor(file="alloc/logs/web.stdout.0", offset=99)
        shipper.poll()
        self.assertEqual(shipper.cursors["alloc-1/web/stdout"].offset, 3)

    def test_utf8_split_across_reads_is_joined(self):
        lines, leftover = self.mod.split_lines("界".encode("utf-8")[1:], "界".encode("utf-8")[:1])
        self.assertEqual(leftover, "界".encode("utf-8"))
        self.assertEqual(lines, [])
        lines, leftover = self.mod.split_lines(b"\n", leftover)
        self.assertEqual(lines, ["界"])
        self.assertEqual(leftover, b"")

    def test_overlong_line_is_truncated(self):
        blob = b"x" * (self.mod.MAX_LINE + 8)
        lines, leftover = self.mod.split_lines(blob, b"")
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(lines[0]), self.mod.MAX_LINE)
        self.assertEqual(leftover, b"")

    def test_labels_use_job_id_and_full_alloc(self):
        source = self._source(alloc="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", job="office-worker")
        rows = self.mod.records_for(source, ["hello"], "2026-09-10T14:00:00.000Z")
        self.assertEqual(rows[0]["app"], "office-worker")
        self.assertEqual(rows[0]["job"], "office-worker")
        self.assertEqual(rows[0]["task"], "web")
        self.assertEqual(rows[0]["stream"], "stdout")
        self.assertEqual(rows[0]["alloc"], "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
        self.assertEqual(rows[0]["node"], "worker")
        self.assertEqual(rows[0]["_msg"], "hello")

    def test_victorialogs_error_drops_batch_and_advances_cursor(self):
        def boom(_rows):
            raise URLError("down")

        shipper = self.mod.Shipper(
            list_allocs=lambda: [
                {
                    "ID": "alloc-1",
                    "JobID": "granary",
                    "TaskGroup": "web",
                    "ClientStatus": "running",
                    "TaskStates": {"web": {}},
                }
            ],
            list_files=lambda alloc: [{"Name": "web.stdout.0", "Size": 5, "IsDir": False}],
            readat=lambda alloc, path, offset, limit: b"line\n",
            insert=boom,
        )
        shipper.cursors["alloc-1/web/stdout"] = self.mod.Cursor(file="alloc/logs/web.stdout.0", offset=0)
        shipper.poll()
        self.assertEqual(shipper.dropped, 1)
        self.assertEqual(shipper.exported, 0)
        self.assertEqual(shipper.cursors["alloc-1/web/stdout"].offset, 5)

    def test_nomad_list_timeout_sets_nomad_down(self):
        def fail():
            raise TimeoutError("nomad")

        shipper = self.mod.Shipper(
            list_allocs=fail,
            list_files=lambda alloc: [],
            readat=lambda *args: b"",
            insert=lambda rows: None,
        )
        shipper.poll()
        self.assertEqual(shipper.nomad_up, 0)
        self.assertIn("luma_observe_log_shipper_nomad_up 0", shipper.metrics())

    def test_round_robin_visits_source_beyond_cap(self):
        sources = [self._source(alloc=f"a{i}", task="web") for i in range(65)]
        first = self.mod.round_robin(sources, 0, cap=64)
        self.assertEqual(len(first), 64)
        self.assertEqual(first[0].alloc, "a0")
        second = self.mod.round_robin(sources, 64, cap=64)
        self.assertEqual(second[0].alloc, "a64")

    def test_insert_url_streams_app_not_alloc(self):
        url = self.mod.insert_jsonline_url("http://127.0.0.1:9428")
        self.assertIn("_stream_fields=app,job,task,stream", url)
        self.assertNotIn("alloc", url.split("_stream_fields=", 1)[1])
        self.assertTrue(url.startswith("http://127.0.0.1:9428/insert/jsonline?"))

    def test_traefik_json_stays_raw_in_msg(self):
        source = self._source(job="traefik", task="traefik")
        raw = '{"RequestHost":"itool.tech","DownstreamStatus":200}'
        rows = self.mod.records_for(source, [raw], "2026-09-10T14:00:00.000Z")
        self.assertEqual(rows[0]["_msg"], raw)
        self.assertEqual(rows[0]["app"], "traefik")


class LogShipperHttpErrorTests(unittest.TestCase):
    def test_http_500_is_a_drop(self):
        mod = load_observe("log_shipper.py", "log_shipper")

        def boom(_rows):
            raise HTTPError("http://127.0.0.1:9428", 500, "fail", hdrs=None, fp=Mock())

        shipper = mod.Shipper(
            list_allocs=lambda: [
                {
                    "ID": "alloc-1",
                    "JobID": "granary",
                    "ClientStatus": "running",
                    "TaskStates": {"web": {}},
                }
            ],
            list_files=lambda alloc: [{"Name": "web.stdout.0", "Size": 2, "IsDir": False}],
            readat=lambda *args: b"x\n",
            insert=boom,
        )
        shipper.cursors["alloc-1/web/stdout"] = mod.Cursor(file="alloc/logs/web.stdout.0", offset=0)
        shipper.poll()
        self.assertGreaterEqual(shipper.dropped, 1)
