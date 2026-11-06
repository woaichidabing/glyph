import { describe, expect, it } from "vitest";
import {
  buildInlineEssay,
  detectionsWithVerdicts,
  recheckOptions,
  type GradeDecision,
  type RecheckVerdictLike,
} from "./inlineGrade";
import type { DetectionDTO } from "./batch";

const det = (d: Partial<DetectionDTO>): DetectionDTO => ({
  id: "x",
  original: "",
  source: "model",
  ...d,
});

const TEXT = "He go to school every day. She like apples.";

// d1: He go -> He goes（区间 [0,5)）；d2: like -> likes（[31,35)）
const D1 = det({ id: "d1", original: "He go", corrected: "He goes", start: 0, end: 5, aligned: true, tier: "error", type: "hard", explanation_zh: "主谓一致" });
const D2 = det({ id: "d2", original: "like", corrected: "likes", start: 31, end: 35, aligned: true, tier: "error", type: "hard", explanation_zh: "三单" });
const DETECTIONS = [D1, D2];

describe("buildInlineEssay · 段渲染", () => {
  it("没有决定时：两处都是 pending，编号按原文位置 1/2，普通文本夹在中间", () => {
    const r = buildInlineEssay(TEXT, DETECTIONS, {});
    const kinds = r.segments.map((s) => s.kind);
    expect(kinds).toEqual(["pending", "plain", "pending", "plain"]);
    const pend = r.segments.filter((s) => s.kind === "pending") as Extract<
      ReturnType<typeof buildInlineEssay>["segments"][number],
      { kind: "pending" }
    >[];
    expect(pend.map((p) => p.index)).toEqual([1, 2]);
    expect(pend[0].text).toBe("He go");
  });

  it("采纳：段落变 accepted（原词 + 改法）；驳回：原文按普通文本保留，不动一个字", () => {
    const decisions: Record<string, GradeDecision> = {
      d1: { action: "accepted" },
      d2: { action: "rejected" },
    };
    const r = buildInlineEssay(TEXT, DETECTIONS, decisions);
    const acc = r.segments.find((s) => s.kind === "accepted");
    expect(acc).toMatchObject({ oldText: "He go", newText: "He goes", mine: false });
    // 驳回的 like 留在普通文本里
    const plain = r.segments.filter((s) => s.kind === "plain").map((s) => (s as { text: string }).text).join("|");
    expect(plain).toContain("like");
  });

  it("我来改：accepted 段标 mine，改法取 override", () => {
    const r = buildInlineEssay(TEXT, [D1], { d1: { action: "edited", override: "He went" } });
    expect(r.segments.find((s) => s.kind === "accepted")).toMatchObject({
      oldText: "He go",
      newText: "He went",
      mine: true,
    });
  });

  it("改后全文：采纳/自己改的替换进去，驳回的保持原样", () => {
    const r = buildInlineEssay(TEXT, DETECTIONS, {
      d1: { action: "edited", override: "He went" },
      d2: { action: "rejected" },
    });
    expect(r.changedText).toBe("He went to school every day. She like apples.");
  });
});

describe("buildInlineEssay · 进度与结账", () => {
  it("全未处理：allDecided=false；每条都有归宿（采纳/改/驳回/复核撤销）后才 true", () => {
    expect(buildInlineEssay(TEXT, DETECTIONS, {}).progress).toEqual({
      total: 2, decided: 0, accepted: 0, edited: 0, rejected: 0,
    });
    const half = buildInlineEssay(TEXT, DETECTIONS, { d1: { action: "accepted" } });
    expect(half.progress.decided).toBe(1);
    expect(half.allDecided).toBe(false);

    const done = buildInlineEssay(TEXT, DETECTIONS, {
      d1: { action: "accepted" },
      d2: { action: "rejected" },
    });
    expect(done.allDecided).toBe(true);
  });

  it("0 条 detection：allDecided=true，changedText 就是原文", () => {
    const r = buildInlineEssay(TEXT, [], {});
    expect(r.allDecided).toBe(true);
    expect(r.changedText).toBe(TEXT);
    expect(r.segments).toEqual([{ kind: "plain", text: TEXT }]);
  });
});

describe("buildInlineEssay · 台内复核结果", () => {
  it("复核「改判」：卡片与改后文本采用新改法/新讲解，状态仍待拍板", () => {
    const verdicts: Record<string, RecheckVerdictLike> = {
      d1: { status: "revised", corrected: "He is going", explanation_zh: "新理由：进行时" },
    };
    const r = buildInlineEssay(TEXT, [D1], {}, verdicts);
    expect(r.cards[0].effective.corrected).toBe("He is going");
    expect(r.cards[0].effective.explanation_zh).toBe("新理由：进行时");
    expect(r.cards[0].recheckStatus).toBe("revised");
    expect(r.allDecided).toBe(false);
  });

  it("复核「维持」：讲解与改法仍是原来的", () => {
    const verdicts: Record<string, RecheckVerdictLike> = {
      d1: { status: "upheld", round: 1, reason_zh: "还是要改" },
    };
    const r = buildInlineEssay(TEXT, [D1], {}, verdicts);
    expect(r.cards[0].effective.corrected).toBe("He goes");
    expect(r.cards[0].recheckStatus).toBe("upheld");
    expect(r.cards[0].recheckRound).toBe(1);
  });

  it("复核「撤销」：算已决定且不进改后全文，原文保留，卡片标 withdrawn", () => {
    const verdicts: Record<string, RecheckVerdictLike> = { d1: { status: "withdrawn" } };
    const r = buildInlineEssay(TEXT, [D1], {}, verdicts);
    expect(r.cards[0].recheckStatus).toBe("withdrawn");
    expect(r.allDecided).toBe(true);
    expect(r.changedText).toBe(TEXT);
    expect(r.segments.some((s) => s.kind === "pending")).toBe(false);
  });

  it("撤销后用户在卡片上点「恢复」：清掉 verdict 即可回到待处理", () => {
    // 恢复动作 = 组件把 verdicts[d1] 删掉重新计算
    const withdrawn = buildInlineEssay(TEXT, [D1], {}, { d1: { status: "withdrawn" } });
    expect(withdrawn.allDecided).toBe(true);
    const restored = buildInlineEssay(TEXT, [D1], {}, {});
    expect(restored.allDecided).toBe(false);
    expect(restored.cards[0].recheckStatus).toBeUndefined();
  });
});

describe("buildInlineEssay · 定位不到的条", () => {
  it("aligned=false / 没有 start 的条不进正文段，落进 loose 卡片，仍计入待处理", () => {
    const loose = det({ id: "d3", original: "good", corrected: "well", aligned: false });
    const r = buildInlineEssay(TEXT, [D1, loose], {});
    expect(r.segments.some((s) => s.kind === "pending" && (s as { detId: string }).detId === "d3")).toBe(false);
    expect(r.looseIds).toEqual(["d3"]);
    expect(r.allDecided).toBe(false);
    const card = r.cards.find((c) => c.id === "d3");
    expect(card?.inline).toBe(false);
  });
});

describe("detectionsWithVerdicts · 改判结论合并进定稿", () => {
  it("revised：新改法/新讲解/新分级覆盖旧字段，其余字段不动", () => {
    const out = detectionsWithVerdicts(
      [D1, D2],
      { d1: { status: "revised", corrected: "He is going", explanation_zh: "进行时", tier: "suggestion", type: "grammar" } },
    );
    expect(out[0].corrected).toBe("He is going");
    expect(out[0].explanation_zh).toBe("进行时");
    expect(out[0].tier).toBe("suggestion");
    expect(out[0].type).toBe("grammar");
    expect(out[0].start).toBe(0);
    // 没改判的条原样
    expect(out[1]).toEqual(D2);
  });

  it("revised 没带的字段（null/缺省）不能把原字段抹成空", () => {
    const out = detectionsWithVerdicts(
      [D1],
      { d1: { status: "revised", corrected: null, explanation_zh: "只换讲解" } },
    );
    expect(out[0].corrected).toBe("He goes");
    expect(out[0].explanation_zh).toBe("只换讲解");
  });

  it("upheld / withdrawn / 没有 verdict：detection 一个字段都不改", () => {
    const out = detectionsWithVerdicts(
      [D1, D2],
      { d1: { status: "upheld", reason_zh: "还是要改" }, d2: { status: "withdrawn" } },
    );
    expect(out[0]).toEqual(D1);
    expect(out[1]).toEqual(D2);
  });
});

describe("recheckOptions · 驳回出口不卡死", () => {
  const cardOf = (verdict?: RecheckVerdictLike) =>
    buildInlineEssay(TEXT, [D1], {}, verdict ? { d1: verdict } : {}).cards[0];

  it("新卡片·还有次数·没报错：只给模型复核，不直接给保持原文（驳回先听第二意见）", () => {
    const g = recheckOptions(cardOf(), 2, false);
    expect(g.canAskModel).toBe(true);
    expect(g.canKeepOriginal).toBe(false);
    expect(g.recheckLabel).toBe("驳回");
  });

  it("复核维持/改判回来后：可以采纳新建议，也可以坚持驳回；模型按钮变「再驳回（剩 N）」", () => {
    const g = recheckOptions(cardOf({ status: "upheld", round: 1 }), 1, false);
    expect(g.canAskModel).toBe(true);
    expect(g.canKeepOriginal).toBe(true);
    expect(g.recheckLabel).toBe("再驳回（剩 1 次）");
  });

  it("配额用尽：模型复核封死，但「保持原文」必须在——不能卡在看完了之前", () => {
    const g = recheckOptions(cardOf({ status: "upheld", round: 2 }), 0, false);
    expect(g.canAskModel).toBe(false);
    expect(g.canKeepOriginal).toBe(true);
  });

  it("复核请求失败（网络/502/409 已刷成 0）：给出保持原文出口，且允许重试模型", () => {
    const failedFresh = recheckOptions(cardOf(), null, true);
    expect(failedFresh.canAskModel).toBe(true);
    expect(failedFresh.canKeepOriginal).toBe(true);
    const exhausted = recheckOptions(cardOf(), 0, true);
    expect(exhausted.canAskModel).toBe(false);
    expect(exhausted.canKeepOriginal).toBe(true);
  });

  it("后端不可达（quota=null）且没报错：不拦复核，文案不带剩余次数", () => {
    const g = recheckOptions(cardOf({ status: "revised" }), null, false);
    expect(g.canAskModel).toBe(true);
    expect(g.recheckLabel).toBe("再驳回");
  });
});


describe("没有 id 的 detection（真机三篇批改后点不动的那次）", () => {
  const NOID = [
    { original: "go", corrected: "went", start: 0, end: 2, alignment: "ok" },
    { original: "was", corrected: "were", start: 10, end: 13, alignment: "ok" },
  ] as never;

  it("卡片用 det-N 兜底，采纳能落到卡片上", () => {
    const essay = buildInlineEssay("go to park. We was happy.", NOID, {
      "det-1": { action: "accepted" },
    });
    expect(essay.cards[0].id).toBe("det-1");
    expect(essay.cards[0].decided).toBe(true);
    expect(essay.progress.accepted).toBe(1);
  });

  it("全部采纳（给 det-N）后每条都有归宿", () => {
    const essay = buildInlineEssay("go to park. We was happy.", NOID, {
      "det-1": { action: "accepted" },
      "det-2": { action: "accepted" },
    });
    expect(essay.allDecided).toBe(true);
    expect(essay.progress).toMatchObject({ total: 2, accepted: 2 });
  });
});
