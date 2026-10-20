"""每轮一行 trace：她为什么回这句，落盘可查（默认开，面板可选）。

为什么要落盘而不是只放在 /api/health 的实时状态里：
  - 面试时被问「你是怎么决策出这一句的」，要能翻出**那一轮**的证据，而不是重新复现；
  - 预设命中率、首字延迟、表演覆盖这些验收指标都要靠它统计（见 docs/DIALOGUE_WORKFLOW_V2.md §7）；
  - 只写文件不动画面：面板默认关，`MIRA_TRACE=0` 可以整层关掉（CI/压测时不写盘）。

一行一个 JSON，字段只增不改（旧脚本按字段名读，加字段不会崩）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "logs" / "turns.jsonl"
MAX_BYTES = 8 * 1024 * 1024      # 超过 8MB 轮转一次，避免演示机上的日志无限长


def enabled() -> bool:
    return os.environ.get("MIRA_TRACE", "1").lower() not in ("0", "off", "false")


def log(record: dict) -> None:
    """追加一行。任何异常都吞掉——trace 是观测层，绝不能因为它把一轮对话搞挂。"""
    if not enabled():
        return
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        try:
            if LOG.exists() and LOG.stat().st_size > MAX_BYTES:
                LOG.replace(LOG.with_suffix(".jsonl.1"))
        except OSError:
            pass
        rec = {"ts": round(time.time(), 3), "at": time.strftime("%H:%M:%S"), **record}
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:  # noqa: BLE001
        pass


def tail(n: int = 20) -> list[dict]:
    """给调试面板用：读最近 n 条。文件不存在时返回空表。"""
    if not LOG.exists():
        return []
    try:
        lines = LOG.read_text(encoding="utf-8").splitlines()[-n:]
        return [json.loads(x) for x in lines if x.strip()]
    except Exception:  # noqa: BLE001
        return []
