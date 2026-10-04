"""本地 AI 音轨分离服务。

这个进程是本项目的**唯一常驻服务**：它既服务桌面界面（Electron 壳与浏览器），
也对外提供个人 Agent 服务的契约端点（GET /api/health、GET /api/agent/tools、
GET /api/agent/manifest、POST /api/agent/tool）。契约端点由 `backend/agent_api.py`
挂载，工具声明与实现放在 `agent/tools.py`，只监听 127.0.0.1，不新增端口。

界面与 Agent 走的是同一批能力函数：`probe_environment()`、`create_job()`、
`run_separation()`、`cancel_job()`、`history_entries()`。
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

import local_guard

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("VOCAL_SEPARATOR_DATA_DIR", BASE_DIR)).resolve()
UPLOADS_DIR = DATA_DIR / "uploads"
OUTPUTS_DIR = DATA_DIR / "outputs"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

MODEL_NAME = "htdemucs"
MAX_FILE_SIZE = 500 * 1024 * 1024
FILE_TTL_SECONDS = 3600
UPLOAD_CHUNK_SIZE = 1024 * 1024
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
ACCEPTED_EXTENSIONS = ALLOWED_EXTENSIONS | VIDEO_EXTENSIONS
STEM_NAMES = ("vocals", "drums", "bass", "other")
BACKING_STEM_NAMES = ("vocals", "no_vocals")
PROGRESS_PATTERN = re.compile(r"(\d{1,3})%")
STEM_SAFE_PATTERN = re.compile(r"^[a-z][a-z_]{0,19}$")

# 音轨预设：一个预设 = 一组 Demucs 参数 + 一组输出轨名。界面与 Agent 共用这张表。
PRESETS: dict[str, dict[str, Any]] = {
    "four_stems": {
        "label": "四条音轨",
        "description": "人声 / 鼓组 / 贝斯 / 其他乐器，适合编曲与采样",
        "stems": STEM_NAMES,
        "two_stems": None,
    },
    "vocal_backing": {
        "label": "人声 + 伴奏",
        "description": "两轨：清唱人声与去人声伴奏，适合翻唱与卡拉 OK",
        "stems": BACKING_STEM_NAMES,
        "two_stems": "vocals",
    },
}
DEFAULT_PRESET = "four_stems"

STEM_LABELS = {
    "vocals": {"zh": "人声", "en": "vocals"},
    "drums": {"zh": "鼓组", "en": "drums"},
    "bass": {"zh": "贝斯", "en": "bass"},
    "other": {"zh": "其他乐器", "en": "other"},
    "no_vocals": {"zh": "伴奏", "en": "instrumental"},
}

# 任务状态里"被打断"专用一个阶段名，绝不复用 finished/failed 的措辞。
INTERRUPTED_PHASE = "interrupted"

PHASE_LABELS = {
    "queued": "排队等待",
    "loading_model": "加载模型",
    "downloading_model": "下载模型权重",
    "extracting_audio": "从视频提取音频",
    "separating": "分离音轨",
    "writing": "写出音轨文件",
    "finished": "已完成",
    "cancelled": "已取消",
    "failed": "失败",
    # 服务重启把任务打断时专用：既不是"完成"也不是普通的"失败"，说清是没跑完。
    INTERRUPTED_PHASE: "服务重启，未跑完",
}

# Demucs 一次跑满一张显卡才划算，所以分离槽位=1：后来的任务留在 queued 并给出排队位次。
SEPARATION_SLOTS = threading.Semaphore(1)
# 排队时每隔 1 秒回看一次取消标记，否则"取消排在后面的那首"要等前一首跑完才生效。
SLOT_POLL_SECONDS = 1.0
# 排队最长等多久（放弃了要说得清原因，而不是干等）。
QUEUE_WAIT_TIMEOUT_SECONDS = int(os.getenv("VOCAL_SEPARATOR_QUEUE_TIMEOUT", "3600"))
# 一个任务从"拿到显卡"开始的墙钟上限。判据是墙钟而不是输出行数：
# Demucs 卡在驱动/磁盘上时可以一个字都不吐，那时候唯一还说得过去的动作就是按时间收手。
JOB_WALL_CLOCK_SECONDS = int(os.getenv("VOCAL_SEPARATOR_JOB_TIMEOUT", "1800"))
# 子进程没说话时也要醒来看一眼取消与墙钟（秒）。以前是 for line in stdout：
# 它一阻塞，取消就只能等到 3600 秒的 TTL 清扫。
CHILD_POLL_SECONDS = float(os.getenv("VOCAL_SEPARATOR_CHILD_POLL", "0.5"))

# 失败时留给用户看的原始输出行数（Demucs/ffmpeg 的真实 stderr，不改写、不美化）。
ERROR_OUTPUT_TAIL = 12

HISTORY_FILE_NAME = "history.json"
HISTORY_LIMIT = 60
# 任务表落盘：崩了/重启了不该把排队中和在跑的任务一起丢掉（验收的 MAJOR 之一）。
QUEUE_FILE_NAME = "queue.json"
QUEUE_KEEP_JOBS = 120

jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()
history_lock = threading.Lock()
queue_lock = threading.Lock()
cleanup_stop = threading.Event()
# 开机恢复与清扫的报告（/api/health 的 data.recovery 就是它，界面上看得见"回收了什么"）。
STARTUP_REPORT: dict[str, Any] = {"status": "not-run"}
startup_report_lock = threading.Lock()

_START_TIME = time.time()


def _read_app_version() -> str:
    for candidate in (BASE_DIR.parent / "package.json", BASE_DIR / "package.json"):
        try:
            return str(json.loads(candidate.read_text(encoding="utf-8")).get("version") or "0.0.0")
        except (OSError, ValueError):
            continue
    return "0.0.0"


APP_VERSION = _read_app_version()


class SeparationCancelled(Exception):
    """用户主动取消音轨分离。"""


def find_ffmpeg() -> str:
    """按环境变量、程序目录、PATH 和 imageio-ffmpeg 的顺序查找 ffmpeg。"""
    candidates = [
        os.getenv("VOCAL_SEPARATOR_FFMPEG", "").strip('"'),
        str(BASE_DIR / "ffmpeg.exe"),
        str(BASE_DIR / "ffmpeg"),
    ]
    path_found = shutil.which("ffmpeg")
    if path_found:
        candidates.append(path_found)
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return ""


def _run_quiet(command: list[str], timeout: int = 15) -> str:
    try:
        completed = subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    first_line = (completed.stdout or completed.stderr or "").splitlines()
    return first_line[0].strip() if first_line else ""


# ────────────────────────────── 本机环境探测 ──────────────────────────────

ENV_PROBE: dict[str, Any] = {"status": "probing"}
env_probe_lock = threading.Lock()
_probe_state: dict[str, Any] = {"started_at": None, "inflight": False}
# 超过这个时间还停在 probing/failed 就自动补一次探测，不让界面卡在"正在检测"。
PROBE_STALE_SECONDS = 30.0
_demucs_model_sigs_cache: dict[str, list[str]] = {}


def _demucs_model_sigs(model_name: str) -> list[str]:
    """从 demucs/remote/<model>.yaml 里读出模型签名，用来在缓存目录里找权重文件。"""
    if model_name in _demucs_model_sigs_cache:
        return _demucs_model_sigs_cache[model_name]
    sigs: list[str] = []
    spec = importlib.util.find_spec("demucs")
    if spec and spec.submodule_search_locations:
        yaml_path = Path(list(spec.submodule_search_locations)[0]) / "remote" / f"{model_name}.yaml"
        try:
            sigs = re.findall(r"['\"]([0-9a-f]{8})['\"]", yaml_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            sigs = []
    _demucs_model_sigs_cache[model_name] = sigs
    return sigs


def _model_cache_info(model_name: str) -> dict[str, Any]:
    """模型权重是否已经在本机缓存（缺失时首次分离会先下载）。"""
    sigs = _demucs_model_sigs(model_name)
    search_dirs: list[Path] = []
    try:
        import torch

        search_dirs.append(Path(torch.hub.get_dir()) / "checkpoints")
    except Exception:  # torch 不可用时只查默认路径
        pass
    hf_root = Path(os.getenv("HF_HOME", str(Path.home() / ".cache" / "huggingface")))
    search_dirs.append(hf_root / "hub")

    matched: list[dict[str, Any]] = []
    scanned: list[str] = []
    for directory in search_dirs:
        scanned.append(str(directory))
        if not directory.is_dir():
            continue
        for file_path in directory.rglob("*"):
            if not file_path.is_file():
                continue
            suffix = file_path.suffix.lower()
            if suffix not in {".th", ".safetensors"}:
                continue
            name = file_path.name
            is_match = bool(sigs) and any(name.startswith(f"{sig}-") or name.startswith(sig) for sig in sigs)
            if not is_match:
                is_match = model_name.replace("_", "-").lower() in str(file_path.parent).lower()
            if is_match:
                matched.append({"path": str(file_path), "size_mb": round(file_path.stat().st_size / 1_048_576, 1)})

    total_mb = round(sum(item["size_mb"] for item in matched), 1)
    return {
        "model": model_name,
        "cached": bool(matched),
        "signatures": sigs,
        "size_mb": total_mb,
        "files": matched[:8],
        "searched_dirs": scanned,
        "note": None if matched else f"本机没有 {model_name} 权重，首次分离会先下载（约 80 MB 起，取决于模型）。",
    }


def _compute_environment() -> dict[str, Any]:
    """真实探测本机 AI 环境：Python、torch/CUDA、Demucs、ffmpeg、模型缓存、目录。"""
    environment: dict[str, Any] = {
        "status": "ready",
        "probed_at": time.time(),
        "python_executable": sys.executable,
        "python_version": ".".join(str(part) for part in sys.version_info[:3]),
        "cpu_count": os.cpu_count(),
        "platform": sys.platform,
    }

    for package in ("fastapi", "uvicorn", "python-multipart"):
        try:
            environment[f"{package}_version"] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            environment[f"{package}_version"] = None

    environment["demucs_installed"] = importlib.util.find_spec("demucs") is not None
    try:
        environment["demucs_version"] = importlib.metadata.version("demucs")
    except importlib.metadata.PackageNotFoundError:
        environment["demucs_version"] = None

    torch_available = importlib.util.find_spec("torch") is not None
    environment["torch_installed"] = torch_available
    environment["torch_version"] = None
    environment["cuda_available"] = False
    environment["cuda_device_name"] = None
    environment["cuda_device_count"] = 0
    if torch_available:
        try:
            import torch

            environment["torch_version"] = str(torch.__version__)
            cuda_ready = bool(torch.cuda.is_available())
            environment["cuda_available"] = cuda_ready
            if cuda_ready:
                environment["cuda_device_count"] = int(torch.cuda.device_count())
                environment["cuda_device_name"] = str(torch.cuda.get_device_name(0))
            else:
                # nvidia-smi 存在不等于 torch 能用 CUDA：CPU 版 torch 是最常见的原因。
                environment["cuda_note"] = "torch.cuda.is_available() 为 False，将使用 CPU 分离（明显更慢）。"
        except Exception as exc:  # torch 装了但起不来，原因要说出来
            environment["cuda_error"] = str(exc)
    else:
        environment["cuda_error"] = "未安装 torch"

    environment["nvidia_smi_present"] = shutil.which("nvidia-smi") is not None

    ffmpeg = find_ffmpeg()
    environment["ffmpeg_path"] = ffmpeg or None
    environment["ffmpeg_available"] = bool(ffmpeg)
    environment["ffmpeg_version"] = _run_quiet([ffmpeg, "-version"]) if ffmpeg else None

    environment["model"] = MODEL_NAME
    environment["model_cache"] = _model_cache_info(MODEL_NAME)
    environment["data_dir"] = str(DATA_DIR)
    environment["uploads_dir"] = str(UPLOADS_DIR)
    environment["outputs_dir"] = str(OUTPUTS_DIR)
    environment["requirements_file"] = str(BASE_DIR / "requirements.txt")
    environment["outputs_writable"] = _is_writable(OUTPUTS_DIR)
    try:
        environment["disk_free_mb"] = round(shutil.disk_usage(str(OUTPUTS_DIR)).free / 1_048_576, 1)
    except OSError:
        environment["disk_free_mb"] = None
    environment["file_ttl_seconds"] = FILE_TTL_SECONDS
    environment["max_file_size_bytes"] = MAX_FILE_SIZE
    environment["issues"] = environment_issues(environment)
    environment["can_separate"] = not any(item["severity"] == "blocking" for item in environment["issues"])
    return environment


def _is_writable(directory: Path) -> bool:
    """真的试写一个文件——只看权限位在这个场景里不够。"""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".write-probe-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def environment_issues(environment: dict[str, Any]) -> list[dict[str, Any]]:
    """把探测到的事实翻译成能执行的下一步。

    五种最卡人的情况（没 python / 没 demucs / 没 ffmpeg / CUDA 用不上 / 权重没缓存）
    都必须带一条真命令和一个真实路径，`blocking` 表示现在这份活干不了。
    """
    issues: list[dict[str, Any]] = []
    interpreter = environment.get("python_executable") or sys.executable
    requirements = environment.get("requirements_file") or str(BASE_DIR / "requirements.txt")
    install = f'"{interpreter}" -m pip install -r "{requirements}"'

    version_parts = str(environment.get("python_version") or "").split(".")
    try:
        python_minor = int(version_parts[1])
    except (IndexError, ValueError):
        python_minor = None
    if python_minor is not None and python_minor < 10:
        issues.append(
            {
                "key": "python",
                "label": f"Python {environment.get('python_version')} 太旧，Demucs 4 需要 3.10 以上",
                "severity": "blocking",
                "detail": f"当前解释器：{interpreter}",
                "command": "安装 Python 3.10–3.12，再对它重装依赖：" + install,
            }
        )

    if not environment.get("demucs_installed"):
        issues.append(
            {
                "key": "demucs",
                "label": "这个 Python 里没有 Demucs，界面只能检测不能分离",
                "severity": "blocking",
                "detail": f"解释器：{interpreter}；依赖清单：{requirements}",
                "command": install,
            }
        )
    elif not environment.get("torch_installed"):
        issues.append(
            {
                "key": "torch",
                "label": "装了 Demucs 但缺 PyTorch，模型跑不起来",
                "severity": "blocking",
                "detail": f"解释器：{interpreter}",
                "command": install,
            }
        )

    if not environment.get("cuda_available"):
        if environment.get("nvidia_smi_present"):
            issues.append(
                {
                    "key": "cuda",
                    "label": "本机有显卡但 torch 用不上 CUDA，会用 CPU 分离（慢 10–30 倍）",
                    "severity": "warning",
                    "detail": environment.get("cuda_note") or environment.get("cuda_error")
                    or "torch.cuda.is_available() 为 False，而 nvidia-smi 能列出显卡：多半装成了 CPU 版 torch 或驱动不匹配。",
                    "command": install + "  （清单里锁的是 cu124 轮子；装完重启后端，并更新显卡驱动）",
                }
            )
        else:
            issues.append(
                {
                    "key": "cuda",
                    "label": "没有可用显卡，按 CPU 分离",
                    "severity": "warning",
                    "detail": "一首 4 分钟的歌在 CPU 上要几分钟到十几分钟；队列仍按一次一首处理。",
                    "command": None,
                }
            )

    if not environment.get("ffmpeg_available"):
        bundled_ffmpeg = str(BASE_DIR / "ffmpeg.exe")
        issues.append(
            {
                "key": "ffmpeg",
                "label": "没找到 ffmpeg，视频文件读不了（音频不受影响）",
                "severity": "warning",
                "detail": f"后端按顺序找：环境变量 VOCAL_SEPARATOR_FFMPEG、{bundled_ffmpeg}、PATH。",
                "command": f'winget install --id Gyan.FFmpeg -E   或把 ffmpeg.exe 放进 "{BASE_DIR}"',
            }
        )

    model_cache = environment.get("model_cache") or {}
    if not model_cache.get("cached"):
        searched = "、".join(str(item) for item in model_cache.get("searched_dirs") or [])
        issues.append(
            {
                "key": "weights",
                "label": f"本机没有 {MODEL_NAME} 权重，第一次分离要先联网下载（约 80 MB）",
                "severity": "warning",
                "detail": f"找过这些目录：{searched or '无'}。完全离线的机器请先在有网时跑通一次，再把缓存目录整体复制过去。",
                "command": f'"{interpreter}" -m demucs --name {MODEL_NAME} -o "<一个空目录>" "<任意音频>"',
            }
        )

    if environment.get("outputs_writable") is False:
        issues.append(
            {
                "key": "disk_write",
                "label": "输出目录写不进去，分离结果存不下来",
                "severity": "blocking",
                "detail": f"目录：{environment.get('outputs_dir')}",
                "command": f'用 VOCAL_SEPARATOR_DATA_DIR 指到一个可写目录，例如 set "VOCAL_SEPARATOR_DATA_DIR=D:\\声析数据"',
            }
        )
    free_mb = environment.get("disk_free_mb")
    if isinstance(free_mb, (int, float)) and free_mb < 1024:
        issues.append(
            {
                "key": "disk_space",
                "label": f"磁盘只剩 {free_mb} MB，四条 WAV 轨可能写不完",
                "severity": "blocking" if free_mb < 200 else "warning",
                "detail": f"输出目录：{environment.get('outputs_dir')}",
                "command": "清理磁盘，或把 VOCAL_SEPARATOR_DATA_DIR 指到空间充足的盘",
            }
        )

    return issues


def probe_environment(force: bool = False) -> dict[str, Any]:
    """返回缓存的环境探测结果；首次调用返回 probing 而不是卡住事件循环。

    "探测中"不是失败：调用方按 `retry_after_seconds` 再问一次就能拿到实测值。
    探测卡住或上次炸了（超过 30 秒还是 probing/failed）会自动补一次后台探测，
    不至于让界面永远停在"正在检测"。
    """
    with env_probe_lock:
        cached = dict(ENV_PROBE)
        started_at = _probe_state["started_at"]
        inflight = _probe_state["inflight"]
    if cached.get("status") == "ready" or force:
        if not force:
            return cached
        result = _compute_environment()
        with env_probe_lock:
            ENV_PROBE.clear()
            ENV_PROBE.update(result)
        return dict(result)
    stale = started_at is not None and (time.time() - started_at) > PROBE_STALE_SECONDS
    if cached.get("status") in {"probing", "failed"} and (stale or started_at is None) and not inflight:
        _start_environment_probe()
    return cached


def _start_environment_probe() -> None:
    with env_probe_lock:
        if _probe_state["inflight"]:
            return
        _probe_state["inflight"] = True
        _probe_state["started_at"] = time.time()
    threading.Thread(target=_probe_environment_in_background, name="vocal-env-probe", daemon=True).start()


def _probe_environment_in_background() -> None:
    requirements = str(BASE_DIR / "requirements.txt")
    try:
        result = _compute_environment()
    except Exception as exc:  # 探测失败也要留痕，界面上好解释
        result = {
            "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "probed_at": time.time(),
            "issues": [
                {
                    "key": "probe",
                    "label": "环境探测本身失败了",
                    "severity": "blocking",
                    "detail": f"{type(exc).__name__}: {exc}",
                    "command": f'"{sys.executable}" -m pip install -r "{requirements}"',
                }
            ],
        }
    with env_probe_lock:
        ENV_PROBE.clear()
        ENV_PROBE.update(result)
        _probe_state["inflight"] = False


# ────────────────────────────── 任务表 ──────────────────────────────


# 任务表落盘的节流：状态/阶段一变就写，纯进度变化最多 2 秒写一次
# （Demucs 一条任务能刷几百行进度，每行都重写一遍 JSON 是白给的磁盘活动）。
_last_queue_persist = {"at": 0.0}


def _update_job(job_id: str, **changes: Any) -> None:
    with jobs_lock:
        current = jobs.setdefault(job_id, {})
        current.update(changes)
        now = time.time()
        current["updated_at"] = now
        structural = any(key in changes for key in ("status", "phase", "stems", "input_path", "preset"))
        due = structural or (now - _last_queue_persist["at"]) >= 2.0
        if due:
            _last_queue_persist["at"] = now
    if due:
        persist_queue()


def _queue_position(job_id: str) -> int:
    """前面还压着几个任务。

    正在占显卡的那个也算：以前只数 status=="queued" 的同类，于是"排在一首后面"
    报成 0，界面写"前面还有 0 首"，用户以为马上轮到自己。
    """
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return 0
        created_at = job.get("created_at", 0)
        active = {"queued", "processing", "cancelling"}
        ahead = [
            other_id
            for other_id, other in jobs.items()
            if other_id != job_id
            and other.get("status") in active
            and (
                other.get("created_at", 0) < created_at
                or (other.get("created_at", 0) == created_at and other_id < job_id)
            )
        ]
    return len(ahead)


def job_progress(job_id: str) -> int:
    """当前进度值；Demucs 会打多条进度条，任务进度只往前走不回退。"""
    with jobs_lock:
        return int((jobs.get(job_id) or {}).get("progress", 0) or 0)


def _public_job(job_id: str) -> dict[str, Any] | None:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return None
        status_name = job["status"]
        phase = job.get("phase") or (
            "queued" if status_name == "queued" else {"done": "finished", "error": "failed", "cancelled": "cancelled"}.get(status_name, "separating")
        )
        progress = job.get("progress", 0)
        started_at = job.get("running_at")
        elapsed = round(time.time() - started_at, 1) if started_at and status_name not in {"done", "error", "cancelled"} else job.get("elapsed_seconds")
        created_at = job.get("created_at", 0)
        eta_seconds: float | None = None
        # 只有真的量到一步以上才外推剩余时间：否则"约剩 45 秒"会随已用时间一路往上涨，
        # 那是看着比没有更慌的假承诺。
        if status_name in {"queued", "processing"} and progress >= 20 and elapsed and progress < 100:
            eta_seconds = round(max(elapsed * (100 - progress) / progress, 0), 1)
        snapshot = {
            "job_id": job_id,
            "status": status_name,
            "progress": progress,
            "phase": phase,
            "phase_label": PHASE_LABELS.get(phase, phase),
            "error": job.get("error"),
            "stems": dict(job.get("stems", {})),
            "preset": job.get("preset", DEFAULT_PRESET),
            "stem_names": list(job.get("stem_names", STEM_NAMES)),
            "source_name": job.get("source_name"),
            "source_bytes": job.get("source_bytes"),
            "elapsed_seconds": elapsed,
            "eta_seconds": eta_seconds,
            "queue_position": 0,
            "output_dir": job.get("output_dir"),
            "created_at": created_at,
            "finished_at": job.get("finished_at"),
            "ttl_seconds": FILE_TTL_SECONDS,
        }
    # 排队位次单独取锁：jobs_lock 不是可重入锁，别在持锁时调用别的加锁函数。
    snapshot["queue_position"] = _queue_position(job_id) if status_name == "queued" else 0
    return snapshot


def job_snapshot(job_id: str) -> dict[str, Any] | None:
    """任务快照（界面、Agent 工具、下载路由共用）。"""
    public = _public_job(job_id)
    if public is None:
        return None
    stems = public["stems"]
    for stem_name, info in stems.items():
        path = Path(str(info.get("path", "")))
        info["exists"] = path.is_file()
        if info["exists"]:
            info["bytes"] = path.stat().st_size
        info.setdefault("label", STEM_LABELS.get(stem_name, {}).get("zh", stem_name))
    return public


def _job_cancel_requested(job_id: str) -> bool:
    with jobs_lock:
        job = jobs.get(job_id)
        return bool(job and job.get("cancel_requested"))


def create_job(*, source_name: str, source_bytes: int, preset: str = DEFAULT_PRESET) -> str:
    """登记一个分离任务（内存任务表 + 排队信息）。"""
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "progress": 1,
            "phase": "queued",
            "cancel_requested": False,
            "preset": preset,
            "stem_names": list(PRESETS[preset]["stems"]),
            "source_name": source_name,
            "source_bytes": source_bytes,
            "output_dir": str(OUTPUTS_DIR / job_id / "stems"),
            "created_at": now,
            "updated_at": now,
        }
    # 立刻就落盘：万一"刚排上队就断电"，开机恢复才有东西可恢复。
    persist_queue()
    return job_id


def validate_preset(preset: str | None) -> str:
    candidate = (preset or DEFAULT_PRESET).strip().lower()
    if candidate not in PRESETS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"未知音轨预设 {candidate}，可用：{'、'.join(PRESETS)}",
        )
    return candidate


def preset_descriptors() -> list[dict[str, Any]]:
    """预设的对外描述——界面渲染与 Agent 工具共用这一份，避免两处各写一遍轨名。"""
    return [
        {
            "preset": key,
            "label": preset["label"],
            "description": preset["description"],
            "is_default": key == DEFAULT_PRESET,
            "stems": [
                {"name": name, "label_zh": STEM_LABELS[name]["zh"], "label_en": STEM_LABELS[name]["en"]}
                for name in preset["stems"]
            ],
        }
        for key, preset in PRESETS.items()
    ]


def launch_separation(job_id: str, input_path: Path) -> None:
    """在守护线程里跑分离（Agent 提交走这条；HTTP 提交由 BackgroundTasks 触发同一个函数）。"""
    threading.Thread(
        target=run_separation,
        args=(job_id, Path(input_path)),
        name=f"vocal-separation-{job_id}",
        daemon=True,
    ).start()


def submit_source_file(source_path: Path, preset: str = DEFAULT_PRESET) -> tuple[str, Path]:
    """把一个**本机已有**的音频/视频文件登记成分离任务：复制进 uploads 后开工。

    Agent API 用它按路径提交；界面走 multipart 上传，两者最终都是 `run_separation`。
    """
    path = Path(source_path)
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")
    extension = path.suffix.lower()
    if extension not in ACCEPTED_EXTENSIONS:
        raise ValueError(
            f"不支持 {extension or '未知'} 格式，可用：{'、'.join(sorted(ACCEPTED_EXTENSIONS))}"
        )
    size = path.stat().st_size
    if size == 0:
        raise ValueError(f"文件为空：{path}")
    if size > MAX_FILE_SIZE:
        raise ValueError(f"文件超过 {MAX_FILE_SIZE // (1024 * 1024)} MB：{path}")

    job_id = create_job(source_name=path.name, source_bytes=size, preset=preset)
    destination = UPLOADS_DIR / f"{job_id}{extension}"
    try:
        shutil.copyfile(path, destination)
    except OSError as exc:
        with jobs_lock:
            jobs.pop(job_id, None)
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"复制输入文件失败：{exc}") from exc
    _update_job(job_id, input_path=str(destination))
    launch_separation(job_id, destination)
    return job_id, destination


# ────────────────────────────── 任务表落盘与开机恢复 ──────────────────────────────


def queue_path() -> Path:
    return DATA_DIR / QUEUE_FILE_NAME


def _persistable(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    """任务表里能写进磁盘的那部分（子进程句柄、临时字段一律不进）。"""
    return {
        "job_id": job_id,
        "status": job.get("status"),
        "phase": job.get("phase"),
        "progress": job.get("progress", 0),
        "preset": job.get("preset", DEFAULT_PRESET),
        "stem_names": list(job.get("stem_names", STEM_NAMES)),
        "source_name": job.get("source_name"),
        "source_bytes": job.get("source_bytes"),
        "output_dir": job.get("output_dir"),
        "input_path": job.get("input_path"),
        "stems": dict(job.get("stems", {})),
        "error": job.get("error"),
        "cancel_requested": bool(job.get("cancel_requested")),
        "created_at": job.get("created_at", 0),
        "running_at": job.get("running_at"),
        "finished_at": job.get("finished_at"),
        "updated_at": job.get("updated_at", 0),
    }


def persist_queue() -> bool:
    """把当前任务表原子写进 queue.json。"""
    with jobs_lock:
        records = [_persistable(job_id, job) for job_id, job in jobs.items()]
    records.sort(key=lambda record: record.get("updated_at") or 0)
    if len(records) > QUEUE_KEEP_JOBS:
        records = records[-QUEUE_KEEP_JOBS:]
    target = queue_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps({"version": 1, "saved_at": time.time(), "jobs": records}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, target)
        return True
    except OSError:
        return False  # 落盘失败不能让正在跑的分离停下来；重启会少一条恢复记录而已


def load_queue_records() -> list[dict[str, Any]]:
    try:
        payload = json.loads(queue_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(payload, dict):
        records = payload.get("jobs")
    elif isinstance(payload, list):  # 早期形状（万一有）：只认列表，不猜
        records = payload
    else:
        return []
    return [record for record in records if isinstance(record, dict) and record.get("job_id")] if isinstance(records, list) else []


def _interrupted_reason(missing_input: bool) -> str:
    if missing_input:
        return (
            "服务在这一点重启过，这个任务当时还没跑完，而且它要的输入文件已经不在了 —— "
            "没有把它记成完成。请重新提交这首歌。"
        )
    return (
        "服务在这个任务运行中重启了。它没有被记成完成，也没有自动重跑（避免在你没看着的时候"
        "占住显卡）。输入文件还在，重新提交即可。"
    )


def recover_jobs_on_startup() -> dict[str, Any]:
    """开机把 queue.json 读回来：没跑完的要么重投、要么如实标成"被打断"。

    判据是"绝不说谎"：曾经 processing/queued 的任务永远不会被恢复成 done，
    也不会悄悄写进 history.json。曾经排队、输入文件还在的，重新开工；
    正在跑的（半成品由 sweep_partial_outputs() 回收）标成 interrupted。
    """
    records = load_queue_records()
    report: dict[str, Any] = {
        "status": "ok",
        "loaded": len(records),
        "requeued": [],
        "interrupted": [],
        "restored_terminal": 0,
        "skipped": [],
    }
    now = time.time()
    for record in records:
        job_id = str(record.get("job_id") or "")
        if not re.fullmatch(r"[0-9a-f]{12}", job_id):
            report["skipped"].append({"job_id": job_id, "reason": "编号不合法"})
            continue
        status = record.get("status")
        input_path = Path(str(record.get("input_path") or ""))
        has_input = input_path.is_file()
        job = {
            key: value
            for key, value in record.items()
            if key not in {"job_id"}
        }
        job.setdefault("created_at", now)
        job.setdefault("updated_at", now)
        job.setdefault("stem_names", list(PRESETS.get(str(job.get("preset") or DEFAULT_PRESET), PRESETS[DEFAULT_PRESET])["stems"]))
        with jobs_lock:
            if job_id in jobs:  # 内存里已经有（同进程内重建），不覆盖
                continue
            jobs[job_id] = job

        if status in {"queued", "processing", "cancelling"}:
            if status == "queued" and has_input:
                job["status"] = "queued"
                job["phase"] = "queued"
                job["error"] = None
                job["cancel_requested"] = False
                report["requeued"].append(job_id)
                launch_separation(job_id, input_path)
                continue
            job.update(
                {
                    "status": "error",
                    "phase": INTERRUPTED_PHASE,
                    "progress": 0,
                    "error": _interrupted_reason(not has_input),
                    "cancel_requested": False,
                    "stems": {},
                    "finished_at": now,
                }
            )
            report["interrupted"].append({"job_id": job_id, "was": status, "had_input": has_input})
        else:
            report["restored_terminal"] += 1
    if report["requeued"] or report["interrupted"] or report["loaded"]:
        persist_queue()
    return report


def _directory_bytes(directory: Path) -> int:
    total = 0
    for item in directory.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def sweep_partial_outputs() -> dict[str, Any]:
    """开机回收"没有主人的"输出目录与输入文件，并把回收了什么如实报出来。

    以前半成品要等到 3600 秒的 TTL 清扫才走，期间占着盘也没人说什么；
    现在服务一起来就按任务表点名：不在表里、或对应任务没成功留下音轨的，收走。
    """
    reclaimed: list[dict[str, Any]] = []
    with jobs_lock:
        live = {job_id: dict(job) for job_id, job in jobs.items()}
    active = {"queued", "processing", "cancelling"}

    for directory in sorted(OUTPUTS_DIR.iterdir()) if OUTPUTS_DIR.is_dir() else []:
        if not directory.is_dir():
            continue
        job_id = directory.name
        job = live.get(job_id)
        if job is not None and (
            job.get("status") in active or (job.get("status") == "done" and job.get("stems"))
        ):
            continue  # 还在排/还在跑的由它自己收尾；成功的成果更不该在开机时被收走
        size = _directory_bytes(directory)
        _remove_quiet(directory)
        reclaimed.append(
            {
                "kind": "output",
                "path": str(directory),
                "job_id": job_id if re.fullmatch(r"[0-9a-f]{12}", job_id) else None,
                "bytes": size,
                "reason": "任务表里没有这个输出目录的主人" if job is None else "对应任务没有成功留下音轨",
            }
        )

    for file_path in sorted(UPLOADS_DIR.iterdir()) if UPLOADS_DIR.is_dir() else []:
        if not file_path.is_file():
            continue
        job_id = file_path.stem
        job = live.get(job_id)
        if job is not None and job.get("status") in active:
            continue  # 排队/在跑的任务还要用它
        if job is not None and job.get("status") == "done":
            continue  # 正常收尾会删；留着就别在开机时抢着删
        size = 0
        try:
            size = file_path.stat().st_size
            file_path.unlink(missing_ok=True)
        except OSError:
            continue
        reclaimed.append(
            {
                "kind": "upload",
                "path": str(file_path),
                "job_id": job_id if re.fullmatch(r"[0-9a-f]{12}", job_id) else None,
                "bytes": size,
                "reason": "输入文件已经没有对应的活动任务",
            }
        )

    report = {
        "items": reclaimed,
        "dirs": sum(1 for item in reclaimed if item["kind"] == "output"),
        "uploads": sum(1 for item in reclaimed if item["kind"] == "upload"),
        "bytes": sum(int(item["bytes"]) for item in reclaimed),
    }
    return report


def startup_report() -> dict[str, Any]:
    with startup_report_lock:
        return json.loads(json.dumps(STARTUP_REPORT))


def record_startup_report(recovery: dict[str, Any], swept: dict[str, Any]) -> dict[str, Any]:
    report = {**recovery, "swept": swept, "reported_at": time.time()}
    with startup_report_lock:
        STARTUP_REPORT.clear()
        STARTUP_REPORT.update(report)
    if swept["items"] or recovery.get("interrupted") or recovery.get("requeued"):
        print(
            "[vocal] 开机恢复："
            f"读回 {report['loaded']} 条任务，重新排队 {len(report['requeued'])} 条，"
            f"标记被打断 {len(report['interrupted'])} 条；"
            f"回收半成品 {swept['dirs']} 个输出目录 + {swept['uploads']} 个输入文件，"
            f"共 {round(swept['bytes'] / 1_048_576, 1)} MB。",
            flush=True,
        )
    return report


# ────────────────────────────── 历史输出索引 ──────────────────────────────


def history_path() -> Path:
    return DATA_DIR / HISTORY_FILE_NAME


def load_history() -> list[dict[str, Any]]:
    try:
        entries = json.loads(history_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return entries if isinstance(entries, list) else []


def _save_history(entries: list[dict[str, Any]]) -> None:
    target = history_path()
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def append_history(job_id: str) -> dict[str, Any] | None:
    """把完成的任务写进历史索引（结果文件被 1 小时清理后条目会被剪掉）。"""
    snapshot = job_snapshot(job_id)
    if snapshot is None:
        return None
    entry = {
        "job_id": job_id,
        "preset": snapshot["preset"],
        "source_name": snapshot["source_name"],
        "source_bytes": snapshot["source_bytes"],
        "stems": [
            {
                "name": name,
                "label": STEM_LABELS.get(name, {}).get("zh", name),
                "path": info.get("path"),
                "bytes": info.get("bytes"),
            }
            for name, info in snapshot["stems"].items()
        ],
        "output_dir": snapshot["output_dir"],
        "created_at": snapshot["created_at"],
        "finished_at": snapshot["finished_at"],
    }
    with history_lock:
        entries = [item for item in load_history() if item.get("job_id") != job_id]
        entries.insert(0, entry)
        _save_history(entries[:HISTORY_LIMIT])
    return entry


def prune_history() -> int:
    """丢掉输出目录已经不存在的历史条目，返回删除数量。"""
    with history_lock:
        entries = load_history()
        kept = [item for item in entries if Path(str(item.get("output_dir") or "")).is_dir()]
        removed = len(entries) - len(kept)
        if removed:
            _save_history(kept)
    return removed


def history_entries(
    limit: int = 20,
    *,
    source_name: str | None = None,
    source_bytes: int | None = None,
) -> dict[str, Any]:
    """历史输出列表；给了 name+bytes 时只返回**同一份源文件**的可下载记录（用于重复分离提示）。"""
    entries = load_history()
    items: list[dict[str, Any]] = []
    for entry in entries:
        output_dir = Path(str(entry.get("output_dir") or ""))
        stems = []
        for stem in entry.get("stems", []):
            path = Path(str(stem.get("path") or ""))
            exists = path.is_file()
            stems.append(
                {
                    **stem,
                    "exists": exists,
                    "bytes": path.stat().st_size if exists else stem.get("bytes"),
                }
            )
        available = [stem for stem in stems if stem["exists"]]
        if source_name is not None:
            if str(entry.get("source_name") or "").lower() != source_name.lower():
                continue
            if source_bytes is not None and int(entry.get("source_bytes") or -1) != int(source_bytes):
                continue
            if not available:
                continue  # 文件已被清理，不该再提醒"分离过了"
        items.append(
            {
                **entry,
                "stems": stems,
                "available_stems": len(available),
                "total_bytes": sum(int(stem.get("bytes") or 0) for stem in stems),
                "expired": not available,
                "output_dir_exists": output_dir.is_dir(),
            }
        )
    total = len(items)
    capped = max(1, min(int(limit), HISTORY_LIMIT))
    return {"total": total, "items": items[:capped], "truncated": total > capped}


# ────────────────────────────── 清理 ──────────────────────────────


def cleanup_old_files(max_age_seconds: int = FILE_TTL_SECONDS) -> None:
    """清理过期上传、输出和内存中的任务记录。"""
    now = time.time()
    for directory in (UPLOADS_DIR, OUTPUTS_DIR):
        if not directory.is_dir():
            continue
        for item in directory.iterdir():
            try:
                age = now - item.stat().st_mtime
                if age <= max_age_seconds:
                    continue
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink(missing_ok=True)
            except OSError:
                continue

    with jobs_lock:
        expired = [
            job_id
            for job_id, job in jobs.items()
            if now - job.get("updated_at", now) > max_age_seconds
        ]
        for job_id in expired:
            jobs.pop(job_id, None)
        dropped = len(expired)

    if dropped:
        persist_queue()
    prune_history()


def _cleanup_loop() -> None:
    while not cleanup_stop.wait(1800):
        cleanup_old_files()


# ────────────────────────────── 分离主流程 ──────────────────────────────


def _close_pipe(process: Any) -> None:
    """关掉子进程的输出管道；测试里的假进程没有 close()，所以按能力调用。"""
    close = getattr(getattr(process, "stdout", None), "close", None)
    if callable(close):
        try:
            close()
        except OSError:
            pass


def child_environment() -> dict[str, str]:
    """给子进程一份确定的环境。

    Windows 上把 stdout 重进管道后 Python 默认按 GBK 写、而且块缓冲到进程退出才刷出来：
    前者会让后端原文变成乱码，后者让"加载模型/分离中"这些阶段话到最后一刻才露面。
    进度条本身是 tqdm 显式 flush 的，所以实时性靠这一份环境补齐。
    """
    return {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}


def _terminate_process(process: Any) -> None:
    """真的把子进程按下去：terminate -> 等它落地 -> 还没死就 kill。

    只 terminate 不 wait 的话，Windows 上它可能还在写那个 WAV，紧接着的删除就会
    撞 PermissionError，半成品留在盘上。
    """
    if process is None:
        return
    try:
        if process.poll() is not None:
            return
    except (OSError, ValueError):
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=5)
        return
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
    except (OSError, AttributeError):
        return
    try:
        process.wait(timeout=5)
    except (OSError, TypeError, ValueError, subprocess.SubprocessError):
        pass


def _remove_quiet(target: Path) -> None:
    """删文件或目录；被占用/已经没了都不影响主流程（槽位释放比这重要）。"""
    try:
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
    except OSError:
        pass


class SeparationTimeout(Exception):
    """墙钟到点：子进程还占着显卡但没有可确认的进展，按时间收手。"""


_STREAM_END = object()


def iter_child_output(
    job_id: str,
    process: Any,
    *,
    stage_label: str = "分离",
    deadline_at: float | None = None,
) -> Any:
    """按行读子进程输出，但**不等它说话**。

    以前是 `for line in process.stdout:` —— Demucs 卡住不吐字时这一行就永远阻塞，
    于是"取消"和"超时"都要等到 3600 秒 TTL 清扫才可能生效（验收的 MAJOR 之一）。
    现在读行放到守护线程里，主循环每 CHILD_POLL_SECONDS 醒一次，先看取消标记、
    再看墙钟；任一成立就走 terminate -> wait -> kill（`_terminate_process`）。
    """
    lines: queue.Queue = queue.Queue(maxsize=256)

    def pump() -> None:
        stream = getattr(process, "stdout", None)
        if stream is None:
            lines.put(_STREAM_END)
            return
        try:
            for line in stream:
                lines.put(line)
        except (OSError, ValueError):  # 进程被终止后读管道会抛，正常收尾
            pass
        finally:
            lines.put(_STREAM_END)

    threading.Thread(target=pump, name=f"vocal-output-reader-{job_id}", daemon=True).start()

    exhausted = False
    while not exhausted:
        if _job_cancel_requested(job_id):
            _terminate_process(process)
            raise SeparationCancelled
        if deadline_at is not None and time.time() >= deadline_at:
            _terminate_process(process)
            raise SeparationTimeout(
                f"{stage_label}超过 {human_minutes(JOB_WALL_CLOCK_SECONDS)} 没跑完，已终止子进程并放开发显卡的槽位。"
            )
        try:
            item = lines.get(timeout=CHILD_POLL_SECONDS)
        except queue.Empty:
            continue
        if item is _STREAM_END:
            exhausted = True
            continue
        yield item


def child_deadline() -> float:
    """本回合的墙钟红线：从现在起再给 JOB_WALL_CLOCK_SECONDS。"""
    return time.time() + JOB_WALL_CLOCK_SECONDS


def human_minutes(seconds: int) -> str:
    """把秒数说成人能照着判断的单位（不到一分钟就说秒，别写"0 分钟"）。"""
    value = max(1, int(seconds))
    if value < 60:
        return f"{value} 秒"
    minutes = value / 60
    text = f"{int(minutes)}" if float(minutes).is_integer() else f"{round(minutes, 1)}"
    return f"{text} 分钟"


def wait_for_child(process: Any, timeout: float, *, stage_label: str = "分离") -> int:
    """等子进程退出，但有上限。

    管道关了就等于"它说完了"，不等于"它退了"：一个卡在退出路径上的 Demucs
    （显存没释放、文件句柄没关）会让无界的 process.wait() 永远不返回，
    于是槽位一直被占。到点同样走 terminate -> wait -> kill。
    """
    try:
        try:
            return int(process.wait(timeout=timeout))
        except TypeError:
            # 测试里的假进程只有 wait() 一个形参（和 _terminate_process 同一套按能力调用）。
            return int(process.wait())
    except subprocess.TimeoutExpired as exc:
        _terminate_process(process)
        raise SeparationTimeout(
            f"{stage_label}的子进程在到点后没有退出，已强制终止并释放显卡槽位。"
        ) from exc


def acquire_separation_slot(job_id: str) -> str:
    """拿分离槽位，返回 acquired / cancelled / timeout。

    排队中的任务以前只在开工前检查一次取消标记，于是"取消一首排队的歌"会静默失效——
    槽位一空出来照样把整首跑完，白占显卡。这里改成每秒回看一次取消。
    """
    deadline = time.time() + QUEUE_WAIT_TIMEOUT_SECONDS
    while True:
        if _job_cancel_requested(job_id):
            return "cancelled"
        if SEPARATION_SLOTS.acquire(timeout=SLOT_POLL_SECONDS):
            if _job_cancel_requested(job_id):
                SEPARATION_SLOTS.release()
                return "cancelled"
            return "acquired"
        if time.time() >= deadline:
            return "timeout"


def output_tail(lines: Any) -> str:
    """把子进程的原始输出收成一段可读文本（保留最后几行，不截断单行）。"""
    return "\n".join(str(line).rstrip() for line in list(lines)[-ERROR_OUTPUT_TAIL:] if str(line).strip())


def _demucs_phase(line: str) -> str | None:
    lowered = line.lower()
    if "downloading" in lowered:
        return "downloading_model"
    if "separating track" in lowered or "applying" in lowered or "%|" in lowered:
        return "separating"
    return None


def demucs_progress(line: str) -> int | None:
    """把 Demucs 自己打印的百分比换成任务进度；这一行没有百分比就返回 None。

    进度不是假装的：加载模型占前 10%，Demucs 的实测推进度映射到 10-95%，
    写完音轨文件才给 97%，100% 只在四条轨都落到磁盘上之后给。
    """
    match = PROGRESS_PATTERN.search(line)
    if not match:
        return None
    return max(10, min(10 + int(int(match.group(1)) * 0.85), 95))


def convert_video_to_audio(job_id: str, input_path: Path, deadline_at: float | None = None) -> Path:
    """把视频文件解码为 16 位 PCM WAV，输出与输入同名以便沿用 Demucs 目录结构。"""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "未找到 ffmpeg，无法读取视频文件。请安装 ffmpeg，或把 ffmpeg.exe 放到后端目录（"
            f"{BASE_DIR}），或设置环境变量 VOCAL_SEPARATOR_FFMPEG 指向它。"
        )

    _update_job(job_id, phase="extracting_audio", progress=4)
    audio_path = input_path.with_suffix(".wav")
    command = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        str(audio_path),
    ]

    output_lines: deque[str] = deque(maxlen=ERROR_OUTPUT_TAIL)
    process = None
    try:
        process = subprocess.Popen(  # noqa: S603
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_environment(),
        )
        _update_job(job_id, process=process)

        # 墙钟与取消都在 iter_child_output 里判：ffmpeg 卡住不吐字时这条也不能干等。
        for line in iter_child_output(job_id, process, stage_label="从视频提取音频", deadline_at=deadline_at):
            clean_line = line.strip()
            if clean_line:
                output_lines.append(clean_line)

        return_code = wait_for_child(
            process, max(30.0, (deadline_at - time.time()) if deadline_at else 60.0), stage_label="从视频提取音频"
        )
        _close_pipe(process)
        if _job_cancel_requested(job_id):
            raise SeparationCancelled
        if return_code != 0:
            raise RuntimeError(output_tail(output_lines) or f"ffmpeg 退出码：{return_code}")
    except SeparationCancelled:
        _terminate_process(process)
        _remove_quiet(audio_path)
        raise
    except SeparationTimeout:
        _terminate_process(process)
        _remove_quiet(audio_path)
        raise

    if not audio_path.is_file() or audio_path.stat().st_size == 0:
        raise RuntimeError(
            "视频里没有可用的音轨（或该视频编码 ffmpeg 读不了）。"
            "可以先用别的工具导出音频再上传，或换一个 ffmpeg 构建。"
        )
    return audio_path


def failure_remedy(detail: str, return_code: int) -> str:
    """失败原因 = 原始输出最后几行 + 一句能执行的出路。

    只贴 traceback 等于把排查工作丢回给用户；这里按真实报错特征给具体命令，
    认不出来的情况原样把输出交出去（不编造原因）。
    """
    head = f"Demucs 退出码：{return_code}"
    body = detail or head
    lowered = detail.lower()
    hint = None
    if "no module named" in lowered and "demucs" in lowered:
        hint = f"后端用的 Python 里没有 Demucs：\"{sys.executable}\" -m pip install -r \"{BASE_DIR / 'requirements.txt'}\""
    elif "no module named" in lowered and "torch" in lowered:
        hint = f"后端用的 Python 里没有 PyTorch：\"{sys.executable}\" -m pip install -r \"{BASE_DIR / 'requirements.txt'}\""
    elif "out of memory" in lowered:
        hint = "显存不够：先关掉其它占显卡的程序再重试；还不行就用 CPU 分离（慢很多，但能跑完）。"
    elif "no kernel image" in lowered or "cuda error" in lowered or "cudnn" in lowered:
        hint = f"PyTorch 与本机显卡驱动/CUDA 版本不匹配：重装轮子 \"{sys.executable}\" -m pip install -r \"{BASE_DIR / 'requirements.txt'}\""
    elif "not a valid file" in lowered or "unable to find a suitable" in lowered or "corrupt" in lowered:
        hint = "这个文件读不出来：确认它没被下载截断，或先导出一条 WAV 再分离。"
    elif "permission denied" in lowered or "winerror 5" in lowered:
        hint = "输出目录写不进去：释放该目录权限，或用 VOCAL_SEPARATOR_DATA_DIR 指到可写的盘。"
    if hint:
        return f"{body}\n下一步：{hint}"
    return body


def run_separation(job_id: str, input_path: Path) -> None:
    """在后台线程中运行 Demucs，并把结果写入任务独占目录。"""
    work_dir = OUTPUTS_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    # 记下输入文件：开机恢复要靠它判断"这个任务还能不能重投"。
    _update_job(job_id, input_path=str(input_path))
    if _job_cancel_requested(job_id):
        _remove_quiet(work_dir)
        _remove_quiet(input_path)
        _update_job(job_id, status="cancelled", progress=0, phase="cancelled", error=None, finished_at=time.time())
        return

    with jobs_lock:
        job = jobs.get(job_id) or {}
        preset_name = job.get("preset", DEFAULT_PRESET)
        stem_names = tuple(job.get("stem_names", PRESETS[preset_name]["stems"]))

    slot = acquire_separation_slot(job_id)
    if slot != "acquired":
        _remove_quiet(work_dir)
        _remove_quiet(input_path)
        if slot == "cancelled":
            _update_job(job_id, status="cancelled", progress=0, phase="cancelled", error=None, finished_at=time.time())
        else:
            _update_job(
                job_id,
                status="error",
                progress=0,
                phase="failed",
                error=(
                    f"排队等待超过 {human_minutes(QUEUE_WAIT_TIMEOUT_SECONDS)}，任务已放弃。"
                    "前面那一首如果卡住会自己按墙钟收手；也可以直接取消它腾出槽位。"
                ),
                finished_at=time.time(),
            )
        return

    _update_job(job_id, status="processing", progress=2, phase="loading_model", running_at=time.time())

    audio_path = input_path
    command: list[str] = []
    output_lines: deque[str] = deque(maxlen=ERROR_OUTPUT_TAIL)
    process = None
    try:
        # 墙钟从拿到槽位、真正开工这一刻算起，排队时间不计入。
        deadline_at = child_deadline()
        if input_path.suffix.lower() in VIDEO_EXTENSIONS:
            audio_path = convert_video_to_audio(job_id, input_path, deadline_at=deadline_at)
            _update_job(job_id, progress=6, phase="loading_model")

        command = [
            sys.executable,
            "-m",
            "demucs",
            "-o",
            str(work_dir),
            "--name",
            MODEL_NAME,
        ]
        two_stems = PRESETS[preset_name].get("two_stems")
        if two_stems:
            command += ["--two-stems", two_stems]
        command.append(str(audio_path))

        process = subprocess.Popen(  # noqa: S603
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=child_environment(),
        )
        _update_job(job_id, process=process)

        for line in iter_child_output(job_id, process, stage_label="分离音轨", deadline_at=deadline_at):
            clean_line = line.strip()
            if clean_line:
                output_lines.append(clean_line)
            changes: dict[str, Any] = {}
            phase = _demucs_phase(clean_line)
            if phase:
                changes["phase"] = phase
            step = demucs_progress(clean_line)
            if step is None and phase == "separating":
                # Demucs 刚说"Separating track"时进度条还没开画，
                # 下限给它映射的起点 10%，别让阶段写着"分离音轨"而进度停在 2%。
                step = 10
            if step is not None:
                # 只往前走，不回退（Demucs 每条轨各起一个进度条）。
                changes["progress"] = max(job_progress(job_id), step)
            if changes:
                _update_job(job_id, **changes)

        return_code = wait_for_child(
            process, max(30.0, deadline_at - time.time()), stage_label="分离音轨"
        )
        _close_pipe(process)
        if _job_cancel_requested(job_id):
            raise SeparationCancelled
        if return_code != 0:
            raise RuntimeError(failure_remedy(output_tail(output_lines), return_code))

        _update_job(job_id, progress=97, phase="writing")
        result_dir = work_dir / MODEL_NAME / input_path.stem
        if not result_dir.is_dir():
            raise RuntimeError(
                f"Demucs 跑完了但没找到输出目录（{result_dir}）。"
                "多半是磁盘空间不足或该目录被安全软件拦下，请清理磁盘后重试。"
            )

        stems_dir = work_dir / "stems"
        stems_dir.mkdir(exist_ok=True)
        stems: dict[str, dict[str, float | str]] = {}
        for stem_name in stem_names:
            source = result_dir / f"{stem_name}.wav"
            if not source.is_file():
                continue
            destination = stems_dir / f"{stem_name}.wav"
            shutil.move(str(source), destination)
            size_bytes = destination.stat().st_size
            stems[stem_name] = {
                "size_mb": round(size_bytes / (1024 * 1024), 1),
                "bytes": size_bytes,
                "path": str(destination),
                "label": STEM_LABELS.get(stem_name, {}).get("zh", stem_name),
            }

        if not stems:
            raise RuntimeError(
                f"分离跑完了，但 {result_dir} 里一条音轨都没有；"
                "请确认输入文件不是 0 秒或纯静音，然后重试。"
            )

        shutil.rmtree(work_dir / MODEL_NAME, ignore_errors=True)
        finished_at = time.time()
        started_at = None
        with jobs_lock:
            started_at = (jobs.get(job_id) or {}).get("running_at")
        _update_job(
            job_id,
            status="done",
            progress=100,
            phase="finished",
            stems=stems,
            error=None,
            finished_at=finished_at,
            elapsed_seconds=round(finished_at - (started_at or finished_at), 1),
        )
        append_history(job_id)
    except SeparationCancelled:
        # 先把状态落定再收尾：清理半成品可能因为文件还被占着而失败，
        # 但那不该让用户一直看到"处理中"，更不该把显卡槽位一起带走。
        _update_job(job_id, status="cancelled", progress=0, phase="cancelled", error=None, finished_at=time.time())
        _terminate_process(process)
        _remove_quiet(work_dir)
    except SeparationTimeout as exc:
        # 墙钟到点（或子进程赖着不退）：状态先落定，再收进程与半成品，最后还槽位。
        _update_job(
            job_id,
            status="error",
            progress=0,
            phase="failed",
            error=(
                f"{exc} 这一步是墙钟判定，不看输出行数 —— 子进程一个字都不吐时也只有它还能收手。"
                "这一首没有记成完成，输出目录已清掉。下一步：确认没有别的程序占着显卡；"
                f"CPU 分离本来就慢，可用 VOCAL_SEPARATOR_JOB_TIMEOUT 调大上限（当前 {human_minutes(JOB_WALL_CLOCK_SECONDS)}）。"
            ),
            finished_at=time.time(),
        )
        _terminate_process(process)
        _remove_quiet(work_dir)
    except Exception as exc:
        _update_job(
            job_id,
            status="error",
            progress=0,
            phase="failed",
            error=f"音轨分离失败：{exc}",
            finished_at=time.time(),
        )
        _terminate_process(process)
        _remove_quiet(work_dir)
    finally:
        # 槽位必须还回去：Windows 上删一个还被子进程占着的 WAV 会抛 PermissionError，
        # 以前它会把整个 finally 打断，显卡槽位从此没人能拿到，后面所有任务永远排队。
        _update_job(job_id, process=None)
        try:
            _remove_quiet(input_path)
            if audio_path != input_path:
                _remove_quiet(audio_path)
        finally:
            SEPARATION_SLOTS.release()


def cancel_job(job_id: str) -> dict[str, Any]:
    """请求取消任务：置标记，并终止正在跑的 Demucs/ffmpeg 子进程。"""
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在或已过期")

    process: subprocess.Popen[str] | None = None
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在或已过期")
        if job["status"] not in {"queued", "processing", "cancelling"}:
            raise HTTPException(status.HTTP_409_CONFLICT, "当前任务无法取消：任务已经结束了")
        job["cancel_requested"] = True
        job["status"] = "cancelling"
        job["phase"] = "cancelled"
        job["updated_at"] = time.time()
        process = job.get("process")

    was_running = process is not None and process.poll() is None
    if was_running:
        try:
            process.terminate()
        except OSError:
            pass

    return {
        "job_id": job_id,
        "status": "cancelling",
        "progress": 0,
        "phase": "cancelled",
        "phase_label": PHASE_LABELS["cancelled"],
        "error": None,
        "stems": {},
        "was_running": was_running,
        "note": (
            "已终止在跑的 Demucs 子进程（terminate -> 等一下 -> 还没死就 kill），"
            "输出目录会被清掉。子进程一句输出都没有也照样生效：读输出放在守护线程里，"
            f"主循环每 {CHILD_POLL_SECONDS} 秒醒一次看取消标记。"
            if was_running
            else "这一首还在排队、没占用显卡：线程每秒查一次取消标记，最多 1 秒后就会放弃。"
        ),
    }


# ────────────────────────────── HTTP 接口 ──────────────────────────────


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 先把任务表读回来（排队且输入还在的重新开工，跑过一半的如实标"被打断"），
    # 再按读回来的表回收半成品输出 —— 顺序反了就会把刚要续跑的东西删掉。
    record_startup_report(recover_jobs_on_startup(), sweep_partial_outputs())
    cleanup_old_files()
    _start_environment_probe()
    cleanup_stop.clear()
    threading.Thread(target=_cleanup_loop, name="vocal-cleanup", daemon=True).start()
    try:
        import agent_api

        agent_api.write_endpoint_file(int(os.getenv("VOCAL_SEPARATOR_PORT", "8000")))
    except Exception:
        pass  # Agent 契约层缺失时界面照常工作
    try:
        yield
    finally:
        cleanup_stop.set()


app = FastAPI(title="声析 · 本地音轨分离 API", version=APP_VERSION, lifespan=lifespan)

cors_origins = os.getenv(
    "VOCAL_SEPARATOR_ORIGINS",
    "http://localhost:3000,http://127.0.0.1:3000",
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in cors_origins if origin.strip()],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# 来源闸门（Host 钉死 + Origin/Referer 白名单 + 非 GET 共享令牌）。
# 加在 CORS 之后 = 套在它外面，所以任何请求先过闸门再谈跨域读写；
# 状态变更路由因此永远不可能带出 `Access-Control-Allow-Origin: *`。
app.add_middleware(
    local_guard.make_middleware_class(),
    resolve_state=lambda: local_guard.guard_state_for(DATA_DIR),
)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    """健康检查。

    顶层保留 `status/gpu_available/demucs_available/ffmpeg_available/model`（Electron 壳与
    start.bat 的探针读的是这几个键），同时按 Agent API 标准返回 `{ok, data}` 信封。
    """
    environment = probe_environment()
    legacy_gpu = environment.get("cuda_available")
    if environment.get("status") != "ready":
        legacy_gpu = shutil.which("nvidia-smi") is not None
    payload: dict[str, Any] = {
        "status": "ok",
        "gpu_available": bool(legacy_gpu),
        "demucs_available": bool(environment.get("demucs_installed", importlib.util.find_spec("demucs") is not None)),
        "ffmpeg_available": bool(environment.get("ffmpeg_available", find_ffmpeg())),
        "model": MODEL_NAME,
    }
    with jobs_lock:
        active_jobs = sum(1 for job in jobs.values() if job.get("status") in {"queued", "processing", "cancelling"})
    payload["ok"] = True
    payload["data"] = {
        "project": "vocal-separator",
        "version": APP_VERSION,
        "agent_api": 1,
        "uptime_ms": int((time.time() - _START_TIME) * 1000),
        "active_jobs": active_jobs,
        "presets": preset_descriptors(),
        "guard": local_guard.guard_state_for(DATA_DIR).describe(),
        "recovery": startup_report(),
        **environment,
    }
    return payload


@app.get("/api/session-token")
async def session_token() -> dict[str, Any]:
    """第一方取令牌的下发口：界面（浏览器 / 桌面壳）先问这里要令牌，再发非 GET 请求。

    能走到这里说明 Host 已经是回环、Origin/Referer 已经是第一方 —— 闸门已经跑过了
    （中间件在外层）。所以这句话不是"绕开令牌"，而是"令牌本来就只发给第一方页面"：
    跨站页面既读不到响应（CORS 只放行本机来源），也过不了 Origin 判定。
    未启用令牌时返回 `required:false`，界面据此不带请求头，行为与启用前一致。
    """
    state = local_guard.guard_state_for(DATA_DIR)
    payload: dict[str, Any] = {
        "required": state.token_required,
        "header": local_guard.TOKEN_HEADER,
        "token": state.token if state.token_required else None,
        "source": state.token_source,
    }
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@app.post("/api/separate", status_code=status.HTTP_202_ACCEPTED)
async def separate_audio(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    preset: str | None = Form(None),
) -> dict[str, Any]:
    """流式保存上传文件，并创建后台分离任务。"""
    if not file.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请选择音频文件")

    preset_name = validate_preset(preset)
    extension = Path(file.filename).suffix.lower()
    if extension not in ACCEPTED_EXTENSIONS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"不支持 {extension or '未知'} 格式，请使用音频文件或 MP4、MOV、MKV、WebM、AVI 视频",
        )

    job_id = create_job(source_name=Path(file.filename).name, source_bytes=0, preset=preset_name)
    input_path = UPLOADS_DIR / f"{job_id}{extension}"
    written = 0

    try:
        with input_path.open("wb") as destination:
            while chunk := await file.read(UPLOAD_CHUNK_SIZE):
                written += len(chunk)
                if written > MAX_FILE_SIZE:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        "文件超过 500 MB，请压缩后再试",
                    )
                destination.write(chunk)
    except (HTTPException, asyncio.CancelledError):
        input_path.unlink(missing_ok=True)
        with jobs_lock:
            jobs.pop(job_id, None)
        raise
    except OSError as exc:
        input_path.unlink(missing_ok=True)
        with jobs_lock:
            jobs.pop(job_id, None)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"保存上传文件失败：{exc}",
        ) from exc
    finally:
        await file.close()

    if written == 0:
        input_path.unlink(missing_ok=True)
        with jobs_lock:
            jobs.pop(job_id, None)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "音频文件为空")

    _update_job(job_id, source_bytes=written, input_path=str(input_path))
    background_tasks.add_task(run_separation, job_id, input_path)
    return _public_job(job_id) or {"job_id": job_id, "status": "queued", "progress": 1}


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str) -> dict[str, Any]:
    job = job_snapshot(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在或已过期")
    return job


@app.post("/api/jobs/{job_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_job_route(job_id: str) -> dict[str, Any]:
    return cancel_job(job_id)


@app.get("/api/history")
async def get_history(
    limit: int = Query(20, ge=1, le=HISTORY_LIMIT),
    name: str | None = Query(None, max_length=260),
    source_bytes: int | None = Query(None, ge=0, alias="bytes"),
) -> dict[str, Any]:
    """历史输出索引；带 name+bytes 时用于"这首歌是否已经分离过"的判定。"""
    return history_entries(limit, source_name=name, source_bytes=source_bytes)


@app.get("/api/download/{job_id}/{stem_name}")
async def download_stem(job_id: str, stem_name: str) -> FileResponse:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id) or not STEM_SAFE_PATTERN.match(stem_name):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "音轨文件不存在")

    job = job_snapshot(job_id)
    if job is None or job["status"] != "done" or stem_name not in job["stems"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "音轨文件不存在或已过期")

    file_path = OUTPUTS_DIR / job_id / "stems" / f"{stem_name}.wav"
    if not file_path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "音轨文件不存在或已过期")

    source_stem = Path(str(job.get("source_name") or "声析音轨")).stem or "声析音轨"
    return FileResponse(
        file_path,
        media_type="audio/wav",
        filename=f"{source_stem}-{stem_name}.wav",
    )


try:  # Agent API 契约端点（同一进程、同一端口，不新增监听口）
    from agent_api import router as agent_router

    app.include_router(agent_router)
except Exception as exc:  # 契约层缺失不能让界面挂掉
    import logging

    logging.getLogger(__name__).warning("Agent API 契约层未挂载：%s", exc)


if __name__ == "__main__":
    # `python backend/main.py` 会把本文件加载成 `__main__`。不登记这个别名的话，
    # Agent 工具里的 `import main` 会拿到**第二个模块实例**——另开一张任务表、
    # 另一个环境探测缓存，界面在跑的任务在 Agent 眼里就"不存在"。
    sys.modules.setdefault("main", sys.modules["__main__"])

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("VOCAL_SEPARATOR_PORT", "8000")))
