"""导演层：把「这一轮该怎么答」从模型脑子里搬到确定性的代码里。

为什么需要它（这是本项目对「小模型够不够用」的回答）
- 4B 的问题不是不会说话，是**同时干太多活**：读 1.2k 字角色卡、判情绪/事件、
  想起你刚说的名字、守住「别写旁白」……几个目标一抢注意力，它最先丢掉的
  永远是最不显眼的那个——「回答对方真正问的问题」。
- 大模型看起来「会聊天」，很大一部分是它在做隐式的对话规划（先回答→再延伸、
  该给具体信息还是该挡、这轮能不能松口）。这个规划在本场景里**是可以确定化的**：
  问句类型只有几种，该说什么事实在会话状态里查得到，所以不该交给模型的概率去赌。
- 这一层做四件事：
    1) 判「用户在问什么」——不只是意图类别，还要判**问的是哪件事（槽位）**
    2) 生成一份「本轮指令」，塞到提示词**最末尾**（模型对末尾指令的服从率
       明显高于开头，4B 尤其明显）
    3) 给这一轮**要传达的事实**（content）。没有 content 的轮次，4B 只能现场编，
       或者拿一句景物描写糊过去——上一版「答非所问」的主因就在这里。
    4) 生成一份**可机检**的验收标准：必须点到的topic词 / 硬禁词 / 字数上限，
       回复一落地就能验；验不过的首句会被扣住重写（见 app/main.py 的首句闸门）
- 结果：小模型只负责「把已经决定的内容说得好听」，这是它最擅长的部分。
  这就是为什么这个场景**不需要** 25B：需要规划的部分不归它管。

这一版改了什么（答非所问的结构性修复）
- 旧版 `_INTENT_RULES` 只有 7 个意图，且 **意图→content 是一对一静态映射**：
  「为什么这么晚还在」和「你为什么不去找他」都落到 `why`，拿到同一句
  「约好十点半，他一直没来」——后一个问题等于没回答。这不是模型弱，是结构上
  就没把「问的是哪件事」表示出来。现在先判**槽位**（回去方式/联系方式/照片/
  咖啡/怕不怕/为什么不去找……），每个槽位有自己的 content 和 anchors。
- 旧版 default 轮只给一句元指令「先正面回应对方这句话」，**没有 content**。
  4B 拿不到可说的东西，只能从最近见过的词里抓一个凑（实测会编出
  「司机在后门等，十一点半到站」这种）。现在 default 分成「提问」和「闲聊接话」
  两类，都生成 content。
- 旧版只有 critical（recall/self）轮做机检。现在**所有提问轮**都带 anchors，
  由 main.py 的首句闸门统一校验：答不到那件事上就重写一次，再不行用确定性兜底。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ------------------------------------------------------------------ 问句规则
#
# 判的是「这句话问的是哪件事」，不是「情绪类别」。顺序即优先级：
# 越具体的问题越靠前，因为一句里常常同时命中几个模式（「你为什么不去找他」
# 同时有「为什么」和「找他」；先判 why 就会答成「为什么在这里等」）。
_INTENT_RULES: list[tuple[str, re.Pattern]] = [
    # 「我听不懂 / 什么意思 / 再说一遍」：这不是在问店里的事，是在**对你上一句**提意见。
    # 以前落到兜底 question 槽，抽出的"话题词"是「听不懂」，4B 就答成
    # 「你是不是刚下过雨？地面还滑呢。」——用户实测截图里最刺眼的一句。
    # 这类话只有一种正确反应：别怪他没听清，把上一句换个直白的说法重说。
    ("clarify", re.compile(r"听不懂|没听懂|听不明白|没明白|不明白|什么意思|啥意思|"
                           r"你说什么|说的什么|再说一(遍|次)|重说|说清楚点|没懂|不懂")),
    ("recall", re.compile(r"(还)?记得|我叫什么|我叫啥|我叫谁|我是谁|我刚才(说|讲)|"
                          r"我说过什么|我说了什么|你忘了吗")),
    ("self", re.compile(r"你叫什么|你是谁|你叫啥|怎么称呼|你的名字|你名字")),
    # 只在他直接问「他叫什么」时允许说一次名字（卡包 boundary 的要求）
    ("name", re.compile(r"他叫什么|他的名字|那个人叫什么|等的人叫什么")),
    # 追问「后来」：问的是「他有没有再出现 / 有没有消息」，不是「要不要给你号码」。
    # 必须排在 request / photo 前面——实测「他没再**联系你**？」会被 request 里裸的
    # 「联系你」吃掉，答成「号码不给」（chat_probe 第 7 句就是这么答非所问的）。
    ("followup", re.compile(r"后来|然后呢|之后呢|再联系|有消息|没消息|再出现")),
    # 「打算 / 怎么办 / 寄不寄」：问的是**决定**，不是把照片再描述一遍。排在 photo 前面，
    # 否则「照片要寄出去吗」只因为句子里有「照片」就落到 photo（chat_probe 第 9 句）。
    ("choice", re.compile(r"打算|怎么办|接下来|以后(怎么|呢)|寄出|寄给|要不要寄|该不该")),
    ("photo", re.compile(r"照片|相片|相册|看看.*(照|相)|那张")),
    # 提议：接受/拒绝 + 理由。必须排在 request 前面，否则「帮你打个电话」
    # 会因为句子里有「电话」被当成「索取号码」（实测就踩过这个坑）
    ("offer", re.compile(r"我帮你|帮你打|要不要(我)?帮|需要我|要不我|我替你|我给他打")),
    # 索取联系方式：允许拒绝，但必须拒绝在「号码/手机/电话」这件事上。
    # 只认真正的索取框架——删掉裸的「联系你」「留个」：那是陈述/追问，不是索取。
    ("request", re.compile(r"号码|手机号|微信号|加个好友|留个微信|联系方式|怎么联系|"
                           r"你的电话|给我个电话")),
    ("why_not_go", re.compile(r"(为什么|为啥|怎么)(不|没)(去)?(找|联系)|"
                              r"去找他|找过他|有没有找|怎么不找|不去找他|没去找")),
    ("doubt", re.compile(r"骗|糊弄|真的假的|是不是真的|不信|说实话")),
    ("fear", re.compile(r"害怕|怕")),
    # 对方在**请她**吃喝（「你要不要喝点热水」）。以前落到泛问句槽，答成
    # 「黑咖啡不加糖，你要是想喝热的，我这儿有杯温着的」——把她被请的水反手推回去，
    # 方向答反（独立盲评第三段第 6 句）。必须排在 coffee 前面：句子里常带「喝」。
    ("given_offer", re.compile(r"(喝|吃|来)(点|杯|口|份)?(热水|热茶|水|东西|点|汤|药)"
                               r"|我(给|帮)你(倒|拿|买|端|点)|要不要我(给|帮)")),
    # 「这杯咖啡是你自己做的吗」问的是**谁做的**，不是「好喝吗」。只按实体词命中
    # coffee 槽会答成「黑咖啡，不加糖」——实测就是这样答非所问的（dialogue_sanity 第 5 句）。
    ("coffee_who", re.compile(r"(咖啡|这杯|那杯).{0,8}(谁做|谁冲|谁泡|谁煮|你自己做|你做的|"
                              r"是你做|你冲的|你泡的)|(谁做|谁冲|谁泡|谁煮).{0,6}(咖啡|这杯|那杯)")),
    ("coffee", re.compile(r"咖啡|好喝|喝什么|苦不苦|加糖|糖")),
    # 「你在这儿上班吗」问的是身份，不是「在等谁」。以前落到兜底 question，
    # 抽出来的话题词是「儿上班」这种碎片，4B 为了凑词会编出
    # 「这店是她打烊，不是我上班」（dialogue_sanity 第 4 句）。
    ("work_here", re.compile(r"(你|您)(是|在)(这儿|这店|这里|这家(店)?|这个店).{0,4}"
                             r"(上班|工作|打工|做事)"
                             r"|你是(这儿的|店里的)?(店员|老板|服务生|店长)"
                             r"|这店是(你|您)的")),
    # 「你们几点打烊」问的是营业时间，不是「你等了多久」。以前「几点」命中了
    # howlong，答成「约好十点半，现在快十二点了」——问店，答人（dialogue_sanity 第 3 句）。
    ("closing", re.compile(r"打烊|打佯|关门|关店|营业|几点(关|收|打)|还开(着)?吗|开着吗|"
                           r"能(坐|待)(一会|会儿)|能不能(坐|待)")),
    # 「末班车还有多久」问的是车，不是「你等了多久」。不单列的话会落到 howlong，
    # 答成「约好十点半，现在快十二点了」（chat_probe 第 10 句）。
    ("last_train", re.compile(r"末班车|末班|最后一班|班车")),
    # 「你在这家店做过多久了」问的是**工作年限**，不是「你等了多久」。
    # 2026-10-21：以前被 howlong 的裸「多久了」吃掉，模型逐字照搬错 content
    # （「约好十点半。现在快十二点了。」）——实机可复现。
    ("work_howlong", re.compile(r"(这家店|这店|这儿|这里|在这|店里).{0,8}(多久|几年|多长时间|多长时间了)|"
                                r"(做|干|上班).{0,4}(多久|几年)|工作了?多久")),
    ("howlong", re.compile(r"等多久|多长时间|等了多长|"
                           r"等了?(很久|很长时间|半天|好久)|很久了吧")),
    ("where", re.compile(r"他在哪|去哪了|在哪里|去哪儿|住哪|什么地方|上哪")),
    # 「你平时都这个点回去吗」问的是**习惯**，不是「怎么回去」。以前被 how_home 的
    # 裸「回去」吃掉，答成「我住这条街，走回去十来分钟」——问习惯，答路线。
    # （独立盲评点了这一条。）必须排在 how_home 前面，否则「回去」两个字先被抢走。
    ("habit", re.compile(r"(平时|平常|一般都|习惯|每天都).{0,10}(回去|回家|回|睡|起|作息|走)|"
                         r"都这个点(才)?回")),
    # 2026-10-21：裸「回去/回家」会把陈述句也吃进来——「我明天还要上班，真不想回去。」
    # 是陈述，被 how_home 判成提问后模型回了一句「你住哪条街？我送你一段」（答非所问且反问）。
    # 这里只认真的在问回程的句式。
    ("how_home", re.compile(r"怎么回|怎么走|怎么去|远不远|"
                            r"(回去|回家|回程)[^。！!]{0,4}(吗|呢|？|\?)|"
                            r"(你|您).{0,3}(怎么|如何).{0,2}(回|走|去)")),
    # 「那你呢」：把话题交回去的追问。放在最后，因为它通常依赖上一轮的话题。
    # 两个位置都要认：`你呢？` 起头（纯把话头递过来），以及跟在他自己那句话后面的
    # 「我在躲雨。你呢？」。以前只认行首，后者就落到兜底 question，答成
    # 「躲雨的人，你在这儿多久了？」——既没答"我在做什么"，还倒问他一句（用户截图）。
    ("counter", re.compile(r"(那你|你|您)呢[？?]?\s*$|^(那你呢|你呢|你怎么样|你也是|为什么这么问)")),
    # 「为什么/怎么」的疑问句要排在 wait_who 前面：句子里常常同时出现「等」字，
    # 先判 wait_who 会把「你为什么一个人在这等」当成「你在等谁」，给出同一句答非所问。
    ("why", re.compile(r"为什么|为啥|怎么.*(一个人|等|坐)|原因|图什么|值得吗")),
    # 2026-10-21：结尾的裸 `一个人` 会把「你自己一个人住吗？」判成「你在等谁」（实机样本：
    # 她回「还是那个人。这个我不想说。」）。只保留带疑问语境的写法。
    ("wait_who", re.compile(r"等谁|等人|一个人(坐|待|在|吗|？|\?)|在等什么|等什么|你等人")),
    # 「不用管我 / 不打扰你了 / 我随便坐坐」：对方在客气或准备走。
    # 以前落到 react 槽自由发挥，产出「这么湿，先坐吧」——把人按回去，还带一个
    # 本段没有任何铺垫的「湿」（独立盲评第三段第 6 句）。这类话只需要一句最短的让位。
    ("let_alone", re.compile(r"不用管我|不打扰|你忙你的?|你先忙|别管我|随便坐坐|"
                             r"我随便|自便|你先做你的")),
    # 「你冷不冷」：以前落到泛问句槽，4B 用「咖啡是黑的，不加糖」补了半句毫不相干的废话
    # （独立盲评第二段第 6 句）。单列一个槽，把「手上凉不凉」这个唯一该说的意思钉住。
    ("ask_cold", re.compile(r"冷不冷|冷吗|冻不冻|嫌冷|穿这么少")),
    ("greeting", re.compile(r"^(你好|您好|嗨|hi|hello|在吗|好久不见|晚上好)[!！。.，,~～ ]*$", re.I)),
]

# 「实体词命中」不等于「问的就是那个槽」。一句话里出现「咖啡」时，问的可能是
# 好不好喝（coffee），也可能是谁做的 / 多少钱（不该落到 coffee）。
# 下面这张表声明每个槽**只回答哪几种提问焦点**；检测到别的焦点就跳过这个槽，
# 让后面的规则或兜底去接。没有登记的槽 = 不设防（行为与以前一致）。
_FOCUS_PATTERNS: dict[str, re.Pattern] = {
    "does": re.compile(r"谁(做|冲|泡|煮|买|给)|你(自己)?(做|冲|泡)的|是你?(做|冲|泡)|"
                       r"自己(做|冲|泡)"),
    "price": re.compile(r"多少钱|几块|贵(不贵|吗)|价格|价钱|收费"),
    "time": re.compile(r"几点|什么时候|啥时候|多久|多长时间"),
    "place": re.compile(r"在哪|哪儿|哪里|什么地方|哪家"),
    "reason": re.compile(r"为什么|为啥|怎么(不|没)"),
    "quantity": re.compile(r"几个|多少(杯|次|年|遍)"),
}

# 槽位 → 它能回答的焦点集合。空/未登记 = 不设防。
# 只登记实测踩过坑的槽：coffee 被「谁做的 / 多少钱」劫持过（dialogue_sanity 第 5 句）。
_INTENT_FOCUS: dict[str, frozenset[str]] = {
    "coffee": frozenset({"quality", "kind"}),
    "coffee_who": frozenset({"does"}),
}


def _focus_of(text: str) -> set[str]:
    """这句话问的是「哪个属性」（谁做的 / 多少钱 / 什么时候 / 在哪 …）。"""
    return {name for name, rx in _FOCUS_PATTERNS.items() if rx.search(text or "")}


# ---------------------------------------------------------------- 句式门（2026-10-21）
# 为什么要它：槽位规则是按「出现哪个词」判的，词不区分陈述句和提问句。实测踩到的都是
# 同一个模式——陈述句里出现了提问槽的关键词，于是她按"被问到了"去回答一个没人问的问题：
#   「我明天还要上班，真不想回去。」   → how_home → 「你住哪条街？我送你一段」
#   「今天外面雨真大，我伞都被吹翻了。」 → smalltalk → 预设台词「这几天雨都很大」
# 这些槽是为提问准备的，所以只在句子确实是提问/请求时才允许命中。
_Q_RX = re.compile(r"[？?]|吗|呢|谁|什么|怎么|为什么|为啥|哪|几|多少|多久|"
                   r"是不是|能不能|要不要|有没有|可不可以|怎么样|如何|请|让我|给我")
_NEEDS_QUESTION: frozenset[str] = frozenset({
    "photo", "request", "how_home", "habit", "last_train", "closing",
    "coffee", "coffee_who", "work_here", "work_howlong", "howlong",
    "wait_who", "why", "where", "ask_cold", "given_offer", "handover",
})


# 「能不能让我看看 / 给我看一眼」：单独看什么都指代不明，要配合上一轮她说过什么。
_LOOK_AT = re.compile(r"(让我|给我|我能|能不能|可以)?看(看|一眼|一下)|瞧瞧|瞅瞅")


# 场景里的「道具词」：上一轮刚用过，这一轮就禁掉。
# 4B 的复读不是逐字复读，而是**同一个道具反复出现**（灯牌 → 后门 → 老板娘），
# 靠提示词说「别重复」它记不住，直接给一个禁用词表最有效。
_PROP_WORDS = ("灯牌", "吊灯", "后门", "老板娘", "洗碗池", "门垫", "吧台", "桌面",
               "纸巾", "椅子", "杯子", "小勺", "积水", "屋檐", "玻璃门")

# 一轮里最多允许出现一个「意象词」；这些词在连续几轮里反复出现就会变成念诗
_IMAGE_WORDS = ("雨", "灯", "光", "影子", "夜色", "风")
_METAPHOR = re.compile(r"(像|仿佛|好像|似的|一般)")

# 「答完把话头交回去」有三种自然形态，按轮次轮换。
# 为什么不能只用一句：实测连着两轮用同一条指令，4B 会产出同一种问法
# （#6「你什么时候想见？」→ #9「你想要我什么时候给？」），听感上很机械。
# **不要在这里写例句**：只要括号里出现一句具体的话（「他明天还来不来」
# 「路上顺不顺、要不要坐近一点」），4B 就会把那句原样念出来——两轮独立盲评
# 都抓到过「路上顺不顺？要不要坐近一点？」逐字出现在台词里，而且它和后一句
# 「我该走了」正好相反。这里只给方向，一个例子都不给。
_ASK_HINTS = (
    "答完用半句话问他一件**关于他自己的小事**（冷热、衣服湿没湿、饿不饿这类），"
    "问句放在最后半句，前半句必须先接住他刚说的事。不要问时间表。",
    "答完用半句话问他一句**这店里的事**，不要问时间表；"
    "问句放在最后半句，前半句必须先接住他刚说的事（用他说过的词，别换成你的说法）。",
    "答完用半句话把话头交给他（问一句他此刻的状态），不要替他编时间表；"
    "前半句必须先接住他刚说的事（用他说过的词）。",
)

# 对方**要走了 / 在客气**的时候不许反问：他刚说「我走了」，再接一句问句，
# 观众听到的是「她没听明白我要走」。这类轮次只把话收住。
_LEAVING = re.compile(r"走了|走啦|再见|拜拜|先走|回去|不打扰|不用管我|别管我|"
                      r"你忙|你先忙|自便|随便坐|先坐坐|我该走")

# 「答完把话头交回去」的时机：接话轮全给；被问了一件具体事实/要她给东西的轮次不给。
# 为什么按槽位黑名单而不是白名单：
#   - 白名单（只有 react/volunteer/question）会让 AB 基准的 asks_back 从 4 掉到 0——
#     那套数据集全是「她在被盘问」，反问消失，台词读起来像口供；
#   - 黑名单只排掉「再插一句反问就一定怪」的槽：被考记忆、被要号码、被要照片、
#     被问他在哪/为什么不去找、被怀疑说谎、被提议帮忙。这些轮次先把话答完才对。
_NO_ASK_BACK = frozenset({
    "recall", "self", "name", "request", "photo", "handover", "choice",
    "followup", "where", "why", "why_not_go", "doubt", "offer", "coffee_who",
    # 这几条也是「被问了一个具体属性 / 在被关心 / 在被让位」，答完就收：
    "closing", "habit", "ask_cold", "given_offer", "let_alone",
})

_STAGE = re.compile(r"[（(][^）)]{0,12}[）)]|\*[^*]{0,12}\*")
_SERVICE = re.compile(r"(作为|很高兴|请问|希望|祝您|抱歉|理解您|首先|其次)")

# 4B 偶尔会把英文词混进中文台词（实测出现过「……tonight还不想走」），
# edge TTS 用中文音色念英文会非常出戏，而这条完全可以在**送 TTS 之前**拦住：
#   - 首句闸门里命中 → 重写；
#   - 后面句子命中 → 流式清洗（把这个词剥掉），不为了一个词重写整段。
# 白名单是角色卡/场景里本来就有的名字与常见缩写。
_LATIN = re.compile(r"[A-Za-z]{3,}")
_LATIN_OK = {"Mira", "Portra", "Olympus", "OK", "hi", "Hi", "Cafe"}


def latin_words(text: str) -> list[str]:
    """台词里不该出现的英文单词（白名单之外）。"""
    return [w for w in _LATIN.findall(text or "") if w not in _LATIN_OK]


def strip_latin(text: str) -> tuple[str, list[str]]:
    """把台词里的英文词剥掉，返回 (清洗后文本, 被剥掉的词)。

    为什么不整段重写：只有英文词这一处脏，而且通常出现在第二句以后；整段重写
    会把已经说出口/已经送 TTS 的第一句也对不上。剥词是无损的、确定性的。
    """
    removed: list[str] = []

    def _sub(m: re.Match) -> str:
        w = m.group(0)
        if w in _LATIN_OK:
            return w
        removed.append(w)
        return ""

    out = _LATIN.sub(_sub, text or "")
    if removed:
        # 剥词会留下空格（"…… tonight还不想走" → "…… 还不想走"），顺手压掉
        out = re.sub(r"\s+([，。！？；：、…」”）])", r"\1", out)
        out = re.sub(r"([「“（])\s+", r"\1", out)
        out = re.sub(r"[ \t]{2,}", " ", out).strip()
    return out, removed

# 硬边界：卡包规定「不要主动说出阿岸这个名字」，只有被直接问到时才说一次。
# 这是可机检的（不是提醒模型注意，而是真的会拦），所以放在 forbid 里。
_NAME = "阿岸"

# 卡包事实表里的具体锚点（与 cards/mira_cafe/card.yaml 的 character.facts 对齐）。
# 为什么在这里再抄一份：导演层的通用兜底需要一个「哪些词算具体事实」的白名单，
# 而 director.py 刻意不读卡包（不依赖 yaml、纯函数、可单测）。卡包改事实时这里要一起改。
_FACT_WORDS = ("相机", "胶片", "胶卷", "咖啡", "雨衣", "照片", "内袋", "老板娘",
               "十点半", "十二点", "三年", "四十七", "第二杯", "黑咖啡")

# 通用问题抽话题词时先去掉这些问句/口语功能词，剩下的连续汉字就是「问的是哪件事」
_STOP_WORDS = ("可不可以", "是不是", "能不能", "要不要", "为什么", "怎么样", "什么",
               "怎么", "多少", "多久", "哪里", "哪儿", "哪个", "谁", "你", "我", "他",
               "她", "的", "了", "吗", "呢", "啊", "呀", "吧", "嘛", "是", "在", "有",
               "就", "还", "要", "会", "能", "想", "说", "问", "这", "那", "一个",
               "一下", "现在", "今晚", "今天", "明天", "时候", "觉得", "知道", "真的",
               # 这些留着的话，「你在这儿上班吗」会被切成「儿上班」——
               # 抽出来的碎片既不像人话，也会被当成锚点塞进提示词里逼模型凑词。
               "这儿", "那儿", "这里", "那里", "这边", "那边", "这家", "那家")

# 去停用词之后，被切开的字如果还粘在词头，会得到「儿上班 / 里坐」这种碎片。
# 只剥一个字的头部垃圾，避免误伤「上班」「人」这种本来就只有两三个字的词。
_LEAD_JUNK = "儿里上下个些点家边"

# A 不 A / A 没 A 式问句（冷不冷 / 好不好 / 有没有）
_REDUP = re.compile(r"^(.)(?:不|没|够)\1$")

# 话题同义词组：用户问的话题词只要命中一组，回答里出现组内任意一个都算「答在这件事上」。
# 这是机检「有没有答非所问」的核心：只问「回答里有没有落到这个话题上」，
# 不要求逐字复述问题（那样会逼出机器味），也允许「挡回去」——但要挡在对题上。
_TOPIC_GROUPS: tuple[tuple[str, ...], ...] = (
    ("回去", "回家", "住", "走", "车", "地铁", "打车", "夜班", "路"),
    ("号码", "手机", "电话", "微信", "联系", "不给", "明天"),
    ("照片", "相片", "内袋", "相机", "看"),
    ("咖啡", "苦", "糖", "喝", "杯", "凉"),
    ("怕", "害怕", "一个人", "老板娘", "人"),
    ("骗", "真的", "没骗", "信"),
    ("找", "去", "不知道", "问过", "哪"),
    ("等", "等一个人", "十点半", "第二杯", "人"),
    ("疼", "冷", "累", "困", "饿"),
)


@dataclass
class Plan:
    """一轮的确定性计划 + 可机检的验收标准。"""

    intent: str = "default"
    must: list[str] = field(default_factory=list)      # 必须逐字出现的内容（名字等）
    avoid: list[str] = field(default_factory=list)     # 本轮软禁用的词（复读/意象）
    forbid: list[str] = field(default_factory=list)    # 硬边界：出现即重写（名字）
    max_chars: int = 45
    note: str = ""                                     # 塞进提示词末尾的指令
    critical: bool = False                             # 关键轮：整段阻塞生成 + 校验
    fallback: str = ""                                 # 校验失败时的确定性兜底台词
    content: str = ""                                  # 这一轮要传达的内容（只给意思，措辞交模型）
    content_is_fact: bool = True                       # False = content 是「要做到的事」不是台词素材
    topic: str = ""                                    # 被问的那件事（机检标签）
    anchors: list[str] = field(default_factory=list)   # 回答里必须点到的话题词（机检）
    gate: bool = False                                 # 是否走首句闸门（app/main.py）
    must_ask: bool = False                             # 这一轮要把话头交回去（反问）
    ask_hint: str = ""                                 # 反问的具体形态（按轮次轮换，见 _ASK_HINTS）
    allow_question: bool = False                        # 允许台词以问号收尾（默认不允许，见 gate_check）
    notes: list[str] = field(default_factory=list)     # 运行期备注（重写/兜底），给观测面板看

    def prompt_block(self, use_content: bool = True) -> str:
        # 2026-10-21：这里原来写的是「【本轮怎么做 · 优先级最高，覆盖上面所有说法】」。
        # 「覆盖所有说法」是给**剧情推进**用的，但它同时把「用户这句话」也覆盖掉了：
        # 用户问 A、这一拍想聊 B 时，模型会忠于指令去聊 B —— 用户听到的就是答非所问。
        # 改成「先回答他，再往这个方向延伸」：保留导演层对**节奏**的控制权，
        # 把「回答他刚说的话」放回第一位。
        lines = ["【本轮怎么走 · 先回答对方这句话，再按住下面的方向延伸】"]
        if self.note:
            lines.append(self.note)
        if use_content and self.content:
            # 给「要传达什么」而不是「怎么说」：4B 写不好规划，但很擅长把一句话
            # 换一种说法说自然。把内容钉死、措辞放开，是让它稳定又有活气的关键。
            if self.content_is_fact:
                lines.append(f"这一轮要传达的意思（意思不许改，用你的话说，不要新增事实）：{self.content}")
            else:
                lines.append(f"这一轮要做到的：{self.content}")
        if self.anchors:
            # 把「必须落在哪件事上」直接写成可执行的检查：4B 对「回答必须出现 X」
            # 的服从率远高于「请正面回答」这种元指令（后者是它最容易忽略的一句）。
            lines.append("回答必须点在这件事上（用得到这些词：" +
                         "、".join(self.anchors[:6]) + "），不许用天气/灯/雨/窗外代替回答。")
        if self.must:
            lines.append(f"必须出现的内容：{'、'.join(self.must)}（原样说出来）")
        if self.must_ask:
            lines.append(self.ask_hint or "答完用半句话反问他一个具体问题，把话头交回去（不要问太私人的）。")
        if self.forbid:
            lines.append(f"绝对不能说：{'、'.join(self.forbid)}。")
        if self.avoid:
            lines.append(f"这轮禁用这些词：{'、'.join(self.avoid)}")
        lines.append(f"整轮不超过 {self.max_chars} 字，只写你会说出口的话。")
        return "\n".join(lines)

    def gate_check(self, reply: str, recent: list[str] | None = None,
                   first: bool = False) -> list[str]:
        """首句闸门 / 整段校验：返回违规说明，空列表 = 通过。

        first=True 时只查「不能错」的项（硬禁词、必须内容、话题锚点、复读、旁白），
        不查字数/软禁用词——首句只是整段台词的一部分，拿整轮字数去卡它会误杀。
        """
        out: list[str] = []
        body = (reply or "").strip()
        if not body:
            return ["空台词"]
        for bad in self.forbid:
            if bad in body:
                out.append(f"不能说出「{bad}」")
        for token in self.must:
            if token not in body:
                out.append(f"没有说出「{token}」")
        # 英文词泄漏：中文音色念英文非常出戏，且只修一个词，直接判违规重写最省事
        stray_latin = latin_words(body)
        if stray_latin:
            out.append(f"夹杂英文单词「{stray_latin[0]}」")
        # 核心：回答必须落到被问的那件事上（允许挡，但挡也要挡对题）
        if self.anchors and not any(a in body for a in self.anchors):
            # 2026-10-21：这里原来是**硬判**（返回违规 → 丢掉整段已生成内容，再调一次模型重写）。
            # 问题是它用子串匹配代替语义：锚点里有一条是「外面雨停」这种 4 字短语，
            # 回答里几乎不可能逐字出现，于是 21 轮被它拦下；全量统计 78/496 = 15.7% 的轮次
            # 触发整段重写，每次多一次完整模型调用（+3~9s，最坏一轮 27s），
            # 而重写出来的话不一定更对题（实测有重写后仍然没回答用户问题的）。
            # 现在改成**软判**：只留痕给观测面板，不再重写。真正会跑题的来源
            # （槽位判错、预设盖内容）应该在槽位层修，不该让模型为机检的脆弱买单。
            self.notes.append(
                f"锚点未命中（软）：{self.topic or '对方问的事'}"
                f"（要点到：{'、'.join(self.anchors[:4])}）")
        # 复读：4B 被连问同一件事时会把上一轮原样再说一遍（实测逐字重复）。
        # 整句重叠率对「短句开头复读长句」不敏感（6 字 vs 20 字），所以再加一条
        # 「开头到第一个句号的那截是否出现在最近几句里」——实测这能抓住
        # 「黑咖啡，不加糖。」这种把上一轮开头再说一遍的情况。
        # 窗口取 5 而不是 3：实测在一段 7 轮的对话里，第 7 轮逐字复读了**第 2 轮**
        # （「嗯，雨停了，街面都湿透了。」），3 句窗口刚好漏掉它，观众听到的是
        # 「她在念台词」。5 与 s.recent 的保留长度、提示词里展示的句数一致。
        window = (recent or [])[-5:]
        for prev in window[-3:]:
            if prev and _overlap(body, prev) > 0.8:
                out.append("和最近说过的一句几乎一样，必须换一种说法")
                break
        head = _head_prefix(body)
        # 门槛从 4 降到 3：首句闸门拿**第一句**来判，而 4B 最典型的复读开头就是
        # 「嗯，确实」这种 3 字短句（实测一段 7 轮对话里，「嗯，确实」原样出现在
        # 第 2 / 5 / 7 轮，中间夹着别的句子，靠 4 字门槛一次都没拦住）。
        if len(head) >= 3 and any(head in _norm(p) for p in window if p):
            out.append("开头半句和最近说过的一句一样，必须换一种说法")
        # 复读第二层：**中段/后半句**逐字搬运。实测第 5 轮「你在等谁」答成
        # 「说好会来的人。十点半，现在快十二点了。」——后半句逐字来自第 4 轮，
        # 开头检查完全看不见它。按逗号切成分句，任一截 >=6 字且出现在最近 3 句里就判复读。
        for prev in window[-3:]:
            pn = _norm(prev)
            if not pn:
                continue
            dup = next((c for c in _clauses(body) if c in pn), "")
            if dup:
                out.append(f"「{dup}」上一轮刚说过，必须换一种说法")
                break
        # 结尾又反问：她话少，答完就该收住。4B 最爱用半句问号把话撑长
        # （实测「你是在等人吗」→「嗯。等一个人。……这店里的灯，还亮着吗？」）。
        # 只有明确要求把话头交回去的轮次（must_ask），或槽位本身就允许用一句
        # 关心把话递回去的（allow_question，目前只有「拒绝给号码」——拒绝后问一句
        # 「你明天还来吗」是软化，不是跑题），才允许以问号收尾。
        if not (self.must_ask or self.allow_question) and re.search(r"[？?][」”'\"]?\s*$", body):
            out.append("结尾又反问了：把问号那半句删掉，答完就收住")
        if _STAGE.search(body):
            out.append("出现了括号/星号旁白")
        if _SERVICE.search(body):
            out.append("客服腔")
        if not first:
            for bad in self.avoid:
                if bad in body:
                    out.append(f"又用了「{bad}」")
            clean = re.sub(r"\s", "", body)
            if len(clean) > self.max_chars + 20:
                out.append(f"太长（{len(clean)} 字）")
        return out

    def check(self, reply: str) -> list[str]:
        """兼容旧调用：整段机检。"""
        return self.gate_check(reply)


def _overlap(a: str, b: str) -> float:
    """两个字一组算重叠率：判断「是不是把上一轮的话又说了一遍」。"""
    clean = lambda s: re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s or "")  # noqa: E731
    x, y = clean(a), clean(b)
    if len(x) < 4 or len(y) < 4:
        return 0.0
    bx = {x[i:i + 2] for i in range(len(x) - 1)}
    by = {y[i:i + 2] for i in range(len(y) - 1)}
    return len(bx & by) / max(1, len(bx))


def _topic_words(text: str) -> list[str]:
    """从问句里抽「问的是哪件事」：去掉问句功能词后剩下的连续汉字。

    这是给**没有专门规则的问题**用的兜底（正常问句由 _INTENT_RULES 命中，
    槽位内容是手写的、可审计）；抽不到就返回空，闸门退化为只查复读/旁白。
    """
    t = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", " ", text or "")
    for w in sorted(_STOP_WORDS, key=len, reverse=True):
        t = t.replace(w, " ")
    words = [w for w in t.split() if len(w) >= 2]
    out: list[str] = []
    for w in words:
        # 剥掉粘在词头的一个垃圾字（「儿上班」→「上班」），只有剥完还剩 2 字才剥
        if len(w) >= 3 and w[0] in _LEAD_JUNK:
            w = w[1:]
        # A 不 A 式问句不是话题词：「你冷不冷」抽出来的锚点「冷不冷」会被塞进提示词，
        # 4B 就照着复述成「手凉，咖啡没加糖。冷不冷？我倒是觉得……挺冷的。」
        # （实测 dialogue_sanity set B 第 7 句）。归约成单字，再按同义词组补全。
        m = _REDUP.fullmatch(w)
        if m:
            w = m.group(1)
        if len(w) >= 2:
            out.append(w)
        for group in _TOPIC_GROUPS:
            if any(g in w for g in group):
                out.extend(group)
    # 单字（冷/累/困/饿）只有在同义词组里出现过才留：那是真的能当话题用的字
    if not out:
        leaves = {x for g in _TOPIC_GROUPS for x in g}
        out = [w for w in words if w in leaves]
    # 去重保序
    return list(dict.fromkeys(out))


def _is_question(text: str) -> bool:
    if "？" in text or "?" in text:
        return True
    return bool(re.search(r"(吗|呢|什么|怎么|为什么|多少|多久|哪|谁|"
                          r"是不是|要不要|能不能|可不可以|行不行|好不好)\s*[。.！!~～]*$",
                          text or ""))


# 「接话轮」里必须点到的那几个具体的事（只用在 react 槽的 must 上，见 _plan_turn）
_CONCRETE = ("上班", "加班", "下班", "班", "天气", "雨", "冷", "热", "累", "困", "饿",
             "饭", "车", "路", "家")

# 是非问：答案只有「是/不是」「有/没有」两种。这类问题不需要具体数字，
# 强行要「具体事实」会逼 4B 现编一个（实测「外面雨停了吗」→「大概三点二十」，
# 与同一段对话里「现在快十二点」直接打架）。
_YESNO = re.compile(r"(吗|是不是|有没有|对不对|行不行|好不好|了吧)\s*[。.！!~～？?]*$")


def _last_name(facts: list[str]) -> str | None:
    for item in reversed(facts or []):
        m = re.match(r"他叫(.+)", item or "")
        if m:
            return m.group(1).strip() or None
    return None


def _overused(recent: list[str], minimum: int = 2) -> list[str]:
    """最近几轮里反复出现的意象词 —— 这一轮禁掉，逼模型换一个具体的东西。"""
    hay = " ".join(recent[-3:] if recent else [])
    return [w for w in _IMAGE_WORDS if hay.count(w) >= minimum]


def _props_ban(recent: list[str]) -> list[str]:
    """上一轮刚用过的场景道具，这一轮禁掉（专治 4B 的「同一个道具反复出现」）。"""
    last = (recent[-1] if recent else "") or ""
    return [w for w in _PROP_WORDS if w in last]


def _last_detail(facts: list[str]) -> str:
    """从记忆里挑一条「具体的事」，用来补一句不空的回答。"""
    for item in reversed(facts or []):
        if not item.startswith("他叫"):
            return item
    return ""


def _norm(s: str) -> str:
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s or "")


def _head_prefix(body: str, limit: int = 20) -> str:
    """台词开头到第一个句号为止的那一截（去标点）——用来判「短开头复读」。"""
    m = re.match(r"[^。！？!?]*", body or "")
    return _norm(m.group(0))[:limit]


def _clauses(body: str, min_len: int = 6) -> list[str]:
    """把台词按句号/逗号切成分句（去标点），只留够长的那些。

    为什么要切到逗号这一级：4B 的跨轮复读经常发生在**后半句**——
    「说好会来的人。十点半，现在快十二点了。」后面那截逐字来自上一轮，
    而「开头半句」检查只看开头，一次都没拦住（用户实测截图第 4/5 轮）。
    """
    out: list[str] = []
    for part in re.split(r"[。！？!?，,、；;…\s]+", body or ""):
        norm = _norm(part)
        if len(norm) >= min_len:
            out.append(norm)
    return out


def _plan_turn(text: str, *, facts: list[str] | None = None, new_facts: list[str] | None = None,
               recent: list[str] | None = None, is_open: bool = False, beat: int = 0,
               turn_no: int = 0, prev_contents: list[str] | None = None,
               prev_topics: list[str] | None = None) -> Plan:
    """给这一轮定计划。纯函数：同样的输入永远得到同样的计划（可单测、可复现）。

    beat 是**跳拍之后**的当前拍：内容要守卡包的「这一拍能松口什么」，否则
    提示词里「这一拍绝对不要说」和末尾的 plan 指令会互相打架，4B 会挑省力的那条
    （实测：第 2 拍就说出照片/三年）。
    """
    facts = list(facts or [])
    new_facts = list(new_facts or [])
    recent = list(recent or [])
    prev_contents = list(prev_contents or [])
    prev_topics = list(prev_topics or [])
    text = (text or "").strip()

    if is_open:
        return Plan(
            intent="open", max_chars=25,
            note="对方刚推门进来躲雨，坐在你对面。你抬头看他一眼，先开口：一句自然的招呼或搭话，"
                 "不要自我介绍一长串，不要解释剧情。",
            critical=False,
        )

    intent = "default"
    focus = _focus_of(text)
    asks = bool(_Q_RX.search(text or ""))
    for name, rx in _INTENT_RULES:
        if not rx.search(text):
            continue
        allowed = _INTENT_FOCUS.get(name)
        # 实体词命中、但问的属性不是这个槽能答的 → 让后面的规则/兜底来接
        if allowed is not None and focus and not (allowed & focus):
            continue
        # 2026-10-21：这些槽是**为提问准备的**（问照片、问价钱、问怎么回去、问营业时间…）。
        # 用户说陈述句时命中它们，等于把陈述当提问回答——「我明天还要上班，真不想回去」
        # 被 how_home 吃成「你住哪条街？我送你一段」。陈述句一律让给后面的规则/react。
        if name in _NEEDS_QUESTION and not asks:
            continue
        intent = name
        break

    # 「能让我看看吗 / 给我看一眼」这种**指代上一轮物件**的追问：她上一句刚说了
    # 相机/照片，这一句就该按 photo 槽回答（给不给 + 理由），而不是当泛问句现编。
    # 实测反例：上一轮她答「手里拿的是相机」，下一句「能让我看看吗」落到兜底槽，
    # 生成出「照片在内袋里，你拿去看了——」这种半截话。
    if intent == "default" and _LOOK_AT.search(text):
        # 指代上一轮那个东西：她刚说「手里拿的是相机」，看的就该是相机；
        # 只有上一轮说的是「照片」时才进 photo 槽（否则答成「照片在雨衣内袋里」，
        # 把对方要看的东西换掉了 —— 独立盲评第二段第 5 句）。
        blob = "".join(recent)
        if "照片" in blob or "相片" in blob:
            intent = "photo"
        elif "相机" in blob:
            intent = "handover"

    # 阿岸这个名字只有被直接问到时才允许出现一次；其它轮次硬拦。
    forbid = ["阿岸"] if intent != "name" else []
    avoid = list(dict.fromkeys(_overused(recent) + _props_ban(recent)))
    name = _last_name(facts)

    if intent == "clarify":
        # 唯一正确的反应：不怪他、不追问他哪里不懂、不用景物岔开，把上一句换个说法重说一遍。
        # 4B 在这里最爱的两个错法都在实测里出现过：「你说得不太清楚」（把锅甩给用户），
        # 以及完全跑题（「你是不是刚下过雨？地面还滑呢。」）。
        return Plan(
            intent="clarify", avoid=avoid, forbid=forbid, max_chars=30, gate=False,
            note="对方没听懂你上一句（或者嫌你说得绕）。不要说他没听清、不要问他哪里不懂、"
                 "不要提天气和店里的东西：把上一句的意思用更直白的一句话重说一遍，说完就停。"
                 "不要反问，不要道歉一长串。",
            fallback="……我说得太绕了。我在等人。",
        )

    if intent == "recall":
        if name:
            return Plan(
                intent="recall", must=[name], avoid=avoid, max_chars=32, critical=True,
                content=f"你叫{name}，我记得。",
                note=f"对方在考你记不记得他——他叫「{name}」。第一句必须原样说出这个名字"
                     f"（就说「{name}」），再补半句自然的话。名字只说一次，不要连着重复两遍"
                     "（「张伟。我记得你叫张伟。」听起来像复读机，独立盲评点的就是这个）。"
                     "只许用事实表里的事，"
                     "不要提他没说过的细节（比如他等什么车、穿什么衣服）。"
                     "不要解释你为什么记得，不要说教，不要反问他。",
                fallback=f"{name}。……记得，你刚才说的我都记着。",
            )
        detail = _last_detail(facts)
        tail = f"可以补一句「{detail}」这个你真记得的细节。" if detail else "补一句你观察到的细节（他几点进来、坐在哪）。"
        return Plan(
            intent="recall", avoid=avoid, max_chars=40, critical=False,
            note="对方在问你还记不记得他。你没听他说过名字——第一句老实承认记不清，"
                 "但别用空话（「你刚进来吧」）糊过去：" + tail + "不要编造名字。",
        )

    if intent == "self":
        return Plan(
            intent="self", must=["Mira"], avoid=avoid, max_chars=30, critical=True,
            content="我叫 Mira，今晚在这儿等一个人。",
            note="对方在问你是谁。第一句直接报名字「Mira」，后面半句说你此刻在做什么。"
                 "不要背简历，不要列外形。",
            fallback="Mira。……你呢？",
        )

    if intent == "name":
        return Plan(
            intent="name", must=["阿岸"], avoid=avoid, forbid=[], max_chars=28, critical=True,
            content="阿岸。……他叫阿岸。",
            note="对方直接问了他的名字。只这一次，说出「阿岸」；说完就收住，不要解释、不要继续讲他的事。",
            fallback="阿岸。……就这一次。",
        )

    if intent == "followup":
        # 问「后来 / 有没有消息」：absence 拍（beat>=4）才允许说「再没出现」；
        # 更早的拍只说「没消息」，不提照片、不提三年（守卡包 allow）。
        content = ("后来再没出现过。……没消息，也没回来。" if beat >= 4
                   else "没消息。……从那天起，就没消息了。")
        return Plan(
            intent="followup", avoid=avoid, forbid=forbid, max_chars=42, gate=True,
            topic="他后来有没有再联系（后来的事）",
            anchors=["后来", "再没", "没消息", "没联系", "没回来", "不知道"],
            content=content, fallback=content,
            note="对方在追问「后来」——问的是他**有没有再出现、有没有消息**，"
                 "不是问你要不要给号码，也不是让你再看一遍照片。先直接答这件事"
                 "（再没出现 / 没消息），再补半句你现在的样子。不要拿灯、雨、地垫岔开。",
        )

    if intent == "choice":
        # 问「你打算怎么办 / 照片寄不寄」：答案落在**决定**上（还没决定、先留着），
        # 不是把照片再描述一遍。卡包 choice 拍也要求不自己下结论、把选择抛回去。
        content = ("还没决定。……该不该寄出去，我也在想。" if beat >= 5
                   else "还没决定。……先放着吧。")
        return Plan(
            intent="choice", avoid=avoid, forbid=forbid, max_chars=42, gate=True,
            topic="你打算怎么办 / 照片要不要寄",
            anchors=["决定", "还没", "寄", "留着", "再想想", "放着"],
            content=content, fallback=content,
            note="对方问的是**你打算怎么办**（照片寄不寄），不是让你再描述一遍照片。"
                 "先答「还没决定」，再给半句打算（先留着 / 再想想）；不要自己把结局定下来。",
        )

    if intent == "handover":
        # 「能让我看看吗」指代上一轮她说的**相机**。只答应递过去，不要换成照片。
        return Plan(
            intent="handover", avoid=avoid, forbid=forbid, max_chars=30, gate=True,
            topic="能不能给他看看手里那个东西",
            anchors=["可以", "给", "拿", "看", "慢"],
            content="可以。……给你，拿稳点。",
            fallback="可以。给你，拿稳点。",
            note="对方想看看你手里/身边的那样东西（上一轮你刚说过它是什么）。"
                 "直接答应并递过去（「可以」+「给你」），再补半句提醒。"
                 "不要换成别的东西回答，不要说「要拿的话」。",
        )

    if intent == "photo":
        # 照片在第 4 拍之后才允许拿出来；更早被问到就走「还没到松口的时候」的版本。
        if beat >= 3:
            content = "照片在雨衣内袋里，边角被摸软了。……你要看，我可以拿出来。"
        else:
            content = "照片在雨衣内袋里，一直放着。……现在先不拿出来。"
        return Plan(
            intent="photo", avoid=avoid, forbid=forbid, max_chars=45, gate=True,
            topic="能不能看看照片（给不给）",
            anchors=["照片", "相片", "内袋", "拿", "给", "看"],
            content=content, content_is_fact=True,
            fallback="照片在雨衣内袋里，边角被摸软了。",
            note="对方想看照片。这一轮要给明确反应：给，或者不给 + 一个具体理由。"
                 "不要含糊带过，也不要用「不是」开头。",
        )

    if intent == "request":
        return Plan(
            intent="request", avoid=avoid, forbid=forbid, max_chars=42, gate=True,
            topic="联系方式（号码/手机）",
            anchors=["号码", "手机", "电话", "微信", "不给", "明天", "找"],
            content="号码不给。……你要找我，明天还来这儿就行。",
            fallback="号码不给。你要找我，明天还来这儿就行。",
            # 拒绝之后用一句「你明天还来吗」把话递回去是软化，不是跑题：
            # 只有这一支允许台词以问号收尾（默认不允许，见 gate_check 的结尾反问规则）。
            allow_question=True,
            note="对方在要你的联系方式。卡包规定不给：先明确拒绝，再给一个别的出口"
                 "（明天还来这儿）。拒绝要落在「号码」这件事上，不许用「不说这个」岔开。",
        )

    if intent == "offer":
        return Plan(
            intent="offer", avoid=avoid, forbid=forbid, max_chars=42, gate=True,
            topic="要不要帮我打电话",
            anchors=["电话", "打", "不用", "号码", "早就"],
            content="不用。……我要是想打，早就打了。",
            fallback="不用。我要是想打，早就打了。",
            note="对方提议帮你打电话。明确回应（不用），再给理由：你要是想打，早就打了。"
                 "不要说「不是」，不要扯到照片。",
        )

    if intent == "why_not_go":
        # 为什么不去找他 → 槽位是「不去的理由」，不是「为什么在这里等」。
        if beat >= 4:
            content = "不知道他在哪。……三年了，一直没找到。"
            anchors = ["找", "不知道", "问过", "三年", "哪"]
        else:
            content = "不知道他在哪。……问过，没人知道。"
            anchors = ["找", "不知道", "问过", "哪"]
        return Plan(
            intent="why_not_go", avoid=avoid, forbid=forbid, max_chars=45, gate=True,
            topic="为什么不去找他",
            anchors=anchors, content=content, fallback=content,
            note="注意：对方问的是「你**为什么不去找他**」，不是「你为什么在这里等」。"
                 "先回答「不去的理由」（不知道他在哪 / 问过没人知道），"
                 "不要说等的时间，不要提拍照，不要给结论。",
        )

    if intent == "doubt":
        # 第 1 拍还不能把「十点半/快十二点」全说出来（卡包 allow 只允许含糊承认等人）
        content = ("没骗你。我就是在等人。" if beat < 1
                   else "没骗你。我就是在等人，等到快十二点了。")
        return Plan(
            intent="doubt", avoid=avoid, forbid=forbid, max_chars=40, gate=True,
            topic="你是不是在骗我",
            anchors=["骗", "没骗", "真的", "信", "等", "十二点"],
            content=content,
            fallback=content,
            note="对方在怀疑你说谎。第一句必须正面否认（「没骗你」），再给一个具体事实"
                 "（你真的在等人、十点半等到快十二点）。不要用「不是」两个字糊过去。",
        )

    if intent == "fear":
        return Plan(
            # must 把「怕」钉死：AB 基准实测过「店里还有人没走。……冷不冷？」——
            # 理由给了、怕不怕没说，等于没回答（on_topic 判 False）。
            intent="fear", must=["怕"], avoid=avoid, forbid=forbid, max_chars=40, gate=True,
            topic="你一个人不害怕吗",
            anchors=["怕", "害怕", "一个人", "店", "有人", "老板娘"],
            content="怕什么。店里还有人没走。",
            fallback="怕什么。店里还有人没走。",
            note="对方在问你一个人怕不怕。先答「怕/不怕」，再给一个具体理由"
                 "（店里还有人、街上还有车）。不要用天气或雨回答。",
        )

    if intent == "given_offer":
        # 对方请她喝/吃。先答这一句（不用 + 理由），不要反手给他倒一杯（方向反）。
        return Plan(
            intent="given_offer", avoid=avoid, forbid=forbid, max_chars=34, gate=True,
            topic="对方请她喝点热的",
            anchors=["不用", "不用了", "这杯", "还有", "喝", "谢"],
            content="不用。……我这杯还没喝完。",
            fallback="不用。我这杯还没喝完。",
            note="对方在请你喝点热的（在关心你）。先接受或谢绝（「不用」「不用了，谢谢」），"
                 "再给半句理由（我这杯还没喝完）。不要反过来请他喝、不要答成咖啡什么味道。",
        )

    if intent == "coffee_who":
        # 卡包里没有任何一句说咖啡是谁冲的，但这是「正在喝咖啡的人」被问到的最自然的一句。
        # 只给一个最小事实（不是我做的 / 人家冲的），不编咖啡豆、不编店里的排班。
        return Plan(
            intent="coffee_who", avoid=avoid, forbid=forbid, max_chars=36, gate=True,
            topic="这杯咖啡是谁做的 / 是不是你做的",
            anchors=["做", "不是我", "别人", "老板娘", "冲", "端"],
            content="不是我做的。……人家冲好端过来的，我只管喝。",
            fallback="不是我做的。人家冲好端过来的。",
            note="对方在问这杯咖啡是不是你自己做的。先直接回答「不是」（不是你做的），"
                 "再说一句是谁弄的。不要答成咖啡苦不苦、加不加糖——那是另一个问题。",
        )

    if intent == "coffee":
        # 「桌上那杯凉了」是 admit 拍之后才准说的信息（第二杯是给他的）
        content = ("黑咖啡，不加糖。" if beat < 2
                   else "黑咖啡，不加糖。……桌上那杯已经凉了。")
        return Plan(
            intent="coffee", avoid=avoid, forbid=forbid, max_chars=38, gate=True,
            topic="这咖啡好喝吗",
            anchors=["咖啡", "苦", "糖", "杯", "喝", "凉"],
            content=content,
            fallback="黑咖啡，不加糖。",
            note="对方在问咖啡好不好喝。直接答：黑咖啡、不加糖（可以补一句桌上那杯的事）。"
                 "这是最普通的日常问题，必须正面答，不要转到你自己的心事。",
        )

    if intent == "habit":
        # 问作息/习惯。卡包只给了一个事实：她今晚一直坐在这儿。所以答案是
        # 「不一定」+ 今晚这一句，不许编上班时间表（她不是店员）。
        return Plan(
            intent="habit", avoid=avoid, forbid=forbid, max_chars=40, gate=True,
            topic="你平时是不是都这个点回去",
            anchors=["不一定", "平时", "今天", "坐", "久", "看"],
            content="不一定。……今天坐得久了点。",
            fallback="不一定。今天坐得久了点。",
            note="对方在问你**平时的作息**（是不是每天都这个点回去），不是问你住在哪、怎么回去。"
                 "先回答「不一定」，再补半句今晚的情况。不要答住址、不要回答怎么走。",
        )

    if intent == "how_home":
        return Plan(
            intent="how_home", avoid=avoid, forbid=forbid, max_chars=42, gate=True,
            topic="今晚怎么回去",
            anchors=["回去", "回家", "住", "车", "分钟", "街"],
            content="我住这条街，走回去十来分钟。……今晚还不想走。",
            fallback="我住这条街，走回去十来分钟。",
            note="对方在问你怎么回去。给一个具体方式（住得近、走回去十来分钟），"
                 "不要用「雨停了」「灯」来回答。答完可以补半句今晚还不打算走。",
        )

    if intent == "work_here":
        # 卡包第 1 拍的 allow 只允许「说店里快打烊了 / 雨刚停」，没给她店员身份。
        # 所以这里给的答案是「不是店员 + 我在等人」——既是真话，也不越过这一拍。
        return Plan(
            intent="work_here", avoid=avoid, forbid=forbid, max_chars=32, gate=True,
            topic="你是不是这家店的店员",
            anchors=["不是", "等", "坐", "客人", "店"],
            content="不是。……我就是坐在这儿等人。",
            fallback="不是。我只是坐在这儿等人。",
            note="对方在问你是不是这家店的店员 / 老板。直接否认，再补半句你在这儿干什么"
                 "（就是坐在这儿等人）。不要编班次、不要说自己在打工、不要提老板娘留门。",
        )

    if intent == "closing":
        # 卡包明确允许「可以说店里快打烊了」，但没写具体几点打烊——
        # 所以回答只能是「快打烊了」，不许现编一个 21:30。
        content = ("快打烊了。……老板娘还没关灯。" if beat < 1
                   else "快打烊了。……灯还亮着，你能再坐一会儿。")
        return Plan(
            intent="closing", avoid=avoid, forbid=forbid, max_chars=42, gate=True,
            topic="这家店还开不开 / 几点打烊",
            anchors=["打烊", "关门", "关灯", "老板娘", "坐", "还开"],
            content=content, fallback="快打烊了。老板娘还没关灯。",
            note="对方问这家店还开不开 / 几点打烊。你知道的就是「快打烊了」："
                 "不要编具体钟点，也不要答成你等了多久；可以补半句（老板娘还没关灯 / 还能坐一会儿）。",
        )

    if intent == "last_train":
        # 「末班车还有多久」：她不知道时刻表，就老实说不知道 + 给一个自己的打算。
        # 旧版落到 howlong，答成「约好十点半，现在快十二点了」（问的是车，答的是等）。
        content = "不知道还有几班。……我走回去，十来分钟。"
        return Plan(
            intent="last_train", avoid=avoid, forbid=forbid, max_chars=40, gate=True,
            topic="末班车还有多久",
            anchors=["末班", "车", "几班", "班次", "不知道", "走", "分钟"],
            content=content, fallback=content,
            note="对方问的是**末班车还有多久**，不是问你等了多久。你不知道班次，"
                 "就直说不知道还有几班，再补半句你怎么回去（走回去十来分钟）。",
        )

    if intent == "howlong":
        return Plan(
            intent="howlong", avoid=avoid, forbid=forbid, max_chars=38, gate=True,
            topic="等了多久",
            anchors=["十点半", "十二点", "点", "小时", "久"],
            content="约好十点半。现在快十二点了。",
            fallback="约好十点半。现在快十二点了。",
            note="对方在问等了多久。给时间：约好十点半，现在快十二点了。直接答，不要绕。",
        )

    if intent == "where":
        return Plan(
            intent="where", avoid=avoid, forbid=forbid, max_chars=40, gate=True,
            topic="他在哪 / 去哪了",
            anchors=["不知道", "哪", "没说过", "找", "三年"],
            content="不知道。……他没说过要去哪。" if beat < 4 else "不知道。……三年了，我一直不知道他在哪。",
            fallback="不知道。他没说过要去哪。",
            note="对方在问那个人在哪。你不知道，就直说不知道（卡包规定不许给结论、不许编去处），"
                 "不要用景物岔开。",
        )

    if intent == "why":
        return Plan(
            intent="why", avoid=list(dict.fromkeys(avoid + ["雨", "天气", "夜色"])), max_chars=42,
            forbid=forbid, gate=True, topic="为什么这么晚还在（等）",
            anchors=["等", "十点半", "没来", "约好", "电话"],
            content="约好十点半，他一直没来。我没给他打电话。",
            fallback="约好十点半，他一直没来。",
            note="对方在问你为什么在这种天气里等。第一句必须给一个**具体原因**：你跟他约好了十点半，"
                 "他一直没来。不要用「不是」开头，也不要用灯、后门、老板娘这些店里的事岔开。",
        )

    if intent == "wait_who":
        # 连着两轮问「是不是在等人」= 追问：这一轮必须给一个新的具体信息，
        # 否则 4B 会逐字复读上一轮（实测两轮 100% 一样，闸门会把它打回兜底）
        repeated = "你是不是在等人 / 等谁" in prev_topics
        if beat == 0:
            content, anchors = "嗯。等一个人。", ["等", "一个人", "人"]
        elif repeated:
            # 连着问第二遍「你在等谁」：**不许再把十点半/十二点说一遍**。
            # 以前这一支的 content 是「还是那个人。十点半说好的，现在快十二点了。」——
            # 而上一轮刚说过一模一样的后半句，用户看到的截图就是：
            #   第4轮「说好十点半，现在快十二点了。」→ 第5轮「十点半，现在快十二点了。」
            # 像复读机。这里改成「守住不说的那个边界」：不报名字、不报时间，只说一句不想讲。
            content = "还是那个人。……别问了，这个我不想说。"
            anchors = ["还是", "别问", "不想说", "不想讲", "不提", "那个人"]
            note = ("对方第二次问你在等谁，已经越过你愿意说的那条线了。"
                    "先承认「还是那个人」，再明说这一句不想讲就收住。"
                    "**不要再说十点半、十二点、几点**（上一轮刚说过，再说就是复读），"
                    "也不要反问他。")
            fallback = "还是那个人。……这个我不想说。"
            return Plan(
                intent="wait_who", avoid=avoid, forbid=forbid, max_chars=40, gate=True,
                topic="你是不是在等人 / 等谁", anchors=anchors,
                content=content, fallback=fallback, note=note,
            )
        else:
            content = "嗯。等一个人。说好十点半，现在快十二点了。"
            anchors = ["等", "一个人", "人", "十点半", "第二杯"]
        return Plan(
            intent="wait_who", avoid=avoid, forbid=forbid, max_chars=45, gate=True,
            topic="你是不是在等人 / 等谁",
            anchors=anchors, content=content, fallback=content,
            note="对方在问你是不是在等人。承认在等人就够了（「等一个人」这个程度），"
                 "但不要说他叫什么、不要提三年前的事。"
                 "注意：不能说「我不是在等人」这种否定句——你就是在等人，只是不说等谁。"
                 "不要拿店里的灯、后门、老板娘来岔开话题。",
        )

    if intent == "let_alone":
        # 对方在客气 / 准备走。不挽留、不追问、不关心他冷热，只让位。
        return Plan(
            intent="let_alone", avoid=avoid, forbid=forbid, max_chars=24, gate=True,
            topic="对方说不用管他 / 不打扰了",
            anchors=["嗯", "坐", "叫", "自便", "随便"],
            content="嗯。……你坐你的，有事叫我。",
            fallback="嗯。你坐你的。",
            note="对方在客气（不用管我 / 不打扰你了 / 我随便坐坐）。不要挽留、不要再关心他"
                 "冷热、不要反过来问他问题；用一句最短的话表示「你随便，我在这儿」就够。",
        )

    if intent == "ask_cold":
        # 只答冷不冷 + 一个手上的感觉。不岔咖啡、不反问（这两个都是实测踩过的）。
        return Plan(
            intent="ask_cold", avoid=avoid, forbid=forbid, max_chars=28, gate=True,
            topic="你冷不冷",
            anchors=["冷", "凉", "手", "有点", "还"],
            content="有点冷。……手是凉的。",
            fallback="有点冷。手是凉的。",
            note="对方在问你冷不冷。先答「冷 / 不冷」，再补半句手上的具体感觉就收住。"
                 "不要岔到咖啡、不要反问、不要描述雨。",
        )

    if intent == "greeting":
        return Plan(
            intent="greeting", avoid=avoid, forbid=forbid, max_chars=28,
            note="对方只说了句招呼（你好/嗨/在吗）。用一句招呼接住他，可以带一个此刻的动作"
                 "（「嗯。……你坐吧。」「哦，你好。」「这么湿，坐吧。」），不要自我介绍。"
                 "不要说时间：十点半是你跟别人约的，跟这位客人无关。"
                 "不要拿天气当回答，也不要反问他。",
        )

    if intent == "counter":
        # 「你呢」有两种，答法不一样：
        #   * 起头型（「你呢？」）——对方只是把话头递过来，给一句自己的就够；
        #   * 跟随型（「我在躲雨。你呢？」）——他刚说了自己的处境，你必须**先接住他那件事**
        #     再答自己。否则就是截图里那句「躲雨的人，你在这儿多久了？」：既没回答
        #     "我在做什么"，还倒过来问他一句。
        follow = bool(re.search(r"[^\s]呢[？?]?\s*$", text)) and not re.match(
            r"^\s*(那你呢|你呢|你怎么样|你也是|为什么这么问)", text)
        if follow:
            return Plan(
                intent="counter", avoid=avoid, forbid=forbid, max_chars=34, gate=True,
                topic="你呢（他在问你自己的情况）",
                anchors=["我", "等", "坐", "这儿", "躲"],
                content="我？……这儿坐着等人。", content_is_fact=True,
                fallback="我？……在这儿等人。",
                note="他先说自己的处境（比如在躲雨），再把话头交给你（「你呢」）。先用半句话"
                     "接住他刚说的那件事，再回答**你自己的情况**（你一个人坐在这儿等人）。"
                     "不要反问他，不要问他在店里待多久、等了多久。",
            )
        return Plan(
            intent="counter", avoid=avoid, forbid=forbid, max_chars=35,
            content="我？……还是老样子。",
            content_is_fact=True,
            note="对方把话题交回给你了。先给一句自己的（别空着），再把话头递回去。",
        )

    if new_facts:
        joined = "；".join(new_facts[:2])
        hello = next((f[2:] for f in new_facts if f.startswith("他叫")), "")
        others = "；".join(f for f in new_facts if not f.startswith("他叫"))
        if hello:
            content = (f"他叫{hello}。你重复一遍名字（「{hello}。」），"
                       f"再顺着他说的事接一句关心" + (f"（他说的：{others}）" if others else "") + "。")
        else:
            content = f"他刚说了：{joined}。你回一句反应（确认或关心），别说自己的事。"
        return Plan(
            intent="volunteer", avoid=avoid, forbid=forbid, max_chars=45, gate=True, content=content,
            note=f"对方刚说了关于他自己的事：{joined}。第一句先接住这件事（确认/回应/顺着问一句），"
                 "不要复述他的原话，也不要立刻拐回你自己的心事。",
        )

    if _is_question(text):
        # 兜底：没有专门规则的问题。不假装知道槽位内容，只要求「落在问的那件事上」，
        # 并给一个可以照做的回答策略（具体事实 / 老实说不知道）。
        words = _topic_words(text)
        topic = "、".join(words[:3]) or "对方问的这件事"
        if _YESNO.search(text):
            # 是非问（…吗 / 是不是 / 有没有）：**先给是/不是**。
            # 以前一律要「具体事实（时间/物件/数字）」，结果「外面雨停了吗」答成
            # 「雨停了，大概三点二十」——现编一个跟「快十二点」打架的钟点。
            return Plan(
                intent="question", avoid=avoid, forbid=forbid, max_chars=40, gate=True,
                topic=topic, anchors=words,
                content=f"先直接回答对方问的「{topic}」：是，或者不是（没发生就说没有），"
                        "再补半句你看到的样子。",
                content_is_fact=False, fallback="",
                note="对方问的是一件事有没有发生 / 是不是这样。第一句先给「是 / 不是」"
                     "「有 / 没有」，再补半句你看到的具体样子。不要编具体钟点，"
                     "也不要用天气或灯代替回答。",
            )
        return Plan(
            intent="question", avoid=avoid, forbid=forbid, max_chars=45, gate=True,
            topic=topic, anchors=words,
            content=f"先直接回答对方问的「{topic}」：如果你知道，就给一个具体的事实"
                    "（时间/物件/数字）；如果不知道，就老实说不知道，再补半句。",
            content_is_fact=False,
            fallback="",
            note="对方问了一个具体的问题。第一句必须落在那件事上：知道就给具体事实，"
                 "不知道就说不知道。禁止用天气、灯、雨、窗外代替回答。",
        )

    # 陈述句（对方在说自己的事，且没抽到可记的事实）：接住他说的，别拐回自己。
    # 景物词禁得更狠：这一槽没有 anchors 兜底，4B 最省力的走法就是「嗯，雨停了」——
    # 实测第 2 句「今天人真少啊」被答成「嗯，雨停了，街面都湿透了。」，
    # 第 7 句「那不打扰你了」又逐字重复同一句。对方没提的东西这轮直接禁用。
    scenery = [w for w in ("雨", "街", "灯", "光", "窗", "积水", "湿") if w not in text]
    # 不把对方原句整句塞进 content：4B 会把它照抄进台词（实测「我随便坐坐，你不用管我」
    # →「你随便坐坐，我也不用管人多。」——原词回填，像复读机）。只给关键词 + 「用你的话说」。
    keys_words = _topic_words(text)
    keys = "、".join(keys_words[:3]) or "他刚才说的那件事"
    # 再加一道可机检的底线：他这句话里如果有一个**具体的事**（上班/加班/天气/累…），
    # 回答里必须点到它——否则 4B 会滑到写景（AB 基准实测：「今天天气不错啊，我明天还要上班」
    # 被答成「湿透的路反光挺怪的……你走的时候记得带伞」，一个词都没接住）。
    must = [c for c in _CONCRETE if c in text][:1]
    content = (f"他刚说了一件关于他自己的事（关键词：{keys}）。先用你自己的话说一句反应"
               "（不要照抄他的词），再补半句你自己的、具体的话；不要拐回你在等的人。")
    return Plan(
        intent="react", avoid=list(dict.fromkeys(avoid + scenery[:4])), forbid=forbid,
        must=must, max_chars=45, gate=True,
        content=content, content_is_fact=False,
        note="对方在说自己的事（天气、上班、累）。先接住他那件事（用你的话说，"
             "但要说到他提的那件事上），再给半句你自己的。不要照抄他的原话，"
             "不要用一句景物描写代替回应，也不要答成你上一轮的台词。",
    )


# ---------------------------------------------------------------- 表现（P0-1）
# 选择权归位：情绪/动作**不再由 4B 在首行 JSON 里自由选**，由意图确定。
# 理由：实测 4B 在两轮里都只选 neutral/thinking（最保守的两个），等于表情永远不变；
# 而「这句话该是什么情绪」本来是确定性的——问等谁就该担心，打招呼就该笑。
# 卡包里的枚举是白名单，这里只给映射，非法值由 persona.sanitize_directive 拦。
_EMOTION_BY_INTENT: dict[str, str] = {
    "greeting": "smile",
    "wait_who": "worried",
    "followup": "worried",
    "why_not_go": "worried",
    "why": "thinking",
    "doubt": "worried",
    "fear": "worried",
    "photo": "surprised",
    "choice": "thinking",
    "where": "thinking",
    "howlong": "thinking",
    "last_train": "neutral",
    "how_home": "neutral",
    "coffee": "smile",
    "coffee_who": "smile",
    "work_here": "neutral",
    "closing": "neutral",
    "habit": "neutral",
    "given_offer": "smile",
    "handover": "surprised",
    "let_alone": "neutral",
    "ask_cold": "neutral",
    # listen 是「听对方说话」的脸，她自己在说的时候不该摆：
    # counter / react / free 原来都映射成 listen，于是她张口答话时表情是倾听
    # （expression_match.py 的 R1 就是这么抓到的）。
    "counter": "neutral",
    "clarify": "thinking",
    "self": "neutral",
    "name": "neutral",
    "recall": "neutral",
    "closing": "neutral",
    "react": "neutral",
    "question": "thinking",
    "volunteer": "smile",
    "free": "neutral",
    "off_topic": "thinking",
}

# 动作只给「能读出意图」的那几个；其余交出 control return None，保留本拍的既定动作。
_ACTION_BY_INTENT: dict[str, str] = {
    "photo": "show_photo",
    "coffee": "sip_coffee",
    "coffee_who": "sip_coffee",
    "work_here": "lean_in",
    "closing": "look_window",
    "habit": "look_window",
    "given_offer": "lean_in",
    "handover": "show_photo",
    "ask_cold": "lean_in",
    "greeting": "lean_in",
    "wait_who": "look_window",
    "why_not_go": "lean_in",
    "fear": "lean_in",
    "counter": "lean_in",
    "clarify": "lean_in",
}


def reaction_for(plan: "Plan") -> tuple[dict, list[str]]:
    """本轮的表现指令（只返回需要覆盖的字段）。纯函数，不读卡包。"""
    out: dict = {}
    emo = _EMOTION_BY_INTENT.get(plan.intent)
    if emo:
        out["emotion"] = emo
    act = _ACTION_BY_INTENT.get(plan.intent)
    if act:
        out["action"] = act
    return out, []


# ---------------------------------------------------------------- 台词二次校正（问题 6 · 方案乙）
# 用户 2026-10-21 拍板：动作/神态**仍由框架决定**，但要用台词做一次二次校正。
# 为什么是这一层、而不是「让模型自己选」：4B 实测只会给 neutral/thinking 这两个最保守的
# 值，把选择权交回去等于表情永远不动（上一版验证过）。查表能保证「每句都有表情」，
# 但它看不见这一轮到底说了什么——所以再加一道**只读台词**的校正：
#     表里要掏照片，台词里根本没有「照片」这个词 → 不该掏，降级；
#     台词里提到雨/窗外/门口/咖啡 → 往对应的方向看一眼，而不是表里写的那个默认值。
# 纯函数（不读卡包，规则由调用方传入），同样的输入永远得到同样的输出，可单测、可复现。

_DIALOG_FIX_FIELDS = ("emotion", "action")


def _rule_hit(rule: dict, text: str, intent: str) -> bool:
    if not isinstance(rule, dict):
        return False
    want = rule.get("intent_any") or []
    if want and intent not in want:
        return False
    any_kw = rule.get("reply_any") or []
    if any_kw and not any(k and k in text for k in any_kw):
        return False
    unless_kw = rule.get("unless_reply_any") or []
    if unless_kw and any(k and k in text for k in unless_kw):
        return False
    # 既没有 reply_any 也没有 intent_any 的规则等于无条件命中：拒绝，避免一条空规则接管全部表现
    if not any_kw and not want:
        return False
    return True


def dialog_fix(reply: str, directive: dict, *, rules: list[dict] | None = None,
               intent: str = "", speaking: bool = True) -> tuple[dict, list[str]]:
    """用这一轮真实台词，对 intent 查表给出的 emotion/action 做一次校正。

    返回 (校正后的完整 directive 副本, fixes)。fixes 为空 = 台词和查表一致，不用改。
    - 每个字段各自取**第一条**命中的规则（先到先得），所以规则顺序 = 优先级。
    - `speaking=True` 时禁止 emotion=listen：她自己说话的时候不该摆倾听的脸
      （上一轮反馈里"她说话时表情在听"就是这个兜底路径漏出来的）。
    """
    out = dict(directive or {})
    fixes: list[str] = []
    text = reply or ""
    if speaking and out.get("emotion") == "listen":
        out["emotion"] = "neutral"
        fixes.append("说话时不允许 listen 表情 → neutral")
    if not text or not rules:
        return out, fixes
    decided: set[str] = set()
    for rule in rules:
        if len(decided) == len(_DIALOG_FIX_FIELDS):
            break
        if not _rule_hit(rule, text, intent):
            continue
        changed = False
        for fld in _DIALOG_FIX_FIELDS:
            if fld in decided:
                continue
            val = rule.get(fld)
            if not val:
                continue
            decided.add(fld)
            if out.get(fld) != val:
                fixes.append(f"{fld}: {out.get(fld)} → {val}（{rule.get('id', '?')}："
                             f"{rule.get('why', '台词校正')}）")
                out[fld] = val
    return out, fixes


def off_topic_plan() -> "Plan":
    """硬跑题（写代码/翻译/算题）也不许沉默：一句最短的、符合人设的挡回。"""
    return Plan(
        intent="off_topic", max_chars=24, gate=False,
        note="对方说的和这家店、这个夜晚无关（像在让你做别的事）。不要装作听懂了，"
             "也不要说教或解释；用一句最短的话承认你帮不上/没听懂，可以带一点不知所措。"
             "不要反问他，不要提问，不要说「我是AI」。",
        content="", content_is_fact=False,
    )


def plan_turn(text: str, *, facts: list[str] | None = None, new_facts: list[str] | None = None,
              recent: list[str] | None = None, is_open: bool = False, beat: int = 0,
              turn_no: int = 0, prev_contents: list[str] | None = None,
              prev_topics: list[str] | None = None) -> Plan:
    """给这一轮定计划。纯函数：同样的输入永远得到同样的计划（可单测、可复现）。"""
    plan = _plan_turn(text, facts=facts, new_facts=new_facts, recent=recent,
                      is_open=is_open, beat=beat, turn_no=turn_no,
                      prev_contents=prev_contents, prev_topics=prev_topics)
    prev_contents = list(prev_contents or [])

    # 跨轮去重：连问同一件事时，4B 会把上一轮原样再说一遍（实测逐字重复）。
    # 旧版用「content 前 6 字是否出现在上一轮台词里」判断，模型换个说法就对不上，
    # 等于永远不触发；现在直接比 content 是否与上一轮相同，命中就写进本轮指令。
    if plan.content and any(_norm(plan.content) == _norm(p) for p in prev_contents):
        plan.note += ("（上一轮你已经说过几乎一样的话——这一轮必须换一种说法，"
                      "补一个新的具体信息，别重复上一轮。）")

    # 同一个槽位连着问两次：换个角度，别换汤不换药。
    if plan.topic and plan.topic in (prev_topics or []):
        plan.note += "（同一个问题刚问过一次：这次补一个上一轮没有的具体细节。）"

    # 2026-10-20 晚：**删掉「每 3 轮硬塞一次反问」**。
    # 它是「结尾半句跑题问句」的直接来源：第 4 轮用户问「你是在等人吗」，
    # plan 已经答得很完整（「嗯。等一个人。说好十点半，现在快十二点了。」），
    # 但这条确定性要求逼 4B 再补一句，它就编出「这店的灯，还亮着吗？」——
    # 用户截图里"答非所问"最刺眼的一处。提示词第 8 条也同步改成
    # 「只在导演提示要求时反问」，单一事实源。
    # 现在 must_ask 只留给**槽位自己**要求交回话头的轮次（目前只有拒绝给号码那一支
    # 用 allow_question，不逼问、只允许问）。要恢复节奏感，应该按「他刚说了什么」
    # 生成具体追问，而不是按轮次发问号。
    return plan


def check_plan(plan: Plan, reply: str) -> list[str]:
    return plan.gate_check(reply)
