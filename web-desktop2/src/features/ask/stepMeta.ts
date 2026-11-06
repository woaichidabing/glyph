/**
 * 过程块里每一步的「长什么样」：图标、动词、短参数、右侧那点信息。
 *
 * 纯函数，不碰 React / DOM：这些措辞是**界面查表拼出来的**，不能让模型写，
 * 否则工具行就变成了模型的另一张嘴（和 toolPhrases.ts 同一条理由）。
 */
import type { ToolNode } from "../../adapters/askTimeline";
import type { StepIconName } from "./stepIcons";

type Meta = { icon: StepIconName; verb: string };

const TABLE: Record<string, Meta> = {
  grade_text: { icon: "check", verb: "批改英文" },
  ocr_image: { icon: "image", verb: "认图片文字" },
  describe_image: { icon: "image", verb: "看图" },
  lookup_word: { icon: "book", verb: "查词典" },
  read_file: { icon: "file", verb: "读取" },
  read_memory: { icon: "note", verb: "翻长期记忆" },
  read_artifact: { icon: "file", verb: "调阅旧批改" },
  read_plan: { icon: "file", verb: "读计划" },
  read_study: { icon: "file", verb: "读背词进度" },
  read_attempts: { icon: "file", verb: "读练习记录" },
  remember: { icon: "note", verb: "记了一条" },
  forget: { icon: "note", verb: "删了一条记忆" },
  record_review_result: { icon: "note", verb: "记复习结果" },
  record_attempt: { icon: "note", verb: "存练习记录" },
  delete_attempt: { icon: "note", verb: "删练习记录" },
  confirm_mastery: { icon: "check", verb: "确认掌握" },
  recheck_detection: { icon: "search", verb: "复核" },
  analyze_writing: { icon: "check", verb: "评写作水平" },
  polish_text: { icon: "pencil", verb: "表达升档" },
  web_search: { icon: "globe", verb: "联网搜索" },
  ask_user: { icon: "dot", verb: "问你一句" },
  propose_plan: { icon: "file", verb: "拟计划" },
  propose_wordbook_add: { icon: "book", verb: "收词" },
  get_quiz: { icon: "file", verb: "取练习" },
  check_quiz_answer: { icon: "check", verb: "判卷" },
};

export function stepMetaOf(name: string): Meta {
  return TABLE[name] ?? { icon: "terminal", verb: toolVerbFallback(name) };
}

/** 不认识的工具：把英文名翻成人话（比直接甩 code_exec 强） */
function toolVerbFallback(name: string): string {
  if (/search|grep|glob/i.test(name)) return "搜索";
  if (/read|open/i.test(name)) return "读取";
  if (/write|save|edit/i.test(name)) return "写入";
  if (/exec|run|shell|bash|command/i.test(name)) return "运行命令";
  return "调用工具";
}

/** 折叠态那句短参数：文件名的取文件名，查询取查询词，其余不显示。 */
export function stepArgOf(node: ToolNode): string {
  const args = node.args;
  if (!args || typeof args !== "object") return "";
  const pick = (k: string) => {
    const v = (args as Record<string, unknown>)[k];
    return typeof v === "string" ? v.trim() : "";
  };
  const raw =
    pick("path") || pick("file") || pick("filename") || pick("query") ||
    pick("word") || pick("text") || pick("command") || pick("title") || "";
  if (!raw) return "";
  const base = raw.split(/[\\/]/).pop() || raw;
  return base.length > 28 ? base.slice(0, 28) + "…" : base;
}

/** 右侧那点信息：只有真有内容才返回，成功不写「完成」。 */
export function stepInfoOf(node: ToolNode): string {
  if (node.state === "running") return "运行中";
  if (node.state === "cancelled") return "已停止";
  if (node.state === "fail") return "失败";
  const out = node.output;
  if (out && typeof out === "object") {
    const o = out as Record<string, unknown>;
    if (typeof o.match_count === "number") return o.match_count + " 处匹配";
    if (typeof o.results === "object" && Array.isArray(o.results)) {
      return o.results.length + " 条结果";
    }
    if (typeof o.sources === "object" && Array.isArray(o.sources)) {
      return o.sources.length + " 个来源";
    }
    if (typeof o.total_lines === "number") return o.total_lines + " 行";
    if (typeof o.detections === "object" && Array.isArray(o.detections)) {
      return o.detections.length + " 处";
    }
  }
  if (typeof node.durationMs === "number" && node.durationMs >= 1000) {
    const s = node.durationMs / 1000;
    return s >= 60 ? Math.round(s / 60) + " 分" : s.toFixed(1) + "s";
  }
  return "";
}

/** 展开后的输出：按工具说人话，别甩原始 JSON。 */
export function stepOutputText(node: ToolNode): string {
  if (node.state === "fail" && node.error) return node.error;
  const out = node.output;
  if (out == null) return "";
  const o = (typeof out === "object" ? out : {}) as Record<string, unknown>;

  switch (node.name) {
    case "lookup_word": {
      const word = str(o.word) || str(o.queried_form) || str(o.base_form);
      const phon = phoneticOf(o.phonetic);
      const senses = Array.isArray(o.senses) ? (o.senses as Record<string, unknown>[]) : [];
      const gloss = senses.length
        ? senses.slice(0, 3).map((x) => [str(x.pos), str(x.translation)].filter(Boolean).join(" "))
        : (Array.isArray(o.definitions) ? (o.definitions as unknown[]).map(String) : []).slice(0, 3);
      return [word + (phon ? "  /" + phon + "/" : ""), ...gloss].filter(Boolean).join("\n")
        || compactJson(o);
    }
    case "ocr_image":
      return clip(str(o.text)) || compactJson(o);
    case "describe_image":
      return clip(str(o.description) || str(o.text)) || compactJson(o);
    case "grade_text": {
      const ds = Array.isArray(o.detections) ? (o.detections as Record<string, unknown>[]) : [];
      const stats = (o.stats ?? {}) as Record<string, unknown>;
      // 真实字段：detections[].tier = error/suggestion；stats 里也有汇总计数。
      const hard = num(stats.error_count) ?? ds.filter((d) => String(d.tier ?? "") === "error").length;
      const soft = num(stats.suggestion_count) ?? ds.filter((d) => String(d.tier ?? "") === "suggestion").length;
      const head = "共 " + ds.length + " 处：硬错 " + hard + " · 建议 " + soft;
      const rows = ds.slice(0, 3).map((d, i) => {
        const a = str(d.original), b = str(d.corrected);
        return i + 1 + ") " + a + (b ? " → " + b : "");
      });
      const rest = ds.length > 3 ? ["… 其余 " + (ds.length - 3) + " 条在右侧批改台"] : [];
      const sum = str(o.summary);
      return [head, ...rows, ...rest, sum].filter(Boolean).join("\n");
    }
    case "read_file": {
      const text = str(o.text) || str(o.content);
      return text ? clip(text) : compactJson(o);
    }
    case "web_search": {
      const src = Array.isArray(o.sources) ? (o.sources as Record<string, unknown>[]) : [];
      if (src.length === 0) return compactJson(o);
      return src.slice(0, 5).map((x, i) => i + 1 + ". " + (str(x.title) || str(x.url))).join("\n");
    }
    case "remember": {
      const kind = str(o.kind);
      const title = str(o.title) || str(o.content);
      return [kind ? "类型：" + kind : "", title ? "内容：" + title : "", str(o.action)].filter(Boolean)
        .join("\n") || compactJson(o);
    }
    case "read_memory": {
      const matches = Array.isArray(o.matches) ? (o.matches as Record<string, unknown>[]) : [];
      if (num(o.count) !== undefined && matches.length === 0) return "找到 " + num(o.count) + " 条";
      return matches.slice(0, 5).map((x) => "· " + (str(x.title) || str(x.key))).join("\n") || compactJson(o);
    }
    case "read_attempts": {
      const list = Array.isArray(o.attempts) ? (o.attempts as Record<string, unknown>[]) : [];
      return list.slice(0, 4).map((x) => "· " + (str(x.skill) || str(x.attempt_id))).join("\n")
        || (num(o.count) !== undefined ? "共 " + num(o.count) + " 次" : compactJson(o));
    }
    case "analyze_writing": {
      const range = Array.isArray(o.level_range) ? (o.level_range as unknown[]).map(String) : [];
      const level = str(o.level) || (range.length ? range.join("-") : "");
      return level ? "等级：" + level : compactJson(o);
    }
    default:
      return typeof out === "string" ? out : compactJson(o);
  }
}

/** 取对象里的字符串字段（没有就给空串） */
function str(v: unknown): string {
  return typeof v === "string" ? v.trim() : "";
}
/** 音标可能是对象（{uk,us}）也可能是字符串 */
function phoneticOf(v: unknown): string {
  if (typeof v === "string") return v.trim();
  if (v && typeof v === "object") {
    const o = v as Record<string, unknown>;
    return str(o.uk) || str(o.us) || str(o.en) || "";
  }
  return "";
}

/** 数字字段（没有就给 undefined，别把 0 和"没这个字段"混为一谈） */
function num(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

/** 长正文只给前 12 行，剩下的交给"展开其余" */
function clip(text: string): string {
  const lines = text.split("\n");
  return lines.slice(0, 12).join("\n") + (lines.length > 12 ? "\n… 其余 " + (lines.length - 12) + " 行" : "");
}

/** 兜底：去掉大括号和引号，压成几行 key: value */
function compactJson(o: Record<string, unknown>): string {
  const lines = Object.entries(o)
    .filter(([, v]) => v !== null && v !== undefined && v !== "")
    .slice(0, 6)
    .map(([k, v]) => k + ": " + (typeof v === "object" ? JSON.stringify(v).slice(0, 80) : String(v).slice(0, 120)));
  return lines.join("\n");
}
