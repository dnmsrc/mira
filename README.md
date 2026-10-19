# Mira · 实时互动场景（72 小时实验）

暴雨刚停的夜里，一家即将打烊的咖啡馆。Mira 穿着琥珀色雨衣坐在窗边，似乎在等一个不会来的人。
你可以打字或按住麦克风和她说话，她**会开口（语音 + 字幕）、会变表情、会做动作、会换镜头、会被台词触发一段视频演出**；她说话时你随时可以开口打断。

不是聊天列表：整屏都是场景和角色，交互入口只是一条字幕区 + 一个麦克风。

![首屏](docs/preview.png)

---

## 1. 快速开始

```powershell
# 一条命令：建 venv、装依赖、起服务、开浏览器（首次约 2-3 分钟，之后 <20s）
powershell -ExecutionPolicy Bypass -File .\start.ps1
# 或者双击 start.bat
```

打开 <http://127.0.0.1:8787/>，点「进入场景」。

- **默认 Mock 模式**：不调用任何模型、不需要网络、不需要 API Key，剧本按你的话推进，
  四种状态、5 种表情、6 种动作、镜头/环境切换、3 类视频事件全部可复现。
- **真实模型模式**（本地 Qwen3-4B，OpenAI 兼容接口）：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\llm\start_llm.ps1   # 另开一个窗口，首次加载 10-30s
$env:LLM_MODE="llama"; powershell -ExecutionPolicy Bypass -File .\start.ps1
```

页面右上角「观测」可以在运行时切换 provider、注入故障、查看状态事件与耗时。

| 地址 | 用途 |
| --- | --- |
| `/` | 互动场景 |
| `/debug` | 观测页：一次互动的状态事件、耗时、错误 |
| `/api/health` | 健康检查 |

> 麦克风需要 `localhost` 或 https 环境（浏览器安全上下文），本机 127.0.0.1 满足。

---

## 2. 题目要求 → 本实现

| 题目要求 | 实现 | 位置 |
| --- | --- | --- |
| 场景为视觉主体、非聊天列表 | 全屏场景 + 角色 + 字幕 + 输入坞，无消息气泡 | `app/static/index.html` |
| 移动端优先、桌面正常 | 560px 竖屏居中卡片，`dvh` + 安全区适配 | `style.css` |
| 待机/倾听/思考/说话 四状态 | `data-state` 驱动角色微动、指示灯、文案 | `app.js:setState` |
| 文字 + 麦克风输入 | 输入框 / 按住说话（`MediaRecorder` → `/api/stt`） | `app.js:startMic` |
| 语音回复 + 同步字幕 | 逐句 TTS，音频播放时才把该句推到字幕 | `main.py:tts_worker` |
| 多轮连续互动 | 会话保持 history，剧情节拍随对话推进 | `main.py:Session` |
| 语音打断 | 按下麦克风 = 本地停播 + 清空队列 + 服务端 epoch+1 | `app.js:interrupt` / `main.py` |
| 被打断的回复立即失效 | 每个事件带 `seq`，客户端丢弃旧 seq；服务端 epoch 失效后停止下发 | 见 §4 |
| 结构化角色/场景指令 | **首行 JSON 指令 + 换行 + 台词**，5 个维度、白名单收敛 | `app/persona.py` / `app/main.py:header_probe` |
| ≥3 种情绪 | neutral / smile / worried / surprised / thinking / listen，6 张独立表情贴片 | `assets/char/` |
| ≥2 种非说话动作 | look_window / sip_coffee / lean_in / show_photo / stand，动作贴片 + 镜头微推 | `app.js:applyAction` |
| ≥1 次背景/镜头/环境变化 | 镜头推近（整舞台 transform，不重绘素材）、灯光转暗（环境叠加） | `app.js:applyScene` |
| ≥1 个由对话触发的视觉事件 | 雷闪（台词触发）、照片演出（视频）、末班车 | `app.js:triggerEvent` |
| 一种多模态表现能力 | 状态驱动的 MP4 演出片段（4 段，AniSora 生成） | `assets/video/` |
| 视频加载/结束/打断处理 | 加载中徽标、`ended` 收层、打断即 `pause + removeAttribute` | `app.js:hideCinema` |
| 生成中状态与失败处理 | 「素材加载中…」徽标、6s 超时降级为静态图/闪电 | `app.js:degradeEvent` |
| 模型超时/断线/失败反馈 | LLM 首 token 14s/整体 35s 超时、TTS 失败降级、注入故障可复现 | `main.py` / 观测面板 |
| 快速连续输入不播失效回复 | `seq`/epoch 双保险（见 §4） | `app.js:sendTurn` |
| 无密钥可体验 | Mock 模式（默认） | `app/providers.py:MockLLM` |
| 一条命令启动 | `start.ps1` / `start.bat` | 仓库根目录 |

---

## 3. 系统结构

```
浏览器（移动端优先）
  index.html / style.css / app.js
    ├─ 场景层：背景板（固定）+ 角色贴片（alpha，独立切换）+ 背景氛围循环
    │        + 雨 / 闪电 / 暗场；换表情只动角色层，换镜头只 transform 整舞台
    ├─ 演出层：<video> 事件片段（含加载/失败降级）
    ├─ 状态机：idle / listening / thinking / speaking
    └─ 音频队列：逐句 Audio 顺序播放，打断即清空
        │  POST /api/turn（NDJSON 流式响应）
        │  POST /api/interrupt, /api/stt, /api/session
        ▼
FastAPI（app/main.py）
    ├─ Session：seq（轮次）、epoch（打断代）、history、beat（剧情节拍）
    ├─ providers：
    │    ├─ MockLLM：零依赖剧本（关键词触发事件）
    │    ├─ LlamaLLM：llama.cpp /v1/chat/completions（Qwen3-4B）
    │    ├─ TTSProvider：edge-tts → mp3 文件缓存
    │    └─ STTProvider：faster-whisper small（CPU int8，VAD）
    └─ /api/events/<sid>：一次互动的状态事件（观测页读取）
```

一次 turn 的事件流（NDJSON，每行一个 JSON，均带 `seq`）：

```
state(thinking) → turn_start → directive → subtitle_partial… →
state(speaking) → audio(第1句) → … → reply → done
```

指令**先于**台词和音频发出：`emotion` 到得早，第一句 TTS 就能用对应语气合成，
表情/镜头也和第一句语音同时发生，而不是等整段说完才知道她是什么情绪。
`directive` 事件里带 `fixes`（白名单收敛、节拍闸门、关键词兜底各自的说明），观测面板可见。

---

## 4. 语音与打断：为什么这样做

### 4.1 打断链路（按住说话即打断）

1. `pointerdown` 麦克风 → **先在本地** `stopPlayback()`：暂停当前 `Audio`、清空待播队列、
   取消浏览器语音、收起视频层并清空字幕 —— 这一瞬间画面上已经"停下"。
2. 立刻 `POST /api/interrupt`：服务端 `epoch += 1` 并 `cancel()` 正在跑的 turn 协程。
3. `getUserMedia` 开始录音，状态切到「倾听」。
4. 松手 → `/api/stt` → 转写文本 → `POST /api/turn` 作为新一轮（新 `seq`）。

### 4.2 迟到的响应为什么不会覆盖新状态

双保险：

- **客户端**：全局 `S.activeSeq`。任何事件 `ev.seq !== S.activeSeq` 直接 `continue`。
- **服务端**：每个 turn 捕获自己的 `epoch`；`emit()` 发现 `epoch` 已失效就抛 `TurnCancelled`，
  协程立即结束，不会再有新事件写进流。

所以「上一轮迟到的网络响应」既不会被发出，也不会被渲染。

### 4.3 为什么是「按住说话」而不是自动 VAD

- 按住说话给用户一个**明确的打断意图**，不需要区分"嗯/对"这类附和音和真正打断（这是题目
  可选扩展里最难的部分，自动 VAD 本机没有可靠的 AEC，容易让角色听到自己的声音）。
- 交互成本低：手机上拇指按住即可，松手即发；角色说话时按下去就是打断，交互语义统一。
- 自动 VAD 的降级路径已在结构上留好：`interrupt()` 与录音解耦，换成 VAD 只需在检测到
  语音起点时调用同一个 `interrupt()`。

---

## 5. 角色 / 场景指令协议

**协议 = 第一行一个 JSON 对象，换行之后才是要说的台词**（模型被 system prompt 要求严格这么输出）：

```json
{"emotion":"worried","action":"look_window","scene":"cafe_night","effect":"rain","event":"none"}
这雨下了一整天，我都有点忘了它停下来的样子。
```

为什么把指令放在**开头**而不是结尾：流式输出里指令先到 → 服务端在第一个换行处就拿到
`emotion`，于是**这一轮的第一句语音**就能用对应语气（rate/pitch/volume）合成，表情和镜头
也和第一句语音同时切换。放结尾的旧格式（`###DIRECTIVE###` + JSON）仍被兼容，作为模型
不听话时的降级路径；此时 `directive` 事件标 `parsed:false` 并记录一条 fix。

服务端 `header_probe()` 按**括号配对**扫描首行（不是 `split("\n")[0]`），JSON 没闭合前
一个字都不送去 TTS；`finalize()` 再做三件事：

1. `sanitize_directive()` **白名单收敛**：不在枚举内的值回退默认，写进 `fixes`；
2. `guard_story()` **节拍闸门**：还没到的剧情事件（如第 4 拍前的「照片」）直接拦下；
3. `keyword_event()` **内容触发兜底**：4B 小模型经常只给 `event:"none"`，若用户这句话里
   确实出现了「雷 / 照片 / 末班车」触发词、节拍允许、且这个事件还没放过，就补一个
   —— 事件权限仍然绑在台词内容上，不是计时器。

| 字段 | 取值 | 前端表现 |
| --- | --- | --- |
| emotion | neutral / smile / worried / surprised / thinking / listen | 换角色贴片（叠加在固定背景板上，背景不动） |
| action | idle / look_window / sip_coffee / lean_in / show_photo / stand | 动作贴片 + 镜头微推；几秒后回到当前情绪贴片 |
| scene | cafe_night / window_close / lights_dim | 整舞台镜头变换（不重绘素材）或暗场叠加 |
| effect | rain / thunder / none | 雨丝层、闪电补光 |
| event | none / thunder / photo_reveal / last_train | 视频演出层（MP4），失败降级为静态 |

`fixes` 让「模型不听话」可观测：白名单收敛、节拍拦截、关键词兜底各有说明，观测面板能看到。

---

## 6. 多模态表现：状态驱动的视频演出

`assets/video/` 下的 MP4 由本地 **AniSora（Wan2.1-I2V-14B fp8，lightx2v 推理栈）**
从**背景板**或角色帧生成，16fps、49 帧、约 3 秒：

| 片段 | 触发 | 表现 |
| --- | --- | --- |
| `thunder.mp4` | 台词提到雷/雨，或情绪被戳到 | 冷蓝闪电扫过，雨幕变密 |
| `photo_reveal.mp4` | 剧情第 3 拍后提到照片/相机 | 抽出拍立得，镜头推进 |
| `last_train.mp4` | 提到末班车/打烊/要走 | 窗外车灯扫过 |
| `idle_loop.mp4` | 常驻 | **只有雨、灯光和窗外车流，没有人物**；角色贴片压在它上面，所以氛围循环一直在跑也不会和表情切换打架 |

> 事件片段是「插入镜头」：播放时整屏进入 cinema 层，结束后回到常驻舞台；
> 常驻舞台本身永远是「背景板 + 角色贴片」两层，不依赖任何视频。

播放细节（题目要求的三件事）：

- **加载**：`triggerEvent` 先显示「素材加载中…」徽标；`canplay` 才淡入画面；6 秒未就绪判失败。
- **结束**：`ended` → 收层、清 `src`、回到常驻场景。
- **打断**：`hideCinema()` 立即 `pause()` 并 `removeAttribute("src")`，未播放的事件不会继续。
- **失败降级**：`degradeEvent()` → 闪电用 CSS 补光、照片用静态图 + toast，体验不中断。

生成侧的可复现脚本：`tools/gen/gen_clips.py`（一次加载模型、连续生成 3 段，约 3 分钟/段）。

---

## 7. 模型与素材

> 素材审核入口：**[docs/ASSETS_REVIEW.md](docs/ASSETS_REVIEW.md)**（哪张图在哪、哪些在用、哪些是废弃残留、已知问题）。

| 用途 | 方案 | 说明 |
| --- | --- | --- |
| 场景背景板 | **Qwen-Image 2.1**（t2i 工作流，本地 ComfyUI 0.37） | 864×1536 竖屏、25 步、`euler+simple`、CFG 1.0（蒸馏模型，CFG 必须 1.0） |
| 角色 / 表情 / 动作 | **Qwen-Image 2.1 参考图编辑工作流** | 场景图 + 角色参考图两张输入把人物放进场景，再逐条指令派生 11 个表情/动作 |
| 角色抠像 | rembg `isnet-anime` + 连通域清理 | 得到带 alpha、与背景板同机位的角色贴片 |
| 短片演出 | AniSora V3.1（Wan2.1-I2V-14B fp8 分块 offload） | 12GB 显存可跑，544×960 竖屏，49 帧 @16fps；`idle_loop` 从背景板生成（无人物） |
| 对话 | Mock 剧本 / Qwen3-4B-Instruct-2507（llama.cpp，OpenAI 兼容） | 真实模式完全本地，无云端密钥 |
| TTS | edge-tts `zh-CN-XiaoxiaoNeural`（rate +6%，pitch -2Hz） | 无 Key；mp3 按文本哈希缓存，重复台词 0 开销 |
| STT | faster-whisper `small`，CPU int8，VAD | 热身后 2.2s 语音约 2.2s 转写；`WHISPER_SIZE=base` 可降到 0.8s |

### 7.1 两个专门工作流，配合使用

关键原则：**场景是固定一层，角色是另一层**。先分别生成，再合成；不再「一张图里同时有场景和人物」。
第一版用 SDXL + img2img 做「同构图表情差分」，问题是 img2img 会把背景一起重绘（人物一换，灯和窗也跟着变），
所以改成了下面这条链路：

**① 场景工作流（Qwen-Image 2.1 文生图）**

空咖啡馆背景板，9:16（864×1536，与手机竖屏一致），提示词里明确 `empty, no people`。
用的是 Qwen-Image 2.1 的原生节点图：

```
UNETLoader(qwen_image_2.1_int8_convrot) + CLIPLoader(type=qwen_image) + VAELoader
→ TextEncodeQwenImage21(prompt, negative, cfg 1.0) → EmptyLatentImage(864×1536)
→ KSampler(steps 25, cfg 1.0, euler/simple) → VAEDecode → SaveImage
```

**② 角色工作流（Qwen-Image 2.1 参考图编辑）**

同一个模型的编辑路径（`TextEncodeQwenImage21` 接收参考图 → reference latents），按两步走：

- **把角色放进场景**：参考图 1 = 背景板，参考图 2 = 角色立绘；指令是「把图 2 的女孩放进图 1 的吧台后面，
  保持脸、雨衣、发夹，匹配灯光，不要改房间和镜头」。人物和场景由此在同一次生成里对齐机位。
- **派生整套动作神态**：以上一步合成好的帧为参考图，逐条指令只改「表情 / 动作」：
  `keep the same character, same outfit, same pose and same camera framing; <surprised expression>`
  等 11 条（6 表情 + 5 动作）。因为每次都以同一张 hero 帧为参考，人物不会像 img2img 那样越改越飘。

**③ 抠像与合成**

每帧用 rembg `isnet-anime` 抠出角色，去掉悬空的小连通块（比如头顶的吊灯），得到与背景板
**同一机位、同一尺寸**的 RGBA 贴片；合成验证的像素差在人物区域约 8/255（验证脚本会打印）。
前端把背景板（`plate/cafe_night.webp`）和角色贴片（`char/*.webp`）分开加载，换表情/动作只换上层。

```powershell
# 素材生成三步（需要 D:\ai\qwen-image 的 ComfyUI 0.37 与本地 Qwen-Image 2.1 int8）
python tools\gen\qwen_workflow.py pipeline    # 场景 → 合成 → 背景板 → 11 个表情/动作
python tools\gen\cut_sprites.py               # 抠像 + 连通域清理 + 重合度验证
python tools\gen\postprocess.py               # 压成 webp、写 manifest
```

相关参考（用来选型/对齐官方节点图，不依赖任何在线 API）：

- Comfy Organ 官方工作流库：[Qwen-Image-Edit 工作流](https://comfy.org/workflows/model/qwen-image-edit/)、[全部工作流](https://comfy.org/zh/workflows/)
- 社区 Qwen-Image 2.1 工作流（含节点版本要求与 CFG=1.0 的原因）：<https://github.com/foprc/qwen-image-2.1-comfyui-workflow>
- Qwen-Image 2.1 ComfyUI 重打包权重：<https://huggingface.co/Comfy-Org/Qwen-Image-2.1>
- 透明图层生成的另一条路线（对重训 SDXL 模型兼容性一般，故未采用）：[ComfyUI-layerdiffuse](https://github.com/huchenlei/ComfyUI-layerdiffuse)
- 角色绑定/实时驱动的后续方向：[Inochi2D](https://github.com/Inochi2D/inochi2d)（开源 Live2D 替代）

### 7.2 为什么用「场景工作流 + 角色工作流」而不是一张图

| 做法 | 换表情时背景 | 换角色时场景 | 机位一致性 |
| --- | --- | --- | --- |
| 单张全帧图（第一版） | 背景一起重绘，灯/窗会抖 | 整张场景跟着变 | 靠 seed + img2img 近似，不稳定 |
| **背景板 + 角色贴片（当前）** | **背景像素不动** | **只换贴片，场景不动** | 贴片与背景板同尺寸同机位，几何上对齐 |
| Live2D / Inochi2D 绑定 | 不动 | 需重做绑定 | 最灵活，但 72h 内做不出可用绑定 |

素材全部自制（AI 生成 + 本地推理），无第三方版权素材；未使用任何私有 API Key。

---

## 8. 关键技术选择与取舍

1. **不用 Live2D / Rive / Spine，用「背景板 + 角色贴片 + 视频演出」**
   Live2D 需要拆分图层与绑定，72 小时内做不出可用的二次元角色；Rive/Spine 同理。
   改为分层舞台：场景是固定的一层背景板，角色是带 alpha 的贴片，表情/动作只换贴片，
   镜头变化只对整舞台做 transform —— 背景板一个像素都不动。这正是第一版踩过的坑：
   单张全帧图 + img2img 差分会把背景一起重绘，人物一换，灯和窗也跟着变。
   动态感由 AniSora 生成的 MP4 事件片段补足。
2. **不做端到端语音模型，用 STT + LLM + TTS 三段式**
   端到端语音模型（如 Moshi）本机没有可用的中文权重，且难以插入"打断即停"的确定性控制。
   三段式每一步都可中断、可降级、可观测，符合题目对打断的严格要求。
3. **NDJSON 单流而不是 WebSocket**
   一次 turn 的所有事件必须**严格有序**且能整体取消；HTTP 流 + 轮次 `seq` 天然具备
   "这条流属于哪一轮"的语义，取消只需 epoch+1，比维护双向通道状态更简单。
4. **打断 = 本地先停 + 服务端取消**
   只靠服务端取消会让音频继续播 100-300ms；只靠本地会让服务端继续烧 TTS。两边都做。
5. **Mock 优先**
   面试演示最大的风险是网络/额度/环境。Mock 模式是**真实代码路径**（同一套流、同一套指令、
   同一套前端状态机），只把「LLM 文本生成」换成剧本，因此能保证演示零失败，也方便自动化测试。
6. **视频预生成而不是实时生成**
   实时生成一段 3 秒视频需要 1-3 分钟，无法满足互动节奏。改为离线生成 + 事件驱动播放，
   同时保留「加载中/失败降级」的完整体验，符合题目"使用视频时处理加载、结束、打断"的要求。

---

## 9. 观测、降级与测试

- 页面「观测」面板：provider 切换、故障注入（LLM 超时 / TTS 失败 / TTS 变慢 / 视频失败）、
  实时事件流水、首包与总耗时。
- `/debug`：独立观测页，读取 `/api/events/<session>`，展示 turn 耗时、错误、provider 健康。
- 降级链：TTS 失败 → 字幕 + 浏览器 `speechSynthesis`（可关）；视频失败 → 静态图/CSS 特效；
  LLM 超时 → 错误提示 + 可重试，不打断已完成的画面状态。
- 自动化测试：

```powershell
# 先启动服务，然后：
.\.venv\Scripts\python.exe tools\qa\api_test.py     # 16/16 通过：流/指令/打断/STT
.\.venv\Scripts\python.exe tools\qa\layers_test.py  # 3/3 通过：分层独立性
.\.venv\Scripts\python.exe tools\qa\mic_test.py     # 真实麦克风链路（Chromium 假设备喂 WAV）
.\.venv\Scripts\python.exe tools\qa\shots.py        # 真机截图：开场/待机/事件/观测面板
.\.venv\Scripts\python.exe tools\qa\record_demo.py  # 重录 3-5 分钟演示（见 §14）
```

- `api_test.py`：turn 事件完整性、指令枚举收敛、关键词触发事件、打断后不再发 `done`、
  打断后新 turn 的 seq 递增、STT 中文转写。当前 **16/16 通过**。
- `layers_test.py`：驱动真实页面后断言分层不变量 ——
  换表情只改变 **0.3%** 的画面（背景必须 < 2%）、换动作只改变角色区域、
  镜头变换作用在同时包住背景板和角色贴片的 `.stage` 上（两层不可能各走各的）。

---

## 10. 已知问题

1. STT 在 CPU 上约 1x 实时（2.2s 语音 ≈ 2.2s），首次调用还要下载/加载模型（约 20s）；
   已用 VAD 裁剪静音，`WHISPER_SIZE=base` 可降到 ~0.8s 但中文标点会漂移（繁体/简体混出）。
2. 打断目前是「按住说话」显式打断；自动 VAD + 回声消除未做（本机无可靠 AEC 设备）。
3. 视频片段是离线预生成的固定 4 段，不是每次对话实时生成；生成侧脚本已留好接口。
   事件片段是插入镜头，预渲染时不区分当前表情（演出期间整屏进入 cinema 层）。
4. 角色贴片由同一张 hero 帧派生：动作幅度大的姿势（stand）会有轻微重绘位移，
   发丝边缘的抠像仍有 1-2px 软边；贴片之间的几何对齐靠同尺寸同机位保证，不是像素级绑定。
5. 桌面端是居中竖屏卡片而非自适应大屏布局（题目要求桌面"可正常体验"）。
6. Mock 剧本是线性节拍 + 关键词分支，不理解语义；真实模型模式的剧情节拍控制弱于剧本。
7. 单进程内存会话，重启后会话丢失（题目不要求账号系统）。
8. 真实模型（Qwen3-4B-Instruct）在指令里**几乎从不给 `event`**，视觉事件主要靠
   `keyword_event()` 的关键词兜底。事件仍然绑在台词内容上（不是计时器），但「是不是该演」
   目前一半是规则判断、一半是模型判断；换更大模型或改成工具调用会更自然。
9. 麦克风手势是「点一下开始录音，说完停顿 1.1s 自动发送；按住则松手发送；录音中再点一下立即发送」。
   没有做回声消除（AEC）：外放时角色可能听到自己的声音，演示建议戴耳机。

---

## 11. 投入时间（大致）

| 阶段 | 时间 |
| --- | --- |
| 读题、技术选型、素材路线 | ~1h |
| 第一版素材（Animagine SDXL 差分 + 场景图）+ 发现「人物一改场景跟着变」 | ~1.5h（含模型下载） |
| 素材链路重做：场景工作流（Qwen-Image 2.1 t2i）+ 角色工作流（参考图编辑）+ 抠像/合成 | ~2h |
| AniSora 推理栈打通与短片生成（含 25GB 权重下载/解包） | ~1.5h |
| 后端（会话、流、指令协议、TTS/STT） | ~2h |
| 前端（场景、状态机、音频队列、打断、降级） | ~2.5h |
| 录屏、README、AI_USAGE、自动化测试 | ~1.5h |
| **合计** | **约 10 小时**（其中约 30% 是等模型下载/推理） |

---

## 12. 如果继续做两周

1. **语音体验**：`webrtcvad` + Silero VAD 做自动打断，区分附和音；AEC 去掉回声；
   流式字幕（partial STT + 边说边显示）。
2. **表演系统**：把「贴片」升级成**配置驱动的角色资源包**（表情/动作/口型/视线分层，
   带淡入淡出与优先级队列）；用 **Qwen-Image-Layered** 把一张成品图自动拆成背景/前景/角色图层，
   再用 **Inochi2D** 做骨骼绑定，换表情/动作就不用逐条编辑生成。
   接入口型同步（音素 → 视位）。
3. **视频实时化**：把 AniSora 放到常驻推理服务，关键剧情节点在线生成 2-3 秒短片，
   生成期间用 idle 循环承接；加内容审核与失败重试。
4. **一致性**：角色参考图 + IP-Adapter / 角色 LoRA，保证新场景里服装、发型、发夹不变。
5. **工程质量**：Provider 抽象再抽一层（LLM/TTS/STT 各自可热插拔）、会话持久化、
   OpenTelemetry 埋点、Playwright 端到端回归（含打断时序断言）。
6. **剧情**：把剧本抽象成数据驱动的剧情图（节点 + 条件 + 演出），支持多分支与结局。

---

## 13. 第三方与素材来源

- 图像模型：Qwen-Image 2.1（通义千问，ComfyUI 原生节点；生成场景 + 参考图编辑角色）
- 视频模型：AniSora V3.1 / Wan2.1-I2V-14B（阿里 Wan 团队权重；AniSora 由 B 站开源）
- 推理栈：lightx2v（fp8 分块 offload，12GB 显存可用）
- 抠像：rembg / isnet-anime（MIT）
- 语音合成：edge-tts（Microsoft Edge 在线语音，免费无 Key）
- 语音识别：faster-whisper（Systran/whisper-small，MIT）
- 本地大模型：Qwen3-4B-Instruct-2507（Apache-2.0），llama.cpp 推理
- 前端：原生 HTML/CSS/JS，无框架无构建步骤
- 所有场景板、角色贴片、视频片段均由上述本地模型生成，未使用第三方版权素材
