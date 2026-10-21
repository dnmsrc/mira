"""Neural TTS client for the local CosyVoice2 sidecar (127.0.0.1:8790).

This module runs inside the app's own .venv, which has **no torch** on purpose:
it only speaks HTTP to `tools/tts/serve_neural.py` (running in the separate
`mira-tts` conda env) and transcodes the returned WAV to mp3 with PyAV, which the
app already depends on for the edge post-processing pipeline.

Contract (see docs/PERF_CONTRACT.md §5, docs/VOICE_NEURAL.md):
    available() -> bool                        # sidecar healthy AND weights on disk
    await synth(text, emotion="neutral", *, position="follow", timeout=30.0) -> Path
    await synth_cached(...) -> tuple[Path, bool]   # same, plus cache-hit flag

Audio always lands in ``media/tts/<sha1>.mp3`` because the web player fetches
``/media/tts/<name>``; the *cache* lives in ``media/tts_neural/`` so a neural
build can be invalidated independently from the edge cache (bump VERSION).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TTS_CACHE = ROOT / "media" / "tts"          # 网页 URL 指向这里，必须写到这里
NEURAL_CACHE = ROOT / "media" / "tts_neural"  # 神经音频的本地缓存（可整目录清空）
NEURAL_CACHE.mkdir(parents=True, exist_ok=True)
TTS_CACHE.mkdir(parents=True, exist_ok=True)

VERSION = "neural-4"          # 换模型/改 instruct/改后处理/换参考音色时 +1，旧缓存自动失效
ENGINE = "cosyvoice2"

# 参考音色：**从卡里读**（cards/mira_cafe/card.yaml → character.voice.neural）。
# 为什么不再写死：卡是"换引擎/换参考"的唯一入口，写死在这里就等于在卡上没法表达
# （v9 之前 character.voice.name 只有 edge 音色名，providers 的神经路径根本不读它）。
# ref_sha1 进缓存 key → 换参考文件缓存自动失效；/api/health 还能对出侧车加载的是不是
# 卡里这份（serve_neural /health 会报 prompt_sha1）。
def _voice_cfg() -> dict:
    try:
        from .. import persona          # 卡已经由 persona 读好，不新开 IO

        return dict(getattr(persona, "VOICE_NEURAL", {}) or {})
    except Exception as exc:  # noqa: BLE001  卡坏了也不能让 TTS 模块 import 失败
        print(f"[tts] read card voice.neural failed: {type(exc).__name__}: {exc}", flush=True)
        return {}


_VC = _voice_cfg()
REF = ROOT / str(_VC.get("ref") or "assets/voice/ref_zh_female_young.wav")
REF_SHA1 = str(_VC.get("ref_sha1") or "").strip().lower()
SPEED = float(_VC.get("speed") or 1.0)
PROMPT_TEXT = str(_VC.get("prompt_text") or "")
# 进缓存 key 的音色标识：优先卡里的 ref_sha1 前 12 位，退化为文件名。
VOICE = REF_SHA1[:12] if REF_SHA1 else REF.stem
BASE_URL = os.environ.get("MIRA_TTS_NEURAL_URL", "http://127.0.0.1:8790").rstrip("/")

# 模型文件目录（mira-tts 环境里下载的 ModelScope 快照）。available() 会扫这个目录,
# 如果换机器/换缓存路径，用 MIRA_TTS_MODEL_DIR 覆盖。
MODEL_ROOT = Path(os.environ.get("MIRA_TTS_MODEL_DIR", ROOT / "models" / "modelscope"))
SNIPPET_FILES = ("llm.pt", "flow.pt", "hift.pt")

_PROBE_TTL = 5.0
_probe = {"at": 0.0, "ok": False, "engine": "", "detail": "", "ref_match": None, "prompt": ""}


def _model_present() -> bool:
    """weights on disk? (modelscope 快照会多一层 hash 目录，所以按文件名扫)"""
    if not MODEL_ROOT.exists():
        return False
    seen = 0
    try:
        for p in MODEL_ROOT.rglob("llm.pt"):
            seen += 1
            if all((p.parent / f).exists() for f in SNIPPET_FILES[1:]) or p.stat().st_size > 50_000_000:
                return True
    except OSError:
        return False
    return False


def health(timeout: float = 1.5) -> dict:
    """GET /health（同步，只用于 available()/调试；不要在事件循环里频繁调用）。"""
    import httpx

    try:
        with httpx.Client(timeout=httpx.Timeout(timeout, connect=0.4), trust_env=False) as c:
            r = c.get(BASE_URL + "/health")
            data = r.json()
            return {"status": r.status_code, **data}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": 0, "error": f"{type(exc).__name__}: {exc}"}


def available() -> bool:
    """侧车在线 + 模型已加载 + 权重在盘上。结果缓存 5s,避免每句都探一次。

    另外核对**侧车加载的参考音频是不是卡里那份**（serve_neural /health 的 prompt_sha1）：
    对不上时打一行警告并在 _probe 里记 ref_match=False（/api/health 可见），但**不掐掉
    声音**——演示时宁可音色是旧的，也不能因为一次配置漂移整轮没声。想换成真·强制一致:
    把这里 return 改成 `ok and ref_match`。
    """
    import time

    now = time.time()
    if now - _probe["at"] < _PROBE_TTL:
        return _probe["ok"]
    ok = False
    detail = ""
    ref_match = None
    prompt_name = ""
    if _model_present():
        h = health()
        ok = bool(h.get("ok")) and h.get("status") == 200
        prompt_name = str(h.get("prompt") or "")
        side_sha = str(h.get("prompt_sha1") or "").strip().lower()
        if REF_SHA1 and side_sha:
            ref_match = side_sha == REF_SHA1
            if not ref_match:
                print(f"[tts] 参考音色不一致：卡里 ref_sha1={REF_SHA1[:12]} "
                      f"({REF.name})，侧车加载的是 {prompt_name or '?'} sha1={side_sha[:12]}"
                      f" —— 重启侧车（tools/tts/start_neural.ps1）才会用卡里那份", flush=True)
        detail = f"{h.get('engine')}/{h.get('device')}/{h.get('error')}"
    else:
        detail = "model files missing"
    _probe.update({"at": now, "ok": ok, "engine": detail, "detail": detail,
                   "ref_match": ref_match, "prompt": prompt_name})
    return ok


def probe_state() -> dict:
    """调试/健康检查用：上一次探测的结果（含参考音频是否与卡一致）。"""
    return dict(_probe)


def cache_name(text: str, emotion: str, position: str) -> str:
    key = "|".join([VERSION, ENGINE, VOICE, emotion, position, text])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".mp3"


def _publish(src: Path, dst: Path) -> None:
    """把缓存文件放到 /media/tts/ 下可访问的位置（优先硬链接，失败再复制）。"""
    if dst.exists() and dst.stat().st_size == src.stat().st_size:
        return
    try:
        if dst.exists():
            dst.unlink()
        os.link(src, dst)
    except OSError:
        dst.write_bytes(src.read_bytes())


def _to_mp3(wav_bytes: bytes, out: Path) -> Path:
    """WAV 字节 → 44.1k mp3（96kbps），复用 edge 管线的 PyAV 编码器。"""
    from . import postprocess as pp

    tmp_wav = out.with_suffix(".src.wav")
    tmp_mp3 = out.with_suffix(".part.mp3")
    tmp_wav.write_bytes(wav_bytes)
    try:
        y = pp.decode(tmp_wav, 44100)
        if y.size < 1000:
            raise RuntimeError("decoded audio too short")
        pp.write_mp3(y, tmp_mp3, 44100, 96000)
        os.replace(tmp_mp3, out)
        return out
    finally:
        for p in (tmp_wav, tmp_mp3):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


async def synth_cached(text: str, emotion: str = "neutral", *,
                       position: str = "follow", timeout: float = 30.0) -> tuple[Path, bool]:
    """合成一句。返回 (media/tts 下的 mp3 路径, 是否命中缓存)。失败抛异常,由调用方回退。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("text is empty")
    name = cache_name(text, emotion, position)
    cached = NEURAL_CACHE / name
    published = TTS_CACHE / name
    if published.exists() and published.stat().st_size > 1500:
        return published, True
    if cached.exists() and cached.stat().st_size > 1500:
        _publish(cached, published)
        return published, True

    import httpx

    payload = {"text": text, "emotion": emotion, "position": position}
    if SPEED and abs(SPEED - 1.0) > 1e-6:
        # 卡里 voice.neural.speed：侧车会把它传给 CosyVoice 的 speed 参数
        # （**流式路径上游会忽略它**，见 docs/VOICE_NEURAL.md §7；非 1.0 时行为可能不一致）
        payload["speed"] = SPEED
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=1.0),
                                 trust_env=False) as client:
        r = await client.post(BASE_URL + "/synth", json=payload)
        if r.status_code != 200:
            body = r.text[:200]
            raise RuntimeError(f"sidecar HTTP {r.status_code}: {body}")
        wav = r.content
        dur = int(r.headers.get("X-Duration-Ms") or 0)
        if not wav or len(wav) < 2000:
            raise RuntimeError(f"sidecar returned empty audio ({len(wav)} bytes)")
        if dur and dur < 120:
            raise RuntimeError(f"sidecar audio too short: {dur}ms")

    # 转码是 CPU/ffmpeg 活，扔到线程里，别卡住事件循环
    loop = asyncio.get_running_loop()
    tmp_out = NEURAL_CACHE / (name + ".part")
    await loop.run_in_executor(None, lambda: _to_mp3(wav, tmp_out))
    os.replace(tmp_out, cached)
    _publish(cached, published)
    return published, False


async def synth(text: str, emotion: str = "neutral", *, position: str = "follow",
                timeout: float = 30.0) -> Path:
    """Contract entry point used by app/providers.py."""
    path, _cached = await synth_cached(text, emotion, position=position, timeout=timeout)
    return path


def main() -> None:
    """调试：python -m app.tts.neural "一句话" worried"""
    import sys

    text = sys.argv[1] if len(sys.argv) > 1 else "我在等一个……说好会来的人。十点半。"
    emo = sys.argv[2] if len(sys.argv) > 2 else "worried"
    print("available:", available(), health())
    p, c = asyncio.run(synth_cached(text, emo))
    print("->", p, "cached=", c, "size=", p.stat().st_size)


if __name__ == "__main__":
    main()
