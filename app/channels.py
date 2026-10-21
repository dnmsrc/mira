"""表演通道仲裁：卡包声明「谁占哪个通道」，程序只判断「什么时候不能做」。

2026-10-21 用户拍板的规则（原文 → docs/CHANNEL_SPEC_2026-10-21.md）：

  · 5 个通道：speech（口部/发声）· face（神态）· body（身体姿态）·
    hand（手部动作）· gaze（头部与眼神）。
    **同一通道同一时间只能有一个动作；不同通道可以叠加。**
  · **以说话通道为主**：说话时，任何"要用到嘴"的动作（喝水 / 吞咽 / 叹气 / 咳嗽）
    都不能播 —— 按 degrade_to 降级成不占嘴的动作；没有降级目标就**不播**
    （宁可不做，也不要一边说一边喝）。
  · 表演怎么演由**角色卡**定义（channels / face_map / actions 三段）；本模块只做两件事：
    冲突判定 + 降级。它不决定"她该演什么"，那是 decision/director 的活。

为什么单独一层
- 以前互斥检查只有一处（director.dialog_fix 禁 listen 脸，本人说话时），action 完全
  没有检查：客户端 applyAction 直接播，perf 又把 2400ms 的手势锚在句中 → 就是用户
  看到的「她一边喝咖啡一边说话」。
- 互斥规则写在代码里会越长越乱；写进卡包以后，换角色/加动作只改 yaml，不改 Python，
  客户端和服务端读的是**同一张表**（/api/card 的 stage 字段）。

本模块是纯数据 + 纯函数：不读文件、不 import 其它 app 模块，输入 card dict 即可。
服务端（perf / director）和客户端（app.js）用的是**同一套算法**，两边各有一份实现，
`tools/qa/check_channels.py` 会拿同一批用例把两边对拍。
"""

from __future__ import annotations

CHANNELS = ("speech", "face", "body", "hand", "gaze")
CHANNEL_LABEL = {
    "speech": "口部/发声",
    "face": "神态",
    "body": "身体姿态",
    "hand": "手部动作",
    "gaze": "头部与眼神",
}
_POSTURES = ("sitting", "standing")
_MAX_DEGRADE_HOPS = 3


class StageError(RuntimeError):
    """卡包的通道声明自相矛盾（启动期就炸，不要等到演示中途）。"""


# ---------------------------------------------------------------- 校验


def validate(card: dict) -> tuple[list[str], list[str]]:
    """返回 (errors, warnings)。errors 非空 → 卡不能用。"""
    errors: list[str] = []
    warnings: list[str] = []
    chan = card.get("channels")
    actions = card.get("actions")
    directives = card.get("directives") or {}
    enums = {k: set(v or {}) for k, v in directives.items() if isinstance(v, dict)}

    if not isinstance(chan, dict):
        return ["缺少 channels 段（表演通道声明）"], warnings
    if chan.get("schema") != "mira.channels/1":
        errors.append(f"channels.schema 必须是 mira.channels/1，实际 {chan.get('schema')!r}")

    listed = chan.get("list")
    if not isinstance(listed, dict) or not listed:
        errors.append("channels.list 必须是非空映射（5 个通道）")
        listed = {}
    for name in CHANNELS:
        if name not in listed:
            errors.append(f"channels.list 缺少通道 {name}（{CHANNEL_LABEL[name]}）")
            continue
        meta = listed[name]
        if not isinstance(meta, dict) or not meta.get("label"):
            errors.append(f"channels.list.{name} 必须是带 label 的映射")
        elif not meta.get("states"):
            warnings.append(f"channels.list.{name} 没写该通道的状态词表")
    for name in listed:
        if name not in CHANNELS:
            errors.append(f"channels.list 出现未知通道 {name!r}（合法：{', '.join(CHANNELS)}）")

    # 神态：emotion 枚举 ↔ 通道状态词表 必须一一对上，否则「卡的表演定义」和
    # 代码里真正会发出的枚举会悄悄漂移（这正是本轮要避免的那类 bug）。
    face_map = chan.get("face_map")
    if not isinstance(face_map, dict) or not face_map:
        errors.append("channels.face_map 必须把 directives.emotion 的每个值映射到神态词")
    else:
        emos = enums.get("emotion") or set()
        for e in emos - set(face_map):
            errors.append(f"channels.face_map 缺少情绪 {e}")
        for e in set(face_map) - emos:
            errors.append(f"channels.face_map 里的 {e} 不是合法 emotion 枚举")
        face_states = set(((listed.get("face") or {}).get("states") or []))
        for e, s in face_map.items():
            if face_states and s not in face_states:
                errors.append(f"channels.face_map.{e}={s!r} 不在 face 通道状态词表 {sorted(face_states)} 内")

    # ---- actions：每个可发动作都必须声明通道
    if not isinstance(actions, dict) or not actions:
        errors.append("缺少 actions 段（每个动作的通道 / 是否占嘴 / 优先级 / 降级目标）")
        return errors, warnings
    act_enums = enums.get("action") or set()
    for a in sorted(act_enums - set(actions)):
        errors.append(f"actions 缺少动作 {a} 的通道声明（directives.action 里有）")
    for a in sorted(set(actions) - act_enums):
        errors.append(f"actions 里的 {a} 不是合法 action 枚举（directives.action 里没有）")

    speech_pri = chan.get("speech_priority")
    if not isinstance(speech_pri, int):
        errors.append("channels.speech_priority 必须是整数")
        speech_pri = 100
    top = 0
    for name, spec in actions.items():
        where = f"actions.{name}"
        if not isinstance(spec, dict):
            errors.append(f"{where} 必须是映射")
            continue
        chs = spec.get("channels")
        if not isinstance(chs, list):
            errors.append(f"{where}.channels 必须是列表")
            chs = []
        for c in chs:
            if c not in CHANNELS:
                errors.append(f"{where}.channels 里的 {c!r} 不是合法通道")
        if "speech" in chs and not spec.get("speaks"):
            errors.append(f"{where} 声明占 speech 通道，但 speaks 不是 true")
        if spec.get("speaks") and "speech" not in chs:
            errors.append(f"{where}.speaks=true 时 channels 必须包含 speech")
        pri = spec.get("priority")
        if not isinstance(pri, int):
            errors.append(f"{where}.priority 必须是整数")
        else:
            top = max(top, pri)
        for key in ("hold_ms",):
            if not isinstance(spec.get(key), int):
                errors.append(f"{where}.{key} 必须是整数")
        if not isinstance(spec.get("seated", True), bool):
            errors.append(f"{where}.seated 必须是布尔")
        post = spec.get("posture")
        if post is not None and post not in _POSTURES:
            errors.append(f"{where}.posture 只能是 {' / '.join(_POSTURES)}")
        deg = spec.get("degrade_to")
        if deg is not None:
            if deg not in actions:
                errors.append(f"{where}.degrade_to={deg!r} 不是已声明的动作")
            else:
                d = actions[deg]
                if not isinstance(d, dict):
                    errors.append(f"{where}.degrade_to={deg!r} 声明不成形状")
                elif spec.get("speaks") and d.get("speaks"):
                    # 关键：占嘴的动作只能降级到**不占嘴**的动作，否则等于没降
                    errors.append(f"{where}.degrade_to={deg!r} 也占嘴，降级无效")
                elif spec.get("seated", True) and not d.get("seated", True):
                    errors.append(f"{where}.degrade_to={deg!r} 是站姿动作，不能当坐姿动作的降级目标")

    if top >= speech_pri:
        errors.append(f"channels.speech_priority={speech_pri} 必须大于所有动作的最高优先级 {top}，"
                      "否则某个动作可以抢话")

    # 降级链不能成环（成环时 pick 会在 API 层退化成「不播」，但这是卡写错了）
    for name, spec in actions.items():
        seen, cur = {name}, (spec or {}).get("degrade_to") if isinstance(spec, dict) else None
        while cur:
            if cur in seen:
                errors.append(f"actions 的 degrade_to 成环：{name} → … → {cur}")
                break
            seen.add(cur)
            nxt = actions.get(cur) or {}
            cur = nxt.get("degrade_to") if isinstance(nxt, dict) else None
    return errors, warnings


# ---------------------------------------------------------------- 仲裁


class Stage:
    """一张卡包 + 一套判定。纯函数，没有状态：调用方带自己的 busy/posture 进来。"""

    def __init__(self, channels: dict, actions: dict):
        self.raw_channels = channels or {}
        self.actions: dict[str, dict] = actions or {}
        self.speech_priority = int((channels or {}).get("speech_priority") or 100)
        self.list = (channels or {}).get("list") or {}
        self.face_map = (channels or {}).get("face_map") or {}

    # -- 查询 ------------------------------------------------------
    def spec(self, action: str | None) -> dict:
        return self.actions.get(action or "") or {}

    def priority(self, action: str | None) -> int:
        return int(self.spec(action).get("priority") or 0)

    def hold_ms(self, action: str | None, default: int = 4200) -> int:
        return int(self.spec(action).get("hold_ms") or default)

    def channels_of(self, action: str | None) -> list[str]:
        return list(self.spec(action).get("channels") or [])

    def speaks(self, action: str | None) -> bool:
        return bool(self.spec(action).get("speaks"))

    # -- 判定 ------------------------------------------------------
    def conflicts(self, action: str | None, busy: dict[str, str] | None = None, *,
                  speaking: bool = False, posture: str = "sitting") -> list[str]:
        """返回冲突原因（人话，可直接进日志 / 观测面板）。空列表 = 可以播。"""
        spec = self.spec(action)
        if not spec:
            return []
        busy = dict(busy or {})
        if speaking:
            busy.setdefault("speech", "说话")
        # 站姿是**可自愈**的：谁占着 body 通道、而那个动作声明了 posture=standing，
        # 就在它占用期间按站姿判定（不必额外维护一个全局 posture 状态，也就不会忘复位）。
        standing = posture == "standing" or self.spec(busy.get("body")).get("posture") == "standing"
        out: list[str] = []
        if spec.get("speaks") and "speech" in busy:
            out.append(f"口部：{action} 要用到嘴，正在说话（{busy['speech']}）→ 说话优先")
        if standing and spec.get("seated", True):
            out.append(f"身体姿态：已经起身，坐着的 {action} 不能再播")
        for ch in spec.get("channels") or []:
            if ch == "speech":
                continue  # 占嘴的动作已由上面 speaks 判定，避免重复一条
            occ = busy.get(ch)
            if occ and occ != action:
                out.append(f"通道{CHANNEL_LABEL.get(ch, ch)}：被 {occ} 占着")
        return out

    def can_preempt(self, action: str | None, busy: dict[str, str] | None) -> bool:
        """优先级够不够抢断。**口部不可抢**：动作优先级必须 ≥ speech_priority。"""
        p = self.priority(action)
        for ch, occ in dict(busy or {}).items():
            if ch == "speech":
                if p < self.speech_priority:
                    return False
                continue
            if p <= self.priority(occ):
                return False
        return True

    def pick(self, action: str | None, busy: dict[str, str] | None = None, *,
             speaking: bool = False, posture: str = "sitting") -> tuple[str | None, list[str]]:
        """决策：原动作 / 降级动作 / 不播（None）。第二个返回值是理由链（写日志用）。"""
        if not action or action == "idle":
            return action, []
        spec = self.spec(action)
        if not spec:
            # 未声明的动作不接管：让旧行为继续跑，但卡包校验会先把它报成 error
            return action, [f"{action} 没在 actions 里声明通道，按旧行为放行"]
        busy = dict(busy or {})
        reasons = self.conflicts(action, busy, speaking=speaking, posture=posture)
        if not reasons:
            return action, []
        if self.can_preempt(action, busy) and not (speaking and spec.get("speaks")):
            return action, [f"抢断（优先级 {self.priority(action)}）：" + "；".join(reasons)]

        chain: list[str] = []
        seen = {action}
        cur = action
        for _ in range(_MAX_DEGRADE_HOPS):
            nxt = self.spec(cur).get("degrade_to")
            if not nxt or nxt in seen:
                break
            seen.add(nxt)
            chain.append(f"{cur} → {nxt}")
            if not self.conflicts(nxt, busy, speaking=speaking, posture=posture):
                return nxt, reasons + ["降级：" + "、".join(chain)]
            cur = nxt
        tail = "降级链也全被挡住 → 不播" if chain else "没有降级目标 → 不播"
        return None, reasons + [tail]

    # -- 给客户端/日志用 --------------------------------------------
    def snapshot(self) -> dict:
        return {
            "schema": "mira.stage/1",
            "speech_priority": self.speech_priority,
            "channels": {
                name: {"label": (self.list.get(name) or {}).get("label", CHANNEL_LABEL[name]),
                       "states": list((self.list.get(name) or {}).get("states") or [])}
                for name in CHANNELS
            },
            "face_map": dict(self.face_map),
            "actions": {
                name: {
                    "channels": list(spec.get("channels") or []),
                    "speaks": bool(spec.get("speaks")),
                    "priority": int(spec.get("priority") or 0),
                    "hold_ms": int(spec.get("hold_ms") or 0),
                    "seated": bool(spec.get("seated", True)),
                    "posture": spec.get("posture"),
                    "degrade_to": spec.get("degrade_to"),
                    "note": spec.get("note"),
                }
                for name, spec in self.actions.items() if isinstance(spec, dict)
            },
        }

    def describe(self) -> list[str]:
        """观测面板用的一行人话。"""
        out = []
        for name, spec in self.actions.items():
            if not isinstance(spec, dict):
                continue
            chs = "+".join(CHANNEL_LABEL.get(c, c) for c in (spec.get("channels") or [])) or "不占通道"
            mouth = "占嘴" if spec.get("speaks") else "不占嘴"
            deg = f" · 降级→{spec['degrade_to']}" if spec.get("degrade_to") else ""
            out.append(f"{name:<12} {chs} · {mouth} · P{spec.get('priority', 0)} · "
                       f"{spec.get('hold_ms', 0)}ms{deg}")
        return out


def build(card: dict) -> Stage:
    return Stage(card.get("channels") or {}, card.get("actions") or {})


def snapshot(card: dict) -> dict:
    return build(card).snapshot()
