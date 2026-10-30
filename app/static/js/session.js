/* Proctor session detail: live updates, score chart, timeline review, actions. */
(function () {
  const cfg = window.SESSION;
  const $ = (s) => document.querySelector(s);
  const esc = App.escapeHtml;
  let events = [];
  let lastId = 0;
  let chart = null;

  // ----------------------------------------------------------------- chart
  function drawChart(points) {
    const canvas = $('#score-chart');
    if (!window.Chart || !canvas) return;
    const css = getComputedStyle(document.documentElement);
    const text = css.getPropertyValue('--text-3').trim();
    const grid = css.getPropertyValue('--border').trim();
    const data = points.map((p) => ({ x: new Date(p.t).getTime(), y: p.score }));
    const line = (y, color) => ({ type: 'line', label: '', data: data.length ? [{ x: data[0].x, y }, { x: data[data.length - 1].x, y }] : [], borderColor: color, borderDash: [5, 5], borderWidth: 1.2, pointRadius: 0, fill: false });
    const datasets = [
      { label: 'Fraud score', data, borderColor: '#6366f1', backgroundColor: 'rgba(99,102,241,.12)', fill: true, tension: 0.25, pointRadius: 0, borderWidth: 2.2, stepped: false },
      line(cfg.warn, '#f97316'), line(cfg.critical, '#ef4444'),
    ];
    if (chart) { chart.data.datasets = datasets; chart.update('none'); return; }
    chart = new Chart(canvas, {
      type: 'line',
      data: { datasets },
      options: {
        responsive: true, maintainAspectRatio: false, animation: false, parsing: false,
        interaction: { mode: 'nearest', intersect: false },
        plugins: { legend: { display: false }, tooltip: { filter: (i) => i.datasetIndex === 0, callbacks: { title: (it) => new Date(it[0].parsed.x).toLocaleTimeString(), label: (it) => `Score ${it.parsed.y}` } } },
        scales: {
          x: { type: 'linear', ticks: { color: text, maxTicksLimit: 6, callback: (v) => new Date(v).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }, grid: { color: grid } },
          y: { min: 0, max: 100, ticks: { color: text, stepSize: 25 }, grid: { color: grid } },
        },
      },
    });
  }

  // -------------------------------------------------------------- timeline
  function renderTimeline() {
    const f = $('#ev-filter').value;
    const list = events.filter((e) => {
      if (f === 'high') return e.severity === 'high' || e.severity === 'critical';
      if (f === 'pending') return e.review === 'pending';
      if (f === 'evidence') return !!e.evidence;
      return true;
    }).slice().reverse();
    const ul = $('#timeline');
    ul.innerHTML = list.map((e) => {
      const details = Object.entries(e.details || {}).filter(([k]) => k !== 'demo').slice(0, 5).map(([k, v]) => `${esc(k)}: ${esc(typeof v === 'object' ? JSON.stringify(v) : v)}`).join(' · ');
      return `<li class="${e.review === 'dismissed' ? 'dismissed' : ''}" data-id="${e.id}">
        <div class="time">${App.fmt.time(e.at)}</div>
        <div>
          <div class="ev-title">${esc(e.label)} <span class="badge sev-${e.severity}">${e.severity}</span>
            ${e.review === 'confirmed' ? '<span class="badge badge-red">Confirmed</span>' : e.review === 'dismissed' ? '<span class="badge">Dismissed</span>' : ''}</div>
          ${e.message && e.message !== e.label ? `<div class="small">${esc(e.message)}</div>` : ''}
          <div class="ev-meta">${esc(e.source)} · confidence ${Math.round((e.confidence || 0) * 100)}%${details ? ' · ' + details : ''}</div>
          <div class="row mt-1">
            <button class="btn btn-sm" data-review="confirmed" ${e.review === 'confirmed' ? 'disabled' : ''}><i data-lucide="check"></i> Confirm</button>
            <button class="btn btn-sm" data-review="dismissed" ${e.review === 'dismissed' ? 'disabled' : ''}><i data-lucide="x"></i> Dismiss</button>
            ${e.review !== 'pending' ? '<button class="btn btn-sm btn-ghost" data-review="pending">Undo</button>' : ''}
          </div>
        </div>
        ${e.evidence ? `<img class="thumb" src="${e.evidence}" data-zoom="${e.evidence}" alt="Evidence">` : '<span></span>'}
      </li>`;
    }).join('');
    $('#timeline-empty').hidden = list.length > 0;
    App.icons();
  }

  $('#ev-filter').addEventListener('change', renderTimeline);
  $('#timeline').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-review]');
    if (!btn) return;
    const id = Number(btn.closest('li').dataset.id);
    try {
      const r = await App.api(cfg.review.replace('/0/', `/${id}/`), { status: btn.dataset.review });
      const ev = events.find((x) => x.id === id);
      if (ev) ev.review = btn.dataset.review;
      renderTimeline();
      paintScore(r.score, r.risk);
      App.toast(`Event ${btn.dataset.review === 'pending' ? 'reset' : btn.dataset.review}. Score recalculated: ${r.score}.`, 'success', 2500);
      refresh(true);
    } catch (err) { App.toast(err.message, 'error'); }
  });

  // ------------------------------------------------------------ live state
  function paintScore(score, risk, peak) {
    $('#ring').innerHTML = `<div class="score-ring lg" style="--v:${score};--c:${App.riskColor(risk)}"><b>${Math.round(score)}</b></div>`;
    $('#risk-text').textContent = `${risk[0].toUpperCase()}${risk.slice(1)} risk`;
    if (peak != null) $('#peak').textContent = Math.round(peak);
  }

  function paintBreakdown(b) {
    const entries = Object.entries(b || {});
    const max = Math.max(1, ...entries.map(([, v]) => v));
    $('#breakdown').innerHTML = entries.length ? entries.map(([k, v]) => `<div class="bar-row"><span style="text-transform:capitalize">${esc(k)}</span><div class="meter"><span style="width:${Math.round(100 * v / max)}%"></span></div><span class="num small">${v}</span></div>`).join('') : '<div class="small muted">No contributing events.</div>';
  }

  function paintMessages(msgs) {
    $('#messages').innerHTML = msgs.length ? msgs.map((m) => `<div><div class="row between"><span class="badge ${m.kind === 'warning' ? 'badge-amber' : m.kind === 'system' ? 'badge-red' : 'badge-blue'}">${esc(m.sender)}</span><span class="tiny muted">${App.fmt.time(m.at)} · ${m.delivered ? 'seen' : 'pending'}</span></div><div class="mt-1">${esc(m.body)}</div></div>`).join('<hr style="margin:6px 0">') : '<span class="muted">No messages sent.</span>';
  }

  async function refresh(full = false) {
    try {
      const r = await App.api(`${cfg.live}?since=${full ? 0 : lastId}`);
      if (full) events = r.events; else events = events.concat(r.events);
      if (r.events.length || full) { lastId = Math.max(lastId, ...events.map((e) => e.id), 0); renderTimeline(); }
      const s = r.session;
      paintScore(s.score, s.risk, s.peak);
      $('#ev-count').textContent = events.length;
      paintBreakdown(r.breakdown);
      paintMessages(r.messages);
      drawChart(r.points);
      if (s.frame) { $('#latest-frame').src = s.frame; $('#latest-meta').textContent = App.fmt.ago(s.last_seen); }
      if (s.identity != null) $('#id-sim').textContent = `Latest similarity ${s.identity.toFixed(3)}`;
      const rem = $('#remaining'); if (rem) rem.textContent = s.status === 'in_progress' ? `${App.fmt.clock(s.remaining)} remaining` : s.status_label;
      if (cfg.active && s.status !== 'in_progress' && s.status !== 'pending_approval') { cfg.active = false; setTimeout(() => location.reload(), 800); }
      if (cfg.wasPending && s.status === 'in_progress') location.reload();
      cfg.wasPending = s.status === 'pending_approval';
    } catch (e) { /* retry */ }
  }

  // --------------------------------------------------------------- actions
  document.querySelectorAll('[data-action]').forEach((b) => b.addEventListener('click', async () => {
    const a = b.dataset.action;
    try {
      if (a === 'message') {
        const body = await App.modal({ title: 'Send a warning to the student', body: 'The message appears as a pop-up in the exam room.', icon: 'message-square-warning', confirmText: 'Send', input: { multiline: true, value: 'Please keep your face visible and your eyes on the screen.' } });
        if (body) { await App.api(cfg.message, { body }); App.toast('Warning sent.', 'success'); }
      } else if (a === 'terminate') {
        const reason = await App.modal({ title: 'Terminate this exam?', body: 'The student is removed immediately and their answers so far are submitted. This cannot be undone.', icon: 'octagon-x', danger: true, confirmText: 'Terminate exam', input: { multiline: true, placeholder: 'Reason shown to the student', value: 'Terminated due to suspected malpractice.' } });
        if (reason !== null) { await App.api(cfg.terminate, { reason }); location.reload(); }
      } else if (a === 'approve') {
        const ok = await App.modal({ title: 'Approve identity?', body: 'Confirm the start photo shows the registered student. Their timer starts now.', icon: 'user-check', confirmText: 'Approve' });
        if (ok) { await App.api(cfg.approve, {}); location.reload(); }
      } else if (a === 'flag') {
        const note = await App.modal({ title: 'Flag this session', body: 'Adds a confirmed proctor event, which raises the fraud score.', icon: 'flag', confirmText: 'Flag', input: { placeholder: 'What did you observe?' } });
        if (note !== null) { await App.api(cfg.flag, { note }); refresh(true); App.toast('Session flagged.', 'success'); }
      } else if (a === 'extend') {
        const m = await App.modal({ title: 'Give extra time', body: 'Minutes to add (1–120):', icon: 'timer-reset', confirmText: 'Add time', input: { type: 'number', value: '10' } });
        if (m) { await App.api(cfg.extend, { minutes: Number(m) }); App.toast(`Added ${m} minutes.`, 'success'); refresh(); }
      }
    } catch (e) { App.toast(e.message, 'error'); }
  }));

  cfg.wasPending = false;
  refresh(true);
  if (cfg.active) setInterval(refresh, 3000);
})();
