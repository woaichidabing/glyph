"""模型供应商 v2：多档并存 + 当前启用 + 每档一份模型目录与角色分工。

与 v1（glyph.provider.config 的 ProviderConfig）的关系：

- v1 是「默认档 / 自定义档二选一」，一次只能存一家；
- v2 是一张表：几档并存，只有一档是当前启用；每档自带模型目录（带上下文窗口等
  元数据）与角色分工（批改 / 看图 / 润色 / 压缩 / 兜底）。
- v1 的结构没有删：to_legacy() 把当前启用那一档换算成 v1 形状，好让运行时装配
  （glyph.provider.apply）先原样跑着，等接口层切过来再收口。

两条硬规则（产品口径，2026-10-27 定）：

1. 没配好就不算存在：缺地址 / 缺密钥 / 模型清单空 的档不落库、不列出、不参与启用
   （validate_entry 是唯一判定处）。
2. 主模型由用户自己选：这里不规定“谁是主模型”，也不筛“能不能当主脑”。
   清单里有什么就是什么，只填一个看图模型也算数。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace

__all__ = [
    "BOOK_VERSION",
    "KIND_BUILTIN",
    "KIND_CUSTOM",
    "API_PROTOCOLS",
    "ROLE_IDS",
    "ModelEntry",
    "ProviderEntry",
    "ProviderBook",
    "normalize_inputs",
    "normalize_models",
    "normalize_reasoning",
    "normalize_reasoning_values",
    "model_can_see_image",
    "validate_entry",
    "book_from_dict",
    "book_to_dict",
    "entry_from_dict",
    "entry_to_dict",
    "active_entry",
    "active_provider_id",
    "first_valid",
    "to_legacy",
    "from_legacy_any",
    "book_to_disk_dict",
]

BOOK_VERSION = 2

KIND_BUILTIN = "builtin"
KIND_CUSTOM = "custom"
KINDS = (KIND_BUILTIN, KIND_CUSTOM)

#: 三种协议；手写档必须显式给一个，内置档由内置定义决定
API_PROTOCOLS = ("openai-chat", "openai-responses", "anthropic-messages")

#: 角色绑定的全集（顺序即界面顺序）。空串 = 同主模型（看图是“自动”、兜底是“不启用”）
ROLE_IDS = ("grade", "vision", "polish", "compact", "fallback")

INPUT_KINDS = ("text", "image")


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _clean_int(value: object) -> int:
    """正整数才收；字符串数字也认（输入框回来的是字符串）；其余归 0（=不知道）。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, float):
        return int(value) if value > 0 else 0
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("_", "")
        return int(text) if text.isdigit() and int(text) > 0 else 0
    return 0


def normalize_inputs(items: object) -> tuple[str, ...]:
    """输入类型：只收 text / image，去重保序；空或非法一律当纯文本。"""
    if isinstance(items, str):
        items = (items,)
    if not isinstance(items, Iterable):
        return ("text",)
    out: list[str] = []
    for item in items:
        kind = _clean(item).lower()
        if kind in INPUT_KINDS and kind not in out:
            out.append(kind)
    return tuple(out) if out else ("text",)


@dataclass(frozen=True)
class ModelEntry:
    """模型目录里的一行：id 是唯一键，其余都是“知道就写、不知道留空”。"""

    id: str
    label: str = ""
    context_window: int = 0
    max_output: int = 0
    inputs: tuple[str, ...] = ("text",)
    #: 这个模型支持的思考档（low/medium/high/xhigh/max 的子集）；空 = 不支持思考
    reasoning_levels: tuple[str, ...] = ()
    #: 档位 → 发给厂商的参数值；没填就用档位名本身（照 DSH 那张表多出来的一列）
    reasoning_values: dict[str, str] = field(default_factory=dict)

    @property
    def shown(self) -> str:
        """界面上显示的名字：没填就用 id。"""
        return self.label or self.id

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "context_window": self.context_window,
            "max_output": self.max_output,
            "inputs": list(self.inputs),
            "reasoning_levels": list(self.reasoning_levels),
            "reasoning_values": dict(self.reasoning_values),
        }


def normalize_reasoning(items: object) -> tuple[str, ...]:
    """档位：只收词表里的值（low/medium/high/xhigh/max），去重并按词表顺序排。"""
    from ..config import REASONING_LEVELS_ALL

    if isinstance(items, str):
        items = (items,)
    if not isinstance(items, Iterable):
        return ()
    wanted = {_clean(item).lower() for item in items}
    return tuple(level for level in REASONING_LEVELS_ALL if level in wanted)


def normalize_reasoning_values(raw: object) -> dict[str, str]:
    """档位 → 参数值：键只收词表里的档位，值去空白；空值不存（等于“同档位名”）。"""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        level = _clean(key).lower()
        text = _clean(value)
        if level in ROLE_IDS:  # 占位，见下方真实校验
            continue
        if level and text:
            out[level] = text
    from ..config import REASONING_LEVELS_ALL

    return {k: v for k, v in out.items() if k in REASONING_LEVELS_ALL}


def model_can_see_image(model: ModelEntry) -> bool:
    return "image" in model.inputs


def normalize_models(items: object) -> tuple[ModelEntry, ...]:
    """模型清单：只收有 id 的条目，按 id 去重保序；label 缺省回填 id。"""
    if not isinstance(items, Iterable) or isinstance(items, (str, bytes)):
        return ()
    out: list[ModelEntry] = []
    seen: set[str] = set()
    for raw in items:
        entry = raw if isinstance(raw, ModelEntry) else _model_from_any(raw)
        if entry is None or not entry.id or entry.id in seen:
            continue
        seen.add(entry.id)
        out.append(replace(entry, label=entry.label or entry.id))
    return tuple(out)


def _model_from_any(raw: object) -> ModelEntry | None:
    """一条模型：可以是字符串（只给 id），也可以是字典。"""
    if isinstance(raw, str):
        mid = _clean(raw)
        return ModelEntry(id=mid) if mid else None
    if not isinstance(raw, dict):
        return None
    mid = _clean(raw.get("id"))
    if not mid:
        return None
    return ModelEntry(
        id=mid,
        label=_clean(raw.get("label")) or _clean(raw.get("name")),
        context_window=_clean_int(raw.get("context_window")),
        max_output=_clean_int(raw.get("max_output") or raw.get("max_tokens")),
        inputs=normalize_inputs(raw.get("inputs")),
        reasoning_levels=normalize_reasoning(raw.get("reasoning_levels")),
        reasoning_values=normalize_reasoning_values(raw.get("reasoning_values")),
    )


@dataclass(frozen=True)
class ProviderEntry:
    """一档供应商：地址 / 密钥 / 模型目录 / 角色分工。"""

    id: str
    name: str = ""
    kind: str = KIND_CUSTOM
    api: str = "openai-chat"
    base_url: str = ""
    #: 凭据引用名（DSH 的 apiKeyEnv）：配置里只放名字，值在 ~/.markr/credentials.json
    api_key_env: str = ""
    #: 兼容旧数据：迁移前的明文钥匙（迁移后清空）
    api_key: str = ""
    models: tuple[ModelEntry, ...] = ()
    roles: dict[str, str] = field(default_factory=dict)

    @property
    def shown(self) -> str:
        return self.name or self.id

    def model(self, model_id: str) -> ModelEntry | None:
        return next((m for m in self.models if m.id == model_id), None)

    def role_model(self, role: str) -> ModelEntry | None:
        """角色绑定的模型；没绑或绑的名不在清单里 → None（调用方按“同主模型”处理）。"""
        return self.model(self.roles.get(role, ""))

    def to_dict(self) -> dict:
        return entry_to_dict(self)


def _clean_roles(raw: object, models: tuple[ModelEntry, ...]) -> dict[str, str]:
    """角色绑定：只收已知角色；指向清单外的名字一律丢弃（不留死名字）。"""
    if not isinstance(raw, dict):
        return {}
    known = {m.id for m in models}
    out: dict[str, str] = {}
    for role in ROLE_IDS:
        model_id = _clean(raw.get(role))
        if model_id and model_id in known:
            out[role] = model_id
    return out


def entry_from_dict(data: object) -> ProviderEntry | None:
    """从任意 JSON 数据容错解析一档；没有 id 返回 None。"""
    if not isinstance(data, dict):
        return None
    pid = _clean(data.get("id"))
    if not pid:
        return None
    kind = _clean(data.get("kind")) or KIND_CUSTOM
    if kind not in KINDS:
        kind = KIND_CUSTOM
    api = _clean(data.get("api")) or "openai-chat"
    if api not in API_PROTOCOLS:
        api = "openai-chat"
    models = normalize_models(data.get("models"))
    return ProviderEntry(
        id=pid,
        name=_clean(data.get("name")) or pid,
        kind=kind,
        api=api,
        base_url=_clean(data.get("base_url")).rstrip("/"),
        api_key_env=_clean(data.get("api_key_env")),
        api_key=_clean(data.get("api_key")),
        models=models,
        roles=_clean_roles(data.get("roles"), models),
    )


def entry_to_dict(entry: ProviderEntry) -> dict:
    return {
        "id": entry.id,
        "name": entry.shown,
        "kind": entry.kind,
        "api": entry.api,
        "base_url": entry.base_url,
        "api_key_env": entry.api_key_env,
        # 明文钥匙永不落盘（运行时那份只在内存里；文件里只留引用名）
        "api_key": "",
        "models": [m.to_dict() for m in entry.models],
        "roles": dict(entry.roles),
    }


def validate_entry(entry: ProviderEntry) -> None:
    """这一档能不能算“配置成功”。不成功抛 ValueError（web 层转 400，不落库）。

    判定只有这一处：缺地址 / 缺密钥（自定义档）/ 没有模型 → 都不行。
    内置档的密钥允许留空（走 .env / 环境变量）。
    """
    if not entry.id:
        raise ValueError("缺少供应商标识")
    if entry.kind == KIND_BUILTIN:
        # 内置档永远可用：地址写死、模型来自内置目录、密钥允许走 .env
        return
    if not entry.base_url:
        raise ValueError("还没填 API 地址")
    if not entry.api_key:
        raise ValueError("还没填 API 密钥")
    if not entry.models:
        raise ValueError("模型清单一格都没填")
    ids = [m.id for m in entry.models]
    if len(set(ids)) != len(ids):
        raise ValueError("模型 ID 有重复")


@dataclass(frozen=True)
class ProviderBook:
    """整本配置：当前启用哪一档 + 所有档 + 每档上次选过的模型。"""

    version: int = BOOK_VERSION
    #: 每次落盘 +1；前端保存时带上打开卡片时的值，不一致就 409（设置页"被别处改动"提示）
    revision: int = 0
    active: str = ""
    providers: tuple[ProviderEntry, ...] = ()
    last_models: dict[str, str] = field(default_factory=dict)
    #: 每档上次选的思考档（重启后要能恢复；DSH 也是按模型记住档位）
    last_efforts: dict[str, str] = field(default_factory=dict)

    def find(self, provider_id: str) -> ProviderEntry | None:
        return next((p for p in self.providers if p.id == provider_id), None)

    def valid(self) -> tuple[ProviderEntry, ...]:
        """能用的档（= 过得了 validate_entry），界面只该看到这些。"""
        out: list[ProviderEntry] = []
        for entry in self.providers:
            try:
                validate_entry(entry)
            except ValueError:
                continue
            out.append(entry)
        return tuple(out)

    def current(self) -> ProviderEntry | None:
        entry = self.find(self.active)
        if entry is None:
            return None
        try:
            validate_entry(entry)
        except ValueError:
            return None
        return entry

    def to_dict(self) -> dict:
        return book_to_dict(self)


def book_from_dict(data: object, *, settle: bool = True) -> ProviderBook:
    """容错解析整本配置；认不出的东西一律丢掉，绝不让配置成为异常源。

    兼容 v1（{"source": ..., "custom": {...}}）：迁移规则见 from_legacy_any。
    """
    if not isinstance(data, dict):
        return ProviderBook()
    if _clean_int(data.get("version")) >= 2 or "providers" in data:
        raw_providers = data.get("providers")
        entries: list[ProviderEntry] = []
        if isinstance(raw_providers, dict):
            for pid, raw in raw_providers.items():
                payload = dict(raw) if isinstance(raw, dict) else {}
                payload.setdefault("id", pid)
                entry = entry_from_dict(payload)
                if entry is not None:
                    entries.append(entry)
        elif isinstance(raw_providers, list):
            for raw in raw_providers:
                entry = entry_from_dict(raw)
                if entry is not None:
                    entries.append(entry)
        efforts: dict[str, str] = {}
        raw_efforts = data.get("last_efforts")
        if isinstance(raw_efforts, dict):
            for pid, level in raw_efforts.items():
                key = _clean(pid)
                value = _clean(level)
                if key and value:
                    efforts[key] = value
        last: dict[str, str] = {}
        raw_last = data.get("last_models")
        if isinstance(raw_last, dict):
            for pid, mid in raw_last.items():
                key = _clean(pid)
                value = _clean(mid)
                if key and value:
                    last[key] = value
        parsed = ProviderBook(
            revision=_clean_int(data.get("revision")),
            active=_clean(data.get("active")),
            providers=tuple(entries),
            last_models=last,
            last_efforts=efforts,
        )
        # settle=False：读盘时不落定（文件里没有明文钥匙，这一刻判不准哪档能用），
        # 由调用方补水之后再 settle_active。
        return _settle_active(parsed) if settle else parsed
    return from_legacy_any(data)


def settle_active(book: ProviderBook) -> ProviderBook:
    """对外版：把"当前启用"落定到第一个能用的档上（凭据补回之后要重新做一次）。"""
    return _settle_active(book)


def _settle_active(book: ProviderBook) -> ProviderBook:
    """当前档不合法（或没指定）时，落到第一个能用的档上。"""
    if book.current() is not None:
        return book
    fallback = book.valid()
    return replace(book, active=fallback[0].id if fallback else "")


def book_to_dict(book: ProviderBook) -> dict:
    return {
        "version": BOOK_VERSION,
        "revision": book.revision,
        "active": book.active,
        "providers": [entry_to_dict(p) for p in book.providers],
        "last_models": dict(book.last_models),
        "last_efforts": dict(book.last_efforts),
    }


def bumped(book: ProviderBook) -> ProviderBook:
    """落盘前统一加一版（任何写操作都算一次改动）。"""
    return replace(book, revision=book.revision + 1)


def active_provider_id(book: ProviderBook) -> str:
    current = book.current()
    return current.id if current is not None else ""


def active_entry(book: ProviderBook) -> ProviderEntry | None:
    return book.current()


def first_valid(book: ProviderBook) -> ProviderEntry | None:
    valid = book.valid()
    return valid[0] if valid else None


def from_legacy_any(data: object) -> ProviderBook:
    """v1 数据 → v2。保留原自定义档，并按需补一档内置千问。"""
    if not isinstance(data, dict):
        return ProviderBook()
    from .config import ProviderConfig  # 局部导入：避免循环

    legacy = ProviderConfig.from_dict(data)
    builtin = ProviderEntry(
        id="qwen",
        name="千问（DashScope）",
        kind=KIND_BUILTIN,
        api="openai-responses",
        base_url="",
        api_key=_clean(legacy.default_api_key),
        models=(),
        # 内置档没有模型清单（模型来自内置目录），绑定按名字原样留着：
        # 运行时装配会拿它去对内置目录，对不上就当“同主模型”，不会跑出死名字。
        roles={
            role: value
            for role, value in (
                ("vision", _clean(legacy.role_models.vision)),
                ("grade", _clean(legacy.role_models.grade)),
            )
            if value
        },
    )
    entries: list[ProviderEntry] = [builtin]
    custom = legacy.custom
    if custom.base_url and custom.models:
        models = tuple(ModelEntry(id=mid) for mid in custom.models)
        raw_roles: dict[str, str] = {}
        if legacy.role_models.vision:
            raw_roles["vision"] = legacy.role_models.vision
        if legacy.role_models.grade:
            raw_roles["grade"] = legacy.role_models.grade
        host = custom.base_url.split("//")[-1].split("/")[0]
        entries.append(
            ProviderEntry(
                id="custom",
                name="自建 " + host,
                kind=KIND_CUSTOM,
                api="openai-chat",
                base_url=custom.base_url,
                api_key=custom.api_key,
                models=models,
                roles=_clean_roles(raw_roles, models),
            )
        )
    last: dict[str, str] = {}
    if legacy.source == "custom" and custom.selected_model:
        last["custom"] = custom.selected_model
    active = "custom" if legacy.source == "custom" else "qwen"
    return _settle_active(ProviderBook(active=active, providers=tuple(entries), last_models=last))


def apply_legacy(book: ProviderBook, legacy) -> ProviderBook:
    """把一份 v1 形状的改动并回 v2，返回新书（老接口 POST /api/config 的落盘口）。

    只有它管的那些字段会变，其余档原样保留——老接口再也不能把多档配置冲成两档。
    目标档的选法：
    - source=default → 内置千问那一档；
    - source=custom → 当前档若是自定义就用它，否则用第一个自定义档，一个都没有就新建 id=custom。
    """
    entries = list(book.providers)
    last = dict(book.last_models)
    qwen_index = next((i for i, e in enumerate(entries) if e.kind == KIND_BUILTIN), None)

    if legacy.source == "default":
        if qwen_index is None:
            entries.insert(0,
                ProviderEntry(id="qwen",
                name="千问（DashScope）",
                kind=KIND_BUILTIN,
                api="openai-responses"))
            qwen_index = 0
        entry = entries[qwen_index]
        entries[qwen_index] = replace(
            entry,
            api_key=_clean(legacy.default_api_key),
            roles=_merge_roles(entry.roles, legacy.role_models),
        )
        return _settle_active(replace(book,
            active=entries[qwen_index].id,
            providers=tuple(entries),
            last_models=last))

    custom = legacy.custom
    # 老接口只交一串 model id：不能直接 normalize（那会把上下文窗口 / 思考档 / 参数值
    # 全抹成默认值）。按 id 复用已存条目的元数据，只有新出现的 id 才是空条目。
    active_entry = book.find(book.active)
    existing_by_id = {m.id: m for m in (active_entry.models if active_entry else ())}
    models = tuple(
        replace(existing_by_id[m.id], label=existing_by_id[m.id].label or m.id)
        if m.id in existing_by_id
        else m
        for m in normalize_models(custom.models)
    )
    target_index = next(
        (i for i, e in enumerate(entries) if e.id == book.active and e.kind == KIND_CUSTOM),
        None,
    )
    if target_index is None:
        target_index = next((i for i, e in enumerate(entries) if e.kind == KIND_CUSTOM), None)
    if target_index is None:
        entries.append(
            ProviderEntry(id="custom", name="自建 " + _host_of(custom.base_url), kind=KIND_CUSTOM)
        )
        target_index = len(entries) - 1
    entry = entries[target_index]
    entries[target_index] = replace(
        entry,
        name=entry.name or ("自建 " + _host_of(custom.base_url)),
        api=_clean(getattr(custom, "api", "")) or entry.api,
        base_url=_clean(custom.base_url).rstrip("/") or entry.base_url,
        api_key=_clean(custom.api_key) or entry.api_key,
        models=models or entry.models,
        roles=_clean_roles(_merge_roles(entry.roles, legacy.role_models), models or entry.models),
    )
    if _clean(custom.selected_model):
        last[entries[target_index].id] = _clean(custom.selected_model)
    return _settle_active(
        replace(book, active=entries[target_index].id, providers=tuple(entries), last_models=last)
    )


def _host_of(url: str) -> str:
    text = _clean(url)
    return text.split("//")[-1].split("/")[0] or "自定义端点"


def _merge_roles(current: dict[str, str], role_models) -> dict[str, str]:
    """v1 的 RoleModels 覆盖到现有绑定上；空串表示“清掉，回到同主模型”。"""
    out = dict(current)
    for role, attr in (("vision", "vision"), ("grade", "grade")):
        value = _clean(getattr(role_models, attr, ""))
        if value:
            out[role] = value
        else:
            out.pop(role, None)
    return out


def to_legacy(book: ProviderBook):
    """把“当前启用那一档”换算成 v1 的 ProviderConfig，给运行时装配过渡用。"""
    from .config import CustomProvider, ProviderConfig, RoleModels

    entry = book.current()
    if entry is None:
        return ProviderConfig()
    roles = RoleModels(
        vision=entry.roles.get("vision", ""),
        grade=entry.roles.get("grade", ""),
    )
    if entry.kind == KIND_BUILTIN:
        return ProviderConfig(source="default", default_api_key=entry.api_key, role_models=roles)
    return ProviderConfig(
        source="custom",
        default_api_key="",
        custom=CustomProvider(
            base_url=entry.base_url,
            api_key=entry.api_key,
            api=entry.api,
            models=tuple(m.id for m in entry.models),
            selected_model=book.last_models.get(entry.id, ""),
            reasoning_levels=_selected_levels(book, entry),
            reasoning_values=_selected_values(book, entry),
            selected_context_window=_selected_context_window(book, entry),
            selected_max_output=_selected_max_output(book, entry),
            main_model_can_see_image=_selected_sees_images(book, entry),
        ),
        role_models=roles,
    )


def book_to_disk_dict(book: ProviderBook) -> dict:
    """落盘用：v2 主体 + v1 兼容键（并集）。

    为什么要兼容键：老接口 / 老版本读的是 v1 形状（source / default_api_key /
    custom / role_models）。只写 v2 会让它们读到空配置。这里把「当前启用那一档」
    再投影一份到 v1 键上，读 v1 的代码看到的仍然是它认识的那一套；
    读 v2 的代码只看 providers 那几个键，多余键会被忽略。
    """
    data = book_to_dict(book)
    legacy = to_legacy(book)
    # v1 兼容键只留结构：老读者要钥匙请读凭据文件 / 环境变量
    data.update(
        {
            **legacy.to_dict(),
            "default_api_key": "",
            "custom": {**legacy.custom.to_dict(), "api_key": ""},
        }
    )
    return data


def default_credential_ref(provider_id: str) -> str:
    """给一档派生凭据名：``<ID>_API_KEY``（照 DSH 由 provider 名派生 ref 的做法）。"""
    clean = "".join(ch if ch.isalnum() else "_" for ch in provider_id).upper()
    clean = "_".join(part for part in clean.split("_") if part) or "PROVIDER"
    return clean + "_API_KEY"


def hydrate_credentials(book: ProviderBook, store) -> ProviderBook:
    """把"明文钥匙"迁移成"凭据引用"，并把引用解析成运行时用的值（幂等）。"""
    entries: list[ProviderEntry] = []
    for entry in book.providers:
        ref = entry.api_key_env
        legacy_value = entry.api_key
        if not ref and legacy_value:
            ref = default_credential_ref(entry.id)
            try:
                store.set(ref, legacy_value)
            except ValueError:
                ref = ""
        resolved = store.resolve(ref) if ref else None
        entries.append(replace(entry, api_key_env=ref, api_key=resolved or ""))
    return replace(book, providers=tuple(entries))

def _selected_model(book: ProviderBook, entry: ProviderEntry) -> ModelEntry | None:
    """这一档"上次选的那个模型"。

    没选过（或选过的那个已被删）就落到清单第一个 —— 和 apply.py 里"型号不在清单就回落
    第一个"是同一条口径。不回落的话，刚加好的档在用户去胶囊里点一次之前，
    能力字段（能不能看图 / 窗口多大）全是空的，界面会把图片入口关掉且不给理由。
    """
    chosen = book.last_models.get(entry.id, "")
    picked = entry.model(chosen) if chosen else None
    if picked is not None:
        return picked
    return entry.models[0] if entry.models else None


def _selected_levels(book: ProviderBook, entry: ProviderEntry) -> tuple[str, ...]:
    model = _selected_model(book, entry)
    return model.reasoning_levels if model is not None else ()


def _selected_sees_images(book: ProviderBook, entry: ProviderEntry) -> bool:
    """选中模型自己能不能看图（模型目录里"输入类型"勾了图片）。"""
    model = _selected_model(book, entry)
    return bool(model is not None and "image" in model.inputs)


def _selected_context_window(book: ProviderBook, entry: ProviderEntry) -> int:
    model = _selected_model(book, entry)
    return model.context_window if model is not None else 0


def _selected_max_output(book: ProviderBook, entry: ProviderEntry) -> int:
    model = _selected_model(book, entry)
    return model.max_output if model is not None else 0


def _selected_values(book: ProviderBook, entry: ProviderEntry) -> dict[str, str]:
    model = _selected_model(book, entry)
    return dict(model.reasoning_values) if model is not None else {}
