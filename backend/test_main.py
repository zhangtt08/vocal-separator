import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class FakeDemucsProcess:
    def __init__(self, command, **_):
        output_root = Path(command[command.index("-o") + 1])
        input_path = Path(command[-1])
        result_dir = output_root / main.MODEL_NAME / input_path.stem
        result_dir.mkdir(parents=True)
        for stem_name in main.STEM_NAMES:
            (result_dir / f"{stem_name}.wav").write_bytes(b"RIFF" + b"\0" * 2048)
        self.stdout = iter(["10%\n", "65%\n", "100%\n"])
        self.returncode = 0

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15


class FakeFfmpegProcess:
    def __init__(self, command, **_):
        Path(command[-1]).write_bytes(b"RIFF" + b"\0" * 4096)
        self.stdout = iter([])
        self.returncode = 0

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.uploads = self.root / "uploads"
        self.outputs = self.root / "outputs"
        self.uploads.mkdir()
        self.outputs.mkdir()
        self.uploads_patch = patch.object(main, "UPLOADS_DIR", self.uploads)
        self.outputs_patch = patch.object(main, "OUTPUTS_DIR", self.outputs)
        self.uploads_patch.start()
        self.outputs_patch.start()
        with main.jobs_lock:
            main.jobs.clear()

    def tearDown(self):
        self.outputs_patch.stop()
        self.uploads_patch.stop()
        self.temporary_directory.cleanup()

    def test_rejects_invalid_inputs_and_download_paths(self):
        with TestClient(main.app) as client:
            self.assertEqual(client.get("/api/health").status_code, 200)
            self.assertEqual(
                client.post(
                    "/api/separate",
                    files={"file": ("notes.txt", b"text", "text/plain")},
                ).status_code,
                400,
            )
            self.assertEqual(
                client.post(
                    "/api/separate",
                    files={"file": ("empty.wav", b"", "audio/wav")},
                ).status_code,
                400,
            )
            self.assertEqual(
                client.post(
                    "/api/separate",
                    files={"file": ("clip.mp4", b"video", "video/mp4")},
                ).status_code,
                202,
            )
            self.assertEqual(
                client.get("/api/download/not-a-job/vocals").status_code,
                404,
            )

    def test_background_separation_uses_job_isolated_output(self):
        job_id = "abc123def456"
        input_path = self.uploads / f"{job_id}.wav"
        input_path.write_bytes(b"RIFF")
        now = time.time()
        with main.jobs_lock:
            main.jobs[job_id] = {
                "status": "queued",
                "progress": 1,
                "created_at": now,
                "updated_at": now,
            }

        with patch.object(main.subprocess, "Popen", FakeDemucsProcess):
            main.run_separation(job_id, input_path)

        job = main._public_job(job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["progress"], 100)
        self.assertEqual(set(job["stems"]), set(main.STEM_NAMES))
        self.assertFalse(input_path.exists())
        self.assertFalse((self.outputs / job_id / main.MODEL_NAME).exists())
        for stem_name in main.STEM_NAMES:
            self.assertTrue(
                (self.outputs / job_id / "stems" / f"{stem_name}.wav").is_file()
            )

    def test_video_input_converts_to_audio_before_demucs(self):
        job_id = "aaa111bbb222"
        input_path = self.uploads / f"{job_id}.mp4"
        input_path.write_bytes(b"fake video bytes")
        now = time.time()
        with main.jobs_lock:
            main.jobs[job_id] = {
                "status": "queued",
                "progress": 1,
                "cancel_requested": False,
                "created_at": now,
                "updated_at": now,
            }

        converted_path = input_path.with_suffix(".wav")

        def popen_side_effect(command, **_):
            if command[0] == "ffmpeg":
                return FakeFfmpegProcess(command)
            return FakeDemucsProcess(command)

        with (
            patch.object(main, "find_ffmpeg", return_value="ffmpeg"),
            patch.object(
                main.subprocess, "Popen", side_effect=popen_side_effect
            ) as popen_mock,
        ):
            main.run_separation(job_id, input_path)

        self.assertEqual(popen_mock.call_count, 2)
        ffmpeg_command = popen_mock.call_args_list[0].args[0]
        demucs_command = popen_mock.call_args_list[1].args[0]
        self.assertEqual(ffmpeg_command[-1], str(converted_path))
        self.assertEqual(demucs_command[-1], str(converted_path))

        job = main._public_job(job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["progress"], 100)
        self.assertEqual(set(job["stems"]), set(main.STEM_NAMES))
        self.assertFalse(input_path.exists())
        self.assertFalse(converted_path.exists())
        for stem_name in main.STEM_NAMES:
            self.assertTrue(
                (self.outputs / job_id / "stems" / f"{stem_name}.wav").is_file()
            )

    def test_video_input_fails_without_ffmpeg(self):
        job_id = "ccc333ddd444"
        input_path = self.uploads / f"{job_id}.mp4"
        input_path.write_bytes(b"fake video bytes")
        now = time.time()
        with main.jobs_lock:
            main.jobs[job_id] = {
                "status": "queued",
                "progress": 1,
                "cancel_requested": False,
                "created_at": now,
                "updated_at": now,
            }

        with patch.object(main, "find_ffmpeg", return_value=""):
            main.run_separation(job_id, input_path)

        job = main._public_job(job_id)
        self.assertIsNotNone(job)
        self.assertEqual(job["status"], "error")
        self.assertIn("ffmpeg", job["error"])
        self.assertFalse(input_path.exists())
        self.assertFalse((self.outputs / job_id).exists())

    def test_cancel_queued_job(self):
        job_id = "fed654cba321"
        now = time.time()
        with main.jobs_lock:
            main.jobs[job_id] = {
                "status": "queued",
                "progress": 1,
                "cancel_requested": False,
                "created_at": now,
                "updated_at": now,
            }

        with TestClient(main.app) as client:
            response = client.post(f"/api/jobs/{job_id}/cancel")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "cancelling")
        with main.jobs_lock:
            self.assertTrue(main.jobs[job_id]["cancel_requested"])


if __name__ == "__main__":
    unittest.main()
