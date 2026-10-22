/* Mira · 实时互动场景 前端状态机
 *
 * 三条不变量：
 * 1) 每个 turn 有唯一递增 seq；任何事件的 seq 与当前 seq 不一致 → 直接丢弃。
 *    服务端在 epoch 失效后也会停止下发，因此「迟到的网络响应」无法覆盖新状态。
 * 2) 音频按句排队（queue），打断 = 停当前 + 清空队列 + 隐藏一切未播放的媒体事件。
 * 3) 视觉表现只由 directive 驱动；directive 在回复开头就能到达（首行 JSON 协议），
 *    所以表情/镜头和第一句语音基本同时发生，不再等整段说完。
 */

const $ = (id) => document.getElementById(id);

// 结局「再看一遍」：整页重来（会话状态、故事节点、时钟全部回到 20:30）
document.addEventListener("DOMContentLoaded", () => {
  const b = document.getElementById("endingRestart");
  if (b) b.addEventListener("click", () => location.reload());
});

/* 资源模型：场景板（plate，无人物）和角色贴片（sprite，带 alpha）是两套独立素材。
   换表情只切 char 层、背景像素不动；换镜头只对 .stage 做 transform，两层一起缩放。 */

/* 素材缓存版本：**改任何图片就 +1**。
   app.js 从 v11 起就用 `?v=14` 防旧 JS，但图片一直没有版本号。后果是实测过的：
   17:53 把 phone_call.webp 从「整身版」换成「半身版」后，已经开着的页面仍然显示旧图 ——
   画面里她的脚悬在桌面上方，看着就是穿帮。素材换过之后，光换文件不够，URL 也得换。 */
const ASSET_V = "17";   // 17：neutral.webp 换成无损版（96KB lossy → 406KB lossless，脸/发丝不再糊）

/* ---------------------------------------------------------- 素材基址（v17）
   素材不再塞在 /static/assets（那是**所有卡共用**的一棵树，第二张卡必然撞名），
   而是跟着卡走：一张卡一个前缀 /card-assets/<卡id>/，服务端启动时按发现的卡逐张挂载。
   GET /api/card 会下发 assets_base / assets_manifest，并且 assets 里已经是**绝对 URL**
   —— 所以前端不拼任何目录名，下面的 ASSET_REL 只是「/api/card 还没回来」时的兜底形状。
   加一张卡：改卡、放图；这个文件一个字都不用动。见 docs/CARD_ASSETS_LAYOUT_V17.md。 */
let ASSET_BASE = "/static/assets/";                            // 兜底 = v16 旧布局
let ASSET_MANIFEST = ASSET_BASE + "char/manifest.json";

const assetUrl = (rel) =>
  `${ASSET_BASE}${String(rel).replace(/^\.?\//, "")}?v=${ASSET_V}`;

/* 素材清单的**相对形状**（唯一写死目录的地方是上面 ASSET_BASE 的默认值）：
   setAssetBase() 换基址时按它整棵重算，所以拿不到卡包快照时画面也不会白。 */
const ASSET_REL = {
  char: {
    neutral: "char/neutral.webp",
    smile: "char/smile.webp",
    worried: "char/worried.webp",
    surprised: "char/surprised.webp",
    thinking: "char/thinking.webp",
    listen: "char/listen.webp",
  },
  pose: {
    look_window: "char/look_window.webp",
    sip_coffee: "char/sip_coffee.webp",
    lean_in: "char/lean_in.webp",
    phone_call: "char/phone_call.webp",
    stand: "char/stand.webp",
  },
  plate: { cafe_night: "plate/cafe_night.webp" },
  /* v14：照片道具整组删除（新故事没有照片）。结局图与粒子点在这里声明。 */
  ending: { friend: "ending/friend.webp",
            stranger: "ending/stranger.webp" },
  fx: { softdot: "fx/softdot.webp" },
  /* 动作的**多帧序列**（动作名 → 帧 URL 数组）。由 loadSeqManifest() 从卡里的
     char/manifest.json 填进来（v17 由服务端把帧路径绝对化后再下发），所以换素材不用改这份代码。
     为什么要多帧：用户实测「卡顿与闪速」——一张静止姿势 + 380ms 叠化，
     动作看起来是"跳"过去的；连续 4–8 帧硬切才有动作的方向感。 */
  seq: {},
  /* 2026-10-20：`video` 这一组已删除（idle 循环 + 三段 AniSora 片段都不用了）。
     演出改成静帧 + 程序合成，见 triggerEvent()。 */
};

/* 按当前基址重算整棵 ASSETS。只改声明，不改任何调用点：
   paint()/showSprite()/preload() 拿到的就已经是带版本号的绝对 URL。 */
const ASSETS = {};
function stampAssets() {
  Object.keys(ASSET_REL).forEach((group) => {
    const g = ASSET_REL[group];
    if (Array.isArray(g)) { ASSETS[group] = g.map(assetUrl); return; }
    const out = {};
    Object.keys(g || {}).forEach((k) => {
      const v = g[k];
      if (typeof v === "string") out[k] = assetUrl(v);
      else if (Array.isArray(v)) out[k] = v.map(assetUrl);
      else if (v && typeof v === "object") {
        out[k] = {};
        Object.keys(v).forEach((k2) => {
          out[k][k2] = typeof v[k2] === "string" ? assetUrl(v[k2]) : v[k2];
        });
      }
    });
    ASSETS[group] = out;
  });
}

/* 换卡/换布局：基址一变，整棵素材（含清单地址）跟着变。 */
function setAssetBase(base) {
  if (!base || typeof base !== "string") return false;
  ASSET_BASE = base.endsWith("/") ? base : `${base}/`;
  ASSET_MANIFEST = `${ASSET_BASE}char/manifest.json`;
  stampAssets();
  return true;
}
stampAssets();

const LABEL = {
  emotion: { neutral: "平静", smile: "微笑", worried: "担心", surprised: "惊讶", thinking: "思索", listen: "倾听" },
  action: { idle: "待机", look_window: "望向窗外", sip_coffee: "抿了一口咖啡", lean_in: "靠近了些",
            phone_call: "接起电话", stand: "站起身" },
  scene: { cafe_night: "常驻机位 · 吧台", window_close: "镜头 · 近景", lights_dim: "环境 · 灯光转暗" },
  effect: { rain: "雨声", thunder: "雷声", none: "安静" },
  event: { none: "", thunder: "闪电演出", phone_call: "打电话演出", still: "静帧演出", ending: "结局" },
};

const S = {
  sid: null,
  // 前端只暴露两档：本地 Qwen3（llama）与云端 DeepSeek（cloud）。
  // 服务端 LLM_MODE 默认仍是 auto（strata → llama → mock 依次回落），那是服务端的
  // 降级策略，不是给用户选的档位；bootProvider() 会把「本地档」映射成 llama，
  // 并在观测面板里写清服务端实际落在哪个 provider 上。
  mode: "llama",
  cloudConfigured: false,  // /api/health 的 cloud.configured；选 cloud 但没 key 要提示回落
  provider: "",            // 这一轮真正产出台词的来源（done 事件里的 provider）
  seq: 0,
  activeSeq: 0,
  state: "idle",
  emotion: "neutral",
  face: "",          // 画面上当前是哪个姿势（表情名或动作名），poseTo 的起点
  scene: "cafe_night",
  plateFront: "A",
  spriteFront: "A",
  queue: [],
  playing: null,
  audioDone: true,
  abort: null,
  lastFirstAudio: null,
  lastTotal: null,
  errors: 0,
  turns: 0,
  beat: 0,
  eventFail: false,   // 注入故障：演出素材不可用（原来的 videoFail）
  ttsFallback: false,
  mediaEventActive: false,
  card: null,
  stage: null,        // 表演通道表（卡包 chips… 见 chanPick）
  gapTimer: 0,
  holdTimer: 0,
  holdPending: false,
  turnSentAt: 0,
  directive: null,
  _deferT: 0,
  _bargeT: 0,
  suppressSprite: false,
  recEnabled: new URLSearchParams(location.search).get("rec") === "1",
};

/* ------------------------------------------------------- 卡包快照（一份数据第三处）
   素材路径 / 标签 / 句间停顿都从 GET /api/card 来，客户端不硬编码。
   服务端换一张卡，前端跟着换素材；缺图时回退到情绪贴片，不白屏。 */
function applyCard(card) {
  if (!card || !card.assets) return;
  const a = card.assets;
  /* v17：素材前缀 / 清单地址都从快照来（服务端按卡挂载，前端不拼目录）。
     顺序必须是「先按基址重算整棵，再让快照点名的组覆盖」——反过来会把服务端给的
     绝对 URL 冲掉。快照里的 URL 已经能用，这里只补一个缓存版本号。 */
  if (card.assets_base) setAssetBase(card.assets_base);
  if (card.assets_manifest) ASSET_MANIFEST = card.assets_manifest;
  const wv = (v) => (typeof v === "string" && v.startsWith("/") && !v.includes("?")
    ? `${v}?v=${ASSET_V}` : v);
  const wm = (m) => {
    if (!m || typeof m !== "object") return m;
    const out = {};
    Object.keys(m).forEach((k) => { out[k] = wv(m[k]); });
    return out;
  };
  // 只有一套素材，卡包快照就是它；清单加载会再覆盖一次（含序列帧），互为兜底。
  if (a.char) ASSETS.char = wm(a.char);
  if (a.pose) ASSETS.pose = wm(a.pose);
  if (a.plate) ASSETS.plate = wm(a.plate);
  if (a.insert) ASSETS.insert = wm(a.insert);
  if (a.still) ASSETS.still = wm(a.still);
  if (a.ending) ASSETS.ending = wm(a.ending);
  if (card.labels) {
    ["emotion", "action", "scene", "effect", "event"].forEach((k) => {
      if (card.labels[k]) LABEL[k] = { ...LABEL[k], ...card.labels[k] };
    });
  }
  S.card = card;
  if (card.stage) S.stage = card.stage;   // 表演通道表（动作互斥规则），见 chanPick()
  if (card.title) document.title = `Mira · ${card.title}`;
  if (card.character && card.character.name) {
    const n = document.querySelector(".who .name");
    if (n) n.textContent = card.character.name;
  }
  if ((card.warnings || []).length) console.warn("[card] 卡包告警：", card.warnings);
}

/* ------------------------------------------------------------ 表演通道仲裁
 * 规则来自卡包：GET /api/card 的 stage 字段 ← cards/mira_cafe/card.yaml 的
 * channels / face_map / actions 三段。程序只做一件事：**判断此刻能不能做**。
 *   5 个通道：speech 口部/发声 · face 神态 · body 身体姿态 · hand 手部动作 · gaze 头部眼神
 *   同一通道同一时间只能有一个动作，不同通道可以叠加。
 *   **口部优先**：说话期间"要占嘴"的动作一律让路（按卡包 degrade_to 降级；
 *   降不了就不播——宁可不做，也不要一边说一边喝）。
 * 与 app/channels.py 是同一套算法，两边由 tools/qa/check_channels.py 对拍。
 */
const CHAN = { claims: Object.create(null) };

function stageSpec(action) {
  const st = S.stage;
  return (st && st.actions && st.actions[action]) || null;
}

/* 口部是不是被「说话」占着。看四件事：状态机 / 正在播的 mp3 / 已排期还没播完的 PCM /
   **本轮台词还没说完**（正在生成和思考时演一个占嘴动作，一定会撞上马上要说的话）。 */
function speechBusy() {
  const ctx = AMP.ctx;
  if (S.playing && !S.playing.paused && !S.playing.ended) return true;   // 整句 mp3 在播
  if (ctx && ctx.currentTime < STREAM.endTime - 0.05) return true;       // 流式 PCM 已排期未播完
  if (STREAM.items.length) return true;                                  // 还有句子没排到时间线
  if (S.state === "speaking") return true;                               // 状态机兜底
  /* 本轮台词还没播完（正在生成/思考）：现在演一个"要占嘴"的动作，一定会撞上马上
     要说的话 —— 按「说话通道优先」先挡住。 */
  if (!S.audioDone && (S.state === "thinking" || PERF.active)) return true;
  return false;
}

function chanBusy() {
  const now = Date.now();
  const busy = Object.create(null);
  for (const k in CHAN.claims) {
    const c = CHAN.claims[k];
    if (c && c.until > now) busy[k] = c.action;
    else delete CHAN.claims[k];
  }
  if (speechBusy()) busy.speech = "说话";
  return busy;
}

function chanConflicts(action, busy) {
  const spec = stageSpec(action);
  if (!spec) return [];
  const out = [];
  if (spec.speaks && busy.speech) out.push(`口部：${action} 要用到嘴，正在说话 → 说话优先`);
  /* 站姿是可自愈的：谁占着 body、而那个动作声明了 posture=standing，就按站姿判定 */
  const bodySpec = busy.body ? stageSpec(busy.body) : null;
  if (bodySpec && bodySpec.posture === "standing" && spec.seated !== false)
    out.push(`身体姿态：已经起身，坐着的 ${action} 不能再播`);
  for (const ch of spec.channels || []) {
    if (ch === "speech") continue;
    const occ = busy[ch];
    if (occ && occ !== action) {
      const label = ((S.stage.channels || {})[ch] || {}).label || ch;
      out.push(`通道${label}：被 ${occ} 占着`);
    }
  }
  return out;
}

function chanPick(action) {
  if (!action || action === "idle") return { action, reasons: [] };
  const spec = stageSpec(action);
  const st = S.stage;
  if (!spec || !st) return { action, reasons: [] };  // 旧协议 / 没拿到卡包快照：不接管
  const busy = chanBusy();
  const reasons = chanConflicts(action, busy);
  if (!reasons.length) return { action, reasons: [] };
  /* 抢断：优先级高的动作可以顶掉正在播的手势；**口部不可抢**（要 speech_priority 以上） */
  const sp = st.speech_priority || 100;
  const pri = spec.priority || 0;
  let can = true;
  for (const ch in busy) {
    if (ch === "speech") { if (pri < sp) can = false; continue; }
    const other = stageSpec(busy[ch]);
    if (pri <= ((other && other.priority) || 0)) can = false;
  }
  if (can && !(spec.speaks && busy.speech))
    return { action, reasons: ["抢断：" + reasons.join("；")] };
  let cur = action;
  for (let hop = 0; hop < 3; hop++) {
    const nxt = (stageSpec(cur) || {}).degrade_to;
    if (!nxt) break;
    if (!chanConflicts(nxt, busy).length)
      return { action: nxt, reasons: reasons.concat(["降级：" + cur + " → " + nxt]) };
    cur = nxt;
  }
  return { action: null, reasons: reasons.concat(["没有可用降级 → 不播"]) };
}

function chanClaim(action, holdMs) {
  const spec = stageSpec(action);
  if (!spec) return;
  const until = Date.now() + Math.max(400, holdMs);
  for (const ch of spec.channels || []) CHAN.claims[ch] = { action, until };
}

/* 句间停顿：TTS 是按句合成的，句与句之间要有长短不一的呼吸，
   否则听起来像流水线上的逐句朗读（这是「音频没有起伏」的主因之一）。 */
function gapAfter(text) {
  const g = (S.card && S.card.tts && S.card.tts.gaps_ms) || {};
  const t = String(text || "").trim();
  if (/……|\.\.\./.test(t)) return g.after_ellipsis || 420;
  if (/[？?]/.test(t)) return g.after_question || 300;
  if (/[，,、；;]/.test(t)) return g.after_comma || 80;
  return g.default || 190;
}

/* ------------------------------------------------------- 音频上下文（只建 ctx，不放任何底噪）
   2026-10-21（用户看完 v15 成片，第 2 条）：「去掉雨声的底噪，这个不要了」。
   这里原来用棕噪声合成一层"屋檐滴水 + 远处车流"的底噪，还带 ducking 和面板开关 ——
   全部删掉。现在这个 ctx 只服务两处的**事件音**（雷声低频、电话时的极短压低），
   不再有常驻循环音源，所以没有台词的时候就是真安静。 */
const AMB = { ctx: null, gain: null, on: false };

function ambienceInit() {
  if (AMB.ctx) return;
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    const ctx = new AC();
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    AMB.ctx = ctx;
  } catch (e) {
    /* 没有 Web Audio 就安静，不影响主流程 */
  }
}

/* 常驻底噪已下线：保留空实现，调用点（duck 之类）不用改也不会有声音。 */
function ambienceSet(on) {
  if (on) ambienceInit();
}

function ambDuck(on) {
  if (!AMB.ctx) return;
}

/* ------------------------------------------------------------------ 工具 */

function toast(msg, ms = 3200) {
  const el = $("toast");
  el.textContent = msg;
  el.hidden = false;
  requestAnimationFrame(() => el.classList.add("show"));
  clearTimeout(toast._t);
  toast._t = setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => (el.hidden = true), 260);
  }, ms);
}

function setCaption(text, typing = false) {
  const cap = $("caption");
  cap.textContent = text || "";
  const sub = $("subtitle");
  sub.classList.toggle("typing", !!typing);
  sub.classList.toggle("dim", !text);
}

/* 旁白条：她没说话时的系统叙述（与台词分开一条，见 index.html / style.css） */
function showNarration(text) {
  const bar = $("narration"), t = $("narrationText");
  if (!bar || !t) return;
  t.textContent = text || "";
  bar.classList.add("on");
  clearTimeout(showNarration._t);
  showNarration._t = setTimeout(() => bar.classList.remove("on"), 2600);
}

function clearNarration() {
  clearTimeout(showNarration._t);
  const bar = $("narration");
  if (bar) bar.classList.remove("on");
}

/* --------------------------------------------------------- 「你说」回显（主画面）
   把用户这句话留在画面上（为什么需要，见 index.html / style.css 里的长注释）。
   三条输入通路——#sendBtn 打字、麦克风 STT、#suggest 建议条——最后都进 sendTurn()，
   所以只在 sendTurn() 里上屏一处就够了。**建议条那条是重点**：以前点完只换了建议条、
   她立刻开口，画面没有任何"我选了这句话"的痕迹，观众看到的就是她在自说自话。

   停留规则（按真人对话的读法定的）：
     - 发送瞬间就亮，一直挂到她这一轮说完、回到待机之后 ~1.4s 才淡出 ——
       两条同屏才看得出"她在接我哪一句"。
     - 最短 3.2s：她秒回（mock/短句）也不能一闪而过，观众还没读完。
     - 最长 24s：她不回（网络失败、被连续打断）时也不能永久挂着一句旧话。
     - 新的一句直接替换旧的：同屏永远只有一条「你说」，不会叠两条。 */
const YOU_SAID = { on: false, shownAt: 0, readyAt: 0, maxT: 0, fadeT: 0 };

function showYouSaid(text) {
  const box = $("youSaid"), t = $("youSaidText");
  if (!box || !t) return;
  const s = String(text || "").replace(/\s+/g, " ").trim();
  if (!s) {                       // 开场轮（kind=open）没有说话内容，不挂空行
    hideYouSaid();
    return;
  }
  t.textContent = s;
  box.classList.add("on");
  YOU_SAID.on = true;
  YOU_SAID.shownAt = performance.now();
  YOU_SAID.readyAt = YOU_SAID.shownAt + 3200;
  clearTimeout(YOU_SAID.maxT);
  clearTimeout(YOU_SAID.fadeT);
  YOU_SAID.maxT = setTimeout(hideYouSaid, 24000);
}

/* 她这一轮说完了（状态回到待机）→ 再给 1.4s 让她的话落一下，然后收掉。
   若她回得很快、还没到 3.2s，就等到 3.2s 再收（见上面的最短停留）。 */
function settleYouSaid() {
  if (!YOU_SAID.on) return;
  const left = YOU_SAID.readyAt - performance.now();
  clearTimeout(YOU_SAID.fadeT);
  YOU_SAID.fadeT = setTimeout(hideYouSaid, Math.max(1400, left));
}

function hideYouSaid() {
  YOU_SAID.on = false;
  clearTimeout(YOU_SAID.maxT);
  clearTimeout(YOU_SAID.fadeT);
  const box = $("youSaid");
  if (box) box.classList.remove("on");
}

function setState(name) {
  /* 回到待机 = 这一轮说完了 → 「你说」行开始收起（见 settleYouSaid 里的停留规则） */
  if (name === "idle") settleYouSaid();

  S.state = name;
  $("app").dataset.state = name;
  $("stateLabel").textContent =
    { idle: "待机", listening: "倾听", thinking: "思考", speaking: "说话" }[name] || name;
  const hints = {
    idle: "按住麦克风说完松开，或轻点一下说话（说完停顿会自动发送）",
    listening: "正在录… 说完停顿一下自动发送；再点一下麦克风也能立刻发送",
    thinking: "正在想怎么回答…",
    speaking: "点一下麦克风可以打断她",
  };
  if (!MIC.rec) $("hint").textContent = hints[name] || "";
  /* 状态要能从画面上看出来，而不只是顶部标签。
     表演层接管时（PERF.active）表情由 beat 排期，这里只保留「倾听」这一个
     必须是即时反应的：用户一按话筒，她的脸就该立刻转向你。 */
  if (S.suppressSprite) {
    /* 打断时先保状态标签、别抢脸（见 interrupt()） */
  } else if (name === "listening") { if (!SEQ.on) poseTo("listen", { prio: PRIO.emo }); }
  else if (PERF.active) {
    /* beat 负责 thinking/speaking/idle 的表情，这里不抢 */
  } else if (name === "thinking") { if (!SEQ.on) poseTo("thinking", { prio: PRIO.base }); }
  else if (name === "speaking" || name === "idle") {
    // 动作贴片（sip_coffee / phone_call …）还在时效内就别抢它的画面
    // 2026-10-20 反馈 12：**呼吸循环在跑的时候也不许抢**。实测（sprite_trace）待机
    // 8 秒里插进来一次 neutral.webp(150ms)，把 340ms 的呼吸叠化硬切断，接着下一次
    // breathTick 又从半途重新淡入 —— 合成上就是每 ~5 秒一次的"闪一下"。
    if (!BREATH.on && Date.now() >= (S.poseUntil || 0)) {
      if (!SEQ.on && poseTo(S.emotion, { prio: PRIO.base })) { /* 走帧路径 */ }
      else paint(ASSETS.char[S.emotion] || ASSETS.char.neutral, PRIO.base);
    }
  }
  if (name === "idle") scheduleNudge();
  /* 呼吸循环只在 idle 且是中性脸时跑（见 breathStart）；离开 idle 立刻收，
     免得它盖住 listening/thinking 的脸。延时 200ms 是为了等静态层的 170ms 叠化走完。 */
  if (name === "idle") setTimeout(breathStart, 200);
  else breathStop();
  ambDuck(name === "speaking");
}

/* 待机氛围：**没有视频可播了**（2026-10-20 全删）。背景是一张静帧，动的只有角色层。
   旧的 updateIdleLoop() 只负责起停 idle_loop.mp4，随视频一起删除。 */

/* 素材只有一套：assets/char + assets/plate，全部由 Krea-2 Turbo（JANK2 int8，
   Qwen-Image VAE）重画 —— 2026-10-20 把旧的 SDXL(waiIllustrious) 那套整个删了，
   不再保留第二套、也不再要 ?assets= 开关：同时存在两套就必然出现「一部分戴帽子
   一部分不戴」的混用。清单固定 char/manifest.json，前端与素材解耦。 */
function preload() {
  // 先把清单拉下来（姿势/动作/过渡都在里面），再预加载所有会用到的图。
  const ready = loadSeqManifest();
  ready.finally(() => {
    const all = { ...ASSETS.char, ...ASSETS.pose, ...ASSETS.plate, ...ASSETS.insert };
    Object.values(all).forEach((src) => {
      const img = new Image();
      img.src = src;
    });
    // v17（2026-10-22）：动作序列帧 + 表情过渡帧整套已下架（char/_parked/v17，见
    // manifest._v17_parked）。清单里不再有 seq / transitions 键，序列帧预取一并删掉 ——
    // 否则每次进页面会白拉 188 张已经不播的图，还白占内存。
    // 呼吸帧仍要预取：它现在走 A/B 主通道（不是覆盖层），第一轮没缓存就会"换图等 onload"，
    // 于是每张都晚 10–40ms 落地 —— 6 张叠起来就是一串不均匀的节拍，必须一起预取。
    (BREATH.mode === "frames" ? (BREATH.frames || []) : []).forEach((src) => {
      const img = new Image();
      img.src = src;
    });
  });
}

/* -------------------------------------------------------------- 场景/表情 */

function showPlate(src) {
  const front = S.plateFront === "A" ? $("plateA") : $("plateB");
  const back = S.plateFront === "A" ? $("plateB") : $("plateA");
  if (front.getAttribute("src") === src) return;
  back.onload = () => {
    back.classList.add("is-front");
    front.classList.remove("is-front");
    S.plateFront = S.plateFront === "A" ? "B" : "A";
  };
  back.classList.remove("is-front");
  back.src = src;
}

/* 换表情/姿势只切这一层。叠化的快慢分场合：
   - 换脸 220ms（两张图 96% 像素相同，慢了像脸在"融化"，快了像闪）；
   - 换姿态 220ms（差异大，需要一点时间读成"她动了"）。
   关键：**同一时刻只允许一次叠化**。 */
/* ------------------------------------------------------------ 换图（唯一路径）
 * 全站只有这一处改「她长什么样」。呼吸、表情、姿势、过渡帧收尾，全部走它。
 *
 * 四条铁律（每条都是被用户当面报过、又被量出来过的）：
 *  1) **新图在旧图上面淡入，旧图保持 opacity 1**。两张各 0.5 时合成 alpha 只有 0.75，
 *     会漏 25% 背景 → 一次发暗的闪（用户第 3 条）。
 *  2) **旧图等新图真的到 1 才撤**（transitionend，不是死等 ms）：早撤会把下面那层
 *     按 DOM 顺序顶上来，又闪一次。
 *  3) **正在叠化/正在加载时来了新目标，不许"瞬间结算"**（旧 settleSprites 会一步跳到
 *     终值 = 字面硬切），排队到本次结束再执行。
 *  4) **撤场不许被"更新的一代"挡住**。旧版把 done() 用 token 拦了一道：只要在这期间
 *     又发起了新换图，旧层的 is-front 就永远撤不掉，于是两层都停在 opacity 1。
 *     下一帧呼吸从这层"僵尸"开始交叉，一个单帧跳变就出现了 —— 这就是
 *     shimmer_map 在 idle 8 秒里量到的 4 次 cut（peak 2.8–4.5）的直接来源。
 *     现在 done() 无条件执行，并且每次落图后强制"最多只有一层 is-front"。
 */
/* 换图时长（**唯一来源**，改这里就够）。
   2026-10-20 实测：13 张贴片互相之间是**不同的画**（raw mad 61~80/255，
   最优点对齐后仍有 379k~608k 个像素差 >32/255，头部位置最大差 42px）——
   不是同一张脸换表情。所以任何长叠化都会同时显示两张不同的画 = 重影：
     - 380ms（修 transition 之前的实际值）：13~23 帧双影，用户报的就是这个
     - 80ms：5 帧，边缘只是轻微柔化，读成她换了姿势，不会读成两个人
   想要完全无重影就把这里改成 0（硬切）：代价是大幅动作（sip_coffee 头差 42px）
   看起来是跳而不是动。80ms 是这两者之间的取中。 */
/* 2026-10-21 用户第五次反馈（看完 v15 成片）：
 *   「换姿势还是硬切；硬切解决不了就做淡入淡出；千万别一次硬切多张图，看起来像人物闪了一下」
 * 之前把这里设成 0（硬切）+ 序列层播 4 帧（每帧 60~140ms 换一张 src）——那正是
 * 用户看到的「闪」：一秒钟内换 4~5 张不同的画。现在做两处根本性改动：
 *   ① 这里给一个**真叠化**：260ms 交叉淡入（两张不同的画各占一半只持续十几帧，
 *      在有头对头注册的素材上读成"她换了个姿势"，不会被读成两个人）；
 *   ② **删掉所有多帧硬切路径**（playSequence / playTransition / poseTo 的帧路径），
 *      任何时刻最多只有一次换图在发生，换完至少停 360ms 才有下一次。
 * 想 A/B 就加 `?fade=0`（硬切）或 `?fade=120`。 */
let SPRITE_MS = 260;
const SPRITE_MIN_DWELL = 360;
const SWAP = { busyUntil: 0, timer: 0, pending: null, inflight: false };

/* ------------------------------------------------------ 单一画师（2026-10-21）
 * 用户第三次报「硬切换 / 重叠 / 闪烁」。这次没有再猜，而是用探针
 * （tools/qa/flicker_turn.py）在真浏览器里跑 2 轮对话、逐帧采样 + 给 showSprite
 * 装调用栈钩子，结论不是"某处叠化太长"，是**换图没有主人**：
 *
 *   19 秒里 showSprite 被调用 13 次，来自 6 个不同的源
 *   （setState / applyEmotion / playTransition 尾帧与 400ms 兜底 /
 *     applyAction 尾帧 / gaze 回位 / 看门狗），谁最后调谁赢；
 *   相邻两次最小间隔 **51ms** —— smile 刚落地 51ms 就被 neutral 覆盖。
 *   逐帧看：34 个"观众看到的图段"里 25 个短于 200ms，10 个短于 120ms。
 *   同时 seq 层在 1141 帧里有 128 帧（11%）**两张不同的画同时不透明** —— 那就是重叠。
 *
 * 三条约束全部收在这一个函数里，调用点不要再各写一份：
 *   ① 幂等：已经在放这张就直接返回（顺带丢掉过期的排队目标）
 *   ② 持有期：高优先级持有期内，低优先级请求**直接丢**，不排队
 *   ③ 最短驻留：任意两次换图间隔 ≥ SPRITE_MIN_DWELL，挡住连闪
 * 优先级：4=序列帧 3=动作贴片 2=视线 1=表情 0=状态/兜底/回位
 */
const PRIO = { seq: 4, pose: 3, gaze: 2, emo: 1, base: 0 };
const HOLD = { prio: -1, until: 0, url: "" };
const SPRITE = { now: "", last: 0 };

function spriteName(url) {
  return String(url || "").split("/").pop();
}

/* 所有"她长什么样"的请求的唯一入口。返回 true = 真的排上了。 */
function paint(url, prio, holdMs) {
  if (!url) return false;
  const p = prio == null ? PRIO.emo : prio;
  const now = performance.now();
  if (url === SPRITE.now) {
    SWAP.pending = null;
    clearTimeout(SWAP.timer);
    /* 幂等分支也必须确认真有人**在画面上**。曾经出现过这种状态：SPRITE.now 记着
       neutral.webp，但两层 A/B 的 is-front 都被撤掉了（叠化收尾时新层没接上），
       于是整张角色层透明，而之后每一次 applyEmotion("neutral") 都命中这里直接
       return —— 画面永远没有人。用户看到的就是「人物突然消失」。
       检测到没人就破幂等，让它真的重画一次。 */
    const a = $("charA"), b = $("charB");
    const shown = (a && a.classList.contains("is-front")) ||
                  (b && b.classList.contains("is-front"));
    if (shown) return true;
    SPRITE.now = "";
    SPRITE.last = 0;
  }
  if (now < HOLD.until && p < HOLD.prio) {
    console.info("[sprite] 让步", spriteName(url), "←", spriteName(HOLD.url));
    return false;
  }
  HOLD.prio = p;
  HOLD.until = now + Math.max(Number(holdMs) || 0, SPRITE_MIN_DWELL);
  HOLD.url = url;
  showSprite(url, SPRITE_MS);
  /* 记下"画面上现在是哪个姿势"。poseTo 要靠它算下一次的起点——以前没有这个变量，
     所以每次换姿势都只能假设"从 neutral 出发"，这正是硬切的第二个来源。 */
  if (!SEQ.on) S.face = nameOfUrl(url) || S.face;
  return true;
}


/* 不变量：任意时刻最多一层 is-front。keep 是刚落成的前景层。 */
function sweepSprites(keep) {
  [$("charA"), $("charB")].forEach((el) => {
    if (el && el !== keep) el.classList.remove("is-front");
  });
}

/* 当前前景层是哪一张 img（按 S.spriteFront，而不是闭包里记的旧值） */
function spriteFrontEl() {
  return S.spriteFront === "A" ? $("charA") : $("charB");
}

function flushSwap() {
  clearTimeout(SWAP.timer);
  const p = SWAP.pending;
  if (!p) return;
  if (SWAP.inflight || performance.now() < SWAP.busyUntil) {
    SWAP.timer = setTimeout(flushSwap, 40);
    return;
  }
  SWAP.pending = null;
  showSprite(p.src, p.ms);
}

function showSprite(src, dur) {
  if (!src) return;
  const now = performance.now();
  /* ③ 最短驻留（最后一道闸）：即使上层的优先级算错了，也不允许 220ms 内换两次。
     探针实测最小间隔 51ms —— 那就是用户嘴里的「闪一下」。 */
  const wait = SPRITE.now ? SPRITE.last + SPRITE_MIN_DWELL - now : 0;
  if (wait > 0) {
    SWAP.pending = { src, ms: dur };
    clearTimeout(SWAP.timer);
    SWAP.timer = setTimeout(flushSwap, Math.ceil(wait));
    return;
  }
  if (SWAP.inflight || now < SWAP.busyUntil) {
    SWAP.pending = { src, ms: dur == null ? SPRITE_MS : dur };
    clearTimeout(SWAP.timer);
    SWAP.timer = setTimeout(flushSwap, 40);
    return;
  }
  /* 前后景层**按真实不透明度**判定，而不是按 S.spriteFront 这个记账变量。
     2026-10-21 成片复核发现：38 次换图里偶发 1 次是"一帧换掉"（不是叠化）。
     根因是记账和实际显示可能错开一格（上一轮叠化收尾的 sweep、或某次让位被丢掉）：
     一旦 front/back 说反，代码会把**正在显示的那层**瞬间 opacity 设 0 —— 观众看到
     人物闪一下。这里改成读计算样式，谁不透明谁是前景，记账变量只用来兜底。 */
  const elA = $("charA");
  const elB = $("charB");
  if (!elA || !elB) return;
  const opA = parseFloat(getComputedStyle(elA).opacity || "0") || 0;
  const opB = parseFloat(getComputedStyle(elB).opacity || "0") || 0;
  /* 先认 is-front（那是"这一层是当前画面"的正式标记），只有标记缺失/冲突时才退回
     比不透明度。为什么不直接比不透明度：一次叠化进行到一半时两层都是 0.5，比大小
     会选错，把**正在淡入**的那层当成前景再淡出一次 —— 观众看到的就是一帧换掉。 */
  const aFront = elA.classList.contains("is-front");
  const bFront = elB.classList.contains("is-front");
  let front;
  if (aFront && !bFront) front = elA;
  else if (bFront && !aFront) front = elB;
  else front = opA >= opB ? elA : elB;
  const back = front === elA ? elB : elA;
  S.spriteFront = front === elA ? "A" : "B";
  if (front.getAttribute("src") === src || SPRITE.now === src) {
    // 目标就是当前这张：把排队中的旧目标丢掉，别让过期的换图在 200ms 后才落地
    SWAP.pending = null;
    clearTimeout(SWAP.timer);
    return;
  }
  /* 换姿势永远是叠化（用户第 3 条：「不要硬切，就做淡入淡出」）。
     调用方传 0 或很小的值时不再当成硬切，抬到 SPRITE_MS —— 真正的硬切只留给
     下面 BREATH 的帧流（现在默认不跑），以及首帧（那时还没有前景层）。 */
  const asked = dur == null ? SPRITE_MS : Number(dur) || 0;
  const ms = Math.max(SPRITE_MS, asked);
  SWAP.inflight = true;
  back.style.transition = "none";
  back.style.opacity = "0";
  back.classList.remove("is-front");
  back.style.transitionDuration = `${ms}ms`;
  front.style.transitionDuration = `${ms}ms`;
  const old = front;
  let started = false;          // start 可能被 onload / 超时兜底调用两次，只认第一次
  const start = () => {
    if (started) return;
    started = true;
    back.onload = null;
    SWAP.inflight = false;
    void back.offsetWidth;                    // 强制回流，保证 0 → 1 真的能跑过渡
    /* 2026-10-20 修：这里原来是 `back.style.transition = ""`。
       transition 是**简写**，清空它会把上面刚设的 transitionDuration 一起抹掉，
       transition-duration 于是落回样式表里的 380ms —— 调用方要的 ms 从来没生效过。
       实测（idle_trace + render_trace）：呼吸帧要 340ms、表情要 170/220ms，
       实际每一层都是 380ms，这就是"每次换图都在长叠化里"的直接原因。
       现在只把简写换成等价的三个长写，duration 用调用方给的值。 */
    back.style.transitionProperty = "opacity";
    back.style.transitionTimingFunction = "ease";
    back.style.transitionDuration = `${ms}ms`;
    back.style.opacity = "";
    back.style.zIndex = "2";
    old.style.zIndex = "1";
    back.classList.add("is-front");
    S.spriteFront = S.spriteFront === "A" ? "B" : "A";
    SPRITE.now = src;
    SPRITE.last = performance.now();
    SWAP.busyUntil = SPRITE.last + ms;
    /* 诊断钩子（零成本：没人装就一行判断）。为什么需要：
       换图有 14 个调用点，用户在页面上看到「闪一下」时无从知道是谁改的脸。
       探针（tools/qa/flicker_turn.py / record_demo_v16.py）装上它就能拿到**调用栈**，
       把每次换图归因到具体函数，而不是靠猜。
       2026-10-21：原来这个钩子挂在函数入口，**排队/让位的那次也会记一笔** ——
       于是「两次换图间隔 47ms」这种告警其实是一次真换 + 一次被延后的重排。
       现在只在真的开始叠化时记，量出来的间隔才是观众看到的间隔。 */
    if (window.__miraSwapHook) {
      try { window.__miraSwapHook(src, ms, (new Error()).stack || ""); } catch (e) { /* 探针自己的错不传染 */ }
    }
    const settle = () => {
      sweepSprites(spriteFrontEl());
      flushSwap();
    };
    /* 硬切（ms = 0）必须**同步**撤旧层。改成"等一个定时器再撤"的话，从新层不透明
       到旧层撤掉之间会有一帧以上两张不同的画同时 opacity 1 —— 那正是探针量到的
       「重叠」（two drawings visible at once）。只有真的在做叠化时才该等新层到 1。 */
    if (ms > 0) setTimeout(settle, ms + 60);
    else settle();
  };
  const fail = () => {
    back.onload = null;
    SWAP.inflight = false;
    SWAP.busyUntil = 0;
    flushSwap();
  };
  if (back.getAttribute("src") === src) {
    /* src 相同不能直接再赋一次后等 onload：浏览器认为"这张已经在加载/加载完了"，
       不会重新触发 load 事件，onload 永远不来。而上面已经把这层的 is-front 撤掉、
       opacity 设成 0 —— 于是 A/B 两层同时透明，整张角色层是空的，人物从画面上消失
       且再也不会自己回来（幂等分支还会因为 SPRITE.now 没变而直接返回）。
       已经加载好的直接 start；还在路上的，除了挂 onload 再按 complete 兜一次。 */
    if (back.complete && back.naturalWidth > 0) { start(); return; }
    back.onload = start;
    back.onerror = fail;
    setTimeout(() => {
      if (SWAP.inflight && !started && back.complete && back.naturalWidth > 0) start();
    }, 140);
    return;
  }
  back.onload = start;
  back.onerror = fail;
  back.src = src;
}

/* ---- 多帧动作序列 ------------------------------------------------------
 * 数据：ASSETS.seq[action] = [url0, url1, …]（由 char/manifest.json 填进来）。
 * 播放：显示第一帧 → 按 fps 硬切 → 最后一帧停到 hold 结束 → 静态层换成目标表情、
 * 序列层 140ms 淡出。收尾是一次真正的叠化，不会「闪一下」。
 *
 * 为什么必须单独一层（#charC）：A/B 两层的 .sprite 有 380ms 过渡，是给"一帧换一帧"
 * 用的；连续帧需要 80–120ms 的硬切，塞进同一层会被 380ms 模糊掉 —— 那看起来就是
 * 用户说的「卡顿与闪速」。
 *
 * 为什么用 setInterval 而不是 rAF：帧是离散的，时间轴要可预期；QA 探针靠
 * window.__mira.seq 断言"这一轮播了第几帧"。
 */
const SEQ = { timer: 0, endT: 0, hideT: 0, on: false, frames: [], playing: "", idx: 0, side: 0, ms: [] };

/* 每个动作的帧与「每帧停留多久」都来自 manifest（不再假设恒定 fps）。
   sip_coffee 的帧长是 110/110/110/110/900 —— 最后一帧停 900ms，等于「播完定在那儿」，
   这是交付方定的约定：frames[-1] 永远就是该动作的定稿贴片。 */
const SEQ_META = {};
const TRANS = {};            // "neutral->smile" → { frames:[url0,url1], ms:[70,70] }
/* 帧间交叉淡入：**上限**，不是固定值。
   用户 2026-10-20 的 9 条反馈里第 3 条是「没有连贯帧动画，而是极快的闪烁」。
   根因用 flicker_probe 量出来：v8 清单给的是 110ms 一帧 + 90ms 交叉淡入，
   于是序列层 82% 的时间都在「两张半透明之间」；五张各画一次的中间帧连起来就是闪。
   现在改成 ①每帧至少停 SEQ_MIN_MS ②淡入 ≤ 帧长的 35%（见 frameFade）。
   视觉上从「闪」变成 7fps 的手绘动画：每帧有 65% 的时间是清晰的一张图。 */
/* 帧间交叉淡入。**2026-10-21 改成 0（硬切）**，理由是被量出来的：
   这些帧不是同一张画的 denoise 变体，而是**四个不同姿势**（stand_00..03 头/肩位置
   逐帧在动）。两张不同的画各给 50% 不透明度 = 双影，探针（flicker_turn.py）在 19s 里
   量到 128 帧（11%）两帧同时不透明，用户报的「重叠」就是它。
   硬切之后每帧 100% 时间都是清晰的一张图，读成 7fps 的定格动画；而"闪"来自
   **帧太短 + 没有停顿**，不是来自硬切——所以真正的解是帧长（SEQ_MIN_MS）与
   帧序设计，不是把两张画糊在一起。manifest 里 playback.crossfadeMs 不再覆盖它。 */
let XFADE = 0;               // 帧间交叉淡入（0 = 硬切）
const SEQ_MIN_MS = 140;      // 单帧最短停留

function frameFade(ms) {
  const m = Math.max(SEQ_MIN_MS, Number(ms) || SEQ_MIN_MS);
  return Math.round(Math.min(XFADE, m * 0.35));
}

/* 待机呼吸（v9.1：**换成连续 transform**，v9 的 6 帧离散换图下线）
 *
 * 为什么把 v9 的呼吸帧停掉（2026-10-20 第三次反馈「闪烁 + 硬切换 + 重影」）：
 *   v9 的 6 张 breath_*.webp 并不是"画出来的 6 帧"，而是把 neutral 的**躯干带**
 *   沿 smoothstep 权重垂直平移 0 / -1.7 / -3.0 / -2.5 / -1.3 / 0 px（见
 *   D:\ai\mira_anime_work\v9\breath_deform.py）。工具量出来的后果：
 *     - breath_0 和 breath_5 的 MD5 与 neutral.webp 完全相同（639640547F）→
 *       循环里有 840ms 完全静止，然后突然开始动；
 *     - 位移带 x241..725 / y714..1090 里的高对比线稿（背带、衣领、袖口折线）
 *       整条挪 3px：局部 mad 15.0/255、p99.9 235/255、>64/255 的像素 15861 个；
 *     - 代码又在 420ms 的帧长上做 340ms 叠化（且被 380ms 的 CSS 默认值覆盖），
 *       于是 92.6% 的时间两层同时可见、37.8% 的采样落在叠化中途 ——
 *       两条相差 3px 的线半透明叠在一起，就是用户嘴里的「重影」；
 *       位移带的收边（y 690→780 / 1040→1130）正好横跨轮廓线，轮廓在那里被
 *       撕开一道，就是上一轮说的「轮廓锯齿」。
 *   一句话：静止的线稿动 3px + 比帧长还长的叠化 = 每 0.42s 一次的双影。
 *
 * 现在：呼吸交回**同一张图**的连续 scaleY（motionTick 的 sy 路径），
 *   亚像素级、单调、无换图、无透明度叠加 —— 结构上不可能重影，也不可能硬切。
 *   BREATH.mode 留在 "transform"；改成 "frames" 可以把 6 帧路径接回来做对照实验。
 */
const BREATH = { frames: [], ms: [], on: false, timer: 0, idx: 0, mode: "transform",
  /* 呼吸振幅（scaleY 的直流偏置）。2026-10-20 实测（tools/qa/breath_ab.py，
     渲染像素帧间差，clip 只有人物躯干）：
       amp 0.000 → 帧间差 0.000（完全静止，零风险）
       amp 0.003 → mean 0.49，>24/255 约 3.4k 像素（边缘重采样的量级）
       amp 0.006 → mean ≈0.8，行程约 4.4 设备像素（≈2.2 CSS px）
       amp 0.010 → mean 1.3，>24/255 约 20k
       amp 0.018 → mean 2.4（旧默认，明显"抖"）
     取 0.006：画面上她真的在起伏，但每帧只有 ~0.05 设备像素的位移。 */
  amp: 0.010 };

/* 评审/排障开关：`?breath=0` 完全静止（判定"闪"是不是呼吸引起的），`?breath=3` 弱，
   默认 10（千分比 = scaleY 1.0%）。`?fade=0` 把换图改成硬切、`?fade=160` 回退成旧的长叠化
   —— 都不用改代码就能在同一个浏览器里 A/B。 */
(function () {
  try {
    const q = new URLSearchParams(location.search);
    const v = q.get("breath");
    if (v !== null && v !== "" && !isNaN(Number(v))) {
      BREATH.amp = Math.max(0, Math.min(0.02, Number(v) / 1000));
    }
    const f = q.get("fade");
    if (f !== null && f !== "" && !isNaN(Number(f))) {
      SPRITE_MS = Math.max(0, Math.min(380, Number(f)));
    }
  } catch (e) { /* 老浏览器没有 URLSearchParams：用默认值 */ }
})();

function breathStop() {
  if (!BREATH.on) return;
  BREATH.on = false;
  clearTimeout(BREATH.timer);
  BREATH.timer = 0;
}

function breathTick() {
  if (!BREATH.on) return;
  const i = BREATH.idx % BREATH.frames.length;
  // 帧间**硬切**（xfade=0）：两张只差零点几像素，叠化只会制造双影。
  showSprite(BREATH.frames[i], 40);
  BREATH.idx += 1;
  BREATH.timer = setTimeout(breathTick, BREATH.ms[i] || 420);
}

function breathStart() {
  /* v9.1：默认不再跑换图循环（见上面 BREATH 的注释）。 */
  if (BREATH.mode !== "frames") return false;
  if (BREATH.on || SEQ.on || SEQ.playing) return false;
  if (!BREATH.frames.length) return false;
  if (S.state !== "idle" || S.emotion !== "neutral") return false;
  if (S.motionOff) return false;
  BREATH.on = true;
  BREATH.idx = 0;
  breathTick();
  return true;
}

/* 序列层是带 alpha 的贴片，不是满幅不透明图（CSS 里那条注释写错了）。
   它盖在 A/B 上时，底层那张旧姿势会从新帧透明的地方透出来——抬手时旧手臂还在原位，
   合成上就是两条手臂。所以播帧期间把 A/B 藏起来，收尾时同一帧里一起切换。 */
function seqBaseShow(on) {
  const s = $("stage");
  if (s) s.classList.toggle("seqmode", !!on);
}

function seqStop() {
  clearTimeout(SEQ.timer);
  clearTimeout(SEQ.endT);
  clearTimeout(SEQ.hideT);
  SEQ.timer = 0;
  SEQ.endT = 0;
  SEQ.hideT = 0;
  SEQ.on = false;
  SEQ.playing = "";
  SEQ.idx = 0;
  const box = $("charC");
  if (box) box.classList.remove("on");
  seqBaseShow(false);
  ["charC1", "charC2"].forEach((id) => {
    const e = $(id);
    if (e) {
      e.classList.remove("show");
      e.style.zIndex = "";
    }
  });
}

/* 画一帧：写到「另一张」img 上并抬它的 opacity，旧的淡出。
   xfade=0 就是硬切（第一帧用，避免淡入两次）。 */
function seqPaint(url, xfade) {
  const a = $("charC1");
  const b = $("charC2");
  if (!a || !b) return;
  if (window.__miraSwapHook) {
    try { window.__miraSwapHook(url, -1, (new Error()).stack || ""); } catch (e) { /* 同上 */ }
  }
  const cur = SEQ.side === 0 ? a : b;
  const nxt = SEQ.side === 0 ? b : a;
  SEQ.cur = url;      // 被打断时新路径从这里接（否则会用上一次的静态贴片，视觉上跳一下）
  const ms = xfade == null ? XFADE : xfade;
  nxt.style.transitionDuration = ms + "ms";
  const flip = () => {
    /* 和 showSprite 同一个道理：新帧在旧帧**上面**淡入、旧帧先保持 1。
       两张各 0.5 时合成 alpha 只有 0.75，会漏 25% 背景 —— 那就是「闪一下」。 */
    nxt.style.zIndex = "2";
    cur.style.zIndex = "1";
    nxt.classList.add("show");
    SEQ.side = SEQ.side === 0 ? 1 : 0;
    /* 新帧到 1 之后再撤旧帧（同 showSprite）：先把旧帧撤掉会让底下那层按 DOM 顺序顶上来。
       2026-10-21：硬切（ms=0）时必须**同步**撤，原来固定 `ms + 90` 会让两张不同姿势
       各 100% 不透明地同时挂 90ms —— 虽然上面那张盖住了下面那张，但探针照样量到
       "两帧同时可见"，而且任何一次渲染误差都会露出来。 */
    clearTimeout(SEQ.hideT);
    if (ms > 0) SEQ.hideT = setTimeout(() => { cur.classList.remove("show"); }, ms + 90);
    else cur.classList.remove("show");
  };
  if (nxt.getAttribute("src") === url && nxt.complete) return flip();
  nxt.onload = flip;
  nxt.src = url;
}

/* 通用的「按帧表播放」，playSequence / playTransition 共用。
   onDone 在最后一帧的停留时长走完后触发（此刻序列层整体 140ms 淡出）。 */
function playFrames(urls, ms, onDone, opts) {
  const box = $("charC");
  if (!box || !urls.length) return false;
  const hardCut = !!(opts && opts.hardCut);
  seqStop();
  SEQ.on = true;
  SEQ.frames = urls;
  // 每帧都抬到 SEQ_MIN_MS 以上：清单里 110ms 那种节奏播出来是闪，不是动作。
  /* 原本一律抬到 SEQ_MIN_MS=140（动作序列 110ms 一帧会被"闪"）。但换姿势的补间路径
     一帧 90ms、整段 3~4 帧，抬到 140 会把一次换姿势拖成 0.6s 以上 —— 所以允许调用方
     指定下限：动作序列仍用 140，补间路径用 60。 */
  const minMs = (opts && opts.minMs) || SEQ_MIN_MS;
  SEQ.ms = (ms || []).map((v) => Math.max(minMs, Number(v) || minMs));
  SEQ.idx = 0;
  seqBaseShow(true);
  box.classList.add("on");
  seqPaint(urls[0], 0);
  const advance = (i) => {
    if (!SEQ.on || i >= urls.length) return;
    SEQ.idx = i;
    if (i > 0) seqPaint(urls[i], hardCut ? 0 : frameFade(SEQ.ms[i]));
    SEQ.timer = setTimeout(() => {
      if (!SEQ.on) return;
      if (i + 1 < urls.length) advance(i + 1);
      else SEQ.endT = setTimeout(() => {
        if (!SEQ.on) return;
        const done = onDone || ((emotion) => paint(ASSETS.char[emotion] || ASSETS.char.neutral, PRIO.base));
        done(S.emotion);
        SEQ.on = false;
        SEQ.playing = "";
        SEQ.idx = 0;
        /* 收尾必须**同一帧**切：静态层已经画好目标贴片，序列层最后一帧也是同一张图。
           先撤序列层再画静态层会露出底板（人物闪空一帧），反过来会看到旧姿势。 */
        requestAnimationFrame(() => {
          box.classList.remove("on");
          seqBaseShow(false);
          ["charC1", "charC2"].forEach((id) => {
            const e = $(id);
            if (e) e.classList.remove("show");
          });
        });
      }, SEQ.ms[i] || 110);
    }, (i === 0 ? 0 : SEQ.ms[i - 1]) || 110);
  };
  advance(0);
  return true;
}

function playSequence(action, opts) {
  /* 2026-10-21（用户第五次反馈）彻底下线：这就是「一秒内硬切 4~5 张图」的元凶。
     动作序列帧是 110ms 一帧、fps≈9，播放时序列层每帧换一次 src —— 观众看到的是
     人物在闪，不是手臂在抬。换成"一次 260ms 的交叉淡入直接到动作定稿贴片"。
     函数保留（调用点不改），但永远返回 false = 没有可播的序列路径。 */
  return false;
}

/* 表情过渡（2 帧 70ms）同上：也是多帧硬切，一并下线。 */
function playTransition(key, targetEmotion, opts) {
  return false;
}

/* ------------------------------------------------ 换姿势：只走「帧」，不走叠化
 *
 * 用户 2026-10-21 第四次反馈：立绘还是在硬切换、没有动画帧。
 * 前三次的修法都停在「叠化多长」上（380 → 80 → 0），但那是在**两张不同的画之间**
 * 调透明度：调长了是重影，调到 0 就是硬切。真正的缺口是**换姿势根本没有中间帧**：
 *   · 表情只在 neutral→X 时有两帧过渡，其余组合直接硬切；
 *   · 动作贴片每次都从 neutral 起播自己的 4 帧序列，演完又一下跳回表情贴片。
 *
 * 现在把"换姿势"统一成一条**帧路径**，交给序列层（#charC）播：
 *   表情↔表情  清单里的补间矩阵 trans_<a>_<b>_<i>.webp（tools/gen/morph_matrix.py，
 *              sdf 生成：剪影由两端 SDF 插值、脸区锁单一来源，中间帧是实心的单张画，
 *              不是两张半透明加起来 —— 实测 soft_alpha ≤0.01，光流/叠化是 0.14~0.27）
 *   动作↔表情  先把它自己的 4 帧动作序列**倒放**（手臂放回、身子坐正），再补到目标表情
 *   表情→动作  先补到 neutral，再正放目标动作的序列（手臂抬起来）
 * 路径上每一帧都是画出来的（补间帧 or 序列帧），端点永远是原贴片。
 * 没有可用路径的对（素材缺帧）仍然硬切 —— 有路径才播，缺席不装。
 */
function poseUrl(name) {
  return (ASSETS.char && ASSETS.char[name]) || (ASSETS.pose && ASSETS.pose[name]) || "";
}

function nameOfUrl(url) {
  if (!url) return "";
  for (const m of [ASSETS.char, ASSETS.pose]) {
    for (const k in m || {}) if (m[k] === url) return k;
  }
  return "";
}

/* 动作序列的帧表与每帧时长（<action>_00…<action>_03 + <action>，末帧 900ms 是"停住"）。
   这里用它两次：正放 = 手臂抬起来；倒放 = 手臂放回去。 */
function seqOf(name) {
  const meta = SEQ_META[name];
  const urls = ((meta && meta.frames) || ASSETS.seq[name] || []).filter(Boolean);
  if (urls.length < 2) return null;
  const metaMs = (meta && meta.ms && meta.ms.length === urls.length) ? meta.ms : null;
  return { urls, ms: metaMs || urls.map(() => 110) };
}

/* 每帧停留：序列帧本来 110ms，补间帧 90ms。夹在 60~140 之间 ——
   低于 60 是闪，高于 140 一次换姿势就拖成 1 秒。 */
function stepMs(v) {
  const n = Number(v) || 90;
  return Math.max(60, Math.min(140, Math.round(n)));
}

/* 回到 neutral 的一段帧（动作倒放 / 表情补间）。返回 null = 没有这条路。 */
function legToNeutral(from) {
  if (!from || from === "neutral") return { urls: [], ms: [] };
  const t = TRANS[from + "->neutral"];
  if (t && t.frames && t.frames.length) {
    return { urls: t.frames.slice(), ms: t.frames.map((_, i) => stepMs(t.ms && t.ms[i])) };
  }
  const s = seqOf(from);
  if (s) {
    // 倒放：从"动作定稿"那一张回到第 0 帧（≈neutral）。第一帧是当前画面，去掉。
    const urls = s.urls.slice(0, -1).reverse();
    const ms = urls.map((_, i) => stepMs(s.ms[s.urls.length - 2 - i]));
    return { urls, ms };
  }
  return null;
}

/* 从 neutral 走到目标的一段帧。 */
function legFromNeutral(to) {
  if (!to || to === "neutral") return { urls: [], ms: [] };
  const s = seqOf(to);
  if (s) {   // 动作：正放整条序列，末帧就是定稿贴片
    return { urls: s.urls.slice(), ms: s.urls.map((_, i) => stepMs(s.ms[i])) };
  }
  const t = TRANS["neutral->" + to];
  if (t && t.frames && t.frames.length) {
    return { urls: t.frames.slice(), ms: t.frames.map((_, i) => stepMs(t.ms && t.ms[i])) };
  }
  return null;
}

/* from → to 的整条帧路径（不含两端的原贴片）。null = 素材里没有可播的中间帧。 */
function posePath(from, to) {
  if (!from || !to || from === to) return { urls: [], ms: [] };
  const direct = TRANS[from + "->" + to];
  if (direct && direct.frames && direct.frames.length) {
    return { urls: direct.frames.slice(),
             ms: direct.frames.map((_, i) => stepMs(direct.ms && direct.ms[i])) };
  }
  if (from === "neutral") return legFromNeutral(to);
  if (to === "neutral") return legToNeutral(from);
  const a = legToNeutral(from);
  const b = legFromNeutral(to);
  if (!a || !b) return null;
  return { urls: a.urls.concat(b.urls), ms: a.ms.concat(b.ms) };
}

/* 换到某个姿势（**唯一入口**）。返回 true = 已经排上（真的落图或在叠化中）。
 *
 * 2026-10-21（用户第五次反馈）把这里从"帧路径"改成"一次叠化"：
 *   「换姿势还是硬切；硬切解决不了就做淡入淡出；**千万别一次性硬切多张图**，
 *     看起来就像人物闪了一下。」
 * v15 在这里串的是 4~16 帧的路径（补间帧 + 动作序列正放/倒放），每帧换一次 src，
 * 帧长 60~140ms —— 用户看到的"闪"就是它，不管每帧画得多准。
 * 现在只有一条规则：**目标贴片在 SPRITE_MS 内交叉淡入，一次只换一张。**
 * 中间靠 paint() 的三道闸（幂等 / 持有期 / 最短驻留）保证不会连闪。
 */
function poseTo(name, opts) {
  const o = opts || {};
  const url = poseUrl(name);
  if (!url) return false;
  if (o.hold) S.poseUntil = Date.now() + Number(o.hold);
  if (name === S.face) return true;          // 已经是这个姿势：只更新持有期，不换图
  S.face = name;
  paint(url, o.prio == null ? PRIO.emo : o.prio, o.hold);
  /* 恒返回 true：调用方（applyEmotion / applyAction / gaze）都把 false 当成"没有路径、你别管"，
     于是不再安排收尾计时器。现在唯一的路径就是叠化，收尾逻辑必须照常跑。 */
  return true;
}

/* 素材清单：卡自己的 assets/char/manifest.json。
   v17：清单地址由 /api/card 的 assets_manifest 给（`/card-assets/<id>/char/manifest.json`），
   而且服务端**已经把清单里的帧路径绝对化**，所以下面 seqAbsUrl 只负责兜底
   （旧布局/清单里写了相对路径时）。容忍几种写法（actions{} / sequences[] /
   直接「动作名 → 帧数组」），这样美术那边换命名不用改前端。 */
function seqBase() {
  return `${ASSET_BASE}char/`;
}

function seqAbsUrl(p) {
  if (!p) return "";
  const s = String(p);
  // 服务端下发的是绝对 URL（/card-assets/…），原样用；相对路径才拼基址（兜底）。
  if (/^(https?:)?\//.test(s) || s.startsWith("data:")) {
    return s.includes("?") ? s : (s.startsWith("/") ? `${s}?v=${ASSET_V}` : s);
  }
  /* 带上素材版本号：清单里的帧也是图片，换素材后同样会被浏览器缓存挡住。 */
  return seqBase() + s.replace(/^\.?\//, "") + `?v=${ASSET_V}`;
}

async function loadSeqManifest() {
  let m;
  try {
    const r = await fetch(ASSET_MANIFEST, { cache: "no-store" });
    if (!r.ok) return false;
    m = await r.json();
  } catch (e) {
    return false;
  }
  /* 1) 姿势：v8 用 poses{name:{file,bbox}}，早期草稿用 char{name:"x.webp"}，两种都认。
     只覆盖 manifest 里点名的键，缺失的继续用旧素材 —— 这样半套素材也能上线。 */
  const poseMap = {};
  if (m && m.poses && typeof m.poses === "object") {
    Object.entries(m.poses).forEach(([k, v]) => {
      const f = typeof v === "string" ? v : (v && v.file);
      if (f) poseMap[k] = f;
    });
  }
  if (m && m.char && typeof m.char === "object") {
    Object.entries(m.char).forEach(([k, v]) => {
      if (typeof v === "string" && !poseMap[k]) poseMap[k] = v;
    });
  }
  if (m && m.pose && typeof m.pose === "object") {
    Object.entries(m.pose).forEach(([k, v]) => {
      if (typeof v === "string" && !poseMap[k]) poseMap[k] = v;
    });
  }
  Object.keys(poseMap).forEach((k) => {
    const url = seqAbsUrl(poseMap[k]);
    if (k in ASSETS.char) ASSETS.char[k] = url;
    if (k in ASSETS.pose) ASSETS.pose[k] = url;
    // 清单里有、ASSETS 里没登记的（v8 多出来的 check_phone 等）也收下，
    // 否则 director 派了动作、前端却找不到图，会静默回落到表情层。
    if (!(k in ASSETS.char) && !(k in ASSETS.pose)) ASSETS.pose[k] = url;
  });

  /* 2) 动作序列：{frames:[...], frameMs:[...]} —— frameMs 逐帧给，最后一位是「播完停多久」。 */
  const seq = {};
  const meta = {};
  const take = (name, v) => {
    const frames = Array.isArray(v) ? v : (v && Array.isArray(v.frames) ? v.frames : null);
    if (!name || !frames || frames.length < 2) return;
    seq[name] = frames.map(seqAbsUrl);
    const ms = (v && Array.isArray(v.frameMs)) ? v.frameMs : null;
    meta[name] = { frames: seq[name], ms: (ms && ms.length === frames.length) ? ms.slice() : null };
  };
  if (m && m.actions && typeof m.actions === "object") {
    Object.entries(m.actions).forEach(([k, v]) => take(k, v));
  }
  if (m && Array.isArray(m.sequences)) m.sequences.forEach((s) => s && take(s.action || s.name, s));
  Object.entries(m || {}).forEach(([k, v]) => { if (Array.isArray(v)) take(k, v); });

  /* 3) 表情过渡帧：transitions{"neutral->smile":{frames,frameMs}}。 */
  const trans = {};
  if (m && m.transitions && typeof m.transitions === "object") {
    Object.entries(m.transitions).forEach(([k, v]) => {
      const frames = (v && Array.isArray(v.frames)) ? v.frames : null;
      if (!frames || !frames.length) return;
      const tm = (v.frameMs && v.frameMs.length === frames.length) ? v.frameMs.slice() : null;
      trans[k] = { frames: frames.map(seqAbsUrl), ms: tm };
    });
  }

  /* 4) 呼吸循环：breath{frames,frameMs}（v9 起交付）。没有就退回 CSS 微动。 */
  BREATH.frames = [];
  BREATH.ms = [];
  if (m && m.breath && typeof m.breath === "object") {
    const bf = Array.isArray(m.breath.frames) ? m.breath.frames : null;
    if (bf && bf.length >= 2) {
      BREATH.frames = bf.map(seqAbsUrl);
      const bms = (m.breath.frameMs && m.breath.frameMs.length === bf.length)
        ? m.breath.frameMs.map((x) => Math.max(180, Number(x) || 420)) : null;
      BREATH.ms = bms || BREATH.frames.map(() => 420);
    }
  }

  ASSETS.seq = seq;
  Object.keys(SEQ_META).forEach((k) => delete SEQ_META[k]);
  Object.assign(SEQ_META, meta);
  Object.keys(TRANS).forEach((k) => delete TRANS[k]);
  Object.assign(TRANS, trans);
  if (m && m.playback && typeof m.playback.crossfadeMs === "number") {
    /* 2026-10-21：清单里的 crossfadeMs 不再生效。它想解决的问题（帧间"描线沸腾"）
       和它实际造成的问题（两张不同的画同时半透明 = 重叠）不在同一个量级上，
       而后者是用户当面报的。需要做对照实验时用 `?xfade=60`，不要改清单。 */
    XFADE = 0;
  }

  console.info("[assets] 清单：" + Object.keys(seq).length + " 个动作序列 / "
    + Object.keys(trans).length + " 组表情过渡 / "
    + (BREATH.mode === "frames"
        ? BREATH.frames.length + " 帧呼吸循环"
        : "连续 transform 呼吸（amp " + (BREATH.amp * 100).toFixed(1) + "%）")
    + " / 交叉淡入 " + XFADE + "ms");
  // 静止时的底图也换成新的一套（manifest.poses.neutral 可能已经换了文件）
  const a = $("charA");
  if (a && !SEQ.on) a.src = ASSETS.char.neutral;
  if (S.state === "idle" && S.emotion === "neutral") setTimeout(breathStart, 60);
  return true;
}

function applyEmotion(emotion) {
  const prev = S.emotion;
  if (prev !== emotion) emoImpulse(emotion);
  S.emotion = emotion;
  // 呼吸只对 neutral 脸有效（清单里只有一套中性脸）；换表情就停掉呼吸帧流
  if (emotion !== "neutral") breathStop();
  if (SEQ.on) {
    /* 序列（动作或表情过渡）正在播：**不要中途 seqStop**。
       以前这里 seqStop() 之后立刻换脸，结果是一段动作/过渡被切成两半 ——
       探针里 4 帧过渡帧只放了 2 帧就被 thinking 顶掉，肉眼就是「硬切换」。
       现在只记下情绪就返回；序列收尾的尾帧画的是 **S.emotion**（不是入参），
       所以这期间又来的新情绪也不会被旧的那次覆盖。 */
    return;
  }
  // neutral→smile 这类组合有专门的过渡帧（2×70ms），比纯叠化更像「表情动了一下」
  /* 2026-10-21 第 4 次修：不再"有过渡帧就播两帧、没有就硬切"，而是统一走 poseTo ——
     它会从**当前画面上这张**（可能是动作贴片）出发，串出「倒放动作 → 补间到目标表情」
     的完整帧路径。没有路径的极端情况才落回硬切。 */
  if (prev !== emotion) {
    if (poseTo(emotion, { prio: PRIO.emo })) return;
  }
  paint(ASSETS.char[emotion] || ASSETS.char.neutral, PRIO.emo);
}

function applyScene(scene) {
  S.scene = scene;
  const stage = $("stage");
  $("dim").classList.toggle("lights-dim", scene === "lights_dim");
  stage.dataset.cam = scene === "window_close" ? "close" : "";
  showPlate(ASSETS.plate.cafe_night);
  chip(LABEL.scene[scene] || "");
}

function chip(text) {
  const el = $("actionChip");
  if (!text) return;
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(chip._t);
  chip._t = setTimeout(() => el.classList.remove("show"), 2600);
}

/* 卡包声明 7 个动作，贴片现在 7 张都有（check_phone 于 2026-10-20 补齐），
   所以这里不再做近似映射。缺图时下面的 `if (!pose) return;` 会静默跳过，
   回退逻辑在服务端/卡包层，不在客户端猜。 */

function applyAction(action, holdMs, silent) {
  if (action === "idle") return;
  /* 2026-10-21 用户拍板「一切以说话通道为主」：动作进画面之前先过通道仲裁。
     以前 applyAction 什么都不查 —— 服务端把 sip_coffee 排到句中、patch 又立刻执行，
     she 就一边喝一边说。挡住 / 降级都写进观测面板，否则"动作没了"会被当成 bug 去查图。 */
  const pick = chanPick(action);
  if (!pick.action) {
    addMetric("动作让步", `${action}：${pick.reasons.join("；")}`);
    console.info("[channels] 不播", action, pick.reasons);
    return;
  }
  if (pick.action !== action) {
    addMetric("动作降级", `${action} → ${pick.action}`);
    console.info("[channels] 降级", action, "→", pick.action, pick.reasons);
    action = pick.action;
  }
  breathStop();          // 动作要占用序列层，先把待机呼吸循环让出来
  if (!silent) chip(LABEL.action[action] || action);
  const stage = $("stage");
  const spec = stageSpec(action);
  const hold = Number(holdMs) || (spec && spec.hold_ms) || 4200;
  chanClaim(action, hold);
  if (action === "look_window") stage.dataset.cam = "push";
  if (action === "lean_in") stage.dataset.cam = "close";
  if (action === "stand") {
    stage.dataset.cam = "push";
    setTimeout(() => (stage.dataset.cam = ""), Math.min(hold, 3200));
  }
  if (action === "sip_coffee" || action === "check_phone") {
    stage.dataset.cam = "push";
    setTimeout(() => (stage.dataset.cam = ""), Math.min(hold, 2600));
  }
  if (action === "look_window")
    setTimeout(() => (stage.dataset.cam = ""), Math.min(hold + 600, 4000));

  MOTION.settleUntil = performance.now() + 260;
  S.poseUntil = Date.now() + hold;
  clearTimeout(applyAction._t);
  /* 动作也是"换姿势"，走同一个入口：poseTo 会先把当前表情补到 neutral、再正放这条动作
     自己的 4 帧序列（手臂抬起来）；演完 hold 之后再倒放回 neutral + 补到当前表情。
     以前这里是 playSequence(action) —— 它永远从 neutral 起播，所以"刚才还是担心的脸"
     会在序列第 1 帧硬跳成中性脸再演动作，收尾又硬跳回表情贴片：用户看到的那两次硬切
     就是它。 */
  if (!poseTo(action, { prio: PRIO.pose, hold: hold })) return;
  applyAction._t = setTimeout(() => {
    /* 2026-10-20 反馈 12：动作演完不要自己塞一张 neutral —— 那是**第二次**抢画面。
       sprite_trace 实测：applyAction 的收尾 showSprite(neutral,…) 落在呼吸循环中间，
       叠化被切断 → 肉眼看就是"闪一下"。这里直接把控制权交回呼吸循环；
       只有真的在用呼吸帧换图时（BREATH.mode==="frames"）才交回去，
       v9.1 的连续 transform 呼吸不需要这一段。 */
    if (S.state === "idle" && BREATH.mode === "frames" && BREATH.frames.length) {
      BREATH.idx = 0;
      if (!BREATH.on) setTimeout(breathStart, 40);
    } else {
      /* 动作演完回到当前表情。走 poseTo 而不是 paint：倒放序列 + 补间回表情，
         手臂是"放回去"的，不是一张剪影直接跳回坐姿。 */
      if (!poseTo(S.emotion, { prio: PRIO.base }))
        paint(ASSETS.char[S.emotion] || ASSETS.char.neutral, PRIO.base);
    }
  }, hold);
}

function applyEffect(effect) {
  /* 2026-10-21：雨丝层（#rain）与雨声一起下线 —— 用户第 2 条「去掉雨声的底噪，这个不要了」，
     视觉那层细斜线本来也是给"雨"做气氛的，留着会和"雨已经停了"的设定打架。
     雷只用闪光，其余 effect 一律静默。 */
  if (effect === "thunder") doFlash();
}

function doFlash() {
  const f = $("flash");
  f.classList.remove("on");
  void f.offsetWidth;
  f.classList.add("on");
}

/* ------------------------------------------------------------ 演出层（无视频）
 *
 * 2026-10-20：用户要求「删除所有视频，目前在用的视频都有问题」。删掉之后这里不再是
 * 一个"播放器 + 失败降级"的结构，而是三个各自成立的演出，每个都由**台词内容**触发
 * （服务端 event 字段 / 关键词兜底，见 app/main.py: keyword_event）：
 *
 *   thunder      → 闪电补光（三层渐变闪）+ 低频滚雷（WebAudio 噪声 + 低通 + 包络）
 *   phone_call   → 她接过手机拨号（v14；贴片 + 屏幕光晕 + 压低环境音）
 *   ending       → 全屏结局静帧（v14：朋友 / 陌生人）
 *
 * 为什么不再去找替代视频：这类片段必须和底板同一机位/同一光线才不"跳台"，
 * 而程序合成 + 一张静帧已经能把"画面变了、而且是因为你这句话变的"说清楚，
 * 成本还是 0 素材、0 授权、0 加载时间。
 */

function rumble(strength, dur) {
  /* 低频轰鸣：棕噪声 → 低通 180Hz → 慢包络。雷用 1.4s，列车用 2.6s。
     不用素材的原因和上面一样：体积、授权、以及它能按事件时长拉伸。 */
  try {
    if (!AMB.ctx) ambienceInit();
    const ctx = AMB.ctx;
    if (!ctx) return;
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    const n = Math.floor(ctx.sampleRate * Math.max(0.6, dur || 1.4));
    const buf = ctx.createBuffer(1, n, ctx.sampleRate);
    const d = buf.getChannelData(0);
    let last = 0;
    for (let i = 0; i < n; i++) {
      last = (last + 0.03 * (Math.random() * 2 - 1)) / 1.03;
      d[i] = last * 2.4;
    }
    const src = ctx.createBufferSource();
    src.buffer = buf;
    const lp = ctx.createBiquadFilter();
    lp.type = "lowpass";
    lp.frequency.value = 180;
    const g = ctx.createGain();
    const t = ctx.currentTime;
    g.gain.setValueAtTime(0.0001, t);
    g.gain.linearRampToValueAtTime(0.22 * (strength || 1), t + 0.12);
    g.gain.exponentialRampToValueAtTime(0.0001, t + (dur || 1.4));
    src.connect(lp);
    lp.connect(g);
    g.connect(ctx.destination);
    src.start(t);
    src.stop(t + (dur || 1.4) + 0.05);
  } catch (e) {
    /* 没有 Web Audio 就安静：闪电本身已经是视觉事件 */
  }
}

function hideCinema() {
  const cinema = $("cinema");
  clearTimeout(showStill._t);
  clearTimeout(triggerEvent._t);
  cinema.classList.remove("show", "still-ready", "fallback");
  const still = $("eventStill");
  if (still) {
    still.onerror = null;
    still.onload = null;
    still.removeAttribute("src");
  }
  $("cinemaBadge").classList.remove("show");
  S.mediaEventActive = false;
  if (S.audioDone && !S.playing && !S.queue.length) setState("idle");
}

function showStill(src, badge, ms) {
  const cinema = $("cinema");
  const still = $("eventStill");
  if (!still) return;
  if (!src) {
    degradeEvent("still", "该事件没有静帧");
    return;
  }
  cinema.classList.add("show");
  cinema.classList.remove("fallback", "still-ready");
  $("cinemaBadge").textContent = "静帧加载中…";
  $("cinemaBadge").classList.add("show");
  S.mediaEventActive = true;
  still.onerror = () => {
    S.errors += 1;
    degradeEvent("still", "静帧加载失败");
  };
  still.onload = () => {
    if (!S.mediaEventActive) return;
    cinema.classList.add("still-ready");
    $("cinemaBadge").textContent = badge || "";
    setTimeout(() => $("cinemaBadge").classList.remove("show"), 1200);
    clearTimeout(showStill._t);
    showStill._t = setTimeout(() => {
      if (S.mediaEventActive) hideCinema();
    }, Number(ms) || 4200);
  };
  still.src = src;
}

function triggerEvent(kind) {
  if (!kind || kind === "none") return;
  const badge = LABEL.event[kind] || kind;
  if (S.eventFail) {
    S.errors += 1;
    degradeEvent(kind, "注入故障：演出素材不可用");
    return;
  }
  if (kind === "thunder") {
    // 闪电本身就是"明显视觉事件"：补光 + 滚雷，不需要占用整块演出层
    doFlash();
    rumble(1.0, 1.6);
    chip(badge);
    return;
  }
  if (kind === "phone_call") {
    /* v14 的视觉事件：她把手机贴到耳边打电话。
       贴片由 directive.action=phone_call 负责换（见 showSprite），这里只做
       「世界安静了一瞬」——压低环境音 + 手机屏的一点冷光 + 顶部徽标。
       不占全屏演出层：打电话不是"切镜头"，是她此刻在做的事。 */
    const g = $("flash");
    if (g) {
      g.style.transition = "none";
      g.style.background =
        "radial-gradient(40% 30% at 52% 48%, rgba(207,228,255,.20), transparent 70%)";
      g.classList.remove("on");
      void g.offsetWidth;
      g.classList.add("on");
      g.style.transition = "";
      setTimeout(() => { g.style.background = ""; g.classList.remove("on"); }, 900);
    }
    ambDuck(true);
    setTimeout(() => ambDuck(false), 2600);
    chip(badge);
    return;
  }
  if (kind === "ending") return;   // 结局由 handleEnding 单独接管
  // 其余事件：有静帧就走静帧，没有就走最简演出
  const src = (ASSETS.still && ASSETS.still[kind]) || null;
  showStill(src, badge, 4600);
}

function degradeEvent(kind, reason) {
  toast(`${reason}：改为最简演出`);
  if (kind === "thunder") {
    doFlash();
    hideCinema();
    return;
  }
  // 没有图也不能黑屏：只留暗角 + 徽标，2.6s 后自动收
  const cinema = $("cinema");
  cinema.classList.add("show", "fallback");
  $("cinemaBadge").textContent = LABEL.event[kind] || kind;
  $("cinemaBadge").classList.add("show");
  S.mediaEventActive = true;
  clearTimeout(degradeEvent._t);
  degradeEvent._t = setTimeout(() => {
    if (S.mediaEventActive) hideCinema();
  }, 2600);
}

/* ================================================================ 表演层
 * 用户反馈「互动过于人机 / 神态动作反馈不到位」。根因不是模型，是**时序**：
 *   1) 旧路径是「收到 directive 就换脸换动作」——她在开口之前就把表情换完、动作演完了，
 *      于是画面永远比声音早一步，看起来像幻灯片；
 *   2) 换表情不分场合，全局 380ms 淡入；动作没有"落在句间停顿"的概念；
 *   3) 待机只有 0.25%/7.5s 的 CSS 呼吸（肉眼不可见），一说话身体就死了。
 * 现在：服务端下发带锚点的 beat（格式见 docs/PERF_CONTRACT.md），客户端按**音频播放
 * 时钟**执行；rAF 驱动呼吸/重心/说话起伏，AnalyserNode 让头和声音同拍。
 * 收不到 perf 事件时自动退回旧路径，老行为不会丢。
 */

const PERF = {
  active: false,     // 本轮是否由 beat 驱动表现（false = 旧的 directive 路径）
  seq: 0,
  rev: 0,
  holdMs: 0,
  t0: 0,             // turn.start 的 performance.now()
  beats: [],         // 待触发：[{beat, abs, fired}]
  sent: {},          // audio.index -> {start, dur}
  log: [],           // 触发记录，QA 断言用
  timer: 0,
  gazeTimer: 0,
};

function perfReset(seq) {
  clearTimeout(PERF.timer);
  clearTimeout(PERF.gazeTimer);
  PERF.active = false;
  PERF.seq = seq || 0;
  PERF.rev = 0;
  PERF.holdMs = 0;
  PERF.t0 = performance.now();
  PERF.beats = [];
  PERF.sent = {};
  PERF.log = [];
}

function perfResolve(beat) {
  const at = String(beat.at || "turn.start");
  const off = Number(beat.off) || 0;
  if (at === "turn.start") return PERF.t0 + off;
  const m = /^s(\d+)\.(start|end)$/.exec(at);
  if (!m) return null;
  const info = PERF.sent[Number(m[1])];
  if (!info) return null;
  return info.start + (m[2] === "end" ? info.dur : 0) + off;
}

/* 服务端可以分两帧下发（rev1：开口前的表演；rev2：句子切完后的手势落点）。
   rev 变大 = 取消还没触发的旧 beat，已经演过的不重播。 */
function perfAccept(ev) {
  if (!ev) return;
  PERF.active = true;
  PERF.raw = PERF.raw || [];
  PERF.raw.push({ rev: ev.rev, hold: ev.start_hold_ms, beats: ev.beats });
  const rev = Number(ev.rev) || 1;
  if (rev > PERF.rev) {
    PERF.beats = PERF.beats.filter((b) => b.fired);
    PERF.rev = rev;
  } else if (rev < PERF.rev) {
    return;
  }
  if (typeof ev.start_hold_ms === "number" && ev.start_hold_ms > 0) PERF.holdMs = ev.start_hold_ms;
  (ev.beats || []).forEach((b) => {
    PERF.beats.push({ beat: b, abs: perfResolve(b), fired: false, rev });
  });
  perfTick();
}

function perfSentStart(index, dur) {
  if (index === undefined || index === null) return;
  PERF.sent[index] = { start: performance.now(), dur: dur || 0 };
  PERF.beats.forEach((b) => {
    if (!b.fired && b.abs === null) b.abs = perfResolve(b.beat);
  });
  perfTick();
}

function perfTick() {
  const now = performance.now();
  // 锚点兜底：如果 beat 指向的那一句一直没播（TTS 失败、切句结果和本地不一致），
  // 不能让它永远待命。条件收紧成「队列空了、本轮也结束了」才兜，否则第 2 句还在路上
  // 就把动作提前演掉，反而更假。
  let maxIdx = -1;
  let lastInfo = null;
  Object.keys(PERF.sent).forEach((k) => {
    const i = Number(k);
    if (i > maxIdx) { maxIdx = i; lastInfo = PERF.sent[k]; }
  });
  if (lastInfo && S.audioDone && !S.playing && !S.queue.length && now - lastInfo.start > 800) {
    PERF.beats.forEach((b) => {
      if (b.fired || b.abs !== null) return;
      const m = /^s(\d+)\.(start|end)$/.exec(String(b.beat.at || ""));
      if (!m || Number(m[1]) > maxIdx) {
        const off = Number(b.beat.off) || 0;
        b.abs = lastInfo.start + lastInfo.dur + off;
      }
    });
  }
  let due = PERF.beats.filter((b) => !b.fired && b.abs !== null && b.abs <= now + 3);
  if (due.length) {
    due.sort((a, b) => a.abs - b.abs);
    due.forEach((b) => {
      b.fired = true;
      PERF.log.push({ t: Math.round(now), kind: b.beat.kind, value: b.beat.value,
                      at: b.beat.at, off: b.beat.off || 0 });
      try { fireBeat(b.beat); } catch (e) { console.warn("[perf] beat 失败", b.beat, e); }
    });
    PERF.beats = PERF.beats.filter((b) => !b.fired);
  }
  clearTimeout(PERF.timer);
  const pending = PERF.beats.filter((b) => b.abs !== null).sort((a, b) => a.abs - b.abs);
  // 还有没解析出锚点的 beat（那一句还没开始播）→ 250ms 轮询一次就够
  const wait = pending.length ? Math.max(6, Math.min(250, pending[0].abs - performance.now())) : 250;
  PERF.timer = setTimeout(perfTick, wait);
}

function fireBeat(b) {
  const kind = b.kind;
  const v = b.value;
  // 任何新 beat 都要撤掉"视线回位"的定时器：否则 gaze 的 1.5s 回位定时器会在
  // 后面的 gesture 演到一半时把姿态抢回去（实测会看到"她刚前倾又被打回原样"）。
  if (kind !== "gaze") clearTimeout(PERF.gazeTimer);
  if (kind === "expression") {
    applyEmotion(v);
    MOTION.settleUntil = performance.now() + 180;
  } else if (kind === "gesture") {
    applyAction(v, Number(b.hold) || 2400);
  } else if (kind === "gaze") {
    clearTimeout(PERF.gazeTimer);
    const hold = Number(b.hold) || 1600;
    /* 视线也占 gaze 通道：记一笔，后面要抬手的动作才知道"她这会儿正看着窗外" */
    CHAN.claims.gaze = { action: "gaze:" + v, until: Date.now() + hold };
    /* 视线也是换姿势，同样走帧路径（以前这里是硬切：她"看一眼窗外"是跳过去的） */
    if (v === "window") { if (!SEQ.on) poseTo("look_window", { prio: PRIO.gaze, hold: hold }); }
    else if (v === "away") { if (!SEQ.on) poseTo("thinking", { prio: PRIO.gaze, hold: hold }); }
    else paint(ASSETS.char[S.emotion] || ASSETS.char.neutral, PRIO.base);
    if (v !== "user") {
      PERF.gazeTimer = setTimeout(() => {
        paint(ASSETS.char[S.emotion] || ASSETS.char.neutral, PRIO.base);
      }, hold);
    }
  } else if (kind === "breath") {
    MOTION.breathUntil = performance.now() + (v === "sharp" ? 300 : 560);
    MOTION.breathAmp = v === "sharp" ? 1 : 0.55;
  } else if (kind === "effect") {
    applyEffect(v === "rain_on" ? "rain" : v === "rain_off" ? "none" : v);
  }
}

/* 服务端只发了 directive（旧协议 / 降级）时，延迟 90ms 再按老路子执行：
   这 90ms 里如果来了 pacing/perf，就让 beat 接管，避免"先硬换一次脸再叠化"
   出现两次表情跳变。 */
function deferDirective(d, fixes) {
  S.directive = d;
  clearTimeout(S._deferT);
  S._deferT = setTimeout(() => {
    if (PERF.active) return;
    applyDirective(d, fixes || []);
  }, 90);
}

/* ------------------------------------------------------------ 持续微动
 * 为什么不用纯 CSS keyframes：呼吸/重心要跟**状态**和**声音**耦合。
 * 说话时的起伏必须来自真实音频包络（AnalyserNode），否则头部动作和声音各走各的，
 * 反而更像假人。这里统一由 rAF 写一个 transform，避免多个动画互相覆盖。
 *
 * 2026-10-20 用户反馈「抖动有问题，不是正常呼吸那种抖」。用 tools/qa/motion_probe.py
 * 量了三个独立的毛病，都不是"幅度调小一点"能解决的：
 *   1) 拿整层 translateY 当呼吸。她坐在桌前，屁股、衣摆、手里那只杯子**不该动**。
 *      实测 idle 时角色底边也跟着上下挪 5.6px、左右漂 4.1px（`bottom_line` / `center_x`），
 *      人相对背景在漂 —— 观感是"人在抖"，不是"人在呼吸"。
 *   2) 说话时 y 直接跟音频 RMS 走（0.95%≈8px）。6 秒里 118 次单帧跳变、>2.5Hz 能量占 18%：
 *      身体在按音节抽动。
 *   3) 换表情时的 `impulse*k*k` 在 t=0 处 k=1，等于**瞬间位移 10.5px** 再慢慢回来（抽一下）。
 * 现在的做法：
 *   - 呼吸只剩一个数：**以底边为轴的 scaleY**（transform-origin: 50% 100%，见 style.css）。
 *     头顶抬、底边钉死 → 接触线不动，读到的是"胸口起伏"。
 *   - 相位是两个不同周期正弦的叠加（4.35s / 6.9s），不是节拍器。
 *   - 说话包络先过 180ms 低通、系数压到呼吸的 1/3：嘴动，身子不倒。
 *   - 情绪冲量改成 220ms 起 + 600ms 落，幅度 0.9px（原来 10.5px 且无起手）。
 *   - 状态切换只改"呼吸深度"倍数，深度本身连续变化，没有任何硬跳。
 */
/* 取景常量：人物画布占高 80%（v7/v8），铺满 480×960 舞台等于把脸怼到镜头前。
   0.90 缩放（origin 在 50% 100%，底边钉死）→ 看起来"镜头退后一步"，
   而且素材底边那 90px 的渐隐仍然停在屏幕底边上，不会把"切边"露出来（曾经试过
   再 translateY(-4%)，结果贴片底边离屏底 38px，一条硬边直接看得见）。
   v9 素材按 70% 身高交付后改回 1.0。 */
const CHAR_FIT = 0.84;
const CHAR_LIFT = 0;

const MOTION = {
  t0: performance.now(),
  amp: 0,             // 音频包络原始值（ampLoop 写 / 测试注入）
  env: 0,             // 低通后的包络：真正驱动身体的是这个
  level: 0,           // 用户麦克风电平 0..1
  levelSm: 0,
  depth: 1,           // 当前呼吸深度（向状态目标平滑）
  settleUntil: 0,     // 兼容保留：换表情的回弹已经并进 impulse
  breathUntil: 0,
  breathAmp: 0,
  nextShift: 0,
  shiftUntil: 0,
  impulse: 0,         // 情绪冲击（惊讶上抬/微笑下沉），见 EMO_IMPULSE
  impulseStart: -1e9,
  raf: 0,
  reduced: false,
};

/* 表情切换的"体感放大器"：
   素材已经补强（2026-10-20：smile / listen 重出，见 docs/ASSETS_REVIEW.md §9），
   但"换脸"本身只有 160ms 的叠化，缺一个"身体也接收到了"的信号。
   所以情绪变化同时给一个很短的躯干冲量：惊讶往后靠一点（上抬）、微笑往前松一点（下沉）。
   单位是"千分之一画面高"，乘 0.9e-3 → 1 单位 ≈ 0.76px。
   注意：这是**起手 220ms 的缓动**，不是瞬间位移（旧版 impulse*k*k 在 k=1 处直接落地，
   实测一次换表情就是 10.5px 的瞬移，那才是用户说的"抖"）。 */
const EMO_IMPULSE = { surprised: +1.15, smile: -0.85, worried: +0.60, thinking: +0.30, listen: -0.20 };

function emoImpulse(emotion) {
  const v = EMO_IMPULSE[emotion];
  if (!v) return;
  MOTION.impulse = v;
  MOTION.impulseStart = performance.now();
}

function motionEnabled() {
  if (S.motionOff) return false;
  return true;
}

/* 系统开了「减少动态效果」时**不关掉呼吸**：旧版直接 return，整层一动不动，
   用户 2026-10-20 第 6 条「人物就是静止的」有一半可能是这么来的。
   现在压到 45%，只保留胸口起伏，去掉横摆与换重心。 */
function motionAmplitude() {
  if (MOTION.prefReduced === undefined) {
    MOTION.prefReduced = !!(window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }
  return MOTION.prefReduced ? 0.45 : 1;
}

function motionTick() {
  MOTION.raf = requestAnimationFrame(motionTick);
  if (!motionEnabled()) return;
  const layer = document.querySelector(".char-layer");
  if (!layer) return;
  const now = performance.now();
  const dt = Math.min(0.05, Math.max(0.004, (now - (motionTick._last || now - 16)) / 1000));
  motionTick._last = now;
  const t = (now - MOTION.t0) / 1000;
  const st = S.state;

  // ---- 包络先低通：音节级的 RMS 起伏不能直接进身体（旧版就是 1:1 灌进去的）
  const kEnv = 1 - Math.exp(-dt / 0.18);
  MOTION.env += (MOTION.amp - MOTION.env) * kEnv;
  const kLvl = 1 - Math.exp(-dt / 0.20);
  MOTION.levelSm += (MOTION.level - MOTION.levelSm) * kLvl;

  // ---- 呼吸深度：状态之间只改倍数，倍数本身连续 → 不会有任何硬跳
  const depthTarget = st === "thinking" ? 0.55 : st === "listening" ? 0.90 : 1.0;
  MOTION.depth += (depthTarget - MOTION.depth) * (1 - Math.exp(-dt / 0.55));

  // ---- 呼吸相位：4.35s 主周期 + 6.9s 副周期（两个不正交的周期叠起来不像节拍器）
  const br = Math.sin((t / 4.35) * Math.PI * 2) * 0.75 +
             Math.sin((t / 6.9) * Math.PI * 2 + 1.7) * 0.25;
  // sy 是"以底边为轴的纵向拉伸量"，不是位移：底边一个像素都不动（见文件头注释）。
  // 振幅要按「人物在屏幕上的高度」算，不能按「容器高度」算：
  // 素材里人物只占画布高约 40%（bbox 609/1536），拿容器幅 0.0040 折算到头顶只有 ~2px，
  // 再被 object-fit:cover 一缩，肉眼等于静止——这就是用户说的「呼吸感没有了，像一张图片」。
  // 0.0082 × 视口高 → 头顶行程约 4.5~5px、胸口约 2px：看得见在呼吸，但不至于「在抖」。
  // 0.72 的直流偏置：原来 (0.5 + 0.5*br) 会周期性压到 0，读起来是「动一下然后突然静止」。
  // 0.018 × 视口高 ≈ 头顶 14~17px 的行程。用户 2026-10-20 第 6 条「没有呼吸动画、
  // 人物静止」：旧值 0.0082 折算到屏幕只有 4px 峰峰，再乘 0.90 的取景缩放就基本看不见。
  // 现在 0.018 + 横摆 0.22%，并且「减少动态效果」下保留 45%（见 motionAmplitude）。
  const amp = motionAmplitude();
  let sy = BREATH.amp * amp * MOTION.depth * (0.72 + 0.28 * br);

  // ---- 说话：嘴在动，身子只带一点点（低通后），最多 ≈0.9px
  sy += 0.0011 * MOTION.env;

  // ---- 倾听：对方说话时胸口略收（不用位移，底边不能动）
  sy -= 0.0007 * MOTION.levelSm;

  // ---- 换重心：每 8–20s 一次"换坐姿"，头顶沉下去约 0.5px 再回来（1400ms，不是 760ms）
  if (!MOTION.nextShift) MOTION.nextShift = now + 7000 + Math.random() * 9000;
  if (now > MOTION.nextShift) {
    MOTION.shiftUntil = now + 1400;
    MOTION.nextShift = now + 9000 + Math.random() * 13000;
  }
  if (now < MOTION.shiftUntil) {
    const p = 1 - (MOTION.shiftUntil - now) / 1400;
    sy -= 0.0006 * Math.sin(Math.PI * p);
  }

  // ---- 吸气（beat: breath）：肩线补一次，300–620ms 上去再下来
  if (now < MOTION.breathUntil) {
    const dur = MOTION.breathAmp > 0.8 ? 300 : 620;
    const p = Math.max(0, Math.min(1, 1 - (MOTION.breathUntil - now) / dur));
    sy += 0.0016 * MOTION.breathAmp * Math.sin(Math.PI * p);
  }

  // ---- 情绪冲量：220ms 起手 + 600ms 回落（旧的 k*k 在 t=0 处就是 1，等于瞬移）
  if (MOTION.impulse) {
    const ATTACK = 220, DUR = 820;
    const age = now - MOTION.impulseStart;
    if (age >= DUR) {
      MOTION.impulse = 0;
    } else {
      const e = Math.min(1, Math.max(0, age / ATTACK)) *
                Math.min(1, Math.max(0, (DUR - age) / (DUR - ATTACK)));
      sy += MOTION.impulse * 0.0009 * e;
    }
  }

  /* ---- v9.1：呼吸回到"同一张图的连续缩放"，所以下面两条都恢复
     （v9 为了让 6 帧换图接管动效，把这条路径关成了 sy=0 / sway=0）。
     实测：连续缩放每帧的位移 ≈0.03 设备像素，是单调的"起伏"；
     而 6 帧换图每 0.42s 让躯干里的线稿整条挪 3px，那才是"硬切换 + 重影"。 */

  /* 兜底看门狗（每 500ms 一次，几乎零成本）：**画面里永远不许没有人**。
     两条 A/B 层都没有 is-front 时，整张角色层就是透明的 —— walk 截图里那几张
     只有背景的图就是这么来的。这里直接按当前情绪补一张，不依赖任何一次过渡的收尾。 */
  if (!motionTick._wd || now - motionTick._wd > 500) {
    motionTick._wd = now;
    const a = $("charA");
    const b = $("charB");
    if (a && b && !a.classList.contains("is-front") && !b.classList.contains("is-front")) {
      /* 这里必须**绕过优先级**：paint() 的持有期会让低优先级请求被丢掉，而"画面里没人"
         是唯一不能让的状态（宁可打断一次动作，也不能整张角色层透明）。所以先清掉
         幂等/持有状态，再以动作档强画一张。 */
      SPRITE.now = "";
      HOLD.prio = -1;
      HOLD.until = 0;
      SWAP.pending = null;
      clearTimeout(SWAP.timer);
      paint(ASSETS.char[S.emotion] || ASSETS.char.neutral, PRIO.pose);
    }
  }

  if (!(sy >= 0)) sy = 0;
  /* 取景（用户 2026-10-20 第 9 条「角色不要离屏幕那么近」）：
     素材画布里人物占高 80%（bbox 1230/1536），铺满 480×960 的舞台就等于把她怼到镜头前。
     这里把角色层整体缩到 0.90 再上移 4%（origin 是 50% 100%，所以底边——也就是她坐的
     桌沿——一个像素都不动），看起来是「镜头退后一步」，不是「把人裁掉」。
     横向再叠一个 0.15% / 7.3s 的慢摆，让"呼吸"不只是纵向收缩。
     换成 v9 素材（人物只占 70%）之后把 CHAR_FIT 改回 1.0 即可。 */
  /* 横向慢摆继续归零（2026-10-20 实测：它是整层平移，量化到 1 设备像素后
     每次挪一格整张图，shimmer_map 量到 4 次单帧跳变 peak 3.9~4.3，同一次运行
     峰值可复现 —— 那是肉眼可见的"抖"，不是呼吸。纵向缩放已经够表达呼吸了。 */

  /* ---- 归一化 transform（2026-10-20 第三次反馈后重定）
     上一版为了躲"重采样微闪"把 sy 量化到 1 设备像素一档，结果换来的是**台阶**：
     每跨一格整张位图换一套抗锯齿花纹，实测 3.5 次/秒。两个极端都不对。
     现在的判断依据是渲染像素实测（tools/qa/breath_ab.py）：
       - 量化（1 设备像素一跳）：整层位移 1px × 3.5 次/秒  → 看得见的"抖"
       - 不量化 + 连续缩放：每帧位移 ≈0.03 设备像素，单调 → 读成"起伏"
     所以这里**不再量化**，只做字符串去重（同一 transform 不重复写，避免样式失效）。
     呼吸帧换图那条路（每帧 3px、双影）已经下线，见 BREATH 的注释。 */
  const lay = layerSize(layer);
  const dpr = window.devicePixelRatio || 1;
  const liftPx = Math.round((CHAR_LIFT / 100) * lay.h * dpr) / dpr;
  const tx = `translate3d(0px, ${liftPx}px, 0) ` +
    `scale(${CHAR_FIT}, ${(CHAR_FIT * (1 + sy)).toFixed(6)})`;
  if (tx === motionTick._tx) return;
  motionTick._tx = tx;
  layer.style.transform = tx;
}

/* 角色层的布局尺寸（缓存：每帧读 clientWidth 会强制同步布局） */
function layerSize(layer) {
  if (!motionTick._size || motionTick._sizeW0 !== layer.offsetWidth) {
    motionTick._size = { w: layer.offsetWidth || 480, h: layer.offsetHeight || 960 };
    motionTick._sizeW0 = layer.offsetWidth;
  }
  return motionTick._size;
}

/* 说话起伏：取正在播的声音的 RMS。两条播放路径（流式 PCM / 整句 mp3）共用
   同一个 AudioContext 和 Analyser，所以切换路径不会让"头肩起伏"断掉。 */
const AMP = { ctx: null, an: null, buf: null, src: null, raf: 0 };

function ampAttach(audio) {
  try {
    const ctx = audioCtx();
    if (!ctx || !AMP.an) return;
    if (AMP.src) { try { AMP.src.disconnect(); } catch (e) {} AMP.src = null; }
    AMP.src = ctx.createMediaElementSource(audio);
    AMP.src.connect(AMP.an);   // 到 analyser 再到 destination（见 audioCtx）
  } catch (e) {
    /* 已路由过 / 上下文被挂起：静态起伏也能看，不影响主流程 */
  }
}

function ampLoop() {
  if (!AMP.an) return;
  cancelAnimationFrame(AMP.raf);
  const step = () => {
    AMP.raf = requestAnimationFrame(step);
    if (!AMP.an) return;
    AMP.an.getByteTimeDomainData(AMP.buf);
    let sum = 0;
    for (let i = 0; i < AMP.buf.length; i += 4) {
      const v = (AMP.buf[i] - 128) / 128;
      sum += v * v;
    }
    const rms = Math.sqrt(sum / (AMP.buf.length / 4));
    // 语音 RMS 大约 0.02–0.25：做一次开方+截断，得到 0..1 的"用力程度"
    const target = Math.max(0, Math.min(1, Math.sqrt(rms) * 1.5 - 0.08));
    MOTION.amp = MOTION.amp * 0.72 + target * 0.28;
  };
  step();
}

function ampDetach() {
  // 只断开"整句 mp3"那条线；Analyser 本身是共享的（流式播放还要用），不要置空
  MOTION.amp = 0;
  if (AMP.src) { try { AMP.src.disconnect(); } catch (e) {} AMP.src = null; }
}

/* ------------------------------------------------------------ 沉默搭话
   真人不会在对面沉默 12 秒时保持完全静止。她不会开口（那会打断用户），
   只是自己动一下：抿一口、看窗外。两次之间至少 20s，不弹 toast、不改状态。 */
const NUDGE = { timer: 0, last: 0 };

function scheduleNudge() {
  clearTimeout(NUDGE.timer);
  if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  NUDGE.timer = setTimeout(() => {
    const quiet = S.state === "idle" && !S.playing && !S.queue.length && !S.mediaEventActive;
    const cold = Date.now() - (NUDGE.last || 0) > 20000;
    if (quiet && cold) {
      NUDGE.last = Date.now();
      applyAction(Math.random() < 0.55 ? "sip_coffee" : "look_window", 2600, true);
    }
    scheduleNudge();
  }, 12000 + Math.random() * 4000);
}

/* ============================================================ 流式 TTS 播放器
 * 侧车（CosyVoice2）逐块吐裸 PCM：服务端同源转发，这里边收边排期。
 * 为什么不用 <audio>：裸 PCM 没有容器，浏览器不能直接播；用 AudioContext 按块
 * 建 AudioBuffer 排在同一个时间线上，块与块之间无缝，第一块到就出声。
 * 这决定了"她多久接话"——整句合成完再播要 3–6s，那是播放器，不是对话。
 */
const STREAM = {
  log: [],
  items: [],
  busy: false,
  abort: null,
  sources: [],
  endTime: 0,
  seq: 0,
  // 每句一个"到点才切字幕"的定时器（见 playStreamItem）。不能共用一格：
  // 下一句的数据 0.3s 就到，会把上一句还没到点的定时器清掉。
  capTimers: new Set(),
};

function streamReset(seq) {
  STREAM.log = [];
  STREAM.seq = seq;
  STREAM.items = [];
  if (STREAM.abort) { try { STREAM.abort.abort(); } catch (e) {} STREAM.abort = null; }
  STREAM.sources.forEach((s) => { try { s.stop(0); } catch (e) {} });
  STREAM.sources = [];
  STREAM.endTime = 0;
  /* 字幕定时器是"每句一个"，打断时必须全部撤掉，否则旧句会在打断后把字幕写回来 */
  for (const t of STREAM.capTimers || []) clearTimeout(t);
  STREAM.capTimers = new Set();
}

/* 共用一个 AudioContext + Analyser：既给"说话起伏"供包络，也避免多个上下文。 */
function audioCtx() {  if (!AMP.ctx) {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return null;
    AMP.ctx = new AC();
    if (AMP.ctx.state === "suspended") AMP.ctx.resume().catch(() => {});
  }
  if (!AMP.an) {
    AMP.an = AMP.ctx.createAnalyser();
    AMP.an.fftSize = 512;
    AMP.an.smoothingTimeConstant = 0.55;
    AMP.buf = new Uint8Array(AMP.an.fftSize);
    AMP.an.connect(AMP.ctx.destination);
    ampLoop();
  }
  return AMP.ctx;
}

/* ---- 流式 PCM 的房间混响（把"第一句"的听感拉回和后几句一致）--------------------
   整句 mp3 那条路的混响是**服务端** DSP 加的：postprocess.make_room_ir 生成 0.55s
   小房间 IR（6 个早期反射 11/17/23/29/37/47ms + 指数衰减扩散尾），finalize 里按
   情绪把湿声混到干声 RMS 的 16%–20%，再过一级 8.2kHz 低通。
   流式路径是逐块合成，服务端拿不到整句做卷积——所以在客户端用 ConvolverNode 补同一套
   IR，参数按上面的口径复刻（湿声能量归一化到干声的 0.16，干声 ×0.96）。
   为什么必须补：不补的话第一句明显比后面几句"干"、像贴着脸说话，切换点会被听出来；
   播放器本来就走 WebAudio 时间线，加这一层不增加任何等待时间。
   注意：这条 bus 只接流式 PCM；整句 mp3（自带服务端混响）不经过它，避免双重混响。 */
const ROOM = { inp: null, gain: null, dry: null, conv: null, wet: null, lp: null, ready: false };

/* 流式 PCM 的级别对齐（0.962 ≈ -0.34dB）。
   为什么需要：整句 mp3 那条路在服务端 postprocess.finalize 里被归一化到 **-20dBFS**；
   live 裸 PCM 是 edge 的原始电平（实测 RMS -19.0 ~ -22.1dBFS，逐句浮动，因为 edge 不归一化）。
   ⚠ 2026-10-21 重标（根因见 docs/AUDIO_CLIP_2026-10-21.md 的 R1/R2）：
   修前这里写的是 0.115（-18.8dB）——那是给**削波方波**做的补偿：量纲 bug 把波形压成 1-bit
   方波，RMS 被抬高 18.0dB，0.115 正好把响度还回正常，所以"听着音量没问题、但声音是炸的"。
   量纲修好后同一段音频小了 18dB，沿用 0.115 会让台词掉到 -38.9dBFS（几乎听不见）。
   标定方法：tools/qa/_probe_recalibrate.py 复刻本文件的 ROOM bus（×gain → dry 0.96 + 卷积湿声
   → LP 8.2k → shelf），对 6 句台词求"让 destination RMS 落在整句路径 -20.45dBFS"的增益：
   逐句 0.923–1.000、均值 0.959，取 0.962。实测 destination RMS -20.11 ~ -20.82dBFS。 */
const ROOM_GAIN = 0.962;

/* 流式 PCM 的**音色**对齐（-1.9dB @ 3kHz 高架）。
   ⚠ 2026-10-21 重标：修前是 -10dB，那是照着**削波产物**标的——1-bit 方波的奇次谐波把
   5kHz 以上抬高 34–35dB（实测 3–5k +31.1、5–7k +35.3、7–9k +34.9、9–11k +34.5dB，
   docs/AUDIO_CLIP_2026-10-21.md §2.4），-10dB 只是在压失真，压不平也没有意义。
   量纲修好后重新做 1/3 倍频程标定（tools/qa/_probe_recalibrate.py，同文本同音色 6 句，
   250Hz–10kHz）：shelf=0 时两条路的残差只剩 +3.7dB 以内，取 -1.9dB 后
   3–10kHz 残差 +1.87 ~ -1.72dB、全带 +2.15 ~ -1.43dB（验收标准 ±3dB）。
   另一个方向（把 DSP 那条路提亮）会改动所有句子的音色，属于"音色归用户判断"，默认不动。 */
const ROOM_SHELF = { f: 3000, gainDb: -1.9 };

/* 按 postprocess.make_room_ir 的口径生成 IR，并按能量归一化到指定湿声比例。 */
function makeRoomIR(ctx, wet) {
  const sr = ctx.sampleRate;
  const n = Math.max(1, Math.round(sr * 0.55));
  const buf = ctx.createBuffer(1, n, sr);
  const ch = buf.getChannelData(0);
  for (const [ms, g] of [[11, 0.26], [17, 0.20], [23, 0.15], [29, 0.11], [37, 0.08], [47, 0.05]]) {
    const i = Math.round((sr * ms) / 1000);
    if (i < n) ch[i] += g;
  }
  // 扩散尾：白噪声 × 指数衰减，再做一阶低通（≈3.2kHz）去掉"嘶"的高频
  const a = Math.exp((-2 * Math.PI * 3200) / sr);
  let lp = 0;
  for (let i = 0; i < n; i++) {
    const t = i / sr;
    lp = (1 - a) * (Math.random() * 2 - 1) + a * lp;
    ch[i] += lp * 0.30 * Math.exp(-t / 0.17);
  }
  // 能量归一：卷积输出的 RMS ≈ 输入 RMS × ||ir||₂，把 ||ir||₂ 归到 wet
  let e = 0;
  for (let i = 0; i < n; i++) e += ch[i] * ch[i];
  e = Math.sqrt(e) || 1;
  const k = wet / e;
  for (let i = 0; i < n; i++) ch[i] *= k;
  return buf;
}

function roomBus() {
  const ctx = audioCtx();
  if (!ctx) return null;
  if (ROOM.ready) return ROOM.inp;
  try {
    ROOM.inp = ctx.createGain();
    ROOM.gain = ctx.createGain();
    ROOM.dry = ctx.createGain();
    ROOM.wet = ctx.createGain();
    ROOM.lp = ctx.createBiquadFilter();
    ROOM.shelf = ctx.createBiquadFilter();
    ROOM.conv = ctx.createConvolver();
    ROOM.gain.gain.value = ROOM_GAIN;        // 与整句 mp3 的 -20dBFS 对齐
    ROOM.dry.gain.value = 1 - 0.25 * 0.16;   // 与 finalize 的 x*(1-0.25*wet) 同口径
    ROOM.wet.gain.value = 1.0;
    ROOM.lp.type = "lowpass";
    ROOM.lp.frequency.value = 8200;          // finalize 的 high-frequency dulling
    ROOM.shelf.type = "highshelf";
    ROOM.shelf.frequency.value = ROOM_SHELF.f;
    ROOM.shelf.gain.value = ROOM_SHELF.gainDb;   // 与整句 mp3 同谱（见 ROOM_SHELF 注释）
    ROOM.conv.normalize = false;             // IR 已经按能量归一，别让浏览器再缩一次
    ROOM.conv.buffer = makeRoomIR(ctx, 0.16);
    ROOM.inp.connect(ROOM.gain);
    ROOM.gain.connect(ROOM.dry);
    ROOM.gain.connect(ROOM.conv);
    ROOM.conv.connect(ROOM.wet);
    ROOM.dry.connect(ROOM.lp);
    ROOM.wet.connect(ROOM.lp);
    ROOM.lp.connect(ROOM.shelf);
    ROOM.shelf.connect(AMP.an);
    ROOM.ready = true;
  } catch (e) {
    /* 建不起来就退回直连（听感差点，但别哑） */
    ROOM.ready = false;
    return null;
  }
  return ROOM.inp;
}

function enqueueStream(url, text, index, estMs) {
  STREAM.items.push({ url, text, index, estMs });
  streamPump();
}

async function streamPump() {
  if (STREAM.busy) return;
  STREAM.busy = true;
  const mySeq = STREAM.seq;
  try {
    while (STREAM.items.length) {
      if (STREAM.seq !== mySeq) break;
      const item = STREAM.items.shift();
      let played = false;
      try {
        played = await playStreamItem(item);
      } catch (e) {
        console.warn("[tts] 流式失败，改整句 mp3", e);
        played = false;
      }
      if (!played) await streamFallbackMp3(item);
      if (STREAM.seq !== mySeq) break;
    }
  } finally {
    STREAM.busy = false;
    // 等时间线上最后一块播完再回待机（不能 sleep 固定值，句长差别很大）
    const ctx = AMP.ctx;
    if (ctx) {
      const wait = (STREAM.endTime - ctx.currentTime) * 1000;
      if (wait > 0) await new Promise((r) => setTimeout(r, wait + 60));
    }
    if (!STREAM.items.length && S.audioDone && !S.playing) setState("idle");
  }
}

/* 收一块 → 转 Int16 → 排到时间线上。返回是否真的播出了东西。 */
async function playStreamItem(item) {
  const ctx = audioCtx();
  if (!ctx) return false;
  const mySeq = STREAM.seq;   // 排期途中被打断时，字幕定时器要知道自己已经过期
  if (ctx.state === "suspended") await ctx.resume().catch(() => {});
  const t0 = performance.now();
  let res;
  // 冷启动/侧车挂了时不要干等：2.5s 内一个字节都没来，就退回整句 mp3。
  // 这个超时只约束"首块"，之后的块按流式节奏来。
  const ctl = new AbortController();
  STREAM.abort = ctl;
  const killer = setTimeout(() => { try { ctl.abort(); } catch (e) {} }, 2000);
  try {
    res = await fetch(item.url, { cache: "no-store", signal: ctl.signal });
  } catch (e) {
    clearTimeout(killer);
    return false;
  }
  if (!res.ok || !res.body) { clearTimeout(killer); return false; }
  const rate = Number(res.headers.get("X-Sample-Rate")) || 24000;
  /* 反应时间：从"用户说完"算起，扣掉已经花掉的合成/网络时间。
     这里不阻塞 fetch（先让 PCM 流起来），只是把第一块**排期**往后推，
     所以等待期间数据已经在缓冲，不会多花时间。 */
  const elapsed = performance.now() - (S.turnSentAt || performance.now());
  const holdMs = S.holdPending ? Math.max(0, PERF.holdMs - elapsed) : 0;
  S.holdPending = false;
  STREAM.abort = new AbortController();
  const reader = res.body.getReader();
  let carry = null;
  let rendered = 0;
  let firstChunkAt = 0;
  STREAM.endTime = Math.max(ctx.currentTime + 0.08, STREAM.endTime);
  if (holdMs > 0) STREAM.endTime = Math.max(STREAM.endTime, ctx.currentTime + holdMs / 1000);
  const startAt = STREAM.endTime;

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (!value || !value.byteLength) continue;
      if (!firstChunkAt) {
        firstChunkAt = performance.now();
        clearTimeout(killer);
        S.firstChunkMs = Math.round(firstChunkAt - t0);
        /* 2026-10-21 用户反馈「明明话还没说到那里，字幕已经放完了」：
           以前是**数据一到**就切字幕，可这一句的音频还要排在 STREAM.endTime 上等
           上一句播完 / 等反应时间 / 等句间停顿 —— 数据只要 0.3s 就收全了，声音却
           还有好几秒才出来，字幕自然跑在声音前面。
           现在字幕和「说话」状态都跟着**排期时刻**走：at0 是哪一刻开口，字幕就是
           哪一刻换；数据到达时刻只用来统计首包延迟。 */
        const at0 = Math.max(startAt, ctx.currentTime + 0.03);
        const leadMs = Math.max(0, Math.round((at0 - ctx.currentTime) * 1000));
        /* 每句一个定时器，**不能共用**一个：下一句的数据很快就到（0.3s），
           若共用 STREAM.capT，它会在上一句的"开口时刻"还没到时就把它 clear 掉，
           上一句字幕就永远不出现（实测：字幕跳到第 2 句，看起来像"少了一整句"）。 */
        const capTimer = setTimeout(() => {
          STREAM.capTimers.delete(capTimer);
          if (mySeq !== STREAM.seq) return;
          setState("speaking");
          setCaption(item.text, false);
          // 表演层锚点也钉在真实开口时刻（s{i}.start / s{i}.end 才有意义）
          perfSentStart(item.index, item.estMs || 0);
        }, leadMs);
        STREAM.capTimers.add(capTimer);
      }
      let bytes = value;
      if (carry) {
        const joined = new Uint8Array(carry.length + bytes.length);
        joined.set(carry, 0);
        joined.set(bytes, carry.length);
        bytes = joined;
        carry = null;
      }
      const n = Math.floor(bytes.length / 2);
      if (n * 2 < bytes.length) carry = bytes.subarray(n * 2);
      if (!n) continue;
      const pcm = new Int16Array(n);
      const dv = new DataView(bytes.buffer, bytes.byteOffset, n * 2);
      for (let i = 0; i < n; i++) pcm[i] = dv.getInt16(i * 2, true);
      const buf = ctx.createBuffer(1, n, rate);
      const ch = buf.getChannelData(0);
      for (let i = 0; i < n; i++) ch[i] = pcm[i] / 32768;
      const src = ctx.createBufferSource();
      src.buffer = buf;
      /* 2026-10-21 13:4x **直连 analyser（全干）**：这条 bus 原本给流式 PCM 补
         0.55s 小房间 + 8.2k 低通 + -1.9dB 高架，是为了和服务器 mp3 那条路的音色对齐。
         用户听完 B 档反馈「有回音」——源就是它，流式路径每一句都过一遍卷积混响。
         现在两条路都全干（服务器 finalize 也关掉了），所以 bus 不再需要；
         roomBus()/makeRoomIR 保留，想加回房间把下面这行改回 `src.connect(roomBus() || AMP.an)`。 */
      src.connect(AMP.an);
      const at = Math.max(STREAM.endTime, ctx.currentTime + 0.03);
      src.start(at);
      STREAM.sources.push(src);
      STREAM.log.push({ t: Math.round(performance.now()), at: +at.toFixed(3),
                        dur: +(n / rate).toFixed(3), idx: item.index });
      STREAM.endTime = at + n / rate;
      rendered += n / rate;
    }
  } finally {
    clearTimeout(killer);
    try { reader.cancel(); } catch (e) {}
  }
  if (!rendered) return false;
  // 侧车中途死掉时流会"正常结束"但只吐出半句：这时不能当作播放成功，
  // 否则这一句会被吞掉。低于预估时长 45% 就认定失败，退回整句 mp3 兜底。
  const est = Number(item.estMs) || 0;
  if (est > 900 && rendered * 1000 < est * 0.45) {
    /* 2026-10-21 13:4x：这里原来 `return false` → streamFallbackMp3 把**同一句**整段
       再播一遍。已经排进时间线的那半句不会被撤销，于是同一句话叠两次——这就是用户听到的
       「回音」的第二个来源（第一个是上面的房间 bus）。现在半句就半句：宁可这句短一截，
       也不能一句话说两遍。真正的补救在服务端（live_edge 空音频重试 + 合成前不吐字节）。 */
    console.warn(`[tts] 流只到 ${(rendered * 1000) | 0}ms / 估 ${est}ms，保留半句（不再重播整句）`);
  }
  // 真实时长回填：服务端估的时长只用来排 beat，收到真声音后要校正
  if (PERF.sent[item.index]) PERF.sent[item.index].dur = rendered * 1000;
  /* 句间停顿按**时间线**预留，而不是 await sleep：
     这样下一句的首包生成（CosyVoice2 首块约 1.4s）可以和这一句的播放并行，
     句与句之间不会出现"等首包"的空白。这是流式播放能连成一段的关键。 */
  STREAM.endTime += Math.max(0, gapAfter(item.text)) / 1000;
  return true;
}

/* 流式崩了就退回整句 mp3（同一条 TTSProvider，永远有声音） */
async function streamFallbackMp3(item) {
  try {
    const q = new URLSearchParams({ text: item.text, emotion: S.emotion || "neutral",
                                    position: item.index === 0 ? "first" : "follow",
                                    index: String(item.index || 0) });
    const data = await (await fetch(`/api/tts_file?${q}`)).json();
    if (data && data.url) {
      S.queue.push({ url: data.url, text: item.text, index: item.index });
      if (!S.playing) playNext();
      return true;
    }
  } catch (e) {}
  setCaption(item.text, false);
  speakFallback(item.text);
  return false;
}


/* ---------------------------------------------------------------- 音频播放 */

function stopPlayback() {
  // 预取过的 Audio 要先掐断，否则打断之后它还在后台下载，挤占下一句的带宽
  for (const it of S.queue) {
    try { if (it.audio) { it.audio.pause(); it.audio.src = ""; } } catch (e) {}
  }
  S.queue = [];
  clearTimeout(S.gapTimer);
  S.gapTimer = 0;
  clearTimeout(S.holdTimer);
  S.holdTimer = 0;
  S.holdPending = false;
  streamReset(S.activeSeq);
  ampDetach();
  if (S.playing) {
    try {
      /* onplaying 也必须清：play() 是异步的，打断瞬间那一句的 onplaying 可能还没触发。
         不清它的话，stopPlayback() 之后它照样会跑一遍 setCaption(item.text)，
         于是屏幕上残留一句「嗯……」（用户报的就是这个）。 */
      S.playing.onplaying = null;
      S.playing.onended = null;
      S.playing.onerror = null;
      S.playing.pause();
      S.playing.currentTime = 0;
    } catch (e) {}
    S.playing = null;
  }
  clearTimeout(S.streamWaitT);
  S.streamWaitT = 0;
  if (window.speechSynthesis) window.speechSynthesis.cancel();
}

function enqueueAudio(url, text, index) {
  /* 预取：整句 mp3 以前是「上一句播完才 new Audio」，于是句与句之间要等一次
     建连 + 解码，听感就是"她说一句、卡一下"。现在入队时就把 Audio 建好开始拉，
     下一句在这一句播放期间已经缓冲完，接得上是无缝的。 */
  const item = { url, text, index };
  try {
    item.audio = new Audio(url);
    item.audio.preload = "auto";
    item.audio.crossOrigin = "anonymous";
    recRoute(item.audio);
  } catch (e) {
    item.audio = null;
  }
  S.queue.push(item);
  if (!S.playing) playNext(S.activeSeq);
}

/* 流式那条路是不是还在占着音频时间线。
   为什么需要：MIRA_TTS_LIVE=first 时第一句走流式 PCM、后面几句走整句 mp3，
   两条播放路径各有一套队列；mp3 一旦入队就立刻 play()，会和还在排期的流式 PCM
   叠在一起（听感就是两句同时在说）。让 mp3 等到流式时间线排空再起播即可。 */
function streamDraining() {
  if (STREAM.busy || STREAM.items.length) return true;
  const ctx = AMP.ctx;
  return !!(ctx && STREAM.endTime - ctx.currentTime > 0.05);
}

function playNext(seq) {
  if (seq !== undefined && seq !== S.activeSeq) return;
  const item = S.queue.shift();
  if (!item) {
    if (S.audioDone) {
      setState("idle");
      /* 不要清空字幕：她说完最后一句后字幕要留在画面上，
         否则用户会以为「刚才什么都没发生」（这也是会被误判成话筒坏了）。 */
    }
    return;
  }
  const audio = item.audio || new Audio(item.url);
  if (!item.audio) {          // 预取路径已经在入队时配过 preload/crossOrigin/recRoute
    audio.preload = "auto";
    audio.crossOrigin = "anonymous";
    recRoute(audio);
  }
  S.playing = audio;

  const begin = () => {
    if (seq !== undefined && seq !== S.activeSeq) return;
    // 流式 PCM 还在时间线上排着：等它播完再起 mp3，否则两条播放路径会叠音。
    // 上限 10s：一句最长约 8s，真卡住也不能让整轮哑掉（到点就直接播 mp3）。
    if (streamDraining() && Date.now() - (begin._t0 || (begin._t0 = Date.now())) < 10000) {
      S.streamWaitT = setTimeout(begin, 60);
      return;
    }
    audio.play().catch(() => {
      setCaption(item.text, false);
      S.playing = null;
      ampDetach();
      playNext(seq);
    });
  };

  audio.onplaying = () => {
    if (seq !== undefined && seq !== S.activeSeq) return;
    setState("speaking");
    setCaption(item.text, false);
    ampAttach(audio);   // 说话起伏跟着真实音频包络走
    // 表演层的锚点靠真实音频时长解析：s{i}.end 只有在这里才成立
    perfSentStart(item.index, Number.isFinite(audio.duration) ? audio.duration * 1000 : 0);
  };
  audio.onended = () => {
    S.playing = null;
    ampDetach();
    const gap = gapAfter(item.text);
    if (S.queue.length && gap > 0) {
      clearTimeout(S.gapTimer);
      S.gapTimer = setTimeout(() => playNext(seq), gap);
    } else {
      playNext(seq);
    }
  };
  audio.onerror = () => {
    S.playing = null;
    ampDetach();
    S.errors += 1;
    setCaption(item.text, false);
    speakFallback(item.text);
    playNext(seq);
  };
  /* 反应时间：真人不会在对方话音刚落 0ms 内接话。
     但"反应时间"应该从**用户说完**算起，而不是从音频合成好开始算——否则缓存命中时
     她秒回（假），冷启动时她又多等 400ms（更假）。所以这里扣掉已经过去的合成时间：
     想得久就少顿一点，想得快就多顿一点，总延迟稳定在 400ms 上下。 */
  const elapsed = performance.now() - (S.turnSentAt || performance.now());
  let hold = 0;
  if (S.holdPending) hold = Math.max(0, PERF.holdMs - elapsed);
  S.holdPending = false;
  if (hold > 0) {
    clearTimeout(S.holdTimer);
    S.holdTimer = setTimeout(begin, hold);
  } else {
    begin();
  }
}

function speakFallback(text) {
  if (!S.ttsFallback || !window.speechSynthesis) return;
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "zh-CN";
  u.rate = 1.02;
  window.speechSynthesis.speak(u);
}

/* ---------------------------------------------------------------- 网络 */

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error(`${path} ${res.status}`);
  return res.json();
}

async function startSession(mode) {
  const data = await api("/api/session", { mode });
  S.sid = data.session_id;
  /* 服务端会把请求的 mode 原样回显。前端只有 llama / cloud 两个 option，万一回了
     auto/mock/strata，绝不能把 select 设成一个不存在的值（select.value 会变成空串，
     之后每一轮都不知道自己在哪一档）。规则：不是这两档就一律落到「本地 Qwen3」。 */
  const KNOWN_MODES = ["llama", "cloud"];
  S.mode = KNOWN_MODES.includes(data.mode) ? data.mode : "llama";
  const modeSel = $("modeSelect");
  if (modeSel) modeSel.value = S.mode;
  syncCloudRow();
  /* 选了 cloud 但服务端还没有 key：llm_for("cloud") 会回落 auto 链，实际说话的是
     本地/自动。这一点必须当场说出来，否则用户以为自己在用 DeepSeek。 */
  if (S.mode === "cloud" && !S.cloudConfigured) toast("未填 key，已回落本地/自动");
  /* 卡包快照：素材路径 / 标签 / 句间停顿都跟着卡走（一份数据三处用） */
  try {
    applyCard(await (await fetch("/api/card")).json());
    $("gateKicker").textContent =
      `${S.card.scene.time || "23:47"} · ${S.card.scene.name || "即将打烊的咖啡馆"}`;
    if (S.card.tagline) $("gateTagline").textContent = S.card.tagline;
  } catch (e) {
    console.warn("[card] 快照加载失败，使用内置素材清单", e);
  }
  return data;
}

/* ---------------------------------------------------------------- 预热 */

/* 她开口前的 850ms 是 edge 云端的握手（TCP+TLS+WSS，实测 795–870ms 很稳定）。
   用户「按下话筒开始说」到「真的发出去」之间有几秒空白，这段时间把握手付掉最划算，
   否则这 850ms 有一半会露在「她第一句台词」上（2026-10-21 14:5x 用户报的语音延迟）。
   8s 内不重复打；失败静默——服务端 /api/turn 入口还有一次 prewarm 兜着。 */
let _warmAt = 0;
function warmTts(force) {
  const now = performance.now();
  if (!force && now - _warmAt < 8000) return;
  _warmAt = now;
  fetch("/api/warm", { method: "POST" }).catch(() => {});
}

async function sendTurn(text, kind = "user") {
  showYouSaid(text);              // 先把你这句话放上画面，再谈她的回答
  if (!S.sid) await startSession(S.mode);
  let seq = ++S.seq;
  S.activeSeq = seq;
  S.audioDone = false;
  S.errors = 0;
  S.lastFirstAudio = null;
  perfReset(seq);          // 旧轮的 beat 作废，新一轮从 turn.start 重新排期
  streamReset(seq);        // 旧轮的流式音频也要断掉，否则会盖到新一轮上
  S.holdPending = true;    // 第一句要等"反应时间"（服务端给，没给就是 0）
  clearTimeout(S._deferT);
  stopPlayback();
  hideCinema();
  clearSuggest();
  setCaption("", false);
  clearNarration();        // 新一轮开始，上一轮的旁白不该挂在新台词上方
  setState("thinking");
  S.turnSentAt = performance.now();
  if (S.abort) S.abort.abort();
  S.abort = new AbortController();

  const t0 = performance.now();
  try {
    const res = await fetch("/api/turn", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: S.sid, text, kind, mode: S.mode }),
      signal: S.abort.signal,
    });
    if (!res.ok || !res.body) throw new Error(`turn HTTP ${res.status}`);
    /* 轮次号以服务端为准：打断（interrupt）会在两侧各占一个号，任何一侧数错一次，
       整轮事件都会被下面的 seq 校验当成「迟到事件」丢掉 —— 表现就是角色听不见你说话。 */
    const srvSeq = parseInt(res.headers.get("X-Turn-Seq") || "", 10);
    if (Number.isFinite(srvSeq)) {
      S.seq = Math.max(S.seq, srvSeq);
      S.activeSeq = srvSeq;
      seq = srvSeq;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop() || "";
      for (const line of lines) {
        if (!line.trim()) continue;
        let ev;
        try {
          ev = JSON.parse(line);
        } catch (e) {
          continue;
        }
        if (ev.seq !== S.activeSeq) continue; // 迟到事件：丢弃
        handleEvent(ev, seq, t0);
      }
    }
  } catch (err) {
    if (err.name === "AbortError") return;
    S.errors += 1;
    setState("idle");
    toast(`连接失败：${err.message}。检查后端是否在运行。`);
    addMetric("网络错误", err.message);
  } finally {
    if (S.activeSeq === seq) {
      S.audioDone = true;
      // 兜底：这一轮一个事件都没驱动状态（例如 seq 对不上/服务端静默），
      // 不能让界面永远停在「思考」—— 那是用户以为「话筒坏了」的样子。
      if (S.state === "thinking") {
        setState("idle");
        toast("这一轮没有收到回复，请再说一次");
      }
    }
  }
}

function handleEvent(ev, seq, t0) {
  switch (ev.t) {
    case "state":
      if (ev.state === "idle" && (S.playing || S.queue.length)) break;
      setState(ev.state);
      break;
    case "turn_start":
      S.turns += 1;
      break;
    case "subtitle_partial":
      /* 2026-10-21：以前这里把**正在生成的整段台词**一边打字一边铺到字幕上，
         于是声音还没到，字幕已经"念"完了（用户报的「字幕跑在声音前面」就是它）。
         字幕现在只由音频播放时刻驱动（见 playStreamItem / audio.onplaying），
         这里只留最后一版文本给调试面板，不上屏。 */
      $("subtitle").classList.remove("narration");
      S.partialText = ev.text;
      break;
    case "reply":
      $("subtitle").classList.remove("narration");
      S.replyText = ev.text;      // 同上：不抢字幕（她说哪句，由音频决定）
      break;
    case "directive":
      // 剧情类（场景/特效/事件）立刻执行；表演类（表情/动作）交给 beat 排期，
      // 90ms 内没有 perf 事件则退回旧路径（见 deferDirective）。
      applyDirectiveVisual(ev.directive);
      deferDirective(ev.directive, ev.fixes || []);
      break;
    case "directive_patch":
      /* 台词二次校正（问题 6 · 方案乙，2026-10-21）：服务端读完这一轮**真实台词**后，
         把和台词对不上的情绪/动作改掉再发一次（表里要掏照片、台词却没提照片 → 改掉）。
         这里只应用 emotion/action：reaction hold、场景、特效、事件都已经演过，不重排。 */
      if (ev.directive) {
        if (ev.directive.emotion) applyEmotion(ev.directive.emotion);
        if (ev.directive.action) applyAction(ev.directive.action, 3200);
        const fx = (ev.fixes || []).join(" / ") || "台词校正";
        addMetric("校正", fx);
        console.info("[directive_patch]", ev.directive, ev.fixes);
        if (window.__miraRecord)
          window.__miraRecord.push({ type: "directive_patch", d: ev.directive, fixes: ev.fixes });
      }
      break;
    case "pacing":
      if (typeof ev.reaction_ms === "number") {
        PERF.holdMs = ev.reaction_ms;
        S.holdPending = true;
      }
      PERF.active = true;
      clearTimeout(S._deferT);
      break;
    case "perf":
      PERF.active = true;
      clearTimeout(S._deferT);
      if (ev.seq !== undefined && ev.seq !== PERF.seq) perfReset(ev.seq);
      if (ev.perf_debug) addMetric("表演", ev.perf_debug);
      perfAccept(ev);
      break;
    case "audio":
      if (!S.lastFirstAudio) {
        const ms = Math.round(performance.now() - t0);
        S.lastFirstAudio = ms;
        $("latency").textContent = `首包 ${ms}ms`;
        addMetric("首包", `${ms}ms`);
      }
      enqueueAudio(ev.url, ev.text, ev.index);
      break;
    case "audio_stream":
      // 服务端只给"去哪拿这段 PCM"：浏览器边收边放，首包 200–400ms
      if (STREAM.seq !== S.activeSeq) streamReset(S.activeSeq);
      enqueueStream(ev.url, ev.text, ev.index, ev.est_ms);
      break;
    case "audio_error":
      S.errors += 1;
      // 6500ms：这是交付时唯一要「看得出降级」的提示，3.2s 在录屏里一闪就没了
      toast(ev.message || "语音不可用，已降级为字幕", 6500);
      setCaption(ev.text, false);
      speakFallback(ev.text);
      break;
    case "subtitle":
      /* 旁白（她**没说话**时的系统叙述）：没有音频，也不是她说的话。
         P0-5：以前它复用 #subtitle，位置/字号都接近台词，观众会以为是她在说。
         现在旁白走独立的 #narration 条（见 index.html / style.css），
         台词区保持干净；旁白 2.6s 后自动收起，不打断用户继续输入。 */
      if (ev.narration) {
        showNarration(ev.narration);
      } else {
        setCaption(ev.text || "", false);
      }
      if (ev.reason) addMetric("旁白", `${ev.reason}${ev.gate !== undefined ? " " + ev.gate : ""}`);
      break;
    case "suggest":
      showSuggest(ev.items || []);
      break;
    case "ending":
      /* 故事收束（v14）。到这一步不再有下一轮：收起输入、盖结局静帧、给重开入口。 */
      handleEnding(ev);
      break;
    case "done":
      S.audioDone = true;
      S.lastTotal = ev.ms;
      S.beat = ev.beat ?? S.beat;
      /* 这一轮真正是谁说的话：card（卡包定稿）/ strata / llama / mock。
         掉到 mock 时界面必须看得见——静默降级会让一整场演示"看起来在跑模型"。 */
      if (ev.provider && ev.provider !== S.provider) {
        S.provider = ev.provider;
        addMetric("模型", ev.provider);
        /* 「界面必须看得见」不能只写进观测面板（它默认是隐藏的）：真降级到 Mock 时，
           用户的感受是「她正常回话」，没有任何理由去点观测。所以主界面也要说。
           这是唯一诚实的信号——/api/health 的 chain 是**配置**的链，不是可用性
           （llama 永远在链里，哪怕 :8080 上什么都没有）。 */
        if (ev.provider === "mock") {
          const note = document.querySelector(".gate-note");
          if (note && !note.classList.contains("mock-note")) {
            note.textContent = "Mock 模式：本地模型没起，对话按剧本推进（流程与结局完整）· 点麦克风可打断她";
            note.classList.add("mock-note");
          }
        }
      }
      $("latency").textContent =
        `首包 ${ev.first_audio_ms ?? S.lastFirstAudio ?? "-"}ms · 总 ${ev.ms}ms`;
      if (!S.playing && S.queue.length === 0 && !STREAM.items.length && !STREAM.busy)
        setState("idle");
      break;
    case "error":
      S.errors += 1;
      setState("idle");
      toast(`出错了：${ev.message}`);
      addMetric("错误", ev.message);
      break;
    case "cancelled":
      break;
  }
}

/* ---------------------------------------------------------------- 结局
 * 三个结束方式（见 docs/STORY_v15_EXPANDED.md）：
 *   friend   —— 借了电话，知道小雪来不了，你们交换了名字
 *   stranger —— 没借，她等到九点，准时走
 *   （用户主动说「我要走了」也走 stranger，文案由后端 why 区分为「你先走的」）
 */
/* 三个结局的收束语。stranger 与 leave 用的是同一张静帧（卡包里 ending_leave 的 ending
   字段就是 stranger），但**说的是两件事**：一种是"她等到九点自己走了"，一种是"你先走了"。
   文案跟着节点走，不跟着静帧走。 */
const ENDING_TEXT = {
  friend: "你们交换了名字。她说下次她请。",
  stranger: "她等到九点，然后走了。你大概不会再见到她。",
  leave: "你先走了。她没留你，只说了句路上小心。",
};

function handleEnding(ev) {
  /* 节点名决定文案：ending_leave 的 kind 也是 stranger（同一个静帧），但它是"你先走了" */
  S.ending = (ev.node === "ending_leave") ? "leave" : (ev.kind || "stranger");
  const box = $("ending");
  if (!box) return;
  const img = $("endingImg");
  const bg = $("endingBg");
  const src = (ASSETS.ending && (ASSETS.ending[S.ending] || ASSETS.ending[ev.kind])) || "";
  // 两层同一张图：bg 铺满模糊当底（手机竖屏时填掉上下黑边），sharp 是清晰原图。
  if (img && src) img.src = src;
  if (bg && src) bg.src = src;
  $("endingKicker").textContent = (ev.clock || "21:00") + " · 打烊";
  $("endingText").textContent = ENDING_TEXT[S.ending] || "";
  box.setAttribute("aria-hidden", "false");
  box.classList.add("show");
  document.body.classList.add("ended");
  // 结局时停掉还在播的语音、清空待播字幕——不能"结语出来了，她还在说上一句"
  try { if (window.speechSynthesis) window.speechSynthesis.cancel(); } catch (e) { /* 没有就算了 */ }
  S.queue.length = 0;
  STREAM.items.length = 0;
  setState("idle");
}

function applyDirective(d, fixes) {
  if (!d) return;
  // 先情绪后动作：动作贴片是临时姿势，几秒后回到当前情绪
  if (d.emotion) applyEmotion(d.emotion);
  if (d.scene) applyScene(d.scene);
  if (d.action) applyAction(d.action);
  if (d.effect) applyEffect(d.effect);
  if (d.event && d.event !== "none") triggerEvent(d.event);
  if (fixes && fixes.length) console.info("[directive] 已收敛：", fixes);
  if (window.__miraRecord) window.__miraRecord.push({ type: "directive", d, fixes });
}

/* 表演层接管时，directive 里只执行"跟表演无关"的那部分：
   场景切换、环境音特效、剧情演出视频。表情/动作要等 beat。 */
function applyDirectiveVisual(d) {
  if (!d) return;
  if (d.scene) applyScene(d.scene);
  if (d.effect) applyEffect(d.effect);
  if (d.event && d.event !== "none") triggerEvent(d.event);
}

/* ---------------------------------------------------------------- 打断 */

async function interrupt(reason = "voice") {
  const prev = S.activeSeq;
  S.activeSeq = ++S.seq;
  perfReset(S.activeSeq);
  stopPlayback();
  hideCinema();
  setCaption("");
  $("actionChip").classList.remove("show");
  /* 被打断的反应：先愣一下，再回到「听」。只换状态标签不换脸，
     否则会先闪一下 listen 再变 surprised（两次换脸挤在同一帧里）。 */
  S.suppressSprite = true;
  setState("listening");
  S.suppressSprite = false;
  applyEmotion("surprised");
  MOTION.settleUntil = performance.now() + 200;
  clearTimeout(S._bargeT);
  S._bargeT = setTimeout(() => {
    if (S.state === "listening" || S.state === "idle") {
      applyEmotion("listen");
      paint(ASSETS.char.listen, PRIO.emo);
    }
  }, 620);
  if (!S.sid) return;
  try {
    await api("/api/interrupt", { session_id: S.sid, seq: prev, mode: S.mode, reason });
  } catch (e) {}
}

/* ---------------------------------------------------------------- 麦克风 */

/* 手势设计（三种都支持，互不冲突）：
   - 按住超过 350ms：按住说话，松手立即发送
   - 轻点一下：开始录音，说完停顿 1.1s 自动发送；再点一下立刻发送
   - 录音条上的「取消」：丢弃这一段

   实测踩过的三个真实故障（这就是「话筒无法说话」的来源）：
   a) 只在 audio/webm;codecs=opus 上建 MediaRecorder：Safari / 部分 Edge 直接抛
      NotSupportedError，而构造代码没有 try —— 录音机根本没建起来，界面还停在倾听。
   b) 静音自动发送要求先出现 rms>0.012 的帧：麦克风偏小（笔记本内置麦、降噪强）时
      speechAt 永远为 0，于是只能干等到 15s 上限，用户以为话筒坏了。现在改为
      **自适应底噪**估计阈值，并给可见的录音计时/电平，不让它静默失败。
   c) 录音过程中只有麦克风红圈，没有「录了几秒 / 有没有电平 / 什么时候发出去」，
      用户无法判断自己有没有被听见。 */
const MIC_MIMES = [
  "audio/webm;codecs=opus", "audio/webm",
  "audio/ogg;codecs=opus", "audio/mp4", "audio/aac",
];
const MIC = {
  rec: null, stream: null, chunks: [], ac: null, analyser: null, raf: 0,
  startedAt: 0, pressAt: 0, speechAt: 0, lastVoice: 0, voiced: 0,
  floor: 0.004, mime: "", cancel: false, busy: false,
};

function pickMicMime() {
  if (typeof window.MediaRecorder === "undefined") return null;
  for (const m of MIC_MIMES) {
    try {
      if (MediaRecorder.isTypeSupported(m)) return m;
    } catch (e) {
      /* 有的实现会抛，继续试下一个 */
    }
  }
  return "";
}

function recBar(show, tip) {
  const bar = $("recBar");
  if (!bar) return;
  bar.hidden = !show;
  bar.classList.toggle("on", !!show);
  if (tip) $("recTip").textContent = tip;
}

async function startMic() {
  if (MIC.rec || MIC.busy) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    toast("这个浏览器不能录音（需要 http://127.0.0.1 或 https）。可以直接打字。", 6500);
    addMetric("麦克风", "不支持 getUserMedia");
    return;
  }
  const mime = pickMicMime();
  if (mime === null) {
    toast("这个浏览器不支持 MediaRecorder 录音。可以直接打字。", 6500);
    addMetric("麦克风", "不支持 MediaRecorder");
    return;
  }
  if (S.state === "speaking" || S.state === "thinking") await interrupt("voice");
  setState("listening");
  warmTts();          // 用户说话的这几秒：把握手（≈850ms）提前付掉，不 await
  MIC.busy = true;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
  } catch (err) {
    MIC.busy = false;
    setState("idle");
    let why = (err && err.name) || (err && err.message) || "未知错误";
    if (why === "NotAllowedError" || why === "SecurityError") {
      why = "被浏览器拒绝：点地址栏左边的图标，允许麦克风后刷新";
    } else if (why === "NotFoundError" || why === "DevicesNotFoundError") {
      why = "没有找到麦克风设备";
    } else if (why === "NotReadableError") {
      why = "麦克风被其它程序占用（会议/录音软件先关掉）";
    }
    toast(`麦克风不可用：${why}。也可以直接打字。`, 7000);
    addMetric("麦克风", String(why));
    return;
  }
  MIC.busy = false;
  MIC.stream = stream;
  MIC.chunks = [];
  MIC.cancel = false;
  let rec;
  try {
    rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    MIC.mime = mime;
  } catch (err) {
    try {
      rec = new MediaRecorder(stream);
      MIC.mime = "";
    } catch (err2) {
      stream.getTracks().forEach((t) => t.stop());
      MIC.stream = null;
      setState("idle");
      toast("录音初始化失败，可以直接打字。", 6000);
      addMetric("麦克风", `MediaRecorder: ${err2 && err2.name}`);
      return;
    }
  }
  rec.ondataavailable = (e) => e.data && e.data.size && MIC.chunks.push(e.data);
  rec.onerror = () => {
    MIC.cancel = true;
    toast("录音出错，这一段已丢弃。", 5000);
    stopMic(false);
  };
  rec.onstop = async () => {
    const chunks = MIC.chunks;
    const cancelled = MIC.cancel;
    const type = String((rec && rec.mimeType) || MIC.mime || "audio/webm").split(";")[0];
    MIC.rec = null;
    MIC.cancel = false;
    endMeter();
    recBar(false);
    if (MIC.stream) {
      MIC.stream.getTracks().forEach((t) => t.stop());
      MIC.stream = null;
    }
    $("micBtn").classList.remove("recording");
    const dur = (Date.now() - MIC.startedAt) / 1000;
    if (cancelled) {
      setState("idle");
      return;
    }
    if (!chunks.length || dur < 0.5) {
      setState("idle");
      toast("这段太短了（<0.5s）。按住麦克风说完再松开，说完停顿一下也会自动发送。", 6000);
      return;
    }
    await transcribeAndSend(new Blob(chunks, { type }), dur);
  };
  MIC.rec = rec;
  MIC.startedAt = Date.now();
  MIC.speechAt = 0;
  MIC.lastVoice = 0;
  MIC.voiced = 0;
  MIC.floor = 0.004;
  try {
    rec.start(120);
  } catch (err) {
    MIC.rec = null;
    MIC.stream.getTracks().forEach((t) => t.stop());
    MIC.stream = null;
    setState("idle");
    toast("启动录音失败，可以直接打字。", 6000);
    return;
  }
  $("micBtn").classList.add("recording");
  $("recTime").textContent = "0.0s";
  recBar(true, "在等你说话…");
  startMeter(stream);
}

function stopMic(send = true) {
  if (!MIC.rec) return;
  MIC.cancel = !send;
  if (MIC.rec.state !== "inactive") {
    try {
      MIC.rec.stop();
    } catch (e) {
      MIC.rec = null;
      endMeter();
      recBar(false);
    }
  } else {
    MIC.rec = null;
    endMeter();
    recBar(false);
  }
}

/* 电平表 + 自适应静音检测：先估计底噪，说话后连续 1.15s 静音就自动发送 */
function startMeter(stream) {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    const ac = new AC();
    if (ac.state === "suspended") ac.resume().catch(() => {});
    const src = ac.createMediaStreamSource(stream);
    const an = ac.createAnalyser();
    an.fftSize = 1024;
    src.connect(an);
    MIC.ac = ac;
    MIC.analyser = an;
    const buf = new Float32Array(an.fftSize);
    let warm = 0;
    const tick = () => {
      if (!MIC.analyser || !MIC.rec) return;
      an.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      const now = Date.now();
      if (warm < 8) {
        /* 前 ~130ms 只估底噪，不判语音 */
        MIC.floor = MIC.floor * 0.6 + rms * 0.4;
        warm++;
      } else if (rms < MIC.floor) {
        MIC.floor = MIC.floor * 0.7 + rms * 0.3;      // 安静下去时快速跟上
      } else {
        MIC.floor = MIC.floor * 0.998 + rms * 0.002;  // 噪声抬升时缓慢跟随
      }
      const thr = Math.min(0.055, Math.max(0.006, MIC.floor * 2.4));
      const lvl = Math.min(1, rms / thr / 2.6);
      $("micBtn").style.setProperty("--lvl", lvl.toFixed(3));
      MOTION.level = lvl;   // 倾听时她的点头/前倾跟着用户音量走
      if (warm >= 8 && rms > thr) {
        MIC.lastVoice = now;
        MIC.voiced += 1;
        if (!MIC.speechAt) {
          MIC.speechAt = now;
          recBar(true, "听到了，继续说…");
        }
      }
      const secs = (now - MIC.startedAt) / 1000;
      $("recTime").textContent = secs.toFixed(1) + "s";
      $("hint").textContent = MIC.speechAt
        ? `录 ${secs.toFixed(1)}s · 说完停顿一下自动发送，或再点一次麦克风`
        : `录 ${secs.toFixed(1)}s · 在等声音…（麦克风音量偏低可以靠近一点）`;
      /* 说话结束判定：检到过语音、连续 1.15s 静音、且语音帧足够 */
      if (MIC.speechAt && MIC.voiced >= 5 && now - MIC.lastVoice > 1150) {
        stopMic(true);
        return;
      }
      if (now - MIC.startedAt > 20000) {
        toast(MIC.speechAt ? "已录 20 秒，自动发送" : "20 秒没听到声音，这段丢弃");
        stopMic(!!MIC.speechAt);
        return;
      }
      MIC.raf = requestAnimationFrame(tick);
    };
    MIC.raf = requestAnimationFrame(tick);
  } catch (e) {
    /* 没有 AudioContext 也能录音，只是少了电平表与自动发送 */
    recBar(true, "正在录 · 点麦克风结束并发送");
  }
}

function endMeter() {
  cancelAnimationFrame(MIC.raf);
  MIC.raf = 0;
  if (MIC.ac) {
    try {
      MIC.ac.close();
    } catch (e) {}
  }
  MIC.ac = null;
  MIC.analyser = null;
  $("micBtn").style.removeProperty("--lvl");
}

async function transcribeAndSend(blob, dur) {
  setState("thinking");
  $("hint").textContent = dur ? `识别中…（录了 ${dur.toFixed(1)}s）` : "识别中…";
  const fd = new FormData();
  fd.append("audio", blob, "utterance.webm");
  fd.append("session_id", S.sid || "");
  const t0 = performance.now();
  try {
    const res = await fetch("/api/stt", { method: "POST", body: fd });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `STT HTTP ${res.status}`);
    const ms = Math.round(performance.now() - t0);
    addMetric("STT", `${data.text || "(空)"} · ${ms}ms`);
    if (!data.text) {
      setState("idle");
      toast("没听清。靠近麦克风，或者在安静一点的地方再说一次。", 5000);
      return;
    }
    $("hint").textContent = `听到：「${data.text}」`;
    S.lastHeard = data.text;
    await sendTurn(data.text, "user");
  } catch (err) {
    setState("idle");
    toast(`语音识别失败：${err.message}。可以直接打字。`, 6000);
    addMetric("STT 失败", String(err.message));
  }
}

/* ---------------------------------------------------------------- 建议问题 */

function showSuggest(items) {
  const el = $("suggest");
  el.innerHTML = "";
  (items || []).slice(0, 4).forEach((t) => {
    const b = document.createElement("button");
    b.textContent = t;
    b.onclick = () => {
      clearSuggest();
      sendTurn(t, "user");
    };
    el.appendChild(b);
  });
  el.classList.toggle("show", !!(items && items.length));
}

function clearSuggest() {
  const el = $("suggest");
  if (!el.classList.contains("show") && !el.innerHTML) return;
  el.classList.remove("show");
  setTimeout(() => {
    if (!el.classList.contains("show")) el.innerHTML = "";
  }, 260);
}

/* ---------------------------------------------------------------- 观测面板 */

function addMetric(k, v) {
  S.metrics = S.metrics || [];
  S.metrics.unshift(`${k}: ${v}`);
  S.metrics = S.metrics.slice(0, 8);
  $("metrics").innerHTML = S.metrics.map((m) => `<span>${escapeHtml(m)}</span>`).join("");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

async function refreshEvents() {
  if (!S.sid) return;
  try {
    const res = await fetch(`/api/events/${S.sid}`);
    const data = await res.json();
    $("metrics").innerHTML = [
      `<span>provider <b>${S.mode}</b></span>`,
      `<span>turns <b>${S.turns}</b></span>`,
      `<span>beat <b>${data.beat ?? S.beat}</b></span>`,
      `<span>首包 <b>${S.lastFirstAudio ?? "-"}ms</b></span>`,
      `<span>错误 <b>${S.errors}</b></span>`,
      `<span>seq <b>${data.seq}</b></span>`,
    ].join("");
    $("events").innerHTML = (data.events || [])
      .slice(-40)
      .reverse()
      .map(
        (e) =>
          `<div class="${e.kind.includes("error") ? "err" : ""}">${new Date(e.ts * 1000)
            .toLocaleTimeString("zh-CN", { hour12: false })} <em>${e.kind}</em> ${escapeHtml(
            JSON.stringify({ ...e, ts: undefined, kind: undefined })
          ).slice(0, 150)}</div>`
      )
      .join("");
  } catch (e) {}
}

/* key 行只在选中云端时出现：选本地档时它没有意义，留着会让人以为本地也要配 key。
   切回本地时顺手清空输入框——key 不在页面上留明文，也不写 localStorage/sessionStorage。 */
function syncCloudRow() {
  const row = document.querySelector(".dp-row.dp-cloud");
  if (!row) return;
  const isCloud = S.mode === "cloud";
  row.hidden = !isCloud;
  if (!isCloud) {
    const key = $("cloudKey");
    if (key) key.value = "";
  }
}

/* ---------------------------------------------------------------- 历史记录面板
   数据源是只读的 GET /api/history/{sid}：assistant 那条服务端只存台词正文
   （s.history 里的「首行 JSON + 台词」是给模型的协议，不能直接给人看）。 */
const HIST = { data: null, sig: "" };

function historyMetaLine(t) {
  // 第N轮 · 拍N · preset/gen · XXXms —— 没值的字段不显示（用户气泡没有 path/ms）
  const bits = [`第${t.i}轮`];
  if (t.beat !== undefined && t.beat !== null) bits.push(`拍${t.beat}`);
  if (t.path) bits.push(t.path === "preset" ? "preset" : "gen");
  if (t.ms) bits.push(`${t.ms}ms`);
  return bits.join(" · ");
}

function paintHistory(data) {
  HIST.data = data || { turns: [] };
  const turns = Array.isArray(HIST.data.turns) ? HIST.data.turns : [];
  const list = $("historyList");
  if (!list) return;
  /* 只在内容变了才重绘：面板每 1.5s 轮询一次，整表重建会让滚动条和文字选择一直跳；
     重绘后滚到底部，用户就能看到最新一轮。 */
  const sig = JSON.stringify(turns);
  if (sig !== HIST.sig) {
    HIST.sig = sig;
    list.innerHTML = turns.length
      ? turns
          .map((t) => {
            const role = t.role === "user" ? "user" : "assistant";
            return `<div class="hist-item ${role}">
            <div class="hist-bubble">${escapeHtml(t.text || "")}</div>
            <div class="hist-meta-line">${escapeHtml(historyMetaLine(t))}</div>
          </div>`;
          })
          .join("")
      : '<div class="hist-empty">还没有对话</div>';
    list.scrollTop = list.scrollHeight; // 有新内容才自动滚到底部
  }
  const rounds = turns.filter((t) => t.role === "user").length;
  $("historyMeta").textContent = S.sid
    ? `sid ${S.sid} · ${turns.length} 条 · ${rounds} 轮 · 服务端拍 ${HIST.data.beat ?? "-"}`
    : "还没有会话：先进入场景";
}

async function refreshHistory() {
  if (!S.sid) {
    paintHistory({ turns: [] });
    return;
  }
  try {
    const res = await fetch(`/api/history/${S.sid}`, { cache: "no-store" });
    const data = await res.json();
    paintHistory(data);
  } catch (e) {
    $("historyMeta").textContent = "拉取失败：后端没响应（刷新页面或重启服务）";
  }
}

/* 导出当前 transcript：纯前端生成 Blob，不经过任何第三方库/CDN。 */
function exportHistory() {
  const payload = {
    exported_at: new Date().toISOString(),
    ...(HIST.data || { session_id: S.sid, mode: S.mode, beat: S.beat, turns: [] }),
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `mira-history-${S.sid || "session"}.json`;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

/* ---------------------------------------------------------------- 录屏音频 */

const REC = { enabled: false, ctx: null, dest: null, recorder: null, chunks: [] };

function recInit() {
  if (!S.recEnabled) return;
  REC.enabled = true;
  REC.ctx = new (window.AudioContext || window.webkitAudioContext)();
  REC.dest = REC.ctx.createMediaStreamDestination();
  REC.recorder = new MediaRecorder(REC.dest.stream, { mimeType: "audio/webm;codecs=opus" });
  REC.recorder.ondataavailable = (e) => e.data.size && REC.chunks.push(e.data);
}

function recRoute(audio) {
  if (!REC.enabled || !REC.ctx) return;
  try {
    const src = REC.ctx.createMediaElementSource(audio);
    src.connect(REC.ctx.destination);
    src.connect(REC.dest);
  } catch (e) {}
}

window.__miraRec = {
  start() {
    if (!REC.enabled) recInit();
    if (REC.recorder && REC.recorder.state === "inactive") {
      REC.chunks = [];
      REC.recorder.start();
    }
    return REC.recorder ? REC.recorder.state : "unavailable";
  },
  async stop() {
    if (!REC.recorder) return null;
    const done = new Promise((res) => (REC.recorder.onstop = res));
    REC.recorder.stop();
    await done;
    const blob = new Blob(REC.chunks, { type: "audio/webm" });
    const buf = await blob.arrayBuffer();
    let bin = "";
    const bytes = new Uint8Array(buf);
    for (let i = 0; i < bytes.length; i += 0x8000) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return btoa(bin);
  },
};

/* ---------------------------------------------------------------- 绑定 */

function bind() {
  $("startBtn").onclick = async () => {
    try {
      await startSession($("modeSelect").value || S.mode || "llama");
    } catch (e) {
      toast("后端未启动，请先运行 start.ps1");
      return;
    }
    $("gate").classList.add("hide");
    ambienceInit();
    preload();
    if (S.recEnabled) {
      recInit();
      window.__miraRec.start();
    }
    setState("thinking");
    await sendTurn("", "open");
  };

  $("sendBtn").onclick = () => {
    const v = $("textInput").value.trim();
    if (!v) return;
    $("textInput").value = "";
    sendTurn(v, "user");
  };
  $("textInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("sendBtn").click();
  });
  // 打字也是"她在等你说完"的时间：第一次敲字就把合成长连热起来
  $("textInput").addEventListener("input", () => warmTts());

  const mic = $("micBtn");
  mic.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    mic.setPointerCapture?.(e.pointerId);
    MIC.pressAt = Date.now();
    if (!MIC.rec) {
      startMic();
      return;
    }
    /* 已经在录：再点一下 = 立刻结束并发送（轻点录音模式的手动出口） */
    if (Date.now() - MIC.startedAt > 400) stopMic(true);
  });
  mic.addEventListener("pointerup", () => {
    const held = Date.now() - (MIC.pressAt || 0);
    /* 按住说话：松手即发送。轻点（<350ms）不打断，交给停顿自动发送 */
    if (MIC.rec && held > 350) stopMic(true);
  });
  mic.addEventListener("pointercancel", () => {
    if (MIC.rec) stopMic(true);
  });
  const recCancel = $("recCancel");
  if (recCancel) recCancel.onclick = () => stopMic(false);
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && MIC.rec) stopMic(false);
  });

  $("debugBtn").onclick = () => {
    const p = $("debugPanel");
    p.hidden = !p.hidden;
    $("debugBtn").classList.toggle("on", !p.hidden);
    if (!p.hidden) {
      // 两块面板不同时开：观测面板是底部抽屉，历史面板是右侧抽屉，同开会把画面盖住。
      $("historyPanel").hidden = true;
      $("historyBtn").classList.remove("on");
      refreshEvents();
    }
  };
  $("debugClose").onclick = () => {
    $("debugPanel").hidden = true;
    $("debugBtn").classList.remove("on");
  };

  /* ---- 历史记录：只读聊天记录，打开时拉一次，之后只在面板可见时每 1.5s 刷新。
     为什么是轮询而不是 SSE：一轮对话的落库在 done 之后，1.5s 的粒度已经够；再加一条
     长连接会让这个 Demo 多一个要维护的失败面。 */
  $("historyBtn").onclick = () => {
    const p = $("historyPanel");
    p.hidden = !p.hidden;
    $("historyBtn").classList.toggle("on", !p.hidden);
    if (!p.hidden) {
      $("debugPanel").hidden = true;
      $("debugBtn").classList.remove("on");
      refreshHistory();
    }
  };
  $("historyClose").onclick = () => {
    $("historyPanel").hidden = true;
    $("historyBtn").classList.remove("on");
  };
  $("historyRefresh").onclick = () => refreshHistory();
  $("historyExport").onclick = async () => {
    await refreshHistory();     // 先拉一次，导出的就是「此刻面板上看到的」
    exportHistory();
  };

  setInterval(() => {
    if (!$("debugPanel").hidden) refreshEvents();
    // 只在面板可见时轮询：关着的时候不该每 1.5s 打一次接口。
    if (!$("historyPanel").hidden) refreshHistory();
  }, 1500);

  $("modeSelect").onchange = async (e) => {
    S.mode = e.target.value === "cloud" ? "cloud" : "llama";
    S.seq = 0;
    S.activeSeq = 0;
    syncCloudRow();             // 切到 cloud 才显示 key 行，切回本地立刻收起并清空
    paintSrvMode();             // 「本轮」那一半要立刻跟着变，不然和上面的下拉自相矛盾
    await startSession(S.mode); // 未填 key 时 startSession 里会 toast「未填 key，已回落本地/自动」
    if (!(S.mode === "cloud" && !S.cloudConfigured)) {
      toast(`已切换 provider：${S.mode === "cloud" ? "云端 DeepSeek" : "本地 Qwen3"}`);
    }
    await sendTurn("", "open");
  };

  /* ---- 云端 DeepSeek（P0-4）：key 只发给服务端，落地在进程内存里；
     前端不写入 localStorage / sessionStorage，刷新即丢。 */
  const cloudState = $("cloudState");
  const paintCloud = (st) => {
    S.cloudConfigured = !!(st && st.configured);
    if (!cloudState) return;
    cloudState.textContent = st && st.configured
      ? `已连接 · ${st.model} · ${st.masked || ""}（仅内存）`
      : "未配置 · 仅存内存，重启即失效，不写磁盘";
    cloudState.classList.toggle("on", !!(st && st.configured));
  };
  // 读状态用 GET：POST /api/cloud 是「设置 key」，空 key 会按设计回 400，
  // 每次开页都在控制台留一条红字，看起来像故障（实际是调用方式错了）。
  fetch("/api/cloud").then((r) => r.json()).then(paintCloud).catch(() => {});
  $("cloudSave").onclick = async () => {
    const key = ($("cloudKey").value || "").trim();
    if (!key) { toast("先粘贴 DeepSeek key"); return; }
    $("cloudSave").disabled = true;
    $("cloudSave").textContent = "验证中…";
    try {
      const r = await fetch("/api/cloud", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key }),
      });
      const j = await r.json();
      if (!r.ok || !j.ok) { toast(j.error || "Key 不可用"); return; }
      $("cloudKey").value = "";            // 不在页面上留明文
      S.cloudConfigured = true;
      paintCloud({ configured: true, model: j.model, masked: j.masked });
      S.mode = "cloud";
      $("modeSelect").value = "cloud";
      syncCloudRow();
      await startSession("cloud");
      toast("已切到云端 DeepSeek（仅本次运行有效）");
      await sendTurn("", "open");
    } finally {
      $("cloudSave").disabled = false;
      $("cloudSave").textContent = "连接";
    }
  };
  $("cloudClear").onclick = async () => {
    await fetch("/api/cloud", { method: "DELETE" });
    S.cloudConfigured = false;
    paintCloud({ configured: false });
    toast(S.mode === "cloud" ? "已清除云端 Key：下一轮会回落本地/自动" : "已清除云端 Key");
  };

  document.querySelectorAll("[data-inject]").forEach((btn) => {
    btn.onclick = async () => {
      const key = btn.dataset.inject;
      if (key === "event_fail") {
        S.eventFail = !S.eventFail;
        btn.classList.toggle("on", S.eventFail);
        toast(S.eventFail ? "已注入：演出素材不可用（走最简演出）" : "已恢复演出素材");
        return;
      }
      if (!S.sid) await startSession(S.mode);
      const cur = JSON.parse(btn.dataset.state || "{}");
      const next = !cur[key];
      btn.dataset.state = JSON.stringify({ ...cur, [key]: next });
      btn.classList.toggle("on", next);
      await api("/api/debug/inject", { session_id: S.sid, [key]: next });
      // 中文标签：这行提示会出现在录屏画面里，不能是 "tts_fail → 开" 这种内部键名
      const INJECT_LABEL = {
        tts_fail: "语音合成失败", llm_timeout: "模型超时", slow_tts: "语音变慢",
        gate_fail: "对话闸门拦截", time_fail: "时间校验失败",
        bad_header: "指令协议异常", bad_header_only: "只吐指令不给台词",
      };
      toast(`${INJECT_LABEL[key] || key}：${next ? "已注入" : "已恢复"}`);
    };
  });

  /* 2026-10-21：面板上的「雨声底噪」开关随底噪一起删掉（用户明确不要这层音）。 */
  const injectSample = (url) => async () => {
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error("示例音频缺失");
      const blob = await res.blob();
      await transcribeAndSend(blob);
    } catch (e) {
      toast(`示例语音不可用：${e.message}`);
    }
  };
  /* 示例语音也属于「卡素材」（cards/<id>/assets/audio/），跟着基址走，别再写死 /static/。 */
  $("sampleVoice").onclick = injectSample(assetUrl("audio/sample_user.wav"));
  $("sampleVoice2").onclick = injectSample(assetUrl("audio/sample_user_2.wav"));
}

/* 启动时问一次服务端「你现在跑的是哪个 provider」。
   服务端 LLM_MODE 默认仍是 auto，/api/health 的 mode 可能是 auto / mock / strata ——
   这些都不在前端的两个 option 里，所以这里只做映射：cloud 且服务端真的配了 key
   才选 cloud，其余一律落到「本地 Qwen3」这一档（服务端自己的 auto 链负责降级），
   并把「服务端实际 provider」写在 select 下方 + title 里，避免用户误以为选啥就是啥。

   auto 那一档的括号里**不写死链成员**：以前写的是「25B → Qwen3 → Mock」，
   而这台机器上 25B 服务（1234）从来没起过，标签等于在骗人（用户实测截图指出）。
   现在只渲染 /api/health 的 chain（服务端启动时探端口算出来的真实链）。 */
const SERVER_MODE_LABEL = {
  auto: "auto（自动链）",
  llama: "llama（本地 Qwen3）",
  strata: "strata（本地 25B）",
  mock: "mock（零依赖剧本）",
  cloud: "cloud（DeepSeek）",
};

/* 「服务端默认 … · 本轮 …」这行在切档后必须跟着变。以前只在 bootProvider 里算一次，
   于是演示里出现「下拉已是云端 DeepSeek、下面却写本轮 llama」——被录进过一版成片。 */
function paintSrvMode() {
  const srv = $("srvMode");
  if (!srv) return;
  const mode = S.serverMode || "auto";
  const actual = mode === "auto"
    ? `auto（实际链：${S.serverChain || "Qwen3 → Mock"}）`
    : (SERVER_MODE_LABEL[mode] || mode);
  srv.textContent = `服务端默认 ${actual} · 本轮 ` +
    (S.mode === "cloud" ? "cloud（DeepSeek）" : "llama（本地 Qwen3）") +
    (S.cloudConfigured ? " · DeepSeek 已配置 key" : " · DeepSeek 未配置");
}

function chainText(h) {
  const chain = Array.isArray(h && h.chain) ? h.chain.filter(Boolean) : [];
  return chain.length ? chain.join(" → ") : "Qwen3 → Mock";
}

async function bootProvider() {
  try {
    const h = await (await fetch("/api/health")).json();
    if (!h || !h.mode) return;
    S.cloudConfigured = !!(h.cloud && h.cloud.configured);
    S.mode = h.mode === "cloud" && S.cloudConfigured ? "cloud" : "llama";
    const sel = $("modeSelect");
    if (sel) {
      sel.value = S.mode;
      sel.title = `服务端实际 provider：${h.mode}（llm=${h.llm || "-"}，链 ${chainText(h)}）`;
    }
    const srv = $("srvMode");
    if (srv) {
      /* 这一行原来只写服务端**进程默认**档（auto），和上面 select 选中的「云端 DeepSeek」
         并排时读起来像自相矛盾（真机演示里被拍进去过）。分开写清楚：默认是什么、
         这一轮用的是什么。 */
      S.serverMode = h.mode;
      S.serverChain = chainText(h);
      paintSrvMode();
      // auto/mock/strata 都不在前端两档里：标黄提醒「你选的本地档 ≠ 服务端实际档」
      srv.classList.toggle("warn", h.mode !== "llama" && h.mode !== "cloud");
    }
    const note = document.querySelector(".gate-note");
    if (note && h.mode === "mock") {
      /* 只有显式 LLM_MODE=mock 能在 boot 时就确定。auto 链是否真的退到 Mock
         在这里判断不了（见 done 事件里的说明），交给首轮 done.provider。 */
      note.textContent = "Mock 模式：本地模型没起，对话按剧本推进（流程与结局完整）· 点麦克风可打断她";
      note.classList.add("mock-note");
    }
    syncCloudRow();
  } catch (e) {
    const srv = $("srvMode");
    if (srv) srv.textContent = "服务端未响应：启动服务后刷新页面可见实际 provider";
    /* 后端没起来时下面 startBtn 会给出提示 */
  }
}

bind();
setState("idle");
bootProvider();
/* 首屏两层图（场景板 + 人物）。素材基址要等 /api/card 回来才知道（v17：素材在卡里），
   所以先画兜底的旧布局，拿不到就维持——拿到后按新基址重画一次，避免首屏 404 白屏。 */
function paintInitialAssets() {
  $("plateA").src = ASSETS.plate.cafe_night;
  $("plateA").classList.add("is-front");
  $("charA").src = ASSETS.char.neutral;
  $("charA").classList.add("is-front");
}
paintInitialAssets();
/* 卡包快照的**预取**：startSession 里还会再取一次（那时要拿 labels/stage，且必须成功）。
   这里只为一个目的——素材基址在用户点「开始」之前就该定下来，否则首屏那两张图
   会按兜底布局去取、404。拿不到（服务端没起）就保持兜底，不影响别的流程。 */
fetch("/api/card")
  .then((r) => (r.ok ? r.json() : null))
  .then((c) => {
    if (!c || !c.assets_base || S.card) return;   // 已经应用过就不再重复
    applyCard(c);
    paintInitialAssets();
  })
  .catch(() => { /* 服务端没起：交给 startBtn 的报错提示 */ });
/* 打断/新一轮会 abort 在途的 fetch 和 reader：这是**预期行为**，不该冒到控制台变成
   "unhandled rejection"。只吞 AbortError，其它错误照常抛。 */
window.addEventListener("unhandledrejection", (e) => {
  const r = e.reason;
  const msg = r && (r.name === "AbortError" || /aborted/i.test(String(r.message || r)));
  if (msg) e.preventDefault();
});

if (S.recEnabled) recInit();
motionTick();               // 持续微动（呼吸/重心/说话起伏）
scheduleNudge();            // 沉默 12–16s 让她自己动一下

/* 调试/验收句柄：QA 探针（tools/qa/perf_probe.py）靠它断言 beat 有没有按时触发。
   testPerf 可以脱离服务端直接喂一段 beat，用于验证播放器本身。 */
window.__mira = {
  S, sendTurn, interrupt, setState, applyDirective, startSession,
  perf: PERF, motion: MOTION, fireBeat, stream: STREAM, enqueueStream, audioCtx,
  /* QA 用：多帧动作序列（可直接喂一次 playSequence 验帧序；见 tools/qa/sequence_test.py） */
  seq: SEQ, playSequence, seqStop, loadSeqManifest, seqMeta: SEQ_META, trans: TRANS,
  breath: { start: breathStart, stop: breathStop, state: BREATH,
    /* QA：现场改呼吸振幅/驱动方式（breath_ab.py 的四档对照用） */
    setAmp(v) { BREATH.amp = Number(v); motionTick._tx = null; },
    getAmp() { return BREATH.amp; } },
  /* QA：待机每 20s 一次的"自己动一下"（sip_coffee / look_window）。
     量呼吸/闪烁时必须能关掉它，否则一次动作换图会污染帧间差。 */
  nudge: NUDGE,
  applyEmotion, showSprite, playTransition,
  /* QA 用：表演通道仲裁（tools/qa/probe_caption_sync.py / check_channels.py 对拍用）。
     claims 是当前通道占用表，pick() 会真的算一遍——看到"动作没了"先看这里。 */
  chan: { claims: CHAN.claims, pick: chanPick, busy: chanBusy, speech: speechBusy, spec: stageSpec },
  /* QA 用：历史面板（tools 里可直接断言 transcript 有没有落、台词里有没有 JSON 头） */
  history: HIST, refreshHistory, exportHistory,
  /* QA 用：演出层（没有视频了，这几个是现在的全部入口，见 tools/qa/v14_shots.py） */
  triggerEvent, hideCinema, showStill, rumble, handleEnding,
  /* QA 用：关掉持续微动（layers_test 要比较「换表情时背景是否一个像素都没动」，
     呼吸位移会让人物轮廓移动 5px，那是动画不是图层串位）。 */
  setMotion(on) {
    S.motionOff = !on;
    if (!on) {
      clearTimeout(NUDGE.timer);
      const layer = document.querySelector(".char-layer");
      if (layer) layer.style.transform = "";
    } else {
      MOTION.t0 = performance.now();
      scheduleNudge();
    }
    return !S.motionOff;
  },
  testPerf(payload) {
    // 探针用：清空待排期 beat，并把 rev 抬到当前之后，避免被 rev 过滤掉
    const keepRev = PERF.rev;
    PERF.t0 = performance.now();
    PERF.beats = [];
    PERF.rev = keepRev;
    perfAccept({ rev: keepRev + 1, start_hold_ms: 0, beats: [], ...(payload || {}) });
    return PERF.log.length;
  },
};
