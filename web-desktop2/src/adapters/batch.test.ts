import { describe, expect, it } from "vitest";
import { emptyTimeline, reduce, replayToTimeline, type AskEvent, type Timeline, type TimelineAttachment } from "./askTimeline";
import { BATCH_MAX, batchSummary, canvasGridClass, canAddToTray, capacityRemaining, deriveBatch, isMultiBatch, itemStep, narrowWorkEntry, openCount, splitConfidenceMarkers, stageInfo, workPanelMode, type Batch, type BatchItem, type TrayItem } from "./batch";

/** 把一串事件打进时间线，测试里用来造各种批次进度 */
function timelineOf(events: AskEvent[]): Timeline {
  return events.reduce<Timeline>((tl, ev, i) => reduce(tl, ev, 1000 + i), emptyTimeline());
}

type UserStep = { user: { text?: string; images?: { name: string; url?: string; imageId?: string }[] } };
const isUserStep = (s: AskEvent | UserStep): s is UserStep => "user" in s;

/**
 * 按真实发生顺序装配时间线：用户回合与工具事件交错排列
 *（多回合场景里，上一回合的工具必须排在下一回合 user 之前，否则篇的顺序会失真）。
 */
function timelineSteps(steps: Array<AskEvent | UserStep>): Timeline {
  let tl = emptyTimeline();
  let userIdx = 0;
  const pushUser = (u: UserStep["user"]) => {
    const attachments: TimelineAttachment[] = (u.images ?? []).map((im, j) => ({
      id: `up-${userIdx}-${j}`,
      name: im.name,
      kind: "image",
      url: im.url,
      imageId: im.imageId,
    }));
    tl = {
      ...tl,
      seq: tl.seq + 1,
      nodes: [...tl.nodes, {
        id: `u-${userIdx}`, kind: "user" as const, text: u.text ?? "", attachments, sortKey: tl.seq,
      }],
    };
    userIdx += 1;
  };
  steps.forEach((s, i) => {
    if (isUserStep(s)) pushUser(s.user);
    else tl = reduce(tl, s, 2000 + i);
  });
  return tl;
}

/** 单回合便捷封装：一个 user（可带图）+ 其后的一串事件 */
function timelineWithUsers(
  user: { text?: string; images?: { name: string; url?: string; imageId?: string }[] },
  events: AskEvent[],
): Timeline {
  return timelineSteps([{ user }, ...events]);
}

describe("deriveBatch · 没有批改行为时不产生批次", () => {
  it("空时间线：0 篇、批次视为已关闭", () => {
    const b = deriveBatch(emptyTimeline());
    expect(b.items).toHaveLength(0);
    expect(b.closed).toBe(true);
  });

  it("普通问答（只有文字、没有 grade/ocr 工具）不产生任何篇", () => {
    const tl = timelineOf([
      { type: "tool_call", id: "q1", name: "mini_dict", args: { word: "commit" } },
      { type: "tool_result", id: "q1", ok: true, output: {} },
      { type: "assistant_message", text: "commit 是动词，意思是犯（罪、错）。" },
      { type: "result", finishReason: "final_text" },
    ]);
    expect(deriveBatch(tl).items).toHaveLength(0);
  });
});

describe("deriveBatch · 纯文本作文", () => {
  const essay = "My Dream Job\nEveryone has a dream job. Although being a doctor is very tired, but I will not give up.";
  const gradeOutput = {
    detections: [
      { id: "d1", original: "very tired, but", corrected: "very tired,", tier: "error", type: "hard", explanation_zh: "although 与 but 不连用" },
      { id: "d2", original: "very tired", corrected: "very tiring", tier: "suggestion", type: "hard", explanation_zh: "-ing 修饰职业" },
    ],
    summary: "内容完整，两处小错。",
    stats: { words: 20, error_count: 1, suggestion_count: 1, total: 2 },
    warnings: [],
  };

  it("grade_text 调用中：1 篇文本、状态 grading、标题取首行", () => {
    const tl = timelineOf([{ type: "tool_call", id: "g1", name: "grade_text", args: { text: essay } }]);
    const b = deriveBatch(tl);
    expect(b.items).toHaveLength(1);
    const it = b.items[0];
    expect(it.origin).toBe("text");
    expect(it.status).toBe("grading");
    expect(it.gradeCallId).toBe("g1");
    expect(it.title).toBe("My Dream Job");
    expect(b.closed).toBe(false);
  });

  it("grade_text 成功返回：状态转 needs_decision，detections/summary/stats 就位", () => {
    const tl = timelineOf([
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: essay } },
      { type: "tool_result", id: "g1", ok: true, output: gradeOutput },
    ]);
    const it = deriveBatch(tl).items[0];
    expect(it.status).toBe("needs_decision");
    expect(it.grade?.detections).toHaveLength(2);
    expect(it.grade?.summary).toBe("内容完整，两处小错。");
    expect(it.grade?.stats?.error_count).toBe(1);
  });

  it("finalize 节点到达（按 grade_call_id 关联）：状态 done、记下定稿成果，批次关闭", () => {
    const tl = timelineOf([
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: essay } },
      { type: "tool_result", id: "g1", ok: true, output: gradeOutput },
      { type: "result", finishReason: "final_text" },
    ]);
    // finalize 节点由历史回放/落账后出现；这里直接构造一个挂在时间线尾部
    tl.nodes.push({
      id: "f-g1", kind: "finalize", callId: "g1",
      finalText: "My Dream Job\nEveryone has…very tiring, I will not give up.",
      comment: "", keptCount: 2, sortKey: 99,
    });
    const b = deriveBatch(tl);
    expect(b.items).toHaveLength(1);
    expect(b.items[0].status).toBe("done");
    expect(b.items[0].final?.keptCount).toBe(2);
    expect(b.closed).toBe(true);
  });
});

describe("图片展示名 · 哈希文件名兜底", () => {
  const ocrOk = { text: "Some essay text here.", low_confidence: [], warnings: [], image_id: "x" };

  it("正常文件名去扩展名；32 位十六进制哈希名（粘贴截图）兜底成「图片作文」", () => {
    const normal = timelineSteps([
      { user: { images: [{ name: "2026年6月四级第1套·学生习作（图片版）.png" }] } },
      { type: "tool_call", id: "o1", name: "ocr_image", args: { image_id: "x" } },
      { type: "tool_result", id: "o1", ok: true, output: ocrOk },
    ]);
    expect(deriveBatch(normal).items[0].title).toBe("2026年6月四级第1套·学生习作（图片版）");

    const hashed = timelineSteps([
      { user: { images: [{ name: "637b04f3e41d5a62616f222e8c11a5eb.jpg" }] } },
      { type: "tool_call", id: "o1", name: "ocr_image", args: { image_id: "x" } },
      { type: "tool_result", id: "o1", ok: true, output: ocrOk },
    ]);
    expect(deriveBatch(hashed).items[0].title).toBe("图片作文");
  });

  it("同一批两张粘贴截图：依次叫「图片作文」「图片作文 2」，不重复", () => {
    const tl = timelineSteps([
      { user: { images: [
        { name: "637b04f3e41d5a62616f222e8c11a5eb.jpg" },
        { name: "898cfadb1aaf4fcb9ce0f55c23db698d.png" },
      ] } },
      { type: "tool_call", id: "oa", name: "ocr_image", args: { image_id: "xa" } },
      { type: "tool_result", id: "oa", ok: true, output: { ...ocrOk, image_id: "xa", text: "FIRST PAGE" } },
      { type: "tool_call", id: "ob", name: "ocr_image", args: { image_id: "xb" } },
      { type: "tool_result", id: "ob", ok: true, output: { ...ocrOk, image_id: "xb", text: "SECOND PAGE" } },
    ]);
    const titles = deriveBatch(tl).items.map((i) => i.title);
    expect(titles).toEqual(["图片作文", "图片作文 2"]);
  });
});

describe("deriveBatch · 图片作文 OCR 全流程", () => {
  const transcript = "Last weekend I get up early. I [?] to the zoo with my mother. We see many animal.";
  const ocrOut = {
    text: transcript,
    low_confidence: [{ start: 28, end: 31, text: "[?]", reason: "模型未能确认此处内容，请对照原图核对" }],
    warnings: ["有 1 处模型没把握，已用 [?] 标出，请对照原图重点核对"],
    image_id: "abc123",
  };

  it("只发了图、还没任何批改/OCR 行为：不产生批次（用户没说要干嘛，不许预先入队）", () => {
    const tl = timelineWithUsers({ text: "", images: [{ name: "IMG_2041.jpg", url: "blob:x" }] }, []);
    expect(deriveBatch(tl).items).toHaveLength(0);
  });

  it("用户说了『批改』但 OCR 还没调：仍不产生批次——入队以 ocr_image 调用为准，不以图片/意图文案为准", () => {
    const tl = timelineWithUsers({ text: "帮我批改", images: [{ name: "IMG_2041.jpg", url: "blob:x" }] }, []);
    expect(deriveBatch(tl).items).toHaveLength(0);
  });

  it("先 describe_image 看图、后改口批改再调 ocr_image：图片篇仍带原图与文件名，不许变孤儿（BUG：先看图导致『原图不可用』）", () => {
    const tl = timelineSteps([
      { user: { text: "", images: [{ name: "2026年6月四级第1套·学生习作（图片版）.png", url: "blob:essay" }] } },
      { type: "tool_call", id: "d1", name: "describe_image", args: { image_id: "13845b7b" } },
      { type: "tool_result", id: "d1", ok: true, output: { description: "一篇关于机器人的四级习作" } },
      { user: { text: "帮我批改一下" } },
      { type: "tool_call", id: "o1", name: "ocr_image", args: { image_id: "13845b7b" } },
      { type: "tool_result", id: "o1", ok: true, output: ocrOut },
    ]);
    const items = deriveBatch(tl).items;
    expect(items).toHaveLength(1);
    expect(items[0].orphan).toBeUndefined();
    expect(items[0].title).toBe("2026年6月四级第1套·学生习作（图片版）");
    expect(items[0].url).toBe("blob:essay");
    expect(items[0].status).toBe("needs_review");
  });

  it("ocr_image 进行中 → transcribing；成功返回 → needs_review，转写稿/[?]/image_id 就位", () => {
    const running = timelineWithUsers(
      { text: "批改", images: [{ name: "IMG_2041.jpg" }] },
      [{ type: "tool_call", id: "o1", name: "ocr_image", args: { image_id: "abc123" } }],
    );
    expect(deriveBatch(running).items[0].status).toBe("transcribing");

    const done = timelineWithUsers(
      { text: "批改", images: [{ name: "IMG_2041.jpg" }] },
      [
        { type: "tool_call", id: "o1", name: "ocr_image", args: { image_id: "abc123" } },
        { type: "tool_result", id: "o1", ok: true, output: ocrOut },
      ],
    );
    const it = deriveBatch(done).items[0];
    expect(it.status).toBe("needs_review");
    expect(it.transcript).toBe(transcript);
    expect(it.lowConfidence).toHaveLength(1);
    expect(it.imageId).toBe("abc123");
    expect(it.warnings?.[0]).toContain("[?]");
    expect(it.title).toBe("IMG_2041");
    expect(it.url).toBeUndefined(); // 该用例没给 url
  });

  it("grade 文本是『斜杠转写稿』的干净版（模型去掉了 / 与粘连标点）时仍归入同一篇，不开第二个批改台", () => {
    const slashed = "Nowadays-, / technology / develop / very / fast-. / I / believe / robots / are / useful-.";
    const cleaned = "Nowadays, technology develop very fast. I believe robots are useful.";
    const tl = timelineSteps([
      { user: { text: "批改", images: [{ name: "essay.png" }] } },
      { type: "tool_call", id: "o1", name: "ocr_image", args: { image_id: "x1" } },
      { type: "tool_result", id: "o1", ok: true, output: { ...ocrOut, image_id: "x1", text: slashed } },
      { user: { text: "这篇照片作文的转写稿我已核对确认，请据此批改" } },
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: cleaned } },
      {
        type: "tool_result", id: "g1", ok: true,
        output: { detections: [{ id: "d1" }], summary: "", stats: { words: 10, error_count: 1, suggestion_count: 0, total: 1 }, warnings: [] },
      },
    ]);
    const b = deriveBatch(tl, { "item-u-0-img-0": slashed });
    expect(b.items).toHaveLength(1);
    expect(b.items[0].origin).toBe("image");
    expect(b.items[0].gradeCallId).toBe("g1");
  });

  it("grade 的文本等于转写稿时归入同一篇（不重复建篇），走完到批改完成始终 1 篇", () => {
    const tl = timelineSteps([
      { user: { text: "批改这张", images: [{ name: "IMG_2041.jpg" }] } },
      { type: "tool_call", id: "o1", name: "ocr_image", args: { image_id: "abc123" } },
      { type: "tool_result", id: "o1", ok: true, output: ocrOut },
      { user: { text: "转写无误，批改吧" } },
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: transcript } },
      {
        type: "tool_result", id: "g1", ok: true,
        output: { detections: [{ id: "d1" }], summary: "时态错误为主", stats: { words: 18, error_count: 3, suggestion_count: 0, total: 3 }, warnings: [] },
      },
    ]);
    const b = deriveBatch(tl);
    expect(b.items).toHaveLength(1);
    const it = b.items[0];
    expect(it.origin).toBe("image");
    expect(it.ocrCallId).toBe("o1");
    expect(it.gradeCallId).toBe("g1");
    expect(it.status).toBe("needs_decision");
    expect(it.grade?.detections).toHaveLength(1);
  });

  it("两张图并行 OCR：ocr 调用按宣告顺序与图片配对（标题/预览不串位）", () => {
    const tl = timelineSteps([
      { user: { text: "两张都批改", images: [{ name: "first.jpg", url: "blob:1" }, { name: "second.jpg", url: "blob:2" }] } },
      { type: "tool_call", id: "oa", name: "ocr_image", args: { image_id: "id-a" } },
        { type: "tool_call", id: "ob", name: "ocr_image", args: { image_id: "id-b" } },
        { type: "tool_result", id: "oa", ok: true, output: { ...ocrOut, image_id: "id-a", text: "FIRST PAGE TEXT" } },
        { type: "tool_result", id: "ob", ok: true, output: { ...ocrOut, image_id: "id-b", text: "SECOND PAGE TEXT" } },
    ]);
    const items = deriveBatch(tl).items;
    expect(items).toHaveLength(2);
    expect(items[0].title).toBe("first");
    expect(items[0].url).toBe("blob:1");
    expect(items[0].transcript).toBe("FIRST PAGE TEXT");
    expect(items[1].title).toBe("second");
    expect(items[1].url).toBe("blob:2");
    expect(items[1].transcript).toBe("SECOND PAGE TEXT");
  });
});

describe("deriveBatch · 只发图的看图路线（describe_image 摘篇）", () => {
  it("describe_image 带 image_id 且附件可配对：精确摘掉那一篇，批次归空", () => {
    const tl = timelineWithUsers(
      { text: "", images: [{ name: "photo.png", url: "blob:x", imageId: "img-abc" }] },
      [
        { type: "tool_call", id: "d1", name: "describe_image", args: { image_id: "img-abc" } },
        { type: "tool_result", id: "d1", ok: true, output: { description: "一只猫趴在窗台上" } },
      ],
    );
    const b = deriveBatch(tl);
    expect(b.items).toHaveLength(0);
    expect(b.closed).toBe(true);
  });

  it("活流里附件还没有 uuid（对不上 args.image_id）：按声明顺序摘掉最早一篇还在排队的", () => {
    const tl = timelineWithUsers(
      { text: "", images: [{ name: "photo.png", url: "blob:x" }] },
      [{ type: "tool_call", id: "d1", name: "describe_image", args: { image_id: "uuid-from-upload" } }],
    );
    expect(deriveBatch(tl).items).toHaveLength(0);
  });

  it("describe_image 不碰已经进入 OCR 流程的篇，只摘仍在排队的", () => {
    const tl = timelineWithUsers(
      {
        text: "",
        images: [
          { name: "a.png", url: "blob:a", imageId: "img-a" },
          { name: "b.png", url: "blob:b", imageId: "img-b" },
        ],
      },
      [
        { type: "tool_call", id: "o1", name: "ocr_image", args: { image_id: "img-a" } },
        { type: "tool_call", id: "d1", name: "describe_image", args: { image_id: "img-b" } },
      ],
    );
    const b = deriveBatch(tl);
    expect(b.items).toHaveLength(1);
    expect(b.items[0].title).toBe("a");
    expect(b.items[0].status).toBe("transcribing");
  });

  it("没有排队图时 describe_image 是空操作（描述历史图片不误伤批次）", () => {
    const tl = timelineWithUsers({ text: "看看这张", images: [] }, [
      { type: "tool_call", id: "d1", name: "describe_image", args: { image_id: "img-old" } },
    ]);
    expect(deriveBatch(tl).items).toHaveLength(0);
  });
});

describe("deriveBatch · 会话累积与失败隔离", () => {
  it("上一轮的文本篇还没定稿，这一轮再发两张图：自动并成 3 篇同一批", () => {
    // 回合 1：一篇文本已批改完、等拍板（未定稿）
    // 回合 2：两张图，一张核对中、一张还在转写
    const tl = timelineSteps([
      { user: { text: "帮我批改这篇作文：My Dream Job. I want to be a doctor." } },
      { type: "tool_call", id: "g0", name: "grade_text", args: { text: "My Dream Job. I want to be a doctor." } },
      { type: "tool_result", id: "g0", ok: true, output: { detections: [{ id: "d1" }], summary: "不错", stats: { words: 10, error_count: 1, suggestion_count: 0, total: 1 }, warnings: [] } },
      { user: { text: "再把这两张图转成文字，和上一篇一起批改", images: [{ name: "letter.jpg" }, { name: "weekend.jpg" }] } },
      { type: "tool_call", id: "o1", name: "ocr_image", args: { image_id: "id-letter" } },
      { type: "tool_result", id: "o1", ok: true, output: { text: "Dear Tom, thank you for your letter.", low_confidence: [], warnings: [], image_id: "id-letter" } },
      { type: "tool_call", id: "o2", name: "ocr_image", args: { image_id: "id-weekend" } },
    ]);
    const b = deriveBatch(tl);
    expect(b.items).toHaveLength(3);
    expect(openCount(b.items)).toBe(3);
    expect(b.closed).toBe(false);
    expect(b.items.map((x) => x.status)).toEqual(["needs_decision", "needs_review", "transcribing"]);
    expect(b.items.map((x) => x.title)).toEqual(["My Dream Job.", "letter", "weekend"]);
  });

  it("某一篇批改失败：只红这一篇（grade_failed），其他两篇进度不受影响", () => {
    const tl = timelineSteps([
      { user: { text: "三篇一起改", images: [{ name: "a.jpg" }, { name: "b.jpg" }, { name: "c.jpg" }] } },
      { type: "tool_call", id: "oa", name: "ocr_image", args: { image_id: "ia" } },
        { type: "tool_result", id: "oa", ok: true, output: { text: "AAAAAAAAAA text one here", low_confidence: [], warnings: [], image_id: "ia" } },
        { type: "tool_call", id: "ob", name: "ocr_image", args: { image_id: "ib" } },
        { type: "tool_result", id: "ob", ok: true, output: { text: "BBBBBBBBBB text two here", low_confidence: [], warnings: [], image_id: "ib" } },
        { type: "tool_call", id: "oc", name: "ocr_image", args: { image_id: "ic" } },
        { type: "tool_result", id: "oc", ok: true, output: { text: "CCCCCCCCCC text three here", low_confidence: [], warnings: [], image_id: "ic" } },
        { type: "tool_call", id: "ga", name: "grade_text", args: { text: "AAAAAAAAAA text one here" } },
        { type: "tool_result", id: "ga", ok: true, output: { detections: [], summary: "ok", stats: { words: 5, error_count: 0, suggestion_count: 0, total: 0 }, warnings: [] } },
        { type: "tool_call", id: "gb", name: "grade_text", args: { text: "BBBBBBBBBB text two here" } },
        { type: "tool_result", id: "gb", ok: true, output: { detections: [], summary: "ok", stats: { words: 5, error_count: 0, suggestion_count: 0, total: 0 }, warnings: [] } },
        { type: "tool_call", id: "gc", name: "grade_text", args: { text: "CCCCCCCCCC text three here" } },
        { type: "tool_result", id: "gc", ok: false, error: "本地后端超时" },
    ]);
    const items = deriveBatch(tl).items;
    expect(items).toHaveLength(3);
    expect(items[0].status).toBe("needs_decision");
    expect(items[1].status).toBe("needs_decision");
    expect(items[2].status).toBe("grade_failed");
    expect(items[2].error).toBe("本地后端超时");
    expect(openCount(items)).toBe(3);
  });

  it("OCR 失败只卡这一篇（ocr_failed），已核对的另一篇照样可以进批改", () => {
    const tl = timelineSteps([
      { user: { text: "两张", images: [{ name: "ok.jpg" }, { name: "bad.jpg" }] } },
      { type: "tool_call", id: "o1", name: "ocr_image", args: { image_id: "i1" } },
      { type: "tool_result", id: "o1", ok: true, output: { text: "OOOOOOOOOO good essay text", low_confidence: [], warnings: [], image_id: "i1" } },
      { type: "tool_call", id: "o2", name: "ocr_image", args: { image_id: "i2" } },
      { type: "tool_result", id: "o2", ok: false, error: "视觉模型调用失败" },
    ]);
    const items = deriveBatch(tl).items;
    expect(items.map((x) => x.status)).toEqual(["needs_review", "ocr_failed"]);
    expect(items[1].error).toBe("视觉模型调用失败");
  });
});

describe("批次上限、单篇隐身与步骤条", () => {
  const tray = (n: number): TrayItem[] =>
    Array.from({ length: n }, (_, i) => ({ id: `t${i}`, kind: "image" as const, name: `t${i}.jpg` }));

  it("容量上限 BATCH_MAX=3：空批 3 个名额；1 篇在途剩 2 个；托盘占名额", () => {
    expect(BATCH_MAX).toBe(3);
    const emptyItems = deriveBatch(emptyTimeline()).items;
    expect(capacityRemaining(emptyItems, [])).toBe(3);
    // canAddToTray 问的是「托盘当前状态下还能不能再追加一张」
    expect(canAddToTray(emptyItems, tray(0))).toBe(true);
    expect(canAddToTray(emptyItems, tray(2))).toBe(true); // 2 张时还能追加第 3 张
    expect(canAddToTray(emptyItems, tray(3))).toBe(false); // 已 3 张，不能第 4 张

    // 已有 1 篇在途（未定稿）时，只剩 2 个名额
    const oneOpen = deriveBatch(timelineOf([
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: "One open essay text here." } },
    ])).items;
    expect(capacityRemaining(oneOpen, [])).toBe(2);
    expect(canAddToTray(oneOpen, tray(1))).toBe(true);
    expect(canAddToTray(oneOpen, tray(2))).toBe(false);
  });

  it("已定稿的篇不占名额：上一批定稿后名额恢复为 3", () => {
    const tl = timelineOf([
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: "Finished essay text body." } },
      { type: "tool_result", id: "g1", ok: true, output: { detections: [], summary: "", stats: { words: 4, error_count: 0, suggestion_count: 0, total: 0 }, warnings: [] } },
    ]);
    tl.nodes.push({ id: "f-g1", kind: "finalize", callId: "g1", finalText: "Finished essay text body.", comment: "", keptCount: 0, sortKey: 99 });
    const items = deriveBatch(tl).items;
    expect(openCount(items)).toBe(0);
    expect(capacityRemaining(items, [])).toBe(3);
    expect(canAddToTray(items, tray(2))).toBe(true);
  });

  it("isMultiBatch：只有 1 篇时货架隐身；2 篇及以上才出现；全定稿后隐去", () => {
    const single = deriveBatch(timelineOf([
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: "Single essay body text." } },
    ]));
    expect(isMultiBatch(single, [])).toBe(false);
    expect(isMultiBatch(single, tray(1))).toBe(true); // 1 篇在途 + 托盘 1 张 = 2 篇

    // 图片未被 OCR 认领前不算批次篇：两张裸图 + 0 工具调用 = 0 篇，不出现货架
    const twoBareImages = deriveBatch(timelineWithUsers(
      { text: "两张", images: [{ name: "a.jpg" }, { name: "b.jpg" }] },
      [],
    ));
    expect(twoBareImages.items).toHaveLength(0);
    expect(isMultiBatch(twoBareImages, [])).toBe(false);
    expect(isMultiBatch({ items: twoBareImages.items, closed: true }, [])).toBe(false); // 全定稿关闭
  });

  it("itemStep：图片篇 5 步（转写→核对→批改→拍板→定稿），文本篇 3 步（批改→拍板→定稿）", () => {
    // 直接用最小 item 断言步骤映射（纯映射，不依赖时间线装配）
    const img = (s: string) => itemStep({ id: "i", origin: "image", title: "p", status: s as never });
    const txt = (s: string) => itemStep({ id: "t", origin: "text", title: "t", status: s as never });
    expect(img("transcribing")).toEqual({ index: 0, total: 5 });
    expect(img("needs_review")).toEqual({ index: 1, total: 5 });
    expect(img("grading")).toEqual({ index: 2, total: 5 });
    expect(img("needs_decision")).toEqual({ index: 3, total: 5 });
    expect(img("done")).toEqual({ index: 4, total: 5 });
    expect(img("ocr_failed")).toEqual({ index: 0, total: 5, failed: true });
    expect(img("grade_failed")).toEqual({ index: 2, total: 5, failed: true });
    expect(txt("grading")).toEqual({ index: 0, total: 3 });
    expect(txt("needs_decision")).toEqual({ index: 1, total: 3 });
    expect(txt("done")).toEqual({ index: 2, total: 3 });
    expect(txt("grade_failed")).toEqual({ index: 0, total: 3, failed: true });
  });
});

describe("deriveBatch · 历史回放与兜底", () => {
  it("从后端历史回放（含图片事实陈述、finalize 记录）能重建出已定稿的图片篇", () => {
    const transcript = "Dear Tom, thank you for your last letter.";
    const tl = replayToTimeline([
      { kind: "user", seq: 1, text: "帮我批改这张 （用户上传了一张图片：a.jpg，image_id: iaaa1111）" },
      { kind: "tool", seq: 2, call_seq: 2, tool_name: "ocr_image", call_id: "o1", ok: true,
        arguments: { image_id: "iaaa1111" },
        output: { text: transcript, low_confidence: [], warnings: [], image_id: "iaaa1111" } },
      { kind: "tool", seq: 3, call_seq: 3, tool_name: "grade_text", call_id: "g1", ok: true,
        arguments: { text: transcript },
        output: { detections: [{ id: "d1", tier: "error" }], summary: "不错", stats: { words: 8, error_count: 1, suggestion_count: 0, total: 1 }, warnings: [] } },
      { kind: "finalize", seq: 4, call_id: "g1", final_text: transcript, kept_ids: ["d1"] },
    ]);
    const b = deriveBatch(tl);
    expect(b.items).toHaveLength(1);
    const it = b.items[0];
    expect(it.origin).toBe("image");
    expect(it.title).toBe("a");
    expect(it.imageId).toBe("iaaa1111");
    expect(it.url).toBeUndefined(); // 历史图片没有本地预览
    expect(it.status).toBe("done");
    expect(it.final?.keptCount).toBe(1);
    expect(b.closed).toBe(true);
  });

  it("没有图片附件却来了 ocr 调用：挂孤儿项如实展示，不瞎配、不丢弃", () => {
    const tl = timelineOf([
      { type: "tool_call", id: "ox", name: "ocr_image", args: { image_id: "deadbeef1234" } },
      { type: "tool_result", id: "ox", ok: true, output: { text: "ORPHAN ESSAY BODY TEXT", low_confidence: [], warnings: [], image_id: "deadbeef1234" } },
    ]);
    const it = deriveBatch(tl).items[0];
    expect(it.origin).toBe("image");
    expect(it.orphan).toBe(true);
    expect(it.status).toBe("needs_review");
    expect(it.imageId).toBe("deadbeef1234");
  });

  it("grade 的文本和任何转写稿都对不上：兜底新建一篇文本项，原图片篇停在待核对，不静默吞掉", () => {
    const tl = timelineSteps([
      { user: { text: "批改", images: [{ name: "p.jpg" }] } },
      { type: "tool_call", id: "o1", name: "ocr_image", args: { image_id: "i1" } },
      { type: "tool_result", id: "o1", ok: true, output: { text: "THE PHOTO TRANSCRIPT WORDS HERE", low_confidence: [], warnings: [], image_id: "i1" } },
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: "A COMPLETELY DIFFERENT ESSAY THAT THE USER TYPED INSTEAD" } },
      { type: "tool_result", id: "g1", ok: true, output: { detections: [], summary: "", stats: { words: 8, error_count: 0, suggestion_count: 0, total: 0 }, warnings: [] } },
    ]);
    const b = deriveBatch(tl);
    expect(b.items).toHaveLength(2);
    expect(b.items[0].status).toBe("needs_review"); // 图片篇仍停在核对闸
    expect(b.items[1].origin).toBe("text");
    expect(b.items[1].status).toBe("needs_decision");
  });
});

describe("stageInfo · 货架行的阶段徽章与文案", () => {
  const item = (over: Partial<BatchItem>): BatchItem => ({
    id: "x", origin: "image", title: "t", status: "transcribing", ...over,
  });

  it("图片篇各状态：转写中/待核对（区分有无没看清）/失败/批改中/待拍板/已定稿", () => {
    expect(stageInfo(item({ status: "transcribing" })).label).toBe("转写中");
    expect(stageInfo(item({ status: "needs_review" })).kind).toBe("ocr");
    expect(stageInfo(item({ status: "needs_review" })).detail).toContain("待核对");
    const low = stageInfo(item({ status: "needs_review", lowConfidence: [{ start: 0, end: 1, text: "[?]", reason: "模糊" }] }));
    expect(low.detail).toContain("1 处没看清");
    expect(stageInfo(item({ status: "ocr_failed", error: "网络断了" }))).toMatchObject({ kind: "fail", label: "转写失败" });
    // 失败副标题给稳定人话，不把后端原始错误（HTTP / 超时 / 堆栈）直接甩给用户
    const ocrFail = stageInfo(item({ status: "ocr_failed", error: "HTTP 500 · 本地后端超时（30s 无响应）" }));
    expect(ocrFail.detail).toBe("转写失败，可重试这一篇");
    expect(ocrFail.detail).not.toContain("HTTP");
    expect(ocrFail.detail).not.toContain("后端");
    expect(stageInfo(item({ status: "grading" })).label).toBe("批改中");
    expect(stageInfo(item({ status: "grade_failed" })).kind).toBe("fail");
    expect(stageInfo(item({ status: "grade_failed", error: "视觉模型调用失败" })).detail).toBe(
      "批改失败，可重试这一篇",
    );
    expect(stageInfo(item({ status: "needs_decision" }))).toMatchObject({ kind: "review", label: "待拍板" });
    expect(stageInfo(item({ status: "done" }))).toMatchObject({ kind: "done", label: "已定稿" });
  });

  it("batchSummary：优先提示需要用户动手的（失败 > 待核对 > 待拍板 > 处理中），并附定稿进度", () => {
    const st = (status: BatchItem["status"]): BatchItem => item({ status });
    expect(batchSummary([st("grading"), st("transcribing")])).toBe("2 篇处理中");
    expect(batchSummary([st("needs_review"), st("grading")])).toBe("1 篇待核对");
    expect(batchSummary([st("needs_decision"), st("needs_review")])).toBe("1 篇待核对"); // 核对闸优先
    expect(batchSummary([st("grade_failed"), st("needs_review")])).toBe("1 篇失败待处理");
    expect(batchSummary([st("needs_decision"), st("grading")])).toBe("1 篇待拍板");
    expect(batchSummary([st("done"), st("done")])).toBe("全部定稿");
    expect(batchSummary([st("done"), st("grading")])).toBe("1 篇处理中 · 已定稿 1/2");
  });

  it("文本篇没有转写/核对阶段：排队语义直接是等待批改", () => {
    // 文本篇不会出现 queued/needs_review；grading 即第一步
    expect(stageInfo(item({ origin: "text", status: "grading" })).label).toBe("批改中");
    expect(stageInfo(item({ origin: "text", status: "needs_decision" })).label).toBe("待拍板");
    expect(stageInfo(item({ origin: "text", status: "done" })).label).toBe("已定稿");
  });

  it("已确认转写（前端确认闸）：绿色已确认、停在批改步前", () => {
    const st = stageInfo(item({ status: "review_confirmed" }));
    expect(st.kind).toBe("done");
    expect(st.label).toBe("已确认");
    expect(itemStep(item({ status: "review_confirmed" }))).toEqual({ index: 2, total: 5 });
  });
});

describe("OCR 确认闸（前端）", () => {
  it("needs_review 的图片篇在 confirmed 覆盖后变 review_confirmed，并记录确认稿", () => {
    const tl = timelineSteps([
      { user: { images: [{ name: "a.jpg" }] } },
      { type: "tool_call", id: "oa", name: "ocr_image", args: { image_id: "x" } },
      { type: "tool_result", id: "oa", ok: true, output: { text: "I [?] to school.", low_confidence: [{ start: 2, end: 5, text: "[?]" }] } },
    ]);
    const before = deriveBatch(tl);
    expect(before.items[0].status).toBe("needs_review");
    const after = deriveBatch(tl, { [before.items[0].id]: "I go to school." });
    expect(after.items[0].status).toBe("review_confirmed");
    expect(after.items[0].confirmedText).toBe("I go to school.");
  });

  it("确认后 grade_text 一旦开始，状态回到 grading（确认覆盖不再影响显示）", () => {
    const tl = timelineSteps([
      { user: { images: [{ name: "a.jpg" }] } },
      { type: "tool_call", id: "oa", name: "ocr_image", args: { image_id: "x" } },
      { type: "tool_result", id: "oa", ok: true, output: { text: "I go to school.", low_confidence: [] } },
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: "I go to school." } },
    ]);
    const uid = tl.nodes.find(n => n.kind === "user")!.id;
    const b = deriveBatch(tl, { [`${uid}-img-0`]: "I go to school." });
    expect(b.items[0].status).toBe("grading");
  });

});

describe("workPanelMode · 右翼面板何时出现（单/多篇统一）", () => {
  it("没有任何篇 → none", () => {
    expect(workPanelMode(deriveBatch(emptyTimeline()), [])).toBe("none");
  });

  it("单张照片待核对 / 单篇待拍板 → single（单篇也要 OCR 闸和批阅台）", () => {
    const tl1 = timelineSteps([
      { user: { images: [{ name: "a.jpg" }] } },
      { type: "tool_call", id: "oa", name: "ocr_image", args: { image_id: "x" } },
      { type: "tool_result", id: "oa", ok: true, output: { text: "I go to school.", low_confidence: [] } },
    ]);
    expect(workPanelMode(deriveBatch(tl1), [])).toBe("single");

    const tl2 = timelineSteps([
      { user: { text: "帮我批改：I goes to school." } },
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: "I goes to school." } },
      { type: "tool_result", id: "g1", ok: true, output: { detections: [{ id: "d1" }], summary: "", stats: {} } },
    ]);
    expect(workPanelMode(deriveBatch(tl2), [])).toBe("single");
  });

  it("单篇处理中（grading）或已定稿 → none，不打扰", () => {
    const grading = timelineSteps([
      { user: { text: "帮我批改：I goes to school." } },
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: "I goes to school." } },
    ]);
    expect(workPanelMode(deriveBatch(grading), [])).toBe("none");

    const done = timelineOf([
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: "I goes to school." } },
      { type: "tool_result", id: "g1", ok: true, output: { detections: [{ id: "d1" }] } },
    ]);
    done.nodes.push({ id: "f-g1", kind: "finalize", callId: "g1", finalText: "I go to school.", comment: "", keptCount: 1, sortKey: 99 });
    expect(workPanelMode(deriveBatch(done), [])).toBe("none");
  });

  it("两篇在途 → multi（货架）；一篇定稿后只剩一篇待拍板 → single（定稿篇留在对话流）", () => {
    const two = timelineSteps([
      { user: { images: [{ name: "a.jpg" }, { name: "b.jpg" }] } },
      { type: "tool_call", id: "oa", name: "ocr_image", args: { image_id: "xa" } },
      { type: "tool_result", id: "oa", ok: true, output: { text: "I go to school.", low_confidence: [] } },
      { type: "tool_call", id: "ob", name: "ocr_image", args: { image_id: "xb" } },
      { type: "tool_result", id: "ob", ok: true, output: { text: "I go home.", low_confidence: [] } },
    ]);
    expect(workPanelMode(deriveBatch(two), [])).toBe("multi");

    const oneDoneOneOpen = timelineSteps([
      { user: { text: "帮我批改：I goes to school." } },
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: "I goes to school." } },
      { type: "tool_result", id: "g1", ok: true, output: { detections: [{ id: "d1" }] } },
      { user: { text: "再批改：He go home." } },
      { type: "tool_call", id: "g2", name: "grade_text", args: { text: "He go home." } },
      { type: "tool_result", id: "g2", ok: true, output: { detections: [{ id: "d2" }] } },
    ]);
    // 第一篇定稿（直接推节点，模拟后端回灌）
    oneDoneOneOpen.nodes.push({ id: "f-g1", kind: "finalize", callId: "g1", finalText: "I go to school.", comment: "", keptCount: 1, sortKey: 99 });
    expect(workPanelMode(deriveBatch(oneDoneOneOpen), [])).toBe("single");
  });

  it("托盘里还没发送的图不算批次（items 为空时不出空货架）", () => {
    const tray: TrayItem[] = [
      { id: "t1", kind: "image", name: "a.jpg", url: "", blob: new Blob(), size: 1 },
      { id: "t2", kind: "image", name: "b.jpg", url: "", blob: new Blob(), size: 1 },
    ];
    expect(workPanelMode(deriveBatch(emptyTimeline()), tray)).toBe("none");
  });
});

describe("canvasGridClass · 放大画布栏数档位", () => {
  const gradeResult = (id: string): AskEvent[] => [
    { type: "tool_call", id, name: "grade_text", args: { text: `essay ${id}` } },
    { type: "tool_result", id, ok: true, output: { detections: [{ id: `${id}d1` }], summary: "", stats: {} } },
  ];

  it("没有篇 / 全部定稿 → null（画布不出现）", () => {
    expect(canvasGridClass(deriveBatch(emptyTimeline()).items)).toBeNull();
  });

  it("1/2/3 篇开放 → n1/n2/n3", () => {
    const one = timelineOf(gradeResult("g1"));
    expect(canvasGridClass(deriveBatch(one).items)).toBe("n1");

    const two = timelineSteps([
      { user: { images: [{ name: "a.jpg" }, { name: "b.jpg" }] } },
      { type: "tool_call", id: "oa", name: "ocr_image", args: { image_id: "xa" } },
      { type: "tool_result", id: "oa", ok: true, output: { text: "I go to school.", low_confidence: [] } },
      { type: "tool_call", id: "ob", name: "ocr_image", args: { image_id: "xb" } },
      { type: "tool_result", id: "ob", ok: true, output: { text: "I go home.", low_confidence: [] } },
    ]);
    expect(canvasGridClass(deriveBatch(two).items)).toBe("n2");

    const three = timelineOf([
      ...gradeResult("g1"), ...gradeResult("g2"), ...gradeResult("g3"),
    ]);
    // timelineOf 只放工具节点没有 user 节点时 grade_text 也会 upsert 成文本篇
    expect(canvasGridClass(deriveBatch(three).items)).toBe("n3");
  });

  it("定稿的篇不占画布栏：3 篇中 1 篇定稿 → n2", () => {
    const tl = timelineOf([...gradeResult("g1"), ...gradeResult("g2"), ...gradeResult("g3")]);
    tl.nodes.push({ id: "f-g1", kind: "finalize", callId: "g1", finalText: "x", comment: "", keptCount: 1, sortKey: 99 });
    expect(canvasGridClass(deriveBatch(tl).items)).toBe("n2");
  });
});

describe("失败重试 · 新工具调用重新认领失败篇（不新增重复篇）", () => {
  it("OCR 重试：相同 image_id 的新 call 认领回失败的原篇；旁边还没 OCR 的图不入批次、不会被误领", () => {
    const tl = timelineSteps([
      { user: { images: [{ name: "a.jpg", imageId: "ia" }, { name: "b.jpg", imageId: "ib" }] } },
      { type: "tool_call", id: "o1", name: "ocr_image", args: { image_id: "ia" } },
      { type: "tool_result", id: "o1", ok: false, error: "视觉模型超时" },
      // 重试：相同 image_id、新 call id，先转写中再成功
      { type: "tool_call", id: "o1r", name: "ocr_image", args: { image_id: "ia" } },
      { type: "tool_result", id: "o1r", ok: true, output: { text: "I go to school.", low_confidence: [] } },
    ]);
    const items = deriveBatch(tl).items;
    // 只有 a 被 OCR 认领过；b 还在待认领池里，不产生篇（更不可能被重试 call 领走）
    expect(items.length).toBe(1);
    expect(items[0].status).toBe("needs_review");
    expect(items[0].ocrCallId).toBe("o1r");
    expect(items[0].error).toBeUndefined();
    expect(items[0].transcript).toBe("I go to school.");
    expect(items[0].imageId).toBe("ia");
  });

  it("OCR 重试 call 还在 running：失败篇翻回转写中、错误清除", () => {
    const tl = timelineSteps([
      { user: { images: [{ name: "a.jpg", imageId: "ia" }] } },
      { type: "tool_call", id: "o1", name: "ocr_image", args: { image_id: "ia" } },
      { type: "tool_result", id: "o1", ok: false, error: "视觉模型超时" },
      { type: "tool_call", id: "o1r", name: "ocr_image", args: { image_id: "ia" } },
    ]);
    const items = deriveBatch(tl).items;
    expect(items.length).toBe(1);
    expect(items[0].status).toBe("transcribing");
    expect(items[0].ocrCallId).toBe("o1r");
    expect(items[0].error).toBeUndefined();
  });

  it("图片篇 grade 重试：同确认稿的新 grade call 认领回失败篇，成功后待拍板", () => {
    const tl = timelineSteps([
      { user: { images: [{ name: "a.jpg", imageId: "ia" }] } },
      { type: "tool_call", id: "o1", name: "ocr_image", args: { image_id: "ia" } },
      { type: "tool_result", id: "o1", ok: true, output: { text: "I goes school.", low_confidence: [] } },
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: "I goes school." } },
      { type: "tool_result", id: "g1", ok: false, error: "批改超时" },
      { type: "tool_call", id: "g1r", name: "grade_text", args: { text: "I goes school." } },
      { type: "tool_result", id: "g1r", ok: true, output: { detections: [{ id: "d1" }], summary: "", stats: {} } },
    ]);
    const items = deriveBatch(tl, { "item-u-0-img-0": "I goes school." }).items;
    expect(items.length).toBe(1);
    expect(items[0].origin).toBe("image");
    expect(items[0].status).toBe("needs_decision");
    expect(items[0].gradeCallId).toBe("g1r");
    expect(items[0].error).toBeUndefined();
  });

  it("文本篇 grade 重试：同原文新 call 认领回失败的文本篇，不新建第二篇", () => {
    const tl = timelineSteps([
      { user: { text: "批改：I goes school." } },
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: "I goes school." } },
      { type: "tool_result", id: "g1", ok: false, error: "超时" },
      { type: "tool_call", id: "g1r", name: "grade_text", args: { text: "I goes school." } },
      { type: "tool_result", id: "g1r", ok: true, output: { detections: [{ id: "d1" }], summary: "", stats: {} } },
    ]);
    const items = deriveBatch(tl).items;
    expect(items.length).toBe(1);
    expect(items[0].origin).toBe("text");
    expect(items[0].status).toBe("needs_decision");
    expect(items[0].gradeCallId).toBe("g1r");
  });

  it("图片篇 grade 重试：用户核对时改过 [?]，新 call 按上次 grade 的确认稿认领（不退回原始转写稿）", () => {
    const tl = timelineSteps([
      { user: { images: [{ name: "a.jpg", imageId: "ia" }] } },
      { type: "tool_call", id: "o1", name: "ocr_image", args: { image_id: "ia" } },
      { type: "tool_result", id: "o1", ok: true, output: { text: "I [?] to the zoo.", low_confidence: [{ start: 2, end: 5, text: "[?]" }] } },
      // 用户核对后把 [?] 改成 went，grade 用的是改后稿；第一次批改失败
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: "I went to the zoo." } },
      { type: "tool_result", id: "g1", ok: false, error: "批改超时" },
      // 重试：agent 照抄确认稿
      { type: "tool_call", id: "g1r", name: "grade_text", args: { text: "I went to the zoo." } },
      { type: "tool_result", id: "g1r", ok: true, output: { detections: [{ id: "d1" }], summary: "", stats: {} } },
    ]);
    const items = deriveBatch(tl, { "item-u-0-img-0": "I went to the zoo." }).items;
    expect(items.length).toBe(1);
    expect(items[0].status).toBe("needs_decision");
    expect(items[0].gradeCallId).toBe("g1r");
  });

  it("重试 call 不得误认领别人：篇1 失败、篇2 批改中，篇2 的结果仍归篇2", () => {
    const tl = timelineSteps([
      { user: { text: "第一篇：I goes school." } },
      { type: "tool_call", id: "g1", name: "grade_text", args: { text: "I goes school." } },
      { type: "tool_result", id: "g1", ok: false, error: "超时" },
      { user: { text: "第二篇：He go home." } },
      { type: "tool_call", id: "g2", name: "grade_text", args: { text: "He go home." } },
      { type: "tool_call", id: "g1r", name: "grade_text", args: { text: "I goes school." } },
      { type: "tool_result", id: "g1r", ok: true, output: { detections: [{ id: "d1" }], summary: "", stats: {} } },
    ]);
    const items = deriveBatch(tl).items;
    expect(items.length).toBe(2);
    expect(items[0].status).toBe("needs_decision");
    expect(items[0].gradeCallId).toBe("g1r");
    expect(items[1].status).toBe("grading");
    expect(items[1].gradeCallId).toBe("g2");
  });
});

describe("narrowWorkEntry · 窄屏（<1020px）全屏处理入口", () => {
  const ni = (over: Partial<BatchItem> & { id: string }): BatchItem => ({
    origin: "text", title: "My Essay", status: "grading", ...over,
  });
  const batchOf = (items: BatchItem[]): Batch => ({ items, closed: items.every(i => i.status === "done") });

  it("没有篇 / 单篇处理中 / 全定稿 → null（不加入口、不打扰）", () => {
    expect(narrowWorkEntry(batchOf([]), [])).toBeNull();
    expect(narrowWorkEntry(batchOf([ni({ id: "a", status: "grading" })]), [])).toBeNull();
    expect(narrowWorkEntry(batchOf([ni({ id: "a", status: "done" })]), [])).toBeNull();
  });

  it("单篇待核对 / 待拍板 → 给「打开」入口，带该篇标题与阶段", () => {
    const review = narrowWorkEntry(batchOf([ni({ id: "a", origin: "image", status: "needs_review" })]), []);
    expect(review).not.toBeNull();
    expect(review?.actionText).toBe("打开");
    expect(review?.statusText).toBe("待核对");
    expect(review?.title).toContain("My Essay");

    const decide = narrowWorkEntry(batchOf([ni({ id: "a", status: "needs_decision" })]), []);
    expect(decide?.actionText).toBe("打开");
    expect(decide?.statusText).toBe("待拍板");
  });

  it("单篇失败 → 入口动作文案是「重试」，色调为 fail", () => {
    const entry = narrowWorkEntry(batchOf([ni({ id: "a", status: "grade_failed", error: "超时" })]), []);
    expect(entry).toMatchObject({ actionText: "重试", tone: "fail" });
  });

  it("多篇有待办 → 标题说「N 篇作文」，动作为「打开」，状态优先喊待核对/失败", () => {
    const multi = batchOf([
      ni({ id: "a", origin: "image", status: "needs_review" }),
      ni({ id: "b", status: "needs_decision" }),
    ]);
    const entry = narrowWorkEntry(multi, []);
    expect(entry).not.toBeNull();
    expect(entry?.actionText).toBe("打开");
    expect(entry?.title).toBe("2 篇作文");
    expect(entry?.statusText).toContain("待核对");

    const withFail = batchOf([
      ni({ id: "a", status: "grade_failed" }),
      ni({ id: "b", status: "grading" }),
    ]);
    expect(narrowWorkEntry(withFail, [])?.actionText).toBe("重试");
    expect(narrowWorkEntry(withFail, [])?.tone).toBe("fail");
  });
});

describe("splitConfidenceMarkers（保留原测试）", () => {
  it("splitConfidenceMarkers：把 [?] 切成高亮段，其余为普通段", () => {
    expect(splitConfidenceMarkers("I [?] to school.")).toEqual([
      { t: "I ", conf: false },
      { t: "[?]", conf: true },
      { t: " to school.", conf: false },
    ]);
    expect(splitConfidenceMarkers("no markers")).toEqual([{ t: "no markers", conf: false }]);
    expect(splitConfidenceMarkers("[?] start and end [?]")).toEqual([
      { t: "[?]", conf: true },
      { t: " start and end ", conf: false },
      { t: "[?]", conf: true },
    ]);
  });
});
