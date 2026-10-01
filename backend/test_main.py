"""后端契约与能力测试。

覆盖三件事：
1. 原有分离链路（任务隔离目录、视频转音频、缺 ffmpeg、取消）；
2. 新能力（音轨预设、阶段/进度、历史索引与重复判定、按路径提交）；
3. Agent API 契约端点的四种响应形状。

测试用假 Demucs/ffmpeg 进程，不碰真显卡；但 `vocal.env_probe` 那条**故意断言真实值**
（解释器路径、是否装了 demucs），因为"工具不得返回假数据"是契约里最容易被悄悄破坏的一条。
"""

import importlib.util
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class FakeDemucsProcess:
    """假 Demucs：按命令行里的 --two-stems 决定输出哪几条轨，并打印真实形状的进度。"""

    def __init__(self, command, **_):
        output_root = Path(command[command.index("-o") + 1])
        input_path = Path(command[-1])
        result_dir = output_root / main.MODEL_NAME / input_path.stem
        result_dir.mkdir(parents=True)
        if "--two-stems" in command:
            stem_names = main.BACKING_STEM_NAMES
        else:
            stem_names = main.STEM_NAMES
        for stem_name in stem_names:
            (result_dir / f"{stem_name}.wav").write_bytes(b"RIFF" + b"\0" * 2048)
        self.stdout = iter(
            [
                "Selected model is a bag of 1 models.\n",
                "Separating track song\n",
                " 10%| # | 1.0/10.0 [00:01<00:09]\n",
                " 65%| #### | 6.5/10.0 [00:06<00:03]\n",
                "100%| ###### | 10.0/10.0 [00:09<00:00]\n",
            ]
        )
        self.returncode = 0

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    def close(self):
        return None


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
        self.data_patch = patch.object(main, "DATA_DIR", self.root)
        self.uploads_patch.start()
        self.outputs_patch.start()
        self.data_patch.start()
        with main.jobs_lock:
            main.jobs.clear()

    def tearDown(self):
        self.data_patch.stop()
        self.outputs_patch.stop()
        self.uploads_patch.stop()
        self.temporary_directory.cleanup()

    # ────────────────────────── 原有链路 ──────────────────────────

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

    # ────────────────────────── 进度与阶段 ──────────────────────────

    def _finished_job(self, job_id: str, preset: str) -> str:
        input_path = self.uploads / f"{job_id}.wav"
        input_path.write_bytes(b"RIFF")
        main.jobs[job_id] = {
            "status": "queued",
            "progress": 1,
            "cancel_requested": False,
            "preset": preset,
            "stem_names": list(main.PRESETS[preset]["stems"]),
            "source_name": "song.wav",
            "source_bytes": 4,
            "output_dir": str(self.outputs / job_id / "stems"),
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        with patch.object(main.subprocess, "Popen", FakeDemucsProcess):
            main.run_separation(job_id, input_path)
        return job_id

    def test_progress_reports_phase_and_real_stem_paths(self):
        job = main.job_snapshot(self._finished_job("fa5e00000001", "four_stems"))
        self.assertEqual(job["phase"], "finished")
        self.assertEqual(job["phase_label"], "已完成")
        self.assertIsInstance(job["elapsed_seconds"], float)
        for name, info in job["stems"].items():
            self.assertTrue(Path(info["path"]).is_file())
            self.assertEqual(info["bytes"], Path(info["path"]).stat().st_size)
            self.assertTrue(info["exists"])
            self.assertEqual(info["label"], main.STEM_LABELS[name]["zh"])

    def test_vocal_backing_preset_produces_two_stems(self):
        job_id = self._finished_job("7b2c57000001", "vocal_backing")
        job = main.job_snapshot(job_id)
        self.assertEqual(set(job["stems"]), {"vocals", "no_vocals"})
        with TestClient(main.app) as client:
            self.assertEqual(client.get(f"/api/download/{job_id}/no_vocals").status_code, 200)
            # 这个预设根本没产出 drums：下载它必须是 404，而不是"文件不存在"含糊过关。
            self.assertEqual(client.get(f"/api/download/{job_id}/drums").status_code, 404)
            self.assertEqual(client.get(f"/api/download/{job_id}/../x").status_code, 404)

    def test_history_index_and_duplicate_lookup(self):
        job_id = self._finished_job("415708700001", "four_stems")
        with TestClient(main.app) as client:
            listed = client.get("/api/history").json()
            self.assertEqual(listed["total"], 1)
            entry = listed["items"][0]
            self.assertEqual(entry["job_id"], job_id)
            self.assertEqual(entry["source_name"], "song.wav")
            self.assertEqual(entry["available_stems"], 4)
            self.assertFalse(entry["expired"])

            matched = client.get("/api/history", params={"name": "SONG.WAV", "bytes": 4}).json()
            self.assertEqual(matched["total"], 1)
            missed = client.get("/api/history", params={"name": "other.wav", "bytes": 4}).json()
            self.assertEqual(missed["total"], 0)

        # 结果文件被清理之后，重复提示不该再拿这条历史拦人。
        import shutil

        shutil.rmtree(self.outputs / job_id, ignore_errors=True)
        with TestClient(main.app) as client:
            stale = client.get("/api/history", params={"name": "song.wav", "bytes": 4}).json()
            self.assertEqual(stale["total"], 0)

    def test_unknown_preset_is_rejected(self):
        with TestClient(main.app) as client:
            response = client.post(
                "/api/separate",
                files={"file": ("song.wav", b"RIFF", "audio/wav")},
                data={"preset": "twelve_stems"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertIn("预设", response.json()["detail"])

    def test_submit_source_file_validates_before_touching_demucs(self):
        calls: list[tuple[str, Path]] = []

        def fake_launch(job_id: str, input_path: Path) -> None:
            calls.append((job_id, Path(input_path)))

        source = self.root / "input.mp3"
        source.write_bytes(b"ID3fake")

        with patch.object(main, "launch_separation", fake_launch):
            with self.assertRaises(FileNotFoundError):
                main.submit_source_file(self.root / "missing.wav")

            bad = self.root / "notes.txt"
            bad.write_bytes(b"text")
            with self.assertRaises(ValueError):
                main.submit_source_file(bad)

            empty = self.root / "empty.wav"
            empty.write_bytes(b"")
            with self.assertRaises(ValueError):
                main.submit_source_file(empty)

            job_id, working_copy = main.submit_source_file(source, "vocal_backing")

        self.assertTrue(working_copy.is_file())
        self.assertEqual(working_copy.read_bytes(), b"ID3fake")
        self.assertEqual(calls[0][0], job_id)
        snapshot = main.job_snapshot(job_id)
        self.assertEqual(snapshot["preset"], "vocal_backing")
        self.assertEqual(snapshot["source_name"], "input.mp3")
        self.assertEqual(snapshot["source_bytes"], 7)
        self.assertEqual(snapshot["phase"], "queued")

    # ────────────────────────── Agent API 契约 ──────────────────────────

    def test_health_carries_both_shapes(self):
        with TestClient(main.app) as client:
            payload = client.get("/api/health").json()
        # Electron 壳与 start.bat 读的旧键
        self.assertEqual(payload["status"], "ok")
        self.assertIn("demucs_available", payload)
        self.assertIn("ffmpeg_available", payload)
        # 标准信封
        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["project"], "vocal-separator")
        self.assertEqual(data["agent_api"], 1)
        self.assertIsInstance(data["uptime_ms"], int)
        self.assertEqual({entry["preset"] for entry in data["presets"]}, set(main.PRESETS))

    def test_agent_tools_and_manifest_envelopes(self):
        with TestClient(main.app) as client:
            tools = client.get("/api/agent/tools").json()
            manifest = client.get("/api/agent/manifest").json()

        self.assertTrue(tools["ok"])
        names = [entry["name"] for entry in tools["data"]]
        self.assertEqual(len(names), len(main.PRESETS) + 5)
        self.assertTrue(all(name.startswith("vocal.") for name in names), names)
        for entry in tools["data"]:
            self.assertTrue(entry["description"])
            self.assertEqual(entry["input_schema"]["type"], "object")
            self.assertIn(entry["risk"], {"read", "write", "exec"})
        submit = next(entry for entry in tools["data"] if entry["name"] == "vocal.separate_submit")
        self.assertEqual(submit["risk"], "exec")
        self.assertIn("confirm", submit["input_schema"]["required"])

        self.assertTrue(manifest["ok"])
        self.assertEqual(manifest["data"]["project"], "vocal-separator")
        self.assertEqual(len(manifest["data"]["tools"]), len(names))

    def _tool(self, client: TestClient, name: str, argument: dict):
        return client.post("/api/agent/tool", json={"tool": name, "input": argument})

    def test_agent_env_probe_returns_real_machine_facts(self):
        with TestClient(main.app) as client:
            response = self._tool(client, "vocal.env_probe", {})
            self.assertEqual(response.status_code, 200)
            data = response.json()["data"]
            self.assertEqual(response.json()["tool"], "vocal.env_probe")
            self.assertIn("ms", response.json())

        if data["status"] == "ready":
            self.assertEqual(data["python_executable"], sys.executable)
            self.assertEqual(data["python_version"], ".".join(str(part) for part in sys.version_info[:3]))
            self.assertEqual(
                data["demucs_installed"],
                importlib.util.find_spec("demucs") is not None,
            )
            self.assertEqual(data["model_cache"]["model"], main.MODEL_NAME)
            self.assertTrue(data["outputs_dir"])
        else:
            self.assertEqual(data["status"], "probing")

    def test_agent_error_shapes(self):
        with TestClient(main.app) as client:
            missing = self._tool(client, "vocal.job_status", {})
            self.assertEqual(missing.status_code, 400)
            self.assertEqual(missing.json()["error"]["code"], "bad_input")
            self.assertIn("job_id", missing.json()["error"]["message"])

            unknown_key = self._tool(client, "vocal.job_status", {"job_id": "x", "extra": 1})
            self.assertEqual(unknown_key.status_code, 400)
            self.assertEqual(unknown_key.json()["error"]["code"], "bad_input")

            unknown_tool = self._tool(client, "vocal.nope", {})
            self.assertEqual(unknown_tool.status_code, 400)
            self.assertEqual(unknown_tool.json()["error"]["code"], "unknown_tool")
            self.assertIn("vocal.env_probe", unknown_tool.json()["error"]["available"])

            bad_json = client.post(
                "/api/agent/tool",
                content=b"not json",
                headers={"content-type": "application/json"},
            )
            self.assertEqual(bad_json.status_code, 400)
            self.assertEqual(bad_json.json()["error"]["code"], "bad_json")

            not_found = self._tool(client, "vocal.job_status", {"job_id": "deadbeef0000"})
            self.assertEqual(not_found.json()["error"]["code"], "not_found")

    def test_exec_tool_requires_explicit_confirm(self):
        source = self.root / "karaoke.flac"
        source.write_bytes(b"fAKE")
        with TestClient(main.app) as client:
            no_field = self._tool(client, "vocal.separate_submit", {"input_path": str(source)})
            self.assertEqual(no_field.status_code, 400)
            self.assertIn("confirm", no_field.json()["error"]["message"])

            refused = self._tool(
                client, "vocal.separate_submit", {"input_path": str(source), "confirm": False}
            )
            self.assertEqual(refused.status_code, 400)
            self.assertEqual(refused.json()["error"]["code"], "bad_input")
            # 没确认就一个任务都不该建出来，也不该碰 uploads。
            self.assertEqual(main.job_snapshot("whatever"), None)
            self.assertEqual(list(self.uploads.iterdir()), [])

            missing_file = self._tool(
                client,
                "vocal.separate_submit",
                {"input_path": str(self.root / "gone.wav"), "confirm": True},
            )
            self.assertEqual(missing_file.json()["error"]["code"], "not_found")

            bad_preset = self._tool(
                client,
                "vocal.separate_submit",
                {"input_path": str(source), "confirm": True, "preset": "nope"},
            )
            self.assertEqual(bad_preset.json()["error"]["code"], "bad_input")

    def test_agent_tools_read_write_against_real_files(self):
        job_id = self._finished_job("b017d1900001", "four_stems")
        with TestClient(main.app) as client:
            status = self._tool(client, "vocal.job_status", {"job_id": job_id}).json()["data"]
            self.assertTrue(status["terminal"])
            self.assertEqual(status["status"], "done")
            self.assertEqual(len(status["output_files"]), 4)
            for path in status["output_files"]:
                self.assertTrue(Path(path).is_file())
            self.assertEqual(
                status["total_output_bytes"],
                sum(Path(path).stat().st_size for path in status["output_files"]),
            )

            waited = self._tool(
                client, "vocal.job_wait", {"job_id": job_id, "timeout_seconds": 2}
            ).json()["data"]
            self.assertFalse(waited["timed_out"])

            outputs = self._tool(client, "vocal.output_list", {"limit": 5}).json()["data"]
            self.assertEqual(outputs["total"], 1)
            item = outputs["items"][0]
            self.assertEqual(item["job_id"], job_id)
            self.assertEqual(item["source_name"], "song.wav")
            on_disk = sum(
                Path(stem["path"]).stat().st_size for stem in item["stems"].values() if stem["exists"]
            )
            self.assertEqual(item["total_bytes"], on_disk)
            self.assertIn(f"/api/download/{job_id}/vocals", item["download_paths"]["vocals"])

            mode_list = self._tool(client, "vocal.mode_list", {}).json()["data"]
            self.assertEqual(mode_list["model"], main.MODEL_NAME)
            self.assertEqual({entry["preset"] for entry in mode_list["presets"]}, set(main.PRESETS))

            cancelled = self._tool(client, "vocal.job_cancel", {"job_id": job_id})
            self.assertEqual(cancelled.json()["error"]["code"], "conflict")


if __name__ == "__main__":
    unittest.main()
