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
  video: {
    thunder: "/static/assets/video/thunder.mp4",
    photo_reveal: "/static/assets/video/photo_reveal.mp4",
    last_train: "/static/assets/video/last_train.mp4",
  },
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
  mode: "llama",
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
  videoFail: false,
  ttsFallback: false,
  mediaEventActive: false,
  recEnabled: new URLSearchParams(location.search).get("rec") === "1",
};

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

function setState(name) {
  S.state = name;
  $("app").dataset.state = name;
  $("stateLabel").textContent =
    { idle: "待机", listening: "倾听", thinking: "思考", speaking: "说话" }[name] || name;
  const hints = {
    idle: "点一下麦克风说话 · 说完停顿一下自动发送",
    listening: "正在录… 再点一下发送（停顿 1 秒也会自动发）",
    thinking: "正在想怎么回答…",
    speaking: "点一下麦克风可以打断她",
  };
  if (!MIC.rec) $("hint").textContent = hints[name] || "";
  /* 状态要能从画面上看出来，而不只是顶部标签：
     倾听 / 思考 用对应贴片；说话 / 待机回到当前情绪贴片。 */
  if (name === "listening") showSprite(ASSETS.char.listen);
  else if (name === "thinking") showSprite(ASSETS.char.thinking);
  else if (name === "speaking" || name === "idle") {
    // 动作贴片（sip_coffee / show_photo …）还在时效内就别抢它的画面
    if (Date.now() >= (S.poseUntil || 0)) showSprite(ASSETS.char[S.emotion] || ASSETS.char.neutral);
  }
  updateIdleLoop();
}

/* 待机氛围循环（idle_loop.mp4）只有雨、灯光、窗外车流，角色是独立贴片 */
function updateIdleLoop() {
  const v = $("bgLoop");
  const shouldShow = !S.mediaEventActive;
  if (shouldShow) {
    if (!v.classList.contains("on")) {
      v.classList.add("on");
      v.play().catch(() => {});
    }
  } else if (v.classList.contains("on")) {
    v.classList.remove("on");
    setTimeout(() => {
      if (!v.classList.contains("on")) v.pause();
    }, 950);
  }
}

function preload() {
  Object.values({ ...ASSETS.char, ...ASSETS.pose, ...ASSETS.plate, ...ASSETS.insert }).forEach((src) => {
    const img = new Image();
    img.src = src;
  });
  const v = document.createElement("video");
  v.preload = "metadata";
  v.src = ASSETS.video.thunder;
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

/* 换表情/姿势只切这一层：硬切，避免两张不同姿态交叉淡化时出现双影 */
function showSprite(src) {
  const front = S.spriteFront === "A" ? $("charA") : $("charB");
  const back = S.spriteFront === "A" ? $("charB") : $("charA");
  if (front.getAttribute("src") === src) return;
  back.onload = () => {
    back.classList.add("is-front");
    front.classList.remove("is-front");
    S.spriteFront = S.spriteFront === "A" ? "B" : "A";
  };
  back.classList.remove("is-front");
  back.src = src;
}

function applyEmotion(emotion) {
  S.emotion = emotion;
  showSprite(ASSETS.char[emotion] || ASSETS.char.neutral);
}

function applyScene(scene) {
  S.scene = scene;
  const stage = $("stage");
  $("dim").classList.toggle("lights-dim", scene === "lights_dim");
  stage.dataset.cam = scene === "window_close" ? "close" : "";
  showPlate(ASSETS.plate.cafe_night);
  chip(LABEL.scene[scene] || "");
  updateIdleLoop();
}

function chip(text) {
  const el = $("actionChip");
  if (!text) return;
  el.textContent = text;
  el.classList.add("show");
  clearTimeout(chip._t);
  chip._t = setTimeout(() => el.classList.remove("show"), 2600);
}

function applyAction(action) {
  if (action === "idle") return;
  chip(LABEL.action[action] || action);
  const stage = $("stage");
  if (action === "look_window") stage.dataset.cam = "push";
  if (action === "lean_in") stage.dataset.cam = "close";
  if (action === "stand") {
    stage.dataset.cam = "push";
    setTimeout(() => (stage.dataset.cam = ""), 3200);
  }
  if (action === "sip_coffee") {
    stage.dataset.cam = "push";
    setTimeout(() => (stage.dataset.cam = ""), 2600);
  }
  if (action === "look_window") setTimeout(() => (stage.dataset.cam = ""), 4000);

  const pose = ASSETS.pose[action];
  if (!pose) return;
  showSprite(pose);
  S.poseUntil = Date.now() + 4200;
  clearTimeout(applyAction._t);
  applyAction._t = setTimeout(() => {
    showSprite(ASSETS.char[S.emotion] || ASSETS.char.neutral);
  }, 4200);
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

/* ------------------------------------------------------------ 视频演出层 */

function hideCinema() {
  const cinema = $("cinema");
  const video = $("eventVideo");
  clearTimeout(triggerEvent._t);
  video.onerror = null;
  video.oncanplay = null;
  video.onended = null;
  cinema.classList.remove("show", "video-ready", "fallback");
  video.pause();
  video.removeAttribute("src");
  video.load();
  $("cinemaBadge").classList.remove("show");
  S.mediaEventActive = false;
  updateIdleLoop();
  if (S.audioDone && !S.playing && !S.queue.length) setState("idle");
}

function showInsertImage(src, badge) {
  const cinema = $("cinema");
  const video = $("eventVideo");
  cinema.classList.add("show", "fallback");
  video.pause();
  video.removeAttribute("src");
  video.poster = src;
  video.load();
  $("cinemaBadge").textContent = badge;
  $("cinemaBadge").classList.add("show");
  S.mediaEventActive = true;
  setTimeout(() => {
    if (S.mediaEventActive) hideCinema();
  }, 4200);
}

function triggerEvent(kind) {
  if (!kind || kind === "none") return;
  if (kind === "thunder") doFlash();
  const badge = LABEL.event[kind] || kind;

  if (S.videoFail) {
    S.errors += 1;
    degradeEvent(kind, "视频素材加载失败（注入故障）");
    return;
  }
  const src = ASSETS.video[kind];
  if (!src) {
    degradeEvent(kind, "该事件没有视频素材");
    return;
  }
  const cinema = $("cinema");
  const video = $("eventVideo");
  const badgeEl = $("cinemaBadge");
  cinema.classList.add("show");
  cinema.classList.remove("fallback", "video-ready");
  badgeEl.textContent = "素材加载中…";
  badgeEl.classList.add("show");
  S.mediaEventActive = true;
  const bgLoop = $("bgLoop");
  bgLoop.classList.remove("on");
  bgLoop.pause();

  const onError = () => {
    if (!S.mediaEventActive) return;
    S.errors += 1;
    degradeEvent(kind, "视频加载失败，已降级");
  };
  video.onerror = onError;
  video.oncanplay = () => {
    cinema.classList.add("video-ready");
    badgeEl.textContent = badge;
    video.play().catch(() => {});
    setTimeout(() => badgeEl.classList.remove("show"), 1200);
  };
  video.onended = () => {
    $("cinemaBadge").classList.remove("show");
    hideCinema();
  };
  video.src = src;
  video.load();
  clearTimeout(triggerEvent._t);
  triggerEvent._t = setTimeout(() => {
    if (!cinema.classList.contains("video-ready")) onError();
  }, 6000);
}

function degradeEvent(kind, reason) {
  const map = { photo_reveal: ASSETS.insert.polaroid, last_train: ASSETS.insert.polaroid, thunder: null };
  toast(`${reason}：改为静态演出`);
  if (kind === "thunder") {
    doFlash();
    hideCinema();
    return;
  }
  showInsertImage(map[kind] || ASSETS.insert.polaroid, "静态降级演出");
}

/* ---------------------------------------------------------------- 音频播放 */

function stopPlayback() {
  S.queue = [];
  if (S.playing) {
    try {
      S.playing.onended = null;
      S.playing.onerror = null;
      S.playing.pause();
      S.playing.currentTime = 0;
    } catch (e) {}
    S.playing = null;
  }
  if (window.speechSynthesis) window.speechSynthesis.cancel();
}

function enqueueAudio(url, text) {
  S.queue.push({ url, text });
  if (!S.playing) playNext();
}

function playNext(seq) {
  if (seq !== undefined && seq !== S.activeSeq) return;
  const item = S.queue.shift();
  if (!item) {
    if (S.audioDone) {
      setState("idle");
      setCaption("");
    }
    return;
  }
  const audio = new Audio(item.url);
  audio.preload = "auto";
  audio.crossOrigin = "anonymous";
  recRoute(audio);
  S.playing = audio;
  audio.onplaying = () => {
    if (seq !== undefined && seq !== S.activeSeq) return;
    setState("speaking");
    setCaption(item.text, false);
  };
  audio.onended = () => {
    S.playing = null;
    playNext(seq);
  };
  audio.onerror = () => {
    S.playing = null;
    S.errors += 1;
    setCaption(item.text, false);
    speakFallback(item.text);
    playNext(seq);
  };
  audio.play().catch(() => {
    setCaption(item.text, false);
    S.playing = null;
    playNext(seq);
  });
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
  S.mode = data.mode;
  $("modeSelect").value = data.mode;
  return data;
}

async function sendTurn(text, kind = "user") {
  if (!S.sid) await startSession(S.mode);
  let seq = ++S.seq;
  S.activeSeq = seq;
  S.audioDone = false;
  S.errors = 0;
  stopPlayback();
  hideCinema();
  clearSuggest();
  setCaption("", false);
  setState("thinking");
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
      if (S.state !== "speaking") setCaption(ev.text, true);
      break;
    case "reply":
      if (S.state !== "speaking") setCaption(ev.text, false);
      break;
    case "directive":
      applyDirective(ev.directive, ev.fixes || []);
      break;
    case "audio":
      if (!S.lastFirstAudio) {
        const ms = Math.round(performance.now() - t0);
        S.lastFirstAudio = ms;
        $("latency").textContent = `首包 ${ms}ms`;
        addMetric("首包", `${ms}ms`);
      }
      enqueueAudio(ev.url, ev.text);
      break;
    case "audio_error":
      S.errors += 1;
      toast(ev.message || "语音不可用，已降级为字幕");
      setCaption(ev.text, false);
      speakFallback(ev.text);
      break;
    case "suggest":
      showSuggest(ev.items || []);
      break;
    case "done":
      S.audioDone = true;
      S.lastTotal = ev.ms;
      S.beat = ev.beat ?? S.beat;
      $("latency").textContent =
        `首包 ${ev.first_audio_ms ?? S.lastFirstAudio ?? "-"}ms · 总 ${ev.ms}ms`;
      if (!S.playing && S.queue.length === 0) setState("idle");
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

/* ---------------------------------------------------------------- 打断 */

async function interrupt(reason = "voice") {
  const prev = S.activeSeq;
  S.activeSeq = ++S.seq;
  stopPlayback();
  hideCinema();
  setCaption("");
  $("actionChip").classList.remove("show");
  setState("listening");
  if (!S.sid) return;
  try {
    await api("/api/interrupt", { session_id: S.sid, seq: prev, mode: S.mode, reason });
  } catch (e) {}
}

/* ---------------------------------------------------------------- 麦克风 */

/* 手势设计（之前「点一下就松手」只能录到 50ms 空音频，用户会以为话筒坏了）：
   - 轻点一下：开始录音，免按住；说完停顿 1.1s 自动发送
   - 按住超过 350ms：按住说话，松手发送
   - 录音中再点一下：立即结束并发送
   - 最长 15s 自动截断，最短 0.35s 以下提示重说 */
const MIC = {
  rec: null, stream: null, chunks: [], ac: null, analyser: null, raf: 0,
  startedAt: 0, pressAt: 0, speechAt: 0, lastVoice: 0,
};

async function startMic() {
  if (MIC.rec) return;
  if (S.state === "speaking" || S.state === "thinking") await interrupt("voice");
  setState("listening");
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
  } catch (err) {
    setState("idle");
    let why = (err && err.name) || (err && err.message) || "未知错误";
    if (why === "NotAllowedError") why = "被浏览器拒绝（点地址栏左侧图标，允许麦克风）";
    else if (why === "NotFoundError") why = "没有找到麦克风设备";
    else if (!navigator.mediaDevices) why = "需要 http://127.0.0.1 或 https 打开（安全上下文）";
    toast(`麦克风不可用：${why}。也可以直接打字。`, 6500);
    addMetric("麦克风", String(why));
    return;
  }
  MIC.stream = stream;
  MIC.chunks = [];
  const mime = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
    ? "audio/webm;codecs=opus"
    : "audio/webm";
  const rec = new MediaRecorder(stream, { mimeType: mime });
  rec.ondataavailable = (e) => e.data.size && MIC.chunks.push(e.data);
  rec.onstop = async () => {
    const chunks = MIC.chunks;
    MIC.rec = null;
    endMeter();
    if (stream.getTracks) stream.getTracks().forEach((t) => t.stop());
    $("micBtn").classList.remove("recording");
    const dur = (Date.now() - MIC.startedAt) / 1000;
    if (!chunks.length || dur < 0.35) {
      setState("idle");
      toast("没录到声音（太短）。点一下麦克风，说完再点一下发送。");
      return;
    }
    await transcribeAndSend(new Blob(chunks, { type: mime.split(";")[0] }), dur);
  };
  MIC.rec = rec;
  MIC.startedAt = Date.now();
  MIC.speechAt = 0;
  MIC.lastVoice = 0;
  rec.start(120);
  $("micBtn").classList.add("recording");
  startMeter(stream);
}

function stopMic() {
  if (MIC.rec && MIC.rec.state !== "inactive") MIC.rec.stop();
}

/* 电平表 + 静音自动结束：说话后连续 1.1s 静音就自动发送 */
function startMeter(stream) {
  try {
    const AC = window.AudioContext || window.webkitAudioContext;
    const ac = new AC();
    const src = ac.createMediaStreamSource(stream);
    const an = ac.createAnalyser();
    an.fftSize = 1024;
    src.connect(an);
    MIC.ac = ac;
    MIC.analyser = an;
    const buf = new Float32Array(an.fftSize);
    const tick = () => {
      if (!MIC.analyser) return;
      an.getFloatTimeDomainData(buf);
      let sum = 0;
      for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
      const rms = Math.sqrt(sum / buf.length);
      $("micBtn").style.setProperty("--lvl", Math.min(1, rms * 14).toFixed(3));
      const now = Date.now();
      if (rms > 0.012) {
        MIC.lastVoice = now;
        if (!MIC.speechAt) MIC.speechAt = now;
      }
      const secs = ((now - MIC.startedAt) / 1000).toFixed(1);
      $("hint").textContent = `正在录 ${secs}s · 说完停顿一下自动发送，再点一下也可以`;
      if (MIC.speechAt && now - MIC.lastVoice > 1100) { stopMic(); return; }
      if (now - MIC.startedAt > 15000) { toast("已录 15 秒，自动发送"); stopMic(); return; }
      MIC.raf = requestAnimationFrame(tick);
    };
    MIC.raf = requestAnimationFrame(tick);
  } catch (e) {
    /* 没有 AudioContext 也能录音，只是少一个电平表 */
  }
}

function endMeter() {
  cancelAnimationFrame(MIC.raf);
  MIC.raf = 0;
  if (MIC.ac) { try { MIC.ac.close(); } catch (e) {} }
  MIC.ac = null;
  MIC.analyser = null;
  $("micBtn").style.removeProperty("--lvl");
}

async function transcribeAndSend(blob) {
  setState("thinking");
  $("hint").textContent = "识别中…";
  const fd = new FormData();
  fd.append("audio", blob, "utterance.webm");
  fd.append("session_id", S.sid || "");
  const t0 = performance.now();
  try {
    const res = await fetch("/api/stt", { method: "POST", body: fd });
    if (!res.ok) throw new Error(`STT HTTP ${res.status}`);
    const data = await res.json();
    const ms = Math.round(performance.now() - t0);
    addMetric("STT", `${data.text} · ${ms}ms`);
    if (!data.text) {
      setState("idle");
      toast("没听清，再说一次？");
      return;
    }
    $("hint").textContent = `听到：「${data.text}」`;
    await sendTurn(data.text, "user");
  } catch (err) {
    setState("idle");
    toast(`语音识别失败：${err.message}。可以直接打字。`);
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
      await startSession($("modeSelect").value || "llama");
    } catch (e) {
      toast("后端未启动，请先运行 start.ps1");
      return;
    }
    $("gate").classList.add("hide");
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
    if (!MIC.rec) startMic();
    else if (Date.now() - MIC.startedAt > 300) stopMic();
  });
  mic.addEventListener("pointerup", () => {
    const held = Date.now() - (MIC.pressAt || 0);
    if (MIC.rec && held > 350) stopMic();
  });
  mic.addEventListener("pointercancel", () => {
    if (MIC.rec) stopMic();
  });

  $("debugBtn").onclick = () => {
    const p = $("debugPanel");
    p.hidden = !p.hidden;
    $("debugBtn").classList.toggle("on", !p.hidden);
    if (!p.hidden) refreshEvents();
  };
  $("debugClose").onclick = () => {
    $("debugPanel").hidden = true;
    $("debugBtn").classList.remove("on");
  };
  setInterval(() => {
    if (!$("debugPanel").hidden) refreshEvents();
  }, 1500);

  $("modeSelect").onchange = async (e) => {
    S.mode = e.target.value;
    S.seq = 0;
    S.activeSeq = 0;
    await startSession(S.mode);
    toast(`已切换 provider：${S.mode}`);
    await sendTurn("", "open");
  };

  document.querySelectorAll("[data-inject]").forEach((btn) => {
    btn.onclick = async () => {
      const key = btn.dataset.inject;
      if (key === "video_fail") {
        S.videoFail = !S.videoFail;
        btn.classList.toggle("on", S.videoFail);
        toast(S.videoFail ? "已注入：视频素材加载失败" : "已恢复视频素材");
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

bind();
setState("idle");
$("plateA").src = ASSETS.plate.cafe_night;
$("plateA").classList.add("is-front");
$("charA").src = ASSETS.char.neutral;
$("charA").classList.add("is-front");
if (S.recEnabled) recInit();
window.__mira = { S, sendTurn, interrupt, setState, applyDirective, startSession };
