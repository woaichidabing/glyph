import { useEffect, useRef, useState } from "react";
import type { Accent } from "../types";
import { pronounce } from "../adapters/pronounce";

type S = "idle" | "loading" | "playing";

/**
 * 词头发音：英/美两个喇叭按钮各跟一个音标。
 * 有道音频 → 浏览器朗读兜底；两口音互不打断；autoPlay 换词才念（学习卡"看到即听到"）。
 * 默认美音在前（多数学习者的首选），偏好设置以后接入时只改这里的 order。
 */
export function Pronounce({
  word,
  phonetic,
  autoPlay,
  hidePhonetic,
  single,
}: {
  word: string;
  phonetic?: { uk: string; us: string };
  autoPlay?: boolean;
  /** 音标已在别处显示时，这里只留喇叭 */
  hidePhonetic?: boolean;
  /** 只保留一个发音按钮，用在列表行里 */
  single?: boolean;
}) {
  const [state, setState] = useState<Record<Accent, S>>({ us: "idle", uk: "idle" });
  const [note, setNote] = useState("");
  const timer = useRef<number | null>(null);
  const spoken = useRef<string>("");
  const order: Accent[] = single ? ["us"] : ["us", "uk"];

  useEffect(() => () => {
    if (timer.current) window.clearTimeout(timer.current);
  }, []);

  const speak = async (a: Accent) => {
    if (timer.current) window.clearTimeout(timer.current);
    setState({ us: "idle", uk: "idle" });
    setState((s) => ({ ...s, [a]: "loading" }));
    const way = await pronounce(word, a);
    if (way === "failed") {
      setState({ us: "idle", uk: "idle" });
      setNote("此环境无法发音");
      return;
    }
    setState((s) => ({ ...s, [a]: "playing" }));
    setNote(way === "audio" ? `已播${a === "uk" ? "英" : "美"}音` : "音频不可用，已用浏览器朗读");
    timer.current = window.setTimeout(
      () => setState({ us: "idle", uk: "idle" }),
      way === "audio" ? 1000 : 1600,
    );
  };

  useEffect(() => {
    if (!autoPlay) return;
    if (spoken.current === word) return;
    spoken.current = word;
    void speak("us");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoPlay, word]);

  return (
    <>
      {order.map((a) => {
        const st = state[a];
        const on = st !== "idle";
        return (
          <span key={a} style={{ display: "inline-flex", alignItems: "center", gap: 5 }}>
            <button
              type="button"
              className="e-sound"
              onClick={() => void speak(a)}
              title={a === "uk" ? "播放英音" : "播放美音"}
              aria-label={a === "uk" ? "播放英音" : "播放美音"}
              style={on ? { borderColor: "var(--accent)", color: "var(--accent)" } : undefined}
            >
              {st === "loading" ? (
                <span style={{ fontSize: 11 }}>…</span>
              ) : (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M11 5 6 9H2v6h4l5 4V5z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/><path d="M18.5 5.5a9 9 0 0 1 0 13"/></svg>
              )}
            </button>
            {hidePhonetic ? null : <span className="e-phn">{(a === "uk" ? phonetic?.uk : phonetic?.us) || "—"}</span>}
          </span>
        );
      })}
      {note ? <span style={{ fontSize: 11.5, color: "rgb(var(--ink) /.45)" }}>{note}</span> : null}
    </>
  );
}
