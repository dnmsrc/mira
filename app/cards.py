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

## 资源跟着卡走（v17，2026-10-21）

v16 及以前素材挂在 `app/static/assets/`，所有卡共用一棵树：加第二张卡时
`assets/char/neutral.webp` 直接撞名，`char/manifest.json` 也没有卡的身份。
v17 起**资源在卡里**，代码里不再出现任何素材目录：

    cards/<card_id>/card.yaml
    cards/<card_id>/presets.yaml
    cards/<card_id>/assets/{char,plate,ending,fx,insert,audio}/…

- 卡内 `assets:` 只写**卡内相对路径**（`char/neutral.webp`），不再写 `/static/...`；
  好处是整张卡（数据+美术）可以整体拷贝/删除/换盘，路径不绑在服务端 URL 上。
- 对外 URL 由服务端按发现的卡逐张挂载：`/card-assets/<card_id>/…`（app/main.py）。
- `assets.<x>` 到 `manifest()` 时统一变成**可直接用的绝对 URL**，客户端不再拼目录。
- 老布局（`/static/assets/...`）仍然认，只给一条 deprecation 警告，方便回滚：
  回滚 = 把 `cards/<id>/assets` 搬回 `app/static/assets` 并还原 card.yaml（见
  tools/migrate_cards_v2.py --rollback）。
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

from . import channels

ROOT = Path(__file__).resolve().parent.parent
CARDS_DIR = ROOT / "cards"
STATIC = ROOT / "app" / "static"
SCHEMA = "mira.card/2"

# 每张卡的素材 URL 前缀：/card-assets/<card_id>/<group>/<file>
# 为什么不用 /static/assets：那是**所有卡共用**的一棵树，第二张卡必然撞名；
# 而 /card-assets/<id> 让「一个前缀 = 一张卡」，浏览器缓存、回滚、删卡都是整块操作。
ASSET_URL_PREFIX = "/card-assets"
# v16 及以前的素材根（app/static/assets/）。只读兼容，不再作为新卡的写法。
LEGACY_STATIC_PREFIX = "/static/assets/"
# 客户端要的清单文件名（相对卡素材根）：char/manifest.json
CHAR_MANIFEST = "char/manifest.json"

# 特效原语：**封闭集合**。程序只承诺能渲染这六种，卡只能在里面拼装。
# 为什么封闭：开放自定义原语 = 把程序逻辑塞回卡里；而每次新增特效都要改 CSS+JS。
FX_PRIMITIVES = {"glow", "flash", "particle", "still", "shade", "audio"}
# 只允许铺满全屏的原语（其余必须给 zone）。这条是「全屏光带穿脸」事故的制度化防线。
FX_FULLSCREEN_OK = {"glow", "shade", "audio", "still"}


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
    # 默认卡 = 目录序第一张**可运行**的卡。走 discover() 而不是 glob：
    # cards/_template 这种下划线目录排序在 mira_cafe 前面，直接用 glob 会把它当默认卡。
    found = discover()
    if not found:
        raise CardError(f"{CARDS_DIR} 下没有任何可运行的卡包（有 card.yaml 的非下划线目录）")
    return found[0]["yaml"]


# ---------------------------------------------------------------- 卡目录 / 资源根


def _is_runnable(d: Path) -> bool:
    """能当卡用的是「有 card.yaml 且名字不像工具目录」的目录。

    下划线开头的是**不参与运行**的目录：`_template`（抄卡模板）、`_bak_*`（整卡备份）。
    这条规则让「加一张卡」不用改代码，也让模板/备份不会被误当成第二张卡挂上去。
    """
    return (d / "card.yaml").is_file() and not d.name.startswith(("_", "."))


def discover() -> list[dict]:
    """扫描 cards/ 下所有可运行的卡，返回定位信息（不解析 yaml，启动时便宜）。

    每项：{id, dir, yaml, assets_dir, assets_base, manifest_url}
    - assets_dir：这张卡的素材根（可能不存在，纯文字卡也允许）
    - assets_base：对外 URL 前缀，主程序按它逐张挂静态目录
    - manifest_url：客户端要 fetch 的清单地址（存在才有，否则 None）
    """
    out: list[dict] = []
    for d in sorted(CARDS_DIR.iterdir() if CARDS_DIR.is_dir() else []):
        if not d.is_dir() or not _is_runnable(d):
            continue
        out.append(_describe_dir(d))
    return out


def _describe_dir(d: Path) -> dict:
    cid = d.name
    assets_dir = d / "assets"
    base = f"{ASSET_URL_PREFIX}/{cid}"
    man = assets_dir / CHAR_MANIFEST
    return {
        "id": cid,
        "dir": d,
        "yaml": d / "card.yaml",
        "assets_dir": assets_dir,
        "assets_base": base,
        "manifest_url": f"{base}/{CHAR_MANIFEST}" if man.is_file() else None,
    }


def find(card_id: str) -> dict | None:
    """按 id 找一张可运行的卡（给静态挂载 / 清单路由用，不抛异常）。"""
    if not card_id or "/" in card_id or "\\" in card_id or card_id.startswith("."):
        return None
    d = CARDS_DIR / card_id
    if not d.is_dir() or not _is_runnable(d):
        return None
    return _describe_dir(d)


def _assets_dir_of(card: dict) -> Path | None:
    d = card.get("_assets_dir")
    return Path(d) if d else None


def asset_url(card: dict, value: Any) -> str:
    """把卡里写的资源引用变成**客户端可直接用的绝对 URL**。

    认三种写法：
      1. `char/neutral.webp`            → `/card-assets/<id>/char/neutral.webp`（v17 唯一推荐）
      2. `/card-assets/<id>/x.webp`     → 原样（已经是绝对路径；客户端/工具可以写死）
      3. `/static/assets/x.webp`        → 原样 + deprecation 警告（v16 老卡，回滚用）
    空字符串 / 非字符串：原样返回（`""` 表示这一项停用，见 _check_assets）。
    """
    if not isinstance(value, str) or not value:
        return value
    if value.startswith(("http://", "https://", "data:")):
        return value
    if value.startswith((f"{ASSET_URL_PREFIX}/", LEGACY_STATIC_PREFIX, "/media/")):
        return value
    rel = value.lstrip("./").replace("\\", "/")
    return f"{card['_assets_base']}/{rel}"


def _rel_of(card: dict, url: str) -> tuple[str | None, str]:
    """绝对 URL → (落在哪儿, 相对路径)。落在哪儿：'card' | 'static' | 'media' | None。

    校验只关心文件到底在不在：新布局在卡目录里，老布局在 app/static 里。
    """
    base = card["_assets_base"] + "/"
    if url.startswith(base):
        return "card", url[len(base):]
    if url.startswith(LEGACY_STATIC_PREFIX):
        return "static", url[len(LEGACY_STATIC_PREFIX):]
    if url.startswith("/media/"):
        return "media", url[len("/media/"):]
    return None, url


def _exists(card: dict, url: str) -> bool:
    where, rel = _rel_of(card, url)
    if where == "card":
        d = _assets_dir_of(card)
        return bool(d and (d / rel).exists())
    if where == "static":
        return (STATIC / rel).exists()
    if where == "media":
        return (ROOT / "media" / rel).exists()
    return False


def _check_assets(assets: dict, warnings: list[str], card: dict) -> None:
    """资源缺失只警告不报错：图还没生成时也要能启动（客户端会回退表情贴片）。

    卡内相对路径是 v17 的规范；`/static/assets/...` 仍然认，但会提示该迁进卡里。
    """
    for group, mapping in assets.items():
        if isinstance(mapping, str):
            mapping = {"_": mapping}
        if not isinstance(mapping, dict):
            continue
        for key, raw in mapping.items():
            if not isinstance(raw, str) or not raw:
                continue  # 空字符串 = 这一项停用（例如 ambience_video 先不播）
            if raw.startswith(LEGACY_STATIC_PREFIX):
                warnings.append(
                    f"assets.{group}.{key} 还是老布局 /static/：{raw}（v17 应写卡内相对路径，"
                    "见 tools/migrate_cards_v2.py）")
            elif raw.startswith("/"):
                warnings.append(f"assets.{group}.{key} 不是卡内相对路径：{raw}")
                continue
            if "/../" in f"/{raw}/" or raw.startswith("/"):
                warnings.append(f"assets.{group}.{key} 想跳出卡目录：{raw}")
                continue
            url = asset_url(card, raw)
            if not _exists(card, url):
                warnings.append(f"assets.{group}.{key} 文件不存在：{raw}（应位于 {url}）")



def _topic_from_ask(ask: object) -> str:
    """把卡包里的 ask（「问你在等人吗」）还原成可做子串匹配的话题词。

    beat_topic_hit / beat_jump 是拿 `topics` 在台词里做 `in` 判断的，而 ask 原文
    带「问/说」前缀和句末标点，永远匹配不上任何一句台词——派生时必须剥掉。
    """
    t = str(ask or "").strip()
    for verb in ("问对方", "问她", "问我", "问", "说"):
        if t.startswith(verb):
            t = t[len(verb):].strip()
            break
    return t.rstrip("。！？!?，,")


def _derive_beats(story: dict) -> list[dict]:
    """把 story.nodes 映射成旧的 beats 形状，供表演层（persona.perf/director）继续用。

    为什么不直接删 beats：表演层的通道仲裁、台词模板、建议句都按「一拍」工作，
    它们关心的是「这一刻她该是什么状态」，和「这一拍怎么跳转」是两件事。
    节点图负责剧情，beats 只剩它的旧角色：一份「这一拍长什么样」的表。
    """
    beats = []
    for n in story.get("nodes") or []:
        if n.get("clock") == "any":      # ending_leave 这类旁路节点不进主循环
            continue
        b = dict(n)
        adv = dict(b.get("advance") or {})
        adv.setdefault("max_turns", 3)
        adv.setdefault("min_turns", 1)
        # v14 卡包的节点没有 advance.topics（旧的线性七拍才有），而
        # beat_topic_hit / _beat_jump_topics 都读它 —— 不派生就等于恒为空，
        # 那两条判定全部失效（实测 19/19 个节点的 topics 都是 []）。
        # 口径和 persona.must_hit_phrases 一致：节点 ask 说的那件事，就是这一拍
        # 「该聊到的东西」。
        if not adv.get("topics"):
            adv["topics"] = [t for t in (_topic_from_ask(a) for a in n.get("ask") or [])
                             if len(t) >= 2]
        b["advance"] = adv
        beats.append(b)
    return beats


def _validate_story(card: dict, story: dict, enums: dict, warnings: list[str]) -> list[dict]:
    """节点图校验。坏卡在这里失败，而不是演到一半发现 goto 指向不存在的节点。"""
    nodes = story.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise CardError("story.nodes 必须是非空列表")
    ids = []
    for i, n in enumerate(nodes):
        where = f"story.nodes[{i}]"
        if not isinstance(n, dict):
            raise CardError(f"{where} 必须是映射")
        for k in ("id", "goal", "allow", "forbid", "suggest", "mock", "directive"):
            _require(n, k, where)
        if n["id"] in ids:
            raise CardError(f"{where}.id 重复：{n['id']}")
        ids.append(n["id"])
        # forbid_words（可选）：本拍不能说出口的具体词。导演层用它做两件事——
        # 拦掉越界的固定台词、进出口闸门的硬禁词表（见 director.apply_node）。
        fw = n.get("forbid_words")
        if fw is not None:
            if not isinstance(fw, list) or not all(isinstance(x, str) and x.strip() for x in fw):
                raise CardError(f"{where}.forbid_words 必须是字符串列表")
        for key, allowed in enums.items():
            val = (n["directive"] or {}).get(key)
            if val is not None and val not in allowed:
                raise CardError(f"{where}.directive.{key}={val!r} 不在枚举内")
    # 跳转引用
    idset = set(ids)
    for n in nodes:
        for g in (n.get("goto") or []):
            to = g.get("to")
            if to and to not in idset:
                raise CardError(f"story.{n['id']}.goto 指向不存在的节点 {to!r}")
    if story.get("start") not in idset:
        raise CardError(f"story.start={story.get('start')!r} 不是合法节点")
    if not any(n.get("ending") for n in nodes):
        raise CardError("story 里没有任何 ending 节点——故事将永远不结束")
    # 时钟
    clock = card.get("clock") or {}
    for k in ("start", "end"):
        if not isinstance(clock.get(k), str):
            raise CardError(f"clock.{k} 必须是 'HH:MM' 字符串")
    for f in (clock.get("forced") or []):
        node = f.get("node")
        if node != "__ending__" and node not in idset:
            raise CardError(f"clock.forced 指向不存在的节点 {node!r}")
    return nodes


def _validate_fx(card: dict, warnings: list[str]) -> None:
    """特效声明校验（8 条规则的前 6 条；zone 覆盖与 asset 存在在这里查）。"""
    fx = card.get("fx")
    if not fx:
        return  # 没声明特效的卡也能跑
    zones = ((card.get("scene") or {}).get("geometry") or {}).get("zones") or {}
    if not zones:
        raise CardError("声明了 fx 但 scene.geometry.zones 为空——特效不知道贴哪儿")
    instances = fx.get("instances") or {}
    if not isinstance(instances, dict):
        raise CardError("fx.instances 必须是映射")
    for name, spec in instances.items():
        where = f"fx.instances.{name}"
        if not isinstance(spec, dict):
            raise CardError(f"{where} 必须是映射")
        prim = spec.get("primitive")
        if prim not in FX_PRIMITIVES:
            raise CardError(f"{where}.primitive={prim!r} 不是合法原语 {sorted(FX_PRIMITIVES)}")
        zone = spec.get("zone")
        # audio 不落在画面上，不需要 zone；其余原语必须给（否则程序不知道贴哪儿）
        if prim != "audio":
            if not zone:
                raise CardError(f"{where} 缺少 zone")
            if zone not in zones:
                raise CardError(f"{where}.zone={zone!r} 未在 scene.geometry.zones 声明")
            if zone == "full" and prim not in FX_FULLSCREEN_OK:
                # 这条规则是上次「全屏光带穿过她的脸」的事故防线：粒子/闪光不许铺满全屏。
                raise CardError(f"{where}: primitive={prim} 不许用 zone=full（只允许 {sorted(FX_FULLSCREEN_OK)}）")
        asset = spec.get("asset")
        if asset:
            # fx 的 asset 写的是**相对素材根的路径**（不带扩展名）：`fx/softdot`、`ending/friend`。
            # 客户端按 `${assets_base}/${asset}.webp` 取图，这里只验证文件在不在。
            rel = str(asset) if str(asset).endswith(".webp") else f"{asset}.webp"
            if not _exists(card, asset_url(card, rel)):
                warnings.append(f"{where}.asset={asset} 找不到 {rel}")
    for i, cue in enumerate(fx.get("cues") or []):
        where = f"fx.cues[{i}]"
        if not isinstance(cue, dict):
            raise CardError(f"{where} 必须是映射")
        if not (cue.get("when") or {}):
            raise CardError(f"{where}.when 为空（会每轮都播）")
        for ref in (cue.get("play") or []):
            base = str(ref).split("@")[0]
            if base not in instances:
                raise CardError(f"{where}.play 引用了未声明的实例 {ref!r}")
        ch = (cue.get("when") or {}).get("chance")
        if ch is not None and not (0 < float(ch) <= 1):
            raise CardError(f"{where}.when.chance={ch} 必须在 (0,1]")


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

    # 表演通道声明（2026-10-21）：哪个动作占哪个通道、要不要用嘴、优先级、降级目标。
    # 这张表服务端（app/perf.py）与客户端（app/static/app.js）**共用**，写错一个字
    # 就是「她一边喝咖啡一边说话」——所以在启动时把矛盾顶出来，别等演示时看画面猜。
    ch_errors, ch_warnings = channels.validate(card)
    warnings.extend(ch_warnings)
    if ch_errors:
        raise CardError("表演通道声明有误：" + "；".join(ch_errors))

    # 剧情结构：v2 用 story.nodes（节点图，支持分支/结局），旧卡用线性 beats。
    story = card.get("story")
    if story is not None:
        _validate_story(card, story, enums, warnings)
        # 派生 beats 供表演层使用（通道仲裁 / 建议句 / 台词模板都按「一拍」工作）
        card["beats"] = _derive_beats(story)
    else:
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

    beats = card["beats"]
    _validate_fx(card, warnings)

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
    _check_assets(assets, warnings, card)
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
    # 资源根由**目录名**决定，不由卡里的 id 字段决定：目录才是磁盘上的真相，
    # id 只用于展示/日志。两者不一致时报错，否则 /card-assets/<id> 会挂错目录。
    info = _describe_dir(path.parent)
    if str(card.get("id") or "") != info["id"]:
        raise CardError(f"{path}: id={card.get('id')!r} 与目录名 {info['id']!r} 不一致"
                        "（静态前缀按目录名挂载，必须同名）")
    card["_dir"] = str(info["dir"])
    card["_assets_dir"] = str(info["assets_dir"])
    card["_assets_base"] = info["assets_base"]
    card["_manifest_url"] = info["manifest_url"]
    card["_warnings"] = _validate(card)
    return card


def manifest(card: dict) -> dict:
    """给客户端的最小快照：素材 URL + 指令可选项 + 句间停顿。

    v17：`assets` 下发的是**可直接用的绝对 URL**（`/card-assets/<id>/…`），并且
    额外给 `assets_base` / `assets_manifest` 两个定位字段 —— 客户端不再自己拼
    「素材在哪个目录」，换卡/挪目录只改服务端。
    """
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
        # 资源定位（v17）：基址 + 清单地址 + 绝对 URL 的素材表。
        "assets_base": card.get("_assets_base"),
        "assets_manifest": card.get("_manifest_url"),
        "assets": _abs_assets(card),
        "labels": {
            "emotion": directives["emotion"],
            "action": directives["action"],
            "scene": directives["scene"],
            "effect": directives["effect"],
            "event": directives["event"],
        },
        "tts": {"gaps_ms": (card.get("tts") or {}).get("gaps_ms") or {}},
        # 表演通道表：客户端拿它做同一套冲突判定（说话通道优先，占嘴的动作让它）。
        "stage": channels.snapshot(card),
        "beats": [{"id": b["id"], "title": b.get("title"), "suggest": b.get("suggest", [])}
                  for b in card["beats"]],
        # 故事节点图快照：客户端按 node 刷新建议句（不是按 beat 序号），
        # 并在收到 ending 事件时切结局态。leave_triggers 让「我想走了」在本地也能判。
        "story": _story_snapshot(card),
        # 特效：客户端拿 zones + instances 预载素材、建立原语渲染器。
        "fx": {
            "schema": (card.get("fx") or {}).get("schema"),
            "instances": (card.get("fx") or {}).get("instances") or {},
            "zones": (((card.get("scene") or {}).get("geometry") or {}).get("zones") or {}),
        },
        "warnings": card.get("_warnings", []),
    }


def _abs_assets(card: dict) -> dict:
    """把卡里 assets 的每一层相对路径换成绝对 URL（保持原形状，group 可以是字符串）。"""
    out: dict[str, Any] = {}
    for group, mapping in (card.get("assets") or {}).items():
        if isinstance(mapping, dict):
            out[group] = {k: asset_url(card, v) for k, v in mapping.items()}
        else:
            out[group] = asset_url(card, mapping)
    return out


def char_manifest(card: dict) -> dict | None:
    """读卡自己的 `assets/char/manifest.json`，并把里面的帧/姿势路径**绝对化**。

    为什么要服务端改写而不是直接静态下发：清单里的路径是相对卡素材根的
    （`neutral.webp`、`frames/a_00.webp`），客户端拿到 `base` 之后还得自己拼目录 ——
    那正是 v17 要去掉的东西。服务端知道这张卡的前缀，由它拼完，客户端直接用。

    只改写客户端真正会读的键（poses/char/pose/actions/sequences/transitions/breath），
    `source` / `registration` / `model` 这些**美术溯源信息原样保留**（不是 URL）。
    """
    path = _assets_dir_of(card)
    if not path:
        return None
    f = path / CHAR_MANIFEST
    if not f.is_file():
        return None
    import json   # 只有这一条路径需要 json：其余卡数据都是 yaml

    try:
        m = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict):
        return None
    m = copy.deepcopy(m)
    base = f"{card['_assets_base']}/char/"

    def one(v: Any) -> Any:
        """帧/文件 → 绝对 URL；已经是绝对路径或 URL 的不重复拼。"""
        if not isinstance(v, str) or not v:
            return v
        if v.startswith(("http://", "https://", "data:", "/")):
            return v
        return base + v.lstrip("./").replace("\\", "/")

    for sec in ("poses", "char", "pose"):
        block = m.get(sec)
        if isinstance(block, dict):
            for k, v in list(block.items()):
                if isinstance(v, str):
                    block[k] = one(v)
                elif isinstance(v, dict) and isinstance(v.get("file"), str):
                    v["file"] = one(v["file"])
    for sec in ("actions", "transitions", "breath"):
        block = m.get(sec)
        if isinstance(block, dict):
            for v in block.values():
                if isinstance(v, dict) and isinstance(v.get("frames"), list):
                    v["frames"] = [one(x) for x in v["frames"]]
    for v in (m.get("sequences") or []):
        if isinstance(v, dict) and isinstance(v.get("frames"), list):
            v["frames"] = [one(x) for x in v["frames"]]
    # 顶层「动作名 → 帧数组」的老写法也认
    for k, v in list(m.items()):
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            m[k] = [one(x) for x in v]
    m["base"] = base            # 覆盖清单里写死的旧 base（"assets/char/"）
    m["card_id"] = card["id"]   # 清单也有卡的身份了（v16 之前没有）
    return m


def _story_snapshot(card: dict) -> dict:
    story = card.get("story") or {}
    nodes = story.get("nodes") or []
    return {
        "schema": story.get("schema"),
        "start": story.get("start"),
        "clock": card.get("clock") or {},
        "leave_triggers": story.get("leave_triggers") or [],
        "nodes": [
            {
                "id": n["id"],
                "clock": n.get("clock"),
                "title": n.get("title"),
                "suggest": n.get("suggest") or [],
                "ending": n.get("ending"),
            }
            for n in nodes
        ],
    }
