"""Record the 3-5 minute deliverable demo by driving the real UI in headless Chromium.

题目交付项 4 要求：一段 3-5 分钟录屏，必须展示
  a) 文字或语音互动（两种都要）
  b) 角色状态和画面变化
  c) 在角色说话时成功打断
  d) 所选择的动画/视频或生成媒体能力
  e) 至少一种失败或降级状态

怎么录到的：
- Playwright 的 screencast 没有声音，所以页面在 ?rec=1 时把自己播放的每个 <audio>
  同时接进一个 AudioContext -> MediaStreamDestination，录成 audio.webm；
  最后用 ffmpeg 把「页面视频 + 页面真实音频」mux 成 mp4，音画同源，不是后期配的。

  python tools/qa/record_demo.py [base_url]

输出: production/reports/demo/mira_demo_4min.mp4（另存分镜截图与 chapters.json）
"""
import base64
import json
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(r"D:\interview\mira")
OUT = ROOT / "production" / "reports" / "demo"
OUT.mkdir(parents=True, exist_ok=True)
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787"
FFMPEG = (ROOT / "models" / "anisora" / "wan" / "runtime" / "Lib" / "site-packages"
          / "imageio_ffmpeg" / "binaries" / "ffmpeg-win-x86_64-v7.1.exe")
AUDIO_OFFSET = 0.25      # 页面音频比 video 起点晚一点点
CHAPTERS = []           # [{t, title}] 用于截图说明，不烧进视频


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def chapter(page, title, shot=None):
    CHAPTERS.append({"t": round(time.time() - T0, 1), "title": title})
    log(f"== {title}")
    if shot:
        page.screenshot(path=str(OUT / f"{len(CHAPTERS):02d}_{shot}.png"))


def make_fake_mic() -> Path:
    """48 kHz mono WAV with leading/trailing silence, fed to Chromium as the mic."""
    dst = OUT / "fake_mic_48k.wav"
    if dst.exists():
        return dst
    subprocess.run(
        [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(ROOT / "app" / "static" / "assets" / "audio" / "sample_user.wav"),
         "-af", "adelay=900,apad=pad_dur=6", "-ar", "48000", "-ac", "1",
         "-c:a", "pcm_s16le", str(dst)],
        check=True,
    )
    return dst


def wait_idle(page, timeout=90):
    page.wait_for_function("() => window.__mira && window.__mira.S.state === 'idle'",
                           timeout=timeout * 1000)


def wait_state(page, name, timeout=30):
    page.wait_for_function(
        "n => window.__mira && window.__mira.S.state === n", arg=name, timeout=timeout * 1000)


def say(page, text, hold_ms=900):
    """打字 + 发送，然后等这一轮说完（音频播完才回 idle）。"""
    log("type:", text)
    page.fill("#textInput", text)
    page.wait_for_timeout(hold_ms)          # 真人打字的节奏
    page.click("#sendBtn")
    page.wait_for_timeout(450)
    wait_idle(page, timeout=70)
    page.wait_for_timeout(1200)


def press_mic(page, hold=4.6):
    box = page.locator("#micBtn").bounding_box()
    log("mic press (hold %.1fs)" % hold)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.wait_for_timeout(int(hold * 1000))
    page.mouse.up()
    page.wait_for_timeout(300)


def voice_turn(page, hold=4.6):
    press_mic(page, hold)
    page.wait_for_timeout(400)
    wait_idle(page, timeout=90)
    page.wait_for_timeout(1200)


def main() -> int:
    global T0
    T0 = time.time()
    fake_mic = make_fake_mic()
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--autoplay-policy=no-user-gesture-required",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                f"--use-file-for-fake-audio-capture={fake_mic}",
                "--allow-file-access-from-files",
            ],
        )
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844},
            device_scale_factor=2,
            locale="zh-CN",
            permissions=["microphone"],
            record_video_dir=str(OUT / "raw"),
            record_video_size={"width": 390, "height": 844},
        )
        page = ctx.new_page()
        page.on("pageerror", lambda e: log("pageerror:", str(e)[:200]))
        page.on("console", lambda m: log("console", m.type, m.text[:140])
                if m.type in ("error", "warning") else None)

        page.goto(f"{BASE}/?rec=1", wait_until="networkidle")
        page.wait_for_timeout(1200)
        chapter(page, "开始页：场景与角色是视觉主体，不是聊天列表", "gate")
        page.wait_for_timeout(2600)

        page.click("#startBtn")
        page.wait_for_timeout(600)
        wait_idle(page, timeout=60)
        chapter(page, "进入场景：待机状态（雨、灯光、窗外车流在动）", "idle")
        page.wait_for_timeout(2800)

        # ---------- a) 文字互动 ----------
        chapter(page, "文字互动 1/3：她说话（语音 + 字幕 + 表情/动作）", "text1_thinking")
        say(page, "打扰了，外面雨刚停。你一个人在这儿？")
        chapter(page, "文字互动 2/3", "text2")
        say(page, "这么晚还开着，是这家店通宵吗？")

        # ---------- a) 语音互动（真实麦克风链路：getUserMedia -> MediaRecorder -> /api/stt） ----------
        chapter(page, "语音互动：Chromium 用 WAV 伪装麦克风，走同一条 STT 通路", "voice_listening")
        voice_turn(page, hold=4.8)
        chapter(page, "语音输入的转写结果出现在观测指标里", "voice_after")

        say(page, "我猜你在等人。他迟到了？")
        say(page, "所以你一直在等，没给他打电话？")
        chapter(page, "角色状态：思考→说话，表情/动作随指令切换", "thinking")
        say(page, "你是摄影师吧？我好像看到你包里有相机。")
        # 第 3 拍之后「照片」关键词会触发 photo_reveal 的视频演出
        chapter(page, "d) 多模态：台词触发视频演出（照片特写）", "photo_event")
        say(page, "那张照片是谁？能让我看看吗？", hold_ms=1100)
        page.wait_for_timeout(2200)

        # ---------- c) 语音打断 ----------
        chapter(page, "c) 打断：她正在说话时按麦克风", "bargein")
        page.fill("#textInput", "那你打算等到什么时候？")
        page.click("#sendBtn")
        wait_state(page, "speaking", timeout=30)
        page.wait_for_timeout(900)               # 让她先说出半句
        press_mic(page, hold=4.8)                # 按下即打断 + 立刻开始新录音
        page.wait_for_timeout(500)
        wait_idle(page, timeout=90)
        chapter(page, "打断后：旧回复被取消，页面回到倾听→思考，新输入生效", "after_bargein")
        page.wait_for_timeout(1200)

        # ---------- e) 失败/降级状态：LLM 超时 ----------
        chapter(page, "e) 失败状态 1：注入 LLM 超时 → 错误提示 + 可恢复", "fail_llm")
        page.click("#debugBtn")
        page.wait_for_timeout(600)
        page.click('[data-inject="llm_timeout"]')
        page.click("#debugClose")
        page.wait_for_timeout(300)
        page.fill("#textInput", "你听见我刚才说的了吗？")
        page.wait_for_timeout(700)
        page.click("#sendBtn")
        page.wait_for_timeout(5200)              # 3s 注入延迟 + 事件
        wait_idle(page, timeout=40)
        page.wait_for_timeout(900)

        # ---------- e) 失败状态 2：TTS 失败 → 降级为字幕 ----------
        chapter(page, "e) 失败状态 2：注入 TTS 失败 → 语音降级为字幕，体验不断", "fail_tts")
        page.click("#debugBtn")
        page.wait_for_timeout(500)
        page.click('[data-inject="tts_fail"]')
        page.click("#debugClose")
        page.wait_for_timeout(300)
        say(page, "没关系，那就再坐一会儿。外面还在下雨吗？")
        page.wait_for_timeout(800)

        # 关掉所有注入，恢复正常
        page.click("#debugBtn")
        page.wait_for_timeout(500)
        page.click('[data-inject="tts_fail"]')
        page.wait_for_timeout(200)
        page.screenshot(path=str(OUT / "90_debug_panel.png"))
        chapter(page, "观测面板：provider、故障注入、事件流水、首包/总耗时", "debug_panel")
        page.click("#debugClose")
        page.wait_for_timeout(300)

        # ---------- 收尾：再一次语音互动 + 末班车 ----------
        chapter(page, "收尾：语音提问，情绪收束", "closing")
        page.fill("#textInput", "末班车还有多久？我陪你等到最后一班。")
        page.wait_for_timeout(900)
        page.click("#sendBtn")
        page.wait_for_timeout(600)
        try:
            page.wait_for_function("() => window.__mira.S.state === 'speaking'", timeout=25000)
            page.wait_for_timeout(2600)
        except Exception:
            pass
        wait_idle(page, timeout=70)
        page.wait_for_timeout(2200)

        stats = page.evaluate("""() => ({
            state: window.__mira.S.state, beat: window.__mira.S.beat,
            turns: window.__mira.S.turns, errors: window.__mira.S.errors,
            seq: window.__mira.S.seq, emotion: window.__mira.S.emotion,
            scene: window.__mira.S.scene,
            latency: document.getElementById('latency').textContent,
        })""")
        log("stats:", json.dumps(stats, ensure_ascii=False))
        chapter(page, "结束", "end")
        page.wait_for_timeout(1500)

        audio_b64 = page.evaluate("async () => await window.__miraRec.stop()")
        log("audio bytes:", len(audio_b64 or "") * 3 // 4)
        video = page.video
        ctx.close()
        raw_video = Path(video.path())
        browser.close()

    (OUT / "audio.webm").write_bytes(base64.b64decode(audio_b64))
    raw_copy = OUT / "page_video.webm"
    raw_copy.write_bytes(raw_video.read_bytes())
    log("video:", raw_copy, raw_copy.stat().st_size // 1024, "KB")

    final = OUT / "mira_demo_4min.mp4"
    subprocess.run(
        [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(raw_copy), "-itsoffset", str(AUDIO_OFFSET), "-i", str(OUT / "audio.webm"),
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-shortest",
         "-movflags", "+faststart", str(final)],
        check=True,
    )
    (OUT / "chapters.json").write_text(
        json.dumps({"stats": stats, "chapters": CHAPTERS}, ensure_ascii=False, indent=2),
        encoding="utf-8")
    dur = subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(final)],
                         capture_output=True, text=True).stderr
    log("final:", final, final.stat().st_size // 1024, "KB")
    for line in dur.splitlines():
        if "Duration" in line:
            log(line.strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
