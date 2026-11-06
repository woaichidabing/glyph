/**
 * 出站消息组装：只返回用户原话。
 *
 * 图片走 multipart 附件，agent 逐张处理；前端不再往消息里拼任何约定或指令——
 * 用户没说的一个字都不加，意图判断归模型（能推断就办、推断不出用 ask_user 问）。
 * 作文流程（每图一篇、先转写后核对）写在系统提示词里，不属于用户发言。
 */
import type { BatchItem, TrayItem } from "./batch";
import type { Uploaded } from "./uploadAdapter";

/** 组装发送的文本：只返回用户原话（裁掉首尾空白，别的一个字不加）。 */
export function buildBatchMessage(instruction: string): string {
  return instruction.trim();
}

/**
 * OCR 核对确认后触发批改的消息。
 * 单篇：请批改这一篇；多篇：要求逐篇分别调用 grade_text、不要合并成一篇。
 * 确认稿是用户核对过（可能改过 [?]）的最终文本，走纯文本通道。
 */
export function buildOcrConfirmMessage(essays: { title: string; text: string }[]): string {
  const list = essays.filter((e) => e.text.trim() !== "");
  if (list.length === 0) return "";
  if (list.length === 1) {
    const e = list[0];
    return `这篇照片作文的转写稿我已核对确认，请据此批改（${e.title}）：\n\n${e.text.trim()}`;
  }
  const body = list
    .map((e, i) => `第 ${i + 1} 篇（${e.title}）：\n${e.text.trim()}`)
    .join("\n\n");
  return `下面 ${list.length} 篇照片作文的转写稿我都已核对确认，请逐篇分别批改（每篇各调用一次批改工具，不要合并、不要漏掉任何一篇）：\n\n${body}`;
}

/**
 * 单篇失败后点「重试」发给 agent 的指令：只重跑这一篇、其他篇不动。
 * - ocr_failed：让 agent 用相同 image_id 重调 ocr_image（图片在会话历史里，按 id 取）；
 * - grade_failed（图片篇）：附已确认转写稿，明确无需再核对；
 * - grade_failed（文本篇）：附原文。
 */
export function buildRetryMessage(item: BatchItem, index: number): string {
  const why = item.error?.trim() || "未知原因";
  if (item.status === "ocr_failed") {
    const id = item.imageId ? `（image_id：${item.imageId}）` : "（image_id 与上一轮相同）";
    return `第 ${index + 1} 张照片（${item.title}）的文字转写失败了：${why}。`
      + `请重新调用一次 ocr_image${id}，只重新识别这一张照片，其他篇不要动。`;
  }
  if (item.origin === "image") {
    // 取稿优先级：上次 grade 实际请求的稿子（=用户确认稿，grade_failed 时它最权威）
    // > 确认态暂存 > 原始转写稿（可能还带没看清的 [?]，绝不能拿去重批）
    const text = item.grade?.text?.trim() || item.confirmedText?.trim() || item.transcript?.trim() || "";
    return `第 ${index + 1} 篇（${item.title}）批改失败了：${why}。这篇照片的转写稿我已经核对确认过、`
      + `不需要再核对，请重新调用一次 grade_text 只批改这一篇（其他篇不要重跑），批改文本如下：\n\n${text}`;
  }
  const text = item.grade?.text?.trim() || "";
  return `第 ${index + 1} 篇（${item.title}）批改失败了：${why}。请重新调用一次 grade_text 只批改这一篇`
    + `（其他篇不要重跑），原文如下：\n\n${text}`;
}

/** 从输入区取出图片，转成 send() 需要的 Uploaded（blob 在手，直接重建） */
export function trayImagesToUploaded(tray: TrayItem[]): Uploaded[] {
  return tray
    .filter((x): x is TrayItem & { blob: Blob } => x.kind === "image" && !!x.blob)
    .map((x) => ({
      id: x.id,
      name: x.name,
      url: x.url ?? "",
      blob: x.blob,
      size: x.size ?? x.blob.size,
      kind: "image" as const,
    }));
}
