# 第二轮体验反馈 → 修复记录（2026-10-20）

> 这一轮不是按题目清单补功能，而是把「用户实际用起来不成立」的四件事逐个修掉：
> 对话接不住、声音像朗读、场景有假文字和幽灵杯子、以及题目对照说不清。

---

## 0. 一句话总结

| 用户反馈 | 根因 | 这一轮的处理 |
| --- | --- | --- |
| 对话答非所问、无法互动 | 4B 模型在长提示词下丢指令（温度 0.9 更放大），且旁白混进台词 | 主模型换 **25B（Qwen3.8-Flash-Next，本地 Strata）**；提示词重写成「静态前缀 + 动态尾巴」并命中前缀缓存；温度 0.65；流式旁白过滤；**自动降级链 strata→llama→mock** |
| 音频太人机、没有感情 | edge-tts 公开端点只接受 `prosody`，`mstts:express-as` 风格 SSML 被 **WebSocket 1007** 硬拒；`volume` 参数几乎不起作用 | 保留最不「播音」的 Xiaoxiao，加一层**后处理管线**：去 Edge 自带静音 → 按标点重插停顿/气口 → 小房间混响 + 高频柔化 + 底噪 → 44.1k；客户端句间停顿归零 |
| 前景两个杯子、背景店名乱码 | 生成提示词里**明确要求**了「两个白瓷杯 + 纸团」和「街对面的红色霓虹招牌」；图像模型画中文必乱码 | 用 Qwen-Image-Edit 2.1 做**局部重绘**（seed 11）：删杯子/纸巾、删霓虹招牌换成普通暗店面；再用 alpha 规则清掉贴片底部的杯沿残影 |
| 是否满足面试题 | 之前 README 只有分散描述，没有逐条对照 | 新增 §4「题目逐条对照」+ 本文件；已知问题按「哪些还没做到」重写 |

---

## 1. 对话：从「接不住」到「记得住」

### 1.1 先说一个自测陷阱（不是用户问题的原因）

我上一轮的验收脚本把会话 id 写成 `sid`，而 API 契约是 `session_id`。后端为每次请求新建会话，
于是**每一轮都没有历史**——这会把「模型答非所问」的结论放大成假象。
浏览器前端一直用的是 `session_id`（`app/static/app.js:567`），所以用户看到的问题另有原因，
但如果我只看脚本结论，会修错地方。

### 1.2 真正的三个原因

1. **模型能力不足**：Qwen3-4B-Instruct 在 1.2k token 的复杂提示词下会丢后半段指令，
   温度 0.9 时还会自创情节（"你不是说等末班车吗"）。
2. **提示词结构问题**：节拍/禁令/格式说明全部塞在系统提示中段，且要求「1-2 句、情绪藏在动作里」，
   小模型于是把「不主动说破秘密」执行成「用天气搪塞任何问题」。
3. **旁白混进台词**：模型会写「（低头看了一眼窗外）」「手指抚过照片边缘」，而流式字幕/TTS 是
   边生成边播的，等生成完再清理已经来不及——观众会**听见**括号。

### 1.3 这一轮的改法

- **模型**：新增 `StrataLLM`（本地 `127.0.0.1:1234`，Qwen3.8-Flash-Next 25B MoE，decode≈65 tok/s，
  请求带 `chat_template_kwargs.enable_thinking=false` 关掉思考）；原 4B 保留为第二档；
  新增 `FallbackLLM` 组成 `auto` 链：**Strata → llama(4B) → Mock**，第一个模型在吐出第一个字之前
  失败就换下一个。全部本地、不需要任何 API Key，符合题目「无私人密钥也能体验」。
- **提示词**（`app/persona.py`）：拆成 `_SYSTEM`（人设/场景/说话纪律/输出格式，**固定前缀**）
  + `_DYNAMIC`（节拍/禁说/台账/最近台词，**放最后**）。好处是 llama.cpp / Strata 的前缀缓存能命中，
  每轮只重新预填充尾巴。新增三条硬规则：只写会说出口的话、被问记不记得先给具体答案、
  不许用一句天气代替回答。
- **采样**：温度 0.9→0.65，`top_p` 0.9，关掉对 4B 有效的 repeat penalty（25B 不需要）。
- **流式旁白过滤**（`app/main.py:StageDirectionFilter`）：字符状态机，遇 `（ ( 【 [` 丢弃到配对结束，
  跨 chunk 有效；字幕和 TTS 都拿到干净文本；末尾再用无状态版兜底。
- **记忆**：修复事实抽取把「你还记得我**叫什么吗**」误抽成名字的 bug（新增 `_valid_name` 黑名单）。

### 1.4 实测（25B，5 轮连续对话，`tools/qa/turn_debug.py`）

```
U: 你好，第一次见面。你叫什么名字？        → Mira。……这雨刚停，外头冷得厉害，你坐吧。     4.2s
U: 我叫张伟，今天加班到十一点，手机也快没电了。 → 十一点……这店九点就停单了，你刚进来时灯还亮着。
                                             手机没电的话，靠窗这插座能用。              5.9s
U: 你还记得我叫什么吗？                    → 张伟。……你刚才说加班到十一点，手机快没电了。  4.2s
U: 你为什么一个人坐在这儿？是在等谁吗？      → 嗯。……我在等一个说好会来的人，十点半那班。    4.0s
U: 能给我看看你拍的照片吗？                → ……嗯。那张是三年前拍的。他答应十点半来，我就在那儿等。 7.1s
```

首包音频 2.5–4.3s，整轮 4–7s；每一轮音频段数 2–3（上一版同样内容是 12–43 段，见 §2.1）。

---

## 2. 声音：为什么「人机」，这一轮做到哪

### 2.1 先修掉一个把话念碎的真 bug

上一轮字幕/TTS 流水线里，累积文本被整段重复喂进过滤状态机，得到
`MMiraMira。`、`手机手机没手机没电手机没电，` 这样的文本，一句话被合成成 12 段音频，
一轮 50 秒。修法：过滤器只吃增量（`speech_raw[raw_fed:]`）。这条修复本身就是「人机感」的一半——
之前听起来像机器在复读。

### 2.2 edge-tts 的硬边界（实测结论，不是猜测）

- 自己构造带 `mstts:express-as` 的 SSML 提交给 Edge 朗读端点：服务端只回 `turn.start`，
  随即 **WebSocket close 1007**，0 字节音频；`<break>`、`<emphasis>`、`<say-as>` 同样被拒。
- 官方 SSML / 仅改 `xml:lang` / 仅声明 mstts 命名空间三者输出**字节完全相同**，跑两次 sha 也相同
  → 端点是确定性的，**任何风格标签都没被执行**。
- `zh-CN` 目录只有 8 个音色；Xiaohan/Xiaomo 直接无音频；目录外音色会被静默替换成 Xiaoyi。
- `volume` 参数对 `energy_cv` 几乎无影响（0.83–0.84），情绪只能靠**语速/音高/停顿**承载。

### 2.3 后处理管线（`app/tts/`，来自 `tools/tts_lab` 的验收版）

1. 裁掉 Edge 每句自带的 200–300ms 首尾静音；
2. 按标点重新插入停顿与气口（句号/问号/省略号不同）；
3. 情绪/标点/句长/位置（首句 or 追句）+ 文本哈希抖动决定 rate/pitch 与停顿；
4. 0.55s 小房间 IR、8.2kHz 柔化、-58dB 底噪，输出 44.1k mp3；
5. 客户端 `card.yaml → tts.gaps_ms` 全部归零，避免和音频内部停顿叠加成「一句一顿」。

客观指标（Xiaoxiao，同一句）：句内静音比 0.22→0.12（whole）/0.07（clause）；
频谱质心 1646→1411Hz；犹豫句尾 F0 由 +20.8Hz 上扬改为 **-63.8Hz 下倾**；
字/秒 3.26→3.12（不是靠拖慢制造感情）。详细表格见 [`docs/research/tts_voice.md`](docs/research/tts_voice.md)。

### 2.4 仍然不够的地方

真正的「演」需要换引擎：**CosyVoice2 / IndexTTS2** 这类带 instruct 情感控制的本地 TTS
（本机 12GB 显存、RTX 4070 SUPER 可跑 0.5B～2B 模型），本轮只做了 PoC 调研没接。
这是「音频有感情」的下一刀，README §12 已列。

---

## 3. 场景：假文字和幽灵杯子

### 3.1 根因

不是模型随机出错，而是**提示词写错了**：`tools/gen/gen_turn.py` 里明确写着
「FOREGROUND… two white ceramic cups, one half-finished… a crumpled paper napkin」
和「the red neon sign of a shop across the street」。
Qwen-Image 画中文招牌必然产出乱码字；杯子在「顾客视角」的构图里也没有主人。

### 3.2 修法

不重生成整套素材（贴片是由同一张 hero 帧派生的，重生成会全部错位），而是**局部重绘**：

1. `tools/gen/fix_plate2.py`：把原 plate 作为参考图交给 Qwen-Image-Edit 2.1，
   指令只做两件事——删前景杯/纸巾、删窗外霓虹招牌换普通暗店面，其余「保持像素级不变、
   不要加纹理/颗粒」。seed 11 最好（seed 1 会把整间店重绘成照片质感油画，已废弃）。
2. `tools/gen/clean_sprites.py`：用 alpha 连通域 + 空间规则（`y>1285 且 x<545` 置透明）
   清掉贴片底部残留的两段杯沿、杯柄和桌面影子——它们在旧 plate 上被杯子挡住，换新 plate 后
   会变成悬空的幽灵杯子。
3. `tools/gen/integrate_plate.py`：转 webp、更新 manifest、出合成验收图。

验收图：[`docs/review/plate_composite_check.png`](docs/review/plate_composite_check.png)。

### 3.3 诚实说明

- 这仍然是「生成的一家虚构咖啡馆」，不是某家真实店铺的复刻。用户建议的「找实际的店来生成」
  方向正确，下一步做法：拿真实店铺照片走 ControlNet depth/canny + img2img，保持真实布局，
  并且**所有招牌文字交给后期排版渲染**（Qwen-Image 2.1 对中文标牌可以，但需要专门测）。
- 人物贴片与 plate 的光比仍然靠手工调，极端姿势（stand）有 1-2px 位移。

---

## 4. 面试题逐条对照

| 题目要求 | 本实现 | 证据 / 位置 |
| --- | --- | --- |
| 页面以场景+角色为视觉主体，不要聊天列表 | 9:16 舞台，底板 + 角色贴片交叉淡入 + 字幕条 + 输入/麦克风 | `app/static/index.html`、`app/static/app.js` |
| 至少 4 种状态：待机/倾听/思考/说话 | `#stateLabel` + 贴片/动作条随事件切换 | `app.js` 状态机、`/api/turn` 事件流 |
| 用户能感知当前状态 | 左上角状态标签 + 动作 chip + 字幕 + 观测面板 | 截图 `docs/reports/live_3_reply.png` |
| 文本输入 | `POST /api/turn` | `app/main.py:turn` |
| 语音输入 | 按住说话（WebM/Opus）→ faster-whisper | `#micBtn`、`/api/stt` |
| 角色语音回复 + 同步字幕 | 逐句 TTS，字幕先出、音频按序 | `tts_worker`、`subtitle_partial` |
| 一次会话多轮 | 服务端 Session 保留 history/ledger/beat | `main.py:Session` |
| 语音打断 | 按住麦克风立即停音频 + 取消当前轮 | `/api/interrupt` + epoch/seq 双重失效 |
| 打断后：停音频/取消回复/不播未播事件/切倾听/迟到响应不覆盖 | epoch 递增 + 客户端丢弃旧 seq | README §4.2；`app.js` 的 `dropStale` |
| 结构化角色/场景指令 | 首行 JSON：emotion/action/scene/effect/event | `cards/mira_cafe/card.yaml`、`persona.DIRECTIVE_SPEC` |
| ≥3 种可辨认情绪 | neutral/worried/surprised/thinking/smile… 11 个贴片 | `assets/char/*.webp` |
| ≥2 种非说话动作/视觉状态 | look_window、sip_coffee、show_photo、lean_in、stand | 同上 |
| ≥1 次背景/镜头/环境变化 | scene 切换 + 关灯收尾拍（`pin` 固定） | `card.yaml` beats、`guard_story` |
| ≥1 个对话内容触发的明显视觉事件 | 照片揭示 / 末班车 / 雷声，关键词 + 模型 event 双通道 | `keyword_event`、`EVENT_KEYWORDS` |
| 视觉事件不能只靠计时器 | 事件绑定台词关键词 + 节拍门槛 | `main.py:keyword_event` |
| ≥1 种多模态表现（视频/生成图/动画） | **程序合成演出**：闪电补光 + 滚雷 / 照片静帧推近 / 车灯横扫（原 4 段 MP4 已删） | `app.js: triggerEvent` |
| 素材加载/结束/打断处理 | 静帧加载徽标 + `cinema` 层 + 打断即清 `src` | `app.js` |
| 可运行前后端闭环 | 一条命令 `start.ps1`，本地模型/Mock 双模式 | README §1 |
| 超时/断线/失败反馈与恢复 | LLMError → error 事件；TTS 失败逐句降级；模型 `auto` 链；`/api/debug/inject` 可注入故障 | `main.py` 异常分支、观测面板 |
| 快速连续输入/打断不播失效回复 | seq + epoch | README §4.2 |
| 无私人 Key 可体验 | Mock 模式 + 本地模型；`auto` 链全本地 | `LLM_MODE=mock` |
| README 6 项 | §3 结构、§4 语音打断、§5 指令设计、§7 模型/素材、§8 取舍、§10 已知问题、§11 投入时间、§12 两周演进 | README |
| AI_USAGE.md | 工具、分工、人工验证、AI 出错被修正的例子 | `AI_USAGE.md` |
| 3–5 分钟演示录屏 | **已随视频一起删除**（录的是旧状态）；需要时 `tools/qa/record_demo.py` 重录 | 无（`production/reports/demo/` 只剩截图） |

---

## 5. 这一轮之后的已知问题（诚实清单）

1. **演示录屏仍是旧素材旧模型**，需要按新场景/新语音重录一遍（§4 最后一行）。
2. 语音打断仍是「按住说话」显式打断；自动 VAD、回声消除、附和音区分都没做。
3. 25B 依赖本机常驻的 Strata 服务（`127.0.0.1:1234`）。没起时自动降级 4B，再没起降级 Mock；
   降级过程只有 `/api/health` 和观测面板可见，界面上没有明显提示（可加 toast）。
4. 场景仍是虚构店铺，不是真实店铺复刻；窗外街景是画的，不是视频。
5. TTS 仍是 edge-tts + 后处理，不是情感可控的本地 TTS；网络抖动时单句仍可能 8–12s。
6. 桌面端是居中竖屏卡片，不是自适应大屏布局。
7. 会话在内存里，重启即丢；节拍推进对「用户乱聊」的容错仍然依赖关键词。

---

## 6. 复现

```powershell
# 1) 本地模型（二选一，或都起，auto 链会自动挑）
powershell -ExecutionPolicy Bypass -File .\tools\llm\start_llm.ps1          # 4B 兜底
# 25B: 用 D:\model\Strata\run-iq2_xs-256k-vision.bat（端口 1234）

# 2) 应用（默认 LLM_MODE=auto：strata → llama → mock）
powershell -ExecutionPolicy Bypass -File .\start.ps1
# 打开 http://127.0.0.1:8787/

# 3) 自测
.\.venv\Scripts\python.exe .\tools\qa\turn_debug.py "你好" "我叫张伟" "你还记得我叫什么吗？"
```

素材返工复跑：

```powershell
python tools/gen/fix_plate2.py --seeds 11 --steps 20     # 需要 ComfyUI 8188
python tools/gen/clean_sprites.py                        # 清贴片残留
python tools/gen/integrate_plate.py                      # 转 webp + manifest + 验收图
```
