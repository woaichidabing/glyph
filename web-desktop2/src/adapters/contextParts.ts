/**
 * 上下文构成（「上下文检查」的数据与摆位）。纯函数，vitest 直接测。
 *
 * 两个数字来自不同地方，配合着看：
 * - **总量**：模型实测的 `context_input_tokens`（上一轮真实发出去的输入）；
 * - **比例**：后端把这一轮的消息按来源称重后、用本地字节保守估算出来的（7 个桶）。
 *
 * 估算和实测对不上是正常的（估算口径保守、且不含平台内部开销）。界面不硬凑：
 * 比例用估算，总量用实测，各段按比例缩放到实测总量上——**看比例不失真，看总量不说谎**。
 */

export type ContextPartsPayload = {
  /** 键与后端 ContextStats.parts 一致：system / context / user / assistant / tool_call / artifact / scaffolding */
  parts: Record<string, number>;
  estimated_total: number;
  total_bytes: number;
  total_messages: number;
};

export type ContextSlice = {
  key: string;
  label: string;
  tokens: number;
  pct: number;
  /** 配色档位（0-6），颜色写在 CSS 里，组件不写死色值 */
  tone: number;
};

/**
 * **面板只给三类**：系统提示词 / 工具定义 / 对话。
 *
 * 前两类每轮都发（固定前导），是唯一能省的地方；其余五个桶（每轮注入 / 你说的话 /
 * Glyph 的回答 / 工具入参 / 工具结果）全并进"对话"。三类就能回答"该砍谁"，
 * 七个桶里那五个小到两三位数，列出来只是噪音（见样稿 §2 的取舍记录）。
 *
 * 键以后端 ContextStats.parts 为准（两边同一套，R4）；将来后端加桶，这里不认识的
 * 一律并进"对话"，不会白屏、也不会漏算。
 */
export const CONTEXT_GROUPS: { key: string; label: string; from: string[] }[] = [
  { key: "system", label: "系统提示词", from: ["system"] },
  { key: "tools", label: "工具定义", from: ["tools"] },
  {
    key: "talk",
    label: "对话",
    from: ["context", "user", "assistant", "tool_call", "artifact", "scaffolding"],
  },
];



export type ContextView = {
  total: number;
  slices: ContextSlice[];
  /** true = 总量来自模型实测、各段是按比例缩放过的 */
  scaled: boolean;
  /** true = 还没有实测（新会话 / 模型没回用量），只能拿估算当总量 */
  fromEstimate: boolean;
};

/** 拉当前会话的上下文构成。失败抛人话错误，由弹层显示。 */
export async function loadContextParts(): Promise<ContextPartsPayload> {
  const res = await fetch("/api/context/composition");
  if (!res.ok) throw new Error("暂时取不到上下文构成。");
  return (await res.json()) as ContextPartsPayload;
}

/**
 * 把后端给的桶摆成能画的段。
 *
 * `measuredTotal` 有值时按"估算各段 × (实测 / 估算)"缩放；没有就用估算总量。
 * 空桶不显示（0 的一段没有信息量，还占一行）。
 */
export function buildContextView(
  payload: ContextPartsPayload,
  measuredTotal?: number | null,
): ContextView {
  const estimated = payload.estimated_total ?? 0;
  const hasMeasured = typeof measuredTotal === "number" && measuredTotal > 0;
  const total = hasMeasured ? (measuredTotal as number) : estimated;

  const raw = (key: string) => payload.parts?.[key] ?? 0;
  const slices: ContextSlice[] = [];
  CONTEXT_GROUPS.forEach((group, tone) => {
    const sum = group.from.reduce((acc, key) => acc + raw(key), 0);
    if (sum <= 0) return;
    const tokens = estimated > 0 && hasMeasured ? Math.round((sum / estimated) * total) : sum;
    slices.push({
      key: group.key,
      label: group.label,
      tokens,
      pct: total > 0 ? Math.round((tokens / total) * 100) : 0,
      tone,
    });
  });



  return {
    total,
    slices,
    scaled: hasMeasured && estimated > 0,
    // 只有"确实有一份估算"时才算"拿估算当总量"；空会话 0/0 什么都不算（也不显示）
    fromEstimate: !hasMeasured && estimated > 0,
  };
}
