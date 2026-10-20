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
    page.wait_for_timeout(1500)


def send(page, text, hold_ms=900):
    """只发送、不等这一轮结束（用于紧接着盯着演出层看，或者准备好做打断）。"""
    log("send:", text)
    page.fill("#textInput", text)
    page.wait_for_timeout(hold_ms)
    page.click("#sendBtn")
    page.wait_for_timeout(400)


def watch_flash(page, timeout=10000):
    """等「打雷」这一拍真的闪起来再截。

    2026-10-21：闪电不再走演出层（#cinema），而是 #flash 补光 + WebAudio 滚雷。
    旧脚本在这里等 #cinema 12 秒，等于在成片里插了 13 秒死画面（第一版录屏就带着它）。
    """
    try:
        page.wait_for_function(
            "() => { const c = document.getElementById('actionChip');"
            "        return !!c && c.classList.contains('show') && c.textContent.indexOf('闪电') >= 0; }",
            timeout=timeout)
        # 补光是 0.6s 的 CSS 动画，连拍两张抓中段，两张都不中就用第一张
        page.wait_for_timeout(90)
        page.screenshot(path=str(OUT / "cinema_thunder.png"))
        page.wait_for_timeout(120)
        page.screenshot(path=str(OUT / "cinema_thunder_b.png"))
        state = page.evaluate("""() => {
            const f = document.getElementById('flash');
            const c = document.getElementById('actionChip');
            return {flashOn: !!(f && f.classList.contains('on')),
                    chip: c ? c.textContent : '',
                    opacity: f ? getComputedStyle(f).opacity : ''}; }""")
        log("flash:", json.dumps(state, ensure_ascii=False))
        page.wait_for_timeout(1400)
        return state
    except Exception as exc:
        log("flash never fired:", type(exc).__name__)
        page.screenshot(path=str(OUT / "cinema_thunder_missing.png"))
        return None


def watch_cinema(page, name, timeout=12000):
    """等演出层真的出现，截图存证（2026-10-20 起是静帧 + 程序合成，不再有 MP4）。"""
    try:
        page.wait_for_function(
            "() => document.getElementById('cinema').classList.contains('show')",
            timeout=timeout)
        page.wait_for_timeout(700)
        shot = OUT / f"cinema_{name}.png"
        page.screenshot(path=str(shot))
        state = page.evaluate("""() => { const c = document.getElementById('cinema');
            const s = document.getElementById('eventStill');
            return {shown: c.classList.contains('show'),
                    ready: c.classList.contains('still-ready'),
                    fallback: c.classList.contains('fallback'),
                    badge: document.getElementById('cinemaBadge').textContent,
                    src: s && s.currentSrc ? s.currentSrc.split('/').pop() : ''}; }""")
        log("cinema:", json.dumps(state, ensure_ascii=False))
        page.wait_for_timeout(2600)
        return state
    except Exception as exc:
        log("cinema layer never showed:", type(exc).__name__)
        page.screenshot(path=str(OUT / f"cinema_{name}_missing.png"))
        return None


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
        chapter(page, "进入场景：待机状态（雨后、两盏暖灯、湿街反光）", "idle")
        page.wait_for_timeout(4200)

        # ---------- a) 文字互动 ----------
        chapter(page, "a) 文字互动：她先开口（语音 + 字幕 + 表情/动作同步）", "text1")
        say(page, "打扰了，外面雨刚停。你一个人在这儿？")
        say(page, "我叫小周，今天加班到现在。你是在等人吗？")

        # ---------- b) 角色状态与表演：情绪/动作由指令驱动 ----------
        chapter(page, "b) 角色状态：倾听 → 思考 → 说话，表情和动作随指令切换", "states")
        say(page, "等多久了？给他打过电话吗？")
        say(page, "外面雨好像小了点，你等的人还没来吗？")

        # ---------- d) 台词触发的视觉事件：照片（内容触发，不是计时器） ----------
        # 顺序很重要：先问照片。问「相机」也会触发同一个事件，如果先问相机，
        # 事件会被去重规则吃掉，后面再问照片就不会演了（第一次录屏就踩了这个坑）。
        chapter(page, "d) 台词触发事件 1：提到照片 → 镜头切近景 + 照片演出", "photo_event")
        send(page, "能让我看看那张照片吗？")
        cinema_photo = watch_cinema(page, "photo")
        wait_idle(page, timeout=70)
        page.wait_for_timeout(2200)

        say(page, "你这相机不错啊，平时拍什么？")
        say(page, "这张照片你一直带在身上吗？")

        # ---------- d) 台词触发的视觉事件：雷闪 ----------
        chapter(page, "d) 台词触发事件 2：提到打雷 → 闪电补光 + 滚雷（不占用演出层）",
                "thunder_event")
        send(page, "外面好像又打雷了。")
        cinema_thunder = watch_flash(page)
        wait_idle(page, timeout=70)
        page.wait_for_timeout(1200)

        # ---------- a) 语音互动（真实麦克风链路：getUserMedia → MediaRecorder → /api/stt） ----------
        chapter(page, "a) 语音互动：WAV 伪装麦克风，走同一条 STT 通路", "voice_listening")
        voice_turn(page, hold=4.8)
        chapter(page, "语音转写结果出现在观测指标里，并直接成为下一轮输入", "voice_after")

        say(page, "后来呢？他就没再出现过？")

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

        # ---------- 收尾：末班车 + 关灯（环境变化由卡包 pin 保证一定出现） ----------
        chapter(page, "收尾：末班车 → 灯光转暗（环境变化）→ 情绪收束", "closing")
        page.fill("#textInput", "末班车还有多久？我陪你等到最后一班。")
        page.wait_for_timeout(900)
        page.click("#sendBtn")
        page.wait_for_timeout(600)
        try:
            page.wait_for_function("() => window.__mira.S.state === 'idle'", timeout=60000)
        except Exception:
            pass
        # 再推一拍：收尾拍的场景变化是卡包 pin 的，聊到这一拍就会关灯
        page.fill("#textInput", "快打烊了吧？灯都要关了。")
        page.wait_for_timeout(800)
        page.click("#sendBtn")
        page.wait_for_timeout(600)
        try:
            page.wait_for_function("() => window.__mira.S.scene === 'lights_dim'", timeout=45000)
            page.wait_for_timeout(1200)
            page.screenshot(path=str(OUT / "closing_lights_dim.png"))
            log("lights_dim reached -> screenshot saved")
        except Exception:
            log("lights_dim never reached")
        wait_idle(page, timeout=70)
        page.wait_for_timeout(4200)

        # ---------- 无密钥路径（题目要求「无需私人 API 密钥也能查看核心流程」） ----------
        # 2026-10-20 改：观测面板现在**只暴露两档**——本地 Qwen3 与云端 DeepSeek（产品要求）。
        # 原来这一章是 `select_option("#modeSelect", "mock")`，mock 选项删掉后这里会直接抛错。
        # 改成拍「本地档本来就不需要任何 Key」：面板里 key 那一行默认就不出现，
        # 这比切到 Mock 更能说明「无密钥可跑」——Mock 只是零依赖兜底，不是产品路径。
        chapter(page, "无密钥路径：本地档不填任何 Key（DeepSeek 的 key 行默认不出现）",
                "no_key_mode")
        page.click("#debugBtn")
        page.wait_for_timeout(600)
        page.screenshot(path=str(OUT / "no_key_panel.png"))
        page.evaluate("""() => {
            const row = document.querySelector('.dp-row.dp-cloud');
            const sel = document.getElementById('modeSelect');
            return {
                options: Array.from(sel.options).map(o => o.value),
                cloudRowHidden: !!(row && row.hidden),
                srvMode: (document.getElementById('srvMode') || {}).textContent || '',
            };
        }""")
        page.wait_for_timeout(600)
        page.click("#debugClose")
        page.wait_for_timeout(300)
        say(page, "你是在等人吗？")
        say(page, "外面好像打雷了。")
        page.wait_for_timeout(3200)

        # ---------- 历史记录（可选扩展：会话回放 / 问题诊断）----------
        # walk.py 里那张 20_history.png 是手工 setState 走出来的会话，一条真实轮次都没有，
        # 面板当然是空的 —— 不能当证据。这里用**这一场真聊过的会话**打开面板。
        page.click("#historyBtn")
        page.wait_for_timeout(1600)
        chapter(page, "历史记录：这一场真实对话逐轮回看，可导出 JSON", "history")
        hist = page.evaluate("""() => ({
            meta: (document.getElementById('historyMeta') || {}).textContent || '',
            items: document.querySelectorAll('#historyList .hist-item').length,
            first: ((document.querySelector('#historyList .hist-bubble') || {}).textContent || '').slice(0, 40),
        })""")
        log("history:", json.dumps(hist, ensure_ascii=False))
        page.screenshot(path=str(OUT / "history_panel.png"))
        page.wait_for_timeout(2400)
        page.click("#historyClose")
        page.wait_for_timeout(600)

        stats = page.evaluate("""() => ({
            state: window.__mira.S.state, beat: window.__mira.S.beat,
            turns: window.__mira.S.turns, errors: window.__mira.S.errors,
            seq: window.__mira.S.seq, emotion: window.__mira.S.emotion,
            scene: window.__mira.S.scene,
            latency: document.getElementById('latency').textContent,
        })""")
        log("stats:", json.dumps(stats, ensure_ascii=False))
        log("cinema:", json.dumps({"photo": cinema_photo, "thunder": cinema_thunder},
                                  ensure_ascii=False))
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
