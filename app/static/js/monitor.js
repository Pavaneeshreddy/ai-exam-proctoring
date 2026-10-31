/* Proctoring client for the exam room.
 *
 *  vision loop  (~6 fps, on device)  face count, presence, head pose, gaze, repeated turns
 *  object loop  (~0.7 fps, on device) phones, books, screens, extra people
 *  frame upload (every N s)          server re-verifies identity (same person as the start photo), faces, YOLOv8
 *  audio loop   (10 Hz, on device)   calibrated voice activity + pitch track, summaries analysed by the server
 *  browser      (events)             tab switch, focus, full-screen, clipboard, shortcuts, devtools, monitors
 *  heartbeat    (every 5 s)          server clock, proctor messages, termination
 */
(function () {
  const cfg = window.ROOM;
  const urls = cfg.urls;
  const SEVERE = new Set(['multiple_faces', 'phone_detected', 'book_detected', 'extra_device', 'multiple_persons', 'left_camera_view', 'face_missing', 'camera_blocked', 'head_turned', 'repeated_head_movement', 'tab_switch', 'fullscreen_exit', 'devtools_open']);
  const CLIENT_COOLDOWN = 8000;
  const written = cfg.examMode === 'written';

  // what the student is told for each violation
  const WARN_TEXT = {
    face_missing: 'Your face is not visible. Stay in front of the camera.',
    left_camera_view: 'You left the camera view.',
    multiple_faces: 'Another face is visible. You must be alone.',
    multiple_persons: 'Another person was detected near you.',
    head_turned: 'Face the screen. Do not turn your head away.',
    repeated_head_movement: 'Stop turning your head repeatedly.',
    gaze_away: 'Keep your eyes on the screen.',
    phone_detected: 'Mobile phone detected. Put it away immediately.',
    book_detected: 'Book or notes detected. Remove them from view.',
    extra_device: 'Another screen or device was detected.',
    camera_blocked: 'Your camera is covered or blocked.',
    camera_disabled: 'Your camera was disconnected.',
    tab_switch: 'You switched away from the exam tab.',
    focus_lost: 'The exam window lost focus.',
    fullscreen_exit: 'You left full-screen mode.',
    clipboard: 'Copy and paste are not allowed.',
    devtools_open: 'Developer tools are not allowed.',
    multiple_monitors: 'Disconnect additional monitors.',
    screenshot_attempt: 'Screenshots are not allowed.',
    identity_mismatch: 'The person in front of the camera does not match the start photo.',
    voice_detected: 'Speaking was detected. Remain silent.',
    multiple_voices: 'A conversation was detected. You must be alone and silent.',
  };

  const listeners = {};
  const on = (name, fn) => { (listeners[name] = listeners[name] || []).push(fn); };
  const emit = (name, data) => (listeners[name] || []).forEach((fn) => fn(data));

  const state = {
    running: false,
    stream: null,
    video: null,
    landmarker: null,
    detector: null,
    lastSent: {},
    queue: [],
    noFaceSince: null, absenceReported: false, longAbsenceReported: false,
    multiSince: null,
    awaySince: null, gazeSince: null,
    turnLog: [], wasTurned: false,
    baseline: { yaw: 0, pitch: 0, n: 0 },
    objectHits: {},
    lastObjects: [],
    health: {},
  };

  function setHealth(key, level, title) {
    state.health[key] = level;
    const el = document.querySelector(`[data-h="${key}"]`);
    if (el) { el.className = `h ${level}`; if (title) el.title = title; }
  }

  // ------------------------------------------------------------- reporting
  function report(type, confidence = 0.8, details = {}, withImage = SEVERE.has(type)) {
    const now = Date.now();
    if (state.lastSent[type] && now - state.lastSent[type] < CLIENT_COOLDOWN) return;
    state.lastSent[type] = now;
    const ev = { type, confidence, details, at: new Date().toISOString() };
    if (withImage) ev.image = window.Vision.captureFrame(state.video, { maxWidth: 640, quality: 0.7 });
    state.queue.push(ev);
    emit('event', ev);
    if (WARN_TEXT[type]) emit('violation', { type, text: WARN_TEXT[type] });
    flush();
  }

  let flushing = false;
  async function flush() {
    if (flushing || !state.queue.length || !navigator.onLine) return;
    flushing = true;
    const batch = state.queue.splice(0, 10);
    try {
      const r = await App.api(urls.events, { events: batch }, { allowError: true });
      if (r && r.status && r.status !== 'in_progress') emit('status', r);
      if (r && r.risk) emit('risk', r);
      if (r && r.max_violations !== undefined) emit('strikes', r);
    } catch (e) {
      state.queue.unshift(...batch); // keep for retry when the network returns
    } finally {
      flushing = false;
      if (state.queue.length && navigator.onLine) setTimeout(flush, 1500);
    }
  }

  // ---------------------------------------------------------------- camera
  async function openStream() {
    const s = await navigator.mediaDevices.getUserMedia({
      video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'user' },
      audio: cfg.monitorAudio ? { echoCancellation: true, noiseSuppression: false, autoGainControl: false } : false,
    });
    state.stream = s;
    state.video.srcObject = s;
    await state.video.play();
    const vt = s.getVideoTracks()[0];
    vt.addEventListener('ended', () => cameraLost('ended'));
    vt.addEventListener('mute', () => setTimeout(() => { if (vt.muted) cameraLost('muted'); }, 3000));
    const at = s.getAudioTracks()[0];
    if (at) {
      at.addEventListener('ended', () => report('mic_disabled', 0.95, { reason: 'ended' }, false));
      at.addEventListener('mute', () => setTimeout(() => { if (at.muted) report('mic_disabled', 0.9, { reason: 'muted' }, false); }, 3000));
    }
    emit('camera', { ok: true });
    return s;
  }

  function cameraLost(reason) {
    report('camera_disabled', 0.95, { reason }, false);
    setHealth('face', 'bad', 'Camera disconnected');
    emit('camera', { ok: false, reason });
  }

  async function reconnectCamera() {
    if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
    await openStream();
    if (cfg.monitorAudio) startAudio();
  }

  // ---------------------------------------------------------------- vision
  async function loadModels() {
    const label = document.getElementById('ai-mode');
    try {
      state.landmarker = await window.Vision.loadFace(4);
      label.textContent = 'On-device + server AI';
    } catch (e) {
      label.textContent = 'Server AI only';
    }
    if (cfg.monitorObjects) {
      try { state.detector = await window.Vision.loadObjects(); } catch (e) { /* server YOLOv8 still runs */ }
    }
  }

  function visionTick() {
    if (!state.running) return;
    const v = state.video;
    if (state.landmarker && v.readyState >= 2) {
      try {
        const ts = performance.now();
        const r = window.Vision.analyzeFaces(state.landmarker, v, ts);
        handleFaces(r);
        window.Vision.drawOverlay(document.getElementById('cam-overlay'), v, r.faces, state.lastObjects, { away: !!state.awaySince });
      } catch (e) { /* dropped frame */ }
    }
    setTimeout(visionTick, 160);
  }

  function handleFaces(r) {
    const now = Date.now();
    // presence
    if (r.count === 0) {
      state.noFaceSince = state.noFaceSince || now;
      const gone = now - state.noFaceSince;
      if (gone > 3000 && !state.absenceReported) { state.absenceReported = true; report('face_missing', 0.85, { seconds: 3 }); }
      if (gone > 15000 && !state.longAbsenceReported) { state.longAbsenceReported = true; report('left_camera_view', 0.9, { seconds: Math.round(gone / 1000) }); }
      state.lastFace = null; state.lastCount = 0;
      setHealth('face', 'bad', 'No face visible');
      setHealth('gaze', 'warn');
      state.multiSince = null; state.awaySince = null; state.gazeSince = null;
      return;
    }
    if (state.noFaceSince && state.absenceReported) emit('notice', { kind: 'info', text: `Face back in view after ${Math.round((now - state.noFaceSince) / 1000)} s.` });
    state.noFaceSince = null; state.absenceReported = false; state.longAbsenceReported = false;

    // multiple faces
    if (r.count > 1) {
      state.multiSince = state.multiSince || now;
      if (now - state.multiSince > 1500) report('multiple_faces', 0.9, { count: r.count });
      setHealth('face', 'bad', `${r.count} faces visible`);
    } else {
      state.multiSince = null;
      setHealth('face', 'ok', 'One face visible');
    }

    const f = r.faces[0];
    state.lastCount = r.count;
    if (f.box.w < 0.12) report('face_too_far', 0.6, { width: +f.box.w.toFixed(3) }, false);

    // personal baseline for the first ~80 frontal samples (people sit at different angles)
    const b = state.baseline;
    if (b.n < 80 && Math.abs(f.yaw) < 25 && Math.abs(f.pitch) < 25) {
      b.yaw = (b.yaw * b.n + f.yaw) / (b.n + 1); b.pitch = (b.pitch * b.n + f.pitch) / (b.n + 1); b.n += 1;
    }
    const yaw = f.yaw - b.yaw;
    const pitch = f.pitch - b.pitch;
    state.lastFace = { yaw: Math.round(yaw), pitch: Math.round(pitch) };

    // head pose: sideways always counts; looking down is normal while writing on paper
    const sideways = Math.abs(yaw) > 22;
    const up = pitch < -20;
    const down = !written && pitch > 28;
    const turned = sideways || up || down;
    if (turned) {
      state.awaySince = state.awaySince || now;
      if (now - state.awaySince > 1500) report('head_turned', 0.75, { yaw: Math.round(yaw), pitch: Math.round(pitch), direction: sideways ? (yaw > 0 ? 'side-a' : 'side-b') : up ? 'up' : 'down' });
    } else state.awaySince = null;

    // repeated rotation: count transitions into a turned state in the last 60 s
    const turnedLoose = Math.abs(yaw) > 18;
    if (turnedLoose && !state.wasTurned) state.turnLog.push(now);
    state.wasTurned = turnedLoose;
    state.turnLog = state.turnLog.filter((t) => now - t < 60000);
    if (state.turnLog.length >= 5) { report('repeated_head_movement', 0.8, { turns_per_minute: state.turnLog.length }); state.turnLog = []; }

    // eye gaze (only when the head is roughly frontal, otherwise head_turned covers it)
    const gazeOff = !turned && (Math.abs(f.gazeH) > 0.42 || (!written && f.gazeV > 0.6) || f.gazeV < -0.55);
    if (gazeOff && f.blink < 0.4) {
      state.gazeSince = state.gazeSince || now;
      if (now - state.gazeSince > 2000) report('gaze_away', 0.65, { h: +f.gazeH.toFixed(2), v: +f.gazeV.toFixed(2) }, false);
    } else state.gazeSince = null;

    setHealth('gaze', turned || gazeOff ? 'warn' : 'ok', turned ? 'Head turned' : gazeOff ? 'Looking away' : 'Focused');
  }

  function objectTick() {
    if (!state.running) return;
    const v = state.video;
    if (state.detector && v.readyState >= 2) {
      try {
        const items = window.Vision.analyzeObjects(state.detector, v, performance.now());
        handleObjects(items);
      } catch (e) { /* dropped frame */ }
    }
    setTimeout(objectTick, 700);
  }

  function handleObjects(items) {
    const hits = { phone_detected: 0, book_detected: 0, extra_device: 0, persons: 0 };
    const shown = [];
    for (const it of items) {
      if (it.label === 'person' && it.score >= 0.5) hits.persons += 1;
      else if ((it.label === 'cell phone' || it.label === 'remote') && it.score >= 0.32) { hits.phone_detected = Math.max(hits.phone_detected, it.score); shown.push(it); }
      else if (it.label === 'book' && it.score >= 0.55 && !written) { hits.book_detected = Math.max(hits.book_detected, it.score); shown.push(it); }
      else if ((it.label === 'laptop' || it.label === 'tv') && it.score >= 0.55) { hits.extra_device = Math.max(hits.extra_device, it.score); shown.push(it); }
    }
    state.lastObjects = shown;
    // require two consecutive detections to suppress single-frame false positives
    for (const type of ['phone_detected', 'book_detected', 'extra_device']) {
      if (hits[type]) {
        state.objectHits[type] = (state.objectHits[type] || 0) + 1;
        // a confident single detection is enough; weaker ones must repeat in consecutive frames
        if (state.objectHits[type] >= 2 || hits[type] >= 0.6) report(type, +hits[type].toFixed(2), { score: +hits[type].toFixed(2), source: 'efficientdet' });
      } else state.objectHits[type] = 0;
    }
    if (hits.persons > 1) {
      state.objectHits.persons = (state.objectHits.persons || 0) + 1;
      if (state.objectHits.persons >= 2) report('multiple_persons', 0.8, { persons: hits.persons, source: 'efficientdet' });
    } else state.objectHits.persons = 0;
  }

  // ------------------------------------------------------- server frames
  async function frameTick() {
    if (!state.running) return;
    const img = window.Vision.captureFrame(state.video, { maxWidth: 640, quality: 0.72 });
    if (img && navigator.onLine) {
      try {
        const r = await App.api(urls.frame, { image: img, faces: state.landmarker ? state.lastCount : undefined, pose: state.lastFace || undefined }, { allowError: true });
        if (r && r.ok) {
          if (r.identity) {
            if (r.identity.skipped) setHealth('identity', 'warn', 'Face the camera to verify identity');
            else setHealth('identity', r.identity.match ? 'ok' : 'bad', `Similarity ${r.identity.similarity.toFixed(2)}`);
          } else if (r.faces === 0) setHealth('identity', 'warn', 'No face to verify');
          if (!state.landmarker) setHealth('face', r.faces === 1 ? 'ok' : 'bad', `${r.faces} face(s)`);
          (r.events || []).forEach((t) => { if (WARN_TEXT[t]) emit('violation', { type: t, text: WARN_TEXT[t] }); });
          if (r.max_violations !== undefined) emit('strikes', r);
          emit('risk', r);
        } else if (r && r.status && r.status !== 'in_progress') emit('status', r);
      } catch (e) { /* network hiccup; heartbeat handles connectivity */ }
    }
    setTimeout(frameTick, cfg.frameInterval * 1000);
  }

  // ----------------------------------------------------------------- audio
  let audioCtx = null;
  function startAudio() {
    const track = state.stream && state.stream.getAudioTracks()[0];
    if (!track) { setHealth('audio', 'warn', 'No microphone'); return; }
    try {
      audioCtx = audioCtx || new AudioContext();
      const src = audioCtx.createMediaStreamSource(new MediaStream([track]));
      const an = audioCtx.createAnalyser();
      an.fftSize = 2048;
      src.connect(an);
      const buf = new Float32Array(an.fftSize);
      const calib = [];
      let floor = null;
      let win = { frames: 0, voiced: 0, loud: 0, levels: [], pitches: [] };
      const meter = document.getElementById('mic-level');
      setInterval(() => {
        if (!state.running) return;
        an.getFloatTimeDomainData(buf);
        let sum = 0; for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i];
        const rms = Math.sqrt(sum / buf.length);
        if (meter) { meter.style.width = Math.min(100, rms * 700) + '%'; }
        if (floor === null) {
          calib.push(rms);
          if (calib.length >= 30) { calib.sort((a, b) => a - b); floor = Math.max(0.002, calib[Math.floor(calib.length / 2)]); }
          return;
        }
        win.frames += 1;
        const pitch = rms > floor * 2.5 ? detectPitch(buf, audioCtx.sampleRate) : null;
        if (pitch) { win.voiced += 1; win.pitches.push(Math.round(pitch)); win.levels.push(rms); }
        if (rms > floor * 4) win.loud += 1;
        if (rms < floor * 1.3) floor = floor * 0.995 + rms * 0.005; // slowly track a changing room
        if (win.frames >= 50) { // 5 s window
          const voicedRatio = win.voiced / win.frames;
          const meanVoiced = win.levels.length ? win.levels.reduce((a, b) => a + b, 0) / win.levels.length : floor;
          const summary = { voiced_ratio: +voicedRatio.toFixed(3), loud_ratio: +(win.loud / win.frames).toFixed(3), level_db: +(20 * Math.log10(meanVoiced / floor)).toFixed(1), pitches: win.pitches.slice(0, 80) };
          setHealth('audio', voicedRatio > 0.25 ? 'warn' : 'ok', voicedRatio > 0.25 ? 'Speech detected' : 'Quiet');
          if ((voicedRatio > 0.15 || summary.loud_ratio > 0.5) && navigator.onLine) {
            App.api(urls.audio, summary, { allowError: true }).then((r) => {
              if (!r) return;
              if (r.risk) emit('risk', r);
              (r.events || []).forEach((t) => { if (WARN_TEXT[t]) emit('violation', { type: t, text: WARN_TEXT[t] }); });
              if (r.max_violations !== undefined) emit('strikes', r);
              if (r.status && r.status !== 'in_progress') emit('status', r);
            }).catch(() => {});
          }
          win = { frames: 0, voiced: 0, loud: 0, levels: [], pitches: [] };
        }
      }, 100);
      setHealth('audio', 'ok', 'Calibrating…');
    } catch (e) { setHealth('audio', 'warn', 'Audio analysis unavailable'); }
  }

  // autocorrelation pitch estimate; returns Hz or null when not clearly periodic
  function detectPitch(buf, rate) {
    const n = 1024;
    const minLag = Math.floor(rate / 400), maxLag = Math.floor(rate / 70);
    let best = -1, bestLag = -1, energy = 0;
    for (let i = 0; i < n; i++) energy += buf[i] * buf[i];
    if (energy === 0) return null;
    for (let lag = minLag; lag <= maxLag; lag++) {
      let c = 0;
      for (let i = 0; i < n; i++) c += buf[i] * buf[i + lag];
      c /= energy;
      if (c > best) { best = c; bestLag = lag; }
    }
    return best > 0.55 ? rate / bestLag : null;
  }

  // --------------------------------------------------------------- browser
  function installBrowserGuards() {
    let hiddenAt = null;
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { hiddenAt = Date.now(); report('tab_switch', 0.95, {}, false); }
      else if (hiddenAt) { emit('notice', { kind: 'warning', text: `You left the exam tab for ${Math.round((Date.now() - hiddenAt) / 1000)} s. This was recorded.` }); hiddenAt = null; }
    });
    window.addEventListener('blur', () => setTimeout(() => { if (!document.hidden && !document.hasFocus()) report('focus_lost', 0.8, {}, false); }, 400));
    document.addEventListener('fullscreenchange', () => {
      if (!document.fullscreenElement && cfg.enforceFullscreen && state.running) { report('fullscreen_exit', 0.9, {}, false); emit('fullscreen', false); }
      else emit('fullscreen', !!document.fullscreenElement);
    });
    ['copy', 'cut', 'paste'].forEach((t) => document.addEventListener(t, (e) => { e.preventDefault(); report('clipboard', 0.9, { action: t }, false); emit('notice', { kind: 'warning', text: 'Copy and paste are disabled during the exam.' }); }));
    document.addEventListener('contextmenu', (e) => { e.preventDefault(); report('right_click', 0.6, {}, false); });
    document.addEventListener('keydown', (e) => {
      const k = (e.key || '').toLowerCase();
      const mod = e.ctrlKey || e.metaKey;
      if (k === 'printscreen') { report('screenshot_attempt', 0.9, {}, false); return; }
      if (k === 'f12' || (mod && e.shiftKey && ['i', 'j', 'c', 'k'].includes(k)) || (mod && ['u', 's', 'p'].includes(k))) {
        e.preventDefault(); report(k === 'f12' || e.shiftKey ? 'devtools_open' : 'blocked_shortcut', 0.7, { key: (mod ? 'Ctrl+' : '') + (e.shiftKey ? 'Shift+' : '') + e.key }, false);
      }
      if (mod && ['c', 'v', 'x'].includes(k)) { e.preventDefault(); report('clipboard', 0.85, { key: 'Ctrl+' + e.key }, false); }
    }, true);
    document.addEventListener('keyup', (e) => { if (e.key === 'PrintScreen') report('screenshot_attempt', 0.9, {}, false); });

    setInterval(() => {
      if (!state.running) return;
      const gapW = window.outerWidth - window.innerWidth, gapH = window.outerHeight - window.innerHeight;
      if (!document.fullscreenElement && (gapW > 170 || gapH > 220)) report('devtools_open', 0.6, { gapW, gapH }, false);
      if (window.screen.isExtended) report('multiple_monitors', 0.95, {}, false);
      const small = window.innerWidth < screen.availWidth * 0.75 || window.innerHeight < screen.availHeight * 0.6;
      if (small && !document.fullscreenElement) report('window_resized', 0.7, { w: innerWidth, h: innerHeight }, false);
      setHealth('screen', document.fullscreenElement || !cfg.enforceFullscreen ? 'ok' : 'warn', document.fullscreenElement ? 'Full-screen' : 'Not full-screen');
    }, 3000);

    window.addEventListener('offline', () => { setHealth('network', 'bad', 'Offline'); report('network_offline', 0.9, {}, false); emit('notice', { kind: 'warning', text: 'You are offline. Answers will sync when the connection returns.' }); });
    window.addEventListener('online', () => { setHealth('network', 'ok', 'Online'); flush(); emit('online'); });
    window.addEventListener('beforeunload', (e) => { if (state.running) { e.preventDefault(); e.returnValue = ''; } });
  }

  // ------------------------------------------------------------- heartbeat
  async function heartbeatTick() {
    if (!state.running) return;
    try {
      const r = await App.api(urls.heartbeat, { client: { health: state.health, onDeviceAI: !!state.landmarker } }, { allowError: true });
      if (r && r.ok) {
        setHealth('network', 'ok', 'Connected');
        emit('heartbeat', r);
        if (r.max_violations !== undefined) emit('strikes', r);
        (r.messages || []).forEach((m) => emit('message', m));
        if (r.status !== 'in_progress') emit('status', r);
      }
    } catch (e) { setHealth('network', 'bad', 'Connection problem'); }
    setTimeout(heartbeatTick, 5000);
  }

  async function start(videoEl) {
    state.video = videoEl;
    await openStream();
    state.running = true;
    installBrowserGuards();
    if (cfg.monitorAudio) startAudio(); else setHealth('audio', 'ok', 'Not monitored');
    setHealth('network', 'ok'); setHealth('identity', 'warn', 'Waiting for first check'); setHealth('screen', 'ok');
    heartbeatTick();
    frameTick();
    loadModels().then(() => { visionTick(); objectTick(); });
  }

  function stop() {
    state.running = false;
    if (state.stream) state.stream.getTracks().forEach((t) => t.stop());
    if (audioCtx) audioCtx.close().catch(() => {});
  }

  function captureFrame(opts) { return window.Vision.captureFrame(state.video, opts); }

  window.Proctor = { start, stop, on, report, reconnectCamera, captureFrame, flush };
})();
