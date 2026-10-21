# 2026-10-21 语音回退说明：instruct2 把情绪指令念出来了 + 侧车 RTF 2.3

用户原话（11:51）：**「语音功能被你整坏了，你并没有正常说话，而且还变得很卡，
说话的内容也不是对话内容。」** 三条都能复现，而且是两个独立故障叠加：
**内容错**（指令被念出来）+ **节奏错**（RTF 2.3，音频追不上字幕）。
处理结果：`cards/mira_cafe/card.yaml` 的 `character.voice.engine` 从 `neural` 回退到 `edge`。

---

## 1. 「说的不是对话内容」= 情绪指令被当成台词念出来

神经路径用的是 CosyVoice2 的 **instruct2**：把「情绪指令」放进 `prompt_text` 位，
同时删掉 `llm_prompt_speech_token`。本仓库 vendored 的 LLM 里，`prompt_text` 是
**直接拼到 text 前面**的（`third_party/CosyVoice/cosyvoice/llm/llm.py:178`）：

```python
text = torch.concat([prompt_text, text], dim=1)   # prompt_text 会被当成要念的字
```

于是模型把 `EMOTION_INSTRUCT[emotion]` 这句话**原样念了出来**，再念台词。
`tools/tts/serve_neural.py:90` 的指令表就是被念出来的那几句。

### 证据（faster-whisper-small，CPU int8，`tools/qa/_asr_check.py`）

| 文件 | 时长 | ASR 听到的内容 |
|---|---|---|
| `docs/review/voice/ab_edge.mp3`（edge） | 5.21s | 我再等一個說好會來的人10點半 |
| `docs/review/voice/ab_old_ref.mp3`（神经·旧参考） | 6.60s | **用担心克制的语气说这句话声音低一点** 我再等一个说好未来的人10点半 |
| `docs/review/voice/ab_new_ref.mp3`（神经·新参考） | 5.76s | **用担心克制的语气说这句话声音低一点** 我再等一个说好会来的人10点半 |
| 线上实况第一段（语气词「嗯，」） | 1.32s | **用平静自然的语** ← 两个字的气口只剩这句指令 |
| 线上实况第二段（整句回复） | 9.32s | **用担心客制的语气若这句话声音低一点**再等一个人说好十点半的… |

结论：
* 换参考音频（3.73s → 8.55s）**不影响**这个故障，两份参考都会念指令；
* 我 11:44 交给用户听的三条 A/B 样本 **本身就是坏的**（两条神经样本的前 2.3s 是指令），
  所以「听感投票」这个环节得重来；
* 指令是每句都念，一轮 3–4 句就是三四次。这就是「说话的内容也不是对话内容」。

## 2. 「很卡」= RTF 2.3，音频永远追不上字幕

`tools/qa/_probe_neural_rtf.py`（非流式 `/synth`，12 字句，侧车就它一个请求）：

| 轮次 | 文本 | 音频 | 合成耗时 | RTF | 字/秒 |
|---|---|---|---|---|---|
| 1 | 这雨下了一整晚，没停过。 | 5.80s | 14.35s | **2.47** | 2.07 |
| 2 | 同上 | 5.54s | 12.68s | **2.29** | 2.17 |

流式同样跑不满：11:52 那次 `stream chunks=11 audio=9240ms total=21997ms`（RTF 2.38）。
RTF > 1 意味着**块到得比播放慢**，浏览器时间线上必然出现空档 → 断断续续。
叠加下面这条雪上加霜：

* `providers.TTSProvider.NEURAL_BUDGET_S = 6`（单句预算 6s）先判超时 → **回退 edge 再合成一遍**，
  同一句要付两遍代价；`logs/tts_neural.log` 里成对出现的同文本行就是它。
* 一轮说完 22s，用户下一句已经上屏，耳朵里还是上一句 → 「说的不是对话内容」的另一半原因。

## 3. 回退后实测（engine=edge，MIRA_TTS_LIVE 长连）

服务端探针 `tools/qa/_probe_live_voice.py` + 浏览器探针 `tools/qa/_probe_browser_voice.py`：

| 指标 | 神经（回退前） | edge（回退后） |
|---|---|---|
| 首音频 | 2457–2591ms（流式首块） | **385ms** |
| 一轮总耗时 | 21997ms（9.2s 音频） | **1199ms** |
| 音频时长 vs 文本 | 9.32s，ASR 听到指令 | 10.03s，ASR 与字幕逐字一致 |
| 连续说话 | 块间空档 | 3381ms→13531ms **一段连续**，中途没回 idle |
| 浏览器 console | — | 0 条错误 |

浏览器证据：`docs/review/voice_live/browser/<tag>/report.json` + `shots/`。

## 4. 现在的状态与下一步（待用户定）

* 默认档 = edge（`card.yaml` 里 `engine: edge` 并写了原因）。**听感回到 Xiaoxiao**，但内容、节奏都对。
* 想临时再试神经：`MIRA_TTS_ENGINE=neural`（env 优先于卡）。
* 要让「克隆音色」真的能用，必须先做两件缺一不可的事，**只做一件没有意义**：
  1. 换掉 instruct2：改成 zero-shot（`prompt_text` 用参考音频的转写，情绪不再靠指令），
     ASR 验到 CER 正常为止；
  2. 把 RTF 从 2.3 压到 0.8 以下，或只用于**预烘焙**（启动时预合成 14 条预设 + 开场白，
     命中缓存直出，延迟 0）。注意混用两套音色会更怪，所以这条只在整场演示都用预烘焙台词时成立。
* 判定「好没好」的方式从这次起固定为：`_probe_browser_voice.py`（时序）+ `_asr_check.py`（内容），
  不再拿「听着还行」当结论。
