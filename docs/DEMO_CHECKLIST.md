# 演示前检查清单（单卡 12GB，四个服务抢同一张卡）

这份文件只解决一个问题：**演示时别让显卡被自己拖死**。
2026-10-20 实测记录，不是推测。

---

## 1. 为什么会抢：一台机器，一张 4070 SUPER（12 GB）

| 服务 | 端口 | 显存 | 什么时候需要 |
| --- | --- | --- | --- |
| `app`（FastAPI + 静态页） | 8787 | ~0 | 全程 |
| `llama-server`（Qwen3-4B Q4_K_M） | 8080 | ~3 GB | 全程（台词） |
| `mira-tts` 神经音色（CosyVoice2 侧车） | 8790 | ~3–4 GB | 全程（声音） |
| ComfyUI（Qwen-Image 2.1 int8） | 8188 | ~7 GB | **只有出素材时需要** |

三个常驻服务 ≈ 7 GB，ComfyUI 一进来就超 12 GB。超了之后不是"慢一点"，是**退到 CPU**：

- `llama-server`：实测 4.13 tok/s（GPU 正常时是几十 tok/s），首字能拖到 40 s 以上；
- ComfyUI：实测从 2 s/step 掉到 **97.5 s/step**，一张图 25 步要 40 分钟；
- 神经 TTS：单句超过 6 s 预算 → `[tts] neural fallback:` → 退回 edge-tts（声音会变，功能不丢）。

**同一套台词，两种显卡状态下的端到端实测（2026-10-20，同一台机器）**：

| 显卡状态 | provider | 首个音频包 | 单轮总时长 |
| --- | --- | --- | --- |
| ComfyUI 在生成（显存 11.6/12 GB） | `mock`（链掉了两档） | 38 475 ms | 38 475 ms |
| ComfyUI 空闲（显存 1.6 GB） | `llama` | 3 100–7 700 ms | 同左 |

也就是说：**卡被占的时候不是"有点卡"，是"整场演示变成放录音"**，
而且现在能在观测面板「模型」一行看到 `mock`——以前看不出来。

**结论：出素材和演示不能同时进行。** 素材轮跑完就关 ComfyUI，演示前只留 app + llama + tts。
补充实测（同一轮）：ComfyUI 跑完一个任务会**自己把权重卸掉**（5469 → 1618 MiB），
所以演示时它开着但不再提交任务，通常不影响；要让 1618 MiB 也归零得关掉 `D:\ai\qwen-image\env\python.exe`。

---

## 2. 演示前的启动顺序（照抄即可）

```powershell
# 0) 确认没有抢卡的东西在跑
nvidia-smi --query-gpu=memory.used,memory.free,utilization.gpu --format=csv
#   期望：显存占用 < 1.5 GB 才算干净。ComfyUI 开着就先关掉（它不会自己释放权重）。

# 1) 台词模型（首次加载 5–15s）
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\llm\start_llm.ps1

# 2) 神经音色（首次加载 ~20s；没起来也能跑，会自动退回 edge-tts）
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\tts\start_neural.ps1

# 3) 应用（会自己检测上面两个是否在线）
powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
```

三条自检（30 秒内能做完）：

```powershell
curl http://127.0.0.1:8080/health                       # {"status":"ok"}
curl http://127.0.0.1:8787/api/health                   # llm/mode/voice/card.version
# 页面右下「观测」面板 →「模型」一行应该是 llama（不是 mock）
```

`/api/health` 与观测面板的「模型」一行必须显示 **`llama`**。
显示 `mock` 说明这一轮根本没用模型——台词来自卡包定稿，能听但不会接话，
**演示前必须修好，不能带着 mock 上场**。

---

## 3. 降级是看得见的（2026-10-20 补）

以前 `FallbackLLM` 链 (strata → llama → mock) 静默掉到 mock：界面照样回一句很像样的话，
没人知道背后没调模型。这在演示时比报错更危险——**报错会停下来，静默降级不会**。

现在的可观测点：

| 位置 | 字段 | 含义 |
| --- | --- | --- |
| `done` 事件 | `provider` | 这一轮真正产出的是谁：`card`（卡包定稿开场白）/ `llama` / `strata` / `mock` |
| 观测面板 | 「模型」一行 | 上面那个值，**只在变化时打印一行** |
| 服务端日志 | `[turn] 内部异常` + traceback | 不该发生的兜底路径，必须能自证是哪段炸的 |

链内每个 provider 拿到第一个字的预算是 **9 s**（`MIRA_FIRST_TOKEN_BUDGET` 可调）。
超过就换下一个，而不是让外层 14 s 的首字截止把整条链一起取消——
以前那条写法会让排在最后的 `mock` 永远轮不到，用户看到的是报错而不是降级。

---

## 4. 故障对照表

| 现象 | 最可能的原因 | 怎么确认 |
| --- | --- | --- |
| 她说话但明显在背卡包台词，不会接话 | 链掉到 `mock` | 观测面板「模型」= mock；`curl /v1/models` 看 8080 是否在线 |
| 每轮 20–45 s 才开口 | llama 退到 CPU（显存被占） | `nvidia-smi` 看占用；日志里 `eval time` 是否 200 ms/token 量级 |
| 没声音 / 声音变播音腔 | 神经 TTS 侧车没起或超预算 | 日志 `[tts] neural fallback:`；`curl 127.0.0.1:8790` |
| 问"今天中午吃什么"她照样答 | 决策层没生效 | `curl -X POST /api/turn` 看有没有 `narration` + 0 条 `audio_stream`；日志里 `backend` 应是 `cross_encoder` |
| 她对着刚坐下的人装死 | 场景门原型表缺这类句子 | 场景门拒了 → 有一条旁白、没有音频；按 `SCENE_PROTOS` 的设计原则补原型 |

---

## 5. 演示时不要说错的三件事

1. **开场白不是模型写的**，是卡包里的定稿台词（`provider=card`）。理由是首包能压到 300 ms 内、
   第一印象不交给 4B 的概率；从第二句起才走模型。
2. **动作贴片只有 6 张是真的表情**，`look_window / sip_coffee / show_photo / stand` 这几个
   目前只做到"视线 + 表情 + 光影"，没有转头、道具、起身（`docs/ASSETS_REVIEW.md` §10.3）。
3. **照片不是贴图，是静帧演出**：`photo_reveal` 放的是 `insert/polaroid.jpg` + 推近，
   和"她从内袋里抽出来"是两件事，别混着讲。
