"""OCR 转写（图片 → 英文文本）的领域模型与确定性扫描。

这里是「文本进口」那一侧的模型，**不含任何批改概念**：转写只负责把图片里的字
抄下来，批改只能由 ``grade_text`` 做（职责闸：OCR 出参永不出现 detections）。

低置信的唯一真源是模型自曝的 ``[?]`` 占位——工具**不猜**哪里可疑，只把模型自己
标出来的地方原样交给人看（诚实闸）。新增「可疑」判定方式必须同步改这里的扫描
函数与其单测。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

# 一行里只要还有拉丁字母，就当它是作文正文的一部分
_LATIN_RE = re.compile(r"[A-Za-z]")

# 模型认不出时的占位符：文本里出现它就代表"这里没把握，请人核对"
PLACEHOLDER = "[?]"
PLACEHOLDER_REASON = "模型未能确认此处内容，请对照原图核对"

__all__ = [
    "PLACEHOLDER",
    "LowConfidenceSpan",
    "Transcription",
    "find_low_confidence",
    "strip_noise_lines",
    "unspell_transcription",
]


def strip_noise_lines(text: str) -> str:
    """确定性降噪：剔除「不含任何拉丁字母」的整行，保留原行序。

    冒烟实测（10 张学习 App 截图样张）发现：即使提示词已经写明忽略界面元素，
    视觉模型偶尔仍会把一行中文界面文案（如「今日总结：」）混进输出。这类夹带
    与 OCR 认错字同属污染——混进正文就会被当成作文的一部分去批改。

    为什么不交给模型自己判：那是不可离线测的不确定面。这里是**确定性规则**——
    批改的对象是英文作文，一行里连一个英文字母都没有，就不可能是正文。
    """
    kept = [line for line in text.splitlines() if _LATIN_RE.search(line)]
    return "\n".join(kept).strip()


# 逐字母拼写链：单字符（字母/撇号）用连字符连接，如 b-u-y-e-d、d-o-n-'-t。
# 两侧边界保证不拆 x-ray 这类真连字符词——ray 是多字符片段，链在 r 后紧跟 a
# 处无法继续，边界断言失败即不匹配。
_SPELLED_RE = re.compile(r"(?<![A-Za-z'])[A-Za-z'](?:-[A-Za-z'])+(?![A-Za-z'])")
# 独占一个 token 的句读标点（"一词一行"格式下模型会把句号单独放一行）
_PUNCT_ONLY_RE = re.compile(r"^[.,!?;:()]+$")


def _unspell_token(token: str) -> str:
    """还原单个 token 内的逐字母拼写链（b-u-y-e-d -> buyed），其余原样。"""
    token = _SPELLED_RE.sub(lambda m: m.group(0).replace("-", ""), token)
    # 半守格式的残渣：模型把连字符粘在标点前（Nowadays-, / fields-.）。
    # 真连字符词（x-ray）连字符两侧都是字母，打不到这条规则。
    return re.sub(r"(?<=[A-Za-z])-(?=[.,!?;:])", "", token)


def _join_tokens(tokens: list[str]) -> str:
    """拼回一行/一段：独占标点粘到前一个词上，其余词之间用单空格。"""
    out: list[str] = []
    for tok in tokens:
        if not tok:
            continue
        if tok == "/":
            # 逐字母格式的词间分隔符被模型当正文抄出来（2026-10-13 打印体真机），
            # 它永远不是内容；「and/or」这类词内斜杠不带空格、不走这个分支。
            continue
        if _PUNCT_ONLY_RE.match(tok) and out:
            out[-1] = out[-1] + tok
        else:
            out.append(tok)
    return " ".join(out)


def unspell_transcription(raw: str) -> str:
    """把逐字母转写还原成正常文本，兼容模型真机的两种排法（#98）。

    真机实测视觉模型对逐字母格式有两种排法：

    - **一词一行（提示词要求的主格式）**：每个词独占一行、原文行之间留空行、
      句号有时独占一行；
    - **一行多词（模型偶尔不遵守时）**：一行放多个拼写词、空格分隔；极端情况
      下还会直接输出正常英文句子。

    判定：非空行里"不含空格的行"过半且行数足够，即按一词一行处理——空行分段、
    词行用空格拼回、独占标点粘回前词；否则按普通行处理（行内拼回、保留物理
    换行）。两条分支对正常英文句子都幂等。若跨词被连字符整条焊死
    （I-g-o-e-d-t-o...），词层已无边界可分，属格式彻底崩坏，只能靠转写后的人工
    确认，本函数不猜词（诚实闸）。
    """
    lines = raw.splitlines()
    nonempty = [ln for ln in lines if ln.strip()]
    bare = [ln for ln in nonempty if " " not in ln.strip()]
    # 一词一行格式下几乎每行都是一个无空格 token；正常句子则每行含多个词。
    # 要求至少 3 行且无空格行过半才切换解析，避免误伤"恰好一个词独占一行"的普通文本。
    one_per_line = len(nonempty) >= 3 and len(bare) * 2 > len(nonempty)

    if one_per_line:
        paragraphs: list[list[str]] = [[]]
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if paragraphs[-1]:
                    paragraphs.append([])
                continue
            paragraphs[-1].append(_unspell_token(stripped))
        joined = [_join_tokens(part) for part in paragraphs if part]
        return "\n".join(joined).strip()

    restored = [
        _join_tokens([_unspell_token(tok) for tok in line.split()]) for line in nonempty
    ]
    return "\n".join(restored).strip()


class LowConfidenceSpan(BaseModel):
    """一处「模型没把握」的片段，给人看的高亮依据。"""

    start: int
    end: int
    text: str
    reason: str


class Transcription(BaseModel):
    """一次转写的成品（``ocr_image`` 工具出参的核心）。"""

    text: str
    low_confidence: list[LowConfidenceSpan] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def find_low_confidence(text: str) -> list[LowConfidenceSpan]:
    """确定性扫描 ``[?]`` 占位 → 低置信片段列表（按出现顺序）。

    纯函数、不联网、可离线测。找不到占位就返回空列表。
    """
    spans: list[LowConfidenceSpan] = []
    cursor = 0
    while True:
        idx = text.find(PLACEHOLDER, cursor)
        if idx < 0:
            return spans
        spans.append(
            LowConfidenceSpan(
                start=idx,
                end=idx + len(PLACEHOLDER),
                text=PLACEHOLDER,
                reason=PLACEHOLDER_REASON,
            )
        )
        cursor = idx + len(PLACEHOLDER)
