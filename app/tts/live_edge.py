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
#
# 2026-10-21 14:5x 用 tools/tts/probe_idle_lifetime.py 重测了这条连接的寿命：
#   不 ping：连上后 121s 内没有收到 close 帧（但 alive() 只反映"收到过 close"，
#            不能证明 transport 还活着）；
#   每 12s ping：16s / 26s 两次写成功，**36s 写出去就 ClientConnectionResetError:
#            Cannot write to closing transport** —— edge 现在大约 30–40s 就关空闲长连，
#            而且不发 close 帧。
# 所以"退役一条旧连接"的判据只能是「年龄」+「后台已经换过新的」，
# 真正的保障在 maintain_loop()：空闲时主动换新，让下一条被用到的连接永远年轻。
IDLE_REUSE_S = float(os.environ.get("MIRA_TTS_LIVE_IDLE", "25"))
PING_EVERY_S = float(os.environ.get("MIRA_TTS_LIVE_PING", "12"))
# 后台换新的节奏：离上一次"用/聊"越久越省（别长期骚扰 edge）
REFRESH_S = float(os.environ.get("MIRA_TTS_LIVE_REFRESH", "15"))
REFRESH_IDLE_S = float(os.environ.get("MIRA_TTS_LIVE_REFRESH_IDLE", "45"))
REFRESH_ACTIVE_S = float(os.environ.get("MIRA_TTS_LIVE_ACTIVE", "180"))
REFRESH_STOP_S = float(os.environ.get("MIRA_TTS_LIVE_REFRESH_STOP", "900"))


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


class WSSDown(RuntimeError):
    """合成长连断了（服务端关连接 / 传输层被 reset）。

    单独一个类型，是为了让上层能区分「这条连接坏了，换一条重来」和真正的代码错：
    只有前者允许在**还没吐第一个字节**时重试（见 pcm_stream）。
    """


def refresh_interval(quiet_s: float) -> float:
    """按「多久没人用」决定后台换新的节奏（0 = 停止后台换新）。

    抽成纯函数是为了能单测（tools/qa/test_live_refresh.py）：这是唯一一处
    「为了延迟去换连接」的策略，写死在一个函数里才不会和生产行为漂移。
    """
    if quiet_s > REFRESH_STOP_S:
        return 0.0
    return REFRESH_S if quiet_s <= REFRESH_ACTIVE_S else REFRESH_IDLE_S


def retryable_pre_audio(exc: BaseException) -> bool:
    """吐第一个字节之前发生的失败 → 可以安全重试（不会让同一句说两遍）。

    为什么要在「首块前」这一层也重试（原来只重试空音频）：连接判活靠 ping，
    而 edge 会**不发 close 帧**就关掉空闲长连（见 maintain_loop 的实测），
    所以理论上可能拿到一条刚被关掉、ping 还没探到的连接。这种连接写第一帧就会
    reset——如果不重试，这一句就要退到 /api/tts_file 整句 mp3（≈1.5s+），
    用户听到的是第一句明显慢一截；重试只是多付一次 850ms 握手，且只发生在这一种情况。
    """
    if isinstance(exc, (WSSDown, asyncio.TimeoutError, ConnectionError, OSError)):
        return True
    # 真正的类优先（aiohttp / edge_tts 都是重 import，只在失败路径上按需加载）
    try:
        import aiohttp

        if isinstance(exc, aiohttp.ClientError):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        from edge_tts.exceptions import NoAudioReceived

        if isinstance(exc, NoAudioReceived):
            return True
    except Exception:  # noqa: BLE001
        pass
    # 兜底：按类名判（真实类在上面已覆盖；这里是给还没 import 成功的场景和测试用的）
    name = type(exc).__name__
    return name in (
        "NoAudioReceived",              # edge 云端成片返回空音频（已实测过的老毛病）
        "ClientError", "ClientConnectionError", "ClientConnectionResetError",
        "ClientOSError", "ServerDisconnectedError", "ServerTimeoutError",
        "ClientPayloadError", "InvalidStateError",
    )


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

    @staticmethod
    def _to_float(fr) -> np.ndarray:
        """一帧 → ±1 的 float32，**先归一化再交给下游**。

        为什么必须有这一步（2026-10-21 炸麦根因，见 docs/AUDIO_CLIP_2026-10-21.md）：
        PyAV 解 mp3 出来的帧格式是 `s16p`，`to_ndarray()` 返回 **dtype=int16、量纲 ±32768**
        （实测同一句 -4.78dBFS 的音频 raw 范围 ±18909）。旧代码只做
        `np.asarray(arr, dtype=np.float32)`——只换 dtype 不换量纲——于是下游
        `np.clip(arr, -1.0, 1.0)` 实际是在 **1 LSB** 上截，等价于 `sign(x)`：
        整段语音被压成 1-bit 方波（实测 70% 样本钉在满刻度、最长平顶 11.7ms、
        RMS 抬高 18dB），听感就是「炸麦」。

        这里只做量纲归一化：**不做任何限幅/压缩**。真正的削波（源端 overshoot）
        留给下游 `np.clip` 兜底，量纲正确后再 clip 才是它本来的语义。
        """
        arr = fr.to_ndarray()
        if arr.dtype == np.int16:
            arr = arr.astype(np.float32) / 32768.0
        elif arr.dtype == np.int32:
            arr = arr.astype(np.float32) / 2147483648.0
        elif arr.dtype == np.uint8:                     # 8bit PCM 是偏移码
            arr = (arr.astype(np.float32) - 128.0) / 128.0
        else:
            arr = arr.astype(np.float32)
        if arr.ndim > 1:
            arr = arr.reshape(-1) if arr.shape[0] == 1 else arr[0]
        return arr

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
                arr = self._to_float(fr)
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
            arr = self._to_float(fr)
            out.append((np.clip(arr, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())
        return b"".join(out)


class LiveEdge:
    """一条（可重连的）edge 合成长连。"""

    def __init__(self):
        self._session = None
        self._ws = None
        self._cfg_sent = False
        self._last_use = 0.0
        self._last_activity = 0.0   # 最近一次「有人在用」（turn/stt/warm/合成）
        self._started_at = time.time()
        self._last_ping = 0.0
        # 正在用这条连接做合成的个数（合成期间后台换新**不能**把旧连接关掉）
        self._inflight = 0
        self._retired: list[tuple] = []   # 被换下来、等空闲时再关的 (ws, session)
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
            # 2026-10-21 14:5x：第一句又慢了，根因是「合成前还要等一次握手」。
            # 三个计数器把这层直接暴露出来（health.tts_live 可读，不用翻日志）：
            #   prewarms       本轮入口真的发起了预热（应当 ≈ 轮次数）
            #   prewarm_skips  预热被跳过（连接已经是热的）
            #   last_ensure_ms 最近一次合成在 ensure() 里等了多少（>60ms = 冷）
            #   refreshes      后台主动换新的次数（这些握手**不在**用户的关键路径上）
            "prewarms": 0, "prewarm_skips": 0, "last_ensure_ms": 0,
            "refreshes": 0, "pings": 0, "ping_fails": 0,
        }

    # ---------------------------------------------------------------- 连接
    def note_activity(self) -> None:
        """有人开始用这条连接了（turn 入口 / STT / 打字或说话时的 /api/warm）。

        只用来决定后台换新的频率：正在对话时 15s 一换，久无人用就降频、最后停掉。
        停掉之后不会"以为它还热"——下一次 turn 入口的 prewarm 会重新连。
        """
        self._last_activity = time.time()

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

    async def _connect(self, keep_old: bool = False) -> None:
        """建一条新的 WSS。整段 ≈850ms（DNS+TCP+TLS+upgrade），这是要藏进思考时间的那段。

        keep_old=True：先建新的、成功了再把旧的关掉（后台换新用）。
        为什么不能像普通重连那样先 _drop 再连：那样会有一个 800–1700ms 的空窗，
        恰好在这段时间有人说话，第一句就要等这条新连接——正是我们要消掉的延迟。
        两条 WSS 同时存在几百毫秒没有副作用（协议上每条连接各自独立）。
        """
        import aiohttp
        from edge_tts.communicate import _SSL_CTX
        from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
        from edge_tts.drm import DRM

        old_ws, old_sess = self._ws, self._session
        if not keep_old:
            await self._drop()
        t0 = time.time()
        session = aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=1, force_close=False, ssl=_SSL_CTX),
            trust_env=True,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=8),
        )
        try:
            url = (f"{WSS_URL}&ConnectionId={uuid.uuid4().hex}"
                   f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
                   f"&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}")
            ws = await asyncio.wait_for(
                session.ws_connect(
                    url, compress=15, headers=DRM.headers_with_muid(WSS_HEADERS), ssl=_SSL_CTX),
                timeout=8.0,
            )
        except Exception:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass
            raise                      # 旧连接还原封不动（keep_old 时它还在用）
        self._session, self._ws = session, ws
        self._cfg_sent = False
        self._last_use = time.time()
        self.stats["connects"] += 1
        self.stats["last_connect_ms"] = int((time.time() - t0) * 1000)
        if keep_old and old_ws is not None:
            # 旧连接上还挂着一个正在跑的合成：**不能现在关**（会把那一句掐断），
            # 先记下来，等空闲时再关（见 _sweep_retired）。
            if self._inflight > 0 or self._syn_lock.locked():
                self._retired.append((old_ws, old_sess))
            else:
                await self._close_pair(old_ws, old_sess)
        print(f"[tts-live] WSS 已连接 {self.stats['last_connect_ms']}ms "
              f"(共 {self.stats['connects']} 次)", flush=True)

    async def _close_pair(self, ws, sess) -> None:
        try:
            if ws is not None:
                await asyncio.wait_for(ws.close(), timeout=0.8)
        except Exception:  # noqa: BLE001
            pass
        try:
            if sess is not None:
                await sess.close()
        except Exception:  # noqa: BLE001
            pass

    async def _sweep_retired(self) -> None:
        """把换下来、暂时没关的旧连接关掉（只在没有合成进行时调用）。"""
        if self._inflight > 0 or self._syn_lock.locked() or not self._retired:
            return
        pairs, self._retired = self._retired, []
        for ws, sess in pairs:
            await self._close_pair(ws, sess)

    async def ensure(self, force: bool = False) -> None:
        """确保有一条可用连接。空闲超时或已断就重连。

        force=True 是后台维护线程用的：不管年龄，直接换一条新的（见 maintain_loop），
        这样"下一条被用到的连接"永远是年轻的。

        非 force 的快路径**故意不抢 _conn_lock**：后台换新正在握手时，如果合成也去排队，
        第一句就会白等一次 850ms（2026-10-21 15:0x 实测到过：正好撞上换新窗口，
        第一块从 210ms 变成 1147ms）。现有连接只要还是活的、不太旧，就直接用它——
        换新完成后会看到 _inflight>0，把旧连接记进 _retired 而不是关掉（见 _connect）。
        """
        if not live_enabled():
            raise RuntimeError("MIRA_TTS_LIVE=0")
        if (not force and not self._poisoned and self.alive()
                and (time.time() - self._last_use) <= IDLE_REUSE_S):
            self.stats["reuses"] += 1
            return
        async with self._conn_lock:
            if self._poisoned:
                self._poisoned = False
                await self._drop("上一条 turn 未读完（被打断），换新连接")
            had = self.alive()
            fresh = ((not force) and had
                     and (time.time() - self._last_use) <= IDLE_REUSE_S)
            if fresh:
                self.stats["reuses"] += 1
                return
            if force:
                if had:
                    self.stats["refreshes"] += 1
                await self._connect(keep_old=had)
                return
            if had:
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
        self.note_activity()
        if self.alive() and (time.time() - self._last_use) <= IDLE_REUSE_S:
            self.stats["prewarm_skips"] += 1
            return
        if self._warm_task is not None and not self._warm_task.done():
            self.stats["prewarm_skips"] += 1
            return
        self.stats["prewarms"] += 1
        self._warm_task = asyncio.create_task(self._warm())

    async def _warm(self) -> None:
        try:
            await self.ensure()
        except Exception as exc:  # noqa: BLE001  预热失败不算错，合成时还会再试一次
            print(f"[tts-live] 预热失败（不影响主链路）：{type(exc).__name__}: {exc}",
                  flush=True)

    async def maintain_loop(self) -> None:
        """后台维护：让「下一条被用到的连接」几乎总是热的。

        为什么不能再靠 ping 保活（2026-10-21 14:5x 实测，tools/tts/probe_idle_lifetime.py）：
          - 不 ping：121s 内没收到 close 帧（但 alive() 只反映"收到过 close"，
            不代表 transport 还活着——见下一条）；
          - 每 12s ping：16s / 26s 写入正常，**36s 就 ClientConnectionResetError:
            Cannot write to closing transport**，而 alive() 这时仍然是 True。
        也就是说 edge 现在大约 30–40s 就悄悄关掉空闲长连、还不发 close 帧：
        ping 只能**发现**死亡，不能**防止**死亡。而死亡一旦落在空闲期，
        下一轮第一句就要重新付 850ms 握手；预热带只能遮住其中一部分
        （LLM 首句 500–1200ms vs 握手 800–1700ms），漏出去的就是用户听到的语音延迟。

        所以改成**后台主动换新**（握手离开关键路径）：
          - 最近 3 分钟内有互动（说了话/打了字）：连接超过 REFRESH_S(15s) 就换一条；
          - 3–15 分钟没互动：降到 REFRESH_IDLE_S(45s) 一换；
          - 超过 REFRESH_STOP_S(15min) 没人用：停止后台换新（别长期骚扰 edge），
            下一次 turn 入口的 prewarm 会重新连。
        换新期间握一次手的时间用户看不见；合成进行中绝不换（_syn_lock 锁着就跳过）。
        """
        fails = 0
        while True:
            try:
                await asyncio.sleep(1.0)
                if not live_enabled():
                    continue
                if self._syn_lock.locked():      # 正在合成：绝不在这时候动连接
                    continue
                await self._sweep_retired()      # 换下来但还没关的旧连接，空闲时收掉
                now = time.time()
                # 只用"人什么时候最后动过"来定节奏。不能拿 _last_use：它会被后台换新
                # 自己刷新，于是换新→安静 0s→再换新，永远高频，等于永不降频。
                last_touch = max(self._last_activity, self._started_at)
                quiet = now - last_touch
                interval = refresh_interval(quiet)
                if interval <= 0:
                    continue
                if self.alive() and (now - self._last_use) < interval:
                    # 连接还年轻：只做一次存活探测，别等到合成时才发现它已经半死
                    if now - self._last_ping >= PING_EVERY_S:
                        self._last_ping = now
                        try:
                            await asyncio.wait_for(self._ws.ping(), timeout=3.0)
                            self.stats["pings"] += 1
                        except Exception as exc:  # noqa: BLE001
                            self.stats["ping_fails"] += 1
                            print(f"[tts-live] 存活探测失败（后台换新）："
                                  f"{type(exc).__name__}: {exc}", flush=True)
                            await self._drop("存活探测失败")
                    continue
                # 死了 / 太旧：后台换一条新的（用户看不见这一次握手）
                try:
                    await self.ensure(force=True)
                    fails = 0
                except Exception as exc:  # noqa: BLE001
                    fails += 1
                    print(f"[tts-live] 后台换新失败（{fails}）："
                          f"{type(exc).__name__}: {exc}", flush=True)
                    await asyncio.sleep(min(30.0, 4.0 * fails))
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001  维护线程绝不能死
                print(f"[tts-live] 维护循环异常：{type(exc).__name__}: {exc}", flush=True)
                await asyncio.sleep(2.0)

    def start(self) -> None:
        if self._bg is None or self._bg.done():
            self._bg = asyncio.create_task(self.maintain_loop())

    async def close(self) -> None:
        if self._bg is not None:
            self._bg.cancel()
            self._bg = None
        await self._drop("shutdown")
        pairs, self._retired = self._retired, []
        for ws, sess in pairs:
            await self._close_pair(ws, sess)

    # ---------------------------------------------------------------- 合成
    async def pcm_stream(self, text: str, *, voice: str = DEFAULT_VOICE,
                         rate: str = "+0%", pitch: str = "-4Hz", volume: str = "+0%",
                         first_chunk_timeout: float = 4.0,
                         total_timeout: float = 20.0) -> AsyncIterator[bytes]:
        """对外入口：包一层「合成中」计数 + 首块前失败重试。

        计数（_inflight）是给后台换新用的：合成正在进行时，被换下来的旧连接
        不能立刻关（会把这一句掐断），记进 _retired 等空闲再关。见 _connect/maintain_loop。
        """
        if not (text or "").strip():
            return
        self._inflight += 1
        try:
            async for pcm in self._pcm_stream_retry(
                text, voice=voice, rate=rate, pitch=pitch, volume=volume,
                first_chunk_timeout=first_chunk_timeout, total_timeout=total_timeout,
            ):
                yield pcm
        finally:
            self._inflight -= 1

    async def _pcm_stream_retry(self, text: str, *, voice: str = DEFAULT_VOICE,
                                rate: str = "+0%", pitch: str = "-4Hz", volume: str = "+0%",
                                first_chunk_timeout: float = 4.0,
                                total_timeout: float = 20.0) -> AsyncIterator[bytes]:
        """一次合成（含首块前失败的重试）。**没吐字节就失败时重连重试**。

        为什么敢重试（2026-10-21 13:4x 实测）：edge 云端现在会成片地返回空音频——
        同一句、同样参数，串行三次全 `NoAudioReceived`，紧接着并发三次全成功。
        这种失败发生在**吐第一个字节之前**，重试不会造成「同一句说两遍」；
        反过来，不重试就会在这句上丢音（客户端 2s 看门狗一到就把流 abort，
        半句保留 → 这句短一截）。只重试一次：max_latency 比稳定性更值钱的是首句延迟。

        2026-10-21 14:5x 扩展：连接层面在首块前的失败（WSS 被服务端关掉 / transport reset）
        也走同一条重试。见 retryable_pre_audio 的注释。
        """
        attempts = max(1, int(os.environ.get("MIRA_TTS_LIVE_ATTEMPTS", "2") or 2))
        for attempt in range(1, attempts + 1):
            got_any = False
            try:
                async for pcm in self._stream_once(
                    text, voice=voice, rate=rate, pitch=pitch, volume=volume,
                    first_chunk_timeout=first_chunk_timeout,
                    total_timeout=total_timeout,
                ):
                    got_any = True
                    yield pcm
                return
            except Exception as exc:  # noqa: BLE001
                if got_any or attempt >= attempts or not retryable_pre_audio(exc):
                    raise
                self.stats["retries"] = self.stats.get("retries", 0) + 1
                print(f"[tts-live] 首块前失败（{type(exc).__name__}: {exc}）→ 重连重试 "
                      f"{attempt}/{attempts - 1}：{text[:14]!r}", flush=True)

    async def _stream_once(self, text: str, *, voice: str = DEFAULT_VOICE,
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
            t_ens = time.time()
            await self.ensure()
            ens_ms = int((time.time() - t_ens) * 1000)
            self.stats["last_ensure_ms"] = ens_ms
            if ens_ms > 60:
                print(f"[tts-live] 合成前等连接 {ens_ms}ms"
                      f"（预热没赶上/连接被判空闲，这段会直接加在第一句上）", flush=True)
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
                            self._last_activity = self._last_use
                            break
                        # turn.start / response / audio.metadata 都不需要
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE,
                                      aiohttp.WSMsgType.CLOSED):
                        raise WSSDown(f"WSS 断开 type={msg.type}")
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
