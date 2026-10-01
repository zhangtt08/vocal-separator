"""声析（vocal-separator）的 Agent 工具声明。

这是本项目按 `personal-agent-hub/docs/AGENT_API_STANDARD.md` 唯一需要写的文件：
声明工具 + 把调用接到后端的真实能力上。

关键点：**没有第二个数据源**。所有 handler 都 `import main`（常驻 FastAPI 服务模块），
调用界面同款函数 —— `probe_environment()` / `job_snapshot()` / `submit_source_file()` /
`cancel_job()` / `history_entries()`。Agent 与 Electron 界面看到的是同一张任务表、
同一批输出文件，所以返回的版本号、路径、字节数都是本机实测值，不是占位数据。

契约端点（/api/health、/api/agent/*）挂在同一个 FastAPI 服务上（127.0.0.1:8000），
不新增端口；见 backend/agent_api.py 与 agent/README.md。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

from errors import AgentError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BACKEND_DIR = ROOT / "backend"


def _app_version() -> str:
    try:
        return str(json.loads((ROOT / "package.json").read_text(encoding="utf-8")).get("version") or "0.0.0")
    except (OSError, ValueError):
        return "0.0.0"


PROJECT = {
    "name": "vocal-separator",
    "version": _app_version(),
    "summary": "声析：本机 Demucs 音轨分离。探测 AI 环境、提交分离任务、查进度、取消、列历史输出。",
}


def backend() -> Any:
    """拿到**正在跑的那个**后端模块实例（同一个进程、同一张任务表，绝不重开一份服务）。

    `python backend/main.py` 时后端是 `__main__`；main.py 已在启动时把自己登记成
    `sys.modules["main"]`。这里两边都认，任何一边拿不到就明确报错，绝不静默
    import 出第二个实例（那会有一张空任务表，然后所有任务都"不存在"）。
    """
    module = sys.modules.get("main")
    if module is None:
        candidate = sys.modules.get("__main__")
        if candidate is not None and hasattr(candidate, "run_separation"):
            module = candidate
    if module is None:
        if str(BACKEND_DIR) not in sys.path:
            sys.path.insert(0, str(BACKEND_DIR))
        import main as imported_module

        module = imported_module
    if not hasattr(module, "probe_environment") or not hasattr(module, "submit_source_file"):
        raise AgentError(
            "backend_unavailable",
            "拿不到后端能力模块（缺少 probe_environment/submit_source_file）；"
            "请用 python backend/main.py 或 npm run agent:serve 启动后端后重试。",
        )
    return module


# ────────────────────────────── 工具实现 ──────────────────────────────


def _stem_view(main: Any, stems: dict[str, Any]) -> dict[str, Any]:
    """把内部 stems 整理成对外结构，并现场核对文件是否还在。"""
    view: dict[str, Any] = {}
    for name, info in (stems or {}).items():
        path = Path(str(info.get("path") or ""))
        exists = path.is_file()
        view[name] = {
            "label": info.get("label") or main.STEM_LABELS.get(name, {}).get("zh", name),
            "path": str(path) if info.get("path") else None,
            "exists": exists,
            "bytes": path.stat().st_size if exists else info.get("bytes"),
            "size_mb": info.get("size_mb"),
            "download_path": f"/api/download/{info.get('_job_id')}/{name}" if info.get("_job_id") else None,
        }
    return view


def _job_payload(main: Any, job_id: str) -> dict[str, Any]:
    snapshot = main.job_snapshot(job_id)
    if snapshot is None:
        raise AgentError("not_found", f"任务不存在或已过期（结果文件保留 {main.FILE_TTL_SECONDS // 60} 分钟）：{job_id}")
    stems = {name: {**info, "_job_id": job_id} for name, info in snapshot["stems"].items()}
    snapshot["stems"] = _stem_view(main, stems)
    snapshot["output_files"] = [item["path"] for item in snapshot["stems"].values() if item["exists"]]
    snapshot["total_output_bytes"] = sum(int(item["bytes"] or 0) for item in snapshot["stems"].values() if item["exists"])
    snapshot["terminal"] = snapshot["status"] in {"done", "error", "cancelled"}
    return snapshot


def _wait_for_terminal(main: Any, job_id: str, timeout_seconds: int, poll_seconds: float = 2.0) -> dict[str, Any]:
    deadline = time.time() + max(1, min(int(timeout_seconds), 1800))
    payload = _job_payload(main, job_id)
    while not payload["terminal"] and time.time() < deadline:
        time.sleep(poll_seconds)
        payload = _job_payload(main, job_id)
    payload["timed_out"] = not payload["terminal"]
    return payload


def _env_probe(input: dict[str, Any], _ctx: dict[str, Any]) -> dict[str, Any]:
    main = backend()
    environment = dict(main.probe_environment(force=bool(input.get("refresh"))))
    if environment.get("status") != "ready":
        # 后端刚起来时 torch 还在导入：如实说在探测，不猜结论。
        threading_probe = environment.get("status", "probing")
        return {
            "status": threading_probe,
            "retry_after_seconds": 5,
            "note": "环境探测仍在进行（torch/CUDA 首次导入需要几秒），请稍后重试。",
            "python_executable": sys.executable,
        }
    with main.jobs_lock:
        active = [job_id for job_id, job in main.jobs.items() if job.get("status") in {"queued", "processing", "cancelling"}]
    environment["active_jobs"] = len(active)
    environment["status"] = "ready"
    return environment


def _mode_list(_input: dict[str, Any], _ctx: dict[str, Any]) -> dict[str, Any]:
    main = backend()
    # 预设与轨名只有 backend.preset_descriptors() 这一个来源；这里补上底层参数，不再各写一遍标签。
    presets = [
        {
            **item,
            "demucs_args": ["-m", "demucs", "--name", main.MODEL_NAME]
            + (
                ["--two-stems", main.PRESETS[item["preset"]]["two_stems"]]
                if main.PRESETS[item["preset"]].get("two_stems")
                else []
            ),
        }
        for item in main.preset_descriptors()
    ]
    return {
        "model": main.MODEL_NAME,
        "presets": presets,
        "accepted_audio_extensions": sorted(main.ALLOWED_EXTENSIONS),
        "accepted_video_extensions": sorted(main.VIDEO_EXTENSIONS),
        "output_format": "wav",
        "max_file_size_bytes": main.MAX_FILE_SIZE,
        "file_ttl_seconds": main.FILE_TTL_SECONDS,
        "single_concurrency_note": "一次只跑一个分离任务（显卡独占更划算），后来的任务留在队列里并给出排队位次。",
    }


def _separate_submit(input: dict[str, Any], _ctx: dict[str, Any]) -> dict[str, Any]:
    main = backend()
    if input.get("confirm") is not True:
        raise AgentError(
            "bad_input",
            "分离会真的启动 Demucs 子进程并占用显卡/CPU：必须显式传 confirm:true（risk=exec 的二次确认）。",
        )
    preset = input.get("preset") or main.DEFAULT_PRESET
    if preset not in main.PRESETS:
        raise AgentError("bad_input", f"未知 preset：{preset}；可用：{'、'.join(main.PRESETS)}")

    source = Path(str(input["input_path"]))
    try:
        job_id, working_copy = main.submit_source_file(source, preset)
    except FileNotFoundError as exc:
        raise AgentError("not_found", str(exc)) from exc
    except ValueError as exc:
        raise AgentError("bad_input", str(exc)) from exc
    except (RuntimeError, OSError) as exc:
        raise AgentError("submit_failed", f"提交分离任务失败：{exc}") from exc

    payload = _job_payload(main, job_id)
    payload["submitted_from"] = str(source.resolve())
    payload["working_copy"] = str(working_copy)
    payload["estimated_note"] = "耗时取决于音频长度与是否用 GPU；进度、阶段与预计剩余时间在 vocal.job_status / vocal.job_wait 里返回。"
    if input.get("wait"):
        payload = _wait_for_terminal(main, job_id, int(input.get("timeout_seconds") or 600))
    return payload


def _job_status(input: dict[str, Any], _ctx: dict[str, Any]) -> dict[str, Any]:
    main = backend()
    return _job_payload(main, str(input["job_id"]))


def _job_wait(input: dict[str, Any], _ctx: dict[str, Any]) -> dict[str, Any]:
    main = backend()
    return _wait_for_terminal(main, str(input["job_id"]), int(input.get("timeout_seconds") or 300))


def _job_cancel(input: dict[str, Any], _ctx: dict[str, Any]) -> dict[str, Any]:
    main = backend()
    job_id = str(input["job_id"])
    if main.job_snapshot(job_id) is None:
        raise AgentError("not_found", f"任务不存在或已过期：{job_id}")
    snapshot = main.job_snapshot(job_id) or {}
    if snapshot.get("terminal") or snapshot.get("status") in {"done", "error", "cancelled"}:
        raise AgentError("conflict", f"任务已经结束了，无法取消（当前状态：{snapshot.get('status')}）")
    result = main.cancel_job(job_id)
    result["source_name"] = snapshot.get("source_name")
    result["note"] = "已请求取消：Demucs 子进程被终止，任务目录已清理，源文件不会被保留。"
    return result


def _output_list(input: dict[str, Any], _ctx: dict[str, Any]) -> dict[str, Any]:
    main = backend()
    limit = int(input.get("limit") or 20)
    history = main.history_entries(
        limit=limit,
        source_name=input.get("name"),
        source_bytes=int(input["bytes"]) if input.get("bytes") is not None else None,
    )
    items = []
    for item in history["items"]:
        stems = {}
        for stem in item.get("stems", []):
            path = Path(str(stem.get("path") or ""))
            exists = path.is_file()
            stems[stem.get("name")] = {
                "label": stem.get("label"),
                "path": str(path) if stem.get("path") else None,
                "exists": exists,
                "bytes": path.stat().st_size if exists else stem.get("bytes"),
            }
        items.append(
            {
                "job_id": item.get("job_id"),
                "preset": item.get("preset"),
                "source_name": item.get("source_name"),
                "source_bytes": item.get("source_bytes"),
                "created_at": item.get("created_at"),
                "created_at_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(item["created_at"]))
                if item.get("created_at")
                else None,
                "finished_at": item.get("finished_at"),
                "output_dir": item.get("output_dir"),
                "output_dir_exists": item.get("output_dir_exists"),
                "total_bytes": item.get("total_bytes"),
                "expired": item.get("expired"),
                "stems": stems,
                "download_paths": {name: f"/api/download/{item.get('job_id')}/{name}" for name, info in stems.items() if info["exists"]},
            }
        )
    outputs_dir = Path(main.OUTPUTS_DIR)
    on_disk = sorted(path.name for path in outputs_dir.iterdir() if path.is_dir()) if outputs_dir.is_dir() else []
    return {
        "history_file": str(main.history_path()),
        "outputs_dir": str(outputs_dir),
        "total": history["total"],
        "truncated": history["truncated"],
        "items": items,
        "output_dirs_on_disk": on_disk,
        "ttl_note": f"上传与输出文件在 {main.FILE_TTL_SECONDS // 60} 分钟后自动清理；过期条目 expired=true。",
    }


TOOLS: list[dict[str, Any]] = [
    {
        "name": "vocal.env_probe",
        "description": (
            "探测本机 AI 环境并返回实测值：python 解释器与版本、torch 版本、CUDA 是否真的可用（含显卡名）、"
            "demucs 是否安装及版本、ffmpeg 路径与版本、htdemucs 权重是否已缓存（含文件路径与 MB）、"
            "数据目录、CPU 核数、当前在跑的任务数。回答“这台机器能不能跑/为什么慢”先调它。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"refresh": {"type": "boolean", "description": "true = 重新探测（会阻塞几秒等 torch 导入）"}},
            "additionalProperties": False,
        },
        "risk": "read",
        "handler": _env_probe,
    },
    {
        "name": "vocal.mode_list",
        "description": (
            "列出支持的分离预设（四条音轨 / 人声+伴奏）、每个预设会输出哪些轨（中英文轨名）、"
            "底层 Demucs 参数、接受的输入格式、单文件大小上限与结果保留时长。"
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
        "risk": "read",
        "handler": _mode_list,
    },
    {
        "name": "vocal.separate_submit",
        "description": (
            "对本机已存在的音频/视频文件启动真实分离任务（复用后端的 Demucs 调用链），返回 job_id、输出目录、"
            "阶段与进度。必须 confirm:true；risk=exec。传 wait:true 时会阻塞到任务结束（受 timeout_seconds 限制），"
            "完成后可在 stems 里拿到真实输出文件路径与字节数。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "input_path": {"type": "string", "description": "本机音频/视频文件的绝对或相对路径"},
                "preset": {"type": "string", "description": "four_stems（默认）或 vocal_backing"},
                "confirm": {"type": "boolean", "description": "必须为 true：确认启动真实分离"},
                "wait": {"type": "boolean", "description": "true = 等到任务结束或超时"},
                "timeout_seconds": {"type": "integer", "description": "wait 时最长等待秒数，默认 600，上限 1800"},
            },
            "required": ["input_path", "confirm"],
            "additionalProperties": False,
        },
        "risk": "exec",
        "handler": _separate_submit,
    },
    {
        "name": "vocal.job_status",
        "description": (
            "查询分离任务当前状态：status/progress/phase（含中文阶段名）、已用秒数与预计剩余秒数、排队位次、"
            "每条音轨的真实路径、字节数与是否还在磁盘上。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "提交时返回的 12 位任务编号"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
        "risk": "read",
        "handler": _job_status,
    },
    {
        "name": "vocal.job_wait",
        "description": "阻塞轮询直到任务进入终态（done/error/cancelled）或超时，返回与 vocal.job_status 同样的结构并带 timed_out 标记。",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "timeout_seconds": {"type": "integer", "description": "默认 300，上限 1800"},
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
        "risk": "read",
        "handler": _job_wait,
    },
    {
        "name": "vocal.job_cancel",
        "description": "取消排队中或正在跑的任务：置取消标记并终止 Demucs 子进程，清理任务目录。已结束的任务返回 conflict。",
        "input_schema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
            "additionalProperties": False,
        },
        "risk": "write",
        "handler": _job_cancel,
    },
    {
        "name": "vocal.output_list",
        "description": (
            "列出历史分离结果（持久索引 backend/../history.json）：源文件名与字节数、预设、完成时间、"
            "每条音轨的真实路径/字节数/是否仍存在，以及 download_paths。带 name+bytes 用于“这首歌是否已经分离过”。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "最多返回条数，默认 20，上限 60"},
                "name": {"type": "string", "description": "只返回源文件名匹配的记录"},
                "bytes": {"type": "integer", "description": "配合 name，按源文件字节数精确匹配"},
            },
            "additionalProperties": False,
        },
        "risk": "read",
        "handler": _output_list,
    },
]


HANDLERS: dict[str, Callable[..., Any]] = {tool["name"]: tool["handler"] for tool in TOOLS}
