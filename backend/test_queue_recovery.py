"""任务表落盘与开机恢复的测试（验收 MAJOR：重启不再丢掉排队中/进行中的活）。

一条主线：状态**绝不说谎**。曾经 queued/processing 的任务在恢复之后只能是
「重新排队」或「如实标成被打断」，不能是 done，也不能悄悄进 history.json。
半成品输出与孤儿输入由 sweep_partial_outputs() 开机收走，并留下"收了什么"的报告。
"""

from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from test_main import LOOPBACK_BASE, ServiceSandbox


def write_queue_file(root: Path, records: list[dict]) -> Path:
    target = root / main.QUEUE_FILE_NAME
    target.write_text(
        json.dumps({"version": 1, "saved_at": 1.0, "jobs": records}, ensure_ascii=False),
        encoding="utf-8",
    )
    return target


def record(job_id: str, **changes) -> dict:
    """一条 queue.json 记录；时间戳用"刚刚"，否则开机 TTL 清扫会先把它当过期丢掉。"""
    now = time.time()
    base = {
        "job_id": job_id,
        "status": "queued",
        "phase": "queued",
        "progress": 1,
        "preset": "four_stems",
        "stem_names": list(main.STEM_NAMES),
        "source_name": "song.wav",
        "source_bytes": 1024,
        "output_dir": str(main.OUTPUTS_DIR / job_id / "stems"),
        "input_path": str(main.UPLOADS_DIR / f"{job_id}.wav"),
        "stems": {},
        "error": None,
        "cancel_requested": False,
        "created_at": now,
        "updated_at": now,
    }
    base.update(changes)
    return base


class QueuePersistenceTests(ServiceSandbox, unittest.TestCase):
    def test_creating_a_job_writes_the_queue_file(self):
        self.assertFalse((self.root / main.QUEUE_FILE_NAME).exists())
        job_id = main.create_job(source_name="song.wav", source_bytes=2048, preset="vocal_backing")
        target = self.root / main.QUEUE_FILE_NAME
        self.assertTrue(target.is_file(), "排上队的那一刻就该落盘，不是等状态变化")
        records = main.load_queue_records()
        self.assertEqual([item["job_id"] for item in records], [job_id])
        stored = records[0]
        self.assertEqual(stored["preset"], "vocal_backing")
        self.assertEqual(stored["stem_names"], list(main.BACKING_STEM_NAMES))
        # 子进程句柄这类不可序列化的东西根本不该出现在记录里。
        self.assertNotIn("process", stored)
        self.assertNotIn("process", json.dumps(records))

    def test_status_and_phase_changes_are_persisted_immediately(self):
        job_id = main.create_job(source_name="song.wav", source_bytes=2048)
        main._update_job(job_id, status="processing", phase="separating", progress=42)
        stored = {item["job_id"]: item for item in main.load_queue_records()}[job_id]
        self.assertEqual(stored["status"], "processing")
        self.assertEqual(stored["phase"], "separating")
        self.assertEqual(stored["progress"], 42)

    def test_corrupt_queue_file_is_ignored_not_guessed(self):
        target = self.root / main.QUEUE_FILE_NAME
        target.write_text("{ 这不是 JSON", encoding="utf-8")
        self.assertEqual(main.load_queue_records(), [])
        report = main.recover_jobs_on_startup()
        self.assertEqual(report["loaded"], 0)
        self.assertEqual(main.jobs, {})

    def test_interrupted_running_job_is_never_reported_as_succeeded(self):
        job_id = "aa11bb22cc33"
        write_queue_file(
            self.root,
            [record(job_id, status="processing", phase="separating", progress=70, running_at=900.0)],
        )
        (self.root / "history.json").write_text("[]", encoding="utf-8")
        with main.jobs_lock:
            main.jobs.clear()
        report = main.recover_jobs_on_startup()

        self.assertEqual([item["job_id"] for item in report["interrupted"]], [job_id])
        snapshot = main.job_snapshot(job_id)
        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["phase"], main.INTERRUPTED_PHASE)
        self.assertEqual(snapshot["phase_label"], "服务重启，未跑完")
        self.assertEqual(snapshot["progress"], 0)
        self.assertEqual(snapshot["stems"], {})
        self.assertIn("重启", snapshot["error"])
        self.assertNotEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["progress"], 0, "被打断的任务不许留着一个像做完了的进度")
        # 被打断的活不进历史索引：history.json 只记真的跑完过的东西。
        self.assertEqual(main.load_history(), [])
        # 恢复结果自己也要落盘，否则下一次重启又会看到同一条 processing。
        stored = {item["job_id"]: item for item in main.load_queue_records()}[job_id]
        self.assertEqual(stored["status"], "error")
        self.assertEqual(stored["phase"], main.INTERRUPTED_PHASE)

    def test_queued_job_with_surviving_input_is_requeued(self):
        job_id = "cc22dd33ee44"
        (self.uploads / f"{job_id}.wav").write_bytes(b"RIFF" + b"\0" * 128)
        write_queue_file(self.root, [record(job_id)])
        with main.jobs_lock:
            main.jobs.clear()

        launched: list[tuple[str, str]] = []
        # launch_separation 起的是守护线程，所以这里等的是"线程确实把活接走了"。
        with patch.object(main, "run_separation", side_effect=lambda job, path: launched.append((job, str(path)))):
            report = main.recover_jobs_on_startup()
        self.assertEqual(report["requeued"], [job_id])
        self.assertEqual(report["interrupted"], [])
        deadline = time.time() + 5
        while not launched and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(launched, [(job_id, str(self.uploads / f"{job_id}.wav"))])

    def test_queued_job_without_input_is_marked_interrupted_with_the_reason(self):
        job_id = "ee33ff44aa55"
        write_queue_file(self.root, [record(job_id, status="queued", input_path=str(self.uploads / f"{job_id}.wav"))])
        with main.jobs_lock:
            main.jobs.clear()
        report = main.recover_jobs_on_startup()
        self.assertEqual(report["requeued"], [])
        self.assertEqual([item["job_id"] for item in report["interrupted"]], [job_id])
        snapshot = main.job_snapshot(job_id)
        self.assertEqual(snapshot["phase"], main.INTERRUPTED_PHASE)
        self.assertIn("输入文件已经不在了", snapshot["error"])

    def test_cancelled_before_restart_stays_cancelled(self):
        job_id = "bb44cc55dd66"
        write_queue_file(self.root, [record(job_id, status="cancelled", phase="cancelled")])
        with main.jobs_lock:
            main.jobs.clear()
        report = main.recover_jobs_on_startup()
        self.assertEqual(report["interrupted"], [])
        self.assertEqual(report["restored_terminal"], 1)
        self.assertEqual(main.job_snapshot(job_id)["status"], "cancelled")

    def test_invalid_job_ids_in_the_queue_file_are_skipped(self):
        write_queue_file(self.root, [record("../../etc/passwd"), record("短号")])
        report = main.recover_jobs_on_startup()
        self.assertEqual(report["requeued"], [])
        self.assertEqual(report["interrupted"], [])
        self.assertEqual(len(report["skipped"]), 2)
        self.assertEqual(main.jobs, {})


class PartialOutputSweepTests(ServiceSandbox, unittest.TestCase):
    def plant(self, directory: Path, size: int = 2048) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        payload = directory / main.MODEL_NAME / "song" / "vocals.wav"
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(b"\0" * size)
        return directory

    def test_orphan_partial_output_and_orphan_upload_are_reclaimed_and_reported(self):
        orphan = self.plant(self.outputs / "ab12cd34ef56", size=4096)
        stray_upload = self.uploads / "ab12cd34ef56.wav"
        stray_upload.write_bytes(b"\0" * 100)

        report = main.sweep_partial_outputs()
        self.assertFalse(orphan.exists(), "没有主人的半成品目录必须开机就收走")
        self.assertFalse(stray_upload.exists())
        self.assertEqual(report["dirs"], 1)
        self.assertEqual(report["uploads"], 1)
        self.assertEqual(report["bytes"], 4096 + 100)
        item = report["items"][0]
        self.assertEqual(item["job_id"], "ab12cd34ef56")
        self.assertIn("任务表里没有", item["reason"])

    def test_finished_and_live_jobs_are_left_alone(self):
        done_id = "111111111111"
        running_id = "222222222222"
        queued_id = "333333333333"
        done_dir = self.plant(self.outputs / done_id)
        live_dir = self.plant(self.outputs / running_id)
        queued_dir = self.plant(self.outputs / queued_id)
        queued_upload = self.uploads / f"{queued_id}.wav"
        queued_upload.write_bytes(b"\0" * 10)
        with main.jobs_lock:
            main.jobs.update(
                {
                    done_id: {"status": "done", "stems": {"vocals": {"path": str(done_dir / "x.wav")}}, "updated_at": 1.0},
                    running_id: {"status": "processing", "stems": {}, "updated_at": 1.0},
                    queued_id: {"status": "queued", "stems": {}, "updated_at": 1.0},
                }
            )
        report = main.sweep_partial_outputs()
        self.assertEqual(report["dirs"], 0, report["items"])
        self.assertTrue(done_dir.is_dir() and live_dir.is_dir() and queued_dir.is_dir())
        self.assertTrue(queued_upload.is_file(), "还在排队的任务的输入文件不能被开机清扫带走")

    def test_failed_job_output_is_reclaimed(self):
        failed_id = "deadbeef0011"

        directory = self.plant(self.outputs / failed_id, size=1234)
        with main.jobs_lock:
            main.jobs[failed_id] = {"status": "error", "phase": "failed", "stems": {}, "updated_at": 1.0}
        report = main.sweep_partial_outputs()
        self.assertFalse(directory.exists())
        self.assertEqual(report["bytes"], 1234)
        self.assertIn("没有成功留下音轨", report["items"][0]["reason"])


class RecoveryEndToEndTests(ServiceSandbox, unittest.TestCase):
    """开机恢复跑在 lifespan 里，所以这条按"真起一次服务"的形状测。"""

    def test_startup_report_is_visible_over_http(self):
        job_id = "ff00aa11bb22"
        write_queue_file(
            self.root,
            [record(job_id, status="processing", phase="separating", progress=55)],
        )
        self.plant_dir = self.outputs / "0f0f0f0f0f0f"
        (self.plant_dir / main.MODEL_NAME).mkdir(parents=True, exist_ok=True)
        (self.plant_dir / main.MODEL_NAME / "junk.wav").write_bytes(b"\0" * 512)
        with main.jobs_lock:
            main.jobs.clear()

        with patch.object(main, "run_separation"):
            with TestClient(main.app, base_url=LOOPBACK_BASE) as client:
                recovery = client.get("/api/health").json()["data"]["recovery"]
        self.assertEqual(recovery["status"], "ok")
        self.assertEqual(recovery["loaded"], 1)
        self.assertEqual([item["job_id"] for item in recovery["interrupted"]], [job_id])
        self.assertEqual(recovery["swept"]["dirs"], 1)
        self.assertEqual(recovery["swept"]["bytes"], 512)
        self.assertFalse(self.plant_dir.exists())
        snapshot = main.job_snapshot(job_id)
        self.assertEqual(snapshot["phase"], main.INTERRUPTED_PHASE)

    def test_second_restart_does_not_requeue_the_same_job_forever(self):
        # 重投后要先把恢复结果落盘，否则每次开机都从同一条记录重新跑一遍。
        job_id = "abcdef012345"
        (self.uploads / f"{job_id}.wav").write_bytes(b"RIFF")
        write_queue_file(self.root, [record(job_id)])
        started: list[str] = []
        with patch.object(main, "run_separation", side_effect=lambda job, _path: started.append(job)):
            first = main.recover_jobs_on_startup()
        self.assertEqual(first["requeued"], [job_id])
        # 第一次恢复把状态推进到 processing（模拟真跑起来）；第二次重启它就成了"跑一半被打断"。
        main._update_job(job_id, status="processing", phase="separating", progress=30)
        with main.jobs_lock:
            main.jobs.clear()
        second = main.recover_jobs_on_startup()
        self.assertEqual(second["requeued"], [])
        self.assertEqual([item["job_id"] for item in second["interrupted"]], [job_id])
        self.assertEqual(len(started), 1, "同一条任务不能被两次开机各跑一遍")


if __name__ == "__main__":
    unittest.main()
