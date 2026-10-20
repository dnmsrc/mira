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

from . import cards, decision

CARD = cards.load()
MANIFEST = cards.manifest(CARD)

CARD_ID = CARD["id"]
CARD_VERSION = CARD["version"]

# ---------------------------------------------------------------- 角色 / 场景

_CH = CARD["character"]
_SC = CARD["scene"]

VOICE_NAME = _CH["voice"]["name"]

CHARACTER = {
    "name": _CH["name"],
    "age": _CH["age"],
    "job": _CH["job"],
    "look": " ".join(str(_CH["look"]).split()),
    "temperament": _CH["temperament"],
    "voice": VOICE_NAME,
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

# 台词二次校正规则（问题 6 · 方案乙）：卡包里的数据，逻辑在 director.dialog_fix。
# 放这里而不是硬编码进 py，是为了"规则可写、可审、可回滚"——改完不用动代码。
DIALOG_FIX_RULES: list[dict] = list(CARD.get("dialog_fix") or [])


def _brief(say: str) -> str:
    """一条事实压成一句话：只留能被追问的那个具体物件/数字。

    为什么需要：4B 会把「说好十点半」编成「七点半末班车」，不是因为它想编，
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
         用户一进来就问照片，角色要么硬挡、要么直接念第 4 拍，中间全塌。

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


def suggestions(beat: int, anchors: tuple[str, ...] = ()) -> list[str]:
    """按当前拍 + 本轮话题生成 3 条建议（P0-3）。

    以前是卡包里每组固定 3 句，整场不变——用户实测「根本不知道说什么」，
    因为它既不对应当前聊到哪，也没有一条能把戏往前推。
    现在三条各有职责：
      1) 深入当前话题：优先和本轮锚点同字的那句（说明刚聊到这件事）
      2) 推进剧情：下一拍的建议句（问出来就会把剧情推到下一拍）
      3) 关心她 / 换角度：当前拍还没用到的句子
    数据仍全部来自卡包（BEATS[i].suggest），换卡即换建议，代码里不写死剧情。
    """
    n = len(BEATS)
    cur = [s for s in (BEATS[max(0, min(beat, n - 1))].get("suggest") or []) if s]
    nxt = [s for s in (BEATS[min(beat + 1, n - 1)].get("suggest") or []) if s]
    out: list[str] = []
    for s in cur:
        if s not in out and any(a and a in s for a in anchors):
            out.append(s)
            break
    if not out and cur:
        out.append(cur[0])
    for s in nxt:
        if len(out) < 3 and s not in out:
            out.append(s)
            break
    for s in cur + nxt:
        if len(out) < 3 and s not in out:
            out.append(s)
            break
    for f in ("你还好吗？", "……（不说话，看着她）", "这家店你常来吗？"):
        if len(out) < 3:
            out.append(f)
    return out[:3]


def beat_suggestions(beat: int) -> list[str]:
    beat = max(0, min(beat, len(BEATS) - 1))
    return list(BEATS[beat].get("suggest") or [])


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
     （「号码？……不给。」「找他？……不知道他在哪。」「照片……现在不拿出来。」）；
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
   「雨 / 灯 / 等 / 照片」这些词不要每轮都出现。
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
"""


def render_system_prompt(beat: int = 0, ledger: str = "（刚开始，对方什么都不知道）",
                         recent: str = "（还没有说过话）", plan_block: str = "") -> str:
    """拼本轮提示词：固定前缀（可命中 KV cache）+ 动态尾巴 + 导演层的本轮指令。

    plan_block 放在**最末尾**是有意的：4B 对末尾指令的服从率明显高于开头，
    而 prefix cache 只要求「前缀不变」，把每轮变的内容都压在尾段正好两全。
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
    )
    if plan_block:
        tail = tail + "\n" + plan_block.strip() + "\n"
    return head + tail


# ---------------------------------------------------------------- Mock 剧本

MOCK_SCRIPT = [{"beat": i, "reply": b["mock"], "directive": dict(b["directive"])}
               for i, b in enumerate(BEATS)]
MOCK_KEYWORD_EVENTS = EVENT_KEYWORDS
