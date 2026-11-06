import type { Accent } from "../types";
import { audioUrl } from "./dictAdapter";

/**
 * 发音适配器：有道音频 → 浏览器 speechSynthesis → 失败。
 * 两口音互不打断：切换口音要先停掉当前这一路。
 */

export type PronounceWay = "audio" | "tts" | "failed";

let currentAudio: HTMLAudioElement | null = null;

export function stopPronounce() {
  if (currentAudio) {
    currentAudio.pause();
    currentAudio.currentTime = 0;
    currentAudio = null;
  }
  if (typeof window !== "undefined" && "speechSynthesis" in window) {
    window.speechSynthesis.cancel();
  }
}

function playUrl(url: string): Promise<boolean> {
  return new Promise((resolve) => {
    const a = new Audio(url);
    a.preload = "auto";
    currentAudio = a;
    let settled = false;
    const done = (ok: boolean) => {
      if (settled) return;
      settled = true;
      if (currentAudio === a) currentAudio = null;
      resolve(ok);
    };
    a.addEventListener("ended", () => done(true));
    a.addEventListener("error", () => done(false));
    a.addEventListener("stalled", () => {
      /* 网络慢也别立刻判死，交给 error 兜底 */
    });
    a.play().catch(() => done(false));
    // 8 秒没有结束就认为失败，转 TTS
    window.setTimeout(() => done(false), 8000);
  });
}

export async function pronounce(word: string, accent: Accent): Promise<PronounceWay> {
  stopPronounce();
  const w = word.trim();
  if (!w) return "failed";

  if (await playUrl(audioUrl(w, accent))) return "audio";

  if (typeof window !== "undefined" && "speechSynthesis" in window) {
    const u = new SpeechSynthesisUtterance(w);
    u.lang = accent === "uk" ? "en-GB" : "en-US";
    u.rate = 0.9;
    window.speechSynthesis.speak(u);
    return "tts";
  }
  return "failed";
}

/** 朗读一整段时的默认上限。太长浏览器会自己截，不如我们截得干净。 */
const SPEAK_MAX = 1200;

/**
 * 把一段 markdown 洗成**能念的纯文本** —— 朗读整段之前先过这一步。
 *
 * 不洗的话 TTS 会把「星号星号」「反引号」也念出来；链接还会把整个网址念一遍。
 * 截断时**不加省略号** —— 念出来不该有「点点点」。
 */
export function speakableText(md: string, maxChars: number = SPEAK_MAX): string {
  if (!md) return "";
  let s = md;
  // 代码块：去围栏、留内容（英语工具里基本没有代码，保险起见还是洗一下）
  s = s.replace(/```[\s\S]*?```/g, (block) => block.replace(/```[\w-]*\n?/g, ""));
  s = s.replace(/\[([^\]]*)\]\([^)\s]*\)/g, "$1");        // 链接 → 只留文字
  s = s.replace(/^\s{0,3}#{1,6}\s+/gm, "");                  // 标题井号
  s = s.replace(/^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+/gm, "");  // 列表符号
  s = s.replace(/^\s{0,3}>\s?/gm, "");                       // 引用符号
  s = s.replace(/\*\*(.+?)\*\*/g, "$1");                   // **粗**
  s = s.replace(/__(.+?)__/g, "$1");                          // __粗__
  s = s.replace(/\*([^*\n]+?)\*/g, "$1");                   // *斜*
  s = s.replace(/`/g, "");                                    // 行内代码的反引号
  s = s.replace(/\*+/g, "");                                  // 残留的孤立星号（如整段只有 "**"）
  s = s.replace(/\s+/g, " ").trim();                          // 换行与连续空白压成单空格
  return s.length > maxChars ? s.slice(0, maxChars) : s;
}

/** 当前环境能不能发音（都不行就把按钮置灰，别点了没反应） */
export function canPronounce(): boolean {
  if (typeof window === "undefined") return false;
  return "Audio" in window || "speechSynthesis" in window;
}
