"""End-to-end microphone test with a fake audio device.

Chromium is fed a real 16 kHz mono WAV of Chinese speech, so this exercises the
same path a human uses: getUserMedia -> MediaRecorder -> /api/stt -> turn.

Run: .venv\\Scripts\\python.exe tools\\qa\\mic_test.py
"""
import sys
from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8787/"
WAV = r"D:\interview\mira\app\static\assets\audio\sample_user.wav"
SHOT = r"D:\interview\mira\docs\review\demo\mic_test.png"
HOLD_MS = 3200

logs, reqs = [], []

with sync_playwright() as p:
    b = p.chromium.launch(
        headless=True,
        args=[
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            "--use-file-for-fake-audio-capture=" + WAV,
            "--autoplay-policy=no-user-gesture-required",
        ],
    )
    ctx = b.new_context(viewport={"width": 390, "height": 844}, permissions=["microphone"])
    pg = ctx.new_page()
    pg.on("console", lambda m: logs.append("console.%s: %s" % (m.type, m.text)))
    pg.on("pageerror", lambda e: logs.append("pageerror: %s" % e))
    pg.on(
        "response",
        lambda r: logs.append("resp %s %s" % (r.status, r.url)) if "/api/" in r.url else None,
    )
    pg.goto(URL)
    pg.wait_for_timeout(800)
    pg.click("#startBtn")
    pg.wait_for_timeout(1200)

    box = pg.locator("#micBtn").bounding_box()
    print("mic box:", box)
    if not box:
        print("FAIL: mic button has no box")
        sys.exit(2)
    pg.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    pg.mouse.down()
    pg.wait_for_timeout(400)
    print("after down, state =", pg.inner_text("#stateLabel"))
    pg.wait_for_timeout(HOLD_MS)
    pg.mouse.up()
    print("after up, state =", pg.inner_text("#stateLabel"))
    pg.wait_for_timeout(9000)

    print("final state :", pg.inner_text("#stateLabel"))
    print("hint        :", pg.inner_text("#hint"))
    print("caption     :", pg.inner_text("#subtitle").strip()[:120])
    print("metrics     :", pg.inner_text("#metrics").replace("\n", " | "))
    try:
        print("toast       :", pg.inner_text("#toast") if pg.locator("#toast").is_visible() else "(hidden)")
    except Exception as exc:
        print("toast       : <err %s>" % exc)
    pg.screenshot(path=SHOT)
    print("screenshot  :", SHOT)
    b.close()

print("--- browser log (last 40) ---")
for line in logs[-40:]:
    print(" ", line)
