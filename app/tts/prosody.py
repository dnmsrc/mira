"""句级韵律规划：把「一句台词 + 情绪」变成「若干小句 + 每小句的参数 + 停顿」。

为什么要在本地做而不是靠 edge-tts：实测 Edge 朗读端点会以 1007 关闭任何含
mstts:express-as / break / emphasis 的 SSML（见 docs/research/tts_voice.md），
所以「句内」只能靠把一句话切成小句、分别合成、再按标点拼回去——
这是本文件存在的唯一理由。

设计要点（对应「去人机感」）：
  1. 音高不是整句一个值，而是按小句位置做「下倾」（declination）；
  2. 句尾有「末字拖长」：最后一个小句更慢、更轻；
  3. 问句末小句抬音高，省略号前放慢并加长停顿；
  4. 同样的句子每次合成结果必须一致（缓存友好），所以抖动由文本哈希播种；
  5. 第一句（开场）与追句（接话）参数不同：开场略提，追句略降。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# 与 cards/mira_cafe/card.yaml 的 tts.emotion 对齐（rate/pitch/volume 原样复用）
EMOTION_BASE: dict[str, dict] = {
    "neutral":   {"rate": -6.0,  "pitch": -8.0,  "volume": 0.0},
    "smile":     {"rate": -2.0,  "pitch": -2.0,  "volume": 4.0},
    "worried":   {"rate": -12.0, "pitch": -12.0, "volume": -6.0},
    "surprised": {"rate": +2.0,  "pitch": +4.0,  "volume": 6.0},
    "thinking":  {"rate": -16.0, "pitch": -12.0, "volume": -4.0},
    "listen":    {"rate": -6.0,  "pitch": -8.0,  "volume": 0.0},
}

# 情绪 → 句内下倾幅度(Hz/小句)、小句间停顿时长缩放、混响湿度、气口强度
EMOTION_SHAPE: dict[str, dict] = {
    "neutral":   {"decl": 1.6, "gap_scale": 1.00, "wet": 0.16, "breath": 0.55, "final_slow": -4.0},
    "smile":     {"decl": 1.1, "gap_scale": 0.90, "wet": 0.13, "breath": 0.65, "final_slow": -2.0},
    "worried":   {"decl": 2.4, "gap_scale": 1.25, "wet": 0.19, "breath": 0.75, "final_slow": -6.0},
    "surprised": {"decl": 0.6, "gap_scale": 0.80, "wet": 0.12, "breath": 0.35, "final_slow": -1.0},
    "thinking":  {"decl": 2.1, "gap_scale": 1.35, "wet": 0.20, "breath": 0.85, "final_slow": -7.0},
    "listen":    {"decl": 1.4, "gap_scale": 1.05, "wet": 0.16, "breath": 0.50, "final_slow": -4.0},
}

# 标点 → 基础停顿（毫秒）。比卡包 gaps_ms 更细，因为现在由合成端统一写入音频。
GAP_MS = {
    "，": 170, ",": 170,
    "、": 130, "；": 240, ";": 240,
    "：": 200, ":": 200,
    "。": 360, "．": 360,
    "！": 330, "!": 330,
    "？": 390, "?": 390,
    "……": 540, "…": 480,
    "—": 300,
}

_SPLIT_RE = re.compile(r"([，。！？；：、,.!?;:]|……|…|——)")


@dataclass
class Clause:
    text: str                  # 不含标点，交给 TTS
    punct: str = ""            # 该小句后的标点
    rate: float = -6.0
    pitch: float = -8.0
    volume: float = 0.0
    gap_ms: int = 200          # 该小句结束后的静音（含呼吸气口）
    breath: float = 0.0        # 0–1，气口强度
    final: bool = False        # 是否整句最后一个小句
    question: bool = False
    ellipsis: bool = False
    index: int = 0


def split_clauses(text: str) -> list[tuple[str, str]]:
    """切分为 [(正文, 后置标点)]，保留标点用于决定停顿与语调。"""
    text = (text or "").strip()
    if not text:
        return []
    parts = _SPLIT_RE.split(text)
    out: list[tuple[str, str]] = []
    buf = ""
    for p in parts:
        if not p:
            continue
        if _SPLIT_RE.fullmatch(p):
            # 连续标点（如 ？！ 或 。」）合并到上一个
            if buf.strip():
                out.append((buf.strip(), p))
                buf = ""
            elif out:
                body, punct = out[-1]
                out[-1] = (body, punct + p)
        else:
            buf += p
    if buf.strip():
        out.append((buf.strip(), ""))
    # 太碎的片段并回去：少于 2 个汉字且前面有小句时并入前句
    merged: list[tuple[str, str]] = []
    for body, punct in out:
        if merged and len(body) < 2:
            pb, pp = merged[-1]
            merged[-1] = (pb + body, punct or pp)
        else:
            merged.append((body, punct))
    return merged


def _seed(text: str) -> float:
    h = hashlib.sha1(text.encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") / 0xFFFFFFFF  # 0..1


def _jitter(text: str, salt: str, span: float) -> float:
    """确定性抖动：同一句话每次得到同一个值，不同句子得到不同的值。"""
    h = hashlib.sha1((salt + "|" + text).encode("utf-8")).digest()
    return (int.from_bytes(h[4:8], "big") / 0xFFFFFFFF * 2 - 1) * span


def plan(text: str, emotion: str = "neutral", *, position: str = "follow",
         prev_text: str = "") -> list[Clause]:
    """把一句台词规划成小句序列。

    position: "first"（本轮第一句）/ "follow"（追句）/ "last"（收尾句）
    """
    base = EMOTION_BASE.get(emotion, EMOTION_BASE["neutral"])
    shape = EMOTION_SHAPE.get(emotion, EMOTION_SHAPE["neutral"])
    pieces = split_clauses(text)
    if not pieces:
        return []

    # 位置微调：开场略提气，追句略收，收尾更轻
    pos = {"first": (1.0, 1.5, 0.0), "follow": (0.0, 0.0, 0.0),
           "last": (-1.5, -2.0, -2.0)}.get(position, (0.0, 0.0, 0.0))
    seed = _seed(text)
    n = len(pieces)
    clauses: list[Clause] = []
    for i, (body, punct) in enumerate(pieces):
        final = i == n - 1
        question = final and punct in ("？", "?")
        ell = "……" in punct or "…" in punct or "..." in body
        # 下倾：越靠后越低；末小句再压一点（陈述）/ 抬一点（问句）
        decl = -shape["decl"] * i
        pitch = base["pitch"] + decl + pos[0]
        rate = base["rate"] + (1.5 if i == 0 else 0.0) + pos[1]
        vol = base["volume"] + pos[2]
        if final:
            rate += shape["final_slow"]
            pitch += -1.0 if not question else 2.0
            if question:
                rate += 1.0
        if ell:
            rate -= 6.0
        # 确定性抖动
        pitch += _jitter(body, "p", 1.8)
        rate += _jitter(body, "r", 1.2)
        vol += _jitter(body, "v", 0.8)
        # 停顿：标点基础值 × 情绪缩放 × 位置 × 抖动；省略号额外拉长
        gap = GAP_MS.get(punct, 190 if punct else 160) * shape["gap_scale"]
        if ell:
            gap *= 1.25
        if final:
            gap *= 0.8
        gap *= 1.0 + _jitter(text, "g", 0.12)
        # 气口：第一个小句前、以及长停顿处更明显；纯陈述压低
        breath = shape["breath"] * (0.55 + 0.45 * seed)
        if not final and gap > 200:
            breath *= 1.15
        clauses.append(Clause(
            text=body, punct=punct,
            rate=round(rate, 1), pitch=round(pitch, 1), volume=round(vol, 1),
            gap_ms=int(round(gap)), breath=round(breath, 2),
            final=final, question=question, ellipsis=ell, index=i,
        ))
    return clauses


def fmt(value: float, unit: str) -> str:
    """edge-tts 接受 '+3%' / '-8Hz' 这种带符号写法。"""
    return f"{value:+.0f}{unit}"


def whole_params(text: str, emotion: str = "neutral", position: str = "follow") -> Clause:
    """整句一次合成时的参数（不做小句切分，保住 TTS 自身的句内语调连续性）。

    规则与 app/providers.py 的 style_for 同源：问句抬、省略号慢、太短放慢、太长略快，
    再叠位置偏移与确定性抖动。
    """
    base = EMOTION_BASE.get(emotion, EMOTION_BASE["neutral"])
    rate, pitch, vol = base["rate"], base["pitch"], base["volume"]
    t = (text or "").strip()
    if t.endswith(("？", "?")):
        pitch += 3.0
        rate += 1.0
    elif t.endswith(("！", "!")):
        rate += 2.0
    if "……" in t or "..." in t:
        rate -= 5.0
    n = len(t)
    if n <= 8:
        rate -= 3.0
    elif n >= 28:
        rate += 2.0
    pos = {"first": (1.0, 1.5, 0.0), "follow": (0.0, 0.0, 0.0),
           "last": (-1.5, -2.0, -2.0)}.get(position, (0.0, 0.0, 0.0))
    pitch += pos[0] + _jitter(t, "wp", 1.6)
    rate += pos[1] + _jitter(t, "wr", 1.0)
    vol += pos[2] + _jitter(t, "wv", 0.7)
    return Clause(text=t, rate=round(rate, 1), pitch=round(pitch, 1),
                  volume=round(vol, 1), gap_ms=0, breath=0.0, final=True,
                  question=t.endswith(("？", "?")), ellipsis=("……" in t or "..." in t))


def should_split(text: str) -> bool:
    """什么时候值得切成小句：省略号（要长停顿 + 放慢）或长句多标点。"""
    t = (text or "").strip()
    if "……" in t or "…" in t:
        return True
    marks = sum(t.count(m) for m in "，。！？；、")
    return marks >= 2 and len(t) >= 28


def as_dict(c: Clause) -> dict:
    return {
        "text": c.text, "punct": c.punct, "rate": fmt(c.rate, "%"),
        "pitch": fmt(c.pitch, "Hz"), "volume": fmt(c.volume, "%"),
        "gap_ms": c.gap_ms, "breath": c.breath, "final": c.final,
    }
