"""出口闸单测（问题 1 语气词 / 问题 4 答非所问）：用**成片里真实出现的台词**当用例。

为什么不用合成用例：验收时要回答的是「用户看到的那几条错话，现在还会不会发出去」。
所以这里直接取 v15 成片那一场（logs/turns.jsonl，sid=263823db0aae）的原句：
  · 应当拦下的：turn4「是吗，我没听清。这玻璃太厚了…」/ turn6「还是那个人。别问了…」
                / turn10「嗯……嗯，我听着。」/ turn11「老板娘还没关…」
  · 应当放行的：同一场里答得对的 turn2/3/7/8/9（含「嗯，等一个人。…」这种
                语气词起头但**有内容**的句子——闸门不能把正常口语一起打掉）
外加卡包里每个节点的 mock 必须自己过闸（最后一层兜底不能自己不合格）。

用法：.venv\\Scripts\\python.exe tools/qa/exit_gate_units.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import director, persona  # noqa: E402
from app.main import filler_violation, topic_gate_check  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))


def gate(reply: str, node_id: str, user_text: str = "") -> list[str]:
    node = persona.NODES.get(node_id) or {}
    plan = director.plan_turn(user_text)
    return topic_gate_check(reply, node=node, user_text=user_text, plan=plan,
                            scene_clock=str(node.get("clock") or ""))


# (节点, 台词, 期望拦下?, 当时的用户话——成片里已知的按 PLAN_A 填，未知的留空)
CASES = [
    ("two_years", "是吗，我没听清。这玻璃太厚了，里面又闷。", True, "外面好像又打雷了。"),
    ("rain_worry", "还是那个人。别问了，这个我不想说。", True, "你打过电话吗？"),
    ("call_ok", "嗯……嗯，我听着。", True, "灯要关了？"),
    ("ending_friend", "老板娘还没关，她特意给我留着后门。", True, ""),
    # ---- 同一场里答得对的：不能误杀 ----
    ("waiting", "嗯，等一个人。说好八点半的，现在快九点了。", False, "你在等人？"),
    ("xiaoxue", "大学室友，两年没见了。上个月她才调回来。", False, "等的是谁？"),
    ("phone_dead", "没有。手机七点就没电了，号码在里头，一个也翻不出来。", False,
     "要不要用我的手机打个电话？"),
    ("phone_offer", "可以吗……真的就一个电话，会不会太麻烦你。", False, "不麻烦，你打。"),
    ("phone_call", "谢谢，真的谢谢。喂……是我。", False, "（别说话，等她）"),
    # ---- 念提示词（prompt leak）：只可能来自【导演提示】的短语，说出来就是穿帮 ----
    ("waiting", "嗯……等一个人吧。说好八点半，现在快九点了。这一轮必须点到：你在等人吗、约的几点", True, ""),
    ("xiaoxue", "大学室友，小雪。两年没见。这一轮要回应的话题：等的是谁、多久没见。", True, ""),
    ("waiting", "这一轮雨下得真大，我约了人。", False, ""),
    # ---- 语气词闸门的两条边界 ----
    ("waiting", "嗯。", True, ""),
    ("waiting", "嗯，我在听。", True, ""),
    ("arrival", "快打烊了。你要是只坐一会儿，老板娘应该不会赶你。", False, "你好"),
]


def main() -> int:
    for node_id, reply, should_fail, user_text in CASES:
        viol = gate(reply, node_id, user_text)
        ok = bool(viol) == should_fail
        check(f"{node_id}: {'拦下' if should_fail else '放行'}「{reply[:18]}…」", ok,
              "实际违规：" + ("；".join(viol) if viol else "无"))

    # 每个节点的 mock 必须自己过闸（最后一层兜底不能自己不合格）
    bad = []
    for nid in persona.STORY_ORDER + ["ending_leave"]:
        node = persona.NODES[nid]
        v = gate(str(node.get("mock") or ""), nid)
        if v:
            bad.append((nid, v))
    check("所有节点的 mock 自己都能过闸", not bad, str(bad))

    # filler_violation 本身：只有语气词/语气词起头短句
    for txt, expect in (("嗯……", True), ("嗯嗯。", True), ("……", True),
                        ("嗯，等一个人。说好八点半的，现在快九点了。", False),
                        ("我拍雨后的城市。", False)):
        got = bool(filler_violation(txt))
        check(f"filler_violation({txt[:12]}…)={expect}", got == expect,
              "实际：" + (filler_violation(txt) or "通过"))

    # 成片日志回放（可选）：如果 logs/turns.jsonl 里有那一场，逐条复核
    log = ROOT / "logs" / "turns.jsonl"
    if log.exists():
        rows = []
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        sel = [r for r in rows if r.get("sid") == "263823db0aae"]
        if sel:
            flagged = [r["turn"] for r in sel
                       if gate(r.get("reply") or "", str(r.get("beat") or ""), "")]
            check("成片日志回放：被拦下的正是 turn 4/6/10/11",
                  set(flagged) == {4, 6, 10, 11}, f"实际：{flagged}")

    print(f"PASS {len(PASS)} / FAIL {len(FAIL)}")
    if FAIL:
        print("失败项：" + "；".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
