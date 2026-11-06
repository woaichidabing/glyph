"""从 ECDICT 完整 csv 构建桌面端用的精简 SQLite 词库（可重复运行）。

输入：data/ecdict.csv（skywind3000/ECDICT，约 77 万词条）
输出：data/dict.db
  - words：按考试标签 + 词频裁剪到约 5 万高频词（覆盖高考/CET4/6/考研/雅思/托福/GRE）
  - forms：词形变化（过去式 / 复数 / 分词…）→ 原形，供查 went 自动回到 go

跑法（在 glyph 目录）：.venv/Scripts/python scripts/build_dict_db.py
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

GLYPH = Path(__file__).resolve().parents[1]
CSV_PATH = GLYPH / "data" / "ecdict.csv"
DB_PATH = GLYPH / "data" / "dict.db"

EXAM_TAGS = {"gk", "cet4", "cet6", "ky", "ielts", "toefl", "gre"}
FREQ_LIMIT = 40000

# exchange 里需要建反查的变形类型：
# p 过去式、d 过去分词、i 现在分词、3 第三人称单数、s 名词复数、r 比较级、t 最高级
FORM_KEYS = {"p", "d", "i", "3", "s", "r", "t"}


def keep(row: dict) -> bool:
    """裁剪口径：考试标签命中，或 BNC/当代语料词频排在前 4 万，或柯林斯/牛津核心词。"""
    tag = row["tag"] or ""
    if any(t in EXAM_TAGS for t in tag.split()):
        return True
    bnc = int(row["bnc"] or 0)
    frq = int(row["frq"] or 0)
    if 0 < bnc <= FREQ_LIMIT or 0 < frq <= FREQ_LIMIT:
        return True
    if int(row["collins"] or 0) > 0 or int(row["oxford"] or 0) == 1:
        return True
    return False


def parse_exforms(exchange: str) -> list[str]:
    """exchange 形如 'p:went/d:gone/i:going'，取出需要反查的变形词（小写）。"""
    out: list[str] = []
    for piece in (exchange or "").split("/"):
        if ":" not in piece:
            continue
        key, val = piece.split(":", 1)
        if key in FORM_KEYS and val:
            out.append(val.lower())
    return out


def main() -> None:
    if not CSV_PATH.exists():
        raise SystemExit(f"找不到 {CSV_PATH}，请先下载 ecdict.csv")
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute(
            """
            CREATE TABLE words (
                word TEXT PRIMARY KEY,
                phonetic TEXT,
                definition TEXT,
                translation TEXT,
                pos TEXT,
                collins INTEGER,
                oxford INTEGER,
                tag TEXT,
                bnc INTEGER,
                frq INTEGER,
                exchange TEXT
            )
            """
        )
        con.execute("CREATE TABLE forms (form TEXT PRIMARY KEY, base TEXT)")

        kept = 0
        form_rows: dict[str, str] = {}
        with open(CSV_PATH, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if not keep(row):
                    continue
                word = (row["word"] or "").strip().lower()
                if not word:
                    continue
                con.execute(
                    "INSERT OR REPLACE INTO words VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        word,
                        row["phonetic"] or "",
                        row["definition"] or "",
                        row["translation"] or "",
                        row["pos"] or "",
                        int(row["collins"] or 0),
                        int(row["oxford"] or 0),
                        row["tag"] or "",
                        int(row["bnc"] or 0),
                        int(row["frq"] or 0),
                        row["exchange"] or "",
                    ),
                )
                kept += 1
                for form in parse_exforms(row["exchange"] or ""):
                    # 一个变形可能对应多个候选，先到先得；独立词（如 found）会先命中 words 表
                    form_rows.setdefault(form, word)

        con.executemany(
            "INSERT OR REPLACE INTO forms (form, base) VALUES (?,?)",
            list(form_rows.items()),
        )
        con.execute("CREATE INDEX idx_words_bnc ON words(bnc)")
        con.commit()
        print(f"保留词条 {kept}，变形映射 {len(form_rows)}，写出 {DB_PATH}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
