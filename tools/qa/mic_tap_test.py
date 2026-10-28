"""轻点麦克风（不按住）+ 停顿自动发送：验证「说完话什么都不点也会发出去」。

这是用户投诉「话筒无法说话」最可能撞上的场景：很多人第一次用是「点一下，然后说话」，
如果实现里只支持按住说话、或者静音阈值写死导致检不到语音，就会一直录到上限、
什么也不发生。这个脚本把那条路径单独跑一遍。

  .venv\\Scripts\\python.exe tools\\qa\\mic_tap_test.py
"""
import sys
import wave
from pathlib import Path

from playwright.sync_api import sync_playwright

URL = "http://127.0.0.1:8787/?rec=1"
ROOT = Path(__file__).resolve().parents[2]
SRC_WAV = ROOT / "cards" / "mira_cafe" / "assets" / "audio" / "sample_user.wav"
TMP = ROOT / ".tmp"
WAV = TMP / "mic_tap_16k.wav"
SHOT = TMP / "mic_tap_test.png"
TAP_MS = 90


def make_fixture() -> Path:
    """人声 + 尾部静音。仓库只带卡包里的示例语音，而「轻点后停顿自动发送」需要
    尾部有一段真静音来触发 VAD，所以在本地拼一份（.tmp/ 已在 .gitignore 里）。"""
    TMP.mkdir(parents=True, exist_ok=True)
    if WAV.is_file():
        return WAV
    with wave.open(str(SRC_WAV), "rb") as w:
        params = w.getparams()
        frames = w.readframes(w.getnframes())
    silence = b"\x00" * (params.framerate * params.sampwidth * params.nchannels * 2)
    with wave.open(str(WAV), "wb") as out:
        out.setparams(params)
        out.writeframes(frames + silence)
    return WAV


WAV = make_fixture()

logs = []

with sync_playwright() as p:
    b = p.chromium.launch(
        headless=True,
        args=[
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            "--use-file-for-fake-audio-capture=" + str(WAV),
            "--autoplay-policy=no-user-gesture-required",
        ],
    )
    ctx = b.new_context(viewport={"width": 390, "height": 844}, permissions=["microphone"])
    pg = ctx.new_page()
    pg.on("console", lambda m: logs.append(f"console.{m.type}: {m.text}"))
    pg.on("pageerror", lambda e: logs.append(f"pageerror: {e}"))
    pg.on("response", lambda r: logs.append(f"resp {r.status} {r.url}") if "/api/" in r.url else None)

    pg.goto(URL)
    pg.wait_for_timeout(900)
    pg.click("#startBtn")
    pg.wait_for_timeout(2500)

    box = pg.locator("#micBtn").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    print(f"tap at ({cx:.0f},{cy:.0f}) for {TAP_MS}ms, then do nothing")
    pg.mouse.move(cx, cy)
    pg.mouse.down()
    pg.wait_for_timeout(TAP_MS)
    pg.mouse.up()

    # 录音条应该出现，并且有可见的计时
    try:
        pg.wait_for_selector("#recBar:not([hidden])", timeout=3000)
        print("recBar visible        :", pg.inner_text("#recTime"), "/", pg.inner_text("#recTip"))
    except Exception:
        print("FAIL: recBar never shown")

    # 之后什么都不点，等自动发送（自适应阈值 + 1.15s 静音）
    sent = False
    for _ in range(24):  # 最多等 ~12s
        pg.wait_for_timeout(500)
        st = pg.inner_text("#stateLabel")
        hint = pg.inner_text("#hint")
        if "STT" in pg.inner_text("#metrics"):
            sent = True
            print("auto-sent after      :", hint)
            break
        if st in ("思考", "说话"):
            sent = True
            print("state moved to        :", st)
            break
    pg.wait_for_timeout(6000)

    print("final state          :", pg.inner_text("#stateLabel"))
    print("hint                 :", pg.inner_text("#hint"))
    print("caption              :", pg.inner_text("#subtitle").strip()[:120])
    print("metrics              :", pg.inner_text("#metrics").replace("\n", " | "))
    print("recBar still shown   :", not pg.locator("#recBar").is_hidden())
    pg.screenshot(path=str(SHOT))
    print("screenshot           :", SHOT)
    b.close()

print("--- browser log ---")
for line in logs[-25:]:
    print(" ", line)

print("\nRESULT:", "PASS 轻点后停顿自动发送" if sent else "FAIL 轻点后没有自动发送")
sys.exit(0 if sent else 1)
