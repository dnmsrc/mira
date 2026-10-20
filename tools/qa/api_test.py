"""API contract tests against a running local server (no test framework needed).

  python tools/qa/api_test.py [base_url]

Checks the pieces the interview task actually grades:
  1. a turn streams state -> reply -> directive -> audio -> done
  2. directives are clamped to the allowed enums
  3. an interrupt bumps the epoch and the cancelled turn stops emitting
  4. late events cannot arrive with an old seq after a new turn starts
  5. STT endpoint transcribes the bundled sample utterance
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787"
ROOT = Path(r"D:\interview\mira")
PASS, FAIL = [], []


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    return urllib.request.urlopen(req, timeout=30)


def stream_turn(sid, text, kind="user", collect_until_done=True, stop_after=None):
    body = json.dumps({"session_id": sid, "text": text, "kind": kind, "mode": "mock"}).encode()
    req = urllib.request.Request(BASE + "/api/turn", data=body,
                                 headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=60)
    events, started = [], time.time()
    for raw in resp:
        line = raw.decode("utf-8").strip()
        if not line:
            continue
        ev = json.loads(line)
        events.append(ev)
        if stop_after and stop_after(ev):
            break
    return events, time.time() - started


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))


def main():
    health = json.loads(urllib.request.urlopen(BASE + "/api/health", timeout=10).read())
    check("health responds", health.get("ok") is True, str(health))

    sid = json.loads(post("/api/session", {"mode": "mock"}).read())["session_id"]

    # 开场是"沉默"的：她抬眼看你一眼、把相机推开、一行旁白、三个选项，但不主动
    # 搭话（app/main.py::run_opening 的 docstring，用户反馈"开局莫名其妙"之后改的）。
    # 旧断言要求 reply + audio，那是沉默开场之前的契约，所以会一直红——不是回归。
    ev, dt = stream_turn(sid, "", kind="open")
    kinds = [e["t"] for e in ev]
    check("open turn streams state", "state" in kinds, str(kinds[:6]))
    check("open turn stays silent (no reply)", not any(e["t"] == "reply" for e in ev),
          str(kinds))
    # audio=整句 mp3（edge/非流式），audio_stream=流式 PCM（侧车）——开场两个都不该有
    check("open turn has no audio by design",
          not ({"audio", "audio_stream"} & set(kinds)), str(kinds))
    check("open turn has narration instead of speech",
          any(e["t"] == "subtitle" and e.get("silent") for e in ev), str(kinds))
    check("open turn opens with the look_door action",
          any(e["t"] == "directive" and e["directive"]["action"] == "look_door" for e in ev),
          str([e["directive"]["action"] for e in ev if e["t"] == "directive"]))
    check("open turn offers suggestions to move on",
          any(e["t"] == "suggest" and e.get("items") for e in ev), str(kinds))
    check("open turn ends with done", kinds[-1] == "done", str(kinds[-3:]))
    check("open turn has no last_train event (content-triggered)",
          not any(e["t"] == "directive" and e["directive"]["event"] == "last_train" for e in ev))
    check("every event carries the same seq", len({e.get("seq") for e in ev}) == 1)
    print(f"       turn wall time {dt*1000:.0f} ms, events {len(ev)}")

    # directive clamp: feed a bad directive straight through the sanitiser path via mock keywords
    ev2, _ = stream_turn(sid, "刚才是不是打雷了？", kind="user")
    d = [e for e in ev2 if e["t"] == "directive"][0]["directive"]
    check("keyword triggers thunder event", d["event"] == "thunder", str(d))
    check("directive keys complete", set(d) == {"emotion", "action", "scene", "effect", "event"}, str(d))
    # 枚举不再硬编码在测试里：从卡包快照拿（一份数据三处用，测试也是消费者之一）
    card = json.loads(urllib.request.urlopen(BASE + "/api/card", timeout=10).read())
    labels = card["labels"]
    check("directive values in enum",
          all(d[k] in labels[k] for k in d),
          f"{d} vs { {k: sorted(v) for k, v in labels.items()} }")
    check("card snapshot lists assets",
          bool(card["assets"].get("char")) and bool(card["assets"].get("plate")),
          str(list(card["assets"])))

    # interrupt: start a turn, cancel it, then make sure it stops emitting
    seq_before = json.loads(urllib.request.urlopen(BASE + f"/api/events/{sid}", timeout=10).read())["seq"]
    body = json.dumps({"session_id": sid, "text": "打断测试", "kind": "user", "mode": "mock"}).encode()
    req = urllib.request.Request(BASE + "/api/turn", data=body, headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=30)
    first = json.loads(resp.readline().decode("utf-8"))
    post("/api/interrupt", {"session_id": sid, "seq": first["seq"], "mode": "mock"}).read()
    tail = []
    t0 = time.time()
    while time.time() - t0 < 8:
        line = resp.readline()
        if not line:
            break
        ev = json.loads(line.decode("utf-8"))
        tail.append(ev["t"])
        if ev["t"] in ("cancelled", "done", "error"):
            break
    check("interrupt stops the stream quickly", time.time() - t0 < 5, f"{time.time()-t0:.1f}s")
    check("interrupted turn never says done", "done" not in tail, str(tail))

    # a brand new turn after interrupt must carry a larger seq
    ev3, _ = stream_turn(sid, "你还在吗", kind="user")
    check("new turn after interrupt has larger seq", ev3[0]["seq"] > seq_before, f"{ev3[0]['seq']} vs {seq_before}")

    # STT with the baked sample utterance
    sample = ROOT / "app" / "static" / "assets" / "audio" / "sample_user.wav"
    boundary = "----mira"
    data = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"u.wav\"\r\n"
            f"Content-Type: audio/wav\r\n\r\n").encode() + sample.read_bytes() + \
           (f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"session_id\"\r\n\r\n{sid}"
            f"\r\n--{boundary}--\r\n").encode()
    req = urllib.request.Request(BASE + "/api/stt", data=data,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    stt = json.loads(urllib.request.urlopen(req, timeout=180).read())
    text = stt.get("text", "")
    check("STT returns Chinese text", any("\u4e00" <= c <= "\u9fff" for c in text), str(stt))
    check("STT sees the expected words", "等" in text or "人" in text, text)
    print(f"       STT {stt.get('ms')} ms for {stt.get('audio_s')}s audio: {text}")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
