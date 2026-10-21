"""app/story.py — 故事节点图引擎。

为什么单独一层：原来的 `beats` 是线性七拍 + 「话题命中够两次才推进」，用户实测
「故事推不动」「无法结束」「没有分支」。节点图把这三件事一次解决：

  · 推进  —— clock 硬闸门（到点必走）+ goto 条件可提前跳，不再等"用户说对话"
  · 结束  —— ending 节点 + clock.end 强制进结局
  · 分支  —— flags（offered_phone / call_result）决定 goto 走哪条

所有判定都是**纯函数**：不调模型、不看台词生成结果，所以 0 延迟、可单测。
理解用户那句话（语义匹配）是 understanding 层的事，这里只消费它的结论。
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------- 关于"演示钩子"（已删除）
# 2026-10-21：这里原本有两个只在录屏时用的强制钩子——
#   · MIRA_FORCE_CALL=ok|fail      把 phone_call 的 70/30 随机结果钉死
#   · MIRA_MINUTES_PER_TURN=3      把剧情时钟步长从 2.5 改成 3，少走两轮
# 用户看完 v15 成片后的第 5 条意见：「**结局要是自然对话触发的，而不是你手动触发的**」。
# 钩子就是"手动触发"的入口，所以整段删除，连"不设就恢复原逻辑"都不留 ——
# 留着就还有一条能绕过剧情的暗门，而演示片要证明的正是"走剧情能自然到结局"。
# 现在结局只有三个入口，全部来自对话本身：
#   1) 用户说「我要走了」（story.leave_triggers）      → ending_leave
#   2) lights_dim 节点按 offered_phone 分支            → ending_friend / ending_stranger
#   3) clock 到 21:00 的强制收束                       → ending_friend / ending_stranger
import os  # noqa: F401  （保留导入：其它模块/测试可能沿用本模块的 os 引用习惯）


def set_force_call(value: Any = None) -> None:
    """已删除（见上）。留一个显式报错的桩，避免"静默不生效"这种更难查的坑。"""
    raise RuntimeError("set_force_call 已移除：结局必须由自然对话触发（见 app/story.py 顶部注释）")


def set_minutes_per_turn(value: Any = None) -> None:
    """已删除（见上）。"""
    raise RuntimeError("set_minutes_per_turn 已移除：剧情节奏取卡包 clock.minutes_per_turn")


def parse_hhmm(s: str) -> int:
    """'20:30' -> 分钟数（自 00:00）。坏格式返回 0。"""
    try:
        h, m = str(s).strip().split(":")
        return int(h) * 60 + int(m)
    except Exception:  # noqa: BLE001
        return 0


def fmt_hhmm(minutes: int) -> str:
    minutes = int(minutes) % (24 * 60)
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


@dataclass
class Step:
    """一次推进的结果。"""

    node: str = ""            # 推进后所在节点
    moved: bool = False       # 是否换节点
    why: str = ""             # 人话原因（进 trace）
    ending: str | None = None  # 非空 = 到达结局，kind = friend / stranger
    forced: bool = False      # 是否由 clock 强制


@dataclass
class StoryState:
    story: dict
    clock_cfg: dict
    nodemap: dict[str, dict] = field(init=False)
    node_id: str = field(init=False)
    minutes: int = field(init=False)          # 当前场景时钟（绝对分钟数）
    turns_in_node: int = field(init=False)
    visited: list[str] = field(init=False)
    flags: dict[str, Any] = field(init=False)
    ending: str | None = field(init=False)
    rng: random.Random = field(init=False, repr=False)
    _fired: set = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.nodemap = {n["id"]: n for n in self.story.get("nodes") or []}
        self.node_id = self.story.get("start") or next(iter(self.nodemap))
        self.minutes = parse_hhmm(self.clock_cfg.get("start") or "20:30")
        self.turns_in_node = 0
        self.visited = [self.node_id]
        self.flags = {"offered_phone": False, "call_result": None, "warmth": 0}
        self.ending = None
        self.rng = random.Random()
        self._fired = set()

    # ------------------------------------------------------------ 查询
    @property
    def node(self) -> dict:
        return self.nodemap.get(self.node_id) or {}

    @property
    def end_minutes(self) -> int:
        return parse_hhmm(self.clock_cfg.get("end") or "21:00")

    def clock_str(self) -> str:
        return fmt_hhmm(self.minutes)

    def is_ending(self) -> bool:
        return self.ending is not None or bool(self.node.get("ending"))

    def snapshot(self) -> dict:
        return {
            "node": self.node_id,
            "title": self.node.get("title"),
            "clock": self.clock_str(),
            "turns_in_node": self.turns_in_node,
            "flags": dict(self.flags),
            "ending": self.ending or self.node.get("ending"),
        }

    # ------------------------------------------------------------ 每轮推进
    def tick(self, minutes_per_turn: float | None = None) -> None:
        """一轮开始：场景时钟前进。

        这是「3–5 分钟」的度量单位：clock 不随真实时间走（否则用户发呆也算），
        而是随**对话轮数**走，这样 12–15 轮正好落在 20:30–21:00。

        2026-10-22：这里**不再**给 turns_in_node +1，改成由 end_turn() 在一轮的
        goto 求值之后做。原因是一个真实的 bug：resolve() 在 tick() 之后执行，而
        卡包每个节点的兜底 goto 都是 `turn_gte: 1`，tick 先把 turns_in_node 加到了 1，
        于是 `1 < 1` 为假 —— **条件当场就满足，每轮必跳**。实测把用户那 9 轮真实
        对话喂进来，arrival→order→waiting→…→phone_call 一路跑完，用户说的每句话
        都没参与决定。节点图退化成计时器，这就是「剧情自己在跑、跟对话无关」。
        """
        step = float(minutes_per_turn if minutes_per_turn is not None
                     else (self.clock_cfg.get("minutes_per_turn") or 2.5))
        self.minutes = min(self.end_minutes, self.minutes + step)

    def end_turn(self) -> None:
        """一轮真正结束：本节点轮数 +1。

        **必须在 resolve() 之后调用。** 这样 goto 里的 `turn_gte: N` 读到的才是
        「进入本节点以来**已完成**的轮数」：turn_gte 1 = 在本节点待满一轮，第二轮才走。
        """
        self.turns_in_node += 1

    def forced(self) -> Step | None:
        """clock 强制节点：到点必到，不看用户聊了什么。

        每个强制点**只触发一次**（`_fired` 记 at 值）。不记的话会死循环：
        20:57 时「20:48 phone_dead」和「20:56 lights_dim」两条都满足，
        切过去又被切回来，节点在两者之间反复横跳——实测就是这个卡住。
        """
        now = self.minutes
        pending = []
        for f in self.clock_cfg.get("forced") or []:
            at = parse_hhmm(f.get("at") or "")
            if now < at or at in self._fired:
                continue
            pending.append((at, f))
        if not pending:
            return None
        pending.sort(key=lambda x: x[0])
        at, f = pending[0]
        self._fired.add(at)
        target = f.get("node")
        why = f.get("why") or "到点强制推进"
        if target == "__ending__":
            kind = self._ending_kind()
            self.ending = kind
            return Step(node=self.node_id, moved=False, ending=kind, why=why, forced=True)
        if target and target != self.node_id:
            # 2026-10-22：**强制点不许把剧情往回送**。卡里 20:48 的强制点是
            # 「必须说出手机没电」，用户可能在第 5 轮就因为一句「要不要打电话」
            # 提前跳到了 phone_dead（问句加速）——这时候 _fired 里没有 20:48 这条，
            # 到 20:50 时 forced 会把她从已经走到的 phone_call **拖回 phone_dead**。
            # 实测回放里 turn7 phone_call → turn8 phone_dead → turn9 phone_call，
            # 剧情在自己的节点上来回跳，观众看到的就是「她又开始说手机没电」。
            # 已经发生过的事不再发生：节点访问过就只记 fired、不动人。
            if target in self.visited:
                return Step(node=self.node_id, moved=False, why=why, forced=True)
            self.node_id = target
            self.turns_in_node = 0
            self.visited.append(target)
            kind = self.node.get("ending")
            if kind:
                self.ending = kind
            return Step(node=target, moved=True, ending=kind, why=why, forced=True)
        return Step(node=self.node_id, moved=False, why=why, forced=True)

    def _ending_kind(self) -> str:
        """结局种类由 flags 决定（卡里 story.rules 是人话说明，这里是对应的实现）。"""
        if self.flags.get("offered_phone"):
            return "friend"
        return "stranger"

    # ------------------------------------------------------------ 跳转
    @staticmethod
    def _is_specific(when: dict) -> bool:
        """这条 goto 是**语义**条件（看用户说了什么）还是**进度**条件（只看轮次/时钟）。

        ask_any / flag / roll / clock_gte / node 属于语义类，必须排在全表加速**之前**判：
        本节点自己声明的跳转优先于「别的节点说这个词也会跳」。
        """
        return any(k in when for k in ("ask_any", "flag", "roll", "clock_gte", "node"))

    def _jump(self, to: str | None, why: str) -> Step:
        if not to or to == self.node_id:
            return Step(node=self.node_id)
        self.node_id = to
        self.turns_in_node = 0
        self.visited.append(to)
        kind = self.node.get("ending")
        if kind:
            self.ending = kind
        return Step(node=to, moved=True, ending=kind, why=why)

    def _accelerate(self, ask_hits: set[str]) -> Step | None:
        """全表问句加速：本节点的 goto 没吃到这句话，但卡里**别的节点**声明了
        「说这句话就该跳到那里」。

        2026-10-22 加。原本只有当前节点的 ask_any 生效，实测后果很具体：
        用户第 5 轮说「你要不打电话问问？」，此时节点停在 two_years（它只写了一条
        `turn_gte: 1` 的兜底），这句话被丢掉；而真正声明了「电话 → phone_dead」的
        rain_worry 已经在上一轮被兜底跳过去了。**用户说出了一句能推进剧情的话，
        程序却没听见**——这正是「剧情推不动/自说自话」的主因之一。

        范围限制在当前节点之后 4 个节点内：
          · 不后退，剧情不会被打回去；
          · 不跳太远 —— 否则 arrival 里一句「手机」能直接把人送到 phone_call，
            中间五拍的铺垫全没了（实测过一次，观感是「剧情突然跳到结尾」）。
        """
        if not ask_hits:
            return None
        order = [n.get("id") for n in self.story.get("nodes") or []]
        try:
            cur = order.index(self.node_id)
        except ValueError:
            return None
        for i in range(cur + 1, min(cur + 5, len(order))):
            nid = order[i]
            n = self.nodemap.get(nid) or {}
            for g in n.get("goto") or []:
                kws = (g.get("when") or {}).get("ask_any") or []
                hit = next((k for k in kws if k in ask_hits), "")
                if hit:
                    return self._jump(g.get("to"), f"问句加速：命中「{hit}」→ {g.get('to')}")
        return None

    def resolve(self, ask_hits: set[str] | None = None, text: str = "") -> Step:
        """按当前节点的 goto 求下一条。

        判定顺序（2026-10-22）：
          1) 本节点的**语义**分支（ask_any / flag / roll / clock_gte）——说到就跳；
          2) **全表问句加速**（_accelerate）——本节点没写，但后面某拍写了；
          3) 兜底分支（turn_gte / else）——纯粹按轮数推进；
          4) 都没有：停在本节点，靠 clock.forced 兜底。

        为什么把「1) 语义」和「3) 兜底」拆开：兜底是 `turn_gte: 1`，几乎每轮都成立，
        先判兜底就会**每轮必跳**，用户说的那句话根本没有机会参与决定。
        """
        if self.is_ending():
            return Step(node=self.node_id, ending=self.ending or self.node.get("ending"))
        hits = set(ask_hits or ())
        if text:
            for n in self.story.get("nodes") or []:
                for g in n.get("goto") or []:
                    for kw in (g.get("when") or {}).get("ask_any") or []:
                        if kw and kw in text:
                            hits.add(kw)
        ask_hits = hits
        goto = [g for g in (self.node.get("goto") or []) if isinstance(g, dict)]
        # 1) 本节点自己的语义分支
        for g in goto:
            when = g.get("when") or {}
            if self._is_specific(when) and self._cond(when, ask_hits):
                return self._jump(g.get("to"), g.get("why") or "条件命中")
        # 2) 全表加速
        step = self._accelerate(ask_hits)
        if step is not None:
            return step
        # 3) 兜底
        for g in goto:
            when = g.get("when") or {}
            if not self._is_specific(when) and self._cond(when, ask_hits):
                return self._jump(g.get("to"), g.get("why") or "兜底跳转")
        # 没有 goto：停在本节点，靠 clock.forced 兜底推进
        return Step(node=self.node_id)

    def _cond(self, when: dict, ask_hits: set[str]) -> bool:
        """求值一条 goto 条件。空条件 = 无条件（兜底必须放最后一条）。"""
        if not when:
            return True
        if "else" in when:
            return True
        if "ask_any" in when:
            if not ask_hits & set(when["ask_any"]):
                return False
        if "node" in when:
            if self.node_id not in set(when["node"]):
                return False
        if "flag" in when:
            if not self.flags.get(when["flag"]):
                return False
        if "turn_gte" in when:
            if self.turns_in_node < int(when["turn_gte"]):
                return False
        if "clock_gte" in when:
            if self.minutes < parse_hhmm(when["clock_gte"]):
                return False
        if "roll" in when:
            # 随机分支：只在显式声明时消耗随机数，且命中就走（不命中继续看下一条）。
            # 卡里唯一的 roll 是 phone_call 的 70%（打通）/ else（打不通）——
            # 这是剧情自己的运气，不是外部开关，谁录都一样（不再有强制钩子）。
            if self.rng.random() > float(when["roll"]):
                return False
        return True

    # ------------------------------------------------------------ 用户想走
    def leave_requested(self, text: str) -> bool:
        """用户明确说「我要走了」——命中即结束，不再生成下一轮。"""
        t = str(text or "")
        for trig in self.story.get("leave_triggers") or []:
            if trig and trig in t:
                return True
        return False

    def begin_leave(self) -> Step:
        """进入「你先走了」结局节点（如果卡里声明了）。"""
        target = next((n["id"] for n in self.story.get("nodes") or []
                       if n["id"] == "ending_leave"), None)
        if target:
            self.node_id = target
            self.turns_in_node = 0
            self.visited.append(target)
        self.ending = (self.node.get("ending") or "stranger")
        return Step(node=self.node_id, moved=True, ending=self.ending,
                    why="用户主动说要走")

    # ------------------------------------------------------------ flag
    def set_flag(self, name: str, value: Any = True) -> None:
        self.flags[name] = value

    def offer_phone(self) -> None:
        self.flags["offered_phone"] = True
        self.flags["warmth"] = int(self.flags.get("warmth") or 0) + 1

    def roll_call(self) -> str:
        """打电话结果：卡里 phone_call 节点的 goto 第一条 roll=0.7。"""
        ok = self.rng.random() <= 0.7
        self.flags["call_result"] = "ok" if ok else "fail"
        return self.flags["call_result"]
