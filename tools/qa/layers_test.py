"""Prove the stage layers are independent.

The whole point of the layered stage is that changing the character must not
change the scene. This test drives the real UI, freezes the background loop and
checks three invariants:

1. swapping the emotion sprite changes **nothing outside the character alpha**;
2. a pose swap without a camera move still touches only the character area;
3. the camera rescale stays OFF (it was removed on 2026-10-20 because rescaling the
   whole stage every frame is what made the picture "flash"); what must hold is that
   plate and sprite still live in the same .stage, so any future integer-pixel pan
   cannot drift the two layers apart;
4. scene=lights_dim really switches the environment layer to the dimmed look.

Why "outside the alpha" instead of the old "less than 2% of the frame": the 12
states are generated one by one, so the *shading across her body* differs
slightly between two states even at the same pose. That difference is legitimate
(it is the character layer), while the room behind her must be pixel-identical.
The old threshold conflated the two and would either fail on good assets or pass
on a broken layer.

Usage: python tools/qa/layers_test.py [base_url]
"""
import json
import sys
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "production" / "reports" / "layers"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787"
VIEW = (780, 1688)          # 390x844 @ device_scale_factor=2
# 与 style.css 里 .layer/.sprite 的 object-fit:cover + object-position:50% 46% 保持一致
OBJ_POS_Y = 0.46


def card_assets() -> dict:
    with urllib.request.urlopen(BASE + "/api/card", timeout=10) as r:
        return json.loads(r.read())["assets"]


def mask_on_screen(webp_path: Path, view=VIEW, origin=(864, 1536)) -> np.ndarray:
    """把角色贴片的 alpha 映射到截图像素坐标（object-fit: cover + object-position）。"""
    sw, sh = view
    iw, ih = origin
    scale = max(sw / iw, sh / ih)
    rw, rh = int(round(iw * scale)), int(round(ih * scale))
    alpha = Image.open(webp_path).convert("RGBA").getchannel("A").resize((rw, rh), Image.BILINEAR)
    left = int(round((rw - sw) * 0.5))
    top = int(round((rh - sh) * OBJ_POS_Y))
    crop = alpha.crop((left, top, left + sw, top + sh))
    return np.asarray(crop, dtype=np.uint8) > 96


def diff_outside(a, b, mask, threshold=8.0):
    """只统计 mask 之外的改动（mask=True 表示角色像素，允许变）。"""
    d = np.abs(a - b).mean(axis=2)
    changed = (d > threshold) & (~mask)
    frac = float(changed.sum()) / max(1, int((~mask).sum()))
    if not changed.any():
        return frac, None
    ys, xs = np.nonzero(changed)
    h, w = changed.shape
    return frac, (xs.min() / w, ys.min() / h, xs.max() / w, ys.max() / h)


def grab(page, name):
    path = OUT / f"{name}.png"
    page.screenshot(path=str(path))
    return np.asarray(Image.open(path).convert("RGB"), dtype=np.float32)


def diff(a, b, threshold=8.0):
    d = np.abs(a - b).mean(axis=2)
    changed = d > threshold
    frac = float(changed.sum()) / changed.size
    if not changed.any():
        return frac, None
    ys, xs = np.nonzero(changed)
    h, w = changed.shape
    return frac, (xs.min() / w, ys.min() / h, xs.max() / w, ys.max() / h)


FREEZE = """() => {
  const v = document.getElementById('bgLoop');
  if (v) { v.pause(); v.style.display = 'none'; }
  document.querySelectorAll('.sprite, .char-layer, .layer').forEach((e) => {
    e.style.animation = 'none';
    e.style.transition = 'none';
  });
  document.getElementById('rain')?.classList.remove('on');
  // 机位推拉（stage transform）是另一条时间轴：入场动作可能留下 push 还没走完，
  // 比较背景时必须先归位并冻住 .stage 的 1200ms transition，否则"背景在动"
  // 会被误报成"图层串位"。
  const stage = document.getElementById('stage');
  if (stage) { stage.style.transition = 'none'; stage.dataset.cam = ''; }
  // 关掉 rAF 持续微动：呼吸会把人物轮廓整体移动 ~5px，那是动画，不是图层串位。
  // 这里要断言的是「换表情时背景像素一个都不变」，所以先把动画冻住再比。
  if (window.__mira && window.__mira.setMotion) window.__mira.setMotion(false);
  // 氛围叠加层（颗粒 / 暗角 / 车灯横扫 / 闪电 / 雨痕）都是程序动画：只要它们在跑，
  // 两张截图就不可能逐像素相同。比较图层时必须先关掉，否则测的是"噪声"。
  document.querySelectorAll('#grain, #dim, #sweep, #flash, #rain').forEach((e) => {
    e.style.display = 'none';
  });
  // hide the HUD / chips / dock so only the stage is compared
  document.querySelectorAll('.hud, .action-chip, .bottom, #toast, #recBar').forEach((e) => {
    e.style.visibility = 'hidden';
  });
  return true;
}"""

HIDE_CHAR = "() => { document.querySelector('.char-layer').style.visibility = 'hidden'; }"
SHOW_CHAR = "() => { document.querySelector('.char-layer').style.visibility = ''; }"


def footprint(with_char: np.ndarray, plate_only: np.ndarray, dilate: int = 13) -> np.ndarray:
    """角色在**屏幕上**真正占的像素（含投影/柔边），由「显示 vs 隐藏角色层」两张图求出来。

    为什么不用素材 alpha：v8 贴片带一圈很淡的投影（alpha 30–60），
    alpha>96 的 mask 会把投影漏在外面，测出来就是「角色之外变了 8%」——那是素材的柔边，
    不是图层串位。用渲染差集做 footprint，测的是"她实际盖住了哪些像素"，才是要守的东西。
    """
    d = np.abs(with_char - plate_only).mean(axis=2)
    m = Image.fromarray(((d > 2.0) * 255).astype(np.uint8))
    m = m.filter(ImageFilter.MaxFilter(dilate))
    return np.asarray(m) > 0


def grab_pair(page, name: str):
    """同一状态下取两张：带角色 / 只有背景板。"""
    with_char = grab(page, name)
    page.evaluate(HIDE_CHAR)
    page.wait_for_timeout(60)
    plate = grab(page, name + "_plate")
    page.evaluate(SHOW_CHAR)
    page.wait_for_timeout(60)
    return with_char, plate


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ok = True
    assets = card_assets()
    char = {k: ROOT / "app" / "static" / v.replace("/static/", "", 1) for k, v in
            assets["char"].items()}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--mute-audio"])
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                  locale="zh-CN")
        page = ctx.new_page()
        page.goto(BASE, wait_until="networkidle")
        page.click("#startBtn")
        # 开场是「只做动作、不说话」的静默开场（server: fixes=["开场不主动搭话"]），
        # 所以 state 只在 thinking↔idle 之间闪 58ms —— 用 state 变化当「开场完成」的
        # 信号是错的：等待函数按 rAF 轮询，这个窗口会漏掉。以 sid + 首轮 done 为准。
        page.wait_for_function("() => window.__mira && window.__mira.S.sid", timeout=25000)
        page.wait_for_timeout(1200)
        page.wait_for_function(
            "() => window.__mira && window.__mira.S.state === 'idle'", timeout=25000)
        page.wait_for_timeout(700)
        page.evaluate(FREEZE)
        page.wait_for_timeout(300)
        base, base_plate = grab_pair(page, "01_neutral")

        # 1. 换表情：背景板必须逐像素不变；角色层的变化必须都落在她自己的覆盖区里
        page.evaluate("() => window.__mira.applyDirective({emotion: 'surprised'})")
        page.wait_for_timeout(800)
        frame, frame_plate = grab_pair(page, "02_surprised")
        bg = float((np.abs(base_plate - frame_plate).mean(axis=2) > 2.0).mean())
        fp = footprint(base, base_plate) | footprint(frame, frame_plate)
        frac, box = diff_outside(base, frame, fp)
        status = "PASS" if frac < 0.005 and bg < 0.0005 else "FAIL"
        ok &= status == "PASS"
        print(f"[{status}] emotion swap: 背景板变了 {bg*100:.4f}%（阈值 0.05%），"
              f"角色覆盖区之外变了 {frac*100:.3f}%（阈值 0.5%）bbox={box}", flush=True)

        # 2. pose without camera: 同样只允许角色层变化
        page.evaluate("() => window.__mira.applyDirective({action: 'lean_in'})")
        page.wait_for_timeout(800)
        frame, frame_plate = grab_pair(page, "03_lean_in")
        bg = float((np.abs(base_plate - frame_plate).mean(axis=2) > 2.0).mean())
        fp2 = fp | footprint(frame, frame_plate)
        frac, box = diff_outside(base, frame, fp2)
        status = "PASS" if frac < 0.005 and bg < 0.0005 else "FAIL"
        ok &= status == "PASS"
        print(f"[{status}] pose swap (no camera): 背景板变了 {bg*100:.4f}%，"
              f"角色之外变了 {frac*100:.3f}%（阈值 0.5%）bbox={box}", flush=True)

        # 3. 镜头/环境变化：2026-10-20 起推拉（scale）刻意取消（整台舞台逐帧重采样
        #    就是"说话时闪"的根源），所以这里断言的是**没有 transform**；真正要守的是
        #    两层仍在同一个 .stage 里（将来若加整数像素平移，两层也不会错位），
        #    以及 lights_dim 真的把环境层切到暗场。
        page.evaluate("() => window.__mira.applyDirective({scene: 'window_close'})")
        page.wait_for_timeout(1400)
        frame = grab(page, "04_window_close")
        cam = page.evaluate(
            """() => {
              const stage = document.getElementById('stage');
              const t = getComputedStyle(stage).transform;
              const inStage = (sel) => !!stage.querySelector(sel);
              return {transform: t, plateInside: inStage('#plateA'),
                      spriteInside: inStage('#charA')};
            }"""
        )
        no_scale = cam["transform"] in ("none", "matrix(1, 0, 0, 1, 0, 0)")
        status = "PASS" if no_scale and cam["plateInside"] and cam["spriteInside"] else "FAIL"
        ok &= status == "PASS"
        print(f"[{status}] camera off (no rescale): stage transform={cam['transform'][:42]} "
              f"plate/sprite share stage={cam['plateInside']}/{cam['spriteInside']}", flush=True)

        # 4. 环境变化：lights_dim 必须真的切到暗场（背景/环境变化）
        page.evaluate("() => window.__mira.applyDirective({scene: 'lights_dim'})")
        page.wait_for_timeout(1000)
        dim_on = page.evaluate(
            "() => document.getElementById('dim').classList.contains('lights-dim')"
        )
        status = "PASS" if dim_on else "FAIL"
        ok &= status == "PASS"
        print(f"[{status}] lights_dim: #dim lights-dim class={dim_on}", flush=True)

        ctx.close()
        browser.close()
    print(f"[png] {OUT}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
