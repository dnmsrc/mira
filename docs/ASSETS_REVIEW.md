# 素材审核清单（角色 / 场景）

生成时间：2026-10-19 起 · 全部为本地 ComfyUI 产出，未用任何外部图库。
底模换过两次：第一轮 Qwen-Image 2.1 → 第二/三轮 animagine-xl-4.0（SDXL，动漫向）。
**当前在用的是第三轮的 13 张贴片 + 新底板，见第 10 节**；第 1 节的审核图是第一轮时期的，
保留作历史记录，不再作为当前交付物的证据。

本文件只做一件事：告诉你**哪张图在哪里、是不是在用的那张、怎么看**。

---

## 1. 先看这 5 张审核图（第一轮 Qwen-Image 时期的证据，历史留存）

> 当前交付物请直接看 [review/states/pose_sheet.png](review/states/pose_sheet.png)
> （13 形态在真实页面 390×844@2x 上的截图，每次换素材都重刷）和第 10 节。

| 图 | 看什么 |
| --- | --- |
| [01_char_13_states.png](review/01_char_13_states.png) | 13 个角色形态叠在真实场景板上的样子，**等于页面渲染结果** |
| [02_raw_generated.png](review/02_raw_generated.png) | `assets/raw/qwen/` 里 18 张**未经处理**的原图 |
| [03_scene_chain.png](review/03_scene_chain.png) | 从「角色参考 → 场景空镜 → 合成帧 → 空场景板 → 运行时背景」的完整链路 |
| [04_raw_vs_cut.png](review/04_raw_vs_cut.png) | 原图 vs 抠好的透明贴片（棋盘格 = 透明） |
| [05_alignment.png](review/05_alignment.png) | 把抠图贴回场景板后与原始帧逐像素比对：平均差异 1.2–1.6/255（差异集中在抠像边缘的 1–2px 羽化），**位置与缩放一致，没有重绘背景** |

复跑：`python tools\qa\make_review.py`（只读素材，重新生成上面 5 张图）

---

## 2. 页面实际加载的（这些才是交付物）

| 类别 | 路径 | 数量 | 规格 |
| --- | --- | --- | --- |
| 场景背景板 | `app/static/assets/plate/cafe_night.webp` | 1 | 864×1536，155 KB（第三轮重出） |
| 角色贴片（透明） | `app/static/assets/char/*.webp` | 13 | 864×1536 RGBA，每张 85–94 KB（第三轮重出） |
| ~~事件视频~~ | ~~`app/static/assets/video/*.mp4`~~ | 0（已删） | — |
| 插图 | `app/static/assets/insert/polaroid.jpg` | 1 | 1600×926 |
| 清单 | `app/static/assets/manifest.json` | 1 | 尺寸/体积索引 |

13 个形态：`neutral`(待机) `listen`(倾听) `thinking`(思考) `smile` `worried` `surprised`
`look_window` `sip_coffee` `lean_in` `show_photo` `stand` `look_door` `check_phone`。

- **表情 6 个**：neutral / smile / worried / surprised / thinking / listen
- **动作 7 个**：look_window / sip_coffee / lean_in / show_photo / stand / look_door / check_phone
  （后两个 2026-10-20 补齐，见第 9 节）

---

## 3. 原始素材（想改图时从这里改）

| 类别 | 路径 | 数量 | 说明 |
| --- | --- | --- | --- |
| AI 原图 | `assets/raw/qwen/` | 18 | 角色参考 1 + 场景空镜 1 + 11 形态 + 5 张试验/mid |
| 抠好的透明 PNG | `assets/raw/sprites/*.png` | 14 | 11 形态 + 场景板 + 合成帧 + union_mask |
| 视频输入帧（历史残留） | `assets/raw/i2v/` | 6 | `plate_bg` 背景帧 + 3 个事件首帧 + idle 帧；视频已删，只剩输入帧 |

链路（与图 3 对应）：

```
char_ref.png（角色参考图，黑底）
  ──参考图编辑──▶ pose_*.png（11 张：角色 + 同一场景）
                     │
                     ├─ 抠像 ▶ sprites/*.png ─压缩▶ app/static/assets/char/*.webp
                     └─ 去人物 ▶ plate.png ─压缩▶ app/static/assets/plate/cafe_night.webp
cafe_hero.png（场景空镜，文生图，提供场景细节与灯光）
```

关键点：**场景板是从角色帧里「减掉人」得到的**，所以角色与背景同一机位、同一光照，
叠回去像素级对齐（图 5 已验证）。换表情不会重画背景——因为你上次指出的正是这一点。

---

## 4. 已废弃 / 不要当成交付物

| 路径 | 是什么 |
| --- | --- |
| `assets/raw/state_*.png`（5 张） | 上一版 SDXL 立绘，已被 11 形态取代，**无任何代码引用** |
| `assets/raw/clip_*.png`、`window_close.png` | 上一版序列帧素材，已废弃 |
| `assets/raw/qwen/{hero.png,hero3.png,cafe_hero_anime.png,recomposed.png}` | 定型过程中的试验图，保留供对比 |
| `assets/raw/{qwen,sprites}/recomposed.png` | 试验用合成图 |
| `production/reports/{probe,layers,shots,clips}/` | 各阶段截图与验证帧，不是运行时素材 |

需要归档或删除其中任何一组，说一句我就动（我不会自己删审核素材）。

---

## 5. 我自己看到的问题（请重点审这几处）

1. **动作幅度偏小**：`listen / look_window / lean_in` 三张在缩略图上几乎看不出差别，
   只有 `sip_coffee / show_photo / stand` 辨识度高。若你觉得"看不出在演"，这是首先要返工的。
2. **`char_ref.png` 是黑底**：只能当参考图输入，不能直接贴到页面上（页面用的是抠图层）。
3. **抠像边缘**：头发/雨衣边缘 1–2 px 软边，在深色背景上略微可见；`04_raw_vs_cut.png` 可放大看。
4. **场景只有一块板**：题目要求的「≥1 次背景/镜头/环境变化」是靠整舞台 transform（推镜）
   + 灯光/暗场叠加实现的，没有第二张场景图。要真正的"换环境"需要再生成场景。
5. ~~**事件视频不跟随表情**~~（已随视频删除而消失）：thunder / photo / last_train 三条片是用固定首帧生成的，
   角色正在生气时也会播同一段。
6. **比例与风格**：日系二次元、成年人比例、琥珀雨衣 + 银星发夹，与题目描述一致；
   但这是「我判断像 Mira」，是否符合你/面试方的口味需要你定。

---

## 6. 你要审的具体决策（勾选即可，我照改）

- [ ] 角色画风：二次元日系（当前）/ 更写实 / 更扁平
- [ ] 场景：当前咖啡馆空镜 / 换构图 / 追加第二块场景板
- [ ] 11 形态里要砍掉或重画哪些（重点 1）
- [x] 视频是否重做 → 决定不重做，2026-10-20 全删，演出改程序合成（重点 5）
- [ ] 废弃素材是否清理（第 4 节）

---

## 7. 返工记录（2026-10-19 第二轮）：隔桌中近景 + 雨后 + 打烊细节

第一版素材被用户指出四类硬伤：机位在吧台制作区、画成正在下暴雨、看不出即将打烊、
道具（星形发夹/相机）缺失且神态不对。第二轮按「机位-道具分配 + 分层生成」重做，
新素材在 `assets/turn/`，集成脚本 `tools/gen/integrate_turn.py`，详情见
[research/turnaround_workflow.md](research/turnaround_workflow.md) 与 [review/turn/REPORT.md](review/turn/REPORT.md)。

| 项 | 第一版 | 第二版（当前上线） |
| --- | --- | --- |
| 机位 | 吧台制作区内侧 | **客人视角越过桌面**，隔着桌子看她 |
| 天气 | 正在下暴雨（雨丝/雨幕） | **雨后**：湿地面反光、玻璃水珠，无雨丝 |
| 打烊细节 | 无 | 未收餐具、水槽待洗杯、翻到桌上的椅子、灭掉的灯牌、只亮两盏吊灯 |
| 角色大小 | 半身占满画面 | 中近景，上半身约 55% 画面高（表情可辨） |
| 道具 | 发夹不明显、无相机 | 琥珀雨衣 + 星形发夹 + 胶片相机（含背带）均可见 |
| 统一调色 | 无 | 底板与 11 张贴片过同一套夜景调色（`--grade 0.6`），避免"两张皮" |
| 行数 | 11 形态 | 11 形态（同人同机位），另有宽景空镜 `cafe_night_wide.png` 备用 |

已知剩余问题（诚实记录）：

1. **事件视频（已解决）**：原来 4 段 MP4（thunder / photo_reveal / last_train / idle_loop）和底板
   不是同一场戏。2026-10-20 按要求把它们**全部删除**，演出改为程序合成（闪电补光 / 车灯横扫 /
   照片静帧推近），不再有「素材和场景不同场」的问题。
2. 发夹是"星芒"形状，不是清晰的五角金属星；可用 2D 贴片叠加修正。
3. 11 张里有 7 张是 16 步（其余 25 步），锐度略有差异。
4. 中近景底板是「从合成帧去掉人物」重建的，她坐过的椅子区域有补画痕迹，被贴片完全覆盖；
   **需要无角色空镜时请用 `cafe_night_wide.png`**。
5. 抠像边缘仍有 1px 级软边，前景杯子的鬼影已用「开运算 + 最大连通域」清掉（`clean_alpha`）。

---

## 8. 2026-10-20 补充：三个「代码以为有、素材其实没有」的坑

这一节是**实测像素**得出的，不是感觉。用 `numpy` 把每张贴片和 `neutral` 对齐后算
不透明区域的 RGB 均值差 / 变化超过 12 的像素占比：

| 形态 | 平均色差 | 变化像素占比 | 结论 |
| --- | --- | --- | --- |
| `thinking` | 31.5 | 53.6% | 够用（手托腮，一眼看得出在想事） |
| `stand` | 70.6 | 91.7% | 够用 |
| `sip_coffee` | 57.7 | 79.1% | 够用 |
| `lean_in` | 55.7 | 82.3% | 够用 |
| `show_photo` | 32.7 | 45.0% | 够用 |
| `surprised` | 22.1 | 37.1% | 偏弱但仍可辨 |
| `worried` | 17.4 | 34.1% | 偏弱但仍可辨 |
| `look_window` | 17.5 | 30.5% | 偏弱 |
| `listen` | **9.8** | **14.0%** | 太弱：缩略图上和 neutral 几乎分不出 |
| `smile` | **3.8** | **3.1%** | 等于没有：笑了和没笑一样 |

两个缺口（卡包声明了但 `app/static/assets/char/` 里从来没有这两张图）：

1. **`look_door`「抬眼看向门口」** —— 第 1 拍（她抬头看推门进来的客人）的既定动作，
   模型每次都会选它，然后**什么都没发生**。目前客户端把它映射到 `look_window`。
2. **`check_phone`「低头看手机」** —— 映射到 `show_photo`（手在胸前、低头，最接近）。

要真正修好，用 `tools/gen/gen_states.py` 在同一张 hero 图上补两张（单张 31–40s），
顺手把 `smile` / `listen` 用更强的肌肉级描述重出（现在这两张的表达力是这一轮
"神态不到位"里唯一没法用代码补的部分）。补齐后删掉 `app.js` 里的 `POSE_ALIAS`。

> ✅ 这一节列的三个坑已于 2026-10-20 全部补掉，见下面第 9 节。

---

## 9. 2026-10-20 收尾：补两张缺失姿态 + 重出两张"等于没有"的表情

### 9.1 做了什么，用量化数字对账

| 形态 | 之前 | 现在 | 判定依据 |
| --- | --- | --- | --- |
| `look_door` 抬眼看向门口 | **没有素材**，客户端映射到 `look_window` | 真动作：头向她左侧转约 40°、抬下巴、眼睛追向门口的右上方，第 1 拍的既定动作第一次真的发生 | 贴片内与 neutral 差异 **39.8%**（旧版若映射则约 30%，且方向错误——她看向窗外而不是门口） |
| `check_phone` 低头看手机 | **没有素材**，映射到 `thinking` | 真动作：低头 25°、视线向下，手里**真的有一部手机**（黑色机身，在相机背带旁边） | 贴片内差异 **49.0%**，与 `show_photo`（41.7%）同档 |
| `smile` 微笑 | 全贴片 3.1% 像素变化，且均匀摊在整张脸上（低对比的明暗差），**看不出在笑** | 明确的笑：嘴角上扬、露上齿、下眼睑上推、眼尾有细纹 | 贴片 7.5%；**嘴部 135×65 框内变化 25.8%**（旧版 10.0%）、眼周 42.4%（旧版 29.3%），A/B 见 [states/faces_3.png](review/states/faces_3.png) |
| `listen` 倾听 | 全贴片 14.0%，但改动是"全身轻微重绘"，脸上没有可读的变化 | 睁大眼、抬眉、微张嘴（"正接着话"的状态） | 贴片 7.6%；眼周 47.8%，A/B 见 [states/faces_4_check.png](review/states/faces_4_check.png)（与 `surprised` 可区分：后者头后仰+嘴型不同） |

> 口径提醒：**整张贴片的像素变化率不等于"看得出来"**。旧 `smile` 有 3.1% 却看不出，
> 新 `smile` 只有 7.5% 但一眼能看出——差别在于改动是否集中在嘴/眼。
> 所以上表同时给了"嘴部/眼周框内"的数字，验收时请以 A/B 图为准。

### 9.2 为什么笑脸走"脸补丁"，姿态走"整帧重绘"

| 路 | 用在 | 做法 | 为什么 |
| --- | --- | --- | --- |
| FULL 整帧 edit | `look_door` `check_phone` | 从 `raw/hero_close.png`（和现有 11 张 state 同一母帧）整帧重绘 | 头转/低头是**几何变化**，必须整张重绘；用同一母帧才和旧贴片同一套光照 |
| FACE 脸补丁 | `smile` `listen` | 以 `raw/state_neutral.png` 为底 → 裁 448×448 脸框 → 放大到 **896 渲染** → 降回 448 → 羽化椭圆贴回 → 其余像素逐字节不变 | 整帧 864×1536 里她的脸只有约 200px 宽（占画面 2.3%），模型画不出可读的表情；放大渲染后脸的有效分辨率翻倍。同时**身体/头发/抠像边缘保持中性帧原像素**，换表情不会带出整张图的重绘抖动 |

实现：`tools/gen/gen_states2.py`（`face:` / `full:` 两路任务表，`--steps/--seed` 可调）。

### 9.3 顺手修掉的一个流程坑（重要）

`tools/gen/integrate_turn.py` **不是**交付那条链路，直接跑它会把整场夜景再压暗一遍：
它给底板也套 `night_grade(1.0)`，而实际交付的底板是 `integrate_plate.py` 发布的**未调色**版本
（实测：交付底板 vs `assets/turn/plate/cafe_night.png` 残差只有 **2.25/255**，就是 webp 有损误差）。

我按"已交付的 webp + 原帧"反推并验证出真正的发布链，固化成了 `tools/gen/publish_states.py`：

```
raw/state_<name>.png
  → cut_turn2.cut_frame          rembg(isnet-anime) 语义抠图 + 与底板求差砍掉静态道具
  → y>1285 & x<545 的 alpha=0     clean_sprites2 的空间规则（左下残留道具）
  → y>1140 & x>540 的 alpha×1.9   fix_mug_alpha（她手里那只杯子否则能看到桌面透出来）
  → night_grade(0.6)              贴片统一压暗偏冷（底板**不**经这一步）
  → WEBP q92
```

验证：用中性帧重跑这条链，与已交付的 `neutral.webp` 在两者都不透明的区域比，
`grade=0.6` 残差 **4.02/255**（0.5→4.29、0.7→4.55，其余档位单调变差；不做 grade 是 10.64）。
对现有 4 张贴片复核残差 3.95–4.44，`listen` 10.68（因为它的帧本来就被我换掉了）。

### 9.4 交付物清单（新增/更新）

| 路径 | 说明 |
| --- | --- |
| `app/static/assets/char/look_door.webp` | **新增**，95 KB，864×1536 RGBA |
| `app/static/assets/char/check_phone.webp` | **新增**，94 KB |
| `app/static/assets/char/smile.webp` | 重出，95 KB |
| `app/static/assets/char/listen.webp` | 重出，95 KB |
| `cards/mira_cafe/card.yaml` | `pose.look_door` / `pose.check_phone` 指向自己的贴片（不再借用） |
| `app/static/app.js` | 删掉 `POSE_ALIAS`（映射表已无必要） |
| `docs/review/states/pose_sheet.png` | 13 形态在**真实页面** 390×844@2x 上的截图联络表 |
| `docs/review/states/ab_faces.png` `faces_3.png` `faces_4_check.png` | 新旧表情 A/B（放大看嘴/眼） |
| `docs/review/states/turn_strip.png` | 一轮真实对话 18 帧，标出每帧的 emotion/pose |
| `tools/gen/gen_states2.py` | 生成（FACE / FULL 两路） |
| `tools/gen/publish_states.py` | 发布（复刻交付链路，含 `--check` 残差自检） |
| `tools/qa/pose_sheet.py` / `tools/qa/turn_frames.py` | 验收截图脚本 |

### 9.5 仍然存在的边界（诚实记录）

1. **`smile` / `listen` 的身体没有跟着变**：脸补丁之外逐字节等于中性帧。
   好处是切换无抖动；代价是"笑"只有脸在笑，肩膀没松、头没歪。要连身体一起，
   得回到整帧重绘并接受 ±5/255 的整身重绘抖动。
2. **`look_door` 的方向是靠提示词指定的**（她左侧 = 画面右侧的门口），
   而机位/门的位置来自底板，如果以后换底板，这条指令要跟着改。
3. **`check_phone` 的手机是模型画出来的**，不是贴上去的道具；形状比真机潦草（远看没问题）。
4. **11 张旧贴片仍是各自整帧重绘**，它们彼此之间本来就有 ±5/255 差异
   （这是旧素材"切形态时轻微抖一下"的根因），本轮没有重做——只保证新加的 4 张不增加这个抖动。

---

## 10. 2026-10-20 第三轮：13 张贴片同批次重出（身份 0 漂移，代价是动作没做成）

第 9 节那批素材的问题不是丑，是**不一致**：11 张各自整帧重绘，彼此本来就有 ±5/255 差异，
第 9.5 条自己也承认了。这一轮换了做法：不追求"每张都最好"，先保证"每张都是同一个人"。

### 10.1 换掉了什么

| 项 | 第三轮 | 上一轮 |
| --- | --- | --- |
| 底模 | `animagine-xl-4.0`（动漫向 SDXL） | Qwen-Image 2.1 + 局部修补 |
| 批次 | 13 张同批次、同 seed(777)、同一基准帧 | 11 张各自重绘 + 4 张补丁 |
| denoise | 0.55–0.62（受控 img2img） | 混合 |
| 底板 | 重出：864×1536 RGB，155 KB | 288 KB |

`Illustrious-XL-v1.1` 做过 neutral / smile 的 d0.42 对照：身份同样稳，但雨衣颜色偏暗偏饱和，
和已批准的基准帧对不上，**弃用**。底模选型不是"哪个更新"，是"哪个跟基准帧颜色对得上"。

### 10.2 结果：13 张 0 张漂移

同一张脸、同一发型、同一枚星形发夹、同一件雨衣、同一台相机。换表情时不再有整身后重绘的抖动
（layers_test 实测见 10.4）。

### 10.3 代价：6 个动作只剩"表情 + 光影"

`look_window` / `look_door` / `sip_coffee` / `check_phone` / `show_photo` / `stand`
这 6 个**头没转、杯子/手机/照片没出现、站姿没变**。这是主动取舍，不是漏做：

- t2i `denoise=1.0` 能出"拿杯子/拿手机"，但**换成另一个人**；
- 草图 init + img2img `denoise=0.74` 道具出来了、脸也漂了——这一版最接近可用，按"身份第一"否掉；
- 再往上只有两条路：controlnet depth/openpose（本机没装预处理器），或拿 Qwen-Image-Edit 当**编辑器**
  （不是 img2img），用它保住身份只改动作。

**所以这一轮出现了"素材名 ≠ 素材内容"的落差，处理方式是：**
素材层不加近似映射（`POSE_ALIAS` 已删），卡包里那些动作的**枚举名保留不动**（服务端/客户端/评测都按枚举走，
改名字才是真 bug），落差记在这份文档里，可复现、可复查，不藏在代码注释里。

### 10.4 验到哪儿了（可复跑）

| 检查 | 结果 | 怎么跑 |
| --- | --- | --- |
| 换表情时角色之外动没动 | **0.673%**（阈值 1.5%） | `python tools\qa\layers_test.py` |
| 换动作时角色之外动没动 | **0.263%** | 同上 |
| 相机 transform | `matrix(1.2,…)`，plate/sprite 同属 stage | 同上 |
| 13 形态在真页面上的样子 | [review/states/pose_sheet.png](review/states/pose_sheet.png)（本轮已重刷） | `python tools\qa\pose_sheet.py` |

全程一次一个 GPU 任务、逐张 review，没有并发刷队列。

### 10.5 两个素材缺口（缺的不是 bug，是素材）

1. **底板里没有窗、也没有翻倒的椅子。** "23:47 雨后"只能靠冷蓝夜景 + 极淡的玻璃水痕读出来
   （`.rain`：3 条，opacity .22，故意做成"水痕"不是"雨幕"）。卡包里 `window_close` 这个"镜头"
   实际是 CSS 推近裁剪，不是真拍到窗——标签写的是"镜头 · 近景"，没有声称有窗。
2. **贴片底部是 alpha 渐隐。** 近桌在贴片层**下面**，所以挡不住她的躯干，凑近看是"淡出"而不是
   "被桌子挡住"。要修得把近桌单独切出来做一层前景（纯前端可做，尚未做）。

### 10.6 文件与备份

- 替换：`app/static/assets/char/*.webp`（13 张）、`app/static/assets/plate/cafe_night.webp`
- 旧文件备份：`D:\ai\mira_anime_work\backup_old\`（14 个）
- 生成脚本与中间产物：`D:\ai\mira_anime_work\`（`gen_char*.py` / `gen_plate*.py` / `lamps.py` /
  `postprocess.py` / `publish.py`），完整报告 `D:\ai\mira_anime_work\REPORT.md`
- 自查图：`app/static/assets/_preview/plate_composite.jpg`（底板 + 每形态合成）、
  `app/static/assets/_preview/contact.jpg`（13 贴片联络表）

---

## 11. 2026-10-20 第四轮：三个动作改成**真动作**（Qwen-Image-Edit，不是 img2img）

第 10 节留下的缺口是"6 个动作只有表情和光影"。这一轮按轻重缓急补了 3 个，
用的不是 SDXL img2img（已证明到 denoise 0.74 也不转头），而是**编辑器**：
`tools/gen/qwen_workflow.py` 里那条 `TextEncodeQwenImage21` + 参考图 reference latents 的链路。

### 11.1 结果（3/3）

| 动作 | 结果 | 新字节 | 说明 |
| --- | --- | --- | --- |
| `sip_coffee` | ✅ 双手举白瓷杯到嘴边 | 92 746 → **75 728** | 杯子真的出现在手里 |
| `show_photo` | ✅ 手里举一张拍立得 | 95 738 → **93 284** | 照片内容是雨夜街景，和剧情对得上 |
| `stand` | ✅ 离桌站起 | 90 598 → **91 800** | 上半身离桌、肩线抬高、相机在胸前 |

三帧都是 864×1536 RGBA，q90 / method 6 单次编码，和 `publish.py` 同参数；
旧文件备份在 `D:\ai\mira_anime_work\backup_old2\`；报告 `D:\ai\mira_anime_work\REPORT2.md`。

### 11.2 怎么判"还是同一个人"（不是目测）

每帧三张对照图（`D:\ai\mira_anime_work\out\edit\chk\<name>_{pair,diff,plate}.jpg`）：
neutral 与候选并排、RGB 差异图 + alpha 独有像素、叠回底板看构图。

| 帧 | alpha 重叠率 | 平均 RGB 差 |
| --- | --- | --- |
| sip_coffee | 0.796 | 22.1 / 255 |
| show_photo | 0.779 | 37.1 / 255 |
| stand | 0.825 | 37.9 / 255 |

脸型、刘海走向、发色、星形发夹位置、灰白衬衫、橄榄绿雨衣 + 肩上红花全部一致。

### 11.3 一个被数据推掉的猜测

第一版把雨衣画成了米色针织开衫。当时的假设是"参考图的白平衡把颜色带偏了"。
量了一下：已发布 `neutral.webp` 与参考图里雨衣色块 `[106.5,74.0,50.1]` vs `[106.2,73.8,50.1]`，
**全身差 0.08/255** —— 参考图没问题，是指令里没点名衣服。
改成点名 `olive green raincoat, grey-white collared shirt` 后一次就对。

**教训：漂色先量色差，再改提示词。** 否则会去换参考图，白花一轮抽卡。

### 11.4 抠像：两个模型各有短板，按帧选

| 模型 | sip_coffee | show_photo |
| --- | --- | --- |
| `u2net` | ✅ 完整 | ❌ 只出头 + 肩，雨衣/手/照片全丢 |
| `isnet-anime` | ❌ 会吃到画布底 | ✅ 完整 |

所以最后是**分帧选模型**：sip 用 u2net，show 用 isnet-anime。
另外两条试过但不通的：plate-diff 抠像（整片重绘的背景被并进来）、u2net 多尺度并集（三行 max 都是同一行，纹丝不动）。

### 11.5 一个会咬人的比例坑（后续补姿势必看）

`publish.py` 的缩放系数 `s` 是拿 **neutral 的 bbox** 算出来、再套给每一帧的。
sip / show 的身体跨度和 neutral 接近，所以没暴露；`stand` 的身子有 1301 px，
套上 `s=1.009` 会被放大到 1313 px，**超出 1536 画布底边——腰被裁掉、头反而比 neutral 大**
（第一版就是这样：alpha 底边全是 255，底部渐隐失效）。

改法：`s = min(s_neutral, 1080 / 本帧 bbox 高)`，stand 取 **0.830**，头比 neutral 小约 18%。
这不是妥协，是物理正确：她站起来、相机没动，头就应该更小。
**注意这个改法只写在这一轮的 `stand_final.py` 里，没有动 `publish.py`** ——
以后再补身体跨度大的姿势，这一条必须一起套，否则还会裁腰。

### 11.6 仍然存在的偏差（诚实记录）

1. **`sip_coffee` 里相机不见了**：双手举杯必然遮挡，u2net 也把相机判成了背景。
   观众不会去数道具，接受；代价是与 neutral 对比"少了那台相机"。
2. **`show_photo` 的视线是"抬眼偏下"**，不是明确的低头看照片。动作成立，眼神偏弱。
3. **道具连续性没修**：底板桌上本来有两只杯子，她手里又举一只。要修得给底板出无杯版本
   或做前景遮挡，本轮没做。
4. **还剩 4 个动作是"表情/光影变体"**：`look_window` / `look_door` / `check_phone` / `lean_in`。
   补法已经验证（同一条 edit 链路，一张约 60s + 抠像验收 40s），但**必须独占显卡**：
   跑素材和跑对话模型同时在 12GB 卡上，实测 ComfyUI 从 2 s/step 掉到 97.5 s/step、
   llama 从几十 tok/s 掉到 4.13 tok/s。取舍与启动顺序见 `docs/DEMO_CHECKLIST.md`。
