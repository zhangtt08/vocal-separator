"use client";

import { useEffect, useState } from "react";

interface VocalShell {
  windowControls: {
    minimize: () => Promise<void>;
    toggleMaximize: () => Promise<boolean>;
    close: () => Promise<void>;
    isMaximized: () => Promise<boolean>;
    onMaximizedChange: (cb: (maximized: boolean) => void) => () => void;
  };
}

function shell(): VocalShell | null {
  return typeof window !== "undefined" && (window as unknown as { vocal?: VocalShell }).vocal
    ? ((window as unknown as { vocal: VocalShell }).vocal)
    : null;
}

function useMaximized(): boolean {
  const [maximized, setMaximized] = useState(false);
  useEffect(() => {
    const s = shell();
    if (!s) return;
    s.windowControls.isMaximized().then(setMaximized).catch(() => {});
    return s.windowControls.onMaximizedChange(setMaximized);
  }, []);
  return maximized;
}

const btn =
  "grid h-full w-11 place-items-center text-white/50 transition-colors hover:bg-white/10 hover:text-white";

/** 窗口三键簇：嵌入应用自己的头部行。 */
export function WindowControls() {
  const maximized = useMaximized();
  const s = shell();
  if (!s) return null;
  const btn =
    "grid h-8 w-11 place-items-center text-white/50 transition-colors hover:bg-white/10 hover:text-white";
  return (
    <div className="flex items-center [-webkit-app-region:no-drag]">
      <button aria-label="最小化" className={btn} onClick={() => void s.windowControls.minimize()} type="button">
        <svg height="10" viewBox="0 0 10 10" width="10"><path d="M0 5h10" stroke="currentColor" strokeWidth="1" /></svg>
      </button>
      <button
        aria-label={maximized ? "还原" : "最大化"}
        className={btn}
        onClick={() => void s.windowControls.toggleMaximize()}
        type="button"
      >
        {maximized ? (
          <svg height="10" viewBox="0 0 10 10" width="10">
            <path d="M2.5 2.5V1h7v7H8" fill="none" stroke="currentColor" strokeWidth="1" />
            <rect fill="none" height="6.5" stroke="currentColor" strokeWidth="1" width="6.5" x="0.5" y="2.5" />
          </svg>
        ) : (
          <svg height="10" viewBox="0 0 10 10" width="10">
            <rect fill="none" height="8" stroke="currentColor" strokeWidth="1" width="8" x="1" y="1" />
          </svg>
        )}
      </button>
      <button
        aria-label="关闭"
        className={`${btn} hover:bg-[#cf3f4f] hover:text-white`}
        onClick={() => void s.windowControls.close()}
        type="button"
      >
        <svg height="10" viewBox="0 0 10 10" width="10"><path d="M0 0l10 10M10 0L0 10" stroke="currentColor" strokeWidth="1" /></svg>
      </button>
    </div>
  );
}

/** 独立标题条：仅在页面没有自己的头部行时兜底使用（当前布局已改为内嵌模式）。 */
export function TitleBar() {
  const maximized = useMaximized();
  const s = shell();
  return (
    <div
      className="flex h-9 shrink-0 select-none items-center justify-between bg-[#0a222b] pl-3 [-webkit-app-region:drag]"
      onDoubleClick={(e) => {
        if ((e.target as HTMLElement).closest("button")) return;
        void s?.windowControls.toggleMaximize();
      }}
    >
      <div className="flex items-center gap-2 text-xs font-medium text-white/60">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/icon.svg" alt="" className="size-4" />
        <span>声析 · 本地 AI 音轨分离</span>
      </div>
      <div className="flex h-9 [-webkit-app-region:no-drag]">
        <button aria-label="最小化" className={btn} onClick={() => void s?.windowControls.minimize()} type="button">
          <svg height="10" viewBox="0 0 10 10" width="10"><path d="M0 5h10" stroke="currentColor" strokeWidth="1" /></svg>
        </button>
        <button
          aria-label={maximized ? "还原" : "最大化"}
          className={btn}
          onClick={() => void s?.windowControls.toggleMaximize()}
          type="button"
        >
          {maximized ? (
            <svg height="10" viewBox="0 0 10 10" width="10">
              <path d="M2.5 2.5V1h7v7H8" fill="none" stroke="currentColor" strokeWidth="1" />
              <rect fill="none" height="6.5" stroke="currentColor" strokeWidth="1" width="6.5" x="0.5" y="2.5" />
            </svg>
          ) : (
            <svg height="10" viewBox="0 0 10 10" width="10">
              <rect fill="none" height="8" stroke="currentColor" strokeWidth="1" width="8" x="1" y="1" />
            </svg>
          )}
        </button>
        <button
          aria-label="关闭"
          className={`${btn} hover:bg-[#cf3f4f] hover:text-white`}
          onClick={() => void s?.windowControls.close()}
          type="button"
        >
          <svg height="10" viewBox="0 0 10 10" width="10"><path d="M0 0l10 10M10 0L0 10" stroke="currentColor" strokeWidth="1" /></svg>
        </button>
      </div>
    </div>
  );
}
