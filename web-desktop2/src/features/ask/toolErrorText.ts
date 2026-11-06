/**
 * 工具失败的**人话翻译层**（纯函数：无 React、无 DOM、无网络）。
 *
 * 为什么必须有这一层：工具失败时后端给的是给机器看的错误串（HTTP 码、异常类名、
 * 堆栈片段、响应体），用户既看不懂也无从下手。所以界面上出现的失败措辞**只能**来自
 * 本文件的两张表——判类别、查措辞，**绝不拼接、回显原始错误的任何片段**。
 * 这是 `adapters/batch.ts:453` 同款规矩在工具调用这一侧的落地。
 *
 * 与相邻模块的边界：
 * - `batch.ts::stageInfo` 管**批量任务**的篇级状态（`BatchItem.status`），产物是徽章与副标题；
 * - 本文件管**工具调用**的失败（`ToolResultEvent.error`），产物是组头里接在动作后面的后缀；
 * - `agentStream.ts` 里那段额度 / 鉴权判定管的是**建连阶段**的 HTTP 响应，时机不同。
 *
 * 措辞单点：`docs/design/ask-transcript-render-design.md` §7.3。
 */

/** 失败的类别。只用于查措辞，不对外展示。 */
export type FailKind = "quota" | "format" | "timeout" | "offline" | "unknown";

/**
 * 关键字表。**判定顺序即数组顺序**——先确定性、后临时性：
 * 额度 / 鉴权最确定（重试无用），格式次之（换个文件就能成），
 * 然后才是超时与连不上（两者的错误串常常互相包含，故超时排前面）。
 */
const RULES: ReadonlyArray<{ kind: FailKind; re: RegExp }> = [
  // `invalid\s*api\s*key` 用 `\s*` 而非空格：千问返回的是驼峰 `InvalidApiKey`，没有空格
  { kind: "quota", re: /quota|exhausted|forbidden|unauthorized|invalid\s*api\s*key|额度|欠费|鉴权|余额/i },
  { kind: "format", re: /格式|不支持|另存为|unsupported|not supported|invalid type/i },
  { kind: "timeout", re: /超时|timeout|timed out|deadline/i },
  { kind: "offline", re: /连不上|无法连接|网络|connection|refused|unreachable/i },
];

/** 原始错误串 → 失败类别。认不出来的归 `unknown`，绝不把原文当类别抛出去。 */
export function classifyToolError(raw?: string | null): FailKind {
  if (!raw) return "unknown";
  for (const rule of RULES) {
    if (rule.re.test(raw)) return rule.kind;
  }
  return "unknown";
}

/** 认不出类别时的兜底后缀。**唯一定义在这里**——`toolPhrases.ts` 拼组头时也引这一句。 */
export const FAIL_FALLBACK = "没跑完";

/** 按类别给的通用措辞 */
const GENERIC: Record<FailKind, string> = {
  quota: "模型用不了",
  format: "格式不支持",
  timeout: "等太久了",
  offline: "没连上",
  unknown: FAIL_FALLBACK,
};

type ToolOverride = Partial<Record<FailKind, string>> & {
  /** 任何类别都适用的说法（比按类别更具体时用） */
  any?: string;
};

/** 工具自己的说法，优先于通用措辞——通用话在这个工具上读不通时才写进来 */
const BY_TOOL: Record<string, ToolOverride> = {
  read_artifact: { any: "那篇没找到" },
};

/**
 * 组头里接在动作短语后面的失败后缀。例：`联网搜索` + `没连上` → `联网搜索没连上`。
 *
 * 未知工具也有话说（走通用措辞），不会空着、也不会抛术语。
 */
export function failSuffix(name: string, kind: FailKind): string {
  const own = BY_TOOL[name];
  return own?.[kind] ?? own?.any ?? GENERIC[kind];
}