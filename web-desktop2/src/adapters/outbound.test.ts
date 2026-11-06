import { describe, expect, it } from "vitest";
import { buildBatchMessage, buildOcrConfirmMessage, buildRetryMessage } from "./outbound";
import type { BatchItem } from "./batch";

describe("buildBatchMessage（只返回用户原话）", () => {
  it("用户原话原样返回，不掺一个字", () => {
    expect(buildBatchMessage("查一下 serendipity 什么意思")).toBe("查一下 serendipity 什么意思");
  });

  it("只裁首尾空白", () => {
    expect(buildBatchMessage("  只有空白被裁掉  ")).toBe("只有空白被裁掉");
  });

  it("空串返回空串（发送层拦掉空请求）", () => {
    expect(buildBatchMessage("  ")).toBe("");
  });

  it("发图场景也不追加任何约定：用户说一句就只发一句", () => {
    const msg = buildBatchMessage("这是我的作文");
    expect(msg).toBe("这是我的作文");
    expect(msg).not.toContain("如果");
    expect(msg).not.toContain("批改");
    expect(msg).not.toContain("核对");
  });
});

describe("buildOcrConfirmMessage（OCR 核对确认后触发批改）", () => {
  it("单篇：说明已核对、请批改，并带确认稿全文", () => {
    const msg = buildOcrConfirmMessage([{ title: "My Weekend", text: "I go to school." }]);
    expect(msg).toContain("已核对确认");
    expect(msg).toContain("My Weekend");
    expect(msg).toContain("I go to school.");
    expect(msg).not.toContain("逐篇");
  });

  it("多篇：要求逐篇分别批改、不得合并，逐篇带标题与全文", () => {
    const msg = buildOcrConfirmMessage([
      { title: "My Weekend", text: "AAA BODY" },
      { title: "A Letter", text: "BBB BODY" },
    ]);
    expect(msg).toContain("逐篇");
    expect(msg).toContain("不要合并");
    expect(msg).toContain("第 1 篇");
    expect(msg).toContain("AAA BODY");
    expect(msg).toContain("第 2 篇");
    expect(msg).toContain("BBB BODY");
  });

  it("空数组返回空串", () => {
    expect(buildOcrConfirmMessage([])).toBe("");
  });
});

describe("buildRetryMessage · 单篇失败后的重试指令", () => {
  const base = { id: "x", title: "My Weekend" } as Partial<BatchItem>;

  it("OCR 失败：要求用相同 image_id 重调 ocr_image，只重试这一张", () => {
    const item = { ...base, status: "ocr_failed", origin: "image", imageId: "abc123", error: "视觉模型超时" } as BatchItem;
    const msg = buildRetryMessage(item, 1);
    expect(msg).toContain("第 2 张");
    expect(msg).toContain("My Weekend");
    expect(msg).toContain("abc123");
    expect(msg).toContain("ocr_image");
    expect(msg).toContain("视觉模型超时");
    expect(msg).toContain("其他篇");
  });

  it("图片篇批改失败：附上已确认转写稿，要求只重批这一篇", () => {
    const item = {
      ...base, status: "grade_failed", origin: "image",
      confirmedText: "I go to school.", transcript: "I [?] to school.", error: "批改超时",
    } as BatchItem;
    const msg = buildRetryMessage(item, 0);
    expect(msg).toContain("第 1 篇");
    expect(msg).toContain("grade_text");
    expect(msg).toContain("I go to school.");
    expect(msg).not.toContain("[?]");
    expect(msg).toContain("其他篇");
  });

  it("图片篇批改失败（真实路径：confirmedText 已随状态机失效）：取上次 grade 请求稿，不退回带 [?] 的原始转写稿", () => {
    const item = {
      ...base, status: "grade_failed", origin: "image",
      transcript: "I [?] to school.",
      grade: { text: "I go to school.", detections: [], summary: "", stats: undefined },
      error: "批改超时",
    } as BatchItem;
    const msg = buildRetryMessage(item, 0);
    expect(msg).toContain("I go to school.");
    expect(msg).not.toContain("[?]");
  });

  it("文本篇批改失败：附上原文重批；无 error 时用兜底原因", () => {
    const item = {
      ...base, status: "grade_failed", origin: "text",
      grade: { text: "He go home.", detections: [], summary: "", stats: undefined },
    } as BatchItem;
    const msg = buildRetryMessage(item, 2);
    expect(msg).toContain("第 3 篇");
    expect(msg).toContain("He go home.");
    expect(msg).toContain("grade_text");
  });
});
