import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  fetchProviderModels,
  getAgentConfig,
  getBrainModels,
  saveCustomProvider,
  saveDefaultProviderKey,
  saveRoleModels,
  saveSpeakingStyle,
} from "./configRepo";

/* configRepo 是 /api/config 的前端 seam。这里只锁说话风格：GET 回显映射、POST 只发风格。 */

function mockJson(body: unknown, ok = true) {
  return { ok, status: ok ? 200 : 400, json: async () => body } as unknown as Response;
}

describe("说话风格 speaking_style", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("GET 把后端 speaking_style 映射成 speakingStyle", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      mockJson({
        has_key: true,
        base_url: "x",
        model: "m",
        reasoning_effort: "medium",
        context_mode: "standard",
        speaking_style: "witty",
      }),
    );

    expect((await getAgentConfig()).speakingStyle).toBe("witty");
  });

  it("GET 缺字段时 speakingStyle 回退 natural", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(mockJson({ has_key: false }));

    expect((await getAgentConfig()).speakingStyle).toBe("natural");
  });

  it("saveSpeakingStyle 只 POST speaking_style，不带钥匙 / 模型", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(mockJson({ has_key: true, speaking_style: "calm" }));

    const cfg = await saveSpeakingStyle("calm");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/config");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({ speaking_style: "calm" });
    expect(cfg.speakingStyle).toBe("calm");
  });
});

describe("模型来源 source / custom / supports_vision", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("GET 完整映射自定义档来源、能力与自定义详情", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      mockJson({
        provider: "custom",
        source: "custom",
        has_key: true,
        base_url: "https://relay.example.com/v1",
        model: "gpt-4o",
        supports_vision: true,
        custom: {
          base_url: "https://relay.example.com/v1",
          has_key: true,
          models: ["gpt-4o", "deepseek-chat"],
          selected_model: "gpt-4o",
        },
      }),
    );

    const cfg = await getAgentConfig();
    expect(cfg.source).toBe("custom");
    expect(cfg.provider).toBe("custom"); // 旧字段随来源变为 custom
    expect(cfg.supportsVision).toBe(true);
    expect(cfg.custom).toEqual({
      baseUrl: "https://relay.example.com/v1",
      hasKey: true,
      models: ["gpt-4o", "deepseek-chat"],
      selectedModel: "gpt-4o",
      details: [],
    });
  });

  it("GET 缺字段时回退默认档、无看图能力、custom 为空结构", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(mockJson({ has_key: false }));

    const cfg = await getAgentConfig();
    expect(cfg.source).toBe("default");
    expect(cfg.provider).toBe("dashscope");
    expect(cfg.supportsVision).toBe(false);
    expect(cfg.custom).toEqual({
      baseUrl: "",
      hasKey: false,
      models: [],
      selectedModel: "",
      details: [],
    });
  });

  it("GET 来源是脏值时一律回退默认档", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      mockJson({ source: "weird", supports_vision: "yes" }),
    );

    const cfg = await getAgentConfig();
    expect(cfg.source).toBe("default");
    expect(cfg.supportsVision).toBe(false); // 非布尔 true 不认
  });

  it("saveDefaultProviderKey 只发 default 来源段与千问 Key", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(mockJson({ source: "default", has_key: true }));

    const cfg = await saveDefaultProviderKey("sk-qwen");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/config");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({
      provider: { source: "default", default_api_key: "sk-qwen" },
    });
    expect(cfg.source).toBe("default");
  });
});

describe("自定义端点：拉取上游模型 / 保存自定义档", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("fetchProviderModels POST 地址与 Key，回传 id 清单与能力 details", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      mockJson({
        models: ["gpt-4o", "tts-1"],
        count: 2,
        details: [
          {
            id: "gpt-4o",
            brand: "openai",
            context_window: 128000,
            reasoning_levels: [],
            supports_vision: true,
            can_be_main: true,
          },
          {
            id: "tts-1",
            brand: "openai",
            context_window: 128000,
            reasoning_levels: [],
            supports_vision: false,
            can_be_main: false,
          },
        ],
      }),
    );

    const res = await fetchProviderModels("https://x/v1/", "sk");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/provider/fetch-models");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({
      base_url: "https://x/v1/",
      api_key: "sk",
    });
    expect(res.models).toEqual(["gpt-4o", "tts-1"]);
    expect(res.details).toHaveLength(2);
    expect(res.details[0]).toMatchObject({ id: "gpt-4o", supportsVision: true, canBeMain: true });
  });

  it("fetchProviderModels 失败时抛出后端人话 detail", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      mockJson({ detail: "API Key 不对或没有权限" }, false),
    );
    await expect(fetchProviderModels("https://x/v1", "bad")).rejects.toThrow(
      "API Key 不对或没有权限",
    );
  });

  it("saveCustomProvider 发 custom 来源段（地址/Key/勾选模型/首选）", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(mockJson({ source: "custom", provider: "custom" }));

    const cfg = await saveCustomProvider({
      baseUrl: "https://x/v1",
      apiKey: "sk",
      models: ["gpt-4o", "deepseek-chat"],
      selectedModel: "gpt-4o",
    });

    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/config");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual({
      provider: {
        source: "custom",
        custom: {
          base_url: "https://x/v1",
          api_key: "sk",
          models: ["gpt-4o", "deepseek-chat"],
          selected_model: "gpt-4o",
        },
      },
    });
    expect(cfg.source).toBe("custom");
  });
});

describe("GET /api/models 下发的自定义能力字段", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("映射 brand / supports_vision；缺字段一律保守（不假装能看图 / 联网）", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      mockJson({
        default: "gpt-4o",
        models: [
          {
            id: "gpt-4o",
            label: "gpt-4o",
            desc: "能看图、可拍照认字",
            reasoning_levels: [],
            web_search: false,
            context_window: 128000,
            supports_vision: true,
            brand: "openai",
          },
          { id: "mystery", label: "mystery", reasoning_levels: ["low"] },
        ],
      }),
    );

    const cat = await getBrainModels();
    expect(cat.default).toBe("gpt-4o");
    const [gpt, mystery] = cat.models;
    expect(gpt.brand).toBe("openai");
    expect(gpt.supports_vision).toBe(true);
    // 缺字段：品牌空串、看不了图、不能联网、窗口 0，交给 UI 走保守分支
    expect(mystery.brand).toBe("");
    expect(mystery.supports_vision).toBe(false);
    expect(mystery.web_search).toBe(false);
    expect(mystery.context_window).toBe(0);
  });
});

describe("三能力槽 roles 回显与 role_models 提交", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("GET 把后端 roles 映射成 roles，缺字段按未绑定兜", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      mockJson({
        has_key: true,
        roles: {
          chat: { bound: "m1", effective: "m1" },
          vision: { bound: null, effective: "m2", available: true },
          grade: { bound: "m3", effective: "m3" },
        },
        role_fallbacks: [],
      }),
    );
    const cfg = await getAgentConfig();
    expect(cfg.roles.chat.effective).toBe("m1");
    expect(cfg.roles.vision).toEqual({ bound: null, effective: "m2", available: true });
    expect(cfg.roles.grade.bound).toBe("m3");
    expect(cfg.roleFallbacks).toEqual([]);
  });

  it("GET 缺 roles 整段：三槽按未绑定 / 不可用兜，不炸渲染", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(mockJson({ has_key: false }));
    const cfg = await getAgentConfig();
    expect(cfg.roles.chat).toEqual({ bound: null, effective: null });
    expect(cfg.roles.vision).toEqual({ bound: null, effective: null, available: false });
    expect(cfg.roles.grade).toEqual({ bound: null, effective: null });
  });

  it("saveRoleModels 只发 provider.role_models（两槽都带，null 原样传）", async () => {
    const spy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(mockJson({ has_key: true, source: "default" }));
    await saveRoleModels("default", { vision: "qv", grade: null });
    const body = JSON.parse((spy.mock.calls[0]?.[1] as RequestInit).body as string);
    expect(body).toEqual({
      provider: { source: "default", role_models: { vision: "qv", grade: null } },
    });
  });

  it("saveCustomProvider 带上 role_models；保存响应里的 role_fallbacks 进配置", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      mockJson({
        has_key: true,
        source: "custom",
        role_fallbacks: ["vision"],
        roles: { chat: {}, vision: {}, grade: {} },
      }),
    );
    const cfg = await saveCustomProvider({
      baseUrl: "https://x/v1",
      apiKey: null,
      models: ["m1"],
      roleModels: { vision: null, grade: "m1" },
    });
    const body = JSON.parse((spy.mock.calls[0]?.[1] as RequestInit).body as string);
    expect(body.provider.role_models).toEqual({ vision: null, grade: "m1" });
    expect(cfg.roleFallbacks).toEqual(["vision"]);
  });

  it("saveCustomProvider 不带 roleModels 时绝不发 role_models 键（不误清后端绑定）", async () => {
    const spy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(mockJson({ has_key: true, source: "custom" }));
    await saveCustomProvider({ baseUrl: "https://x/v1", apiKey: null, models: ["m1"] });
    const body = JSON.parse((spy.mock.calls[0]?.[1] as RequestInit).body as string);
    expect(body.provider.role_models).toBeUndefined();
  });
});
