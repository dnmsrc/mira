"""可直接合并进 app/providers.py 的候选实现（不改线上文件）。

对外接口（与现有 TTSProvider.synth 对齐，返回值同样是 (path, used_cache)）：
    async def synth(text, emotion="neutral", out_path=None, *,
                    timeout=12.0, position="follow", voice=None,
                    mode="auto") -> tuple[Path, bool]

三种模式：
  whole   整句一次合成（保住 TTS 自身的句内语调连续性），只做空间/气口/抖动后处理
  clause  按标点切小句分别合成，逐小句控制音高下倾与停顿（适合省略号/长句）
  auto    默认：省略号或「长句 + 多标点」才切小句，其余整句

与现方案的区别：
  现方案  一句话 → 一次 edge-tts（整句一个 rate/pitch）→ 24k mp3 直接播
  本方案  一句话 → 整句或小句 → 44.1k 拼接（精确停顿 + 气口）
          → 小房间混响 / 高频柔化 / -58dB 底噪 → mp3

实测约束（见 docs/research/tts_voice.md）：Edge 朗读端点拒绝 mstts:express-as
（close code 1007），所以「情绪风格」只能靠参数 + 后处理近似，没有 style 可传。

合并步骤：
  1) proposal.py / prosody.py / postprocess.py 放进 app/（建议 app/tts_lab/）
  2) import 改成包内相对导入
  3) TTSProvider.synth 转调本函数；缓存目录改回 media/tts
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import time
from pathlib import Path

try:  # 包内导入（合并进 app/ 后走这条）
    from . import postprocess as pp
    from . import prosody
except ImportError:  # 直接脚本运行
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import postprocess as pp  # type: ignore
    import prosody  # type: ignore

import edge_tts

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = Path(os.environ.get("MIRA_TTS_CACHE", ROOT / "media" / "tts_lab"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
VERSION = "lab-2"          # 改算法时 +1，旧缓存自动失效
MAX_PARALLEL = 4


class TTSError(RuntimeError):
    pass


def cache_path(text: str, emotion: str, position: str, voice: str, mode: str) -> Path:
    key = "|".join([VERSION, voice, emotion, position, mode, text])
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return CACHE_DIR / f"{h}.mp3"


def _safe_unlink(p: Path) -> None:
    """Windows 上失败句柄未释放时 unlink 会抛 PermissionError，不能让它盖住真错误。"""
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


async def _synth_clause(text: str, voice: str, rate: str, pitch: str, volume: str,
                        out: Path, timeout: float) -> Path:
    tmp = out.with_suffix(".part.mp3")
    try:
        comm = edge_tts.Communicate(text, voice, rate=rate, pitch=pitch, volume=volume)
        await asyncio.wait_for(comm.save(str(tmp)), timeout=timeout)
        if not tmp.exists() or tmp.stat().st_size < 400:
            raise TTSError(f"小句合成输出为空：{text[:12]}")
        tmp.replace(out)
        return out
    except Exception:
        _safe_unlink(tmp)
        raise


async def synth(
    text: str,
    emotion: str = "neutral",
    out_path: str | Path | None = None,
    *,
    timeout: float = 12.0,
    position: str = "follow",
    voice: str | None = None,
    mode: str = "auto",
    also_wav: bool = False,
) -> tuple[Path, bool]:
    """合成一句台词。返回 (音频路径, 是否命中缓存)。"""
    text = (text or "").strip()
    if not text:
        raise TTSError("text 为空")
    voice = voice or DEFAULT_VOICE
    out = Path(out_path) if out_path else cache_path(text, emotion, position, voice, mode)
    # 只有「自动缓存路径」才复用；显式给 out_path 时必须真的重新合成（否则改算法后拿到旧文件）
    if out_path is None and out.exists() and out.stat().st_size > 1500:
        return out, True
    out.parent.mkdir(parents=True, exist_ok=True)

    use_whole = mode == "whole" or (mode == "auto" and not prosody.should_split(text))
    clauses = [prosody.whole_params(text, emotion, position)] if use_whole \
        else prosody.plan(text, emotion, position=position)
    if not clauses:
        raise TTSError("无法切分文本")

    sem = asyncio.Semaphore(MAX_PARALLEL)
    tmp_dir = out.parent / f".part-{out.stem}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    async def one(i: int, c) -> Path:
        async with sem:
            return await _synth_clause(
                c.text, voice, prosody.fmt(c.rate, "%"), prosody.fmt(c.pitch, "Hz"),
                prosody.fmt(c.volume, "%"), tmp_dir / f"c{i}.mp3", timeout,
            )

    try:
        files = await asyncio.gather(*(one(i, c) for i, c in enumerate(clauses)))
    except Exception as exc:  # 小句方案失败 → 退回整句单发，至少还有声音
        for f in list(tmp_dir.glob("*.mp3")):
            _safe_unlink(f)
        try:
            base = prosody.EMOTION_BASE.get(emotion, prosody.EMOTION_BASE["neutral"])
            tmp = out.with_suffix(".fallback.mp3")
            await _synth_clause(text, voice, prosody.fmt(base["rate"], "%"),
                                prosody.fmt(base["pitch"], "Hz"),
                                prosody.fmt(base["volume"], "%"), tmp, timeout)
            tmp.replace(out)
            return out, False
        except Exception:
            raise TTSError(f"TTS 失败（小句 + 整句回退均失败）：{type(exc).__name__}: {exc}") from exc

    def _post(files: list[Path]) -> None:
        """同步 DSP：解码 → 裁静音 → 重排停顿 → 混响/柔化/底噪 → 编码 mp3。

        2026-10-21：这一整段原来跑在事件循环里。它是纯 CPU 同步代码（PyAV 解码
        + 44.1k 重采样 + FFT 卷积 + libmp3lame 编码），三句并发的 TTS 任务会互相
        把事件循环堵住——实测单句 synth_ms 从隔离测的 2.4–2.9s 涨到 11.8s / 13.9s，
        而同一时刻 LLM 的流也在等这个循环。挪到线程池后事件循环不再被它占住。
        """
        shape = prosody.EMOTION_SHAPE.get(emotion, prosody.EMOTION_SHAPE["neutral"])
        # 裁掉每个小句自带的首尾静音，停顿完全由我们控制
        clips = [pp.tail_fade(pp.trim_silence(pp.decode(f)), 40) for f in files]
        if len(clips) > 1:
            clips[-1] = (clips[-1] * 0.87).astype("float32")  # 末小句收气
        lead_ms = int(round((70 + (40 if position == "first" else 0))
                            * (1 + prosody._jitter(text, "lead", 0.15))))
        gaps = [c.gap_ms for c in clauses] if len(clauses) > 1 else [0]
        y = pp.place(
            clips, gaps, lead_ms=lead_ms, tail_ms=150,
            breaths=[c.breath for c in clauses],
            intro_breath=shape["breath"] if use_whole else 0.35,
        )
        y = pp.finalize(y, wet=shape["wet"], dull=True,
                        room_tone_db=-58.0, target_rms_db=-20.0)
        tmp_out = out.with_suffix(".part.mp3")
        pp.write_mp3(y, tmp_out, 44100, 96000)
        tmp_out.replace(out)
        if also_wav:
            pp.write_wav(y, out.with_suffix(".wav"), 44100)

    try:
        await asyncio.to_thread(_post, list(files))
        return out, False
    finally:
        for f in list(tmp_dir.glob("*.mp3")):
            _safe_unlink(f)
        try:
            tmp_dir.rmdir()
        except OSError:
            pass


def plan_json(text: str, emotion: str = "neutral", position: str = "follow",
              mode: str = "auto") -> list[dict]:
    """调试用：看一句话被切成什么样。"""
    if mode == "whole" or (mode == "auto" and not prosody.should_split(text)):
        return [prosody.as_dict(prosody.whole_params(text, emotion, position))]
    return [prosody.as_dict(c) for c in prosody.plan(text, emotion, position=position)]


if __name__ == "__main__":
    import json

    if len(sys.argv) < 2:
        demo = "我在等一个……说好会来的人。十点半。"
        print(json.dumps(plan_json(demo, "worried", "first"), ensure_ascii=False, indent=1))
        p, cached = asyncio.run(synth(demo, "worried", "demo_proposal.mp3", position="first"))
        print(p, cached)
    else:
        text = sys.argv[1]
        emo = sys.argv[2] if len(sys.argv) > 2 else "neutral"
        outp = sys.argv[3] if len(sys.argv) > 3 else "demo_proposal.mp3"
        md = sys.argv[4] if len(sys.argv) > 4 else "auto"
        p, cached = asyncio.run(synth(text, emo, outp, mode=md))
        print(p, cached)
