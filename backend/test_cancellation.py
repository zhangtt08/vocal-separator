"""卡住/静默的子进程也要能被收手（验收 MAJOR：取消与墙钟都不该依赖子进程说话）。

靶子是"一个字都不吐、也不退出"的 Demucs 子进程 —— 显卡驱动卡死时的真实形状。
以前的实现是 `for line in process.stdout:`：那一行永远阻塞，取消要等到 3600 秒的
TTL 清扫才可能生效。现在读行放在守护线程里，主循环每 CHILD_POLL_SECONDS 醒一次，
先看取消标记再看墙钟，任一成立就走 terminate -> wait -> kill。
"""

from __future__ import annotations

import subprocess
import threading
import time
import unittest
from unittest.mock import patch

import main
from test_main import ServiceSandbox


class BlockingStdout:
    """可迭代对象，一直阻塞到被放行（模拟子进程不吐字）。"""

    def __init__(self) -> None:
        self.released = threading.Event()

    def __iter__(self):
        self.released.wait(timeout=120)
        return iter([])


class HungChild:
    """不吐字也不退出的假子进程；记录对它做过什么，用来验证 terminate -> wait -> kill。"""

    def __init__(self, command=None, stubborn: bool = False, **_kwargs) -> None:
        self.command = list(command or ["demucs"])
        self.stubborn = stubborn
        self.stdout = BlockingStdout()
        self.actions: list[str] = []
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.actions.append("terminate")
        if not self.stubborn:
            self.returncode = -15
            self.stdout.released.set()

    def kill(self) -> None:
        self.actions.append("kill")
        self.returncode = -9
        self.stdout.released.set()

    def wait(self, timeout: float | None = None) -> int:
        self.actions.append(f"wait({timeout})")
        if self.returncode is None:
            raise subprocess.TimeoutExpired(cmd=self.command, timeout=timeout or 0)
        return self.returncode

    def close(self) -> None:
        return None


class SilentChildTests(ServiceSandbox, unittest.TestCase):
    """真起一条 run_separation 线程，面对一个永远不出声的子进程。"""

    def setUp(self):
        super().setUp()
        self.pop_patchers: list = []

    def tearDown(self):
        for patcher in self.pop_patchers:
            patcher.stop()
        self.pop_patchers.clear()
        super().tearDown()

    def start_job(self, child: HungChild, job_id: str, **job_changes) -> threading.Thread:
        input_path = self.uploads / f"{job_id}.wav"
        input_path.write_bytes(b"RIFF" + b"\0" * 64)
        with main.jobs_lock:
            main.jobs[job_id] = {
                "status": "queued",
                "progress": 1,
                "phase": "queued",
                "cancel_requested": False,
                "preset": "four_stems",
                "stem_names": list(main.STEM_NAMES),
                "source_name": "song.wav",
                "source_bytes": 68,
                "output_dir": str(self.outputs / job_id / "stems"),
                "created_at": time.time(),
                "updated_at": time.time(),
                **job_changes,
            }
        patcher = patch.object(main.subprocess, "Popen", return_value=child)
        patcher.start()  # 一直挂到用例结束：线程是后台起的，出了 with 就换不到假进程了
        self.pop_patchers.append(patcher)
        thread = threading.Thread(target=main.run_separation, args=(job_id, input_path), daemon=True)
        thread.start()
        return thread

    def wait_state(self, job_id: str, wanted: set[str], timeout: float = 10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with main.jobs_lock:
                if (main.jobs.get(job_id) or {}).get("status") in wanted:
                    return True
            time.sleep(0.05)
        return False

    def terminal(self, job_id: str, timeout: float = 15.0) -> dict | None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            snapshot = main.job_snapshot(job_id)
            if snapshot and snapshot["status"] not in {"queued", "processing", "cancelling"}:
                return snapshot
            time.sleep(0.05)
        return None

    def test_cancel_a_child_that_says_nothing(self):
        child = HungChild(stubborn=False)
        job_id = "aaaa00000001"
        thread = self.start_job(child, job_id)
        self.assertTrue(self.wait_state(job_id, {"processing"}), "任务没能进入 processing")
        time.sleep(1.0)  # 已经在"静默卡住"里待了一秒，一个字都没吐
        started = time.time()
        receipt = main.cancel_job(job_id)
        self.assertTrue(receipt["was_running"], "取消应当走到终止子进程那一步")
        snapshot = self.terminal(job_id, timeout=8)
        self.assertIsNotNone(snapshot, "取消静默卡住的任务在 8 秒内没有任何结果 —— 又回到 for line in stdout 了")
        self.assertLess(time.time() - started, 5.0, f"取消生效用了 {round(time.time() - started, 1)} 秒")
        self.assertEqual(snapshot["status"], "cancelled")
        self.assertIn("terminate", child.actions, "必须真的终止子进程，不是只置一个标记")
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_wall_clock_stops_a_silent_child_with_no_cancel_at_all(self):
        child = HungChild()
        job_id = "bbbb00000001"
        with patch.object(main, "JOB_WALL_CLOCK_SECONDS", 2):
            thread = self.start_job(child, job_id)
            self.assertTrue(self.wait_state(job_id, {"processing"}))
            snapshot = self.terminal(job_id)
        self.assertIsNotNone(snapshot, "墙钟没能收住静默卡住的子进程")
        self.assertEqual(snapshot["status"], "error")
        self.assertEqual(snapshot["progress"], 0)
        self.assertEqual(snapshot["stems"], {})
        self.assertIn(f"超过 {main.human_minutes(2)} 没跑完", snapshot["error"])
        self.assertIn("墙钟", snapshot["error"])
        self.assertIn("VOCAL_SEPARATOR_JOB_TIMEOUT", snapshot["error"], "出路要指名可调的那个开关")
        self.assertIn("terminate", child.actions)
        thread.join(timeout=5)

    def test_queue_time_does_not_eat_the_wall_clock_budget(self):
        """排了 1.5 秒不该让 3 秒预算只剩 1.5 秒 —— 墙钟从拿到显卡那一刻算起。"""
        job_id = "cccc00000001"
        child = HungChild()
        self.assertTrue(main.SEPARATION_SLOTS.acquire(timeout=1), "测试开始前槽位就该是空的")
        try:
            with patch.object(main, "JOB_WALL_CLOCK_SECONDS", 3):
                thread = self.start_job(child, job_id)
                time.sleep(1.5)  # 一直排在队里（槽位被本用例占着）
                main.SEPARATION_SLOTS.release()
                self.assertTrue(self.wait_state(job_id, {"processing"}), "放行后没接上槽位")
                time.sleep(2.2)
                mid = main.job_snapshot(job_id)
                self.assertEqual(
                    mid["status"],
                    "processing",
                    "开工 2.2 秒就被墙钟掐了：排队那 1.5 秒被算进了预算",
                )
                snapshot = self.terminal(job_id, timeout=8)
        finally:
            if (main.job_snapshot(job_id) or {}).get("status") in {"queued", "processing", "cancelling"}:
                main.SEPARATION_SLOTS.release()
            thread.join(timeout=5)
        self.assertEqual(snapshot["status"], "error")
        self.assertIn(f"超过 {main.human_minutes(3)} 没跑完", snapshot["error"])

    def test_terminate_then_wait_then_kill_sequence(self):
        child = HungChild(stubborn=True)  # 对 terminate 没反应：必须升级到 kill
        job_id = "dddd00000001"
        thread = self.start_job(child, job_id)
        self.assertTrue(self.wait_state(job_id, {"processing"}))
        main.cancel_job(job_id)
        snapshot = self.terminal(job_id, timeout=25)
        self.assertIsNotNone(snapshot, "kill 之后任务还停在原地")
        self.assertEqual(snapshot["status"], "cancelled")
        self.assertIn("terminate", child.actions)
        self.assertIn("kill", child.actions, "terminate 不响就该 kill，不能发一次信号就干等 TTL")
        self.assertGreater(child.actions.index("kill"), child.actions.index("terminate"), "顺序必须是 terminate 在前")
        thread.join(timeout=5)

    def test_gpu_slot_is_back_after_a_wall_clock_kill(self):
        job_id = "eeee00000001"
        child = HungChild()
        with patch.object(main, "JOB_WALL_CLOCK_SECONDS", 2):
            thread = self.start_job(child, job_id)
            self.assertIsNotNone(self.terminal(job_id))
        thread.join(timeout=5)
        self.assertTrue(
            main.SEPARATION_SLOTS.acquire(timeout=3),
            "墙钟收手后显卡槽位没还回来 —— 后面所有任务都会永远排队",
        )
        main.SEPARATION_SLOTS.release()

    def test_cancel_receipt_covers_the_silent_case(self):
        child = HungChild()
        job_id = "ffff00000001"
        thread = self.start_job(child, job_id)
        self.assertTrue(self.wait_state(job_id, {"processing"}))
        receipt = main.cancel_job(job_id)
        self.assertIn("一句输出都没有", receipt["note"])
        self.assertIn(str(main.CHILD_POLL_SECONDS), receipt["note"])
        self.assertIsNotNone(self.terminal(job_id))
        thread.join(timeout=5)

    def test_abandoned_queue_job_explains_the_next_step(self):
        job_id = "1a1a1a1a1a1a"
        input_path = self.uploads / f"{job_id}.wav"
        input_path.write_bytes(b"RIFF")
        with main.jobs_lock:
            main.jobs[job_id] = {
                "status": "queued",
                "progress": 1,
                "phase": "queued",
                "cancel_requested": False,
                "preset": "four_stems",
                "stem_names": list(main.STEM_NAMES),
                "created_at": time.time(),
                "updated_at": time.time(),
            }
        self.assertTrue(main.SEPARATION_SLOTS.acquire(timeout=1))
        try:
            with patch.object(main, "QUEUE_WAIT_TIMEOUT_SECONDS", 1):
                main.run_separation(job_id, input_path)  # 就地跑：1 秒后放弃排队
        finally:
            main.SEPARATION_SLOTS.release()
        snapshot = main.job_snapshot(job_id)
        self.assertEqual(snapshot["status"], "error")
        self.assertIn(f"排队等待超过 {main.human_minutes(1)}", snapshot["error"])
        self.assertIn("取消", snapshot["error"], "放弃排队也要给出下一步，不能只说等超时了")


class WaitForChildTests(unittest.TestCase):
    def test_a_child_that_never_exits_is_terminated_then_killed(self):
        child = HungChild(stubborn=True)
        started = time.time()
        with self.assertRaises(main.SeparationTimeout):
            main.wait_for_child(child, 0.3, stage_label="分离音轨")
        self.assertLess(time.time() - started, 20)
        self.assertIn("terminate", child.actions)
        self.assertIn("kill", child.actions)

    def test_return_code_passes_through(self):
        class Fine:
            stdout = None

            def wait(self, timeout=None):
                return 0

            def poll(self):
                return 0

        self.assertEqual(main.wait_for_child(Fine(), 5), 0)

    def test_fakes_without_a_timeout_parameter_still_work(self):
        class Legacy:
            stdout = None

            def wait(self):  # 老假进程：wait() 不收 timeout
                return 3

        self.assertEqual(main.wait_for_child(Legacy(), 5), 3)


class TimeoutConstantsTests(unittest.TestCase):
    def test_named_timeouts_are_the_single_source(self):
        # 排队与墙钟各有名字，不再一处写 3600、一处借 TTL。
        self.assertGreater(main.QUEUE_WAIT_TIMEOUT_SECONDS, 0)
        self.assertGreater(main.JOB_WALL_CLOCK_SECONDS, 0)
        self.assertLessEqual(main.CHILD_POLL_SECONDS, 1.0)
        with patch.object(main, "JOB_WALL_CLOCK_SECONDS", 7):
            self.assertAlmostEqual(main.child_deadline(), time.time() + 7, delta=1)

    def test_human_minutes_reads_well_at_both_scales(self):
        self.assertEqual(main.human_minutes(1800), "30 分钟")
        self.assertEqual(main.human_minutes(2), "2 秒")
        self.assertEqual(main.human_minutes(90), "1.5 分钟")


if __name__ == "__main__":
    unittest.main()
