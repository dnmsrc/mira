# -*- coding: utf-8 -*-
"""台词二次校正（问题 6 · 方案乙）的实跑验收。

判据：只看服务端真正发出来的事件。
每一轮记录：
  · 初始 directive（intent 查表给出的 emotion/action）
  · directive_patch 事件（读完台词后校正成什么、为什么）
  · done.dialog_fix（本轮校正了几次、最终 emotion/action）
  · 台词本身 + 首字/首音频延迟

用法（D:\\interview\\mira）：
    $env:PYTHONIOENCODING='utf-8'
    .\\.venv\\Scripts\\python.exe tools\\qa\\dialog_fix_probe.py [--out docs/review/dialogue/dialog_fix_after.json]
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8787"

# 10 轮固定台词：每一轮都故意带一个"动作关键词"，用来看查表结果会不会被台词纠正。
TURNS = [
    "你好，外面雨真大。",
    "你一个人在这儿？在等人吗？",
    "等了多久了？",
    "你手里那张照片是谁？",
    "这杯咖啡苦吗？",
    "我叫周然，你叫什么？",
    "外面雨好像停了。",
    "你要不要给他打个电话？",
    "我该走了，你还不关门吗？",
    "谢谢你陪我聊这么久。",
]


def run_turn(client: httpx.Client, sid: str, text: str) -> dict:
    body = {"session_id": sid, "text": text, "mode": "auto"}
    t0 = time.perf_counter()
    marks: dict[str, int] = {}
    events: list[dict] = []
    reply = ""
    with client.stream("POST", f"{BASE}/api/turn", json=body, timeout=180) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append(ev)
            kind = ev.get("t") or "?"
            if kind not in marks:
                marks[kind] = int((time.perf_counter() - t0) * 1000)
            if kind == "reply":
                reply = ev.get("text") or ""
    marks["_total"] = int((time.perf_counter() - t0) * 1000)
    return {"text": text, "marks": marks, "reply": reply, "events": events}


def summarize(row: dict) -> dict:
    evs = row["events"]
    first_dir = next((e for e in evs if e.get("t") == "directive"), {}) or {}
    patches = [e for e in evs if e.get("t") == "directive_patch"]
    done = next((e for e in evs if e.get("t") == "done"), {}) or {}
    plan = done.get("plan") or {}
    fix = done.get("dialog_fix") or {}
    m = row["marks"]
    return {
        "user": row["text"],
        "reply": row["reply"],
        "intent": plan.get("intent"),
        "table_directive": {k: (first_dir.get("directive") or {}).get(k)
                            for k in ("emotion", "action")},
        "patches": [{"directive": p.get("directive"), "fixes": p.get("fixes"),
                     "at_ms": m.get("directive_patch")} for p in patches],
        "final": {"emotion": fix.get("emotion"), "action": fix.get("action")},
        "fix_n": fix.get("n"),
        "first_text_ms": done.get("first_text_ms"),
        "first_audio_ms": done.get("first_audio_ms"),
        "gate_rewrites": (done.get("gate") or {}).get("rewrites"),
        "total_ms": m.get("_total"),
    }


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/probe_out/dialog_fix_after.json")
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    BASE = args.base

    rows: list[dict] = []
    with httpx.Client(trust_env=False) as client:
        sess = client.post(f"{BASE}/api/session", json={"mode": "auto"}, timeout=30).json()
        sid = sess["session_id"]
        print(f"session={sid} mode={sess.get('mode')} llm={sess.get('llm')} voice={sess.get('voice')}")
        run_turn(client, sid, "")  # 开场白：预热 TTS 缓存
        for text in TURNS:
            r = run_turn(client, sid, text)
            s = summarize(r)
            rows.append(s)
            tbl = s["table_directive"]
            fin = s["final"]
            mark = "→校正" if s["fix_n"] else "  一致"
            print(f"\nuser: {text}")
            print(f"  intent={s['intent']}  表:{tbl.get('emotion')}/{tbl.get('action')}  "
                  f"最终:{fin.get('emotion')}/{fin.get('action')}  {mark}")
            for p in s["patches"]:
                print(f"  patch @{p['at_ms']}ms {json.dumps(p['directive'], ensure_ascii=False)}")
                for f in (p["fixes"] or []):
                    print(f"    · {f}")
            print(f"  reply={s['reply']!r}")
            print(f"  first_text={s['first_text_ms']}ms first_audio={s['first_audio_ms']}ms "
                  f"total={s['total_ms']}ms")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    n_fix = sum(1 for r in rows if r["fix_n"])
    print(f"\n== {n_fix}/{len(rows)} 轮发生了台词校正；明细写入 {out} ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
