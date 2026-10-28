"""把 assets/turn 里验收通过的新素材集成进 app/static（webp），并备份旧素材。

用法：
  .venv\\Scripts\\python.exe tools\\gen\\integrate_turn.py --dry-run   # 只看会做什么
  .venv\\Scripts\\python.exe tools\\gen\\integrate_turn.py            # 真替换

规则：
- 只替换**存在**的文件，缺哪张就跳过哪张（并打印出来），不会把缺图写成 404。
- 角色贴片保留 alpha（RGBA webp）；场景板转 RGB。
- 旧素材先整体备份到 assets/legacy/<时间戳>/，随时可回滚。
- 名字必须和卡包 assets 段一致，卡包不用改；如果新增了动作贴片，记得同步 cards/*/card.yaml。
"""

from __future__ import annotations

import argparse
import shutil
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter
import numpy as np


def night_grade(img: Image.Image, strength: float = 1.0) -> Image.Image:
    """统一调色：把「傍晚偏亮」的素材压成「23:47 快打烊」的夜景。

    为什么要这一步：生成的 plate 整体偏亮偏暖，读不出「快要关灯」；
    而角色贴片是另一路生成的，亮度和色温跟 plate 不一致，叠上去就是「两张皮」。
    对 plate 和所有贴片**用同一套调色**，是最便宜也最有效的统一手段。

    做法：压亮度 → 暗部偏冷（蓝）、高光保留暖 → 四角压暗（vignette）。
    """
    if strength <= 0:
        return img
    alpha = None
    if img.mode == "RGBA":
        alpha = img.getchannel("A")
        img = img.convert("RGB")
    else:
        img = img.convert("RGB")

    # 1) 亮度 / 对比
    img = ImageEnhance.Brightness(img).enhance(1 - 0.13 * strength)
    img = ImageEnhance.Contrast(img).enhance(1 + 0.05 * strength)

    # 2) 暗部偏冷、亮部偏暖：按亮度做通道权重
    r, g, b = img.split()
    lum = img.convert("L")
    cool = lum.point(lambda v: int(max(0, (150 - v)) * 2.0))       # 暗部权重
    r = ImageChops.subtract(r, cool.point(lambda v: int(v * 0.10 * strength)))
    b = ImageChops.add(b, cool.point(lambda v: int(v * 0.16 * strength)))
    img = Image.merge("RGB", (r, g, b))

    # 3) vignette：中心留亮，四角压暗
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(mask)
    d.ellipse([-w * 0.25, -h * 0.18, w * 1.25, h * 1.18], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(max(w, h) // 8))
    shade = ImageEnhance.Brightness(img).enhance(1 - 0.22 * strength)
    img = Image.composite(img, shade, mask)

    if alpha is not None:
        img = img.convert("RGBA")
        img.putalpha(alpha)
    return img


def clean_alpha(img: Image.Image, min_keep: float = 0.02) -> Image.Image:
    """清掉抠图残留：细线鬼影、孤立小碎块。

    实测：合成中近景时，compose 会把前景的杯子/桌面轻微重画，diff 于是在杯子位置留下
    一圈半透明的「鬼影」。它的 alpha 只有几十，但是会在页面上叠出第二对杯子。
    做法：alpha 开运算（先腐蚀再膨胀）去掉细结构 → 只留最大连通块 → 羽化。
    """
    a = img.getchannel("A")
    m = a.point(lambda v: 255 if v > 110 else 0)
    m = m.filter(ImageFilter.MinFilter(7)).filter(ImageFilter.MaxFilter(9))
    mask = np.array(m) > 127
    # 最大连通域（1/4 分辨率上做，够快）
    h, w = mask.shape
    small = np.array(Image.fromarray((mask * 255).astype(np.uint8))
                     .resize((w // 4, h // 4), Image.BILINEAR)) > 60
    seen = np.zeros_like(small, dtype=bool)
    best, best_size = None, 0
    H, W = small.shape
    for y in range(H):
        row = small[y]
        for x in range(W):
            if not row[x] or seen[y, x]:
                continue
            stack = [(y, x)]
            seen[y, x] = True
            comp = []
            while stack:
                cy, cx = stack.pop()
                comp.append((cy, cx))
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = cy + dy, cx + dx
                    if 0 <= ny < H and 0 <= nx < W and small[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if len(comp) > best_size:
                best_size, best = len(comp), comp
    if best and best_size > 12:
        keep = np.zeros((H, W), dtype=np.uint8)
        ys = np.array([p[0] for p in best])
        xs = np.array([p[1] for p in best])
        keep[ys, xs] = 255
        keep = np.array(Image.fromarray(keep).resize((w, h), Image.BILINEAR)) > 40
        mask = mask & keep
    out = Image.fromarray((mask * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(1.2))
    cleaned = img.convert("RGB").copy()
    cleaned.putalpha(out)
    return cleaned


def convert(src: Path, dst: Path, alpha: bool, grade: float = 1.0) -> tuple[int, int]:
    img = Image.open(src)
    img = img.convert("RGBA" if alpha else "RGB")
    if alpha:
        img = clean_alpha(img)
    if grade > 0:
        img = night_grade(img, grade)
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst, "WEBP", quality=88, method=6)
    return img.size

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "assets" / "turn"
DST = ROOT / "app" / "static" / "assets"
LEGACY = ROOT / "assets" / "legacy"

# 新素材 -> 线上路径（相对 app/static/assets）
PLATES = {
    "plate/cafe_night.png": "plate/cafe_night.webp",
    "plate/cafe_window_close.png": "plate/cafe_window_close.webp",
}
STATES = {
    # 6 个表情
    "neutral": "char/neutral.webp",
    "smile": "char/smile.webp",
    "worried": "char/worried.webp",
    "surprised": "char/surprised.webp",
    "thinking": "char/thinking.webp",
    "listen": "char/listen.webp",
    # 动作
    "look_window": "char/look_window.webp",
    "sip_coffee": "char/sip_coffee.webp",
    "lean_in": "char/lean_in.webp",
    "show_photo": "char/show_photo.webp",
    "stand": "char/stand.webp",
    # 2026-10-20 补齐：这两张以前压根没有，客户端靠 POSE_ALIAS 硬映射
    "look_door": "char/look_door.webp",
    "check_phone": "char/check_phone.webp",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--grade", type=float, default=1.0,
                    help="统一夜景调色强度（0 关闭，1 默认，1.4 更暗）")
    a = ap.parse_args()

    jobs: list[tuple[Path, Path, bool]] = []
    for rel, out in PLATES.items():
        p = SRC / rel
        if p.exists():
            jobs.append((p, DST / out, False))
    for name, out in STATES.items():
        p = SRC / "state" / f"{name}.png"
        if p.exists():
            jobs.append((p, DST / out, True))

    missing = [f"state/{n}.png" for n in STATES if not (SRC / "state" / f"{n}.png").exists()]
    print(f"源目录: {SRC}")
    print(f"可集成的文件: {len(jobs)} 个；缺失: {len(missing)} 个")
    for m in missing:
        print("  [缺] ", m)
    if not jobs:
        print("没有可集成的素材，退出。")
        return 1
    if a.dry_run:
        for src, dst, alpha in jobs:
            print(f"  [would] {src.relative_to(SRC)} -> {dst.relative_to(ROOT)}"
                  f"{'  (RGBA)' if alpha else ''}")
        return 0

    if not a.no_backup:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup = LEGACY / stamp
        backup.mkdir(parents=True, exist_ok=True)
        if DST.exists():
            shutil.copytree(DST, backup / "assets", dirs_exist_ok=True)
        print(f"[backup] 旧素材 -> {backup}")

    total_kb = 0
    for src, dst, alpha in jobs:
        size = convert(src, dst, alpha, grade=a.grade)
        kb = dst.stat().st_size // 1024
        total_kb += kb
        print(f"  [ok] {src.relative_to(SRC)} -> {dst.relative_to(ROOT)} {size[0]}x{size[1]} {kb}KB")
    print(f"[done] {len(jobs)} 个文件，{total_kb} KB（统一调色 grade={a.grade}）")
    print("下一步：重启服务（静态文件有缓存），再看一眼页面；卡包 assets 段不用改（名字对齐）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
