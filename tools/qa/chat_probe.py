"""对话质量探针：直接打 /api/turn，把多轮回复、指令、节拍、耗时打到终端。

用途：改完提示词 / 节拍 / 卡包以后，用同一串用户台词快速对比「回复是不是在真的接话」。

跑法：
  .venv\\Scripts\\python.exe tools\\qa\\chat_probe.py                # 默认剧本
  .venv\\Scripts\\python.exe tools\\qa\\chat_probe.py --mode mock    # 对比降级路径
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8787"

# 一串有代表性的用户台词：既有直接提问（必须正面回答），也有闲聊（不许硬拐回心事），
# 还有几个内容触发词（照片 / 雷 / 末班车），用来验证视觉事件是不是真的由台词触发。
SCRIPT = [
    "",
    "我叫小周，今天加班到现在，外面雨大吗？",
    "你这相机不错啊，拍什么的？",
    "你是在等人吗？",
    "等多久了？给他打电话试试？",
    "外面好像又打雷了。",
    "能让我看看那张照片吗？",
    "后来呢？他没再联系你？",
    "你为什么一直等？换我早走了。",
    "那你打算怎么办？照片要寄出去吗？",
    "末班车还有多久？",
    "要打烊了，我陪你等一会儿？",
]


def turn(sid: str, text: str, kind: str, mode: str) -> dict:
    t0 = time.time()
    events: list[dict] = []
    with httpx.stream("POST", f"{BASE}/api/turn",
                      json={"session_id": sid, "text": text, "kind": kind, "mode": mode},
                      timeout=180) as resp:
        seq = resp.headers.get("x-turn-seq")
        for line in resp.iter_lines():
            if line.strip():
                events.append(json.loads(line))
    reply = "".join(e.get("text", "") for e in events if e["t"] == "reply")
    directive = next((e["directive"] for e in events if e["t"] == "directive"), {})
    fixes = next((e.get("fixes") or [] for e in events if e["t"] == "directive"), [])
    done = next((e for e in events if e["t"] == "done"), {})
    errors = [e.get("message") for e in events if e["t"] == "error"]
    audio = sum(1 for e in events if e["t"] == "audio")
    return {
        "seq": seq, "reply": reply, "directive": directive, "fixes": fixes,
        "ms": done.get("ms"), "beat": done.get("beat"), "advanced": done.get("advanced"),
        "first_audio_ms": done.get("first_audio_ms"),
        "audio": audio, "errors": errors, "wall_s": round(time.time() - t0, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="llama")
    ap.add_argument("--script", default="")
    args = ap.parse_args()

    script = [s for s in args.script.split("|")] if args.script else SCRIPT
    sid = f"probe-{int(time.time())}"
    httpx.post(f"{BASE}/api/session", json={"session_id": sid, "mode": args.mode}, timeout=15)

    total_wall = 0.0
    bad = 0
    for i, text in enumerate(script):
        kind = "open" if i == 0 else "user"
        r = turn(sid, text, kind, args.mode)
        total_wall += r["wall_s"]
        d = r["directive"]
        print(f"\n[{i}] {'（开场）' if kind == 'open' else text}")
        print(f"    Mira : {r['reply'] or '（空！）'}")
        print(f"    指令 : emo={d.get('emotion')} act={d.get('action')} scene={d.get('scene')} "
              f"fx={d.get('effect')} ev={d.get('event')}")
        print(f"    节拍 : beat={r['beat']} {'→推进' if r['advanced'] else ''} | "
              f"整轮 {r['ms']}ms（首包 {r['first_audio_ms']}ms）/ wall {r['wall_s']}s | 音频段 {r['audio']}")
        if r["fixes"]:
            print(f"    收敛 : {r['fixes']}")
        if r["errors"]:
            print(f"    !!错误: {r['errors']}")
            bad += 1
        if not r["reply"]:
            bad += 1

    print(f"\n=== 合计 wall {total_wall:.1f}s / {len(script)} 轮；异常轮次 {bad}")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
