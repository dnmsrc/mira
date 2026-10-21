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

/* 资源模型：场景板（plate，无人物）和角色贴片（sprite，带 alpha）是两套独立素材。
   换表情只切 char 层、背景像素不动；换镜头只对 .stage 做 transform，两层一起缩放。 */
const ASSETS = {
  char: {
    neutral: "/static/assets/char/neutral.webp",
    smile: "/static/assets/char/smile.webp",
    worried: "/static/assets/char/worried.webp",
    surprised: "/static/assets/char/surprised.webp",
    thinking: "/static/assets/char/thinking.webp",
    listen: "/static/assets/char/listen.webp",
  },
  pose: {
    look_window: "/static/assets/char/look_window.webp",
    sip_coffee: "/static/assets/char/sip_coffee.webp",
    lean_in: "/static/assets/char/lean_in.webp",
    show_photo: "/static/assets/char/show_photo.webp",
    stand: "/static/assets/char/stand.webp",
  },
  plate: { cafe_night: "/static/assets/plate/cafe_night.webp" },
  insert: { polaroid: "/static/assets/insert/polaroid.jpg" },
  /* 动作的**多帧序列**（动作名 → 帧 URL 数组）。由 loadSeqManifest() 从
     static/assets/char/manifest.json 填进来，所以换素材不用改这份代码。
     为什么要多帧：用户实测「卡顿与闪速」——一张静止姿势 + 380ms 叠化，
     动作看起来是"跳"过去的；连续 4–8 帧硬切才有动作的方向感。 */
  seq: {},
  /* 2026-10-20：`video` 这一组已删除（idle 循环 + 三段 AniSora 片段都不用了）。
     演出改成静帧 + 程序合成，见 triggerEvent()。 */
};

const LABEL = {
  emotion: { neutral: "平静", smile: "微笑", worried: "担心", surprised: "惊讶", thinking: "思索", listen: "倾听" },
  action: { idle: "待机", look_window: "望向窗外", sip_coffee: "抿了一口咖啡", lean_in: "靠近了些",
            show_photo: "掏出照片", stand: "站起身" },
  scene: { cafe_night: "常驻机位 · 吧台", window_close: "镜头 · 近景", lights_dim: "环境 · 灯光转暗" },
  effect: { rain: "雨声", thunder: "雷声", none: "安静" },
  event: { none: "", thunder: "闪电演出", photo_reveal: "照片演出", last_train: "末班车演出" },
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
  // 只有一套素材，卡包快照就是它；清单加载会再覆盖一次（含序列帧），互为兜底。
  if (a.char) ASSETS.char = a.char;
  if (a.pose) ASSETS.pose = a.pose;
  if (a.plate) ASSETS.plate = a.plate;
  if (a.insert) ASSETS.insert = a.insert;
  if (a.still) ASSETS.still = a.still;
  if (card.labels) {
    ["emotion", "action", "scene", "effect", "event"].forEach((k) => {
      if (card.labels[k]) LABEL[k] = { ...LABEL[k], ...card.labels[k] };
    });
  }
  S.card = card;
  if (card.title) document.title = `Mira · ${card.title}`;
  if (card.character && card.character.name) {
    const n = document.querySelector(".who .name");
    if (n) n.textContent = card.character.name;
  }
  if ((card.warnings || []).length) console.warn("[card] 卡包告警：", card.warnings);
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

/* ------------------------------------------------------- 环境音（程序合成，无素材）
   雨后的房间不该是「静音的 TTS 真空」：一层很低的棕噪声当屋檐滴水 + 远处车流的底噪，
   让语音听起来是「在这个空间里」说的。音量压得很低，不盖台词。 */
const AMB = { ctx: null, gain: null, on: false };

function ambienceInit() {
  if (AMB.ctx) return;
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    const ctx = new AC();
    if (ctx.state === "suspended") ctx.resume().catch(() => {});
    const len = Math.floor(ctx.sampleRate * 4);
    const buf = ctx.createBuffer(1, len, ctx.sampleRate);
    const data = buf.getChannelData(0);
    let last = 0;
    for (let i = 0; i < len; i++) {
      const white = Math.random() * 2 - 1;
      last = (last + 0.02 * white) / 1.02;
      data[i] = last * 3.2;
    }
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.loop = true;
    const hp = ctx.createBiquadFilter();
    hp.type = "highpass";
    hp.frequency.value = 140;
    const lp = ctx.createBiquadFilter();
    lp.type = "lowpass";
    lp.frequency.value = 1100;
    const gain = ctx.createGain();
    gain.gain.value = 0;
    src.connect(hp);
    hp.connect(lp);
    lp.connect(gain);
    gain.connect(ctx.destination);
    src.start();
    AMB.ctx = ctx;
    AMB.gain = gain;
    AMB.on = true;
    gain.gain.linearRampToValueAtTime(0.05, ctx.currentTime + 3.0);
  } catch (e) {
    /* 没有 Web Audio 就安静，不影响主流程 */
  }
}

function ambienceSet(on) {
  if (!AMB.ctx) {
    if (on) ambienceInit();
    return;
  }
  if (AMB.ctx.state === "suspended") AMB.ctx.resume().catch(() => {});
  AMB.on = on;
  const t = AMB.ctx.currentTime;
  AMB.gain.gain.cancelScheduledValues(t);
  AMB.gain.gain.linearRampToValueAtTime(on ? (AMB.ducked ? 0.026 : 0.05) : 0.0, t + 1.1);
}

/* 她说话时把房间底噪压一点（像真人说话时你会不自觉忽略环境音）。
   幅度很小，只在听感上让台词更"近"。 */
function ambDuck(on) {
  AMB.ducked = !!on;
  if (!AMB.ctx || !AMB.on) return;
  const t = AMB.ctx.currentTime;
  AMB.gain.gain.cancelScheduledValues(t);
  AMB.gain.gain.linearRampToValueAtTime(on ? 0.026 : 0.05, t + 0.5);
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

function setState(name) {
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
  } else if (name === "listening") showSprite(ASSETS.char.listen, SPRITE_MS);
  else if (PERF.active) {
    /* beat 负责 thinking/speaking/idle 的表情，这里不抢 */
  } else if (name === "thinking") showSprite(ASSETS.char.thinking, SPRITE_MS);
  else if (name === "speaking" || name === "idle") {
    // 动作贴片（sip_coffee / show_photo …）还在时效内就别抢它的画面
    // 2026-10-20 反馈 12：**呼吸循环在跑的时候也不许抢**。实测（sprite_trace）待机
    // 8 秒里插进来一次 neutral.webp(150ms)，把 340ms 的呼吸叠化硬切断，接着下一次
    // breathTick 又从半途重新淡入 —— 合成上就是每 ~5 秒一次的"闪一下"。
    if (!BREATH.on && Date.now() >= (S.poseUntil || 0))
      showSprite(ASSETS.char[S.emotion] || ASSETS.char.neutral, SPRITE_MS);
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
    // 序列帧也预取：第一帧没加载完就开播，会闪一下白。
    // 呼吸帧现在走 A/B 主通道（不是覆盖层），第一轮没缓存就会"换图等 onload"，
    // 于是每张都晚 10–40ms 落地 —— 6 张叠起来就是一串不均匀的节拍，必须一起预取。
    Object.values(ASSETS.seq).forEach((arr) =>
      (arr || []).forEach((src) => {
        const img = new Image();
        img.src = src;
      })
    );
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
let SPRITE_MS = 80;
const SWAP = { busyUntil: 0, timer: 0, pending: null, inflight: false };

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
  if (SWAP.inflight || now < SWAP.busyUntil) {
    SWAP.pending = { src, ms: dur || 220 };
    clearTimeout(SWAP.timer);
    SWAP.timer = setTimeout(flushSwap, 40);
    return;
  }
  const front = S.spriteFront === "A" ? $("charA") : $("charB");
  const back = S.spriteFront === "A" ? $("charB") : $("charA");
  if (!front || !back) return;
  if (front.getAttribute("src") === src) {
    // 目标就是当前这张：把排队中的旧目标丢掉，别让过期的换图在 200ms 后才落地
    SWAP.pending = null;
    clearTimeout(SWAP.timer);
    return;
  }
  const ms = Math.max(40, dur || SPRITE_MS);
  SWAP.inflight = true;
  back.style.transition = "none";
  back.style.opacity = "0";
  back.classList.remove("is-front");
  back.style.transitionDuration = `${ms}ms`;
  front.style.transitionDuration = `${ms}ms`;
  const old = front;
  const start = () => {
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
    SWAP.busyUntil = performance.now() + ms;
    setTimeout(() => {
      /* 撤场（见铁律 4）**但不能把当前前景层撤掉**。
         踩过的坑：上一次叠化的撤场定时器排在 ms+60，而下一次叠化在 ms 就可以开始。
         于是可能出现「新层还在 onload（opacity 0）、旧层的撤场定时器刚好到点」——
         两层都没有 is-front，角色整个人从画面上消失，walk 截图里 listening / 换表情
         那几张就是空的。这里改成永远以"当前前景层"为准做一次清扫。 */
      sweepSprites(spriteFrontEl());
      flushSwap();
    }, ms + 60);
  };
  if (back.getAttribute("src") === src && back.complete) { start(); return; }
  back.onload = start;
  back.onerror = () => { SWAP.inflight = false; SWAP.busyUntil = 0; flushSwap(); };
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
let XFADE = 55;              // 帧间淡入上限（manifest playback.crossfadeMs 只作参考）
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
  const cur = SEQ.side === 0 ? a : b;
  const nxt = SEQ.side === 0 ? b : a;
  const ms = xfade == null ? XFADE : xfade;
  nxt.style.transitionDuration = ms + "ms";
  const flip = () => {
    /* 和 showSprite 同一个道理：新帧在旧帧**上面**淡入、旧帧先保持 1。
       两张各 0.5 时合成 alpha 只有 0.75，会漏 25% 背景 —— 那就是「闪一下」。 */
    nxt.style.zIndex = "2";
    cur.style.zIndex = "1";
    nxt.classList.add("show");
    SEQ.side = SEQ.side === 0 ? 1 : 0;
    /* 新帧到 1 之后再撤旧帧（同 showSprite）：先把旧帧撤掉会让底下那层按 DOM 顺序顶上来。 */
    clearTimeout(SEQ.hideT);
    SEQ.hideT = setTimeout(() => {
      cur.classList.remove("show");
    }, ms + 90);
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
  SEQ.ms = (ms || []).map((v) => Math.max(SEQ_MIN_MS, Number(v) || SEQ_MIN_MS));
  SEQ.idx = 0;
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
        const done = onDone || ((emotion) => showSprite(ASSETS.char[emotion] || ASSETS.char.neutral, SPRITE_MS));
        done(S.emotion);
        box.classList.remove("on");
        SEQ.on = false;
        SEQ.playing = "";
        SEQ.idx = 0;
        ["charC1", "charC2"].forEach((id) => {
          const e = $(id);
          if (e) e.classList.remove("show");
        });
      }, SEQ.ms[i] || 110);
    }, (i === 0 ? 0 : SEQ.ms[i - 1]) || 110);
  };
  advance(0);
  return true;
}

function playSequence(action, opts) {
  const meta = SEQ_META[action];
  const urls = ((meta && meta.frames) || ASSETS.seq[action] || []).filter(Boolean);
  if (urls.length < 2) return false;
  const fps = Math.max(6, Math.min(24, (opts && opts.fps) || 11));
  const step = Math.round(1000 / fps);
  let ms = (meta && meta.ms && meta.ms.length === urls.length) ? meta.ms.slice() : urls.map(() => step);
  if (opts && opts.holdMs) ms[ms.length - 1] = Math.max(opts.holdMs, ms[ms.length - 1] || 0);
  SEQ.playing = action;
  return playFrames(urls, ms, null, opts);
}

/* 表情过渡：director 从思考切到笑，中间插两帧 70ms 的过渡帧，比 380ms 叠化更像「表情动了一下」。
   只对 manifest 里给了 from->to 的组合生效，其余照旧走 showSprite 交叉淡化。 */
function playTransition(key, targetEmotion, opts) {
  const t = TRANS[key];
  if (!t || !t.frames || t.frames.length < 1) return false;
  const ms = (t.ms && t.ms.length === t.frames.length) ? t.ms.slice() : t.frames.map(() => 70);
  ms[ms.length - 1] = 60;   // 末帧只停一拍，随即交给静态表情层，避免「过渡帧多挂一拍」的僵硬感
  /* 兜底：过渡帧要是加载失败（换素材时出现过 404），playFrames 的 onDone 永远不会来，
     表情就被吞掉了 —— 用户看到的是「表情没变」，比硬切换更糟。400ms 没落地就直接换脸。 */
  const fallback = setTimeout(() => {
    if (S.emotion === targetEmotion && (!SEQ.on || SEQ.playing !== key)) return;
    showSprite(ASSETS.char[targetEmotion] || ASSETS.char.neutral, SPRITE_MS);
  }, 400);
  const ok = playFrames(t.frames, ms, () => {
    clearTimeout(fallback);
    showSprite(ASSETS.char[targetEmotion] || ASSETS.char.neutral, SPRITE_MS);
  }, Object.assign({ hardCut: false }, opts || {}));
  if (!ok) clearTimeout(fallback);
  return ok;
}

/* 素材清单：static/assets/char/manifest.json。容忍几种写法
   （actions{} / sequences[] / 直接「动作名 → 帧数组」），
   这样美术那边换命名不用改前端。没有 manifest 就一切走旧路径。 */
const SEQ_BASE = "/static/assets/char/";

function seqAbsUrl(p) {
  if (!p) return "";
  const s = String(p);
  if (/^(https?:)?\//.test(s) || s.startsWith("data:")) return s;
  return SEQ_BASE + s.replace(/^\.?\//, "");
}

async function loadSeqManifest() {
  let m;
  try {
    const r = await fetch(SEQ_BASE + "manifest.json", { cache: "no-store" });
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
    // 清单里有、ASSETS 里没登记的（v8 多出来的 look_door / check_phone 等）也收下，
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
    // 清单里给多少都只当参考：帧间淡入超过 60ms 就会「两张半透明同时挂着」（见 frameFade）
    XFADE = Math.max(0, Math.min(60, m.playback.crossfadeMs));
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
    // 动作没播完就换表情：先收序列层，别让它盖住新表情
    seqStop();
    showSprite(ASSETS.char[emotion] || ASSETS.char.neutral, SPRITE_MS);
    return;
  }
  // neutral→smile 这类组合有专门的过渡帧（2×70ms），比纯叠化更像「表情动了一下」
  if (prev && prev !== emotion && TRANS[prev + "->" + emotion]) {
    if (playTransition(prev + "->" + emotion, emotion)) return;
  }
  /* 时长见 SPRITE_MS（2026-10-20 实测改定）：13 张贴片是 13 张**不同的画**
     （raw mad 61~80/255，头/肩位置差最多 42px），所以叠化时长就等于"双影窗口"长度。
     380ms（修 transition 之前实际生效的值）→ 13~23 帧双影，用户报的"重影"就是它；
     80ms → 5 帧，边缘只轻微柔化，读成"她换了表情"而不是"两个人叠在一起"。 */
  showSprite(ASSETS.char[emotion] || ASSETS.char.neutral, SPRITE_MS);
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

/* 卡包声明 8 个动作，贴片现在 8 张都有（look_door / check_phone 于 2026-10-20 补齐），
   所以这里不再做近似映射。缺图时下面的 `if (!pose) return;` 会静默跳过，
   回退逻辑在服务端/卡包层，不在客户端猜。 */

function applyAction(action, holdMs, silent) {
  if (action === "idle") return;
  breathStop();          // 动作要占用序列层，先把待机呼吸循环让出来
  if (!silent) chip(LABEL.action[action] || action);
  const stage = $("stage");
  const hold = Number(holdMs) || 4200;
  if (action === "look_window" || action === "look_door") stage.dataset.cam = "push";
  if (action === "lean_in") stage.dataset.cam = "close";
  if (action === "stand") {
    stage.dataset.cam = "push";
    setTimeout(() => (stage.dataset.cam = ""), Math.min(hold, 3200));
  }
  if (action === "sip_coffee" || action === "check_phone") {
    stage.dataset.cam = "push";
    setTimeout(() => (stage.dataset.cam = ""), Math.min(hold, 2600));
  }
  if (action === "look_window" || action === "look_door")
    setTimeout(() => (stage.dataset.cam = ""), Math.min(hold + 600, 4000));

  MOTION.settleUntil = performance.now() + 260;
  S.poseUntil = Date.now() + hold;
  clearTimeout(applyAction._t);
  /* 有多帧序列（清单里有 seq 的动作）就走序列：帧与帧之间是硬切，动作才有方向感；
     没有就退回原来的单张姿势 + 240ms 叠化（旧素材的行为完全不变）。 */
  if (playSequence(action, { holdMs: hold })) return;
  const pose = ASSETS.pose[action];
  if (!pose) return;
  /* 姿势互换是两个不同剪影，380/240ms 叠化会先出现「两张半透明的人」（实测截到过），
     读起来是闪不是动。压到 150ms：肉眼只看到一次交接，看不到双影。 */
  showSprite(pose, SPRITE_MS);
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
      /* 动作贴片的姿势是个剪影差异很大的另一张画，收尾要回到中性脸，
         否则她会一直保持"举着相机"的姿势。 */
      showSprite(ASSETS.char[S.emotion] || ASSETS.char.neutral, SPRITE_MS);
    }
  }, hold);
}

function applyEffect(effect) {
  const rain = $("rain");
  if (effect === "none") rain.classList.remove("on");
  else rain.classList.add("on");
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
 *   photo_reveal → 角色贴片换成 show_photo + 照片静帧推近 + 徽标
 *   last_train   → 窗外车灯横扫（一条 CSS 光带）+ 远处低频轰鸣
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

function carSweep() {
  const s = $("sweep");
  if (!s) return;
  s.classList.remove("on");
  void s.offsetWidth;
  s.classList.add("on");
  clearTimeout(carSweep._t);
  carSweep._t = setTimeout(() => s.classList.remove("on"), 2900);
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
    degradeEvent("photo_reveal", "该事件没有静帧");
    return;
  }
  cinema.classList.add("show");
  cinema.classList.remove("fallback", "still-ready");
  $("cinemaBadge").textContent = "静帧加载中…";
  $("cinemaBadge").classList.add("show");
  S.mediaEventActive = true;
  still.onerror = () => {
    S.errors += 1;
    degradeEvent("photo_reveal", "静帧加载失败");
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
  if (kind === "last_train") {
    carSweep();
    rumble(0.5, 2.8);
    chip(badge);
    return;
  }
  // photo_reveal（以及以后可能加的事件）：有静帧就走静帧，没有就走最简演出
  const src = (ASSETS.still && ASSETS.still[kind]) || ASSETS.insert.polaroid;
  showStill(src, badge, 4600);
}

function degradeEvent(kind, reason) {
  toast(`${reason}：改为最简演出`);
  if (kind === "thunder") {
    doFlash();
    hideCinema();
    return;
  }
  if (kind === "last_train") {
    carSweep();
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
    if (v === "window") showSprite(ASSETS.pose.look_window, SPRITE_MS);
    else if (v === "away") showSprite(ASSETS.char.thinking, SPRITE_MS);
    else showSprite(ASSETS.char[S.emotion] || ASSETS.char.neutral, SPRITE_MS);
    if (v !== "user") {
      PERF.gazeTimer = setTimeout(() => {
        showSprite(ASSETS.char[S.emotion] || ASSETS.char.neutral, SPRITE_MS);
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
      showSprite(ASSETS.char[S.emotion] || ASSETS.char.neutral, SPRITE_MS);
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
};

function streamReset(seq) {
  STREAM.log = [];
  STREAM.seq = seq;
  STREAM.items = [];
  if (STREAM.abort) { try { STREAM.abort.abort(); } catch (e) {} STREAM.abort = null; }
  STREAM.sources.forEach((s) => { try { s.stop(0); } catch (e) {} });
  STREAM.sources = [];
  STREAM.endTime = 0;
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

/* 流式 PCM 的级别补偿（-18.8dB）。
   为什么需要：edge 的裸音频几乎是满刻度（实测 RMS ≈ -1.8dBFS），而整句 mp3 那条路
   在服务端 postprocess.finalize 里被归一化到 **-20dBFS**。两边差 18.7dB —— 不补的话
   第一句会明显比后面几句响一大截，切换点比没有混响更容易被听出来。
   取值依据：tools/tts/render_ab_reverb.py 实测 delta = 18.66dB（同文本同音色）。
   这是固定值：服务端是逐句归一化，所以句与句之间仍有 ±2dB 浮动（见文档 §6）。 */
const ROOM_GAIN = 0.115;

/* 流式 PCM 的**音色**补偿（-10dB @ 3kHz 高架）。
   为什么需要（2026-10-21 08:1x 同文本实测，tools/tts 外的新证据在 docs/review/p4_verify/）：
   同一句台词分别走两条路 —— `/api/tts_file`（整句 mp3 + 服务端 postprocess）与
   `/api/tts_stream`（live 长连，裸 PCM）—— 拿到音频后按 1/3 倍频程比较长时谱，
   两条的**频谱倾斜差了 4.7dB/oct**（-8.14 vs -3.45），折算成带内差：
   1–3k +2.4dB、3–5k +6.7、5–7k +11.3、7–9k +12.9、9–11k +15.5。
   也就是说：第一句明显比后面几句**亮/脆**，而服务端 DSP 那条路被压得很暗。
   一条回复里第一句亮、第二句起发闷，比"整体偏闷"更容易被听出来（切换点）。
   这里用一级 high-shelf 把流式路径压到与 DSP 路径同谱：实测带内残差从
   +15.5dB 收到 ±3.3dB（1–9kHz，见同目录 timbre_match 的两个 wav 与报告 §6）。
   另一个方向（把 DSP 那条路提亮）会改动所有句子的音色，属于"音色归用户判断"的范围，
   所以默认不动它；要试就在 postprocess.finalize 里放宽低通/去掉 dulling。 */
const ROOM_SHELF = { f: 3000, gainDb: -10 };

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
        // 第一块到就开始"说话"：字幕、状态、表演层锚点都在这一刻对齐
        setState("speaking");
        setCaption(item.text, false);
        perfSentStart(item.index, item.estMs || 0);
        S.firstChunkMs = Math.round(firstChunkAt - t0);
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
      const bus = roomBus();       // 走混响 bus（客户端补的 0.55s 小房间，见 roomBus）
      src.connect(bus || AMP.an);
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
    console.warn(`[tts] 流只到 ${(rendered * 1000) | 0}ms / 估 ${est}ms，改整句 mp3`);
    return false;
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

async function sendTurn(text, kind = "user") {
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
      $("subtitle").classList.remove("narration");
      if (S.state !== "speaking") setCaption(ev.text, true);
      break;
    case "reply":
      $("subtitle").classList.remove("narration");
      if (S.state !== "speaking") setCaption(ev.text, false);
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
      toast(ev.message || "语音不可用，已降级为字幕");
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
    case "done":
      S.audioDone = true;
      S.lastTotal = ev.ms;
      S.beat = ev.beat ?? S.beat;
      /* 这一轮真正是谁说的话：card（卡包定稿）/ strata / llama / mock。
         掉到 mock 时界面必须看得见——静默降级会让一整场演示"看起来在跑模型"。 */
      if (ev.provider && ev.provider !== S.provider) {
        S.provider = ev.provider;
        addMetric("模型", ev.provider);
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
      showSprite(ASSETS.char.listen, SPRITE_MS);
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
      toast(`${key} → ${next ? "开" : "关"}`);
    };
  });

  const ambBtn = $("ambToggle");
  if (ambBtn) {
    ambBtn.onclick = () => {
      const next = !AMB.on;
      ambienceSet(next);
      ambBtn.classList.toggle("on", next);
      toast(next ? "环境音：开（雨后的房间底噪）" : "环境音：关");
    };
  }

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
  $("sampleVoice").onclick = injectSample("/static/assets/audio/sample_user.wav");
  $("sampleVoice2").onclick = injectSample("/static/assets/audio/sample_user_2.wav");
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
      const actual = h.mode === "auto"
        ? `auto（实际链：${chainText(h)}）`
        : (SERVER_MODE_LABEL[h.mode] || h.mode);
      srv.textContent = `服务端实际 provider：${actual} · llm=${h.llm || "-"}` +
        (S.cloudConfigured ? " · DeepSeek 已配置 key" : " · DeepSeek 未配置 key");
      // auto/mock/strata 都不在前端两档里：标黄提醒「你选的本地档 ≠ 服务端实际档」
      srv.classList.toggle("warn", h.mode !== "llama" && h.mode !== "cloud");
    }
    const note = document.querySelector(".gate-note");
    if (note && h.mode === "mock") {
      note.textContent = "服务端当前是 Mock 模式（本地模型没起也能完整体验）· 点麦克风可打断她";
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
$("plateA").src = ASSETS.plate.cafe_night;
$("plateA").classList.add("is-front");
$("charA").src = ASSETS.char.neutral;
$("charA").classList.add("is-front");
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
  /* QA 用：历史面板（tools 里可直接断言 transcript 有没有落、台词里有没有 JSON 头） */
  history: HIST, refreshHistory, exportHistory,
  /* QA 用：演出层（没有视频了，这三个是现在的全部入口，见 tools/qa/event_shots.py） */
  triggerEvent, hideCinema, showStill, carSweep, rumble,
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
