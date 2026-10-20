"""Headless QA pass for the Mira scene: drive the real UI, capture screenshots.

Usage: python tools/qa/shots.py [base_url]

Requires: playwright + chromium (python -m playwright install chromium),
and the backend already running (start.ps1 / uvicorn).
"""
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(r"D:\interview\mira")
SHOTS = ROOT / "production" / "reports" / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787"


def log(*a):
    print(*a, flush=True)


def wait_idle(page, timeout=25):
    page.wait_for_function(
        "() => window.__mira && window.__mira.S.state === 'idle'", timeout=timeout * 1000
    )


def send(page, text):
    page.fill("#textInput", text)
    page.click("#sendBtn")
    page.wait_for_selector("#caption:not(:empty)", timeout=15000)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"],
        )
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                  locale="zh-CN")
        page = ctx.new_page()
        page.on("console", lambda m: log("  [console]", m.type, m.text[:200]) if m.type == "error" else None)
        page.on("pageerror", lambda e: log("  [pageerror]", str(e)[:300]))

        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(900)
        page.screenshot(path=str(SHOTS / "01_gate.png"))
        metrics = page.evaluate(
            """() => {
              const r = (s) => { const e = document.querySelector(s); if (!e) return null;
                const b = e.getBoundingClientRect(); return {x: b.x, w: b.width, right: b.right}; };
              return {gateP: r('.gate p'), gateCard: r('.gate-card'), gate: r('.gate'),
                      vw: window.innerWidth, app: r('#app'), overflowX: document.documentElement.scrollWidth};
            }"""
        )
        log("layout:", metrics)

        page.click("#startBtn")
        page.wait_for_function("() => window.__mira.S.state !== 'idle'", timeout=20000)
        page.wait_for_timeout(1200)
        page.screenshot(path=str(SHOTS / "02_opening.png"))
        log("after open state:", page.evaluate("() => window.__mira.S.state"))

        wait_idle(page)
        page.screenshot(path=str(SHOTS / "03_idle.png"))
        log("directive/audio ok, state:", page.evaluate("() => window.__mira.S.state"))

        send(page, "你是在等人吗？")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(SHOTS / "04_turn2.png"))
        wait_idle(page)

        send(page, "刚才是不是打雷了？")
        page.wait_for_timeout(1400)
        page.screenshot(path=str(SHOTS / "05_thunder_event.png"))
        log("state during event:", page.evaluate("() => window.__mira.S.state"),
            "event:", page.evaluate("() => window.__mira.S.scene"))
        try:
            page.wait_for_selector("#cinema.show", timeout=8000)
            page.wait_for_timeout(900)
            page.screenshot(path=str(SHOTS / "07_cinema.png"))
            log("cinema layer visible:",
                page.evaluate("() => ({ready: document.getElementById('cinema').classList.contains('still-ready'),"
                              " src: (document.getElementById('eventStill').currentSrc || '').split('/').pop()})"))
        except Exception as exc:  # noqa: BLE001
            log("no cinema layer:", type(exc).__name__)
        wait_idle(page)

        page.click("#debugBtn")
        page.wait_for_timeout(1200)
        page.screenshot(path=str(SHOTS / "06_debug.png"))
        page.click("#debugClose")

        log("session:", page.evaluate("() => window.__mira.S.sid"),
            "turns:", page.evaluate("() => window.__mira.S.turns"),
            "errors:", page.evaluate("() => window.__mira.S.errors"))
        ctx.close()
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
