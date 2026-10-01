import {
  AudioWaveform,
  Cpu,
  ShieldCheck,
  SlidersHorizontal,
} from "lucide-react";

import { VocalWorkspace } from "@/components/vocal-workspace";

const CAPABILITIES = [
  {
    icon: ShieldCheck,
    title: "音频留在本机",
    description: "不上传云端",
  },
  {
    icon: SlidersHorizontal,
    title: "四条音轨或人声+伴奏",
    description: "标准 WAV 输出",
  },
  {
    icon: Cpu,
    title: "有显卡就用显卡",
    description: "CUDA 不可用时走 CPU",
  },
];

export default function Home() {
  return (
    <main className="studio-shell min-h-[100dvh]">
      <div className="studio-frame mx-auto flex min-h-[100dvh] w-full max-w-7xl flex-col px-5 py-4 sm:px-7 lg:px-9">
        <header className="studio-header flex h-16 items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="brand-mark" aria-hidden="true">
              <AudioWaveform strokeWidth={2} />
            </div>
            <div>
              <p className="font-heading text-lg font-semibold tracking-[0.12em]">
                声析
              </p>
              <p className="text-xs text-muted-foreground">本地音轨工作室</p>
            </div>
          </div>
          <div className="hidden items-center gap-2 text-xs text-muted-foreground sm:flex">
            <ShieldCheck className="size-4 text-meter" aria-hidden="true" />
            所有处理均在此电脑完成
          </div>
        </header>

        <div className="studio-stage my-auto grid items-center gap-8 py-7 lg:grid-cols-[0.7fr_1.3fr] lg:gap-12 lg:py-9">
          <section className="hero-copy order-2 flex max-w-xl flex-col gap-7 lg:order-1">
            <div className="flex flex-col gap-4">
              <p className="eyebrow">本地 AI 音轨分离</p>
              <h1 className="font-heading text-4xl font-semibold leading-[1.08] tracking-[-0.045em] text-balance sm:text-5xl xl:text-[3.45rem]">
                <span className="block">一首歌，</span>
                <span className="block">拆成四条音轨</span>
              </h1>
              <p className="max-w-md text-base leading-7 text-muted-foreground text-pretty">
                默认拆成人声、鼓组、贝斯和其他乐器四条轨；只要伴奏时选
                「人声 + 伴奏」两轨预设。用于翻唱、混音、采样与练习。
              </p>
            </div>

            <ul className="capability-list" aria-label="软件能力">
              {CAPABILITIES.map(({ icon: Icon, title, description }) => (
                <li key={title} className="capability-row">
                  <span className="capability-icon" aria-hidden="true">
                    <Icon />
                  </span>
                  <span className="min-w-0">
                    <span className="block font-heading text-sm font-medium text-foreground">
                      {title}
                    </span>
                    <span className="block text-xs text-muted-foreground">
                      {description}
                    </span>
                  </span>
                </li>
              ))}
            </ul>
          </section>

          <VocalWorkspace />
        </div>

        <footer className="studio-footer flex flex-col gap-1.5 border-t border-border/70 py-4 text-xs text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
          <p>声析，本地 AI 音轨分离工具</p>
          <p>临时结果保留 1 小时，已保存文件不受影响</p>
        </footer>
      </div>
    </main>
  );
}
