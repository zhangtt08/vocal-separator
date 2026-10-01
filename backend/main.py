"""本地 AI 音轨分离服务。

这个进程是本项目的**唯一常驻服务**：它既服务桌面界面（Electron 壳与浏览器），
也对外提供个人 Agent 服务的契约端点（GET /api/health、GET /api/agent/tools、
GET /api/agent/manifest、POST /api/agent/tool）。契约端点由 `backend/agent_api.py`
挂载，工具声明与实现放在 `agent/tools.py`，只监听 127.0.0.1，不新增端口。

界面与 Agent 走的是同一批能力函数：`probe_environment()`、`create_job()`、
`run_separation()`、`cancel_job()`、`history_entries()`。
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

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
}

# Demucs 一次跑满一张显卡才划算，所以分离槽位=1：后来的任务留在 queued 并给出排队位次。
SEPARATION_SLOTS = threading.Semaphore(1)

HISTORY_FILE_NAME = "history.json"
HISTORY_LIMIT = 60

jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()
history_lock = threading.Lock()
cleanup_stop = threading.Event()

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
    environment["file_ttl_seconds"] = FILE_TTL_SECONDS
    environment["max_file_size_bytes"] = MAX_FILE_SIZE
    return environment


def probe_environment(force: bool = False) -> dict[str, Any]:
    """返回缓存的环境探测结果；首次调用返回 probing 而不是卡住事件循环。"""
    with env_probe_lock:
        cached = dict(ENV_PROBE)
    if cached.get("status") == "ready" and not force:
        return cached
    if force:
        result = _compute_environment()
        with env_probe_lock:
            ENV_PROBE.clear()
            ENV_PROBE.update(result)
        return dict(result)
    return cached


def _probe_environment_in_background() -> None:
    try:
        result = _compute_environment()
    except Exception as exc:  # 探测失败也要留痕，界面上好解释
        result = {"status": "failed", "error": str(exc), "probed_at": time.time()}
    with env_probe_lock:
        ENV_PROBE.clear()
        ENV_PROBE.update(result)


# ────────────────────────────── 任务表 ──────────────────────────────


def _update_job(job_id: str, **changes: Any) -> None:
    with jobs_lock:
        current = jobs.setdefault(job_id, {})
        current.update(changes)
        current["updated_at"] = time.time()


def _queue_position(job_id: str) -> int:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return 0
        created_at = job.get("created_at", 0)
        ahead = [
            other
            for other_id, other in jobs.items()
            if other_id != job_id
            and other.get("status") == "queued"
            and other.get("created_at", 0) <= created_at
        ]
    return len(ahead)


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
        if status_name in {"queued", "processing"} and progress >= 8 and elapsed and progress < 100:
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
    launch_separation(job_id, destination)
    return job_id, destination


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


def _demucs_phase(line: str) -> str | None:
    lowered = line.lower()
    if "downloading" in lowered:
        return "downloading_model"
    if "separating track" in lowered or "applying" in lowered or "%|" in lowered:
        return "separating"
    return None


def convert_video_to_audio(job_id: str, input_path: Path) -> Path:
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

    output_lines: list[str] = []
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _update_job(job_id, process=process)

        if process.stdout is not None:
            for line in process.stdout:
                if _job_cancel_requested(job_id):
                    process.terminate()
                    raise SeparationCancelled
                clean_line = line.strip()
                if clean_line:
                    output_lines.append(clean_line)

        return_code = process.wait()
        _close_pipe(process)
        if _job_cancel_requested(job_id):
            raise SeparationCancelled
        if return_code != 0:
            detail = "\n".join(output_lines[-8:])
            raise RuntimeError(detail or f"ffmpeg 退出码：{return_code}")
    except SeparationCancelled:
        audio_path.unlink(missing_ok=True)
        raise

    if not audio_path.is_file() or audio_path.stat().st_size == 0:
        raise RuntimeError("视频转换音频失败：未生成有效音轨")
    return audio_path


def run_separation(job_id: str, input_path: Path) -> None:
    """在后台线程中运行 Demucs，并把结果写入任务独占目录。"""
    work_dir = OUTPUTS_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    if _job_cancel_requested(job_id):
        shutil.rmtree(work_dir, ignore_errors=True)
        input_path.unlink(missing_ok=True)
        _update_job(job_id, status="cancelled", progress=0, phase="cancelled", error=None)
        return

    with jobs_lock:
        job = jobs.get(job_id) or {}
        preset_name = job.get("preset", DEFAULT_PRESET)
        stem_names = tuple(job.get("stem_names", PRESETS[preset_name]["stems"]))

    if not SEPARATION_SLOTS.acquire(timeout=FILE_TTL_SECONDS):
        shutil.rmtree(work_dir, ignore_errors=True)
        input_path.unlink(missing_ok=True)
        _update_job(job_id, status="error", progress=0, phase="failed", error="排队等待超过 1 小时，任务已放弃。")
        return

    _update_job(job_id, status="processing", progress=2, phase="loading_model", running_at=time.time())

    audio_path = input_path
    command: list[str] = []
    output_lines: list[str] = []
    try:
        if input_path.suffix.lower() in VIDEO_EXTENSIONS:
            audio_path = convert_video_to_audio(job_id, input_path)
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

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        _update_job(job_id, process=process)

        if process.stdout is not None:
            for line in process.stdout:
                if _job_cancel_requested(job_id):
                    process.terminate()
                    raise SeparationCancelled
                clean_line = line.strip()
                if clean_line:
                    output_lines.append(clean_line)
                changes: dict[str, Any] = {}
                phase = _demucs_phase(clean_line)
                if phase:
                    changes["phase"] = phase
                match = PROGRESS_PATTERN.search(clean_line)
                if match:
                    # Demucs 自己那 0-100% 映射到任务的 10-95%，前面留给加载/下载，后面留给写文件。
                    changes["progress"] = max(10, min(10 + int(int(match.group(1)) * 0.85), 95))
                if changes:
                    _update_job(job_id, **changes)

        return_code = process.wait()
        _close_pipe(process)
        if _job_cancel_requested(job_id):
            raise SeparationCancelled
        if return_code != 0:
            detail = "\n".join(output_lines[-8:])
            raise RuntimeError(detail or f"Demucs 退出码：{return_code}")

        _update_job(job_id, progress=97, phase="writing")
        result_dir = work_dir / MODEL_NAME / input_path.stem
        if not result_dir.is_dir():
            raise RuntimeError("未找到 Demucs 输出目录")

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
            raise RuntimeError("分离完成，但未生成任何音轨文件")

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
        shutil.rmtree(work_dir, ignore_errors=True)
        _update_job(job_id, status="cancelled", progress=0, phase="cancelled", error=None, finished_at=time.time())
    except Exception as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        _update_job(
            job_id,
            status="error",
            progress=0,
            phase="failed",
            error=f"音轨分离失败：{exc}",
            finished_at=time.time(),
        )
    finally:
        _update_job(job_id, process=None)
        input_path.unlink(missing_ok=True)
        if audio_path != input_path:
            audio_path.unlink(missing_ok=True)
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

    if process is not None and process.poll() is None:
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
    }


# ────────────────────────────── HTTP 接口 ──────────────────────────────


@asynccontextmanager
async def lifespan(_: FastAPI):
    cleanup_old_files()
    probe_environment()
    cleanup_stop.clear()
    threading.Thread(target=_probe_environment_in_background, name="vocal-env-probe", daemon=True).start()
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
        **environment,
    }
    return payload


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
    except HTTPException:
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

    _update_job(job_id, source_bytes=written)
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
