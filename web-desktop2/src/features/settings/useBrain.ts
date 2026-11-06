/**
 * 大脑选择的 React 薄封装：拉清单 + 拉现状 → 用 brainRepo 纯函数决策 →
 * 仅在后端型号 / 档位本身非法时纠偏并回推后端 → 把后端权威值写回本地缓存；
 * 用户切换时乐观更新、失败回滚。
 *
 * 决策规则本身（默认值 / 后端非法时纠偏 / 要不要同步）都在 `brainRepo` 纯函数里，
 * 这里只管"什么时候发请求、怎么摆 React 状态"。从正式版 web-desktop 移植。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import {
  getAgentConfig,
  getBrainModels,
  saveBrainChoice,
  saveContextMode,
  type BrainModelsResponse,
} from "./configRepo";
import {
  DEFAULT_EFFORT,
  effortForModel,
  readCachedChoice,
  resolveInitialChoice,
  writeCachedChoice,
  type BrainChoice,
  type Effort,
} from "./brainRepo";
import {
  normalizeContextMode,
  readContextMode,
  writeContextMode,
  type ContextMode,
} from "./contextPrefRepo";

export type BrainApi = {
  /** 清单与当前选择都到位才为 true（未就绪时胶囊不可点开） */
  ready: boolean;
  catalog: BrainModelsResponse | null;
  choice: BrainChoice | null;
  error: string | null;
  /** 切换大脑；effort 省略时按该型号能力落到合适档 */
  select: (model: string, effort?: Effort) => Promise<void>;
  /** 清掉切换失败的提示 */
  clearError: () => void;
  /** 最大上下文档位（标准 30 万 / Max 100 万）；本地乐观显示，以后端确认为准 */
  contextMode: ContextMode;
  /** 切上下文档位；本地立即生效并同步后端，忙碌 / 失败时回滚并给错误提示 */
  selectContextMode: (mode: ContextMode) => Promise<void>;
  /** 当前所选模型能不能看图 / 拍照认字；false 时前端禁用图片入口（纯文本功能不受影响） */
  supportsVision: boolean;
  /** 重新拉模型清单与当前配置（切换供应商 / 自定义端点保存后必须调，否则选择器还是旧清单） */
  refresh: () => Promise<void>;
};

export function useBrain(online: boolean): BrainApi {
  const [catalog, setCatalog] = useState<BrainModelsResponse | null>(null);
  // 初值取本地缓存只为"后端返回前先显示一下"；后端一回来就以后端为准校正（BUG-02）
  const [choice, setChoice] = useState<BrainChoice | null>(() => readCachedChoice());
  const [error, setError] = useState<string | null>(null);
  // 上下文档位：先 lazy 读本地即时显示，拉到后端配置后再以后端档位校正；切换走乐观更新
  const [contextMode, setContextMode] = useState<ContextMode>(() => readContextMode());
  // 未拉到配置前先按"能看图"对待，避免内置档加载瞬间误禁图片入口；拿到 cfg 立即校正
  const [supportsVision, setSupportsVision] = useState(true);
  const startedRef = useRef(false);

  /** 拉清单 + 拉现状 → 纠偏 → 回写缓存。启动一次，切换供应商后由 refresh() 再拉。 */
  const load = useCallback(async (alive: () => boolean = () => true) => {
    try {
      const [cat, cfg] = await Promise.all([getBrainModels(), getAgentConfig()]);
      if (!alive()) return;
      setCatalog(cat);
      // 档位以后端为装配唯一配置源：拉到后校正本地（多端 / 重开都以后端为准）
      const backendMode = normalizeContextMode(cfg.contextMode);
      setContextMode(backendMode);
      writeContextMode(backendMode);
      setSupportsVision(cfg.supportsVision);
      const decision = resolveInitialChoice({
        catalog: cat,
        backend: { model: cfg.model, effort: cfg.reasoningEffort },
      });
      let settled = decision.choice;
      if (decision.needsSync) {
        // 仅当后端型号 / 档位本身非法、我们纠了偏时才回推（缓存与后端不一致**不**回推——
        // 否则一次降级到 kimi-k3 的缓存会被反复覆盖用户所选，正是 BUG-02）。
        const updated = await saveBrainChoice(decision.choice.model, decision.choice.effort);
        if (!alive()) return;
        settled = { model: updated.model, effort: updated.reasoningEffort as Effort };
      }
      setChoice(settled);
      // 以后端为准把缓存刷成权威值：下次启动即便先显示缓存，也已经是后端那份
      writeCachedChoice(settled);
    } catch (e) {
      if (alive()) setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    if (!online || startedRef.current) return;
    startedRef.current = true;
    let alive = true;
    void load(() => alive);
    return () => {
      alive = false;
    };
  }, [online, load]);

  /** 切换供应商 / 保存自定义端点后调用：清单与当前脑都重新对齐（否则选择器还是旧清单）。 */
  const refresh = useCallback(async () => {
    startedRef.current = true;
    await load();
  }, [load]);

  const select = useCallback(
    async (model: string, effort?: Effort) => {
      setError(null);
      const m = catalog?.models.find((x) => x.id === model);
      const eff = effortForModel(m?.reasoning_levels ?? [], effort ?? DEFAULT_EFFORT) ?? DEFAULT_EFFORT;
      const prev = choice;
      const next: BrainChoice = { model, effort: eff };
      setChoice(next); // 乐观更新，失败再回滚
      try {
        const updated = await saveBrainChoice(model, eff);
        setChoice({ model: updated.model, effort: updated.reasoningEffort as Effort });
        writeCachedChoice(next);
      } catch (e) {
        if (prev) setChoice(prev);
        setError((e as Error).message);
      }
    },
    [catalog, choice],
  );

  const clearError = useCallback(() => setError(null), []);

  const selectContextMode = useCallback(
    async (mode: ContextMode) => {
      setError(null);
      const next = normalizeContextMode(mode);
      const prev = contextMode;
      setContextMode(next); // 乐观更新：用量环立即变，请求失败再回滚
      writeContextMode(next);
      try {
        const updated = await saveContextMode(next);
        const settled = normalizeContextMode(updated.contextMode);
        setContextMode(settled);
        writeContextMode(settled);
      } catch (e) {
        setContextMode(prev);
        writeContextMode(prev);
        setError((e as Error).message);
      }
    },
    [contextMode],
  );

  return {
    ready: Boolean(catalog && choice),
    catalog,
    choice,
    error,
    select,
    clearError,
    contextMode,
    selectContextMode,
    supportsVision,
    refresh,
  };
}
