import { describe, expect, it } from "vitest";
import {
  BATCH_MAX_PAPERS,
  batchFileName,
  batchKey,
  buildBatchForm,
  examCode,
  fileSupported,
  formatSize,
  guessPaperMeta,
  normalizePaperOutcome,
  pairIntoSlots,
  type BatchSlot,
} from "./quizImport";

const fileNamed = (name: string, size = 10) =>
  new File([new Uint8Array(size)], name);

describe("fileSupported", () => {
  it("后端 extract_text 认的类型放行，大小写不敏感", () => {
    expect(fileSupported("卷子.pdf")).toBe(true);
    expect(fileSupported("卷子.DOCX")).toBe(true);
    expect(fileSupported("notes.txt")).toBe(true);
    expect(fileSupported("a.md")).toBe(true);
  });

  it("老 .doc 和图片不放行，前端先给一句人话", () => {
    expect(fileSupported("2003年真题.doc")).toBe(false);
    expect(fileSupported("photo.jpg")).toBe(false);
  });

  it("没有后缀交给后端按纯文本处理", () => {
    expect(fileSupported("卷子")).toBe(true);
  });
});

describe("formatSize", () => {
  it("按人的口径报大小", () => {
    expect(formatSize(512)).toBe("512 B");
    expect(formatSize(2048)).toBe("2 KB");
    expect(formatSize(5 * 1024 * 1024)).toBe("5.0 MB");
  });
});

describe("examCode", () => {
  it("四六级映射成和出厂卡一致的 cet4 / cet6", () => {
    expect(examCode("四级")).toBe("cet4");
    expect(examCode(" 六级 ")).toBe("cet6");
  });

  it("其它考试保留原名但去掉不能当文件名的字符", () => {
    expect(examCode("高考")).toBe("高考");
    expect(examCode("考研 英语一")).toBe("考研-英语一");
  });
});

describe("batchKey", () => {
  it("拼成 exam-year-setN 的套 key", () => {
    expect(batchKey("四级", "2026", 1)).toBe("cet4-2026-set1");
  });

  it("没填年份也能成 key，不会留下空段", () => {
    expect(batchKey("六级", "", 3)).toBe("cet6-set3");
  });
});

describe("batchFileName", () => {
  it("卷子 / 答案分别加 q_ / a_ 前缀并保留后缀", () => {
    expect(batchFileName("真题.pdf", "q", "cet4-2026-set1")).toBe("q_cet4-2026-set1.pdf");
    expect(batchFileName("答案与详解.DOCX", "a", "cet4-2026-set1")).toBe(
      "a_cet4-2026-set1.docx",
    );
  });

  it("无后缀文件不加多余的点", () => {
    expect(batchFileName("卷子", "q", "cet4-2026-set1")).toBe("q_cet4-2026-set1");
  });
});

describe("guessPaperMeta", () => {
  it("从中文真题文件名认出考试、年份、套次", () => {
    expect(guessPaperMeta("2026年6月大学英语四级真题第1套.pdf")).toEqual({
      exam: "四级",
      year: "2026",
      setNo: 1,
    });
  });

  it("认英文下划线命名 CET4_2025-12_set2", () => {
    expect(guessPaperMeta("CET4_2025-12_set2.docx")).toEqual({
      exam: "四级",
      year: "2025",
      setNo: 2,
    });
  });

  it("只认得出部分信息也照给（六级第三套、没年份）", () => {
    expect(guessPaperMeta("六级真题第三套.pdf")).toEqual({ exam: "六级", setNo: 3 });
  });

  it("高考新课标卷认成高考、年份照取，罗马数字卷别不当成套次", () => {
    expect(guessPaperMeta("2024新课标Ⅰ卷.pdf")).toEqual({ exam: "高考", year: "2024" });
  });

  it("完全无关的文件名给空对象，不硬猜", () => {
    expect(guessPaperMeta("随便.txt")).toEqual({});
  });
});

describe("buildBatchForm", () => {
  const slot = (q: string, a: string | null): BatchSlot => ({
    questionFile: fileNamed(q),
    answerFile: a ? fileNamed(a) : null,
  });

  it("每套的卷子改名 q_key、单独答案改名 a_key，后端据此精确配对", () => {
    const fd = buildBatchForm(
      { exam: "四级", year: "2026" },
      [slot("真题.pdf", "答案.pdf"), slot("第二套.docx", null)],
    );
    const names = (fd.getAll("files") as File[]).map(f => f.name).sort();
    expect(names).toEqual([
      "a_cet4-2026-set1.pdf",
      "q_cet4-2026-set1.pdf",
      "q_cet4-2026-set2.docx", // 答案就在卷子里：只传 q，不造空 a
    ]);
    expect(fd.get("exam")).toBe("四级");
  });
});

describe("pairIntoSlots", () => {
  const pair = (names: string[], max = BATCH_MAX_PAPERS) =>
    pairIntoSlots(names.map(n => fileNamed(n)), max);
  const shape = (slots: BatchSlot[]) =>
    slots.map(s => [s.questionFile.name, s.answerFile ? s.answerFile.name : null]);

  it("q_/a_ 前缀按本名配成一套：3 套 5 个文件（含一份题答合一）不多不少", () => {
    const r = pair([
      "q_cet4-2026-set1.txt",
      "a_cet4-2026-set1.txt",
      "q_cet4-2026-set2.txt",
      "a_cet4-2026-set2.txt",
      "q_empty.txt",
    ]);
    expect(shape(r.slots)).toEqual([
      ["q_cet4-2026-set1.txt", "a_cet4-2026-set1.txt"],
      ["q_cet4-2026-set2.txt", "a_cet4-2026-set2.txt"],
      ["q_empty.txt", null],
    ]);
    expect(r.orphanAnswers).toEqual([]);
    expect(r.droppedPapers).toEqual([]);
  });

  it("题答合一（文件名无任何答案标记）每套只一个文件、答案位留空", () => {
    const r = pair(["combo-cet6-2025.txt", "高考卷.txt", "考研英语.txt"]);
    expect(shape(r.slots)).toEqual([
      ["combo-cet6-2025.txt", null],
      ["高考卷.txt", null],
      ["考研英语.txt", null],
    ]);
  });

  it("中文「答案」标记也能配对：卷子与「XX答案」剥标记后本名相同", () => {
    const r = pair(["四级真题1.txt", "四级真题1答案.txt"]);
    expect(shape(r.slots)).toEqual([["四级真题1.txt", "四级真题1答案.txt"]]);
    expect(r.orphanAnswers).toEqual([]);
  });

  it("英文 answer / key 标记同样认成答案侧", () => {
    const r = pair(["paper1.docx", "paper1-answer.docx"]);
    expect(shape(r.slots)).toEqual([["paper1.docx", "paper1-answer.docx"]]);
  });

  it("只有答案、没有同名卷子时不静默丢，单列进 orphanAnswers", () => {
    const r = pair(["a_lonely.txt"]);
    expect(r.slots).toEqual([]);
    expect(r.orphanAnswers.map(f => f.name)).toEqual(["a_lonely.txt"]);
  });

  it("上限按「套」算：3 套每套题+答共 6 个文件也全部收下，不按文件数截断", () => {
    const r = pair([
      "q_set1.txt", "a_set1.txt",
      "q_set2.txt", "a_set2.txt",
      "q_set3.txt", "a_set3.txt",
    ]);
    expect(r.slots).toHaveLength(3);
    expect(r.droppedPapers).toEqual([]);
  });

  it("超过 3 套：第 4 套卷子进 droppedPapers，前 3 套（含各自答案）保留", () => {
    const r = pair([
      "q_set1.txt", "a_set1.txt",
      "q_set2.txt", "a_set2.txt",
      "q_set3.txt", "a_set3.txt",
      "q_set4.txt", "a_set4.txt",
    ]);
    expect(r.slots).toHaveLength(3);
    expect(r.droppedPapers.map(f => f.name)).toEqual(["q_set4.txt"]);
  });
});

describe("normalizePaperOutcome", () => {
  it("成功套：后端 snake_case 的 by_qtype 归一成 byQtype，missing 原样带上", () => {
    expect(
      normalizePaperOutcome({
        key: "cet4-2026-set1",
        count: 2,
        by_qtype: { 仔细阅读: ["a", "b"] },
        missing: ["写作"],
      }),
    ).toEqual({
      key: "cet4-2026-set1",
      count: 2,
      byQtype: { 仔细阅读: ["a", "b"] },
      missing: ["写作"],
      error: null,
    });
  });

  it("失败套：只有 key+error，count/byQtype/missing 全兜底，结果页读不到 undefined", () => {
    expect(normalizePaperOutcome({ key: "cet4-2026-set3", error: "「x」缺卷子内容" })).toEqual({
      key: "cet4-2026-set3",
      count: 0,
      byQtype: {},
      missing: [],
      error: "「x」缺卷子内容",
    });
  });

  it("已经是 camel 的 byQtype 也认，不强求 snake", () => {
    expect(normalizePaperOutcome({ key: "k", count: 1, byQtype: { x: ["1"] }, missing: [] }).byQtype)
      .toEqual({ x: ["1"] });
  });

  it("null / 垃圾输入也不抛，给一套空壳而不是炸掉整页", () => {
    expect(normalizePaperOutcome(null)).toEqual({
      key: "", count: 0, byQtype: {}, missing: [], error: null,
    });
  });
});
