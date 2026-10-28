"""回放用户两段真实对话（2026-10-22 导出），逐轮打印台词/节点/路径/延迟。

为什么要这个：用户导出的 mira-history-8ec2827456af.json（4B）和
mira-history-cb0d2ec555df.json（deepseek）各只有 3–4 轮、全在开场就崩了。
这两个文件是"证据"，本脚本把它们变成"可重复的实验"：同样的用户话、同样的顺序，
改完代码再跑一遍，逐句对比。

用法：
  .venv/Scripts/python.exe tools/qa/probe_issue_replay.py --port 8000 --mode llama --case ds
  .venv/Scripts/python.exe tools/qa/probe_issue_replay.py --port 8000 --mode llama --case l4b
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

CASES = {
    # deepseek 段（mira-history-cb0d2ec555df.json）
    "ds": ["怎么一个人，这么晚了？", "你说话怎么莫名其妙的？",
           "我想喝什么关你什么事儿？", "你这个人莫名其妙的"],
    # 4B 段（mira-history-8ec2827456af.json）
    "l4b": ["你怎么一个人在这儿？", "我们认识吗？", "我不认识你，别跟我说话"],
    # 长程：模拟一个真实玩家的 20 轮（验证 3–5 分钟演示 + 信息逐级释放）
    "long": [
        "这么晚了，你一个人？",
        "外面雨好大啊",
        "你在等谁？",
        "等很久了吧？",
        "你们多久没见了？",
        "她是什么样的人？",
        "她住得远吗？",
        "你有她电话吗？",
        "怎么不打一个？",
        "手机没电了？",
        "要不我用我的打？",
        "给你，你打吧",
        "打通了吗？",
        "她说什么？",
        "那你现在过去？",
        "她让你等这么久，你不生气？",
        "你平时也这样吗？",
        "这么晚了一个人回去小心点",
        "她来了你打算说什么？",
        "我先走了",
    ],
}


def post(base: str, path: str, payload: dict, stream: bool = False):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + path, data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    resp = urllib.request.urlopen(req, timeout=180)
    if not stream:
        return json.loads(resp.read().decode("utf-8"))
    out = []
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--mode", default="llama")
    ap.add_argument("--case", default="ds", choices=sorted(CASES))
    ap.add_argument("--sid", default="")
    a = ap.parse_args()
    sid = a.sid or f"replay-{a.case}-{int(time.time())}"
    base = f"http://127.0.0.1:{a.port}"

    print(f"mode={a.mode} case={a.case} sid={sid}")
    print(post(base, "/api/session", {"session_id": sid, "mode": a.mode}))

    evs = post(base, "/api/turn", {"session_id": sid, "mode": a.mode, "kind": "open"}, stream=True)
    for ev in evs:
        if ev.get("t") == "reply":
            print(f"\n[开场] {ev.get('text')}")

    for i, text in enumerate(CASES[a.case], 1):
        print(f"\n=== turn {i} · 你：{text}")
        t0 = time.time()
        evs = post(base, "/api/turn", {"session_id": sid, "mode": a.mode,
                                       "text": text, "kind": "user"}, stream=True)
        node = ""
        for ev in evs:
            t = ev.get("t")
            if t == "reply":
                print(f"  她：{ev.get('text')}")
            elif t == "suggest":
                node = ev.get("node") or ""
                print(f"  [sug] {ev.get('items')}  @{ev.get('clock')} node={node}")
            elif t == "done":
                print(f"  [done] node={ev.get('node')} beat={ev.get('beat')} "
                      f"path={ev.get('path')} first_audio={ev.get('first_audio_ms')}ms "
                      f"total={ev.get('ms')}ms "
                      f"gate={(ev.get('topic_gate') or {}).get('verdict')}")
        print(f"  实测耗时 {int((time.time()-t0)*1000)}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
