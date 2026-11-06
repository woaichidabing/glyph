"""会话流水的记录模型（R4：事件词典与信封只此一处定义）。

一个会话文件 = 若干行 JSON，每行都是同一个信封：

    {"v": 1, "seq": 3, "ts": "...", "type": "tool_call", "payload": {...}}

- ``RecordType`` 是 9 类事件的词典；每个载荷模型用 ``record_type`` 声明自己的归属，
  新增事件 = 加一个枚举成员 + 一个载荷模型，两处都在这一个文件里。
- ``Record`` 是信封本身。``type`` 故意用 ``str`` 而不是枚举：读到本版不认识的类型时要能
  整行忽略（向前兼容），而不是在校验阶段就把整个文件判死；``record_type`` 属性负责翻译。
- 载荷在 ``Record`` 里保存为普通 JSON 字典（落盘什么样、内存里什么样），读者再用
  ``payload_as()`` 换回类型安全的载荷模型。
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, ClassVar, TypeVar

from pydantic import BaseModel, Field

from ..types import FinishReason, Usage

SCHEMA_VERSION = 1
"""行级 schema 版本：写永远写这一个值；读靠「忽略未知类型」兼容未来小版本。"""

APP_VERSION = "0.1.0"
"""写进首行 meta 的程序版本，与 pyproject 的 version 对齐。

不用 ``importlib.metadata.version``：本项目路径含中文且禁止 editable 安装（R9），
运行时读不到已安装的包元数据。升级版本时改这里（有心就顺势同步 pyproject）。
"""

FINGERPRINT_PREFIX = "sha256:"
FINGERPRINT_LENGTH = 12


class RecordType(StrEnum):
    """流水里的事件类型词典。取值范围即本文件里全部载荷模型的集合。"""

    SESSION_META = "session_meta"  # 首行身份证
    USER_MESSAGE = "user_message"  # 用户说话
    ASSISTANT_MESSAGE = "assistant_message"  # 模型一轮输出的文本
    TOOL_CALL = "tool_call"  # 该轮里的一个工具调用
    TOOL_RESULT = "tool_result"  # 该调用的回报（成功/失败/取消都算）
    TURN_END = "turn_end"  # 一次 run 的终态
    CANCEL = "cancel"  # 用户取消 / 中断
    REASONING = "reasoning"  # 模型思考过程，只给人看
    COMPACT_BOUNDARY = "compact_boundary"  # L3 摘要落盘后的边界（v2.1 #65 起产出）
    # L3 摘要正文（v2.1 #65，§9.5）：写手产出的结构化摘要，永远先于配套 boundary 写入，
    # boundary.summary_record_seq 指回本记录；只追加，多次压缩每次各一条。
    COMPACT_SUMMARY = "compact_summary"
    FINALIZE = "finalize"  # 用户对一次批改的定稿（不经 loop，不进 messages()）


class RecordPayload(BaseModel):
    """载荷基类：子类用类属性 ``record_type`` 声明自己属于哪一类事件。"""


PayloadT = TypeVar("PayloadT", bound=RecordPayload)


class SessionMeta(RecordPayload):
    """首行元信息：文件的身份证。"""

    record_type: ClassVar[RecordType] = RecordType.SESSION_META

    schema_version: int = SCHEMA_VERSION
    session_id: str
    # 预留：本地匿名学习者档案 / 一机多人（D17）。第一版恒为 None，路径不含它。
    learner_id: str | None = None
    model: str
    cwd: str
    created_at: str
    app_version: str = APP_VERSION
    system_prompt_fingerprint: str


class UserMessagePayload(RecordPayload):
    """用户说的一句话。"""

    record_type: ClassVar[RecordType] = RecordType.USER_MESSAGE
    text: str


class AssistantMessagePayload(RecordPayload):
    """模型一轮输出的文本部分（它带的工具调用各占一行 tool_call）。"""

    record_type: ClassVar[RecordType] = RecordType.ASSISTANT_MESSAGE
    text: str


class ToolCallPayload(RecordPayload):
    """模型要调的一个工具。arguments 是适配层已经拼全、解析好的 dict。"""

    record_type: ClassVar[RecordType] = RecordType.TOOL_CALL
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResultPayload(RecordPayload):
    """工具跑完的回报。ok=False 时 error 里是人话原因（错误也是数据）。"""

    record_type: ClassVar[RecordType] = RecordType.TOOL_RESULT
    call_id: str
    name: str
    ok: bool = True
    duration_ms: float = 0.0
    output: dict[str, Any] | None = None
    error: str | None = None


class TurnEndPayload(RecordPayload):
    """一次 run 的终态：为什么停、多久、花了多少 token、迭代几轮。

    ``usage`` 是**逐轮累计**的总用量；``context_input_tokens`` 是**最后一轮**实际发送的
    全量上下文 token（用量环的分子）。两者语义不同：累计是"这次 run 一共花了多少"，
    最后一轮是"结束时上下文有多大"，界面回显用量环用的是后者。
    """

    record_type: ClassVar[RecordType] = RecordType.TURN_END
    stop_reason: FinishReason
    duration_ms: float = 0.0
    usage: Usage | None = None
    context_input_tokens: int | None = None
    iterations: int = 0


class CancelPayload(RecordPayload):
    """一次取消。取消前已开始的 tool_call 仍各自有一条 tool_result 闭合。"""

    record_type: ClassVar[RecordType] = RecordType.CANCEL
    reason: str = "user"
    iteration: int | None = None


class ReasoningPayload(RecordPayload):
    """模型思考过程：落盘给人看，永远不进 messages()。"""

    record_type: ClassVar[RecordType] = RecordType.REASONING
    text: str


class CompactBoundaryPayload(RecordPayload):
    """一次 L3 压缩落盘后的边界标记（v2.1 #65 起产出，§9.5）。

    只追加、不覆盖；重建 messages() 时，``at_seq`` 之前的对话记录被配套摘要记录
    （``summary_record_seq`` 指向的那条）+ 提货单替代呈现，原文记录永不删除
    （可审计、可检索、切大档可整段恢复，见 C5）。
    """

    record_type: ClassVar[RecordType] = RecordType.COMPACT_BOUNDARY
    boundary_id: str
    at_seq: int
    summary_record_seq: int


class SummaryPayload(RecordPayload):
    """一次 L3 压缩的摘要正文（v2.1 #65，§9.5）。

    写手（便宜大窗口模型）按固定小节产出的结构化摘要：学习者画像、任务时间线、
    未决问题、典型错误模式……**全量锚点清单不存这里**——它在每次渲染时从全量流水
    实时重扫（§9.6），避免摘要转述漏掉成品、也避免清单过期。

    永远先于配套的 :class:`CompactBoundaryPayload` 写入；多次压缩每次各一条，
    续接时只渲染最后一条（§9.6）。

    - ``covered_up_to_seq``：本次摘要覆盖到的切点（= 配套 boundary.at_seq），便于
      多次压缩时只摘要"上一 boundary 之后的增量"，也便于人肉审计覆盖范围；
    - ``material_truncated``：喂给写手的材料是否因预算（写手窗口 ×60%）截掉了最旧的
      完整往返（§9.3）。截掉不等于丢——成品事实在提货单、跨会话在记忆、原文在流水；
    - ``model``：写手模型 id，供 D2 遥测与排查（空串表示未记录）。
    """

    record_type: ClassVar[RecordType] = RecordType.COMPACT_SUMMARY
    text: str
    covered_up_to_seq: int = 0
    material_truncated: bool = False
    model: str = ""


class FinalizePayload(RecordPayload):
    """用户对一次批改的定稿（v2.1 #69 / A1，方案 §8.6）。

    定稿走独立 HTTP 端点、**不经过 agent loop**，所以它既不是 tool_call 也不是
    tool_result，而是单独一类记录。它**绝不进 ``messages()``**（不是对话，不能当普通
    消息发给模型）；对模型只通过提货单"已定稿"状态与 ``read_artifact(which="final")``
    暴露，对人通过 replay 的定稿区块暴露。

    只追加：同一 ``grade_call_id`` 多次定稿全部保留，读取一律取**最新一条**。

    刻意**不存**原文 ``text``（挂 grade_call_id 从 grade_text 入参取）与可再生的
    marks / result_text / 讲解清单（finalize_decisions 是纯函数，随时可由本记录 +
    流水里的 grade 入参/结果重算）——存了反而是两份会漂移的真相。
    """

    record_type: ClassVar[RecordType] = RecordType.FINALIZE
    grade_call_id: str
    # 每条 detection 的 id → 四态表态（accepted/rejected/edited/cancelled）
    decisions: dict[str, str] = Field(default_factory=dict)
    # edited 条目里用户手改的内容（detection_id → 新文本）
    overrides: dict[str, str] = Field(default_factory=dict)
    edited_ids: list[str] = Field(default_factory=list)
    # 最终保留（采纳 / 改后采纳）的条目 id
    kept_ids: list[str] = Field(default_factory=list)
    comment: str = ""
    # 服务端 finalize_text 算出的改后全文（read_artifact(which="final") 取它）
    final_text: str = ""


class Record(BaseModel):
    """一行流水的信封：版本号 + 序号 + 时间戳 + 类型 + 载荷。"""

    v: int = SCHEMA_VERSION
    seq: int = 0
    ts: str = ""
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)

    @property
    def record_type(self) -> RecordType | None:
        """本行的类型（词典成员）；不在词典里返回 None —— 读的一方整行忽略，不报错。"""
        try:
            return RecordType(self.type)
        except ValueError:
            return None

    def payload_as(self, model: type[PayloadT]) -> PayloadT:
        """把载荷字典换回具体载荷模型，给调用方一个类型安全的读法。"""
        return model.model_validate(self.payload)

    def to_json_line(self) -> str:
        """序列化成一行 JSON（不含行尾换行；中文不转义，便于人肉看流水）。"""
        return json.dumps(self.model_dump(mode="json"), ensure_ascii=False)


def build_record(seq: int, ts: str, payload: RecordPayload) -> Record:
    """按信封格式封装一行：类型取自载荷自己的 ``record_type``，不用调用方拼字符串。"""
    return Record(
        v=SCHEMA_VERSION,
        seq=seq,
        ts=ts,
        type=payload.record_type.value,
        payload=payload.model_dump(mode="json"),
    )


def system_prompt_fingerprint(system_prompt: str) -> str:
    """系统提示词指纹：``"sha256:" + sha256(text)[:12]``。

    提示词正文不落盘（长且会变，整段存进 meta 会让首行变成几百行），只在首行留指纹，
    重开时据此判断「这次的提示词跟当时是不是同一份」。
    """
    digest = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
    return f"{FINGERPRINT_PREFIX}{digest[:FINGERPRINT_LENGTH]}"
