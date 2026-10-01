"use client";

import {
  AudioLines,
  Check,
  CircleCheck,
  Cpu,
  Download,
  Drum,
  FileAudio,
  Guitar,
  Headphones,
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
const VIDEO_EXTENSIONS = new Set(["mp4", "mov", "mkv", "webm", "avi"]);
const POLL_INTERVAL = 1000;

type WorkspaceStatus =
  | "idle"
  | "uploading"
  | "processing"
  | "done"
  | "error";
type ServiceState = "checking" | "online" | "offline" | "missing";

interface Stem {
  size_mb: number;
}

interface JobResponse {
  job_id: string;
  status:
    | "queued"
    | "processing"
    | "cancelling"
    | "cancelled"
    | "done"
    | "error";
  progress: number;
  error?: string | null;
  stems?: Record<string, Stem>;
}

interface CompletedJob extends JobResponse {
  status: "done";
  stems: Record<string, Stem>;
}

interface StemMeta {
  label: string;
  fileLabel: string;
  icon: LucideIcon;
}

const STEM_ORDER = ["vocals", "drums", "bass", "other"] as const;
const STEM_META: Record<string, StemMeta> = {
  vocals: { label: "人声", fileLabel: "人声", icon: Mic2 },
  drums: { label: "鼓组", fileLabel: "鼓组", icon: Drum },
  bass: { label: "贝斯", fileLabel: "贝斯", icon: Guitar },
  other: { label: "其他乐器", fileLabel: "其他乐器", icon: Music2 },
};

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

function formatSize(sizeMb: number) {
  return `${sizeMb.toFixed(1)} MB`;
}

function delay(milliseconds: number) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
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

export function VocalWorkspace() {
  const [status, setStatus] = useState<WorkspaceStatus>("idle");
  const [serviceState, setServiceState] = useState<ServiceState>("checking");
  const [progress, setProgress] = useState(0);
  const [fileName, setFileName] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<CompletedJob | null>(null);
  const [downloading, setDownloading] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const uploadControllerRef = useRef<AbortController | null>(null);
  const serviceControllerRef = useRef<AbortController | null>(null);
  const currentJobIdRef = useRef<string | null>(null);
  const requestSequenceRef = useRef(0);
  const dragDepthRef = useRef(0);

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
      const health = (await response.json()) as { demucs_available?: boolean };
      setServiceState(health.demucs_available ? "online" : "missing");
    } catch {
      if (!controller.signal.aborted) setServiceState("offline");
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => void checkService(), 0);
    return () => {
      window.clearTimeout(timer);
      serviceControllerRef.current?.abort();
    };
  }, [checkService]);

  useEffect(() => {
    return () => {
      requestSequenceRef.current += 1;
      uploadControllerRef.current?.abort();
      serviceControllerRef.current?.abort();
    };
  }, []);

  const pollJob = useCallback(async (jobId: string, sequence: number) => {
    let consecutiveFailures = 0;

    while (requestSequenceRef.current === sequence) {
      await delay(POLL_INTERVAL);

      try {
        const response = await fetch(`${API_BASE}/api/jobs/${jobId}`, {
          cache: "no-store",
        });
        if (!response.ok) throw new Error(await getErrorMessage(response));

        const job = (await response.json()) as JobResponse;
        consecutiveFailures = 0;
        setProgress(Math.max(1, Math.min(job.progress ?? 0, 100)));

        if (job.status === "done" && job.stems) {
          setResult({ ...job, status: "done", stems: job.stems });
          setStatus("done");
          return;
        }

        if (job.status === "cancelled") {
          setStatus("idle");
          setProgress(0);
          setFileName(null);
          currentJobIdRef.current = null;
          return;
        }

        if (job.status === "error") {
          throw new Error(job.error || "音轨分离失败，请重试。");
        }
      } catch (pollError) {
        consecutiveFailures += 1;
        if (consecutiveFailures < 3) continue;
        throw pollError;
      }
    }
  }, []);

  const handleFile = useCallback(
    async (file: File) => {
      if (serviceState !== "online") {
        setError("本地 AI 服务尚未就绪，请先重新检测。");
        setStatus("error");
        return;
      }

      const validationError = validateFile(file);
      if (validationError) {
        setFileName(file.name);
        setError(validationError);
        setStatus("error");
        return;
      }

      const sequence = requestSequenceRef.current + 1;
      requestSequenceRef.current = sequence;
      uploadControllerRef.current?.abort();
      const controller = new AbortController();
      uploadControllerRef.current = controller;

      setFileName(file.name);
      setError(null);
      setResult(null);
      currentJobIdRef.current = null;
      setProgress(1);
      setStatus("uploading");

      const formData = new FormData();
      formData.append("file", file);

      try {
        const response = await fetch(`${API_BASE}/api/separate`, {
          method: "POST",
          body: formData,
          signal: controller.signal,
        });

        if (!response.ok) throw new Error(await getErrorMessage(response));
        const job = (await response.json()) as JobResponse;
        if (!job.job_id) throw new Error("服务未返回有效任务编号。");
        if (requestSequenceRef.current !== sequence) return;

        currentJobIdRef.current = job.job_id;
        setServiceState("online");
        setStatus("processing");
        setProgress(Math.max(job.progress || 1, 2));
        await pollJob(job.job_id, sequence);
      } catch (submitError) {
        if (controller.signal.aborted || requestSequenceRef.current !== sequence) {
          return;
        }
        const message = friendlyError(
          submitError,
          "无法连接本地 AI 服务，请重新检测后再试。",
        );
        setError(message);
        setStatus("error");
        if (message.includes("fetch") || message.includes("连接")) {
          setServiceState("offline");
        }
      }
    },
    [pollJob, serviceState],
  );

  const handleInputChange = useCallback(
    (event: React.ChangeEvent<HTMLInputElement>) => {
      const file = event.currentTarget.files?.[0];
      event.currentTarget.value = "";
      if (file) void handleFile(file);
    },
    [handleFile],
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
        setError("本地 AI 服务尚未就绪，请先重新检测。");
        setStatus("error");
        return;
      }
      const file = event.dataTransfer.files[0];
      if (file) void handleFile(file);
    },
    [handleFile, serviceState],
  );

  const handleReset = useCallback(() => {
    requestSequenceRef.current += 1;
    uploadControllerRef.current?.abort();
    setStatus("idle");
    setProgress(0);
    setFileName(null);
    setError(null);
    setResult(null);
    setDownloading(null);
    currentJobIdRef.current = null;
  }, []);

  const handleCancel = useCallback(async () => {
    const jobId = currentJobIdRef.current;
    requestSequenceRef.current += 1;
    uploadControllerRef.current?.abort();
    currentJobIdRef.current = null;
    setStatus("idle");
    setProgress(0);
    setFileName(null);
    setError(null);
    setResult(null);

    if (!jobId) return;
    try {
      const response = await fetch(`${API_BASE}/api/jobs/${jobId}/cancel`, {
        method: "POST",
      });
      if (!response.ok && response.status !== 409) {
        throw new Error(await getErrorMessage(response));
      }
    } catch (cancelError) {
      setError(friendlyError(cancelError, "任务取消失败，请稍后重试。"));
      setStatus("error");
    }
  }, []);

  const downloadStem = useCallback(
    async (jobId: string, stemKey: string, meta: StemMeta) => {
      setDownloading(stemKey);
      setError(null);
      try {
        const response = await fetch(
          `${API_BASE}/api/download/${jobId}/${encodeURIComponent(stemKey)}`,
        );
        if (!response.ok) throw new Error(await getErrorMessage(response));

        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        const baseName = fileName?.replace(/\.[^.]+$/, "") || "声析音轨";
        anchor.download = `${baseName}-${meta.fileLabel}.wav`;
        document.body.append(anchor);
        anchor.click();
        anchor.remove();
        window.setTimeout(() => URL.revokeObjectURL(url), 0);
      } catch (downloadError) {
        setError(
          friendlyError(downloadError, "下载失败，请稍后重试。"),
        );
      } finally {
        setDownloading(null);
      }
    },
    [fileName],
  );

  const isBusy = status === "uploading" || status === "processing";
  const uploadDisabled = serviceState !== "online";
  const isVideoFile = fileName ? VIDEO_EXTENSIONS.has(getExtension(fileName)) : false;
  const visibleStems = result
    ? STEM_ORDER.flatMap((stemKey) => {
        const stem = result.stems[stemKey];
        return stem ? [[stemKey, stem] as const] : [];
      })
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
            <CardDescription>添加音频或视频，自动生成 4 条 WAV 音轨。</CardDescription>
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

        {(status === "idle" || status === "error") && (
          <CardContent className="flex flex-col gap-4">
            <button
              type="button"
              className={cn("upload-zone", isDragging && "is-dragging")}
              onClick={() => fileInputRef.current?.click()}
              onDragEnter={handleDragEnter}
              onDragOver={(event) => event.preventDefault()}
              onDragLeave={handleDragLeave}
              onDrop={handleDrop}
              aria-describedby="upload-help"
              disabled={uploadDisabled}
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
                  {uploadDisabled
                    ? "等待本地 AI 服务"
                    : isDragging
                      ? "松开即可添加音频"
                      : "拖入音频，或点击选择"}
                </span>
                <span
                  id="upload-help"
                  className="text-sm leading-6 text-muted-foreground"
                >
                  音频：MP3、WAV、FLAC、OGG、M4A、AAC
                  <br />
                  视频：MP4、MOV、MKV、WebM、AVI（自动提取音频）
                  <br />
                  最大 500 MB
                </span>
              </span>
              <span className="upload-action">
                <Upload aria-hidden="true" />
                {uploadDisabled ? "暂不可用" : "选择音频"}
              </span>
            </button>
            <input
              ref={fileInputRef}
              type="file"
              accept=".mp3,.wav,.flac,.ogg,.m4a,.aac,.mp4,.mov,.mkv,.webm,.avi"
              className="hidden"
              onChange={handleInputChange}
              tabIndex={-1}
            />

            {uploadDisabled && (
              <div className="service-notice" data-state={serviceState} role="status">
                <span className="flex min-w-0 items-start gap-2">
                  {serviceState === "checking" ? (
                    <LoaderCircle className="service-spinner" aria-hidden="true" />
                  ) : serviceState === "missing" ? (
                    <Cpu aria-hidden="true" />
                  ) : (
                    <TriangleAlert aria-hidden="true" />
                  )}
                  <span>
                    {serviceState === "checking" && "正在启动本地 AI 服务，请稍候。"}
                    {serviceState === "offline" && "本地 AI 服务未连接，请重新检测。"}
                    {serviceState === "missing" && "未检测到 Demucs 音轨分离模型。"}
                  </span>
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

            {error && (
              <p className="error-message" role="alert">
                <TriangleAlert aria-hidden="true" />
                <span>{error}</span>
              </p>
            )}
          </CardContent>
        )}

        {isBusy && (
          <CardContent className="flex flex-col gap-6 py-5">
            <div className="processing-visual" aria-hidden="true">
              <AudioLines />
              <span className="processing-ring" />
            </div>
            <div className="flex flex-col items-center gap-2 text-center">
              <p className="font-heading text-xl font-medium text-balance">
                {status === "uploading"
                  ? "正在读取音频"
                  : isVideoFile
                    ? "正在提取音频并拆分音轨"
                    : "正在拆分四条音轨"}
              </p>
              <p className="max-w-full truncate text-sm text-muted-foreground">
                {fileName}
              </p>
            </div>
            <Progress value={progress} aria-label="处理进度">
              <ProgressLabel>
                {status === "uploading"
                  ? "正在读取音频"
                  : isVideoFile
                    ? "正在提取音频并分离"
                    : "Demucs 正在处理"}
              </ProgressLabel>
              <ProgressValue>
                {(_, value) => `${Math.round(value ?? 0)}%`}
              </ProgressValue>
            </Progress>
            <p className="text-center text-xs leading-5 text-muted-foreground text-pretty">
              处理时间取决于音频长度与显卡性能，请保持声析运行。
            </p>
          </CardContent>
        )}

        {status === "done" && result && (
          <>
            <CardContent className="flex flex-col gap-5">
              <div className="success-summary" role="status" aria-live="polite">
                <span className="success-icon" aria-hidden="true">
                  <Check />
                </span>
                <div className="min-w-0">
                  <p className="font-heading text-lg font-medium">四轨分离完成</p>
                  <p className="truncate text-sm text-muted-foreground">
                    {fileName}
                  </p>
                </div>
              </div>

              <div className="flex flex-col gap-2">
                {visibleStems.map(([stemKey, stem]) => {
                  const meta = STEM_META[stemKey] ?? {
                    label: stemKey,
                    fileLabel: stemKey,
                    icon: Music2,
                  };
                  const Icon = meta.icon;
                  const audioUrl = `${API_BASE}/api/download/${result.job_id}/${stemKey}`;

                  return (
                    <article key={stemKey} className="stem-row">
                      <span
                        className="stem-icon"
                        data-stem={stemKey}
                        aria-hidden="true"
                      >
                        <Icon />
                      </span>
                      <div className="min-w-0 flex-1">
                        <h3 className="font-heading text-sm font-medium">
                          {meta.label}
                        </h3>
                        <p className="text-xs text-muted-foreground tabular-nums">
                          WAV，{formatSize(stem.size_mb)}
                        </p>
                      </div>
                      <audio
                        controls
                        preload="none"
                        className="audio-player"
                        aria-label={`试听${meta.label}`}
                        src={audioUrl}
                      />
                      <Button
                        variant="outline"
                        disabled={downloading === stemKey}
                        aria-busy={downloading === stemKey}
                        onClick={() =>
                          void downloadStem(result.job_id, stemKey, meta)
                        }
                        aria-label={
                          downloading === stemKey
                            ? `正在下载${meta.label}`
                            : `下载${meta.label}`
                        }
                      >
                        {downloading === stemKey ? (
                          "正在下载"
                        ) : (
                          <>
                            <Download data-icon="inline-start" />
                            下载
                          </>
                        )}
                      </Button>
                    </article>
                  );
                })}
              </div>

              {error && (
                <p className="error-message" role="alert">
                  <TriangleAlert aria-hidden="true" />
                  <span>{error}</span>
                </p>
              )}
            </CardContent>
            <CardFooter className="justify-between gap-4">
              <span className="hidden items-center gap-2 text-xs text-muted-foreground sm:flex">
                <Headphones className="size-4" aria-hidden="true" />
                可先试听，再按需下载
              </span>
              <Button variant="ghost" onClick={handleReset}>
                <RefreshCw data-icon="inline-start" />
                分离另一首
              </Button>
            </CardFooter>
          </>
        )}

        {status !== "done" && (
          <CardFooter className="justify-between gap-4">
            <span className="flex items-center gap-2 text-xs text-muted-foreground">
              <FileAudio className="size-4" aria-hidden="true" />
              {isBusy ? "处理中请保持声析运行" : "输出 4 条 WAV 音轨"}
            </span>
            {isBusy && (
              <Button variant="ghost" onClick={() => void handleCancel()}>
                <X data-icon="inline-start" />
                取消任务
              </Button>
            )}
          </CardFooter>
        )}
      </Card>
    </section>
  );
}
