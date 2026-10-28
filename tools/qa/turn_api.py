"""Dump one turn's NDJSON events from the running server (no browser)."""
import json
import sys
import time

import httpx

BASE = "http://127.0.0.1:8787"
mode = sys.argv[1] if len(sys.argv) > 1 else "llama"
text = sys.argv[2] if len(sys.argv) > 2 else "嗯，你一个人在这里等吗？"

with httpx.Client(timeout=60) as c:
    s = c.post(BASE + "/api/session", json={"mode": mode}).json()
    print("session:", s)
    t0 = time.time()
    with c.stream("POST", BASE + "/api/turn",
                  json={"session_id": s["session_id"], "mode": mode, "text": "", "kind": "open"}) as r:
        for line in r.iter_lines():
            if not line.strip():
                continue
            ev = json.loads(line)
            keep = ev.get("t") in ("state", "directive", "reply", "done", "error", "audio")
            if keep:
                shown = dict(ev)
                if shown.get("t") == "audio":
                    shown["url"] = shown["url"].split("/")[-1]
                print(f"{time.time()-t0:5.2f}s {json.dumps(shown, ensure_ascii=False)[:300]}")
    print("--- second turn ---")
    with c.stream("POST", BASE + "/api/turn",
                  json={"session_id": s["session_id"], "mode": mode, "text": text, "kind": "user"}) as r:
        for line in r.iter_lines():
            if not line.strip():
                continue
            ev = json.loads(line)
            if ev.get("t") in ("state", "directive", "reply", "done", "error", "audio"):
                shown = dict(ev)
                if shown.get("t") == "audio":
                    shown["url"] = shown["url"].split("/")[-1]
                print(f"{time.time()-t0:5.2f}s {json.dumps(shown, ensure_ascii=False)[:300]}")
