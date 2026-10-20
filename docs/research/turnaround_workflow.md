# Mira：三视图 → 表情/动作派生 + 咖啡馆多机位 · 可执行素材方案

调研日期 2026-10-19 · 目标机 RTX 4070 SUPER 12GB · 只写方案，不改代码。

证据等级：`[官方]` 官方文档/仓库/模型卡（厂商或权重作者自己写的） · `[文档]` 教程文章 · `[社区]` 论坛/视频/帖子 · `[本机]` 我在本机核查（文件系统、端口、GitHub API、项目日志） · `[推断]` 我的推理，未验证。
链接前的 `★` = 本次实际打开并读到内容；没有 `★` 的只来自搜索结果标题，未逐条打开。

本文与 `docs/research/asset_workflow.md` 分工：那篇回答"用哪个模型"，这篇回答"五层资产怎么拆、11 个状态怎么不穿帮、今天先跑哪一步"。

---

## 0. 结论先行（6 条）

1. **五个问题不是"模型不够强"，是五层资产被挤进一条 prompt**：身份 / 道具 / 表情 / 场景 / 机位。一次生成只能稳住其中 2–3 层，剩下的必然崩，而且崩的位置每次不同。→ 必须分层生产（§4.3）。`[推断]`
2. **三视图不是运行时素材，是"身份锚"**。运行时要的是"同机位的坐姿 11 状态"。三视图解决"脸/发型/雨衣对不对"，**不解决"相机没画出来"**——那是道具层的事。`[推断]`
3. 本机唯一已跑通、边际成本≈0 的编辑链路是 Qwen-Image-Edit 2.1（`D:` 安装）。所以路线选 **(b) 指令编辑**，不选 (a) IPAdapter+openpose。`[本机]`
4. **"真转过去"是这个 base 的弱项**。Multiple-Angles LoRA 作者给出的 base 对照（step 0）显示：不加 LoRA 时，base 面对 `back view` 提示**基本还停在原视角**。→ 要真三视图，要么装 319 MB 的 LoRA，要么接受"伪三视图"。`[官方·模型卡]`
5. **"视点在吧台制作区"的根因很可能是：把当前机位看不见的道具写进了 prompt**（洗碗池、待洗杯具）。模型为了满足 prompt，只能把机位挪到能看见它们的地方。修法见 §5.4 的"机位-道具分配表"。`[推断，但与你的现象吻合]`
6. 今天约 2 小时可跑完最小验证（§8）。**第一个要判的不是"好不好看"，是"侧/背面到底转没转过去"**——这一条不过，后面所有派生都建在流沙上。

---

## 1. 本机事实核对（决定了后面所有取舍）

| 项 | 核查结果 | 影响 |
|---|---|---|
| F: ComfyUI 版本 | 0.33.2 `[本机]` | 老版本；缺 Qwen 2.x 节点 |
| F: custom_nodes | 仅 `ComfyUI-GGUF`、`ComfyUI-Manager` `[本机]` | **没有** controlnet_aux（depth 预处理）、Impact Pack（分区采样）、IPAdapter |
| F: `models/clip_vision` | **空** `[本机]` | 装 IPAdapter 必须另下 ViT-H（约 2.5 GB） |
| F: controlnet | `openpose_sdxl`、`control_v11p_sd15_openpose_fp16`、`diffusion_pytorch_model{,_promax}.fp16` `[本机]` | 后两个按命名应是 xinsir union SDXL 两版，**用前必须在节点里确认类型**，不能当事实 |
| F: upscale | `4x-UltraSharp`、`4xNomosWebPhoto_RealPLKSR`、`1xSkinContrast` `[本机]` | 够用 |
| D: ComfyUI 版本 | 0.37.0 `[本机]` | Qwen 2.1 链路在这边 |
| D: Qwen 权重 | `qwen_image_2.1_int8_convrot` 6.76 GB + `qwen3vl_8b_int8` 8.71 GB + VAE bf16 `[本机]` | 12 GB 可整模常驻（fp16 不行） |
| Qwen "Multiangle" 节点 | `D:` 源码树 grep `multiangle` **0 命中**；`F:` 只查了 `comfy_extras/*.py` `[本机]` | ComfyUI 官方那个 `QwenMultiangleCameraNode` 模板本机大概率不可直接用（也可能只是前端节点，未验证） |
| rembg | 项目 `.venv` 已装 2.0.85，但 `~/.u2net` **不存在** `[本机]` | 首次抠图要联网下 `isnet-anime`（约 176 MB） |
| Blender | **未安装**（C:/D: Program Files 均无）`[本机]` | 白模 depth 路线今天不可行 |
| 磁盘 | C 47.7 / D 48.7 / F 17.0 GB 空闲 `[本机]` | F: 装不下 Kontext 全家桶，D: 可以 |
| ComfyUI 进程 | 8188 / 8189 / 8000 均未监听 `[本机]` | 今天第一步是起服务 |

---

## 2. Q1 · 三视图的标准做法与社区工作流

### 2.1 行业标准（一句话）
**Model sheet = 正交前/侧/背 + 同一身高基准线 + T-pose 或 A-pose + 中性表情 + 平光纯色底 + 无透视**。目的是给 3D/动画提供几何依据，所以"对齐"比"好看"重要。`[社区]`

### 2.2 社区六类做法与各自代价

| # | 做法 | 身份 | 几何 | 代价 / 失败模式 | 证据 |
|---|---|---|---|---|---|
| 1 | **一张 grid 出 4 视图再切** | 弱 | 弱 | 面板互相污染、比例不一致、无法正交；只能当草稿 | `[社区]` |
| 2 | **逐视图 + openpose 骨架模板** | 弱（不控脸） | 强 | 要手搓骨架；手/脸容易崩；换装即失效 | `[社区]` [Reddit 骨架帖](https://www.reddit.com/r/StableDiffusion/comments/1180fls/sharing_my_openpose_template_for_character/)（打开被人机验证拦住，只读到标题，未核实内容） |
| 3 | **编辑模型一句话出整张 sheet**（Kontext / Gemini） | 强 | 中 | 面板细节糊；Kontext 专用 LoRA 作者明说"真人照片效果差""不擅长表情表" | ★[Kontext turnaround LoRA](https://huggingface.co/reverentelusarca/kontext-turnaround-sheet-lora-v1) `[模型卡]` |
| 4 | **多角度相机 LoRA / camera 节点**（Qwen `<mva>`、fal `<sks>`） | 中强 | 强（逐角度可控） | 要逐张跑；LoRA 在 512² 训练，大图属域外；不装 LoRA 时 base 不动 | ★[akhaliq/Qwen-Image-2.1-Multiple-Angles-LoRA](https://huggingface.co/akhaliq/Qwen-Image-2.1-Multiple-Angles-LoRA) `[模型卡]` |
| 5 | **3D 白模 + depth/lineart** | — | 最强 | 要建模；本机没 Blender | `[文档]` |
| 6 | **CharTurner 类 LoRA**（SD1.5/SD2.1） | 中 | 中 | 老底座，SDXL 线用不了 | ★[CharTurner](https://civitai.com/models/3036/charturner-character-turnaround-helper-for-15-and-21) `[社区]` |

另有两个官方模板值得知道，但**都需要云端 API key**，本机跑不了：
- ComfyUI 官方的 [Character Turnaround Sheet Generator](https://comfy.org/workflows/ad315e41d8c2-ad315e41d8c2/)：`GeminiImage2Node` + 参考图逐角度重绘 + 可预期文件名落盘。★ 页面明确写"依赖外部 API，无需下载模型"。`[官方]`
- ComfyUI 官方 [Generate Multiple Angles with 3D Camera Node](https://comfy.org/workflows/templates-qwen_multiangle.app-4724032fa666/)：`QwenMultiangleCameraNode` 直接转机位。★ `[官方]`

**结论**：社区所有"三视图"方案里，只有 **#4 逐角度生成**和 **#5 几何驱动**能称得上"真三视图"；#1/#3 得到的是"看起来像的四格图"。你的项目要拿去派生坐姿/表情，**只需要 #4**（甚至只需要前 3/4、侧、背三张）。

### 2.3 关键词表（直接抄）

| 用途 | 英文 | 中文 |
|---|---|---|
| 版式 | `character sheet`, `character design sheet`, `model sheet`, `reference sheet`, `turnaround`, `three-view`, `multiple views`, `line-up` | 三视图、角色设定图、角色设计稿、立绘、多视角 |
| 视角 | `front view`, `three-quarter view`, `side view / profile`, `back view`, `orthographic`, `no perspective`, `eye-level` | 正面/侧面/背面、正交视图、平视 |
| 姿势 | `T-pose`, `A-pose`, `arms at sides`, `feet shoulder-width`, `neutral standing pose`, `full body` | T 字站姿、A 字站姿、全身 |
| 底与光 | `plain white background`, `flat even lighting`, `no shadow`, `evenly spaced`, `height guide lines` | 纯白底、平光、无投影 |
| 表情表 | `expression sheet`, `bust shot`, `same framing`, `neutral / smile / worried / surprised` | 表情差分、头肩构图 |
| SDXL 质量 tag | `masterpiece, high score, great score, absurdres` | （Animagine 专用） |

---

## 3. Q2 · 2025 一代一致性方案：谁该进这条流水线

| 方案 | 类型 | 身份 | 保道具/饰品 | 12 GB 本机 | 无密钥 | 在这条流水线里的位置 | 证据 |
|---|---|---|---|---|---|---|---|
| **Qwen-Image-Edit 2.1（本机）** | 指令编辑 + 多图参考 | 强 | 中强 | ✅ 已跑通，30–45 s/张 | ✅ | **主线**：状态/表情/换机位 | `[本机]` + ★[2509 官方博客](https://qwen.ai/blog?id=7a90090115ee193ce6a7f619522771dd9696dd93) |
| **nano-banana（Gemini Flash Image）** | 网页/API | 强（整张 sheet 一次成型，业界口碑最好之一） | 强 | 网页端 | ✅ 免费额度（限额未核实） | **只做"参考稿/选型"**，不进 sprite 链路 | ★[AI Studio 模型页](https://aistudio.google.com/models/nano-banana)、★[API 文档](https://ai.google.dev/gemini-api/docs/image-generation) `[官方]` |
| **Seedream 4.0 / 即梦** | 网页 | 强 | 强 | 网页端 | ✅ 有免费额度（官网搜索摘要称"图片4.0 2K 限时免费"，未逐条核实） | 中文提示 + 4K，适合出场景与三视图参考 | ★[Seedream 4.0](https://seed.bytedance.com/zh/seedream4_0)、[即梦](https://jimeng.jianying.com/) `[官方]` |
| **Flux Kontext dev** | 指令编辑 | 强 | 中 | ⚠️ 能跑但慢（GGUF Q4–Q6，数分钟/张） | 本地 | 备胎：Qwen 改不动时试 | [12 GB GGUF 实测帖](https://www.reddit.com/r/StableDiffusion/comments/1lm2k1o/flux_kontext_what_ggufs_to_use_with_12_gbs_of_vram/) `[社区]`；dev 权重**非商用**许可 |
| **GPT-image-1** | API | 强 | 中 | 不可用（无 key） | ❌ | 排除 | `[推断]` |
| **IPAdapter（SDXL）** | 适配器 | 中 | 弱（传"感觉"不传具体饰品） | 可跑，但**无权重、无 clip_vision** | 需下载 3.5 GB | 排除出主线 | ★[h94/IP-Adapter](https://huggingface.co/h94/IP-Adapter/tree/main/sdxl_models)；仓库最后 push 2025-04-14 `[本机·GitHub API]` |
| **InstantID** | 适配器 | 强（脸） | 无 | 可跑 | 需 antelopev2+onnx | 排除（仅 SDXL、只锁脸） | `[文档]` |
| **PuLID / PuLID-Flux** | 适配器 | 强 | 无 | Flux 侧吃紧 | 需 EVA-CLIP + antelopev2 | 排除（解决的不是你的问题） | ★[ToTheBeginning/PuLID](https://github.com/ToTheBeginning/PuLID) `[官方]` |
| **ControlNet openpose** | 条件图 | — | — | ✅ 有 openpose_sdxl | ✅ | 只作为**骨架图**喂给 Qwen，不用它出图 | `[本机]` |
| **LayerDiffusion** | 生成即透明 | — | — | 需装 `ComfyUI-layerdiffuse` | ✅ | 备选，不如 rembg 省事 | ★[huchenlei/ComfyUI-layerdiffuse](https://github.com/huchenlei/ComfyUI-layerdiffuse) `[官方]` |
| **角色 LoRA** | 微调 | 最强 | 中（道具仍靠 prompt） | SDXL ✅ / Qwen 偏紧 | ✅ | **二期**，见 §4.4 | `[推断]` |

**取舍三句话**：
1. **能用网页免费额度解决的，只有"参考稿"**——分辨率、注册点、水印都不受你控，所以不要把它们当交付物。`[推断]`
2. **身份类技术（IPAdapter/InstantID/PuLID）解决"脸像"，你抱怨的是"发夹/相机/场景"——药不对症。** 而且三者本机都没装。`[本机][推断]`
3. **本地能出交付物的只有 Qwen 2.1 与 SDXL 两条**，前者强在"改"，后者强在"画风可控"。

---

## 4. Q3 · 一份三视图 → 多张同角色状态：怎么最稳

### 4.1 三条路线横向对比

| | (a) img2img + openpose + IPAdapter | (b) 指令编辑（Qwen / Kontext） | (c) 角色 LoRA |
|---|---|---|---|
| 准备成本 | 1.5–2 h：装 IPAdapter + clip_vision + 权重 3.5 GB；aux 预处理 | **0**（链路已存在） | 2–4 h：20–40 图 + 打标 + 训练 + 试采样 |
| 单张耗时 | 20–30 s（含 hires） | 30–45 s | 8–12 s（SDXL） |
| 身份保持 | 中（脸 60–70%） | 中强（80–90%） | 强（90–95%） |
| 道具保持 | **差（20%）**：IPAdapter 传氛围不传具体物件 | 中（50–70%，靠 prompt 绑定） | 中（仍靠 prompt/tag） |
| 失败模式 | 手崩、饰品消失、画风被拉平、骨架与坐姿自遮挡冲突 | 链式漂移、改表情顺带改衣领/背景 | 过拟合、换装失效、**把当前错误一起固化** |
| 适合 | 纯站姿立绘、姿势要精确 | 本项目的 11 状态、换机位、加道具 | 20+ 资产、要批量出视频帧 |

### 4.2 推荐：**b 为主线，a 的骨架只当"条件图"，c 放二期**

理由（按重要性排）：
1. 你的痛点是道具与场景，**只有"编辑"能表达"只改这一处、别动其它"**；a 路线的控制量是"姿势 + 脸"，无法表达"发夹别动"。`[推断]`
2. b 的边际成本≈0：本机已用它产出 11 形态并做过像素级对齐验证。`[本机·项目记录]`
3. 坐姿 + 手部交互（端杯、掏照片）在 openpose 里自遮挡严重，SDXL 出图手会崩；**编辑模型保留原手更稳**。`[推断]`
4. 2509 起官方原生支持 depth/edge/keypoint 条件图 `[官方]`，意味着 a 的骨架可以直接喂进 b，不需要维护两套链路。
5. **在有错误样本时训 LoRA = 把错误固化进权重**。等 11 状态验收通过、且要上视频批量帧时再训。`[推断]`

### 4.3 分层资产模型（把你的 5 个问题归因）

| 层 | 由谁保证 | 你看到的问题 | 该在哪一层修 |
|---|---|---|---|
| 身份 | 锚图 + 每次回锚图（不链式） | 不符合人设、脸不像 | 重出锚图，固定 seed 与服装词串 |
| 道具 | prompt 位置绑定 + 局部重绘 + 2D 贴片 | **发夹没画好、相机没有** | 位置/材质绑定 + crop 重绘 + 贴片兜底（§7.2） |
| 表情 | 头肩裁切单改脸 | **神态不对** | 只写一个变化量 + 肌肉级描述 + 只裁头肩 |
| 场景 | plate + 机位-道具表 | **穿帮（吧台制作区入画）** | 按机位分配道具清单（§5.4） |
| 机位 | 平面图 / depth / 全景 | **视点不对、反打重画房间** | 几何约束，不靠形容词（§5） |

> 一句话：**"发夹 + 相机 + 雨衣 + 表情 + 打烊道具 + 机位"同时写进一条 prompt，等于要求模型一次解 6 个约束——它每次会牺牲不同的一个。** 这就是"每次崩的地方都不一样"的机制解释。`[推断]`

### 4.4 如果真要训 LoRA（二期）

- 数据：**24 张起**（建议 4 角度 × 6 表情，或验收通过的 11 状态 × 2 seed），1024²，同一锚图派生，**不要混入失败图**。`[推断]`
- 参数起点（SDXL / kohya）：rank 16–32，lr UNet 1e-4、TE 5e-5，1500–2000 步，batch 1–2，梯度检查点开，优化器 adafactor/adafactor8bit。12 GB 可训，约 1.5–3 h。`[推断/社区]`
- Qwen 2.1（约 7B）本地训练：底座 + 优化器状态会顶到 12 GB 上限，**不推荐本地训**，要用就用 ai-toolkit + 云卡（Multiple-Angles LoRA 就是这么训的：ai-toolkit、rank 64、512²、2500 步）。`[模型卡][推断]`

---

## 5. Q4 · 同一间咖啡馆的多机位，怎么不穿帮

### 5.1 四种做法

| 做法 | 几何一致 | 成本 | 本机今天 | 失败模式 |
|---|---|---|---|---|
| 纯 prompt 换机位（"shot from behind"） | **不可用** | 0 | ✅ | 重画房间、跑到室外、窗/门数量变 |
| 场景三视图 + 图生图 | 弱 | 低 | ✅ | 只是"另一个像的咖啡馆" |
| **平面图线稿 + 编辑模型（多图）** | 中强 | 低（画 12 条线） | ✅ | 透视不严格、家具位置近似 |
| 白模 depth（Blender） | 最强 | 20–60 min 建模 | ❌ 未装 Blender | 归一化错、室内细节缺失 |
| 全景 equirect → E2P 裁切 | 强（无漂移） | 高（采样贵、裁切损失分辨率） | ⚠️ 需节点 + 全景模型 | 极点畸变、接缝、细节糊 |

参考：[ComfyUI 全景工作流](https://comfy.org/zh/workflows/33c621f7803f-33c621f7803f/) `[官方]`（页面正文未给文字说明，仅有工作流 JSON）；equirect→透视裁切节点见 [ComfyUI_pytorch360convert](https://github.com/ProGamerGov/ComfyUI_pytorch360convert)（搜索结果，未打开验证）；2509 + depth 的社区试验见 [r/StableDiffusion 专帖](https://www.reddit.com/r/StableDiffusion/comments/1om3z0r/qwenimageedit2509_and_depth_map/) `[社区]`。

### 5.2 推荐顺序
1. **今天**：主视点 plate 用 T3 直接生成；**第二个机位**用"平面图线稿 + 编辑模型"（§6 T4）。零安装，30–45 s/张。
2. **本周**：装 Blender 出白模 depth，把机位 3/4 做成几何驱动（12 GB 足够跑 SDXL union depth 或把 depth 当 Qwen 的第二张参考图）。
3. **要 4 个以上机位 / 要 4K**：再上全景 → E2P。

### 5.3 防穿帮 Checklist（打印出来逐项打勾）
- [ ] **元素计数**：窗格 4、吊灯 3、吧台长度占画面比例、桌椅数、门的位置——任一变化即 layout 漂了，重做而不是修图。
- [ ] **光源方向**：主光始终来自画面左侧 45°（吊灯），街灯只做轮廓光。
- [ ] **色温**：室内 2700–3000 K，窗外 5600 K，两者都要在同一张里出现。
- [ ] **机位高度**：坐姿眼高 1.15–1.25 m；站姿 1.55–1.65 m。写进 prompt，别靠"eye level"含糊。
- [ ] **道具状态连续**：杯子数量、椅子是否倒扣、CLOSED 牌朝向，跨机位必须一致。
- [ ] **不写看不见的东西**（见 5.4）。

### 5.4 机位-道具分配表（这条最省钱）

| 机位 | 画面里能看到 | 只准写这些道具 | **绝对不要写** | 理由 |
|---|---|---|---|---|
| **A 客人区 → 窗边桌**（主，9:16） | 窗、街景、桌面、门、部分吊灯 | 未收餐具、倒扣椅子、CLOSED 牌、拖把、湿脚印、窗上水痕 | 洗碗池、待洗杯具、espresso 机、制作台 | 写了模型就必然把机位挪到能看见它们的地方 → **就是你现在这个 bug** `[推断]` |
| **B 窗边反打 → 吧台** | 吧台、洗碗池、后墙、吊灯 | 待洗杯具、水龙头、抹布、蒸汽 | 窗外街景细节（会被画成"窗在背后"） | 看不到的窗，模型会硬塞一扇 |
| **C 侧面中景** | 桌子侧面、窗、椅背 | 桌面物件（杯、相机、照片） | 吧台（会挤进半个画面） | 中景最容易被"补背景" |

---

## 6. Q5 · 提示词模板（英文，可直接填空）

> 语言纪律：Qwen 系中英皆可但**同一项目只用一种**；SDXL/Animagine 是 tag 训练，**只能 tag 语法，长句会被忽略** `[模型卡]`。

### T1 · 角色三视图（Animagine / SDXL，tag 语法，横向 1344×768）
```
1girl, solo, original character, character sheet, reference sheet, turnaround,
front view, three-quarter view, side view, back view, full body, standing,
A-pose, arms slightly away from torso, feet shoulder-width, neutral expression,
chestnut brown wavy medium hair, silver star hairpin on her left temple,
amber yellow long raincoat, two front pockets, black vintage camera on leather neck strap,
orthographic, no perspective, plain white background, flat even lighting, no shadow,
evenly spaced, height guide lines,
masterpiece, high score, great score, absurdres
```
Negative：
```
lowres, worst quality, low quality, bad score, low score, bad anatomy, bad hands,
extra digits, fewer digits, extra limbs, extra arms, fused fingers,
merged bodies, fused limbs, inconsistent character design, different face, different hairstyle,
cropped, out of frame, perspective, foreshortening, dynamic pose,
background scenery, furniture, text, signature, watermark, logo, blurry
```
**为什么这么写**
- `character sheet, reference sheet, turnaround` 三个版式词叠加，比单个 `multiple views` 触发率高。**原创角色不要写 `mira \(series\)` 这种 tag**——模型没有这个 tag，会当噪声；用 `original character`，把外观全部写成可见属性。这是"一次出整张"的写法，融合风险最高；要严格对齐就同 seed 逐张跑（首行只换 `front view` / `side view` / `back view`），再后期拼版。`[社区][推断]`
- **负向里绝不能写 `multiple girls / 2girls`**——那正是你要的 4 个人形。要压的是"融合/串味"：`merged bodies, fused limbs, inconsistent character design`。这是社区模板最常见的自伤错误。反过来，如果只画出 1 个人，把 `(character sheet:1.2)` 提权，或者干脆逐张生成。`[推断]`
- `height guide lines` + `evenly spaced` + 纯白底，是把"四格图"变成"可对齐 sheet"的关键；没有它们，四个视图身高会不一致。
- 小饰品写两处：正向 `silver star hairpin on her left temple`（**绑位置**才能防镜像），且**不要**再加 `hair ornament` 之类的泛词（会生成一堆别的发饰）。
- `no perspective` 压的是"艺术照式构图"，不写这三个词，模型会给你 3/4 透视。

### T2 · 单人表情/动作（Qwen 编辑，固定机位；也是运行时 11 状态的模板）
```
Same woman, same face, same chestnut wavy hair, same silver star hairpin clipped above her LEFT ear,
same amber long raincoat, same black camera hanging on its leather strap across her chest,
same café window table, same camera position across the table, same framing, same lighting.

Change ONLY this: [ACTION_OR_EXPRESSION].

Keep the background, the crop, the colour grade and the props unchanged.
Do not add or remove any object. Do not change the hair or the coat.
```
填 `[ACTION_OR_EXPRESSION]` 的写法（**动作必须拆成"手-物-接触点-视线-躯干角度"五件事**，否则模型只换姿势不换动作）：
- `she is sipping coffee: right hand lifting a white ceramic cup to her lips, elbow resting on the table, cup touching her lower lip, eyes lowered to the cup, torso leaning forward 10 degrees`
- `she is thinking: chin on her left palm, elbow on the table, eyes looking up-left past the camera, lips pressed, brows slightly drawn`
- `she is showing a photo: both hands holding a small glossy print at chest height, print facing the viewer, chin lifted, eyes on the viewer`
- `worried: inner brow corners raised and drawn together, lower eyelids tense, mouth slightly open at the corners, gaze down-left`
- `surprised: eyes wide, upper eyelids raised, brows high, lips parted, head pulled back 5 degrees`

**为什么这么写**
- 开头的常量串是**合同条款**：每多写一遍，模型改动它的概率就低一档；这是"只改一处"唯一可靠的实现方式。`[推断]`
- 只允许一个变化量。写"微笑并转头看窗外"= 两个变量，必然有一个不执行。
- 不说"背景不变"时，Qwen 会顺手把背景重画（这是它"编辑"能力的副作用）。
- 项目里 `listen / look_window / lean_in` 三张辨识度低，就是因为只写了状态词、没写"手在哪、躯干多少度"。补上五要素即可拉开差异。`[本机·ASSETS_REVIEW §5]`

### T3 · 空场景板 A（Qwen t2i，864×1536，客人区视点）
```
Photoreal cinematic interior, a small neighbourhood café fifteen minutes after closing,
on a night when the downpour has just stopped. Vertical 9:16 frame.

CAMERA: standing in the guest area, 2.5 m in front of the window table, lens at 1.2 m
above the floor (seated eye level), 35 mm, no wide-angle distortion. The bar counter is
BEHIND the camera and must not appear in frame. No prep area, no sink, no espresso machine
in view.

WHAT IS IN FRAME: one large rain-streaked window with four vertical mullions, a wet
pavement outside reflecting a red neon sign, a small round table with two chairs, a door
at the back right, three pendant lamps above (one switched off).

SET DRESSING (countable): one plate with a fork and crumbs left on the table, two chairs
upside down on the next table, a folded grey rag on the window sill, the CLOSED sign
already flipped, wet footprints drying by the door, condensation running down the glass.

WEATHER: no rain is falling. Rain has stopped. Wet asphalt, puddles with concentric
ripples, water still dripping from the awning, overcast sky clearing, cold blue-grey
exterior against warm interior.

LIGHTING: 2700 K tungsten pendants as the only key from camera-left, hard falloff,
corners near black; 5600 K street spill as a rim on the window frame.

No people. No text. No watermark.
```
Negative：
```
rain, raining, downpour, heavy rain, rain streaks, falling rain, storm, lightning,
umbrella, splashing water, wet hair, people, person, crowd, staff, bartender,
bar counter in foreground, kitchen, prep area, sink, espresso machine,
text, watermark, logo, fisheye, HDR glow, oversaturated, cluttered
```
**为什么这么写**
- **"雨后"必须三件事同时做**：正向断言 `no rain is falling / rain has stopped` + 负向压 `rain, raining, storm` + 给替代物证（湿地面、涟漪、滴水、窗上水痕）。只写 `after rain` 会得到正在下雨——因为"雨夜咖啡馆"的高频共现就是雨丝。`[推断，机制与社区经验一致]`
- 打烊细节要**可数**（`one plate`、`two chairs`、`four mullions`、`three pendant lamps`）。数量词是把"氛围"变成"布景"的唯一手段。
- 视点用**空间介词 + 排除法**（`behind the camera and must not appear`、`no prep area in view`），形容词（wide/cozy）对机位无效。
- 灯只给一盏关掉，比写 `dim lighting` 更能表达"打烊"——因为模型会把"暗"理解成曝光不足，而不是"少了一盏灯"。

### T4 · 场景反打机位（Qwen 多图：picture 1 = 已定稿 plate，picture 2 = 平面图线稿）
```
Picture 1 is the approved café plate. Picture 2 is a top-down floor-plan sketch of the
SAME room; the red arrow marks the new camera position and the direction it faces.

Render the same café from that new camera position: same window with four mullions, same
three pendant lamps, same counter run, same tables and chairs, same set dressing, same
time of night, same colour temperature.

Keep the count and the position of every architectural element identical to picture 1.
Move nothing. Add nothing. Remove nothing. No people, no text.
```
**为什么这么写**
- 反打是"模型没见过的墙"，**必须给几何**；prompt 里的 `turn around / shot from behind` 只会让它重画正面或跑出室外。`[社区][推断]`
- 平面图线稿是最便宜的白模：你只需要画"矩形 + 箭头"，它提供的是**拓扑**（谁在谁的左边），这正是编辑模型能吃的条件。`[推断]`
- 若 Qwen 不吃线稿：退到 SDXL union depth（本机有 `diffusion_pytorch_model_promax.fp16`，需在节点里确认是 depth 类型），strength 0.6–0.8。`[推断]`

---

## 7. Q6 · 实操细节

### 7.1 分辨率与采样链

| 用途 | 尺寸 | 采样 | 说明 |
|---|---|---|---|
| SDXL 立绘 / 三视图单张 | 832×1216（竖）/ 1344×768（横） | Euler a，28 步，CFG 5 `[模型卡]` | Animagine 官方推荐值；`>1536` 会退化 `[模型卡]` |
| SDXL hires 第二段 | 1.5×（1248×1824） | 同 sampler，20–24 步，**denoise 0.40–0.50** | denoise >0.55 会重画脸；<0.3 只是放大 `[推断]` |
| Qwen 编辑（状态） | 864×1536（与运行时一致） | cfg 1.0，euler/simple，25 步，30–45 s `[本机·日志]` | 尺寸必须与 plate 完全一致，否则贴不回 |
| Qwen Turbo（快速试错） | 同上 | 8 步，cfg 1 `[社区]` | 试构图用它，定稿用 25 步 |
| 抠像后交付 | 864×1536 WebP q90 | — | 项目现状 43–49 KB/张 |

节点顺序（Qwen 编辑）：`LoadImage(plate)` → `TextEncodeQwenImage21(images.image_1=plate, image_2=anchor)` → `KSampler(cfg 1.0)` → `VAEDecode` → `SaveImage`。
节点顺序（SDXL）：`CheckpointLoader` → `CLIPTextEncode ×2` → `EmptyLatentImage` → `KSampler` → `VAEDecode` → `ImageScaleBy 1.5` → `KSampler(denoise 0.45)` → `VAEDecode` →（可选）`ImageUpscaleWithModel(4x-UltraSharp)` → 缩放回目标尺寸。

### 7.2 保住小饰品的手段（按成本递增）

1. **位置绑定 + 权重 + 换 seed 重抽**：`silver star hairpin clipped above her LEFT ear, clearly visible`；去掉一切泛化的 `hair ornament`；负向加 `no other hair accessories, mismatched earrings`。同 prompt 换 4 个 seed，命中率大约 1/4，是最便宜的一步。`[推断]`
2. **Crop + inpaint（主力）**：把头部裁到 1024 级再重绘，mask 羽化 8–16 px，回贴时保持同一光照参考图。**这是社区公认"接缝差色"的成因**——整图上直接重绘必出色差 `[社区，B站 BV1uanE6dESy]`。
3. **分区采样**：装 `ComfyUI-Impact-Pack`（F: 还没装），用 `Regional Sampler` + mask 分区，只让饰品区域吃 prompt（A1111 里同概念叫 Regional Prompter）。`[官方]` ★[ComfyUI-Impact-Pack](https://github.com/ltdrdata/ComfyUI-Impact-Pack)
4. **2D 贴片（生产兜底）**：864 宽的画面里，发夹只有 **12–18 px**。指望模型每次都画对不现实；正确做法是"生成时保证有一处银色高光小饰物"，再用一张自己画的星星 PNG 贴上去（可复用、可统一大小）。这不是作弊，是资产管线。`[推断]`

相机同理，但更简单：**相机是"挂在身上"而不是"拿在手里"**时最稳——`black vintage camera hanging on its leather strap across her chest` 比 `holding a camera` 稳定一个量级（手部交互会抢走全部注意力）。`[推断]`

### 7.3 透明贴片

| 方案 | 质量 | 成本 | 结论 |
|---|---|---|---|
| **rembg + isnet-anime** | 头发/雨衣边缘 1–2 px 软边，够用 | 项目已装，首次下模型 176 MB | **首选**（项目现用，已验证） `[本机]` |
| LayerDiffusion（SDXL） | 生成即透明，但细发丝不如 isnet-anime | 要装节点 + 权重 | 只在"一次出 20+ 张、不想跑 rembg"时考虑 `[官方]` |
| 色键（绿幕） | 硬边、溢色 | 0 | 不推荐，雨衣是琥珀色，与绿幕对比还好但边缘会脏 `[推断]` |

后处理三步：`alpha matting` 去半透明 → **1 px erode**（吃掉背景残留）→ 反预乘去色边（边缘颜色用邻近前景色替换），再导出 WebP。

### 7.4 统一色调（"两张皮"的解法）

"两张皮"几乎总是这四个不匹配里的一个：**色温 / 对比度 / 噪声颗粒 / 边缘光方向**。按顺序修：
1. **对齐直方图**：把 sprite 的暗部与中间调对齐 plate（曲线或 Lab 的 L 通道），别动色相。
2. **Light wrap**：plate 模糊 8–12 px 后以 `screen` 10–15% 叠在 sprite **边缘**（不是整张），让背景光"包"进人物轮廓——这是让贴片"坐进"场景最有效的一步。`[推断]`
3. **统一颗粒**：plate 与 sprite 叠同一层噪声（不要只给人物加，会露馅）。
4. **一次全局 grade**：合成后再上同一 LUT/曲线，这一步能掩盖前两步的残差。

验收：跨机位合成**不能**用"像素差 <2/255"当标准（那只对"同一帧减人"成立）；要看**边缘 1–2 px 是否有亮边/暗边**和**人物肤色与背景光是否同一色温**。

---

## 8. Q7 · 今天就能跑的最小验证（约 2 小时）

前置：`D:\ai\qwen-image\comfyui-src` 起服务（8188 当前未监听）。全程用现成工作流，不改代码。

| # | 动作 | 耗时 | 成功判据（看哪张图） | 失败 → 改法 |
|---|---|---|---|---|
| 0 | 起 ComfyUI，用现成 11 状态工作流出 1 张，确认环境 | 10 min | 出图且尺寸 864×1536 | 报错先修环境，别往下走 |
| 1 | **出身份锚**：Mira 全身正面、灰底、不带场景（人物卡式） | 10 min | 发夹在**左**、相机挂在身上、雨衣两个口袋 | 缺件 → 加位置绑定重抽 3 张 seed；全崩 → 回 SDXL 出锚 |
| 2 | **从锚图出 `back view`（先不装 LoRA）** | 15 min | 背面是真背面（看不到脸、背带在背上） | 还停在正面 → 装 Multiple-Angles LoRA（319 MB，2 min），strength 1.0 重跑 |
| 3 | 从锚图出 3 个表情（smile / worried / surprised），头肩裁切 | 20 min | 发夹与刘海不动、眼神有方向、嘴角/眉形符合描述 | 背景被重画 → 加"keep background unchanged"；表情过火 → 拆到肌肉级描述 |
| 4 | **出场板 A**（T3，864×1536） | 25 min | ①画面里**没有雨丝** ②**没有吧台/制作台** ③能数出 3 个打烊道具 | 有雨 → 负向加 `rain, raining, storm` 并正向断言 `rain has stopped`；有吧台 → 正向加 `bar counter is behind the camera`，负向加 `bar counter, kitchen, prep area` |
| 5 | 角色入景（T2 neutral：plate 当 image_1，锚图当 image_2）→ 抠像 → 贴回 | 30 min | 边缘 1–2 px 软边、肤色与背景同色温、发夹仍可见 | 色差 → 走 §7.4；发夹丢 → 走 §7.2 第 4 级 |
| 6 | （可选）反打/中景：画一张平面图线稿 → T4 | 20 min | 窗格 4、吊灯 3、吧台长度比例不变 | 布局漂 → 放弃线稿，转白模 depth（本周任务） |

**第一个真正的决策点在步骤 2**：如果 base 转不过去、LoRA 也救不回来，那"三视图"这条路线在本机就应降级为"单张 3/4 参考 + 全部靠编辑派生"，别在几何上继续投入。`[推断]`

---

## 9. 明确跑不动的路线（别浪费时间）

| 路线 | 为什么跑不动 | 替代 |
|---|---|---|
| ComfyUI 官方 Gemini 三视图模板 | 需要 Google API key，本机无 `[官方]` | 网页端 AI Studio 手动跑一次，只取参考 |
| GPT-image-1 | 无 API key | nano-banana 网页端 |
| Flux Kontext 全家桶 | 下载 ≈12 GB，F: 只剩 17 GB；12 GB 上 Q4/Q6 每张数分钟 `[社区]`；dev 权重非商用 | Qwen 2.1；实在不行再用 Kontext 单张救火 |
| IPAdapter / InstantID / PuLID | 权重全无，需 3.5 GB+；IPAdapter 仓库自 2025-04-14 无 push `[本机·API]`；且只锁脸不锁道具 | 不用装 |
| Qwen 1.0 的 20B ControlNet（InstantX/DiffSynth） | 权重按 20B 发布，与本机 2.1（≈7B，int8 6.76 GB）不兼容 `[推断]` | depth 图当第二张参考图 |
| Blender 白模 depth | Blender 未安装 | 今天用平面图线稿；本周装 |
| 本地训 Qwen LoRA | 7B + 优化器状态顶满 12 GB `[推断]` | 训 SDXL LoRA；或云卡 |

---

## 10. 来源清单

**官方 / 模型卡**
- ★ Qwen-Image-Edit-2509 官方博客（多图输入、原生 depth/edge/keypoint、人物一致性）https://qwen.ai/blog?id=7a90090115ee193ce6a7f619522771dd9696dd93
- ★ Qwen-Image-2.1 Multiple-Angles LoRA（`<mva>`、36 姿态、v2 rank64/step1500、319 MB、base 不加 LoRA 不动镜头）https://huggingface.co/akhaliq/Qwen-Image-2.1-Multiple-Angles-LoRA
- Qwen-Image-Edit-2511 Multiple-Angles LoRA（`<sks>`，96 机位）https://huggingface.co/fal/Qwen-Image-Edit-2511-Multiple-Angles-LoRA
- ★ Animagine XL 4.0 模型卡（Euler a / 28 步 / CFG 5 / 832×1216 / 1536 退化 / 不支持文字 / tag 语法）https://huggingface.co/cagliostrolab/animagine-xl-4.0
- ★ Kontext Character Turnaround Sheet LoRA（推荐提示词；明说真人照片差、不擅长表情表）https://huggingface.co/reverentelusarca/kontext-turnaround-sheet-lora-v1
- ★ h94/IP-Adapter SDXL 权重清单（plus 848 MB、plus-face 848 MB）https://huggingface.co/h94/IP-Adapter/tree/main/sdxl_models
- ★ PuLID（NeurIPS 2024，Apache-2.0，最后 push 2025-07-31）https://github.com/ToTheBeginning/PuLID
- ★ ComfyUI IPAdapter_plus（最后 push 2025-04-14，未归档，226 open issues）https://github.com/cubiq/ComfyUI_IPAdapter_plus
- ★ ComfyUI-layerdiffuse https://github.com/huchenlei/ComfyUI-layerdiffuse
- ★ ComfyUI-Impact-Pack（Regional Sampler / Detailer）https://github.com/ltdrdata/ComfyUI-Impact-Pack
- ★ ComfyUI 官方 Character Turnaround Sheet Generator（Gemini API）https://comfy.org/workflows/ad315e41d8c2-ad315e41d8c2/
- ★ ComfyUI 官方 QwenMultiangleCameraNode 工作流 https://comfy.org/workflows/templates-qwen_multiangle.app-4724032fa666/
- ★ ComfyUI 官方全景图像生成工作流 https://comfy.org/zh/workflows/33c621f7803f-33c621f7803f/
- ★ Seedream 4.0 官方介绍（统一生成+编辑、参考图一致性、4K）https://seed.bytedance.com/zh/seedream4_0
- ★ Gemini 图像 API 文档 / AI Studio 模型页 https://ai.google.dev/gemini-api/docs/image-generation · https://aistudio.google.com/models/nano-banana

**社区 / 教程**
- Flux Kontext 12 GB GGUF 实测（4070Ti 上 Q6/Q8 约 9–10 分钟/张）https://www.reddit.com/r/StableDiffusion/comments/1lm2k1o/flux_kontext_what_ggufs_to_use_with_12_gbs_of_vram/
- Qwen-Image-Edit-2509 + depth map 讨论 https://www.reddit.com/r/StableDiffusion/comments/1om3z0r/qwenimageedit2509_and_depth_map/
- Kontext 三视图 LoRA 讨论 https://www.reddit.com/r/StableDiffusion/comments/1ltsm47/flux_kontext_character_turnaround_sheet_lora/
- OpenPose 三视图骨架模板 https://www.reddit.com/r/StableDiffusion/comments/1180fls/sharing_my_openpose_template_for_character/
- CharTurner（SD1.5/2.1）https://civitai.com/models/3036/charturner-character-turnaround-helper-for-15-and-21
- Pony CharTurn / Multi-View LoRA https://civitai.com/models/692970/pony-charturn-multi-view-turnaround-model-sheet-character-design
- 立绘 + 表情差分（AI 编辑法，含"横脸比正面糙"的坦白）https://note.com/tamayura_404/n/ncc23e7472a55
- B站：ComfyUI 多视图工作流 BV1fyygBMEP4 · 漫剧三视图 BV1Vsap6TESf · 一键多表情差分 BV1WP9MBcEPp · PixelSmile 参数 BV16SXCBpEG3 · Qwen-Edit-2511 表情编辑 BV1mjDFBuEb6 · Qwen2.1 局部重绘接缝 BV1uanE6dESy · Qwen2.1 Turbo 8 步 BV1CLnV6wE4y
- 全景/等距柱状裁切节点 https://github.com/ProGamerGov/ComfyUI_pytorch360convert （仅搜索结果，未打开验证）
> 检索缺口（如实记录）：OpenArt、知乎三视图拆解（`zhuanlan.zhihu.com/p/2022311445476459151`）、小红书相关笔记都检索到了标题，但正文抓取失败（知乎 403、小红书需登录、Reddit 部分线程人机验证），**未引用其中任何结论**；`comfy.org` 的三视图/多角度模板我逐字读过，但它们的正文是工作流说明，没有给参数值。
