"""「台词里冒出 JSON 骨架」的端到端压测（真模型、真服务，2026-10-22 用户现场）。

现场（会话 3f6b15dfb992 第 2 轮，00:59:22）：
    reply = {"": "_", "": "", "": ""} 嗯……店里快打烊了，雨刚停。坐会儿就走。
模型把协议 JSON 和台词写在同一行（无换行），键名缺失 → header_probe 判 plain →
整段当台词 → strip_latin 把英文键名剥掉 → 留下一个 `{"": ""}` 空壳进了字幕。

为什么单独跑这个：单测（empty_reply_units.py 第 6 节）钉的是**纯函数**，
而这条 bug 的触发条件是「模型这次恰好怎么写」，只能靠重复采样命中。
每个会话只问第 2 轮（开场白之后的第一次用户输入）——现场就是这一轮。

用法：
  .venv\\Scripts\\python.exe tools/qa/probe_json_shell.py --port 8000 --n 12
判据：任何一轮的台词里出现 `{`、`}`、成对引号或退化骨架，即 FAIL。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request

BAD = re.compile(r"[{}]|```|^\s*\"|```json")
SHELL = re.compile(r"\{\s*\"[^\"]*\"\s*:")
LEAK = re.compile(r"字数控制|不要旁白|只写会说|对方确认是|她先注意到|语气轻一点")


def post(base: str, path: str, payload: dict) -> list[dict]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + path, data=body,
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    resp = urllib.request.urlopen(req, timeout=180)
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
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--say", default="嗯，一个人？")
    a = ap.parse_args()
    base = f"http://127.0.0.1:{a.port}"

    bad = 0
    for n in range(1, a.n + 1):
        sid = f"shell-{int(time.time())}-{n}"
        post(base, "/api/session", {"session_id": sid, "mode": a.mode})
        post(base, "/api/turn", {"session_id": sid, "mode": a.mode, "kind": "open"})
        evs = post(base, "/api/turn", {"session_id": sid, "mode": a.mode,
                                       "text": a.say, "kind": "user"})
        reply = next((e.get("text", "") for e in evs if e.get("t") == "reply"), "")
        done = next((e for e in evs if e.get("t") == "done"), {})
        flag = ""
        if BAD.search(reply) or SHELL.search(reply):
            flag = "  <<< FAIL 含 JSON 骨架/代码块"
            bad += 1
        elif LEAK.search(reply):
            flag = "  <<< FAIL 念了导演提示"
            bad += 1
        elif not re.search(r"[\u4e00-\u9fff]", reply):
            flag = "  <<< FAIL 没有可听的字"
            bad += 1
        print(f"[{n:02d}] path={done.get('path')} first_audio={done.get('first_audio_ms')}ms "
              f"| {reply}{flag}")

    print(f"\n{a.n - bad}/{a.n} 轮干净，{bad} 轮失败")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
