import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from tests.test_observe import load_observe


class LogShipperTests(unittest.TestCase):
    def setUp(self):
        self.mod = load_observe("log_shipper.py", "log_shipper")

    def _source(self, **values):
        return self.mod.Source(
            alloc=values.get("alloc", "alloc-1"),
            job=values.get("job", "ledger"),
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
                    "JobID": "ledger",
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
                    "JobID": "ledger",
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

    def test_victorialogs_error_retains_cursor_for_retry(self):
        def boom(_rows):
            raise URLError("down")

        shipper = self.mod.Shipper(
            list_allocs=lambda: [
                {
                    "ID": "alloc-1",
                    "JobID": "ledger",
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
        self.assertEqual(shipper.dropped, 0)
        self.assertEqual(shipper.export_retries, 1)
        self.assertEqual(shipper.exported, 0)
        self.assertEqual(shipper.cursors["alloc-1/web/stdout"].offset, 0)
        self.assertEqual(shipper.backlog_bytes, 5)

    def test_backlogged_stream_catches_up_and_shares_allocation_listing(self):
        data = {"alloc/logs/web.stdout.0": b"event\n" * 20000,
                "alloc/logs/web.stderr.0": b"quiet\n"}
        listing = Mock(return_value=[{"Name": path.rsplit("/", 1)[1], "Size": len(value)}
                                     for path, value in data.items()])
        inserted = []
        shipper = self.mod.Shipper(
            list_allocs=lambda: [{"ID": "a", "JobID": "app", "ClientStatus": "running", "TaskStates": {"web": {}}}],
            list_files=listing, readat=lambda alloc, path, offset, limit: data[path][offset:offset+limit],
            insert=inserted.extend)
        shipper.cursors["a/web/stdout"] = self.mod.Cursor("alloc/logs/web.stdout.0", 0)
        shipper.poll()
        listing.assert_called_once_with("a")
        self.assertEqual(len(inserted), 20001)
        self.assertEqual(shipper.backlog_bytes, 0)
        self.assertGreater(shipper.coverage_snapshot()[0]["at"], 0)

    def test_byte_budget_rotates_sources_and_keeps_unvisited_backlog(self):
        data = {f"alloc/logs/{task}.{stream}.0": (task.encode()+b"\n") * (1024 if task != "quiet" else 1)
                if stream == "stdout" else b"" for task in ("hot", "next", "quiet") for stream in ("stdout", "stderr")}
        reads, inserted = [], []
        def readat(alloc, path, offset, limit):
            value = data[path][offset:offset+limit]
            reads.append(len(value))
            return value
        shipper = self.mod.Shipper(
            list_allocs=lambda: [{"ID": "a", "JobID": "app", "ClientStatus": "running",
                                 "TaskStates": {task: {} for task in ("hot", "next", "quiet")}}],
            list_files=lambda alloc: [{"Name": path.rsplit("/", 1)[1], "Size": len(value)} for path, value in data.items()],
            readat=readat, insert=inserted.extend)
        for path in data:
            task, stream, _ = path.rsplit("/", 1)[1].split(".")
            shipper.cursors[f"a/{task}/{stream}"] = self.mod.Cursor(path, 0)
        with patch.object(self.mod, "READ_LIMIT", 1024), patch.object(self.mod, "POLL_READ_LIMIT", 1024):
            shipper.poll()
            hot_backlog = shipper.backlog_bytes
            reads.clear()
            shipper.poll()
            self.assertLessEqual(sum(reads), 1024)
            self.assertEqual(shipper.backlogs["a/hot/stdout"], hot_backlog)
            self.assertGreater(shipper.backlog_bytes, hot_backlog)
            reads.clear()
            shipper.poll()
            self.assertLessEqual(sum(reads), 1024)
        self.assertIn("quiet", [row["_msg"] for row in inserted])
        self.assertGreater(shipper.backlogs["a/hot/stdout"], 0)

    def test_short_read_keeps_unread_tail_before_following_rotation(self):
        data = {"alloc/logs/web.stdout.0": b"one\ntwo\n", "alloc/logs/web.stdout.1": b"three\n",
                "alloc/logs/web.stderr.0": b""}
        inserted = []
        def readat(alloc, path, offset, limit):
            if path.endswith("stdout.0") and offset == 0:
                return data[path][:4]
            return data[path][offset:offset+limit]
        shipper = self.mod.Shipper(
            list_allocs=lambda: [{"ID": "a", "JobID": "app", "ClientStatus": "running", "TaskStates": {"web": {}}}],
            list_files=lambda alloc: [{"Name": path.rsplit("/", 1)[1], "Size": len(value)} for path, value in data.items()],
            readat=readat, insert=inserted.extend)
        shipper.cursors["a/web/stdout"] = self.mod.Cursor("alloc/logs/web.stdout.0", 0)
        shipper.poll()
        self.assertEqual(shipper.coverage_snapshot()[0]["at"], 0)
        shipper.poll()
        self.assertEqual([row["_msg"] for row in inserted], ["one", "two", "three"])
        self.assertGreater(shipper.coverage_snapshot()[0]["at"], 0)

    def test_unreachable_allocation_listing_is_attempted_once_per_poll(self):
        listing = Mock(side_effect=TimeoutError("offline"))
        shipper = self.mod.Shipper(
            list_allocs=lambda: [{"ID": "a", "JobID": "app", "ClientStatus": "running",
                                 "TaskStates": {"web": {}, "worker": {}}}],
            list_files=listing, readat=Mock(), insert=Mock())
        shipper.poll()
        listing.assert_called_once_with("a")
        shipper.readat.assert_not_called()
        self.assertTrue(all(row["at"] == 0 for row in shipper.coverage_snapshot()))

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
        raw = '{"RequestHost":"example.net","DownstreamStatus":200}'
        rows = self.mod.records_for(source, [raw], "2026-09-10T14:00:00.000Z")
        self.assertEqual(rows[0]["_msg"], raw)
        self.assertEqual(rows[0]["app"], "traefik")


class LogShipperHttpErrorTests(unittest.TestCase):
    def test_http_500_is_retryable(self):
        mod = load_observe("log_shipper.py", "log_shipper")

        def boom(_rows):
            raise HTTPError("http://127.0.0.1:9428", 500, "fail", hdrs=None, fp=Mock())

        shipper = mod.Shipper(
            list_allocs=lambda: [
                {
                    "ID": "alloc-1",
                    "JobID": "ledger",
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
        self.assertEqual(shipper.dropped, 0)
        self.assertEqual(shipper.export_retries, 1)

class StructuredLogsAndCheckpointTests(unittest.TestCase):
    def test_keep_both_clocks_and_only_normalized_correlation_fields(self):
        import json
        m=load_observe('log_shipper.py','shipper_clocks')
        source=m.Source('alloc','app','worker','group','stdout','node')
        data={'timestamp':1000, 'trace_id':'a'*32,'span_id':'b'*16,'service.name':'worker',
              'service.instance.id':'worker:1','attributes':{'messaging.message.id':'task-1'},'secret':'never-a-label'}
        row=m.records_for(source,[json.dumps(data)],'2026-11-04T00:00:00Z')[0]
        self.assertEqual(row['_time'],row['observed_at'])
        self.assertNotEqual(row['event_time'],row['observed_at'])
        self.assertEqual(row['trace_id'],'a'*32)
        self.assertEqual(row['task_id'],'task-1')
        self.assertEqual(row['service_name'],'worker')
        self.assertNotIn('secret',row)

    def test_cursor_checkpoint_survives_process_recreation(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        m=load_observe('log_shipper.py','shipper_checkpoint')
        with tempfile.TemporaryDirectory() as root, patch.dict(m.os.environ,{'LUMA_LOG_CHECKPOINT':str(Path(root)/'checkpoint.json')}):
            args=dict(list_allocs=lambda:[],list_files=lambda _:[],readat=lambda *_:b'',insert=lambda _:None)
            first=m.Shipper(**args)
            first.cursors['alloc/task/stdout']=m.Cursor('alloc/logs/task.stdout.2',42,b'partial')
            first.save_checkpoint()
            second=m.Shipper(**args)
            self.assertEqual(second.cursors['alloc/task/stdout'].offset,42)
            self.assertEqual(second.cursors['alloc/task/stdout'].leftover,b'partial')
