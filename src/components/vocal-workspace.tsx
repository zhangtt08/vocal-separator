"use client";

import {
  AudioLines,
  Check,
  CircleCheck,
  Clock3,
  Cpu,
  Download,
  Drum,
  FileAudio,
  FolderOpen,
  Guitar,
  Headphones,
  History,
  LoaderCircle,
  Mic2,
  Music2,
  RefreshCw,
  TriangleAlert,
  Upload,
  X,
  type LucideIcon,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Progress,
  ProgressLabel,
  ProgressValue,
} from "@/components/ui/progress";
import { cn } from "@/lib/utils";
import { WindowControls } from "@/components/TitleBar";

const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE_URL?.replace(/\/$/, "") ||
  (process.env.NODE_ENV === "production" ? "" : "http://localhost:8000");
const MAX_FILE_SIZE = 500 * 1024 * 1024;
const ALLOWED_EXTENSIONS = new Set([
  "mp3",
  "wav",
  "flac",
  "ogg",
  "m4a",
  "aac",
  "mp4",
  "mov",
  "mkv",
  "webm",
  "avi",
]);
const POLL_INTERVAL = 1000;

type ServiceState = "checking" | "online" | "offline" | "missing";
type ItemState =
  | "confirm"
  | "waiting"
  | "uploading"
  | "processing"
  | "done"
  | "error"
  | "cancelled";

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

interface PresetStem {
  name: string;
  label_zh: string;
  label_en: string;
}

interface PresetInfo {
  preset: string;
  label: string;
  description: string;
  is_default?: boolean;
  stems: PresetStem[];
}

interface EnvIssue {
  key: string;
  label: string;
  severity: "blocking" | "warning";
  detail?: string | null;
  command?: string | null;
}

interface HealthInfo {
  status?: string;
  demucs_available?: boolean;
  ffmpeg_available?: boolean;
  gpu_available?: boolean;
  cuda_available?: boolean;
  cuda_device_name?: string | null;
  cuda_note?: string;
  torch_version?: string | null;
  demucs_version?: string | null;
  ffmpeg_path?: string | null;
  python_version?: string | null;
  active_jobs?: number;
  version?: string;
  model?: string;
  presets?: PresetInfo[];
  issues?: EnvIssue[];
  can_separate?: boolean;
  model_cache?: {
    cached?: boolean;
    size_mb?: number;
    note?: string | null;
  };
}

interface HistoryStem {
  name: string;
  label?: string;
  path?: string;
  bytes?: number;
  exists?: boolean;
}

interface HistoryEntry {
  job_id: string;
  preset?: string;
  source_name?: string;
  source_bytes?: number;
  created_at?: number;
  finished_at?: number;
  output_dir?: string;
  stems: HistoryStem[];
  available_stems?: number;
  total_bytes?: number;
  expired?: boolean;
}

interface QueueItem {
  key: string;
  file: File | null;
  name: string;
  size: number;
  state: ItemState;
  uploadPct: number;
  job?: JobSnapshot;
  error?: string | null;
  duplicateOf?: string;
}

interface StemMeta {
  label: string;
  icon: LucideIcon;
}

const STEM_META: Record<string, StemMeta> = {
  vocals: { label: "人声", icon: Mic2 },
  drums: { label: "鼓组", icon: Drum },
  bass: { label: "贝斯", icon: Guitar },
  other: { label: "其他乐器", icon: Music2 },
  no_vocals: { label: "伴奏", icon: Headphones },
};
const STEM_ORDER = ["vocals", "no_vocals", "drums", "bass", "other"];

/** 桌面壳（electron/preload.cjs）才有；浏览器模式返回 null，导出退回默认下载目录。 */
interface DesktopShell {
  saveStem?: (payload: { jobId: string; stem: string; suggestedName: string }) => Promise<{
    saved: boolean;
    path?: string;
    bytes?: number;
    reason?: string;
  }>;
  revealPath?: (targetPath: string) => Promise<boolean>;
}

function getExtension(fileName: string) {
  return fileName.split(".").pop()?.toLowerCase() || "";
}

function validateFile(file: File) {
  if (!ALLOWED_EXTENSIONS.has(getExtension(file.name))) {
    return "不支持这个格式，请选择音频文件或 MP4、MOV、MKV、WebM、AVI 视频。";
  }
  if (file.size === 0) {
    return "这个文件是空的，请重新选择。";
  }
  if (file.size > MAX_FILE_SIZE) {
    return "文件超过 500 MB，请压缩后再试。";
  }
  return null;
}

function formatSize(bytes?: number) {
  if (!bytes && bytes !== 0) return "大小未知";
  const megabytes = bytes / (1024 * 1024);
  return `${megabytes.toFixed(1)} MB`;
}

function formatDuration(seconds?: number | null) {
  if (seconds === null || seconds === undefined) return "--";
  const total = Math.max(0, Math.round(seconds));
  if (total < 60) return `${total} 秒`;
  return `${Math.floor(total / 60)} 分 ${String(total % 60).padStart(2, "0")} 秒`;
}

function formatClock(unixSeconds?: number | null) {
  if (!unixSeconds) return "";
  return new Date(unixSeconds * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function delay(milliseconds: number) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

/** 把后端的真实报错翻译成"下一步该做什么"，找不到对应原因就原样给出后端信息。 */
function remedyFor(message?: string | null): string | null {
  if (!message) return null;
  if (message.includes("No module named") && message.includes("demucs")) {
    return "后端用的那个 Python 里没装 Demucs：在本机执行 python -m pip install -r backend/requirements.txt";
  }
  if (message.includes("未找到 ffmpeg")) {
    return "装 ffmpeg（或把 ffmpeg.exe 放进 backend 目录），也可以直接上传音频格式避开转码。";
  }
  if (message.toLowerCase().includes("cuda out of memory")) {
    return "显卡显存不够：先关掉其它占用显卡的程序，或改用 CPU（慢很多）后重试。";
  }
  if (message.includes("torch") || message.toLowerCase().includes("cuda")) {
    return "PyTorch/CUDA 没就绪：python -m pip install -r backend/requirements.txt 里锁了 CUDA 12.4 版轮子。";
  }
  if (message.includes("任务不存在或已过期")) {
    return "结果只保留 1 小时，超时后会自动清理，需要重新分离。";
  }
  return null;
}

function friendlyError(error: unknown, fallback: string) {
  if (!(error instanceof Error)) return fallback;
  if (
    error.message.includes("Failed to fetch") ||
    error.message.includes("NetworkError") ||
    error.message.includes("Load failed")
  ) {
    return "无法连接本地 AI 服务，请重新检测后再试。";
  }
  return error.message || fallback;
}

async function getErrorMessage(response: Response) {
  try {
    const body = (await response.json()) as { detail?: string };
    if (body.detail) return body.detail;
  } catch {
    // 非 JSON 错误响应使用下方的兜底文案。
  }
  return `服务请求失败（${response.status}）`;
}

function makeKey() {
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

/** fetch 拿不到上传进度（大文件要等几十秒没有反馈），所以上传用 XHR。 */
function uploadForSeparation(
  file: File,
  preset: string,
  onProgress: (percent: number) => void,
): Promise<{ job: JobSnapshot; controller: AbortController }> {
  return new Promise((resolve, reject) => {
    const controller = new AbortController();
    const formData = new FormData();
    formData.append("file", file, file.name);
    formData.append("preset", preset);

    const request = new XMLHttpRequest();
    request.open("POST", `${API_BASE}/api/separate`);
    request.responseType = "text";
    request.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.min(99, Math.round((event.loaded / event.total) * 100)));
      }
    };
    request.onabort = () => reject(new DOMException("已取消", "AbortError"));
    request.onerror = () => reject(new Error("无法连接本地 AI 服务"));
    request.onload = () => {
      if (controller.signal.aborted) {
        reject(new DOMException("已取消", "AbortError"));
        return;
      }
      let parsed: JobSnapshot | { detail?: string } = {};
      try {
        parsed = JSON.parse(request.responseText || "{}") as JobSnapshot;
      } catch {
        reject(new Error(`服务返回了无法解析的内容（${request.status}）`));
        return;
      }
      if (request.status < 200 || request.status >= 300) {
        reject(new Error((parsed as { detail?: string }).detail || `服务请求失败（${request.status}）`));
        return;
      }
      const job = parsed as JobSnapshot;
      if (!job.job_id) {
        reject(new Error("服务未返回有效任务编号。"));
        return;
      }
      onProgress(100);
      resolve({ job, controller });
    };
    controller.signal.addEventListener("abort", () => request.abort());
    request.send(formData);
  });
}

export function VocalWorkspace() {
  const [items, setItems] = useState<QueueItem[]>([]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [presetChoice, setPresetChoice] = useState<string | null>(null);
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [serviceState, setServiceState] = useState<ServiceState>("checking");
  const [history, setHistory] = useState<HistoryEntry[]>([]);
  // 历史默认摊开：首屏就该看得到"本机有什么结果"，而不是先猜再点一个折叠按钮。
  const [historyOpen, setHistoryOpen] = useState(true);
  const [saving, setSaving] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ text: string; tone: "ok" | "warn" } | null>(null);

  const itemsRef = useRef<QueueItem[]>([]);
  const runningRef = useRef(false);
  const activeJobRef = useRef<string | null>(null);
  const abandonRef = useRef<Set<string>>(new Set());
  const serviceControllerRef = useRef<AbortController | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const presetRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const dragDepthRef = useRef(0);
  const [isDragging, setIsDragging] = useState(false);
  const mountedRef = useRef(true);

  // 预设标签与轨名全部来自后端 /api/health 的 data.presets（backend.preset_descriptors()）；
  // 这里只做"没选过就用后端默认"的推导，不在 effect 里回写 state。
  const presets: PresetInfo[] = health?.presets || [];
  const preset =
    presetChoice && presets.some((entry) => entry.preset === presetChoice)
      ? presetChoice
      : presets[0]?.preset || "four_stems";

  useEffect(() => {
    itemsRef.current = items;
  }, [items]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      serviceControllerRef.current?.abort();
    };
  }, []);

  const patch = useCallback((key: string, changes: Partial<QueueItem>) => {
    setItems((current) =>
      current.map((item) => (item.key === key ? { ...item, ...changes } : item)),
    );
  }, []);

  const loadHistory = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/api/history?limit=60`, { cache: "no-store" });
      if (!response.ok) return;
      const body = (await response.json()) as { items?: HistoryEntry[] };
      if (mountedRef.current) setHistory(body.items || []);
    } catch {
      // 服务没起来时历史列表留空即可，不额外打扰用户。
    }
  }, []);

  const checkService = useCallback(async () => {
    serviceControllerRef.current?.abort();
    const controller = new AbortController();
    serviceControllerRef.current = controller;
    setServiceState("checking");

    try {
      const response = await fetch(`${API_BASE}/api/health`, {
        cache: "no-store",
        signal: controller.signal,
      });
      if (!response.ok) {
        setServiceState("offline");
        return;
      }
      const payload = (await response.json()) as HealthInfo & { data?: HealthInfo };
      // /api/health 同时带旧键（Electron 探针读的那几个）与标准信封 data。
      const merged: HealthInfo = { ...(payload.data || {}), ...payload };
      setHealth(merged);
      setServiceState(merged.demucs_available ? "online" : "missing");
      if (merged.demucs_available) void loadHistory();
    } catch {
      if (!controller.signal.aborted) setServiceState("offline");
    }
  }, [loadHistory]);

  useEffect(() => {
    const timer = window.setTimeout(() => void checkService(), 0);
    return () => window.clearTimeout(timer);
  }, [checkService]);

  const pollJob = useCallback(
    async (key: string, jobId: string) => {
      let failures = 0;
      for (;;) {
        if (!mountedRef.current) return null;
        if (abandonRef.current.has(key)) {
          activeJobRef.current = null;
          return null;
        }
        await delay(POLL_INTERVAL);
        try {
          const response = await fetch(`${API_BASE}/api/jobs/${jobId}`, { cache: "no-store" });
          if (!response.ok) throw new Error(await getErrorMessage(response));
          const job = (await response.json()) as JobSnapshot;
          failures = 0;
          patch(key, { job });

          if (job.status === "done") {
            activeJobRef.current = null;
            void loadHistory();
            return job;
          }
          if (job.status === "error") {
            activeJobRef.current = null;
            patch(key, {
              state: "error",
              error: job.error || "音轨分离失败，请重试。",
            });
            return null;
          }
          if (job.status === "cancelled") {
            activeJobRef.current = null;
            patch(key, { state: "cancelled", job });
            return null;
          }
        } catch (pollError) {
          failures += 1;
          if (failures < 3) continue;
          activeJobRef.current = null;
          patch(key, {
            state: "error",
            error: friendlyError(pollError, "读取任务进度失败，请稍后重试。"),
          });
          return null;
        }
      }
    },
    [loadHistory, patch],
  );

  const runItem = useCallback(
    async (key: string) => {
      const item = itemsRef.current.find((entry) => entry.key === key);
      const file = item?.file;
      if (!file) return;

      setSelectedKey(key);
      patch(key, { state: "uploading", uploadPct: 0, error: null });
      try {
        const { job } = await uploadForSeparation(file, preset, (percent) =>
          patch(key, { uploadPct: percent }),
        );
        if (abandonRef.current.has(key)) {
          abandonRef.current.delete(key);
          activeJobRef.current = job.job_id;
          await fetch(`${API_BASE}/api/jobs/${job.job_id}/cancel`, { method: "POST" }).catch(() => null);
          patch(key, { state: "cancelled" });
          return;
        }
        activeJobRef.current = job.job_id;
        patch(key, { state: "processing", job });
        await pollJob(key, job.job_id);
      } catch (submitError) {
        activeJobRef.current = null;
        if (submitError instanceof DOMException && submitError.name === "AbortError") {
          patch(key, { state: "cancelled" });
          return;
        }
        const message = friendlyError(submitError, "无法连接本地 AI 服务，请重新检测后再试。");
        patch(key, { state: "error", error: message });
        if (message.includes("连接") || message.includes("fetch")) setServiceState("offline");
      }
    },
    [patch, pollJob, preset],
  );

  const pump = useCallback(async () => {
    if (runningRef.current || !mountedRef.current) return;
    const next = itemsRef.current.find((entry) => entry.state === "waiting");
    if (!next) return;
    runningRef.current = true;
    setItems((current) =>
      current.map((entry) =>
        entry.key === next.key ? { ...entry, state: "uploading" as ItemState } : entry,
      ),
    );
    try {
      await runItem(next.key);
    } finally {
      runningRef.current = false;
    }
    if (mountedRef.current) void pump();
  }, [runItem]);

  // items 变化后驱动队列：任何一行变成 waiting 就顺着往下跑。
  useEffect(() => {
    if (items.some((entry) => entry.state === "waiting")) void pump();
  }, [items, pump]);

  const enqueue = useCallback(
    (files: File[]) => {
      const accepted: QueueItem[] = [];
      const rejected: QueueItem[] = [];

      for (const file of files) {
        const key = makeKey();
        const validation = validateFile(file);
        const duplicate = validation
          ? undefined
          : history.find(
              (entry) =>
                (entry.source_name || "").toLowerCase() === file.name.toLowerCase() &&
                entry.source_bytes === file.size &&
                (entry.available_stems || 0) > 0,
            );
        if (validation) {
          rejected.push({
            key,
            file: null,
            name: file.name,
            size: file.size,
            state: "error",
            uploadPct: 0,
            error: validation,
          });
          continue;
        }
        accepted.push({
          key,
          file,
          name: file.name,
          size: file.size,
          state: duplicate ? "confirm" : "waiting",
          uploadPct: 0,
          duplicateOf: duplicate?.job_id,
          error: duplicate
            ? `${formatClock(duplicate.finished_at || duplicate.created_at)} 已经分离过，${duplicate.available_stems} 条音轨还在本机。`
            : null,
        });
      }

      if (accepted.length) setSelectedKey(accepted[accepted.length - 1].key);
      setItems((current) => [...current, ...rejected, ...accepted]);
    },
    [history],
  );

  const handleInputChange = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const files = Array.from(event.currentTarget.files || []);
      event.currentTarget.value = "";
      if (files.length) enqueue(files);
    },
    [enqueue],
  );

  const handleDragEnter = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    if (serviceState !== "online") return;
    dragDepthRef.current += 1;
    setIsDragging(true);
  }, [serviceState]);

  const handleDragLeave = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setIsDragging(false);
  }, []);

  const handleDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      dragDepthRef.current = 0;
      setIsDragging(false);
      if (serviceState !== "online") {
        setNotice({ text: "本地 AI 服务尚未就绪，请先看上面的提示。", tone: "warn" });
        return;
      }
      const files = Array.from(event.dataTransfer.files || []);
      if (files.length) enqueue(files);
    },
    [enqueue, serviceState],
  );

  /** 从队列里划掉一行。正在跑的那首必须同时通知后端取消，否则它会继续在背后占显卡。 */
  const removeItem = useCallback(
    (key: string) => {
      const item = itemsRef.current.find((entry) => entry.key === key);
      abandonRef.current.add(key);
      setItems((current) => current.filter((entry) => entry.key !== key));
      setSelectedKey((current) => (current === key ? null : current));
      const jobId = item?.job?.job_id;
      if (jobId && item && (item.state === "processing" || item.state === "uploading")) {
        void fetch(`${API_BASE}/api/jobs/${jobId}/cancel`, { method: "POST" }).catch(() => null);
      }
    },
    [],
  );

  const retryItem = useCallback(
    (key: string) => {
      abandonRef.current.delete(key);
      patch(key, { state: "waiting", error: null, uploadPct: 0, job: undefined });
    },
    [patch],
  );

  const cancelItem = useCallback(
    async (key: string, jobId?: string) => {
      abandonRef.current.add(key);
      patch(key, { state: "cancelled" });
      if (!jobId) return;
      try {
        const response = await fetch(`${API_BASE}/api/jobs/${jobId}/cancel`, { method: "POST" });
        if (!response.ok && response.status !== 409 && response.status !== 404) {
          throw new Error(await getErrorMessage(response));
        }
      } catch (cancelError) {
        patch(key, { error: friendlyError(cancelError, "任务取消失败，请稍后重试。") });
      }
    },
    [patch],
  );

  const startDuplicate = useCallback(
    (key: string) => {
      abandonRef.current.delete(key);
      patch(key, { state: "waiting", error: null, duplicateOf: undefined });
    },
    [patch],
  );

  const loadHistoryEntry = useCallback(async (entry: HistoryEntry) => {
    try {
      const response = await fetch(`${API_BASE}/api/jobs/${entry.job_id}`, { cache: "no-store" });
      if (!response.ok) throw new Error(await getErrorMessage(response));
      const job = (await response.json()) as JobSnapshot;
      const key = makeKey();
      setItems((current) => [
        ...current,
        {
          key,
          file: null,
          name: job.source_name || entry.source_name || "已分离结果",
          size: entry.source_bytes || 0,
          state: "done",
          uploadPct: 100,
          job: { ...job, source_name: job.source_name || entry.source_name },
        },
      ]);
      setSelectedKey(key);
    } catch (loadError) {
      setNotice({
        text: `这条结果的任务记录已经不在了（${friendlyError(loadError, "任务不存在或已过期")}），文件路径：${entry.output_dir || "未知"}`,
        tone: "warn",
      });
    }
  }, []);

  const exportStem = useCallback(
    async (
      jobId: string,
      stemKey: string,
      label: string,
      sourceName: string | null,
      viaDialog: boolean,
    ) => {
      const baseName = (sourceName || "声析音轨").replace(/\.[^.]+$/, "");
      const suggestedName = `${baseName}-${stemKey}.wav`;
      const shell = shellApi();

      if (viaDialog && shell?.saveStem) {
        setSaving(stemKey);
        setNotice(null);
        const result = await shell.saveStem({ jobId, stem: stemKey, suggestedName });
        setSaving(null);
        if (result.saved && result.path) {
          setNotice({ text: `已保存：${result.path}（${formatSize(result.bytes)}）`, tone: "ok" });
        } else if (result.reason && result.reason !== "cancelled") {
          setNotice({ text: result.reason, tone: "warn" });
        }
        return;
      }

      setSaving(stemKey);
      setNotice(null);
      try {
        const response = await fetch(
          `${API_BASE}/api/download/${jobId}/${encodeURIComponent(stemKey)}`,
        );
        if (!response.ok) throw new Error(await getErrorMessage(response));
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = `${baseName}-${label}.wav`;
        document.body.append(anchor);
        anchor.click();
        anchor.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 0);
      } catch (downloadError) {
        setNotice({
          text: friendlyError(downloadError, "下载失败，请稍后重试。"),
          tone: "warn",
        });
      } finally {
        setSaving(null);
      }
    },
    [],
  );

  const activeItem = items.find(
    (entry) => entry.state === "uploading" || entry.state === "processing",
  );
  const lastDone = [...items].reverse().find((entry) => entry.state === "done");
  const shown =
    items.find((entry) => entry.key === selectedKey) || activeItem || lastDone || null;
  const shownJob = shown?.job;
  const shownSource = shown?.name || shownJob?.source_name || null;
  const busyItem = activeItem || null;
  const queueIsFull = items.length > 0;
  const visibleStems = (() => {
    const stems = shownJob?.stems;
    if (!stems || shown?.state !== "done") return [];
    const keys = Object.keys(stems).sort(
      (a, b) => (STEM_ORDER.indexOf(a) + 1 || 99) - (STEM_ORDER.indexOf(b) + 1 || 99),
    );
    return keys.map((key) => [key, stems[key]] as const);
  })();

  const isDesktop = !!shellApi()?.saveStem;
  const issues = health?.issues || [];
  const environment = health
    ? [
        health.demucs_version ? `Demucs ${health.demucs_version}` : "Demucs 版本未知",
        health.torch_version ? `torch ${health.torch_version}` : null,
        health.cuda_available
          ? health.cuda_device_name || "GPU 可用"
          : "CPU 运行（较慢）",
        health.ffmpeg_available ? "ffmpeg 可用" : "ffmpeg 缺失（视频读不了）",
        health.model_cache?.cached
          ? `模型权重已缓存 ${formatSize((health.model_cache.size_mb || 0) * 1024 * 1024)}`
          : "模型权重未缓存（首次分离会先下载）",
      ].filter(Boolean)
    : [];

  return (
    <section
      className="workspace-region order-1 lg:order-2"
      aria-label="音轨分离工作台"
    >
      <Card className="workspace-card">
        <CardHeader className="workspace-header drag-region flex w-full flex-col items-start justify-between gap-3 sm:flex-row sm:items-center">
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <CardTitle as="h2" className="text-lg">新建分离任务</CardTitle>
            <CardDescription>
              可一次拖入多首，本机按顺序处理，输出 WAV 音轨。
            </CardDescription>
          </div>
          <WindowControls />
          <button
            type="button"
            className="service-status"
            data-state={serviceState}
            onClick={() => void checkService()}
            disabled={serviceState === "checking"}
            aria-label={
              serviceState === "online"
                ? "本地 AI 服务已就绪，点击重新检测"
                : "重新检测本地 AI 服务"
            }
          >
            {serviceState === "checking" && (
              <LoaderCircle className="service-spinner" aria-hidden="true" />
            )}
            {serviceState === "online" && <CircleCheck aria-hidden="true" />}
            {serviceState === "offline" && <TriangleAlert aria-hidden="true" />}
            {serviceState === "missing" && <Cpu aria-hidden="true" />}
            {serviceState === "checking" && "正在检测"}
            {serviceState === "online" && "服务已就绪"}
            {serviceState === "offline" && "服务未连接"}
            {serviceState === "missing" && "模型未安装"}
          </button>
          <span className="sr-only" role="status" aria-live="polite">
            {serviceState === "checking" && "正在检测本地 AI 服务"}
            {serviceState === "online" && "本地 AI 服务已就绪"}
            {serviceState === "offline" && "本地 AI 服务未连接"}
            {serviceState === "missing" && "未检测到音轨分离模型"}
          </span>
        </CardHeader>

        <CardContent className="flex flex-col gap-4 py-0">
          {serviceState === "online" && environment.length > 0 && (
            <p className="env-strip">
              <Cpu aria-hidden="true" />
              <span>{environment.join(" · ")}</span>
              {typeof health?.active_jobs === "number" && health.active_jobs > 0 && (
                <span className="env-strip-extra">
                  （{health.active_jobs} 个任务在跑）
                </span>
              )}
            </p>
          )}

          {serviceState === "online" && issues.length > 0 && (
            <ul className="env-issues" aria-label="本机环境待办">
              {issues.map((issue) => (
                <li key={issue.key} data-severity={issue.severity}>
                  <span className="env-issue-head">
                    {issue.severity === "blocking" ? (
                      <TriangleAlert aria-hidden="true" />
                    ) : (
                      <Clock3 aria-hidden="true" />
                    )}
                    <span>{issue.label}</span>
                  </span>
                  {issue.detail && <span className="env-issue-detail">{issue.detail}</span>}
                  {issue.command && <code className="command-text">{issue.command}</code>}
                </li>
              ))}
            </ul>
          )}

          {serviceState !== "online" && (
            <div className="service-notice" data-state={serviceState} role="status">
              <span className="flex min-w-0 flex-col gap-1">
                <span className="flex items-start gap-2">
                  {serviceState === "checking" ? (
                    <LoaderCircle className="service-spinner" aria-hidden="true" />
                  ) : serviceState === "missing" ? (
                    <Cpu aria-hidden="true" />
                  ) : (
                    <TriangleAlert aria-hidden="true" />
                  )}
                  <span>
                    {serviceState === "checking" && "正在启动本地 AI 服务，请稍候。"}
                    {serviceState === "offline" && "本地 AI 服务未连接，界面无法提交任务。"}
                    {serviceState === "missing" && "这个 Python 环境里没有 Demucs，界面只能检测不能分离。"}
                  </span>
                </span>
                {serviceState === "offline" && (
                  <code className="command-text">
                    {isDesktop ? "桌面版会自动启动后端；浏览器模式请双击 start.bat 或执行 python backend/main.py" : "python backend/main.py"}
                  </code>
                )}
                {serviceState === "missing" && (
                  <code className="command-text">python -m pip install -r backend/requirements.txt</code>
                )}
              </span>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={serviceState === "checking"}
                onClick={() => void checkService()}
              >
                <RefreshCw data-icon="inline-start" />
                重新检测
              </Button>
            </div>
          )}

          {presets.length > 0 && (
            <div className="preset-picker" role="radiogroup" aria-label="音轨预设">
              {presets.map((entry, index) => (
                <button
                  key={entry.preset}
                  type="button"
                  role="radio"
                  aria-checked={preset === entry.preset}
                  data-state={preset === entry.preset ? "on" : "off"}
                  className="preset-option"
                  ref={(node) => {
                    presetRefs.current[index] = node;
                  }}
                  // radiogroup 的键盘约定是一枚 Tab 停靠点 + 方向键换档。
                  tabIndex={preset === entry.preset ? 0 : -1}
                  onKeyDown={(event) => {
                    const step =
                      event.key === "ArrowRight" || event.key === "ArrowDown"
                        ? 1
                        : event.key === "ArrowLeft" || event.key === "ArrowUp"
                          ? -1
                          : 0;
                    if (!step || presets.length < 2) return;
                    event.preventDefault();
                    const next = (index + step + presets.length) % presets.length;
                    setPresetChoice(presets[next].preset);
                    presetRefs.current[next]?.focus();
                  }}
                  onClick={() => setPresetChoice(entry.preset)}
                  disabled={!!busyItem && preset !== entry.preset}
                >
                  <span className="preset-label">{entry.label}</span>
                  <span className="preset-tracks">
                    {entry.stems.map((stem) => stem.label_zh).join(" / ")}
                  </span>
                  <span className="preset-desc">{entry.description}</span>
                </button>
              ))}
            </div>
          )}
          {busyItem && presets.length > 0 && (
            <p className="preset-lock">
              队列在按 <strong>{presets.find((entry) => entry.preset === preset)?.label}</strong>{" "}
              处理，改预设从下一首开始生效；正在跑的这首不会重来。
            </p>
          )}

          <button
            type="button"
            className={cn("upload-zone", isDragging && "is-dragging", queueIsFull && "upload-zone-compact")}
            onClick={() => fileInputRef.current?.click()}
            onDragEnter={handleDragEnter}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            aria-describedby="upload-help"
            disabled={serviceState !== "online"}
          >
            <span className="upload-disc" aria-hidden="true">
              <span className="wave-bars">
                {[18, 32, 48, 26, 54, 36, 22].map((height, index) => (
                  <span
                    key={`${height}-${index}`}
                    style={{ height: `${height}%` }}
                  />
                ))}
              </span>
            </span>
            <span className="flex flex-col gap-2">
              <span className="font-heading text-lg font-medium text-foreground">
                {serviceState !== "online"
                  ? "等待本地 AI 服务"
                  : isDragging
                    ? "松开即可加入队列"
                    : "拖入音频，或点击选择（可多选）"}
              </span>
              <span
                id="upload-help"
                className="text-sm leading-6 text-muted-foreground"
              >
                音频：MP3、WAV、FLAC、OGG、M4A、AAC
                <br />
                视频：MP4、MOV、MKV、WebM、AVI（自动提取音频）
                <br />
                最大 500 MB，结果在本机保留 1 小时
              </span>
            </span>
            <span className="upload-action">
              <Upload aria-hidden="true" />
              {serviceState !== "online" ? "暂不可用" : "选择音频"}
            </span>
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept=".mp3,.wav,.flac,.ogg,.m4a,.aac,.mp4,.mov,.mkv,.webm,.avi"
            className="hidden"
            onChange={handleInputChange}
            tabIndex={-1}
          />

          {items.length > 0 && (
            <ul className="queue-list" aria-label="分离队列">
              {items.map((item) => {
                const job = item.job;
                const percent =
                  item.state === "uploading"
                    ? item.uploadPct
                    : Math.round(job?.progress ?? 0);
                return (
                  <li key={item.key} className="queue-row" data-state={item.state}>
                    <button
                      type="button"
                      className="queue-main"
                      onClick={() => setSelectedKey(item.key)}
                      disabled={item.state !== "done"}
                      aria-label={`查看 ${item.name} 的结果`}
                    >
                      <span className="queue-name">{item.name}</span>
                      <span className="queue-meta">
                        {item.state === "confirm" && "重复：已分离过"}
                        {item.state === "waiting" && "排队中"}
                        {item.state === "uploading" && `上传 ${percent}%`}
                        {item.state === "processing" &&
                          `${job?.phase_label || "处理中"} ${percent}%${
                            job?.eta_seconds != null ? ` · 约剩 ${formatDuration(job.eta_seconds)}` : ""
                          }${
                            job?.elapsed_seconds != null
                              ? ` · 已用 ${formatDuration(job.elapsed_seconds)}`
                              : ""
                          }${(job?.queue_position || 0) > 0 ? ` · 前面还有 ${job?.queue_position} 首` : ""}`}
                        {item.state === "done" && doneSummary(item)}
                        {item.state === "error" && "失败"}
                        {item.state === "cancelled" && "已取消"}
                      </span>
                    </button>
                    <div className="queue-actions">
                      {item.state === "confirm" && (
                        <>
                          <Button size="sm" variant="outline" onClick={() => void startDuplicate(item.key)}>
                            仍然分离
                          </Button>
                          {item.duplicateOf && (
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() =>
                                void loadHistoryEntry(
                                  history.find((entry) => entry.job_id === item.duplicateOf) || {
                                    job_id: item.duplicateOf || "",
                                    stems: [],
                                  },
                                )
                              }
                            >
                              看上次结果
                            </Button>
                          )}
                          <Button size="sm" variant="ghost" onClick={() => removeItem(item.key)}>
                            不要了
                          </Button>
                        </>
                      )}
                      {(item.state === "processing" || item.state === "uploading") && (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => void cancelItem(item.key, item.job?.job_id)}
                        >
                          <X data-icon="inline-start" />
                          取消
                        </Button>
                      )}
                      {item.state === "waiting" && (
                        <Button size="sm" variant="ghost" onClick={() => removeItem(item.key)}>
                          <X data-icon="inline-start" />
                          移出队列
                        </Button>
                      )}
                      {item.state === "error" && item.file && (
                        <Button size="sm" variant="outline" onClick={() => retryItem(item.key)}>
                          <RefreshCw data-icon="inline-start" />
                          重试
                        </Button>
                      )}
                      {["done", "error", "cancelled"].includes(item.state) && (
                        <Button size="sm" variant="ghost" onClick={() => removeItem(item.key)}>
                          <X data-icon="inline-start" />
                          移除
                        </Button>
                      )}
                    </div>
                    {item.error && (
                      <p className="queue-error">
                        {item.error}
                        {remedyFor(item.error) && <span className="queue-remedy">{remedyFor(item.error)}</span>}
                      </p>
                    )}
                  </li>
                );
              })}
            </ul>
          )}

          {busyItem && (
            <div className="flex flex-col gap-3 py-1">
              <div className="flex flex-col items-center gap-2 text-center">
                <div className="processing-visual" aria-hidden="true">
                  <AudioLines />
                  <span className="processing-ring" />
                </div>
                <p className="font-heading text-xl font-medium text-balance">
                  {busyItem.state === "uploading"
                    ? `正在读取音频 ${busyItem.uploadPct}%`
                    : busyItem.job?.phase_label || "正在拆分音轨"}
                </p>
                <p className="max-w-full truncate text-sm text-muted-foreground">
                  {busyItem.name}
                </p>
              </div>
              <Progress value={busyItem.state === "uploading" ? busyItem.uploadPct : Math.round(busyItem.job?.progress ?? 0)} aria-label="处理进度">
                <ProgressLabel>
                  {busyItem.state === "uploading"
                    ? "上传到本地服务"
                    : busyItem.job?.phase_label || "Demucs 正在处理"}
                </ProgressLabel>
                <ProgressValue>
                  {(_, value) => `${Math.round(value ?? 0)}%`}
                </ProgressValue>
              </Progress>
              <p className="text-center text-xs leading-5 text-muted-foreground text-pretty">
                已用 {formatDuration(busyItem.job?.elapsed_seconds)}
                {busyItem.job?.eta_seconds != null && ` · 预计剩余 ${formatDuration(busyItem.job?.eta_seconds)}`}
                {typeof busyItem.job?.queue_position === "number" && busyItem.job.queue_position > 0
                  ? ` · 前面还有 ${busyItem.job.queue_position} 首`
                  : ""}
                。耗时取决于音频长度与显卡，首次运行还要下载模型权重。
              </p>
            </div>
          )}

          {notice && (
            <p className="service-notice" data-tone={notice.tone} role="status">
              <span className="flex min-w-0 items-start gap-2">
                {notice.tone === "warn" ? (
                  <TriangleAlert aria-hidden="true" />
                ) : (
                  <CircleCheck aria-hidden="true" />
                )}
                <span className="min-w-0 break-all">{notice.text}</span>
              </span>
              <Button size="sm" variant="ghost" onClick={() => setNotice(null)}>
                知道了
              </Button>
            </p>
          )}

          {shownJob?.error && shown?.state === "error" && (
            <p className="error-message" role="alert">
              <TriangleAlert aria-hidden="true" />
              <span>
                {shownJob.error}
                {remedyFor(shownJob.error) && (
                  <span className="queue-remedy">{remedyFor(shownJob.error)}</span>
                )}
              </span>
            </p>
          )}
        </CardContent>

        {visibleStems.length > 0 && shownJob?.job_id && (
          <>
            <CardContent className="flex flex-col gap-5">
              <div className="success-summary" role="status" aria-live="polite">
                <span className="success-icon" aria-hidden="true">
                  <Check />
                </span>
                <div className="min-w-0">
                  <p className="font-heading text-lg font-medium">
                    {presetLabel(presets, shownJob.preset)}完成
                  </p>
                  <p className="truncate text-sm text-muted-foreground">
                    {shownSource}
                    {shownJob.elapsed_seconds ? ` · 用时 ${formatDuration(shownJob.elapsed_seconds)}` : ""}
                  </p>
                </div>
              </div>

              <div className="flex flex-col gap-2">
                {visibleStems.map(([stemKey, stem]) => {
                  const meta = STEM_META[stemKey] || { label: stemKey, icon: Music2 };
                  const Icon = meta.icon;
                  const label = stem.label || meta.label;
                  // 后端的 exists 是当场对磁盘 stat 出来的：文件被移走就照实说没了，
                  // 而不是摆一个点了必然失败的播放器。
                  const missing = stem.exists === false;
                  const audioUrl = `${API_BASE}/api/download/${shownJob.job_id}/${encodeURIComponent(stemKey)}`;

                  return (
                    <article key={stemKey} className="stem-row" data-missing={missing ? "true" : "false"}>
                      <span
                        className="stem-icon"
                        data-stem={stemKey}
                        aria-hidden="true"
                      >
                        <Icon />
                      </span>
                      <div className="min-w-0 flex-1">
                        <h3 className="font-heading text-sm font-medium">{label}</h3>
                        <p className="text-xs text-muted-foreground tabular-nums">
                          {missing
                            ? "结果已不存在（超过 1 小时自动清理，或文件被移走）"
                            : `WAV，${formatSize(stem.bytes ?? (stem.size_mb || 0) * 1024 * 1024)}`}
                        </p>
                      </div>
                      {missing ? null : (
                        <audio
                          controls
                          preload="none"
                          className="audio-player"
                          aria-label={`试听${label}`}
                          src={audioUrl}
                        />
                      )}
                      <Button
                        variant="outline"
                        disabled={saving === stemKey || missing}
                        aria-busy={saving === stemKey}
                        onClick={() => void exportStem(shownJob.job_id, stemKey, label, shownSource, isDesktop)}
                        aria-label={
                          missing
                            ? `${label}的结果文件已不存在`
                            : saving === stemKey
                              ? `正在导出${label}`
                              : isDesktop
                                ? `保存${label}`
                                : `下载${label}`
                        }
                      >
                        {saving === stemKey ? (
                          "正在导出"
                        ) : (
                          <>
                            <Download data-icon="inline-start" />
                            {isDesktop ? "保存" : "下载"}
                          </>
                        )}
                      </Button>
                    </article>
                  );
                })}
              </div>

              {shownJob.output_dir && (
                <div className="output-path">
                  <span className="min-w-0 break-all">
                    本机结果目录：{shownJob.output_dir}
                  </span>
                  {isDesktop && (
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => void shellApi()?.revealPath?.(shownJob.output_dir || "")}
                    >
                      <FolderOpen data-icon="inline-start" />
                      打开目录
                    </Button>
                  )}
                </div>
              )}
            </CardContent>
            <CardFooter className="justify-between gap-4">
              <span className="hidden items-center gap-2 text-xs text-muted-foreground sm:flex">
                <Headphones className="size-4" aria-hidden="true" />
                {isDesktop ? "可先试听，再选目录保存" : "可先试听，再按需下载"}
              </span>
              <Button
                variant="ghost"
                onClick={() => {
                  setItems((current) => current.filter((entry) => entry.state === "waiting" || entry.state === "processing" || entry.state === "uploading"));
                  setSelectedKey(null);
                  setNotice(null);
                }}
              >
                <RefreshCw data-icon="inline-start" />
                收起来，只留进行中的
              </Button>
            </CardFooter>
          </>
        )}

        {!busyItem && (
          <CardFooter className="flex-col items-stretch gap-3">
            <div className="flex items-center justify-between gap-4">
              <span className="flex min-w-0 items-center gap-2 text-xs text-muted-foreground">
                <FileAudio className="size-4" aria-hidden="true" />
                {items.length > 0 ? `队列 ${items.length} 首` : "输出 WAV 音轨，结果保留 1 小时"}
              </span>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setHistoryOpen((open) => !open);
                  if (!historyOpen) void loadHistory();
                }}
                aria-expanded={historyOpen}
              >
                <History data-icon="inline-start" />
                本机最近结果
              </Button>
            </div>
            {historyOpen && (
              <ul className="history-list" aria-label="本机最近分离结果">
                {history.length === 0 && (
                  <li className="history-empty">还没有可查的历史：完成一次分离后会出现在这里。</li>
                )}
                {history.map((entry) => (
                  <li key={entry.job_id} className="history-row">
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-sm">{entry.source_name || entry.job_id}</span>
                      <span className="block truncate text-xs text-muted-foreground">
                        <Clock3 className="inline size-3" aria-hidden="true" />{" "}
                        {formatClock(entry.finished_at || entry.created_at)} ·{" "}
                        {presetLabel(presets, entry.preset)} · {entry.stems.length} 轨 ·{" "}
                        {formatSize(entry.total_bytes)}
                        {entry.expired ? " · 文件已清理" : ""}
                      </span>
                    </span>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={entry.expired}
                      onClick={() => void loadHistoryEntry(entry)}
                    >
                      载入试听
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </CardFooter>
        )}
      </Card>
    </section>
  );
}

function shellApi(): DesktopShell | null {
  if (typeof window === "undefined") return null;
  return ((window as unknown as { vocal?: DesktopShell }).vocal as DesktopShell) || null;
}

function presetLabel(presets: PresetInfo[], preset?: string) {
  if (!preset) return "";
  return presets.find((entry) => entry.preset === preset)?.label || preset;
}

/** 音轨还在不在磁盘上——后端每条轨都带当场 stat 出来的 exists。 */
function stemInventory(stems?: Record<string, StemInfo>) {
  const entries = Object.values(stems || {});
  const missing = entries.filter((info) => info.exists === false).length;
  return { total: entries.length, missing };
}

function doneSummary(item: QueueItem) {
  const { total, missing } = stemInventory(item.job?.stems);
  if (total === 0) return "完成";
  if (missing === total) return "结果已不存在（文件被移走或超时清理）";
  if (missing > 0) return `完成 · ${total - missing}/${total} 条音轨还在本机`;
  return `完成 · ${total} 条音轨`;
}
