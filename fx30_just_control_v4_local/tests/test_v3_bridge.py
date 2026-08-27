import threading
import tempfile
import unittest
import queue
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from just_control_api import JustControlApiError
from v4_bridge import Bridge, notify_request_failure


class StartApi:
    def __init__(self, starts_despite_error: bool):
        self.starts_despite_error = starts_despite_error
        self.recording = set()
        self.stop_calls = []

    def start_recording(self, channel: str, proposed_filename: str = "") -> dict:
        if self.starts_despite_error:
            self.recording.add(channel)
        raise JustControlApiError("false HTTP 400", status=400)

    def recording_status(self, channel: str) -> dict:
        return {"rec": channel in self.recording}

    def recording_paths(self, channel: str) -> dict:
        if channel in self.recording:
            return {"paths": [{"path": f"/recordings/{channel}.mov"}]}
        return {"paths": []}

    def stop_recording(self, channel: str) -> dict:
        self.stop_calls.append(channel)
        self.recording.discard(channel)
        return {}


def bridge_with(api: StartApi) -> Bridge:
    bridge = Bridge.__new__(Bridge)
    bridge.api = api
    bridge.channels = [{"name": "Channel1"}, {"name": "Channel2"}, {"name": "Channel3"}]
    bridge.lock = threading.RLock()
    bridge.recording = False
    bridge.session_started_at = None
    bridge.last_result = None
    bridge.post_status = {"state": "idle"}
    bridge.auto_post_record = False
    bridge.storage_manager = type("Capacity", (), {
        "consume_rodecaster_override": lambda _self: None,
        "mark_rodecaster_take_complete": lambda _self, _start, _stop: {"state": "confirmed"},
        "mark_rodecaster_take_attention": lambda _self, _reason: {"state": "attention"},
    })()
    bridge.prepare = lambda: {"preparedChannels": bridge.requested_names}
    return bridge


class BridgeStartTests(unittest.TestCase):
    def test_capacity_gate_failure_starts_no_recorder_channels(self):
        api = StartApi(starts_despite_error=True)
        bridge = bridge_with(api)
        bridge.prepare = lambda: (_ for _ in ()).throw(RuntimeError("RØDECaster capacity needs confirmation"))

        with self.assertRaisesRegex(RuntimeError, "capacity"):
            bridge.start_recording()

        self.assertEqual(api.recording, set())
        self.assertEqual(api.stop_calls, [])

    def test_false_http_error_is_accepted_when_recording_side_effect_is_verified(self):
        api = StartApi(starts_despite_error=True)
        bridge = bridge_with(api)

        result = bridge.start_recording()

        self.assertTrue(result["ok"])
        self.assertEqual(result["channels"], ["Channel1", "Channel2", "Channel3"])
        self.assertTrue(bridge.recording)
        self.assertEqual(api.stop_calls, [])

    @patch("v4_bridge.time.sleep")
    def test_failed_attempt_is_stopped_even_when_start_never_returned_success(self, _sleep):
        api = StartApi(starts_despite_error=False)
        bridge = bridge_with(api)

        with self.assertRaises(JustControlApiError):
            bridge.start_recording()

        self.assertFalse(bridge.recording)
        self.assertEqual(api.stop_calls, ["Channel1"])


class RequestFailureNotificationTests(unittest.TestCase):
    @patch("v4_bridge.notify_studio")
    def test_record_start_failure_uses_truthful_wording(self, notify):
        notify_request_failure("/v4/record/start", "Channel2 did not start")

        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], "record-start-failed")
        self.assertIn("Channel2 did not start", notify.call_args.args[2])

    @patch("v4_bridge.notify_studio")
    def test_stop_failure_uses_shared_dedupe_key(self, notify):
        notify_request_failure("/v4/record/stop", "Channel3 still recording")

        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], "stop-incomplete")
        self.assertIn("Channel3 still recording", notify.call_args.args[2])

    @patch("v4_bridge.notify_studio")
    def test_unrelated_request_does_not_notify(self, notify):
        notify_request_failure("/v4/prepare", "not ready")

        notify.assert_not_called()


class ChannelReadinessTests(unittest.TestCase):
    def test_recording_status_500_is_a_warning_not_channel_loss(self):
        class Api:
            def all_channels(self):
                return ["Channel1", "Channel2", "Channel3"]

            def active_channels(self):
                return ["Channel1", "Channel2", "Channel3"]

            def can_record(self, _name):
                return {"rec": True}

            def recording_status(self, name):
                if name in {"Channel2", "Channel3"}:
                    raise JustControlApiError("HTTP 500 for /ingest/requestRecordingStatus", status=500)
                return {"rec": False}

            def current_filename(self, _name):
                return {"value": ""}

        bridge = Bridge.__new__(Bridge)
        bridge.api = Api()
        bridge.channels = [{"name": name} for name in ("Channel1", "Channel2", "Channel3")]
        bridge.recording = False
        bridge.post_status = {"state": "idle"}
        bridge.last_result = None
        bridge.source_root = Path("/tmp")
        bridge.backup_root = None
        bridge.delivery_root = Path("/tmp")
        bridge.proxy_working_root = Path("/tmp")
        bridge.proxy_delivery_root = Path("/tmp")
        bridge.local_only = True
        bridge.include_rodecaster_in_completion = False
        bridge.storage_manager = type("Storage", (), {
            "report": lambda _self: {"ok": True},
            "_state": lambda _self: {},
        })()
        bridge._post_readiness = lambda: {"ok": True, "checks": {}}

        status = bridge.status()

        self.assertTrue(status["ok"])
        self.assertTrue(all(item["active"] and item["canRecord"] for item in status["channels"]))
        self.assertNotIn("error", status["channels"][1])
        self.assertIn("Recording status unavailable", status["channels"][1]["warnings"][0])


class BridgeCancellationTests(unittest.TestCase):
    def test_active_post_record_job_can_be_cancelled(self):
        bridge = Bridge.__new__(Bridge)
        bridge.lock = threading.RLock()
        bridge.post_cancel_event = threading.Event()
        bridge.post_status = {"state": "copying_masters", "jobId": "job-1", "cancellable": True}

        result = bridge.cancel_post_record()

        self.assertTrue(result["ok"])
        self.assertTrue(bridge.post_cancel_event.is_set())
        self.assertEqual(bridge.post_status["state"], "cancelling")
        self.assertEqual(bridge.post_status["previousState"], "copying_masters")

    def test_idle_post_record_job_cannot_be_cancelled(self):
        bridge = Bridge.__new__(Bridge)
        bridge.lock = threading.RLock()
        bridge.post_cancel_event = threading.Event()
        bridge.post_status = {"state": "idle"}

        with self.assertRaisesRegex(RuntimeError, "no active"):
            bridge.cancel_post_record()


class BridgeAcknowledgementTests(unittest.TestCase):
    def bridge(self):
        bridge = Bridge.__new__(Bridge)
        bridge.lock = threading.RLock()
        bridge.recording = False
        bridge.post_status = {"state": "idle"}
        bridge.last_result = None
        bridge.storage_manager = type("State", (), {})()
        return bridge

    def test_terminal_attention_can_be_acknowledged_without_media_work(self):
        bridge = self.bridge()
        bridge.post_status = {"state": "failed", "error": "old proxy failure"}
        bridge.storage_manager._state = lambda: {
            "postRecordVerification": {
                "state": "failed",
                "jobId": "old-job",
                "error": "old proxy failure",
            },
            "unrelated": {"preserved": True},
        }
        persisted = {}
        bridge._persist_runtime_state = lambda key, value: persisted.update({key: value})

        result = bridge.acknowledge_post_record_attention()

        self.assertTrue(result["ok"])
        self.assertEqual(result["previousState"], "failed")
        self.assertEqual(persisted["postRecordVerification"]["state"], "acknowledged")
        self.assertEqual(persisted["postRecordVerification"]["previousJobId"], "old-job")
        self.assertEqual(bridge.post_status, {"state": "idle"})

    def test_acknowledgement_is_blocked_while_recording(self):
        bridge = self.bridge()
        bridge.recording = True
        with self.assertRaisesRegex(RuntimeError, "while recording"):
            bridge.acknowledge_post_record_attention()


class BridgePostRecordRetryTests(unittest.TestCase):
    def bridge(self, root, state):
        bridge = Bridge.__new__(Bridge)
        bridge.lock = threading.RLock()
        bridge.recording = False
        bridge.post_status = {"state": "failed", "jobId": "failed-job"}
        bridge.source_root = root
        bridge.extensions = {".mov"}
        bridge.channels = [
            {"name": "Channel1"}, {"name": "Channel2"}, {"name": "Channel3"}
        ]
        bridge.post_cancel_event = threading.Event()
        bridge.post_queue = queue.Queue()
        bridge.storage_manager = type("State", (), {"_state": lambda _self: state})()
        bridge._persist_runtime_state = lambda key, value: state.update({key: value})
        return bridge

    def failed_state(self, started_at, stopped_at):
        return {
            "postRecordVerification": {"state": "failed", "jobId": "failed-job"},
            "rodecasterCapacity": {"lastVerifiedTake": {
                "startedAt": datetime.fromtimestamp(started_at, timezone.utc).isoformat(),
                "stoppedAt": datetime.fromtimestamp(stopped_at, timezone.utc).isoformat(),
            }},
        }

    def test_retry_queues_only_one_file_per_requested_channel_from_failed_take_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = time.time()
            for channel in ("Channel1", "Channel2", "Channel3"):
                path = root / f"take_{channel}.mov"
                path.write_bytes(channel.encode())
                os.utime(path, (now, now))
            old = root / "old_Channel1.mov"
            old.write_bytes(b"old")
            os.utime(old, (now - 300, now - 300))
            state = self.failed_state(now - 5, now + 1)
            bridge = self.bridge(root, state)

            result = bridge.retry_failed_post_record()
            job = bridge.post_queue.get_nowait()

            self.assertTrue(result["ok"])
            self.assertEqual(result["filesFound"], 3)
            self.assertEqual({item.name for item in job["files"]}, {
                "take_Channel1.mov", "take_Channel2.mov", "take_Channel3.mov"
            })
            self.assertEqual(state["postRecordVerification"]["state"], "running")

    def test_retry_accepts_only_valid_explicit_paths_inside_the_source_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = time.time()
            files = []
            for channel in ("Channel1", "Channel2", "Channel3"):
                path = root / f"take_{channel}.mov"
                path.write_bytes(channel.encode())
                os.utime(path, (now, now))
                files.append(str(path))
            state = self.failed_state(now - 5, now + 1)
            bridge = self.bridge(root, state)

            result = bridge.retry_failed_post_record(files)
            job = bridge.post_queue.get_nowait()

            self.assertEqual(result["filesFound"], 3)
            self.assertEqual({str(item) for item in job["files"]}, set(files))

    def test_retry_fails_closed_if_a_requested_channel_file_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            now = time.time()
            for channel in ("Channel1", "Channel2"):
                path = root / f"take_{channel}.mov"
                path.write_bytes(channel.encode())
                os.utime(path, (now, now))
            state = self.failed_state(now - 5, now + 1)
            bridge = self.bridge(root, state)

            with self.assertRaisesRegex(RuntimeError, "Channel3"):
                bridge.retry_failed_post_record()

            self.assertTrue(bridge.post_queue.empty())
            self.assertEqual(state["postRecordVerification"]["state"], "failed")

    def test_retry_is_blocked_while_recording(self):
        with tempfile.TemporaryDirectory() as temporary:
            now = time.time()
            state = self.failed_state(now - 5, now)
            bridge = self.bridge(Path(temporary), state)
            bridge.recording = True
            with self.assertRaisesRegex(RuntimeError, "while recording"):
                bridge.retry_failed_post_record()


class BridgePostReadinessTests(unittest.TestCase):
    def test_source_folder_that_exists_but_cannot_be_listed_is_not_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = Bridge.__new__(Bridge)
            bridge.source_root = root
            bridge.backup_root = None
            bridge.proxy_working_root = root / "Proxies"
            bridge.ffmpeg_path = "/opt/homebrew/bin/ffmpeg"
            bridge.ffprobe_path = "/opt/homebrew/bin/ffprobe"
            bridge.local_only = True

            with patch("v4_bridge.os.scandir", side_effect=PermissionError("blocked by macOS")):
                readiness = bridge._post_readiness()

            self.assertTrue(readiness["checks"]["sourceRoot"])
            self.assertFalse(readiness["checks"]["sourceFilesReadable"])
            self.assertFalse(readiness["ok"])


class OneJobQueue:
    def __init__(self, job):
        self.job = job
        self.read = False
        self.done = 0

    def get(self):
        if self.read:
            raise StopIteration
        self.read = True
        return self.job

    def task_done(self):
        self.done += 1


class PostWorkerNotificationTests(unittest.TestCase):
    @patch("v4_bridge.notify_studio")
    def test_terminal_worker_failure_notifies_once(self, notify):
        bridge = Bridge.__new__(Bridge)
        bridge.post_queue = OneJobQueue({"id": "failed-job"})
        bridge.mac_notifications = True
        bridge.last_result = None
        bridge.post_status = {"state": "idle"}
        bridge._persist_runtime_state = lambda _key, _value: None
        bridge._process_post_job = lambda _job: (_ for _ in ()).throw(RuntimeError("proxy verification failed"))

        with self.assertRaises(StopIteration):
            bridge._post_worker()

        self.assertEqual(bridge.post_status["state"], "failed")
        self.assertEqual(bridge.post_queue.done, 1)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], "post-record-failed-failed-job")
        self.assertIn("proxy verification failed", notify.call_args.args[2])


class PostRecordSummaryTests(unittest.TestCase):
    def bridge_with_state(self, verification, cleanup):
        bridge = Bridge.__new__(Bridge)
        bridge.recording = False
        bridge.post_status = {"state": "complete"}
        bridge.storage_manager = type("StorageState", (), {"_state": lambda _self: {
            "postRecordVerification": verification,
            "rodecasterCleanup": cleanup,
        }})()
        return bridge

    def test_all_checks_pass_only_after_video_and_rodecaster_verification(self):
        bridge = self.bridge_with_state(
            {"state": "complete", "mastersVerified": True, "proxiesVerified": True},
            {"state": "complete"},
        )

        result = bridge._post_record_summary()

        self.assertEqual(result["state"], "complete")
        self.assertTrue(result["allChecksPassed"])

    def test_video_pass_with_rodecaster_error_is_visible_attention(self):
        bridge = self.bridge_with_state(
            {"state": "complete", "mastersVerified": True, "proxiesVerified": True},
            {"state": "attention", "error": "card unavailable"},
        )

        result = bridge._post_record_summary()

        self.assertEqual(result["state"], "attention")
        self.assertIn("RØDECASTER", result["headline"])
        self.assertFalse(result["allChecksPassed"])

    def test_rodecaster_can_be_excluded_from_main_completion_result(self):
        bridge = self.bridge_with_state(
            {"state": "complete", "mastersVerified": True, "proxiesVerified": True},
            {"state": "attention", "error": "card unavailable"},
        )
        bridge.include_rodecaster_in_completion = False

        result = bridge._post_record_summary()

        self.assertEqual(result["state"], "complete")
        self.assertTrue(result["allChecksPassed"])
        self.assertNotIn("rodecaster", [item["key"] for item in result["checks"]])


class OpenDeliveredFilesTests(unittest.TestCase):
    @patch("v4_bridge.subprocess.run")
    def test_all_opens_only_configured_existing_delivery_folders(self, run):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = Bridge.__new__(Bridge)
            bridge.delivery_root = root / "masters"
            bridge.proxy_delivery_root = root / "proxies"
            bridge.delivery_root.mkdir()
            bridge.proxy_delivery_root.mkdir()

            result = bridge.open_delivered_files("all")

        self.assertTrue(result["ok"])
        opened_targets = [item["target"] for item in result["opened"]]
        self.assertEqual(opened_targets[:2], ["masters", "proxies"])
        self.assertGreaterEqual(run.call_count, 2)

    def test_unknown_target_is_rejected(self):
        bridge = Bridge.__new__(Bridge)
        bridge.delivery_root = Path("/tmp/masters")
        bridge.proxy_delivery_root = Path("/tmp/proxies")

        with self.assertRaisesRegex(ValueError, "Unknown delivered-files target"):
            bridge.open_delivered_files("somewhere-else")


class LocalPostRecordTests(unittest.TestCase):
    @patch("v4_bridge.copy_verified", side_effect=AssertionError("local mode must not copy masters or proxies"))
    @patch("v4_bridge.create_proxies")
    @patch("v4_bridge.stable_paths")
    def test_local_mode_finalises_masters_and_creates_proxies_without_delivery_copy(
        self, stable_paths, create_proxies, _copy_verified
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "masters"
            proxy_root = root / "Proxies"
            source_root.mkdir()
            master = source_root / "take_Channel1.mov"
            master.write_bytes(b"master")
            proxy = proxy_root / "take_Channel1_PROXY.mp4"
            proxy.parent.mkdir()
            proxy.write_bytes(b"proxy")
            stable_paths.return_value = [master]
            create_proxies.return_value = [{
                "source": str(master),
                "proxy": str(proxy),
                "bytes": proxy.stat().st_size,
                "skipped": False,
                "verified": True,
            }]

            bridge = Bridge.__new__(Bridge)
            bridge.source_root = source_root
            bridge.delivery_root = source_root
            bridge.proxy_working_root = proxy_root
            bridge.proxy_delivery_root = proxy_root
            bridge.proxy_extensions = {".mov"}
            bridge.local_only = True
            bridge.backup_root = None
            bridge.stable_seconds = 0
            bridge.settle_timeout_seconds = 1
            bridge.post_cancel_event = threading.Event()
            bridge.ffmpeg_path = "ffmpeg"
            bridge.ffprobe_path = "ffprobe"
            bridge.proxy_height = 1080
            bridge.proxy_video_bitrate = "10M"
            bridge.proxy_audio_bitrate = "192k"
            bridge.post_status = {"state": "idle"}
            bridge.last_result = None
            bridge._post_progress = lambda *_args, **_kwargs: (lambda _update: None)
            persisted = {}
            bridge._persist_runtime_state = lambda key, value: persisted.update({key: value})

            result = bridge._process_post_job({"id": "local-job", "files": [master]})

            self.assertTrue(result["ok"])
            self.assertEqual(result["masters"][0]["destination"], str(master))
            self.assertEqual(result["proxiesDelivered"][0]["destination"], str(proxy))
            self.assertTrue(persisted["postRecordVerification"]["mastersVerified"])
            self.assertTrue(persisted["postRecordVerification"]["proxiesVerified"])

    @patch("v4_bridge.copy_verified", side_effect=AssertionError("local mode must not copy masters or proxies"))
    @patch("v4_bridge.create_proxies")
    @patch("v4_bridge.stable_paths")
    @patch("v4_bridge.stable_files")
    def test_post_record_waits_for_files_that_appear_after_just_in_reports_stopped(
        self, stable_files, _stable_paths, create_proxies, _copy_verified
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "masters"
            proxy_root = root / "Proxies"
            source_root.mkdir()
            master = source_root / "late_Channel1.mov"
            master.write_bytes(b"master")
            proxy = proxy_root / "late_Channel1_PROXY.mp4"
            proxy.parent.mkdir()
            proxy.write_bytes(b"proxy")
            stable_files.return_value = [master]
            create_proxies.return_value = [{
                "source": str(master),
                "proxy": str(proxy),
                "bytes": proxy.stat().st_size,
                "skipped": False,
                "verified": True,
            }]

            bridge = Bridge.__new__(Bridge)
            bridge.source_root = source_root
            bridge.delivery_root = source_root
            bridge.proxy_working_root = proxy_root
            bridge.proxy_delivery_root = proxy_root
            bridge.extensions = {".mov"}
            bridge.proxy_extensions = {".mov"}
            bridge.local_only = True
            bridge.backup_root = None
            bridge.stable_seconds = 0
            bridge.settle_timeout_seconds = 1
            bridge.post_cancel_event = threading.Event()
            bridge.ffmpeg_path = "ffmpeg"
            bridge.ffprobe_path = "ffprobe"
            bridge.proxy_height = 1080
            bridge.proxy_video_bitrate = "10M"
            bridge.proxy_audio_bitrate = "192k"
            bridge.post_status = {"state": "idle"}
            bridge.last_result = None
            bridge._post_progress = lambda *_args, **_kwargs: (lambda _update: None)
            bridge._persist_runtime_state = lambda *_args: None

            result = bridge._process_post_job({"id": "late-job", "files": [], "notBefore": 123.0})

            self.assertTrue(result["ok"])
            stable_files.assert_called_once_with(
                source_root,
                {".mov"},
                not_before=123.0,
                stable_seconds=0,
                settle_timeout_seconds=1,
            )

    @patch("v4_bridge.notify_studio")
    @patch("v4_bridge.copy_verified", side_effect=AssertionError("local mode must not copy masters or proxies"))
    @patch("v4_bridge.create_proxies", return_value=[])
    @patch("v4_bridge.stable_paths")
    def test_missing_verified_proxy_is_attention_and_notified(self, stable_paths, _create, _copy, notify):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "masters"
            source_root.mkdir()
            master = source_root / "take.mov"
            master.write_bytes(b"master")
            stable_paths.return_value = [master]

            bridge = Bridge.__new__(Bridge)
            bridge.source_root = source_root
            bridge.delivery_root = source_root
            bridge.proxy_working_root = root / "proxies"
            bridge.proxy_delivery_root = bridge.proxy_working_root
            bridge.proxy_extensions = {".mov"}
            bridge.local_only = True
            bridge.backup_root = None
            bridge.stable_seconds = 0
            bridge.settle_timeout_seconds = 1
            bridge.post_cancel_event = threading.Event()
            bridge.ffmpeg_path = "ffmpeg"
            bridge.ffprobe_path = "ffprobe"
            bridge.proxy_height = 1080
            bridge.proxy_video_bitrate = "10M"
            bridge.proxy_audio_bitrate = "192k"
            bridge.mac_notifications = True
            bridge.notify_post_success = False
            bridge.post_status = {"state": "idle"}
            bridge.last_result = None
            bridge._post_progress = lambda *_args, **_kwargs: (lambda _update: None)
            bridge._persist_runtime_state = lambda _key, _value: None

            bridge._process_post_job({"id": "attention-job", "files": [master]})

        self.assertEqual(bridge.post_status["state"], "attention")
        self.assertFalse(bridge.last_result["ok"])
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], "post-record-attention-attention-job")


class StopApi:
    def __init__(self, false_error_channels=(), stuck_channels=()):
        self.recording = {"Channel1", "Channel2", "Channel3"}
        self.stop_calls = []
        self.false_error_channels = set(false_error_channels)
        self.stuck_channels = set(stuck_channels)

    def stop_recording(self, channel: str) -> dict:
        self.stop_calls.append(channel)
        if channel not in self.stuck_channels:
            self.recording.discard(channel)
        if channel in self.false_error_channels:
            raise JustControlApiError("false HTTP 500", status=500)
        return {}

    def recording_status(self, channel: str) -> dict:
        return {"rec": channel in self.recording}

    def recording_paths(self, channel: str) -> dict:
        return {"paths": [f"/recordings/{channel}.mov"] if channel in self.recording else []}


class BridgeStopTests(unittest.TestCase):
    def test_stop_sends_every_channel_and_clears_session(self):
        api = StopApi()
        bridge = bridge_with(api)
        bridge.recording = True
        bridge.session_started_at = 123.0

        result = bridge.stop_recording()

        self.assertTrue(result["ok"])
        self.assertFalse(result["alreadyStopped"])
        self.assertEqual(api.stop_calls, ["Channel1", "Channel2", "Channel3"])
        self.assertFalse(bridge.recording)
        self.assertIsNone(bridge.session_started_at)

    @patch("v4_bridge.time.time", return_value=183.0)
    def test_verified_stop_charges_capacity_from_verified_take_elapsed_time(self, _time):
        api = StopApi()
        bridge = bridge_with(api)
        bridge.recording = True
        bridge.session_started_at = 123.0
        charged = []
        bridge.storage_manager.mark_rodecaster_take_complete = lambda start, stop: charged.append((start, stop)) or {"state": "confirmed"}

        result = bridge.stop_recording()

        self.assertEqual(charged, [(123.0, 183.0)])
        self.assertEqual(result["rodecasterCapacity"]["state"], "confirmed")

    def test_false_http_error_is_accepted_when_stop_side_effect_is_verified(self):
        api = StopApi(false_error_channels={"Channel2"})
        bridge = bridge_with(api)
        bridge.recording = True

        result = bridge.stop_recording()

        self.assertTrue(result["ok"])
        self.assertEqual(result["channels"], ["Channel1", "Channel2", "Channel3"])

    @patch("v4_bridge.time.sleep")
    def test_channel_still_recording_fails_but_other_channels_are_stopped(self, _sleep):
        api = StopApi(stuck_channels={"Channel2"})
        bridge = bridge_with(api)
        bridge.recording = True

        with self.assertRaisesRegex(RuntimeError, "Channel2"):
            bridge.stop_recording()

        self.assertEqual(api.stop_calls, ["Channel1", "Channel2", "Channel3"])
        self.assertEqual(api.recording, {"Channel2"})

    @patch("v4_bridge.time.sleep")
    def test_incomplete_stop_requires_capacity_reconfirmation(self, _sleep):
        api = StopApi(stuck_channels={"Channel2"})
        bridge = bridge_with(api)
        bridge.recording = True
        attention = []
        bridge.storage_manager.mark_rodecaster_take_attention = attention.append

        with self.assertRaisesRegex(RuntimeError, "Channel2"):
            bridge.stop_recording()

        self.assertEqual(len(attention), 1)
        self.assertIn("not every recorder stop", attention[0].lower())

    def test_repeated_stop_does_not_queue_duplicate_post_record_job(self):
        api = StopApi()
        api.recording.clear()
        bridge = bridge_with(api)
        bridge.auto_post_record = True
        bridge._queue_post_record = lambda _started: self.fail("duplicate post job queued")

        result = bridge.stop_recording()

        self.assertTrue(result["alreadyStopped"])
        self.assertNotIn("postRecord", result)


if __name__ == "__main__":
    unittest.main()
