import { describe, expect, it } from "vitest";
import { modelChipText, effortChipText, shouldShowEffortChip, railX, pickIndex, sheetHeight } from "./brainPickerLabels";

/* BrainPicker 拆成两颗后，chip 上显示什么、要不要显示，是三句纯判断。
   抽出来测，组件里只留渲染。 */
describe("modelChipText · 模型 chip 上的字", () => {
  it("有型号：显示型号名（不带强度 —— 强度拆出去了）", () => {
    expect(modelChipText({ label: "千问 3.8 27B", effort: "中", ready: true })).toBe("千问 3.8 27B");
    expect(modelChipText({ label: "qwen3.8-27b", effort: "中", ready: true })).toBe("qwen3.8-27b");
  });
  it("没配置型号：给「未配置模型」", () => {
    expect(modelChipText({ label: null, effort: null, ready: true })).toBe("未配置模型");
  });
  it("目录还在加载：给「模型…」", () => {
    expect(modelChipText({ label: null, effort: null, ready: false })).toBe("模型…");
  });
});

describe("effortChipText · 强度 chip 上的字", () => {
  it("有档位：一个字（低 / 中 / 高）", () => {
    expect(effortChipText("medium")).toBe("中");
    expect(effortChipText("high")).toBe("高");
  });
  it("空档位给空串（调用方据此整颗不渲染）", () => {
    expect(effortChipText(null)).toBe("");
    expect(effortChipText(undefined)).toBe("");
  });
});

describe("shouldShowEffortChip · 这颗 chip 什么时候出现", () => {
  it("型号支持思考（levels 非空）→ 显示", () => {
    expect(shouldShowEffortChip(["low", "medium"])).toBe(true);
  });
  it("型号不支持思考 → 整颗不出现（不是置灰 —— 不给的东西不占地方）", () => {
    expect(shouldShowEffortChip([])).toBe(false);
  });
});

/* 思考深度滑杆（照定稿样稿 §5.6 那张：更快 ←→ 更聪明，三档，两端各内缩 11px）。
   位置换算抽成纯函数，拖拽 / 点击 / 方向键都只是喂它一个坐标。 */
describe("railX · 第 i 档的滑柄落在哪", () => {
  const W = 196, INSET = 11;

  it("第一档贴左边内缩点", () => {
    expect(railX(0, 3, W, INSET)).toBe(11);
  });
  it("最后一档贴右边内缩点", () => {
    expect(railX(2, 3, W, INSET)).toBe(185);
  });
  it("中间档落在正中", () => {
    expect(railX(1, 3, W, INSET)).toBe(98);
  });
  it("只有两档时对半分", () => {
    expect(railX(0, 2, W, INSET)).toBe(11);
    expect(railX(1, 2, W, INSET)).toBe(185);
  });
  it("只有一档时停在左边，不除零", () => {
    expect(railX(0, 1, W, INSET)).toBe(11);
  });
  it("轨道太窄（宽度小于两倍内缩）也不出负数", () => {
    expect(railX(1, 3, 20, INSET)).toBe(11);
  });
});

describe("pickIndex · 手指 / 鼠标落在 x 处，该选第几档", () => {
  const W = 196, INSET = 11;

  it("点在刻度上就选那一档", () => {
    expect(pickIndex(11, 3, W, INSET)).toBe(0);
    expect(pickIndex(98, 3, W, INSET)).toBe(1);
    expect(pickIndex(185, 3, W, INSET)).toBe(2);
  });
  it("点在两档之间就近吸附（不是向下取整）", () => {
    expect(pickIndex(40, 3, W, INSET)).toBe(0);   // 靠左
    expect(pickIndex(70, 3, W, INSET)).toBe(1);   // 靠右
  });
  it("点到轨道外面会被夹住，不会越界", () => {
    expect(pickIndex(-999, 3, W, INSET)).toBe(0);
    expect(pickIndex(9999, 3, W, INSET)).toBe(2);
  });
  it("只有一档时永远返回 0", () => {
    expect(pickIndex(150, 1, W, INSET)).toBe(0);
  });
});

/* 模型列表要能滚轮滚，就得先算清面板实际多高 —— 原来写死 370，
   但列表限高 196 + 下面「更大上下文」那块，实际高度差得远，
   结果向上弹 / 向下弹判断错，面板会被切掉一半。 */
describe("sheetHeight · 面板实际多高（给「向上弹还是向下弹」用）", () => {
  it("模型面板：列表限高 + 底部那块 + 上下内边距", () => {
    expect(sheetHeight({ view: "models", listMaxH: 196, hasBottomBlock: true })).toBe(196 + 150 + 8);
  });
  it("没有「更大上下文」那块（小窗口型号）时短一截", () => {
    expect(sheetHeight({ view: "models", listMaxH: 196, hasBottomBlock: false })).toBe(196 + 8);
  });
  it("强度滑杆面板是固定的 150 高", () => {
    expect(sheetHeight({ view: "levels", listMaxH: 196, hasBottomBlock: false })).toBe(150);
  });
  it("上下文面板按 300 算", () => {
    expect(sheetHeight({ view: "context", listMaxH: 196, hasBottomBlock: false })).toBe(300);
  });
  it("列表比限高高的模型也好、矮也好，都不超过限高", () => {
    expect(sheetHeight({ view: "models", listMaxH: 196, hasBottomBlock: false })).toBeLessThanOrEqual(196 + 150 + 8);
  });
});
