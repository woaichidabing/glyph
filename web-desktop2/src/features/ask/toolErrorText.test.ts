/**
 * 失败文案翻译层的规格测试。
 *
 * 两条硬约束（设计稿 §7.3 + `adapters/batch.ts:453` 同款规矩）：
 * 1. **绝不回显后端原始错误**——用户看不懂 HTTP 码 / 堆栈 / 响应体；
 * 2. 译文是**固定人话**，从「工具 × 失败类别」查表得来，不拼原文片段。
 *
 * 与 `batch.ts::stageInfo` 的边界：那张表管**批量任务**的篇级状态（BatchItem.status），
 * 这张表管**工具调用**的失败（ToolResultEvent.error），输入不同、不重复。
 */
import { describe, it, expect } from "vitest";
import { classifyToolError, failSuffix, type FailKind } from "./toolErrorText";

describe("classifyToolError：只读关键字判类别，不做展示", () => {
  it("没有错误串就是未知，不抛异常", () => {
    expect(classifyToolError()).toBe<FailKind>("unknown");
    expect(classifyToolError(null)).toBe<FailKind>("unknown");
    expect(classifyToolError("")).toBe<FailKind>("unknown");
  });

  it("额度 / 鉴权是确定性错误，优先级最高", () => {
    expect(classifyToolError("InvalidApiKey")).toBe<FailKind>("quota");
    expect(classifyToolError("当前模型额度已用完")).toBe<FailKind>("quota");
    expect(classifyToolError("HTTP 402 Forbidden: quota exhausted")).toBe<FailKind>("quota");
  });

  it("格式 / 参数类错误单独成一类", () => {
    expect(classifyToolError("gif 请另存为 png / jpg 后上传")).toBe<FailKind>("format");
    expect(classifyToolError("不支持的文件类型：.docx")).toBe<FailKind>("format");
  });

  it("超时优先于连不上：两者都像时按“等太久”理解", () => {
    expect(classifyToolError("HTTP 500 · 本地后端超时（30s 无响应）")).toBe<FailKind>("timeout");
    expect(classifyToolError("连接超时")).toBe<FailKind>("timeout");
  });

  it("连不上单独成一类", () => {
    expect(classifyToolError("无法连接")).toBe<FailKind>("offline");
    expect(classifyToolError("网络断了")).toBe<FailKind>("offline");
    expect(classifyToolError("Connection refused")).toBe<FailKind>("offline");
  });

  it("认不出来的归 unknown，绝不把原文当类别抛出去", () => {
    expect(classifyToolError("KeyError: 'detections'")).toBe<FailKind>("unknown");
  });
});

describe("failSuffix：组头里接在动作后面的人话后缀", () => {
  it("按类别给稳定措辞", () => {
    expect(failSuffix("grade_text", "offline")).toBe("没连上");
    expect(failSuffix("grade_text", "timeout")).toBe("等太久了");
    expect(failSuffix("grade_text", "format")).toBe("格式不支持");
    expect(failSuffix("grade_text", "quota")).toBe("模型用不了");
    expect(failSuffix("grade_text", "unknown")).toBe("没跑完");
  });

  it("工具自己的说法优先：旧批改取不到时说的是“那篇没找到”", () => {
    expect(failSuffix("read_artifact", "unknown")).toBe("那篇没找到");
    expect(failSuffix("read_artifact", "offline")).toBe("那篇没找到");
  });

  it("未知工具也有话说，不空着、不抛术语", () => {
    expect(failSuffix("brand_new_tool", "unknown")).toBe("没跑完");
  });

  it("译文里绝不出现原始错误串的任何片段", () => {
    const raw = "HTTP 500 · KeyError: 'detections' at line 42";
    const kinds: FailKind[] = ["quota", "format", "timeout", "offline", "unknown"];
    for (const kind of kinds) {
      const text = failSuffix("grade_text", kind);
      expect(text).not.toContain("HTTP");
      expect(text).not.toContain("KeyError");
      expect(text).not.toContain("42");
      expect(text).not.toContain("detections");
    }
    // 而类别判定也确实没把原文透出来
    expect(classifyToolError(raw)).toBe<FailKind>("unknown");
  });
});