/* Exam room UI: questions, autosave, palette, timer, answer sheets, submission. */
(function () {
  const cfg = window.ROOM;
  const urls = cfg.urls;
  const Qs = window.QUESTIONS;
  const $ = (s) => document.querySelector(s);
  const KEYS = 'ABCDEFGH';
  let current = 0;
  let remaining = cfg.remaining;
  let deadline = Date.now() + remaining * 1000;
  let finished = false;
  let sheets = cfg.sheets || [];
  const pending = new Map();

  // ------------------------------------------------------------- rendering
  function answered(q) {
    const r = q.response;
    return !(r === null || r === undefined || r === '' || (Array.isArray(r) && r.length === 0));
  }

  function render() {
    const q = Qs[current];
    const card = $('#q-card');
    if (!q) { card.innerHTML = '<div class="empty">This exam has no questions.</div>'; return; }
    const typeLabel = { mcq: 'Single choice', multi: 'Select all that apply', truefalse: 'True or false', short: 'Short answer', essay: cfg.examMode === 'written' ? 'Answer on paper' : 'Long answer' }[q.type];
    card.innerHTML = `
      <div class="q-head">
        <div class="q-num">Question ${current + 1} of ${Qs.length} · ${typeLabel}</div>
        <div class="row"><span class="badge">${q.marks} mark${q.marks === 1 ? '' : 's'}</span>
          <button class="btn btn-sm ${q.flagged ? 'btn-primary' : ''}" id="flag-btn"><i data-lucide="flag"></i> ${q.flagged ? 'Marked for review' : 'Mark for review'}</button></div>
      </div>
      <div class="q-text"></div>
      <div id="q-input"></div>
      <div class="q-nav">
        <button class="btn" id="prev-btn" ${current === 0 ? 'disabled' : ''}><i data-lucide="arrow-left"></i> Previous</button>
        ${current < Qs.length - 1 ? '<button class="btn btn-primary" id="next-btn">Save & next <i data-lucide="arrow-right"></i></button>' : '<button class="btn btn-primary" id="finish-btn"><i data-lucide="check-check"></i> Review & submit</button>'}
      </div>`;
    card.querySelector('.q-text').textContent = q.text;
    const box = card.querySelector('#q-input');
    if (q.type === 'mcq' || q.type === 'multi' || q.type === 'truefalse') {
      const opts = q.type === 'truefalse' ? [{ value: true, text: 'True' }, { value: false, text: 'False' }] : q.options;
      opts.forEach((o, i) => {
        const multi = q.type === 'multi';
        const selected = multi ? (q.response || []).includes(o.value) : q.response === o.value || (q.type === 'truefalse' && String(q.response) === String(o.value));
        const el = document.createElement('label');
        el.className = 'opt' + (selected ? ' selected' : '');
        el.innerHTML = `<input type="${multi ? 'checkbox' : 'radio'}" name="opt"><span class="key">${KEYS[i]}</span><span class="t"></span>`;
        el.querySelector('.t').textContent = o.text;
        el.addEventListener('click', (e) => { e.preventDefault(); choose(o.value); });
        box.appendChild(el);
      });
    } else if (q.type === 'short') {
      box.innerHTML = '<input class="input" style="font-size:16px;padding:14px" placeholder="Type your answer" maxlength="500">';
      const inp = box.querySelector('input');
      inp.value = q.response || '';
      inp.addEventListener('input', () => { q.response = inp.value; queueSave(q, 800); paintPalette(); });
    } else if (cfg.examMode === 'written') {
      box.innerHTML = '<div class="callout"><i data-lucide="pen-line"></i><div>Write this answer on your answer sheet and label it <b>Q' + (current + 1) + '</b>. Keep the paper and your hands in view of the camera.</div></div>';
    } else {
      box.innerHTML = '<textarea class="input" style="min-height:280px;font-size:15.5px;line-height:1.6" placeholder="Write your answer here…"></textarea><div class="small muted mt-1" id="wc"></div>';
      const ta = box.querySelector('textarea');
      ta.value = q.response || '';
      const wc = () => { box.querySelector('#wc').textContent = `${(ta.value.trim().match(/\S+/g) || []).length} words`; };
      wc();
      ta.addEventListener('input', () => { q.response = ta.value; wc(); queueSave(q, 1200); paintPalette(); });
    }
    $('#flag-btn').addEventListener('click', () => { q.flagged = !q.flagged; queueSave(q, 0, { flaggedOnly: true }); render(); });
    $('#prev-btn').addEventListener('click', () => go(current - 1));
    $('#next-btn')?.addEventListener('click', () => go(current + 1));
    $('#finish-btn')?.addEventListener('click', confirmSubmit);
    paintPalette();
    App.icons();
    $('.q-area').scrollTop = 0;
  }

  function choose(value) {
    const q = Qs[current];
    if (q.type === 'multi') {
      const set = new Set(q.response || []);
      set.has(value) ? set.delete(value) : set.add(value);
      q.response = [...set].sort((a, b) => a - b);
    } else q.response = value;
    queueSave(q, 0);
    render();
  }

  function go(i) {
    if (i < 0 || i >= Qs.length) return;
    flushSaves();
    current = i;
    render();
  }

  function paintPalette() {
    const pal = $('#palette');
    pal.innerHTML = '';
    Qs.forEach((q, i) => {
      const b = document.createElement('button');
      b.textContent = i + 1;
      b.className = [answered(q) ? 'answered' : '', q.flagged ? 'flagged' : '', i === current ? 'current' : ''].join(' ');
      b.title = `Question ${i + 1}${answered(q) ? ' · answered' : ''}${q.flagged ? ' · marked' : ''}`;
      b.onclick = () => go(i);
      pal.appendChild(b);
    });
    const n = Qs.filter(answered).length;
    $('#progress-text').textContent = cfg.examMode === 'written' ? `${Qs.length} questions` : `${n}/${Qs.length} answered`;
  }

  // ---------------------------------------------------------------- saving
  function setSave(text, icon) {
    $('#save-state').innerHTML = `<i data-lucide="${icon}" style="width:14px;height:14px"></i> ${text}`;
    App.icons();
  }

  function queueSave(q, delay, opts = {}) {
    clearTimeout(pending.get(q.id)?.timer);
    const job = { q, opts, timer: setTimeout(() => save(q, opts), delay) };
    pending.set(q.id, job);
    setSave('Saving…', 'loader');
  }

  function flushSaves() {
    for (const [, job] of pending) { clearTimeout(job.timer); save(job.q, job.opts); }
  }

  async function save(q, opts) {
    pending.delete(q.id);
    if (finished) return;
    const body = { question_id: q.id, flagged: !!q.flagged };
    if (!opts.flaggedOnly) body.response = q.response;
    try {
      const r = await App.api(urls.answer, body, { allowError: true });
      if (r.ok) setSave('All changes saved', 'cloud');
      else if (r.status && r.status !== 'in_progress') handleStatus(r);
      else throw new Error(r.error);
    } catch (e) {
      setSave('Offline — will retry', 'cloud-off');
      setTimeout(() => queueSave(q, 0, opts), 4000);
    }
  }

  // ----------------------------------------------------------------- timer
  function tick() {
    if (finished) return;
    const left = Math.max(0, Math.round((deadline - Date.now()) / 1000));
    $('#timer-text').textContent = App.fmt.clock(left);
    const t = $('#timer');
    t.className = 'timer' + (left <= 60 ? ' danger' : left <= 300 ? ' warn' : '');
    if (left === 300) App.toast('5 minutes remaining.', 'warning');
    if (left === 0) { autoSubmit(); return; }
    setTimeout(tick, 1000);
  }

  // ------------------------------------------------------------ submission
  function confirmSubmit() {
    flushSaves();
    const unanswered = Qs.filter((q) => !answered(q)).length;
    const flagged = Qs.filter((q) => q.flagged).length;
    const body = document.createElement('div');
    if (cfg.examMode === 'written') {
      body.innerHTML = `<p>You have uploaded <b>${sheets.length}</b> answer page(s).</p>${sheets.length === 0 ? '<div class="callout danger"><div>You have not uploaded any answer sheets. Capture each page before submitting.</div></div>' : ''}<p class="mt-1">After submitting you cannot change your answers.</p>`;
    } else {
      body.innerHTML = `<div class="grid g-3 center" style="gap:10px;margin:6px 0 14px">
        <div class="card card-pad"><div class="stat-value" style="font-size:24px;font-weight:700">${Qs.length - unanswered}</div><div class="small muted">Answered</div></div>
        <div class="card card-pad"><div style="font-size:24px;font-weight:700;color:var(--danger)">${unanswered}</div><div class="small muted">Unanswered</div></div>
        <div class="card card-pad"><div style="font-size:24px;font-weight:700;color:var(--warning)">${flagged}</div><div class="small muted">Marked</div></div></div>
        <p>${unanswered ? 'Some questions are unanswered. ' : ''}After submitting you cannot change your answers.</p>`;
    }
    App.modal({ title: 'Submit your exam?', body, icon: 'send', confirmText: 'Submit now', cancelText: 'Keep working' }).then((ok) => { if (ok) doSubmit(); });
  }

  async function doSubmit() {
    flushSaves();
    finished = true;
    try {
      const r = await App.api(urls.submit, {}, { allowError: true });
      leave(r.redirect);
    } catch (e) { finished = false; App.toast('Could not submit — check your connection and try again.', 'error'); }
  }

  async function autoSubmit() {
    flushSaves();
    finished = true;
    App.toast('Time is up. Submitting your exam…', 'warning');
    const r = await App.api(urls.submit, {}, { allowError: true }).catch(() => ({}));
    leave(r.redirect || location.href);
  }

  function leave(url) {
    window.Proctor.stop();
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    location.href = url;
  }

  function handleStatus(r) {
    if (finished && r.status === 'in_progress') return;
    if (r.status === 'terminated') {
      finished = true;
      window.Proctor.stop();
      App.modal({ title: 'Exam terminated', body: App.escapeHtml(r.reason || 'Your exam was terminated by the proctor.'), icon: 'octagon-x', danger: true, confirmText: 'View details', cancelText: '' })
        .then(() => leave(r.redirect || location.href));
    } else if (r.status === 'submitted' || r.status === 'auto_submitted') {
      finished = true;
      leave(r.redirect || location.href);
    }
  }

  // ---------------------------------------------------------- answer sheets
  function renderSheets() {
    const grid = $('#sheets');
    if (!grid) return;
    grid.innerHTML = '';
    sheets.forEach((s, i) => {
      const d = document.createElement('div');
      d.className = 'sheet';
      d.innerHTML = `<img alt="Page ${i + 1}"><span class="n">p.${i + 1}</span><button class="btn btn-sm icon-btn" title="Remove" style="width:26px;height:26px"><i data-lucide="x"></i></button>`;
      d.querySelector('img').src = s.url;
      d.querySelector('img').dataset.zoom = s.url;
      d.querySelector('button').onclick = async () => {
        const r = await App.api(urls.sheet, { remove: s.path }, { allowError: true });
        if (r.ok) { sheets = r.sheets; renderSheets(); }
      };
      grid.appendChild(d);
    });
    $('#sheet-count').textContent = `${sheets.length} page${sheets.length === 1 ? '' : 's'}`;
    App.icons();
  }

  async function uploadSheet(dataUrl) {
    const r = await App.api(urls.sheet, { image: dataUrl }, { allowError: true });
    if (r.ok) { sheets = r.sheets; renderSheets(); App.toast('Page saved.', 'success', 2000); } else App.toast(r.error || 'Upload failed.', 'error');
  }

  function setupSheets() {
    if (cfg.examMode !== 'written') return;
    renderSheets();
    $('#sheet-capture').addEventListener('click', async () => {
      const ok = await App.modal({ title: 'Capture answer page', body: 'Hold the page flat and close to the camera so the writing fills the frame, then click <b>Capture</b>.', icon: 'camera', confirmText: 'Capture' });
      if (ok) uploadSheet(window.Proctor.captureFrame({ maxWidth: 1600, quality: 0.9 }));
    });
    $('#sheet-file').addEventListener('change', (e) => {
      [...e.target.files].forEach((file) => {
        const reader = new FileReader();
        reader.onload = () => {
          const img = new Image();
          img.onload = () => {
            const scale = Math.min(1, 1800 / Math.max(img.width, img.height));
            const c = document.createElement('canvas');
            c.width = img.width * scale; c.height = img.height * scale;
            c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
            uploadSheet(c.toDataURL('image/jpeg', 0.85));
          };
          img.src = reader.result;
        };
        reader.readAsDataURL(file);
      });
      e.target.value = '';
    });
  }

  // ------------------------------------------------------- proctor wiring
  const P = window.Proctor;
  P.on('heartbeat', (r) => {
    if (typeof r.remaining === 'number') deadline = Date.now() + r.remaining * 1000; // server clock wins
    paintRisk(r.risk);
  });
  P.on('risk', (r) => paintRisk(r.risk));
  P.on('status', handleStatus);
  P.on('message', (m) => {
    const title = m.kind === 'warning' ? 'Message from your proctor' : m.kind === 'system' ? 'Integrity notice' : 'Notice';
    App.modal({ title, body: App.escapeHtml(m.body), icon: m.kind === 'warning' ? 'alert-triangle' : 'info', danger: m.kind !== 'info', confirmText: 'I understand', cancelText: '' });
  });
  P.on('notice', (n) => App.toast(n.text, n.kind === 'warning' ? 'warning' : 'info', 4500));
  P.on('fullscreen', (isFull) => { $('#fs-blocker').hidden = isFull || finished || !cfg.enforceFullscreen; });
  P.on('camera', (c) => { $('#cam-blocker').hidden = c.ok; });
  P.on('online', flushSaves);

  function paintRisk(risk) {
    const el = $('#integrity');
    const map = { low: ['badge-green', 'Integrity: good'], medium: ['badge-amber', 'Integrity: caution'], high: ['risk-high', 'Integrity: warning'], critical: ['badge-red', 'Integrity: critical'] };
    const [cls, text] = map[risk] || map.low;
    el.className = `badge ${cls}`;
    el.innerHTML = `<span class="dot"></span> ${text}`;
  }

  $('#fs-return').addEventListener('click', () => document.documentElement.requestFullscreen().catch(() => {}));
  $('#cam-retry').addEventListener('click', async () => {
    try { await P.reconnectCamera(); $('#cam-blocker').hidden = true; } catch (e) { App.toast('Camera still unavailable. Check permissions and cables.', 'error'); }
  });
  $('#submit-btn').addEventListener('click', confirmSubmit);

  document.addEventListener('keydown', (e) => {
    if (e.target.matches('input, textarea') || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'ArrowRight') go(current + 1);
    else if (e.key === 'ArrowLeft') go(current - 1);
    else {
      const idx = KEYS.indexOf(e.key.toUpperCase());
      const q = Qs[current];
      if (idx >= 0 && q) {
        const opts = q.type === 'truefalse' ? [true, false] : (q.options || []).map((o) => o.value);
        if (idx < opts.length) choose(opts[idx]);
      }
    }
  });

  // ------------------------------------------------------------- start gate
  $('#gate-btn').addEventListener('click', async () => {
    const btn = $('#gate-btn');
    btn.disabled = true;
    $('#gate-msg').textContent = 'Starting camera and AI monitoring…';
    if (cfg.enforceFullscreen) { try { await document.documentElement.requestFullscreen(); } catch (e) { /* recorded by monitor */ } }
    try {
      await P.start($('#cam'));
      $('#gate').hidden = true;
      tick();
    } catch (e) {
      btn.disabled = false;
      $('#gate-msg').textContent = 'Camera access is required to take this exam. Allow camera and microphone permissions, then try again.';
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    }
  });

  $('#timer-text').textContent = App.fmt.clock(remaining);
  setupSheets();
  render();
})();
