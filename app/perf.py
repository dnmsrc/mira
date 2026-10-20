"""表演导演：把「这一轮该演什么」变成带时间锚的 beat 列表。

为什么单独一层：模型（4B）只会吐一个平铺的 directive（emotion/action/event），
客户端收到就换图 —— 表情是「啪」一下换的，动作在开口前就演完了，所以看起来像
在看幻灯片。真正让角色"活着"的不是更多表情素材，而是**时间**：
  - 听到问题先「顿一下」再开口（反应时间）；
  - 顿的这段时间里视线移开、而不是僵着脸；
  - 手势落在句间停顿，而不是开场一次性做完；
  - 说完之后回到「听」的脸，而不是保持最后一帧。
本模块只做规则排期，不调模型；输出格式见 docs/PERF_CONTRACT.md。

设计约束：
  1) 纯函数 + 显式 rnd 注入，便于复现；
  2) 锚点只用 turn.start / s{i}.start / s{i}.end，客户端拿真实音频时长解析；
  3) 宁可少给 beat，也不要给时间上互相打架的 beat（同一个 200ms 里换两次脸最假）。
"""

from __future__ import annotations

import random
import re
from typing import Any

# 开口前"想一下"时看向别处的视线，用 thinking 姿态实现（客户端映射见契约 §3）
THINK_GAZE_MS = 200

# 语气词：真人接话常常先"嗯"一下。只在这些意图上用，关键轮（报名字/我是谁）不用，
# 那几轮已经在做「生成→校验→重写」，再加拖延会让用户以为卡住了。
FILLERS = ("嗯……", "嗯，", "啊……", "……嗯。")
FILLER_INTENTS = {"why", "wait_who", "default", "volunteer", "photo"}


def new_rnd(seq: int) -> random.Random:
    """按轮次播种：同一轮重放得到同一套表演，方便复现问题。"""
    return random.Random(20261020 + int(seq or 0))


def reaction_ms(*, kind: str, text: str, plan: Any, directive: dict, rnd: random.Random) -> int:
    """本轮开口前的反应时间（毫秒）。

    真人不会在对方话音刚落就接话；太整齐的 300ms 本身就是"人机感"。
    这里按三件事给：问题多难、这轮重不重要、情绪多强。
    """
    n = len(text or "")
    base = 250
    intent = getattr(plan, "intent", "default")
    if kind == "open":
        base = 520                      # 她先抬头看一眼再说话
    elif n >= 24:
        base += 130                     # 长问题要消化
    elif n <= 6:
        base -= 70                      # 「嗯？」这种短促的，反应快
    if getattr(plan, "critical", False):
        base += 140                     # 记不记得名字 / 你是谁：真的要想一下
    if intent == "why":
        base += 110                     # 被问到痛处，会顿
    elif intent == "greeting":
        base -= 90
    if directive.get("emotion") == "surprised":
        base -= 60                      # 被惊到是瞬间的
    elif directive.get("emotion") == "worried":
        base += 60
    base += rnd.randint(-55, 85)
    return max(150, min(640, base))


def filler_for(*, kind: str, plan: Any, rnd: random.Random) -> str | None:
    """要不要用语气词起头。返回 None 表示直接说。"""
    if kind == "open" or getattr(plan, "critical", False):
        return None
    intent = getattr(plan, "intent", "default")
    p = 0.42 if intent in FILLER_INTENTS else 0.16
    if rnd.random() >= p:
        return None
    return rnd.choice(FILLERS)


def early_beats(*, kind: str, directive: dict, hold_ms: int) -> list[dict]:
    """directive 一到就能定的 beat：反应时间的"演法" + 开口瞬间的表情。"""
    emo = directive.get("emotion") or "neutral"
    beats: list[dict] = []
    if kind == "open":
        beats.append({"at": "turn.start", "off": 0, "kind": "breath", "value": "soft"})
        beats.append({"at": "turn.start", "off": 0, "kind": "expression", "value": emo})
        return beats
    # 1) 先是一张"在听/在想"的脸
    beats.append({"at": "turn.start", "off": 0, "kind": "expression", "value": "thinking"})
    # 2) 反应时间里视线移开（僵着看镜头最假）；必须在换情绪之前收回来
    gaze_hold = hold_ms - 220
    if gaze_hold >= THINK_GAZE_MS:
        beats.append({"at": "turn.start", "off": 70,
                      "kind": "gaze", "value": "away", "hold": gaze_hold})
    # 3) 开口前 ~140ms 换成本轮情绪，人先有表情再出声
    #    （本轮情绪本来就是 thinking 时不用再来一次，否则同一张脸被排两个 beat）
    hold_off = max(0, hold_ms - 140)
    if emo != "thinking":
        beats.append({"at": "turn.start", "off": hold_off,
                      "kind": "expression", "value": emo})
    # 4) 担心/惊讶：开口前吸一口气（肩上能看见）
    if emo in ("worried", "surprised"):
        beats.append({"at": "turn.start", "off": max(0, hold_ms - 90),
                      "kind": "breath", "value": "sharp"})
    return beats


def _sentence_with(sentences: list[str], pattern: str) -> int | None:
    rx = re.compile(pattern)
    for i, s in enumerate(sentences):
        if rx.search(s or ""):
            return i
    return None


def late_beats(*, directive: dict, plan: Any, sentences: list[str],
               text: str = "", reply: str = "") -> list[dict]:
    """台词已经分好句之后才能定的 beat：手势落点、视线、收尾表情。

    sentences 必须和 TTS/客户端的 audio.index 一一对应（同一套切句结果）。
    """
    n = len(sentences)
    if n == 0:
        return []
    last = n - 1
    intent = getattr(plan, "intent", "default")
    emo = directive.get("emotion") or "neutral"
    action = directive.get("action") or "idle"
    beats: list[dict] = []

    # ---- 手势：落在第 2 句末尾（或唯一一句的后半段），不在开场就演完
    if action not in ("idle", ""):
        if action == "show_photo":
            k = _sentence_with(sentences, r"照片|相片|给你看|你看")
            k = last if k is None else k
            anchor, off = f"s{k}.end", -260
        elif n == 1:
            anchor, off = f"s{last}.end", -760   # 只有一句：在句中偏后抬手动一下
        else:
            k = min(1, last)
            anchor, off = f"s{k}.end", -320
        beats.append({"at": anchor, "off": off, "kind": "gesture",
                      "value": action, "hold": 2400})

    # ---- 视线：对方提到窗外/雨/天气，或她本来就在想事 → 看向窗外
    #      锚在句子开头（off 小），否则短句会排在「说完回脸」之后，变成说完才扭头
    wants_window = bool(re.search(r"窗|外面|雨|天气|夜|路上", text or "")) and action != "look_window"
    if wants_window and n >= 2:
        beats.append({"at": f"s{min(1, last)}.start", "off": 60,
                      "kind": "gaze", "value": "window", "hold": 1500})

    # ---- 中段微表情：问候/自述时，中间一句松一下（整轮一个表情最假）
    if n >= 3 and intent in ("greeting", "volunteer") and emo in ("neutral", "thinking"):
        beats.append({"at": f"s1.start", "off": 160,
                      "kind": "expression", "value": "smile"})
    # 被问到痛处（为什么/在等谁）：整轮只有一张 "thinking" 脸太扁，
    # 第一句说完时松成"担心"——这是卡包里 admit 拍的既定情绪，不是乱加。
    if n >= 2 and emo == "thinking" and intent in ("why", "wait_who"):
        beats.append({"at": f"s0.end", "off": -150,
                      "kind": "expression", "value": "worried"})

    # ---- 收尾：说完回到「听」的脸（反问/试探）或平静
    tail_face = "listen" if re.search(r"[？?]\s*$|吗|呢|你要|你呢", reply or "") else "neutral"
    if emo == "smile" and tail_face == "listen":
        tail_face = "smile"          # 笑着问，不要笑到一半变脸
    beats.append({"at": f"s{last}.end", "off": 80, "kind": "expression", "value": tail_face})
    return beats


def plan_reply_split(reply: str) -> list[str]:
    """与 app/main.py 的 take_sentences 保持一致的切句（用于离线预演/自测）。"""
    parts = re.split(r"(?<=[。！？!?…])", reply or "")
    return [p.strip() for p in parts if p.strip()]


def describe(beats: list[dict]) -> list[str]:
    """给观测面板/日志用的一行人话。"""
    out = []
    for b in beats:
        off = b.get("off") or 0
        sign = f"{off:+d}ms" if off else "0"
        extra = f" {b['hold']}ms" if b.get("hold") else ""
        out.append(f"{b['at']} {sign} {b['kind']}={b['value']}{extra}")
    return out
