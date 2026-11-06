import { describe, expect, it } from "vitest";
import { speakableText } from "./pronounce";

/**
 * 朗读一段消息之前，先把 markdown 洗成**能念的纯文本**。
 * 不洗的话 TTS 会把「星号星号」「反引号」也念出来；太长还会被浏览器直接截断。
 *
 * （只测纯函数。真正发声那部分要 Audio / speechSynthesis，node 环境没有，不测。）
 */
describe("speakableText · 洗成能念的纯文本", () => {
  it("空 / 全空白 → 空串（调用方据此把按钮置灰）", () => {
    expect(speakableText("")).toBe("");
    expect(speakableText("   \n\n  ")).toBe("");
  });

  it("去掉强调标记，保留文字", () => {
    expect(speakableText("**arrive** 应该改成 arrived")).toBe("arrive 应该改成 arrived");
    expect(speakableText("__重点__是时态")).toBe("重点是时态");
    expect(speakableText("这是 *斜体* 吗")).toBe("这是 斜体 吗");
  });

  it("去掉行内代码的反引号，保留里面的词", () => {
    expect(speakableText("\`grade_text\` 这个工具")).toBe("grade_text 这个工具");
  });

  it("链接只留文字，不留地址", () => {
    expect(speakableText("见 [剑桥词典](https://dictionary.cambridge.org/x) 的释义")).toBe(
      "见 剑桥词典 的释义",
    );
  });

  it("去掉行首标记（标题井号 / 列表符号 / 引用）", () => {
    expect(speakableText("## 批改结果")).toBe("批改结果");
    expect(speakableText("- 第 2 句有错\n- 第 5 句也有")).toBe("第 2 句有错 第 5 句也有");
    expect(speakableText("> 引用一句")).toBe("引用一句");
  });

  it("代码块围栏整行去掉，里面的内容留着", () => {
    expect(speakableText("\`\`\`\nWhen I arrived at school.\n\`\`\`")).toBe(
      "When I arrived at school.",
    );
  });

  it("换行与连续空白压成单个空格（念出来才顺）", () => {
    expect(speakableText("第一句。\n\n第二句。")).toBe("第一句。 第二句。");
    expect(speakableText("a    b")).toBe("a b");
  });

  it("超长截断，且**不加省略号**（念出来不该有「点点点」）", () => {
    const long = "字".repeat(50);
    const out = speakableText(long, 20);
    expect(out).toBe("字".repeat(20));
    expect(out.endsWith("…")).toBe(false);
  });

  it("默认上限存在，超长不会原样返回", () => {
    const out = speakableText("a".repeat(5000));
    expect(out.length).toBeLessThan(5000);
    expect(out.length).toBeGreaterThan(100);
  });

  it("洗完之后是空的 → 空串（比如整段只有一个链接标记）", () => {
    expect(speakableText("**")).toBe("");
  });
});
