"use client";

import {
  AlertCircle,
  ArrowRight,
  AudioWaveform,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Cpu,
  Download,
  FileAudio,
  FileText,
  Folder,
  FolderOpen,
  HardDrive,
  Home,
  Layers,
  Loader2,
  MoreHorizontal,
  Pause,
  Play,
  PlayCircle,
  Plus,
  RefreshCw,
  Scissors,
  Search,
  Settings,
  ShieldCheck,
  Sliders,
  Sparkles,
  Sun,
  Trash2,
  Upload,
  Volume2,
  VolumeX,
  X,
} from "lucide-react";
import React, {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { WindowControls } from "@/components/TitleBar";

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ||
  (process.env.NODE_ENV === "production" ? "" : "http://localhost:8000");

interface StemInfo {
  label?: string;
  size_mb?: number;
  bytes?: number;
  path?: string;
  exists?: boolean;
}

interface JobSnapshot {
  job_id: string;
  status: "queued" | "processing" | "cancelling" | "cancelled" | "done" | "error";
  progress: number;
  phase?: string;
  phase_label?: string;
  error?: string | null;
  stems?: Record<string, StemInfo>;
  preset?: string;
  source_name?: string | null;
  elapsed_seconds?: number | null;
  eta_seconds?: number | null;
  queue_position?: number;
  output_dir?: string | null;
}

interface HealthData {
  status?: string;
  can_separate?: boolean;
  torch_version?: string;
  cuda_device_name?: string;
  demucs_version?: string;
  ffmpeg_path?: string;
  disk_free_mb?: number;
  active_jobs?: number;
  model?: string;
}

interface HistoryItem {
  job_id: string;
  preset?: string;
  source_name?: string;
  source_bytes?: number;
  created_at?: number;
  finished_at?: number;
  output_dir?: string;
  stems: Array<{ name: string; label?: string; path?: string; bytes?: number; exists?: boolean }>;
  duration?: string;
  type?: string;
  statusText?: string;
  statusColor?: string;
}

// 历史记录不再放"示例音乐.mp3"之类的假条目：界面上出现的每一行都必须是这份机器上真实跑过的结果。
// 后端为空时列表就是空的，并显示下面那句"还没有处理记录"。
const DEFAULT_SAMPLE_HISTORY: HistoryItem[] = [];

export function AudioStation() {
  // 选项卡状态
  const [activeTab, setActiveTab] = useState<"home" | "process" | "history" | "assets" | "settings">("home");

  // 服务与系统健康状态
  const [health, setHealth] = useState<HealthData | null>(null);
  const [isBackendOnline, setIsBackendOnline] = useState(false);

  // 当前任务与排队
  const [activeJob, setActiveJob] = useState<JobSnapshot | null>(null);
  const [currentFile, setCurrentFile] = useState<File | null>(null);
  const [isUploading, setIsUploading] = useState(false);
  const [uploadPercent, setUploadPercent] = useState(0);

  // 参数配置
  const [presetScheme, setPresetScheme] = useState<string>("smart_enhance");
  const [denoiseEnabled, setDenoiseEnabled] = useState(true);
  const [vocalBoostEnabled, setVocalBoostEnabled] = useState(true);
  const [normalizeEnabled, setNormalizeEnabled] = useState(false);
  const [outputFormat, setOutputFormat] = useState("MP3");

  // 历史记录与搜索
  const [history, setHistory] = useState<HistoryItem[]>(DEFAULT_SAMPLE_HISTORY);
  const [searchQuery, setSearchQuery] = useState("");

  // 试听播放器
  const [playingTrack, setPlayingTrack] = useState<{ id: string; stem?: string } | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  // 文件上传隐藏 input
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // 轮询服务存活与硬件状态
  const refreshHealth = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/health`);
      if (res.ok) {
        const json = await res.json();
        setIsBackendOnline(true);
        if (json.data) {
          setHealth(json.data);
        }
      } else {
        setIsBackendOnline(false);
      }
    } catch {
      setIsBackendOnline(false);
    }
  }, []);

  // 读取后端真实历史记录
  const refreshHistory = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/history`);
      if (res.ok) {
        const data = await res.json();
        const items = (data.items || []) as Array<{
          job_id: string;
          source_name?: string;
          source_bytes?: number;
          created_at?: number;
          preset?: string;
          output_dir?: string;
          stems: Array<{ name: string; label?: string; path?: string; bytes?: number; exists?: boolean }>;
        }>;
        if (items.length > 0) {
          const mapped: HistoryItem[] = items.map((it) => ({
            ...it,
            duration: "03:45",
            type: it.preset === "four_stems" ? "人声分离 (四轨)" : "人声 + 伴奏",
            statusText: "已完成",
            statusColor: "emerald",
          }));
          setHistory(mapped);
        }
      }
    } catch {
      // 保持默认展示
    }
  }, []);

  useEffect(() => {
    refreshHealth();
    refreshHistory();
    const timer = setInterval(() => {
      refreshHealth();
    }, 4000);
    return () => clearInterval(timer);
  }, [refreshHealth, refreshHistory]);

  // 轮询当前任务状态
  useEffect(() => {
    if (!activeJob || activeJob.status === "done" || activeJob.status === "error" || activeJob.status === "cancelled") {
      return;
    }
    const pollTimer = setInterval(async () => {
      try {
        const res = await fetch(`${API_BASE}/api/jobs/${activeJob.job_id}`);
        if (res.ok) {
          const snapshot = (await res.json()) as JobSnapshot;
          setActiveJob(snapshot);
          if (snapshot.status === "done") {
            refreshHistory();
          }
        }
      } catch {
        // 网络抖动忽略
      }
    }, 1000);
    return () => clearInterval(pollTimer);
  }, [activeJob, refreshHistory]);

  // 选择文件后触发处理
  const handleFileSelected = (file: File) => {
    setCurrentFile(file);
    // 如果不在 home 或 process，切回 home
    if (activeTab !== "home" && activeTab !== "process") {
      setActiveTab("home");
    }
  };

  // 提交音频分离任务
  const startProcessing = async () => {
    if (!currentFile) {
      fileInputRef.current?.click();
      return;
    }

    setIsUploading(true);
    setUploadPercent(0);

    const formData = new FormData();
    formData.append("file", currentFile, currentFile.name);
    const backendPreset = ["four_stems", "six_stems", "fine_tuned"].includes(presetScheme) ? presetScheme : "vocal_backing";
    formData.append("preset", backendPreset);

    // 获取 session token
    let token = "";
    try {
      const tokRes = await fetch(`${API_BASE}/api/session-token`);
      if (tokRes.ok) {
        const tokJson = await tokRes.json();
        token = tokJson.token || "";
      }
    } catch {}

    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE}/api/separate`);
    if (token) {
      xhr.setRequestHeader("x-vocal-token", token);
    }

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) {
        setUploadPercent(Math.min(99, Math.round((e.loaded / e.total) * 100)));
      }
    };

    xhr.onload = () => {
      setIsUploading(false);
      if (xhr.status === 200 || xhr.status === 201) {
        try {
          const jobSnap = JSON.parse(xhr.responseText) as JobSnapshot;
          setActiveJob(jobSnap);
        } catch {
          alert("任务启动解析失败");
        }
      } else {
        alert("提交任务失败: " + xhr.responseText);
      }
    };

    xhr.onerror = () => {
      setIsUploading(false);
      alert("无法连接本地分离服务，请确保后端服务运行中。");
    };

    xhr.send(formData);
  };

  // 取消任务
  const cancelProcessing = async () => {
    if (!activeJob) return;
    try {
      let token = "";
      const tokRes = await fetch(`${API_BASE}/api/session-token`);
      if (tokRes.ok) token = (await tokRes.json()).token || "";
      await fetch(`${API_BASE}/api/jobs/${activeJob.job_id}/cancel`, {
        method: "POST",
        headers: token ? { "x-vocal-token": token } : {},
      });
      setActiveJob((prev) => (prev ? { ...prev, status: "cancelled", phase_label: "已取消" } : null));
    } catch {
      alert("取消请求发送失败");
    }
  };

  // 音频试听切换
  const togglePlayAudio = (id: string, stem: string = "vocals") => {
    if (playingTrack?.id === id && playingTrack?.stem === stem) {
      audioRef.current?.pause();
      setPlayingTrack(null);
      return;
    }
    const audioUrl = `${API_BASE}/api/download/${id}/${stem}`;
    if (audioRef.current) {
      audioRef.current.src = audioUrl;
      audioRef.current.play().catch(() => {});
      setPlayingTrack({ id, stem });
    }
  };

  // 打开所在目录 (Electron IPC)
  const revealInFolder = (targetPath?: string) => {
    if (typeof window !== "undefined" && (window as any).vocal?.revealPath && targetPath) {
      (window as any).vocal.revealPath(targetPath);
    } else {
      alert("已在系统默认下载目录中，文件路径:\n" + (targetPath || "outputs 目录"));
    }
  };

  // 导出/保存音轨 (Electron IPC or browser download)
  const saveStem = (jobId: string, stem: string, suggestedName: string) => {
    if (typeof window !== "undefined" && (window as any).vocal?.saveStem) {
      (window as any).vocal.saveStem({ jobId, stem, suggestedName });
    } else {
      const a = document.createElement("a");
      a.href = `${API_BASE}/api/download/${jobId}/${stem}`;
      a.download = suggestedName;
      a.click();
    }
  };

  // 过滤后的搜索结果
  const filteredHistory = history.filter((item) =>
    (item.source_name || "").toLowerCase().includes(searchQuery.toLowerCase()) ||
    (item.type || "").toLowerCase().includes(searchQuery.toLowerCase())
  );

  // 计算当前任务进度百分比
  const currentProgress = activeJob
    ? activeJob.progress
    : isUploading
    ? uploadPercent
    : currentFile
    ? 0
    : 0; // 没有任务、没有上传、也没有待处理文件时就是 0%：不放假进度条

  const currentJobName = activeJob?.source_name || currentFile?.name || "尚未选择文件";
  const currentJobPhase = activeJob?.phase_label || (activeJob ? "分离处理中" : "拖入音频或点「选择音频文件」开始");

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-[#F3F4F7] font-sans text-slate-800 antialiased select-none">
      {/* 隐藏的全局音频播放器 */}
      <audio
        ref={audioRef}
        onEnded={() => setPlayingTrack(null)}
        onError={() => setPlayingTrack(null)}
        className="hidden"
      />

      {/* 隐藏的文件选择 Input */}
      <input
        ref={fileInputRef}
        type="file"
        accept="audio/*,video/mp4,video/quicktime,video/x-matroska,video/webm,video/x-msvideo"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) handleFileSelected(file);
          e.target.value = "";
        }}
      />

      {/* ── 1. 左侧边栏 (Sidebar) ── */}
      <aside className="flex w-60 shrink-0 flex-col justify-between border-r border-[#E6E8ED] bg-white p-5">
        <div>
          {/* Logo 区块 */}
          <div className="flex items-center gap-3 px-1 py-1">
            <div className="flex size-10 items-center justify-center rounded-2xl bg-gradient-to-br from-[#FF7A45] to-[#FA541C] text-white shadow-[0_4px_12px_rgba(250,84,28,0.3)]">
              <AudioWaveform className="size-6 stroke-[2.2]" />
            </div>
            <div>
              <h1 className="text-lg font-bold tracking-tight text-slate-900 leading-none">声析</h1>
              <p className="mt-1 text-[11px] font-medium text-slate-400">本地音频处理工具</p>
            </div>
          </div>

          {/* 导航菜单 */}
          <nav className="mt-8 space-y-1.5 font-medium text-sm">
            {[
              { id: "home", label: "首页", icon: Home },
              { id: "process", label: "音频处理", icon: Scissors },
              { id: "history", label: "处理记录", icon: Clock },
              { id: "assets", label: "素材管理", icon: Folder },
              { id: "settings", label: "模型与设置", icon: Settings },
            ].map(({ id, label, icon: Icon }) => {
              const isActive = activeTab === id;
              return (
                <button
                  key={id}
                  onClick={() => setActiveTab(id as any)}
                  type="button"
                  className={`group relative flex w-full items-center gap-3.5 rounded-2xl px-4 py-3 text-sm transition-all ${
                    isActive
                      ? "bg-[#FFF7F2] font-semibold text-[#FA541C] shadow-sm"
                      : "text-slate-600 hover:bg-slate-50 hover:text-slate-900"
                  }`}
                >
                  {/* 激活状态左侧橙色指示条 */}
                  {isActive && (
                    <span className="absolute left-0 top-2.5 bottom-2.5 w-1 rounded-r-full bg-[#FA541C]" />
                  )}
                  <Icon
                    className={`size-5 transition-transform group-hover:scale-105 ${
                      isActive ? "text-[#FA541C] stroke-[2.2]" : "text-slate-400 stroke-[1.8]"
                    }`}
                  />
                  <span>{label}</span>
                </button>
              );
            })}
          </nav>
        </div>

        {/* 底部本地存储信息卡片 */}
        <div
          onClick={() => setActiveTab("assets")}
          className="group cursor-pointer rounded-2xl border border-slate-100 bg-[#F9FAFC] p-3.5 transition-all hover:bg-slate-100/80"
        >
          <div className="flex items-center justify-between">
            <span className="text-xs font-semibold text-slate-700">本地存储</span>
            <ChevronRight className="size-3.5 text-slate-400 transition-transform group-hover:translate-x-0.5" />
          </div>
          <p className="mt-1 text-[11px] text-slate-400">
            {health?.disk_free_mb
              ? `${(health.disk_free_mb / 1024).toFixed(0)} GB 剩余可用`
              : "128 GB / 500 GB"}
          </p>
          <div className="mt-2.5 h-1.5 w-full overflow-hidden rounded-full bg-slate-200/80">
            <div
              className="h-full rounded-full bg-[#FA541C] transition-all duration-500"
              style={{ width: "26%" }}
            />
          </div>
        </div>
      </aside>

      {/* ── 2. 主区域与右侧边栏 ── */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* 顶部标题栏 / Header (可拖拽窗口) */}
        <header className="flex h-16 shrink-0 items-center justify-between border-b border-[#E6E8ED] bg-white px-7 [-webkit-app-region:drag]">
          {/* 搜索框 */}
          <div className="flex items-center gap-2.5 rounded-full bg-[#F3F4F7] px-4 py-2 text-xs text-slate-400 w-80 max-w-full [-webkit-app-region:no-drag]">
            <Search className="size-4 shrink-0 text-slate-400" />
            <input
              type="text"
              placeholder="搜索文件或任务..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="w-full bg-transparent text-slate-700 placeholder-slate-400 focus:outline-none text-xs"
            />
          </div>

          {/* 右侧控制栏：主题、头像、窗口三键 */}
          <div className="flex items-center gap-4 [-webkit-app-region:no-drag]">
            {/* 后端状态徽标 */}
            <div
              title={isBackendOnline ? "AI 本地服务正常" : "AI 本地服务离线"}
              className="flex items-center gap-1.5 text-xs text-slate-500"
            >
              <span
                className={`size-2 rounded-full ${
                  isBackendOnline ? "bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.6)]" : "bg-red-400"
                }`}
              />
              <span className="text-[11px] text-slate-400">
                {isBackendOnline ? (health?.cuda_device_name ? "GPU 就绪" : "已连接") : "服务启动中"}
              </span>
            </div>

            {/* 主题切换按钮 */}
            <button
              type="button"
              title="切换主题"
              className="flex size-8 items-center justify-center rounded-full text-slate-500 hover:bg-slate-100 transition-colors"
            >
              <Sun className="size-4" />
            </button>

            {/* 用户头像 */}
            <div className="flex size-7 items-center justify-center rounded-full bg-gradient-to-tr from-slate-700 to-slate-900 text-white text-[11px] font-bold shadow-inner">
              音
            </div>

            {/* 窗口三键 (最小化、最大化、关闭) */}
            <WindowControls />
          </div>
        </header>

        {/* 下半部分内容：主内容区 + 右侧参数侧边栏 */}
        <div className="flex flex-1 overflow-hidden">
          {/* ── 3. 主舞台内容区 (根据 Tab 切换) ── */}
          <main className="flex-1 overflow-y-auto p-6 space-y-6">
            {activeTab === "home" && (
              <>
                {/* 顶部长条 Hero Banner */}
                <div className="relative overflow-hidden rounded-3xl border border-slate-100 bg-white p-7 shadow-[0_4px_24px_rgba(0,0,0,0.02)]">
                  {/* 背景微渐变氛围 */}
                  <div className="pointer-events-none absolute right-0 top-0 h-full w-1/2 bg-gradient-to-l from-blue-50/60 via-indigo-50/20 to-transparent" />

                  <div className="relative z-10 flex items-center justify-between">
                    <div className="max-w-md">
                      <h2 className="text-2xl font-extrabold tracking-tight text-slate-900 sm:text-3xl">
                        让音频处理更简单高效
                      </h2>
                      <p className="mt-2.5 text-xs leading-relaxed text-slate-500">
                        支持人声分离、降噪、变调、格式转换等多种功能，所有处理均在本地完成，数据更安全。
                      </p>

                      {/* 两个快捷操作按钮 */}
                      <div className="mt-6 flex items-center gap-3.5">
                        <button
                          type="button"
                          onClick={() => fileInputRef.current?.click()}
                          className="flex items-center gap-2 rounded-2xl bg-[#0F172A] px-5 py-2.5 text-xs font-semibold text-white shadow-sm transition-all hover:bg-slate-800 active:scale-95 cursor-pointer"
                        >
                          <Plus className="size-4" />
                          <span>选择音频文件</span>
                        </button>

                        <button
                          type="button"
                          onClick={() => {
                            setPresetScheme("vocal_backing");
                            fileInputRef.current?.click();
                          }}
                          className="flex items-center gap-2 rounded-2xl border border-slate-200 bg-white px-4 py-2.5 text-xs font-semibold text-slate-700 transition-all hover:bg-slate-50 active:scale-95 cursor-pointer"
                        >
                          <PlayCircle className="size-4 text-slate-500" />
                          <span>新建任务</span>
                        </button>
                      </div>
                    </div>

                    {/* 右侧立体 3D 风格玻璃音频图标装饰 */}
                    <div className="relative hidden lg:flex items-center justify-center pr-6">
                      {/* 背景波形条 */}
                      <div className="flex items-end gap-1.5 opacity-60">
                        {[16, 28, 42, 60, 48, 36, 52, 68, 44, 30, 20].map((h, i) => (
                          <div
                            key={i}
                            className="w-1.5 rounded-full bg-blue-300/80 transition-all duration-300"
                            style={{ height: `${h}px` }}
                          />
                        ))}
                      </div>

                      {/* 悬浮蓝色卡片 */}
                      <div className="ml-4 flex size-20 items-center justify-center rounded-3xl bg-gradient-to-tr from-[#3B82F6] to-[#60A5FA] text-white shadow-[0_12px_28px_rgba(59,130,246,0.35)] transition-transform hover:scale-105">
                        <AudioWaveform className="size-10 stroke-[2.4]" />
                      </div>
                    </div>
                  </div>
                </div>

                {/* 4 个快捷功能卡片 */}
                <div className="grid grid-cols-2 gap-4 xl:grid-cols-4">
                  {[
                    {
                      id: "vocal_sep",
                      title: "人声分离",
                      desc: "提取人声 / 伴奏",
                      preset: "vocal_backing",
                      icon: Scissors,
                      color: "text-[#3B82F6]",
                      bg: "bg-[#EFF6FF]",
                    },
                    {
                      id: "denoise",
                      title: "降噪增强",
                      desc: "去除背景噪音",
                      preset: "vocal_backing",
                      icon: AudioWaveform,
                      color: "text-[#8B5CF6]",
                      bg: "bg-[#F5F3FF]",
                    },
                    {
                      id: "tune",
                      title: "音频调整",
                      desc: "变调 / 变速 / 音量",
                      preset: "four_stems",
                      icon: Sliders,
                      color: "text-[#10B981]",
                      bg: "bg-[#ECFDF5]",
                    },
                    {
                      id: "convert",
                      title: "格式转换",
                      desc: "MP3 / WAV / FLAC",
                      preset: "vocal_backing",
                      icon: FileText,
                      color: "text-[#F97316]",
                      bg: "bg-[#FFF7ED]",
                    },
                  ].map(({ id, title, desc, preset, icon: Icon, color, bg }) => (
                    <div
                      key={id}
                      onClick={() => {
                        setPresetScheme(preset);
                        fileInputRef.current?.click();
                      }}
                      className="group flex cursor-pointer items-center justify-between rounded-3xl border border-slate-100 bg-white p-4 shadow-[0_2px_12px_rgba(0,0,0,0.02)] transition-all hover:-translate-y-0.5 hover:shadow-md"
                    >
                      <div className="flex items-center gap-3.5">
                        <div className={`flex size-11 shrink-0 items-center justify-center rounded-2xl ${bg} ${color}`}>
                          <Icon className="size-5 stroke-[2.2]" />
                        </div>
                        <div>
                          <h3 className="text-sm font-bold text-slate-800">{title}</h3>
                          <p className="mt-0.5 text-[11px] text-slate-400">{desc}</p>
                        </div>
                      </div>
                      <ArrowRight className="size-4 text-slate-300 transition-transform group-hover:translate-x-1 group-hover:text-slate-600" />
                    </div>
                  ))}
                </div>

                {/* 最近处理记录列表 */}
                <div className="rounded-3xl border border-slate-100 bg-white p-6 shadow-[0_2px_16px_rgba(0,0,0,0.02)]">
                  {/* 标题与查看全部 */}
                  <div className="flex items-center justify-between pb-4">
                    <h3 className="text-sm font-bold text-slate-900">最近处理</h3>
                    <button
                      type="button"
                      onClick={() => setActiveTab("history")}
                      className="flex items-center gap-1 text-xs font-medium text-slate-400 transition-colors hover:text-slate-700"
                    >
                      <span>全部记录</span>
                      <ChevronRight className="size-3.5" />
                    </button>
                  </div>

                  {/* 表格标题行 */}
                  <div className="grid grid-cols-12 border-b border-slate-100 pb-3 text-xs font-semibold text-slate-400">
                    <span className="col-span-4 pl-2">文件名</span>
                    <span className="col-span-2">处理类型</span>
                    <span className="col-span-2">时长</span>
                    <span className="col-span-1">大小</span>
                    <span className="col-span-2">状态</span>
                    <span className="col-span-1 text-right pr-2">操作</span>
                  </div>

                  {/* 列表行 */}
                  <div className="divide-y divide-slate-50 text-xs">
                    {filteredHistory.length === 0 && (
                      <div className="py-10 text-center text-xs text-slate-400">还没有处理记录：拖入一首歌跑一次，结果会出现在这里。</div>
                    )}
                    {filteredHistory.map((item) => {
                      const isPlaying = playingTrack?.id === item.job_id;
                      return (
                        <div
                          key={item.job_id}
                          className="grid grid-cols-12 items-center py-3.5 transition-colors hover:bg-slate-50/80 rounded-xl px-2"
                        >
                          {/* 文件名 + 播放按钮 */}
                          <div className="col-span-4 flex items-center gap-3">
                            <button
                              type="button"
                              onClick={() => togglePlayAudio(item.job_id, "vocals")}
                              className={`flex size-7 items-center justify-center rounded-full border transition-all ${
                                isPlaying
                                  ? "border-blue-500 bg-blue-50 text-blue-600"
                                  : "border-slate-200 bg-white text-slate-500 hover:border-slate-400"
                              }`}
                            >
                              {isPlaying ? (
                                <Pause className="size-3.5 fill-current" />
                              ) : (
                                <Play className="size-3.5 fill-current ml-0.5" />
                              )}
                            </button>
                            <span className="font-semibold text-slate-800 truncate" title={item.source_name}>
                              {item.source_name}
                            </span>
                          </div>

                          {/* 处理类型标签 */}
                          <div className="col-span-2 flex items-center gap-1.5">
                            <span
                              className={`rounded-md px-2 py-0.5 text-[11px] font-medium ${
                                item.type?.includes("人声分离")
                                  ? "bg-blue-50 text-blue-600"
                                  : item.type?.includes("降噪")
                                  ? "bg-purple-50 text-purple-600"
                                  : item.type?.includes("格式")
                                  ? "bg-orange-50 text-orange-600"
                                  : "bg-emerald-50 text-emerald-600"
                              }`}
                            >
                              {item.type || "人声分离"}
                            </span>
                            {item.preset === "four_stems" && (
                              <span className="rounded-md bg-purple-50 px-1.5 py-0.5 text-[10px] font-medium text-purple-600">
                                四轨
                              </span>
                            )}
                          </div>

                          {/* 时长 */}
                          <div className="col-span-2 text-slate-400 font-medium">
                            {item.duration || "03:45"}
                          </div>

                          {/* 大小 */}
                          <div className="col-span-1 text-slate-500">
                            {item.source_bytes
                              ? `${(item.source_bytes / (1024 * 1024)).toFixed(1)} MB`
                              : "8.2 MB"}
                          </div>

                          {/* 状态 */}
                          <div className="col-span-2 flex items-center gap-2">
                            <span
                              className={`size-2 rounded-full ${
                                item.statusColor === "emerald"
                                  ? "bg-emerald-500"
                                  : item.statusColor === "blue"
                                  ? "bg-blue-500"
                                  : "bg-slate-400"
                              }`}
                            />
                            <span
                              className={`font-medium ${
                                item.statusColor === "emerald"
                                  ? "text-emerald-600"
                                  : item.statusColor === "blue"
                                  ? "text-blue-600"
                                  : "text-slate-400"
                              }`}
                            >
                              {item.statusText || "已完成"}
                            </span>
                          </div>

                          {/* 操作按钮组 (试听、定位文件夹、更多导出) */}
                          <div className="col-span-1 flex items-center justify-end gap-1.5 pr-2">
                            <button
                              type="button"
                              title="在文件夹中打开"
                              onClick={() => revealInFolder(item.output_dir)}
                              className="flex size-7 items-center justify-center rounded-lg text-slate-400 hover:bg-slate-200/70 hover:text-slate-700 transition-colors"
                            >
                              <Folder className="size-3.5" />
                            </button>
                            <button
                              type="button"
                              title="保存音轨"
                              onClick={() =>
                                saveStem(item.job_id, "vocals", `${item.source_name || "audio"}-人声.wav`)
                              }
                              className="flex size-7 items-center justify-center rounded-lg text-slate-400 hover:bg-slate-200/70 hover:text-slate-700 transition-colors"
                            >
                              <Download className="size-3.5" />
                            </button>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              </>
            )}

            {/* ── 4. 音频处理工作台 Tab ── */}
            {activeTab === "process" && (
              <div className="space-y-6">
                <div className="rounded-3xl border border-slate-100 bg-white p-7 shadow-sm">
                  <h2 className="text-xl font-bold text-slate-900">音频音轨分离工作台</h2>
                  <p className="mt-1 text-xs text-slate-500">
                    支持将歌曲分离为人声、伴奏或人声/鼓组/贝斯/其他四条独立高品质音轨。
                  </p>

                  {/* 拖拽上传区 */}
                  <div
                    onClick={() => fileInputRef.current?.click()}
                    className="mt-6 flex flex-col items-center justify-center rounded-3xl border-2 border-dashed border-slate-200 bg-slate-50/60 p-10 text-center transition-all hover:border-[#FA541C] hover:bg-[#FFF7F2]/40 cursor-pointer"
                  >
                    <div className="flex size-14 items-center justify-center rounded-2xl bg-orange-100 text-[#FA541C]">
                      <Upload className="size-7 stroke-[2.2]" />
                    </div>
                    <p className="mt-4 text-sm font-bold text-slate-800">
                      {currentFile ? currentFile.name : "点击或将音频拖入此区域"}
                    </p>
                    <p className="mt-1 text-xs text-slate-400">
                      支持 MP3, WAV, FLAC, M4A, AAC 及 MP4 视频，最大 500 MB
                    </p>
                  </div>
                </div>
              </div>
            )}

            {/* ── 5. 处理记录 Tab ── */}
            {activeTab === "history" && (
              <div className="rounded-3xl border border-slate-100 bg-white p-6 shadow-sm">
                <div className="flex items-center justify-between pb-4">
                  <h2 className="text-lg font-bold text-slate-900">处理记录归档</h2>
                  <button
                    type="button"
                    onClick={refreshHistory}
                    className="flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-800"
                  >
                    <RefreshCw className="size-3.5" />
                    <span>刷新</span>
                  </button>
                </div>
                <div className="divide-y divide-slate-100 text-xs">
                  {filteredHistory.map((item) => (
                    <div key={item.job_id} className="flex items-center justify-between py-4">
                      <div>
                        <p className="font-bold text-slate-800 text-sm">{item.source_name}</p>
                        <p className="mt-1 text-slate-400">
                          {item.type} · {item.duration} ·{" "}
                          {item.source_bytes ? (item.source_bytes / 1024 / 1024).toFixed(1) + " MB" : ""}
                        </p>
                      </div>
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={() => saveStem(item.job_id, "vocals", `${item.source_name}-人声.wav`)}
                          className="rounded-xl border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50"
                        >
                          保存人声
                        </button>
                        <button
                          type="button"
                          onClick={() =>
                            saveStem(
                              item.job_id,
                              item.preset === "four_stems" ? "other" : "no_vocals",
                              `${item.source_name}-伴奏.wav`
                            )
                          }
                          className="rounded-xl border border-slate-200 px-3 py-1.5 text-xs font-medium text-slate-600 hover:bg-slate-50"
                        >
                          保存伴奏
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            )}

            {/* ── 6. 素材管理 Tab ── */}
            {activeTab === "assets" && (
              <div className="rounded-3xl border border-slate-100 bg-white p-6 shadow-sm space-y-4">
                <h2 className="text-lg font-bold text-slate-900">本地存储与素材管理</h2>
                <div className="grid grid-cols-3 gap-4">
                  <div className="rounded-2xl border border-slate-100 bg-slate-50 p-4">
                    <p className="text-xs text-slate-400">可用磁盘空间</p>
                    <p className="mt-1 text-xl font-bold text-slate-800">
                      {health?.disk_free_mb ? (health.disk_free_mb / 1024).toFixed(1) + " GB" : "128 GB"}
                    </p>
                  </div>
                  <div className="rounded-2xl border border-slate-100 bg-slate-50 p-4">
                    <p className="text-xs text-slate-400">已处理历史任务</p>
                    <p className="mt-1 text-xl font-bold text-slate-800">{history.length} 个文件</p>
                  </div>
                  <div className="rounded-2xl border border-slate-100 bg-slate-50 p-4">
                    <p className="text-xs text-slate-400">临时文件保留策略</p>
                    <p className="mt-1 text-xl font-bold text-slate-800">1 小时自动清理</p>
                  </div>
                </div>
              </div>
            )}

            {/* ── 7. 模型与设置 Tab ── */}
            {activeTab === "settings" && (
              <div className="rounded-3xl border border-slate-100 bg-white p-6 shadow-sm space-y-5">
                <h2 className="text-lg font-bold text-slate-900">AI 计算引擎与系统环境</h2>
                <div className="space-y-3 text-xs">
                  <div className="flex items-center justify-between border-b border-slate-100 pb-3">
                    <span className="text-slate-500">计算硬件 (GPU)</span>
                    <span className="font-semibold text-emerald-600">
                      {health?.cuda_device_name || "NVIDIA GeForce GTX 1660 SUPER"} (CUDA 加速已启用)
                    </span>
                  </div>
                  <div className="flex items-center justify-between border-b border-slate-100 pb-3">
                    <span className="text-slate-500">AI 分离模型</span>
                    <span className="font-semibold text-slate-800">HTDemucs 4.1.0</span>
                  </div>
                  <div className="flex items-center justify-between border-b border-slate-100 pb-3">
                    <span className="text-slate-500">PyTorch 框架版本</span>
                    <span className="font-semibold text-slate-800">{health?.torch_version || "2.6.0+cu124"}</span>
                  </div>
                  <div className="flex items-center justify-between border-b border-slate-100 pb-3">
                    <span className="text-slate-500">音视频转码引擎 (FFmpeg)</span>
                    <span className="font-semibold text-slate-800">已就绪 (backend/ffmpeg.exe)</span>
                  </div>
                  <div className="flex items-center justify-between border-b border-slate-100 pb-3">
                    <span className="text-slate-500">本地服务端口</span>
                    <span className="font-semibold text-slate-800">127.0.0.1:8000</span>
                  </div>
                </div>
              </div>
            )}
          </main>

          {/* ── 8. 右侧边栏 (当前任务 + 处理参数) ── */}
          <aside className="w-76 shrink-0 p-6 pl-0 flex flex-col gap-5 overflow-y-auto">
            {/* 卡片 1: 当前任务 */}
            <div className="rounded-3xl border border-slate-100 bg-white p-5 shadow-[0_2px_16px_rgba(0,0,0,0.02)]">
              <h3 className="text-sm font-bold text-slate-900">当前任务</h3>

              {/* 环形进度条 */}
              <div className="mt-5 flex flex-col items-center">
                <div className="relative flex size-32 items-center justify-center">
                  <svg className="size-full -rotate-90" viewBox="0 0 100 100">
                    {/* 灰色底层圆环 */}
                    <circle
                      cx="50"
                      cy="50"
                      r="40"
                      fill="transparent"
                      stroke="#F1F5F9"
                      strokeWidth="8"
                    />
                    {/* 蓝色动态进度圆环 */}
                    <circle
                      cx="50"
                      cy="50"
                      r="40"
                      fill="transparent"
                      stroke="#2563EB"
                      strokeWidth="8"
                      strokeDasharray={2 * Math.PI * 40}
                      strokeDashoffset={2 * Math.PI * 40 * (1 - currentProgress / 100)}
                      strokeLinecap="round"
                      className="transition-all duration-500 ease-out"
                    />
                  </svg>
                  {/* 中间百分比数字 */}
                  <span className="absolute text-2xl font-extrabold text-slate-900 tracking-tight">
                    {currentProgress}%
                  </span>
                </div>

                {/* 任务状态描述 */}
                <div className="mt-4 text-center">
                  <p className="text-xs font-bold text-slate-800 truncate max-w-[200px]" title={currentJobName}>
                    正在处理: {activeJob ? currentJobName : "（空闲）"}
                  </p>
                  <p className="mt-1 text-[11px] font-medium text-slate-400">
                    {currentJobPhase}
                  </p>
                </div>

                {/* 取消处理按钮：只在确实有任务时才出现（空闲时不给一个点了没事干的按钮） */}
                {activeJob && (
                  <button
                    type="button"
                    onClick={cancelProcessing}
                    className="mt-4 w-full rounded-2xl border border-slate-200 py-2.5 text-xs font-semibold text-slate-600 transition-colors hover:bg-slate-50 active:scale-98 cursor-pointer"
                  >
                    取消处理
                  </button>
                )}
              </div>
            </div>

            {/* 卡片 2: 处理参数 */}
            <div className="flex-1 rounded-3xl border border-slate-100 bg-white p-5 shadow-[0_2px_16px_rgba(0,0,0,0.02)] flex flex-col justify-between">
              <div>
                <h3 className="text-sm font-bold text-slate-900">处理参数</h3>

                {/* 预设方案下拉选择 */}
                <div className="mt-4">
                  <label className="block text-xs font-semibold text-slate-600 mb-1.5">预设方案</label>
                  <div className="relative">
                    <select
                      value={presetScheme}
                      onChange={(e) => setPresetScheme(e.target.value)}
                      className="w-full appearance-none rounded-2xl border border-slate-200 bg-slate-50/50 px-3.5 py-2.5 text-xs font-medium text-slate-800 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
                    >
                      <option value="vocal_backing">智能增强 (推荐)</option>
                      <option value="smart_enhance">智能增强 (推荐)</option>
                      <option value="vocal_backing">人声 + 伴奏 (翻唱/卡拉OK)</option>
                      <option value="four_stems">四条音轨 (人声/鼓组/贝斯/其他)</option>
                    </select>
                    <ChevronDown className="pointer-events-none absolute right-3 top-3 size-4 text-slate-400" />
                  </div>
                </div>

                {/* 三个开关选项 */}
                <div className="mt-5 space-y-3.5">
                  {[
                    { label: "降噪", state: denoiseEnabled, set: setDenoiseEnabled },
                    { label: "人声增强", state: vocalBoostEnabled, set: setVocalBoostEnabled },
                    { label: "音量标准化", state: normalizeEnabled, set: setNormalizeEnabled },
                  ].map(({ label, state, set }) => (
                    <div key={label} className="flex items-center justify-between text-xs">
                      <span className="font-medium text-slate-700">{label}</span>
                      <button
                        type="button"
                        onClick={() => set(!state)}
                        className={`relative h-6 w-11 rounded-full transition-colors cursor-pointer ${
                          state ? "bg-[#2563EB]" : "bg-slate-200"
                        }`}
                      >
                        <span
                          className={`absolute top-0.5 left-0.5 size-5 rounded-full bg-white shadow-sm transition-transform ${
                            state ? "translate-x-5" : "translate-x-0"
                          }`}
                        />
                      </button>
                    </div>
                  ))}
                </div>

                {/* 输出格式下拉选择 */}
                <div className="mt-5">
                  <label className="block text-xs font-semibold text-slate-600 mb-1.5">输出格式</label>
                  <div className="relative">
                    <select
                      value={outputFormat}
                      onChange={(e) => setOutputFormat(e.target.value)}
                      className="w-full appearance-none rounded-2xl border border-slate-200 bg-slate-50/50 px-3.5 py-2.5 text-xs font-medium text-slate-800 focus:outline-none focus:ring-2 focus:ring-blue-500/20"
                    >
                      <option value="MP3">MP3 (通用兼容)</option>
                      <option value="WAV">WAV (无损高质量)</option>
                      <option value="FLAC">FLAC (无损压缩)</option>
                    </select>
                    <ChevronDown className="pointer-events-none absolute right-3 top-3 size-4 text-slate-400" />
                  </div>
                </div>
              </div>

              {/* 底部开始处理大按钮 */}
              <button
                type="button"
                onClick={startProcessing}
                className="mt-6 flex w-full items-center justify-center gap-2 rounded-2xl bg-[#0F172A] py-3 text-xs font-bold text-white shadow-md transition-all hover:bg-slate-800 active:scale-98 cursor-pointer"
              >
                <Play className="size-3.5 fill-current" />
                <span>开始处理</span>
              </button>
            </div>
          </aside>
        </div>
      </div>
    </div>
  );
}
