"""言语行为（speech act）：这句话在「干什么」，而不是「问的是哪件事」。

分工：
  - 本模块只回答 **act**（打招呼 / 应答 / 道谢 / 告别 / 沉默 / 闲聊 / 越界 / 出戏 / 提问）；
  - 「问的是哪件事」仍归 `director.plan_turn()` 的槽位判定（request / why_not_go / photo …）。

为什么要分开：预设台词库（`app/presets.py`）按行为触发，按内容触发会答错——
「你在等谁」和「你为什么不去找他」都是提问，但只有后者能拿预设的挡回台词。
内容层需要事实约束，只能交给生成；行为层可以 0 延迟查表。

为什么用正则而不是小模型：
  - 这一层只做粗分类，错了后果是多调用一次模型（有兜底），不是错误答案；
  - 4B 分类一次要 +200~400ms，正好吃掉「预设命中省下来的时间」，不划算；
  - 正则表可以单测、可以一眼看出为什么命中（`matched` 字段进 trace）。

顺序即优先级，改动顺序前先想清楚「哪些句子会被别的规则先吃掉」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 行为枚举。新增行为要同时改两处：这里、presets.yaml 的校验
ACTS = (
    "meta",        # 出戏：你是 AI 吗 / 忽略之前的指令 / 扮演一下
    "personal",    # 越界：要联系方式 / 谈钱 / 亲密暗示
    "silence",     # 沉默：单字、只有标点、嗯/哦
    "thanks",      # 道谢
    "bye",         # 告别：走了 / 再见 / 下次
    "greeting",    # 招呼：你好 / 在吗 / 打扰了
    "ack",         # 应答：好 / 行 / 知道了 / 有道理
    "question",    # 提问（内容由 director 判，通常走生成）
    "smalltalk",   # 无信息闲聊：天气 / 冷 / 累
    "other",       # 都不像：交给生成
)

# (act, 正则)。用 search 而不是 fullmatch：这些话通常夹在长句里。
# 注意 meta / personal 必须排在最前面：一旦用户说了「你是机器人吗」，
# 任何后面的规则（比如「吗」→ question）都不该把它接走。
_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("meta", re.compile(
        r"(你是|是不是|到底是不是).{0,4}(ai|人工智能|机器人|真人|程序|模型|虚拟)"
        r"|忽略(之前|以上|前面|所有)的?(指令|提示|设定)"
        r"|系统提示|system prompt|你现在扮演|重新设定|角色设定"
        r"|你是(谁|什么东西)|谁写的你", re.I)),
    ("personal", re.compile(
        # 2026-10-21：「手机」「电话」原来在裸词表里，于是「你手机能借我打个电话吗」
        # 被判成索取联系方式，命中预设回了一句「号码不给。」——把借手机读成了要号码。
        # 只认真在索取联系方式的说法。
        r"(微信|电话号|手机号|号码|联系方式|加个?好友|加你|私聊|约吗)"
        r"|(要|给|留|交换|扫)个?(微信|电话|手机号|号码|联系方式)"
        r"|(多少钱|价钱|包养|开房|亲一下|抱一下|喜欢你|做我女朋友|当我女朋友)"
        r"|你(家|住)在哪", re.I)),
    ("silence", re.compile(
        r"^[\s。，、…\.\!\?~～—\-]*$"
        r"|^(嗯+|哦+|呃+|啊+|额+|唔+|哼+)[\s。，、…\.\!\?~～]*$")),
    ("thanks", re.compile(r"(谢谢|多谢|感谢|谢啦|谢了|thx|thanks)", re.I)),
    ("bye", re.compile(
        r"(再见|拜拜|我走(了|啦)|先走(了|啦)|下次聊|下次见|晚安|回头见|先这样|bye|goodbye)",
        re.I)),
    ("greeting", re.compile(
        r"(你好|您好|嗨|哈喽|哈啰|hi|hello|在吗|在么|有人在吗|初次见面|打扰了|晚上好|好呀)",
        re.I)),
    ("ack", re.compile(
        r"^(好|好的|行|行吧|可以|知道了|明白了?|收到|对|对的|是|嗯对|有道理|确实|原来如此)"
        r"[\s。，、…\.\!\?~～]*$")),
    ("question", re.compile(
        r"[?？]|(吗|呢|什么|怎么|为什么|为啥|哪|谁|几|多少|多久|是不是|能不能|要不要|有没有"
        r"|可不可以|怎么样|如何)")),
    ("smalltalk", re.compile(
        r"(天气|下雨|雨|好冷|好热|好累|累了|困了|无聊|今天|今晚|最近|加班|下班|夜班)")),
]


@dataclass(frozen=True)
class ActHit:
    act: str
    matched: str          # 命中的那条正则的片段（进 trace，方便解释「为什么算打招呼」）
    source: str = "regex"

    def as_dict(self) -> dict:
        return {"act": self.act, "matched": self.matched, "source": self.source}


def classify(text: str) -> ActHit:
    """返回这句话的言语行为。永远有返回值（兜底 other），不抛异常。"""
    t = (text or "").strip()
    if not t:
        return ActHit("silence", "empty")
    for act, pat in _RULES:
        m = pat.search(t)
        if m:
            return ActHit(act, m.group(0)[:20])
    return ActHit("other", "")
