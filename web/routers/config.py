"""config 路由（独立模块，app.include_router 挂载）。

本模块承载两类接口：
1. 老接口（GET/POST /api/config、/api/provider/fetch-models、/api/tools）：现有前端在用，
   行为一律保持不变；
2. 新接口（/api/providers 增删改查 + 切档、GET /api/models 按当前档下发）：多档供应商口径。
两者共用同一个落地点（runtime._apply_rebuild）：写成功后立刻生效，不必重启进程。
"""

from typing import Any

from fastapi import APIRouter

from .. import runtime as rout
from ..deps import state

router = APIRouter()


def _remember_effort(effort: str) -> None:
    """把"这一档选的思考档"记进配置（重启后恢复；DSH 也是按模型记住档位）。"""
    book = rout.provider_book()
    active = rout.active_provider_id(book)
    if not active:
        return
    rout.persist_book(rout.replace(book, last_efforts={**book.last_efforts, active: effort}))


def _validate_model_fields(models: list[Any]) -> None:
    """窗口 / 最大输出必须是正数；思考档除「关」外都要有非空参数值。

    文案照 DSH 原文（见 docs/design/dsh-model-page-reference.md）。
    """
    for raw in models or []:
        data = raw if isinstance(raw, dict) else {}
        model_id = str(data.get("id") or "").strip() or "（未填 ID）"
        for key, message in (
            ("context_window", "上下文窗口必须是正数，例如 131072、256K 或 1M。"),
            ("max_output", "最大输出 token 数必须是正数，例如 8192、64K 或 1M。"),
        ):
            value = data.get(key)
            if value in (None, "", 0, "0"):
                continue
            try:
                number = int(str(value).strip())
            except (TypeError, ValueError):
                raise rout.HTTPException(status_code=400, detail=message) from None
            if number <= 0:
                raise rout.HTTPException(status_code=400, detail=message)
        levels = data.get("reasoning_levels") or []
        values = data.get("reasoning_values")
        if not isinstance(values, dict):
            values = {}
        for level in levels:
            if level == "off":
                continue
            # 显式清空 → 拦下（DSH 的判法）；没写 → 默认与档位同名，自动补上
            if level in values and not str(values[level] or "").strip():
                raise rout.HTTPException(
                    status_code=400,
                    detail="Off 以外的档位都需要填写 API 参数值，且参数值不能为空字符串。",
                )
            values[level] = str(values.get(level) or "").strip() or level
        data["reasoning_values"] = values


def _assert_revision(expected: int | None, current: int) -> None:
    """版本对不上 = 打开卡片期间别处改过。照 DSH 的口径：拒绝本次写入并让人重开卡片。"""
    if expected is None or expected == current:
        return
    raise rout.HTTPException(
        status_code=409,
        detail="这张卡片打开期间，这些设置已被其他地方改动。请关闭后重新打开，在当前值上编辑。",
    )


def _persist_provider(new_provider: Any, model_id: str | None = None) -> None:
    """把 v1 形状的改动并回整本 v2 配置并落盘（老接口唯一的写盘口）。

    老接口收到的是一份"老形状"的配置，直接写文件会把多档配置冲成两档；
    这里改成：读整本 → 只改它该改的那一档 → 原样写回。model_id 给了就记进
    last_models（哪个档上次选的哪个模型），重启后选择器还是它。
    """
    book = rout.provider_book()
    book = rout.apply_legacy(book, new_provider)
    if model_id:
        book = rout.replace(book, last_models={**book.last_models, book.active: model_id})
    rout.persist_book(book)


class CustomProviderIn(rout.BaseModel):
    """自定义 OpenAI 兼容供应商的补丁：所有字段 None = 这次不改、保留现状。"""

    base_url: str | None = None
    api_key: str | None = None
    models: list[str] | None = None
    selected_model: str | None = None


class RoleModelsIn(rout.BaseModel):
    """逐能力指定的模型绑定补丁（两档通用）。

    字段缺省 = 这次不改；显式 ``null`` / 空串 = 清除绑定、回到自动适配；
    非空字符串 = 指定该模型。web 层以 ``exclude_unset`` 下发，这样「只改批改」
    不会把看图的绑定顺手清掉。
    """

    vision: str | None = None
    grade: str | None = None


class ProviderIn(rout.BaseModel):
    """模型来源两档：default（千问，只需 Key）/ custom（自带地址、Key、模型清单）。"""

    source: str
    default_api_key: str | None = None
    custom: CustomProviderIn | None = None
    role_models: RoleModelsIn | None = None


class FetchModelsIn(rout.BaseModel):
    """一键拉取上游模型：用"待保存"的地址 / 密钥做只读探测，不改动当前运行配置。"""

    base_url: str
    # 可以为空：地址与已保存的一致时，后端沿用已保存的 Key（不必每次重抄一遍）。
    api_key: str = ""


class ConfigRequest(rout.BaseModel):
    # 钥匙：旧前端只发 key；新前端只切模型 / 档位时不带 key（None = 这次不改钥匙）。
    api_key: str | None = None
    base_url: str | None = None
    # 换主脑（agent 大脑）：必须是能力表里实测能当大脑的型号；None = 不换。
    model: str | None = None
    # 思考档 low/medium/high；None = 不换。最终按"换后型号"校验它是否支持。
    reasoning_effort: str | None = None
    # 最大上下文档位 standard/max；None = 不换。脏值由 normalize 静默回退标准档。
    context_mode: str | None = None
    # 说话风格 natural/witty/calm；None = 不换。脏值由 normalize 静默回退自然档。
    speaking_style: str | None = None
    # 模型来源（默认千问 / 自定义端点）；None = 这次不动来源。给了就合并、落盘并整体重建。
    provider: ProviderIn | None = None


def _roles_payload(
    provider: rout.ProviderConfig,
    settings: rout.Settings,
    fallbacks: tuple[str, ...] = (),
) -> dict:
    """三能力槽（对话 / 看图 / 批改）的绑定与生效结果：前端渲染槽位的唯一事实源。

    - ``chat``：绑定就是主脑选择本身——默认档是 ``Settings.model``、自定义档是
      ``selected_model``，不另存一份（两处存必然漂移），所以 bound == effective；
    - ``vision`` / ``grade``：``bound`` 是用户显式指定（None = 自动适配），
      ``effective`` 是解析后真正生效的模型——解析规则与 :func:`apply_provider`
      保持同一套（那里是运行时落地点，这里是界面回显），绿字「自动 → X」的 X
      就是 effective，不是前端猜的；
    - ``vision.available``：这台上到底有没有看图能力（纯文本自定义端点为 False）。
    """
    rm = provider.role_models
    if provider.source == rout.SOURCE_CUSTOM:
        models = provider.custom.models
        chat = provider.custom.selected_model or (models[0] if models else "")
        vision = (
            rm.vision
            if rm.vision and rm.vision in models
            else next((m for m in models if rout.capabilities_for(m).supports_vision), None)
        )
        grade = rm.grade if rm.grade and rm.grade in models else (chat or None)
    else:
        chat = settings.model
        vision = rm.vision or (settings.vision_model.strip() or None)
        grade = rm.grade or (settings.grade_model.strip() or None)
    return {
        "chat": {"bound": chat or None, "effective": chat or None},
        "vision": {"bound": rm.vision or None, "effective": vision, "available": bool(vision)},
        "grade": {"bound": rm.grade or None, "effective": grade},
    }


@router.get("/api/config")
async def get_config() -> dict:
    """当前钥匙与模型来源状态（不回传明文 key，只说配没配、用的哪个地址 / 模型）。"""
    assert state.settings is not None
    source = state.provider.source
    return {
        # provider 为旧字段（default 档保持 "dashscope" 不变，兼容旧前端）；
        # 新前端用语义更清楚的 source。
        "provider": "dashscope" if source == "default" else "custom",
        "source": source,
        # has_key = 当前生效的钥匙（自定义档就是自定义 Key）；default_has_key 单独回答
        # "切回默认千问时有没有 Key 可用"，别让自定义的 Key 冒充千问已配置。
        "has_key": bool(state.settings.api_key),
        "default_has_key": bool(
            state.provider.default_api_key.strip() or rout.load_settings().api_key.strip()
        ),
        "base_url": state.settings.base_url,
        "model": state.settings.model,
        "reasoning_effort": state.settings.reasoning_effort,
        "context_mode": state.settings.context_mode,
        "speaking_style": state.settings.speaking_style,
        # 当前装配是否真有看图能力（视觉子模型非空）：自定义纯文本端点为 False，
        # 前端据此禁用图片上传入口，和后端"不注册视觉工具"的软降级保持同一口径。
        # 能发图 = 主模型自己看得见 **或** 配了视觉子模型（2026-10-31 合并口径：
        # 以前只看视觉子模型，主模型本身能看图却被判"看不了"，输入框上方还常驻黄条）
        "supports_vision": bool(
            state.settings.vision_model.strip() or state.settings.main_model_can_see_image
        ),
        # 三能力槽的绑定与生效结果（设置页槽位卡与选择器的事实源）；
        # role_fallbacks 只在保存响应里携带（见 set_config），GET 恒空——
        # 黄条只在"配置变化后的那一次"出现，不常驻。
        "roles": _roles_payload(state.provider, state.settings),
        "role_fallbacks": [],
        # 自定义档详情：只回有没有 Key，不回明文；模型清单 / 选中供设置页回显。
        "custom": {
            "base_url": state.provider.custom.base_url,
            "has_key": bool(state.provider.custom.api_key),
            "models": list(state.provider.custom.models),
            "selected_model": state.provider.custom.selected_model,
            # 每个已存模型的能力（与 models 同序）：设置页回显分组、选择器显隐的事实源。
            "details": [rout.capabilities_payload(m) for m in state.provider.custom.models],
        },
    }


@router.post("/api/config")
async def set_config(req: ConfigRequest) -> dict:
    """更新运行配置：可只换钥匙、只换主脑 / 思考档，或一起换。

    任何一项改动都整体重建模型客户端与全部模型工具、重建 loop（保留当前会话账本）。
    主脑 ``model`` / ``reasoning_effort`` 只动 agent 大脑这一个模型；批 grade / 看图
    子模型经 ``provider.role_models`` 逐能力指定（两档通用，未指定走自动适配），
    随 provider 一起合并、修复、落盘。
    """
    assert state.settings is not None and state.history is not None
    # 任何会话在跑都不能换配置（闸的判据见 _assert_not_busy，三个供应商写接口共用）
    _assert_not_busy()

    # ① 模型来源（默认千问 / 自定义 OpenAI 兼容端点）：给了 provider 就逐字段合并、落盘。
    new_provider = state.provider
    role_fallbacks: tuple[str, ...] = ()
    if req.provider is not None:
        try:
            new_provider = rout.merge_provider(
                state.provider,
                source=req.provider.source,
                default_api_key=req.provider.default_api_key,
                custom=(
                    req.provider.custom.model_dump() if req.provider.custom is not None else None
                ),
                role_models=(
                    req.provider.role_models.model_dump(exclude_unset=True)
                    if req.provider.role_models is not None
                    else None
                ),
            )
        except ValueError as exc:
            raise rout.HTTPException(status_code=400, detail=str(exc)) from exc
        # 引用修复：清单变化导致绑定指向已删模型 → 置回自动，并把回退槽位带回响应
        # （前端黄条只报这一次，不常驻）。数据库里永远不存在指向已删模型的绑定。
        new_provider, role_fallbacks = rout.repair_role_models(new_provider)
        _persist_provider(new_provider)
    elif req.api_key is not None:
        # 旧前端兼容：裸 key 一律当作"默认档千问 key"；显式空串仍 400（保留旧契约）。
        api_key = req.api_key.strip()
        if not api_key:
            raise rout.HTTPException(status_code=400, detail="钥匙不能为空")
        try:
            new_provider = rout.merge_provider(
                state.provider, source="default", default_api_key=api_key
            )
        except ValueError as exc:
            raise rout.HTTPException(status_code=400, detail=str(exc)) from exc
        _persist_provider(new_provider)

    # 来源决定 base_url / api_key / 主脑的起点。
    # 切来源（设置页保存默认 / 自定义）时必须从干净的 .env 配置打底再叠加，否则上一档
    # 的污染会被带到新档——典型是自定义全纯文本档把看图模型置空、子模型压到第三方名、
    # 兜底链清空，切回默认后这些不会自动还原。只改主脑 / 思考档等偏好（不带 provider）
    # 时才以当前运行 settings 为基底，保留钥匙 / 来源 / 已选偏好。
    provider_switch = req.provider is not None
    base = rout.load_settings() if provider_switch else state.settings
    base_settings = rout.apply_provider(base, new_provider)
    changes: dict = {}

    # ② 主脑型号：内置能力表 或 当前自定义档已添加的模型清单，二者其一即可。
    custom_models = new_provider.custom.models if new_provider.source == rout.SOURCE_CUSTOM else ()
    target_model = base_settings.model
    if req.model is not None:
        model_id = req.model.strip()
        if new_provider.source == rout.SOURCE_CUSTOM:
            # 自定义档的上游只有用户自己的模型：内置型号（qwen / kimi …）在这里必然 404。
            # 以前放行并把内置名写进 selected_model，落盘后模型选择器里就"找不到当前模型"。
            if model_id not in custom_models:
                raise rout.HTTPException(
                    status_code=400,
                    detail=(
                        f"「{model_id}」不在自定义端点已添加的模型里；"
                        "先在设置 → 自定义里把它加进来再切换"
                    ),
                )
        elif not rout.is_supported_main_model(model_id):
            raise rout.HTTPException(status_code=400, detail=f"不支持的模型：{model_id}")
        target_model = model_id
        changes["model"] = model_id
        # 换主脑要落盘（重启后仍是它）：自定义档记进 v1 的 selected_model，
        # v2 里统一记进 last_models[当前档]。两条口径同一次写盘完成。
        if new_provider.source == rout.SOURCE_CUSTOM:
            new_provider = rout.replace(
                new_provider,
                custom=rout.replace(new_provider.custom, selected_model=model_id),
            )
        _persist_provider(new_provider, model_id)

    # ③ 思考档：给了就按"换后型号"校验（档位非法 / 该型号不支持思考，一律 400，
    #    绝不把模型不认的 reasoning.effort 发出去招来 400）。
    if req.reasoning_effort is not None:
        effort = req.reasoning_effort.strip().lower()
        levels = rout.reasoning_levels_for(target_model)
        if levels and effort not in levels:
            # 模型声明了档位、但给的值不在其中 → 400（绝不把厂商不认的档发出去）
            raise rout.HTTPException(
                status_code=400,
                detail=f"模型 {target_model} 不支持思考档：{req.reasoning_effort}",
            )
        # 模型没声明档位（绝大多数自定义模型）：档位照存，但请求里不会带 ——
        # 发不带由 responses 层按能力表拦（supports_reasoning）。以前这里直接 400，
        # 导致"看着能选、点了没反应"（2026-10-28 真机踩到）。
        # 档位全集（关 / 低 / 中 / 高 / 极高 / 最高）：模型声明了哪几档由 /api/models 决定，
        # 这里只负责"用户选了什么就存什么"，不再把极高/最高悄悄压回中档（2026-10-28 真机踩到）。
        changes["reasoning_effort"] = (
            effort if effort in rout.REASONING_LEVELS_ALL else rout.DEFAULT_REASONING_EFFORT
        )
        # 记进配置：重启后按"这一档上次选的档位"恢复（DSH 也按模型记住档位）
        _remember_effort(changes["reasoning_effort"])
    elif provider_switch:
        # 切来源没带思考档：沿用当前运行档（不在此处强制校验，自定义档不支持时由装配层兜底）
        changes["reasoning_effort"] = state.settings.reasoning_effort

    # ④ 最大上下文档位：给了就归一化（只认 standard/max，脏值 / 大小写静默回退标准档，
    #    与 .env 加载同一套容错）；档位只改环分母与压缩阈值，不碰钥匙 / 大脑 / 思考档。
    if req.context_mode is not None:
        changes["context_mode"] = rout.normalize_context_mode(req.context_mode)
    elif provider_switch:
        changes["context_mode"] = state.settings.context_mode

    # ⑤ 说话风格：给了就归一化（只认 natural/witty/calm，脏值静默回退自然档）。
    #    只换系统提示词 tone 段的口吻，随下面 runtime 整体重建、从下一轮生效；不碰钥匙 / 大脑。
    if req.speaking_style is not None:
        changes["speaking_style"] = rout.normalize_speaking_style(req.speaking_style)
    elif provider_switch:
        changes["speaking_style"] = state.settings.speaking_style

    new_settings = rout.replace(base_settings, **changes)
    # 整体重建（共享部分 + 池里每个会话的循环），并把"当前生效的供应商配置"换成这次保存后
    # 的形状。这是换配置的唯一落地路径，/api/providers 的三个写接口也走它。
    rout._apply_rebuild(new_settings, new_provider)
    return {
        "ok": True,
        "provider": "dashscope" if new_provider.source == "default" else "custom",
        "source": new_provider.source,
        "has_key": bool(new_settings.api_key),
        "base_url": new_settings.base_url,
        "model": new_settings.model,
        "reasoning_effort": new_settings.reasoning_effort,
        "context_mode": new_settings.context_mode,
        "speaking_style": new_settings.speaking_style,
        "supports_vision": bool(
            new_settings.vision_model.strip() or new_settings.main_model_can_see_image
        ),
        # 三能力槽绑定 / 生效结果 + 本次发生回退的槽位（驱动黄条，只报一次）
        "roles": _roles_payload(new_provider, new_settings),
        "role_fallbacks": list(role_fallbacks),
        "custom": {
            "base_url": new_provider.custom.base_url,
            "has_key": bool(new_provider.custom.api_key),
            "models": list(new_provider.custom.models),
            "selected_model": new_provider.custom.selected_model,
            "details": [rout.capabilities_payload(m) for m in new_provider.custom.models],
        },
    }


# ------------------------------------------------ v2 多档供应商（2026-10-27 口径）
#
# 1. 几档供应商并存，但**只能启用一档**；选供应商只在设置页做。
# 2. 主界面的模型入口只列「当前启用那一档」的模型（见 list_models），不显示供应商名。
# 3. 没配好的档（缺地址 / 缺密钥 / 空模型）既不显示也不能启用——判定只有一个地方：
#    glyph.provider.book.validate_entry。这里所有接口都只用它的结论，不另写一套判断。
# 4. 写成功后立刻生效：换算成运行时配置并整体重建（save_and_apply_book），不必重启进程。

_PROVIDER_SLUG_MAX = 40
"""档位 id 的最大长度（从名字派生时截断，避免名字很长时 id 也跟着很长）。"""


class ProviderEntryIn(rout.BaseModel):
    """新增 / 更新一档供应商（字段与 GET /api/providers 的每一档对齐）。

    - id 缺省 = 由 name 派生（派生出同名 id，重复保存就是更新那一档）；
    - api_key 缺省 / 空 = 这次不改，沿用已存的那把（改个名字不必重新抄密钥）；
    - models 缺省 / 空 = 这次不改，沿用已存的清单（语义同上）。
    """

    id: str | None = None
    name: str = ""
    # 以下字段缺省 = 沿用这一档已存的值（新建时取括号里的默认）：
    kind: str | None = None  # custom
    api: str | None = None  # openai-chat
    base_url: str = ""
    api_key: str | None = None
    models: list[Any] = []
    roles: dict[str, str] | None = None
    #: 打开卡片时的整本配置版本号；对不上 → 409（期间被别处改过）
    revision: int | None = None


def _slugify(name: str) -> str:
    """由名字派生档位 id：小写、只留字母数字与汉字、其余压成一个连字符。

    英文名 DeepSeek Cloud → deepseek-cloud；中文名原样保留汉字（id 只当 JSON 键与界面
    标识用，不进文件路径，不必强行转拼音）；全空则退回 provider。
    """
    out: list[str] = []
    for ch in name.strip().lower():
        if ch.isalnum():
            out.append(ch)
        elif out and out[-1] != "-":
            out.append("-")
    slug = "".join(out).strip("-")[:_PROVIDER_SLUG_MAX].strip("-")
    return slug or "provider"


def _assert_not_busy() -> None:
    """正在回答中不许换供应商：重建会把在跑那一轮手里的旧循环换掉。

    换掉之后用户的「停下」按钮就成了废按钮（取消信号落在一个没人跑的新循环上）。
    与 POST /api/config 同一道闸，三个写接口共用。
    """
    if any(s.in_use() for s in state.sessions.values()) or (
        state.loop is not None and state.loop.busy
    ):
        raise rout.HTTPException(status_code=409, detail="正在回答中，请先点「停下」再修改设置")


def _entry_models_payload(entry: rout.ProviderEntry) -> list[dict]:
    """这一档的模型目录。

    内置档的模型来自实测能力表（MAIN_MODELS，现填、不落库——它随版本更新）；
    自定义档就是用户自己填的那几行（顺序即用户顺序）。
    """
    if entry.kind == rout.KIND_BUILTIN:
        return [
            {
                "id": m.id,
                "label": m.label,
                "context_window": m.context_window,
                # 内置表没登记单次输出上限：不知道就如实给 0，不编一个数字
                "max_output": 0,
                "inputs": ["text"]
                + (["image"] if rout.capabilities_for(m.id).supports_vision else []),
                "reasoning_levels": list(m.reasoning_levels),
            }
            for m in rout.MAIN_MODELS
        ]
    return [m.to_dict() for m in entry.models]


def _provider_payload(entry: rout.ProviderEntry, book: rout.ProviderBook) -> dict:
    """一档供应商的对外形状。**不回明密钥**，只说配没配（has_key）。"""
    builtin = entry.kind == rout.KIND_BUILTIN
    if builtin:
        # 内置档的钥匙可以留空走 .env / 环境变量，所以"有没有钥匙"要看最终生效的那把
        has_key = bool(entry.api_key.strip() or rout.load_settings().api_key.strip())
        base_url = entry.base_url or rout.DEFAULT_BASE_URL
    else:
        has_key = bool(entry.api_key.strip())
        base_url = entry.base_url
    return {
        "id": entry.id,
        "name": entry.shown,
        "kind": entry.kind,
        "api": entry.api,
        "base_url": base_url,
        # 凭据引用名（配置里只存名字，值在 ~/.markr/credentials.json）
        "api_key_env": entry.api_key_env,
        # 密钥从哪来：store=本地凭据文件 / env=环境变量 / dotenv=.env / missing=没有
        "key_source": (
            rout.credential_store().describe(entry.api_key_env).source
            if entry.api_key_env
            else ("missing" if not has_key else "env")
        ),
        "has_key": has_key,
        "models": _entry_models_payload(entry),
        "roles": dict(entry.roles),
        "is_active": entry.id == rout.active_provider_id(book),
    }


def _providers_payload(book: rout.ProviderBook) -> dict:
    """GET /api/providers 的响应体：只列配好的档（validate_entry 是唯一判定处）。"""
    return {
        "active": rout.active_provider_id(book),
        # 版本号：前端保存时回传，对不上就是"期间别处改过"
        "revision": book.revision,
        # 落盘文件不可写（只读部署）时前端要禁用所有写按钮
        "read_only": not rout.provider_file_writable(),
        "providers": [_provider_payload(e, book) for e in book.valid()],
    }


def _upsert_provider(book: rout.ProviderBook, entry: rout.ProviderEntry) -> rout.ProviderBook:
    """把一档放回整本配置：同 id 覆盖原位（顺序不乱），新档追加到末尾。"""
    entries: list[rout.ProviderEntry] = []
    found = False
    for existing in book.providers:
        if existing.id == entry.id:
            entries.append(entry)
            found = True
        else:
            entries.append(existing)
    if not found:
        entries.append(entry)
    return rout.replace(book, providers=tuple(entries))


@router.get("/api/providers")
async def get_providers() -> dict:
    """所有「配好了的」供应商档 + 当前启用的是哪一档。

    没配好的档（缺地址 / 缺密钥 / 空模型）不返回——它们既不显示也不能启用，前端
    不必再自己判一遍"这条算不算存在"。当前启用由 active 给出（同一时刻只有一个 true）。
    """
    return _providers_payload(rout.provider_book())


@router.post("/api/providers")
async def set_provider(req: ProviderEntryIn) -> dict:
    """新增或更新一档供应商。配不好（validate_entry 不通过）就 400 + 中文原因，且不落库。

    两处"聪明"的地方，都是为了少让用户重抄一遍：
    - 请求没带 id / id 不存在时先按名字派生 id，同名再次保存 = 更新那一档；
    - 没带 api_key / models 时沿用这一档已存的值（改名字不该把密钥清空）。
    """
    _assert_not_busy()
    _assert_revision(req.revision, rout.provider_book().revision)
    if (req.api or "").strip().lower() == "anthropic-messages":
        raise rout.HTTPException(
            status_code=400,
            detail=(
                "暂不支持 Anthropic Messages 协议："
                "这一版只有 OpenAI Chat / OpenAI Responses 两条通道"
            ),
        )
    # 明文钥匙绝不进配置文件：写进凭据库，配置里只留引用名（DSH 的 apiKeyEnv）
    incoming_key = (req.api_key or "").strip()
    req = req.model_copy(update={"api_key": None}) if incoming_key else req
    book = rout.provider_book()
    requested_id = (req.id or "").strip()
    existing = book.find(requested_id or _slugify(req.name))
    provider_id = requested_id or (existing.id if existing is not None else _slugify(req.name))
    name = req.name.strip() or (existing.shown if existing is not None else provider_id)
    # 有 ref 就沿用；没有就派生一个（<ID>_API_KEY），真值写进凭据库
    api_key_env = (existing.api_key_env if existing is not None else "") or (
        rout.default_credential_ref(provider_id)
    )
    if incoming_key:
        try:
            rout.credential_store().set(api_key_env, incoming_key)
        except ValueError as exc:
            raise rout.HTTPException(status_code=400, detail=str(exc)) from exc
    resolved_key = rout.credential_store().resolve(api_key_env) or ""
    base_url = req.base_url.strip() or (existing.base_url if existing is not None else "")
    kind = req.kind or (existing.kind if existing is not None else rout.KIND_CUSTOM)
    api = req.api or (existing.api if existing is not None else "openai-chat")
    if req.models:
        models: list[Any] = list(req.models)
    elif existing is not None:
        models = [m.to_dict() for m in existing.models]
    else:
        models = []
    roles = req.roles if req.roles is not None else (existing.roles if existing is not None else {})

    # 能力与思考档的校验（2026-10-29，照 DSH：保存时就拦，不等上游 400）
    _validate_model_fields(models)

    entry = rout.entry_from_dict(
        {
            "id": provider_id,
            "name": name,
            "kind": kind,
            "api": api,
            "base_url": base_url,
            "api_key_env": api_key_env,
            "api_key": resolved_key,
            "models": models,
            "roles": dict(roles),
        }
    )
    if entry is None:
        raise rout.HTTPException(status_code=400, detail="缺少供应商标识")
    # 内置档由内置定义维护（地址写死、模型来自能力表）：不许新建第二个内置档，
    # 也不许把手里的内置档改成自定义档（要换家就新建一档，内置那档留着）。
    if entry.kind == rout.KIND_BUILTIN and (existing is None or existing.kind != rout.KIND_BUILTIN):
        raise rout.HTTPException(
            status_code=400,
            detail="内置千问那一档由系统维护，不能新建；要接自己的服务请选自定义",
        )
    if (
        existing is not None
        and existing.kind == rout.KIND_BUILTIN
        and entry.kind != rout.KIND_BUILTIN
    ):
        raise rout.HTTPException(
            status_code=400,
            detail="内置千问那一档不能改成自定义档，请新建一档填自己的服务",
        )
    try:
        rout.validate_entry(entry)
    except ValueError as exc:
        # 不落库、不重建：没配好的档连"存在"都不算
        raise rout.HTTPException(status_code=400, detail=str(exc)) from exc

    updated = _upsert_provider(book, entry)
    # 第一档（用户手里还没有别的自定义档时）自动成为当前启用：刚配好一档就想用它，
    # 不该还要用户再点一次「启用」。
    if not updated.active or updated.current() is None:
        updated = rout.replace(updated, active=entry.id)
    elif entry.kind != rout.KIND_BUILTIN and not any(
        p.kind != rout.KIND_BUILTIN for p in book.valid()
    ):
        updated = rout.replace(updated, active=entry.id)
    settled = rout.save_and_apply_book(updated)
    return {"ok": True, "provider_id": entry.id, **_providers_payload(settled)}


@router.delete("/api/providers/{provider_id}")
async def delete_provider(provider_id: str, revision: int | None = None) -> dict:
    """删掉一档。内置千问那一档不许删；删掉的正好是当前启用档时，落到第一个可用档。"""
    _assert_revision(revision, rout.provider_book().revision)
    _assert_not_busy()
    book = rout.provider_book()
    entry = book.find(provider_id)
    if entry is None:
        raise rout.HTTPException(status_code=404, detail=f"没有这一档：{provider_id}")
    if entry.kind == rout.KIND_BUILTIN:
        raise rout.HTTPException(status_code=400, detail="内置千问那一档不能删除")
    # 删档连带删掉它自己的凭据（DSH 的删除确认里也写明"会移除存储的 API 密钥"）
    if entry.api_key_env:
        try:
            rout.credential_store().unset(entry.api_key_env)
        except OSError:
            pass
    updated = rout.replace(
        book,
        providers=tuple(p for p in book.providers if p.id != provider_id),
        # 顺手清掉"这一档上次选的模型"，别给已删的档留一条悬空记录
        last_models={pid: mid for pid, mid in book.last_models.items() if pid != provider_id},
    )
    settled = rout.save_and_apply_book(updated)
    return {"ok": True, **_providers_payload(settled)}


@router.post("/api/providers/{provider_id}/activate")
async def activate_provider(provider_id: str, revision: int | None = None) -> dict:
    """切换当前启用的一档。**必须先配好**：没配好的档不能启用（400 + 中文原因）。"""
    _assert_revision(revision, rout.provider_book().revision)
    _assert_not_busy()
    book = rout.provider_book()
    entry = book.find(provider_id)
    if entry is None:
        raise rout.HTTPException(status_code=404, detail=f"没有这一档：{provider_id}")
    try:
        rout.validate_entry(entry)
    except ValueError as exc:
        raise rout.HTTPException(
            status_code=400, detail=f"这一档还没配好，不能启用：{exc}"
        ) from exc
    settled = rout.save_and_apply_book(rout.replace(book, active=provider_id))
    return {"ok": True, **_providers_payload(settled)}


def _entry_brain_item(model: rout.ModelEntry) -> dict:
    """把模型目录里的一行映射成模型入口项（与内置档同一张皮）。

    **不再按"能不能当大脑"过滤**（2026-10-27 口径）：清单里有什么就是什么——用户只填
    一个看图模型，它照样是可选的主模型，选错了是他自己的判断，产品不替他挡。

    两个信息源有分工：用户自己填的（显示名 / 上下文窗口 / 输入类型）优先；没填的才落到
    推断层（capabilities_for 按型号名推断品牌与思考档）。看图位取"填了 image" **或**
    推断层认为能看图——v1 迁移过来的条目只带 id、输入类型默认纯文本，不合并会把
    gpt-4o 这类真能看图的标成看不见。自定义端点不臆造联网能力，web_search 恒 False。
    """
    cap = rout.capabilities_payload(model.id)
    vision = rout.model_can_see_image(model) or bool(cap["supports_vision"])
    # 档位以用户在设置页勾的为准（2026-10-27 起可勾到极高/最高）；没勾才用推断层的三档
    levels = list(model.reasoning_levels) or list(cap["reasoning_levels"])
    has_reason = bool(levels)
    if vision and has_reason:
        desc = "能看图、可拍照认字 · 支持思考强度"
    elif vision:
        desc = "能看图、可拍照认字"
    elif has_reason:
        desc = "支持思考强度调节"
    else:
        desc = ""
    return {
        "id": model.id,
        "label": model.label or model.id,
        "desc": desc,
        "reasoning_levels": levels,
        "web_search": False,
        "context_window": int(model.context_window or cap["context_window"]),
        "supports_vision": vision,
        "brand": cap["brand"],
    }


def _custom_brain_item(model_id: str) -> dict:
    """兼容包装：只给一个型号 id 时按"只有 id 的一行目录"处理（不过滤）。"""
    return _entry_brain_item(rout.ModelEntry(id=model_id))


def _builtin_brain_item(model: rout.MainModel) -> dict:
    """把一个内置实测型号映射成大脑菜单项（与自定义档同一张皮）。

    字段集合必须与 :func:`_custom_brain_item` 一致（有守线测试比对两档的键）：
    品牌与看图能力同样取自 capabilities_for 这一唯一事实源，不另写一套判定，
    前端也就不必按型号 id 再猜一遍品牌。内置主脑是文本模型，看图由独立 vision
    子模型负责，这里如实标 False（不是"没查"，是 capabilities_for 的刻意结论）。
    """
    cap = rout.capabilities_for(model.id)
    return {
        "id": model.id,
        "label": model.label,
        "desc": model.desc,
        "reasoning_levels": list(model.reasoning_levels),
        "web_search": model.web_search,
        "context_window": model.context_window,
        "supports_vision": cap.supports_vision,
        "brand": cap.brand,
    }


@router.get("/api/models")
async def list_models() -> dict:
    """模型入口的清单：前端据此渲染主界面的模型选择，型号不写死在前端。

    口径（2026-10-27 定稿）：
    - 只列**当前启用那一档**的模型，内置模型与自定义模型绝不混排（两档随时可在设置里切）；
    - **不按"能不能当大脑"过滤**：用户填了什么就列什么，只填一个看图模型也照样可选；
    - 不显示供应商名（入口只说模型）。
    下发的字段两档完全一致（有守线测试比对）：思考档、是否联网、上下文窗口（用量环分母）、
    品牌 logo、是否支持看图。批改 / 看图等子模型的身份经设置页逐能力绑定，不在此列。
    """
    book = rout.provider_book()
    entry = book.current()
    if entry is None or entry.kind == rout.KIND_BUILTIN:
        return {
            "default": rout.DEFAULT_MAIN_MODEL_ID,
            # 「更大上下文」档位只服务内置档（自定义档的窗口由用户自己在模型目录里填）
            "context_mode_applies": True,
            "models": [_builtin_brain_item(m) for m in rout.MAIN_MODELS],
        }
    items = [_entry_brain_item(m) for m in entry.models]
    last = book.last_models.get(entry.id, "")
    present = any(i["id"] == last for i in items)
    default = last if present else (items[0]["id"] if items else "")
    return {
        "default": default,
        "models": items,
        # 上次选的模型已经不在清单里（下架 / 被用户删了）：前端要提示一句并跟着切过去，
        # 不能让它静默换掉（DSH 的 routable 判法，提示语是我们补的）。
        "selected_missing": bool(last) and not present and bool(items),
        "replaced_from": last if last and not present else "",
        # 自定义档没有「更大上下文」这个档位：窗口由用户在模型目录里填
        "context_mode_applies": False,
    }


@router.post("/api/provider/fetch-models")
async def fetch_models(req: FetchModelsIn) -> dict:
    """一键拉取自定义端点的上游模型清单（同时验证地址 / 密钥通不通）。

    只读探测：不保存、不重建运行配置；前端拿到清单后让用户确认，再走 POST /api/config
    整体保存。连不上 / 密钥错 / 地址不对 / 返回格式怪，统一转 400 + 人话提示。
    """
    base_url = req.base_url.strip()
    api_key = req.api_key.strip()
    if base_url and not api_key:
        # 没带 Key 时按地址借一把**地址一致**的已存 Key，绝不把 A 家的 Key 发给 B 家：
        # 1) 先看多档配置里有没有同地址的档（内置千问 / 自定义档都算）；
        # 2) 再看老的两档配置（state.provider.custom / 默认千问的 .env Key）。
        want = base_url.rstrip("/")
        for entry in rout.provider_book().providers:
            entry_url = (entry.base_url or "").rstrip("/")
            if entry.api_key and entry_url and entry_url == want:
                api_key = entry.api_key
                break
        if not api_key:
            saved = state.provider.custom
            if saved.api_key and saved.base_url.strip().rstrip("/") == want:
                api_key = saved.api_key
        if not api_key and want == rout.DEFAULT_BASE_URL.rstrip("/"):
            # 内置千问：Key 可能只在 .env 里，界面没保存过任何东西也让它能拉清单。
            api_key = rout.load_settings().api_key.strip()
    if not base_url or not api_key:
        raise rout.HTTPException(
            status_code=400,
            detail="Base URL 和 API Key 都要填：先在这张卡片里填入密钥（或先保存一次），再点获取"
        )
    try:
        models = await rout.fetch_upstream_models(base_url, api_key)
    except rout.ProviderDiscoverError as exc:
        raise rout.HTTPException(status_code=400, detail=exc.message) from exc
    if not models:
        raise rout.HTTPException(
            status_code=400,
            detail="连上了，但没有读到任何模型，换个地址或密钥再试",
        )
    return {
        "models": list(models),
        "count": len(models),
        # 每个模型的能力（与 models 同序）：设置页据此分"能看图/其他对话"、隐藏非对话，
        # 也是 #67 自定义模型进选择器后显隐思考强度 / 上下文 / 看图入口的唯一事实源。
        "details": [rout.capabilities_payload(m) for m in models],
    }


@router.get("/api/tools")
async def list_tools() -> dict:
    assert state.registry is not None and state.settings is not None  # lifespan 启动时已装配
    settings = state.settings
    return {
        "tools": state.registry.schemas(),
        # 真实模型名下发（web-ui-spec G5）：前端 model-tag / OCR 台 meta 不再写死
        "models": {
            "chat": settings.model,
            "grade": settings.grade_model,
            "vision": settings.vision_model,
        },
    }


__all__ = [
    "CustomProviderIn",
    "RoleModelsIn",
    "ProviderIn",
    "ProviderEntryIn",
    "FetchModelsIn",
    "ConfigRequest",
    "_roles_payload",
    "get_config",
    "set_config",
    "get_providers",
    "set_provider",
    "delete_provider",
    "activate_provider",
    "_entry_brain_item",
    "_custom_brain_item",
    "_builtin_brain_item",
    "list_models",
    "fetch_models",
    "list_tools",
]
