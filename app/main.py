"""FastAPI app: one turn = one NDJSON stream; an interrupt is an epoch bump.

Why this shape:
- A single POST /api/turn streams every event of a reply (state, subtitles,
  directive, audio, done), so the client has exactly one ordering authority.
- Every event carries the turn `seq`. The client drops events whose seq is stale,
  and the server refuses to emit once its epoch is stale, so a cancelled turn can
  never "come back" and overwrite the current state.
- 指令采用「首行 JSON + 台词」协议：第一个换行处就能拿到 emotion，TTS 立刻用
  对应语气合成；剧情节拍由服务端推进，模型只负责在当前节拍内自由对话。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import time
import traceback
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import acts, cards, decision, director, paths, perf, persona, presets, providers, selector, trace

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
MEDIA = ROOT / "media"
MEDIA.mkdir(parents=True, exist_ok=True)

LLM_MODE = os.environ.get("LLM_MODE", "auto").lower()      # mock | llama | strata | auto
LLAMA_URL = os.environ.get("LLAMA_URL", "http://127.0.0.1:8080")
STRATA_URL = os.environ.get("STRATA_URL", "http://127.0.0.1:1234")
STRATA_MODEL = os.environ.get("STRATA_MODEL", "qwen3.8-flash-next-iq2_xs-256k-vision")
WHISPER_SIZE = os.environ.get("WHISPER_SIZE", "small")

# 哪些「言语行为」允许直接查预设表（barks）。
# 2026-10-21：预设是按行为查表的，**看不见内容**，所以只允许那些"行为即全部答案"的场景：
# 打招呼、应答、道谢、告别、沉默、出戏、越界。smalltalk（天气/冷/累/加班）不在其中——
# 「今天外面雨真大，我伞都被吹翻了」命中 smalltalk 后回了一句「这几天雨都很大」，
# 用户说的伞和吹翻全被丢掉。实测全量 25.4% 的轮次走预设路径，smalltalk 是重灾区。
PRESET_ACTS = frozenset({"greeting", "ack", "thanks", "bye", "silence", "meta", "personal"})

# ---- 流式 TTS ---------------------------------------------------------------
# 两条音源，同一个 audio/L16 24000 单声道契约，客户端播放器不区分：
#   live     edge-tts 长连（app/tts/live_edge.py）：一次握手 ≈850ms 只付一次，
#            之后每句「发 SSML → 首块」≈230–320ms，边收 mp3 边解码成 PCM 就能播。
#   neural   侧车（tools/tts/serve_neural.py）逐块吐 PCM，服务端同源转发。
# 非流式（整句合成完再返回）在 edge 云端一句要 1.4–2.2s，交互里等于"她卡住了"。
# 开关：MIRA_TTS_LIVE=1|first|0（见 app/tts/live_edge.py:live_mode），
#       MIRA_TTS_STREAM=0 / edge 是更粗暴的总开关（强制整句 mp3）。
NEURAL_TTS_URL = os.environ.get("NEURAL_TTS_URL", "http://127.0.0.1:8790")
TTS_STREAM_MODE = os.environ.get("MIRA_TTS_STREAM", "auto").lower()


def live_tts_mode() -> str:
    """edge 长连的档位：off / first / all（默认 all）。"""
    try:
        from .tts import live_edge

        if live_edge.LIVE.override_mode:
            return live_edge.LIVE.override_mode      # A/B 调试覆盖（/api/debug/inject）
        return live_edge.live_mode()
    except Exception as exc:  # noqa: BLE001  模块坏了就当没有，退回整句 mp3
        print(f"[tts] live_edge 不可用：{type(exc).__name__}: {exc}", flush=True)
        return "off"


def stream_engine() -> str | None:
    """这次该用哪个流式音源：'live' | 'neural' | None（None = 走整句 mp3）。"""
    if TTS_STREAM_MODE in ("0", "off", "false", "mp3"):
        return None
    if TTS_STREAM_MODE == "edge":        # 旧开关语义：强制整句 mp3
        return None
    if TTS_STREAM_MODE == "neural":
        return "neural"
    # auto：跟着当前 TTS 引擎走，而不是跟着"谁在不在"走。
    # 之前按侧车可用性判断，结果引擎切到 edge 了还在走 /api/tts_stream（那条链路只通向
    # 神经侧车），等于用错误的引擎出音。
    if getattr(tts, "engine", "edge") != "edge":
        try:
            from .tts import neural

            return "neural" if neural.available() else None
        except Exception:  # noqa: BLE001
            return None
    return "live" if live_tts_mode() != "off" else None


def stream_tts_enabled() -> bool:
    return stream_engine() is not None


def _live_status() -> dict:
    """debug/QA 用：长连是不是热的、握手花了多少、首块多少（不用翻日志）。"""
    try:
        from .tts import live_edge

        st = dict(live_edge.LIVE.stats)
        st["mode"] = live_tts_mode()
        st["alive"] = live_edge.LIVE.alive()
        return st
    except Exception as exc:  # noqa: BLE001
        return {"mode": "error", "err": f"{type(exc).__name__}: {exc}"}


def live_prewarm() -> None:
    """轮次一开始就把 850ms 握手藏进 LLM 首字（≈1.0s）的等待里。

    不做成"只在常驻连接上省时间"：实测这条 WSS 空闲 ~75s 就会被服务端关掉
    （即使每 12–15s 发 ping），所以答案不是假设它一直活着，而是每次对话前重新热一次。
    冷启动的第一次对话同样有效——这正是重复率最高的场景。
    """
    if live_tts_mode() == "off":
        return
    try:
        from .tts import live_edge

        live_edge.LIVE.prewarm()
    except Exception as exc:  # noqa: BLE001
        print(f"[tts] live prewarm 失败（不影响主链路）：{type(exc).__name__}: {exc}",
              flush=True)


def est_speech_ms(text: str) -> int:
    """按中文语速估一句台词时长：给表演层排 beat 用，收到真实 PCM 后客户端会校正。"""
    n = max(1, len(text or ""))
    return max(500, min(8000, int(n * 185 + 260)))


def stream_url(text: str, emotion: str, index: int) -> str:
    from urllib.parse import urlencode

    q = urlencode({"text": text, "emotion": emotion,
                   "position": "first" if index == 0 else "follow"})
    return f"/api/tts_stream?{q}"

LLMS = {
    "mock": providers.MockLLM(),
    "llama": providers.LlamaLLM(LLAMA_URL),
    "strata": providers.StrataLLM(STRATA_URL, STRATA_MODEL),
}
# 「自动档」：优先 25B（Strata），不可用退 4B（llama），都不可用退 Mock。
# 演示机上只要有一个模型活着就能对话，全部挂掉也不会白屏。
def _port_open(url: str, timeout: float = 0.25) -> bool:
    """启动时探一次 TCP：这个 provider 到底在不在本机跑着。"""
    try:
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port or 80
        if not host:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# 自动链只在**真的有人在这台机器上跑那个服务**时才把它算进去。
# 以前把 Strata(25B) 写死在链首：演示机上 1234 端口从来没起过，于是每一场开局
# 都要先撞一次超时再退到 4B，/api/health 和页面上还写着「自动链：25B → Qwen3 → Mock」，
# 让用户以为自己正在用 25B。现在启动时探一次端口，链和标签都按**实际**来：
# 1234 没起 → `Qwen3 → Mock`（这台机器的常态）。
AUTO_CHAIN = ["strata" if _port_open(STRATA_URL) else "", "llama", "mock"]
AUTO_CHAIN = [k for k in AUTO_CHAIN if k]
LLMS["auto"] = providers.FallbackLLM([LLMS[k] for k in AUTO_CHAIN])

# 显式选了一档模型（llama / strata）时也要能降级到 Mock。
# 原来的行为：客户端 mode=llama 就直接拿 LlamaLLM，模型进程没起时整轮失败，
# 界面上每个字都弹「llama-server 连接失败」——用户看到的就是「它不会说话了」。
# 2026-10-20 真实发生：一键启动脚本发现了 8787 上已经有一个旧实例，于是在
# 「已经跑起来了」那条分支提前 exit，第 3 步（起 4B 模型）根本没执行，页面却
# 按 mode=llama 发请求 → 全部失败。只靠启动脚本堵不住这种情况（旧实例、模型
# 中途崩、GPU 被 ComfyUI 占满都可能），所以网络型 provider 统一加 Mock 兜底。
# 名字仍是原档位；done 事件的 provider 取 FallbackLLM.last，真降级了照样看得见。
for _key in ("llama", "strata"):
    _one = LLMS[_key]
    if getattr(_one, "base_url", None):
        LLMS[_key] = providers.FallbackLLM([_one, LLMS["mock"]], sticky_primary=True)
        LLMS[_key].name = _key

# 链上每一项给前端的短名字（页面上那句「实际链：Qwen3 → Mock」直接用它拼，不再手写）。
CHAIN_LABEL = {"strata": "25B", "llama": "Qwen3", "mock": "Mock"}

# 云端 DeepSeek：key **只存在进程内存里**（临时使用、不落盘、不回传给前端）。
# 为什么允许它存在：本地 4B 偶尔丢指令，演示需要一个「本地没有好模型也能把话说漂亮」
# 的备选；但题目要求无私人密钥也能跑，所以它永远是可选档，没填 key 时自动回落。
CLOUD: dict[str, str] = {"key": "", "model": "deepseek-chat",
                         "base": "https://api.deepseek.com"}


def cloud_llm() -> providers.DeepSeekLLM | None:
    if not CLOUD["key"]:
        return None
    return providers.DeepSeekLLM(CLOUD["key"], CLOUD["base"], CLOUD["model"])


def llm_for(mode: str | None):
    m = (mode or LLM_MODE).lower()
    if m == "cloud":
        llm = cloud_llm()
        # 没填 key 时不许把整轮打挂：回落自动链，/api/health 里看得见
        return llm if llm is not None else LLMS["auto"]
    return LLMS.get(m, LLMS["mock"])


tts = providers.TTSProvider(voice=persona.CHARACTER["voice"])

# ---- 开口前的语气词音频（问题 4 · 纯延迟手段之一） --------------------------
# 背景：edge-tts 是云端合成，实测单句「首音频」p50 2.2s / p90 3.2s（2026-10-21 00:5x
# 在同一台机器上量的，见 docs/LATENCY_2026-10-21.md）。而这句台词已经用语气词
# （「嗯……」）做字幕前的"接话感"了——以前只在字幕上给，声音上没有。
# 现在把它**预合成成静态文件**，在她的反应时间一过就放出来：首音频≈反应时间
# （250–600ms），真实台词随后接上。听感从「她愣了 2 秒」变成「她先应了一声，然后说」。
# 关掉：MIRA_FILLER_AUDIO=0（如果听起来像两个"嗯"就关）。
FILLER_AUDIO = os.environ.get("MIRA_FILLER_AUDIO", "1").lower() not in ("0", "false", "off", "no")
FILLER_PATHS: dict[str, Path] = {}
# 轮换用的语气词（不连着重复；都和 perf.FILLERS 对齐，才能命中预合成缓存）
FILLER_CYCLE = ("嗯……", "嗯，", "啊……", "……嗯。")
# 预设台词库（barks）：启动即加载并校验。坏数据要在这里就炸（和卡包一致），
# 不能等到用户说「你好」的那一轮才抛异常。
PRESETS = presets.load(persona.CARD)
stt = providers.STTProvider(size=WHISPER_SIZE)

app = FastAPI(title="Mira · 实时互动场景")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/media", StaticFiles(directory=MEDIA), name="media")


class TurnCancelled(Exception):
    pass


class Session:
    def __init__(self, sid: str, mode: str):
        self.sid = sid
        self.mode = mode
        self.seq = 0
        self.epoch = 0
        self.beat = 0
        self.turns_in_beat = 0
        self.beat_hits = 0
        self.revealed: list[str] = []
        self.user_facts: list[str] = []      # 观众说过的事（轻量记忆，见 extract_facts）
        self.recent: list[str] = []          # 角色最近说过的台词（反重复）
        self.last_contents: list[str] = []   # 最近几轮导演层给的 content（连问同一件事时去重）
        self.last_topics: list[str] = []     # 最近几轮被问的槽位（同一问题追问答新细节）
        self.last_directive = dict(persona.DEFAULT_DIRECTIVE)
        self.played: set[str] = set()          # 已经放过的视觉事件（不重复播）
        # 预设库（barks）的会话状态：用过的轮次 / 变体游标 / once 记录 / 连续命中数。
        # 放会话里而不是模块级：开第二个会话不能继承上一个会话的冷却。
        self.preset_used: dict[str, int] = {}
        self.preset_cursor: dict[str, int] = {}
        self.preset_once: set[str] = set()
        self.preset_run = 0
        self.history: list[dict[str, str]] = []
        # 历史面板（GET /api/history/{sid}）的数据源：只存「给人看」的口径——
        # assistant 那条只有台词正文。为什么不直接复用 history：history 里 assistant
        # 的 content 是「首行 JSON + 台词」（协议需要），直接渲染会把 JSON 头糊到观众脸上。
        self.transcript: list[dict[str, Any]] = []
        self.task: asyncio.Task | None = None
        self.events: list[dict[str, Any]] = []
        self.inject = {"llm_timeout": False, "tts_fail": False, "slow_tts": False,
                       "gate_fail": False}
        self.lock = asyncio.Lock()

    def log(self, kind: str, **detail) -> None:
        self.events.append({"ts": round(time.time(), 3), "kind": kind, **detail})
        del self.events[:-400]


SESSIONS: dict[str, Session] = {}


def get_session(sid: str, mode: str | None = None) -> Session:
    s = SESSIONS.get(sid)
    if s is None:
        s = Session(sid, mode or LLM_MODE)
        SESSIONS[sid] = s
    elif mode:
        s.mode = mode
    return s


def make_emitter(s: Session, seq: int, epoch: int, queue: asyncio.Queue):
    async def emit(event: dict) -> None:
        if s.epoch != epoch or s.seq != seq:
            raise TurnCancelled()
        event["seq"] = seq
        await queue.put(event)

    return emit


SENT_END = "。！？!?"
SENT_SOFT = "，,；;"

# 事件的中文标签（用于观测面板/日志里的修正说明，来自卡包，不重复维护）
KEYWORD_LABEL = dict(persona.DIRECTIVE_SPEC["event"])


def take_sentences(chunk: str) -> tuple[list[str], int]:
    """Split `chunk` into speakable sentences; return (sentences, consumed_chars).

    2026-10-21（问题 4 · 延迟）：**第一句**的软切分门槛从 14 字降到 8 字。
    为什么只降第一句：首音频要等这一句合成完（edge 云端 p50 ≈2.2s），
    短句合成更快、也更早开始合成；后面几句反正会在第一句播放期间并行合成，
    切太短只会让停停顿顿。实测 8 字首句让首音频再早 0.3–0.6s。
    """
    out: list[str] = []
    start = 0
    for i, ch in enumerate(chunk):
        if ch in SENT_END and i - start >= 2:
            out.append(chunk[start:i + 1])
            start = i + 1
        elif ch in SENT_SOFT and i - start >= (8 if not out else 14):
            out.append(chunk[start:i + 1])
            start = i + 1
    return out, start


def split_all_sentences(text: str) -> list[str]:
    """整段台词切句（首句闸门重写后用：重写结果是完整文本，不是流式增量）。"""
    rest = (text or "").strip()
    out: list[str] = []
    while rest:
        sents, used = take_sentences(rest)
        if not sents:
            out.append(rest)
            break
        out.extend(sents)
        rest = rest[used:]
    return [s.strip() for s in out if s.strip()]


_PAIRS = {"（": "）", "(": ")", "【": "】", "[": "]", "〔": "〕"}


class StageDirectionFilter:
    """吃掉模型写出来的舞台提示：`（低头看了一眼窗外）` / `(sighs)` / `【沉默】`。

    为什么要流式过滤而不是事后清理：字幕和 TTS 是边流边发的，等整句生成完再
    清理已经晚了——观众会**听见**「括号 低头看了一眼窗外 括号」。
    所以状态机在字符层面工作：遇到左括号就丢弃直到右括号，跨 chunk 也能接上。
    """

    def __init__(self) -> None:
        self.out = ""
        self._closer: str | None = None

    def feed(self, text: str) -> str:
        buf = self.out
        for ch in text:
            if self._closer:
                if ch == self._closer:
                    self._closer = None
                continue
            closer = _PAIRS.get(ch)
            if closer:
                self._closer = closer
                continue
            buf += ch
        self.out = buf
        return buf


def strip_stage(text: str) -> str:
    """无状态版括号过滤（用于最终台词；流式路径用 StageDirectionFilter）。"""
    out = []
    closer: str | None = None
    for ch in text or "":
        if closer:
            if ch == closer:
                closer = None
            continue
        c2 = _PAIRS.get(ch)
        if c2:
            closer = c2
            continue
        out.append(ch)
    return "".join(out)


def tidy_reply(text: str) -> str:
    """最后一层保险：去掉括号旁白、星号动作、外层引号和多余空行。"""
    t = strip_stage(text or "").strip()
    t = re.sub(r"[*＊]{1,2}[^*＊\n]{1,40}[*＊]{1,2}", "", t)   # *smiles* / **轻笑**
    t = re.sub(r"\n{2,}", "\n", t).strip()
    if len(t) >= 2 and t[0] in "「“\"" and t[-1] in "」”\"":
        t = t[1:-1].strip()
    return t


async def stream_llm(gen, total_timeout: float, first_timeout: float):
    """Enforce a first-token deadline and a whole-reply deadline on a stream."""
    start = time.monotonic()
    it = gen.__aiter__()
    first = True
    while True:
        remain = first_timeout if first else (total_timeout - (time.monotonic() - start))
        if remain <= 0:
            raise providers.LLMError(f"LLM 超时（{total_timeout:.0f}s）")
        try:
            piece = await asyncio.wait_for(it.__anext__(), remain)
        except asyncio.TimeoutError as exc:  # noqa: PERF203
            # wait_for 抛的是 asyncio.TimeoutError（内置 TimeoutError 的别名，str() 为空串），
            # 不转换的话它会以 `TimeoutError: ` 的裸异常冒出去，日志里看不出是哪一级超时。
            phase = "首字" if first else "整段"
            raise providers.LLMError(f"LLM {phase}超时（{remain:.0f}s 内没有新 token）") from exc
        except StopAsyncIteration:
            return
        first = False
        yield piece


def visible_reply(raw: str) -> str:
    """Legacy fallback: strip a trailing `###DIRECTIVE###` block from the text."""
    cut = len(raw)
    for size in range(2, len(persona.MARKER) + 1):
        idx = raw.find(persona.MARKER[:size])
        if idx != -1:
            cut = min(cut, idx)
    return raw[:cut].strip()


def header_probe(raw: str, at_end: bool = False) -> tuple[str, dict | None, int]:
    """首行协议探针：`{"emotion":...}\\n台词`。

    返回 (state, directive, header_end)：
    - `wait`  ：还判断不出（JSON 没闭合 / 还没等到换行），这段文本**一个字都不能送去 TTS**
    - `json`  ：首行是合法 JSON 指令，header_end 指向台词起点
    - `plain` ：模型没按协议输出，整段当台词（旧格式兜底），header_end 指向正文起点

    为什么要按括号配对扫描而不是 `raw.split("\\n")[0]`：流式到达时首行可能只到一半，
    旧写法会把 `{"emotion":"neutral",` 这种半截 JSON 当成台词送进 TTS——这是实际发生过的
    缺陷（音频里在念 JSON）。这里宁可等，也不能提前说话。
    """
    n = len(raw)
    i = 0
    while i < n and raw[i] in " \r\n\t":
        i += 1
    # 兼容模型把 JSON 包在 ``` 代码块里
    if raw.startswith("```", i):
        nl = raw.find("\n", i)
        if nl == -1:
            return ("wait", None, 0)
        i = nl + 1
        while i < n and raw[i] in " \r\n\t":
            i += 1
    if i >= n:
        return ("wait", None, 0)
    if raw[i] != "{":
        return ("plain", None, i)
    depth = 0
    in_str = False
    esc = False
    j = i
    while j < n:
        ch = raw[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    if j >= n:  # JSON 还没闭合
        return ("plain", None, i) if n - i > 400 else ("wait", None, 0)
    tail_nl = raw.find("\n", j + 1)
    if tail_nl == -1:
        if at_end:  # 流结束：整条就是一个 JSON，没有台词
            tail_nl = j
        elif n - i > 400:
            return ("plain", None, i)
        else:
            return ("wait", None, 0)
    try:
        obj = json.loads(raw[i:j + 1])
    except json.JSONDecodeError:
        return ("plain", None, i)
    if not isinstance(obj, dict):
        return ("plain", None, i)
    return ("json", obj, tail_nl + 1)


def parse_tail_directive(raw: str) -> dict | None:
    if persona.MARKER not in raw:
        return None
    body = raw.split(persona.MARKER, 1)[1].strip()
    body = body.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    lo, hi = body.find("{"), body.rfind("}")
    if lo < 0 or hi <= lo:
        return None
    try:
        return json.loads(body[lo:hi + 1])
    except Exception:  # noqa: BLE001
        return None


def guard_story(directive: dict, beat: int) -> list[str]:
    """节拍闸门：不允许模型把还没到的事件提前放出来（最早节拍来自卡包）。"""
    fixes: list[str] = []
    ev = directive["event"]
    min_beat = persona.EVENT_MIN_BEAT.get(ev)
    if min_beat is not None and beat < min_beat:
        directive["event"] = "none"
        fixes.append(f"{ev} 早于第 {min_beat + 1} 拍，已拦下")
        ev = "none"
    if ev == "photo_reveal" and directive["action"] != "show_photo":
        directive["action"] = "show_photo"
    if ev == "thunder" and directive["effect"] != "thunder":
        directive["effect"] = "thunder"
    return fixes


REVEAL_LABEL = {
    "arrival": "你们刚打过招呼，店里快打烊了、外面雨刚停",
    "probe": "对方问过你为什么这么晚还在外面",
    "admit": "你说过你在等一个「说好会来的人」，十点半那班",
    "photo": "你给他看过那张三年前的照片",
    "absence": "你说过他后来再没出现、照片一直在你口袋里",
    "choice": "你把「该不该把照片寄出去」交给了对方",
    "closing": "店里要关灯了，你们准备走",
}


def ledger_text(s: Session) -> str:
    known = [REVEAL_LABEL[b] for b in s.revealed if b in REVEAL_LABEL]
    if not known:
        known = ["你们刚开始说话，对方什么都不知道"]
    if s.user_facts:
        known.append("对方说过：" + "；".join(s.user_facts[-4:]))
    known.append(f"这是你们的第 {len(s.history) // 2 + 1} 轮对话")
    return "；".join(known)


def recent_text(s: Session) -> str:
    recent = [r for r in s.recent[-5:] if r]
    if not recent:
        return "（还没有说过话）"
    return " / ".join(f"「{r}」" for r in recent)


_NAME_STOP = set("什么 啥 谁 哪 这个 那个 他 她 我 你 这里 那里".split())


def _valid_name(name: str) -> bool:
    """过滤「我叫什么吗」这类问句被正则当成名字的误抽。

    真实事故：用户问「你还记得我叫什么吗」，正则抽到「什么吗」，
    记忆里于是多了一条「他叫什么吗」，下一轮提示词把它当已知事实喂给模型。
    """
    if not name or name in _NAME_STOP or len(name) > 4:
        return False
    return not any(ch in name for ch in "什么吗呢吧啊呀哦的了是")


_FACT_RULES = [
    (r"我叫([\u4e00-\u9fa5A-Za-z]{1,6})", "他叫{d}"),
    (r"我是([\u4e00-\u9fa5A-Za-z]{2,4})[，,。 ]", "他叫{d}"),
    # 一条规则一条事实：早先把「加班」和「末班车」合在一条里，角色就跟着说
    # 「你要等末班车吧」——其实是模板自己塞给她的，属于**我们编的**细节。
    (r"(加班|刚下班|上夜班|通宵)", "他今晚加班到很晚"),
    (r"(末班车|末班地铁|最后一班车|最后一班地铁)", "他在等末班车"),
    (r"(手机没电|没电了|快没电|要没电)", "他手机快没电了"),
    (r"(没带伞|淋湿|湿透|淋雨)", "他今晚被雨淋了"),
    (r"(摄影师|拍照片|拍照|相机)", "他对拍照这件事有兴趣"),
    (r"(失恋|分手|心情不好|难过)", "他今晚心情不太好"),
]


def extract_facts(text: str, store: list[str]) -> list[str]:
    """从观众这句话里抽一两条可复用的记忆。返回**本轮新增**的记忆（给导演层用）。

    不追求 NLU 准确率：目的是让角色真的能「记得你说过什么」——
    3–5 分钟的体验里，被记住一次比多一条华丽台词更有说服力。
    """
    added: list[str] = []
    for pat, tmpl in _FACT_RULES:
        m = re.search(pat, text or "")
        if not m:
            continue
        if "{d}" in tmpl and not _valid_name(m.group(1)):
            continue
        item = tmpl.format(d=m.group(1)) if "{d}" in tmpl else tmpl
        if item not in store:
            store.append(item)
            added.append(item)
    del store[:-4]
    return added


def keyword_event(text: str, beat: int, played: set[str]) -> str | None:
    """内容触发的兜底：小模型（4B）经常不敢在指令里给 event。

    题目要求「至少 1 个由当前对话内容触发的明显视觉事件，不能只按计时器播放」，
    所以事件权限仍然绑在台词内容上：用户这句话里出现了触发词、且剧情节拍允许、
    且这个事件还没放过，才补一个。人设/节拍闸门照旧生效。
    """
    for event, words in persona.EVENT_KEYWORDS.items():
        if event in played:
            continue
        if not any(w in (text or "") for w in words):
            continue
        if beat < persona.EVENT_MIN_BEAT.get(event, 0):
            continue
        return event
    return None


# --------------------------------------------------------------- 导演层接入
#
# 分工：导演层决定「这一轮说什么」（意图、必须出现的事实、禁用词），
# 模型只负责「把已经决定的内容说得好听」。这是本场景不需要大模型的根据——
# 需要推理/规划的部分交给了确定性代码，留给模型的只有它擅长的措辞。

PLAN_CONTENT = os.environ.get("MIRA_PLAN_CONTENT", "1") != "0"


def split_first_json(raw: str) -> tuple[dict | None, str]:
    """拆「首行 JSON + 台词」。解析不了就把整段当台词（check 里会看到）。"""
    text = (raw or "").strip()
    first, sep, rest = text.partition("\n")
    if sep:
        try:
            obj = json.loads(first.strip())
            if isinstance(obj, dict):
                return obj, rest.strip()
        except (json.JSONDecodeError, ValueError):
            pass
    return None, text


async def generate_blocking(llm, messages: list[dict], plan, recent: list[str] | None = None,
                            timeout: float = 25.0) -> tuple[str, list[str]]:
    """关键轮 / 首句闸门失败后的重写：整段生成 → 机检 → 不合格重写一次 → 再不合格用确定性兜底。

    为什么关键轮和「答非所问的首句」值得这么做：这两类错在体验上是不可逆的——
    被问「你是在等谁」答一句雨，或者「你还记得我叫什么吗」答错，用户当场就出戏；
    而普通轮次走流式，首字延迟最低。4B 单轮生成实测 0.4–0.6s，这点代价换一个
    可校验的结果是划算的（首句闸门只在**校验失败**时才走到这里）。
    """
    def violations_of(text: str) -> list[str]:
        _, reply = split_first_json(text)
        # gate_check 里已经含「和最近说过的话几乎一样」的复读检测、旁白/客服腔、
        # 话题锚点（有没有答在被问的那件事上）与硬禁词（阿岸）。
        return plan.gate_check(reply, recent=recent)

    raw = ""
    async for piece in stream_llm(llm.stream(messages), total_timeout=timeout, first_timeout=12.0):
        raw += piece
    viol = violations_of(raw)
    if not viol:
        return raw, []

    retry_messages = list(messages) + [
        {"role": "assistant", "content": raw},
        {"role": "user",
         "content": "（重来。刚才那句不合格：" + "；".join(viol) +
                    "。第一句就必须点在被问的那件事上，只按【本轮怎么做】重写一条，"
                    "还是首行 JSON + 换行 + 台词，别的都不要。）"},
    ]
    raw2 = ""
    async for piece in stream_llm(llm.stream(retry_messages), total_timeout=timeout, first_timeout=12.0):
        raw2 += piece
    viol2 = violations_of(raw2)
    if not viol2:
        return raw2, [f"重写一次后通过（上一次：{'；'.join(viol)}）"]
    if plan.fallback:
        directive = dict(persona.DEFAULT_DIRECTIVE)
        return (json.dumps(directive, ensure_ascii=False) + "\n" + plan.fallback,
                [f"两次都没过（{'；'.join(viol2)}），改用确定性兜底台词"])
    return raw2, [f"两次都没过（{'；'.join(viol2)}）"]


def _overlap(a: str, b: str) -> float:
    """两个字一组算重叠率：判断「是不是把上一轮的话又说了一遍」。"""
    clean = lambda s: re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s or "")  # noqa: E731
    x, y = clean(a), clean(b)
    if len(x) < 4 or len(y) < 4:
        return 0.0
    bx = {x[i:i + 2] for i in range(len(x) - 1)}
    by = {y[i:i + 2] for i in range(len(y) - 1)}
    return len(bx & by) / max(1, len(bx))


async def run_stranger(s: Session, seq: int, epoch: int, emit, text: str,
                       decision) -> None:
    """陌生人状态：决策层两道门都没过时，她不接这句话。

    为什么单独一条路径，而不是「让模型回一句敷衍的话」：
    - 敷衍话会作为 assistant 轮写进 history，下一轮模型会以为自己确实说了那句话，
      越聊越脏；而这一轮的事实是**她根本没开口**。
    - 题面 §14 要求录屏里必须出现「至少一种失败或降级状态」。这句话如果也有音频，
      就和正常轮没有可视差异，讲不出「这是降级」；**没有音频**才是可演示的证据。
    - 不推进节拍：她没有回应，剧情就不该往前走（推进条件是「话题命中/视觉事件/轮数」，
      这一轮三者都不成立）。

    刻意不做的事：不换表情、不换场景。她只是不接话，切镜头会让人以为她"有反应"。
    """
    t0 = time.time()
    narration = persona.stranger_narration(seed=seq)
    directive = dict(persona.DEFAULT_DIRECTIVE)
    await emit({"t": "directive", "directive": directive,
                "fixes": [f"决策层未命中（{decision.reason}）：进入陌生人状态，不说话"],
                "parsed": True})
    s.last_directive = directive
    # 字幕有、音频没有：客户端按 silent 字段跳过 TTS 与口型。
    await emit({"t": "subtitle", "narration": narration, "silent": True,
                "reason": decision.reason, "gate": round(decision.gate, 2),
                "slot": decision.slot, "backend": decision.backend})
    await emit({"t": "state", "state": "idle"})
    await emit({"t": "suggest", "items": persona.suggestions(s.beat, ()), "beat": s.beat})
    await emit({"t": "done", "audio": 0, "audio_errors": 0, "first_audio_ms": None,
                "ms": int((time.time() - t0) * 1000), "beat": s.beat, "advanced": False,
                "silent": True, "synth_ms": [], "first_text_ms": 0,
                "gate": {"on": False, "done": False, "rewrites": 0, "checks": [],
                         "ms": 0, "wait_ms": 0},
                "plan": {"intent": "stranger", "violations": [], "topic": "",
                         "anchors": [], "gate": {"on": False, "rewrites": 0},
                         "must": [], "avoid": []},
                "selector": decision.as_dict()})
    s.log("turn_stranger", seq=seq, reason=decision.reason, gate=round(decision.gate, 2),
          slot=decision.slot, backend=decision.backend,
          ms=int((time.time() - t0) * 1000), text=text[:40])


async def run_opening(s: Session, seq: int, epoch: int, emit, queue: asyncio.Queue) -> None:
    """开场：她先注意到你，但**不主动搭话**（用户反馈「开局莫名其妙」）。

    为什么不是一句台词：陌生人推门进来，她先开口并送出信息，在一场「她本来不想
    理人」的戏里是错的；而且开场台词是卡包写死的，和后面模型生成的口吻接不上。
    现在开场只做三件事：抬头看你一眼（动作）+ 一行旁白 + 三个可推进的选项。
    用户不开口，戏就不开始——这符合她「等的是别人」的状态。
    """
    t0 = time.time()
    directive = dict(persona.BEATS[0]["directive"])
    directive["emotion"] = "neutral"
    directive["action"] = "look_door"
    try:
        await emit({"t": "state", "state": "thinking"})
        await emit({"t": "turn_start", "provider": "card", "kind": "open", "beat": 0})
        await emit({"t": "directive", "directive": directive,
                    "fixes": ["开场不主动搭话：只做动作，不说话"], "parsed": True})
        s.last_directive = directive
        await emit({"t": "subtitle", "silent": True,
                    "narration": "（她抬头看了你一眼，把桌上的相机往旁边推了推，没开口。）"})
        await emit({"t": "state", "state": "idle"})
        await emit({"t": "suggest", "items": persona.suggestions(0, ()), "beat": 0})
        await emit({"t": "done", "audio": 0, "audio_errors": 0, "first_audio_ms": None,
                    "ms": int((time.time() - t0) * 1000), "beat": 0, "advanced": False,
                    "silent": True, "synth_ms": [], "first_text_ms": 0, "provider": "card",
                    "gate": {"on": False, "done": False, "rewrites": 0, "checks": [],
                             "ms": 0, "wait_ms": 0},
                    "plan": {"intent": "open", "violations": [], "topic": "", "anchors": [],
                             "gate": {"on": False, "rewrites": 0}, "must": [], "avoid": []}})
        s.log("turn_open", seq=seq, ms=int((time.time() - t0) * 1000))
    finally:
        try:
            await queue.put(None)
        except Exception:  # noqa: BLE001
            pass


async def run_turn(s: Session, seq: int, epoch: int, text: str, kind: str, queue: asyncio.Queue) -> None:
    t0 = time.time()
    emit = make_emitter(s, seq, epoch, queue)

    if kind == "open":
        await run_opening(s, seq, epoch, emit, queue)
        return

    # ---- 决策层：这句话还在这场戏里吗？（app/selector.py，见 docs/PLAN_v4.md）
    # 只拦真实用户输入；系统注入/开场白不经过这道门。
    # 决策层是可选依赖（缺 torch 时降级为关键词门），但**不允许**它把整轮搞挂：
    # 出异常就回落到原来的链路，并留一条日志——降级要看得见，不能静默。
    if kind == "user":
        try:
            decision = selector.decide(text)
        except Exception as exc:  # noqa: BLE001
            decision = None
            print(f"[selector] 决策层异常，回落旧链路：{type(exc).__name__}: {exc}",
                  flush=True)
        if decision is not None and (not decision.accepted or decision.slot == "off_topic"):
            # P0-2 永不沉默：决策层已经不再把「门外」判成拒绝；这里只剩硬跑题
            # （写代码/翻译/算题）——它也不沉默，走一句最短的挡回，由导演层给指令。
            decision.slot = "off_topic"
    else:
        decision = None

    tts_queue: asyncio.Queue = asyncio.Queue()
    stats = {"audio": 0, "errors": 0, "first_audio_ms": None, "speaking": False,
             "synth_ms": []}
    ctx = {"emotion": "neutral"}
    # 表演层状态（见 app/perf.py 与 docs/PERF_CONTRACT.md）：
    # 反应时间/语气词在拿到 directive 时就定下来，先发给客户端；手势落点要等
    # 句子切好（sent_list）才能给，所以分 rev1 / rev2 两帧下发。
    rnd = perf.new_rnd(seq)
    faces: dict[str, Any] = {"hold_ms": 250, "filler": None, "early_sent": False}
    sent_list: list[str] = []
    stream_mode = stream_engine()
    live_mode = live_tts_mode()

    async def tts_worker() -> None:
        """并发合成 + 按序下发。

        之前是「一句一句串行 synth → emit」：edge-tts 是外网服务，偶尔一次要 8–12s，
        这时后面的句子全部排队，还排在播放的音频后面干等，听起来就断断续续。
        现在最多 2 路并发合成，但**仍然严格按句子顺序下发**，保证字幕/音频/口型对得上。
        """
        # 并发度：edge 管线单句 ≈1.5s（网络 + 解码 + 重采样 + 混响），一句一句串行时
        # 句间必然出现 1.5s 空白——听感就是"说一句卡一下"。这里放到 4 路并发，
        # 让第 2、3 句在**第 1 句播放期间**就合成好（下发仍严格按句子顺序）。
        sem = asyncio.Semaphore(max(1, int(os.environ.get("MIRA_TTS_CONC", "4"))))
        running: dict[int, asyncio.Task] = {}
        emitted = 0

        async def synth_one(sentence: str, index: int):
            # 流式档：服务端不合成，只把「去哪拿这段 PCM」告诉客户端。
            # 为什么让浏览器直接拉：音源是逐块吐 PCM 的，服务端再中转一遍等于多一跳；
            # 而客户端拿到第一块就能开播（首包 200–400ms），这是唯一能压到真人反应速度的做法。
            # 例外：预设命中时**走文件**更好——那条音频启动时已经预合成进 media/tts 缓存，
            # 直接给一个静态 mp3 是 0 合成；走流式反而要现算一遍，把省下的时间又还回去。
            # live 档的 first 模式只让第一句走流式：首句之外走整句管线，保证句间用
            # 预取好的 mp3（4 路并发合成）衔接，不依赖流式串行。
            want_stream = bool(stream_mode) and hit is None and (
                stream_mode == "neural" or live_mode == "all" or index == 0)
            if want_stream:
                return {"stream": True, "est_ms": est_speech_ms(sentence),
                        "url": stream_url(sentence, ctx["emotion"], index)}
            async with sem:
                if s.inject["tts_fail"]:
                    raise RuntimeError("注入故障：TTS 合成失败")
                if s.inject["slow_tts"]:
                    await asyncio.sleep(3.0)
                t0 = time.time()
                path, cached = await tts.synth(sentence, emotion=ctx["emotion"],
                                        position="first" if index == 0 else "follow",
                                        timeout=float(os.environ.get("MIRA_TTS_TIMEOUT", "6")))
                return path, cached, int((time.time() - t0) * 1000)

        async def flush_ready(final: bool = False) -> bool:
            nonlocal emitted
            while emitted in running:
                task = running.pop(emitted)
                index = emitted
                try:
                    res = await task
                except TurnCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    stats["errors"] += 1
                    await emit({"t": "audio_error", "text": "",
                                "index": index,
                                "message": f"TTS 不可用：{type(exc).__name__}，降级为字幕"})
                else:
                    if not stats["speaking"]:
                        stats["speaking"] = True
                        # first_audio_ms = 观众听到的**第一声**（可能是语气词，见 emit_directive），
                        # 这里只补「第一句台词」的时间点，不覆盖语气词已经记下的那一声。
                        if stats["first_audio_ms"] is None:
                            stats["first_audio_ms"] = int((time.time() - t0) * 1000)
                        stats.setdefault("first_line_audio_ms", int((time.time() - t0) * 1000))
                        await emit({"t": "state", "state": "speaking"})
                    stats["audio"] += 1
                    if isinstance(res, dict):
                        stats["synth_ms"].append(0)
                        await emit({"t": "audio_stream", "url": res["url"],
                                    "text": task_text[index], "index": index,
                                    "emotion": ctx["emotion"], "est_ms": res["est_ms"],
                                    "synth_ms": stats["first_audio_ms"]})
                    else:
                        path, cached, ms = res
                        stats["synth_ms"].append(ms)
                        await emit({"t": "audio", "url": f"/media/tts/{path.name}",
                                    "text": task_text[index], "index": index,
                                    "emotion": ctx["emotion"], "cached": cached, "synth_ms": ms})
                emitted += 1
            return emitted

        task_text: dict[int, str] = {}
        while True:
            item = await tts_queue.get()
            if item is None:
                break
            sentence, index = item
            task_text[index] = sentence
            running[index] = asyncio.create_task(synth_one(sentence, index))
            await flush_ready()
        # 队列关闭后把剩下的按序发完
        while emitted < len(task_text):
            before = emitted
            await flush_ready(final=True)
            if emitted == before:  # 还差的那一句任务还没注册（不应发生），跳过去避免死等
                emitted += 1

    async def emit_directive(directive: dict, fixes: list[str], parsed: bool) -> None:
        """发 directive，并定下本轮的「反应时间 + 起头语气词 + 开口前的表演」。

        为什么必须在这里定：客户端的表情/动作以前是收到 directive 就立刻执行，
        于是她在开口前就把脸换完了、动作也演完了。表演层要求
        「先顿一下 → 视线移开 → 换情绪 → 开口」，这几件事必须在同一根时间轴上，
        由这里统一定，客户端只按 beat 执行。格式见 docs/PERF_CONTRACT.md。
        """
        await emit({"t": "directive", "directive": directive, "fixes": fixes, "parsed": parsed})
        if faces["early_sent"]:
            return
        faces["early_sent"] = True
        hold = perf.reaction_ms(kind=kind, text=text, plan=plan, directive=directive, rnd=rnd)
        faces["hold_ms"] = hold
        faces["filler"] = perf.filler_for(kind=kind, plan=plan, rnd=rnd)
        beats = perf.early_beats(kind=kind, directive=directive, hold_ms=hold)
        await emit({"t": "pacing", "reaction_ms": hold, "filler": faces["filler"] or ""})
        await emit({"t": "perf", "rev": 1, "start_hold_ms": hold, "beats": beats})
        s.log("perf", rev=1, hold_ms=hold, filler=faces["filler"], beats=perf.describe(beats))
        # ---- 语气词先出声（问题 4：延迟） --------------------------------------
        # 走生成路径才有语气词（预设轮/开场白本身已经很快）。它是本地缓存文件，
        # 服务端只发一个 URL —— 观众的"第一声"≈反应时间，不再等云端合成。
        # 2026-10-21 01:0x 实跑：真实台词首音频 2.2–3.2s，语气词 0.3s（=反应时间）。
        # 语气词以前是概率触发（perf.filler_for 0.42/0.16），做延迟遮挡不够稳；
        # 现在生成轮**必定**给一个（轮换，不连着重复），音频不过来时它就是那一声"嗯"。
        # 如果听起来像两个"嗯"或太刻意：MIRA_FILLER_AUDIO=0。
        filler = faces["filler"]
        if FILLER_AUDIO and not filler and hit is None and kind != "open":
            filler = FILLER_CYCLE[s.seq % len(FILLER_CYCLE)]
        if FILLER_AUDIO and filler and hit is None and kind != "open":
            fp = FILLER_PATHS.get(filler)
            rel = ""
            if fp is not None and fp.exists():
                try:
                    rel = fp.resolve().relative_to(MEDIA.resolve()).as_posix()
                except ValueError:
                    rel = ""
            if rel:
                if stats["first_audio_ms"] is None:
                    stats["first_audio_ms"] = int((time.time() - t0) * 1000)
                    stats["filler_ms"] = stats["first_audio_ms"]
                await emit({"t": "audio", "url": f"/media/{rel}",
                            "text": filler, "index": -1,
                            "emotion": ctx["emotion"], "filler": True,
                            "cached": True, "synth_ms": 0})

    async def push_sentence(sent: str) -> None:
        """入 TTS 队列，同时记住它在 audio.index 上的顺序（表演层的锚点靠它）。

        第一句前面会加一个语气词（「嗯……」）：真人接话常常先出声再成句，
        语气词由 TTS 合成在同一句里，字幕不重复显示——听感上就是"她先应了一声"。
        """
        nonlocal sent_index
        if not (sent or "").strip():
            return
        if sent_index == 0 and faces["filler"] and hit is None:
            sent = faces["filler"] + sent
        # 英文词（4B 实测会把 tonight 混进中文台词）在送 TTS 前剥掉：
        # 中文音色念英文极出戏；这里只删词，不整段重写（第二句以后重写会把
        # 已经说出口的第一句对不上）。首句里的英文由闸门 gate_check 直接判重写。
        sent, stray = director.strip_latin(sent)
        if stray:
            plan.notes.append(f"台词夹杂英文词「{'、'.join(stray)}」，已剥掉（不进 TTS）")
        if not (sent or "").strip():
            return
        sent_list.append(sent.strip())
        await tts_queue.put((sent.strip(), sent_index))
        sent_index += 1

    worker = asyncio.create_task(tts_worker())

    try:
        await emit({"t": "state", "state": "thinking"})
        await emit({"t": "turn_start", "provider": s.mode, "kind": kind, "beat": s.beat})

        new_facts: list[str] = []
        if kind != "open":
            # 先把「对方这句话里值得记住的东西」收进记忆，本轮提示词就能用上
            new_facts = extract_facts(text, s.user_facts)
            # 内容触发剧情：用户问到后面的剧情点，就把节拍跳过去（而不是让角色硬挡）
            jump = persona.beat_jump(s.beat, text)
            if jump != s.beat:
                s.revealed.extend(persona.BEATS[i]["id"] for i in range(s.beat, jump))
                s.log("beat_jump", frm=s.beat, to=jump, because=text[:40])
                s.beat = jump
                s.turns_in_beat = 0
                s.beat_hits = 0

        llm = llm_for(s.mode)
        # 导演层：先判「对方问的是哪件事」→ 本轮该传达的内容 + 可机检的答案标准。
        # beat（跳拍之后）决定这一拍能松口多少；turn_no 决定这轮要不要把话头交回去；
        # prev_contents/prev_topics 用来识别「连着问同一件事」的追问（否则 4B 会复读）。
        turn_no = len([m for m in s.history if m.get("role") == "user"]) + 1
        if decision is not None and decision.slot == "off_topic":
            plan = director.off_topic_plan()
        else:
            plan = director.plan_turn(text, facts=s.user_facts, new_facts=new_facts,
                                      recent=s.recent, is_open=(kind == "open"),
                                      beat=s.beat, turn_no=turn_no,
                                      prev_contents=s.last_contents, prev_topics=s.last_topics)

        # ---- 预设库（barks）第一道门：这句话能不能直接查表回答？
        # 为什么放在最前面（在构造提示词、调模型之前）：命中路径要「说完就答」，
        # 提示词拼装和 4B 首字（p50 ≈ 570ms）都不能进入这条路径。
        # 不拦的情况：系统注入/开场白（kind != user）、被判成硬跑题（off_topic 有自己的挡回）。
        act_hit = acts.classify(text) if kind == "user" else None
        hit = None
        # 对方刚说了关于他自己的事（自我介绍 / 名字 / 今天干了什么）时不许查表：
        # 这类轮次必须**接住他说的那件事**，而预设是按「言语行为」查的，看不见内容。
        # 实测反例：用户说「我叫张伟，今天加班到现在」，act=smalltalk 命中了天气预设，
        # 回了一句「这几天雨都很大。」——名字和加班全被无视。
        # 有 new_facts 就说明这句话里有值得记住的内容，交给生成（volunteer 槽位）。
        if act_hit is not None and not new_facts \
                and act_hit.act in PRESET_ACTS \
                and not (decision is not None and decision.slot == "off_topic"):
            try:
                hit = PRESETS.match(
                    text=text, act=act_hit,
                    rel=persona.BEATS[s.beat].get("rel", ""),
                    beat_id=persona.BEATS[s.beat]["id"], now=s.seq,
                    recent=s.recent, used=s.preset_used, cursor=s.preset_cursor,
                    once_used=s.preset_once, consec=s.preset_run)
            except Exception as exc:  # noqa: BLE001
                # 预设是加速层，不是正确性依赖：查表炸了就回落生成，但降级要看得见。
                print(f"[preset] 查询异常，回落生成：{type(exc).__name__}: {exc}", flush=True)
                hit = None

        # ---- P0-1 决策权归位：表现（情绪/动作/事件）由决策层算，4B 只负责措辞。
        # 为什么必须提前到这里：以前要等模型吐出首行 JSON 才敢发 directive，
        # 首包延迟被绑在模型上；现在它在 L0 就有答案（0ms），而且不再让 4B
        # 用最保守的 neutral 把表情锁死。
        ev = keyword_event(text, s.beat, s.played) if kind != "open" else "none"
        over, ofix = director.reaction_for(plan)
        merged = dict(persona.BEATS[s.beat]["directive"])
        merged.update(over)
        # 命中预设时，她的表现由台词自己的 perf 决定（写死在这句台词旁边，
        # 比让模型挑动作更准）；没命中才用决策层算出来的反应。
        if hit is not None:
            merged.update(hit.perf)
        if ev != "none":
            merged["event"] = ev
            if ev == "photo_reveal":
                merged["action"] = "show_photo"
            if ev == "thunder":
                merged["effect"] = "thunder"
        for _k, _v in (persona.BEATS[s.beat].get("pin") or {}).items():
            merged[_k] = _v
        plan_directive, dfix = persona.sanitize_directive(merged)
        if ev != "none":
            s.played.add(ev)
        ctx["emotion"] = plan_directive["emotion"]
        direct_emitted = False
        messages: list[dict] = [
            {"role": "system",
             "content": persona.render_system_prompt(
                 s.beat, ledger=ledger_text(s), recent=recent_text(s),
                 plan_block=plan.prompt_block(use_content=PLAN_CONTENT))}
        ]
        for h in s.history[-12:]:
            messages.append(h)
        if kind == "open":
            messages.append({"role": "user",
                             "content": "（一位躲雨的客人刚推门走进来，在吧台前坐下。你抬头看他一眼，先说一句。）"})
        else:
            messages.append({"role": "user", "content": text})
        messages[-1]["beat"] = s.beat  # mock provider uses this to pick the script
        messages[-1]["kind"] = kind

        if os.environ.get("MIRA_DUMP_PROMPT"):
            try:
                (ROOT / "logs").mkdir(exist_ok=True)
                (ROOT / "logs" / "prompt_last.json").write_text(
                    json.dumps(messages, ensure_ascii=False, indent=1), encoding="utf-8")
            except Exception:  # noqa: BLE001
                pass

        if s.inject["llm_timeout"]:
            await asyncio.sleep(3.0)
            raise providers.LLMError("LLM 超时（注入故障：3s 内没有返回任何 token）")

        # 表现指令 0ms 先发：客户端不必等模型首字就能换表情/演动作。
        # 从这一刻起 dir_emitted=True，后面所有「解析到首行 JSON 再发 directive」的
        # 旧路径都只留痕、不再重复发（L0 已经定了，模型没有选择权）。
        if not direct_emitted:
            await emit_directive(plan_directive,
                                 list(ofix) + list(dfix) + ["表现由决策层决定（4B 只出词）"],
                                 True)
            s.last_directive = plan_directive
            direct_emitted = True

        raw = ""
        header_end = 0
        sfilter = StageDirectionFilter()
        raw_fed = 0          # 已喂给过滤器的原始字数（filter 只吃增量）
        legacy = False
        directive: dict | None = None
        fixes: list[str] = []
        reply = ""
        consumed = 0
        sent_index = 0
        last_partial = 0.0
        last_shown = ""

        # ---- 首句闸门 -------------------------------------------------------
        # 为什么要有：流式生成没法「事后校验」——第一句一落地就进 TTS 了，等整段
        # 生成完再发现答非所问，声音已经出去了。但 take_sentences 本来就要等到句号
        # 才切句送 TTS，所以「扣住第一句先校验」的额外延迟 ≈ 0（只多几微秒正则）；
        # 真正花时间的只有校验失败后的整段重写（走 generate_blocking，有确定性兜底）。
        # 覆盖范围：所有提问轮（plan.gate），不再只有 recall/self 两个 critical 轮。
        # 预设轮不设闸门：那几句是写死的短句，点题与禁词在 presets.yaml 校验和去重时就守住了；
        # 而闸门失败会触发整段重写（一次模型调用），等于把预设省下的时间又还回去。
        gate_on = (kind != "open") and bool(plan.gate) and not plan.critical and hit is None
        gate_done = not gate_on
        gate_pending: list[str] = []
        gate_viol: list[str] = []
        gate_failed = False
        gate_checks = 0
        gate_ms = 0.0
        gate_wait_ms: int | None = None
        first_text_ms: int | None = None

        # ---- 台词二次校正（问题 6 · 方案乙，2026-10-21 用户拍板） ----------------
        # 表现指令在 L0 就由 intent 查表算好并发给客户端了（0ms），但那一刻我们还不知道
        # 她这一轮会说什么。等第一句台词落地（~1s）再读一遍台词，把和台词对不上的
        # emotion/action 校正掉：
        #   · 表里要掏照片、台词里却没有「照片」→ 降级（不许无缘无故掏照片）
        #   · 台词提到雨/窗外/门口/咖啡/手机 → 往那个方向看、喝一口
        #   · 她自己说话时不允许 listen 表情（上一轮反馈的遗留 bug）
        # 为什么不等整段生成完：动作在首包就排给了客户端，等 2s 再改，观众已经看见错了。
        # 规则在 cards/mira_cafe/card.yaml 的 dialog_fix 段，逻辑在 director.dialog_fix（纯函数）。
        spoken: list[str] = []
        patch_n = 0            # 本轮校正次数（最多 2：首句一次、整段一次），写进 trace
        patch_log: list[str] = []

        async def dialog_fix_patch(text_so_far: str) -> None:
            nonlocal patch_n
            if not text_so_far or patch_n >= 2:
                return
            base = s.last_directive or plan_directive
            fixed, pfix = director.dialog_fix(
                text_so_far, base, rules=persona.DIALOG_FIX_RULES,
                intent=plan.intent, speaking=True)
            if not pfix:
                return
            patch_n += 1
            payload = {k: fixed[k] for k in ("emotion", "action") if k in fixed}
            await emit({"t": "directive_patch", "directive": payload,
                        "src": "dialog_fix", "fixes": pfix})
            s.last_directive = fixed
            if directive is not None:
                directive.update(payload)
            plan.notes.extend(f"台词校正：{v}" for v in pfix)
            patch_log.extend(pfix)
            try:
                s.log("dialog_fix", intent=plan.intent, said=text_so_far[:40], fixes=pfix)
            except Exception:  # noqa: BLE001
                pass

        def finalize(raw_directive: dict | None, extra: list[str]) -> tuple[dict, list[str]]:
            """表现指令已经在 L0 由决策层算好；模型给的首行 JSON 一律忽略。

            为什么保留这个函数：首行解析 / 重写 / 收尾三处旧调用点还在按它的签名走。
            为什么不删干净：万一模型仍按旧协议输出，也得能安静地剥掉，而不是报错。
            """
            f = list(extra) + list(ofix) + list(dfix) + guard_story(plan_directive, s.beat)
            if isinstance(raw_directive, dict) and raw_directive:
                diff = [k for k in plan_directive
                        if k in raw_directive and raw_directive.get(k) != plan_directive.get(k)]
                if diff:
                    f.append("模型输出的表现指令已忽略：" + "、".join(diff))
            return dict(plan_directive), f

        async def turn_pieces():
            """关键轮先整段生成并校验，普通轮直接流式（保持最低首字延迟）。"""
            if hit is not None:
                # 命中预设：不调模型，直接把「首行指令 + 台词」当成一次生成结果吐出去。
                # 复用后面整条流水线（分句 → 闸门 → TTS → 表演 → 落库），
                # 保证预设轮和生成轮在客户端看起来完全一样，只是快很多。
                yield json.dumps(plan_directive, ensure_ascii=False) + "\n" + hit.text
                return
            if kind == "open":
                # 开场白用卡包里的定稿台词（不是模型写的）：一是首包能压到 300ms 内
                # （start.ps1 启动时已经把卡包里的台词预热进 TTS 缓存），二是开场这
                # 一句直接决定第一印象，不值得交给 4B 的概率。
                yield json.dumps(persona.BEATS[0]["directive"], ensure_ascii=False) \
                    + "\n" + persona.BEATS[0]["mock"]
                return
            if plan.critical:
                raw_full, notes = await generate_blocking(llm, messages, plan, recent=s.recent)
                plan.notes = notes
                yield raw_full
                return
            async for piece in llm.stream(messages):
                yield piece

        async for piece in stream_llm(turn_pieces(), total_timeout=35.0, first_timeout=14.0):
            raw += piece
            if directive is None:
                state, obj, hdr = header_probe(raw)
                if state == "json":
                    directive, fixes = finalize(obj, [])
                    ctx["emotion"] = directive["emotion"]
                    header_end = hdr
                    if not direct_emitted:
                        await emit_directive(directive, fixes, True)
                    s.last_directive = directive
                elif state == "plain":
                    # 模型没按首行协议：整段当台词，指令走默认值（fixes 里留痕，可观测）
                    legacy = True
                    header_end = hdr
                    directive, fixes = finalize(None, ["模型没有按首行协议输出指令，使用默认表现"])
                    ctx["emotion"] = directive["emotion"]
                    if not direct_emitted:
                        await emit_directive(directive, fixes, False)
                    s.last_directive = directive

            if directive is None:
                speech_raw = ""  # 首行 JSON 还没闭合：半截 JSON 绝不能送 TTS
            elif legacy:
                speech_raw = visible_reply(raw)
            else:
                speech_raw = raw[header_end:]
            # 旁白过滤器在流式阶段就生效，否则括号里的动作会被念出来。
            # 注意只喂**增量**：speech_raw 是累积文本，整段重喂会得到
            # "MMiraMira。Mira。……" 这种复读（实测会把一句话合成成 12 段音频）。
            speech = sfilter.feed(speech_raw[raw_fed:])
            raw_fed = len(speech_raw)
            chunk = speech[consumed:] if consumed <= len(speech) else ""
            if hit is not None:
                # 预设是一句短台词（≤40 字），整句送 TTS 而不是切成两句：
                # 侧车是串行合成的，切两半 = 两次排队，实测把「命中即秒回」拆成了两个 7s。
                sentences, used = ([chunk.strip()] if chunk.strip() else []), len(chunk)
            else:
                sentences, used = take_sentences(chunk)
            for sent in sentences:
                if first_text_ms is None:
                    first_text_ms = int((time.time() - t0) * 1000)
                if gate_done:
                    await push_sentence(sent)
                    spoken.append(sent)
                    await dialog_fix_patch("".join(spoken))
                    continue
                # 每来一句就判一次：已经答在题上就立刻放行（成功路径不加延迟）；
                # 没答上先别急着重写——「不是。」这种句子要等它后面那半句，
                # 攒到第二句还不对才判失败。
                gate_pending.append(sent)
                joined = "".join(gate_pending)
                gate_checks += 1
                if s.inject.get("gate_fail"):
                    # 注入一次「首句答非所问」：用来端到端验证重写/兜底路径
                    # （正常模型答对时这条路径永远不会被走到，而没测过的兜底等于没有）
                    s.inject["gate_fail"] = False
                    gate_viol = ["注入故障：模拟首句答非所问"]
                    gate_failed = True
                    break
                if s.inject.get("time_fail"):
                    # 注入一次「自创时间」：把一句**已知编造**的台词交给**真实的** plan.gate_check，
                    # 验证时间白名单确实接在首句闸门上、且能走到重写/兜底。
                    # 为什么非注入不可：真模型很少编时间（183 条历史真台词里只出现 2 次，
                    # 见 docs/TIMECHECK_2026-10-21.md），不注入就永远测不到这条路径。
                    # 如果白名单失效，下面的替代违规文本会出现在日志里（可区分"没测到"与"通过"）。
                    s.inject["time_fail"] = False
                    probe = "从早上七点开始，我在这里坐了四个半小时。"
                    gate_viol = plan.gate_check(probe, recent=s.recent, first=True)
                    if not gate_viol:
                        gate_viol = ["注入故障：时间白名单没有生效（这是失败，不是通过）"]
                    gate_failed = True
                    break
                gt = time.monotonic()
                gate_viol = plan.gate_check(joined, recent=s.recent, first=True)
                gate_ms += (time.monotonic() - gt) * 1000
                if not gate_viol:
                    gate_done = True
                    # 闸门对首句延迟的实际代价：第一句已完整 → 放行 的毫秒差
                    gate_wait_ms = int((time.time() - t0) * 1000) - (first_text_ms or 0)
                    for ready in gate_pending:
                        await push_sentence(ready)
                        spoken.append(ready)
                    gate_pending.clear()
                    await dialog_fix_patch("".join(spoken))
                elif len(gate_pending) >= 2:
                    gate_failed = True
                    break
            if gate_failed:
                break
            consumed = min(consumed + used, len(speech))
            now = time.monotonic()
            # 闸门没放行之前不刷字幕：宁可晚 100ms 出字，也不要把一句马上要被
            # 重写掉的错话先糊到屏幕上。
            if gate_done and speech and speech != last_shown and now - last_partial > 0.08:
                last_partial = now
                last_shown = speech
                await emit({"t": "subtitle_partial", "text": speech})

        # 流结束时闸门还没放行（回复太短，只吐了一句就停）：用整段来判。
        if gate_on and not gate_done and not gate_failed and gate_pending:
            joined = "".join(gate_pending)
            gate_checks += 1
            gt = time.monotonic()
            gate_viol = plan.gate_check(joined, recent=s.recent, first=True)
            gate_ms += (time.monotonic() - gt) * 1000
            if gate_viol:
                gate_failed = True
            else:
                gate_done = True
                gate_wait_ms = int((time.time() - t0) * 1000) - (first_text_ms or 0)
                for ready in gate_pending:
                    await push_sentence(ready)
                    spoken.append(ready)
                gate_pending.clear()
                await dialog_fix_patch("".join(spoken))

        if gate_failed:
            # 首句答非所问 → 丢掉这轮已流式生成的内容，整段重写一次（失败才付延迟）。
            # 重写仍不过时用 plan.fallback：那是这一轮**本来就该说的事实**，
            # 不是「随便糊一句」——所以兜底也是一句对题的台词。
            if directive is None:
                directive, fixes = finalize(None, ["首句闸门重写时首行协议缺失，使用默认表现"])
                ctx["emotion"] = directive["emotion"]
                if not direct_emitted:
                    await emit_directive(directive, fixes, False)
                s.last_directive = directive
            plan.notes = list(plan.notes) + [
                f"首句闸门拦下（{'；'.join(gate_viol)}），整段重写"]
            raw_full, notes = await generate_blocking(llm, messages, plan, recent=s.recent)
            plan.notes.extend(notes)
            _, g_body = split_first_json(raw_full)
            reply = tidy_reply(g_body)
            if not reply:
                reply = plan.fallback or persona.BEATS[s.beat]["mock"]
                plan.notes.append("重写后仍没有台词，改用兜底台词")
            for sent in split_all_sentences(reply):
                await push_sentence(sent)
            # 这一段已经整段送过 TTS：把 raw/consumed 摆成「已消费」，让下面的
            # 收尾逻辑不要再送一遍，也不重新解析指令（表现指令沿用第一条）。
            raw = json.dumps(directive, ensure_ascii=False) + "\n" + reply
            header_end = len(raw) - len(reply)
            legacy = False
            consumed = len(reply)

        # ---- 收尾：首行 JSON 后没有换行（整条就是指令），或模型用了旧格式（指令在末尾）
        if directive is None:
            state, obj, hdr = header_probe(raw, at_end=True)
            if state == "json":
                directive, fixes = finalize(obj, [])
                header_end = hdr
                parsed = True
            else:
                legacy = True
                tail = parse_tail_directive(raw)
                parsed = tail is not None
                directive, fixes = finalize(
                    tail, [] if parsed else ["模型没有按首行协议输出指令，使用默认表现"])
            ctx["emotion"] = directive["emotion"]
            if not direct_emitted:
                await emit_directive(directive, fixes, parsed)
            s.last_directive = directive

        reply = visible_reply(raw) if legacy else raw[header_end:]
        if persona.MARKER in reply:
            reply = visible_reply(reply)
        # 无状态再滤一次：流式阶段 sfilter 已滤过绝大部分，这里保证最终文本干净
        reply = tidy_reply(reply)
        if not reply:
            # 4B 模型有时只输出首行 JSON 就停（本轮实测真的会），画面里没有台词会像卡住。
            # 兜底用本拍的既定台词，并在观测面板里留痕，不假装是模型说的。
            reply = persona.BEATS[s.beat]["mock"]
            fixes = list(fixes) + ["模型只输出指令、没有台词，已用本拍兜底台词"]
            await emit_directive(directive, fixes, True)
        # 最终文本与逐句送 TTS 的口径保持一致：把英文词剥掉（push_sentence 里已逐句剥过）
        reply, stray = director.strip_latin(reply)
        if stray:
            plan.notes.append(f"台词夹杂英文词「{'、'.join(stray)}」，已剥掉")
        # 整段复核（只记不重写）：首句过了不代表后面没跑题——4B 常见的后半句失守
        # 是「先答一句，然后拐回写景」。写进 done.plan 给观测面板/QA 看，不额外加延迟。
        if plan.gate and reply and not gate_failed:
            late_checks = plan.gate_check(reply, recent=s.recent)
            if late_checks:
                plan.notes.extend(f"整段复核：{v}" for v in late_checks)
        tail_text = reply[consumed:] if consumed < len(reply) else ("" if consumed else reply)
        tail_text = tail_text.strip()
        if tail_text:
            await push_sentence(tail_text)

        await emit({"t": "reply", "text": reply})
        # 整段台词到手后再校正一次（首句看漏的关键词在这里补上；已经一致就不发事件）
        await dialog_fix_patch(reply)
        # 表演层第二帧：句子切完了，手势/视线的落点才能钉在具体句尾上。
        # rev 2 只补「还没到时间的」beat，客户端不会重播已经演过的动作。
        late = perf.late_beats(directive=directive, plan=plan, sentences=sent_list,
                               text=text, reply=reply)
        if late:
            await emit({"t": "perf", "rev": 2, "start_hold_ms": faces["hold_ms"], "beats": late})
            s.log("perf", rev=2, sentences=len(sent_list), beats=perf.describe(late))

        await tts_queue.put(None)
        await worker

        # 台词留档：下一轮提示词里的「不要重复这些句式 / 意象」用它
        if reply:
            s.recent.append(reply)
            del s.recent[:-6]
        # 本轮的计划留档：连着问同一件事时，下一轮要「换个说法 / 补新细节」而不是复读
        if plan.content:
            s.last_contents.append(plan.content)
            del s.last_contents[:-3]
        if plan.topic:
            s.last_topics.append(plan.topic)
            del s.last_topics[:-3]

        # 节拍推进：话题命中够多次 / 视觉事件 / 轮数上限（阈值都在卡包里配）
        s.turns_in_beat += 1
        if persona.beat_topic_hit(s.beat, user_text=text, reply=reply):
            s.beat_hits += 1
        advanced = False
        if persona.beat_advance(s.beat, s.turns_in_beat, hits=s.beat_hits,
                                event=directive["event"]):
            s.revealed.append(persona.BEATS[s.beat]["id"])
            s.beat = min(s.beat + 1, len(persona.BEATS) - 1)
            s.turns_in_beat = 0
            s.beat_hits = 0
            advanced = True

        await emit({"t": "suggest", "items": persona.suggestions(s.beat, tuple(plan.anchors)), "beat": s.beat})
        # no "idle" here: the client owns playback, it flips to idle when the
        # last queued sentence finishes (or immediately on barge-in).
        await emit({"t": "done", "audio": stats["audio"], "audio_errors": stats["errors"],
                    "first_audio_ms": stats["first_audio_ms"], "ms": int((time.time() - t0) * 1000),
                    "beat": s.beat, "advanced": advanced, "synth_ms": stats["synth_ms"],
                    # provider：这一轮**真正**是谁产出的台词。开场白来自卡包（provider=card），
                    # auto 链里会写 strata / llama / mock —— 静默掉到 mock 必须能从事件里看出来。
                    # provider：这一轮**真正**是谁产出的台词。preset = 命中预设库（0 生成），
                    # card = 开场白，其余是模型供应商。
                    "provider": ("preset" if hit is not None else
                                 "card" if kind == "open" else
                                 (getattr(llm, "last", "") or s.mode)),
                    # path / preset / act：给观测面板与 QA 用（谁回答的、命中了哪条、为什么算这个行为）
                    "path": "preset" if hit is not None else "gen",
                    "preset": hit.as_dict() if hit is not None else None,
                    "act": act_hit.as_dict() if act_hit is not None else None,
                    # 台词二次校正（问题 6 · 方案乙）：本轮按台词改了几次、改了什么，
                    # 观测面板与验收截图都读这一栏，避免"改没改"只能靠肉眼看。
                    "dialog_fix": {"n": patch_n, "fixes": list(patch_log),
                                   "emotion": (s.last_directive or directive or {}).get("emotion"),
                                   "action": (s.last_directive or directive or {}).get("action")},
                    # 首句延迟的可审计口径：first_text_ms = 第一句完整进入服务端的时间，
                    # first_audio_ms = 第一段音频真正发出；gate 字段说明闸门有没有触发重写。
                    "first_text_ms": first_text_ms,
                    # 语气词先出声的耗时（问题 4）：first_audio_ms 里如果是语气词，
                    # 这里给出它的时间点，验收表上"第一声"和"第一句台词"分开算。
                    "filler_ms": stats.get("filler_ms"),
                    "first_line_audio_ms": stats.get("first_line_audio_ms"),
                    "gate": {"on": gate_on, "done": gate_done,
                             "rewrites": 1 if gate_failed else 0,
                             "checks": gate_checks, "ms": round(gate_ms, 2),
                             "wait_ms": gate_wait_ms},
                    "plan": {"intent": plan.intent, "violations": plan.notes,
                             "topic": plan.topic, "anchors": plan.anchors[:5],
                             "gate": {"on": gate_on, "rewrites": 1 if gate_failed else 0},
                             "must": plan.must, "avoid": plan.avoid}})

        # 历史面板落库：和 s.history 用同一份 reply / directive / hit / stats，
        # 但 assistant 只写台词正文——history 那份要给模型看「首行 JSON + 台词」的协议，
        # 面板是给人看的，不能把 JSON 头显示出来。beat 用推进后的 s.beat，
        # 和 done 事件、观测面板看到的「第几拍」保持同一个口径。
        turn_i = len(s.history) // 2 + 1
        s.transcript.append({"i": turn_i, "role": "user",
                             "text": text or "（走进店里）", "ts": round(t0, 3),
                             "beat": s.beat})
        s.transcript.append({
            "i": turn_i, "role": "assistant", "text": reply,
            "ts": round(time.time(), 3), "beat": s.beat,
            "path": "preset" if hit is not None else "gen",
            "act": act_hit.act if act_hit is not None else "",
            "emotion": (s.last_directive or directive)["emotion"],
            "action": (s.last_directive or directive)["action"],
            "ms": int((time.time() - t0) * 1000),
            "first_audio_ms": stats["first_audio_ms"],
        })
        s.history.append({"role": "user", "content": text or "（走进店里）"})
        # 历史里存「模型真正说的协议格式」（首行 JSON + 台词），而不是只有台词：
        # 否则模型看着自己前几轮的纯文本回复，会以为这个协议是可选的，越聊越不守格式。
        s.history.append({"role": "assistant",
                          "content": json.dumps(directive, ensure_ascii=False) + "\n" + reply})
        s.log("turn_done", seq=seq, ms=int((time.time() - t0) * 1000), provider=s.mode,
              directive=directive, audio=stats["audio"], fixes=fixes, beat=s.beat)

        # 「活人配额」：同一拍连着命中预设到上限后，下一轮强制走生成，
        # 保证她时不时说一句没准备过的话（否则整段都像在念表）。
        if hit is not None:
            s.preset_run += 1
        else:
            s.preset_run = 0

        # 每轮一行 trace：预设命中率、首字延迟、表演覆盖都从这里统计（docs §7）
        trace.log({
            "turn": s.seq, "sid": s.sid, "kind": kind,
            "beat": persona.BEATS[s.beat]["id"], "rel": persona.BEATS[s.beat].get("rel", ""),
            "act": act_hit.act if act_hit is not None else "",
            "act_src": act_hit.source if act_hit is not None else "",
            "path": "preset" if hit is not None else "gen",
            "preset": hit.as_dict() if hit is not None else None,
            "preset_run": s.preset_run,
            "ms": int((time.time() - t0) * 1000),
            "first_text_ms": first_text_ms, "first_audio_ms": stats["first_audio_ms"],
            "audio": stats["audio"], "audio_errors": stats["errors"],
            "gate_rewrites": 1 if gate_failed else 0,
            "dialog_fix_n": patch_n,
            "emotion": (s.last_directive or directive or {}).get("emotion"),
            "action": (s.last_directive or directive or {}).get("action"),
            "notes": list(plan.notes)[:6],
            "reply": (reply or "")[:160],
        })
    except TurnCancelled:
        try:
            await queue.put({"t": "cancelled", "seq": seq})
        except Exception:  # noqa: BLE001
            pass
        s.log("turn_cancelled", seq=seq)
    except providers.LLMError as exc:
        s.log("turn_error", seq=seq, error=str(exc))
        try:
            await emit({"t": "error", "code": "llm", "message": str(exc), "recoverable": True})
        except TurnCancelled:
            s.log("turn_cancelled_late", seq=seq)
    except Exception as exc:  # noqa: BLE001
        s.log("turn_error", seq=seq, error=repr(exc), tb=traceback.format_exc()[-800:])
        # 内部异常一律把 traceback 打到服务端日志：这一层是"不该发生"的兜底，
        # 只在客户端看到一个 `超时` 之类的短语时，服务端必须能自证是哪一段炸的。
        print(f"[turn] 内部异常 seq={seq}\n{traceback.format_exc()}", flush=True)
        try:
            await emit({"t": "error", "code": "internal", "message": f"{type(exc).__name__}: {exc}",
                        "recoverable": True})
        except TurnCancelled:
            pass
    finally:
        if not worker.done():
            worker.cancel()
        try:
            await queue.put(None)
        except Exception:  # noqa: BLE001
            pass


async def ndjson_stream(queue: asyncio.Queue):
    while True:
        item = await queue.get()
        if item is None:
            return
        yield (json.dumps(item, ensure_ascii=False) + "\n").encode("utf-8")


@app.post("/api/session")
async def create_session(payload: dict | None = None):
    payload = payload or {}
    sid = payload.get("session_id") or uuid.uuid4().hex[:12]
    mode = (payload.get("mode") or LLM_MODE).lower()
    s = get_session(sid, mode)
    s.log("session_start", mode=mode)
    live_prewarm()      # 会话一建就热一条长连：用户还在想说什么，握手已经付掉了
    return {"session_id": sid, "mode": mode, "character": persona.CHARACTER["name"],
            "llm": llm_for(mode).name, "voice": tts.voice, "beat": s.beat}


@app.post("/api/turn")
async def turn(request: Request):
    body = await request.json()
    sid = body.get("session_id") or uuid.uuid4().hex[:12]
    mode = body.get("mode")
    s = get_session(sid, mode)
    text = (body.get("text") or "").strip()
    kind = "open" if body.get("kind") == "open" else "user"

    # a new turn always supersedes the running one: bump epoch, cancel the task
    s.epoch += 1
    epoch = s.epoch
    s.seq += 1
    seq = s.seq
    if s.task and not s.task.done():
        s.task.cancel()
        s.log("turn_preempted", seq=seq)
    queue: asyncio.Queue = asyncio.Queue()
    # 预热：LLM 首字 p50 ≈1.0s，比 edge 握手（≈850ms）长，正好把这段藏起来。
    # 放在 create_task 之前（非阻塞）：连得上就连得上，连不上合成时会再试并回落。
    live_prewarm()
    s.task = asyncio.create_task(run_turn(s, seq, epoch, text, kind, queue))
    # X-Turn-Seq 把「服务端认为这是第几轮」直接告诉前端：前端用它对齐 activeSeq，
    # 任何一侧多算/少算一次都不会再让整轮事件被当成迟到事件丢掉。
    headers = {"X-Session": sid, "X-Turn-Seq": str(seq), "Cache-Control": "no-store",
               "X-Accel-Buffering": "no"}
    return StreamingResponse(ndjson_stream(queue), media_type="application/x-ndjson", headers=headers)


@app.post("/api/interrupt")
async def interrupt(payload: dict):
    s = get_session(payload.get("session_id") or "", payload.get("mode"))
    seq = payload.get("seq")
    s.epoch += 1
    # 打断也占一个轮次号：客户端在打断时同样会 ++seq，两边必须同步，
    # 否则打断后的下一轮事件全部落在「旧 seq」上被前端丢掉（角色像没听见你说话）。
    s.seq += 1
    if s.task and not s.task.done():
        s.task.cancel()
    s.log("interrupt", seq=seq, epoch=s.epoch, next_seq=s.seq)
    return {"ok": True, "epoch": s.epoch, "seq": s.seq}


@app.post("/api/stt")
async def speech_to_text(audio: UploadFile = File(...), session_id: str = Form("")):
    s = get_session(session_id)
    data = await audio.read()
    if not data:
        return JSONResponse({"error": "empty audio"}, status_code=400)
    tmp = MEDIA / "stt" / f"{uuid.uuid4().hex}.webm"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    t0 = time.time()
    try:
        result = await stt.transcribe(tmp)
    except Exception as exc:  # noqa: BLE001
        s.log("stt_error", error=repr(exc))
        return JSONResponse({"error": f"STT 失败: {type(exc).__name__}: {exc}"}, status_code=503)
    finally:
        tmp.unlink(missing_ok=True)
    s.log("stt", text=result["text"], ms=result["ms"], bytes=len(data))
    return {"text": result["text"], "ms": result["ms"], "audio_s": result["audio_s"],
            "total_ms": int((time.time() - t0) * 1000)}


@app.get("/api/events/{sid}")
async def events(sid: str):
    s = SESSIONS.get(sid)
    if not s:
        return {"events": []}
    return {"session_id": sid, "seq": s.seq, "epoch": s.epoch, "beat": s.beat,
            "turns_in_beat": s.turns_in_beat, "events": s.events[-200:]}


@app.get("/api/history/{sid}")
async def history(sid: str):
    """历史面板：这个会话已经发生的对话（只读，逐轮）。

    为什么单独一条而不是复用 /api/events：events 是给观测面板的流水（含内部字段），
    这里要的是**人能读的聊天记录**——assistant 只留台词正文（不含首行 JSON）。
    会话不存在时返回空列表而不是 404：前端刷新后会换一个新 sid，面板轮询旧 sid
    不该在控制台里刷一片红色 404。
    """
    s = SESSIONS.get(sid)
    if s is None:
        return {"session_id": sid, "mode": None, "beat": 0, "turns": []}
    return {"session_id": sid, "mode": s.mode, "beat": s.beat, "turns": s.transcript}


@app.post("/api/debug/inject")
async def inject(payload: dict):
    s = get_session(payload.get("session_id") or "")
    for key in ("llm_timeout", "tts_fail", "slow_tts", "gate_fail", "time_fail"):
        if key in payload:
            s.inject[key] = bool(payload[key])
    # A/B 用：进程内切换 MIRA_TTS_LIVE 档位（off/first/all），不用重启服务。
    # 为什么需要：A/B 必须交替跑、同一个进程、同一批缓存，重启一次就多一批变量。
    # 只影响本进程；重启后回到环境变量值。
    if "tts_live" in payload:
        from .tts import live_edge

        live_edge.LIVE.override_mode = str(payload["tts_live"] or "").strip().lower() or None
        print(f"[tts] live 档位切到 {live_edge.live_mode()}（A/B）", flush=True)
    s.log("inject", **s.inject)
    return {"ok": True, "inject": s.inject, "tts_live": _live_status()}


@app.get("/api/health")
async def health():
    """健康检查：一台新机器上「缺了什么、降到了哪一档」必须在这里一眼看到。

    为什么要写这个：演示最怕的不是缺模型，是**缺模型但行为看起来正常**。
    之前决策层没有模型时会静默走关键词门、生成退到 mock 剧本，页面上照样能说话，
    只有深入看日志才知道这一场的质量已经掉了一档。所以这里把三层的真实档位
    连同「找过哪些路径」一起吐出来，录屏前刷一次就能确认自己在哪套配置上演示。
    """
    dec = decision.decision()
    return {
        "ok": True, "llm": llm_for(LLM_MODE).name, "mode": LLM_MODE, "voice": tts.voice,
        # 自动链的**真实**成员：前端那句「实际链：…」由它拼，不再手写死 25B。
        # 1234 端口没起时这里就是 ["Qwen3", "Mock"]，与页面上看到的一致。
        "chain": [CHAIN_LABEL.get(k, k) for k in AUTO_CHAIN],
        "chain_keys": list(AUTO_CHAIN),
        "modes": list(LLMS) + ["cloud"], "tts_cache": len(list(providers.TTS_CACHE.glob("*.mp3"))),
        "tts_stream": stream_tts_enabled(), "neural_url": NEURAL_TTS_URL,
        # live 档位的自证：前端/QA 不用看日志就能知道长连是不是热的、赚到了多少
        "tts_live": _live_status(),
        # 预设库：条数 / 路径 / 覆盖的行为。健康检查里能看到它，才知道加速层在不在。
        "presets": PRESETS.summary(),
        "trace": trace.enabled(),
        "cloud": {"configured": bool(CLOUD["key"]), "model": CLOUD["model"]},
        "decision": dec.status(),
        "decision_paths": [
            paths.describe("MIRA_SELECTOR_MODEL", "bge-reranker-base"),
            paths.describe("MIRA_ROUTER_MODEL", "arch-router-1.5b"),
        ],
        "last_beat_pick": persona.LAST_BEAT_PICK,
        "card": {"id": persona.CARD_ID, "version": persona.CARD_VERSION,
                 "path": persona.CARD.get("_path"),
                 "warnings": persona.CARD.get("_warnings", [])}}


# ---------------------------------------------------------------- 云端模型（P0-4）
# 只支持 DeepSeek，只填 key，**临时使用、不保存**：
#   - key 只放在本进程的 CLOUD 字典里，重启即失效；
#   - 不写 .env / 不写磁盘 / 不回传给前端（只回 masked 预览）；
#   - 填之前先拿一个最小请求验一次，验不过就不写入，避免「看起来配好了其实不可用」。

async def _probe_deepseek(key: str, model: str) -> tuple[bool, str]:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=6.0),
                                     trust_env=False) as c:
            r = await c.post(f"{CLOUD['base']}/v1/chat/completions",
                             headers={"Authorization": f"Bearer {key}"},
                             json={"model": model, "stream": False, "max_tokens": 1,
                                   "messages": [{"role": "user", "content": "hi"}]})
        if r.status_code == 200:
            return True, ""
        if r.status_code in (401, 403):
            return False, "Key 无效或没有权限（401/403）"
        return False, f"DeepSeek 返回 HTTP {r.status_code}：{r.text[:160]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"连不上 DeepSeek：{type(exc).__name__}: {exc}"


@app.get("/api/cloud")
async def cloud_status():
    return {"configured": bool(CLOUD["key"]), "model": CLOUD["model"],
            "note": "仅存于服务进程内存，重启即失效；不写磁盘、不回传。"}


@app.post("/api/cloud")
async def cloud_set(payload: dict | None = None):
    payload = payload or {}
    key = str(payload.get("key") or "").strip()
    model = str(payload.get("model") or CLOUD["model"]).strip() or "deepseek-chat"
    if not key:
        return JSONResponse({"ok": False, "error": "key 为空"}, status_code=400)
    ok, err = await _probe_deepseek(key, model)
    if not ok:
        return JSONResponse({"ok": False, "error": err}, status_code=400)
    CLOUD["key"] = key
    CLOUD["model"] = model
    print(f"[cloud] DeepSeek 已配置：model={model} key={len(key)} 字符（仅内存）", flush=True)
    s = len(key)
    masked = (key[:4] + "***" + key[-4:]) if s > 10 else "***"
    return {"ok": True, "configured": True, "model": model, "masked": masked}


@app.delete("/api/cloud")
async def cloud_clear():
    CLOUD["key"] = ""
    return {"ok": True, "configured": False}


@app.get("/api/tts_stream")
async def tts_stream(text: str, emotion: str = "neutral", position: str = "follow"):
    """同源流式 PCM（audio/L16 24000 单声道）。客户端边收边排期播放：第一块到就出声。

    两个音源共用这一条契约，播放器不区分：
      live    edge 长连（默认）：一条保温 WSS 合成，边收 mp3 边增量解码成 PCM。
              音色/参数和整句路径完全一致（同一个 tts.voice + providers.style_for）。
      neural  神经侧车：原样转发侧车的裸 PCM。

    客户端断开（打断）时 StreamingResponse 会取消这个生成器 → live 连接被标脏并
    立刻停读 WSS（下次 ensure() 重连），所以打断不会把上一句的残音带进下一轮。
    """
    engine = stream_engine()
    if engine == "live":
        from .tts import live_edge

        st = tts.style_for(text, emotion)

        async def gen_live():
            async for pcm in live_edge.LIVE.pcm_stream(
                text, voice=tts.voice, rate=st["rate"], pitch=st["pitch"],
                volume=st["volume"],
            ):
                yield pcm

        return StreamingResponse(gen_live(), media_type="audio/L16;rate=24000;channels=1",
                                 headers={"Cache-Control": "no-store", "X-Sample-Rate": "24000",
                                          "X-TTS-Engine": "live",
                                          "X-Accel-Buffering": "no"})

    import httpx

    async def gen():
        client = httpx.AsyncClient(timeout=None)
        try:
            async with client.stream(
                "POST", f"{NEURAL_TTS_URL}/synth_stream",
                json={"text": text, "emotion": emotion, "position": position},
            ) as r:
                if r.status_code != 200:
                    return
                async for chunk in r.aiter_raw():
                    if chunk:
                        yield chunk
        except Exception as exc:  # noqa: BLE001
            print(f"[tts] stream fallback: {type(exc).__name__}: {exc}", flush=True)
        finally:
            await client.aclose()

    return StreamingResponse(gen(), media_type="audio/L16;rate=24000;channels=1",
                             headers={"Cache-Control": "no-store", "X-Sample-Rate": "24000",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/tts_file")
async def tts_file(text: str, emotion: str = "neutral", position: str = "follow", index: int = 0):
    """整句 mp3 兜底：流式失败或环境不支持时走这里（内部仍是 TTSProvider，含 edge 兜底）。"""
    try:
        path, cached = await tts.synth(text, emotion=emotion, position=position)
        return {"url": f"/media/tts/{path.name}", "cached": cached, "text": text, "index": index}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=503)


@app.get("/api/_test_pcm")
async def test_pcm(ms: int = 1200, rate: int = 24000):
    """QA 专用：一段带包络的 220Hz 正弦裸 PCM，用来单独验证客户端的流式排期是否无缝。"""
    import math
    import struct

    n = int(rate * ms / 1000)
    buf = bytearray()
    secs = ms / 1000.0
    for i in range(n):
        t = i / rate
        env = min(1.0, t * 10) * min(1.0, max(0.0, (secs - t) * 10))
        buf += struct.pack("<h", int(9000 * env * math.sin(2 * math.pi * 220 * t)))
    return Response(bytes(buf), media_type="audio/L16;rate=24000;channels=1",
                    headers={"X-Sample-Rate": str(rate), "X-Duration-Ms": str(ms)})


@app.get("/api/trace")
async def trace_tail(n: int = 20):
    """调试面板用：最近 n 轮 trace（她为什么回这句、走了哪条路、每段耗时）。

    默认只在文件里（logs/turns.jsonl）；面板关掉时不影响这条接口，看的人自己开。
    `MIRA_TRACE=0` 时不写盘，这里就只能拿到空表——这是刻意的一致性。
    """
    return {"enabled": trace.enabled(), "items": trace.tail(max(1, min(200, n)))}


@app.get("/api/card")
async def card_snapshot():
    """卡包快照：素材清单、指令标签、句间停顿、节拍建议。

    「一份数据三处用」的第三处——客户端不再硬编码素材路径，改卡就能换素材。
    """
    return persona.MANIFEST


@app.on_event("startup")
async def warm_tts() -> None:
    """后台预热：Mock 台词 + 决策层模型 + 4B 的系统提示前缀。

    为什么必须预热（实测）：第一次 /api/turn 要现加载 bge-reranker + arch-router，
    首字延迟从 3.5s 涨到 14.7s——用户双击 start.bat 后说的第一句「你好」正好撞上这个，
    听起来就是"卡死了"。4B 那边同理：系统提示有 2.7k 字，第一次要全量预填充。
    """

    async def _warm_tts() -> None:
        for item in persona.MOCK_SCRIPT:
            try:
                await tts.synth(item["reply"], emotion=item["directive"]["emotion"])
            except Exception:  # noqa: BLE001
                pass

    async def _warm_decision() -> None:
        try:
            print(f"[warm] selector: {await asyncio.to_thread(selector.warm)}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[warm] selector failed: {type(exc).__name__}: {exc}", flush=True)

    async def _warm_llm() -> None:
        """4B 的系统提示前缀进 KV cache，同时让降级链记住哪个 provider 活着。

        用 auto 而不是 llama：链里第一个是 Strata，演示机上它常常没起，预热这一发
        正好把「谁活着」记下来（FallbackLLM 粘住成功项 + 冷却死掉的），
        用户说的第一句话就不用再白等那一次失败探测。
        """
        try:
            msgs = [
                {"role": "system", "content": persona.render_system_prompt(0)},
                {"role": "user", "content": "你好"},
            ]
            async for _ in LLMS["auto"].stream(msgs):
                pass
            print(f"[warm] llm: auto -> {LLMS['auto'].last}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[warm] llm: {type(exc).__name__}: {exc}", flush=True)

    async def _warm_presets() -> None:
        """把预设台词预合成进 media/tts 缓存：命中路径要「说完就出声」。

        为什么要有它：命中路径要「说完就出声」，而冷合成一句要 0.7–1.5s（侧车串行），
        等于把省下的模型时间又还回去。这里按优先级从高到低预合成，已经缓存过的句子秒返回。
        和 mock 台词串在同一条队列里跑（见 _warm_audio），不并行抢卡。
        """
        await asyncio.sleep(0.5)
        ok = 0
        try:
            items = sorted(PRESETS.items, key=lambda p: -p.priority)
            for p in items:
                emo = (p.perf or {}).get("emotion") or "neutral"
                for line in p.lines:
                    try:
                        await tts.synth(line, emotion=emo, position="first")
                        ok += 1
                    except Exception:  # noqa: BLE001  侧车没起/单句失败都不致命
                        continue
            print(f"[warm] presets: {ok}/{sum(len(p.lines) for p in items)} 句预合成完成",
                  flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[warm] presets failed: {type(exc).__name__}: {exc}", flush=True)

    async def _warm_fillers() -> None:
        """预合成"开口前的语气词"：它是她的**第一声**，必须来自本地静态文件（0 合成）。"""
        for f in perf.FILLERS:
            try:
                path, _cached = await tts.synth(f, emotion="neutral", position="first")
                FILLER_PATHS[f] = path
            except Exception:  # noqa: BLE001  没合成出来只是少一个语气词，不影响对话
                continue
        print(f"[warm] fillers: {len(FILLER_PATHS)}/{len(perf.FILLERS)} 句预合成", flush=True)

    async def _warm_audio() -> None:
        """音频预热串成一条队列。

        为什么不能并行：侧车只有一个模型、串行处理请求；两个预热任务并行只会互相排队，
        还会和用户的第一句话抢算力（实测第一句被拖到 7s，就是这个原因）。
        顺序也有讲究：**先热预设**——用户的第一句话大概率命中预设，先把它备好。
        """
        await _warm_presets()
        # 语气词排在预设之后、Mock 台词之前：用户第一句话多数命中预设，
        # 而语气词只有走生成路径的轮次才用得上（先用先热，别把它排到最后）。
        await _warm_fillers()
        await _warm_tts()

    async def _warm_live() -> None:
        """长连预热 + 保活：启动就连一条，此后每轮 turn 开始再检查一次（见 live_prewarm）。

        为什么启动就付一次 850ms：录屏/演示时第一句话是最容易被相机拍到的，
        它不能背这个握手。保活任务负责"一直热着"，turn 级预热负责"死掉也能马上热回来"。
        """
        try:
            from .tts import live_edge

            live_edge.LIVE.start()
            await live_edge.LIVE.ensure()
            print(f"[warm] tts-live: {live_edge.LIVE.stats['last_connect_ms']}ms "
                  f"(MIRA_TTS_LIVE={live_edge.live_mode()})", flush=True)
        except Exception as exc:  # noqa: BLE001  长连起不来只是慢一点，不是致命错误
            print(f"[warm] tts-live 未就绪（会自动回退整句 mp3）："
                  f"{type(exc).__name__}: {exc}", flush=True)

    asyncio.create_task(_warm_audio())
    asyncio.create_task(_warm_decision())
    asyncio.create_task(_warm_llm())
    asyncio.create_task(_warm_live())


@app.on_event("shutdown")
async def close_live_tts() -> None:
    """退出时把长连关掉：不留半条 WSS 让对端等超时。"""
    try:
        from .tts import live_edge

        await live_edge.LIVE.close()
    except Exception:  # noqa: BLE001
        pass


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/debug")
async def debug_page():
    return FileResponse(STATIC / "debug.html")
