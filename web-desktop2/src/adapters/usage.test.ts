import { describe, expect, it } from "vitest";
import { emptyTimeline, reduce, type TimelineNode } from "./askTimeline";
import { CACHE_MIN_PREFIX_TOKENS, deriveUsage, friendlyTokens } from "./usage";

/* ---------------- 造数据 ---------------- */

type TurnSpec = { input: number; cached: number | null; output?: number; tool?: boolean };

/**
 * 造一段连续时间线（一条 reduce 链走到底）。
 *
 * ⚠️ 必须连续：节点 id 由时间线的 seq 算出来，每轮各起一条新链会让所有终态的 id 撞成
 * 同一个，而"哪几轮要解释"正是按节点 id 挂的——撞 id 的测试数据测不出真问题。
 */
function timelineOf(specs: TurnSpec[]): TimelineNode[] {
  let tl = emptyTimeline();
  for (const s of specs) {
    // 用户节点由 hook 直接追加（归约器没有 user 分支），照真实流程来：
    // 上一轮的终态之后必须接一条 user，下一轮的 result 才会被认（hasTerminal 只看末尾）
    tl = {
      ...tl,
      seq: tl.seq + 1,
      nodes: [
        ...tl.nodes,
        {
          id: `u-${tl.seq}`,
          kind: "user",
          text: "问一句",
          attachments: [],
          sortKey: tl.seq,
        },
      ],
    };
    tl = reduce(tl, { type: "run_started" });
    if (s.tool) {
      tl = reduce(tl, { type: "tool_call", id: `c${tl.seq}`, name: "grade_text", args: {} });
    }
    tl = reduce(tl, {
      type: "result",
      finishReason: "final_text",
      usage: {
        inputTokens: s.input,
        outputTokens: s.output ?? 10,
        cachedTokens: s.cached,
      },
    });
  }
  return tl.nodes;
}

const terminalsOf = (nodes: TimelineNode[]) => nodes.filter((n) => n.kind === "terminal");

/* ---------------- 汇总 ---------------- */

describe("deriveUsage · 汇总", () => {
  it("按轮累加输入 / 输出 / 命中缓存，并数出轮数与工具步数", () => {
    const nodes = timelineOf([
      { input: 15000, cached: 14000 },
      { input: 15000, cached: 0, tool: true },
    ]);

    const { summary } = deriveUsage(nodes);

    expect(summary.turns).toBe(2);
    expect(summary.steps).toBe(1);
    expect(summary.inputTokens).toBe(30000);
    expect(summary.cachedTokens).toBe(14000);
  });

  it("省了多少是**按钱**算的：命中部分按 2 折计费 → 省 80%", () => {
    // 命中 14000 / 输入 30000 ≈ 46.7% → 省 46.7% × 0.8 ≈ 37%
    const nodes = timelineOf([
      { input: 15000, cached: 14000 },
      { input: 15000, cached: 0 },
    ]);

    const { summary } = deriveUsage(nodes);

    expect(summary.hitPct).toBe(47);
    expect(summary.savedPct).toBe(37);
  });

  it("模型从没给过缓存明细时两个百分比都是 null —— 未知，绝不谎报 0%", () => {
    const nodes = timelineOf([
      { input: 15000, cached: null },
      { input: 15000, cached: null },
    ]);

    const { summary } = deriveUsage(nodes);

    expect(summary.inputTokens).toBe(30000);
    expect(summary.cachedTokens).toBe(0);
    expect(summary.hitPct).toBeNull();
    expect(summary.savedPct).toBeNull();
  });

  it("一条用量都没有（历史回放）时输入为 0，界面据此整行不显示", () => {
    const nodes: TimelineNode[] = [
      { id: "u-1", kind: "user", text: "问一句", attachments: [], sortKey: 0 },
    ];

    const { summary } = deriveUsage(nodes);

    expect(summary.turns).toBe(1);
    expect(summary.inputTokens).toBe(0);
    expect(summary.hitPct).toBeNull();
  });
});

/* ---------------- 没省到时的解释 ---------------- */

describe("deriveUsage · 没省到", () => {
  it("第一轮没省到：说成「新会话刚开始」，不吓唬人", () => {
    const nodes = timelineOf([{ input: 15000, cached: 0 }]);

    const { missNotes } = deriveUsage(nodes);
    const [terminal] = terminalsOf(nodes);

    expect(missNotes.get(terminal.id)).toContain("新会话");
  });

  it("后面几轮没省到：说成「公共前缀变了」，同一个原因只提示一次", () => {
    const nodes = timelineOf([
      { input: 15000, cached: 14000 }, // 省到了，不提示
      { input: 15000, cached: 300 }, // 没省到 → 提示
      { input: 16000, cached: 0 }, // 又没省到 → 同一个原因不再念
    ]);

    const { missNotes } = deriveUsage(nodes);
    const terminals = terminalsOf(nodes);

    expect(missNotes.size).toBe(1);
    expect(missNotes.get(terminals[1].id)).toContain("前缀");
    expect(missNotes.has(terminals[2].id)).toBe(false);
  });

  it("高命中不提示", () => {
    const nodes = timelineOf([{ input: 15000, cached: 14000 }]);

    expect(deriveUsage(nodes).missNotes.size).toBe(0);
  });

  it("输入还没到平台的缓存门槛（1024）时不提示：本来就没资格命中", () => {
    const nodes = timelineOf([{ input: CACHE_MIN_PREFIX_TOKENS - 1, cached: 0 }]);

    expect(deriveUsage(nodes).missNotes.size).toBe(0);
  });

  it("模型没给缓存明细时不提示：不能拿「没数据」当「没省到」", () => {
    const nodes = timelineOf([{ input: 15000, cached: null }]);

    expect(deriveUsage(nodes).missNotes.size).toBe(0);
  });
});

/* ---------------- 历史会话的合计（切回旧会话时状态条不该空） ---------------- */

describe("deriveUsage · 历史合计", () => {
  it("后端回灌的合计算进去：切回旧会话时数字还在", () => {
    const nodes = timelineOf([{ input: 15000, cached: 14000 }]);

    const { summary } = deriveUsage(nodes, {
      inputTokens: 30000,
      outputTokens: 200,
      cachedTokens: 28000,
    });

    expect(summary.inputTokens).toBe(45000);
    expect(summary.cachedTokens).toBe(42000);
    expect(summary.hitPct).toBe(93); // 42000 / 45000
    expect(summary.savedPct).toBe(75); // 0.8 × 93.3
  });

  it("只有合计、没有逐轮明细（纯回放的旧会话）也能给百分比", () => {
    const nodes: TimelineNode[] = [
      { id: "u-1", kind: "user", text: "问一句", attachments: [], sortKey: 0 },
    ];

    const { summary } = deriveUsage(nodes, {
      inputTokens: 45000,
      outputTokens: 270,
      cachedTokens: 42000,
    });

    expect(summary.turns).toBe(1);
    expect(summary.hitPct).toBe(93);
  });

  it("合计里缓存未知时不给百分比（未知不是 0）", () => {
    const { summary } = deriveUsage([], {
      inputTokens: 45000,
      outputTokens: 270,
      cachedTokens: null,
    });

    expect(summary.hitPct).toBeNull();
    expect(summary.savedPct).toBeNull();
  });

  it("历史合计不吃掉实时那几轮的解释", () => {
    const nodes = timelineOf([{ input: 15000, cached: 0 }]);

    const { missNotes } = deriveUsage(nodes, {
      inputTokens: 30000,
      outputTokens: 200,
      cachedTokens: 28000,
    });

    expect(missNotes.size).toBe(1);
  });
});

/* ---------------- 数字写法 ---------------- */

describe("friendlyTokens", () => {
  it("大写 K/M、一位小数（照参考图的 148.2K/256.0K · 57.9%）", () => {
    expect(friendlyTokens(677)).toBe("677");
    expect(friendlyTokens(999)).toBe("999");
    expect(friendlyTokens(1000)).toBe("1.0K");
    expect(friendlyTokens(1500)).toBe("1.5K");
    expect(friendlyTokens(8947)).toBe("8.9K");
    expect(friendlyTokens(15074)).toBe("15.1K");
    expect(friendlyTokens(184000)).toBe("184.0K");
    expect(friendlyTokens(300000)).toBe("300.0K");
    expect(friendlyTokens(999000)).toBe("999.0K");
    expect(friendlyTokens(999999)).toBe("1.0M"); // 四舍五入到 M，不给"1000.0K"这种怪数
    expect(friendlyTokens(1000000)).toBe("1.0M");
    expect(friendlyTokens(1600000)).toBe("1.6M");
  });

  it("0 / 负数 / 非法值都给 0，不给 NaN", () => {
    expect(friendlyTokens(0)).toBe("0");
    expect(friendlyTokens(-5)).toBe("0");
    expect(friendlyTokens(Number.NaN)).toBe("0");
  });
});
