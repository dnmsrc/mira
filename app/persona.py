"""卡包 → 服务端可用的数据结构（提示词 / 指令枚举 / 节拍推进 / Mock 剧本）。

这个模块不是「角色的真相」，它只是把 cards/*/card.yaml 读出来，变成
main.py 和 providers.py 直接能用的名字。改内容请改卡，不要改这里。

为什么提示词要这么写（这是本项目踩过的最大坑）：
- 上一版提示词把「1–2 句、不超过 45 字、情绪藏在动作里」写死了，4B 模型于是把每一轮
  都做成一句写景的短诗：用户问「你是在等谁」，它答「这雨，好像下得挺久的」——
  技术上合规，体验上是**答非所问**，于是整段互动像在念固定台词。
- 这一版把「先回答再延伸」放成第 1 条铁律，并且明确要求大部分回答平实具体
  （给物件、给时间、给数字），还加了「最近说过的话」做反重复。
- 节拍从「按轮数硬推进」改成「话题命中 / 事件触发 / 轮数上限」三选一，
  避免用户明明在聊别的，剧情却硬跳到下一拍。
"""

from __future__ import annotations

import os
import re

from . import cards, channels, decision

CARD = cards.load()
MANIFEST = cards.manifest(CARD)
# 表演通道仲裁器：perf 排手势、director 校正动作时都问它"此刻能不能做"。
# 规则全在卡包里（channels / face_map / actions），这里只是把卡变成可调用对象。
STAGE = channels.build(CARD)

CARD_ID = CARD["id"]
CARD_VERSION = CARD["version"]

# ---------------------------------------------------------------- 角色 / 场景

_CH = CARD["character"]
_SC = CARD["scene"]

# ------------------------------------------------ 音色（卡说走谁，代码就走谁）
# v10 的卡把「音色」从一个 edge 名字扩成三件事（三者缺一，换引擎/换参考音频在卡上
# 就没有落脚点——实测症状：卡里 voice.name=zh-CN-XiaoxiaoNeural，providers 的神经
# 路径根本不读这个字段，所以换参考 wav 一声都听不到）：
#     voice.engine  : neural | edge | auto   （MIRA_TTS_ENGINE 环境变量仍然优先）
#     voice.edge    : {name, rate, pitch, volume}   edge 链路用
#     voice.neural  : {ref, ref_sha1, prompt_text, speed}   神经侧车/缓存 key 用
# 向后兼容：v9 及以前的扁平写法 voice.name/rate/pitch/volume 仍然有效，等价于 voice.edge.*
_VOICE = _CH["voice"]
_V_EDGE = dict(_VOICE.get("edge") or {})
for _k in ("name", "rate", "pitch", "volume"):
    if _k not in _V_EDGE and _k in _VOICE:
        _V_EDGE[_k] = _VOICE[_k]
VOICE_EDGE = _V_EDGE
VOICE_NAME = str(_V_EDGE.get("name") or "")
VOICE_ENGINE = str(_VOICE.get("engine") or "auto").strip().lower()
VOICE_NEURAL = dict(_VOICE.get("neural") or {})

CHARACTER = {
    "name": _CH["name"],
    "age": _CH["age"],
    "job": _CH["job"],
    "look": " ".join(str(_CH["look"]).split()),
    "temperament": _CH["temperament"],
    "voice": VOICE_NAME,          # 兼容：各处仍把它当 edge 音色名用（health/调试面板）
    "voice_engine": VOICE_ENGINE,
    "voice_neural": VOICE_NEURAL,
    "secret": " ".join(str(_CH["secret"]).split()),
    "facts": _CH["facts"],
    "boundaries": _CH["boundaries"],
}

SCENE = {
    "id": _SC["id"],
    "name": _SC["name"],
    "time": _SC.get("time", ""),
    "weather": _SC.get("weather", ""),
    "camera_pov": " ".join(str(_SC.get("camera_pov", "")).split()),
    "details": _SC["details"],
    "place": f"{_SC.get('time', '')}，{_SC.get('weather', '')}",
    "ambience": _SC.get("ambience", {}),
}

BEATS = CARD["beats"]
# v14：卡里的 beats 是由 story.nodes 派生的（见 cards._derive_beats），
# 表演层仍按「第几拍」工作。这张表让 story 的 node_id 能同步回 beat 索引。
NODE_TO_BEAT = {b["id"]: i for i, b in enumerate(BEATS)}

# 节点全表（含 ending_leave 这类 clock: "any" 的旁路节点——它们不在 BEATS 里，
# 但「这一轮该建议玩家说什么」是按**节点**算的，不能只看拍序号）。
NODES: dict[str, dict] = {n["id"]: n for n in (CARD.get("story") or {}).get("nodes") or []}
# 主线节点顺序（去掉旁路节点）：给 --selftest / 观测面板按剧情顺序遍历用。
STORY_ORDER: list[str] = [n["id"] for n in (CARD.get("story") or {}).get("nodes") or []
                          if n.get("clock") != "any"]


def node_for(node_id: str, beat: int = 0) -> dict:
    """按节点 id 取节点数据；取不到才回落到「拍」的数据（老卡包只有 beats）。"""
    return NODES.get(node_id or "") or BEATS[max(0, min(beat, len(BEATS) - 1))]


def advance_keywords(node: dict) -> list[str]:
    """当前节点靠哪些**用户说的话**能提前跳转（goto[*].when.ask_any）。

    这是「快速对话的推进项」和「贴题证据」两个功能的同一个数据源：
    卡包作者已经写下了「说这句话就会推进剧情」，程序不需要再猜。
    """
    out: list[str] = []
    for g in node.get("goto") or []:
        for kw in ((g.get("when") or {}).get("ask_any") or []):
            kw = str(kw or "").strip()
            if kw and kw not in out:
                out.append(kw)
    return out


def _ask_to_line(ask: str) -> str:
    """把卡里的 ask 描述（「问你在等人吗」/「说外面在下雨」）还原成玩家会说的一句话。

    ask 的写法统一是「问…」或「说…」+ 一句自然中文，所以去掉前导动词就能直接
    当建议句用（问 → 问号结尾，说 → 句号结尾）。这比在代码里另写一套建议句可靠：
    换卡即换建议，且和节点语义天然一致。
    """
    t = str(ask or "").strip()
    for verb in ("问对方", "问她", "问我", "问", "说"):
        if t.startswith(verb):
            body = t[len(verb):].strip()
            if not body:
                break
            body = body.rstrip("。！？!?，,")
            return body + ("？" if verb.startswith("问") else "。")
    return t


def must_hit_phrases(node: dict, limit: int = 4) -> list[str]:
    """本轮「必须点到」的候选物：节点 ask 里的具体说法（给提示词 / 出口校验共用）。

    为什么从 ask 取而不是从 goal 取：goal 是作者写给导演看的一句话（「你松口承认
    在等人」），ask 是**玩家会问的那件事**（「问你在等人吗」），后者才是「听见了吗」
    的判据。去掉「问/说」和句末语气词之后剩下的，就是可机检的短语。
    """
    out: list[str] = []
    for a in node.get("ask") or []:
        line = _ask_to_line(str(a))
        line = line.rstrip("？?。！!，,")
        if line and line not in out:
            out.append(line)
    return out[:limit]

# 旁路节点（clock: "any"，如 ending_leave）按设计不进 BEATS——它不在线性推进里，
# 混进去会把「下一拍」的语义搞乱。但它一样要有「这一拍长什么样」：否则用户说
# 「我先走了」时 beat 停在上一节点，结局演的是上一句的动作和情绪。
# 按结局种类映射到同结局的主线节点，动作/情绪/建议全都复用那一套。
_last_beat = len(BEATS) - 1
for _n in (CARD.get("story") or {}).get("nodes") or []:
    if _n.get("id") in NODE_TO_BEAT:
        continue
    _kind = _n.get("ending")
    NODE_TO_BEAT[_n["id"]] = (
        NODE_TO_BEAT.get(f"ending_{_kind}", _last_beat) if _kind else _last_beat
    )

# 台词二次校正规则（问题 6 · 方案乙）：卡包里的数据，逻辑在 director.dialog_fix。
# 放这里而不是硬编码进 py，是为了"规则可写、可审、可回滚"——改完不用动代码。
DIALOG_FIX_RULES: list[dict] = list(CARD.get("dialog_fix") or [])


def _brief(say: str) -> str:
    """一条事实压成一句话：只留能被追问的那个具体物件/数字。

    为什么需要：4B 会把「说好八点半」编成「从早上七点开始等了四个半小时」，不是因为它想编，
    是因为它手里没有可用的具体锚点，只能从最近见过的词里抓一个凑。
    给一张短事实表，它就有东西可给；不给，它就只能造。
    """
    s = " ".join(str(say or "").split())
    for sep in ("；", "。"):
        if sep in s:
            s = s.split(sep)[0]
            break
    return s.strip()


FACT_BRIEFS = [_brief(f["say"]) for f in CHARACTER["facts"]]

# ---------------------------------------------------------------- 指令协议

_DIRECTIVES = CARD["directives"]
DIRECTIVE_SPEC = {k: _DIRECTIVES[k] for k in ("emotion", "action", "scene", "effect", "event")}
ALLOWED = {k: set(v) for k, v in DIRECTIVE_SPEC.items()}
DEFAULT_DIRECTIVE = dict(_DIRECTIVES["default"])

TTS_EMOTION = CARD["tts"]["emotion"]
TTS_SENTENCE = CARD["tts"].get("sentence_rules", {})
TTS_GAPS = CARD["tts"].get("gaps_ms", {})

EVENT_KEYWORDS = {k: list(v) for k, v in (CARD.get("event_keywords") or {}).items()}
EVENT_MIN_BEAT = {k: int(v) for k, v in (CARD.get("event_min_beat") or {}).items()}

# 陌生人状态的旁白（决策层全部候选未命中时用，见 app/selector.py 与 docs/PLAN_v4.md）。
# 内容在卡包里，代码只负责按轮次轮换——她"不接这句话"时说什么，是角色设定的一部分，
# 不是程序文案。缺这一段时给一句兜底，保证老卡包也能跑。
STRANGER_NARRATION = [str(x) for x in ((CARD.get("narration") or {}).get("stranger") or [])]
if not STRANGER_NARRATION:
    STRANGER_NARRATION = ["（她没有接这句话。）"]


def stranger_narration(seed: int = 0) -> str:
    """按轮次轮换旁白，避免连续几轮"她不接话"都用同一句。"""
    return STRANGER_NARRATION[seed % len(STRANGER_NARRATION)]

# 兼容旧格式：若模型把指令放在结尾，仍按这个分隔符切分
MARKER = "###DIRECTIVE###"

# 兼容旧调用点（新逻辑用 beat_advance）
TURNS_PER_BEAT = int(max((b.get("advance") or {}).get("max_turns", 3) for b in BEATS))


def sanitize_directive(raw: dict | None) -> tuple[dict, list[str]]:
    """收敛到合法枚举，返回 (directive, 修正说明)。"""
    out = dict(DEFAULT_DIRECTIVE)
    fixes: list[str] = []
    if not isinstance(raw, dict):
        return out, ["directive 不是对象，使用默认值"]
    for key, allowed in ALLOWED.items():
        val = raw.get(key)
        if isinstance(val, str) and val in allowed:
            out[key] = val
        elif val is None:
            pass
        else:
            fixes.append(f"{key}={val!r} 不在枚举内，回退 {out[key]}")
    return out, fixes


# ---------------------------------------------------------------- 节拍推进


def beat_topic_hit(beat: int, user_text: str = "", reply: str = "") -> bool:
    """这一轮有没有真的聊到本拍该聊的东西（用户说的或角色说的都算）。"""
    beat = max(0, min(beat, len(BEATS) - 1))
    topics = (BEATS[beat].get("advance") or {}).get("topics") or []
    if not topics:
        return False
    hay = f"{user_text or ''}|{reply or ''}"
    return any(str(t) in hay for t in topics)


def beat_jump(beat: int, user_text: str) -> int:
    """用户这句话把剧情推到哪一拍：**交给决策层做一道选择题**，不再数关键词。

    上一版这里是「把每拍的 topics 当子串在用户这句话里搜」：搜到就跳过去。
    两个问题都不是调参能解决的：
      1) 子串匹配没有语义。「我等了一个小时」里没有「等多久」，不跳；
         「你对咖啡怎么看」里有「咖啡」，跳到咖啡拍——全是运气。
      2) 它只能回答「跳不跳」，回答不了「跳到哪」。剧情骨架是线性的，
         用户一进来就问「她是不是不来了」，角色要么硬挡、要么直接念最后一拍，中间全塌。

    现在这一跳是一个**决策模型的选择题**（app/decision.py，形态同 TypeSafe 的 Jev）：
    选项 = 从当前拍到最后的每一拍，判据 = 那一拍玩家会说的话（卡包的 suggest），
    问「玩家刚说的这句话落在哪一拍」。选择题的答案永远在选项里、不需要解析，
    本地 ranker 拿不准时会去问免费云端模型复核。

    仍然保留的老行为：
      - 只有当前拍可选时不问（没得选）；
      - 决策层降级成 keyword 时，退回原来的 topics 子串匹配，行为与旧版一致；
      - **单调前进**：只会往前跳，不会把剧情拉回去。
    """
    if not user_text:
        return beat
    global LAST_BEAT_PICK
    beat = max(0, min(beat, len(BEATS) - 1))
    opts = beat_options(beat)
    if len(opts) <= 1:
        return beat
    try:
        p = decision.pick(
            "玩家的这句话，把剧情推到了第几拍？只是在搭话、或者没有推进，就选当前这一拍。",
            opts,
            state=(f"这是第 {beat + 1} 拍「{BEATS[beat].get('title', '')}」。"
                   f"玩家刚才说：{user_text}"),
            tau=BEAT_TAU,
        )
    except Exception as exc:  # noqa: BLE001
        # 决策层任何异常都不能让一轮对话挂掉，也不能静默假装成功
        LAST_BEAT_PICK = {"choice": "", "backend": "error", "err": f"{type(exc).__name__}: {exc}"}
        return _beat_jump_topics(beat, user_text)

    if p.backend == "keyword":
        # 没有模型也没有 key：退回旧的关键词行为，但如实记录 backend
        LAST_BEAT_PICK = p.as_dict()
        return _beat_jump_topics(beat, user_text)

    try:
        chosen = int(p.choice)
    except (TypeError, ValueError):
        LAST_BEAT_PICK = p.as_dict()
        return beat
    LAST_BEAT_PICK = p.as_dict()
    # 拿不准就留在原地：往前跳是不可逆的，比停在原地贵得多
    if chosen <= beat or p.confidence < BEAT_TAU:
        return beat
    # 2026-10-21：一次最多 +1 拍。ranker 在阈值附近会抖，「等谁啊？」实测 conf 0.845
    # 直接把剧情从第 1 拍跳到第 3 拍——中间那一拍的铺垫全没了，用户看到的是"剧情突然
    # 快进"。跳两拍的收益远小于它造成的断档，所以封顶 +1（真实推进靠轮次累积）。
    return min(chosen, beat + 1, len(BEATS) - 1)


# 最近一次「跳拍」的决策结果（/api/health 与日志用，别把它当成状态）
LAST_BEAT_PICK: dict = {}
# 跳拍阈值：比槽位门槛高。槽位选错只是这一句答歪，跳拍选错会把整场戏跳过去。
BEAT_TAU = float(os.environ.get("MIRA_BEAT_TAU", "0.80"))


def beat_options(beat: int) -> list[decision.Option]:
    """把「从当前拍到结尾」变成一道选择题的选项。

    判据（criteria）用卡包里那一拍的 suggest —— 那是**玩家在那一拍会说的话**，
    正好是 cross-encoder 能比的语义锚点；detail 给云端模型看，写这一拍想干什么。
    """
    out: list[decision.Option] = []
    for i in range(max(0, beat), len(BEATS)):
        b = BEATS[i]
        sug = "；".join(str(s) for s in (b.get("suggest") or []))
        topics = "、".join(str(t) for t in ((b.get("advance") or {}).get("topics") or []))
        # 判据 = 那一拍玩家会说的话 + 卡包里写死的触发词。
        # 为什么要拼上触发词：「等多久了？」和 admit 的 suggest「你在等谁？」语义上够近，
        # 但和当前拍（arrival）的「你一个人在这儿？」也很近，实测只差 0.8 的领先量，
        # 落在阈值外面。触发词是作者**明确写下来**的入口，把它塞进判据等于告诉打分器
        # 「这句话出现这些词就是这一拍」——这是数据变强，不是把阈值调低。
        criteria = "；".join(x for x in (sug, topics) if x)
        out.append(decision.Option(
            id=str(i),
            label=f"{i + 1}. {b.get('title', '')}",
            criteria=criteria or " ".join(str(b.get("goal") or "").split()),
            detail=("目的：" + " ".join(str(b.get("goal") or "").split())
                    + "｜可以松口：" + " ".join(str(b.get("allow") or "").split())
                    + (f"｜触发词：{topics}" if topics else "")),
        ))
    return out


def _beat_jump_topics(beat: int, user_text: str) -> int:
    """旧版的关键词跳拍（只在决策层降级时用）。"""
    best = beat
    for i in range(beat + 1, len(BEATS)):
        topics = (BEATS[i].get("advance") or {}).get("topics") or []
        if any(str(t) in user_text for t in topics):
            best = i
    return best


def beat_advance(beat: int, turns_in_beat: int, hits: int = 0, event: str = "none") -> bool:
    """节拍是否推进：视觉事件 / 话题命中够多次 + 最少轮数 / 轮数上限。

    之前是「聊满 3 轮就跳」，用户随口问一句「你喜欢喝什么咖啡」也会把剧情推走，
    看起来像有个隐藏剧本在跑。现在要求「真的聊到了这一拍的核心话题至少两次」，
    同时用 max_turns 兜底，保证 3–5 分钟里剧情一定会往前走。
    """
    if beat >= len(BEATS) - 1:
        return False
    if event and event != "none":
        return True
    adv = BEATS[beat].get("advance") or {}
    max_turns = int(adv.get("max_turns", 4))
    min_turns = int(adv.get("min_turns", 2))
    min_hits = int(adv.get("min_hits", 2))
    if turns_in_beat >= max_turns:
        return True
    if turns_in_beat < min_turns:
        return False
    return hits >= min_hits


def _clean(line: object) -> str:
    return " ".join(str(line or "").split()).strip()


# 内容二元组用的虚词表：两个字都是虚词时不算「内容」。只用来算「这句话和那句话
# 是不是在说同一件事」，不追求分词精度——漏判只会让建议里多一句安全的话。
_STOP_CHARS = set(
    "的了着过是在有和与就不都也还又很太更最我你他她它们这那个么吗呢吧啊呀哦嗯额呃唔诶"
    "一二三四五六七八九十一上下里外去来说做要会能可对把被给让从到为以而且但所以如果"
    "将已经没多少点时候现在知道觉得可能应该什么怎么为什么一样这样那样就是不是")


def _tokens(text: str) -> set[str]:
    """中文内容二元组（虚词二元组丢掉）。给「相关/不相关」做粗判，纯函数。"""
    out: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]+", text or ""):
        for i in range(len(run) - 1):
            a, b = run[i], run[i + 1]
            if a in _STOP_CHARS and b in _STOP_CHARS:
                continue
            out.add(run[i:i + 2])
    return out


def _synth_ask_line(kw: str) -> str:
    """兜底：用一个 goto 触发词造一句最短的自然中文（真的包含该词）。

    正常路径用不到它——卡包里每个 ask_any 触发词都能在目标节点的 suggest 里找到
    对应的一句玩家台词（作者已经写好）。这里只是保证「推进项永远存在且真的能跳」，
    不给作者留"忘写 suggest 就推不动剧情"的坑。
    """
    kw = _clean(kw)
    if not kw:
        return ""
    if kw.startswith("你"):
        return f"{kw}？"
    return f"你{kw}？"


def _advance_suggestion(node: dict) -> tuple[str, str]:
    """「推进剧情」那一条：点下去真的能把当前节点推到下一个节点。

    数据全部来自卡包：goto[*].when.ask_any 是作者写下的**剧情加速关键词**，
    目标节点的 suggest 是"玩家在那个节点会说的话"。优先用作者原句，
    因为它是自然中文、且天然包含触发词；实在没有才用关键词现造一句。
    返回 (建议句, 人话说明)。
    """
    for g in node.get("goto") or []:
        kws = [_clean(k) for k in ((g.get("when") or {}).get("ask_any") or []) if _clean(k)]
        if not kws:
            continue
        target = NODES.get(_clean(g.get("to"))) or {}
        for s in (_clean(x) for x in target.get("suggest") or []):
            if not s or s.startswith(("（", "(")):
                continue          # 「（别说话，等她）」是选项不是台词，不能当推进项
            hit = next((k for k in kws if k in s), "")
            if hit:
                return s, f"推进到 {target.get('id')}（含触发词「{hit}」）"
        for a in (_ask_to_line(x) for x in target.get("ask") or []):
            hit = next((k for k in kws if k in a), "")
            if a and hit:
                return a, f"推进到 {target.get('id')}（含触发词「{hit}」）"
        line = _synth_ask_line(kws[0])
        if line:
            return line, f"推进到 {target.get('id') or '下一节点'}（触发词「{kws[0]}」）"
    # 没有 ask_any 分支：这个节点靠轮次/时钟自动推进，所以给「下一节点玩家会说的话」
    for g in node.get("goto") or []:
        target = NODES.get(_clean(g.get("to"))) or {}
        for s in (_clean(x) for x in target.get("suggest") or []):
            if s and not s.startswith(("（", "(")):
                return s, f"推进到 {target.get('id')}（按轮次推进，先说到那边的事）"
    return "", ""


def _ending_suggestions(node: dict, turn: int) -> list[str]:
    """结局节点的三条：都是「道别/收尾」类，不能出现推进剧情的问句。"""
    sugs = [_clean(x) for x in node.get("suggest") or [] if _clean(x)]
    out: list[str] = []
    pool: list[str] = list(sugs)
    farewell = ["路上小心。", "那我先走了。", "再见。"]
    if node.get("id") == "ending_leave":
        out.append("我先走了。")          # 用户主动要走：第一条点下去就是结局
        pool = [p for p in pool if "先走" not in p]
        farewell = [p for p in farewell if "先走" not in p]
    pool = pool + [f for f in farewell if f not in pool]
    for i in range(len(pool)):
        cand = pool[(int(turn) + i) % len(pool)]
        if cand and cand not in out:
            out.append(cand)
        if len(out) >= 3:
            break
    return out[:3]


def suggestions_for_node(story_state: object = None, node_id: str = "",
                         turn_in_node: int = 0, last_user: str = "",
                         last_reply: str = "") -> list[str]:
    """快速对话的三条建议：按**当前故事节点**实时生成，且每轮都不一样。

    为什么重写（用户问题 6：「快速对话不应该是几个固定的选项，它应该根据每轮对话
    与当前所属故事节点实时改变，有着推动剧情发展的功能」）：
    上一版按「第几拍」从 BEATS[i].suggest 取固定三句，同一节点连坐几轮给的是
    一模一样的三条，而且没有一条能把戏往前推——用户点了两句就得自己打字。

    现在三条各有职责（数据全在卡包 story.nodes 里，换卡即换建议，代码不写死剧情）：
      1) 接住当前话题 —— 节点 suggest 里和「他上一句 / 她上一句」最相关的那句；
      2) **推进剧情**   —— 当前节点 goto 的 ask_any 触发词 → 目标节点 suggest 里
                          天然包含触发词的那句（点下去真的会跳节点）；
      3) 关心 / 换角度 —— 本节点还没用过的 suggest（用完就用 ask 还原的玩家问句）。
    三条按 turn_in_node 轮换，同一节点连着两轮不会给出一模一样的三条。

    纯函数：不调模型、不读文件、0 延迟；同一个输入永远得到同样的三条（可单测）。
    story_state 只用来在 node_id 缺省时补一个当前节点（读属性，不修改它）。
    """
    nid = _clean(node_id) or _clean(getattr(story_state, "node_id", ""))
    if not nid:
        nid = STORY_ORDER[0] if STORY_ORDER else (BEATS[0]["id"] if BEATS else "")
    node = NODES.get(nid)
    if node is None:
        # 老卡包（只有 beats）：退回拍数据，形状兼容
        node = BEATS[max(0, min(int(turn_in_node or 0), max(0, len(BEATS) - 1)))]
    turn = max(0, int(turn_in_node or 0))
    if node.get("ending"):
        return _ending_suggestions(node, turn)
    sugs = [_clean(x) for x in node.get("suggest") or [] if _clean(x)]
    out: list[str] = []

    # 2) 先定推进项：它决定了另外两条不能重复哪一句
    advance, advance_why = _advance_suggestion(node)
    if advance:
        out.append(advance)

    # 1) 接住当前话题：和他刚说的 / 她刚说的最相关的一句
    hay = " ".join(x for x in (_clean(last_user), _clean(last_reply)) if x)
    toks = _tokens(hay)
    topic = ""
    if toks:
        best_score = 0
        for s in sugs:
            if s in out:
                continue
            sc = len(toks & _tokens(s))
            if sc > best_score:
                topic, best_score = s, sc
    if not topic:
        pool = [s for s in sugs if s not in out]
        if pool:
            topic = pool[turn % len(pool)]
    if topic and topic not in out:
        out.insert(0, topic)

    # 3) 关心 / 换角度：本节点还没用过的 suggest；用完了就用 ask 还原的玩家问句
    care_pool = [s for s in sugs if s not in out]
    if not care_pool:
        care_pool = [_ask_to_line(a) for a in node.get("ask") or []
                     if _ask_to_line(a) and _ask_to_line(a) not in out]
    if care_pool:
        out.append(_sug_pick(care_pool, turn))

    # 节点自带数据不够三条时：用后面节点作者写的玩家台词补（仍然随节点变化）
    if len(out) < 3:
        for later in STORY_ORDER[STORY_ORDER.index(nid) + 1:] if nid in STORY_ORDER else []:
            for s in (_clean(x) for x in (NODES.get(later) or {}).get("suggest") or []):
                if s and s not in out and len(out) < 3:
                    out.append(s)
            if len(out) >= 3:
                break
    for fb in ("你还好吗？", "……（不说话，看着她）", "这家店你常来吗？"):
        if len(out) < 3 and fb not in out:
            out.append(fb)
    return out[:3]


def _sug_pick(pool: list[str], turn: int) -> str:
    """按轮次轮换着取一句：同一节点连着两轮不会给同一句。"""
    if not pool:
        return ""
    return pool[int(turn) % len(pool)]


def beat_goal(beat: int) -> dict:
    beat = max(0, min(beat, len(BEATS) - 1))
    return BEATS[beat]


# ---------------------------------------------------------------- 提示词

_SYSTEM = """你在一场实时语音对话里扮演 {name}——{age} 岁的{job}。不是在写小说，是在说话。

【你是谁】
外形：{look}
性格：{temperament}
你心里的事（这是动机，不是台词，绝不主动说破）：{secret}

【你此刻在哪】
{place}
（注意：雨**已经停了**，外面不会正在下雨；只有屋檐在滴水、地面积水在反光。）
店家快打烊了：{scene_details}
你坐在靠窗的位子，对方（一个刚躲雨进来的客人）坐在你对面。

【你今晚记得的事——时间、物件、数字只能从这里取，表里没有就说记不清，绝不现场编】
{facts}

【怎么说话——最重要的一节】
1. 先回答，再延伸。对方问什么，第一句必须**针对那个问题**做出反应：
   - 知道的就正面回答，给具体的东西（物件、时间、数字）；
   - 不想说、不能说的，就**点着那件事**挡回去：先提到他问的东西，再说挡的话
     （「号码？……不给。」「她在哪？……我不知道，联系不上。」「手机……没电了，从七点开始。」）；
   - **挡也要挡对题。** 不允许只甩一句「不是。」「不说这个。」，更不允许用一句
     天气、景物或比喻来代替回答——在用户听来那就是没听见他说话。
     上一版就是在这里坏的：对方问「你在等谁」，她答「这雨好像下得挺久的」——
     技术上守住了秘密，体验上是答非所问。
2. 大部分回答要平实、具体：给物件、给时间、给数字。**数字和物件只能从上面那张
   「你今晚记得的事」里取**；表里没有的，就说记不清，不要现场编一个新的时间、地点或去处。
   一轮里最多一个比喻。被问到角色卡没写的事（住哪、怎么回去、怕不怕、要号码），
   也照常给一个具体反应：能说的就说，不能说的按第 1 条挡在对题上。
3. 1～2 句，一般 15～50 字。允许口语碎片：“嗯。”“也不是。”“……你坐吧。”
   不要写成三个小短句的排比——那样既不像说话，也会让语音一段一段地断。
4. **只写你会说出口的话。** 禁止旁白，禁止括号或星号写的动作（不要出现"（低头）""（笑）"这类内容），
   禁止 emoji，禁止复述对方的话，禁止总结，禁止客服口吻，禁止把规则念出来。
   要做动作就交给第一行的 action 字段，正文里一个字都不要提。
5. **记住对方说过的事。** 他问"你记得我叫什么""我刚才说什么了"，
   先用具体信息正面回答（名字、时间、他说过的事）；答不上来就说记不清，
   绝对不要用"你刚进来吧"这种空话糊过去，也不要编。
6. 你最近说过的话在下面【导演提示】里。再被问到同一件事必须换一种说法，
   每轮给一个**新的具体信息**（新物件、时间、动作或态度）。
   「雨 / 灯 / 等 / 小雪 / 手机没电」这些词不要每轮都出现。
7. 对方聊和你无关的事（手机没电、加班、堵车）就正常接，不要硬拐回你的心事。
8. 反问**只在【导演提示】明确要求那一轮才做**（那里会直接给你要问的话头）。
   没被要求就不要用问句结尾——答完就收住。实测 4B 最爱在答完之后补半句
   「……这店的灯，还亮着吗？」把话撑长，听起来既跑题又像在盘问对方。
   被要求反问时要贴着他刚说的话（他说要上班 → 问他几点下、累不累），不要凭空问一句。
9. 情绪放在语气和动作里，不要解释情绪（别说「我很担心」）。
10. **对面这个人不是你等的人。** 你等的人还没来，你也从不跟这位客人解释你在等谁。
    不要把对方当成那个人：不要问他为什么才来、为什么迟到，更不要说「你来得挺晚」
    这种把他当成约好的人的句子。「跟那个人说好十点半」是你和那个人的事，跟这位客人无关。
11. 对方只打招呼（你好／嗨／在吗／晚上好）时，就回一句招呼或让他坐：
    「嗯。」「你好。」「……坐吧。」「这么湿，先坐吧。」——不要拿天气当回答，不要报时间，
    不要反问他，一句就够。
12. **每一轮都要接住对方这句话。** 【导演提示】最后一行会写出「这一轮要回应的话题」，
    那是给你看的提示，**绝对不要照着念、也不要把它列出来**。要用你自己的话给出一个
    具体反应（时间/物件/人/动作），否则在观众听来就是你根本没听见他说话。
    宁可短，也不许用「嗯」「是啊」「我不知道」这种空话把这一轮糊过去——那是答非所问。
    也不能用「我没听清」「玻璃太厚」这类现场编的理由代替回答。

【输出格式】
第一行必须是 JSON 对象（不要代码块、不要任何前缀），换行之后才是你要说的话。
**JSON 那一行之后必须真的有台词。只输出一行 JSON 就结束，等于这一轮你什么都没说，算失败。**
字段与取值：
  emotion: {emotions}
  action: {actions}
  scene: {scenes}
  effect: {effects}
  event: {events}
只有对方这句话真的触发了视觉事件，event 才不是 none，否则一律 none。

示例（只学格式，不要照抄内容）：
{{"emotion":"smile","action":"lean_in","scene":"cafe_night","effect":"rain","event":"none"}}
嗯。……坐吧。
"""

# 动态段全部放在**最后**：前面那一大段是固定前缀，llama.cpp / Strata 都能命中前缀缓存，
# 每轮只重新预填充这一小段——首包延迟由此从 2s+ 压到 1s 以内（Strata 实测）。
_DYNAMIC = """
【导演提示 · 不要念出来，只用来决定你怎么回应】
第 {beat_no}/{beat_total} 拍 · {beat_title}
动机：{goal}
这一拍可以松口的：{allow}
这一拍绝对不要说的：{forbid}
已经说过 / 对方已经知道：{ledger}
你最近说过的台词（不要重复，换个说法）：{recent}
这一轮要回应的话题（用你自己的话，别念这一行）：{must_hit}
"""


def render_system_prompt(beat: int = 0, ledger: str = "（刚开始，对方什么都不知道）",
                         recent: str = "（还没有说过话）", plan_block: str = "",
                         must_hit: str = "") -> str:
    """拼本轮提示词：固定前缀（可命中 KV cache）+ 动态尾巴 + 导演层的本轮指令。

    plan_block 放在**最末尾**是有意的：4B 对末尾指令的服从率明显高于开头，
    而 prefix cache 只要求「前缀不变」，把每轮变的内容都压在尾段正好两全。

    must_hit（问题 4）：当前节点 ask 锚点里的具体物，写在动态段最后一行——
    它是「这一轮要回应的话题」的机检口径，和 main.py 的 topic_gate 用同一份数据。
    空字符串时写「（按他刚问的那件事答）」，保证提示词形状稳定（前缀缓存不受影响）。
    """
    beat = max(0, min(beat, len(BEATS) - 1))
    b = BEATS[beat]
    head = _SYSTEM.format(
        name=CHARACTER["name"],
        age=CHARACTER["age"],
        job=CHARACTER["job"],
        look=CHARACTER["look"],
        temperament=CHARACTER["temperament"],
        secret=CHARACTER["secret"],
        place=SCENE["place"],
        scene_details="；".join(SCENE["details"][:5]),
        facts="\n".join(f"- {x}" for x in FACT_BRIEFS),
        emotions=" / ".join(DIRECTIVE_SPEC["emotion"]),
        actions=" / ".join(DIRECTIVE_SPEC["action"]),
        scenes=" / ".join(DIRECTIVE_SPEC["scene"]),
        effects=" / ".join(DIRECTIVE_SPEC["effect"]),
        events=" / ".join(DIRECTIVE_SPEC["event"]),
    )
    tail = _DYNAMIC.format(
        beat_no=beat + 1,
        beat_total=len(BEATS),
        beat_title=b.get("title") or b["id"],
        goal=b["goal"],
        allow=b["allow"],
        forbid=b["forbid"],
        ledger=ledger,
        recent=recent,
        must_hit=must_hit or "（按他刚问的那件事答，把他的原话里的一个词说出来）",
    )
    if plan_block:
        tail = tail + "\n" + plan_block.strip() + "\n"
    return head + tail


# ---------------------------------------------------------------- Mock 剧本

MOCK_SCRIPT = [{"beat": i, "reply": b["mock"], "directive": dict(b["directive"])}
               for i, b in enumerate(BEATS)]
MOCK_KEYWORD_EVENTS = EVENT_KEYWORDS
