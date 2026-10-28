# -*- coding: utf-8 -*-
"""「台词全是空」那条 bug 的回归单测（不起服务、不花模型）。

现场：会话 8fb1df3f3d7c 第 3 轮（2026-10-21 13:55:22），历史面板里出现
`{"":"","":"_","":"_","":"","":}`，TTS 收到的也是这串（= 一段静音）。

根因链（本文件逐环钉死）：
  1. 4B 把协议骨架原样抄了一遍、一个字台词都没写：
     `{"emotion":"","action":"_","scene":"_","effect":"","event":none}`（`none` 没引号 → 非法 JSON）
  2. `header_probe` 只认「合法 JSON」，解不开就判成 plain（整段当台词）→ 骨架进字幕/TTS
  3. `strip_latin` 把 Latin 键名全剥掉 → 变成 `{"":"","":"_",…}`
  4. 兜底层没有「有没有可听的字」这道检查 → 空壳一路落库

修法（对应断言）：
  A. `header_probe` 新增 broken：块里认得指令字段就整块剥掉，绝不当台词
  B. `has_speech`：剥完之后一个字都不剩 → 用本拍兜底台词
  C. `push_sentence` 用 has_speech 挡掉空句，不再往 TTS 送静音

用法：.venv\\Scripts\\python.exe tools/qa/empty_reply_units.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import director  # noqa: E402
from app.main import has_speech, header_probe  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))


# 现场原样：模型吐出来的骨架（`none` 没加引号，所以不是合法 JSON）
SKELETON = '{"emotion":"","action":"_","scene":"_","effect":"","event":none}'
# 现场原样：落进历史/字幕/TTS 的那串
OBSERVED = '{"":"","":"_","":"_","":"","":}'


def main() -> int:
    print("== 1. 复现：骨架是怎么变成空壳的（记录机制，不是要它变回去）==")
    stripped, removed = director.strip_latin(SKELETON)
    check("strip_latin 把骨架剥成现场那串", stripped == OBSERVED, f"得到 {stripped!r}")
    check("剥掉的是 5 个键名 + none", removed == ["emotion", "action", "scene", "effect",
                                                  "event", "none"], str(removed))

    print("\n== 2. A：坏指令行必须被认出来（不能当台词）==")
    state, obj, hdr = header_probe(SKELETON, at_end=True)
    check("骨架 → broken", state == "broken", f"得到 {state}")
    check("broken 时 head 指向骨架之后", hdr == len(SKELETON), f"hdr={hdr}")
    check("broken 不给 directive（表现仍由决策层定）", obj is None, str(obj))

    raw_body = SKELETON + "\n嗯……雨是挺大的。"
    state, obj, hdr = header_probe(raw_body, at_end=True)
    check("骨架 + 真台词 → broken，且正文起点正确",
          state == "broken" and raw_body[hdr:] == "嗯……雨是挺大的。",
          f"{state} / {raw_body[hdr:]!r}")

    print("\n== 3. 合法的首行协议不能被误伤 ==")
    good = '{"emotion":"smile","action":"lean_in","scene":"cafe_night","effect":"rain","event":"none"}\n嗯。……坐吧。'
    state, obj, hdr = header_probe(good, at_end=True)
    check("合法首行 → json", state == "json" and obj is not None and obj["emotion"] == "smile",
          f"{state} / {obj}")
    check("合法首行 → 正文起点正确", good[hdr:] == "嗯。……坐吧。", repr(good[hdr:]))

    plain = "这雨下了一整晚。"
    state, obj, hdr = header_probe(plain, at_end=True)
    check("纯台词（无协议）→ plain", state == "plain" and plain[hdr:] == plain, f"{state} / hdr={hdr}")

    # 只有普通花括号的台词（极罕见）不能被当成指令行
    brace = "{好}。"
    state, obj, hdr = header_probe(brace, at_end=True)
    check("花括号但不是指令 → 不当 broken", state in ("plain", "wait", "json"), state)

    print("\n== 4. B：有没有可听的字 ==")
    check("空壳没有可听的字", not has_speech(OBSERVED))
    check("空串/标点没有可听的字", not has_speech("") and not has_speech("……？！"))
    check("正常台词有", has_speech("嗯……你先坐吧。"))
    check("剥完英文只剩标点 → 没有", not has_speech("…… "))

    print("\n== 5. 结论串起来：这条链现在会走兜底，不会静音落库 ==")
    # 模拟收尾路径：broken → reply = raw[header_end:] = "" → 非空检查 → 兜底台词
    state, _, hdr = header_probe(SKELETON, at_end=True)
    reply = SKELETON[hdr:]
    from app import persona  # noqa: E402

    fallback = persona.BEATS[1]["mock"]
    final = reply if has_speech(reply) else fallback
    check("空骨架最终会落到兜底台词", final == fallback and has_speech(final), repr(final))

    print("\n== 6. 2026-10-22 二次现场：骨架和台词写在同一行（无换行）==")
    from app.main import drop_json_shells, strip_leading_json, tidy_reply  # noqa: E402

    live = '{"": "_", "": "", "": ""} 嗯……店里快打烊了，雨刚停。坐会儿就走。'
    check("现场串：开头骨架被剥掉",
          strip_leading_json(live) == "嗯……店里快打烊了，雨刚停。坐会儿就走。",
          repr(strip_leading_json(live)))

    # 真实形态是「协议 JSON + 空格 + 台词」（模型没换行）。流式阶段必须**直接认定**，
    # 不能等到流结束——旧逻辑会等换行，首字被压到 1232ms、整段一次合成。
    same_line = '{"emotion":"thinking","action":"idle"} 嗯……店里快打烊了。'
    state, obj, hdr = header_probe(same_line, at_end=False)
    check("同行输出（无换行）→ 立刻判 json，不等流结束", state == "json", f"得到 {state}")
    check("同行输出 → 正文起点正确",
          same_line[hdr:] == "嗯……店里快打烊了。", repr(same_line[hdr:]))

    bad_line = '{"emotion":"","action":"_","event":none} 嗯……店里快打烊了。'
    state, obj, hdr = header_probe(bad_line, at_end=False)
    check("同行输出 + 坏骨架 → broken，且正文起点正确",
          state == "broken" and bad_line[hdr:] == "嗯……店里快打烊了。",
          f"{state} / {bad_line[hdr:]!r}")

    # 旁白/引号前缀：新判据要能跳过纯装饰前缀找到 `{`（旧实现只看首字符，直接放弃）。
    dirty = '（她抬眼）{"scene": "cafe_night"} 嗯……店里快打烊了。'
    check("旁白前缀：直接 strip 就能剥（跳过装饰前缀）",
          strip_leading_json(dirty) == "嗯……店里快打烊了。", repr(strip_leading_json(dirty)))
    quote_dirty = '「{"scene": "cafe_night"} 嗯……店里快打烊了。」'
    check("引号前缀：strip 后再 tidy 也干净",
          tidy_reply(strip_leading_json(quote_dirty)) == "嗯……店里快打烊了。",
          repr(tidy_reply(strip_leading_json(quote_dirty))))

    # strip_latin 把英文键名/值剥掉后，骨架退化成 `{"": ""}`：必须整块删掉。
    degraded = '嗯……{"": "", "": ""}店里快打烊了。'
    check("退化骨架：整块删掉",
          drop_json_shells(degraded) == "嗯……店里快打烊了。", repr(drop_json_shells(degraded)))

    # 反向：真台词里的花括号不能被误删 / 误剥（`她说{笑}了一下`）
    hanzi_brace = "她说{笑}了一下。"
    check("真台词花括号：drop_json_shells 不动", drop_json_shells(hanzi_brace) == hanzi_brace)
    mid = "嗯……我说的{笑}就是这个意思。"
    check("花括号在句中：strip_leading_json 不动",
          strip_leading_json(mid) == mid, repr(strip_leading_json(mid)))

    print("\n== 7. 压测撞到的另两种穿帮：``` 残留 / 念导演提示 ==")
    from app.main import strip_prompt_leak  # noqa: E402

    fence = "```\n嗯。……我一个人坐会儿。雨刚停没多久。"
    check("台词开头的 ``` 被清掉",
          tidy_reply(fence) == "嗯。……我一个人坐会儿。雨刚停没多久。", repr(tidy_reply(fence)))
    fence2 = "嗯……坐吧，等一会儿就走。\n```"
    check("台词末尾的 ``` 被清掉", tidy_reply(fence2) == "嗯……坐吧，等一会儿就走。",
          repr(tidy_reply(fence2)))

    # 现场（12 轮压测第 10 轮）：整段是系统提示里的规则原文，被当台词送进了 TTS
    leak = ("对方确认是“一个人”。\n"
            "一轮内最多一个比喻，这句可以带一点画面感来缓冲。\n"
            "只写会说的内容，不要旁白、括号、动作描述。字数控制在 40 字以内。")
    check("整段提示词 → 删空", strip_prompt_leak(leak) == "", repr(strip_prompt_leak(leak)))
    mixed = "嗯……坐会儿就走。\n字数控制在 40 字以内。"
    check("真台词 + 提示词行 → 只留真台词",
          strip_prompt_leak(mixed) == "嗯……坐会儿就走。", repr(strip_prompt_leak(mixed)))
    normal = "嗯。一个人来的，等一个朋友。店里快打烊了。"
    check("正常台词不误删", strip_prompt_leak(normal) == normal, repr(strip_prompt_leak(normal)))

    # 现场（16 轮压测第 6 轮）：节点描述 + 舞台提示被念了出来，还夹一个 `</>` 标签
    stage_leak = ("她先注意到对方，刚想开口打声招呼。听到“一个人？语气轻一点，别把心事压出来。\n"
                  "</>\n\n嗯……是。")
    leaked = strip_prompt_leak(stage_leak)
    check("节点/舞台说明整行删掉（标签行留给 tidy 清）",
          "她先注意到" not in leaked and "嗯……是。" in leaked, repr(leaked))
    tagged = tidy_reply(leaked)
    check("XML 标签 `</>` 被清掉，只留真台词", tagged == "嗯……是。", repr(tagged))

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
