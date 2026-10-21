# P4：第一句台词延迟 2.4–4.3s —— edge 云端地板的拆解与改造

> 2026-10-21 · 负责人：voice · 作用域：本机 Win11 / edge 公网 / 同一时段 · 单位 ms
> 复现脚本与原始数据都在 `tools/tts/`（见文末「证据索引」）。**A/B 数字见 §4，未测的项目见 §9。**

## 1. 结论先行

| 指标（生成路径，首句台词） | 改前 | 改后 | 说明 |
|---|---|---|---|
| 服务端 `first_line_audio_ms` p50 | **2085–2503**（本机同批实测，见 §4 off 档） | **633–894** | 服务端把"音频可听"从"整句合成完"变成"首块 PCM 已发出" |
| 浏览器里真实可听起点 p50 | **2095–2517** | **1070–1171** | playwright 实测 AudioBufferSource 排期 / `<audio>` playing |
| 首块的固定成本 | 850ms（每次 TCP+TLS+WSS 握手） | 0（长连复用，握手藏进 LLM 首字） | 探针证明：握手 850ms、握手后首块仅 223–270ms |

**一句话**：2.4–4.3s 里，TTS 侧能砍掉的是两段——每次合成都重付的 **≈850ms 云端握手**，以及"等整句合成完 + 52–335ms DSP 后处理"才出声的等待。前者用一条保温 WSS（一次握手多句复用）摊掉，后者用「边收 mp3 边解码成 PCM 边播」消掉。剩下的 1.0–1.2s 是 LLM 首字（本机 4B p50 ≈0.7–0.9s）+ 反应时间，不在 TTS 侧。

## 2. 探针：把一次合成拆开量（不是猜）

`tools/tts/probe_edge_timing.py`（原始数据 `tools/tts/out/probe_edge_timing.json`，3 轮冷连接 + 一条连接连发 5 句 + 负控）：

| 分段（冷连接，n=3） | 读数 |
|---|---|
| DNS | 8 / 11 / 17 |
| **TCP + TLS + WSS upgrade** | **838 / 850 / 855**（p50 849.8，极稳） |
| 发 SSML → 第一个音频块 | 222 / 227 / 340 |
| 首个音频块 → turn.end | 116 / 120 / 119 |
| 官方 `edge_tts.Communicate.save` 端到端 | 1394 / 1394 / 1555 |

同一条 WSS 连发 5 句（只第 1 句发 `speech.config`）：

| 句 | 首块 | turn.end | 字节 |
|---|---|---|---|
| 0 | **222.8** | 319.7 | 12816 |
| 1 | **270.5** | 284.6 | 9936 |
| 2 | **263.8** | 288.8 | 11520 |
| 3 | **389.4** | 398.4 | 9648 |
| 4 | **278.7** | 291.1 | 8784 |

→ **连接复用成立**：复用后的首块 223–389ms，比冷连接的 1085–1200ms 少 **≈800ms**；每句都有独立 `turn.end`，字节数正常（没有串音）。协议上 `speech.config` 每条连接发一次即可，后续只发 SSML。

负控（`probe_edge_timing.py` 第 3 段）：合成后暴力 `transport.abort()` 断开 → 重连 1250ms → 首块 249.5ms 恢复正常。

### 2.1 保活：不能假设长连一直活着

`tools/tts/probe_edge_keepalive.py`（同一轮里每 15s 发一次 WSS ping）：

| 空闲累计 | 还能直接复用吗 | 首块 |
|---|---|---|
| 30s | ✅ | 203 |
| 75s | ❌ `ClientConnectionResetError` | — |
| 40s（另一次运行） | ✅ | 219 |

→ 结论：**空闲 ~70s 服务端就会关掉**，ping 只能续一段。所以实现不押"常驻连接"，
而是 **按需预热**：`/api/turn` 一进来就开连接（LLM 首字 p50 ≈0.7–1.0s > 850ms 握手，
正好藏进思考时间），冷启动的第一次对话同样有效。常驻保活只当"大概率已经热"的加分项。

## 3. 改了什么

### 3.1 新文件 `app/tts/live_edge.py`
- `LiveEdge`：一条可复用的 edge WSS。懒连接 / `ensure()` 重连 / 空闲 25s 以上判"不新鲜"重连 /
  12s 一次的空闲 ping 保活（合成中不 ping：aiohttp 写帧在 await 点不是原子的）。
- `pcm_stream()`：串行化合成（一条 WSS 上不能交错两段语音），把 edge 的 mp3 块
  **增量解码**成 24kHz 单声道 s16 PCM（`av.CodecContext.parse/decode`，不额外装依赖）。
- 断开/打断：生成器被取消时**不能 await**（`GeneratorExit` 上下文），所以只给连接打
  `_poisoned` 标记，下一次 `ensure()` 丢掉重连——这样上一句的残音绝不会串进下一轮。
- 音色/参数与整句路径完全一致：`voice=tts.voice`，`rate/pitch/volume` 来自
  `providers.style_for(text, emotion)`（同一个函数，没改）。

### 3.2 `app/main.py`
- `stream_engine()` → `live | neural | None`；`stream_tts_enabled()` 跟着它走。
  以前 `auto` 档在引擎是 edge 时强制返回 False（那时流式只能通向已弃用的神经侧车），
  现在 edge 也有流式音源了。
- `/api/tts_stream` 两个音源共用 `audio/L16;rate=24000` 契约（客户端播放器一行没改）。
- `live_prewarm()`：`/api/session` 与 `/api/turn` 入口各调一次（**同步**方法，内部 create_task；
  这里踩过一个坑：写成 `async def` 却同步调用 → `coroutine was never awaited`，预热变哑弹）。
- 启动时 `_warm_live()`：连一条 + 起保活任务；`shutdown` 关掉。
- `/api/health` 增加 `tts_live`（mode / alive / connects / reuses / synths / aborted / 首个握手耗时 / 首块耗时）。
- `/api/debug/inject` 支持 `tts_live`：**进程内**切档位，A/B 不用重启（重启会带入缓存/连接/进程温度三类变量）。

### 3.3 `app/static/app.js`
- `roomBus()`：客户端 `ConvolverNode` 补房间混响，参数复刻 `postprocess.make_room_ir`
  （0.55s、6 个早期反射 11/17/23/29/37/47ms、指数衰减扩散尾、能量归一化到干声 RMS 的 0.16，
  再串一级 8.2kHz 低通）。**只接流式 PCM**，整句 mp3（服务端已加混响）不经过它，避免双重混响。
- `stopPlayback()` 清 `onplaying`：`play()` 是异步的，打断瞬间 `onplaying` 还没触发，
  不清它就会在 `stopPlayback(); setCaption("")` 之后又跑一遍 `setCaption("嗯……")`
  —— 这就是「打断时字幕残留『嗯……』」的根因，就在这里修掉。
- `streamDraining()` + `playNext()` 起播前等流式时间线排空：`MIRA_TTS_LIVE=first` 时
  第 1 句走流式 PCM、第 2 句起走整句 mp3，两条播放路径各有队列，不等就会叠音（上限 4s）。

### 3.4 开关（行为可一键回退）

| 环境变量 | 值 | 行为 |
|---|---|---|
| `MIRA_TTS_LIVE` | `all`（**默认**） | 每句都走长连流式（句间靠时间线预留衔接） |
| | `first` | 只第一句走流式，其余走原整句 mp3 管线 |
| | `0` | **完全不碰长连**，回到改造前的整句 mp3 行为（负控实测见 §5） |
| `MIRA_TTS_STREAM` | `0`/`edge` | 更粗暴的总开关：强制整句 mp3 |
| `MIRA_TTS_LIVE_IDLE` / `MIRA_TTS_LIVE_PING` | 25 / 12（秒） | 复用窗口 / 保活间隔 |

默认值为什么是 `all`：`first` 档第 2 句起仍然每句付 850ms 握手 + 1.5s 合成 + DSP，
一句话的回复里后面几句会各多等 1.5s。`all` 档下每句 ≈330ms 且能在上一句播放期间就排进时间线，
句间没有额外空白。两档都实测了（§4）。

## 4. A/B：同一 10 轮固定台词，交替 3 轮取中位数

脚本 `tools/tts/ab_first_line.py`（服务端时间戳 + **浏览器真实可听起点**，
playwright headless Chromium，`--autoplay-policy=no-user-gesture-required`）。
"可听起点" = 页面内 `AudioBufferSourceNode.prototype.start` 记录的时间线排期时刻
（加上 `at - ctx.currentTime` 的排期延迟），整句 mp3 则取 `<audio>` 的 `playing` 事件。

**设置**：同一进程（A/B 档位在线切换）、同一浏览器实例、同一批固定台词 10 轮 × 3 轮交替。
只看 `path=gen`（预设命中是 0 合成，会把中位数压平；预设轮次被排除，逐档见下表）。
三档的 `first_text_ms`（LLM 首字）p50 分别是 **848 / 834 / 804**，几乎相同——所以下面的差值就是 TTS 侧的差值。

| 档位 | n(gen) | 可听起点 p50 | p90 | max | 抖动(σ) | 服务端 first_line p50 | 首块 p50（浏览器侧） | 兜底次数 | 未完成 |
|---|---|---|---|---|---|---|---|---|---|
| `MIRA_TTS_LIVE=0`（改前行为） | 25 | **2482** | 3615 | 3920 | 468 | 2459 | — | 0 | 0 |
| `MIRA_TTS_LIVE=all`（默认） | 30 | **1121** | 1258 | 2414 | 360 | 844 | 310 | 0 | 0 |
| `MIRA_TTS_LIVE=first` | 30 | **1148** | 1435 | 3107 | 478 | 819 | 293 | 0 | 0 |

逐轮 p50（看轮间一致性，防"某一轮运气好"）：

| 轮 | off | all | first |
|---|---|---|---|
| 1 | 2394 | 1096 | 1110 |
| 2 | 2559 | 1187 | 1290 |
| 3 | 2447 | 1070 | 1157 |

减法（同机同网同批台词）：
- **p50：2482 → 1121（−1361ms，−55%）**；p90：3615 → 1258（−2357ms）；最坏值 3920 → 2414。
- 拆开看：`all` 档里 可听起点 = LLM 首字(834) + **TTS 首块 310 + 排期 ≈ 280**；`off` 档里 = LLM 首字(848) + **整句合成+DSP+下载 ≈ 1.6s + 23ms**。
- 抖动 σ 从 468ms 降到 360ms（p90−p50 从 1133ms 缩到 137ms，尾部明显收窄）。
- 失败率：三档都是 **0 次 /api/tts_file 兜底、0 次未完成、0 次 audio_error**。

为什么默认 `all` 而不是 `first`：`first` 档第 2 句起仍走整句管线，每句要 850ms 握手 + 1.5s 合成 + DSP，
一句话的回复里后面几句会各多等 1.5s（能听到"说一句卡一下"）；`all` 档每句 ≈330ms，
且能在上一句播放期间就排进音频时间线。实测 `all` 略快（1121 vs 1148），且句间更连贯。

## 5. 回退实测（负控）

`MIRA_TTS_LIVE=0` 就是"退回当前行为"的开关，上表的 `off` 档**就是**那条回退路径的实测：
可听起点 p50 **2482ms**、p90 3615ms、max 3920ms —— 和本轮之前记录的"第一句台词 p50 2.4–4.3s"一致。

切换方式两种，都验过：
1. **进程内**（A/B 用）：`POST /api/debug/inject {"session_id":"","tts_live":"off"}` → `/api/health.tts_live.mode` 立刻变 `off`，下一轮就走整句 mp3，不用重启；
2. **重启生效**：`$env:MIRA_TTS_LIVE='0'` 起服务 → 启动日志 `[warm] tts-live: …` 不再出现（`live_prewarm()` 直接 return），`/api/health` 里 `tts_stream:false`、`tts_live.mode:"off"`。

回退路径的行为完全等于改前：`stream_engine()` 返回 `None` → `tts_worker` 走 `tts.synth()`（整句 mp3 管线），
前端收到的还是 `{"t":"audio"}` 事件 —— 也就是说即使 edge 哪天改了协议、长连彻底不可用，这一条仍然能用。

## 6. 听感一致性：第一句和后面几句的混响

这条是**主观项为主、客观项为辅**，所以两样都给：可听的 wav + 客观指标。

文件（同一句台词「你一个人在这儿？在等人吗？」、同一个音色）：
- `media/voice_ab/p4/stream_dry.wav` —— 长连拿到的原始 PCM（**没补**混响/增益），用来听"不补会怎样"
- `media/voice_ab/p4/stream_conv.wav` —— 按客户端 `roomBus()` 同一套算法（0.55s IR + 8.2k 低通 + −18.8dB 增益）处理后 = **第一句实际听到的**
- `media/voice_ab/p4/mp3_dsp.wav` —— 现有整句管线（服务端 DSP）= **第 2 句起听到的**

客观指标（`tools/tts/render_ab_reverb.py` → `out/reverb_ab.json`，统一 24kHz）：

| 样本 | RMS dBFS | 频谱倾斜 dB/oct | 6–11k/300–3k (dB) |
|---|---|---|---|
| stream_dry | −1.79 | −3.52 | −5.1 |
| stream_conv（第一句） | **−21.29** | −3.62 | **−9.0** |
| 同上再过一遍 44.1k/96k mp3（归因用） | −21.06 | −3.48 | −5.2 |
| mp3_dsp（第 2 句起） | **−20.45** | −6.99 | −21.6 |

**响度（客观、已对齐）**：`ROOM_GAIN=0.115`（−18.8dB）把 edge 的满刻度裸音频压到服务端
`finalize` 归一化的 −20dBFS 附近。浏览器实测（`tools/tts/probe_room_level.py`，从真正送扬声器的
AnalyserNode 取样，3 轮共 143 个流式帧 / 220 个 mp3 帧）：

| 指标 | 流式 PCM | 整句 mp3 | 差 |
|---|---|---|---|
| 有声帧平均电平（db_mean，响度代理） | −20.5 | −21.35 | **+0.85 dB** |
| p75 | −18.35 | −18.21 | −0.14 dB |
| p90 | −17.66 | −17.02 | −0.64 dB |

→ 响度上两条路已经听不出差别（±1dB 内）。

**混响（主观项）**：`add_room` 是"整段卷积 + 湿声按干声 RMS 的 0.16 混入"，客户端用同一个 IR、
同样的干湿比，数学上是同一件事；差别只有两个：
1. 服务端的尾巴按句子截断（0.55s IR 的长度），客户端卷积器会跨句延续 —— 后者其实更接近真实房间；
2. 服务端还叠了 −58dBFS 粉噪底噪和 RMS 归一化，客户端没有（底噪 −58dB 听不见；归一化造成的差别
   已由 `ROOM_GAIN` 固定补偿，残余 ±2dB 浮动未做逐句自适应）。

**已知不一致（客观、未修）**：第一句比后面几句**亮** —— 6–11kHz 能量高约 12.6dB，频谱倾斜
−3.62 vs −6.99 dB/oct。归因：我把同一段流式 PCM 又过了一遍 44.1k/96kbps 的 mp3 编解码，
高频几乎没有变化（−5.2 vs −5.1），说明**不是编解码**造成的；`finalize(dull=True)` 单独作用能解释
约 6.4dB（−5.1 → −11.5），剩下约 10dB 在 30 分钟时间盒内没能归因（怀疑是整句管线里
逐小句合成 + 拼接引入的，未验证）。听感上表现为"第一句更清楚一点"。
**这一条是主观判断项**：wav 在上面，如果 Lead 觉得明显，最省的修法是给 `roomBus()` 加一个
high-shelf（比如 6.5kHz 起 −4dB），我没有默认打开——那等于为了迁就后处理痕迹去劣化第一句。

**另一处参数差异（小，但要记）**：第一句走 `providers.style_for()` 的**单组**参数，
第 2 句起走 `prosody.plan()` 的**逐小句**参数。实测同文本同情绪下两者最多差
3% / 4Hz（例：neutral「你一个人在这儿？在等人吗？」style_for `-5%/-5Hz`，
prosody 整句 `-2%/-4Hz`、两个小句分别是 `-3%/-8Hz` 和 `-8%/-6Hz`）。
所以第一句的语调起伏比后面几句略平。这是 brief 指定"情绪 style 仍按 providers.style_for"的直接结果；
要完全一致得让长连也按剪句发多次 SSML，那样每句首块会变慢，本轮**没做**。

## 7. 打断（barge-in）

**服务端必须立刻停、且不能把残音带进下一轮** —— 这一条用两个探针验证：

1. `tools/tts/probe_bargein_live.py`（HTTP 层打断）：

| 检查 | 结果 |
|---|---|
| 服务端是否感知到断开（`tts_live.aborted` +1） | **PASS**（+1） |
| 打断后马上再拉一句，流是否正常 | **PASS**（1800ms PCM） |
| 新句里有没有上一句的残音 | **PASS**——新句 1800ms，与"干净连接上同一句"的 1800ms **完全一致**（比值 1.000） |

   机制：客户端断开 → `StreamingResponse` 取消生成器 → `LiveEdge` 把连接标 `_poisoned`
   （**不能在这种上下文里 await**，见 §3.1）→ 下次 `ensure()` 丢弃重连。
   所以"上一条 turn 没读完的连接"永远不会被下一句复用。代价是打断后那一句多付一次 850ms 握手，
   实测仍在客户端 2s 首包超时之内（`probe_bargein_live` 里新句首块 395ms 属正常，重连发生在之前的预热里）。

2. 客户端「打断时字幕残留『嗯……』」：

| 检查 | 结果 |
|---|---|
| `tools/tts/probe_interrupt_caption.py`（在语气词刚开始播的那一帧打断，再等 1.8s） | **3/3 PASS**（300ms 后与 1.8s 后字幕都是空） |

   根因与修法见 §3.3（`stopPlayback()` 没清 `onplaying`，异步的 `play()` 兑现后又把字幕写回来）。

3. 原有 `tools/qa/bargein_probe.py`（麦克风真打断 + STT + 新一轮）：4.8s 内回到 idle，
   没有卡死、没有 toast 报错；流式请求确实被 `net::ERR_ABORTED` 掐断。

## 8. 回归

| 用例 | 结果 |
|---|---|
| `tools/qa/api_test.py` | **19 passed, 0 failed**（含打断后不再 done、新轮 seq 更大、STT 识别） |
| `tools/qa/bargein_probe.py` | 4.8s 恢复 idle，无卡死 |
| `tools/tts/probe_bargein_live.py` | 3/3 PASS（见 §7） |
| `tools/tts/probe_interrupt_caption.py` | 3/3 PASS（见 §7） |
| `tools/tts/check_live_edge.py` | 长连自检：首块 226–253ms；取消后重连 857ms 恢复；`aborted` 计数正确 |
| A/B 里的兜底/失败计数 | 三档各 30 轮：`/api/tts_file` 兜底 0 次、`audio_error` 0 次 |

一处**语义变化**要留意：`done.first_line_audio_ms` 在流式档下不再是"音频合成完"，
而是"音频 URL 已发给浏览器"（真实可听起点由浏览器侧量，见 §4）。老的日志/看板和新的不可直接对比。

## 9. 真假未做项（别把没测写成已实现）

**没测 / 没做（别当成已实现）**
1. **没有盲听（ABX）**。响度是对齐了（客观 0.85dB），混响"听不听得出来"只有 wav 供人耳判断，没有做 ABX 统计。
2. **第一句比后面几句亮**（6–11k 高 12.6dB，§6），只归因了一半（编码不是原因、`finalize` 低通解释约 6.4dB），剩余 ~10dB 未归因，也没修。
3. **并发没测**：一条长连上的合成是**串行**的（协议不允许交错）。四句并发时第 2–4 句会排队，
   单轮 3 句实测没问题（330ms × 3 仍在首句播放时间内），但**两个会话同时说话**没测——按当前实现会互相排队。
4. **断网/网络抖动下的重连成功率**只有一次负控（`probe_edge_timing` 第 3 段，1 次），没有统计。
5. **`first` 档只有 A/B 的 30 轮数据**，没有长期跑；默认是 `all`。
6. **保活策略依赖实测的空闲上限**（~70s 被服务端关）；如果 edge 改策略（更短或更长），
   需要重跑 `probe_edge_keepalive.py`。这不影响正确性（失败就重连/回退），只影响收益。
7. **`Sec-MS-GEC` DRM token 只在建连时算一次**。长连接期间不做 token 刷新——实测跨 5 分钟边界
   没断（保活只有 40–75s 的样本），更长时间的连接没验证；断了会重连并重新算 token。
8. **服务端 mp3→PCM 增量解码的 CPU 占用没量**（只量了端到端延迟）。每句 ≈3.2s 音频、
   解码耗时在 A/B 时间戳里看不出来（<10ms 量级），但没单独打点。

**已知代价**
- 比改前多一个模块（`app/tts/live_edge.py`，约 380 行）+ 3 个前端钩子（混响 bus / 起播门控 / onplaying 清理）。
- 打断后要多付一次 850ms 握手（下一轮预热里已经吸收，实测不影响 A/B 结论）。
- 服务端 `first_line_audio_ms` 的语义变了（见 §8 末尾）。

## 10. 证据索引

| 文件 | 内容 |
|---|---|
| `tools/tts/probe_edge_timing.py` / `out/probe_edge_timing.json` | 握手拆段 + 长连复用 + 断线恢复 |
| `tools/tts/probe_edge_keepalive.py` / `out/probe_edge_keepalive.json` | 空闲多久会被服务端关掉 |
| `tools/tts/ab_first_line.py` / `out/ab_first_line.json` | 三档 A/B（服务端 + 浏览器可听起点） |
| `tools/tts/probe_bargein_live.py` / `out/probe_bargein_live.json` | 打断能不能立刻停 + 有没有残音 |
| `tools/tts/probe_interrupt_caption.py` / `out/probe_interrupt_caption.json` | 打断后字幕残留（定点用例） |
| `tools/tts/probe_room_level.py` / `out/probe_room_level.json` | 两条播放路径在浏览器里的响度是否一致 |
| `tools/tts/render_ab_reverb.py` / `out/reverb_ab.json` | 混响一致性的可听 wav + 客观指标 |
| `tools/tts/check_live_edge.py` / `out/check_live_edge.json` | 长连自检（首块 / 取消 / 重连 / PCM 长度正确性） |
| `app/tts/live_edge.py` | 实现（长连 + 增量 mp3→PCM） |
