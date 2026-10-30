/* Proctor live monitor: tiles sorted by risk + real-time alert feed. */
(function () {
  const cfg = window.LIVE;
  const $ = (s) => document.querySelector(s);
  const esc = App.escapeHtml;
  let lastAlert = 0;
  let firstAlertLoad = true;
  const filter = $('#exam-filter');

  function ring(score, risk) {
    return `<div class="score-ring sm" style="--v:${score};--c:${App.riskColor(risk)}"><b>${Math.round(score)}</b></div>`;
  }

  function tile(s) {
    const pending = s.status === 'pending_approval';
    const frame = s.frame ? `<img src="${s.frame}" alt="Live frame of ${esc(s.student)}" loading="lazy">` : `<div class="cam-placeholder"><div><div class="avatar lg" style="margin:0 auto">${esc(s.initials)}</div><div class="small mt-1">Waiting for first frame…</div></div></div>`;
    const conn = s.connection_lost ? '<span class="badge badge-red">Offline</span>' : `<span class="badge">${App.fmt.ago(s.last_seen)}</span>`;
    const faces = s.faces == null ? '' : `<span class="badge">${s.faces === 1 ? '1 face' : s.faces + ' faces'}</span>`;
    const ident = s.identity == null ? '' : `<span class="badge">ID ${s.identity.toFixed(2)}</span>`;
    const last = s.last_event ? `<span class="badge sev-${s.last_event.severity}">${esc(s.last_event.severity)}</span><span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(s.last_event.label)}</span><span class="muted tiny" style="margin-left:auto">${App.fmt.time(s.last_event.at)}</span>` : '<span class="muted">No events yet</span>';
    return `<a class="card tile r-${s.risk}" href="${s.url}">
      <div class="shot">${frame}<div class="ov"><span class="row" style="gap:6px">${faces}${ident}</span>${pending ? '<span class="badge badge-amber">Needs approval</span>' : conn}</div></div>
      <div class="info">${ring(s.score, s.risk)}<div class="who"><b>${esc(s.student)}</b><small>${esc(s.exam)}${s.student_code ? ' · ' + esc(s.student_code) : ''}</small></div>
        <div class="center"><div class="mono strong">${pending ? '--:--' : App.fmt.clock(s.remaining)}</div><div class="tiny muted">${s.events} events</div></div></div>
      <div class="last">${last}</div></a>`;
  }

  async function loadSessions() {
    try {
      const q = filter.value ? `?exam_id=${filter.value}` : '';
      const r = await App.api(cfg.sessions + q);
      $('#tiles').innerHTML = r.sessions.map(tile).join('');
      $('#tiles-empty').hidden = r.sessions.length > 0;
      $('#c-total').textContent = r.total;
      $('#c-low').textContent = r.counts.low;
      $('#c-mid').textContent = r.counts.medium + r.counts.high;
      $('#c-crit').textContent = r.counts.critical;
      $('#live-indicator').className = 'badge badge-green pulse';
    } catch (e) {
      $('#live-indicator').className = 'badge badge-red';
    }
  }

  function beep() {
    if (!$('#sound').checked) return;
    try {
      const ctx = new AudioContext();
      const o = ctx.createOscillator(); const g = ctx.createGain();
      o.frequency.value = 880; o.connect(g); g.connect(ctx.destination);
      g.gain.setValueAtTime(0.15, ctx.currentTime); g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
      o.start(); o.stop(ctx.currentTime + 0.4);
    } catch (e) { /* audio blocked */ }
  }

  async function loadAlerts() {
    try {
      const r = await App.api(`${cfg.alerts}?since=${lastAlert}`);
      const box = $('#alerts');
      if (r.alerts.length) {
        if (lastAlert === 0) box.innerHTML = '';
        const html = r.alerts.map((a) => `<a class="alert-item ${a.severity} ${firstAlertLoad ? '' : 'new'}" href="${a.url}"><span class="bar"></span>
          ${a.evidence ? `<img src="${a.evidence}" alt="" style="width:56px;height:42px;object-fit:cover;border-radius:6px">` : ''}
          <div style="min-width:0"><div class="strong small">${esc(a.label)}</div><div class="tiny muted">${esc(a.student)} · ${App.fmt.time(a.at)}</div></div>
          <span class="badge sev-${a.severity}" style="margin-left:auto">${a.severity}</span></a>`).join('');
        box.insertAdjacentHTML('afterbegin', html);
        if (!firstAlertLoad && r.alerts.some((a) => a.severity === 'critical')) beep();
        if (!firstAlertLoad) App.toast(`${r.alerts[0].student}: ${r.alerts[0].label}`, 'error', 4000);
        lastAlert = Math.max(lastAlert, ...r.alerts.map((a) => a.id));
      } else if (lastAlert === 0) {
        box.innerHTML = '<div class="empty small"><i data-lucide="bell-off"></i><div>No high-severity alerts in the last 6 hours.</div></div>';
        App.icons();
      }
      firstAlertLoad = false;
      $('#alert-updated').textContent = 'updated ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    } catch (e) { /* retry next tick */ }
  }

  filter.addEventListener('change', () => {
    const u = new URL(location.href);
    filter.value ? u.searchParams.set('exam_id', filter.value) : u.searchParams.delete('exam_id');
    history.replaceState(null, '', u);
    loadSessions();
  });

  loadSessions(); loadAlerts();
  setInterval(loadSessions, 3000);
  setInterval(loadAlerts, 4000);
})();
