import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ingest import OperationCancelled, copy_verified, create_proxies, movie_files, stable_files, stable_paths


class IngestTests(unittest.TestCase):
    def test_stable_paths_waits_for_an_exact_path_to_appear(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "late.mov"

            def publish():
                time.sleep(0.05)
                target.write_bytes(b"complete")

            thread = threading.Thread(target=publish)
            thread.start()
            result = stable_paths(
                [target], stable_seconds=0.05, settle_timeout_seconds=1
            )
            thread.join()
            self.assertEqual(result, [target])

    def test_copy_is_verified_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            clip = source / "Channel1" / "clip.mov"
            clip.parent.mkdir()
            clip.write_bytes(b"test recording")

            files = stable_files(source, {".mov"}, not_before=clip.stat().st_mtime - 1, stable_seconds=0, settle_timeout_seconds=0)
            self.assertEqual(files, [clip])
            first = copy_verified(files, source, destination)
            second = copy_verified(files, source, destination)
            self.assertTrue(first[0]["verified"])
            self.assertFalse(first[0]["skipped"])
            self.assertTrue(second[0]["skipped"])
            self.assertEqual((destination / "Channel1" / "clip.mov").read_bytes(), b"test recording")

    def test_movie_files_ignores_hidden_macos_sidecars(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            root.joinpath("clip.mp4").write_bytes(b"x")
            root.joinpath("._clip.mp4").write_bytes(b"x")
            self.assertEqual([p.name for p in movie_files(root, {".mp4"})], ["clip.mp4"])

    def test_cancelled_copy_removes_partial_and_preserves_existing_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            destination = root / "destination"
            source.mkdir()
            destination.mkdir()
            clip = source / "clip.mov"
            delivered = destination / "clip.mov"
            clip.write_bytes(b"new recording" * 1024)
            delivered.write_bytes(b"previous verified delivery")
            cancelled = threading.Event()

            def request_cancel(_progress):
                cancelled.set()

            with self.assertRaises(OperationCancelled):
                copy_verified(
                    [clip],
                    source,
                    destination,
                    cancel_event=cancelled,
                    progress=request_cancel,
                )

            self.assertEqual(delivered.read_bytes(), b"previous verified delivery")
            self.assertFalse((destination / ".clip.mov.partial").exists())

    def test_stable_paths_only_checks_the_session_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = root / "first.mov"
            later = root / "later.mov"
            first.write_bytes(b"closed take")
            snapshot = [first]
            later.write_bytes(b"next take")
            self.assertEqual(stable_paths(snapshot, stable_seconds=0, settle_timeout_seconds=0), [first])

    def test_create_proxy_and_verify_media(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            proxies = root / "proxies"
            source.mkdir()
            clip = source / "camera.mov"
            import subprocess

            subprocess.run(
                [
                    "/opt/homebrew/bin/ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc2=size=320x180:rate=25",
                    "-t",
                    "0.2",
                    "-c:v",
                    "prores_ks",
                    str(clip),
                ],
                check=True,
            )
            receipts = create_proxies([clip], source, proxies, height=180)
            self.assertTrue(receipts[0]["verified"])
            self.assertTrue(Path(receipts[0]["proxy"]).is_file())

    def test_cancelled_proxy_removes_partial_file(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            proxies = root / "proxies"
            source.mkdir()
            clip = source / "camera.mov"
            clip.write_bytes(b"recording")

            def cancelled_ffmpeg(command, **_kwargs):
                Path(command[-1]).write_bytes(b"unfinished proxy")
                raise OperationCancelled("cancelled")

            with patch("ingest._run_cancellable", side_effect=cancelled_ffmpeg):
                with self.assertRaises(OperationCancelled):
                    create_proxies([clip], source, proxies)

            self.assertFalse((proxies / ".camera_PROXY.partial.mp4").exists())


if __name__ == "__main__":
    unittest.main()
