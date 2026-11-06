import { describe, it, expect } from "vitest";
import {
  enabledIds,
  filterSelectable,
  firstEnabledId,
  manualModel,
  partitionModels,
  removeModel,
  resolveRoles,
  rolesBoundTo,
  selectableFromDetails,
  setEnabled,
  upsertModels,
  type SelectableModel,
} from "./customModels";
import type { ModelCapability } from "./configRepo";

/* customModels 是"自定义端点拉回来的模型清单"的纯逻辑 seam：
   只测分组 / 隐藏非对话 / 勾选显隐 / 去重合并 / 手动添加，不碰网络与 React。 */

function cap(partial: Partial<ModelCapability> & { id: string }): ModelCapability {
  return {
    brand: "generic",
    contextWindow: 128000,
    reasoningLevels: [],
    supportsVision: false,
    canBeMain: true,
    ...partial,
  };
}

describe("selectableFromDetails：只留对话模型，非对话计数隐藏", () => {
  it("剔掉不能当大脑的（向量/语音/画图），其余默认勾选", () => {
    const details = [
      cap({ id: "gpt-4o", supportsVision: true }),
      cap({ id: "deepseek-chat" }),
      cap({ id: "tts-1", canBeMain: false, supportsVision: false }),
      cap({ id: "embedding-3", canBeMain: false }),
    ];
    const { models, hiddenCount } = selectableFromDetails(details);
    expect(models.map((m) => m.id)).toEqual(["gpt-4o", "deepseek-chat"]);
    expect(models.every((m) => m.enabled)).toBe(true);
    expect(hiddenCount).toBe(2);
  });
});

describe("partitionModels：按能不能看图分两组", () => {
  it("视觉项进 vision、纯文本对话项进 chat，都保留勾选态", () => {
    const models: SelectableModel[] = [
      { ...cap({ id: "gpt-4o", supportsVision: true }), enabled: true },
      { ...cap({ id: "gpt-4o-mini", supportsVision: true }), enabled: false },
      { ...cap({ id: "deepseek-chat" }), enabled: true },
    ];
    const { vision, chat } = partitionModels(models);
    expect(vision.map((m) => m.id)).toEqual(["gpt-4o", "gpt-4o-mini"]);
    expect(chat.map((m) => m.id)).toEqual(["deepseek-chat"]);
  });
});

describe("upsertModels：重新拉取与手动项并存", () => {
  it("同 id 保留用户的勾选态，新项默认启用，上游不再返回的手动项保留", () => {
    const prev: SelectableModel[] = [
      { ...cap({ id: "gpt-4o", supportsVision: true }), enabled: false }, // 用户取消勾选
      { ...cap({ id: "my-rogue-model" }), enabled: true }, // 手动加的，上游 /models 不返回
    ];
    const incoming: SelectableModel[] = [
      { ...cap({ id: "gpt-4o", supportsVision: true }), enabled: true },
      { ...cap({ id: "gpt-3.5-turbo" }), enabled: true },
    ];
    const merged = upsertModels(prev, incoming);
    const byId = Object.fromEntries(merged.map((m) => [m.id, m]));
    expect(byId["gpt-4o"].enabled).toBe(false); // 没被一键拉取复活
    expect(byId["gpt-3.5-turbo"].enabled).toBe(true); // 新模型默认勾选
    expect(byId["my-rogue-model"]).toBeDefined(); // 手动项不丢
    expect(merged.map((m) => m.id)).toEqual(["gpt-4o", "gpt-3.5-turbo", "my-rogue-model"]);
  });
});

describe("勾选 / 删除 / 搜索 / 保存投影", () => {
  const base: SelectableModel[] = [
    { ...cap({ id: "gpt-4o", supportsVision: true }), enabled: true },
    { ...cap({ id: "deepseek-chat" }), enabled: false },
    { ...cap({ id: "gpt-3.5-turbo" }), enabled: true },
  ];

  it("setEnabled 只改目标项", () => {
    const next = setEnabled(base, "gpt-4o", false);
    expect(next.find((m) => m.id === "gpt-4o")?.enabled).toBe(false);
    expect(next.find((m) => m.id === "deepseek-chat")?.enabled).toBe(false);
  });

  it("removeModel 删掉目标项，其余不动", () => {
    expect(removeModel(base, "deepseek-chat").map((m) => m.id)).toEqual([
      "gpt-4o",
      "gpt-3.5-turbo",
    ]);
  });

  it("filterSelectable 按模型名小写包含匹配", () => {
    expect(filterSelectable(base, "GPT").map((m) => m.id)).toEqual([
      "gpt-4o",
      "gpt-3.5-turbo",
    ]);
  });

  it("enabledIds 只投影勾选的模型、保持顺序", () => {
    expect(enabledIds(base)).toEqual(["gpt-4o", "gpt-3.5-turbo"]);
  });

  it("firstEnabledId 取第一个勾选项作为对话首选", () => {
    expect(firstEnabledId(base)).toBe("gpt-4o");
    expect(firstEnabledId([{ ...cap({ id: "x" }), enabled: false }])).toBeNull();
  });
});

describe("manualModel：手动按名添加走最保守的纯文本对话能力", () => {
  it("trim、默认勾选、不臆造看图/思考能力", () => {
    const m = manualModel("  GPT-4o-Mini  ")!;
    expect(m.id).toBe("GPT-4o-Mini");
    expect(m.enabled).toBe(true);
    expect(m.canBeMain).toBe(true);
    expect(m.supportsVision).toBe(false);
    expect(m.reasoningLevels).toEqual([]);
    expect(m.brand).toBe("generic");
  });

  it("空白名返回 null", () => {
    expect(manualModel("   ")).toBeNull();
  });
});

describe("resolveRoles：三槽回退链的前端镜像（删除预告 / 即时回显用）", () => {
  const models: SelectableModel[] = [
    { ...cap({ id: "deepseek-chat" }), enabled: true },
    { ...cap({ id: "gpt-4o", supportsVision: true }), enabled: true },
    { ...cap({ id: "qv-72b", supportsVision: true }), enabled: true },
  ];

  it("全部未指定：批改跟对话同款，看图取第一个能看图的", () => {
    const out = resolveRoles(models, { vision: null, grade: null }, "deepseek-chat");
    expect(out).toEqual({ chat: "deepseek-chat", vision: "gpt-4o", grade: "deepseek-chat" });
  });

  it("显式绑定优先于自动", () => {
    const out = resolveRoles(models, { vision: "qv-72b", grade: "gpt-4o" }, "deepseek-chat");
    expect(out).toEqual({ chat: "deepseek-chat", vision: "qv-72b", grade: "gpt-4o" });
  });

  it("绑定不在清单内视为失效，落回自动（对齐后端引用修复）", () => {
    const out = resolveRoles(models, { vision: "gone", grade: "gone" }, "deepseek-chat");
    expect(out).toEqual({ chat: "deepseek-chat", vision: "gpt-4o", grade: "deepseek-chat" });
  });

  it("没选主脑时 chat 落到第一个勾选模型，批改跟着它", () => {
    const out = resolveRoles(models, { vision: null, grade: null }, null);
    expect(out.chat).toBe("deepseek-chat");
    expect(out.grade).toBe("deepseek-chat");
  });

  it("纯文本清单：vision 为 null（能力不可用），批改仍跟对话", () => {
    const textOnly = models.filter((m) => !m.supportsVision);
    const out = resolveRoles(textOnly, { vision: null, grade: null }, "deepseek-chat");
    expect(out.vision).toBeNull();
    expect(out.grade).toBe("deepseek-chat");
  });
});

describe("rolesBoundTo：删除前的绑定预告", () => {
  it("报出绑定在该模型上的槽位，没绑定返回空", () => {
    const rm = { vision: "qv-72b", grade: "gpt-4o" };
    expect(rolesBoundTo("qv-72b", rm)).toEqual(["看图"]);
    expect(rolesBoundTo("gpt-4o", rm)).toEqual(["批改"]);
    expect(rolesBoundTo("deepseek-chat", rm)).toEqual([]);
  });
});
