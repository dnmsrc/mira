# 素材审核清单（角色 / 场景）

生成时间：2026-10-19 · 全部为本地 Qwen-Image 2.1（ComfyUI 0.37）产出，未用任何外部图库。
本文件只做一件事：告诉你**哪张图在哪里、是不是在用的那张、怎么看**。

---

## 1. 先看这 5 张审核图

| 图 | 看什么 |
| --- | --- |
| [01_char_11_states.png](review/01_char_11_states.png) | 11 个角色形态叠在真实场景板上的样子，**等于页面渲染结果** |
| [02_raw_generated.png](review/02_raw_generated.png) | `assets/raw/qwen/` 里 18 张**未经处理**的原图 |
| [03_scene_chain.png](review/03_scene_chain.png) | 从「角色参考 → 场景空镜 → 合成帧 → 空场景板 → 运行时背景」的完整链路 |
| [04_raw_vs_cut.png](review/04_raw_vs_cut.png) | 原图 vs 抠好的透明贴片（棋盘格 = 透明） |
| [05_alignment.png](review/05_alignment.png) | 把抠图贴回场景板后与原始帧逐像素比对：平均差异 1.2–1.6/255（差异集中在抠像边缘的 1–2px 羽化），**位置与缩放一致，没有重绘背景** |

复跑：`python tools\qa\make_review.py`（只读素材，重新生成上面 5 张图）

---

## 2. 页面实际加载的（这些才是交付物）

| 类别 | 路径 | 数量 | 规格 |
| --- | --- | --- | --- |
| 场景背景板 | `app/static/assets/plate/cafe_night.webp` | 1 | 864×1536，288 KB |
| 角色贴片（透明） | `app/static/assets/char/*.webp` | 11 | 864×1536，每张 43–49 KB |
| 事件视频 | `app/static/assets/video/{thunder,photo_reveal,last_train,idle_loop}.mp4` | 4 | 544×960 |
| 插图 | `app/static/assets/insert/polaroid.jpg` | 1 | 1600×926 |
| 清单 | `app/static/assets/manifest.json` | 1 | 尺寸/体积索引 |

11 个形态：`neutral`(待机) `listen`(倾听) `thinking`(思考) `smile` `worried` `surprised`
`look_window` `sip_coffee` `lean_in` `show_photo` `stand`。

- **表情 6 个**：neutral / smile / worried / surprised / thinking / listen
- **动作 5 个**：look_window / sip_coffee / lean_in / show_photo / stand

---

## 3. 原始素材（想改图时从这里改）

| 类别 | 路径 | 数量 | 说明 |
| --- | --- | --- | --- |
| AI 原图 | `assets/raw/qwen/` | 18 | 角色参考 1 + 场景空镜 1 + 11 形态 + 5 张试验/mid |
| 抠好的透明 PNG | `assets/raw/sprites/*.png` | 14 | 11 形态 + 场景板 + 合成帧 + union_mask |
| 视频输入帧 | `assets/raw/i2v/` | 6 | `plate_bg` 背景帧 + 3 个事件首帧 + idle 帧 |

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
5. **事件视频不跟随表情**：thunder / photo / last_train 三条片是用固定首帧生成的，
   角色正在生气时也会播同一段。
6. **比例与风格**：日系二次元、成年人比例、琥珀雨衣 + 银星发夹，与题目描述一致；
   但这是「我判断像 Mira」，是否符合你/面试方的口味需要你定。

---

## 6. 你要审的具体决策（勾选即可，我照改）

- [ ] 角色画风：二次元日系（当前）/ 更写实 / 更扁平
- [ ] 场景：当前咖啡馆空镜 / 换构图 / 追加第二块场景板
- [ ] 11 形态里要砍掉或重画哪些（重点 1）
- [ ] 视频是否重做（重点 5）
- [ ] 废弃素材是否清理（第 4 节）
