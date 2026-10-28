"""record_demo_v15.py — 一条演示片覆盖**全部分支与三个结局**。

题目交付项 4 要一段 3–5 分钟的录屏，能看清「互动 / 状态变化 / 打断 / 生成媒体 / 降级」。
v14 只录了主线（借手机 → 朋友结局），另外两条分支和另外两个结局只能靠探针另证；
v15 把它们放进同一条片子，四段各自独立会话、各自录「页面视频 + 页面真实音频」：

  A 主线   借手机 → 打通 → 朋友结局      （完整演：文字 / 语音打断 / TTS 降级 / 观测面板）
  B 分支   不借手机 → 陌生人结局
  C 分支   借手机 → 打不通（call_fail）    ← 注意：卡里「借过就是朋友线」，见 docs
  D 结局   中途说「我先走了」→ 提前道别（ending_leave）

为什么每段都必须单独开一段会话：
  · 故事状态（flags / clock）在服务端是**每会话一份**，同一条会话里没法既借又不借；
  · phone_call 的 70/30 是未播种 rng，同一段重录一次就会漂到另一条分支。
  所以「分支隔离 = 每段刷新页面 = 新 session」，随机则靠 app/story.py 的
  MIRA_FORCE_CALL / POST /api/debug/inject {"force_call": "ok"|"fail"} 钉死
  （仅演示用，默认关闭；服务重启后凭据回到环境变量值）。

从 v14 原样保留的三套机制（每一套都是踩过坑换来的，别省）：
  1. 音频不是后期配的：Playwright 的 screencast 只有画面。页面在 ?rec=1 时会录
     <audio>，但 TTS 主通路是流式 PCM（createBufferSource → analyser → destination），
     根本不是 <audio> 元素 —— v14 第一版成片 3/4 是 -91dB 静音。所以用 AUDIO_PATCH
     把 AudioContext 收成单例、在 AudioNode.connect(destination) 上做旁路，录真正
     在响的那条线，最后和页面视频 mux：音画同源，不是后期配音。
  2. 拍板：视频从建页就开始录，音轨从第一次出声才开始，两边差多少没法凭空知道。
     页面加载后打一次「白闪 + 1kHz 蜂鸣」同时发生，各自测出时刻相减 = 精确偏移。
  3. PATCH 钩子：覆写 window.handleEvent 读每轮的 story 节点（不用改 app.js）。

用法：
    python tools/qa/record_demo_v15.py             # 录 4 段 + 各自 mux（不出整片）
    python tools/qa/record_demo_v15.py --full      # 再加章节卡、拼成 mira_demo_v15.mp4
    python tools/qa/record_demo_v15.py --pack      # 不重录，只重做 mux/章节卡/拼接（调时长用）
    python tools/qa/record_demo_v15.py --sim       # 纯本地跑故事引擎验 4 条路径（不连服务）
    python tools/qa/record_demo_v15.py --only A,C  # 只录指定段
    python tools/qa/record_demo_v15.py --check     # 只复核已有成片（时长 / 音轨 / 结局抽帧）

节奏（成片时长主要靠这几档，改动后要重新量）：
    DEMO_GAP_A   A 段每轮停顿  默认 0.8   （演给人看，留呼吸）
    DEMO_GAP     B/C/D 段停顿  默认 0.25  （分支走通即可）
    DEMO_END     结局静帧停留  默认 8     （v14 是 26，四段拼起来撑不住）
    DEMO_GATE_A  A 段开始页    默认 4.5
    DEMO_GATE    其它段开始页  默认 2
    DEMO_CARD    章节卡停留    默认 1.6
    --verify 会把这些压到最短（只验路径，不求好看）
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "production" / "reports" / "demo_v20"
RAW = OUT / "raw"          # Playwright 原始 webm / 音轨
SEG = OUT / "segments"     # 每段 mux 好的 mp4
CARDS = OUT / "cards"      # 章节卡 png + mp4
SHOTS = OUT / "shots"      # 关键帧截图（验收用）
for _d in (OUT, RAW, SEG, CARDS, SHOTS):
    _d.mkdir(parents=True, exist_ok=True)

BASE = os.environ.get("DEMO_BASE", "http://127.0.0.1:8000")
FFMPEG = (ROOT / "models" / "anisora" / "wan" / "runtime" / "Lib" / "site-packages"
          / "imageio_ffmpeg" / "binaries" / "ffmpeg-win-x86_64-v7.1.exe")

W, H, FPS = 780, 1688, 25          # 成片尺寸：390×844 的 2 倍（录屏画布必须等于 CSS 视口）


def _env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


FAST = "--verify" in sys.argv           # 只验路径：把每一档停顿压到最短
FULL = "--full" in sys.argv
SIM = "--sim" in sys.argv
CHECK = "--check" in sys.argv
PACK = "--pack" in sys.argv          # 只重跑「mux + 章节卡 + 拼接」，不重录
CARDS_ONLY = "--cards" in sys.argv   # 只重画章节卡并重拼（分段 mp4 不动）

GAP_A = _env("DEMO_GAP_A", 1.5)
GAP_FAST = _env("DEMO_GAP", 1.5)
END_HOLD = _env("DEMO_END", 8.0)
GATE_A = _env("DEMO_GATE_A", 3.0)
GATE = _env("DEMO_GATE", 2.0)
CARD_HOLD = _env("DEMO_CARD", 1.5)
# 原速交付：不加速、不剪口子。这两个旋钮保留只为临时排障（1.0 = 不动）。
# 时长靠「MIRA_MINUTES_PER_TURN 步长 → 轮次变少」来压，不是靠变速。
SPEED_A = _env("DEMO_SPEED_A", 1.0)
SPEED_FAST = _env("DEMO_SPEED_FAST", 1.0)
END_TARGET = _env("DEMO_END_TARGET", 0.0)   # 0 = 不额外冻结，结局静帧按录制时的 8s

if FAST:
    GAP_A, GAP_FAST, END_HOLD = 0.4, 0.3, 0.8
    GATE_A, GATE = 1.2, 1.0
if os.environ.get("DEMO_AUDIO_TAP", "1") == "1":
    AUDIO_TAP = True
else:
    AUDIO_TAP = False

T0 = 0.0


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


# ---------------------------------------------------------------- 页面钩子
# handleEvent 是顶层 function 声明 → 本身就是 window 的属性，内部调用也走这个绑定，
# 所以覆盖 window.handleEvent 能截到流事件，不用改 app.js。
PATCH = """
() => {
  window.__node = null;
  window.__seen = [];
  window.__log = [];
  const orig = window.handleEvent;
  window.handleEvent = function (ev, seq, t0) {
    if (ev && ev.node) {
      window.__node = ev.node;
      if (window.__seen[window.__seen.length - 1] !== ev.node) window.__seen.push(ev.node);
    }
    if (ev && ev.t === 'done') {
      window.__log.push({node: window.__node, provider: ev.provider, ms: ev.ms,
                         first: ev.first_audio_ms, errors: ev.audio_errors,
                         advanced: !!ev.advanced});
    }
    return orig.apply(this, arguments);
  };
}
"""

# 页内音频录制补丁（init script，比 app.js 先跑）。原理见文件头第 1 条。
AUDIO_PATCH = r"""
(() => {
  const AC = window.AudioContext || window.webkitAudioContext;
  let single = null;
  const Wrapped = function (...args) {
    if (!single || single.state === "closed") single = new AC(...args);
    return single;
  };
  Wrapped.prototype = AC.prototype;
  window.AudioContext = Wrapped;
  window.webkitAudioContext = Wrapped;

  // 为什么 close 要被废掉：app.js 的 endMeter() 每次松开麦克风都会 close() 掉
  // 麦克风的 AudioContext。补丁把 AudioContext 收成了单例，这一 close 会把**播放**
  // 用的同一个上下文一起关掉 —— 此后 TTS 音频永远播不完，状态卡在 speaking。
  // 实测正是它让打断之后整轮不回归 idle、录屏僵死 3 分钟。
  // 上下文归补丁所有，close 变空操作；麦克风轨道的释放由 stopMic 自己 stop tracks。
  AC.prototype.close = function () { return Promise.resolve(); };

  const R = { ctx: null, dest: null, rec: null, chunks: [], startedAt: null };
  window.__REC = R;
  window.__T0 = performance.now();

  const origConnect = AudioNode.prototype.connect;
  AudioNode.prototype.connect = function (d, ...rest) {
    const out = origConnect.call(this, d, ...rest);
    try {
      const c = this.context;
      if (d === c.destination) {
        if (!R.dest || R.ctx !== c) {
          R.ctx = c;
          R.dest = c.createMediaStreamDestination();
          R.rec = new MediaRecorder(R.dest.stream, { mimeType: "audio/webm;codecs=opus" });
          R.rec.ondataavailable = (e) => { if (e.data && e.data.size) R.chunks.push(e.data); };
          R.startedAt = performance.now();
          R.rec.start();
        }
        if (R.ctx === c) origConnect.call(this, R.dest);
      }
    } catch (e) { /* 旁路失败不能影响播放 */ }
    return out;
  };

  window.__MYREC = {
    start() { return R.rec ? R.rec.state : "unavailable"; },
    async stop() {
      if (!R.rec) return null;
      const done = new Promise((res) => { R.rec.onstop = res; });
      try { R.rec.stop(); } catch (e) { return null; }
      await done;
      const blob = new Blob(R.chunks, { type: "audio/webm" });
      const buf = await blob.arrayBuffer();
      let bin = "";
      const bytes = new Uint8Array(buf);
      for (let i = 0; i < bytes.length; i += 0x8000) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
      }
      return btoa(bin);
    },
  };
})();
"""

# 拍板：白闪 + 1kHz 蜂鸣同一瞬间，用来量音画偏移（成片里会被剪掉）。
CLAPPER = """
() => new Promise((res) => {
  const sr = 48000, dur = 0.25, n = Math.floor(sr * dur);
  const buf = new ArrayBuffer(44 + n * 2), dv = new DataView(buf);
  const w = (o, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
  w(0, "RIFF"); dv.setUint32(4, 36 + n * 2, true); w(8, "WAVEfmt ");
  dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, sr, true); dv.setUint32(28, sr * 2, true);
  dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
  w(36, "data"); dv.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) {
    dv.setInt16(44 + i * 2, Math.sin(2 * Math.PI * 1000 * i / sr) * 0.9 * 32767, true);
  }
  let bin = "";
  const u = new Uint8Array(buf);
  for (let i = 0; i < u.length; i++) bin += String.fromCharCode(u[i]);
  const a = new Audio("data:audio/wav;base64," + btoa(bin));
  a.crossOrigin = "anonymous";
  try { window.recRoute(a); } catch (e) {}
  const d = document.createElement("div");
  d.style.cssText = "position:fixed;left:0;top:0;width:10px;height:10px;background:#fff;"
    + "z-index:99999;pointer-events:none";
  (document.body || document.documentElement).appendChild(d);
  a.play().catch(() => {});
  setTimeout(() => d.remove(), 130);
  res(true);
})
"""


# ---------------------------------------------------------------- 台词表
# 每段在**当前节点**说哪句话。为什么按节点而不是按轮次背台词表：
# 故事推进由 clock / turn_gte / 关键词三套规则共同决定，任何一处节奏差一轮，
# 按轮次排的台词表就会说到别的节点上去；按节点取词则怎么漂都能对上台。
# B 路径（不借手机）单独一份：phone_dead 那几句里**绝不能**出现
# 「借你 / 用我的 / 给你 / 手机借 / 打个电话」——app/main.py 一命中就把
# offered_phone 掀了，结局会从陌生人悄悄变成朋友。
LINES_A = {
    "arrival": ["外面雨刚停吧。", "我可以坐这儿吗？"],
    "order": ["你喝的是什么？", "有热水吗？"],
    "waiting": ["你在等人？", "约的几点？"],
    "xiaoxue": ["等的是谁？", "你们多久没见了？"],
    "two_years": ["你们以前很熟吧？", "为什么约今天？"],
    "rain_worry": ["雨这么大，她会不会不来了？", "你打过电话吗？"],
    "window_habit": ["你一直在看门口？"],
    "phone_dead": ["你手机呢？", "要不要用我的手机打个电话？", "用我的吧，我不急。"],
    "phone_offer": ["不麻烦，你打。", "给你，打吧。"],
    "phone_call": ["（别说话，等她）"],
    "call_ok": ["她怎么说？"],
    "call_fail": ["没人接？"],
    "release": ["那你现在可以走了。"],
    "anxious": ["她可能也在找你。"],
    "keep_waiting": ["你平时都拍什么？", "这家店你常来？"],
    "last_minutes": ["你要走了吗？", "我陪你等一会儿。"],
    "lights_dim": ["灯要关了？"],
    "ending_friend": ["你叫什么？"],
    "ending_stranger": ["路上小心。"],
    "ending_leave": ["路上小心。"],
}

LINES_B = {
    "arrival": ["外面雨刚停吧。", "我可以坐这儿吗？"],
    "order": ["你喝的是什么？"],
    "waiting": ["你在等人？"],
    "xiaoxue": ["等的是谁？"],
    "two_years": ["你们以前很熟吧？"],
    "rain_worry": ["雨这么大，她会不会不来了？"],
    "window_habit": ["你一直在看门口？"],
    # 不提手机（一个字都不能沾），只把话头递给她
    "phone_dead": ["你手机呢？", "那你今晚怎么办？"],
    "keep_waiting": ["你平时都拍什么？", "这家店你常来？"],
    "last_minutes": ["你要走了吗？"],
    "lights_dim": ["灯要关了？"],
    "ending_stranger": ["路上小心。"],
    "ending_friend": ["路上小心。"],
}

LINES_C = {
    "arrival": ["外面雨刚停吧。"],
    "order": ["你喝的是什么？"],
    "waiting": ["你在等人？"],
    "xiaoxue": ["等的是谁？"],
    "ending_leave": ["路上小心。"],
}


def make_picker(lines: dict):
    """按节点循环取词：同一节点停多轮时换着说（避免复读同一句）。

    开场白那一轮没有 node 事件（/api/turn kind=open 不带 node），所以 node 为 None
    时回落到 arrival —— 否则第一句会对着服务端说一句「……」，白白废掉一轮。
    """
    idx: dict[str, int] = {}

    def pick(node: str | None) -> str:
        key = node or "arrival"
        pool = lines.get(key) or lines.get("arrival") or ["……"]
        i = idx.get(key, 0)
        idx[key] = i + 1
        return pool[i % len(pool)]

    return pick


# ---------------------------------------------------------------- 三段编排（v20）
# v20 与 v15 的差别只有一条：**不再有任何剧情钩子**。
#   · v15 用 /api/debug/inject 钉死 phone_call 的 70/30、并改剧情时钟步长；
#     这两个入口在 2026-10-21 夜连同 app/story.py 的 set_force_call 一起删除了
#     （用户第 5 条：结局必须由对话自然触发）。
#   · 所以 v20 只能**顺着剧情走**：借了手机，打通/打不通都进 ending_friend，
#     所以 A 段不需要钉骰子；B/C 段更不依赖随机。
#   · 三段全部用**云端 DeepSeek**（用户 2026-10-22 明确要求用云端大模型录制）。
# 每一段是一串「动作」，live 录制和 --sim 本地模拟吃的是**同一串**，
# 所以模拟能直接证明录制会走到哪个结局（除了 STT 那句只能近似）。
#   ("chapter", 标题, 截图名)   章节（截图是验收证据，不进成片）
#   ("start",)                  点开始 + 拍板 + 等开场白
#   ("say", 台词或None)         打字发送并等她说完；None = 按当前节点取词
#   ("send", 台词)              只发不等（打断前用）
#   ("mic",)                    按住麦克风（假麦克风走真 STT）
#   ("inject_on"/"inject_off", key)  通过观测面板注入/解除故障
#   ("panels",)                 观测面板 + 历史记录
#   ("end",)                    结局章节 + 静帧停留
PLAN_A = [
    ("chapter", "开始页：先给画面，不给一个聊天列表", "gate"),
    ("chapter", "观测面板：Provider 选云端 DeepSeek（key 只在进程内存）", "provider"),
    ("mode", "cloud"),
    ("start",),
    ("chapter", "a) 文字互动：她先开口，台词 / 字幕 / 表情同步", "text1"),
    ("say", "你在等人？", {"human": True}),
    ("say", "等的是谁？", {"human": True}),
    # ---- 语音插话 = 说话中打断（趁她说长句时按住麦克风）----
    ("chapter", "c) 打断：她正说着，按住麦克风插一句话", "bargein"),
    ("send", "外面好像又打雷了。"),
    ("mic",),
    ("chapter", "转写结果直接成为这一轮输入（真 STT 通路，不是写死的）", "voice_after"),
    ("chapter", "剧情推进：她一层层说出等的是谁、等多久", "story_mid"),
    ("say", "你们以前很熟吧？"),
    ("say", "雨这么大，她会不会不来了？"),
    ("say", "你打过电话吗？"),
    # ---- 分叉：借手机（这一轮同时用来演 TTS 失败降级）----
    ("chapter", "d) 剧情分叉：你问她手机呢，然后把手机借给她", "phone_offer"),
    ("inject_on", "tts_fail"),
    ("wait", 900),
    ("say", "要不要用我的手机打个电话？"),
    ("chapter", "e) 降级状态：注入 TTS 失败 → 声音没了，字幕照常，对话不断", "fail_tts"),
    ("inject_off", "tts_fail"),
    # 解除故障的 toast 会压在字幕区上，等它自己收掉再拍电话那一章
    ("wait", 3800),
    # ---- 生成媒体：打电话贴片 ----
    ("chapter", "d) 多模态演出：接电话贴片 + 屏幕光晕 + 环境音压低", "phone_call"),
    ("say", "不麻烦，你打。"),
    ("chapter", "电话有了结果：小雪那边路淹了", "call_ok"),
    ("say", "（别说话，等她）"),
    # ---- 观测面板 / 历史记录（结局之后 HUD 会收起，必须抢在结局前）----
    ("panels",),
    ("say", "路淹了，那她今晚过不来。"),
    ("say", "那你现在可以走了。"),
    ("chapter", "收束：灯要关了，她说出最后一句", "pre_ending"),
    ("say", "灯要关了？"),
    ("say", "我陪你等一会儿。"),
    ("end",),
]

# B/C 用短句（一句一义）：原速下时长完全由她的台词长度决定，输入越长模型越容易
# 写成一大段；短句能明显减少每句的播放时间，这不是剪辑，是正常对话。
PLAN_B = [
    ("chapter", "开始页：同一条街上，换一个选择", "gate"),
    ("mode", "cloud"),
    ("start",),
    ("chapter", "B 分支：这次你没有开口借手机", "b1"),
    ("say", "你在等人？"),
    ("say", "等的是谁？"),
    ("say", "你们以前很熟吧？"),
    ("say", "雨这么大，她会不会不来了？"),
    ("say", "你打过电话吗？"),
    ("say", "你手机呢？"),
    ("say", "那你今晚怎么办？"),
    ("say", "你平时都拍什么？"),
    ("say", "你要走了吗？"),
    ("say", "灯要关了？"),
    ("say", "我陪你等一会儿。"),
    ("end",),
]

PLAN_C = [
    ("chapter", "开始页：戏还没演完，人就先走了", "gate"),
    ("mode", "cloud"),
    ("start",),
    ("chapter", "C 结局：对话中途你说「我先走了」", "c1"),
    ("say", "你在等人？"),
    ("say", "等的是谁？"),
    ("say", "那我先走了，谢谢你的咖啡。"),
    ("end",),
]

SEGMENTS = [
    {"key": "A", "index": 1,
     "chapter": "主线 · 文字 + 打断 + 降级 + 借电话 → 朋友结局",
     "ending_label": "ending_friend",
     "ending_text_has": "交换了名字",
     "provider": "cloud", "clock": None, "speed": 1.0,
     "force": None, "end_node": "ending_friend", "end_kind": "friend",
     "ending_asset": "friend.webp",
     "must_visit": ["phone_offer", "phone_call"],
     "must_not": ["ending_stranger", "ending_leave"],
     "gate_hold": GATE_A, "gap": GAP_A, "plan": PLAN_A, "lines": LINES_A},
    {"key": "B", "index": 2,
     "chapter": "分支 · 没借电话 → 陌生人结局",
     "ending_label": "ending_stranger",
     "ending_text_has": "她等到九点",
     "provider": "cloud", "clock": None, "speed": 1.0,
     "force": None, "end_node": "ending_stranger", "end_kind": "stranger",
     "ending_asset": "stranger.webp",
     # 整轮跳剪：B 段前三轮（等人/等的是谁/你们很熟吧）与 A 段完全同质，剪掉省 ≈22s。
     # 剪点都在每轮结束的 idle 上，段首是重新开的新会话，不在同一会话里跳节点。
     "keep_turns": [3, 4, 5, 6, 7, 9, 10, 11],
     "note": "本章只保留分支与结局：与主线重复的开场铺垫已剪",
     "must_visit": ["phone_dead"],
     "must_not": ["phone_offer", "call_ok", "call_fail"],
     "gate_hold": GATE, "gap": GAP_FAST, "plan": PLAN_B, "lines": LINES_B},
    {"key": "C", "index": 3,
     "chapter": "分支 · 你先走了 → 提前道别",
     "ending_label": "ending_leave",
     "ending_text_has": "你先走了",
     "provider": "cloud", "clock": None, "speed": 1.0,
     "force": None, "end_node": "ending_leave", "end_kind": "leave",
     "ending_asset": "stranger.webp",
     # 剪掉第一轮「你在等人？」（与 A/B 段同质），只留「等的是谁？」→「那我先走了」，
     # 省 ~11s——三段加起来正好卡在 5 分钟以内。
     "keep_turns": [2, 3],
     "note": "本章只保留道别那一轮：与主线重复的开场铺垫已剪",
     "must_visit": [],
     "must_not": ["ending_friend", "ending_stranger"],
     "gate_hold": GATE, "gap": GAP_FAST, "plan": PLAN_C, "lines": LINES_C},
]

SEGBY = {s["key"]: s for s in SEGMENTS}


# ---------------------------------------------------------------- 页面小工具
def make_fake_mic() -> Path:
    """48 kHz mono WAV，前置 0.9s 静音 —— Chromium 的假麦克风输入。

    内容「你是在等什么人吗?」：这场戏里最自然的一句追问，也正好不命中任何
    分支关键词（不会把故事提前推到 borrow 或 leave）。
    """
    dst = OUT / "fake_mic_48k.wav"
    if dst.exists():
        return dst
    subprocess.run(
        [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(ROOT / ".tmp" / "sample_user.wav"),
         "-af", "adelay=900,apad=pad_dur=6", "-ar", "48000", "-ac", "1",
         "-c:a", "pcm_s16le", str(dst)],
        check=True)
    return dst


def state(page) -> dict:
    return page.evaluate("() => ({s: window.__mira.S.state, node: window.__node,"
                         " t: window.__mira.S.turns, err: window.__mira.S.errors,"
                         " emo: window.__mira.S.emotion, scene: window.__mira.S.scene,"
                         " ending: window.__mira.S.ending || null})")


def wait_idle(page, timeout=180):
    page.wait_for_function("() => window.__mira && window.__mira.S.state === 'idle'",
                           timeout=timeout * 1000)


def wait_state(page, name, timeout=45):
    page.wait_for_function("n => window.__mira && window.__mira.S.state === n",
                           arg=name, timeout=timeout * 1000)


def ended(page) -> bool:
    return page.evaluate("() => document.body.classList.contains('ended')")


def say(page, text, hold=350, human=False, gap=None):
    """打字 + 发送 + 等她说完（音频播完才回 idle）。

    hold 只在人打字时才有意义（要拍到逐字输入）；fill 是瞬时的，停 1.2s 纯属浪费，
    四段 38 轮攒起来就是 45 秒。
    """
    log("  type:", text)
    if human:
        page.click("#textInput")
        page.type("#textInput", text, delay=60)
        hold = max(hold, 1100)
    else:
        page.fill("#textInput", text)
    page.wait_for_timeout(hold)
    page.click("#sendBtn")
    page.wait_for_timeout(320 if not FAST else 120)
    if not ended(page):
        wait_idle(page)
    page.wait_for_timeout((gap if gap is not None else 0) * 1000)


def send_now(page, text, hold=500):
    """只发不等（用来盯着演出层，或准备打断）。"""
    log("  send:", text)
    page.fill("#textInput", text)
    page.wait_for_timeout(hold if not FAST else 200)
    page.click("#sendBtn")
    page.wait_for_timeout(450 if not FAST else 150)


def press_mic(page, hold=4.8):
    box = page.locator("#micBtn").bounding_box()
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    log(f"  mic press ({hold:.1f}s)")
    page.mouse.move(cx, cy)
    page.mouse.down()
    page.wait_for_timeout(int(hold * 1000))
    page.mouse.up()
    page.wait_for_timeout(300 if not FAST else 120)


def toggle_inject(page, key: str):
    page.click("#debugBtn")
    page.wait_for_timeout(700 if not FAST else 200)
    page.click(f'[data-inject="{key}"]')
    page.wait_for_timeout(300 if not FAST else 100)
    page.click("#debugClose")
    page.wait_for_timeout(300 if not FAST else 100)
    log("  inject toggle:", key)


def open_debug(page):
    page.click("#debugBtn")
    page.wait_for_timeout(700 if not FAST else 200)


def close_panel(page):
    page.click("#debugClose")
    page.wait_for_timeout(400 if not FAST else 120)


def wait_ending(page, timeout=60):
    page.wait_for_function("() => document.body.classList.contains('ended')",
                           timeout=timeout * 1000)


class Driver:
    """把「一段演出」需要的页面动作收在一处，顺带记账（回合 / 节点 / 用时）。"""

    def __init__(self, page, seg):
        self.page = page
        self.seg = seg
        self.key = seg["key"]
        self.pick = make_picker(seg["lines"])
        self.gap = seg["gap"]
        self.trace: list[dict] = []
        self.chapters: list[dict] = []
        self.t0 = time.time()

    def node(self):
        return state(self.page)["node"]

    def chapter(self, title, shot=None):
        rel = round(time.time() - self.t0, 1)
        self.chapters.append({"t": rel, "title": title})
        log(f"  == +{rel:6.1f}s [{self.key}] {title}")
        if shot:
            self.page.screenshot(path=str(SHOTS / f"{self.key}_{len(self.chapters):02d}_{shot}.png"))

    def turn(self, text=None, human=False, hold=350):
        node0 = self.node()
        text = text or self.pick(node0)
        say(self.page, text, hold=hold, human=human, gap=self.gap)
        node1 = self.node()
        self.trace.append({"n": len(self.trace) + 1, "kind": "text", "node": node0,
                           "text": text, "next": node1,
                           "t": round(time.time() - self.t0, 1)})
        log(f"  node {node0} -> {node1}")
        return node0, node1

    def barge_in(self) -> bool:
        """说话中打断：等她说出半句，按住麦克风插话。

        转写成功会自己变成下一轮输入（走真 STT）；万一 STT 没吐字就不硬等，
        退回一句话继续走 —— 演示片不能卡在这里。
        """
        page = self.page
        n0 = state(page)["t"]
        press_mic(page)
        try:
            page.wait_for_function("n => window.__mira.S.turns > n", arg=n0,
                                   timeout=120000)
            page.wait_for_timeout(300 if not FAST else 150)
            if not ended(page):
                wait_idle(page)
            self.trace.append({"n": len(self.trace) + 1, "kind": "voice",
                               "node": self.node(), "text": "(STT 转写)"})
            self.page.wait_for_timeout(self.gap * 1000)
            log("  voice turn OK")
            return True
        except Exception as exc:      # noqa: BLE001
            log("  STT 没产生新一轮（", type(exc).__name__, "）→ 退回打字")
            # 页面可能还卡在「识别中」：等状态机自己回来再补一句。
            # 实测状态没回来就补发新轮，新轮也会一直不结束（录音条还盖着输入区）。
            try:
                page.wait_for_function(
                    "() => ['idle', 'speaking', 'listening'].includes(window.__mira.S.state)",
                    timeout=180000)
                if not ended(page):
                    wait_idle(page, timeout=240)
            except Exception:      # noqa: BLE001
                pass
            self.turn("你觉得她今天会来吗？")
            return False

    def finish_ending(self, shot="ending"):
        page = self.page
        if not ended(page):
            try:
                wait_ending(page, timeout=45)
            except Exception:      # noqa: BLE001
                log("  等结局超时")
        page.wait_for_timeout(1200 if not FAST else 300)
        asset = page.evaluate(
            "() => (document.getElementById('endingImg') || {}).src || ''")
        kicker = page.evaluate(
            "() => (document.getElementById('endingKicker') || {}).textContent || ''")
        text = page.evaluate(
            "() => (document.getElementById('endingText') || {}).textContent || ''")
        self.chapter(f"结局静帧：{self.seg['ending_label']}", shot)
        self.ending_dom = {"src": asset, "kicker": kicker, "text": text}
        page.wait_for_timeout(END_HOLD * 1000)


# ---------------------------------------------------------------- 一段的录制
def warm_stt() -> None:
    """把 faster-whisper 的首次加载赶在录制之前。

    为什么必须：它懒加载（app/providers.py: STTProvider），CPU 上第一次 transcribe
    要 2–3 分钟。这个时间落在「按麦克风」那一步，页面会一直卡在「识别中」，
    A 段的打断直接断在那里（实测：等 30s 超时、退回打字也救不回来）。之后模型常驻，
    单次识别 ~1.3s。用同一段假麦克风音频先打一次，把加载付掉。
    """
    src = make_fake_mic()
    probe = OUT / "stt_probe.webm"
    if not probe.exists():
        subprocess.run([str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(src), "-c:a", "libopus", "-b:a", "32k", str(probe)],
                       check=True)
    data = probe.read_bytes()
    boundary = "----mira" + uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="audio"; filename="probe.webm"\r\n',
        b"Content-Type: audio/webm\r\n\r\n", data, b"\r\n",
        f"--{boundary}\r\n".encode(),
        b'Content-Disposition: form-data; name="session_id"\r\n\r\n', b"", b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        f"{BASE}/api/stt", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=420) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        log(f"  STT 预热 {time.time() - t0:.1f}s → 「{out.get('text')}」"
            f"（模型常驻后单次 {out.get('ms')}ms）")
    except Exception as exc:      # noqa: BLE001
        log("  !! STT 预热失败（不致命，但麦克风那一步可能很慢）:",
            type(exc).__name__, str(exc)[:120])


def warmup(browser) -> None:
    """正式开录前先空跑一轮，把冷启动留在录像之外。

    实测第一轮要 20s 上下（LLM 首次加载 + TTS 长连握手 + 素材），直接录进 A 段就是
    十几秒干等。这段视频跑完即删，不进成片；DEMO_NO_WARMUP=1 可跳过。
    """
    if os.environ.get("DEMO_NO_WARMUP") == "1":
        return
    log("===== 预热（不进成片）=====")
    t0 = time.time()
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844}, device_scale_factor=2,
        locale="zh-CN", permissions=["microphone"],
        record_video_dir=str(RAW), record_video_size={"width": 390, "height": 844})
    page = ctx.new_page()
    vh = None
    try:
        page.goto(f"{BASE}/?rec=1", wait_until="networkidle")
        page.click("#startBtn")
        page.wait_for_timeout(600)
        wait_idle(page, timeout=120)
        page.fill("#textInput", "外面雨刚停吧。")
        page.click("#sendBtn")
        wait_idle(page, timeout=120)
    except Exception as exc:      # noqa: BLE001
        log("  预热没跑完（不影响正式录制）:", type(exc).__name__, str(exc)[:120])
    finally:
        vh = page.video
        ctx.close()
    try:
        if vh:
            Path(vh.path()).unlink(missing_ok=True)
    except Exception:      # noqa: BLE001
        pass
    log(f"  预热完成 {time.time() - t0:.1f}s")


def _inject(payload: dict) -> dict:
    """在**建会话之前**设演示参数（服务端进程内全局，只对之后新建的会话生效）。"""
    body = json.dumps({"session_id": "", **payload}).encode("utf-8")
    req = urllib.request.Request(f"{BASE}/api/debug/inject", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        log("  inject", payload, "->", bool(out.get("ok")))
        return out
    except Exception as exc:       # noqa: BLE001
        log("  !! inject 失败（服务没重启？）:", payload,
            type(exc).__name__, str(exc)[:120])
        return {}


def set_force(value: str | None) -> dict:
    """把 phone_call 的 70/30 钉死（A 段 ok / C 段 fail）。"""
    return _inject({"force_call": value or ""})


def set_clock(step: float | None) -> dict:
    """换剧情时钟步长：原速下用更少的轮次走完 20:30→20:56。"""
    return _inject({"minutes_per_turn": step if step else ""})


def record_segment(browser, seg) -> dict:
    key = seg["key"]
    info = {"key": key, "ok": False, "why": [], "started": time.time()}
    log(f"===== 段 {key}：{seg['chapter']} =====")
    set_force(seg["force"])
    set_clock(seg.get("clock"))

    fake_mic = make_fake_mic()
    ctx = browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        locale="zh-CN",
        permissions=["microphone"],
        record_video_dir=str(RAW),
        # 录屏尺寸必须等于 CSS 视口：给更大的画布，Playwright 只会把 390×844 贴在
        # 左上角、其余涂灰。清晰度靠 mux 时 lanczos 放大到 780×1688 补。
        record_video_size={"width": 390, "height": 844},
    )
    if AUDIO_TAP:
        ctx.add_init_script(AUDIO_PATCH)
    page = ctx.new_page()
    page.on("pageerror", lambda e: log("  pageerror:", str(e)[:200]))
    page.on("console", lambda m: log("  console", m.type, m.text[:160])
            if m.type == "error" else None)

    drv = Driver(page, seg)
    audio_b64 = None
    raw_video = None
    try:
        page.goto(f"{BASE}/?rec=1", wait_until="networkidle")
        # app.js 会把 window.__miraRec 换成它自己那个（只录 mp3 路），换成混音版
        if AUDIO_TAP:
            page.evaluate("() => { window.__miraRec = window.__MYREC; }")
        page.evaluate(PATCH)
        if AUDIO_TAP:
            page.evaluate("() => { try { window.__miraRec.start(); } catch (e) {} }")
        run_plan(drv, seg)
        info["stats"] = page.evaluate("""() => ({
            state: window.__mira.S.state, turns: window.__mira.S.turns,
            errors: window.__mira.S.errors, node: window.__node,
            seen: window.__seen, log: window.__log,
            ending: window.__mira.S.ending || null,
            provider: window.__mira.S.provider,
            latency: (document.getElementById('latency') || {}).textContent || '',
        })""")
        info["ending_dom"] = getattr(drv, "ending_dom", {})
        info["trace"] = drv.trace
        info["chapters"] = drv.chapters
        info["clap_driver_t"] = getattr(drv, "clap_t", None)
        if AUDIO_TAP:
            try:
                audio_b64 = page.evaluate("async () => await window.__miraRec.stop()")
            except Exception as exc:      # noqa: BLE001
                log("  取音轨失败:", type(exc).__name__, str(exc)[:120])
    finally:
        try:
            video = page.video
            raw_video = Path(video.path()) if video else None
        except Exception:      # noqa: BLE001
            raw_video = None
        ctx.close()

    if raw_video and raw_video.exists():
        dst = RAW / f"{key}_video.webm"
        shutil.copyfile(raw_video, dst)
        info["video"] = str(dst)
    else:
        info["why"].append("没录到画面")
    if audio_b64:
        (RAW / f"{key}_audio.webm").write_bytes(base64.b64decode(audio_b64))
        info["audio"] = str(RAW / f"{key}_audio.webm")
        info["audio_kb"] = (RAW / f"{key}_audio.webm").stat().st_size // 1024
        log("  audio:", info["audio_kb"], "KB")
    elif AUDIO_TAP:
        info["why"].append("没录到音轨")

    info["wall"] = round(time.time() - info["started"], 1)
    info["why"] += check_result(seg, info)
    info["warn"] = check_provider(seg, info)
    for w in info["warn"]:
        log("  ! 提醒:", w)
    info["ok"] = not info["why"]
    log(f"  段 {key} 用时 {info['wall']}s；判定 {'通过' if info['ok'] else '不合格：' + '；'.join(info['why'])}")
    return info


def run_plan(drv: Driver, seg):
    page = drv.page
    for step in seg["plan"]:
        act = step[0]
        # 已经进结局：HUD 收起、sendBtn 不可点，剩下的「说话」动作一律跳过，
        # 但章节卡和 end 还要走（end 负责截图与静帧停留）
        if ended(page) and act not in ("end", "chapter"):
            log("  已进结局，跳过剩余动作:", act)
            continue
        if act == "chapter":
            drv.chapter(step[1], step[2] if len(step) > 2 else None)
        elif act == "start":
            page.wait_for_timeout(seg["gate_hold"] * 1000)
            page.click("#startBtn")     # 用户手势之后 AudioContext 才是 running 的
            page.wait_for_timeout(150)
            page.evaluate(CLAPPER)      # 拍板：白屏闪 + 1kHz 蜂鸣（同一瞬间）
            # 记下拍板在**驱动器时间轴**上的时刻：剪片段时要用它把轮次边界
            # 换算到视频时间轴（白色闪点的画面时间由 mux 阶段量出来）。
            drv.clap_t = round(time.time() - drv.t0, 2)
            page.wait_for_timeout(700 if not FAST else 250)
            wait_idle(page, timeout=120)
            page.wait_for_timeout((2.5 if not FAST else 0.4) * 1000)
        elif act == "say":
            text = step[1] if len(step) > 1 and step[1] else None
            opts = step[2] if len(step) > 2 else {}
            drv.turn(text, **opts)
        elif act == "send":
            node0 = drv.node()
            send_now(page, step[1])
            drv.trace.append({"n": len(drv.trace) + 1, "kind": "send",
                              "node": node0, "text": step[1]})
            try:
                wait_state(page, "speaking", timeout=45)
                page.wait_for_timeout(1100 if not FAST else 300)   # 让她先说半句
            except Exception:      # noqa: BLE001
                log("  没等到 speaking，仍按打断走")
        elif act == "mic":
            drv.barge_in()
        elif act == "mode":
            # 切换入口在观测面板的 Provider 行：打开 → 选档 → 停一会儿让画面拍到 →
            # 关掉再点「进入场景」，因为 startBtn 读的就是 #modeSelect 的值。
            # 用 JS click 不用 mouse click：开始页的 .gate 层盖住了顶栏，坐标点击会被
            # Playwright 判成「被其它元素遮挡」并超时（实测 30s）；DOM 点击效果一样。
            page.evaluate("() => document.getElementById('debugBtn').click()")
            page.wait_for_timeout(500 if not FAST else 150)
            page.select_option("#modeSelect", step[1])
            page.wait_for_timeout(1600 if not FAST else 400)
            page.evaluate("() => document.getElementById('debugClose').click()")
            page.wait_for_timeout(350 if not FAST else 100)
        elif act in ("inject_on", "inject_off"):
            toggle_inject(page, step[1])
        elif act == "wait":
            # 等一个 toast 自己消失再截图/继续：注入/解除故障会弹一条 3.2s 的提示，
            # 它正好压在字幕区上（实测 A_09 的电话贴片截图被 "tts_fail → 关" 挡住）。
            page.wait_for_timeout(int(step[1]) if len(step) > 1 else 1200)
        elif act == "panels":
            drv.chapter("观测面板：真实 provider、首包/总耗时、事件流水、故障注入入口", "debug")
            open_debug(page)
            page.wait_for_timeout(4500 if not FAST else 700)
            close_panel(page)
            drv.chapter("历史记录：这一场真聊过的每一轮都能回看 / 导出", "history")
            try:
                page.click("#historyBtn")
                page.wait_for_timeout(1800 if not FAST else 400)
                page.screenshot(path=str(SHOTS / f"{drv.key}_history.png"))
                page.wait_for_timeout(3000 if not FAST else 500)
                page.click("#historyClose")
                page.wait_for_timeout(500 if not FAST else 100)
            except Exception as exc:      # noqa: BLE001
                log("  history panel:", type(exc).__name__)
        elif act == "end":
            drv.finish_ending()
        else:
            raise ValueError(f"陌生动作 {act}")


# ---------------------------------------------------------------- 判定
def check_result(seg, info) -> list[str]:
    """一段是否真的走到了**这个**分支 / 这个结局。"""
    problems = []
    st = info.get("stats") or {}
    if not st:
        return ["没读到页面状态"]
    seen = set(st.get("seen") or [])
    node, kind = st.get("node"), st.get("ending")
    if not info.get("ending_dom"):
        problems.append("没走到结局（ending 静帧没出现）")
    if node != seg["end_node"]:
        problems.append(f"结局节点是 {node}，期望 {seg['end_node']}")
    if kind != seg["end_kind"]:
        problems.append(f"ending kind={kind}，期望 {seg['end_kind']}")
    for n in seg["must_visit"]:
        if n not in seen:
            problems.append(f"分支节点 {n} 没在事件流里出现")
    for n in seg["must_not"]:
        if n in seen:
            problems.append(f"不该出现的节点出现了：{n}")
    src = (info.get("ending_dom") or {}).get("src", "")
    # 页面给的是 /static/assets/ending/stranger.webp?v=16（带缓存戳），只比文件名
    name = Path(src.split("?")[0]).name
    if seg["ending_asset"] and name != seg["ending_asset"]:
        problems.append(f"结局静帧是 {name or '空'}，期望 {seg['ending_asset']}")
    must_have = seg.get("ending_text_has")
    if must_have:
        text = (info.get("ending_dom") or {}).get("text", "")
        if must_have not in text:
            problems.append(f"结局文案里没有「{must_have}」（实际：{text[:40]}）")
    return problems


def check_provider(seg, info) -> list[str]:
    """这一段的台词到底是谁生成的（事件流里 done.provider）。

    为什么要单列：A 段要拍到 provider=cloud，B/C/D 要本地 Qwen3。如果云端回落了，
    片子照样能发，但得在报告里说清楚——不能把回落当成云端的成绩。
    """
    want = seg.get("provider")
    log_ = (info.get("stats") or {}).get("log") or []
    got = [x.get("provider") for x in log_ if x.get("provider")]
    if not want or not got:
        return []
    # card = 开场白（卡包直出）、preset = 命中预设库、story = 用户说「我先走了」的
    # 结局分支（main.py 直接发 ending，不经过模型）——三者都不是模型产出，不参与比较
    bad = [p for p in got if p not in (want, "card", "preset", "story")]
    if bad:
        from collections import Counter
        return [f"provider 期望 {want}，实际 {dict(Counter(bad))}"]
    return []


# ---------------------------------------------------------------- 本地模拟（--sim）
# 一份 app/main.py run_turn 里故事推进的**等价复刻**（只是不调模型、不出声）：
#   offer 门 → tick → forced → resolve，顺序与 main.py:1068-1091 一致。
# 改那边（app/main.py 的故事推进段）要回来同步这里，否则模拟会说谎。
BORROW_KWS = ("借你", "用我的", "给你", "手机借", "拿我",
              "用你的", "你的手机", "打个电话", "打电话")


def _load_card() -> dict:
    import yaml
    return yaml.safe_load((ROOT / "cards" / "mira_cafe" / "card.yaml")
                          .read_text(encoding="utf-8"))


def sim(only: list[str] | None = None) -> int:
    sys.path.insert(0, str(ROOT))
    from app import story as story_mod

    card = _load_card()
    bad = 0
    print(f"{'段':<3}{'回合':<5}{'结局节点':<18}{'kind':<10}{'用时':<7}结论")
    for seg in SEGMENTS:
        if only and seg["key"] not in only:
            continue
        # 2026-10-22：两个剧情钩子已从 app/story.py 删除（结局必须由对话自然触发）。
        # v20 不再需要它们：借了手机→ending_friend（打通/打不通都是），没借→stranger，
        # 说「我先走了」→ending_leave，三条都由对话本身决定，不钉任何骰子。
        st = story_mod.StoryState(card["story"], card["clock"])
        pick = make_picker(seg["lines"])
        t0 = time.time()
        ended_at = None
        turns = 0
        for step in seg["plan"]:
            act = step[0]
            if act in ("chapter", "start", "inject_on", "inject_off", "panels", "end",
                       "mode"):
                continue
            if act == "mic":
                text = "你是在等什么人吗？"      # STT 实际输出只能是近似
            elif act == "send":
                text = step[1]
            elif act == "say":
                text = step[1] if len(step) > 1 and step[1] else pick(st.node_id)
            else:
                continue
            turns += 1
            if st.leave_requested(text):
                step_ = st.begin_leave()
                ended_at = step_.ending
                break
            if "phone_dead" in st.visited and not st.flags.get("offered_phone"):
                if any(k in text for k in BORROW_KWS):
                    st.offer_phone()
            st.tick()
            fstep = st.forced()
            st.resolve(text=text)
            st.end_turn()
            if st.is_ending():
                ended_at = st.ending
                break
        seen = set(st.visited)
        problems = []
        # 引擎里 ending_leave 的 kind 也是 stranger（剧情节点自己的 ending 字段）；
        # 页面上「leave」是 app.js 按节点名派生的展示值。模拟器比引擎，按 stranger 比。
        want_kind = "stranger" if seg["end_kind"] == "leave" else seg["end_kind"]
        if ended_at != want_kind:
            problems.append(f"kind={ended_at}≠{want_kind}")
        if st.node_id != seg["end_node"]:
            problems.append(f"节点={st.node_id}≠{seg['end_node']}")
        for n in seg["must_visit"]:
            if n not in seen:
                problems.append(f"缺节点{n}")
        for n in seg["must_not"]:
            if n in seen:
                problems.append(f"多节点{n}")
        verdict = "OK" if not problems else "FAIL " + "；".join(problems)
        if problems:
            bad += 1
        print(f"{seg['key']:<3}{turns:<5}{st.node_id:<18}{str(ended_at):<10}"
              f"{time.time() - t0:<7.2f}{verdict}")
        print(f"     路径: {' -> '.join(st.visited)}")
    return 1 if bad else 0


# ---------------------------------------------------------------- 后期
def probe(f: Path) -> dict:
    info = subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(f)],
                          capture_output=True, text=True).stderr
    out = {"raw": info}
    for line in info.splitlines():
        if "Duration" in line:
            h, m, s = line.split("Duration:")[1].split(",")[0].strip().split(":")
            out["seconds"] = int(h) * 3600 + int(m) * 60 + float(s)
        if "Stream #" in line and "Video" in line:
            out["video"] = line.strip()
        if "Stream #" in line and "Audio" in line:
            out["audio"] = line.strip()
    return out


def make_clap_probe(video: Path, audio: Path) -> tuple[float, float]:
    """找出拍板在画面和音轨里各自的时刻：画面取最亮的一帧，音轨取最响的 1kHz 爆点。"""
    import numpy as np

    raw = subprocess.run(
        [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-t", "20",
         "-i", str(video), "-vf", "scale=390:844,format=gray", "-f", "rawvideo", "-"],
        capture_output=True).stdout
    a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
    n = len(a) // (390 * 844)
    if not n:
        return 0.0, 0.0
    px = a[:n * 390 * 844].reshape(n, 844, 390)[:, 0:14, 0:14].mean(axis=(1, 2))
    t_flash = float(np.argmax(px)) / 25.0

    wav = OUT / "_clap.wav"
    subprocess.run([str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
                    "-t", "20", "-i", str(audio), "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", str(wav)], check=True)
    import wave
    w = wave.open(str(wav))
    x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    hop = int(16000 * 0.02)
    if len(x) < hop * 2:
        return t_flash, 0.0
    freqs = np.fft.rfftfreq(hop, 1 / 16000)
    band = (freqs > 900) & (freqs < 1100)
    best, t_beep = -1.0, 0.0
    for i in range(0, len(x) - hop, hop):
        seg = x[i:i + hop] * np.hanning(hop)
        e = float(np.abs(np.fft.rfft(seg))[band].max())
        if e > best:
            best, t_beep = e, i / 16000
    log(f"  拍板：画面 {t_flash:.2f}s / 音轨 {t_beep:.2f}s（1kHz 峰值 {best:.1f}）")
    return t_flash, t_beep


def _ff(args: list[str]) -> None:
    subprocess.run([str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error"] + args,
                   check=True)


def _clap(info: dict) -> tuple[float, float]:
    """拍板在画面/音轨里的时刻（只量一次，--pack 重跑时读缓存）。"""
    if not info.get("clap"):
        info["clap"] = list(make_clap_probe(Path(info["video"]), Path(info["audio"])))
    return tuple(info["clap"])            # type: ignore[return-value]


def _norm(info: dict, out: Path) -> Path:
    """页面视频 + 页面真实音频 → 参数统一的中间件（780×1688 / 25fps / AAC 48k mono）。

    音画对齐用 **adelay 补真实静音**，不能用 `-itsoffset`：后者只是给输入时间戳
    加偏移，后面 `_speed_pad` 再编码一次时 ffmpeg 会把流时间戳重新归零，偏移就没了
    （实测拍板蜂鸣在段里落到 0.22s、白闪在 6.92s，差 6.7s —— 整个成片音画错位）。
    adelay 把静音写进样本里，怎么再编码都不会丢。

    apad + shortest：音轨比画面短（起录晚）时补静音，让输出长度严格等于画面长度，
    否则段与段拼接时会累积错位。
    """
    video = Path(info["video"])
    # 中间件比原始 webm 新就直接复用：调速度/剪法时不用把 300s 的画面再解一遍
    if out.exists() and out.stat().st_mtime >= video.stat().st_mtime:
        return out
    t_flash, t_beep = _clap(info)
    off = t_flash - t_beep
    ms = int(round(max(0.0, off) * 1000))
    # apad 必须给整段时长（whole_dur）：无限长的 apad 接在 filter_complex 的音频输出上，
    # -shortest 等不到 EOF，ffmpeg 会一直编码下去（实测挂了 12 分钟、日志零输出）。
    vdur = probe(Path(info["video"])).get("seconds") or 0.0
    _ff(["-i", info["video"], "-i", info["audio"],
         "-filter_complex",
         f"[0:v]scale={W}:{H}:flags=lanczos,fps={FPS},format=yuv420p[v];"
         f"[1:a]adelay={ms}:all=1,apad=whole_dur={vdur:.2f}[a]",
         "-map", "[v]", "-map", "[a]", "-shortest",
         "-c:v", "libx264", "-preset", "medium", "-crf", "19",
         "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "1",
         "-movflags", "+faststart", str(out)])
    info["clap_offset"] = round(off, 2)
    return out


def _cut_ranges(info: dict, keep: list[int], dur: float) -> list[tuple[float, float]]:
    """把录制时间轴上「只保留这几轮」翻译成视频时间轴上的保留区间。

    轮次边界取 trace 里每轮**结束**的时刻（那时正好回 idle、没有台词在响），
    剪断处不会有半句话被切断。驱动器时间 → 视频时间靠拍板换算：
    拍板那一刻 driver_t = clap_driver_t，video_t = t_flash，所以两者只差一个常量。
    """
    t_flash = _clap(info)[0]
    shift = (info.get("clap_driver_t") or 0.0) - t_flash

    def to_video(t: float) -> float:
        return max(0.0, min(dur, t - shift))

    ends = [to_video(e["t"]) for e in (info.get("trace") or [])]
    bounds = [0.0] + ends + [dur]
    ranges: list[tuple[float, float]] = []
    for turn in keep:
        a = bounds[max(0, turn - 1)]
        b = dur if turn == keep[-1] else bounds[min(turn, len(bounds) - 1)]
        if b - a < 0.1:
            continue
        if ranges and a - ranges[-1][1] < 0.8:
            ranges[-1] = (ranges[-1][0], b)
        else:
            ranges.append((a, b))
    return ranges


def _speed_pad(src: Path, dst: Path, speed: float, pad: float) -> None:
    """（默认 speed=1.0 / pad=0 时就是一次纯重编码）压时长 / 冻结局，仅排障时用。"""
    vf = "[0:v]"
    if speed != 1.0:
        vf += f"setpts=PTS/{speed:.3f},"
    if pad > 0.05:
        vf += f"tpad=stop_mode=clone:stop_duration={pad:.2f},"
    vf += f"fps={FPS},format=yuv420p[v]"
    af = (f"atempo={speed:.3f}," if speed != 1.0 else "")
    af += f"apad=pad_dur={pad + 1.0:.2f}"
    _ff(["-i", str(src), "-filter_complex", vf,
         "-map", "[v]", "-map", "0:a", "-af", af, "-shortest",
         "-c:v", "libx264", "-preset", "medium", "-crf", "19",
         "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "1",
         "-movflags", "+faststart", str(dst)])


def mux_segment(info: dict) -> dict:
    """一段：音画对齐 → （可选）剪掉重复铺垫 → 压时长 → 结局冻结。

    为什么有「剪」这一步：故事时钟每轮 +2.5 分钟，从 20:30 到 20:56 关灯最少要
    11 轮，光靠加速压不到 50s。B/C 的前几轮与 A 段完全同质，剪掉后章节卡上写明
    「片段为压缩时长，前段铺垫已剪」——**剪掉的是重复铺垫，不是分支本身**。
    """
    key = info["key"]
    seg = SEGBY.get(key, {})
    speed = float(seg.get("speed") or (SPEED_A if key == "A" else SPEED_FAST))
    out = SEG / f"seg_{key}.mp4"
    norm = _norm(info, RAW / f"{key}_norm.mp4")
    # 加速会按比例缩短结局静帧，这里把最后一帧冻回去，保证成片里 8–10s
    pad = max(0.0, END_TARGET - END_HOLD / speed)
    keep = seg.get("keep_turns")
    if keep:
        dur = probe(norm).get("seconds") or 0.0
        ranges = _cut_ranges(info, keep, dur)
        parts = []
        for i, (a, b) in enumerate(ranges):
            p = RAW / f"{key}_part{i}.mp4"
            _ff(["-i", str(norm), "-filter_complex",
                 f"[0:v]trim=start={a:.2f}:end={b:.2f},setpts=PTS-STARTPTS[v];"
                 f"[0:a]atrim=start={a:.2f}:end={b:.2f},asetpts=PTS-STARTPTS[a]",
                 "-map", "[v]", "-map", "[a]",
                 "-c:v", "libx264", "-preset", "medium", "-crf", "19",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                 "-ar", "48000", "-ac", "1", str(p)])
            parts.append(p)
            log(f"  段 {key} 保留 {a:6.1f}s→{b:6.1f}s")
        joined = RAW / f"{key}_joined.mp4"
        concat(parts, joined)
        src = joined
    else:
        src = norm
    _speed_pad(src, out, speed, pad)
    p = probe(out)
    info.update({"mp4": str(out), "mp4_seconds": p.get("seconds"),
                 "speed": speed, "end_pad": round(pad, 2),
                 "kept_turns": keep or "all"})
    log(f"  段 {key} mux: {p.get('seconds')}s，速度 {speed}x，结局补 {pad:.1f}s，"
        f"保留轮次 {keep or '全部'}")
    return info


def _font(path: str, size: int):
    from PIL import ImageFont
    return ImageFont.truetype(path, size)


def make_card_png(seg, total: int) -> Path:
    """章节卡：一段一张，写清「A/B/C/D + 这一段的结局名」。

    手机竖屏尺寸与成片一致（780×1688），拼接时不用再缩放。
    """
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), (9, 10, 13))
    d = ImageDraw.Draw(img)
    for y in range(H):                       # 竖向渐变：上暖（灯）下冷（雨夜）
        t = y / H
        d.line([(0, y), (W, y)],
               fill=(int(10 + 16 * t), int(11 + 15 * t), int(14 + 20 * t)))

    f_tag = _font("C:/Windows/Fonts/msyh.ttc", 30)
    f_letter = _font("C:/Windows/Fonts/msyhbd.ttc", 300)
    f_sub = _font("C:/Windows/Fonts/msyh.ttc", 38)
    f_foot = _font("C:/Windows/Fonts/msyh.ttc", 28)
    warm, white, gray = (232, 196, 122), (238, 238, 240), (140, 146, 156)

    def center(y, text, font, fill):
        b = d.textbbox((0, 0), text, font=font)
        d.text(((W - (b[2] - b[0])) / 2 - b[0], y), text, font=font, fill=fill)

    def fit(text, start, maxw=680):
        """标题按宽度收字号：C 段标题 17 个字，60px 会顶出画外（第一版就剪掉了两头）。"""
        size = start
        while size > 26:
            f = _font("C:/Windows/Fonts/msyhbd.ttc", size)
            b = d.textbbox((0, 0), text, font=f)
            if b[2] - b[0] <= maxw:
                return f
            size -= 4
        return _font("C:/Windows/Fonts/msyhbd.ttc", 26)

    center(140, "MIRA · 雨后的咖啡馆", f_tag, gray)
    b = d.textbbox((0, 0), seg["key"], font=f_letter)
    center(340 - b[1], seg["key"], f_letter, warm)
    d.line([(W // 2 - 60, 790), (W // 2 + 60, 790)], fill=warm, width=3)
    center(870, seg["chapter"], fit(seg["chapter"], 56), white)
    center(985, seg["ending_label"], f_sub, warm)
    if seg.get("note"):
        # 剪过铺垫的段落必须自己说清楚，不能让观众以为故事跳帧是 bug
        f_note = _font("C:/Windows/Fonts/msyh.ttc", 24)
        words = seg["note"]
        lines = [words[i:i + 26] for i in range(0, len(words), 26)]
        for i, ln in enumerate(lines):
            center(1090 + i * 34, ln, f_note, gray)
    center(1500, f"第 {seg['index']} 段 / 共 {total} 段 · 演示片 v20 · 云端 DeepSeek", f_foot, gray)
    png = CARDS / f"{seg['index']:02d}_{seg['key']}.png"
    img.save(png)
    return png


def card_video(seg, png: Path) -> Path:
    """章节卡 → 带静音音轨的小 mp4（没有音轨的片段会把整条 concat 搞崩）。"""
    out = CARDS / f"{seg['index']:02d}_{seg['key']}.mp4"
    subprocess.run(
        [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
         "-loop", "1", "-framerate", str(FPS), "-i", str(png),
         "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=48000",
         "-t", f"{CARD_HOLD:.2f}",
         "-c:v", "libx264", "-preset", "medium", "-crf", "19",
         "-pix_fmt", "yuv420p", "-r", str(FPS),
         "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "1",
         "-shortest", "-movflags", "+faststart", str(out)], check=True)
    return out


def concat(pieces: list[Path], final: Path) -> None:
    """拼接。各段参数已被 mux/card_video 统一（780×1688 / 25fps / AAC 48k mono），
    所以先试 -c copy（快且无损）；时间戳对不上再退回重编码。"""
    lst = OUT / "_concat.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in pieces), encoding="utf-8")
    head = [str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error"]
    try:
        subprocess.run(head + ["-f", "concat", "-safe", "0", "-i", str(lst),
                               "-c", "copy", "-movflags", "+faststart", str(final)],
                       check=True, capture_output=True)
        p = probe(final)
        want = sum((probe(x).get("seconds") or 0) for x in pieces)
        if p.get("seconds") and abs(p["seconds"] - want) < 1.5:
            log(f"  concat(-c copy) OK：{p['seconds']}s（各段合计 {want:.1f}s）")
            return
        log(f"  concat 时间戳不对（{p.get('seconds')} vs {want:.1f}）→ 重编码")
    except subprocess.CalledProcessError as exc:
        log("  concat -c copy 失败 → 重编码：", exc.stderr.decode("utf-8", "ignore")[:200])
    ins, fc = [], []
    for i, p in enumerate(pieces):
        ins += ["-i", str(p)]
        fc.append(f"[{i}:v]setsar=1[v{i}];[{i}:a]aresample=48000,"
                  f"aformat=sample_fmts=fltp:channel_layouts=mono[a{i}]")
    fc.append("".join(f"[v{i}][a{i}]" for i in range(len(pieces)))
              + f"concat=n={len(pieces)}:v=1:a=1[v][a]")
    subprocess.run(head + ins + ["-filter_complex", ";".join(fc),
                   "-map", "[v]", "-map", "[a]",
                   "-c:v", "libx264", "-preset", "medium", "-crf", "19",
                   "-pix_fmt", "yuv420p", "-r", str(FPS),
                   "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", str(final)], check=True)


def frame_at(mp4: Path, t: float, dst: Path) -> Path:
    subprocess.run([str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
                    "-ss", f"{max(0.0, t):.2f}", "-i", str(mp4),
                    "-frames:v", "1", str(dst)], check=True)
    return dst


def frame_corr(frame: Path, asset: Path) -> float:
    """灰度 48×48 归一化互相关：结局屏有暗罩和文字，直接比像素差不靠谱，
    但「暖调门口回头」和「冷调背影走远」两张图的明暗结构完全不同。"""
    import numpy as np
    from PIL import Image

    def vec(p):
        im = Image.open(p).convert("L").resize((48, 48))
        a = np.asarray(im, dtype=np.float32).ravel()
        a -= a.mean()
        s = a.std() or 1.0
        return a / s

    return float(np.dot(vec(frame), vec(asset)) / (48 * 48))


def detect_silences(mp4: Path, min_s: float = 2.0) -> list[tuple[float, float]]:
    """列出 >min_s 的静音窗口（题目允许「刻意的沉默」，所以只报数不判错）。"""
    out = subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(mp4),
                          "-af", f"silencedetect=noise=-42dB:d={min_s}",
                          "-f", "null", "-"], capture_output=True, text=True).stderr
    res, start = [], None
    for line in out.splitlines():
        if "silence_start:" in line:
            start = float(line.split("silence_start:")[1].split()[0])
        elif "silence_end:" in line and start is not None:
            end = float(line.split("silence_end:")[1].split()[0])
            dur = float(line.split("silence_duration:")[1].split()[0])
            res.append((round(start, 2), round(dur, 2)))
            start = None
    return res


def check_final(final: Path, info: dict, offsets: dict) -> list[str]:
    """成片自检：时长 / 音轨 / 每段结局抽帧。"""
    problems = []
    p = probe(final)
    sec = p.get("seconds")
    log(f"成片 {final.name}: {sec}s, {final.stat().st_size // 1024} KB")
    log("  视频流:", p.get("video"))
    log("  音频流:", p.get("audio") or "【没有音轨】")
    if not p.get("audio"):
        problems.append("没有音轨")
    if not sec:
        problems.append("读不到时长")
    elif not 180 <= sec <= 300:
        problems.append(f"时长 {sec}s 不在 180–300s")
    sil = detect_silences(final)
    for st, dur in sil:
        log(f"  静音窗口 {st:6.1f}s 持续 {dur:5.1f}s（章节卡/开始页/结局静帧属刻意沉默）")

    for seg in SEGMENTS:
        key = seg["key"]
        si = info.get(key) or {}
        # offsets 的 key 是 f"seg_{key}"（pack 里按片名登记的），不是裸 key。
        # 这里取错过一次：取不到就退回 (0,0)，于是每段都从**段首**抽帧去比结局素材，
        # A 段（friend）因此被判成「更像 stranger」——错的是自检，不是成片。
        start, dur = offsets.get(f"seg_{key}") or offsets.get(key) or (0.0, 0.0)
        t = start + max(0.0, dur - END_HOLD * 0.5)
        dst = SHOTS / f"final_frame_{key}.png"
        try:
            frame_at(final, t, dst)
        except subprocess.CalledProcessError:
            problems.append(f"{key} 段抽帧失败 @{t:.1f}s")
            continue
        cand = {n: frame_corr(dst, ROOT / "cards" / "mira_cafe" / "assets" / "ending" / n)
                for n in ("friend.webp", "stranger.webp")}
        best = max(cand, key=cand.get)
        ok = best == seg["ending_asset"]
        log(f"  段 {key} 结局帧 @{t:6.1f}s → 最像 {best}"
            f"（friend {cand['friend.webp']:.3f} / stranger {cand['stranger.webp']:.3f}）"
            f"{'✓' if ok else '✗'}")
        if not ok:
            problems.append(f"{key} 段结局帧更像 {best}，期望 {seg['ending_asset']}")
        # 音画同源检查：拍板的白闪与 1kHz 必须落在同一时刻（差 >0.3s 就是错位）。
        # 剪过的段（B/C/D）用剪之前的中间件量：拍板在段首被剪掉了，拿成品量到的是假峰。
        norm = RAW / f"{key}_norm.mp4"
        mp4 = norm if norm.exists() else Path(si.get("mp4") or "")
        if mp4.exists():
            tf, tb = make_clap_probe(mp4, mp4)
            log(f"  段 {key} 音画同步复查（{'剪前中间件' if mp4 == norm else '成品'}）："
                f"白闪 {tf:.2f}s / 1kHz {tb:.2f}s（差 {abs(tf - tb):.2f}s）")
            if abs(tf - tb) > 0.3:
                problems.append(f"{key} 段音画错位 {abs(tf - tb):.2f}s")
    return problems


# ---------------------------------------------------------------- 主流程
def summary(results: dict) -> None:
    """四段一句话结论：判定 / 时长 / 走到哪些节点 / 问题 / 提醒。"""
    log("===== 四段结果 =====")
    total = 0.0
    for seg in SEGMENTS:
        r = results.get(seg["key"], {})
        secs = r.get("mp4_seconds") or 0
        total += secs
        log(f"  {seg['key']}  {seg['chapter']:<22} {'OK ' if r.get('ok') else 'FAIL'} "
            f"{secs:6.1f}s（{r.get('speed', 1.0)}x，保留轮次 {r.get('kept_turns', 'all')}）")
        log(f"     seen={','.join((r.get('stats') or {}).get('seen') or [])}")
        for w in r.get("warn") or []:
            log("     ! 提醒:", w)
        if r.get("why"):
            log("     问题:", "；".join(r["why"]))
    log(f"四段合计 {total:.1f}s + 章节卡 {len(SEGMENTS) * CARD_HOLD:.1f}s ≈ "
        f"{total + len(SEGMENTS) * CARD_HOLD:.1f}s（目标 230–290s）")


def main() -> int:
    global T0
    only = []
    for i, a in enumerate(sys.argv):
        if a == "--only" and i + 1 < len(sys.argv):
            only = [x.strip().upper() for x in sys.argv[i + 1].split(",") if x.strip()]
    if SIM:
        return sim(only or None)
    if CHECK:
        rep = json.loads((OUT / "report.json").read_text(encoding="utf-8"))
        final = OUT / "mira_demo_v20.mp4"
        # offsets 写在 report["final"]["offsets"] 里（pack 的落盘位置），不是顶层
        off = (rep.get("final") or {}).get("offsets") or rep.get("offsets") or {}
        if not off:
            # 上一次 pack 在写 report["final"] 之前就抛了（自检自己引用了已迁移的素材路径），
            # 所以 offsets 没落盘。这里按「章节卡 + 各段时长」现场重算，位置和 pack 一致。
            cursor = 0.0
            for seg in SEGMENTS:
                off[f"card_{seg['key']}"] = (cursor, CARD_HOLD)
                cursor += CARD_HOLD
                dur = (rep.get("segments", {}).get(seg["key"]) or {}).get("mp4_seconds") or 0.0
                off[f"seg_{seg['key']}"] = (cursor, float(dur))
                cursor += float(dur)
            log(f"  offsets 缺失，按段时长重算：总 {cursor:.1f}s")
        problems = check_final(final, rep.get("segments", {}), off)
        log("自检:", "通过" if not problems else "；".join(problems))
        return 0 if not problems else 1
    if CARDS_ONLY:
        results = json.loads((OUT / "report.json").read_text(encoding="utf-8"))["segments"]
        summary(results)
        return pack(results)
    if PACK:
        # 录一次很贵（4 段 ≈ 8 分钟），调时长/剪法/加速比只需重跑这一步
        results = json.loads((OUT / "report.json").read_text(encoding="utf-8"))["segments"]
        for seg in SEGMENTS:
            info = results.get(seg["key"]) or {}
            if not (info.get("video") and info.get("audio")):
                continue
            # 判据可能改过（比如 ending_leave 的 kind 变成 leave），重判一次
            info["why"] = check_result(seg, info)
            info["warn"] = check_provider(seg, info)
            info["ok"] = not info["why"]
            mux_segment(info)
        summary(results)
        return pack(results)

    T0 = time.time()
    fake_mic = make_fake_mic()
    log("base:", BASE, "| fake mic:", fake_mic.name, "| FAST:", FAST, "| FULL:", FULL)
    results: dict[str, dict] = {}
    if (OUT / "report.json").exists() and only:
        # --only 重录时保留其它段的结论，免得报告被清空
        results = dict(json.loads((OUT / "report.json").read_text(encoding="utf-8"))
                       .get("segments", {}))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--autoplay-policy=no-user-gesture-required",
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            f"--use-file-for-fake-audio-capture={fake_mic}",
            "--allow-file-access-from-files",
        ])
        try:
            warm_stt()
            warmup(browser)
            for seg in SEGMENTS:
                if only and seg["key"] not in only:
                    continue
                try:
                    info = record_segment(browser, seg)
                except Exception as exc:      # noqa: BLE001
                    # 一段崩了不能把后面三段一起带走：记下来接着录，最后一起看
                    log(f"  段 {seg['key']} 录制异常:", type(exc).__name__, str(exc)[:200])
                    info = {"key": seg["key"], "ok": False,
                            "why": [f"录制异常 {type(exc).__name__}"],
                            "stats": {}, "video": None, "audio": None}
                results[seg["key"]] = info
                if info.get("video") and info.get("audio"):
                    try:
                        mux_segment(info)
                    except subprocess.CalledProcessError as exc:
                        info["ok"] = False
                        info["why"].append(f"mux 失败 {exc.returncode}")
        finally:
            browser.close()

    segs_ok = all(results.get(s["key"], {}).get("ok") for s in SEGMENTS)
    report = {"base": BASE, "fast": FAST, "full": FULL,
              "gap_a": GAP_A, "gap_fast": GAP_FAST, "end_hold": END_HOLD,
              "segments": results,
              "chapters": [c for s in SEGMENTS for c in
                           (results.get(s["key"], {}).get("chapters") or [])]}
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8")

    print()
    summary(results)
    if not segs_ok:
        log("有段不合格 → 不拼成片；先看上面的问题")
        return 1
    if not FULL:
        log("分段已就绪（production/reports/demo_v15/segments/）。加 --full 才拼成片。")
        return 0
    return pack(results)


def pack(results: dict) -> int:
    """章节卡 + 拼接 + 自检。--pack 可以只重跑这一步（重录很贵，调时长/速度不必重录）。"""
    report = json.loads((OUT / "report.json").read_text(encoding="utf-8")) \
        if (OUT / "report.json").exists() else {}
    report.update({"base": BASE, "fast": FAST, "end_hold": END_HOLD,
                   "speed_fast": SPEED_FAST, "segments": results})
    pieces, offsets, cursor = [], {}, 0.0
    for seg in SEGMENTS:
        png = make_card_png(seg, len(SEGMENTS))
        card = card_video(seg, png)
        pieces.append(card)
        cdur = probe(card).get("seconds") or 0
        offsets[f"card_{seg['key']}"] = (cursor, cdur)
        cursor += cdur
        mp4 = Path(results[seg["key"]]["mp4"])
        pieces.append(mp4)
        dur = probe(mp4).get("seconds") or 0
        offsets[seg["key"]] = (cursor, dur)
        cursor += dur
    final = OUT / "mira_demo_v20.mp4"
    concat(pieces, final)
    sec = probe(final).get("seconds") or 0
    if sec < 180:
        # 差一点不到 3 分钟：尾部冻最后一帧补静音（加画面，不加黑场）
        pad = min(60.0, 200.0 - sec)
        log(f"比 3 分钟短 {200 - sec:.1f}s → 尾部补 {pad:.1f}s 静帧")
        tmp = OUT / "_padded.mp4"
        subprocess.run([str(FFMPEG), "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(final), "-vf",
                        f"tpad=stop_mode=clone:stop_duration={pad:.2f}",
                        "-af", f"apad=pad_dur={pad:.2f}",
                        "-c:v", "libx264", "-preset", "medium", "-crf", "19",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                        "-movflags", "+faststart", str(tmp)], check=True)
        tmp.replace(final)
    problems = check_final(final, results, offsets)
    report["final"] = {"path": str(final), "seconds": probe(final).get("seconds"),
                       "offsets": offsets, "problems": problems}
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
    log(f"成片: {final}  {probe(final).get('seconds')}s  "
        f"{final.stat().st_size // 1024} KB")
    log("自检:", "通过" if not problems else "；".join(problems))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
