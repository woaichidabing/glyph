import { useEffect, useMemo, useRef, useState } from "react";
import { useStore } from "../../store";
import { useAgent } from "../../agent/AgentContext";
import { todayList, type PlanTask } from "../../adapters/planStats";
import { greeting, formatEyebrow, latestSession, nextOpenTask, sessionWhen } from "./welcome";
import { BrainPicker } from "../settings";
import Transcript from "./Transcript";
import AskCard from "./AskCard";
import BatchBoard, { NarrowWorkEntryBar } from "../../components/BatchBoard";
import CanvasStage from "../../components/CanvasStage";
import { upload, type Uploaded } from "../../adapters/uploadAdapter";
import { deriveUsage, friendlyTokens, type UsageBaseline } from "../../adapters/usage";
import type { TimelineNode } from "../../adapters/askTimeline";
import { canAddToTray, capacityRemaining, deriveBatch, workPanelMode, type TrayItem } from "../../adapters/batch";
import { buildBatchMessage, trayImagesToUploaded } from "../../adapters/outbound";

/* ---------- 今日计划摘要条：没有今日任务时整块不显示；首页只做提醒，管理去计划页 ---------- */
function PlanStrip({ tasks }: { tasks: PlanTask[] }) {
  const setView = useStore(s => s.setView);
  if (tasks.length === 0) return null;
  const done = tasks.filter(t => t.done).length;
  const next = nextOpenTask(tasks);
  const goPlan = () => setView("plan");

  return (
    <div className="plan-strip">
      <div className="ps-top">
        <span className="ps-k">今天</span>
        <b className="ps-n">{done} / {tasks.length}</b>
        <span className="ps-sp" />
        <button type="button" className="ps-open" onClick={goPlan}>计划</button>
      </div>
      {next ? (
        <button type="button" className="ps-next" onClick={goPlan} title="去计划页勾选或查看全部">
          <span className="pd" aria-hidden />
          <span className="ps-next-t">{next.t}</span>
        </button>
      ) : null}
    </div>
  );
}

/* ---------- 用量状态条：输入框正下方一行，平时只这一行 ----------
   只在真有用量数据时才出现（历史回放没有用量 → 整行不显示，而不是显示 0）。
   "省了多少"是按钱算的：命中缓存的部分平台按 2 折计费（口径见 adapters/usage.ts）。 */
function UsageBar({ nodes, baseline }: { nodes: TimelineNode[]; baseline: UsageBaseline | null }) {
  const { summary } = useMemo(() => deriveUsage(nodes, baseline), [nodes, baseline]);
  if (summary.inputTokens <= 0) return null;

  return (
    <div className="ubar" title="命中公共前缀的输入按标准价 2 折计费，所以“省了多少”是按钱算的">
      <span className="ub-seg">
        <svg className="ub-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12a8 8 0 0 1-8 8H7l-4 3V12a8 8 0 0 1 8-8h2a8 8 0 0 1 8 8z"/></svg>
        {summary.turns} 轮{summary.steps > 0 ? ` · ${summary.steps} 步` : ""}
      </span>
      <span className="ub-dot" aria-hidden />
      <span className="ub-seg">
        <svg className="ub-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3"/><path d="M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3"/></svg>
        本会话 {friendlyTokens(summary.inputTokens)}
        {summary.savedPct === null ? null : (
          <>
            {" · "}
            <b>省了 {summary.savedPct}%</b>
            <span className="ub-sub">（缓存命中 {summary.hitPct}%）</span>
          </>
        )}
      </span>
    </div>
  );
}

/* ---------- 输入区（两处挂载共用一份附件状态） ---------- */
function Composer({ onSend, running, onStop }: {
  onSend: (text: string, images: Uploaded[]) => void;
  running: boolean;
  onStop: () => void;
}) {
  const askDraft = useStore(s => s.askDraft);
  const askDraftAt = useStore(s => s.askDraftAt);
  const setAskDraft = useStore(s => s.setAskDraft);
  const tray = useStore(s => s.tray);
  const addTrayItem = useStore(s => s.addTrayItem);
  const removeTrayItem = useStore(s => s.removeTrayItem);
  const clearTray = useStore(s => s.clearTray);
  const t = useAgent();
  const ref = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  // 模型看不了图时不再常驻黄条（2026-10-31 产品所有者：一直挂在那儿体验差）；
  // 只有用户真去点「+ 添加图片」才提示几秒
  const [visionHint, setVisionHint] = useState(false);

  // 会话里已在途（未定稿）的篇数；输入区名额 = 3 - 在途 - 已放附件
  const remaining = capacityRemaining(deriveBatch(t.timeline).items, tray);
  const trayFull = remaining === 0;
  // 当前模型看不了图时，图片 / 粘贴 / 拖拽三条入口一起关掉（纯文本功能不受影响）
  const visionOk = t.brain.supportsVision;

  useEffect(() => {
    if (!askDraft || !ref.current) return;
    const el = ref.current;
    el.value = askDraft;
    el.focus();
    const at = el.value.indexOf("____");
    if (at >= 0) el.setSelectionRange(at, at + 4);
    else el.setSelectionRange(el.value.length, el.value.length); // 无占位符时把光标放到末尾
    autosize();
    setAskDraft(""); // 一次性草稿：填进输入框即清，空态→对话中重新挂载时不会残留旧句子
  }, [askDraft, askDraftAt, setAskDraft]);

  /** 文本域随内容长高，到 160px 为止，再长自己滚（与旧版桌面端一致） */
  const autosize = () => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  };

  /** 逐张收图：每加一张都按当时的剩余名额判断，满了就不收（按钮此时也已置灰） */
  // 提示自己会消失：只在用户试图拖图的那几秒给一次反馈
  useEffect(() => {
    if (!visionHint) return;
    const timer = window.setTimeout(() => setVisionHint(false), 4000);
    return () => window.clearTimeout(timer);
  }, [visionHint]);

  const ingestFiles = (files: FileList | File[]) => {
    // 纯文本模型收不了图。但**不能静默**：拖进来的图会凭空消失，用户只会以为拖拽坏了
    if (!visionOk) {
      setVisionHint(true);
      return;
    }
    const list = Array.from(files);
    let accepted = 0;
    for (const f of list) {
      const isGif = /\.gif$/i.test(f.name) || f.type === "image/gif";
      if (!f.type.startsWith("image/") || isGif) continue;
      // 用「已接收 + 附件现状」模拟添加后的状态做容量判断
      const virtual: TrayItem[] = [...tray, ...Array.from({ length: accepted }, (_, k) => ({
        id: `virtual-${k}`, kind: "image" as const, name: "x",
      }))];
      if (!canAddToTray(deriveBatch(t.timeline).items, virtual)) break;
      const u = upload(f);
      addTrayItem({ id: u.id, kind: "image", name: u.name, url: u.url, blob: u.blob, size: u.size });
      accepted += 1;
    }
  };

  const send = () => {
    if (running) return;
    const said = (ref.current?.value ?? "").trim();
    const message = buildBatchMessage(said);
    // 只发图不带字是合法的（意图判断归模型）：有附件就不拦，正文可以是空串
    if (!message && tray.length === 0) return;
    onSend(message, trayImagesToUploaded(tray));
    if (ref.current) ref.current.value = "";
    clearTray();
    autosize();
  };

  return (
    <div className={"composer2" + (dragOver ? " drag-over" : "")}
      onDragOver={e => { e.preventDefault(); setDragOver(true); }}
      onDragLeave={() => setDragOver(false)}
      onDrop={e => {
        e.preventDefault();
        setDragOver(false);
        if (e.dataTransfer.files?.length) ingestFiles(e.dataTransfer.files);
      }}>
      <div className="cbox">
        {tray.length ? (
          <div className="ccatts">
            {tray.map(item => (
              <div className="ccatt" key={item.id}>
                {item.url ? <img src={item.url} alt={item.name} /> : null}
                <button type="button" className="cc-x" aria-label={`移除 ${item.name}`} onClick={() => removeTrayItem(item.id)}>×</button>
              </div>
            ))}
          </div>
        ) : null}
        {visionHint ? (
          <div className="cvision-warn" role="status">
            当前模型看不了图，也没配视觉模型；要用图片请到设置里换一个能看图的模型。
          </div>
        ) : null}
        <input ref={fileRef} type="file" accept="image/*" multiple hidden
          onChange={e => {
            if (e.target.files?.length) ingestFiles(e.target.files);
            e.target.value = "";
          }} />
        {/* 发送学 CC：**框内右下角一个不起眼的回车符号**，不是一颗大按钮（定稿样稿 §5.6）。
            跑的时候同一位置变停止，不挪到框外。 */}
        {running ? (
          <button className="csend stop sendin" title="停止当前任务" aria-label="停止当前任务" onClick={onStop}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>
          </button>
        ) : (
          <button className="csend sendin" title="发送（Enter）" aria-label="发送" onClick={send}>
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round"><path d="M9 10 4 15l5 5" /><path d="M20 4v7a4 4 0 0 1-4 4H4" /></svg>
          </button>
        )}
        <textarea ref={ref} rows={1} placeholder="把作文照片拖进来，或者问我一个问题" aria-label="问题"
          onInput={autosize}
          onPaste={e => {
            const files = Array.from(e.clipboardData.files ?? []);
            if (files.some(f => f.type.startsWith("image/"))) {
              e.preventDefault();
              ingestFiles(files);
            }
          }}
          onKeyDown={e => {
            // isComposing：中文输入法正在组词时回车只确认候选词，不发送（旧版同款保护）
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); }
          }} />
      </div>
        <div className="crow">
          <div className="crow-l">
            <button className="cadd"
              disabled={trayFull || !visionOk}
              title={!visionOk
                ? "当前选的模型看不了图，拍照认字暂不可用；文字问答、文本批改不受影响"
                : trayFull ? "本批已达 3 篇上限，等有篇定稿后才能再加" : "添加图片"}
              onClick={() => fileRef.current?.click()}>
              <span className="cadd-plus" aria-hidden="true">+</span>
              添加图片
            </button>
            <span className="chint">Enter 发送 · Shift+Enter 换行</span>
          </div>
          <div className="crow-r">
            <BrainPicker agent={t} />

          </div>
        </div>
      <UsageBar nodes={t.timeline.nodes} baseline={t.historyUsage} />
    </div>
  );
}


/* ---------- 主视图 ---------- */
export default function AskView({ hidden, onOpenSettings }: { hidden: boolean; onOpenSettings?: () => void }) {
  const allTasks = useStore(s => s.tasks);
  const tasks = useMemo(() => todayList(allTasks), [allTasks]);
  const setAskDraft = useStore(s => s.setAskDraft);
  const t = useAgent();
  const running = t.running;
  const online = t.online;
  // 首屏「继续上次」：最近创建的那条会话。没有就 null，那一行整行不显示。
  const lastSession = useMemo(() => latestSession(t.sessions), [t.sessions]);
  const streamRef = useRef<HTMLDivElement>(null);
  const tray = useStore(s => s.tray);
  const panelMode = workPanelMode(deriveBatch(t.timeline), tray);
  const canvasMode = useStore(s => s.canvasMode);
  // 哪几轮要解释"没省到"：跟状态条同一份口径（都在 adapters/usage.ts 里算）
  const missNotes = useMemo(
    () => deriveUsage(t.timeline.nodes, t.historyUsage).missNotes,
    [t.timeline.nodes, t.historyUsage],
  );

  /* DEV 可视化钩子（生产构建被 dead-code 消除）：
     ?demo=tray[3] 注入附件；?demo=board[-collapse] 播种会话累积 3 篇的在途批次 */
  const demoInjected = useRef(false);
  useEffect(() => {
    if (!import.meta.env.DEV || demoInjected.current) return;
    const demo = new URLSearchParams(location.search).get("demo");
    if (!demo) return;
    demoInjected.current = true;
    const add = useStore.getState().addTrayItem;
    const mkPhotoDataUrl = () => {
      const cv = document.createElement("canvas");
      cv.width = 320; cv.height = 200;
      const ctx = cv.getContext("2d");
      if (ctx) {
        ctx.fillStyle = "#fffdf8"; ctx.fillRect(0, 0, 320, 200);
        ctx.strokeStyle = "#c9ced6"; ctx.strokeRect(14, 14, 292, 172);
        ctx.fillStyle = "#3a4150"; ctx.font = "15px Georgia, serif";
        ["Everyone has a dream job.", "My dream is to be a doctor,", "because I want to help people."].forEach((l, i) => ctx.fillText(l, 30, 52 + i * 30));
        ctx.strokeStyle = "#c2402a"; ctx.beginPath(); ctx.moveTo(30, 150); ctx.lineTo(220, 150); ctx.stroke();
      }
      return cv.toDataURL("image/png");
    };
    const mkImg = (name: string) => {
      const url = mkPhotoDataUrl();
      return { id: `demo-${name}`, kind: "image" as const, name, url, blob: new Blob(), size: 18432 };
    };

    if (demo.startsWith("tray")) {
      add(mkImg("IMG_2041.jpg"));
      add(mkImg("IMG_2042.jpg"));
      if (demo === "tray3") {
        add({ id: "demo-txt", kind: "text", name: "My Weekend.", text: "My Weekend. I went to the park with my family last Sunday. We flew a kite and had a picnic by the lake." });
      }
    } else if (demo === "ocr1") {
      // 单张照片：转写完成待核对（single 模式，无货架直接铺满核对台）
      const p1 = mkPhotoDataUrl();
      setTimeout(() => {
        const seed = (window as unknown as { __seedTimeline?: (steps: unknown[]) => void }).__seedTimeline;
        seed?.([
          { kind: "user", text: "把这张照片转成文字，我核对后再批改", images: [{ name: "IMG_2066.jpg", url: p1 }] },
          { type: "tool_call", id: "oa", name: "ocr_image", args: { image_id: "ia" } },
          { type: "tool_result", id: "oa", ok: true, output: { text: "Last weekend I get up early. I [?] to the zoo with my mother. We see many animal there.", low_confidence: [{ start: 31, end: 34, text: "[?]", reason: "字迹模糊" }], warnings: [], image_id: "ia" } },
        ]);
      }, 0);
    } else if (demo === "fold") {
      // 折叠组头走查（设计稿 ask-transcript-render-design.md）：一轮里三个工具聚成一块、
      // 来源内联在块下方；下一轮工具失败——组头只给人话，原始错误串绝不上屏。
      // 阶段 2 起含思考块（reasoning_delta）：折起、点开看思考原文。
      setTimeout(() => {
        const seed = (window as unknown as { __seedTimeline?: (steps: unknown[]) => void }).__seedTimeline;
        seed?.([
          { kind: "user", text: "这段有没有语法错误？顺便说说 virtual reality 是啥" },
          {
            type: "reasoning_delta",
            text: "先逐句看一遍。第一句 He go to school every day. 主语是第三人称单数，go 应该用 goes。\n再看看 virtual reality 是不是专有名词，需要查词典和联网。",
          },
          { type: "assistant_message", text: "好，我先逐句看一遍，同时查一下这个短语。" },
          { type: "tool_call", id: "fx1", name: "grade_text", args: { text: "He go to school every day." } },
          { type: "tool_call", id: "fx2", name: "lookup_word", args: { word: "virtual reality" } },
          { type: "tool_call", id: "fx3", name: "web_search", args: { query: "virtual reality" } },
          { type: "tool_result", id: "fx1", ok: true, output: {}, durationMs: 1800 },
          { type: "tool_result", id: "fx2", ok: true, output: {}, durationMs: 100 },
          {
            type: "tool_result", id: "fx3", ok: true, durationMs: 2100,
            output: {
              query: "virtual reality",
              sources: [
                { title: "Virtual reality — 维基百科", url: "https://zh.wikipedia.org/wiki/Virtual_reality" },
                { title: "What is VR? — Britannica", url: "https://www.britannica.com/technology/virtual-reality" },
                { title: "virtual reality 释义 — 剑桥词典", url: "https://dictionary.cambridge.org/dictionary/english/virtual-reality" },
              ],
            },
          },
          { type: "assistant_message", text: "一共 3 处语法问题，已经放到右边的批改台了。\n\n`virtual reality` 指的是用电脑造出来的、能让人身临其境的虚拟环境。" },
          { type: "result", finishReason: "final_text" },
          { kind: "user", text: "那再帮我搜一下今年四级报名时间" },
          { type: "reasoning_delta", text: "用户想知道四级报名时间，这条信息有时效性，必须联网确认。" },
          { type: "assistant_message", text: "我搜一下。" },
          { type: "tool_call", id: "fx4", name: "web_search", args: { query: "2026 年四级报名时间" } },
          { type: "tool_result", id: "fx4", ok: false, error: "Connection refused", durationMs: 1200 },
          { type: "assistant_message", text: "联网没成功，我先按往年规律说：一般 3 月和 9 月各有一次报名。" },
          { type: "result", finishReason: "final_text" },
        ]);
      }, 0);
    } else if (demo === "ask" || demo === "askdone") {
      // ask_user 决策卡：ask=等待回答（可点选/打字/数字键，提交会因无后端而安全回退）；
      // askdone=已答收合态（可展开回看原问题）
      setTimeout(() => {
        const seed = (window as unknown as { __seedTimeline?: (steps: unknown[]) => void }).__seedTimeline;
        const askArgs = {
          question: "这篇作文按哪个考试的标准来评分？",
          options: [
            { label: "四级 CET-4 (Recommended)", description: "按四级 150 分制评分，标注常考扣分点" },
            { label: "六级 CET-6", description: "按六级评分习惯，对词汇和句式丰富度要求更高" },
            { label: "先不打分，只改错误", description: "这次只把语法和表达问题改对，分数以后再说" },
          ],
        };
        // 纯文本作文，绝不出现 ocr_image（没传图就不能有图片识别）。
        // 合理剧情：用户没说按什么标准，模型「动手批改前」先 ask_user 问清再批——
        // 不能等批改完了才问，也不能问用户已经说过 / 能推断出来的事。
        const essay = "Nowadays more and more people choose to work from home. I think it have both advantage and disadvantage.";
        const steps: unknown[] = [
          { kind: "user", text: "帮我看看这篇作文，看能怎么提高：" + essay },
          { type: "assistant_message", text: "作文我先通读了一遍。动手批改前先跟你确认一件事，免得按错了标准白批一遍：" },
          { type: "tool_call", id: "q1", name: "ask_user", args: askArgs },
        ];
        if (demo === "askdone") {
          // 选完标准后才真正批改、给分
          steps.push({ type: "tool_result", id: "q1", ok: true, output: { status: "answered", choice: "四级 CET-4", custom_text: "", summary: "" } });
          steps.push({ type: "tool_call", id: "ga", name: "grade_text", args: { text: essay } });
          steps.push({ type: "tool_result", id: "ga", ok: true, output: {} });
          steps.push({ type: "assistant_message", text: "好，就按四级标准。总分 106/150，一共 6 处问题，主要是主谓一致和名词单复数，我逐条给你列出来。" });
        }
        seed?.(steps);
      }, 0);
    } else if (demo === "usage") {
      // 用量状态条 + "这一轮没省到"两种形态的走查（不连后端、不花额度）。
      // 数字取真机实测量级：第一轮冷启动 0 命中（该解释一句），第二轮热、九成命中（干净）。
      setTimeout(() => {
        const seed = (window as unknown as { __seedTimeline?: (steps: unknown[]) => void }).__seedTimeline;
        seed?.([
          { kind: "user", text: "帮我改改这句：I very like English." },
          {
            type: "assistant_message",
            text: "一处要改：**very like** 是中文直译，英语里说 **like ... very much**，或者干脆用 **really like**。",
          },
          {
            type: "result",
            finishReason: "final_text",
            usage: { inputTokens: 15033, outputTokens: 106, cachedTokens: 0 },
            // 带上"最后一轮上下文"，用量环才会出现（不然只有状态条、没有环）
            contextInputTokens: 15900,
            contextWindow: 300000,
          },
          { kind: "user", text: "那我这个月先干什么？" },
          {
            type: "assistant_message",
            text: "先过一遍六级高频词，每周一篇作文，考前把真题阅读刷两遍。",
          },
          {
            type: "result",
            finishReason: "final_text",
            usage: { inputTokens: 31240, outputTokens: 487, cachedTokens: 29696 },
            contextInputTokens: 15900,
            contextWindow: 300000,
          },
        ]);
      }, 0);
    } else if (demo === "quiz" || demo === "quizread") {
      // quiz 作答卡：quiz=单题（点字母）；quizread=阅读（逐题点选后整串提交）
      setTimeout(() => {
        const seed = (window as unknown as { __seedTimeline?: (steps: unknown[]) => void }).__seedTimeline;
        if (demo === "quiz") {
          seed?.([
            { kind: "user", text: "给我出一道四级语法题练练" },
            { type: "assistant_message", text: "从本地题库挑了一道语法选择题，做完点下面的字母，我来对答案：" },
            { type: "tool_call", id: "gq", name: "get_quiz", args: { exam: "四级", qtype: "语法" } },
            { type: "tool_result", id: "gq", ok: true, output: { found: true, quiz_id: "demo-q1", qtype: "语法选择", body: "Choose the correct one to fill in the blank.\n\nNeither of the students ____ finished the homework.\n\nA. have    B. has    C. is    D. are" } },
          ]);
        } else {
          seed?.([
            { kind: "user", text: "来一篇四级阅读" },
            { type: "assistant_message", text: "一篇阅读理解，文章和题目在下面，逐题选完一起提交，我帮你判：" },
            { type: "tool_call", id: "gq", name: "get_quiz", args: { exam: "四级", qtype: "阅读理解" } },
            { type: "tool_result", id: "gq", ok: true, output: {
              found: true, quiz_id: "demo-r1", qtype: "阅读理解",
              passage: "Reading is to the mind what exercise is to the body. A strong body comes from repeated training, and a sharp mind in the same way comes from repeated reading. Regular reading builds vocabulary and sharpens thinking, just as regular exercise builds strength and health.",
              questions: [
                { number: 1, stem: "What is the main idea of the passage?", options: { A: "Exercise is better than reading.", B: "Reading benefits the mind as exercise benefits the body.", C: "People should read less.", D: "Vocabulary is hard to build." } },
                { number: 2, stem: "According to the passage, regular reading helps ___.", options: { A: "build strength", B: "cure illness", C: "build vocabulary and sharpen thinking", D: "save time" } },
              ],
            } },
          ]);
        }
      }, 0);
    } else if (demo === "plan") {
      // 计划提议卡：propose_plan 产出一份计划，用户确认后才落本地计划页
      setTimeout(() => {
        const seed = (window as unknown as { __seedTimeline?: (steps: unknown[]) => void }).__seedTimeline;
        const planTasks = [
          { t: "每天背 30 个四级核心词，当晚复习前一天的" },
          { t: "每周做一套四级真题听力，错题精听两遍" },
          { t: "每周写一篇作文，拍照发给我批改" },
          { t: "每两天精读一篇短文，积累 3 个好用句型" },
          { t: "考前两周每两天掐时间做一套完整真题" },
        ];
        seed?.([
          { kind: "user", text: "我想三个月内过四级，帮我规划下怎么准备" },
          { type: "assistant_message", text: "按四级和你现在的节奏，我拆成了一份能马上动手的计划。你看看合不合适，确认后就放进计划页，想改直接跟我说：" },
          { type: "tool_call", id: "pp", name: "propose_plan", args: { goal: "三个月内通过四级", tasks: planTasks } },
          { type: "tool_result", id: "pp", ok: true, output: { proposed: true, goal: "三个月内通过四级", tasks: planTasks, note: "等用户确认后再加入计划。" } },
        ]);
      }, 0);
    } else if (demo === "finalized") {
      // 已定稿：grade 结果 + 定稿卡（模拟提交成功后的时间线）
      setTimeout(() => {
        const seed = (window as unknown as { __seedTimeline?: (steps: unknown[]) => void }).__seedTimeline;
        const essay = "I goes to school yesterday. It were fun.";
        const dets = [
          { id: "d1", original: "goes", corrected: "went", type: "hard", tier: "error", explanation_zh: "yesterday 是过去的时间，动词要用过去式 went。", source: "model", error_code: null, start: 2, end: 6, aligned: true },
          { id: "d2", original: "were", corrected: "was", type: "hard", tier: "error", explanation_zh: "主语 It 是第三人称单数，be 动词过去式用 was。", source: "model", error_code: null, start: 31, end: 35, aligned: true },
        ];
        seed?.([
          { kind: "user", text: "帮我批改这篇作文：" + essay },
          { type: "tool_call", id: "g1", name: "grade_text", args: { text: essay } },
          { type: "tool_result", id: "g1", ok: true, output: { detections: dets, summary: "两处都是时态小错，改完就是正确的句子。", stats: { words: 8, error_count: 2, suggestion_count: 0, total: 2 }, scores: { content: { score: 66, comment_zh: "意思清楚，一句话把想法说明白了。" }, structure: { score: null, comment_zh: "单句不评篇章结构。" }, accuracy: { score: 58, comment_zh: "一句话里有 2 处时态错误，先把动词过去式练稳。" }, richness: { score: 55, comment_zh: "用词很基础，可尝试更具体的时间和动作表达。" } }, warnings: [] } },
        ]);
        setTimeout(() => {
          t.recordLocalFinalize({
            callId: "g1",
            finalText: "I went to school yesterday. It was fun.",
            comment: "",
            keptCount: 2,
            title: "My School Day",
            marks: [
              { kind: "plain", text: "I " },
              { kind: "removed", text: "goes" },
              { kind: "added", text: "went", index: 1, detection_id: "d1" },
              { kind: "plain", text: " to school yesterday. It " },
              { kind: "removed", text: "were" },
              { kind: "added", text: "was", index: 2, detection_id: "d2" },
              { kind: "plain", text: " fun." },
            ],
            explanations: dets.map(d => ({ original: d.original, corrected: d.corrected, explanationZh: d.explanation_zh, tier: d.tier })),
          });
        }, 100);
      }, 0);
    } else if (demo === "grade" || demo === "grade3" || demo === "canvas") {
      // 批阅拍板演示：真实 grade_text 结果结构（单篇 / 三篇混合 / 放大画布）
      setTimeout(() => {
        const seed = (window as unknown as { __seedTimeline?: (steps: unknown[]) => void }).__seedTimeline;
        const essay = "Everyone has a dream job. My dream job is to be a doctor, because I want to help people who are sick. Although being a doctor is very tired, but I will study hard to make my dream come true.";
        const span = (s: string) => {
          const i = essay.indexOf(s);
          return { start: i, end: i + s.length, aligned: i >= 0 };
        };
        const dets = [
          { id: "d1", original: "very tired", corrected: "very tiring", type: "hard", tier: "error", explanation_zh: "形容一件事「累人」用 tiring；tired 只用来形容人感到累。", source: "model", error_code: null, ...span("very tired") },
          { id: "d2", original: "Although being a doctor is very tired, but", corrected: "Although being a doctor is very tired,", type: "hard", tier: "error", explanation_zh: "although 和 but 不能同时用，一个复合句里留一个就够了。", source: "model", error_code: null, ...span("Although being a doctor is very tired, but") },
          { id: "d3", original: "help people who are sick", corrected: "treat sick patients", type: "chinglish", tier: "suggestion", explanation_zh: "语法没错，但医生「治病救人」更地道的说法是 treat sick patients，书面感更强。", source: "model", error_code: null, ...span("help people who are sick") },
        ];
        const gradeOut = {
          detections: dets,
          summary: "内容完整，逻辑清楚。两处小错不影响理解，改完就是一篇干净的短文。",
          stats: { words: 38, error_count: 2, suggestion_count: 1, total: 3 },
          scores: {
            content: { score: 78, comment_zh: "切题、观点完整，篇幅偏短，医生这个理想可以再写一两个具体理由支撑。" },
            structure: { score: 72, comment_zh: "有开头有结论，整体连贯；句子之间衔接词偏少，可分两句、加一层递进。" },
            accuracy: { score: 74, comment_zh: "38 词里有 2 处语法小错（tiring、although/but 连用），改掉后准确度会明显上来。" },
            richness: { score: 62, comment_zh: "词汇和句式偏单一，多用简单句；可尝试替换高级词、加入从句或长短句结合。" },
          },
          warnings: [],
        };
        if (demo === "grade") {
          seed?.([
            { kind: "user", text: "帮我批改这篇作文：" + essay },
            { type: "tool_call", id: "g1", name: "grade_text", args: { text: essay } },
            { type: "tool_result", id: "g1", ok: true, output: gradeOut },
          ]);
          return;
        }
        // grade3：篇1 待拍板（文本）、篇2 待核对（照片）、篇3 批改中（文本）
        const p1 = mkPhotoDataUrl();
        seed?.([
          { kind: "user", text: "帮我批改这篇作文：" + essay },
          { type: "tool_call", id: "g1", name: "grade_text", args: { text: essay } },
          { type: "tool_result", id: "g1", ok: true, output: gradeOut },
          { kind: "user", text: "这张也转成文字，等我核对", images: [{ name: "IMG_2050.jpg", url: p1 }] },
          { type: "tool_call", id: "oa", name: "ocr_image", args: { image_id: "ix" } },
          { type: "tool_result", id: "oa", ok: true, output: { text: "Last weekend I get up early. I [?] to the zoo with my mother.", low_confidence: [{ start: 31, end: 34, text: "[?]", reason: "字迹模糊" }], warnings: [], image_id: "ix" } },
          { kind: "user", text: "再批改这篇：I goes to the library every weekend." },
          { type: "tool_call", id: "g2", name: "grade_text", args: { text: "I goes to the library every weekend." } },
        ]);
        // canvas：播种后等面板挂载，再打开放大并排画布
        if (demo === "canvas") {
          setTimeout(() => useStore.getState().setCanvasMode(true), 120);
        }
      }, 0);
    } else if (demo === "fail1") {
      // 单篇失败：单张照片转写失败，single 面板直接给重试（无货架）
      const p1 = mkPhotoDataUrl();
      setTimeout(() => {
        const seed = (window as unknown as { __seedTimeline?: (steps: unknown[]) => void }).__seedTimeline;
        seed?.([
          { kind: "user", text: "把这张照片转成文字，我核对后再批改", images: [{ name: "IMG_2066.jpg", url: p1 }] },
          { type: "tool_call", id: "oa", name: "ocr_image", args: { image_id: "ic" } },
          { type: "tool_result", id: "oa", ok: false, error: "视觉模型额度已用完" },
        ]);
      }, 0);
    } else if (demo === "fail3") {
      // 失败隔离演示：篇1 待拍板（文本）、篇2 转写失败（照片）、篇3 批改失败（文本）
      const p1 = mkPhotoDataUrl();
      setTimeout(() => {
        const seed = (window as unknown as { __seedTimeline?: (steps: unknown[]) => void }).__seedTimeline;
        const essay = "Everyone has a dream job. My dream job is to be a doctor, because I want to help people who are sick.";
        const i = essay.indexOf("want to help people who are sick");
        const gradeOut = {
          detections: [
            { id: "d1", original: "want to help people who are sick", corrected: "treat sick patients", type: "chinglish", tier: "suggestion", explanation_zh: "「治病救人」更地道的说法是 treat sick patients。", source: "model", error_code: null, start: i, end: i + 31, aligned: true },
          ],
          summary: "内容完整，一处表达可以更地道。", stats: { words: 26, error_count: 0, suggestion_count: 1, total: 1 },
          scores: {
            content: { score: 76, comment_zh: "理想和理由都写清楚了，可再补一个具体打算让内容更充实。" },
            structure: { score: null, comment_zh: "单句为主，暂不评篇章结构。" },
            accuracy: { score: 88, comment_zh: "没有语法硬错，只有一处表达可以更地道，准确度不错。" },
            richness: { score: 68, comment_zh: "句式正确但偏简单，可积累更地道的职业话题搭配。" },
          },
          warnings: [],
        };
        seed?.([
          { kind: "user", text: "帮我批改这篇作文：" + essay },
          { type: "tool_call", id: "g1", name: "grade_text", args: { text: essay } },
          { type: "tool_result", id: "g1", ok: true, output: gradeOut },
          { kind: "user", text: "这张也转成文字，等我核对", images: [{ name: "IMG_2060.jpg", url: p1 }] },
          { type: "tool_call", id: "oa", name: "ocr_image", args: { image_id: "ib" } },
          { type: "tool_result", id: "oa", ok: false, error: "识别服务暂时没有响应" },
          { kind: "user", text: "再批改这篇：I goes to the library every weekend." },
          { type: "tool_call", id: "g2", name: "grade_text", args: { text: "I goes to the library every weekend." } },
          { type: "tool_result", id: "g2", ok: false, error: "批改服务暂时没有响应" },
        ]);
        if (new URLSearchParams(location.search).get("canvas") === "1") {
          setTimeout(() => useStore.getState().setCanvasMode(true), 120);
        }
      }, 0);
    } else if (demo === "ocr2" || demo.startsWith("board")) {
      const p1 = mkPhotoDataUrl();
      const p2 = mkPhotoDataUrl();
      // Provider 的 __seedTimeline 注册 effect 在本组件之后跑，延迟一帧再播种
      setTimeout(() => {
        const seed = (window as unknown as { __seedTimeline?: (steps: unknown[]) => void }).__seedTimeline;
        if (demo === "ocr2") {
          // 两张都转写完成、待核对（一张 1 处没看清，一张全看清）
          seed?.([
            { kind: "user", text: "这两张照片转成文字，我核对后再批改", images: [{ name: "IMG_2041.jpg", url: p1 }, { name: "IMG_2042.jpg", url: p2 }] },
            { type: "tool_call", id: "oa", name: "ocr_image", args: { image_id: "ia" } },
            { type: "tool_result", id: "oa", ok: true, output: { text: "Last weekend I get up early. I [?] to the zoo with my mother. We see many animal there. It was a happy day.", low_confidence: [{ start: 31, end: 34, text: "[?]", reason: "字迹模糊" }], warnings: [], image_id: "ia" } },
            { type: "tool_call", id: "ob", name: "ocr_image", args: { image_id: "ib" } },
            { type: "tool_result", id: "ob", ok: true, output: { text: "Dear Tom, thank you for your last letter. How are you? I am fine. I went to the park last Saturday.", low_confidence: [], warnings: [], image_id: "ib" } },
          ]);
          return;
        }
        seed?.([
          { kind: "user", text: "帮我批改这篇作文：My Dream Job. I want to be a doctor." },
          { type: "tool_call", id: "g0", name: "grade_text", args: { text: "My Dream Job. I want to be a doctor." } },
          { kind: "user", text: "再把这两张转成文字，和上一篇一起批改", images: [{ name: "IMG_2041.jpg", url: p1 }, { name: "IMG_2042.jpg", url: p2 }] },
          { type: "tool_call", id: "oa", name: "ocr_image", args: { image_id: "ia" } },
          { type: "tool_result", id: "oa", ok: true, output: { text: "Dear Tom, thank you for your last letter. How are you?", low_confidence: [{ start: 0, end: 3, text: "[?]", reason: "字迹模糊" }], warnings: [], image_id: "ia" } },
          { type: "tool_call", id: "ob", name: "ocr_image", args: { image_id: "ib" } },
        ]);
      }, 0);
    }
  }, []);

  useEffect(() => {
    const el = streamRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [t.timeline]);

  // 没接模型时不把消息发出去：直接问后端"现在有哪些模型"，
  // 空清单就地提示，别让后端报一串看不懂的错。
  const [noModel, setNoModel] = useState(false);
  useEffect(() => {
    if (import.meta.env.DEV && new URLSearchParams(location.search).get("nomodel") === "1") setNoModel(true);
  }, []);
  const send = async (text: string, images: Uploaded[]) => {
    let hasModel = true;
    try {
      const res = await fetch("/api/models");
      const data = (await res.json()) as { models?: unknown[] };
      hasModel = Array.isArray(data.models) && data.models.length > 0;
    } catch {
      hasModel = true; // 连不上后端就别拦，让真正的错误照常暴露
    }
    if (!hasModel) {
      setNoModel(true);
      window.setTimeout(() => setNoModel(false), 6000);
      return;
    }
    t.send(text, images);
  };

  // 欢迎页 / 对话页完全由「当前会话时间线是否为空」派生：
  // 发消息会乐观追加用户节点、打开历史会话会回放、新建/删除会清空——三种情况都自动正确，
  // 不再手动维护一个 welcome/chat 开关。
  const empty = t.timeline.nodes.length === 0;
  // ask_user 等待回答时，决策卡钉在底部输入区（替换输入框），不随时间线滚动、不被遮挡
  const waitingAsk = t.timeline.nodes.find(n => n.kind === "ask" && n.state === "waiting");

  return (
    <section className="view" id="view-ask" hidden={hidden} style={hidden ? undefined : { overflow: "hidden" }}>
      {noModel ? (
        <div className="ask3-nomodel" role="status">
          <span>还没接模型，消息发不出去。</span>
          <button type="button" onClick={() => onOpenSettings?.()}>去设置接一个</button>
        </div>
      ) : null}
      <div className={"ask3"
        + (!empty && panelMode !== "none" ? " wb-open" : "")
        + (!empty && panelMode !== "none" && canvasMode ? " canvas-mode" : "")}>
        <section className="ask3-conv" aria-label="对话" style={{ display: "flex", flexDirection: "column", minHeight: 0 }}>
          {/* 首屏空态 */}
          <div className="ask3-welcome" id="askWelcome" hidden={!empty}
            style={{ display: empty ? "flex" : "none" }}>
            {/* 上半：在"输入框以上那块空间"里垂直居中，再从正中上移一点 */}
            <div className="wc-top">
              <p className="wc-eyebrow">{formatEyebrow()}</p>
              <p className="wc-line" id="wcLine">{greeting(new Date().getHours())}</p>
              <PlanStrip tasks={tasks} />
              {/* 继续上次：没有会话就整行不显示。
                  原来这里摆五颗快捷入口，实测 0 次命中，已删。 */}
              {lastSession ? (
                <button type="button" className="wc-resume"
                  onClick={() => t.openSession(lastSession.session_id)}
                  title="接着上次那条会话继续">
                  <span className="wc-resume-k">继续上次：</span>
                  <span className="wc-resume-t">{lastSession.title}</span>
                  <span className="wc-resume-w">{sessionWhen(lastSession.created_at)}</span>
                </button>
              ) : null}
            </div>
            {/* 下半：输入区贴底 */}
            <div className="wc-foot">
              <Composer onSend={send} running={running} onStop={t.stop} />
            </div>
          </div>

          {/* 会话时间线：真实 /api/ask 节点流（细节照原版 Transcript） */}
          <div className="ask3-scroll" id="askChat" ref={streamRef}
            hidden={empty}
            style={{ display: empty ? "none" : "flex", flexDirection: "column", gap: 14 }}>
            <Transcript
              nodes={t.timeline.nodes}
              running={running}
              onRetry={t.reconnect}
              missNotes={missNotes}
            />
            {t.notice ? (
              <div className="finrow" style={{ color: "var(--sem-yellow-ink)" }}>
                {t.notice}
                <button className="mc-link" style={{ marginLeft: "auto" }} onClick={t.dismissNotice}>知道了</button>
              </div>
            ) : null}
          </div>

          <div id="cmpDock" hidden={empty} style={{ display: empty ? "none" : "block" }}>
            {waitingAsk && waitingAsk.kind === "ask" ? (
              /* 等待拍板：决策卡钉底浮层，替掉输入框（卡内「我自己回答」可打字），答完恢复 */
              <div className="ask-layer">
                <AskCard node={waitingAsk} onAnswer={t.submitAskAnswer} />
              </div>
            ) : (
              <>
                <NarrowWorkEntryBar />
<Composer onSend={send} running={running} onStop={t.stop} />
              </>
            )}
          </div>
          <div className="askfoot" id="askFoot" hidden={empty || !!waitingAsk || online}>
            <span className="rdot" aria-hidden style={{ background: "rgb(var(--ink) /.35)" }} />
            <span>暂时连不上，稍后重试</span>
          </div>
        </section>

        {/* 右翼：单/多篇统一工作面板（OCR 核对、批阅拍板）；纯问答/处理中/全定稿时自隐 */}
        {!empty ? <BatchBoard /> : null}

        {/* 放大并排画布：OCR 核对/批阅时刻全屏铺满（货架仍挂载，配额 effect 不中断） */}
        {!empty ? <CanvasStage /> : null}
      </div>
      {/* 快速添加的隐藏入口（计划页用） */}
    </section>
  );
}
