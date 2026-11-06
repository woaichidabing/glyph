"""产品名一次性改名：Glyph → Glyph（2026-10-22 拍板）。

规则：
- 只改**产品名** Glyph（首字母大写），代码标识符一律不动：
  `GlyphError`（代码类名）、`src/glyph/`、`GLYPH_*` 环境变量、
  `~/.markr` 数据目录、`markr-wip` 分支、`glyph-desktop` 包名（均小写，天然不匹配）。
- 字节级读写，保留原换行符（LF / CRLF 都不碰）。
- 幂等：`Glyph → Glyph` / `Glyph→Glyph` 已是历史引用的写法（改名说明里会出现），
  先用哨兵占位再替换，重跑不会把说明文洗坏。
- **只跑一次**。之后新写的文档直接用 Glyph。

范围 = 活文档（描述当前产品、后续仍会读/改）：
- 仓库根：AGENTS.md / README.md / REFACTOR-DECISIONS.md
- docs/**（design / prd / review / README / REVIEW / logging-explained）
- learning/interview-cards.md
- web-desktop2 的 README 与 design-lab README

明确**不在**范围（留档，改了就是篡改记录）：
- web-desktop/**（旧前端，AGENTS.md 定性为「设计过程留档」）
- coordination/**（多 agent 信箱，追加式记录）
- .workbuddy/**（工作记忆）

用法（仓库根）：

    .venv\\Scripts\\python.exe scripts/rename_to_glyph.py            # 实跑
    .venv\\Scripts\\python.exe scripts/rename_to_glyph.py --dry-run  # 只报告
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 改名说明里会出现的历史写法，先占位保护，替换完再放回
SENTINELS = [
    ("Glyph \u2192 Glyph", "\x00HIST1\x00"),
    ("Glyph\u2192Glyph", "\x00HIST2\x00"),
]

# 显式名单（docs/ 与 web-desktop2/ 用递归 glob，见下）
EXPLICIT = [
    "AGENTS.md",
    "README.md",
    "REFACTOR-DECISIONS.md",
    "learning/interview-cards.md",
    "web-desktop2/README.md",
    "web-desktop2/design-lab/plan-redesign/README.md",
    "web-desktop2/design-lab/ui-refresh/README.md",
    "web-desktop2/design-lab/wordbook-card/README.md",
]

# 整目录递归（均为活文档区）
RECURSE = ["docs", "web-desktop2"]

# 不在任何 glob 范围内；写在这里是为了失败时好排查
OUT_OF_SCOPE = ["web-desktop", "coordination", ".workbuddy", "node_modules"]


def collect_files() -> list[Path]:
    files: list[Path] = []
    for rel in EXPLICIT:
        p = ROOT / rel
        if p.is_file():
            files.append(p)
        else:
            print(f"[warn] 缺文件：{rel}", file=sys.stderr)
    for d in RECURSE:
        base = ROOT / d
        if not base.is_dir():
            print(f"[warn] 缺目录：{d}", file=sys.stderr)
            continue
        for p in sorted(base.rglob("*.md")):
            # 递归目录里也可能混进留档子目录，按前缀排除
            rel = p.relative_to(ROOT).as_posix()
            if any(rel.startswith(f"{x}/") for x in OUT_OF_SCOPE):
                continue
            if p not in files:
                files.append(p)
    return files


def rename_text(text: str) -> tuple[str, int]:
    for old, sentinel in SENTINELS:
        text = text.replace(old, sentinel)
    # GlyphError 是代码类名，负向断言保它
    new_text, n = re.subn(r"Glyph(?!Error)", "Glyph", text)
    for old, sentinel in SENTINELS:
        new_text = new_text.replace(sentinel, old)
    return new_text, n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    files = collect_files()
    total = 0
    touched = 0
    for p in files:
        raw = p.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            print(f"[skip] 非 UTF-8：{p.relative_to(ROOT)}", file=sys.stderr)
            continue
        new_text, n = rename_text(text)
        if n == 0:
            continue
        touched += 1
        total += n
        rel = p.relative_to(ROOT).as_posix()
        if args.dry_run:
            print(f"  {n:3d}  {rel}")
        else:
            # 字节级写回：换行符一个字符都不动
            p.write_bytes(new_text.encode("utf-8"))
            print(f"  {n:3d}  {rel}")

    mode = "（dry-run）" if args.dry_run else ""
    print(f"\n共 {touched} 个文件、{total} 处替换 {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
