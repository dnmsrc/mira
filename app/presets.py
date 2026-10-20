"""预设台词库（barks）：命中就直接出字，不调模型、不合成。

为什么需要它（这是「不卡」的第一杠杆）：
  - 现在每轮都要过 4B：首字 p50 ≈ 570ms，加上 TTS 首包，用户说一句「你好」要等 1.5s 以上；
  - 「你好 / 谢谢 / 再见 / 嗯」这些话**没有信息量**，模型生成不会更好，只会更慢、更容易跑题
    （上一版问「你好」，她答「嗯。十点半了，你来得挺晚」就是生成跑题的典型）；
  - 命中预设 = 0 生成 + 预合成音频（启动时已经 TTS 进缓存）= 说完就答。

为什么不干脆全用预设：剧情信息必须走生成。卡包里每拍能松口多少、该给哪个事实、
哪些词绝对不能说，都是 `director` 算出来的；预设写死在文件里，改了卡就答错。
所以预设只覆盖两类：**高频低信息量**（招呼/应答/道谢/告别/闲聊/沉默）
和**必须精确**（越界拒绝、出戏拉回）。

两条防「念台词」的机制（上一版就死在念台词上）：
  1. 每条 ≥3 变体，按游标轮换；同一句和最近 3 轮台词二字组重叠 > 0.6 的直接跳过；
  2. **连续命中上限**：同一拍连续命中 2 次后，第 3 轮强制走生成，保证她时不时说一句没准备过的。

数据结构与校验：卡包旁边的 `cards/mira_cafe/presets.yaml`，启动时校验（行为枚举、关系枚举、
拍引用、变体数、禁词）。校验不过直接抛 `cards.CardError`，和卡包行为一致——**坏数据不许进运行时**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from . import acts, cards

# 同一拍连续命中几次后强制生成（「活人配额」）。2 = 最多连着两条预设。
MAX_CONSEC = 2
# 配额只对这些行为生效：应答 / 闲聊 / 沉默 —— 它们最容易听出"在念表"。
# 招呼 / 道谢 / 告别 / 越界 / 出戏是**仪式性的话**，重复是正常的；而且它们一旦被推去生成，
# 反而更慢、更容易跑题（实测"你好"被送进 4B 后答成"嗯。十点半了，你来得挺晚"就是这么来的）。
QUOTA_ACTS = frozenset({"ack", "smalltalk", "silence"})
# 这条变体与最近某句的重叠率超过它就换下一个变体（全撞则本条不命中）
DUP_RATIO = 0.6


def _clean(s: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s or "")


def _overlap(a: str, b: str) -> float:
    x, y = _clean(a), _clean(b)
    if len(x) < 4 or len(y) < 4:
        return 0.0
    bx = {x[i:i + 2] for i in range(len(x) - 1)}
    by = {y[i:i + 2] for i in range(len(y) - 1)}
    return len(bx & by) / max(1, len(bx))


@dataclass
class Preset:
    id: str
    act: str
    rel: tuple[str, ...]
    beat: tuple[str, ...]
    priority: int
    cooldown: int
    once: bool
    lines: tuple[str, ...]
    perf: dict[str, str]
    when: dict[str, Any] = field(default_factory=dict)

    def directive(self) -> dict[str, str]:
        """把 barks 的表现意图翻译成卡包的指令枚举（emotion/action）。"""
        out = {k: v for k, v in (self.perf or {}).items()
               if k in ("emotion", "action", "scene", "effect", "event") and v}
        return out


@dataclass
class Hit:
    preset_id: str
    act: str
    text: str
    variant: int
    priority: int
    perf: dict[str, str]

    def as_dict(self) -> dict:
        return {"id": self.preset_id, "act": self.act, "variant": self.variant,
                "priority": self.priority, "perf": self.perf, "text": self.text}


class Store:
    """加载 + 查询。游标/冷却状态不放在这里（要跟会话走），由调用方传入字典。"""

    def __init__(self, items: list[Preset], path: Path | None = None) -> None:
        self.items = items
        self.path = path

    # ---------------------------------------------------------------- 查询
    def match(self, *, text: str, rel: str, beat_id: str, now: int,
              recent: Iterable[str], used: dict[str, int], cursor: dict[str, int],
              once_used: set[str], consec: int, act: acts.ActHit | None = None) -> Hit | None:
        """命中返回 Hit，否则 None（调用方回落生成）。

        参数 now 用会话的轮次号（`s.seq`），单调递增即可，不要求是秒。
        act 可以由调用方先算好传进来（trace 里要记它，省一次重复分类）。
        """
        a = act or acts.classify(text)
        if a.act == "other":
            return None
        # 活人配额：同一拍连着命中太多次就交回给模型（预设只管接话，不负责推进）
        if consec >= MAX_CONSEC and a.act in QUOTA_ACTS:
            return None

        cands: list[Preset] = []
        for p in self.items:
            if p.act != a.act:
                continue
            if p.rel and rel not in p.rel:
                continue
            if p.beat and beat_id not in p.beat:
                continue
            if p.once and p.id in once_used:
                continue
            if p.cooldown and p.id in used and now - used[p.id] <= p.cooldown:
                continue
            if not self._when_ok(p, used, recent, text):
                continue
            cands.append(p)
        if not cands:
            return None

        # 优先级高的先试；同优先级「最久没用过的」先试，避免总同一句开头
        cands.sort(key=lambda p: (-p.priority, used.get(p.id, -10**6)))

        recent_list = [r for r in recent if r]
        for p in cands:
            variant = cursor.get(p.id, 0) % len(p.lines)
            for k in range(len(p.lines)):
                idx = (variant + k) % len(p.lines)
                line = p.lines[idx]
                if all(_overlap(line, r) <= DUP_RATIO for r in recent_list[-3:]):
                    cursor[p.id] = (idx + 1) % len(p.lines)
                    used[p.id] = now
                    return Hit(p.id, p.act, line, idx, p.priority, p.directive())
        return None

    @staticmethod
    def _when_ok(p: Preset, used: dict[str, int], recent: Iterable[str],
                 text: str = "") -> bool:
        w = p.when or {}
        after = w.get("after")
        if after and after not in used:
            return False
        not_after = w.get("not_after")
        if not_after and not_after in used:
            return False
        # 内容相关性闸门：`needs` 里任意一个词出现在用户这句话里才算接得上。
        # 为什么必须有：act 只说明「这句话在干什么」（闲聊），不说明「在聊什么」。
        # 实测反例——用户说「我叫张伟，今天加班到现在」，act=smalltalk 命中了
        # small_weather 的「这几天雨都很大。」，回了一句跟对方说的毫无关系的话。
        # 这类「预设把话接歪」比生成慢半秒严重得多：它没有闸门兜底，直接出声。
        needs = w.get("needs") or ()
        if needs and not any(str(n) in (text or "") for n in needs):
            return False
        return True

    def summary(self) -> dict:
        return {"count": len(self.items),
                "path": str(self.path) if self.path else "",
                "acts": sorted({p.act for p in self.items})}


# ---------------------------------------------------------------- 加载与校验
def _fail(msg: str) -> None:
    raise cards.CardError(msg)


def _resolve_path() -> Path | None:
    try:
        card_path = cards._resolve_card_path(None)  # noqa: SLF001  同包复用，避免三处路径逻辑
    except Exception:  # noqa: BLE001  卡包不能解析时由 cards.load 自己报错
        return None
    p = card_path.parent / "presets.yaml"
    return p if p.exists() else None


def _load_items(raw: dict, card: dict) -> list[Preset]:
    beats = card.get("beats") or []
    beat_ids = {b["id"] for b in beats}
    rels = {b.get("rel") for b in beats if b.get("rel")}

    items: list[Preset] = []
    seen: set[str] = set()
    for i, d in enumerate(raw.get("presets") or []):
        where = f"presets[{i}]"
        if not isinstance(d, dict):
            _fail(f"{where} 不是映射")
        pid = (d.get("id") or "").strip()
        if not pid:
            _fail(f"{where} 缺少 id")
        if pid in seen:
            _fail(f"{where} id 重复：{pid}")
        seen.add(pid)

        act = (d.get("act") or "").strip()
        if act not in acts.ACTS:
            _fail(f"{pid}: act「{act}」不在枚举里（{', '.join(acts.ACTS)}）")

        rel = tuple(d.get("rel") or ())
        for r in rel:
            if rels and r not in rels:
                _fail(f"{pid}: rel「{r}」不是卡包里任何一拍的 rel（{', '.join(sorted(rels))}）")
        beat = tuple(d.get("beat") or ())
        for b in beat:
            if b not in beat_ids:
                _fail(f"{pid}: beat「{b}」不在卡包的拍里")

        lines = [str(x).strip() for x in (d.get("lines") or []) if str(x).strip()]
        if len(lines) < 3:
            _fail(f"{pid}: 变体少于 3 条（现有 {len(lines)}）；少于 3 条一定会被听出是念台词")
        for ln in lines:
            if len(ln) > 40:
                _fail(f"{pid}: 台词超过 40 字（{len(ln)}），预设只放短句，长句走生成：{ln[:12]}…")
            if "阿岸" in ln:
                _fail(f"{pid}: 台词里出现「阿岸」——卡包明确要求不主动说出这个名字")

        perf = d.get("perf") or {}
        bad = set(perf) - {"emotion", "action", "scene", "effect", "event"}
        if bad:
            _fail(f"{pid}: perf 里有未知字段 {sorted(bad)}")

        when = dict(d.get("when") or {})
        bad_when = set(when) - {"after", "not_after", "needs"}
        if bad_when:
            _fail(f"{pid}: when 里有未知字段 {sorted(bad_when)}")
        needs = when.get("needs")
        if needs is not None:
            if not isinstance(needs, list) or not needs:
                _fail(f"{pid}: when.needs 必须是非空列表（内容相关性闸门）")
            if not all(isinstance(x, str) and x.strip() for x in needs):
                _fail(f"{pid}: when.needs 里必须都是非空字符串")

        items.append(Preset(
            # rel / beat 留空 = 任意（在 match 里按 falsy 处理）
            id=pid, act=act, rel=rel,
            beat=beat,
            priority=int(d.get("priority", 50)),
            cooldown=int(d.get("cooldown", 0)),
            once=bool(d.get("once", False)),
            lines=tuple(lines),
            perf=dict(perf),
            when=when,
        ))
    return items


_CACHE: dict[str, Store] = {}


def load(card: dict | None = None, path: Path | None = None) -> Store:
    """加载（带缓存）。文件不存在时返回空库——预设是加速层，不该成为启动硬依赖。"""
    p = path or _resolve_path()
    key = str(p) if p else ""
    if key in _CACHE:
        return _CACHE[key]
    card = card or cards.load()
    if not p or not p.exists():
        store = Store([], p)
        _CACHE[key] = store
        return store
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if raw.get("schema") != "mira.presets/1":
        _fail(f"{p.name}: schema 必须是 mira.presets/1")
    store = Store(_load_items(raw, card), p)
    _CACHE[key] = store
    return store


def reload() -> Store:
    _CACHE.clear()
    return load()


def store() -> Store:
    return load()
