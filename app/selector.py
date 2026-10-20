"""决策层：把「这句话到底在问什么」交给一个专门做选择题的模型，而不是让 4B 边说话边赌。

## 为什么要单独一层
4B 同时干三件事（读懂这句话、决定该演什么、把话说自然）时，最先塌的是第一件：
它会把「你为什么不去找他」答成「你为什么在这里等」。让同一个模型既理解又选择，
等于让它在一句话里既当考生又当阅卷人，错了也没有任何信号能告诉你它错在哪。

所以拆成两层：
- **理解层（4B）**：只负责把已经决定好的内容说得像人话。它不参与选择。
- **决策层（本文件）**：一个 cross-encoder 选择题模型，对**固定候选集**打一次分。
  每题都是「这句话属于哪一类」的单选题，答案在卡包的 directives 里，模型只能在这几个
  选项里挑——它不是「生成一个答案」，是「在给定选项里选一个」，这正好是小模型擅长的形态。

## 为什么是 cross-encoder 而不是生成式单选
- 一次 forward 就把 26 个候选（16 条场景原型 + 10 个槽位）全打完，实测 101ms/轮（CPU）；
- 分数是连续值，可以直接卡阈值，而不是「相信它说的那个字母」；
- 换候选集不用改提示词、不用重训，改数据就行（和项目「卡 = 数据契约」一致）。

## 兜底为什么不能只靠 argmax（实测结论）
argmax **永远有答案**。实测「今天中午吃什么」在 10 个槽位里被选成 coffee（-0.24），
因为咖啡是这 10 个里最接近的那个。所以选择层必须配一道**场景门**：
先问「这句话是不是还在这个场景里」，门外的话直接进陌生人状态。

标定数据（tools/qa/selector_calibrate.py，21 条人工标注）：
    在场台词最低 gate = +1.83；跑题台词最高 gate = -1.73
两组之间 3.56 的空隙 → 阈值取中点 0.0，两边各留 1.7 以上余量。
16 条在场台词的槽位 top1 全对；5 条跑题全部被门拦下。

## 陌生人状态（题面要求的「最终兜底」）
门外 / 分不够 → 角色不说话，但字幕打一行旁白说明她现在不想接这句话（不朗读）。
旁白文本属于**内容**，放在卡包里（见 card.yaml narration），本文件只负责判「该不该」。
"""

from __future__ import annotations

import importlib.util
import os
import re
import threading
from dataclasses import dataclass, field

from . import paths

# ---------------------------------------------------------------- 标定参数
# 场景门阈值：见模块注释里的标定数据，两组分数中点。
# 偏保守（宁可错拒）还是偏宽松（宁可错收）？这里取中点：
# - 错拒的代价：一句在场的话被演成「她不想理你」，观众立刻觉得角色坏了；
# - 错收的代价：一句跑题的话被硬答成咖啡/末班车，观众觉得她答非所问。
# 两种都难看，所以不额外偏移，交点 0.0。
GATE_TAU = 0.0
# 槽位最小领先量：实测在场台词 margin 最小 0.74（「他没再联系你？」），取 0.3 留余量。
SLOT_MARGIN_MIN = 0.3

MODEL_DIR = paths.model_dir("MIRA_SELECTOR_MODEL", "bge-reranker-base")
# 生成式路由模型（Arch-Router-1.5B）：只用来处理「打分式模型拿不准」的那一小批话。
# off / auto / on：auto = 只有 CUDA 可用时才启用（CPU 上它要 3.4s/句，实时对话不能这样）。
ROUTER_MODE = os.environ.get("MIRA_ROUTER", "auto").lower()
ROUTER_DIR = paths.model_dir("MIRA_ROUTER_MODEL", "arch-router-1.5b")
# 门槛：低于这个 gate 或在门口，就算「拿不准」，交给路由模型复核。
# 依据：标定集里在场台词 gate 最低 +1.83，所以 +2.0 以上基本是明确在场。
UNSURE_GATE = 2.0

# 硬跑题：唯一允许「不算这句在戏里」的一类——明显让助手干活的任务型请求。
# 为什么只留这一类：这类请求即使由角色硬答，也一定是错的（她不该会写代码），
# 交给导演层的 off_topic 分支用一句最短的话挡回，比装懂安全。
_HARD_OFF = re.compile(
    r"(写|改|修|调|debug|review).{0,6}(代码|脚本|程序|函数|接口|bug)|"
    r"\bpython\b|\bcode\b|翻译|翻译成|语法|算法|数学题|算一下|计算|列方程|"
    r"写(一|个)?(篇|段)?(文章|作文|论文|报告|邮件|简历)|查一下资料|搜索一下|"
    r"总结一下|帮我(写|做|查|翻译|算)", re.I)

# ---------------------------------------------------------------- 候选集
# 槽位候选：每一条都写成「对方这句话在问什么」的自然语言。
# 注意这里**不是**意图标签列表（"photo"/"coffee"），而是给人也读得懂的一句话——
# cross-encoder 比较的是语义，「问照片、相机」比 "photo" 有用得多。
SLOTS: dict[str, str] = {
    "closing": "问这家店还开不开、是不是要打烊了、能不能坐一会儿",
    "alone": "问对方是不是一个人、在等谁、等了多久",
    "identity": "问对方的身份：是不是摄影师、叫什么名字",
    "photo": "问照片、相机、胶片，想看看那张照片，问照片里的人是谁",
    "later": "问那个人后来有没有再出现、有没有消息",
    "choice": "问接下来打算怎么办、该不该把照片寄出去",
    "train": "问末班车、怎么回家、路远不远",
    "coffee": "问咖啡、喝什么、苦不苦、加不加糖",
    "why_not": "问为什么不去找他、为什么一直等",
    "weather": "说天气、说雨、说外面冷不冷，作为开场搭话",
}

# 场景门原型：都是「坐在打烊的咖啡馆里会说的话」。这张表本身就是这道门的边界：
# 往里加一句，就等于允许一句话以这种方式进入这场戏。
SCENE_PROTOS: tuple[str, ...] = (
    "这么晚还没打烊？",
    "外面雨刚停吧。",
    "你一个人在这儿？",
    "你在等谁？",
    "等多久了？",
    "你是摄影师？",
    "能让我看看那张照片吗？",
    "后来呢？",
    "他没再联系你？",
    "你打算怎么办？",
    "末班车还有多久？",
    "黑咖啡不加糖？",
    "你为什么不去找他？",
    "这家店几点关？",
    "要不要给他打个电话？",
    "你在躲雨吗？",
    # 2026-10-20 补两条：真实的第一句往往是「你好，我叫XX，你叫什么名字？」这类
    # 自我介绍 / 求座。原表里一句都没有，实测 gate=-1.24 被判成跑题，角色会对着
    # 一个刚坐下的陌生人装死 —— 这是最难看的一种错拒。按本表的设计原则，加一句
    # 就等于「允许这句话以这种方式进入这场戏」，所以只加这两条，不再扩大。
    "你好，你叫什么名字？",
    "我可以坐这儿吗？",
)


@dataclass
class Decision:
    """一轮的决策结果。accepted=False 时，调用方应该走陌生人分支（不说话 + 旁白）。"""

    accepted: bool
    gate: float = 0.0            # 场景门分数（越高越像「这场戏里的话」）
    slot: str = ""               # 选中的槽位（accepted 时有效）
    slot_score: float = 0.0      # 该槽位的分数
    margin: float = 0.0          # 与第二名的差距
    reason: str = ""             # 拒绝原因：off_scene / low_margin / no_model
    backend: str = "none"        # cross_encoder / keyword
    scores: dict[str, float] = field(default_factory=dict)  # 全部槽位分数（观测用）

    def as_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "gate": round(self.gate, 3),
            "slot": self.slot,
            "slot_score": round(self.slot_score, 3),
            "margin": round(self.margin, 3),
            "reason": self.reason,
            "backend": self.backend,
        }


class _CrossEncoder:
    """bge-reranker 的懒加载封装。模型不存在时 available() 为 False，调用方走降级。"""

    def __init__(self, path: str | None) -> None:
        self.path = path
        self._tok = None
        self._model = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        # 光有模型目录不够，还要**真的能 import torch**。
        # 这台机器 torch 是可选依赖：只判目录会让 available() 撒谎 ——
        # 上一版就是如此：available()=True → scores() 抛 ModuleNotFoundError →
        # main.py 的 try/except 把它当成"决策层没有"直接放行整轮，
        # 结果是**门被静默关掉**：跑题的话被当成台词答出来还配了音。
        # path=None 表示 app/paths.py 在所有候选位置都没找到模型（不是错误，是降级）。
        if not self.path or not os.path.isdir(self.path):
            return False
        return importlib.util.find_spec("torch") is not None

    def _load(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            # 只在这里 import：没有装 torch 的部署也能起服务（走 keyword 降级）
            from transformers import AutoModelForSequenceClassification, AutoTokenizer

            self._tok = AutoTokenizer.from_pretrained(self.path)
            self._model = AutoModelForSequenceClassification.from_pretrained(self.path).eval()

    def scores(self, text: str, passages: list[str]) -> list[float]:
        import torch

        self._load()
        with torch.no_grad():
            b = self._tok([text] * len(passages), passages, padding=True,
                          truncation=True, max_length=192, return_tensors="pt")
            return self._model(**b).logits.squeeze(-1).tolist()


class _ArchRouter:
    """生成式路由决策模型（katanemo/Arch-Router-1.5B）。

    和打分式模型的本质区别：它的训练目标就是**输出一个 route 名**，而且候选里
    天生带 "other"（无关/已满足）。也就是说「拒绝」对它是第一类输出，
    对我们这种「没命中就要装死」的需求，语义上比 argmax 干净得多。

    实测（tools/qa/decision_bench.py，24 条标注）：
        bge-reranker（裸 argmax）      槽位 17/18  跑题拒绝 0/5   114 ms
        Arch-Router-1.5B（生成式）     槽位 17/18  跑题拒绝 4/5  3374 ms（CPU）
    结论：决策质量确实更好，但 CPU 上慢 30 倍，不能进热路径——
    所以它只做「快速模型拿不准时」的第二意见（cascade），不做每轮的默认路径。
    """

    TASK = """You are a helpful assistant designed to find the best suited route.
You are provided with route description within <routes></routes> XML tags:
<routes>

{routes}

</routes>

<conversation>

{conversation}

</conversation>
"""

    FORMAT = """
Your task is to decide which route is best suit with user intent on the conversation in <conversation></conversation> XML tags.  Follow the instruction:
1. If the latest intent from user is irrelevant or user intent is full filled, response with other route {"route": "other"}.
2. You must analyze the route descriptions and find the best match route for user latest intent. 
3. You only response the name of the route that best matches the user's request, use the exact name in the <routes></routes>.

Based on your analysis, provide your response in the following JSON formats if you decide to match any route:
{"route": "route_name"} 
"""

    def __init__(self, path: str) -> None:
        self.path = path
        self._tok = None
        self._model = None
        self._lock = threading.Lock()

    def available(self) -> bool:
        if ROUTER_MODE in ("0", "off", "false"):
            return False
        if not self.path or not os.path.isdir(self.path):
            return False
        if ROUTER_MODE == "auto":
            # 只有在 CUDA 上才值得开：CPU 上 3.4s/轮会毁掉实时感。
            try:
                import torch

                if not torch.cuda.is_available():
                    return False
            except Exception:  # noqa: BLE001
                return False
        return True

    def _load(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            self._tok = AutoTokenizer.from_pretrained(self.path)
            self._model = AutoModelForCausalLM.from_pretrained(
                self.path, dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
                device_map="auto" if torch.cuda.is_available() else None).eval()

    def decide(self, text: str) -> str:
        import json
        import re

        self._load()
        routes = [{"name": k, "description": v} for k, v in SLOTS.items()]
        convo = [{"role": "user", "content": text}]
        prompt = self.TASK.format(
            routes=json.dumps(routes, ensure_ascii=False),
            conversation=json.dumps(convo, ensure_ascii=False)) + self.FORMAT
        enc = self._tok.apply_chat_template(
            [{"role": "user", "content": prompt}],
            add_generation_prompt=True, return_tensors="pt")
        ids = enc if hasattr(enc, "shape") else enc["input_ids"]
        if torch_has_cuda():
            ids = ids.to(next(self._model.parameters()).device)
        import torch

        with torch.no_grad():
            out = self._model.generate(ids, max_new_tokens=24, do_sample=False)
        txt = self._tok.decode(out[0][ids.shape[1]:], skip_special_tokens=True)
        m = re.search(r'"route"\s*:\s*"([^"]+)"', txt)
        if m:
            return m.group(1)
        for name in SLOTS:
            if name in txt:
                return name
        return "other"


def torch_has_cuda() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:  # noqa: BLE001
        return False


class Selector:
    """决策层的对外接口。线程安全（一次 forward 加锁，避免并发时串批）。"""

    def __init__(self, model_dir: str | None = None) -> None:
        self._ce = _CrossEncoder(model_dir or MODEL_DIR)
        self._router = _ArchRouter(ROUTER_DIR)
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self._ce.available()

    def _ask_router(self, text: str) -> str | None:
        """拿不准时的第二意见。返回 route 名 / "other" / None（不可用或出错）。"""
        if not self._router.available():
            return None
        try:
            return self._router.decide(text)
        except Exception as exc:  # noqa: BLE001
            print(f"[selector] 路由模型不可用，回落打分式：{type(exc).__name__}: {exc}",
                  flush=True)
            return None

    def decide(self, text: str) -> Decision:
        text = (text or "").strip()
        if not text:
            return Decision(accepted=False, reason="off_scene", backend="none")
        if not self._ce.available():
            # 降级：没有模型时用关键词门，宁可承认能力不足，也不要静默地乱选一个。
            return self._decide_keyword(text)

        # 一次 forward 同时打完场景原型与槽位候选：25 次 pair 一次 batch。
        try:
            with self._lock:
                scene = self._ce.scores(text, list(SCENE_PROTOS))
            labels = list(SLOTS.keys())
            with self._lock:
                vals = self._ce.scores(text, [SLOTS[k] for k in labels])
        except Exception as exc:  # noqa: BLE001
            # 模型在、但跑不起来（缺 torch / 权重坏 / OOM）：降级到关键词门。
            # **不能**让异常冒出去当成"接受"——那等于把门关掉，而且看不出来。
            print(f"[selector] 打分模型不可用，降级关键词门：{type(exc).__name__}: {exc}",
                  flush=True)
            return self._decide_keyword(text)
        gate = max(scene)
        pairs = sorted(zip(labels, vals), key=lambda kv: -kv[1])
        top, second = pairs[0], pairs[1]
        margin = top[1] - second[1]
        scores = {k: v for k, v in pairs}

        # 明确在场且分得开 → 快速路径，不再问第二个模型（这是绝大多数轮次）。
        clear = gate >= UNSURE_GATE and margin >= SLOT_MARGIN_MIN
        if not clear:
            route = self._ask_router(text)
            if route == "other":
                # 路由模型说 other：不沉默，走自由回答（P0-2）。拒绝权降级为「换个说法」。
                return Decision(accepted=True, gate=gate, slot="free", reason="router_other",
                                backend="cross_encoder+arch_router", scores=scores)
            if route in SLOTS and gate >= GATE_TAU:
                # 准入权仍然在场景门手里：门已经判定这句跑题（gate < 0）时，
                # 路由模型不能把它"救"回来。这是实测撞出来的——「今天中午吃什么」
                # gate=-2.22 明确跑题，路由模型却给了 coffee，如果让它覆盖门，
                # 兜底就形同虚设。所以路由模型只能在门内**换一个更好的槽位**。
                return Decision(accepted=True, gate=gate, slot=route,
                                slot_score=scores.get(route, 0.0), margin=margin,
                                backend="cross_encoder+arch_router", scores=scores)

        # ---- P0-2 永不沉默 -------------------------------------------------
        # 用户实测：「全部落到兜底，她不说话看着别处，根本聊不下去」。
        # 结论：把「拒答」放在大门口是错的——沉默比答歪更致命，观众只会觉得程序坏了。
        # 现在的策略：门只用来**降级**（门外的话不套既定答案，由导演层走自由回答），
        # 不再决定「说不说」。真正该拒绝的只有一种：明显让人干活的任务型请求。
        if _HARD_OFF.search(text):
            return Decision(accepted=True, gate=gate, slot="off_topic", slot_score=0.0,
                            margin=margin, reason="off_topic", backend="cross_encoder",
                            scores=scores)
        if gate < GATE_TAU or margin < SLOT_MARGIN_MIN:
            # 门外 / 分不开：不沉默，交给导演层的自由回答（content 为空，靠人设接话）
            return Decision(accepted=True, gate=gate, slot="free", slot_score=top[1],
                            margin=margin, reason="free", backend="cross_encoder",
                            scores=scores)
        return Decision(accepted=True, gate=gate, slot=top[0], slot_score=top[1],
                        margin=margin, backend="cross_encoder", scores=scores)

    # ------------------------------------------------------------ 降级后端
    def _decide_keyword(self, text: str) -> Decision:
        """没有模型时的关键词门：只做「明显跑题就拒」，不假装能判槽位。

        为什么要留这个后端：这台机器的 torch 是可选依赖。缺模型时服务仍要能起，
        但决策质量必须**显式下降**并在返回里标明 backend=keyword，不能悄悄用坏结果。
        """
        import re as _re  # 局部保留旧引用，避免与模块级 re 混用

        kw = {
            "closing": r"打烊|关门|几点关|坐一会儿|坐这儿",
            "alone": r"一个人|等谁|等多久|在等",
            "photo": r"照片|相机|相片|胶片",
            "later": r"后来|再联系|消息|再出现",
            "choice": r"打算|怎么办|寄",
            "train": r"末班|车|回去|回家",
            "coffee": r"咖啡|糖",
            "identity": r"摄影师|叫什么名字|你是谁",
            "why_not": r"为什么不去找|一直等",
            "weather": r"雨|天气|冷",
        }
        if _HARD_OFF.search(text):
            return Decision(accepted=True, gate=0.0, slot="off_topic", slot_score=0.0,
                            margin=1.0, backend="keyword", reason="off_topic")
        for slot, pat in kw.items():
            if _re.search(pat, text):
                return Decision(accepted=True, gate=0.0, slot=slot, slot_score=0.0,
                                margin=1.0, backend="keyword")
        # 没模型时也绝不沉默：门外的话一律走自由回答
        return Decision(accepted=True, gate=0.0, slot="free", slot_score=0.0,
                        margin=1.0, backend="keyword", reason="free")


_default: Selector | None = None


def selector() -> Selector:
    global _default
    if _default is None:
        _default = Selector()
    return _default


def decide(text: str) -> Decision:
    return selector().decide(text)


def warm() -> dict:
    """启动预热：把打分模型和路由模型都加载出来各跑一次。

    第一次 decide() 只加载 cross-encoder；arch-router 要等到"拿不准"的那句才加载，
    而第一次拿不准的句子往往就是用户说的第一句话——实测首轮因此多等 5–10s。
    """
    sel = selector()
    out = {"ce": False, "router": False}
    try:
        sel.decide("你好")
        out["ce"] = True
    except Exception as exc:  # noqa: BLE001
        out["ce_error"] = f"{type(exc).__name__}: {exc}"
    try:
        out["router"] = sel._ask_router("你好") is not None
    except Exception as exc:  # noqa: BLE001
        out["router_error"] = f"{type(exc).__name__}: {exc}"
    return out
