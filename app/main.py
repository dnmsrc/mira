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
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import persona, providers

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
MEDIA = ROOT / "media"
MEDIA.mkdir(parents=True, exist_ok=True)

LLM_MODE = os.environ.get("LLM_MODE", "mock").lower()      # mock | llama
LLAMA_URL = os.environ.get("LLAMA_URL", "http://127.0.0.1:8080")
WHISPER_SIZE = os.environ.get("WHISPER_SIZE", "small")

LLMS = {
    "mock": providers.MockLLM(),
    "llama": providers.LlamaLLM(LLAMA_URL),
}


def llm_for(mode: str | None):
    return LLMS.get((mode or LLM_MODE).lower(), LLMS["mock"])


tts = providers.TTSProvider(voice=persona.CHARACTER["voice"])
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
        self.revealed: list[str] = []
        self.last_directive = dict(persona.DEFAULT_DIRECTIVE)
        self.played: set[str] = set()          # 已经放过的视觉事件（不重复播）
        self.history: list[dict[str, str]] = []
        self.task: asyncio.Task | None = None
        self.events: list[dict[str, Any]] = []
        self.inject = {"llm_timeout": False, "tts_fail": False, "slow_tts": False}
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


def take_sentences(chunk: str) -> tuple[list[str], int]:
    """Split `chunk` into speakable sentences; return (sentences, consumed_chars)."""
    out: list[str] = []
    start = 0
    for i, ch in enumerate(chunk):
        if ch in SENT_END and i - start >= 2:
            out.append(chunk[start:i + 1])
            start = i + 1
        elif ch in SENT_SOFT and i - start >= 14:
            out.append(chunk[start:i + 1])
            start = i + 1
    return out, start


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
    """节拍闸门：不允许模型把还没到的事件提前放出来。"""
    fixes: list[str] = []
    if directive["event"] == "photo_reveal" and beat < 3:
        directive["event"] = "none"
        fixes.append("photo_reveal 早于第 4 拍，已拦下")
    if directive["event"] == "last_train" and beat < 5:
        directive["event"] = "none"
        fixes.append("last_train 早于第 6 拍，已拦下")
    if directive["event"] == "photo_reveal" and directive["action"] != "show_photo":
        directive["action"] = "show_photo"
    if directive["event"] == "thunder" and directive["effect"] != "thunder":
        directive["effect"] = "thunder"
    return fixes


def ledger_text(s: Session) -> str:
    known: list[str] = []
    if "admit" in s.revealed:
        known.append("你说过你在等一个「说好会来的人」")
    if "photo" in s.revealed:
        known.append("你给他看过三年前的那张照片")
    if "absence" in s.revealed:
        known.append("你说过他后来再没出现过")
    if not known:
        known.append("对方什么都不知道")
    known.append(f"这是你们的第 {len(s.history) // 2 + 1} 轮对话")
    return "；".join(known)


KEYWORD_LABEL = {"thunder": "雷/闪电", "last_train": "末班/打烊", "photo_reveal": "照片/相机"}


def keyword_event(text: str, beat: int, played: set[str]) -> str | None:
    """内容触发的兜底：小模型（4B）经常不敢在指令里给 event。

    题目要求「至少 1 个由当前对话内容触发的明显视觉事件，不能只按计时器播放」，
    所以事件权限仍然绑在台词内容上：用户这句话里出现了触发词、且剧情节拍允许、
    且这个事件还没放过，才补一个。人设/节拍闸门照旧生效（早于第 4 拍不拿照片）。
    """
    for event, words in persona.MOCK_KEYWORD_EVENTS.items():
        if event in played:
            continue
        if not any(w in (text or "") for w in words):
            continue
        if event == "photo_reveal" and beat < 3:
            continue
        if event == "last_train" and beat < 5:
            continue
        return event
    return None


async def run_turn(s: Session, seq: int, epoch: int, text: str, kind: str, queue: asyncio.Queue) -> None:
    t0 = time.time()
    emit = make_emitter(s, seq, epoch, queue)
    tts_queue: asyncio.Queue = asyncio.Queue()
    stats = {"audio": 0, "errors": 0, "first_audio_ms": None, "speaking": False}
    ctx = {"emotion": "neutral"}

    async def tts_worker() -> None:
        while True:
            item = await tts_queue.get()
            if item is None:
                return
            sentence, index = item
            if s.inject["tts_fail"]:
                stats["errors"] += 1
                await emit({"t": "audio_error", "text": sentence, "index": index,
                            "message": "TTS 合成失败（注入故障），降级为字幕 + 浏览器朗读"})
                continue
            if s.inject["slow_tts"]:
                await asyncio.sleep(3.0)
            try:
                path, cached = await tts.synth(sentence, emotion=ctx["emotion"])
                if not stats["speaking"]:
                    stats["speaking"] = True
                    stats["first_audio_ms"] = int((time.time() - t0) * 1000)
                    await emit({"t": "state", "state": "speaking"})
                stats["audio"] += 1
                await emit({"t": "audio", "url": f"/media/tts/{path.name}", "text": sentence,
                            "index": index, "emotion": ctx["emotion"], "cached": cached})
            except TurnCancelled:
                return
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                await emit({"t": "audio_error", "text": sentence, "index": index,
                            "message": f"TTS 不可用：{type(exc).__name__}，降级为字幕"})

    worker = asyncio.create_task(tts_worker())
    try:
        await emit({"t": "state", "state": "thinking"})
        await emit({"t": "turn_start", "provider": s.mode, "kind": kind, "beat": s.beat})

        llm = llm_for(s.mode)
        messages: list[dict] = [
            {"role": "system", "content": persona.render_system_prompt(s.beat, ledger=ledger_text(s))}
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

        if s.inject["llm_timeout"]:
            await asyncio.sleep(3.0)
            raise providers.LLMError("LLM 超时（注入故障：3s 内没有返回任何 token）")

        raw = ""
        header_end = 0
        legacy = False
        directive: dict | None = None
        fixes: list[str] = []
        reply = ""
        consumed = 0
        sent_index = 0
        last_partial = 0.0
        last_shown = ""

        def finalize(raw_directive: dict | None, extra: list[str]) -> tuple[dict, list[str]]:
            """白名单收敛 → 节拍闸门 → 关键词补事件（内容触发兜底）。"""
            if raw_directive is None:
                out, f = dict(persona.DEFAULT_DIRECTIVE), []
            else:
                out, f = persona.sanitize_directive(raw_directive)
            f = list(extra) + f + guard_story(out, s.beat)
            if out["event"] == "none" and kind != "open":
                ev = keyword_event(text, s.beat, s.played)
                if ev:
                    out["event"] = ev
                    if ev == "photo_reveal":
                        out["action"] = "show_photo"
                    if ev == "thunder":
                        out["effect"] = "thunder"
                    f.append(f"模型没给 event，按台词里的「{KEYWORD_LABEL[ev]}」补了 {ev}")
            if out["event"] != "none":
                s.played.add(out["event"])
            return out, f

        async for piece in stream_llm(llm.stream(messages), total_timeout=35.0, first_timeout=14.0):
            raw += piece
            if directive is None:
                state, obj, hdr = header_probe(raw)
                if state == "json":
                    directive, fixes = finalize(obj, [])
                    ctx["emotion"] = directive["emotion"]
                    header_end = hdr
                    await emit({"t": "directive", "directive": directive, "fixes": fixes,
                                "parsed": True})
                    s.last_directive = directive
                elif state == "plain":
                    # 模型没按首行协议：整段当台词，指令走默认值（fixes 里留痕，可观测）
                    legacy = True
                    header_end = hdr
                    directive, fixes = finalize(None, ["模型没有按首行协议输出指令，使用默认表现"])
                    ctx["emotion"] = directive["emotion"]
                    await emit({"t": "directive", "directive": directive, "fixes": fixes,
                                "parsed": False})
                    s.last_directive = directive

            if directive is None:
                speech = ""  # 首行 JSON 还没闭合：半截 JSON 绝不能送 TTS
            elif legacy:
                speech = visible_reply(raw)
            else:
                speech = raw[header_end:]
            chunk = speech[consumed:] if consumed <= len(speech) else ""
            sentences, used = take_sentences(chunk)
            for sent in sentences:
                if sent.strip():
                    await tts_queue.put((sent.strip(), sent_index))
                    sent_index += 1
            consumed = min(consumed + used, len(speech))
            now = time.monotonic()
            if speech and speech != last_shown and now - last_partial > 0.08:
                last_partial = now
                last_shown = speech
                await emit({"t": "subtitle_partial", "text": speech})

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
            await emit({"t": "directive", "directive": directive, "fixes": fixes,
                        "parsed": parsed})
            s.last_directive = directive

        reply = visible_reply(raw) if legacy else raw[header_end:]
        if persona.MARKER in reply:
            reply = visible_reply(reply)
        tail_text = reply[consumed:].strip() if consumed <= len(reply) else ""
        if tail_text:
            await tts_queue.put((tail_text, sent_index))
            sent_index += 1
        reply = reply.strip()

        await emit({"t": "reply", "text": reply})

        await tts_queue.put(None)
        await worker

        # 节拍推进：至少聊够 TURNS_PER_BEAT 轮，或触发了本拍的视觉事件
        s.turns_in_beat += 1
        advanced = False
        if directive["event"] != "none" or s.turns_in_beat >= persona.TURNS_PER_BEAT:
            if s.beat < len(persona.BEATS) - 1:
                s.revealed.append(persona.BEATS[s.beat]["id"])
                s.beat += 1
                s.turns_in_beat = 0
                advanced = True

        await emit({"t": "suggest", "items": persona.beat_suggestions(s.beat), "beat": s.beat})
        # no "idle" here: the client owns playback, it flips to idle when the
        # last queued sentence finishes (or immediately on barge-in).
        await emit({"t": "done", "audio": stats["audio"], "audio_errors": stats["errors"],
                    "first_audio_ms": stats["first_audio_ms"], "ms": int((time.time() - t0) * 1000),
                    "beat": s.beat, "advanced": advanced})

        s.history.append({"role": "user", "content": text or "（走进店里）"})
        # 历史里存「模型真正说的协议格式」（首行 JSON + 台词），而不是只有台词：
        # 否则模型看着自己前几轮的纯文本回复，会以为这个协议是可选的，越聊越不守格式。
        s.history.append({"role": "assistant",
                          "content": json.dumps(directive, ensure_ascii=False) + "\n" + reply})
        s.log("turn_done", seq=seq, ms=int((time.time() - t0) * 1000), provider=s.mode,
              directive=directive, audio=stats["audio"], fixes=fixes, beat=s.beat)
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


@app.post("/api/debug/inject")
async def inject(payload: dict):
    s = get_session(payload.get("session_id") or "")
    for key in ("llm_timeout", "tts_fail", "slow_tts"):
        if key in payload:
            s.inject[key] = bool(payload[key])
    s.log("inject", **s.inject)
    return {"ok": True, "inject": s.inject}


@app.get("/api/health")
async def health():
    return {"ok": True, "llm": llm_for(LLM_MODE).name, "mode": LLM_MODE, "voice": tts.voice,
            "modes": list(LLMS), "tts_cache": len(list(providers.TTS_CACHE.glob("*.mp3")))}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/debug")
async def debug_page():
    return FileResponse(STATIC / "debug.html")
