"""把 crudelab 字標外框化（字形轉成向量路徑），輸出 SVG / PNG，不依賴任何字體。

字體：Nunito（SIL Open Font License 1.1）。字標 crude = Nunito 800，l·b = Nunito 300，a = 油滴。

字體檔不進倉庫。重新生成前把 Nunito 的 300 和 800 兩個字重放進 brand/fonts/
（命名 Nunito-300.ttf、Nunito-800.ttf），然後：
    pip install fonttools cairosvg && python brand/build_logo.py
生成後把 crudelab-icon.svg 複製成 src/crudelab/web/favicon.svg，
crudelab-lockup-*.svg、icon@180/@512.png 複製到 src/crudelab/web/brand/。
"""

from __future__ import annotations

import json
from pathlib import Path

import cairosvg
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont

HERE = Path(__file__).parent
OUT = HERE

TRACK = -25  # 字距 -0.025em（1000 units/em）
BASE = 760  # 基線在畫布中的 y（字形向上為正，畫布向下為正）
ASC = 760  # 以 d/b/l 升部 ~714 + 留白

# 油滴：主體寬 340、底部 -10（與字母圓弧下沉一致）、頂端約 585；左右邊距各 70
DROP_W, DROP_BOTTOM, DROP_SIDE = 340.0, -10.0, 70.0
DROP_SRC = "M20,0 C20,0 4,22 4,36 C4,46.5 11.2,56 20,56 C28.8,56 36,46.5 36,36 C36,22 20,0 20,0 Z"
GLOSS_SRC = "M11,38 C11,33 13.5,28.5 16,25.5"

COLORS = {
    "light": {"crude": "#1d1d1f", "lab": "#6e6e73", "drop": ("#5b3a22", "#0d0805")},
    "dark": {"crude": "#f5f5f7", "lab": "#a1a1a6", "drop": ("#ffc24a", "#e0791c")},
}


def glyph_path(font: TTFont, ch: str, x: float) -> tuple[str, float]:
    gs = font.getGlyphSet()
    name = font.getBestCmap()[ord(ch)]
    pen = SVGPathPen(gs)
    gs[name].draw(TransformPen(pen, (1, 0, 0, -1, x, BASE)))
    return pen.getCommands(), gs[name].width


def wordmark_paths() -> dict[str, object]:
    bold, light = TTFont(HERE / "fonts/Nunito-800.ttf"), TTFont(HERE / "fonts/Nunito-300.ttf")
    x = 0.0
    crude = []
    for ch in "crude":
        d, adv = glyph_path(bold, ch, x)
        crude.append(d)
        x += adv + TRACK
    lab = []
    d, adv = glyph_path(light, "l", x)
    lab.append(d)
    x += adv + TRACK
    s = DROP_W / 32.0
    drop_x = x + DROP_SIDE - 4 * s
    drop_y = BASE - DROP_BOTTOM - 56 * s
    drop_tf = f"translate({drop_x:.2f} {drop_y:.2f}) scale({s:.4f})"
    x += DROP_SIDE + DROP_W + DROP_SIDE + TRACK
    d, adv = glyph_path(light, "b", x)
    lab.append(d)
    x += adv
    return {"crude": " ".join(crude), "lab": " ".join(lab), "drop_tf": drop_tf, "width": x}


def wordmark_group(wm: dict[str, object], theme: str, pid: str) -> tuple[str, str]:
    c = COLORS[theme]
    top, bot = c["drop"]
    defs = (
        f'<linearGradient id="{pid}" x1="0.3" y1="0" x2="0.7" y2="1">'
        f'<stop offset="0" stop-color="{top}"/><stop offset="1" stop-color="{bot}"/></linearGradient>'
    )
    body = (
        f'<path fill="{c["crude"]}" d="{wm["crude"]}"/>'
        f'<path fill="{c["lab"]}" d="{wm["lab"]}"/>'
        f'<g transform="{wm["drop_tf"]}"><path fill="url(#{pid})" d="{DROP_SRC}"/>'
        f'<path d="{GLOSS_SRC}" fill="none" stroke="#fff" stroke-opacity="0.55" stroke-width="3" '
        f'stroke-linecap="round"/></g>'
    )
    return defs, body


# C3 · 分層 圖標（100×100）
ICON_DEFS = (
    '<linearGradient id="ib" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffffff"/>'
    '<stop offset="1" stop-color="#efeae2"/></linearGradient>'
    '<clipPath id="ic"><path d="M42,22 L42,42 L26,72 C24,76 26.5,80 31,80 L69,80 C73.5,80 76,76 74,72 '
    'L58,42 L58,22 Z"/></clipPath>'
)
ICON_BODY = (
    '<path fill="url(#ib)" d="M50,0 C88,0 100,12 100,50 C100,88 88,100 50,100 C12,100 0,88 0,50 '
    'C0,12 12,0 50,0 Z"/>'
    '<g clip-path="url(#ic)"><rect x="18" y="46" width="66" height="9" fill="#ffd66b"/>'
    '<rect x="18" y="55" width="66" height="9" fill="#f29a2e"/>'
    '<rect x="18" y="64" width="66" height="8" fill="#8a4a1f"/>'
    '<rect x="18" y="72" width="66" height="18" fill="#160c06"/></g>'
    '<path d="M42,22 L42,42 L26,72 C24,76 26.5,80 31,80 L69,80 C73.5,80 76,76 74,72 L58,42 L58,22" '
    'fill="none" stroke="#1d1d1f" stroke-width="4.2" stroke-linejoin="round" stroke-linecap="round"/>'
    '<path d="M38,22 L62,22" stroke="#1d1d1f" stroke-width="4.2" stroke-linecap="round"/>'
)


def svg(w: float, h: float, defs: str, body: str, title: str, bg: str | None = None) -> str:
    rect = f'<rect width="{w:.0f}" height="{h:.0f}" fill="{bg}"/>' if bg else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:.0f} {h:.0f}" width="{w:.0f}" '
        f'height="{h:.0f}" role="img" aria-label="{title}"><title>{title}</title>'
        f"<defs>{defs}</defs>{rect}{body}</svg>\n"
    )


def write(name: str, content: str, png_heights: tuple[int, ...] = ()) -> None:
    (OUT / f"{name}.svg").write_text(content, encoding="utf-8")
    for h in png_heights:
        cairosvg.svg2png(bytestring=content.encode(), write_to=str(OUT / f"{name}@{h}.png"), output_height=h)


def main() -> None:
    wm = wordmark_paths()
    W = float(wm["width"])  # type: ignore[arg-type]
    pad = 60.0
    meta = {"wordmark_width_units": round(W, 1)}

    for theme in ("light", "dark"):
        d, b = wordmark_group(wm, theme, f"drop-{theme}")
        # 字形墨跡範圍 y 46（升部頂）~ 771（圓弧下沉），上下各留 46
        body = f'<g transform="translate({pad} 0)">{b}</g>'
        content = svg(W + 2 * pad, 817, d, body, "crudelab")
        write(f"crudelab-wordmark-{theme}", content, (64, 128, 256))

        # 橫式組合：圖標 + 字標；圖標高 = 1.18 × 字高（約與升部到基線對齊居中）
        icon = 1000.0
        gap = 260.0
        scale_wm = 0.8  # 字標升部高約為圖標高的 57%
        total_w = icon + gap + W * scale_wm
        lock_h = icon
        wm_y = lock_h / 2 - (46 + BASE) / 2 * scale_wm  # 升部頂到基線的區段垂直居中
        body = (
            f'<g transform="scale(10)">{ICON_BODY}</g>'
            f'<g transform="translate({icon + gap} {wm_y:.1f}) scale({scale_wm})">{b}</g>'
        )
        bg = "#1d1d1f" if theme == "dark" else None
        content = svg(total_w, lock_h, ICON_DEFS + d, body, "crudelab", None)
        write(f"crudelab-lockup-{theme}", content, (64, 128, 256))
        if bg:
            meta["dark_background"] = bg

    icon_svg = svg(100, 100, ICON_DEFS, ICON_BODY, "crudelab")
    write("crudelab-icon", icon_svg, (16, 32, 64, 180, 512, 1024))

    print(json.dumps(meta))


if __name__ == "__main__":
    main()
