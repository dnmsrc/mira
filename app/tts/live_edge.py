"""edge-tts 长连（连接复用）+ 增量 mp3→PCM 解码。

背景（P4：第一句台词 2.4–4.3s，见 docs/LATENCY_LIVE_2026-10-21.md）：
edge_tts 7.3.1 的公开实现每次合成都 `async with ClientSession() as session,
session.ws_connect(...)`，也就是**每句都重新 TCP + TLS + WSS upgrade**。
实测（tools/tts/probe_edge_timing.py，本机/同网）：
    握手 TCP+TLS+upgrade ≈ 850ms（三轮 838–855，很稳定）
    握手后「发 SSML → 首个音频块」= 223–270ms
    同一条 WSS 连发 5 句全部正常（每句独立 turn.end，首块 222–389ms）
所以把这 850ms 摊掉，首块就从 ≈1090ms 降到 ≈250ms。

这个模块干三件事：
  1. 维护一条可复用的 WSS（懒连接 / 按需预热 / 空闲 ping 保活 / 断线重连）；
  2. 把一条连接上的合成**串行化**（一条 WSS 不能交错两段语音）；
  3. 把 edge 吐的 mp3 块**增量解码**成 24kHz 单声道 s16 PCM，好让浏览器边收边播
     （沿用现有 /api/tts_stream 的 audio/L16 契约，客户端播放器不用改）。

开关：MIRA_TTS_LIVE=1/0（默认 1，见 live_enabled()）。关掉就完全不碰这个模块，
走原来的整句 mp3 管线——这条回退路径必须实测过（负控）。

失败处理约定：任何异常都往外抛，由调用方（app/main.py:/api/tts_stream）结束这次流，
客户端发现字节数不够会自己退到 /api/tts_file 整句 mp3。**不做静默重试**：
重试会让首块变成 2 倍延迟，不如直接回退。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from typing import AsyncIterator

import numpy as np

# 与线上完全一致的音色/情绪参数来源（providers.style_for）。这里只做兜底默认值。
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"

# 空闲多久以内的连接可以直接用（超过就先重连，避免拿到快被服务端关掉的连接）。
# 实测（tools/tts/probe_edge_keepalive.py）：15s ping 保活下，空闲 40s 仍可复用，
# 累计 ~70s 时服务端已断开。取 25s：远离危险区，又足够覆盖一轮对话的正常间隔。
IDLE_REUSE_S = float(os.environ.get("MIRA_TTS_LIVE_IDLE", "25"))
PING_EVERY_S = float(os.environ.get("MIRA_TTS_LIVE_PING", "12"))


def live_mode() -> str:
    """开关档位：'off' | 'first' | 'all'（默认 all）。

    off    完全不碰长连，走原来的整句 mp3 管线（**回退路径，必须实测**）
    first  只有每轮第一句走长连流式，第 2 句起仍走整句 mp3（保守档）
    all    每一句都走长连流式（延迟最低，句间靠时间线预留无缝衔接）

    为什么默认 all：整句 mp3 路径每句都要一次 850ms 握手 + 1.5s 合成 + DSP，
    一句话的回复里第 2、3 句会各多等 1.5s；长连路径每句 ≈330ms 且能提前排在时间线上。
    """
    m = (os.environ.get("MIRA_TTS_LIVE", "1") or "1").strip().lower()
    if m in ("0", "off", "false", "no", "none"):
        return "off"
    if m in ("first", "first-only", "one"):
        return "first"
    return "all"


def live_enabled() -> bool:
    """总开关。默认开；只有显式关掉才走老路径。"""
    return live_mode() != "off"


class Mp3StreamDecoder:
    """增量 mp3 → s16le 单声道 PCM（PyAV 自带 ffmpeg，不需要额外依赖）。

    为什么不用 resampler：mp3 解码出来本来就是 24kHz 单声道（edge 的输出格式），
    手动 float→int16 只多一次乘法，且不会有重采样器内部的缓冲延迟。
    """

    def __init__(self, sample_rate: int = 24000):
        import av

        self.sample_rate = sample_rate
        self._codec = av.CodecContext.create("mp3", "r")
        self.frames = 0
        self.samples = 0

    def feed(self, data: bytes) -> bytes:
        """喂一块 mp3 字节，返回这块能解出来的 PCM（可能为空：帧还没收全）。"""
        out: list[bytes] = []
        try:
            packets = self._codec.parse(data)
        except Exception:  # noqa: BLE001  半帧/脏数据：跳过，等下一块
            return b""
        for pkt in packets:
            try:
                frames = self._codec.decode(pkt)
            except Exception:  # noqa: BLE001
                continue
            for fr in frames:
                arr = fr.to_ndarray()
                arr = np.asarray(arr, dtype=np.float32)
                if arr.ndim > 1:
                    arr = arr.reshape(-1) if arr.shape[0] == 1 else arr[0]
                pcm = np.clip(arr, -1.0, 1.0)
                pcm = (pcm * 32767.0).astype("<i2").tobytes()
                if pcm:
                    out.append(pcm)
                    self.frames += 1
                    self.samples += len(pcm) // 2
        return b"".join(out)

    def flush(self) -> bytes:
        try:
            frames = self._codec.decode(None)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            return b""
        out = []
        for fr in frames or []:
            arr = np.asarray(fr.to_ndarray(), dtype=np.float32)
            if arr.ndim > 1:
                arr = arr.reshape(-1) if arr.shape[0] == 1 else arr[0]
            out.append((np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())
        return b"".join(out)


class LiveEdge:
    """一条（可重连的）edge 合成长连。"""

    def __init__(self):
        self._session = None
        self._ws = None
        self._cfg_sent = False
        self._last_use = 0.0
        self._syn_lock = asyncio.Lock()   # 一条 WSS 上不能交错两段语音
        self._conn_lock = asyncio.Lock()  # 建连/重连只允许一个进行
        self._bg: asyncio.Task | None = None
        self._warm_task: asyncio.Task | None = None
        # A/B 用：进程内覆盖档位（/api/debug/inject 设），None = 跟着环境变量走
        self.override_mode: str | None = None
        # 连接是否「脏」：上一次合成被中途放弃（客户端打断 / 客户端断开），这条 WSS 上
        # 还挂着一个没读完的 turn。**不能当成可复用**——下一句会串到上一句的残音。
        # 为什么只打标记不立刻断：生成器被 aclose() 时拿到的是 GeneratorExit，
        # 那个上下文里 await 会被 runtime 拒绝，所以真正的关闭放到下一次 ensure()。
        self._poisoned = False
        self.stats = {
            "connects": 0, "reuses": 0, "synths": 0, "failures": 0,
            "last_connect_ms": 0, "last_first_chunk_ms": 0,
            "aborted": 0, "idle_reconnects": 0,
        }

    # ---------------------------------------------------------------- 连接
    def alive(self) -> bool:
        ws = self._ws
        return ws is not None and not getattr(ws, "closed", True)

    async def _drop(self, why: str = "") -> None:
        """丢弃当前连接。为什么是丢弃而不是复用：被取消/半途断掉的合成会让这条
        连接上留下未读完的 turn，下一句要么串音要么解析错位，重连一次更干净。"""
        ws, self._ws = self._ws, None
        sess, self._session = self._session, None
        self._cfg_sent = False
        try:
            if ws is not None:
                await asyncio.wait_for(ws.close(), timeout=1.0)
        except Exception:  # noqa: BLE001
            pass
        try:
            if sess is not None:
                await sess.close()
        except Exception:  # noqa: BLE001
            pass
        if why:
            print(f"[tts-live] 连接丢弃：{why}", flush=True)

    async def _connect(self) -> None:
        """建一条新的 WSS。整段 ≈850ms（DNS+TCP+TLS+upgrade），这是要藏进思考时间的那段。"""
        import aiohttp
        from edge_tts.communicate import _SSL_CTX
        from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
        from edge_tts.drm import DRM

        await self._drop()
        t0 = time.time()
        self._session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=1, force_close=False, ssl=_SSL_CTX),
            trust_env=True,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=8),
        )
        url = (f"{WSS_URL}&ConnectionId={uuid.uuid4().hex}"
               f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
               f"&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}")
        self._ws = await asyncio.wait_for(
            self._session.ws_connect(
                url, compress=15, headers=DRM.headers_with_muid(WSS_HEADERS), ssl=_SSL_CTX),
            timeout=8.0,
        )
        self._cfg_sent = False
        self._last_use = time.time()
        self.stats["connects"] += 1
        self.stats["last_connect_ms"] = int((time.time() - t0) * 1000)
        print(f"[tts-live] WSS 已连接 {self.stats['last_connect_ms']}ms "
              f"(共 {self.stats['connects']} 次)", flush=True)

    async def ensure(self) -> None:
        """确保有一条可用连接。空闲超时或已断就重连。"""
        if not live_enabled():
            raise RuntimeError("MIRA_TTS_LIVE=0")
        async with self._conn_lock:
            if self._poisoned:
                self._poisoned = False
                await self._drop("上一条 turn 未读完（被打断），换新连接")
            fresh = self.alive() and (time.time() - self._last_use) <= IDLE_REUSE_S
            if fresh:
                self.stats["reuses"] += 1
                return
            if self.alive():
                self.stats["idle_reconnects"] += 1
            await self._connect()

    def prewarm(self) -> None:
        """按需预热：turn 一开始就调用，把 850ms 握手藏进 LLM 首字的等待里。

        为什么不等真要合成时再连：LLM 首字 p50 ≈1.0s（> 850ms），这段时间本来是空的。
        为什么还要常驻保活：turn 与 turn 之间可能几十秒，保活能省掉下一次预热。
        两条都用：保活负责"大概率已经热"，预热负责"冷启动也感觉不到"。

        注意这是**同步**方法（内部 create_task）：调用点都在请求处理的热路径上，
        不能 await（会把这 850ms 加在本轮延迟上），也绝不能忘了 await 变成
        "coroutine was never awaited" 的哑弹。
        """
        if not live_enabled():
            return
        if self.alive() and (time.time() - self._last_use) <= IDLE_REUSE_S:
            return
        if self._warm_task is not None and not self._warm_task.done():
            return
        self._warm_task = asyncio.create_task(self._warm())

    async def _warm(self) -> None:
        try:
            await self.ensure()
        except Exception as exc:  # noqa: BLE001  预热失败不算错，合成时还会再试一次
            print(f"[tts-live] 预热失败（不影响主链路）：{type(exc).__name__}: {exc}",
                  flush=True)

    async def keepalive_loop(self) -> None:
        """后台保活：空闲时 ping 一下，死了就下次用之前重连。

        只在自己空闲时 ping：aiohttp 的 ws 写帧在 await 点上不是原子的，
        合成进行中插一个 ping 帧有把 SSML 帧写坏的风险。
        """
        while True:
            try:
                await asyncio.sleep(PING_EVERY_S)
                if not live_enabled():
                    continue
                if self._syn_lock.locked():
                    continue
                if not self.alive():
                    continue
                if time.time() - self._last_use < PING_EVERY_S / 2:
                    continue
                await asyncio.wait_for(self._ws.ping(), timeout=3.0)
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001  连接被服务端关了：标死，下次 ensure 会重连
                print(f"[tts-live] 保活失败（下次使用时重连）：{type(exc).__name__}: {exc}",
                      flush=True)
                try:
                    await self._drop("保活失败")
                except Exception:  # noqa: BLE001
                    pass

    def start(self) -> None:
        if self._bg is None or self._bg.done():
            self._bg = asyncio.create_task(self.keepalive_loop())

    async def close(self) -> None:
        if self._bg is not None:
            self._bg.cancel()
            self._bg = None
        await self._drop("shutdown")

    # ---------------------------------------------------------------- 合成
    async def pcm_stream(self, text: str, *, voice: str = DEFAULT_VOICE,
                         rate: str = "+0%", pitch: str = "-4Hz", volume: str = "+0%",
                         first_chunk_timeout: float = 4.0,
                         total_timeout: float = 20.0) -> AsyncIterator[bytes]:
        """一次合成：吐 24kHz 单声道 s16le PCM 块。

        串行化：一条 WSS 上的多段语音会互相穿插（协议没有 turn id 隔离），
        所以同一时刻只允许一段在跑。语音本来也是按句顺序播的，串行不吃亏。
        """
        import aiohttp
        from edge_tts.communicate import date_to_string, mkssml, ssml_headers_plus_data
        from edge_tts.data_classes import TTSConfig
        from edge_tts.exceptions import NoAudioReceived

        if not (text or "").strip():
            return
        async with self._syn_lock:
            await self.ensure()
            ws = self._ws
            assert ws is not None
            t0 = time.time()
            t_first = 0.0
            dec = Mp3StreamDecoder(24000)
            try:
                if not self._cfg_sent:
                    await ws.send_str(
                        "X-Timestamp:" + date_to_string() + "\r\n"
                        "Content-Type:application/json; charset=utf-8\r\n"
                        "Path:speech.config\r\n\r\n"
                        '{"context":{"synthesis":{"audio":{"metadataoptions":{'
                        '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},'
                        '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
                    )
                    self._cfg_sent = True
                cfg = TTSConfig(voice=voice, rate=rate, pitch=pitch, volume=volume,
                                boundary="WordBoundary")
                await ws.send_str(ssml_headers_plus_data(
                    uuid.uuid4().hex, date_to_string(), mkssml(cfg, text)))
                got_audio = False
                deadline = t0 + total_timeout
                while True:
                    timeout = (first_chunk_timeout if not got_audio
                               else max(1.0, deadline - time.time()))
                    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        data = msg.data
                        if len(data) < 2:
                            continue
                        if data[:2] == b"\x00\x00":   # turn 结束标记（无 content-type 的空帧）
                            continue
                        hlen = int.from_bytes(data[:2], "big")
                        if hlen > len(data):
                            continue
                        body = data[hlen + 2:]
                        if not body:
                            continue
                        pcm = dec.feed(body)
                        if pcm:
                            got_audio = True
                            if not t_first:
                                t_first = time.time()
                                self.stats["last_first_chunk_ms"] = int(
                                    (t_first - t0) * 1000)
                            yield pcm
                    elif msg.type == aiohttp.WSMsgType.TEXT:
                        enc = msg.data.encode()
                        i = enc.find(b"\r\n\r\n")
                        path = b""
                        for line in enc[:i].split(b"\r\n"):
                            if line.startswith(b"Path:"):
                                path = line[5:].strip()
                                break
                        if path == b"turn.end":
                            tail = dec.flush()
                            if tail:
                                yield tail
                            self.stats["synths"] += 1
                            self._last_use = time.time()
                            break
                        # turn.start / response / audio.metadata 都不需要
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE,
                                      aiohttp.WSMsgType.CLOSED):
                        raise RuntimeError(f"WSS 断开 type={msg.type}")
                if not got_audio:
                    raise NoAudioReceived(f"没有收到音频：{text[:16]}")
            except (asyncio.CancelledError, GeneratorExit):
                # 客户端断开 / 打断：立刻停止合成，并把这条连接标脏。
                # **这里不能 await**：aclose() 走的是 GeneratorExit 上下文，await 会被
                # 解释器拒绝（"async generator ignored GeneratorExit"）。真正的关闭在
                # 下一次 ensure() 里做——见 _poisoned。
                self._poisoned = True
                self.stats["aborted"] += 1
                self.stats["failures"] += 1
                raise
            except Exception:
                self.stats["failures"] += 1
                await self._drop("合成失败")
                raise


# 进程内单例（app/main.py 只用这个实例）
LIVE = LiveEdge()
