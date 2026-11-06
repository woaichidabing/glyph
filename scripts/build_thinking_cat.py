"""生成 Glyph 思考强度滑杆上的像素小猫（8 帧跑步循环 → 一张横向精灵图）。

用法（在仓库根目录）：

    .venv\\Scripts\\python.exe scripts\\build_thinking_cat.py            # 写 web-desktop2/src/assets/thinking-cat/
    .venv\\Scripts\\python.exe scripts\\build_thinking_cat.py --preview  # 额外输出放大预览与逐帧检查图

给接手的人：

- 小猫是**本项目原创的像素画**，画在下面这段几何里，不是拷来的素材。
  想调样子就改常量 / 重跑脚本；**不要手改导出的 PNG**。
- 输出两张 8 帧横向条：``thinking-cat-8x.png``（深色主题）/ ``thinking-cat-light-8x.png``（浅色主题）。
  名字里的 ``8x`` 指 8 帧，不是 8 倍缩放。
- 单帧 32×40 像素，界面里按 2 倍显示（16×20 CSS 像素），像素边缘保持锐利。
- 坐标约定：轮廓先画（比填充大一圈），再用填充盖回去，于是自动得到 1 像素描边。
  耳朵的轮廓与头顶那行**重叠一行**，避免"耳朵浮在头上"。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image

W, H = 32, 40
FRAMES = 8
OUT_DIR = Path("web-desktop2/src/assets/thinking-cat")

# --------------------------------------------------------------------------
# 调色
# --------------------------------------------------------------------------

PALETTE_DARK = {
    ".": (0, 0, 0, 0),
    "K": (16, 18, 28, 255),
    "F": (233, 239, 252, 255),
    "S": (176, 190, 228, 255),
    "E": (86, 216, 255, 255),
    "N": (255, 148, 192, 255),
    "C": (152, 118, 255, 255),
    "W": (255, 255, 255, 255),
}

PALETTE_LIGHT = {
    ".": (0, 0, 0, 0),
    "K": (52, 60, 88, 255),
    "F": (255, 255, 255, 255),
    "S": (210, 220, 240, 255),
    "E": (24, 92, 168, 255),
    "N": (230, 118, 166, 255),
    "C": (72, 108, 200, 255),
    "W": (255, 255, 255, 255),
}

# --------------------------------------------------------------------------
# 几何常量（改这里就能调外形）
# --------------------------------------------------------------------------

HEAD_L, HEAD_R = 8, 21          # 头：左右（含）→ 14 像素宽
HEAD_TOP = 12                   # 头顶那一行
HEAD_BOT = 23                   # 下巴那一行 → 12 像素高
BODY_L, BODY_R = 10, 19         # 身子比头窄
BODY_TOP = 25
BODY_BOT = 31
EAR_H = 5                       # 耳朵高（底边与头顶重叠一行）
EAR_W = 5                       # 耳朵底边宽


def blank() -> list[list[str]]:
    return [["." for _ in range(W)] for _ in range(H)]


def put(g, x: int, y: int, ch: str) -> None:
    if 0 <= x < W and 0 <= y < H:
        g[y][x] = ch


def rect(g, x0: int, y0: int, x1: int, y1: int, ch: str) -> None:
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            put(g, x, y, ch)


def triangle(g, base_x: int, base_y: int, width: int, height: int, ch: str) -> None:
    """底边贴在 base_y、朝上收尖的三角；width 为奇数时顶尖正好落在中间。"""
    half = width // 2
    for row in range(height):
        y = base_y - row
        span = half - (row * half) // max(1, height - 1)
        rect(g, base_x - span, y, base_x + span, y, ch)


def draw_ear(g, cx: int, base_y: int, inner_y: int) -> None:
    """耳朵：底边那一行**与头顶重叠**（base_y = 头顶轮廓那一行），所以不会浮在头上。"""
    triangle(g, cx, base_y, EAR_W, EAR_H, "K")
    triangle(g, cx, base_y - 1, EAR_W - 2, EAR_H - 1, "F")
    put(g, cx, inner_y, "N")


def draw_head(g, bob: int, nod: int) -> None:
    top = HEAD_TOP + bob + nod
    bot = HEAD_BOT + bob + nod
    # 先画耳朵（底边落在头顶轮廓行 top-1 上），再画头把中间那块盖平，两者自然连成一体
    for cx in ((HEAD_L + HEAD_R) // 2 - 3, (HEAD_L + HEAD_R) // 2 + 3):
        draw_ear(g, cx, top - 1, top - EAR_H + 2)
    rect(g, HEAD_L - 1, top - 1, HEAD_R + 1, bot + 1, "K")
    rect(g, HEAD_L, top, HEAD_R, bot, "F")
    # 两侧下颌压暗，让方头有点体积
    rect(g, HEAD_L, bot - 2, HEAD_L + 1, bot, "S")
    rect(g, HEAD_R - 1, bot - 2, HEAD_R, bot, "S")
    # 高光
    put(g, HEAD_L + 2, top + 1, "W")
    put(g, HEAD_L + 3, top + 1, "W")

    # 眼睛：2×2
    eye_y = top + 5
    for ex in (HEAD_L + 2, HEAD_R - 3):
        rect(g, ex, eye_y, ex + 1, eye_y + 1, "E")
    # 鼻子 + 嘴
    mid = (HEAD_L + HEAD_R) // 2
    put(g, mid, eye_y + 3, "N")
    put(g, mid - 1, eye_y + 4, "S")
    put(g, mid + 1, eye_y + 4, "S")


def draw_scarf(g, bob: int, phase: int) -> None:
    """围巾：脖子上一条 + 两条短飘带左右交替抬起。"""
    y = BODY_TOP - 1 + bob
    rect(g, HEAD_L, y, HEAD_R, y + 1, "K")
    rect(g, HEAD_L + 1, y, HEAD_R - 1, y, "C")

    # 左飘带
    lx = HEAD_L - 1
    if phase < 4:
        rect(g, lx - 2, y - 1, lx, y, "K")
        rect(g, lx - 1, y - 1, lx, y, "C")
    else:
        rect(g, lx - 2, y, lx, y + 1, "K")
        rect(g, lx - 1, y, lx, y + 1, "C")
    # 右飘带（与左反相）
    rx = HEAD_R + 1
    if phase < 4:
        rect(g, rx, y, rx + 2, y + 1, "K")
        rect(g, rx, y, rx + 1, y + 1, "C")
    else:
        rect(g, rx, y - 1, rx + 2, y, "K")
        rect(g, rx, y - 1, rx + 1, y, "C")


def draw_body(g, bob: int) -> None:
    top = BODY_TOP + bob
    bot = BODY_BOT + bob
    rect(g, BODY_L - 1, top, BODY_R + 1, bot, "K")
    rect(g, BODY_L, top, BODY_R, bot - 1, "F")
    rect(g, BODY_L + 1, bot - 3, BODY_R - 1, bot - 1, "S")
    rect(g, BODY_L + 1, bot - 2, BODY_R - 2, bot - 1, "F")


# 尾巴：一条连续的钩形折线（相邻点只差一格，保证连得上）
TAIL_PATH = [(20, 28), (21, 27), (22, 26), (23, 25), (24, 24), (25, 23), (25, 22)]


def draw_tail(g, bob: int, phase: int) -> None:
    """尾巴：锥形——根粗尖细，从屁股往右上钩起来。"""
    lift = 0 if phase < 4 else 1
    pts = [(x, y - bob + lift) for x, y in TAIL_PATH]
    for x, y in pts:                      # 描边：每个点外扩一圈
        rect(g, x - 1, y - 1, x + 1, y + 1, "K")
    n = len(pts)
    for i, (x, y) in enumerate(pts):      # 填充：越靠尖越细
        put(g, x, y, "F")
        put(g, x + 1, y, "F")
        if i < n - 2:                     # 根部两格厚
            put(g, x, y + 1, "F")
            put(g, x + 1, y + 1, "F")
        elif i < n - 1:
            put(g, x, y + 1, "F")


LEG_CYCLE = [
    (1, 1, 0, 0),
    (1, 0, 0, 1),
    (0, 0, 1, 1),
    (0, 1, 1, 0),
    (0, 1, 1, 0),
    (0, 0, 1, 1),
    (1, 0, 0, 1),
    (1, 1, 0, 0),
]
LEG_X = (11, 13, 16, 18)


def draw_legs(g, bob: int, phase: int) -> None:
    bot = BODY_BOT + bob
    for x, down in zip(LEG_X, LEG_CYCLE[phase], strict=True):
        if down:
            rect(g, x - 1, bot - 3, x + 1, bot, "K")
            rect(g, x, bot - 3, x + 1, bot - 1, "F")
        else:
            rect(g, x - 1, bot - 3, x + 1, bot - 2, "K")
            rect(g, x, bot - 3, x + 1, bot - 3, "F")


def render_frame(phase: int, palette: dict[str, tuple[int, int, int, int]]) -> Image.Image:
    g = blank()
    bob = 0 if phase % 2 == 0 else 1
    nod = 0 if phase < 4 else 1
    draw_tail(g, bob, phase)
    draw_legs(g, bob, phase)
    draw_body(g, bob)
    draw_scarf(g, bob, phase)
    draw_head(g, bob, nod)

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    px = img.load()
    for y in range(H):
        for x in range(W):
            px[x, y] = palette[g[y][x]]
    return img


def build_strip(palette: dict[str, tuple[int, int, int, int]]) -> Image.Image:
    strip = Image.new("RGBA", (W * FRAMES, H), (0, 0, 0, 0))
    for i in range(FRAMES):
        strip.paste(render_frame(i, palette), (i * W, 0))
    return strip


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="生成思考强度滑杆用的像素小猫精灵图")
    parser.add_argument("--preview", action="store_true", help="额外输出放大预览与逐帧检查图")
    parser.add_argument("--out", default=str(OUT_DIR), help="输出目录")
    args = parser.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    targets = [
        ("thinking-cat-8x.png", PALETTE_DARK),
        ("thinking-cat-light-8x.png", PALETTE_LIGHT),
    ]
    for name, palette in targets:
        strip = build_strip(palette)
        strip.save(out / name)
        print(f"写出 {out / name}  ({strip.width}x{strip.height}，{FRAMES} 帧)")
        if args.preview:
            strip.resize((strip.width * 8, strip.height * 8), Image.NEAREST).save(
                out / name.replace(".png", "-preview.png")
            )
            cell, cols = 8, 4
            sheet = Image.new(
                "RGBA",
                (W * cell * cols + (cols + 1) * 8, H * cell * 2 + 24),
                (24, 26, 34, 255),
            )
            for i in range(FRAMES):
                f = render_frame(i, palette).resize((W * cell, H * cell), Image.NEAREST)
                sheet.paste(f, ((i % cols) * (W * cell + 8) + 8, (i // cols) * (H * cell + 8) + 8), f)
            sheet.save(out / name.replace(".png", "-sheet.png"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
