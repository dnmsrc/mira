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
import urllib.parse
from pathlib import Path
from typing import AsyncIterator

ROOT = Path(__file__).resolve().parent.parent
TTS_CACHE = ROOT / "media" / "tts"
TTS_CACHE.mkdir(parents=True, exist_ok=True)
# 后处理管线自己的缓存目录默认是 media/tts_lab，而 /api/turn 吐的 URL 是 /media/tts/<name>；
# 两边必须指向同一个目录，否则前端拿到 404 音频、字幕一闪就掉回待机（实测踩过）。
os.environ.setdefault("MIRA_TTS_CACHE", str(TTS_CACHE))

from . import persona
from .persona import MARKER  # noqa: E402  (单一来源，避免两处漂移)

# FallbackLLM 里每个 provider 拿到第一个字的预算（秒）。
# 依据：4B 在这台机器上 GPU 正常时首字 ~0.3-0.8s；GPU 被占满退到 CPU 时会到 40s+，
# 那种情况应该直接降级，而不是让用户干等到外层 14s 的首字截止。
FIRST_TOKEN_BUDGET = float(os.environ.get("MIRA_FIRST_TOKEN_BUDGET", "9"))


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
                    directive["event"] = event
                    if event == "phone_call":
                        directive["action"] = "phone_call"
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


class OpenAICompatLLM(BaseLLM):
    """Streaming client for any `/v1/chat/completions` server (llama.cpp, Strata, …).

    两个实现共用这段流式解析，只在 `extra_payload()` 里放各家私有参数：
    llama.cpp 认 repeat_penalty，Strata 认 chat_template_kwargs（关思考）。
    把私有字段隔离在子类里，换模型时不用碰状态机。
    """

    name = "openai"
    label = "openai"

    def __init__(self, base_url: str, model: str, *, temperature: float = 0.7,
                 top_p: float = 0.9, max_tokens: int = 240):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.max_tokens = max_tokens

    def extra_payload(self) -> dict:
        return {}

    def payload(self, messages: list[dict]) -> dict:
        body = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "temperature": self.temperature,
            "top_p": self.top_p,
            "max_tokens": self.max_tokens,
        }
        body.update(self.extra_payload())
        return body

    def headers(self) -> dict:
        return {}

    async def stream(self, messages: list[dict], *, timeout: float = 30.0) -> AsyncIterator[str]:
        import httpx

        payload = self.payload(messages)
        try:
            # trust_env=False：本地 127.0.0.1 的调用**绝不能走系统代理**。
            # 实测这台机器上 httpx 默认读环境代理，窗口里没有 HTTP_PROXY 也会把
            # localhost 的请求转给代理；模型没起来时代理返回 502（而不是连接被拒绝），
            # 于是界面上看到的是「llama-server HTTP 502」，很难查出真因。
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=5.0),
                                         trust_env=False,
                                         headers=self.headers()) as client:
                async with client.stream("POST", f"{self.base_url}/v1/chat/completions", json=payload) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "ignore")[:300]
                        raise LLMError(f"{self.label} HTTP {resp.status_code}: {body}")
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
            raise LLMError(f"{self.label} 超时（{timeout:.0f}s 未完成）") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"{self.label} 连接失败: {exc}") from exc


class LlamaLLM(OpenAICompatLLM):
    """llama.cpp `llama-server`, 本机 4B 档（2026-10-20 起是 Qwen3.8-4B-Q4_K_M）."""

    name = "llama"
    label = "llama-server"

    def __init__(self, base_url: str, model: str = "qwen3.8-4b"):
        super().__init__(base_url, model, temperature=0.75, top_p=0.9, max_tokens=220)

    def extra_payload(self) -> dict:
        # 2026-10-20：这个档的权重从 Qwen3-4B-Instruct-2507 换成 Qwen3.8-4B 之后，
        # 它和 Strata 上的 Qwen3.8 一样**默认带 thinking**：chat template 在
        # add_generation_prompt 时吐 `<think>\n`，于是首包 = 整段思考（实测 1.1–4.5s，
        # 关掉后 0.1–0.4s），而且 reasoning 会吃掉 max_tokens=220 的预算、
        # 偶尔把首行 JSON 指令挤掉（甚至包进 ```json 代码块）。
        # 这个参数对"说台词"没有任何收益，所以一律关掉；非 thinking 的老权重
        # 收到不认识的 chat_template_kwargs 也只是忽略，换回去不用改代码。
        return {
            "chat_template_kwargs": {"enable_thinking": False},
            # 小模型很容易逐字重复上一句：轻微的 frequency/presence penalty 能压住
            # 「雨/灯/等」这类反复出现的词，又不至于把句子压得不通顺。
            "frequency_penalty": 0.15,
            "presence_penalty": 0.15,
            # llama.cpp 的 repeat_penalty 会回看上下文里已经出现过的 token（包括上一轮
            # 角色自己说的话），这是压住「跨轮逐字复读」最有效的一个参数。
            "repeat_penalty": 1.15,
        }


class StrataLLM(OpenAICompatLLM):
    """Strata engine serving Qwen3.8-Flash-Next (25B MoE) locally — OpenAI-compatible.

    为什么用它当「真实模型」档：4B 在长提示词下会丢指令（答非所问、把旁白当台词），
    这台机器上已经常驻一个 25B MoE 的 Strata 服务（127.0.0.1:1234），
    decode 约 65 tok/s、支持前缀缓存，够快也明显更会听话。
    它是**本地**服务，不需要任何 API key，符合题目「无私人密钥也能体验」的要求；
    没起时应用会按 LLM_MODE 回退到 llama/mock。
    """

    name = "strata"
    label = "Strata"

    def __init__(self, base_url: str, model: str = "qwen3.8-flash-next-iq2_xs-256k-vision"):
        super().__init__(base_url, model, temperature=0.65, top_p=0.9, max_tokens=240)

    def extra_payload(self) -> dict:
        # Qwen3.8 默认带 thinking：思考内容会把首包延迟推到 3s+，而且它对“说话”没帮助。
        # 关掉后是纯台词生成，实测首包 0.6–1.2s。
        return {"chat_template_kwargs": {"enable_thinking": False}}


class DeepSeekLLM(OpenAICompatLLM):
    """云端 DeepSeek（OpenAI 兼容），**key 只活在进程内存里**。

    为什么单独一类：base_url / 鉴权头不同，而且 key 是运行期由观测面板临时填的
    （不写 .env、不落盘、不回报给前端），只能在 headers() 里现取。
    没有 key 时由 main.llm_for("cloud") 回落到本地链，不会把整轮打挂。
    """

    name = "cloud"
    label = "DeepSeek"

    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com",
                 model: str = "deepseek-chat"):
        super().__init__(base_url, model, temperature=0.8, top_p=0.95, max_tokens=240)
        self._key = (api_key or "").strip()

    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self._key}"}

    def extra_payload(self) -> dict:
        return {}  # deepseek-chat 不接受 llama.cpp 的私有字段


class FallbackLLM(BaseLLM):
    """按顺序试：Strata(25B) → llama(4B) → Mock。

    题目要求「对模型超时、断线提供基本反馈和恢复方式」。这里的恢复方式是降级：
    只要一个 provider 在**吐出第一个字之前**失败，就换下一个，用户只会在
    /api/health 和观测面板里看到 `degraded`，对话还能继续。一旦已经出字就不换
    （中途换模型会把说过的话重说或跳字，比降级更难看）。

    顺序不是死的（实测教训）：链里第 1 个是 Strata，而演示机上它经常根本没起。
    以前每一轮都先撞它一次，连接失败再退到 llama——**每轮白付 1.8s**，
    用户听到的就是"发一句要等好几秒"。现在两件事：
      * 粘住上次成功的 provider（正常时第 1 次就命中，0 代价）；
      * 失败过的进冷却名单，且先用 250ms 的 TCP 预检拦一道，别再等 HTTP 层超时。
    """

    name = "auto"
    DEAD_COOLDOWN = 45.0   # 失败后这段时间内不再尝试，过了再给一次机会
    # TCP 预检超时。2026-10-21 从 0.25s 提到 1.2s：本机 loopback 连不上 8080 只可能是
    # 端口真没开，而 0.25s 在"事件循环正被同步 CPU 活占住"（启动预热、后处理）时会
    # 误判 —— 实测启动时误判一次，整轮就掉到 Mock，界面看着正常其实没调模型。
    PROBE_TIMEOUT = 1.2

    def __init__(self, chain: list[BaseLLM], *, sticky_primary: bool = False):
        self.chain = chain
        # 上一次真正产出 token 的是谁（strata / llama / mock）。
        # 存在的理由：降级必须是**看得见**的。以前链里静默掉到 MockLLM，界面照样
        # 回一句很像样的话，没人知道背后根本没调用模型 —— 演示时这比报错更危险。
        self.last = ""
        self._good: int | None = None
        self._dead: dict[int, float] = {}
        # sticky_primary：只把「链首真的成功了」记成粘性记忆，末尾的兜底（Mock）
        # 成功不粘。给「显式选一档模型」用的链 [llama, mock] 必须这样：否则模型
        # 崩一次 → 退到 Mock → _good 钉在 Mock 上 → 模型恢复了也永远不再用，
        # 界面从此静默降级到重启为止。链首死了靠 DEAD_COOLDOWN 冷却后重试。
        self.sticky_primary = sticky_primary

    async def _reachable(self, provider: BaseLLM) -> bool:
        """TCP 预检：只花 250ms 就能知道这个 provider 现在值不值得试。"""
        base = getattr(provider, "base_url", None)
        if not base:
            return True                      # Mock / 非 HTTP provider
        try:
            parts = urllib.parse.urlsplit(base)
            host = parts.hostname
            if not host:
                return True
            port = parts.port or (443 if parts.scheme == "https" else 80)
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), self.PROBE_TIMEOUT)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception:  # noqa: BLE001
            return False

    def _order(self) -> list[int]:
        order = list(range(len(self.chain)))
        if self._good is not None and self._good in order:
            order.remove(self._good)
            order.insert(0, self._good)
        now = time.monotonic()
        live = [i for i in order if now - self._dead.get(i, -1e9) > self.DEAD_COOLDOWN]
        return live or order   # 全在冷却里：还是按顺序试一遍，别把自己锁死

    async def stream(self, messages: list[dict], *, timeout: float = 30.0) -> AsyncIterator[str]:
        last: Exception | None = None
        now = time.monotonic()
        for i in self._order():
            provider = self.chain[i]
            if i != self._good and not await self._reachable(provider):
                self._dead[i] = now          # 端口都没开：直接跳过，不当成错误
                if os.environ.get("MIRA_LLM_DEBUG"):
                    print(f"[llm] {provider.name} TCP 预检没过（{getattr(provider, 'base_url', None)}），"
                          f"跳过", flush=True)
                continue
            if os.environ.get("MIRA_LLM_DEBUG"):
                print(f"[llm] 试 {provider.name} …", flush=True)
            started = False
            it = provider.stream(messages, timeout=timeout).__aiter__()
            try:
                while True:
                    # 每个 provider 的「第一个字」自带预算：GPU 被别的任务占满时
                    # llama.cpp 会退到 CPU（实测 4 tok/s），首字能拖到 40s+。
                    # 如果没有这一层，外层（main.stream_llm）的首字超时会直接取消
                    # 整条链，后面的 Mock 根本没机会接 —— 用户看到的是报错，而不是降级。
                    budget = timeout if started else FIRST_TOKEN_BUDGET
                    try:
                        piece = await asyncio.wait_for(it.__anext__(), budget)
                    except StopAsyncIteration:
                        break
                    if not started:
                        started = True
                        self.last = provider.name
                        # 2026-10-21：**链尾（兜底）不许钉成 _good**。以前 auto 链第一次
                        # 预热撞上瞬时超时 → 退到 Mock → _good 钉在 Mock 上，模型恢复了
                        # 也永远不再用，界面从此静默降级到重启为止（今天实测踩到：重启后
                        # 每轮都回卡包 mock 台词，看着"正常"其实根本没调模型）。
                        if (not self.sticky_primary or i == 0) and i != len(self.chain) - 1:
                            self._good = i
                        self._dead.pop(i, None)
                    yield piece
                return
            except Exception as exc:  # noqa: BLE001
                last = exc
                self._dead[i] = time.monotonic()
                if os.environ.get("MIRA_LLM_DEBUG"):
                    print(f"[llm] {provider.name} 失败（降级到下一个）: "
                          f"{type(exc).__name__}: {exc}", flush=True)
                if started:
                    raise LLMError(f"{provider.name} 中途失败: {exc}") from exc
                continue
        raise LLMError(f"所有模型都不可用（最后一个错误: {last}）")


# --------------------------------------------------------------------- TTS


class TTSProvider:
    """TTS 门面：本地神经引擎优先，edge-tts 兜底。

    引擎选择（环境变量 MIRA_TTS_ENGINE，默认 auto）：
      auto    侧车 127.0.0.1:8790 可用就用 CosyVoice2（真人音色 + 情感指令），否则 edge
      neural  只尝试神经引擎；**失败仍然回退 edge**（保证永远有声音，实时对话哑掉比难听更糟）
      edge    强制老链路（A/B 对照与紧急降级用）

    神经路径的硬预算：单句 >6s 视为失败 → 打一行 `[tts] neural fallback: ...` → 退回 edge。
    侧车在 8790，见 tools/tts/serve_neural.py；客户端 app/tts/neural.py 只走 HTTP，不 import torch。

    edge 链路本身的三层结构保持不变（情绪基调 / 句级韵律 / 停顿后处理），
    它现在是**兜底**而不是主路径：Xiaoxiao 的播音腔是模型本身，参数化到顶了。
    """

    EMOTION_STYLE = {k: dict(v) for k, v in persona.TTS_EMOTION.items()}
    SENTENCE = dict(persona.TTS_SENTENCE)
    # 单句神经合成的硬预算。默认 6s：超过就判失败回落 edge（宁可换音色也不能卡住整轮）。
    # 冷启动 / 换参考音频后的第一批烘焙会明显变慢，可用 MIRA_TTS_BUDGET 临时放宽
    # （tools/qa/prewarm_presets.py 就会把它放大到能覆盖冷启动）。
    NEURAL_BUDGET_S = float(os.environ.get("MIRA_TTS_BUDGET", "6") or 6)

    # TTS 引擎常量：main.py 的 /api/health 与调试面板只读这两项，不参与逻辑
    ENGINE_ENV = "MIRA_TTS_ENGINE"

    def __init__(self, voice: str = persona.CHARACTER["voice"], engine: str | None = None):
        self.voice = voice
        # 默认档：**卡说了算**（cards/mira_cafe/card.yaml → character.voice.engine）。
        # 优先级：构造参数 > MIRA_TTS_ENGINE 环境变量 > 卡。三档都还是 neural|edge|auto，
        # 语义不变；区别只是"默认值从哪来"。这样"换引擎/换参考音频"在卡上就有落脚点，
        # 而不是散落在代码和启动脚本里（v9 的坑：卡里只有 edge 音色名，神经路径不读它，
        # 所以用户听到的永远是 Xiaoxiao —— 换参考 wav 一声都听不到）。
        # 现在 auto = 侧车在线就神经、侧车挂了/超时才 edge，另配两处配合改动（见 main.py
        # /api/tts_stream：命中神经缓存直出 PCM；未命中先发 80ms 静音避免客户端 2s 看门狗
        # abort 后掉回 edge）。想回「延迟优先」：MIRA_TTS_ENGINE=edge，不用改卡。
        # 量测与代价见 docs/VOICE_NEURAL.md §5.4 / §5.6 / §7。
        card_engine = str(getattr(persona, "VOICE_ENGINE", "") or "auto").strip().lower()
        eng = (engine or os.environ.get(self.ENGINE_ENV) or card_engine or "auto").strip().lower()
        self.engine = eng if eng in ("auto", "neural", "edge") else "auto"
        self.last_engine = "edge"        # 上一句实际用的引擎（调试面板用）
        self.last_latency_ms = 0
        # 侧车熔断：连续 N 次失败就在冷却期内直接走 edge。
        # 为什么需要：侧车 /synth 偶尔会卡住（实测一次卡住后，后面每一句都要白等满 6s 预算
        # 才回落），一轮 3 句就是 18s，用户体验比音色差更糟。熔断让「卡住」只付一次代价。
        self._neural_fail = 0
        self._neural_skip_until = 0.0
        self.NEURAL_FAIL_LIMIT = 3
        self.NEURAL_SKIP_S = 60.0

    def _neural_available(self) -> bool:
        try:
            from .tts import neural

            return bool(neural.available())
        except Exception as exc:  # noqa: BLE001  侧车模块坏了也不能拖垮对话
            print(f"[tts] neural probe failed: {type(exc).__name__}: {exc}", flush=True)
            return False

    @staticmethod
    def _shift(value: str, delta: float) -> str:
        unit = "Hz" if value.endswith("Hz") else "%"
        n = float(value.rstrip("%Hz")) + delta
        if unit == "%":
            n = max(-40.0, min(40.0, n))
            return f"{n:+.0f}%"
        n = max(-40.0, min(40.0, n))
        return f"{n:+.0f}Hz"

    def style_for(self, text: str, emotion: str) -> dict[str, str]:
        base = dict(self.EMOTION_STYLE.get(emotion, self.EMOTION_STYLE.get("neutral", {})))
        base.setdefault("rate", "+0%")
        base.setdefault("pitch", "-4Hz")
        base.setdefault("volume", "+0%")
        t = (text or "").strip()

        if t.endswith(("？", "?")):
            base["pitch"] = self._shift(base["pitch"], self.SENTENCE.get("question_lift_hz", 3))
            base["rate"] = self._shift(base["rate"], self.SENTENCE.get("question_rate_pct", 1))
        elif t.endswith(("！", "!")):
            base["rate"] = self._shift(base["rate"], self.SENTENCE.get("exclaim_rate_pct", 2))
        if "……" in t or "..." in t:
            base["rate"] = self._shift(base["rate"], self.SENTENCE.get("ellipsis_rate_pct", -5))

        # 句长也影响语速：太短要压低速度才有重量，太长略提速才不拖
        n = len(t)
        if n <= 8:
            base["rate"] = self._shift(base["rate"], -3)
        elif n >= 28:
            base["rate"] = self._shift(base["rate"], 2)
        return base

    def cache_path(self, text: str, emotion: str) -> Path:
        st = self.style_for(text, emotion)
        key = "|".join([self.voice, st["rate"], st["pitch"], st["volume"], text])
        h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        return TTS_CACHE / f"{h}.mp3"

    async def synth(self, text: str, emotion: str = "neutral", timeout: float = 12.0,
                    position: str = "follow") -> tuple[Path, bool]:
        """Return (path, used_cache)。

        先试本地神经引擎（真人参考音色 + 情感指令，见 docs/VOICE_NEURAL.md）；
        任何异常/超时/未就绪都退回现有 edge 后处理管线——**签名与返回值不变**，
        app/main.py 只依赖 (Path, bool)。

        edge 管线：裁掉 Edge 每句自带的静音、按标点重插停顿/气口、加一层小房间混响
        与高频柔化；管线失败再退到「一句一合成」的裸 edge 路径，保证永远有声音。
        """
        if (self.engine != "edge" and (text or "").strip()
                and time.time() >= self._neural_skip_until):
            try:
                from .tts import neural

                if neural.available():
                    budget = min(self.NEURAL_BUDGET_S, max(2.0, float(timeout)))
                    t0 = time.time()
                    path, cached = await asyncio.wait_for(
                        neural.synth_cached(text, emotion, position=position,
                                            timeout=budget),
                        timeout=budget,
                    )
                    self.last_engine = "neural"
                    self.last_latency_ms = int((time.time() - t0) * 1000)
                    self._neural_fail = 0
                    return path, cached
            except Exception as exc:  # noqa: BLE001  任何失败都必须有声
                self._neural_fail += 1
                if self._neural_fail >= self.NEURAL_FAIL_LIMIT:
                    # 侧车大概率已经卡死：冷却期内不再尝试，直接走 edge，
                    # 否则每一句都要白等满预算（演示里就是"她卡住了"）。
                    self._neural_skip_until = time.time() + self.NEURAL_SKIP_S
                    self._neural_fail = 0
                    print(f"[tts] neural 熔断 {int(self.NEURAL_SKIP_S)}s（侧车可能卡死，"
                          f"这期间走 edge）：{type(exc).__name__}: {exc}", flush=True)
                else:
                    print(f"[tts] neural fallback: {type(exc).__name__}: {exc}", flush=True)

        self.last_engine = "edge"
        try:
            from .tts import proposal as tts_pipeline

            t0 = time.time()
            out = await tts_pipeline.synth(text, emotion, timeout=timeout,
                                           position=position, voice=self.voice)
            self.last_latency_ms = int((time.time() - t0) * 1000)
            return out
        except Exception as exc:  # noqa: BLE001
            print(f"[tts] pipeline fallback ({type(exc).__name__}: {exc})", flush=True)

        path = self.cache_path(text, emotion)
        if path.exists() and path.stat().st_size > 800:
            return path, True
        import edge_tts

        st = self.style_for(text, emotion)
        tmp = path.with_suffix(".part.mp3")
        try:
            t0 = time.time()
            comm = edge_tts.Communicate(text, self.voice, rate=st["rate"], pitch=st["pitch"],
                                        volume=st["volume"])
            await asyncio.wait_for(comm.save(str(tmp)), timeout=timeout)
            if not tmp.exists() or tmp.stat().st_size < 500:
                raise RuntimeError("TTS 输出为空")
            os.replace(tmp, path)
            self.last_latency_ms = int((time.time() - t0) * 1000)
            return path, False
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
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
