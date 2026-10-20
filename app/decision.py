"""决策层：像 Jev 那样做「选择题」，而不是让 4B 边说话边赌。

## 这一层在模仿什么

TypeSafe 的 Jev 是一个**决策模型**（decision model）：给它一段 state 和一组带判据的
选项，它不写文本，只回答「选哪一个」以及每个选项的置信度；置信度不够就把这一手交给
语言模型。它擅长的是「一次前向、答案在给定选项里、错不了 → 不需要解析」，
不擅长生成，也不需要生成。

我们没有 TypeSafe 的 key，而且演示不该依赖任何外部 key，所以这一层做成**可替换的链**：

    backend    依赖                延迟        什么时候用
    ---------  ------------------  ----------  ------------------------------
    ranker     本地 bge-reranker   ~100ms      auto 的第一选择（免密钥、离线）
    glm        免费 API（智谱）     ~400-900ms  本地拿不准时复核；或显式指定
    keyword    无                   <1ms        上面都没有（降级，结果里必须写明）

免费后端选型（2026-01）：智谱 **GLM-4.5-Flash / GLM-4.7-Flash 完全免费、国内直连、
支持 JSON 结构化输出**，是「免费里最好的」当中唯一不需要翻墙的，接口是 OpenAI 兼容，
填一个 key 就能换。没有 key 时退到本地 ranker —— 质量差一档，但演示不依赖网络。

## 置信度不是拍脑袋：用「领先量」而不是 softmax

bge 的 logits 差得很开（同一题 top1-top2 常见 2~8），softmax 会永远饱和到 0.99，
那个数字没有信息量。这里用**领先量的 logistic**：

    confidence = 1 / (1 + exp(-(top1 - top2) / 2))

含义直接：领先 0（分不开）→ 0.5；领先 2 → 0.73；领先 4 → 0.88。
阈值默认 0.72（≈ 领先 2.0），低于它就认为「本地没看懂」，这时才去问免费云端模型。
标定方式与结论写在 docs/PLAN_v8.md 的 S0 一节，改阈值前先重跑 tools/qa/decision_bench.py。

## 为什么 ranker 复用 selector 的权重

bge-reranker-base 有 2.8 亿参数，加载一份约 1.1GB 内存。decision.py 再加载一份纯属
浪费（冷启动多 1.5s、内存多 1.1GB），所以这里通过 `app.selector` 拿同一个实例，
只共享前向、不共享「问什么」：selector 问的是「这句话在问什么」（槽位），
本文件问的是「这句话把剧情推到哪一拍」（节拍）——同一台机器，两个问题。
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Sequence

# ------------------------------------------------------------------ 可调参数
# 本地 ranker 判「拿不准」的阈值（见模块注释里的 logistic 定义）
TAU = float(os.environ.get("MIRA_DECISION_TAU", "0.72"))
# auto | local | llm | keyword
MODE = os.environ.get("MIRA_DECISION", "auto").strip().lower()
# 免费云端：智谱开放平台（OpenAI 兼容）。key 从这几个环境变量里任意一个取。
LLM_URL = os.environ.get("MIRA_DECISION_LLM_URL",
                         "https://open.bigmodel.cn/api/paas/v4/chat/completions")
LLM_MODELS = [m for m in (os.environ.get("MIRA_DECISION_LLM_MODEL") or "").split(",") if m] \
    or ["glm-4.7-flash", "glm-4.5-flash"]
LLM_KEY_ENVS = ("ZHIPU_API_KEY", "GLM_API_KEY", "BIGMODEL_API_KEY", "MIRA_DECISION_API_KEY")
LLM_TIMEOUT = float(os.environ.get("MIRA_DECISION_LLM_TIMEOUT", "8"))


@dataclass(frozen=True)
class Option:
    """一个候选答案。label 给人看，criteria 给模型/打分器看（判据必须能区分对错）。"""

    id: str
    label: str
    criteria: str = ""
    detail: str = ""


@dataclass
class Pick:
    """一次选择题的结果。backend 里能看到它到底走了哪一档（health 与日志都按它归类）。"""

    choice: str
    confidence: float = 0.0
    backend: str = "none"
    scores: dict[str, float] = field(default_factory=dict)
    question: str = ""
    ms: int = 0
    why: str = ""

    def as_dict(self) -> dict:
        return {
            "choice": self.choice,
            "confidence": round(self.confidence, 3),
            "backend": self.backend,
            "ms": self.ms,
            "scores": {k: round(v, 3) for k, v in self.scores.items()},
        }


def _logistic(x: float) -> float:
    if x >= 30:
        return 1.0
    if x <= -30:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


# ------------------------------------------------------------------ 本地打分
class _Ranker:
    """cross-encoder 打分：把「这句话」和每个选项的判据配成对，一次前向全打完。

    复用 app.selector 里已经加载好的 bge 权重（见模块注释最后一段）。
    """

    name = "ranker"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._err: str = ""

    def _ce(self):
        from . import selector as sel

        s = sel.selector()
        return s._ce  # noqa: SLF001 —— 故意共享权重，不再加载一份

    def available(self) -> bool:
        try:
            return bool(self._ce().available())
        except Exception as exc:  # noqa: BLE001
            self._err = f"{type(exc).__name__}: {exc}"
            return False

    def scores(self, state: str, options: Sequence[Option]) -> dict[str, float] | None:
        try:
            with self._lock:
                vals = self._ce().scores(state, [o.criteria or o.label for o in options])
        except Exception as exc:  # noqa: BLE001
            self._err = f"{type(exc).__name__}: {exc}"
            return None
        return {o.id: float(v) for o, v in zip(options, vals)}


# ------------------------------------------------------------------ 免费云端
class _FreeLLM:
    """免费云端选择题（智谱 GLM-Flash）：把选项写成判据，只允许它回一个 id。

    免费额度足够演示（3–5 分钟一场、每轮一次请求，十几轮）。没有 key 时 available()
    为 False，链路自动跳过它——**不把「没有 key」当成错误**，只当成降级。
    """

    name = "glm"

    def __init__(self) -> None:
        self._err = ""
        self._model_ok: str | None = None

    def key(self) -> str | None:
        for env in LLM_KEY_ENVS:
            v = os.environ.get(env)
            if v and v.strip():
                return v.strip()
        return None

    def available(self) -> bool:
        return MODE != "keyword" and self.key() is not None

    def _payload(self, model: str, question: str, state: str,
                 options: Sequence[Option]) -> dict:
        lines = []
        for i, o in enumerate(options, 1):
            crit = f"｜判据：{o.criteria}" if o.criteria else ""
            det = f"｜补充：{o.detail}" if o.detail else ""
            lines.append(f"{i}. id={o.id}｜选项：{o.label}{crit}{det}")
        prompt = (
            f"【问题】{question}\n\n"
            f"【上下文】\n{state}\n\n"
            f"【选项】\n" + "\n".join(lines) + "\n\n"
            "只输出 JSON，不要解释：{\"choice\": \"<上面的某个 id>\", "
            "\"confidence\": <0 到 1 之间的小数>}"
        )
        return {
            "model": model,
            "messages": [
                {"role": "system", "content":
                 "你是一个决策模型：只在一组给定选项里选一个，从不生成选项以外的答案。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.0,
            "max_tokens": 120,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }

    def ask(self, question: str, state: str,
            options: Sequence[Option]) -> Pick | None:
        key = self.key()
        if not key:
            return None
        models = [self._model_ok] if self._model_ok else LLM_MODELS
        for model in models:
            body = json.dumps(self._payload(model, question, state, options)).encode()
            req = urllib.request.Request(
                LLM_URL, data=body,
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {key}"})
            t0 = time.time()
            try:
                with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
                    data = json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                # 模型下线/改名会在这里出现（GLM-4.5-Flash 2026-01-30 起路由到 4.7）
                self._err = f"{model}: HTTP {exc.code}"
                continue
            except Exception as exc:  # noqa: BLE001
                self._err = f"{model}: {type(exc).__name__}"
                continue
            try:
                txt = data["choices"][0]["message"]["content"]
                # 模型偶尔会把 JSON 包在 ```里，抠出第一对花括号
                lo, hi = txt.find("{"), txt.rfind("}")
                obj = json.loads(txt[lo:hi + 1])
                choice = str(obj.get("choice", "")).strip()
                conf = float(obj.get("confidence", 0.0))
            except Exception as exc:  # noqa: BLE001
                self._err = f"{model}: bad json ({type(exc).__name__})"
                continue
            ids = {o.id for o in options}
            if choice not in ids:
                self._err = f"{model}: choice={choice!r} 不在选项内"
                continue
            self._model_ok = model
            return Pick(choice=choice, confidence=max(0.0, min(1.0, conf)),
                        backend="glm", question=question,
                        ms=int((time.time() - t0) * 1000), why=f"via {model}")
        return None


# ------------------------------------------------------------------ 关键词降级
class _Keyword:
    """没有模型、没有 key 时的最后一道：选项判据里出现的关键词命中数。

    明确承认能力不足：只在恒等场景（选项判据是离散词表）才准，结果里 backend=keyword。
    """

    name = "keyword"

    @staticmethod
    def _terms(o: Option) -> list[str]:
        raw = f"{o.label}|{o.criteria}"
        for sep in ("/", "|", "、", "，", ",", " "):
            raw = raw.replace(sep, "\n")
        return [t.strip() for t in raw.splitlines() if len(t.strip()) >= 1]

    def pick(self, state: str, options: Sequence[Option]) -> Pick:
        scores: dict[str, float] = {}
        for o in options:
            scores[o.id] = float(sum(1 for t in self._terms(o) if t in state))
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        top, second = ranked[0], ranked[1] if len(ranked) > 1 else ("", 0.0)
        return Pick(choice=top[0], confidence=0.5 + 0.5 * min(1.0, max(0.0, top[1] - second[1])),
                    backend="keyword", scores=scores)


# ------------------------------------------------------------------ 对外接口
class DecisionModel:
    """选择题的唯一入口。线程安全；一次 pick 最多问两个后端。"""

    def __init__(self) -> None:
        self.ranker = _Ranker()
        self.llm = _FreeLLM()
        self.keyword = _Keyword()
        self._lock = threading.Lock()
        self.last: Pick | None = None
        self.stats = {"ranker": 0, "ranker+glm": 0, "glm": 0, "keyword": 0}

    # -- 状态：给 /api/health 用 ------------------------------------------
    def status(self) -> dict:
        return {
            "mode": MODE,
            "tau": TAU,
            "ranker_available": self.ranker.available(),
            "llm_available": self.llm.available(),
            "llm_key_env": next((e for e in LLM_KEY_ENVS if os.environ.get(e)), None),
            "llm_models": LLM_MODELS,
            "counts": dict(self.stats),
            "last": self.last.as_dict() if self.last else None,
        }

    # -- 主路径 ------------------------------------------------------------
    def pick(self, question: str, options: Sequence[Option], state: str = "",
             *, tau: float | None = None, allow_llm: bool = True) -> Pick:
        if not options:
            return Pick(choice="", backend="none", question=question)
        tau = TAU if tau is None else tau
        mode = MODE if MODE in ("auto", "local", "llm", "keyword") else "auto"
        t0 = time.time()

        # 1) 显式 keyword / 没有本地模型 → 关键词
        if mode == "keyword":
            p = self.keyword.pick(f"{state} {question}", options)
            return self._done(p, t0)

        # 2) 本地 ranker（auto / local 的第一选择；llm 模式下留作兜底）
        local: Pick | None = None
        if self.ranker.available():
            scores = self.ranker.scores(f"{state}".strip() or question, options)
            if scores:
                ranked = sorted(scores.items(), key=lambda kv: -kv[1])
                top, second = ranked[0], ranked[1] if len(ranked) > 1 else ("", 0.0)
                local = Pick(choice=top[0], confidence=_logistic((top[1] - second[1]) / 2.0),
                             backend="ranker", scores=scores, question=question)
        if mode == "local" and local:
            return self._done(local, t0)

        # 3) 免费云端：auto 模式只在本地拿不准时问；llm 模式直接问
        need_llm = allow_llm and self.llm.available() and (
            mode == "llm" or local is None or local.confidence < tau)
        if need_llm:
            cloud = self.llm.ask(question, state, options)
            if cloud is not None:
                if local is not None and mode != "llm":
                    cloud.backend = "ranker+glm"
                return self._done(cloud, t0)

        if local is not None:
            return self._done(local, t0)

        # 4) 全都不可用 → 关键词（并如实标记）
        p = self.keyword.pick(f"{state} {question}", options)
        return self._done(p, t0)

    def _done(self, p: Pick, t0: float) -> Pick:
        with self._lock:
            p.ms = int((time.time() - t0) * 1000)
            self.last = p
            self.stats[p.backend] = self.stats.get(p.backend, 0) + 1
        return p


_default: DecisionModel | None = None


def decision() -> DecisionModel:
    global _default
    if _default is None:
        _default = DecisionModel()
    return _default


def pick(question: str, options: Sequence[Option], state: str = "", **kw) -> Pick:
    return decision().pick(question, options, state, **kw)
