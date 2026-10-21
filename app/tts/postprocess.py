"""「去人机感」DSP 后处理：小房间混响 / 轻微高频衰减 / 底噪 / 气口 / 统一 44.1k。

只依赖 numpy + PyAV（PyAV 自带 ffmpeg，负责 mp3 解码与编码；不额外装库）。

处理链（顺序有讲究）：
  1. 每个小句各自解码到 44.1kHz 单声道
  2. 句尾 40ms 淡出 + 末小句 -1.2dB（自然收气，避免「一刀切」的尾巴）
  3. 按标点插入静音，并在长停顿前叠一个极轻的吸气气口
  4. 房间脉冲响应卷积（早期反射 + 扩散尾），湿声按情绪配比
  5. 两级一阶低通（≈8.5kHz），去掉 TTS 干声的「齿音发亮」
  6. 铺 -58dBFS 粉噪底噪：让停顿不再是数字静音
  7. RMS 归一化到 -20dBFS，峰值 ≤ -1.5dBFS

2026-10-21 13:4x：**第 4/5/6 步默认全关**（用户反馈「有回音」，且「AI 感还是很浓」）。
0.55s 的房间尾 + 8.2k 低通 + 底噪三段叠起来，在耳机上是「录音棚里的朗读」；关掉之后
听到的是 edge 干声本身，齿音亮但近、有人味。代码全部保留（make_room_ir / lowpass_fast /
pink_noise），要加回房间把 wet 调回 0.16、dull=True、room_tone_db=-58 即可。
"""

from __future__ import annotations

import io
import wave
from pathlib import Path

import av
import numpy as np

SR = 44100


# ------------------------------------------------------------------ I/O

def decode(path: str | Path, sr: int = SR) -> np.ndarray:
    container = av.open(str(path))
    stream = container.streams.audio[0]
    res = av.AudioResampler(format="flt", layout="mono", rate=sr)
    chunks = []
    for frame in container.decode(stream):
        for out in res.resample(frame):
            chunks.append(out.to_ndarray().reshape(-1))
    for out in res.resample(None):
        chunks.append(out.to_ndarray().reshape(-1))
    container.close()
    return np.concatenate(chunks).astype(np.float32) if chunks else np.zeros(0, np.float32)


def write_wav(y: np.ndarray, path: str | Path, sr: int = SR) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = np.clip(y, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def write_mp3(y: np.ndarray, path: str | Path, sr: int = SR, bitrate: int = 96000) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    container = av.open(buf, "w", format="mp3")
    stream = container.add_stream("libmp3lame", rate=sr, layout="mono")
    stream.bit_rate = bitrate
    pcm = np.clip(y, -1.0, 1.0).astype(np.float32).reshape(1, -1)
    step = 1024
    pts = 0
    for i in range(0, pcm.shape[1], step):
        block = np.ascontiguousarray(pcm[:, i:i + step])
        frame = av.AudioFrame.from_ndarray(block, format="flt", layout="mono")
        frame.sample_rate = sr
        frame.pts = pts
        pts += block.shape[1]
        for pkt in stream.encode(frame):
            container.mux(pkt)
    for pkt in stream.encode(None):
        container.mux(pkt)
    container.close()
    path.write_bytes(buf.getvalue())
    return path


# ------------------------------------------------------------------ DSP

def _fft_filter(x: np.ndarray, gain_fn, sr: int = SR) -> np.ndarray:
    n = len(x)
    if n == 0:
        return x
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    return np.fft.irfft(spec * gain_fn(freqs), n).astype(np.float32)


def lowpass(x: np.ndarray, fc: float = 8500.0, passes: int = 2, sr: int = SR) -> np.ndarray:
    """一阶级联低通（相位非线性无所谓，听感上更「近场」）。"""
    a = float(np.exp(-2 * np.pi * fc / sr))
    y = x.astype(np.float32)
    for _ in range(passes):
        out = np.empty_like(y)
        acc = 0.0
        for i in range(len(y)):
            acc = (1 - a) * float(y[i]) + a * acc
            out[i] = acc
        y = out
    return y


def lowpass_fast(x: np.ndarray, fc: float = 8500.0, sr: int = SR) -> np.ndarray:
    """频域平滑滚降：5k 以上逐渐衰减，16k 处约 -12dB。"""
    return _fft_filter(x, lambda f: 1.0 / np.sqrt(1.0 + (f / fc) ** 2), sr)


def _norm(x: np.ndarray) -> np.ndarray:
    p = np.sqrt((x ** 2).mean()) + 1e-9
    return x / p


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt((x ** 2).mean() + 1e-12))


def make_room_ir(wet: float, sr: int = SR, seed: int = 7) -> np.ndarray:
    """0.55s 小房间 IR：6 个早期反射 + 指数衰减扩散尾（尾巴低通）。"""
    rng = np.random.default_rng(seed)
    n = int(sr * 0.55)
    t = np.arange(n) / sr
    tail = rng.standard_normal(n) * np.exp(-t / 0.17)
    tail = _fft_filter(tail, lambda f: 1.0 / np.sqrt(1.0 + (f / 3200.0) ** 2), sr)
    tail = _norm(tail) * 0.30
    ir = np.zeros(n, dtype=np.float32)
    ir[0] = 1.0
    for ms, g in ((11, 0.26), (17, 0.20), (23, 0.15), (29, 0.11), (37, 0.08), (47, 0.05)):
        ir[int(sr * ms / 1000)] += g * wet
    ir += (tail * wet).astype(np.float32)
    return ir


def convolve(x: np.ndarray, h: np.ndarray) -> np.ndarray:
    n = len(x) + len(h) - 1
    nfft = 1 << (n - 1).bit_length()
    y = np.fft.irfft(np.fft.rfft(x, nfft) * np.fft.rfft(h, nfft), nfft)[:len(x)]
    return y.astype(np.float32)


def add_room(x: np.ndarray, wet: float, sr: int = SR) -> np.ndarray:
    """湿声按干声 RMS 的比例混入，保证不同句子的空间感一致。"""
    if wet <= 0:
        return x
    ir = make_room_ir(1.0, sr)
    wet_sig = convolve(x, ir)
    wet_sig = wet_sig / (_rms(wet_sig) + 1e-9) * _rms(x) * wet
    return (x * (1.0 - 0.25 * wet) + wet_sig).astype(np.float32)


def pink_noise(n: int, sr: int = SR, seed: int = 11) -> np.ndarray:
    rng = np.random.default_rng(seed)
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.fft.rfftfreq(n, 1 / sr)
    f[0] = f[1] if len(f) > 1 else 1.0
    spec = spec / np.sqrt(f)
    spec[0] = 0
    y = np.fft.irfft(spec, n).astype(np.float32)
    return _norm(y)


def breath(dur_ms: int = 150, sr: int = SR, seed: int = 3) -> np.ndarray:
    """吸气：噪声经 600–2600Hz 带通 + 快起慢落包络。"""
    n = int(sr * dur_ms / 1000)
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n).astype(np.float32)
    x = _fft_filter(x, lambda f: np.exp(-((f - 1500.0) / 1100.0) ** 2), sr)
    env = np.ones(n, dtype=np.float32)
    atk = max(1, int(n * 0.35))
    env[:atk] = np.linspace(0, 1, atk) ** 1.5
    env[atk:] = np.linspace(1, 0, n - atk) ** 1.8
    x = x * env
    # 轻微高频滚降，避免听起来像「嘶」
    return lowpass_fast(x, 3000, sr)


def place(clips: list[np.ndarray], gaps_ms: list[int], lead_ms: int = 80,
          tail_ms: int = 140, sr: int = SR, breaths: list[float] | None = None,
          breath_gain: float = -30.0, intro_breath: float = 0.0) -> np.ndarray:
    """按小句拼接，句间插入静音；长停顿前叠一个气口。"""
    speech_rms = max((_rms(c) for c in clips if len(c)), default=0.05)
    lead = int(sr * lead_ms / 1000)
    total = lead + int(sr * tail_ms / 1000) + sum(len(c) for c in clips)
    total += sum(int(sr * g / 1000) for g in gaps_ms)
    out = np.zeros(total, dtype=np.float32)
    pos = lead
    for i, clip in enumerate(clips):
        if breaths and i < len(breaths) and breaths[i] > 0.05 and i > 0:
            b = breath(int(120 + 60 * breaths[i]), sr, seed=17 + i)
            b = b / (_rms(b) + 1e-9) * speech_rms * (10 ** (breath_gain / 20.0)) * breaths[i]
            # 气口放在小句开头前，和句间静音重叠一部分
            start = max(int(sr * 0.02), pos - len(b))
            seg = b[:max(0, min(len(b), total - start))]
            out[start:start + len(seg)] += seg
        if i == 0 and intro_breath > 0.05:
            b = breath(int(130 + 50 * intro_breath), sr, seed=41)
            b = b / (_rms(b) + 1e-9) * speech_rms * (10 ** (breath_gain / 20.0)) * intro_breath
            start = max(0, lead - len(b) - int(sr * 0.015))
            seg = b[-min(len(b), total - start):]
            out[start:start + len(seg)] += seg
        out[pos:pos + len(clip)] += clip
        pos += len(clip) + int(sr * gaps_ms[i] / 1000)
    return out


def finalize(x: np.ndarray, *, wet: float = 0.0, dull: bool = False,
             room_tone_db: float | None = None, target_rms_db: float = -20.0,
             peak_db: float = -1.5, sr: int = SR) -> np.ndarray:
    y = add_room(x, wet, sr)
    if dull:
        y = lowpass_fast(y, 8200, sr)
    # 底噪：整段（含停顿）都铺，避免出现突兀的数字静音
    if room_tone_db is not None:
        nz = pink_noise(len(y), sr) * (10 ** (room_tone_db / 20.0))
        y = y + nz
    # 峰值/响度
    rms = _rms(y)
    if rms > 0:
        y = y * (10 ** (target_rms_db / 20.0) / rms)
    peak = float(np.abs(y).max()) + 1e-9
    limit = 10 ** (peak_db / 20.0)
    if peak > limit:
        y = y * (limit / peak)
    return np.clip(y, -1.0, 1.0).astype(np.float32)


def tail_fade(x: np.ndarray, ms: int = 40, sr: int = SR) -> np.ndarray:
    n = min(len(x), int(sr * ms / 1000))
    if n <= 0:
        return x
    y = x.copy()
    y[-n:] *= np.linspace(1, 0, n) ** 1.2
    return y


def trim_silence(x: np.ndarray, sr: int = SR, thresh_db: float = -42.0,
                 keep_ms: int = 20) -> np.ndarray:
    """裁掉 edge-tts 每个小句自带的首尾静音（约 200–300ms）。

    不裁的话，把 3 个小句拼起来会平白多出 ~0.7s 死气，整句被拖慢 40%，
    听起来像「一句一句念」。裁完由 place() 按标点重新给精确停顿。
    """
    if len(x) < int(sr * 0.05):
        return x
    fl = int(sr * 0.02)
    hop = int(sr * 0.01)
    n = 1 + (len(x) - fl) // hop
    idx = np.arange(fl)[None, :] + hop * np.arange(n)[:, None]
    frames = x[idx]
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-12)
    thresh = max(db.max() - 42.0, thresh_db)
    voiced = np.flatnonzero(db > thresh)
    if len(voiced) == 0:
        return x
    a = max(0, (voiced[0] * hop) - int(sr * keep_ms / 1000))
    b = min(len(x), (voiced[-1] * hop + fl) + int(sr * keep_ms / 1000))
    return x[a:b]
