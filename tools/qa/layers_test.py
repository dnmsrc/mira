"""Prove the stage layers are independent.

The whole point of the layered stage is that changing the character must not
change the scene. This test drives the real UI, freezes the background loop and
checks three invariants:

1. swapping the emotion sprite changes < 2% of the frame (the room is untouched);
2. a pose swap without a camera move stays inside the character area;
3. a camera move transforms the whole .stage (background + character share one
   transform), so the two layers can never drift apart.

Usage: python tools/qa/layers_test.py [base_url]
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(r"D:\interview\mira")
OUT = ROOT / "production" / "reports" / "layers"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787"

FREEZE = """() => {
  const v = document.getElementById('bgLoop');
  if (v) { v.pause(); v.style.display = 'none'; }
  document.querySelectorAll('.sprite, .char-layer').forEach((e) => {
    e.style.animation = 'none';
    e.style.transition = 'none';
  });
  // hide the HUD / chips / dock so only the stage is compared
  document.querySelectorAll('.hud, .action-chip, .bottom, #toast').forEach((e) => {
    e.style.visibility = 'hidden';
  });
  return true;
}"""


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


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ok = True
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--mute-audio"])
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                  locale="zh-CN")
        page = ctx.new_page()
        page.goto(BASE, wait_until="networkidle")
        page.click("#startBtn")
        page.wait_for_function("() => window.__mira && window.__mira.S.state !== 'idle'", timeout=25000)
        page.wait_for_function("() => window.__mira && window.__mira.S.state === 'idle'", timeout=25000)
        page.wait_for_timeout(700)
        page.evaluate(FREEZE)
        page.wait_for_timeout(300)
        base = grab(page, "01_neutral")

        # 1. emotion: only the character layer may change
        page.evaluate("() => window.__mira.applyDirective({emotion: 'surprised'})")
        page.wait_for_timeout(800)
        frame = grab(page, "02_surprised")
        frac, box = diff(base, frame)
        status = "PASS" if frac < 0.02 else "FAIL"
        ok &= status == "PASS"
        print(f"[{status}] emotion swap: {frac*100:.2f}% of frame changed "
              f"(background must stay < 2%) bbox={box}", flush=True)

        # 2. pose without camera: changes stay around the character
        page.evaluate("() => window.__mira.applyDirective({action: 'show_photo'})")
        page.wait_for_timeout(800)
        frame = grab(page, "03_show_photo")
        frac, box = diff(base, frame)
        inside = box is not None and box[0] > 0.05 and box[2] < 0.95 and box[3] < 0.85
        status = "PASS" if frac < 0.30 and inside else "FAIL"
        ok &= status == "PASS"
        print(f"[{status}] pose swap (no camera): {frac*100:.1f}% changed, bbox={box}", flush=True)

        # 3. camera: one transform on the stage that contains both layers
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
        status = "PASS" if cam["transform"] not in ("none", "matrix(1, 0, 0, 1, 0, 0)") \
            and cam["plateInside"] and cam["spriteInside"] else "FAIL"
        ok &= status == "PASS"
        print(f"[{status}] camera: stage transform={cam['transform'][:42]} "
              f"plate/sprite share stage={cam['plateInside']}/{cam['spriteInside']}", flush=True)

        ctx.close()
        browser.close()
    print(f"[png] {OUT}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
