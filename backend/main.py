"""本地 AI 音轨分离服务。"""

from __future__ import annotations

import importlib.util
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

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile, status
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
PROGRESS_PATTERN = re.compile(r"(\d{1,3})%")

jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()
cleanup_stop = threading.Event()


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


def convert_video_to_audio(job_id: str, input_path: Path) -> Path:
    """把视频文件解码为 16 位 PCM WAV，输出与输入同名以便沿用 Demucs 目录结构。"""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            "未找到 ffmpeg，无法读取视频文件。请安装 ffmpeg，或把 ffmpeg.exe 放到后端目录。"
        )

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


def _update_job(job_id: str, **changes: Any) -> None:
    with jobs_lock:
        current = jobs.setdefault(job_id, {})
        current.update(changes)
        current["updated_at"] = time.time()


def _public_job(job_id: str) -> dict[str, Any] | None:
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return None
        return {
            "job_id": job_id,
            "status": job["status"],
            "progress": job.get("progress", 0),
            "error": job.get("error"),
            "stems": job.get("stems", {}),
        }


def _job_cancel_requested(job_id: str) -> bool:
    with jobs_lock:
        job = jobs.get(job_id)
        return bool(job and job.get("cancel_requested"))


def cleanup_old_files(max_age_seconds: int = FILE_TTL_SECONDS) -> None:
    """清理过期上传、输出和内存中的任务记录。"""
    now = time.time()
    for directory in (UPLOADS_DIR, OUTPUTS_DIR):
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


def _cleanup_loop() -> None:
    while not cleanup_stop.wait(1800):
        cleanup_old_files()


@asynccontextmanager
async def lifespan(_: FastAPI):
    cleanup_old_files()
    cleanup_stop.clear()
    cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
    cleanup_thread.start()
    try:
        yield
    finally:
        cleanup_stop.set()


app = FastAPI(title="声析 · 本地音轨分离 API", lifespan=lifespan)

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


def run_separation(job_id: str, input_path: Path) -> None:
    """在后台线程中运行 Demucs，并把结果写入任务独占目录。"""
    work_dir = OUTPUTS_DIR / job_id
    work_dir.mkdir(parents=True, exist_ok=True)
    if _job_cancel_requested(job_id):
        shutil.rmtree(work_dir, ignore_errors=True)
        input_path.unlink(missing_ok=True)
        _update_job(job_id, status="cancelled", progress=0, error=None)
        return
    _update_job(job_id, status="processing", progress=2)

    audio_path = input_path
    command: list[str] = []
    output_lines: list[str] = []
    try:
        if input_path.suffix.lower() in VIDEO_EXTENSIONS:
            audio_path = convert_video_to_audio(job_id, input_path)
            _update_job(job_id, progress=2)

        command = [
            sys.executable,
            "-m",
            "demucs",
            "-o",
            str(work_dir),
            "--name",
            MODEL_NAME,
            str(audio_path),
        ]
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
                match = PROGRESS_PATTERN.search(line)
                if match:
                    _update_job(
                        job_id,
                        progress=max(2, min(int(match.group(1)), 96)),
                    )

        return_code = process.wait()
        if _job_cancel_requested(job_id):
            raise SeparationCancelled
        if return_code != 0:
            detail = "\n".join(output_lines[-8:])
            raise RuntimeError(detail or f"Demucs 退出码：{return_code}")

        result_dir = work_dir / MODEL_NAME / input_path.stem
        if not result_dir.is_dir():
            raise RuntimeError("未找到 Demucs 输出目录")

        stems_dir = work_dir / "stems"
        stems_dir.mkdir(exist_ok=True)
        stems: dict[str, dict[str, float]] = {}
        for stem_name in STEM_NAMES:
            source = result_dir / f"{stem_name}.wav"
            if not source.is_file():
                continue
            destination = stems_dir / f"{stem_name}.wav"
            shutil.move(str(source), destination)
            stems[stem_name] = {
                "size_mb": round(destination.stat().st_size / (1024 * 1024), 1)
            }

        if not stems:
            raise RuntimeError("分离完成，但未生成任何音轨文件")

        shutil.rmtree(work_dir / MODEL_NAME, ignore_errors=True)
        _update_job(job_id, status="done", progress=100, stems=stems, error=None)
    except SeparationCancelled:
        shutil.rmtree(work_dir, ignore_errors=True)
        _update_job(job_id, status="cancelled", progress=0, error=None)
    except Exception as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        _update_job(
            job_id,
            status="error",
            progress=0,
            error=f"音轨分离失败：{exc}",
        )
    finally:
        _update_job(job_id, process=None)
        input_path.unlink(missing_ok=True)
        if audio_path != input_path:
            audio_path.unlink(missing_ok=True)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "gpu_available": shutil.which("nvidia-smi") is not None,
        "demucs_available": importlib.util.find_spec("demucs") is not None,
        "ffmpeg_available": bool(find_ffmpeg()),
        "model": MODEL_NAME,
    }


@app.post("/api/separate", status_code=status.HTTP_202_ACCEPTED)
async def separate_audio(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """流式保存上传文件，并创建后台分离任务。"""
    if not file.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请选择音频文件")

    extension = Path(file.filename).suffix.lower()
    if extension not in ACCEPTED_EXTENSIONS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"不支持 {extension or '未知'} 格式，请使用音频文件或 MP4、MOV、MKV、WebM、AVI 视频",
        )

    job_id = uuid.uuid4().hex[:12]
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
        raise
    except OSError as exc:
        input_path.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"保存上传文件失败：{exc}",
        ) from exc
    finally:
        await file.close()

    if written == 0:
        input_path.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "音频文件为空")

    now = time.time()
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "progress": 1,
            "cancel_requested": False,
            "created_at": now,
            "updated_at": now,
        }

    background_tasks.add_task(run_separation, job_id, input_path)
    return _public_job(job_id) or {"job_id": job_id, "status": "queued", "progress": 1}


@app.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str) -> dict[str, Any]:
    job = _public_job(job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在或已过期")
    return job


@app.post("/api/jobs/{job_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_job(job_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在或已过期")

    process: subprocess.Popen[str] | None = None
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "任务不存在或已过期")
        if job["status"] not in {"queued", "processing", "cancelling"}:
            raise HTTPException(status.HTTP_409_CONFLICT, "当前任务无法取消")
        job["cancel_requested"] = True
        job["status"] = "cancelling"
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
        "error": None,
        "stems": {},
    }


@app.get("/api/download/{job_id}/{stem_name}")
async def download_stem(job_id: str, stem_name: str) -> FileResponse:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id) or stem_name not in STEM_NAMES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "音轨文件不存在")

    job = _public_job(job_id)
    if job is None or job["status"] != "done" or stem_name not in job["stems"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "音轨文件不存在或已过期")

    file_path = OUTPUTS_DIR / job_id / "stems" / f"{stem_name}.wav"
    if not file_path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "音轨文件不存在或已过期")

    return FileResponse(
        file_path,
        media_type="audio/wav",
        filename=f"{stem_name}.wav",
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
