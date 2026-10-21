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
  「约好八点半，她一直没来」——后一个问题等于没回答。这不是模型弱，是结构上
  就没把「问的是哪件事」表示出来。现在先判**槽位**（回去方式/联系方式/手机/
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
    # 2026-10-22：**对方把手机借给她**——这是本场戏的分岔点，必须单独成槽。
    # 实测（mira-history-2a2eeba53425）：用户说「我的借你吧」「我不是说了借给你吗」，
    # 两轮都没命中任何槽（含「手机」的被 photo 吃成「我手机没电」，不含的被兜底成 react），
    # 于是导演层给的是「不要拐回你在等的人」——**直接压掉了这条剧情线该有的反应**，
    # 她只好复读「我手机七点就没电」。必须排在 photo / offer / request 之前：
    # 这几条都会先抢走「手机」「电话」「打」这些字。
    ("give_phone", re.compile(r"我的?借你|借给你|借我吧|我给你用|用我的?(手机)?(吧|啊|打)?|"
                              r"拿我的|手机给你|你拿去用|我这儿?有手机|我手机(还)?有电|"
                              r"^那?你?打吧|[你您]打吧|去打吧|用你(的)?(手机)?打")),
    ("photo", re.compile(r"手机|充电|没电|电量|打电话|拨号")),
    # 2026-10-22：**「她不会来了」这类判断**。实测用户连说两轮
    # 「是不是不会来了」「我感觉她不会来了」，两轮都落到 react，
    # 导演层给的指令是「不要拐回你在等的人」——她说自己等的人会不会来，
    # 这轮恰恰**必须**拐回去（那是整场戏的压力源）。这才是「自说自话」的主因。
    ("she_not_come", re.compile(r"(她|他|人家).{0,6}(不会|没|不)(来了|来)|不会来了|来不了|"
                                r"不来了|还没来|一直没来|是不是不来了|会不会不来")),
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
    ("last_train", re.compile(r"末班车|末班|最后一班|班车")),  # v14 故事里没有末班车，恒不命中
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
_NAME = "小雪"

# 卡包事实表里的具体锚点（与 cards/mira_cafe/card.yaml 的 character.facts 对齐）。
# 为什么在这里再抄一份：导演层的通用兜底需要一个「哪些词算具体事实」的白名单，
# 而 director.py 刻意不读卡包（不依赖 yaml、纯函数、可单测）。卡包改事实时这里要一起改。
_FACT_WORDS = ("相机", "胶片", "胶卷", "咖啡", "雨衣", "手机", "小雪", "老板娘",
               "八点半", "九点", "两年", "四十七", "第二杯", "黑咖啡")

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
    ("手机", "没电", "充电", "打不了", "联系"),
    ("咖啡", "苦", "糖", "喝", "杯", "凉"),
    ("怕", "害怕", "一个人", "老板娘", "人"),
    ("骗", "真的", "没骗", "信"),
    ("找", "去", "不知道", "问过", "哪"),
    ("等", "等人", "八点半", "小雪", "第二杯", "人"),
    ("疼", "冷", "累", "困", "饿"),
)


@dataclass
class Plan:
    """一轮的确定性计划 + 可机检的验收标准。"""

    intent: str = "default"
    must: list[str] = field(default_factory=list)      # 必须逐字出现的内容（名字等）
    avoid: list[str] = field(default_factory=list)     # 本轮软禁用的词（复读/意象）
    forbid: list[str] = field(default_factory=list)    # 硬边界：句中出现 → 整句弃用（零延迟）
    withhold: list[str] = field(default_factory=list)  # 本拍「还没到说的时候」的词（信息闸门）
    max_chars: int = 45
    note: str = ""                                     # 塞进提示词末尾的指令
    critical: bool = False                             # 关键轮：整段阻塞生成 + 校验
    fallback: str = ""                                 # 校验失败时的确定性兜底台词
    content: str = ""                                  # 这一轮要传达的内容（只给意思，措辞交模型）
    content_is_fact: bool = True                       # False = content 是「要做到的事」不是台词素材
    topic: str = ""                                    # 被问的那件事（机检标签）
    anchors: list[str] = field(default_factory=list)   # 回答里必须点到的话题词（机检）
    time_ok: set[str] = field(default_factory=set)     # 本轮额外允许的时间（对方刚说过的时间）
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
            # 2026-10-22：与「还没到说的时候」（self.forbid）撞车的词**不展示**。
            # 提示词一边说「他刚问的就是八点半这件事」、一边说「还不能说八点半」，
            # 模型只能二选一——实测它选说漏嘴，然后被删句，这一轮就白了。
            shown = [a for a in self.anchors[:4]
                     if not any(w and (w in str(a) or str(a) in w)
                                for w in (self.withhold or self.forbid))]
            if shown:
                lines.append("他刚问的是这件事：" + "、".join(shown)
                             + "。先用一句话正面回应它（用你自己的说法，不要照抄这几个字），"
                             "不许用天气/灯/雨/窗外代替回答。")
            else:
                lines.append("他刚问的是他那句话本身。先用一句话正面回应它（用你自己的说法），"
                             "不许用天气/灯/雨/窗外代替回答。")
        if self.must:
            lines.append(f"必须出现的内容：{'、'.join(self.must)}（原样说出来）")
        if self.must_ask:
            lines.append(self.ask_hint or "答完用半句话反问他一个具体问题，把话头交回去（不要问太私人的）。")
        if self.forbid:
            lines.append(f"绝对不能说：{'、'.join(self.forbid)}。")
        if self.avoid:
            lines.append(f"这轮禁用这些词：{'、'.join(self.avoid)}")
        lines.append(f"整轮不超过 {self.max_chars} 字，只写你会说出口的话。"
                     f"时间只用事实表里的说法，不要自己编钟点或时长。")
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
        # 本拍禁词不在这里判（2026-10-22）：句子里出现禁词已经由
        # main.push_sentence 整句弃用（不进 TTS、不上字幕，零延迟）。
        # 在这里再判一次只会触发「整段重写」——多一次模型调用（1.5–3s），
        # 而换掉的通常只是一句话。两者是同一个后果，后者贵十倍。
        for token in self.must:
            if token not in body:
                out.append(f"没有说出「{token}」")
        # 英文词泄漏：中文音色念英文非常出戏，且只修一个词，直接判违规重写最省事
        stray_latin = latin_words(body)
        if stray_latin:
            out.append(f"夹杂英文单词「{stray_latin[0]}」")
        # 时间白名单（问题 8 残项）：钟点/具体时长必须能在事实表或对方这句话里找到出处。
        # 放在首句闸门里也查（first=True）——实测那句「从早上七点开始……四个半小时」
        # 就落在第一句，只查整段的话已经念出去了才判，等于没查。
        allow, hint = _time_base()
        if allow:
            hard_bad, soft_bad = time_audit(body, allow | set(self.time_ok or ()))
            if hard_bad:
                out.append(f"时间「{hard_bad[0][0]}」不在事实表里（只说过：{hint}），"
                           f"不要自己编钟点或时长")
            for surf, _canon in soft_bad:
                note = f"时间白名单（软，未重写）：「{surf}」事实表里没有"
                if note not in self.notes:
                    self.notes.append(note)
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
        # 以下四项以前都是**硬判（→ 整段重写）**，2026-10-22 全部降为留痕：
        # 用户实测「改完比改之前更差」，主因就是这一串硬判把每一轮都拖进
        # 「判违规 → 再调一次模型（1.5–3s）→ 还不合格 → 念节点 mock」。
        # 这些判据本身没错，错在**用重写去治措辞**：措辞该由提示词和后处理
        # （删句/剥词）解决，重写只留给「必须出现的事实没出现」这类硬失败。
        # 硬失败只剩三条：空台词、must 没出现、英文词、编时间（见上）。
        window = (recent or [])[-5:]
        for prev in window[-3:]:
            if prev and _overlap(body, prev) > 0.8:
                self.notes.append("复读（软）：和最近说过的一句几乎一样")
                break
        head = _head_prefix(body)
        if len(head) >= 3 and any(head in _norm(p) for p in window if p):
            self.notes.append("复读（软）：开头半句和最近说过的一句一样")
        for prev in window[-3:]:
            pn = _norm(prev)
            if not pn:
                continue
            dup = next((c for c in _clauses(body) if c in pn), "")
            if dup:
                self.notes.append(f"复读（软）：「{dup}」上一轮刚说过")
                break
        if not (self.must_ask or self.allow_question) and re.search(r"[？?][」”'\"]?\s*$", body):
            self.notes.append("结尾反问（软）：她话少，答完更该收住")
        if _STAGE.search(body):
            self.notes.append("旁白（软）：台词里出现了括号/星号动作描写")
        if _SERVICE.search(body):
            self.notes.append("客服腔（软）")
        if not first:
            for bad in self.avoid:
                if bad in body:
                    self.notes.append(f"又用了「{bad}」（软，这轮禁用词）")
                    break
            clean = re.sub(r"\s", "", body)
            if len(clean) > self.max_chars + 20:
                self.notes.append(f"太长（软，{len(clean)} 字）")
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


# ----------------------------------------------------- 时间白名单（问题 8 残项）
#
# 为什么需要：4B 手里没有可用的时间锚点时**会自己造一个**。2026-10-21 实跑里
# 「说好十点半」被编成「从早上七点开始，我在这里坐了四个半小时」——句子通顺，
# 但和事实表直接打架，观众听到的是"她在编"，比语病更像故障。
# 修法不是再写一遍提示词（提示词里早就写了「不要编具体钟点」，服从率不够），
# 而是**机检**：台词里出现的钟点/具体时长，必须能在事实表（或对方刚说的话）里
# 找到出处，否则本轮判违规，走已有的「整段重写一次 → 仍不过用确定性兜底」。
#
# 刻意保留的模糊表达不算违规：「一会儿」「半天」「好几天」、≤30 分钟的短时长，
# 以及数量词（一杯、一张、两个人）。只卡「观众会当成剧情信息的时间」。

_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_CLOCK = "一二三四五六七八九十两"
_PERIOD = r"(?:凌晨|清晨|早上|上午|中午|下午|傍晚|晚上|夜里|半夜|深夜)"

_CLOCK_RX = re.compile(
    _PERIOD + r"?\s*(?P<h>[0-9]{1,2}|[" + _CN_CLOCK + r"]{1,3})\s*[点點]\s*"
    r"(?P<m>半|一刻|三刻|[0-9]{1,2}|[" + _CN_CLOCK + r"]{1,3})?")
_COLON_RX = re.compile(r"(?<![0-9])(?P<h>[01]?[0-9]|2[0-3])\s*[:：]\s*(?P<m>[0-5][0-9])(?![0-9])")
_DUR_RX = re.compile(
    r"(?P<n>[0-9]{1,2}|[" + _CN_CLOCK + r"]{1,3})\s*个?\s*(?P<half>半)?\s*"
    r"(?P<unit>个?小时|钟头|分钟|秒钟|秒|年|个月|月)")
# 没有数字的「半年 / 半小时 / 半个月」也得认：那不是模糊表达，是具体时长。
_BARE_DUR_RX = re.compile(r"(?P<pre>半)\s*个?\s*(?P<unit>小时|钟头|分钟|年|个月|月)")
# 口语填充时长：≤30 分钟不当剧情信息（「等我十分钟」不该被判成编时间）。
_DUR_FREE = frozenset({"t:2", "t:5", "t:10", "t:15", "t:30"})

_BASE: dict = {"ok": None, "hint": ""}


def _num(s: str) -> int | None:
    """中文/阿拉伯数字 → int（支持 十一 / 二十 / 二十三 / 两）。"""
    s = (s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if "十" in s:
        a, _, b = s.partition("十")
        tens = _CN_DIGIT.get(a, 1) if a else 1
        return tens * 10 + (_CN_DIGIT.get(b, 0) if b else 0)
    if len(s) == 1:
        return _CN_DIGIT.get(s)
    try:
        return int("".join(str(_CN_DIGIT[c]) for c in s))
    except (KeyError, ValueError):
        return None


def _clock_canon(h: int, m: int) -> str:
    return f"c:{(h % 24) * 60 + m}"


def _time_hits(s: str) -> list[tuple[int, str, str]]:
    """(位置, 原文片段, 规范化) —— 钟点、冒号时间、具体时长三种写法都收。"""
    s = re.sub(r"\s+", " ", s or "")
    hits: list[tuple[int, str, str]] = []
    for mo in _CLOCK_RX.finditer(s):
        h = _num(mo.group("h"))
        if h is None or h > 24:
            continue
        raw_m = mo.group("m")
        if not raw_m:
            # 没有分钟部分时只认 6 点以后：「有一点冷」「差一点」「快一点」不是时间。
            if h < 6:
                continue
            m = 0
        elif raw_m == "半":
            m = 30
        elif raw_m == "一刻":
            m = 15
        elif raw_m == "三刻":
            m = 45
        else:
            m = _num(raw_m)
            if m is None or m > 59:
                continue
        hits.append((mo.start(), mo.group(0).strip(), _clock_canon(h, m)))
    for mo in _COLON_RX.finditer(s):
        hits.append((mo.start(), mo.group(0).strip(),
                     _clock_canon(int(mo.group("h")), int(mo.group("m")))))
    for mo in _DUR_RX.finditer(s):
        n = _num(mo.group("n"))
        if n is None:
            continue
        val = n + (0.5 if mo.group("half") else 0)   # 四个半小时 = 4.5
        unit = mo.group("unit")
        if unit in ("小时", "个小时", "钟头"):
            hits.append((mo.start(), mo.group(0).strip(), f"t:{int(round(val * 60))}"))
        elif unit == "分钟":
            hits.append((mo.start(), mo.group(0).strip(), f"t:{int(round(val))}"))
        elif unit in ("秒", "秒钟"):
            hits.append((mo.start(), mo.group(0).strip(), f"s:{val:g}"))
        elif unit == "年":
            hits.append((mo.start(), mo.group(0).strip(), f"y:{val:g}"))
        elif unit in ("个月", "月"):
            hits.append((mo.start(), mo.group(0).strip(), f"mo:{val:g}"))
    # 没有数字的「半年 / 半小时 / 半个月」：半小时类落在 _DUR_FREE 里放行，
    # 半年/半个月是真时长，必须能对上事实表。
    for mo in _BARE_DUR_RX.finditer(s):
        surf = mo.group(0).strip()
        unit = mo.group("unit")
        if unit in ("小时", "钟头", "分钟"):
            hits.append((mo.start(), surf, "t:30"))
        elif unit == "年":
            hits.append((mo.start(), surf, "y:0.5"))
        elif unit in ("个月", "月"):
            hits.append((mo.start(), surf, "mo:0.5"))
    return sorted(hits)


def time_tokens(text: str) -> set[str]:
    """一段文字里所有「会被当成剧情信息的时间」→ 规范化集合。纯函数，可单测。"""
    return {c for _, _, c in _time_hits(text)}


def _clause_at(s: str, pos: int) -> str:
    """取 pos 所在的那个分句（按中文标点切）——判断"这个时间是在说谁的事"用。"""
    starts = [0]
    for mo in re.finditer(r"[，。！？；、,.!?;\n]", s):
        if mo.start() >= pos:
            break
        starts.append(mo.end())
    head = starts[-1] if starts else 0
    end = len(s)
    for mo in re.finditer(r"[，。！？；、,.!?;\n]", s):
        if mo.start() >= pos:
            end = mo.start()
            break
    return s[head:end]


# 「她自己的时间线」在说什么：等待 / 约好 / 坐着 / 上班 / 打烊 / 一直……
# 只有这类时长才和事实表（十点半、快十二点、三年前）存在被观众抓到矛盾的风险。
_SELF_TIME = re.compile(r"[我咱]|等+待?|约|说好|坐|开始|上班|下班|打烊|守|一直|来了|到场")


def time_audit(body: str, allow: set[str]) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """分成两档：该重写的（硬）/ 只留痕的（软）。

    为什么分档：历史 173 条真台词里，机检会响 7 次，其中 6 次是**环境铺垫**
    （「雨停了快一个小时」「这雨停了快三小时了」）——时间上无害，但按硬判会让
    每一轮都多付一次整段重写（+3~9s），首句延迟又回到上一轮被骂的位置。
    真正和事实表打架的是**钟点**（「早上七点」vs 十点半）和**她自己时间线上的时长**
    （「我在这里坐了四个半小时」）。钟点一律硬判；时长只在分句里有她自己时才硬判，
    其余发观测面板留痕、不进重写。这条分档规则本身在 tools/qa/test_timecheck.py 里有用例。
    """
    s = re.sub(r"\s+", " ", body or "")
    hard: list[tuple[str, str]] = []
    soft: list[tuple[str, str]] = []
    for pos, surf, canon in _time_hits(s):
        if canon in allow or canon in _DUR_FREE:
            continue
        if canon.startswith("c:"):
            hard.append((surf, canon))
            continue
        (hard if _SELF_TIME.search(_clause_at(s, pos)) else soft).append((surf, canon))
    return hard, soft


def time_offenders(body: str, allow: set[str]) -> list[tuple[str, str]]:
    """台词里「事实表没有出处、且会被观众当成剧情时间」的片段；空 = 通过。"""
    return time_audit(body, allow)[0]


def _time_base() -> tuple[set[str], str]:
    """角色事实表（卡包 facts + 各拍既定台词）里出现过的时间 → (集合, 人话提示)。

    懒加载：director 是纯逻辑模块，顶层 import persona 会绕成环
    （persona → cards，main → director + persona）。取不到就**失败放行**：
    宁可漏检一次，也不能因为白名单为空把「十点半」误杀成违规、每轮都白重写。
    """
    if _BASE["ok"] is not None:
        return _BASE["ok"], _BASE["hint"]
    blob: list[str] = []
    try:
        from . import persona

        for f in persona.CHARACTER.get("facts") or []:
            blob.append(str(f.get("say") or ""))
        for b in persona.BEATS or []:
            if isinstance(b, dict):
                for k in ("mock", "say"):
                    blob.append(str(b.get(k) or ""))
    except Exception:  # noqa: BLE001  纯逻辑模块被单独 import 时不报错
        blob = []
    hits = _time_hits(" ".join(blob))
    ok = {c for _, _, c in hits}
    seen: list[str] = []
    for _, surf, _ in hits:
        if surf not in seen:
            seen.append(surf)
    _BASE["ok"], _BASE["hint"] = ok, ("、".join(seen[:6]) or "—")
    return ok, _BASE["hint"]


def _plan_turn(text: str, *, facts: list[str] | None = None, new_facts: list[str] | None = None,
               recent: list[str] | None = None, is_open: bool = False, beat: int = 0,
               turn_no: int = 0, prev_contents: list[str] | None = None,
               prev_topics: list[str] | None = None) -> Plan:
    """给这一轮定计划。纯函数：同样的输入永远得到同样的计划（可单测、可复现）。

    beat 是**跳拍之后**的当前拍：内容要守卡包的「这一拍能松口什么」，否则
    提示词里「这一拍绝对不要说」和末尾的 plan 指令会互相打架，4B 会挑省力的那条
    （实测：第 2 拍就说出手机没电）。
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
        if "相机" in blob or "拍" in blob:
            intent = "handover"
        else:
            intent = "photo"   # 默认按「她想让你帮个忙 / 问手机」处理

    # 阿岸这个名字只有被直接问到时才允许出现一次；其它轮次硬拦。
    forbid = ["小雪"] if intent != "name" else []
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
            intent="name", must=["小雪"], avoid=avoid, forbid=[], max_chars=28, critical=True,
            content="小雪。……她叫小雪。",
            note="对方直接问了她等的人叫什么。只这一次，说出「小雪」；说完就收住，不要解释、不要继续讲她的事。",
            fallback="小雪。……大学室友。",
        )

    if intent == "followup":
        # 问「后来 / 有没有消息」：absence 拍（beat>=4）才允许说「再没出现」；
        # 更早的拍只说「没消息」，不提照片、不提三年（守卡包 allow）。
        content = ("还没消息。……我手机没电，也问不了。" if beat >= 6
                   else "还没消息。……她一向不迟到。")
        return Plan(
            intent="followup", avoid=avoid, forbid=forbid, max_chars=42, gate=True,
            topic="他后来有没有再联系（后来的事）",
            anchors=["后来", "再没", "没消息", "没联系", "没回来", "不知道"],
            content=content, fallback=content,
            note="对方在追问「后来」——问的是他**有没有再出现、有没有消息**，"
                 "不是问你要不要给号码，也不是让你再说一遍手机。先直接答这件事"
                 "（再没出现 / 没消息），再补半句你现在的样子。不要拿灯、雨、地垫岔开。",
        )

    if intent == "choice":
        # 问「你打算怎么办 / 照片寄不寄」：答案落在**决定**上（还没决定、先留着），
        # 不是把照片再描述一遍。卡包 choice 拍也要求不自己下结论、把选择抛回去。
        content = ("还没决定。……等到打烊吧，再等等。" if beat >= 12
                   else "还没决定。……先坐着吧。")
        return Plan(
            intent="choice", avoid=avoid, forbid=forbid, max_chars=42, gate=True,
            topic="你打算怎么办 / 还等不等",
            anchors=["决定", "还没", "等", "走", "再想想", "坐着"],
            content=content, fallback=content,
            note="对方问的是**你打算怎么办**（还等不等），不是让你再描述一遍小雪。"
                 "先答「还没决定」，再给半句打算（再等等 / 等打烊）；不要自己把结局定下来。",
        )

    if intent == "she_not_come":
        # 内容随拍推进：早拍还在自我安慰，中间只能说不知道，借到电话之后转向不安。
        # 三条都不给结论——卡包 boundaries 明写「不要给出小雪不来了/出事了这类结论」。
        if beat >= 8:
            content = "……我还是不放心。她不是不接电话的人。"
        elif beat >= 5:
            content = "不知道。……她那边地势低。"
        else:
            content = "……应该快了。她说下雨也来。"
        return Plan(
            intent="she_not_come", avoid=avoid, forbid=forbid, max_chars=40, gate=True,
            topic="她等的那个人会不会来",
            anchors=["不知道", "应该", "快了", "地势", "下雨", "放心", "等", "来"],
            content=content, fallback=content,
            note="对方在替你担心「她会不会不来」。先接住这句担心（用你的话说，别只回一个「嗯」），"
                 "再给一句你自己的判断或自我安慰。**不许给结论**（不说「她不会来了」"
                 "「她出事了」），也不许用天气、灯、窗外代替回答。",
        )

    if intent == "give_phone":
        # content 由 _apply_node 用**当前节点的 mock** 覆盖（作者写的本拍台词就是方向）。
        # 这里给的是节点未知时也能用的兜底：接过手机、犹豫一下、不要推第三轮。
        return Plan(
            intent="give_phone", avoid=avoid, forbid=forbid, max_chars=34, gate=True,
            topic="对方要把手机借给你打电话",
            anchors=["借", "打", "电话", "手机", "可以", "麻烦", "谢谢"],
            content="……可以吗？真的就一个电话。",
            content_is_fact=True,
            fallback="……可以吗？真的就一个电话。",
            note="对方**已经把手机递过来了**。不要再说「我手机没电」——那件事已经说过，"
                 "对方正是为此才借你手机。先接住这个动作（可以吗 / 谢谢 / 真的就一个电话），"
                 "再给半句犹豫；不要假客气推第三轮，也不要自己先拨号。",
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
        # v14：照片道具整组删除，这个槽改成「问手机 / 想帮忙联系」。
        # 她七点起手机就没电，所以答案是「打不了」，但这个答案本身要留一点余地。
        if beat >= 6:
            content = "我手机没电了。……从七点开始。我知道该打个电话。"
        else:
            content = "手机没电了。……从七点就没了。"
        return Plan(
            intent="photo", avoid=avoid, forbid=forbid, max_chars=45, gate=True,
            topic="手机 / 能不能联系上小雪",
            anchors=["手机", "没电", "充电", "打不了", "七点", "联系"],
            content=content, content_is_fact=True,
            fallback="手机没电了。……从七点就没了。",
            note="对方在问你手机 / 能不能联系上小雪。老实说手机没电（七点起），"
                 "但**不要自己开口借手机**——这个要求必须对方提出来。",
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
                 "不要说「不是」，不要扯到手机没电。",
        )

    if intent == "why_not_go":
        # 问「为什么不去找她」——槽位是「不去的理由」，不是「为什么在这里等」。
        # 2026-10-22：这里原来答的是「不知道他在哪……三年了，一直没找到」，
        # 是 v13（阿岸线、三年）的文案；v14 是「小雪 / 两年 / 手机没电」，
        # 她根本**知道**小雪在哪（约好了这家店），不去的理由是联系不上。
        content = ("不知道她那边什么情况。……我手机没电，联系不上。"
                   if beat >= 5 else
                   "只知道约的这家店。……别的联系方式也没有。")
        return Plan(
            intent="why_not_go", avoid=avoid, forbid=forbid, max_chars=45, gate=True,
            topic="为什么不去找她",
            anchors=["不知道", "联系", "没电", "地址", "店", "号码"],
            content=content, fallback=content,
            note="注意：对方问的是「你**为什么不去找她**」，不是「你为什么在这里等」。"
                 "先回答「不去的理由」（只知道约的这家店 / 手机没电联系不上），"
                 "不要说等了多久，不要给结论。",
        )

    if intent == "doubt":
        # 2026-10-22：这里原来的后半句是「等到快十二点了」——那是 v13（阿岸线、
        # 23:47 打烊）的时间。v14 换成 20:30 开场的闺蜜线之后，这句话既不合场景
        # 时钟，也会被时间白名单判成编钟点，白重写一轮。现在只用卡包里真有的
        # 「八点半」（clock.start 是 20:30，但约定时间就是八点半）。
        content = "没骗你。我就是在等人。" if beat < 1 \
            else "没骗你。我就是在等人。……说好八点半的。"
        return Plan(
            intent="doubt", avoid=avoid, forbid=forbid, max_chars=40, gate=True,
            topic="你是不是在骗我",
            anchors=["骗", "没骗", "真的", "信", "等", "八点半"],
            content=content,
            fallback=content,
            note="对方在怀疑你说谎。第一句必须正面否认（「没骗你」），再给一个具体事实"
                 "（你真的在等人 / 说好八点半）。不要用「不是」两个字糊过去。",
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
        # v14：故事里没有末班车了。这个槽保留形状，但答案改成「店要关了，我还没打算走」。
        content = "没看车。……老板娘还没关灯，我再坐会儿。"
        return Plan(
            intent="last_train", avoid=avoid, forbid=forbid, max_chars=40, gate=True,
            topic="怎么回去 / 还坐多久",
            anchors=["车", "回去", "走", "关灯", "坐"],
            content=content, fallback=content,
            note="对方问你打算怎么回去 / 还坐多久。你说没看车，"
                 "老板娘还没关灯，你想再坐一会儿——还没打算走。",
        )

    if intent == "howlong":
        return Plan(
            intent="howlong", avoid=avoid, forbid=forbid, max_chars=38, gate=True,
            topic="等了多久",
            anchors=["八点半", "九点", "点", "小时", "久"],
            content="约好八点半。……现在快九点了。",
            fallback="约好八点半。现在快九点了。",
            note="对方在问等了多久。给时间：约好八点半，现在快九点了。直接答，不要绕。",
        )

    if intent == "where":
        return Plan(
            intent="where", avoid=avoid, forbid=forbid, max_chars=40, gate=True,
            topic="她为什么没来 / 在哪",
            anchors=["不知道", "哪", "没来", "堵", "找", "两年"],
            content="不知道。……她那边地势低。" if beat < 6 else "不知道。……两年没见，我也不知道她那边什么情况。",
            fallback="不知道。她那边地势低。",
            note="对方在问小雪为什么没来 / 她人在哪。你确实不知道——手机没电，联系不上。"
                 "直说不知道，可以补半句你猜的（她那边地势低）。不要编结论。",
        )

    if intent == "why":
        return Plan(
            intent="why", avoid=list(dict.fromkeys(avoid + ["雨", "天气", "夜色"])), max_chars=42,
            forbid=forbid, gate=True, topic="为什么这么晚还在（等）",
            anchors=["等", "八点半", "没来", "约好", "手机"],
            content="约好八点半，她一直没来。我手机没电，联系不上。",
            fallback="约好八点半，她一直没来。",
            note="对方在问你为什么在这种天气里等。第一句必须给一个**具体原因**：你跟小雪约好了八点半，"
                 "她一直没来，而你手机没电联系不上。不要用「不是」开头，也不要用灯、后门、老板娘岔开。",
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
            content = "嗯。等一个人。说好八点半，现在快九点了。"
            anchors = ["等", "一个人", "人", "八点半", "第二杯"]
        return Plan(
            intent="wait_who", avoid=avoid, forbid=forbid, max_chars=45, gate=True,
            topic="你是不是在等人 / 等谁",
            anchors=anchors, content=content, fallback=content,
            note="对方在问你是不是在等人。承认在等人就够了（「等一个人」这个程度），"
                 "但不要说她叫什么、不要提两年没见的事。"
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
    content = (f"他刚说的那句（关键词：{keys}）是冲你说的。先用你自己的话对这句话做出"
               "反应（不要照抄他的词），再补半句你自己的、具体的话。")
    return Plan(
        intent="react", avoid=list(dict.fromkeys(avoid + scenery[:4])), forbid=forbid,
        must=must, max_chars=45, gate=True,
        content=content, content_is_fact=False,
        # 2026-10-22 修：原来这条 note 写死「对方在说自己的事（天气、上班、累）」——
        # 那是在**替模型描述用户说了什么**，而用户实际说的是「我不认识你，别跟我说话」
        # （拒绝/表态）或「你说话怎么莫名其妙的」（质问）。模型照着这段错误的描述
        # 去答，自然接不住，于是出口闸判不合格、整段重写、最终落 mock——
        # 用户听到的「自说自话、念台词」就是这条链。只描述该怎么反应，不预设内容。
        note="对方刚说了一句冲你来的话（表态 / 拒绝 / 评价）。先对这句话本身做出反应："
             "认下、不认、或者挡回去，用你自己的话说，再补半句。"
             "不要改成聊天气景物，也不要答成你上一轮的台词。",
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
    "photo": "check_phone",
    "coffee": "sip_coffee",
    "coffee_who": "sip_coffee",
    "work_here": "lean_in",
    "closing": "look_window",
    "habit": "look_window",
    "given_offer": "lean_in",
    "handover": "lean_in",
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


# ---------------------------------------------------------------- 节点约束（真相源）
def node_forbid_words(node: dict | None) -> list[str]:
    """本拍不能说出口的具体词（卡包 story.nodes[].forbid_words）。

    为什么不从 forbid 那句自然语言里现抽：卡包里的 forbid 是写给作者看的一句话
    （「不要说在等人；不要提手机」），机检需要的是**能逐字匹配的词**。让作者显式
    写一次词表，比在代码里猜「等一个人」算不算「等人」可靠，也换卡即换规则。
    """
    out: list[str] = []
    for w in (node or {}).get("forbid_words") or []:
        w = str(w or "").strip()
        if w and w not in out:
            out.append(w)
    return out


def apply_node(plan: Plan, node: dict | None, *, first_entry: bool = False) -> Plan:
    """把「这一拍」的边界压到导演层的计划上。

    ## 为什么必须有这一层（2026-10-22 实测）

    `_plan_turn` 那张 30 个槽的意图表是**线性七拍时代**写的：它给每个槽配一句
    写死的 content，优先级高于卡包的节点。v14 换成 story 节点图之后，两套东西
    各说各的，而导演层永远赢：

      · arrival 节点的 forbid 明写「不要说在等人；不要提手机」；用户第一句
        「这么晚了，你一个人？」被判成 wait_who，content 直接给
        「嗯。等一个人。说好八点半，现在快九点了。」——**第一句就把底牌交了**。
        用户反馈的「一开始就这么热情、什么都往外说」就是这一句的连锁反应。
      · phone_dead 该说手机没电；用户「那你打吧」被判成 react，content 换成
        通用反应，模型只能从最近台词里抓，于是复读「我手机没电了」。
      · 用户「我的借你吧」落到 react，content 写着「**不要拐回你在等的人**」，
        把整条借电话的剧情线按了下去。

    这一层只做四件事，不重写那张表：

      1) 节点的 forbid_words → 出口闸门的**硬禁词**（说出口就会被 rewrite/兜底）；
      2) content 里一旦命中硬禁词，整条 content **作废**——宁可让模型按节点的
         allow 自己说，也不让它照着一条越界的台词演（作废后 main.py 的兜底会
         自动落到本节点作者写的 mock 上，安全）；
      3) 节点的 goal / allow / forbid 进 note **最前面**，压过意图表的措辞指令；
      4) 借手机这类「动作已经发生」的轮次，content 直接用本节点 mock 当方向。
    """
    if not isinstance(node, dict) or not node.get("id"):
        return plan

    words = node_forbid_words(node)
    # 信息闸门（2026-10-22 重做）：节点的 forbid_words 是「这一拍还没到说的时候」的
    # 信息，**不是**「说了就重写」的违规词。两者区别很关键：
    #   · 作为 withhold：写进提示词（他问起要挡回去）+ 句中命中就删掉那一句（零延迟）；
    #   · 作为 forbid 硬判：会触发整段重写（+1.5~3s），而上一条实测就是这样把
    #     每一轮都逼进「重写→仍不合格→念节点 mock」。
    # 另外它**不再与 anchors 求差集**：那样等于「他问什么就把什么从禁词表里放开」，
    # 一被追问就全交代了。要守的是信息，展示层冲突由 prompt_block 过滤 anchors 解决。
    if words:
        plan.withhold = list(dict.fromkeys(list(plan.withhold or []) + words))
        plan.forbid = list(dict.fromkeys(list(plan.forbid or []) + words))

    # 1b) note 里也可能命令她说出禁词。实测 wait_who 槽的固定提示词是
    #     「第一句必须给一个具体原因：你跟小雪约好了八点半，她一直没来，而你手机
    #     没电联系不上」——而本拍禁词正是「小雪 / 没电 / 电话」。note 在提示词里
    #     优先级最高（就在【本轮怎么走】第一段），留着它等于每一轮都逼模型违规，
    #     然后被闸门判死、整段重写、最终落 mock。含禁词的句子整句删掉：
    #     宁可让她按节点 allow 自己说，也不留一条"必须违规"的指令。
    if plan.note and words:
        kept: list[str] = []
        for sent in re.split(r"(?<=[。！？；\n])", plan.note):
            if not sent.strip():
                continue
            hit = next((w for w in words if w in sent), "")
            if hit:
                plan.notes.append(f"提示词命令她说「{hit}」（本拍禁词），已删该句")
                continue
            kept.append(sent)
        plan.note = "".join(kept).strip()

    # 1c) avoid（「这轮禁用这些词」）与节点 allow 冲突时解除。实测 arrival 的 allow
    #     明写「可以说雨很大」，而意图表给 wait_who 的 avoid 是「雨、天气、夜色」——
    #     同一段提示词里又允许又禁止，整段复核必然判「又用了雨」再重写一轮
    #     （logs/turns.jsonl 里这条理由出现过 4 次）。
    allow_text = str(node.get("allow") or "") + str(node.get("mock") or "")
    if plan.avoid and allow_text:
        freed = [w for w in plan.avoid if w and w in allow_text]
        if freed:
            plan.avoid = [w for w in plan.avoid if w not in freed]
            plan.notes.append("本拍 avoid 与节点 allow 冲突，已解除：" + "、".join(freed))

    # 4) 「对方已经把手机借给你」：本拍的 mock 就是作者写的那句正确反应。
    #    2026-10-22 实测两种落点都要处理：
    #    · 还在手机线（phone_dead / phone_offer）：mock 就是「可以吗？真的就一个电话」；
    #    · 已经打完了（call_ok / release / ending…）：用户说「我不是说了借给你吗」
    #      只是在确认/抱怨，她该按**当前这一拍**说话，不能再接一句「可以吗」。
    if plan.intent == "give_phone":
        mock = str(node.get("mock") or "").strip()
        if mock:
            plan.content = mock
            plan.fallback = mock
            plan.content_is_fact = True
            if not str(node.get("id") or "").startswith("phone"):
                plan.notes.append(
                    f"借手机这件事已经过去了（现在在 {node['id']}），先应一句就按本拍说")

    # 5) scripted 节点：这一拍的台词是**事件本身**（她在打电话 / 已经到结局），
    #    不该被「对方刚问了什么」改写。卡包给它 mock + scripted: true。
    if node.get("scripted"):
        mock = str(node.get("mock") or "").strip()
        if mock:
            plan.content = mock
            plan.fallback = mock
            plan.content_is_fact = True
            plan.note = ("这一拍是在演一件正在发生的事，不是回答对方的问题："
                         "只说她此刻真的在说的那几句，短、断续。\n" + plan.note)

    # 2) content 越界 → 作废。必须清掉 fallback：兜底台词是同一句越界的话，
    #    留着它等于"重写失败后照样念出来"。
    if plan.content and words:
        hit = next((w for w in words if w in plan.content), "")
        if hit:
            plan.notes.append(
                f"本拍（{node['id']}）不能说「{hit}」，导演层的固定台词已作废，改由节点 allow 驱动")
            plan.content = ""
            plan.content_is_fact = False
            plan.fallback = ""

    # 3) 节点是这一拍的真相：目标/能说什么/不能说什么放最前面
    def _one_line(v: object) -> str:
        return " ".join(str(v or "").split())

    title = _one_line(node.get("title")) or str(node.get("id"))
    goal = _one_line(node.get("goal"))
    allow = _one_line(node.get("allow"))
    forbid = _one_line(node.get("forbid"))
    deflect = _one_line(node.get("deflect"))
    # 信息闸门（2026-10-22）：节点不只是"这一拍该说什么"，更是**她此刻肯让人知道到
    # 哪一层**。用三层写法把它讲清楚，模型才知道"没到说的时候"该怎么处理：
    #   可以让人知道的（allow）→ 这一层能松口的事实；
    #   还没到说的时候（forbid）→ **不是「不能说」，而是「不会主动说，被问要挡」**；
    #   怎么挡（deflect）      → 一句可执行的挡法，否则模型只能沉默或编造。
    # 最后一句「一次只说一层」是治「一上来就把底牌全交」的那半句：3–5 分钟的戏
    # 靠的是同一件事分三次松口，而不是第一轮把名字、时间、原因一口气说完。
    head = f"你现在在「{title}」这一拍" + (f"：{goal}" if goal else "。")
    if allow:
        head += f"\n这一拍你可以让人知道的：{allow}"
    if forbid:
        head += f"\n还没到说的时候：{forbid}"
    if deflect:
        head += f"\n他要是问到这些，你就这样挡过去：{deflect}"
    head += "\n一次只松一层，别把后面几拍的事一起说完；已经说过的话不要再说一遍。"
    plan.note = head + ("\n" + plan.note if plan.note else "")
    if first_entry and words:
        plan.notes.append(f"本拍硬禁词：{'、'.join(words)}")
    return plan


def plan_turn(text: str, *, facts: list[str] | None = None, new_facts: list[str] | None = None,
              recent: list[str] | None = None, is_open: bool = False, beat: int = 0,
              turn_no: int = 0, prev_contents: list[str] | None = None,
              prev_topics: list[str] | None = None,
              node: dict | None = None, first_entry: bool = False) -> Plan:
    """给这一轮定计划。纯函数：同样的输入永远得到同样的计划（可单测、可复现）。

    node = 当前故事节点（卡包 story.nodes 里那一条）。传进来后，节点的
    goal/allow/forbid 会成为**硬约束**，见 apply_node 的说明。
    """
    plan = _plan_turn(text, facts=facts, new_facts=new_facts, recent=recent,
                      is_open=is_open, beat=beat, turn_no=turn_no,
                      prev_contents=prev_contents, prev_topics=prev_topics)
    plan = apply_node(plan, node, first_entry=first_entry)
    # 对方自己说过的时间允许她复述（「你不是说七点来的吗」）：把他这句话里的时间
    # 并进白名单，否则她会因为「引用了对方说过的时间」被判编造。
    plan.time_ok = time_tokens(text)
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
