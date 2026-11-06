"""本地 ECDICT（SQLite）查词核心：agent 查词工具与 ``/api/dict/lookup`` 共用。

只读、纯本地、不触网。输出前端 ``DictEntry`` 形状的 dict（见 web-desktop 的 types）。
ECDICT 没有的东西不编造：例句留空、近/反义不返回；发音由前端走有道 dictvoice。

两条查询路径：先直接命中 ``words``；查不到再用 ``forms`` 把变形（went/children）
反查回原形（go/child）。
"""

from __future__ import annotations

import os
import re
import sqlite3
from pathlib import Path

from .cefr_scale import estimate_word_level

__all__ = ["DictRepository", "default_dict_path", "TAG_ORDER"]

# exchange 字段里我们关心的词形：键是 ECDICT 代号，值是中文说明
_EXCHANGE_LABELS = {
    "p": "过去式",
    "d": "过去分词",
    "i": "现在分词",
    "3": "第三人称单数",
    "s": "名词复数",
    "r": "比较级",
    "t": "最高级",
}

# 释义行首的词性缩写（n./vt./a.…）。注意：方括号的是语域/领域标签
# （[法]/[经]/[医]/[网络]…），不是词性——必须原样留在译文里，否则界面会把
# 「[法] 犯…」显示成「法：犯…」，还会污染背词干扰项的词性判断。
_POS_LINE = re.compile(r"^\s*([A-Za-z]{1,6}\.)\s*")

# ECDICT 考试标签 → 可读名称（顺序即展示顺序）
_TAG_LABELS = {
    "zk": "中考",
    "gk": "高考",
    "cet4": "CET4",
    "cet6": "CET6",
    "ky": "考研",
    "ielts": "雅思",
    "toefl": "托福",
    "gre": "GRE",
    "bec": "BEC",
}
_TAG_ORDER = ["zk", "gk", "cet4", "cet6", "ky", "ielts", "toefl", "gre", "bec"]

# 词书接口对外公开的标签顺序（背词页按此列出考试词书）
TAG_ORDER = _TAG_ORDER


def default_dict_path() -> Path | None:
    """定位裁剪后的 ECDICT db。

    顺序：环境变量 ``GLYPH_DICT_PATH`` → 项目 ``data/dict.db``（开发）→
    包内 ``_data/dict.db``（将来打包）；都找不到返回 None（调用方据此降级）。
    """
    env = os.environ.get("GLYPH_DICT_PATH")
    if env:
        p = Path(env)
        return p if p.exists() else None
    here = Path(__file__).resolve()
    candidates = [
        here.parents[3] / "data" / "dict.db",  # 开发：markr/data/dict.db
        here.parents[1] / "_data" / "dict.db",  # 包内：src/glyph/_data
    ]
    return next((c for c in candidates if c.exists()), None)


def _parse_senses(text: str) -> list[dict]:
    """ECDICT 释义（中文 translation，或兜底英文 definition）→ senses。

    ECDICT 用字面 ``\\n``（反斜杠+n）表示换行，先还原成真换行再按行拆；
    每行形如 'n. 苹果, 家伙'，剥出行首词性；行首的 '[法]/[网络]' 等语域标签
    不是词性，连同译文原样保留（可能出现在行首或行中）。
    """
    text = (text or "").replace("\\n", "\n")
    senses: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        pos = ""
        m = _POS_LINE.match(line)
        if m:
            pos = m.group(1)
            line = line[m.end():].strip()
        if line:
            senses.append({"pos": pos, "translation": line, "examples": []})
    return senses


def _parse_definitions(text: str | None) -> list[str]:
    """ECDICT 英文释义（definition）→ 展示用英文行。

    真实换行与字面 \\n 都可能出现，统一还原后按行拆；strip、去空、最多 6 行。
    只给词典页「英文释义」块用，**不要塞进中文 senses**。
    """
    normalized = (text or "").replace("\\n", "\n")
    return [line.strip() for line in normalized.splitlines() if line.strip()][:6]


def _parse_forms(exchange: str | None) -> list[str]:
    """exchange 'p:went/d:gone/i:going/3:goes' → ['过去式 went', ...]。

    ECDICT 对"本身是变形"的词条（children）用内部代号 0（原形）/1（变形类型
    编号）反指词根，不是给学习者看的变形，忽略；原形关系由 lookup 走 forms
    表得到 base_form。
    """
    out: list[str] = []
    for piece in (exchange or "").split("/"):
        if ":" not in piece:
            continue
        code, val = piece.split(":", 1)
        code = code.strip()
        if code in ("0", "1"):  # 内部代号：原形 / 变形类型编号，不展示
            continue
        val = val.strip()
        if not val:
            continue
        label = _EXCHANGE_LABELS.get(code)
        out.append(f"{label} {val}" if label else val)
    return out


def _map_tags(tag: str) -> list[str]:
    """'cet4 cet6 ky' → ['CET4', 'CET6', '考研']；未知原始标签追加在后。"""
    present = set((tag or "").split())
    out = [_TAG_LABELS[t] for t in _TAG_ORDER if t in present]
    out.extend(sorted(t for t in present if t not in _TAG_LABELS))
    return out


class DictRepository:
    """从裁剪后的 ECDICT SQLite 只读查词（含变形反查）。"""

    def __init__(self, db_path: str | Path | None) -> None:
        self._db = Path(db_path) if db_path is not None else None

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self._db.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def lookup(self, word: str) -> dict | None:
        key = (word or "").strip().lower()
        if not key or self._db is None or not self._db.exists():
            return None
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM words WHERE word = ? LIMIT 1", (key,)
            ).fetchone()
            queried_form: str | None = None
            base_form: str | None = None
            if row is None:
                # 词本身未收录：用 forms 把变形反查回原形（went→go）
                link = con.execute(
                    "SELECT base FROM forms WHERE form = ? LIMIT 1", (key,)
                ).fetchone()
                if link is not None:
                    queried_form = key
                    row = con.execute(
                        "SELECT * FROM words WHERE word = ? LIMIT 1", (link["base"],)
                    ).fetchone()
            else:
                # 词本身被收录，但它可能仍是另一词的变形（children 是 child 的复数）
                link = con.execute(
                    "SELECT base FROM forms WHERE form = ? LIMIT 1", (key,)
                ).fetchone()
                if link is not None and link["base"] != key:
                    base_form = link["base"]
            if row is None:
                return None
        entry = self._row_to_entry(row)
        if queried_form is not None:
            entry["queried_form"] = queried_form
        if base_form is not None:
            # 词本身就是另一词的变形（children）：其 exchange 反指原形、含噪声，
            # 常规"词形变化"不适用，丢弃，只保留原形关系
            entry.pop("forms", None)
            entry["base_form"] = base_form
        return entry

    def word_level(self, word: str) -> str | None:
        """查单词的 CEFR 等级近似（变形先反查原形）；未收录或无 db 返回 None。"""
        key = (word or "").strip().lower()
        if not key or self._db is None or not self._db.exists():
            return None
        with self._connect() as con:
            row = con.execute(
                "SELECT tag, frq, oxford FROM words WHERE word = ? LIMIT 1", (key,)
            ).fetchone()
            if row is None:
                link = con.execute(
                    "SELECT base FROM forms WHERE form = ? LIMIT 1", (key,)
                ).fetchone()
                if link is None:
                    return None
                row = con.execute(
                    "SELECT tag, frq, oxford FROM words WHERE word = ? LIMIT 1",
                    (link["base"],),
                ).fetchone()
        if row is None:
            return None
        return estimate_word_level(tag=row["tag"], frq=row["frq"], oxford=row["oxford"])

    def is_available(self) -> bool:
        """词库 db 是否就绪（词书接口据此决定能否服务）。"""
        return self._db is not None and self._db.exists()

    def book_stats(self) -> list[dict]:
        """各考试标签的真实词量，按 ``TAG_ORDER`` 排列；db 缺失返回空表。

        标签按空格切 token 精确匹配（``cet4`` 不能误命中 ``cet4x``）。
        """
        if not self.is_available():
            return []
        out: list[dict] = []
        with self._connect() as con:
            for tag in _TAG_ORDER:
                total = con.execute(
                    "SELECT COUNT(*) FROM words WHERE ' ' || tag || ' ' LIKE ?",
                    (f"% {tag} %",),
                ).fetchone()[0]
                out.append({"tag": tag, "label": _TAG_LABELS[tag], "total": total})
        return out

    def book_words(self, tag: str, cursor: int = 0, limit: int = 600) -> dict:
        """按考试标签取一页词书词表。

        排序：ECDICT ``frq`` 是语料词频排名（数字越小越常见），有值的按升序
        排前面，缺值（0/NULL）的按字母序兜底排后面——背词从高频词背起。
        分页用 OFFSET 游标，返回 ``{tag,total,cursor,next_cursor,words}``。
        """
        tag = (tag or "").strip().lower()
        if tag not in _TAG_ORDER:
            raise ValueError(f"未知词书标签：{tag!r}")
        empty = {"tag": tag, "total": 0, "cursor": max(0, cursor),
                 "next_cursor": None, "words": [], "entries": []}
        if not self.is_available():
            return empty
        cursor = max(0, cursor)
        limit = max(1, min(limit, 2000))
        order = ("CASE WHEN frq IS NULL OR frq <= 0 THEN 1 ELSE 0 END, "
                 "CASE WHEN frq > 0 THEN frq ELSE 0 END, word")
        with self._connect() as con:
            total = con.execute(
                "SELECT COUNT(*) FROM words WHERE ' ' || tag || ' ' LIKE ?",
                (f"% {tag} %",),
            ).fetchone()[0]
            rows = con.execute(
                f"SELECT word, phonetic, translation FROM words "
                f"WHERE ' ' || tag || ' ' LIKE ? "
                f"ORDER BY {order} LIMIT ? OFFSET ?",
                (f"% {tag} %", limit, cursor),
            ).fetchall()
        words = [r["word"] for r in rows]
        # 同页附带轻量释义：学习卡四选项要靠同书词的词性/第一释义做干扰项，
        # 只给第一义项的第一个分句，整页体积可控（完整释义仍走 /api/dict/lookup）
        entries = [{
            "word": r["word"],
            "phonetic": (r["phonetic"] or "").strip().lstrip("'"),
            **self._first_gloss(r["translation"] or ""),
        } for r in rows]
        end = cursor + len(words)
        return {
            "tag": tag,
            "total": total,
            "cursor": cursor,
            "next_cursor": end if end < total else None,
            "words": words,
            "entries": entries,
        }

    @staticmethod
    def _first_gloss(translation: str) -> dict:
        """从 ECDICT 原始释义里取第一义项：``{pos, gloss}``；gloss 只留第一个分句。"""
        senses = _parse_senses(translation)
        if not senses:
            return {"pos": None, "gloss": ""}
        first = senses[0]
        gloss = re.split(r"[；;]", first.get("translation") or "")[0].strip()
        return {"pos": first.get("pos"), "gloss": gloss}

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> dict:
        """ECDICT 行 → 前端 DictEntry dict。"""
        phon = row["phonetic"] or ""
        phon = f"/{phon}/" if phon else ""
        senses = _parse_senses(row["translation"] or "")
        if not senses:
            # 中文释义缺失时退回英文释义兜底，绝不让 senses 为空
            senses = _parse_senses(row["definition"] or "")
        entry: dict = {
            "word": row["word"],
            "phonetic": {"uk": phon, "us": phon},
            "senses": senses,
        }
        definitions = _parse_definitions(row["definition"])
        if definitions:
            entry["definitions"] = definitions
        tags = _map_tags(row["tag"] or "")
        if tags:
            entry["tags"] = tags
        forms = _parse_forms(row["exchange"])
        if forms:
            entry["forms"] = forms
        return entry
