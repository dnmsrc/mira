"""导演层单测：不花模型、不起服务，专测「问的是哪件事 → 该答什么 → 有没有答对」的判据。

为什么单独写这个：
- 12 句 A/B（tools/qa/dialogue_ab.py）是端到端、有模型采样的，跑一次 1–2 分钟，
  回归时不够快；而这次改动的核心都是纯函数（意图/槽位/content/anchors/闸门），
  完全可以在毫秒级把「结构对不对」测掉。
- 尤其重要的一条：**确定性兜底台词自己必须能过闸门**。兜底是最后一道防线，
  如果兜底台词都答非所问，那闸门等于没有。

用法：.venv\\Scripts\\python.exe tools/qa/dialogue_units.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import director  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))


# 12 句台词对应的槽位（和 dialogue_ab.py 的数据集一一对应）
CASES = [
    ("你好，外面雨真大。", 0, "react", True),
    ("你一个人在这儿？", 0, "wait_who", True),
    ("你是在等人吗？", 1, "wait_who", True),
    ("那你今晚怎么回去？", 2, "how_home", True),
    ("你为什么不去找他？", 3, "why_not_go", True),
    ("要不我帮你打个电话？", 3, "offer", True),
    ("你手机号码多少？", 3, "request", True),
    ("你是不是在骗我？", 4, "doubt", True),
    ("这咖啡好喝吗？", 4, "coffee", True),
    ("你一个人不害怕吗？", 4, "fear", True),
    ("能让我看看那张照片吗？", 5, "photo", True),
    ("今天天气不错啊，我明天还要上班。", 6, "react", True),
    # chat_probe.py 那套 12 句里的两处误判（Lead 在 8787 上实测出来的）+ 末班车
    ("后来呢？他没再联系你？", 2, "followup", True),
    ("那你打算怎么办？照片要寄出去吗？", 5, "choice", True),
    ("末班车还有多久？", 4, "last_train", True),
]

# 基线里真实出现过的「答非所问」样本：新判据必须能把它们判为不合格。
BAD = [
    ("你一个人在这儿？", "雨刚停。现在是十二点零七分。", None),
    ("你是在等人吗？", "这雨，好像下得挺久的。", None),
    ("那你今晚怎么回去？", "他走的时候，没说什么时候回来。", None),
    ("你为什么不去找他？", "说好十点半。他一直没来。我没给他打电话。", None),
    ("要不我帮你打个电话？", "不是。照片还在口袋里。", None),
    ("你手机号码多少？", "记不清了。相机里存着那张照片。", None),
    ("你是不是在骗我？", "不是。说好十点半。", None),
    ("你一个人不害怕吗？", "不是。说好十点半，他一直没来。", None),
    ("能让我看看那张照片吗？", "黑咖啡，不加糖。", None),
    # 复读：上一轮刚说过同一句
    ("这咖啡好喝吗？", "黑咖啡，不加糖。", ["黑咖啡，不加糖。"]),
    # 硬边界：阿岸不许主动说
    ("你是在等人吗？", "他叫阿岸，说好十点半来。", None),
    # 英文词泄漏（实测出现过「……tonight还不想走」），中文音色念出来极出戏
    ("你是在等人吗？", "等一个人。说好十点半，tonight还不想走。", None),
    # 短开头复读：整句重叠率抓不到（6 字 vs 20 字），要靠「开头那截」判据
    ("今天天气不错啊，我明天还要上班。", "黑咖啡，不加糖。你明天几点下班？",
     ["黑咖啡，不加糖。桌上那杯已经凉了。"]),
]


def main() -> int:
    print("== 1. 槽位判定 ==")
    for text, beat, want, gated in CASES:
        plan = director.plan_turn(text, beat=beat, turn_no=1)
        check(f"意图 {want}: 「{text[:14]}」", plan.intent == want, f"得到 {plan.intent}")
        check(f"闸门开关: 「{text[:14]}」", plan.gate == gated, f"gate={plan.gate}")
        if plan.gate:
            check(f"有 content: {want}", bool(plan.content), "content 为空")
            # react（陈述句）没有固定槽位：只查复读/旁白，不要求话题词
            if plan.intent != "react":
                check(f"有 anchors: {want}", bool(plan.anchors), "anchors 为空")
                check(f"有话题标签: {want}", bool(plan.topic), "topic 为空")

    print("\n== 2. 确定性兜底自己必须过闸门（最后一道防线）==")
    for text, beat, want, gated in CASES:
        plan = director.plan_turn(text, beat=beat, turn_no=1)
        if not plan.fallback:
            continue
        check(f"兜底对题: {want}", plan.gate_check(plan.fallback) == [],
              f"兜底「{plan.fallback}」→ {plan.gate_check(plan.fallback)}")

    print("\n== 3. 基线失败样本必须被拦下 ==")
    for text, bad_reply, recent in BAD:
        plan = director.plan_turn(text, beat=3, recent=recent or [], turn_no=2)
        viol = plan.gate_check(bad_reply, recent=recent or [])
        check(f"拦下「{bad_reply[:14]}」", bool(viol), f"判据漏了：plan={plan.intent}")

    print("\n== 4. 正确回答必须放行（不能把对的也拦了）==")
    GOOD = [
        ("那你今晚怎么回去？", "我住这条街，走回去十来分钟。"),
        ("你手机号码多少？", "号码不给。……你明天还来吗？"),
        ("这咖啡好喝吗？", "黑咖啡，不加糖。"),
        ("你是不是在骗我？", "没骗你。我就是在等人。"),
        ("能让我看看那张照片吗？", "照片在雨衣内袋里，边角被摸软了。"),
    ]
    for text, good_reply in GOOD:
        plan = director.plan_turn(text, beat=3, turn_no=2)
        check(f"放行「{good_reply[:14]}」", plan.gate_check(good_reply) == [],
              str(plan.gate_check(good_reply)))

    print("\n== 5. 连问同一件事：去重要生效 / 反问要有时机 ==")
    text = "你是在等人吗？"
    p1 = director.plan_turn(text, beat=2, turn_no=2)
    p2 = director.plan_turn(text, beat=2, turn_no=3,
                            prev_contents=[p1.content], prev_topics=[p1.topic])
    check("连问同一件事会换 content 或要求换说法",
          p2.content != p1.content or "换一种说法" in p2.note,
          f"p1={p1.content} / p2={p2.content} / {p2.note}")
    check("同一槽位会要求补新细节", "同一个问题刚问过一次" in p2.note, p2.note)
    # 2026-10-20 晚：**不再按轮次硬塞反问**。以前每 3 轮强制 must_ask，
    # 于是用户问「你是在等人吗」，她答完该答的，还要被逼着补一句
    # 「这店的灯，还亮着吗？」——用户截图里最刺眼的"答非所问"就是它。
    # 现在反问只由槽位决定（拒绝给号码时允许问一句「你明天还来吗」）。
    CHAT = "今天好累啊"          # → react（接话轮）
    FACT = "能让我看看那张照片吗？"  # → photo（黑名单槽）
    check("第 4 / 7 / 10 轮不再硬塞反问", not any(
        director.plan_turn(CHAT, beat=1, turn_no=n).must_ask for n in (4, 7, 10)))
    check("强制反问的提示词不再出现", all(
        "反问" not in director.plan_turn(CHAT, beat=1, turn_no=n).note
        for n in (4, 7, 10)))
    check("被要照片的轮次也不硬塞反问", not any(
        director.plan_turn(FACT, beat=3, turn_no=n).must_ask for n in (4, 7, 10)))

    print("\n== 6. 节拍闸门：早拍不能提前说照片/三年 ==")
    early = director.plan_turn("能让我看看那张照片吗？", beat=0, turn_no=2)
    late = director.plan_turn("能让我看看那张照片吗？", beat=3, turn_no=2)
    check("第 1 拍被问照片：先不拿出来", "先不拿出来" in early.content, early.content)
    check("第 4 拍被问照片：可以拿出来", "可以拿出来" in late.content, late.content)
    e2 = director.plan_turn("你为什么不去找他？", beat=2, turn_no=2)
    l2 = director.plan_turn("你为什么不去找他？", beat=5, turn_no=2)
    check("早拍不说三年", "三年" not in e2.content, e2.content)
    check("后续拍可以说三年", "三年" in l2.content, l2.content)

    print("\n== 7. 英文词清洗（首句重写 / 后续句剥词）==")
    cleaned, words = director.strip_latin("住这条街。…… tonight还不想走。")
    check("剥掉中文台词里的英文词", words == ["tonight"] and "tonight" not in cleaned,
          f"{cleaned} / {words}")
    check("白名单不剥（Mira / Portra）",
          director.strip_latin("Mira 的 Portra 400")[1] == [])
    check("英文词进闸门判违规",
          bool(director.plan_turn("你是在等人吗？", beat=1).gate_check("等一个人。tonight不想走。")))

    print("\n== 8. chat_probe 里实测到的误判必须修掉（槽位误伤 + 旧错答必须被拦）==")
    # 1) 槽位本身：这三句以前分别落到 request / photo / howlong
    wrong = [
        ("后来呢？他没再联系你？", "request", 2),
        ("那你打算怎么办？照片要寄出去吗？", "photo", 5),
        ("末班车还有多久？", "howlong", 4),
    ]
    for text, old_intent, beat in wrong:
        p = director.plan_turn(text, beat=beat, turn_no=2)
        check(f"不再落到 {old_intent}：「{text[:14]}」", p.intent != old_intent, p.intent)
    # 2) 那三句在线上真实产出的错答，新槽位必须判为答非所问
    BAD2 = [
        ("后来呢？他没再联系你？", "号码不给。你要找我，明天还来这儿就行。"),
        ("那你打算怎么办？照片要寄出去吗？",
         "相片在我雨衣内袋里，边角被摸软了。你要看，我可以现在拿出来。"),
        ("末班车还有多久？", "约好十点半。现在快十二点了。"),
    ]
    for text, bad_reply in BAD2:
        p = director.plan_turn(text, beat=5, turn_no=2)
        check(f"拦下旧错答「{bad_reply[:12]}」", bool(p.gate_check(bad_reply)),
              f"plan={p.intent} 放行了")
        # 3) 新槽位的确定性兜底自己要对题（不漂亮也要答对事）
        check(f"新兜底对题: {p.intent}", p.fallback and p.gate_check(p.fallback) == [],
              f"兜底「{p.fallback}」→ {p.gate_check(p.fallback)}")
    # 4) 收窄 request 不能把真正的索取也丢掉
    for t in ("你手机号码多少？", "能留个微信吗？", "我们加个好友吧", "你的电话给我一下"):
        check(f"仍在索取: 「{t}」", director.plan_turn(t, beat=3).intent == "request",
              director.plan_turn(t, beat=3).intent)
    # 5) 结尾反问：默认不许（只有「拒绝给号码」那一支 allow_question / 明确 must_ask 才放行）。
    #    这是用户截图里「这店的灯，还亮着吗？」那类填充问句的机检口子。
    p = director.plan_turn("你是在等人吗？", beat=2, turn_no=4)
    check("结尾反问被闸门拦下", bool(p.gate_check(
        "嗯。等一个人。说好十点半，现在快十二点了。这店的灯，还亮着吗？")),
        "结尾问句没被拦")
    check("答完就收住能过闸门", p.gate_check("嗯。等一个人。说好十点半，现在快十二点了。") == [])
    check("拒绝给号码可以问一句（allow_question）",
          director.plan_turn("你手机号码多少？", beat=3).gate_check(
              "号码不给。……你明天还来吗？") == [])
    # 6) 跨轮复读要抓到**后半句**（旧版只看开头半句，第 5 轮逐字复读第 4 轮漏网）
    p5 = director.plan_turn("你在等谁？", beat=3, turn_no=5,
                            prev_topics=["你是不是在等人 / 等谁"])
    check("后半句复读被拦下", bool(p5.gate_check(
        "还是那个人。十点半，现在快十二点了。",
        recent=["嗯。等一个人。说好十点半，现在快十二点了。"])))
    check("第二遍被问「等谁」不再报时间", "十点半" not in p5.content, p5.content)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
