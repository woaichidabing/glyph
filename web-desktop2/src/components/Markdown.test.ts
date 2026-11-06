import { describe, it, expect } from "vitest";
import { parseBlocks } from "./Markdown";

/* parseBlocks 是 Markdown 渲染的公开纯函数 seam：只断言解析结构，
   不碰 DOM。流式容错（未闭合围栏、半截表格）是重点。 */

describe("parseBlocks · 围栏代码块", () => {
  it("三反引号围栏整体成一个 code 块，里面的 #/-/| 不被当 markdown", () => {
    const src = "```\n# 标题？不\n- 列表？不\n| a | b |\n```\n后面是段落";
    const blocks = parseBlocks(src);
    expect(blocks[0]).toEqual({
      kind: "code",
      lang: "",
      text: "# 标题？不\n- 列表？不\n| a | b |",
    });
    expect(blocks[1]).toEqual({ kind: "p", text: "后面是段落" });
  });

  it("带语言标记：```python", () => {
    const blocks = parseBlocks("```python\nx = 1\n```");
    expect(blocks[0]).toMatchObject({ kind: "code", lang: "python", text: "x = 1" });
  });

  it("流式容错：结尾围栏还没吐出来，已收到的内容先按代码块渲染", () => {
    const blocks = parseBlocks("```js\nconst a = 1;\nconsole.log(a)");
    expect(blocks[0]).toMatchObject({
      kind: "code",
      lang: "js",
      text: "const a = 1;\nconsole.log(a)",
    });
  });

  it("波浪线围栏同样支持", () => {
    const blocks = parseBlocks("~~~\ncode\n~~~");
    expect(blocks[0]).toMatchObject({ kind: "code", text: "code" });
  });
});

describe("parseBlocks · GFM 表格", () => {
  it("表头 + 分隔行 + 数据行", () => {
    const src = "| 维度 | 分数 |\n| --- | --- |\n| 内容 | 8 |\n| 结构 | 7 |";
    const blocks = parseBlocks(src);
    expect(blocks[0]).toEqual({
      kind: "table",
      head: ["维度", "分数"],
      align: ["left", "left"],
      rows: [["内容", "8"], ["结构", "7"]],
    });
  });

  it("无外侧竖线的简写法也认", () => {
    const blocks = parseBlocks("a | b\n--- | ---\n1 | 2");
    expect(blocks[0]).toMatchObject({ kind: "table", head: ["a", "b"], rows: [["1", "2"]] });
  });

  it("流式容错：只到分隔行、数据行还没来，也先建表（不闪成段落）", () => {
    const blocks = parseBlocks("| 维度 | 分数 |\n| --- | --- |");
    expect(blocks[0]).toMatchObject({ kind: "table", head: ["维度", "分数"], rows: [] });
  });

  it("冒号对齐：左/右/居中", () => {
    const blocks = parseBlocks("| a | b | c |\n| :-- | --: | :-: |\n| 1 | 2 | 3 |");
    expect((blocks[0] as { align: string[] }).align).toEqual(["left", "right", "center"]);
  });

  it("单元格内容 trim", () => {
    const blocks = parseBlocks("|  h  |  i |\n| --- | --- |\n| x |  y  |");
    const t = blocks[0] as { head: string[]; rows: string[][] };
    expect(t.head).toEqual(["h", "i"]);
    expect(t.rows[0]).toEqual(["x", "y"]);
  });

  it("普通文本里的竖线不是表格（下一行不是分隔行）", () => {
    const blocks = parseBlocks("a | b\n只是普通下一行");
    expect(blocks[0].kind).toBe("p");
  });
});

describe("parseBlocks · 既有语法回归", () => {
  it("段落、无序列表、标题、引用仍正常", () => {
    const src = "# 标题\n\n- 甲\n- 乙\n\n> 引用一句\n\n普通段落";
    const blocks = parseBlocks(src);
    expect(blocks.map((b) => b.kind)).toEqual(["h", "ul", "quote", "p"]);
  });

  it("有序列表", () => {
    const blocks = parseBlocks("1. 第一\n2. 第二");
    expect(blocks[0]).toMatchObject({ kind: "ol", items: ["第一", "第二"] });
  });
});

describe("parseBlocks · 列表连续性与嵌套（GFM）", () => {
  it("列表项之间的空行不会把一个有序列表拆成多个（编号保持连续）", () => {
    const blocks = parseBlocks("1. 甲\n\n2. 乙\n\n3. 丙");
    expect(blocks.length).toBe(1);
    expect(blocks[0]).toMatchObject({ kind: "ol", items: ["甲", "乙", "丙"] });
  });

  it("有序列表起始编号不是 1 时带 start（被拆也能显示对序号）", () => {
    const blocks = parseBlocks("3. 三\n4. 四");
    expect(blocks[0]).toMatchObject({ kind: "ol", start: 3, items: ["三", "四"] });
  });

  it("编号义项 + 缩进例句子列表：一个 ol 含连续编号，例句作为该项的子 ul", () => {
    const src =
      "## 作名词\n\n" +
      "1. **礼物**\n" +
      "   - `She gave.`（她送了礼物）\n\n" +
      "2. **现在**\n" +
      "   - `We have.`（我们没有计划）";
    const blocks = parseBlocks(src);
    expect(blocks.map((b) => b.kind)).toEqual(["h", "ol"]);
    const ol = blocks[1] as {
      kind: "ol";
      items: string[];
      subs?: ({ kind: "ul" | "ol"; items: string[] } | null)[];
    };
    expect(ol.items).toEqual(["**礼物**", "**现在**"]);
    expect(ol.subs?.[0]).toMatchObject({ kind: "ul", items: ["`She gave.`（她送了礼物）"] });
    expect(ol.subs?.[1]).toMatchObject({ kind: "ul", items: ["`We have.`（我们没有计划）"] });
  });

  it("六个跨小标题分组的义项仍各自连续（每组从 1 开始是模型分组，渲染器只保证组内连续）", () => {
    // 同一列表内编号连续；模型若用 ## 分节则每节是独立列表、各自从 1（符合 GFM）
    const src = "## A\n1. 一\n   - a\n2. 二\n   - b\n## B\n3. 三\n   - c\n4. 四\n   - d";
    const blocks = parseBlocks(src);
    const ols = blocks.filter((b) => b.kind === "ol") as {
      kind: "ol";
      items: string[];
      start?: number;
    }[];
    expect(ols.length).toBe(2);
    expect(ols[0].items).toEqual(["一", "二"]); // 从 1
    expect(ols[1].start).toBe(3); // 模型跨节仍写 3、4，start=3 显示 3、4
    expect(ols[1].items).toEqual(["三", "四"]);
  });
});
