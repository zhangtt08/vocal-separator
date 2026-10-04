"""后端契约与能力测试。

覆盖三件事：
1. 原有分离链路（任务隔离目录、视频转音频、缺 ffmpeg、取消）；
2. 新能力（音轨预设、阶段/进度、历史索引与重复判定、按路径提交）；
3. Agent API 契约端点的四种响应形状。

测试用假 Demucs/ffmpeg 进程，不碰真显卡；但 `vocal.env_probe` 那条**故意断言真实值**
（解释器路径、是否装了 demucs），因为"工具不得返回假数据"是契约里最容易被悄悄破坏的一条。
"""

import asyncio
import importlib.util
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main

# 闸门只认回环 Host（local_guard 的判据），所以测试客户端也必须用它要打的那个地址：
# 默认的 http://testserver 会被当成"不是本机"直接拒掉 —— 那正是这道闸门要做的事。
LOOPBACK_BASE = "http://127.0.0.1:8000"


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


class RecordingDemucsProcess(FakeDemucsProcess):
    """记下 Popen 收到的参数：子进程环境必须自己可控，否则进度与原文都会失真。"""

    captured: dict = {}

    def __init__(self, command, **kwargs):
        RecordingDemucsProcess.captured = {"command": list(command), "kwargs": dict(kwargs)}
        super().__init__(command, **kwargs)


class ServiceSandbox:
    """把 DATA_DIR / uploads / outputs 挪进临时目录：后端在测试里不碰真实工作目录。"""

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
        # Background environment probing uses subprocess.Popen too. Keep it out
        # of pipeline mocks; the explicit environment test still probes reality.
        self.probe_start_patch = patch.object(main, "_start_environment_probe")
        self.probe_start_patch.start()
        with main.jobs_lock:
            main.jobs.clear()

    def tearDown(self):
        self.probe_start_patch.stop()
        self.data_patch.stop()
        self.outputs_patch.stop()
        self.uploads_patch.stop()
        self.temporary_directory.cleanup()

class BackendTests(ServiceSandbox, unittest.TestCase):
    def test_cancelled_upload_leaves_no_file_or_queued_job(self):
        class InterruptedUpload:
            filename = "song.wav"
            reads = 0
            closed = False

            async def read(self, _size):
                self.reads += 1
                if self.reads == 1:
                    return b"partial audio"
                raise asyncio.CancelledError

            async def close(self):
                self.closed = True

        upload = InterruptedUpload()
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(main.separate_audio(main.BackgroundTasks(), upload, preset=None))
        self.assertTrue(upload.closed)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assertEqual(main.jobs, {})

    # ────────────────────────── 原有链路 ──────────────────────────

    def test_rejects_invalid_inputs_and_download_paths(self):
        with TestClient(main.app, base_url=LOOPBACK_BASE) as client:
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

        with TestClient(main.app, base_url=LOOPBACK_BASE) as client:
            response = client.post(f"/api/jobs/{job_id}/cancel")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "cancelling")
        with main.jobs_lock:
            self.assertTrue(main.jobs[job_id]["cancel_requested"])

    # ────────────────────────── 进度与阶段 ──────────────────────────

    def _seed_job(self, job_id: str, preset: str = "four_stems") -> Path:
        """造一个刚排进队的任务，返回它的上传副本路径。"""
        input_path = self.uploads / f"{job_id}.wav"
        input_path.write_bytes(b"RIFF")
        now = time.time()
        with main.jobs_lock:
            main.jobs[job_id] = {
                "status": "queued",
                "progress": 1,
                "phase": "queued",
                "cancel_requested": False,
                "preset": preset,
                "stem_names": list(main.PRESETS[preset]["stems"]),
                "source_name": "song.wav",
                "source_bytes": 4,
                "output_dir": str(self.outputs / job_id / "stems"),
                "created_at": now,
                "updated_at": now,
            }
        return input_path

    def _finished_job(self, job_id: str, preset: str) -> str:
        input_path = self._seed_job(job_id, preset)
        with patch.object(main.subprocess, "Popen", FakeDemucsProcess):
            main.run_separation(job_id, input_path)
        return job_id

    def test_progress_comes_from_demucs_own_percent(self):
        # 没有百分比的行不给进度：加载模型期间就该老实停在低位，不编一个好看的数。
        self.assertIsNone(main.demucs_progress("Selected model is a bag of 1 models."))
        self.assertEqual(main.demucs_progress("  0%| | 0.0/5.85 [00:00<?, ?seconds/s]"), 10)
        self.assertEqual(main.demucs_progress(" 30%| ## | 1.8/5.85 [00:01<00:03]"), 10 + int(30 * 0.85))
        self.assertEqual(main.demucs_progress("100%| ### | 5.85/5.85 [00:02<00:00]"), 95)

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
        with TestClient(main.app, base_url=LOOPBACK_BASE) as client:
            self.assertEqual(client.get(f"/api/download/{job_id}/no_vocals").status_code, 200)
            # 这个预设根本没产出 drums：下载它必须是 404，而不是"文件不存在"含糊过关。
            self.assertEqual(client.get(f"/api/download/{job_id}/drums").status_code, 404)
            self.assertEqual(client.get(f"/api/download/{job_id}/../x").status_code, 404)

    def test_history_index_and_duplicate_lookup(self):
        job_id = self._finished_job("415708700001", "four_stems")
        with TestClient(main.app, base_url=LOOPBACK_BASE) as client:
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
        with TestClient(main.app, base_url=LOOPBACK_BASE) as client:
            stale = client.get("/api/history", params={"name": "song.wav", "bytes": 4}).json()
            self.assertEqual(stale["total"], 0)

    def test_unknown_preset_is_rejected(self):
        with TestClient(main.app, base_url=LOOPBACK_BASE) as client:
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

    # ────────────────── 排队取消 / 槽位 / 子进程环境 / 出路 ──────────────────

    def test_eta_is_not_promised_before_the_first_measured_step(self):
        """进度只到地板值时不外推剩余时间，否则界面那句"约剩 N 秒"会一路涨。"""
        job_id = "0e7a00000001"
        self._seed_job(job_id)
        with main.jobs_lock:
            main.jobs[job_id].update(
                {"status": "processing", "progress": 10, "running_at": time.time() - 30}
            )
        self.assertIsNone(main._public_job(job_id)["eta_seconds"])
        with main.jobs_lock:
            main.jobs[job_id]["progress"] = 40
        eta = main._public_job(job_id)["eta_seconds"]
        self.assertIsInstance(eta, float)
        self.assertTrue(30 < eta < 70, eta)

    def test_queue_position_counts_the_job_holding_the_gpu(self):
        """排队位次要把"正在占显卡的那一首"也算在前面，否则界面写"前面还有 0 首"。"""
        self._seed_job("0100aa11bb22")
        self._seed_job("0200aa11bb33")
        with main.jobs_lock:
            main.jobs["0100aa11bb22"]["status"] = "processing"
        self.assertEqual(main._public_job("0200aa11bb33")["queue_position"], 1)
        with main.jobs_lock:
            main.jobs["0100aa11bb22"]["status"] = "done"
        self.assertEqual(main._public_job("0200aa11bb33")["queue_position"], 0)

    def test_cancel_while_queued_never_starts_demucs(self):
        """取消一首还在排队的歌必须真的作废，不能等槽位空出来再白跑一遍。"""
        job_id = "ca5e00000001"
        input_path = self._seed_job(job_id)
        self.assertTrue(main.SEPARATION_SLOTS.acquire(timeout=2), "测试先占住唯一的显卡槽位")
        calls: list = []
        try:
            with patch.object(main.subprocess, "Popen", side_effect=lambda *a, **k: calls.append(a)):
                thread = threading.Thread(target=main.run_separation, args=(job_id, input_path), daemon=True)
                thread.start()
                time.sleep(0.4)
                self.assertEqual(main._public_job(job_id)["status"], "queued")
                main.cancel_job(job_id)
                thread.join(timeout=20)
        finally:
            main.SEPARATION_SLOTS.release()
        self.assertFalse(thread.is_alive(), "取消后线程要退出")
        self.assertEqual(calls, [], "取消的排队任务不该启动 Demucs")
        self.assertEqual(main._public_job(job_id)["status"], "cancelled")
        self.assertFalse(input_path.exists(), "上传副本要删掉")
        self.assertFalse((self.outputs / job_id).exists(), "半成品目录要清掉")

    def test_gpu_slot_is_released_even_when_cleanup_fails(self):
        """Windows 上删一个还被子进程占着的 WAV 会抛 OSError——槽位不能跟着一起丢。"""
        job_id = "510700000001"
        input_path = self._seed_job(job_id)

        def explode(_target: Path) -> None:
            raise OSError("模拟文件占用")

        with (
            patch.object(main.subprocess, "Popen", FakeDemucsProcess),
            patch.object(main, "_remove_quiet", side_effect=explode),
        ):
            try:
                main.run_separation(job_id, input_path)
            except OSError:
                pass  # 清理失败可以往上冒，但后面的任务不能被永久堵死
        self.assertTrue(main.SEPARATION_SLOTS.acquire(timeout=2), "下一个任务还拿得到槽位")
        main.SEPARATION_SLOTS.release()
        self.assertEqual(main._public_job(job_id)["status"], "done")

    def test_demucs_child_is_given_utf8_and_unbuffered_output(self):
        job_id = "b0b000000001"
        input_path = self._seed_job(job_id)
        with patch.object(main.subprocess, "Popen", RecordingDemucsProcess):
            main.run_separation(job_id, input_path)
        environment = RecordingDemucsProcess.captured["kwargs"].get("env") or {}
        # 默认按 GBK 写、块缓冲到退出才刷：原文会变乱码，阶段话也到最后一刻才露面。
        self.assertEqual(environment.get("PYTHONIOENCODING"), "utf-8")
        self.assertEqual(environment.get("PYTHONUNBUFFERED"), "1")

    def test_environment_issues_give_actionable_exits(self):
        base = {
            "python_executable": sys.executable,
            "python_version": "3.12.4",
            "demucs_installed": True,
            "torch_installed": True,
            "cuda_available": True,
            "nvidia_smi_present": True,
            "ffmpeg_available": True,
            "model_cache": {"cached": True, "searched_dirs": []},
            "outputs_writable": True,
            "disk_free_mb": 50000,
        }
        self.assertEqual(main.environment_issues(base), [], "环境齐整时不该编出问题")

        missing_demucs = main.environment_issues({**base, "demucs_installed": False})
        self.assertEqual([item["key"] for item in missing_demucs], ["demucs"])
        self.assertEqual(missing_demucs[0]["severity"], "blocking")
        self.assertIn("pip install", missing_demucs[0]["command"])
        self.assertIn(sys.executable, missing_demucs[0]["command"])

        missing_ffmpeg = main.environment_issues({**base, "ffmpeg_available": False})
        self.assertEqual([item["key"] for item in missing_ffmpeg], ["ffmpeg"])
        self.assertEqual(missing_ffmpeg[0]["severity"], "warning")  # 只做音频照样能干活
        self.assertIn("ffmpeg.exe", missing_ffmpeg[0]["detail"])

        no_weights = main.environment_issues({**base, "model_cache": {"cached": False, "searched_dirs": ["D:\\缓存"]}})
        self.assertEqual([item["key"] for item in no_weights], ["weights"])
        self.assertIn("demucs", no_weights[0]["command"])
        self.assertIn("联网", no_weights[0]["label"])

        cpu_only = main.environment_issues({**base, "cuda_available": False, "nvidia_smi_present": False})
        self.assertEqual([item["key"] for item in cpu_only], ["cuda"])
        self.assertEqual(cpu_only[0]["severity"], "warning")  # 没显卡不是故障

        broken_cuda = main.environment_issues({**base, "cuda_available": False, "nvidia_smi_present": True})
        self.assertEqual(broken_cuda[0]["key"], "cuda")
        self.assertIn("pip install", broken_cuda[0]["command"])

        unwritable = main.environment_issues({**base, "outputs_writable": False})
        self.assertEqual(unwritable[0]["severity"], "blocking")
        self.assertIn("VOCAL_SEPARATOR_DATA_DIR", unwritable[0]["command"])

        old_python = main.environment_issues({**base, "python_version": "3.8.10"})
        self.assertEqual(old_python[0]["key"], "python")

    def test_failure_remedy_keeps_raw_output_and_next_step(self):
        raw = "ModuleNotFoundError: No module named 'demucs'"
        message = main.failure_remedy(raw, 1)
        self.assertIn(raw, message)  # 原始 stderr 一个字不改
        self.assertIn("下一步", message)
        self.assertIn("pip install", message)
        # 认不出来的报错就原样交出去，不编一个原因糊弄过去。
        self.assertEqual(main.failure_remedy("完全没见过的报错", 7), "完全没见过的报错")

    # ────────────────────────── Agent API 契约 ──────────────────────────

    def test_health_carries_both_shapes(self):
        with TestClient(main.app, base_url=LOOPBACK_BASE) as client:
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
        with TestClient(main.app, base_url=LOOPBACK_BASE) as client:
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
        with TestClient(main.app, base_url=LOOPBACK_BASE) as client:
            response = self._tool(client, "vocal.env_probe", {"refresh": True})
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
        with TestClient(main.app, base_url=LOOPBACK_BASE) as client:
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

    def test_agent_env_probe_treats_probing_as_retry_not_failure(self):
        """冷启动首呼是"探测中"，不是错误：要给出可轮询的 retry_after_seconds。"""
        with TestClient(main.app, base_url=LOOPBACK_BASE) as client:
            with patch.object(main, "probe_environment", return_value={"status": "probing"}):
                body = self._tool(client, "vocal.env_probe", {}).json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["data"]["status"], "probing")
            self.assertIsInstance(body["data"]["retry_after_seconds"], int)

            with patch.object(
                main,
                "probe_environment",
                return_value={"status": "failed", "error": "RuntimeError: boom", "issues": []},
            ):
                failed = self._tool(client, "vocal.env_probe", {}).json()["data"]
            self.assertEqual(failed["status"], "failed")
            self.assertIn("boom", failed["error"])

    def test_agent_submit_refuses_when_environment_is_blocked(self):
        source = self.root / "take.flac"
        source.write_bytes(b"fAKE")
        blocked = {
            "status": "ready",
            "issues": [
                {
                    "key": "demucs",
                    "label": "这个 Python 里没有 Demucs",
                    "severity": "blocking",
                    "command": 'python -m pip install -r "backend/requirements.txt"',
                }
            ],
        }
        with TestClient(main.app, base_url=LOOPBACK_BASE) as client:
            with patch.object(main, "probe_environment", return_value=blocked):
                response = self._tool(
                    client, "vocal.separate_submit", {"input_path": str(source), "confirm": True}
                )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "environment_blocked")
        self.assertIn("没有 Demucs", response.json()["error"]["message"])
        self.assertIn("pip install", response.json()["error"]["message"])
        # 明知跑不了就不该起任务、也不该动 uploads。
        self.assertEqual(list(self.uploads.iterdir()), [])

    def test_exec_tool_requires_explicit_confirm(self):
        source = self.root / "karaoke.flac"
        source.write_bytes(b"fAKE")
        with TestClient(main.app, base_url=LOOPBACK_BASE) as client:
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
        with TestClient(main.app, base_url=LOOPBACK_BASE) as client:
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
