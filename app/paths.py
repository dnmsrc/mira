"""资源路径解析：把「模型放在哪」从代码里挪出来。

## 为什么要单独一层

`app/selector.py` 原先在**模块级常量**里写死了 `D:\\ai\\mira-models\\bge-reranker-base`。
后果不是「换台机器要改一行」，而是：

- import 期间就取值 → 换一台没有这个目录的电脑，服务**起不来**，报的还是一个
  和真实原因无关的错；
- 面试机上没有 conda 环境、没有那 3 个模型，演示就变成了「在我电脑上能跑」。

所以这里只做一件事：**按优先级找一个目录，找不到就返回 None**，由调用方显式降级
（决策层退到关键词门、生成退到 mock），并在 `/api/health` 里说清楚降到了哪一档。

## 解析顺序（第一个存在的赢）

1. 环境变量（`MIRA_SELECTOR_MODEL` / `MIRA_ROUTER_MODEL`）—— 显式覆盖，优先级最高
2. `<repo>/models/<name>` —— 随仓库走（启动脚本可下载到这里，git 不跟踪大文件）
3. `D:\\ai\\mira-models\\<name>` —— 这台机器的历史位置，保留兼容
4. `<repo>/../mira-models/<name>` —— 仓库的兄弟目录

返回 str 而不是 Path：调用点（transformers / os.path.isdir）都吃 str，少一层转换。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
LEGACY = Path(r"D:\ai\mira-models")


def candidates(name: str, env: str | None = None) -> list[Path]:
    """这个资源可能出现的所有位置，按优先级排列（不判断存在性）。"""
    out: list[Path] = []
    if env:
        val = os.environ.get(env)
        if val:
            out.append(Path(val))
    out.append(MODELS / name)
    out.append(LEGACY / name)
    out.append(ROOT.parent / "mira-models" / name)
    return out


def model_dir(env: str, name: str) -> str | None:
    """找到就返回绝对路径，找不到返回 None（调用方必须处理 None）。"""
    for p in candidates(name, env):
        if p.is_dir():
            return str(p)
    return None


def describe(env: str, name: str) -> dict:
    """给 /api/health 用：说清楚找没找到、找过哪些地方。"""
    found = model_dir(env, name)
    return {
        "env": env,
        "name": name,
        "found": found,
        "searched": [str(p) for p in candidates(name, env)],
    }


def rel(p: str | None) -> str | None:
    """日志里不要打绝对路径（会在录屏里暴露用户名/盘符）。"""
    if not p:
        return None
    try:
        return str(Path(p).resolve().relative_to(ROOT))
    except Exception:  # noqa: BLE001
        return Path(p).name
