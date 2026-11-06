/**
 * 轻量 Markdown 渲染（零依赖、不拼 HTML 字符串，天然防注入）。
 *
 * 助手回复只需要一小撮语法：段落 / 换行、有序无序列表、引用、标题、
 * GFM 表格、``` 围栏代码块，以及行内 `代码`、**加粗**、*斜体*、
 * [链接](https://…)。全部转成 React 元素，用户文本走 JSX 自动转义，
 * 不使用 dangerouslySetInnerHTML。
 *
 * 流式容错：回复是逐字拼出来的，结尾围栏没闭合、表格只到分隔行都可能出现，
 * 解析器对这两种「半截」结构按已完整处理，避免内容闪成普通段落。
 *
 * 刻意不做：代码块语法高亮 / 嵌套引用 —— 英语学习回复用不上，多了反而出错。
 */
import { memo, useMemo, useState, type ReactNode } from "react";

// 行内切分：代码 → 加粗 → 斜体 → 链接。字符类 [] 不是捕获组、链接用 (?:…)，
// 保证 split 结果只在 [普通文本, 命中片段, 普通文本…] 间交替。
const INLINE_RE =
  /(`[^`]+`|\*\*[^*]+?\*\*|\*[^*]+?\*|\[[^\]]+\]\((?:https?:\/\/[^\s)]+)\))/g;

function renderInline(text: string, keyPrefix: string): ReactNode[] {
  return text.split(INLINE_RE).map((part, i) => {
    const key = `${keyPrefix}-${i}`;
    if (!part) return null;
    if (part.startsWith("`") && part.endsWith("`") && part.length > 1) {
      return <code key={key}>{part.slice(1, -1)}</code>;
    }
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return <strong key={key}>{part.slice(2, -2)}</strong>;
    }
    const link = part.match(/^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/);
    if (link) {
      return (
        <a key={key} href={link[2]} target="_blank" rel="noreferrer noopener">
          {link[1]}
        </a>
      );
    }
    if (part.startsWith("*") && part.endsWith("*") && part.length > 2) {
      return <em key={key}>{part.slice(1, -1)}</em>;
    }
    return <span key={key}>{part}</span>;
  });
}

/** 段落 / 引用内保留软换行（Markdown 单换行在对话里应当看得见）。 */
function renderWithBreaks(text: string, keyPrefix: string): ReactNode[] {
  const lines = text.split("\n");
  const out: ReactNode[] = [];
  lines.forEach((ln, idx) => {
    out.push(...renderInline(ln, `${keyPrefix}-${idx}`));
    if (idx < lines.length - 1) out.push(<br key={`${keyPrefix}-br${idx}`} />);
  });
  return out;
}

/** 递归渲染列表：有序列表带 start（续号），每项可挂缩进的子列表（如例句）。 */
function renderListBlock(b: ListBlock, keyPrefix: string, key: number | string): ReactNode {
  const items = b.items.map((it, j) => (
    <li key={j}>
      {renderInline(it, `${keyPrefix}-${j}`)}
      {b.subs?.[j] ? renderListBlock(b.subs[j] as ListBlock, `${keyPrefix}-${j}-s`, `${key}-sub${j}`) : null}
    </li>
  ));
  return b.kind === "ol" ? (
    <ol key={key} start={b.start}>
      {items}
    </ol>
  ) : (
    <ul key={key}>{items}</ul>
  );
}

type Align = "left" | "right" | "center";
/** 列表块：items 是每一项的主文本；subs[i] 是该项缩进的子列表（例句等），没有则 null。
 *  start 仅有序列表、且源文本起始编号不是 1 时带上（GFM 允许列表被标题切开后续号）。 */
type ListBlock = {
  kind: "ul" | "ol";
  items: string[];
  subs?: (ListBlock | null)[];
  start?: number;
};
type Block =
  | ListBlock
  | { kind: "quote"; text: string }
  | { kind: "h"; level: number; text: string }
  | { kind: "p"; text: string }
  | { kind: "code"; lang: string; text: string }
  | { kind: "table"; head: string[]; align: Align[]; rows: string[][] };

const isUlist = (s: string) => /^\s*[-*•·]\s+/.test(s);
const isOlist = (s: string) => /^\s*\d+[.)、]\s+/.test(s);
const isQuote = (s: string) => /^\s*>\s?/.test(s);
const isHeading = (s: string) => /^\s*#{1,3}\s+/.test(s);
/** 列表项标记：捕获「前导缩进 + 标记（数字序号或 - * • ·）+ 内容」，用于 GFM 嵌套列表解析 */
const LIST_MARKER_RE = /^(\s*)(\d+[.)、]|[-*•·])(?:[ \t]+(.*))?$/;
const listKindOf = (marker: string): "ul" | "ol" => (/\d/.test(marker[0]) ? "ol" : "ul");
const indentOf = (line: string): number => line.match(/^(\s*)/)![1].length;

/**
 * 解析一个 GFM 列表（支持每项下缩进的子列表、项间空行、有序号续号）。
 * 从 lines[start]（已是列表项）开始，返回列表块与第一个「不属于本列表」的行号。
 * 子内容列 = 缩进 + 标记宽度 + 1（`1. ` 为 3、`- ` 为 2）；更深缩进的行归入当前项。
 */
function parseList(lines: string[], start: number, baseIndent: number): { block: ListBlock; next: number } {
  const first = lines[start].match(LIST_MARKER_RE)!;
  const kind = listKindOf(first[2]);
  const contentCol = first[1].length + first[2].length + 1;
  const items: string[] = [];
  const subs: (ListBlock | null)[] = [];
  let cur = -1;
  const beginItem = (text: string) => {
    items.push(text);
    subs.push(null);
    cur = items.length - 1;
  };
  beginItem(first[3] ?? "");
  let i = start + 1;

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      // 空行：看后面第一个非空行——同级/更深的列表项、或更深的续行，列表都继续；否则结束
      let j = i + 1;
      while (j < lines.length && !lines[j].trim()) j += 1;
      if (j >= lines.length) {
        i = j;
        break;
      }
      const jm = lines[j].match(LIST_MARKER_RE);
      const jInd = indentOf(lines[j]);
      if ((jm && (jInd <= baseIndent || jInd >= contentCol)) || (!jm && jInd >= contentCol)) {
        i = j;
        continue;
      }
      break;
    }
    const m = line.match(LIST_MARKER_RE);
    if (m) {
      const ind = m[1].length;
      if (ind <= baseIndent) {
        if (listKindOf(m[2]) !== kind) break; // 同级却换了列表类型（ol 中插顶层 ul）→ 本列表到此为止
        beginItem(m[3] ?? "");
        i += 1;
        continue;
      }
      if (ind >= contentCol) {
        // 更深一层 → 递归成当前项的子列表
        const sub = parseList(lines, i, ind);
        if (cur >= 0) subs[cur] = sub.block;
        i = sub.next;
        continue;
      }
      break;
    }
    // 非标记行：缩进够深就是当前项的续行，否则列表结束
    if (indentOf(line) >= contentCol && cur >= 0) {
      items[cur] = `${items[cur]} ${line.trim()}`;
      i += 1;
      continue;
    }
    break;
  }

  const block: ListBlock = { kind, items };
  if (subs.some(Boolean)) block.subs = subs;
  if (kind === "ol") {
    const n = parseInt(first[2], 10);
    if (n && n !== 1) block.start = n;
  }
  return { block, next: i };
}
/** 围栏代码块开头：``` 或 ~~~，可带语言（```python） */
const FENCE_RE = /^(\s*)(`{3,}|~{3,})\s*([^\s`]*)[^\n]*$/;
/** GFM 表格分隔行：| --- | :-- | --: | :-: | */
const TABLE_SEP_RE = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;

const splitCells = (line: string): string[] =>
  line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|\s*$/, "")
    .split("|")
    .map((c) => c.trim());

const sepAlign = (cell: string): Align => {
  const c = cell.trim();
  if (c.startsWith(":") && c.endsWith(":")) return "center";
  if (c.endsWith(":")) return "right";
  return "left";
};

export function parseBlocks(src: string): Block[] {
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const raw = lines[i];
    const t = raw.trim();
    if (!t) {
      i += 1;
      continue;
    }
    // 围栏代码块：吃到同符号闭合行；流式中结尾围栏缺失就吃到末尾（容错）
    const fence = raw.match(FENCE_RE);
    if (fence) {
      const mark = fence[2];
      const lang = fence[3] || "";
      const closeRe = new RegExp(`^\\s*${mark[0]}{${mark.length},}\\s*$`);
      const buf: string[] = [];
      i += 1;
      while (i < lines.length && !closeRe.test(lines[i])) {
        buf.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1; // 跳过闭合行（没闭合则已到末尾）
      // 流式中最后一行可能带着半个换行产生的空串，trim 掉尾部空行即可
      while (buf.length && !buf[buf.length - 1].trim()) buf.pop();
      blocks.push({ kind: "code", lang, text: buf.join("\n") });
      continue;
    }
    const h = t.match(/^(#{1,3})\s+(.*)$/);
    if (h) {
      blocks.push({ kind: "h", level: h[1].length, text: h[2] });
      i += 1;
      continue;
    }
    const lm = raw.match(LIST_MARKER_RE);
    if (lm) {
      const parsed = parseList(lines, i, lm[1].length);
      blocks.push(parsed.block);
      i = parsed.next;
      continue;
    }
    if (isQuote(raw)) {
      const q: string[] = [];
      while (i < lines.length && isQuote(lines[i])) {
        q.push(lines[i].trim().replace(/^\s*>\s?/, ""));
        i += 1;
      }
      blocks.push({ kind: "quote", text: q.join("\n") });
      continue;
    }
    // GFM 表格：本行像表头（含 |），下一行是分隔行
    if (t.includes("|") && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1])) {
      const head = splitCells(raw);
      const align = splitCells(lines[i + 1]).map(sepAlign);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].trim().includes("|")) {
        const cells = splitCells(lines[i]);
        // 空行或分隔行（流式重试等）结束表格
        if (!lines[i].trim() || TABLE_SEP_RE.test(lines[i])) break;
        rows.push(cells);
        i += 1;
      }
      blocks.push({ kind: "table", head, align, rows });
      continue;
    }
    // 普通段落：吃到空行或下一个块起始行为止
    const buf: string[] = [];
    while (i < lines.length) {
      const cur = lines[i];
      const ct = cur.trim();
      if (
        !ct ||
        isHeading(cur) ||
        isUlist(cur) ||
        isOlist(cur) ||
        isQuote(cur) ||
        FENCE_RE.test(cur) ||
        (ct.includes("|") && i + 1 < lines.length && TABLE_SEP_RE.test(lines[i + 1]))
      ) {
        break;
      }
      buf.push(cur);
      i += 1;
    }
    const text = buf.join("\n").trim();
    if (text) blocks.push({ kind: "p", text });
  }
  return blocks;
}

function MarkdownImpl({ text, className }: { text: string; className?: string }) {
  const blocks = useMemo(() => parseBlocks(text), [text]);
  return (
    <div className={"md" + (className ? ` ${className}` : "")}>
      {blocks.map((b, bi) => {
        switch (b.kind) {
          case "h": {
            const Tag = (`h${b.level}` as "h1" | "h2" | "h3");
            return <Tag key={bi}>{renderInline(b.text, `h${bi}`)}</Tag>;
          }
          case "ul":
          case "ol":
            return renderListBlock(b, `l${bi}`, bi);
          case "quote":
            return <blockquote key={bi}>{renderWithBreaks(b.text, `q${bi}`)}</blockquote>;
          case "code":
            return <CodeBlock key={bi} text={b.text} />;
          case "table":
            return (
              <table key={bi}>
                <thead>
                  <tr>
                    {b.head.map((c, j) => (
                      <th key={j} style={{ textAlign: b.align[j] || "left" }}>
                        {renderInline(c, `th${bi}-${j}`)}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {b.rows.map((row, ri) => (
                    <tr key={ri}>
                      {b.head.map((_, j) => (
                        <td key={j} style={{ textAlign: b.align[j] || "left" }}>
                          {renderInline(row[j] ?? "", `td${bi}-${ri}-${j}`)}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            );
          default:
            return <p key={bi}>{renderWithBreaks(b.text, `p${bi}`)}</p>;
        }
      })}
    </div>
  );
}

/* 代码块右上角的复制。独立成小组件，是为了它有自己的「已复制」状态 ——
   放进 switch 里没法挂 state。剪贴板不可用时静默失败，不打扰。 */
function CodeBlock({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const onCopy = () => {
    navigator.clipboard?.writeText(text).then(
      () => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1400);
      },
      () => {},
    );
  };
  return (
    <div className="codewrap">
      <button type="button" className="codecopy" onClick={onCopy} title="复制这段">
        {copied ? "已复制" : "复制"}
      </button>
      <pre>
        <code>{text}</code>
      </pre>
    </div>
  );
}

export const Markdown = memo(MarkdownImpl);
