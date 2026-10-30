/* Pre-exam wizard: system check -> rules -> liveness + identity photo -> start. */
(function () {
  const cfg = window.PREPARE;
  const $ = (s) => document.querySelector(s);
  let stream = null;
  let landmarker = null;
  let aiReady = false;
  let photo = null;
  let liveness = { passed: cfg.liveness ? null : true, challenges: [] };
  const results = {};

  // ------------------------------------------------------------ navigation
  function goto(step) {
    document.querySelectorAll('[data-panel]').forEach((p) => { p.hidden = p.dataset.panel !== String(step); });
    document.querySelectorAll('#steps .s').forEach((s) => {
      const n = Number(s.dataset.step);
      s.classList.toggle('active', n === step);
      s.classList.toggle('done', n < step);
    });
    if (step === 3) startIdentity();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
  document.querySelectorAll('[data-back]').forEach((b) => b.addEventListener('click', () => goto(Number(b.dataset.back))));
  $('#to-step-2').addEventListener('click', () => goto(2));
  $('#to-step-3').addEventListener('click', () => goto(3));
  $('#consent').addEventListener('change', (e) => { $('#to-step-3').disabled = !e.target.checked; });

  // ----------------------------------------------------------- system check
  function setCheck(key, state, text) {
    results[key] = state;
    const li = document.querySelector(`[data-check="${key}"]`);
    const icon = li.querySelector('.status-icon');
    const badge = li.querySelector('.st');
    icon.className = 'status-icon ' + (state === 'ok' ? 'ok' : state === 'bad' ? 'bad' : state === 'warn' ? 'warn' : '');
    badge.className = 'st badge ' + (state === 'ok' ? 'badge-green' : state === 'bad' ? 'badge-red' : state === 'warn' ? 'badge-amber' : '');
    badge.textContent = state === 'ok' ? 'Ready' : state === 'bad' ? 'Problem' : state === 'warn' ? 'Warning' : 'Checking…';
    if (text) li.querySelector('[data-desc]').textContent = text;
    const required = ['camera', 'browser', 'network'].concat(cfg.monitorAudio ? ['mic'] : []);
    const blocking = required.some((k) => results[k] !== 'ok') || results.screen === 'bad';
    $('#to-step-2').disabled = blocking;
  }

  async function runChecks() {
    ['camera', 'mic', 'browser', 'screen', 'network', 'ai'].forEach((k) => setCheck(k, 'pending'));
    if (stream) stream.getTracks().forEach((t) => t.stop());
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 1280 }, height: { ideal: 720 }, facingMode: 'user' },
        audio: cfg.monitorAudio ? { echoCancellation: true, noiseSuppression: false } : false,
      });
      const v = $('#preview'); v.srcObject = stream; await v.play();
      const track = stream.getVideoTracks()[0];
      const s = track.getSettings();
      setCheck('camera', 'ok', `${track.label || 'Camera'} · ${s.width}×${s.height}`);
      $('#preview-msg').textContent = 'Camera is working.';
      if (cfg.monitorAudio) {
        const a = stream.getAudioTracks()[0];
        if (a) { setCheck('mic', 'ok', a.label || 'Microphone connected'); micMeter(stream); }
        else setCheck('mic', 'bad', 'No microphone found.');
      } else setCheck('mic', 'ok', 'Audio monitoring is disabled for this exam.');
    } catch (e) {
      setCheck('camera', 'bad', 'Camera permission denied or no camera found. Allow access in the address bar and re-run.');
      setCheck('mic', cfg.monitorAudio ? 'bad' : 'ok', cfg.monitorAudio ? 'Microphone permission denied.' : 'Not required.');
      $('#preview-msg').textContent = 'Camera unavailable.';
    }

    const fsOk = !!(document.documentElement.requestFullscreen || document.documentElement.webkitRequestFullscreen);
    const wasm = typeof WebAssembly === 'object';
    setCheck('browser', fsOk && wasm && navigator.mediaDevices ? 'ok' : 'bad', fsOk && wasm ? navigator.userAgent.match(/(Chrome|Firefox|Safari|Edg)\/[\d.]+/)?.[0] || 'Supported browser' : 'Please use the latest Chrome, Edge or Firefox.');

    if (window.screen.isExtended) setCheck('screen', 'bad', 'Multiple monitors detected. Disconnect extra displays and re-run.');
    else setCheck('screen', 'ok', `${screen.width}×${screen.height} display`);

    const t0 = performance.now();
    try {
      await fetch(location.href, { method: 'HEAD', cache: 'no-store' });
      const ms = Math.round(performance.now() - t0);
      setCheck('network', ms < 1500 ? 'ok' : 'warn', `Latency ${ms} ms${navigator.connection?.effectiveType ? ' · ' + navigator.connection.effectiveType : ''}`);
    } catch (e) { setCheck('network', 'bad', 'Cannot reach the exam server.'); }

    if (!landmarker) {
      try {
        landmarker = await window.Vision.loadFace(2);
        aiReady = true;
        setCheck('ai', 'ok', 'Face landmark model loaded on this device.');
        previewLoop();
      } catch (e) {
        setCheck('ai', 'warn', 'On-device AI could not load (network/GPU). Server-side AI will still monitor you.');
      }
    } else setCheck('ai', 'ok', 'Face landmark model loaded on this device.');
  }

  function micMeter(s) {
    try {
      const ctx = new AudioContext();
      const an = ctx.createAnalyser(); an.fftSize = 1024;
      ctx.createMediaStreamSource(s).connect(an);
      const buf = new Float32Array(an.fftSize);
      const tick = () => {
        an.getFloatTimeDomainData(buf);
        let sum = 0; for (const x of buf) sum += x * x;
        const level = Math.min(100, Math.sqrt(sum / buf.length) * 600);
        const el = $('#mic-level'); if (el) el.style.width = level + '%';
        requestAnimationFrame(tick);
      };
      tick();
    } catch (e) { /* audio context unavailable */ }
  }

  function previewLoop() {
    const v = $('#preview');
    const canvas = $('#preview-overlay');
    const loop = () => {
      if (!document.querySelector('[data-panel="1"]').hidden && v.readyState >= 2 && landmarker) {
        const r = window.Vision.analyzeFaces(landmarker, v, performance.now());
        window.Vision.drawOverlay(canvas, v, r.faces, []);
        $('#preview-msg').textContent = r.count === 0 ? 'No face detected. Move into the frame.' : r.count > 1 ? 'More than one face detected.' : 'Face detected. Looking good.';
      }
      setTimeout(() => requestAnimationFrame(loop), 120);
    };
    loop();
  }

  $('#recheck').addEventListener('click', runChecks);
  runChecks();

  // ------------------------------------------------------- identity + liveness
  const CHALLENGES = {
    blink: { label: 'Blink twice', icon: 'eye' },
    turn: { label: 'Turn your head to one side, then back', icon: 'move-horizontal' },
    smile: { label: 'Smile', icon: 'smile' },
    nod: { label: 'Nod (look down, then back)', icon: 'move-vertical' },
  };
  let queue = [];
  let state = {};
  let identityStarted = false;

  function pickChallenges() {
    const keys = Object.keys(CHALLENGES).sort(() => Math.random() - 0.5).slice(0, 2);
    return keys;
  }

  function renderChallenges() {
    const el = $('#challenges');
    if (!el) return;
    el.innerHTML = '';
    liveness.challenges.forEach((k, i) => {
      const done = i < liveness.challenges.length - queue.length;
      const current = !done && i === liveness.challenges.length - queue.length;
      const c = document.createElement('div');
      c.className = 'c' + (done ? ' done' : current ? ' current' : '');
      c.innerHTML = `<i data-lucide="${done ? 'check' : CHALLENGES[k].icon}"></i> ${CHALLENGES[k].label}`;
      el.appendChild(c);
    });
    App.icons();
  }

  async function startIdentity() {
    const v = $('#id-video');
    if (stream && v.srcObject !== stream) { v.srcObject = stream; await v.play().catch(() => {}); }
    if (identityStarted) return;
    identityStarted = true;
    if (cfg.liveness && aiReady) {
      liveness.challenges = pickChallenges();
      queue = liveness.challenges.slice();
      state = { blinks: 0, eyesClosed: false, turned: false, nodded: false, smiled: 0, baseYaw: null, basePitch: null, samples: [] };
      renderChallenges();
    } else if (cfg.liveness) {
      liveness = { passed: null, challenges: [], note: 'on-device AI unavailable' };
      const el = $('#challenges'); if (el) el.innerHTML = '<span class="small muted">On-device AI unavailable — liveness will be reviewed by the proctor.</span>';
    }
    idLoop();
  }

  function idLoop() {
    const v = $('#id-video');
    const canvas = $('#id-overlay');
    const guide = $('#id-guide');
    const msg = $('#id-msg');
    const capture = $('#capture');
    const tick = () => {
      if ($('[data-panel="3"]').hidden || photo) { setTimeout(tick, 250); return; }
      if (!landmarker || v.readyState < 2) {
        capture.disabled = v.readyState < 2;
        msg.textContent = 'Look straight at the camera, then capture your photo.';
        setTimeout(tick, 250);
        return;
      }
      const r = window.Vision.analyzeFaces(landmarker, v, performance.now());
      window.Vision.drawOverlay(canvas, v, r.faces, []);
      const f = r.faces[0];
      let ok = false;
      if (r.count === 0) msg.textContent = 'No face detected — move into the oval.';
      else if (r.count > 1) msg.textContent = 'Only you should be visible.';
      else if (f.box.w < 0.14) msg.textContent = 'Move closer to the camera.';
      else {
        ok = true;
        if (queue.length) msg.textContent = CHALLENGES[queue[0]].label + '…';
        else msg.textContent = Math.abs(f.yaw) < 15 && Math.abs(f.pitch) < 18 ? 'Perfect — hold still and capture.' : 'Now look straight at the camera.';
        if (queue.length) evaluateChallenge(f);
      }
      guide.className = 'guide ' + (ok ? 'ok' : 'bad');
      const livenessDone = !cfg.liveness || queue.length === 0;
      capture.disabled = !(ok && livenessDone);
      setTimeout(() => requestAnimationFrame(tick), 80);
    };
    tick();
  }

  function evaluateChallenge(f) {
    if (state.baseYaw === null) {
      state.samples.push(f);
      if (state.samples.length >= 8) {
        state.baseYaw = state.samples.reduce((a, s) => a + s.yaw, 0) / state.samples.length;
        state.basePitch = state.samples.reduce((a, s) => a + s.pitch, 0) / state.samples.length;
      }
      return;
    }
    const cur = queue[0];
    let done = false;
    if (cur === 'blink') {
      if (f.blink > 0.5 && !state.eyesClosed) state.eyesClosed = true;
      if (f.blink < 0.2 && state.eyesClosed) { state.eyesClosed = false; state.blinks += 1; }
      done = state.blinks >= 2;
    } else if (cur === 'turn') {
      if (Math.abs(f.yaw - state.baseYaw) > 22) state.turned = true;
      done = state.turned && Math.abs(f.yaw - state.baseYaw) < 8;
    } else if (cur === 'smile') {
      state.smiled = f.smile > 0.55 ? state.smiled + 1 : 0;
      done = state.smiled >= 4;
    } else if (cur === 'nod') {
      if (f.pitch - state.basePitch > 14) state.nodded = true;
      done = state.nodded && Math.abs(f.pitch - state.basePitch) < 7;
    }
    if (done) {
      queue.shift();
      renderChallenges();
      if (!queue.length) {
        liveness.passed = true;
        $('#id-state').className = 'badge badge-green'; $('#id-state').textContent = 'Liveness passed';
      }
    }
  }

  $('#capture').addEventListener('click', () => {
    const v = $('#id-video');
    photo = window.Vision.captureFrame(v, { maxWidth: 960, quality: 0.9 });
    const img = $('#id-photo'); img.src = photo; img.hidden = false;
    v.style.visibility = 'hidden'; $('#id-overlay').style.visibility = 'hidden';
    $('#id-msg').textContent = 'Photo captured. Click "Verify & start exam".';
    $('#capture').hidden = true; $('#retake').hidden = false; $('#start').disabled = false;
  });

  $('#retake').addEventListener('click', () => {
    photo = null;
    $('#id-photo').hidden = true; $('#id-video').style.visibility = ''; $('#id-overlay').style.visibility = '';
    $('#capture').hidden = false; $('#retake').hidden = true; $('#start').disabled = true;
  });

  $('#start').addEventListener('click', async () => {
    const btn = $('#start');
    btn.disabled = true; btn.innerHTML = '<i data-lucide="loader"></i> Verifying identity…'; App.icons();
    try {
      try { await document.documentElement.requestFullscreen(); } catch (e) { /* room page will ask again */ }
      const res = await App.api(cfg.beginUrl, {
        photo,
        liveness,
        access_code: $('#access-code')?.value || '',
        device: {
          ua: navigator.userAgent, platform: navigator.platform, screen: `${screen.width}x${screen.height}`,
          cores: navigator.hardwareConcurrency, memory: navigator.deviceMemory, tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
          onDeviceAI: aiReady,
        },
      }, { allowError: true });
      if (res.ok) {
        $('#id-state').className = 'badge badge-green';
        $('#id-state').textContent = res.verified ? `Verified${res.similarity != null ? ' · ' + res.similarity.toFixed(2) : ''}` : 'Pending proctor approval';
        if (stream) stream.getTracks().forEach((t) => t.stop());
        location.href = res.redirect;
        return;
      }
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
      $('#id-state').className = 'badge badge-red'; $('#id-state').textContent = 'Not verified';
      App.modal({ title: 'Verification failed', body: App.escapeHtml(res.error || 'Please try again.'), icon: 'scan-face', danger: true, confirmText: 'Try again', cancelText: '' });
      $('#retake').click();
    } catch (e) {
      App.toast(e.message, 'error');
    } finally {
      btn.innerHTML = '<i data-lucide="play"></i> Verify & start exam'; App.icons();
    }
  });
})();
