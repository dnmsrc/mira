# Mira 语音方案研究：edge-tts 的 style 到底能不能用，以及怎么让它不像朗读

> 实验代码 `tools/tts_lab/`，样音 `media/tts_lab/`，指标原始数据 `tools/tts_lab/out/metrics.json`。
> 环境：Windows / Python 3.12 / edge-tts 7.3.1 / numpy / PyAV 14（自带 ffmpeg，负责 mp3 解码与编码）。
> 所有合成走同一台机器、同一时段，端点 `wss://speech.platform.bing.com/.../edge/v1`（Edge 朗读消费端，免费）。

---

## 0. 一句话结论

**edge-tts 的 style/情感 SSML 不可用**：服务端对任何 `mstts:express-as`（以及 `<break>`、`<emphasis>`、`<say-as>`）
直接以 **WebSocket close code 1007** 断开，一帧音频都不给；这不是「忽略」，是 SSML 白名单校验失败。
所以「情感」只能在 **参数（rate/pitch/volume）+ 停顿 + 后处理** 三层里做，
本报告给出的方案把「像朗读」的三个来源（每句参数一样、停顿靠客户端硬插、干声无空间）分别处理掉。

推荐：**zh-CN-XiaoxiaoNeural**，整句合成（不切小句）+ 按情绪/标点/句长/位置做参数与抖动 + 小房间混响/柔化/气口，
默认 `mode="auto"`（仅省略号或长句才切小句）。实现见 `tools/tts_lab/proposal.py`。

---

## 1. 实验一：style SSML 是否被服务端执行

### 1.1 做法

复用 edge-tts 的 DRM 令牌与连接逻辑，只替换 SSML 生成函数（`edge_tts.communicate.mkssml`），
把 `mstts:express-as style="sad" styledegree="1.4"` 套在 `<prosody>` 外层：

```xml
<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis'
       xmlns:mstts='https://www.w3.org/2001/mstts' xml:lang='zh-CN'>
  <voice name='zh-CN-XiaoxiaoNeural'>
    <mstts:express-as style='sad' styledegree='1.4'>
      <prosody pitch='-10Hz' rate='-8%' volume='+0%'>我在等一个……说好会来的人。十点半。</prosody>
    </mstts:express-as>
  </voice>
</speak>
```

同时写了裸 WebSocket 探针（`ws_probe.py`），因为 edge-tts 7.x 的 `__stream` 只处理 TEXT/BINARY，
收到 CLOSE 就静默结束循环，最后只抛笼统的 `NoAudioReceived` —— 看不到真实原因。

### 1.2 结果（同一句、同一 voice、同一 rate/pitch）

| 变体 | 音频字节 | close code | 说明 |
|---|---|---|---|
| 官方 SSML（对照组） | 28080 | — | 正常出音频，43 帧 |
| 只把 `xml:lang` 改 zh-CN | 28080 | — | 字节与对照组**完全相同**，lang 不影响 |
| 只声明 mstts 命名空间（不含 express-as） | 28080 | — | 字节相同，声明命名空间无害 |
| `express-as style='sad' styledegree='1.4'`（https ns） | 0 | **1007** | 只收到 `turn.start` 就断开 |
| `express-as`（http ns） | 0 | **1007** | 同上 |
| `express-as`（不带 styledegree） | 0 | **1007** | 同上 |
| `express-as`（xml:lang=en-US） | 0 | **1007** | 同上 |
| `express-as`（**连 style 属性都不给**） | 0 | **1007** | 说明拒绝的是元素本身，不是某个 style 名 |
| `<break time='520ms'/>` | 0 | **1007** | 标准 SSML 也不在白名单里 |
| `<emphasis level='strong'>` | 0 | **1007** | 同上 |
| `<say-as interpret-as='digits'>` | 0 | **1007** | 同上 |

`style` 名单（sad / gentle / calm / serious / disgruntled / cheerful / whisper / empathetic / affectionate）
全部同一结果：`NoAudioReceived`，二进制帧 0 字节。见 `probe_style.py` 与 `ws_probe.json`。

### 1.3 两条对照，排除了「服务端随机」和「我们写错 SSML」

1. **同一请求连跑两次，输出 mp3 字节完全相同**（sha 前 12 位都是 `8cf9a650ffa9`，时长都是 4.68s）。
   说明这个端点是确定性的：**只要输出字节不同，就一定是输入真的改变了行为**，不存在随机波动来冒充 style 差异。
2. `ns_only`（声明了 mstts 命名空间但不使用）= 官方的 28080 字节。所以 1007 不是命名空间声明导致，
   而是解析器遇到白名单外的**元素**就断。

### 1.4 结论

- **Edge 朗读消费端只接受 `<speak><voice><prosody>` 三个元素**，`mstts:express-as`、`break`、`emphasis` 一律 1007。
- 这符合产品事实：`express-as` 是 **Azure 语音服务（付费）** 的能力，Edge 浏览器「大声朗读」不需要、也不开放它。
  本机 `edge_tts.list_voices()` 返回的中文音色 `VoiceTag` 里也**没有 StyleList 字段**，只有 `Warm`/`Lively` 这类性格标签，
  进一步印证消费端不提供 style。
- 因此**不要再尝试 monkeypatch mkssml 注入 style**：它不会「静默降级」，而是让整次请求**没有任何音频**，
  在实时对话里等于这一句直接哑掉（比难听更严重）。
- 同理，**服务端停顿也做不到**：`<break>` 被拒，句间停顿只能客户端插，或者由我们合成时切小句自己拼（见第 4 节）。

---

## 2. 实验二：哪些音色真的可用

`list_voices()` 里 zh-CN 只有 8 个：

```
zh-CN-XiaoxiaoNeural      Female  Warm         ← 现方案，可用
zh-CN-XiaoyiNeural        Female  Lively       ← 可用
zh-CN-YunjianNeural       Male    Passion
zh-CN-YunxiNeural         Male    Lively/Sunshine
zh-CN-YunxiaNeural        Male    Cute
zh-CN-YunyangNeural       Male    Professional/Reliable
zh-CN-liaoning-XiaobeiNeural Female Humorous   ← 东北口音
zh-CN-shaanxi-XiaoniNeural  Female Bright      ← 陕西口音
```

题目点名的候选只有 2 个真能用，另外 3 个的结果本身就是结论：

| 候选 | 结果 | 证据 |
|---|---|---|
| zh-CN-XiaoxiaoNeural | ✅ 可用 | 3 句全部出音频 |
| zh-CN-XiaoyiNeural | ✅ 可用 | 同上 |
| zh-CN-XiaohanNeural | ❌ 不可用 | `NoAudioReceived`，3 句全失败 |
| zh-CN-XiaomoNeural | ❌ 不可用 | 同上 |
| zh-CN-XiaoxuanNeural | ⚠️ 不可依赖 | calm 句与 Xiaoyi **字节完全相同**（`2f081a9260`），另两句指标几乎一致 → 端点把未知音色静默替换成了别的音色 |
| zh-CN-liaoning-XiaobeiNeural | ✅（口音） | 可用，东北腔明显 |
| zh-CN-shaanxi-XiaoniNeural | ✅（口音） | 可用，陕西腔明显 |

> **坑**：传一个不在目录里的音色名，不一定报错——可能直接拿另一个音色合成。
> `Xiaoxuan` 的 calm 句和 `Xiaoyi` 一模一样就是这个现象。线上如果换音色，必须**核对试听/字节**，不能只看「没报错」。

---

## 3. 候选音色客观指标

三句台词（含标点、情绪对应卡包枚举）：

- `calm`（平静陈述，neutral）：「店里快打烊了。你要是只坐一会儿，老板娘应该不会赶你。」
- `question`（反问，smile）：「这个点还在外面的，一般不是刚下班，就是不想回家吧？」
- `hesitate`（犹豫带省略号，worried）：「我在等一个……说好会来的人。十点半。」

指标定义（`audio_metrics.py`，纯 numpy 自相关，无 librosa）：
时长、有效发声时长、字/秒（中文汉字数 ÷ 发声时长）、F0 均值/标准差/10–90 分位跨度、
句内下倾（5 帧中值滤波后，后 1/3 与前 1/3 的 F0 中位数差，负=越说越低）、
能量变异系数、句内静音比、有声段数（≈ 小句/停顿分段数）、频谱质心（亮度）。

### 3.1 五种女声 × 三句（现方案整句合成，未后处理）

| 音色 | 台词 | 时长s | 字/秒 | F0均值 | F0标准差 | 下倾Hz | 能量CV | 静音比 | 质心Hz |
|---|---|---|---|---|---|---|---|---|---|
| Xiaoxiao | calm | 5.86 | 4.54 | 236 | 56.6 | -42.4 | 0.78 | 0.22 | 1494 |
| Xiaoxiao | question | 5.23 | 4.91 | 254 | 56.7 | +28.9 | 0.74 | 0.15 | 1831 |
| Xiaoxiao | hesitate | 5.18 | 3.26 | 221 | 44.8 | +20.8 | 0.86 | 0.27 | 1646 |
| Xiaoyi | calm | 5.98 | 4.71 | 276 | 51.2 | -19.0 | 0.79 | 0.21 | 1765 |
| Xiaoyi | question | 5.14 | 5.41 | 307 | 54.5 | +47.1 | 0.80 | 0.17 | 2142 |
| Xiaoyi | hesitate | 5.16 | 3.55 | 250 | 54.6 | +18.9 | 0.94 | 0.28 | 1906 |
| Xiaobei（东北） | calm | 6.48 | 4.31 | 219 | 52.7 | -41.2 | 0.85 | 0.18 | 1225 |
| Xiaobei（东北） | question | 5.81 | 4.65 | 248 | 59.4 | -33.3 | 0.83 | 0.17 | 1380 |
| Xiaobei（东北） | hesitate | 5.81 | 3.11 | 216 | 57.1 | -10.2 | 0.99 | 0.22 | 1368 |
| Xiaoni（陕西） | calm | 6.41 | 4.35 | 226 | 56.0 | -46.1 | 0.92 | 0.25 | 1633 |
| Xiaoni（陕西） | question | 5.38 | 5.19 | 238 | 65.2 | -29.1 | 0.83 | 0.17 | 2076 |
| Xiaoni（陕西） | hesitate | 5.78 | 3.10 | 211 | 57.7 | -30.2 | 0.99 | 0.30 | 2004 |

读法：

- **Xiaoxiao 基频最低（220–254Hz）、质心最低（1494–1831Hz）**，是这几个里最「贴耳、不亮」的；
  Xiaoyi 高 40–50Hz、质心高 300–500Hz，听感更年轻更「播报」；两个方言声线更暗但口音会把角色带跑（Mira 是旅行摄影师，不是东北/陕西人）。
- 三句之间的**字/秒差异**已经说明「同一套参数不能通吃」：陈述 4.5、反问 4.9、犹豫 3.1 字/秒。
  现方案对省略号只减 5% 语速，实际还是偏快（3.26 字/秒 vs 人说话带省略号通常会到 2.5–3）。
- 句内下倾（`hesitate` 的 +20.8Hz）说明整句合成时 TTS 尾音是**上扬**的，和「越说越轻」相反——这是「像朗读」的客观来源之一。

### 3.2 同音色 A/B：现方案 vs 候选实现

| 音色 | 台词 | 策略 | 时长s | 字/秒 | F0均值 | F0标准差 | 下倾Hz | 能量CV | 静音比 | 有声段 | 质心Hz |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Xiaoxiao | calm | 现方案 | 5.86 | 4.54 | 236 | 56.6 | -42.4 | 0.78 | 0.22 | 9 | 1494 |
| Xiaoxiao | calm | whole | 5.26 | 4.52 | 235 | 52.8 | -25.4 | 0.74 | **0.12** | 3 | 1466 |
| Xiaoxiao | calm | clause | 5.41 | 4.49 | 235 | 50.2 | **-37.8** | 0.70 | **0.07** | 5 | 1472 |
| Xiaoxiao | question | 现方案 | 5.23 | 4.91 | 254 | 56.7 | +28.9 | 0.74 | 0.15 | 9 | 1831 |
| Xiaoxiao | question | whole | 4.79 | 4.79 | 250 | 59.4 | +41.3 | 0.73 | 0.07 | 3 | 1701 |
| Xiaoxiao | question | clause | 5.05 | 4.84 | 238 | 54.4 | **-26.8** | 0.67 | 0.04 | 3 | 1715 |
| Xiaoxiao | hesitate | 现方案 | 5.18 | 3.26 | 221 | 44.8 | +20.8 | 0.86 | 0.27 | 7 | 1646 |
| Xiaoxiao | hesitate | whole | 4.59 | 3.12 | 221 | 45.2 | +11.7 | 0.83 | 0.13 | 3 | 1411 |
| Xiaoxiao | hesitate | clause | 5.21 | 2.94 | 227 | 54.3 | **-63.8** | 0.91 | 0.20 | 3 | 1512 |

要点：

1. **质心下降 30–230Hz**（1494→1466、1831→1701、1646→1411）：高频被柔化，这是「干声/录音棚」感的主要来源之一。
2. **句内静音比从 0.15–0.27 降到 0.04–0.13**：现方案的长静音其实是 edge 自己在句号后塞的 400–600ms，
   加上客户端又按标点插 80–420ms，**两套停顿叠加**，听起来就是「一句一顿」。候选实现把停顿改成我们自己算的 170–540ms，
   并且不再需要客户端插。
3. **clause 模式把尾部下倾从 +20.8Hz 掰到 -63.8Hz**（犹豫句），这是「越说越轻」的直接证据；
   whole 模式只到 +11.7Hz，因为整句合成时 TTS 自己的尾音上扬改不动。
4. **字/秒基本不变**（±0.2），说明不是靠拖慢语速制造「感情」，语速没有被牺牲。

### 3.3 同一句话的六种情绪（参数化，替代 style）

`我在等一个……说好会来的人。十点半。`，Xiaoxiao，whole 模式：

| 情绪 | 时长s | 字/秒 | F0均值 | F0标准差 | 下倾Hz | 能量CV |
|---|---|---|---|---|---|---|
| neutral | 4.24 | 3.41 | 227 | 45.4 | +23.9 | 0.84 |
| smile | 4.08 | 3.54 | 235 | 48.6 | +14.7 | 0.83 |
| worried | 4.52 | 3.19 | 220 | 46.1 | +6.7 | 0.83 |
| surprised | 3.93 | 3.69 | 243 | 50.9 | +22.1 | 0.84 |
| thinking | 4.73 | 3.04 | 221 | 46.5 | +6.7 | 0.83 |
| listen | 4.24 | 3.41 | 227 | 45.4 | +23.9 | 0.84 |

情绪之间确实分得开（surprised 比 thinking 快 20%、高 22Hz），但**能量 CV 几乎不动（0.83–0.84）**：
edge 对 volume 的响应很弱，`+6%` 听不出明显更响。这也说明「情绪」目前主要靠**语速 + 音高**承载，
想要「气声、哽咽、笑腔」这类真正的表演，必须换引擎（第 7 节）。

> `listen` 与 `neutral` 指标完全相同是预期的：卡包里 `listen` 本来就是「不抢话的注视」，参数与 neutral 同源。

---

## 4. 去人机感后处理层（`tools/tts_lab/proposal.py`）

### 4.1 三层结构

```
text + emotion
   │
   ├─ 1) 韵律规划（prosody.py）
   │     情绪基调(rate/pitch/volume) × 标点(问句抬/省略号慢) × 句长(≤8字放慢, ≥28字略快)
   │     × 位置(first/follow/last) × 文本哈希抖动(±1.2% / ±1.8Hz / ±0.8%)
   │     可选：按标点切小句，逐小句做音高下倾（-1.6～-2.4 Hz/小句）与末句拖长
   │
   ├─ 2) 合成（edge-tts 官方路径，不注入任何 SSML）
   │     整句 1 次请求，或 2–4 个小句并行（Semaphore=4，实测 1.7–1.9s）
   │
   └─ 3) 空间化（postprocess.py，44.1kHz 单声道）
        裁掉每小句自带的首尾静音(200–300ms) → 按标点插精确停顿 + 气口
        → 小房间 IR 卷积（6 个早期反射 + 0.55s 扩散尾）
        → 高频柔化（8.2kHz 一阶滚降）
        → 铺 -58dBFS 粉噪底噪（停顿不再是数字静音）
        → RMS -20dBFS / 峰值 ≤ -1.5dBFS → mp3 96kbps
```

### 4.2 关键参数（每种情绪一套）

| 情绪 | 近似风格意图 | rate | pitch | volume | 下倾/小句 | 停顿缩放 | 混响湿度 | 气口 | 模式 |
|---|---|---|---|---|---|---|---|---|---|
| neutral | calm 平述 | -6% | -8Hz | 0 | -1.6Hz | ×1.00 | 0.16 | 0.55 | 整句 |
| smile | cheerful（克制版） | -2% | -2Hz | +4% | -1.1Hz | ×0.90 | 0.13 | 0.65 | 整句 |
| worried | sad / concerned | -12% | -12Hz | -6% | -2.4Hz | ×1.25 | 0.19 | 0.75 | 省略号时切小句 |
| surprised | surprised | +2% | +4Hz | +6% | -0.6Hz | ×0.80 | 0.12 | 0.35 | 整句 |
| thinking | hesitant / calm | -16% | -12Hz | -4% | -2.1Hz | ×1.35 | 0.20 | 0.85 | 整句，省略号切小句 |
| listen | gentle | -6% | -8Hz | 0 | -1.4Hz | ×1.05 | 0.16 | 0.50 | 整句 |

停顿基础值（毫秒，再乘情绪缩放与 ±12% 抖动）：`，` 170、`、` 130、`；` 240、`。` 360、`！` 330、`？` 390、`……` 540。

### 4.3 模式选择（实测）

| 文本 | whole | clause | 结论 |
|---|---|---|---|
| calm（2 标点，19 字） | 5.26s / 质心 1466 | 5.41s / 1472 | 差别不大，whole 更稳（不破坏 TTS 句内连贯） |
| question | 4.79s / 1701 | 5.05s / 1715 | whole 保留了自然上扬，clause 硬切后下倾更强但「像两句」 |
| hesitate（含……） | 4.59s / +11.7Hz 下倾 | 5.21s / **-63.8Hz** | 省略号必须 clause，才能做出「停一下、越说越轻」 |
| 延迟 | 1660 / 1925ms | 1895 / 1721ms | 并行小句几乎不增加延迟（±200ms） |

所以默认 `mode="auto"`：**只有省略号或「长句 + ≥2 标点」才切小句**，其余整句合成。
这样既保住 edge 自身的句内语调连续性（整句不会「一句一断」），又能在犹豫句上做真正的停顿与下倾。

---

## 5. 与现方案的对比

| 维度 | 现方案（app/providers.py） | 候选实现 |
|---|---|---|
| 情绪表达 | 每情绪一套固定 rate/pitch（6 套） | 情绪 × 标点 × 句长 × 位置 × 哈希抖动；同句每次仍确定 |
| 句内停顿 | edge 自带 + 客户端按标点再插一遍 | 自己裁掉重插，只插一遍，按情绪缩放 |
| 句间停顿 | 客户端 80–420ms，所有句子同一套 | 合在音频里，按位置/情绪变化，客户端可设 0 |
| 空间感 | 干声，数字静音 | 0.55s 小房间 IR + 8.2k 柔化 + -58dB 底噪 |
| 气口 | 无 | 句首长停顿前 120–180ms 极轻吸气（-30dB） |
| 采样率 | 24kHz mp3 48kbps | 44.1kHz mp3 96kbps |
| 高频亮度（质心） | 1494–1831Hz | 1411–1715Hz（降 30–230Hz） |
| 句内静音比 | 0.15–0.27 | 0.04–0.13 |
| 尾音下倾（犹豫句） | +20.8Hz（上扬） | -63.8Hz（落下） |
| 首次合成延迟 | ~1.6s | ~1.7–1.9s（小句并行） |
| 每句请求数 | 1 | 1（整句）或 2–4（切小句） |

### 主观听感（重要声明）

**我没有耳朵，下面是根据客观特征推断的预期，不是我真听过。** 样音已全部落盘，请按这个顺序试听：

1. `media/tts_lab/xiaoxiao__hesitate__base.mp3` vs `xiaoxiao__hesitate__clause.mp3`
   —— 听「我在等一个……」之后那一下停顿是不是自然，尾音有没有落下去。
2. `media/tts_lab/xiaoxiao__calm__base.mp3` vs `xiaoxiao__calm__whole.mp3`
   —— 听底噪和混响有没有让声音「进到房间里」，还是变闷了。
3. `media/tts_lab/emo_*.mp3` 六连播
   —— 听 surprised / thinking 的差异够不够，`volume` 是不是几乎没用（指标显示 CV 不变，预期如此）。

预期（待人工确认）：whole 模式应当「更近、更暖、停顿更短」，clause 模式在省略号句上应当明显「更犹豫」，
但**如果切分点选得不好，clause 会有「一句一句念」的顿挫**——这也是默认只用 auto 的原因。

---

## 6. 推荐方案

**voice：`zh-CN-XiaoxiaoNeural`**（备选 `zh-CN-XiaoyiNeural`，只在她需要更年轻/更亮时用；方言声线不建议）。

**style：没有 style**。用下面的参数近似（表见 4.2），并在代码里把 `style` 这个参数**彻底删掉**，
避免后人再试一次 1007。

**模式**：`auto`（省略号/长句切小句，其余整句）+ 后处理常开。

**客户端配合**：
- `gaps_ms` 全部设 0（或 ≤60ms），因为停顿已经写进音频；
- 播放器按 44.1kHz 处理，检查有没有硬编码 24kHz 的可视化/口型逻辑；
- 首句用 `position="first"`，追句 `"follow"`，收尾 `"last"`（会略微改变音高与停顿）。

**合并注意（按重要性排序）**：
1. **不要传 style**，任何 `mstts:*` 都会让整句没有音频（1007），实时对话里等于哑火；如果非要试，必须 try/except 回退。
2. **音色必须在 `list_voices()` 目录里**；不在目录的音色可能被静默替换（Xiaoxuan→Xiaoyi 的 calm 句字节相同）。
3. **客户端停顿要关掉**，否则和我们写入的停顿叠加，又回到「一句一顿」。
4. 缓存 key 要含 `VERSION|voice|emotion|position|mode|text`，否则改算法后拿到旧音频；
   显式传 `out_path` 时不要命中缓存（本次实验就踩到过：旧文件让情绪表全是错的）。
5. Windows 上合成失败时 `.part.mp3` 句柄可能没释放，`unlink` 会抛 `PermissionError` 盖住真实错误；
   `proposal.py` 里用 `_safe_unlink` 处理了，合并时保留。
6. 内层 `MAX_PARALLEL=4` 叠加 `main.py` 的 2 路并发，最坏 8 路打到 edge，可能被限流；建议内层降到 2–3。
7. 输出从 48kbps 变 96kbps，文件约 2 倍；307 个旧缓存不会被复用（key 变了），但也不会被删。

---

## 7. 下一步（如果要投入多天）

| 方案 | 能解决什么 | 代价 | 建议 |
|---|---|---|---|
| **Azure 语音（付费）** | 真正的 `mstts:express-as` style（sad/gentle/calm…）、`<break>`、role、styledegree | 按字符计费；需要 key；网络到 Azure 区域 | 如果预算允许，**这是最短路径**：同样的 SSML 在 Azure 端是合法的，本报告的 style 表可以直接搬过去 |
| **CosyVoice2** | zero-shot 音色克隆 + instruct 情绪（「用犹豫的语气说」）、中文自然度高 | 需要 GPU（或 CPU 慢速）；模型 ~2GB；部署 1–2 天 | 想要「Mira 专属音色」而不是「晓晓」时首选 |
| **IndexTTS2** | 工业级中文、情绪/时长控制、音色克隆 | 模型更大，显存要求高，3–5 天 | 如果要做到「配音级」且能上 GPU |
| **GPT-SoVITS** | 少量样本克隆、中文强 | 情绪控制弱（主要靠参考音频），更像「音色像」而非「演得好」 | 只解决音色，不解决表演 |
| **FishSpeech / F5-TTS** | 开源、克隆快 | 中文韵律与情绪弱于上面两个 | 备选 |

**决策规则**：
- 不能上 GPU、必须实时（<2s 首音）→ **留在 edge + 本报告的后处理**，先把「停顿/空间/参数抖动」做扎实，收益已经能看到（静音比 0.27→0.13、质心降 200Hz）。
- 能上 GPU、要「演」→ CosyVoice2（先做 1 天 PoC：同一句 6 情绪，人工盲听排序）。
- 有预算、要合规商用 → Azure，把本报告的 6 情绪参数表映射成 style + prosody，一天能切完。

**下一步实验（按优先级）**：
1. 人工盲听：把 `media/tts_lab/` 里 base/whole/clause 各 3 句打乱，找 3 个人做 A/B，记录「更像真人」的比例（这是唯一能验证「去人机感」的指标）。
2. 若盲听显示 clause 有顿挫 → 把切分点从「所有标点」收窄到「省略号 + 句号」，逗号不切。
3. 若「情绪不够」→ 试 Azure 的 6 个 style（同一段文本），复用本报告的 metrics 脚本做客观对比，再决定是否付费。
4. 加「不完美」：句首 0–80ms 的轻微延迟抖动、偶发的 30ms 换气、极轻的嘴角笑（smile 时 +2% 亮度）——但要有 A/B 证据再加，避免变成「加特效」。

---

## 8. 复跑

```powershell
cd D:\interview\mira
# style 是否可用的最小验证（约 30s）
.venv\Scripts\python.exe tools\tts_lab\probe_style.py
.venv\Scripts\python.exe tools\tts_lab\ws_probe.py

# 全量：候选 × 台词 × 策略，生成 media/tts_lab/*.mp3 与指标表（约 80s）
.venv\Scripts\python.exe tools\tts_lab\run_lab.py

# 改指标定义后只重算，不重新合成
.venv\Scripts\python.exe tools\tts_lab\reanalyze.py

# 延迟对比
.venv\Scripts\python.exe tools\tts_lab\bench_latency.py
```

文件说明：

| 文件 | 作用 |
|---|---|
| `tools/tts_lab/edge_style.py` | 自定义 SSML（含 express-as）封装，用于证明 style 不可用；生产不要用 |
| `tools/tts_lab/ws_probe.py` | 裸 WebSocket 探针，抓 close code / 服务端帧 |
| `tools/tts_lab/probe_style.py` | 多 style 对照 + 重复性对照 |
| `tools/tts_lab/audio_metrics.py` | 解码与客观指标（numpy 自相关 F0） |
| `tools/tts_lab/prosody.py` | 韵律规划（情绪/标点/句长/位置/抖动） |
| `tools/tts_lab/postprocess.py` | 混响/柔化/底噪/气口/拼接/44.1k 输出 |
| `tools/tts_lab/proposal.py` | **可合并的候选实现**，`async def synth(text, emotion, out_path)` |
| `tools/tts_lab/run_lab.py` | 全量实验与指标表 |
| `tools/tts_lab/reanalyze.py` | 离线重算指标 |
| `tools/tts_lab/bench_latency.py` | 整句 vs 小句延迟 |
| `media/tts_lab/*.mp3` | 样音（命名：`音色__台词__策略.mp3`、`emo_情绪__音色.mp3`） |
| `tools/tts_lab/out/metrics.json` / `metrics_table.md` | 原始指标 / 可贴表格 |
