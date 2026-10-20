"""卡包加载器：把 cards/<id>/card.yaml 变成服务端与客户端共用的一份数据。

为什么要有这一层
- 角色、场景、节拍、指令枚举、TTS 语气、素材清单都在卡里，服务端只做编排：
  换角色 / 换场景 / 换素材 = 换一张卡，不改 Python 代码。
- 卡在启动时就校验（枚举、引用、资源路径），坏卡直接报错，而不是等到演示中途
  才发现「模型给了 action=look_door，客户端没有这张图」。
- 同一份数据三处使用：
    1) 提示词（character/scene/beats/boundaries）
    2) 角色指令枚举（directives -> 服务端白名单 + TTS 语气）
    3) 客户端素材清单（assets -> GET /api/card，客户端不再硬编码路径）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
CARDS_DIR = ROOT / "cards"
STATIC = ROOT / "app" / "static"
SCHEMA = "mira.card/1"


class CardError(RuntimeError):
    pass


def _require(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise CardError(f"卡包缺少字段 {where}.{key}")
    return d[key]


def _resolve_card_path(card_id: str | None = None) -> Path:
    card_id = card_id or os.environ.get("CARD_ID") or ""
    if card_id:
        p = CARDS_DIR / card_id / "card.yaml"
        if not p.exists():
            raise CardError(f"找不到卡包：{p}")
        return p
    found = sorted(CARDS_DIR.glob("*/card.yaml"))
    if not found:
        raise CardError(f"{CARDS_DIR} 下没有任何 card.yaml")
    return found[0]


def _check_assets(assets: dict, warnings: list[str]) -> None:
    """资源缺失只警告不报错：图还没生成时也要能启动（客户端会回退表情贴片）。"""
    for group, mapping in assets.items():
        if isinstance(mapping, str):
            mapping = {"_": mapping}
        if not isinstance(mapping, dict):
            continue
        for key, url in mapping.items():
            if not isinstance(url, str) or not url:
                continue  # 空字符串 = 这一项停用（例如 ambience_video 先不播）
            if not url.startswith("/static/"):
                warnings.append(f"assets.{group}.{key} 不是 /static/ 路径：{url}")
                continue
            if not (STATIC / url[len("/static/"):]).exists():
                warnings.append(f"assets.{group}.{key} 文件不存在：{url}")


def _validate(card: dict) -> list[str]:
    warnings: list[str] = []
    if card.get("schema") != SCHEMA:
        raise CardError(f"schema 必须是 {SCHEMA}，实际 {card.get('schema')!r}")

    character = _require(card, "character", "card")
    for k in ("name", "age", "job", "look", "temperament", "voice", "facts", "boundaries"):
        _require(character, k, "character")

    scene = _require(card, "scene", "card")
    for k in ("id", "name", "details", "camera_pov"):
        _require(scene, k, "scene")

    directives = _require(card, "directives", "card")
    enums = {}
    for key in ("emotion", "action", "scene", "effect", "event"):
        table = _require(directives, key, "directives")
        if not isinstance(table, dict) or not table:
            raise CardError(f"directives.{key} 必须是非空映射")
        enums[key] = set(table)
    default = _require(directives, "default", "directives")
    for key, allowed in enums.items():
        if default.get(key) not in allowed:
            raise CardError(f"directives.default.{key}={default.get(key)!r} 不在枚举 {sorted(allowed)} 内")
    if "none" not in enums["event"]:
        raise CardError("directives.event 必须包含 none")

    beats = _require(card, "beats", "card")
    if not isinstance(beats, list) or not beats:
        raise CardError("beats 必须是非空列表")
    seen = set()
    for i, b in enumerate(beats):
        where = f"beats[{i}]"
        for k in ("id", "goal", "allow", "forbid", "suggest", "mock", "directive", "advance"):
            _require(b, k, where)
        if b["id"] in seen:
            raise CardError(f"{where}.id 重复：{b['id']}")
        seen.add(b["id"])
        for key, allowed in enums.items():
            val = b["directive"].get(key)
            if val not in allowed:
                raise CardError(f"{where}.directive.{key}={val!r} 不在枚举内")
        adv = b["advance"]
        _require(adv, "max_turns", f"{where}.advance")
        if int(adv["max_turns"]) < 1:
            raise CardError(f"{where}.advance.max_turns 必须 >= 1")

    keywords = card.get("event_keywords") or {}
    for ev in keywords:
        if ev not in enums["event"]:
            raise CardError(f"event_keywords 里的 {ev!r} 不是合法 event 枚举")
    min_beat = card.get("event_min_beat") or {}
    for ev, idx in min_beat.items():
        if ev not in enums["event"]:
            raise CardError(f"event_min_beat 里的 {ev!r} 不是合法 event 枚举")
        if not (0 <= int(idx) < len(beats)):
            raise CardError(f"event_min_beat.{ev}={idx} 超出节拍范围")

    tts = _require(card, "tts", "card")
    tts_emotion = _require(tts, "emotion", "tts")
    for emo in enums["emotion"]:
        if emo not in tts_emotion:
            raise CardError(f"tts.emotion 缺少 {emo} 的语气配置")

    assets = _require(card, "assets", "card")
    _check_assets(assets, warnings)
    # 每个 emotion 都应该有贴片；没有的话客户端会回退，但值得提示
    for emo in enums["emotion"]:
        if emo not in (assets.get("char") or {}):
            warnings.append(f"assets.char 缺少情绪贴片 {emo}（客户端会回退到 neutral）")
    # 台词二次校正规则（问题 6 · 方案乙）：枚举写错 = 这条规则静默失效，
    # 所以在启动时就把坏规则顶出来，而不是等到演示时"动作没跟上台词"再猜。
    dfix = card.get("dialog_fix")
    if dfix is not None:
        if not isinstance(dfix, list):
            raise CardError("dialog_fix 必须是列表")
        for i, rule in enumerate(dfix):
            where = f"dialog_fix[{i}]"
            if not isinstance(rule, dict):
                raise CardError(f"{where} 必须是映射")
            for fld in ("emotion", "action"):
                val = rule.get(fld)
                if val is not None and val not in enums[fld]:
                    raise CardError(f"{where}.{fld}={val!r} 不在枚举 {sorted(enums[fld])} 内")
            if not (rule.get("reply_any") or rule.get("intent_any")):
                raise CardError(f"{where} 既没有 reply_any 也没有 intent_any（等于无条件命中，拒绝）")
    return warnings


def load(card_id: str | None = None) -> dict:
    path = _resolve_card_path(card_id)
    with path.open("r", encoding="utf-8") as fh:
        card = yaml.safe_load(fh)
    if not isinstance(card, dict):
        raise CardError(f"{path} 解析出来不是对象")
    card["_path"] = str(path)
    card["_warnings"] = _validate(card)
    return card


def manifest(card: dict) -> dict:
    """给客户端的最小快照：素材路径 + 指令可选项 + 句间停顿。"""
    directives = card["directives"]
    return {
        "schema": "mira.card-snapshot/1",
        "id": card["id"],
        "version": card["version"],
        "title": card.get("title"),
        "tagline": card.get("tagline"),
        "character": {
            "name": card["character"]["name"],
            "job": card["character"].get("job"),
            "age": card["character"].get("age"),
        },
        "scene": {
            "id": card["scene"]["id"],
            "name": card["scene"].get("name"),
            "time": card["scene"].get("time"),
            "weather": card["scene"].get("weather"),
        },
        "assets": card["assets"],
        "labels": {
            "emotion": directives["emotion"],
            "action": directives["action"],
            "scene": directives["scene"],
            "effect": directives["effect"],
            "event": directives["event"],
        },
        "tts": {"gaps_ms": (card.get("tts") or {}).get("gaps_ms") or {}},
        "beats": [{"id": b["id"], "title": b.get("title"), "suggest": b.get("suggest", [])}
                  for b in card["beats"]],
        "warnings": card.get("_warnings", []),
    }
