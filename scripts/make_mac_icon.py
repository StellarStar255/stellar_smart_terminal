#!/usr/bin/env python3
"""从满幅源图生成符合 macOS 规范的 Dock 图标（padded PNG + icns）。

    python3 scripts/make_mac_icon.py

macOS 图标画布必须自带透明留白：1024 画布里图形只占 ~80.5%（824×824
居中），圆角按 Apple squircle 比例 ≈ 图形边长的 22.5%。满幅出血的图标
在 Dock 里会显得比所有邻居大一圈、且圆角偏方。

产出：
- assets/smart_terminal_icon_mac.png  (2048 画布、1648 图形居中)
- assets/smart_terminal.icns          (iconutil 生成，直接提交进仓库；
  build.sh 仅在 icns 缺失时重新生成)

Windows .ico / Linux hicolor 用满幅版是各自平台的正确做法，不动。
"""
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "smart_terminal.png"
PADDED = ROOT / "assets" / "smart_terminal_icon_mac.png"
ICNS = ROOT / "assets" / "smart_terminal.icns"

CANVAS = 2048
ARTWORK_RATIO = 824 / 1024        # Apple 图标网格：图形占画布 80.47%
CORNER_RATIO = 185.4 / 824        # Apple squircle 圆角 ≈ 图形边长 22.5%
SS = 4                            # 圆角遮罩超采样倍数（边缘抗锯齿）


def make_padded_master() -> Image.Image:
    art_size = round(CANVAS * ARTWORK_RATIO)      # 1648
    radius = round(art_size * CORNER_RATIO)       # ~371

    art = Image.open(SRC).convert("RGBA").resize(
        (art_size, art_size), Image.LANCZOS)

    # 按 Apple 比例重裁圆角（原图圆角只有 ~11%，偏方）；超采样画遮罩再缩回
    mask_big = Image.new("L", (art_size * SS, art_size * SS), 0)
    ImageDraw.Draw(mask_big).rounded_rectangle(
        (0, 0, art_size * SS - 1, art_size * SS - 1),
        radius=radius * SS, fill=255)
    mask = mask_big.resize((art_size, art_size), Image.LANCZOS)
    art.putalpha(Image.composite(
        art.getchannel("A"), Image.new("L", art.size, 0), mask))

    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    offset = (CANVAS - art_size) // 2
    canvas.paste(art, (offset, offset), art)
    return canvas


def build_icns(master: Image.Image):
    with tempfile.TemporaryDirectory() as td:
        iconset = Path(td) / "smart_terminal.iconset"
        iconset.mkdir()
        for sz in (16, 32, 128, 256, 512):
            master.resize((sz, sz), Image.LANCZOS).save(
                iconset / f"icon_{sz}x{sz}.png")
            master.resize((sz * 2, sz * 2), Image.LANCZOS).save(
                iconset / f"icon_{sz}x{sz}@2x.png")
        subprocess.run(["iconutil", "-c", "icns", str(iconset),
                        "-o", str(ICNS)], check=True)


def main() -> int:
    if sys.platform != "darwin":
        print("iconutil 仅 macOS 可用", file=sys.stderr)
        return 1
    master = make_padded_master()
    master.save(PADDED)
    build_icns(master)
    print(f"written: {PADDED.relative_to(ROOT)}, {ICNS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
