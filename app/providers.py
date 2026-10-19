"""Model / media providers: LLM (mock or llama.cpp), TTS (edge-tts), STT (faster-whisper).

Every provider is swappable: the app only depends on the small interfaces below,
so a different vendor can be plugged in without touching the scene state machine.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import time
from pathlib import Path
from typing import AsyncIterator

ROOT = Path(__file__).resolve().parent.parent
TTS_CACHE = ROOT / "media" / "tts"
TTS_CACHE.mkdir(parents=True, exist_ok=True)

from .persona import MARKER  # noqa: E402  (单一来源，避免两处漂移)


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------- LLM


class BaseLLM:
    name = "base"

    async def stream(self, messages: list[dict], *, timeout: float = 30.0) -> AsyncIterator[str]:
        raise NotImplementedError


class MockLLM(BaseLLM):
    """Deterministic scripted partner: no model, no network, no key.

    The script advances only when the user actually says something, and a few
    keywords override the beat so the visual events stay *content-triggered*.
    """

    name = "mock"

    def __init__(self) -> None:
        from . import persona

        self.persona = persona

    async def stream(self, messages: list[dict], *, timeout: float = 30.0) -> AsyncIterator[str]:
        from . import persona

        user_text = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_text = m.get("content", "")
                break
        beat = int(messages[-1].get("beat", 0)) if messages and "beat" in messages[-1] else 0
        beat = min(beat, len(persona.MOCK_SCRIPT) - 1)
        item = persona.MOCK_SCRIPT[beat]
        directive = dict(item["directive"])

        # keyword-triggered events (content-driven, not timed); never on the
        # scripted opening beat, the user has not said anything yet.
        if messages and messages[-1].get("kind") != "open":
            for event, words in persona.MOCK_KEYWORD_EVENTS.items():
                if any(w in user_text for w in words):
                    if event == "photo_reveal" and beat < 2:
                        continue  # 太早拿出照片不符合剧情节拍
                    directive["event"] = event
                    if event == "photo_reveal":
                        directive["action"] = "show_photo"
                    if event == "thunder":
                        directive["effect"] = "thunder"
                        directive["emotion"] = "surprised"
                    break

        # 与真实模型同一条协议：首行 JSON 指令，换行后才是台词
        text = json.dumps(directive, ensure_ascii=False) + "\n" + item["reply"]

        # emulate token streaming so the frontend pipeline is the same as real mode
        i = 0
        while i < len(text):
            step = random.randint(2, 5)
            yield text[i:i + step]
            i += step
            await asyncio.sleep(0.045)


class LlamaLLM(BaseLLM):
    """llama.cpp `llama-server` (OpenAI-compatible /v1/chat/completions)."""

    name = "llama"

    def __init__(self, base_url: str, model: str = "qwen3-4b-instruct"):
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def stream(self, messages: list[dict], *, timeout: float = 30.0) -> AsyncIterator[str]:
        import httpx

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": 0.85,
            "top_p": 0.9,
            "max_tokens": 220,
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0)) as client:
                async with client.stream("POST", f"{self.base_url}/v1/chat/completions", json=payload) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "ignore")[:300]
                        raise LLMError(f"llama-server HTTP {resp.status_code}: {body}")
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            yield piece
        except httpx.TimeoutException as exc:
            raise LLMError(f"LLM 超时（{timeout:.0f}s 未完成）") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM 连接失败: {exc}") from exc


# --------------------------------------------------------------------- TTS


class TTSProvider:
    """edge-tts → mp3 file cache.

    edge-tts 不支持 SSML style，只能用 rate/pitch/volume 做「语气」。
    这里按情绪给一套基调，再按句子的标点做微调（问句抬一点、省略号放慢），
    让 4 段音色不再是一条平的朗读。
    """

    EMOTION_STYLE = {
        "neutral":   {"rate": "+0%",  "pitch": "-4Hz", "volume": "+0%"},
        "smile":     {"rate": "+5%",  "pitch": "+6Hz", "volume": "+6%"},
        "worried":   {"rate": "-8%",  "pitch": "-8Hz", "volume": "-6%"},
        "surprised": {"rate": "+7%",  "pitch": "+14Hz", "volume": "+8%"},
        "thinking":  {"rate": "-12%", "pitch": "-10Hz", "volume": "-4%"},
    }

    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural", rate: str = "+0%", pitch: str = "-4Hz"):
        self.voice = voice
        self.rate = rate
        self.pitch = pitch

    @staticmethod
    def _shift(value: str, delta: int) -> str:
        n = int(value.rstrip("%Hz"))
        unit = "Hz" if value.endswith("Hz") else "%"
        return f"{n + delta:+d}{unit}"

    def style_for(self, text: str, emotion: str) -> dict[str, str]:
        base = dict(self.EMOTION_STYLE.get(emotion, self.EMOTION_STYLE["neutral"]))
        if text.rstrip().endswith(("？", "?")):
            base["pitch"] = self._shift(base["pitch"], 3)
            base["rate"] = self._shift(base["rate"], 2)
        if "……" in text or "..." in text:
            base["rate"] = self._shift(base["rate"], -4)
        return base

    def cache_path(self, text: str, emotion: str) -> Path:
        st = self.style_for(text, emotion)
        key = "|".join([self.voice, st["rate"], st["pitch"], st["volume"], text])
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return TTS_CACHE / f"{h}.mp3"

    async def synth(self, text: str, emotion: str = "neutral", timeout: float = 12.0) -> tuple[Path, bool]:
        """Return (path, used_cache)."""
        path = self.cache_path(text, emotion)
        if path.exists() and path.stat().st_size > 800:
            return path, True
        import edge_tts

        st = self.style_for(text, emotion)
        tmp = path.with_suffix(".part.mp3")
        try:
            comm = edge_tts.Communicate(text, self.voice, rate=st["rate"], pitch=st["pitch"],
                                        volume=st["volume"])
            await asyncio.wait_for(comm.save(str(tmp)), timeout=timeout)
            if not tmp.exists() or tmp.stat().st_size < 500:
                raise RuntimeError("TTS 输出为空")
            os.replace(tmp, path)
            return path, False
        except Exception:
            tmp.unlink(missing_ok=True)
            raise


# --------------------------------------------------------------------- STT


class STTProvider:
    """faster-whisper (CPU int8). Lazy-loaded so the app boots instantly."""

    def __init__(self, size: str = "small"):
        self.size = size
        self._model = None
        self._lock = asyncio.Lock()

    def _load(self):
        if self._model is None:
            os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
            from faster_whisper import WhisperModel

            t0 = time.time()
            self._model = WhisperModel(
                self.size,
                device="cpu",
                compute_type="int8",
                download_root=str(ROOT / "models" / "whisper"),
                num_workers=2,
            )
            print(f"[stt] loaded faster-whisper-{self.size} in {time.time()-t0:.1f}s", flush=True)
        return self._model

    async def transcribe(self, audio_path: Path) -> dict:
        async with self._lock:
            model = await asyncio.get_running_loop().run_in_executor(None, self._load)
            t0 = time.time()

            def _run():
                segments, info = model.transcribe(
                    str(audio_path), language="zh", beam_size=1, vad_filter=True,
                    condition_on_previous_text=False,
                )
                return "".join(s.text for s in segments).strip(), info.duration

            text, duration = await asyncio.get_running_loop().run_in_executor(None, _run)
        text = re.sub(r"[，。！？、\s]+$", "", text)
        return {"text": text, "ms": int((time.time() - t0) * 1000), "audio_s": round(duration, 2)}
