# cards/_template/assets/

这是**一张卡自己的**素材目录。素材不再放 `app/static/assets/`（那是所有卡共用的一棵树，
第二张卡必然撞名），跟着卡走：删卡 = 删目录，拷卡 = 拷目录。

## 目录契约

| 目录       | 放什么                                             | 被谁用 |
| ---------- | -------------------------------------------------- | ------ |
| `char/`    | 角色姿态/表情 webp(864×1536, RGBA) + `manifest.json` | `app.js` 贴片层、`SEQ_BASE` 清单 |
| `plate/`   | 场景板 webp（无人物的背景）                         | `showPlate()` / `style.css` 兜底 |
| `ending/`  | 结局静帧 webp                                      | `handleEnding()` |
| `fx/`      | 特效贴图 webp（卡里 `fx.instances.*.asset` 点名）    | 特效原语 |
| `insert/`  | 插入画面 webp（特写/道具）                          | `ASSETS.insert` |
| `audio/`   | 音频素材（示例语音 wav 等，**不是 TTS 输出**）        | 调试按钮、环境音 |

TTS 输出在 `media/tts/`、`media/tts_neural/`（运行时缓存，不属于卡）；
音色参考 wav 目前在仓库根 `assets/voice/`（见 docs/VOICE_NEURAL.md §9）。

## 最小可用清单

```
assets/char/manifest.json      # 清单：poses{name:{file,bbox}}；没有它前端回退到内置路径
assets/char/neutral.webp
assets/plate/main.webp
assets/ending/ending.webp
```

`manifest.json` 里的路径写**相对 `char/` 的**（`neutral.webp`）就行，
服务端下发时会改写成绝对 URL 并补 `card_id`/`base`（见 `cards.char_manifest()`）。

## 命名约定

- 文件名 = 卡里 `assets.*` 的键（`char/neutral.webp` ↔ `assets.char.neutral`）。
- 换素材 = 换文件 + 给 `assets.yml`/`card.yaml` 的 URL 加版本（前端 `ASSET_V`）。
- 历史坏帧不要删，挪进 `_parked/<批次>/`（下划线目录不会被扫描）。
